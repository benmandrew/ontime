"""Download the BODS North West GTFS feed and cache the parts we need.

The published archive is about 89MB zipped and 544MB unpacked, with
`stop_times.txt` alone accounting for 398MB. Nothing is unpacked to disk and
no file is read whole into memory: every pass streams rows out of the zip.

Two passes over `stop_times.txt` are needed. The first finds which trips call
at a watched stop; the second collects the complete stop sequence for exactly
those trips, which is what the arrival estimator walks.
"""

from __future__ import annotations

import csv
import io
import sys
import time
import zipfile
from collections.abc import Iterator
from datetime import date

import requests

from . import config, db

CHUNK = 1 << 20


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
        print(f"  using cached {config.GTFS_ZIP.name} ({mb:.0f}MB, <20h old)")
        return

    print(f"  downloading {config.GTFS_URL}")
    tmp = config.GTFS_ZIP.with_suffix(".part")
    with requests.get(config.GTFS_URL, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(CHUNK):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    pct = 100 * done / total
                    print(
                        f"\r  {done / 1e6:6.1f}/{total / 1e6:.0f}MB ({pct:4.1f}%)",
                        end="",
                        flush=True,
                    )
    print()
    tmp.replace(config.GTFS_ZIP)


def rows(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with zf.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def build() -> None:
    conn = db.connect()
    db.init(conn)
    cur = conn.cursor()
    for table in (
        "stops",
        "trips",
        "trip_stops",
        "target_calls",
        "calendar",
        "calendar_dates",
    ):
        cur.execute(f"DELETE FROM {table}")

    with zipfile.ZipFile(config.GTFS_ZIP) as zf:
        print("  pass 1/2: finding trips that call at the watched stops")
        target: dict[str, list[tuple[str, int, int | None]]] = {}
        for i, r in enumerate(rows(zf, "stop_times.txt")):
            if i and i % 2_000_000 == 0:
                print(
                    f"\r    {i / 1e6:.0f}M rows scanned, {len(target)} trips hit",
                    end="",
                    flush=True,
                )
            if r["stop_id"] in config.STOP_IDS:
                target.setdefault(r["trip_id"], []).append(
                    (
                        r["stop_id"],
                        int(r["stop_sequence"]),
                        parse_gtfs_time(r["arrival_time"]),
                    )
                )
        print(f"\r    {len(target)} trips call at the watched stops" + " " * 20)

        print("  pass 2/2: collecting full stop sequences for those trips")
        seq_rows = []
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
        cur.executemany(
            "INSERT OR REPLACE INTO trip_stops (trip_id,seq,stop_id,arr,dep) "
            "VALUES (?,?,?,?,?)",
            seq_rows,
        )
        print(f"    {len(seq_rows)} stop_times cached")

        routes = {
            r["route_id"]: r.get("route_short_name") or r.get("route_long_name", "")
            for r in rows(zf, "routes.txt")
        }

        needed_stops: set[str] = {r[2] for r in seq_rows}
        stop_rows = [
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
        cur.executemany("INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?)", stop_rows)
        print(f"    {len(stop_rows)} stops cached")

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

        trip_rows = []
        for r in rows(zf, "trips.txt"):
            tid = r["trip_id"]
            if tid not in target:
                continue
            o_stop, o_dep, d_stop, d_arr = endpoints.get(tid, (None, None, None, None))
            trip_rows.append(
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
        cur.executemany(
            "INSERT OR REPLACE INTO trips VALUES (?,?,?,?,?,?,?,?,?)", trip_rows
        )
        print(f"    {len(trip_rows)} trips cached")

        services = {t[1] for t in trip_rows}
        cal = [
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
        cur.executemany("INSERT OR REPLACE INTO calendar VALUES (?,?,?,?,?,?,?,?,?,?)", cal)

        cd = [
            (r["service_id"], r["date"], int(r["exception_type"]))
            for r in rows(zf, "calendar_dates.txt")
            if r["service_id"] in services
        ]
        cur.executemany("INSERT OR REPLACE INTO calendar_dates VALUES (?,?,?)", cd)
        print(f"    {len(cal)} calendar + {len(cd)} exception rows cached")

        calls = [
            (tid, sid, seq, arr) for tid, items in target.items() for sid, seq, arr in items
        ]
        cur.executemany("INSERT OR REPLACE INTO target_calls VALUES (?,?,?,?)", calls)
        print(f"    {len(calls)} scheduled calls at the watched stops")

    cur.execute(
        "INSERT OR REPLACE INTO meta VALUES ('built_at', ?)", (date.today().isoformat(),)
    )
    conn.commit()
    conn.close()


def main() -> None:
    force = "--force" in sys.argv
    print("Building timetable cache")
    download(force=force)
    build()
    print(f"Done. Cache at {config.DB_PATH}")


if __name__ == "__main__":
    main()
