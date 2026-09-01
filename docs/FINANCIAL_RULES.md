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

- O Privilège DI funciona como o caixa central da família, embora tecnicamente seja uma aplicação.
- A sobra operacional do mês é destinada a essa conta; quando as receitas não cobrem as saídas, o déficit é retirado dela.
- Aplicações e resgates são movimentos patrimoniais e não viram receita, despesa ou consumo do teto.
- Todo o saldo permanece em uma única conta de liquidez.
- O saldo mínimo é uma meta de segurança e um alerta, não uma separação bancária nem dinheiro bloqueado.
- Um resultado negativo consome o saldo do Privilège DI até zerá-lo, mesmo que isso rompa o piso.
- Se o déficit for maior que o saldo informado, o sistema mostra saldo final zero e o valor restante como déficit sem cobertura.
- Um resultado positivo é somado ao saldo do Privilège DI como sobra destinada à liquidez.
- A distância do piso é calculada sobre o saldo depois do fechamento; se for negativa, o sistema mostra quanto falta recompor.
- A taxa líquida mensal estimada é calculada a partir do retorno bruto anual e do IR conservador.
- O rendimento mensal incide sobre o saldo inicial positivo do mês.
- Entradas do mês começam a influenciar o rendimento no mês seguinte.

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
  `acknowledged` (ciência humana registrada, ainda ativo) contam para `consolidated_integrity_status`
  e para os gates; `superseded` é um estado determinístico e automático — a reavaliação mais recente
  do mesmo fingerprint retornou `PASS` — que remove o finding dos gates ativos sem preencher
  `resolved_at`/`resolved_by`/`resolution_reason`, que ficam reservados para a ação humana explícita
  de `POST /findings/{id}/resolve` (ainda não implementada nesta fatia); `ignored` e `false_positive`
  seguem a mesma reserva para ações humanas futuras. Um finding `superseded` cuja condição volte a
  falhar é reaberto automaticamente para `open` (nunca direto para um estado terminal reservado a
  ação humana), preservando `first_seen_at` e incrementando `occurrence_count`; a transição fica
  registrada em `metadata_json`. Isso evita tanto o finding eternamente `open` após uma resolução
  legítima (evidência obsoleta) quanto o finding preso em `superseded` durante uma reincidência real
  (falsa confiança) — ambos contradizem a regra de que ausência de evidência não vira `pass`.
- Findings não resolvem nem corrigem dados automaticamente. A correção continua no fluxo próprio e
  deve ser ligada à trilha de auditoria.
- `trusted_for_projection` e `trusted_for_reports` são gates específicos e começam fechados quando
  não existe evidência aplicável; eles não são inferidos apenas do score geral.
- O Codex não participa do cálculo do score, status, finding determinístico ou gate de confiança.
- A interface permanece oculta por padrão em `INTEGRITY_UI_ENABLED=false` até a etapa dedicada de UX.

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
