# Segurança e operação

## Ameaças consideradas

- exposição acidental de extratos no Git;
- acesso indevido pela rede local;
- vazamento de backup ou disco;
- reimportação e dupla contagem;
- modificação silenciosa de classificação;
- perda de chave de criptografia;
- ransomware ou falha física do servidor.

## Controles implementados

- repositório sem dados e `.gitignore` restritivo;
- documentos criptografados em repouso;
- senha com `scrypt` e salt aleatório;
- cookie de sessão `HttpOnly` e `SameSite=Strict`;
- separação da rede interna do PostgreSQL;
- contêiner da aplicação sem privilégios e com filesystem somente leitura;
- limite de tamanho de upload;
- SHA-256 por documento;
- trilha de auditoria;
- backup diário do banco.
- acesso remoto privado por identidade e dispositivo no Tailscale, sem porta pública;
- serviço Codex isolado, sem credenciais do banco e sem acesso ao volume de documentos;
- cálculo financeiro determinístico antes de qualquer explicação gerada por IA;
- OCR e transcrição locais com confirmação humana antes da gravação;
- perfis de administrador e consulta (Fase 4): toda rota mutável exige `app.api._require_admin`,
  fail-closed e verificado no backend antes de qualquer busca por id -- ver "Perfis de administrador e
  consulta (Fase 4)" em `docs/ARCHITECTURE.md`.
- autenticação multifator local obrigatória (Fase 4, TOTP RFC 6238): senha correta nunca produz sessão
  completa isoladamente -- ver "MFA local TOTP (Fase 4)" abaixo e `docs/WORK_ORDER_LOCAL_MFA_TOTP.md`.

## Antes da produção

1. Use Ubuntu Server atualizado e aplique atualizações de segurança.
2. Restrinja SSH por chave e desabilite senha quando possível.
3. Libere a porta do sistema somente para a VLAN ou rede dos usuários autorizados.
4. Para acesso remoto, use Tailscale Serve em HTTPS e mantenha o Funnel desabilitado.
5. Configure cópia externa criptografada dos backups.
6. Teste uma restauração completa.
7. Guarde o `.env` em cofre offline; ele contém a chave dos documentos.
8. Monitore espaço em disco, saúde dos contêineres e execução dos backups.

## O que não fazer

- não publicar a porta do PostgreSQL;
- não expor a aplicação (`app`, a UI/API principal) diretamente à internet -- a única exceção
  deliberada é o serviço isolado `whatsapp-gateway` (ver "Gateway WhatsApp (WA-01)" abaixo), que
  nunca compartilha banco de documentos, sessão de usuário ou qualquer rota do `app`;
- não compartilhar a conta do Tailscale nem o usuário do sistema entre Vinicius e Kelly;
- não armazenar senha de internet banking;
- não configurar `OPENAI_API_KEY` no serviço `advisor`; a API tem cobrança separada e não é necessária;
- não sincronizar a pasta de dados com serviço público sem criptografia adicional;
- não enviar `.env`, dump ou documento em chamados ou issues;
- não considerar um backup no mesmo disco como recuperação de desastre.

## HTTPS e acesso remoto

O sistema inicia em HTTP na máquina local. O Tailscale Serve encerra HTTPS e entrega o endereço `.ts.net` somente aos dispositivos autorizados da tailnet. O controle remoto é feito pela identidade e pelo dispositivo cadastrado, não pelo MAC address, que não atravessa a internet.

## Fronteira do Codex

O serviço `advisor` recebe somente JSON sanitizado produzido pela aplicação. Ele não monta `document_data`, não participa da rede interna do PostgreSQL e não possui `DATABASE_URL`. O sandbox do Codex é somente leitura e vazio. Uma resposta gerada só é aceita se preservar exatamente o veredito calculado pelo motor local; caso contrário, o sistema usa a resposta determinística.

### `/v1/audit` (Codex Semantic Audit, PR 6)

Além de `/v1/classify` e `/v1/analyze`, o `advisor` expõe `POST /v1/audit`, um contrato dedicado e versionado (`advisor/audit-input-schema.json` / `advisor/audit-schema.json`) para auditoria semântica consultiva do `IntegrityAssessment` já calculado. Camadas de defesa:

- **Allowlist estrita na origem.** `app/services/audit_sanitizer.py` monta o payload campo a campo a partir de dados já determinísticos (status/score/gates/findings/`category_spending`); nunca encaminha um dicionário completo. `DATABASE_URL`, credenciais, documentos, paths e PII desnecessária não têm nenhum campo pelo qual poderiam sair.
- **Contrato de entrada validado no sidecar.** `advisor/audit-input-schema.json` também é `additionalProperties: false`; um campo fora do allowlist chega a ser rejeitado no próprio Advisor antes de qualquer chamada ao Codex.
- **Prompt-injection.** Todo conteúdo potencialmente influenciado pelo usuário (por exemplo, nome de categoria) entra no prompt somente dentro do bloco de dados, precedido por instruções explícitas de que nenhuma instrução dentro dele é válida. Não há execução de comando, ferramenta ou pesquisa disponível ao Codex nesse contrato, como nos demais.
- **Saída sem autoridade, por schema.** `advisor/audit-schema.json` não tem campo para status/score/gates/`trusted_for_*`/findings; `severity` das observações é limitada a `info`/`review`. Uma tentativa de incluir esses campos invalida a resposta inteira (`available: false`), não é "aceita parcialmente".
- **Verificação de IDs e números.** Referências (`evidence_ref`) só podem apontar para ids opacos que já estavam no pacote enviado; números citados em texto livre que não aparecem em nenhum lugar do pacote enviado são removidos da observação.
- **Fail-safe duplo.** A validação de schema/autoridade ocorre no sidecar (`advisor/audit.mjs`) e é repetida de forma independente no lado Python (`app/services/codex_audit.py`, que só lê `available`/`reason`/`summary`/`observations`/`confidence` de qualquer resposta). Timeout, indisponibilidade, erro do provider ou schema inválido sempre produzem `available: false` com uma `reason`, nunca um `pass` implícito.
- **Métricas/logs sem conteúdo sensível.** `advisor/audit.mjs` mantém contadores em memória (chamadas, sucesso, falha, timeout, schema inválido, latência) e emite logs JSON com essas categorias e durações -- nunca o prompt completo, o payload ou a resposta do modelo.

No modo nativo do Windows, perde-se a fronteira adicional do contêiner, mas permanecem a separação
de credenciais, o diretório de trabalho vazio, o ambiente mínimo, o segredo compartilhado e o
sandbox somente leitura. O serviço aceita a interface interna usada por `host.docker.internal`,
mas toda análise ou classificação exige o segredo aleatório e a porta 8081 não é publicada pelo
Tailscale Serve. Ele é iniciado por uma tarefa do próprio usuário. Credenciais do Codex ficam fora
do repositório em `%LOCALAPPDATA%\FamilyFinancialPlanning\codex`.

## MFA local TOTP (Fase 4)

`docs/WORK_ORDER_LOCAL_MFA_TOTP.md` é a especificação normativa completa. Resumo operacional:

- todo usuário ativo (administrador ou consulta) precisa de um segundo fator TOTP confirmado para
  receber `ffp_session`; senha correta isolada só produz um cookie `ffp_mfa_pending` de curta duração
  (`MFA_ENCRYPTION_KEY`/`mfa_pending_ttl_seconds`), lido por uma dependência FastAPI distinta da que
  protege as rotas financeiras (`app.security.get_current_user` nunca lê `ffp_mfa_pending`);
- o segredo TOTP é criptografado em repouso com `MFA_ENCRYPTION_KEY` -- uma chave Fernet exclusiva,
  nunca `SECRET_KEY` nem `FILE_ENCRYPTION_KEY`; a aplicação falha explicitamente na inicialização se
  ela estiver ausente ou inválida (`app.services.mfa.MfaConfigurationError`), nunca cai para plaintext;
- recovery codes (10 por enrollment/regeneração) só existem no banco como HMAC-SHA256 keyed pela
  mesma chave -- nunca plaintext, nunca reversível;
- anti-replay e rate limit são aplicados com `UPDATE ... WHERE` atômico e condicional
  (`app.api._accept_timestep`/`_register_mfa_failure`), sem depender de `SELECT ... FOR UPDATE`, então
  funcionam identicamente em SQLite (testes) e PostgreSQL (produção);
- `session_version` em `User` permite revogar todo cookie de sessão emitido antes de uma
  reconfiguração ou de um reset local -- um cookie assinado antes da existência dessa coluna (ou com
  versão divergente) falha fechado;
- reconfiguração do autenticador nunca desativa o fator anterior antes da confirmação do novo
  (`MfaFactor.pending_secret_encrypted` fica lado a lado com o `secret_encrypted` ativo até a troca);
- reset de emergência é exclusivamente local (`python -m app.cli.mfa reset --username <usuario>`,
  `app.cli.mfa.reset_mfa_for_user`) -- nunca uma rota HTTP, nunca uma senha mestra ou código universal;
- QR Code e URI `otpauth://` são gerados inteiramente no processo da aplicação (`qrcode`/`pyotp`); nenhum
  segredo ou URI é enviado a um serviço externo.

## Alertas de e-mail — SMTP e Gmail (MAIL-01)

`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #68. Cobre apenas o adapter de envio e o
`POST /notification-settings/test-email` administrativo; o scheduler/worker que efetivamente envia
D-1/D0 automaticamente é a seção "Alertas de e-mail — outbox e worker (MAIL-02)" logo abaixo.

- a credencial SMTP (`ALERT_SMTP_USERNAME`/`ALERT_SMTP_APP_PASSWORD`) só existe em variável de
  ambiente (`.env`/segredo do orquestrador) -- nunca no banco, nunca na UI, nunca em resposta de API;
  `NotificationSettings`/`NotificationRecipient` (MAIL-00) não têm coluna para ela;
- gere uma **App Password** do Gmail (exige verificação em duas etapas na conta remetente) em
  https://myaccount.google.com/apppasswords -- nunca use a senha normal de login da conta nesse campo;
- com `ALERT_EMAIL_ENABLED=false` (padrão) ou qualquer variável `ALERT_SMTP_*`/`ALERT_EMAIL_FROM`
  vazia, `app.services.email_delivery.SmtpEmailAdapter.configured` é `False` e nenhuma tentativa de
  conexão SMTP é feita -- `POST /notification-settings/test-email` responde `503` de forma explícita,
  nunca finge sucesso;
- erros de transporte (`smtplib`/socket) são reduzidos a um conjunto fixo de códigos sanitizados
  (`app.services.email_delivery.EmailErrorCode`) antes de alcançar resposta HTTP, `AuditEvent.details`
  ou qualquer log -- o texto bruto da exceção/resposta do servidor SMTP nunca é propagado;
- `test-email` é `_require_admin`-gated, envia somente para um `NotificationRecipient` já cadastrado do
  household do chamador (nunca aceita host/username/password do navegador) e é limitado a uma chamada
  por `ALERT_TEST_EMAIL_MIN_INTERVAL_SECONDS` (padrão 60s) por household
  (`app.services.email_delivery.EmailSendThrottle`, em memória -- `scripts/entrypoint.sh` roda um único
  processo `uvicorn` sem `--workers`, então o limite é efetivo, não apenas best-effort);
- o e-mail é sempre HTML com alternativa texto puro (`app.services.notification_templates`), nunca
  inclui senha, segredo MFA, token de sessão ou dado bancário completo, e todo campo de texto livre do
  household (nome da obrigação, categoria) é escapado antes de entrar no HTML;
- CI e testes automatizados (`tests/test_email_delivery.py`, `tests/test_notification_test_email_api.py`)
  sempre substituem `smtplib.SMTP`/`SmtpEmailAdapter` por um dublê determinístico -- nenhum job de CI
  autentica no Gmail real.

**Troubleshooting operacional:**

| Sintoma | Causa provável | Verificação |
| --- | --- | --- |
| `test-email` responde `503` | `ALERT_EMAIL_ENABLED=false` ou uma variável `ALERT_SMTP_*`/`ALERT_EMAIL_FROM` vazia | conferir `.env` do serviço `app` |
| `test-email` responde `502` com "Falha de autenticação" | usuário/App Password do Gmail incorretos ou revogados | gerar uma nova App Password |
| `test-email` responde `502` com "Não foi possível conectar" | host/porta incorretos ou rede sem saída para `smtp.gmail.com:587` | validar `ALERT_SMTP_HOST`/`ALERT_SMTP_PORT` e a rede do contêiner |
| `test-email` responde `429` | mais de uma chamada dentro de `ALERT_TEST_EMAIL_MIN_INTERVAL_SECONDS` | aguardar o intervalo configurado |

## Alertas de e-mail — outbox e worker (MAIL-02)

`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #69. Cobre o envio automático de D-1/D0 em si
(`app.services.notification_scheduler`, `app.cli.notification_worker`).

- a credencial SMTP continua exclusivamente em variável de ambiente (nada muda aqui em relação ao
  MAIL-01) -- o worker reusa o mesmo `SmtpEmailAdapter`, nunca lê/grava a credencial em
  `notification_deliveries`;
- `AuditEvent` gravado pelo worker (`source="notification_worker"`) tem `user_id=None` (processo de
  sistema) e `details` restrito a ids/`alert_kind`/código de erro sanitizado -- nunca o endereço de
  e-mail do destinatário, nunca o corpo do e-mail, nunca texto bruto de exceção SMTP;
- descoberta, claim e finalização são sempre filtradas por `household_id`; nenhuma consulta do
  worker lê ou grava `Obligation`/`NotificationRecipient`/`NotificationDelivery` de outro household
  (`tests/test_notification_worker.py::test_household_isolation_worker_processes_each_household_independently`);
- o worker nunca cria `Transaction`, nunca marca `Obligation` como paga, nunca altera valor/
  vencimento -- ele só observa esse estado para decidir enviar/cancelar
  (`tests/test_notification_worker.py::test_obligation_paid_between_discovery_and_send_is_canceled_not_sent`);
- CI/testes automatizados (`tests/test_notification_scheduler.py`,
  `tests/test_notification_worker.py`) sempre substituem o adapter por um dublê em memória -- nenhum
  job de CI autentica no Gmail real, igual ao MAIL-01.

## Alertas de e-mail — integração operacional e observabilidade (MAIL-03)

`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #70. Cobre o serviço `notification-worker`
(`compose.yaml`), o heartbeat `notification_worker_heartbeats` (migração `0022`) e
`GET /notification-settings/status`. Ver `docs/RUNBOOK_MAIL_ALERTS.md` para operação/troubleshooting
completos.

- a credencial SMTP continua exclusivamente em `.env`/segredo do orquestrador -- o serviço
  `notification-worker` usa `env_file: .env`, os mesmos `ALERT_SMTP_*` que `app` já usava, nunca uma
  cópia ou uma segunda fonte de configuração;
- `GET /notification-settings/status` não é `_require_admin`-gated (qualquer membro autenticado do
  household lê, mesma fronteira de `GET /notification-settings`) porque nada na resposta é segredo
  ou endereço de destinatário: `sender_configured` é um booleano, `worker` traz apenas timestamps/
  booleano/código de erro sanitizado, `deliveries` são contagens por status filtradas pelo
  `household_id` do chamador (`app.services.notification_worker_status.delivery_status_counts`,
  `tests/test_notification_worker_status.py::test_status_endpoint_never_leaks_another_households_deliveries`);
- `notification_worker_heartbeats` é uma tabela global (um único processo cobre todos os households
  numa passada), nunca filtrada por `household_id` -- por isso `worker` na resposta nunca inclui
  nada específico de household, só timestamps/booleano/código de erro do processo;
- `app/cli/notification_worker_healthcheck.py` (o `HEALTHCHECK` do contêiner) nunca imprime um
  endereço de e-mail, um id de entrega ou uma credencial -- só a palavra de status sanitizada
  (`ok`/`stale`/`never_run`) em stdout, verificado em
  `tests/test_notification_worker_healthcheck.py::test_main_exits_zero_when_healthy_and_nonzero_when_not`;
- o contêiner `notification-worker` roda com `read_only: true`, `cap_drop: [ALL]` (mais restrito que
  `app`, que precisa bindar uma porta HTTP) e `security_opt: no-new-privileges:true`, sem `ports:` e
  sem o volume `document_data` -- superfície de ataque menor que `app`, já que o processo nunca serve
  HTTP e nunca precisa ler um documento;
- uma passada que lança uma exceção não tratada nunca fica invisível: `run_once` grava o heartbeat
  com `ok=False`/`last_error_code="worker_pass_exception"` num `finally` antes de repropagar a
  exceção (`tests/test_notification_worker.py::test_run_once_records_a_failed_heartbeat_and_still_raises_when_a_pass_crashes`),
  e a mesma passada nunca deixa uma `notification_delivery`/`Transaction` órfã ou parcial (a
  transação da sessão correspondente é revertida pelo `with Session(...)` do SQLAlchemy).

## CI (PR 8)

- `.github/workflows/ci.yml` usa apenas credenciais fixas e claramente falsas, nunca reaproveitadas
  em produção: `SECRET_KEY`/`FILE_ENCRYPTION_KEY`/`MFA_ENCRYPTION_KEY` de teste (o mesmo padrão que
  `tests/test_api.py` já usa para seu próprio processo) e um usuário/senha `family`/`family` para o
  serviço PostgreSQL descartável dos jobs `alembic-migration`/`integration-postgres`, que existe só
  durante o job e é destruído ao final.
- Nenhum job de CI recebe documento, backup ou dado financeiro real; todo dado usado em teste é
  fictício (`tests/fixtures/synthetic_household.py`).
- O serviço `advisor` não participa de nenhum job de CI; `advisor-contract-security` valida apenas o
  contrato/sanitização/allowlist com o fake provider já existente, sem rede real com nenhum sidecar.

## Gateway WhatsApp (WA-01, issue #73)

- Transporte/autenticação apenas -- não interpreta mensagem, não executa ação tipada, não cria
  nenhuma `Transaction`/`Obligation`. Ver "Gateway WhatsApp — transporte e autorização (WA-01)" em
  `docs/ARCHITECTURE.md` para o desenho completo.
- Único componente do projeto pensado para ficar atrás de um endpoint público -- processo isolado
  (`app/whatsapp_gateway_app.py`), sem montar `document_data`, sem servir a UI, sem as ~150 rotas do
  `app` principal. Desligado por padrão (`WHATSAPP_GATEWAY_ENABLED=false`).
- Toda requisição POST é rejeitada (`403`, sem tocar no banco) antes de qualquer parsing se a
  assinatura `X-Hub-Signature-256` (HMAC-SHA256 do corpo bruto) não bater com `WHATSAPP_APP_SECRET`.
- Nenhum número de telefone é armazenado em claro: `phone_hash` (HMAC) é a única forma usada para
  localizar o remetente; `phone_encrypted` (Fernet, mesma chave `WHATSAPP_PHONE_ENCRYPTION_KEY`)
  existe só para exibição futura ao admin, nunca é devolvido por nenhuma rota hoje.
  `WHATSAPP_PHONE_ENCRYPTION_KEY` nunca é `SECRET_KEY`/`FILE_ENCRYPTION_KEY`/`MFA_ENCRYPTION_KEY`
  (mesma separação de chaves de `app.services.mfa`).
- Resposta HTTP idêntica (`{"status": "ok"}`) para mensagem aceita, duplicada, de remetente não
  autorizado ou limitada por taxa -- um atacante não consegue enumerar households/usuários pelo
  código/corpo da resposta.
- Nenhum texto de mensagem é persistido; `whatsapp_inbound_events` guarda só identificador/hash/
  timestamp/status, suficiente para idempotência e auditoria operacional, nunca conteúdo.
- Gerenciamento do allowlist (`POST/GET/DELETE /api/whatsapp/authorized-numbers`) é admin-only,
  fail-closed via `app.api._require_admin`, e vive no `app` principal (sessão de cookie existente) --
  nunca no processo do gateway.
- Testado inteiramente com `FakeWhatsAppProvider`/payloads fabricados
  (`tests/test_whatsapp_gateway.py`); nenhum job de CI faz chamada real à Meta.

## Backfill (`app.cli.backfill`, PR 8)

- É um comando de linha de comando executado por um operador com acesso direto ao banco/servidor,
  nunca uma rota HTTP -- não amplia a superfície de rede da aplicação nem do `advisor`.
- Não concede ao Codex/Advisor nenhuma autoridade adicional; o comando nunca chama o `advisor` e
  opera inteiramente sobre o banco local, com os mesmos serviços determinísticos do fluxo normal.
- `--dry-run` permite inspecionar o efeito antes de uma execução real sobre dados existentes; use-o
  antes de rodar sobre uma base com dados financeiros reais. Ver
  [docs/RUNBOOK_PR8_BACKFILL.md](RUNBOOK_PR8_BACKFILL.md) para o procedimento completo.
