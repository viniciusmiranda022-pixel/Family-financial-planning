# Work Order — October Go-Live Slice 5: navegação e UX final

## Prioridade e fonte normativa

Este é o Slice 5 do P0 #87. A fonte normativa principal é `docs/OCTOBER_GO_LIVE_REBASELINE.md`, especialmente a navegação alvo e os fluxos já implementados nos Slices 1–4. Em conflito de semântica de produto, o rebaseline de outubro prevalece; qualquer substituição de comportamento legado exige documentação e teste explícitos.

Claude é o executor. Não fazer merge e não iniciar Slice 6. Não consumir prioridade com MFA, hardening, observabilidade ou parser não bloqueador.

## Objetivo

Entregar a navegação e a experiência final do produto para uso cotidiano, expondo de forma simples os motores financeiros já integrados, sem criar um segundo cálculo, segundo estado financeiro ou segundo fluxo de escrita.

## Navegação principal obrigatória

A navegação principal deve ser exatamente:

- `🏠 Dashboard Financeiro`
- `↓ Entradas`
- `↑ Saídas`
- `▤ Contas a pagar`
- `✦ Gastos & Economia`
- `▥ Relatórios`
- `✦ Assistente Financeiro`
- separador
- `👥 Acessos`
- `⚙ Configurações`

Não manter `+ Lançar` como item global principal.

## Absorção obrigatória de superfícies legadas

- `Rendas` → `Entradas`.
- `Transferências` → ações contextuais em Entradas/Saídas/Assistente; sem item principal separado.
- `Importações` → `Configurações > Dados e importações` e ações contextuais.
- `Lançamentos` → `Relatórios` como livro-razão/auditoria.
- `Revisar` → alertas contextuais e Dashboard; sem navegação permanente.
- `Planejamento` → `Gastos & Economia` e projeção no Dashboard.
- `Consultor` → `Assistente Financeiro`.
- `Lançar agora` → remover como navegação separada; captura rápida deve ocorrer via Assistente ou contexto de Entrada/Saída.

Não apagar capacidade necessária durante a absorção: rotas legadas podem permanecer internamente quando necessárias para compatibilidade, mas a UX alvo não deve duplicar conceitos nem oferecer dois caminhos concorrentes sem motivo documentado.

## Escopo funcional obrigatório

1. **Entradas**
   - ação `+ Nova entrada`;
   - tipos existentes e templates aprendidos do Slice 4;
   - `Outra entrada` com fluxo de sugestão/confirmação de tipo reutilizável;
   - transferência interna apresentada como transferência, nunca renda.

2. **Saídas**
   - ação `+ Nova saída`;
   - tipos existentes e templates aprendidos;
   - `Outra saída` com o mesmo ciclo de aprendizado confirmado;
   - origem de recurso explícita quando material;
   - transferência interna apresentada como transferência, nunca gasto.

3. **Contas a pagar**
   - obrigações e cartões/faturas em uma experiência coerente, preservando suas fontes canônicas;
   - `Conferir e pagar` do Slice 2 acessível e compreensível;
   - estados `REALIZADO`, `COMPROMETIDO` e `PREVISTO` exibidos sem mistura;
   - atrasadas, vencendo, parcelas/recorrências, faturas fechadas e pagas recentemente identificáveis sem fabricar fatos.

4. **Assistente Financeiro**
   - consumir o contrato do Slice 4: `interpret -> proposal_id -> execute`;
   - pergunta de desambiguação antes de qualquer execução quando faltar dado material;
   - provável duplicidade deve expor `Pular`, `Importar mesmo assim` e `Ver existente` antes da escrita;
   - resultado da ação, trilha/auditoria e `Desfazer` quando reversível; recusa explícita quando não reversível;
   - nunca permitir que a UI envie typed action/alvo livre para contornar a proposta server-side.

5. **Dashboard/alertas contextuais**
   - usar somente os endpoints/motores canônicos já integrados;
   - destacar compromissos, divergências e itens que exigem revisão sem reintroduzir uma aba global `Revisar`;
   - saldo confirmado continua soberano e divergência nunca é mascarada por ajuste sintético.

6. **Configurações e Relatórios**
   - `Dados e importações` deve absorver o acesso anteriormente exposto como `Importações`;
   - `Relatórios` deve absorver o livro-razão/auditoria anteriormente exposto como `Lançamentos`;
   - não alterar regra financeira para facilitar apresentação.

## Invariantes e proibições

- Pagamento de fatura não vira nova despesa.
- Transferência interna não vira renda/despesa.
- Nenhum resgate/aplicação Privilège é fabricado por déficit/sobra.
- `REALIZADO`, `COMPROMETIDO` e `PREVISTO` não podem ser somados ou rotulados como se fossem o mesmo estado.
- Comissão de Vinicius não entra automaticamente como prevista.
- Hipótese/Codex não vira fato silenciosamente.
- Deduplicação continua fail-safe e humana.
- Não duplicar lógica de cálculo no frontend; o frontend apresenta contratos canônicos do backend.
- Não criar migration salvo se houver necessidade real e explicitamente justificada por dado persistente de UX; preferência é zero migration neste slice.
- Não tocar em dados financeiros reais para “corrigir” apresentação.

## Testes obrigatórios

Cobrir, no mínimo:

1. menu principal contém exatamente os itens alvo e não contém as entradas legadas absorvidas;
2. cada item alvo navega para sua superfície correta;
3. `+ Nova entrada` e `+ Nova saída` funcionam sem classificar transferência interna como renda/despesa;
4. templates aprendidos aparecem e exigem confirmação conforme contrato existente;
5. Contas a pagar expõe obrigação/fatura e `Conferir e pagar` sem dupla contagem;
6. Assistente: mensagem completa → proposta → execução → resultado/auditoria;
7. Assistente: falta de origem/conta/cartão/natureza → pergunta e zero mutações;
8. Assistente: provável duplicidade → três escolhas humanas e zero escrita até decisão explícita;
9. Assistente: ação reversível → undo visível e funcional; não reversível → motivo explícito;
10. alertas/revisões relevantes continuam alcançáveis sem item `Revisar` permanente;
11. importações permanecem alcançáveis em Configurações; livro-razão/auditoria permanece alcançável em Relatórios;
12. regressão responsiva básica das superfícies alteradas e ausência de links/rotas principais quebrados;
13. suíte backend/frontend aplicável, lint e todos os 12 gates de CI verdes.

## Critérios de aceite

- Usuário consegue operar o cotidiano pelas superfícies alvo sem depender de nomes internos ou abas legadas.
- Nenhuma capacidade necessária fica órfã após remoção/absorção da navegação antiga.
- A UI reutiliza os contratos dos Slices 1–4 e não reimplementa semântica financeira.
- Não há link duplicado que represente o mesmo conceito financeiro com semânticas diferentes.
- Testes provam navegação, desambiguação, deduplicação, audit/undo e preservação dos invariantes.
- Documentação (`README`/`ARCHITECTURE`/`ROADMAP` e matriz de conflito quando aplicável) é atualizada para refletir a UX alvo e marcar o legado absorvido.

## Riscos a revisar antes do merge

- capacidade perdida ao remover item de menu sem realocação;
- frontend chamando endpoints legados com semântica incompatível;
- duplicação de cálculo/estado no cliente;
- desambiguação visual que ainda permita execução sem proposta server-side;
- estado financeiro ou saldo apresentado com rótulo enganoso;
- navegação quebrada por URLs/bookmarks antigos — redirecionar quando razoável em vez de falhar silenciosamente;
- regressão de autorização/household isolation ao reorganizar superfícies.

## Entrega de Claude no PR

Responder com inventário `legado -> destino`, arquivos/rotas alterados, decisões de compatibilidade/redirecionamento, testes executados, CI, riscos residuais e qualquer Technical Challenge com evidência concreta. Não fazer merge.