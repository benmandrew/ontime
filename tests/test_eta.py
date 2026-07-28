"""Arrival prediction: sequence walking, learned overrides, and edge cases."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ontime import config, eta, siri
from ontime.matching import LONDON, haversine, load_trips, nearest_on_trip

from .conftest import (
    STOP_192,
    STOP_ABSENT,
    any_trip_serving,
    load_trip,
    siri_document,
    vehicle_on_trip,
    vehicle_xml,
)


@pytest.fixture
def trip(built_db):
    return load_trip(
        built_db, any_trip_serving(built_db, STOP_192), datetime.now(LONDON).date()
    )


def parse_one(xml: str) -> siri.Vehicle:
    return siri.parse(siri_document([xml]))[0]


def target_index(trip, stop_id: str) -> int:
    return next(i for i, s in enumerate(trip.stops) if s[1] == stop_id)


def leg_length(trip, i: int) -> float:
    a, b = trip.stops[i], trip.stops[i + 1]
    return haversine(a[3], a[4], b[3], b[4])


def widest_leg_before(trip, target_idx: int) -> int:
    """The longest gap between two stops upstream of the target.

    The widest leg is where a vehicle can sit furthest from any stop, which is
    where positional errors show up largest and least ambiguously.
    """
    return max(range(target_idx - 1), key=lambda i: leg_length(trip, i))


def vehicle_along(trip, i: int, frac: float) -> siri.Vehicle:
    """A vehicle `frac` of the way along the leg out of stop `i`."""
    _, _, _, alat, alon = trip.stops[i]
    _, _, _, blat, blon = trip.stops[i + 1]
    now = datetime.now(UTC)
    return parse_one(
        vehicle_xml(
            line=trip.route_name,
            origin_ref=trip.origin_stop_id,
            dest_ref=trip.dest_stop_id,
            origin_dep=now,
            recorded_at=now,
            lat=alat + (blat - alat) * frac,
            lon=alon + (blon - alon) * frac,
        )
    )


class TestPredict:
    def test_returns_none_for_stop_not_on_trip(self, trip):
        v = parse_one(vehicle_on_trip(trip, 0))
        assert eta.predict(v, trip, STOP_ABSENT, {}) is None

    def test_returns_none_once_the_stop_is_behind(self, trip):
        idx = target_index(trip, STOP_192)
        v = parse_one(vehicle_on_trip(trip, idx + 2))
        assert eta.predict(v, trip, STOP_192, {}) is None

    def test_predicts_from_upstream_position(self, trip):
        idx = target_index(trip, STOP_192)
        v = parse_one(vehicle_on_trip(trip, max(0, idx - 4)))
        p = eta.predict(v, trip, STOP_192, {})
        assert p is not None
        assert p.minutes > 0
        assert p.stops_away == min(4, idx)
        assert p.source == "timetable"

    def test_closer_vehicle_predicts_sooner(self, trip):
        idx = target_index(trip, STOP_192)
        far = eta.predict(
            parse_one(vehicle_on_trip(trip, max(0, idx - 6))), trip, STOP_192, {}
        )
        near = eta.predict(parse_one(vehicle_on_trip(trip, idx - 1)), trip, STOP_192, {})
        assert far.minutes > near.minutes

    def test_at_the_stop_is_due_now(self, trip):
        idx = target_index(trip, STOP_192)
        p = eta.predict(parse_one(vehicle_on_trip(trip, idx)), trip, STOP_192, {})
        assert p is not None
        assert p.minutes == pytest.approx(0.0, abs=0.5)
        assert p.stops_away == 0

    def test_eta_is_never_negative(self, trip):
        idx = target_index(trip, STOP_192)
        for i in range(0, idx + 1):
            p = eta.predict(parse_one(vehicle_on_trip(trip, i)), trip, STOP_192, {})
            if p is not None:
                assert p.minutes >= 0


class TestFirstSegmentCredit:
    """Only progress the vehicle has genuinely made may be taken off the sum.

    `nearest_on_trip` gives an undirected distance to the closest stop, and the
    correction treated it as distance travelled past that stop. A bus still
    approaching its nearest stop was therefore credited with a chunk of the
    segment beyond it, and the ETA fell the further short of the stop it was:
    walking a vehicle up a 197m leg produced 3.68, 3.84 and 4.00 minutes, an
    estimate rising by 19 seconds as the bus advanced 98m towards its target,
    with the earliest reading some 50 seconds optimistic.
    """

    def test_approaching_a_stop_never_reads_sooner_than_standing_at_it(self, trip):
        idx = target_index(trip, STOP_192)
        approach = widest_leg_before(trip, idx)
        readings = [
            eta.predict(vehicle_along(trip, approach, f), trip, STOP_192, {}).minutes
            for f in (0.6, 0.8, 1.0)
        ]

        assert readings == sorted(readings, reverse=True), "the ETA rose as the bus closed"
        assert all(r >= readings[-1] for r in readings[:-1])

    def test_progress_past_a_stop_is_still_credited(self, trip):
        """The correction must survive being made honest — a bus that really has
        left its nearest stop behind is closer than one still sitting at it."""
        idx = target_index(trip, STOP_192)
        leg = widest_leg_before(trip, idx)
        at_stop = eta.predict(vehicle_along(trip, leg, 0.0), trip, STOP_192, {})
        under_way = eta.predict(vehicle_along(trip, leg, 0.3), trip, STOP_192, {})

        assert under_way.minutes < at_stop.minutes

    def test_credit_uses_the_seconds_the_segment_contributed(self, trip):
        """The subtraction has to undo what the addition put in.

        The loop adds the timetabled gap; the credit looked the segment up again
        and fell back to DEFAULT_SEGMENT_SECS, so a 120-second segment gave back
        24 seconds where 48 was due — and on a learned segment of 300 seconds
        the shortfall is minutes.
        """
        idx = target_index(trip, STOP_192)
        timed = next(
            i
            for i in range(idx - 1)
            if trip.stops[i][2] is not None
            and trip.stops[i + 1][2] is not None
            and trip.stops[i + 1][2] - trip.stops[i][2] != eta.DEFAULT_SEGMENT_SECS
        )
        gap = trip.stops[timed + 1][2] - trip.stops[timed][2]

        part_way = vehicle_along(trip, timed, 0.4)
        assert nearest_on_trip(trip, part_way.lat, part_way.lon)[0] == timed

        at_stop = eta.predict(vehicle_along(trip, timed, 0.0), trip, STOP_192, {})
        moved = eta.predict(part_way, trip, STOP_192, {})
        credited = (at_stop.minutes - moved.minutes) * 60

        assert credited == pytest.approx(0.4 * gap, abs=1.0)

    def test_credit_uses_the_learned_time_when_there_is_one(self, trip):
        """Same rule with history in play: the learned segment is what was added."""
        idx = target_index(trip, STOP_192)
        leg = widest_leg_before(trip, idx)
        part_way = vehicle_along(trip, leg, 0.4)
        local = datetime.fromtimestamp(datetime.now(UTC).timestamp(), LONDON)
        learned = {
            (
                trip.route_name,
                trip.stops[leg][1],
                trip.stops[leg + 1][1],
                local.hour,
                int(local.weekday() >= 5),
            ): (300.0, 390.0, 20)
        }
        now = datetime.now(UTC).timestamp()

        at_stop = eta.predict(
            vehicle_along(trip, leg, 0.0), trip, STOP_192, learned, now=now
        )
        moved = eta.predict(part_way, trip, STOP_192, learned, now=now)

        assert (at_stop.minutes - moved.minutes) * 60 == pytest.approx(0.4 * 300.0, abs=1.0)


class TestClockChangeDays:
    """The two Sundays a year the local day is not twenty-four hours long.

    GTFS measures a trip's times from noon minus twelve hours, not from local
    midnight, and one calendar row in the real cache covers both 2026-10-18 and
    2026-10-25 with 245 trips at identical times. Anchoring on local midnight
    resolved every one of them an hour out on clocks-back Sunday and an hour
    the other way on clocks-forward Sunday, and 60 minutes sits inside
    MAX_PLAUSIBLE_DELAY_SECS, so the mismatch suppression never fired.
    """

    NORMAL = date(2026, 10, 18)
    CLOCKS_BACK = date(2026, 10, 25)
    CLOCKS_FORWARD = date(2026, 3, 29)
    DAYS = (NORMAL, CLOCKS_BACK, CLOCKS_FORWARD)

    def _trip(self, conn, day):
        return load_trip(conn, any_trip_serving(conn, STOP_192), day)

    def test_timetabled_times_land_on_the_clock_they_advertise(self, built_db):
        for day in self.DAYS:
            trip = self._trip(built_db, day)
            arr = trip.stops[target_index(trip, STOP_192)][2]
            local = datetime.fromtimestamp(eta.sched_timestamp(trip, arr), LONDON)

            assert local.date() == day
            assert local.hour * 3600 + local.minute * 60 == arr, day

    def test_a_bus_on_time_is_not_reported_an_hour_out(self, built_db):
        """The board's version of the bug: every bus exactly 60 minutes late.

        `now` here is the advertised clock time built independently of the
        module — a timetabled 15:00 means 15:00 on the local clock, whichever
        offset that day happens to be on — so a vehicle standing at the stop at
        that moment is on time by construction.
        """
        for day in self.DAYS:
            trip = self._trip(built_db, day)
            idx = target_index(trip, STOP_192)
            arr = trip.stops[idx][2]
            due = datetime(
                day.year, day.month, day.day, arr // 3600, arr % 3600 // 60, tzinfo=LONDON
            ).timestamp()

            v = parse_one(vehicle_on_trip(trip, idx))
            p = eta.predict(v, trip, STOP_192, {}, now=due)

            assert p.delay_secs == pytest.approx(0.0, abs=1.0), day

    def test_scheduled_rows_keep_their_clock_times_across_a_clock_change(self, built_db):
        """The no-vehicle rows read off the same anchor, so they moved too.

        All three Sundays share one calendar row and one set of times, so the
        board must show the same departures at the same clock times on each.
        """
        boards = {}
        for day in self.DAYS:
            anchor = (
                datetime(day.year, day.month, day.day, 12, tzinfo=LONDON).timestamp()
                - 12 * 3600
            )
            rows = eta.scheduled_only(
                load_trips(built_db, (day,)), set(), anchor, 26 * 3600
            )
            boards[day] = sorted(
                (r.stop_id, datetime.fromtimestamp(r.eta_ts, LONDON).strftime("%H:%M"))
                for r in rows
            )

        assert boards[self.NORMAL]
        assert boards[self.CLOCKS_BACK] == boards[self.NORMAL]
        assert boards[self.CLOCKS_FORWARD] == boards[self.NORMAL]


class TestLearnedSegments:
    def _segments(self, trip, idx, secs, hour, weekend):
        """Learned stats covering every remaining segment up to `idx`."""
        return {
            (trip.route_name, trip.stops[i][1], trip.stops[i + 1][1], hour, weekend): (
                secs,
                secs * 1.3,
                20,
            )
            for i in range(len(trip.stops) - 1)
        }

    def test_learned_times_override_the_timetable(self, trip):
        idx = target_index(trip, STOP_192)
        start = max(0, idx - 5)
        v = parse_one(vehicle_on_trip(trip, start))
        now = datetime.now(UTC).timestamp()
        local = datetime.fromtimestamp(now, LONDON)

        baseline = eta.predict(v, trip, STOP_192, {}, now=now)
        slow = self._segments(trip, idx, 300.0, local.hour, int(local.weekday() >= 5))
        learned = eta.predict(v, trip, STOP_192, slow, now=now)

        assert learned.source == "learned"
        assert learned.learned_coverage == pytest.approx(1.0)
        # Five minutes per segment is far slower than any timetabled gap here.
        assert learned.minutes > baseline.minutes

    def test_partial_coverage_is_reported(self, trip):
        idx = target_index(trip, STOP_192)
        start = max(0, idx - 4)
        v = parse_one(vehicle_on_trip(trip, start))
        now = datetime.now(UTC).timestamp()
        local = datetime.fromtimestamp(now, LONDON)
        one = {
            (
                trip.route_name,
                trip.stops[start][1],
                trip.stops[start + 1][1],
                local.hour,
                int(local.weekday() >= 5),
            ): (120.0, 150.0, 9)
        }
        p = eta.predict(v, trip, STOP_192, one, now=now)
        assert 0 < p.learned_coverage < 1

    def test_wrong_hour_bucket_is_not_used(self, trip):
        idx = target_index(trip, STOP_192)
        v = parse_one(vehicle_on_trip(trip, max(0, idx - 3)))
        now = datetime.now(UTC).timestamp()
        local = datetime.fromtimestamp(now, LONDON)
        wrong_hour = self._segments(
            trip, idx, 600.0, (local.hour + 5) % 24, int(local.weekday() >= 5)
        )
        p = eta.predict(v, trip, STOP_192, wrong_hour, now=now)
        assert p.learned_coverage == 0.0
        assert p.source == "timetable"


class TestScheduledOnly:
    def test_fills_the_board_for_unmatched_trips(self, built_db):
        today = datetime.now(LONDON).date()
        trips = load_trips(built_db, (today,))
        midnight = datetime(today.year, today.month, today.day, tzinfo=LONDON).timestamp()

        # A window wide enough to catch the fixture's daytime departures.
        rows = eta.scheduled_only(trips, set(), midnight, 24 * 3600)
        assert rows
        assert all(r.source == "scheduled" for r in rows)
        assert all(r.vehicle_ref is None for r in rows)
        assert {r.stop_id for r in rows} <= config.STOP_IDS

    def test_matched_trips_are_excluded(self, built_db):
        today = datetime.now(LONDON).date()
        trips = load_trips(built_db, (today,))
        midnight = datetime(today.year, today.month, today.day, tzinfo=LONDON).timestamp()
        all_ids = {t.trip_id for t in trips}

        assert eta.scheduled_only(trips, all_ids, midnight, 24 * 3600) == []
