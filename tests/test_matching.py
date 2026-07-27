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
        assert got.trip.trip_id == trip.trip_id
        assert got.tier == 1, "origin, destination and departure time should pin it exactly"
        assert got.schedule_confident

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
        assert got is not None and got.trip.trip_id == trip.trip_id
        assert vehicle.journey_ref == "604253"


class TestDirectionGate:
    """Regression cover for wrong-direction buses on the board.

    A watched stop served in one direction only caches trips for that direction
    — all 467 live 192 trips run towards Manchester, because MANADTDW is the
    northwest-bound stop. Ten of thirty-six live 192s were bound for Stockport,
    and the time-only tier assigned them to northbound runs. The dashboard then
    showed arrivals for buses travelling away from the stop, one of them
    "362 minutes early".
    """

    def _trip(self, conn):
        return load_trip(
            conn, any_trip_serving(conn, STOP_192), datetime.now(LONDON).date()
        )

    def test_vehicle_heading_the_other_way_is_rejected(self, built_db):
        today = datetime.now(LONDON).date()
        trip = self._trip(built_db)
        # Same route, same road, but bound for a stop behind it in this sequence.
        behind = trip.stops[1][1]
        xml = vehicle_on_trip(trip, len(trip.stops) - 3).replace(
            f"<DestinationRef>{trip.dest_stop_id}</DestinationRef>",
            f"<DestinationRef>{behind}</DestinationRef>",
        )
        vehicle = siri.parse(siri_document([xml]))[0]
        assert match(vehicle, load_trips(built_db, (today,))) is None

    def test_destination_this_timetable_never_serves_is_rejected(self, built_db):
        today = datetime.now(LONDON).date()
        trip = self._trip(built_db)
        xml = vehicle_on_trip(trip, 2).replace(
            f"<DestinationRef>{trip.dest_stop_id}</DestinationRef>",
            "<DestinationRef>1800NOWHERE1</DestinationRef>",
        )
        vehicle = siri.parse(siri_document([xml]))[0]
        assert match(vehicle, load_trips(built_db, (today,))) is None

    def test_vehicle_heading_our_way_still_matches(self, built_db):
        today = datetime.now(LONDON).date()
        trip = self._trip(built_db)
        vehicle = siri.parse(siri_document([vehicle_on_trip(trip, 2)]))[0]
        got = match(vehicle, load_trips(built_db, (today,)))
        assert got is not None and got.trip.trip_id == trip.trip_id

    def test_intermediate_destination_is_accepted(self, built_db):
        """A short working terminating part-way is still coming towards us."""
        today = datetime.now(LONDON).date()
        trip = self._trip(built_db)
        ahead = trip.stops[-2][1]
        xml = vehicle_on_trip(trip, 1).replace(
            f"<DestinationRef>{trip.dest_stop_id}</DestinationRef>",
            f"<DestinationRef>{ahead}</DestinationRef>",
        )
        vehicle = siri.parse(siri_document([xml]))[0]
        assert match(vehicle, load_trips(built_db, (today,))) is not None


class TestMatchConfidence:
    def test_tie_is_broken_on_departure_time_not_distance(self, built_db):
        """Every trip on a route shares the road, so distance cannot choose.

        Picking the geometrically closest path among candidates within the time
        tolerance selected an essentially arbitrary run and reported delays of
        20 to 45 minutes for buses running to time.
        """
        today = datetime.now(LONDON).date()
        trips = load_trips(built_db, (today,))
        trip = load_trip(built_db, any_trip_serving(built_db, STOP_192), today)
        vehicle = siri.parse(siri_document([vehicle_on_trip(trip, 2)]))[0]

        got = match(vehicle, trips)
        assert got is not None
        best_possible = min(
            abs(t.first_dep - trip.first_dep)
            for t in trips
            if t.route_name == trip.route_name
        )
        assert abs(got.trip.first_dep - trip.first_dep) == best_possible

    def test_schedule_confidence_gates_the_delay_claim(self, built_db):
        from ontime import eta

        today = datetime.now(LONDON).date()
        trip = load_trip(built_db, any_trip_serving(built_db, STOP_192), today)
        target = next(i for i, s in enumerate(trip.stops) if s[1] == STOP_192)
        vehicle = siri.parse(siri_document([vehicle_on_trip(trip, target - 1)]))[0]

        # Anchor `now` near the scheduled arrival, so the delay this produces is
        # a plausible one and is not suppressed for being absurd.
        now = eta.sched_timestamp(trip, trip.stops[target][2]) - 120

        confident = eta.predict(
            vehicle, trip, STOP_192, {}, now=now, schedule_confident=True
        )
        unsure = eta.predict(vehicle, trip, STOP_192, {}, now=now, schedule_confident=False)

        assert confident.delay_secs is not None
        assert unsure.delay_secs is None, "an unidentified run must not claim a delay"
        # The arrival itself is positional, so it survives either way.
        assert unsure.minutes == pytest.approx(confident.minutes)

    def test_implausible_delay_is_suppressed(self, built_db):
        """Six hours out is a mismatched trip, not a late bus."""
        from ontime import eta

        today = datetime.now(LONDON).date()
        trip = load_trip(built_db, any_trip_serving(built_db, STOP_192), today)
        vehicle = siri.parse(siri_document([vehicle_on_trip(trip, 1)]))[0]
        target = next(i for i, s in enumerate(trip.stops) if s[1] == STOP_192)

        shifted = eta.sched_timestamp(trip, trip.stops[target][2])
        p = eta.predict(
            vehicle, trip, STOP_192, {}, now=shifted + 6 * 3600, schedule_confident=True
        )
        assert p is None or p.delay_secs is None
