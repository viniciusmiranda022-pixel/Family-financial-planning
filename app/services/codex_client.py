import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import get_settings


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
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload else None
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
