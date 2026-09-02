from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.services.financial_snapshots as financial_snapshots_module
from app.api import dashboard, reports
from app.db import Base
from app.models import (
    Account,
    AccountBalanceObservation,
    Category,
    FinancialProfile,
    FinancialSnapshot,
    FinancialSnapshotLineage,
    Household,
    Transaction,
    User,
)
from app.services.financial_invariants import InvariantContext, InvariantScope, InvariantStatus
from app.services.financial_snapshots import (
    _observation_is_trusted,
    build_snapshot,
    dashboard_and_report_consistency_facts,
    liquidity_transition_facts,
    snapshot_lineage_facts,
)
from app.services.invariant_registry import evaluate_invariant


def test_only_confirmed_or_reconciled_balance_evidence_is_trusted() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Evidência")
        db.add(household)
        db.flush()
        investment = Account(
            household_id=household.id,
            name="Reserva DI",
            account_type="investment",
        )
        db.add(investment)
        db.flush()
        imported = AccountBalanceObservation(
            household_id=household.id,
            account_id=investment.id,
            amount=Decimal("100"),
            as_of_date=date(2026, 9, 1),
            observation_type="closing",
            source="statement",
            confidence=Decimal("0.7000"),
            trace_id="unreconciled",
        )
        confirmed = AccountBalanceObservation(
            household_id=household.id,
            account_id=investment.id,
            amount=Decimal("100"),
            as_of_date=date(2026, 9, 2),
            observation_type="point_in_time",
            source="manual_confirmed",
            confidence=Decimal("1.0000"),
            trace_id="confirmed",
        )
        db.add_all([imported, confirmed])
        db.flush()

        assert not _observation_is_trusted(db, imported)
        assert _observation_is_trusted(db, confirmed)


def test_point_in_time_balance_becomes_next_period_opening_evidence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Ponto no Tempo")
        db.add(household)
        db.flush()
        investment = Account(
            household_id=household.id,
            name="Reserva DI",
            account_type="investment",
        )
        profile = FinancialProfile(
            household_id=household.id,
            investment_name="Reserva DI",
            investment_balance=Decimal("999"),
        )
        db.add_all([investment, profile])
        db.flush()
        db.add(
            AccountBalanceObservation(
                household_id=household.id,
                account_id=investment.id,
                amount=Decimal("321.45"),
                as_of_date=date(2026, 9, 2),
                observation_type="point_in_time",
                source="manual_confirmed",
                confidence=Decimal("1.0000"),
                trace_id="confirmed-point",
            )
        )
        db.flush()

        september = build_snapshot(db, household_id=household.id, period="2026-09")
        october = build_snapshot(db, household_id=household.id, period="2026-10")

        assert not september.payload["balance_evidence_trusted"]
        assert october.payload["balance_evidence_trusted"]
        assert october.opening_liquidity_balance == Decimal("321.45")


def _transaction(
    household: Household,
    account: Account,
    category: Category,
    *,
    booked_at: date,
    amount: str,
    transaction_type: str,
    suffix: str,
    excluded: bool = False,
) -> Transaction:
    return Transaction(
        household_id=household.id,
        account_id=account.id,
        category_id=category.id,
        booked_at=booked_at,
        description=f"Movimento {suffix}",
        normalized_description=f"MOVIMENTO {suffix}",
        amount=Decimal(amount),
        transaction_type=transaction_type,
        fingerprint=suffix.rjust(64, "0"),
        canonical_status="canonical",
        excluded=excluded,
    )


def test_snapshots_separate_economic_cash_card_and_patrimonial_flows() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Snapshot")
        db.add(household)
        db.flush()
        investment = Account(
            household_id=household.id,
            name="Privilège DI",
            institution="Itaú",
            account_type="investment",
        )
        card = Account(
            household_id=household.id,
            name="Cartão",
            institution="Banco",
            account_type="credit_card",
        )
        expense = Category(household_id=household.id, name="Compras")
        income = Category(household_id=household.id, name="Receitas")
        reconciliation = Category(household_id=household.id, name="Conciliação")
        patrimonial = Category(household_id=household.id, name="Transferência patrimonial")
        db.add_all([investment, card, expense, income, reconciliation, patrimonial])
        db.flush()
        profile = FinancialProfile(
            household_id=household.id,
            monthly_cash_cap=Decimal("1000"),
            emergency_floor=Decimal("20"),
            investment_name="Privilège DI",
            investment_balance=Decimal("9999"),
        )
        observation = AccountBalanceObservation(
            household_id=household.id,
            account_id=investment.id,
            amount=Decimal("50"),
            as_of_date=date(2026, 8, 1),
            observation_type="opening",
            source="manual_confirmed",
            confidence=Decimal("1"),
            trace_id="observation-trace",
        )
        db.add_all(
            [
                profile,
                observation,
                _transaction(
                    household,
                    card,
                    expense,
                    booked_at=date(2026, 8, 5),
                    amount="-100",
                    transaction_type="expense",
                    suffix="1",
                ),
                _transaction(
                    household,
                    card,
                    reconciliation,
                    booked_at=date(2026, 8, 10),
                    amount="100",
                    transaction_type="reconciliation",
                    suffix="2",
                    excluded=True,
                ),
                _transaction(
                    household,
                    investment,
                    patrimonial,
                    booked_at=date(2026, 8, 12),
                    amount="25",
                    transaction_type="transfer",
                    suffix="3",
                    excluded=True,
                ),
                _transaction(
                    household,
                    investment,
                    income,
                    booked_at=date(2026, 9, 2),
                    amount="75",
                    transaction_type="income",
                    suffix="4",
                ),
            ]
        )
        duplicate_candidate = _transaction(
            household,
            card,
            expense,
            booked_at=date(2026, 8, 6),
            amount="-200",
            transaction_type="expense",
            suffix="5",
        )
        duplicate_candidate.canonical_status = "unassigned"
        duplicate_candidate.possible_duplicate = True
        db.add(duplicate_candidate)
        db.commit()

        # Requesting September first must materialize August and carry exactly
        # the same deterministic predecessor state.
        september = build_snapshot(db, household_id=household.id, period="2026-09")
        db.commit()
        august = db.scalar(
            select(FinancialSnapshot).where(
                FinancialSnapshot.household_id == household.id,
                FinancialSnapshot.period == "2026-08",
                FinancialSnapshot.status == "current",
            )
        )
        assert august is not None
        assert august.opening_liquidity_balance == Decimal("50.00")
        assert august.operating_expenses == Decimal("100.00")
        assert august.card_spend == Decimal("100.00")
        assert august.card_payments == Decimal("100.00")
        assert august.redemptions == Decimal("25.00")
        assert august.operating_income == Decimal("0.00")
        assert august.closing_liquidity_balance == Decimal("0.00")
        assert august.closing_uncovered_deficit == Decimal("50.00")
        assert august.payload["opening_balance_source"] == "observation"
        assert august.payload["duplicates_ignored"] == 1
        duplicate_lineage = db.scalar(
            select(FinancialSnapshotLineage).where(
                FinancialSnapshotLineage.snapshot_id == august.id,
                FinancialSnapshotLineage.entity_id == duplicate_candidate.id,
            )
        )
        assert duplicate_lineage is not None
        assert duplicate_lineage.source_role == "excluded"
        assert duplicate_lineage.contribution is None
        payment_lineage = db.scalar(
            select(FinancialSnapshotLineage).where(
                FinancialSnapshotLineage.snapshot_id == august.id,
                FinancialSnapshotLineage.metric_key == "card_payments",
            )
        )
        assert payment_lineage is not None
        assert payment_lineage.source_role == "canonical"
        assert payment_lineage.contribution == Decimal("100.00")

        same = build_snapshot(db, household_id=household.id, period="2026-08")
        assert same.id == august.id
        assert september.opening_uncovered_deficit == Decimal("50.00")
        assert september.closing_uncovered_deficit == Decimal("0.00")
        assert september.closing_liquidity_balance == Decimal("25.00")
        lineage = tuple(
            db.scalars(
                select(FinancialSnapshotLineage).where(FinancialSnapshotLineage.snapshot_id == september.id)
            ).all()
        )
        assert any(item.rule_id == "PRIOR-UNCOVERED-DEFICIT-CARRY" for item in lineage)

        rebuilt = build_snapshot(db, household_id=household.id, period="2026-08", force=True)
        db.commit()
        db.refresh(august)
        assert rebuilt.version == august.version + 1
        assert rebuilt.checksum == august.checksum
        assert august.status == "superseded"
        assert august.superseded_by_id == rebuilt.id
        assert db.scalar(select(FinancialSnapshot).where(FinancialSnapshot.id == rebuilt.id))


def test_report_aggregates_snapshot_refunds_and_latest_balance_observation() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household = Household(name="Família Relatório")
        db.add(household)
        db.flush()
        user = User(
            household_id=household.id,
            name="Admin",
            username="report-admin",
            password_hash="hash",
            is_admin=True,
        )
        investment = Account(
            household_id=household.id,
            name="Reserva DI",
            account_type="investment",
        )
        card = Account(
            household_id=household.id,
            name="Cartão",
            account_type="credit_card",
        )
        purchases = Category(household_id=household.id, name="Compras")
        profile = FinancialProfile(
            household_id=household.id,
            monthly_cash_cap=Decimal("500"),
            emergency_floor=Decimal("0"),
            investment_name="Reserva DI",
            investment_balance=Decimal("9999"),
        )
        db.add_all([user, investment, card, purchases, profile])
        db.flush()
        db.add_all(
            [
                AccountBalanceObservation(
                    household_id=household.id,
                    account_id=investment.id,
                    amount=Decimal("100"),
                    as_of_date=date(2026, 8, 1),
                    observation_type="opening",
                    source="manual_confirmed",
                    confidence=Decimal("1"),
                    trace_id="august-balance",
                ),
                AccountBalanceObservation(
                    household_id=household.id,
                    account_id=investment.id,
                    amount=Decimal("1000"),
                    as_of_date=date(2026, 9, 1),
                    observation_type="opening",
                    source="manual_confirmed",
                    confidence=Decimal("1"),
                    trace_id="september-balance",
                ),
                _transaction(
                    household,
                    card,
                    purchases,
                    booked_at=date(2026, 8, 5),
                    amount="-100",
                    transaction_type="expense",
                    suffix="report-expense",
                ),
                _transaction(
                    household,
                    card,
                    purchases,
                    booked_at=date(2026, 8, 6),
                    amount="20",
                    transaction_type="refund",
                    suffix="report-refund",
                ),
            ]
        )
        db.commit()

        result = reports(end_month="2026-09", months=2, user=user, db=db)

        assert result["summary"]["total_spending"] == 80.0
        assert result["categories"] == [
            {
                "category": "Compras",
                "amount": 80.0,
                "average": 80.0,
                "share": 100.0,
                "color": "#64748B",
            }
        ]
        assert result["summary"]["liquidity_starting_balance"] == 100.0
        assert result["summary"]["liquidity_balance"] == 1000.0
        assert result["summary"]["liquidity_withdrawal"] == 80.0
        assert result["monthly"][-1]["snapshot_id"]


def _evaluate_report_check(invariant_id: str, facts: dict[str, object]):
    return evaluate_invariant(
        invariant_id,
        InvariantContext(
            facts=facts,
            scope=InvariantScope.REPORT,
            entity_type="monthly_close",
            entity_id="monthly-close:household-consistency:2026-09",
            period="2026-09",
            trace_id="trace-report-consistency-test",
        ),
    )


def _consistency_household(db: Session) -> tuple[Household, FinancialProfile, FinancialSnapshot]:
    household = Household(name="Família Consistência Publicada")
    db.add(household)
    db.flush()
    checking = Account(household_id=household.id, name="Conta Corrente", account_type="checking")
    income_category = Category(household_id=household.id, name="Salário")
    expense_category = Category(household_id=household.id, name="Mercado")
    profile = FinancialProfile(
        household_id=household.id,
        investment_name="Reserva DI",
        monthly_cash_cap=Decimal("1000"),
        investment_balance=Decimal("5000.00"),
    )
    db.add_all([checking, income_category, expense_category, profile])
    db.flush()
    db.add_all(
        [
            _transaction(
                household,
                checking,
                income_category,
                booked_at=date(2026, 9, 3),
                amount="1500",
                transaction_type="income",
                suffix="1",
            ),
            _transaction(
                household,
                checking,
                expense_category,
                booked_at=date(2026, 9, 5),
                amount="-400",
                transaction_type="expense",
                suffix="2",
            ),
        ]
    )
    db.commit()
    snapshot = build_snapshot(db, household_id=household.id, period="2026-09")
    return household, profile, snapshot


def test_dashboard_and_report_consistency_facts_observe_the_real_publication_path(
    monkeypatch,
) -> None:
    """INV-019/INV-020 must fail on a real divergence, not agree by construction.

    `dashboard_and_report_consistency_facts()` builds `dashboard_values`/
    `report_values` by calling `dashboard_monetary_dataset()`/
    `report_month_monetary_dataset()` -- the exact functions `app/api.py`'s
    `dashboard()`/`reports()` call to build their own responses (see those
    functions' docstrings). This proves the check is wired to that
    publication path rather than fabricating three copies of
    `financial_engine_values` internally: patching only
    `report_month_monetary_dataset` -- simulating `/reports` starting to
    serialize a stale or independently-computed figure, with no change to
    the financial rule or to the snapshot itself -- makes INV-020 FAIL,
    while INV-019 (whose function was left untouched) stays PASS against the
    same snapshot.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _household, profile, snapshot = _consistency_household(db)

        baseline_facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        assert (
            baseline_facts["dashboard"]["dashboard_values"]
            == baseline_facts["dashboard"]["financial_engine_values"]
        )
        assert (
            baseline_facts["report"]["report_values"]
            == baseline_facts["report"]["financial_engine_values"]
        )
        assert _evaluate_report_check("INV-019", baseline_facts["dashboard"]).status is InvariantStatus.PASS
        assert _evaluate_report_check("INV-020", baseline_facts["report"]).status is InvariantStatus.PASS

        real_report_dataset = financial_snapshots_module.report_month_monetary_dataset

        def _stale_report_dataset(snapshot_arg: FinancialSnapshot) -> dict[str, Decimal]:
            values = dict(real_report_dataset(snapshot_arg))
            values["operating_expenses"] = values["operating_expenses"] + Decimal("0.02")
            return values

        monkeypatch.setattr(
            financial_snapshots_module, "report_month_monetary_dataset", _stale_report_dataset
        )

        divergent_facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        report_result = _evaluate_report_check("INV-020", divergent_facts["report"])
        assert report_result.status is InvariantStatus.FAIL
        assert report_result.severity.value == "block"
        assert report_result.difference == Decimal("0.02")
        # Only report_month_monetary_dataset was patched -- INV-019 (dashboard)
        # reads dashboard_monetary_dataset, untouched, and must stay PASS.
        assert (
            _evaluate_report_check("INV-019", divergent_facts["dashboard"]).status
            is InvariantStatus.PASS
        )


def test_inv019_catches_a_dashboard_investment_balance_divergence(monkeypatch) -> None:
    """INV-019 must cover `investment_balance`, not just the snapshot-derived fields.

    Before PR 7's post-review correction, `dashboard()` read
    `profile.investment_balance` directly and `_snapshot_monetary_fields`
    never included it, so `dashboard_and_report_consistency_facts` had no
    fact about it at all -- a `dashboard()` regression that published a wrong
    `investment_balance` would sail through `trusted_for_reports=true`. This
    patches `dashboard_monetary_dataset` (the function both `dashboard()` and
    this fact builder call) to publish a wrong `investment_balance` and
    proves INV-019 now catches it.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _household, profile, snapshot = _consistency_household(db)

        baseline_facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        assert baseline_facts["dashboard"]["financial_engine_values"]["investment_balance"] == Decimal(
            "5000.00"
        )
        assert _evaluate_report_check("INV-019", baseline_facts["dashboard"]).status is InvariantStatus.PASS

        real_dashboard_dataset = financial_snapshots_module.dashboard_monetary_dataset

        def _stale_dashboard_dataset(
            snapshot_arg: FinancialSnapshot, *, profile: FinancialProfile
        ) -> dict[str, Decimal]:
            values = dict(real_dashboard_dataset(snapshot_arg, profile=profile))
            values["investment_balance"] = values["investment_balance"] + Decimal("500.00")
            return values

        monkeypatch.setattr(
            financial_snapshots_module, "dashboard_monetary_dataset", _stale_dashboard_dataset
        )

        divergent_facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        result = _evaluate_report_check("INV-019", divergent_facts["dashboard"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"]["investment_balance"]["difference"] == Decimal("500.00")


def test_inv020_catches_a_reports_savings_rate_divergence(monkeypatch) -> None:
    """INV-020 must cover `savings_rate`, not just the snapshot-derived fields.

    Before PR 7's post-review correction, `reports()` computed `savings_rate`
    with its own inline formula and no invariant read it at all (see the
    engineering review on PR 7: "reports independently calculates
    savings_rate"). This patches `savings_rate_from_totals` (the function
    both `reports()` and this fact builder now call) to publish a wrong
    value and proves INV-020 catches it.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _household, profile, snapshot = _consistency_household(db)

        baseline_facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        expected_rate = financial_snapshots_module.savings_rate_from_totals(
            Decimal(snapshot.operating_income), Decimal(snapshot.operating_expenses)
        )
        assert baseline_facts["report"]["report_values"]["savings_rate"] == expected_rate
        assert _evaluate_report_check("INV-020", baseline_facts["report"]).status is InvariantStatus.PASS

        real_savings_rate = financial_snapshots_module.savings_rate_from_totals

        def _stale_savings_rate(total_cash_in: Decimal, total_cash_out: Decimal) -> Decimal:
            return real_savings_rate(total_cash_in, total_cash_out) + Decimal("1.00")

        monkeypatch.setattr(
            financial_snapshots_module, "savings_rate_from_totals", _stale_savings_rate
        )

        divergent_facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        result = _evaluate_report_check("INV-020", divergent_facts["report"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"]["savings_rate"]["difference"] == Decimal("1.00")
        # Only the report side computes savings_rate -- INV-019 (dashboard)
        # has no such fact and must stay unaffected/PASS.
        assert (
            _evaluate_report_check("INV-019", divergent_facts["dashboard"]).status
            is InvariantStatus.PASS
        )


def test_reports_endpoint_uses_the_shared_summary_publication_function(monkeypatch) -> None:
    """`GET /reports` itself must spread `report_summary_monetary_publication`
    verbatim into `summary`, not assign `summary["savings_rate"]` from a
    private local.

    PR 7, Round 6: `reports()` used to call `savings_rate_from_totals`
    directly and assign `summary["savings_rate"]` from that local -- the
    engineering review on PR 7, Round 6 found that assembly step invisible
    to INV-020 no matter what `savings_rate_from_totals` returned (see
    `report_summary_monetary_publication`'s docstring). Complements the two
    facts-level tests above (which exercise the invariant in isolation) by
    proving the live endpoint's `summary` is genuinely spread from that
    shared function's return value: patch `report_summary_monetary_publication`
    as imported into `app.api` and show the real HTTP response changes.
    """

    import app.api as api_module

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, _profile, _snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-savings-rate",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_summary_publication = api_module.report_summary_monetary_publication
        captured_kwargs: dict[str, object] = {}

        def _corrupted_summary_publication(**kwargs):
            captured_kwargs.update(kwargs)
            values = dict(real_summary_publication(**kwargs))
            values["savings_rate"] = values["savings_rate"] + Decimal("7.00")
            return values

        monkeypatch.setattr(
            api_module, "report_summary_monetary_publication", _corrupted_summary_publication
        )

        result = reports(end_month="2026-09", months=1, user=user, db=db)
        expected = real_summary_publication(**captured_kwargs)["savings_rate"]
        assert Decimal(str(result["summary"]["savings_rate"])) == expected + Decimal("7.00")


def test_dashboard_publication_mutation_is_caught_by_inv019_and_the_real_endpoint(monkeypatch) -> None:
    """A bug confined to `/dashboard`'s response-shaping step -- not the
    Financial Engine, not the pre-aliasing attribute reads in
    `dashboard_monetary_dataset` -- must corrupt the real HTTP response
    *and* fail INV-019.

    Before PR 7 Round 3's structural fix, `dashboard_and_report_consistency_facts()`
    built `dashboard_values` by independently calling `dashboard_monetary_dataset()`
    again, so a bug confined to the endpoint's own key-aliasing/mapping step
    (a wrong key, a stray offset applied after the canonical monetary
    calculation) would corrupt what `/dashboard` actually returns while the
    invariant, recomputing separately from the same untouched inputs, kept
    agreeing with itself and stayed PASS -- see the engineering review on PR
    7, Round 3: "the invariant never captures or compares the endpoint's
    actual serialized response."

    This patches `dashboard_monetary_publication` -- the one function
    `dashboard()` now spreads verbatim into its response, and the same
    function `dashboard_and_report_consistency_facts()` calls for
    `dashboard_values` -- to add 0.02 to `cash_out` only, strictly after the
    canonical monetary calculation already ran inside it (`snapshot`/
    `profile`, the engine truth, are never touched). Proves both halves of
    the required convergence in one test: the real endpoint publishes the
    wrong number, and INV-019 -- whose `financial_engine_values` are read
    straight off `snapshot`/`profile` and never call the mutated function --
    fails against that same real divergence, not a hand-simulated one.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-dashboard-publication",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_publication = financial_snapshots_module.dashboard_monetary_publication
        expected_cash_out = real_publication(snapshot, profile=profile)["cash_out"]

        def _corrupted_publication(snapshot_arg, *, profile):
            values = dict(real_publication(snapshot_arg, profile=profile))
            values["cash_out"] = values["cash_out"] + Decimal("0.02")
            return values

        monkeypatch.setattr(
            financial_snapshots_module, "dashboard_monetary_publication", _corrupted_publication
        )
        import app.api as api_module

        monkeypatch.setattr(api_module, "dashboard_monetary_publication", _corrupted_publication)

        # (1) The real endpoint's actual published contract diverges.
        response = dashboard(month="2026-09", user=user, db=db)
        assert Decimal(str(response["cash_out"])) == expected_cash_out + Decimal("0.02")

        # (2) INV-019, evaluated against the same untouched snapshot, fails
        # on the same field -- the check observes the real divergence rather
        # than one reconstructed independently of what `/dashboard` returned.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        assert facts["dashboard"]["financial_engine_values"]["cash_out"] == expected_cash_out
        result = _evaluate_report_check("INV-019", facts["dashboard"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"]["cash_out"]["difference"] == Decimal("0.02")


def test_report_publication_mutation_is_caught_by_inv020_and_the_real_endpoint(monkeypatch) -> None:
    """Same requirement as the dashboard test above, for `/reports`/INV-020.

    Mutates `report_month_monetary_publication` to publish `remaining_cap`
    aliased from the wrong field (`cash_cap` instead of the real
    `budget_remaining`) -- a realistic "wrong key mapping" bug, strictly
    after the canonical monetary calculation already ran -- and proves the
    real `/reports` response and INV-020 diverge together, against the same
    untouched snapshot.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-report-publication",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_publication = financial_snapshots_module.report_month_monetary_publication
        real_values = real_publication(snapshot)
        expected_remaining_cap = real_values["remaining_cap"]
        wrong_remaining_cap = real_values["cash_cap"]
        assert expected_remaining_cap != wrong_remaining_cap

        def _corrupted_publication(snapshot_arg):
            values = dict(real_publication(snapshot_arg))
            values["remaining_cap"] = values["cash_cap"]
            return values

        monkeypatch.setattr(
            financial_snapshots_module, "report_month_monetary_publication", _corrupted_publication
        )
        import app.api as api_module

        monkeypatch.setattr(api_module, "report_month_monetary_publication", _corrupted_publication)

        # (1) The real endpoint's actual published contract diverges.
        response = reports(end_month="2026-09", months=1, user=user, db=db)
        assert Decimal(str(response["monthly"][0]["remaining_cap"])) == wrong_remaining_cap

        # (2) INV-020, evaluated against the same untouched snapshot, fails
        # on the same field.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        assert facts["report"]["financial_engine_values"]["remaining_cap"] == expected_remaining_cap
        result = _evaluate_report_check("INV-020", facts["report"])
        assert result.status is InvariantStatus.FAIL
        assert "remaining_cap" in result.metadata["mismatches"]


def test_dashboard_liquidity_available_gap_is_caught_by_inv019_and_the_real_endpoint(monkeypatch) -> None:
    """PR 7, Round 5: `liquidity_starting_balance`, `liquidity_available`,
    `liquidity_deposit` and `liquidity_withdrawal` used to be built inline in
    `dashboard()`'s own `return {...}`, straight off `snapshot`/`snapshot_payload`,
    outside `dashboard_monetary_publication` and therefore outside anything
    INV-019 observed -- see the engineering review on PR 7, Round 5: "canonical
    snapshot outputs such as liquidity_starting_balance, liquidity_available,
    liquidity_deposit, liquidity_withdrawal... remain shaped separately".

    Mutates `dashboard_monetary_publication` to add 0.02 to `liquidity_available`
    only, strictly after the canonical monetary calculation already ran, and
    proves both halves of the required convergence: the real `/dashboard`
    response now publishes the wrong figure, and INV-019 -- whose
    `financial_engine_values["liquidity_available"]` is read straight off
    `snapshot.distance_to_floor` and never calls the mutated function -- fails
    against that same real divergence.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-liquidity-available",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_publication = financial_snapshots_module.dashboard_monetary_publication
        expected_liquidity_available = real_publication(snapshot, profile=profile)["liquidity_available"]

        def _corrupted_publication(snapshot_arg, *, profile):
            values = dict(real_publication(snapshot_arg, profile=profile))
            values["liquidity_available"] = values["liquidity_available"] + Decimal("0.02")
            return values

        monkeypatch.setattr(
            financial_snapshots_module, "dashboard_monetary_publication", _corrupted_publication
        )
        import app.api as api_module

        monkeypatch.setattr(api_module, "dashboard_monetary_publication", _corrupted_publication)

        # (1) The real endpoint's actual published contract diverges.
        response = dashboard(month="2026-09", user=user, db=db)
        assert Decimal(str(response["liquidity_available"])) == expected_liquidity_available + Decimal(
            "0.02"
        )

        # (2) INV-019, evaluated against the same untouched snapshot, fails
        # on the same field.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        assert (
            facts["dashboard"]["financial_engine_values"]["liquidity_available"]
            == expected_liquidity_available
        )
        result = _evaluate_report_check("INV-019", facts["dashboard"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"]["liquidity_available"]["difference"] == Decimal("0.02")


def test_profile_selector_corruption_is_caught_by_inv019_and_the_real_endpoint(monkeypatch) -> None:
    """PR 7, Round 7: `_profile_monetary_fields` used to feed both the
    publication side (`dashboard_monetary_dataset`, via `dashboard()`) and
    INV-019's "expected" side (`_dashboard_financial_engine_truth`), on the
    theory that plain attribute selection off `profile` was safe to share --
    see that function's docstring. The engineering review on PR 7, Round 7
    rejected that: a bug confined to that one selector would move both sides
    identically and INV-019 would pass by construction regardless of what
    `/dashboard` actually publishes.

    Mutates only `_profile_monetary_fields` (not `profile` itself, not the
    snapshot, not `dashboard_monetary_publication`) to publish the wrong
    `investment_balance`, and proves both halves of the required divergence:
    the real `/dashboard` response changes, while INV-019's
    `financial_engine_values["investment_balance"]` -- now read directly off
    `profile.investment_balance`, never through this selector -- stays at
    the real value, so the check fails on the real gap instead of agreeing
    with itself.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-profile-selector",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_selector = financial_snapshots_module._profile_monetary_fields
        expected_investment_balance = real_selector(profile)["investment_balance"]

        def _corrupted_selector(profile_arg):
            values = dict(real_selector(profile_arg))
            values["investment_balance"] = values["investment_balance"] + Decimal("50.00")
            return values

        monkeypatch.setattr(financial_snapshots_module, "_profile_monetary_fields", _corrupted_selector)

        # (1) The real endpoint's actual published contract diverges.
        response = dashboard(month="2026-09", user=user, db=db)
        assert Decimal(str(response["investment_balance"])) == expected_investment_balance + Decimal(
            "50.00"
        )

        # (2) INV-019, evaluated against the same untouched snapshot/profile,
        # fails on the same field -- the "expected" side never calls the
        # mutated selector.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        assert (
            facts["dashboard"]["financial_engine_values"]["investment_balance"]
            == expected_investment_balance
        )
        assert facts["dashboard"]["dashboard_values"]["investment_balance"] == (
            expected_investment_balance + Decimal("50.00")
        )
        result = _evaluate_report_check("INV-019", facts["dashboard"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"]["investment_balance"]["difference"] == Decimal("50.00")


def test_dashboard_cash_flow_by_account_gap_is_caught_by_inv019_and_the_real_endpoint(monkeypatch) -> None:
    """PR 7, Round 5: the monetary amounts nested in `cash_flow_by_account`
    were published verbatim from `snapshot.payload` but never observed by
    INV-019 at all -- see the engineering review on PR 7, Round 5: "the
    monetary values nested in cash_flow_by_account remain shaped separately".

    `dashboard()` now sources its `cash_flow_by_account` key from
    `account_cash_flow_rows(snapshot)`, the same function
    `dashboard_and_report_consistency_facts()` flattens into
    `dashboard_values`. Mutating that one function corrupts both the real
    per-account amounts `/dashboard` returns *and* the flattened fact INV-019
    reads, while the independent "expected" side
    (`_dashboard_account_cash_flow_engine_truth`, reading
    `snapshot.payload["cash_flow_by_account"]` directly) stays untouched.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-cash-flow-by-account",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_rows = financial_snapshots_module.account_cash_flow_rows
        baseline_rows = real_rows(snapshot)
        assert len(baseline_rows) == 1
        account_key = str(baseline_rows[0]["account_id"])
        expected_cash_in = Decimal(str(baseline_rows[0]["cash_in"]))

        def _corrupted_rows(snapshot_arg):
            rows = [dict(row) for row in real_rows(snapshot_arg)]
            rows[0]["cash_in"] = float(Decimal(str(rows[0]["cash_in"])) + Decimal("50.00"))
            return rows

        monkeypatch.setattr(financial_snapshots_module, "account_cash_flow_rows", _corrupted_rows)
        import app.api as api_module

        monkeypatch.setattr(api_module, "account_cash_flow_rows", _corrupted_rows)

        # (1) The real endpoint's actual published per-account amount diverges.
        response = dashboard(month="2026-09", user=user, db=db)
        assert Decimal(str(response["cash_flow_by_account"][0]["cash_in"])) == expected_cash_in + Decimal(
            "50.00"
        )

        # (2) INV-019, evaluated against the same untouched snapshot, fails on
        # the corresponding flattened fact -- the independent engine-truth
        # side never called the mutated function.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        fact_key = f"cash_flow_by_account.{account_key}.cash_in"
        assert facts["dashboard"]["financial_engine_values"][fact_key] == expected_cash_in
        result = _evaluate_report_check("INV-019", facts["dashboard"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"][fact_key]["difference"] == Decimal("50.00")


def test_reports_summary_savings_rate_gap_is_caught_by_inv020_and_the_real_endpoint(monkeypatch) -> None:
    """PR 7, Round 5: `report_values["savings_rate"]` used to be computed
    from the raw, pre-publication snapshot totals, not from
    `report_month_monetary_publication`'s own `cash_in`/`cash_out` -- the
    values `/reports` actually sums into `summary.total_cash_in`/
    `total_cash_out` before computing `summary.savings_rate` -- see the
    engineering review on PR 7, Round 5: "the serialized one-month summary,
    including savings_rate, must come from the publication object INV-020
    observes".

    `_consistency_household`'s income transaction is not categorized as
    "Receitas", so its `operating_income`/`cash_in` is genuinely `0.00` --
    this mutates `report_month_monetary_publication` to publish `cash_in` as
    1000.00 instead (strictly after the canonical monetary calculation
    already ran) so `savings_rate` moves off its `total_cash_in <= 0` early
    return, and proves both halves: `/reports`' real `summary.savings_rate`
    (a one-month window, the shape a monthly close checks) diverges from
    what it would have been, *and* INV-020 fails, while
    `financial_engine_values["savings_rate"]` (`_savings_rate_engine_truth`,
    reading `snapshot` columns directly) stays correct.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-report-savings-rate",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_publication = financial_snapshots_module.report_month_monetary_publication
        real_values = real_publication(snapshot)
        assert real_values["cash_in"] == Decimal("0.00")
        expected_savings_rate = financial_snapshots_module.savings_rate_from_totals(
            real_values["cash_in"], real_values["cash_out"]
        )
        corrupted_cash_in = Decimal("1000.00")
        corrupted_savings_rate = financial_snapshots_module.savings_rate_from_totals(
            corrupted_cash_in, real_values["cash_out"]
        )
        assert expected_savings_rate != corrupted_savings_rate

        def _corrupted_publication(snapshot_arg):
            values = dict(real_publication(snapshot_arg))
            values["cash_in"] = corrupted_cash_in
            return values

        monkeypatch.setattr(
            financial_snapshots_module, "report_month_monetary_publication", _corrupted_publication
        )
        import app.api as api_module

        monkeypatch.setattr(api_module, "report_month_monetary_publication", _corrupted_publication)

        # (1) The real endpoint's actual published summary diverges.
        response = reports(end_month="2026-09", months=1, user=user, db=db)
        assert Decimal(str(response["summary"]["savings_rate"])) == corrupted_savings_rate

        # (2) INV-020, evaluated against the same untouched snapshot, fails
        # on `savings_rate` -- the engine-truth side never called the
        # mutated publication function.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        assert facts["report"]["financial_engine_values"]["savings_rate"] == expected_savings_rate
        result = _evaluate_report_check("INV-020", facts["report"])
        assert result.status is InvariantStatus.FAIL
        assert "savings_rate" in result.metadata["mismatches"]


def test_dashboard_liquidity_flow_emergency_floor_food_benefits_gap_is_caught_by_inv019_and_the_real_endpoint(
    monkeypatch,
) -> None:
    """PR 7, Round 6: `liquidity_flow`, `emergency_floor` and `food_benefits`
    used to be built inline in `dashboard()`'s own `return {...}` --
    `liquidity_flow` aliasing `cash_net` a second time, `emergency_floor`
    straight off `profile`, `food_benefits` from a private
    `food_allowance + meal_allowance_daily * workdays_month` local -- outside
    `dashboard_monetary_publication` and therefore outside anything INV-019
    observed. See the engineering review on PR 7, Round 6: "canonical
    snapshot outputs ... such as liquidity_flow, emergency_floor and
    food_benefits ... remain shaped separately".

    Mutates `dashboard_monetary_publication` to add distinct offsets to all
    three fields at once, strictly after the canonical monetary calculation
    already ran, and proves both halves of the required convergence for
    each: the real `/dashboard` response now publishes the wrong figures,
    and INV-019 -- whose `financial_engine_values` for these three fields
    are read straight off `snapshot`/`profile` and never call the mutated
    function -- fails against that same real divergence, on all three
    fields at once.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-liquidity-flow-floor-benefits",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_publication = financial_snapshots_module.dashboard_monetary_publication
        baseline = real_publication(snapshot, profile=profile)
        expected_liquidity_flow = baseline["liquidity_flow"]
        expected_emergency_floor = baseline["emergency_floor"]
        expected_food_benefits = baseline["food_benefits"]

        def _corrupted_publication(snapshot_arg, *, profile):
            values = dict(real_publication(snapshot_arg, profile=profile))
            values["liquidity_flow"] = values["liquidity_flow"] + Decimal("0.03")
            values["emergency_floor"] = values["emergency_floor"] + Decimal("11.00")
            values["food_benefits"] = values["food_benefits"] + Decimal("5.00")
            return values

        monkeypatch.setattr(
            financial_snapshots_module, "dashboard_monetary_publication", _corrupted_publication
        )
        import app.api as api_module

        monkeypatch.setattr(api_module, "dashboard_monetary_publication", _corrupted_publication)

        # (1) The real endpoint's actual published contract diverges on all
        # three fields.
        response = dashboard(month="2026-09", user=user, db=db)
        assert Decimal(str(response["liquidity_flow"])) == expected_liquidity_flow + Decimal("0.03")
        assert Decimal(str(response["emergency_floor"])) == expected_emergency_floor + Decimal("11.00")
        assert Decimal(str(response["food_benefits"])) == expected_food_benefits + Decimal("5.00")

        # (2) INV-019, evaluated against the same untouched snapshot, fails
        # on all three fields.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        assert facts["dashboard"]["financial_engine_values"]["liquidity_flow"] == expected_liquidity_flow
        assert (
            facts["dashboard"]["financial_engine_values"]["emergency_floor"] == expected_emergency_floor
        )
        assert facts["dashboard"]["financial_engine_values"]["food_benefits"] == expected_food_benefits
        result = _evaluate_report_check("INV-019", facts["dashboard"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"]["liquidity_flow"]["difference"] == Decimal("0.03")
        assert result.metadata["mismatches"]["emergency_floor"]["difference"] == Decimal("11.00")
        assert result.metadata["mismatches"]["food_benefits"]["difference"] == Decimal("5.00")


def test_dashboard_category_spending_gap_is_caught_by_inv019_and_the_real_endpoint(monkeypatch) -> None:
    """PR 7, Round 6: the monetary amounts nested in `category_spending` were
    published verbatim from `snapshot.payload` but never observed by
    INV-019 at all -- see the engineering review on PR 7, Round 6:
    "category_spending contains monetary amounts nested and continues
    coming direct from snapshot.payload".

    `dashboard()` now sources its `category_spending` key from
    `category_spending_rows(snapshot)`, the same function
    `dashboard_and_report_consistency_facts()` flattens into
    `dashboard_values`. Mutating that one function corrupts both the real
    per-category amount `/dashboard` returns *and* the flattened fact
    INV-019 reads, while the independent "expected" side
    (`_dashboard_category_spending_engine_truth`, reading
    `snapshot.payload["category_spending"]` directly) stays untouched.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-category-spending",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_rows = financial_snapshots_module.category_spending_rows
        baseline_rows = real_rows(snapshot)
        assert len(baseline_rows) == 1
        category_key = str(baseline_rows[0]["category"])
        expected_amount = Decimal(str(baseline_rows[0]["amount"]))

        def _corrupted_rows(snapshot_arg):
            rows = [dict(row) for row in real_rows(snapshot_arg)]
            rows[0]["amount"] = float(Decimal(str(rows[0]["amount"])) + Decimal("25.00"))
            return rows

        monkeypatch.setattr(financial_snapshots_module, "category_spending_rows", _corrupted_rows)
        import app.api as api_module

        monkeypatch.setattr(api_module, "category_spending_rows", _corrupted_rows)

        # (1) The real endpoint's actual published per-category amount diverges.
        response = dashboard(month="2026-09", user=user, db=db)
        assert Decimal(str(response["category_spending"][0]["amount"])) == expected_amount + Decimal(
            "25.00"
        )

        # (2) INV-019, evaluated against the same untouched snapshot, fails on
        # the corresponding flattened fact -- the independent engine-truth
        # side never called the mutated function.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        fact_key = f"category_spending.{category_key}.amount"
        assert facts["dashboard"]["financial_engine_values"][fact_key] == expected_amount
        result = _evaluate_report_check("INV-019", facts["dashboard"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"][fact_key]["difference"] == Decimal("25.00")


def test_dashboard_does_not_certify_future_commissions_gross_under_inv019(monkeypatch) -> None:
    """PR 7, Round 6: `future_commissions_gross` is a live query against
    `Commission` rows, not a Financial Engine/snapshot output -- it must stay
    explicitly uncertified by INV-019 rather than silently omitted (see the
    engineering review on PR 7, Round 6: "classify explicitly ... do not
    present as certified by snapshot/INV-019").

    Locks in that classification: the publication contract INV-019 observes
    never gains this key, so a live-query value that legitimately changes
    between two reads of the same snapshot (a commission gets cancelled a
    moment later) can never make a monthly close's `trusted_for_reports`
    gate flap.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, profile, snapshot = _consistency_household(db)
        del household

        publication = financial_snapshots_module.dashboard_monetary_publication(snapshot, profile=profile)
        assert "future_commissions_gross" not in publication

        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        assert "future_commissions_gross" not in facts["dashboard"]["dashboard_values"]
        assert "future_commissions_gross" not in facts["dashboard"]["financial_engine_values"]


def test_reports_summary_savings_rate_builder_gap_is_caught_by_inv020_and_the_real_endpoint(
    monkeypatch,
) -> None:
    """PR 7, Round 6: even after `report_values["savings_rate"]` started
    reading `report_month_monetary_publication`'s own `cash_in`/`cash_out`
    (Round 5), `reports()` still assembled `summary["savings_rate"]` as its
    own separate literal (`savings_rate_from_totals(...)` assigned inline),
    so a regression confined to that one assignment -- not `cash_in`, not
    the snapshot, not the canonical formula -- would still corrupt the real
    response with nothing for INV-020 to observe. See the engineering
    review on PR 7, Round 6: "the endpoint continues building savings_rate
    = savings_rate_from_totals(...) and inserting summary['savings_rate']
    separately ... It is necessary [to have] a builder of the summary's
    monetary contract ... consumed verbatim by the endpoint ... the
    regression must adulterate only the published field of the summary."

    This mutates `report_summary_monetary_publication` -- the shared
    builder both `reports()` and `dashboard_and_report_consistency_facts()`
    now call -- to overwrite `savings_rate` after it is computed, touching
    neither `cash_in`/`cash_out`, the snapshot, nor `savings_rate_from_totals`
    itself, and proves both halves: the real `/reports` response (a
    one-month window, the shape a monthly close checks) diverges, and
    INV-020 fails on the same field against the untouched snapshot.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-report-summary-builder",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_summary_publication = financial_snapshots_module.report_summary_monetary_publication
        captured_kwargs: dict[str, object] = {}

        def _corrupted_summary_publication(**kwargs):
            captured_kwargs.update(kwargs)
            values = dict(real_summary_publication(**kwargs))
            values["savings_rate"] = values["savings_rate"] + Decimal("3.00")
            return values

        monkeypatch.setattr(
            financial_snapshots_module, "report_summary_monetary_publication", _corrupted_summary_publication
        )
        import app.api as api_module

        monkeypatch.setattr(
            api_module, "report_summary_monetary_publication", _corrupted_summary_publication
        )

        real_report_publication = financial_snapshots_module.report_month_monetary_publication(snapshot)

        # (1) The real endpoint's actual published summary diverges, with
        # `cash_in`/`cash_out`/`total_cash_in`/`total_cash_out` untouched.
        response = reports(end_month="2026-09", months=1, user=user, db=db)
        assert Decimal(str(response["summary"]["total_cash_in"])) == real_report_publication["cash_in"]
        assert Decimal(str(response["summary"]["total_cash_out"])) == real_report_publication["cash_out"]
        expected_savings_rate = real_summary_publication(**captured_kwargs)["savings_rate"]
        assert Decimal(str(response["summary"]["savings_rate"])) == expected_savings_rate + Decimal(
            "3.00"
        )

        # (2) INV-020, evaluated against the same untouched snapshot, fails
        # on `savings_rate` -- the engine-truth side never called the
        # mutated builder.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        assert facts["report"]["financial_engine_values"]["savings_rate"] == expected_savings_rate
        result = _evaluate_report_check("INV-020", facts["report"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"]["savings_rate"]["difference"] == Decimal("3.00")


def test_report_summary_scalar_total_mutation_is_caught_by_inv020_and_the_real_endpoint(monkeypatch) -> None:
    """PR 7, Round 7: `report_summary_monetary_publication` now covers every
    canonical total/average/liquidity field `/reports` publishes in
    `summary`, not `savings_rate` alone -- the previous Round 6 fix left
    `total_spending`, `total_cash_in`, the liquidity fields, etc. each
    assembled as reports()'s own private-local literal, none of it observed
    by INV-020 (see the engineering review on PR 7, Round 7).

    Mutates only `total_bank_cash_out`, strictly after the canonical
    monetary calculation already ran, and proves both halves: the real
    `/reports` response diverges on that one field, and INV-020 fails on
    the same field against the untouched snapshot, while every other
    summary field (`savings_rate` included) stays correct.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-report-summary-scalar",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        expected_total_bank_cash_out = financial_snapshots_module._report_summary_engine_truth(
            snapshot, profile=profile
        )["total_bank_cash_out"]

        real_summary_publication = financial_snapshots_module.report_summary_monetary_publication

        def _corrupted_summary_publication(**kwargs):
            values = dict(real_summary_publication(**kwargs))
            values["total_bank_cash_out"] = values["total_bank_cash_out"] + Decimal("12.00")
            return values

        monkeypatch.setattr(
            financial_snapshots_module, "report_summary_monetary_publication", _corrupted_summary_publication
        )
        import app.api as api_module

        monkeypatch.setattr(
            api_module, "report_summary_monetary_publication", _corrupted_summary_publication
        )

        # (1) The real endpoint's actual published summary diverges on the
        # mutated field only.
        response = reports(end_month="2026-09", months=1, user=user, db=db)
        assert Decimal(
            str(response["summary"]["total_bank_cash_out"])
        ) == expected_total_bank_cash_out + Decimal("12.00")

        # (2) INV-020, evaluated against the same untouched snapshot, fails
        # on the same field -- the "expected" side never calls the mutated
        # builder.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        assert (
            facts["report"]["financial_engine_values"]["total_bank_cash_out"]
            == expected_total_bank_cash_out
        )
        result = _evaluate_report_check("INV-020", facts["report"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"]["total_bank_cash_out"]["difference"] == Decimal("12.00")


def test_report_categories_gap_is_caught_by_inv020_and_the_real_endpoint(monkeypatch) -> None:
    """PR 7, Round 7: the monetary amounts nested in `/reports`' `categories`
    array were still assembled from `snapshot.payload["category_spending"]`
    directly, outside anything INV-020 observed (see the engineering review
    on PR 7, Round 7: "nested amounts of categories ... continues coming
    direct from snapshot.payload").

    `reports()` now sums `category_spending_rows(snapshot)` -- the same
    function `/dashboard`/INV-019 already observe -- into its own
    `categories` totals. Mutating that one shared function corrupts both the
    real per-category amount `/reports` returns *and* the flattened
    `categories.<category>.amount` fact INV-020 reads, while the independent
    "expected" side (`_report_categories_engine_truth`, reading
    `snapshot.payload["category_spending"]` directly) stays untouched.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-report-categories",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_rows = financial_snapshots_module.category_spending_rows
        real_row_list = real_rows(snapshot)
        assert real_row_list
        category_key = str(real_row_list[0]["category"])
        expected_amount = Decimal(str(real_row_list[0]["amount"]))

        def _corrupted_rows(snapshot_arg):
            rows = [dict(row) for row in real_rows(snapshot_arg)]
            rows[0]["amount"] = float(Decimal(str(rows[0]["amount"])) + Decimal("18.00"))
            return rows

        monkeypatch.setattr(financial_snapshots_module, "category_spending_rows", _corrupted_rows)
        import app.api as api_module

        monkeypatch.setattr(api_module, "category_spending_rows", _corrupted_rows)

        # (1) The real endpoint's actual published per-category amount diverges.
        response = reports(end_month="2026-09", months=1, user=user, db=db)
        response_category = next(
            item for item in response["categories"] if item["category"] == category_key
        )
        assert Decimal(str(response_category["amount"])) == expected_amount + Decimal("18.00")

        # (2) INV-020, evaluated against the same untouched snapshot, fails
        # on the corresponding flattened fact -- the independent
        # engine-truth side never called the mutated function.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        fact_key = f"categories.{category_key}.amount"
        assert facts["report"]["financial_engine_values"][fact_key] == expected_amount
        result = _evaluate_report_check("INV-020", facts["report"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"][fact_key]["difference"] == Decimal("18.00")


def test_report_accounts_gap_is_caught_by_inv020_and_the_real_endpoint(monkeypatch) -> None:
    """Same requirement as the categories test above, for `/reports`'
    `accounts` array.

    `reports()` now sums `account_cash_flow_rows(snapshot)` -- the same
    function `/dashboard`/INV-019 already observe -- into its own `accounts`
    totals, instead of reading `snapshot.payload["cash_flow_by_account"]`
    directly. Mutates only `cash_in`, strictly after the canonical monetary
    calculation already ran.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household, profile, snapshot = _consistency_household(db)
        user = User(
            household_id=household.id,
            name="Revisor",
            username="revisor-report-accounts",
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.commit()

        real_rows = financial_snapshots_module.account_cash_flow_rows
        real_row_list = real_rows(snapshot)
        assert real_row_list
        account_key = str(real_row_list[0]["account_id"] or "unidentified")
        expected_cash_in = Decimal(str(real_row_list[0]["cash_in"]))

        def _corrupted_rows(snapshot_arg):
            rows = [dict(row) for row in real_rows(snapshot_arg)]
            rows[0]["cash_in"] = float(Decimal(str(rows[0]["cash_in"])) + Decimal("9.00"))
            return rows

        monkeypatch.setattr(financial_snapshots_module, "account_cash_flow_rows", _corrupted_rows)
        import app.api as api_module

        monkeypatch.setattr(api_module, "account_cash_flow_rows", _corrupted_rows)

        # (1) The real endpoint's actual published per-account amount diverges.
        response = reports(end_month="2026-09", months=1, user=user, db=db)
        response_account = next(
            item
            for item in response["accounts"]
            if str(item.get("account_id") or "unidentified") == account_key
        )
        assert Decimal(str(response_account["cash_in"])) == expected_cash_in + Decimal("9.00")

        # (2) INV-020, evaluated against the same untouched snapshot, fails
        # on the corresponding flattened fact -- the independent
        # engine-truth side never called the mutated function.
        facts = dashboard_and_report_consistency_facts(snapshot, profile=profile)
        fact_key = f"accounts.{account_key}.cash_in"
        assert facts["report"]["financial_engine_values"][fact_key] == expected_cash_in
        result = _evaluate_report_check("INV-020", facts["report"])
        assert result.status is InvariantStatus.FAIL
        assert result.metadata["mismatches"][fact_key]["difference"] == Decimal("9.00")


def _evaluate_projection_gate_check(invariant_id: str, facts: dict[str, object]):
    return evaluate_invariant(
        invariant_id,
        InvariantContext(
            facts=facts,
            scope=InvariantScope.PROJECTION,
            entity_type="projection",
            entity_id="projection:household-gate:2026-09",
            period="2026-09",
            trace_id="trace-projection-gate-test",
        ),
    )


def test_liquidity_transition_facts_prove_a_debt_carrying_snapshot() -> None:
    """INV-005/INV-006 must evaluate PASS on a real, debt-carrying snapshot.

    `calculate_liquidity_transition` (the invariants' own independent
    formula) has no parameter for prior uncovered debt, so
    `liquidity_transition_facts` must fold it into a single-step transition
    before calling it. This proves that fold reproduces the engine's own
    numbers for a snapshot that actually carries debt into the period being
    checked -- the exact shape of PR 34's regression scenario -- not just the
    debt-free case already covered by `tests/test_financial_invariants.py`.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Liquidez")
        db.add(household)
        db.flush()
        investment = Account(household_id=household.id, name="Reserva DI", account_type="investment")
        card = Account(household_id=household.id, name="Cartão", account_type="credit_card")
        expense = Category(household_id=household.id, name="Compras")
        income = Category(household_id=household.id, name="Receitas")
        db.add_all([investment, card, expense, income])
        db.flush()
        db.add_all(
            [
                FinancialProfile(household_id=household.id, investment_name="Reserva DI"),
                AccountBalanceObservation(
                    household_id=household.id,
                    account_id=investment.id,
                    amount=Decimal("50"),
                    as_of_date=date(2026, 8, 1),
                    observation_type="opening",
                    source="manual_confirmed",
                    confidence=Decimal("1"),
                    trace_id="opening-observation",
                ),
                _transaction(
                    household,
                    card,
                    expense,
                    booked_at=date(2026, 8, 5),
                    amount="-100",
                    transaction_type="expense",
                    suffix="1",
                ),
                _transaction(
                    household,
                    investment,
                    income,
                    booked_at=date(2026, 9, 2),
                    amount="75",
                    transaction_type="income",
                    suffix="2",
                ),
            ]
        )
        db.commit()

        september = build_snapshot(db, household_id=household.id, period="2026-09")
        assert september.opening_uncovered_deficit == Decimal("50.00")
        assert september.closing_uncovered_deficit == Decimal("0.00")
        assert september.closing_liquidity_balance == Decimal("25.00")

        facts = liquidity_transition_facts(september)
        assert facts["opening_liquidity_balance"] == Decimal("0.00")
        assert facts["monthly_operating_result"] == Decimal("25.00")
        non_negative_balance = _evaluate_projection_gate_check("INV-005", facts)
        deficit_consumes_liquidity = _evaluate_projection_gate_check("INV-006", facts)
        assert non_negative_balance.status is InvariantStatus.PASS
        assert deficit_consumes_liquidity.status is InvariantStatus.PASS


def test_liquidity_transition_facts_catch_a_broken_engine_result() -> None:
    """A snapshot that misreports its own closing figures must FAIL.

    This is the fold's contrapositive to the previous test: if `settle_liquidity`
    ever mis-happens to publish a closing balance that doesn't match the
    single-step transition implied by the snapshot's own opening/result/debt
    fields, `liquidity_transition_facts` + INV-005 must catch it instead of
    trusting the engine's own arithmetic -- proving the check is a real
    second opinion, not a tautology that always agrees with the engine.
    """

    from types import SimpleNamespace

    broken_snapshot = SimpleNamespace(
        opening_liquidity_balance=Decimal("0.00"),
        operating_result=Decimal("75.00"),
        investment_yield=Decimal("0.00"),
        opening_uncovered_deficit=Decimal("50.00"),
        # Correct closing balance would be 25.00 (75 - 50 of debt paid down).
        closing_liquidity_balance=Decimal("30.00"),
        closing_uncovered_deficit=Decimal("0.00"),
        liquidity_used=Decimal("0.00"),
    )
    facts = liquidity_transition_facts(broken_snapshot)
    result = _evaluate_projection_gate_check("INV-005", facts)
    assert result.status is InvariantStatus.FAIL


def test_snapshot_lineage_facts_require_transactions_accounts_and_categories() -> None:
    """INV-022 must FAIL for an otherwise-trusted snapshot with an empty period.

    A manually confirmed opening balance alone identifies no transaction,
    account, category or rule lineage, so a period with zero transactions
    cannot claim complete traceability -- exactly the gap that let
    `trusted_for_projection` become true without genuine invariant coverage.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Rastreabilidade")
        db.add(household)
        db.flush()
        investment = Account(household_id=household.id, name="Reserva DI", account_type="investment")
        card = Account(household_id=household.id, name="Cartão", account_type="credit_card")
        expense = Category(household_id=household.id, name="Compras")
        income = Category(household_id=household.id, name="Receitas")
        db.add_all([investment, card, expense, income])
        db.flush()
        db.add_all(
            [
                FinancialProfile(household_id=household.id, investment_name="Reserva DI"),
                AccountBalanceObservation(
                    household_id=household.id,
                    account_id=investment.id,
                    amount=Decimal("50"),
                    as_of_date=date(2026, 8, 1),
                    observation_type="opening",
                    source="manual_confirmed",
                    confidence=Decimal("1"),
                    trace_id="opening-observation",
                ),
            ]
        )
        db.commit()

        empty_period = build_snapshot(db, household_id=household.id, period="2026-08")
        empty_facts = snapshot_lineage_facts(db, empty_period)
        assert empty_facts["transaction_ids"] == []
        assert empty_facts["account_ids"] == []
        assert empty_facts["category_ids"] == []
        empty_result = _evaluate_projection_gate_check("INV-022", empty_facts)
        assert empty_result.status is InvariantStatus.FAIL

        db.add(
            _transaction(
                household,
                card,
                expense,
                booked_at=date(2026, 8, 5),
                amount="-100",
                transaction_type="expense",
                suffix="lineage-1",
            )
        )
        db.commit()

        populated_period = build_snapshot(db, household_id=household.id, period="2026-08", force=True)
        populated_facts = snapshot_lineage_facts(db, populated_period)
        assert populated_facts["transaction_ids"] == [
            db.scalar(select(Transaction.id).where(Transaction.household_id == household.id))
        ]
        assert populated_facts["account_ids"] == [card.id]
        assert populated_facts["category_ids"] == [expense.id]
        assert populated_facts["source_count"] == len(populated_facts["source_ids"])
        populated_result = _evaluate_projection_gate_check("INV-022", populated_facts)
        assert populated_result.status is InvariantStatus.PASS
