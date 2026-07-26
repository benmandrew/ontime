"""SQLite schema and connection handling for the cached timetable."""

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
CREATE INDEX IF NOT EXISTS trips_route_dep ON trips (route_name, first_dep);

-- Full stop sequence for every trip that calls at a watched stop.
CREATE TABLE IF NOT EXISTS trip_stops (
    trip_id TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    arr     INTEGER,
    dep     INTEGER,
    PRIMARY KEY (trip_id, seq)
);
CREATE INDEX IF NOT EXISTS trip_stops_stop ON trip_stops (stop_id);

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
CREATE INDEX IF NOT EXISTS obs_trip ON observations (trip_id, recorded_at);
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
    PRIMARY KEY (service_date, trip_id, vehicle_ref, seq)
);
CREATE INDEX IF NOT EXISTS stop_events_stop ON stop_events (stop_id, actual_at);

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
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
