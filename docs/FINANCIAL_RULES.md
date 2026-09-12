# Regras financeiras

## Orçamento familiar

- O sistema consolida toda a família em um único orçamento.
- Conta, cartão e titular são preservados para rastreabilidade.
- Transferências internas não são renda nem despesa.

## Cartões

- Compras de faturas entram no mês de referência da fatura; a carga da planilha usa o mês de vencimento.
- A data original da compra continua preservada na própria planilha consolidada.
- Pagamento da fatura é conciliação e não é contado novamente.
- Estorno é crédito.
- Parcelas futuras são projetadas pelo valor observado e pelas marcações `atual/total`.
- Mudança de valor ou antecipação exige revisão manual.

## Documentos sobrepostos

- Reimportação exata é bloqueada pelo hash do arquivo.
- Lançamento com fingerprint já existente é marcado como possível duplicidade e excluído provisoriamente.
- O usuário decide se mantém excluído ou confirma como lançamento legítimo.
- Quando a planilha consolidada e um documento histórico contêm o mesmo lançamento, a planilha é a fonte canônica nos totais mensais; a cópia não é apagada e permanece auditável.
- Cartões são conciliados pela competência da fatura; contas correntes usam a data exata do lançamento.

## Renda PJ e comissões

- Cada recebível possui mês esperado, bruto, alíquota e atraso conservador.
- O imposto é arredondado por recebível, e não apenas sobre o total agregado.
- Uma comissão cadastrada em 2027 não pode aparecer em 2026.
- O cenário conservador desloca o mês; não muda o ano de origem nem antecipa receita.

## Holerites

- O líquido regular alimenta o salário-base somente quando configurado no perfil.
- Consignado registrado no holerite não é descontado novamente.
- 13º e férias são eventos separados.
- Adiantamento salarial de férias não é renda extra.
- Apenas o adicional líquido real deve ser cadastrado como `vacation_extra`.

## Benefícios

- VA e VR são capacidade de consumo alimentar, mas não caixa livre.
- O VR mensal é calculado como valor diário vezes dias trabalhados.
- Benefícios não financiam parcelas da chácara ou cartão.

## Plano de cortes

- A análise usa até seis meses de lançamentos importados e não excluídos.
- Pagamentos de fatura, transferências internas, aplicações e resgates não entram como consumo.
- A média mensal de cada categoria é comparada ao teto configurado.
- Categorias não essenciais e com teto zero aparecem primeiro.
- Gastos essenciais recebem recomendação somente sobre o excedente ao teto.
- Mercado pago em dinheiro pode ter teto zero quando a estratégia é usar VA/VR.
- O potencial de economia é uma meta operacional; não é tratado como renda na projeção.

## Conta central de liquidez — Privilège DI

**Reescopado pelo October Go-Live Rebaseline, Slice 1 (P0 #87, 2026-10):** as
regras abaixo marcadas "PROJEÇÃO" descrevem apenas a matemática hipotética de
projeção/simulação (30/60/90 dias, comparação de cenário de compra). Elas
**não** descrevem mais o fechamento de um período REALIZADO — ver a seção
"Liquidez REALIZADO" logo abaixo. Ver `docs/OCTOBER_GO_LIVE_REBASELINE.md`
§4 e `docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §8 (Technical Challenge #1).

- O Privilège DI funciona como o caixa central da família, embora tecnicamente seja uma aplicação.
- Aplicações e resgates são movimentos patrimoniais e não viram receita, despesa ou consumo do teto,
  seja em projeção ou em fato realizado.
- Todo o saldo permanece em uma única conta de liquidez.
- O saldo mínimo é uma meta de segurança e um alerta, não uma separação bancária nem dinheiro bloqueado.
- **PROJEÇÃO:** a sobra operacional projetada é modelada como se fosse destinada a essa conta; quando
  as receitas projetadas não cobrem as saídas projetadas, o déficit hipotético é modelado como
  retirado dela. Esta é uma simulação de cenário, nunca um fato.
- **PROJEÇÃO:** um resultado negativo projetado consome o saldo projetado do Privilège DI até
  zerá-lo, mesmo que isso rompa o piso; se o déficit projetado for maior que o saldo, o sistema
  mostra saldo final zero e o valor restante como déficit sem cobertura projetado.
- **PROJEÇÃO:** um resultado positivo projetado é somado ao saldo projetado do Privilège DI como
  sobra destinada à liquidez hipotética.
- A distância do piso é calculada sobre o saldo depois do fechamento/projeção; se for negativa, o
  sistema mostra quanto falta recompor.
- A taxa líquida mensal estimada é calculada a partir do retorno bruto anual e do IR conservador.
- O rendimento mensal incide sobre o saldo inicial positivo do mês.
- Entradas do mês começam a influenciar o rendimento no mês seguinte.

### Liquidez REALIZADO (fato fechado)

- Um resultado operacional REALIZADO negativo **nunca** prova ou fabrica um resgate do Privilège; um
  resultado positivo **nunca** prova ou fabrica uma aplicação. Rebaseline §4.3: é proibido o
  comportamento `resultado_operacional < 0 -> fabricar liquidity_withdrawal` (e o espelho para
  sobra/aplicação) como fato.
- O saldo REALIZADO de fechamento do Privilège é `saldo_inicial + aplicação_evidenciada -
  resgate_evidenciado + rendimento_evidenciado` — nunca uma função do resultado operacional.
  "Evidenciado" significa uma transação real categorizada "Transferência patrimonial", criada a
  partir de um movimento bancário importado/observado ou de uma ação explicitamente confirmada pelo
  usuário/Assistente (ex.: `funding_source="privilege"`).
- Uma `AccountBalanceObservation` isolada nunca é evidência suficiente para fabricar um
  resgate/aplicação: ela prova a posição soberana naquele instante, não a causa do delta. Na
  ausência de qualquer movimento evidenciado, o saldo de fechamento é simplesmente o saldo de
  abertura, e o resultado operacional do período é exposto como `unexplained_operating_result` para
  revisão humana — nunca mascarado, nunca usado para fabricar o movimento.
- Quando existe observação de saldo confirmada, ela é soberana no instante observado. Qualquer
  divergência entre o saldo confirmado e a reconstrução baseada em evidência é exposta para
  investigação — nunca corrigida silenciosamente por uma transação de ajuste sintética.
- Implementação: `app.services.financial_engine.reconcile_actual_liquidity`. Invariantes:
  INV-023/INV-024 (`docs/FINANCIAL_INVARIANTS.md`).

## Integridade persistente

- Toda execução registra escopo, gatilho, versões, duração, `trace_id` e resumo; ela não altera
  lançamento, documento, perfil ou qualquer outro dado financeiro de origem.
- Somente regras com fatos determinísticos disponíveis são executadas. Sem checks aplicáveis, o
  status é `unknown`, o score é `null` e os gates de confiança permanecem fechados.
- O score usa os pesos formais INFO=1, WARNING=2, REVIEW=4, CRITICAL=8 e BLOCK=16, combinados aos
  fatores pass=1, warning=0,75, unknown=0,50 e fail=0.
- Resultados são agregados por `(invariant_id, scope, period)`; o pior resultado da chave prevalece
  para impedir que repetição de itens maquie o score.
- Findings não aprovados são persistidos por fingerprint. Reincidência atualiza `last_seen_at` e o
  contador sem criar cópia nem apagar o primeiro registro.
- Ciclo de vida de `integrity_findings.status`: `open` (evidência não aprovada, ativo) e
  `acknowledged` (ciência humana registrada via `POST /findings/{id}/acknowledge`, ainda ativo) contam
  para `consolidated_integrity_status` e para os gates; `superseded` é um estado determinístico e
  automático — a reavaliação mais recente do mesmo fingerprint retornou `PASS` — que remove o finding
  dos gates ativos sem preencher `resolved_at`/`resolved_by`/`resolution_reason`, que ficam reservados
  para a ação humana explícita de `POST /findings/{id}/resolve`; `ignored`
  (`POST /findings/{id}/ignore`) e `false_positive` (`POST /findings/{id}/false-positive`) seguem a
  mesma reserva de `resolved_at`/`resolved_by`/`resolution_reason` para suas próprias ações humanas —
  o campo distingue qual decisão foi tomada, não uma coluna por ação. `acknowledge` tem seu próprio
  campo de motivo, `acknowledgement_reason`, já que ela não é terminal. As quatro ações exigem motivo
  obrigatório (mínimo 3 caracteres) e só partem de `open`/`acknowledged` (nunca de `superseded` ou de
  um estado terminal já decidido); reconhecer de novo um finding já `acknowledged` também é rejeitado
  — é uma decisão única, não uma nota atualizável. Um finding `superseded`, `resolved`, `ignored` ou
  `false_positive` cuja condição volte a falhar é reaberto automaticamente para `open` (nunca direto
  para outro estado terminal), preservando `first_seen_at` e incrementando `occurrence_count`; a
  transição fica registrada em `metadata_json`. Isso evita tanto o finding eternamente `open` após uma
  resolução legítima (evidência obsoleta) quanto o finding preso em estado terminal durante uma
  reincidência real (falsa confiança) — ambos contradizem a regra de que ausência de evidência não
  vira `pass`.
- Findings não resolvem nem corrigem dados automaticamente: as quatro ações de lifecycle alteram
  somente as colunas de status/motivo/responsável do próprio finding, nunca `Transaction`, `Document`,
  snapshot ou qualquer outro fato financeiro. A correção continua no fluxo próprio (por exemplo,
  `PATCH /transactions/{id}`) e deve ser ligada à trilha de auditoria.
- `trusted_for_projection` e `trusted_for_reports` são gates específicos e começam fechados quando
  não existe evidência aplicável; eles não são inferidos apenas do score geral.
- O Codex não participa do cálculo do score, status, finding determinístico ou gate de confiança.
- A interface de Integridade (menu, tela, findings, lifecycle, banner BLOCK e Monthly Financial Close)
  existe a partir do PR 7, mas permanece oculta por padrão: só é exibida quando o administrador define
  `INTEGRITY_UI_ENABLED=true`.

### Monthly Financial Close

- `monthly_financial_closes` guarda o estado de fechamento por `household_id`/`period`:
  `open` (nenhuma execução vinculada ainda), `review_required` (há um `snapshot_id`/`integrity_run_id`
  vinculados, mas ainda não confiado ou reaberto) e `trusted` (confiado explicitamente).
- `POST /monthly-closes/{period}/run`, `POST .../trust` e `POST .../reopen` são ações deliberadas e
  separadas: executar checks nunca promove automaticamente para `trusted`, mesmo quando o resultado é
  limpo — a confirmação humana é sempre um passo explícito à parte.
- `run` executa uma nova `IntegrityRun` de escopo `period` (gatilho `close`) e reconstrói o
  `FinancialSnapshot` canônico do período, depois vincula `snapshot_id`/`integrity_run_id` e deixa o
  fechamento em `review_required`. Se a execução de integridade falhar, o próprio run fica `failed` e é
  persistido para auditoria (mesmo comportamento de `POST /integrity/runs`); se algo falhar depois disso
  a operação inteira é desfeita — não existe estado em que o fechamento aponte para um snapshot/run que
  não corresponde à execução mais recente.
- `trust` só é aceito quando: (1) o `FinancialSnapshot` vinculado ainda é o `current` do período (dados
  não mudaram desde o último `run`); (2) `consolidated_integrity_status` do período está em `healthy`
  ou `attention` (nunca `unknown`, `review_required`, `critical` ou `blocked`); e (3) o snapshot atual
  tem `trusted_for_reports` e `trusted_for_projection` verdadeiros. Qualquer pendência bloqueia com um
  motivo explícito por item, sem mudar o estado do fechamento.
- `reopen` só é aceito a partir de `trusted`, exige motivo e preserva todo o histórico: `closed_at`,
  `closed_by`, o `snapshot_id` e o `integrity_run_id` anteriores nunca são apagados; apenas
  `reopened_at`/`reopened_by`/`reason` são preenchidos e o estado volta para `review_required`.
- Nenhuma dessas ações apaga ou reescreve `Transaction`, `Document`, `FinancialSnapshot`,
  `IntegrityFinding` ou `DocumentReconciliation` anteriores.
- `run`, `trust` e `reopen` tomam, cada um como sua primeira ação, a mesma barreira
  (`household_financial_revisions`, seção 8.11 do plano de integridade) antes de ler o `status` atual do
  fechamento. Isso serializa as três ações concorrentes entre si por household: quem adquire a barreira
  primeiro conclui sua própria transição (leitura de estado até a escrita final e commit) antes que
  qualquer uma das outras duas consiga sequer ler `status`. Sem essa ordem, um `run` que terminasse depois
  de um `trust` concorrente já commitado sobrescreveria `trusted` de volta para `review_required` sem
  passar por `reopen` — sem motivo, sem `reopened_by` e sem trilha de auditoria da demoção.

### Backfill (reprocessamento de dados existentes)

- `app.cli.backfill` reprocessa famílias já cadastradas reutilizando os mesmos serviços
  determinísticos do fluxo de importação/API; nunca reimplementa uma regra financeira própria.
- Nunca escreve em `Transaction`, `Document`, `PayrollRecord`, `Commission` ou `Obligation` -- nem
  mesmo nas colunas de classificação de duplicidade (`canonical_status`, `possible_duplicate`,
  `excluded`, `duplicate_group_id`). Duplicidade legada é reportada como evidência derivada
  (`DuplicateGroup`/`DuplicateGroupMember`, via `discover_transaction_duplicates`), imediatamente
  visível em `GET /duplicate-groups`; aplicar essa classificação a uma transação já publicada
  continua sendo uma decisão humana explícita (`resolve_duplicate_group`) ou um efeito de uma
  transação genuinamente nova chegando pelo fluxo ao vivo (`register_transaction_duplicates`), nunca
  um efeito colateral automático do reprocessamento histórico.
- Documento sem reconciliação prévia recebe um registro `unknown` explícito, nunca um total
  declarado/reconstruído fabricado -- a evidência de parsing original não é retida após a importação.
- Saldo legado sem `AccountBalanceObservation` confiável permanece `unknown`/não confiado; o backfill
  nunca infere uma data efetiva nem reconstrói saldo por suposição.
- É idempotente e retomável por construção: cada passo é um no-op sobre dado inalterado ou fica
  restrito às linhas que ainda precisam dele. Repetir a execução -- inclusive após falha parcial --
  reproduz o mesmo estado final; apenas a trilha de auditoria (`IntegrityRun`, `BackfillRun`) cresce
  a cada execução, por definição.
- `--dry-run` executa o mesmo processamento e reverte a transação em vez de persistir, reportando o
  que mudaria sem alterar dado algum; o manifesto `BackfillRun` da execução em dry-run é registrado à
  parte, preservando a evidência de auditoria mesmo com o rollback.
- Nunca resolve, reconhece ou corrige um `IntegrityFinding` automaticamente -- apenas a reavaliação
  determinística de `execute_integrity_run` pode superar (`superseded`) um finding.

## Reconciliação, duplicidades e anomalias

- Todo parser financeiro produz um `ParsedDocument` versionado. Um arquivo tabular sem nenhum
  lançamento reconhecido é rejeitado; zero linhas nunca é tratado como importação válida.
- Extrato reconciliado exige `saldo inicial + créditos - débitos = saldo final declarado`, com
  tolerância de R$ 0,01. Fatura exige `saldo anterior + compras + encargos - créditos - pagamentos =
  total declarado`. Holerite exige `bruto - descontos = líquido declarado`.
- A ausência de qualquer componente obrigatório produz `unknown`; o sistema não inventa saldo,
  total, competência ou data efetiva para fazer um documento fechar.
- Observações de saldo são imutáveis. Uma correção cria nova observação, aponta `supersedes_id` e
  invalida a anterior com usuário, data e justificativa; nenhuma linha histórica é apagada.
- Duplicidades são agrupadas sem remover lançamentos. Sinais ponderados geram bandas `low`,
  `probable` e `strong`. A partir da banda provável (confiança >= 0,60), enquanto a resolução
  estiver pendente, o lado não canônico do par fica excluído dos totais (`included_in_totals =
  false`, INV-014) -- nunca ambos os lados, nunca nenhum -- preservado e auditável até decisão
  humana (`resolve_duplicate_group`). Evidência forte também aplica precedência canônica
  persistida aos papéis `canonical`/`supporting`, mantendo a cópia de suporte auditável e excluída
  apenas dos totais derivados.
- A precedência de fonte é: planilha financeira consolidada (100), lançamento manual confirmado
  (90), documento financeiro (70), captura confirmada (60) e legado/desconhecido (50).
- Resolver um grupo como `distinct` ou `duplicate` é uma ação humana com justificativa. Uma nova
  ocorrência compatível reabre o grupo para revisão sem apagar a resolução anterior, preservada
  nos sinais do grupo.
- Regras locais de classificação precisam de três correções confirmadas e distintas para chegar a
  `pending_acceptance`; somente um administrador pode ativá-las. A ativação vale para classificações
  futuras e nunca recategoriza o histórico silenciosamente.
- Ciclo de vida de `classification_rules.status`: `observed` (uma confirmação), `suggested` (duas),
  `pending_acceptance` (três confirmações distintas — reconfirmar a mesma transação nunca infla a
  contagem) e `active` (aceite explícito de um administrador via
  `POST /classification-rules/{id}/activate`). Um administrador pode desativar uma regra `active`
  (`POST /classification-rules/{id}/deactivate`), levando-a a `inactive`; a desativação nunca apaga
  `confirmation_count` nem `evidence`, e nunca é revertida diretamente pelo administrador — a regra só
  volta a `pending_acceptance` (e dali pode ser reativada) quando uma nova correção confirmada
  genuinamente distinta chega por `record_confirmed_correction`. Um administrador também pode editar
  apenas a `category_id` de uma regra `pending_acceptance`, `active` ou `inactive`
  (`PATCH /classification-rules/{id}`); `movement_type` nunca é editável por essa via porque é herdado,
  no momento da correção confirmada, do `transaction_type` já atribuído deterministicamente à transação
  (o schema de atualização de transação não expõe `transaction_type`), nunca uma escolha livre do
  usuário. Como `confirmation_count`/`evidence` provam apenas o resultado `(estabelecimento normalizado,
  category_id, movement_type)` para o qual foram registrados, uma edição que muda `category_id` nunca
  herda a contagem/evidência da categoria anterior como prova da nova: ela zera `confirmation_count`,
  volta `status` para `observed` e `active` para `false`, e limpa `accepted_at`/`accepted_by` — a regra
  editada precisa acumular suas próprias três correções confirmadas e distintas e uma nova aceitação
  explícita do administrador antes de valer novamente, exatamente como uma regra nova. A evidência
  anterior nunca é apagada ou sobrescrita; fica preservada em `evidence["history"]` para auditoria. Toda
  ativação, desativação e edição é registrada na trilha de auditoria existente com ator, estado
  anterior/posterior e motivo; como a edição também é um reset de ciclo de vida (zera
  `confirmation_count`, `status` e `active`), o estado anterior/posterior registrado para
  `classification_rule.edit` inclui `category_id`, `status`, `active` e `confirmation_count` — não
  apenas a categoria — para que a trilha prove o reset, além da ativação registrar seu próprio estado
  anterior determinístico (`pending_acceptance`/`active=false`).
- Precedência determinística: uma regra local de estabelecimento só pode refinar a *categoria* de uma
  classificação comum de receita/despesa. Ela nunca pode substituir uma classificação estrutural do
  classificador determinístico — transferência interna (INV-001), pagamento/conciliação de fatura
  (INV-002), movimento patrimonial de aplicação/resgate (INV-003/INV-004) ou estorno (INV-016); essas
  continuam sempre vencendo, mesmo que uma regra ativa exista para a mesma descrição normalizada. Dentro
  do espaço comum de receita/despesa, a regra só se aplica quando seu `movement_type` (o resultado que as
  três correções confirmadas efetivamente provaram) é igual ao `transaction_type` que o classificador
  determinístico atribuiria ao evento atual; uma regra aprendida de correções de despesa nunca é aplicada
  a um evento positivo (receita) para o mesmo estabelecimento normalizado, e vice-versa — a evidência
  nunca provou esse outro resultado. Quando a regra se aplica, o `transaction_type`/`excluded`
  determinísticos do evento são preservados; só a categoria é substituída. Todo caminho que *classifica
  automaticamente* uma descrição (importação de documento e captura inteligente) passa pela mesma função
  (`classify_with_local_rules`); não existe uma segunda política de classificação na API, na UI, no
  importador ou na captura inteligente. Lançamento manual e reclassificação manual
  (`POST`/`PATCH /transactions`) continuam sendo escolha direta e autoritativa do usuário — não há
  classificação automática a substituir ali. Isolamento por família: uma regra de uma `household` nunca é
  aplicada, listada, editada ou desativada por outra.
- A primeira baseline de anomalias usa somente meses explicitamente reconciliados e exige três
  competências distintas. O limite é `mediana + máximo(3 × MAD, 50% da mediana)`. Amostra
  insuficiente resulta em `unknown`, não em alerta inventado.
- Codex/Advisor não calcula reconciliação, confiança de duplicidade, baseline, status canônico ou
  decisão de exclusão; esses resultados permanecem determinísticos e auditáveis.

## Projeções

O sistema produz três cenários:

1. sem comissões;
2. comissões com atraso conservador;
3. comissões no mês esperado.

Cada linha mensal considera:

```text
saldo anterior
+ rendimento estimado
+ salário líquido
+ eventos adicionais da folha
+ comissão líquida do cenário
- compromissos
- parcelas futuras
- teto de gastos em dinheiro
```

O fechamento canônico aplica:

```text
saldo final = máximo(0, saldo anterior + resultado do mês)
déficit sem cobertura = máximo(0, -(saldo anterior + resultado do mês))
```

Viabilidade significa que o menor saldo projetado do Privilège DI no cenário conservador permanece
maior ou igual ao piso de segurança. Abaixo do piso o cenário continua calculável, mas recebe alerta;
com déficit sem cobertura, não pode ser apresentado como plenamente confiável.
