# Work Order — compatibilidade PDF Nubank e Mercado Pago

## Executor e autoridade

Claude é o executor deste bugfix. O engenheiro responsável revisa e decide o merge. Claude **não pode fazer merge**, alterar regras financeiras documentadas, reclassificar/corrigir dados reais automaticamente nem dar ao Codex/Advisor autoridade sobre fatos determinísticos. Claude pode e deve contestar esta orientação quando houver evidência concreta de documentação, código, testes ou segurança; divergência não resolvida bloqueia o merge.

## Contexto verificável

Na carga inicial real de 03/09/2026, `app.cli.initial_load --dry-run` recebeu 46 documentos financeiros: 11 passaram para `pending_full_validation` e 35 ficaram `review_required` por `parse_failed`. Os formatos que falham são PDFs textuais reais de Nubank e Mercado Pago; Itaú e holerite já passam.

A documentação normativa permanece fonte de verdade: `docs/FINANCIAL_RULES.md`, `docs/FINANCIAL_INVARIANTS.md`, `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`, `docs/ROADMAP.md` e contratos executáveis existentes. Em especial:

- transferência interna não é renda/despesa (INV-001);
- pagamento de fatura é conciliação (INV-002);
- estorno/crédito não vira renda operacional comum (INV-016);
- compras de cartão seguem a competência da fatura e preservam a data original (INV-017);
- ausência de componente obrigatório produz `unknown`, nunca fato inventado;
- reconciliação de extrato: saldo inicial + créditos - débitos = saldo final, tolerância R$ 0,01;
- reconciliação de fatura: saldo anterior + compras + encargos - créditos - pagamentos = total declarado;
- documentos reais/PII não podem ser commitados.

## Evidência de layout observada

Use fixtures **sintéticas e anonimizadas** que reproduzam somente a estrutura textual necessária. Não copie nome, CPF, conta, IDs de operação ou valores reais do usuário para o Git.

### Nubank — fatura PDF

Layout observado inclui:

- `FATURA 03 AGO 2026 EMISSÃO E ENVIO 25 JUL 2026`;
- `RESUMO DA FATURA ATUAL`;
- `Fatura anterior R$ ...`, `Pagamento recebido −R$ ...`, `Total de compras ... R$ ...`, `IOF ...`, `Outros lançamentos ...`, `Total a pagar R$ ...`;
- bloco `TRANSAÇÕES DE ...` com linhas como `24 JUN Estabelecimento R$ 6,00`, `24 JUN Compra - Parcela 12/12 R$ 23,00`, `01 JUL Pagamento em 01 JUL −R$ ...`;
- algumas transações possuem descrição/encargos em múltiplas linhas.

### Nubank — extrato de conta PDF

Layout observado inclui:

- período por extenso, por exemplo `01 DE AGOSTO DE 2026 a 31 DE AGOSTO DE 2026`;
- `Saldo inicial`, `Rendimento líquido`, `Total de entradas`, `Total de saídas`, `Saldo final do período`;
- blocos `14 AGO 2026 Total de entradas + ...` / `Total de saídas - ...`;
- descrições podem continuar por várias linhas antes do valor final.

### Mercado Pago — fatura PDF

Layout observado inclui:

- `Vencimento: dd/mm/yyyy`;
- `Resumo da fatura`, `Consumos de ... R$ ...`, `Tarifas e encargos`, `Pagamentos e créditos devolvidos`, `Total R$ ...`;
- `Detalhes de consumo` / `Movimentações na fatura`;
- linhas como `12/01 Pagamento da fatura ... R$ ...`, `12/01 Devolução ... R$ ...`;
- bloco de cartão `Cartão Visa [************1624]` seguido de compras como `08/11 ... Parcela 4 de 18 R$ 54,85`.

### Mercado Pago — extrato de conta PDF

Layout observado inclui:

- `EXTRATO DE CONTA`;
- `Periodo: De 01-02-2026 al 28-02-2026`;
- `Saldo inicial`, `Entradas`, `Saidas`, `Saldo final`;
- tabela `Data Descrição ID da operação Valor Saldo`;
- linha simples como `05-02-2026 Reembolso Compra garantida 123456 R$ 12,70 R$ 12,70`;
- descrição pode quebrar em múltiplas linhas antes do ID/valor/saldo.

## Objetivo

Fazer o pipeline oficial (`parse_document_contract` -> reconciliação -> `/api/imports`) reconhecer com segurança os layouts PDF textuais Nubank e Mercado Pago já observados, sem regressão nos parsers Itaú/CSV/OFX/holerite e sem criar uma segunda política financeira.

## Escopo obrigatório

1. Detectar o formato/instituição pelo **conteúdo do PDF**, não pelo nome do arquivo nem por dados pessoais.
2. Implementar parsers especializados ou estratégia equivalente dentro do pipeline canônico existente; não duplicar regras no initial-load.
3. Preservar `booked_at`/competência e `occurred_at` conforme INV-017 para cartão.
4. Preservar sinais de pagamento, crédito/estorno e compras para que o classificador canônico aplique INV-002/INV-016; o parser não deve transformar pagamento de fatura em compra nem crédito em renda.
5. Extrair campos declarados suficientes para reconciliação quando realmente presentes; campo ausente deve ficar `unknown`, nunca estimado.
6. Suportar descrições quebradas em múltiplas linhas sem incorporar cabeçalhos, rodapés, juros simulados, limites ou textos explicativos como transações.
7. Não alterar migrations/Alembic salvo evidência de necessidade real; a expectativa é **zero migration**.
8. Não alterar `FINANCIAL_RULES_VERSION` porque isto é compatibilidade de parser, não mudança de regra financeira.
9. Atualizar `PARSER_CONTRACT_VERSION`/parser version somente se o contrato/semântica de parsing realmente exigir e documentar a justificativa.
10. Não armazenar nem commitar documentos reais, PII, CPF, número de conta/cartão completo, IDs reais de operação ou valores financeiros reais do usuário.

## Casos de zero movimento

Alguns extratos Mercado Pago observados têm saldos/entradas/saídas todos zero e nenhuma movimentação. **Não invente uma transação zero para fazê-los passar.** Verifique o contrato existente antes de decidir se um PDF financeiro reconciliado com zero movimentos pode ser persistido como documento válido. Se o pipeline atual proibir isso, mantenha comportamento conservador e documente a limitação; qualquer mudança de semântica para aceitar documento sem transação exige argumento técnico explícito no PR e teste de não regressão. Não transforme ausência de movimentos em `pass` por conveniência.

## Critérios de aceite

- Fixtures anonimizadas de cada um dos quatro layouts acima.
- Nubank fatura: transações de compra/pagamento/crédito reconhecidas, competência da fatura correta e data original preservada; linhas de resumo/limite/simulação não viram transação.
- Nubank extrato: entradas/saídas reais reconhecidas, sinais corretos e reconciliação possível a partir dos totais declarados quando completos.
- Mercado Pago fatura: compras, parcelas, pagamentos e devoluções reconhecidos com sinais e competência corretos; total declarado extraído quando presente.
- Mercado Pago extrato: linhas simples e descrições multiline reconhecidas com valor e saldo; reconciliação correta quando completa.
- O caso real que hoje produz 35 `parse_failed` deve reduzir os `parse_failed` **somente onde há fatos suficientes**; não existe meta artificial de `0` se algum PDF for realmente vazio/ambíguo.
- Parsers Itaú atuais continuam produzindo os mesmos resultados nos fixtures existentes.
- Nenhum dado real/PII entra no diff.

## Testes obrigatórios

- testes unitários por parser/layout com fixtures sintéticas;
- testes de reconciliação para Nubank e Mercado Pago;
- casos multiline;
- pagamento de fatura não tratado como despesa econômica nova;
- crédito/estorno preservado como crédito;
- parcelas `n/total` preservadas;
- linha de resumo/limite/simulação não vira transação;
- caso inválido/ambíguo continua falhando ou `unknown`, sem heurística permissiva;
- regression dos parsers Itaú existentes;
- `ruff`, suíte unitária, `financial-invariants`, `parser-reconciliation` e demais gates aplicáveis 100% verdes.

## Proibições

- não fazer regex baseada em nome de arquivo;
- não hardcodar nome, CPF, conta, cartão ou estabelecimento do usuário;
- não inventar ano/data/total/saldo quando o documento não fornece evidência suficiente;
- não mudar sinais/regra financeira para fazer reconciliação fechar;
- não reduzir cobertura, remover teste ou relaxar invariant;
- não auto-resolver duplicidade ou `ReviewItem`;
- não inserir dados reais automaticamente na base;
- não fazer merge.

## Entrega esperada do executor

Responder no PR com: diagnóstico do motivo dos quatro formatos falharem, arquitetura escolhida, diff, testes executados, efeito sobre reconciliação, riscos, zero-movement decision, e qualquer objeção técnica ao Work Order sustentada por código/documentação/teste.