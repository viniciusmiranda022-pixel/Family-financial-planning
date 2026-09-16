"""Service-level tests for MAIL-00, due-date e-mail alert configuration
(`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #67). Mirrors
`tests/test_entry_type_templates.py`'s structure -- this module exercises
`app.services.notification_settings` directly against an in-memory SQLite
database. HTTP-level household isolation/admin-authorization coverage lives
in `tests/test_role_based_authorization.py`
(`test_consulta_rejected_from_every_mutation`/`test_consulta_reads_household_data`).
"""

import os
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-notification-settings-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "notification-settings-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.db import Base  # noqa: E402
from app.models import Household, NotificationRecipient, NotificationSettings, User  # noqa: E402
from app.services.notification_settings import (  # noqa: E402
    NotificationSettingsError,
    create_notification_recipient,
    delete_notification_recipient,
    get_notification_recipient,
    get_or_create_notification_settings,
    list_notification_recipients,
    normalize_email,
    update_notification_recipient,
    update_notification_settings,
    validate_send_time_local,
    validate_timezone,
)


def _household(db: Session, *, name: str = "Família") -> Household:
    household = Household(name=name)
    db.add(household)
    db.flush()
    return household


def _engine_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


# ---------------------------------------------------------------------------
# Settings: defaults, get-or-create, update validation.
# ---------------------------------------------------------------------------


def test_settings_default_to_disabled_and_household_default_timezone() -> None:
    with _engine_session() as db:
        household = _household(db)
        settings = get_or_create_notification_settings(db, household_id=household.id)
        assert settings.enabled is False
        assert settings.send_time_local == "08:00"
        assert settings.timezone == "America/Sao_Paulo"


def test_get_or_create_is_idempotent_one_row_per_household() -> None:
    with _engine_session() as db:
        household = _household(db)
        first = get_or_create_notification_settings(db, household_id=household.id)
        second = get_or_create_notification_settings(db, household_id=household.id)
        assert first.id == second.id
        rows = list(db.scalars(select(NotificationSettings).where(NotificationSettings.household_id == household.id)))
        assert len(rows) == 1


def test_update_settings_persists_enabled_time_and_timezone() -> None:
    with _engine_session() as db:
        household = _household(db)
        updated = update_notification_settings(
            db, household_id=household.id, enabled=True, send_time_local="07:30", timezone="America/Manaus"
        )
        assert updated.enabled is True
        assert updated.send_time_local == "07:30"
        assert updated.timezone == "America/Manaus"


def test_invalid_send_time_is_rejected() -> None:
    with _engine_session() as db:
        household = _household(db)
        try:
            update_notification_settings(
                db, household_id=household.id, enabled=True, send_time_local="25:00", timezone="America/Sao_Paulo"
            )
            raise AssertionError("expected NotificationSettingsError")
        except NotificationSettingsError:
            pass


def test_unsupported_timezone_is_rejected() -> None:
    with _engine_session() as db:
        household = _household(db)
        try:
            update_notification_settings(
                db, household_id=household.id, enabled=True, send_time_local="08:00", timezone="Europe/Lisbon"
            )
            raise AssertionError("expected NotificationSettingsError")
        except NotificationSettingsError:
            pass


def test_validate_send_time_local_accepts_boundary_values() -> None:
    assert validate_send_time_local("00:00") == "00:00"
    assert validate_send_time_local("23:59") == "23:59"


def test_validate_timezone_accepts_only_curated_brazilian_zones() -> None:
    assert validate_timezone("UTC") == "UTC"
    try:
        validate_timezone("America/New_York")
        raise AssertionError("expected NotificationSettingsError")
    except NotificationSettingsError:
        pass


# ---------------------------------------------------------------------------
# Recipients: normalization, uniqueness, CRUD, household isolation.
# ---------------------------------------------------------------------------


def test_normalize_email_lowercases_and_trims() -> None:
    result = normalize_email("  Kelly@Exemplo.COM  ")
    assert result.email == "Kelly@Exemplo.COM"
    assert result.normalized_email == "kelly@exemplo.com"


def test_normalize_email_rejects_garbage() -> None:
    for bad in ["", "   ", "not-an-email", "@exemplo.com", "kelly@", "kelly@exemplo"]:
        try:
            normalize_email(bad)
            raise AssertionError(f"expected NotificationSettingsError for {bad!r}")
        except NotificationSettingsError:
            pass


def test_create_recipient_defaults_active_and_both_alert_kinds() -> None:
    with _engine_session() as db:
        household = _household(db)
        recipient = create_notification_recipient(db, household_id=household.id, email="vinicius@exemplo.com")
        assert recipient.active is True
        assert recipient.notify_d1 is True
        assert recipient.notify_d0 is True
        assert recipient.normalized_email == "vinicius@exemplo.com"


def test_two_distinct_recipients_receive_independent_preferences() -> None:
    with _engine_session() as db:
        household = _household(db)
        first = create_notification_recipient(
            db, household_id=household.id, email="vinicius@exemplo.com", notify_d1=True, notify_d0=False
        )
        second = create_notification_recipient(
            db, household_id=household.id, email="kelly@exemplo.com", notify_d1=False, notify_d0=True
        )
        recipients = list_notification_recipients(db, household_id=household.id)
        assert {item.id for item in recipients} == {first.id, second.id}
        assert first.notify_d1 is True and first.notify_d0 is False
        assert second.notify_d1 is False and second.notify_d0 is True


def test_duplicate_email_case_insensitive_is_rejected_at_service_layer() -> None:
    with _engine_session() as db:
        household = _household(db)
        create_notification_recipient(db, household_id=household.id, email="kelly@exemplo.com")
        try:
            create_notification_recipient(db, household_id=household.id, email="Kelly@Exemplo.com")
            raise AssertionError("expected NotificationSettingsError")
        except NotificationSettingsError:
            pass


def test_duplicate_email_is_also_rejected_by_the_database_unique_constraint() -> None:
    """The service-layer pre-check in `_assert_email_available` is a
    convenience, not the barrier of record; two concurrent requests could
    both pass that check before either commits. The `UniqueConstraint` on
    `(household_id, normalized_email)` is what actually prevents two
    successful rows for the same address -- assert it exists and fires
    independent of the service-layer guard."""

    with _engine_session() as db:
        household = _household(db)
        db.add(
            NotificationRecipient(
                household_id=household.id, email="kelly@exemplo.com", normalized_email="kelly@exemplo.com"
            )
        )
        db.flush()
        db.add(
            NotificationRecipient(
                household_id=household.id, email="kelly2@exemplo.com", normalized_email="kelly@exemplo.com"
            )
        )
        try:
            db.flush()
            raise AssertionError("expected IntegrityError")
        except IntegrityError:
            db.rollback()


def test_same_email_allowed_across_different_households() -> None:
    with _engine_session() as db:
        household_a = _household(db, name="Família A")
        household_b = _household(db, name="Família B")
        recipient_a = create_notification_recipient(db, household_id=household_a.id, email="mesmo@exemplo.com")
        recipient_b = create_notification_recipient(db, household_id=household_b.id, email="mesmo@exemplo.com")
        assert recipient_a.id != recipient_b.id


def test_update_recipient_partial_fields_only_changes_what_is_passed() -> None:
    with _engine_session() as db:
        household = _household(db)
        recipient = create_notification_recipient(db, household_id=household.id, email="vinicius@exemplo.com")
        updated = update_notification_recipient(
            db, household_id=household.id, recipient_id=recipient.id, notify_d1=False
        )
        assert updated.notify_d1 is False
        assert updated.notify_d0 is True
        assert updated.email == "vinicius@exemplo.com"
        assert updated.active is True


def test_update_recipient_email_revalidates_uniqueness() -> None:
    with _engine_session() as db:
        household = _household(db)
        create_notification_recipient(db, household_id=household.id, email="kelly@exemplo.com")
        second = create_notification_recipient(db, household_id=household.id, email="vinicius@exemplo.com")
        try:
            update_notification_recipient(
                db, household_id=household.id, recipient_id=second.id, email="kelly@exemplo.com"
            )
            raise AssertionError("expected NotificationSettingsError")
        except NotificationSettingsError:
            pass


def test_update_recipient_can_keep_its_own_email_unchanged() -> None:
    with _engine_session() as db:
        household = _household(db)
        recipient = create_notification_recipient(db, household_id=household.id, email="kelly@exemplo.com")
        updated = update_notification_recipient(
            db, household_id=household.id, recipient_id=recipient.id, email="Kelly@Exemplo.com", active=False
        )
        assert updated.normalized_email == "kelly@exemplo.com"
        assert updated.active is False


def test_update_unknown_recipient_raises_lookup_error() -> None:
    with _engine_session() as db:
        household = _household(db)
        try:
            update_notification_recipient(db, household_id=household.id, recipient_id="missing", notify_d1=False)
            raise AssertionError("expected LookupError")
        except LookupError:
            pass


def test_delete_recipient_removes_it_and_leaves_others_untouched() -> None:
    with _engine_session() as db:
        household = _household(db)
        keep = create_notification_recipient(db, household_id=household.id, email="kelly@exemplo.com")
        remove = create_notification_recipient(db, household_id=household.id, email="vinicius@exemplo.com")
        delete_notification_recipient(db, household_id=household.id, recipient_id=remove.id)
        remaining = list_notification_recipients(db, household_id=household.id)
        assert [item.id for item in remaining] == [keep.id]


def test_household_isolation_recipient_of_household_b_not_visible_to_a() -> None:
    with _engine_session() as db:
        household_a = _household(db, name="Família A")
        household_b = _household(db, name="Família B")
        recipient_b = create_notification_recipient(db, household_id=household_b.id, email="kelly@exemplo.com")

        assert list_notification_recipients(db, household_id=household_a.id) == []
        try:
            get_notification_recipient(db, household_id=household_a.id, recipient_id=recipient_b.id)
            raise AssertionError("expected LookupError")
        except LookupError:
            pass


def test_notification_settings_and_recipients_are_not_financial_revision_models() -> None:
    """Rebaseline principle: delivery configuration is not a financial
    fact. `HouseholdFinancialRevision` must never bump because an admin
    edited an alert recipient or the send time -- verify neither model is
    wired into that mechanism."""

    from app.models import FINANCIAL_REVISION_MODELS

    assert NotificationSettings not in FINANCIAL_REVISION_MODELS
    assert NotificationRecipient not in FINANCIAL_REVISION_MODELS


def _admin(db: Session, household: Household) -> User:
    admin = User(
        household_id=household.id,
        name="Admin",
        username=f"admin-{uuid.uuid4().hex[:8]}",
        password_hash="hash",
        is_admin=True,
    )
    db.add(admin)
    db.flush()
    return admin


def test_creating_a_recipient_never_touches_household_financial_revision() -> None:
    from app.models import HouseholdFinancialRevision

    with _engine_session() as db:
        household = _household(db)
        _admin(db, household)
        db.commit()
        before = db.scalar(
            select(HouseholdFinancialRevision.revision).where(
                HouseholdFinancialRevision.household_id == household.id
            )
        )
        create_notification_recipient(db, household_id=household.id, email="kelly@exemplo.com")
        db.commit()
        after = db.scalar(
            select(HouseholdFinancialRevision.revision).where(
                HouseholdFinancialRevision.household_id == household.id
            )
        )
        assert before == after
