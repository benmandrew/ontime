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


def match(vehicle: Vehicle, candidates: list[Trip]) -> Trip | None:
    """Best timetabled trip for a live vehicle, or None if nothing fits."""
    pool = [t for t in candidates if t.route_name == vehicle.route_name]
    if not pool:
        return None

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
    for tier in tiers:
        if len(tier) == 1:
            return tier[0]
        if tier:
            # Break ties on how close the vehicle sits to each trip's path.
            return min(tier, key=lambda t: nearest_on_trip(t, vehicle.lat, vehicle.lon)[1])

    # Last resort: the trip whose path passes closest, but only if plausibly close.
    best = min(pool, key=lambda t: nearest_on_trip(t, vehicle.lat, vehicle.lon)[1])
    return best if nearest_on_trip(best, vehicle.lat, vehicle.lon)[1] < 250 else None
