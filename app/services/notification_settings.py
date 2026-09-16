"""Due-date e-mail alert configuration: household-wide settings and
recipients (MAIL-00, `docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #67).

This module only persists *configuration*. It never reads `Obligation`,
never decides who gets alerted about what, and never sends anything --
that is MAIL-02's outbox/scheduler and MAIL-01's SMTP adapter,
respectively, each its own later slice. Nothing here is a financial fact:
`app.models.NotificationSettings`/`NotificationRecipient` are deliberately
absent from `app.models.FINANCIAL_REVISION_MODELS`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

# A pragmatic, RFC 5321-adjacent check -- not a full grammar. Good enough to
# reject obvious typos/garbage without pulling in a new dependency
# (`pydantic[email]`/`email-validator` is not installed anywhere in this
# project today; see `pyproject.toml`).
_EMAIL_PATTERN = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,24}$")
_MAX_EMAIL_LENGTH = 254

# Deliberately a curated whitelist, not `zoneinfo.available_timezones()`:
# the production image (`python:3.12-slim`) does not guarantee the OS
# `tzdata` package (and thus `/usr/share/zoneinfo`) is present, so a
# `zoneinfo.ZoneInfo(...)` validity check could pass in local dev/CI and
# still raise `ZoneInfoNotFoundError` at request time in Docker --
# non-deterministic across environments is worse than a fixed, explicit
# list here. Scoped to the IANA zones that actually exist in Brazil (this
# app has no non-Brazilian household anywhere in its documentation or
# data), matching `app.config.Settings.default_timezone`.
_ALLOWED_TIMEZONES = frozenset(
    {
        "America/Sao_Paulo",
        "America/Manaus",
        "America/Rio_Branco",
        "America/Noronha",
        "America/Cuiaba",
        "America/Campo_Grande",
        "America/Belem",
        "America/Fortaleza",
        "America/Recife",
        "America/Bahia",
        "America/Maceio",
        "America/Araguaina",
        "America/Boa_Vista",
        "America/Porto_Velho",
        "America/Eirunepe",
        "UTC",
    }
)

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class NotificationSettingsError(ValueError):
    """A request that fails a notification-settings/recipient precondition
    (invalid e-mail, invalid time/timezone, duplicate recipient)."""


@dataclass(frozen=True, slots=True)
class NormalizedEmail:
    email: str
    normalized_email: str


def normalize_email(raw_email: str) -> NormalizedEmail:
    """Validates and normalizes an e-mail address for storage/comparison.

    Normalization is lowercase + trim only (no Gmail-style dot/plus-alias
    folding): the Work Order's uniqueness key is "o endereço normalizado",
    and folding aliases could silently merge two addresses a household
    actually intends to keep distinct (e.g. a shared vs. personal inbox).
    """

    clean = (raw_email or "").strip()
    if not clean:
        raise NotificationSettingsError("O e-mail não pode ser vazio")
    if len(clean) > _MAX_EMAIL_LENGTH:
        raise NotificationSettingsError("O e-mail é muito longo")
    if not _EMAIL_PATTERN.match(clean):
        raise NotificationSettingsError("E-mail inválido")
    return NormalizedEmail(email=clean, normalized_email=clean.lower())


def validate_send_time_local(value: str) -> str:
    clean = (value or "").strip()
    if not _TIME_PATTERN.match(clean):
        raise NotificationSettingsError("Horário inválido -- use HH:MM (ex.: 08:00)")
    return clean


def validate_timezone(value: str) -> str:
    clean = (value or "").strip()
    if clean not in _ALLOWED_TIMEZONES:
        raise NotificationSettingsError(
            "Fuso horário não suportado -- use um fuso IANA do Brasil (ex.: America/Sao_Paulo)"
        )
    return clean


def get_or_create_notification_settings(db: Session, *, household_id: str) -> Any:
    from app.models import NotificationSettings

    settings = db.scalar(
        select(NotificationSettings).where(NotificationSettings.household_id == household_id)
    )
    if settings is None:
        settings = NotificationSettings(household_id=household_id)
        db.add(settings)
        db.flush()
    return settings


def update_notification_settings(
    db: Session,
    *,
    household_id: str,
    enabled: bool,
    send_time_local: str,
    timezone: str,
) -> Any:
    settings = get_or_create_notification_settings(db, household_id=household_id)
    settings.enabled = bool(enabled)
    settings.send_time_local = validate_send_time_local(send_time_local)
    settings.timezone = validate_timezone(timezone)
    db.flush()
    return settings


def list_notification_recipients(db: Session, *, household_id: str) -> list[Any]:
    from app.models import NotificationRecipient

    query = (
        select(NotificationRecipient)
        .where(NotificationRecipient.household_id == household_id)
        .order_by(NotificationRecipient.created_at.asc())
    )
    return list(db.scalars(query))


def get_notification_recipient(db: Session, *, household_id: str, recipient_id: str) -> Any:
    from app.models import NotificationRecipient

    recipient = db.scalar(
        select(NotificationRecipient).where(
            NotificationRecipient.id == recipient_id,
            NotificationRecipient.household_id == household_id,
        )
    )
    if recipient is None:
        raise LookupError("notification recipient not found")
    return recipient


# Internal alias kept short for the mutators below.
_get_recipient = get_notification_recipient


def _assert_email_available(
    db: Session, *, household_id: str, normalized_email: str, exclude_recipient_id: str | None = None
) -> None:
    from app.models import NotificationRecipient

    query = select(NotificationRecipient.id).where(
        NotificationRecipient.household_id == household_id,
        NotificationRecipient.normalized_email == normalized_email,
    )
    if exclude_recipient_id is not None:
        query = query.where(NotificationRecipient.id != exclude_recipient_id)
    if db.scalar(query) is not None:
        raise NotificationSettingsError("Este e-mail já está cadastrado para esta família")


def create_notification_recipient(
    db: Session,
    *,
    household_id: str,
    email: str,
    active: bool = True,
    notify_d1: bool = True,
    notify_d0: bool = True,
) -> Any:
    from app.models import NotificationRecipient

    normalized = normalize_email(email)
    _assert_email_available(db, household_id=household_id, normalized_email=normalized.normalized_email)
    recipient = NotificationRecipient(
        household_id=household_id,
        email=normalized.email,
        normalized_email=normalized.normalized_email,
        active=bool(active),
        notify_d1=bool(notify_d1),
        notify_d0=bool(notify_d0),
    )
    db.add(recipient)
    db.flush()
    return recipient


def update_notification_recipient(
    db: Session,
    *,
    household_id: str,
    recipient_id: str,
    email: str | None = None,
    active: bool | None = None,
    notify_d1: bool | None = None,
    notify_d0: bool | None = None,
) -> Any:
    recipient = _get_recipient(db, household_id=household_id, recipient_id=recipient_id)
    if email is not None:
        normalized = normalize_email(email)
        _assert_email_available(
            db,
            household_id=household_id,
            normalized_email=normalized.normalized_email,
            exclude_recipient_id=recipient.id,
        )
        recipient.email = normalized.email
        recipient.normalized_email = normalized.normalized_email
    if active is not None:
        recipient.active = bool(active)
    if notify_d1 is not None:
        recipient.notify_d1 = bool(notify_d1)
    if notify_d0 is not None:
        recipient.notify_d0 = bool(notify_d0)
    db.flush()
    return recipient


def delete_notification_recipient(db: Session, *, household_id: str, recipient_id: str) -> None:
    recipient = _get_recipient(db, household_id=household_id, recipient_id=recipient_id)
    db.delete(recipient)
    db.flush()


def serialize_notification_settings(settings: Any) -> dict[str, Any]:
    return {
        "enabled": settings.enabled,
        "send_time_local": settings.send_time_local,
        "timezone": settings.timezone,
    }


def serialize_notification_recipient(recipient: Any) -> dict[str, Any]:
    return {
        "id": recipient.id,
        "email": recipient.email,
        "active": recipient.active,
        "notify_d1": recipient.notify_d1,
        "notify_d0": recipient.notify_d0,
    }
