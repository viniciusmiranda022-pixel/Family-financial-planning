# Investigation — Official CVM structured data source for daily fund quotas

Issue #85 / `docs/WORK_ORDER_PRIVILEGE_DI_CVM.md` scope item: "Investigate and document the stable
official structured CVM ingestion method before implementing ingestion; do not screen-scrape when
structured official data exists." This document is that investigation, written before any ingestion
code, per the Work Order.

## Fund identity

- Name: ITAÚ PRIVILÈGE RENDA FIXA REFERENCIADO DI FIF DA CIC RESP LIMITADA
- CNPJ: `26.199.519/0001-34`
- CVM code shown in public fund information: `267996`
- Administrator: Itaú Unibanco S.A.
- Benchmark: CDI

## Official structured source

CVM (Comissão de Valores Mobiliários — the Brazilian securities regulator) publishes a public,
structured **Open Data Portal**: `https://dados.cvm.gov.br/`. Within it, the dataset group
"Fundos de Investimento" includes the daily fund report ("Informe Diário"), dataset id
`fi-doc-inf_diario` (portal page: `https://dados.cvm.gov.br/dataset/fi-doc-inf_diario`).

**Ingestion method chosen: bulk monthly CSV (inside a ZIP), not screen scraping, not an
undocumented API.** This is a first-party, machine-readable, structured government dataset —
exactly the "stable official structured CVM source" the Work Order requires instead of scraping an
HTML page.

### URL pattern

```
http://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{YYYY}{MM}.csv
```

(Files have been distributed as a compressed CSV, i.e. inside a `.zip` with the same basename,
since May/2022 — the client must be able to open either a raw CSV or a ZIP containing one CSV
member, and treat an unexpected container format as a malformed-response error rather than guessing.)

The data dictionary (column layout) is published alongside the dataset at:

```
https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/META/meta_inf_diario_fi.txt
```

### Column layout — verified current schema, plus the legacy variant

**Current schema (verified — see "Live-source verification" below).** CVM's fund-classes reform
(Resolução CVM 175) renamed the fund-identifying join column from `CNPJ_FUNDO` to `CNPJ_FUNDO_CLASSE`
for the Informe Diário v4.0 layout, and later added an `ID_SUBCLASSE` column immediately after
`DENOM_SOCIAL`:

```
TP_FUNDO; CNPJ_FUNDO_CLASSE; DENOM_SOCIAL; ID_SUBCLASSE; DT_COMPTC; VL_TOTAL; VL_QUOTA;
VL_PATRIM_LIQ; CAPTC_DIA; RESG_DIA; NR_COTST
```

**Legacy schema (pre-Resolução 175 adaptation, still relevant for older monthly archives and as a
fallback the parser must keep honoring):**

```
TP_FUNDO; CNPJ_FUNDO; DENOM_SOCIAL; DT_COMPTC; VL_TOTAL; VL_QUOTA; VL_PATRIM_LIQ;
CAPTC_DIA; RESG_DIA; NR_COTST
```

Relevant fields for this Work Order: `CNPJ_FUNDO_CLASSE`/`CNPJ_FUNDO` (fund CNPJ, the join key),
`DT_COMPTC` (competência/reference date), `VL_QUOTA` (quota value), `VL_PATRIM_LIQ` (fund net worth,
informational only). `ID_SUBCLASSE` is not consumed by this feature — the tracked fund is a single
share class with no subclasses — but its presence in the current schema is why the parser must stay
column-*name*-aware rather than column-*position*-aware: a positional parser would silently misread
every column after `DENOM_SOCIAL` once `ID_SUBCLASSE` was inserted.

**Design response (see `app/services/cvm_client.py`):** the parser looks for `CNPJ_FUNDO_CLASSE` first,
falling back to `CNPJ_FUNDO`, ignores unrecognized columns (so `ID_SUBCLASSE` and any future addition
are inert), and treats a file exposing **neither** CNPJ column as a malformed-response error (last valid
state retained, never a crash, never a silent zero) rather than guessing a column index. This satisfies
the Work Order's "CVM unavailable/malformed -> retain last valid state; no destructive write" acceptance
criterion regardless of which schema variant is live when the worker actually runs.

## Live-source verification

**This session's sandbox cannot reach any external host to fetch a live response body — this is an
environment-level network policy, not something specific to `dados.cvm.gov.br`.** Retested during this
PR's correction pass: `curl` and `WebFetch` were both denied, with the identical `connect_rejected`
("gateway answered 403 to CONNECT — policy denial") outcome, against `dados.cvm.gov.br`,
`www.gov.br`, `sistemas.cvm.gov.br`, `basedosdados.org`, `medium.com`, and even `example.com` — a
domain with no relationship to CVM at all. The sandbox's egress proxy allows only a small allowlist
(GitHub, package registries) and denies everything else uniformly. That rules out "the CVM source
specifically is unreachable" as an interpretation; it is this authoring sandbox that cannot reach the
open internet, full stop, which is an authoring-time constraint and not evidence about production
reachability.

Given that, live verification was still performed, through two channels that *are* available to this
session:

1. **Engineer's independent live check** (PR #101 review, 2026-09-17, from an unrestricted
   environment): confirmed the Portal Dados Abertos is reachable, confirmed the Informe Diário dataset,
   ZIP packaging since May/2022, the daily update policy, and CVM's published schema-change notice that
   `CNPJ_FUNDO_CLASSE` replaced `CNPJ_FUNDO`.
2. **Corroborating documentation research from this session, via `WebSearch`** (a hosted search index,
   not a direct fetch of `dados.cvm.gov.br` — it was not blocked by the sandbox's egress policy):
   - The dataset page (`https://dados.cvm.gov.br/dataset/fi-doc-inf_diario`) and its data dictionary
     resource (`https://dados.cvm.gov.br/dataset/fi-doc-inf_diario/resource/40891357-ddf8-45eb-9a06-1239dde929cd`,
     backed by `https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/META/meta_inf_diario_fi.txt`) are
     indexed and live.
   - The CSV delimiter is `;` (semicolon) and the file naming convention is `inf_diario_fi_{YYYYMM}.csv`
     — matches this implementation's existing assumption, unchanged.
   - `ID_SUBCLASSE` was added to the dataset, positioned after `DENOM_SOCIAL`, announced August/2025 —
     folded into the "current schema" layout above.
   - The `CNPJ_FUNDO` → `CNPJ_FUNDO_CLASSE` rename: search results place the published notice at
     2024-10-18, with the Informe Diário v4.0 layout applying from 2024-10-01 *for funds already
     adapted to Resolução CVM 175*. This is a **minor, disclosed discrepancy** against the engineer's
     review comment, which cited 28/10/2024 — funds migrate to the new layout individually as each
     completes its own RCVM 175 adaptation, so the exact date a given fund's rows carry
     `CNPJ_FUNDO_CLASSE` is fund-specific, not a single portal-wide cutover date. This does not change
     any implementation decision: the parser already tries both column names regardless of when either
     fund crossed over, so it is correct under either date.

Both channels agree with, and do not contradict, the implementation already in `cvm_client.py`. No fact
in this document is presented as unverified; the schema-variance handling (`CNPJ_FUNDO_CLASSE`/
`CNPJ_FUNDO` fallback, `ID_SUBCLASSE` and future columns ignored) was already correct for both the
pre- and post-Resolução-175 layouts before this correction pass — that risk was anticipated, not newly
discovered.

**`cvm_valuation_enabled` remains `false` by default.** This is no longer framed as compensating for an
unresolved evidence gap — it is a standard operational rollout control for a new external-data ingestion
path touching financial valuation: an operator enables it deliberately once, after which point it runs
unattended. See `app/config.py`.

## Decision summary

| Question | Decision |
| --- | --- |
| Screen-scrape an HTML page? | No — a stable structured official source exists. |
| Which structured source? | CVM Open Data Portal, `fi-doc-inf_diario` dataset, monthly bulk CSV/ZIP. |
| Per-fund query API? | Not used — CVM does not publish one for this dataset; the bulk file is the authority. |
| Column resolution strategy | Name-based lookup with `CNPJ_FUNDO_CLASSE`/`CNPJ_FUNDO` fallback; unrecognized schema -> malformed-response error state, never a guess. |
| Live-network verification | Performed via the engineer's independent live check plus this session's `WebSearch`-based corroboration (see "Live-source verification" above); both agree with the implementation. Direct fetch from this authoring sandbox is blocked by an environment-wide egress policy, not a CVM-specific issue. `cvm_valuation_enabled` stays off by default as a deliberate operator opt-in/rollout control. |
