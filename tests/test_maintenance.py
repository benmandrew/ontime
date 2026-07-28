"""The periodic upkeep loop that runs in its own container."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, date, datetime, timedelta

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

    def test_a_non_wal_database_is_still_readable(self, cache):
        """The read-only probe used to fail on a database in delete mode.

        Selecting a journal mode is a write, so `PRAGMA journal_mode=WAL` on a
        read-only handle returns SQLITE_READONLY. That surfaced here as
        "no usable cache", and the loop rebuilt on every tick — a rebuild that
        cannot change the answer, because the new database is read the same way.
        """
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("PRAGMA journal_mode=delete")
        conn.commit()
        conn.close()

        assert maintenance.cache_is_current() is True


class TestBuildStampIsAServiceDate:
    """`build` writes the stamp; `cache_is_current` reads it. Both must mean
    the same date.

    The containers run on UTC and every date decision in the project is
    London's, which through BST disagree for the hour after midnight. A stamp
    written from the container's date was a day behind for that hour, so the
    comparison below could not be satisfied and the loop rebuilt the timetable
    on every tick until UTC caught up — a minute-long write lock taken roughly
    every two minutes against the volume the board polls.
    """

    # 00:30 in London, still 23:30 on the 27th in UTC.
    AFTER_MIDNIGHT = datetime(2026, 7, 28, 0, 30, tzinfo=LONDON)

    @pytest.fixture
    def after_midnight(self, monkeypatch):
        instant = self.AFTER_MIDNIGHT

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return instant.astimezone(UTC).replace(tzinfo=None)
                return instant.astimezone(tz)

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return instant.astimezone(UTC).date()

        monkeypatch.setattr(ingest, "datetime", Frozen)
        monkeypatch.setattr(maintenance, "datetime", Frozen)
        # The container's own clock, frozen to the same instant. Without this
        # the test passes against the bug on any machine whose local date
        # already agrees with London's, which is most of them most of the time.
        monkeypatch.setattr(ingest, "date", FrozenDate, raising=False)

    def test_the_stamp_is_the_london_date(self, data_dir, after_midnight):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        ingest.build()

        conn = db.connect()
        row = conn.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
        conn.close()
        assert row["value"] == "2026-07-28", "UTC was still on the 27th"

    def test_a_cache_is_current_the_moment_it_is_built(self, data_dir, after_midnight):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        ingest.build()

        assert maintenance.cache_is_current() is True, (
            "a rebuild that does not satisfy the staleness check loops forever"
        )


def run_loop(monkeypatch, *, ticks: int, ingest_fn, learn_fn):
    """Drive `maintenance.main` for a fixed number of ticks, then break out."""
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


@pytest.fixture
def stale_cache(cache):
    """A cache whose build stamp will never satisfy `cache_is_current`."""
    conn = db.connect()
    conn.execute("UPDATE meta SET value='2020-01-01' WHERE key='built_at'")
    conn.commit()
    conn.close()
    return cache


class TestIngestFloor:
    """Rebuilds need a floor between them, and only between successful ones.

    Anything that keeps `cache_is_current` returning False — a feed still
    publishing yesterday's data, a stopped clock — used to re-enter the rebuild
    on every 60s tick: a 90MB download and a full timetable write, once a
    minute, against the volume the board is polling.
    """

    def test_a_permanently_stale_cache_rebuilds_once_not_every_tick(
        self, stale_cache, monkeypatch
    ):
        counts = {"ingest": 0}
        run_loop(
            monkeypatch,
            ticks=5,
            ingest_fn=lambda: counts.__setitem__("ingest", counts["ingest"] + 1),
            learn_fn=lambda: None,
        )
        assert counts["ingest"] == 1, "the rebuild must not repeat within the floor"

    def test_the_floor_lifts_once_the_interval_has_passed(self, stale_cache, monkeypatch):
        counts = {"ingest": 0}
        clock = {"t": 0.0}

        def advancing_monotonic() -> float:
            # A whole interval elapses between every reading of the clock.
            clock["t"] += maintenance.MIN_INGEST_INTERVAL
            return clock["t"]

        monkeypatch.setattr(maintenance.time, "monotonic", advancing_monotonic)
        run_loop(
            monkeypatch,
            ticks=4,
            ingest_fn=lambda: counts.__setitem__("ingest", counts["ingest"] + 1),
            learn_fn=lambda: None,
        )
        assert counts["ingest"] == 4, "a floor is a delay, not a one-shot latch"

    def test_a_failing_rebuild_is_retried_on_the_next_tick(self, stale_cache, monkeypatch):
        """A cold volume has no timetable at all; throttling that is an hour of
        empty board. Only a rebuild that succeeded may hold the floor."""
        counts = {"ingest": 0}

        def failing_ingest():
            counts["ingest"] += 1
            raise RuntimeError("no timetable in this volume yet")

        run_loop(monkeypatch, ticks=3, ingest_fn=failing_ingest, learn_fn=lambda: None)
        assert counts["ingest"] == 3, "a failed rebuild must retry immediately"


class TestMainLoop:
    def _run(self, monkeypatch, *, ticks: int, ingest_fn, learn_fn):
        return run_loop(monkeypatch, ticks=ticks, ingest_fn=ingest_fn, learn_fn=learn_fn)

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
