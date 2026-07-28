"""Periodic upkeep: refresh the timetable, relearn segments, trim history.

The dashboard process only polls and predicts. Everything slow runs here, in
a separate container sharing the same volume, so a 90MB timetable download
never stalls the departure board.
"""

from __future__ import annotations

import sqlite3
import time
import traceback
from datetime import datetime, timedelta

from . import config, db, history, ingest, locking, logs
from .matching import LONDON, load_trips

log = logs.get("ontime.maint")

# How often the loop wakes to check whether anything is due.
TICK_SECS = 60
# Segments are relearned this often; the timetable is rebuilt once per day,
# decided by the persisted build date rather than by elapsed time.
LEARN_INTERVAL = 3600
# Floor between successful rebuilds. The build date decides *whether* to
# rebuild, but anything that leaves it unsatisfied — a stopped clock, a feed
# publishing yesterday's data — otherwise re-enters the rebuild on every tick,
# a minute-long download and write burst once a minute, forever. A rebuild that
# raises is deliberately not throttled: a cold volume has no timetable at all
# and an hour of waiting is an hour of empty board.
MIN_INGEST_INTERVAL = 3600


def run_ingest() -> None:
    log.info("refreshing timetable")
    ingest.download()
    # force=True: this container shares a named volume with the web container
    # by design, and rebuilding while it polls was verified safe. The guard in
    # build() exists for a host process racing a bind-mounted container, which
    # is a different and genuinely unsafe situation.
    ingest.build(force=True)


def run_learn() -> None:
    log.info("relearning segments")
    # This pass derives events, empties and repopulates segment_stats and trims
    # observations — minutes of writing that used to announce itself as nothing
    # at all, so a host ingest started during it saw a clear database.
    with locking.writing("learn"):
        conn = db.connect()
        db.init(conn)
        today = datetime.now(LONDON).date()
        days = tuple(today - timedelta(days=i) for i in range(config.RETAIN_DAYS + 1))
        trips = {t.trip_id: t for t in load_trips(conn, days)}
        events = history.derive_stop_events(conn, trips)
        segments = history.learn_segments(conn)
        removed = history.trim(conn, config.RETAIN_DAYS)
        log.info(
            "events=%d segments=%d trimmed=%d %s",
            events,
            segments,
            removed,
            history.stats_summary(conn),
        )
        conn.close()


def cache_is_current() -> bool:
    """Whether the timetable cache was built for today's service date.

    Process uptime is the wrong thing to measure here. A container that
    restarts every few hours would keep resetting a monotonic timer and could
    go indefinitely without refreshing, so the check reads the build date
    persisted in the database instead.
    """
    if not config.DB_PATH.exists():
        return False
    try:
        conn = db.connect(readonly=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return bool(row) and row["value"] == datetime.now(LONDON).date().isoformat()


def main() -> None:
    logs.setup()
    last_learn: float | None = None
    last_ingest: float | None = None
    while True:
        try:
            if not cache_is_current() and (
                last_ingest is None or time.monotonic() - last_ingest >= MIN_INGEST_INTERVAL
            ):
                run_ingest()
                # Stamped only on success, so a failed rebuild retries next tick.
                last_ingest = time.monotonic()
            now = time.monotonic()
            if last_learn is None or now - last_learn >= LEARN_INTERVAL:
                run_learn()
                last_learn = now
        except Exception:
            log.error("cycle failed:\n%s", config.redact(traceback.format_exc()))
        time.sleep(TICK_SECS)


if __name__ == "__main__":
    main()
