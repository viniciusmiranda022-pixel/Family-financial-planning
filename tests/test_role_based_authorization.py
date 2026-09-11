"""HTTP-level tests for Fase 4: perfis separados para administrador e consulta.

`docs/WORK_ORDER_ADMIN_READONLY_PROFILES.md`: `User.is_admin` is reused
as-is; the only new behavior is that `app.api._require_admin` -- already
used by a handful of routes (`POST /users`, the monthly-close lifecycle) --
is now applied consistently to every mutating route. This module exercises
that boundary end to end over the real FastAPI app:

- a consulta (`is_admin=False`) user reads freely within its own household
  but gets `403` with no side effect on every create/update/delete/import/
  confirm/link/unlink/lifecycle/close/run/trust/reopen/config route;
- an admin keeps the exact same mutable access it always had;
- consulta cannot create or promote an admin (`POST /users` is itself
  admin-gated, so this falls out of the same boundary);
- household isolation stays fail-closed for both roles.

Gives itself a fully isolated database via `dependency_overrides[get_db]`
(same reasoning as `tests/test_monthly_close_api.py`): `app.db.engine` is a
process-wide singleton bound to whichever `DATABASE_URL` the first-imported
test module set, so a plain `os.environ["DATABASE_URL"] = ...` here would
silently share a database with another test module instead of isolating
this one.
"""

import os
import uuid
from dataclasses import dataclass
from unittest import mock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "role-based-authz-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-role-authz-data-{uuid.uuid4().hex}")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditEvent, Category, Household, User  # noqa: E402
from app.security import hash_password  # noqa: E402

_test_engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_TestSessionLocal = sessionmaker(bind=_test_engine, autoflush=False, expire_on_commit=False)
Base.metadata.create_all(bind=_test_engine)


def _override_get_db():
    db = _TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _isolated_db():
    """Install this module's isolated `get_db` override only for the
    duration of one of *this module's* tests, then restore whatever was
    there before.

    `app.dependency_overrides` lives on the process-wide `app` singleton
    (`app.main.app`), so an unconditional module-level assignment -- the
    pattern `tests/test_monthly_close_api.py` uses -- would otherwise stay
    installed for the rest of the pytest process the moment this module is
    *collected* (before any of its tests even run), silently redirecting
    every other test module's `TestClient(app)` calls at this module's
    database too. Scoping it to a fixture keeps that blast radius to
    exactly this module's own tests.
    """

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


@dataclass(slots=True)
class RoleFixture:
    admin: TestClient
    consulta: TestClient
    account_id: str
    second_account_id: str
    category_id: str
    household_name: str


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _create_household_admin(*, household_name: str, username: str, password: str) -> None:
    """Insert a household + its first admin directly, bypassing `POST
    /auth/setup` -- that endpoint 409s once *any* `User` row exists
    anywhere in the database (single-tenant bootstrap), which this module
    would otherwise hit on every household after the first. Same pattern
    `tests/test_monthly_close_api.py` already uses for its second,
    cross-household case.
    """

    with _TestSessionLocal() as db:
        household = Household(name=household_name)
        db.add(household)
        db.flush()
        db.add(
            User(
                household_id=household.id,
                name="Administrador",
                username=username,
                password_hash=hash_password(password),
                is_admin=True,
            )
        )
        db.add(Category(household_id=household.id, name="Geral", color="#888888"))
        db.commit()


def _setup_household(tag: str) -> RoleFixture:
    household_name = f"Família {tag}"
    admin_username = _unique(f"admin-{tag}")
    consulta_username = _unique(f"consulta-{tag}")
    password = "senha-local-bem-segura"

    _create_household_admin(household_name=household_name, username=admin_username, password=password)
    admin = TestClient(app)
    login_admin = admin.post(
        "/api/auth/login", json={"username": admin_username, "password": password}
    )
    assert login_admin.status_code == 200, login_admin.text

    created = admin.post(
        "/api/users",
        json={
            "name": "Usuário Consulta",
            "username": consulta_username,
            "password": password,
            "is_admin": False,
        },
    )
    assert created.status_code == 201, created.text

    consulta = TestClient(app)
    login = consulta.post(
        "/api/auth/login", json={"username": consulta_username, "password": password}
    )
    assert login.status_code == 200, login.text
    assert login.json()["is_admin"] is False

    account = admin.post(
        "/api/accounts",
        json={"name": "Conta corrente", "account_type": "checking", "owner_label": "Família"},
    )
    assert account.status_code == 201, account.text
    second_account = admin.post(
        "/api/accounts",
        json={"name": "Conta liquidez", "account_type": "investment", "owner_label": "Família"},
    )
    assert second_account.status_code == 201, second_account.text

    category_id = admin.get("/api/categories").json()[0]["id"]

    return RoleFixture(
        admin=admin,
        consulta=consulta,
        account_id=account.json()["id"],
        second_account_id=second_account.json()["id"],
        category_id=category_id,
        household_name=household_name,
    )


@pytest.fixture
def roles() -> RoleFixture:
    return _setup_household(uuid.uuid4().hex[:8])


# ---------------------------------------------------------------------------
# Reads: consulta keeps full read access within its own household.
# ---------------------------------------------------------------------------


def test_consulta_reads_household_data(roles: RoleFixture) -> None:
    read_only_paths = [
        "/api/auth/me",
        "/api/dashboard",
        "/api/accounts",
        "/api/categories",
        f"/api/accounts/{roles.account_id}/balances",
        "/api/imports",
        "/api/transactions",
        "/api/transactions/manual/installment-preview?account_id="
        + roles.account_id
        + "&description=Compra&amount=10&booked_at=2026-08-01"
        + "&installment_current=1&installment_total=3",
        "/api/reviews",
        "/api/duplicate-groups",
        "/api/card-payment-reconciliations",
        "/api/card-payment-reconciliations/invoices",
        "/api/classification-rules",
        "/api/commissions",
        "/api/payroll",
        "/api/obligations",
        "/api/profile",
        "/api/forecast",
        "/api/cut-plan",
        "/api/reports",
        "/api/captures",
        "/api/integrity/status",
        "/api/integrity/findings",
        "/api/advisor/status",
    ]
    for path in read_only_paths:
        response = roles.consulta.get(path)
        assert response.status_code == 200, f"GET {path} -> {response.status_code}: {response.text}"

    # Pre-existing behavior (unchanged by this Work Order): user management
    # is admin-only reading too, not only writing.
    assert roles.consulta.get("/api/users").status_code == 403


def test_consulta_rejected_from_semantic_audit_and_advisor_chat_without_side_effect(
    roles: RoleFixture,
) -> None:
    """`POST /integrity/semantic-audit` and `POST /advisor/chat` never
    decide or alter a financial fact (see their docstrings in `app/api.py`),
    but both are `POST`s that persist an `AuditEvent` of the consultative
    call itself -- operational state, not a data read, per
    `docs/WORK_ORDER_ADMIN_READONLY_PROFILES.md`. `_require_admin` must
    therefore reject consulta before either route ever reaches the Codex
    client or `db.commit()`.
    """

    with (
        mock.patch("app.api.run_semantic_audit") as semantic_audit_mock,
        mock.patch("app.api.CodexAdvisorClient") as advisor_client_mock,
    ):
        audit_response = roles.consulta.post(
            "/api/integrity/semantic-audit", json={"audit_type": "period_review"}
        )
        assert audit_response.status_code == 403, audit_response.text

        chat_response = roles.consulta.post(
            "/api/advisor/chat", json={"message": "Como está meu mês?"}
        )
        assert chat_response.status_code == 403, chat_response.text

    semantic_audit_mock.assert_not_called()
    advisor_client_mock.assert_not_called()

    with _TestSessionLocal() as db:
        success_events = db.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type.in_(("integrity.semantic_audit", "advisor.question"))
            )
        ).all()
        assert success_events == []

    # Admin keeps full access to both: this is a role boundary, not a
    # removal of the feature.
    admin_audit_response = roles.admin.post(
        "/api/integrity/semantic-audit", json={"audit_type": "period_review"}
    )
    assert admin_audit_response.status_code == 200, admin_audit_response.text

    admin_chat_response = roles.admin.post(
        "/api/advisor/chat", json={"message": "Como está meu mês?"}
    )
    assert admin_chat_response.status_code == 200, admin_chat_response.text


# ---------------------------------------------------------------------------
# Writes: consulta gets 403, fail-closed, on every mutating route.
# ---------------------------------------------------------------------------


def _mutations() -> list[tuple[str, str, str, dict]]:
    """`(label, method, path, request_kwargs)` for every mutating route
    `_require_admin` must guard. Every id here -- account, transaction,
    finding, etc. -- is a deliberately non-existent placeholder, never a
    real id from any fixture: `_require_admin` runs as the first statement
    of every one of these handlers, before any lookup, so a consulta caller
    must be rejected with `403` -- never a `404` that would leak whether the
    id exists -- regardless of whether the id is real. That also means this
    list is pure/static data, safe to build at collection time (before any
    fixture, including the isolated-database one, has run) for
    `@pytest.mark.parametrize`.
    """

    reason = {"reason": "tentativa de acesso indevido"}
    placeholder_account = "placeholder-account-id"
    placeholder_second_account = "placeholder-second-account-id"
    placeholder_category = "placeholder-category-id"
    return [
        ("integrity run", "post", "/api/integrity/runs", {"json": {"scope": "global"}}),
        (
            "finding acknowledge",
            "post",
            "/api/integrity/findings/missing/acknowledge",
            {"json": reason},
        ),
        ("finding resolve", "post", "/api/integrity/findings/missing/resolve", {"json": reason}),
        ("finding ignore", "post", "/api/integrity/findings/missing/ignore", {"json": reason}),
        (
            "finding false-positive",
            "post",
            "/api/integrity/findings/missing/false-positive",
            {"json": reason},
        ),
        ("monthly close run", "post", "/api/monthly-closes/2026-08/run", {}),
        ("monthly close trust", "post", "/api/monthly-closes/2026-08/trust", {}),
        ("monthly close reopen", "post", "/api/monthly-closes/2026-08/reopen", {"json": reason}),
        (
            "card competence repair preview",
            "post",
            "/api/maintenance/card-competence/preview",
            {},
        ),
        (
            "card competence repair apply",
            "post",
            "/api/maintenance/card-competence/apply",
            {
                "json": {
                    "transaction_ids": ["missing-transaction-id"],
                    "expected_financial_revision": 0,
                    "reason": "tentativa de acesso indevido",
                }
            },
        ),
        (
            "card competence repair rollback",
            "post",
            "/api/maintenance/card-competence/rollback",
            {
                "json": {
                    "transaction_ids": ["missing-transaction-id"],
                    "expected_financial_revision": 0,
                    "reason": "tentativa de acesso indevido",
                }
            },
        ),
        (
            "user create",
            "post",
            "/api/users",
            {
                "json": {
                    "name": "Escalada",
                    "username": _unique("escalada"),
                    "password": "senha-bem-segura-123",
                    "is_admin": True,
                }
            },
        ),
        ("user deactivate", "delete", "/api/users/missing", {}),
        (
            "account create",
            "post",
            "/api/accounts",
            {"json": {"name": "Conta indevida", "account_type": "checking"}},
        ),
        (
            "account balance confirm",
            "post",
            "/api/account-balances",
            {
                "json": {
                    "account_id": placeholder_account,
                    "amount": "100.00",
                    "as_of_date": "2026-08-01",
                    "observation_type": "point_in_time",
                }
            },
        ),
        (
            "import single",
            "post",
            "/api/imports",
            {
                "data": {"document_type": "bank_statement"},
                "files": {"file": ("extrato.csv", b"data,valor\n1,2", "text/csv")},
            },
        ),
        (
            "import batch",
            "post",
            "/api/imports/batch",
            {
                "data": {"document_type": "bank_statement"},
                "files": [("files", ("extrato.csv", b"data,valor\n1,2", "text/csv"))],
            },
        ),
        (
            "import reconcile",
            "post",
            "/api/imports/missing/reconcile",
            {},
        ),
        (
            "capture preview",
            "post",
            "/api/captures/preview",
            {"data": {"text": "Gastei 10 reais no mercado"}},
        ),
        ("capture cancel", "delete", "/api/captures/missing", {}),
        ("capture retry", "post", "/api/captures/missing/retry", {}),
        (
            "capture confirm",
            "post",
            "/api/captures/missing/confirm",
            {
                "json": {
                    "items": [
                        {
                            "kind": "transaction",
                            "booked_at": "2026-08-01",
                            "movement_type": "expense",
                            "description": "teste",
                            "amount": "10.00",
                        }
                    ]
                }
            },
        ),
        (
            "transaction create",
            "post",
            "/api/transactions",
            {
                "json": {
                    "booked_at": "2026-08-01",
                    "description": "Compra indevida",
                    "amount": "10.00",
                    "movement_type": "expense",
                    "account_id": placeholder_account,
                }
            },
        ),
        (
            "transfer create",
            "post",
            "/api/transfers",
            {
                "json": {
                    "booked_at": "2026-08-01",
                    "description": "Transferência indevida",
                    "amount": "10.00",
                    "from_account_id": placeholder_account,
                    "to_account_id": placeholder_second_account,
                }
            },
        ),
        ("transaction update", "patch", "/api/transactions/missing", {"json": {"reviewed": True}}),
        ("transaction delete", "delete", "/api/transactions/missing", {}),
        ("review resolve", "post", "/api/reviews/missing/resolve", {}),
        (
            "duplicate group resolve",
            "post",
            "/api/duplicate-groups/missing/resolve",
            {"json": {"resolution": "distinct", "reason": "não é duplicidade"}},
        ),
        (
            "card payment link",
            "post",
            "/api/card-payment-reconciliations/link",
            {
                "json": {
                    "checking_transaction_id": "missing-1",
                    "card_transaction_id": "missing-2",
                    "reason": "vínculo indevido",
                }
            },
        ),
        (
            "card payment unlink",
            "post",
            "/api/card-payment-reconciliations/unlink",
            {"json": {"transaction_id": "missing", "reason": "desvínculo indevido"}},
        ),
        (
            "card payment pay",
            "post",
            "/api/card-payment-reconciliations/pay",
            {
                "json": {
                    "card_transaction_id": "missing",
                    "paying_account_id": placeholder_account,
                    "amount": "10.00",
                    "booked_at": "2026-08-01",
                    "description": "Pagamento indevido",
                    "confirmed": True,
                }
            },
        ),
        ("classification rule activate", "post", "/api/classification-rules/missing/activate", {}),
        (
            "classification rule edit",
            "patch",
            "/api/classification-rules/missing",
            {"json": {"category_id": placeholder_category, "reason": "reclassificação indevida"}},
        ),
        (
            "classification rule deactivate",
            "post",
            "/api/classification-rules/missing/deactivate",
            {"json": {"reason": "desativação indevida"}},
        ),
        (
            "commission create",
            "post",
            "/api/commissions",
            {
                "json": {
                    "description": "Comissão indevida",
                    "expected_date": "2026-08-01",
                    "gross_amount": "100.00",
                }
            },
        ),
        ("commission delete", "delete", "/api/commissions/missing", {}),
        (
            "payroll create",
            "post",
            "/api/payroll",
            {
                "json": {
                    "person_name": "Fulano",
                    "competence": "2026-08-01",
                    "payment_date": "2026-08-05",
                    "net_amount": "1000.00",
                }
            },
        ),
        ("payroll delete", "delete", "/api/payroll/missing", {}),
        (
            "obligation create",
            "post",
            "/api/obligations",
            {"json": {"name": "Conta indevida", "due_date": "2026-08-10", "amount": "50.00"}},
        ),
        ("obligation delete", "delete", "/api/obligations/missing", {}),
        (
            "obligation pay",
            "post",
            "/api/obligations/missing/pay",
            {"json": {"transaction_id": "missing-transaction-id"}},
        ),
        (
            "obligation unpay",
            "post",
            "/api/obligations/missing/unpay",
            {"json": reason},
        ),
        (
            "profile update",
            "put",
            "/api/profile",
            {
                "json": {
                    "monthly_salary_net": "5000.00",
                    "monthly_cash_cap": "2000.00",
                    "emergency_floor": "1000.00",
                    "food_allowance": "0.00",
                    "meal_allowance_daily": "0.00",
                    "workdays_month": 22,
                    "investment_name": "Conta liquidez",
                    "investment_balance": "0.00",
                    "investment_gross_annual_rate": "0.10",
                    "investment_income_tax_rate": "0.15",
                    "projection_end": "2027-12-01",
                }
            },
        ),
        ("financial snapshot rebuild", "post", "/api/financial-snapshots/2026-08/rebuild", {}),
        (
            "integrity semantic audit",
            "post",
            "/api/integrity/semantic-audit",
            {"json": {"audit_type": "period_review"}},
        ),
        ("advisor chat", "post", "/api/advisor/chat", {"json": {"message": "Como está meu mês?"}}),
    ]


@pytest.mark.parametrize("label,method,path,kwargs", _mutations())
def test_consulta_rejected_from_every_mutation(
    roles: RoleFixture, label: str, method: str, path: str, kwargs: dict
) -> None:
    # `roles` (function-scoped) supplies a fresh household/session pair for
    # the consulta caller; `_mutations()` supplies placeholder ids only
    # (see its docstring) -- `_require_admin` rejects the call before any id
    # is ever looked up, so which household issues the request is
    # irrelevant to whether a placeholder id is "real".
    response = getattr(roles.consulta, method)(path, **kwargs)
    assert response.status_code == 403, f"{label}: {method.upper()} {path} -> {response.status_code}: {response.text}"


def test_denied_mutation_has_no_side_effect(roles: RoleFixture) -> None:
    accounts_before = roles.admin.get("/api/accounts").json()
    obligations_before = roles.admin.get("/api/obligations").json()

    denied_account = roles.consulta.post(
        "/api/accounts", json={"name": "Conta fantasma", "account_type": "checking"}
    )
    assert denied_account.status_code == 403
    denied_obligation = roles.consulta.post(
        "/api/obligations",
        json={"name": "Obrigação fantasma", "due_date": "2026-08-10", "amount": "50.00"},
    )
    assert denied_obligation.status_code == 403

    assert roles.admin.get("/api/accounts").json() == accounts_before
    assert roles.admin.get("/api/obligations").json() == obligations_before

    # A 403 from `_require_admin` happens before `audit()`/`db.commit()` are
    # ever reached, so the attempted (rejected) mutation must leave no audit
    # trail of a *successful* create -- matching "requisição negada não
    # produz alteração no banco nem audit event de sucesso da operação
    # proibida" in the Work Order's acceptance criteria.
    with _TestSessionLocal() as db:
        success_events = db.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type.in_(("account.create", "obligation.create")),
                AuditEvent.details.like("%fantasma%"),
            )
        ).all()
        assert success_events == []


def test_consulta_cannot_create_or_promote_admin(roles: RoleFixture) -> None:
    users_before = roles.admin.get("/api/users").json()

    escalation = roles.consulta.post(
        "/api/users",
        json={
            "name": "Auto Promoção",
            "username": _unique("auto-promo"),
            "password": "senha-bem-segura-123",
            "is_admin": True,
        },
    )
    assert escalation.status_code == 403

    users_after = roles.admin.get("/api/users").json()
    assert users_after == users_before
    assert {item["username"] for item in users_after if item["is_admin"]} == {
        item["username"] for item in users_before if item["is_admin"]
    }


# ---------------------------------------------------------------------------
# Admin keeps its existing mutable access; only consulta is newly rejected.
# ---------------------------------------------------------------------------


def test_admin_retains_mutation_access(roles: RoleFixture) -> None:
    created = roles.admin.post(
        "/api/obligations",
        json={"name": "Internet", "due_date": "2026-09-05", "amount": "150.00"},
    )
    assert created.status_code == 201, created.text

    updated_transaction = roles.admin.post(
        "/api/transactions",
        json={
            "booked_at": "2026-08-01",
            "description": "Compra legítima",
            "amount": "20.00",
            "movement_type": "expense",
            "account_id": roles.account_id,
            "category_id": roles.category_id,
        },
    )
    assert updated_transaction.status_code == 201, updated_transaction.text
    transaction_id = updated_transaction.json()["id"]
    patch = roles.admin.patch(f"/api/transactions/{transaction_id}", json={"reviewed": True})
    assert patch.status_code == 200, patch.text
    deleted = roles.admin.delete(f"/api/transactions/{transaction_id}")
    assert deleted.status_code == 200, deleted.text

    integrity_run = roles.admin.post("/api/integrity/runs", json={"scope": "global"})
    assert integrity_run.status_code == 201, integrity_run.text


# ---------------------------------------------------------------------------
# Household isolation stays fail-closed for both roles.
# ---------------------------------------------------------------------------


def test_household_isolation_holds_for_both_roles(roles: RoleFixture) -> None:
    other = _setup_household(uuid.uuid4().hex[:8])
    assert roles.household_name != other.household_name

    # Reads: neither role of household B ever sees household A's account by
    # id (both households legitimately create same-named accounts in
    # `_setup_household`, so identity, not the name, is what must stay
    # isolated).
    own_account_ids = {item["id"] for item in roles.admin.get("/api/accounts").json()}
    assert roles.account_id in own_account_ids
    assert roles.account_id not in {item["id"] for item in other.admin.get("/api/accounts").json()}
    assert roles.account_id not in {item["id"] for item in other.consulta.get("/api/accounts").json()}

    # Writes: household B's *admin* (so `_require_admin` alone doesn't
    # explain the rejection) still cannot reach a real row that belongs to
    # household A -- the household_id filter must fail closed with `404`,
    # same as it always did, role notwithstanding.
    own_transaction = roles.admin.post(
        "/api/transactions",
        json={
            "booked_at": "2026-08-01",
            "description": "Só desta família",
            "amount": "5.00",
            "movement_type": "expense",
            "account_id": roles.account_id,
            "category_id": roles.category_id,
        },
    )
    assert own_transaction.status_code == 201, own_transaction.text
    cross_household_patch = other.admin.patch(
        f"/api/transactions/{own_transaction.json()['id']}", json={"reviewed": True}
    )
    assert cross_household_patch.status_code == 404

    cross_household_balance_history = other.admin.get(f"/api/accounts/{roles.account_id}/balances")
    assert cross_household_balance_history.status_code == 404
