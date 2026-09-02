# Work Order — PR 7: Integrity UI and Monthly Close

## Fonte de verdade

Implementar estritamente conforme `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`, `docs/FINANCIAL_RULES.md`, `docs/FINANCIAL_INVARIANTS.md`, `docs/ROADMAP.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY.md` e contratos executáveis existentes. Em divergência, levantar challenge técnico no PR com evidência antes de alterar regra ou escopo.

## Executor e autoridade

Claude é o executor deste PR. Claude **não pode fazer merge**, alterar regra financeira não documentada, fabricar fatos, resolver divergências determinísticas por IA, fazer auto-fix destrutivo, apagar histórico, recategorizar dados reais silenciosamente ou reduzir cobertura para fazer testes passarem. Claude pode e deve contestar este Work Order ou revisão do engenheiro quando houver evidência verificável de solução superior ou contradição normativa.

## Objetivo

Entregar exatamente o **PR 7 — Integrity UI and Monthly Close** da divisão definitiva do plano: tornar a integridade visível e operável, com resolução humana auditável e fechamento mensal baseado em snapshot/runs/findings já existentes, preservando o Financial Engine e Integrity Engine como autoridades determinísticas.

## Escopo obrigatório

1. **Tela/menu Integridade** com UX mobile sem rolagem lateral, respeitando `INTEGRITY_UI_ENABLED` e sem reimplementar cálculo financeiro no frontend.
2. Exibir status geral, score explicado, data da última execução, `trusted_for_projection`, `trusted_for_reports`, cards aplicáveis, findings filtráveis, reconciliações, fechamento mensal e ação “Executar auditoria completa”.
3. **Detalhe de finding**: problema, impacto, expected/actual/difference quando disponíveis, invariant e versão, cálculo/evidência, entidades/documentos, lineage, histórico, ação recomendada e observação Codex estritamente separada do resultado determinístico.
4. **Ações humanas de lifecycle** necessárias nesta fatia, com motivo obrigatório e audit trail (`acknowledge`, `resolve`, `ignore`, `false-positive` conforme contratos aplicáveis). Resolver/ignorar finding não pode alterar automaticamente `Transaction`, `Document`, snapshot ou outro fato financeiro.
5. **Banner global para BLOCK**, persistente mas não bloqueante para navegação/correção. Dashboard, relatório e projeção devem refletir o mesmo trust state canônico, sem recalcular score/status no frontend.
6. **Monthly Financial Close** com `monthly_financial_closes` e estados normativos `open|review_required|trusted`, vinculando `snapshot_id` e `integrity_run_id`, com `closed_at/by`, `reopened_at/by` e `reason` quando aplicável.
7. APIs normativas do fechamento: `GET /api/monthly-closes/{period}`, `POST /api/monthly-closes/{period}/run`, `/trust`, `/reopen`, mantendo separação entre executar checks, confiar e reabrir.
8. Auditoria completa/manual deve usar `integrity_runs`; não introduzir Redis/worker obrigatório nesta etapa.
9. Mensagens de ausência/incompletude sem falsa precisão: saldo sem data, documento não reconciliável, período incompleto, Codex indisponível e score sem checks devem permanecer explicitamente `unknown`/indisponíveis, nunca convertidos em 100%/healthy.

## Critérios de aceite

- UI consome APIs/contratos canônicos; não contém fórmula financeira duplicada.
- Nenhum finding BLOCK/CRITICAL é escondido por paginação/filtro padrão ou por indisponibilidade do Codex.
- Score/status/trust exibidos são exatamente os produzidos pelo Integrity Engine para o escopo/período correspondente.
- Um fechamento só pode ficar `trusted` quando os gates determinísticos aplicáveis estiverem satisfeitos; `unknown`, BLOCK, CRITICAL ou pendência material incompatível com confiança impede `trusted` e mantém `review_required`/estado apropriado.
- Reabrir fechamento exige motivo e preserva histórico; não apaga snapshot, run, finding, reconciliação ou evidência anterior.
- Ações em findings exigem autenticação, isolamento por `household_id`, autorização adequada e audit event com before/after/reason/trace quando aplicável.
- Codex continua sem autoridade para status, score, gate, resolução ou fechamento.
- Nenhuma migration destrutiva; upgrades preservam dados existentes e downgrade da revisão deve ser seguro quando implementado.
- Compatibilidade com dashboard, relatórios, projeção e Advisor existentes deve ser preservada.

## Invariants e gates prioritários

Respeitar todos os INV-001..INV-022. Nesta fatia, testar especialmente o comportamento de exposição/lifecycle de findings, status consolidado, `unknown` sem falsa precisão, trust gates de projeção/relatórios, BLOCK persistente e consistência com snapshot/lineage. Não alterar a semântica de invariant para acomodar UI ou close.

## Testes obrigatórios

- unit/integration para lifecycle de findings e autorização por household/role;
- monthly close: criação/run, `review_required`, transição segura para `trusted`, reabertura com motivo e histórico;
- tentativa de confiar com BLOCK/CRITICAL/unknown material deve falhar de forma determinística;
- regressão garantindo que ações de finding não alteram dados financeiros automaticamente;
- UI/endpoint para score sem checks exibe `—`/unknown, não 100%; Codex indisponível não altera resultado determinístico;
- banner BLOCK e trust state coerentes com backend;
- mobile sem rolagem lateral relevante;
- Ruff, pytest completo, sintaxe JavaScript, testes Node existentes, PowerShell, Docker app/advisor e `docker compose config` no CI.

## Migrations e dados

A criação de `monthly_financial_closes` deve ser aditiva. Não apagar nem reescrever transações, documentos, snapshots, findings ou reconciliações existentes. Sem backfill destrutivo nesta fatia. Toda correção posterior cria nova evidência/estado e preserva histórico.

## Riscos a controlar

- transformar `unknown` em confiança visual;
- divergência entre UI e status canônico;
- resolução automática de findings por ação de interface;
- permitir `trusted` com gate material aberto;
- IDOR/vazamento entre households;
- esconder BLOCK por filtros/paginação;
- quebrar mobile ou consumidores existentes;
- acoplar Monthly Close ao Codex.

## Fora de escopo

PR 8 (CI financeiro completo, PostgreSQL/Alembic ampliado, backfill final, datasets finais e documentação final) permanece para a próxima fatia. Não antecipar PR 8 exceto ajuste mínimo indispensável para manter CI atual verde.

## Condição de entrega

Atualizar o PR com resumo, problema, solução, arquitetura, arquivos, migration/backfill, riscos, testes, screenshots da UI, CI, documentação, rollback, invariants afetados e compatibilidade de dados existentes. Ao concluir, solicitar revisão do engenheiro e **não fazer merge**.