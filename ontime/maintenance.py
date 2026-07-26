"""Periodic upkeep: refresh the timetable, relearn segments, trim history.

The dashboard process only polls and predicts. Everything slow runs here, in
a separate container sharing the same volume, so a 90MB timetable download
never stalls the departure board.
"""

from __future__ import annotations

import time
import traceback
from datetime import UTC, datetime, timedelta

from . import config, db, history, ingest
from .matching import LONDON, load_trips

INGEST_INTERVAL = 24 * 3600
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


def main() -> None:
    last_ingest = 0.0
    last_learn = 0.0
    while True:
        now = time.monotonic()
        try:
            if now - last_ingest >= INGEST_INTERVAL or not config.DB_PATH.exists():
                run_ingest()
                last_ingest = now
            if now - last_learn >= LEARN_INTERVAL:
                run_learn()
                last_learn = now
        except Exception:
            print("[maint] error:\n" + config.redact(traceback.format_exc()))
        time.sleep(60)


if __name__ == "__main__":
    main()
