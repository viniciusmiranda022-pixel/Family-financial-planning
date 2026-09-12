# Contrato de invariantes financeiros

**Versão das regras:** `2026.10.1`
**Status:** normativo

**Controle de mudança (2026-10, October Go-Live Slice 1, P0 #87):** INV-005,
INV-006 e INV-007 foram rescopadas de "fato realizado" para "PROJEÇÃO/cenário
hipotético apenas" — `docs/OCTOBER_GO_LIVE_REBASELINE.md` §4.3 proíbe inferir
resgate/aplicação REALIZADO do Privilège a partir do sinal do resultado
operacional, e a liquidação automática que essas três regras validavam
(`settle_liquidity`/`calculate_liquidity_transition`) permanece correta
apenas como matemática de projeção hipotética. INV-023 e INV-024 foram
adicionadas para cobrir a semântica REALIZADO equivalente: resgate/aplicação
só é reconhecido com evidência de movimento real, e saldo confirmado é
soberano com divergência sinalizada. Nenhum snapshot histórico é reescrito;
cada um mantém seu próprio `calculation_version`. Ver
`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §8 (Technical Challenge #1) e
`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_1.md`.

**Controle de mudança (2026-10, October Go-Live Slice 2, P0 #87):** INV-025 a
INV-028 foram adicionadas para o ciclo de vida de fatura de cartão
(`CardInvoice` — `app/services/card_invoice_lifecycle.py`, migração `0015`):
pagamento parcial/integral nunca duplica gasto, principal carregado nunca é
uma nova despesa, estorno neutraliza sem apagar histórico (inclusive quando
cai em ciclo posterior a uma fatura já paga) e divergência de fatura nunca é
ajustada silenciosamente. Nenhuma calcula um valor de snapshot/projeção
existente nem altera `INVARIANT_REGISTRY` (`app/services/invariant_
registry.py` permanece em INV-001..INV-024) ou o `FINANCIAL_RULES_VERSION`
de cálculo — cada uma documenta explicitamente que ainda não está integrada
ao Financial Integrity Engine e aponta para o teste dedicado que a cobre
nesta fase. Ver `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_2.md`.
**Implementação executável:** `app/services/invariant_registry.py` (INV-001
a INV-024; INV-025 a INV-028 ainda pendentes de integração -- ver acima).

Este documento define as condições que precisam permanecer verdadeiras em importações,
fechamentos, projeções, snapshots, dashboards, relatórios e contextos enviados ao Advisor.
Os identificadores `INV-XXX` são permanentes: uma mudança semântica deve alterar a versão das
regras, nunca reaproveitar silenciosamente um identificador com outro significado.

O PostgreSQL, as regras aqui documentadas, o Financial Engine determinístico e o Financial
Integrity Engine formam a fonte oficial. O Codex pode encontrar e explicar uma possível
incoerência, mas não pode transformar `fail` em `pass`, modificar valores ou substituir uma regra.

## Convenções de avaliação

Cada execução retorna, no mínimo:

```json
{
  "invariant_id": "INV-002",
  "financial_rules_version": "2026.09.1",
  "status": "pass|fail|warning|unknown",
  "severity": "info|warning|review|critical|block",
  "scope": "transaction|document|period|projection|report|system",
  "entity_type": "transaction",
  "entity_id": "identificador",
  "period": "2026-08",
  "trace_id": "identificador-da-execução",
  "message": "explicação objetiva",
  "expected": "resultado esperado",
  "actual": "resultado encontrado",
  "difference": null,
  "metadata": {}
}
```

- `pass`: a regra foi avaliada e satisfeita.
- `fail`: a regra foi avaliada e violada.
- `warning`: a condição merece atenção, sem prova suficiente de violação.
- `unknown`: faltam fatos ou o formato não permite avaliação segura. `unknown` nunca equivale a
  `pass`.
- Valores monetários são arredondados em cada evento econômico com `ROUND_HALF_UP` e duas casas.
- Comparações entre motores e canais usam tolerância padrão de `R$ 0,01`, declarada no contexto.
- Uma regra só é executada sobre entidades às quais ela se aplica. Ausência de dados obrigatórios
  produz `unknown`; o motor não fabrica fatos para completar a avaliação.
- O piso de segurança do Privilège DI é indicador e alerta. Ele não é subtraído da liquidez
  disponível para cobrir déficit.
- Confiança de duplicidade: `0,00–0,59` baixa; `0,60–0,84` provável; `0,85–1,00` forte.

## INV-001 — Transferência interna

**Título:** Transferência interna sem efeito operacional
**Descrição:** Transferência entre contas pertencentes à família movimenta duas posições de caixa,
mas não cria receita, despesa, consumo de teto ou resultado operacional.
**Motivação:** Evitar que origem e destino sejam tratados como dois eventos econômicos e inflem os
totais.
**Entradas:** efeito em receita, despesa, consumo, resultado operacional e caixa familiar líquido.
**Resultado esperado:** todos os efeitos agregados são `R$ 0,00`; os movimentos de origem e destino
permanecem rastreáveis.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_internal_transfer_never_changes_operating_result`.

## INV-002 — Pagamento de cartão

**Título:** Pagamento de fatura não é nova despesa
**Descrição:** O pagamento liquida uma obrigação. As compras individuais são as despesas econômicas.
**Motivação:** Impedir dupla contabilização de compras mais pagamento da mesma fatura.
**Entradas:** efeitos do pagamento em receita, despesa, consumo e resultado operacional.
**Resultado esperado:** todos os efeitos operacionais são `R$ 0,00`; a saída de caixa permanece na
conciliação.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_card_payment_is_reconciliation_only`.

## INV-003 — Aplicações

**Título:** Aplicação é movimento patrimonial
**Descrição:** Aplicação no Privilège DI ou em outro investimento não é despesa operacional e não
consome o teto.
**Motivação:** Separar patrimônio, liquidez, consumo e resultado operacional.
**Entradas:** efeitos em receita, despesa, consumo, resultado operacional e patrimônio líquido.
**Resultado esperado:** efeitos operacionais e patrimonial líquido iguais a zero; apenas a composição
do patrimônio muda.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_application_is_patrimonial_movement`.

## INV-004 — Resgates

**Título:** Resgate não é receita operacional
**Descrição:** Resgate converte investimento em caixa e não aumenta renda familiar.
**Motivação:** Evitar renda artificialmente inflada e decisões baseadas em liquidez já existente.
**Entradas:** efeitos em receita, despesa, consumo, resultado operacional e patrimônio líquido.
**Resultado esperado:** efeitos operacionais e patrimonial líquido iguais a zero; a liquidez muda de
posição.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_redemption_is_not_operating_income`.

## INV-005 — Privilège não negativo (PROJEÇÃO)

**Escopo:** PROJEÇÃO/cenário hipotético apenas (`settle_liquidity_projection`/
`build_projection`). Nunca se aplica a um período REALIZADO — ver INV-023/INV-024.
**Título:** Saldo de liquidez nunca negativo
**Descrição:** Se o déficit projetado exceder o saldo disponível, o saldo final é zero e a diferença
é déficit sem cobertura.
**Motivação:** Um saldo patrimonial negativo fictício esconde dívida ou falta de recursos, mesmo em
projeção.
**Entradas:** saldo inicial, resultado mensal, saldo final, liquidez utilizada e déficit sem cobertura.
**Resultado esperado:**
`saldo_final = max(0, saldo_inicial + resultado_mensal)` e
`deficit_sem_cobertura = max(0, -(saldo_inicial + resultado_mensal))`.
**Severidade se violado:** `BLOCK`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_liquidity_transition_never_produces_negative_balance`.

## INV-006 — Déficit consome liquidez (PROJEÇÃO)

**Escopo:** PROJEÇÃO/cenário hipotético apenas. Nunca se aplica a um período REALIZADO — ver
INV-023/INV-024.
**Título:** Resultado negativo usa a conta central
**Descrição:** Resultado mensal projetado negativo consome o Privilège DI disponível antes de
produzir déficit sem cobertura, inclusive quando rompe o piso.
**Motivação:** O Privilège DI é o caixa operacional real e diariamente movimentado pela família, e a
projeção precisa modelar esse comportamento hipoteticamente.
**Entradas:** saldo inicial, resultado mensal, retirada utilizada, saldo final e déficit sem cobertura.
**Resultado esperado:**
`retirada = min(saldo_inicial, abs(min(resultado_mensal, 0)))`.
**Severidade se violado:** `BLOCK`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_deficit_uses_all_available_liquidity_before_becoming_uncovered`.

## INV-007 — Piso não é dinheiro bloqueado (PROJEÇÃO)

**Escopo:** PROJEÇÃO/cenário hipotético apenas.
**Título:** Piso de segurança é referência
**Descrição:** O piso gera indicador e alerta, mas não impede resgate projetado necessário para
cobrir déficit hipotético.
**Motivação:** Não exibir simultaneamente um saldo “protegido” e uma dívida que esse próprio saldo
deveria ter reduzido.
**Entradas:** saldo inicial, resultado mensal, piso, retirada utilizada, saldo final e déficit sem
cobertura.
**Resultado esperado:** a retirada canônica independe do piso; cruzar o piso altera somente alertas e
confiança.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_safety_floor_never_blocks_real_deficit_coverage`.

## INV-008 — Comissão por competência

**Título:** Comissão somente no período do cenário
**Descrição:** A comissão existe apenas no período definido para o cenário analisado.
**Motivação:** Evitar antecipação de receita e comparação entre períodos incompatíveis.
**Entradas:** período da comissão, período do cenário, indicador de que o cenário considera comissões
e indicador de inclusão em receita.
**Resultado esperado:** inclusão somente quando o cenário considerar comissões e os períodos forem
iguais. O cenário “sem comissão” permanece válido sem incluir o evento.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_commission_is_included_only_in_its_scenario_period`.

## INV-009 — Imposto PJ individual

**Título:** Imposto calculado por recebível
**Descrição:** O imposto total é a soma do imposto arredondado individualmente em cada recebível.
**Motivação:** `total × alíquota` pode divergir da obrigação produzida por arredondamento individual.
**Entradas:** lista de recebíveis com valor bruto e alíquota; imposto total calculado.
**Resultado esperado:**
`imposto_total = soma(arredondar_centavos(bruto_i × aliquota_i))`.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_tax_is_rounded_for_each_receivable`.

## INV-010 — Cenário conservador

**Título:** Atraso nunca antecipa comissão
**Descrição:** O cenário conservador mantém ou desloca o recebimento para uma data posterior.
**Motivação:** Uma hipótese conservadora não pode criar renda antes da data original.
**Entradas:** data original e data conservadora do recebimento.
**Resultado esperado:** `data_conservadora >= data_original`.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_conservative_delay_never_advances_receipt`.

## INV-011 — Consignado

**Título:** Consignado líquido não é descontado duas vezes
**Descrição:** Se o empréstimo já está refletido no salário líquido do holerite, a projeção não cria
novo abatimento.
**Motivação:** Evitar despesa artificial e redução dupla da capacidade familiar.
**Entradas:** indicador de desconto no salário e abatimento consignado da projeção.
**Resultado esperado:** abatimento adicional igual a zero quando já descontado.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_net_salary_does_not_repeat_payroll_loan`.

## INV-012 — Férias

**Título:** Adiantamento de férias não é renda extra
**Descrição:** Apenas o adicional líquido efetivo pode ser um evento adicional; adiantamento salarial
é competência deslocada.
**Motivação:** Impedir inflação temporária de renda seguida de déficit artificial.
**Entradas:** adiantamento, adicional líquido efetivo e efeito de renda adicional.
**Resultado esperado:** efeito adicional igual somente ao adicional líquido efetivo.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_vacation_advance_is_not_extra_income`.

## INV-013 — Benefícios

**Título:** VA/VR não são caixa livre
**Descrição:** Benefícios representam capacidade de consumo específica, sem financiar dívida, imóvel
ou saldo bancário.
**Motivação:** Preservar a diferença entre benefício restrito, renda e liquidez.
**Entradas:** valor do benefício e efeitos em caixa, dívida e parcela de imóvel.
**Resultado esperado:** todos esses efeitos livres são zero; o benefício pode aparecer em capacidade
alimentar separada.
**Severidade se violado:** `REVIEW`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_benefits_do_not_increase_free_cash`.

## INV-014 — Duplicidade

**Título:** Duplicidade provável fora dos totais
**Descrição:** Registro com confiança provável ou forte permanece preservado, auditável e excluído dos
totais até resolução.
**Motivação:** Evitar dupla contagem sem apagar duas compras legítimas semelhantes.
**Entradas:** confiança, status da resolução e indicador de inclusão nos totais.
**Resultado esperado:** confiança a partir de `0,60` e status pendente implica
`included_in_totals = false`.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_probable_duplicate_stays_out_of_totals_until_resolution`.

## INV-015 — Fonte canônica histórica

**Título:** Uma fonte nos totais, todas preservadas
**Descrição:** Quando planilha e documento histórico representam o mesmo evento, somente a fonte
canônica entra nos totais; todas permanecem armazenadas e relacionadas.
**Motivação:** Evitar dupla contagem sem destruir evidência histórica.
**Entradas:** fonte canônica, todas as fontes relacionadas, fontes contadas e fontes preservadas.
**Resultado esperado:** exatamente a fonte canônica é contada uma vez e todas as fontes relacionadas
permanecem preservadas sem duplicação.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_only_canonical_historical_source_is_counted`.

## INV-016 — Estorno

**Título:** Estorno compensa gasto
**Descrição:** O estorno reduz a despesa correspondente ou cria crédito corretamente, sem virar
receita operacional comum.
**Motivação:** Não inflar simultaneamente receitas e despesas pela reversão do mesmo evento.
**Entradas:** valor do estorno, redução de despesa e efeito em receita operacional.
**Resultado esperado:** redução igual ao estorno e efeito em receita igual a zero.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_refund_offsets_expense_without_creating_income`.

## INV-017 — Competência de cartão

**Título:** Compra segue competência da fatura
**Descrição:** A compra é contabilizada segundo a política canônica da fatura, preservando a data
original como linhagem.
**Motivação:** Evitar que dashboard, fechamento e relatório escolham competências diferentes.
**Entradas:** competência canônica e competência em que a compra foi contada.
**Resultado esperado:** competências iguais.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_card_purchase_uses_statement_competence`.

## INV-018 — Projeção consistente

**Título:** Motor e validador de projeção concordam
**Descrição:** Todos os cenários usam a fórmula canônica e são reproduzidos por caminho independente.
**Motivação:** Detectar regressão no componente que orienta decisões futuras.
**Entradas:** valores mensais do Financial Engine, valores do Projection Validator e tolerância.
**Resultado esperado:** mesmas chaves e diferença absoluta de cada valor menor ou igual à tolerância.
**Severidade se violado:** `BLOCK`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_projection_mismatch_blocks_trust`.

## INV-019 — Fonte única do dashboard

**Título:** Dashboard não recalcula totais
**Descrição:** O dataset do dashboard é derivado do snapshot canônico produzido no backend.
**Motivação:** Eliminar regras financeiras escondidas em templates e JavaScript.
**Entradas:** valores do Financial Engine, valores fornecidos ao dashboard e tolerância.
**Resultado esperado:** datasets monetários equivalentes dentro da tolerância.
**Severidade se violado:** `BLOCK`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_dashboard_dataset_matches_financial_engine`.

## INV-020 — Fonte única dos relatórios

**Título:** Relatórios não recalculam totais
**Descrição:** PDF, impressão, Excel e relatório web recebem o snapshot canônico já calculado.
**Motivação:** Garantir que o mesmo período não publique números diferentes por canal.
**Entradas:** valores do Financial Engine, valores do relatório e tolerância.
**Resultado esperado:** datasets monetários equivalentes dentro da tolerância.
**Severidade se violado:** `BLOCK`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_report_dataset_matches_financial_engine`.

## INV-021 — Advisor sem cálculo oficial independente

**Título:** IA explica, código prova
**Descrição:** O Advisor pode interpretar e comparar, mas preserva todos os números oficiais recebidos
do backend.
**Motivação:** Impedir que uma resposta probabilística substitua o livro e os motores determinísticos.
**Entradas:** valores oficiais, valores repetidos no contexto/resposta e tolerância.
**Resultado esperado:** os valores financeiros são idênticos; observações semânticas ficam separadas.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_advisor_cannot_override_official_values`.

## INV-022 — Rastreabilidade

**Título:** Agregado reproduzível até a origem
**Descrição:** Todo agregado relevante identifica lançamentos, fontes, documentos quando aplicável,
regras, período, contas, categorias e versão do cálculo.
**Motivação:** Permitir responder de onde veio qualquer número e reproduzi-lo durante auditoria.
**Entradas:** contagem e IDs de fontes, lançamentos, documentos, regras, contas, categorias, período e
versão do cálculo.
**Resultado esperado:** dimensões obrigatórias não vazias, documentos presentes quando exigidos e
`source_count` igual ao número de fontes únicas.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_invariants.py::test_aggregate_requires_complete_lineage`.

## INV-023 — Resgate/aplicação REALIZADO exige evidência

**Escopo:** REALIZADO (`reconcile_actual_liquidity`/`calculate_actual_snapshot`).
**Título:** Liquidez realizada não é inferida do resultado operacional
**Descrição:** Um período REALIZADO só pode reportar `liquidity_used`/`liquidity_deposit` do
Privilège quando há evidência de um movimento real de "Transferência patrimonial" (transação
importada/observada do extrato bancário, ou ação explicitamente confirmada pelo usuário/Assistente —
por exemplo `funding_source="privilege"`). Nunca é inferido do sinal do resultado operacional, e uma
`AccountBalanceObservation` isolada nunca basta como evidência: ela prova posição, não causa.
**Motivação:** Rebaseline §4.2/§4.3 — resultado operacional negativo não prova resgate; resultado
positivo não prova aplicação. A fabricação automática (`resultado_operacional < 0 -> liquidity_used`)
esconde a diferença entre resultado econômico e movimento real de caixa.
**Entradas:** `liquidity_used`, `liquidity_deposit`, `has_transfer_evidence`.
**Resultado esperado:** `has_transfer_evidence` é verdadeiro sempre que `liquidity_used > 0` ou
`liquidity_deposit > 0`; caso contrário ambos são zero.
**Severidade se violado:** `BLOCK`.
**Teste automatizado associado:**
`tests/test_financial_snapshots.py::test_account_balance_observation_alone_never_materializes_liquidity_transfer`,
`tests/test_financial_snapshots.py::test_realized_liquidity_evidence_facts_catch_a_snapshot_that_fabricated_movement_without_evidence`,
`tests/test_financial_engine.py::test_actual_snapshot_never_fabricates_liquidity_withdrawal_without_transfer_movement_or_confirmed_action`.

## INV-024 — Saldo confirmado soberano; divergência sinalizada

**Escopo:** REALIZADO.
**Título:** Observação de saldo confirmada é soberana; nenhum ajuste sintético
**Descrição:** Quando existe observação de saldo confirmada (`AccountBalanceObservation` confiável),
ela prevalece sobre a reconstrução derivada no instante observado. Qualquer divergência entre o
saldo confirmado e a reconstrução baseada em evidência é exposta para investigação — nunca mascarada
— e o sistema nunca fabrica uma transação de ajuste para zerá-la. Disclosure isolado não basta: o
`closing_liquidity_balance` publicado pelo snapshot precisa, ele mesmo, ser a âncora confirmada mais
o movimento evidenciado posterior a ela (`derived_balance_since_observation`) sempre que existir uma
observação confiável intra-período — nunca a reconstrução aditiva desde a abertura, mesmo que a
divergência entre as duas apareça corretamente exposta em `reconciliation`. A busca por essa
observação intra-período tampouco pode depender de a abertura do período já ser confiável: uma
observação confirmada no meio do período torna-se a âncora soberana a partir de sua própria data
mesmo quando a abertura caiu no fallback legado não confiável.
**Motivação:** Rebaseline §5.1/§5.2 — hipótese não vira fato; saldo confirmado é soberano no instante
observado. (Correção pós-review de engenharia na PR #89, 2026-09-12: a versão anterior permitia que
`closing_liquidity_balance` continuasse na reconstrução aditiva mesmo com divergência divulgada, e
ignorava uma observação confiável intra-período sempre que a abertura do período não fosse, ela
mesma, confiável.)
**Entradas:** `reconciliation_divergence`, `divergence_disclosed`, `synthetic_adjustment_created`,
`confirmed_anchor_present`, `closing_matches_confirmed_anchor`.
**Resultado esperado:** `synthetic_adjustment_created` é sempre falso; quando
`reconciliation_divergence != 0`, `divergence_disclosed` é verdadeiro; quando
`confirmed_anchor_present` é verdadeiro, `closing_matches_confirmed_anchor` também é.
**Severidade se violado:** `CRITICAL`.
**Teste automatizado associado:**
`tests/test_financial_snapshots.py::test_intra_period_balance_observation_is_reflected_without_rewriting_it`,
`tests/test_financial_snapshots.py::test_intra_period_balance_observation_divergence_is_disclosed_not_masked`,
`tests/test_financial_snapshots.py::test_intra_period_observation_becomes_sovereign_anchor_without_opening_evidence`,
`tests/test_financial_snapshots.py::test_point_in_time_balance_becomes_next_period_opening_evidence`.

## INV-025 — Pagamento de fatura nunca duplica gasto (parcial ou integral)

**Escopo:** REALIZADO (`CardInvoice`, `app.services.card_invoice_lifecycle.pay_invoice`).
**Título:** Pagamento de fatura é liquidação, nunca despesa nova
**Descrição:** Qualquer pagamento de fatura de cartão — integral ou parcial, em um ou mais
pagamentos ao longo do tempo — cria apenas um lançamento de conciliação
(`transaction_type="reconciliation"`, `excluded=True`) na conta pagadora; nunca cria ou duplica um
lançamento de despesa. O número de lançamentos `expense` do ciclo permanece igual ao das compras
originais, independentemente de quantos pagamentos parciais liquidam a fatura.
**Motivação:** Rebaseline §6.3/§6.5 — "Pagamento de fatura nunca é novo gasto."
**Entradas:** contagem de lançamentos `expense` do ciclo antes e depois de cada pagamento;
`CardInvoice.paid_total`/`status`.
**Resultado esperado:** a contagem de lançamentos `expense` do ciclo é idêntica antes e depois de
qualquer pagamento (parcial ou integral).
**Severidade se violado:** `BLOCK`.
**Implementação executável:** ainda não integrada ao Financial Integrity Engine
(`app/services/invariant_registry.py`) — coberta nesta fase apenas pelo teste dedicado abaixo; ver
riscos residuais do PR do Slice 2.
**Teste automatizado associado:**
`tests/test_card_invoice_lifecycle.py::test_pay_invoice_partial_then_remainder_reaches_paid_without_double_counting`.

## INV-026 — Principal carregado não é gasto novo

**Escopo:** REALIZADO (`CardInvoice`).
**Título:** Saldo não pago transportado é campo, não transação
**Descrição:** O saldo não pago de uma fatura fechada é transportado para o ciclo seguinte apenas
como `CardInvoice.principal_carried_in`/`principal_carried_out` — nunca como uma nova `Transaction`
de compra ou despesa no ciclo seguinte.
**Motivação:** Rebaseline §6.5 — "não é nova compra e não como novo gasto."
**Entradas:** `principal_carried_in` do ciclo seguinte; contagem de lançamentos `expense` criados
nesse ciclo.
**Resultado esperado:** `principal_carried_in` reflete exatamente o saldo em aberto do ciclo
anterior no momento da sincronização; nenhuma `Transaction` nova é criada apenas para representá-lo.
**Severidade se violado:** `BLOCK`.
**Implementação executável:** ainda não integrada ao Financial Integrity Engine — mesma ressalva da
INV-025.
**Teste automatizado associado:**
`tests/test_card_invoice_lifecycle.py::test_principal_carried_forward_without_creating_new_expense`.

## INV-027 — Estorno neutraliza sem apagar histórico (cartão)

**Escopo:** REALIZADO (`Transaction.refund_of_transaction_id`).
**Título:** Vínculo explícito de estorno preserva a compra original
**Descrição:** Um estorno só neutraliza o valor da compra original depois de vinculado
explicitamente (`link_refund`) — nunca inferido por valor/data/descrição. O vínculo neutraliza
apenas o valor efetivamente somado dos estornos vinculados àquela compra (nunca mais do que o valor
original) e nunca apaga a compra original. Quando o estorno cai em um ciclo posterior ao da compra —
inclusive depois de a fatura original já ter sido paga — o crédito reduz o total do ciclo em que o
próprio estorno foi lançado, nunca reescrevendo o ciclo/fatura original já liquidado.
**Motivação:** Rebaseline §6.6.
**Entradas:** `Transaction.refund_of_transaction_id`, soma dos estornos vinculados a uma compra,
`CardInvoice.computed_total`/`paid_total`/`status` do ciclo original e do ciclo em que o estorno foi
lançado.
**Resultado esperado:** a compra original nunca é apagada; a soma dos estornos vinculados a uma
compra nunca excede o valor original; um ciclo já `paid` mantém `computed_total`/`paid_total`/`status`
inalterados quando um estorno da compra que o compõe é lançado em um ciclo posterior.
**Severidade se violado:** `BLOCK`.
**Implementação executável:** ainda não integrada ao Financial Integrity Engine — mesma ressalva da
INV-025.
**Teste automatizado associado:**
`tests/test_card_invoice_lifecycle.py::test_refund_integral_neutralizes_purchase_without_deleting_it`,
`tests/test_card_invoice_lifecycle.py::test_refund_partial_neutralizes_only_the_linked_amount_and_rejects_overshoot`,
`tests/test_card_invoice_lifecycle.py::test_refund_after_invoice_already_paid_credits_later_cycle_without_rewriting_it`.

## INV-028 — Divergência de fatura nunca é ajustada silenciosamente

**Escopo:** REALIZADO (`CardInvoice`, `app.services.card_invoice_lifecycle.invoice_divergence`).
**Título:** Diferença entre total do sistema e total declarado é sinalizada, nunca corrigida
sozinha
**Descrição:** Quando `declared_total` (evidência de fatura/extrato já importado) diverge de
`computed_total + principal_carried_in` além da tolerância monetária padrão, o sistema procura uma
causa determinística e não contada ainda (estorno não vinculado, encargo Juros/IOF/tarifa ainda não
reclamado por nenhuma fatura) e a reporta; se nenhuma causa for encontrada, o resultado é
`unreconciled_unexplained`. Em nenhum dos dois casos o sistema fabrica um ajuste sintético para
zerar a diferença.
**Motivação:** Rebaseline §6.4.
**Entradas:** `declared_total`, `computed_total + principal_carried_in`, diferença, lista de
`explanations`.
**Resultado esperado:** `status in {reconciled, unreconciled_explained, unreconciled_unexplained,
not_applicable}`; nenhum campo de valor é reescrito para forçar `reconciled`.
**Severidade se violado:** `CRITICAL`.
**Implementação executável:** ainda não integrada ao Financial Integrity Engine — mesma ressalva da
INV-025.
**Teste automatizado associado:**
`tests/test_card_invoice_lifecycle.py::test_divergence_reconciled_when_declared_matches_computed`,
`tests/test_card_invoice_lifecycle.py::test_divergence_unreconciled_unexplained_without_any_candidate_cause`,
`tests/test_card_invoice_lifecycle.py::test_divergence_explained_by_unlinked_refund_in_cycle`.

## Controle de mudança

Toda alteração deste contrato exige:

1. justificativa financeira explícita;
2. atualização de `financial_rules_version` conforme versionamento `AAAA.MM.revisão`;
3. alteração conjunta de documentação, registry e testes;
4. avaliação de impacto em snapshots já persistidos;
5. migração ou backfill somente quando necessário, sem reescrever silenciosamente o histórico;
6. registro no audit trail quando a alteração chegar ao ambiente da família.

Uma regra crítica ou `BLOCK` sem teste automatizado impede publicação confiável e merge.
