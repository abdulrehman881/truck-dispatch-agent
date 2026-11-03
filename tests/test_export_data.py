"""
Tests for the /export-data in-browser view endpoint.

Proves: auth required, validation of review_status and limit, the
shared rows query is reused (same data source as /export-excel), and
rows come back as dicts keyed by the column names.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)
AUTHED = TestClient(
    main.app, headers={"Authorization": "Bearer test-admin-token"}
)

COLUMNS = [
    "usdot_number", "company_name", "phone", "vehicle_count",
    "lead_score", "priority", "review_status", "follow_up_at",
    "notes", "reviewed_at",
]

SAMPLE_ROWS = [
    (1234567, "Alpha Trucking LLC", "3125550142", 12, 95, "high",
     "interested", None, "Wants reefer loads", None),
    (7654321, "Beta Carriers Inc", None, 3, 41, "low",
     None, None, None, None),
]


class FakeCursor:
    def __init__(self, rows):
        self.description = [
            SimpleNamespace(name=name) for name in COLUMNS
        ]
        self._rows = list(rows)

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConnection:
    def __init__(self, rows):
        self._rows = rows
        self._cursor = None

    def cursor(self):
        if self._cursor is None:
            self._cursor = FakeCursor(self._rows)
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_export_data_requires_token():
    assert client.get("/export-data").status_code == 401


def test_export_data_rejects_unknown_review_status():
    assert (
        AUTHED.get("/export-data?review_status=maybe")
        .status_code == 422
    )


def test_export_data_rejects_bad_limit():
    assert AUTHED.get("/export-data?limit=0").status_code == 422
    assert AUTHED.get("/export-data?limit=3000").status_code == 422


def test_export_data_returns_rows_as_dicts(monkeypatch):
    conn = FakeConnection(SAMPLE_ROWS)
    monkeypatch.setattr(main, "get_db_connection", lambda: conn)

    response = AUTHED.get("/export-data")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(SAMPLE_ROWS)
    assert len(body["rows"]) == body["count"]
    first = body["rows"][0]
    assert first["usdot_number"] == 1234567
    assert first["company_name"] == "Alpha Trucking LLC"
    assert first["priority"] == "high"
    assert set(first.keys()) == set(COLUMNS)


def test_export_data_passes_filter_and_limit_to_query(monkeypatch):
    conn = FakeConnection([])
    cursor = conn.cursor()
    monkeypatch.setattr(main, "get_db_connection", lambda: conn)

    response = AUTHED.get(
        "/export-data?review_status=interested&limit=17"
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"count": 0, "limit": 17, "rows": []}
    sql = cursor.last_sql
    assert "review_status = %s" in sql
    assert "LIMIT %s" in sql


def test_export_data_handles_db_failure(monkeypatch):
    def _broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(main, "get_db_connection", _broken)
    response = AUTHED.get("/export-data")
    assert response.status_code == 500
    assert "server terminal" in response.json()["detail"]


def test_export_excel_still_works_after_refactor(monkeypatch):
    """The refactor must not break the original download endpoint."""
    conn = FakeConnection(SAMPLE_ROWS)
    monkeypatch.setattr(main, "get_db_connection", lambda: conn)

    response = AUTHED.get("/export-excel")
    assert response.status_code == 200
    assert "spreadsheetml" in response.headers["content-type"]
    assert response.content[:2] == b"PK"
