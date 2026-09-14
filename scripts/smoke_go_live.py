#!/usr/bin/env python3
"""Produce the Work Order's go-live smoke results document.

`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_8.md` §5 requires the smoke to
record, for each step: entrada, ação, resultado esperado, resultado
observado, evidência. `tests/test_go_live_smoke_slice8.py::run_go_live_smoke`
is the single source of truth for that flow (also run on every `pytest`
invocation, so it never drifts from what this script reports) -- this
script only drives it once more and renders its result as Markdown.

Usage:
    python -m scripts.smoke_go_live [--output docs/SMOKE_OCTOBER_GO_LIVE_RESULTS.md]

Synthetic data only -- see the module docstring of
`tests/test_go_live_smoke_slice8.py` for why: this session has no access to
real household data. A real-data manual smoke remains a separate step for
the engenheiro responsável before go-live.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

if "DATABASE_URL" not in os.environ:
    from cryptography.fernet import Fernet

    os.environ["DATABASE_URL"] = f"sqlite:////tmp/ffp-smoke-go-live-{uuid.uuid4().hex}.sqlite"
    os.environ.setdefault("SECRET_KEY", "smoke-go-live-script-secret-long-enough-value")
    os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_go_live_smoke_slice8 import _client, run_go_live_smoke  # noqa: E402


def _render_markdown(steps, *, generated_at: str) -> str:
    lines = [
        "# Smoke Real de Go-Live -- October Go-Live Slice 8",
        "",
        f"Gerado em: {generated_at}",
        "",
        (
            "Dados sintéticos (rebaseline/Work Order permitem "
            '"datasets sintéticos equivalentes" para este E2E automatizado). '
            "O smoke manual com dados reais da família é um passo humano "
            "separado, fora do alcance desta sessão."
        ),
        "",
        f"Resultado: {'TODOS OS PASSOS PASSARAM' if all(step.ok for step in steps) else 'FALHA -- ver detalhe abaixo'}",
        "",
    ]
    for step in sorted(steps, key=lambda item: item.number):
        status = "✅ OK" if step.ok else "❌ FALHOU"
        lines.extend(
            [
                f"## Passo {step.number} -- {step.title} [{status}]",
                "",
                f"- **Entrada:** {step.input}",
                f"- **Ação:** {step.action}",
                f"- **Resultado esperado:** {step.expected}",
                f"- **Resultado observado:** {step.observed}",
            ]
        )
        if step.evidence:
            lines.append(f"- **Evidência:** {step.evidence}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="docs/SMOKE_OCTOBER_GO_LIVE_RESULTS.md",
        help="Caminho do arquivo Markdown de saída.",
    )
    args = parser.parse_args(argv)

    client, session_factory = _client()
    with client:
        steps = run_go_live_smoke(client, session_factory)

    generated_at = datetime.now(UTC).isoformat()
    markdown = _render_markdown(steps, generated_at=generated_at)
    output_path = Path(args.output)
    output_path.write_text(markdown, encoding="utf-8")
    print(f"Smoke concluído: {sum(1 for step in steps if step.ok)}/{len(steps)} passos OK.")
    print(f"Relatório salvo em {output_path}")
    return 0 if all(step.ok for step in steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
