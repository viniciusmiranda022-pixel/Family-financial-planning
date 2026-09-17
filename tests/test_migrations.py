from io import StringIO
from pathlib import Path

import pytest
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
EXPECTED_0005_TABLES = {"financial_snapshots", "financial_snapshot_lineage"}
EXPECTED_0006_TABLES = {"monthly_financial_closes"}
EXPECTED_0007_TABLES = {"household_financial_revisions"}

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
    "acknowledgement_reason",
    "resolved_at",
    "resolved_by",
    "resolution_reason",
    "created_at",
    "updated_at",
}

EXPECTED_MONTHLY_FINANCIAL_CLOSE_COLUMNS = {
    "id",
    "household_id",
    "period",
    "status",
    "snapshot_id",
    "integrity_run_id",
    "closed_at",
    "closed_by",
    "reopened_at",
    "reopened_by",
    "reason",
    "created_at",
    "updated_at",
    "financial_revision",
}

EXPECTED_HOUSEHOLD_FINANCIAL_REVISION_COLUMNS = {
    "household_id",
    "revision",
    "updated_at",
}

EXPECTED_0009_TABLES = {"backfill_runs"}

EXPECTED_BACKFILL_RUN_COLUMNS = {
    "id",
    "status",
    "dry_run",
    "household_scope",
    "period_from",
    "period_to",
    "financial_rules_version",
    "calculation_version",
    "app_version",
    "started_at",
    "completed_at",
    "duration_ms",
    "summary",
    "error_code",
    "trace_id",
    "triggered_by",
}

EXPECTED_0011_TABLES = {"capture_processing_jobs"}

EXPECTED_0014_TABLES = {"mfa_factors", "mfa_recovery_codes"}

EXPECTED_MFA_FACTOR_COLUMNS = {
    "id",
    "user_id",
    "secret_encrypted",
    "confirmed_at",
    "pending_secret_encrypted",
    "pending_setup_started_at",
    "pending_setup_expires_at",
    "last_accepted_timestep",
    "pending_last_accepted_timestep",
    "failed_attempts",
    "locked_until",
    "created_at",
    "updated_at",
}

EXPECTED_MFA_RECOVERY_CODE_COLUMNS = {
    "id",
    "factor_id",
    "code_hash",
    "created_at",
    "used_at",
}

EXPECTED_CAPTURE_PROCESSING_JOB_COLUMNS = {
    "id",
    "household_id",
    "capture_draft_id",
    "document_id",
    "job_type",
    "content_type",
    "status",
    "attempts",
    "max_attempts",
    "claimed_by",
    "started_at",
    "completed_at",
    "duration_ms",
    "error_code",
    "trace_id",
    "created_at",
    "updated_at",
}

EXPECTED_0015_TABLES = {"card_invoices"}

EXPECTED_CARD_INVOICE_COLUMNS = {
    "id",
    "household_id",
    "account_id",
    "competence",
    "opens_at",
    "closes_at",
    "due_date",
    "status",
    "declared_total",
    "computed_total",
    "principal_carried_in",
    "principal_carried_out",
    "paid_total",
    "closed_at",
    "trace_id",
    "created_at",
    "updated_at",
}

EXPECTED_0016_TABLES = {"assistant_action_events"}

EXPECTED_ASSISTANT_ACTION_EVENT_COLUMNS = {
    "id",
    "audit_event_id",
    "original_message",
    "structured_interpretation",
    "disambiguation_qa",
    "typed_action",
    "target_entity_type",
    "target_entity_ids",
    "before_state",
    "after_state",
    "undoable",
    "non_reversible_reason",
    "undone_at",
    "undone_by",
    "undo_audit_event_id",
    "created_at",
}

EXPECTED_0017_TABLES = {"entry_type_templates"}

EXPECTED_ENTRY_TYPE_TEMPLATE_COLUMNS = {
    "id",
    "household_id",
    "movement_type",
    "label",
    "normalized_label",
    "category_id",
    "confirmation_count",
    "status",
    "active",
    "accepted_at",
    "accepted_by",
    "evidence",
    "created_at",
    "updated_at",
}

EXPECTED_0018_TABLES = {"assistant_action_proposals"}

EXPECTED_ASSISTANT_ACTION_PROPOSAL_COLUMNS = {
    "id",
    "household_id",
    "user_id",
    "trace_id",
    "original_message",
    "structured_interpretation",
    "typed_action",
    "payload",
    "path_params",
    "created_at",
    "expires_at",
    "consumed_at",
    "consumed_action_event_id",
}

EXPECTED_0019_TABLES = {"investments", "investment_valuations"}

EXPECTED_INVESTMENT_COLUMNS = {
    "id",
    "household_id",
    "name",
    "historical_cost",
    "current_value",
    "expected_receivable_value",
    "last_updated_at",
    "expected_receipt_date",
    "notes",
    "active",
    "created_at",
    "updated_at",
}

EXPECTED_INVESTMENT_VALUATION_COLUMNS = {
    "id",
    "household_id",
    "investment_id",
    "valuation_date",
    "historical_cost",
    "current_value",
    "expected_receivable_value",
    "contribution_amount",
    "funding_transaction_id",
    "source",
    "recorded_by",
    "note",
    "invalidated_at",
    "invalidated_by",
    "invalidation_reason",
    "trace_id",
    "created_at",
}

EXPECTED_0020_TABLES = {"notification_settings", "notification_recipients"}

EXPECTED_NOTIFICATION_SETTINGS_COLUMNS = {
    "id",
    "household_id",
    "enabled",
    "send_time_local",
    "timezone",
    "created_at",
    "updated_at",
}

EXPECTED_NOTIFICATION_RECIPIENT_COLUMNS = {
    "id",
    "household_id",
    "email",
    "normalized_email",
    "active",
    "notify_d1",
    "notify_d0",
    "created_at",
    "updated_at",
}

EXPECTED_0021_TABLES = {"notification_deliveries"}

EXPECTED_NOTIFICATION_DELIVERY_COLUMNS = {
    "id",
    "household_id",
    "obligation_id",
    "recipient_id",
    "obligation_due_date",
    "alert_kind",
    "status",
    "attempt_count",
    "max_attempts",
    "next_attempt_at",
    "claimed_at",
    "claimed_by",
    "sent_at",
    "last_error_code",
    "message_id",
    "created_at",
    "updated_at",
}

EXPECTED_0022_TABLES = {"notification_worker_heartbeats"}

EXPECTED_NOTIFICATION_WORKER_HEARTBEAT_COLUMNS = {
    "id",
    "worker_id",
    "started_at",
    "finished_at",
    "ok",
    "counts",
    "last_error_code",
    "created_at",
    "updated_at",
}

EXPECTED_0023_TABLES = {"fund_reference_quotes", "fund_unit_positions", "fund_valuations"}

EXPECTED_FUND_REFERENCE_QUOTE_COLUMNS = {
    "id",
    "fund_cnpj",
    "quota_reference_date",
    "quota_value",
    "net_worth",
    "provider",
    "source_url",
    "ingestion_batch",
    "retrieved_at",
    "created_at",
}

EXPECTED_FUND_UNIT_POSITION_COLUMNS = {
    "id",
    "household_id",
    "fund_cnpj",
    "units_held",
    "effective_date",
    "evidence_type",
    "delta_units",
    "evidence_document_id",
    "evidence_balance_observation_id",
    "note",
    "recorded_by",
    "invalidated_at",
    "invalidated_by",
    "invalidation_reason",
    "trace_id",
    "created_at",
}

EXPECTED_FUND_VALUATION_COLUMNS = {
    "id",
    "household_id",
    "fund_cnpj",
    "account_id",
    "quote_id",
    "position_id",
    "quota_reference_date",
    "quota_value",
    "units_held",
    "gross_value",
    "previous_gross_value",
    "variation_amount",
    "variation_pct",
    "observed_balance",
    "observed_balance_as_of",
    "reconciliation_diff",
    "provider",
    "retrieved_at",
    "valuation_version",
    "status",
    "error_code",
    "invalidated_at",
    "invalidated_by",
    "invalidation_reason",
    "trace_id",
    "created_at",
}


EXPECTED_0024_TABLES = {
    "whatsapp_authorized_numbers",
    "whatsapp_inbound_events",
    "whatsapp_rate_limit_buckets",
}

EXPECTED_WHATSAPP_AUTHORIZED_NUMBER_COLUMNS = {
    "id",
    "household_id",
    "user_id",
    "phone_hash",
    "phone_encrypted",
    "phone_last4",
    "active",
    "created_by",
    "deactivated_at",
    "deactivated_by",
    "created_at",
    "updated_at",
}

EXPECTED_WHATSAPP_INBOUND_EVENT_COLUMNS = {
    "id",
    "provider_message_id",
    "sender_phone_hash",
    "household_id",
    "user_id",
    "status",
    "received_at",
    "trace_id",
}

EXPECTED_WHATSAPP_RATE_LIMIT_BUCKET_COLUMNS = {
    "id",
    "bucket_key",
    "window_start",
    "count",
    "updated_at",
}


def _alembic_config(monkeypatch, database_url: str, *, output_buffer=None) -> Config:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SECRET_KEY", "migration-test-secret-that-is-long-enough")
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("MFA_ENCRYPTION_KEY", "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=")
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
            *EXPECTED_0005_TABLES,
            *EXPECTED_0006_TABLES,
            *EXPECTED_0007_TABLES,
            *EXPECTED_0009_TABLES,
            *EXPECTED_0011_TABLES,
            *EXPECTED_0014_TABLES,
            *EXPECTED_0015_TABLES,
            *EXPECTED_0016_TABLES,
            *EXPECTED_0017_TABLES,
            *EXPECTED_0018_TABLES,
            *EXPECTED_0019_TABLES,
            *EXPECTED_0020_TABLES,
            *EXPECTED_0021_TABLES,
            *EXPECTED_0022_TABLES,
            *EXPECTED_0023_TABLES,
            *EXPECTED_0024_TABLES,
        "capture_drafts",
        "alembic_version",
    }
    assert {index["name"] for index in inspector.get_indexes("capture_drafts")} == {
        "ix_capture_drafts_household_id",
        "ix_capture_household_created",
    }
    assert {column["name"] for column in inspector.get_columns("notification_deliveries")} == (
        EXPECTED_NOTIFICATION_DELIVERY_COLUMNS
    )
    assert {index["name"] for index in inspector.get_indexes("notification_deliveries")} == {
        "ix_notification_deliveries_household_id",
        "ix_notification_deliveries_status_next_attempt",
    }
    assert {column["name"] for column in inspector.get_columns("notification_worker_heartbeats")} == (
        EXPECTED_NOTIFICATION_WORKER_HEARTBEAT_COLUMNS
    )
    assert {column["name"] for column in inspector.get_columns("fund_reference_quotes")} == (
        EXPECTED_FUND_REFERENCE_QUOTE_COLUMNS
    )
    assert {column["name"] for column in inspector.get_columns("fund_unit_positions")} == (
        EXPECTED_FUND_UNIT_POSITION_COLUMNS
    )
    assert {column["name"] for column in inspector.get_columns("fund_valuations")} == (
        EXPECTED_FUND_VALUATION_COLUMNS
    )
    assert {index["name"] for index in inspector.get_indexes("fund_reference_quotes")} == {
        "ix_fund_reference_quotes_fund_cnpj",
    }
    assert {index["name"] for index in inspector.get_indexes("fund_unit_positions")} == {
        "ix_fund_unit_positions_household_id",
        "ix_fund_unit_positions_fund_cnpj",
        "ix_fund_unit_positions_trace_id",
        "ix_fund_unit_positions_household_fund_date",
    }
    assert {index["name"] for index in inspector.get_indexes("fund_valuations")} == {
        "ix_fund_valuations_household_id",
        "ix_fund_valuations_fund_cnpj",
        "ix_fund_valuations_trace_id",
        "ix_fund_valuations_household_fund_date",
    }
    assert {column["name"] for column in inspector.get_columns("whatsapp_authorized_numbers")} == (
        EXPECTED_WHATSAPP_AUTHORIZED_NUMBER_COLUMNS
    )
    assert {index["name"] for index in inspector.get_indexes("whatsapp_authorized_numbers")} == {
        "ix_whatsapp_authorized_numbers_household_id",
        "ix_whatsapp_authorized_numbers_user_id",
        "ix_whatsapp_authorized_numbers_phone_hash",
        "uq_whatsapp_authorized_numbers_active_user",
        "uq_whatsapp_authorized_numbers_active_phone_hash",
    }
    assert {column["name"] for column in inspector.get_columns("whatsapp_inbound_events")} == (
        EXPECTED_WHATSAPP_INBOUND_EVENT_COLUMNS
    )
    assert {index["name"] for index in inspector.get_indexes("whatsapp_inbound_events")} == {
        "ix_whatsapp_inbound_events_sender_phone_hash",
        "ix_whatsapp_inbound_events_household_id",
        "ix_whatsapp_inbound_events_received_at",
    }
    assert {column["name"] for column in inspector.get_columns("whatsapp_rate_limit_buckets")} == (
        EXPECTED_WHATSAPP_RATE_LIMIT_BUCKET_COLUMNS
    )
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
        "ix_transaction_household_transfer_group",
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
    assert {index["name"] for index in inspector.get_indexes("financial_snapshots")} == {
        "ix_financial_snapshots_checksum",
        "ix_financial_snapshots_household_id",
        "ix_financial_snapshots_household_period_status",
        "ix_financial_snapshots_trace_id",
    }
    assert {
        index["name"] for index in inspector.get_indexes("financial_snapshot_lineage")
    } == {
        "ix_financial_snapshot_lineage_household_id",
        "ix_financial_snapshot_lineage_metric",
        "ix_financial_snapshot_lineage_snapshot_id",
        "ix_financial_snapshot_lineage_trace_id",
    }
    assert {
        column["name"] for column in inspector.get_columns("monthly_financial_closes")
    } == EXPECTED_MONTHLY_FINANCIAL_CLOSE_COLUMNS
    assert {
        index["name"] for index in inspector.get_indexes("monthly_financial_closes")
    } == {
        "ix_monthly_financial_closes_household_id",
        "ix_monthly_financial_closes_period",
        "ix_monthly_financial_closes_status",
        "ix_monthly_financial_closes_snapshot_id",
        "ix_monthly_financial_closes_integrity_run_id",
        "ix_monthly_financial_closes_household_status",
    }
    assert {
        column["name"] for column in inspector.get_columns("household_financial_revisions")
    } == EXPECTED_HOUSEHOLD_FINANCIAL_REVISION_COLUMNS
    assert {
        column["name"] for column in inspector.get_columns("backfill_runs")
    } == EXPECTED_BACKFILL_RUN_COLUMNS
    assert {index["name"] for index in inspector.get_indexes("backfill_runs")} == {
        "ix_backfill_runs_status",
        "ix_backfill_runs_trace_id",
    }
    assert {
        column["name"] for column in inspector.get_columns("capture_processing_jobs")
    } == EXPECTED_CAPTURE_PROCESSING_JOB_COLUMNS
    assert {index["name"] for index in inspector.get_indexes("capture_processing_jobs")} == {
        "ix_capture_jobs_household_id",
        "ix_capture_jobs_capture_draft_id",
        "ix_capture_jobs_trace_id",
        "ix_capture_jobs_household_status",
        "ix_capture_jobs_status_created",
    }
    assert "session_version" in {column["name"] for column in inspector.get_columns("users")}
    assert {column["name"] for column in inspector.get_columns("mfa_factors")} == (
        EXPECTED_MFA_FACTOR_COLUMNS
    )
    assert {index["name"] for index in inspector.get_indexes("mfa_factors")} == {
        "ix_mfa_factors_user_id"
    }
    assert {column["name"] for column in inspector.get_columns("mfa_recovery_codes")} == (
        EXPECTED_MFA_RECOVERY_CODE_COLUMNS
    )
    assert {index["name"] for index in inspector.get_indexes("mfa_recovery_codes")} == {
        "ix_mfa_recovery_codes_factor_id",
        "ix_mfa_recovery_codes_code_hash",
    }
    assert {column["name"] for column in inspector.get_columns("card_invoices")} == (
        EXPECTED_CARD_INVOICE_COLUMNS
    )
    assert {index["name"] for index in inspector.get_indexes("card_invoices")} == {
        "ix_card_invoice_household_id",
        "ix_card_invoice_account_id",
        "ix_card_invoice_household_competence",
        "ix_card_invoice_trace_id",
    }
    assert {
        column["name"] for column in inspector.get_columns("assistant_action_events")
    } == EXPECTED_ASSISTANT_ACTION_EVENT_COLUMNS
    assert {
        index["name"] for index in inspector.get_indexes("assistant_action_events")
    } == {
        "ix_assistant_action_events_audit_event_id",
        "ix_assistant_action_events_typed_action",
        "ix_assistant_action_events_created_at",
    }
    assert {
        column["name"] for column in inspector.get_columns("entry_type_templates")
    } == EXPECTED_ENTRY_TYPE_TEMPLATE_COLUMNS
    assert {
        index["name"] for index in inspector.get_indexes("entry_type_templates")
    } == {
        "ix_entry_type_templates_household_id",
        "ix_entry_type_templates_household_label",
    }
    assert {
        column["name"] for column in inspector.get_columns("assistant_action_proposals")
    } == EXPECTED_ASSISTANT_ACTION_PROPOSAL_COLUMNS
    assert {
        index["name"] for index in inspector.get_indexes("assistant_action_proposals")
    } == {
        "ix_assistant_action_proposals_household_id",
        "ix_assistant_action_proposals_trace_id",
        "ix_assistant_action_proposals_typed_action",
        "ix_assistant_action_proposals_created_at",
    }
    assert {"card_invoice_id", "refund_of_transaction_id"}.issubset(
        {column["name"] for column in inspector.get_columns("transactions")}
    )
    assert "ix_transaction_card_invoice_id" in {
        index["name"] for index in inspector.get_indexes("transactions")
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


def test_downgrade_from_0015_refuses_to_discard_confirmed_refund_lineage(monkeypatch, tmp_path) -> None:
    """Engineering review round (2026-09-12, `BLOQUEIO DE MERGE` #5): a
    confirmed `refund_of_transaction_id` link is a human/Assistant-approved
    fact, not derived cache -- `downgrade()` must refuse instead of
    silently discarding it, and must still succeed once no link remains."""

    from datetime import date
    from decimal import Decimal

    from sqlalchemy.orm import sessionmaker

    from app.models import Account, Category, Household, Transaction

    database_url = f"sqlite:///{tmp_path / 'migrations-refund-guard.sqlite'}"
    config = _alembic_config(monkeypatch, database_url)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        household = Household(name="Família Downgrade")
        db.add(household)
        db.flush()
        account = Account(household_id=household.id, name="Cartão", account_type="credit_card")
        category = Category(household_id=household.id, name="Compras")
        db.add_all([account, category])
        db.flush()
        purchase = Transaction(
            household_id=household.id,
            account_id=account.id,
            category_id=category.id,
            booked_at=date(2026, 9, 3),
            occurred_at=date(2026, 9, 3),
            competence="2026-09",
            description="Compra",
            normalized_description="COMPRA",
            amount=Decimal("-100.00"),
            transaction_type="expense",
            fingerprint="f" * 64,
            source_priority=50,
            confidence=Decimal("1"),
            reviewed=True,
        )
        db.add(purchase)
        db.flush()
        refund = Transaction(
            household_id=household.id,
            account_id=account.id,
            category_id=category.id,
            booked_at=date(2026, 9, 8),
            occurred_at=date(2026, 9, 8),
            competence="2026-09",
            description="Estorno",
            normalized_description="ESTORNO",
            amount=Decimal("100.00"),
            transaction_type="refund",
            fingerprint="g" * 64,
            source_priority=50,
            confidence=Decimal("1"),
            reviewed=True,
            refund_of_transaction_id=purchase.id,
        )
        db.add(refund)
        db.commit()
        purchase_id, refund_id = purchase.id, refund.id
    engine.dispose()

    with pytest.raises(RuntimeError, match="vínculo"):
        command.downgrade(config, "0014")

    # Refused: schema (and the confirmed link itself) is left untouched.
    engine, inspector = _inspect(database_url)
    assert "card_invoices" in inspector.get_table_names()
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        assert db.get(Transaction, refund_id).refund_of_transaction_id == purchase_id
    engine.dispose()

    # The documented export-then-clear path: once no link remains, the
    # exact same downgrade proceeds like before this guard existed.
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE transactions SET refund_of_transaction_id = NULL")
    engine.dispose()

    command.downgrade(config, "0014")
    engine, inspector = _inspect(database_url)
    assert "card_invoices" not in inspector.get_table_names()
    assert "refund_of_transaction_id" not in {
        column["name"] for column in inspector.get_columns("transactions")
    }
    engine.dispose()
    get_settings.cache_clear()


def test_downgrade_from_0016_refuses_to_discard_assistant_action_events(monkeypatch, tmp_path) -> None:
    """Engineering review of PR #92 (blocker 3): the original `downgrade()`
    claimed `assistant_action_events` is always reconstructable from its
    paired `audit_events` row and therefore safe to drop unconditionally --
    false, since `original_message`/`structured_interpretation`/
    `disambiguation_qa` and the undo bookkeeping live only here. Same
    guarded pattern as `test_downgrade_from_0015_refuses_to_discard_confirmed_refund_lineage`."""

    from sqlalchemy.orm import sessionmaker

    from app.models import AssistantActionEvent, AuditEvent, Household

    database_url = f"sqlite:///{tmp_path / 'migrations-assistant-action-guard.sqlite'}"
    config = _alembic_config(monkeypatch, database_url)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        household = Household(name="Família Downgrade Assistente")
        db.add(household)
        db.flush()
        audit_event = AuditEvent(
            household_id=household.id,
            event_type="assistant.execute",
            entity_type="transaction",
            details='{"typed_action": "create_expense"}',
            source="assistant",
        )
        db.add(audit_event)
        db.flush()
        action_event = AssistantActionEvent(
            audit_event_id=audit_event.id,
            original_message="Gastei 300 de combustível",
            typed_action="create_expense",
            target_entity_ids=["fake-transaction-id"],
            undoable=True,
        )
        db.add(action_event)
        db.commit()
    engine.dispose()

    with pytest.raises(RuntimeError, match="assistant_action_events"):
        command.downgrade(config, "0015")

    # Refused: schema (and the narration/undo trail itself) is left
    # untouched.
    engine, inspector = _inspect(database_url)
    assert "assistant_action_events" in inspector.get_table_names()
    engine.dispose()

    # The documented export-then-clear path: once no row remains, the exact
    # same downgrade proceeds like before this guard existed.
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM assistant_action_events")
    engine.dispose()

    command.downgrade(config, "0015")
    engine, inspector = _inspect(database_url)
    assert "assistant_action_events" not in inspector.get_table_names()
    engine.dispose()
    get_settings.cache_clear()


def test_downgrade_from_0019_refuses_to_discard_investment_valuations(monkeypatch, tmp_path) -> None:
    """October Go-Live Slice 6: `investment_valuations` is the only durable
    record of every valuation/contribution event -- `investments` itself
    only ever holds the *current* state. Same guarded-downgrade pattern as
    0016/0015."""

    from datetime import date
    from decimal import Decimal

    from sqlalchemy.orm import sessionmaker

    database_url = f"sqlite:///{tmp_path / 'migrations-investment-guard.sqlite'}"
    config = _alembic_config(monkeypatch, database_url)

    from app.models import Household, Investment, InvestmentValuation

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        household = Household(name="Família Downgrade Investimento")
        db.add(household)
        db.flush()
        investment = Investment(
            household_id=household.id,
            name="Studio",
            historical_cost=Decimal("30000.00"),
            current_value=Decimal("35000.00"),
            last_updated_at=date(2026, 9, 1),
        )
        db.add(investment)
        db.flush()
        valuation = InvestmentValuation(
            household_id=household.id,
            investment_id=investment.id,
            valuation_date=date(2026, 9, 1),
            historical_cost=Decimal("30000.00"),
            current_value=Decimal("35000.00"),
            source="manual_confirmed",
            trace_id="trace-investment-guard",
        )
        db.add(valuation)
        db.commit()
    engine.dispose()

    with pytest.raises(RuntimeError, match="investment_valuations"):
        command.downgrade(config, "0018")

    engine, inspector = _inspect(database_url)
    assert "investment_valuations" in inspector.get_table_names()
    assert "investments" in inspector.get_table_names()
    engine.dispose()

    # Clearing the valuation history alone still leaves a real `investments`
    # row behind -- the downgrade must refuse a second time, now naming
    # `investments` instead.
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM investment_valuations")
    engine.dispose()

    with pytest.raises(RuntimeError, match="investments"):
        command.downgrade(config, "0018")

    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM investments")
    engine.dispose()

    command.downgrade(config, "0018")
    engine, inspector = _inspect(database_url)
    assert "investments" not in inspector.get_table_names()
    assert "investment_valuations" not in inspector.get_table_names()
    engine.dispose()
    get_settings.cache_clear()


def test_downgrade_from_0023_refuses_to_discard_fund_valuations(monkeypatch, tmp_path) -> None:
    """Issue #85: `fund_valuations`/`fund_unit_positions`/`fund_reference_quotes`
    are the only durable record of a CVM-derived daily valuation and of the
    evidence it was computed from -- none of it is reconstructible from
    CVM after the fact (CVM only guarantees twelve months of published
    files, and the evidence for `units_held` may not exist anywhere else at
    all). Same guarded-downgrade pattern as 0015/0016/0019/0020."""

    from datetime import UTC, date, datetime
    from decimal import Decimal

    from sqlalchemy.orm import sessionmaker

    database_url = f"sqlite:///{tmp_path / 'migrations-fund-valuation-guard.sqlite'}"
    config = _alembic_config(monkeypatch, database_url)

    from app.models import FundReferenceQuote, FundUnitPosition, FundValuation, Household

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    session_factory = sessionmaker(bind=engine)
    retrieved_at = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)
    with session_factory() as db:
        household = Household(name="Família Downgrade Privilège DI")
        db.add(household)
        db.flush()
        quote = FundReferenceQuote(
            fund_cnpj="26199519000134",
            quota_reference_date=date(2026, 9, 15),
            quota_value=Decimal("28.1234567890"),
            provider="cvm_dados_abertos",
            source_url="http://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_202609.csv",
            ingestion_batch="202609",
            retrieved_at=retrieved_at,
        )
        db.add(quote)
        db.flush()
        position = FundUnitPosition(
            household_id=household.id,
            fund_cnpj="26199519000134",
            units_held=Decimal("1445.12345678"),
            effective_date=date(2026, 9, 11),
            evidence_type="confirmed_balance_reconciliation",
            trace_id="trace-fund-position-guard",
        )
        db.add(position)
        db.flush()
        valuation = FundValuation(
            household_id=household.id,
            fund_cnpj="26199519000134",
            quote_id=quote.id,
            position_id=position.id,
            quota_reference_date=date(2026, 9, 15),
            quota_value=quote.quota_value,
            units_held=position.units_held,
            gross_value=Decimal("40650.12"),
            provider="cvm_dados_abertos",
            retrieved_at=retrieved_at,
            valuation_version="v1",
            status="ok",
            trace_id="trace-fund-valuation-guard",
        )
        db.add(valuation)
        db.commit()
    engine.dispose()

    with pytest.raises(RuntimeError, match="fund_valuations"):
        command.downgrade(config, "0022")

    engine, inspector = _inspect(database_url)
    assert "fund_valuations" in inspector.get_table_names()
    assert "fund_unit_positions" in inspector.get_table_names()
    assert "fund_reference_quotes" in inspector.get_table_names()
    engine.dispose()

    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM fund_valuations")
    engine.dispose()

    with pytest.raises(RuntimeError, match="fund_unit_positions"):
        command.downgrade(config, "0022")

    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM fund_unit_positions")
    engine.dispose()

    with pytest.raises(RuntimeError, match="fund_reference_quotes"):
        command.downgrade(config, "0022")

    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM fund_reference_quotes")
    engine.dispose()

    command.downgrade(config, "0022")
    engine, inspector = _inspect(database_url)
    assert "fund_valuations" not in inspector.get_table_names()
    assert "fund_unit_positions" not in inspector.get_table_names()
    assert "fund_reference_quotes" not in inspector.get_table_names()
    engine.dispose()
    get_settings.cache_clear()


def test_downgrade_from_0020_refuses_to_discard_notification_recipients(monkeypatch, tmp_path) -> None:
    """MAIL-00: a `notification_recipients` row is the household's only
    record of who asked to be alerted and how -- not reconstructible from
    anything else in the schema. Same guarded-downgrade pattern as
    0015/0016/0019."""

    from sqlalchemy.orm import sessionmaker

    database_url = f"sqlite:///{tmp_path / 'migrations-notification-guard.sqlite'}"
    config = _alembic_config(monkeypatch, database_url)

    from app.models import Household, NotificationRecipient, NotificationSettings

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        household = Household(name="Família Downgrade Alertas")
        db.add(household)
        db.flush()
        db.add(
            NotificationRecipient(
                household_id=household.id, email="kelly@exemplo.com", normalized_email="kelly@exemplo.com"
            )
        )
        db.commit()
    engine.dispose()

    with pytest.raises(RuntimeError, match="notification_recipients"):
        command.downgrade(config, "0019")

    engine, inspector = _inspect(database_url)
    assert "notification_recipients" in inspector.get_table_names()
    assert "notification_settings" in inspector.get_table_names()
    engine.dispose()

    # Clearing recipients alone still leaves a real `notification_settings`
    # row behind (enabled/send_time_local/timezone the household configured)
    # -- the downgrade must refuse a second time, now naming
    # `notification_settings` instead.
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM notification_recipients")
    engine.dispose()

    engine = create_engine(database_url)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        household_id = db.scalar(select(Household.id))
        db.add(NotificationSettings(household_id=household_id, enabled=True))
        db.commit()
    engine.dispose()

    with pytest.raises(RuntimeError, match="notification_settings"):
        command.downgrade(config, "0019")

    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM notification_settings")
    engine.dispose()

    command.downgrade(config, "0019")
    engine, inspector = _inspect(database_url)
    assert "notification_settings" not in inspector.get_table_names()
    assert "notification_recipients" not in inspector.get_table_names()
    engine.dispose()
    get_settings.cache_clear()


def test_downgrade_from_0024_refuses_to_discard_whatsapp_authorized_numbers(
    monkeypatch, tmp_path
) -> None:
    """Issue #73: `whatsapp_authorized_numbers` is the only record of which
    admin authorized which household member's WhatsApp number -- not
    reconstructible from anything else. Same guarded-downgrade pattern as
    0015/0016/0019/0020/0023. `whatsapp_inbound_events`/
    `whatsapp_rate_limit_buckets` are unguarded operational telemetry, same
    treatment as `notification_worker_heartbeats` (0022)."""

    from sqlalchemy.orm import sessionmaker

    database_url = f"sqlite:///{tmp_path / 'migrations-whatsapp-guard.sqlite'}"
    config = _alembic_config(monkeypatch, database_url)

    from app.models import Household, User, WhatsAppAuthorizedNumber

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        household = Household(name="Família Downgrade WhatsApp")
        db.add(household)
        db.flush()
        member = User(
            household_id=household.id,
            name="Vinicius",
            username=f"vinicius-{household.id[:8]}",
            password_hash="scrypt$16384$8$1$AAAA$BBBB",
        )
        db.add(member)
        db.flush()
        db.add(
            WhatsAppAuthorizedNumber(
                household_id=household.id,
                user_id=member.id,
                phone_hash="a" * 64,
                phone_encrypted="gAAAAA-fake-fernet-token",
                phone_last4="8888",
            )
        )
        db.commit()
    engine.dispose()

    with pytest.raises(RuntimeError, match="whatsapp_authorized_numbers"):
        command.downgrade(config, "0023")

    engine, inspector = _inspect(database_url)
    assert "whatsapp_authorized_numbers" in inspector.get_table_names()
    engine.dispose()

    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM whatsapp_authorized_numbers")
    engine.dispose()

    command.downgrade(config, "0023")
    engine, inspector = _inspect(database_url)
    assert "whatsapp_authorized_numbers" not in inspector.get_table_names()
    assert "whatsapp_inbound_events" not in inspector.get_table_names()
    assert "whatsapp_rate_limit_buckets" not in inspector.get_table_names()
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
    assert "CREATE TABLE financial_snapshots" in sql
    assert "CREATE TABLE financial_snapshot_lineage" in sql
    assert "CREATE TABLE monthly_financial_closes" in sql
    assert "CREATE TABLE household_financial_revisions" in sql
    assert "CREATE TABLE backfill_runs" in sql
    assert "ALTER TABLE integrity_findings ADD COLUMN acknowledgement_reason TEXT" in sql
    assert "ALTER TABLE audit_events ADD COLUMN before_state JSONB" in sql
    assert "CREATE TABLE transactions" in sql
    assert "ALTER TABLE users ADD COLUMN session_version" in sql
    assert "CREATE TABLE mfa_factors" in sql
    assert "CREATE TABLE mfa_recovery_codes" in sql
    assert "CREATE TABLE card_invoices" in sql
    assert "CREATE TABLE assistant_action_events" in sql
    assert "CREATE TABLE entry_type_templates" in sql
    assert "CREATE TABLE assistant_action_proposals" in sql
    assert "CREATE TABLE whatsapp_authorized_numbers" in sql
    assert "CREATE TABLE whatsapp_inbound_events" in sql
    assert "CREATE TABLE whatsapp_rate_limit_buckets" in sql
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
        ).scalar_one() == "0025"
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
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0025"
    engine.dispose()
    get_settings.cache_clear()
