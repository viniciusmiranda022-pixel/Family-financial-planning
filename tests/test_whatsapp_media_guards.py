"""Unit tests for `app/services/whatsapp_media.py` (WA-07,
`docs/WORK_ORDER_WA_07.md`, issue #79) -- pure functions, no DB/network.
"""

from app.services.whatsapp_media import (
    MediaRejected,
    guard_media_bytes,
    interpret_confirmation_reply,
    media_filename,
)


def test_guard_media_bytes_accepts_allowlisted_image() -> None:
    guard_media_bytes(media_kind="image", mime_type="image/jpeg", payload=b"x" * 10, max_bytes=1000)


def test_guard_media_bytes_strips_mime_parameters() -> None:
    # Real WhatsApp voice notes report "audio/ogg; codecs=opus".
    guard_media_bytes(
        media_kind="audio", mime_type="audio/ogg; codecs=opus", payload=b"x" * 10, max_bytes=1000
    )


def test_guard_media_bytes_rejects_disallowed_mime() -> None:
    try:
        guard_media_bytes(media_kind="image", mime_type="image/gif", payload=b"x" * 10, max_bytes=1000)
        raise AssertionError("expected MediaRejected")
    except MediaRejected as exc:
        assert exc.reason == "mime_not_allowed"
        assert "R$" not in exc.user_message  # sanity: no financial content leaks into a rejection reply


def test_guard_media_bytes_rejects_mime_for_wrong_kind() -> None:
    # A PDF mime_type declared on an "image" message (mismatched/spoofed
    # kind) must not slip through the audio/document allowlists.
    try:
        guard_media_bytes(media_kind="image", mime_type="application/pdf", payload=b"x" * 10, max_bytes=1000)
        raise AssertionError("expected MediaRejected")
    except MediaRejected as exc:
        assert exc.reason == "mime_not_allowed"


def test_guard_media_bytes_rejects_oversized_payload() -> None:
    try:
        guard_media_bytes(
            media_kind="document", mime_type="application/pdf", payload=b"x" * 2000, max_bytes=1000
        )
        raise AssertionError("expected MediaRejected")
    except MediaRejected as exc:
        assert exc.reason == "media_too_large"


def test_guard_media_bytes_rejects_empty_payload() -> None:
    try:
        guard_media_bytes(media_kind="image", mime_type="image/jpeg", payload=b"", max_bytes=1000)
        raise AssertionError("expected MediaRejected")
    except MediaRejected as exc:
        assert exc.reason == "media_empty"


def test_guard_media_bytes_rejects_unknown_kind() -> None:
    try:
        guard_media_bytes(media_kind="sticker", mime_type="image/webp", payload=b"x", max_bytes=1000)
        raise AssertionError("expected MediaRejected")
    except MediaRejected as exc:
        assert exc.reason == "unsupported_media_kind"


def test_media_filename_uses_mime_extension() -> None:
    assert media_filename("image", "image/jpeg", None) == "whatsapp-image.jpg"
    assert media_filename("audio", "audio/ogg; codecs=opus", None) == "whatsapp-audio.ogg"
    assert media_filename("document", "application/pdf", "fatura.pdf") == "whatsapp-document.pdf"


def test_media_filename_falls_back_to_provided_extension() -> None:
    assert media_filename("document", None, "conta.PDF") == "whatsapp-document.pdf"


def test_media_filename_never_trusts_unsafe_provided_extension() -> None:
    # No extension recoverable from either the mime type or a safe suffix of
    # the (attacker-controlled) provided filename -- falls back to a fixed,
    # inert extension rather than embedding arbitrary sender-supplied text.
    assert media_filename("document", None, "../../etc/passwd") == "whatsapp-document.bin"


def test_interpret_confirmation_reply_matches_confirm_words() -> None:
    for text in ("sim", "Sim!", "CONFIRMO", "pode confirmar", "  ok  "):
        assert interpret_confirmation_reply(text).action == "confirm", text


def test_interpret_confirmation_reply_matches_cancel_words() -> None:
    for text in ("não", "nao", "cancelar", "Cancela", "descartar"):
        assert interpret_confirmation_reply(text).action == "cancel", text


def test_interpret_confirmation_reply_ignores_unrelated_text() -> None:
    for text in (
        None,
        "",
        "quanto gastei esse mês?",
        "confirma que hoje é sexta?",
        "gastei 30 no mercado",
    ):
        assert interpret_confirmation_reply(text).action is None, text
