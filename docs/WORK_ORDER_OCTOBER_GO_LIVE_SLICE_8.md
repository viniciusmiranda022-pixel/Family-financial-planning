# Work Order — October Go-Live Slice 8

## Status
P0 #87 — último slice obrigatório antes do go-live.

Claude é o executor. O engenheiro responsável revisa, decide bloqueios e faz merge. Claude **não pode fazer merge** nem iniciar trabalho lateral fora deste Work Order.

## Fontes normativas
Ordem de precedência:
1. `docs/OCTOBER_GO_LIVE_REBASELINE.md`
2. este Work Order
3. `docs/FINANCIAL_INVARIANTS.md`
4. `docs/FINANCIAL_RULES.md`
5. `docs/ARCHITECTURE.md`
6. `docs/INTELLIGENCE.md`
7. `docs/ROADMAP.md`
8. README e documentação legada, somente quando não conflitar com o rebaseline.

## Objetivo
Fechar o P0 #87 com evidência operacional de que o sistema está confiável para uso real no próximo mês. Este slice não cria um novo produto; ele reconcilia dados reais, executa migrações/backfills necessários de forma auditável, valida ponta a ponta os fluxos cotidianos e produz o smoke de go-live.

## Escopo obrigatório

### 1. Inventário real e reconciliação
- identificar contas, cartões, obrigações, investimentos, Studio, saldos confirmados e fatos financeiros existentes relevantes ao go-live;
- comparar saldos confirmados com reconstruções derivadas no mesmo instante/contexto;
- preservar saldo confirmado como soberano;
- registrar divergências de forma explícita, com causa/evidência quando identificável;
- nunca mascarar diferença por ajuste sintético, resgate fabricado, aplicação fabricada, receita/despesa inventada ou deleção silenciosa;
- preservar dados reais já confirmados.

### 2. Migração/backfill
Quando semântica legada conflitar com o rebaseline:
- identificar exatamente quais registros/colunas/estados precisam ser migrados;
- implementar migration/backfill idempotente, auditável e reversível quando aplicável;
- documentar before/after esperado e risco;
- nunca reclassificar fato real por heurística não verificável;
- divergência ou ambiguidade material vira revisão explícita, não correção silenciosa.

### 3. Deduplicação fail-safe
Para importações/capturas/reprocessamentos usados no smoke:
- provável duplicidade deve ser sinalizada e vinculada ao registro existente;
- nunca apagar, fundir ou pular silenciosamente;
- preservar as opções humanas `Pular este lançamento`, `Importar mesmo assim`, `Ver lançamento existente` quando a superfície permitir decisão;
- reprocessamento não pode duplicar fatos previamente aceitos.

### 4. E2E financeiro obrigatório
Executar e provar os fluxos abaixo ponta a ponta, preferencialmente por testes automatizados e, quando necessário, smoke controlado sobre dados reais/sanitizados:

#### Caixa / Privilège
- entrada real;
- saída real com origem explícita;
- transferência interna Conta Corrente ↔ Privilège sem virar renda/despesa;
- saldo confirmado soberano;
- divergência reconstruído × confirmado mostrada, nunca corrigida por movimento sintético.

#### Cartão
- compra real entra em gasto e fatura, sem reduzir banco naquele instante;
- fechamento de fatura;
- pagamento integral como saída de caixa/reconciliation, nunca segunda despesa;
- pagamento parcial com principal carregado uma única vez;
- juros/IOF separados do principal e somente quando fato informado/confirmado;
- estorno neutraliza gasto sem apagar histórico;
- compra parcelada preserva valor contratado, impacto do ciclo e parcelas futuras comprometidas sem fabricar transações realizadas.

#### Obrigações
- obrigação futura como COMPROMETIDO;
- pagamento transforma compromisso em REALIZADO;
- pagamento antecipado remove compromisso futuro na data real;
- se já houver fato bancário correspondente, não duplicar despesa.

#### Estados financeiros
- REALIZADO, COMPROMETIDO e PREVISTO separados em Dashboard, Forecast e Relatórios;
- salário recorrente da Kelly pode aparecer como PREVISTO;
- comissão de Vinicius não aparece automaticamente como PREVISTA;
- competência financeira não pode ser confundida com `booked_at` quando o contrato exigir competência distinta.

#### Assistente Financeiro
- frase materialmente completa executa somente a ação tipada correta;
- frase ambígua pede informação crítica antes de agir;
- toda ação registra usuário, data/hora, mensagem original, interpretação, ação tipada, registros criados/alterados, before/after quando aplicável e undo;
- undo desfaz efeito operacional preservando trilha de auditoria;
- hipótese do Codex nunca vira fato sem base confirmada;
- divergência motor × Codex é sinalizada.

#### Patrimônio / Studio
- patrimônio atual = caixa canônico + valor atual dos ativos aplicáveis;
- não somar custo histórico + valor atual + valor previsto;
- aporte não vira consumo;
- aporte que consome caixa exige origem real;
- `valor investido`, `valor de hoje` e `valor previsto a receber` permanecem dimensões distintas.

#### Gastos & Economia / Relatórios
- pagamento de fatura, transferências internas e aportes não entram como gasto econômico;
- fluxo físico por conta permanece visível separadamente;
- Dashboard, Forecast, Relatórios e Gastos & Economia concordam para o mesmo fato/contexto;
- análises Codex mantêm `Seus dados` / `Referências externas` / `Análise` / `Recomendação` separados quando houver referência externa.

### 5. Smoke real de go-live
O smoke final deve cobrir, no mínimo:
1. login e navegação principal alvo;
2. lançamento manual de entrada;
3. lançamento manual de saída;
4. lançamento pelo Assistente com ação tipada;
5. caso ambíguo no Assistente exigindo pergunta;
6. criação/consulta de obrigação;
7. compra em cartão e consulta da fatura;
8. pagamento de fatura sem duplicar gasto;
9. transferência interna Conta Corrente ↔ Privilège;
10. saldo confirmado e reconciliação;
11. patrimônio/Studio;
12. Gastos & Economia e Relatórios;
13. undo de ação do Assistente;
14. cenário de provável duplicidade sem decisão destrutiva automática.

Para cada passo registrar: entrada, ação, resultado esperado, resultado observado e evidência. Falha material bloqueia go-live.

## Parser Nubank / PR #57
O parser Nubank permanece fora de prioridade **a menos que este Slice 8 demonstre com evidência que ele bloqueia a reconciliação/importação real necessária ao go-live**.

Antes de tocar #57:
- testar o caminho atual do `main` contra a necessidade real do smoke/reconciliação;
- se não bloquear, deixar #57 pausado;
- se bloquear, registrar a evidência no PR #96 e então reavaliar o código já existente no #57, evitando implementação duplicada;
- qualquer reprocessamento de documento real deve ser auditável e não pode alterar fatos silenciosamente.

## Proibições
- não criar novo motor financeiro paralelo;
- não fabricar resgate/aplicação para fechar saldo;
- não transformar pagamento de fatura em gasto;
- não transformar transferência interna em renda/despesa;
- não misturar REALIZADO/COMPROMETIDO/PREVISTO;
- não prever comissão automaticamente;
- não sobrescrever saldo confirmado com reconstrução;
- não corrigir dados reais silenciosamente;
- não introduzir MFA, hardening, observabilidade ou features laterais neste slice;
- não fechar P0 #87 apenas por CI verde sem smoke real documentado.

## Testes e gates obrigatórios
- suíte completa `pytest`;
- `ruff check .`;
- frontend syntax;
- todos os 12 jobs de GitHub Actions executados e verdes no head final;
- testes E2E/integrados específicos para os fluxos acima;
- testes de household isolation;
- testes de idempotência de migration/backfill/reprocessamento quando aplicável;
- testes de não dupla contagem;
- testes de reconciliação com saldo confirmado divergente do reconstruído;
- testes de Assistente audit/undo;
- smoke final documentado.

## Entregáveis
- código/migrations estritamente necessários;
- testes automatizados;
- evidência de reconciliação/migração;
- documento de smoke/go-live com resultados observados;
- atualização de arquitetura/regras apenas quando necessária para refletir a implementação alvo;
- comentário final no PR com: diagnóstico, arquivos alterados, migrations/backfills, dados preservados, divergências encontradas, testes, 12 gates, smoke, riscos residuais e Technical Challenges.

## Critérios de aceite / Definition of Done
Só considerar o Slice 8 concluído quando:
- fatos financeiros reais estão preservados;
- saldos confirmados vencem reconstruções no instante observado e divergências ficam visíveis;
- não existe dupla contagem conhecida nos fluxos de caixa, cartão, obrigações, patrimônio ou relatórios;
- nenhum resgate/aplicação sintético é usado para fechar déficit/sobra;
- todos os fluxos cotidianos listados acima passam E2E;
- Assistente respeita desambiguação, typed actions, auditoria e undo;
- Dashboard/Forecast/Relatórios são semanticamente coerentes;
- migration/backfill/reprocessamento, se houver, é seguro, idempotente e documentado;
- todos os 12 gates estão verdes no head final;
- smoke real de go-live está documentado e sem blocker material;
- não há Technical Challenge ou divergência não resolvida.

Após aprovação e merge deste slice, o engenheiro responsável pode declarar o P0 #87 concluído. Não iniciar novos slices fora do roadmap como continuação automática.