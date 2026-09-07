# Work Order — Go-live manual slice 3: Contas a pagar + Pagamento de fatura

## Executor

Claude é o executor deste slice. O engenheiro responsável revisará o resultado antes de qualquer merge.

Claude **não pode fazer merge**, alterar regras financeiras não documentadas, mudar invariantes para acomodar testes, corrigir dados reais automaticamente, reduzir cobertura, introduzir cálculo/reconciliação paralelos ou dar a Claude/Codex/Advisor autoridade sobre fatos determinísticos. Claude **pode e deve contestar tecnicamente** este Work Order quando houver evidência concreta; divergência não resolvida bloqueia merge.

## Posição no roadmap

Este é exatamente o **slice 3** de `docs/GO_LIVE_MANUAL_UX_PLAN.md`, depois dos slices 1 e 2 já mesclados:

**Contas a pagar + Pagamento de fatura — centro de obrigações, quitação manual e fluxo combinado com origem de recursos confirmada pelo usuário.**

Não antecipar o slice 4 (`Lançamentos como livro-razão`) nem o slice 5 (`E2E + Go-live`).

## Fonte de verdade

Revisar antes de implementar:

- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/ROADMAP.md`;
- `docs/GO_LIVE_MANUAL_UX_PLAN.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`;
- serviços atuais de obrigações, transações, reconciliação de pagamento de cartão, transferências, snapshots, projeção, auditoria e duplicidade.

## Objetivo e escopo

Entregar fluxos estruturados para:

1. **Contas a pagar**: apresentar obrigações canônicas existentes com estados que possam ser provados pelos dados atuais (a vencer, próximas do vencimento, vencidas e pagas quando houver fato de quitação comprovado), sem inventar status ou saldos.
2. **Pagamento manual de fatura**: cartão/fatura, valor pago, conta pagadora, data e confirmação humana explícitos. O pagamento é `reconciliation`, nunca nova despesa (INV-002).
3. Reutilizar `card_payment_reconciliation` e demais serviços canônicos existentes; não criar uma segunda política de pareamento, tolerância, janela ou reconciliação.
4. **Fluxo combinado opcional**: quando o usuário explicitamente escolher resgatar recursos antes de pagar a fatura, reutilizar o comando canônico de resgate do slice 2 e o comando canônico de pagamento deste slice, mantendo dois fatos auditáveis e sem inferir a origem dos recursos automaticamente.
5. Se já houver saldo suficiente ou não houver evidência confiável de insuficiência, não criar nem tratar resgate como fato consumado por inferência.
6. Preservar household isolation, autorização, audit trail, proveniência e compatibilidade com dados existentes.

## Critérios de aceite

- INV-002 preservado: pagamento de fatura tem efeito de nova despesa igual a zero; compras da fatura continuam sendo os fatos econômicos.
- Conta pagadora e cartão/fatura pertencem ao mesmo household; origem dos recursos é sempre confirmada pelo usuário.
- O vínculo pagamento ↔ fatura/transação usa o contrato canônico existente e permanece auditável; candidato ambíguo nunca é resolvido automaticamente.
- Pagamento parcial só pode ser implementado se o contrato normativo/código atual já o suportar de forma inequívoca; caso contrário, deve permanecer explicitamente fora do escopo, sem regra inventada.
- Fluxo combinado resgate → pagamento cria exatamente os fatos confirmados pelo usuário, sem duplicar despesa, receita ou saldo e sem mutar `FinancialProfile.investment_balance` automaticamente.
- Nenhuma operação corrige dados históricos, saldo ou reconciliação silenciosamente.
- Dashboard, snapshots, relatórios e projeção continuam usando serviços canônicos e não ganham cálculo paralelo.
- Migrations, se necessárias, devem ser aditivas, justificadas, PostgreSQL/Alembic-safe e compatíveis com dados existentes.
- Os 12 gates nomeados do CI devem passar no head final.

## Testes obrigatórios

Cobrir no mínimo:

- pagamento manual de fatura com saldo suficiente: `reconciliation`, zero nova despesa e audit trail;
- household isolation/autorização para cartão, fatura, obrigação e conta pagadora;
- vínculo determinístico quando há candidato único e comportamento de revisão quando há ambiguidade/ausência de evidência;
- ausência de dupla contagem entre compras da fatura e pagamento;
- fluxo combinado explicitamente confirmado: resgate patrimonial + pagamento, dois fatos separados e R$ 0 de nova despesa no pagamento;
- nenhuma criação automática de resgate quando não confirmado pelo usuário;
- ausência de mutação silenciosa de `FinancialProfile.investment_balance`/`AccountBalanceObservation`;
- estados de Contas a pagar derivados apenas de fatos disponíveis;
- comportamento de pagamento parcial conforme contrato vigente ou teste explícito de rejeição/fora de escopo;
- regressão de dashboard/snapshot/projeção;
- paridade entre tela estruturada e comandos backend canônicos;
- autenticação, household isolation e auditoria.

## Proibições e riscos

- Não contar pagamento de fatura como despesa.
- Não inferir conta pagadora, resgate, origem de recursos, fatura correspondente ou quitação por IA.
- Não criar segunda fórmula de reconciliação ou tolerância no frontend/backend.
- Não transformar ausência de evidência em `reconciled`/`paid`.
- Não usar o fluxo para ajustar automaticamente saldo real ou corrigir histórico.
- Não apagar evidência financeira para fazer a fatura fechar.
- Não enfraquecer INV-002 ou testes para acomodar a implementação.
- Não criar PR/branch dos slices 4/5 antes deste ser revisado e mesclado.

## Entrega esperada

No PR, Claude deve reportar: arquitetura escolhida, serviços canônicos reutilizados, qualquer Technical Challenge, suporte ou não a pagamento parcial com evidência normativa, migrations/dependências, compatibilidade de dados, invariantes cobertos, testes adicionados, resultado dos 12 gates e riscos residuais.

**Não fazer merge.**
