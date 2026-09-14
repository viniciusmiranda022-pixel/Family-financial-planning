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

**Correção de revisão (2026-09-14, revisão de engenharia do PR #92, bloqueio 1):** a versão original
deste slice fazia `POST /assistant/execute` aceitar `typed_action`/`payload`/`path_params`/
`structured_interpretation` diretamente do cliente HTTP, sem vínculo verificável a uma chamada prévia
de `POST /assistant/interpret` -- permitindo, em tese, pular a desambiguação e executar uma ação
tipada com uma interpretação fabricada. Corrigido com uma tabela adicional,
`assistant_action_proposals` (migração `0018`):

```text
id UUID PK
household_id FK -> households.id
user_id FK -> users.id nullable
trace_id String
original_message Text
structured_interpretation JSONB nullable
typed_action String
payload JSONB
path_params JSONB
created_at DateTime
expires_at DateTime
consumed_at DateTime nullable
consumed_action_event_id FK -> assistant_action_events.id nullable
```

`POST /assistant/interpret` persiste uma linha aqui *somente* quando a proposta resolvida já pode
executar (`can_execute=True`) e devolve apenas o `id` gerado; `POST /assistant/execute` aceita só esse
`proposal_id` -- nunca mais um `typed_action`/`payload` que o cliente possa fabricar. Proposta de
uso único (`consumed_at`/`consumed_action_event_id`) e com validade de 30 minutos (`expires_at`). Ver
`docs/ARCHITECTURE.md` ("Assistente Financeiro operacional — typed actions") para o fluxo completo.

**Correção de revisão (2026-09-14, revisão de engenharia do PR #92, segunda rodada):** "proposta de
uso único" acima ainda não era verdade sob concorrência real -- a leitura da proposta em
`execute_typed_action` era um `SELECT` comum, e `consumed_at` só era gravado depois da mutação de
domínio, então duas execuções concorrentes do mesmo `proposal_id` podiam ambas observar `consumed_at
IS NULL` e ambas despachar a ação financeira (dupla mutação real, não apenas uma trilha duplicada).
Corrigido com `SELECT ... FOR UPDATE` (PostgreSQL) na leitura inicial, mantido por toda a transação
até o commit final -- ver `docs/ARCHITECTURE.md` e `docs/FINANCIAL_INVARIANTS.md` (INV-032) para o
mecanismo completo e a regressão de concorrência em PostgreSQL real que prova isso.

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

---

## 10. Status pós-implementação — Slice 1 e Slice 2 (2026-09-12)

Este documento permanece o registro histórico do plano do Slice 0; esta seção apenas anota o que
foi efetivamente resolvido nas implementações seguintes, sem reescrever a análise acima.

**Slice 1** (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_1.md`) resolveu o Technical Challenge #1
adotando a alternativa proposta (`reconcile_actual_liquidity` evidence-based; `settle_liquidity`
isolado para a projeção hipotética) — ver `docs/FINANCIAL_INVARIANTS.md` INV-023/INV-024.

**Slice 2** (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_2.md`) implementou o plano do §3.1/§3.2/§4
essencialmente como proposto, com um desvio deliberado registrado abaixo:

- `card_invoices` (migração `0015`), `Transaction.card_invoice_id`,
  `Transaction.refund_of_transaction_id` — exatamente como especificado no §3.1/§3.2.
- `app/services/card_invoice_lifecycle.py` (`get_or_sync_invoice`, `close_invoice`, `pay_invoice`,
  `invoice_divergence`, `link_refund`/`unlink_refund`) e os endpoints `POST /card-invoices/sync`,
  `POST /card-invoices/{id}/close`, `POST /card-invoices/{id}/pay`,
  `GET /card-invoices/{id}/divergence`, `GET /card-invoices`,
  `POST /transactions/{id}/link-refund`/`.../unlink-refund` — como especificado no §4.
- **Desvio deliberado do §4 (não um Technical Challenge — refinamento de implementação, não
  discordância normativa):** o §4 descreve `POST /card-invoices/{id}/pay` como "superset do atual
  `pay_card_invoice`", o que se manteve literalmente (o novo endpoint faz tudo que o antigo faz, e
  mais aceita valor parcial). Mas a linha 66 também sugeria que o teste
  `test_manual_payment_rejects_amount_outside_tolerance_partial_payment` do endpoint **legado**
  (`POST /card-payment-reconciliations/pay`, `pay_card_invoice`) precisaria ser "substituído por
  teste que prova o novo comportamento". Na implementação, esse endpoint legado **não foi
  alterado** e esse teste **não foi removido**: ele continua correto para o contrato que
  efetivamente testa. A razão é estrutural, não de política — `pay_card_invoice` opera sobre uma
  única `Transaction` de "pagamento recebido" (linha importada de PDF/CSV) com um único
  `linked_transaction_id` escalar, que não tem como representar múltiplos pagamentos parciais ao
  longo do tempo sem uma entidade de acumulação separada. `CardInvoice.paid_total` é exatamente
  essa entidade; o novo endpoint `POST /card-invoices/{id}/pay` é o caminho correto e completo para
  pagamento parcial, coexistindo com o antigo sem alterar seu comportamento nem duplicar sua
  política de correspondência banco↔fatura (reaproveita `register_transaction_duplicates`, a mesma
  construção de `ParsedTransaction`/`transaction_fingerprint`, e a mesma categoria/`excluded=True`).
  Nenhum teste existente foi alterado para este PR passar; a suíte legada permanece 100% verde.
- **Risco residual explícito (resolvido na rodada de revisão de engenharia de 2026-09-12,
  `BLOQUEIO DE MERGE`):** a entrega inicial deste Slice registrava INV-025 a INV-028
  (`docs/FINANCIAL_INVARIANTS.md`) como documentadas e testadas, mas ainda fora do
  `INVARIANT_REGISTRY`/Financial Integrity Engine. O engenheiro responsável bloqueou o merge exigindo
  a integração antes de aceitar o Slice como concluído. Corrigido: as quatro passaram a integrar
  `app/services/invariant_registry.py` e são avaliadas em tempo real por
  `app.api._run_card_invoice_integrity_checks` a cada `sync`/`close`/`pay`/`divergence`/
  `link-refund`, com `fail` abortando a requisição — ver `docs/FINANCIAL_INVARIANTS.md` e
  `docs/ARCHITECTURE.md` para o mecanismo completo.

**Rodada de revisão de engenharia (2026-09-12, `BLOQUEIO DE MERGE`) — demais itens corrigidos nesta
entrega, sem reabrir o plano acima:**

1. UI normativa "Conferir e pagar" (rebaseline §6.2/§6.3) implementada em
   `app/templates/index.html`/`app/static/app.js`, ao lado da UI legada existente.
2. Parcelamento (rebaseline §6.7) exposto por `GET /card-invoices/{id}/lines`, reaproveitando apenas
   `Transaction.amount`/`installment_current`/`installment_total` já confirmados.
3. Idempotência de pagamento: `pay_invoice` reconhece um retry exato da mesma requisição pelo mesmo
   `transaction_fingerprint` já usado pelo leg de conciliação, e devolve o lançamento existente em
   vez de duplicá-lo.
4. INV-025..INV-028 integradas ao Financial Integrity Engine (ver acima).
5. Rollback de `0015` deixou de ser destrutivo silencioso para `refund_of_transaction_id` — a
   migração agora recusa o downgrade quando existe algum vínculo de estorno confirmado.
6. Regressão adicional corrigida: `principal_carried_in` deixou de oscilar retroativamente depois
   que a própria fatura já recebeu um pagamento (`get_or_sync_invoice`'s `paid_total <= 0` guard) —
   ver `tests/test_card_invoice_lifecycle.py::test_principal_carried_in_does_not_oscillate_after_invoice_already_paid`.

## 11. Status pós-implementação — Slice 3 (2026-09-13)

**Slice 3** (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_3.md`) implementou o plano do §3.3/§4
essencialmente como proposto, sem coluna persistida nova para `financial_state` — computado na
camada de serialização, exatamente como recomendado:

- `app/services/financial_state.py` (constantes `REALIZADO`/`COMPROMETIDO`/`PREVISTO`) e
  `app/services/recurring_income.py` (`reconcile_recurring_income`) — módulos novos, ambos puros ou
  quase puros (o segundo só lê `Transaction`, nunca escreve).
- `_obligation_rows` ganhou `financial_state` computado (§4 do plano). **Desvio deliberado do §4
  (refinamento de implementação, não Technical Challenge):** o plano original sugeria mesclar
  `CardInvoice` e `_future_installments` na mesma resposta de `GET /obligations`. Investigação do
  frontend (`app/static/app.js`, tela "Contas a pagar") mostrou que essa tela **já** chama
  `GET /obligations` e `GET /card-invoices` em paralelo e renderiza cada um em sua própria tabela
  (comentário existente em `app/static/app.js`: "`GET /obligations` (unchanged, slice-1-era) e
  `GET /card-invoices`"). Mesclar `CardInvoice` na resposta de `/obligations` duplicaria essas faturas
  nas duas tabelas simultaneamente e exigiria retrabalho de frontend fora do escopo deste slice (que é
  UX/navegação, reservado ao Slice 5). Em vez de mesclar payloads HTTP, cada superfície ganhou seu
  próprio `financial_state` computado de forma independente e consistente:
  `serialize_card_invoice` (`app/services/card_invoice_lifecycle.py::invoice_financial_state`) e
  `GET /commissions`. Nenhuma tabela soma duas vezes a mesma linha porque nenhuma união de payload
  foi criada.
- **Achado adicional durante a implementação, corrigido no mesmo PR (não estava listado
  explicitamente no plano do Slice 0, mas está diretamente coberto pelo Work Order do Slice 3 e pelo
  rebaseline §7 — "atraso não vira gasto duplicado nem desaparece da projeção"):**
  `_forecast_obligations` bucketava cada ocorrência pela própria data de vencimento; uma obrigação
  vencida cujo mês já ficou no passado nunca era revisitada pelo cursor somente-para-frente de
  `build_projection` (que começa em `start_month`, sempre o próximo mês a partir de hoje) — o valor
  simplesmente nunca aparecia em nenhuma linha da projeção, uma forma de desaparecimento silencioso
  de dinheiro comprometido. O mesmo problema existia de forma mais grave para `CardInvoice`: uma
  fatura `closed`/`partially_paid` nunca entrava em `ForecastInput` de forma alguma — `/forecast`
  simplesmente não sabia que ela existia. Corrigido com `start_month` opcional em
  `_forecast_obligations` (compatibilidade total com quem não o passa — `tests/test_plan_workbook.py`
  continua com o mesmo resultado) e a nova `_forecast_card_invoices`, ambas dobrando (`clamp`) um
  vencimento já passado para dentro do próprio `start_month` em vez de deixá-lo em um mês que o
  cursor nunca visita.
- `ForecastInput` ganhou `card_invoices`/`monthly_salary_overrides` (ambos com default vazio — zero
  regressão para quem já constrói `ForecastInput` sem eles: `tests/test_finance.py`,
  `tests/test_purchase_scenario_comparison.py`, `tests/test_financial_invariants.py` continuam
  passando sem alteração). `app.services.projection_engine`/`projection_validator` replicam a mesma
  fórmula estendida em paralelo, preservando a garantia de INV-018 (motor e validador concordam).
  `PROJECTION_CALCULATION_VERSION` avançou de `2026.09.2` para `2026.10.1`.
- `reconcile_recurring_income` liga o crédito real ao `monthly_salary_net` PREVISTO do mês exatamente
  como especificado no §4, sem hardcodar nenhum nome de pessoa — `owner_label` é um filtro opcional.
- `Commission.received_date` passou a ser lido: `POST /commissions/{id}/receive` (novo, admin-only)
  marca a comissão como recebida; `_build_projection_gate_checks` a partir de então a exclui de
  `ForecastInput.commissions`. Fecha o Technical Challenge #2 do Slice 0 (§8 acima) — a lacuna era
  real e pré-existente, não introduzida por este slice.
- INV-029/INV-030 registradas em `app/services/invariant_registry.py` e avaliadas em tempo real por
  `GET /forecast`/`POST /monthly-closes/{period}/run` (`app.api._build_projection_gate_checks`), o
  mesmo ponto único que já avalia INV-005/006/018/022/023/024 — nunca um segundo mecanismo de
  avaliação.
- Nenhuma migration foi necessária: `Commission.received_date` já existe desde a migração `0001`;
  nenhuma coluna nova foi criada para `financial_state`.
- Testes novos: `tests/test_recurring_income_reconciliation.py` (unitário, puro),
  `tests/test_obligation_lifecycle_slice3.py` (unitário + HTTP, inclui a suíte funcional completa de
  `pay_obligation`/`unpay_obligation` que faltava — plano §6 item 11),
  `tests/test_commission_lifecycle_slice3.py` (HTTP), mais casos novos em
  `tests/test_financial_invariants.py` e uma propriedade nova em
  `tests/test_property_based_financial_rules.py`. Suíte completa (727 testes, incluindo
  `tests/test_postgresql_integration.py`), `ruff check` e migrations Alembic (SQLite e PostgreSQL 16
  reais) verificados verdes localmente antes da entrega.
- **Pendência/decisão consciente fora de escopo:** a restrição de `pay_obligation` a
  `recurrence_months == 0`/`occurrence_count == 1` (linha 70 do §1.2 acima, "decisão de design em
  aberto") não foi alterada. O Work Order do Slice 3 não exige pagar uma ocorrência específica de uma
  obrigação recorrente diretamente; o contorno documentado (cadastrar cada parcela como obrigação
  individual) continua funcionando e já é coberto pela nova suíte funcional. Mudar esse contrato
  criaria uma decisão de design nova (qual ocorrência? como marcar as demais?) fora do escopo restrito
  deste slice — registrado aqui para o Slice 4/5 avaliar se necessário.

### 11.1 Round 2 — correções da revisão de engenharia (PR #91, 2026-09-14)

A revisão do engenheiro responsável bloqueou o head `1cf165c` (`BLOCKING CHANGES REQUIRED`) com
quatro pontos materiais. Nenhum foi contestado por Technical Challenge — todos eram violações reais
e concretas, com evidência de código, confirmadas ao investigar:

1. **Comissão pendente inflava a projeção automaticamente**, violando o rebaseline §8.3 de forma
   direta: INV-029 (Round 1) só protegia uma comissão *já recebida* de reentrar; nada impedia a
   inclusão automática de toda comissão pendente antes disso. Corrigido:
   `_build_projection_gate_checks` nunca mais deriva `ForecastInput.commissions` de `Commission`
   — sempre uma tupla vazia. Nova **INV-031** prova isso contra a saída real de `build_projection`.
   `GET /commissions` mantém `financial_state` como rótulo puramente descritivo.
2. **`reconcile_recurring_income` podia promover renda errada por coincidência de valor** — valor +
   competência, sem exigir nenhuma evidência de identidade, e um desempate silencioso quando havia
   mais de um candidato. Corrigido: exige `owner_label` explícito ou marcador de descrição de folha
   de pagamento; múltiplos candidatos identificados mantêm PREVISTO com `ambiguous=True` exposto em
   `GET /forecast` (`salary_reconciliation_ambiguous`), em vez de escolher um por desempate.
3. **`CardInvoice.open` rotulado REALIZADO** conflava o estado da fatura-como-obrigação com o estado
   das compras (já REALIZADO) dentro dela. Corrigido: `open` passa a PREVISTO (total ainda não
   consolidado, rebaseline §6.2); `closed`/`partially_paid` continuam COMPROMETIDO; `paid` continua
   REALIZADO.
4. **Dashboard/relatório sem prova de paridade de ponta a ponta.** `GET /dashboard` ganhou
   `noncanonical.commitments` (COMPROMETIDO, mesmas fontes de `GET /obligations`/`GET /forecast`,
   respondendo rebaseline §44); `GET /reports` permanece REALIZADO-only por construção (nunca lê
   `Obligation`/`CardInvoice` diretamente) — regressão coberta por teste dedicado provando que um
   compromisso pendente não vaza para `spending`/`cash_out`.

Nenhuma migration nova; nenhum dado real alterado. `FINANCIAL_RULES_VERSION` avançou de `2026.10.2`
para `2026.10.3`. Ver `docs/FINANCIAL_INVARIANTS.md` (Controle de mudança, Slice 3 Round 2) e
`docs/ARCHITECTURE.md` ("Obrigações e projeção REALIZADO/COMPROMETIDO/PREVISTO") para o detalhamento
técnico completo.

## 12. Status pós-implementação — Slice 4 (2026-09-14)

**Slice 4** (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_4.md`) implementou o plano do §3.5/§3.6/§4
essencialmente como proposto:

- Sidecar ganhou o quarto contrato consultivo `POST /v1/interpret`
  (`advisor/server.mjs`/`advisor/interpret-schema.json`), mesmo padrão estrutural sem campo de
  autoridade de `/v1/classify`/`/v1/analyze`/`/v1/audit`.
- `app/services/assistant_interpreter.py` (`interpret_message`/`StructuredInterpretation`) espelha
  `app.services.codex_audit` -- fail-safe, lê só cinco chaves da resposta do sidecar.
- `app/services/assistant_sanitizer.py` (`build_interpret_payload`) espelha `audit_sanitizer` --
  envia só mensagem/histórico/vocabulário de intenções, nunca um dado financeiro do household.
- `app/services/assistant_actions.py`: `build_typed_action_proposal` (resolução determinística de
  indícios contra dados reais, nunca uma inferência silenciosa), `persist_action_proposal` (única
  escrita de `POST /assistant/interpret`, só quando a proposta já pode executar) e
  `execute_typed_action`/`undo_assistant_action` (despacho, com `commit=False`, para o *corpo* dos
  endpoints já existentes e revisados dos Slices 1-3 -- `_create_manual_transaction_impl`,
  `_create_manual_transfer_impl`, `_pay_obligation_impl`, `_pay_card_invoice_lifecycle_impl`,
  `_link_refund_transaction_impl`, `_unpay_obligation_impl`, `_unlink_refund_transaction_impl`,
  `_delete_manual_transaction_impl`; cada rota HTTP pública correspondente é hoje um wrapper fino
  chamando o `_impl` com `commit=True`, sem mudança de contrato).
- Novos endpoints: `POST /assistant/interpret`, `POST /assistant/execute`, `GET /assistant/actions`,
  `POST /assistant/actions/{id}/undo`.
- `app/services/entry_type_templates.py` + `POST /entry-type-templates`,
  `POST /entry-type-templates/{id}/activate`, `POST /entry-type-templates/{id}/deactivate`,
  `GET /entry-type-templates` -- ciclo de vida idêntico a `classification_rules` (§3.6).
- Migrations `0016` (`assistant_action_events`, FK 1:1 com `audit_events`), `0017`
  (`entry_type_templates`) e `0018` (`assistant_action_proposals`, §3.5 "Correção de revisão
  2026-09-14") -- todas puramente aditivas. `0017`/`0018` têm downgrade seguro e incondicional
  (nenhuma é fato financeiro irrecuperável); `0016` teve seu downgrade corrigido na mesma revisão
  para recusar (não apagar silenciosamente) quando existir ao menos uma linha -- a alegação original
  de que a tabela seria sempre reconstruível a partir de `audit_events` era falsa (`original_message`/
  `structured_interpretation`/`disambiguation_qa`/bookkeeping de undo só existem ali), mesmo padrão
  de guarda já usado por `refund_of_transaction_id` na migração `0015`.
- INV-032 (ação do Assistente sempre auditável) e INV-033 (undo nunca apaga histórico) registradas
  em `app/services/invariant_registry.py`/`docs/FINANCIAL_INVARIANTS.md`. `FINANCIAL_RULES_VERSION`
  avançou de `2026.10.3` para `2026.10.4` (nenhuma regra financeira anterior foi alterada; o
  conjunto de invariantes deste contrato mudou).

**Desvios deliberados do §4 (refinamento de implementação, não Technical Challenge):**

1. `POST /liquidity/confirm-movement` e as typed actions de patrimônio
   (`UPDATE_ASSET_VALUE`/`REGISTER_ASSET_CONTRIBUTION`, `POST /investments/{id}/valuations`) **não
   foram implementadas neste slice.** O modelo `Investment`/`InvestmentValuation` é escopo do Slice 6
   (ainda não executado) -- expor uma typed action apontando para um endpoint que não existe
   antecipararia funcionalidade fora de sequência (Work Order item 13, "não antecipar Slices
   futuros"). O `funding_source="privilege"` para saída em caixa/cheque (rebaseline §4.4) já é
   suportado diretamente por `ManualTransactionRequest`/`create_manual_transaction`
   (Slice 1, `FAMILY_FINANCE_PRIVILEGE_FUNDING_V15`) e por `ObligationPaymentRequest`/
   `pay_obligation` -- não há necessidade de um endpoint de confirmação de movimento separado para
   os fluxos deste slice; a pergunta "Conta Corrente ou Privilège?" é feita e resolvida inteiramente
   dentro de `build_typed_action_proposal`/`execute_typed_action`.
2. `register_refund` espera que o lançamento de estorno já exista (importado ou criado
   manualmente/via `create_expense` com `movement_type="refund"`) e só confirma o vínculo -- não é
   uma ação composta "criar + vincular". Mantém cada typed action como um despacho de exatamente um
   endpoint determinístico, evitando uma transação de dois efeitos dentro do dispatcher.
3. `POST /captures/{id}/confirm` não foi adicionado como typed action: o fluxo de confirmação de
   captura já tem sua própria tela de revisão editável (Central Inteligente); o Work Order não
   exige expor esse fluxo especificamente através do Assistente neste slice, e os seis fluxos
   obrigatórios (entrada, saída, transferência interna, obrigação paga, compra/cartão via
   `create_expense`, estorno) já estão cobertos.
4. Nenhuma mudança de frontend/navegação: o Slice 5 é o dono da UX/navegação alvo (rebaseline §19).
   Este slice entrega o contrato de API completo e testado (interpretação, proposta, execução,
   auditoria, undo, aprendizado), pronto para a tela "Assistente Financeiro" consumir.

**Testes:** `tests/test_assistant_slice4.py` (proposta determinística + suíte funcional HTTP de
execute/undo/idempotência/isolamento de household), `tests/test_assistant_interpreter.py`,
`tests/test_assistant_sanitizer.py`, `tests/test_entry_type_templates.py`, casos novos em
`tests/test_financial_invariants.py` (INV-032/INV-033) e `tests/test_role_based_authorization.py`
(todas as novas rotas mutantes rejeitadas para o perfil consulta). Migrations validadas offline
(SQLite) e contra PostgreSQL 16 real (`tests/test_postgresql_integration.py`,
`tests/test_migrations.py`). Suíte completa (769 testes SQLite + 11 PostgreSQL), `ruff check .` e
`node --test advisor/test/*.test.mjs` verificados verdes localmente antes da entrega.

**Pendência/decisão consciente fora de escopo:** o `advisor/Dockerfile` precisou de um ajuste
mínimo (adicionar `interpret-schema.json` à lista `COPY`) para que o novo contrato funcione na
imagem de produção -- corrigido neste PR. O build real das imagens Docker (`docker-build`,
`advisor` Docker build, Docker Compose validation) não pôde ser executado neste ambiente de execução
(daemon Docker indisponível na sandbox); `node --check`/`npm test` no sidecar e a suíte Python
completa foram verificados como proxy, mas o engenheiro responsável deve confirmar os gates Docker
no CI real antes do merge.

## 13. Status pós-implementação — Slice 6 (2026-09-14)

**Slice 6** (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_6.md`) implementou o plano do §3.4
essencialmente como proposto, com duas extensões deliberadas sobre o esboço original:

- **Inventário confirmado greenfield.** Busca completa por "Studio"/`Investment`/`Asset`/`NetWorth`
  no repositório antes de qualquer alteração: zero ocorrências fora de `docs/` (todas em planejamento
  normativo). Nenhum modelo, migration, endpoint ou dado legado de patrimônio/investimentos existia
  -- confirmado também pelas notas já existentes em `app/services/assistant_actions.py:89-92` e
  `docs/ARCHITECTURE.md` ("o modelo `Investment` ainda não existe"). Não há, portanto, migração de
  dados reais a fazer para o Studio especificamente; a migração `0019` é puramente aditiva.
- **Modelo:** `Investment` (`app/models.py`) -- estado atual (`historical_cost`/`current_value`/
  `expected_receivable_value`/`last_updated_at`/`expected_receipt_date`/`notes`/`active`) --
  e `InvestmentValuation` -- histórico imutável, mesmo padrão de `AccountBalanceObservation`.
  **Extensão deliberada #1:** cada linha de `investment_valuations` grava o snapshot *completo*
  pós-evento (`historical_cost`/`current_value`/`expected_receivable_value`), não somente os campos
  que mudaram -- o esboço do §3.4 listava só `current_value`/`expected_receivable_value`; a extensão
  garante que o histórico sozinho reconstrua o estado do ativo em qualquer ponto no tempo.
  **Extensão deliberada #2:** `invalidated_at`/`invalidated_by`/`invalidation_reason` (ausentes no
  esboço original) foram adicionados para suportar undo sem nunca apagar uma linha -- exigido pelo
  próprio Work Order ("não apagar histórico") e pelo mesmo padrão que `AccountBalanceObservation` já
  usa para invalidar uma observação superada.
- **Migração `0019`**, puramente aditiva (`investments`, `investment_valuations`). Downgrade
  guardado como o de `0016`: recusa (levanta `RuntimeError`, não altera nada) quando qualquer uma das
  duas tabelas tem ao menos uma linha -- `investment_valuations` é o único registro durável de cada
  evento de avaliação/aporte, e mesmo um `investments` isolado sem histórico ainda é um fato
  financeiro real.
- **Invariante central estrutural por construção.** `app.services.investments.net_worth_summary` é a
  única função que soma `current_value` de ativos ativos de um household -- `historical_cost`/
  `expected_receivable_value` nunca entram na soma, porque não há nenhuma outra função que calcule
  esse total. `GET /investments` e `GET /dashboard` (`noncanonical.net_worth`/
  `noncanonical.investments`) chamam exatamente essa função, provado por
  `test_dashboard_regression_never_sums_historical_or_projected_into_net_worth`. INV-034
  (`app/services/invariant_registry.py`/`docs/FINANCIAL_INVARIANTS.md`) formaliza o mesmo contrato
  como invariante executável para uma futura validação independente do Codex/Financial Integrity
  Engine -- `FINANCIAL_RULES_VERSION` avançou de `2026.10.4` para `2026.10.5` (nenhuma regra anterior
  foi alterada; o conjunto de invariantes mudou).
- **Endpoints novos:** `POST /investments`, `GET /investments`, `GET /investments/{id}/valuations`,
  `POST /investments/{id}/valuations`, `POST /investments/{id}/contributions`,
  `POST /investments/{id}/valuations/{valuation_id}/undo`. Todos admin-only
  (`_require_admin`), todos household-scoped por filtro explícito de query, seguindo exatamente a
  convenção já usada em `Account`/`Obligation`.
- **Aportes nunca fabricam origem de caixa.** `_register_investment_contribution_impl` só aumenta
  `historical_cost`; `funding_transaction_id`, quando informado, apenas vincula uma
  `Transaction` `movement_type="investment"`/"Transferência patrimonial" já existente (criada pelo
  fluxo manual já suportado desde o Slice 1) -- nunca a cria. Mesmo padrão "vincular, nunca criar e
  vincular numa ação composta" que `register_refund` já estabeleceu no Slice 4.
- **Assistente Financeiro:** `update_asset_value`/`register_asset_contribution` entram em
  `TYPED_ACTIONS`, despachando para os mesmos `_update_investment_value_impl`/
  `_register_investment_contribution_impl` que os endpoints manuais chamam, com o mesmo contrato de
  atomicidade (`commit=False` + `domain_audit_sink`) de `pay_obligation`. Novo campo extraído
  `asset_value_kind_hint` (`current_value`/`expected_receivable_value`/`unspecified`) desambigua qual
  valor um "Hoje acho que o Studio vale 35 mil" está atualizando -- um hint ausente/`unspecified`
  sempre pergunta, nunca assume. `register_asset_contribution` exige exatamente uma transação de
  financiamento "Transferência patrimonial" ainda não vinculada e compatível em valor para resolver
  sem pergunta -- zero ou múltiplas candidatas devolvem a pergunta obrigatória do Work Order ("De onde
  saiu esse aporte?"), nunca uma inferência.
- **Undo sem apagar histórico.** `_undo_investment_valuation_impl` invalida (nunca apaga) a
  `InvestmentValuation` alvo e restaura o `Investment` ao estado exato anterior; recusa (409) quando
  já existe uma avaliação mais recente ainda válida -- mesma guarda que
  `_undo_obligation_privilege_funding` já aplica a um pagamento de obrigação financiado pelo
  Privilège. `target_entity_ids` do `AssistantActionEvent` grava `[investment_id, valuation_id]` (não
  só o id do investimento), então o undo sempre reverte o evento exato que a ação registrou, nunca "a
  avaliação mais recente" no momento do undo.
- **Dashboard/UX:** painel "Patrimônio" dentro de `#view-dashboard` (`app/templates/index.html`/
  `app/static/app.js`), sem item novo no menu principal -- consome
  `noncanonical.net_worth`/`noncanonical.investments` de `GET /dashboard` verbatim, sem segundo
  cálculo no frontend. Ações de edição via `window.prompt`, mesmo padrão já usado para "motivo do
  undo" em outras telas -- UI mínima suficiente para o slice, não uma tela dedicada.

**Desvios deliberados (refinamento de implementação, não Technical Challenge):** ver as duas
extensões acima (snapshot completo + invalidação); nenhum endpoint de exclusão/desativação de
investimento foi adicionado (não exigido pelo Work Order); `Investment`/`InvestmentValuation` foram
deliberadamente excluídos de `FINANCIAL_REVISION_MODELS` (patrimônio não é entrada do
`FinancialSnapshot`/fechamento mensal neste slice -- ver o comentário em `app/models.py`).

**Testes:** `tests/test_investments_slice6.py` (20 testes -- cálculos derivados, `net_worth_summary`,
suíte funcional HTTP de criação/valuation/aporte/undo/isolamento de household, proposta determinística
do Assistente para os dois novos typed actions, execução/undo/idempotência via
`/assistant/execute`/`/assistant/actions/{id}/undo`, regressão de Dashboard), `tests/test_migrations.py`
(guarda de downgrade da migração `0019`, tabelas/cabeça de revisão atualizadas), 4 novos casos em
`tests/test_role_based_authorization.py` (as quatro rotas mutantes de investimento rejeitadas para o
perfil consulta), 1 novo caso em `tests/test_financial_invariants.py` (INV-034, pass/fail/zero-cost).
Suíte completa verificada localmente: **834 testes** (822 SQLite + 12 PostgreSQL 16 real via
`tests/test_postgresql_integration.py`, incluindo os dois testes de migração que verificam a cabeça
`0019` upgrade/downgrade contra PostgreSQL de verdade), `ruff check .` limpo, `node --check` nos
arquivos do sidecar e em `app/static/app.js`, e `node --test test/*.test.mjs` do advisor (37 testes)
todos verdes. Build das imagens Docker (`docker-build`, `advisor` Docker build, Docker Compose
validation) não pôde ser executado até o fim neste ambiente de execução (rede da sandbox bloqueia
`docker.io` para baixar a imagem base `python:3.12-slim`, mesma limitação de rede já registrada no
Slice 4) -- o engenheiro responsável deve confirmar esses três gates no CI real antes do merge.
