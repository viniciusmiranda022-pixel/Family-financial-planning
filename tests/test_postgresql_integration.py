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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

os.environ.setdefault("SECRET_KEY", "postgres-integration-test-secret-not-used-in-production")
os.environ.setdefault("FILE_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("MFA_ENCRYPTION_KEY", "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=")
os.environ.setdefault("WHATSAPP_PHONE_ENCRYPTION_KEY", "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC=")

from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, func, inspect, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alembic import command  # noqa: E402
from app.api import _accept_timestep  # noqa: E402
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
    CaptureDraft,
    CaptureProcessingJob,
    Document,
    DocumentReconciliation,
    DuplicateGroup,
    FinancialSnapshot,
    Household,
    MfaFactor,
    Obligation,
    Transaction,
    User,
)
from app.services import capture_worker  # noqa: E402
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
        "notification_settings",
        "notification_recipients",
    }.issubset(tables)
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0025"
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
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0025"
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


def test_capture_job_finalize_cas_is_race_safe_on_real_postgresql() -> None:
    """`docs/WORK_ORDER_ASYNC_OCR_AUDIO_WORKER.md` review (second
    BLOQUEIO DE MERGE pass), item 1: `app.services.capture_worker.claim_capture_job`/
    `finalize_capture_job` deliberately use a plain atomic `UPDATE ...
    WHERE ...` compare-and-swap instead of `SELECT ... FOR UPDATE SKIP
    LOCKED` specifically so the exact same claim/finalize code is
    race-safe on both SQLite (the fast unit coverage in
    `tests/test_async_capture_worker.py`) and PostgreSQL (production) --
    see that module's docstring. This proves the property the SQLite
    suite already covers extensively (`test_stale_lease_cannot_finalize_after_being_superseded_by_a_reclaim`
    et al.) holds against the real engine too: a worker whose lease was
    superseded by a newer reclaim (`attempts` bumped, `status` unchanged)
    must not be able to finalize the job out from under the new lease,
    even with PostgreSQL's own MVCC/row-visibility semantics in the loop
    instead of SQLite's simpler single-writer locking.
    """

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        with Session(engine) as db:
            household = Household(name="Família CAS PostgreSQL")
            db.add(household)
            db.flush()
            draft = CaptureDraft(
                household_id=household.id,
                source_type="image",
                detected_type="auto",
                status="processing",
                processor="pending",
                proposal_json="[]",
            )
            db.add(draft)
            db.flush()
            job = CaptureProcessingJob(
                household_id=household.id,
                capture_draft_id=draft.id,
                document_id=None,
                job_type="ocr",
                content_type="image/png",
                status="queued",
                max_attempts=5,
            )
            db.add(job)
            db.commit()
            job_id = job.id

        # Worker A claims attempt 1 on its own connection/session.
        with Session(engine) as db_a:
            claimed_a = capture_worker.claim_capture_job(
                db_a, job_id, worker_id="worker-a", stale_after_seconds=600
            )
            assert claimed_a is not None
            assert claimed_a.attempts == 1
            claimed_attempt_a = claimed_a.attempts

        # A's lease goes stale; worker B reclaims on a separate
        # connection/session -- `status` stays `"processing"`, only
        # `attempts` moves to 2.
        with Session(engine) as db:
            job = db.get(CaptureProcessingJob, job_id)
            job.started_at = datetime.now(UTC) - timedelta(seconds=900)
            db.commit()
        with Session(engine) as db_b:
            claimed_b = capture_worker.claim_capture_job(
                db_b, job_id, worker_id="worker-b", stale_after_seconds=600
            )
            assert claimed_b is not None
            assert claimed_b.attempts == 2

        # A finally finishes and tries to finalize the attempt it
        # originally claimed: must lose against real PostgreSQL, exactly
        # like it does against SQLite.
        with Session(engine) as db_a2:
            won_a = capture_worker.finalize_capture_job(
                db_a2, job_id, status="completed", claimed_attempt=claimed_attempt_a
            )
            assert won_a is False
            db_a2.rollback()

        with Session(engine) as db:
            refreshed = db.get(CaptureProcessingJob, job_id)
            assert refreshed.status == "processing"  # still B's in-flight attempt
            assert refreshed.attempts == 2
            assert refreshed.claimed_by == "worker-b"

        # B, the legitimate owner of attempt 2, must still be able to
        # finalize for real.
        with Session(engine) as db_b2:
            won_b = capture_worker.finalize_capture_job(
                db_b2, job_id, status="completed", claimed_attempt=2
            )
            assert won_b is True
            db_b2.commit()

        with Session(engine) as db:
            refreshed = db.get(CaptureProcessingJob, job_id)
            assert refreshed.status == "completed"
    finally:
        engine.dispose()


def test_exhausted_reconciler_cas_is_race_safe_on_real_postgresql() -> None:
    """`docs/WORK_ORDER_ASYNC_OCR_AUDIO_WORKER.md` review (third BLOQUEIO
    DE MERGE pass): `app.services.capture_worker.fail_exhausted_stale_jobs`
    now finalizes an exhausted/stale job through the same `finalize_capture_job`
    `(status='processing', attempts=:claimed_attempt)` compare-and-swap the
    normal completion/cancellation paths already use, instead of an
    unconditional `UPDATE ... WHERE id = :id`. This proves that CAS holds
    under real PostgreSQL row-level locking, not just the SQLite unit
    coverage (`test_exhausted_reconciler_never_overwrites_a_worker_that_finalized_first`
    in `tests/test_async_capture_worker.py`, which simulates the
    interleaving via a monkeypatch injection point rather than genuinely
    overlapping transactions/connections).

    Two real, concurrent connections: the job's actual (slow, not crashed)
    worker finalizes its exhausted attempt on one connection but
    deliberately holds the transaction open before commit; a second,
    fully concurrent connection runs the crash-recovery reconciler
    (`fail_exhausted_stale_jobs`), which must block on PostgreSQL's row
    lock for that same job, then -- once the worker commits -- observe the
    now-`completed` row and lose its own CAS instead of overwriting it
    back to `failed`.
    """

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        with Session(engine) as setup_db:
            household = Household(name="Família Corrida Esgotada PostgreSQL")
            setup_db.add(household)
            setup_db.flush()
            draft = CaptureDraft(
                household_id=household.id,
                source_type="image",
                detected_type="auto",
                status="processing",
                processor="pending",
                proposal_json="[]",
            )
            setup_db.add(draft)
            setup_db.flush()
            document = Document(
                household_id=household.id,
                original_name="recibo-corrida-pg.png",
                document_type="smart_capture",
                sha256="f" * 64,
                encrypted_path="ignored-for-this-test",
                status="capture_processing",
            )
            setup_db.add(document)
            setup_db.flush()
            job = CaptureProcessingJob(
                household_id=household.id,
                capture_draft_id=draft.id,
                document_id=document.id,
                job_type="ocr",
                content_type="image/png",
                status="processing",
                attempts=3,
                max_attempts=3,
                started_at=datetime.now(UTC) - timedelta(seconds=900),
            )
            setup_db.add(job)
            setup_db.commit()
            job_id, draft_id, document_id = job.id, draft.id, document.id

        reconciler_done = threading.Event()
        reconciler_error: list[BaseException] = []
        reconciler_result: list[int] = []

        def _reconciler_attempt() -> None:
            try:
                reconciler_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(reconciler_engine) as reconciler_db:
                    failed_count = capture_worker.fail_exhausted_stale_jobs(
                        reconciler_db, stale_after_seconds=600
                    )
                    reconciler_db.commit()
                    reconciler_result.append(failed_count)
                reconciler_engine.dispose()
            except BaseException as exc:  # noqa: BLE001 - surfaced via assertion below
                reconciler_error.append(exc)
            finally:
                reconciler_done.set()

        with Session(engine) as worker_db:
            won = capture_worker.finalize_capture_job(
                worker_db, job_id, status="completed", claimed_attempt=3, duration_ms=1234
            )
            assert won is True
            # Deliberately do not commit yet: the reconciler's own
            # `finalize_capture_job` UPDATE for this same job must block on
            # PostgreSQL's row-level write lock for as long as this
            # transaction stays open.
            worker = threading.Thread(target=_reconciler_attempt, daemon=True)
            worker.start()
            still_blocked = not reconciler_done.wait(timeout=1.0)
            assert still_blocked, (
                "reconciler completed before the worker's transaction "
                "committed -- the row lock is not actually serializing"
            )
            worker_db.commit()

        assert reconciler_done.wait(timeout=10.0), "reconciler never finished"
        if reconciler_error:
            raise reconciler_error[0]
        # The reconciler's CAS must have lost the race -- it must not count
        # this job as one it failed.
        assert reconciler_result == [0]

        with Session(engine) as verify_db:
            job_row = verify_db.get(CaptureProcessingJob, job_id)
            draft_row = verify_db.get(CaptureDraft, draft_id)
            document_row = verify_db.get(Document, document_id)
            # The worker's legitimate terminal state stands, completely
            # undisturbed by the reconciler's lost attempt to fail it for
            # exhaustion.
            assert job_row.status == "completed"
            assert job_row.error_code is None
            assert job_row.duration_ms == 1234
            assert draft_row.status == "processing"  # never overwritten to "failed"
            assert document_row.status == "capture_processing"  # never overwritten
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_mfa_concurrent_totp_accept_of_same_timestep_yields_exactly_one_success_on_real_postgresql() -> None:
    """Engineer review of PR #83 (2026-09-11), BLOQUEIO DE MERGE item 2: the
    PR's own evidence claimed `tests/test_postgresql_integration.py`
    "additionally proves [anti-replay] under genuine thread concurrency
    against real PostgreSQL", but at that head this file only carried the
    `MFA_ENCRYPTION_KEY` env var and the Alembic head bump -- no such test
    existed. `tests/test_mfa.py::test_concurrent_accept_of_same_timestep_yields_at_most_one_success`
    only proves `app.api._accept_timestep` is a correct atomic CAS when
    called *sequentially* (`db_a.commit()` happens before `_accept_timestep`
    is even called on `db_b`) -- useful, portable coverage, but not a
    concurrency proof. The Work Order is explicit: "duas validações
    concorrentes do mesmo código resultam em no máximo uma aceitação."

    This proves it under a real two-connection PostgreSQL race, not merely
    by reading the code: the first connection accepts the timestep and
    deliberately holds its transaction open before commit; a second, fully
    concurrent connection attempts to accept the *exact same* timestep for
    the *exact same* factor and must block on PostgreSQL's row-level write
    lock for as long as the first transaction stays open, then observe the
    now-committed acceptance and correctly lose the race (`rowcount == 0`)
    instead of a lost update silently letting both "accept".
    """

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        with Session(engine) as setup_db:
            household = Household(name="Família MFA Concorrência PostgreSQL")
            setup_db.add(household)
            setup_db.flush()
            user = User(
                household_id=household.id,
                name="Usuário MFA Concorrência",
                username=f"mfa-concur-{household.id[:8]}",
                password_hash="not-a-real-password-hash",
                is_admin=True,
                active=True,
            )
            setup_db.add(user)
            setup_db.flush()
            factor = MfaFactor(
                user_id=user.id,
                secret_encrypted="not-a-real-encrypted-secret",
                confirmed_at=datetime.now(UTC),
            )
            setup_db.add(factor)
            setup_db.commit()
            factor_id = factor.id

        timestep = 424242  # arbitrary, shared by both concurrent attempts

        second_blocked_confirmed = threading.Event()
        second_done = threading.Event()
        second_error: list[BaseException] = []
        second_accepted: list[bool] = []

        def _second_attempt() -> None:
            try:
                second_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(second_engine) as second_db:
                    second_factor = second_db.get(MfaFactor, factor_id)
                    accepted = _accept_timestep(second_db, second_factor, timestep, pending=False)
                    second_db.commit()
                    second_accepted.append(accepted)
                second_engine.dispose()
            except BaseException as exc:  # noqa: BLE001 - surfaced via assertion below
                second_error.append(exc)
            finally:
                second_done.set()

        with Session(engine) as first_db:
            first_factor = first_db.get(MfaFactor, factor_id)
            first_accepted = _accept_timestep(first_db, first_factor, timestep, pending=False)
            assert first_accepted is True
            # Deliberately do not commit yet: the second connection's
            # `UPDATE ... WHERE ...` against the same row must block on
            # PostgreSQL's row-level write lock for as long as this
            # transaction stays open -- proving the two attempts genuinely
            # overlap instead of merely running one after the other.
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
        assert second_accepted == [False], (
            "the concurrent second acceptance of the exact same timestep must "
            "lose the race once it observes the first connection's commit"
        )

        with Session(engine) as verify_db:
            factor_row = verify_db.get(MfaFactor, factor_id)
            assert factor_row.last_accepted_timestep == timestep
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_assistant_execute_concurrent_same_proposal_is_serialized_to_a_single_mutation() -> None:
    """Engineering review of PR #92, second round: `AssistantActionProposal`
    must be single-use under real concurrency, not merely under one
    session's sequential retry (`tests/test_assistant_slice4.py` already
    proves the sequential-retry/atomic-rollback cases against SQLite). Two
    connections calling `execute_typed_action` for the *same* `proposal_id`
    at the same time must produce exactly one financial mutation (one
    `Transaction`) and exactly one `AssistantActionEvent` -- the loser is
    required to observe the winner's committed outcome and return
    `idempotent_replay=True`, never a second dispatch.

    This proves it under a real two-connection PostgreSQL race, not merely
    by reading the code: `execute_typed_action`'s initial
    `SELECT ... FOR UPDATE` of the proposal row (see its docstring) is
    exercised by pausing the *first* connection mid-transaction -- after it
    has already taken the row lock and dispatched the domain write, but
    before its own commit -- while a fully concurrent second connection
    attempts to execute the exact same `proposal_id`. The second must block
    on PostgreSQL's row-level write lock for as long as the first stays
    open, then observe the now-consumed proposal and cleanly replay instead
    of mutating a second time.
    """

    from app.models import AssistantActionEvent, AssistantActionProposal, AuditEvent
    from app.services.assistant_actions import (
        build_typed_action_proposal,
        execute_typed_action,
        persist_action_proposal,
    )
    from app.services.assistant_interpreter import StructuredInterpretation

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        with Session(engine) as setup_db:
            synthetic = build_synthetic_household(setup_db)
            synthetic.user.is_admin = True
            setup_db.commit()
            household_id = synthetic.household.id
            user_id = synthetic.user.id

        interpretation = StructuredInterpretation(
            available=True,
            reason=None,
            intent="create_expense",
            extracted_fields={
                "amount_text": "120",
                "description": "Mercado concorrência",
                "account_hint": "Conta Corrente",
                "funding_source_hint": "account",
                "date_text": "2026-09-05",
                "category_hint": "Alimentação",
            },
            missing_fields=(),
            clarifying_question=None,
            confidence=0.9,
        )
        with Session(engine) as propose_db:
            proposal = build_typed_action_proposal(
                propose_db, household_id=household_id, interpretation=interpretation
            )
            assert proposal.can_execute is True, proposal.clarifying_question
            proposal_id = persist_action_proposal(
                propose_db,
                household_id=household_id,
                user_id=user_id,
                trace_id=str(uuid.uuid4()),
                original_message="Gastei 120 no mercado",
                structured_interpretation=interpretation.to_dict(),
                proposal=proposal,
            )

        first_paused = threading.Event()
        release_first = threading.Event()
        first_done = threading.Event()
        first_result: list[dict] = []
        first_error: list[BaseException] = []

        def _first_attempt() -> None:
            try:
                first_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                # `autoflush=False`: a plain autoflush Session calls
                # `self.flush()` -- which resolves to the very same
                # monkeypatched method below -- before *every* query,
                # including the proposal's own locking `SELECT`. With
                # autoflush left on, the pause below fires before that
                # `SELECT ... FOR UPDATE` ever runs, so the row lock this
                # test means to hold is never actually taken.
                with Session(first_engine, autoflush=False) as first_db:
                    user = first_db.get(User, user_id)
                    real_flush = first_db.flush

                    def _pausing_flush(*args, **kwargs):
                        # By the time anything in `execute_typed_action`
                        # first calls `db.flush()`, the locked `SELECT ...
                        # FOR UPDATE` of the proposal has already run and
                        # the domain write has already been dispatched
                        # (`commit=False` never flushes on its own to get an
                        # id -- every id here is a client-side default) --
                        # pausing here holds the row lock open across the
                        # gap the second connection needs to race into.
                        first_paused.set()
                        release_first.wait(timeout=10.0)
                        return real_flush(*args, **kwargs)

                    first_db.flush = _pausing_flush
                    result = execute_typed_action(first_db, user=user, proposal_id=proposal_id)
                    first_result.append(result)
                first_engine.dispose()
            except BaseException as exc:  # noqa: BLE001 - surfaced via assertion below
                first_error.append(exc)
            finally:
                first_done.set()

        first_worker = threading.Thread(target=_first_attempt, daemon=True)
        first_worker.start()
        assert first_paused.wait(timeout=5.0), "first execute never reached its pause point"

        second_done = threading.Event()
        second_result: list[dict] = []
        second_error: list[BaseException] = []

        def _second_attempt() -> None:
            try:
                second_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(second_engine) as second_db:
                    user = second_db.get(User, user_id)
                    result = execute_typed_action(second_db, user=user, proposal_id=proposal_id)
                    second_result.append(result)
                second_engine.dispose()
            except BaseException as exc:  # noqa: BLE001 - surfaced via assertion below
                second_error.append(exc)
            finally:
                second_done.set()

        second_worker = threading.Thread(target=_second_attempt, daemon=True)
        second_worker.start()
        still_blocked = not second_done.wait(timeout=1.0)
        assert still_blocked, (
            "second execute completed before the first transaction committed -- "
            "the proposal's SELECT ... FOR UPDATE is not actually serializing"
        )

        release_first.set()
        assert first_done.wait(timeout=10.0), "first execute never finished"
        if first_error:
            raise first_error[0]
        assert second_done.wait(timeout=10.0), "second execute never finished"
        if second_error:
            raise second_error[0]

        assert first_result[0]["idempotent_replay"] is False
        assert second_result[0]["idempotent_replay"] is True
        assert second_result[0]["action"]["id"] == first_result[0]["action"]["id"]

        with Session(engine) as verify_db:
            # `build_synthetic_household` seeds several unrelated
            # transactions of its own for this same household -- scope the
            # count to the one description this typed action can possibly
            # have produced, not every row on the household.
            transactions = verify_db.scalars(
                select(Transaction).where(
                    Transaction.household_id == household_id,
                    Transaction.description == "Mercado concorrência",
                )
            ).all()
            assert len(transactions) == 1, "exactly one financial mutation, never two"
            assert transactions[0].amount == Decimal("-120.00")
            action_events = verify_db.scalars(select(AssistantActionEvent)).all()
            assert len(action_events) == 1, "exactly one AssistantActionEvent, never two"
            assistant_audit_events = verify_db.scalars(
                select(AuditEvent).where(AuditEvent.event_type == "assistant.execute")
            ).all()
            assert len(assistant_audit_events) == 1
            proposal_row = verify_db.get(AssistantActionProposal, proposal_id)
            assert proposal_row.consumed_at is not None
            assert proposal_row.consumed_action_event_id == action_events[0].id
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_investment_contribution_concurrent_same_funding_transaction_is_serialized_to_a_single_link() -> None:
    """Engineering review on PR #94, blocking item 3: the "already linked?"
    read and the `InvestmentValuation` insert in
    `_register_investment_contribution_impl` are two separate statements;
    without `lock_household_financial_revision` serializing them, two
    concurrent requests linking the *same* `funding_transaction_id` (even
    against two different investments) can both observe "not linked" and
    both commit a contribution, double-increasing historical cost against
    one real cash movement.

    Proves the fix under a real two-connection PostgreSQL race, mirroring
    `test_assistant_execute_concurrent_same_proposal_is_serialized_to_a_single_mutation`
    above: pausing the first connection mid-transaction (after it has taken
    the household-revision row lock and validated/dispatched the write, but
    before its own commit) while a fully concurrent second connection
    attempts to link the exact same funding transaction (to a *different*
    investment, proving this is a household-wide lock, not a
    per-investment one). The second must block on PostgreSQL's row-level
    write lock until the first commits, then observe the now-linked
    funding transaction and be refused with 409 -- never a second,
    silently-accepted link.
    """

    from fastapi import HTTPException

    from app.api import _register_investment_contribution_impl
    from app.models import Investment, InvestmentValuation
    from app.schemas import InvestmentContributionRequest

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        with Session(engine) as setup_db:
            synthetic = build_synthetic_household(setup_db)
            synthetic.user.is_admin = True
            household_id = synthetic.household.id
            user_id = synthetic.user.id
            # `investment_out` is `build_synthetic_household`'s existing
            # "Transferência patrimonial" cash-out (-1000.00, transfer,
            # category "Transferência patrimonial") -- exactly the shape
            # `_register_investment_contribution_impl` requires a funding
            # transaction to have; no need to fabricate a second one.
            funding_transaction_id = synthetic.transactions["investment_out"].id
            investment_a = Investment(
                household_id=household_id,
                name="Studio A",
                historical_cost=Decimal("30000.00"),
                current_value=Decimal("35000.00"),
                last_updated_at=date(2026, 6, 8),
            )
            investment_b = Investment(
                household_id=household_id,
                name="Studio B",
                historical_cost=Decimal("10000.00"),
                current_value=Decimal("12000.00"),
                last_updated_at=date(2026, 6, 8),
            )
            setup_db.add_all([investment_a, investment_b])
            setup_db.commit()
            investment_a_id = investment_a.id
            investment_b_id = investment_b.id

        payload = InvestmentContributionRequest(
            contribution_amount=Decimal("1000.00"),
            valuation_date=date(2026, 6, 8),
            funding_transaction_id=funding_transaction_id,
        )

        first_paused = threading.Event()
        release_first = threading.Event()
        first_done = threading.Event()
        first_result: list[dict] = []
        first_error: list[BaseException] = []

        def _first_attempt() -> None:
            try:
                first_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(first_engine, autoflush=False) as first_db:
                    user = first_db.get(User, user_id)
                    real_flush = first_db.flush

                    def _pausing_flush(*args, **kwargs):
                        # By the time `_register_investment_contribution_impl`
                        # first calls `db.flush()` (adding the new
                        # `InvestmentValuation`), it has already taken the
                        # household-revision row lock and passed the
                        # "already linked?" check -- pausing here holds
                        # that lock open across the gap the second
                        # connection needs to race into.
                        first_paused.set()
                        release_first.wait(timeout=10.0)
                        return real_flush(*args, **kwargs)

                    first_db.flush = _pausing_flush
                    result = _register_investment_contribution_impl(
                        investment_a_id, payload, user, first_db, commit=True
                    )
                    first_result.append(result)
                first_engine.dispose()
            except BaseException as exc:  # noqa: BLE001 - surfaced via assertion below
                first_error.append(exc)
            finally:
                first_done.set()

        first_worker = threading.Thread(target=_first_attempt, daemon=True)
        first_worker.start()
        assert first_paused.wait(timeout=5.0), "first contribution never reached its pause point"

        second_done = threading.Event()
        second_result: list[dict] = []
        second_error: list[BaseException] = []

        def _second_attempt() -> None:
            try:
                second_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(second_engine) as second_db:
                    user = second_db.get(User, user_id)
                    result = _register_investment_contribution_impl(
                        investment_b_id, payload, user, second_db, commit=True
                    )
                    second_result.append(result)
                second_engine.dispose()
            except BaseException as exc:  # noqa: BLE001 - the 409 below is asserted, not swallowed
                second_error.append(exc)
            finally:
                second_done.set()

        second_worker = threading.Thread(target=_second_attempt, daemon=True)
        second_worker.start()
        still_blocked = not second_done.wait(timeout=1.0)
        assert still_blocked, (
            "second contribution completed before the first transaction committed -- "
            "lock_household_financial_revision is not actually serializing"
        )

        release_first.set()
        assert first_done.wait(timeout=10.0), "first contribution never finished"
        if first_error:
            raise first_error[0]
        assert first_result[0]["funding_transaction_id"] == funding_transaction_id

        assert second_done.wait(timeout=10.0), "second contribution never finished"
        assert len(second_error) == 1, "second contribution must be refused, never silently linked"
        assert isinstance(second_error[0], HTTPException), second_error[0]
        assert second_error[0].status_code == 409
        assert not second_result, "no result may be returned for a refused contribution"

        with Session(engine) as verify_db:
            linked = verify_db.scalars(
                select(InvestmentValuation).where(
                    InvestmentValuation.household_id == household_id,
                    InvestmentValuation.funding_transaction_id == funding_transaction_id,
                    InvestmentValuation.invalidated_at.is_(None),
                )
            ).all()
            assert len(linked) == 1, "exactly one contribution may ever link this funding transaction"
            assert linked[0].investment_id == investment_a_id
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_fund_reference_quote_concurrent_ingestion_is_serialized_to_a_single_row() -> None:
    """Issue #85 (`docs/WORK_ORDER_PRIVILEGE_DI_CVM.md`): "Idempotent
    reruns for the same fund/reference date must not create duplicate
    financial facts." `app.services.privilege_valuation.ingest_latest_quote`
    relies on `fund_reference_quotes`' unique `(fund_cnpj,
    quota_reference_date)` constraint plus a `begin_nested()`/
    `IntegrityError` retry -- the same pattern
    `app.services.notification_scheduler.discover_due_deliveries` already
    uses for its own unique-constraint race. Proves it under a real
    two-connection PostgreSQL race: two threads, synchronized with a
    `threading.Barrier` to maximize transaction overlap, both attempt to
    ingest the exact same fund/date quote concurrently -- exactly one row
    may exist afterward, and neither call may raise."""

    from app.models import FundReferenceQuote
    from app.services import privilege_valuation as pv
    from app.services.cvm_client import CvmQuote, CvmResult

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        quote = CvmQuote(
            fund_cnpj=pv.TRACKED_FUND_CNPJ,
            quota_reference_date=date(2026, 9, 15),
            quota_value=Decimal("28.1234567890"),
            net_worth=Decimal("900000.00"),
            source_url="http://fixture/inf_diario_fi_202609.csv",
            ingestion_batch="202609",
        )

        class _FakeClient:
            configured = True

            def latest_quote(self, fund_cnpj, reference_month=None):
                return CvmResult(quote)

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _ingest() -> None:
            try:
                thread_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(thread_engine) as db:
                    barrier.wait(timeout=5.0)
                    pv.ingest_latest_quote(db, fund_cnpj=pv.TRACKED_FUND_CNPJ, client=_FakeClient())
                    db.commit()
                thread_engine.dispose()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=_ingest, daemon=True) for _ in range(2)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=10.0)

        assert not errors, f"concurrent ingestion must never raise: {errors}"
        with Session(engine) as verify_db:
            rows = verify_db.scalars(
                select(FundReferenceQuote).where(
                    FundReferenceQuote.fund_cnpj == pv.TRACKED_FUND_CNPJ,
                    FundReferenceQuote.quota_reference_date == date(2026, 9, 15),
                )
            ).all()
            assert len(rows) == 1, "concurrent ingestion of the same fund/date duplicated a quote row"
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_fund_valuation_concurrent_run_is_serialized_to_a_single_row() -> None:
    """Same idempotency guarantee as the ingestion test above, one layer up:
    two threads run `app.services.privilege_valuation.run_valuation_for_household`
    for the same household/fund/reference-date concurrently. Exactly one
    `FundValuation` row may exist afterward -- `fund_valuations`' unique
    `(household_id, fund_cnpj, quota_reference_date)` constraint plus this
    function's own `begin_nested()`/`IntegrityError` handling is what
    guarantees it, never application-level discipline alone."""

    from app.models import FundReferenceQuote, FundValuation
    from app.services import privilege_valuation as pv

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        with Session(engine) as setup_db:
            household = Household(name="Família Privilège CVM Concorrência")
            setup_db.add(household)
            setup_db.flush()
            setup_db.add(
                FundReferenceQuote(
                    fund_cnpj=pv.TRACKED_FUND_CNPJ,
                    quota_reference_date=date(2026, 9, 15),
                    quota_value=Decimal("28.1234567890"),
                    net_worth=Decimal("900000.00"),
                    provider="cvm_dados_abertos",
                    source_url="http://fixture/inf_diario_fi_202609.csv",
                    ingestion_batch="202609",
                    retrieved_at=datetime(2026, 9, 15, 8, 0, tzinfo=UTC),
                )
            )
            household_id = household.id
            account = Account(
                household_id=household_id, name="Privilege DI", institution="Itau", account_type="investment"
            )
            setup_db.add(account)
            setup_db.flush()
            observation = AccountBalanceObservation(
                household_id=household_id,
                account_id=account.id,
                amount=Decimal("40667.49"),
                as_of_date=date(2026, 9, 11),
                observation_type="point_in_time",
                source="manual_confirmed",
                confidence=Decimal("1.0000"),
                trace_id="trace-fund-valuation-concurrency",
            )
            setup_db.add(observation)
            setup_db.commit()
            pv.record_position_snapshot(
                setup_db,
                household_id=household_id,
                fund_cnpj=pv.TRACKED_FUND_CNPJ,
                units_held=Decimal("1445.12345678"),
                effective_date=date(2026, 9, 11),
                evidence_type="confirmed_balance_reconciliation",
                evidence_balance_observation_id=observation.id,
            )
            setup_db.commit()

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _run() -> None:
            try:
                thread_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(thread_engine) as db:
                    barrier.wait(timeout=5.0)
                    pv.run_valuation_for_household(
                        db, household_id=household_id, fund_cnpj=pv.TRACKED_FUND_CNPJ
                    )
                    db.commit()
                thread_engine.dispose()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=_run, daemon=True) for _ in range(2)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=10.0)

        assert not errors, f"concurrent valuation run must never raise: {errors}"
        with Session(engine) as verify_db:
            rows = verify_db.scalars(
                select(FundValuation).where(
                    FundValuation.household_id == household_id,
                    FundValuation.fund_cnpj == pv.TRACKED_FUND_CNPJ,
                    FundValuation.quota_reference_date == date(2026, 9, 15),
                )
            ).all()
            assert len(rows) == 1, "concurrent valuation run duplicated a valuation row"
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_whatsapp_inbound_event_concurrent_redelivery_is_serialized_to_a_single_event() -> None:
    """Engineering review on PR #103 (WA-01, issue #73), item 1:
    `app.whatsapp_gateway_app._process_one_message`'s `is_duplicate_message`
    pre-check is a plain SELECT, not the actual idempotency guarantee --
    two genuinely concurrent deliveries of the same `provider_message_id`
    can both pass it before either has committed. The real barrier is
    `uq_whatsapp_inbound_events_message_id`, enforced when
    `gateway.record_inbound_event` flushes; the fix wraps each message's
    processing in `db.begin_nested()` and catches the loser's
    `IntegrityError`, exactly the `begin_nested()`/`IntegrityError` idiom
    `app.services.notification_scheduler.discover_due_deliveries` and
    `app.services.privilege_valuation.ingest_latest_quote` already use for
    their own unique constraints.

    Proves it under a real two-connection PostgreSQL race: two threads,
    synchronized with a `threading.Barrier` to maximize transaction overlap,
    both process the exact same inbound message concurrently. Neither call
    may raise (previously: the loser's `record_inbound_event` INSERT could
    surface as an unhandled `IntegrityError` at the caller's later,
    request-wide `db.commit()`), exactly one `WhatsAppInboundEvent` row may
    exist afterward, and the pre-seeded rate-limit bucket must show exactly
    one accounted hit -- the loser's own rate-limit accounting must be
    rolled back together with its failed insert, not left double-counting a
    message that was never actually processed twice.
    """

    from app.models import WhatsAppAuthorizedNumber, WhatsAppInboundEvent, WhatsAppRateLimitBucket
    from app.services import whatsapp_gateway as gateway
    from app.whatsapp_gateway_app import _process_one_message

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        sender_digits = "5511999998888"
        phone_hash = gateway.hash_phone(sender_digits)
        with Session(engine) as setup_db:
            household = Household(name="Família WhatsApp Concorrência")
            setup_db.add(household)
            setup_db.flush()
            user = User(
                household_id=household.id,
                name="Usuário WhatsApp Concorrência",
                username=f"wa-concur-{household.id[:8]}",
                password_hash="not-a-real-password-hash",
                is_admin=True,
                active=True,
            )
            setup_db.add(user)
            setup_db.flush()
            setup_db.add(
                WhatsAppAuthorizedNumber(
                    household_id=household.id,
                    user_id=user.id,
                    phone_hash=phone_hash,
                    phone_encrypted=gateway.encrypt_phone(sender_digits),
                    phone_last4=gateway.phone_last4(sender_digits),
                )
            )
            # Pre-seed the rate-limit bucket so this test isolates the
            # message-dedup race from the separate bucket-creation race
            # covered by the test below.
            setup_db.add(
                WhatsAppRateLimitBucket(
                    bucket_key=phone_hash,
                    window_start=datetime.now(UTC),
                    count=5,
                )
            )
            setup_db.commit()

        message = gateway.NormalizedInboundMessage(
            provider_message_id="wa-msg-concurrent-redelivery",
            sender_digits=sender_digits,
        )

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _deliver() -> None:
            try:
                thread_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(thread_engine) as db:
                    barrier.wait(timeout=5.0)
                    _process_one_message(db, message, settings=get_settings())
                    db.commit()
                thread_engine.dispose()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=_deliver, daemon=True) for _ in range(2)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=10.0)

        assert not errors, f"concurrent redelivery must never raise: {errors}"
        with Session(engine) as verify_db:
            events = verify_db.scalars(
                select(WhatsAppInboundEvent).where(
                    WhatsAppInboundEvent.provider_message_id == "wa-msg-concurrent-redelivery"
                )
            ).all()
            assert len(events) == 1, "concurrent redelivery duplicated the inbound event row"
            # WA-03: this message carries no `text` (`NormalizedInboundMessage`
            # default), so it reaches `_run_assistant_reply`'s no-content
            # branch and its terminal status becomes "unsupported_content",
            # not the original WA-01 claim status "accepted" -- the
            # concurrency guarantee under test (exactly one row, exactly one
            # rate-limit hit accounted) is unaffected by that later update.
            assert events[0].status == "unsupported_content"

            bucket = verify_db.scalar(
                select(WhatsAppRateLimitBucket).where(WhatsAppRateLimitBucket.bucket_key == phone_hash)
            )
            assert bucket.count == 6, (
                "the losing racer's rate-limit accounting for what turned out to be a "
                "duplicate must roll back with its failed insert, not double-count a "
                "message that was only ever processed once"
            )
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_whatsapp_rate_limit_bucket_concurrent_first_hit_is_serialized_to_a_single_bucket() -> None:
    """Engineering review on PR #103 (WA-01, issue #73), item 2:
    `SELECT ... FOR UPDATE` cannot lock a row that does not exist yet, so a
    brand-new sender's very first two messages arriving concurrently can
    both observe no `WhatsAppRateLimitBucket` row for their shared
    `bucket_key` and both attempt the INSERT. The fix wraps that INSERT in
    `db.begin_nested()`/`IntegrityError` and, on losing the race, re-selects
    `FOR UPDATE` (now that the winner's row exists) and joins the normal
    locked-row accounting instead of short-circuiting `True` unaccounted.

    Proves it under a real two-connection PostgreSQL race: two threads,
    synchronized with a `threading.Barrier`, each process a *different*
    first message from the same brand-new sender concurrently (isolating
    this bucket-creation race from the message-dedup race covered by the
    test above -- two distinct `provider_message_id`s, so
    `uq_whatsapp_inbound_events_message_id` never comes into play). Neither
    call may raise, exactly one bucket row may exist for that sender
    afterward, its count must reflect both legitimate hits, and both
    messages must each get their own accepted inbound event.
    """

    from app.models import WhatsAppAuthorizedNumber, WhatsAppInboundEvent, WhatsAppRateLimitBucket
    from app.services import whatsapp_gateway as gateway
    from app.whatsapp_gateway_app import _process_one_message

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        sender_digits = "5511988887777"
        phone_hash = gateway.hash_phone(sender_digits)
        with Session(engine) as setup_db:
            household = Household(name="Família WhatsApp Bucket Concorrência")
            setup_db.add(household)
            setup_db.flush()
            user = User(
                household_id=household.id,
                name="Usuário WhatsApp Bucket Concorrência",
                username=f"wa-bucket-concur-{household.id[:8]}",
                password_hash="not-a-real-password-hash",
                is_admin=True,
                active=True,
            )
            setup_db.add(user)
            setup_db.flush()
            setup_db.add(
                WhatsAppAuthorizedNumber(
                    household_id=household.id,
                    user_id=user.id,
                    phone_hash=phone_hash,
                    phone_encrypted=gateway.encrypt_phone(sender_digits),
                    phone_last4=gateway.phone_last4(sender_digits),
                )
            )
            setup_db.commit()

        messages = [
            gateway.NormalizedInboundMessage(
                provider_message_id=f"wa-msg-bucket-race-{i}", sender_digits=sender_digits
            )
            for i in range(2)
        ]

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _deliver(message: gateway.NormalizedInboundMessage) -> None:
            try:
                thread_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(thread_engine) as db:
                    barrier.wait(timeout=5.0)
                    _process_one_message(db, message, settings=get_settings())
                    db.commit()
                thread_engine.dispose()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=_deliver, args=(m,), daemon=True) for m in messages]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=10.0)

        assert not errors, f"concurrent first-hit accounting must never raise: {errors}"
        with Session(engine) as verify_db:
            buckets = verify_db.scalars(
                select(WhatsAppRateLimitBucket).where(WhatsAppRateLimitBucket.bucket_key == phone_hash)
            ).all()
            assert len(buckets) == 1, "concurrent first hits duplicated the rate-limit bucket row"
            assert buckets[0].count == 2, "both legitimate concurrent hits must be accounted for"

            events = verify_db.scalars(
                select(WhatsAppInboundEvent).where(
                    WhatsAppInboundEvent.provider_message_id.in_(
                        [m.provider_message_id for m in messages]
                    )
                )
            ).all()
            assert len(events) == 2, "each distinct message must still get its own inbound event"
            # WA-03: no `text` on either message -> terminal status is
            # "unsupported_content" (see the sibling redelivery test above);
            # the bucket-creation race under test is unaffected.
            assert {event.status for event in events} == {"unsupported_content"}
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_whatsapp_webhook_concurrent_confirm_from_two_messages_serializes_to_one_mutation(monkeypatch) -> None:
    """WA-03 (`docs/WORK_ORDER_WA_03.md`, issue #75), Work Order item 7/
    acceptance criterion "duplicidade por retry é bloqueada" and item 10
    ("concorrência de confirmação"): two *distinct* WhatsApp messages (e.g.
    the household member tapping "confirmar" twice, or a mobile client
    retrying a slow send) that both resolve to the same pending
    `AssistantActionProposal` must still produce exactly one financial
    mutation -- this is a different race than
    `test_assistant_execute_concurrent_same_proposal_is_serialized_to_a_single_mutation`
    above (which drives `execute_typed_action` directly): here, two
    completely independent `app.whatsapp_gateway_app._process_one_message`
    calls -- each with its own `provider_message_id`, each running the full
    `plan_and_execute` Tool Layer round-trip (`confirm_typed_action` ->
    `_find_pending_proposal` -> `execute_typed_action`) -- race on the
    *lookup and dispatch* of the same underlying proposal, not merely on a
    caller-supplied `proposal_id`. `_find_pending_proposal`'s own `SELECT`
    (no locking) can let both callers resolve the same still-unconsumed
    proposal id before either reaches `execute_typed_action`'s
    `SELECT ... FOR UPDATE` -- this test proves that later lock still
    collapses both attempts to a single dispatch even though the race began
    one layer higher than the lock itself.
    """

    from app.models import (
        AssistantActionProposal,
        Category,
        FinancialProfile,
        WhatsAppAuthorizedNumber,
        WhatsAppInboundEvent,
    )
    from app.services import whatsapp_gateway as gateway
    from app.services.assistant_actions import build_typed_action_proposal, persist_action_proposal
    from app.services.assistant_interpreter import StructuredInterpretation
    from app.services.codex_client import CodexAdvisorClient, CodexResult
    from app.whatsapp_gateway_app import _process_one_message

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        # No `WHATSAPP_APP_SECRET`/`WHATSAPP_VERIFY_TOKEN`/`WHATSAPP_GATEWAY_ENABLED`
        # needed: this test drives `_process_one_message` directly, the same
        # layer the sibling WA-01 concurrency tests above exercise, never
        # `receive_webhook`'s own signature/`_ready()` gate.
        sender_digits = "5511966665555"
        phone_hash = gateway.hash_phone(sender_digits)
        with Session(engine) as setup_db:
            household = Household(name="Família WhatsApp Confirmação Concorrência")
            setup_db.add(household)
            setup_db.flush()
            household_id = household.id
            setup_db.add(FinancialProfile(household_id=household_id, monthly_cash_cap=Decimal("5000")))
            setup_db.add(Account(household_id=household_id, name="Conta Corrente", account_type="checking"))
            setup_db.add(Category(household_id=household_id, name="Mercado"))
            user = User(
                household_id=household_id,
                name="Usuário WhatsApp Confirmação Concorrência",
                username=f"wa-confirm-concur-{household_id[:8]}",
                password_hash="not-a-real-password-hash",
                is_admin=True,
                active=True,
            )
            setup_db.add(user)
            setup_db.flush()
            user_id = user.id
            setup_db.add(
                WhatsAppAuthorizedNumber(
                    household_id=household_id,
                    user_id=user_id,
                    phone_hash=phone_hash,
                    phone_encrypted=gateway.encrypt_phone(sender_digits),
                    phone_last4=gateway.phone_last4(sender_digits),
                )
            )
            setup_db.commit()

        interpretation = StructuredInterpretation(
            available=True,
            reason=None,
            intent="create_expense",
            extracted_fields={
                "amount_text": "120",
                "description": "Combustível concorrência WhatsApp",
                "account_hint": "Conta Corrente",
                "funding_source_hint": "account",
                "category_hint": "Mercado",
            },
            missing_fields=(),
            clarifying_question=None,
            confidence=0.9,
        )
        with Session(engine) as propose_db:
            proposal = build_typed_action_proposal(
                propose_db, household_id=household_id, interpretation=interpretation
            )
            assert proposal.can_execute is True, proposal.clarifying_question
            proposal_id = persist_action_proposal(
                propose_db,
                household_id=household_id,
                user_id=user_id,
                trace_id=str(uuid.uuid4()),
                original_message="Gastei 120 de combustível",
                structured_interpretation=interpretation.to_dict(),
                proposal=proposal,
            )

        def _confirm_plan(_self, _payload: dict) -> CodexResult:
            return CodexResult(
                {
                    "schema_version": "1.0.0",
                    "needs_clarification": False,
                    "clarifying_question": None,
                    "steps": [{"tool": "confirm_typed_action", "arguments": {}}],
                    "model": "modelo-de-teste",
                }
            )

        monkeypatch.setattr(CodexAdvisorClient, "configured", property(lambda _self: True))
        monkeypatch.setattr(CodexAdvisorClient, "plan", _confirm_plan)

        messages = [
            gateway.NormalizedInboundMessage(
                provider_message_id=f"wa-confirm-race-{i}", sender_digits=sender_digits, text="confirmo"
            )
            for i in range(2)
        ]

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _deliver(message: gateway.NormalizedInboundMessage) -> None:
            try:
                thread_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(thread_engine) as db:
                    barrier.wait(timeout=5.0)
                    _process_one_message(db, message, settings=get_settings())
                thread_engine.dispose()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=_deliver, args=(m,), daemon=True) for m in messages]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=10.0)

        assert not errors, f"concurrent confirmation must never raise: {errors}"
        with Session(engine) as verify_db:
            transactions = verify_db.scalars(
                select(Transaction).where(
                    Transaction.household_id == household_id,
                    Transaction.description == "Combustível concorrência WhatsApp",
                )
            ).all()
            assert len(transactions) == 1, "concurrent confirmation from two messages must mutate exactly once"

            proposal_row = verify_db.get(AssistantActionProposal, proposal_id)
            assert proposal_row.consumed_at is not None
            assert proposal_row.consumed_action_event_id is not None

            events = verify_db.scalars(
                select(WhatsAppInboundEvent).where(
                    WhatsAppInboundEvent.provider_message_id.in_(
                        [m.provider_message_id for m in messages]
                    )
                )
            ).all()
            assert len(events) == 2, "each confirmation message still gets its own inbound event"
            assert {event.status for event in events} == {"processed"}
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_whatsapp_webhook_concurrent_undo_from_two_messages_reverses_exactly_once(monkeypatch) -> None:
    """WA-03 (`docs/WORK_ORDER_WA_03.md`, issue #75), engineering review of
    PR #105 (MERGE BLOCKED comment): `undo_assistant_action` must serialize
    under real concurrency exactly like `execute_typed_action`/
    `cancel_action_proposal` already do -- two *distinct* WhatsApp
    "desfaz"/"desfazer" messages (e.g. a household member tapping undo
    twice) that both resolve to the same undoable `AssistantActionEvent`
    must reverse it exactly once, never twice.

    This exercises the exact stale-identity-map scenario the review called
    out, not merely the row lock in isolation:
    `app.services.assistant_tools._find_undoable_action` already runs an
    *unlocked* `SELECT` of the `AssistantActionEvent` row -- in the same
    session -- immediately before `undo_assistant_action`'s own locked
    `SELECT ... FOR UPDATE`, the identical shape that
    `test_whatsapp_webhook_concurrent_confirm_from_two_messages_serializes_to_one_mutation`
    above already proves matters for confirmation. Without
    `populate_existing()` on the locked re-`SELECT`, the second message's
    session would unblock from the lock and still read its own stale,
    pre-lock `undone_at IS NULL` from the identity map instead of the
    winner's just-committed `undone_at`, and both would dispatch
    `_delete_manual_transaction_impl` against the same `Transaction` --
    the second deleting an already-deleted row.
    """

    from app.models import (
        AssistantActionEvent,
        AuditEvent,
        Category,
        FinancialProfile,
        Transaction,
        WhatsAppAuthorizedNumber,
        WhatsAppInboundEvent,
    )
    from app.services import whatsapp_gateway as gateway
    from app.services.assistant_actions import (
        build_typed_action_proposal,
        execute_typed_action,
        persist_action_proposal,
    )
    from app.services.assistant_interpreter import StructuredInterpretation
    from app.services.codex_client import CodexAdvisorClient, CodexResult
    from app.whatsapp_gateway_app import _process_one_message

    command.upgrade(_alembic_config(), "head")
    engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        sender_digits = "5511966664444"
        phone_hash = gateway.hash_phone(sender_digits)
        with Session(engine) as setup_db:
            household = Household(name="Família WhatsApp Undo Concorrência")
            setup_db.add(household)
            setup_db.flush()
            household_id = household.id
            setup_db.add(FinancialProfile(household_id=household_id, monthly_cash_cap=Decimal("5000")))
            setup_db.add(Account(household_id=household_id, name="Conta Corrente", account_type="checking"))
            setup_db.add(Category(household_id=household_id, name="Mercado"))
            user = User(
                household_id=household_id,
                name="Usuário WhatsApp Undo Concorrência",
                username=f"wa-undo-concur-{household_id[:8]}",
                password_hash="not-a-real-password-hash",
                is_admin=True,
                active=True,
            )
            setup_db.add(user)
            setup_db.flush()
            user_id = user.id
            setup_db.add(
                WhatsAppAuthorizedNumber(
                    household_id=household_id,
                    user_id=user_id,
                    phone_hash=phone_hash,
                    phone_encrypted=gateway.encrypt_phone(sender_digits),
                    phone_last4=gateway.phone_last4(sender_digits),
                )
            )
            setup_db.commit()

        # Establish one real, undoable AssistantActionEvent directly through
        # the Tool Layer's own dispatcher -- the create/confirm path itself
        # is already proven above; this test's subject is solely the undo
        # race that follows.
        interpretation = StructuredInterpretation(
            available=True,
            reason=None,
            intent="create_expense",
            extracted_fields={
                "amount_text": "80",
                "description": "Combustível concorrência undo WhatsApp",
                "account_hint": "Conta Corrente",
                "funding_source_hint": "account",
                "category_hint": "Mercado",
            },
            missing_fields=(),
            clarifying_question=None,
            confidence=0.9,
        )
        with Session(engine) as setup_db:
            proposal = build_typed_action_proposal(
                setup_db, household_id=household_id, interpretation=interpretation
            )
            assert proposal.can_execute is True, proposal.clarifying_question
            proposal_id = persist_action_proposal(
                setup_db,
                household_id=household_id,
                user_id=user_id,
                trace_id=str(uuid.uuid4()),
                original_message="Gastei 80 de combustível",
                structured_interpretation=interpretation.to_dict(),
                proposal=proposal,
            )
            user_row = setup_db.get(User, user_id)
            execute_typed_action(setup_db, user=user_row, proposal_id=proposal_id)

        def _undo_plan(_self, _payload: dict) -> CodexResult:
            return CodexResult(
                {
                    "schema_version": "1.0.0",
                    "needs_clarification": False,
                    "clarifying_question": None,
                    "steps": [{"tool": "undo_typed_action", "arguments": {}}],
                    "model": "modelo-de-teste",
                }
            )

        monkeypatch.setattr(CodexAdvisorClient, "configured", property(lambda _self: True))
        monkeypatch.setattr(CodexAdvisorClient, "plan", _undo_plan)

        messages = [
            gateway.NormalizedInboundMessage(
                provider_message_id=f"wa-undo-race-{i}", sender_digits=sender_digits, text="desfaz essa ultima acao"
            )
            for i in range(2)
        ]

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _deliver(message: gateway.NormalizedInboundMessage) -> None:
            try:
                thread_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
                with Session(thread_engine) as db:
                    barrier.wait(timeout=5.0)
                    _process_one_message(db, message, settings=get_settings())
                thread_engine.dispose()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=_deliver, args=(m,), daemon=True) for m in messages]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=10.0)

        assert not errors, f"concurrent undo must never raise: {errors}"
        with Session(engine) as verify_db:
            remaining = verify_db.scalars(
                select(Transaction).where(
                    Transaction.household_id == household_id,
                    Transaction.description == "Combustível concorrência undo WhatsApp",
                )
            ).all()
            assert remaining == [], "the transaction must be reversed exactly once, never left half-undone"

            action_events = verify_db.scalars(
                select(AssistantActionEvent)
                .join(AuditEvent, AuditEvent.id == AssistantActionEvent.audit_event_id)
                .where(AuditEvent.household_id == household_id)
            ).all()
            assert len(action_events) == 1, "undo reverses the existing action, it never creates a new one"
            assert action_events[0].undone_at is not None
            assert action_events[0].undone_by == user_id

            undo_audit_events = verify_db.scalars(
                select(AuditEvent).where(
                    AuditEvent.household_id == household_id, AuditEvent.event_type == "assistant.undo"
                )
            ).all()
            assert len(undo_audit_events) == 1, "exactly one reversal effect, never two"

            events = verify_db.scalars(
                select(WhatsAppInboundEvent).where(
                    WhatsAppInboundEvent.provider_message_id.in_([m.provider_message_id for m in messages])
                )
            ).all()
            assert len(events) == 2, "each undo message still gets its own inbound event"
            # The winner reverses the action ("processed"); the loser's
            # locked, populate_existing() re-read observes the now-committed
            # `undone_at` and fails closed with a clean clarifying reply
            # ("Esta ação já foi desfeita") instead of raising or double
            # -reversing.
            assert {event.status for event in events} == {"processed", "needs_clarification"}
        engine.dispose()
    finally:
        get_settings.cache_clear()
