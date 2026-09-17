# Work Order — WA-00 Discovery, ADR, custos e contratos

Issue: #72 (parent #71)

## Objetivo
Definir, com evidência verificável, a arquitetura executável do Family Finance AI Assistant via WhatsApp antes de qualquer integração de produção.

## Escopo
1. Inventariar Advisor/Codex e APIs financeiras existentes no `main`.
2. Documentar autenticação/chamada atual do Codex e testar suitability para runtime contínuo: disponibilidade, concorrência, timeout, recuperação e tool calling.
3. Produzir ADR para `WhatsApp -> gateway -> orchestrator -> tool layer -> Finance Engine/DB`.
4. Definir contratos genéricos de tools para consulta, agregação, comparação, projeção e drafts de escrita; perguntas devem ser livres/dinâmicas.
5. Manter adapter de LLM desacoplado. Ordem obrigatória: Codex atual sem custo adicional -> modelo local -> API paga somente mediante Technical Challenge e aprovação explícita.
6. Levantar custos/limites atuais da WhatsApp Business Platform e dependências externas; custo recorrente não aprovado bloqueia ativação, não a documentação.
7. Definir PII/logs/retenção, idempotência, household isolation, webhook/auth Meta, Docker e requisitos operacionais sem executar Oracle/OCI.
8. Mapear regras financeiras canônicas que as tools devem reutilizar; produzir matriz de riscos e rollback.

## Invariantes e proibições
- `LLM entende e orquestra -> Family Finance calcula, valida e persiste`.
- LLM não recebe SQL livre e não cria segundo motor financeiro.
- Não criar catálogo fechado de comandos/perguntas.
- Nenhuma hipótese do LLM vira fato financeiro silenciosamente.
- Nenhuma implementação de produção, webhook real, número real, credencial, dado real ou migration neste slice.
- Não antecipar WA-01+.
- Oracle/OCI #59–65 permanece congelado; referências a requisitos futuros de operação não autorizam implementação/deploy OCI.
- API paga não pode ser ativada nem escolhida silenciosamente.

## Critérios de aceite
- ADR completo e coerente com arquitetura/invariantes atuais.
- Inventário do runtime Advisor/Codex baseado em código atual e evidência objetiva de viabilidade/inviabilidade para WhatsApp.
- Tool contracts genéricos cobrem consultas dinâmicas e drafts sem SQL do LLM.
- Política de segurança/PII/retenção/idempotência/household isolation documentada.
- Custos e limites externos documentados com data/fonte e Technical Challenge quando aplicável.
- Riscos, rollback e decisões para WA-01..WA-07 explicitados.
- Nenhuma alteração de semântica financeira ou dado real.
- CI documental/lint aplicável verde.

## Testes/evidências obrigatórios
- Evidência reproduzível do comportamento do adapter Codex atual, sem expor tokens/segredos.
- Validação dos contratos contra APIs financeiras existentes e invariantes.
- Links/fontes oficiais para custos/limites externos sujeitos a mudança.
- Revisão de segurança das fronteiras de trust e PII.

Claude implementa/documenta e pode contestar este Work Order com evidência concreta. Claude não faz merge.