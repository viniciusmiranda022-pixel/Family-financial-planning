# Work Order — PR 8: Financial Safety CI, Backfill and Final Docs

## Objetivo

Concluir o incremento final documentado do Financial Integrity Engine, exatamente na ordem prevista em `docs/INTEGRITY_IMPLEMENTATION_PLAN.md` e `docs/ROADMAP.md`, adicionando safety gates de CI sobre PostgreSQL/Alembic, regressões financeiras finais, property tests, backfill controlado e documentação operacional final.

Claude é o executor desta implementação. Claude **não pode fazer merge**, alterar regras financeiras não documentadas, fabricar fatos ausentes, reduzir cobertura para fazer CI passar, corrigir dados financeiros reais automaticamente, executar migration destrutiva, criar cálculo financeiro paralelo ou conceder ao Codex/Advisor autoridade sobre fatos determinísticos, invariants, score, gates, findings, snapshots, reconciliação ou fechamento mensal.

Claude pode e deve contestar tecnicamente este Work Order ou comentários de revisão quando houver evidência concreta em documentação normativa, comportamento verificável, testes, segurança, integridade financeira ou menor risco de regressão. Divergência não resolvida bloqueia merge.

## Fonte de verdade obrigatória

Antes de implementar, reler integralmente:

- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/ROADMAP.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`;
- `docs/INTELLIGENCE.md`;
- migrations Alembic existentes e workflow atual de CI.

A documentação normativa prevalece sobre conveniência de implementação ou comportamento legado contraditório.

## Escopo obrigatório

1. **PostgreSQL e Alembic no CI**
   - subir PostgreSQL compatível com produção;
   - validar `alembic upgrade head` em banco vazio;
   - validar upgrade de baseline legado documentado até `head`;
   - validar downgrade somente onde a documentação declarar seguro;
   - preservar dados históricos em todos os testes de migration/backfill.

2. **Gates financeiros completos**
   - integrar no CI os invariants e gates determinísticos exigidos pelo plano;
   - `unknown` nunca pode ser promovido implicitamente a `pass`;
   - CI deve falhar quando invariant material ou trust gate exigido falhar;
   - nenhum gate pode depender de Codex/Advisor.

3. **Datasets fictícios de regressão**
   - adicionar fixtures/datasets sintéticos suficientes para cobrir regras financeiras críticas, reconciliação, duplicidades, snapshots, projeção, publicação e monthly close;
   - nenhum dado pessoal ou documento financeiro real deve entrar no repositório.

4. **Property tests finais**
   - cobrir propriedades financeiras e invariants onde o plano exigir;
   - incluir bordas de arredondamento `ROUND_HALF_UP`, liquidez zero, déficit maior que saldo, piso de segurança, comissão por recebível, competência e idempotência;
   - não duplicar a implementação sob teste no próprio oracle do teste.

5. **Backfill idempotente e controlado**
   - fornecer comando explícito, retomável e idempotente;
   - o backfill pode criar snapshots, findings, reconciliações, observações legadas e grupos derivados previstos pela documentação;
   - não pode modificar silenciosamente `Transaction`, `Document`, `PayrollRecord`, `Commission`, `Obligation` ou fatos financeiros de origem;
   - saldo legado sem data efetiva confiável deve permanecer `unknown/review_required` até confirmação humana; não inferir data nem reconstruir saldo por suposição;
   - registrar run, versões, duração, resultado e rastreabilidade suficiente para auditoria.

6. **Execução controlada sobre dados existentes**
   - implementar dry-run ou mecanismo equivalente seguro conforme arquitetura existente;
   - detectar e reportar divergências sem auto-fix destrutivo;
   - demonstrar idempotência e ausência de alteração dos fatos históricos.

7. **Documentação final**
   - atualizar `README.md`, `docs/ARCHITECTURE.md`, `docs/FINANCIAL_RULES.md`, `docs/INTELLIGENCE.md`, `docs/SECURITY.md` e `docs/ROADMAP.md`;
   - adicionar runbook de rollout e rollback;
   - documentar pré-condições, migrations, backfill, feature flags, observabilidade, recuperação e critérios de parada;
   - marcar explicitamente o estado final do roadmap somente quando todos os critérios desta fatia forem atendidos.

## Invariants e contratos que não podem regredir

Todos os contratos `INV-001` a `INV-022`, versão financeira vigente, permanecem normativos. Em especial:

- ausência de evidência => `unknown`, nunca `pass`;
- `FinancialSnapshot` é a base canônica de publicação onde documentado;
- dashboard/report/projection trust gates devem observar evidência determinística real;
- Privilège DI nunca pode produzir saldo negativo fictício;
- piso de segurança é alerta, não bloqueio de liquidez;
- movimentos patrimoniais não viram renda/despesa operacional;
- duplicidade/reconciliação não apagam evidência histórica;
- ações humanas e mudanças de lifecycle permanecem auditáveis;
- Codex/Advisor permanece sem autoridade sobre fatos, score e gates.

## Testes obrigatórios

A implementação só pode ser considerada pronta quando, no mínimo:

- CI completo estiver verde no head exato;
- PostgreSQL real estiver presente no pipeline para migrations e testes que dependem de locking/semântica específica;
- upgrade de banco vazio até head passar;
- upgrade de snapshot/schema legado até head passar;
- backfill sobre fixture legada passar duas vezes com resultado idempotente;
- teste provar que fatos históricos não foram alterados pelo backfill;
- property tests financeiros finais passarem;
- regressões de trust/integrity existentes continuarem verdes;
- lint, syntax checks e builds existentes continuarem verdes;
- nenhum teste for removido, afrouxado ou reescrito apenas para aceitar comportamento incorreto.

## Riscos a tratar explicitamente no PR

- migration incompatível com dados existentes;
- backfill destrutivo ou não idempotente;
- divergência entre SQLite e PostgreSQL;
- false trust causado por `unknown` ou evidência parcial;
- cálculo duplicado entre engine, teste, dashboard, report ou projection;
- vazamento de PII/segredos em fixtures, logs ou artifacts;
- rollback que remova fatos financeiros ou evidência histórica;
- aumento de autoridade do Codex/Advisor;
- CI verde que não exerça migrations/backfill reais.

## Proibições

Não:

- fazer merge;
- alterar regras financeiras para acomodar teste;
- limpar ou normalizar dados reais automaticamente;
- usar `Base.metadata.create_all()`/`drop_all()` como substituto de migration histórica;
- executar backfill destrutivo;
- inventar fatos ausentes;
- reduzir cobertura;
- esconder `unknown`;
- criar PR seguinte enquanto este slice estiver aberto.

## Conteúdo obrigatório da atualização final do PR

Ao concluir, atualizar o corpo do PR conforme a seção 22.1 do plano, incluindo resumo, problema, solução, arquitetura, arquivos alterados, migration/backfill, riscos, testes, CI, documentação, compatibilidade, segurança/privacidade, evidência de idempotência e pendências reais. Solicitar revisão do engenheiro responsável e aguardar decisão de merge.
