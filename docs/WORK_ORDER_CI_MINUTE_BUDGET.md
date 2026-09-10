# Work Order — controle de consumo do GitHub Actions

## Objetivo

Reduzir consumo desnecessário da franquia mensal de GitHub-hosted Actions sem reduzir os 12 gates obrigatórios, a cobertura de testes ou os critérios técnicos de merge.

## Escopo

- documentar a política operacional de uso de minutos;
- tornar o CI principal orientado a pull request;
- evitar uma segunda execução automática completa após merge em `main`;
- cancelar execução obsoleta do mesmo PR quando um novo head for enviado;
- não disparar a suíte pesada em PR exclusivamente documental;
- manter `workflow_dispatch` para validação manual excepcional.

## Critérios de aceite

1. Os 12 jobs/gates existentes continuam presentes e semanticamente inalterados.
2. Toda mudança executável continua exigindo CI completo no head final antes do merge.
3. Novo push no mesmo PR cancela a execução antiga ainda em andamento.
4. PR docs-only não dispara a suíte pesada.
5. Merge em `main` não repete automaticamente a suíte completa já aprovada no PR.
6. É possível disparar manualmente o workflow quando necessário.
7. Nenhuma regra financeira, migration, código de aplicação, dado real ou teste é alterado.

## Proibições

- não remover gates;
- não reduzir cobertura para economizar minutos;
- não transformar testes obrigatórios em opcionais para mudanças executáveis;
- não aceitar CI de head antigo como validação de head novo;
- não usar falha de billing/quota como justificativa para merge sem validação;
- não alterar regras financeiras ou invariantes.

## Risco principal

Mudanças diretas em `main` deixam de disparar automaticamente os 12 gates. Mitigação: código executável deve entrar por PR validado; `workflow_dispatch` permanece disponível para exceções controladas. O repositório atualmente não possui branch protection ativa, portanto essa disciplina é processual e deve ser tratada como obrigatória pelo engenheiro responsável.

## Executor e autoridade

Claude pode revisar/contestar tecnicamente esta política com evidência concreta, mas não pode fazer merge nem reduzir os gates. O engenheiro responsável decide o merge após revisar o workflow e uma execução válida quando a franquia estiver disponível.
