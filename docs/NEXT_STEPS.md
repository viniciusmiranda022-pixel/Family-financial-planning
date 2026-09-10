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
