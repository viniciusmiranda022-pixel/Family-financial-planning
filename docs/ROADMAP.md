# Roteiro do produto

## Fase 1 — MVP atual

- autenticação e primeiro acesso;
- contas e titulares;
- CSV, OFX e PDF textual;
- parsers específicos de extrato/fatura Itaú, CSV Nubank e demonstrativo de pagamento;
- criptografia e deduplicação;
- classificação e revisão;
- comissões, holerites e compromissos;
- perfil, orçamento e projeção;
- plano objetivo de cortes por categoria;
- Docker Compose e backup.

## Fase 2 — Aderência e produtividade

- regras editáveis de estabelecimento;
- conciliação visual de fatura x débito bancário;
- importação em lote;
- exportação Excel/PDF;
- testes com cópias anonimizadas dos documentos reais.

## Entregue em 25/08/2026 — captura e acesso remoto

- OCR com Tesseract para PDFs escaneados;
- transcrição local de áudio com Whisper;
- captura por texto, áudio, foto, boleto, fatura, extrato e holerite;
- prévia editável e auditável antes da confirmação;
- classificação ambígua assistida pelo Codex;
- consultor com cálculo local, parcelamento/juros e explicação do Codex;
- acesso privado por Tailscale para Vinicius e Kelly.

## Entregue em 26/08/2026 — experiência mobile e análises

- identidade visual própria do Family Finance;
- navegação e tabelas redesenhadas para celular, sem rolagem lateral;
- leitura rápida da evolução dos últimos seis meses na visão geral;
- relatórios configuráveis de 1 a 12 meses;
- relatório anual por ano-calendário;
- comparação de entradas, saídas, gastos, saldo operacional e teto;
- ranking de categorias e detalhamento consolidado por banco e cartão;
- impressão do relatório para PDF pelo navegador.
- Privilège DI tratado como conta central de liquidez, com sobra para aplicar, déficit para retirar e piso de segurança separado;
- barra lateral preenchida e rolável em telas com pouca altura.

## Em andamento — Financial Integrity Engine

- discovery técnico e matriz de fontes de verdade documentados;
- contrato formal `INV-001` a `INV-022` na versão `2026.09.1`;
- registry executável com resultado estruturado e `unknown` para fatos insuficientes;
- regra canônica do Privilège DI coberta por testes de propriedades e cenários de borda;
- migração inicial congelada, sem dependência dos modelos ORM futuros;
- runs e findings persistentes com fingerprint, score explicado, status, gates específicos e
  trilha before/after/reason/trace;
- APIs autenticadas de execução e consulta, ainda sem habilitar a interface de Integridade;
- contrato `ParsedDocument`, reconciliação determinística de documentos e observações imutáveis de
  saldo implementados no incremento de reconciliação;
- grupos persistentes de duplicidade com precedência de fonte, resolução explícita e nenhuma
  limpeza destrutiva;
- primeira baseline robusta de anomalias restrita a meses reconciliados e aprendizado local que
  exige três confirmações mais aceite administrativo;
- auditor semântico Codex (`POST /v1/audit` no sidecar `advisor`, allowlist, defesa contra prompt
  injection, saída sem autoridade sobre status/score/gates/findings, fallback seguro e métricas --
  ver `docs/ARCHITECTURE.md`) entregue como PR 6; pendente revisão do engenheiro responsável antes
  do merge;
- próximos incrementos: UI de Integridade e safety gates completos de CI (PostgreSQL/Alembic no
  pipeline, property tests financeiros e backfill controlado).

## Fase 3 — Próximas evoluções

- worker assíncrono para filas de OCR e áudio;
- regras editáveis e aprendizado pelas correções confirmadas;
- comparação visual de cenários de compra;
- notificações de vencimento no navegador;

## Fase 4 — Operação endurecida

- proxy HTTPS automatizado;
- perfis separados para administrador e consulta;
- autenticação multifator local;
- backup externo automatizado;
- observabilidade e alertas;
- rotina documentada de atualização e rollback.

## Fora do escopo inicial

- conexão direta ao internet banking;
- armazenamento de credenciais bancárias;
- iniciação de pagamentos;
- aconselhamento de investimentos;
- substituição de contador ou cálculo fiscal oficial.
