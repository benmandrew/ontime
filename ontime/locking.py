"""A heartbeat file per writing process, so a second writer can be detected.

Two processes writing this database concurrently is fine on an ordinary
filesystem — write-ahead logging is built for it, and the compose topology
does exactly that with the web container polling while maintenance rebuilds.

It is not fine when one of them is the macOS host and the other is a container
sharing the directory through a bind mount. In write-ahead logging mode SQLite
memory-maps the `-shm` file unconditionally, and two kernels mapping one file
across a VirtioFS boundary produces a SIGBUS: no exception, no message, just a
dead process. That is unrecoverable in-process, so the only useful defence is
to notice the other writer beforehand and say so.

`flock` is deliberately not used. Its semantics across a bind mount are exactly
what cannot be relied on here. A heartbeat is only file content, which crosses
that boundary intact.
"""

from __future__ import annotations

import contextlib
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

# A writer that has not checked in for this long is treated as gone. Comfortably
# longer than a poll interval, so an ordinary cycle never looks stale.
STALE_AFTER_SECS = 90


@dataclass(frozen=True)
class Writer:
    name: str
    pid: int
    host: str
    age_secs: float

    def __str__(self) -> str:
        return f"{self.name} (pid {self.pid} on {self.host}, {self.age_secs:.0f}s ago)"


def _dir() -> Path:
    return config.DATA_DIR / ".writers"


def heartbeat(name: str) -> None:
    """Record that this process is writing. Cheap enough to call every poll.

    Advisory, so every failure is swallowed — broadly, not just OSError. A
    heartbeat exists to make someone else's crash less mysterious, and it would
    be a poor trade if it could cause one. The write is to a temporary file and
    renamed, so a reader never sees a half-written record.
    """
    with contextlib.suppress(Exception):
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{name}.tmp"
        tmp.write_text(f"{os.getpid()} {socket.gethostname()} {time.time():.0f}\n")
        tmp.replace(d / name)


def release(name: str) -> None:
    with contextlib.suppress(Exception):
        (_dir() / name).unlink(missing_ok=True)


def other_writers(exclude: str, max_age: int = STALE_AFTER_SECS) -> list[Writer]:
    """Writers other than `exclude` that have checked in recently."""
    d = _dir()
    if not d.is_dir():
        return []
    now = time.time()
    found: list[Writer] = []
    for path in d.iterdir():
        if path.name == exclude or path.name.startswith("."):
            continue
        try:
            pid, host, stamp = path.read_text().split()
            age = now - float(stamp)
        except (OSError, ValueError):
            continue
        if age <= max_age:
            found.append(Writer(path.name, int(pid), host, age))
    return found
