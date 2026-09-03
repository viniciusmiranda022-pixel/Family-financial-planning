import asyncio
import json
import os
from decimal import Decimal

os.environ.setdefault("SECRET_KEY", "initial-load-test-secret-not-used-in-production")
os.environ.setdefault("FILE_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

import pytest  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.cli.initial_load import (  # noqa: E402
    AccountSeed,
    BalanceSeed,
    DocumentSeed,
    InitialLoadManifest,
    ObligationSeed,
    ProfileSeed,
    _apply_balances,
    _apply_documents,
    _apply_obligations,
    _apply_profile,
    _document_path,
    _lock_household_initial_load,
    _preview_documents,
    _resolve_accounts,
    _sanitize_validation_error,
    _summary,
    load_manifest,
)
from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AccountBalanceObservation,
    AuditEvent,
    Document,
    FinancialProfile,
    Household,
    Obligation,
    Transaction,
    User,
)


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _seed_household(db: Session) -> tuple[Household, User]:
    household = Household(name="Família Teste")
    db.add(household)
    db.flush()
    user = User(
        household_id=household.id,
        name="Admin Teste",
        username="admin-teste",
        password_hash="not-a-real-password-hash",
        is_admin=True,
        active=True,
    )
    db.add(user)
    db.commit()
    return household, user


def _manifest() -> InitialLoadManifest:
    return InitialLoadManifest(
        version=1,
        load_id="synthetic-2026-09",
        accounts=[
            AccountSeed(
                key="investment",
                name="Reserva Teste",
                institution="Banco Teste",
                account_type="investment",
                owner_label="Família",
                last_four="1234",
            )
        ],
        profile=ProfileSeed(
            investment_name="Reserva Teste",
        ),
        balances=[
            BalanceSeed(
                account="investment",
                amount=Decimal("1000.00"),
                as_of_date="2026-09-03",
                observation_type="point_in_time",
            )
        ],
        obligations=[
            ObligationSeed(
                name="Ativo teste - parcela 1",
                due_date="2026-09-10",
                amount=Decimal("1500.00"),
                category="asset_acquisition",
            )
        ],
    )


def test_lock_household_initial_load_is_a_no_op_on_sqlite() -> None:
    """`_lock_household_initial_load` only takes a real
    `pg_advisory_xact_lock` on PostgreSQL (see its docstring and
    `tests/test_postgresql_integration.py::
    test_initial_load_concurrent_apply_does_not_duplicate_obligation_or_balance`
    for the real-lock proof). This is the fast SQLite-side guarantee that
    `main()` calling it unconditionally never breaks dev/test runs, which
    always use SQLite (see `app/db.py`)."""
    engine = _engine()
    with Session(engine) as db:
        household, _ = _seed_household(db)
        db.commit()
        _lock_household_initial_load(db, household.id)
        _lock_household_initial_load(db, household.id)


def test_manifest_rejects_unknown_account_reference(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "load_id": "bad-ref",
                "accounts": [],
                "balances": [
                    {
                        "account": "missing",
                        "amount": "10.00",
                        "as_of_date": "2026-09-03",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="conta desconhecida"):
        load_manifest(path)


def test_dry_run_does_not_persist_structure() -> None:
    with Session(_engine()) as db:
        household, user = _seed_household(db)
        manifest = _manifest()

        accounts, account_stats = _resolve_accounts(
            db, household.id, manifest, apply=False, user_id=user.id
        )
        profile_stats = _apply_profile(
            db, household.id, manifest, apply=False, user_id=user.id
        )
        obligation_stats = _apply_obligations(
            db, household.id, manifest, apply=False, user_id=user.id
        )
        balance_stats = _apply_balances(
            db, household.id, manifest, accounts, apply=False, user_id=user.id
        )
        db.rollback()

        assert account_stats == {"created": 1, "skipped": 0}
        assert profile_stats["created"] is True
        assert obligation_stats == {"created": 1, "skipped": 0}
        assert balance_stats == {"created": 1, "skipped": 0}
        assert db.scalar(select(func.count(Account.id))) == 0
        assert db.scalar(select(func.count(FinancialProfile.id))) == 0
        assert db.scalar(select(func.count(Obligation.id))) == 0
        assert db.scalar(select(func.count(AccountBalanceObservation.id))) == 0


def test_apply_is_idempotent_for_structure_and_confirmed_balance() -> None:
    with Session(_engine()) as db:
        household, user = _seed_household(db)
        manifest = _manifest()

        accounts, first_accounts = _resolve_accounts(
            db, household.id, manifest, apply=True, user_id=user.id
        )
        first_profile = _apply_profile(
            db, household.id, manifest, apply=True, user_id=user.id
        )
        first_obligations = _apply_obligations(
            db, household.id, manifest, apply=True, user_id=user.id
        )
        first_balances = _apply_balances(
            db, household.id, manifest, accounts, apply=True, user_id=user.id
        )
        db.commit()

        accounts, second_accounts = _resolve_accounts(
            db, household.id, manifest, apply=True, user_id=user.id
        )
        second_profile = _apply_profile(
            db, household.id, manifest, apply=True, user_id=user.id
        )
        second_obligations = _apply_obligations(
            db, household.id, manifest, apply=True, user_id=user.id
        )
        second_balances = _apply_balances(
            db, household.id, manifest, accounts, apply=True, user_id=user.id
        )
        db.commit()

        assert first_accounts == {"created": 1, "skipped": 0}
        assert first_profile["created"] is True
        assert first_obligations == {"created": 1, "skipped": 0}
        assert first_balances == {"created": 1, "skipped": 0}
        assert second_accounts == {"created": 0, "skipped": 1}
        assert second_profile == {
            "created": False,
            "changed_count": 0,
            "changed_fields": [],
        }
        assert second_obligations == {"created": 0, "skipped": 1}
        assert second_balances == {"created": 0, "skipped": 1}
        assert db.scalar(select(func.count(Account.id))) == 1
        assert db.scalar(select(func.count(FinancialProfile.id))) == 1
        assert db.scalar(select(func.count(Obligation.id))) == 1
        assert db.scalar(select(func.count(AccountBalanceObservation.id))) == 1

        observation = db.scalar(select(AccountBalanceObservation))
        assert observation is not None
        assert observation.amount == Decimal("1000.00")
        assert observation.source == "manual_confirmed"
        assert observation.confirmed_by == user.id


def test_profile_report_and_summary_never_leak_financial_values() -> None:
    """Work Order `docs/WORK_ORDER_INITIAL_LOAD_BOOTSTRAP.md` ("Segurança e
    privacidade"): the CLI must never print sensitive data, only file names,
    status and counts. `_apply_profile`'s report (which flows straight into
    `_summary()` and then `main()`'s stdout `json.dumps`) must therefore
    carry field names/counts only — never the literal salary/cap/rate
    values — in both dry-run and apply mode. The real values must still
    reach the DB (profile row + audit trail), since only stdout/log
    exposure is prohibited, not persistence or auditability."""
    salary = Decimal("12345.67")
    cap = Decimal("999.99")
    with Session(_engine()) as db:
        household, user = _seed_household(db)
        manifest = InitialLoadManifest(
            version=1,
            load_id="privacy-check",
            accounts=[],
            profile=ProfileSeed(monthly_salary_net=salary, monthly_cash_cap=cap),
        )

        dry_run_report = _apply_profile(
            db, household.id, manifest, apply=False, user_id=user.id
        )
        db.rollback()
        assert dry_run_report == {
            "created": True,
            "changed_count": 2,
            "changed_fields": ["monthly_cash_cap", "monthly_salary_net"],
        }
        dry_run_json = json.dumps(dry_run_report)
        assert str(salary) not in dry_run_json
        assert str(cap) not in dry_run_json

        apply_report = _apply_profile(
            db, household.id, manifest, apply=True, user_id=user.id
        )
        db.commit()
        assert apply_report == {
            "created": True,
            "changed_count": 2,
            "changed_fields": ["monthly_cash_cap", "monthly_salary_net"],
        }

        summary = _summary(
            mode="apply",
            manifest=manifest,
            accounts={"created": 0, "skipped": 0},
            profile=apply_report,
            obligations={"created": 0, "skipped": 0},
            balances={"created": 0, "skipped": 0},
            documents=[],
        )
        # This is exactly the payload `main()` prints to stdout via
        # `json.dumps(_summary(...))`.
        stdout_json = json.dumps(summary, ensure_ascii=False, default=str)
        assert str(salary) not in stdout_json
        assert str(cap) not in stdout_json

        # The values must still be persisted: privacy applies to stdout, not
        # to the DB record or its audit trail.
        profile = db.scalar(
            select(FinancialProfile).where(FinancialProfile.household_id == household.id)
        )
        assert profile is not None
        assert profile.monthly_salary_net == salary
        assert profile.monthly_cash_cap == cap

        audit = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "initial_load.profile.apply")
        )
        assert audit is not None
        assert audit.after_state["monthly_salary_net"] == str(salary)
        assert audit.after_state["monthly_cash_cap"] == str(cap)


def test_conflicts_block_instead_of_overwriting() -> None:
    with Session(_engine()) as db:
        household, user = _seed_household(db)
        manifest = _manifest()
        accounts, _ = _resolve_accounts(
            db, household.id, manifest, apply=True, user_id=user.id
        )
        _apply_obligations(db, household.id, manifest, apply=True, user_id=user.id)
        _apply_balances(
            db, household.id, manifest, accounts, apply=True, user_id=user.id
        )
        db.commit()

        conflicting_account = manifest.model_copy(deep=True)
        conflicting_account.accounts[0].institution = "Outro Banco"
        with pytest.raises(ValueError, match="Conflito na conta") as account_exc:
            _resolve_accounts(
                db,
                household.id,
                conflicting_account,
                apply=True,
                user_id=user.id,
            )
        # stdout privacy contract: identify by the manifest `key` (a
        # restricted slug), never the free-text account `name`.
        assert manifest.accounts[0].key in str(account_exc.value)
        assert manifest.accounts[0].name not in str(account_exc.value)
        db.rollback()

        conflicting_obligation = manifest.model_copy(deep=True)
        conflicting_obligation.obligations[0].amount = Decimal("1600.00")
        with pytest.raises(ValueError, match="Conflito na obrigação") as obligation_exc:
            _apply_obligations(
                db,
                household.id,
                conflicting_obligation,
                apply=True,
                user_id=user.id,
            )
        assert manifest.obligations[0].name not in str(obligation_exc.value)
        db.rollback()

        conflicting_balance = manifest.model_copy(deep=True)
        conflicting_balance.balances[0].amount = Decimal("999.00")
        with pytest.raises(ValueError, match="Conflito de saldo") as balance_exc:
            _apply_balances(
                db,
                household.id,
                conflicting_balance,
                accounts,
                apply=True,
                user_id=user.id,
            )
        assert manifest.balances[0].account in str(balance_exc.value)
        assert manifest.accounts[0].name not in str(balance_exc.value)


def test_apply_documents_delegates_to_official_import_pipeline(tmp_path, monkeypatch) -> None:
    with Session(_engine()) as db:
        household, user = _seed_household(db)
        account = Account(
            household_id=household.id,
            name="Conta Teste",
            institution="Banco Teste",
            account_type="checking",
            owner_label="Família",
        )
        db.add(account)
        db.commit()

        document = tmp_path / "statement.csv"
        document.write_text("data,descricao,valor\n2026-09-01,Compra teste,-10.00\n", encoding="utf-8")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")
        manifest = InitialLoadManifest(
            version=1,
            load_id="pipeline-test",
            accounts=[AccountSeed(key="bank", name="Conta Teste")],
            documents=[
                DocumentSeed(
                    path="statement.csv",
                    document_type="bank_statement",
                    account="bank",
                )
            ],
        )
        calls = []

        async def fake_import_document(**kwargs):
            calls.append(kwargs)
            return {
                "status": "imported",
                "records": 1,
                "review_items": 0,
                "reconciliation": {"status": "reconciled"},
            }

        monkeypatch.setattr("app.cli.initial_load.import_document", fake_import_document)
        report = asyncio.run(
            _apply_documents(
                db,
                manifest_path,
                manifest,
                {"bank": account},
                user,
            )
        )

        assert len(calls) == 1
        assert calls[0]["account_id"] == account.id
        assert calls[0]["document_type"] == "bank_statement"
        assert report[0]["status"] == "imported"
        assert report[0]["reconciliation"] == "reconciled"


def test_manifest_rejects_legacy_investment_balance_field(tmp_path) -> None:
    """`FinancialProfile.investment_balance` is the mutable, non-reconciled
    counter `docs/INTEGRITY_IMPLEMENTATION_PLAN.md` (section 1) flags as the
    project's single riskiest source of financial drift. The initial load
    must not accept it as a second, independent source for the same balance
    `balances[]`/`AccountBalanceObservation` already covers."""
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "load_id": "legacy-investment-balance",
                "accounts": [],
                "profile": {"investment_balance": "1000.00"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_manifest(path)


def test_apply_balances_blocks_when_existing_observation_has_different_source() -> None:
    """Idempotency must match the seed's own provenance contract
    (`source=manual_confirmed`, `confidence=1.0000`), not merely the amount.
    A same-amount/date/type fact from a different, less-trusted source (e.g.
    `source="statement"` created by app/services/reconciliation.py) is not
    evidence of a prior manual confirmation and must block instead of being
    silently accepted as an equivalent seed."""
    with Session(_engine()) as db:
        household, user = _seed_household(db)
        manifest = _manifest()
        accounts, _ = _resolve_accounts(
            db, household.id, manifest, apply=True, user_id=user.id
        )
        db.commit()

        seed = manifest.balances[0]
        db.add(
            AccountBalanceObservation(
                household_id=household.id,
                account_id=accounts[seed.account].id,
                amount=seed.amount,
                as_of_date=seed.as_of_date,
                observation_type=seed.observation_type,
                source="statement",
                confidence=Decimal("1.0000"),
                trace_id="11111111-1111-1111-1111-111111111111",
            )
        )
        db.commit()

        with pytest.raises(ValueError, match="Conflito de saldo"):
            _apply_balances(
                db, household.id, manifest, accounts, apply=True, user_id=user.id
            )

        # The conflicting fact must not have been silently accepted: only
        # the pre-existing `statement` observation is present.
        assert db.scalar(select(func.count(AccountBalanceObservation.id))) == 1
        remaining = db.scalar(select(AccountBalanceObservation))
        assert remaining.source == "statement"


def test_preview_never_claims_ready_before_full_pipeline_validation(tmp_path) -> None:
    """`--dry-run` only exercises parsing + reconciliation (pure and
    deterministic); it cannot safely run classification/duplicate detection
    without invoking `import_document`, which writes the encrypted payload
    to disk (`EncryptedDocumentStore.save`) before any DB commit — a side
    effect a later rollback cannot undo. A document that parses and
    reconciles cleanly in preview can still be routed to `ReviewItem` during
    `--apply`, so preview must never claim it is unconditionally `ready`."""
    with Session(_engine()) as db:
        household, _user = _seed_household(db)
        account = Account(
            household_id=household.id,
            name="Conta Teste",
            institution="Banco Teste",
            account_type="checking",
            owner_label="Família",
        )
        db.add(account)
        db.commit()

        (tmp_path / "statement.csv").write_text(
            "data,descricao,valor\n2026-09-01,Compra teste,-10.00\n", encoding="utf-8"
        )
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")
        manifest = InitialLoadManifest(
            version=1,
            load_id="preview-test",
            accounts=[AccountSeed(key="bank", name="Conta Teste")],
            documents=[
                DocumentSeed(path="statement.csv", document_type="bank_statement", account="bank")
            ],
        )

        report = _preview_documents(db, household.id, manifest_path, manifest)

        assert len(report) == 1
        assert report[0]["status"] == "pending_full_validation"
        assert "note" in report[0]
        assert all(item["status"] not in {"ready", "ready_with_review"} for item in report)


def test_apply_documents_real_pipeline_is_idempotent_on_rerun(tmp_path, monkeypatch) -> None:
    """Regression for the review boundary the mocked
    `test_apply_documents_delegates_to_official_import_pipeline` above
    cannot cover: this calls the real, unmocked `import_document` so
    encryption, hashing/idempotency, classification, duplicate grouping,
    persisted reconciliation and the audit trail are all genuinely
    exercised, then asserts a second application of the same document is a
    true no-op."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        with Session(_engine()) as db:
            household, user = _seed_household(db)
            account = Account(
                household_id=household.id,
                name="Conta Teste",
                institution="Banco Teste",
                account_type="checking",
                owner_label="Família",
            )
            db.add(account)
            db.commit()

            (tmp_path / "statement.csv").write_text(
                "data,descricao,valor\n2026-09-01,Compra teste,-10.00\n", encoding="utf-8"
            )
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            manifest = InitialLoadManifest(
                version=1,
                load_id="real-pipeline-test",
                accounts=[AccountSeed(key="bank", name="Conta Teste")],
                documents=[
                    DocumentSeed(
                        path="statement.csv", document_type="bank_statement", account="bank"
                    )
                ],
            )

            first_report = asyncio.run(
                _apply_documents(db, manifest_path, manifest, {"bank": account}, user)
            )
            assert len(first_report) == 1
            assert first_report[0]["status"] in {"imported", "imported_with_review"}
            assert db.scalar(select(func.count(Document.id))) == 1
            assert db.scalar(select(func.count(Transaction.id))) == 1

            second_report = asyncio.run(
                _apply_documents(db, manifest_path, manifest, {"bank": account}, user)
            )
            assert second_report[0]["status"] == "skipped_existing"
            assert db.scalar(select(func.count(Document.id))) == 1
            assert db.scalar(select(func.count(Transaction.id))) == 1
    finally:
        get_settings.cache_clear()


def test_apply_documents_resumes_only_remaining_after_partial_run(tmp_path, monkeypatch) -> None:
    """Each document is its own commit unit (`_apply_documents` /
    `import_document`), so an interrupted run is equivalent to having only
    applied a manifest prefix. Rerunning the full manifest must skip what
    was already committed and complete only the remainder, never
    duplicating the first document."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        with Session(_engine()) as db:
            household, user = _seed_household(db)
            account = Account(
                household_id=household.id,
                name="Conta Teste",
                institution="Banco Teste",
                account_type="checking",
                owner_label="Família",
            )
            db.add(account)
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
                load_id="resume-test",
                accounts=[AccountSeed(key="bank", name="Conta Teste")],
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
    finally:
        get_settings.cache_clear()


def test_manifest_validation_error_never_leaks_input_value(tmp_path) -> None:
    """Work Order `docs/WORK_ORDER_INITIAL_LOAD_BOOTSTRAP.md` ("Segurança e
    privacidade") covers the manifest-rejection path too: Pydantic's default
    `str(ValidationError)` appends `input_value=...`, so an invalid salary in
    the manifest would otherwise reappear verbatim on stdout via `main()`'s
    `except (ValueError, HTTPException)` handler. `load_manifest` must report
    which field failed without echoing the value itself."""
    sentinel = "-918273.45"
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "load_id": "privacy-validation-error",
                "accounts": [],
                "profile": {"monthly_salary_net": sentinel},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_manifest(path)

    message = str(exc_info.value)
    assert sentinel not in message
    assert "monthly_salary_net" in message


def test_sanitize_validation_error_strips_input_value_by_construction() -> None:
    """Unit-level companion to the test above: proves the helper itself
    drops Pydantic's `input_value=...`/`input_type=...` suffix for a
    directly constructed `ValidationError`, independent of `load_manifest`'s
    plumbing."""
    from pydantic import ValidationError

    sentinel = Decimal("-42.42")
    try:
        ProfileSeed(monthly_salary_net=sentinel)
    except ValidationError as exc:
        message = _sanitize_validation_error(exc)
    else:
        pytest.fail("expected ProfileSeed to reject a negative salary")

    assert str(sentinel) not in message
    assert "input_value" not in message
    assert "monthly_salary_net" in message


def test_document_not_found_error_reports_file_name_only(tmp_path) -> None:
    """The Work Order's stdout contract allows file names, not local
    directory structure -- a resolved absolute path can embed the
    operator's home directory or a real household/document folder name
    chosen outside Git."""
    nested = tmp_path / "real-household-name" / "extrato.csv"
    manifest_path = tmp_path / "manifest.json"
    seed = DocumentSeed(path=str(nested), document_type="bank_statement", account="bank")

    with pytest.raises(ValueError) as exc_info:
        _document_path(manifest_path, seed)

    message = str(exc_info.value)
    assert "extrato.csv" in message
    assert "real-household-name" not in message
    assert str(nested.parent) not in message


def test_preview_parse_failure_never_leaks_document_fragment(tmp_path) -> None:
    """`_preview_documents` must not forward `str(exc)` from the parser:
    `parse_decimal`/`parse_date`/the payroll month lookup all build their
    message from the document's own content (see
    `app/services/importer.py`), so doing so would put a fragment of a real
    financial document on stdout in exactly the path meant to report
    `status`/`records`, violating the same stdout contract as the profile
    values fix."""
    with Session(_engine()) as db:
        household, _user = _seed_household(db)

        sentinel = "SENTINEL-99.99.99"
        (tmp_path / "statement.csv").write_text(
            f"data,descricao,valor\n2026-09-01,Compra teste,{sentinel}\n", encoding="utf-8"
        )
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")
        manifest = InitialLoadManifest(
            version=1,
            load_id="preview-parse-failure",
            accounts=[AccountSeed(key="bank", name="Conta Teste")],
            documents=[
                DocumentSeed(path="statement.csv", document_type="bank_statement", account="bank")
            ],
        )

        report = _preview_documents(db, household.id, manifest_path, manifest)

        assert len(report) == 1
        assert report[0]["status"] == "review_required"
        assert report[0]["message"] == "parse_failed"
        report_json = json.dumps(report, ensure_ascii=False, default=str)
        assert sentinel not in report_json
