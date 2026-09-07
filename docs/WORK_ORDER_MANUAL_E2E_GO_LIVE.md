# Work Order — Go-live manual slice 5: E2E + Go-live

## Objetivo

Implementar exatamente o slice 5 de `docs/GO_LIVE_MANUAL_UX_PLAN.md`: validar, de ponta a ponta, que os fluxos financeiros manuais entregues nos slices 1–4 estão prontos para uso diário sem violar regras financeiras, invariantes, household isolation, auditoria, compatibilidade ou paridade entre UI e backend.

Este slice é de **validação, integração e correção mínima de gaps comprovados pelos testes**. Não é autorização para inventar novas funcionalidades ou uma nova política financeira.

## Fonte de verdade

Antes de implementar, leia e confronte este Work Order com:

- `docs/GO_LIVE_MANUAL_UX_PLAN.md`;
- `docs/ROADMAP.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`;
- Work Orders e contratos já entregues nos slices 1–4.

Documentação normativa e comportamento determinístico verificável prevalecem. Se este Work Order estiver incorreto, incompleto ou tecnicamente inferior, abra **Technical Challenge** no PR com evidência antes de alterar a direção.

## Escopo obrigatório

Validar por testes completos API/UI, usando os mesmos serviços canônicos já entregues, os fluxos mínimos documentados:

1. despesa à vista em conta;
2. despesa em cartão;
3. compra parcelada com projeção futura e posterior substituição da projeção por fato observado sem duplicação;
4. receita;
5. transferência entre contas próprias;
6. aplicação em investimento;
7. resgate de investimento;
8. pagamento manual de fatura com saldo suficiente;
9. pagamento manual de fatura precedido por resgate do Privilège DI, somente quando o usuário confirmar explicitamente ambos os fatos;
10. pagamento parcial de fatura somente se já houver contrato canônico/normativo inequívoco; caso contrário deve permanecer explicitamente fora do escopo;
11. estorno/refund sem virar receita indevida;
12. consulta de todos os fatos anteriores no livro-razão com proveniência correta e sem dupla contagem.

Também validar:

- household isolation em todas as leituras e mutações envolvidas;
- autenticação/autorização;
- trilha de auditoria dos comandos mutáveis;
- compatibilidade com dados existentes e estados legados/inconsistentes cobertos pelos contratos atuais;
- paridade entre UI e backend: navegador não calcula classificação, competência, projeção, reconciliação, deduplicação ou regra financeira paralela;
- mobile/usabilidade sem regressão funcional relevante;
- projeção e substituição de parcela observada sem criar fatos sintéticos persistidos ou dupla contagem;
- ausência de regressão em snapshots, relatórios, Advisor e gates de confiança decorrente dos fluxos manuais.

## Critérios de aceite

1. Todos os 12 fluxos mínimos aplicáveis possuem cobertura E2E/API/UI suficiente para provar o comportamento canônico.
2. INV-001, INV-002, INV-003, INV-004 e demais invariantes afetados permanecem verdadeiros em cada fluxo e no conjunto.
3. Pagamento de fatura nunca cria nova despesa; transferência/aplicação/resgate nunca criam receita ou despesa.
4. Fato observado substitui projeção da mesma série quando o contrato canônico assim determina; nunca coexistem como dupla obrigação por erro do fluxo manual.
5. Origem de recursos, conta pagadora, resgate, transferência ou pagamento nunca são inferidos ou executados automaticamente por Claude/Codex/Advisor.
6. Nenhum teste de E2E pode ser feito passar alterando regra financeira, reduzindo cobertura, ignorando erro de reconciliação ou normalizando dados reais silenciosamente.
7. Nenhuma migration destrutiva. Se um gap exigir migration, ela deve ser estritamente necessária, aditiva, Alembic/PostgreSQL-safe e compatível com dados existentes.
8. O ledger do slice 4 exibe os fatos resultantes com origem/lineage coerentes e sem vazamento cross-household.
9. Fluxos mutáveis relevantes deixam auditoria verificável e preservam guards de imutabilidade/lineage existentes.
10. O uso diário manual pode ser declarado pronto somente quando os 12 gates do CI estiverem verdes no mesmo head final e não houver bloqueio técnico aberto.

## Testes obrigatórios

Adicionar ou consolidar testes E2E/API/UI que cubram os fluxos 1–12 acima, com assertions financeiras explícitas. Reutilizar fixtures e serviços canônicos existentes; não duplicar o motor financeiro no teste.

Cobrir pelo menos:

- resultados operacionais antes/depois de transferências, aplicações, resgates e pagamento de fatura;
- projeção de parcelamento e posterior observação da próxima parcela sem dupla contagem;
- lineage de transferências e reconciliações;
- proveniência no livro-razão;
- household isolation, inclusive referências inconsistentes quando aplicável;
- auditoria;
- autorização;
- mobile/frontend syntax e contratos de UI;
- regressão de dados existentes.

No head final, executar e reportar os 12 gates do CI:

`lint`, `unit`, `financial-invariants`, `property-tests`, `parser-reconciliation`, `projection-parity`, `snapshot-channel-consistency`, `advisor-contract-security`, `frontend-syntax`, `docker-build`, `alembic-migration`, `integration-postgres`.

## Riscos a controlar

- teste E2E mascarar regra financeira incorreta;
- criação de um segundo fluxo de cálculo/reconciliação apenas para a UI;
- dupla contagem entre projeção e fato observado;
- resgate ou origem de recursos inferidos automaticamente;
- alteração destrutiva de fato financeiro ou lineage;
- vazamento cross-household;
- drift de escopo para itens posteriores da Fase 3/4;
- declarar go-live com CI parcial ou dívida técnica que comprometa integridade financeira.

## Proibições

- Não alterar regras financeiras ou invariantes para fazer teste passar.
- Não reduzir cobertura ou remover regressões existentes.
- Não fazer auto-fix de dados financeiros reais.
- Não apagar/regravar silenciosamente fatos históricos.
- Não criar migration destrutiva.
- Não criar cálculo, classificação, deduplicação, reconciliação ou projeção paralelos.
- Não persistir fatos sintéticos de projeção como se fossem observados.
- Não inferir origem de recursos ou executar resgate/transferência/pagamento sem confirmação humana.
- Claude/Codex/Advisor não têm autoridade sobre fatos determinísticos.
- Não antecipar itens posteriores do roadmap.
- **Claude é o executor e não pode fazer merge.**

## Relação de revisão

Claude pode e deve contestar tecnicamente este Work Order, comentários de revisão ou interpretação arquitetural quando houver evidência concreta em documentação, código, testes, segurança, integridade financeira ou menor risco de regressão. Divergência não resolvida bloqueia merge.
