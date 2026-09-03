# Family Financial Planning

Sistema financeiro familiar **on-premises**, com PostgreSQL, importação de documentos, deduplicação,\nfila de revisão, projeções auditáveis e contrato determinístico de integridade financeira.

O código pode ficar no GitHub, mas extratos, faturas, holerites, banco de dados, backups e chaves permanecem somente no servidor Linux.

## Estado do MVP

Esta primeira versão entrega:

- configuração segura do primeiro administrador;
- acessos individuais para membros da mesma família, com gestão restrita ao administrador;
- cadastro de contas, cartões, conta central de liquidez e titulares;
- importação de CSV, OFX/QFX e PDF com texto selecionável;
- parsers validados para extrato Itaú, fatura Itaú em duas colunas, CSV Nubank e holerite;
- criptografia dos documentos armazenados;
- bloqueio de arquivo já importado por SHA-256;
- detecção de possível duplicidade entre períodos sobrepostos;
- classificação automática com nível de confiança;
- fila de revisão para itens ambíguos;
- conciliação de pagamentos de fatura, transferências internas e aplicação/resgate;
- cadastro de comissões com imposto individual por recebível;
- importação automática de holerites e cadastro de 13º/adicional líquido de férias;
- compromissos únicos ou recorrentes;
- projeção sem comissão, no mês esperado e com atraso conservador;
- Privilège DI tratado como conta central de liquidez, recebendo sobras e cobrindo déficits;
- rendimento do Privilège DI sobre o saldo inicial de cada mês;
- teto de gastos, benefícios VA/VR e saldo mínimo de segurança;
- visão mensal navegável, com totais e categorias separados por competência;
- identidade visual própria e interface responsiva sem rolagem lateral no celular;
- relatórios comparativos configuráveis de 1 a 12 meses e visão por ano-calendário;
- gráficos de evolução, ranking de categorias, médias, variações e destaques do período;
- entradas e saídas operacionais separadas de aplicações, resgates e estornos;
- detalhamento mensal por banco, conta corrente e cartão, sem duplicar o pagamento da fatura;
- lançamento manual de despesa, receita, aplicação, resgate e reembolso;
- criação automática de uma nova categoria ao usar “Outra categoria” no lançamento manual;
- exclusão auditada de registros manuais e desativação segura de acessos;
- revisão assistida com correção de categoria e decisão de considerar ou ignorar cada item;
- alertas de obrigações com 30 dias de antecedência e destaque nos últimos 7 dias;
- consultor conversacional local para compras, fluxo mensal, vencimentos e cortes;
- central “Lançar agora” com texto, gravação de áudio, foto, PDF, CSV e OFX;
- OCR local de comprovantes, boletos, faturas, extratos e holerites;
- transcrição local de áudio e prévia editável antes da gravação;
- classificação assistida pelo Codex somente quando a regra local estiver ambígua;
- consultor com cálculo local e explicação opcional pelo Codex, sem acesso direto ao banco;
- acesso remoto privado para os celulares de Vinicius e Kelly por Tailscale Serve;
- consolidação automática de lançamentos repetidos entre a planilha e importações históricas;
- plano de cortes por categoria com metas iniciais conservadoras, sem prometer corte integral;
- backup diário do PostgreSQL com retenção configurável;
- trilha de auditoria das alterações.

## Arquitetura

```text
Celular / navegador via LAN ou Tailscale
        |
        v
FastAPI + interface web ---- resumo sanitizado ----> Codex isolado
   |              |
   v              v
PostgreSQL   Documentos criptografados
   |
   v
Backup diário local
```

O Docker Compose cria `app`, `db`, `backup` e o serviço opcional `advisor`. O contêiner do Codex não recebe credenciais do banco nem monta o volume dos documentos. O banco, os arquivos, os modelos locais e os backups usam volumes persistentes e não são publicados no Git.

## Servidor recomendado

- Ubuntu Server 24.04 LTS x86-64;
- 4 vCPU;
- 8 GB de RAM para o MVP sem modelo de IA local;
- 100 GB de disco, ajustado à retenção de documentos e backups;
- IP fixo na rede local;
- Docker Engine e Docker Compose v2;
- sincronização de horário habilitada;
- backup externo criptografado em mídia ou storage distinto.

Para OCR pesado ou modelo local futuro, planeje ao menos 16 GB de RAM e dimensione CPU/GPU separadamente.

## Instalação no Linux

### 1. Instalar Docker

Use o repositório oficial do Docker para Ubuntu:

<https://docs.docker.com/engine/install/ubuntu/>

Confirme:

```bash
docker --version
docker compose version
```

### 2. Baixar o projeto

```bash
git clone https://github.com/viniciusmiranda022-pixel/Family-financial-planning.git
cd Family-financial-planning
```

### 3. Inicializar

```bash
chmod +x scripts/*.sh
./scripts/bootstrap.sh
```

O instalador gera automaticamente:

- senha aleatória do PostgreSQL;
- chave de sessão;
- chave de criptografia dos documentos;
- arquivo `.env` com permissão `600`.

Depois, acesse:

```text
http://IP_DO_SERVIDOR:8080
```

No primeiro acesso será criado o administrador local.

Depois do primeiro acesso, siga esta ordem:

1. configure o saldo do Privilège DI, o piso de segurança, salário líquido, VA/VR, teto de caixa e taxa estimada de rendimento;
2. cadastre as contas e cartões;
3. importe primeiro os holerites, depois extratos e faturas;
4. resolva a fila de revisão antes de confiar nos totais;
5. cadastre comissões e compromissos futuros;
6. em **Acessos**, crie o usuário individual de cada membro da família;
7. confira os relatórios comparativos, o plano de cortes e os três cenários da projeção.

Para usar o Codex com a assinatura do ChatGPT no Windows, sem configurar uma chave de API,
abra um PowerShell normal na pasta do projeto e execute:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-codex-windows.ps1
```

Esse modo executa somente o consultor no Windows e mantém aplicação, PostgreSQL e documentos no
Docker. Ele é recomendado quando a rede do Docker/WSL não alcança os serviços da OpenAI.

Em um servidor Linux cujo Docker tenha saída HTTPS normal, use:

```bash
sh ./scripts/setup-codex.sh
```

Para acesso privado fora de casa, siga [docs/TAILSCALE.md](docs/TAILSCALE.md). Não abra a porta `8090` no roteador e não habilite o Tailscale Funnel.

Na tela **Lançamentos**, despesas entram no consumo do teto. Aplicações e resgates atualizam o saldo do Privilège DI, mas não entram nas receitas ou saídas operacionais. A visão geral mostra a sobra que deve ir para essa conta ou o déficit que precisa ser coberto por ela. Registros importados podem ser ignorados no cálculo sem perder a fonte; somente lançamentos manuais podem ser apagados definitivamente.

O **Consultor** calcula localmente o veredito com regras financeiras auditáveis. Se o Codex estiver autenticado, recebe apenas pergunta, totais agregados, projeções e o veredito para produzir uma explicação; não recebe documentos, credenciais ou conexão com o banco. Para compras, informe pagamento à vista ou quantidade de parcelas e juros. Sem Codex, o consultor continua operando localmente.

Na central **Lançar agora**, toda extração gera uma prévia. Confira data, valor, conta, categoria e tipo antes de confirmar. O primeiro áudio pode demorar mais porque o modelo pequeno de transcrição é baixado para o volume local.

## Carga da planilha consolidada

Para preencher o sistema com o plano já consolidado, mantenha a planilha fora do Git e execute a carga pelo Ubuntu/WSL. O comando lê o arquivo diretamente no computador, grava a cópia criptografada no volume local e não envia os dados financeiros para o GitHub.

Primeiro gere uma prévia, sem alterar o banco:

```bash
./scripts/import-plan.sh "/mnt/c/Users/ViniciusMiranda/Downloads/Plano_Financeiro_Chacara_Vinicius_v3.xlsx"
```

Confira os totais exibidos e confirme a gravação:

```bash
./scripts/import-plan.sh "/mnt/c/Users/ViniciusMiranda/Downloads/Plano_Financeiro_Chacara_Vinicius_v3.xlsx" --apply
```

A carga inclui:

- perfil financeiro, saldo do Privilège DI, VA, VR, teto e piso de segurança;
- contas Itaú, cartão Itaú familiar, Nubank e Privilège DI;
- três holerites reais da Kelly e três eventos estimados de 13º/férias, devidamente identificados;
- quatro comissões, cada uma com imposto de 6% e cenário de atraso de 60 dias;
- dez parcelas mensais e três reforços da chácara;
- lançamentos consolidados das abas `Cartão - Dados`, `Nubank - Dados` e `Banco - Dados`;
- envelopes de corte e pendências selecionadas da aba `Revisar`.

O importador é idempotente: executar novamente atualiza registros reconhecidos e não duplica a carga. Depois dela, não reimporte os mesmos documentos históricos já cobertos pela planilha; use a tela **Importações** apenas para arquivos novos posteriores a 17/08/2026.

## Diagnóstico mensal para conferência

Para investigar um total mensal sem expor credenciais ou os documentos brutos, gere um pacote de diagnóstico somente de leitura:

```bash
./scripts/export-diagnostics.sh 2026-08
```

O ZIP será criado na pasta `diagnostics` e conterá resumo, lançamentos, documentos de origem, contas, revisões abertas e possíveis sobreposições. Ele inclui descrições e valores financeiros; compartilhe somente por um canal privado.

## Atualização

```bash
git pull
docker compose up -d --build
```

As migrações do banco são executadas automaticamente antes de iniciar a aplicação.

## Backup

O contêiner `backup` gera diariamente um dump no volume `backup_data`. Para listar os arquivos:

```bash
docker compose exec backup ls -lh /backups
```

Um backup que permanece no mesmo servidor **não é suficiente** contra falha de disco ou ransomware. Copie periodicamente os dumps para mídia externa criptografada.

Para copiar um arquivo ao host:

```bash
docker compose cp backup:/backups/family_finance_AAAAMMDDTHHMMSSZ.dump ./backups/
```

Restauração manual:

```bash
set -a
. ./.env
set +a
./scripts/restore.sh ./backups/arquivo.dump
```

## Segurança

- nunca comite `.env`, documentos ou backups;
- mantenha o repositório privado;
- restrinja a porta `8080` à rede local no firewall;
- use HTTPS antes de permitir acesso por Wi-Fi não confiável, VPN ou internet;
- não encaminhe a porta do sistema diretamente no roteador;
- use Tailscale Serve, contas individuais e o usuário próprio da Kelly;
- faça backup também do `.env`: sem a chave, documentos antigos não podem ser descriptografados;
- não restaure dumps de origem desconhecida.

Consulte [docs/SECURITY.md](docs/SECURITY.md) antes de colocar o servidor em produção.

## Desenvolvimento e testes

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest -q
ruff check .
```

`pytest -q` roda a suíte completa sobre SQLite. O CI (`.github/workflows/ci.yml`) exige, além
disso, doze gates nomeados e obrigatórios -- lint, a suíte completa, invariantes financeiros,
property tests, parser/reconciliação, paridade de projeção, consistência dashboard/relatórios,
segurança do contrato do Advisor, sintaxe do frontend, build das imagens Docker e, com PostgreSQL
real (não apenas SQLite), migração Alembic e idempotência do backfill. Para rodar os testes que
exigem PostgreSQL localmente:

```bash
export POSTGRES_TEST_DATABASE_URL=postgresql+psycopg://family:family@localhost:5432/family_finance
pytest -q tests/test_postgresql_integration.py
```

Sem essa variável definida, esses testes são pulados (skip), não falham.

### Backfill (reprocessamento de dados existentes)

`python -m app.cli.backfill` reconcilia documentos legados sem evidência retida como `unknown`,
classifica duplicidades ainda não avaliadas e reconstrói snapshots/integrity runs para famílias já
cadastradas, usando os mesmos serviços determinísticos do fluxo de importação -- nunca escreve em
`Transaction`, `Document`, `PayrollRecord`, `Commission` ou `Obligation`. É idempotente e retomável:
rodar de novo após uma falha parcial, ou repetir a execução, reproduz o mesmo estado final.

```bash
python -m app.cli.backfill --dry-run                 # relatório do que mudaria, nada é gravado
python -m app.cli.backfill                            # todas as famílias
python -m app.cli.backfill --household "Família X"    # uma família específica
python -m app.cli.backfill --from 2026-01 --to 2026-06 # intervalo de competência explícito
```

Consulte [docs/RUNBOOK_PR8_BACKFILL.md](docs/RUNBOOK_PR8_BACKFILL.md) para pré-condições, plano de
rollback e critérios de parada antes de rodar em uma base com dados reais.

## Limitações conscientes do MVP

- OCR e transcrição podem errar; nenhum resultado é gravado sem prévia e confirmação.
- PDFs bancários podem mudar de layout; arquivos inválidos, incompletos ou não reconhecidos vão para revisão.
- o Codex é opcional; quando conectado, seu uso segue os limites da assinatura autenticada do ChatGPT.
- possíveis duplicidades são excluídas provisoriamente do cálculo, mas nunca apagadas.
- o sistema não acessa internet banking e não armazena credenciais bancárias.
- nenhuma classificação automática substitui a revisão do usuário quando a confiança é baixa.

## Documentação

- [Arquitetura](docs/ARCHITECTURE.md)
- [Regras financeiras](docs/FINANCIAL_RULES.md)
- [Segurança e operação](docs/SECURITY.md)
- [Acesso remoto com Tailscale](docs/TAILSCALE.md)
- [Central inteligente e Codex](docs/INTELLIGENCE.md)
- [Roteiro do produto](docs/ROADMAP.md)
- [Runbook de backfill (rollout/rollback)](docs/RUNBOOK_PR8_BACKFILL.md)
