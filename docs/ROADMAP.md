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
