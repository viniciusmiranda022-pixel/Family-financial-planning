"""HTML/plain-text e-mail templates for due-date alerts and the
administrative test e-mail (MAIL-01, `docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`,
issue #68).

Pure string rendering only: no DB access, no `Obligation`/`Transaction`
reads, no decision about *whether* or *when* to send. `render_due_date_alert`
takes the primitives a caller already read from the canonical `Obligation`
record -- this module has no way to look them up itself, so it cannot
become a second financial engine. MAIL-02's worker is the eventual caller
for that one; nothing in this slice invokes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import escape

_APP_NAME = "Family Finance"


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    html_body: str
    text_body: str


def format_brl(amount: Decimal) -> str:
    """`Decimal("1234.5")` -> `"R$ 1.234,50"`. Formats the already-Decimal
    value handed in; never parses a float, matching the project's monetary
    precision rules (`docs/FINANCIAL_INVARIANTS.md`)."""

    quantized = amount.quantize(Decimal("0.01"))
    negative = quantized < 0
    whole, _, cents = f"{abs(quantized):.2f}".partition(".")
    groups: list[str] = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    formatted = f"R$ {'.'.join(groups)},{cents}"
    return f"-{formatted}" if negative else formatted


def _format_date_br(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _wrap_html(*, heading: str, body_html: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="pt-BR"><body style="font-family:Arial,sans-serif;color:#1a1a1a;">'
        f'<h2 style="margin:0 0 12px;">{escape(heading)}</h2>'
        f"{body_html}"
        '<p style="color:#777;font-size:12px;margin-top:16px;">'
        "Esta é uma notificação automática do Family Finance. Nenhuma ação é feita em seu nome."
        "</p></body></html>"
    )


def render_due_date_alert(
    *,
    obligation_name: str,
    amount: Decimal,
    due_date: date,
    alert_kind: str,
    category: str | None = None,
) -> RenderedEmail:
    """Renders the D-1/D0 due-date alert per the Work Order's "Conteúdo do
    e-mail" section. `alert_kind` must be `"d1"` or `"d0"` -- the same two
    values `NotificationRecipient.notify_d1`/`notify_d0` (MAIL-00) gate."""

    if alert_kind not in ("d1", "d0"):
        raise ValueError(f"alert_kind inválido: {alert_kind!r}")

    when_label = "vence amanhã" if alert_kind == "d1" else "vence hoje"
    subject = f"{_APP_NAME} — conta {when_label}: {obligation_name}"
    amount_label = format_brl(amount)
    due_label = _format_date_br(due_date)

    text_lines = [
        _APP_NAME,
        "",
        f"Uma conta {when_label}.",
        "",
        f"Obrigação: {obligation_name}",
        f"Valor: {amount_label}",
        f"Vencimento: {due_label}",
    ]
    if category:
        text_lines.append(f"Categoria: {category}")
    text_lines += [
        "",
        "Esta é uma notificação automática do Family Finance. Nenhuma ação é feita em seu nome.",
    ]

    rows = [
        ("Obrigação", obligation_name),
        ("Valor", amount_label),
        ("Vencimento", due_label),
    ]
    if category:
        rows.append(("Categoria", category))
    rows_html = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#555;">{escape(label)}</td>'
        f'<td style="padding:4px 0;font-weight:600;">{escape(value)}</td></tr>'
        for label, value in rows
    )
    body_html = (
        f'<p>Uma conta <strong>{escape(when_label)}</strong>.</p>'
        f'<table style="border-collapse:collapse;">{rows_html}</table>'
    )

    return RenderedEmail(
        subject=subject,
        html_body=_wrap_html(heading=_APP_NAME, body_html=body_html),
        text_body="\n".join(text_lines),
    )


def render_test_email() -> RenderedEmail:
    """The `POST /notification-settings/test-email` message: confirms SMTP
    is reachable, carries no obligation data (there may be none yet)."""

    subject = f"{_APP_NAME} — e-mail de teste"
    intro = (
        "Este é um e-mail de teste, disparado manualmente pela tela de Configurações "
        "para confirmar que o envio de alertas está operacional."
    )
    confirmation = "Se você recebeu esta mensagem, a configuração de SMTP está funcionando."
    text_body = f"{_APP_NAME}\n\n{intro}\n\n{confirmation}"
    body_html = f"<p>{escape(intro)}</p><p>{escape(confirmation)}</p>"
    return RenderedEmail(
        subject=subject,
        html_body=_wrap_html(heading=_APP_NAME, body_html=body_html),
        text_body=text_body,
    )
