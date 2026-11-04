"""
API tests for critical TruckDispatchAgent endpoints.

AI providers are mocked. Database-dependent tests run read-only
and skip automatically when PostgreSQL is unreachable.
"""

import os
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import main


client = TestClient(
    main.app,
    headers={"Authorization": "Bearer test-admin-token"},
)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    def create(self, **kwargs):
        return FakeResponse("This is a mocked reply.")


class FakeGroqClient:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions()


@pytest.fixture(autouse=True)
def mock_groq(monkeypatch):
    monkeypatch.setattr(main, "groq_client", FakeGroqClient())


def db_available():
    try:
        with main.get_db_connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def test_home():
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/pipeline"

    followed = client.get("/")
    assert followed.status_code == 200
    assert "Operator console" in followed.text or "pipeline" in followed.text.lower()


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_chat_mocked():
    response = client.post("/chat", json={"message": "Hello Adam"})
    assert response.status_code == 200
    assert response.json() == {"reply": "This is a mocked reply."}


def test_chat_rejects_empty_message():
    response = client.post("/chat", json={"message": ""})
    assert response.status_code == 422


def test_chat_rejects_oversized_message():
    response = client.post("/chat", json={"message": "a" * 4001})
    assert response.status_code == 422


def test_review_commands_recognized():
    cases = {
        "mark this carrier interested": "interested",
        "mark carrier as interested": "interested",
        "mark this carrier not interested": "not_interested",
        "mark this carrier for callback": "callback_requested",
        "mark this carrier do not contact": "do_not_contact",
        "Mark This Carrier Interested.": "interested",
    }
    for message, expected in cases.items():
        assert main.detect_review_request(message) == expected, message


def test_review_commands_reject_other_text():
    assert main.detect_review_request("please mark them interested") is None
    assert main.detect_review_request("hello there") is None


def test_confirmation_commands_accepted():
    for message in (
        "confirm carrier review",
        "yes, confirm carrier review",
        "Confirm the carrier review.",
        "save the carrier review",
    ):
        assert main.is_explicit_review_confirmation(message) is True, message


def test_confirmation_commands_reject_other_text():
    for message in ("maybe", "save it", "confirm please", "yes"):
        assert main.is_explicit_review_confirmation(message) is False, message


def test_review_callback_requires_follow_up():
    response = client.post(
        "/carrier-reviews",
        json={
            "usdot_number": "123456",
            "review_status": "callback_requested",
        },
    )
    assert response.status_code == 400


def test_review_rejects_invalid_status():
    response = client.post(
        "/carrier-reviews",
        json={
            "usdot_number": "123456",
            "review_status": "never_gonna_work",
            "follow_up_at": datetime.now().isoformat(),
        },
    )
    assert response.status_code == 422


def test_call_attempt_rejects_invalid_status():
    response = client.post(
        "/carrier-call-attempts",
        json={"usdot_number": "123456", "call_status": "teleported"},
    )
    assert response.status_code == 422


def test_eligible_carriers_limit_bounds():
    response = client.get("/eligible-carriers?limit=0")
    assert response.status_code == 400

    response = client.get("/eligible-carriers?limit=101")
    assert response.status_code == 400


@pytest.mark.skipif(
    not db_available(), reason="PostgreSQL is not reachable"
)
def test_eligible_carriers_read_only():
    response = client.get("/eligible-carriers")
    assert response.status_code == 200
    body = response.json()
    assert "count" in body and "candidates" in body
    assert isinstance(body["candidates"], list)
    for candidate in body["candidates"]:
        assert "usdot_number" in candidate
        assert "phone" in candidate
