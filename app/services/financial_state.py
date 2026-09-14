"""Canonical REALIZADO / COMPROMETIDO / PREVISTO labels.

October Go-Live Rebaseline §3 (`docs/OCTOBER_GO_LIVE_REBASELINE.md`), Slice 3
(`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_3.md`,
`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §3.3).

These three strings are the only vocabulary any surface (obligations,
card invoices, forecast rows, future reports) may use to label a financial
fact/commitment/hypothesis. `docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §3.3
deliberately recommends against a persisted column: the state is always
derivable from data that already carries its own authoritative meaning
(`Transaction` = REALIZADO, `Obligation.status`/`CardInvoice.status` =
COMPROMETIDO until liquidated, a projection row = PREVISTO) -- adding a
second, independently-editable classification would risk drifting from the
source of truth it is supposed to describe. This module exists only so every
call site spells the same three concepts identically; it holds no logic of
its own.
"""

from __future__ import annotations

REALIZADO = "REALIZADO"
COMPROMETIDO = "COMPROMETIDO"
PREVISTO = "PREVISTO"

FINANCIAL_STATES = (REALIZADO, COMPROMETIDO, PREVISTO)
