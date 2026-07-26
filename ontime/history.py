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

from . import config, db
from .matching import LONDON, Trip, haversine

# A position must come within this distance of a stop to count as passing it.
STOP_RADIUS_M = 120.0

# Segments need at least this many observations before the learned time is used.
MIN_SAMPLES = 5


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


def derive_stop_events(conn: sqlite3.Connection, trips: dict[str, Trip]) -> int:
    """Reduce raw positions to one closest-approach event per stop per run.

    Only runs that have gone quiet for an hour are processed, so a trip still
    in progress is not frozen halfway.
    """
    quiet_before = int((datetime.now(UTC) - timedelta(hours=1)).timestamp())
    groups: dict[tuple[str, str], list[tuple[int, float, float]]] = defaultdict(list)

    q = (
        "SELECT trip_id, vehicle_ref, recorded_at, lat, lon FROM observations "
        "WHERE trip_id IS NOT NULL ORDER BY recorded_at"
    )
    for r in conn.execute(q):
        groups[(r["trip_id"], r["vehicle_ref"])].append(
            (r["recorded_at"], r["lat"], r["lon"])
        )

    written = 0
    for (trip_id, vehicle_ref), points in groups.items():
        if not points or points[-1][0] > quiet_before:
            continue
        trip = trips.get(trip_id)
        if trip is None:
            continue
        svc_date = datetime.fromtimestamp(points[0][0], LONDON).date().isoformat()

        for seq, stop_id, sched_arr, slat, slon in trip.stops:
            best_t, best_d = None, float("inf")
            for ts, lat, lon in points:
                d = haversine(lat, lon, slat, slon)
                if d < best_d:
                    best_t, best_d = ts, d
            if best_t is None or best_d > STOP_RADIUS_M:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO stop_events "
                "(trip_id, vehicle_ref, service_date, seq, stop_id, actual_at, "
                " sched_arr, dist_m) VALUES (?,?,?,?,?,?,?,?)",
                (trip_id, vehicle_ref, svc_date, seq, stop_id, best_t, sched_arr, best_d),
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
    from .matching import load_trips

    conn = db.connect()
    db.init(conn)
    today = datetime.now(LONDON).date()
    days = tuple(today - timedelta(days=i) for i in range(config.RETAIN_DAYS + 1))
    trips = {t.trip_id: t for t in load_trips(conn, days)}

    events = derive_stop_events(conn, trips)
    segments = learn_segments(conn)
    removed = trim(conn, config.RETAIN_DAYS)
    print(f"stop events written: {events}")
    print(f"segments learned:    {segments}")
    print(f"old positions trimmed: {removed}")
    print(stats_summary(conn))
    conn.close()


if __name__ == "__main__":
    main()
