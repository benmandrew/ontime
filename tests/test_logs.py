"""Log formatting and the redaction filter."""

from __future__ import annotations

import logging
import re

import pytest

from ontime import logs

ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture
def emit(capsys):
    """Emit through a freshly configured handler and return what was written."""

    def _emit(msg, *args, level=logging.INFO, name="ontime.test"):
        root = logging.getLogger()
        saved = root.handlers[:]
        root.handlers = []
        logs.setup(level="DEBUG")
        try:
            logging.getLogger(name).log(level, msg, *args)
        finally:
            root.handlers = saved
        return capsys.readouterr().out

    return _emit


class TestTimestamps:
    def test_every_line_is_timestamped(self, emit):
        out = emit("hello").strip()
        assert out, "expected output"
        stamp = out.split()[0]
        assert ISO_UTC.match(stamp), f"not ISO-8601 UTC: {stamp!r}"

    def test_timestamp_is_utc_not_local(self, emit):
        assert emit("x").strip().split()[0].endswith("Z")

    def test_level_and_logger_name_present(self, emit):
        out = emit("something failed", level=logging.ERROR, name="ontime.web")
        assert "ERROR" in out
        assert "ontime.web" in out
        assert "something failed" in out

    def test_lazy_args_are_interpolated(self, emit):
        assert "feed=7 matched=3" in emit("feed=%d matched=%d", 7, 3)


class TestRedactFilter:
    def test_key_is_stripped_from_the_message(self, emit, api_key):
        out = emit(f"GET /datafeed/?api_key={api_key}")
        assert api_key not in out
        assert "<redacted>" in out

    def test_key_is_stripped_from_positional_args(self, emit, api_key):
        out = emit("fetching %s", f"https://x/?api_key={api_key}")
        assert api_key not in out
        assert "<redacted>" in out

    def test_key_is_stripped_from_dict_args(self, emit, api_key):
        out = emit("fetching %(url)s", {"url": f"https://x/?api_key={api_key}"})
        assert api_key not in out

    def test_non_string_args_pass_through(self, emit, api_key):
        assert "count=42" in emit("count=%d", 42)

    def test_no_key_configured_is_harmless(self, emit, monkeypatch):
        monkeypatch.setenv("BODS_API_KEY", "")
        assert "plain message" in emit("plain message")


class TestSetup:
    def test_is_idempotent(self):
        root = logging.getLogger()
        saved = root.handlers[:]
        root.handlers = []
        try:
            logs.setup()
            logs.setup()
            logs.setup()
            ours = [h for h in root.handlers if getattr(h, "_ontime", False)]
            assert len(ours) == 1, "repeated setup must not duplicate handlers"
        finally:
            root.handlers = saved

    def test_uvicorn_loggers_are_routed_through_ours(self):
        root = logging.getLogger()
        saved = root.handlers[:]
        root.handlers = []
        try:
            logging.getLogger("uvicorn.error").addHandler(logging.StreamHandler())
            logs.setup()
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
                lg = logging.getLogger(name)
                assert lg.handlers == []
                assert lg.propagate is True
        finally:
            root.handlers = saved
