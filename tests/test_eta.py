"""Arrival prediction: sequence walking, learned overrides, and edge cases."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ontime import config, eta, siri
from ontime.matching import LONDON, load_trips

from .conftest import (
    STOP_192,
    STOP_ABSENT,
    any_trip_serving,
    load_trip,
    siri_document,
    vehicle_on_trip,
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
