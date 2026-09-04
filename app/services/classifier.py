import re
import unicodedata
from dataclasses import dataclass


def normalize_description(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", value.upper())).strip()


@dataclass(frozen=True)
class Classification:
    category: str
    transaction_type: str
    excluded: bool
    confidence: float
    review_reason: str | None = None


# Canonical Portuguese month abbreviations, shared with
# `app/services/importer.py` (which imports this dict rather than defining
# its own) so the set of "real" three-letter months used to *parse* Nubank
# dates and the set used to *classify* the Nubank invoice-payment line below
# cannot drift apart into two different policies.
MONTHS_PT_ABBR = {
    "JAN": 1,
    "FEV": 2,
    "MAR": 3,
    "ABR": 4,
    "MAI": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8,
    "SET": 9,
    "OUT": 10,
    "NOV": 11,
    "DEZ": 12,
}
_MONTH_PT_ABBR_ALTERNATION = "|".join(MONTHS_PT_ABBR)


# Shared with `app/services/importer.py`, which uses these same patterns to
# decide the *sign* of a credit-card amount (`_normalize_credit_card_amount`)
# and to bucket it for reconciliation (`_transaction_components`). Defining
# them once here and importing them there keeps "what counts as a card
# payment/refund/fee" a single financial policy instead of two regexes that
# could silently drift apart (see docs/WORK_ORDER_PDF_PARSERS_NUBANK_MERCADO_PAGO.md,
# "não criar segunda política de classificação"). `DEVOLU` (Mercado Pago's
# "Devolução") and `ENCARGO` (Mercado Pago's "Tarifas e encargos") are
# additive synonyms: every description the previous, narrower patterns
# already matched still matches.
#
# `PAGAMENTO EM \d{1,2} (JAN|FEV|...)` is the Nubank invoice's own literal
# wording for the credit-card-bill payment line item (observed layout:
# "Pagamento em 01 JUL", i.e. day + three-letter month abbreviation, matching
# the day/month tokens `_NUBANK_CARD_START` and `MONTHS_PT_ABBR` above already
# parse). Before this, that description matched none of the payment
# alternatives, so `classify()` fell through to plain positive `income` --
# violating INV-002 (card-bill payment must be reconciliation, zero operating
# effect) even though `_transaction_components`' separate positive-credit
# fallback already bucketed the same row into `payments_total` for
# reconciliation. Matching it here, in the one shared pattern, is a positive
# identity check -- the exact Nubank phrasing plus a day-of-month and one of
# the twelve real `MONTHS_PT_ABBR` tokens, spelled out from that same dict so
# it cannot drift out of sync with the parser's own month vocabulary -- not a
# generic "any positive credit is a payment" heuristic, and not a bare
# `[A-Z]{3}` wildcard that would also match a non-month token such as "XYZ".
# It does not touch the reconciliation fallback above, which still only ever
# applies to credits no named pattern recognizes.
PAYMENT_PATTERN = re.compile(
    r"PAGAMENTO.*FATURA"
    r"|PAGAMENTO RECEBIDO"
    r"|FATURA PAGA"
    r"|PAG(?:AMENTO)? BOLETO.*(?:NU PAGAMENTOS|NUBANK)"
    rf"|PAGAMENTO EM \d{{1,2}}\s+(?:{_MONTH_PT_ABBR_ALTERNATION})\b"
)
REFUND_PATTERN = re.compile(r"ESTORNO|CREDITO.*COMPRA|CREDITO.*CARTAO|DEVOLU")
FEE_PATTERN = re.compile(r"IOF|JUROS|TARIFA|ENCARGO")


RULES: tuple[tuple[re.Pattern[str], Classification], ...] = (
    (PAYMENT_PATTERN, Classification("Conciliação", "reconciliation", True, 0.99)),
    (
        re.compile(r"PRIVILEGE|PRIVILEGE DI|APLICACAO|RESGATE"),
        Classification("Transferência patrimonial", "transfer", True, 0.97),
    ),
    (REFUND_PATTERN, Classification("Reembolsos e estornos", "refund", False, 0.98)),
    (
        re.compile(r"IFOOD|RESTAURANTE|LANCHONETE|PIZZARIA|PADARIA|PANIF"),
        Classification("Restaurantes e delivery", "expense", False, 0.92),
    ),
    (
        re.compile(r"SUPERMERCADO|MERCADO|HORTIFRUTI|ATACADAO|ASSAI"),
        Classification("Mercado e itens domésticos", "expense", False, 0.92),
    ),
    (
        re.compile(r"CPFL|SABESP|BRK|ENERGIA|AGUA"),
        Classification("Água e energia da residência", "expense", False, 0.95),
    ),
    (
        re.compile(r"POSTO|COMBUSTIVEL|PEDAGIO|SEM PARAR|VIA COLINAS|LOCALIZA|CENTRO AUTOMOTIVO|UBER"),
        Classification("Transporte", "expense", False, 0.9),
    ),
    (
        re.compile(r"DROGARIA|FARMACIA|HOSPITAL|CLINICA|MEDIC"),
        Classification("Saúde e farmácia", "expense", False, 0.9),
    ),
    (re.compile(r"ACADEMIA|SMART FIT|PANOBIANCO"), Classification("Academia", "expense", False, 0.9)),
    (
        re.compile(r"SPOTIFY|APPLE|GOOGLE|ANTHROPIC|OPENAI|NETFLIX|MELI"),
        Classification("Assinaturas digitais", "expense", False, 0.88),
    ),
    (
        re.compile(r"SALAO|BELEZA|ESTETICA|BARBEARIA|CABEL"),
        Classification("Estética e beleza", "expense", False, 0.85),
    ),
    (
        re.compile(r"HOTEL|HOSPEDAGEM|PASSAGEM|TURISMO|AIRBNB"),
        Classification("Viagens e lazer", "expense", False, 0.87),
    ),
    (
        re.compile(r"MERCADOLIVRE|SHOPEE|AMAZON|\bCEA\b|AKI TEM|CENTER PANOS"),
        Classification("Compras, casa e vestuário", "expense", False, 0.86),
    ),
    (re.compile(r"SEGURO|PORTO SEGURO|AZUL SEGUROS"), Classification("Seguros", "expense", False, 0.9)),
    (FEE_PATTERN, Classification("Juros, IOF e tarifas", "expense", False, 0.95)),
)


def classify(description: str, amount: float, internal_aliases: tuple[str, ...] = ()) -> Classification:
    normalized = normalize_description(description)
    if "MERCADO PAG" in normalized:
        return Classification(
            "Revisar",
            "income" if amount > 0 else "expense",
            False,
            0.45,
            "Mercado Pago é intermediador; confirmar a finalidade real",
        )
    if re.search(r"REPASSE|REEMBOLSO", normalized):
        return Classification(
            "Repasses a confirmar", "transfer", True, 0.65, "Confirmar se é repasse ou reembolso"
        )
    normalized_aliases = tuple(
        alias for raw_alias in internal_aliases if len(alias := normalize_description(raw_alias)) >= 3
    )
    if any(alias in normalized for alias in normalized_aliases) and re.search(r"PIX|TED|TRANSF", normalized):
        return Classification(
            "Transferência interna", "transfer", True, 0.8, "Confirmar transferência entre o casal"
        )
    for pattern, result in RULES:
        if pattern.search(normalized):
            if amount > 0 and result.transaction_type == "expense":
                return Classification("Reembolsos e estornos", "refund", False, 0.86)
            return result
    transaction_type = "income" if amount > 0 else "expense"
    return Classification("Revisar", transaction_type, False, 0.35, "Categoria não reconhecida")
