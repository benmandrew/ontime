"""Logging setup: ISO-8601 UTC timestamps, and redaction as a safety net.

Every record passes through a filter that strips the API key. The call sites
already redact explicitly, but a filter on the handler catches anything added
later that forgets to — a credential in a log file is the most likely way this
one escapes, so it is worth defending twice.

Timestamps are UTC with a trailing Z rather than local time. Container logs get
aggregated and read from other timezones, and the feed's own RecordedAtTime is
UTC too, so a mixed-zone log makes staleness arithmetic needlessly confusing.
"""

from __future__ import annotations

import logging
import os
import sys
import time

# levelname is padded to 8 so WARNING and CRITICAL do not shunt the columns.
FORMAT = "%(asctime)s %(levelname)-8s %(name)-16s %(message)s"
DATEFMT = "%Y-%m-%dT%H:%M:%S"


class UtcFormatter(logging.Formatter):
    """ISO-8601 with millisecond precision, always UTC."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        stamp = time.strftime(datefmt or DATEFMT, time.gmtime(record.created))
        return f"{stamp}.{int(record.msecs):03d}Z"


class RedactFilter(logging.Filter):
    """Remove the BODS key from any record before it reaches a handler.

    Every field a handler can end up printing is covered, not just the format
    string: `log.error(exc)` puts the key in a non-str `msg`, and
    `log.exception(...)` puts it in a traceback that no amount of care at the
    call site would reach. A filter that only understood `str` messages was a
    backstop that stopped exactly where the interesting leaks start.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        key = os.getenv("BODS_API_KEY", "").strip()
        if not key:
            return True
        if isinstance(record.msg, str):
            if key in record.msg:
                record.msg = record.msg.replace(key, "<redacted>")
        elif key in str(record.msg):
            # `log.error(RuntimeError(url))` — the key is invisible until the
            # object is rendered, so render it here and keep the string.
            record.msg = str(record.msg).replace(key, "<redacted>")
        if record.exc_info and record.exc_text is None:
            # The traceback is rendered by the formatter, which runs after
            # every filter. Formatter.format reuses `exc_text` when it is
            # already set, so pre-rendering it here is the only way in.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text and key in record.exc_text:
            record.exc_text = record.exc_text.replace(key, "<redacted>")
        if isinstance(record.stack_info, str) and key in record.stack_info:
            record.stack_info = record.stack_info.replace(key, "<redacted>")
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (v.replace(key, "<redacted>") if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    v.replace(key, "<redacted>") if isinstance(v, str) else v
                    for v in record.args
                )
        return True


def _redact_everything_on(root: logging.Logger) -> None:
    """Put the filter on every root handler, ours and anyone else's.

    A handler left by someone else's `basicConfig` — a dependency's, or code
    that ran before this — keeps receiving every record in parallel with ours,
    and would write them out unredacted. Removing it instead would be simpler
    and ruder: it is not ours to throw away, and doing so silently breaks
    anything that installed a handler on purpose.
    """
    for h in root.handlers:
        if not any(isinstance(f, RedactFilter) for f in h.filters):
            h.addFilter(RedactFilter())


def setup(level: str | int | None = None) -> None:
    """Configure root logging. Safe to call more than once."""
    root = logging.getLogger()
    if any(getattr(h, "_ontime", False) for h in root.handlers):
        _redact_everything_on(root)
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(UtcFormatter(FORMAT, DATEFMT))
    handler._ontime = True  # type: ignore[attr-defined]

    root.handlers = [h for h in root.handlers if not getattr(h, "_ontime", False)]
    root.addHandler(handler)
    _redact_everything_on(root)
    root.setLevel(level or os.getenv("ONTIME_LOG_LEVEL", "INFO").upper())

    # uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
