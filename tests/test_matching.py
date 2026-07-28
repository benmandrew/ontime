"""Geometry, service-day arithmetic, and the tiered vehicle-to-trip matcher."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from ontime import siri
from ontime.matching import (
    LONDON,
    MAX_OFF_ROUTE_M,
    haversine,
    load_trips,
    match,
    nearest_on_trip,
    service_day_offsets,
    service_midnight,
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


def opposite_direction(trip):
    """The other direction of a real trip, shaped the way the real cache is.

    The cut-down archive holds one direction per route, because that is what the
    watched stops are served by; production caches both whenever two watched
    stops sit on opposite directions of one route. The shape that matters here
    is taken from the real data: the two termini share an ATCO code between
    directions — Hazel Grove Park and Ride does — while every intermediate stop
    carries the across-the-road code, which appears on no forward trip.
    """
    reversed_geometry = list(reversed(trip.stops))
    stops = []
    for i, (forward, back) in enumerate(zip(trip.stops, reversed_geometry, strict=True)):
        terminus = i in (0, len(trip.stops) - 1)
        stop_id = back[1] if terminus else back[1][:-1] + "2"
        stops.append((forward[0], stop_id, forward[2], back[3], back[4]))
    return replace(
        trip,
        trip_id=f"{trip.trip_id}-reverse",
        origin_stop_id=stops[0][1],
        dest_stop_id=stops[-1][1],
        stops=stops,
    )


def vehicle_at(trip, lat: float, lon: float) -> str:
    """A vehicle carrying this trip's refs exactly, but positioned by hand."""
    away = datetime.fromtimestamp(service_midnight(trip.service_date) + trip.first_dep, UTC)
    return vehicle_xml(
        line=trip.route_name,
        origin_ref=trip.origin_stop_id,
        dest_ref=trip.dest_stop_id,
        origin_dep=away,
        recorded_at=datetime.now(UTC),
        lat=lat,
        lon=lon,
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


class TestClockChanges:
    """The two Sundays a year when the local day is not twenty-four hours long.

    GTFS anchors a service day on noon minus twelve hours rather than local
    midnight, and the clock changes are the whole reason for the definition:
    one calendar row in the real cache covers both 2026-10-18 and 2026-10-25
    with 245 trips at identical times, which only works if the anchor moves
    with the clocks. Anchoring on local midnight reported every bus exactly 60
    minutes late for the whole of clocks-back Sunday and 60 early on
    clocks-forward Sunday — under MAX_PLAUSIBLE_DELAY_SECS, so the suppression
    that catches mismatched trips never saw it.
    """

    CLOCKS_BACK = date(2026, 10, 25)  # 02:00 BST becomes 01:00 GMT
    CLOCKS_FORWARD = date(2026, 3, 29)  # 01:00 GMT becomes 02:00 BST
    NORMAL = date(2026, 10, 18)  # the Sunday before, on the same calendar row

    @staticmethod
    def local_midnight(day: date) -> float:
        return datetime(day.year, day.month, day.day, tzinfo=LONDON).timestamp()

    def test_anchor_is_noon_minus_twelve_hours(self):
        assert (
            service_midnight(self.CLOCKS_BACK)
            == datetime(2026, 10, 25, 0, 0, tzinfo=UTC).timestamp()
        )
        assert (
            service_midnight(self.CLOCKS_FORWARD)
            == datetime(2026, 3, 28, 23, 0, tzinfo=UTC).timestamp()
        )
        assert (
            service_midnight(self.NORMAL)
            == datetime(2026, 10, 17, 23, 0, tzinfo=UTC).timestamp()
        )

    def test_local_midnight_is_an_hour_out_on_the_transition_days(self):
        """The hour the old anchor lost, and the 363 days it got away with it."""
        assert service_midnight(self.CLOCKS_BACK) - self.local_midnight(
            self.CLOCKS_BACK
        ) == pytest.approx(3600)
        assert service_midnight(self.CLOCKS_FORWARD) - self.local_midnight(
            self.CLOCKS_FORWARD
        ) == pytest.approx(-3600)
        assert service_midnight(self.NORMAL) == pytest.approx(
            self.local_midnight(self.NORMAL)
        )

    def test_previous_service_day_is_measured_from_its_own_anchor(self):
        """A flat +86400 loses the hour the clocks give back overnight.

        Adding a constant day to the wall clock put 02:30 on clocks-back Sunday
        at 26:30 of Saturday's service day, when the timetable calls it 27:30.
        The cache holds 44 calls running out to 27:30 and these corridors run
        every 15 to 30 minutes at night, so a real trip an hour off was always
        there to absorb the error: it scored dep_delta 0 and won at tier 1,
        turning a delay that should have been withheld into a confident
        60-minute phantom.
        """
        back = service_day_offsets(datetime(2026, 10, 25, 2, 30, tzinfo=UTC))
        assert back[1] == (date(2026, 10, 24), 27 * 3600 + 30 * 60)

        # The mirror image: clocks-forward Sunday swallows an hour, so 03:30 BST
        # is 26:30 of Saturday and not the 27:30 a flat day would make of it.
        forward = service_day_offsets(datetime(2026, 3, 29, 2, 30, tzinfo=UTC))
        assert forward[1] == (date(2026, 3, 28), 26 * 3600 + 30 * 60)

        # Every other night really is a day long, and must stay that way.
        plain = service_day_offsets(datetime(2026, 7, 15, 1, 30, tzinfo=UTC))
        assert plain[1] == (date(2026, 7, 14), 26 * 3600 + 30 * 60)

    def test_same_service_day_still_tracks_the_wall_clock(self):
        """The offset that was already right is not to be disturbed."""
        cases = [
            (datetime(2026, 10, 25, 2, 30, tzinfo=UTC), 2 * 3600 + 30 * 60),
            (datetime(2026, 3, 29, 2, 30, tzinfo=UTC), 3 * 3600 + 30 * 60),
            (datetime(2026, 7, 15, 11, 0, tzinfo=UTC), 12 * 3600),
        ]
        for when, expected in cases:
            assert service_day_offsets(when)[0][1] == expected

    def test_post_midnight_vehicle_matches_the_run_it_is_actually_on(self, built_db):
        """The wrong-run consequence of the flat day, reproduced at tier 1.

        Two real trips a clock hour apart, both departing after midnight the way
        44 cached calls do, and a vehicle away at 27:30 reporting from 02:30 GMT
        on clocks-back Sunday. The flat +86400 read it as 26:30 and handed it
        the earlier run with dep_delta 0.
        """
        day = date(2026, 10, 24)
        base = load_trip(built_db, any_trip_serving(built_db, STOP_192), day)
        early = replace(base, trip_id="dep-2630", first_dep=26 * 3600 + 30 * 60)
        late = replace(base, trip_id="dep-2730", first_dep=27 * 3600 + 30 * 60)

        away = datetime.fromtimestamp(service_midnight(day) + late.first_dep, UTC)
        assert away == datetime(2026, 10, 25, 2, 30, tzinfo=UTC)
        xml = vehicle_xml(
            line=base.route_name,
            origin_ref=base.origin_stop_id,
            dest_ref=base.dest_stop_id,
            origin_dep=away,
            recorded_at=away,
            lat=base.stops[2][3],
            lon=base.stops[2][4],
        )

        got = match(siri.parse(siri_document([xml]))[0], [early, late])
        assert got is not None
        assert got.trip.trip_id == "dep-2730", "the flat day picked the run an hour early"
        assert got.tier == 1
        assert got.dep_delta == 0


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

    def test_the_right_trip_survives_whatever_order_candidates_arrive_in(self, built_db):
        """The direction filter must not borrow one candidate's stop ids.

        Opposite-direction trips reach the candidate set for exactly the reason
        this gate exists — the termini share a code. Resolving the vehicle's
        position once, on whichever trip happened to sort first, meant a reverse
        trip's across-the-road stop id was looked up in every forward trip,
        found in none of them, and the correct match was thrown away. Whether
        the board showed the bus came down to list order.
        """
        today = datetime.now(LONDON).date()
        trip = self._trip(built_db)
        reverse = opposite_direction(trip)
        vehicle = siri.parse(siri_document([vehicle_on_trip(trip, 2)]))[0]
        trips = load_trips(built_db, (today,))

        for candidates in ([reverse, *trips], [*trips, reverse]):
            got = match(vehicle, candidates)
            assert got is not None
            assert got.trip.trip_id == trip.trip_id
            assert got.tier == 1

    def test_the_opposite_direction_is_still_rejected_on_its_own(self, built_db):
        """Order independence must not cost the guarantee it came from.

        The reverse trip shares its terminus code with the forward one, so the
        destination alone cannot separate them. It is rejected because the
        destination sits behind the vehicle, not in front of it.
        """
        trip = self._trip(built_db)
        vehicle = siri.parse(siri_document([vehicle_on_trip(trip, 2)]))[0]
        assert match(vehicle, [opposite_direction(trip)]) is None

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


class TestGeometricSanity:
    """Matching refs are not proof the vehicle is running the trip.

    Operators keep broadcasting a finished journey's origin, destination and
    aimed departure while the bus drives back to the depot, and the
    RecordedAtTime stays fresh, so the staleness filter never sees it — the
    live-timestamped form of the ghost problem. Those refs matched at tier 1
    from twelve kilometres off the corridor, with schedule_confident set, and
    the board grew a departure 54 minutes and 45 stops away that was never
    going to happen.
    """

    def _trip(self, conn):
        return load_trip(
            conn, any_trip_serving(conn, STOP_192), datetime.now(LONDON).date()
        )

    def test_off_corridor_vehicle_is_rejected_despite_perfect_refs(self, built_db):
        today = datetime.now(LONDON).date()
        trip = self._trip(built_db)
        # Twelve kilometres east of the origin. Its nearest stop is still the
        # origin, so the destination is downstream and the direction gate lets
        # it through: geometry is the only thing left that can reject it.
        stray = siri.parse(siri_document([vehicle_on_trip(trip, 0, offset_m=12000)]))[0]
        assert nearest_on_trip(trip, stray.lat, stray.lon)[0] == 0
        assert nearest_on_trip(trip, stray.lat, stray.lon)[1] > 10_000

        assert match(stray, load_trips(built_db, (today,))) is None

    def test_a_bus_between_two_stops_is_not_rejected(self, built_db):
        """The bound must never throw off a bus that is exactly where it should be.

        Halfway along the widest gap in the fixture is the furthest from a stop
        a vehicle on this corridor can legitimately be, and it still has to
        match, with the schedule confidence its refs earn it.
        """
        today = datetime.now(LONDON).date()
        trip = self._trip(built_db)
        widest = max(
            range(len(trip.stops) - 1),
            key=lambda i: haversine(
                trip.stops[i][3],
                trip.stops[i][4],
                trip.stops[i + 1][3],
                trip.stops[i + 1][4],
            ),
        )
        lat = (trip.stops[widest][3] + trip.stops[widest + 1][3]) / 2
        lon = (trip.stops[widest][4] + trip.stops[widest + 1][4]) / 2
        vehicle = siri.parse(siri_document([vehicle_at(trip, lat, lon)]))[0]

        got = match(vehicle, load_trips(built_db, (today,)))
        assert got is not None
        assert got.trip.trip_id == trip.trip_id
        assert got.schedule_confident

    def test_the_bound_clears_the_widest_real_stop_spacing(self):
        """1,538m is the longest gap between consecutive stops in the real cache,
        so a bus stranded halfway along one reads 769m from its nearest stop.
        A tighter bound would reject buses that are precisely where they belong.
        """
        assert MAX_OFF_ROUTE_M > 769


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
