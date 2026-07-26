"""Observation storage, stop-event derivation, and segment learning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ontime import history
from ontime.matching import LONDON

from .conftest import STOP_192, WIDE_LOOKBACK_HOURS, any_trip_serving, load_trip


@pytest.fixture
def trip(built_db):
    return load_trip(
        built_db, any_trip_serving(built_db, STOP_192), datetime.now(LONDON).date()
    )


class FakeVehicle:
    def __init__(self, ref, when, lat, lon, route="192", bearing=90.0):
        self.vehicle_ref = ref
        self.recorded_at = when
        self.lat = lat
        self.lon = lon
        self.route_name = route
        self.bearing = bearing


def drive(conn, trip, vehicle_ref, start: datetime, secs_per_stop: int, upto=None):
    """Record a vehicle passing each stop of a trip at a fixed cadence."""
    stops = trip.stops[:upto] if upto else trip.stops
    for i, (_seq, _sid, _arr, lat, lon) in enumerate(stops):
        history.record(
            conn,
            FakeVehicle(
                vehicle_ref, start + timedelta(seconds=i * secs_per_stop), lat, lon
            ),
            trip.trip_id,
        )
    conn.commit()


class TestRecord:
    def test_stores_a_position(self, built_db, trip):
        now = datetime.now(UTC)
        history.record(built_db, FakeVehicle("V1", now, 53.46, -2.22), trip.trip_id)
        built_db.commit()
        assert built_db.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 1

    def test_duplicate_timestamps_are_ignored(self, built_db, trip):
        """The feed repeats a record until the vehicle reports again."""
        now = datetime.now(UTC)
        for _ in range(5):
            history.record(built_db, FakeVehicle("V1", now, 53.46, -2.22), trip.trip_id)
        built_db.commit()
        assert built_db.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 1

    def test_unmatched_vehicles_are_still_stored(self, built_db):
        history.record(built_db, FakeVehicle("V2", datetime.now(UTC), 53.4, -2.2), None)
        built_db.commit()
        row = built_db.execute("SELECT trip_id FROM observations").fetchone()
        assert row["trip_id"] is None


class TestTrim:
    def test_removes_only_old_positions(self, built_db, trip):
        now = datetime.now(UTC)
        history.record(built_db, FakeVehicle("NEW", now, 53.46, -2.22), trip.trip_id)
        history.record(
            built_db,
            FakeVehicle("OLD", now - timedelta(days=40), 53.46, -2.22),
            trip.trip_id,
        )
        built_db.commit()

        removed = history.trim(built_db, retain_days=21)
        assert removed == 1
        refs = [
            r["vehicle_ref"]
            for r in built_db.execute("SELECT vehicle_ref FROM observations")
        ]
        assert refs == ["NEW"]


class TestDeriveStopEvents:
    def test_finished_run_produces_one_event_per_stop(self, built_db, trip):
        start = datetime.now(UTC) - timedelta(hours=4)
        drive(built_db, trip, "V1", start, 60)

        written = history.derive_stop_events(
            built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
        )
        assert written == len(trip.stops)

        seqs = [
            r["seq"] for r in built_db.execute("SELECT seq FROM stop_events ORDER BY seq")
        ]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs), "one event per stop"

    def test_in_progress_run_is_skipped(self, built_db, trip):
        """A trip still reporting must not be frozen halfway."""
        drive(built_db, trip, "V1", datetime.now(UTC) - timedelta(minutes=5), 30)
        assert (
            history.derive_stop_events(
                built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
            )
            == 0
        )

    def test_distant_positions_do_not_count_as_passing(self, built_db, trip):
        start = datetime.now(UTC) - timedelta(hours=4)
        for i in range(10):
            history.record(
                built_db,
                FakeVehicle("V1", start + timedelta(seconds=60 * i), 54.5, -1.0),
                trip.trip_id,
            )
        built_db.commit()
        assert (
            history.derive_stop_events(
                built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
            )
            == 0
        )


class TestLearnSegments:
    def test_learns_median_traversal_times(self, built_db, trip):
        base = datetime.now(UTC) - timedelta(hours=6)
        for day, pace in enumerate((60, 60, 90, 60, 120, 60)):
            drive(
                built_db,
                trip,
                f"V{day}",
                base - timedelta(days=day),
                pace,
                upto=6,
            )
        history.derive_stop_events(
            built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
        )
        learned = history.learn_segments(built_db)
        assert learned > 0

        rows = list(built_db.execute("SELECT * FROM segment_stats"))
        assert all(5 <= r["median_secs"] <= 1800 for r in rows)
        assert all(r["p85_secs"] >= r["median_secs"] for r in rows)
        assert all(r["route_name"] == "192" for r in rows)

    def test_implausible_gaps_are_rejected(self, built_db, trip):
        """A three-hour gap is a layover, not a segment traversal."""
        start = datetime.now(UTC) - timedelta(hours=8)
        history.record(
            built_db, FakeVehicle("V1", start, *trip.stops[0][3:5]), trip.trip_id
        )
        history.record(
            built_db,
            FakeVehicle("V1", start + timedelta(hours=3), *trip.stops[1][3:5]),
            trip.trip_id,
        )
        built_db.commit()
        history.derive_stop_events(
            built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
        )
        history.learn_segments(built_db)
        assert built_db.execute("SELECT COUNT(*) c FROM segment_stats").fetchone()["c"] == 0

    def test_only_well_sampled_segments_are_loaded(self, built_db, trip):
        base = datetime.now(UTC) - timedelta(hours=6)
        for day in range(2):  # below history.MIN_SAMPLES
            drive(built_db, trip, f"V{day}", base - timedelta(days=day), 60, upto=4)
        history.derive_stop_events(
            built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
        )
        history.learn_segments(built_db)
        assert history.load_segment_stats(built_db) == {}

    def test_relearning_replaces_rather_than_accumulates(self, built_db, trip):
        base = datetime.now(UTC) - timedelta(hours=6)
        for day in range(6):
            drive(built_db, trip, f"V{day}", base - timedelta(days=day), 60, upto=5)
        history.derive_stop_events(
            built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
        )
        first = history.learn_segments(built_db)
        second = history.learn_segments(built_db)
        assert first == second


class TestStatsSummary:
    def test_reports_zero_on_an_empty_database(self, built_db):
        s = history.stats_summary(built_db)
        assert s == {
            "observations": 0,
            "history_days": 0.0,
            "stop_events": 0,
            "learned_segments": 0,
        }

    def test_counts_accumulated_history(self, built_db, trip):
        drive(built_db, trip, "V1", datetime.now(UTC) - timedelta(hours=4), 60, upto=10)
        s = history.stats_summary(built_db)
        assert s["observations"] == 10
        assert s["history_days"] > 0
