"""Configuration and credential handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from ontime import config

REPO = Path(__file__).resolve().parent.parent


class TestApiKey:
    def test_returns_the_configured_key(self, api_key):
        assert config.api_key() == api_key

    def test_missing_key_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("BODS_API_KEY", "")
        with pytest.raises(SystemExit) as excinfo:
            config.api_key()
        assert "BODS_API_KEY" in str(excinfo.value)

    def test_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv("BODS_API_KEY", "  padded-key  ")
        assert config.api_key() == "padded-key"


class TestRedact:
    def test_removes_the_key(self, api_key):
        text = f"GET /api/v1/datafeed/?api_key={api_key}&boundingBox=1,2,3,4"
        out = config.redact(text)
        assert api_key not in out
        assert "<redacted>" in out

    def test_removes_every_occurrence(self, api_key):
        assert config.redact(f"{api_key} and again {api_key}").count("<redacted>") == 2

    def test_passes_text_through_when_no_key_is_set(self, monkeypatch):
        monkeypatch.setenv("BODS_API_KEY", "")
        assert config.redact("nothing to hide") == "nothing to hide"

    def test_handles_empty_input(self, api_key):
        assert config.redact("") == ""


class TestStops:
    def test_four_stops_are_watched(self):
        assert len(config.STOPS) == 4
        assert {s.naptan for s in config.STOPS} == {
            "MANADGMT",
            "MANGPWTD",
            "MANADTDW",
            "MANGTMGT",
        }

    def test_atco_codes_are_greater_manchester(self):
        """ATCO area 180 is Greater Manchester."""
        assert all(s.atco.startswith("1800") for s in config.STOPS)

    def test_lookup_tables_agree(self):
        assert frozenset(config.STOP_BY_ID) == config.STOP_IDS
        assert len(config.STOP_IDS) == len(config.STOPS)


class TestStopServes:
    """`Stop.routes` restricts a stop to named services; None watches all."""

    def test_an_unrestricted_stop_serves_everything(self):
        stop = next(s for s in config.STOPS if s.routes is None)
        assert config.stop_serves(stop.atco, "192")
        assert config.stop_serves(stop.atco, "anything at all")

    def test_a_restricted_stop_serves_only_its_own_routes(self):
        """University Shopping Centre is the whole of Oxford Road otherwise."""
        usc = config.STOP_BY_ID["1800SB30631"]
        assert usc.routes == frozenset({"41"})
        assert config.stop_serves(usc.atco, "41")
        for other in ("143", "43", "142", "191"):
            assert not config.stop_serves(usc.atco, other)

    def test_an_unwatched_stop_serves_nothing(self):
        """Not merely False for tidiness: `Trip.target_calls` asks about every
        stop on a trip, most of which are not watched at all."""
        assert not config.stop_serves("1800EB99999", "41")


class TestBoundingBox:
    def test_is_well_formed(self):
        min_lon, min_lat, max_lon, max_lat = config.BBOX
        assert min_lon < max_lon
        assert min_lat < max_lat

    def test_contains_every_watched_stop(self):
        min_lon, min_lat, max_lon, max_lat = config.BBOX
        # Coordinates resolved from NaPTAN area 180.
        for lat, lon in (
            (53.464085, -2.222301),
            (53.46225, -2.223839),
            (53.4674, -2.219859),
            (53.467531, -2.234924),
        ):
            assert min_lat < lat < max_lat
            assert min_lon < lon < max_lon


class TestRepositoryHygiene:
    """Guards against the most likely way the key escapes: a stray commit."""

    def test_env_is_gitignored(self):
        assert ".env" in (REPO / ".gitignore").read_text()

    def test_env_is_excluded_from_the_docker_context(self):
        assert ".env" in (REPO / ".dockerignore").read_text()

    def test_example_env_holds_no_real_key(self):
        text = (REPO / ".env.example").read_text()
        assert "your-key-here" in text

    def test_fixtures_contain_no_key(self):
        for path in (REPO / "tests" / "fixtures").iterdir():
            if path.suffix == ".xml":
                assert "api_key" not in path.read_text()
