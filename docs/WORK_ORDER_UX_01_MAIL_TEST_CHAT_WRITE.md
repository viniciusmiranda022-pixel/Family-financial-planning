# Work Order — UX-01

Issue: #114

## Objetivo
Corrigir duas regressões comprovadas em uso real: throttle excessivo no teste de e-mail e chat Codex que ainda não executa o ciclo WRITE ponta a ponta.

## E-mail
O throttle atual é process-local e indexado somente por household. Torná-lo granular por household + destinatário (ou chave equivalente), preservando proteção de duplo clique. Default de teste deve ser curto e apropriado para operação administrativa, com UX clara de retry. Não alterar scheduler D-1/D0 nem retry do worker.

## Chat WRITE
Instrumentar e corrigir browser -> API -> orchestrator -> tools -> draft -> confirmação -> persistência. WRITE completo gera preview. WRITE incompleto mantém draft/contexto e pergunta apenas campos materiais faltantes. Follow-up continua a ação anterior. Nunca degradar WRITE para QUERY/advisor silenciosamente.

## Casos obrigatórios
1. `Gastei R$ 150 de combustível hoje no cartão Itaú` -> proposta -> confirmar -> persistido.
2. `fiz uma compra de 2459 parcelada no cartão nubank` -> coleta nº parcelas/juros se necessários; `parcelada` mantém contexto.
3. `voce conseguiu lançar o que eu gastei?` -> status do draft/ação.
4. teste e-mail destinatário A seguido imediatamente por B funciona; duplo clique em A permanece protegido.

## Invariantes
LLM entende/orquestra; backend calcula/valida/persiste. Sem SQL/DB pelo LLM. Confirmação humana antes de mutação. Dedup fail-safe. Audit/undo e household/RBAC obrigatórios. Sem Oracle/OCI #59–65. Sem segundo motor financeiro. Sem custo recorrente novo.

## Testes obrigatórios
Integração web/API/orchestrator/tools; multi-turn; WRITE confirm/idempotency/audit/undo; fail-closed; prompt/tool allowlist; throttle por destinatário e concorrência; UI; regressão MAIL/QUERY/ADVISOR; ruff e CI.

## Riscos
Dupla gravação, contexto cruzado, interpretação WRITE->QUERY, confirmação bypassada, spam de teste, exposição de destinatário/segredo em logs.

## Proibições
Não mascarar falha com heurística estática. Não remover confirmação. Não alterar semântica financeira. Não tocar Oracle/OCI.