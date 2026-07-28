"""Tests for the learned-model report.

The arithmetic in `median_ci` is the load-bearing claim on the page — it is
what licenses the words "95% confident" — so it is tested against the closed
form of its own coverage probability rather than against remembered outputs.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from ontime import history, segments
from ontime.matching import LONDON

from .conftest import STOP_192, WIDE_LOOKBACK_HOURS, any_trip_serving, load_trip
from .test_history import FakeVehicle, drive


@pytest.fixture
def trip(built_db):
    return load_trip(
        built_db, any_trip_serving(built_db, STOP_192), datetime.now(LONDON).date()
    )


class TestMedianCI:
    """Coverage of [x_(k+1), x_(n-k)] is 1 - 2*P(Bin(n, 0.5) <= k)."""

    @pytest.mark.parametrize("n", range(2, 20))
    def test_reported_confidence_matches_the_binomial_coverage(self, n):
        xs = [float(i) for i in range(n)]
        got = segments.median_ci(xs)
        if got is None:
            return
        lo, hi, conf = got
        k = xs.index(lo)
        expected = 1.0 - 2.0 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n
        assert conf == pytest.approx(expected)
        assert hi == xs[n - 1 - k]

    @pytest.mark.parametrize("n", range(2, 20))
    def test_the_interval_is_the_tightest_one_that_still_qualifies(self, n):
        """Trimming one more sample from each end must drop below the target."""
        xs = [float(i) for i in range(n)]
        got = segments.median_ci(xs)
        if got is None:
            return
        k = xs.index(got[0]) + 1
        if n - 1 - k < k:
            return
        tighter = 1.0 - 2.0 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n
        assert tighter < segments.TARGET_CONF

    @pytest.mark.parametrize("n", range(1, 6))
    def test_no_interval_exists_below_six_samples(self, n):
        """The whole observed range only reaches 93.75% at n=5. Hence CONFIDENT_N."""
        assert segments.median_ci([float(i) for i in range(n)]) is None

    def test_six_samples_is_the_first_that_qualifies(self):
        got = segments.median_ci([float(i) for i in range(segments.CONFIDENT_N)])
        assert got is not None
        assert got[2] >= segments.TARGET_CONF

    def test_the_predictor_gate_is_one_short_of_significance(self):
        """Not an accident worth hiding: the page is built to point at it."""
        assert history.MIN_SAMPLES == segments.CONFIDENT_N - 1

    def test_a_flat_sample_gives_a_zero_width_interval(self):
        lo, hi, _conf = segments.median_ci([7.0] * 8)
        assert lo == hi == 7.0


class TestScheduledCells:
    def test_counts_a_bucket_per_weekday_and_weekend_pattern(self, built_db):
        cells = segments._scheduled_cells(built_db).gaps
        assert cells, "the fixture timetable implies no segments at all"
        assert all(len(k) == 5 for k in cells)
        assert all(0 <= k[3] <= 23 for k in cells)
        assert all(k[4] in (0, 1) for k in cells)
        assert all(v > 0 for v in cells.values())

    def test_a_service_running_all_week_fills_both_day_buckets(self, built_db):
        row = built_db.execute(
            "SELECT 1 monday, 0 tuesday, 0 wednesday, 0 thursday, 0 friday, "
            "1 saturday, 0 sunday"
        ).fetchone()
        assert segments._weekend_flags(row) == (0, 1)

    def test_a_weekday_only_service_fills_one(self, built_db):
        row = built_db.execute(
            "SELECT 1 monday, 1 tuesday, 1 wednesday, 1 thursday, 1 friday, "
            "0 saturday, 0 sunday"
        ).fetchone()
        assert segments._weekend_flags(row) == (0,)


class TestBuild:
    def test_an_empty_database_reports_nothing_learned(self, built_db):
        report = segments.build(built_db)
        assert report["totals"]["observed"] == 0
        assert report["totals"]["used"] == 0
        assert report["segments"] == []
        # Coverage still has a denominator: the timetable is loaded.
        assert report["totals"]["scheduled_cells"] > 0

    def test_the_funnel_only_ever_narrows(self, built_db, trip):
        base = datetime.now(UTC) - timedelta(hours=6)
        for day in range(8):
            drive(built_db, trip, f"V{day}", base - timedelta(days=day), 60, upto=6)
        history.derive_stop_events(
            built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
        )
        t = segments.build(built_db)["totals"]
        assert t["observed"] >= t["stored"] >= t["used"] >= t["significant"]

    def test_the_report_agrees_with_the_table_the_predictor_reads(self, built_db, trip):
        """The page must describe the running model, not a parallel one."""
        base = datetime.now(UTC) - timedelta(hours=6)
        for day in range(7):
            drive(built_db, trip, f"V{day}", base - timedelta(days=day), 60, upto=6)
        history.derive_stop_events(
            built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
        )
        history.learn_segments(built_db)

        report = segments.build(built_db)
        stored = {
            (
                r["route_name"],
                r["from_stop_id"],
                r["to_stop_id"],
                r["hour"],
                r["is_weekend"],
            ): r
            for r in built_db.execute("SELECT * FROM segment_stats")
        }
        assert report["totals"]["stored"] == len(stored)
        assert report["totals"]["used"] == len(history.load_segment_stats(built_db))

        for s in report["segments"]:
            key = (s["route"], s["from_id"], s["to_id"], s["hour"], s["is_weekend"])
            if key not in stored:
                assert s["n"] < segments.STORE_FLOOR
                continue
            assert s["median"] == pytest.approx(stored[key]["median_secs"])
            assert s["p85"] == pytest.approx(stored[key]["p85_secs"])
            assert s["n"] == stored[key]["samples"]

    def test_a_hop_over_an_undetected_stop_is_not_learned(self, built_db, trip):
        """The two events either side of a missed stop are adjacent in the run
        and two stops apart on the road. Pairing them recorded a segment no
        trip runs, which `eta.predict` — keying on scheduled adjacency — could
        never look up. It is counted as skipped instead of learned."""
        base = datetime.now(UTC) - timedelta(hours=6)
        stops = trip.stops[:6]
        missed = stops[2][1]
        for day in range(3):
            start = base - timedelta(days=day)
            for i, (_seq, _sid, _arr, lat, lon) in enumerate(stops):
                if i == 2:  # never observed, as a detection gap would leave it
                    continue
                history.record(
                    built_db,
                    FakeVehicle(f"V{day}", start + timedelta(seconds=i * 60), lat, lon),
                    trip.trip_id,
                )
        built_db.commit()
        history.derive_stop_events(
            built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
        )

        sampled = history.segment_samples(built_db)
        assert sampled.skipped >= 3, "one hop per run should have been refused"
        spanning = [
            k for k in sampled.buckets if k[1] == stops[1][1] and k[2] == stops[3][1]
        ]
        assert not spanning, "a segment spanning the undetected stop was learned"
        assert not any(missed in (k[1], k[2]) for k in sampled.buckets)

        report = segments.build(built_db)
        assert report["totals"]["unreachable"] == 0
        assert report["totals"]["skipped"] == sampled.skipped

    def test_every_learned_segment_is_one_the_predictor_could_look_up(self, built_db, trip):
        """The whole point of the adjacency rule: no learning is spent unusably."""
        base = datetime.now(UTC) - timedelta(hours=6)
        for day in range(5):
            drive(built_db, trip, f"V{day}", base - timedelta(days=day), 60, upto=8)
        history.derive_stop_events(
            built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
        )
        report = segments.build(built_db)
        assert report["segments"], "nothing was learned, so the check is vacuous"
        assert all(s["reachable"] for s in report["segments"])

    def test_a_stop_pair_timetabled_within_one_minute_still_counts_as_adjacent(
        self, built_db
    ):
        """Published times are rounded to the minute, so a zero-second gap is
        a real adjacency even though it is useless as a delta comparator."""
        sched = segments._scheduled_cells(built_db)
        rows = built_db.execute(
            "SELECT DISTINCT t.route_name r, a.stop_id f, b.stop_id t2 "
            "FROM trip_stops a JOIN trip_stops b "
            "  ON b.trip_id = a.trip_id AND b.seq = a.seq + 1 "
            "JOIN trips t ON t.trip_id = a.trip_id "
            "WHERE COALESCE(b.arr, b.dep) <= a.dep"
        ).fetchall()
        if not rows:
            pytest.skip("the fixture timetable has no same-minute stop pair")

        # Every same-minute pair is still an adjacency the predictor can key on.
        for row in rows:
            assert (row["r"], row["f"], row["t2"]) in sched.adjacent

        # And at least one of them is reachable *only* through that set: it has
        # no usable gap on any trip, so deriving adjacency from `gaps` would
        # have lost it. That is the regression this guards.
        with_gap = {k[:3] for k in sched.gaps}
        only_adjacent = [r for r in rows if (r["r"], r["f"], r["t2"]) not in with_gap]
        assert only_adjacent, "no pair exercises the split; the sets are equivalent here"

    def test_histogram_accounts_for_every_segment(self, built_db, trip):
        base = datetime.now(UTC) - timedelta(hours=6)
        for day in range(4):
            drive(built_db, trip, f"V{day}", base - timedelta(days=day), 60, upto=6)
        history.derive_stop_events(
            built_db, {trip.trip_id: trip}, lookback_hours=WIDE_LOOKBACK_HOURS
        )
        report = segments.build(built_db)
        assert (
            sum(h["segments"] for h in report["histogram"]) == report["totals"]["observed"]
        )
