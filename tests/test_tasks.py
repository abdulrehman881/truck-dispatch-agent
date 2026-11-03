"""
Persistent scheduled-task tests.

Every follow-up is stored in PostgreSQL, the worker flips it to "due"
when its time comes, and the app surfaces it as a reminder. These tests
prove the storage helpers, the callback auto-creation, the worker, the
API, and the assistant commands — without placing any call.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)
authed = TestClient(
    main.app, headers={"Authorization": "Bearer test-admin-token"}
)

NOW = datetime.now(timezone.utc)

REVIEW_ROW = (42, "1234567", "callback_requested", NOW + timedelta(hours=2), NOW)
TASK_ROW = (
    7,
    "callback",
    "1234567",
    "Call back carrier 1234567",
    "asked for later",
    NOW + timedelta(hours=2),
    "pending",
    "carrier_review",
    None,
    None,
)
REMINDER_ROW = (
    8,
    "reminder",
    None,
    "Check carrier prices",
    None,
    NOW + timedelta(hours=1),
    "pending",
    "assistant",
    None,
    None,
)


class _Cursor:
    def __init__(self, fetches=None, rows=None):
        self.fetches = list(fetches or [])
        self.rows = list(rows or [])
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return self.fetches.pop(0) if self.fetches else None

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _install(monkeypatch, cursor):
    monkeypatch.setattr(
        main, "get_db_connection", lambda: _Connection(cursor)
    )
    return cursor


def _task_dict(row):
    return main._task_row_to_dict(row)



def test_create_task_reminder(monkeypatch):
    cursor = _install(monkeypatch, _Cursor(fetches=[REMINDER_ROW]))

    task = main.create_task(
        "reminder",
        "Check carrier prices",
        NOW + timedelta(hours=1),
        source="assistant",
    )

    assert task["id"] == 8
    assert task["task_type"] == "reminder"
    assert task["status"] == "pending"
    assert "INSERT INTO public.scheduled_tasks" in cursor.calls[0][0]


def test_callback_task_cancels_previous_active_task(monkeypatch):
    cursor = _install(monkeypatch, _Cursor(fetches=[TASK_ROW]))

    task = main.create_task(
        "callback",
        "Call back carrier 1234567",
        NOW + timedelta(hours=2),
        usdot_number="1234567",
    )

    assert task["task_type"] == "callback"
    assert len(cursor.calls) == 2
    assert "UPDATE public.scheduled_tasks" in cursor.calls[0][0]
    assert "status IN ('pending', 'due')" in cursor.calls[0][0]
    assert cursor.calls[0][1] == ("1234567",)


def test_unknown_task_type_is_rejected(monkeypatch):
    _install(monkeypatch, _Cursor())

    with pytest.raises(ValueError):
        main.create_task("delete_everything", "x", NOW)


def test_list_tasks_default_excludes_done(monkeypatch):
    cursor = _install(monkeypatch, _Cursor(rows=[TASK_ROW]))

    tasks = main.list_tasks()

    assert len(tasks) == 1
    assert tasks[0]["id"] == 7
    assert "status IN ('pending', 'due')" in cursor.calls[0][0]


def test_list_tasks_status_filter(monkeypatch):
    cursor = _install(monkeypatch, _Cursor(rows=[]))

    main.list_tasks(status="done")

    sql, params = cursor.calls[0]
    assert "status = %s" in sql
    assert params[0] == "done"


def test_list_tasks_due_only(monkeypatch):
    cursor = _install(monkeypatch, _Cursor(rows=[]))

    main.list_tasks(due_only=True)

    assert "due_at <= CURRENT_TIMESTAMP" in cursor.calls[0][0]


def test_update_task_status_done(monkeypatch):
    done_row = TASK_ROW[:6] + ("done",) + TASK_ROW[7:]
    cursor = _install(monkeypatch, _Cursor(fetches=[done_row]))

    task = main.update_task_status(7, "done")

    assert task["status"] == "done"
    sql = cursor.calls[0][0]
    assert "completed_at = CASE" in sql


def test_update_task_status_unknown_raises():
    with pytest.raises(ValueError):
        main.update_task_status(1, "deleted")


def test_update_task_status_missing_returns_none(monkeypatch):
    _install(monkeypatch, _Cursor(fetches=[None]))
    assert main.update_task_status(999, "done") is None



def test_process_due_tasks_marks_due_without_calling(monkeypatch):
    cursor = _install(
        monkeypatch,
        _Cursor(
            rows=[
                (7, "callback", "1234567", "Call back carrier 1234567",
                 NOW - timedelta(minutes=1)),
            ]
        ),
    )

    count = main.process_due_tasks()

    assert count == 1
    update_sql, update_params = cursor.calls[1]
    assert "SET" in update_sql
    assert "status = 'due'" in update_sql
    assert update_params == (7,)

    every_sql = " ".join(call[0] for call in cursor.calls)
    assert "carrier_call_attempts" not in every_sql
    assert "INSERT INTO public.carrier_reviews" not in every_sql


def test_process_due_tasks_nothing_due(monkeypatch):
    cursor = _install(monkeypatch, _Cursor(rows=[]))
    assert main.process_due_tasks() == 0
    assert len(cursor.calls) == 1



def test_callback_review_creates_callback_task(monkeypatch):
    cursor = _install(
        monkeypatch,
        _Cursor(fetches=[(1,), REVIEW_ROW, TASK_ROW]),
    )

    result = main.save_carrier_review(
        main.CarrierReviewRequest(
            usdot_number="1234567",
            review_status="callback_requested",
            follow_up_at=NOW + timedelta(hours=2),
            notes="asked for later",
        )
    )

    assert result["success"] is True
    insert_sql = [call[0] for call in cursor.calls if "INSERT INTO public.scheduled_tasks" in call[0]]
    assert len(insert_sql) == 1
    assert any(
        "UPDATE public.scheduled_tasks" in call[0] for call in cursor.calls
    )


def test_interested_review_creates_no_callback_task(monkeypatch):
    row = (43, "1234567", "interested", None, NOW)
    cursor = _install(monkeypatch, _Cursor(fetches=[(1,), row]))

    main.save_carrier_review(
        main.CarrierReviewRequest(
            usdot_number="1234567",
            review_status="interested",
        )
    )

    assert not any(
        "INSERT INTO public.scheduled_tasks" in call[0]
        for call in cursor.calls
    )



def test_tasks_require_admin_token():
    assert client.get("/tasks").status_code == 401
    assert client.post(
        "/tasks/reminders", json={"title": "x", "due_at": "in 1 hour"}
    ).status_code == 401


def test_create_reminder_endpoint(monkeypatch):
    captured = {}

    def fake_create(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _task_dict(REMINDER_ROW)

    monkeypatch.setattr(main, "create_task", fake_create)

    response = authed.post(
        "/tasks/reminders",
        json={"title": "Check carrier prices", "due_at": "in 2 hours"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["task"]["id"] == 8
    assert captured["args"][0] == "reminder"


def test_create_reminder_endpoint_rejects_bad_time():
    response = authed.post(
        "/tasks/reminders",
        json={"title": "Check prices", "due_at": "whenever"},
    )
    assert response.status_code == 400


def test_list_tasks_endpoint(monkeypatch):
    monkeypatch.setattr(main, "list_tasks", lambda **kwargs: [_task_dict(TASK_ROW)])

    response = authed.get("/tasks?due_only=true")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["due_count"] == 0


def test_complete_task_endpoint(monkeypatch):
    done_row = TASK_ROW[:6] + ("done",) + TASK_ROW[7:]
    monkeypatch.setattr(main, "update_task_status", lambda task_id, status: _task_dict(done_row))

    response = authed.post("/tasks/7/done")

    assert response.status_code == 200
    assert response.json()["task"]["status"] == "done"


def test_complete_missing_task_is_404(monkeypatch):
    monkeypatch.setattr(main, "update_task_status", lambda task_id, status: None)
    assert authed.post("/tasks/999/done").status_code == 404



def test_assistant_add_reminder(monkeypatch):
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {
            "action": "add_reminder",
            "args": {"title": "Check carrier prices", "due_at": "in 2 hours"},
        },
    )
    monkeypatch.setattr(main, "create_task", lambda *a, **k: _task_dict(REMINDER_ROW))

    body = authed.post(
        "/assistant", json={"message": "remind me to check prices in 2 hours"}
    ).json()

    assert body["action"] == "add_reminder"
    assert "Reminder #8" in body["reply"]
    assert "no call is placed" in body["reply"]


def test_assistant_list_tasks(monkeypatch):
    due_task = REMINDER_ROW[:6] + ("due",) + REMINDER_ROW[7:]
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {"action": "list_tasks", "args": {}},
    )
    monkeypatch.setattr(main, "list_tasks", lambda **kwargs: [_task_dict(due_task), _task_dict(TASK_ROW)])

    body = authed.post(
        "/assistant", json={"message": "my tasks"}
    ).json()

    assert body["action"] == "list_tasks"
    assert body["data"]["due_count"] == 1
    assert "Check carrier prices" in body["reply"]


def test_assistant_complete_task(monkeypatch):
    done_row = TASK_ROW[:6] + ("done",) + TASK_ROW[7:]
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {"action": "complete_task", "args": {"task_id": 7}},
    )
    monkeypatch.setattr(main, "update_task_status", lambda task_id, status: _task_dict(done_row))

    body = authed.post(
        "/assistant", json={"message": "mark task 7 done"}
    ).json()

    assert body["action"] == "complete_task"
    assert "Task #7 marked done" in body["reply"]


def test_assistant_complete_unknown_task_is_404(monkeypatch):
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {"action": "complete_task", "args": {"task_id": 1234}},
    )
    monkeypatch.setattr(main, "update_task_status", lambda task_id, status: None)

    response = authed.post(
        "/assistant", json={"message": "mark task 1234 done"}
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "message,action",
    [
        ("my tasks", "list_tasks"),
        ("what do i need to do", "list_tasks"),
        ("mark task 12 done", "complete_task"),
        ("remind me to check prices in 2 hours", "add_reminder"),
    ],
)
def test_task_fallback_parser(message, action):
    assert main._assistant_fallback_plan(message)["action"] == action


def test_reminder_fallback_extracts_title_and_time():
    plan = main._assistant_fallback_plan(
        "remind me to call Rob in 3 hours"
    )
    assert plan["action"] == "add_reminder"
    assert plan["args"]["title"] == "call Rob"
    assert plan["args"]["due_at"] == "in 3 hours"
