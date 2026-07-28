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

# How far from the nearest stop of the matched trip a vehicle may sit and still
# be believed. The longest gap between consecutive stops anywhere in the real
# cache is 1,538m, so a bus stranded mid-segment is at most 769m from a stop;
# add GPS scatter and the offset between the kerbside stop and the road centre
# and the worst legitimate reading is comfortably under a kilometre. Anything
# past that is not on this corridor at all — a bus deadheading to the depot
# still broadcasting its finished journey's refs reads 2,905m out, and its
# RecordedAtTime is fresh so the staleness filter cannot see it.
MAX_OFF_ROUTE_M = 1000.0

# Geometry alone has to be far stricter: with no origin, destination or
# departure time to corroborate it, position is the only evidence there is.
TIER4_MAX_OFF_ROUTE_M = 250.0


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


def service_midnight(day: date) -> float:
    """Unix timestamp the GTFS times of `day` are measured from.

    GTFS defines the service day as noon minus twelve hours, not local
    midnight, and the distinction is the whole point of the definition: on the
    two clock-change Sundays the local day is 23 or 25 hours long, so local
    midnight drifts an hour away from the anchor the published times assume.
    One calendar row in the real cache covers both 2026-10-18 and 2026-10-25
    with 245 trips at identical times, which only works if the anchor moves
    with the clocks.

    Anchoring on local midnight instead put every bus exactly 60 minutes late
    for the whole of clocks-back Sunday and 60 early on clocks-forward Sunday
    — inside MAX_PLAUSIBLE_DELAY_SECS, so nothing downstream caught it.
    """
    return datetime(day.year, day.month, day.day, 12, tzinfo=LONDON).timestamp() - DAY / 2


def service_day_offsets(when: datetime) -> list[tuple[date, int]]:
    """Express an instant as (service_date, seconds-since-service-midnight).

    GTFS lets a trip run past midnight with times such as 25:10:00, so an
    instant at 00:30 belongs to both today's service day and yesterday's.

    Both offsets are measured from their own day's anchor rather than by adding
    a flat 86,400 to the wall clock. Yesterday was 23 or 25 hours long across a
    clock change, and the flat version put 02:30 on clocks-back Sunday at 26:30
    of the previous service day when the timetable calls it 27:30. The cache
    holds 44 calls running out to 27:30, and because these corridors run every
    15 to 30 minutes at night there is always a real trip an hour off to soak
    up the error — it scored dep_delta 0 and matched the wrong run at tier 1,
    turning a withheld delay into a confident 60-minute phantom.
    """
    local = when.astimezone(LONDON)
    today = local.date()
    secs = local.hour * 3600 + local.minute * 60 + local.second
    days = [today]
    if secs < 6 * 3600:
        days.append(today - timedelta(days=1))
    return [(day, round(when.timestamp() - service_midnight(day))) for day in days]


def _nearest(
    trip: Trip, lat: float, lon: float, seen: dict[str, float]
) -> tuple[int, float]:
    """Index and metres to the closest stop, reusing distances already measured.

    Every candidate for a vehicle is a trip on one route, so they walk largely
    the same stops: 890 cached trips share about six hundred distinct ones. A
    stop id has a single position, so its distance can be measured once per
    vehicle and looked up thereafter, which is what keeps the per-candidate
    direction test affordable.
    """
    best_i, best_d = 0, float("inf")
    for i, (_seq, sid, _arr, slat, slon) in enumerate(trip.stops):
        d = seen.get(sid)
        if d is None:
            d = seen[sid] = haversine(lat, lon, slat, slon)
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


def nearest_on_trip(trip: Trip, lat: float, lon: float) -> tuple[int, float, int]:
    """Index, distance in metres, and stop sequence of the closest stop."""
    best_i, best_d = _nearest(trip, lat, lon, {})
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


def _dest_is_downstream(
    trip: Trip, dest_ref: str, lat: float, lon: float, seen: dict[str, float]
) -> bool:
    """Whether `dest_ref` lies ahead of a vehicle at (lat, lon) on this trip.

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

    The position is resolved against *this* trip, not borrowed from another
    candidate. Sharing one anchor was cheaper but depended on list order: a
    trip running the other way reaches `serving` precisely because the termini
    share a code, and its intermediate stops carry the across-the-road codes,
    which appear on no forward trip. Whenever such a trip happened to sort
    first, its anchor was a stop id the forward candidates had never heard of,
    every one of them looked up None, and the correct match was thrown away.
    Cached route variants that skip the anchor stop failed the same way.
    Resolving per candidate costs little once distances are shared — see
    `_nearest`.
    """
    here = _nearest(trip, lat, lon, seen)[0]
    there = trip.index_of.get(dest_ref)
    return there is not None and there > here


def match(vehicle: Vehicle, candidates: list[Trip]) -> Match | None:
    """Best timetabled trip for a live vehicle, or None if nothing fits."""
    pool = [t for t in candidates if t.route_name == vehicle.route_name]
    if not pool:
        return None

    # One haversine per distinct stop id, shared by every candidate below.
    seen: dict[str, float] = {}

    if vehicle.dest_ref:
        serving = [t for t in pool if vehicle.dest_ref in t.index_of]
        if not serving:
            return None
        heading_there = [
            t
            for t in serving
            if _dest_is_downstream(t, vehicle.dest_ref, vehicle.lat, vehicle.lon, seen)
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
            key=lambda t: (dep_delta(t), _nearest(t, vehicle.lat, vehicle.lon, seen)[1]),
        )
        # Matching refs are not proof the vehicle is running the trip. Operators
        # keep broadcasting a finished journey's origin, destination and aimed
        # departure while the bus drives back to the depot, and those refs match
        # perfectly at tier 1 from twelve kilometres off the corridor. This is a
        # sanity bound on the winner, never a way of choosing between candidates
        # — they all share one road, so distance cannot rank them.
        if _nearest(best, vehicle.lat, vehicle.lon, seen)[1] > MAX_OFF_ROUTE_M:
            # Fall through rather than return. A looser tier cannot rescue a bus
            # that is genuinely elsewhere, but the geometric tier below can still
            # place one whose refs point at the wrong variant of its own route,
            # and it will say so by withholding schedule confidence.
            continue
        return Match(best, level, dep_delta(best))

    # Nothing to go on but geometry, which happens when the feed omits
    # OriginAimedDepartureTime. Good enough to place the vehicle on the route
    # and estimate an arrival; not good enough to name the run, so the caller
    # is told not to trust the schedule comparison.
    best = min(pool, key=lambda t: _nearest(t, vehicle.lat, vehicle.lon, seen)[1])
    if _nearest(best, vehicle.lat, vehicle.lon, seen)[1] >= TIER4_MAX_OFF_ROUTE_M:
        return None
    return Match(best, 4, float("inf"))
