# Work Order — AI-CHAT-01: Codex como orquestrador obrigatório do chat web

Issue: #112

## Objetivo
Fazer do Codex/LLM o interpretador e orquestrador obrigatório da linguagem natural do chat web. O Family Finance permanece autoridade exclusiva para cálculos, validação e persistência.

## Arquitetura alvo
`mensagem -> Codex/orquestrador -> tool(s) permitida(s) -> serviços canônicos Family Finance -> resultado tipado -> Codex -> resposta`.

Reusar WA-02..WA-07 e serviços existentes. Não criar segundo motor financeiro.

## Escopo
- substituir o roteamento estático/silencioso do chat web por orquestração LLM tool-driven;
- expor ao orquestrador apenas schemas de tools permitidas;
- QUERY genérica, CONTEXT/follow-up, ADVISOR e WRITE;
- WRITE sempre draft/preview/confirm;
- resposta do LLM aterrada em outputs das tools;
- estado explícito de indisponibilidade do Codex;
- fail closed: sem fallback estático mascarado quando o interpretador estiver indisponível;
- manter household, RBAC, idempotência, auditoria e undo.

## Critérios de aceite
- frases e reformulações não cadastradas são interpretadas via tool selection do Codex;
- follow-ups reutilizam contexto;
- cálculos vêm do backend;
- mutação nunca persiste antes de confirmação;
- indisponibilidade do Codex é exibida e não dispara roteador estático;
- sem SQL/DB access pelo LLM;
- nenhuma hipótese vira fato;
- testes de segurança, contexto, tools, indisponibilidade e regressão verdes.

## Testes obrigatórios
Tool allowlist/schema validation; prompt injection; household isolation; RBAC; arbitrary paraphrases; multi-tool composition; context follow-up; Codex unavailable; WRITE draft/confirm/idempotency/audit/undo; QUERY canonical parity; ADVISOR backend-number provenance; frontend behavior; lint/CI.

## Riscos
Prompt injection/tool escalation; cross-household context; duplicate writes; hallucinated numbers; hidden static fallback; divergence web/WhatsApp.

## Proibições
Oracle/OCI #59–65; paid recurring dependency without Technical Challenge; generated SQL; direct DB access; duplicated finance calculations; phrase catalog/regex as primary intent engine; silent fallback; weakening confirmation/audit/undo.

## Nota de produto
O badge "Codex indisponível" deve significar exatamente isso: o chat de linguagem natural não está operacional. O sistema não deve simular a experiência Codex respondendo por heurística local.