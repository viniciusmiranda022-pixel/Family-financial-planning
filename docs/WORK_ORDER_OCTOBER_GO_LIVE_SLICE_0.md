# Work Order — October Go-Live Rebaseline — Slice 0

## Objetivo

Produzir o inventário técnico e normativo necessário para executar o rebaseline de outubro sem inventar regras durante a implementação. Este slice é documental e de engenharia: **não altera comportamento financeiro, dados reais, schema produtivo nem UI funcional**.

Fonte normativa primária: `docs/OCTOBER_GO_LIVE_REBASELINE.md` no `main` a partir do commit `62a76fb981146736680358d699b57a4e9c4f15ce`.

Também devem ser confrontados, no mínimo: `README.md`, `docs/FINANCIAL_RULES.md`, `docs/FINANCIAL_INVARIANTS.md`, `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`, `docs/ARCHITECTURE.md`, `docs/INTELLIGENCE.md`, `docs/ROADMAP.md`, modelos e migrations atuais, motor financeiro/snapshots/projeções, obrigações, cartões/faturas/reconciliação, Advisor/Codex, API, frontend e testes que cristalizam a semântica anterior.

## Entregáveis obrigatórios

1. Criar `docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` com matriz `MANTER / ALTERAR / REMOVER / MIGRAR`, citando arquivo, símbolo, regra e teste afetado.
2. Mapear explicitamente as contradições entre o estado atual e o rebaseline, incluindo:
   - déficit operacional inferido virando uso automático do Privilège;
   - resultado positivo virando aplicação automática no Privilège;
   - qualquer projeção que recalcule ou sobrescreva saldo observado posterior;
   - comissão futura/projeção automática em desacordo com o rebaseline;
   - ciclo de cartão/fatura sem `open/closed/partially_paid/paid`, carry-forward, encargos, estorno e parcelamento pedidos;
   - limites atuais do Codex/Advisor versus typed actions, auditoria e validação desejadas;
   - navegação atual versus menu final.
3. Propor, sem implementar, o plano de dados/migration necessário para:
   - lifecycle de fatura;
   - principal carregado sem novo gasto;
   - estorno vinculado à compra original;
   - estados `REALIZADO / COMPROMETIDO / PREVISTO`;
   - investimentos/Studio e histórico de valuation;
   - action log do Assistente com mensagem, interpretação, before/after e undo;
   - templates dinâmicos de Entrada/Saída.
4. Propor contratos de API/serviço para os Slices 1–8, reutilizando estruturas existentes e evitando segundo motor financeiro.
5. Descrever estratégia de compatibilidade e migração dos dados existentes: nenhuma correção silenciosa; observações de saldo continuam soberanas; qualquer backfill deve ser explícito, auditável, idempotente quando aplicável e reversível/recuperável.
6. Criar matriz de testes P0 que mapeie as invariantes do rebaseline para: teste existente a preservar, teste existente a alterar e teste novo obrigatório.
7. Registrar sequenciamento técnico final dos Slices 1–8, dependências, riscos e critérios de entrada/saída.

## Critérios de aceite

- Nenhuma mudança executável de regra financeira neste PR.
- Nenhuma migration Alembic criada neste PR; somente plano de migration, se necessário.
- Nenhum dado real ou fixture com PII introduzido.
- Cada conflito relevante aponta evidência verificável em código, documento ou teste.
- Nenhuma regra é alterada apenas para “fazer teste passar”.
- Não há segundo motor de cálculo/reconciliação proposto.
- O saldo observado confirmado é tratado como fato soberano no instante observado.
- Transferência interna, aplicação, resgate e pagamento de fatura não são reclassificados como nova renda/despesa.
- Codex/Claude nunca vira autoridade factual sobre valores determinísticos nem recebe SQL arbitrário.
- Household isolation, auditoria, segurança e privacidade permanecem invariantes de arquitetura.
- Technical Challenges são documentados no PR quando houver conflito real entre a especificação e a arquitetura/dados.

## Testes e gates

Como o Slice 0 deve ser documental, não é necessário fabricar testes de produto. Ainda assim, o PR deve:

- não quebrar lint/documentação existente;
- não alterar ou reduzir cobertura;
- não modificar testes produtivos para acomodar regra ainda não implementada;
- listar exatamente quais testes existentes serão preservados/alterados e quais novos testes serão necessários nos Slices 1–8;
- manter os 12 gates existentes como requisito para qualquer slice executável subsequente.

## Riscos a analisar

- dupla contagem entre compra, fatura, pagamento e carry-forward;
- dupla subtração do Privilège quando existe saldo observado;
- projeção fabricando transações futuras;
- migration que reinterpreta fatos históricos sem confirmação;
- estorno apagando trilha histórica em vez de neutralizar economicamente a compra;
- duplicidade resolvida automaticamente sem decisão humana;
- regressão de compatibilidade com dados reais já importados;
- typed actions do Assistente bypassando o motor determinístico ou autorização;
- divergência entre Dashboard, Relatórios, snapshots e projeção.

## Proibições

- Não alterar fatos financeiros reais automaticamente.
- Não implementar Slices 1+ neste PR.
- Não criar migration destrutiva.
- Não remover auditoria ou lineage.
- Não transformar hipótese do Codex em fato financeiro.
- Não usar #83 ou #57 como base; ambos permanecem preservados e pausados.
- Claude não pode fazer merge.

## Relação de revisão

Claude é o executor e pode/deve contestar este Work Order ou interpretações do engenheiro quando houver evidência concreta de contradição, risco, solução inferior ou incompatibilidade com a documentação/código real. A objeção deve citar evidência verificável no PR. Divergência não resolvida bloqueia merge.

## Saída esperada do executor

Ao concluir, reportar no PR: branch/head, arquivos alterados, matriz de conflitos, migrations previstas, APIs/serviços afetados, testes afetados, Technical Challenges, riscos restantes e recomendação exata para iniciar o Slice 1.
