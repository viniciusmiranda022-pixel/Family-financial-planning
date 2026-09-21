# Runbook — alertas de vencimento por e-mail (MAIL-00..04)

Este runbook cobre a operação completa dos alertas de vencimento por e-mail
(`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`,
`docs/WORK_ORDER_MAIL_04_UI_SMTP_CONFIG.md`): configuração do remetente SMTP
(pela interface ou por `.env`), o serviço `notification-worker` em Docker
Compose, seu health check, troubleshooting por código de erro sanitizado e
rollback. Não altera nenhuma regra de elegibilidade D-1/D0 -- essas
continuam exclusivamente em `app/services/notification_scheduler.py`.

## 1. Visão geral dos componentes

| Componente | Onde vive | O que faz |
|---|---|---|
| `notification_settings` / `notification_recipients` | Postgres (migração `0020`) | Configuração por household: ativo, horário, fuso, destinatários e preferências D-1/D0 (MAIL-00). |
| `notification_deliveries` | Postgres (migração `0021`) | Outbox/trilha de entrega -- uma linha por evento lógico `(household, obrigação, destinatário, vencimento, tipo)` (MAIL-02). |
| `notification_worker_heartbeats` | Postgres (migração `0022`) | Linha única com a última execução do worker: início, fim, sucesso/falha, contagens, último erro sanitizado (MAIL-03). |
| `smtp_sender_configs` | Postgres (migração `0028`) | Linha única, global, com o remetente SMTP configurado pela interface -- App Password criptografada em repouso (MAIL-04). |
| `app.services.email_delivery.SmtpEmailAdapter` | Código | Transporte SMTP/STARTTLS. Nunca decide elegibilidade, nunca persiste, nunca decide de onde vem sua própria credencial (MAIL-01). |
| `app.services.smtp_config` | Código | Criptografia da App Password e resolução da configuração SMTP *efetiva* (banco vs. `ALERT_SMTP_*`) -- a única fonte dessa decisão (MAIL-04). |
| `app.cli.notification_worker` | Código + serviço `notification-worker` no `compose.yaml` | Descobre, reivindica, renderiza, envia e finaliza cada entrega, em loop (MAIL-02/03). Resolve a configuração SMTP efetiva a cada passada -- sem cache, sem restart (MAIL-04). |
| `GET /notification-settings/status` | API | Leitura operacional: alertas ativos, sender configurado, última execução do worker, contagens de entrega por status (MAIL-03). |
| `GET`/`PUT /smtp-config` | API (admin apenas) | Lê metadados sanitizados (nunca a senha) e salva o remetente SMTP (MAIL-04). |

## 2. Configurando o remetente SMTP

Duas formas coexistem, com precedência explícita: uma configuração salva
pela interface, **habilitada e completa**, tem prioridade total sobre as
variáveis `ALERT_SMTP_*` -- nunca uma mistura de campos das duas fontes
(`app.services.smtp_config.resolve_effective_smtp_settings`). Se nenhuma
configuração foi salva pela interface, ou ela está desabilitada/incompleta,
o sistema usa `ALERT_SMTP_*` inteiramente, como antes do MAIL-04.

### 2.1 Pela interface (recomendado -- sem editar arquivos, sem restart)

1. Gere a chave de criptografia dedicada, uma única vez por instalação, e
   adicione a `.env` (nunca versionada):
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   ```bash
   SMTP_ENCRYPTION_KEY=<chave gerada>
   ```
   Reinicie `app` e `notification-worker` **uma única vez** para carregar
   essa chave nova:
   ```bash
   docker compose up -d --force-recreate app notification-worker
   ```
   Instalações que nunca definem `SMTP_ENCRYPTION_KEY` continuam
   funcionando exatamente como antes (fallback `ALERT_SMTP_*`) -- essa
   chave só é obrigatória para usar o painel de SMTP da interface.
2. Ative a verificação em duas etapas na conta Gmail remetente (obrigatório
   para gerar uma App Password) e gere uma **App Password** em
   <https://myaccount.google.com/apppasswords> (nunca use a senha normal de
   login).
3. Em **Configurações → Remetente SMTP** (admin apenas), preencha
   servidor/porta/usuário/remetente/STARTTLS/timeout e a App Password, e
   marque "Sim" em "Envio pelo remetente configurado aqui". Salvar não
   exige restart nem edição de `.env` -- a próxima passada do worker e o
   próximo teste já usam essa configuração.
4. Deixar a App Password em branco ao editar preserva a senha já salva;
   "Remover senha salva" é a única forma explícita de apagá-la.
5. Cadastre destinatários na tela **Configurações → Alertas de vencimento
   por e-mail** e use o botão **Testar** de um destinatário já cadastrado --
   isso chama `POST /notification-settings/test-email`, que sempre usa a
   configuração SMTP efetiva (banco, se habilitada e completa; senão
   `ALERT_SMTP_*`), nunca um host/usuário/senha vindo do navegador.

### 2.2 Por `.env` (fallback, compatível com instalações anteriores ao MAIL-04)

1. Ative a verificação em duas etapas e gere uma App Password (igual acima).
2. Preencha em `.env` (nunca em `.env.example`, nunca versionado):
   ```bash
   ALERT_EMAIL_ENABLED=true
   ALERT_SMTP_HOST=smtp.gmail.com
   ALERT_SMTP_PORT=587
   ALERT_SMTP_USERNAME=seu-remetente@gmail.com
   ALERT_SMTP_APP_PASSWORD=xxxxxxxxxxxxxxxx
   ALERT_EMAIL_FROM=seu-remetente@gmail.com
   ALERT_EMAIL_USE_STARTTLS=true
   ```
3. Reinicie `app` e `notification-worker` para carregar o novo `.env`:
   ```bash
   docker compose up -d --force-recreate app notification-worker
   ```
   Essa via continua funcionando exatamente como antes do MAIL-04, desde
   que nenhuma configuração habilitada/completa tenha sido salva pela
   interface (seção 2.1) -- nesse caso ela é ignorada por inteiro, nunca
   combinada campo a campo com a configuração do banco.

Em nenhum dos dois caminhos a App Password aparece em log, exceção
serializada, evento de auditoria, response da API/UI ou screenshot desta
documentação -- apenas os códigos sanitizados da seção 5 abaixo, e, no
caminho da interface, apenas o booleano "senha configurada".

## 3. Subindo o worker

```bash
docker compose up -d db app notification-worker
```

- `notification-worker` reusa a mesma imagem de `app`
  (`scripts/entrypoint.sh worker` → `python -m app.cli.notification_worker`,
  sem `--once`, loop contínuo a cada `NOTIFICATION_WORKER_POLL_SECONDS`).
- `depends_on: app: condition: service_healthy` garante que a migration
  (incluindo `0022`) já rodou antes do worker iniciar -- o worker nunca roda
  `alembic upgrade head` por conta própria.
- `restart: unless-stopped`: sobrevive a reboot do host e a um crash do
  processo, sem depender do navegador aberto (Work Order, "Worker/
  scheduler").
- Sem `ports:` e sem o volume `document_data`: o worker nunca serve HTTP e
  nunca precisa de um documento.

Validar que subiu:

```bash
docker compose ps notification-worker
docker compose logs -f notification-worker
```

## 4. Verificando saúde operacional

### 4.1 Container (Docker)

```bash
docker compose ps notification-worker
```

A coluna de saúde reflete `app/cli/notification_worker_healthcheck.py`:
lê o heartbeat (`notification_worker_heartbeats`) e reporta `ok` se uma
passada terminou há menos de `NOTIFICATION_WORKER_POLL_SECONDS * 3`
(mínimo 5 minutos), `stale` se o worker parece travado, ou `never_run` se
nenhuma passada terminou ainda (esperado nos primeiros segundos após o
container subir -- veja `start_period: 90s` em `compose.yaml`). Nunca
imprime endereço de e-mail, id de entrega ou segredo.

### 4.2 API (para a UI/operador)

```bash
curl -s -H "Cookie: ..." https://<host>/api/notification-settings/status
```

Resposta (exemplo):

```json
{
  "enabled": true,
  "sender_configured": true,
  "worker": {
    "last_started_at": "2026-09-17T11:00:03+00:00",
    "last_finished_at": "2026-09-17T11:00:04+00:00",
    "last_run_ok": true,
    "last_error_code": null,
    "last_counts": {"discovered": 0, "sent": 1, "failed": 0, "canceled": 0, "retry_scheduled": 0, "claim_lost": 0, "exhausted_failed": 0}
  },
  "deliveries": {"pending": 0, "sending": 0, "sent": 12, "failed": 0, "canceled": 1}
}
```

Qualquer usuário autenticado do household pode ler esse endpoint (mesma
fronteira de `GET /notification-settings`) -- nada nele é segredo ou
endereço de destinatário. `worker` é `null` se nenhuma passada terminou
ainda.

## 5. Troubleshooting por código sanitizado

Todos vêm de `app.services.email_delivery.EmailErrorCode`, mais dois
específicos do worker/heartbeat -- nunca o texto bruto de exceção do
`smtplib`.

| Código | Significado | Ação |
|---|---|---|
| `not_configured` | Nenhuma configuração SMTP efetiva: nem uma configuração habilitada/completa salva pela interface, nem `ALERT_SMTP_*`/`ALERT_EMAIL_FROM` preenchidos | Complete a seção 2.1 (interface, sem restart) ou 2.2 (`.env`, recria `app`/`notification-worker`). Falha permanente -- não entra em retry. |
| `smtp_auth_failed` | Usuário/App Password incorretos ou revogados | Gere uma nova App Password (a antiga pode ter sido revogada ao trocar a senha da conta Google) e salve-a em Configurações → Remetente SMTP (ou em `ALERT_SMTP_APP_PASSWORD`, conforme qual fonte está efetiva). Falha permanente. |
| `smtp_connection_failed` | Não conseguiu conectar ao host/porta SMTP | Verifique egress de rede do container (`lan` em `compose.yaml`) e se o host/porta efetivos (interface ou `ALERT_SMTP_HOST`/`ALERT_SMTP_PORT`) estão corretos. Transitório -- entra em retry com backoff. |
| `smtp_timeout` | Servidor SMTP não respondeu dentro de `ALERT_EMAIL_TIMEOUT_SECONDS` | Normalmente transitório (rede lenta); se persistente, aumente o timeout. Transitório -- entra em retry. |
| `smtp_recipient_refused` | Servidor rejeitou o endereço do destinatário | Confirme o e-mail cadastrado em Configurações. Falha permanente. |
| `smtp_send_failed` | Outra falha SMTP não classificada acima | Verifique `docker compose logs notification-worker` (nunca inclui a senha). Transitório -- entra em retry. |
| `worker_timeout_exhausted` | Uma entrega ficou `sending` além de `NOTIFICATION_DELIVERY_STALE_AFTER_SECONDS` e já sem tentativas | O worker provavelmente crashou no meio do envio; verifique se o e-mail chegou de fato (SMTP não tem transação distribuída com o banco -- ver "Limite técnico" no Work Order) antes de qualquer ação manual. |
| `worker_pass_exception` | A própria passada (`run_once`) lançou uma exceção não tratada (ex.: banco indisponível) | Verifique `docker compose logs notification-worker` e a saúde de `db`. O heartbeat registra `ok=false` para esta passada; a próxima passada tenta de novo automaticamente. |
| `never_run` (healthcheck) | Nenhuma passada terminou ainda | Normal logo após subir; persistente além de `start_period` indica processo travado antes do primeiro `run_once` completar -- veja logs. |
| `stale` (healthcheck) | Última passada terminou há mais tempo que a janela de frescor | Worker travado ou reiniciando em loop; `docker compose logs notification-worker` e `docker compose ps` para o estado de restart. |

Nenhum desses códigos, nem a resposta de `GET /notification-settings/status`,
inclui a senha SMTP, o corpo do e-mail ou o endereço do destinatário.

## 6. Comportamento após reboot/restart (catch-up)

O worker não precisa estar vivo exatamente no horário configurado
(`send_time_local`). Se o host reiniciar e o container subir mais tarde no
mesmo dia:

- um D-1 ainda elegível é enviado uma única vez;
- um D0 ainda elegível é enviado uma única vez;
- nada é enviado retroativamente para um D-1 de um dia anterior;
- `restart: unless-stopped` traz o container de volta sem intervenção
  manual; a durabilidade de "o que já foi enviado" vive inteiramente em
  `notification_deliveries` no Postgres, nunca em memória do processo.

Validação manual pós-reboot:

```bash
docker compose down notification-worker
# simula o host ficando indisponível por um tempo
docker compose up -d notification-worker
docker compose logs -f notification-worker
curl -s .../api/notification-settings/status   # last_started_at deve avançar
```

## 7. OCI/ARM64

O worker reusa a mesma imagem/`Dockerfile` de `app`
(`python:3.12-slim` + dependências já validadas para ARM64 no backlog
Oracle/OCI, `docs/ROADMAP.md` EPIC #59) e a mesma imagem `postgres:17-alpine`
de `compose.yaml` -- nenhuma dependência nova, nativa ou binária, foi
adicionada por este slice. Nenhuma execução do backlog Oracle/OCI (#59-65)
foi antecipada; esta seção é apenas a nota de compatibilidade que o Work
Order pede ("validar compatibilidade com OCI/ARM64 conforme a `main`
vigente quando o slice começar").

## 8. Rollback

Nenhum rollback de alertas altera pagamento, obrigação ou transação -- o
worker só observa `Obligation`, nunca escreve nela.

1. **Desabilitar o envio sem parar o worker:** `PUT /notification-settings`
   com `enabled=false` por household, ou globalmente
   `ALERT_EMAIL_ENABLED=false` em `.env` + recriar `app`/`notification-worker`.
   `notification_scheduler.discover_due_deliveries` simplesmente para de
   criar novas linhas; nada em `notification_deliveries` é apagado.
2. **Parar só o worker, preservando configuração:**
   ```bash
   docker compose stop notification-worker
   ```
   A UI de Configurações continua funcionando (leitura/gestão de
   destinatários); apenas o envio automático para.
3. **Remover o worker do Compose** (reverter `compose.yaml` a um commit
   anterior a este) -- `notification_deliveries`/`notification_settings`/
   `notification_recipients` continuam intactas para diagnóstico.
4. **Reverter o código:** `git revert`/checkout do commit anterior a este
   slice, mantendo a migration `0022` aplicada (não é destrutiva) ou:
5. **Downgrade da migration `0022`, apenas se necessário:**
   ```bash
   docker compose exec app alembic downgrade 0021
   ```
   Remove somente `notification_worker_heartbeats` (recriada do zero na
   próxima passada do worker) -- nunca toca `notification_deliveries`,
   `notification_recipients`, `notification_settings` ou qualquer tabela
   financeira.

## 9. Regressão obrigatória antes de qualquer mudança futura neste worker

```bash
pytest -q tests/test_notification_worker.py tests/test_notification_settings_api.py \
  tests/test_notification_scheduler.py tests/test_email_delivery.py \
  tests/test_notification_worker_status.py
ruff check .
```

Mais os gates completos de `.github/workflows/ci.yml` (`docker-build` valida
`docker compose config -q` com o novo serviço; `alembic-migration`/
`integration-postgres` validam a migration `0022` contra PostgreSQL real).
