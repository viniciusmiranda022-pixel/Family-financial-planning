# Work Order — Fase 3: worker assíncrono para OCR e áudio

## Objetivo

Implementar exatamente o primeiro item da Fase 3 de `docs/ROADMAP.md`: mover processamento potencialmente demorado de OCR e transcrição de áudio para um worker assíncrono/fila, preservando integralmente os contratos financeiros, de captura, segurança, privacidade e auditoria existentes.

Este slice é de infraestrutura/orquestração. O worker não recebe autoridade financeira e não pode criar, alterar, excluir, reconciliar, classificar definitivamente ou publicar fatos no livro financeiro por conta própria.

## Fonte de verdade

Ler antes de implementar e confrontar este Work Order com:

- `docs/ROADMAP.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- contratos existentes da Central Inteligente/captura, OCR, Whisper, documentos criptografados, auditoria e confirmação humana.

A documentação normativa e o comportamento determinístico verificável prevalecem. Claude pode abrir Technical Challenge quando houver evidência concreta de que este Work Order está incorreto, incompleto ou cria risco.

## Escopo mínimo

1. Introduzir uma fila/worker assíncrono para tarefas de OCR de imagem/PDF escaneado e transcrição de áudio já suportadas pelo sistema.
2. Reutilizar os mesmos parsers/processadores/serviços canônicos existentes; não criar segunda implementação de OCR, transcrição, classificação, importação ou captura.
3. Persistir estado explícito e auditável da tarefa (`queued`, `processing`, `completed`, `failed` ou equivalente tecnicamente justificado), com correlação inequívoca ao household e ao `capture_draft`/documento de origem.
4. Tornar enfileiramento e processamento idempotentes: retry não pode duplicar draft, documento, transação, obrigação, payroll ou qualquer fato financeiro.
5. Falha/timeout deve ser fail-closed e recuperável, com erro sanitizado e possibilidade de retry sem apagar ou reescrever silenciosamente o original.
6. O arquivo original continua criptografado em repouso; o worker deve receber apenas o mínimo necessário e respeitar household isolation.
7. A conclusão do worker deve produzir somente proposta/resultado de processamento compatível com o fluxo atual. A confirmação humana existente continua sendo a única ação autorizada a criar fatos financeiros.
8. Preservar compatibilidade com dados existentes e com o caminho síncrono durante migração/rollout quando necessário; qualquer mudança de schema deve ser aditiva, Alembic/PostgreSQL-safe e reversível sem perda de dados.
9. Não antecipar os demais itens da Fase 3 (`regras editáveis/aprendizado`, `comparação visual de cenários`, `notificações`) nem Fase 4.

## Critérios de aceite

- OCR e áudio podem ser submetidos sem bloquear a requisição até o término do processamento pesado.
- Estado da tarefa pode ser consultado de forma autenticada e isolada por household.
- Retry concorrente ou repetido não cria processamento financeiro duplicado nem múltiplos fatos derivados.
- Worker indisponível, crash ou timeout não promove dados incompletos a sucesso.
- Nenhum resultado probabilístico de OCR/Whisper/Codex ganha autoridade sobre regras financeiras ou invariantes.
- Confirmação humana e preview editável permanecem obrigatórios antes de qualquer criação de lançamento/obrigação/folha.
- Documentos e payloads sensíveis não aparecem em logs, mensagens de erro ou eventos de auditoria além do mínimo necessário.
- Testes provam sucesso, falha, retry/idempotência, household isolation, autenticação, recuperação de worker e ausência de mutação financeira antes da confirmação.
- Os 12 gates existentes do CI permanecem verdes no mesmo head final; adicionar gate novo somente se tecnicamente necessário e justificado, sem reduzir ou substituir cobertura existente.

## Invariantes e proibições

- Não alterar `FINANCIAL_RULES` ou `FINANCIAL_INVARIANTS` para acomodar a fila.
- Não fazer auto-fix de dados financeiros reais.
- Não apagar/regravar fatos históricos ou lineage silenciosamente.
- Não criar segundo motor de classificação, deduplicação, reconciliação, projeção, importação ou cálculo.
- Não persistir resultado de OCR/transcrição como fato financeiro confirmado.
- Não permitir processamento cross-household nem lookup não escopado de documento/draft/job.
- Não expor conteúdo bruto, áudio, imagem, documento, segredo ou credencial em logs/erros.
- Não usar Claude/Codex/Advisor como autoridade determinística.
- Não criar migration destrutiva.
- Não fazer merge.

## Testes obrigatórios

Cobrir ao menos:

- enfileiramento OCR;
- enfileiramento/transcrição de áudio;
- transição de estados e correlação com origem;
- idempotência em retry e entrega duplicada;
- crash/timeout/falha do processador com estado honesto;
- autenticação e household isolation nas leituras/mutações da fila;
- original criptografado e ausência de vazamento de payload sensível;
- confirmação humana ainda necessária para criação de fato financeiro;
- nenhuma dupla criação de `Transaction`, `Obligation`, `PayrollRecord`, `Document` ou `CaptureDraft` por retry;
- PostgreSQL/Alembic real se houver migration;
- regressão do fluxo atual de captura/importação;
- os 12 gates existentes no head final.

## Relação de revisão

Claude é o executor. Não pode fazer merge. Pode e deve contestar tecnicamente este Work Order, comentários de revisão ou interpretação arquitetural quando trouxer evidência verificável em documentação, código, testes, segurança, integridade financeira ou menor risco de regressão. Divergência não resolvida bloqueia merge.