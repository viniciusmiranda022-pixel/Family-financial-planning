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
    DocumentSeed,
    InitialLoadManifest,
    _apply_documents,
)
from app.config import get_settings  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Document,
    DocumentReconciliation,
    DuplicateGroup,
    FinancialSnapshot,
    Household,
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
