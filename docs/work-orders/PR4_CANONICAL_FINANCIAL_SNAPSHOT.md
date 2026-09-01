# Work Order — PR 4: Canonical Financial Snapshot

## Executor

Claude é o executor deste vertical slice. O engenheiro responsável revisará arquitetura, semântica financeira, migrations, testes e CI antes de qualquer merge.

Claude não pode fazer merge, alterar regra financeira não documentada, reduzir cobertura para fazer CI passar, corrigir dados financeiros reais automaticamente, executar auto-fix destrutivo ou dar a Claude/Codex/Advisor autoridade sobre fatos determinísticos. Claude pode e deve contestar tecnicamente este Work Order ou uma revisão quando houver evidência concreta na documentação, código, testes ou documentação oficial da tecnologia.

## Fonte de verdade

Ler antes de implementar:

- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- documentação e código produzidos pelos PRs 1–3;
- migrations Alembic e testes existentes aplicáveis.

A documentação normativa prevalece sobre conveniência de implementação. Se houver contradição técnica real, abrir `TECHNICAL CHALLENGE` no PR em vez de mudar a regra silenciosamente.

## Objetivo

Implementar exatamente o PR 4 documentado no plano de integridade: **Canonical Financial Snapshot**.

Criar uma fonte financeira canônica e determinística para o período, com lineage suficiente para explicar os agregados e eliminar cálculos paralelos entre consumidores. O snapshot deve modelar corretamente liquidez, resultado operacional, gasto, caixa bancário, cartão e movimentos patrimoniais, sem introduzir ainda a migração dos consumidores do PR 5.

## Escopo obrigatório

1. Introduzir contrato/entidade `FinancialSnapshot` conforme a arquitetura documentada, com identificação de household, período, versões de regra/cálculo, timestamps, `trace_id`, status de integridade aplicável e campos financeiros canônicos.
2. Persistir lineage/provenance dos agregados relevantes para que seja possível rastrear quais transações, reconciliações, observações ou fontes contribuíram para cada valor material.
3. Implementar o `Financial Engine` central e determinístico para construir o snapshot a partir das fontes canônicas existentes.
4. Aplicar a regra normativa do Privilège DI: resultado negativo consome liquidez disponível até zero; excesso vira `uncovered_deficit`; piso de segurança é apenas referência/alerta e nunca dinheiro bloqueado.
5. Separar explicitamente, sem ambiguidade semântica:
   - `operating_income`;
   - `operating_expenses`;
   - `operating_result`;
   - `budget_usage`;
   - `bank_cash_in`;
   - `bank_cash_out`;
   - `card_spend`/compromissos de cartão conforme competência canônica;
   - movimentos patrimoniais (aplicações/resgates/transferências), sem contaminarem renda, despesa ou consumo;
   - saldo inicial/final de liquidez, liquidez utilizada, déficit sem cobertura e distância do piso.
6. Usar precedência canônica persistida e dados/reconciliações produzidos no PR 3 quando aplicável; não recriar heuristicamente uma segunda política de precedência.
7. Integrar checks determinísticos necessários do Financial Integrity Engine ao snapshot sem transformar ausência de evidência em `pass`.
8. Preservar compatibilidade com dados existentes e manter alterações de schema aditivas e reversíveis.
9. Atualizar documentação técnica/normativa somente quando necessário para refletir contratos concretos já previstos; não mudar semântica financeira para acomodar código.

## Fora de escopo

Não antecipar os próximos slices. Em particular, não migrar ainda dashboard, relatórios, exportações ou Advisor para consumir exclusivamente o snapshot se isso pertence ao PR 5 documentado. Não implementar Projection Validator, monthly close final, UX de Integrity Center ou auditoria semântica do Advisor fora do que o PR 4 requer.

## Invariantes prioritários

A implementação deve preservar e exercitar, no mínimo quando aplicáveis ao snapshot:

- `INV-001` transferência interna sem efeito operacional;
- `INV-002` pagamento de fatura sem nova despesa;
- `INV-003` aplicação como movimento patrimonial;
- `INV-004` resgate sem receita operacional;
- `INV-005` saldo de liquidez nunca negativo;
- `INV-006` déficit consome liquidez antes de ficar sem cobertura;
- `INV-007` piso de segurança não bloqueia liquidez;
- `INV-013` VA/VR não viram caixa livre;
- `INV-014` duplicidade não pode contaminar totais segundo sua resolução/precedência;
- `INV-015` somente a fonte canônica histórica entra nos totais;
- `INV-016` estorno compensa gasto sem criar renda;
- `INV-017` competência de cartão é única e canônica;
- `INV-019`, `INV-020`, `INV-021` e `INV-022` conforme o contrato normativo aplicável a snapshot/publicação e consistência entre canais.

Se algum invariant ainda não puder ser avaliado por falta de fatos nesta fatia, o resultado deve ser `unknown`, nunca `pass` fabricado.

## Regras financeiras obrigatórias

- Valores monetários devem usar `Decimal`/`Numeric` e arredondamento normativo `ROUND_HALF_UP` em centavos; evitar `float` em fatos financeiros canônicos.
- `saldo_final = max(0, saldo_inicial + resultado_mensal)`.
- `deficit_sem_cobertura = max(0, -(saldo_inicial + resultado_mensal))`.
- Resultado negativo usa todo saldo disponível necessário mesmo abaixo do piso.
- Aplicações, resgates, transferências internas e pagamentos de fatura não podem inflar `operating_income`, `operating_expenses`, `budget_usage` ou resultado operacional.
- Compra no cartão e saída bancária devem permanecer conceitos distintos.
- Não criar cálculo duplicado para atender endpoint/tela específica.
- Codex/Advisor não participa da construção, correção ou validação determinística do snapshot.

## Migrations e compatibilidade

Se houver migration:

- revisar a cadeia Alembic atual;
- usar nova revisão com `down_revision` correto;
- preferir schema aditivo;
- não apagar, reclassificar ou sobrescrever dados financeiros existentes;
- não executar backfill destrutivo;
- testar upgrade/downgrade e DDL PostgreSQL;
- testar upgrade a partir do schema real resultante do PR 3;
- documentar rollback.

Dados históricos insuficientes devem produzir snapshot incompleto/status correspondente ou finding, nunca números inventados.

## Testes obrigatórios

Adicionar cobertura proporcional ao risco, incluindo quando aplicável:

- unidade do Financial Engine;
- integração snapshot + banco;
- invariants do snapshot;
- property-based tests para propriedades financeiras;
- precisão/rounding monetário;
- idempotência: mesmos fatos e versões produzem mesmo resultado econômico;
- lineage: agregado material é rastreável às fontes;
- transferências internas não mudam resultado;
- pagamento de cartão não duplica despesa;
- aplicações/resgates não alteram resultado operacional;
- déficit menor, igual e maior que o saldo do Privilège;
- piso acima do saldo não impede consumo da liquidez;
- cartão separado de `bank_cash_out`;
- estorno sem inflação de renda;
- duplicata/cópia não canônica não entra nos totais;
- ausência de saldo/reconciliação obrigatória resulta em `unknown`/incompleto, sem default artificial;
- migration upgrade/downgrade e equivalência ORM/Alembic quando houver schema novo;
- regressão dos testes existentes.

Executar todos os gates existentes: Ruff, Pytest completo, testes Alembic/PostgreSQL, JavaScript/PowerShell quando aplicáveis, Docker builds, Compose e GitHub Actions.

## Segurança, privacidade e auditabilidade

- Não introduzir secrets, PII ou documentos financeiros no Git.
- Não ampliar acesso do Advisor/Codex ao banco ou documentos.
- Lineage deve usar identificadores/metadados necessários e evitar copiar conteúdo sensível desnecessariamente.
- Mudanças relevantes devem manter `trace_id` e versão das regras/cálculo.
- Nenhuma execução do snapshot pode alterar silenciosamente transações, reconciliações, grupos de duplicidade, observações de saldo ou perfil financeiro de origem.

## Critérios de aceite

O PR só está pronto para revisão final quando:

- o snapshot canônico e o Financial Engine do PR 4 estão implementados sem antecipar PR 5;
- não há cálculos financeiros concorrentes novos;
- regras do Privilège e separação gasto/caixa/cartão/patrimônio estão corretas;
- lineage é auditável;
- invariants aplicáveis estão cobertos e nenhum `unknown` foi mascarado;
- migrations são seguras, reversíveis e compatíveis;
- testes novos e regressão completa passam;
- todos os checks de CI aplicáveis estão verdes;
- documentação e rollback estão atualizados;
- não há Technical Challenge relevante sem resolução.

## Proibições

- Não fazer merge.
- Não iniciar PR 5.
- Não alterar regras financeiras não documentadas.
- Não mudar invariant para fazer teste passar.
- Não reduzir cobertura.
- Não corrigir dados reais automaticamente.
- Não criar auto-fix destrutivo.
- Não transformar ausência de evidência em sucesso.
- Não permitir que IA determine fatos, saldos ou reconciliação.
- Não remover lineage ou evidência histórica para simplificar a implementação.
