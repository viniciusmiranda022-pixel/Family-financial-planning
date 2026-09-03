# Runbook — PR 8: rollout e rollback do backfill e dos safety gates de CI

Este runbook cobre a operação da fatia final do Financial Integrity Engine
(`docs/WORK_ORDER_PR8_FINANCIAL_SAFETY_CI_BACKFILL_FINAL_DOCS.md`): a migration
`0009` (`backfill_runs`), o comando `python -m app.cli.backfill` e os gates de
CI descritos em `docs/ARCHITECTURE.md`.

## 1. Pré-condições

Antes de rodar o backfill sobre uma base com dados reais:

1. **Backup recente e testado.** Rode `scripts/backup.sh` (ou confirme que o
   backup diário automático está saudável) e valide que uma restauração
   funciona (`scripts/restore.sh` em um banco descartável). O backfill nunca
   escreve em `Transaction`/`Document`/`PayrollRecord`/`Commission`/
   `Obligation` (ver `docs/FINANCIAL_RULES.md`), mas ainda assim cria linhas em
   `document_reconciliations`, `duplicate_groups`/`duplicate_group_members`,
   `financial_snapshots`, `financial_snapshot_lineage`, `integrity_runs`,
   `integrity_findings` e `backfill_runs` -- o backup cobre a base inteira.
2. **Migration `0009` aplicada.** `alembic upgrade head` deve terminar em
   `0009` antes do backfill (ele depende da tabela `backfill_runs`). Confirme:
   ```bash
   docker compose exec app alembic current
   ```
3. **CI verde no commit exato do deploy.** Os doze gates de
   `.github/workflows/ci.yml` (`lint`, `unit`, `financial-invariants`,
   `property-tests`, `parser-reconciliation`, `projection-parity`,
   `snapshot-channel-consistency`, `advisor-contract-security`,
   `frontend-syntax`, `docker-build`, `alembic-migration`,
   `integration-postgres`) devem estar verdes -- necessário, não suficiente
   (ver `docs/ARCHITECTURE.md`).
4. **Nenhum processo concorrente de import/API em produção durante a janela
   de execução real** (não-`--dry-run`) sobre a mesma família, se possível.
   O backfill é seguro sob concorrência normal (ver "Concorrência" abaixo),
   mas uma janela de manutenção reduz o risco de um relatório antes/depois
   confuso por mudanças simultâneas de outra origem.

Não há feature flag dedicada para o backfill: é um comando de operador
(`app.cli.backfill`), não uma rota exposta pela aplicação, então não há
como habilitá-lo/desabilitá-lo em produção além de executá-lo ou não.

## 2. Migration

```bash
docker compose exec app alembic upgrade head
```

- Aditiva: cria somente a tabela `backfill_runs` (nenhuma coluna existente é
  alterada, nenhuma linha existente é lida ou reescrita). Ver
  `alembic/versions/0009_backfill_runs.py`.
- Reversível: `alembic downgrade 0008` remove apenas `backfill_runs` (um
  manifesto de auditoria do próprio backfill, sem nenhuma chave estrangeira
  apontando para ela) -- nunca remove um fato financeiro.
- Testada em CI contra PostgreSQL real (`alembic-migration`): banco vazio até
  `head`, baseline legada `0002` até `head` preservando linhas existentes, e
  downgrade/upgrade de ida e volta.

## 3. Backfill

### 3.1 Dry-run primeiro, sempre

```bash
docker compose exec app python -m app.cli.backfill --dry-run
```

Executa o processamento completo (reconciliação, duplicidade, snapshots,
integrity runs) e imprime o relatório antes/depois, depois reverte a
transação -- nada é persistido além do próprio manifesto `BackfillRun` da
execução em dry-run (gravado à parte, para preservar a evidência de que o
dry-run aconteceu e o que ele reportou).

Revise a saída: quantos documentos ficariam `unknown`, quantas transações
seriam examinadas para duplicidade (evidência derivada em
`duplicate_groups`/`duplicate_group_members` -- o backfill nunca aplica essa
classificação à própria `Transaction`, ver `docs/FINANCIAL_RULES.md` seção
"Backfill"), quantos findings abertos restariam por família. Divergência
inesperada (por exemplo, um número de duplicidades muito maior do que o
esperado) é motivo para investigar antes de rodar de verdade, não para
prosseguir.

### 3.2 Execução real

```bash
# Uma família por vez, na primeira rodada em produção:
docker compose exec app python -m app.cli.backfill --household "Nome exato"

# Todas as famílias, depois de validar a primeira:
docker compose exec app python -m app.cli.backfill

# Intervalo de competência explícito (em vez do intervalo inferido das
# transações existentes):
docker compose exec app python -m app.cli.backfill --from 2026-01 --to 2026-06
```

Acompanhe a saída (contagens por família) e confirme no banco:

```sql
select id, status, dry_run, started_at, completed_at, duration_ms, error_code
from backfill_runs
order by started_at desc
limit 5;
```

### 3.3 Idempotência e retomada

O comando é idempotente e retomável por construção (ver
`app/cli/backfill.py` e `docs/FINANCIAL_RULES.md`): se a execução for
interrompida (falha de rede, container reiniciado, `Ctrl+C`), basta rodar o
mesmo comando novamente. Não há um checkpoint separado para retomar -- o
próprio idempotente-por-linha já garante que o trabalho já persistido não é
duplicado, e o trabalho que faltou é concluído. Isso foi validado em CI
(`tests/test_backfill.py`, `tests/test_property_based_financial_rules.py`,
`tests/test_postgresql_integration.py`) rodando o comando duas e três vezes
seguidas e comparando o estado resultante.

### 3.4 Concorrência

`discover_transaction_duplicates`/`register_transaction_duplicates`,
`execute_integrity_run`/`_persist_finding` e `build_snapshot` já toleram
duas execuções concorrentes sobre a mesma família -- por idempotência
determinística (`group_key` único por `household_id`, checksum de
snapshot) e, onde a linha já existe, `SELECT ... FOR UPDATE`/savepoints
(ver os docstrings dessas funções em `app/services/`). O backfill não
introduz um novo mecanismo de concorrência -- herda as mesmas garantias
que o fluxo de importação/API já
usa em produção.

## 4. Observabilidade

- Toda execução (real ou dry-run) grava uma linha em `backfill_runs` com
  `status`, `dry_run`, escopo (famílias/período), versões
  (`financial_rules_version`, `calculation_version`, `app_version`),
  `started_at`/`completed_at`/`duration_ms`, `trace_id` e o relatório
  completo (`summary`, incluindo o antes/depois por família).
- Cada `IntegrityRun` criado pelo backfill tem `trigger = "backfill"`,
  distinguível de execuções `manual`/`import`/`transaction`/`close`/`system`.
- Nenhum dado financeiro sensível é logado além do que já é persistido nas
  tabelas do próprio Financial Integrity Engine (mesmo padrão de
  `IntegrityRun`/`IntegrityFinding`).

## 5. Critérios de parada

Interrompa a execução real (`Ctrl+C` é seguro -- a transação da família em
andamento é revertida pelo `SessionLocal`) e investigue antes de continuar
se:

- o relatório do `--dry-run` mostrar uma contagem de duplicidades ou
  documentos `unknown` muito acima do esperado para o volume da família;
- `backfill_runs.status = 'failed'` para qualquer família;
- qualquer teste de CI (especialmente `financial-invariants`,
  `property-tests`, `alembic-migration`, `integration-postgres`) não estiver
  verde no commit em uso.

## 6. Recuperação

- **Falha durante uma execução real:** a família que estava em processamento
  não recebe commit parcial (a sessão inteira é revertida no `except`
  handler de `main()`; um `BackfillRun` com `status="failed"` é registrado
  para auditoria). Rode o comando novamente -- é seguro repetir.
- **Suspeita de resultado incorreto após uma execução real:** o backfill
  nunca apaga nem sobrescreve `Transaction`/`Document`/`PayrollRecord`/
  `Commission`/`Obligation`, então os fatos financeiros de origem
  permanecem intactos independentemente do que o backfill produziu.
  Restaurar apenas os efeitos do backfill significa reverter as linhas
  criadas em `document_reconciliations`, `duplicate_groups`/
  `duplicate_group_members`, `financial_snapshots`/
  `financial_snapshot_lineage`, `integrity_runs`/`integrity_findings` desde
  o `started_at` do `BackfillRun` em questão -- prefira restaurar o backup
  tirado antes da execução (passo 1) a uma exclusão manual seletiva, que
  arrisca deixar rastro parcial.

## 7. Rollback

1. Restaurar o backup do banco tirado antes da execução (ver "Recuperação"
   acima) é o caminho recomendado para desfazer o *efeito* de uma execução
   real do backfill.
2. Para reverter a migration em si (por exemplo, antes mesmo de rodar o
   backfill, se o deploy precisar ser desfeito):
   ```bash
   docker compose exec app alembic downgrade 0008
   ```
   Remove apenas `backfill_runs`; nenhuma outra tabela ou fato financeiro é
   afetado.
3. Reverter o deploy da imagem da aplicação para a tag anterior ao PR 8
   também é seguro: a migration `0009` é aditiva e o schema anterior
   (`0008`) continua válido para o código anterior.

## 8. Pendências conhecidas

- O backfill não tem um mecanismo de retomada baseado em checkpoint
  explícito -- ele confia inteiramente em cada etapa ser idempotente (ver
  seção 3.3). Isso é uma decisão de design deliberada (evita uma classe
  inteira de bugs de checkpoint incorreto), não uma lacuna, mas fica
  registrado aqui para o engenheiro responsável avaliar se algum volume de
  dados futuro justificaria um mecanismo de retomada mais granular.
