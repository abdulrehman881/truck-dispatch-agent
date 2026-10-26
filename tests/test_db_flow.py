"""
Phase 5 tests: database-flows with a fake psycopg connection.

No real database writes; the fake connection records SQL and returns
scripted rows so endpoint behavior can be asserted deterministically.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import main
from fastapi.testclient import TestClient


class FakeCursor:
    """Minimal cursor shell: scripted fetchone results, recorded SQL."""

    def __init__(self, results, all_results=None):
        # Each TODO entry is the value that the next fetchone returns.
        self._results = list(results)
        self._all_results = list(all_results or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql.strip(), params))

    def fetchone(self):
        return self._results.pop(0)

    def fetchall(self):
        return self._all_results.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        # Mirror psycopg semantics: commit on success, rollback on error.
        if exc_type is None:
            self.commits += 1
        else:
            self.rollbacks += 1
        return False


@pytest.fixture(autouse=True)
def mock_groq(monkeypatch):
    class FakeCompletions:
        def create(self, **kwargs):
            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]

            return R()

    class FakeGroq:
        chat = type("Chat", (), {})()
        chat.completions = FakeCompletions()

    monkeypatch.setattr(main, "groq_client", FakeGroq())


client = TestClient(
    main.app,
    headers={"Authorization": "Bearer test-admin-token"},
)


def _fake_db(monkeypatch, cursor):
    connection = FakeConnection(cursor)
    monkeypatch.setattr(
        main, "get_db_connection", lambda: connection
    )
    return connection


USDOT = "111111"


def test_carrier_call_attempt_flow_and_reservation_release(monkeypatch):
    attempt_row = (
        7,
        USDOT,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        "initiated",
        True,
        False,
    )

    cursor = FakeCursor(
        [
            (1,),   # carrier exists
            attempt_row,  # INSERT ... RETURNING
        ]
    )
    connection = _fake_db(monkeypatch, cursor)

    response = client.post(
        "/carrier-call-attempts",
        json={
            "usdot_number": USDOT,
            "call_status": "initiated",
            "interested": True,
            "consent_status": "granted",
            "notes": "mock test: simulated interested carrier",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["attempt"]["id"] == 7
    assert body["attempt"]["usdot_number"] == USDOT
    assert body["attempt"]["call_status"] == "initiated"

    # The reservation must be released in the same transaction.
    assert connection.commits >= 1
    sqls = " | ".join(sql for sql, _ in cursor.executed)
    assert "review_reserved_at = NULL" in sqls


def test_call_attempt_rejects_unknown_carrier(monkeypatch):
    cursor = FakeCursor([None])  # carrier does not exist
    _fake_db(monkeypatch, cursor)

    response = client.post(
        "/carrier-call-attempts",
        json={"usdot_number": "NOSUCH", "call_status": "initiated"},
    )
    assert response.status_code == 404


def test_review_save_flow(monkeypatch):
    review_row = (
        3,
        USDOT,
        "interested",
        None,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    cursor = FakeCursor([(1,), review_row])
    connection = _fake_db(monkeypatch, cursor)

    response = client.post(
        "/carrier-reviews",
        json={
            "usdot_number": USDOT,
            "review_status": "interested",
            "notes": "mock test",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["review"]["review_status"] == "interested"
    assert "No phone call was made" in body["message"]

    # Saving a review also releases any reservation held on the lead.
    assert "review_reserved_until = NULL" in " | ".join(
        sql for sql, _ in cursor.executed
    )


def test_metrics_shape(monkeypatch):
    cursor = FakeCursor(
        [(100, 80, 20, 90), (2,)],
        all_results=[
            [("completed", 5), ("no_answer", 2)],
            [("interested", 3), ("do_not_contact", 1)],
        ],
    )
    _fake_db(monkeypatch, cursor)

    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["leads"]["total_leads"] == 100
    assert body["leads"]["qualified_leads"] == 80
    assert body["call_attempts_by_status"]["completed"] == 5
    assert body["reviews_by_status"]["interested"] == 3
    assert body["active_reservations"] == 2
    assert body["outbound_call_mode"] in ("offline", "mock")


def test_outbound_call_mode_guard():
    # The live mode must be impossible without approval; offline/mock allowed.
    assert main.OUTBOUND_CALL_MODE in ("offline", "mock")


# --------------------------------------------------
# Audit transactionality and rollback
# --------------------------------------------------

def test_review_audit_written_within_transaction(monkeypatch):
    review_row = (
        5,
        USDOT,
        "interested",
        None,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    cursor = FakeCursor([(1,), review_row])
    connection = _fake_db(monkeypatch, cursor)

    response = client.post(
        "/carrier-reviews",
        json={"usdot_number": USDOT, "review_status": "interested"},
    )

    assert response.status_code == 200

    audit_sqls = [
        sql for sql, _ in cursor.executed if "operator_audit_log" in sql
    ]
    assert len(audit_sqls) == 1
    # Parameterized audit insert: no string-built SQL.
    assert "%s" in audit_sqls[0]

    # One successful transaction commit; nothing was torn back.
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_failed_audit_rolls_back_review(monkeypatch):
    class FailingAuditCursor(FakeCursor):
        """Explodes exactly when the audit insert would run."""

        def execute(self, sql, params=None):
            if "operator_audit_log" in sql:
                raise RuntimeError("simulated audit failure")
            super().execute(sql, params)

    review_row = (
        6,
        USDOT,
        "interested",
        None,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    cursor = FailingAuditCursor([(1,), review_row])
    connection = _fake_db(monkeypatch, cursor)

    response = client.post(
        "/carrier-reviews",
        json={"usdot_number": USDOT, "review_status": "interested"},
    )

    # The save must fail, and the transaction must be rolled back:
    # an event is only recorded if the audit entry is also saved.
    assert response.status_code == 500
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_call_attempt_audit_written(monkeypatch):
    attempt_row = (
        21,
        USDOT,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        "initiated",
        True,
        False,
    )
    cursor = FakeCursor([(1,), attempt_row])
    _fake_db(monkeypatch, cursor)

    response = client.post(
        "/carrier-call-attempts",
        json={
            "usdot_number": USDOT,
            "call_status": "initiated",
            "interested": True,
            "consent_status": "granted",
        },
    )

    assert response.status_code == 200
    audit_sqls = [
        sql for sql, _ in cursor.executed if "operator_audit_log" in sql
    ]
    assert len(audit_sqls) == 1


# --------------------------------------------------
# Injection resistance (search query is fully parameterized)
# --------------------------------------------------

def test_search_is_parameterized_against_injection(monkeypatch):
    cursor = FakeCursor([], all_results=[[]])
    _fake_db(monkeypatch, cursor)

    payload = "'; DROP TABLE carrier_leads; --"
    response = client.get(
        "/carriers/search", params={"q": payload}
    )

    assert response.status_code == 200
    assert response.json()["count"] == 0

    # Nothing gets concatenated into SQL text: the payload only travels
    # as a bound parameter.
    injection_sqls = [
        sql for sql, _ in cursor.executed if payload in sql
    ]
    assert injection_sqls == []

    search_calls = [
        (sql, params) for sql, params in cursor.executed if "ILIKE" in sql
    ]
    assert len(search_calls) == 1
    assert payload in search_calls[0][1][1]
    assert payload in search_calls[0][1][2]


def test_live_mode_is_refused_at_startup():
    import subprocess
    import sys

    env = dict(os.environ)
    env["OUTBOUND_CALL_MODE"] = "live"

    result = subprocess.run(
        [sys.executable, "-c", "import main"],
        capture_output=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        timeout=60,
    )

    stderr = result.stderr.decode(errors="replace")

    assert result.returncode != 0
    assert "explicit operator approval" in stderr


# --------------------------------------------------
# Mock calling workflow (dry-run provider)
# --------------------------------------------------

MOCK_ATTEMPT_RETURNING_ROW = (
    11,
    USDOT,
    datetime(2026, 1, 1, tzinfo=timezone.utc),
    "completed",
    True,
    False,
)


@pytest.mark.parametrize(
    "outcome,expected_call_status,expected_interested",
    [
        ("interested", "completed", True),
        ("not_interested", "completed", False),
        ("no_answer", "no_answer", None),
        ("invalid_number", "failed", None),
        ("failed", "failed", None),
    ],
)
def test_mock_call_records_simulated_outcome(
    monkeypatch, outcome, expected_call_status, expected_interested
):
    monkeypatch.setattr(main, "OUTBOUND_CALL_MODE", "mock")

    row = (
        11,
        USDOT,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        expected_call_status,
        expected_interested,
        False,
    )
    cursor = FakeCursor([(1,), None, row])
    connection = _fake_db(monkeypatch, cursor)

    response = client.post(
        "/mock-call",
        json={"usdot_number": USDOT, "simulated_outcome": outcome},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "NOT a real phone call" in body["message"]
    assert body["attempt"]["call_status"] == expected_call_status
    assert body["attempt"]["interested"] is expected_interested

    sqls = " ".join(sql for sql, _ in cursor.executed)
    # Reservation release + audit both happen in the transaction.
    assert "review_reserved_until = NULL" in sqls
    assert "operator_audit_log" in sqls


def test_mock_call_blocked_in_offline_mode(monkeypatch):
    monkeypatch.setattr(main, "OUTBOUND_CALL_MODE", "offline")
    cursor = FakeCursor([])
    _fake_db(monkeypatch, cursor)

    response = client.post(
        "/mock-call",
        json={"usdot_number": USDOT, "simulated_outcome": "interested"},
    )
    assert response.status_code == 409
    assert "OUTBOUND_CALL_MODE=mock" in response.json()["detail"]


def test_mock_call_rejects_unknown_carrier(monkeypatch):
    monkeypatch.setattr(main, "OUTBOUND_CALL_MODE", "mock")
    cursor = FakeCursor([None])
    _fake_db(monkeypatch, cursor)

    response = client.post(
        "/mock-call",
        json={"usdot_number": "NOSUCH", "simulated_outcome": "interested"},
    )
    assert response.status_code == 404


def test_mock_call_blocked_by_dnc_history(monkeypatch):
    monkeypatch.setattr(main, "OUTBOUND_CALL_MODE", "mock")
    cursor = FakeCursor([(1,), (1,)])  # carrier exists, DNC attempt exists
    _fake_db(monkeypatch, cursor)

    response = client.post(
        "/mock-call",
        json={"usdot_number": USDOT, "simulated_outcome": "interested"},
    )
    assert response.status_code == 409
    assert "do-not-call" in response.json()["detail"]


# --------------------------------------------------
# Operator search
# --------------------------------------------------

def test_carriers_search_shape(monkeypatch):
    cursor = FakeCursor(
        [],
        all_results=[
            [
                (
                    USDOT,
                    "Movers LLC",
                    "5551234567",
                    12,
                    100,
                    "high",
                    True,
                    "interested",
                    None,
                )
            ]
        ],
    )
    _fake_db(monkeypatch, cursor)

    response = client.get("/carriers/search?q=Movers")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    result = body["results"][0]
    assert result["usdot_number"] == USDOT
    assert result["latest_review_status"] == "interested"


def test_carriers_search_limit_bounds():
    response = client.get("/carriers/search?limit=0")
    assert response.status_code == 400


def test_request_id_header_is_set():
    response = client.get("/health")
    assert response.headers.get("x-request-id", "").startswith("req_")

    response = client.get("/health", headers={"X-Request-ID": "req_custom123"})
    assert response.headers.get("x-request-id") == "req_custom123"
