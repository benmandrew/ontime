"""Cut road geometry for the watched routes out of OpenStreetMap.

The map draws each route as a line through its stops, because the BODS General
Transit Feed Specification (GTFS) archive ships no geometry for these operators:
every watched trip carries an empty `shape_id`. A straight hop between stops is
296m at the median, which follows a straight road well and cuts every corner.

OpenStreetMap carries the same routes as `type=route` relations whose member
ways are the actual roads. Measured against the cached stop sequences, the
Stagecoach 192 relation puts every one of its 55 stops a median 16m from the
line — road geometry at metre resolution in place of a 296m chord.

Relations are matched to routes by proximity, never by `ref` alone. Querying
the board's own bounding box for `ref=41` returns three relations, one of them
a First Manchester service around Ashton whose stops lie a median 8,598m from
anything the watched 41 calls at. Coverage rejects it at 0.0% where the two
genuine Go North West relations score 85.5% and 82.1%.

This runs offline and writes a file into the package. Nothing at runtime talks
to Overpass, so the board keeps no dependency on it and no key is needed —
OpenStreetMap's licence is satisfied by the attribution the map already carries
for its tiles.

Run after an ingest, with the timetable cache in place:
    python scripts/build_route_shapes.py
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ontime import config

OUT = ROOT / "ontime" / "static" / "route_shapes.json"

OVERPASS = "https://overpass-api.de/api/interpreter"
# Overpass answers an unidentified client with 406, not with data.
USER_AGENT = "ontime/0.1 (private bus dashboard; github.com/benmandrew)"
TIMEOUT = 300
RETRIES = 4
RETRY_WAIT = 20

# A stop counts as covered when the line passes within this far of it. Real
# relations put 79.5%-98.2% of their route's stops inside 40m; the mismatched
# 41 puts none of them there, so the gate has a wide margin either side.
NEAR_M = 40.0
MIN_COVERAGE = 0.60

# Ways in a relation are ordered but not always joined — the 41 relations have
# holes of up to 8,670m. Bridging one would draw a line straight across
# Manchester, so the polyline is cut instead. The 33 real holes fall either
# side of a wide divide: fifteen at 156m or less, then nothing until 371m, then
# kilometres. 200m therefore closes every missing link and splits at every
# genuine hole, and the widest thing it draws across is one short block.
MAX_BRIDGE_M = 200.0

# Below this a piece is a stub of the way chain rather than a stretch of road —
# the 41 produced one of 19m — and drawing it says nothing.
MIN_PIECE_M = 100.0

# Ramer-Douglas-Peucker tolerance. The raw relations are 12,719 points, which
# is 298KB of JSON for a fetch the page makes on load; 2m leaves 2,556 points
# and 60KB. At the map's maximum zoom of 18 one pixel is 0.36m at this
# latitude, so 2m is the point at which simplification stops being free.
SIMPLIFY_M = 2.0

EARTH_R = 6371000.0


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    p = math.pi / 180
    dlat, dlon = (b[0] - a[0]) * p, (b[1] - a[1]) * p
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(a[0] * p) * math.cos(b[0] * p) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def cached_routes(conn: sqlite3.Connection) -> dict[str, list[tuple[float, float]]]:
    """Every route in the timetable cache, with the stops its trips call at.

    Read from the cache rather than from `config`, because which routes are
    cached depends on the per-stop route limits and on what the operators
    published — the script should ask for exactly what the board will draw.
    """
    out: dict[str, set[str]] = {}
    for route, stop_id in conn.execute(
        "SELECT t.route_name, ts.stop_id FROM trips t "
        "JOIN trip_stops ts ON ts.trip_id = t.trip_id"
    ):
        out.setdefault(route, set()).add(stop_id)
    points = {
        sid: (lat, lon)
        for sid, lat, lon in conn.execute("SELECT stop_id, lat, lon FROM stops")
        if lat is not None and lon is not None
    }
    return {
        route: [points[s] for s in sorted(stops) if s in points]
        for route, stops in sorted(out.items())
    }


def fetch_relations(refs: list[str]) -> list[dict]:
    """Bus route relations carrying one of these numbers, inside the board's box.

    `out geom` returns each member way's nodes inline, which is one request for
    every route rather than one per relation. The whole answer is 1.2MB.
    """
    min_lon, min_lat, max_lon, max_lat = config.BBOX
    pattern = "|".join(sorted(refs))
    query = (
        f"[out:json][timeout:{TIMEOUT}];"
        f'rel[type=route][route=bus][ref~"^({pattern})$"]'
        f"({min_lat},{min_lon},{max_lat},{max_lon});"
        f"out geom;"
    )
    # The public endpoint is shared and sheds load by answering 504, which it
    # did on three of seven requests while this was being written. Retrying is
    # what makes the script runnable rather than lucky.
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.post(
                OVERPASS,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()["elements"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            if attempt == RETRIES:
                raise
            wait = RETRY_WAIT * attempt
            print(f"  overpass attempt {attempt}/{RETRIES} failed ({exc}); {wait}s")
            time.sleep(wait)
    raise AssertionError("unreachable")


def stitch(relation: dict) -> list[list[tuple[float, float]]]:
    """Member ways in relation order, oriented and joined into polylines.

    A way's nodes run in whichever direction it was drawn, which is unrelated
    to the direction of travel, so each way is flipped when its far end sits
    closer to the running tail than its near one. Where the chain breaks by
    more than `MAX_BRIDGE_M` the polyline ends and another begins.

    Only members with an empty role are roads. The Public Transport v2 scheme
    gives stops and platforms their own roles, and those are nodes rather than
    ways in any case.

    A way flipped the wrong way round would show up as a hole the width of that
    way, so the two rules correct each other: the polyline splits rather than
    doubling back on itself.
    """
    ways = [
        [(p["lat"], p["lon"]) for p in m["geometry"]]
        for m in relation.get("members", ())
        if m.get("type") == "way" and not m.get("role") and m.get("geometry")
    ]
    lines: list[list[tuple[float, float]]] = []
    run: list[tuple[float, float]] = []
    for way in ways:
        if not run:
            run = list(way)
            continue
        if haversine(run[-1], way[-1]) < haversine(run[-1], way[0]):
            way = way[::-1]
        gap = haversine(run[-1], way[0])
        if gap > MAX_BRIDGE_M:
            lines.append(run)
            run = list(way)
        else:
            run.extend(way[1:] if gap < 1.0 else way)
    if run:
        lines.append(run)
    return [ln for ln in lines if len(ln) >= 2 and length(ln) >= MIN_PIECE_M]


def length(line: list[tuple[float, float]]) -> float:
    return sum(haversine(line[i], line[i + 1]) for i in range(len(line) - 1))


def score(
    lines: list[list[tuple[float, float]]], stops: list[tuple[float, float]]
) -> tuple[float, float]:
    """Fraction of the route's stops within `NEAR_M` of the line, and the median.

    Distance is measured to the nearest vertex rather than to the nearest point
    on a segment. Relation vertices sit far closer together than `NEAR_M` —
    2,556 of them across 12,719m of the 192 — so the two agree to well inside
    the tolerance, and the cheaper one is enough to tell a route from a
    different route.
    """
    if not stops or not lines:
        return 0.0, math.inf
    vertices = [p for line in lines for p in line]
    dists = sorted(min(haversine(s, v) for v in vertices) for s in stops)
    near = sum(1 for d in dists if d <= NEAR_M)
    return near / len(dists), dists[len(dists) // 2]


def simplify(points: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker, iterative so a long way cannot blow the stack.

    Latitude and longitude are projected to metres about the first point before
    measuring. Over a route a few tens of kilometres long the error in that
    approximation is far below `eps`.

    A span whose ends coincide is measured radially rather than perpendicularly.
    A closed span has no perpendicular — every distance to it comes out zero —
    so the plain form discards the whole of it, which flattened a 41 roundabout
    into a pair of identical points.
    """
    if len(points) < 3:
        return points
    kx = 111320.0 * math.cos(points[0][0] * math.pi / 180)
    xy = [(lon * kx, lat * 110540.0) for lat, lon in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = xy[i]
        bx, by = xy[j]
        dx, dy = bx - ax, by - ay
        span = math.hypot(dx, dy)
        worst, at = -1.0, -1
        for k in range(i + 1, j):
            px, py = xy[k]
            if span < 1e-9:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * (px - ax) - dx * (py - ay)) / span
            if d > worst:
                worst, at = d, k
        if worst > eps:
            keep[at] = True
            stack.append((i, at))
            stack.append((at, j))
    return [p for p, k in zip(points, keep, strict=True) if k]


def round_points(lines: list[list[tuple[float, float]]]) -> list[list[list[float]]]:
    """Six decimal places is 0.11m of latitude — below the tolerance, above the
    resolution anyone will ever see, and a third off the file."""
    return [[[round(lat, 6), round(lon, 6)] for lat, lon in line] for line in lines]


def build() -> dict:
    if not config.DB_PATH.exists():
        raise SystemExit(f"Need {config.DB_PATH}. Run: python -m ontime.ingest")
    with sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True) as conn:
        routes = cached_routes(conn)
    if not routes:
        raise SystemExit("Timetable cache holds no trips; nothing to match against.")
    print(f"{len(routes)} cached routes: {', '.join(routes)}")

    elements = fetch_relations(list(routes))
    print(f"{len(elements)} candidate relations from Overpass")

    by_route: dict[str, list[dict]] = {}
    for element in sorted(elements, key=lambda e: (e["tags"].get("ref", ""), e["id"])):
        tags = element.get("tags", {})
        ref = tags.get("ref")
        stops = routes.get(ref or "")
        if not stops:
            continue
        lines = stitch(element)
        coverage, median = score(lines, stops)
        verdict = "keep" if coverage >= MIN_COVERAGE else "DROP"
        print(
            f"  {verdict:>4} {ref:>4} rel/{element['id']:<9} "
            f"coverage={coverage:6.1%} median={median:7.0f}m "
            f"pieces={len(lines):>2} {tags.get('operator', '?')[:24]}"
        )
        if coverage < MIN_COVERAGE:
            continue
        simplified = [simplify(line, SIMPLIFY_M) for line in lines]
        by_route.setdefault(ref, []).append(
            {
                "relation": element["id"],
                "operator": tags.get("operator"),
                "from": tags.get("from"),
                "to": tags.get("to"),
                "coverage": round(coverage, 4),
                "median_m": round(median, 1),
                "lines": round_points(simplified),
            }
        )

    missing = sorted(set(routes) - set(by_route))
    if missing:
        print(f"no usable relation for {', '.join(missing)} — these keep the stop line")
    total = sum(len(ln) for r in by_route.values() for v in r for ln in v["lines"])
    print(f"{len(by_route)} routes, {total} points after simplification")
    return {
        "source": "OpenStreetMap contributors, ODbL 1.0, via the Overpass API",
        "bbox": list(config.BBOX),
        "near_m": NEAR_M,
        "min_coverage": MIN_COVERAGE,
        "simplify_m": SIMPLIFY_M,
        "routes": by_route,
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f}KB)")
