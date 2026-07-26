"""Fetch and parse the BODS SIRI-VM vehicle location feed.

The feed carries positions only. There is no MonitoredCall, no OnwardCalls and
no ExpectedArrivalTime element anywhere in it — the standard permits them but
the Greater Manchester publishers do not populate them, so every arrival time
in this project is computed locally.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime

import requests

from . import config


@dataclass(frozen=True)
class Vehicle:
    vehicle_ref: str
    route_name: str
    direction: str
    journey_ref: str
    operator: str
    origin_ref: str
    dest_ref: str
    dest_name: str
    origin_dep: datetime | None
    recorded_at: datetime
    lat: float
    lon: float
    bearing: float | None

    @property
    def age_secs(self) -> float:
        return (datetime.now(UTC) - self.recorded_at).total_seconds()


def _text(node: ET.Element, tag: str) -> str:
    found = node.find(f".//{{*}}{tag}")
    return (found.text or "").strip() if found is not None and found.text else ""


def _time(node: ET.Element, tag: str) -> datetime | None:
    raw = _text(node, tag)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse(xml: bytes) -> list[Vehicle]:
    root = ET.fromstring(xml)
    out: list[Vehicle] = []
    for act in root.iterfind(".//{*}VehicleActivity"):
        recorded = _time(act, "RecordedAtTime")
        loc = act.find(".//{*}VehicleLocation")
        if recorded is None or loc is None:
            continue
        try:
            lat = float(_text(loc, "Latitude"))
            lon = float(_text(loc, "Longitude"))
        except ValueError:
            continue
        bearing_raw = _text(act, "Bearing")
        out.append(
            Vehicle(
                vehicle_ref=_text(act, "VehicleRef"),
                route_name=_text(act, "PublishedLineName") or _text(act, "LineRef"),
                direction=_text(act, "DirectionRef"),
                journey_ref=_text(act, "DatedVehicleJourneyRef"),
                operator=_text(act, "OperatorRef"),
                origin_ref=_text(act, "OriginRef"),
                dest_ref=_text(act, "DestinationRef"),
                dest_name=_text(act, "DestinationName").replace("_", " "),
                origin_dep=_time(act, "OriginAimedDepartureTime"),
                recorded_at=recorded,
                lat=lat,
                lon=lon,
                bearing=float(bearing_raw) if bearing_raw else None,
            )
        )
    return out


def fetch(routes: set[str] | None = None) -> list[Vehicle]:
    """Pull the feed for the configured bounding box.

    Stale records are dropped here. The feed retains vehicles for hours after
    their journey finishes, so without this filter the dashboard shows buses
    that stopped running much earlier in the day.
    """
    params = {
        "api_key": config.api_key(),
        "boundingBox": ",".join(str(v) for v in config.BBOX),
    }
    try:
        resp = requests.get(config.SIRI_VM_URL, params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(config.redact(str(exc))) from None

    vehicles = parse(resp.content)
    fresh = [v for v in vehicles if v.age_secs <= config.STALE_SECS]
    if routes:
        fresh = [v for v in fresh if v.route_name in routes]
    return fresh
