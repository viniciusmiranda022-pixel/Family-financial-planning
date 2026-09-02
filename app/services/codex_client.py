import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import get_settings


def _json_default(value: object) -> float | str:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _encode_payload(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


@dataclass(frozen=True)
class CodexResult:
    payload: dict | None
    error: str | None = None


class CodexAdvisorClient:
    """Small client for the isolated Codex sidecar.

    The sidecar has no database connection or document volume.  It receives only
    the sanitized JSON prepared by the financial backend.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.advisor_enabled
            and self.settings.advisor_url
            and self.settings.advisor_shared_secret
        )

    def _request(
        self,
        path: str,
        payload: dict | None = None,
        timeout_seconds: int | None = None,
    ) -> CodexResult:
        if not self.configured:
            return CodexResult(None, "Codex não configurado")
        body = _encode_payload(payload) if payload else None
        request = Request(
            f"{self.settings.advisor_url.rstrip('/')}{path}",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Advisor-Token": self.settings.advisor_shared_secret,
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with urlopen(
                request,
                timeout=timeout_seconds or self.settings.advisor_timeout_seconds,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
            return CodexResult(result if isinstance(result, dict) else None)
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error")
            except Exception:
                detail = None
            return CodexResult(None, detail or f"Serviço Codex respondeu HTTP {exc.code}")
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            return CodexResult(None, f"Serviço Codex indisponível: {exc}")

    def status(self) -> CodexResult:
        return self._request("/health", timeout_seconds=3)

    def classify(self, payload: dict) -> CodexResult:
        return self._request("/v1/classify", payload)

    def analyze(self, payload: dict) -> CodexResult:
        return self._request("/v1/analyze", payload)

    def audit(self, payload: dict) -> CodexResult:
        """Call the dedicated `/v1/audit` semantic-audit contract.

        The Advisor sidecar always answers this route with HTTP 200, even
        when the underlying Codex call failed or timed out -- a safe
        `{"available": false, "reason": ...}` body is a normal, schema-shaped
        result, not an exception. A ``CodexResult(None, error)`` from this
        method therefore means the *transport* itself failed (network,
        auth, sidecar down); see `app.services.codex_audit` for how both
        cases collapse into one fail-safe outcome.
        """

        return self._request("/v1/audit", payload)
