# Work Order — alertas de vencimento por e-mail (D-1 e D0)

## Estado e sequência

Este documento registra antecipadamente a próxima evolução funcional solicitada para o Family Finance: **alertas de vencimento por e-mail um dia antes (D-1) e no próprio dia do vencimento (D0)**.

A presença deste Work Order em `main` registra intenção, arquitetura e critérios de aceite. Ela **não autoriza execução paralela** nem permite atropelar slices/PRs normativos que estejam abertos na fila do projeto. Quando este trabalho entrar efetivamente em execução, Claude deve reler a `main` vigente, confrontar este documento com os contratos já integrados e abrir branch/Draft PR isolados conforme a governança do repositório.

**Claude executa a implementação e não faz merge.**

Branch raiz prevista para o primeiro slice: `feat/due-date-email-alerts`.

## Objetivo

Enviar automaticamente alertas de contas/obrigações financeiras persistidas no Family Finance quando:

- a obrigação vencer **amanhã** (`D-1`);
- a obrigação vencer **hoje** (`D0`).

O sistema deve enviar somente para destinatários configurados pelo household, nunca para endereços inferidos. Deve suportar **um ou vários e-mails destinatários**.

Se uma obrigação já estiver paga ou desativada, nenhum alerta futuro dessa obrigação deve ser enviado.

## Decisão de produto solicitada

Na tela de Configurações, o usuário não cadastra senha do Gmail nem segredo de SMTP. A interface expõe somente os **destinatários** e suas preferências de alerta.

Exemplo conceitual:

```text
Alertas de vencimento

Ativo: [sim]
Horário: 08:00
Fuso: America/Sao_Paulo

Destinatários
-------------------------------------------------
vinicius@exemplo.com   D-1 [x]   D0 [x]   Ativo [x]
kelly@exemplo.com      D-1 [x]   D0 [x]   Ativo [x]

[ + Adicionar destinatário ]
```

O endereço Gmail remetente e sua credencial técnica ficam exclusivamente na configuração segura do backend/ambiente.

## Escopo inicial

Incluído:

1. configuração de um ou vários destinatários;
2. toggle global de alertas;
3. preferência D-1 e D0 por destinatário;
4. horário diário configurável, default `08:00`;
5. timezone explícito, default `America/Sao_Paulo`;
6. envio de e-mail HTML + alternativa texto puro;
7. integração técnica com Gmail por SMTP autenticado;
8. scheduler/worker durável;
9. idempotência e prevenção de duplicidade;
10. retry controlado em falha transitória;
11. trilha de auditoria sem segredos e sem vazar endereço de e-mail em logs desnecessários;
12. UI administrativa para configuração;
13. testes determinísticos sem envio real de e-mail no CI;
14. documentação operacional e troubleshooting.

Fora do escopo inicial:

- SMS;
- WhatsApp;
- push mobile;
- notificação no navegador;
- e-mail de cobrança para terceiros;
- alerta de conta atrasada após D0;
- botão de pagamento no e-mail;
- pagamento automático;
- leitura da caixa Gmail;
- OAuth/Login com Google;
- serviços pagos de e-mail;
- alteração automática de vencimento, valor, categoria ou status financeiro.

## Fonte de verdade obrigatória

Claude deve reler, no início de cada slice:

- `docs/ROADMAP.md`;
- `docs/NEXT_STEPS.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `README.md`;
- `compose.yaml`;
- `.env.example`;
- `.github/workflows/ci.yml`;
- `app/models.py`;
- `app/api.py`;
- `app/config.py`;
- migrations Alembic atuais;
- implementação canônica de `Obligation` e qualquer serviço de recorrência/projeção já existente;
- implementação de papéis/autorizações efetivamente mesclada em `main`;
- implementação MFA efetivamente mesclada, se já estiver integrada quando este slice começar.

Se a `main` mais nova tiver contrato diferente deste documento, Claude deve apresentar Technical Challenge com evidência concreta antes de inventar uma segunda política.

## Estado financeiro canônico

Na `main` na data deste documento, `Obligation` já contém, entre outros:

- `household_id`;
- `name`;
- `due_date`;
- `amount`;
- `recurrence_months`;
- `occurrence_count`;
- `category`;
- `status`;
- `paid_at`;
- `active`;
- referências de pagamento/funding.

O sistema de alertas deve **observar** esse estado; ele não se torna um novo motor financeiro.

### Regra fundamental

O worker de alertas não pode:

- criar uma despesa;
- criar um pagamento;
- alterar valor;
- alterar vencimento;
- marcar obrigação como paga;
- materializar parcelas/recorrências por política própria;
- recalcular projeções;
- reclassificar transações;
- alterar snapshots;
- resolver finding de Integridade.

Se uma recorrência ou parcela futura precisar existir para ser alertada, deve vir do mecanismo financeiro canônico já existente. O worker não pode criar um segundo cálculo de recorrência somente para disparar e-mail.

## Elegibilidade de alerta

Uma ocorrência concreta é elegível somente quando todas as condições forem verdadeiras:

1. pertence ao `household_id` correto;
2. `active = true`;
3. não está paga segundo o contrato canônico vigente (`status`/`paid_at`/relação de pagamento conforme a implementação de `main`);
4. possui `due_date` válida;
5. possui destinatário ativo;
6. o tipo de alerta (`D-1` ou `D0`) está habilitado para aquele destinatário;
7. o horário mínimo de disparo do dia já foi alcançado no timezone configurado;
8. não existe entrega `sent` para a mesma chave lógica.

Regras de data:

- `D-1`: `obligation.due_date == local_today + 1 dia`;
- `D0`: `obligation.due_date == local_today`.

Usar sempre a data local no timezone configurado; não comparar `datetime.utcnow()` diretamente com `Date` de vencimento.

## Não usar flags de envio dentro de Obligation

Não adicionar campos como `alerta_d1_enviado` e `alerta_d0_enviado` diretamente em `obligations`.

Motivo: o sistema aceita vários destinatários e preferências diferentes. Um único booleano na obrigação não representa corretamente `obrigação x vencimento x tipo de alerta x destinatário`, além de misturar estado financeiro com estado de entrega.

A arquitetura normativa é manter entrega em estrutura própria.

## Modelo de dados esperado

Claude deve confrontar o schema vigente e aplicar a menor modelagem aditiva coerente. Estrutura lógica preferencial:

### `notification_settings`

Um registro por household, contendo no mínimo:

- `household_id` único;
- `enabled`;
- `send_time_local`;
- `timezone`;
- timestamps.

### `notification_recipients`

Um ou mais registros por household:

- `id`;
- `household_id`;
- `email`;
- `active`;
- `notify_d1`;
- `notify_d0`;
- timestamps.

Restrições:

- e-mail validado no backend;
- normalização consistente;
- unicidade por household para o endereço normalizado;
- nenhum vínculo obrigatório com `User`, pois o modelo atual não deve ganhar e-mail de login apenas para satisfazer notificações.

### `notification_deliveries`

Outbox/trilha de entrega:

- `id`;
- `household_id`;
- `obligation_id`;
- `recipient_id`;
- `obligation_due_date`;
- `alert_kind` (`d1` | `d0`);
- `status` (`pending` | `sending` | `sent` | `failed`);
- `attempt_count`;
- `next_attempt_at` nullable;
- `claimed_at` nullable;
- `sent_at` nullable;
- `last_error_code` nullable;
- `message_id` nullable;
- timestamps.

Unique constraint lógica obrigatória:

```text
(household_id, obligation_id, recipient_id, obligation_due_date, alert_kind)
```

Essa restrição é a barreira final contra duas entregas bem-sucedidas sendo criadas para o mesmo evento lógico no banco.

## Sender Gmail e secrets

A implementação inicial deve usar SMTP de forma encapsulada em um adapter de entrega, sem espalhar detalhes Gmail pelo domínio.

Configuração conceitual por ambiente:

```text
ALERT_EMAIL_ENABLED=false
ALERT_SMTP_HOST=smtp.gmail.com
ALERT_SMTP_PORT=587
ALERT_SMTP_USERNAME=
ALERT_SMTP_APP_PASSWORD=
ALERT_EMAIL_FROM=
ALERT_EMAIL_USE_STARTTLS=true
ALERT_EMAIL_TIMEOUT_SECONDS=15
```

Requisitos:

- valor real de `ALERT_SMTP_APP_PASSWORD` nunca entra no Git;
- senha não entra no banco;
- senha não entra na UI;
- senha não entra em log, exception serializada, audit event ou screenshot de documentação;
- `.env.example` contém somente placeholders seguros;
- CI usa fake/in-memory SMTP adapter e nunca tenta autenticar no Gmail real;
- ausência de configuração deve falhar de forma explícita, sem marcar mensagem como enviada.

O adapter pode continuar genericamente SMTP mesmo que o provedor inicial seja Gmail. Não criar dependência de serviço pago.

## Autorização da tela de Configurações

A alteração de destinatários, horário, timezone e ativação de alertas é configuração operacional mutável.

Quando o slice de perfis administrador/consulta já estiver integrado:

- leitura deve respeitar household isolation;
- alteração deve exigir o helper de autorização administrativo canônico de `main`;
- usuário de consulta não pode adicionar, remover ou editar destinatários;
- segredo SMTP nunca é retornado pela API, inclusive para administrador.

Não implementar uma segunda taxonomia de papéis.

## API desejada

Os nomes exatos podem ser ajustados à convenção vigente, mantendo um contrato equivalente:

```text
GET    /api/notification-settings
PUT    /api/notification-settings
POST   /api/notification-recipients
PATCH  /api/notification-recipients/{id}
DELETE /api/notification-recipients/{id}
POST   /api/notification-settings/test-email
```

`test-email`:

- somente admin;
- envia apenas para um destinatário já cadastrado/selecionado do mesmo household;
- não aceita host/username/password arbitrários enviados pelo navegador;
- não cria fato financeiro;
- registra resultado operacional sem gravar segredo;
- deve ser rate-limited de forma simples para evitar spam acidental.

## Worker/scheduler

Não colocar a lógica crítica apenas em um timer JavaScript do navegador. O envio deve acontecer no backend mesmo sem usuário com a página aberta.

Arquitetura preferencial:

- processo/serviço separado reutilizando a mesma imagem da aplicação;
- entrypoint equivalente a `python -m app.cli.notification_worker`;
- polling leve e barato;
- sem Redis/RabbitMQ para este caso de dois usuários;
- PostgreSQL é a fonte de coordenação/durabilidade;
- compatível com Docker Compose local e futura execução OCI;
- restart-safe.

O worker pode verificar periodicamente, por exemplo a cada poucos minutos, se o horário do household já chegou. A frequência exata é detalhe operacional; o contrato importante é que **D-1 e D0 sejam avaliados pela data local e enviados no máximo uma vez por destinatário/evento após o horário configurado**.

## Catch-up após indisponibilidade

O worker não deve exigir que esteja vivo exatamente às `08:00:00`.

Se o sistema iniciar mais tarde no mesmo dia:

- um D-1 ainda elegível deve ser enviado uma vez;
- um D0 ainda elegível deve ser enviado uma vez;
- não enviar retroativamente D-1 em dias posteriores;
- não criar alerta de atraso neste escopo.

Isso permite operação correta após reboot/manutenção e evita perder a janela apenas porque o processo não estava ativo no segundo exato configurado.

## Concorrência e idempotência

A implementação deve ser segura contra:

- dois loops do worker rodando por erro operacional;
- restart durante processamento;
- retry de uma falha transitória;
- duas requisições concorrentes tentando criar a mesma entrega.

Direção preferencial:

1. descobrir evento elegível;
2. criar/reservar `notification_delivery` sob unique constraint;
3. claim atômico de uma entrega pendente;
4. enviar;
5. marcar `sent` somente após sucesso confirmado pelo adapter;
6. em erro, registrar código sanitizado e reagendar retry;
7. recuperar `sending` abandonado após timeout seguro.

Não confiar somente em `SELECT` seguido de `INSERT` sem proteção de unicidade.

### Limite técnico

SMTP não oferece transação distribuída com o banco. Um crash exatamente depois do servidor SMTP aceitar a mensagem e antes do commit de `sent` pode tornar o resultado externo ambíguo. A implementação deve minimizar o risco com outbox, `Message-ID` determinístico quando tecnicamente adequado e recuperação conservadora, mas não deve afirmar garantia matemática de exactly-once fora do banco.

## Retry

Falhas transitórias devem ter retry limitado com backoff.

Exemplo inicial aceitável:

- máximo 3 tentativas por evento no mesmo dia;
- backoff crescente;
- falhas permanentes de configuração/autenticação não entram em loop agressivo;
- depois do limite, status `failed` permanece visível para diagnóstico;
- uma entrega `sent` nunca volta automaticamente para `pending`.

Claude pode ajustar números se a `main` já tiver política de retry consolidada.

## Cancelamento por pagamento

Antes de criar a entrega e novamente imediatamente antes do envio, o worker deve revalidar o estado canônico da obrigação.

Se ela tiver sido paga/desativada entre discovery e delivery:

- não enviar;
- finalizar/cancelar a tentativa de forma auditável conforme modelagem escolhida;
- não alterar o pagamento nem a obrigação.

Não enviar D0 de uma conta paga no D-1 depois do primeiro alerta.

## Conteúdo do e-mail

Assuntos conceituais:

```text
Family Finance — conta vence amanhã: <nome>
Family Finance — conta vence hoje: <nome>
```

Corpo mínimo:

- indicação clara D-1 ou D0;
- nome da obrigação;
- valor formatado em BRL;
- data de vencimento;
- categoria, quando disponível;
- aviso de que a informação vem do Family Finance.

Não incluir:

- senha;
- segredo MFA;
- token de sessão;
- conteúdo de documentos importados;
- dados bancários completos;
- credenciais;
- links mágicos de autenticação;
- botão de pagamento neste primeiro escopo.

HTML deve ter fallback texto puro.

## Privacidade e logs

Endereço de e-mail é dado pessoal operacional e deve ser tratado com minimização.

Logs devem preferir:

- `household_id`/`recipient_id`/`delivery_id` quando necessário;
- status e código de erro sanitizado;
- nunca senha SMTP;
- nunca corpo SMTP bruto;
- evitar imprimir endereço completo do destinatário em logs normais.

Audit events podem registrar criação/alteração de configuração e resultado de envio usando ids e metadados mínimos. Não duplicar conteúdo sensível no audit trail.

## Observabilidade

A UI/operador deve conseguir distinguir:

- envio desabilitado;
- sender SMTP não configurado;
- destinatário inexistente;
- mensagem pendente;
- enviada;
- falhou;
- última execução do worker;
- último erro sanitizado.

Não adicionar ferramenta paga de observabilidade.

## Migração

As migrations devem ser:

- aditivas;
- reversíveis quando tecnicamente seguro;
- sem backfill que invente destinatários;
- sem alterar fatos financeiros existentes;
- sem reescrever `Obligation`;
- compatíveis com PostgreSQL real e testes SQLite conforme política vigente.

## Testes obrigatórios

Cobrir no mínimo:

### Elegibilidade

- D-1 pendente envia;
- D0 pendente envia;
- obrigação paga não envia;
- obrigação inativa não envia;
- obrigação fora da janela não envia;
- D-1 desabilitado por destinatário não envia D-1;
- D0 desabilitado não envia D0;
- alertas globais desabilitados não enviam;
- dois destinatários ativos recebem eventos independentes.

### Data/timezone

- cálculo de `local_today` no timezone configurado;
- execução exatamente antes e depois do horário;
- catch-up no mesmo dia após o horário;
- nenhum catch-up indevido em dia posterior.

### Idempotência/concorrência

- reexecução do worker não duplica `sent`;
- dois workers concorrentes não criam duas linhas lógicas;
- retry de falha não duplica uma entrega já concluída;
- recovery de claim abandonado;
- unique constraint aplicada no banco.

### Pagamento entre descoberta e envio

- obrigação marcada como paga depois de enfileirada mas antes do SMTP não é enviada.

### Segurança/autorização

- household A não lê/edita destinatários do household B;
- consulta não altera settings após integração do RBAC;
- senha SMTP nunca aparece em response/log/audit fixture;
- endpoint test-email não aceita credenciais arbitrárias do cliente;
- e-mail inválido é rejeitado no backend.

### Delivery adapter

- sucesso SMTP mockado;
- erro de autenticação sanitizado;
- timeout transient com retry;
- HTML e texto puro gerados;
- `Message-ID`/metadados estáveis quando aplicável;
- nenhuma rede externa no CI.

### Regressão financeira

- nenhum teste de envio modifica `Transaction`;
- nenhum teste de envio modifica valor/vencimento/status de `Obligation` exceto fixtures que simulam pagamento deliberadamente;
- invariants financeiros existentes permanecem verdes.

## Critérios de aceite do trabalho completo

O trabalho só está concluído quando:

1. administrador consegue configurar um ou vários destinatários na UI;
2. credenciais Gmail não aparecem na UI nem no banco;
3. D-1 é enviado uma única vez por destinatário/evento elegível;
4. D0 é enviado uma única vez por destinatário/evento elegível;
5. conta paga antes do disparo não gera alerta;
6. sistema recupera execução perdida no mesmo dia após restart;
7. falha de Gmail não é marcada como sucesso;
8. retries são limitados e auditáveis;
9. household isolation e RBAC estão preservados;
10. nenhum dado financeiro é alterado pelo worker;
11. CI/testes usam adapter falso, sem Gmail real;
12. Docker/OCI conseguem manter o worker executando sem depender do navegador;
13. documentação explica configuração segura do Gmail e troubleshooting;
14. todos os gates exigidos pelo repositório estão verdes;
15. merge é feito somente pelo engenheiro responsável.

## Rollback

Rollback deve ser seguro:

1. desabilitar `ALERT_EMAIL_ENABLED` ou configuração global do household;
2. parar o worker;
3. preservar histórico de `notification_deliveries` para diagnóstico;
4. reverter código se necessário;
5. downgrade de migration somente com procedimento explícito e sem tocar em tabelas financeiras.

Nenhum rollback de alertas deve alterar pagamentos, obrigações ou transações.

## Divisão em slices

A implementação deverá ser dividida em slices pequenos e revisáveis:

### MAIL-00 — modelo, configuração e contratos

- migrations aditivas;
- settings/recipients;
- validação de e-mail;
- APIs e autorização;
- UI de Configurações;
- sem scheduler real e sem Gmail real.

### MAIL-01 — adapter SMTP Gmail e templates

- configuração segura por ambiente;
- adapter SMTP;
- HTML + texto puro;
- test-email administrativo;
- mocks/fakes no CI;
- sanitização de erros.

### MAIL-02 — outbox, scheduler, idempotência e retry

- `notification_deliveries`;
- cálculo D-1/D0;
- worker dedicado;
- claim concorrente;
- retry/catch-up/recovery;
- revalidação de pagamento imediatamente antes de enviar.

### MAIL-03 — integração operacional e validação E2E

- Docker/Compose/OCI conforme `main` vigente;
- health/observabilidade mínima;
- runbook Gmail;
- smoke tests;
- validação ponta a ponta com conta de teste controlada;
- regressão completa e gates.

Cada slice deve ter seu próprio Work Order de execução quando iniciado, branch isolada, Draft PR, revisão, CI e merge pelo engenheiro.

## Saída esperada do Claude

Para cada slice autorizado:

1. reler documentação e código atual de `main`;
2. declarar arquivos/contratos que serão tocados;
3. implementar somente o escopo do slice;
4. adicionar testes determinísticos;
5. executar os gates aplicáveis;
6. documentar riscos e divergências;
7. abrir Draft PR;
8. responder a bloqueios técnicos com evidência;
9. não fazer merge;
10. não avançar automaticamente para o próximo slice antes do merge/revisão do anterior.
