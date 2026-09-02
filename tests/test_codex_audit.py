"""Tests for the Python-side Codex Semantic Audit authority boundary.

These tests never touch the network or a real Codex/Advisor process: they
use a deterministic fake `CodexAdvisorClient` (mirroring the Node fake
provider in advisor/providers/fakeProvider.mjs), matching
docs/WORK_ORDER_PR6_CODEX_SEMANTIC_AUDIT.md's requirement for a fake
provider/test double that covers success, timeout, error and invalid
schema deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.services.codex_audit import AuditOutcome, run_semantic_audit
from app.services.codex_client import CodexResult


def test_codex_audit_modules_have_no_database_or_http_server_access() -> None:
    """The Advisor sidecar and its Python-side callers must never gain
    direct Postgres access (Work Order item 9). These two modules are the
    entire Python-side Codex Semantic Audit surface, so proving neither one
    imports SQLAlchemy/the app's DB session/ORM models is a direct,
    structural regression guard for that isolation requirement -- mirrors
    `test_integrity_orchestrator_has_no_advisor_or_codex_dependency` in
    tests/test_integrity_engine.py, in the opposite direction."""

    for relative_path in ("app/services/codex_audit.py", "app/services/audit_sanitizer.py"):
        import_lines = [
            line.strip().lower()
            for line in Path(relative_path).read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert "sqlalchemy" not in line
            assert "app.db" not in line
            assert "app.models" not in line


BASE_INTEGRITY_STATUS = {
    "status": "attention",
    "score": 88.5,
    "trusted_for_projection": True,
    "trusted_for_reports": True,
    "open_findings": 0,
}


@dataclass
class FakeCodexClient:
    """Deterministic stand-in for CodexAdvisorClient in these tests."""

    response: dict | None
    configured_value: bool = True
    captured_payload: dict | None = None

    @property
    def configured(self) -> bool:
        return self.configured_value

    def audit(self, payload: dict) -> CodexResult:
        self.captured_payload = payload
        if self.response is None:
            return CodexResult(None, "Serviço Codex indisponível: fake")
        return CodexResult(self.response)


class RaisingCodexClient:
    """Client double for unexpected transport/configuration exceptions."""

    configured = True

    def audit(self, payload: dict) -> CodexResult:
        raise ValueError("invalid advisor URL")


def test_codex_disabled_returns_unavailable_without_calling_the_client() -> None:
    client = FakeCodexClient(response={"available": True}, configured_value=False)
    outcome = run_semantic_audit(
        audit_type="period_review", integrity_status=BASE_INTEGRITY_STATUS, client=client
    )
    assert outcome.available is False
    assert outcome.reason == "codex_disabled"
    assert client.captured_payload is None


def test_success_response_is_parsed_into_observations() -> None:
    client = FakeCodexClient(
        response={
            "available": True,
            "reason": None,
            "schema_version": "1.0.0",
            "summary": "Status attention: nenhuma inconsistência adicional identificada.",
            "confidence": 0.6,
            "observations": [
                {
                    "category": "liquidity",
                    "type": "explanation",
                    "severity": "info",
                    "message": "Explica o déficit já calculado.",
                    "evidence_ref": ["finding-1"],
                    "recommendation": "Nenhuma ação necessária.",
                }
            ],
            "model": "modelo-de-teste",
        }
    )
    outcome = run_semantic_audit(
        audit_type="period_review", integrity_status=BASE_INTEGRITY_STATUS, client=client
    )
    assert outcome.available is True
    assert outcome.reason is None
    assert outcome.confidence == 0.6
    assert outcome.model == "modelo-de-teste"
    assert len(outcome.observations) == 1
    assert outcome.observations[0].category == "liquidity"
    assert outcome.observations[0].severity == "info"


def test_sidecar_reported_unavailable_is_passed_through_as_unavailable() -> None:
    client = FakeCodexClient(response={"available": False, "reason": "timeout"})
    outcome = run_semantic_audit(
        audit_type="period_review", integrity_status=BASE_INTEGRITY_STATUS, client=client
    )
    assert outcome.available is False
    assert outcome.reason == "timeout"


def test_transport_error_is_unavailable_never_raises() -> None:
    client = FakeCodexClient(response=None)
    outcome = run_semantic_audit(
        audit_type="period_review", integrity_status=BASE_INTEGRITY_STATUS, client=client
    )
    assert outcome.available is False
    assert outcome.reason == "provider_unreachable"


def test_unexpected_client_exception_is_unavailable_never_raises() -> None:
    outcome = run_semantic_audit(
        audit_type="period_review",
        integrity_status=BASE_INTEGRITY_STATUS,
        client=RaisingCodexClient(),
    )
    assert outcome.available is False
    assert outcome.reason == "provider_unreachable"


def test_malformed_response_shape_is_unavailable() -> None:
    client = FakeCodexClient(
        response={"available": True, "summary": None, "observations": [], "confidence": 0.5}
    )
    outcome = run_semantic_audit(
        audit_type="period_review", integrity_status=BASE_INTEGRITY_STATUS, client=client
    )
    assert outcome.available is False
    assert outcome.reason == "invalid_schema"


def test_attempt_to_smuggle_a_verdict_field_is_never_read() -> None:
    """Even if a defensive layer upstream were bypassed and a response
    carried status/score/trusted_for_* fields, `_coerce_outcome` never reads
    them: `AuditOutcome` has no such field to put them in."""

    client = FakeCodexClient(
        response={
            "available": True,
            "summary": "Tudo aprovado.",
            "confidence": 1.0,
            "observations": [],
            "status": "pass",
            "score": 100,
            "trusted_for_projection": True,
            "trusted_for_reports": True,
            "findings": [],
        }
    )
    outcome = run_semantic_audit(
        audit_type="period_review", integrity_status=BASE_INTEGRITY_STATUS, client=client
    )
    assert outcome.available is False
    assert outcome.reason == "verdict_contradiction"
    assert not hasattr(outcome, "status")
    assert not hasattr(outcome, "score")
    assert not hasattr(outcome, "trusted_for_projection")
    assert not hasattr(outcome, "trusted_for_reports")
    assert not hasattr(outcome, "findings")
    # AuditOutcome's dataclass fields are exactly this fixed set -- there is
    # no way for the extra keys above to have landed anywhere on it.
    assert {f for f in AuditOutcome.__dataclass_fields__} == {
        "available",
        "reason",
        "summary",
        "observations",
        "confidence",
        "model",
    }


def test_observation_with_disallowed_severity_is_dropped_not_upgraded() -> None:
    client = FakeCodexClient(
        response={
            "available": True,
            "summary": "Status attention: resumo.",
            "confidence": 0.5,
            "observations": [
                {
                    "category": "reconciliation",
                    "type": "observation",
                    "severity": "block",
                    "message": "Tentativa de bloquear por conta própria.",
                },
                {
                    "category": "reconciliation",
                    "type": "observation",
                    "severity": "info",
                    "message": "Observação válida.",
                },
            ],
        }
    )
    outcome = run_semantic_audit(
        audit_type="period_review", integrity_status=BASE_INTEGRITY_STATUS, client=client
    )
    assert outcome.available is True
    assert len(outcome.observations) == 1
    assert outcome.observations[0].message == "Observação válida."


def test_the_payload_sent_to_the_client_never_contains_the_household_id_or_secrets() -> None:
    client = FakeCodexClient(
        response={
            "available": True,
            "summary": "Status attention: ok",
            "confidence": 0.4,
            "observations": [],
        }
    )
    run_semantic_audit(
        audit_type="period_review",
        integrity_status={**BASE_INTEGRITY_STATUS, "database_url": "postgresql://leak"},
        findings=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "invariant_id": "INV-006",
                "check_status": "warning",
                "severity": "warning",
                "message": "Déficit consumiu parte da liquidez disponível.",
            }
        ],
        client=client,
    )
    assert client.captured_payload is not None
    import json

    serialized = json.dumps(client.captured_payload)
    assert "postgresql://" not in serialized
    assert "household" not in serialized.lower()
