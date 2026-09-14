# Work Order — October Go-Live Slice 4 — Assistente Financeiro operacional

## Prioridade
P0 #87. Este slice vem imediatamente após o Slice 3. Não iniciar Slice 5 nem trabalho lateral enquanto este Work Order não estiver tecnicamente aprovado e integrado.

## Objetivo
Transformar o Assistente Financeiro em uma interface operacional confiável sobre os fatos financeiros reais do sistema, conforme `docs/OCTOBER_GO_LIVE_REBASELINE.md` §§10–12, sem permitir que interpretação do Codex vire fato silenciosamente.

## Escopo obrigatório
1. Linguagem natural -> interpretação estruturada -> ação tipada de backend -> resultado persistido.
2. Executar automaticamente somente quando a mensagem estiver materialmente completa e não ambígua.
3. Perguntar apenas pelos fatos críticos ausentes: natureza/tipo, conta/cartão/origem de recursos, data/competência ou alvo quando necessário.
4. Para saída bancária sem origem explícita, perguntar se o valor já estava na Conta Corrente ou se houve resgate do Privilège; nunca fabricar resgate/aplicação.
5. Suportar, no mínimo, os fluxos de go-live já normatizados: entrada, saída, transferência interna, obrigação paga, compra/cartão, estorno quando alvo inequívoco, atualização factual suportada e consultas/análises sem mutação.
6. Toda mutação do Assistente deve produzir trilha persistente contendo: usuário, data/hora, mensagem original, interpretação estruturada do Codex, perguntas/respostas de desambiguação relevantes, ação tipada, IDs criados/alterados, before/after quando aplicável, `trace_id`, resultado e capacidade de undo quando tecnicamente reversível.
7. Undo deve ser ação explícita, auditada e segura; nunca apagar história nem desfazer fato externo impossível de reverter. Quando uma operação não puder ser revertida com segurança, a API deve declarar isso de forma explícita.
8. Aprendizado persistente para `Outra entrada`/`Outra saída`: distinguir tipo, categoria, favorecido e descrição; evitar sinônimos duplicados; criação/reuso de template somente após confirmação humana quando houver novidade semântica.
9. Deduplicação continua fail-safe e humana: provável duplicidade deve mostrar evidência e oferecer pular/importar mesmo/ver existente; o Assistente não funde nem apaga automaticamente.
10. Codex pode interpretar, validar, recalcular, comparar e explicar divergências, mas o backend permanece autoridade de escrita/cálculo determinístico e hipótese nunca vira fato.

## Invariantes obrigatórios
- Pagamento de fatura não cria nova despesa econômica.
- Transferência entre contas próprias não vira renda/despesa.
- Aplicação/resgate Privilège <-> Conta Corrente é liquidez interna, nunca déficit/superávit sintético.
- Saldo confirmado permanece soberano; Assistente não cria ajuste para fechar divergência.
- REALIZADO/COMPROMETIDO/PREVISTO não podem ser misturados pela interpretação.
- Comissão não recebida não entra automaticamente como prevista.
- Estorno neutraliza gasto preservando o lançamento original e seu lineage.
- Nenhuma ação ambígua é executada automaticamente.
- Nenhuma ação do Assistente fica sem audit trail/trace e sem informação de reversibilidade.

## Critérios de aceite
1. Mensagem completa gera plano tipado determinístico e, quando autorizado pelo contrato, executa exatamente uma vez.
2. Mensagem incompleta gera pergunta de desambiguação e zero mutações até resposta suficiente.
3. Retry da mesma ação/trace não duplica registros financeiros.
4. Cada mutação permite reconstruir quem pediu, o texto original, como foi interpretado, qual ação ocorreu e quais registros mudaram.
5. Undo reversível restaura estado financeiro sem apagar a trilha; undo não reversível é recusado com razão verificável.
6. Fluxos de PIX/origem de caixa respeitam Conta Corrente vs Privilège e nunca fabricam transferência interna.
7. Operações em cartão/fatura/obrigação reutilizam os serviços canônicos dos Slices 1–3, sem segundo motor financeiro.
8. Duplicidade provável nunca é resolvida destrutivamente sem decisão humana.
9. Aprendizado de tipo/template só persiste após confirmação e não cria sinônimos equivalentes sem necessidade.
10. Segurança por household/role permanece preservada em leitura, ação tipada, audit e undo.

## Testes obrigatórios
- ação completa de despesa com conta/cartão explícito;
- despesa PIX sem origem -> pergunta e zero escrita;
- resposta de desambiguação completa -> uma única execução;
- transferência interna -> zero renda/despesa econômica;
- obrigação/fatura paga pelo Assistente -> sem dupla contagem;
- retry/idempotência por `trace_id`/chave documental equivalente;
- ambiguidade de alvo de estorno -> pergunta, nunca escolha silenciosa;
- audit completo com mensagem/interpretação/typed action/IDs/before-after;
- undo reversível e recusa segura de undo não reversível;
- deduplicação provável com as três escolhas humanas;
- isolamento entre households e autorização por role;
- regressão dos Financial Invariants e CI completo.

## Arquitetura / proibições
- Reutilizar serviços e endpoints canônicos; não criar um segundo ledger, segundo motor de projeção, segunda regra de competência ou uma camada paralela de cálculo financeiro.
- Codex nunca recebe SQL arbitrário nem credenciais bancárias.
- Não permitir escrita genérica baseada em JSON livre sem `action_type`/schema validado.
- Não inferir fato crítico faltante por heurística silenciosa.
- Não transformar resposta do modelo em mutation direta; sempre passar por validação e execução tipada de backend.
- Não consumir prioridade com MFA, hardening geral, observabilidade, parser não bloqueador ou UX do Slice 5.

## Migrations / compatibilidade
Se forem necessários novos campos/tabelas para action log, trace, desambiguação, undo ou templates aprendidos, migrations devem ser aditivas, reversíveis quando seguro, preservar dados existentes e incluir teste Alembic PostgreSQL. Nenhum backfill destrutivo.

## Documentação obrigatória
Atualizar, quando afetados: `docs/ARCHITECTURE.md`, `docs/INTELLIGENCE.md`, `docs/FINANCIAL_RULES.md`, `docs/FINANCIAL_INVARIANTS.md`, `docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` e README apenas se o contrato público realmente mudar.

## Definition of Done
O Slice 4 só está concluído quando o Assistente puder operar os fluxos cotidianos do go-live com typed actions, desambiguação material, audit/undo, aprendizado confirmado, idempotência e deduplicação humana, sem violar as invariantes financeiras já integradas. CI completo verde e revisão de engenharia obrigatória antes do merge.

Claude é o executor. Não fazer merge. Não iniciar Slice 5.