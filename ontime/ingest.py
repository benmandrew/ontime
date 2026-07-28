"""Download the BODS North West GTFS feed and cache the parts we need.

The published archive is about 89MB zipped and 544MB unpacked, with
`stop_times.txt` alone accounting for 398MB. Nothing is unpacked to disk and
no file is read whole into memory: every pass streams rows out of the zip.

Two passes over `stop_times.txt` are needed. The first finds which trips call
at a watched stop; the second collects the complete stop sequence for exactly
those trips, which is what the arrival estimator walks.

The scan is deliberately separated from the write. What survives the scan is a
few tens of thousands of tuples out of four million rows, so it costs little to
hold in memory, and holding it means the database is touched only for the few
seconds it takes to swap the old cache for the new one.
"""

from __future__ import annotations

import csv
import io
import sys
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests

from . import config, db, locking, logs
from .matching import LONDON

log = logs.get("ontime.ingest")

CHUNK = 1 << 20
LOG_EVERY_BYTES = 20 << 20

# Members the build reads. A download missing any of these is not an archive we
# can build from, whatever the server said the response was.
REQUIRED_MEMBERS = (
    "stop_times.txt",
    "trips.txt",
    "stops.txt",
    "routes.txt",
    "calendar.txt",
)

# The tables the rebuild replaces wholesale, in the order they are emptied.
CACHED_TABLES = (
    "stops",
    "trips",
    "trip_stops",
    "target_calls",
    "calendar",
    "calendar_dates",
)


def parse_gtfs_time(value: str) -> int | None:
    """Seconds since service-day midnight. GTFS allows hours past 24."""
    if not value:
        return None
    try:
        h, m, s = (int(p) for p in value.split(":"))
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def download(force: bool = False) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fresh = (
        config.GTFS_ZIP.exists()
        and time.time() - config.GTFS_ZIP.stat().st_mtime < 20 * 3600
    )
    if fresh and not force:
        mb = config.GTFS_ZIP.stat().st_size / 1e6
        log.info("using cached %s (%.0fMB, under 20h old)", config.GTFS_ZIP.name, mb)
        return

    log.info("downloading %s", config.GTFS_URL)
    tmp = config.GTFS_ZIP.with_suffix(".part")
    try:
        with requests.get(config.GTFS_URL, stream=True, timeout=300) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            last_logged = 0
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(CHUNK):
                    fh.write(chunk)
                    done += len(chunk)
                    if total and done - last_logged >= LOG_EVERY_BYTES:
                        last_logged = done
                        log.info(
                            "  %.0f/%.0fMB (%.0f%%)",
                            done / 1e6,
                            total / 1e6,
                            100 * done / total,
                        )
        _check_archive(tmp, expected_bytes=total)
        # Only now is the known-good copy allowed to go. Replacing first and
        # discovering the corruption at build time cost twenty hours of empty
        # board: the ruined file carried a fresh mtime, so the freshness check
        # above refused to re-fetch it and every rebuild raised BadZipFile.
        tmp.replace(config.GTFS_ZIP)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("downloaded %.0fMB", done / 1e6)


def _check_archive(path: Path, *, expected_bytes: int = 0) -> None:
    """Reject a download that is not a complete, buildable archive.

    Truncation is not reliably an error at the HTTP layer. When the response
    has no `Content-Length` the body is framed by the connection closing, and
    urllib3 cannot tell a finished transfer from a severed one — the read just
    ends and `download()` returns as if all were well. So the bytes are checked
    against the header when there is one, and the file is opened as a zip
    either way, which is the only evidence that does not depend on the server.
    """
    got = path.stat().st_size
    if expected_bytes and got != expected_bytes:
        raise OSError(
            f"truncated download: {got} bytes received, {expected_bytes} declared"
        )
    try:
        with zipfile.ZipFile(path) as zf:
            missing = [m for m in REQUIRED_MEMBERS if m not in zf.namelist()]
    except zipfile.BadZipFile as exc:
        raise OSError(f"downloaded file is not a readable zip: {exc}") from exc
    if missing:
        raise OSError(f"archive is missing {', '.join(missing)}")


def rows(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with zf.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def build(force: bool = False) -> None:
    """Rebuild the timetable cache.

    Refuses to start if another process is already writing this database.
    Concurrency is safe on an ordinary filesystem, but a host process and a
    bind-mounted container sharing the directory will SIGBUS on the mmap'd
    write-ahead `-shm` file — a silent kill with nothing to catch. Callers that
    know their topology is safe, such as the maintenance container, pass
    `force=True`.
    """
    if not force:
        others = locking.other_writers(exclude="ingest")
        if others:
            raise SystemExit(
                "Refusing to rebuild: another ontime process is writing "
                f"{config.DB_PATH}\n  " + "\n  ".join(str(w) for w in others) + "\n"
                "Stop it first. If it is a container bind-mounting this directory, "
                "that combination can kill this process with SIGBUS rather than an "
                f"error. A writer killed uncleanly ages out after "
                f"{locking.STALE_AFTER_SECS}s. Pass --force to override."
            )
    locking.heartbeat("ingest")
    try:
        _build()
    finally:
        locking.release("ingest")


@dataclass
class Cache:
    """Everything the scan keeps, ready to be inserted verbatim."""

    trip_stops: list[tuple[str, int, str, int | None, int | None]] = field(
        default_factory=list
    )
    stops: list[tuple[str, str, str, float, float]] = field(default_factory=list)
    trips: list[
        tuple[str, str, str, str, str, str | None, str | None, int | None, int | None]
    ] = field(default_factory=list)
    calendar: list[tuple[str, int, int, int, int, int, int, int, str, str]] = field(
        default_factory=list
    )
    calendar_dates: list[tuple[str, str, int]] = field(default_factory=list)
    target_calls: list[tuple[str, str, int, int | None]] = field(default_factory=list)


def _read_archive() -> Cache:
    """Scan the zip and return the rows to cache. Touches no database.

    This takes well over a minute against the real 89MB archive, which is why
    it happens before a connection is opened at all. Scanning inside the write
    transaction held the lock for the whole pass, and the dashboard — writing
    every polled position to the same file every 15 seconds — failed with
    "database is locked" from the first DELETE to the final commit.
    """
    cache = Cache()
    with zipfile.ZipFile(config.GTFS_ZIP) as zf:
        log.info("pass 1/2: finding trips that call at the watched stops")
        target: dict[str, list[tuple[str, int, int | None]]] = {}
        for i, r in enumerate(rows(zf, "stop_times.txt")):
            if i and i % 2_000_000 == 0:
                log.info("  %dM rows scanned, %d trips hit", i / 1e6, len(target))
            if r["stop_id"] in config.STOP_IDS:
                target.setdefault(r["trip_id"], []).append(
                    (
                        r["stop_id"],
                        int(r["stop_sequence"]),
                        parse_gtfs_time(r["arrival_time"]),
                    )
                )
        log.info("%d trips call at the watched stops", len(target))

        log.info("pass 2/2: collecting full stop sequences for those trips")
        seq_rows = cache.trip_stops
        for r in rows(zf, "stop_times.txt"):
            if r["trip_id"] in target:
                seq_rows.append(
                    (
                        r["trip_id"],
                        int(r["stop_sequence"]),
                        r["stop_id"],
                        parse_gtfs_time(r["arrival_time"]),
                        parse_gtfs_time(r["departure_time"]),
                    )
                )
        log.info("%d stop_times found", len(seq_rows))

        routes = {
            r["route_id"]: r.get("route_short_name") or r.get("route_long_name", "")
            for r in rows(zf, "routes.txt")
        }

        needed_stops: set[str] = {r[2] for r in seq_rows}
        cache.stops = [
            (
                r["stop_id"],
                r.get("stop_code", ""),
                r["stop_name"],
                float(r["stop_lat"]),
                float(r["stop_lon"]),
            )
            for r in rows(zf, "stops.txt")
            if r["stop_id"] in needed_stops
        ]

        # Endpoints of each trip, used as the strongest matching key against
        # the live feed's OriginRef / DestinationRef / OriginAimedDepartureTime.
        by_trip: dict[str, list[tuple[int, str, int | None, int | None]]] = {}
        for trip_id, seq, stop_id, arr, dep in seq_rows:
            by_trip.setdefault(trip_id, []).append((seq, stop_id, arr, dep))
        endpoints = {}
        for trip_id, items in by_trip.items():
            items.sort()
            endpoints[trip_id] = (
                items[0][1],
                items[0][3] if items[0][3] is not None else items[0][2],
                items[-1][1],
                items[-1][2],
            )

        for r in rows(zf, "trips.txt"):
            tid = r["trip_id"]
            if tid not in target:
                continue
            o_stop, o_dep, d_stop, d_arr = endpoints.get(tid, (None, None, None, None))
            cache.trips.append(
                (
                    tid,
                    r["service_id"],
                    routes.get(r["route_id"], "?"),
                    r.get("trip_headsign", ""),
                    r.get("direction_id", ""),
                    o_stop,
                    d_stop,
                    o_dep,
                    d_arr,
                )
            )

        services = {t[1] for t in cache.trips}
        cache.calendar = [
            (
                r["service_id"],
                int(r["monday"]),
                int(r["tuesday"]),
                int(r["wednesday"]),
                int(r["thursday"]),
                int(r["friday"]),
                int(r["saturday"]),
                int(r["sunday"]),
                r["start_date"],
                r["end_date"],
            )
            for r in rows(zf, "calendar.txt")
            if r["service_id"] in services
        ]

        cache.calendar_dates = [
            (r["service_id"], r["date"], int(r["exception_type"]))
            for r in rows(zf, "calendar_dates.txt")
            if r["service_id"] in services
        ]

        cache.target_calls = [
            (tid, sid, seq, arr) for tid, items in target.items() for sid, seq, arr in items
        ]
    return cache


def _write(cache: Cache) -> None:
    """Swap the cached timetable for a freshly scanned one, in one transaction.

    A concurrent reader sees the old rows right up to the commit and the new
    ones after it, never the empty gap between the DELETEs and the inserts.
    """
    conn = db.connect()
    try:
        db.init(conn)
        cur = conn.cursor()
        for table in CACHED_TABLES:
            cur.execute(f"DELETE FROM {table}")
        cur.executemany(
            "INSERT OR REPLACE INTO trip_stops (trip_id,seq,stop_id,arr,dep) "
            "VALUES (?,?,?,?,?)",
            cache.trip_stops,
        )
        cur.executemany("INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?)", cache.stops)
        cur.executemany(
            "INSERT OR REPLACE INTO trips VALUES (?,?,?,?,?,?,?,?,?)", cache.trips
        )
        cur.executemany(
            "INSERT OR REPLACE INTO calendar VALUES (?,?,?,?,?,?,?,?,?,?)",
            cache.calendar,
        )
        cur.executemany(
            "INSERT OR REPLACE INTO calendar_dates VALUES (?,?,?)", cache.calendar_dates
        )
        cur.executemany(
            "INSERT OR REPLACE INTO target_calls VALUES (?,?,?,?)", cache.target_calls
        )
        # The service date, not the container's. Everything that reads this
        # stamp compares it against Europe/London, and the containers run on
        # UTC, so `date.today()` disagreed for the hour after midnight through
        # BST — long enough for the maintenance loop to decide the cache was
        # stale on every tick and rebuild it around twenty-five times before
        # UTC caught up.
        cur.execute(
            "INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
            (datetime.now(LONDON).date().isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()
    log.info(
        "cached %d stop_times, %d stops, %d trips, %d calendar + %d exception rows, "
        "%d scheduled calls at the watched stops",
        len(cache.trip_stops),
        len(cache.stops),
        len(cache.trips),
        len(cache.calendar),
        len(cache.calendar_dates),
        len(cache.target_calls),
    )


def _build() -> None:
    _write(_read_archive())


def main() -> None:
    logs.setup()
    force = "--force" in sys.argv
    log.info("building timetable cache")
    download(force=force)
    build(force=force)
    log.info("done, cache at %s", config.DB_PATH)


if __name__ == "__main__":
    main()
