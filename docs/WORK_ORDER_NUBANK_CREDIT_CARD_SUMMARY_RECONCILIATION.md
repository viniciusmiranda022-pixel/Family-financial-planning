# Work Order â€” Nubank credit-card current-summary reconciliation

## Context

Operational validation on 2026-09-09 proved that the Nubank credit-card
declared-field parser scans the entire extracted PDF for `FATURA ANTERIOR`
and `TOTAL A PAGAR` and selects the last occurrence of each label.

Real textual invoices can repeat those labels in payment, financing or
simulation blocks outside the canonical `RESUMO DA FATURA ATUAL`. The
result is a structurally valid but financially wrong `opening_balance` or
`declared_total`, which makes the canonical reconciliation report
`not_reconciled` even when the transaction components are otherwise
consistent.

This is a parser compatibility bugfix. It does not change financial rules.

## Normative sources

- `docs/FINANCIAL_RULES.md`
- `docs/FINANCIAL_INVARIANTS.md`
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`
- `docs/WORK_ORDER_PDF_PARSERS_NUBANK_MERCADO_PAGO.md`

## Required behavior

1. Declared reconciliation facts for a Nubank credit-card invoice must be
   scoped to the explicit `RESUMO DA FATURA ATUAL` block.
2. The first `TOTAL A PAGAR` after that marker closes the canonical current
   summary for declared-field extraction.
3. `FATURA ANTERIOR` is accepted only when it occurs after the current
   summary marker and before that canonical total.
4. Repeated `FATURA ANTERIOR` / `TOTAL A PAGAR` labels outside that block
   must not replace the current invoice facts.
5. If the current-summary marker or its canonical total is absent, the
   relevant reconciliation fields remain unknown. Do not guess.
6. Reuse `ParsedDocument` and `reconcile_parsed_document`; no second
   financial formula or reconciliation path.
7. Do not change transaction signs, competence, payment/refund semantics,
   duplicate governance or real financial data.
8. No real PDF, PII or real financial amount may be committed in fixtures.

## Acceptance

- Synthetic invoice with misleading repeated labels before/after the current
  summary extracts only the canonical current-summary values.
- The synthetic invoice reconciles through the existing canonical formula.
- Missing current-summary evidence fails closed as `unknown`.
- Existing parser/invariant tests remain green.
- Parser contract version is bumped because declared-field selection
  semantics changed.