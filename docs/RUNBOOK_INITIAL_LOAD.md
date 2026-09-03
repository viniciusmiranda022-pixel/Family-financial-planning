# Runbook — carga inicial de uma instalação limpa

Use este procedimento somente para popular uma instalação que já possui a família e pelo menos um administrador criado.

## 1. Segurança

- Faça backup antes da carga.
- Mantenha manifesto e documentos reais em `data/initial-load/`; `data/`, `*.pdf`, `*.csv`, `*.ofx` e planilhas já estão no `.gitignore`.
- Não commite manifesto real, extratos, faturas, holerites ou números de conta/cartão.
- Execute `--dry-run` antes de `--apply`.
- Conflito de conta, obrigação ou saldo bloqueia a carga. O CLI nunca sobrescreve esses fatos para "fazer passar".
- Não copie os documentos originais em claro para `/data` do container. `/data` é persistente e é usado pelo armazenamento criptografado da aplicação. Para a carga, os originais devem existir apenas temporariamente em `/tmp/initial-load` e ser removidos ao final.
- Esta é uma ferramenta de bootstrap de operador único: não execute duas invocações do CLI simultaneamente para a mesma família. Em PostgreSQL (produção/CI), o CLI toma um `pg_advisory_xact_lock` por família durante a resolução de contas/obrigações/saldos, então uma segunda invocação concorrente bloqueia até a primeira terminar em vez de arriscar duplicar `Obligation`/`AccountBalanceObservation` (essas duas entidades não têm `UniqueConstraint` no banco — ver `app/cli/initial_load.py::_lock_household_initial_load`). Em SQLite (dev local) esse lock é um no-op; não rode o CLI concorrentemente fora de produção.
- Erros de validação do manifesto, conflitos de conta/obrigação/saldo e falhas de parser na prévia nunca ecoam o valor rejeitado, o nome livre da conta/obrigação, o caminho local completo do documento nem fragmentos do próprio documento no stdout — apenas o campo/identificador do manifesto (chave de conta, índice da obrigação), nome de arquivo e um código estável de status (por exemplo `parse_failed`). Isso vale mesmo quando a saída é copiada para um chat/ticket de suporte. Para investigar a causa exata de uma falha de parser ou de um valor de manifesto rejeitado, use o arquivo local — o CLI não grava esse detalhe em nenhum outro lugar.

## 2. Manifesto

Crie `data/initial-load/manifest.json`:

```json
{
  "version": 1,
  "load_id": "family-bootstrap-2026-09",
  "accounts": [
    {
      "key": "bank",
      "name": "Conta principal",
      "institution": "Banco Exemplo",
      "account_type": "checking",
      "owner_label": "Família"
    },
    {
      "key": "card",
      "name": "Cartão principal",
      "institution": "Banco Exemplo",
      "account_type": "credit_card",
      "owner_label": "Família",
      "last_four": "1234"
    },
    {
      "key": "liquidity",
      "name": "Reserva DI",
      "institution": "Banco Exemplo",
      "account_type": "investment",
      "owner_label": "Família"
    }
  ],
  "profile": {
    "investment_name": "Reserva DI"
  },
  "balances": [
    {
      "account": "liquidity",
      "amount": "10000.00",
      "as_of_date": "2026-09-03",
      "observation_type": "point_in_time"
    }
  ],
  "obligations": [
    {
      "name": "Exemplo - parcela 1/10",
      "due_date": "2026-09-10",
      "amount": "1500.00",
      "category": "asset_acquisition"
    }
  ],
  "documents": [
    {
      "path": "extrato.pdf",
      "document_type": "bank_statement",
      "account": "bank"
    },
    {
      "path": "fatura.pdf",
      "document_type": "credit_card",
      "account": "card"
    },
    {
      "path": "holerite.pdf",
      "document_type": "payroll"
    }
  ]
}
```

Caminhos relativos são resolvidos a partir da pasta onde está o manifesto.

O manifesto não aceita `profile.investment_balance`. Esse campo é o contador
mutável e não reconciliado descrito em `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`
(seção 1); a carga inicial só registra saldo de investimento através de
`balances[]`, que gera uma `AccountBalanceObservation(source=manual_confirmed)`
auditável, para não criar duas fontes divergentes para o mesmo saldo.

## 3. Preparar os arquivos temporariamente no container

Com o serviço `app` em execução, no PowerShell do host:

```powershell
cd C:\Users\ViniciusMiranda\Family-financial-planning
docker compose exec app sh -lc "rm -rf /tmp/initial-load && mkdir -p /tmp/initial-load"
docker compose cp .\data\initial-load\. app:/tmp/initial-load/
```

Essa cópia em `/tmp` é apenas staging. Quando o documento é efetivamente importado, o pipeline oficial grava sua própria cópia criptografada no armazenamento da aplicação.

## 4. Prévia obrigatória

```powershell
docker compose exec app python -m app.cli.initial_load /tmp/initial-load/manifest.json --dry-run
```

O relatório mostra contas/obrigações/saldos que seriam criados, documentos já importados, quantidade de registros reconhecidos e status de reconciliação. `review_required` é um fato determinístico (o parser rejeitou o arquivo). `pending_full_validation` significa apenas que o arquivo foi parseado e reconciliado com sucesso na prévia — a prévia **não** executa classificação nem detecção de duplicidade (isso só acontece no `--apply`, via pipeline oficial), então um documento `pending_full_validation` ainda pode virar `imported_with_review` na aplicação real. Nenhum dos dois status autoriza corrigir dados automaticamente.

O `--dry-run` não persiste contas, perfil, obrigações ou saldos e não cria `Document`/`Transaction`; ele valida os arquivos e executa parser/reconciliação para produzir a prévia.

## 5. Aplicação

Somente depois de revisar a prévia:

```powershell
docker compose exec app python -m app.cli.initial_load /tmp/initial-load/manifest.json --apply
```

A estrutura é aplicada de forma conservadora. Documentos são encaminhados ao mesmo pipeline de `/api/imports`, preservando:

- SHA-256 e bloqueio de reimportação exata;
- criptografia do documento;
- parser versionado;
- classificação determinística;
- agrupamento de possíveis duplicidades;
- `DocumentReconciliation`;
- `ReviewItem`;
- trilha de auditoria.

A aplicação é retomável por idempotência. Cada documento é importado pelo pipeline normal, que possui sua própria transação/commit; se uma execução for interrompida, reexecute o mesmo manifesto. Contas, obrigações, observações de saldo idênticas e documentos com o mesmo SHA serão ignorados em vez de duplicados. Não trate uma execução interrompida como motivo para apagar ou reescrever fatos já importados.

## 6. Remover o staging em claro

Após o `--apply` — inclusive quando houver itens para revisão — remova a cópia temporária:

```powershell
docker compose exec app sh -lc "rm -rf /tmp/initial-load"
```

Os arquivos originais continuam na pasta local escolhida pelo operador. Dentro do armazenamento persistente do sistema permanece apenas a cópia gerenciada pelo `EncryptedDocumentStore`.

## 7. Depois da carga

1. Revise `ReviewItem` e grupos de duplicidade.
2. Não marque findings como resolvidos sem corrigir/revisar o fato correspondente.
3. Execute o fechamento mensal/integridade dos períodos relevantes antes de confiar em relatórios/projeções.
4. Guarde os documentos originais fora do Git; o sistema mantém a cópia importada criptografada.

## Semântica financeira preservada

- transferência interna: não é renda nem despesa;
- aplicação/resgate: movimento patrimonial;
- pagamento de fatura: conciliação, não uma segunda despesa;
- consignado no holerite: não é descontado novamente;
- saldos só são confirmados na data explicitamente informada pelo operador.
