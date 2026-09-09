# Work Order — Nubank bank statement: real pdfplumber extraction layout

## Context

Production-readiness initial-load validation on 2026-09-09 exposed a compatibility defect in the Nubank bank-statement parser introduced by PR #41.

The current parser assumes each transaction description is followed by a **bare amount line**. The real Nubank PDFs processed by the same `pdfplumber` version used by the application instead extract transaction amounts **at the end of a description line**, while additional counterparty/bank metadata may continue on subsequent lines.

Synthetic shape (no real PII or financial values):

```text
14 AGO 2026 Total de entradas + 300,00
Transferência recebida pelo Pix Pessoa Exemplo - 300,00
BANCO EXEMPLO S.A. Agência: 1 Conta: 0000
Total de saídas - 300,00
Transferência enviada pelo Pix Pessoa Exemplo - BANCO DESTINO 300,00
Agência: 2 Conta: 1111
```

The current `_parse_nubank_bank_statement_pdf()` waits for a later line containing only `300,00`, so it produces zero transactions and initial-load reports `review_required / parse_failed` for statements that have real movements.

This is an importer compatibility bugfix. It is **not** a new financial feature or roadmap slice.

## Normative sources

Treat these as source of truth:

- `docs/FINANCIAL_RULES.md`
- `docs/FINANCIAL_INVARIANTS.md`
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`
- `docs/RUNBOOK_INITIAL_LOAD.md`
- `docs/WORK_ORDER_PDF_PARSERS_NUBANK_MERCADO_PAGO.md`

The repository currently has no root `ROADMAP.md`; do not invent one for this bugfix.

## Objective

Make the canonical Nubank bank-statement parser accept the real `pdfplumber` text shape where each transaction amount is at the end of a transaction line and descriptive metadata may continue after that line, while preserving the already-supported bare-amount-line shape and all existing financial semantics.

## Scope

Allowed changes:

- `app/services/importer.py` only as required for Nubank bank-statement parsing;
- synthetic/anonymous fixtures under `tests/fixtures/`;
- parser tests under `tests/test_pdf_parsers.py`;
- this Work Order if a technically justified clarification is needed.

No database migration is expected.

## Required behavior

1. Continue using content-based issuer detection. Do not use filename, account number, CPF, holder name, document hash or other PII to select a parser.
2. The date and debit/credit direction come only from Nubank's day/section headers (`Total de entradas` / `Total de saídas`). Never infer direction from transaction description.
3. Per-day and period aggregate lines are never transactions.
4. Support both observed layouts:
   - amount on a bare line after a multiline description (already supported);
   - amount at the end of the transaction line, followed optionally by metadata continuation lines.
5. Preserve useful multiline description/metadata deterministically without attaching footer text or the next transaction to the previous transaction.
6. Stop/ignore issuer footer/disclaimer text; footer values or contact data must never become transactions.
7. Multiple transactions in the same section must remain separate even when each amount appears inline.
8. Section switches and new day headers must flush the pending transaction correctly.
9. Zero-movement statements remain zero-movement: do not fabricate a transaction merely to make parsing or reconciliation pass.
10. Reconciliation must continue using the canonical `ParsedDocument` + `reconcile_parsed_document` path. No second reconciliation policy.
11. Do not change transfer/card-payment/refund/competence semantics. Preserve INV-001, INV-002, INV-014, INV-015, INV-016, INV-017 and all other applicable invariants.
12. Do not modify real financial data, auto-resolve duplicate groups, or make any destructive correction.

## Acceptance tests

At minimum add synthetic tests proving:

- inline amount + following metadata continuation parses one entry with the correct positive sign;
- inline amount + following metadata continuation parses one exit with the correct negative sign;
- two or more transactions in one section are not concatenated;
- a section switch flushes the prior transaction and changes sign only for following transactions;
- a new date header flushes the prior transaction and updates `booked_at`;
- aggregate `Total de entradas/saídas` lines never appear in parsed transactions;
- footer/disclaimer lines are not appended to a transaction and never produce a transaction;
- the legacy bare-amount-line fixture remains supported;
- a zero-movement Nubank statement remains rejected with the conservative `sem lançamentos` behavior;
- declared opening/closing balances and `as_of_date` remain correct;
- a reconciliable synthetic statement returns `reconciled` with difference `0.00`;
- existing Itaú and Mercado Pago parser regression tests remain green.

Run all applicable gates, including at least:

```bash
ruff check .
pytest -q tests/test_pdf_parsers.py
pytest -q
```

## Initial-load acceptance evidence

After merge and local rebuild, the operator will repeat the same `--dry-run` against the unchanged real files.

The following Nubank account-statement periods are known to contain movements and therefore must no longer fail because of this layout mismatch: January, February, March, May, June, July and August 2026.

April 2026 and the 01–02 September 2026 statement are known zero-movement statements and may remain intentionally excluded from the transaction-import manifest. Do not modify parser semantics to manufacture rows for them.

## Prohibitions

- No real PDFs, extracted real text, names, account numbers, CPF/CNPJ, transaction descriptions or financial amounts in Git fixtures/comments.
- No filename-based parser routing.
- No rule/version change merely to make tests pass.
- No migration unless concrete schema necessity is demonstrated and reviewed first.
- No silent data correction.
- No weakening/removing tests or coverage.
- No duplicate financial calculation/reconciliation path.
- Claude/Codex has no authority over deterministic financial facts.

## Executor / review model

Claude is the implementation executor and must not merge. The responsible engineer reviews diff, architecture, financial semantics, privacy, tests and CI before any merge.

Claude may and should contest this Work Order or reviewer guidance with concrete evidence. Any unresolved technical divergence blocks merge; neither agent resolves a dispute by authority alone.