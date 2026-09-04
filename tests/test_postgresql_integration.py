"""Real-PostgreSQL migration and backfill gates (PR 8 Work Order scope item 1).

`tests/test_migrations.py` already exercises the full Alembic chain and two
legacy-baseline scenarios, but only against SQLite, and `tests/test_backfill.py`
only against SQLite too. Both are the right tool for fast, hermetic unit
coverage, but the Work Order explicitly requires the production-compatible
engine in the loop for: an empty-database upgrade to head, a legacy-baseline
upgrade to head with existing rows preserved, and backfill idempotency
(locking/transaction semantics genuinely differ between SQLite and
PostgreSQL -- see e.g. `IntegrityRun`/`IntegrityFinding`'s `SELECT ... FOR
UPDATE` usage and `HouseholdFinancialRevision`'s docstring on that exact
gap).

Skipped entirely unless `POSTGRES_TEST_DATABASE_URL` is set to a reachable,
disposable PostgreSQL database -- CI provides one via a `postgres:` service
container (see `.github/workflows/ci.yml`, job `integration-postgres`). Each
test resets the `public` schema itself (`DROP SCHEMA public CASCADE; CREATE
SCHEMA public;`) so tests can run in any order against the same database
without depending on any other test's state -- this file never touches
`app.db.SessionLocal`/`engine` (which bind once, at import time, to whatever
`DATABASE_URL` happened to be cached first across the whole test session);
every engine/session here is built locally against `POSTGRES_TEST_DATABASE_URL`.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

os.environ.setdefault("SECRET_KEY", "postgres-integration-test-secret-not-used-in-production")
os.environ.setdefault("FILE_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, func, inspect, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alembic import command  # noqa: E402
from app.cli.backfill import process_household  # noqa: E402
from app.cli.initial_load import (  # noqa: E402
    AccountSeed,
    BalanceSeed,
    DocumentSeed,
    InitialLoadManifest,
    ObligationSeed,
    _apply_balances,
    _apply_documents,
    _apply_obligations,
    _lock_household_initial_load,
)
from app.config import get_settings  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AccountBalanceObservation,
    Document,
    DocumentReconciliation,
    DuplicateGroup,
    FinancialSnapshot,
    Household,
    Obligation,
    Transaction,
    User,
)
from tests.fixtures.fact_fingerprint import fact_fingerprint  # noqa: E402
from tests.fixtures.synthetic_household import build_synthetic_household  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_TEST_DATABASE_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="POSTGRES_TEST_DATABASE_URL not set; skipping real-PostgreSQL migration/backfill gates",
)


def _alembic_config() -> Config:
    # `alembic/env.py` reads `get_settings().database_url` itself at import
    # time (not the `Config` object's `sqlalchemy.url`) -- match
    # `tests/test_migrations.py::_alembic_config`'s pattern exactly: the
    # environment variable is the actual source of truth `env.py` consumes.
    os.environ["DATABASE_URL"] = POSTGRES_TEST_DATABASE_URL
    get_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=StringIO())
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _reset_schema() -> None:
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_database():
    _reset_schema()
    yield
    _reset_schema()
    get_settings.cache_clear()


def test_upgrade_empty_postgresql_database_to_head() -> None:
    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "households",
        "transactions",
        "integrity_runs",
        "integrity_findings",
        "document_reconciliations",
        "duplicate_groups",
        "financial_snapshots",
        "monthly_financial_closes",
        "household_financial_revisions",
        "backfill_runs",
    }.issubset(tables)
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0009"
        )
    engine.dispose()


def test_upgrade_from_legacy_0002_baseline_preserves_existing_rows() -> None:
    config = _alembic_config()
    command.upgrade(config, "0002")

    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    with Session(engine) as db:
        household = Household(name="Família Legada PostgreSQL")
        db.add(household)
        db.commit()
        household_id = household.id
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0009"
        preserved_name = connection.execute(
            text("SELECT name FROM households WHERE id = :id"), {"id": household_id}
        ).scalar_one()
        assert preserved_name == "Família Legada PostgreSQL"
    engine.dispose()


def test_downgrade_of_the_head_revision_is_safe_and_reversible() -> None:
    config = _alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, "0008")

    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    inspector = inspect(engine)
    assert "backfill_runs" not in inspector.get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    inspector = inspect(engine)
    assert "backfill_runs" in inspector.get_table_names()
    engine.dispose()


def test_backfill_is_idempotent_against_real_postgresql_locking_semantics() -> None:
    """The SQLite-based `tests/test_backfill.py` proves idempotency and
    non-mutation of source facts against SQLAlchemy's ORM behavior alone.
    This proves the same properties hold with PostgreSQL actually enforcing
    the `SELECT ... FOR UPDATE` row locks `_persist_finding`/
    `_supersede_finding_if_active`/`HouseholdFinancialRevision` rely on
    (SQLite has no real cross-connection row lock -- see those functions'
    docstrings), i.e. against the engine backfill runs on in production.
    """

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    with Session(engine) as db:
        synthetic = build_synthetic_household(db)
        db.commit()
        household_id = synthetic.household.id

        # Same structurally-complete, mapping-derived fingerprint
        # `tests/test_backfill.py` uses -- see
        # `tests/fixtures/fact_fingerprint.py` for why a hand-curated subset
        # of columns is not trusted here either.
        before_facts = fact_fingerprint(db, Transaction, household_id=household_id)

        household = db.get(Household, household_id)
        process_household(db, household, period_from=None, period_to=None)
        db.commit()

        def _counts() -> tuple[int, int, int]:
            return (
                len(
                    db.scalars(
                        select(FinancialSnapshot).where(
                            FinancialSnapshot.household_id == household_id
                        )
                    ).all()
                ),
                len(
                    db.scalars(
                        select(DocumentReconciliation).where(
                            DocumentReconciliation.household_id == household_id
                        )
                    ).all()
                ),
                len(
                    db.scalars(
                        select(DuplicateGroup).where(DuplicateGroup.household_id == household_id)
                    ).all()
                ),
            )

        after_first = _counts()

        household = db.get(Household, household_id)
        process_household(db, household, period_from=None, period_to=None)
        db.commit()
        after_second = _counts()

        assert after_first == after_second
        assert after_first[0] == 1
        assert after_first[1] == 3
        assert after_first[2] == 1

        after_facts = fact_fingerprint(db, Transaction, household_id=household_id)
        assert before_facts == after_facts
    engine.dispose()


def test_backfill_dry_run_leaves_postgresql_database_unchanged() -> None:
    from app.cli.backfill import run_backfill

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    with Session(engine) as db:
        synthetic = build_synthetic_household(db)
        db.commit()
        household_id = synthetic.household.id

        run_backfill(
            db,
            household_names=[synthetic.household.name],
            period_from=None,
            period_to=None,
            dry_run=True,
        )
        db.rollback()

        assert (
            db.scalar(
                select(FinancialSnapshot).where(FinancialSnapshot.household_id == household_id)
            )
            is None
        )
    engine.dispose()


def test_initial_load_documents_are_idempotent_and_resumable_on_real_postgresql(
    tmp_path, monkeypatch
) -> None:
    """`tests/test_initial_load.py` proves the same properties against
    SQLite for fast, hermetic unit coverage, but the initial-load Work
    Order explicitly requires PostgreSQL compatibility for the real,
    unmocked `import_document` pipeline the CLI delegates to (encryption,
    hashing/idempotency, classification, duplicate grouping, persisted
    reconciliation, audit trail). This proves both: a second application of
    an already-imported document is a true no-op, and an interrupted run
    (each document is its own commit unit) resumes by completing only the
    remaining documents rather than duplicating what was already committed.
    """
    command.upgrade(_alembic_config(), "head")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        engine = create_engine(POSTGRES_TEST_DATABASE_URL)
        with Session(engine) as db:
            household = Household(name="Família PostgreSQL Initial Load")
            db.add(household)
            db.flush()
            user = User(
                household_id=household.id,
                name="Admin PostgreSQL",
                username=f"admin-pg-{household.id[:8]}",
                password_hash="not-a-real-password-hash",
                is_admin=True,
                active=True,
            )
            account = Account(
                household_id=household.id,
                name="Conta PostgreSQL",
                institution="Banco Teste",
                account_type="checking",
                owner_label="Família",
            )
            db.add_all([user, account])
            db.commit()

            (tmp_path / "statement-1.csv").write_text(
                "data,descricao,valor\n2026-09-01,Compra teste 1,-10.00\n", encoding="utf-8"
            )
            (tmp_path / "statement-2.csv").write_text(
                "data,descricao,valor\n2026-09-02,Compra teste 2,-20.00\n", encoding="utf-8"
            )
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            full_manifest = InitialLoadManifest(
                version=1,
                load_id="postgres-resume-test",
                accounts=[AccountSeed(key="bank", name="Conta PostgreSQL")],
                documents=[
                    DocumentSeed(
                        path="statement-1.csv", document_type="bank_statement", account="bank"
                    ),
                    DocumentSeed(
                        path="statement-2.csv", document_type="bank_statement", account="bank"
                    ),
                ],
            )
            partial_manifest = full_manifest.model_copy(deep=True)
            partial_manifest.documents = partial_manifest.documents[:1]

            # Simulate a process interrupted right after the first document
            # committed.
            asyncio.run(
                _apply_documents(db, manifest_path, partial_manifest, {"bank": account}, user)
            )
            assert db.scalar(select(func.count(Document.id))) == 1

            resumed_report = asyncio.run(
                _apply_documents(db, manifest_path, full_manifest, {"bank": account}, user)
            )
            assert resumed_report[0]["status"] == "skipped_existing"
            assert resumed_report[1]["status"] in {"imported", "imported_with_review"}
            assert db.scalar(select(func.count(Document.id))) == 2
            assert db.scalar(select(func.count(Transaction.id))) == 2

            # Full idempotent rerun: nothing new is created for either
            # document.
            rerun_report = asyncio.run(
                _apply_documents(db, manifest_path, full_manifest, {"bank": account}, user)
            )
            assert {item["status"] for item in rerun_report} == {"skipped_existing"}
            assert db.scalar(select(func.count(Document.id))) == 2
            assert db.scalar(select(func.count(Transaction.id))) == 2
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_initial_load_concurrent_apply_does_not_duplicate_obligation_or_balance() -> None:
    """PR #40 review, P1: unlike `Account`/`FinancialProfile`/`Document`,
    `Obligation` and `AccountBalanceObservation` carry no DB-level
    `UniqueConstraint` (see `app/models.py`), so
    `_apply_obligations`/`_apply_balances` are idempotent only through a
    plain SELECT-then-INSERT in Python. Two overlapping invocations of this
    CLI against the same household could each pass the SELECT before
    either commits and each INSERT its own row, silently doubling an
    obligation or a confirmed balance -- exactly the class of race
    docs/INTEGRITY_IMPLEMENTATION_PLAN.md and protocol §20 warn against.

    `_lock_household_initial_load` closes this with a transaction-scoped
    `pg_advisory_xact_lock` (mirroring
    `app/services/financial_snapshots.py::_lock_snapshot_key`). This proves
    it holds under a real two-connection race against the engine
    production uses, not merely by reading the code: a first transaction
    takes the lock, resolves and inserts both facts, and deliberately holds
    the transaction open (no commit yet) while a second, fully concurrent
    transaction attempts the same seed; the second's lock acquisition must
    block until the first commits, after which its own SELECT observes the
    already-created rows and takes the idempotent skip path. Exactly one
    obligation row and one balance observation survive -- never two, and
    no `IntegrityError`.
    """
    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        with Session(engine) as setup_db:
            household = Household(name="Família Concorrência Initial Load")
            setup_db.add(household)
            setup_db.flush()
            user = User(
                household_id=household.id,
                name="Admin Concorrência",
                username=f"admin-concur-{household.id[:8]}",
                password_hash="not-a-real-password-hash",
                is_admin=True,
                active=True,
            )
            account = Account(
                household_id=household.id,
                name="Reserva Concorrência",
                institution="Corretora Teste",
                account_type="investment",
                owner_label="Família",
            )
            setup_db.add_all([user, account])
            setup_db.commit()
            household_id = household.id
            account_id = account.id
            user_id = user.id

        manifest = InitialLoadManifest(
            version=1,
            load_id="postgres-concurrency-test",
            accounts=[
                AccountSeed(key="inv", name="Reserva Concorrência", account_type="investment")
            ],
            obligations=[
                ObligationSeed(
                    name="Financiamento concorrente",
                    due_date=date(2026, 10, 10),
                    amount=Decimal("1000.00"),
                )
            ],
            balances=[
                BalanceSeed(
                    account="inv",
                    amount=Decimal("5000.00"),
                    as_of_date=date(2026, 9, 1),
                    observation_type="point_in_time",
                )
            ],
        )

        second_lock_acquired = threading.Event()
        second_done = threading.Event()
        second_error: list[BaseException] = []

        def _second_run() -> None:
            try:
                second_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(second_engine) as second_db:
                    _lock_household_initial_load(second_db, household_id)
                    # Only reachable once the first transaction below has
                    # committed and released the advisory lock.
                    second_lock_acquired.set()
                    second_account = second_db.get(Account, account_id)
                    _apply_obligations(
                        second_db, household_id, manifest, apply=True, user_id=user_id
                    )
                    _apply_balances(
                        second_db,
                        household_id,
                        manifest,
                        {"inv": second_account},
                        apply=True,
                        user_id=user_id,
                    )
                    second_db.commit()
                second_engine.dispose()
            except BaseException as exc:  # noqa: BLE001 - surfaced via assertion below
                second_error.append(exc)
            finally:
                second_done.set()

        with Session(engine) as first_db:
            _lock_household_initial_load(first_db, household_id)
            first_account = first_db.get(Account, account_id)
            _apply_obligations(first_db, household_id, manifest, apply=True, user_id=user_id)
            _apply_balances(
                first_db, household_id, manifest, {"inv": first_account}, apply=True, user_id=user_id
            )
            # Deliberately do not commit yet: the second connection must
            # block on `pg_advisory_xact_lock` for as long as this
            # transaction (and the lock it holds) stays open.
            worker = threading.Thread(target=_second_run, daemon=True)
            worker.start()
            still_blocked = not second_lock_acquired.wait(timeout=1.0)
            assert still_blocked, (
                "second connection acquired the advisory lock before the first "
                "transaction committed -- the lock is not actually serializing"
            )
            first_db.commit()

        assert second_done.wait(timeout=10.0), "second connection never finished"
        if second_error:
            raise second_error[0]

        with Session(engine) as verify_db:
            obligations = verify_db.scalars(
                select(Obligation).where(Obligation.household_id == household_id)
            ).all()
            balances = verify_db.scalars(
                select(AccountBalanceObservation).where(
                    AccountBalanceObservation.household_id == household_id
                )
            ).all()
            assert len(obligations) == 1
            assert len(balances) == 1
            assert obligations[0].amount == Decimal("1000.00")
            assert balances[0].amount == Decimal("5000.00")
            assert balances[0].source == "manual_confirmed"
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_card_payment_link_concurrent_confirmations_do_not_leave_asymmetric_pair() -> None:
    """PR #43 engineer review, secondary concurrency hardening: two
    concurrent human confirmations that both try to link the very same bank
    debit (`checking`) to two *different* card-side candidates must not
    both succeed. Before the `SELECT ... FOR UPDATE` fix,
    `link_card_payment` only checked `linked_transaction_id is None` in
    Python after a plain, non-locking read -- two overlapping calls could
    both pass that check before either wrote, and both commit, leaving
    `checking` pointing at whichever card row committed last while the
    *other* card row's `linked_transaction_id` still (wrongly) points back
    at `checking`: a non-symmetric pair, exactly what
    `test_link_is_symmetric_non_destructive_and_preserves_inv002` proves a
    *single* link never produces.

    This proves it under a real two-connection PostgreSQL race, not merely
    by reading the code: the first connection locks and links `checking` to
    `card_a` but deliberately holds the transaction open before commit; a
    second, fully concurrent connection attempts to link the same
    `checking` to a different `card_b` and must block on the row lock until
    the first commits, then observe the now-linked state and cleanly raise
    `CardPaymentLinkError` instead of overwriting it.
    """

    from app.services.card_payment_reconciliation import CardPaymentLinkError, link_card_payment

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        with Session(engine) as setup_db:
            synthetic = build_synthetic_household(setup_db)
            checking = synthetic.transactions["card_payment"]
            card_a = Transaction(
                household_id=synthetic.household.id,
                account_id=synthetic.credit_card.id,
                category_id=synthetic.categories["Conciliação"].id,
                booked_at=date(2026, 6, 1),
                occurred_at=date(2026, 6, 1),
                competence="2026-06",
                description="Pagamento em 15 JUN (A)",
                normalized_description="PAGAMENTO EM 15 JUN A",
                amount=Decimal("220.00"),
                transaction_type="reconciliation",
                fingerprint=f"pgconcura{uuid.uuid4().hex}".ljust(64, "0")[:64],
                source_priority=70,
                confidence=Decimal("1"),
                excluded=True,
            )
            card_b = Transaction(
                household_id=synthetic.household.id,
                account_id=synthetic.credit_card.id,
                category_id=synthetic.categories["Conciliação"].id,
                booked_at=date(2026, 6, 2),
                occurred_at=date(2026, 6, 2),
                competence="2026-06",
                description="Pagamento em 15 JUN (B)",
                normalized_description="PAGAMENTO EM 15 JUN B",
                amount=Decimal("220.00"),
                transaction_type="reconciliation",
                fingerprint=f"pgconcurb{uuid.uuid4().hex}".ljust(64, "0")[:64],
                source_priority=70,
                confidence=Decimal("1"),
                excluded=True,
            )
            setup_db.add_all([card_a, card_b])
            setup_db.commit()
            household_id = synthetic.household.id
            checking_id = checking.id
            card_a_id = card_a.id
            card_b_id = card_b.id

        second_blocked_confirmed = threading.Event()
        second_done = threading.Event()
        second_error: list[BaseException] = []
        second_result: list[str] = []

        def _second_attempt() -> None:
            try:
                second_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(second_engine) as second_db:
                    try:
                        link_card_payment(
                            second_db,
                            household_id=household_id,
                            checking_transaction_id=checking_id,
                            card_transaction_id=card_b_id,
                        )
                        second_db.commit()
                        second_result.append("linked")
                    except CardPaymentLinkError:
                        second_db.rollback()
                        second_result.append("rejected")
                second_engine.dispose()
            except BaseException as exc:  # noqa: BLE001 - surfaced via assertion below
                second_error.append(exc)
            finally:
                second_done.set()

        with Session(engine) as first_db:
            link_card_payment(
                first_db,
                household_id=household_id,
                checking_transaction_id=checking_id,
                card_transaction_id=card_a_id,
            )
            # Deliberately do not commit yet: the second connection's
            # `SELECT ... FOR UPDATE` on the same `checking` row must block
            # on PostgreSQL's row-level write lock for as long as this
            # transaction stays open.
            worker = threading.Thread(target=_second_attempt, daemon=True)
            worker.start()
            still_blocked = not second_done.wait(timeout=1.0)
            assert still_blocked, (
                "second connection completed before the first transaction "
                "committed -- the row lock is not actually serializing"
            )
            second_blocked_confirmed.set()
            first_db.commit()

        assert second_done.wait(timeout=10.0), "second connection never finished"
        if second_error:
            raise second_error[0]
        assert second_result == ["rejected"], (
            "second concurrent link must be rejected once it observes the "
            "first connection's committed link, never silently overwrite it"
        )

        with Session(engine) as verify_db:
            checking_row = verify_db.get(Transaction, checking_id)
            card_a_row = verify_db.get(Transaction, card_a_id)
            card_b_row = verify_db.get(Transaction, card_b_id)
            assert checking_row.linked_transaction_id == card_a_id
            assert card_a_row.linked_transaction_id == checking_id
            # The rejected candidate must remain completely untouched --
            # this is exactly the non-symmetric corruption the lock
            # prevents: `card_b` pointing at `checking` while `checking`
            # points at `card_a` instead.
            assert card_b_row.linked_transaction_id is None
        engine.dispose()
    finally:
        get_settings.cache_clear()
