# Plano de migração — Oracle Cloud Always Free

## Status

**Próxima iniciativa de infraestrutura registrada em `main`.**

Este documento é a fonte de verdade para a migração da hospedagem do Family Finance do host local Windows/WSL2 para Oracle Cloud Infrastructure (OCI), mantendo o requisito de **R$ 0,00/mês**.

Epic GitHub: #59.

A existência deste plano **não autoriza execução paralela** nem altera a disciplina vigente do projeto: cada slice deve ser executado individualmente com Work Order próprio, branch isolada, Draft PR, revisão técnica, gates/CI e merge somente pelo engenheiro responsável.

## Objetivo

Permitir que o Family Finance continue disponível para Vinicius e Kelly mesmo com o computador local desligado, preservando:

- Docker Compose;
- PostgreSQL;
- documentos/uploads;
- Advisor e demais serviços existentes;
- regras financeiras e invariants;
- acesso remoto privado;
- backup e restore;
- capacidade de rollback;
- custo recorrente de **R$ 0,00**.

## Requisito não negociável — custo zero

A arquitetura só é aprovada enquanto todos os recursos utilizados permanecerem dentro dos limites **Always Free vigentes** da Oracle.

Antes de criar qualquer recurso, o executor deve validar a elegibilidade na documentação oficial da Oracle e na OCI Console.

Se qualquer etapa indicar:

- custo estimado maior que zero;
- recurso não marcado/elegível como Always Free;
- necessidade de upgrade para Pay As You Go;
- dependência de crédito promocional/trial;
- ambiguidade material de cobrança;

**PARAR a execução e abrir Technical Challenge.**

Não substituir automaticamente um recurso gratuito indisponível por alternativa paga.

## Arquitetura alvo inicial

```text
Vinicius / Kelly
       |
       v
   Tailscale
       |
       v
 Tailscale Serve
   HTTPS privado
       |
       v
OCI Ampere A1 Flex
1 OCPU / 2 GB RAM (alvo inicial)
       |
       v
Docker Compose
 |- Family Finance
 |- PostgreSQL
 |- Advisor
 |- OCR/serviços existentes
 `- documentos/uploads persistentes
```

O sizing de **1 OCPU / 2 GB RAM** é apenas o ponto inicial. O slice ORA-00 deve medir e validar a carga real, principalmente PostgreSQL, OCR/Tesseract, Whisper e Advisor. Se houver evidência de insuficiência, usar o menor sizing tecnicamente adequado que continue dentro do Always Free.

## Segurança e exposição

A arquitetura normativa já adotada pelo projeto continua válida:

- acesso remoto privado via Tailscale;
- HTTPS via Tailscale Serve;
- Tailscale Funnel desabilitado;
- aplicação não publicada diretamente na Internet;
- PostgreSQL nunca publicado;
- Advisor e portas internas nunca publicados;
- SSH público apenas se indispensável ao bootstrap e temporariamente restrito; após Tailscale funcionar, deve ser fechado;
- segredos, tokens, auth keys, `.env`, dumps e documentos reais nunca entram no Git ou nos artefatos de CI.

O trabalho entregue no PR #55 deve ser reutilizado; não criar um segundo modelo de proxy/acesso apenas por preferência de ferramenta.

## Risco de reclaim da OCI

Instâncias Always Free podem ser recuperadas pela Oracle conforme a política vigente de ociosidade.

A mitigação aprovada é:

1. dimensionar a VM de acordo com a carga real;
2. manter backup externo criptografado e testado;
3. monitorar saúde, capacidade e uso legítimo;
4. documentar reconstrução da VM;
5. manter rollback operacional.

**É proibido criar workload artificial cujo propósito seja consumir CPU, memória ou rede para aparentar utilização e tentar contornar a política de reclaim.**

Health-checks, backups, rotação de logs e inspeções operacionais legítimas são permitidos, mas não devem ser tratados como mecanismo garantido contra reclaim.

## Sequência oficial de slices

### ORA-00 — Discovery ARM64, sizing e ADR

Issue: #60.

Gate técnico anterior ao provisionamento.

Entregas mínimas:

- inventário de containers, imagens e dependências nativas;
- compatibilidade `linux/arm64` comprovada;
- validação de Docker build ARM64;
- avaliação de PostgreSQL, OCR/Tesseract, Whisper e Advisor;
- baseline de CPU/RAM/disco;
- sizing mínimo aprovado;
- validação Tailscale/Tailscale Serve em ARM64;
- ADR formal de hosting e rollback para host local.

Nenhuma migração real ocorre neste slice.

### ORA-01 — Provisionamento OCI Always Free

Issue: #61.

Depende de ORA-00.

Entregas mínimas:

- VM Ampere A1 Flex ARM64 elegível ao Always Free;
- sizing aprovado pelo ORA-00;
- storage dentro dos limites gratuitos vigentes;
- Linux endurecido;
- SSH por chave;
- firewall mínimo;
- Tailscale instalado e validado;
- portas públicas desnecessárias fechadas;
- checklist de custo antes/depois do provisionamento;
- teardown documentado.

Nenhum dado financeiro real deve ser migrado neste slice.

### ORA-02 — Docker/Compose ARM64 e perfil cloud

Issue: #62.

Depende de ORA-01.

Entregas mínimas:

- Dockerfile/Compose compatíveis com ARM64;
- persistência explícita do PostgreSQL e documentos;
- restart policy após reboot;
- healthchecks;
- integração com Tailscale Serve existente;
- migrations Alembic executadas de forma controlada;
- secrets fora do Git;
- smoke test com dados sintéticos;
- preservação da execução local/rollback.

Não modificar regras financeiras para adaptar a infraestrutura.

### ORA-03 — Backup externo, restore e reconstrução

Issue: #63.

Depende de ORA-02.

Este slice é o principal controle contra perda causada por reclaim/terminação da VM.

Entregas mínimas:

- `pg_dump` restaurável;
- backup de uploads/documentos e volumes necessários;
- criptografia antes de o backup sair da VM;
- checksums e manifesto;
- armazenamento externo ao compute somente dentro do Always Free vigente;
- retenção limitada ao teto gratuito;
- bloqueio antes de potencial cobrança;
- restore de ponta a ponta testado;
- runbook de reconstrução de VM perdida/reclaimed.

A chave de criptografia não pode ficar no Git nem junto do backup.

### ORA-04 — Migração real e cutover

Issue: #64.

Depende de ORA-03 e de restore comprovadamente funcional.

Entregas mínimas:

- backup final do host local;
- freeze curto de escrita;
- dump consistente do PostgreSQL;
- transferência segura de banco e documentos;
- restore no OCI;
- validação de schema/Alembic;
- validação de autenticação e household isolation;
- comparação determinística origem x destino;
- validação de Transactions, Documents, Obligations, contas/cartões, saldos/snapshots e totais de controle;
- ativação do acesso privado via Tailscale Serve;
- ambiente local mantido temporariamente como fallback, mas sem dual-write;
- rollback documentado.

Qualquer divergência financeira bloqueia o go-live.

### ORA-05 — Observabilidade, operação e custo final

Issue: #65.

Depende de ORA-04.

Entregas mínimas:

- monitoramento de CPU, RAM, rede e disco;
- saúde de containers e PostgreSQL;
- idade/status do último backup;
- controle do espaço usado pelo storage gratuito;
- health-check seguro da aplicação;
- rotinas legítimas de backup, verificação e log rotation;
- procedimento de rebuild após reclaim;
- checklist recorrente de custo;
- rotina de atualização e rollback;
- validação final de **R$ 0,00/mês**.

## Ordem de execução e governança

As issues #60 a #65 representam uma **fila ordenada**, não trabalhos paralelos.

Fluxo obrigatório por slice:

```text
main/documentação normativa
        |
        v
Work Order do slice
        |
        v
branch isolada
        |
        v
Claude implementa
        |
        v
Draft PR
        |
        v
revisão técnica/adversarial
        |
        v
CI + gates
        |
        v
merge pelo engenheiro responsável
        |
        v
próximo slice
```

Claude não pode fazer merge.

O executor pode e deve contestar um Work Order quando houver evidência técnica concreta; divergência não resolvida bloqueia merge.

## Relação com a Fase 4 existente

Este plano complementa a **Fase 4 — Operação endurecida** de `docs/ROADMAP.md`.

O proxy HTTPS automatizado já foi entregue no PR #55 e deve ser preservado.

Existem trabalhos da Fase 4 já iniciados/planejados, incluindo perfis administrador/consulta e autenticação multifator. A migração OCI **não autoriza atropelar slices normativos anteriores**. Antes de iniciar ORA-00, o engenheiro deve verificar o estado dos PRs/slices em andamento e decidir explicitamente a ordem conforme `docs/ROADMAP.md`.

A infraestrutura OCI não substitui MFA, backup externo, observabilidade ou atualização/rollback; ao contrário, ORA-03 e ORA-05 devem integrar esses controles ao novo host sem duplicar motores ou contratos existentes.

## Invariants e integridade financeira

A mudança de hosting não autoriza alterações em:

- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- classificação;
- reconciliação;
- projeções;
- snapshots;
- competência;
- parsers;
- regras de cartões;
- Privilège DI;
- qualquer fato financeiro existente.

Infraestrutura deve se adaptar ao produto; o produto não deve ter suas regras alteradas para facilitar a migração.

## Gate final da iniciativa

A Epic #59 só pode ser considerada concluída quando todos os seguintes pontos forem verdadeiros:

- o Family Finance funciona normalmente com o computador local desligado;
- Vinicius e Kelly conseguem acessar pelo modelo privado aprovado;
- dados atuais foram migrados com paridade comprovada;
- PostgreSQL e uploads estão persistentes;
- backup externo criptografado funciona;
- restore foi testado;
- rebuild após perda/reclaim está documentado;
- nenhuma porta/serviço interno está exposto publicamente;
- nenhum segredo/dado financeiro real foi versionado;
- rollback local está documentado;
- gates financeiros e de CI aplicáveis permanecem verdes;
- todos os recursos OCI usados foram reconfirmados como elegíveis ao Always Free;
- custo recorrente esperado foi validado como **R$ 0,00**.

## Referências internas

- Epic: #59
- ORA-00: #60
- ORA-01: #61
- ORA-02: #62
- ORA-03: #63
- ORA-04: #64
- ORA-05: #65
- `docs/ROADMAP.md`
- `docs/ARCHITECTURE.md`
- `docs/SECURITY.md`
- `docs/TAILSCALE.md`
- `docs/FINANCIAL_RULES.md`
- `docs/FINANCIAL_INVARIANTS.md`
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`
- `docs/WORK_ORDER_AUTOMATED_HTTPS_PROXY.md`
