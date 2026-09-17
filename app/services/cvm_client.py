"""CVM (Comissão de Valores Mobiliários) official structured daily fund
quota ingestion (issue #85, `docs/WORK_ORDER_PRIVILEGE_DI_CVM.md`).

See `docs/PRIVILEGE_DI_CVM_INGESTION_INVESTIGATION.md` for the investigation
this client implements: CVM's Open Data Portal `fi-doc-inf_diario` dataset,
bulk monthly file `inf_diario_fi_{YYYYMM}.csv` (distributed inside a ZIP
since May/2022), fetched over plain HTTP(S) with stdlib `urllib` only -- no
new HTTP dependency, mirroring `app.services.codex_client.CodexAdvisorClient`
and `app.services.email_delivery.SmtpEmailAdapter`'s established
never-raise, sanitized-error-dataclass shape (`CvmResult`, not an exception,
is this module's only way of reporting failure).

Never screen-scrapes an HTML page: this is CVM's own structured,
machine-readable government dataset -- the Work Order's explicit
requirement ("No screen scraping if official structured CVM data is
available"). Never uses `float` for the quota or net worth: every numeric
field is parsed straight from the CSV string into `Decimal`.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from app.config import get_settings

# 10 decimal places -- matches `fund_reference_quotes.quota_value`
# (`Numeric(20, 10)`, see `app/models.py`). A CVM quota routinely carries
# 6-8 significant decimal digits; quantizing here (never rounding to cents)
# only normalizes the `Decimal`'s representation, it never loses precision
# the column itself preserves.
QUOTA_QUANTUM = Decimal("0.0000000001")
NET_WORTH_QUANTUM = Decimal("0.01")

# CVM's fund-classes reform (Resolução CVM 175) has, for at least one
# adjacent dataset, already renamed the fund-identifying CNPJ column from
# `CNPJ_FUNDO` to `CNPJ_FUNDO_CLASSE` (see the ingestion investigation doc).
# This parser is column-*name*-aware, never column-*position*-aware, and
# tries both, in this order, so it keeps working whichever schema variant is
# live for the bulk quota-value file when the worker actually runs. A file
# exposing neither is a malformed-response error, never a guess.
_CNPJ_COLUMN_CANDIDATES = ("CNPJ_FUNDO_CLASSE", "CNPJ_FUNDO")
_DATE_COLUMN = "DT_COMPTC"
_QUOTA_COLUMN = "VL_QUOTA"
_NET_WORTH_COLUMN = "VL_PATRIM_LIQ"

ERROR_NOT_CONFIGURED = "cvm_not_configured"
ERROR_TRANSPORT = "cvm_transport_error"
ERROR_MALFORMED = "cvm_malformed_response"
ERROR_NO_MATCHING_ROW = "cvm_no_matching_row"


def normalize_fund_cnpj(value: str) -> str:
    """Digits-only CNPJ, e.g. `"26.199.519/0001-34"` -> `"26199519000134"`.
    Both the CSV's `CNPJ_FUNDO(_CLASSE)` column and this codebase's
    configured fund identifier are normalized the same way before
    comparison, so formatting differences (dots/slash/dash present or not)
    never cause a false "no matching row"."""

    return "".join(ch for ch in value if ch.isdigit())


@dataclass(frozen=True)
class CvmQuote:
    fund_cnpj: str
    quota_reference_date: date
    quota_value: Decimal
    net_worth: Decimal | None
    source_url: str
    ingestion_batch: str


@dataclass(frozen=True)
class CvmResult:
    quote: CvmQuote | None
    error: str | None = None
    error_code: str | None = None


def _parse_decimal(raw: str) -> Decimal:
    # CVM's CSV uses a plain dot decimal separator in the machine-readable
    # bulk file (distinct from the comma-decimal formatting CVM sometimes
    # uses in human-facing pages) -- never routed through `float`.
    return Decimal(raw.strip())


def _parse_quota_row(row: dict[str, str], *, fund_cnpj_digits: str) -> CvmQuote | None:
    cnpj_column = next((col for col in _CNPJ_COLUMN_CANDIDATES if col in row), None)
    if cnpj_column is None:
        return None
    if normalize_fund_cnpj(row[cnpj_column]) != fund_cnpj_digits:
        return None
    if row.get(_DATE_COLUMN) is None or row.get(_QUOTA_COLUMN) is None:
        return None
    reference_date = datetime.strptime(row[_DATE_COLUMN].strip(), "%Y-%m-%d").date()
    quota_value = _parse_decimal(row[_QUOTA_COLUMN]).quantize(QUOTA_QUANTUM)
    net_worth_raw = row.get(_NET_WORTH_COLUMN)
    net_worth = _parse_decimal(net_worth_raw).quantize(NET_WORTH_QUANTUM) if net_worth_raw else None
    return CvmQuote(
        fund_cnpj=fund_cnpj_digits,
        quota_reference_date=reference_date,
        quota_value=quota_value,
        net_worth=net_worth,
        source_url="",  # filled in by the caller, which knows the batch's URL
        ingestion_batch="",
    )


def _extract_csv_text(raw_bytes: bytes) -> str:
    if raw_bytes[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(csv_names) != 1:
                raise ValueError(f"esperava exatamente um CSV no ZIP, encontrou {len(csv_names)}")
            return archive.read(csv_names[0]).decode("latin-1")
    return raw_bytes.decode("latin-1")


def _latest_quote_for_cnpj(csv_text: str, *, fund_cnpj_digits: str) -> CvmQuote | None:
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
    latest: CvmQuote | None = None
    for row in reader:
        parsed = _parse_quota_row(row, fund_cnpj_digits=fund_cnpj_digits)
        if parsed is None:
            continue
        if latest is None or parsed.quota_reference_date > latest.quota_reference_date:
            latest = parsed
    return latest


class CvmClient:
    """Fetches the latest official quota for one fund CNPJ from CVM's bulk
    monthly Informe Diário file. Stateless, DB-free --
    `app.services.privilege_valuation` owns persistence/idempotency; this
    class only ever performs the HTTP fetch + parse and returns a
    `CvmResult`, never raising."""

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.cvm_valuation_enabled and self.settings.cvm_quota_base_url)

    def _batch_url(self, year_month: str) -> str:
        return f"{self.settings.cvm_quota_base_url.rstrip('/')}/inf_diario_fi_{year_month}.csv"

    def _fetch_batch(self, year_month: str) -> bytes:
        url = self._batch_url(year_month)
        with urlopen(url, timeout=self.settings.cvm_quota_timeout_seconds) as response:
            return response.read()

    def latest_quote(self, fund_cnpj: str, *, reference_month: date | None = None) -> CvmResult:
        """Returns the most recent official quota for `fund_cnpj` found in
        the reference month's bulk file, falling back to the prior month
        when the reference month's file has no matching row yet (normal in
        the first few days of a new month, before any trading day has been
        reported -- Work Order: "Weekends/holidays with no new quota are
        normal and must not generate artificial yield"). Never raises."""

        if not self.configured:
            return CvmResult(None, "Ingestão CVM desabilitada (cvm_valuation_enabled=false)", ERROR_NOT_CONFIGURED)

        fund_cnpj_digits = normalize_fund_cnpj(fund_cnpj)
        target_month = reference_month or date.today()
        candidates = [target_month, _first_of_previous_month(target_month)]

        last_error: str | None = None
        last_error_code: str | None = None
        for month_start in candidates:
            year_month = month_start.strftime("%Y%m")
            url = self._batch_url(year_month)
            try:
                raw_bytes = self._fetch_batch(year_month)
            except HTTPError as exc:
                last_error = f"CVM respondeu HTTP {exc.code} para {url}"
                last_error_code = ERROR_TRANSPORT
                continue
            except (URLError, TimeoutError, OSError) as exc:
                last_error = f"Fonte CVM indisponível ({url}): {exc}"
                last_error_code = ERROR_TRANSPORT
                continue

            try:
                csv_text = _extract_csv_text(raw_bytes)
                quote = _latest_quote_for_cnpj(csv_text, fund_cnpj_digits=fund_cnpj_digits)
            except (zipfile.BadZipFile, ValueError, UnicodeDecodeError, csv.Error, InvalidOperation) as exc:
                # Covers a non-ZIP/non-CSV payload, an unparseable
                # DT_COMPTC/VL_QUOTA value, and a malformed CSV structure
                # alike -- all the same outcome (Work Order: "CVM
                # unavailable/malformed -> retain last valid state").
                last_error = f"Resposta CVM malformada ({url}): {exc}"
                last_error_code = ERROR_MALFORMED
                continue

            if quote is not None:
                return CvmResult(
                    CvmQuote(
                        fund_cnpj=quote.fund_cnpj,
                        quota_reference_date=quote.quota_reference_date,
                        quota_value=quote.quota_value,
                        net_worth=quote.net_worth,
                        source_url=url,
                        ingestion_batch=year_month,
                    )
                )
            last_error = f"Nenhuma linha para o CNPJ informado em {url}"
            last_error_code = ERROR_NO_MATCHING_ROW

        return CvmResult(None, last_error, last_error_code)


def _first_of_previous_month(reference: date) -> date:
    if reference.month == 1:
        return date(reference.year - 1, 12, 1)
    return date(reference.year, reference.month - 1, 1)
