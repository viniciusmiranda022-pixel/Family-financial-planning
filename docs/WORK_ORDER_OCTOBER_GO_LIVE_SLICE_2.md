# Work Order — October Go-Live Slice 2

## Objetivo
Implementar o Slice 2 do P0 #87: cartões, faturas, pagamento, estorno, pagamento parcial e parcelamento, conforme `docs/OCTOBER_GO_LIVE_REBASELINE.md`, a matriz do Slice 0 e a semântica integrada no Slice 1.

## Executor e autoridade
Claude é o executor. O engenheiro responsável revisa e integra. Claude não pode fazer merge, iniciar Slice 3, alterar regra financeira não documentada, reduzir cobertura ou corrigir dados reais automaticamente. Claude pode abrir Technical Challenge com evidência concreta.

## Escopo obrigatório
- ciclo de vida de fatura pelo menos `OPEN`, `CLOSED`, `PARTIALLY_PAID`, `PAID`;
- compra no cartão gera gasto econômico e obrigação de fatura, mas não saída bancária imediata;
- pagamento de fatura reduz caixa e obrigação, sem criar nova despesa econômica;
- pagamento parcial preserva principal remanescente sem recontá-lo como gasto;
- juros/IOF/taxas posteriores ao parcial são novas despesas financeiras separadas do principal;
- estorno/reembolso preserva compra original e vínculo auditável; neutraliza somente o valor estornado;
- estorno em ciclo futuro deve produzir crédito no ciclo correto sem reescrever fatura paga anterior;
- parcelamento distingue valor total contratado, impacto mensal e parcelas futuras comprometidas;
- respeitar competência/ciclo documental real do emissor e não reatribuir silenciosamente;
- preservar compatibilidade com dados existentes e migrations anteriores;
- convergir Dashboard/Relatórios/snapshots para a mesma fonte canônica, sem segundo motor.

## Semântica financeira obrigatória
- compra no cartão = despesa econômica no período competente;
- pagamento da fatura = movimento de caixa/conciliação, não nova despesa;
- principal carregado de pagamento parcial = obrigação anterior, não gasto novo;
- juros, IOF e taxas = novas despesas financeiras quando efetivamente lançados;
- estorno não apaga histórico;
- parcelas futuras = COMPROMETIDO, não REALIZADO;
- nenhuma dupla contagem entre compra, parcela, fatura e pagamento.

## Modelo / migration
Antes de criar migration, valide a cadeia Alembic atual e reutilize estruturas existentes quando semanticamente corretas. Se o rebaseline exigir `CardInvoice` ou campos adicionais, a migration deve ser aditiva, reversível, preservar registros atuais e ter estratégia explícita de backfill/UNKNOWN para fatos ausentes. Não invente histórico.

## Critérios de aceite
1. Uma compra de cartão aparece uma única vez como gasto econômico.
2. Pagar a fatura não aumenta o gasto econômico.
3. Pagamento parcial reduz a obrigação e mantém saldo restante correto.
4. Principal transportado não vira despesa novamente; juros/IOF ficam separados.
5. Estorno integral zera economicamente a compra sem removê-la; parcial neutraliza somente a parcela estornada.
6. Estorno posterior a fatura paga preserva pagamento anterior e gera crédito no ciclo correto.
7. Compra parcelada mantém total contratado, parcela do mês e saldo futuro sem fabricar transações futuras.
8. Competência/ciclo respeita evidência documental do emissor.
9. APIs e snapshots não duplicam cálculo nem divergem semanticamente.
10. Dados existentes permanecem legíveis/auditáveis; migration, se houver, é segura.

## Testes obrigatórios
- compra cartão não reduz banco imediatamente;
- pagamento integral de fatura não duplica despesa;
- pagamento parcial e saldo remanescente;
- principal anterior + juros + IOF com separação correta;
- estorno integral e parcial;
- estorno após fatura paga;
- parcelamento: total contratado, impacto mensal e parcelas futuras;
- bordas de fechamento/competência/vencimento;
- idempotência de pagamento/importação;
- regressão contra dupla contagem;
- migration upgrade/downgrade e PostgreSQL quando houver schema;
- invariantes/property tests aplicáveis;
- CI completo existente.

## Segurança e integridade
Preserve household isolation, autorização, provenance, lineage, auditabilidade e minimização de dados. Não apague compras/faturas reais para 'corrigir' reconciliação. Não gere ajuste sintético para zerar diferença de fatura.

## Proibições
- não antecipar Slice 3;
- não implementar Assistente/typed actions do Slice 4;
- não transformar pagamento em despesa;
- não apagar compra em estorno;
- não fabricar parcelas futuras como fatos realizados;
- não reclassificar principal como juros/despesa;
- não criar migration destrutiva;
- não alterar regra para fazer teste passar;
- não reduzir cobertura;
- não fazer merge.

## Definition of Done do Slice 2
Diff dentro do escopo, sem Technical Challenge aberto, invariantes preservados, testes novos e regressivos verdes, lint/CI completo verde, migration/backfill validados quando aplicável, documentação harmonizada, rollback descrito e nenhum risco conhecido de dupla contagem ou corrupção de dados.