"""End-to-end: archive on disk to rendered departure board.

Each test drives the real pipeline — ingest, fetch, match, predict, serve —
with the network replaced by a recorded or synthesised SIRI response. Nothing
here reaches BODS, so the suite runs offline and in the Nix sandbox.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests
from starlette.testclient import TestClient

from ontime import config, db, eta, history, ingest, web
from ontime.matching import LONDON

from .conftest import (
    MINI_GTFS,
    STOP_50,
    STOP_192,
    STOP_ABSENT,
    WIDE_LOOKBACK_HOURS,
    any_trip_serving,
    load_trip,
    siri_document,
    vehicle_on_trip,
)

pytestmark = pytest.mark.e2e


@pytest.fixture
def feed(monkeypatch):
    """Replace the network with a controllable SIRI response."""
    holder = {"payload": siri_document([])}

    class FakeResponse:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *_a, **_k: FakeResponse(holder["payload"]))
    return holder


@pytest.fixture
def live_app(data_dir, feed, api_key, monkeypatch):
    """A fully wired application over a temporary database."""
    shutil.copy(MINI_GTFS, config.GTFS_ZIP)
    ingest.build()
    monkeypatch.setattr(web, "state", web.State())
    # The segments report is cached in a module global, which outlives the
    # temporary database it was built from.
    monkeypatch.setattr(web, "_segments_cache", None)
    return feed


NODE = shutil.which("node")

# The page's own script, run over a board payload with a stub document. Only
# innerHTML and textContent are captured, because those two are the entire
# boundary between server-supplied text and the DOM.
RENDER_HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes = {};
globalThis.document = {
  getElementById: id => (nodes[id] = nodes[id] || { innerHTML: '', textContent: '' }),
};
// The page starts polling as it loads. Leaving that promise pending keeps the
// render under test the only thing that writes to the stub document.
globalThis.fetch = () => new Promise(() => {});
globalThis.setInterval = () => 0;
globalThis.__board = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
(0, eval)(script + '\nrender(__board);');
const read = id => nodes[id] || { innerHTML: '', textContent: '' };
process.stdout.write(JSON.stringify({
  err: read('err').innerHTML,
  grid: read('grid').innerHTML,
  meta: read('meta').textContent,
  foot: read('foot').textContent,
}));
"""


def render_dashboard(board: dict, tmp_path: Path) -> dict:
    """What the shipped page writes into the DOM for a given board payload."""
    if not NODE:
        pytest.skip("node is not installed")
    harness = tmp_path / "harness.js"
    harness.write_text(RENDER_HARNESS)
    payload = tmp_path / "board.json"
    payload.write_text(json.dumps(board))
    out = subprocess.run(
        [NODE, str(harness), str(web.DASHBOARD), str(payload)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout)


def board_for(client, atco: str) -> dict:
    body = client.get("/api/board").json()
    return next(s for s in body["stops"] if s["atco"] == atco)


def live_departure(client, atco: str, vehicle: str = "TEST01") -> dict:
    """The board row backed by a given live vehicle."""
    return next(d for d in board_for(client, atco)["departures"] if d["vehicle"] == vehicle)


class TestPipeline:
    def test_vehicle_on_a_real_trip_reaches_the_board(self, live_app, monkeypatch):
        conn = db.connect()
        today = datetime.now(LONDON).date()
        trip = load_trip(conn, any_trip_serving(conn, STOP_192), today)
        target = next(i for i, s in enumerate(trip.stops) if s[1] == STOP_192)
        live_app["payload"] = siri_document([vehicle_on_trip(trip, max(0, target - 5))])
        conn.close()

        with TestClient(web.app) as client:
            stop = board_for(client, STOP_192)

        live = [d for d in stop["departures"] if d["vehicle"] == "TEST01"]
        assert len(live) == 1
        assert live[0]["route"] == "192"
        assert live[0]["minutes"] >= 0
        assert live[0]["source"] in {"timetable", "learned"}
        assert live[0]["stops_away"] == min(5, target)

    def test_stale_vehicles_never_appear(self, live_app):
        conn = db.connect()
        today = datetime.now(LONDON).date()
        trip = load_trip(conn, any_trip_serving(conn, STOP_192), today)
        conn.close()

        live_app["payload"] = siri_document(
            [
                vehicle_on_trip(
                    trip, 2, now=datetime.now(UTC) - timedelta(hours=8), vehicle_ref="GHOST"
                )
            ]
        )
        with TestClient(web.app) as client:
            body = client.get("/api/board").json()

        assert body["counts"]["feed"] == 0
        assert all(d["vehicle"] != "GHOST" for s in body["stops"] for d in s["departures"])

    def test_board_falls_back_to_the_timetable_with_no_vehicles(self, live_app):
        with TestClient(web.app) as client:
            body = client.get("/api/board").json()

        assert body["counts"]["matched"] == 0
        sources = {d["source"] for s in body["stops"] for d in s["departures"]}
        assert sources <= {"scheduled"}

    def test_stop_absent_from_the_archive_renders_empty(self, live_app):
        with TestClient(web.app) as client:
            stop = board_for(client, STOP_ABSENT)
        assert stop["departures"] == []
        assert stop["naptan"] == "MANADGMT"

    def test_observations_are_persisted_for_learning(self, live_app):
        conn = db.connect()
        today = datetime.now(LONDON).date()
        trip = load_trip(conn, any_trip_serving(conn, STOP_50), today)
        conn.close()
        live_app["payload"] = siri_document([vehicle_on_trip(trip, 1)])

        with TestClient(web.app) as client:
            client.get("/api/board")

        conn = db.connect()
        rows = list(conn.execute("SELECT * FROM observations"))
        conn.close()
        assert len(rows) == 1
        assert rows[0]["trip_id"] == trip.trip_id
        assert rows[0]["route_name"] == "50"

    def test_departures_are_ordered_by_arrival(self, live_app):
        with TestClient(web.app) as client:
            for stop in client.get("/api/board").json()["stops"]:
                etas = [d["eta_ts"] for d in stop["departures"]]
                assert etas == sorted(etas)


class TestLearningImprovesPredictions:
    SLOW_PACE_SECS = 300

    def _seed_slow_history(self, conn, trip, pivot_index: int, runs: int = 10) -> None:
        """Record past runs that crawl through the segment after `pivot_index`.

        Learned times are bucketed by the local hour the vehicle *left* the
        upstream stop, and prediction reads the bucket for the current hour.
        Each past run is therefore anchored so that its departure from
        `pivot_index` falls on this hour, on an earlier day.
        """
        from .test_history import FakeVehicle

        now = datetime.now(UTC)
        for run in range(1, runs + 1):
            anchor = now - timedelta(days=run)
            for i, (_s, _sid, _a, lat, lon) in enumerate(trip.stops):
                when = anchor + timedelta(seconds=(i - pivot_index) * self.SLOW_PACE_SECS)
                history.record(conn, FakeVehicle(f"H{run}", when, lat, lon), trip.trip_id)
        conn.commit()
        history.derive_stop_events(
            conn, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
        )
        assert history.learn_segments(conn) > 0
        assert history.load_segment_stats(conn), "segments must clear MIN_SAMPLES"

    def test_accumulated_history_changes_the_estimate(self, live_app):
        """Ten past runs crawling at five minutes a stop push the estimate out."""
        conn = db.connect()
        today = datetime.now(LONDON).date()
        trip = load_trip(conn, any_trip_serving(conn, STOP_192), today)
        target = next(i for i, s in enumerate(trip.stops) if s[1] == STOP_192)
        assert target >= 1, "fixture trip must have an upstream stop"
        pivot = target - 1
        live_app["payload"] = siri_document([vehicle_on_trip(trip, pivot)])
        conn.close()

        with TestClient(web.app) as client:
            before = live_departure(client, STOP_192)
        assert before["source"] == "timetable"
        assert before["coverage"] == 0.0

        conn = db.connect()
        self._seed_slow_history(conn, trip, pivot)
        conn.close()

        with TestClient(web.app) as client:
            after = live_departure(client, STOP_192)

        assert after["source"] == "learned"
        assert after["coverage"] == pytest.approx(1.0)
        assert after["minutes"] == pytest.approx(self.SLOW_PACE_SECS / 60, abs=0.6)
        assert after["minutes"] > before["minutes"]


class TestDashboardRendering:
    """The shipped page, driven under Node over payloads the server can produce."""

    HOSTILE = "<img src=x onerror=alert(1)>"

    def test_feed_text_cannot_execute_in_the_page(self, live_app, tmp_path):
        """Every row was built by interpolating feed strings into innerHTML.

        DestinationName is operator free text. It becomes `Prediction.headsign`
        whenever the GTFS trip carries no headsign of its own — common in real
        archives — and the board serialises it verbatim, so an `<img
        src=x onerror=…>` in that field used to run in the browser.
        """
        conn = db.connect()
        today = datetime.now(LONDON).date()
        trip = load_trip(conn, any_trip_serving(conn, STOP_192), today)
        target = next(i for i, s in enumerate(trip.stops) if s[1] == STOP_192)
        conn.execute("UPDATE trips SET headsign = '' WHERE trip_id = ?", (trip.trip_id,))
        conn.commit()
        conn.close()

        live_app["payload"] = siri_document(
            [
                vehicle_on_trip(trip, max(0, target - 2)).replace(
                    "<DestinationName>Test_Destination</DestinationName>",
                    "<DestinationName>&lt;img src=x onerror=alert(1)&gt;</DestinationName>",
                )
            ]
        )
        with TestClient(web.app) as client:
            board = client.get("/api/board").json()

        row = next(
            d for s in board["stops"] for d in s["departures"] if d["vehicle"] == "TEST01"
        )
        assert row["headsign"] == self.HOSTILE, "the hostile text must reach the client"

        grid = render_dashboard(board, tmp_path)["grid"]
        assert "<img" not in grid, "feed text was rendered as markup"
        assert self.HOSTILE not in grid
        assert "&lt;img src=x onerror=alert(1)&gt;" in grid, "…but is still shown as text"

    def test_a_failed_first_poll_renders_an_honest_empty_state(self, tmp_path):
        """Before any successful poll the board has no counts and no timestamp.

        `fmtClock(null)` computes `new Date(0)` and prints it as a plausible
        clock time, so the header read `updated 01:00 · undefined/undefined
        vehicles matched` — a 1970 reading dressed up as a fresh board.
        """
        out = render_dashboard(
            {"stops": [], "updated": None, "error": web.POLL_ERROR}, tmp_path
        )
        assert out["meta"] == "no data yet"
        assert "undefined" not in out["meta"]
        assert web.POLL_ERROR in out["err"]


class TestHttpApi:
    def test_index_serves_the_dashboard(self, live_app):
        with TestClient(web.app) as client:
            r = client.get("/")
        assert r.status_code == 200
        assert "ontime" in r.text
        assert "/api/board" in r.text

    def test_stops_endpoint(self, live_app):
        with TestClient(web.app) as client:
            body = client.get("/api/stops").json()
        assert {s["naptan"] for s in body} == {"MANADGMT", "MANGPWTD", "MANADTDW"}

    def test_healthz_reports_ready_after_a_poll(self, live_app):
        with TestClient(web.app) as client:
            client.get("/api/board")
            r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_board_shape_is_stable(self, live_app):
        with TestClient(web.app) as client:
            body = client.get("/api/board").json()
        assert set(body) >= {"stops", "vehicles", "updated", "counts", "history"}
        assert [s["atco"] for s in body["stops"]] == [s.atco for s in config.STOPS]

    def test_no_endpoint_leaks_the_key(self, live_app, api_key):
        with TestClient(web.app) as client:
            for path in ("/", "/api/board", "/api/stops", "/healthz"):
                assert api_key not in client.get(path).text

    def test_feed_failure_is_reported_without_internals(
        self, live_app, api_key, monkeypatch, caplog
    ):
        """Both endpoints used to echo `str(exc)` to whoever asked.

        The board has no authentication by design, so the exception text —
        container paths, SQL fragments, driver internals — was readable by
        anything that could reach the port. The detail belongs in the log.
        """
        detail = f"HTTPSConnectionPool /data/ontime.sqlite ?api_key={api_key}"

        def boom(*_a, **_k):
            raise requests.RequestException(detail)

        monkeypatch.setattr(requests, "get", boom)
        with caplog.at_level("ERROR"), TestClient(web.app) as client:
            body = client.get("/api/board").json()
            health = client.get("/healthz")

        assert body["error"] == web.POLL_ERROR
        assert health.json()["error"] == web.POLL_ERROR
        for text in (body["error"], health.text):
            assert api_key not in text
            assert "/data/ontime.sqlite" not in text
            assert "HTTPSConnectionPool" not in text
        assert "/data/ontime.sqlite" in caplog.text, "the detail must survive in the log"

    @pytest.mark.parametrize("path", ("/", "/segments"))
    def test_the_page_locks_itself_down_with_a_policy_it_satisfies(self, live_app, path):
        """An escaping slip should not be one bug away from script execution.

        A policy that allowed 'unsafe-inline' would be decoration, since that
        is exactly what an injected event handler needs; hashing the blocks the
        page ships with permits those two and nothing else. Recomputed here
        from the served body, because a policy the page cannot satisfy is a
        blank screen.

        Every page the server serves is checked, not just the board: hashes are
        per-file, so a second page means a second policy, and a second policy is
        somewhere for a weaker one to hide.
        """
        with TestClient(web.app) as client:
            r = client.get(path)
        csp = r.headers["content-security-policy"]

        assert "default-src 'none'" in csp
        assert "'unsafe-inline'" not in csp, "a hash source is ignored beside it"
        assert "frame-ancestors 'none'" in csp
        assert "connect-src 'self'" in csp, "the page fetches from its own origin"

        for tag in ("script", "style"):
            blocks = re.findall(rf"<{tag}>(.*?)</{tag}>", r.text, re.DOTALL)
            assert blocks, f"expected an inline <{tag}> to hash"
            for block in blocks:
                digest = base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
                assert f"'sha256-{digest}'" in csp, f"inline <{tag}> is not permitted"

        # A hash covers a <style> element but never a style="" attribute.
        assert 'style="' not in r.text

    def test_the_policy_admits_the_basemap_and_nothing_further(self, live_app):
        """The tiles are the only third party the page touches.

        `img-src` is derived from `config.MAP_TILE_URL` rather than written out
        again, so this checks the two agree — a policy that disagrees with the
        tile source fails as a blank grey map with nothing in the log.
        """
        with TestClient(web.app) as client:
            csp = client.get("/").headers["content-security-policy"]

        img = next(d for d in csp.split("; ") if d.startswith("img-src "))
        assert web.tile_origin(config.MAP_TILE_URL) in img
        assert "https://tile.openstreetmap.org" in img
        # Tiles are <img> loads. Nothing on the page opens a socket elsewhere.
        assert "connect-src 'self'" in csp
        for directive in csp.split("; "):
            if directive.startswith(("img-src", "script-src", "style-src")):
                continue
            assert "http" not in directive, f"{directive} reaches off-origin"

    def test_vendored_leaflet_is_served_and_the_page_asks_for_what_exists(self, live_app):
        """The page hardcodes these paths; a rename would 404 in the browser only."""
        with TestClient(web.app) as client:
            page = client.get("/").text
            for name, media in (("leaflet.js", "javascript"), ("leaflet.css", "css")):
                assert f"/vendor/{name}" in page, f"the page never loads {name}"
                r = client.get(f"/vendor/{name}")
                assert r.status_code == 200
                assert media in r.headers["content-type"]
                assert r.headers["x-content-type-options"] == "nosniff"

    def test_everything_the_server_serves_is_packaged(self):
        """The image installs the package; it does not copy the tree.

        So a file the server serves but `package-data` does not list works in a
        checkout and 404s in the container — which is where the map runs. This
        checks the globs cover every static file, rather than trusting that
        whoever adds one remembers to widen them.
        """
        import fnmatch
        import tomllib

        pyproject = tomllib.loads((config.ROOT / "pyproject.toml").read_text())
        globs = pyproject["tool"]["setuptools"]["package-data"]["ontime"]

        served = sorted(p for p in web.STATIC.rglob("*") if p.is_file())
        assert served, "no static files found at all"
        for path in served:
            rel = path.relative_to(web.STATIC.parent).as_posix()
            assert any(fnmatch.fnmatch(rel, g) for g in globs), (
                f"{rel} is served but no package-data glob in {globs} ships it"
            )

    def test_vendor_route_serves_only_the_two_files_it_knows(self, live_app):
        """Named files rather than a directory mount, so there is nothing to walk."""
        with TestClient(web.app) as client:
            for name in ("leaflet.js.map", "../dashboard.html", "%2e%2e%2fdashboard.html"):
                assert client.get(f"/vendor/{name}").status_code == 404

    def test_json_routes_refuse_to_be_run_as_script(self, live_app):
        """'self' in script-src makes every route here a candidate <script src>.

        These carry operator free text, so the content type has to be one the
        browser will not second-guess.
        """
        with TestClient(web.app) as client:
            for path in (
                "/api/board",
                "/api/map",
                "/api/stops",
                "/api/segments",
                "/healthz",
            ):
                r = client.get(path)
                assert r.headers["x-content-type-options"] == "nosniff", path
                assert r.headers["content-type"].startswith("application/json"), path

    def test_map_endpoint_carries_the_basemap_and_route_lines(self, live_app):
        with TestClient(web.app) as client:
            client.get("/api/board")  # the lines are built when the timetable loads
            body = client.get("/api/map").json()

        assert body["tile_url"] == config.MAP_TILE_URL
        assert body["attribution"], "OpenStreetMap's licence requires the credit"
        assert body["routes"], "the mini archive serves at least one route"
        for line in body["routes"]:
            assert len(line["points"]) >= 2, "a line needs two points to be a line"
            for lat, lon in line["points"]:
                assert 53.0 < lat < 54.0 and -3.0 < lon < -1.5, "outside Greater Manchester"

    def test_segments_endpoint_reports_the_model_the_board_is_using(self, live_app):
        with TestClient(web.app) as client:
            body = client.get("/api/segments").json()

        gate = body["gate"]
        assert gate["min_samples"] == history.MIN_SAMPLES, (
            "the page must quote the live gate"
        )
        totals = body["totals"]
        assert (
            totals["observed"]
            >= totals["stored"]
            >= totals["used"]
            >= totals["significant"]
        )
        assert totals["scheduled_cells"] > 0, "the timetable gives coverage a denominator"
        assert totals["covered"] <= totals["scheduled_cells"]
        assert len(body["segments"]) == totals["observed"]

    def test_segments_report_is_cached_rather_than_rebuilt_per_view(self, live_app):
        """It reads all of stop_events; a refresh loop must not re-run that."""
        with TestClient(web.app) as client:
            first = client.get("/api/segments").json()
            second = client.get("/api/segments").json()
        assert second["age_secs"] >= first["age_secs"]
        assert second["totals"] == first["totals"]

    def test_the_segments_page_asks_for_the_endpoint_that_exists(self, live_app):
        """The page names this path itself; a rename would 404 in the browser only."""
        with TestClient(web.app) as client:
            page = client.get("/segments")
            assert page.status_code == 200
            assert "/api/segments" in page.text
            assert client.get("/api/segments").status_code == 200
        assert "text/html" in page.headers["content-type"]

    def test_board_places_every_stop_the_archive_knows(self, live_app):
        """The map cannot pin a stop the board does not locate.

        The cache holds coordinates only for stops the watched trips call at,
        and `STOP_ABSENT` is served by no trip in the mini archive — so it is
        reported unplaced. Null rather than (0, 0): a pin in the Atlantic is a
        worse answer than no pin, and the page filters on exactly this.
        """
        with TestClient(web.app) as client:
            stops = {s["atco"]: s for s in client.get("/api/board").json()["stops"]}

        assert stops[STOP_ABSENT]["lat"] is None
        assert stops[STOP_ABSENT]["lon"] is None
        for atco in (STOP_50, STOP_192):
            assert 53.0 < stops[atco]["lat"] < 54.0, stops[atco]
            assert -3.0 < stops[atco]["lon"] < -1.5, stops[atco]

    def test_a_rebuilt_timetable_re_reads_the_stop_positions(self, live_app):
        """A stop gaining its first trip gains its first position with it.

        The positions are cached because they cannot change between polls, and
        a partial answer — which is what the mini archive produces — would
        otherwise be held until the process restarted.
        """
        with TestClient(web.app) as client:
            client.get("/api/board")
            assert web.state.stop_points, "positions should be cached after a poll"

            web.state.trips_built = "forces a reload on the next poll"
            conn = db.connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?)",
                    (STOP_ABSENT, "MANADGMT", "Hyde Grove", 53.4641, -2.2223),
                )
                conn.commit()
            finally:
                conn.close()

            web.state.board = web.poll_once()
            stops = {s["atco"]: s for s in web.state.board["stops"]}

        assert stops[STOP_ABSENT]["lat"] == pytest.approx(53.4641)

    def test_a_bus_exactly_on_time_is_not_reported_as_unknown(self, live_app, monkeypatch):
        """A delay of 0.0 is falsy, and null on this field means "not known".

        So a bus running exactly to time rendered blank while one twenty
        seconds late rendered "on time" — fact 15 read backwards.
        """
        conn = db.connect()
        today = datetime.now(LONDON).date()
        trip = load_trip(conn, any_trip_serving(conn, STOP_192), today)
        target = next(i for i, s in enumerate(trip.stops) if s[1] == STOP_192)
        live_app["payload"] = siri_document([vehicle_on_trip(trip, max(0, target - 5))])
        conn.close()

        predict = eta.predict

        def exactly_on_time(*args, **kwargs):
            p = predict(*args, **kwargs)
            return dataclasses.replace(p, delay_secs=0.0) if p else p

        monkeypatch.setattr(web.eta, "predict", exactly_on_time)
        with TestClient(web.app) as client:
            row = live_departure(client, STOP_192)

        assert row["delay_mins"] == 0


class TestColdStart:
    """The web process comes up beside the ingest, not after it.

    On a cold volume the first ingest takes around 90 seconds, so the first
    polls read a database with no trips in it. Caching that empty result for
    the day is indistinguishable on the board from a genuinely quiet evening.
    """

    def test_an_empty_timetable_is_not_cached_for_the_day(
        self, data_dir, feed, api_key, monkeypatch
    ):
        monkeypatch.setattr(web, "state", web.State())
        conn = db.connect()
        db.init(conn)

        web.refresh_timetable(conn)
        assert web.state.trips == []
        assert web.state.trips_for is None, (
            "an empty load must leave the next poll to retry"
        )

        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        ingest.build()
        web.refresh_timetable(conn)
        conn.close()

        assert web.state.trips
        assert web.state.trips_for == datetime.now(LONDON).date()
        assert web.state.routes

    def test_the_board_says_the_timetable_is_missing(
        self, data_dir, feed, api_key, monkeypatch
    ):
        monkeypatch.setattr(web, "state", web.State())
        with TestClient(web.app) as client:
            body = client.get("/api/board").json()

        assert body["timetable"] == {"date": None, "trips": 0, "routes": []}
        assert all(s["departures"] == [] for s in body["stops"])

    def test_the_board_reports_a_loaded_timetable(self, live_app):
        with TestClient(web.app) as client:
            body = client.get("/api/board").json()

        assert body["timetable"]["trips"] > 0
        assert body["timetable"]["date"] == datetime.now(LONDON).date().isoformat()
        assert body["timetable"]["routes"]


class TestNightlyRebuild:
    """Both containers cross midnight together, and only one of them rebuilds.

    The rebuild takes minutes; this process polls every 15 seconds. It
    therefore reads the pre-rebuild database first, and caching that on the
    date alone pinned yesterday's trips until the process next restarted.
    """

    def _stamp(self, conn, value: str) -> None:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)", (value,))
        conn.commit()

    def test_a_rebuilt_cache_supersedes_the_loaded_timetable(self, live_app):
        conn = db.connect()
        today = datetime.now(LONDON).date()
        self._stamp(conn, (today - timedelta(days=1)).isoformat())

        web.refresh_timetable(conn)  # the poll that lands before the rebuild
        stale = len(web.state.trips)
        assert stale

        dropped = web.state.trips[0].trip_id
        conn.execute("DELETE FROM trips WHERE trip_id = ?", (dropped,))
        self._stamp(conn, today.isoformat())

        web.refresh_timetable(conn)
        conn.close()

        # One trip_id, two service days: the count drops by more than one.
        assert len(web.state.trips) < stale
        assert dropped not in {t.trip_id for t in web.state.trips}

    def test_an_unchanged_cache_is_not_reloaded_on_every_poll(self, live_app, monkeypatch):
        """`load_trips` reads every stop of every trip; it is not poll-cheap."""
        conn = db.connect()
        web.refresh_timetable(conn)
        assert web.state.trips

        calls = []
        monkeypatch.setattr(web, "load_trips", lambda *a, **_k: calls.append(a) or [])
        web.refresh_timetable(conn)
        web.refresh_timetable(conn)
        conn.close()

        assert calls == []


class TestContainerImage:
    """The Dockerfile is part of the deliverable and nothing else checks it."""

    def test_healthcheck_respects_the_configured_port(self):
        """ONTIME_PORT is an advertised override; the probe hardcoded 8000.

        An exec-form CMD has no shell to expand the variable, so anyone taking
        the documented override got a container that ran perfectly and
        reported itself unhealthy for ever.
        """
        dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile"
        # Comments stripped: a promise in prose is not a probe.
        lines = [ln for ln in dockerfile.read_text().splitlines() if not ln.startswith("#")]
        start = next(i for i, ln in enumerate(lines) if ln.startswith("HEALTHCHECK"))
        probe = "\n".join(lines[start : start + 2])

        assert "ONTIME_PORT" in probe
        assert "127.0.0.1:8000" not in probe


class TestResilience:
    def test_poller_survives_a_malformed_response(self, live_app):
        live_app["payload"] = b"<not-xml"
        with TestClient(web.app) as client:
            body = client.get("/api/board").json()
            assert client.get("/healthz").status_code in (200, 503)
        assert body.get("error")

    def test_empty_feed_is_not_an_error(self, live_app):
        live_app["payload"] = siri_document([])
        with TestClient(web.app) as client:
            body = client.get("/api/board").json()
        assert body["error"] is None
        assert body["counts"]["feed"] == 0


@pytest.mark.live
class TestAgainstTheRealFeed:
    """Deselected by default. Run with: pytest -m live"""

    def test_feed_still_carries_no_arrival_predictions(self):
        import os

        if not os.getenv("BODS_API_KEY"):
            pytest.skip("BODS_API_KEY not set")
        from ontime import siri

        vehicles = siri.fetch()
        assert vehicles, "expected at least one live vehicle in the bounding box"
        assert all(v.lat and v.lon for v in vehicles)

    def test_timetable_archive_is_reachable(self):
        r = requests.head(config.GTFS_URL, timeout=30, allow_redirects=True)
        assert r.status_code == 200
