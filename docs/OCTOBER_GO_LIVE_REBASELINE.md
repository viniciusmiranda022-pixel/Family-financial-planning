# October Go-Live Rebaseline — Family Finance

**Status:** normativa de produto para o próximo go-live.

**Prioridade:** P0. Enquanto este rebaseline não estiver implementado e validado, nenhum slice de conveniência, hardening, MFA, parser não necessário ao go-live, observabilidade ou evolução lateral deve consumir a prioridade principal do projeto.

Este documento registra o comportamento financeiro e de produto esperado pelo usuário real. Ele existe porque o modelo anterior misturou resultado operacional, liquidez, projeção e movimentos bancários de uma forma que não corresponde ao uso diário da família.

> Princípio central: o sistema deve refletir fatos financeiros reais, separar realizado/comprometido/previsto, impedir dupla contagem e usar o Codex como camada inteligente de interpretação, validação e operação assistida — sem inventar fatos financeiros.

---

## 1. Meta do go-live

O Family Finance deve estar pronto para uso cotidiano no início do próximo mês, com os fluxos abaixo funcionando ponta a ponta:

1. registrar entradas e saídas manualmente ou pelo Assistente Financeiro;
2. manter saldos coerentes das contas e do Privilège DI;
3. controlar compras, ciclos, fechamento e pagamento de cartões sem dupla contagem;
4. controlar contas a pagar, parcelas futuras e obrigações;
5. mostrar gasto realizado, compromissos e projeção futura de forma separada;
6. mostrar patrimônio e investimentos, incluindo o Studio;
7. permitir análise, validação e simulação pelo Codex sobre dados reais do sistema;
8. manter trilha de auditoria e possibilidade de desfazer ações do Assistente;
9. reconciliar saldos confirmados e sinalizar divergências em vez de mascará-las.

O critério de sucesso não é “mais funcionalidades”. É o usuário conseguir confiar no Dashboard, lançar o dia a dia e tomar decisões sem precisar compreender tipos internos do banco de dados.

---

## 2. Navegação alvo

A navegação principal deve ser exatamente:

```text
🏠 Dashboard Financeiro
↓  Entradas
↑  Saídas
▤  Contas a pagar
✦  Gastos & Economia
▥  Relatórios
✦  Assistente Financeiro

────────────

👥 Acessos
⚙  Configurações
```

Não existe botão global `+ Lançar` como item principal.

- **Entradas** contém seus próprios fluxos de nova entrada.
- **Saídas** contém seus próprios fluxos de nova saída.
- **Contas a pagar** contém cadastro, conferência e pagamento de compromissos/faturas.
- **Assistente Financeiro** é o atalho inteligente universal por linguagem natural.

Abas antigas devem ser absorvidas:

- `Rendas` → **Entradas**;
- `Transferências` → contexto de Entradas/Saídas/Assistente;
- `Importações` → **Configurações > Dados e importações** e ações contextuais;
- `Lançamentos` → **Relatórios** como livro-razão/auditoria;
- `Revisar` → alertas contextuais e Dashboard, não navegação permanente;
- `Planejamento` → **Gastos & Economia** e projeção do Dashboard;
- `Consultor` → **Assistente Financeiro**;
- `Lançar agora` → removido como navegação separada; captura rápida passa pelo Assistente ou pelo contexto Entrada/Saída.

---

## 3. Estados financeiros obrigatórios

Todo fato/projeção relevante deve ser distinguido em três estados sem ambiguidade:

### REALIZADO
Aconteceu de verdade.

Exemplos:
- PIX efetivamente pago;
- compra real no cartão;
- salário efetivamente creditado;
- parcela da chácara já paga.

### COMPROMETIDO
Já existe obrigação/contrato, mas ainda será liquidado.

Exemplos:
- parcela futura de compra parcelada;
- parcela futura da chácara;
- fatura fechada ainda não paga;
- saldo financiado de fatura parcial.

### PREVISTO
É estimativa futura, não obrigação.

Exemplos:
- estimativa de combustível do próximo mês;
- salário recorrente futuro da Kelly antes do crédito real.

Nenhum PREVISTO pode ser tratado como fato realizado. Nenhum COMPROMETIDO pode fabricar uma transação bancária futura.

---

## 4. Caixa operacional e Privilège DI

### 4.1 Semântica real

O Privilège DI funciona no cotidiano como **caixa principal remunerado** da família. O dinheiro permanece ali para render e é resgatado quando precisa ser usado.

A Conta Corrente Itaú funciona principalmente como **conta transacional/ponte** para PIX, boleto, débito automático, pagamento de fatura e outras liquidações.

Fluxo cotidiano comum:

```text
Privilège DI -> resgate -> Conta Corrente -> pagamento
```

Quando Vinicius ou Kelly recebe dinheiro na Conta Corrente, um pagamento próximo ao recebimento pode sair diretamente do saldo da Conta Corrente. O excedente pode depois ser aplicado no Privilège.

### 4.2 Invariante

Aplicação e resgate entre Conta Corrente e Privilège são **transferências internas de liquidez**.

Nunca são:
- renda;
- despesa;
- consumo;
- déficit;
- superávit inventado.

### 4.3 Regra que deve ser removida

O sistema **não pode** inferir que um resultado operacional negativo implica automaticamente retirada do Privilège.

É proibido o comportamento:

```text
resultado_operacional < 0 -> fabricar liquidity_withdrawal
```

Resgates reais vêm de:
- movimento bancário observado/importado; ou
- ação explicitamente confirmada pelo usuário/Assistente.

### 4.4 Origem do dinheiro

Ao registrar uma saída que exige caixa, se a origem não estiver explícita, o Assistente deve perguntar antes de executar:

> O valor já estava na Conta Corrente ou você resgatou do Privilège?

Se o usuário confirmar resgate:

```text
Privilège        - valor
Conta Corrente   + valor
Pagamento        - valor
```

O gasto econômico é contado uma vez. A transferência interna não é gasto.

Se houver resgate real do mesmo valor/data próximo do pagamento, o Assistente pode sugerir vínculo, mas não deve inventar ou forçar a associação.

---

## 5. Saldos e reconciliação

### 5.1 Saldo confirmado é soberano no instante observado

Se o banco/instituição informar um saldo confirmado em uma data/hora, esse valor é a posição real naquele instante.

Exemplo:

```text
Privilège DI em 11/09/2026 = R$ 40.667,49
```

O sistema pode reconstruir movimentos anteriores para auditoria, mas não pode substituir esse saldo por cálculo derivado.

Se o reconstruído divergir:

```text
Saldo confirmado       R$ X
Saldo reconstruído     R$ Y
Diferença              R$ Z
Status                 DIVERGENTE
```

A divergência deve ser investigada; nunca mascarada.

### 5.2 Atualização após fatos confirmados

Uma ação confirmada atualiza imediatamente a posição da conta afetada no modelo do sistema.

A UI deve distinguir:
- saldo observado/confirmado;
- movimentos posteriores ao último saldo observado;
- saldo corrente derivado desde a observação;
- eventual divergência de reconciliação.

---

## 6. Cartões — ciclo completo

### 6.1 Compra no cartão

Ao realizar compra no cartão:
- entra em **Gastos do mês**;
- entra na fatura aberta correspondente ao ciclo;
- não reduz o saldo bancário naquele momento;
- não cria pagamento bancário antecipado.

### 6.2 Fechamento

Na data de fechamento:
- a fatura muda de `aberta` para `fechada`;
- o total é consolidado;
- o vencimento é exibido;
- aparece a ação **Conferir e pagar**.

### 6.3 Pagamento

Ao clicar em **Pagar/Conferir e pagar**, abrir modal com:
- cartão/fatura/competência;
- total calculado pelo sistema;
- valor a pagar editável;
- conta/origem do recurso;
- data do pagamento;
- observação opcional;
- divergências existentes, se houver.

Só após confirmação ocorre saída bancária.

**Pagamento de fatura nunca é novo gasto.** É liquidação de obrigação já originada pelas compras.

Se a origem for Privilège, o backend deve representar corretamente o resgate interno e a liquidação, mesmo que a UX apresente uma única ação ao usuário.

### 6.4 Divergência de fatura

Se o total do sistema divergir do total bancário, o Assistente deve procurar:
- IOF;
- juros;
- compra ausente;
- estorno/crédito;
- duplicidade;
- ajuste/encargo.

Não ajustar silenciosamente.

Se não encontrar causa:

```text
FATURA NÃO RECONCILIADA
Total banco:       R$ X
Total sistema:     R$ Y
Diferença:         R$ Z
```

O usuário revisa antes de consolidar a diferença.

### 6.5 Pagamento parcial

Exemplo:

```text
Fatura fechada:        R$ 3.000
Pago:                  R$ 2.000
Saldo não pago:        R$ 1.000
Status:                parcialmente paga
```

O saldo restante é carregado ao próximo ciclo como **principal financiado**, não como nova compra e não como novo gasto.

Juros e IOF posteriores são despesas novas, registradas separadamente quando efetivamente informadas pela fatura/documento ou confirmadas pelo usuário. O sistema não fabrica encargos como fato real.

### 6.6 Estorno/chargeback

O registro original não é apagado.

Visualmente pode aparecer riscado e marcado `Estornada`.

O estorno:
- neutraliza economicamente a compra original;
- fica ligado à compra original;
- pode cair na mesma fatura, na posterior ou após a fatura original já ter sido paga;
- se cair em fatura posterior, funciona como crédito nessa fatura sem apagar a história da compra.

O usuário pode informar o estorno pelo Assistente; o Assistente localiza o lançamento e pede confirmação quando houver ambiguidade.

### 6.7 Compra parcelada

Exibição obrigatória:

```text
Compra contratada:        R$ 5.000
Impacto neste mês:        R$   500
Parcelas futuras:         R$ 4.500
Quantidade de parcelas:   10x
```

- a compra contratada preserva o valor total;
- somente a parcela do ciclo impacta a fatura daquele mês;
- parcelas futuras são **COMPROMETIDO**;
- não são criadas como transações realizadas antecipadamente.

---

## 7. Contas a pagar e obrigações

A área deve mostrar separadamente:
- vencendo;
- faturas fechadas;
- parcelas;
- recorrências;
- atrasadas;
- pagas recentemente.

Ao pagar uma obrigação:
- transforma compromisso em REALIZADO;
- vincula a transação real correspondente;
- remove o valor da projeção futura daquele compromisso;
- não cria despesa duplicada se já houver fato bancário;
- pergunta origem do recurso quando necessário.

Pagamento antecipado é permitido: a obrigação passa a paga na data real do pagamento e deixa de ser compromisso futuro.

---

## 8. Entradas e receitas futuras

### 8.1 Página Entradas

A página possui `+ Nova entrada` e tipos dinâmicos, por exemplo:
- Salário / Holerite;
- Comissão;
- Reembolso;
- Recebimento;
- Transferência entre minhas contas;
- tipos aprendidos;
- Outra entrada.

### 8.2 Outra entrada e aprendizado persistente

Ao escolher `Outra entrada`, abrir campo livre para o usuário descrever o novo tipo.

O Codex deve avaliar se é:
- um novo tipo reutilizável;
- sinônimo de tipo já existente;
- categoria/descrição, e não novo tipo.

Após confirmação, um novo tipo/template pode ficar persistente para próximos lançamentos.

Evitar duplicidades semânticas como `Aluguel`, `Recebi aluguel`, `Aluguel recebido`.

### 8.3 Comissões

**Comissões nunca entram como receita PREVISTA automaticamente.**

No uso atual:
- comissão só passa a existir na visão financeira quando efetivamente registrada/recebida;
- não deve inflar projeções futuras por expectativa.

### 8.4 Salário da Kelly

O salário recorrente da Kelly pode participar da projeção futura como PREVISTO.

Quando o crédito real chegar:

```text
PREVISTO -> REALIZADO
```

A conciliação não pode criar uma segunda receita.

Outras receitas só entram na projeção se forem explicitamente configuradas como recorrentes/previsíveis.

---

## 9. Saídas e tipos dinâmicos

A página Saídas possui `+ Nova saída` e contexto próprio.

Tipos universais podem incluir:
- Compra;
- Conta/boleto;
- PIX para terceiro;
- Despesa recorrente;
- Pagamento de fatura;
- Transferência entre minhas contas;
- Outra saída.

`Outra saída` segue o mesmo aprendizado persistente de Entradas.

O Codex deve distinguir **tipo**, **categoria**, **favorecido** e **descrição** para evitar transformar cada fornecedor ou finalidade em um novo tipo.

---

## 10. Antiduplicidade

O sistema não deve excluir/fundir automaticamente um fato apenas porque parece duplicado.

Ao importar/registrar algo semelhante a lançamento existente, mostrar alerta como:

> Já existe um lançamento desse valor para essa categoria nesta data.

Ações:
- **Pular este lançamento**;
- **Importar mesmo assim**;
- **Ver lançamento existente**.

Depois da decisão, continuar para o próximo item.

O Codex pode classificar como `provável duplicidade` e explicar a evidência, mas a decisão destrutiva/final é humana.

Duplicidades legítimas são possíveis e devem ser preserváveis.

---

## 11. Assistente Financeiro — interface operacional inteligente

O Assistente Financeiro não é apenas um chat de explicação. É uma camada de interação operacional sobre o sistema.

Exemplos de comandos:

- `Gastei 300 reais de combustível no cartão Nubank.`
- `Paguei 300 de combustível por PIX.`
- `Recebi uma comissão de 8.500.`
- `Paguei a parcela da chácara.`
- `A fatura do Nubank fechou em 2.340; confere.`
- `Quanto gastei de combustível nos últimos três meses?`
- `Posso comprar uma TV de 4.500?`
- `Atualize o Studio: hoje ele vale 35 mil.`

### 11.1 Política de perguntas

Se todos os fatos críticos estiverem presentes e não houver ambiguidade material, o Assistente pode preparar/executar a ação tipada prevista pelo backend.

Se faltar dado material, deve perguntar apenas o necessário.

Exemplo:

```text
Usuário: Gastei R$ 300.
Assistente: Foi em qual meio/conta/cartão e com o quê?
```

Exemplo com caixa:

```text
Usuário: Paguei R$ 300 por PIX.
Assistente: O valor já estava na Conta Corrente ou você resgatou do Privilège?
```

### 11.2 Authority boundary

O Codex pode:
- interpretar linguagem natural;
- classificar intenção;
- sugerir tipo/categoria;
- consultar fatos sanitizados expostos pelo backend;
- montar plano de ação tipado;
- recalcular/validar totais;
- identificar dupla contagem/inconsistência;
- pesquisar referências externas quando necessário;
- simular decisões;
- aprender a partir de confirmações persistidas.

O Codex não pode:
- inventar saldo, transação, juros ou data;
- alterar silenciosamente valor factual;
- executar SQL arbitrário;
- apagar/fundir fato por conta própria;
- executar operação financeira externa em banco.

O backend é a autoridade de escrita e cálculo determinístico. O Assistente solicita ações tipadas e recebe de volta o resultado real persistido.

### 11.3 Auditoria e undo

Toda ação do Assistente deve guardar:
- usuário;
- data/hora;
- mensagem original;
- interpretação estruturada do Codex;
- perguntas/respostas de desambiguação relevantes;
- ação tipada executada;
- IDs dos registros criados/alterados;
- estado before/after quando houver alteração;
- `trace_id`;
- possibilidade de desfazer quando tecnicamente reversível.

---

## 12. Validação Codex + motor determinístico

Para valores e fechamentos importantes, adotar dupla validação:

```text
fatos reais
   ↓
cálculo determinístico do backend
   ↓
validação/recalculo semântico independente do Codex
   ↓
concordância -> publicar como confiável
inconsistência -> sinalizar/revisar
```

O Codex pode validar contas, procurar dupla contagem e explicar divergência.

Ele não substitui a evidência bancária nem transforma sua própria hipótese em fato.

---

## 13. Dashboard Financeiro

O Dashboard deve responder, sem jargão técnico:

- quanto tenho hoje;
- quanto já gastei este mês;
- quanto entrou;
- o que ainda tenho a pagar;
- situação das faturas;
- compromissos futuros;
- projeção de 30/60/90 dias;
- patrimônio atual;
- investimentos;
- itens que precisam de atenção.

Não expor ao usuário final termos internos como `canonical`, `snapshot`, `funding_source`, `reconciliation` ou nomes de invariantes, salvo em tela técnica de auditoria.

### 13.1 Separação obrigatória

Nunca misturar:

1. **Gasto/consumo** — o que foi economicamente gasto;
2. **Movimento de caixa** — o que entrou/saiu de uma conta específica;
3. **Transferência interna** — dinheiro que mudou entre contas próprias;
4. **Patrimônio** — valor líquido atual dos ativos/caixa;
5. **Compromissos** — valores já contratados ainda não liquidados;
6. **Previsões** — estimativas futuras.

---

## 14. Gastos & Economia

A página deve mostrar:
- gastos do mês;
- categorias;
- origem por conta/cartão;
- comparação histórica;
- tendências;
- oportunidades de economia;
- recomendações do Codex baseadas nos dados reais da família.

Quando houver pesquisa externa, a resposta deve separar claramente:

1. **Seus dados**;
2. **Referências externas**;
3. **Análise**;
4. **Recomendação**.

A recomendação externa nunca deve substituir fatos internos.

---

## 15. Relatórios

Relatórios devem conter, conforme o contexto:
- livro-razão completo;
- fluxo de caixa por conta;
- gastos/consumo por competência;
- cartões e faturas;
- transferências internas;
- patrimônio e evolução;
- investimentos;
- obrigações;
- realizado x comprometido x previsto;
- auditoria e reconciliações relevantes.

A mesma movimentação pode aparecer em visões diferentes, mas nunca ser somada duas vezes na mesma métrica.

---

## 16. Patrimônio e investimentos

Investimentos/ativos devem ser visíveis a partir do Dashboard, sem criar item adicional obrigatório no menu principal.

### 16.1 Modelo mínimo por investimento

Campos editáveis:
- nome;
- **Valor investido** (custo histórico/aportes acumulados);
- **Valor de hoje** (valor atual estimado/confirmado);
- **Valor previsto a receber**;
- data da última atualização;
- data prevista de recebimento, opcional;
- observações.

Derivados:
- ganho/perda atual = valor de hoje - valor investido;
- retorno atual %;
- ganho projetado = valor previsto a receber - valor investido;
- retorno projetado %.

Manter histórico de avaliações/aportes.

### 16.2 Regra patrimonial

O patrimônio atual usa **somente o Valor de hoje** do ativo.

Não somar:

```text
valor investido + valor de hoje + valor previsto a receber
```

Semântica:

```text
Valor investido          = custo histórico
Valor de hoje            = patrimônio atual
Valor previsto a receber = projeção futura
```

### 16.3 Studio

O Studio é investimento/ativo patrimonial, não despesa simples.

Exemplos do Assistente:

- `Hoje acho que o Studio vale 35 mil.` -> propõe atualizar Valor de hoje e mostra impacto no patrimônio;
- `A previsão agora é receber 45 mil.` -> atualiza Valor previsto a receber, sem alterar patrimônio atual;
- `Coloquei mais 5 mil no Studio.` -> registra aporte, pergunta origem do recurso se omitida e atualiza custo histórico/aportes.

---

## 17. Métricas de caixa versus gasto

O sistema deve conseguir responder separadamente:

### Quanto saiu de uma conta específica?
Soma dos débitos físicos daquela conta, inclusive pagamento de fatura e transferências, porque a pergunta é bancária.

### Quanto eu gastei?
Consumo/despesas reais, incluindo compras de cartão, sem contar pagamento da fatura novamente.

### Quanto tenho disponível/patrimônio?
Saldos/ativos reais, respeitando transferências internas e compromissos sem dupla contagem.

A UI deve deixar claro qual pergunta cada número responde.

---

## 18. Invariantes P0 de produto

Estes comportamentos devem virar testes automatizados antes do go-live:

1. pagamento de fatura nunca duplica gasto;
2. aplicação/resgate Privilège <-> Corrente nunca vira renda/despesa;
3. resultado operacional negativo nunca fabrica resgate;
4. saldo observado posterior não sofre subtração novamente de movimentos anteriores;
5. compra de cartão não reduz saldo bancário antes do pagamento;
6. fatura fechada não paga é COMPROMETIDO;
7. pagamento parcial carrega principal sem criar novo gasto;
8. juros/IOF de financiamento são novos gastos apenas quando observados/confirmados;
9. estorno neutraliza a compra sem apagar histórico;
10. parcelas futuras são COMPROMETIDO, não transações REALIZADAS;
11. pagamento de obrigação real remove o compromisso futuro e não duplica despesa;
12. comissão não entra em previsão futura automaticamente;
13. salário recorrente previsto da Kelly concilia com o crédito real sem duplicar renda;
14. patrimônio atual de investimento usa Valor de hoje uma única vez;
15. saldo bancário confirmado é autoridade no instante observado; divergência derivada é sinalizada;
16. duplicidade provável exige decisão humana antes de excluir/fundir;
17. ação do Assistente é auditável e reversível quando aplicável;
18. Assistente pergunta origem do recurso quando a saída em caixa não a informa;
19. motor e Codex podem discordar; discordância relevante não vira número “confiável” silenciosamente;
20. nenhuma tela principal depende de o usuário entender tipos internos de transação.

---

## 19. Ordem de implementação — prioridade P0

### Slice 0 — Rebaseline normativo e inventário de conflito

- comparar este documento com `README.md`, `FINANCIAL_RULES.md`, `FINANCIAL_INVARIANTS.md`, `ARCHITECTURE.md`, `INTELLIGENCE.md`, `ROADMAP.md` e código;
- produzir matriz `manter / alterar / remover / migrar`;
- não fazer mudança financeira silenciosa;
- identificar migrations/backfills necessários;
- preservar dados reais existentes.

### Slice 1 — Motor de caixa/Privilège + saldos

- remover settlement automático de déficit/sobra como fato;
- separar gasto, fluxo de caixa, transferência interna e posição patrimonial;
- respeitar saldo observado `as-of`;
- corrigir Dashboard para não subtrair duas vezes movimentos anteriores;
- testes P0 correspondentes.

### Slice 2 — Ciclo de cartões/faturas

- aberta -> fechada -> parcialmente paga/paga;
- modal Conferir e pagar;
- origem Corrente versus Privilège;
- parcelamento;
- estorno;
- pagamento parcial;
- principal carregado;
- juros/IOF;
- reconciliação de divergência;
- nenhum gasto duplicado.

### Slice 3 — Obrigações e projeção REALIZADO/COMPROMETIDO/PREVISTO

- contas a pagar;
- parcelas futuras;
- antecipação;
- vinculação ao pagamento real;
- projeção 30/60/90;
- salário futuro da Kelly;
- comissão somente realizada.

### Slice 4 — Assistente Financeiro operacional

- intents livres;
- perguntas de desambiguação;
- ações tipadas;
- origem do recurso;
- trilha before/after/trace;
- undo;
- validação Codex;
- criação/aprendizado de tipos reutilizáveis;
- sem SQL arbitrário pelo modelo.

### Slice 5 — UX e navegação alvo

- menu final deste documento;
- absorção das abas antigas;
- Entradas/Saídas contextuais;
- alertas de revisão contextuais;
- remover dependência da Central `Lançar agora` como navegação própria.

### Slice 6 — Patrimônio e investimentos

- modelo de ativo/investimento;
- Studio;
- valor investido/hoje/futuro;
- histórico;
- Dashboard patrimonial;
- simulações do Assistente.

### Slice 7 — Gastos & Economia + Relatórios

- visão mensal/histórica;
- economia contextual;
- referências externas separadas de dados internos;
- relatórios caixa/consumo/patrimônio/compromissos.

### Slice 8 — Migração, reconciliação real e Go-Live

- reconciliar dados reais existentes sem fabricar fatos;
- corrigir obrigações já pagas ainda projetadas;
- reconciliar cartões/contas;
- testes E2E com datasets sintéticos equivalentes;
- smoke manual com dados reais;
- todos os 12 gates verdes;
- checklist de uso diário;
- nenhum BLOCK/REVIEW financeiro relevante aberto para o período inicial.

---

## 20. Política de execução até o go-live

- Um slice por PR.
- Claude pode executar implementação, mas não faz merge.
- O engenheiro responsável revisa código, migrations, regras, segurança, household isolation, testes e regressões.
- Toda divergência entre este documento e a implementação atual deve ser explicitada como Technical Challenge; não resolver escondendo a divergência.
- Não “fazer teste passar” alterando regra financeira sem aprovação.
- Slices anteriores não relacionados ao P0 ficam pausados, preservados em suas branches, até o rebaseline de go-live estar concluído ou até o engenheiro liberá-los explicitamente.

---

## 21. Definition of Done para uso no próximo mês

O sistema só deve ser declarado pronto quando, no mínimo:

- Dashboard não mostra déficit/resgate fabricado;
- saldos reais reconciliam com observações confirmadas;
- compra em cartão, fechamento, pagamento total/parcial e estorno funcionam sem dupla contagem;
- Privilège funciona como caixa operacional remunerado e não como “déficit automático”;
- Entradas/Saídas/Contas a pagar funcionam manualmente;
- Assistente registra e consulta fatos com perguntas quando faltam dados;
- tipos aprendidos persistem sem explodir duplicidades semânticas;
- Studio aparece no patrimônio com campos editáveis corretos;
- Kelly aparece em projeção futura; comissão não;
- projeções distinguem REALIZADO/COMPROMETIDO/PREVISTO;
- importação alerta possíveis duplicidades sem destruir fatos;
- ações do Assistente têm auditoria/undo;
- cálculos críticos podem ser validados pelo Codex e divergências são exibidas;
- os 12 gates de CI ficam verdes no head final integrado;
- smoke manual real dos fluxos cotidianos passa antes do deploy.
