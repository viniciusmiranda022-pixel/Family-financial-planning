from io import StringIO
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from alembic import command
from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_0001_COLUMNS = {
    "accounts": (
        "id",
        "household_id",
        "name",
        "institution",
        "account_type",
        "owner_label",
        "last_four",
        "active",
        "created_at",
        "updated_at",
    ),
    "audit_events": (
        "id",
        "household_id",
        "user_id",
        "event_type",
        "entity_type",
        "entity_id",
        "details",
        "created_at",
    ),
    "categories": (
        "id",
        "household_id",
        "name",
        "color",
        "cash_cap",
        "essential",
        "created_at",
        "updated_at",
    ),
    "commissions": (
        "id",
        "household_id",
        "description",
        "expected_date",
        "gross_amount",
        "tax_rate",
        "delay_days",
        "status",
        "received_date",
        "created_at",
        "updated_at",
    ),
    "documents": (
        "id",
        "household_id",
        "account_id",
        "original_name",
        "document_type",
        "sha256",
        "encrypted_path",
        "status",
        "record_count",
        "notes",
        "created_at",
        "updated_at",
    ),
    "financial_profiles": (
        "id",
        "household_id",
        "monthly_salary_net",
        "monthly_cash_cap",
        "emergency_floor",
        "food_allowance",
        "meal_allowance_daily",
        "workdays_month",
        "investment_name",
        "investment_balance",
        "investment_gross_annual_rate",
        "investment_income_tax_rate",
        "projection_end",
        "created_at",
        "updated_at",
    ),
    "households": ("id", "name", "created_at", "updated_at"),
    "obligations": (
        "id",
        "household_id",
        "name",
        "due_date",
        "amount",
        "recurrence_months",
        "occurrence_count",
        "category",
        "active",
        "created_at",
        "updated_at",
    ),
    "payroll_records": (
        "id",
        "household_id",
        "document_id",
        "person_name",
        "competence",
        "payment_date",
        "payroll_kind",
        "gross_amount",
        "deductions",
        "net_amount",
        "payroll_loan",
        "notes",
        "created_at",
        "updated_at",
    ),
    "review_items": (
        "id",
        "household_id",
        "transaction_id",
        "document_id",
        "reason",
        "details",
        "status",
        "resolved_at",
        "created_at",
        "updated_at",
    ),
    "transactions": (
        "id",
        "household_id",
        "account_id",
        "document_id",
        "category_id",
        "booked_at",
        "description",
        "normalized_description",
        "amount",
        "transaction_type",
        "owner_label",
        "card_last_four",
        "installment_current",
        "installment_total",
        "fingerprint",
        "source_line",
        "confidence",
        "excluded",
        "possible_duplicate",
        "reviewed",
        "created_at",
        "updated_at",
    ),
    "users": (
        "id",
        "household_id",
        "name",
        "username",
        "password_hash",
        "is_admin",
        "active",
        "created_at",
        "updated_at",
    ),
}

EXPECTED_0001_INDEXES = {
    "accounts": {"ix_accounts_household_id"},
    "audit_events": {"ix_audit_events_created_at", "ix_audit_events_household_id"},
    "categories": {"ix_categories_household_id"},
    "commissions": {"ix_commissions_household_id"},
    "documents": {"ix_documents_household_id", "ix_documents_sha256"},
    "financial_profiles": {"ix_financial_profiles_household_id"},
    "obligations": {"ix_obligations_household_id"},
    "payroll_records": {"ix_payroll_records_household_id"},
    "review_items": {"ix_review_items_household_id"},
    "transactions": {
        "ix_transaction_fingerprint",
        "ix_transaction_household_date",
        "ix_transactions_household_id",
        "ix_transactions_normalized_description",
    },
    "users": {"ix_users_household_id", "ix_users_username"},
}

EXPECTED_0003_TABLES = {"integrity_runs", "integrity_findings"}
EXPECTED_0004_TABLES = {
    "account_balance_observations",
    "classification_rules",
    "document_reconciliations",
    "duplicate_group_members",
    "duplicate_groups",
}

EXPECTED_0004_TRANSACTION_COLUMNS = {
    "occurred_at",
    "competence",
    "classification_source",
    "classification_version",
    "canonical_status",
    "duplicate_group_id",
    "linked_transaction_id",
    "transfer_group_id",
    "trace_id",
    "source_priority",
}

EXPECTED_INTEGRITY_RUN_COLUMNS = {
    "id",
    "household_id",
    "scope",
    "scope_entity_type",
    "scope_entity_id",
    "period",
    "status",
    "trigger",
    "financial_rules_version",
    "calculation_version",
    "started_at",
    "completed_at",
    "duration_ms",
    "summary",
    "error_code",
    "trace_id",
    "created_by",
}

EXPECTED_INTEGRITY_FINDING_COLUMNS = {
    "id",
    "household_id",
    "run_id",
    "invariant_id",
    "fingerprint",
    "financial_rules_version",
    "trace_id",
    "source",
    "status",
    "check_status",
    "severity",
    "scope",
    "entity_type",
    "entity_id",
    "period",
    "title",
    "message",
    "expected_amount",
    "actual_amount",
    "difference_amount",
    "expected",
    "actual",
    "metadata",
    "recommended_action",
    "first_seen_at",
    "last_seen_at",
    "occurrence_count",
    "acknowledged_at",
    "acknowledged_by",
    "resolved_at",
    "resolved_by",
    "resolution_reason",
    "created_at",
    "updated_at",
}


def _alembic_config(monkeypatch, database_url: str, *, output_buffer=None) -> Config:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SECRET_KEY", "migration-test-secret-that-is-long-enough")
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    get_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output_buffer)
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _inspect(database_url: str):
    engine = create_engine(database_url)
    return engine, inspect(engine)


def test_initial_migration_does_not_import_live_orm_metadata() -> None:
    source = (ROOT / "alembic/versions/0001_initial_schema.py").read_text(encoding="utf-8")
    assert "Base.metadata" not in source
    assert "from app" not in source
    assert "import models" not in source


def test_migrations_upgrade_and_downgrade_without_schema_drift(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'migrations.sqlite'}"
    config = _alembic_config(monkeypatch, database_url)

    command.upgrade(config, "0001")
    engine, inspector = _inspect(database_url)
    assert set(inspector.get_table_names()) == {*EXPECTED_0001_COLUMNS, "alembic_version"}
    for table, expected_columns in EXPECTED_0001_COLUMNS.items():
        assert tuple(column["name"] for column in inspector.get_columns(table)) == expected_columns
    for table, expected_indexes in EXPECTED_0001_INDEXES.items():
        assert {index["name"] for index in inspector.get_indexes(table)} == expected_indexes
    assert "capture_drafts" not in inspector.get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    engine, inspector = _inspect(database_url)
    assert set(inspector.get_table_names()) == {
        *EXPECTED_0001_COLUMNS,
        *EXPECTED_0003_TABLES,
        *EXPECTED_0004_TABLES,
        "capture_drafts",
        "alembic_version",
    }
    assert {index["name"] for index in inspector.get_indexes("capture_drafts")} == {
        "ix_capture_drafts_household_id",
        "ix_capture_household_created",
    }
    assert {column["name"] for column in inspector.get_columns("integrity_runs")} == (
        EXPECTED_INTEGRITY_RUN_COLUMNS
    )
    assert {column["name"] for column in inspector.get_columns("integrity_findings")} == (
        EXPECTED_INTEGRITY_FINDING_COLUMNS
    )
    assert {
        "before_state",
        "after_state",
        "reason",
        "trace_id",
        "source",
        "request_id",
    }.issubset({column["name"] for column in inspector.get_columns("audit_events")})
    assert {index["name"] for index in inspector.get_indexes("integrity_runs")} == {
        "ix_integrity_runs_household_id",
        "ix_integrity_runs_household_period",
        "ix_integrity_runs_household_status",
        "ix_integrity_runs_trace_id",
    }
    assert {index["name"] for index in inspector.get_indexes("integrity_findings")} == {
        "ix_integrity_findings_household_fingerprint",
        "ix_integrity_findings_household_id",
        "ix_integrity_findings_household_invariant",
        "ix_integrity_findings_household_period",
        "ix_integrity_findings_household_severity",
        "ix_integrity_findings_household_status",
        "ix_integrity_findings_run_id",
        "ix_integrity_findings_trace_id",
    }
    assert EXPECTED_0004_TRANSACTION_COLUMNS.issubset(
        {column["name"] for column in inspector.get_columns("transactions")}
    )
    assert {
        "ix_transaction_household_competence",
        "ix_transactions_duplicate_group_id",
        "ix_transactions_trace_id",
    }.issubset({index["name"] for index in inspector.get_indexes("transactions")})
    assert {index["name"] for index in inspector.get_indexes("duplicate_groups")} == {
        "ix_duplicate_groups_household_id",
        "ix_duplicate_groups_household_status",
    }
    assert {index["name"] for index in inspector.get_indexes("classification_rules")} == {
        "ix_classification_rules_category_id",
        "ix_classification_rules_household_id",
        "ix_classification_rules_household_merchant",
    }
    engine.dispose()

    command.downgrade(config, "0001")
    engine, inspector = _inspect(database_url)
    assert set(inspector.get_table_names()) == {*EXPECTED_0001_COLUMNS, "alembic_version"}
    engine.dispose()

    command.downgrade(config, "base")
    engine, inspector = _inspect(database_url)
    assert inspector.get_table_names() == ["alembic_version"]
    engine.dispose()
    get_settings.cache_clear()


def test_migrations_render_valid_postgresql_ddl_offline(monkeypatch) -> None:
    output = StringIO()
    config = _alembic_config(
        monkeypatch,
        "postgresql+psycopg://family:family@db:5432/family_finance",
        output_buffer=output,
    )
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE households" in sql
    assert "CREATE TABLE capture_drafts" in sql
    assert "CREATE TABLE integrity_runs" in sql
    assert "CREATE TABLE integrity_findings" in sql
    assert "CREATE TABLE document_reconciliations" in sql
    assert "CREATE TABLE account_balance_observations" in sql
    assert "CREATE TABLE duplicate_groups" in sql
    assert "CREATE TABLE duplicate_group_members" in sql
    assert "CREATE TABLE classification_rules" in sql
    assert "ALTER TABLE audit_events ADD COLUMN before_state JSONB" in sql
    assert "CREATE TABLE transactions" in sql
    assert "INSERT INTO alembic_version" in sql
    get_settings.cache_clear()


def test_integrity_core_upgrade_preserves_existing_financial_and_audit_rows(
    monkeypatch, tmp_path
) -> None:
    database_url = f"sqlite:///{tmp_path / 'existing-0002.sqlite'}"
    config = _alembic_config(monkeypatch, database_url)
    command.upgrade(config, "0002")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO households (id, name) VALUES (?, ?)",
            ("household-preserved", "Família preservada"),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO audit_events
                (id, household_id, event_type, details)
            VALUES (?, ?, ?, ?)
            """,
            (
                "audit-preserved",
                "household-preserved",
                "legacy.event",
                '{"preserved": true}',
            ),
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT name FROM households WHERE id = 'household-preserved'"
        ).scalar_one() == "Família preservada"
        audit_row = connection.exec_driver_sql(
            """
            SELECT details, before_state, after_state, reason, trace_id
            FROM audit_events WHERE id = 'audit-preserved'
            """
        ).one()
        assert audit_row == ('{"preserved": true}', None, None, None, None)
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == "0004"
    engine.dispose()
    get_settings.cache_clear()


def test_upgrade_preserves_database_created_by_former_dynamic_0001(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy-dynamic-0001.sqlite'}"
    config = _alembic_config(monkeypatch, database_url)

    from app.db import Base
    from app.models import Household

    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(Household.__table__.insert().values(name="Família legada"))
    engine.dispose()

    command.stamp(config, "0001")
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert "capture_drafts" in inspector.get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(select(Household.name)) == "Família legada"
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0004"
    engine.dispose()
    get_settings.cache_clear()
