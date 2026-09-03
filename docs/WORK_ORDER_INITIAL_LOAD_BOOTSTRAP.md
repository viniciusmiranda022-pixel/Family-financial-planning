# Work Order — Initial Load Bootstrap

## Objetivo

Adicionar uma carga inicial local, auditável e idempotente para uma instalação vazia do Family Financial Planning, permitindo cadastrar estrutura financeira e importar documentos históricos sem inserir PII ou documentos reais no GitHub.

## Fonte de verdade

Este incremento deve obedecer, nesta ordem, a:

1. `docs/FINANCIAL_RULES.md`;
2. `docs/FINANCIAL_INVARIANTS.md`;
3. `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
4. `docs/ARCHITECTURE.md` e contratos de importação existentes.

## Escopo mínimo

- CLI `python -m app.cli.initial_load <manifest.json> --dry-run|--apply`.
- Manifesto local em JSON, mantido sob `data/` (já ignorado pelo Git).
- Upsert conservador de contas por nome: repetir o mesmo manifesto é no-op; conflito de metadados bloqueia em vez de sobrescrever silenciosamente.
- Atualização explícita apenas dos campos de `FinancialProfile` presentes no manifesto.
- Observação de saldo confirmada para contas de investimento; não inferir data ou saldo.
- Obrigações idempotentes; conflito com mesmo nome/vencimento e valor diferente bloqueia.
- Importar `bank_statement`, `credit_card` e `payroll` reutilizando o pipeline oficial de `/api/imports`, incluindo hash, criptografia, parser versionado, classificação, duplicidades, reconciliação, ReviewItems e auditoria.
- Reimportação de documento idêntico deve ser tratada como `skipped`, nunca duplicar dados.
- Relatório JSON de prévia/aplicação com contagens e documentos que exigem revisão.

## Invariantes/proibições

- Transferência interna não é renda nem despesa.
- Aplicação/resgate do Privilège DI é movimento patrimonial, não consumo.
- Pagamento de fatura é conciliação; compra do cartão não pode ser contada novamente no pagamento.
- Consignado já presente no holerite não pode ser descontado uma segunda vez.
- Não criar ou resolver duplicidade automaticamente fora do pipeline determinístico existente.
- Não inventar saldo, competência, data, categoria, total declarado ou reconciliação ausente.
- Nenhum PDF, extrato, holerite, CPF, número integral de conta/cartão ou manifesto real deve ser commitado.
- Nunca fazer `UPDATE` destrutivo em fatos existentes para forçar a carga a passar.
- Codex/Claude não têm autoridade para alterar fatos financeiros determinísticos.

## Segurança e privacidade

- Dados reais ficam somente no diretório local `data/initial-load/`.
- O CLI não imprime conteúdo de documentos nem dados sensíveis; imprime somente nomes de arquivo, status e contagens.
- Documentos aplicados passam pelo mesmo `EncryptedDocumentStore` do fluxo normal.

## Testes obrigatórios

- validação do manifesto e rejeição de referências inválidas;
- dry-run não persiste contas, perfil, saldos ou obrigações;
- segunda aplicação é idempotente para estrutura e documentos já importados;
- conflito de conta/obrigação bloqueia sem alteração silenciosa;
- saldo confirmado cria `AccountBalanceObservation` com `source=manual_confirmed` e não é sobrescrito por reexecução;
- documentos passam pelo pipeline oficial de importação, não por implementação paralela de regra financeira.

## Concorrência

O CLI é uma ferramenta de bootstrap de operador único (execução manual, sequencial, documentada no runbook). `Account`, `FinancialProfile` e `Document` têm `UniqueConstraint` no banco e por isso uma corrida entre execuções sempre falha alto (erro visível) em vez de duplicar. `Obligation` e `AccountBalanceObservation` não têm essa constraint; adicioná-la mudaria o comportamento aceito da API web hoje (`POST /obligations` permite duas obrigações com mesmo nome/vencimento) só para fechar uma corrida local a esta CLI, o que é risco de compatibilidade fora do escopo desta fatia. Em vez disso, o CLI toma um `pg_advisory_xact_lock` por família (mesmo padrão de `app/services/financial_snapshots.py::_lock_snapshot_key`) durante a janela de SELECT-then-INSERT dessas duas entidades: uma segunda invocação concorrente bloqueia em vez de arriscar duplicar. É um no-op em SQLite (dev local); execução concorrente continua fora de contrato nesse ambiente e documentada como tal no runbook.

## Executor/revisão

Claude pode implementar e deve contestar este Work Order quando houver evidência concreta de que ele contradiz os documentos normativos ou cria risco técnico. Claude não pode fazer merge, alterar regra financeira não documentada, corrigir automaticamente dados financeiros reais nem incluir PII no repositório. Divergência não resolvida bloqueia merge.
