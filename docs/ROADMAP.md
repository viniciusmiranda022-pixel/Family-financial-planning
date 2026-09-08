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
- conciliação visual de fatura x débito bancário: `GET /api/card-payment-reconciliations`
  (`period` opcional) mostra, para cada débito bancário já classificado como `reconciliation`
  pelo classificador único, o pagamento de fatura correspondente já vinculado, o candidato único
  determinístico (mesma tolerância de R$ 0,01, janela de `CARD_PAYMENT_MATCH_WINDOW_DAYS = 45`
  dias) ou a lista completa quando há mais de um candidato -- nunca resolvida sozinha. Vincular
  (`POST .../link`) e desvincular (`POST .../unlink`) são ações humanas, com `reason` obrigatório e
  trilha de auditoria (`card_payment_reconciliation.link`/`.unlink`), isoladas por família, e alteram
  somente `Transaction.linked_transaction_id` (coluna já existente desde a migração `0004`) nos dois
  lados -- nenhuma migração nova, nenhum valor, tipo, categoria ou exclusão de lançamento é tocado, e
  portanto nenhum efeito sobre INV-002, snapshots, relatórios ou projeção. Interface em
  "Lançamentos" (`app/templates/index.html`, `app/static/app.js`). Testes em
  `tests/test_card_payment_reconciliation.py` (determinístico único, ambíguo, sem candidato, fora da
  janela, vínculo/desvínculo simétrico e não destrutivo, isolamento por família, autorização, trilha
  de auditoria); pendente revisão do engenheiro responsável antes do merge;
- importação em lote: `POST /api/imports/batch` recebe vários arquivos em um único envio (mesma
  conta/cartão e tipo de documento, mesma faixa de aceitação de `POST /api/imports`) e chama, uma vez
  por arquivo, o mesmo pipeline canônico já usado pelo envio individual (`app.api._import_one_document`
  -- parser, classificação, fingerprint, duplicidade, reconciliação e auditoria de
  `app/services/importer.py`/`app/services/reconciliation.py`/`app/services/duplicates.py`); não existe
  uma segunda política de importação. Cada arquivo é persistido e comitado de forma independente (o
  mesmo `db.commit()` por documento que o envio individual já fazia), então um arquivo com falha nunca
  desfaz ou reescreve o resultado já persistido de outro arquivo do mesmo lote -- a resposta é sempre
  a lista real de resultados por arquivo (`imported`/`imported_with_review`/`review_required`/
  `rejected`, com `document_id`, `records`, `reconciliation` quando aplicável), nunca um "sucesso"
  agregado quando algum arquivo falhou. Duplicidade exata por hash, duplicidade provável não destrutiva
  (INV-014), precedência de fonte canônica e reconciliação `unknown` quando falta evidência seguem
  inalteradas por arquivo. Limite de `MAX_BATCH_FILES` arquivos e `MAX_BATCH_TOTAL_MB` combinados por
  envio, além do limite por arquivo já existente (`MAX_UPLOAD_MB`); autenticação e isolamento por
  família idênticos ao envio individual; nenhuma mensagem de erro devolve conteúdo bruto do arquivo.
  Sem migração de banco -- nenhum modelo novo foi necessário, cada `Document`/`Transaction` já carrega
  tudo que o resultado por arquivo precisa. Se um arquivo falha de forma inesperada depois do artefato
  criptografado já ter sido salvo mas antes do `Document` comitar, o artefato órfão (sem linha dona) é
  removido nesse mesmo momento e a falha é registrada no log do servidor -- nunca fica no disco sem
  proveniência, e nunca é silenciosamente engolida. O evento de auditoria agregado
  `document.import_batch` grava `document_id`/`error_category` por item em `outcomes[]` (além de
  `index`/`status`), tornando a trilha de auditoria persistida, por si só, a lineage determinística
  entre um lote e os `Document`s que ele produziu. Interface em "Importações"
  (`app/templates/index.html`, `app/static/app.js`) apenas exibe os campos que o backend já calculou
  por arquivo, sem somar, parsear ou reclassificar nada no navegador; o envio de um único arquivo
  continua usando `POST /api/imports` sem nenhuma mudança de comportamento. Testes em
  `tests/test_batch_import.py` (dois formatos suportados em um lote, lote misto válido/inválido sem
  contaminação cruzada, duplicidade exata dentro do lote e contra dado pré-existente, duplicidade
  provável preservada e sinalizada, reconciliação `unknown` honesta, isolamento por família, limites de
  quantidade/tamanho do lote, falha inesperada em um arquivo sem afetar os demais nem deixar artefato
  criptografado órfão, mensagens de erro sem conteúdo bruto, lineage de auditoria batch → `document_id`
  por item, regressão do envio individual); pendente revisão do engenheiro responsável antes do merge;
- exportação Excel/PDF: `GET /api/reports/export?format=xlsx|pdf` reutiliza `app.api._build_report_payload`
  -- a mesma função que `GET /api/reports` já usava, extraída sem mudança de comportamento -- então ambos
  os formatos representam exatamente os mesmos totais, categorias, períodos e exclusões do relatório
  canônico; nenhum recálculo, reclassificação, deduplicação ou reconciliação paralela. Excel
  (`app/services/report_export.py`, `openpyxl`, já dependência do projeto) grava células de dado puro com
  formatação apenas de apresentação, nunca fórmula de planilha. PDF (mesmo módulo, `pymupdf.insert_htmlbox`,
  já dependência do projeto) é gerado inteiramente no backend a partir do mesmo dict, sem navegador e sem
  dependência nova -- mecanismo diferente do botão "Imprimir / PDF" existente, que continua funcionando
  sem alteração. Tabelas paginadas em blocos de até 28 linhas mais auto-redução de escala evitam corte
  silencioso de dados; uma seção que ainda assim não coubesse falha com exceção em vez de devolver um
  arquivo incompleto. Sem migração -- nenhum modelo novo, só leitura do dict que `/reports` já monta. Nome
  de arquivo carrega apenas o período (`relatorio_<início>_a_<fim>.<formato>`), nunca família/usuário/conta;
  `Content-Disposition: attachment` força download. Autenticação e isolamento por família idênticos a
  `GET /reports` (mesma função, mesmo `household_id`). Interface em "Relatórios" só solicita o formato e
  baixa o arquivo (`downloadReportExport` em `app/static/app.js`), sem somar/parsear nada no navegador.
  Testes em `tests/test_report_export.py` (paridade Excel/PDF com o JSON canônico, período sem
  movimentação com resultado honesto e não fabricado, isolamento por família, content-type/
  content-disposition seguros, formato inválido rejeitado, autenticação obrigatória); pendente revisão do
  engenheiro responsável antes do merge;
- testes com cópias anonimizadas dos documentos reais;
- **go-live de uso manual / fluxos financeiros do dia a dia**: consolidar uma experiência manual
  explícita e simples para `despesa`, `receita`, `transfer`, `investment`, `redemption`, `refund` e
  `reconciliation`, sem exigir que o usuário conheça os tipos internos. O fluxo deve incluir, no
  mínimo, pagamento manual de fatura (cartão + valor + conta pagadora), transferência entre contas,
  aplicação e resgate de investimento, compra parcelada com projeção das parcelas futuras e o caso
  combinado de resgate do Privilège DI seguido de pagamento de fatura. Pagamento de fatura nunca é
  nova despesa (INV-002); aplicação, resgate e transferência nunca viram receita/despesa; fatos já
  observados não podem ser duplicados por projeções. O formulário manual atual em "Lançamentos" só
  expõe despesa, receita, aplicação, resgate e estorno, enquanto a Central Inteligente já representa
  `transfer` e `reconciliation`; este gap deve ser fechado antes do go-live. Exigir testes de fluxo
  completo pela API/UI, preservação de household isolation, auditoria, compatibilidade com dados
  existentes e paridade com o classificador/motor financeiro canônico. Não criar segundo motor de
  cálculo nem inferir automaticamente origem de recursos sem confirmação humana.

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
  ver `docs/ARCHITECTURE.md`) entregue como PR 6;
- tela/menu de Integridade (atrás de `INTEGRITY_UI_ENABLED`), lifecycle humano de findings
  (`acknowledge`/`resolve`/`ignore`/`false-positive`, com motivo obrigatório e audit trail, sem alterar
  dados financeiros automaticamente), banner global persistente para BLOCK, reconciliação de
  documentos visível e Monthly Financial Close (`open`/`review_required`/`trusted`, com `run`/`trust`/
  `reopen` como ações separadas e gates determinísticos) entregues como PR 7;
- PostgreSQL/Alembic real no CI (`alembic upgrade head` a partir de banco vazio e da baseline legada
  `0002`, downgrade da revisão final), doze gates nomeados e obrigatórios (`lint`, `unit`,
  `financial-invariants`, `property-tests`, `parser-reconciliation`, `projection-parity`,
  `snapshot-channel-consistency`, `advisor-contract-security`, `frontend-syntax`, `docker-build`,
  `alembic-migration`, `integration-postgres`), datasets fictícios de regressão reutilizáveis
  (`tests/fixtures/synthetic_household.py`), property tests finais (arredondamento `ROUND_HALF_UP`,
  liquidez zero, déficit maior que saldo, piso de segurança, comissão por recebível, competência,
  idempotência), comando de backfill idempotente e retomável (`app.cli.backfill`, com `--dry-run`) que
  reconcilia documentos legados como `unknown`, classifica duplicidades e reconstrói
  snapshots/integrity runs sem nunca alterar `Transaction`/`Document`/`PayrollRecord`/`Commission`/
  `Obligation`, e runbook de rollout/rollback (`docs/RUNBOOK_PR8_BACKFILL.md`) entregues como PR 8;
  pendente revisão do engenheiro responsável antes do merge.

PR 8 é o último incremento planejado em `docs/INTEGRITY_IMPLEMENTATION_PLAN.md` para o Financial
Integrity Engine. Com PR 8 revisado e mesclado pelo engenheiro responsável, os incrementos PR 0 a
PR 8 estarão todos entregues; até lá, PR 8 permanece aberto aguardando revisão -- nenhum PR desta
série foi mesclado por conta própria.

## Fase 3 — Próximas evoluções

### Em andamento

- worker assíncrono para filas de OCR e áudio (`docs/WORK_ORDER_ASYNC_OCR_AUDIO_WORKER.md`): fila
  durável `capture_processing_jobs` correlacionada a `capture_drafts`/`documents` por household,
  claim atômico idempotente (`UPDATE ... WHERE status = ...`, sem depender de `SELECT ... FOR
  UPDATE SKIP LOCKED`), submissão de `POST /captures/preview` não bloqueante via `BackgroundTasks`
  do FastAPI, reconciliador de recuperação de falhas (`python -m app.cli.capture_worker`) que
  reclama jobs travados em `processing` e falha explicitamente os que esgotaram tentativas, retry
  explícito (`POST /captures/{id}/retry`) e migração aditiva/reversível `0011`; reutiliza
  integralmente os processadores OCR (`pytesseract`/`pymupdf`/`pdfplumber`) e Whisper
  (`faster-whisper`) existentes, sem segundo motor de OCR/transcrição/classificação; a confirmação
  humana continua sendo a única via de criação de `Transaction`/`Obligation`/`PayrollRecord`;
  pendente revisão do engenheiro responsável antes do merge.

### Restante da Fase 3

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
