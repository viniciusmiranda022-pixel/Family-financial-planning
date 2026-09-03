# Runbook — carga inicial de uma instalação limpa

Use este procedimento somente para popular uma instalação que já possui a família e pelo menos um administrador criado.

## 1. Segurança

- Faça backup antes da carga.
- Mantenha manifesto e documentos reais em `data/initial-load/`; `data/`, `*.pdf`, `*.csv`, `*.ofx` e planilhas já estão no `.gitignore`.
- Não commite manifesto real, extratos, faturas, holerites ou números de conta/cartão.
- Execute `--dry-run` antes de `--apply`.
- Conflito de conta, obrigação ou saldo bloqueia a carga. O CLI nunca sobrescreve esses fatos para "fazer passar".

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
    "investment_name": "Reserva DI",
    "investment_balance": "10000.00"
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

## 3. Prévia obrigatória

No host:

```powershell
cd C:\Users\ViniciusMiranda\Family-financial-planning
docker compose exec app python -m app.cli.initial_load /data/initial-load/manifest.json --dry-run
```

Se o diretório local `data/` não estiver montado dentro do container, copie temporariamente o diretório para o container ou execute o CLI no ambiente Python do projeto. Não altere o Compose somente para expor documentos reais sem necessidade.

O relatório mostra contas/obrigações/saldos que seriam criados, documentos já importados, quantidade de registros reconhecidos e status de reconciliação. `review_required` ou `ready_with_review` não é autorização para corrigir dados automaticamente; significa que o documento deverá ser revisado no sistema.

## 4. Aplicação

Depois de revisar a prévia:

```powershell
docker compose exec app python -m app.cli.initial_load /data/initial-load/manifest.json --apply
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

Reexecutar o mesmo manifesto não duplica contas, obrigações, observações de saldo idênticas ou documentos com o mesmo hash.

## 5. Depois da carga

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
