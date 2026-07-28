"""Dashboard server: a background poller plus a small JSON and HTML frontend.

The API key never leaves this process. The browser talks only to this server,
which is why the page can be served over plain HTTP on the loopback interface
without exposing the credential.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import config, db, eta, history, locking, logs, segments, siri
from .matching import LONDON, Trip, load_trips, match

STATIC = Path(__file__).parent / "static"
log = logs.get("ontime.web")

# What clients are told when a poll fails. The exception behind it names
# container paths, SQL and driver internals; the board has no authentication by
# design, so anyone who can reach it would read all of that. The full text goes
# to the log, which is redacted and stays inside the container.
POLL_ERROR = "Live data is temporarily unavailable."


class State:
    """Everything the poller writes and the request handlers read."""

    def __init__(self) -> None:
        self.trips: list[Trip] = []
        self.trips_for: date | None = None
        self.trips_built: str | None = None
        self.warned_empty_for: date | None = None
        self.segments: dict = {}
        self.board: dict = {"stops": [], "updated": None, "error": None}
        self.vehicles: list = []
        self.last_poll: float | None = None
        self.routes: set[str] = set()
        self.stop_points: dict[str, tuple[float, float]] = {}
        # The map's route lines, rebuilt with the timetable rather than per poll.
        self.map_routes: list[dict] | None = None


state = State()


def _built_at(conn) -> str | None:
    """The service date the cached timetable in this database was built for."""
    row = conn.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
    return row["value"] if row else None


def refresh_timetable(conn) -> None:
    """Reload the day's trips when the date rolls over or the cache is rebuilt.

    The date alone is not enough, for the same reason `refresh_segments` does
    not use it. Both containers cross midnight together: this one polls every
    15 seconds while the rebuild takes minutes, so it reliably reads the
    pre-rebuild database, and keying the cache on the date alone would pin
    yesterday's trips until the process next restarted. The `built_at` stamp
    is written in the rebuild's own transaction, so it changes exactly when
    the new trips become visible — and reading one indexed row is cheap
    enough to do on every poll, which `load_trips` is not.

    An empty load is deliberately not cached. This process and the
    maintenance one start together, and against a cold volume the first
    ingest takes around 90 seconds, so the first few calls here read a
    database that has no trips in it yet. Stamping the date regardless would
    pin that empty list until the date next rolled over, and the failure is
    a quiet one: every stop reports nothing due, no vehicle in the feed ever
    matches a trip, and both look exactly like a genuinely quiet evening.
    """
    today = datetime.now(LONDON).date()
    built = _built_at(conn)
    if state.trips_for == today and state.trips_built == built:
        return
    days = (today, today - timedelta(days=1))
    trips = load_trips(conn, days)
    if not trips:
        # Every poll retries, so warn once a day rather than every 15s.
        if state.warned_empty_for != today:
            state.warned_empty_for = today
            log.warning("no timetable cached for %s yet, retrying", today)
        return
    state.trips = trips
    state.trips_for = today
    state.trips_built = built
    state.routes = {t.route_name for t in state.trips}
    state.map_routes = _route_lines(trips)
    # Both are read out of the timetable cache, so a rebuild is exactly when
    # they can change — including a watched stop gaining its first trip, and so
    # its first position. Without this the partial answer would be kept until
    # the process next restarted.
    state.stop_points = {}
    log.info(
        "timetable loaded: %d trips for %s, routes %s, cache built %s",
        len(state.trips),
        today,
        sorted(state.routes),
        built,
    )


def _route_lines(trips: list[Trip]) -> list[dict]:
    """One polyline per route: the longest stop sequence any of its trips runs.

    GTFS ships real road geometry in `shapes.txt`, but not for these operators.
    Every one of the watched trips carries an empty `shape_id`, as do 63% of the
    North West feed's 106,058 trips, so scanning that 131MB member would return
    nothing for this board. The line is therefore drawn through the stops
    themselves. Median stop spacing is 284m, which follows a straight road
    closely and cuts the corner at a bend: it is context beneath the pins, not a
    claim about which streets the bus uses.

    The longest variant per route is enough. Across the nine routes it covers
    every stop their other variants call at, bar two on the 192 and two on the
    53 — so a short working contributes nothing a rider would notice.
    """
    longest: dict[str, Trip] = {}
    for t in trips:
        best = longest.get(t.route_name)
        if best is None or len(t.stops) > len(best.stops):
            longest[t.route_name] = t
    return [
        {"route": name, "points": [[lat, lon] for _seq, _sid, _arr, lat, lon in trip.stops]}
        for name, trip in sorted(longest.items())
    ]


def refresh_stop_points(conn) -> None:
    """Coordinates for the watched stops, read once and kept.

    `config.STOPS` carries the codes and the human-readable text but no
    position; the coordinates come from GTFS `stops.txt` via the cache, which
    holds only the stops the watched trips call at — so a stop no cached trip
    serves has none, and the board reports it unplaced rather than at (0, 0).

    An empty result is deliberately not cached, for the reason
    `refresh_timetable` gives: against a cold volume the first polls read a
    database the ingest has not filled yet, and pinning that would leave the
    map permanently blank. A partial result is cached, but `refresh_timetable`
    clears it whenever the cache is rebuilt.
    """
    if state.stop_points:
        return
    placeholders = ",".join("?" * len(config.STOPS))
    rows = conn.execute(
        f"SELECT stop_id, lat, lon FROM stops WHERE stop_id IN ({placeholders})",
        tuple(s.atco for s in config.STOPS),
    ).fetchall()
    state.stop_points = {r["stop_id"]: (r["lat"], r["lon"]) for r in rows}


def refresh_segments(conn) -> None:
    """Reload learned segment times on every poll.

    These are rewritten by the maintenance container roughly hourly, and this
    process has no way of being told. Tying the reload to the timetable date
    instead would leave the dashboard using yesterday's model all day. The
    query returns a few thousand rows at most, so re-reading it every cycle
    costs less than tracking staleness would.
    """
    state.segments = history.load_segment_stats(conn)


def build_board(conn) -> dict:
    now = datetime.now(UTC).timestamp()
    vehicles = siri.fetch(routes=state.routes)

    preds: list[eta.Prediction] = []
    matched_ids: set[str] = set()
    live_vehicles = []

    for v in vehicles:
        found = match(v, state.trips)
        trip = found.trip if found else None
        history.record(conn, v, trip.trip_id if trip else None)
        if found is None or trip is None:
            continue
        matched_ids.add(trip.trip_id)
        live_vehicles.append(
            {
                "vehicle_ref": v.vehicle_ref,
                "route": v.route_name,
                "lat": v.lat,
                "lon": v.lon,
                "bearing": v.bearing,
                "headsign": trip.headsign or v.dest_name,
                "age_secs": round(v.age_secs),
            }
        )
        for stop in config.STOPS:
            p = eta.predict(
                v,
                trip,
                stop.atco,
                state.segments,
                now=now,
                schedule_confident=found.schedule_confident,
                # Matching already resolved where on this trip the vehicle is.
                pos_idx=found.pos_idx,
            )
            if p and p.minutes is not None and -1 <= p.minutes <= config.HORIZON_SECS / 60:
                preds.append(p)
    conn.commit()

    preds += eta.scheduled_only(state.trips, matched_ids, now, config.HORIZON_SECS)

    stops_out = []
    for stop in config.STOPS:
        rows = sorted(
            (p for p in preds if p.stop_id == stop.atco), key=lambda p: p.eta_ts or 0
        )[:12]
        point = state.stop_points.get(stop.atco)
        stops_out.append(
            {
                "atco": stop.atco,
                "naptan": stop.naptan,
                "name": stop.name,
                "detail": stop.detail,
                # None until the timetable cache exists. The map skips a stop it
                # cannot place rather than dropping a pin at (0, 0).
                "lat": point[0] if point else None,
                "lon": point[1] if point else None,
                "departures": [
                    {
                        "route": p.route_name,
                        "headsign": p.headsign,
                        "minutes": round(p.minutes) if p.minutes is not None else None,
                        "eta_ts": p.eta_ts,
                        "sched_ts": p.sched_ts,
                        # `is not None`, not truthiness: a delay of exactly zero
                        # is a bus running exactly to time, and the page reads
                        # null as "not known" (see the confidence rule in eta).
                        "delay_mins": (
                            round(p.delay_secs / 60) if p.delay_secs is not None else None
                        ),
                        "source": p.source,
                        "coverage": round(p.learned_coverage, 2),
                        "vehicle": p.vehicle_ref,
                        "stops_away": p.stops_away,
                    }
                    for p in rows
                ],
            }
        )

    return {
        "stops": stops_out,
        "vehicles": live_vehicles,
        "updated": now,
        "counts": {"feed": len(vehicles), "matched": len(live_vehicles)},
        # Without this the page cannot tell a quiet evening from a timetable
        # that never loaded: both render as an empty board at every stop.
        "timetable": {
            "date": state.trips_for.isoformat() if state.trips_for else None,
            "trips": len(state.trips),
            "routes": sorted(state.routes),
        },
        "history": history.stats_summary(conn),
        "error": None,
    }


def poll_once() -> dict:
    """One full cycle, run entirely on a worker thread.

    The connection is opened here rather than held across polls because
    sqlite3 refuses to share a connection between threads, and
    `asyncio.to_thread` does not guarantee the same worker each time.
    Connecting is cheap next to fetching and parsing the feed.
    """
    locking.heartbeat("poller")
    conn = db.connect()
    try:
        db.init(conn)
        refresh_timetable(conn)
        refresh_stop_points(conn)
        refresh_segments(conn)
        return build_board(conn)
    finally:
        conn.close()


async def poll_and_store() -> None:
    """Refresh `state.board`, surfacing failures rather than raising."""
    try:
        state.board = await asyncio.to_thread(poll_once)
        c = state.board["counts"]
        log.info("poll: feed=%d matched=%d", c["feed"], c["matched"])
    except Exception as exc:  # a bad poll must not stop the loop
        # Detail stays in the log, which is redacted and never leaves the
        # container; the board gets a fixed string. See POLL_ERROR.
        log.exception("poll failed: %s", config.redact(str(exc)))
        state.board = {**state.board, "error": POLL_ERROR}


async def poller() -> None:
    while True:
        await asyncio.sleep(config.POLL_SECS)
        await poll_and_store()


@contextlib.asynccontextmanager
async def lifespan(_app: Starlette):
    # Warm the board before accepting traffic, so the first request is not
    # answered with an empty page and /healthz means something immediately.
    await poll_and_store()
    task = asyncio.create_task(poller())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # Drop the heartbeat on the way out so a rebuild started straight after
        # a clean shutdown does not have to wait for it to age out.
        locking.release("poller")


def _json(body, status_code: int = 200) -> JSONResponse:
    """A JSON response the browser will not be talked into running as script.

    `script-src` gained 'self' when Leaflet was vendored, so every same-origin
    URL is now a permitted script source — including these, which carry operator
    free text straight from the feed. `nosniff` makes the browser honour the
    JSON content type and refuse to execute them.
    """
    return JSONResponse(body, status_code=status_code, headers=NOSNIFF)


async def api_board(_request: Request) -> JSONResponse:
    return _json(state.board)


async def api_map(_request: Request) -> JSONResponse:
    """What the map needs and the board poll does not carry.

    The route lines are some 450 points and are rebuilt only when the timetable
    is, so the page fetches this once on load rather than every ten seconds.
    """
    return _json(
        {
            "tile_url": config.MAP_TILE_URL,
            "attribution": config.MAP_ATTRIBUTION,
            "max_zoom": config.MAP_MAX_ZOOM,
            "routes": state.map_routes or [],
        }
    )


# The report re-reads the whole of `stop_events` and the whole timetable, which
# is a second or two once the retention window is full — far too long to spend
# on the event loop, where it would stall the board poll for every viewer. It is
# also answering a question about an hourly batch job, so serving a slightly old
# copy costs nothing. Hence: off-thread, and cached for longer than a glance.
_SEGMENTS_TTL = 300.0
_segments_cache: tuple[float, dict] | None = None


def _segments_report() -> dict:
    global _segments_cache
    now = datetime.now(UTC).timestamp()
    if _segments_cache and now - _segments_cache[0] < _SEGMENTS_TTL:
        return _segments_cache[1]
    conn = db.connect(readonly=True)
    try:
        report = segments.build(conn)
    finally:
        conn.close()
    report["age_secs"] = 0
    _segments_cache = (now, report)
    return report


async def api_segments(_request: Request) -> JSONResponse:
    report = await run_in_threadpool(_segments_report)
    if _segments_cache:
        report = {
            **report,
            "age_secs": round(datetime.now(UTC).timestamp() - _segments_cache[0]),
        }
    return _json(report)


async def healthz(_request: Request) -> JSONResponse:
    """Liveness for the container healthcheck and for `tailscale serve` probes."""
    fresh = state.board.get("updated") is not None and datetime.now(
        UTC
    ).timestamp() - state.board["updated"] < max(120, config.POLL_SECS * 4)
    body = {
        "ok": fresh,
        "updated": state.board.get("updated"),
        "error": state.board.get("error"),
    }
    return _json(body, status_code=200 if fresh else 503)


async def api_stops(_request: Request) -> JSONResponse:
    return _json(
        [
            {
                "atco": s.atco,
                "naptan": s.naptan,
                "name": s.name,
                "detail": s.detail,
            }
            for s in config.STOPS
        ]
    )


def _inline_hashes(html: str, tag: str) -> list[str]:
    """CSP hash sources for every inline `<tag>` block in the page."""
    return [
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
        for body in re.findall(rf"<{tag}>(.*?)</{tag}>", html, re.DOTALL)
    ]


def tile_origin(url: str) -> str:
    """The `img-src` source that permits the configured tile server.

    Derived from `config.MAP_TILE_URL` rather than written out a second time,
    because a policy that disagrees with the tile source fails as a blank grey
    map with nothing in the server log to explain it. Leaflet fills `{s}` from
    its subdomain list, so a URL using one has no single host and only the
    wildcard form covers it.
    """
    parts = urlsplit(url.replace("{s}", "*"))
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"MAP_TILE_URL must be an absolute http(s) URL: {url!r}")
    return f"{parts.scheme}://{parts.netloc}"


def content_security_policy(html: str) -> str:
    """Lock the page down to exactly the script and stylesheet it ships with.

    Hashing the inline blocks rather than allowing 'unsafe-inline' is the
    whole point: with a hash source present the browser ignores
    'unsafe-inline', so an `onerror=` handler smuggled in through a feed
    field does not run even if something upstream forgets to escape it. The
    hashes are computed from the file at import, so editing the page cannot
    silently break it — and the page carries no inline style *attributes*,
    which a hash cannot cover.

    'self' joins the script and style sources for the vendored Leaflet, which
    is far too large to inline and hash. That is a real widening — any URL this
    server answers is now a permitted script — so the JSON routes, which carry
    operator free text, are served `nosniff` to stop one being loaded as script.
    The map is also the only part of the page that talks to anyone else: the
    basemap tiles are `<img>` loads from the tile server, and `img-src` names
    that origin and nothing more. `data:` is there because Leaflet swaps in a
    1x1 data URI when it abandons a tile request, not for anything we draw.
    """
    return "; ".join(
        [
            "default-src 'none'",
            "script-src 'self' " + " ".join(_inline_hashes(html, "script")),
            "style-src 'self' " + " ".join(_inline_hashes(html, "style")),
            "img-src 'self' data: " + tile_origin(config.MAP_TILE_URL),
            "connect-src 'self'",  # the page's own /api/board and /api/map polls
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ]
    )


DASHBOARD = STATIC / "dashboard.html"
CSP = content_security_policy(DASHBOARD.read_text())

# Hashes are per-file, so the second page needs its own policy computed from
# its own body. It loads no vendored script, but `content_security_policy`
# is shared with the board deliberately: two hand-tuned policies would drift,
# and the looser of them would be the one nobody was watching.
SEGMENTS_PAGE = STATIC / "segments.html"
SEGMENTS_CSP = content_security_policy(SEGMENTS_PAGE.read_text())

NOSNIFF = {"X-Content-Type-Options": "nosniff"}

# Leaflet 1.9.4, vendored rather than pulled from a CDN. The page names no
# external script or style source, and a dashboard that stops drawing because
# someone else's CDN is unreachable is not worth the 160KB saved. These are the
# published dist artefacts unmodified; their digests match the SRI values on
# leafletjs.com for that release:
#   leaflet.js   sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=
#   leaflet.css  sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=
# The three `url()` references in the stylesheet are for the layers control and
# the default marker icon; the page uses neither, so nothing requests them.
VENDOR = STATIC / "vendor"

# Served by name rather than through a StaticFiles mount: two known files need
# no directory traversal surface. FileResponse sets ETag and Last-Modified, so
# an hour is only about how often the browser revalidates, not how stale it gets.
_VENDOR_FILES = {
    "leaflet.js": (VENDOR / "leaflet.js", "text/javascript"),
    "leaflet.css": (VENDOR / "leaflet.css", "text/css"),
}


async def vendor(request: Request) -> FileResponse:
    entry = _VENDOR_FILES.get(request.path_params["name"])
    if entry is None:
        raise HTTPException(status_code=404)
    path, media_type = entry
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600", **NOSNIFF},
    )


async def index(_request: Request) -> FileResponse:
    return FileResponse(
        DASHBOARD,
        headers={"Content-Security-Policy": CSP, **NOSNIFF},
    )


async def segments_page(_request: Request) -> FileResponse:
    return FileResponse(
        SEGMENTS_PAGE,
        headers={"Content-Security-Policy": SEGMENTS_CSP, **NOSNIFF},
    )


# Starlette rather than FastAPI. Nothing here uses a FastAPI feature — no
# request models, no dependency injection, no generated schema — and the
# pydantic it pulls in was 8.6MB of a 14MB virtualenv, as well as the only
# compiled dependency in the image.
app = Starlette(
    routes=[
        Route("/", index),
        Route("/segments", segments_page),
        Route("/vendor/{name}", vendor),
        Route("/api/board", api_board),
        Route("/api/map", api_map),
        Route("/api/stops", api_stops),
        Route("/api/segments", api_segments),
        Route("/healthz", healthz),
    ],
    lifespan=lifespan,
)


def main() -> None:
    import uvicorn

    logs.setup()

    if not config.DB_PATH.exists():
        raise SystemExit("No timetable cache. Run: python -m ontime.ingest")
    config.api_key()  # fail fast if the key is missing
    log.info("serving on http://%s:%d", config.HOST, config.PORT)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
