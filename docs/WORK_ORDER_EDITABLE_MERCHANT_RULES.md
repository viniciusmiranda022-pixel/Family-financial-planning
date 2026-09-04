# Work Order — Editable Merchant Rules

## Source of truth

This is the first still-unfinished item in **Fase 2 — Aderência e produtividade** from `docs/ROADMAP.md`: **regras editáveis de estabelecimento**.

Before implementation, reconcile this Work Order with:

- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/ARCHITECTURE.md`;
- the current classifier/import/review/audit pipeline.

The normative rule already states: local classification rules require **three confirmed and distinct corrections** before reaching `pending_acceptance`; **only an administrator may activate them**; activation applies to **future classifications only** and must never silently recategorize historical transactions.

## Executor and authority

Claude is the implementation executor. Claude must not merge this PR, change undocumented financial semantics, auto-correct real financial data, lower test coverage, or give Claude/Codex/Advisor authority over deterministic financial facts.

Claude may and should challenge this Work Order or the engineer's interpretation when concrete code, tests, normative documentation, security evidence, or regression risk shows a better or necessary design. Unresolved disagreement blocks merge.

## Objective

Deliver one end-to-end, household-scoped path for editable merchant/establishment classification rules while preserving deterministic classification, human approval, auditability, and historical immutability.

## Required scope

- Represent local merchant/classification rules explicitly and household-scoped.
- Preserve a deterministic lifecycle that cannot activate a learned rule before the normative evidence threshold and administrator acceptance.
- Allow an administrator to inspect and deliberately activate/deactivate or edit an eligible rule through the canonical application surface.
- Apply active rules only in the canonical classification path for **new/future classification events**.
- Record material rule lifecycle changes in the existing audit trail with actor, before/after and reason where applicable.
- Keep classifier behavior deterministic and local; Codex/Advisor may explain ambiguity but cannot create, activate, edit or override a canonical rule.

## Acceptance criteria

1. A candidate rule cannot become `pending_acceptance` without three distinct confirmed corrections as required by `FINANCIAL_RULES.md`.
2. A non-admin cannot activate, deactivate or edit a canonical local rule.
3. Activating/editing a rule never silently mutates or recategorizes existing `Transaction` rows.
4. Future classification uses the active household rule through one canonical classification path; do not create a parallel classifier in API/UI/importer/smart-capture.
5. Household isolation is enforced: one household's rules cannot affect another household.
6. Rule precedence versus built-in deterministic financial rules must be explicit and must not permit merchant rules to override invariants such as transfer/reconciliation/patrimonial/refund semantics.
7. Lifecycle changes are auditable and reversible without deleting historical evidence.
8. Any schema change is additive, Alembic-safe from both empty DB and the supported legacy baseline, and downgrade behavior is explicitly tested.
9. Existing financial invariants, snapshot parity, reconciliation, Advisor security and PostgreSQL integration gates remain green.
10. Documentation describes the rule lifecycle, precedence and non-retroactivity precisely.

## Mandatory tests

- evidence threshold: 0/1/2 corrections cannot yield `pending_acceptance`; three distinct confirmed corrections can;
- duplicate/replayed confirmation cannot inflate the evidence count;
- admin-only activation/edit/deactivation authorization;
- household isolation;
- active rule affects a future classification event deterministically;
- historical transactions remain byte-for-byte financially unchanged after rule activation/edit;
- built-in financial semantic rules (at minimum INV-001, INV-002, INV-003/004 and INV-016 where applicable) cannot be overridden by a merchant rule;
- audit before/after/reason/actor coverage for rule lifecycle changes;
- migration upgrade/downgrade and PostgreSQL integration when schema changes;
- all 12 named CI gates from the normative roadmap.

## Risks to address explicitly

- rule precedence accidentally overriding financial semantics;
- regex/pattern overreach causing unrelated merchants to be recategorized;
- cross-household leakage;
- duplicate correction evidence gaming the three-confirmation threshold;
- retroactive historical mutation;
- UI/API implementing separate rule logic;
- migrations that rewrite existing financial data;
- Codex/Advisor being allowed to activate or author deterministic rules.

## Prohibitions

- no silent historical recategorization or bulk auto-fix;
- no deletion of evidence to make a rule appear clean;
- no financial-rules version change merely to make tests pass;
- no weakening existing invariants, reconciliation, duplicate handling or CI coverage;
- no second classification policy in importer, API, UI or Advisor;
- no real financial documents, PII or production data in Git fixtures;
- no merge by Claude.

## Delivery report expected from Claude

On completion, report in the PR: architecture and precedence decision, migration impact, lifecycle/evidence model, authorization and household isolation, exact diff scope, tests/gates, security/privacy implications, compatibility with existing data, residual risks, and any technical objection to this Work Order with concrete evidence.
