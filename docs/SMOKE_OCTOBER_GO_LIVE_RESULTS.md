# Smoke Real de Go-Live -- October Go-Live Slice 8

Gerado em: 2026-09-14T23:13:30.173927+00:00

Dados sintéticos (rebaseline/Work Order permitem "datasets sintéticos equivalentes" para este E2E automatizado). O smoke manual com dados reais da família é um passo humano separado, fora do alcance desta sessão.

Resultado: TODOS OS PASSOS PASSARAM

## Passo 1 -- Login e navegação principal [✅ OK]

- **Entrada:** Cadastro de família + login + MFA
- **Ação:** POST /api/auth/setup, enroll MFA, verificar #main-nav
- **Resultado esperado:** 201 na criação; MFA concluído; navegação alvo presente
- **Resultado observado:** setup=201; nav_ok=True

## Passo 2 -- Lançamento manual de entrada [✅ OK]

- **Entrada:** Salário R$4.500,00 em 2026-09-05 na Conta Corrente
- **Ação:** POST /api/transactions (movement_type=income)
- **Resultado esperado:** 201
- **Resultado observado:** status=201

## Passo 3 -- Lançamento manual de saída [✅ OK]

- **Entrada:** Farmácia R$150,00 em 2026-09-06 na Conta Corrente
- **Ação:** POST /api/transactions (movement_type=expense)
- **Resultado esperado:** 201
- **Resultado observado:** status=201

## Passo 4 -- Assistente: frase completa executa ação tipada [✅ OK]

- **Entrada:** "Paguei a parcela da chácara" (Codex resolve conta/data; smoke stuba só o transporte do sidecar)
- **Ação:** POST /api/assistant/interpret -> POST /api/assistant/execute
- **Resultado esperado:** interpret 200; proposal.can_execute=True; execute 201; obrigação paga
- **Resultado observado:** interpret=200; can_execute=True; execute=201
- **Evidência:** {'action_id': '0cd5e031-10dd-4624-b758-bd87fee66069'}

## Passo 5 -- Assistente: frase ambígua pede informação [✅ OK]

- **Entrada:** "Gastei 300 de combustível" (sem conta/cartão)
- **Ação:** POST /api/assistant/interpret
- **Resultado esperado:** 200; can_execute=False; pergunta de esclarecimento; nada persistido (proposal_id nulo)
- **Resultado observado:** interpret=200; can_execute=False; proposal_id=None; question='Preciso de mais informação: valor, conta/cartão e o que foi.'

## Passo 6 -- Criação/consulta de obrigação [✅ OK]

- **Entrada:** Parcela chácara R$500,00, vencimento 2026-08-10
- **Ação:** POST /api/obligations; GET /api/obligations
- **Resultado esperado:** 201 na criação; COMPROMETIDO na consulta
- **Resultado observado:** create=201; financial_state=COMPROMETIDO

## Passo 7 -- Compra em cartão e consulta da fatura [✅ OK]

- **Entrada:** Compra R$150,00 no cartão em 2026-08-05 (competência 2026-08)
- **Ação:** POST /api/transactions; POST /api/card-invoices/sync
- **Resultado esperado:** compra 201; fatura fechada com computed_total=150.00, sem reduzir banco
- **Resultado observado:** purchase=201; invoice_status=closed; computed_total=150.00

## Passo 8 -- Pagamento de fatura sem duplicar gasto [✅ OK]

- **Entrada:** Pagamento integral da fatura de agosto, R$150,00
- **Ação:** POST /api/card-invoices/{id}/pay
- **Resultado esperado:** 201; invoice status=paid; apenas uma despesa da compra original (nunca uma segunda)
- **Resultado observado:** pay=201; invoice_status=paid; expense_rows=1

## Passo 9 -- Transferência interna Conta Corrente <-> Privilège [✅ OK]

- **Entrada:** Resgate R$500,00 do Reserva DI para Conta Corrente
- **Ação:** POST /api/transfers; GET /api/reports antes/depois (total_spending, total_cash_in, internal_transfers_total)
- **Resultado esperado:** 201; total_spending e total_cash_in inalterados (transferência não vira renda/despesa); internal_transfers_total sobe R$1.000,00 (soma bruta das duas pernas de R$500,00, não valor econômico)
- **Resultado observado:** transfer=201; total_spending 150.0 -> 150.0; total_cash_in 4500.0 -> 4500.0; internal_transfers_total 0.0 -> 1000.0

## Passo 10 -- Saldo confirmado e reconciliação [✅ OK]

- **Entrada:** Observação confirmada R$12.000,00 em 2026-09-12 no Reserva DI
- **Ação:** POST /api/account-balances; GET /api/dashboard
- **Resultado esperado:** 201; dashboard publica o saldo confirmado como soberano
- **Resultado observado:** obs=201; observed_value={'value': 12000.0, 'as_of_date': '2026-09-12', 'account_id': '972b8d79-9877-44a5-8438-0f4247677d98', 'source': 'account_balance_observation', 'freshness': 'point_in_time', 'certified_by': 'manual_confirmed', 'trusted': True}

## Passo 11 -- Patrimônio/Studio [✅ OK]

- **Entrada:** Studio: investido 30.000, hoje 35.000, previsto 45.000
- **Ação:** POST /api/investments; GET /api/dashboard (patrimony)
- **Resultado esperado:** 201; patrimônio = caixa confirmado (12.000) + valor de hoje (35.000) = 47.000, nunca soma dos três
- **Resultado observado:** investment=201; patrimony_value=47000.0

## Passo 12 -- Gastos & Economia e Relatórios [✅ OK]

- **Entrada:** Mesmo período (2026-09) já populado pelos passos anteriores
- **Ação:** GET /api/reports; GET /api/spending-economy
- **Resultado esperado:** 200 em ambos; total_spending concorda entre as duas superfícies
- **Resultado observado:** report_total=150.0; economy_total=150.0

## Passo 13 -- Undo de ação do Assistente [✅ OK]

- **Entrada:** Desfazer a ação 0cd5e031-10dd-4624-b758-bd87fee66069 (pagamento da chácara pelo Assistente)
- **Ação:** POST /api/assistant/actions/{id}/undo
- **Resultado esperado:** 200; obrigação volta a pending; trilha de auditoria preservada
- **Resultado observado:** undo=200; obligation_status=pending

## Passo 14 -- Provável duplicidade sem decisão destrutiva automática [✅ OK]

- **Entrada:** "Gastei 300 no posto de novo" (mesmo valor/categoria/data de um lançamento existente)
- **Ação:** POST /api/assistant/interpret (sem duplicate_resolution)
- **Resultado esperado:** 200; can_execute=False; candidate_kind=possible_duplicate; proposal_id nulo; nada é criado/apagado automaticamente
- **Resultado observado:** existing=201; interpret=200; can_execute=False; kind=possible_duplicate; fuel_rows=1
