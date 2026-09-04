# Work Order — Visual Card Payment Reconciliation

## Source of truth

This is the next unfinished item in **Fase 2 — Aderência e produtividade** from `docs/ROADMAP.md`: **conciliação visual de fatura x débito bancário**.

Before implementation, reconcile this Work Order with `docs/FINANCIAL_RULES.md`, `docs/FINANCIAL_INVARIANTS.md`, `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`, `docs/ARCHITECTURE.md`, the existing `DocumentReconciliation`/duplicate/snapshot/monthly-close contracts, and the current card/bank import pipeline. Normative facts remain authoritative: card purchases belong to the invoice competence; paying the invoice is reconciliation and must not be counted as a second expense; absence of sufficient evidence is `unknown`/review, never an invented match.

## Executor and authority

Claude is the implementation executor. Claude must not merge this PR, change undocumented financial semantics, auto-correct real financial data, weaken tests, or give Claude/Codex/Advisor authority over deterministic reconciliation facts. Claude may and should challenge this Work Order or engineer guidance with concrete documentation/code/test/security evidence. Unresolved disagreement blocks merge.

## Objective

Provide one auditable visual workflow that lets the household inspect the deterministic relationship between a credit-card invoice/payment reconciliation and the corresponding bank debit, without changing purchase totals, fabricating matches, or silently mutating historical transactions.

## Required scope

- Surface card invoice/payment reconciliation evidence using existing canonical transaction/document/reconciliation data wherever possible; do not create a parallel financial calculation.
- Show candidate/linked bank debit versus card-payment reconciliation with amount, date/competence, account/card identity and reconciliation status sufficient for human review.
- Preserve INV-002: card payment is reconciliation, not a second expense.
- Use deterministic matching only when the repository already has sufficient evidence; ambiguous or insufficient cases must remain explicit `unknown`/review-required and require human action rather than auto-fix.
- Any human link/unlink/confirm action must be household-scoped, auditable, and non-destructive; historical source rows remain intact.
- UI must consume backend canonical facts and must not recompute financial totals or matching rules independently.

## Acceptance criteria

1. A reconciled card-payment/bank-debit pair is visible from the application with both source records and the evidence/status used.
2. The same payment is never counted as a new expense because of this feature; INV-002 remains true before and after any visual reconciliation action.
3. Ambiguous/no-evidence cases cannot silently become reconciled; they remain reviewable/unknown.
4. Household isolation and authorization are enforced for reads and any state-changing reconciliation action.
5. Human reconciliation changes preserve source transactions/documents and leave an audit trail with actor, before/after and reason where applicable.
6. Existing document reconciliation, duplicate precedence, snapshots, reports, projection gates and Monthly Close consume the same canonical facts; no second matching/reconciliation policy appears in frontend/API helpers.
7. Any schema change is additive, Alembic-safe from empty DB and supported legacy baseline, with downgrade tested and no destructive backfill.
8. All 12 named CI gates remain green; add focused HTTP/service/UI regression coverage for this slice.

## Mandatory tests

- payment-of-invoice remains `reconciliation`/excluded and does not double-count expense;
- exact deterministic pair is surfaced correctly;
- ambiguous multiple bank debits do not auto-resolve;
- missing counterpart remains explicit unknown/review state;
- household isolation and authorization;
- human confirm/link/unlink, if introduced, is audited and never mutates/deletes source financial rows;
- canonical snapshot/report/projection parity remains unchanged except for reconciliation metadata expected by the feature;
- migration upgrade/downgrade + PostgreSQL integration if schema changes;
- all 12 CI jobs.

## Risks and prohibitions

Do not infer a bank debit from amount alone when evidence is ambiguous. Do not edit/delete historical `Transaction` or `Document` rows to make reconciliation close. Do not convert reconciliation into expense/income. Do not introduce frontend-only matching, hidden auto-fix, duplicate financial calculations, real PII/documents in fixtures, or Codex/Advisor authority over match status. Do not skip ahead to batch import, exports or later roadmap items.

## Delivery report expected from Claude

On completion, report architecture/source-of-truth decision, matching/evidence contract, UI/API changes, migrations, audit/authorization, compatibility with existing data, tests/12 gates, security/privacy, residual ambiguity cases, and any technical objection with concrete evidence. Do not merge.