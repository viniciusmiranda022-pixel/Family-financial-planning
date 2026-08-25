import json
from datetime import date
from decimal import Decimal

import pytest

from app.services.codex_client import _encode_payload


def test_codex_payload_serializes_nested_financial_types() -> None:
    encoded = _encode_payload(
        {
            "financial_summary": {
                "remaining_cap": Decimal("-7022.92"),
                "next_due_date": date(2026, 12, 10),
                "payment": {"total_cost": Decimal("20000.00")},
            }
        }
    )

    assert json.loads(encoded) == {
        "financial_summary": {
            "remaining_cap": -7022.92,
            "next_due_date": "2026-12-10",
            "payment": {"total_cost": 20000.0},
        }
    }


def test_codex_payload_rejects_unknown_objects() -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        _encode_payload({"unsupported": object()})
