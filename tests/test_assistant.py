"""
Operator assistant (chatbot) tests.

The assistant may run the app's normal operator actions, but must never
delete data, place real calls, or spend money. These tests prove the
whitelist, the refusals, the fallback parser, and the auth guard.
"""

import json

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main
import pipeline_service

client = TestClient(main.app)
authed = TestClient(
    main.app, headers={"Authorization": "Bearer test-admin-token"}
)


def _ask(message):
    return authed.post("/assistant", json={"message": message})


def test_assistant_requires_token():
    assert client.post(
        "/assistant", json={"message": "show metrics"}
    ).status_code == 401


@pytest.mark.parametrize(
    "message",
    [
        "delete all leads",
        "drop the carrier table",
        "truncate carrier_reviews",
        "wipe the database",
        "buy a DAT subscription",
        "place a real call to this carrier",
        "go live with calling",
    ],
)
def test_destructive_or_costly_requests_are_refused(message, monkeypatch):
    def _explode(msg):
        raise AssertionError("planner must not run for refused requests")

    monkeypatch.setattr(main, "plan_assistant_action", _explode)

    response = _ask(message)
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "refused"
    assert "never delete" in body["reply"]


@pytest.mark.parametrize(
    "message",
    [
        "place a real call now",
        "dial this carrier",
        "make the call",
    ],
)
def test_refusal_patterns_catch_call_phrasings(message):
    assert main._assistant_refusal_reason(message) is not None


@pytest.mark.parametrize(
    "message",
    [
        "what is the outbound calling mode",
        "show me the metrics",
        "how many calls were attempted today",
        "call the lead",
        "call the carrier",
        "call carrier 1234567",
    ],
)
def test_refusal_patterns_allow_ordinary_questions(message):
    assert main._assistant_refusal_reason(message) is None


def test_help_lists_capabilities():
    body = _ask("what can you do").json()
    assert body["action"] == "help"
    assert "metrics" in body["data"]["actions"]
    assert "save_review" in body["data"]["actions"]


def test_metrics_action(monkeypatch):
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {"action": "metrics", "args": {}},
    )
    monkeypatch.setattr(
        main,
        "dashboard_metrics",
        lambda: {
            "leads": {
                "total_leads": 487,
                "qualified_leads": 450,
                "high_priority_leads": 485,
                "leads_with_phone": 480,
            },
            "active_reservations": 2,
            "outbound_call_mode": "offline",
        },
    )

    body = _ask("how many leads do we have").json()
    assert body["action"] == "metrics"
    assert "487 leads" in body["reply"]
    assert body["data"]["leads"]["high_priority_leads"] == 485


def test_search_action(monkeypatch):
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {
            "action": "search_carriers",
            "args": {"q": "LOGISTICS", "limit": 5},
        },
    )
    monkeypatch.setattr(
        main,
        "search_carriers",
        lambda q, limit: {
            "results": [
                {
                    "usdot_number": "3819604",
                    "company_name": "MLA LOGISTICS CORP",
                    "lead_score": 100,
                    "priority": "high",
                }
            ]
        },
    )

    body = _ask("find logistics carriers").json()
    assert body["action"] == "search_carriers"
    assert "MLA LOGISTICS CORP" in body["reply"]


def test_save_review_rejects_non_numeric_usdot(monkeypatch):
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {
            "action": "save_review",
            "args": {"usdot_number": "abc", "review_status": "interested"},
        },
    )
    response = _ask("mark abc interested")
    assert response.status_code == 400


def test_save_review_executes_with_valid_usdot(monkeypatch):
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {
            "action": "save_review",
            "args": {"usdot_number": "1234567", "review_status": "interested"},
        },
    )
    monkeypatch.setattr(
        main,
        "save_carrier_review",
        lambda request: {"success": True, "message": "Carrier review saved."},
    )

    body = _ask("mark 1234567 interested").json()
    assert body["action"] == "save_review"
    assert "saved" in body["reply"].lower()


def test_save_review_passes_follow_up_for_callback(monkeypatch):
    captured = {}

    def fake_save(request):
        captured["request"] = request
        return {"success": True, "message": "Carrier review saved."}

    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {
            "action": "save_review",
            "args": {
                "usdot_number": "1234567",
                "review_status": "callback_requested",
                "follow_up_at": "in 2 hours",
            },
        },
    )
    monkeypatch.setattr(main, "save_carrier_review", fake_save)

    body = _ask("mark 1234567 callback in 2 hours").json()

    assert body["action"] == "save_review"
    follow_up = captured["request"].follow_up_at
    assert follow_up is not None
    assert follow_up.tzinfo is not None
    assert follow_up > datetime.now(timezone.utc)
    assert "Callback scheduled" in body["reply"]


def test_callback_without_follow_up_is_rejected(monkeypatch):
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {
            "action": "save_review",
            "args": {
                "usdot_number": "1234567",
                "review_status": "callback_requested",
            },
        },
    )

    response = _ask("mark 1234567 callback")

    assert response.status_code == 400
    assert "follow-up" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    "value,ok",
    [
        ("in 30 minutes", True),
        ("in 2 hours", True),
        ("in 3 days", True),
        ("2026-12-01T09:00", True),
        ("2026-12-01T09:00:00Z", True),
        ("next tuesday", False),
    ],
)
def test_parse_follow_up_at(value, ok):
    if not ok:
        with pytest.raises(Exception):
            main._parse_follow_up_at(value)
        return

    parsed = main._parse_follow_up_at(value)
    assert isinstance(parsed, datetime)
    assert parsed.tzinfo is not None


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args, **kwargs):
        self.sql = args[0] if args else ""
        self.params = args[1] if len(args) > 1 else None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConnection:
    def __init__(self, rows):
        self._cursor = _FakeCursor(rows)

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_callbacks_due_lists_scheduled(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        ("3819604", "MLA LOGISTICS CORP", "5551234567", 100, "high",
         now - timedelta(minutes=10), "asked for afternoon", True),
        ("1234567", "ACME TRUCKING", "5559876543", 80, "medium",
         now + timedelta(hours=2), None, False),
    ]
    monkeypatch.setattr(
        main, "get_db_connection", lambda: _FakeConnection(rows)
    )
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {"action": "callbacks_due", "args": {}},
    )

    body = _ask("callbacks due").json()

    assert body["action"] == "callbacks_due"
    assert body["data"]["total"] == 2
    assert body["data"]["due_count"] == 1
    assert "MLA LOGISTICS CORP" in body["reply"]
    assert "(DUE)" in body["reply"]


def test_callbacks_due_empty(monkeypatch):
    monkeypatch.setattr(
        main, "get_db_connection", lambda: _FakeConnection([])
    )
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {"action": "callbacks_due", "args": {}},
    )

    body = _ask("callbacks due").json()
    assert "No callbacks scheduled" in body["reply"]


def test_export_action_can_filter_interested(monkeypatch):
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {
            "action": "export_excel",
            "args": {"review_status": "interested"},
        },
    )

    body = _ask("export interested carriers to excel").json()
    assert body["action"] == "export_excel"
    assert body["data"]["url"] == "/export-excel?review_status=interested"
    assert "interested" in body["reply"]


def test_export_action_rejects_unknown_status(monkeypatch):
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {
            "action": "export_excel",
            "args": {"review_status": "everything"},
        },
    )

    response = _ask("export everything")
    assert response.status_code == 400


def test_export_endpoint_rejects_unknown_status():
    response = authed.get("/export-excel?review_status=everything")
    assert response.status_code == 422


def test_pipeline_status_action_uses_registry(monkeypatch):
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {"action": "pipeline_status", "args": {}},
    )

    job = pipeline_service.registry.create("test.pdf")
    pipeline_service.registry._jobs[job.job_id] = job

    body = _ask("pipeline status").json()
    assert body["action"] == "pipeline_status"
    assert job.job_id in body["reply"]


def test_pipeline_status_without_jobs(monkeypatch):
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {"action": "pipeline_status", "args": {}},
    )
    monkeypatch.setattr(
        pipeline_service.registry, "_jobs", {}, raising=False
    )

    body = _ask("pipeline status").json()
    assert "No pipeline job found" in body["reply"]



class _FakeCompletions:
    def __init__(self, content):
        self.content = content

    def create(self, **kwargs):
        message = type("M", (), {"content": self.content})
        choice = type("C", (), {"message": message})
        return type("R", (), {"choices": [choice]})


class _FakeGroq:
    def __init__(self, content):
        self.chat = type(
            "Chat", (), {"completions": _FakeCompletions(content)}
        )


def test_planner_parses_model_json(monkeypatch):
    plan_json = json.dumps(
        {"action": "search_carriers", "args": {"q": "XPO", "limit": 3}}
    )
    monkeypatch.setattr(main, "groq_client", _FakeGroq(plan_json))

    plan = main.plan_assistant_action("find XPO carriers")
    assert plan["action"] == "search_carriers"
    assert plan["args"]["q"] == "XPO"


def test_planner_rejects_unknown_action(monkeypatch):
    monkeypatch.setattr(
        main, "groq_client", _FakeGroq('{"action": "delete_all", "args": {}}')
    )

    plan = main.plan_assistant_action("please delete everything")
    assert plan["action"] in main.ASSISTANT_ACTIONS


def test_planner_falls_back_when_model_fails(monkeypatch):
    class _Broken:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("provider down")

    monkeypatch.setattr(main, "groq_client", _Broken())

    plan = main.plan_assistant_action("search ABC Trucking")
    assert plan["action"] == "search_carriers"
    assert plan["args"]["q"] == "ABC Trucking"


@pytest.mark.parametrize(
    "message,expected",
    [
        ("show me metrics", "metrics"),
        ("next carrier please", "next_carrier"),
        ("export to excel", "export_excel"),
        ("pipeline status", "pipeline_status"),
        ("search Walmart carriers", "search_carriers"),
        ("mark 4455667 interested", "save_review"),
        ("callbacks due", "callbacks_due"),
        ("totally unclear request", "help"),
    ],
)
def test_fallback_parser(message, expected):
    plan = main._assistant_fallback_plan(message)
    assert plan["action"] == expected


def test_fallback_parses_callback_schedule():
    plan = main._assistant_fallback_plan("mark 4455667 callback in 2 hours")

    assert plan["action"] == "save_review"
    assert plan["args"]["review_status"] == "callback_requested"
    assert plan["args"]["follow_up_at"] == "in 2 hours"


def test_fallback_exports_interested_only():
    plan = main._assistant_fallback_plan("export interested carriers to excel")

    assert plan["action"] == "export_excel"
    assert plan["args"]["review_status"] == "interested"


def test_unsupported_action_raises():
    with pytest.raises(Exception):
        main.execute_assistant_action("nonsense", {}, "conv")




@pytest.mark.parametrize(
    "message",
    [
        "let me talk to you",
        "talk to me",
        "can I talk to you",
        "i want to talk to you adam",
        "let's talk",
        "hello",
        "hi adam",
    ],
)
def test_fallback_talk_requests_are_chat_not_calls(message):
    plan = main._assistant_fallback_plan(message)
    assert plan["action"] == "just_chat"
    assert "mic" in plan["args"]["reply"].lower()


@pytest.mark.parametrize(
    "message",
    [
        "call me",
        "let me call you",
        "call him",
        "phone you",
        "call yourself",
    ],
)
def test_fallback_pronoun_call_targets_are_chat_not_calls(message):
    plan = main._assistant_fallback_plan(message)
    assert plan["action"] == "just_chat"


def test_fallback_talk_phrases_do_not_shadow_real_actions():
    assert main._assistant_fallback_plan("show metrics")["action"] == "metrics"
    assert (
        main._assistant_fallback_plan("search XPO carriers")["action"]
        == "search_carriers"
    )


def test_fallback_real_carrier_call_still_works():
    plan = main._assistant_fallback_plan("call carrier 1234567")
    assert plan["action"] == "place_outbound_call"
    assert plan["args"]["usdot_number"] == "1234567"


def test_execute_just_chat_returns_reply_only():
    reply, data = main.execute_assistant_action(
        "just_chat", {"reply": "Press the mic and talk."}, "conv"
    )
    assert reply == "Press the mic and talk."
    assert data == {}
    assert "auto_connect_url" not in str(data)


def test_execute_just_chat_empty_reply_gets_default():
    reply, _ = main.execute_assistant_action("just_chat", {}, "conv")
    assert reply.strip()


def test_execute_pronoun_outbound_call_redirects_to_chat(monkeypatch):
    def _explode(*a, **k):
        raise AssertionError("begin_call must not run for pronoun targets")

    monkeypatch.setattr(main, "begin_call", _explode)

    reply, data = main.execute_assistant_action(
        "place_outbound_call", {"company_name": "me"}, "conv"
    )
    assert "mic" in reply.lower()
    assert data == {}


def test_execute_real_outbound_call_not_affected(monkeypatch):
    monkeypatch.setattr(
        main,
        "begin_call",
        lambda **kwargs: {"session_id": "call_x", "caller_key": "k",
                          "resumed": False},
    )
    reply, data = main.execute_assistant_action(
        "place_outbound_call", {"usdot_number": "1234567"}, "conv"
    )
    assert data["session_id"] == "call_x"
    assert "auto_connect_url" in data


def test_model_planned_just_chat_reaches_endpoint(monkeypatch):
    plan_json = json.dumps(
        {
            "action": "just_chat",
            "args": {"reply": "Sure, press the green mic and talk."},
        }
    )
    monkeypatch.setattr(main, "groq_client", _FakeGroq(plan_json))

    response = _ask("let me talk to you")
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "just_chat"
    assert "mic" in body["reply"].lower()
    assert not body.get("data")


def test_no_destructive_action_exists():
    assert not any(
        word in name
        for name in main.ASSISTANT_ACTIONS
        for word in ("delete", "drop", "truncate", "purge", "reset")
    )
