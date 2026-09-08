# Work Order — Fase 3: comparação visual de cenários de compra

## Objetivo

Implementar exatamente o próximo item ainda não concluído da Fase 3 de `docs/ROADMAP.md`: **comparação visual de cenários de compra**.

Este slice deve melhorar a decisão de compra reutilizando exclusivamente o Projection Engine, o Projection Validator, snapshots, obrigações, parcelas e contratos já canônicos. A UI não pode criar um segundo motor de cálculo, projeção, juros, liquidez, déficit, piso ou recomendação.

## Fonte de verdade

Ler antes de implementar e confrontar este Work Order com:

- `docs/ROADMAP.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`;
- contratos atuais de forecast/projeção, Advisor de compra, snapshots, parcelas futuras e obrigações.

A documentação normativa e o comportamento determinístico verificável prevalecem. Claude pode e deve abrir Technical Challenge se este Work Order estiver incorreto, incompleto ou criar risco.

## Escopo mínimo

1. Criar uma superfície visual que permita comparar alternativas de compra usando os mesmos inputs financeiros já suportados pelo backend canônico (por exemplo: preço/entrada/parcelamento quando o contrato existente permitir), sem inventar nova semântica financeira.
2. Cada alternativa deve ser avaliada pelo mesmo caminho canônico de projeção já usado pelo sistema. Não reproduzir fórmulas no JavaScript ou em endpoint paralelo.
3. Exibir diferenças relevantes entre cenários com dados provenientes do backend: impacto mensal, liquidez, déficit descoberto, distância do piso, parcelas/obrigações futuras e status de integridade/projeção aplicável.
4. Preservar os três cenários normativos de comissão/projeção existentes e a autoridade do Projection Validator/INV-018. Se a projeção não for confiável, a comparação deve refletir isso de forma explícita e não fabricar recomendação.
5. Advisor/Codex pode explicar resultados já calculados, mas não pode alterar números, score, status, recomendação determinística ou escolher a alternativa vencedora por autoridade própria.
6. Household isolation e autenticação são obrigatórios. Nenhum payload deve expor dados de outra família.
7. Não persistir uma compra, obrigação ou transação apenas por comparar um cenário. Qualquer criação de fato financeiro continua exigindo a ação canônica/humana já existente.
8. Não alterar fatos históricos, não fazer auto-fix, não reclassificar dados e não criar migration salvo necessidade estrutural real e aditiva.
9. Não antecipar o próximo item da Fase 3 (`notificações de vencimento no navegador`) nem a Fase 4.

## Critérios de aceite

- Duas ou mais alternativas podem ser comparadas na UI sem cálculo financeiro paralelo no navegador.
- Os resultados de cada alternativa são derivados do backend canônico e são reproduzíveis pelos contratos existentes do Projection Engine/Validator.
- Nenhum cenário mostra saldo negativo fictício; déficit descoberto, piso e liquidez seguem exatamente as regras normativas atuais.
- Diferenças entre alternativas não misturam projeção com fato observado nem duplicam parcelas/obrigações já persistidas.
- A comparação é read-only/simulativa até que o usuário execute explicitamente um comando canônico de gravação.
- Estado `unknown`/não confiável permanece explícito; nunca é convertido silenciosamente em `pass` ou recomendação positiva.
- A UI mobile permanece utilizável e sem rolagem horizontal indevida.
- Os 12 gates existentes permanecem verdes no mesmo head final.

## Invariantes e proibições

- Preservar `INV-005`, `INV-006`, `INV-018`, `INV-019`, `INV-020`, `INV-021` e `INV-022`, além dos demais invariants aplicáveis.
- Não duplicar `build_forecast`, Projection Engine, Projection Validator, cálculo de parcelas, rendimento, déficit, saldo, piso ou comissão.
- Não implementar fórmulas financeiras no frontend.
- Não alterar `FINANCIAL_RULES`/`FINANCIAL_INVARIANTS` para acomodar a UI.
- Não criar fatos financeiros durante simulação/comparação.
- Não permitir que Claude/Codex/Advisor seja autoridade sobre fatos determinísticos.
- Não usar dados reais/PII em fixtures.
- Não fazer merge.

## Testes obrigatórios

Cobrir ao menos:

- comparação de duas alternativas com impactos diferentes;
- paridade de cada resultado com o Projection Engine/Validator canônicos;
- déficit maior que a liquidez e saldo final nunca negativo;
- piso de segurança tratado como referência, nunca bloqueio artificial;
- cenário com parcelas futuras sem dupla contagem;
- projeção não confiável/INV-018 refletida de forma fail-closed;
- ausência de mutação em `Transaction`, `Obligation`, `Commission`, `PayrollRecord`, `Document` e fatos históricos durante comparação;
- autenticação e household isolation;
- frontend sem cálculo financeiro paralelo (teste estrutural quando aplicável);
- regressão do Advisor/forecast já existente;
- responsividade/sintaxe frontend;
- os 12 gates existentes no head final.

## Relação de revisão

Claude é o executor. Não pode fazer merge. Pode e deve contestar tecnicamente este Work Order, critérios de aceite ou orientação do engenheiro quando houver evidência verificável em documentação, código, testes, segurança, integridade financeira ou menor risco de regressão. Divergência não resolvida bloqueia merge.