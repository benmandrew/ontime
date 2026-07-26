"""Geometry, service-day arithmetic, and the tiered vehicle-to-trip matcher."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ontime import siri
from ontime.matching import (
    LONDON,
    haversine,
    load_trips,
    match,
    nearest_on_trip,
    service_day_offsets,
)

from .conftest import (
    STOP_50,
    STOP_192,
    any_trip_serving,
    load_trip,
    siri_document,
    vehicle_on_trip,
    vehicle_xml,
)


class TestHaversine:
    def test_zero_distance(self):
        assert haversine(53.46, -2.22, 53.46, -2.22) == pytest.approx(0.0, abs=1e-6)

    def test_known_separation(self):
        """The two Upper Brook Street stops are about 220m apart."""
        d = haversine(53.464085, -2.222301, 53.46225, -2.223839)
        assert 180 < d < 260

    def test_symmetric(self):
        a = haversine(53.40, -2.30, 53.50, -2.10)
        b = haversine(53.50, -2.10, 53.40, -2.30)
        assert a == pytest.approx(b)


class TestServiceDay:
    def test_midday_yields_one_service_day(self):
        when = datetime(2026, 7, 15, 12, 30, 0, tzinfo=LONDON)
        offsets = service_day_offsets(when)
        assert len(offsets) == 1
        day, secs = offsets[0]
        assert day.isoformat() == "2026-07-15"
        assert secs == 12 * 3600 + 30 * 60

    def test_after_midnight_belongs_to_both_days(self):
        """A 00:30 bus may be yesterday's 24:30 trip."""
        when = datetime(2026, 7, 15, 0, 30, 0, tzinfo=LONDON)
        offsets = service_day_offsets(when)
        assert len(offsets) == 2
        assert offsets[1][0].isoformat() == "2026-07-14"
        assert offsets[1][1] == 24 * 3600 + 30 * 60

    def test_british_summer_time_offset_applied(self):
        """A July UTC instant is one hour earlier than local time."""
        utc_noon = datetime(2026, 7, 15, 11, 0, 0, tzinfo=UTC)
        day, secs = service_day_offsets(utc_noon)[0]
        assert secs == 12 * 3600
        assert day.isoformat() == "2026-07-15"


class TestLoadTrips:
    def test_loads_fixture_trips(self, built_db):
        today = datetime.now(LONDON).date()
        trips = load_trips(built_db, (today,))
        assert len(trips) == 30
        assert {t.route_name for t in trips} == {"192", "50"}
        assert all(len(t.stops) >= 30 for t in trips)

    def test_stops_are_ordered_by_sequence(self, built_db):
        today = datetime.now(LONDON).date()
        for trip in load_trips(built_db, (today,)):
            seqs = [s[0] for s in trip.stops]
            assert seqs == sorted(seqs)

    def test_endpoints_match_stop_sequence(self, built_db):
        today = datetime.now(LONDON).date()
        for trip in load_trips(built_db, (today,)):
            assert trip.origin_stop_id == trip.stops[0][1]
            assert trip.dest_stop_id == trip.stops[-1][1]


class TestNearestOnTrip:
    def test_finds_exact_stop(self, built_db):
        today = datetime.now(LONDON).date()
        trip = load_trip(built_db, any_trip_serving(built_db, STOP_192), today)
        idx, dist, _seq = nearest_on_trip(trip, trip.stops[5][3], trip.stops[5][4])
        assert idx == 5
        assert dist == pytest.approx(0.0, abs=1.0)


class TestMatcher:
    def _trip(self, conn, stop_id):
        return load_trip(conn, any_trip_serving(conn, stop_id), datetime.now(LONDON).date())

    def test_matches_vehicle_placed_on_a_real_trip(self, built_db):
        today = datetime.now(LONDON).date()
        trip = self._trip(built_db, STOP_192)
        xml = siri_document([vehicle_on_trip(trip, 3)])
        vehicle = siri.parse(xml)[0]

        got = match(vehicle, load_trips(built_db, (today,)))
        assert got is not None
        assert got.trip_id == trip.trip_id

    def test_returns_none_for_unknown_route(self, built_db):
        today = datetime.now(LONDON).date()
        now = datetime.now(UTC)
        xml = siri_document(
            [
                vehicle_xml(
                    line="999",
                    origin_ref="X",
                    dest_ref="Y",
                    origin_dep=now,
                    recorded_at=now,
                    lat=53.46,
                    lon=-2.22,
                )
            ]
        )
        assert match(siri.parse(xml)[0], load_trips(built_db, (today,))) is None

    def test_returns_none_when_far_from_every_path(self, built_db):
        """A route-192 vehicle in the Pennines matches nothing."""
        today = datetime.now(LONDON).date()
        now = datetime.now(UTC)
        xml = siri_document(
            [
                vehicle_xml(
                    line="192",
                    origin_ref="unknown-origin",
                    dest_ref="unknown-dest",
                    origin_dep=now - timedelta(hours=6),
                    recorded_at=now,
                    lat=53.60,
                    lon=-1.90,
                )
            ]
        )
        assert match(siri.parse(xml)[0], load_trips(built_db, (today,))) is None

    def test_journey_code_join_is_not_used(self, built_db):
        """The documented trap: GTFS vehicle_journey_code is not the SIRI ref.

        The vehicle below carries a DatedVehicleJourneyRef that matches nothing
        in the archive, and matching must still succeed.
        """
        today = datetime.now(LONDON).date()
        trip = self._trip(built_db, STOP_50)
        xml = vehicle_on_trip(trip, 2).replace(
            "<DatedVehicleJourneyRef>9999</DatedVehicleJourneyRef>",
            "<DatedVehicleJourneyRef>604253</DatedVehicleJourneyRef>",
        )
        vehicle = siri.parse(siri_document([xml]))[0]

        got = match(vehicle, load_trips(built_db, (today,)))
        assert got is not None and got.trip_id == trip.trip_id
        assert vehicle.journey_ref == "604253"
