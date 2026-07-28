"""Configuration and secret loading.

The BODS API key is read from the environment only. It is never written to
disk by this package, never sent to the browser, and never included in log
output — see `redact()`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

# Overridden to /data in the container so the timetable cache and the learned
# history survive image rebuilds.
DATA_DIR = Path(os.getenv("ONTIME_DATA_DIR", str(ROOT / "data")))
DB_PATH = DATA_DIR / "ontime.sqlite"
GTFS_ZIP = DATA_DIR / "north_west_gtfs.zip"

# BODS endpoints.
SIRI_VM_URL = "https://data.bus-data.dft.gov.uk/api/v1/datafeed/"
GTFS_URL = "https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/north_west/"


@dataclass(frozen=True)
class Stop:
    atco: str
    naptan: str
    name: str
    detail: str


# The stops this dashboard watches. ATCO codes resolved from NaPTAN area 180.
STOPS: tuple[Stop, ...] = (
    Stop("1800EB01881", "MANADGMT", "Hyde Grove", "Plymouth Grove, westbound"),
    Stop("1800SB13961", "MANGPWTD", "Swinton Grove", "Upper Brook Street, Stop L"),
    Stop("1800EB06241", "MANADTDW", "Cavanagh Close", "Stockport Road, northwest-bound"),
)

STOP_IDS: frozenset[str] = frozenset(s.atco for s in STOPS)
STOP_BY_ID: dict[str, Stop] = {s.atco: s for s in STOPS}


def _env_float_list(name: str, default: str) -> tuple[float, ...]:
    return tuple(float(p) for p in os.getenv(name, default).split(","))


# Bounding box (min_lon, min_lat, max_lon, max_lat) sent to the SIRI-VM feed.
# Wide enough to catch buses roughly 20 minutes upstream of the stops.
BBOX: tuple[float, ...] = _env_float_list("ONTIME_BBOX", "-2.32,53.38,-2.10,53.52")

POLL_SECS = int(os.getenv("ONTIME_POLL_SECS", "15"))

# Records older than this are ghosts: the BODS feed retains vehicles for hours
# after their journey ends because operators do not reliably signal completion.
STALE_SECS = int(os.getenv("ONTIME_STALE_SECS", "180"))

# How far ahead to show departures.
HORIZON_SECS = int(os.getenv("ONTIME_HORIZON_SECS", "3600"))

# Raw positions older than this are discarded. Learned segment statistics are
# derived before trimming and are kept permanently.
RETAIN_DAYS = int(os.getenv("ONTIME_RETAIN_DAYS", "21"))

# How long a statement waits for another process to release the write lock.
# Five seconds was not enough: the timetable rebuild and the 15s poll loop both
# write to the same database, and a rebuild's insert burst can outlast a short
# timeout. Waiting is always better than failing a poll outright.
BUSY_TIMEOUT_MS = int(os.getenv("ONTIME_BUSY_TIMEOUT_MS", "30000"))

HOST = os.getenv("ONTIME_HOST", "127.0.0.1")
PORT = int(os.getenv("ONTIME_PORT", "8000"))


def api_key() -> str:
    key = os.getenv("BODS_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "BODS_API_KEY is not set. Copy .env.example to .env and add your key "
            "(register free at https://data.bus-data.dft.gov.uk/account/signup/)."
        )
    return key


def redact(text: str) -> str:
    """Strip the API key from any string before it reaches a log or an error.

    requests puts the full URL — query string included — into exception
    messages, so unredacted logging is the most likely way for the key to leak.
    """
    key = os.getenv("BODS_API_KEY", "").strip()
    return text.replace(key, "<redacted>") if key else text
