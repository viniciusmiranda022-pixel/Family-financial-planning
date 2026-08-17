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


RULES: tuple[tuple[re.Pattern[str], Classification], ...] = (
    (
        re.compile(
            r"PAGAMENTO.*FATURA|PAGAMENTO RECEBIDO|FATURA PAGA|PAG(?:AMENTO)? BOLETO.*(?:NU PAGAMENTOS|NUBANK)"
        ),
        Classification("Conciliação", "reconciliation", True, 0.99),
    ),
    (
        re.compile(r"PRIVILEGE|PRIVILEGE DI|APLICACAO|RESGATE"),
        Classification("Transferência patrimonial", "transfer", True, 0.97),
    ),
    (
        re.compile(r"ESTORNO|CREDITO.*COMPRA|CREDITO.*CARTAO"),
        Classification("Reembolsos e estornos", "refund", False, 0.98),
    ),
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
    (re.compile(r"IOF|JUROS|TARIFA"), Classification("Juros, IOF e tarifas", "expense", False, 0.95)),
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
