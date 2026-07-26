"""Derive test fixtures from real published data.

Fixtures built by hand drift away from what the feeds actually contain, so
both of these are cut from genuine sources: the timetable from the BODS North
West GTFS archive, the vehicle sample from a live SIRI-VM response.

Two things are normalised so the tests stay deterministic. Calendar rows are
rewritten to run every day over a wide date range, and the SIRI sample keeps
its original timestamps — tests shift those to the present themselves.

Run with the North West archive already in data/:
    python scripts/build_fixtures.py
"""

from __future__ import annotations

import csv
import io
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ontime import config

FIXTURES = ROOT / "tests" / "fixtures"
MINI_GTFS = FIXTURES / "mini_gtfs.zip"
SIRI_SAMPLE = FIXTURES / "siri_sample.xml"

# Keep the fixture small but representative: the two busiest watched services.
KEEP_ROUTES = {"192", "50"}
MAX_TRIPS_PER_ROUTE = 15


def rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def write_csv(zf: zipfile.ZipFile, name: str, fields: list[str], data: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(data)
    zf.writestr(name, buf.getvalue())


def build_mini_gtfs() -> None:
    if not config.GTFS_ZIP.exists():
        raise SystemExit(f"Need {config.GTFS_ZIP}. Run: python -m ontime.ingest")

    with zipfile.ZipFile(config.GTFS_ZIP) as zf:
        routes = [
            r
            for r in rows(zf, "routes.txt")
            if (r.get("route_short_name") or "") in KEEP_ROUTES
        ]
        route_ids = {r["route_id"] for r in routes}
        print(f"  routes kept: {len(routes)}")

        # Trips on those routes that call at a watched stop.
        print("  scanning stop_times for watched-stop trips (398MB, one pass)")
        hits: set[str] = set()
        for r in rows(zf, "stop_times.txt"):
            if r["stop_id"] in config.STOP_IDS:
                hits.add(r["trip_id"])

        by_route: dict[str, list[dict]] = {}
        for t in rows(zf, "trips.txt"):
            if t["route_id"] in route_ids and t["trip_id"] in hits:
                by_route.setdefault(t["route_id"], []).append(t)

        trips: list[dict] = []
        for _rid, items in by_route.items():
            trips.extend(sorted(items, key=lambda t: t["trip_id"])[:MAX_TRIPS_PER_ROUTE])
        trip_ids = {t["trip_id"] for t in trips}
        print(f"  trips kept: {len(trips)}")

        print("  collecting full stop sequences (second pass)")
        stop_times = [r for r in rows(zf, "stop_times.txt") if r["trip_id"] in trip_ids]
        needed_stops = {r["stop_id"] for r in stop_times}
        stops = [s for s in rows(zf, "stops.txt") if s["stop_id"] in needed_stops]
        print(f"  stop_times: {len(stop_times)}, stops: {len(stops)}")

        services = {t["service_id"] for t in trips}
        # Normalised so fixtures never expire.
        calendar = [
            {
                "service_id": s,
                "monday": "1",
                "tuesday": "1",
                "wednesday": "1",
                "thursday": "1",
                "friday": "1",
                "saturday": "1",
                "sunday": "1",
                "start_date": "20200101",
                "end_date": "20351231",
            }
            for s in services
        ]

    FIXTURES.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(MINI_GTFS, "w", zipfile.ZIP_DEFLATED) as out:
        write_csv(
            out, "routes.txt", ["route_id", "route_short_name", "route_long_name"], routes
        )
        write_csv(
            out,
            "stops.txt",
            ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"],
            stops,
        )
        write_csv(
            out,
            "trips.txt",
            [
                "route_id",
                "service_id",
                "trip_id",
                "trip_headsign",
                "direction_id",
                "block_id",
                "vehicle_journey_code",
            ],
            trips,
        )
        write_csv(
            out,
            "stop_times.txt",
            [
                "trip_id",
                "arrival_time",
                "departure_time",
                "stop_id",
                "stop_sequence",
                "pickup_type",
                "drop_off_type",
            ],
            stop_times,
        )
        write_csv(
            out,
            "calendar.txt",
            [
                "service_id",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
                "start_date",
                "end_date",
            ],
            calendar,
        )
        write_csv(out, "calendar_dates.txt", ["service_id", "date", "exception_type"], [])

    print(f"  wrote {MINI_GTFS} ({MINI_GTFS.stat().st_size / 1024:.0f}KB)")


def build_siri_sample() -> None:
    """Capture a live SIRI-VM response, trimmed to the watched routes."""
    import requests

    resp = requests.get(
        config.SIRI_VM_URL,
        timeout=30,
        params={
            "api_key": config.api_key(),
            "boundingBox": ",".join(str(v) for v in config.BBOX),
        },
    )
    resp.raise_for_status()
    xml = resp.text

    acts = re.findall(r"<VehicleActivity>.*?</VehicleActivity>", xml, re.S)
    keep = [
        a for a in acts if re.search(r"<PublishedLineName>(192|50)</PublishedLineName>", a)
    ]
    keep = keep[:20]
    header = xml[: xml.index("<VehicleActivity>")]
    footer = "</VehicleMonitoringDelivery></ServiceDelivery></Siri>"

    FIXTURES.mkdir(parents=True, exist_ok=True)
    SIRI_SAMPLE.write_text(header + "".join(keep) + footer, encoding="utf-8")
    print(
        f"  wrote {SIRI_SAMPLE} ({len(keep)} vehicles, "
        f"{SIRI_SAMPLE.stat().st_size / 1024:.0f}KB)"
    )
    assert config.api_key() not in SIRI_SAMPLE.read_text(), "key leaked into fixture"


if __name__ == "__main__":
    print("Building mini GTFS fixture")
    build_mini_gtfs()
    print("Building SIRI-VM fixture")
    build_siri_sample()
