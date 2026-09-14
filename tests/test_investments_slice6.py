"""Tests for October Go-Live Slice 6 (P0 #87) -- Patrimônio, investimentos
e Studio.

Work Order: `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_6.md`.

Section 1 unit-tests `app.services.investments` directly (no DB, no HTTP):
the derived gain/return calculations and the canonical `net_worth_summary`
aggregation -- proving the rebaseline §16.2 invariant ("patrimônio atual usa
somente o valor de hoje") holds by construction.

Section 2 is an HTTP-level functional suite for `POST /investments`,
`POST /investments/{id}/valuations`, `POST /investments/{id}/contributions`
and the manual undo endpoint, mirroring `tests/test_obligation_lifecycle_slice3.py`'s
`_client()`/`_setup_household()` pattern.

Section 3 unit-tests `app.services.assistant_actions.build_typed_action_proposal`
for `update_asset_value`/`register_asset_contribution` directly against a
seeded in-memory SQLite session, mirroring `tests/test_assistant_slice4.py`'s
Section 1.

Section 4 is an HTTP-level functional suite for the Assistant's
`interpret -> execute -> undo` contract applied to the two new typed
actions, plus the Dashboard regression proving `noncanonical.net_worth`
never sums historical cost or projected value.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-investments-slice6-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "investments-slice6-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Household, Investment, InvestmentValuation, User  # noqa: E402
from app.services.assistant_actions import (  # noqa: E402
    build_typed_action_proposal,
    persist_action_proposal,
)
from app.services.assistant_interpreter import StructuredInterpretation  # noqa: E402
from app.services.investments import (  # noqa: E402
    investment_gain_current,
    investment_gain_projected,
    investment_return_current_pct,
    investment_return_projected_pct,
    net_worth_summary,
    serialize_investment,
)
from tests.fixtures.mfa_enrollment import complete_mfa_enrollment  # noqa: E402

# ---------------------------------------------------------------------------
# 1. Direct, DB-free unit tests for the derived calculations.
# ---------------------------------------------------------------------------


class _FakeInvestment:
    def __init__(self, historical_cost, current_value, expected_receivable_value=None):
        self.historical_cost = Decimal(historical_cost)
        self.current_value = Decimal(current_value)
        self.expected_receivable_value = (
            Decimal(expected_receivable_value) if expected_receivable_value is not None else None
        )


def test_gain_and_return_are_derived_from_current_value_only() -> None:
    studio = _FakeInvestment("30000.00", "35000.00", "45000.00")
    assert investment_gain_current(studio) == Decimal("5000.00")
    assert investment_return_current_pct(studio) == Decimal("0.1667")
    assert investment_gain_projected(studio) == Decimal("15000.00")
    assert investment_return_projected_pct(studio) == Decimal("0.5000")


def test_percent_calculations_never_divide_by_zero_cost_basis() -> None:
    # Work Order "Testes obrigatórios": "percentuais/ganhos tratam custo
    # zero/nulo sem divisão inválida" -- never a ZeroDivisionError, never a
    # fabricated 0/100%.
    gifted_asset = _FakeInvestment("0.00", "10000.00")
    assert investment_gain_current(gifted_asset) == Decimal("10000.00")
    assert investment_return_current_pct(gifted_asset) is None
    assert investment_gain_projected(gifted_asset) is None
    assert investment_return_projected_pct(gifted_asset) is None

    negative_cost_defensive = _FakeInvestment("-100.00", "10000.00")
    assert investment_return_current_pct(negative_cost_defensive) is None


def test_no_projection_yet_returns_none_never_zero_or_current_gain() -> None:
    no_projection_yet = _FakeInvestment("30000.00", "35000.00")
    assert no_projection_yet.expected_receivable_value is None
    assert investment_gain_projected(no_projection_yet) is None
    assert investment_return_projected_pct(no_projection_yet) is None


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return session_factory


def test_net_worth_summary_sums_current_value_only_never_historical_or_projected() -> None:
    session_factory = _session_factory()
    with session_factory() as db:
        household = Household(name="Família Patrimônio")
        db.add(household)
        db.flush()
        studio = Investment(
            household_id=household.id,
            name="Studio",
            historical_cost=Decimal("30000.00"),
            current_value=Decimal("35000.00"),
            expected_receivable_value=Decimal("45000.00"),
            last_updated_at=date(2026, 9, 1),
        )
        other_asset = Investment(
            household_id=household.id,
            name="Outro ativo",
            historical_cost=Decimal("10000.00"),
            current_value=Decimal("8000.00"),
            last_updated_at=date(2026, 9, 1),
        )
        inactive_asset = Investment(
            household_id=household.id,
            name="Ativo desativado",
            historical_cost=Decimal("100000.00"),
            current_value=Decimal("100000.00"),
            last_updated_at=date(2026, 9, 1),
            active=False,
        )
        db.add_all([studio, other_asset, inactive_asset])
        db.commit()

        summary = net_worth_summary(db, household_id=household.id)
        # Exactly current_value(Studio) + current_value(other_asset); never
        # historical_cost, never expected_receivable_value, and the
        # inactive asset is excluded entirely.
        assert summary["total_current_value"] == Decimal("43000.00")
        names = {row["name"] for row in summary["investments"]}
        assert names == {"Studio", "Outro ativo"}

        # The forbidden sum (rebaseline §16.2) must never equal the reported
        # total by coincidence of this fixture's numbers.
        forbidden_sum = sum(
            (item.historical_cost + item.current_value + (item.expected_receivable_value or Decimal("0")))
            for item in (studio, other_asset)
        )
        assert summary["total_current_value"] != forbidden_sum


def test_serialize_investment_matches_service_level_derivations() -> None:
    session_factory = _session_factory()
    with session_factory() as db:
        household = Household(name="Família Serialização")
        db.add(household)
        db.flush()
        studio = Investment(
            household_id=household.id,
            name="Studio",
            historical_cost=Decimal("30000.00"),
            current_value=Decimal("35000.00"),
            expected_receivable_value=Decimal("45000.00"),
            last_updated_at=date(2026, 9, 1),
        )
        db.add(studio)
        db.commit()
        row = serialize_investment(studio)
        assert row["gain_current"] == investment_gain_current(studio)
        assert row["return_current_pct"] == investment_return_current_pct(studio)
        assert row["gain_projected"] == investment_gain_projected(studio)
        assert row["return_projected_pct"] == investment_return_projected_pct(studio)


# ---------------------------------------------------------------------------
# 2. HTTP-level functional suite.
# ---------------------------------------------------------------------------


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    def _override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app), session_factory


def _setup_household(client, *, username="admin-investments-slice6"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Investimentos HTTP",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text
    complete_mfa_enrollment(client)


def test_create_investment_stores_three_distinct_values_and_first_valuation() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/investments",
            json={
                "name": "Studio",
                "historical_cost": "30000.00",
                "current_value": "35000.00",
                "expected_receivable_value": "45000.00",
                "valuation_date": "2026-09-01",
            },
        )
        assert created.status_code == 201, created.text
        investment_id = created.json()["id"]

        listing = client.get("/api/investments")
        assert listing.status_code == 200, listing.text
        body = listing.json()
        assert body["total_current_value"] == 35000.0
        row = next(item for item in body["investments"] if item["id"] == investment_id)
        assert row["historical_cost"] == 30000.0
        assert row["current_value"] == 35000.0
        assert row["expected_receivable_value"] == 45000.0
        assert row["gain_current"] == 5000.0

        valuations = client.get(f"/api/investments/{investment_id}/valuations")
        assert valuations.status_code == 200, valuations.text
        assert len(valuations.json()) == 1
        assert valuations.json()[0]["current_value"] == 35000.0


def test_updating_expected_receivable_value_never_changes_net_worth() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/investments",
            json={
                "name": "Studio",
                "historical_cost": "30000.00",
                "current_value": "35000.00",
                "valuation_date": "2026-09-01",
            },
        )
        investment_id = created.json()["id"]

        before = client.get("/api/investments").json()["total_current_value"]
        updated = client.post(
            f"/api/investments/{investment_id}/valuations",
            json={"expected_receivable_value": "45000.00", "valuation_date": "2026-09-15"},
        )
        assert updated.status_code == 201, updated.text
        after = client.get("/api/investments").json()["total_current_value"]

        # Rebaseline §16.3: "A previsão agora é receber 45 mil" never alters
        # patrimônio atual.
        assert after == before == 35000.0
        row = next(
            item for item in client.get("/api/investments").json()["investments"] if item["id"] == investment_id
        )
        assert row["expected_receivable_value"] == 45000.0
        assert row["current_value"] == 35000.0


def test_updating_current_value_changes_net_worth_and_preserves_history() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/investments",
            json={
                "name": "Studio",
                "historical_cost": "30000.00",
                "current_value": "35000.00",
                "valuation_date": "2026-09-01",
            },
        )
        investment_id = created.json()["id"]

        updated = client.post(
            f"/api/investments/{investment_id}/valuations",
            json={"current_value": "38000.00", "valuation_date": "2026-09-15"},
        )
        assert updated.status_code == 201, updated.text

        total = client.get("/api/investments").json()["total_current_value"]
        assert total == 38000.0

        # The prior valuation (35000.00) is preserved, never rewritten --
        # rebaseline "manter histórico de avaliações".
        valuations = client.get(f"/api/investments/{investment_id}/valuations").json()
        assert len(valuations) == 2
        values_seen = sorted(item["current_value"] for item in valuations)
        assert values_seen == [35000.0, 38000.0]


def test_contribution_increases_historical_cost_without_double_counting_net_worth() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/investments",
            json={
                "name": "Studio",
                "historical_cost": "30000.00",
                "current_value": "35000.00",
                "valuation_date": "2026-09-01",
            },
        )
        investment_id = created.json()["id"]

        before_net_worth = client.get("/api/investments").json()["total_current_value"]
        contribution = client.post(
            f"/api/investments/{investment_id}/contributions",
            json={"contribution_amount": "5000.00", "valuation_date": "2026-09-20"},
        )
        assert contribution.status_code == 201, contribution.text
        after_net_worth = client.get("/api/investments").json()["total_current_value"]

        # Rebaseline §16.3: "Coloquei mais 5 mil no Studio" changes cost
        # basis only -- patrimônio atual is untouched until a separate
        # valuation confirms a new current_value.
        assert after_net_worth == before_net_worth == 35000.0
        row = next(
            item for item in client.get("/api/investments").json()["investments"] if item["id"] == investment_id
        )
        assert row["historical_cost"] == 35000.0


def test_contribution_with_funding_transaction_is_not_a_second_expense() -> None:
    """Work Order: "aporte não é classificado automaticamente como gasto de
    consumo". Links to an already-existing "Transferência patrimonial"
    cash-out transaction (created exactly like a human would via the
    existing manual-entry form) -- never fabricates it, never counts it as
    an expense."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        account = client.post("/api/accounts", json={"name": "Conta Corrente", "account_type": "checking"})
        account_id = account.json()["id"]

        funding = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-20",
                "description": "Aporte no Studio",
                "amount": "5000.00",
                "movement_type": "investment",
                "account_id": account_id,
                "confirmed_large_amount": True,
            },
        )
        assert funding.status_code == 201, funding.text
        funding_transaction_id = funding.json()["id"]

        created = client.post(
            "/api/investments",
            json={
                "name": "Studio",
                "historical_cost": "30000.00",
                "current_value": "35000.00",
                "valuation_date": "2026-09-01",
            },
        )
        investment_id = created.json()["id"]

        contribution = client.post(
            f"/api/investments/{investment_id}/contributions",
            json={
                "contribution_amount": "5000.00",
                "valuation_date": "2026-09-20",
                "funding_transaction_id": funding_transaction_id,
            },
        )
        assert contribution.status_code == 201, contribution.text
        assert contribution.json()["funding_transaction_id"] == funding_transaction_id

        with session_factory() as db:
            household_id = db.scalar(select(User.household_id))
            all_expenses = list(
                db.scalars(select(InvestmentValuation).where(InvestmentValuation.household_id == household_id))
            )
            # Exactly two rows: the creation snapshot and the contribution --
            # never a third row standing in for a fabricated expense.
            assert len(all_expenses) == 2

        # Linking the same funding transaction to a second contribution is
        # refused -- it is not a reusable source of cash.
        second_investment = client.post(
            "/api/investments",
            json={
                "name": "Outro ativo",
                "historical_cost": "0.00",
                "current_value": "0.00",
                "valuation_date": "2026-09-01",
            },
        )
        second_id = second_investment.json()["id"]
        double_link = client.post(
            f"/api/investments/{second_id}/contributions",
            json={
                "contribution_amount": "5000.00",
                "valuation_date": "2026-09-20",
                "funding_transaction_id": funding_transaction_id,
            },
        )
        assert double_link.status_code == 409, double_link.text


def test_undo_valuation_restores_prior_state_without_deleting_history() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/investments",
            json={
                "name": "Studio",
                "historical_cost": "30000.00",
                "current_value": "35000.00",
                "valuation_date": "2026-09-01",
            },
        )
        investment_id = created.json()["id"]

        updated = client.post(
            f"/api/investments/{investment_id}/valuations",
            json={"current_value": "38000.00", "valuation_date": "2026-09-15"},
        )
        valuation_id = updated.json()["valuation_id"]

        undo = client.post(
            f"/api/investments/{investment_id}/valuations/{valuation_id}/undo",
            json={"reason": "Digitei o valor errado"},
        )
        assert undo.status_code == 200, undo.text

        row = next(
            item for item in client.get("/api/investments").json()["investments"] if item["id"] == investment_id
        )
        assert row["current_value"] == 35000.0

        # The invalidated row is preserved, never deleted.
        valuations = client.get(f"/api/investments/{investment_id}/valuations").json()
        assert len(valuations) == 2
        invalidated = next(item for item in valuations if item["id"] == valuation_id)
        assert invalidated["invalidated_at"] is not None

        # Undoing the same valuation twice is refused.
        redo = client.post(
            f"/api/investments/{investment_id}/valuations/{valuation_id}/undo",
            json={"reason": "Tentando de novo"},
        )
        assert redo.status_code == 409, redo.text


def test_undo_refuses_when_a_later_valuation_already_exists() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/investments",
            json={
                "name": "Studio",
                "historical_cost": "30000.00",
                "current_value": "35000.00",
                "valuation_date": "2026-09-01",
            },
        )
        investment_id = created.json()["id"]
        first_update = client.post(
            f"/api/investments/{investment_id}/valuations",
            json={"current_value": "38000.00", "valuation_date": "2026-09-15"},
        )
        first_valuation_id = first_update.json()["valuation_id"]
        client.post(
            f"/api/investments/{investment_id}/valuations",
            json={"current_value": "40000.00", "valuation_date": "2026-09-20"},
        )

        undo_stale = client.post(
            f"/api/investments/{investment_id}/valuations/{first_valuation_id}/undo",
            json={"reason": "Tarde demais"},
        )
        assert undo_stale.status_code == 409, undo_stale.text
        row = next(
            item for item in client.get("/api/investments").json()["investments"] if item["id"] == investment_id
        )
        assert row["current_value"] == 40000.0


def test_household_isolation_returns_404_for_a_foreign_investment() -> None:
    first_client, _ = _client()
    with first_client:
        _setup_household(first_client, username="admin-household-a")
        created = first_client.post(
            "/api/investments",
            json={
                "name": "Studio",
                "historical_cost": "30000.00",
                "current_value": "35000.00",
                "valuation_date": "2026-09-01",
            },
        )
        investment_id = created.json()["id"]

    second_client, _ = _client()
    with second_client:
        _setup_household(second_client, username="admin-household-b")
        leak = second_client.post(
            f"/api/investments/{investment_id}/valuations",
            json={"current_value": "1.00", "valuation_date": "2026-09-01"},
        )
        assert leak.status_code == 404, leak.text


# ---------------------------------------------------------------------------
# 3. Assistant proposal-building unit tests (no Codex, no HTTP).
# ---------------------------------------------------------------------------


def _household(session_factory, name="Família Assistente Investimentos") -> str:
    with session_factory() as db:
        household = Household(name=name)
        db.add(household)
        db.commit()
        return household.id


def _investment(session_factory, *, household_id, name="Studio", current_value="35000.00") -> str:
    with session_factory() as db:
        investment = Investment(
            household_id=household_id,
            name=name,
            historical_cost=Decimal("30000.00"),
            current_value=Decimal(current_value),
            last_updated_at=date(2026, 9, 1),
        )
        db.add(investment)
        db.commit()
        return investment.id


def _interpretation(intent: str, **fields: str) -> StructuredInterpretation:
    return StructuredInterpretation(
        available=True,
        reason=None,
        intent=intent,
        extracted_fields=fields,
        missing_fields=(),
        clarifying_question=None,
        confidence=0.9,
    )


def test_update_asset_value_asks_which_field_when_kind_is_unspecified() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _investment(session_factory, household_id=household_id)

    with session_factory() as db:
        proposal = build_typed_action_proposal(
            db,
            household_id=household_id,
            interpretation=_interpretation(
                "update_asset_value", target_hint="Studio", amount_text="35000"
            ),
        )
    assert proposal.can_execute is False
    assert "valor de hoje" in proposal.clarifying_question.lower()


def test_update_asset_value_current_value_resolves_when_kind_is_given() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    investment_id = _investment(session_factory, household_id=household_id)

    with session_factory() as db:
        proposal = build_typed_action_proposal(
            db,
            household_id=household_id,
            interpretation=_interpretation(
                "update_asset_value",
                target_hint="Studio",
                amount_text="35000",
                asset_value_kind_hint="current_value",
            ),
        )
    assert proposal.can_execute is True
    assert proposal.typed_action == "update_asset_value"
    assert proposal.path_params == {"investment_id": investment_id}
    assert proposal.payload["current_value"] == "35000"
    assert "expected_receivable_value" not in proposal.payload


def test_update_asset_value_expected_receivable_never_touches_current_value() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _investment(session_factory, household_id=household_id)

    with session_factory() as db:
        proposal = build_typed_action_proposal(
            db,
            household_id=household_id,
            interpretation=_interpretation(
                "update_asset_value",
                target_hint="Studio",
                amount_text="45000",
                asset_value_kind_hint="expected_receivable_value",
            ),
        )
    assert proposal.can_execute is True
    assert proposal.payload["expected_receivable_value"] == "45000"
    assert "current_value" not in proposal.payload


def test_register_asset_contribution_asks_for_origin_when_no_funding_transaction_matches() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _investment(session_factory, household_id=household_id)

    with session_factory() as db:
        proposal = build_typed_action_proposal(
            db,
            household_id=household_id,
            interpretation=_interpretation(
                "register_asset_contribution", target_hint="Studio", amount_text="5000"
            ),
        )
    assert proposal.can_execute is False
    assert "de onde saiu" in proposal.clarifying_question.lower()


def test_register_asset_contribution_resolves_with_one_matching_funding_transaction() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    investment_id = _investment(session_factory, household_id=household_id)

    with session_factory() as db:
        account = Account(household_id=household_id, name="Conta Corrente", account_type="checking")
        db.add(account)
        db.flush()
        from app.api import _create_manual_transaction_impl
        from app.schemas import ManualTransactionRequest

        admin = User(
            household_id=household_id,
            name="Admin",
            username="admin-contrib",
            password_hash="x",
            is_admin=True,
        )
        db.add(admin)
        db.flush()
        _create_manual_transaction_impl(
            ManualTransactionRequest(
                booked_at=date(2026, 9, 20),
                description="Aporte no Studio",
                amount=Decimal("5000.00"),
                movement_type="investment",
                account_id=account.id,
                confirmed_large_amount=True,
            ),
            admin,
            db,
        )

    with session_factory() as db:
        proposal = build_typed_action_proposal(
            db,
            household_id=household_id,
            interpretation=_interpretation(
                "register_asset_contribution", target_hint="Studio", amount_text="5000"
            ),
        )
    assert proposal.can_execute is True
    assert proposal.typed_action == "register_asset_contribution"
    assert proposal.path_params == {"investment_id": investment_id}
    assert proposal.payload["funding_transaction_id"]


# ---------------------------------------------------------------------------
# 4. Assistant HTTP execute/undo/idempotency + Dashboard regression.
# ---------------------------------------------------------------------------


def test_execute_update_asset_value_via_assistant_is_audited_and_undoable() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/investments",
            json={
                "name": "Studio",
                "historical_cost": "30000.00",
                "current_value": "35000.00",
                "valuation_date": "2026-09-01",
            },
        )
        investment_id = created.json()["id"]

        with session_factory() as db:
            household_id = db.scalar(select(User.household_id))
            interpretation = StructuredInterpretation(
                available=True,
                reason=None,
                intent="update_asset_value",
                extracted_fields={
                    "target_hint": "Studio",
                    "amount_text": "38000",
                    "asset_value_kind_hint": "current_value",
                },
                missing_fields=(),
                clarifying_question=None,
                confidence=0.9,
            )
            proposal = build_typed_action_proposal(
                db, household_id=household_id, interpretation=interpretation
            )
            assert proposal.can_execute is True
            proposal_id = persist_action_proposal(
                db,
                household_id=household_id,
                user_id=db.scalar(select(User.id)),
                trace_id="trace-investment-assistant",
                original_message="Hoje acho que o Studio vale 38 mil",
                structured_interpretation=interpretation.to_dict(),
                proposal=proposal,
            )

        execute = client.post("/api/assistant/execute", json={"proposal_id": proposal_id})
        assert execute.status_code == 201, execute.text
        result = execute.json()
        assert result["idempotent_replay"] is False
        action = result["action"]
        assert action["typed_action"] == "update_asset_value"
        assert action["before_state"]["current_value"] == "35000.00"
        assert action["after_state"]["current_value"] == "38000.00"
        assert action["undoable"] is True

        row = next(
            item for item in client.get("/api/investments").json()["investments"] if item["id"] == investment_id
        )
        assert row["current_value"] == 38000.0

        # Idempotent replay: executing the same proposal again never
        # dispatches a second valuation.
        replay = client.post("/api/assistant/execute", json={"proposal_id": proposal_id})
        assert replay.status_code == 201, replay.text
        assert replay.json()["idempotent_replay"] is True
        valuations = client.get(f"/api/investments/{investment_id}/valuations").json()
        assert len(valuations) == 2  # creation snapshot + the one update, never two updates

        undo = client.post(f"/api/assistant/actions/{action['id']}/undo", json={"reason": "Valor incorreto"})
        assert undo.status_code == 200, undo.text
        row_after_undo = next(
            item for item in client.get("/api/investments").json()["investments"] if item["id"] == investment_id
        )
        assert row_after_undo["current_value"] == 35000.0


def test_dashboard_regression_never_sums_historical_or_projected_into_net_worth() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        client.post(
            "/api/investments",
            json={
                "name": "Studio",
                "historical_cost": "30000.00",
                "current_value": "35000.00",
                "expected_receivable_value": "45000.00",
                "valuation_date": "2026-09-01",
            },
        )
        dashboard = client.get("/api/dashboard")
        assert dashboard.status_code == 200, dashboard.text
        net_worth = dashboard.json()["noncanonical"]["net_worth"]
        assert net_worth["value"] == 35000.0
        investments = dashboard.json()["noncanonical"]["investments"]
        assert len(investments) == 1
        assert investments[0]["current_value"] == 35000.0
        assert investments[0]["historical_cost"] == 30000.0
        assert investments[0]["expected_receivable_value"] == 45000.0

        # The exact figure GET /investments reports must never diverge --
        # rebaseline "nenhum cálculo patrimonial duplicado no frontend".
        direct = client.get("/api/investments").json()
        assert direct["total_current_value"] == net_worth["value"]
