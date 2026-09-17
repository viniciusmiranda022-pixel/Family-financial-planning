# Work Order — #85 Privilège DI / CVM daily valuation

## Objective
Implement deterministic, auditable daily valuation of Itaú Privilège RF Referenciado DI (CNPJ 26.199.519/0001-34) using the latest official structured CVM quota while preserving confirmed Itaú balance observations as sovereign reconciliation facts.

## Scope
- Investigate and document the stable official structured CVM ingestion method before implementing ingestion; do not screen-scrape when structured official data exists.
- Persist enough evidence to reproduce each valuation: fund CNPJ, quota reference date/value, units held, calculated gross value, provider/source, retrieved_at and valuation/version identifier.
- Keep official market valuation distinct from observed Itaú balance and expose reconciliation difference/freshness/error state.
- Establish units_held only from reliable same-reference evidence: actual statement quantity preferred; otherwise confirmed balance + matching official quota. Never combine different reference dates silently.
- Applications/redemptions alter units; quota movement alters valuation. Appreciation is not income/cash-flow and no synthetic application/redemption/transaction may be created.
- Daily local/Docker-safe job checks for newer official quota; weekends/holidays/no-new-quota preserve the last valid observation without artificial yield.
- UI must show estimated balance, latest official quota, quota reference date, daily variation when available, estimated gain/loss, observed balance when available, reconciliation difference, source and freshness.

## Acceptance criteria / invariants
- Confirmed AccountBalanceObservation/bank observation remains sovereign at its observed instant and is never silently overwritten by an estimate.
- CVM failure/malformed response cannot zero/corrupt a balance; retain last valid state and surface stale/error.
- Same fund/reference date rerun is idempotent and does not duplicate financial facts.
- All monetary/unit/quota arithmetic uses Decimal, never binary float.
- Quota rise deterministically raises valuation; unchanged quota creates no artificial return; no-new-quota keeps correct reference date.
- Application/redemption is never confused with investment return.
- No LLM/Codex/Claude authority over valuation numbers.
- Household isolation, auditability, backward compatibility and safe additive Alembic migration where needed.

## Mandatory tests
Cover quota rise, unchanged quota, weekend/holiday/no-new-quota, CVM unavailable/malformed, idempotent rerun, application/redemption separation, observed-vs-calculated separation, Decimal precision, household isolation, migration upgrade/downgrade and regression of financial invariants. Use PostgreSQL integration for persistence/concurrency semantics where applicable.

## Risks / prohibitions
- No synthetic cash movement to reconcile differences.
- No silent overwrite of confirmed balance.
- No hard-coded R$ 40.667,49 principal; it is only a historical observation from 2026-09-11.
- No screen scraping if official structured CVM data is available.
- No Oracle/OCI work (#59–65), WhatsApp anticipation, paid provider, second financial engine or unrelated refactor.
- Do not merge. Claude implements; engineer reviews and merges only after exact-head CI/gates approval.

## Source
Issue #85 is the detailed product/financial source of truth. October rebaseline and FINANCIAL_RULES/FINANCIAL_INVARIANTS prevail for financial semantics.