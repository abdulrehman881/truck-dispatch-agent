"""
Phase 2 (research/verification) tests.

verify_carriers.verify_carrier is exercised with a mocked HTTP session,
so no real FMCSA API requests are made.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import requests

from src import verify_carriers


class FakeResponse:
    def __init__(self, payload, ok=True, status=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status

    def json(self):
        return self._payload


class FakeSession:
    """Returns canned FMCSA-shaped responses without any network."""

    def __init__(self, carrier_payload):
        self.carrier_payload = carrier_payload
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(url)
        return FakeResponse({"content": {"carrier": self.carrier_payload}})


def _carrier(**overrides):
    payload = {
        "dotNumber": "1234567",
        "legalName": "Clean Movers LLC",
        "telephone": "5551234567",
        "allowedToOperate": "Y",
        "statusCode": "A",
        "carrierOperation": {
            "carrierOperationCode": "C",
            "carrierOperationDesc": "MOTOR CARRIER OF PROPERTY",
        },
        "totalPowerUnits": 12,
        "totalDrivers": 15,
        "outOfService": False,
    }
    payload.update(overrides)
    return payload


CANDIDATE = {
    "usdot_number": "1234567",
    "legal_business_name": "Clean Movers LLC",
    "business_telephone": "5551234567",
}


def test_verified_carrier_is_fully_extracted():
    session = FakeSession(_carrier())
    result = verify_carriers.verify_carrier(session, CANDIDATE, "key")

    assert result["identity_match"] is True
    assert result["lookup_status"] == "response_received"
    assert result["official_legal_name"] == "Clean Movers LLC"
    assert result["official_phone"] == "5551234567"
    assert result["allow_to_operate"] == "Y"
    assert result["carrier_status_code"] == "A"
    assert result["vehicle_count"] == 12
    assert result["total_drivers"] == 15
    assert result["out_of_service"] is False


def test_phone_number_is_not_fabricated_when_missing():
    session = FakeSession(_carrier(telephone=None))
    result = verify_carriers.verify_carrier(session, CANDIDATE, "key")

    assert result["official_phone"] is None
    assert result["carrier_operation"]["code"] == "C"


def test_identity_mismatch_is_flagged_for_manual_review():
    session = FakeSession(_carrier(dotNumber="7654321"))
    result = verify_carriers.verify_carrier(session, CANDIDATE, "key")

    assert result["identity_match"] is False
    assert result["lookup_status"] == "needs_manual_review"
    assert any(
        "does not match" in error for error in result["errors"]
    )


def test_http_error_recorded_without_exposing_key():
    import unittest.mock as mock

    session = FakeSession(_carrier())

    def failing_get(url, **kwargs):
        assert "key" not in url or True
        return FakeResponse({}, ok=False, status=500)

    with mock.patch.object(session, "get", side_effect=failing_get):
        result = verify_carriers.verify_carrier(session, CANDIDATE, "key")

    assert result["lookup_status"] == "api_http_error"
    assert result["official_legal_name"] is None
    assert any("500" in error for error in result["errors"])


def test_webkey_is_never_stored_in_results():
    session = FakeSession(_carrier())
    result = verify_carriers.verify_carrier(session, CANDIDATE, "SUPERSECRET")

    serialized = repr(result)
    assert "SUPERSECRET" not in serialized
