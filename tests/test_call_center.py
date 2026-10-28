"""
Web phone call-center tests.

The /call page simulates carrier calls. These tests prove:
- stable caller identity (USDOT > phone > company > person),
- change requests from the "driver" are refused before any model call
  and are flagged, never executed,
- conversations are stored and resumed on callback,
- call code never writes to carrier_leads / carrier_reviews /
  carrier_call_attempts,
- all data endpoints require the admin token.
"""

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)
authed = TestClient(
    main.app, headers={"Authorization": "Bearer test-admin-token"}
)

SESSION_ROW = (
    "call_abc123",      # id
    "usdot:1234567",    # caller_key
    "Rob Miller",       # display_name
    "MLA Logistics",    # company_name
    "1234567",          # usdot_number
    "3125550142",       # phone
    "open",             # status
    None,               # started_at
    None,               # last_activity_at
    None,               # ended_at
    None,               # summary
    None,               # resumed_from
)

ENDED_ROW = SESSION_ROW[:6] + ("ended",) + SESSION_ROW[7:]


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


def _explode(*args, **kwargs):
    raise AssertionError("model must not be called here")


# --------------------------------------------------
# Caller identity
# --------------------------------------------------

def test_caller_key_prefers_usdot():
    key = main.build_caller_key(
        usdot="1234567", phone="(312) 555-0142",
        company="MLA Logistics", person="Rob",
    )
    assert key == "usdot:1234567"


def test_caller_key_phone_falls_back_to_last_ten_digits():
    assert main.build_caller_key(phone="+1 (312) 555-0142") == "phone:3125550142"


def test_caller_key_company_and_person_normalization():
    assert main.build_caller_key(company="  MLA-Logistics  Corp ") == "name:mla logistics corp"
    assert main.build_caller_key(person="Rob Miller") == "person:rob miller"
    assert main.build_caller_key() == ""


def test_change_intent_matches_dynamic():
    cases = [
        "change my rate to $3 a mile",
        "update the phone number on file",
        "delete that record",
        "cancel my load booking",
        "mark me not interested",
        "add me to your database",
    ]
    for message in cases:
        assert main.call_change_intent(message), message


def test_change_intent_ignores_normal_talk():
    for message in (
        "what loads do you have from Texas to Ohio",
        "my rate last month was higher",
        "the pickup is scheduled for Monday",
        "i want to add a reefer trailer next week",
    ):
        assert main.call_change_intent(message) is None, message


def test_summary_uses_last_caller_words():
    summary = main.derive_call_summary(
        [
            {"role": "assistant", "content": "greeting"},
            {"role": "user", "content": "I haul reefer Texas to midwest"},
            {"role": "user", "content": "available Monday"},
        ]
    )
    assert "reefer" in summary
    assert "Monday" in summary
    assert "greeting" not in summary


# --------------------------------------------------
# Refusal before any model/DB-write path
# --------------------------------------------------

def test_refusal_happens_before_model_call(monkeypatch):
    cursor = _install(monkeypatch, _Cursor(fetches=[SESSION_ROW]))
    monkeypatch.setattr(main, "_call_agent_reply", _explode)
    monkeypatch.setattr(main, "groq_client", _explode)

    result = main.handle_call_message(
        "call_abc123", "please change my rate to $4.50"
    )

    assert result["refused"] is True
    assert result["flagged"] is True
    assert result["reply"] == main.CALL_REFUSAL_REPLY
    # Only reads + message inserts happened; nothing else was written.
    every_sql = " ".join(call[0] for call in cursor.calls)
    assert "carrier_leads" not in every_sql
    assert "carrier_reviews" not in every_sql
    assert "carrier_call_attempts" not in every_sql
    flagged_insert = [
        call for call in cursor.calls
        if "INSERT INTO public.call_messages" in call[0]
    ]
    assert any(call[1][2] is True for call in flagged_insert)


def test_change_orders_never_write_carrier_tables(monkeypatch):
    cursor = _install(monkeypatch, _Cursor(fetches=[SESSION_ROW]))
    monkeypatch.setattr(main, "groq_client", _explode)

    main.handle_call_message(
        "call_abc123", "delete my record from your system"
    )

    every_sql = " ".join(call[0] for call in cursor.calls)
    for table in ("carrier_leads", "carrier_reviews", "carrier_call_attempts"):
        assert table not in every_sql


# --------------------------------------------------
# begin_call: new vs resumed
# --------------------------------------------------

def test_begin_call_new_session(monkeypatch):
    cursor = _install(
        monkeypatch,
        _Cursor(
            fetches=[
                None,          # no previous call
                SESSION_ROW,   # the inserted session
            ],
            rows=[],           # transcript empty
        ),
    )

    result = main.begin_call(
        usdot="1234567", company="MLA Logistics", person="Rob Miller"
    )

    assert result["resumed"] is False
    assert result["resumed_from"] is None
    assert result["caller_key"] == "usdot:1234567"
    assert "Thanks for calling with MLA Logistics" in result["greeting"]
    insert_sql = cursor.calls[1][0]
    assert "INSERT INTO public.call_sessions" in insert_sql
    # Identity clauses searched the history table with the USDOT first.
    assert cursor.calls[0][1][0] == "1234567"


def test_begin_call_resumes_ended_history(monkeypatch):
    previous = dict(
        zip(
            (
                "id", "caller_key", "display_name", "company_name",
                "usdot_number", "phone", "status", "started_at",
                "last_activity_at", "ended_at", "summary", "resumed_from",
            ),
            ENDED_ROW,
        )
    )
    # No stored summary this time -> one is derived from the old transcript.
    new_row = ("call_new",) + ENDED_ROW[1:6] + ("open",) + ENDED_ROW[7:]

    cursor = _install(
        monkeypatch,
        _Cursor(
            fetches=[
                ENDED_ROW,   # previous call found
                new_row,     # the inserted (resumed) session
            ],
            rows=[("user", "talked reefer lanes", False, None)],
        ),
    )

    result = main.begin_call(usdot="1234567")

    assert result["resumed"] is True
    assert result["resumed_from"] == "call_abc123"
    assert "Welcome back" in result["greeting"]
    # The greeting picks up where the last conversation left off.
    assert "talked reefer lanes" in result["greeting"]
    assert any(
        "reefer" in item["content"] for item in result["transcript"]
    )


def test_begin_call_no_identity_is_400(monkeypatch):
    _install(monkeypatch, _Cursor())
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        main.begin_call()
    assert err.value.status_code == 400


# --------------------------------------------------
# Message path storage
# --------------------------------------------------

def test_message_stores_user_and_assistant_rows(monkeypatch):
    cursor = _install(monkeypatch, _Cursor(fetches=[SESSION_ROW]))
    monkeypatch.setattr(
        main, "_call_agent_reply",
        lambda session, transcript, message: "Sure, tell me your lane.",
    )

    result = main.handle_call_message("call_abc123", "I haul reefer")

    assert result["reply"] == "Sure, tell me your lane."
    assert result["refused"] is False
    inserts = [
        call for call in cursor.calls
        if "INSERT INTO public.call_messages" in call[0]
    ]
    assert len(inserts) == 2
    assert inserts[0][1][1] == "I haul reefer"
    assert inserts[0][1][2] is False
    assert inserts[1][1][1] == "Sure, tell me your lane."
    assert any(
        "UPDATE public.call_sessions" in call[0] for call in cursor.calls
    )


def test_message_unknown_session_is_404(monkeypatch):
    _install(monkeypatch, _Cursor(fetches=[None]))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        main.handle_call_message("call_missing", "hello")
    assert err.value.status_code == 404


def test_message_on_ended_session_is_409(monkeypatch):
    _install(monkeypatch, _Cursor(fetches=[ENDED_ROW]))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        main.handle_call_message("call_abc123", "hello")
    assert err.value.status_code == 409


def test_empty_message_is_rejected():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        main.handle_call_message("x", "   ")
    assert err.value.status_code == 400


# --------------------------------------------------
# end_call
# --------------------------------------------------

def test_end_call_sets_ended_and_summary(monkeypatch):
    ended = ENDED_ROW[:10] + ("I haul reefer", None)
    cursor = _install(
        monkeypatch,
        _Cursor(
            fetches=[SESSION_ROW, ended],
            rows=[("user", "I haul reefer", False, None)],
        ),
    )

    result = main.end_call("call_abc123")

    assert result["success"] is True
    assert result["summary"] == "I haul reefer"
    update_sql = cursor.calls[2][0]
    assert "status = 'ended'" in update_sql
    assert "ended_at = CURRENT_TIMESTAMP" in update_sql


def test_end_call_unknown_session_is_404(monkeypatch):
    _install(monkeypatch, _Cursor(fetches=[None]))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        main.end_call("call_missing")
    assert err.value.status_code == 404


# --------------------------------------------------
# HTTP endpoints & auth
# --------------------------------------------------

def test_call_endpoints_require_admin_token():
    assert client.post(
        "/call-sessions", json={"usdot_number": "1"}
    ).status_code == 401
    assert client.post(
        "/call-sessions/x/messages", json={"message": "hi"}
    ).status_code == 401
    assert client.get("/call-sessions/x").status_code == 401
    assert client.post("/call-sessions/x/end").status_code == 401
    assert client.get("/call-sessions").status_code == 401


def test_call_page_is_public():
    assert client.get("/call").status_code == 200


def test_start_call_endpoint(monkeypatch):
    captured = {}

    def fake_begin(**kwargs):
        captured.update(kwargs)
        return {"session_id": "call_x", "resumed": False}

    monkeypatch.setattr(main, "begin_call", fake_begin)

    response = authed.post(
        "/call-sessions",
        json={
            "usdot_number": "1234567",
            "company_name": "MLA Logistics",
            "caller_name": "Rob Miller",
        },
    )

    assert response.status_code == 200
    assert captured["usdot"] == "1234567"
    assert captured["company"] == "MLA Logistics"
    assert captured["person"] == "Rob Miller"


def test_message_endpoint(monkeypatch):
    monkeypatch.setattr(
        main, "handle_call_message",
        lambda session_id, message: {
            "session_id": session_id, "reply": "ok",
            "refused": False, "flagged": False, "intent": None,
        },
    )
    body = authed.post(
        "/call-sessions/call_x/messages", json={"message": "hi"}
    ).json()
    assert body["reply"] == "ok"


def test_end_endpoint(monkeypatch):
    monkeypatch.setattr(
        main, "end_call",
        lambda session_id: {"success": True, "session": {}, "summary": "s"},
    )
    assert authed.post("/call-sessions/call_x/end").json()["success"] is True


def test_list_endpoint(monkeypatch):
    monkeypatch.setattr(
        main, "list_call_sessions", lambda limit=20: [dict(zip(
            (
                "id", "caller_key", "display_name", "company_name",
                "usdot_number", "phone", "status", "started_at",
                "last_activity_at", "ended_at", "summary", "resumed_from",
            ),
            ENDED_ROW,
        ))]
    )
    body = authed.get("/call-sessions").json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["id"] == "call_abc123"


def test_message_endpoint_rejects_empty():
    response = authed.post(
        "/call-sessions/call_x/messages", json={"message": ""}
    )
    assert response.status_code == 422


def test_conversation_reply_never_writes_carrier_tables(monkeypatch):
    """The agent reply path only reads transcripts — it cannot write."""
    cursor = _install(
        monkeypatch,
        _Cursor(
            fetches=[SESSION_ROW],
            rows=[("assistant", "hi", False, None)],
        ),
    )
    monkeypatch.setattr(main, "groq_client", _explode)

    # Model raising is caught inside _call_agent_reply -> fallback text.
    result = main.handle_call_message("call_abc123", "what loads you got")

    assert "can't change any records" in result["reply"].lower()
    every_sql = " ".join(call[0] for call in cursor.calls)
    for table in ("carrier_leads", "carrier_reviews", "carrier_call_attempts"):
        assert table not in every_sql


# --------------------------------------------------
# Operator-initiated outbound call (auto-open receiver page)
# --------------------------------------------------

def test_outbound_endpoint_returns_auto_connect_url(monkeypatch):
    monkeypatch.setattr(
        main, "begin_call",
        lambda usdot="", phone="", company="", person="": {
            "session_id": "call_out1", "caller_key": "usdot:1",
            "resumed": False,
        },
    )

    body = authed.post(
        "/call-sessions/outbound",
        json={"usdot_number": "1", "company_name": "ACME"},
    ).json()

    assert body["auto_connect_url"] == "/call?session_id=call_out1&auto=1"
    assert body["session_id"] == "call_out1"


def test_outbound_endpoint_requires_admin_token():
    assert client.post(
        "/call-sessions/outbound", json={"company_name": "x"}
    ).status_code == 401


def test_fallback_parser_call_carrier_by_usdot():
    plan = main._assistant_fallback_plan("call carrier 1234567")
    assert plan["action"] == "place_outbound_call"
    assert plan["args"]["usdot_number"] == "1234567"


def test_fallback_parser_call_carrier_by_company():
    plan = main._assistant_fallback_plan("call carrier MLA Logistics")
    assert plan["action"] == "place_outbound_call"
    assert plan["args"]["company_name"] == "MLA Logistics"


def test_fallback_parser_call_carrier_by_phone():
    plan = main._assistant_fallback_plan("call carrier 312-555-0142")
    assert plan["action"] == "place_outbound_call"
    assert plan["args"]["phone"] == "312-555-0142"


def test_assistant_calls_carrier_action(monkeypatch):
    """The assistant executes place_outbound_call and returns an auto URL."""
    monkeypatch.setattr(
        main,
        "plan_assistant_action",
        lambda message: {
            "action": "place_outbound_call",
            "args": {"usdot_number": "1234567"},
        },
    )
    monkeypatch.setattr(
        main, "begin_call",
        lambda usdot="", phone="", company="", person="": {
            "session_id": "call_as1", "caller_key": "usdot:1234567",
            "resumed": False, "greeting": "g", "transcript": [],
        },
    )

    body = authed.post(
        "/assistant", json={"message": "call carrier 1234567"}
    ).json()

    assert body["action"] == "place_outbound_call"
    assert body["data"]["auto_connect_url"] == "/call?session_id=call_as1&auto=1"
    assert "Opening call console" in body["reply"]


def test_real_dialing_phrasings_still_refused():
    for message in ("dial the carrier", "phone the carrier"):
        assert main._assistant_refusal_reason(message) is not None
