"""Unit tests for MAIL-01 (`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`,
issue #68) e-mail templates: pure string rendering, no DB, no network.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.services.notification_templates import (
    format_brl,
    render_due_date_alert,
    render_test_email,
)


def test_format_brl_thousands_and_cents() -> None:
    assert format_brl(Decimal("1234567.89")) == "R$ 1.234.567,89"
    assert format_brl(Decimal("0")) == "R$ 0,00"
    assert format_brl(Decimal("9.5")) == "R$ 9,50"


def test_format_brl_negative_is_prefixed_not_hidden() -> None:
    assert format_brl(Decimal("-150.30")) == "-R$ 150,30"


def test_render_due_date_alert_d1_subject_and_body() -> None:
    rendered = render_due_date_alert(
        obligation_name="Aluguel",
        amount=Decimal("1500.00"),
        due_date=date(2026, 10, 15),
        alert_kind="d1",
        category="Moradia",
    )
    assert rendered.subject == "Family Finance — conta vence amanhã: Aluguel"
    assert "vence amanhã" in rendered.text_body
    assert "R$ 1.500,00" in rendered.text_body
    assert "15/10/2026" in rendered.text_body
    assert "Moradia" in rendered.text_body
    assert "R$ 1.500,00" in rendered.html_body
    assert "15/10/2026" in rendered.html_body


def test_render_due_date_alert_d0_subject() -> None:
    rendered = render_due_date_alert(
        obligation_name="Cartão Nubank",
        amount=Decimal("320.10"),
        due_date=date(2026, 10, 14),
        alert_kind="d0",
    )
    assert rendered.subject == "Family Finance — conta vence hoje: Cartão Nubank"
    assert "vence hoje" in rendered.text_body
    # No category supplied: neither body should claim one.
    assert "Categoria" not in rendered.text_body
    assert "Categoria" not in rendered.html_body


def test_render_due_date_alert_rejects_unknown_alert_kind() -> None:
    with pytest.raises(ValueError):
        render_due_date_alert(
            obligation_name="x",
            amount=Decimal("1.00"),
            due_date=date(2026, 1, 1),
            alert_kind="d2",
        )


def test_render_due_date_alert_escapes_html_in_free_text_fields() -> None:
    """`obligation_name`/`category` are household-entered free text (an
    `Obligation.name` a user typed) -- the HTML body must never let it break
    out of the markup it is interpolated into."""

    rendered = render_due_date_alert(
        obligation_name="<script>alert(1)</script>",
        amount=Decimal("10.00"),
        due_date=date(2026, 1, 1),
        alert_kind="d1",
        category="<img src=x onerror=alert(1)>",
    )
    assert "<script>" not in rendered.html_body
    assert "&lt;script&gt;" in rendered.html_body
    assert "<img src=x" not in rendered.html_body
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered.html_body
    # The raw text body is not HTML and is allowed to carry the literal text.
    assert "<script>alert(1)</script>" in rendered.text_body


def test_render_test_email_has_no_obligation_content() -> None:
    rendered = render_test_email()
    assert rendered.subject == "Family Finance — e-mail de teste"
    assert "teste" in rendered.text_body.lower()
    assert "teste" in rendered.html_body.lower()
    # Never leaks anything resembling due-date alert content.
    assert "vence" not in rendered.text_body.lower()
