# Work Order — Fase 3: notificações de vencimento no navegador

## Objetivo

Implementar exatamente o próximo item ainda não concluído de `docs/ROADMAP.md`: **notificações de vencimento no navegador**.

A funcionalidade deve avisar o usuário sobre vencimentos já derivados de fatos canônicos existentes, sem criar novo motor financeiro, sem antecipar a Fase 4 e sem transformar notificação em fato financeiro.

## Fonte de verdade

Ler antes de implementar e confrontar este Work Order com:

- `docs/ROADMAP.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`;
- contratos atuais de `Obligation`, parcelas futuras, faturas/reconciliação, forecast, snapshots e autenticação/household isolation.

A documentação normativa e o comportamento determinístico verificável prevalecem. Claude pode e deve abrir Technical Challenge se este Work Order estiver incorreto ou inferior.

## Escopo mínimo

1. Criar uma superfície de notificações no navegador para vencimentos futuros já representados por contratos canônicos existentes.
2. Reutilizar datas e valores derivados pelos serviços existentes; não duplicar recorrência, competência, parcelas, projeção, juros, saldo, piso, déficit ou classificação no frontend.
3. A UI pode solicitar permissão ao navegador somente por ação humana explícita. Negar ou revogar permissão não pode quebrar a aplicação.
4. Household isolation e autenticação são obrigatórios. Nenhum payload ou notificação pode expor dados de outra família.
5. Notificações são informativas: não liquidam, criam, alteram, reclassificam, conciliam nem marcam automaticamente fatos financeiros como pagos.
6. Não inferir vencimentos inexistentes. Ausência de data/evidência suficiente permanece `unknown`/não notificável; nunca fabricar data para produzir alerta.
7. Evitar duplicação de alertas para o mesmo fato/janela no mesmo cliente quando o contrato técnico permitir, sem persistir mutação em entidades financeiras.
8. Não introduzir service worker, push server, infraestrutura externa, perfil de administrador/consulta, MFA, observabilidade ou outro item da Fase 4 salvo se a documentação atual já os exigir explicitamente para este slice.

## Critérios de aceite

- O usuário autenticado consegue habilitar/desabilitar a experiência de alertas no navegador sem alterar fatos financeiros.
- Vencimentos exibidos/notificados correspondem aos contratos canônicos existentes e mantêm competência/recorrência/parcelas sem dupla contagem.
- Permissão `denied`, API Notification indisponível ou erro do navegador degrada para UI interna sem falha funcional.
- Não há cálculo financeiro paralelo no JavaScript.
- Household isolation, privacidade e conteúdo mínimo das notificações são preservados; não incluir PII desnecessária nem conteúdo bruto de documentos.
- Nenhuma migration destrutiva e nenhuma correção silenciosa de dados.
- Os 12 gates existentes permanecem verdes no mesmo head final.

## Invariantes e proibições

Preservar todos os invariantes aplicáveis, em especial INV-002, INV-008, INV-010, INV-014, INV-017, INV-018, INV-019, INV-020, INV-021 e INV-022. Não alterar `FINANCIAL_RULES`/`FINANCIAL_INVARIANTS` para fazer teste passar. Não criar segundo motor de agenda financeira. Não dar autoridade determinística a Claude/Codex/Advisor. Não usar dados reais/PII em fixtures. Não fazer merge.

## Testes obrigatórios

Cobrir ao menos:

- vencimento canônico futuro aparece uma única vez na janela aplicável;
- recorrência/parcelas existentes não são duplicadas;
- ausência de data/evidência não produz vencimento fabricado;
- household isolation e autenticação;
- permissão `granted`, `denied` e API Notification indisponível;
- nenhuma mutação em `Transaction`, `Obligation`, `Document`, `Commission`, `PayrollRecord`, snapshots/findings ou demais fatos durante leitura/notificação;
- frontend sem cálculo financeiro paralelo;
- regressão de forecast/parcelas/obrigações e sintaxe/responsividade frontend;
- os 12 gates existentes no head final.

## Relação de revisão

Claude é o executor e não pode fazer merge. Pode e deve contestar tecnicamente este Work Order, critérios de aceite ou orientação do engenheiro quando houver evidência concreta em documentação, código, testes, segurança, integridade financeira ou menor risco de regressão. Divergência não resolvida bloqueia merge.
