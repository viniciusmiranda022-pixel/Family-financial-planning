# Inventário de conflito e plano de migração — October Go-Live Rebaseline (Slice 0)

**Status:** documental. Nenhuma mudança financeira executável, migration ou correção automática de
dados reais foi feita neste PR.

**Fonte normativa confrontada:** `docs/OCTOBER_GO_LIVE_REBASELINE.md` (commit `62a76fb`), contra
`README.md`, `docs/FINANCIAL_RULES.md`, `docs/FINANCIAL_INVARIANTS.md`,
`docs/INTEGRITY_IMPLEMENTATION_PLAN.md`, `docs/ARCHITECTURE.md`, `docs/INTELLIGENCE.md`,
`docs/ROADMAP.md`, `app/models.py`, `alembic/versions/*`, o motor financeiro determinístico
(`app/services/financial_engine.py`, `app/services/finance.py`, `app/services/projection_engine.py`,
`app/services/financial_invariants.py`, `app/services/invariant_registry.py`), o Advisor/Codex
(`app/services/codex_client.py`, `app/services/codex_audit.py`, `advisor/*`), a API
(`app/api.py`), o frontend (`app/templates/index.html`, `app/static/app.js`) e a suíte de testes
(`tests/*`).

Método: leitura direta dos documentos normativos pelo executor, seguida de seis investigações de
código independentes e somente-leitura (uma por área de domínio), cada uma reportando evidência
`arquivo:linha`. Toda afirmação abaixo é rastreável a uma dessas fontes.

**Correções de revisão (2026-09-11, ENGINEERING REVIEW no PR #88, head `0a2da43`):**

1. `AccountBalanceObservation` isolada deixou de ser suficiente para materializar
   `liquidity_used`/`liquidity_deposit` em `reconcile_actual_liquidity` (§4 Slice 1, §8 Technical
   Challenge #1, §6 item 3) — agora exige movimento de transferência importado/observado no extrato
   bancário ou ação explicitamente confirmada pelo usuário/Assistente; uma observação de saldo
   isolada só atualiza a posição soberana e pode sinalizar `reconciliation_divergence`.
2. `EntryTypeTemplate`/tipos aprendidos de Entrada/Saída reatribuído de "Slice 8 (ou incremental
   3-5)" para Slice 4 (mecanismo de aprendizado do Assistente), com apresentação UX-only no Slice 5
   (§1.4, §3.6, §4).
3. Removida a recomendação de paralelizar o Slice 6; mantido sequenciamento estrito
   0→1→2→3→4→5→6→7→8, preservando apenas a nota arquitetural de independência técnica (§7, §9).

---

## 1. Matriz MANTER / ALTERAR / REMOVER / MIGRAR

Convenção: **MANTER** = comportamento atual já satisfaz o rebaseline, preservar tal como está.
**ALTERAR** = comportamento/documento existe mas contradiz ou está incompleto frente ao rebaseline.
**REMOVER** = comportamento existente é proibido pelo rebaseline e deve deixar de ser a fonte de
verdade (a função pode continuar existindo para outro propósito — ver notas). **MIGRAR** = não existe
hoje; requer schema/dado novo, aditivo.

### 1.1 MANTER

| Item | Arquivo:linha | Regra que já cumpre | Teste que prova |
|---|---|---|---|
| `AccountBalanceObservation` como saldo soberano no limite do período | `app/services/financial_snapshots.py:153-260` (`_opening_balance`, `_observation_is_trusted`) | Rebaseline §5.1 — saldo confirmado é soberano | `tests/test_financial_snapshots.py` (testes de `balance_evidence_trusted`) |
| Competência de compra no cartão (fechamento/vencimento) | `app/services/card_competence.py:40-125` | Rebaseline §6.1 — compra entra em "Gastos do mês" e na fatura aberta correta, sem tocar saldo bancário | `tests/test_card_open_invoice_competence.py`; `tests/test_financial_invariants.py::test_card_purchase_uses_statement_competence` (INV-017) |
| Parcelas futuras nunca materializadas como `Transaction` antecipada | `app/api.py:6665-6731` (`_project_installments`, `_future_installments`) | Rebaseline §6.7 — parcelas futuras são COMPROMETIDO, nunca REALIZADO antecipado | `tests/test_manual_financial_flows.py` (`test_manual_installment_feeds_canonical_projection_without_duplicating_observed_installment` e 6 outros) |
| Comissão nunca criada automaticamente (só `POST /commissions` admin ou import explícito de planilha) | `app/api.py:5579-5590`; `app/services/plan_workbook.py:715-731` | Rebaseline §8.3 — comissão só existe quando efetivamente registrada | INV-008/INV-010 (`tests/test_financial_invariants.py:207-267`); `tests/test_finance.py:14-47` |
| Estorno neutraliza a despesa sem virar receita operacional | `app/api.py:4377-4380`; `app/services/classifier.py:77,87,140-154` | Rebaseline §6.6 (parte econômica) | INV-016 (`tests/test_financial_invariants.py:382-397`); `tests/test_manual_financial_flows.py:166-230` |
| Fronteira estrutural do Codex (schema sem campo de autoridade, sandbox read-only, `AuditOutcome` sem `status/score`) | `app/services/codex_audit.py`; `app/services/audit_sanitizer.py`; `advisor/audit-schema.json`; `advisor/server.mjs:110-119` | Rebaseline §11.2 — Codex não pode inventar fato nem mudar veredito; base estrutural para os "typed actions" do Slice 4 | `tests/test_codex_audit.py`; `tests/test_audit_sanitizer.py`; `advisor/test/*.mjs` (gate `advisor-contract-security`) |
| `pay_obligation`/`unpay_obligation`: vincula transação real, remove da projeção futura, evita despesa duplicada, aceita pagamento antecipado, pergunta origem (`funding_source`) | `app/api.py:6026-6262` | Rebaseline §7 | **Gap de cobertura** — hoje só há teste de autorização (`tests/test_role_based_authorization.py:551-561`); nenhum teste funcional. Ver §6. |
| Piso de segurança é indicador/alerta, nunca bloqueia retirada | `app/services/financial_engine.py:146,157-158` (`distance_to_floor`, `floor_breached`) | Rebaseline concorda com INV-007 — piso nunca bloqueia | `tests/test_financial_invariants.py::test_safety_floor_never_blocks_real_deficit_coverage` |
| INV-001, INV-002, INV-003, INV-004, INV-009, INV-011, INV-012, INV-013, INV-014, INV-015, INV-018, INV-019, INV-020, INV-021, INV-022 | `app/services/financial_invariants.py` (linhas mapeadas em `app/services/invariant_registry.py:34-306`) | Nenhum conflito identificado com o rebaseline | Testes já existentes em `tests/test_financial_invariants.py` |

### 1.2 ALTERAR

| Item | Arquivo:linha | Conflito com o rebaseline | Teste afetado |
|---|---|---|---|
| **`settle_liquidity`/`calculate_actual_snapshot`** — liquidação automática de déficit/sobra do Privilège | `app/services/financial_engine.py:100-204` | Rebaseline §4.3 proíbe `resultado_operacional < 0 -> fabricar liquidity_withdrawal` e a aplicação automática de sobra. Contradiz `docs/FINANCIAL_RULES.md:64,66` e INV-005/INV-006/INV-007 **atuais**, que mandam exatamente esse comportamento. **Ver Technical Challenge #1** — a remoção literal quebra a projeção, que precisa continuar hipotética. | `test_liquidity_transition_never_produces_negative_balance`, `test_deficit_uses_all_available_liquidity_before_becoming_uncovered`, `test_safety_floor_never_blocks_real_deficit_coverage` (`tests/test_financial_invariants.py:142-207`); 4 testes em `tests/test_property_based_financial_rules.py:116-181`; `tests/test_financial_snapshots.py:1671,1744` |
| `docs/FINANCIAL_RULES.md` — seção "Conta central de liquidez — Privilège DI" (linhas 57-70) | `docs/FINANCIAL_RULES.md:64,66` | Documenta a liquidação automática como regra vigente; precisa virar "liquidação automática só é usada em projeção/cenário hipotético; em fato realizado exige evidência" | — |
| `docs/FINANCIAL_INVARIANTS.md` — INV-005/INV-006/INV-007 | `docs/FINANCIAL_INVARIANTS.md:103-144` | Mesmo conflito acima; requer bump de `financial_rules_version` (hoje `2026.09.1`) e reescopo textual para "aplica-se a saldo projetado/hipotético, não a fato realizado sem evidência" | Idem acima |
| `docs/INTEGRITY_IMPLEMENTATION_PLAN.md` §7.3 "Regra canônica do Privilège DI" | `docs/INTEGRITY_IMPLEMENTATION_PLAN.md:466-493` | Mesma fórmula automática documentada como regra canônica do Financial Integrity Engine | — |
| `README.md` — instruções de uso (linha 154) | `README.md:154` | "A visão geral mostra a sobra que deve ir para essa conta ou o déficit que precisa ser coberto por ela" descreve fabricação automática como UX esperada | — |
| Bloqueio de pagamento parcial de fatura | `app/services/card_payment_reconciliation.py:989-996` (`pay_card_invoice`) | Rebaseline §6.5 exige suporte a pagamento parcial com principal financiado carregado ao próximo ciclo; código atual **rejeita explicitamente** qualquer valor fora da tolerância de R$ 0,01, com mensagem "pagamento parcial de fatura não é suportado por este contrato" | `tests/test_manual_payables_card_payment.py::test_manual_payment_rejects_amount_outside_tolerance_partial_payment` (289) — precisa ser substituído por teste que prova o novo comportamento |
| Estado binário `pending`/`paid` de "fatura" (na verdade um `Transaction` de reconciliação, não uma entidade própria) | `app/services/card_payment_reconciliation.py:660-725` (`list_card_invoice_obligations`) | Rebaseline §6.2/6.3 exige `aberta -> fechada -> parcialmente_paga/paga`; hoje não existe nem o conceito de "fechada" | 6 testes `test_card_invoice_obligation*`/`test_card_invoice_obligations_reflect_only_provable_states` (`tests/test_manual_payables_card_payment.py:672-897`) |
| Agrupamento de `GET /obligations` | `app/api.py:571-619` (`_obligation_rows`), `alert_level` em `app/api.py:543-568` | Rebaseline §7 exige 6 grupos (vencendo/faturas fechadas/parcelas/recorrências/atrasadas/pagas recentemente); hoje só existe `overdue/urgent/soon/scheduled/paid` numa lista única, sem mesclar faturas de cartão nem parcelas | `tests/test_browser_due_notifications.py` (grupamento atual precisa ser estendido, não quebrado) |
| Navegação principal | `app/templates/index.html:55-72` (`#main-nav`) | Rebaseline §2 define menu exato; hoje existem 16 itens (`dashboard, reports, capture, entradas, saidas, payables, transferencias, imports, transactions, reviews, income, planning, advisor, integrity, users, settings`) versus os 9 alvo, com `Lançar agora` como item principal (proibido) e sem `Gastos & Economia`/`Assistente Financeiro` | Nenhum teste cobre a lista de abas hoje — `tests/test_browser_due_notifications_frontend.py` só testa funções JS específicas, não o menu |
| Restrição de `pay_obligation` a ocorrência única | `app/api.py:6039-6046` | Rebaseline §7 quer "parcelas"/"recorrências" visíveis e pagáveis na área de obrigações; hoje o endpoint recusa pagar diretamente qualquer `Obligation` com `recurrence_months != 0` ou `occurrence_count != 1` | Nenhum teste funcional cobre `pay_obligation` hoje (ver gap em §6) — **decisão de design em aberto, não uma contradição definitiva; ver §7 Slice 3** |

### 1.3 REMOVER

| Item | Arquivo:linha | Motivo |
|---|---|---|
| Uso do resultado de `settle_liquidity` como fonte de `liquidity_used`/`liquidity_deposit` em snapshot **realizado/fechado** (`calculate_actual_snapshot`) | `app/services/financial_engine.py:162-204`, campos surfaced em `app/services/financial_snapshots.py:1126,1193,1376,1585` e `app/api.py:8014-8015,8477-8478,8521` | A função em si continua necessária para projeção (cenário hipotético); o que deve ser removido é sua autoridade sobre o **fato realizado** do período fechado sem evidência observada/confirmada. Ver Technical Challenge #1 para o desenho recomendado. |
| Bloqueio duro de pagamento parcial de fatura | `app/services/card_payment_reconciliation.py:989-996` | Superado pela nova regra de pagamento parcial + principal carregado (Slice 2) |
| Item de navegação principal "Lançar agora" | `app/templates/index.html:58` | Rebaseline §2: "Não existe botão global `+ Lançar` como item principal." Funcionalidade absorvida por Assistente/Entradas/Saídas, não eliminada. |

### 1.4 MIGRAR (schema novo, aditivo — proposto no §3, não criado neste PR)

| Item | Slice | Nota de reutilização |
|---|---|---|
| Entidade de fatura de cartão (`CardInvoice`) + `Transaction.card_invoice_id` | 2 | Reutiliza `card_competence.py` para competência; não duplica o motor de classificação |
| `Transaction.refund_of_transaction_id` (FK autorreferente) | 2 | Mesmo padrão já usado por `Transaction.linked_transaction_id` (migração `0004`) |
| `Investment` + `InvestmentValuation` (histórico imutável) | 6 | Reutiliza o padrão de observação imutável + `supersedes_id` já usado por `AccountBalanceObservation` |
| `AssistantActionEvent` (log estruturado do Assistente) | 4 | Estende `AuditEvent` em vez de duplicar a trilha de auditoria genérica |
| `EntryTypeTemplate`/tipos aprendidos de Entrada/Saída | 4 (mecanismo de aprendizado; apresentação UX-only no Slice 5) | Reutiliza o ciclo de vida já comprovado de `ClassificationRule` (`observed -> suggested -> pending_acceptance -> active`) |
| Backfill de `CardInvoice` a partir de `Transaction` histórico | 8 | Segue o padrão somente-leitura + insert aditivo já usado por `app.cli.backfill`; nunca escreve em `Transaction` |

---

## 2. Contradições explícitas mapeadas (Work Order item 2)

1. **Déficit operacional inferido virando uso automático do Privilège.** `settle_liquidity`
   (`app/services/financial_engine.py:139-144`) computa `used = min(opening, deficit)` sempre que o
   resultado é negativo — exatamente o `resultado_operacional < 0 -> fabricar liquidity_withdrawal`
   que o rebaseline proíbe (§4.3). Documentado atualmente como regra correta em
   `docs/FINANCIAL_RULES.md:64` e INV-006.
2. **Resultado positivo virando aplicação automática no Privilège.** Mesmo arquivo,
   `financial_engine.py:134-138`: `result >= 0 -> deposit = result`. Documentado como regra correta
   em `docs/FINANCIAL_RULES.md:66`.
3. **Projeção que recalcula/sobrescreve saldo observado posterior.** Não há dupla contagem no
   limite do período (a observação confiável substitui o saldo aditivo — `financial_snapshots.py:204-217`),
   mas uma observação **dentro** do período corrente é ignorada até o próximo período
   (`tests/test_financial_snapshots.py:73-108`, prova viva do gap). O rebaseline §5.2 exige
   distinguir continuamente "saldo observado", "movimentos posteriores" e "saldo corrente derivado" —
   isso não existe hoje em granularidade sub-mensal.
4. **Comissão futura/projeção automática em desacordo com o rebaseline.** Não localizada — Commission
   só é criada por ação humana explícita (`app/api.py:5579-5590`) ou import de planilha explícito
   (`plan_workbook.py:715-731`); nenhum caminho de importação/classificação cria `Commission`
   automaticamente. **Este item já está em conformidade** (ver MANTER acima), mas note-se a lacuna:
   `Commission.received_date` (`app/models.py:290`) existe no modelo e nunca é lido em
   `app/api.py`/`projection_engine.py` — uma comissão nunca é excluída da projeção depois de
   efetivamente recebida, o que é uma falha de reconciliação (dupla contagem potencial), não de
   invenção. Deve ser corrigido no Slice 3.
5. **Ciclo de cartão/fatura sem `open/closed/partially_paid/paid`, carry-forward, encargos, estorno
   e parcelamento pedidos.** Confirmado — não existe entidade de fatura; estado é binário
   `pending`/`paid` derivado (`card_payment_reconciliation.py:660-725`); pagamento parcial é
   rejeitado (`:989-996`); não há detecção de divergência (IOF/juros/duplicidade); estorno neutraliza
   mas não tem FK para a compra original.
6. **Limites atuais do Codex/Advisor versus typed actions, auditoria e validação desejadas.**
   Confirmado — os três contratos existentes (`/v1/classify`, `/v1/analyze`, `/v1/audit`) são
   inteiramente consultivos; não existe nenhum caminho de execução de ação tipada por linguagem
   natural, nem log de ação do Assistente, nem undo genérico.
7. **Navegação atual versus menu final.** Confirmado — 16 abas atuais vs. 9 abas alvo; "Lançar
   agora" é item principal (proibido); "Gastos & Economia" e "Assistente Financeiro" não existem
   como tais.

---

## 3. Plano de dados/migration proposto (sem implementar — Work Order item 3)

Todas as migrations abaixo são **aditivas e reversíveis** (nova tabela/coluna nullable ou com
default seguro; downgrade remove o que o upgrade criou, seguindo o padrão já usado em todas as 13
migrations existentes — ver §5). Nenhuma altera ou apaga `Transaction`, `Document`, `PayrollRecord`,
`Commission` ou `Obligation` existentes.

### 3.1 Lifecycle de fatura (Slice 2)

Nova tabela `card_invoices` (nome de classe proposto `CardInvoice`):

```text
id UUID PK
household_id FK
account_id FK (cartão)
competence String (YYYY-MM, mesma semântica de card_competence.py)
opens_at Date
closes_at Date
due_date Date
status String  # open|closed|partially_paid|paid  (String livre, sem CHECK fixo — mesmo padrão de integrity_findings.status)
declared_total Numeric(14,2) nullable   # total informado pelo banco/documento, quando houver
computed_total Numeric(14,2)            # soma determinística das compras da competência
principal_carried_in Numeric(14,2) default 0   # saldo não pago herdado do ciclo anterior
principal_carried_out Numeric(14,2) default 0  # saldo não pago repassado ao próximo ciclo
paid_total Numeric(14,2) default 0
closed_at DateTime nullable
trace_id
created_at / updated_at
```

`Transaction.card_invoice_id` (FK nullable, mesmo padrão de `linked_transaction_id` da migração
`0004`) associa cada compra/pagamento à fatura sem alterar nenhuma coluna existente de
`Transaction`.

`principal_carried_out` de um ciclo alimenta `principal_carried_in` do próximo — nunca cria uma
nova `Transaction` de compra nem de despesa (rebaseline §6.5: "não é nova compra e não como novo
gasto"). Juros/IOF observados entram como `Transaction` de despesa normal, competência do ciclo em
que foram efetivamente informados — nunca fabricados por diferença.

### 3.2 Estorno vinculado à compra original (Slice 2)

`Transaction.refund_of_transaction_id` (FK autorreferente nullable, mesmo padrão de
`linked_transaction_id`). Preenchido apenas quando o usuário/Assistente confirma o vínculo (nunca
inferido silenciosamente) — mantendo INV-016 intacto e fechando o gap de rastreabilidade
identificado no §2.5.

### 3.3 Estados REALIZADO/COMPROMETIDO/PREVISTO (Slice 3)

**Recomendação: não criar coluna persistida.** Os três estados já são determináveis a partir de
dados existentes (origem `Transaction` real = REALIZADO; `Obligation.status="pending"`/
`CardInvoice.status in (open,closed,partially_paid)`/parcela futura = COMPROMETIDO; linha de
projeção/`Commission`/`monthly_salary_net` futuro = PREVISTO). Criar uma coluna redundante
arriscaria um segundo motor de classificação divergente do dado de origem (violação do princípio
"nenhum segundo motor de cálculo/reconciliação"). Proposta: um campo computado
`financial_state` adicionado na camada de serialização/schema (`app/schemas.py`) de cada
superfície relevante (`_obligation_rows`, `_project_installments`, linhas de `build_forecast`),
derivado deterministicamente da origem do dado, nunca persistido nem editável.

### 3.4 Investimentos/Studio e histórico de valuation (Slice 6)

```text
investments
  id UUID PK
  household_id FK
  name String
  historical_cost Numeric(14,2)          # "Valor investido"
  current_value Numeric(14,2)            # "Valor de hoje" — único campo usado no patrimônio
  expected_receivable_value Numeric(14,2) nullable
  last_updated_at Date
  expected_receipt_date Date nullable
  notes Text nullable
  active bool default true
  created_at / updated_at

investment_valuations
  id UUID PK
  investment_id FK
  valuation_date Date
  current_value Numeric(14,2)
  expected_receivable_value Numeric(14,2) nullable
  contribution_amount Numeric(14,2) nullable   # aporte, quando o evento for aporte
  funding_transaction_id FK nullable           # origem do recurso do aporte, quando aplicável
  source String   # manual_confirmed|assistant_confirmed
  recorded_by FK
  note Text nullable
  created_at
```

Mesmo padrão de observação imutável de `AccountBalanceObservation`: uma correção cria nova linha em
`investment_valuations` e atualiza `investments.current_value`/`last_updated_at`; nunca reescreve uma
avaliação anterior.

### 3.5 Action log do Assistente (Slice 4)

Nova tabela `assistant_action_events`, 1:1 com `audit_events` (FK `audit_event_id`, reaproveitando
`AuditEvent.user_id/household_id/trace_id/created_at` em vez de duplicá-los):

```text
id UUID PK
audit_event_id FK -> audit_events.id
original_message Text
structured_interpretation JSONB     # saída do Codex: intenção, campos extraídos, confiança
disambiguation_qa JSONB             # perguntas feitas e respostas do usuário
typed_action String                 # nome da ação tipada executada (ex.: "pay_obligation")
target_entity_type String nullable
target_entity_ids JSONB             # lista de IDs criados/alterados
before_state JSONB nullable
after_state JSONB nullable
undoable bool
undone_at DateTime nullable
undone_by FK nullable
```

A execução em si nunca é um motor novo: `typed_action` sempre corresponde a uma chamada real a um
endpoint/serviço determinístico já existente ou proposto no §4 (`pay_obligation`,
`POST /card-invoices/{id}/pay`, `POST /captures/{id}/confirm`, etc.).

### 3.6 Templates dinâmicos de Entrada/Saída (Slice 4; apresentação UX-only no Slice 5)

**Correção de revisão (2026-09-11):** a atribuição original ("Slice 8, ou incremental 3-5") era
ambígua e conflitava com a sequência normativa. O rebaseline (§"Slice 4 — Assistente Financeiro
operacional... e aprendizado") já inclui explicitamente aprendizado do Assistente no Slice 4;
portanto o mecanismo de tipo aprendido (tabela, ciclo de vida, ativação por administrador) pertence
ao Slice 4. O Slice 5 fica restrito à apresentação/UX (exibir "Outra entrada"/"Outra saída" com os
templates já `active` nos seletores de Entradas/Saídas) — nenhuma lógica de aprendizado nova é
criada no Slice 5. O Slice 8 permanece reservado a migração/reconciliação real e E2E/smoke de
go-live, sem lógica de produto nova.

Reaproveita o ciclo de vida já testado de `classification_rules`
(`docs/FINANCIAL_INVARIANTS.md:198-222`): nova tabela `entry_type_templates` com
`status: observed -> suggested -> pending_acceptance -> active`, `confirmation_count`,
`movement_type (income|expense)`, `label`, `evidence JSONB`, ativação restrita a administrador —
mesma barreira de `POST /classification-rules/{id}/activate`.

---

## 4. Contratos de API/serviço propostos para os Slices 1–8

Reutilizando estruturas existentes; nenhum motor financeiro/reconciliação paralelo é proposto.

- **Slice 1** — `financial_engine.py` ganha `settle_liquidity_projection` (a função atual,
  renomeada/isolada para uso exclusivo da projeção) e `reconcile_actual_liquidity` (novo: só produz
  `liquidity_used`/`liquidity_deposit` quando há um movimento de transferência importado/observado
  no extrato bancário ou uma ação explicitamente confirmada pelo usuário/Assistente no período —
  correção de revisão de 2026-09-11: uma `AccountBalanceObservation` isolada **nunca** basta, pois
  prova a posição soberana num instante, não a causa; ela apenas atualiza a posição soberana e pode
  gerar `reconciliation_divergence`/`unexplained_operating_result` para revisão humana, mas não
  fabrica um resgate/aplicação específico. Na ausência de movimento importado/observado ou ação
  confirmada, `closing_liquidity_balance = opening` e o campo `unexplained_operating_result` é
  exposto para revisão humana, nunca mascarado).
  Novo endpoint `POST /liquidity/confirm-movement` (mesmo padrão de `funding_source` do
  `pay_obligation`) para o usuário confirmar explicitamente um resgate/aplicação. Extensão do
  payload de snapshot/dashboard com `observed_balance`, `movements_since_observation`,
  `derived_balance_since_observation`, `reconciliation_divergence` (campos computados, sem nova
  coluna obrigatória em `financial_snapshots`).
- **Slice 2** — novo `app/services/card_invoice_lifecycle.py` (fechar fatura no dia de fechamento,
  aplicar pagamento total/parcial, carregar principal, procurar causa de divergência), reutilizando
  `card_competence.py` (competência) e estendendo `card_payment_reconciliation.py` (matching
  banco↔fatura já existente) em vez de substituí-lo. Endpoints: `POST /card-invoices/{id}/close`
  (idempotente, disparado por job/cron ou leitura preguiçosa no primeiro acesso pós-fechamento),
  `POST /card-invoices/{id}/pay` (superset do atual `pay_card_invoice`, aceita valor parcial),
  `GET /card-invoices/{id}/divergence`.
- **Slice 3** — `_obligation_rows` ganha `financial_state` computado e passa a mesclar
  `CardInvoice` (faturas fechadas) e `_future_installments` (parcelas) na mesma resposta, mantendo
  cada linha marcada com sua origem para nunca somar duas vezes. Nova função
  `reconcile_recurring_income` (mesmo padrão de tolerância+janela de
  `card_payment_reconciliation.py`) liga o crédito real da Kelly ao `monthly_salary_net` PREVISTO do
  mês, sem duplicar renda. `Commission.received_date` passa a ser lido para excluir comissão já
  recebida da projeção futura (fecha o gap do §2.4).
- **Slice 4** — sidecar ganha um quarto contrato consultivo, `POST /v1/interpret` (mesmo padrão
  estrutural de `/v1/classify`/`/v1/analyze`: schema de saída sem campo de autoridade, apenas
  intenção/campos extraídos/perguntas pendentes). Backend: `POST /assistant/interpret` (chama o
  sidecar, nunca escreve) e `POST /assistant/execute` (só aceita um `typed_action` cujos campos
  batam 1:1 com o schema de um endpoint determinístico já existente — `pay_obligation`,
  `POST /card-invoices/{id}/pay`, `POST /captures/{id}/confirm`, `POST /transactions`,
  `POST /liquidity/confirm-movement`, `POST /investments/{id}/valuations` — nunca uma rota nova de
  escrita exclusiva do Assistente). `POST /assistant/actions/{id}/undo` só é aceito quando o
  `typed_action` subjacente já tem um caminho de reversão determinístico (reaproveita
  `unpay_obligation`, `card_payment_reconciliations/unlink`, exclusão auditada de lançamento
  manual). Ainda neste slice: nova tabela `entry_type_templates` (§3.6) e endpoints
  `POST /entry-type-templates` (criação, status inicial `observed`/`suggested`) e
  `POST /entry-type-templates/{id}/activate` (restrito a administrador, mesma barreira de
  `POST /classification-rules/{id}/activate`) — mecanismo de aprendizado do Assistente para "Outra
  entrada"/"Outra saída".
- **Slice 5** — puramente frontend: renomeação/reordenação de `data-view` em
  `app/templates/index.html`, absorção das views antigas como abas contextuais dentro das novas,
  remoção de "Lançar agora" como item de `#main-nav`. Nenhum endpoint novo. Único ponto não
  puramente cosmético: os seletores de Entradas/Saídas passam a listar os `entry_type_templates`
  já `active` (criados no Slice 4) como opções reutilizáveis — apresentação apenas, nenhuma lógica
  de aprendizado nova.
- **Slice 6** — CRUD simples sobre `Investment`/`InvestmentValuation` (`POST/GET/PATCH
  /investments`, `POST /investments/{id}/valuations`), reaproveitando o padrão de
  `POST /account-balances` (observação imutável + `supersedes_id`). Dashboard/patrimônio somam
  apenas `current_value`.
- **Slice 7** — sem endpoint novo além de agregações de leitura; `Gastos & Economia` reaproveita
  `report_export.py`/`_build_report_payload`; separação "seus dados / referências externas /
  análise / recomendação" é só formatação da resposta já estruturada de `/v1/analyze`.
- **Slice 8** — comando de reconciliação real (mesmo padrão do `app.cli.backfill`: idempotente,
  `--dry-run`, nunca escreve em `Transaction`/`Document`/`PayrollRecord`/`Commission`/`Obligation`;
  só faz insert aditivo em `card_invoices`/observações novas).

---

## 5. Estratégia de compatibilidade e migração dos dados existentes

- Toda migration listada no §3 é aditiva (nova tabela ou coluna nullable/`default` seguro),
  seguindo exatamente o padrão das 13 migrations já existentes (`alembic/versions/0001`–`0013`,
  todas com upgrade aditivo; downgrade remove o que o upgrade criou — nenhuma altera dado histórico
  no upgrade). Cabeça atual da cadeia: `0013_card_cycles_privilege_funding`.
- Nenhuma correção silenciosa: observações de saldo continuam soberanas
  (`AccountBalanceObservation`), e o backfill de `CardInvoice`/`refund_of_transaction_id` é sempre
  uma derivação **de leitura** sobre `Transaction` existentes, inserindo linhas novas nas tabelas
  novas — nunca reescrevendo `Transaction.competence`, `Transaction.transaction_type` ou qualquer
  outro fato já publicado. Mesmo padrão comprovado de `app.cli.backfill` (PR 8): idempotente,
  retomável, `--dry-run` disponível, nunca escreve nas tabelas de fato financeiro.
- Qualquer alteração ao contrato de invariantes (INV-005/006/007) exige, por
  `docs/FINANCIAL_INVARIANTS.md` seção "Controle de mudança": justificativa financeira explícita,
  bump de `financial_rules_version`, atualização conjunta de documentação/registry/testes,
  avaliação de impacto em snapshots já persistidos, e registro em audit trail — nenhuma reescrita
  silenciosa de snapshot histórico é proposta; snapshots antigos mantêm seu `calculation_version`
  original e continuam auditáveis como estavam.
- `financial_rules_version` deve avançar de `2026.09.1` para uma revisão `2026.1X.1` no Slice 1,
  documentando exatamente o reescopo (liquidação automática restrita a projeção).

---

## 6. Matriz de testes P0 (rebaseline §18, "Invariantes P0 de produto")

| # | Invariante P0 | Teste existente a preservar | Teste existente a alterar | Teste novo obrigatório |
|---|---|---|---|---|
| 1 | Pagamento de fatura nunca duplica gasto | `test_manual_payment_with_sufficient_amount_is_reconciliation_with_zero_new_expense` (INV-002) | — | Pagamento **parcial** também não duplica gasto (Slice 2) |
| 2 | Aplicação/resgate Privilège↔Corrente nunca vira renda/despesa | INV-003/004 (`test_application_is_patrimonial_movement`, `test_redemption_is_not_operating_income`) | — | — (não afetado pelo Slice 1) |
| 3 | Resultado operacional negativo nunca fabrica resgate | — | `test_liquidity_transition_never_produces_negative_balance`, `test_deficit_uses_all_available_liquidity_before_becoming_uncovered`, `test_safety_floor_never_blocks_real_deficit_coverage`, 4 testes de `test_property_based_financial_rules.py:116-181`, `test_financial_snapshots.py:1671,1744` — todos precisam ser rescopados para "projeção", não "fato realizado" | `test_actual_snapshot_never_fabricates_liquidity_withdrawal_without_transfer_movement_or_confirmed_action` **e** `test_account_balance_observation_alone_never_materializes_liquidity_transfer` (observação de saldo isolada só pode gerar `reconciliation_divergence`/`unexplained_operating_result`, nunca `liquidity_used`/`liquidity_deposit`) |
| 4 | Saldo observado posterior não sofre subtração dupla | `test_point_in_time_balance_becomes_next_period_opening_evidence` (documenta o comportamento atual — preservar como caso de fronteira) | — | Observação **intra-período** deve refletir imediatamente, não só no período seguinte |
| 5 | Compra de cartão não reduz saldo antes do pagamento | `tests/test_card_open_invoice_competence.py` (suite completa) | — | — |
| 6 | Fatura fechada não paga é COMPROMETIDO | — | — | Suite completa nova para `CardInvoice` (estado `closed`) |
| 7 | Pagamento parcial carrega principal sem novo gasto | — | `test_manual_payment_rejects_amount_outside_tolerance_partial_payment` (comportamento invertido) | `principal_carried_forward` não é novo gasto nem nova compra |
| 8 | Juros/IOF são novos gastos só quando observados/confirmados | — | — | Juros/IOF nunca fabricados automaticamente pela diferença de divergência |
| 9 | Estorno neutraliza sem apagar histórico | INV-016; `test_manual_refund_offsets_expense_without_becoming_income` | — | Vínculo `refund_of_transaction_id` aponta para a compra original e nunca a apaga |
| 10 | Parcelas futuras são COMPROMETIDO, não REALIZADAS | `test_manual_installment_feeds_canonical_projection_without_duplicating_observed_installment` e 6 outros | — | `financial_state == "COMPROMETIDO"` explícito na resposta |
| 11 | Pagamento de obrigação real remove compromisso e não duplica despesa | — (comportamento existe mas só testado por autorização) | — | Suite funcional completa de `pay_obligation`/`unpay_obligation`: antecipação, prevenção de duplicidade, `funding_source`, remoção da projeção |
| 12 | Comissão não entra em previsão automaticamente | INV-008/INV-010 | — | Guarda de regressão: nenhum importador/classificador cria `Commission`; `received_date` exclui da projeção futura |
| 13 | Salário recorrente da Kelly concilia com crédito real sem duplicar | — | — | Suite completa nova (`reconcile_recurring_income`) |
| 14 | Patrimônio usa "Valor de hoje" uma única vez | — | — | Suite completa nova (`Investment`/patrimônio) |
| 15 | Saldo confirmado é autoridade; divergência derivada é sinalizada | Testes de `balance_evidence_trusted` (limite de período) | — | Divergência intra-período contínua (mesmo item do #4) |
| 16 | Duplicidade provável exige decisão humana | INV-014 e testes de `duplicate_groups` | — | — (já conforme) |
| 17 | Ação do Assistente é auditável e reversível | — | — | Suite completa nova (`AssistantActionEvent`, undo) |
| 18 | Assistente pergunta origem quando saída não informa | — | — | Suite completa nova (fluxo de desambiguação) |
| 19 | Motor e Codex podem discordar sem virar "confiável" | INV-021 (`test_advisor_cannot_override_official_values`); testes estruturais do Codex Audit | — | — (já conforme) |
| 20 | Nenhuma tela depende de tipos internos | — | — | Teste estático de fonte (padrão `test_browser_due_notifications_frontend.py`) garantindo ausência de jargão técnico nos rótulos do menu/dashboard |

---

## 7. Sequenciamento técnico final dos Slices 1–8

```text
Slice 1 (Motor de caixa/Privilège)
   │  bloqueia: 2 (funding origem), 3 (estado de projeção), 7 (relatório), 8 (reconciliação real)
   ▼
Slice 2 (Ciclo de cartões/faturas)
   │  depende de: 1 (fluxo de origem Corrente×Privilège já estável)
   │  bloqueia: 3 (faturas fechadas alimentam agrupamento de obrigações)
   ▼
Slice 3 (Obrigações + REALIZADO/COMPROMETIDO/PREVISTO)
   │  depende de: 1, 2
   │  bloqueia: 4 (ações tipadas precisam de contratos finais para apontar)
   ▼
Slice 4 (Assistente Financeiro operacional)
   │  depende de: 1, 2, 3 (typed actions só podem mirar contratos já estáveis)
   ▼
Slice 5 (UX e navegação alvo)
   │  depende de: 1–4 substancialmente prontos (evita navegar para fluxo inacabado)
   │  pode iniciar renomeação mecânica de abas em paralelo, mas a integração final aguarda 4
   ▼
Slice 6 (Patrimônio e investimentos)
   │  nota arquitetural: tecnicamente independente de 2–5 (CRUD de
   │  `Investment`/`InvestmentValuation` não compartilha tabela nem serviço com
   │  cartões/obrigações/Assistente/navegação) — mas isso é só uma observação de dependências, não
   │  uma recomendação de execução. Correção de revisão (2026-09-11): a governança do P0 #87 exige
   │  sequenciamento estrito 0→1→2→3→4→5→6→7→8; Slice 6 não deve ser iniciado/executado em paralelo
   │  a 2–5.
   │  bloqueia: 7 (relatório patrimonial)
   ▼
Slice 7 (Gastos & Economia + Relatórios)
   │  depende de: 1 (fatos de liquidez corretos), 3 (estados para relatório), 6 (patrimônio)
   ▼
Slice 8 (Migração, reconciliação real e Go-Live)
      depende de: 1–7 todos mesclados e estáveis
```

**Riscos por slice** (rebaseline §"Riscos a analisar", mapeados):

| Risco | Slices afetados |
|---|---|
| Dupla contagem entre compra, fatura, pagamento e carry-forward | 2, 3 |
| Dupla subtração do Privilège quando existe saldo observado | 1 |
| Projeção fabricando transações futuras | 1, 3 |
| Migration que reinterpreta fatos históricos sem confirmação | 2 (backfill de `CardInvoice`), 8 |
| Estorno apagando trilha histórica em vez de neutralizar | 2 |
| Duplicidade resolvida automaticamente sem decisão humana | já mitigado (INV-014), vigiar em 2/3 |
| Regressão de compatibilidade com dados reais já importados | todos, crítico em 8 |
| Typed actions do Assistente bypassando motor/autorização | 4 |
| Divergência entre Dashboard, Relatórios, snapshots e projeção | 1, 3, 7 |

**Critérios de entrada/saída por slice:** entrada = slice anterior mesclado no `main` com os 12
gates verdes; saída = testes P0 do próprio slice (§6) verdes, 12 gates verdes no head do slice,
nenhum Technical Challenge relevante em aberto, revisão do engenheiro responsável concluída.
Nenhum slice avança sem o merge explícito do anterior (Work Order, "Proibições": "Não implementar
Slices 1+ neste PR").

---

## 8. Technical Challenges

### Technical Challenge #1 — remoção literal da liquidação automática quebra a projeção

**Orientação questionada:** uma leitura literal do rebaseline §4.3 ("Regra que deve ser removida:
`resultado_operacional < 0 -> fabricar liquidity_withdrawal`") poderia ser interpretada como "apagar
`settle_liquidity`". Isso quebraria a projeção de 30/60/90 dias (rebaseline §13, §19 Slice 3), que
depende inteiramente dessa mesma fórmula para simular cenários futuros hipotéticos — a própria seção
12 do `FINANCIAL_RULES.md` ("Projeções") já usa a fórmula `saldo final = máximo(0, saldo anterior +
resultado do mês)` para as três projeções (sem comissão / atraso conservador / mês esperado), que
são inerentemente PREVISTO, não REALIZADO.

**Evidência:** `app/services/financial_engine.py:100-159` (`settle_liquidity`) é chamado tanto por
`calculate_actual_snapshot` (`:162-204`, fato realizado/fechado) quanto por
`app/services/projection_engine.py:68-74` (`build_projection`, cenário hipotético). O rebaseline
proíbe a fabricação como **fato**; não proíbe a mesma matemática como **hipótese de projeção** — a
própria seção 3 do rebaseline distingue REALIZADO de PREVISTO e a seção 12 do `FINANCIAL_RULES.md`
mantém a fórmula para projeção sem contradição aparente com o rebaseline.

**Risco:** se o Slice 1 remover `settle_liquidity` inteiramente, a projeção perde a capacidade de
simular saldo futuro do Privilège, quebrando `build_forecast`/`GET /forecast` e todo o "Dashboard
patrimonial"/cenários de compra (`compare_purchase_scenarios`) que dependem dele — uma regressão
grave não mencionada explicitamente no Work Order.

**Alternativa proposta:** manter `settle_liquidity` como está, mas usá-la **exclusivamente** dentro
do escopo de projeção (`projection_engine.py`) daqui para frente. Para o snapshot **realizado**
(`calculate_actual_snapshot`), substituir a chamada por uma nova função
(`reconcile_actual_liquidity`, esboçada no §4) que só produz `liquidity_used`/`liquidity_deposit`
quando há evidência de movimento — um movimento de transferência importado/observado no extrato
bancário ou uma ação explicitamente confirmada pelo usuário/Assistente. **Correção de revisão
(2026-09-11):** `AccountBalanceObservation` sozinha não é evidência suficiente — ela prova a posição
soberana num instante, não a causa do delta; usá-la isoladamente para materializar
`liquidity_used`/`liquidity_deposit` seria permissivo demais frente ao rebaseline §4.3/§5.1. Uma
observação de saldo, isolada, apenas atualiza a posição soberana e pode expor
`reconciliation_divergence` (delta não explicado); na ausência de movimento importado/observado ou
ação confirmada, expõe `unexplained_operating_result` para revisão humana em vez de fabricar o
movimento.

**Trade-offs:** preserva 100% da funcionalidade de projeção sem alterar sua semântica (que já está
correta e não é alvo de nenhuma crítica do rebaseline); adiciona uma segunda função no motor, mas
sem duplicar a fórmula — `reconcile_actual_liquidity` **não recalcula** a matemática de liquidação,
apenas decide **se** ela pode ser aplicada como fato, dado a presença de evidência.

**Como validar:** novo teste de propriedade garantindo que `build_forecast`/projeção continua
idêntico ao comportamento atual (regressão zero), par a par com o novo teste do item #3 do §6
provando que o snapshot realizado nunca mais fabrica retirada sem evidência.

**Pendência:** este Technical Challenge não bloqueia a entrega do Slice 0 (é documental), mas
**deve ser resolvido/confirmado pelo engenheiro responsável antes do início da implementação do
Slice 1**, já que define a arquitetura central do slice mais fundamental da sequência.

### Technical Challenge #2 — reconciliação de comissão recebida (gap pré-existente, não introduzido por este slice)

**Observação, não bloqueio:** `Commission.received_date` (`app/models.py:290`) existe desde a
migração original mas nunca é lido por `app/api.py` ou `app/services/projection_engine.py`. Isso
significa que uma comissão já recebida continua sendo projetada como futura até alguém a marque
`status="cancelled"` manualmente — um risco latente de dupla contagem (comissão real + comissão
ainda projetada) que já existe hoje, independente do rebaseline. Recomendo tratá-lo explicitamente
no Slice 3 (§4), não como bug isolado fora de sequência, já que ambos mexem na mesma camada de
projeção.

---

## 9. Resumo para o engenheiro responsável

- Nenhum código de produção foi alterado neste PR; apenas este documento foi adicionado.
- O conflito mais profundo (liquidação automática do Privilège) está entranhado no motor
  determinístico, em INV-005/006/007 e em ~15 testes — não é superficial, e o Technical Challenge
  #1 precisa de uma decisão explícita antes do Slice 1 começar.
- A maior parte do restante do rebaseline (cartões, obrigações, Assistente, investimentos,
  navegação) é funcionalidade **nova**, não uma correção de comportamento incorreto — o sistema
  atual não "faz errado", ele simplesmente ainda não expõe esses conceitos.
- Recomendação de início: Slice 1, seguindo a alternativa proposta no Technical Challenge #1,
  seguido de Slice 2, respeitando o sequenciamento estrito 0→1→2→3→4→5→6→7→8 exigido pela
  governança do P0 #87 (correção de revisão de 2026-09-11 — a recomendação anterior de paralelizar
  o Slice 6 foi removida). Slice 6 (investimentos) é tecnicamente independente de 2–5 como nota
  arquitetural (§7), mas não deve ser adiantado nem executado em paralelo.
