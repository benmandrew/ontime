"""Estimate arrival times at the watched stops.

Prediction walks the remaining stop sequence of the matched trip and adds up
how long each segment takes. Learned medians are used where enough history
exists; the timetabled gap is the fallback everywhere else. Because the sum
starts from where the vehicle actually is, lateness is handled implicitly —
a bus twenty minutes behind is simply twenty minutes further back in the
sequence than it should be.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .matching import LONDON, Trip, nearest_on_trip, service_midnight
from .siri import Vehicle

DEFAULT_SEGMENT_SECS = 60.0

# Beyond this, a reported delay says more about the match than the bus.
MAX_PLAUSIBLE_DELAY_SECS = 90 * 60


@dataclass
class Prediction:
    stop_id: str
    route_name: str
    headsign: str
    vehicle_ref: str | None
    eta_ts: float | None
    sched_ts: float | None
    minutes: float | None
    source: str  # "learned", "timetable", or "scheduled"
    learned_coverage: float = 0.0  # fraction of remaining segments from history
    delay_secs: float | None = None
    lat: float | None = None
    lon: float | None = None
    bearing: float | None = None
    stops_away: int | None = None
    extra: dict = field(default_factory=dict)


def sched_timestamp(trip: Trip, arr_secs: int | None) -> float | None:
    """Wall-clock instant of a timetabled time on this trip's service day.

    The anchor is the GTFS service midnight — noon minus twelve hours — and not
    local midnight; see `matching.service_midnight` for what the difference
    costs on the two clock-change Sundays.
    """
    if arr_secs is None:
        return None
    return service_midnight(trip.service_date) + arr_secs


def _fraction_along(
    alat: float, alon: float, blat: float, blon: float, lat: float, lon: float
) -> float:
    """How far along the a-to-b leg a vehicle has actually got, 0 to 1.

    `nearest_on_trip` returns an undirected straight-line distance to the
    closest stop, which says nothing about which side of it the vehicle is on.
    Treating that distance as progress charged a bus still *approaching* stop a
    as though it were that far past it, and into the wrong segment: walking a
    vehicle up a 197m leg gave 3.68, 3.84 then 4.00 minutes, an ETA rising as
    the bus closed on its target. Projecting onto the leg gives the sign the
    distance lacks, so a bus short of stop a is credited nothing. Plane
    geometry is exact enough over the few hundred metres a leg spans.
    """
    kx = math.cos(math.radians(alat))
    dx, dy = (blon - alon) * kx, blat - alat
    d2 = dx * dx + dy * dy
    if d2 <= 0:
        return 0.0
    vx, vy = (lon - alon) * kx, lat - alat
    return min(max((vx * dx + vy * dy) / d2, 0.0), 1.0)


def predict(
    vehicle: Vehicle,
    trip: Trip,
    target_stop_id: str,
    segments: dict,
    now: float | None = None,
    schedule_confident: bool = True,
    pos_idx: int | None = None,
) -> Prediction | None:
    """Predict this vehicle's arrival at one stop, or None if it has passed.

    `schedule_confident` says whether the matcher pinned down which run this
    is. When it did not, the arrival estimate still holds — every trip on a
    route walks the same stops — but the scheduled time would come from an
    arbitrary run, so no delay is reported rather than a fictional one.

    `pos_idx` is the vehicle's closest stop on this trip, which `match` has
    already had to work out; passing it through skips re-measuring the whole
    stop sequence once per watched stop, four times over per vehicle, for an
    answer that cannot differ. Callers without a `Match` in hand — the tests
    and the benchmark — leave it out and it is measured here as before.
    """
    now = now or datetime.now(UTC).timestamp()

    # Deliberately the first occurrence, not `trip.index_of`, which is built as
    # a dict comprehension and so holds the last. Nothing in the cache calls at
    # a watched stop twice today, so the two agree — but a loop route in some
    # later archive would make them disagree, and the ETA would move without
    # anything saying so. The scan is a few dozen comparisons.
    target_idx = next((i for i, s in enumerate(trip.stops) if s[1] == target_stop_id), None)
    if target_idx is None:
        return None

    if pos_idx is None:
        pos_idx = nearest_on_trip(trip, vehicle.lat, vehicle.lon)[0]
    if pos_idx > target_idx:
        return None

    hour = datetime.fromtimestamp(now, LONDON).hour
    weekend = int(datetime.fromtimestamp(now, LONDON).weekday() >= 5)

    total = 0.0
    first_secs = 0.0
    learned_n = total_n = 0
    for i in range(pos_idx, target_idx):
        _, from_id, from_arr, _, _ = trip.stops[i]
        _, to_id, to_arr, _, _ = trip.stops[i + 1]
        total_n += 1
        key = (trip.route_name, from_id, to_id, hour, weekend)
        hit = segments.get(key)
        if hit:
            secs = hit[0]
            learned_n += 1
        elif from_arr is not None and to_arr is not None and to_arr > from_arr:
            secs = float(to_arr - from_arr)
        else:
            secs = DEFAULT_SEGMENT_SECS
        total += secs
        if i == pos_idx:
            # Kept so the credit below subtracts the very seconds this segment
            # contributed. Recomputing it fell back to DEFAULT_SEGMENT_SECS and
            # credited 24s of a 120s timetabled segment where 48s was due.
            first_secs = secs

    # The vehicle is somewhere on the leg out of pos_idx, or has yet to reach
    # it; remove only the part of that first segment it has genuinely covered.
    if pos_idx < target_idx:
        _, _, _, alat, alon = trip.stops[pos_idx]
        _, _, _, blat, blon = trip.stops[pos_idx + 1]
        frac = _fraction_along(alat, alon, blat, blon, vehicle.lat, vehicle.lon)
        total -= frac * first_secs

    total = max(total, 0.0)
    eta_ts = now + total
    sched_ts = sched_timestamp(trip, trip.stops[target_idx][2])

    delay = (eta_ts - sched_ts) if (sched_ts and schedule_confident) else None
    # A delay past this is not a late bus, it is a mismatched trip. Report the
    # arrival, which is derived from position and holds regardless, and drop
    # the schedule comparison that does not.
    if delay is not None and abs(delay) > MAX_PLAUSIBLE_DELAY_SECS:
        delay = None

    return Prediction(
        stop_id=target_stop_id,
        route_name=trip.route_name,
        headsign=trip.headsign or vehicle.dest_name,
        vehicle_ref=vehicle.vehicle_ref,
        eta_ts=eta_ts,
        sched_ts=sched_ts,
        minutes=total / 60,
        source="learned" if learned_n and learned_n >= total_n / 2 else "timetable",
        learned_coverage=(learned_n / total_n) if total_n else 1.0,
        delay_secs=delay,
        lat=vehicle.lat,
        lon=vehicle.lon,
        bearing=vehicle.bearing,
        stops_away=target_idx - pos_idx,
    )


def scheduled_only(
    trips: list[Trip],
    matched_trip_ids: set[str],
    now: float,
    horizon: float,
) -> list[Prediction]:
    """Timetable rows for trips with no live vehicle, so the board stays full."""
    out: list[Prediction] = []
    for trip in trips:
        if trip.trip_id in matched_trip_ids:
            continue
        # `trip.target_calls`, not a filter over `trip.stops`: only about one
        # call in forty-five is at a watched stop, so scanning the full sequence
        # walked 48,917 (trip, stop) pairs every poll to reach 1,094 of them —
        # and that is one service day of the four-stop cache, where the board
        # loads two.
        for stop_id, arr in trip.target_calls:
            ts = sched_timestamp(trip, arr)
            if ts is None or not (now - 60 <= ts <= now + horizon):
                continue
            out.append(
                Prediction(
                    stop_id=stop_id,
                    route_name=trip.route_name,
                    headsign=trip.headsign,
                    vehicle_ref=None,
                    eta_ts=ts,
                    sched_ts=ts,
                    minutes=(ts - now) / 60,
                    source="scheduled",
                )
            )
    return out
