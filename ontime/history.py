"""Persist observed positions and learn how long segments really take.

A timetable says the 192 takes four minutes from Cavanagh Close to the next
stop. Observation says what it takes at 08:15 on a Tuesday, which is a
different number. This module keeps the raw positions, reduces them to the
moment each vehicle actually passed each stop, and aggregates those into
median traversal times per segment, hour and day type.

Raw positions are a ring buffer trimmed to ONTIME_RETAIN_DAYS. The aggregates
survive trimming, so accuracy keeps improving without the database growing
without bound.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from . import config, db, locking, logs
from .matching import LONDON, Trip, haversine, service_day_offsets

log = logs.get("ontime.history")

# A position must come within this distance of a stop to count as passing it.
STOP_RADIUS_M = 120.0

# Segments need at least this many observations before the learned time is used.
MIN_SAMPLES = 5

# How far back `derive_stop_events` scans raw positions. Wider than the hourly
# cadence it runs at, so a skipped pass loses nothing.
LOOKBACK_HOURS = 26

# Quiet time that separates one run from the next within a (trip, vehicle)
# group. Because the window above is wider than a day, a vehicle rostered onto
# the same trip_id two days running arrives as one group and has to be cut
# apart. Nothing inside a single run comes close to this — the longest journey
# through the watched stops is barely an hour — while consecutive days leave
# some twenty-two hours of silence between them.
RUN_GAP_SECS = 3 * 3600

Points = list[tuple[int, float, float]]  # recorded_at, lat, lon


def record(conn: sqlite3.Connection, vehicle, trip_id: str | None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO observations "
        "(vehicle_ref, recorded_at, route_name, lat, lon, bearing, trip_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            vehicle.vehicle_ref,
            int(vehicle.recorded_at.timestamp()),
            vehicle.route_name,
            vehicle.lat,
            vehicle.lon,
            vehicle.bearing,
            trip_id,
        ),
    )


def trim(conn: sqlite3.Connection, retain_days: int) -> int:
    cutoff = int((datetime.now(UTC) - timedelta(days=retain_days)).timestamp())
    cur = conn.execute("DELETE FROM observations WHERE recorded_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def _split_runs(points: Points) -> list[Points]:
    """Cut a (trip, vehicle) group into the separate runs it holds."""
    runs: list[Points] = [[points[0]]]
    for prev, cur in pairwise(points):
        if cur[0] - prev[0] > RUN_GAP_SECS:
            runs.append([])
        runs[-1].append(cur)
    return runs


def _service_date(trip: Trip, run: Points) -> str:
    """The service day a run belongs to, whatever is left of it.

    The day of the earliest surviving observation is the wrong answer. The scan
    window slides forward, so a run straddling local midnight loses its
    pre-midnight head and its earliest survivor moves into the next calendar
    day; the run is then written again under a second service date instead of
    replacing its own rows, and one journey is learned twice.

    The last observation anchors it instead. The window and the retention trim
    both take positions off the front, never the back, so the end of a finished
    run does not move. GTFS expresses a post-midnight journey as 24:xx-27:30 on
    the *previous* day, so an instant just after midnight belongs to two
    candidate service days; the one whose timetable the run actually fits wins.
    Note that this cannot come from `trip.service_date` — callers build the
    trips dict keyed on trip_id, which collapses every day a trip runs on into
    one entry.
    """
    candidates = service_day_offsets(datetime.fromtimestamp(run[-1][0], UTC))
    sched_end = next(
        (arr for _seq, _sid, arr, _lat, _lon in reversed(trip.stops) if arr is not None),
        trip.first_dep if trip.first_dep >= 0 else None,
    )
    if sched_end is None:
        return candidates[0][0].isoformat()
    # The candidates are a day apart, so this choice cannot flip as the head of
    # the run is trimmed away — which is the whole point of anchoring here.
    day, _secs = min(candidates, key=lambda c: abs(c[1] - sched_end))
    return day.isoformat()


def derive_stop_events(
    conn: sqlite3.Connection,
    trips: dict[str, Trip],
    lookback_hours: float = LOOKBACK_HOURS,
) -> int:
    """Reduce raw positions to one closest-approach event per stop per run.

    Only runs that have gone quiet for an hour are processed, so a trip still
    in progress is not frozen halfway. A group is one (trip, vehicle) pair,
    which can hold more than one run — see `_split_runs`.

    The scan is bounded to `lookback_hours`. Reducing a run is idempotent —
    the write is an upsert keyed on (service_date, trip, vehicle, seq) — so
    anything older has already been reduced by an earlier pass, and rescanning
    the whole retention window each hour would mean loading hundreds of
    thousands of rows to rewrite results that cannot have changed. The window
    is comfortably wider than the hourly cadence, so a few missed runs still
    get picked up on the next pass.
    """
    now = datetime.now(UTC)
    quiet_before = int((now - timedelta(hours=1)).timestamp())
    since = int((now - timedelta(hours=lookback_hours)).timestamp())
    groups: dict[tuple[str, str], Points] = defaultdict(list)

    q = (
        "SELECT trip_id, vehicle_ref, recorded_at, lat, lon FROM observations "
        "WHERE trip_id IS NOT NULL AND recorded_at >= ? ORDER BY recorded_at"
    )
    for r in conn.execute(q, (since,)):
        groups[(r["trip_id"], r["vehicle_ref"])].append(
            (r["recorded_at"], r["lat"], r["lon"])
        )

    written = 0
    for (trip_id, vehicle_ref), points in groups.items():
        trip = trips.get(trip_id)
        if trip is None or not points:
            continue
        for run in _split_runs(points):
            # Judged per run, not per group: yesterday's finished journey must
            # still be reduced while today's is halfway down the road.
            if run[-1][0] > quiet_before:
                continue
            svc_date = _service_date(trip, run)

            for seq, stop_id, sched_arr, slat, slon in trip.stops:
                best_t, best_d = None, float("inf")
                for ts, lat, lon in run:
                    d = haversine(lat, lon, slat, slon)
                    if d < best_d:
                        best_t, best_d = ts, d
                if best_t is None or best_d > STOP_RADIUS_M:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO stop_events "
                    "(trip_id, vehicle_ref, service_date, seq, stop_id, actual_at, "
                    " sched_arr, dist_m) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        trip_id,
                        vehicle_ref,
                        svc_date,
                        seq,
                        stop_id,
                        best_t,
                        sched_arr,
                        best_d,
                    ),
                )
                written += 1
    conn.commit()
    return written


def learn_segments(conn: sqlite3.Connection) -> int:
    """Aggregate consecutive stop events into per-segment traversal times."""
    runs: dict[tuple[str, str, str], list[tuple[int, str, int]]] = defaultdict(list)
    q = (
        "SELECT e.service_date, e.trip_id, e.vehicle_ref, e.seq, e.stop_id, "
        "       e.actual_at, t.route_name "
        "FROM stop_events e JOIN trips t ON t.trip_id = e.trip_id "
        "ORDER BY e.service_date, e.trip_id, e.vehicle_ref, e.seq"
    )
    route_of: dict[tuple[str, str, str], str] = {}
    for r in conn.execute(q):
        key = (r["service_date"], r["trip_id"], r["vehicle_ref"])
        runs[key].append((r["seq"], r["stop_id"], r["actual_at"]))
        route_of[key] = r["route_name"]

    buckets: dict[tuple[str, str, str, int, int], list[float]] = defaultdict(list)
    for key, events in runs.items():
        route = route_of[key]
        events.sort()
        for (_s1, from_id, t1), (_s2, to_id, t2) in pairwise(events):
            secs = t2 - t1
            # Reject impossible or implausible gaps (missed stop, layover).
            if not (5 <= secs <= 1800):
                continue
            local = datetime.fromtimestamp(t1, LONDON)
            buckets[(route, from_id, to_id, local.hour, int(local.weekday() >= 5))].append(
                float(secs)
            )

    conn.execute("DELETE FROM segment_stats")
    rows = []
    for (route, from_id, to_id, hour, weekend), samples in buckets.items():
        if len(samples) < 2:
            continue
        samples.sort()
        p85 = samples[min(len(samples) - 1, int(0.85 * len(samples)))]
        rows.append(
            (
                route,
                from_id,
                to_id,
                hour,
                weekend,
                statistics.median(samples),
                p85,
                len(samples),
            )
        )
    conn.executemany("INSERT OR REPLACE INTO segment_stats VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def load_segment_stats(conn: sqlite3.Connection) -> dict[tuple, tuple[float, float, int]]:
    out = {}
    for r in conn.execute("SELECT * FROM segment_stats WHERE samples >= ?", (MIN_SAMPLES,)):
        out[
            (
                r["route_name"],
                r["from_stop_id"],
                r["to_stop_id"],
                r["hour"],
                r["is_weekend"],
            )
        ] = (r["median_secs"], r["p85_secs"], r["samples"])
    return out


def stats_summary(conn: sqlite3.Connection) -> dict:
    obs = conn.execute(
        "SELECT COUNT(*) c, MIN(recorded_at) a, MAX(recorded_at) b FROM observations"
    ).fetchone()
    ev = conn.execute("SELECT COUNT(*) c FROM stop_events").fetchone()
    seg = conn.execute(
        "SELECT COUNT(*) c FROM segment_stats WHERE samples >= ?", (MIN_SAMPLES,)
    ).fetchone()
    span_days = 0.0
    if obs["a"] and obs["b"]:
        span_days = (obs["b"] - obs["a"]) / 86400
    return {
        "observations": obs["c"],
        "history_days": round(span_days, 2),
        "stop_events": ev["c"],
        "learned_segments": seg["c"],
    }


def main() -> None:
    """Batch job: derive events, relearn segments, trim old positions."""
    logs.setup()
    from .matching import load_trips

    # Every step below writes, and this is the job most likely to be run by
    # hand on the host while a container is polling, so it registers too.
    with locking.writing("history"):
        conn = db.connect()
        db.init(conn)
        today = datetime.now(LONDON).date()
        days = tuple(today - timedelta(days=i) for i in range(config.RETAIN_DAYS + 1))
        trips = {t.trip_id: t for t in load_trips(conn, days)}

        events = derive_stop_events(conn, trips)
        segments = learn_segments(conn)
        removed = trim(conn, config.RETAIN_DAYS)
        log.info(
            "stop events=%d segments=%d trimmed=%d %s",
            events,
            segments,
            removed,
            stats_summary(conn),
        )
        conn.close()


if __name__ == "__main__":
    main()
