"""
Authentication tests for admin/data endpoints.

Proves: unauthenticated and wrong-token requests are rejected (401),
unconfigured-token access fails closed (503), authenticated requests
succeed, and public endpoints (health/chat/UI) never require the token.
"""

from fastapi.testclient import TestClient

import main

TEST_ADMIN_TOKEN = "test-admin-token"
AUTHED = {"Authorization": "Bearer " + TEST_ADMIN_TOKEN}

PROTECTED_PATHS = [
    ("/metrics", "GET"),
    ("/carriers/search", "GET"),
    ("/eligible-carriers", "GET"),
    ("/next-carrier?conversation_id=test", "GET"),
    ("/export-excel", "GET"),
]

PROTECTED_PATHS = [
    ("/metrics", "GET"),
]

client = TestClient(main.app)



def test_metrics_requires_token():
    assert client.get("/metrics").status_code == 401


def test_search_requires_token():
    assert client.get("/carriers/search").status_code == 401


def test_eligible_carriers_requires_token():
    assert client.get("/eligible-carriers").status_code == 401


def test_next_carrier_requires_token():
    assert client.get("/next-carrier?conversation_id=t").status_code == 401


def test_export_excel_requires_token():
    assert client.get("/export-excel").status_code == 401


def test_carrier_reviews_requires_token():
    response = client.post(
        "/carrier-reviews",
        json={"usdot_number": "1", "review_status": "interested"},
    )
    assert response.status_code == 401


def test_carrier_call_attempts_requires_token():
    response = client.post(
        "/carrier-call-attempts",
        json={"usdot_number": "1", "call_status": "initiated"},
    )
    assert response.status_code == 401


def test_mock_call_requires_token():
    response = client.post(
        "/mock-call",
        json={"usdot_number": "1", "simulated_outcome": "interested"},
    )
    assert response.status_code == 401


def test_wrong_token_is_rejected():
    response = TestClient(main.app).get(
        "/metrics", headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


def test_bad_scheme_is_rejected():
    response = TestClient(main.app).get(
        "/metrics", headers={"Authorization": "Basic test-admin-token"}
    )
    assert response.status_code == 401


def test_basic_auth_without_bearer_is_rejected():
    response = TestClient(main.app).get(
        "/eligible-carriers", headers={"Authorization": TEST_ADMIN_TOKEN}
    )
    assert response.status_code == 401



def test_authenticated_metrics_passes_guard(monkeypatch):
    class FakeCursor:
        def __init__(self, results, all_results=None):
            self._results = list(results)
            self._all = list(all_results or [])

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return self._results.pop(0)

        def fetchall(self):
            return self._all.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeConnection:
        def cursor(self):
            return FakeCursor(
                [(1, 1, 1, 1), (0,)],
                all_results=[[["interested", 1]], [["completed", 0]]],
            )

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(main, "get_db_connection", lambda: FakeConnection())

    response = TestClient(main.app, headers=AUTHED).get("/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["leads"]["total_leads"] == 1


def test_invalid_token_is_not_leaked_in_response():
    response = TestClient(main.app).get(
        "/metrics", headers={"Authorization": "Bearer hunter2-super-secret"}
    )
    body = response.json()
    assert "hunter2" not in str(body)



def test_unconfigured_token_fails_closed(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_API_TOKEN", None, raising=False)

    response = TestClient(main.app).get("/metrics")
    assert response.status_code == 503

    response = TestClient(main.app, headers=AUTHED).get("/metrics")
    assert response.status_code == 503



def test_api_token_bootstrap_hands_token_to_local_ui():
    """Localhost browser may fetch the token once (operator convenience)."""
    response = client.get("/admin/api-token")
    assert response.status_code == 200
    assert response.json()["api_token"] == TEST_ADMIN_TOKEN


def test_api_token_bootstrap_is_loopback_only(monkeypatch):
    """A remote-looking client must never see the token."""
    import pytest
    from types import SimpleNamespace

    remote = SimpleNamespace(client=SimpleNamespace(host="203.0.113.7"))
    with pytest.raises(main.HTTPException) as err:
        main.admin_api_token(remote)
    assert err.value.status_code == 403


def test_api_token_bootstrap_refuses_missing_client(monkeypatch):
    """No client info at all (proxy edge) stays closed."""
    import pytest
    from types import SimpleNamespace

    unknown = SimpleNamespace(client=None)
    with pytest.raises(main.HTTPException) as err:
        main.admin_api_token(unknown)
    assert err.value.status_code == 403


def test_api_token_bootstrap_unconfigured_fails_closed(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_API_TOKEN", None, raising=False)
    response = TestClient(main.app).get("/admin/api-token")
    assert response.status_code == 503



def test_health_is_public():
    response = client.get("/health")
    assert response.status_code == 200


def test_home_is_public():
    assert client.get("/").status_code == 200


def test_ui_page_is_public():
    assert client.get("/ui").status_code == 200


def test_dashboard_page_is_public():
    assert client.get("/dashboard").status_code == 200


def test_chat_is_public_and_mocked(monkeypatch):
    class FakeMessage:
        content = "ok"

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeGroqClient:
        chat = FakeChat()

    monkeypatch.setattr(main, "groq_client", FakeGroqClient())

    response = client.post("/chat", json={"message": "Hi Adam"})
    assert response.status_code == 200


def test_voice_ui_html_does_not_embed_token():
    html = client.get("/dashboard").text
    assert TEST_ADMIN_TOKEN not in html

    html = client.get("/ui").text
    assert TEST_ADMIN_TOKEN not in html
