# Family Financial Planning

Sistema financeiro familiar **on-premises**, com PostgreSQL, importação de documentos, deduplicação, fila de revisão e projeções auditáveis.

O código pode ficar no GitHub, mas extratos, faturas, holerites, banco de dados, backups e chaves permanecem somente no servidor Linux.

## Estado do MVP

Esta primeira versão entrega:

- configuração segura do primeiro administrador;
- cadastro de contas, cartões, investimentos e titulares;
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
- rendimento do investimento sobre o saldo inicial de cada mês;
- teto de gastos, benefícios VA/VR e saldo mínimo de segurança;
- plano de cortes por categoria, com média observada, novo teto e economia mensal possível;
- backup diário do PostgreSQL com retenção configurável;
- trilha de auditoria das alterações.

## Arquitetura

```text
Navegador na rede local
        |
        v
FastAPI + interface web
   |              |
   v              v
PostgreSQL   Documentos criptografados
   |
   v
Backup diário local
```

O Docker Compose cria três serviços: `app`, `db` e `backup`. O banco e os documentos usam volumes persistentes e não são publicados no Git.

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

1. configure saldo investido, salário líquido, VA/VR, teto de caixa e taxa estimada do investimento;
2. cadastre as contas e cartões;
3. importe primeiro os holerites, depois extratos e faturas;
4. resolva a fila de revisão antes de confiar nos totais;
5. cadastre comissões e compromissos futuros;
6. confira o plano de cortes e os três cenários da projeção.

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

## Limitações conscientes do MVP

- PDFs escaneados sem camada de texto são encaminhados para revisão; OCR entra na próxima fase.
- PDFs bancários podem mudar de layout; arquivos inválidos, incompletos ou não reconhecidos vão para revisão.
- possíveis duplicidades são excluídas provisoriamente do cálculo, mas nunca apagadas.
- o sistema não acessa internet banking e não armazena credenciais bancárias.
- nenhuma classificação automática substitui a revisão do usuário quando a confiança é baixa.

## Documentação

- [Arquitetura](docs/ARCHITECTURE.md)
- [Regras financeiras](docs/FINANCIAL_RULES.md)
- [Segurança e operação](docs/SECURITY.md)
- [Roteiro do produto](docs/ROADMAP.md)
