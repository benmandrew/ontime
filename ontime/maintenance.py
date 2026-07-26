"""Periodic upkeep: refresh the timetable, relearn segments, trim history.

The dashboard process only polls and predicts. Everything slow runs here, in
a separate container sharing the same volume, so a 90MB timetable download
never stalls the departure board.
"""

from __future__ import annotations

import sqlite3
import time
import traceback
from datetime import UTC, datetime, timedelta

from . import config, db, history, ingest
from .matching import LONDON, load_trips

# How often the loop wakes to check whether anything is due.
TICK_SECS = 60
# Segments are relearned this often; the timetable is rebuilt once per day,
# decided by the persisted build date rather than by elapsed time.
LEARN_INTERVAL = 3600


def run_ingest() -> None:
    print(f"[maint] {datetime.now(UTC).isoformat()} refreshing timetable")
    ingest.download()
    ingest.build()


def run_learn() -> None:
    print(f"[maint] {datetime.now(UTC).isoformat()} relearning segments")
    conn = db.connect()
    db.init(conn)
    today = datetime.now(LONDON).date()
    days = tuple(today - timedelta(days=i) for i in range(config.RETAIN_DAYS + 1))
    trips = {t.trip_id: t for t in load_trips(conn, days)}
    events = history.derive_stop_events(conn, trips)
    segments = history.learn_segments(conn)
    removed = history.trim(conn, config.RETAIN_DAYS)
    print(
        f"[maint] events={events} segments={segments} trimmed={removed} "
        f"{history.stats_summary(conn)}"
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
    last_learn: float | None = None
    while True:
        try:
            if not cache_is_current():
                run_ingest()
            now = time.monotonic()
            if last_learn is None or now - last_learn >= LEARN_INTERVAL:
                run_learn()
                last_learn = now
        except Exception:
            print("[maint] error:\n" + config.redact(traceback.format_exc()))
        time.sleep(TICK_SECS)


if __name__ == "__main__":
    main()
