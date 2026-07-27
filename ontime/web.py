"""Dashboard server: a background poller plus a small JSON and HTML frontend.

The API key never leaves this process. The browser talks only to this server,
which is why the page can be served over plain HTTP on the loopback interface
without exposing the credential.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import config, db, eta, history, logs, siri
from .matching import LONDON, Trip, load_trips, match

STATIC = Path(__file__).parent / "static"
log = logs.get("ontime.web")


class State:
    """Everything the poller writes and the request handlers read."""

    def __init__(self) -> None:
        self.trips: list[Trip] = []
        self.trips_for: date | None = None
        self.segments: dict = {}
        self.board: dict = {"stops": [], "updated": None, "error": None}
        self.vehicles: list = []
        self.last_poll: float | None = None
        self.routes: set[str] = set()


state = State()


def refresh_timetable(conn) -> None:
    """Reload the day's trips when the service date rolls over."""
    today = datetime.now(LONDON).date()
    if state.trips_for == today:
        return
    days = (today, today - timedelta(days=1))
    state.trips = load_trips(conn, days)
    state.trips_for = today
    state.routes = {t.route_name for t in state.trips}
    log.info(
        "timetable loaded: %d trips for %s, routes %s",
        len(state.trips),
        today,
        sorted(state.routes),
    )


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
        stops_out.append(
            {
                "atco": stop.atco,
                "naptan": stop.naptan,
                "name": stop.name,
                "detail": stop.detail,
                "departures": [
                    {
                        "route": p.route_name,
                        "headsign": p.headsign,
                        "minutes": round(p.minutes) if p.minutes is not None else None,
                        "eta_ts": p.eta_ts,
                        "sched_ts": p.sched_ts,
                        "delay_mins": round(p.delay_secs / 60) if p.delay_secs else None,
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
    conn = db.connect()
    try:
        db.init(conn)
        refresh_timetable(conn)
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
        msg = config.redact(str(exc))
        log.error("poll failed: %s", msg)
        state.board = {**state.board, "error": msg}


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


async def api_board(_request: Request) -> JSONResponse:
    return JSONResponse(state.board)


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
    return JSONResponse(body, status_code=200 if fresh else 503)


async def api_stops(_request: Request) -> JSONResponse:
    return JSONResponse(
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


async def index(_request: Request) -> FileResponse:
    return FileResponse(STATIC / "dashboard.html")


# Starlette rather than FastAPI. Nothing here uses a FastAPI feature — no
# request models, no dependency injection, no generated schema — and the
# pydantic it pulls in was 8.6MB of a 14MB virtualenv, as well as the only
# compiled dependency in the image.
app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/board", api_board),
        Route("/api/stops", api_stops),
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
