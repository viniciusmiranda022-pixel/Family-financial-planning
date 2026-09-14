# Work Order — October Go-Live Slice 7: Gastos & Economia + Relatórios

**Parent:** P0 #87 — October Go-Live Rebaseline  
**Executor:** Claude  
**Integration authority:** engenheiro responsável  
**Priority:** P0; não iniciar Slice 8 antes de aprovação e merge deste slice.

## Fonte normativa

Ordem de precedência para semântica de produto:

1. `docs/OCTOBER_GO_LIVE_REBASELINE.md`, especialmente §§3, 12, 13, 14, 15, 16, 17, 18, 19 e 21;
2. este Work Order;
3. `docs/FINANCIAL_RULES.md`;
4. `docs/FINANCIAL_INVARIANTS.md`;
5. `docs/ARCHITECTURE.md`;
6. `docs/INTELLIGENCE.md`;
7. `docs/ROADMAP.md` e Work Orders integrados dos Slices 0–6.

Em conflito de semântica, o rebaseline de outubro é a direção normativa alvo. Não substituir cálculo canônico por aproximação de UI nem introduzir segunda fonte de verdade.

## Objetivo

Entregar as superfícies finais de **Gastos & Economia** e **Relatórios** para uso cotidiano, usando exclusivamente fatos/cálculos já canônicos dos slices anteriores e mantendo separadas as perguntas financeiras distintas: consumo, fluxo de caixa, transferências internas, patrimônio, compromissos e previsões.

O Codex pode analisar, comparar, validar e sugerir economia, mas não pode inventar fato interno nem misturar referência externa com dado da família.

## Ordem obrigatória de trabalho

1. Inventariar a implementação atual de `Gastos & Economia`, `Relatórios`, livro-razão, análises históricas, consultas do Assistente e qualquer cálculo duplicado no frontend/backend.
2. Mapear cada número exibido para seu produtor canônico já integrado; remover/reutilizar derivações paralelas quando houver conflito.
3. Implementar `Gastos & Economia` com visão mensal/histórica e oportunidades contextuais, sem reclassificar silenciosamente fatos.
4. Implementar `Relatórios` consumindo contratos canônicos de caixa, consumo, cartões, obrigações, estados financeiros e patrimônio.
5. Integrar análise Codex somente sobre payload sanitizado e fatos persistidos; referências externas devem permanecer explicitamente separadas.
6. Cobrir regressões e paridade entre Dashboard/Relatórios/Assistente quando responderem à mesma pergunta.
7. Executar CI completo e responder no PR com evidências e qualquer Technical Challenge fundamentado.

## Semântica obrigatória

### Gastos & Economia

Deve mostrar, no mínimo:

- gastos realizados do mês;
- categorias;
- origem por conta/cartão;
- comparação histórica;
- tendências;
- oportunidades de economia;
- recomendações do Codex baseadas nos dados reais da família.

`Gasto` significa consumo/despesa econômica real. Não incluir como gasto:

- pagamento de fatura já originada por compras;
- transferência entre contas próprias;
- aplicação/resgate Privilège ↔ Conta Corrente;
- aporte patrimonial como consumo;
- COMPROMETIDO/PREVISTO ainda não realizado.

Quando houver análise/pesquisa externa, a apresentação deve separar claramente:

1. **Seus dados**;
2. **Referências externas**;
3. **Análise**;
4. **Recomendação**.

Referência externa nunca substitui fato interno e hipótese do Codex nunca vira transação/saldo/categoria confirmada sem fluxo explícito de escrita.

### Relatórios

Relatórios devem oferecer, conforme contexto e sem dupla contagem:

- livro-razão completo;
- fluxo de caixa por conta;
- gastos/consumo por competência;
- cartões e faturas;
- transferências internas;
- patrimônio e evolução;
- investimentos;
- obrigações;
- REALIZADO x COMPROMETIDO x PREVISTO;
- auditoria/reconciliações relevantes.

A mesma movimentação pode aparecer em visões diferentes, mas **não pode ser somada duas vezes na mesma métrica**.

### Perguntas diferentes exigem métricas diferentes

- **Quanto saiu desta conta?** → débitos físicos daquela conta, incluindo pagamento de fatura e transferências.
- **Quanto eu gastei?** → consumo/despesas reais, sem contar pagamento de fatura novamente.
- **Quanto tenho/patrimônio?** → saldos/ativos reais, com saldo confirmado soberano no instante observado e investimentos usando apenas `current_value`.
- **Quanto ainda devo/pagarei?** → COMPROMETIDO; não fabricar transações futuras.
- **Quanto está previsto?** → PREVISTO; não misturar com REALIZADO/COMPROMETIDO.

Toda UI/relatório deve deixar claro qual pergunta o número responde.

## Reuso obrigatório de motores/contratos

Não criar segundo motor financeiro. Reutilizar os serviços/contratos já integrados para:

- publicação canônica do Dashboard/Financial Engine;
- saldos observados/confirmados e reconciliação;
- cartão/fatura e pagamento;
- obrigações/projeção financeira;
- estados `REALIZADO` / `COMPROMETIDO` / `PREVISTO`;
- investimentos via `app.services.investments.investments_summary` e patrimônio via `household_patrimony_summary`;
- auditoria e AssistantActionEvent;
- deduplicação fail-safe/humana.

Se o relatório precisar de nova agregação, ela deve viver em serviço canônico reutilizável e ser testada contra as fontes de fato; não duplicar fórmula no frontend.

## Codex / inteligência

O Codex pode:

- interpretar pergunta;
- resumir e comparar fatos sanitizados;
- recalcular/validar totais de forma independente;
- explicar divergências;
- detectar tendências e oportunidades de economia;
- pesquisar referências externas quando necessário.

O Codex não pode:

- inventar saldo, renda, gasto, juros, data ou categoria factual;
- tratar PREVISTO/COMPROMETIDO como REALIZADO;
- apagar/fundir provável duplicidade;
- escrever SQL arbitrário;
- alterar dados financeiros apenas para fazer relatório “bater”.

Quando motor determinístico e Codex divergirem de forma material, exibir/sinalizar a divergência; não publicar silenciosamente como número confiável.

## Dados reais e compatibilidade

- Nenhuma migração é desejada por padrão neste slice. Se surgir necessidade real, documentar causa, impacto, backfill e rollback antes de integrar.
- Preservar integralmente fatos existentes dos Slices 1–6.
- Não criar lançamentos sintéticos para preencher relatórios.
- Não reclassificar automaticamente histórico real apenas para encaixar uma visualização.
- Filtros por household são obrigatórios em todo endpoint novo.

## Critérios de aceite

1. `Gastos & Economia` mostra gasto realizado sem incluir pagamentos de fatura, transferências internas, aporte patrimonial ou fatos apenas futuros como consumo.
2. Visão histórica/mensal usa competência correta e permite comparação sem dupla contagem.
3. Origem por conta/cartão permanece uma dimensão explicativa e não altera o conceito de gasto.
4. Recomendações do Codex se baseiam em dados reais sanitizados; referências externas aparecem separadas.
5. Relatórios distinguem fluxo de caixa, consumo, transferências, patrimônio, compromissos e previsões.
6. REALIZADO/COMPROMETIDO/PREVISTO aparecem separados nas visões aplicáveis.
7. Patrimônio/Investimentos nos relatórios reutilizam os cálculos canônicos do Slice 6; `historical_cost`/`expected_receivable_value` não inflam patrimônio atual.
8. Pagamento de fatura aparece no fluxo de caixa quando apropriado, mas nunca como segundo gasto.
9. Transferência interna aparece em fluxo/razão quando apropriado, mas nunca como renda/despesa.
10. Saldos confirmados continuam soberanos; divergência reconstruída é exibida/investigável, não mascarada.
11. Livro-razão/auditoria preserva rastreabilidade sem expor jargão técnico desnecessário na visão principal.
12. Não há segundo motor de cálculo no frontend; paridade com Dashboard/serviços canônicos é testada.
13. CI aplicável integralmente verde.

## Testes obrigatórios

No mínimo:

- gasto do mês exclui pagamento de fatura como novo gasto;
- gasto exclui transferência interna Privilège/Corrente;
- aporte patrimonial não vira consumo;
- compra parcelada contabiliza somente impacto realizado do período e deixa parcelas futuras como COMPROMETIDO;
- estorno neutraliza gasto sem apagar histórico;
- comparação histórica respeita competência;
- fluxo de caixa por conta inclui débitos físicos apropriados sem alterar gasto econômico;
- relatório de patrimônio = caixa canônico + `current_value` dos ativos, sem custo histórico/projeção futura;
- relatório de investimentos reutiliza `investments_summary`/derivações canônicas;
- REALIZADO/COMPROMETIDO/PREVISTO não se misturam;
- comissão não aparece como PREVISTO automático; salário recorrente configurado pode aparecer como PREVISTO;
- saldo confirmado soberano permanece igual entre Dashboard e relatório no mesmo instante/contexto;
- paridade Dashboard ↔ Relatórios para métricas compartilhadas;
- household isolation/autorização;
- análise Codex mantém separação `Seus dados` / `Referências externas` / `Análise` / `Recomendação` quando usar fonte externa;
- divergência motor x Codex é sinalizada em vez de sobrescrita.

## Riscos a revisar antes do merge

- relatório somar pagamento de fatura como despesa novamente;
- transferência interna entrar em renda/gasto;
- frontend rederivar totais já calculados no backend;
- histórico por competência usar data de pagamento errada para consumo;
- patrimônio somar custo histórico/valor previsto;
- COMPROMETIDO/PREVISTO contaminarem realizado;
- referência externa aparecer como se fosse dado da família;
- Codex produzir recomendação com fatos não presentes no payload;
- household leakage;
- drift de escopo para Slice 8 (migração/reconciliação real/smoke).

## Proibições

- Não iniciar reconciliação/migração real do Slice 8 neste PR.
- Não fabricar fatos para completar gráfico/relatório.
- Não fazer correção silenciosa de dado real.
- Não criar segundo motor financeiro/contábil.
- Não permitir que análise Codex altere persistência fora dos typed actions já autorizados.
- Não desviar prioridade para MFA, hardening, observabilidade ou parser não bloqueador.

## Definition of Done

O Slice 7 só pode ser integrado quando Gastos & Economia e Relatórios responderem às perguntas financeiras corretas com métricas canônicas, sem dupla contagem, com estados financeiros separados, patrimônio coerente, recomendações Codex claramente segregadas de referências externas e CI integralmente verde.

**Claude não pode fazer merge.**
