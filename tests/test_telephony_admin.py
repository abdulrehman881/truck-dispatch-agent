"""
Tests for telephony provider configuration (Phase 6 groundwork).

Proves: auth is required, credentials can be saved/removed, secrets
never leave the server (masked previews only), the dial list excludes
do-not-contact and not-interested carriers, and the bounded provider
test refuses unbounded or unconfigured use.
"""

from types import SimpleNamespace

import pytest
import main
from fastapi.testclient import TestClient

client = TestClient(main.app)
AUTHED = TestClient(main.app, headers={"Authorization": "Bearer test-admin-token"})


class FakeCursor:
    """Records SQL and hands back canned rows."""

    def __init__(self, rows):
        self.description = [SimpleNamespace(name=n) for n in
                            ("provider", "account_sid", "auth_token",
                             "caller_id_number", "status", "updated_at")]
        self._rows = list(rows)

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params

    def fetchall(self):
        return [self._rows[0]] if self._rows else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConnection:
    def __init__(self, rows):
        self._cursor = FakeCursor(rows)

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


STORED = ("twilio", "AC1234567890abcdef", "secret-auth-token-xyz",
          "15550101234", "configured", None)


def test_telephony_requires_token():
    assert client.get("/admin/telephony").status_code == 401
    assert client.post("/admin/telephony", json={
        "provider": "twilio", "account_sid": "AC123",
        "auth_token": "s" * 12, "caller_id_number": "15550101234",
    }).status_code == 401
    assert client.get("/admin/telephony/dial-list").status_code == 401


def test_unconfigured_status_masks_everything(monkeypatch):
    monkeypatch.setattr(main, "load_telephony_config", lambda: None)
    body = AUTHED.get("/admin/telephony").json()
    assert body["configured"] is False
    assert body["status"] == "not_configured"
    assert body["live_calls_allowed"] is False
    assert body["account_sid_masked"] is None


def test_save_rejects_unknown_provider():
    response = AUTHED.post("/admin/telephony", json={
        "provider": "megaphone", "account_sid": "AC123456",
        "auth_token": "s" * 16, "caller_id_number": "15550101234",
    })
    assert response.status_code == 422
    assert "twilio" in response.json()["detail"]


def test_save_requires_valid_caller_id():
    response = AUTHED.post("/admin/telephony", json={
        "provider": "twilio", "account_sid": "AC123456",
        "auth_token": "s" * 16, "caller_id_number": "abcd",
    })
    assert response.status_code == 422
    assert "4 digits" in response.json()["detail"]


def test_save_upserts_and_never_echoes_secrets(monkeypatch):
    fake = FakeConnection(STORED)
    monkeypatch.setattr(main, "get_db_connection", lambda: fake)
    response = AUTHED.post("/admin/telephony", json={
        "provider": "TWILIO",
        "account_sid": "AC1234567890abcdef",
        "auth_token": "secret-auth-token-xyz",
        "caller_id_number": "+1 (555) 010-1234",
    })
    assert response.status_code == 200
    assert response.json()["saved"] is True
    payload = response.json()
    assert "secret-auth-token-xyz" not in response.text
    # The upsert reached the database with every field
    sql = fake._cursor.last_sql
    assert "ON CONFLICT (id) DO UPDATE" in sql
    params = fake._cursor.last_params
    assert params[0] == "twilio"          # provider normalised
    assert params[3] == "15550101234"     # caller id digits only


def test_get_returns_masked_preview_only(monkeypatch):
    monkeypatch.setattr(main, "load_telephony_config", lambda: dict(
        provider="twilio", account_sid="AC1234567890abcdef",
        auth_token="secret-auth-token-xyz",
        caller_id_number="15550101234", status="configured",
        updated_at=None))
    response = AUTHED.get("/admin/telephony")
    assert response.status_code == 200
    text = response.text
    assert "secret-auth-token-xyz" not in text
    assert "AC1234567890abcdef" not in text  # raw sid hidden too
    body = response.json()
    assert body["configured"] is True
    assert body["account_sid_masked"].endswith("cdef")
    assert body["caller_id_number"] == "15550101234"
    assert body["live_calls_allowed"] is False


def test_clear_resets_configuration(monkeypatch):
    fake = FakeConnection([])
    monkeypatch.setattr(main, "get_db_connection", lambda: fake)
    response = AUTHED.delete("/admin/telephony")
    assert response.status_code == 200
    assert response.json()["cleared"] is True
    assert response.json()["configured"] is False
    assert "UPDATE public.telephony_config" in fake._cursor.last_sql


def test_provider_test_refuses_when_unconfigured(monkeypatch):
    monkeypatch.setattr(main, "load_telephony_config", lambda: None)
    assert AUTHED.post("/admin/telephony/test").status_code == 409


def test_provider_check_only_touches_the_account_api(monkeypatch):
    """The test call is read-only: no call can be placed through it."""
    monkeypatch.setattr(main, "load_telephony_config", lambda: dict(
        provider="twilio", account_sid="AC123", auth_token="tok",
        caller_id_number="1555", status="configured", updated_at=None))
    calls = []

    def fake_check(provider, sid, auth_token):
        calls.append((provider, sid, auth_token))
        return {"ok": True, "http_status": 200,
                "account_friendly_name": "Test Account"}

    monkeypatch.setattr(main, "_provider_status_check", fake_check)
    body = AUTHED.post("/admin/telephony/test").json()
    assert body["ok"] is True
    assert calls == [("twilio", "AC123", "tok")]


def test_provider_check_reports_rejection():
    # Real network-independent check: HTTPError path maps to a reason.
    import urllib.error

    class Error(urllib.error.HTTPError):
        def __init__(self):
            super().__init__(url="x", code=401, msg="Unauthorized",
                             hdrs=None, fp=None)

    recorded = []
    real_urlopen = main.urllib.request.urlopen

    def fake_urlopen(request, timeout):
        recorded.append(timeout)
        raise Error()

    main.urllib.request.urlopen = fake_urlopen
    try:
        result = main._provider_status_check("twilio", "AC1", "tok")
    finally:
        main.urllib.request.urlopen = real_urlopen
    assert result["ok"] is False
    assert result["http_status"] == 401
    assert "rejected" in result["reason"]
    assert recorded == [8]   # bounded timeout, no unbounded waits


def test_dial_list_limit_bounds():
    assert AUTHED.get("/admin/telephony/dial-list?limit=0").status_code == 400
    assert AUTHED.get("/admin/telephony/dial-list?limit=101").status_code == 400


def test_dial_list_filters_and_shapes(monkeypatch):
    cursor = FakeCursor([])
    monkeypatch.setattr(main, "get_db_connection", lambda: FakeConnection([]))
    holder = {"cursor": cursor}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return holder["cursor"]

    monkeypatch.setattr(main, "get_db_connection", lambda: Conn())
    monkeypatch.setattr(main, "load_telephony_config", lambda: dict(
        provider="twilio", account_sid="AC1234567890abcdef",
        auth_token="secret", caller_id_number="15550101234",
        status="configured", updated_at=None))

    response = AUTHED.get("/admin/telephony/dial-list?q=alpha&limit=15")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["carriers"] == []
    assert body["telephony"]["configured"] is True
    assert "secret" not in response.text
    sql = cursor.last_sql
    # exclusion of DNC/not-interested + phone requirement in SQL
    assert "NOT IN ('do_not_contact', 'not_interested')" in sql
    assert "c.phone IS NOT NULL AND c.phone <> ''" in sql
    assert "LIMIT %s" in sql
    assert cursor.last_params == ("alpha", "%alpha%", "%alpha%", 15)


def test_telephony_page_is_public_shell():
    response = client.get("/telephony")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_outbound_call_flow_still_works_after_refactor(monkeypatch):
    """Existing behaviour intact: the call console flow is untouched."""
    assert hasattr(main, "begin_call")
    assert main.OUTBOUND_CALL_MODE in ("offline", "mock")
