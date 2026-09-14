# Work Order — October Go-Live Slice 6: Patrimônio, Investimentos e Studio

**Parent:** P0 #87 — October Go-Live Rebaseline  
**Executor:** Claude  
**Integration authority:** engenheiro responsável  
**Priority:** P0; não iniciar Slice 7 antes de aprovação e merge deste slice.

## Fonte normativa

Ordem de precedência para semântica de produto:

1. `docs/OCTOBER_GO_LIVE_REBASELINE.md`, especialmente §§1, 13.1, 15, 16 e 17;
2. este Work Order;
3. `docs/FINANCIAL_RULES.md`;
4. `docs/FINANCIAL_INVARIANTS.md`;
5. `docs/ARCHITECTURE.md`;
6. `docs/INTELLIGENCE.md`;
7. `docs/ROADMAP.md` e Work Orders já integrados.

Em conflito de semântica, o rebaseline de outubro é a direção normativa alvo. Qualquer substituição do legado exige migration/backfill/documentação/testes explícitos quando aplicável.

## Objetivo

Entregar patrimônio e investimentos confiáveis para uso real, incluindo o Studio, sem dupla contagem entre custo histórico, valor atual, valor futuro, caixa e gasto.

O Dashboard deve permitir entender patrimônio/investimentos sem criar novo item obrigatório no menu principal.

## Ordem obrigatória de trabalho

1. Inventariar modelos, migrations, endpoints, UI, cálculos, relatórios e dados existentes relacionados a patrimônio/investimentos/ativos/Studio.
2. Mapear conflitos do legado contra o rebaseline §16 antes de alterar semântica.
3. Implementar o modelo mínimo e a UX necessária, reutilizando motores e contratos já integrados.
4. Integrar o Studio como ativo patrimonial/investimento, não como despesa simples.
5. Integrar o Assistente Financeiro aos fluxos patrimoniais usando typed actions/audit/undo do Slice 4; não criar segundo write path.
6. Provar cálculos, migrations/backfill, compatibilidade e preservação de dados reais com testes.
7. Executar CI completo e responder no PR com evidências e qualquer Technical Challenge fundamentado.

## Semântica obrigatória

### Modelo mínimo por investimento/ativo

Campos editáveis mínimos:

- nome;
- `valor_investido`: custo histórico/aportes acumulados;
- `valor_hoje`: valor atual estimado/confirmado;
- `valor_previsto_receber`: projeção futura;
- data da última atualização;
- data prevista de recebimento, opcional;
- observações.

Derivados canônicos:

- ganho/perda atual = `valor_hoje - valor_investido`;
- retorno atual %;
- ganho projetado = `valor_previsto_receber - valor_investido`;
- retorno projetado %.

Manter histórico suficiente de avaliações e aportes para auditoria.

### Patrimônio atual — invariante central

O patrimônio atual do ativo usa **somente `valor_hoje`**.

É proibido somar, para o mesmo ativo:

`valor_investido + valor_hoje + valor_previsto_receber`

Semântica:

- `valor_investido` = custo histórico;
- `valor_hoje` = posição patrimonial atual;
- `valor_previsto_receber` = projeção futura.

Valor previsto não aumenta patrimônio atual. Custo histórico não é somado novamente ao valor atual.

### Aportes

Aporte aumenta custo histórico/aportes do ativo, mas o movimento de caixa correspondente deve ser representado conforme a origem real.

Não transformar aporte automaticamente em despesa de consumo. Se a origem do recurso estiver ausente em comando do Assistente, perguntar antes de executar. Transferência interna/liquidação deve obedecer às regras de caixa já integradas.

### Studio

O Studio é ativo/investimento patrimonial.

Fluxos obrigatórios:

- atualizar `valor_hoje` altera patrimônio atual pelo novo valor do ativo, preservando histórico da avaliação;
- atualizar `valor_previsto_receber` altera apenas projeção futura, não patrimônio atual;
- registrar novo aporte aumenta custo histórico e registra o evento auditável, sem fabricar gasto econômico ou origem de caixa;
- histórico existente do Studio deve ser preservado/migrado de forma explícita se houver modelo legado.

Exemplos normativos do Assistente:

- `Hoje acho que o Studio vale 35 mil.` → proposta tipada para atualizar `valor_hoje`, mostrando impacto patrimonial antes da confirmação;
- `A previsão agora é receber 45 mil.` → atualiza `valor_previsto_receber`, sem alterar patrimônio atual;
- `Coloquei mais 5 mil no Studio.` → registra aporte e pergunta origem do recurso quando materialmente ausente.

## Dashboard e UX

Sem adicionar item principal obrigatório ao menu:

- mostrar patrimônio atual;
- mostrar investimentos/ativos relevantes e sua evolução;
- diferenciar claramente custo histórico, valor atual e valor futuro;
- permitir acesso à edição/detalhe a partir do Dashboard ou superfície contextual já normativa;
- não expor jargão interno de implementação ao usuário final.

Nenhum cálculo patrimonial deve ser duplicado no frontend quando puder vir de serviço/backend canônico.

## Relatórios/integração

Preparar/garantir contrato canônico para que Relatórios do Slice 7 consigam consumir:

- patrimônio atual e evolução;
- investimentos;
- histórico de avaliações/aportes;
- distinção entre valor atual e projeção futura.

Não antecipar a implementação ampla de Relatórios do Slice 7.

## Assistente Financeiro

Reutilizar obrigatoriamente o contrato integrado no Slice 4:

`interpret -> proposal_id -> execute`

Toda mutação patrimonial feita pelo Assistente exige:

- usuário;
- data/hora;
- mensagem original;
- interpretação estruturada;
- typed action;
- registros criados/alterados;
- before/after quando aplicável;
- `trace_id`;
- undo quando tecnicamente reversível;
- idempotência/single-use da proposta.

Não permitir typed action/payload livre vindo do cliente.

## Dados reais e migration

- Inventariar dados existentes antes de qualquer migration.
- Preservar fatos financeiros reais e histórico recuperável.
- Se schema legado misturar custo, valor atual ou valor previsto, criar migration/backfill explícito e fail-safe; não escolher silenciosamente qual campo representa verdade quando a evidência for ambígua.
- Migration deve ser segura no upgrade e no downgrade; se downgrade implicar perda de histórico não reconstruível, bloquear/falhar de forma explícita em vez de apagar silenciosamente.
- Não criar lançamentos sintéticos para fazer patrimônio ou caixa “bater”.

## Critérios de aceite

1. Um investimento consegue armazenar separadamente custo histórico, valor atual e valor previsto.
2. Patrimônio atual soma `valor_hoje` de ativos elegíveis uma única vez e nunca soma custo histórico + atual + previsto do mesmo ativo.
3. Atualizar somente valor previsto não altera patrimônio atual.
4. Atualizar valor atual altera patrimônio e preserva a avaliação anterior no histórico.
5. Aporte atualiza custo/aportes e possui tratamento de caixa/origem coerente, sem virar despesa de consumo por inferência.
6. Studio está representado como ativo/investimento e passa pelos mesmos invariantes.
7. Dashboard mostra patrimônio/investimentos com semântica clara e sem cálculo paralelo divergente no frontend.
8. Assistente suporta os fluxos patrimoniais necessários com desambiguação, audit/undo e idempotência já normativos.
9. Dados existentes continuam utilizáveis; migration/backfill, se houver, é testado em base vazia e cenário legado representativo.
10. CI aplicável integralmente verde.

## Testes obrigatórios

No mínimo:

- patrimônio atual considera somente `valor_hoje`;
- alteração de `valor_previsto_receber` não altera patrimônio atual;
- alteração de `valor_hoje` atualiza patrimônio e mantém histórico;
- aporte altera custo histórico sem dupla contagem patrimonial;
- aporte não é classificado automaticamente como gasto de consumo;
- Studio segue as mesmas regras;
- percentuais/ganhos tratam custo zero/nulo sem divisão inválida;
- Assistente pergunta origem quando aporte exige caixa e origem não está materialmente definida;
- ação patrimonial do Assistente registra before/after e pode ser desfeita quando reversível;
- retry/idempotência não duplica avaliação/aporte;
- household isolation/autorização;
- migration upgrade/downgrade/backfill quando houver alteração de schema;
- regressão de Dashboard garantindo ausência da soma proibida.

## Proibições

- Não somar custo histórico, valor atual e valor previsto como patrimônio.
- Não transformar investimento/aporte em consumo por conveniência contábil.
- Não fabricar aplicação/resgate, renda, despesa ou ajuste para reconciliar ativo.
- Não apagar histórico para representar estado atual.
- Não usar hipótese do Codex como fato silencioso.
- Não criar segundo motor patrimonial no frontend.
- Não iniciar Gastos & Economia/Relatórios do Slice 7 além dos contratos mínimos necessários.
- Não desviar prioridade para MFA, hardening, observabilidade ou parser não bloqueador.

## Riscos a revisar antes do merge

- dupla contagem do Studio entre transações, patrimônio e projeção;
- migração destrutiva de dados patrimoniais existentes;
- aporte reduzindo caixa e simultaneamente sendo contado como gasto econômico;
- valor previsto inflando patrimônio atual;
- edição destruindo histórico anterior;
- arredondamento/percentuais inconsistentes;
- household leakage;
- bypass do audit/undo do Assistente;
- drift de escopo para relatórios/UX fora do Slice 6.

## Definition of Done

O Slice 6 só pode ser integrado quando o patrimônio exibido for matematicamente e semanticamente confiável, o Studio estiver modelado como ativo, os dados existentes estiverem preservados, os fluxos do Assistente obedecerem aos contratos já integrados, os testes obrigatórios cobrirem os invariantes e o CI estiver verde.

**Claude não pode fazer merge.**