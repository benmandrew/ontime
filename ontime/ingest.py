"""Download the BODS North West GTFS feed and cache the parts we need.

The published archive is about 89MB zipped and 544MB unpacked, with
`stop_times.txt` alone accounting for 398MB. Nothing is unpacked to disk and
no file is read whole into memory: every pass streams rows out of the zip.

One pass over `stop_times.txt` does both jobs. The file is grouped by trip, so
the scan buffers the trip in hand and keeps its complete stop sequence — which
is what the arrival estimator walks — only once a row shows it calling at a
watched stop. Two passes were needed before that grouping was verified.

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


# The columns the stop_times scan reads, in the order the cached tuples want.
STOP_TIME_COLUMNS = (
    "trip_id",
    "arrival_time",
    "departure_time",
    "stop_id",
    "stop_sequence",
)

# Read size for the byte scan. 1MB measured 2.62s at 72MB peak RSS against the
# real archive; 4MB was no faster and cost 27MB more. Those absolute figures
# predate the value pooling in `_scan_stop_times`, which cut the peak by a fifth
# without touching the chunk size; the comparison between the two sizes is what
# this constant rests on, and that has not been re-run.
SCAN_CHUNK = 1 << 20

TripStops = list[tuple[str, int, str, int | None, int | None]]
TargetCalls = dict[str, list[tuple[str, int, int | None]]]


def _parse_time_bytes(value: bytes) -> int | None:
    return parse_gtfs_time(value.decode())


def _scan_stop_times(zf: zipfile.ZipFile) -> tuple[TripStops, TargetCalls]:
    """Collect every watched trip's full stop sequence in a single pass.

    This used to be two passes over the largest member in the archive — 4.6
    million rows, 398MB — one to find the trips calling at a watched stop and
    another to collect their sequences. One pass suffices because the file is
    grouped by trip: measured across all 106,058 trips in the real archive, no
    trip's rows are ever interrupted by another's and at most one trip is open
    at a time. So the rows of the trip in hand can be buffered — about
    forty-four of them — and either kept or dropped the moment its last row
    goes by. Nothing needs the whole file in memory.

    The other half of the saving is not reading through `csv`. `DictReader`
    builds a dict per row and costs 7.7s of a 7.9s pass; decompressing the
    whole member is 0.26s of it, so the parsing, not the I/O or the zip, was
    the expense. Splitting bytes and decoding only the 54,131 rows actually
    kept takes the pass to 2.6s. Together: 16.0s to 2.9s for the whole
    rebuild, with byte-identical output.

    Splitting on commas is safe here despite `stop_headsign` being a quoted
    free-text field — every row in the feed carries `""` for it — because the
    split is bounded at the last column this reads and every column it reads
    lies before that one. A comma inside the headsign can therefore only
    disturb the remainder, which is discarded. If some future archive reorders
    the columns so that stops being true, the scan hands off to `csv` rather
    than quietly caching shifted times.
    """
    with zf.open("stop_times.txt") as raw:
        header = raw.readline().decode("utf-8-sig").strip().split(",")
        try:
            idx = [header.index(c) for c in STOP_TIME_COLUMNS]
        except ValueError as exc:
            raise OSError(f"stop_times.txt has no {exc} column") from exc
        i_trip, i_arr, i_dep, i_stop, i_seq = idx
        limit = max(idx)
        if "stop_headsign" in header and header.index("stop_headsign") < limit:
            log.warning(
                "stop_times.txt puts quoted text before a column we read; using csv"
            )
            return _scan_stop_times_csv(zf)

        watched = {s.encode() for s in config.STOP_IDS}
        trip_stops: TripStops = []
        target: TargetCalls = {}
        cur: bytes | None = None
        buf: list[tuple[bytes, bytes, bytes, bytes]] = []
        calls: list[tuple[bytes, bytes, bytes]] = []

        # The scan sets the rebuild's high-water mark — the prune and everything
        # after it run under a smaller footprint and never exceed it — so the
        # only allocations that matter are the ones below, once per kept row.
        #
        # Decoding in place made a fresh object every time: 128,208 `stop_id`
        # strings for 583 distinct values, and an int per arrival and departure
        # drawn from a few thousand distinct times. Both are pooled instead. The
        # keys are the raw bytes, which are transient, and the values are what
        # the rows then share.
        stop_pool: dict[bytes, str] = {}
        time_pool: dict[bytes, int | None] = {}

        def pooled_time(raw: bytes) -> int | None:
            hit = time_pool.get(raw)
            if hit is None and raw not in time_pool:
                hit = time_pool[raw] = _parse_time_bytes(raw)
            return hit

        def flush() -> None:
            # Only trips that called at a watched stop are worth decoding, which
            # is why the buffer holds raw bytes until this point: 2,730 trips of
            # 111,484 survive the scan, so 98% of the work is never done at all.
            # Per-stop route limits cut that again, to 1,261, but only afterwards
            # — `_apply_route_limits` needs a route and this file has no column
            # for one.
            if cur is None or not calls:
                return
            trip_id = cur.decode()
            target[trip_id] = [
                (stop_pool.setdefault(sid, sid.decode()), int(seq), pooled_time(arr))
                for sid, seq, arr in calls
            ]
            trip_stops.extend(
                (
                    trip_id,
                    int(seq),
                    stop_pool.setdefault(sid, sid.decode()),
                    pooled_time(arr),
                    pooled_time(dep),
                )
                for sid, seq, arr, dep in buf
            )

        tail = b""
        while True:
            chunk = raw.read(SCAN_CHUNK)
            if not chunk:
                break
            lines = (tail + chunk).split(b"\n")
            tail = lines.pop()
            for line in lines:
                if not line:
                    continue
                f = line.split(b",", limit + 1)
                trip = f[i_trip]
                if trip != cur:
                    flush()
                    cur, buf, calls = trip, [], []
                stop = f[i_stop]
                buf.append((stop, f[i_seq], f[i_arr], f[i_dep]))
                if stop in watched:
                    calls.append((stop, f[i_seq], f[i_arr]))
        if tail.strip():
            f = tail.split(b",", limit + 1)
            trip = f[i_trip]
            if trip != cur:
                flush()
                cur, buf, calls = trip, [], []
            stop = f[i_stop]
            buf.append((stop, f[i_seq], f[i_arr], f[i_dep]))
            if stop in watched:
                calls.append((stop, f[i_seq], f[i_arr]))
        flush()
    return trip_stops, target


def _scan_stop_times_csv(zf: zipfile.ZipFile) -> tuple[TripStops, TargetCalls]:
    """Correct-at-any-cost fallback for an archive the byte scan will not read.

    Kept because the alternative to a slow rebuild is no rebuild: the cache is
    only replaced once a scan succeeds, so raising here would leave the board
    on a timetable that ages by a day for every day the layout stayed odd.
    """
    trip_stops: TripStops = []
    target: TargetCalls = {}
    for r in rows(zf, "stop_times.txt"):
        trip_id, stop_id = r["trip_id"], r["stop_id"]
        seq = int(r["stop_sequence"])
        arr = parse_gtfs_time(r["arrival_time"])
        trip_stops.append(
            (trip_id, seq, stop_id, arr, parse_gtfs_time(r["departure_time"]))
        )
        if stop_id in config.STOP_IDS:
            target.setdefault(trip_id, []).append((stop_id, seq, arr))
    return [r for r in trip_stops if r[0] in target], target


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
    # Held for the whole build, not stamped once at the start. The scan measures
    # a few seconds against the real archive — 2.16s on Apple silicon, 3.27s on
    # x86-64 Linux at four watched stops — comfortably inside the 90s staleness
    # window, but those are warm-page-cache figures on two machines, and
    # `run_learn` under the same guard grows with the observation count rather
    # than the archive. A record that refreshes cannot age out of any of them.
    with locking.writing("ingest"):
        _build()


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


def _apply_route_limits(target: TargetCalls, trip_route: dict[str, str]) -> int:
    """Drop watched calls a stop's `Stop.routes` does not admit. Returns trips lost.

    Mutates `target` in place and removes any trip left with no watched call at
    all — those are exactly the trips the cache exists to hold, so a trip that
    reaches a watched stop only on a barred route is of no further use and its
    whole stop sequence goes with it.

    A trip is *not* dropped merely because one of its calls was barred. The 191
    runs through University Shopping Centre, restricted to the 41, and on to
    Hyde Grove, which is unrestricted: it loses the first call and keeps the
    second, so it still appears on Hyde Grove's board and is still learned from.
    """
    if all(s.routes is None for s in config.STOPS):
        return 0
    before = len(target)
    for trip_id, calls in list(target.items()):
        route = trip_route.get(trip_id, "?")
        kept = [c for c in calls if config.stop_serves(c[0], route)]
        if kept:
            target[trip_id] = kept
        else:
            del target[trip_id]
    return before - len(target)


def _read_archive() -> Cache:
    """Scan the zip and return the rows to cache. Touches no database.

    This is the slowest part of a rebuild — seconds against the real archive,
    and it was 17.2s before the scan became a single pass — which is why it
    happens before a connection is opened at all. Scanning inside the write
    transaction held the lock for that whole pass, and the dashboard — writing
    every polled position to the same file every 15 seconds — failed with
    "database is locked" from the first DELETE to the final commit. Splitting
    it leaves the lock held for the inserts alone: 0.09s, half a percent of
    the rebuild.
    """
    cache = Cache()
    with zipfile.ZipFile(config.GTFS_ZIP) as zf:
        log.info("scanning stop_times.txt for trips calling at the watched stops")
        seq_rows, target = _scan_stop_times(zf)
        log.info(
            "%d trips call at the watched stops, %d stop_times", len(target), len(seq_rows)
        )

        routes = {
            r["route_id"]: r.get("route_short_name") or r.get("route_long_name", "")
            for r in rows(zf, "routes.txt")
        }

        # trips.txt is read here rather than further down because the route is
        # what decides whether a call survives a per-stop limit, and the scan
        # cannot know it — `stop_times.txt` carries no route. Only the trips the
        # scan kept are held: a few thousand rows out of 111,484.
        trip_rows = [r for r in rows(zf, "trips.txt") if r["trip_id"] in target]
        trip_route = {r["trip_id"]: routes.get(r["route_id"], "?") for r in trip_rows}

        dropped = _apply_route_limits(target, trip_route)
        if dropped:
            seq_rows = [r for r in seq_rows if r[0] in target]
            trip_rows = [r for r in trip_rows if r["trip_id"] in target]
            log.info(
                "%d trips dropped by a per-stop route limit, %d remain with %d stop_times",
                dropped,
                len(target),
                len(seq_rows),
            )

        cache.trip_stops = seq_rows
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

        for r in trip_rows:
            tid = r["trip_id"]
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
