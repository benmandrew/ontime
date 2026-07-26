"""Building the timetable cache from a real GTFS archive."""

from __future__ import annotations

import shutil

import pytest

from ontime import config, db, ingest

from .conftest import MINI_GTFS, STOP_50, STOP_192, STOP_ABSENT


class TestParseGtfsTime:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("00:00:00", 0),
            ("06:04:00", 6 * 3600 + 4 * 60),
            ("23:59:59", 86399),
            ("25:10:00", 25 * 3600 + 10 * 60),  # post-midnight trips exceed 24h
            ("", None),
            ("nonsense", None),
        ],
    )
    def test_values(self, raw, expected):
        assert ingest.parse_gtfs_time(raw) == expected


class TestBuild:
    def test_caches_expected_row_counts(self, built_db):
        counts = {
            table: built_db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            for table in ("trips", "stops", "trip_stops", "target_calls", "calendar")
        }
        assert counts["trips"] == 30
        assert counts["target_calls"] == 30
        assert counts["stops"] == 102
        assert counts["trip_stops"] == 1416
        assert counts["calendar"] > 0

    def test_only_watched_stops_become_targets(self, built_db):
        stop_ids = {
            r["stop_id"]
            for r in built_db.execute("SELECT DISTINCT stop_id FROM target_calls")
        }
        assert stop_ids <= config.STOP_IDS
        assert STOP_192 in stop_ids
        assert STOP_50 in stop_ids
        # Not served by routes 192 or 50, so absent from this cut-down archive.
        assert STOP_ABSENT not in stop_ids

    def test_full_sequences_are_kept_not_just_the_watched_call(self, built_db):
        """Prediction needs every stop of the trip, not only the one watched."""
        row = built_db.execute(
            "SELECT trip_id, COUNT(*) n FROM trip_stops GROUP BY trip_id ORDER BY n LIMIT 1"
        ).fetchone()
        assert row["n"] >= 30

    def test_trip_endpoints_and_times_populated(self, built_db):
        for r in built_db.execute("SELECT * FROM trips"):
            assert r["origin_stop_id"]
            assert r["dest_stop_id"]
            assert r["first_dep"] is not None
            assert r["last_arr"] is not None
            assert r["last_arr"] > r["first_dep"]

    def test_rebuild_is_idempotent(self, built_db, data_dir):
        before = built_db.execute("SELECT COUNT(*) c FROM trip_stops").fetchone()["c"]
        built_db.close()
        ingest.build()
        conn = db.connect()
        after = conn.execute("SELECT COUNT(*) c FROM trip_stops").fetchone()["c"]
        conn.close()
        assert before == after

    def test_records_build_date(self, built_db):
        row = built_db.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
        assert row is not None


class TestDownloadCaching:
    def test_recent_archive_is_not_refetched(self, data_dir, monkeypatch):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)

        def fail(*_a, **_k):
            raise AssertionError("should not have hit the network")

        monkeypatch.setattr(ingest.requests, "get", fail)
        ingest.download()  # cached copy is fresh, so this must be a no-op
