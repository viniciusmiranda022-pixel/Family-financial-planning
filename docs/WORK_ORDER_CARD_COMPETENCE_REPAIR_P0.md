# Work Order — PRIORIDADE 0 — reparo auditável de competência histórica de cartão

## Prioridade

**PRIORIDADE 0 — incidente de produção.**

Este trabalho bloqueia temporariamente o retorno ao PR #56 e qualquer outro slice do roadmap até ser revisado, aprovado, mesclado e validado em produção pelo engenheiro responsável.

Este Work Order é complementar ao PR #81 (`docs/WORK_ORDER_CARD_OPEN_INVOICE_COMPETENCE_HOTFIX.md`). O PR #81 corrigiu os caminhos de criação para que novas compras de cartão usem a competência canônica da fatura. Este P0 trata o estado histórico já persistido incorretamente antes da correção, sem apagar lançamentos, sem SQL manual e sem backfill silencioso.

## Fontes normativas obrigatórias

Antes de alterar código, ler e obedecer, no mínimo:

- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`, especialmente INV-017;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/WORK_ORDER_CARD_OPEN_INVOICE_COMPETENCE_HOTFIX.md`;
- `docs/ROADMAP.md`;
- arquitetura, segurança e regras de auditoria já existentes.

Em caso de conflito, as regras financeiras e invariantes documentados prevalecem sobre testes legados ou comportamento atual.

## Incidente observado

Após a implantação do PR #81, a produção ainda contém compras de cartão criadas antes do hotfix cuja `Transaction.competence` permaneceu no mês de `booked_at`, embora o ciclo persistido do cartão determine outra competência de fatura.

O efeito é material:

1. a compra aparece no mês errado na visão de cartões;
2. a despesa econômica entra no snapshot do mês errado;
3. `budget_usage`/teto mensal é consumido no mês errado;
4. relatórios, fechamento mensal, parcelas/projeções e Advisor podem consumir a mesma competência histórica incorreta.

O problema não deve ser resolvido por `UPDATE` manual no PostgreSQL, exclusão/recriação de transação, alteração direta do snapshot, ajuste de valor, mudança de data da compra ou recálculo paralelo no frontend.

## Objetivo

Implementar um mecanismo canônico, determinístico, auditável, reversível e fail-closed para:

1. detectar compras históricas de cartão criadas pelos caminhos afetados e cuja competência persistida diverge da competência calculada pelo ciclo configurado;
2. apresentar preview sem mutação;
3. aplicar somente as correções explicitamente selecionadas e confirmadas por administrador;
4. preservar integralmente o fato econômico original;
5. reconstruir os fatos derivados afetados pelos períodos antigo e novo;
6. provar por testes que a compra deixa de consumir o teto do mês incorreto e passa a ser contabilizada apenas na competência correta.

## Regra financeira normativa

Para uma compra de cartão, a competência é a competência da fatura determinada pelo ciclo persistido da conta (`card_closing_day` + `card_due_day`). `booked_at` e `occurred_at` permanecem a data real da compra.

A regra deve continuar obedecendo ao PR #81 e ao INV-017. O cliente não escolhe livremente a nova competência durante o reparo: o backend a calcula pelo motor canônico.

Pagamento de fatura continua sendo apenas conciliação (INV-002). Aplicações, resgates e transferências permanecem fora do consumo operacional (INV-001/003/004). Duplicidade continua obedecendo INV-014.

## Arquitetura obrigatória

### 1. Extrair competência de cartão para serviço canônico

A regra de competência não deve permanecer como helper privado isolado em `app/api.py` se o reparo histórico também precisar consumi-la.

Criar um serviço canônico pequeno, por exemplo `app/services/card_competence.py`, ou justificar tecnicamente outro local equivalente.

Esse serviço deve conter a única implementação responsável por calcular a competência de compra de cartão a partir da conta e da data.

Todos os consumidores existentes devem usar essa mesma implementação:

- lançamento manual;
- confirmação de Smart Capture;
- preview/projeção de parcelas;
- validação de competência explícita já existente;
- detector/reparador histórico deste P0.

É proibido duplicar a fórmula em API, CLI, JavaScript, testes ou outro serviço.

### 2. Escopo seguro do detector histórico

O detector deve ser conservador.

Por padrão, considerar somente transações que satisfaçam simultaneamente:

- `Transaction.transaction_type == "expense"`;
- conta relacionada com `account_type == "credit_card"`;
- ciclo completo configurado (`card_closing_day` e `card_due_day`);
- competência persistida não nula;
- origem compatível com os caminhos de criação afetados pelo incidente, inicialmente `manual_confirmed` e `capture_confirmed`;
- competência persistida diferente da competência calculada canonicamente.

Não alterar automaticamente transações de importação histórica, planilha canônica, documentos financeiros, conciliações, estornos, pagamentos de fatura, transferências, aplicações ou resgates. Se Claude encontrar evidência de que outra origem precisa entrar no reparo, deve abrir **Technical Challenge** com prova concreta antes de ampliar o escopo.

### 3. Preview obrigatório e sem side effects

Criar uma operação administrativa autenticada de preview. A forma exata da rota pode ser proposta por Claude, mas deve ficar claramente separada da aplicação.

Exemplo aceitável:

`POST /api/maintenance/card-competence/preview`

O preview deve:

- exigir `_require_admin(user)` antes de qualquer lookup;
- filtrar sempre pelo `household_id` do usuário;
- não fazer `commit` de fato financeiro;
- não alterar `Transaction`, `FinancialSnapshot`, `MonthlyFinancialClose`, duplicidades ou documentos;
- retornar apenas candidatos determinísticos;
- retornar para cada candidato: `transaction_id`, conta, `booked_at`, `occurred_at`, valor, `classification_source`, competência atual e competência esperada;
- informar períodos afetados;
- retornar a revisão financeira corrente do household para controle de concorrência;
- indicar bloqueios por duplicidade/fechamento mensal quando aplicável.

Nenhum modelo de IA participa da decisão.

### 4. Aplicação explícita, seletiva e race-safe

Criar uma operação administrativa separada para aplicar o reparo.

Exemplo aceitável:

`POST /api/maintenance/card-competence/apply`

A requisição deve conter, no mínimo:

- ids das transações selecionadas no preview;
- `expected_financial_revision` devolvida pelo preview;
- `reason` humano obrigatório, mínimo 3 caracteres.

A requisição **não** deve aceitar `target_competence` como autoridade do cliente.

No apply:

1. `_require_admin(user)` deve ser a primeira ação;
2. tomar a barreira/revisão financeira do household usando o mecanismo canônico já existente;
3. rejeitar se a revisão corrente divergir da revisão do preview;
4. reler todas as transações pelo `household_id`;
5. recalcular a competência esperada usando o serviço canônico;
6. rejeitar qualquer id que tenha deixado de ser candidato;
7. aplicar tudo em uma única transação de banco: ou todas as correções selecionadas são persistidas, ou nenhuma;
8. nunca executar SQL textual/manual para mutar `transactions`;
9. deixar o `before_flush`/`HouseholdFinancialRevision` existente registrar a mutação do fato financeiro.

### 5. Campos que podem e não podem mudar

Para cada transação reparada, somente `competence` (e timestamps técnicos normais do ORM) pode mudar.

Devem permanecer bit-a-bit/logicamente preservados:

- `id`;
- `household_id`;
- `account_id`;
- `document_id`;
- `category_id`;
- `booked_at`;
- `occurred_at`;
- `description` e `normalized_description`;
- `amount`;
- `transaction_type`;
- `owner_label`;
- `card_last_four`;
- dados de parcelamento;
- `fingerprint`;
- origem/classificação;
- `canonical_status`;
- `duplicate_group_id`;
- `linked_transaction_id`;
- `transfer_group_id`;
- `trace_id` original;
- `source_priority`;
- `confidence`;
- `excluded`;
- `possible_duplicate`;
- `reviewed`.

É proibido apagar e recriar a transação.

### 6. Duplicidades — fail closed

Se uma transação candidata estiver em grupo de duplicidade aberto, marcada como `possible_duplicate` ou possuir papel de suporte/canonical que possa ser afetado pela mudança de assinatura, o apply deve falhar para essa transação e exigir resolução humana prévia, salvo se Claude demonstrar em Technical Challenge uma solução segura que preserve INV-014.

O reparo de competência não pode aproveitar a oportunidade para resolver/reclassificar duplicidades automaticamente.

### 7. Fechamento mensal — fail closed

Se qualquer período afetado estiver em `MonthlyFinancialClose.status == "trusted"`, o reparo deve ser bloqueado até que o fechamento seja reaberto pelo fluxo canônico já existente, com motivo humano.

O reparador não pode reabrir fechamento automaticamente nem invalidar decisão humana silenciosamente.

### 8. Auditoria obrigatória

Cada alteração deve gerar `AuditEvent` com, no mínimo:

- ação específica, por exemplo `transaction.card_competence_repair`;
- `transaction_id`;
- usuário/admin responsável;
- `before_state` com competência anterior;
- `after_state` com competência nova;
- `booked_at` preservado como evidência;
- conta/cartão;
- motivo humano;
- `trace_id` do lote/reparo;
- referência ao detector/regra canônica utilizada.

Também é aceitável/advisável um evento agregado do lote contendo apenas ids e contagens, desde que não substitua a trilha por transação.

### 9. Snapshots e publicação derivados

Após a mutação, os períodos impactados pela remoção e inclusão da compra devem ser recomputados pelo caminho canônico de snapshots, sem editar `FinancialSnapshot` diretamente.

O resultado obrigatório é:

- mês antigo: a compra reparada deixa de integrar `operating_expenses`, `card_spend`, `category_spending` e `budget_usage` daquele mês;
- mês novo: a compra passa a integrar exatamente uma vez os mesmos campos na competência correta;
- `budget_remaining` acompanha a mudança;
- visão de cartões, dashboard e relatórios concordam;
- parcelas futuras, se aplicável, usam o novo anchor canônico sem duplicação;
- Advisor recebe somente os fatos já corrigidos/publicados pelo backend.

Se a arquitetura existente já recompõe snapshots automaticamente em leitura de forma suficiente e demonstrável, Claude pode reutilizar isso, mas deve provar por teste que não sobra snapshot `current` stale após o apply. Não editar snapshot, checksum ou lineage manualmente.

### 10. Rollback lógico

O reparo deve ser reversível sem restaurar o banco inteiro.

Implementar um rollback lógico explícito para um lote de reparo, ou demonstrar uma operação de reversão equivalente segura.

O rollback deve:

- exigir admin e motivo;
- usar audit trail/before_state real, nunca inferência;
- validar que a transação ainda está no estado produzido pelo reparo antes de revertê-la;
- ser race-safe pela revisão financeira;
- **bloquear atomicamente, antes de qualquer mutação, se o período antigo ou o
  novo (os dois períodos do próprio rollback, lidos do `AuditEvent` do
  reparo -- nunca um período recalculado ou informado pelo cliente) tiver um
  `MonthlyFinancialClose.status == "trusted"`, com a mesma semântica
  fail-closed do apply (critério 14).** Isto não é opcional nem inferido: a
  primeira rodada de revisão do PR #82 encontrou esta ambiguidade -- o texto
  original desta seção listava a barreira de revisão financeira mas omitia a
  de fechamento trusted, o que teria permitido reabrir implicitamente um mês
  fechado por decisão humana sem passar por `reopen`. O rollback nunca reabre
  um fechamento sozinho; se um período estiver trusted, a única saída é o
  administrador reabri-lo explicitamente (com motivo) pelo fluxo canônico
  antes de repetir o rollback;
- nunca restaurar dump automaticamente.

O dump de produção é contingência/DR, não mecanismo normal de rollback desta feature.

## Interface

Uma UI sofisticada não é obrigatória para este P0 se atrasar a correção. Porém, o mecanismo precisa ser parte oficial do sistema, não script ad hoc.

É aceitável entregar endpoints administrativos + testes e um comando operacional documentado que apenas invoque esses endpoints/serviço canônico. Se houver UI, ela deve ser admin-only e apenas apresentar o que o backend calculou; nenhum cálculo de competência pode existir no JavaScript.

## Migration / dados

**Nenhuma migration é esperada.**

Este P0 deve preferir os modelos e tabelas de auditoria existentes. Se Claude concluir que uma migration é indispensável para segurança/auditabilidade, deve abrir **Technical Challenge** antes de criar a migration.

Não existe autorização para backfill automático durante startup, Alembic, deploy ou `docker compose up`.

## Critérios de aceite funcionais

Usar somente dados sintéticos nos testes e reproduzir, no mínimo:

1. cartão com fechamento no início do mês e vencimento depois: compra após fechamento sai do mês de `booked_at` e vai para a competência da próxima fatura;
2. cartão com fechamento no fim do mês e vencimento no início do mês seguinte: compra antes do fechamento fica na competência do mês de vencimento correto;
3. virada dezembro/janeiro;
4. compra exatamente no closing day respeita a semântica já aprovada no PR #81;
5. cartão sem ciclo configurado falha explicitamente;
6. conta não-cartão não entra no detector;
7. importação/documento fora do escopo não é reparado automaticamente;
8. preview não altera nenhuma linha financeira nem cria snapshot;
9. apply altera somente `competence` das transações explicitamente selecionadas;
10. apply com revisão stale falha sem mutar nada;
11. apply com id de outro household falha sem vazar existência;
12. apply com candidato que deixou de ser candidato falha atomicamente;
13. duplicidade aberta bloqueia;
14. fechamento `trusted` em qualquer período afetado bloqueia, tanto no apply quanto no rollback (ver seção 10);
15. auditoria before/after/reason é persistida;
16. rollback lógico restaura exatamente a competência anterior quando o estado ainda permite;
17. rollback stale/conflitante falha sem sobrescrever mudança posterior;
18. nenhum lançamento é apagado ou recriado;
19. rollback para um período (antigo ou novo) coberto por fechamento `trusted` falha atomicamente sem mutar `Transaction`, `AuditEvent` de rollback ou snapshot, e só prossegue após `reopen` explícito de todos os períodos afetados.

## Critérios de aceite financeiros obrigatórios

Criar testes de integração que demonstrem o efeito completo no sistema, e não apenas no campo `Transaction.competence`.

Cenário sintético mínimo:

- teto mensal: R$ 1.000,00;
- compra: R$ 200,00;
- compra originalmente persistida na competência errada `2026-09`;
- ciclo determina `2026-10`.

Antes do reparo:

- setembro contém R$ 200,00 de `card_spend`/despesa/teto atribuídos incorretamente;
- outubro não contém essa compra.

Depois do reparo:

- setembro reduz exatamente R$ 200,00 em `operating_expenses`, `card_spend`, categoria e `budget_usage`;
- `budget_remaining` de setembro aumenta exatamente R$ 200,00;
- outubro aumenta exatamente R$ 200,00 nesses mesmos componentes;
- a compra aparece exatamente uma vez no total consolidado dos dois meses;
- visão de cartões, dashboard, relatório e snapshot concordam com `2026-10`;
- INV-017 passa;
- INV-001/002/003/004/014/016 não sofrem regressão.

Adicionar cenário de duas compras em dois cartões diferentes para provar que o reparo não depende de nome de banco e usa somente o ciclo configurado.

## Testes de segurança e integridade

Cobrir no mínimo:

- autenticação obrigatória;
- admin obrigatório;
- consulta/read-only recebe 403 antes de lookup quando esse P0 for combinado com o PR #56; enquanto #56 não estiver em `main`, manter compatibilidade e não criar dependência circular nele;
- household isolation fail-closed;
- nenhum id de outra família vaza por 404/200 diferenciado depois da barreira de autorização aplicável;
- reason obrigatório;
- nenhuma IA decide competência;
- nenhuma mutation em dry-run/preview;
- atomicidade em lote;
- financial revision/race guard;
- audit trail completo;
- nenhum PII/dado real em fixture, screenshot, commit ou log de CI.

## Compatibilidade com PR #56

Este P0 nasce de `main` após o merge do PR #81 e **não deve incorporar o PR #56 por cherry-pick ou merge acidental**.

O PR #56 permanece bloqueado enquanto este P0 estiver ativo. Depois do merge deste P0, o PR #56 deve sincronizar/rebasear sobre o novo `main` e aplicar a política admin/read-only aos novos endpoints administrativos antes de poder ser aceito.

Se o P0 criar endpoints mutáveis antes do merge do #56, o Work Order do #56 deve ser reavaliado para garantir que esses endpoints também recebam `_require_admin` quando o #56 for atualizado.

## CI obrigatório

No head final:

- `ruff check .`;
- suíte completa `pytest`;
- testes específicos do reparo;
- `tests/test_financial_invariants.py` com INV-017;
- testes de snapshot/dashboard/relatório/teto;
- `node --check app/static/app.js` se frontend for tocado;
- Alembic sem nova revisão, salvo Technical Challenge aprovado;
- Docker build;
- integração PostgreSQL;
- os **12 gates GitHub Actions** obrigatórios verdes no head final.

Green CI não substitui revisão de regra financeira.

## Proibições explícitas

- não fazer `UPDATE transactions ...` por SQL manual;
- não fornecer script PowerShell/SQL de correção pontual como solução do produto;
- não apagar e recriar lançamento;
- não alterar `booked_at`/`occurred_at` para mover mês;
- não editar snapshot/lineage diretamente para maquiar resultado;
- não executar backfill automático no deploy/startup/migration;
- não aplicar reparo em todas as transações históricas indiscriminadamente;
- não hardcodar Itaú, Nubank, Mercado Pago ou qualquer banco;
- não aceitar nova competência arbitrária enviada pelo cliente;
- não recalcular competência no frontend;
- não resolver duplicidade automaticamente;
- não reabrir fechamento `trusted` automaticamente;
- não reduzir cobertura ou gates;
- não mudar regra financeira para fazer teste passar;
- não conceder a Claude/Codex autoridade sobre fatos determinísticos;
- não fazer merge por conta própria.

## Procedimento esperado de produção após merge

Depois que o engenheiro responsável revisar e mesclar este P0:

1. criar e validar dump PostgreSQL antes da alteração real;
2. atualizar/rebuildar o app sem remover volumes;
3. executar preview administrativo;
4. revisar manualmente cada candidato;
5. confirmar apenas os ids aprovados com motivo explícito;
6. validar audit trail;
7. validar que quantidades/ids de transações foram preservados;
8. validar setembro/outubro (ou períodos afetados) em dashboard, cartões, relatório e teto;
9. validar INV-017 e integridade;
10. somente então considerar o incidente encerrado.

Nenhuma etapa deve usar `docker compose down -v`, reset destrutivo, exclusão de volume ou restauração de dump salvo como mecanismo normal de aplicação.

## Relação de revisão

Claude é o executor da implementação e **não pode fazer merge**.

Claude deve contestar este Work Order por **Technical Challenge** se encontrar evidência concreta de contradição com:

- regra financeira normativa;
- modelo de dados real;
- household isolation;
- lifecycle de fechamento mensal;
- auditoria/revision barrier;
- atomicidade necessária;
- comportamento verificável do PostgreSQL.

Desacordo sem evidência não autoriza desvio. Divergência técnica não resolvida bloqueia merge.

O engenheiro responsável revisará diff, arquitetura, invariantes, dados, segurança, household isolation, atomicidade, auditoria, rollback, testes e os 12 gates antes de qualquer merge.