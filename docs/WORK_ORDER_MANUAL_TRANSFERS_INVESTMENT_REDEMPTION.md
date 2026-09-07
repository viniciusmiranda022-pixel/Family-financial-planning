# Work Order — Go-live manual slice 2: Transferências + Aplicação/Resgate

## Executor

Claude é o executor deste slice. O engenheiro responsável revisará o resultado antes de qualquer merge.

Claude **não pode fazer merge**, alterar regras financeiras não documentadas, mudar invariantes para acomodar testes, corrigir dados reais automaticamente, reduzir cobertura, introduzir cálculo/reconciliação paralelos ou dar a Claude/Codex/Advisor autoridade sobre fatos determinísticos. Claude **pode e deve contestar tecnicamente** este Work Order quando houver evidência concreta; divergência não resolvida bloqueia merge.

## Posição no roadmap

Este é exatamente o **slice 2** de `docs/GO_LIVE_MANUAL_UX_PLAN.md`, imediatamente após o slice 1 já mesclado:

**Transferências + Aplicação/Resgate — experiência estruturada para movimentos patrimoniais sem receita/despesa.**

Não antecipar o slice 3 (`Contas a pagar + Pagamento de fatura`), slice 4 (`Lançamentos como livro-razão`) ou slice 5 (E2E + Go-live).

## Fonte de verdade

Revisar antes de implementar:

- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/ROADMAP.md`;
- `docs/GO_LIVE_MANUAL_UX_PLAN.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`;
- serviços atuais de transações, snapshots, projeção, classificação, auditoria e duplicidade.

## Objetivo e escopo

Entregar fluxos manuais estruturados para:

1. **Transferência entre contas da mesma família**: origem e destino explicitamente confirmados pelo usuário, duas pernas rastreáveis e atomicamente vinculadas, efeito operacional líquido zero (INV-001), sem criar receita/despesa ou consumo de teto.
2. **Aplicação**: movimento patrimonial, nunca despesa operacional nem consumo de teto (INV-003).
3. **Resgate**: movimento patrimonial, nunca receita operacional (INV-004).
4. Reutilizar os comandos/serviços canônicos já existentes; `Lançar agora`, API e telas estruturadas devem convergir para a mesma semântica.
5. Preservar household isolation, autorização, audit trail, proveniência e compatibilidade com dados existentes.

## Critérios de aceite

- Transferência cria e preserva as duas pernas de forma atômica, com vínculo determinístico/rastreável e sem dupla contagem.
- Conta de origem e destino devem existir, estar ativas, pertencer ao mesmo household e ser distintas; nenhum vínculo é inferido por IA.
- Falha parcial não pode deixar apenas uma perna persistida.
- Aplicação e resgate usam os tipos financeiros canônicos existentes e não alteram silenciosamente `FinancialProfile`, saldo ou fatos históricos por fora do motor documentado.
- Dashboard, snapshots, relatórios e projeção continuam respeitando INV-001/003/004 e não ganham cálculo paralelo.
- Exclusão/edição de transferências não pode quebrar a atomicidade ou deixar uma perna órfã; qualquer comportamento permitido deve ser explícito, auditável e testado.
- Nenhuma migration destrutiva. Se uma migration aditiva for realmente necessária, justificar no PR, provar upgrade/downgrade e compatibilidade com dados existentes.
- Os 12 gates nomeados do CI devem passar no head final.

## Testes obrigatórios

Cobrir no mínimo:

- transferência origem→destino com duas pernas e efeito operacional zero;
- rejeição de mesma conta como origem/destino;
- rejeição de conta de outro household e conta inexistente/inativa;
- atomicidade em falha;
- auditoria e rastreabilidade do vínculo;
- comportamento de edição/exclusão sem órfão ou mutação silenciosa;
- aplicação sem efeito de despesa/teto (INV-003);
- resgate sem efeito de receita operacional (INV-004);
- regressão de dashboard/snapshot/projeção para esses movimentos;
- autenticação e household isolation;
- paridade entre tela estruturada e comando backend canônico.

## Proibições e riscos

- Não implementar pagamento de fatura/reconciliation neste slice.
- Não criar segunda fórmula financeira no frontend ou backend.
- Não inferir conta de origem/destino, investimento ou resgate por IA.
- Não usar aplicação/resgate para ajustar automaticamente saldos reais ou corrigir divergências históricas.
- Não apagar evidência financeira para “fechar” totais.
- Não enfraquecer testes/invariantes para acomodar a implementação.
- Não criar PR/branch dos próximos slices antes deste ser revisado e mesclado.

## Entrega esperada

No PR, Claude deve reportar: arquitetura escolhida, serviços canônicos reutilizados, qualquer Technical Challenge, migrations/dependências, compatibilidade de dados, invariantes cobertos, testes adicionados, resultado dos 12 gates e riscos residuais.

**Não fazer merge.**