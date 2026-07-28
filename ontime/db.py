"""SQLite schema and connection handling for the cached timetable.

Indices here are deliberately sparse. Every one was checked with EXPLAIN QUERY
PLAN against a real cache, and the ones no query reached were removed: an
unused index on `observations` alone cost 45% more insert time (297ms against
430ms for 200,000 rows), and inserting is the hot path — every vehicle, every
poll, all day. The remaining full-table scans are intentional, either because
the caller genuinely wants every row or because the table has a few hundred
rows in it. Re-run `scripts/benchmark.py` before adding another.
"""

from __future__ import annotations

import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS stops (
    stop_id   TEXT PRIMARY KEY,
    stop_code TEXT,
    name      TEXT,
    lat       REAL,
    lon       REAL
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id        TEXT PRIMARY KEY,
    service_id     TEXT NOT NULL,
    route_name     TEXT NOT NULL,
    headsign       TEXT,
    direction_id   TEXT,
    origin_stop_id TEXT,
    dest_stop_id   TEXT,
    first_dep      INTEGER,   -- seconds since service-day midnight
    last_arr       INTEGER
);

-- Full stop sequence for every trip that calls at a watched stop.
CREATE TABLE IF NOT EXISTS trip_stops (
    trip_id TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    arr     INTEGER,
    dep     INTEGER,
    PRIMARY KEY (trip_id, seq)
);

-- The subset of calls that happen at a watched stop.
CREATE TABLE IF NOT EXISTS target_calls (
    trip_id TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    arr     INTEGER,
    PRIMARY KEY (trip_id, stop_id)
);

CREATE TABLE IF NOT EXISTS calendar (
    service_id TEXT PRIMARY KEY,
    monday INTEGER, tuesday INTEGER, wednesday INTEGER, thursday INTEGER,
    friday INTEGER, saturday INTEGER, sunday INTEGER,
    start_date TEXT, end_date TEXT
);

CREATE TABLE IF NOT EXISTS calendar_dates (
    service_id     TEXT NOT NULL,
    date           TEXT NOT NULL,
    exception_type INTEGER NOT NULL,
    PRIMARY KEY (service_id, date)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---------------------------------------------------------------------------
-- Observation history. Raw positions are a ring buffer trimmed to
-- ONTIME_RETAIN_DAYS; the aggregates derived from them are kept permanently.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS observations (
    vehicle_ref TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,   -- unix seconds, from RecordedAtTime
    route_name  TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    bearing     REAL,
    trip_id     TEXT,               -- NULL when unmatched
    PRIMARY KEY (vehicle_ref, recorded_at)
) WITHOUT ROWID;
-- Earns its keep: serves the bounded scan in derive_stop_events, the retention
-- DELETE in trim, and MIN/MAX in stats_summary as a covering index.
CREATE INDEX IF NOT EXISTS obs_time ON observations (recorded_at);

-- Actual time a matched vehicle was observed closest to a stop on its trip.
CREATE TABLE IF NOT EXISTS stop_events (
    trip_id      TEXT NOT NULL,
    vehicle_ref  TEXT NOT NULL,
    service_date TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    stop_id      TEXT NOT NULL,
    actual_at    INTEGER NOT NULL,  -- unix seconds
    sched_arr    INTEGER,           -- seconds since service-day midnight
    dist_m       REAL,              -- closest approach, a confidence proxy
    -- This key order is also learn_segments' required sort order, so the
    -- aggregate reads straight down the primary index with no separate sort.
    PRIMARY KEY (service_date, trip_id, vehicle_ref, seq)
);

-- Learned median traversal time between consecutive stops, by context.
CREATE TABLE IF NOT EXISTS segment_stats (
    route_name   TEXT NOT NULL,
    from_stop_id TEXT NOT NULL,
    to_stop_id   TEXT NOT NULL,
    hour         INTEGER NOT NULL,  -- local hour of departure, 0-23
    is_weekend   INTEGER NOT NULL,
    median_secs  REAL NOT NULL,
    p85_secs     REAL NOT NULL,
    samples      INTEGER NOT NULL,
    PRIMARY KEY (route_name, from_stop_id, to_stop_id, hour, is_weekend)
);
"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    if not readonly:
        # Choosing a journal mode is itself a write, so this raises
        # SQLITE_READONLY on a read-only handle to a database that is not
        # already in WAL. The one read-only caller treats any error as "no
        # usable cache" and rebuilds, so a database created in delete mode sent
        # the maintenance loop into a rebuild it could never satisfy. The
        # setting is persistent anyway: readers inherit it from the writer.
        conn.execute("PRAGMA journal_mode=WAL")
    # Write-ahead logging keeps readers off the writer's back, but it does not
    # help two writers. Without a timeout SQLite gives up instantly, which is
    # enough to fail every poll while the rebuild commits.
    conn.execute(f"PRAGMA busy_timeout={config.BUSY_TIMEOUT_MS}")
    return conn


# Indices that were dropped from SCHEMA once EXPLAIN QUERY PLAN showed no query
# reached them. Removing them from the schema was not enough on its own: `init`
# only ever runs CREATE ... IF NOT EXISTS, so it cannot unmake anything, and
# every database created before the removal still carried all four — including
# the long-lived `ontime-data` volume, which is never recreated. The saving was
# therefore never actually banked anywhere. `obs_trip` is the expensive one:
# 591ms against 1193ms to insert 200,000 rows, so it was doubling the cost of
# the hot path — every vehicle, every poll, all day.
DROPPED_INDICES = (
    "obs_trip",
    "trip_stops_stop",
    "trips_route_dep",
    "stop_events_stop",
)


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for name in DROPPED_INDICES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.commit()
