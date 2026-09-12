# Work Order — October Go-Live Slice 1

## Objetivo

Implementar o motor de caixa operacional/Privilège DI, saldos confirmados/derivados e a visão canônica necessária ao Dashboard, conforme `docs/OCTOBER_GO_LIVE_REBASELINE.md` e `docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md`.

Este é o Slice 1 do P0 #87. Não antecipar Slices 2+.

## Fonte normativa

Ordem de autoridade:

1. `docs/OCTOBER_GO_LIVE_REBASELINE.md`;
2. este Work Order;
3. `docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md`;
4. `docs/FINANCIAL_INVARIANTS.md` e `docs/FINANCIAL_RULES.md`, após harmonização explícita do comportamento legado;
5. arquitetura, testes e código existente.

Quando regra antiga conflitar com o rebaseline, migrar/documentar/testar a nova semântica. Não preservar comportamento incorreto apenas porque existe teste legado.

## Decisão arquitetural aceita do Slice 0

O Technical Challenge #1 foi aceito com esta fronteira obrigatória:

- `settle_liquidity` pode permanecer para PREVISTO/cenários hipotéticos e projeções 30/60/90;
- seu resultado NÃO pode materializar resgate/aplicação REALIZADA;
- fato realizado de transferência Corrente ↔ Privilège exige movimento bancário observado/importado ou ação explicitamente confirmada pelo usuário/Assistente;
- `AccountBalanceObservation` prova posição/saldo em um instante, não prova causa nem transferência individual;
- observação de saldo isolada pode atualizar a posição soberana e produzir divergência/unexplained delta, nunca fabricar resgate/aplicação.

## Escopo obrigatório

### 1. Separar fato realizado de liquidação hipotética

Remover a autoridade de `settle_liquidity` sobre snapshots REALIZADOS/fechados sem evidência.

Criar/refatorar o caminho canônico de reconciliação realizada, mantendo um único motor financeiro e sem cálculo paralelo.

### 2. Saldo confirmado soberano

Implementar/validar a semântica:

- saldo observado/confirmado;
- movimentos posteriores à observação;
- saldo corrente derivado desde a observação;
- divergência entre reconstruído e confirmado.

Nunca substituir saldo confirmado por reconstrução derivada no mesmo instante.

Nunca fabricar transação de ajuste para zerar divergência.

### 3. Privilège DI e Conta Corrente

Aplicação/resgate são transferências internas de liquidez:

- nunca renda;
- nunca despesa;
- nunca consumo;
- nunca superávit/déficit inventado.

Resultado operacional negativo não prova resgate. Resultado operacional positivo não prova aplicação.

### 4. Dashboard / snapshot canônico

Expor uma fonte canônica coerente para, no mínimo:

- saldo atual da Conta Corrente;
- saldo atual do Privilège DI;
- liquidez consolidada atual;
- origem/evidência do último saldo confirmado;
- movimentos posteriores à observação;
- divergência de reconciliação quando existir;
- gasto realizado do período sem dupla contagem;
- entradas realizadas do período;
- sinalização de dados que requerem atenção.

Não implementar ainda cartões/faturas do Slice 2 nem obrigações/projeção do Slice 3 além do mínimo necessário para preservar contratos existentes.

### 5. Harmonização normativa

Atualizar, conforme necessário e somente para esta semântica:

- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- README/arquitetura quando afetados.

INV-005/006/007 e testes relacionados devem ser rescopados para distinguir REALIZADO de PREVISTO/hipotético. Não apagar cobertura: substituir por cobertura semanticamente correta.

Se houver `financial_rules_version`, fazer bump explícito se a documentação vigente exigir.

## Invariantes obrigatórios

1. Transferência interna não cria renda/despesa.
2. Snapshot REALIZADO não fabrica `liquidity_used`/`liquidity_deposit` a partir de resultado operacional.
3. `AccountBalanceObservation` isolada nunca materializa transferência.
4. Movimento observado/importado ou ação explicitamente confirmada pode materializar transferência, com provenance.
5. Saldo confirmado vence reconstrução derivada no instante observado.
6. Divergência permanece visível e investigável; não há ajuste sintético silencioso.
7. Projeção pode usar matemática determinística hipotética, desde que semanticamente marcada como PREVISTO/cenário e nunca promovida a fato.
8. Dados reais históricos são preservados; nenhuma migration destrutiva ou reclassificação silenciosa.
9. Dashboard, snapshot e demais consumidores usam a mesma fonte canônica, sem segundo motor financeiro.
10. Precisão monetária permanece Decimal/Numeric conforme padrões existentes.

## Migration / dados

Prefira mudança sem schema se suficiente. Se schema/migration for realmente necessário para provenance, estado ou compatibilidade:

- migration aditiva/reversível;
- validar cadeia Alembic;
- sem reinterpretação automática de históricos;
- backfill apenas determinístico e auditável;
- UNKNOWN/REVIEW_REQUIRED quando o fato histórico não puder ser provado;
- documentar rollback.

Qualquer necessidade de migration não prevista no inventário do Slice 0 deve ser justificada no PR.

## Testes obrigatórios

Além da suíte existente, adicionar/ajustar testes que provem explicitamente:

- `test_actual_snapshot_never_fabricates_liquidity_withdrawal_without_transfer_movement_or_confirmed_action`;
- `test_account_balance_observation_alone_never_materializes_liquidity_transfer`;
- transferência interna observada altera composição de caixa sem renda/despesa;
- ação confirmada de resgate mantém provenance e não duplica gasto;
- saldo confirmado soberano com reconstrução divergente;
- movimentos posteriores à observação derivam posição corrente sem reescrever observação;
- divergência não gera ajuste sintético;
- projeção hipotética continua funcionando sem contaminar REALIZADO;
- paridade dos canais/consumidores canônicos afetados;
- regressão dos invariantes financeiros impactados;
- property-based tests aplicáveis à separação transferência interna × renda/despesa e realizado × previsto.

Todos os gates CI existentes aplicáveis devem permanecer verdes, incluindo unit, financial-invariants, property-tests, projection-parity, snapshot-channel-consistency, alembic/integration-postgres quando aplicável, lint e demais jobs do workflow.

## Segurança, privacidade e auditoria

- não ampliar acesso do Codex/Advisor;
- não enviar dados financeiros brutos desnecessários ao modelo;
- preservar household isolation/autorização existente;
- toda nova provenance deve ser auditável;
- não registrar secrets/PII desnecessária.

## Proibições

Claude NÃO pode:

- fazer merge;
- iniciar Slice 2;
- sintetizar resgate/aplicação por déficit/sobra;
- usar saldo observado como prova de causa;
- criar ajuste financeiro silencioso;
- apagar/reclassificar dados reais automaticamente;
- reduzir cobertura para passar CI;
- alterar invariantes apenas para satisfazer testes legados;
- criar segundo motor financeiro;
- antecipar cartões/faturas, obrigações, Assistente, navegação, investimentos ou relatórios fora do necessário para compatibilidade do Slice 1.

## Technical Challenge

Claude pode e deve contestar este Work Order quando houver evidência verificável de risco, contradição normativa ou alternativa tecnicamente superior. A objeção deve citar código/documentação/teste, risco, alternativa e estratégia de validação. Divergência relevante não resolvida bloqueia conclusão.

## Definition of Done

Slice 1 está pronto para revisão somente quando:

- fato REALIZADO e liquidação hipotética estiverem separados;
- saldo confirmado for soberano e divergências forem explícitas;
- Privilège/Conta Corrente forem tratados como composição de liquidez, sem renda/despesa sintética;
- Dashboard/snapshot consumirem fonte canônica consistente;
- documentação normativa conflitante estiver harmonizada;
- testes novos e regressivos cobrirem a semântica;
- migrations, se houver, forem seguras e reversíveis;
- CI estiver integralmente verde;
- PR documentar arquitetura, riscos, compatibilidade, testes e rollback;
- nenhuma Technical Challenge relevante permanecer aberta.
