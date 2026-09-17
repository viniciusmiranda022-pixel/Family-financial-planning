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

### Column layout (as documented) and a real schema-variance risk

The historical/documented column layout for `inf_diario_fi_*.csv` is:

```
TP_FUNDO; CNPJ_FUNDO; DENOM_SOCIAL; DT_COMPTC; VL_TOTAL; VL_QUOTA; VL_PATRIM_LIQ;
CAPTC_DIA; RESG_DIA; NR_COTST
```

Relevant fields for this Work Order: `CNPJ_FUNDO` (fund CNPJ, the join key), `DT_COMPTC`
(competência/reference date), `VL_QUOTA` (quota value), `VL_PATRIM_LIQ` (fund net worth, informational
only).

**Open risk, explicitly flagged rather than silently assumed:** CVM's fund-classes reform (Resolução
CVM 175) restructures many funds into "classes"/"subclasses" with their own CNPJ, and CVM's open-data
portal has already introduced `CNPJ_FUNDO_CLASSE`/`TP_FUNDO_CLASSE`/`ID_SUBCLASSE` columns in at least
one adjacent dataset (the informe-diário *document registry* metadata resource) as of the 2025 rollout.
Whether the bulk **quota-value** CSV (`inf_diario_fi_*.csv`, the one this feature actually parses) has
also migrated to `CNPJ_FUNDO_CLASSE` for this specific fund at the time the worker first runs in
production is not something this investigation can confirm with certainty from documentation alone.

**Design response to that risk (see `app/services/cvm_client.py`):** the parser is column-name-aware,
not column-position-aware. It looks for `CNPJ_FUNDO_CLASSE` first, falling back to `CNPJ_FUNDO`, and
treats a file exposing **neither** recognized column as a malformed-response error (last valid state
retained, never a crash, never a silent zero) rather than guessing a column index. This satisfies the
Work Order's "CVM unavailable/malformed -> retain last valid state; no destructive write" acceptance
criterion regardless of which schema variant is live when the worker actually runs.

## Explicit environment limitation — no live verification performed

This implementation was authored inside a sandboxed execution environment whose network egress proxy
**blocks outbound requests to `dados.cvm.gov.br`** (confirmed: both `WebFetch` and a direct `curl`
through the environment's configured HTTPS proxy return `403`/`CONNECT tunnel failed`). Every fact
above was established through documentation search (CVM's own portal pages, indexed and summarized by
search results), not by fetching and inspecting a live response body from this session.

Consequently:

- The CVM client, worker, and tests in this PR use **fixture-based, documented-schema CSV content**
  for all automated tests — never a live network call (tests must never depend on external network
  availability regardless of sandboxing, this is standard practice independent of the sandbox issue).
- **One live smoke run against the real CVM URL, from a network-unrestricted environment, is required
  before enabling the worker against production** — to confirm the exact column set currently being
  served for this fund's CNPJ and the exact container format (raw CSV vs ZIP) at that point in time.
  This is flagged explicitly as a residual, pre-production-enablement verification step in the PR
  (`cvm_valuation_enabled` defaults to `false` — see `app/config.py` — specifically so the feature ships
  dormant until that verification happens and an operator opts in).

## Decision summary

| Question | Decision |
| --- | --- |
| Screen-scrape an HTML page? | No — a stable structured official source exists. |
| Which structured source? | CVM Open Data Portal, `fi-doc-inf_diario` dataset, monthly bulk CSV/ZIP. |
| Per-fund query API? | Not used — CVM does not publish one for this dataset; the bulk file is the authority. |
| Column resolution strategy | Name-based lookup with `CNPJ_FUNDO_CLASSE`/`CNPJ_FUNDO` fallback; unrecognized schema -> malformed-response error state, never a guess. |
| Live-network verification | Not possible from this sandboxed session; required once, pre-production, by an operator with unrestricted network access. Worker ships disabled by default until then. |
