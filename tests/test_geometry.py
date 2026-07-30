"""The map's road geometry: the file that ships, and what happens without it."""

from __future__ import annotations

import json
from datetime import date

import pytest

from ontime import config, geometry, web
from ontime.matching import Trip


@pytest.fixture(autouse=True)
def _fresh_cache():
    """`route_lines` reads once and keeps the result, so tests must not inherit it."""
    geometry._cache = None
    yield
    geometry._cache = None


def trip(route: str, points: list[tuple[float, float]]) -> Trip:
    return Trip(
        trip_id=f"t-{route}-{len(points)}",
        route_name=route,
        headsign="",
        origin_stop_id="A",
        dest_stop_id="B",
        first_dep=0,
        service_date=date(2026, 7, 30),
        stops=[(i, f"s{i}", None, lat, lon) for i, (lat, lon) in enumerate(points)],
    )


class TestTheShippedFile:
    """`scripts/build_route_shapes.py` writes this; it is checked in, not fetched."""

    def test_it_parses_and_every_point_is_inside_the_board_s_box(self):
        doc = json.loads(geometry.SHAPES.read_text(encoding="utf-8"))
        min_lon, min_lat, max_lon, max_lat = config.BBOX
        # OpenStreetMap relations run the whole service, not just the watched
        # part of it, so a line may leave the box the relation was found in.
        # A degree of slack still catches a coordinate pair written backwards.
        for route, relations in doc["routes"].items():
            assert relations, f"{route} is present but carries no relation"
            for relation in relations:
                for line in relation["lines"]:
                    assert len(line) >= 2, f"{route}: a line needs two points"
                    for lat, lon in line:
                        assert min_lat - 1 < lat < max_lat + 1, f"{route}: lat {lat}"
                        assert min_lon - 1 < lon < max_lon + 1, f"{route}: lon {lon}"

    def test_every_kept_relation_covers_the_route_it_is_filed_under(self):
        """The gate that rejects a same-numbered service elsewhere in the city.

        Querying `ref=41` over the board's box returns a First Manchester
        service around Ashton alongside the two real ones. It is dropped at
        0.0% coverage, so nothing this weak may appear in the file.
        """
        doc = json.loads(geometry.SHAPES.read_text(encoding="utf-8"))
        for route, relations in doc["routes"].items():
            for relation in relations:
                assert relation["coverage"] >= doc["min_coverage"], (
                    f"{route} rel/{relation['relation']} is below the gate"
                )

    def test_no_polyline_has_collapsed_to_a_point(self):
        """The simplifier's degenerate case, caught in the artefact it damages.

        A piece that closes on itself — a roundabout taken the whole way round
        — has no perpendicular, so measuring one discards every interior point
        and leaves two identical ends. One 41 piece went that way. The shortest
        surviving piece is 328m.
        """
        doc = json.loads(geometry.SHAPES.read_text(encoding="utf-8"))
        for route, relations in doc["routes"].items():
            for relation in relations:
                for line in relation["lines"]:
                    assert len(set(map(tuple, line))) > 1, (
                        f"{route} rel/{relation['relation']} has a zero-length line"
                    )

    def test_it_credits_openstreetmap(self):
        doc = json.loads(geometry.SHAPES.read_text(encoding="utf-8"))
        assert "OpenStreetMap" in doc["source"], "ODbL requires attribution"

    def test_the_loader_reads_it(self):
        lines = geometry.route_lines()
        assert lines, "the shipped file should yield geometry"
        assert all(len(ln) >= 2 for v in lines.values() for ln in v)


class TestAMissingOrBrokenFile:
    """Geometry is decoration. Losing it must cost the map its lines and no more."""

    def test_an_absent_file_yields_nothing_rather_than_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(geometry, "SHAPES", tmp_path / "gone.json")
        assert geometry.route_lines() == {}

    def test_unparseable_json_yields_nothing(self, tmp_path, monkeypatch):
        path = tmp_path / "route_shapes.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(geometry, "SHAPES", path)
        assert geometry.route_lines() == {}

    @pytest.mark.parametrize(
        "doc",
        [
            {},  # no 'routes' key at all
            {"routes": []},  # 'routes' is not an object
            {"routes": {"192": [{"lines": [[[91.0, -2.2], [53.4, -2.2]]]}]}},  # off-globe
            {"routes": {"192": [{"lines": [[[53.4, -2.2]]]}]}},  # one point is not a line
            {"routes": {"192": [{"lines": [[["53.4", "-2.2"], [53.4, -2.2]]]}]}},  # strings
            {"routes": {"192": [{"lines": [[[True, False], [53.4, -2.2]]]}]}},  # bools
        ],
    )
    def test_malformed_geometry_is_dropped_not_served(self, doc, tmp_path, monkeypatch):
        """A NaN reaching Leaflet blanks the map with nothing in the log."""
        path = tmp_path / "route_shapes.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        monkeypatch.setattr(geometry, "SHAPES", path)
        assert geometry.route_lines() == {}

    def test_one_bad_polyline_does_not_take_its_neighbours_with_it(
        self, tmp_path, monkeypatch
    ):
        good = [[53.40, -2.20], [53.41, -2.21]]
        path = tmp_path / "route_shapes.json"
        path.write_text(
            json.dumps({"routes": {"192": [{"lines": [good, [[0]], good]}]}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(geometry, "SHAPES", path)
        assert geometry.route_lines() == {"192": [good, good]}


class TestRouteLines:
    """`web._route_lines` chooses per route, so the two sources coexist."""

    def test_published_geometry_replaces_the_stop_chords(self, monkeypatch):
        road = [[53.40, -2.20], [53.405, -2.205], [53.41, -2.21]]
        monkeypatch.setattr(geometry, "route_lines", lambda: {"192": [road]})
        lines = web._route_lines([trip("192", [(53.40, -2.20), (53.41, -2.21)])])
        assert lines == [{"route": "192", "points": road}]

    def test_a_route_without_geometry_keeps_its_longest_stop_sequence(self, monkeypatch):
        """The 751 and the 797 have no relation inside the board's box."""
        monkeypatch.setattr(geometry, "route_lines", lambda: {})
        short = trip("751", [(53.40, -2.20), (53.41, -2.21)])
        long = trip("751", [(53.40, -2.20), (53.405, -2.205), (53.41, -2.21)])
        lines = web._route_lines([short, long])
        assert lines == [
            {"route": "751", "points": [[53.40, -2.20], [53.405, -2.205], [53.41, -2.21]]}
        ]

    def test_the_two_sources_coexist_across_routes(self, monkeypatch):
        road = [[53.40, -2.20], [53.41, -2.21]]
        monkeypatch.setattr(geometry, "route_lines", lambda: {"192": [road]})
        lines = web._route_lines(
            [
                trip("192", [(53.30, -2.30), (53.31, -2.31)]),
                trip("751", [(53.40, -2.20), (53.41, -2.21)]),
            ]
        )
        by_route = {ln["route"]: ln["points"] for ln in lines}
        assert by_route["192"] == road, "192 must not fall back to its chords"
        assert by_route["751"] == [[53.40, -2.20], [53.41, -2.21]]

    def test_both_directions_of_a_route_are_drawn(self, monkeypatch):
        """One relation per direction is how the 41's two workings stop sharing a line."""
        out, back = [[53.40, -2.20], [53.41, -2.21]], [[53.41, -2.22], [53.40, -2.23]]
        monkeypatch.setattr(geometry, "route_lines", lambda: {"41": [out, back]})
        lines = web._route_lines([trip("41", [(53.40, -2.20), (53.41, -2.21)])])
        assert [ln["points"] for ln in lines] == [out, back]

    def test_no_trips_means_no_lines(self, monkeypatch):
        monkeypatch.setattr(geometry, "route_lines", lambda: {"192": [[[53.4, -2.2]] * 2]})
        assert web._route_lines([]) == [], "geometry alone must not draw a route"
