"""WA-07 (`docs/WORK_ORDER_WA_07.md`, issue #79) hardening item 2:
"bounded retries/backoff... explicit timeouts". Unit tests for
`app/services/whatsapp_gateway.py`'s bounded retry/backoff primitives
(`_retry_transient`, `_is_transient_send_error`) and their application to
`MetaCloudApiProvider.send_message`/`fetch_media`, with `urlopen` faked --
no real network call, no real Meta credentials.
"""

from __future__ import annotations

import io
import json
import os
import uuid
from urllib.error import HTTPError, URLError

from cryptography.fernet import Fernet

os.environ.setdefault("SECRET_KEY", "whatsapp-provider-retry-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("WHATSAPP_PHONE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-whatsapp-provider-retry-data-{uuid.uuid4().hex}")

from app.config import get_settings  # noqa: E402
from app.services import whatsapp_gateway as gateway  # noqa: E402

# --- _is_transient_send_error -----------------------------------------------


def test_is_transient_send_error_classifies_network_and_5xx_as_transient() -> None:
    for error in ("URLError", "TimeoutError", "OSError", "http_500", "http_502", "http_599"):
        assert gateway._is_transient_send_error(error) is True, error


def test_is_transient_send_error_never_retries_4xx_or_429_or_none() -> None:
    for error in (None, "", "http_400", "http_401", "http_404", "http_429", "media_metadata_malformed"):
        assert gateway._is_transient_send_error(error) is False, error


# --- _retry_transient ---------------------------------------------------


def test_retry_transient_stops_as_soon_as_attempt_succeeds() -> None:
    calls = {"count": 0}

    def _attempt():
        calls["count"] += 1
        return "transient-error" if calls["count"] < 3 else "ok"

    result = gateway._retry_transient(
        _attempt, is_transient=lambda outcome: outcome == "transient-error", max_attempts=5, backoff_seconds=0.0
    )
    assert result == "ok"
    assert calls["count"] == 3


def test_retry_transient_never_exceeds_max_attempts() -> None:
    calls = {"count": 0}

    def _attempt():
        calls["count"] += 1
        return "always-transient"

    result = gateway._retry_transient(
        _attempt, is_transient=lambda _outcome: True, max_attempts=3, backoff_seconds=0.0
    )
    assert result == "always-transient"
    assert calls["count"] == 3  # first attempt + 2 retries, never a 4th


def test_retry_transient_with_max_attempts_one_disables_retrying() -> None:
    calls = {"count": 0}

    def _attempt():
        calls["count"] += 1
        return "always-transient"

    gateway._retry_transient(_attempt, is_transient=lambda _o: True, max_attempts=1, backoff_seconds=0.0)
    assert calls["count"] == 1


def test_retry_transient_never_retries_a_non_transient_outcome() -> None:
    calls = {"count": 0}

    def _attempt():
        calls["count"] += 1
        return "permanent-error"

    result = gateway._retry_transient(
        _attempt, is_transient=lambda outcome: outcome == "transient-error", max_attempts=5, backoff_seconds=0.0
    )
    assert result == "permanent-error"
    assert calls["count"] == 1


# --- MetaCloudApiProvider.send_message retry -----------------------------


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int | None = None) -> bytes:
        return self._body if size is None else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        return None


def _configure_meta_provider(monkeypatch) -> None:
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-token-never-real")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "test-phone-id")
    monkeypatch.setenv("WHATSAPP_SEND_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("WHATSAPP_MEDIA_FETCH_BACKOFF_SECONDS", "0")
    get_settings.cache_clear()


def _clear_meta_provider(monkeypatch) -> None:
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_SEND_BACKOFF_SECONDS", raising=False)
    monkeypatch.delenv("WHATSAPP_MEDIA_FETCH_BACKOFF_SECONDS", raising=False)
    get_settings.cache_clear()


def test_send_message_retries_transient_network_failure_then_succeeds(monkeypatch) -> None:
    _configure_meta_provider(monkeypatch)
    try:
        calls = {"count": 0}

        def _fake_urlopen(_request, timeout):  # noqa: ARG001
            calls["count"] += 1
            if calls["count"] < 2:
                raise URLError("connection refused")
            return _FakeResponse(json.dumps({"messages": [{"id": "wamid-out-1"}]}).encode())

        monkeypatch.setattr(gateway, "urlopen", _fake_urlopen)
        provider = gateway.MetaCloudApiProvider()
        result = provider.send_message(gateway.build_outbound_text_message(to_digits="5511999998888", body="oi"))

        assert result.ok is True
        assert result.provider_message_id == "wamid-out-1"
        assert calls["count"] == 2
    finally:
        _clear_meta_provider(monkeypatch)


def test_send_message_never_retries_a_4xx_http_error(monkeypatch) -> None:
    _configure_meta_provider(monkeypatch)
    try:
        calls = {"count": 0}

        def _fake_urlopen(_request, timeout):  # noqa: ARG001
            calls["count"] += 1
            raise HTTPError("https://graph.facebook.com/x", 400, "Bad Request", {}, io.BytesIO(b"{}"))

        monkeypatch.setattr(gateway, "urlopen", _fake_urlopen)
        provider = gateway.MetaCloudApiProvider()
        result = provider.send_message(gateway.build_outbound_text_message(to_digits="5511999998888", body="oi"))

        assert result.ok is False
        assert calls["count"] == 1  # never retried
    finally:
        _clear_meta_provider(monkeypatch)


def test_send_message_gives_up_after_exhausting_retries_on_persistent_5xx(monkeypatch) -> None:
    _configure_meta_provider(monkeypatch)
    try:
        calls = {"count": 0}

        def _fake_urlopen(_request, timeout):  # noqa: ARG001
            calls["count"] += 1
            raise HTTPError("https://graph.facebook.com/x", 503, "Service Unavailable", {}, io.BytesIO(b"{}"))

        monkeypatch.setattr(gateway, "urlopen", _fake_urlopen)
        provider = gateway.MetaCloudApiProvider()
        result = provider.send_message(gateway.build_outbound_text_message(to_digits="5511999998888", body="oi"))

        assert result.ok is False
        assert calls["count"] == get_settings().whatsapp_send_max_attempts
    finally:
        _clear_meta_provider(monkeypatch)


# --- MetaCloudApiProvider.fetch_media retry -------------------------------


def test_fetch_media_retries_transient_failure_on_metadata_step(monkeypatch) -> None:
    _configure_meta_provider(monkeypatch)
    try:
        calls = {"metadata": 0, "bytes": 0}

        def _fake_urlopen(request, timeout):  # noqa: ARG001
            url = request.full_url if hasattr(request, "full_url") else request.get_full_url()
            if url.endswith("/media-id-1"):
                calls["metadata"] += 1
                if calls["metadata"] < 2:
                    raise URLError("timeout")
                return _FakeResponse(
                    json.dumps({"url": "https://cdn.example/blob", "mime_type": "image/jpeg"}).encode()
                )
            calls["bytes"] += 1
            return _FakeResponse(b"fake-image-bytes")

        monkeypatch.setattr(gateway, "urlopen", _fake_urlopen)
        provider = gateway.MetaCloudApiProvider()
        result = provider.fetch_media("media-id-1")

        assert result.ok is True
        assert result.payload == b"fake-image-bytes"
        assert result.mime_type == "image/jpeg"
        assert calls["metadata"] == 2
        assert calls["bytes"] == 1
    finally:
        _clear_meta_provider(monkeypatch)


def test_fetch_media_unconfigured_provider_fails_closed_without_network_call(monkeypatch) -> None:
    _clear_meta_provider(monkeypatch)
    provider = gateway.MetaCloudApiProvider()
    assert provider.configured is False
    result = provider.fetch_media("any-media-id")
    assert result.ok is False
    assert result.error == "whatsapp_media_fetch_not_configured"
