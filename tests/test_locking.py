"""The writer heartbeat, and the guard that stopped a SIGBUS being possible.

Regression cover for a real incident: a container left running with the data
directory bind-mounted kept polling and writing, and a later `python -m
ontime.ingest` on the host died with a bare `bus error`. Write-ahead logging
memory-maps the `-shm` file unconditionally, and two kernels mapping it across
a VirtioFS bind mount is not survivable in-process. The guard cannot prevent
the mapping, so it prevents the situation and says why.
"""

from __future__ import annotations

import shutil
import time

import pytest

from ontime import config, ingest, locking

from .conftest import MINI_GTFS


@pytest.fixture(autouse=True)
def _isolated(data_dir):
    """Every test in this module gets its own data directory."""
    return data_dir


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
        path = config.DATA_DIR / ".writers" / "poller"
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
        (d / "poller").write_text("not a heartbeat")
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
        path = config.DATA_DIR / ".writers" / "poller"
        pid, host, _ = path.read_text().split()
        path.write_text(f"{pid} {host} {time.time() - 3600:.0f}\n")
        ingest.build()  # a crashed process must not wedge the cache forever

    def test_own_heartbeat_is_released_afterwards(self):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        ingest.build()
        assert not (config.DATA_DIR / ".writers" / "ingest").exists()

    def test_heartbeat_released_even_when_the_build_fails(self, monkeypatch):
        shutil.copy(MINI_GTFS, config.GTFS_ZIP)
        monkeypatch.setattr(ingest, "_build", lambda: (_ for _ in ()).throw(RuntimeError))
        with pytest.raises(RuntimeError):
            ingest.build()
        assert not (config.DATA_DIR / ".writers" / "ingest").exists()


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
