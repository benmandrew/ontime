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
    matched = [(v, t) for v, t in pairs if t]
    print(f"  matched: {len(matched)}/{len(vehicles)}")

    def do_predict():
        out = []
        for v, trip in matched:
            for stop in config.STOPS:
                p = eta.predict(v, trip, stop.atco, segments)
                if p:
                    out.append(p)
        return out

    preds, t_predict = timed("predict all stops", do_predict)
    print(f"  predictions: {len(preds)}")

    _, t_sched = timed(
        "scheduled_only fallback",
        lambda: eta.scheduled_only(
            trips, {t.trip_id for _v, t in matched}, time.time(), config.HORIZON_SECS
        ),
    )

    # A 00:45 sample understates the daytime cost, so replay the recorded
    # busy-period fixture against the full timetable as well.
    sample = ROOT / "tests" / "fixtures" / "siri_sample.xml"
    if sample.exists():
        busy = siri.parse(sample.read_bytes())
        busy = [v for v in busy if v.route_name in routes]
        print(f"\n  replaying recorded busy sample: {len(busy)} vehicles")
        start = time.perf_counter()
        busy_pairs = [(v, match(v, trips)) for v in busy]
        t_busy = time.perf_counter() - start
        hit = sum(1 for _v, t in busy_pairs if t)
        print(
            f"  match {len(busy)} vehicles                  {t_busy * 1000:8.1f} ms  ({hit} matched)"
        )
        if busy:
            per = t_busy / len(busy)
            print(f"  per vehicle                        {per * 1000:8.2f} ms")
            print(f"  extrapolated to 40 vehicles        {per * 40 * 1000:8.0f} ms")
            print(
                f"  duty cycle at 40 vehicles, {config.POLL_SECS}s      {100 * per * 40 / config.POLL_SECS:.1f}%"
            )

    conn.close()
    cpu = t_match + t_predict + t_sched
    cycle = t_load + t_fetch + cpu
    print(f"\n  compute per cycle (excl. network): {cpu * 1000:.0f} ms")
    print(f"  whole cycle:                       {cycle * 1000:.0f} ms")
    print(
        f"  duty cycle at {config.POLL_SECS}s poll:            {100 * cycle / config.POLL_SECS:.1f}%"
    )
    print(f"  peak RSS:                          {rss_mb():.0f} MB")

    daily = len(vehicles) * (86400 / 10)
    print(f"\n  positions/day (deduped est.):      {daily:,.0f}")
    print(f"  storage/day at ~90 B/row:          {daily * 90 / 1e6:.0f} MB")
    print(
        f"  at {config.RETAIN_DAYS}-day retention:               {daily * 90 * config.RETAIN_DAYS / 1e6:.0f} MB"
    )


if __name__ == "__main__":
    main()
