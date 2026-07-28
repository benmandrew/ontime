"""What the learned model actually knows, and how firmly it knows it.

`segment_stats` stores three numbers per segment — a median, a p85 and a
count — and the predictor consults it through a single threshold,
`history.MIN_SAMPLES`. That is enough to run on and far too little to judge,
because a median over five observations is a very different object from a
median over fifty and the stored row does not say which you have.

This module answers the judging question instead. It reads the same sample
vectors the learner was fitted to (`history.segment_samples`), measures each
segment's spread and the uncertainty on its median, and sets the result
against two denominators the stats table has no way to express: how much of
the timetable could be learned at all, and how much of what *is* stored ever
reaches the predictor.

The confidence interval is distribution-free — order statistics on the
samples, no assumption that traversal times are normal, which they are not
(they are bounded below by the road and unbounded above by traffic). The
price is that small samples cannot support a 95% claim at all: see
`CONFIDENT_N`.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from itertools import pairwise
from typing import NamedTuple

from . import config
from .history import MIN_SAMPLES, segment_samples

# The widest interval order statistics can offer is [min, max], which covers
# the true median with probability 1 - 2/2**n. That first reaches 95% at
# n = 6, so no segment with fewer samples has a 95% interval of any width —
# not a wide one, none. MIN_SAMPLES is 5, one short of it.
CONFIDENT_N = 6

# `learn_segments` refuses to store a single-sample segment.
STORE_FLOOR = 2

TARGET_CONF = 0.95


def _quantile(xs: list[float], q: float) -> float:
    """Nearest-rank quantile, matching how `learn_segments` picks its p85.

    Deliberately the same convention rather than a better one: a page that
    reported a p85 the stats table disagreed with would be reporting on a
    different model than the one running.
    """
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def median_ci(
    xs: list[float], target: float = TARGET_CONF
) -> tuple[float, float, float] | None:
    """Tightest distribution-free interval for the median at `target`.

    The interval [x[k], x[n-1-k]] contains the population median with
    probability 1 - 2*P(Bin(n, 0.5) <= k). Coverage falls as k rises, so the
    first k that misses the target ends the search and the last that met it
    is the tightest qualifying interval. Returns None when even k = 0 — the
    full observed range — cannot reach the target.

    `xs` must be sorted ascending.
    """
    n = len(xs)
    best: tuple[float, float, float] | None = None
    tail = 0.0
    for k in range(n // 2 + 1):
        if n - 1 - k < k:
            break
        tail += math.comb(n, k)
        conf = 1.0 - 2.0 * tail / (2**n)
        if conf < target:
            break
        best = (xs[k], xs[n - 1 - k], conf)
    return best


class Scheduled(NamedTuple):
    """What the timetable says, split by the two questions asked of it."""

    # (route, from, to, hour, weekend) -> median scheduled traversal.
    gaps: dict[tuple[str, str, str, int, int], float]
    # (route, from, to) that some trip runs consecutively, gap or no gap.
    adjacent: set[tuple[str, str, str]]


def _scheduled_cells(conn: sqlite3.Connection) -> Scheduled:
    """Every segment the timetable implies, and the gap it allows for each.

    `gaps` is the denominator for coverage and the comparator for the delta
    column: the learner can only ever see a segment a cached trip runs, and
    the median scheduled traversal is what `eta.predict` falls back to when a
    segment is missing or below the gate — so the page compares against the
    number the model is really choosing between, not an abstract one.

    `adjacent` is deliberately not derived from `gaps`. Published times are
    rounded to the minute, so a short hop is routinely timetabled with a
    zero-second allowance; those are excluded from `gaps`, where a gap of zero
    is not a usable comparator, but they are real adjacencies and the
    predictor will happily key on them. Folding the two together reported four
    legitimate segments as unreachable.
    """
    rows = conn.execute(
        "SELECT s.trip_id, s.seq, s.stop_id, s.dep, s.arr, t.route_name, "
        "       c.monday, c.tuesday, c.wednesday, c.thursday, c.friday, "
        "       c.saturday, c.sunday "
        "FROM trip_stops s "
        "JOIN trips t ON t.trip_id = s.trip_id "
        "JOIN calendar c ON c.service_id = t.service_id "
        "ORDER BY s.trip_id, s.seq"
    ).fetchall()

    by_trip: dict[str, list] = {}
    for r in rows:
        by_trip.setdefault(r["trip_id"], []).append(r)

    gaps: dict[tuple[str, str, str, int, int], list[float]] = {}
    adjacent: set[tuple[str, str, str]] = set()
    for trip_rows in by_trip.values():
        for a, b in pairwise(trip_rows):
            if b["seq"] != a["seq"] + 1:
                continue
            adjacent.add((a["route_name"], a["stop_id"], b["stop_id"]))
            start = a["dep"]
            end = b["arr"] if b["arr"] is not None else b["dep"]
            if start is None or end is None or end <= start:
                continue
            hour = (start // 3600) % 24
            for weekend in _weekend_flags(a):
                key = (a["route_name"], a["stop_id"], b["stop_id"], hour, weekend)
                gaps.setdefault(key, []).append(float(end - start))

    return Scheduled({k: sorted(v)[len(v) // 2] for k, v in gaps.items()}, adjacent)


def build(conn: sqlite3.Connection) -> dict:
    """The whole picture: the funnel, the coverage, and every segment."""
    sampled = segment_samples(conn)
    buckets = sampled.buckets
    scheduled = _scheduled_cells(conn)
    gaps, adjacent = scheduled.gaps, scheduled.adjacent
    names = {
        r["stop_id"]: r["name"] for r in conn.execute("SELECT stop_id, name FROM stops")
    }

    segments = []
    for key, xs in buckets.items():
        route, from_id, to_id, hour, weekend = key
        n = len(xs)
        ci = median_ci(xs)
        median = statistics.median(xs)
        sched = gaps.get(key)
        segments.append(
            {
                "route": route,
                "from_id": from_id,
                "from_name": names.get(from_id, from_id),
                "to_id": to_id,
                "to_name": names.get(to_id, to_id),
                "hour": hour,
                "is_weekend": weekend,
                "n": n,
                "min": xs[0],
                "p25": _quantile(xs, 0.25),
                "median": median,
                "p85": _quantile(xs, 0.85),
                "max": xs[-1],
                "ci_lo": ci[0] if ci else None,
                "ci_hi": ci[1] if ci else None,
                "ci_conf": ci[2] if ci else None,
                "sched_secs": sched,
                "delta_secs": (median - sched) if sched is not None else None,
                "stored": n >= STORE_FLOOR,
                "used": n >= MIN_SAMPLES,
                "reachable": (route, from_id, to_id) in adjacent,
            }
        )
    segments.sort(key=lambda s: (-s["n"], s["route"], s["from_name"], s["hour"]))

    hist: dict[int, int] = {}
    for s in segments:
        hist[s["n"]] = hist.get(s["n"], 0) + 1

    # Coverage is measured against the timetable, not against what happened to
    # be observed: the question is how much of the model is still missing.
    covered = {k for k in buckets if k in gaps}
    by_route: dict[str, dict] = {}
    for key in gaps:
        r = by_route.setdefault(
            key[0], {"route": key[0], "scheduled": 0, "seen": 0, "used": 0}
        )
        r["scheduled"] += 1
    for s in segments:
        r = by_route.setdefault(
            s["route"], {"route": s["route"], "scheduled": 0, "seen": 0, "used": 0}
        )
        r["seen"] += 1
        if s["used"]:
            r["used"] += 1

    ev = conn.execute(
        "SELECT COUNT(*) c, COUNT(DISTINCT service_date) d FROM stop_events"
    ).fetchone()

    return {
        "gate": {
            "min_samples": MIN_SAMPLES,
            "store_floor": STORE_FLOOR,
            "confident_n": CONFIDENT_N,
            "target_conf": TARGET_CONF,
            "retain_days": config.STOP_EVENT_RETAIN_DAYS,
        },
        "totals": {
            "observed": len(segments),
            "stored": sum(1 for s in segments if s["stored"]),
            "used": sum(1 for s in segments if s["used"]),
            "significant": sum(1 for s in segments if s["ci_conf"] is not None),
            "unreachable": sum(1 for s in segments if not s["reachable"]),
            "scheduled_cells": len(gaps),
            "covered": len(covered),
            "stop_events": ev["c"],
            "history_days": ev["d"],
            # Consecutive detections two or more stops apart: the stop between
            # them was never seen, so the run cannot say how long that hop took.
            # This measures how continuously vehicles were observed, which is
            # the one input to the model nothing else on the page reports.
            "paired": sampled.paired,
            "skipped": sampled.skipped,
        },
        "histogram": [{"n": n, "segments": c} for n, c in sorted(hist.items())],
        "routes": sorted(by_route.values(), key=lambda r: r["route"]),
        "segments": segments,
    }


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


def _weekend_flags(row: sqlite3.Row) -> tuple[int, ...]:
    """Which weekday/weekend buckets one calendar row contributes to.

    A service pattern running Monday to Saturday fills two buckets, and the
    learner has to observe each separately — so coverage must count them
    separately too.
    """
    flags = []
    if any(row[d] for d in _WEEKDAYS):
        flags.append(0)
    if row["saturday"] or row["sunday"]:
        flags.append(1)
    return tuple(flags)
