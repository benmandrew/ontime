"""Measure the cost of one poll cycle against the real feed and timetable.

Run inside the dev shell with a built cache:
    python scripts/benchmark.py
"""

from __future__ import annotations

import resource
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ontime import config, db, eta, history, siri
from ontime.matching import LONDON, load_trips, match


def rss_mb() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def timed(label: str, fn):
    start = time.perf_counter()
    out = fn()
    elapsed = time.perf_counter() - start
    print(f"  {label:<34} {elapsed * 1000:8.1f} ms")
    return out, elapsed


def main() -> None:
    if not config.DB_PATH.exists():
        raise SystemExit("No cache. Run: python -m ontime.ingest")

    print(f"cache: {config.DB_PATH.stat().st_size / 1e6:.1f} MB")
    conn = db.connect()

    today = datetime.now(LONDON).date()
    trips, t_load = timed(
        "load_trips (today + yesterday)",
        lambda: load_trips(conn, (today, today - timedelta(days=1))),
    )
    segments, _ = timed("load_segment_stats", lambda: history.load_segment_stats(conn))
    routes = {t.route_name for t in trips}
    stop_count = sum(len(t.stops) for t in trips)
    print(f"  trips={len(trips)} stops={stop_count} routes={sorted(routes)}")

    vehicles, t_fetch = timed(
        "siri.fetch (network + parse)", lambda: siri.fetch(routes=routes)
    )
    print(f"  vehicles on watched routes: {len(vehicles)}")

    def do_match():
        return [(v, match(v, trips)) for v in vehicles]

    pairs, t_match = timed("match all vehicles", do_match)
    matched = [(v, m) for v, m in pairs if m]
    print(f"  matched: {len(matched)}/{len(vehicles)}")

    def do_predict():
        out = []
        for v, m in matched:
            for stop in config.STOPS:
                p = eta.predict(
                    v,
                    m.trip,
                    stop.atco,
                    segments,
                    schedule_confident=m.schedule_confident,
                )
                if p:
                    out.append(p)
        return out

    preds, t_predict = timed("predict all stops", do_predict)
    print(f"  predictions: {len(preds)}")

    _, t_sched = timed(
        "scheduled_only fallback",
        lambda: eta.scheduled_only(
            trips, {m.trip.trip_id for _v, m in matched}, time.time(), config.HORIZON_SECS
        ),
    )

    # The recorded fixture is only useful for cost while it is fresh: its
    # vehicles' journeys finish, and matching them against a later day's
    # timetable then measures rejection rather than matching.
    per_vehicle = t_match / len(vehicles) if vehicles else 0
    print(f"\n  per vehicle matched                {per_vehicle * 1000:8.2f} ms")
    print(f"  extrapolated to 60 vehicles        {per_vehicle * 60 * 1000:8.0f} ms")

    conn2 = conn
    cpu = t_match + t_predict + t_sched
    cycle = t_load + t_fetch + cpu
    print(f"\n  compute per cycle (excl. network): {cpu * 1000:.0f} ms")
    print(f"  whole cycle:                       {cycle * 1000:.0f} ms")
    print(
        f"  duty cycle at {config.POLL_SECS}s poll:            {100 * cycle / config.POLL_SECS:.1f}%"
    )
    print(f"  peak RSS:                          {rss_mb():.0f} MB")

    # Measured from accumulated history where there is any, because the naive
    # "vehicles seen now, all day" extrapolation overstates it by an order of
    # magnitude — the feed repeats a vehicle's RecordedAtTime between updates
    # and those duplicates are dropped on insert.
    row = conn2.execute(
        "SELECT COUNT(*) c, MIN(recorded_at) a, MAX(recorded_at) b FROM observations"
    ).fetchone()
    span_h = (row["b"] - row["a"]) / 3600 if row["a"] and row["c"] else 0
    print()
    if span_h > 1:
        used = (
            conn2.execute(
                "SELECT SUM(pgsize) FROM dbstat WHERE name='observations'"
            ).fetchone()[0]
            or 0
        )
        per_row = used / row["c"]
        per_day = row["c"] / span_h * 24
        print(f"  history measured over              {span_h:8.1f} h")
        print(f"  rows/day (deduped, measured)       {per_day:8,.0f}")
        print(f"  bytes/row incl. index              {per_row:8.0f}")
        print(f"  storage/day                        {per_day * per_row / 1e6:8.1f} MB")
        print(
            f"  at {config.RETAIN_DAYS}-day retention               "
            f"{per_day * per_row * config.RETAIN_DAYS / 1e6:8.0f} MB"
        )
    else:
        print("  history: not enough accumulated yet to measure storage growth")
    conn2.close()


if __name__ == "__main__":
    main()
