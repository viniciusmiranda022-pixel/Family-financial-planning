# Family Finance AI Assistant via WhatsApp

## 1. Objetivo

Transformar o WhatsApp em uma interface conversacional oficial do Family Finance para três classes de uso:

1. **WRITE** — criar/alterar lançamentos em linguagem natural, sempre por draft + confirmação humana;
2. **QUERY** — responder perguntas financeiras livres sobre os dados do household;
3. **REASON/ADVISOR** — comparar períodos, diagnosticar mudanças e simular cenários usando números calculados pelo backend.

A experiência **não pode depender de catálogo estático de perguntas ou comandos**. O usuário deve poder formular perguntas novas, combinar filtros e continuar uma conversa sem exigir mudança de código para cada frase.

Exemplos de linguagem esperada:

- `gastei 30 de combustível`;
- `gastei 230 no posto agora, foi no Nubank`;
- `quanto gastei de combustível esse mês?`;
- `quanto eu gastei no mercado nos últimos 3 meses?`;
- `qual foi minha renda no último ano?`;
- `compara meus gastos de posto dos últimos 90 dias com os 90 dias anteriores`;
- `quais categorias aumentaram mais de 20% nos últimos três meses?`;
- `por que meu dinheiro está acabando mais rápido?`;
- `e mês passado?`;
- `qual a diferença?`;
- `e se continuar nessa média até o fim do mês?`.

## 2. Princípio arquitetural

> **LLM entende e orquestra. O Family Finance calcula, valida e persiste.**

O LLM não é fonte de verdade financeira e não recebe acesso SQL livre, conexão direta ao PostgreSQL ou autoridade para alterar fatos.

Arquitetura alvo:

```text
WhatsApp Business / Cloud API
            |
            v
   WhatsApp Gateway
            |
            v
 Conversation Orchestrator
        LLM / tools
            |
      +-----+------+----------------+
      |            |                |
      v            v                v
 Write Tools   Query Tools     Advisor Tools
      |            |                |
      +------------+----------------+
                   |
                   v
        Family Finance Services
                   |
                   v
        Finance Engine / PostgreSQL
```

O gateway de WhatsApp é apenas um canal. A camada `Family Finance AI Assistant` deve poder ser reutilizada futuramente pela interface web/PWA sem duplicar regras.

## 3. Papel do Claude

Claude é o agente de implementação dos slices desta iniciativa e deve seguir o Work Order de cada slice.

Claude **não deve** implementar a funcionalidade por:

- `if pergunta == ...`;
- dicionário fechado de frases;
- regex como motor principal de intenção;
- endpoint diferente para cada pergunta;
- SQL construído pelo modelo;
- envio de todas as transações ao LLM para que ele faça contas;
- novo motor financeiro paralelo ao backend existente.

O runtime do assistente deve ser desacoplado do provedor de LLM. Se for necessário usar uma API externa paga específica, isso exige decisão explícita do engenheiro responsável e deve respeitar o gate de custo.

## 4. Modelo de execução dinâmica

A pergunta do usuário é interpretada em termos de **objetivo + entidades + métricas + período + dimensões + filtros + ação**.

Exemplo:

```text
Usuário: "Quanto gastei no mercado nos últimos 3 meses?"

LLM entende:
- objetivo: aggregate expenses
- categoria semântica: Mercado
- período: últimos 3 meses
- agrupamento desejável: mês
- métricas: total + média

LLM chama tool genérica.
Backend resolve categoria, período e valores.
LLM formata a resposta.
```

Uma pergunta diferente pode ser resolvida por composição das mesmas tools:

```text
"Quais categorias aumentaram mais de 20% nos últimos 3 meses?"

1. aggregate expenses por categoria no período atual
2. aggregate expenses por categoria no período anterior
3. compare periods
4. ordenar variação
5. filtrar > 20%
6. responder
```

Nenhuma frase precisa existir previamente no código.

## 5. Tool layer

A implementação final pode ajustar nomes, mas deve preservar a semântica genérica. Referência inicial:

### Leitura e analytics

```text
resolve_financial_entities(...)
search_transactions(...)
financial_aggregate(...)
compare_periods(...)
get_income(...)
get_expenses(...)
get_balances(...)
get_commitments(...)
get_installments(...)
get_cashflow(...)
forecast_cashflow(...)
```

`financial_aggregate` deve aceitar combinações controladas de:

- metric;
- filters;
- dimensions/group_by;
- period;
- comparison;
- sort;
- limit.

O contrato deve ser fortemente validado. Nenhuma tool recebe SQL, expressão ORM arbitrária ou nome de tabela bruto vindo do modelo.

### Escrita

```text
create_transaction_draft(...)
update_transaction_draft(...)
preview_financial_action(...)
confirm_financial_action(...)
cancel_financial_action(...)
```

A tool de confirmação deve validar novamente todas as pré-condições antes do commit.

## 6. WRITE — lançamentos em linguagem natural

Exemplo:

```text
Usuário: "Gastei 30 de combustível"

Assistente:
Despesa: Combustível
Valor: R$ 30,00
Data: hoje
Origem: não informada

Pergunta somente o campo material ausente:
"Como você pagou?"
```

Após a origem ser escolhida, o assistente apresenta a prévia e exige confirmação explícita.

Se a mensagem já trouxer a origem:

```text
"Gastei 30 de combustível no Nubank"
```

não deve perguntar novamente caso `Nubank` seja resolvido inequivocamente no household.

### Operações suportadas

- despesa;
- receita;
- transferência;
- aplicação;
- resgate;
- reembolso/estorno;
- pagamento de fatura;
- compra parcelada;
- alteração controlada;
- exclusão controlada.

Alterações e exclusões exigem identificação inequívoca + confirmação específica.

## 7. QUERY — perguntas livres

O sistema deve suportar perguntas sem roteiro fixo, incluindo combinações de:

- período absoluto e relativo;
- categoria;
- estabelecimento;
- conta;
- cartão;
- titular;
- receita/despesa;
- recorrência;
- realizado/previsto;
- agrupamento;
- comparação;
- ranking;
- média;
- variação percentual;
- concentração.

Exemplos de aceite:

- `quanto gastei de combustível esse mês?`;
- `quanto gastei de mercado nos últimos 3 meses?`;
- `qual foi minha renda no último ano?`;
- `qual foi o mês em que mais gastei este ano?`;
- `quais foram minhas cinco maiores despesas deste mês?`;
- `quanto tenho comprometido para outubro?`;
- `quanto ainda vou pagar de cartão até dezembro?`;
- `quanto do que entrou este ano veio de comissão?`.

O LLM não soma valores. Ele requisita agregações ao backend e recebe resultados exatos.

## 8. Contexto conversacional

O assistente deve suportar follow-ups:

```text
U: Quanto gastei de combustível esse mês?
A: ...
U: E mês passado?
A: ...
U: Qual a diferença?
A: ...
U: E se continuar nessa média até o fim do mês?
A: ...
```

O contexto deve guardar apenas estado mínimo estruturado, por exemplo:

```text
category = combustivel
metric = expense_total
current_period = current_month
comparison_period = previous_month
```

Nunca persistir cadeia de raciocínio. Contexto não concede autorização, não muda household e não confirma ação financeira.

## 9. Advisor / Reason

Perguntas abertas podem exigir investigação de múltiplos dados.

Exemplo:

```text
"Por que meu dinheiro está acabando mais rápido nos últimos meses?"
```

O LLM pode decidir consultar:

- renda por mês;
- despesas por mês;
- categorias que mais cresceram;
- parcelas;
- compromissos;
- faturas;
- saldo operacional;
- resgates/aplicações separadamente.

O backend fornece os números. O LLM organiza a análise e explica as conclusões.

Cenários hipotéticos como `posso gastar R$ 5.000 agora?` devem reutilizar o motor canônico de projeção e distinguir claramente:

- realizado;
- previsto;
- hipótese;
- recomendação.

Hipótese nunca é persistida como fato sem passar pelo fluxo WRITE e confirmação.

## 10. Regras financeiras obrigatórias

A integração não pode mudar os invariants já definidos.

Em especial:

- pagamento de fatura não é nova despesa;
- transferências internas não são receita/despesa;
- aplicação não é despesa operacional;
- resgate do Privilège DI não é renda;
- Privilège DI permanece conta central de liquidez conforme regra canônica;
- compra parcelada não gera gasto integral realizado antecipadamente; parcelas futuras permanecem compromissos até sua competência;
- estornos/reembolsos seguem o motor canônico;
- projeção não duplica fato já observado;
- nenhuma origem de recurso é inferida quando a ambiguidade altera o efeito financeiro.

## 11. Datas e timezone

Toda interpretação de datas relativas deve ser resolvida de forma determinística usando o timezone do household, com default atual `America/Sao_Paulo`.

Termos como:

- hoje;
- ontem;
- esse mês;
- mês passado;
- últimos 3 meses;
- últimos 90 dias;
- este ano;
- ano passado;
- últimos 12 meses;

devem virar intervalos absolutos antes da consulta financeira.

## 12. WhatsApp Gateway

O canal deve:

- receber mensagens por webhook autenticado/verificado;
- normalizar eventos;
- deduplicar reentregas;
- mapear número autorizado para usuário + household;
- negar acesso fail-closed para números desconhecidos;
- não armazenar token/secret na UI ou no Git;
- manter retries e timeouts controlados;
- não bloquear o Family Finance web em caso de indisponibilidade externa.

## 13. Segurança e privacidade

Requisitos mínimos:

- allowlist de números;
- household isolation em todas as tools;
- RBAC vigente respeitado;
- escrita somente com confirmação humana;
- ações destrutivas em dois passos;
- nenhuma credencial bancária;
- secrets via ambiente/secret store;
- defesa contra prompt injection;
- allowlist de tools;
- rate limiting;
- proteção contra replay;
- logs sanitizados;
- auditoria por ids técnicos sempre que possível;
- retenção mínima de conteúdo conversacional.

## 14. Mídia

Após o fluxo textual estar estabilizado, o WhatsApp poderá receber:

- áudio;
- foto de comprovante;
- boleto;
- fatura;
- extrato/documento.

A integração deve reutilizar OCR/transcrição/capture pipeline já existentes. Não criar um segundo pipeline só para WhatsApp.

Todo conteúdo extraído continua passando por prévia e confirmação antes de gerar fato financeiro.

## 15. Custo

O Family Finance mantém objetivo de custo recorrente **R$ 0,00**.

Antes de qualquer integração de produção, WA-00 deve verificar os custos, franquias e condições **vigentes no momento da execução** para:

- WhatsApp Business/Cloud API;
- número/canal necessário;
- runtime LLM;
- infraestrutura adicional;
- observabilidade/mensageria adicional, se proposta.

Se houver custo recorrente, dependência de trial credits, Pay As You Go ou serviço pago não aprovado, a execução deve parar e abrir **Technical Challenge**. Não substituir silenciosamente por solução paga.

## 16. Sequência de slices

Epic: **#71 — Family Finance AI Assistant via WhatsApp**.

1. **#72 — WA-00:** discovery, ADR, custos e contratos;
2. **#73 — WA-01:** gateway WhatsApp, webhook e autorização de números;
3. **#74 — WA-02:** orquestrador LLM tool-driven e camada semântica genérica;
4. **#75 — WA-03:** WRITE com drafts, confirmação e idempotência;
5. **#76 — WA-04:** QUERY/analytics genérico para perguntas livres;
6. **#77 — WA-05:** contexto conversacional e follow-ups;
7. **#78 — WA-06:** Advisor, comparações e projeções;
8. **#79 — WA-07:** mídia, hardening, observabilidade e E2E.

Cada slice depende do anterior.

## 17. Governança de execução

Este plano está registrado no `main` como evolução futura e **não autoriza execução paralela**.

Quando a EPIC chegar à fila ativa, Claude deve:

1. reler integralmente a `main` vigente;
2. conferir `README.md`, `docs/NEXT_STEPS.md`, `docs/ROADMAP.md`, `docs/ARCHITECTURE.md`, `docs/FINANCIAL_RULES.md`, `docs/FINANCIAL_INVARIANTS.md`, `docs/INTELLIGENCE.md` e este documento;
3. respeitar todos os PRs/slices normativos anteriores ainda pendentes;
4. criar Work Order específico para o slice ativo;
5. criar branch isolada;
6. implementar somente o escopo daquele slice;
7. abrir Draft PR;
8. executar revisão, testes e gates;
9. nunca fazer merge por conta própria.

Mudança arquitetural fundamentada deve ser registrada como Technical Challenge, não aplicada silenciosamente.
