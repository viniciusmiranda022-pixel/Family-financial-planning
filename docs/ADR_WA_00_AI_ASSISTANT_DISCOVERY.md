# ADR/Discovery — WA-00: Family Finance AI Assistant via WhatsApp

**Status:** documental (discovery + ADR). Nenhuma implementação de produção, webhook real, número
real, credencial, dado real ou migration foi feita neste PR. Nenhuma API paga foi ativada.

**Issue:** #72 (parent #71). **Work Order:** `docs/WORK_ORDER_WA_00.md`.

**Fonte normativa confrontada:** issue #72, `docs/WORK_ORDER_WA_00.md`, `docs/ARCHITECTURE.md`,
`docs/FINANCIAL_INVARIANTS.md`, `docs/INTELLIGENCE.md`, `docs/OCTOBER_GO_LIVE_REBASELINE.md`
(§11 "Assistente Financeiro"), `docs/WORK_ORDER_PR6_CODEX_SEMANTIC_AUDIT.md`, `README.md`, e código
vigente: `advisor/server.mjs`, `advisor/providers/fakeProvider.mjs`, `advisor/*.json`,
`app/services/codex_client.py`, `app/services/codex_audit.py`, `app/services/assistant_actions.py`,
`app/services/assistant_interpreter.py`, `app/services/assistant_sanitizer.py`, `app/api.py`
(`/assistant/*`), `app/models.py` (`AssistantActionProposal`), `compose.yaml`,
`advisor/Dockerfile`, `scripts/setup-codex.sh`.

**Método:** leitura direta dos documentos normativos pelo executor, duas investigações de código
independentes e somente-leitura (inventário do runtime Advisor/Codex; leitura da arquitetura e
invariantes), mais pesquisa externa sobre a WhatsApp Business Platform e sobre o mecanismo de
autenticação do Codex CLI. Toda afirmação abaixo é rastreável a `arquivo:linha` ou a uma fonte
externa citada com data.

**HEAD base:** `b8fc0879016e4ccd64118fbf96e3e932c2479226` (main, pós-merge PR #101).

---

## 0. Resumo executivo e decisão

O objetivo do WA-00 é decidir, com evidência, a arquitetura executável de um AI Assistant via
WhatsApp para o Family Finance, sem implementar nada além de documentação.

**Decisão de runtime de IA (ADR-1):** manter a ordem de prioridade determinada pelo issue #72 —
reutilizar o Codex atual primeiro. A evidência de código (§2) mostra que o Codex já opera hoje sob
exatamente o modelo de confiança que o WhatsApp Assistant precisa (LLM só interpreta/explica; o
backend determinístico calcula, valida e persiste) e que essa fronteira já é **channel-agnostic**:
os endpoints `/assistant/interpret` e `/assistant/execute` (Slice 4) não sabem se quem os chamou foi
o navegador ou outro canal. Isso torna a Opção 1 (reuso do Codex, sem custo adicional) tecnicamente
viável para dar início ao WA-01, **condicionada a três mitigações obrigatórias antes de qualquer
implementação** (detalhadas em §3.3) e a **um item que este discovery não conseguiu verificar**
(§13, TECHNICAL CHALLENGE / HUMAN BLOCKER sobre Acceptable Use Policy da OpenAI).

**Decisão de arquitetura (ADR-2):** `WhatsApp Cloud API → Gateway dedicado (novo, isolado do `app`
privado) → Orchestrator (reusa `assistant_interpreter`/`assistant_actions`) → Tool Layer tipada
(consulta/agregação/comparação/projeção somente-leitura + drafts de escrita via typed actions
existentes) → Finance Engine/DB (inalterado)`. Nenhum segundo motor financeiro é criado; nenhuma
tool dá ao LLM acesso a SQL. Ver §4-§6.

**O maior achado de risco não é técnico, é de exposição:** o `app` de hoje é uma aplicação privada
(acesso via rede interna/Tailscale — `docs/ARCHITECTURE.md`, histórico de PR #55 "proxy HTTPS
automatizado", #10 "Codex nativo no Windows"/Tailscale). Um webhook do WhatsApp **exige um endpoint
HTTPS público**, alcançável pelos servidores da Meta. Isto introduz a primeira superfície
genuinamente pública do sistema. A ADR-2 trata isso como uma fronteira de confiança nova e separada,
não uma extensão do `app` privado — ver §4.2 e §11 (risco R1).

---

## 1. Inventário do Advisor/Codex vigente

### 1.1 O que existe hoje

O sidecar Node.js `advisor/server.mjs` expõe, atrás de um segredo compartilhado
(`X-Advisor-Token`, comparado com `timingSafeEqual`, `advisor/server.mjs:50-54`), quatro rotas
consultivas — `POST /v1/classify`, `/v1/analyze`, `/v1/interpret`, `/v1/audit` — e uma rota de saúde
pública, `GET /health` (`advisor/server.mjs:215-271`). Cada chamada faz `spawn()` do CLI
`@openai/codex` como processo efêmero, sandboxed (`--sandbox read-only`, `--ephemeral`,
`--ignore-user-config`), com o prompt via stdin e saída JSON validada contra um schema local
(`advisor/server.mjs:107-166`). Não há chamada de função/tool nativa do modelo: é geração de JSON
estruturado, uma chamada por requisição, sem streaming e sem protocolo multi-turno
(`advisor/providers/fakeProvider.mjs:22-23` confirma a assinatura `(prompt, schemaFile, opts) →
JSON`).

### 1.2 Autenticação — achado central para "suitability" em runtime contínuo

**Não existe chave de API.** `scripts/setup-codex.sh:20-31` documenta o fluxo real: login OAuth por
código de dispositivo (`codex login --device-auth`) contra a conta pessoal do ChatGPT, com o token
de sessão persistido em `auth.json` dentro do volume Docker `codex_auth:/codex-auth`
(`compose.yaml:118-129,156`; `advisor/server.mjs:16,56-63`). `README.md:291` confirma: "seu uso
segue os limites da assinatura autenticada do ChatGPT" — é uma cota de plano de consumidor, não uma
cota de API provisionada.

Pesquisa externa (OpenAI Help Center e documentação de terceiros sobre o Codex CLI, ver fontes em
§13) indica que a sessão OAuth se renova automaticamente durante uso ativo, e só exige novo login
interativo após ~8 dias **sem nenhum refresh** (idle). Como o WA-00 propõe justamente um serviço
ativo continuamente (webhooks chegando o dia todo), esse risco de expiração por inatividade tende a
ser baixo em operação normal — mas ressurge integralmente após qualquer parada prolongada do
serviço (deploy, incidente, manutenção), e existem relatos de falha de refresh em cenários de
múltiplos processos/race conditions (fontes em §13). **Não existe hoje nenhum caminho de
reautenticação automatizada**: se `auth.json` expirar, `/health` passa a reportar
`authenticated:false` (`advisor/server.mjs:216-224`) e o sistema degrada silenciosamente ao modo
local — correto pela invariante de que o Codex é opcional, mas hoje **sem alerta operacional para o
humano corrigir**, o que é aceitável para um recurso "bônus" da UI web e não seria aceitável para um
canal que os usuários passam a esperar que responda.

### 1.3 Concorrência, timeout, retry

Não há fila, semáforo ou limite de concorrência em `advisor/server.mjs`: cada requisição HTTP
concorrente gera seu próprio subprocesso `codex exec` independente, sem limite algum além do que a
própria conta ChatGPT impuser do lado do servidor da OpenAI. Timeout é por chamada
(`CODEX_TIMEOUT_MS`, padrão 70000ms, `SIGKILL` ao expirar, `advisor/server.mjs:137-140`). Não há
retry em nenhuma camada — nem no sidecar, nem no cliente Python (`app/services/codex_client.py:78-85`
captura qualquer erro e devolve `CodexResult(None, mensagem)` sem re-tentar). Falha sempre fecha
(`fail-closed`): nunca produz `pass`/aprovação por ausência de resposta (`app/services/
codex_audit.py:173-182`, invariante já documentada em `docs/INTELLIGENCE.md:26`).

### 1.4 Fronteira de autoridade já existente (channel-agnostic)

O achado mais importante para a arquitetura do WA-00: o Slice 4 (October Go-Live) já implementou um
contrato de **ações tipadas desacoplado de canal**:

- `POST /assistant/interpret` (`app/api.py:11801-11838`) chama `/v1/interpret` (Codex só devolve
  `intent` + campos livres de texto — nunca um id, nunca `confirmed`;
  `advisor/interpret-schema.json`, `$comment`: "there is no field here that could authorize a
  mutation"), depois `assistant_actions.build_typed_action_proposal` resolve os campos contra dados
  reais do household via SQL determinístico e devolve uma proposta (`can_execute` + payload) ou uma
  pergunta de desambiguação.
- Se `can_execute=True`, persiste `AssistantActionProposal` (`app/models.py:610-660`), single-use,
  `expires_at`, `household_id`-scoped.
- `POST /assistant/execute` (`app/api.py:11845-11868`) aceita **somente** `proposal_id` — o typed
  action e o payload são sempre relidos da proposta persistida, nunca reenviados pelo cliente
  (blindagem citada explicitamente no código como correção de "engineering review PR #92, blocker
  1"). Execução despacha para as mesmas funções `_impl` que o formulário manual usa, dentro de uma
  única transação com o `AuditEvent`/`AssistantActionEvent` (INV-032).
- Concorrência: `SELECT ... FOR UPDATE` na proposta serializa execuções concorrentes do mesmo
  `proposal_id` (`assistant_actions.py:1269`, dialeto PostgreSQL); replay idempotente devolve o
  resultado já gravado (`idempotent_replay: true`).

**Vocabulário de typed actions fechado hoje:** `create_expense`, `create_income`,
`create_internal_transfer`, `pay_obligation`, `pay_card_invoice`, `register_refund`,
`update_asset_value`, `register_asset_contribution` (`app/services/assistant_actions.py:96-114`).

**Gap confirmado:** a intent `query` já existe no schema do Codex (`advisor/interpret-schema.json`)
mas **não tem implementação** — `assistant_actions.py:629-632` devolve sempre uma pergunta de
desambiguação para `query`/`unknown`/`None`. Não existe hoje nenhuma tool de consulta, agregação,
comparação ou projeção que o Assistente possa executar. Isso é trabalho novo genuíno do WA-00/WA-01
(§6), não uma reutilização de algo que já funciona.

---

## 2. Autenticação/chamada atual — validação de suitability para runtime contínuo

| Critério (issue #72) | Evidência | Veredito |
|---|---|---|
| Disponibilidade | Sem SLA — cota de plano de consumidor ChatGPT, não de API provisionada (`README.md:291`) | **Risco aceito com mitigação** — ver §3.3 |
| Concorrência | Nenhum limite hoje; N requisições = N subprocessos `codex exec` simultâneos (`advisor/server.mjs`) | **Gap a fechar antes do WA-01** (§3.3, mitigação a) |
| Timeout | Implementado, configurável, `SIGKILL` ao expirar | **OK, reutilizável sem mudança** |
| Recuperação de falha | Fail-closed, sem retry, degrada para motor local (correto pela invariante) | **OK, mas falta alerta operacional** (§3.3, mitigação b) |
| Compatibilidade com tool calling | Não usa tool calling nativo; usa geração de JSON restrito por schema (padrão já usado com sucesso em 4 rotas) | **OK — WA-00 não depende de tool calling nativo do modelo** (a resolução determinística já faz esse papel, §1.4) |
| Autenticação estável para servidor 24/7 | Login humano por device-code, sessão de assinatura pessoal, sem chave de API (`scripts/setup-codex.sh`) | **Frágil, mas mitigável** — ver §3.3 |

---

## 3. Decisão de runtime de LLM (ADR-1)

### 3.1 Ordem obrigatória (issue #72, não revista aqui)
1. Reutilizar o Codex atual, sem custo adicional.
2. Se inviável/insuficiente, avaliar modelo local.
3. Só em terceiro nível, API paga — nunca ativada automaticamente; exige Technical Challenge e
   aprovação explícita do engenheiro responsável antes de qualquer implementação.

### 3.2 Avaliação da Opção 1 (Codex atual)

A favor: sem custo adicional; já produz JSON estruturado e seguro (sem campo de autoridade); já
resolve exatamente o mesmo problema (interpretar linguagem natural em `intent` + campos) que o
WhatsApp Assistant precisa; a fronteira de confiança (§1.4) já é agnóstica de canal.

Contra: autenticação por login humano de assinatura pessoal, sem chave de API e sem SLA de
provisionamento; nenhum limite de concorrência hoje; incerteza não resolvida sobre política de uso
aceitável da OpenAI para "múltiplos usuários por uma assinatura" (§13).

**Conclusão da Opção 1:** viável para iniciar o WA-01 na escala real deste projeto — uma única
família, poucos usuários simultâneos, não um SaaS multi-tenant público — desde que as três
mitigações abaixo sejam tratadas como pré-requisito de implementação do WA-01 (não deste discovery)
e desde que o item de ToS em §13 seja verificado e aceito explicitamente pelo engenheiro
responsável/proprietário antes de qualquer uso em produção.

### 3.3 Mitigações obrigatórias antes do WA-01 (não implementadas neste PR)

a) **Limite de concorrência**: adicionar fila/semáforo no gateway ou no sidecar, limitando quantas
   invocações `codex exec` simultâneas o serviço pode disparar, para não sobrecarregar a mesma
   sessão/conta e os recursos do host.

b) **Alerta operacional de autenticação**: expor `authenticated=false` de `/health` a um canal de
   alerta (o mesmo padrão de e-mail operacional que já existe para outros workers —
   `docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`/notification-worker — é reutilizável), para que a
   reautenticação humana (`codex login --device-auth`) aconteça em minutos, não silenciosamente por
   dias.

c) **Fallback determinístico sem Codex**: o WhatsApp Assistant deve continuar operando (consulta e
   ações tipadas simples) mesmo com `authenticated=false`, exatamente como o Assistente web já faz
   hoje — isso não é uma mitigação nova, é a mesma invariante (`docs/INTELLIGENCE.md:26`) aplicada
   ao novo canal.

Se, na prática do WA-01, a Opção 1 se mostrar insuficiente sob essas mitigações (por exemplo,
throughput real da conta ChatGPT for excedido de forma recorrente), a escalada segue para a Opção 2
(modelo local) antes de qualquer avaliação de API paga, por decisão já registrada no issue #72 — não
cabe a este discovery antecipar essa escolha sem evidência de uso real.

---

## 4. ADR-2 — Arquitetura WhatsApp → Gateway → Orchestrator → Tool Layer → Finance Engine/DB

### 4.1 Diagrama lógico

```
Meta WhatsApp Cloud API
        │  HTTPS POST + X-Hub-Signature-256
        ▼
┌───────────────────────────┐
│ Gateway (novo, isolado)    │  verifica assinatura Meta; rate limit; sem sessão de UI;
│ /webhooks/whatsapp         │  resolve telefone -> household/usuário (WA-01, tabela nova)
└──────────────┬────────────┘
               │ chamada interna (mesma rede privada do app, nunca exposta à internet)
               ▼
┌───────────────────────────┐
│ Orchestrator               │  reusa app.services.assistant_interpreter (POST /v1/interpret)
│ (dentro do `app` FastAPI)  │  e app.services.assistant_actions (proposal/execute)
└──────────────┬────────────┘
               │
     ┌─────────┴──────────────┐
     ▼                        ▼
┌───────────────┐   ┌────────────────────────────┐
│ Tool Layer      │   │ Typed Actions existentes    │
│ (novo, §6)      │   │ (Slice 4, inalterado)       │
│ query/aggregate/│   │ create_expense, pay_...,    │
│ compare/project │   │ register_refund, etc.       │
└───────┬─────────┘   └───────────────┬─────────────┘
        │                             │
        ▼                             ▼
┌─────────────────────────────────────────────┐
│ Finance Engine / DB (inalterado)             │
│ financial_snapshots, projection_engine,      │
│ report_export, assistant_actions._impl       │
└───────────────────────────────────────────────┘
```

### 4.2 Por que o Gateway é um componente isolado, não uma rota a mais no `app` privado

O `app` de hoje é servido para a rede interna/Tailscale da família (`docs/ARCHITECTURE.md`,
histórico de PRs #10/#11/#55 sobre acesso Windows/Tailscale/proxy HTTPS). Um webhook da Meta precisa
de um endpoint HTTPS **publicamente roteável** para que a Meta consiga entregá-lo — isso é uma
mudança de modelo de exposição, não uma rota nova em um sistema já público. Recomendação: o Gateway
deve ser o único componente com uma rota pública (`/webhooks/whatsapp`), validando a assinatura
`X-Hub-Signature-256` (HMAC-SHA256 sobre o corpo bruto, comparação `timing-safe` — mesmo padrão já
usado em `advisor/server.mjs:50-54`) **antes de tocar em qualquer lógica de negócio**, e chamando o
Orchestrator apenas pela rede interna. Isso preserva o restante do sistema (UI, DB, Advisor) fora do
raio de exposição pública — decisão de infraestrutura a confirmar no ADR de implementação do WA-01,
não deste discovery, mas registrada aqui porque muda a superfície de ameaça do projeto inteiro.

### 4.3 Autoridade de execução por número de telefone — decisão de produto em aberto

Hoje, `/assistant/interpret` e `/assistant/execute` exigem `_require_admin` (`app/api.py:1046-1048`).
O WA-00 precisa que o engenheiro responsável decida: um número de WhatsApp vinculado a um membro não
administrador do household deve poder (i) apenas consultar, (ii) propor ações tipadas mas exigir
confirmação por um admin, ou (iii) ter paridade total com a sessão web administrativa? Este discovery
não decide isso — ver Technical Challenge/pergunta em §13.

---

## 5. Perguntas livres/dinâmicas — sem catálogo fechado de comandos

A extração de intenção (`/v1/interpret`) já opera sobre texto livre com um enum fechado de
**intenções**, não de **frases** (`advisor/interpret-schema.json`) — isso satisfaz o requisito do
issue #72 de proibir catálogo fechado de frases/comandos: qualquer forma de escrever "gastei 50 no
mercado" cai na mesma intent `create_expense`. Para perguntas de consulta (`query`, hoje sem
implementação — §1.4), a mesma abordagem se aplica: o Tool Layer (§6) deve aceitar a pergunta como
texto livre e o Codex decide qual tool determinística chamar e com quais parâmetros extraídos (nunca
qual SQL rodar).

---

## 6. Contratos de tools genéricas (novo, escopo WA-01, especificado aqui)

Todas as tools abaixo são **somente leitura de fatos já calculados pelo Finance Engine** (nenhuma
soma nova, nenhuma tool escreve dados) ou wrappers finos sobre os typed actions já existentes (que já
escrevem sob todas as invariantes vigentes). Nenhuma tool aceita SQL, nome de tabela ou coluna como
parâmetro.

| Tool | Parâmetros (extraídos do texto pelo Codex, resolvidos deterministicamente pelo backend) | Fonte de dado reutilizada | Observação |
|---|---|---|---|
| `query_facts` | `household_id` (sempre do contexto de sessão, nunca do LLM), `topic` (saldo/gasto do mês/fatura/obrigações — enum fechado) | `app/services/financial_snapshots.build_snapshot` | Substitui o gap de `query` (§1.4) |
| `aggregate_spending` | `household_id`, `dimension` (categoria/conta/cartão), `period` (mês/intervalo) | `app/services/report_export.py` (mesma fonte do Dashboard/Relatórios — nunca um cálculo paralelo, Rebaseline §47) | |
| `compare_periods` | `household_id`, `period_a`, `period_b`, `dimension` | idem | |
| `project_horizon` | `household_id`, `horizon_days` (30/60/90) | `app/services/projection_engine.py` | REALIZADO/COMPROMETIDO/PREVISTO nunca somados (INV-005/006/007) |
| `draft_typed_action` | texto livre da mensagem | `assistant_interpreter.interpret_message` + `assistant_actions.build_typed_action_proposal` (inalterados) | Nunca executa; devolve proposta ou pergunta de desambiguação, como hoje |
| `confirm_typed_action` | `proposal_id` | `assistant_actions.execute_typed_action` (inalterado) | Requer confirmação explícita do usuário no WhatsApp (ex.: responder "sim") antes de chamar |
| `undo_typed_action` | `action_id` | endpoint de undo existente (`POST /assistant/actions/{action_id}/undo`) | INV-033 — nunca apaga o rastro |

Cada tool devolve **fatos e números já calculados**, nunca uma explicação gerada livremente por IA
como se fosse cálculo — o Codex só recebe o resultado já pronto para fraseá-lo em português natural,
no mesmo padrão de `/v1/analyze` (INV-021: "Advisor sem cálculo oficial independente").

---

## 7. Regras financeiras canônicas que as tools devem respeitar (mapa, não redefinição)

Nenhuma regra abaixo é alterada por este discovery; são as invariantes que qualquer tool de escrita
do WA-00 herda automaticamente por reutilizar os typed actions/`_impl` existentes:

- Privilège como caixa remunerado, sem resgate/aplicação sintética (INV-003/INV-004/INV-023).
- Saldo confirmado soberano, divergência nunca mascarada (INV-024).
- Pagamento de fatura é reconciliação, nunca despesa nova (INV-002/INV-025); principal carregado
  nunca duplicado (INV-026).
- Estorno só neutraliza quando explicitamente vinculado (INV-027).
- REALIZADO/COMPROMETIDO/PREVISTO nunca somados como uma coisa só (INV-005/006/007).
- Comissão nunca prevista automaticamente; conciliação de renda recorrente nunca soma real+previsto
  (INV-029/030/031).

Como todas as tools de escrita (§6) são wrappers sobre `assistant_actions`/`_impl` já existentes, não
há necessidade de reimplementar nenhuma dessas regras — o risco a vigiar é apenas garantir que o
Gateway/Orchestrator nunca contorne esse caminho (ex.: nunca inserir uma `Transaction` diretamente a
partir do canal WhatsApp).

---

## 8. Política de PII, logs, retenção, idempotência, household isolation

- **PII mínima ao LLM:** manter o padrão já vigente — o Codex nunca recebe telefone, nome completo,
  documento ou dado bruto de extrato; recebe apenas o texto da mensagem, histórico curto e fatos
  agregados sanitizados (mesmo padrão de `assistant_sanitizer.py`/`audit_sanitizer.py`).
- **Retenção do texto original da mensagem do WhatsApp:** deve ser preservado no
  `AssistantActionEvent`/`AuditEvent` (INV-032 já exige isso para o canal web); política de retenção
  de logs específica de WhatsApp (quanto tempo guardar o payload bruto do webhook da Meta) fica como
  pergunta aberta em §13 — depende de decisão de produto sobre janela de suporte/auditoria, não de
  limitação técnica.
- **Idempotência do webhook:** a Meta pode reentregar o mesmo evento; o Gateway deve deduplicar por
  `message_id` do WhatsApp antes de chamar o Orchestrator (novo, WA-01) — mesmo princípio de
  idempotência documental já usado em importação de extrato, não duplicidade semântica (§38 do
  mandato geral).
- **Household isolation:** a resolução telefone → `household_id` deve ser feita uma única vez no
  Gateway/Orchestrator e propagada — nunca aceitar `household_id` vindo do payload da Meta ou do
  texto da mensagem.
- **Segredos:** o token do webhook da Meta (verify token) e o app secret usado para validar
  `X-Hub-Signature-256` seguem o mesmo padrão de `.env`/`ADVISOR_SHARED_SECRET` já usado — nunca no
  Git, nunca logado.

---

## 9. Estratégia de webhook, autenticação Meta, Docker/OCI (sem implementar/deployar)

- **Verificação do webhook:** GET de handshake inicial (`hub.verify_token`) e, em cada POST, validar
  `X-Hub-Signature-256: sha256=<hex>` como HMAC-SHA256 do corpo bruto (antes de qualquer parsing
  JSON) usando comparação de tempo constante — confirmado como o mecanismo oficial da Meta por
  múltiplas fontes técnicas (§13); não foi possível confirmar o texto exato na documentação oficial
  da Meta neste ambiente (egress bloqueado, §13).
- **Docker:** o Gateway deve seguir o mesmo padrão de hardening já usado por `notification-worker`/
  `cvm-worker` em `compose.yaml` (`read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`,
  `tmpfs` para `/tmp`), como um novo serviço/perfil, não implementado neste PR.
- **OCI:** explicitamente congelado (#59-65) e fora de escopo — nenhuma implementação/deploy Oracle
  aqui, apenas o registro de que o Gateway precisará, no ciclo de OCI, de uma rota pública estável
  (IP/DNS) e certificado TLS válido, requisito que a issue #72 já antecipa sem autorizar a execução.

---

## 10. Custos e limites externos — WhatsApp Business Platform (pesquisa externa, com data)

**Aviso de método:** os domínios oficiais `developers.facebook.com` e `business.whatsapp.com` (e
`openai.com`) estão bloqueados pelo proxy de rede deste ambiente de execução (`EGRESS_BLOCKED`) — não
foi possível citar a documentação primária diretamente. As informações abaixo vêm de múltiplas fontes
secundárias técnicas independentes, coletadas em 2026-09-17, e **devem ser confirmadas contra a
documentação oficial da Meta pelo engenheiro responsável antes de qualquer decisão de custo**.

- **Modelo de cobrança:** desde 1º de julho de 2025, a Meta substituiu a cobrança por conversa
  (janela de 24h) por cobrança por mensagem de template enviada; respostas dentro da janela de
  atendimento de 24h continuam sem custo. A partir de 1º de outubro de 2026, a Meta passa a cobrar
  também por mensagem de serviço/utilidade dentro da janela de 24h — mudança relevante para o
  timing deste projeto (impacto direto se o Assistente responder ativamente, não seja apenas
  reativo).
- **Faixa de preço:** exemplo citado de US$ 0,025 por mensagem de marketing nos EUA; preço varia até
  13× por país e 6× por categoria de mensagem — nenhum número deve ser tratado como definitivo sem
  confirmação oficial.
- **Limites de throughput:** 80 mensagens/segundo como padrão, upgrade automático até 1000 msg/s para
  contas com qualidade estável; contas novas sem verificação de negócio começam limitadas a 250
  mensagens/24h, subindo por tiers (2.000 → 10.000 → 100.000 → ilimitado) após verificação e
  histórico de qualidade.
- **Assinatura de webhook:** `X-Hub-Signature-256` (HMAC-SHA256 do corpo bruto) confirmado por
  múltiplas fontes técnicas independentes como o mecanismo de verificação — consistente com o padrão
  que este projeto já usa internamente (`timingSafeEqual`).

**Custo recorrente não aprovado bloqueia ativação, não a documentação** (issue #72) — nenhuma decisão
de custo é tomada aqui; qualquer ativação de número/tier pago exige Technical Challenge e aprovação
explícita antes do WA-01.

---

## 11. Matriz de riscos

| # | Risco | Impacto | Mitigação proposta | Fase |
|---|---|---|---|---|
| R1 | Gateway público é a primeira superfície de internet do sistema | Alto — amplia superfície de ataque de um sistema hoje privado | Isolar Gateway como componente próprio, sem acesso direto a DB/documentos, validação de assinatura antes de qualquer lógica (§4.2) | WA-01 |
| R2 | Auth do Codex depende de login humano por assinatura pessoal, sem chave de API | Médio — degradação silenciosa da qualidade do Assistente | Alerta operacional em `authenticated=false` + fallback determinístico já existente (§3.3) | WA-01 |
| R3 | Sem limite de concorrência hoje no sidecar Codex | Médio — sobrecarga da conta/host sob uso simultâneo de vários membros | Fila/semáforo no Gateway/Orchestrator (§3.3) | WA-01 |
| R4 | Incerteza sobre Acceptable Use Policy da OpenAI para "múltiplos usuários por uma assinatura" | Alto — risco contratual/de bloqueio de conta | Verificação humana da ToS oficial antes de produção (§13) | Bloqueio antes do WA-01 produção |
| R5 | Autoridade de execução por número de telefone não decidida (membro não-admin) | Médio — risco de execução de ação financeira sem autorização adequada | Decisão de produto explícita do engenheiro responsável (§4.3, §13) | Antes do WA-01 |
| R6 | Custos da WhatsApp Business Platform não confirmados na fonte oficial | Baixo/Médio — decisão de ativação sem número exato | Confirmação manual da documentação oficial da Meta antes de qualquer ativação | Antes do WA-01 |
| R7 | Mudança de cobrança em 1º de outubro de 2026 (mensagens de serviço passam a ser cobradas) | Médio — pode inviabilizar respostas proativas sem custo | Desenhar o Assistente para operar dentro da janela de atendimento de 24h sempre que possível; revisitar antes de outubro/2026 | WA-01/operacional |

---

## 12. Plano de rollback

Como nenhum código de produção, migration, webhook real ou credencial foi criado neste PR, o
rollback deste discovery é trivial: reverter/fechar o PR sem qualquer impacto em dado real ou em
produção. Para o WA-01 (fora de escopo aqui, registrado para o próximo Work Order): o Gateway deve
ser desligável independentemente do resto do sistema (perfil Docker Compose próprio, como
`advisor`/`notification-worker` já são), sem nenhuma dependência que impeça o `app` de continuar
operando normalmente caso o Gateway seja removido.

---

## 13. TECHNICAL CHALLENGE / questões abertas para o engenheiro responsável

**Orientação questionada:** nenhuma orientação do Work Order é contestada — a ordem de prioridade de
runtime e as proibições foram seguidas integralmente. O que segue são lacunas de evidência genuínas,
não desacordo técnico.

1. **HUMAN BLOCKER — verificação de Acceptable Use Policy da OpenAI.** Não foi possível acessar
   `openai.com` neste ambiente (egress bloqueado pela política de rede da sessão) para confirmar se
   o uso do Codex CLI autenticado por uma assinatura pessoal do ChatGPT, servindo múltiplos membros
   de uma família através de um canal automatizado (WhatsApp), é compatível com os termos de uso da
   OpenAI. Fontes de terceiros (não oficiais) sugerem cautela para "aplicações servindo múltiplos
   usuários com uma assinatura". **Ação necessária:** o proprietário/engenheiro responsável deve ler
   a Acceptable Use Policy oficial da OpenAI (`openai.com/policies`) e confirmar explicitamente que
   esse uso é aceitável antes de qualquer implementação do WA-01 que dependa do Codex em produção.
   **Como validar:** leitura direta da política oficial; se ambígua, considerar abrir um ticket de
   suporte à OpenAI perguntando diretamente.
2. **Autoridade de execução por número de telefone (§4.3):** um membro não-administrador do
   household deve poder executar ações tipadas (gastar, pagar) via WhatsApp com a mesma autoridade
   de uma sessão admin, ou apenas consultar/propor com confirmação de um admin? Isso é uma decisão de
   produto, não técnica — preciso da decisão do engenheiro responsável antes do WA-01.
3. **Retenção do payload bruto do webhook da Meta:** por quanto tempo guardar o corpo original da
   mensagem recebida (para auditoria/depuração) versus quando descartá-lo? Não há requisito
   normativo hoje que responda isso.
4. **Confirmação de custos/limites oficiais (§10):** os números citados vêm de fontes secundárias
   (2026-09-17) porque os domínios oficiais da Meta estavam bloqueados neste ambiente. Pedir para
   alguém com acesso confirmar contra `developers.facebook.com/docs/whatsapp/pricing` antes de
   qualquer decisão de ativação de número.

Nenhum desses itens bloqueia a aceitação deste discovery/ADR como documento de arquitetura — eles
bloqueiam apenas o início da implementação do WA-01 que dependa da resposta.

---

## 14. Critérios de aceite — mapeamento requisito → evidência

| Critério (Work Order) | Evidência neste documento |
|---|---|
| ADR completo e coerente com arquitetura/invariantes atuais | §4, §7 |
| Inventário do runtime Advisor/Codex baseado em código atual, evidência objetiva de viabilidade | §1, §2 |
| Tool contracts genéricos cobrem consultas dinâmicas e drafts sem SQL do LLM | §5, §6 |
| Política de segurança/PII/retenção/idempotência/household isolation documentada | §8, §9 |
| Custos e limites externos documentados com data/fonte, Technical Challenge quando aplicável | §10, §13 |
| Riscos, rollback e decisões para WA-01..WA-07 explicitados | §11, §12 |
| Nenhuma alteração de semântica financeira ou dado real | Confirmado — nenhum arquivo de `app/`, `alembic/`, `advisor/*.mjs` foi alterado neste PR, apenas este documento |
| CI documental/lint aplicável verde | A confirmar após push (gates de markdown/lint do repositório) |

---

## 15. Explicitamente fora de escopo deste discovery

- Implementação de WA-01 em diante (gateway real, webhook real, tabela telefone→household).
- Qualquer credencial, número de telefone real ou dado real.
- Ativação de API paga de LLM.
- Implementação ou deploy de Oracle/OCI (#59-65, permanece congelado).
- Qualquer alteração em `app/`, `alembic/`, `advisor/*.mjs` ou dado financeiro.

**Claude implementou/documentou este discovery e pode contestar o Work Order com evidência
concreta; nenhuma contestação foi necessária. Claude não fez merge.**
