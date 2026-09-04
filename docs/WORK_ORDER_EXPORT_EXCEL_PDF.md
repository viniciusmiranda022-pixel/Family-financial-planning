# Work Order — Exportação Excel/PDF

## Roadmap

Próximo item não concluído da Fase 2 em `docs/ROADMAP.md`: **exportação Excel/PDF**.

Trate `docs/FINANCIAL_RULES.md`, `docs/FINANCIAL_INVARIANTS.md`, `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`, `docs/ARCHITECTURE.md`, os contratos de snapshots/Monthly Close e os serviços canônicos de relatório como fonte de verdade.

## Executor

Claude é o executor da implementação. Claude não pode fazer merge, alterar semântica financeira não documentada, recalcular fatos por política própria, corrigir dados reais automaticamente, reduzir cobertura, remover salvaguardas ou dar a Claude/Codex/Advisor autoridade sobre fatos determinísticos. Claude pode e deve contestar este Work Order quando houver evidência concreta de documentação, código, testes, segurança ou menor risco de regressão. Divergência não resolvida bloqueia merge.

## Objetivo

Permitir exportar relatórios financeiros em Excel e PDF a partir dos mesmos dados canônicos já apresentados pelos relatórios do sistema, sem introduzir uma segunda política de cálculo no frontend, no gerador de planilha ou no gerador de PDF.

## Escopo e critérios de aceite

- Exportar exatamente os filtros/períodos/household e métricas canônicas já suportados pela camada de relatórios.
- O Excel deve ser gerado no backend, com estrutura legível, cabeçalhos claros, tipos numéricos/datas corretos e metadados mínimos de período/geração; não embutir fórmulas que recalculam política financeira fora do motor canônico.
- O PDF deve ser gerado no backend a partir dos mesmos dados canônicos do relatório. Não usar impressão do navegador como implementação do slice; a impressão existente pode continuar como compatibilidade separada.
- Excel e PDF para a mesma consulta devem representar os mesmos totais, categorias, períodos e exclusões do relatório canônico. Nenhum formato pode reclassificar, reconciliar, deduplicar ou recalcular fatos por conta própria.
- Preservar INV-001/002/003/004/014/015/016/017 e todos os demais invariantes aplicáveis.
- Respeitar `excluded`, duplicidades, reconciliações, competência, origem dos fatos e Monthly Close conforme contratos canônicos existentes.
- Household isolation e autenticação obrigatórias. Não permitir exportar dados de outro household por manipulação de filtros/IDs.
- Evitar PII desnecessária nos nomes dos arquivos, logs e mensagens de erro. Não registrar conteúdo financeiro bruto em logs.
- Não criar exportações com HTML/CSV renomeados para `.xlsx`/`.pdf`; usar formatos reais e válidos.
- Compatibilidade com dados existentes é obrigatória. Se houver nova dependência, justificar e manter Docker/CI reproduzíveis.
- Mudanças de schema, se inevitáveis, devem ser aditivas, Alembic/PostgreSQL-safe e com downgrade; preferir nenhum schema novo se não for necessário.
- A UI apenas solicita o formato e baixa o artefato retornado pelo backend. Não calcular totais no browser.
- Não implementar testes com documentos anonimizados nem o item de go-live manual neste PR.

## Testes obrigatórios

Cobrir no mínimo:

1. exportação Excel válida para relatório sintético conhecido;
2. exportação PDF válida para a mesma consulta;
3. paridade dos totais/métricas entre API/relatório canônico, Excel e PDF;
4. filtros de período/ano e household isolation;
5. dados `excluded`/duplicidade/reconciliação respeitados conforme o relatório canônico;
6. zero dados e períodos sem movimento com resultado válido e não fabricado;
7. valores monetários e datas preservados sem erro de arredondamento/formatação que altere o fato;
8. nomes/content-disposition/content-type seguros;
9. erros sem PII/conteúdo bruto em logs/resposta;
10. regressão dos relatórios existentes e da impressão PDF já existente;
11. Docker build e dependências reproduzíveis;
12. todos os 12 gates nomeados do CI.

## Riscos e proibições

- Proibido duplicar lógica de cálculo financeiro em template, biblioteca de Excel/PDF ou frontend.
- Proibido somar novamente linhas já agregadas pelo backend ou excluir fatos por regra local do exportador.
- Proibido tratar pagamento de fatura, transferências, aplicações/resgates ou duplicidades de forma diferente do motor canônico.
- Proibido exportar dados de outro household.
- Proibido gerar artefato parcialmente válido e reportar sucesso silencioso.
- Proibido introduzir fórmula de planilha como fonte de verdade para métricas determinísticas.

## Entrega esperada

Reportar: endpoints/serviços reutilizados, contrato de exportação, bibliotecas adicionadas, paridade com relatório canônico, segurança/privacidade, compatibilidade, migrations se houver, testes/gates, limitações residuais e qualquer objeção técnica ao Work Order. **Não fazer merge.**