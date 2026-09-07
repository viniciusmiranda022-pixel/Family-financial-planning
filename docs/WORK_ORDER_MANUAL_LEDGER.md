# Work Order — Go-live manual slice 4: Lançamentos como livro-razão

## Objetivo

Implementar exatamente o slice 4 de `docs/GO_LIVE_MANUAL_UX_PLAN.md`: transformar **Lançamentos** no livro-razão unificado de consulta e auditoria dos fatos financeiros já registrados, removendo a ambiguidade entre criar operação e consultar histórico.

## Fonte de verdade

Antes de implementar, leia e confronte este Work Order com:

- `docs/GO_LIVE_MANUAL_UX_PLAN.md`;
- `docs/ROADMAP.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`.

Documentação normativa e comportamento determinístico verificável prevalecem. Se este Work Order estiver incorreto, incompleto ou tecnicamente inferior, abra **Technical Challenge** no PR com evidência antes de alterar a direção.

## Escopo obrigatório

- Fazer de **Lançamentos** uma superfície primariamente de consulta/auditoria, não o principal formulário para criação estruturada.
- Reunir fatos originados por Entradas, Saídas, Contas a pagar, Transferências, Lançar agora e importações.
- Expor proveniência suficiente para distinguir, sem inferência da UI, origem/canal e dados auditáveis já persistidos.
- Manter filtros úteis usando contratos backend existentes; não recalcular classificação, competência, reconciliação, duplicidade, valores ou status no navegador.
- Preservar os comandos estruturados já entregues nos slices 1–3 e seus serviços canônicos.
- Correções/exclusões existentes só podem permanecer acessíveis quando o contrato atual permite; linhas vinculadas, reconciliadas, importadas ou protegidas por invariantes não podem ganhar atalhos destrutivos.
- Household isolation, autenticação, autorização e auditoria são obrigatórios em toda leitura/mutação tocada por este slice.

## Critérios de aceite

1. Um usuário consegue consultar no livro-razão fatos de receita, despesa, transferência, aplicação, resgate, estorno/refund, reconciliação/pagamento de fatura e importações já persistidos.
2. A UI mostra proveniência a partir de fatos do backend e não inventa rótulos financeiros que mudem a semântica canônica.
3. Transferência, aplicação, resgate e pagamento de fatura continuam sem efeito operacional indevido (INV-001/002/003/004).
4. Duplicidade, canonicalidade, exclusão, competência e lineage continuam respeitando os serviços/regras atuais; nenhuma regra é duplicada no frontend.
5. Nenhuma consulta vaza conta, transação, documento, categoria ou metadado de outro household, inclusive sob estado inconsistente de referência.
6. Nenhum fluxo de edição/exclusão permite quebrar vínculo de transferência/reconciliação ou alterar silenciosamente fato protegido.
7. Dados existentes continuam compatíveis; migration só se for estritamente necessária, aditiva, Alembic/PostgreSQL-safe e não destrutiva.
8. Mobile continua utilizável sem regressão funcional relevante.

## Testes obrigatórios

Adicionar regressões API/UI suficientes para provar:

- presença no livro-razão de fatos produzidos pelos slices 1–3 e por importação;
- proveniência/filtros coerentes com o backend;
- ausência de dupla contagem ou reclassificação introduzida pelo ledger;
- household isolation, inclusive referências inconsistentes quando aplicável;
- guards de edição/exclusão preservados para transferências e reconciliações vinculadas;
- autorização e auditoria das mutações ainda permitidas;
- compatibilidade com dados existentes;
- sintaxe/frontend e regressão mobile aplicável.

No head final, executar e reportar os 12 gates do CI: `lint`, `unit`, `financial-invariants`, `property-tests`, `parser-reconciliation`, `projection-parity`, `snapshot-channel-consistency`, `advisor-contract-security`, `frontend-syntax`, `docker-build`, `alembic-migration`, `integration-postgres`.

## Riscos a controlar

- UI criar uma segunda classificação ou regra financeira;
- vazamento cross-household via joins/relações ORM não escopados;
- edição/exclusão destrutiva de fatos vinculados;
- ocultar proveniência ou transformar estado desconhecido/inconsistente em sucesso;
- regressão de navegação mobile;
- drift para o slice 5.

## Proibições

- Não alterar regras financeiras ou invariantes para fazer teste passar.
- Não reduzir cobertura.
- Não fazer auto-fix de dados financeiros reais.
- Não apagar/regravar silenciosamente fatos históricos.
- Não criar migration destrutiva.
- Não criar cálculo, deduplicação, reconciliação ou classificação paralelos.
- Claude/Codex/Advisor não têm autoridade sobre fatos determinísticos.
- **Não antecipar o slice 5 (E2E + Go-live).**
- **Claude é o executor e não pode fazer merge.**

## Relação de revisão

Claude pode e deve contestar tecnicamente este Work Order, comentários de revisão ou interpretação arquitetural quando houver evidência concreta em documentação, código, testes, segurança, integridade financeira ou menor risco de regressão. Divergência não resolvida bloqueia merge.