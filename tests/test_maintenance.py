"""The periodic upkeep loop that runs in its own container."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta

import pytest

from ontime import config, db, ingest, maintenance
from ontime.matching import LONDON

from .conftest import MINI_GTFS, STOP_192, any_trip_serving, load_trip
from .test_history import FakeVehicle, drive


@pytest.fixture
def cache(data_dir):
    shutil.copy(MINI_GTFS, config.GTFS_ZIP)
    ingest.build()
    return data_dir


class TestRunIngest:
    def test_rebuilds_the_cache(self, data_dir, monkeypatch):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        monkeypatch.setattr(maintenance.ingest, "download", lambda *_a, **_k: None)

        maintenance.run_ingest()

        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) c FROM trips").fetchone()["c"] == 30
        conn.close()


class TestRunLearn:
    def test_derives_events_and_trims(self, cache):
        conn = db.connect()
        trip = load_trip(
            conn, any_trip_serving(conn, STOP_192), datetime.now(LONDON).date()
        )
        # One finished run to learn from, one ancient position to be trimmed.
        drive(conn, trip, "V1", datetime.now(UTC) - timedelta(hours=6), 90, upto=8)
        from ontime import history

        history.record(
            conn,
            FakeVehicle("OLD", datetime.now(UTC) - timedelta(days=90), 53.46, -2.22),
            trip.trip_id,
        )
        conn.commit()
        conn.close()

        maintenance.run_learn()

        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) c FROM stop_events").fetchone()["c"] > 0
        refs = {
            r["vehicle_ref"] for r in conn.execute("SELECT vehicle_ref FROM observations")
        }
        assert "OLD" not in refs, "positions past the retention window must be trimmed"
        conn.close()

    def test_is_safe_on_an_empty_database(self, cache):
        maintenance.run_learn()  # must not raise


class TestCacheIsCurrent:
    def test_false_when_no_database_exists(self, data_dir):
        assert maintenance.cache_is_current() is False

    def test_true_for_a_cache_built_today(self, cache):
        assert maintenance.cache_is_current() is True

    def test_false_when_the_build_date_is_stale(self, cache):
        conn = db.connect()
        conn.execute("UPDATE meta SET value='2020-01-01' WHERE key='built_at'")
        conn.commit()
        conn.close()
        assert maintenance.cache_is_current() is False

    def test_survives_a_restart(self, cache, monkeypatch):
        """Uptime must not decide this: a restarted container still sees a
        fresh cache and must not rebuild, nor skip a genuinely stale one."""
        monkeypatch.setattr(maintenance.time, "monotonic", lambda: 0.0)
        assert maintenance.cache_is_current() is True


class TestMainLoop:
    def _run(self, monkeypatch, *, ticks: int, ingest_fn, learn_fn):
        calls = {"sleep": 0}

        def fake_sleep(_secs):
            calls["sleep"] += 1
            if calls["sleep"] >= ticks:
                raise KeyboardInterrupt

        monkeypatch.setattr(maintenance, "run_ingest", ingest_fn)
        monkeypatch.setattr(maintenance, "run_learn", learn_fn)
        monkeypatch.setattr(maintenance.time, "sleep", fake_sleep)
        with pytest.raises(KeyboardInterrupt):
            maintenance.main()
        return calls

    def test_fresh_cache_is_not_rebuilt(self, cache, monkeypatch):
        counts = {"ingest": 0, "learn": 0}

        def boom():
            counts["ingest"] += 1

        self._run(
            monkeypatch,
            ticks=3,
            ingest_fn=boom,
            learn_fn=lambda: counts.__setitem__("learn", counts["learn"] + 1),
        )
        assert counts["ingest"] == 0, "cache built today must not be rebuilt"
        assert counts["learn"] == 1, "learning runs once, then waits for the interval"

    def test_stale_cache_triggers_a_rebuild(self, cache, monkeypatch):
        conn = db.connect()
        conn.execute("UPDATE meta SET value='2020-01-01' WHERE key='built_at'")
        conn.commit()
        conn.close()

        counts = {"ingest": 0}
        self._run(
            monkeypatch,
            ticks=2,
            ingest_fn=lambda: counts.__setitem__("ingest", counts["ingest"] + 1),
            learn_fn=lambda: None,
        )
        assert counts["ingest"] >= 1

    def test_survives_a_failing_cycle_and_keeps_going(self, cache, monkeypatch):
        """One bad cycle must not take the maintenance container down."""
        counts = {"learn": 0}

        def failing_learn():
            counts["learn"] += 1
            raise RuntimeError("database locked")

        calls = self._run(
            monkeypatch, ticks=3, ingest_fn=lambda: None, learn_fn=failing_learn
        )
        assert counts["learn"] >= 2, "must retry after a failure"
        assert calls["sleep"] == 3

    def test_error_output_is_redacted(self, cache, monkeypatch, api_key, caplog):
        def leaky_learn():
            raise RuntimeError(f"GET /datafeed/?api_key={api_key} failed")

        with caplog.at_level("ERROR"):
            self._run(monkeypatch, ticks=1, ingest_fn=lambda: None, learn_fn=leaky_learn)

        out = caplog.text
        assert api_key not in out
        assert "<redacted>" in out
