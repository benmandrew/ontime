"""Road geometry for the map's route lines, cut from OpenStreetMap offline.

`scripts/build_route_shapes.py` writes `static/route_shapes.json` and this
reads it. The split is deliberate: matching a relation to a route needs the
timetable cache and a 1.2MB Overpass query, and neither belongs in a process
that answers a board poll every fifteen seconds.

Nothing here raises. A file that is absent, unparseable or the wrong shape
costs the map its road geometry and no more — `web._route_lines` falls back to
the straight line through the stops, which is what the map drew before this
existed. The alternative is a dashboard that will not start because a piece of
decoration is malformed.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import logs

log = logs.get("ontime.geometry")

SHAPES = Path(__file__).parent / "static" / "route_shapes.json"

_cache: dict[str, list[list[list[float]]]] | None = None


def _valid(line: object) -> bool:
    """A polyline of at least two [lat, lon] pairs inside the plausible range.

    The file is generated and checked in, so this is not guarding against an
    attacker. It guards against a half-written file and against a schema that
    drifts, both of which would otherwise reach Leaflet as `NaN` and blank the
    map with nothing in the log.
    """
    if not isinstance(line, list) or len(line) < 2:
        return False
    return all(
        isinstance(p, list)
        and len(p) == 2
        and all(isinstance(c, int | float) and not isinstance(c, bool) for c in p)
        and -90 <= p[0] <= 90
        and -180 <= p[1] <= 180
        for p in line
    )


def _load() -> dict[str, list[list[list[float]]]]:
    if not SHAPES.exists():
        log.warning("no %s; the map falls back to straight lines", SHAPES.name)
        return {}
    try:
        doc = json.loads(SHAPES.read_text(encoding="utf-8"))
        raw = doc["routes"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("cannot read %s (%s); falling back to straight lines", SHAPES.name, exc)
        return {}

    if not isinstance(raw, dict):
        log.warning("%s: 'routes' is not an object; falling back", SHAPES.name)
        return {}

    out: dict[str, list[list[list[float]]]] = {}
    dropped = 0
    for route, relations in raw.items():
        lines = []
        for relation in relations:
            for line in relation.get("lines", ()):
                if _valid(line):
                    lines.append(line)
                else:
                    dropped += 1
        if lines:
            out[str(route)] = lines
    if dropped:
        log.warning("%s: dropped %d malformed polylines", SHAPES.name, dropped)
    log.info(
        "route geometry: %d routes, %d polylines, %d points",
        len(out),
        sum(len(v) for v in out.values()),
        sum(len(ln) for v in out.values() for ln in v),
    )
    return out


def route_lines() -> dict[str, list[list[list[float]]]]:
    """Every route with published geometry, mapped to its polylines.

    Read once. The file ships inside the package and cannot change under a
    running process, so re-reading it on each timetable rebuild would buy
    nothing.
    """
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache
