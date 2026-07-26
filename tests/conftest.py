"""Shared fixtures.

Two kinds of vehicle data are used deliberately. `real_siri_xml` is a recorded
live BODS response and exercises the parser against the structure the feed
actually produces. Synthesised vehicles, built by `vehicle_on_trip`, are placed
on trips that exist in the timetable fixture so matching and prediction can be
asserted exactly — a recorded vehicle is almost never running one of the 30
trips kept in the cut-down archive.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ontime import config, db, ingest
from ontime.matching import LONDON, Trip

FIXTURES = Path(__file__).parent / "fixtures"
MINI_GTFS = FIXTURES / "mini_gtfs.zip"
SIRI_SAMPLE = FIXTURES / "siri_sample.xml"

# Present in the fixture archive.
STOP_192 = "1800EB06241"  # MANADTDW, Cavanagh Close
STOP_50 = "1800SB13961"  # MANGPWTD, Swinton Grove
# Watched but absent from the cut-down archive.
STOP_ABSENT = "1800EB01881"  # MANADGMT, Hyde Grove


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirect all on-disk state into a temporary directory."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ontime.sqlite")
    monkeypatch.setattr(config, "GTFS_ZIP", tmp_path / "gtfs.zip")
    return tmp_path


@pytest.fixture
def built_db(data_dir):
    """A timetable cache built from the real, cut-down GTFS archive."""
    shutil.copy(MINI_GTFS, config.GTFS_ZIP)
    ingest.build()
    conn = db.connect()
    yield conn
    conn.close()


@pytest.fixture
def real_siri_xml() -> bytes:
    return SIRI_SAMPLE.read_bytes()


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("BODS_API_KEY", "test-key-abc123")
    return "test-key-abc123"


def load_trip(conn: sqlite3.Connection, trip_id: str, service_date) -> Trip:
    stops = [
        (r["seq"], r["stop_id"], r["arr"], r["lat"], r["lon"])
        for r in conn.execute(
            "SELECT ts.seq, ts.stop_id, ts.arr, s.lat, s.lon "
            "FROM trip_stops ts JOIN stops s ON s.stop_id = ts.stop_id "
            "WHERE ts.trip_id = ? ORDER BY ts.seq",
            (trip_id,),
        )
    ]
    r = conn.execute("SELECT * FROM trips WHERE trip_id = ?", (trip_id,)).fetchone()
    return Trip(
        trip_id=r["trip_id"],
        route_name=r["route_name"],
        headsign=r["headsign"] or "",
        origin_stop_id=r["origin_stop_id"] or "",
        dest_stop_id=r["dest_stop_id"] or "",
        first_dep=r["first_dep"] if r["first_dep"] is not None else -1,
        service_date=service_date,
        stops=stops,
    )


def any_trip_serving(conn: sqlite3.Connection, stop_id: str) -> str:
    row = conn.execute(
        "SELECT trip_id FROM target_calls WHERE stop_id = ? ORDER BY trip_id LIMIT 1",
        (stop_id,),
    ).fetchone()
    assert row is not None, f"fixture has no trip calling at {stop_id}"
    return row["trip_id"]


def service_midnight(day) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=LONDON).timestamp()


def vehicle_xml(
    *,
    line: str,
    origin_ref: str,
    dest_ref: str,
    origin_dep: datetime,
    recorded_at: datetime,
    lat: float,
    lon: float,
    vehicle_ref: str = "TEST01",
    journey_ref: str = "9999",
    dest_name: str = "Test_Destination",
    bearing: int = 90,
) -> str:
    """One VehicleActivity element, shaped like the live feed."""
    return f"""<VehicleActivity>
<RecordedAtTime>{recorded_at.isoformat()}</RecordedAtTime>
<ItemIdentifier>test-{vehicle_ref}</ItemIdentifier>
<ValidUntilTime>{(recorded_at + timedelta(hours=1)).isoformat()}</ValidUntilTime>
<MonitoredVehicleJourney>
<LineRef>{line}</LineRef>
<DirectionRef>outbound</DirectionRef>
<FramedVehicleJourneyRef>
<DataFrameRef>{recorded_at.date().isoformat()}</DataFrameRef>
<DatedVehicleJourneyRef>{journey_ref}</DatedVehicleJourneyRef>
</FramedVehicleJourneyRef>
<PublishedLineName>{line}</PublishedLineName>
<OperatorRef>TEST</OperatorRef>
<OriginRef>{origin_ref}</OriginRef>
<OriginName>Test_Origin</OriginName>
<DestinationRef>{dest_ref}</DestinationRef>
<DestinationName>{dest_name}</DestinationName>
<OriginAimedDepartureTime>{origin_dep.isoformat()}</OriginAimedDepartureTime>
<VehicleLocation>
<Longitude>{lon}</Longitude>
<Latitude>{lat}</Latitude>
</VehicleLocation>
<Bearing>{bearing}</Bearing>
<BlockRef>1</BlockRef>
<VehicleRef>{vehicle_ref}</VehicleRef>
</MonitoredVehicleJourney>
</VehicleActivity>"""


def siri_document(activities: list[str], now: datetime | None = None) -> bytes:
    now = now or datetime.now(UTC)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Siri version="2.0" xmlns="http://www.siri.org.uk/siri">'
        "<ServiceDelivery>"
        f"<ResponseTimestamp>{now.isoformat()}</ResponseTimestamp>"
        "<ProducerRef>TEST</ProducerRef>"
        '<VehicleMonitoringDelivery version="2.0">'
        f"<ResponseTimestamp>{now.isoformat()}</ResponseTimestamp>"
        f"{''.join(activities)}"
        "</VehicleMonitoringDelivery></ServiceDelivery></Siri>"
    ).encode()


def vehicle_on_trip(
    trip: Trip,
    stop_index: int,
    *,
    now: datetime | None = None,
    vehicle_ref: str = "TEST01",
    offset_m: float = 0.0,
) -> str:
    """Place a vehicle at a given point along a real trip from the fixture.

    `offset_m` nudges it east, which is how a position between two stops is
    simulated without hand-picking coordinates.
    """
    now = now or datetime.now(UTC)
    _seq, _sid, _arr, lat, lon = trip.stops[stop_index]
    lon += offset_m / (111_320 * 0.6)  # crude metres-to-degrees at this latitude
    origin_dep = datetime.fromtimestamp(
        service_midnight(trip.service_date) + trip.first_dep, UTC
    )
    return vehicle_xml(
        line=trip.route_name,
        origin_ref=trip.origin_stop_id,
        dest_ref=trip.dest_stop_id,
        origin_dep=origin_dep,
        recorded_at=now,
        lat=lat,
        lon=lon,
        vehicle_ref=vehicle_ref,
    )
