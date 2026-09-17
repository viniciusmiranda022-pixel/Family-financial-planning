"""Tests for WA-01's WhatsApp webhook gateway (`docs/WORK_ORDER_WA_01.md`,
issue #73): `app/services/whatsapp_gateway.py` (pure primitives) and
`app/whatsapp_gateway_app.py` (the isolated public ASGI app), exercised
without any real Meta credentials or network call -- `FakeWhatsAppProvider`
and hand-built webhook payloads only, matching the Work Order's "Testes com
provider fake; CI sem chamadas externas reais".

Never asserts a `Transaction`/`Obligation`/any financial table is
created -- WA-01 must not be able to (`No WA-02+ orchestration`); several
tests assert the financial tables stay completely empty as an explicit
regression guard for that prohibition.
"""

import hashlib
import hmac
import os
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "whatsapp-gateway-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("WHATSAPP_PHONE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("WHATSAPP_APP_SECRET", "test-meta-app-secret-never-real")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test-verify-token-never-real")
os.environ.setdefault("WHATSAPP_GATEWAY_ENABLED", "true")
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-whatsapp-gateway-data-{uuid.uuid4().hex}")

from app.config import get_settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.models import (  # noqa: E402
    Household,
    Obligation,
    Transaction,
    User,
    WhatsAppAuthorizedNumber,
    WhatsAppInboundEvent,
    WhatsAppRateLimitBucket,
)
from app.services import whatsapp_gateway as gateway  # noqa: E402
from app.whatsapp_gateway_app import gateway_app  # noqa: E402

APP_SECRET = "test-meta-app-secret-never-real"


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    def _override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    gateway_app.dependency_overrides[get_db] = _override_get_db
    return TestClient(gateway_app), session_factory


def _authorize(session_factory, *, sender_digits: str) -> tuple[str, str]:
    """Seeds one household/user/allowlist row authorized for
    `sender_digits`. Returns `(household_id, user_id)`."""

    with session_factory() as db:
        household = Household(name="Família Gateway")
        db.add(household)
        db.flush()
        user = User(
            household_id=household.id,
            name="Vinicius",
            username=f"vinicius-{uuid.uuid4().hex[:8]}",
            password_hash="scrypt$16384$8$1$AAAA$BBBB",
        )
        db.add(user)
        db.flush()
        db.add(
            WhatsAppAuthorizedNumber(
                household_id=household.id,
                user_id=user.id,
                phone_hash=gateway.hash_phone(sender_digits),
                phone_encrypted=gateway.encrypt_phone(sender_digits),
                phone_last4=gateway.phone_last4(sender_digits),
            )
        )
        db.commit()
        return household.id, user.id


def _signed_post(client: TestClient, payload: dict, *, secret: str = APP_SECRET):
    import json

    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"content-type": "application/json", "x-hub-signature-256": signature},
    )


def _inbound_payload(*, message_id: str, sender_digits: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"id": message_id, "from": sender_digits, "type": "text"}
                            ]
                        }
                    }
                ]
            }
        ]
    }


# --- pure primitives -------------------------------------------------------


def test_normalize_phone_strips_formatting_to_digits() -> None:
    assert gateway.normalize_phone("+55 11 99999-8888") == "5511999998888"
    assert gateway.normalize_phone("5511999998888") == "5511999998888"


def test_normalize_phone_rejects_too_short_or_too_long() -> None:
    import pytest

    with pytest.raises(gateway.InvalidPhoneNumberError):
        gateway.normalize_phone("1234")
    with pytest.raises(gateway.InvalidPhoneNumberError):
        gateway.normalize_phone("1" * 20)


def test_verify_signature_accepts_valid_and_rejects_tampered_body() -> None:
    body = b'{"a": 1}'
    valid = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert gateway.verify_signature(body, valid, "secret") is True
    assert gateway.verify_signature(b'{"a": 2}', valid, "secret") is False
    assert gateway.verify_signature(body, None, "secret") is False
    assert gateway.verify_signature(body, "not-even-hex", "secret") is False
    assert gateway.verify_signature(body, valid, "") is False


def test_verify_handshake_matches_configured_token_only() -> None:
    assert gateway.verify_handshake(mode="subscribe", verify_token="tok", configured_token="tok") is True
    assert gateway.verify_handshake(mode="subscribe", verify_token="wrong", configured_token="tok") is False
    assert gateway.verify_handshake(mode="unsubscribe", verify_token="tok", configured_token="tok") is False
    assert gateway.verify_handshake(mode="subscribe", verify_token="tok", configured_token="") is False


def test_fake_provider_records_outbound_payload_without_network() -> None:
    provider = gateway.FakeWhatsAppProvider()
    payload = gateway.build_outbound_text_message(to_digits="5511999998888", body="oi")
    result = provider.send_message(payload)
    assert result.ok is True
    assert provider.sent == [payload]


# --- webhook handshake -------------------------------------------------------


def test_get_handshake_echoes_challenge_when_token_matches() -> None:
    client, _ = _client()
    response = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-verify-token-never-real",
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 200
    assert response.text == "12345"


def test_get_handshake_rejects_wrong_token() -> None:
    client, _ = _client()
    response = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
    )
    assert response.status_code == 403


def test_gateway_disabled_returns_404_for_get_and_post(monkeypatch) -> None:
    client, _ = _client()
    monkeypatch.setenv("WHATSAPP_GATEWAY_ENABLED", "false")
    get_settings.cache_clear()
    try:
        response = client.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"},
        )
        assert response.status_code == 404
        posted = _signed_post(client, _inbound_payload(message_id="m1", sender_digits="5511999998888"))
        assert posted.status_code == 404
    finally:
        monkeypatch.setenv("WHATSAPP_GATEWAY_ENABLED", "true")
        get_settings.cache_clear()


# --- webhook POST: signature, authorization, dedup, rate limit -------------


def test_post_rejects_missing_or_invalid_signature() -> None:
    client, session_factory = _client()
    payload = _inbound_payload(message_id="m1", sender_digits="5511999998888")

    unsigned = client.post("/webhooks/whatsapp", json=payload)
    assert unsigned.status_code == 403

    forged = _signed_post(client, payload, secret="wrong-secret")
    assert forged.status_code == 403

    with session_factory() as db:
        assert db.scalar(select(func.count(WhatsAppInboundEvent.id))) == 0


def test_authorized_sender_is_recorded_as_accepted() -> None:
    client, session_factory = _client()
    household_id, user_id = _authorize(session_factory, sender_digits="5511999998888")

    response = _signed_post(
        client, _inbound_payload(message_id="m1", sender_digits="5511999998888")
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    with session_factory() as db:
        event = db.scalar(select(WhatsAppInboundEvent))
        # WA-03: `_inbound_payload` sends `type: "text"` with no `text.body`
        # (unrealistic for a real Meta payload, but this fixture predates
        # WA-03 and several other tests below still use it just to exercise
        # auth/dedup/rate-limit, not content) -- normalize_inbound_messages
        # extracts no text for it, so the terminal status is
        # "unsupported_content", not the original claim status "accepted".
        assert event.status == "unsupported_content"
        assert event.household_id == household_id
        assert event.user_id == user_id
        assert event.provider_message_id == "m1"


def test_unauthorized_sender_gets_identical_response_and_no_household_leak() -> None:
    client, session_factory = _client()
    # A different number is authorized, but not the one messaging in.
    _authorize(session_factory, sender_digits="5511999998888")

    accepted = _signed_post(client, _inbound_payload(message_id="m1", sender_digits="5511999998888"))
    unauthorized = _signed_post(
        client, _inbound_payload(message_id="m2", sender_digits="5511000001111")
    )

    # Uniform response regardless of internal authorization outcome --
    # Work Order: "número não autorizado... sem vazar PII/existência de
    # household".
    assert accepted.status_code == unauthorized.status_code == 200
    assert accepted.json() == unauthorized.json() == {"status": "ok"}

    with session_factory() as db:
        unauthorized_event = db.scalar(
            select(WhatsAppInboundEvent).where(WhatsAppInboundEvent.provider_message_id == "m2")
        )
        assert unauthorized_event.status == "unauthorized"
        assert unauthorized_event.household_id is None
        assert unauthorized_event.user_id is None


def test_two_authorized_identities_map_independently_to_same_household() -> None:
    """Work Order acceptance criterion: Vinicius/Kelly map individually to
    the same household, never trusting anything from the inbound payload
    beyond the sender's own number."""

    client, session_factory = _client()
    with session_factory() as db:
        household = Household(name="Família Dois Números")
        db.add(household)
        db.flush()
        vinicius = User(
            household_id=household.id,
            name="Vinicius",
            username="vinicius-two",
            password_hash="scrypt$16384$8$1$AAAA$BBBB",
        )
        kelly = User(
            household_id=household.id,
            name="Kelly",
            username="kelly-two",
            password_hash="scrypt$16384$8$1$AAAA$BBBB",
        )
        db.add_all([vinicius, kelly])
        db.flush()
        db.add_all(
            [
                WhatsAppAuthorizedNumber(
                    household_id=household.id,
                    user_id=vinicius.id,
                    phone_hash=gateway.hash_phone("5511999998888"),
                    phone_encrypted=gateway.encrypt_phone("5511999998888"),
                    phone_last4="8888",
                ),
                WhatsAppAuthorizedNumber(
                    household_id=household.id,
                    user_id=kelly.id,
                    phone_hash=gateway.hash_phone("5511977776666"),
                    phone_encrypted=gateway.encrypt_phone("5511977776666"),
                    phone_last4="6666",
                ),
            ]
        )
        db.commit()
        household_id, vinicius_id, kelly_id = household.id, vinicius.id, kelly.id

    _signed_post(client, _inbound_payload(message_id="v1", sender_digits="5511999998888"))
    _signed_post(client, _inbound_payload(message_id="k1", sender_digits="5511977776666"))

    with session_factory() as db:
        vinicius_event = db.scalar(
            select(WhatsAppInboundEvent).where(WhatsAppInboundEvent.provider_message_id == "v1")
        )
        kelly_event = db.scalar(
            select(WhatsAppInboundEvent).where(WhatsAppInboundEvent.provider_message_id == "k1")
        )
        assert vinicius_event.household_id == kelly_event.household_id == household_id
        assert vinicius_event.user_id == vinicius_id
        assert kelly_event.user_id == kelly_id


def test_redelivered_message_id_is_idempotent() -> None:
    client, session_factory = _client()
    _authorize(session_factory, sender_digits="5511999998888")
    payload = _inbound_payload(message_id="dup-1", sender_digits="5511999998888")

    first = _signed_post(client, payload)
    second = _signed_post(client, payload)
    assert first.status_code == second.status_code == 200

    with session_factory() as db:
        assert db.scalar(select(func.count(WhatsAppInboundEvent.id))) == 1


def test_rate_limit_blocks_sender_after_configured_max(monkeypatch) -> None:
    client, session_factory = _client()
    _authorize(session_factory, sender_digits="5511999998888")
    monkeypatch.setenv("WHATSAPP_RATE_LIMIT_MAX_PER_WINDOW", "3")
    get_settings.cache_clear()
    try:
        for i in range(3):
            response = _signed_post(
                client, _inbound_payload(message_id=f"burst-{i}", sender_digits="5511999998888")
            )
            assert response.status_code == 200

        limited = _signed_post(
            client, _inbound_payload(message_id="burst-over", sender_digits="5511999998888")
        )
        assert limited.status_code == 200
        assert limited.json() == {"status": "ok"}

        with session_factory() as db:
            over_event = db.scalar(
                select(WhatsAppInboundEvent).where(
                    WhatsAppInboundEvent.provider_message_id == "burst-over"
                )
            )
            assert over_event.status == "rate_limited"
            assert over_event.household_id is None
    finally:
        monkeypatch.setenv("WHATSAPP_RATE_LIMIT_MAX_PER_WINDOW", "30")
        get_settings.cache_clear()


def test_check_rate_limit_resets_after_window_elapses(monkeypatch) -> None:
    _, session_factory = _client()
    monkeypatch.setenv("WHATSAPP_RATE_LIMIT_MAX_PER_WINDOW", "1")
    get_settings.cache_clear()
    try:
        with session_factory() as db:
            now = datetime(2026, 1, 1, tzinfo=UTC)
            settings = get_settings()
            assert gateway.check_rate_limit(db, bucket_key="k", now=now) is True
            assert gateway.check_rate_limit(db, bucket_key="k", now=now) is False
            later = now + timedelta(seconds=settings.whatsapp_rate_limit_window_seconds + 1)
            assert gateway.check_rate_limit(db, bucket_key="k", now=later) is True
            db.commit()
            assert db.scalar(select(func.count(WhatsAppRateLimitBucket.id))) == 1
    finally:
        monkeypatch.setenv("WHATSAPP_RATE_LIMIT_MAX_PER_WINDOW", "30")
        get_settings.cache_clear()


def test_no_financial_table_is_ever_touched_by_the_webhook() -> None:
    """Regression guard for the Work Order's core prohibition: WA-01 must
    never create/update/delete a `Transaction`/`Obligation`, no matter the
    authorization outcome."""

    client, session_factory = _client()
    _authorize(session_factory, sender_digits="5511999998888")

    _signed_post(client, _inbound_payload(message_id="acc", sender_digits="5511999998888"))
    _signed_post(client, _inbound_payload(message_id="unauth", sender_digits="5511000009999"))

    with session_factory() as db:
        assert db.scalar(select(func.count(Transaction.id))) == 0
        assert db.scalar(select(func.count(Obligation.id))) == 0


def test_non_message_webhook_entries_are_ignored() -> None:
    client, session_factory = _client()
    status_only_payload = {
        "entry": [{"changes": [{"value": {"statuses": [{"id": "s1", "status": "delivered"}]}}]}]
    }
    response = _signed_post(client, status_only_payload)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    with session_factory() as db:
        assert db.scalar(select(func.count(WhatsAppInboundEvent.id))) == 0


def test_malformed_json_body_with_valid_signature_still_acks() -> None:
    client, _ = _client()
    body = b"not json"
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"content-type": "application/json", "x-hub-signature-256": signature},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_endpoint_reports_no_secrets() -> None:
    client, _ = _client()
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "healthy", "gateway_enabled": True}
