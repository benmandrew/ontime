"""Connection handling for the database the web and maintenance containers share."""

from __future__ import annotations

import sqlite3

import pytest

from ontime import config, db


@pytest.fixture
def delete_mode_db(data_dir):
    """A populated database left in the default `delete` journal mode.

    This is what any file created by something other than `db.connect` looks
    like — a restored backup, a `sqlite3` shell session, an older build.
    """
    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(db.SCHEMA)
    conn.execute("PRAGMA journal_mode=delete")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', '2020-01-01')")
    conn.commit()
    conn.close()
    return config.DB_PATH


class TestBusyTimeout:
    """Five seconds was not enough to outlast a rebuild's write burst."""

    def test_follows_the_configured_value(self, data_dir, monkeypatch):
        monkeypatch.setattr(config, "BUSY_TIMEOUT_MS", 12_345)
        conn = db.connect()
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 12_345
        finally:
            conn.close()

    def test_read_only_handles_get_it_too(self, delete_mode_db, monkeypatch):
        monkeypatch.setattr(config, "BUSY_TIMEOUT_MS", 12_345)
        conn = db.connect(readonly=True)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 12_345
        finally:
            conn.close()

    def test_the_default_is_generous(self):
        assert config.BUSY_TIMEOUT_MS >= 30_000


class TestReadOnlyConnections:
    """Choosing a journal mode is itself a write.

    `PRAGMA journal_mode=WAL` on a read-only handle to a database that is not
    already in WAL returns SQLITE_READONLY. The one read-only caller,
    `maintenance.cache_is_current`, treats any `sqlite3.Error` as "no usable
    cache", so a database in delete mode sent the maintenance loop into a
    rebuild on every tick that could not possibly fix it.
    """

    def test_opening_a_non_wal_database_read_only_succeeds(self, delete_mode_db):
        conn = db.connect(readonly=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
        finally:
            conn.close()
        assert row["value"] == "2020-01-01"

    def test_a_reader_does_not_change_the_journal_mode(self, delete_mode_db):
        """WAL is persistent, so readers inherit it from the writer."""
        conn = db.connect(readonly=True)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode == "delete"


class TestWritableConnections:
    def test_writers_still_select_wal(self, data_dir):
        conn = db.connect()
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            conn.close()

    def test_a_reader_inherits_wal_from_the_writer(self, data_dir):
        writer = db.connect()
        db.init(writer)
        writer.close()

        reader = db.connect(readonly=True)
        try:
            assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            reader.close()
