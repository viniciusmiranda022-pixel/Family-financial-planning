# Work Order — Go-live dos fluxos financeiros manuais

## Executor

Claude é o executor deste slice. O engenheiro responsável revisará integralmente a implementação antes de qualquer merge.

Claude **não pode fazer merge**, alterar regras financeiras não documentadas, mudar invariantes para fazer testes passarem, corrigir dados financeiros reais automaticamente, introduzir cálculos paralelos ou dar a Claude/Codex/Advisor autoridade sobre fatos determinísticos. Claude **pode e deve contestar** este Work Order ou orientações do engenheiro quando houver evidência concreta de contradição com a documentação normativa, comportamento verificável, testes, segurança, integridade financeira ou menor risco de regressão. Divergência não resolvida bloqueia merge.

## Posição no roadmap

Próximo item não concluído de `docs/ROADMAP.md`, imediatamente após **testes com cópias anonimizadas dos documentos reais**: **go-live de uso manual / fluxos financeiros do dia a dia**.

Não avançar neste PR para itens de Fase 3 ou Fase 4.

## Fonte de verdade

Antes de implementar, revisar no mínimo:

- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/ROADMAP.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`;
- fluxos atuais de criação/edição de `Transaction`, Central Inteligente, snapshots, duplicidade, reconciliação, auditoria e projeção;
- testes atuais de API, importação, snapshots, invariantes e household isolation.

A documentação normativa e os invariantes prevalecem sobre comportamento legado ou conveniência de UI.

## Objetivo

Consolidar um fluxo manual explícito, simples e auditável para os eventos financeiros cotidianos já documentados, sem exigir que o usuário conheça tipos internos e sem criar um segundo motor financeiro.

## Escopo obrigatório

Implementar, pelo mesmo modelo e serviços canônicos já existentes, os fluxos manuais para:

1. `expense` — despesa;
2. `income` — receita;
3. `transfer` — transferência entre contas;
4. `investment` — aplicação;
5. `redemption` — resgate;
6. `refund` — estorno;
7. `reconciliation` — pagamento de fatura/conciliação.

Também cobrir obrigatoriamente:

- pagamento manual de fatura com cartão, valor e conta pagadora explicitamente informados;
- transferência entre contas da família;
- aplicação e resgate de investimento;
- compra parcelada com preservação do fato observado e projeção das parcelas futuras sem duplicar transações reais;
- cenário combinado de resgate do Privilège DI seguido de pagamento de fatura;
- compatibilidade com dados existentes e com a Central Inteligente/classificador atual;
- API e UI sem cálculo financeiro duplicado no frontend;
- household isolation e autorização;
- audit trail suficiente para criação e vínculos relevantes.

## Invariantes e regras obrigatórias

- **INV-001**: transferência interna não cria renda, despesa, consumo de teto ou resultado operacional.
- **INV-002**: pagamento de fatura é conciliação, nunca nova despesa.
- **INV-003**: aplicação é movimento patrimonial, não despesa.
- **INV-004**: resgate não é receita operacional.
- **INV-014/INV-015**: fatos duplicados permanecem preservados/auditáveis e não podem gerar dupla contagem.
- **INV-016**: estorno compensa gasto sem virar receita operacional comum.
- **INV-017**: compra de cartão mantém a competência canônica e a data original como linhagem.
- Fatos observados nunca podem ser duplicados por projeções de parcelas futuras.
- A origem de recursos nunca deve ser inferida automaticamente quando a regra exige confirmação humana.
- Ausência de evidência suficiente deve permanecer explícita; não fabricar vínculo, origem, competência ou saldo.

## Critérios de aceite

O PR só pode ser considerado concluído quando:

1. Todos os sete tipos manuais documentados podem ser representados por fluxos explícitos e compreensíveis na API/UI.
2. Pagamento manual de fatura não entra novamente como despesa e mantém vínculo/auditoria conforme o contrato existente.
3. Transferência, aplicação e resgate não afetam indevidamente renda/despesa/consumo.
4. Compra parcelada cria/projeta somente o que a documentação permite, sem duplicar fatos já observados.
5. O cenário `resgate do Privilège DI -> pagamento de fatura` preserva a distinção entre movimento patrimonial e conciliação.
6. Nenhum cálculo canônico é reimplementado no frontend ou em serviço paralelo.
7. Dados existentes continuam compatíveis; qualquer migration, se realmente necessária, deve ser aditiva, reversível e não destrutiva, com Alembic e teste de upgrade/downgrade.
8. Household isolation, autorização e auditoria são testados.
9. Nenhum dado financeiro real é auto-corrigido ou alterado silenciosamente.
10. Os 12 gates nomeados do CI passam no head final: `lint`, `unit`, `financial-invariants`, `property-tests`, `parser-reconciliation`, `projection-parity`, `snapshot-channel-consistency`, `advisor-contract-security`, `frontend-syntax`, `docker-build`, `alembic-migration`, `integration-postgres`.

## Testes obrigatórios mínimos

- criação manual de despesa e receita;
- transferência origem/destino com efeito operacional zero;
- aplicação e resgate com efeito patrimonial correto;
- estorno sem receita operacional artificial;
- pagamento manual de fatura sem dupla contabilização e com vínculo consistente;
- compra parcelada e projeção futura sem duplicar parcela já observada;
- fluxo combinado `redemption -> reconciliation`;
- household isolation e autorização;
- regressão de snapshot/report/projection para os fluxos acima;
- auditoria das ações relevantes;
- compatibilidade com dados existentes e migrations, se houver.

## Proibições

- Não criar segundo motor financeiro, segunda política de reconciliação ou segunda lógica de parcelas.
- Não inferir automaticamente conta de origem, pagamento de fatura ou vínculo financeiro quando a documentação exige ação humana.
- Não alterar regras/invariantes para acomodar a UI.
- Não reduzir cobertura nem enfraquecer asserts existentes.
- Não usar Claude/Codex/Advisor para decidir fatos determinísticos.
- Não fazer auto-fix destrutivo, migration destrutiva ou alteração silenciosa de dados financeiros reais.
- Não avançar para notificações, worker assíncrono, MFA, proxy HTTPS ou outros itens de Fase 3/4.

## Entrega esperada de Claude

Ao concluir, reportar no PR:

- arquitetura e fluxos implementados;
- endpoints/UI alterados;
- regras e invariantes exercitados;
- migrations/dependências, se houver;
- compatibilidade com dados existentes;
- testes adicionados/alterados;
- resultado dos 12 gates no head final;
- riscos e limitações residuais;
- qualquer Technical Challenge à orientação do engenheiro, sustentado por evidência.

**Não fazer merge.**