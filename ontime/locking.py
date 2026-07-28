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
import re
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import config

# A writer that has not checked in for this long is treated as gone. Comfortably
# longer than a poll interval, so an ordinary cycle never looks stale.
STALE_AFTER_SECS = 90

# How often `writing()` re-stamps the record it holds. A third of the staleness
# window, so two refreshes can be lost — a stalled machine, a full disk that
# clears — before anyone else concludes the writer has gone.
REFRESH_SECS = STALE_AFTER_SECS / 3


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


def _writer_id(kind: str) -> str:
    """The file name this process registers under, unique to the process.

    Naming the file after the kind of work alone was the original bug. A host
    `python -m ontime.ingest` and the maintenance container's rebuild are both
    "ingest", so they wrote and deleted one shared file and were structurally
    invisible to each other — precisely the host-versus-container pairing this
    module exists to catch. Host and pid go in the name so two processes of the
    same kind get two records; they stay in the contents as well, because that
    is what the refusal message reads back.
    """
    host = "unknown"
    with contextlib.suppress(Exception):
        host = re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname()) or "unknown"
    return f"{kind}.{host}.{os.getpid()}"


def heartbeat(kind: str) -> None:
    """Record that this process is writing. Cheap enough to call every poll.

    Advisory, so every failure is swallowed — broadly, not just OSError. A
    heartbeat exists to make someone else's crash less mysterious, and it would
    be a poor trade if it could cause one. The write is to a temporary file and
    renamed, so a reader never sees a half-written record.
    """
    with contextlib.suppress(Exception):
        name = _writer_id(kind)
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{name}.tmp"
        tmp.write_text(f"{os.getpid()} {socket.gethostname()} {time.time():.3f}\n")
        tmp.replace(d / name)


def release(kind: str) -> None:
    with contextlib.suppress(Exception):
        (_dir() / _writer_id(kind)).unlink(missing_ok=True)


@contextlib.contextmanager
def writing(kind: str, refresh_secs: float = REFRESH_SECS) -> Iterator[None]:
    """Hold a heartbeat for the duration of the work, refreshing it as it runs.

    A single stamp written before a rebuild is not enough: the archive scan and
    the learn pass both run for minutes, so the record aged past
    `STALE_AFTER_SECS` while the process was still very much writing, and
    anyone checking saw nothing. A background ticker re-stamps it instead.

    The thread is a daemon and is joined on the way out, so the work raising
    still both stops the ticker and drops the record. Refreshing goes through
    `heartbeat`, which swallows everything, so a failure to write cannot escape
    the thread and take the program with it — the record simply ages, which is
    the same outcome as the process having died, and is safe.
    """
    heartbeat(kind)
    done = threading.Event()

    def _keep_fresh() -> None:
        while not done.wait(refresh_secs):
            heartbeat(kind)

    ticker = threading.Thread(target=_keep_fresh, name=f"heartbeat-{kind}", daemon=True)
    ticker.start()
    try:
        yield
    finally:
        done.set()
        ticker.join()
        release(kind)


def other_writers(exclude: str, max_age: float = STALE_AFTER_SECS) -> list[Writer]:
    """Recent writers, excluding only this process's own `exclude` record.

    `exclude` is a kind, not a name: a second process doing the same kind of
    work is a genuine other writer and is reported.
    """
    d = _dir()
    if not d.is_dir():
        return []
    mine = _writer_id(exclude)
    now = time.time()
    found: list[Writer] = []
    for path in d.iterdir():
        if path.name == mine or path.name.startswith("."):
            continue
        try:
            pid, host, stamp = path.read_text().split()
            age = now - float(stamp)
        except (OSError, ValueError):
            continue
        if age <= max_age:
            # The kind is the readable half of the name; the rest is only there
            # to keep two processes of that kind apart.
            found.append(Writer(path.name.split(".", 1)[0], int(pid), host, age))
    return found
