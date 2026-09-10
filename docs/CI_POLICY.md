# Política de uso do GitHub Actions

## Objetivo

O projeto possui uma franquia mensal limitada de minutos do GitHub Actions. Essa franquia deve ser tratada como recurso operacional finito. A política abaixo reduz execuções remotas desnecessárias **sem reduzir cobertura, remover gates ou enfraquecer critérios de merge**.

O GitHub Actions é uma confirmação independente do head final; não deve ser usado como ambiente iterativo de desenvolvimento.

## Princípios obrigatórios

1. **Validação local primeiro.** Claude/Codex deve executar localmente os checks aplicáveis ao escopo antes de fazer push.
2. **Pushes devem representar marcos relevantes.** Não fazer commits/pushes pequenos apenas para observar o CI ou descobrir falhas triviais que poderiam ser encontradas localmente.
3. **CI remoto confirma, não desenvolve.** O fluxo esperado é: implementar localmente → rodar testes direcionados → rodar suíte local completa quando aplicável → fazer push → usar GitHub Actions como validação independente.
4. **Os 12 gates obrigatórios permanecem obrigatórios para mudanças de código antes do merge.** Economia de minutos nunca justifica remover gate, reduzir cobertura, pular teste obrigatório, mudar regra financeira ou aceitar CI parcial.
5. **Mudanças apenas documentais não precisam consumir a suíte pesada.** Alterações exclusivamente em `*.md` e documentação sem efeito executável podem ser revisadas sem disparar os 12 gates, desde que não alterem código, workflows, dependências, migrations, schemas ou contratos executáveis.
6. **Novo head invalida o CI anterior quando houver mudança executável.** A validação remota exigida para merge deve corresponder ao head final aplicável.
7. **Não reexecutar jobs por rotina.** Re-run só é justificável após correção real, flake investigado, falha transitória comprovada ou indisponibilidade de infraestrutura resolvida.
8. **Falha de infraestrutura não é falha de código, mas continua sendo bloqueio de merge quando os gates obrigatórios não executaram.** Não contornar isso reduzindo critérios.

## Estratégia por etapa

### Durante implementação

Rodar localmente apenas os testes diretamente relacionados ao slice enquanto o código ainda está mudando. Exemplos:

- parser/reconciliação: testes do parser e reconciliação;
- autorização: testes de RBAC/household isolation;
- migrations: testes Alembic/PostgreSQL locais quando disponíveis;
- frontend: `node --check` e testes direcionados;
- regras financeiras: invariants/property tests correspondentes.

Claude deve acumular correções coerentes antes de fazer push, em vez de usar cada tentativa como uma execução remota.

### Antes de entregar para revisão

Quando aplicável ao ambiente local:

```text
ruff check .
pytest -q
node --check app/static/app.js
```

Além desses, executar os testes específicos do slice e quaisquer checks adicionais definidos no respectivo Work Order.

### No GitHub Actions

Para mudanças executáveis, os 12 gates documentados continuam sendo a barreira independente de merge:

- `lint`
- `unit`
- `financial-invariants`
- `property-tests`
- `parser-reconciliation`
- `projection-parity`
- `snapshot-channel-consistency`
- `advisor-contract-security`
- `frontend-syntax`
- `docker-build`
- `alembic-migration`
- `integration-postgres`

Esses gates devem ser executados no head final relevante antes do merge. Não criar um segundo conjunto de gates "econômicos" que permita merge sem a suíte obrigatória.

## Regras para documentação

Um PR pode ser classificado como **docs-only** apenas quando todas as mudanças forem documentais e não houver alteração em:

- `.github/workflows/`;
- código de aplicação;
- testes;
- Docker/Compose;
- scripts executáveis;
- migrations/Alembic;
- schemas/contratos executáveis;
- dependências;
- configuração com efeito de runtime.

Documentação normativa pode ser revisada semanticamente sem executar a suíte completa quando não houver mudança executável no mesmo PR. Assim que um PR docs-only passar a tocar qualquer área executável, deixa de ser docs-only e os gates completos voltam a ser obrigatórios.

## Controle de concorrência

O workflow deve cancelar automaticamente uma execução antiga de um mesmo PR quando um novo head for enviado. Isso evita gastar minutos validando um commit que já não pode mais ser mesclado.

A exceção é a branch `main`: uma execução já iniciada em `main` não deve ser cancelada por outra sem avaliação específica, pois pode representar validação de integração distinta.

## Push em `main`

O processo normal do projeto exige validação completa do head do PR **antes** do merge seguro. Por isso, o workflow principal não deve repetir automaticamente os 12 jobs em todo push para `main` quando a mesma mudança já passou pela validação obrigatória do PR.

Mudanças diretas em `main` devem ser evitadas para código executável. Se uma mudança executável precisar excepcionalmente entrar direto em `main`, ela deve receber validação equivalente antes ou imediatamente por uma execução manual controlada.

## Responsabilidades de Claude/Codex

Claude/Codex deve:

- trabalhar localmente até atingir um marco coerente;
- informar exatamente quais checks locais executou;
- não criar commits artificiais apenas para disparar CI;
- não solicitar re-run sem motivo técnico concreto;
- não reduzir cobertura para economizar minutos;
- não alterar regras financeiras para fazer testes passarem;
- não considerar CI de head antigo como aprovação do head novo;
- sinalizar quando um check obrigatório não pôde ser executado.

O engenheiro responsável decide se uma nova execução remota é necessária e mantém a autoridade de merge condicionada à documentação normativa, integridade financeira, segurança, testes e estado real do CI.

## Critério de sucesso

Esta política é bem-sucedida quando reduz consumo por iteração sem mudar o padrão de qualidade. O objetivo é ter **menos execuções, mais significativas**, e não menos testes.
