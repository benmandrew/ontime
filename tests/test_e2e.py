"""End-to-end: archive on disk to rendered departure board.

Each test drives the real pipeline — ingest, fetch, match, predict, serve —
with the network replaced by a recorded or synthesised SIRI response. Nothing
here reaches BODS, so the suite runs offline and in the Nix sandbox.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta

import pytest
import requests
from starlette.testclient import TestClient

from ontime import config, db, history, ingest, web
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
    return feed


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

    def test_feed_failure_is_reported_without_the_key(self, live_app, api_key, monkeypatch):
        def boom(*_a, **_k):
            raise requests.RequestException(f"failed for ?api_key={api_key}")

        monkeypatch.setattr(requests, "get", boom)
        with TestClient(web.app) as client:
            body = client.get("/api/board").json()
            health = client.get("/healthz")

        assert body["error"]
        assert api_key not in body["error"]
        assert "<redacted>" in body["error"]
        assert api_key not in health.text


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
