# Go-live manual — arquitetura de operação diária

## Status e posição no roadmap

Este documento detalha o item **go-live de uso manual / fluxos financeiros do dia a dia** da Fase 2 em `docs/ROADMAP.md`.

A implementação deve ocorrer somente quando chegar a esse item na ordem documentada. Não alterar o escopo do slice ativo de importação em lote nem pular exportação Excel/PDF e testes com cópias anonimizadas dos documentos reais.

`docs/FINANCIAL_RULES.md`, `docs/FINANCIAL_INVARIANTS.md`, `docs/INTEGRITY_IMPLEMENTATION_PLAN.md` e os contratos canônicos existentes continuam sendo fonte de verdade. Este plano define experiência e orquestração; não cria uma segunda política financeira.

## Objetivo de produto

O uso normal do Family Finance será predominantemente **manual**, com importação de extratos/faturas como carga inicial, conferência e auditoria eventual. A interface deve permitir que Vinicius e Kelly registrem fatos do dia a dia sem conhecer nomes internos como `reconciliation`, `transfer`, `excluded` ou invariantes.

A UX deve separar claramente **fazer uma operação** de **consultar/auditar fatos já registrados**.

## Arquitetura de informação alvo

### Visão geral

Mantém dashboard, posição, projeções e alertas. Não é tela de criação de fatos.

### Lançar agora

Mantém a Central Inteligente como **atalho opcional** por texto/áudio/documento. Ela interpreta a intenção, mas deve apresentar prévia antes da confirmação e reutilizar exatamente os mesmos comandos/serviços canônicos das telas estruturadas.

Não pode ser o único caminho para operações essenciais e não pode possuir semântica financeira paralela.

### Entradas

Tela estruturada para fatos que representam **receita econômica real**.

Exemplos: salário, comissão, recebimento eventual e demais receitas previstas pelas regras financeiras.

Dinheiro que apenas entrou em uma conta por transferência, resgate de investimento ou estorno não pode ser promovido silenciosamente a receita.

### Saídas

Tela estruturada para **despesa econômica real**.

Deve suportar conta/cartão de origem, categoria, data/competência e compra parcelada. Para parcelamento, a prévia deve mostrar valor da parcela, parcela atual/total quando aplicável, meses futuros e efeito na projeção canônica.

Fatos futuros projetados não podem duplicar parcelas posteriormente observadas/importadas.

### Contas a pagar

Centro de obrigações e pagamentos, separado de `Lançamentos`.

Deve mostrar obrigações a vencer, próximas do vencimento, vencidas e pagas conforme fatos canônicos disponíveis. O ato de pagar deve partir da obrigação/fatura e perguntar explicitamente a origem dos recursos.

#### Fluxo especializado — pagamento de fatura de cartão

Entrada mínima do usuário:

- cartão/fatura;
- valor pago;
- conta pagadora;
- data;
- confirmação humana.

Pagamento de fatura é `reconciliation`/movimento de quitação e **nunca nova despesa** (INV-002). As compras que compõem a fatura continuam sendo os fatos de despesa.

Quando a conta pagadora não tiver liquidez suficiente, a UI pode oferecer um fluxo combinado de obtenção de recursos, mas nunca inferir ou executar a origem automaticamente.

Exemplo suportado:

1. resgatar R$ 4.000 do Privilège DI para a conta Itaú;
2. pagar R$ 5.000 da fatura Itaú pela conta Itaú.

O primeiro passo é movimento patrimonial; o segundo é conciliação. O fluxo combinado deve ser apresentado como uma operação compreensível ao usuário, preservando dois fatos canônicos relacionados e **R$ 0 de nova despesa** no pagamento da fatura.

Se já houver saldo suficiente na conta pagadora, nenhum resgate deve ser criado ou sugerido como fato consumado.

### Transferências

Tela/ação estruturada para movimentação entre contas e patrimônio do próprio household.

Deve cobrir ao menos:

- conta bancária -> outra conta bancária;
- conta -> investimento/aplicação;
- investimento -> conta/resgate.

Transferência, aplicação e resgate não são receita nem despesa. A UX pode usar verbos distintos (`Transferir`, `Aplicar`, `Resgatar`), mas todos devem convergir para o motor financeiro canônico.

### Lançamentos

Passa a ser o **livro-razão unificado de consulta e auditoria**, não o principal formulário de criação.

Deve reunir fatos originados por Entradas, Saídas, Contas a pagar, Transferências, Lançar agora e importações, com filtros e proveniência. Correções/exclusões permitidas devem respeitar os contratos existentes de auditoria, imutabilidade, reconciliação e integridade; não criar atalhos destrutivos.

## Fluxos mínimos de go-live

Antes de declarar a aplicação pronta para uso diário, testes completos API/UI devem cobrir pelo menos:

1. despesa à vista em conta;
2. despesa em cartão;
3. compra parcelada com projeção futura e posterior substituição da projeção por fato observado sem duplicação;
4. receita;
5. transferência entre contas próprias;
6. aplicação em investimento;
7. resgate de investimento;
8. pagamento manual de fatura com saldo suficiente;
9. pagamento manual de fatura precedido por resgate do Privilège DI;
10. pagamento parcial de fatura, se já suportado pelo contrato canônico; caso contrário permanecer explicitamente fora do slice até regra normativa própria;
11. estorno/refund sem virar receita indevida;
12. consulta de todos os fatos acima no livro-razão com origem e sem dupla contagem.

## Invariantes e proibições

- INV-002 e demais invariantes aplicáveis são obrigatórios.
- Pagamento de fatura não cria despesa adicional.
- Transferência, aplicação e resgate não criam receita/despesa.
- A UI nunca calcula uma política financeira diferente do backend.
- `Lançar agora` e telas estruturadas devem convergir para os mesmos serviços/comandos canônicos.
- Nenhum fluxo pode alterar silenciosamente fatos financeiros existentes.
- Nenhum resgate, transferência, pagamento ou origem de recursos pode ser inventado por IA.
- Claude/Codex/Advisor podem sugerir ou explicar, mas não têm autoridade sobre fatos determinísticos.
- Household isolation, autorização e trilha de auditoria são obrigatórios.
- Migrations, se necessárias, devem ser aditivas, PostgreSQL/Alembic-safe e compatíveis com dados existentes.
- Não reduzir cobertura ou modificar regra financeira para fazer teste passar.

## Slices planejados para o item de go-live manual

Quando este item chegar na ordem do roadmap, implementar em slices pequenos e revisáveis:

1. **Navegação + Entradas/Saídas** — separar criação estruturada de consulta e consolidar os comandos canônicos de receita/despesa/parcelamento.
2. **Transferências + Aplicação/Resgate** — experiência estruturada para movimentos patrimoniais sem receita/despesa.
3. **Contas a pagar + Pagamento de fatura** — centro de obrigações, quitação manual e fluxo combinado com origem de recursos confirmada pelo usuário.
4. **Lançamentos como livro-razão** — remover a ambiguidade entre criar e consultar; unificar proveniência e filtros.
5. **E2E + Go-live** — validar os fluxos mínimos acima, projeção/fato observado, household isolation, auditoria, regressão, mobile e os 12 gates antes de uso diário.

Cada slice deve ter Work Order próprio quando chegar sua vez. Não criar branches/PRs antecipadamente e não misturar esses slices com itens anteriores da Fase 2.
