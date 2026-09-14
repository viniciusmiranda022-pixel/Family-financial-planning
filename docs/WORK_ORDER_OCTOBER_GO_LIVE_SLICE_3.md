# Work Order — October Go-Live Slice 3

## Contexto

P0 #87 — October Go-Live Rebaseline. Este slice só começa após a integração do Slice 2 e deve seguir `docs/OCTOBER_GO_LIVE_REBASELINE.md` como direção normativa principal. Claude é o executor. O engenheiro responsável revisa, integra e controla a sequência. Claude não pode fazer merge nem iniciar Slice 4.

## Objetivo

Entregar obrigações e projeção com separação inequívoca entre **REALIZADO**, **COMPROMETIDO** e **PREVISTO**, sem misturar fatos ocorridos, compromissos contratados e hipóteses de planejamento.

## Escopo obrigatório

1. Harmonizar a entidade/fluxo de obrigações com os estados financeiros do rebaseline.
2. Garantir que pagamentos efetivamente ocorridos entram como REALIZADO somente mediante fato persistido/auditável.
3. Garantir que obrigações assumidas e parcelas futuras já contratadas entram como COMPROMETIDO, sem serem contabilizadas como gasto realizado antes do vencimento/pagamento.
4. Garantir que hipóteses de renda/despesa ainda não contratadas permaneçam PREVISTO e nunca sejam silenciosamente promovidas a fato.
5. Integrar a projeção canônica aos mesmos conceitos, sem criar segundo motor de cálculo.
6. Salário recorrente da Kelly pode compor PREVISTO conforme regra documentada; comissão de Vinicius não entra automaticamente como prevista.
7. Pagamento de obrigação não pode duplicar despesa econômica já reconhecida por compra/fatura/transação.
8. Transferências internas, resgates e aplicações permanecem fora de renda/despesa operacional conforme invariantes já existentes.
9. Dashboard/forecast/relatórios afetados devem consumir a mesma fonte canônica e expor claramente REALIZADO/COMPROMETIDO/PREVISTO.
10. Divergências ou ausência de evidência devem ser visíveis; não criar ajuste sintético para fechar números.

## Critérios de aceite

- O sistema consegue distinguir e demonstrar, por período, REALIZADO, COMPROMETIDO e PREVISTO sem dupla contagem.
- Parcelas futuras contratadas do Slice 2 aparecem como COMPROMETIDO na projeção apropriada e não como REALIZADO.
- Obrigações pagas transitam para REALIZADO por evento persistido e auditável.
- Obrigação vencida/não paga permanece compromisso; atraso não vira gasto duplicado nem desaparece da projeção.
- Comissão de Vinicius não é criada/incluída automaticamente como PREVISTO.
- Salário recorrente da Kelly pode aparecer em PREVISTO quando houver configuração/regra já documentada.
- Forecast, Dashboard e relatórios usam o Financial Engine/snapshots canônicos e não fórmulas paralelas.
- Dados reais existentes são preservados; nenhuma migration destrutiva ou backfill silencioso.
- Qualquer migration necessária é aditiva, compatível com dados existentes e possui downgrade seguro/fail-safe.
- Documentação normativa conflitante é atualizada explicitamente.

## Invariantes obrigatórios

- REALIZADO, COMPROMETIDO e PREVISTO nunca são somados ou rotulados como se fossem o mesmo conceito.
- Hipótese não vira fato silenciosamente.
- Pagamento de fatura não vira nova despesa.
- Transferência interna não vira renda/despesa.
- Saldo confirmado continua soberano sobre reconstrução derivada no instante observado.
- Parcelamento distingue compra contratada, impacto mensal e parcelas futuras.
- Estorno neutraliza gasto sem apagar histórico.
- Sem dupla contagem entre obrigação, transação, compra de cartão e pagamento.

## Testes obrigatórios

1. Obrigação futura contratada aparece como COMPROMETIDO e não REALIZADO.
2. Pagamento confirmado converte o efeito pertinente para REALIZADO sem duplicar a despesa econômica.
3. Obrigação vencida e não paga continua visível/comprometida.
4. Parcela futura de cartão permanece COMPROMETIDO e só vira REALIZADO no momento correto conforme a semântica canônica.
5. Comissão de Vinicius não entra automaticamente no PREVISTO.
6. Salário recorrente da Kelly entra em PREVISTO apenas pelo mecanismo documentado.
7. Dashboard/forecast/report mantêm paridade com a fonte canônica.
8. Teste de regressão prova ausência de dupla contagem entre obrigação e transação associada.
9. Testes de migrations/Alembic quando houver alteração de schema.
10. Suíte completa, lint e todos os GitHub Actions aplicáveis verdes.

## Riscos a revisar

- Reutilização de enums/status legados com semântica incompatível.
- Mistura de competência, vencimento e data de pagamento.
- Contagem dupla de obrigação + transação.
- Promoção automática de previsão para realizado sem evidência.
- Inclusão automática indevida de comissão.
- Divergência entre forecast, Dashboard e relatórios.
- Backfill que altere fatos financeiros históricos.

## Proibições

- Não iniciar Slice 4.
- Não trabalhar MFA, hardening, observabilidade ou parser não bloqueador.
- Não inventar resgate/aplicação para cobrir déficit/sobra.
- Não criar renda/despesa sintética para reconciliar projeção.
- Não corrigir dados reais automaticamente.
- Não fazer merge.

## Definition of Done do Slice 3

Código, migrations se houver, testes, documentação e CI provam que REALIZADO/COMPROMETIDO/PREVISTO estão separados de ponta a ponta, obrigações e parcelas futuras estão corretamente refletidas na projeção, fatos reais permanecem soberanos e nenhum caminho cria dupla contagem ou promove hipótese a fato.