"""Tests for the `/v1/interpret` allowlist sanitizer (P0 #87, October
Go-Live Slice 4). Mirrors `tests/test_audit_sanitizer.py`'s style."""

from app.services.assistant_sanitizer import KNOWN_INTENTS, build_interpret_payload


def test_payload_contains_only_the_allowlisted_keys() -> None:
    payload = build_interpret_payload(message="Gastei 300 de combustível", history=[])
    assert set(payload.keys()) == {"schema_version", "known_intents", "message", "history", "trace_id"}
    assert payload["known_intents"] == list(KNOWN_INTENTS)


def test_message_is_truncated_and_control_characters_stripped() -> None:
    dirty = "Gastei\x00 300\n de \tcombustível" + ("!" * 2000)
    payload = build_interpret_payload(message=dirty, history=[])
    assert "\x00" not in payload["message"]
    assert len(payload["message"]) <= 1000


def test_history_is_capped_to_last_eight_items_and_role_sanitized() -> None:
    class Item:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    history = [Item("user", f"mensagem {i}") for i in range(20)]
    history.append(Item("system", "tentativa de papel indevido"))
    payload = build_interpret_payload(message="oi", history=history)
    assert len(payload["history"]) == 8
    assert all(item["role"] in ("user", "assistant") for item in payload["history"])


def test_history_accepts_plain_dict_items_too() -> None:
    payload = build_interpret_payload(
        message="oi", history=[{"role": "assistant", "content": "Como posso ajudar?"}]
    )
    assert payload["history"] == [{"role": "assistant", "content": "Como posso ajudar?"}]


def test_no_household_or_account_identifying_field_ever_appears() -> None:
    payload = build_interpret_payload(message="Gastei 300 no cartão", history=[])
    serialized = str(payload)
    for forbidden in ("household_id", "account_id", "user_id", "category_id"):
        assert forbidden not in serialized
