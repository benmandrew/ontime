"""Estimate arrival times at the watched stops.

Prediction walks the remaining stop sequence of the matched trip and adds up
how long each segment takes. Learned medians are used where enough history
exists; the timetabled gap is the fallback everywhere else. Because the sum
starts from where the vehicle actually is, lateness is handled implicitly —
a bus twenty minutes behind is simply twenty minutes further back in the
sequence than it should be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import config
from .matching import LONDON, Trip, haversine, nearest_on_trip
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


def _service_midnight(day) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=LONDON).timestamp()


def sched_timestamp(trip: Trip, arr_secs: int | None) -> float | None:
    if arr_secs is None:
        return None
    return _service_midnight(trip.service_date) + arr_secs


def predict(
    vehicle: Vehicle,
    trip: Trip,
    target_stop_id: str,
    segments: dict,
    now: float | None = None,
    schedule_confident: bool = True,
) -> Prediction | None:
    """Predict this vehicle's arrival at one stop, or None if it has passed.

    `schedule_confident` says whether the matcher pinned down which run this
    is. When it did not, the arrival estimate still holds — every trip on a
    route walks the same stops — but the scheduled time would come from an
    arbitrary run, so no delay is reported rather than a fictional one.
    """
    now = now or datetime.now(UTC).timestamp()

    target_idx = next((i for i, s in enumerate(trip.stops) if s[1] == target_stop_id), None)
    if target_idx is None:
        return None

    pos_idx, pos_dist, _seq = nearest_on_trip(trip, vehicle.lat, vehicle.lon)
    if pos_idx > target_idx:
        return None

    hour = datetime.fromtimestamp(now, LONDON).hour
    weekend = int(datetime.fromtimestamp(now, LONDON).weekday() >= 5)

    total = 0.0
    learned_n = total_n = 0
    for i in range(pos_idx, target_idx):
        _, from_id, from_arr, _, _ = trip.stops[i]
        _, to_id, to_arr, _, _ = trip.stops[i + 1]
        total_n += 1
        key = (trip.route_name, from_id, to_id, hour, weekend)
        hit = segments.get(key)
        if hit:
            total += hit[0]
            learned_n += 1
        elif from_arr is not None and to_arr is not None and to_arr > from_arr:
            total += to_arr - from_arr
        else:
            total += DEFAULT_SEGMENT_SECS

    # The vehicle sits somewhere between pos_idx and the next stop; remove the
    # portion of that first segment it has already covered.
    if pos_idx < target_idx:
        _, _, _, alat, alon = trip.stops[pos_idx]
        _, _, _, blat, blon = trip.stops[pos_idx + 1]
        leg = haversine(alat, alon, blat, blon)
        if leg > 1:
            frac = min(max(pos_dist / leg, 0.0), 1.0)
            first_key = (
                trip.route_name,
                trip.stops[pos_idx][1],
                trip.stops[pos_idx + 1][1],
                hour,
                weekend,
            )
            first_secs = segments.get(first_key, (DEFAULT_SEGMENT_SECS,))[0]
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
        for _seq, stop_id, arr, _lat, _lon in trip.stops:
            if stop_id not in config.STOP_IDS:
                continue
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
