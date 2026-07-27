"""Match a live vehicle to the timetabled trip it is running.

BODS publishes a `vehicle_journey_code` in its converted GTFS and a
`DatedVehicleJourneyRef` in SIRI-VM, and the two look like they should be the
same key. They are not. Measured against the services calling at the watched
stops, service 50 matched 0 of 16 live vehicles, and the partial overlaps on
other routes are consistent with integer coincidence. The converter
regenerates journey codes rather than preserving the operator's.

What does survive the conversion is the shape of the journey: which stop it
starts from, which it ends at, and when it was due away. SIRI-VM carries all
three as `OriginRef`, `DestinationRef` and `OriginAimedDepartureTime`, and
those are matched here in tiers, loosening until something fits.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .siri import Vehicle

LONDON = ZoneInfo("Europe/London")
DAY = 86400

# Tolerance on the origin departure time, per tier.
TIER1_TOL = 300
TIER2_TOL = 300
TIER3_TOL = 900


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Trip:
    trip_id: str
    route_name: str
    headsign: str
    origin_stop_id: str
    dest_stop_id: str
    first_dep: int
    service_date: date
    stops: list[tuple[int, str, int | None, float, float]]  # seq, stop_id, arr, lat, lon

    @property
    def index_of(self) -> dict[str, int]:
        """Stop id to position in the sequence, built once per trip."""
        cached = getattr(self, "_index_of", None)
        if cached is None:
            cached = {s[1]: i for i, s in enumerate(self.stops)}
            object.__setattr__(self, "_index_of", cached)
        return cached

    @property
    def stop_ids(self) -> Iterable[str]:
        return self.index_of.keys()


def _runs_on(conn: sqlite3.Connection, service_id: str, day: date) -> bool:
    ymd = day.strftime("%Y%m%d")
    row = conn.execute(
        "SELECT exception_type FROM calendar_dates WHERE service_id=? AND date=?",
        (service_id, ymd),
    ).fetchone()
    if row:
        return row["exception_type"] == 1
    cal = conn.execute(
        "SELECT * FROM calendar WHERE service_id=?", (service_id,)
    ).fetchone()
    if not cal:
        return False
    if not (cal["start_date"] <= ymd <= cal["end_date"]):
        return False
    weekday = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )[day.weekday()]
    return bool(cal[weekday])


def load_trips(conn: sqlite3.Connection, days: tuple[date, ...]) -> list[Trip]:
    """Every watched trip running on the given service days, with stop geometry."""
    stops_by_trip: dict[str, list[tuple[int, str, int | None, float, float]]] = {}
    q = (
        "SELECT ts.trip_id, ts.seq, ts.stop_id, ts.arr, s.lat, s.lon "
        "FROM trip_stops ts JOIN stops s ON s.stop_id = ts.stop_id ORDER BY ts.trip_id, ts.seq"
    )
    for r in conn.execute(q):
        stops_by_trip.setdefault(r["trip_id"], []).append(
            (r["seq"], r["stop_id"], r["arr"], r["lat"], r["lon"])
        )

    runs_cache: dict[tuple[str, date], bool] = {}
    out: list[Trip] = []
    for r in conn.execute("SELECT * FROM trips"):
        for day in days:
            key = (r["service_id"], day)
            if key not in runs_cache:
                runs_cache[key] = _runs_on(conn, r["service_id"], day)
            if not runs_cache[key]:
                continue
            seq = stops_by_trip.get(r["trip_id"])
            if not seq:
                continue
            out.append(
                Trip(
                    trip_id=r["trip_id"],
                    route_name=r["route_name"],
                    headsign=r["headsign"] or "",
                    origin_stop_id=r["origin_stop_id"] or "",
                    dest_stop_id=r["dest_stop_id"] or "",
                    first_dep=r["first_dep"] if r["first_dep"] is not None else -1,
                    service_date=day,
                    stops=seq,
                )
            )
    return out


def service_day_offsets(when: datetime) -> list[tuple[date, int]]:
    """Express an instant as (service_date, seconds-since-service-midnight).

    GTFS lets a trip run past midnight with times such as 25:10:00, so an
    instant at 00:30 belongs to both today's service day and yesterday's.
    """
    local = when.astimezone(LONDON)
    today = local.date()
    secs = local.hour * 3600 + local.minute * 60 + local.second
    out = [(today, secs)]
    if secs < 6 * 3600:
        out.append((today - timedelta(days=1), secs + DAY))
    return out


def nearest_on_trip(trip: Trip, lat: float, lon: float) -> tuple[int, float, int]:
    """Index, distance in metres, and stop sequence of the closest stop."""
    best_i, best_d = 0, float("inf")
    for i, (_seq, _sid, _arr, slat, slon) in enumerate(trip.stops):
        d = haversine(lat, lon, slat, slon)
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d, trip.stops[best_i][0]


@dataclass(frozen=True)
class Match:
    """A matched trip, and how much the schedule side of it can be trusted."""

    trip: Trip
    tier: int
    dep_delta: float  # seconds between aimed and timetabled departure

    @property
    def schedule_confident(self) -> bool:
        """Whether this match pins down *which* run the vehicle is on.

        A tier-4 match knows only that the vehicle is somewhere on this route's
        path. That is enough to estimate an arrival, because every trip on a
        route follows the same stops, but not to say whether it is running
        late — the scheduled time would come from an arbitrary run.
        """
        return self.tier <= 3 and self.dep_delta <= TIER3_TOL


def _dest_is_downstream(trip: Trip, dest_ref: str, anchor_stop_id: str) -> bool:
    """Whether `dest_ref` lies ahead of `anchor_stop_id` on this trip.

    This is the direction test. A route's two directions are separate trips,
    and only one of them is cached when a watched stop is served in a single
    direction: all 467 cached trips for the 192 run towards Manchester,
    because MANADTDW is the northwest-bound stop. Ten of thirty-six live 192s
    were heading to Stockport, and a time-only match happily assigned them to
    a northbound run, which is where "362 minutes early" came from.

    Comparing terminus identifiers is not enough on its own, because a terminus
    often shares one ATCO code between directions — Hazel Grove Park and Ride
    does. Requiring the destination to come *after* the vehicle's position
    separates them properly.

    The anchor is resolved once per vehicle rather than per candidate. Trips on
    one route follow one corridor, so the nearest stop is the same whichever of
    them is measured against, and this turns a haversine over every candidate's
    every stop into two dictionary lookups.
    """
    here = trip.index_of.get(anchor_stop_id)
    there = trip.index_of.get(dest_ref)
    return here is not None and there is not None and there > here


def match(vehicle: Vehicle, candidates: list[Trip]) -> Match | None:
    """Best timetabled trip for a live vehicle, or None if nothing fits."""
    pool = [t for t in candidates if t.route_name == vehicle.route_name]
    if not pool:
        return None

    if vehicle.dest_ref:
        serving = [t for t in pool if vehicle.dest_ref in t.index_of]
        if not serving:
            return None
        _pos, _d, _s = nearest_on_trip(serving[0], vehicle.lat, vehicle.lon)
        anchor = serving[0].stops[_pos][1]
        heading_there = [
            t for t in serving if _dest_is_downstream(t, vehicle.dest_ref, anchor)
        ]
        if not heading_there:
            # Going somewhere this timetable does not reach from here: another
            # direction, or a variant that is not cached. Omitting it is right;
            # inventing a trip is what produced nonsense arrivals.
            return None
        pool = heading_there

    dep_offsets: list[tuple[date, int]] = []
    if vehicle.origin_dep is not None:
        dep_offsets = service_day_offsets(vehicle.origin_dep)

    def dep_delta(t: Trip) -> float:
        if not dep_offsets or t.first_dep < 0:
            return float("inf")
        return (
            min(
                abs(t.first_dep - secs)
                for day, secs in dep_offsets
                if day == t.service_date
            )
            if any(day == t.service_date for day, _ in dep_offsets)
            else float("inf")
        )

    tiers = (
        [
            t
            for t in pool
            if t.origin_stop_id == vehicle.origin_ref
            and t.dest_stop_id == vehicle.dest_ref
            and dep_delta(t) <= TIER1_TOL
        ],
        [
            t
            for t in pool
            if t.dest_stop_id == vehicle.dest_ref and dep_delta(t) <= TIER2_TOL
        ],
        [t for t in pool if dep_delta(t) <= TIER3_TOL],
    )
    for level, tier in enumerate(tiers, start=1):
        if not tier:
            continue
        # Break ties on departure time first, position only to settle a draw.
        #
        # Distance alone is worthless here. Every trip on a route follows the
        # same road, so on a frequent corridor like the 192 the whole candidate
        # set sits within metres of the vehicle and the "closest path" is
        # arbitrary. Choosing that way produced delays of 20 to 45 minutes on
        # buses that were running to time.
        best = min(
            tier,
            key=lambda t: (dep_delta(t), nearest_on_trip(t, vehicle.lat, vehicle.lon)[1]),
        )
        return Match(best, level, dep_delta(best))

    # Nothing to go on but geometry, which happens when the feed omits
    # OriginAimedDepartureTime. Good enough to place the vehicle on the route
    # and estimate an arrival; not good enough to name the run, so the caller
    # is told not to trust the schedule comparison.
    best = min(pool, key=lambda t: nearest_on_trip(t, vehicle.lat, vehicle.lon)[1])
    if nearest_on_trip(best, vehicle.lat, vehicle.lon)[1] >= 250:
        return None
    return Match(best, 4, float("inf"))
