# Próximos passos

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

Este arquivo registra prioridade e sequência, mas não autoriza execução paralela. Antes de iniciar ORA-00, devem ser respeitados os slices/PRs normativos anteriores ainda pendentes no projeto.

Para cada item, o fluxo continua sendo:

`documentação normativa -> Work Order -> branch isolada -> implementação pelo Claude -> Draft PR -> revisão -> CI/gates -> merge pelo engenheiro -> próximo slice`.

Claude não faz merge.

## Gate financeiro

Se qualquer recurso OCI indicar custo maior que zero, não for elegível ao Always Free vigente, depender de trial credits ou exigir upgrade para Pay As You Go, a execução deve parar e abrir Technical Challenge. Não substituir automaticamente por recurso pago.
