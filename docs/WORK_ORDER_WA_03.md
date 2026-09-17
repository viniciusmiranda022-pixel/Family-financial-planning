# Work Order — WA-03 Write flow com drafts, confirmação e idempotência

Issue: #75
Depends on: #74 / WA-02 merged.

## Objetivo
Conectar o canal WhatsApp autenticado ao Orchestrator/Tool Layer já existente para permitir mutações financeiras em linguagem natural sem criar segundo motor financeiro, mantendo draft, confirmação humana explícita, idempotência e auditoria integral.

## Escopo obrigatório
1. Reutilizar exclusivamente `assistant_interpreter`, `assistant_actions`, `execute_typed_action`, undo e serviços financeiros canônicos existentes. Nenhum cálculo financeiro novo no LLM/gateway/orchestrator.
2. Suportar drafts estruturados para despesa, receita, transferência, aplicação, resgate, reembolso/estorno e pagamento de fatura conforme capacidades canônicas existentes; qualquer gap real deve ser documentado e tratado fail-closed, não improvisado.
3. Perguntar somente campos materiais ausentes/ambíguos. Não inventar origem/destino, conta/cartão, data, categoria, titular, parcelamento ou natureza financeira.
4. Prévia antes de commit deve mostrar, quando aplicável: tipo, valor, data, categoria, conta/cartão, titular e parcelamento, usando dados resolvidos deterministicamente.
5. Nenhuma mutação antes de confirmação explícita e específica. Confirmação deve estar vinculada a draft/proposta inequívoca do mesmo usuário/household.
6. Alteração/exclusão/desfazer exige identificação inequívoca + confirmação específica e passa pelos mecanismos canônicos de audit/undo.
7. Idempotência end-to-end por mensagem e conversation action: redelivery/retry concorrente não pode duplicar draft nem commit. Usar barreiras atômicas PostgreSQL quando necessário; não usar check-then-insert vulnerável a corrida.
8. Preservar invariantes: resgate Privilège DI não é renda; aplicação/resgate não criam despesa/renda sintética; pagamento de fatura não é nova despesa; transferências internas não são renda/despesa; parcelamento preserva compra contratada, competência mensal e futuro; estorno neutraliza sem apagar histórico; parcial carrega principal e separa encargos.
9. Auditoria deve registrar usuário, timestamp, mensagem/referência mínima necessária, interpretação, typed action, registros before/after quando aplicável, confirmação/cancelamento/resultado e undo, sem persistir PII ou webhook bruto desnecessariamente.
10. O WhatsApp deve chamar o mesmo `plan_and_execute`/Tool Layer do WA-02. Proibido criar parser/roteador financeiro paralelo específico do WhatsApp.
11. Respeitar autoridade/household resolvidos server-side pelo gateway WA-01. Nunca aceitar `household_id`, `user_id`, ids financeiros, SQL ou authority claims vindos do LLM ou payload do cliente.
12. Manter comportamento fail-closed para modelo indisponível, plano inválido, referência ambígua, replay e falha parcial.

## Critérios de aceite
- `gastei 30 de combustível` gera draft e pergunta apenas o materialmente ausente; não grava antes de confirmação.
- `gastei 30 de combustível no Nubank` resolve a conta/cartão quando inequívoco e não pergunta novamente sem necessidade.
- confirmação explícita executa exatamente uma vez; retry/redelivery da mensagem e da confirmação não duplica fato financeiro.
- resgate do Privilège DI não vira renda.
- pagamento de fatura não vira nova despesa.
- transferência interna não vira renda/despesa.
- parcelamento mantém semântica canônica de competência/compromissos.
- cancelamento não executa draft; undo usa trilha canônica e preserva histórico.
- duas famílias/usuários não conseguem referenciar/confirmar drafts entre si.
- prompt injection não consegue escolher tool fora do allowlist, injetar ids/SQL ou burlar confirmação.
- E2E PostgreSQL cobre formulações variadas, webhook redelivery e concorrência de confirmação.
- suíte financeira, invariantes, reconciliação, migrations, integração PostgreSQL, security/advisor, lint e Docker permanecem verdes.

## Revisão obrigatória antes de merge
Diff completo; contratos e autoridade; semântica financeira; idempotência/concorrência; migrations/Alembic; compatibilidade de dados; segurança/PII; audit/undo; testes E2E PostgreSQL; CI; documentação e regressões.

## Proibições
- Não criar segundo motor financeiro.
- Não permitir SQL/ORM gerado pelo LLM.
- Não criar catálogo estático de frases/perguntas.
- Não gravar antes de confirmação explícita.
- Não mascarar ambiguidade com defaults financeiros perigosos.
- Não persistir webhook bruto/PII sem necessidade documentada.
- Não antecipar WA-04+ nem Oracle/OCI #59–65.
- Claude não faz merge.

## Entrega esperada de Claude
Implementar neste branch, documentar arquitetura/contratos/migrations e riscos, executar todos os checks aplicáveis e deixar o PR pronto para revisão do engenheiro responsável. Se houver divergência com este Work Order, contestar com evidência concreta no PR; divergência não resolvida bloqueia merge.