# Runbook — WhatsApp Gateway (WA-01..WA-07)

Este runbook cobre a operação completa do gateway WhatsApp do Family Finance
AI Assistant (`docs/WHATSAPP_AI_ASSISTANT_PLAN.md`, epic #71, slices
WA-01..WA-07): setup seguro das credenciais Meta, o serviço
`whatsapp-gateway` em Docker Compose, seus health checks, o fluxo de texto e
de mídia (foto/áudio/documento), troubleshooting por código sanitizado,
rotação de segredos e rollback. É preciso e reflete exatamente o que está
implementado neste PR — não é aspiracional. Não altera nenhuma regra
financeira: escrita só acontece através dos mesmos caminhos determinísticos
já usados pela web (`app.api._confirm_capture_items`,
`app.services.assistant_actions.execute_typed_action`).

## 1. Visão geral dos componentes

| Componente | Onde vive | O que faz |
|---|---|---|
| `app/whatsapp_gateway_app.py` | Código + serviço `whatsapp-gateway` no `compose.yaml` | ASGI app isolada, a única superfície pública do projeto. `GET/POST /webhooks/whatsapp`, `GET /health`, `GET /health/metrics`. |
| `app/services/whatsapp_gateway.py` | Código | Primitivas de transporte: assinatura HMAC, handshake, hash/cifra de telefone, rate limit, idempotência, `WhatsAppProvider` (`FakeWhatsAppProvider`/`MetaCloudApiProvider`, com retry/backoff). |
| `app/services/whatsapp_media.py` | Código | Guarda de MIME/tamanho de mídia e o classificador determinístico de resposta "confirmar"/"cancelar". |
| `whatsapp_authorized_numbers` | Postgres (migração `0024`, `0027`) | Allowlist número → usuário/household + `pending_capture_id` (WA-07: qual `CaptureDraft` de mídia está aguardando confirmação por este número). |
| `whatsapp_inbound_events` | Postgres (migração `0024`, `0025`) | Idempotência (`provider_message_id` único) + trilha sanitizada de status, nunca o texto/mídia da mensagem. |
| `whatsapp_rate_limit_buckets` | Postgres (migração `0024`) | Janela fixa por `phone_hash`. |
| `app.services.assistant_orchestrator.plan_and_execute` | Código (reuso, não novo) | Texto livre → Tool Layer → typed actions. O mesmo caminho de `POST /assistant/ask` da web. |
| `app.services.smart_capture` + `app.api._analyze_and_persist_capture`/`_confirm_capture_items` | Código (reuso, não novo) | Foto/áudio/documento → OCR local/Whisper local → `CaptureDraft` → confirmação explícita → `Transaction`/`Obligation`/`PayrollRecord`. Mesmo pipeline do upload web. |

## 2. Configuração segura das credenciais Meta

Nenhuma credencial real do WhatsApp Business/Meta é colocada neste
repositório, em `.env.example` ou em fixtures de teste — apenas em `.env`
local do operador.

1. Crie um app Meta Business com o produto WhatsApp habilitado.
2. Gere um **App Secret** (usado para verificar `X-Hub-Signature-256`) e um
   **Verify Token** (usado só no handshake `GET`, escolhido pelo operador).
3. Gere a chave dedicada de telefone (nunca reutilize `SECRET_KEY`/
   `FILE_ENCRYPTION_KEY`/`MFA_ENCRYPTION_KEY`):
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
4. Preencha em `.env` (nunca versionado):
   ```bash
   WHATSAPP_GATEWAY_ENABLED=true
   WHATSAPP_APP_SECRET=<app secret real>
   WHATSAPP_VERIFY_TOKEN=<verify token escolhido>
   WHATSAPP_PHONE_ENCRYPTION_KEY=<chave Fernet gerada acima>
   WHATSAPP_RATE_LIMIT_MAX_PER_WINDOW=30
   WHATSAPP_RATE_LIMIT_WINDOW_SECONDS=60
   WHATSAPP_GATEWAY_PORT=8091
   # Envio de respostas (WA-03) e busca de mídia (WA-07) -- mesmo par de
   # credenciais para ambos, é a mesma Cloud API:
   WHATSAPP_ACCESS_TOKEN=<system user access token>
   WHATSAPP_PHONE_NUMBER_ID=<phone number id>
   WHATSAPP_API_BASE_URL=https://graph.facebook.com/v21.0
   ```
5. Opcional (WA-07 — todos têm default seguro, só ajuste se necessário):
   ```bash
   WHATSAPP_SEND_MAX_ATTEMPTS=3
   WHATSAPP_SEND_BACKOFF_SECONDS=0.2
   WHATSAPP_MEDIA_FETCH_MAX_ATTEMPTS=3
   WHATSAPP_MEDIA_FETCH_BACKOFF_SECONDS=0.2
   WHATSAPP_MEDIA_FETCH_TIMEOUT_SECONDS=20
   WHATSAPP_MEDIA_MAX_MB=16
   ```
6. Suba o serviço (ver §3) e complete o handshake de assinatura do webhook
   no painel Meta apontando para `https://<seu-host-público>/webhooks/whatsapp`
   com o mesmo `WHATSAPP_VERIFY_TOKEN`.
7. Vincule números autorizados pela UI web (admin) em **Configurações →
   Números do WhatsApp** — nunca por texto/payload da própria mensagem
   (`household_id`/`user_id` são sempre resolvidos no servidor).

`WHATSAPP_APP_SECRET`/`WHATSAPP_VERIFY_TOKEN`/`WHATSAPP_ACCESS_TOKEN`/
`WHATSAPP_PHONE_ENCRYPTION_KEY` nunca aparecem em log, exceção serializada,
evento de auditoria, resposta de API ou nos códigos sanitizados da §6.

### 2.1 Exposição pública (fora de escopo deste PR)

O `whatsapp-gateway` é o único componente do projeto pensado para ficar
atrás de um proxy reverso HTTPS público (`docs/ADR_WA_00_AI_ASSISTANT_DISCOVERY.md`
§4.2). Provisionar esse endpoint público (DNS, certificado, proxy) é
trabalho de infraestrutura/deploy explicitamente fora de escopo aqui —
**nunca** envolve Oracle/OCI (#59-65, permanece congelado). `WHATSAPP_GATEWAY_PORT`
só expõe a porta no host para teste local do contrato do webhook.

## 3. Subindo o serviço

```bash
docker compose up -d db app whatsapp-gateway
```

- `whatsapp-gateway` reusa a mesma imagem de `app` (`command: ["gateway"]`),
  mesmo padrão de `notification-worker`/`cvm-worker`.
- `profiles: ["whatsapp-gateway"]`: nunca sobe com `docker compose up`
  simples — é preciso `--profile whatsapp-gateway` ou listar o serviço
  explicitamente, o mesmo "ship disabled by default" de
  `WHATSAPP_GATEWAY_ENABLED=false`.
- `depends_on: app: condition: service_healthy`: garante que as migrations
  (inclusive `0027`, WA-07) já rodaram.
- `networks: [internal, lan]` (corrigido nesta PR — WA-01 originalmente só
  tinha `internal`, o que teria impedido `send_message`/`fetch_media` de
  alcançar `graph.facebook.com`; ver commit "whatsapp-gateway needs outbound
  network for Meta API calls").
- `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges` — mesmo
  hardening de container de `notification-worker`/`advisor`.

Validar que subiu:

```bash
docker compose ps whatsapp-gateway
docker compose logs -f whatsapp-gateway
curl -s http://localhost:${WHATSAPP_GATEWAY_PORT:-8091}/health
```

## 4. Verificando saúde e observabilidade operacional

### 4.1 `GET /health`

```json
{"status": "healthy", "gateway_enabled": true}
```

`gateway_enabled=false` significa que uma das quatro variáveis obrigatórias
(`WHATSAPP_GATEWAY_ENABLED`/`WHATSAPP_APP_SECRET`/`WHATSAPP_VERIFY_TOKEN`/
`WHATSAPP_PHONE_ENCRYPTION_KEY`) está ausente — o webhook responde `404` em
vez de processar qualquer coisa (fail-closed).

### 4.2 `GET /health/metrics` (WA-07, novo)

```json
{
  "window_minutes": 60,
  "inbound_by_status": {"processed": 12, "needs_clarification": 2, "unauthorized": 1},
  "outbound_configured": true,
  "gateway_enabled": true
}
```

Contagens agregadas de `whatsapp_inbound_events.status` na última hora —
nunca uma listagem por mensagem, nunca telefone, texto, valor financeiro ou
segredo. `outbound_configured=false` significa que uma resposta não pode
ser enviada (mas mensagens ainda são recebidas/processadas/persistidas).
Use esse endpoint para dashboards/alertas externos sem tocar no banco
diretamente.

### 4.3 Logs estruturados sanitizados (WA-07)

Todo evento relevante do fluxo de mídia é logado com `trace_id` (o mesmo
`trace_id` do `AuditEvent`/`AssistantActionEvent` correspondente), nunca com
texto, descrição, valor ou segredo:

`whatsapp_gateway.inbound_accepted`, `.media_fetched`, `.media_fetch_failed`,
`.media_rejected`, `.media_duplicate_content`, `.capture_created`,
`.capture_confirmed`, `.capture_cancelled`, `.capture_large_amount_guard`,
`.capture_confirm_rejected`, `.capture_confirm_failed`,
`.orchestration_completed`, `.orchestration_failed`,
`.pending_capture_stale`, `.send_failed`, `.send_not_configured`.

## 5. Fluxo de mídia (WA-07) — o que o household vê

1. Household envia foto/áudio/PDF pelo WhatsApp.
2. Gateway busca a mídia (Meta Cloud API, com retry/backoff limitado),
   valida MIME/tamanho, roda o mesmo pipeline OCR local (`pytesseract`)/
   transcrição local (`faster_whisper`) da captura web, e responde com um
   resumo do que entendeu + `"Responda 'confirmar' para lançar ou
   'cancelar' para descartar."`. **Nada é gravado ainda.**
3. Household responde `"confirmar"` (ou `sim`/`ok`/...) ou `"cancelar"`
   (ou `não`/...) — classificador determinístico, nunca via LLM.
4. Se o valor for considerado alto (mesmo limite de `_large_entry_threshold`
   da web), o gateway pede uma segunda confirmação explícita:
   `"confirmar valor alto"` — nunca passa direto numa resposta genérica
   `"sim"`.
5. Somente depois da confirmação explícita, `app.api._confirm_capture_items`
   (o mesmo caminho de escrita da web) cria o
   `Transaction`/`Obligation`/`PayrollRecord`, com auditoria completa
   (`AuditEvent` com `source="whatsapp"`, `trace_id` correlacionado).
6. Se a extração for ambígua (sem valor, sem conta/cartão identificável), o
   gateway pede esclarecimento e nunca oferece confirmação — a captura fica
   com status `needs_input` (nenhuma conta/cartão) ou aguardando conclusão
   pelo site.

Reentrega/redelivery da mesma mensagem (mesmo `provider_message_id`,
inclusive de mídia ou da resposta de confirmação) nunca duplica nada — a
barreira é `uq_whatsapp_inbound_events_message_id`, a mesma de WA-01.

## 6. Troubleshooting por código sanitizado

| Código/log | Significado | Ação |
|---|---|---|
| `whatsapp_gateway.media_fetch_not_configured` | `WHATSAPP_ACCESS_TOKEN`/`WHATSAPP_PHONE_NUMBER_ID` ausentes | Complete a §2 item 4 e recrie o serviço. |
| `whatsapp_gateway.media_fetch_failed` | Falha ao baixar a mídia da Meta (após esgotar `WHATSAPP_MEDIA_FETCH_MAX_ATTEMPTS`) | Rede/token pode estar com problema; veja `error` no log (nunca a URL/token). Household pode reenviar. |
| `whatsapp_gateway.media_rejected` (`reason=mime_not_allowed`) | Formato de arquivo fora do allowlist (imagem: jpeg/png/webp; áudio: ogg/mp3/m4a/aac/amr; documento: pdf) | Esperado — oriente o household a reenviar em formato suportado. |
| `whatsapp_gateway.media_rejected` (`reason=media_too_large`) | Arquivo maior que `WHATSAPP_MEDIA_MAX_MB` | Esperado — oriente reenvio menor, ou aumente o limite se legítimo. |
| `whatsapp_gateway.media_duplicate_content` | Mesmo arquivo (sha256) já enviado antes para este household | Não é erro — evita duplicar `Document`/`CaptureDraft`; household já tem a captura original. |
| `whatsapp_gateway.capture_large_amount_guard` | Valor acima do limite de confirmação sem a frase de override | Esperado — household deve responder `"confirmar valor alto"` se o valor estiver correto. |
| `whatsapp_gateway.capture_confirm_rejected` | `_confirm_capture_items` recusou (ex.: número não-admin, conta inválida) | Verifique se o número é admin (`whatsapp_authorized_numbers`); mensagem já é enviada ao household. |
| `whatsapp_gateway.capture_confirm_failed` | Erro inesperado ao confirmar (dados corrompidos, exceção não tratada) | Verifique `docker compose logs whatsapp-gateway`; nenhuma escrita parcial fica pendente (`db.rollback()` sempre roda antes de responder). |
| `whatsapp_gateway.orchestration_failed` | `plan_and_execute` (fluxo de texto) lançou uma exceção | Verifique `docker compose logs whatsapp-gateway` e a saúde do `advisor`/Codex; fluxo web continua funcionando normalmente (LLM é opcional). |
| `whatsapp_gateway.send_not_configured` / `.send_failed` | Não conseguiu enviar a resposta | A mensagem já foi processada/persistida (se aplicável) — apenas a resposta ao household falhou. Verifique credenciais de envio (§2). |
| `whatsapp_gateway.configuration_error_at_use` | `WHATSAPP_PHONE_ENCRYPTION_KEY` ausente/inválida no momento de uso | Fail-closed — nada é processado. Corrija a chave e recrie o serviço. |
| `whatsapp_gateway.stale_authorized_number` | O `user`/`household` do número vinculado não bate mais (usuário removido/movido) | Revincule o número pela UI web. |

Nenhum desses logs inclui o texto da mensagem, o valor do lançamento, o
telefone em texto claro, o token de acesso ou o app secret.

## 7. Rotação de segredos

Nenhuma rotação abaixo apaga dado financeiro ou histórico de auditoria.

1. **`WHATSAPP_APP_SECRET`**: gere um novo no painel Meta, atualize `.env`,
   `docker compose up -d --force-recreate whatsapp-gateway`. Webhooks
   assinados com a chave antiga passam a falhar (`403`) até o painel Meta
   também ser atualizado com o mesmo valor — coordene a troca nos dois
   lados quase simultaneamente para não perder mensagens (Meta reentrega
   automaticamente; nada é perdido, apenas atrasado).
2. **`WHATSAPP_ACCESS_TOKEN`**: gere um novo system-user token, atualize
   `.env`, recrie o serviço. Envio/busca de mídia voltam a funcionar
   imediatamente; nenhuma mensagem em trânsito é afetada (idempotência via
   `provider_message_id` é independente do token).
3. **`WHATSAPP_PHONE_ENCRYPTION_KEY`**: **não é uma rotação simples** — essa
   chave deriva `phone_hash` (usado para localizar o remetente) e
   criptografa `phone_encrypted` (exibição admin). Trocá-la sem migrar os
   dados existentes torna toda a allowlist atual ilegível/inacessível
   (mesma limitação documentada em `WhatsAppConfigurationError`). Se for
   estritamente necessário, planeje uma migração de dados dedicada (recifrar
   `phone_encrypted` com a chave nova, recalcular `phone_hash`) antes de
   trocar a variável — fora de escopo deste runbook, registre como Work
   Order separado se for necessário.
4. **`WHATSAPP_VERIFY_TOKEN`**: pode ser trocado a qualquer momento;
   só é usado no handshake `GET` inicial. Atualize `.env` e o painel Meta
   juntos.

## 8. Rollback

Nenhum rollback deste gateway apaga ou reinterpreta um
`Transaction`/`Obligation`/`PayrollRecord` já confirmado — toda escrita
passa pelo mesmo caminho auditável da web, que nunca é revertido
automaticamente.

1. **Desabilitar sem parar o container:** `WHATSAPP_GATEWAY_ENABLED=false`
   em `.env` + `docker compose up -d --force-recreate whatsapp-gateway` —
   o serviço volta a responder `404` em qualquer rota, sem processar nada.
2. **Parar o serviço, preservando configuração:**
   ```bash
   docker compose stop whatsapp-gateway
   ```
   Números autorizados, `CaptureDraft`s pendentes e todo histórico
   permanecem intactos; a UI web continua funcionando normalmente (o
   Assistente web nunca depende deste gateway).
3. **Remover o serviço do Compose** (reverter `compose.yaml` a um commit
   anterior) — nenhuma tabela é apagada.
4. **Reverter o código deste PR:** `git revert`/checkout do commit anterior,
   mantendo as migrations `0024`-`0027` aplicadas (todas aditivas/
   reversíveis) ou:
5. **Downgrade da migration `0027`, apenas se necessário:**
   ```bash
   docker compose exec app alembic downgrade 0026
   ```
   Remove somente `whatsapp_authorized_numbers.pending_capture_id` — um
   número com uma captura de mídia aguardando confirmação simplesmente
   perde essa referência (o `CaptureDraft` em si, e seu histórico, não é
   afetado; o household reenvia a mídia ou confirma pelo site).
6. Uma captura de mídia **já confirmada** (com `Transaction`/`Obligation`/
   `PayrollRecord` gravado) nunca é revertida por este rollback — use o
   fluxo de estorno/exclusão manual já existente na web, exatamente como
   qualquer outro lançamento.

## 9. Custos e dependências (revalidação WA-07)

Nenhuma dependência paga recorrente nova foi introduzida por este slice:

- OCR: `pytesseract`/Tesseract local (já em `Dockerfile`), mesma engine da
  captura web.
- Transcrição de áudio: `faster_whisper` local (`WhisperModel` baixado uma
  vez para `MODELS_DIR`), **não** uma API paga — já documentado como tal em
  `docs/ADR_WA_00_AI_ASSISTANT_DISCOVERY.md`.
- LLM (Codex/Advisor): opcional e só usado pelo fluxo de **texto**
  (`plan_and_execute`) e, quando confiança baixa, para reclassificar uma
  captura — nunca requerido pelo pipeline de mídia em si. Indisponível =
  degrada, nunca bloqueia (ver `docs/ADR_WA_00_AI_ASSISTANT_DISCOVERY.md`
  §3.3 item "c").
- Meta WhatsApp Cloud API: custo de mensageria já revalidado/documentado no
  discovery WA-00 (`docs/ADR_WA_00_AI_ASSISTANT_DISCOVERY.md` §10); nenhuma
  ativação de número/tier pago é feita por este PR.

Se uma necessidade de API paga surgir no futuro, o processo é o já
documentado: Technical Challenge explícito antes de qualquer ativação —
nunca silencioso.

## 10. Regressão obrigatória antes de qualquer mudança futura neste gateway

```bash
pytest -q tests/test_whatsapp_gateway.py tests/test_whatsapp_write_flow.py \
  tests/test_whatsapp_authorized_numbers_api.py tests/test_whatsapp_media_guards.py \
  tests/test_whatsapp_media_e2e.py tests/test_whatsapp_provider_retry.py \
  tests/test_migrations.py
ruff check .
```

Mais os gates completos de `.github/workflows/ci.yml` (`docker-build` valida
`docker compose config -q` com `networks: [internal, lan]`;
`alembic-migration`/`integration-postgres` validam a migration `0027`
contra PostgreSQL real).
