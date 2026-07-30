"""Building the timetable cache from a real GTFS archive."""

from __future__ import annotations

import io
import shutil
import sqlite3
import zipfile

import pytest

from ontime import config, db, ingest

from .conftest import (
    MINI_GTFS,
    STOP_50,
    STOP_192,
    STOP_192_DOWNSTREAM,
    STOP_ABSENT,
)


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

    def test_the_scan_pools_repeated_values(self, data_dir):
        """Identity, not equality — the point is that the rows share objects.

        The scan sets the rebuild's high-water mark, and decoding every row in place
        made one object per row for values drawn from a tiny set: against the
        real archive, 128,208 `stop_id` strings for 583 distinct ones. Pooling
        them took the peak from 68.2MB to 52.8MB with a byte-identical cache.
        Equality would pass whether or not the pool exists, so this counts
        objects.
        """
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        with zipfile.ZipFile(config.GTFS_ZIP) as zf:
            seq_rows, _target = ingest._scan_stop_times(zf)

        assert len(seq_rows) > 1000, "fixture must be big enough for this to mean anything"
        stop_ids = [r[2] for r in seq_rows]
        assert len({id(s) for s in stop_ids}) == len(set(stop_ids))
        times = [r[3] for r in seq_rows if r[3] is not None and r[3] > 256]
        assert times, "fixture must carry times above the small-int cache"
        assert len({id(t) for t in times}) == len(set(times))


class TestPerStopRouteLimits:
    """`Stop.routes` decides what is cached, not just what is displayed.

    The point of restricting University Shopping Centre to the 41 is that the
    other nineteen Oxford Road routes never enter the cache at all: 1,261 trips
    against 2,730. A display-only filter would have paid the whole cost of the
    corridor to hide it.
    """

    def _watch(self, monkeypatch, *stops: config.Stop) -> None:
        monkeypatch.setattr(config, "STOPS", stops)
        monkeypatch.setattr(config, "STOP_IDS", frozenset(s.atco for s in stops))
        monkeypatch.setattr(config, "STOP_BY_ID", {s.atco: s for s in stops})

    def _build(self, monkeypatch, *stops: config.Stop):
        self._watch(monkeypatch, *stops)
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        ingest.build()
        return db.connect()

    def test_a_barred_route_is_never_cached(self, data_dir, monkeypatch):
        """STOP_192 restricted to the 50, which does not run there.

        Its trips have no other watched call, so the trips go too — sequences
        and all, which is the whole saving.
        """
        conn = self._build(
            monkeypatch,
            config.Stop(STOP_192, "N1", "Restricted", "d", routes=frozenset({"50"})),
            config.Stop(STOP_50, "N2", "Open", "d"),
        )
        try:
            routes = {r["route_name"] for r in conn.execute("SELECT route_name FROM trips")}
            assert routes == {"50"}
            assert (
                conn.execute(
                    "SELECT COUNT(*) c FROM target_calls WHERE stop_id = ?", (STOP_192,)
                ).fetchone()["c"]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) c FROM trip_stops WHERE trip_id IN "
                    "(SELECT trip_id FROM trips WHERE route_name = '192')"
                ).fetchone()["c"]
                == 0
            )
        finally:
            conn.close()

    def test_an_allowed_route_is_cached_as_normal(self, data_dir, monkeypatch):
        """The positive control: naming the route that does run changes nothing."""
        conn = self._build(
            monkeypatch,
            config.Stop(STOP_192, "N1", "Restricted", "d", routes=frozenset({"192"})),
        )
        try:
            calls = conn.execute(
                "SELECT COUNT(*) c FROM target_calls WHERE stop_id = ?", (STOP_192,)
            ).fetchone()["c"]
            assert calls == 15
        finally:
            conn.close()

    def test_a_trip_keeps_its_other_watched_call(self, data_dir, monkeypatch):
        """The 191's shape: barred at one watched stop, kept at another.

        It runs through University Shopping Centre, restricted to the 41, and
        on to Hyde Grove, which is open — so the trip must survive with one
        call, not be dropped with both. Reproduced over the 192, whose trips
        call at STOP_192 and then at STOP_192_DOWNSTREAM.
        """
        conn = self._build(
            monkeypatch,
            config.Stop(STOP_192, "N1", "Restricted", "d", routes=frozenset({"50"})),
            config.Stop(STOP_192_DOWNSTREAM, "N2", "Open", "d"),
        )
        try:
            rows = list(conn.execute("SELECT trip_id, stop_id FROM target_calls"))
            assert len(rows) == 15, "the fifteen 192 trips survive"
            assert {r["stop_id"] for r in rows} == {STOP_192_DOWNSTREAM}
            # Still cached in full: the sequence runs through the barred stop.
            assert (
                conn.execute(
                    "SELECT COUNT(*) c FROM trip_stops WHERE stop_id = ?", (STOP_192,)
                ).fetchone()["c"]
                == 15
            )
        finally:
            conn.close()

    def test_no_limits_anywhere_is_a_no_op(self, data_dir, monkeypatch):
        """The unrestricted path must not change while this feature exists."""
        target: ingest.TargetCalls = {"t1": [(STOP_192, 4, 100)]}
        self._watch(monkeypatch, config.Stop(STOP_192, "N1", "Open", "d"))
        assert ingest._apply_route_limits(target, {"t1": "192"}) == 0
        assert target == {"t1": [(STOP_192, 4, 100)]}


def competing_write() -> Exception | None:
    """Write from a second connection that refuses to queue for the lock.

    `timeout=0` is the point: the caller wants to know whether the lock is free
    right now, not to wait until it is.
    """
    conn = sqlite3.connect(config.DB_PATH, timeout=0)
    try:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('probe', '1')")
        conn.commit()
    except sqlite3.OperationalError as exc:
        return exc
    finally:
        conn.close()
    return None


def probe_mid_scan(monkeypatch, probe):
    """Run `probe` once, inside the scan of stop_times.txt.

    Hooked on `_parse_time_bytes`, which the scan calls only when it has found
    a trip worth keeping — so the probe fires partway through the pass rather
    than before it, which is what makes the result mean anything. It used to
    hang off `ingest.rows`; stop_times no longer goes through that, and the
    probe silently stopped firing while the assertions still passed on an
    empty list, so the test proved nothing until it was pointed here.
    """
    real_parse = ingest._parse_time_bytes
    fired = []

    def probing_parse(value):
        if not fired:
            fired.append(probe())
        return real_parse(value)

    monkeypatch.setattr(ingest, "_parse_time_bytes", probing_parse)
    return fired


class TestRebuildDoesNotHoldTheWriteLock:
    """The scan must finish before the database is touched.

    `_build` used to open a connection, `DELETE FROM` six tables and only then
    begin the two-pass scan of `stop_times.txt`, which runs for well over a
    minute against the real 89MB archive. The DELETEs open the write
    transaction, so the lock was held for the whole scan, and the dashboard —
    writing every polled position to the same database every 15 seconds —
    failed with "database is locked" from the first DELETE to the commit.
    """

    def test_another_writer_gets_through_during_the_scan(self, data_dir, monkeypatch):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        conn = db.connect()
        db.init(conn)  # the probe needs somewhere to write
        conn.close()

        fired = probe_mid_scan(monkeypatch, competing_write)
        ingest.build()

        assert fired, "the probe never ran, so the test proves nothing"
        assert fired[0] is None, f"the rebuild held the write lock: {fired[0]}"

    def test_a_reader_sees_the_old_timetable_until_the_commit(self, data_dir, monkeypatch):
        """Emptying the tables early would expose a board with nothing on it."""
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        ingest.build()

        def count_trips() -> int:
            conn = db.connect(readonly=True)
            try:
                return conn.execute("SELECT COUNT(*) c FROM trips").fetchone()["c"]
            finally:
                conn.close()

        fired = probe_mid_scan(monkeypatch, count_trips)
        ingest.build()

        assert fired == [30], "the previous timetable must stay visible until commit"

    def test_the_connection_is_closed_when_the_write_fails(self, data_dir, monkeypatch):
        """A leaked connection keeps a stale WAL reader pinned for the process."""
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        opened = []
        real_connect = db.connect

        def tracking_connect(*a, **k):
            conn = real_connect(*a, **k)
            opened.append(conn)
            return conn

        def exploding_init(_conn):
            raise RuntimeError("boom")

        monkeypatch.setattr(ingest.db, "connect", tracking_connect)
        monkeypatch.setattr(ingest.db, "init", exploding_init)
        with pytest.raises(RuntimeError):
            ingest.build()

        assert opened, "no connection was opened"
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


class FakeResponse:
    """Just enough of a streamed `requests` response for `download`."""

    def __init__(self, body: bytes, content_length: int | None):
        self.body = body
        self.headers = (
            {} if content_length is None else {"content-length": str(content_length)}
        )

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i : i + size]


def serve(monkeypatch, body: bytes, content_length: int | None = None):
    monkeypatch.setattr(
        ingest.requests, "get", lambda *_a, **_k: FakeResponse(body, content_length)
    )


def archive_without(member: str) -> bytes:
    """The real fixture archive, re-zipped with one member left out."""
    buf = io.BytesIO()
    with zipfile.ZipFile(MINI_GTFS) as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            if name != member:
                dst.writestr(name, src.read(name))
    return buf.getvalue()


class TestDownloadValidation:
    """A bad download must never displace a working archive.

    `download` read `content-length` but never compared it against the bytes
    received, and replaced the cached archive unconditionally. When the
    response carries no `Content-Length` the body is framed by the connection
    closing and urllib3 cannot detect truncation, so `download` returned
    happily with a corrupt file in place — which then carried a fresh mtime,
    so the 20-hour freshness check refused to re-fetch it and every rebuild
    raised `BadZipFile` for the next twenty hours.
    """

    @pytest.fixture
    def good(self, data_dir) -> bytes:
        body = MINI_GTFS.read_bytes()
        config.GTFS_ZIP.write_bytes(body)
        return body

    def test_truncation_is_caught_without_a_content_length(
        self, good, data_dir, monkeypatch
    ):
        serve(monkeypatch, good[: len(good) // 2], content_length=None)
        with pytest.raises(OSError):
            ingest.download(force=True)
        assert config.GTFS_ZIP.read_bytes() == good, "the good archive was destroyed"

    def test_a_short_body_is_caught_against_the_declared_length(
        self, good, data_dir, monkeypatch
    ):
        serve(monkeypatch, good[:-100], content_length=len(good))
        with pytest.raises(OSError):
            ingest.download(force=True)
        assert config.GTFS_ZIP.read_bytes() == good

    def test_an_archive_missing_a_required_member_is_rejected(
        self, good, data_dir, monkeypatch
    ):
        body = archive_without("stop_times.txt")
        serve(monkeypatch, body, content_length=len(body))
        with pytest.raises(OSError):
            ingest.download(force=True)
        assert config.GTFS_ZIP.read_bytes() == good

    def test_a_failed_download_leaves_no_part_file(self, good, data_dir, monkeypatch):
        """A stray `.part` is dead weight the size of the archive."""
        serve(monkeypatch, good[: len(good) // 2], content_length=None)
        with pytest.raises(OSError):
            ingest.download(force=True)
        assert not config.GTFS_ZIP.with_suffix(".part").exists()

    def test_a_complete_archive_replaces_the_old_one(self, data_dir, monkeypatch):
        config.GTFS_ZIP.write_bytes(b"stale")
        body = MINI_GTFS.read_bytes()
        serve(monkeypatch, body, content_length=len(body))

        ingest.download(force=True)

        assert config.GTFS_ZIP.read_bytes() == body
        assert not config.GTFS_ZIP.with_suffix(".part").exists()
        ingest.build()  # and it is genuinely buildable


class TestDownloadCaching:
    def test_recent_archive_is_not_refetched(self, data_dir, monkeypatch):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)

        def fail(*_a, **_k):
            raise AssertionError("should not have hit the network")

        monkeypatch.setattr(ingest.requests, "get", fail)
        ingest.download()  # cached copy is fresh, so this must be a no-op
