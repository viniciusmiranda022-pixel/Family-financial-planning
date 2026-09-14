"""Tests for the Python-side Codex `/v1/interpret` authority boundary
(P0 #87, October Go-Live Slice 4).

Mirrors `tests/test_codex_audit.py`'s structure exactly: a deterministic
fake `CodexAdvisorClient` double, never touching the network or a real
Codex/Advisor process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.services.assistant_interpreter import StructuredInterpretation, interpret_message
from app.services.codex_client import CodexResult


def test_assistant_interpreter_and_sanitizer_have_no_database_or_http_server_access() -> None:
    """Same structural isolation guarantee as
    `test_codex_audit_modules_have_no_database_or_http_server_access`: the
    interpretation authority boundary must never gain direct Postgres
    access."""

    for relative_path in (
        "app/services/assistant_interpreter.py",
        "app/services/assistant_sanitizer.py",
    ):
        import_lines = [
            line.strip().lower()
            for line in Path(relative_path).read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert "sqlalchemy" not in line
            assert "app.db" not in line
            assert "app.models" not in line
            assert "app.api" not in line


@dataclass
class FakeCodexClient:
    response: dict | None
    configured_value: bool = True
    captured_payload: dict | None = field(default=None, init=False)

    @property
    def configured(self) -> bool:
        return self.configured_value

    def interpret(self, payload: dict) -> CodexResult:
        self.captured_payload = payload
        if self.response is None:
            return CodexResult(None, "Serviço Codex indisponível: fake")
        return CodexResult(self.response)


class RaisingCodexClient:
    configured = True

    def interpret(self, payload: dict) -> CodexResult:
        raise ValueError("invalid advisor URL")


def test_codex_disabled_returns_unavailable_without_calling_the_client() -> None:
    client = FakeCodexClient(response={"intent": "create_expense"}, configured_value=False)
    outcome = interpret_message(message="Gastei 300", client=client)
    assert outcome.available is False
    assert outcome.reason == "codex_disabled"
    assert client.captured_payload is None


def test_success_response_is_parsed_into_structured_interpretation() -> None:
    client = FakeCodexClient(
        response={
            "schema_version": "1.0.0",
            "intent": "create_expense",
            "extracted_fields": {
                "amount_text": "300",
                "description": "combustível",
                "account_hint": "Nubank",
            },
            "missing_fields": [],
            "clarifying_question": None,
            "confidence": 0.9,
            "model": "modelo-de-teste",
        }
    )
    outcome = interpret_message(message="Gastei 300 de combustível no Nubank", client=client)
    assert outcome.available is True
    assert outcome.intent == "create_expense"
    assert outcome.extracted_fields["amount_text"] == "300"
    assert outcome.confidence == 0.9
    assert outcome.model == "modelo-de-teste"
    # The sanitized outbound payload never carries a household/account id --
    # only the message, a closed intent vocabulary and history.
    assert set(client.captured_payload.keys()) == {
        "schema_version",
        "known_intents",
        "message",
        "history",
        "trace_id",
    }


def test_transport_error_is_unavailable_never_raises() -> None:
    client = FakeCodexClient(response=None)
    outcome = interpret_message(message="Gastei 300", client=client)
    assert outcome.available is False
    assert outcome.reason == "provider_unreachable"


def test_unexpected_client_exception_is_unavailable_never_raises() -> None:
    outcome = interpret_message(message="Gastei 300", client=RaisingCodexClient())
    assert outcome.available is False
    assert outcome.reason == "provider_unreachable"


def test_unknown_intent_value_is_rejected_as_invalid_schema() -> None:
    client = FakeCodexClient(
        response={
            "intent": "delete_household",
            "extracted_fields": {},
            "missing_fields": [],
            "clarifying_question": None,
            "confidence": 0.9,
        }
    )
    outcome = interpret_message(message="algo", client=client)
    assert outcome.available is False
    assert outcome.reason == "invalid_schema"


def test_authority_shaped_extracted_fields_are_never_read() -> None:
    """Even if the sidecar's own schema validation were bypassed and a
    response smuggled an id-shaped field through, `_coerce_interpretation`
    has no code path that reads it -- only the closed allowlist of hint
    keys survives."""

    client = FakeCodexClient(
        response={
            "intent": "create_expense",
            "extracted_fields": {
                "amount_text": "300",
                "account_id": "real-account-id",
                "confirmed": True,
            },
            "missing_fields": [],
            "clarifying_question": None,
            "confidence": 0.9,
        }
    )
    outcome = interpret_message(message="algo", client=client)
    assert outcome.available is True
    assert "account_id" not in outcome.extracted_fields
    assert "confirmed" not in outcome.extracted_fields


def test_malformed_confidence_type_is_unavailable() -> None:
    client = FakeCodexClient(
        response={
            "intent": "query",
            "extracted_fields": {},
            "missing_fields": [],
            "clarifying_question": None,
            "confidence": "alta",
        }
    )
    outcome = interpret_message(message="algo", client=client)
    assert outcome.available is False
    assert outcome.reason == "invalid_schema"


def test_structured_interpretation_to_dict_round_trips() -> None:
    outcome = StructuredInterpretation(
        available=True,
        reason=None,
        intent="query",
        extracted_fields={"description": "x"},
        missing_fields=("amount",),
        clarifying_question="Qual valor?",
        confidence=0.5,
        model="m",
    )
    as_dict = outcome.to_dict()
    assert as_dict["intent"] == "query"
    assert as_dict["missing_fields"] == ["amount"]
