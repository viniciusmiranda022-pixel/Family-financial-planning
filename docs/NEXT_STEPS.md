# Próximos passos

## Próximo trabalho funcional registrado — alertas de vencimento por e-mail

Depois das iniciativas/slices normativos que já estiverem em execução ou anteriormente enfileirados, a próxima evolução funcional registrada para o Family Finance é o envio automático de alertas de contas/obrigações por e-mail:

- **D-1:** um dia antes do vencimento;
- **D0:** no próprio dia do vencimento;
- um ou vários destinatários configuráveis;
- preferências D-1/D0 por destinatário;
- conta paga ou desativada não gera novo alerta;
- envio backend, independente de navegador aberto;
- remetente Gmail configurado de forma segura fora da UI e fora do banco;
- custo adicional alvo: **R$ 0,00**.

Fonte de verdade detalhada: [`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`](./WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md).

Epic: #66.

Sequência prevista da EPIC:

1. #67 — MAIL-00: modelo, configurações e destinatários;
2. #68 — MAIL-01: adapter SMTP Gmail, templates e e-mail de teste;
3. #69 — MAIL-02: outbox, scheduler D-1/D0, idempotência e retry;
4. #70 — MAIL-03: integração operacional, Docker/OCI e validação E2E.

Este registro **não autoriza execução paralela** nem permite pular PRs/slices anteriores. Quando MAIL-00 entrar na fila ativa, Claude deve reler a `main` vigente, criar o Work Order específico do slice, branch isolada e Draft PR. Claude não faz merge.

## Evolução funcional registrada — Family Finance AI Assistant via WhatsApp

Depois dos trabalhos funcionais e normativos que já estiverem enfileirados, fica registrada a evolução do WhatsApp como **interface conversacional oficial do Family Finance**, capaz de lançar, consultar e analisar dados financeiros por linguagem natural livre.

O requisito central é que **as perguntas não sejam estáticas**. Claude deve implementar um orquestrador LLM `tool-driven`, em que o modelo interpreta qualquer formulação suportável e compõe tools genéricas do backend. É proibido criar catálogo fechado de perguntas, `if/else` por frase, regex como motor principal, SQL gerado pelo modelo ou um segundo motor financeiro.

Princípio normativo:

`LLM entende e orquestra -> Family Finance calcula, valida e persiste`.

Capacidades alvo:

- **WRITE:** `gastei 30 de combustível`, `recebi 2500 de comissão`, parcelamentos, transferências e demais lançamentos; toda escrita passa por draft + prévia + confirmação humana;
- **QUERY:** perguntas livres como `quanto gastei de combustível esse mês?`, `quanto gastei no mercado nos últimos 3 meses?`, `qual foi minha renda no último ano?` e combinações não previstas previamente;
- **CONTEXT:** continuidade como `e mês passado?`, `qual a diferença?`, `e se continuar nessa média?`;
- **REASON/ADVISOR:** diagnósticos, comparações e cenários usando números produzidos pelo Finance Engine, nunca calculados livremente pelo LLM;
- **MEDIA:** áudio, comprovantes e documentos reutilizando o pipeline de OCR/transcrição já existente.

Fonte de verdade detalhada: [`docs/WHATSAPP_AI_ASSISTANT_PLAN.md`](./WHATSAPP_AI_ASSISTANT_PLAN.md).

Epic: #71.

Sequência prevista da EPIC:

1. #72 — WA-00: discovery, ADR, custos e contratos;
2. #73 — WA-01: gateway WhatsApp, webhook e autorização de números;
3. #74 — WA-02: orquestrador LLM tool-driven e camada semântica genérica;
4. #75 — WA-03: WRITE com drafts, confirmação e idempotência;
5. #76 — WA-04: QUERY/analytics genérico para perguntas livres;
6. #77 — WA-05: contexto conversacional e follow-ups;
7. #78 — WA-06: Advisor, comparações e projeções;
8. #79 — WA-07: mídia, hardening, observabilidade e validação E2E.

A integração deve preservar todas as regras financeiras canônicas: resgate do Privilège DI não é renda; aplicação/transferência não é despesa/receita operacional; pagamento de fatura não é nova despesa; parcelamentos respeitam competência e compromissos futuros; o LLM não tem acesso SQL livre nem autoridade para gravar sem confirmação.

O objetivo de custo recorrente continua sendo **R$ 0,00**. WA-00 deve validar custos e limites vigentes de WhatsApp Business/Cloud API, runtime LLM e qualquer infraestrutura adicional. Custo recorrente não aprovado, trial credits ou dependência paga exigem **Technical Challenge** antes de prosseguir.

Este registro é roadmap/backlog e **não autoriza iniciar WA-00 imediatamente**. A EPIC #71 só entra em execução quando chegar sua vez na fila normativa.

## Prioridade de infraestrutura — Oracle Cloud Always Free

A próxima iniciativa de infraestrutura registrada para o Family Finance é a migração do host local Windows/WSL2 para Oracle Cloud Infrastructure (OCI) **sem custo recorrente**, mantendo o requisito de **R$ 0,00/mês**.

Fonte de verdade detalhada: [`docs/ORACLE_ALWAYS_FREE_MIGRATION_PLAN.md`](./ORACLE_ALWAYS_FREE_MIGRATION_PLAN.md).

Epic: #59.

Sequência obrigatória:

1. #60 — ORA-00: discovery ARM64, sizing e ADR de hosting;
2. #61 — ORA-01: provisionamento OCI Always Free e guardrails de custo;
3. #62 — ORA-02: Docker/Compose ARM64 e perfil de produção cloud;
4. #63 — ORA-03: backup externo criptografado, restore e reconstrução;
5. #64 — ORA-04: migração dos dados reais e cutover controlado;
6. #65 — ORA-05: observabilidade, operação e validação final de custo zero.

## Regra de execução

Este arquivo registra prioridade e sequência, mas não autoriza execução paralela. Antes de iniciar qualquer nova EPIC/slice, devem ser respeitados os slices/PRs normativos anteriores ainda pendentes no projeto e a ordem ativa definida pelo engenheiro responsável.

Para cada item, o fluxo continua sendo:

`documentação normativa -> Work Order -> branch isolada -> implementação pelo Claude -> Draft PR -> revisão -> CI/gates -> merge pelo engenheiro -> próximo slice`.

Claude não faz merge.

## Gate financeiro

Se qualquer recurso OCI indicar custo maior que zero, não for elegível ao Always Free vigente, depender de trial credits ou exigir upgrade para Pay As You Go, a execução deve parar e abrir Technical Challenge. Não substituir automaticamente por recurso pago.

O mesmo princípio vale para alertas por e-mail: não adicionar serviço pago de e-mail, observabilidade ou mensageria sem decisão explícita do engenheiro responsável.

Para o AI Assistant via WhatsApp, não habilitar API/canal/runtime pago ou cobrança recorrente sem decisão explícita do engenheiro responsável. WA-00 deve verificar preços e franquias vigentes antes de qualquer ativação de produção.
