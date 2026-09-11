# Work Order — Fase 4: perfis separados para administrador e consulta

## Objetivo

Implementar exatamente o segundo item ainda não concluído de `docs/ROADMAP.md` na Fase 4 — **perfis separados para administrador e consulta** — reutilizando o modelo de autenticação e household já existente, sem antecipar MFA ou outros itens posteriores.

O modelo `User` já possui `is_admin`; trate esse fato como ponto de partida e não crie uma taxonomia nova de papéis sem necessidade comprovada. O objetivo deste slice é tornar a autorização efetiva e verificável: administrador pode operar as funções mutáveis previstas; usuário de consulta pode ler somente os dados do próprio household e nunca alterar fatos financeiros, configuração, integridade, usuários ou estado operacional.

## Fonte de verdade

Ler antes de implementar e confrontar este Work Order com:

- `docs/ROADMAP.md`;
- `docs/SECURITY.md`;
- `docs/ARCHITECTURE.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `app/models.py`, `app/security.py`, `app/api.py`, schemas, templates/JavaScript e testes atuais.

Se o contrato atual demonstrar que `User.is_admin` é insuficiente ou semanticamente incorreto, Claude deve abrir Technical Challenge com evidência concreta antes de introduzir outro modelo de roles/permissões.

## Escopo mínimo

1. Criar uma fronteira de autorização canônica e reutilizável no backend para distinguir administrador de consulta; não espalhar verificações ad hoc divergentes por endpoints.
2. Preservar autenticação atual e isolamento estrito por `household_id`; role nunca permite acesso cruzado entre famílias.
3. Usuário de consulta pode acessar superfícies de leitura necessárias para visualizar dashboard, lançamentos, contas, relatórios, projeções, obrigações e demais dados já permitidos ao household, mas deve receber rejeição explícita em qualquer operação que crie, altere, exclua, confirme, vincule, resolva, importe, execute fechamento/integridade ou mude configuração/dados.
4. Administrador preserva o comportamento mutável existente, sem ganhar acesso fora do próprio household.
5. Aplicar autorização no backend como controle real. A UI pode esconder/desabilitar ações mutáveis para usuário de consulta, mas isso é apenas UX e nunca substitui o enforcement da API.
6. Não alterar valores, cálculos, regras financeiras, invariantes, reconciliação, snapshots ou dados reais para implementar autorização.
7. Não antecipar MFA, backup externo, observabilidade/alertas ou rotina geral de atualização/rollback.
8. Compatibilidade com usuários existentes deve ser explícita e segura. Não promover silenciosamente usuários a administrador. Se alguma migration for realmente necessária, deve ser aditiva, reversível e preservar o valor existente de `is_admin`; preferir não criar migration se a coluna atual já satisfizer o contrato.

## Critérios de aceite

- Existe uma única política/backend helper ou dependency canônica para autorização de escrita administrativa.
- Todas as rotas mutáveis aplicáveis estão protegidas por essa política; tentativa direta via API por usuário de consulta falha com `403` sem efeito colateral.
- Rotas de leitura continuam funcionais para usuário de consulta dentro do próprio household.
- Isolamento por household continua fail-closed para ambos os perfis.
- A interface identifica o modo consulta e não oferece ações mutáveis como se fossem permitidas.
- Nenhum fato financeiro, finding, snapshot, documento, transação, obrigação ou configuração é alterado quando uma operação é negada.
- Bootstrap/criação de usuário e administração de usuários não permitem escalada de privilégio por usuário de consulta.
- Compatibilidade de dados existentes é preservada; nenhuma promoção/demotion silenciosa ocorre.
- Os 12 gates existentes permanecem verdes no mesmo head final, com regressões específicas de autorização.

## Testes obrigatórios

Cobrir ao menos:

- administrador mantém acesso às operações mutáveis existentes;
- usuário de consulta consegue acessar leituras do próprio household;
- usuário de consulta recebe `403` em create/update/delete/import/confirm/link/unlink/lifecycle/close/run/trust/reopen/configuração e demais mutações aplicáveis;
- requisição negada não produz alteração no banco nem audit event de sucesso da operação proibida;
- usuário de consulta não cria/promove usuário administrador nem altera privilégios;
- nenhum dos perfis acessa dados de outro household;
- UI não expõe controles mutáveis ativos para consulta, sem depender disso para segurança;
- regressão dos invariantes financeiros e dos 12 jobs do CI.

Não reduza cobertura nem altere testes para acomodar comportamento inseguro; corrija o enforcement.

## Segurança e proibições

Não criar bypass por parâmetro de request, header controlado pelo cliente ou JavaScript. A autoridade vem somente do usuário autenticado carregado pelo backend. Não usar Codex/Claude para decidir role ou autorização. Não alterar regras financeiras para fazer testes passarem, não corrigir dados reais automaticamente, não introduzir migration destrutiva e não fazer merge.

## Relação de revisão

Claude é o executor e não pode fazer merge. Pode e deve contestar tecnicamente este Work Order ou a interpretação do engenheiro quando houver evidência concreta em documentação, código, testes, segurança ou menor risco de regressão. Divergência não resolvida bloqueia merge.