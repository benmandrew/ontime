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
    # Services to watch here. None means every route that calls at the stop.
    #
    # This is a filter on the *timetable cache*, not on the rendered page: a
    # restricted route's trips are never cached, so they are never matched,
    # never predicted and never learned from. That matters because a corridor
    # stop otherwise drags its whole corridor in — watching all of Oxford Road
    # cost 2,730 cached trips against 1,261 for the 41 alone.
    routes: frozenset[str] | None = None


# The stops this dashboard watches. ATCO codes resolved from NaPTAN area 180.
STOPS: tuple[Stop, ...] = (
    Stop("1800EB01881", "MANADGMT", "Hyde Grove", "Plymouth Grove, westbound"),
    Stop("1800SB13961", "MANGPWTD", "Swinton Grove", "Upper Brook Street, Stop L"),
    Stop("1800EB06241", "MANADTDW", "Cavanagh Close", "Stockport Road, northwest-bound"),
    Stop(
        "1800SB30631",
        "MANGTMGT",
        "University Shopping Centre",
        "Oxford Road, Stop C",
        routes=frozenset({"41"}),
    ),
)

STOP_IDS: frozenset[str] = frozenset(s.atco for s in STOPS)
STOP_BY_ID: dict[str, Stop] = {s.atco: s for s in STOPS}


def stop_serves(atco: str, route_name: str) -> bool:
    """Whether this route's departures belong on this stop's board.

    The single place the `Stop.routes` restriction is interpreted, because it
    has to hold in three of them and they must agree. `ingest` applies it when
    deciding what to cache; `Trip.target_calls` applies it so a trip cached for
    one watched stop does not advertise itself at a restricted one it happens
    to pass; `web.build_board` applies it to live vehicles for the same reason.
    Drop any of the three and a restricted stop leaks the route back — the 191
    runs through University Shopping Centre on its way to Hyde Grove, so its
    trips are cached whatever this says.
    """
    stop = STOP_BY_ID.get(atco)
    if stop is None:
        return False
    return stop.routes is None or route_name in stop.routes


def _env_float_list(name: str, default: str) -> tuple[float, ...]:
    return tuple(float(p) for p in os.getenv(name, default).split(","))


# Bounding box (min_lon, min_lat, max_lon, max_lat) sent to the SIRI-VM feed.
# Wide enough to catch buses roughly 20 minutes upstream of the stops.
BBOX: tuple[float, ...] = _env_float_list("ONTIME_BBOX", "-2.32,53.38,-2.10,53.52")

# Raster basemap for the vehicle map. The browser fetches these tiles directly
# from the tile server, which is the one thing on the page that talks to a third
# party — so the host has to be named in the page's `img-src` policy too.
# `web.tile_origin` derives that from this URL rather than repeating the host,
# because a policy that disagrees with the tile source fails as a blank map with
# nothing in the log to say why.
MAP_TILE_URL = os.getenv(
    "ONTIME_MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
)
# OpenStreetMap's licence requires the credit to be visible on the map itself.
MAP_ATTRIBUTION = os.getenv("ONTIME_MAP_ATTRIBUTION", "© OpenStreetMap contributors")
MAP_MAX_ZOOM = int(os.getenv("ONTIME_MAP_MAX_ZOOM", "18"))

POLL_SECS = int(os.getenv("ONTIME_POLL_SECS", "15"))

# Records older than this are ghosts: the BODS feed retains vehicles for hours
# after their journey ends because operators do not reliably signal completion.
STALE_SECS = int(os.getenv("ONTIME_STALE_SECS", "180"))

# How far ahead to show departures.
HORIZON_SECS = int(os.getenv("ONTIME_HORIZON_SECS", "3600"))

# Raw positions older than this are discarded. Learned segment statistics are
# derived before trimming and are kept permanently.
RETAIN_DAYS = int(os.getenv("ONTIME_RETAIN_DAYS", "21"))

# Derived stop events older than this are discarded. They used to be kept for
# ever, which cost twice over: the table grew without bound, and `learn_segments`
# re-aggregates every row it can see on every hourly pass while holding the write
# lock, so that pass grew without bound too — 105ms at a month of history, 1.3s
# at a year, 4.4s and a 192MB table at three. Ninety days is wide enough to hold
# a full seasonal picture of each segment and to keep the hourly pass in the
# hundreds of milliseconds.
STOP_EVENT_RETAIN_DAYS = int(os.getenv("ONTIME_STOP_EVENT_RETAIN_DAYS", "90"))

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
