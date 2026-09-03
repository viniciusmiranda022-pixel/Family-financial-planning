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
    _resolve_accounts,
    load_manifest,
)
from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AccountBalanceObservation,
    FinancialProfile,
    Household,
    Obligation,
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
            investment_balance=Decimal("1000.00"),
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
        assert second_profile == {"created": False, "changes": {}}
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
        with pytest.raises(ValueError, match="Conflito na conta"):
            _resolve_accounts(
                db,
                household.id,
                conflicting_account,
                apply=True,
                user_id=user.id,
            )
        db.rollback()

        conflicting_obligation = manifest.model_copy(deep=True)
        conflicting_obligation.obligations[0].amount = Decimal("1600.00")
        with pytest.raises(ValueError, match="Conflito na obrigação"):
            _apply_obligations(
                db,
                household.id,
                conflicting_obligation,
                apply=True,
                user_id=user.id,
            )
        db.rollback()

        conflicting_balance = manifest.model_copy(deep=True)
        conflicting_balance.balances[0].amount = Decimal("999.00")
        with pytest.raises(ValueError, match="Conflito de saldo"):
            _apply_balances(
                db,
                household.id,
                conflicting_balance,
                accounts,
                apply=True,
                user_id=user.id,
            )


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
