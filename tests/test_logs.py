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

    def _emit(msg, *args, level=logging.INFO, name="ontime.test", exc_info=False):
        root = logging.getLogger()
        saved = root.handlers[:]
        root.handlers = []
        logs.setup(level="DEBUG")
        try:
            logging.getLogger(name).log(level, msg, *args, exc_info=exc_info)
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

    def test_key_is_stripped_from_a_traceback(self, emit, api_key):
        """`log.exception` renders the traceback downstream of every filter.

        requests puts the full URL, query string included, in the exception it
        raises, so the frame that logs it emitted the key verbatim: the filter
        only ever looked at the format string and the args.
        """
        try:
            raise RuntimeError(f"HTTPSConnectionPool: /datafeed/?api_key={api_key}")
        except RuntimeError:
            out = emit("poll failed", level=logging.ERROR, exc_info=True)

        assert "Traceback" in out, "the traceback must still be logged"
        assert api_key not in out
        assert "<redacted>" in out

    def test_key_is_stripped_from_a_non_string_message(self, emit, api_key):
        """`log.error(exc)` passes the exception object, not a format string.

        The key is invisible until logging renders it, and the filter skipped
        any message that was not already a str.
        """
        out = emit(
            RuntimeError(f"GET /datafeed/?api_key={api_key}"),
            level=logging.ERROR,
        )
        assert api_key not in out
        assert "<redacted>" in out


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

    def test_handlers_we_did_not_install_are_redacted_too(self, api_key, capsys):
        """A `basicConfig()` handler receives every record in parallel with ours.

        Nothing had put a filter on it, so a key that our own handler printed
        as `<redacted>` went out verbatim beside it — on stderr, and into
        whatever file that handler was pointed at.
        """
        root = logging.getLogger()
        saved = root.handlers[:]
        root.handlers = []
        try:
            logging.basicConfig(force=True)  # someone else's handler, on stderr
            logs.setup(level="DEBUG")
            logging.getLogger("ontime.test").error("GET /?api_key=%s", api_key)
            captured = capsys.readouterr()
        finally:
            root.handlers = saved

        assert "<redacted>" in captured.out, "our own handler must still log it"
        assert api_key not in captured.err, "and so must the one we inherited"

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
