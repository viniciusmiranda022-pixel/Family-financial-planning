# Work Order — Installment regressions after PR #115

## Objective
Correct three findings from the post-merge review of PR #115 without changing unrelated behavior.

## Scope
1. Interest-period parsing must not treat annual/daily/ambiguous rates as monthly. Fail closed with clarification unless a deterministic documented conversion is intentionally implemented.
2. Explicit natural-language zero rates such as "0% ao mês", "taxa de 0%" and "sem juros" must resolve as an explicit zero.
3. Preserve the user-stated contracted total when equal installments require cent rounding. Represent any remainder deterministically instead of replacing the original total with rounded installment multiplied by count.
4. Preview, persistence, future-installment projection, audit and undo must remain consistent.

## Acceptance
- "12% ao ano" is never applied as 12% monthly.
- Explicit zero variants are accepted.
- 100 in 3 interest-free installments preserves contracted total 100.00.
- Existing simple WRITE behavior remains compatible.
- Existing data is not reinterpreted silently.

## Invariants
The stated contracted total is a fact. Assumptions do not become facts. No duplicate counting. No LLM SQL. Canonical backend remains the calculation and persistence authority.

## Tests
Unit tests for rate period and zero variants; rounding/property tests; orchestration preview; persistence/projection/audit/undo integration; PostgreSQL regression; lint and full CI.

## Risks
Wrong interest period, rounding drift, projection mismatch, duplicate commitments, incompatible historical rows.

## Prohibitions
No second finance engine. No destructive migration. No Oracle/OCI work. Claude does not merge.