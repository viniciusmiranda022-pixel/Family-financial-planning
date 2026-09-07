# Work Order — Go-live manual, slice 1: Navegação + Entradas/Saídas

## Executor

Claude é o executor deste slice. O engenheiro responsável revisará integralmente a implementação antes de qualquer merge.

Claude **não pode fazer merge**, alterar regras financeiras não documentadas, mudar invariantes para fazer testes passarem, corrigir dados financeiros reais automaticamente, introduzir cálculos paralelos ou dar a Claude/Codex/Advisor autoridade sobre fatos determinísticos. Claude **pode e deve contestar** este Work Order ou orientações do engenheiro quando houver evidência concreta; divergência não resolvida bloqueia merge.

## Posição e fonte de verdade

Este é o **primeiro** dos cinco slices do item “go-live de uso manual / fluxos financeiros do dia a dia” da Fase 2.

Fontes obrigatórias, em conjunto:
- `docs/GO_LIVE_MANUAL_UX_PLAN.md`;
- `docs/ROADMAP.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`.

A ordem normativa do plano é: (1) Navegação + Entradas/Saídas; (2) Transferências + Aplicação/Resgate; (3) Contas a pagar + Pagamento de fatura; (4) Lançamentos como livro-razão; (5) E2E + Go-live. **Este PR implementa somente o slice 1.** Não antecipar `transfer`, pagamento de fatura/`reconciliation`, fluxo resgate→pagamento, centro de obrigações ou a transformação final de Lançamentos em livro-razão.

## Objetivo

Separar criação estruturada de receita/despesa da consulta atual, criando experiências explícitas de **Entradas** e **Saídas** que reutilizem os mesmos comandos e serviços financeiros canônicos já existentes.

## Escopo obrigatório

1. Criar navegação/telas estruturadas `Entradas` e `Saídas`.
2. `Entradas`: criação manual de receita econômica real, sem promover transferência, resgate ou estorno a receita.
3. `Saídas`: criação manual de despesa em conta/cartão, com categoria, data/competência e compra parcelada.
4. Parcelamento deve reutilizar os contratos/campos e a projeção canônica existentes; nenhum cálculo financeiro paralelo no frontend.
5. A prévia/UX de parcelamento deve apresentar dados derivados do backend canônico quando houver cálculo; não duplicar fórmula no navegador.
6. Fatos futuros projetados não podem duplicar parcela posteriormente observada/importada.
7. `Lançar agora` permanece atalho opcional e deve convergir para os mesmos comandos; não pode ser o único caminho estruturado.
8. `Lançamentos` pode permanecer como consulta existente até o slice 4, mas deixa de ser o formulário principal de criação de receita/despesa após este slice.
9. Preservar household isolation, autorização, auditoria e compatibilidade com dados existentes.

## Invariantes aplicáveis

- Receita só representa renda econômica real; transferências, resgates e estornos não podem virar `income` silenciosamente.
- **INV-016**: estorno não vira receita operacional comum.
- **INV-017**: compra no cartão preserva competência canônica e data original como linhagem.
- **INV-014/INV-015** permanecem válidos para qualquer fato observado que colida com importação/projeção.
- Fatos projetados não substituem nem duplicam fatos observados sem o mecanismo canônico já existente.
- Ausência de evidência continua `unknown`/revisão; não fabricar competência, origem ou vínculo.

## Critérios de aceite

1. Entradas e Saídas existem como caminhos estruturados separados e compreensíveis.
2. Receita e despesa usam o backend/motor canônico existente; nenhuma segunda política financeira.
3. Despesa à vista em conta e em cartão funcionam e aparecem corretamente nos canais derivados.
4. Compra parcelada persiste `installment_current`/`installment_total` e alimenta a projeção canônica sem duplicar fatos observados.
5. Household isolation, autorização e auditoria são testados.
6. Dados existentes não são reescritos nem auto-corrigidos.
7. Migration só é permitida se estritamente necessária ao slice 1; deve ser aditiva, reversível e Alembic/PostgreSQL-safe.
8. Os 12 gates do CI passam no head final: `lint`, `unit`, `financial-invariants`, `property-tests`, `parser-reconciliation`, `projection-parity`, `snapshot-channel-consistency`, `advisor-contract-security`, `frontend-syntax`, `docker-build`, `alembic-migration`, `integration-postgres`.

## Testes mínimos

- criação manual de receita por Entradas;
- despesa à vista por Saídas em conta;
- despesa em cartão;
- compra parcelada com campos corretos e projeção canônica;
- regressão demonstrando que fato observado não é duplicado por projeção futura;
- household isolation e autorização;
- auditoria das criações;
- paridade/regressão de dashboard/snapshot/report/projection aplicáveis;
- UI/sintaxe para os novos caminhos estruturados.

## Proibições

- Não implementar `transfer`, `reconciliation`, pagamento de fatura, Contas a pagar, fluxo resgate→pagamento ou o slice final E2E neste PR.
- Não criar segundo motor financeiro, segunda lógica de parcelamento ou cálculo paralelo no frontend.
- Não alterar regras/invariantes para acomodar UI/testes.
- Não reduzir cobertura nem enfraquecer asserts existentes.
- Não introduzir auto-fix destrutivo, migration destrutiva ou alteração silenciosa de dados reais.

## Entrega esperada

Ao concluir, reportar: arquitetura/UI/API alteradas; evidência de uso dos serviços canônicos; testes adicionados; migrations/dependências; compatibilidade; resultado dos 12 gates; riscos residuais; e qualquer novo Technical Challenge com evidência.

**Não fazer merge.**
