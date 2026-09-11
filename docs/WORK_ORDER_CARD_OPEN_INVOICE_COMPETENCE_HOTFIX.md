# Work Order — Hotfix de competência de compras em cartão com fatura aberta

## Prioridade

**PRIORIDADE 0 — correção funcional antes de qualquer melhoria pendente.**

Este hotfix deve ser tratado antes dos PRs #57 e #56 e antes de qualquer novo slice do roadmap. O PR #80 deixou de ser necessário após o repositório se tornar público e não deve bloquear este trabalho.

## Contexto observado

Em 10/09/2026 foi observada divergência na tela de cartões: compras manuais recentes lançadas no cartão Itaú e no cartão Nubank foram contabilizadas em setembro, embora pertençam à fatura ainda aberta que fecha no início de outubro e, portanto, devam compor a competência de outubro.

A documentação normativa já exige que compras de cartão sigam a competência da fatura (`docs/FINANCIAL_RULES.md`) e o INV-017 exige uma única competência canônica para dashboard, fechamento e relatórios (`docs/FINANCIAL_INVARIANTS.md`). A data original da compra deve permanecer preservada como linhagem.

## Objetivo

Corrigir o caminho canônico de criação/edição de compras em cartão para que a `competence` seja determinada pelo ciclo configurado do cartão e não simplesmente pelo mês de `booked_at`, garantindo que compras realizadas enquanto a próxima fatura ainda está aberta sejam atribuídas à competência correta da fatura.

## Diagnóstico obrigatório antes de alterar código

Claude deve reproduzir o defeito em teste automatizado contra o `main` atual, usando somente dados sintéticos e contas de cartão com `card_closing_day`/`card_due_day` explicitamente configurados. Deve identificar qual caminho de criação deixa a compra cair no mês de `booked_at` ou calcula a competência incorretamente.

Inspecionar, no mínimo:

- `app.api._card_invoice_competence` e qualquer definição duplicada/sombreada;
- helper responsável por resolver competência de transação;
- `POST /api/transactions` (lançamento manual);
- `PATCH /api/transactions/{id}` se permitir alteração de data/conta/tipo;
- confirmação de Smart Capture, se reutilizar o mesmo contrato;
- serialização/consulta de cartões no dashboard e relatórios;
- projeção de parcelas futuras;
- testes existentes de competência de cartão e INV-017.

Não assumir a causa sem teste falhando contra o comportamento atual.

## Regra financeira normativa

1. Compra em cartão deve ser contabilizada na competência da fatura correspondente ao ciclo configurado da conta.
2. `booked_at`/`occurred_at` continuam representando a data real da compra; não devem ser reescritos para a data da fatura.
3. Dashboard, relatórios, fechamento mensal, parcelas futuras e Advisor devem consumir a mesma `competence` persistida/canônica, sem recálculo divergente no frontend.
4. Pagamento da fatura continua sendo apenas conciliação (INV-002).
5. A correção não pode alterar silenciosamente regras financeiras existentes nem criar segunda implementação de competência.

## Critérios de aceite

- Um teste de regressão demonstra o bug no `main` atual e passa após a correção.
- Compra em cartão com ciclo configurado recebe `competence` coerente com a fatura aberta/fechada e com o INV-017.
- Compra feita após o fechamento da fatura corrente vai para a próxima competência.
- Compra feita antes ou no limite correto do fechamento permanece na competência correta da fatura em curso, conforme a semântica documentada do ciclo.
- Virada de dezembro/janeiro é coberta.
- Conta que não seja cartão mantém competência baseada na data normal, sem regressão.
- Cartão sem ciclo configurado continua falhando de forma explícita/fail-closed; não inventar competência.
- `booked_at` e `occurred_at` permanecem intactos.
- Parcelas futuras continuam projetadas a partir da competência canônica, sem duplicação.
- Nenhuma mudança em INV-001, INV-002, INV-003, INV-004, INV-014 ou demais invariantes.
- Nenhuma migration é esperada. Se Claude concluir que uma migration é indispensável, deve abrir Technical Challenge antes de implementar.
- Nenhum dado real, screenshot com PII, extrato ou valor pessoal deve ser commitado.

## Dados já existentes

Este hotfix **não está autorizado a fazer backfill automático nem corrigir silenciosamente transações reais existentes**. Se o diagnóstico concluir que lançamentos já persistidos possuem `competence` incorreta, Claude deve apenas:

- identificar tecnicamente como detectá-los;
- propor procedimento explícito, auditável e reversível de correção;
- aguardar aprovação do engenheiro responsável antes de qualquer mutação de dados reais.

## Testes obrigatórios

No mínimo:

- teste unitário do cálculo canônico de competência;
- teste de API para lançamento manual em cartão;
- teste de alteração de compra existente, se `PATCH` puder afetar competência;
- teste de virada de mês e de ano;
- teste de cartão sem ciclo configurado;
- teste de conta não-cartão;
- teste de paridade entre competência persistida e publicação na visão de cartões;
- regressão de parcelas futuras;
- `tests/test_financial_invariants.py` cobrindo INV-017;
- suíte completa local;
- `ruff check .`;
- `node --check app/static/app.js` se houver alteração frontend;
- 12 gates do GitHub Actions no head final.

## Proibições

- não alterar a regra normativa apenas para acomodar o teste;
- não hardcodar Itaú/Nubank por nome se o ciclo já está persistido na conta;
- não criar cálculo de competência no JavaScript;
- não corrigir dados reais automaticamente;
- não reescrever datas originais da compra;
- não reduzir cobertura ou remover gates;
- não fazer migration destrutiva;
- não duplicar o motor de competência;
- não conceder ao Claude/Codex autoridade para decidir competência financeira a partir de linguagem natural.

## Relação de revisão

Claude é o executor deste hotfix e **não pode fazer merge**. Deve contestar este Work Order se encontrar evidência concreta de contradição com a documentação normativa, comportamento verificável, contrato de API ou risco de regressão. Divergência não resolvida bloqueia o merge.

O engenheiro responsável revisará diff, arquitetura, invariantes, compatibilidade, segurança, testes e CI antes de qualquer merge.
