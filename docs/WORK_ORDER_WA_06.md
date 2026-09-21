# Work Order — WA-06 Advisor, comparisons and scenarios (#78)

## Objective
Expose the canonical Family Finance advisor/projection capabilities through the conversational Tool Layer so open diagnostic questions and hypothetical household-budget scenarios can be investigated without giving the LLM financial authority.

## Scope
- Reuse canonical projection, obligations, cash-flow, budget, card and WA-04/05 query primitives; extend only with typed deterministic backend tools that are genuinely missing for #78.
- Support safe hypothetical purchase/cash-impact scenarios (`posso gastar R$ X?`) without persisting any fact unless a separate explicit WRITE flow is later requested and confirmed.
- Support open diagnostics by composing bounded typed read-only tools across income, operating expenses, commitments, cards, trends and concentration.
- Keep REALIZADO, COMPROMETIDO, PREVISTO, HIPÓTESE and RECOMENDAÇÃO explicit in tool facts and user-facing explanation; never silently mix them.
- Preserve WA-05 conversational context semantics and generic follow-ups; no static question catalog.
- Keep investment output limited to household budgeting/planning; no regulated investment recommendation.

## Acceptance criteria
- `posso gastar R$ X?` uses deterministic backend scenario calculations, exposes assumptions/horizon/baseline and performs zero writes.
- An open diagnostic such as `por que meu dinheiro está acabando mais rápido?` can be answered through bounded multi-tool composition, with every numeric conclusion traceable to backend tool facts.
- Period comparisons, trends and spending concentration use canonical calculations and preserve existing exclusions: invoice payment is not expense; internal transfer is not income/expense; investment contribution/redemption is not operating expense.
- Hypotheses never become facts, confirmed balances remain sovereign, and divergences remain visible rather than synthesized away.
- No free SQL, arbitrary ORM expressions, LLM arithmetic or second financial engine.
- Existing WA-00..05 behavior remains backward compatible.

## Mandatory tests
- Exact parity between conversational tools and canonical Finance Engine for income, expenses, commitments, cards, budget/projection and period comparisons.
- Purchase/cash-impact scenarios at zero/positive amount, month boundaries, leap February, insufficient cash and existing commitments; verify zero DB mutation/audit facts except normal assistant read audit.
- Open diagnostic composition with deterministic bounded step count and no invented arithmetic.
- REALIZADO/COMPROMETIDO/PREVISTO/HIPÓTESE separation and labels.
- Regression: card purchase + invoice payment cannot double count; transfers, reconciliation, investment application/redemption cannot inflate operating expense/income.
- Household/user isolation, WA-05 context interaction, ambiguity fail-safe, prompt/tool-schema injection fail-closed.
- PostgreSQL integration where persistence/concurrency is touched, lint, migrations if any, and all applicable GitHub Actions.

## Risks / review gates
Review financial semantics, scenario assumptions, horizon/timezone, Decimal precision/rounding, balance sovereignty, double counting, cards/installments, partial payments/interest/IOF, authorization, privacy, bounded tool composition, prompt injection, auditability, performance and migration/backfill compatibility.

## Prohibitions
- No persistence of hypothetical scenarios as financial facts.
- No LLM-generated SQL, financial arithmetic, hidden assumptions or second finance engine.
- No automatic commission forecast for Vinicius; Kelly recurring salary only through the documented recurring mechanism.
- No investment recommendation or product selection.
- No Oracle/OCI (#59–65), WA-07, or unrelated backlog.
- Claude must not merge.

## Execution
Claude implements on this branch/PR, documents any new typed tool and scenario formula with evidence, migrations/rollback if applicable, tests and Technical Challenges. Integration authority reviews and merges only on a stable head with green applicable CI and all criteria satisfied.