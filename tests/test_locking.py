"""The writer heartbeat, and the guard that stopped a SIGBUS being possible.

Regression cover for a real incident: a container left running with the data
directory bind-mounted kept polling and writing, and a later `python -m
ontime.ingest` on the host died with a bare `bus error`. Write-ahead logging
memory-maps the `-shm` file unconditionally, and two kernels mapping it across
a VirtioFS bind mount is not survivable in-process. The guard cannot prevent
the mapping, so it prevents the situation and says why.
"""

from __future__ import annotations

import contextlib
import shutil
import threading
import time

import pytest

from ontime import config, ingest, locking

from .conftest import MINI_GTFS


@pytest.fixture(autouse=True)
def _isolated(data_dir):
    """Every test in this module gets its own data directory."""
    return data_dir


def record_of(kind: str):
    """The heartbeat file this process would register `kind` under."""
    return config.DATA_DIR / ".writers" / locking._writer_id(kind)


@contextlib.contextmanager
def another_process(pid: int = 424242):
    """Register heartbeats inside the block as if from a second process.

    A different pid is the whole of the difference between the host's ingest
    and the maintenance container's, which is the pairing the guard exists to
    catch and the one it was blind to.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(locking.os, "getpid", lambda: pid)
        yield


class TestHeartbeat:
    def test_records_a_writer(self):
        locking.heartbeat("poller")
        found = locking.other_writers(exclude="ingest")
        assert [w.name for w in found] == ["poller"]
        assert found[0].pid > 0
        assert found[0].age_secs < 5

    def test_excludes_itself(self):
        locking.heartbeat("ingest")
        assert locking.other_writers(exclude="ingest") == []

    def test_release_removes_it(self):
        locking.heartbeat("poller")
        locking.release("poller")
        assert locking.other_writers(exclude="ingest") == []

    def test_release_is_safe_when_absent(self):
        locking.release("never-existed")  # must not raise

    def test_stale_writers_are_ignored(self):
        locking.heartbeat("poller")
        path = record_of("poller")
        pid, host, _ = path.read_text().split()
        path.write_text(f"{pid} {host} {time.time() - 3600:.0f}\n")
        assert locking.other_writers(exclude="ingest") == []

    def test_repeated_heartbeats_do_not_duplicate(self):
        for _ in range(5):
            locking.heartbeat("poller")
        assert len(locking.other_writers(exclude="ingest")) == 1

    def test_corrupt_heartbeat_is_ignored(self):
        d = config.DATA_DIR / ".writers"
        d.mkdir(parents=True, exist_ok=True)
        (d / "poller.somehost.7").write_text("not a heartbeat")
        assert locking.other_writers(exclude="ingest") == []

    def test_missing_directory_is_not_an_error(self):
        assert locking.other_writers(exclude="ingest") == []

    def test_heartbeat_never_raises_on_unwritable_dir(self, monkeypatch, tmp_path):
        locked = tmp_path / "readonly"
        locked.mkdir()
        locked.chmod(0o500)
        monkeypatch.setattr(config, "DATA_DIR", locked)
        try:
            locking.heartbeat("poller")  # PermissionError must be swallowed
            assert locking.other_writers(exclude="ingest") == []
        finally:
            locked.chmod(0o700)

    def test_heartbeat_never_raises_on_a_nonsense_path(self, monkeypatch, tmp_path):
        """Not every path failure is an OSError; an embedded null is a ValueError."""
        monkeypatch.setattr(config, "DATA_DIR", tmp_path / "\0bad")
        locking.heartbeat("poller")


class TestWriterIdentity:
    def test_two_processes_of_one_kind_see_each_other(self):
        """Both write "ingest", and each used to mistake the other for itself."""
        with another_process(pid=1001):
            locking.heartbeat("ingest")
        with another_process(pid=1002):
            locking.heartbeat("ingest")

        found = locking.other_writers(exclude="ingest")

        assert sorted(w.pid for w in found) == [1001, 1002]
        assert {w.name for w in found} == {"ingest"}

    def test_excludes_only_this_process(self):
        locking.heartbeat("ingest")
        with another_process(pid=1001):
            locking.heartbeat("ingest")

        found = locking.other_writers(exclude="ingest")

        assert [w.pid for w in found] == [1001]

    def test_release_leaves_another_process_of_the_same_kind_alone(self):
        with another_process(pid=1001):
            locking.heartbeat("ingest")
        locking.heartbeat("ingest")
        locking.release("ingest")

        assert [w.pid for w in locking.other_writers(exclude="ingest")] == [1001]

    def test_the_refusal_message_still_reads_plainly(self):
        """`Writer.__str__` is what the user sees; the name must stay the kind."""
        with another_process(pid=1001):
            locking.heartbeat("learn")

        rendered = str(locking.other_writers(exclude="ingest")[0])

        assert rendered.startswith("learn (pid 1001 on ")
        assert rendered.endswith("s ago)")


class TestKeepingFresh:
    def test_the_record_survives_work_longer_than_the_stale_window(self):
        """A single stamp before minutes of work ages out mid-rebuild.

        The window is compressed here — production allows 90s and refreshes
        every 30 — but the shape is the real one: the work outlasts the window
        several times over and the writer must stay visible throughout.
        """
        window = 0.4
        with locking.writing("learn", refresh_secs=window / 4):
            time.sleep(window * 3)
            found = locking.other_writers(exclude="ingest", max_age=window)

        assert [w.name for w in found] == ["learn"]

    def test_the_record_is_released_when_the_work_raises(self):
        with pytest.raises(RuntimeError), locking.writing("learn"):
            raise RuntimeError("boom")

        assert not record_of("learn").exists()

    def test_no_refresher_thread_outlives_the_work(self):
        before = threading.active_count()
        with locking.writing("learn", refresh_secs=0.01):
            time.sleep(0.05)
            assert threading.active_count() == before + 1

        assert threading.active_count() == before

    def test_a_failing_refresh_does_not_reach_the_caller(self):
        """Refusing to be the cause of a crash is the point of the module.

        A refresh that cannot write must be as quiet as a missed poll, and must
        not kill the ticker: writing has to resume once the fault clears.
        """
        writers = record_of("learn").parent
        with locking.writing("learn", refresh_secs=0.01):
            writers.mkdir(parents=True, exist_ok=True)
            writers.chmod(0o500)  # every refresh from here on fails
            time.sleep(0.05)
            writers.chmod(0o700)
            stale = record_of("learn").read_text()
            time.sleep(0.05)
            assert record_of("learn").read_text() != stale, "the ticker gave up"


class TestIngestGuard:
    def test_refuses_while_another_writer_is_active(self):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        locking.heartbeat("poller")

        with pytest.raises(SystemExit) as excinfo:
            ingest.build()

        msg = str(excinfo.value)
        assert "another ontime process is writing" in msg
        assert "poller" in msg
        assert "SIGBUS" in msg, "the message must explain why this is not just a lock"
        assert "--force" in msg

    def test_force_overrides_the_guard(self, built_db):
        """The maintenance container shares a volume by design and passes force."""
        built_db.close()
        locking.heartbeat("poller")
        ingest.build(force=True)  # must not raise

    def test_proceeds_once_the_other_writer_stops(self):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        locking.heartbeat("poller")
        with pytest.raises(SystemExit):
            ingest.build()

        locking.release("poller")
        ingest.build()  # the exact recovery the incident needed

    def test_stale_writer_does_not_block(self):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        locking.heartbeat("poller")
        path = record_of("poller")
        pid, host, _ = path.read_text().split()
        path.write_text(f"{pid} {host} {time.time() - 3600:.0f}\n")
        ingest.build()  # a crashed process must not wedge the cache forever

    def test_own_heartbeat_is_released_afterwards(self):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        ingest.build()
        assert not record_of("ingest").exists()

    def test_heartbeat_released_even_when_the_build_fails(self, monkeypatch):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        monkeypatch.setattr(ingest, "_build", lambda: (_ for _ in ()).throw(RuntimeError))
        with pytest.raises(RuntimeError):
            ingest.build()
        assert not record_of("ingest").exists()

    def test_refuses_while_a_second_ingest_is_running(self):
        """The case the guard was blind to, and the one that SIGBUSes.

        Both the host's `python -m ontime.ingest` and the maintenance
        container's rebuild register as "ingest". While that was one shared
        file, each excluded the other's record as its own and neither could
        ever see the other.
        """
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        with another_process():
            locking.heartbeat("ingest")

        with pytest.raises(SystemExit) as excinfo:
            ingest.build()

        assert "ingest (pid 424242" in str(excinfo.value)

    def test_a_finished_build_leaves_another_ingest_registered(self):
        """Releasing a shared name let whoever finished first unregister both.

        A rebuild still running was then invisible to any third writer.
        """
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        with another_process():
            locking.heartbeat("ingest")

        ingest.build(force=True)

        assert [w.name for w in locking.other_writers(exclude="ingest")] == ["ingest"]


class TestPollerAnnouncesItself:
    def test_poll_writes_a_heartbeat(self, built_db, monkeypatch):
        """Without this, ingest cannot see the poller and the guard is useless."""
        from ontime import web

        built_db.close()
        monkeypatch.setattr(web, "build_board", lambda _conn: {"counts": {}})
        monkeypatch.setattr(web, "refresh_timetable", lambda _conn: None)
        monkeypatch.setattr(web, "refresh_segments", lambda _conn: None)

        web.poll_once()

        assert [w.name for w in locking.other_writers(exclude="ingest")] == ["poller"]
