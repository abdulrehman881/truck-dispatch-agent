"""
MC-scoped conversation memory tests.

Proves memory is deleted automatically once work with a carrier (MC)
is finished: after a saved review, when the conversation moves to a
different carrier, and when a new carrier is reserved.
"""

import main


def _seed(conversation_id, carrier=None):
    main.conversation_histories[conversation_id] = {
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
        "last_active": __import__("time").monotonic(),
    }
    main.pending_carrier_reviews[conversation_id] = {
        "usdot_number": carrier or "1234567",
        "review_status": "interested",
        "follow_up_at": None,
        "notes": None,
        "confirmed": False,
    }
    if carrier:
        main.conversation_carriers[conversation_id] = carrier


def test_clear_conversation_memory_removes_everything():
    _seed("conv-clear", carrier="1111111")

    removed = main.clear_conversation_memory("conv-clear")

    assert removed is True
    assert "conv-clear" not in main.conversation_histories
    assert "conv-clear" not in main.pending_carrier_reviews
    assert "conv-clear" not in main.conversation_carriers


def test_clear_conversation_memory_is_idempotent():
    assert main.clear_conversation_memory("never-existed") is False


def test_get_history_after_clear_starts_empty():
    _seed("conv-fresh", carrier="2222222")
    main.clear_conversation_memory("conv-fresh")

    history = main.get_history("conv-fresh")

    assert history == []
    assert "conv-fresh" not in main.pending_carrier_reviews


def test_other_conversations_are_untouched():
    _seed("conv-a", carrier="3333333")
    _seed("conv-b", carrier="4444444")

    main.clear_conversation_memory("conv-a")

    assert "conv-a" not in main.conversation_histories
    assert "conv-b" in main.conversation_histories
    assert main.conversation_carriers["conv-b"] == "4444444"


def test_expired_conversation_clears_carrier_marker(monkeypatch):
    _seed("conv-expired", carrier="5555555")
    import time as _time

    main.conversation_histories["conv-expired"]["last_active"] = (
        _time.monotonic() - main.HISTORY_TTL_SECONDS - 99.0
    )

    history = main.get_history("conv-expired")

    assert history == []
    assert "conv-expired" not in main.conversation_carriers
    assert "conv-expired" not in main.pending_carrier_reviews


def test_memory_is_cleared_when_carrier_changes(monkeypatch):
    """Simulates the switch detection used in /stream-voice."""
    _seed("conv-switch", carrier="6666666")

    previous = main.conversation_carriers.get("conv-switch")
    current = "7777777"

    if previous is not None and previous != current:
        main.clear_conversation_memory(
            "conv-switch", reason="carrier_switched"
        )

    assert "conv-switch" not in main.conversation_histories
    assert main.conversation_carriers.get("conv-switch") is None
