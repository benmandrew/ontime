"""Parsing the SIRI-VM feed, including the properties the design depends on."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
import requests

from ontime import config, siri

from .conftest import siri_document, vehicle_xml


def test_parses_recorded_response(real_siri_xml):
    vehicles = siri.parse(real_siri_xml)
    assert len(vehicles) == 20
    assert all(v.vehicle_ref for v in vehicles)
    assert all(-3.0 < v.lon < -1.5 for v in vehicles)
    assert all(53.0 < v.lat < 54.0 for v in vehicles)
    assert {v.route_name for v in vehicles} <= {"192", "50"}


def test_recorded_response_carries_no_arrival_predictions(real_siri_xml):
    """The premise of the whole project: BODS publishes positions only.

    If this ever fails, the feed has started carrying stop-level predictions
    and the local estimator could be replaced with the published values.
    """
    text = real_siri_xml.decode()
    for element in ("MonitoredCall", "OnwardCalls", "ExpectedArrivalTime"):
        assert element not in text


def test_timestamps_are_timezone_aware(real_siri_xml):
    for v in siri.parse(real_siri_xml):
        assert v.recorded_at.tzinfo is not None
        if v.origin_dep is not None:
            assert v.origin_dep.tzinfo is not None


def test_malformed_activities_are_skipped():
    good = vehicle_xml(
        line="192",
        origin_ref="A",
        dest_ref="B",
        origin_dep=datetime.now(UTC),
        recorded_at=datetime.now(UTC),
        lat=53.46,
        lon=-2.22,
    )
    no_location = "<VehicleActivity><RecordedAtTime>2026-01-01T00:00:00+00:00</RecordedAtTime></VehicleActivity>"
    bad_numbers = re.sub(r"<Latitude>[^<]+</Latitude>", "<Latitude>abc</Latitude>", good)

    vehicles = siri.parse(siri_document([good, no_location, bad_numbers]))
    assert len(vehicles) == 1


def test_age_and_staleness(monkeypatch, api_key):
    now = datetime.now(UTC)
    fresh = vehicle_xml(
        line="192",
        origin_ref="A",
        dest_ref="B",
        origin_dep=now,
        recorded_at=now - timedelta(seconds=20),
        lat=53.46,
        lon=-2.22,
        vehicle_ref="FRESH",
    )
    ghost = vehicle_xml(
        line="192",
        origin_ref="A",
        dest_ref="B",
        origin_dep=now,
        recorded_at=now - timedelta(hours=8),
        lat=53.46,
        lon=-2.22,
        vehicle_ref="GHOST",
    )
    payload = siri_document([fresh, ghost])

    class FakeResponse:
        content = payload
        status_code = 200

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *_a, **_k: FakeResponse())
    monkeypatch.setattr(config, "STALE_SECS", 180)

    kept = siri.fetch()
    assert [v.vehicle_ref for v in kept] == ["FRESH"], "8-hour-old ghost must be dropped"


def test_route_filter(monkeypatch, api_key):
    now = datetime.now(UTC)
    acts = [
        vehicle_xml(
            line=line,
            origin_ref="A",
            dest_ref="B",
            origin_dep=now,
            recorded_at=now,
            lat=53.46,
            lon=-2.22,
            vehicle_ref=f"V{line}",
        )
        for line in ("192", "50", "999")
    ]

    class FakeResponse:
        content = siri_document(acts)

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *_a, **_k: FakeResponse())
    kept = siri.fetch(routes={"192", "50"})
    assert {v.route_name for v in kept} == {"192", "50"}


def test_fetch_redacts_key_from_transport_errors(monkeypatch, api_key):
    """A leaked key in a stack trace is the realistic exposure path."""

    def boom(*_a, **_k):
        raise requests.RequestException(
            f"HTTPSConnectionPool: /api/v1/datafeed/?api_key={api_key}&boundingBox=1"
        )

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(RuntimeError) as excinfo:
        siri.fetch()
    assert api_key not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)
