# Work Order — Batch Import

## Roadmap

Next unfinished Fase 2 item in `docs/ROADMAP.md`: **importação em lote**. Treat `docs/FINANCIAL_RULES.md`, `docs/FINANCIAL_INVARIANTS.md`, `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`, `docs/ARCHITECTURE.md` and the existing import/reconciliation/duplicate contracts as source of truth.

## Executor

Claude is the implementation executor. Claude must not merge, change undocumented financial semantics, auto-correct real data, weaken tests, delete/rewrite historical facts, or give Claude/Codex/Advisor authority over deterministic parsing, classification, reconciliation or duplicate decisions. Claude may challenge this Work Order with concrete documentation/code/test/security evidence. Unresolved disagreement blocks merge.

## Objective

Allow multiple financial files to be submitted in one user action while preserving exactly the same canonical semantics and safety controls as the current single-file import path. Batch import is orchestration over the canonical per-file pipeline, not a second financial engine.

## Scope and acceptance

- Reuse the existing canonical parser/import/classification/reconciliation/duplicate path for every file; no parallel policy in API or frontend.
- Return a truthful per-file result. Mixed batches must distinguish successful, duplicate/rejected, review/unknown and failed items according to existing contracts; never report whole-batch success when an item failed.
- One independent bad file must not silently erase or rewrite already-valid file results. Define transaction boundaries explicitly and justify them from existing architecture.
- Preserve exact-file hash behavior, non-destructive duplicate groups/source precedence, parser provenance, `DocumentReconciliation`, auditability and household ownership per file.
- Insufficient evidence remains `unknown`/review. Never fabricate totals, reconciliation, dates, categories or duplicate certainty to make a batch succeed.
- Preserve INV-001/002/003/004/014/015/016/017 and all other applicable invariants.
- Authentication, household isolation, upload limits, encrypted document storage, filename/content safety and privacy must match or tighten the existing single-file path. No raw financial content/PII in logs or errors.
- UI only presents backend canonical per-file outcomes; it does not parse, classify, reconcile, deduplicate or calculate financial totals.
- Existing single-file import compatibility is mandatory. Schema changes, if any, must be additive, justified and Alembic/PostgreSQL-safe with downgrade coverage; no destructive backfill.
- Do not implement Excel/PDF export or later roadmap items.

## Mandatory tests

Cover: two supported synthetic files through different parser paths; mixed valid + invalid file; exact reimport/hash duplicate; possible transaction duplicate remains non-destructive; explicit reconciliation `unknown`/review; household/auth isolation; safe errors without PII; single-file regression; PostgreSQL transaction behavior where relevant; migration upgrade/downgrade if schema changes; all 12 named CI gates.

## Delivery

Report orchestration/transaction model, canonical-service reuse, per-file API/UI contract, duplicate/reconciliation behavior, compatibility, migrations, security/privacy, tests/gates and residual failure cases. **Do not merge.**