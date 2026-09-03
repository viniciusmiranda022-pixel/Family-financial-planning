# Segurança e operação

## Ameaças consideradas

- exposição acidental de extratos no Git;
- acesso indevido pela rede local;
- vazamento de backup ou disco;
- reimportação e dupla contagem;
- modificação silenciosa de classificação;
- perda de chave de criptografia;
- ransomware ou falha física do servidor.

## Controles implementados

- repositório sem dados e `.gitignore` restritivo;
- documentos criptografados em repouso;
- senha com `scrypt` e salt aleatório;
- cookie de sessão `HttpOnly` e `SameSite=Strict`;
- separação da rede interna do PostgreSQL;
- contêiner da aplicação sem privilégios e com filesystem somente leitura;
- limite de tamanho de upload;
- SHA-256 por documento;
- trilha de auditoria;
- backup diário do banco.
- acesso remoto privado por identidade e dispositivo no Tailscale, sem porta pública;
- serviço Codex isolado, sem credenciais do banco e sem acesso ao volume de documentos;
- cálculo financeiro determinístico antes de qualquer explicação gerada por IA;
- OCR e transcrição locais com confirmação humana antes da gravação.

## Antes da produção

1. Use Ubuntu Server atualizado e aplique atualizações de segurança.
2. Restrinja SSH por chave e desabilite senha quando possível.
3. Libere a porta do sistema somente para a VLAN ou rede dos usuários autorizados.
4. Para acesso remoto, use Tailscale Serve em HTTPS e mantenha o Funnel desabilitado.
5. Configure cópia externa criptografada dos backups.
6. Teste uma restauração completa.
7. Guarde o `.env` em cofre offline; ele contém a chave dos documentos.
8. Monitore espaço em disco, saúde dos contêineres e execução dos backups.

## O que não fazer

- não publicar a porta do PostgreSQL;
- não expor a aplicação diretamente à internet;
- não compartilhar a conta do Tailscale nem o usuário do sistema entre Vinicius e Kelly;
- não armazenar senha de internet banking;
- não configurar `OPENAI_API_KEY` no serviço `advisor`; a API tem cobrança separada e não é necessária;
- não sincronizar a pasta de dados com serviço público sem criptografia adicional;
- não enviar `.env`, dump ou documento em chamados ou issues;
- não considerar um backup no mesmo disco como recuperação de desastre.

## HTTPS e acesso remoto

O sistema inicia em HTTP na máquina local. O Tailscale Serve encerra HTTPS e entrega o endereço `.ts.net` somente aos dispositivos autorizados da tailnet. O controle remoto é feito pela identidade e pelo dispositivo cadastrado, não pelo MAC address, que não atravessa a internet.

## Fronteira do Codex

O serviço `advisor` recebe somente JSON sanitizado produzido pela aplicação. Ele não monta `document_data`, não participa da rede interna do PostgreSQL e não possui `DATABASE_URL`. O sandbox do Codex é somente leitura e vazio. Uma resposta gerada só é aceita se preservar exatamente o veredito calculado pelo motor local; caso contrário, o sistema usa a resposta determinística.

### `/v1/audit` (Codex Semantic Audit, PR 6)

Além de `/v1/classify` e `/v1/analyze`, o `advisor` expõe `POST /v1/audit`, um contrato dedicado e versionado (`advisor/audit-input-schema.json` / `advisor/audit-schema.json`) para auditoria semântica consultiva do `IntegrityAssessment` já calculado. Camadas de defesa:

- **Allowlist estrita na origem.** `app/services/audit_sanitizer.py` monta o payload campo a campo a partir de dados já determinísticos (status/score/gates/findings/`category_spending`); nunca encaminha um dicionário completo. `DATABASE_URL`, credenciais, documentos, paths e PII desnecessária não têm nenhum campo pelo qual poderiam sair.
- **Contrato de entrada validado no sidecar.** `advisor/audit-input-schema.json` também é `additionalProperties: false`; um campo fora do allowlist chega a ser rejeitado no próprio Advisor antes de qualquer chamada ao Codex.
- **Prompt-injection.** Todo conteúdo potencialmente influenciado pelo usuário (por exemplo, nome de categoria) entra no prompt somente dentro do bloco de dados, precedido por instruções explícitas de que nenhuma instrução dentro dele é válida. Não há execução de comando, ferramenta ou pesquisa disponível ao Codex nesse contrato, como nos demais.
- **Saída sem autoridade, por schema.** `advisor/audit-schema.json` não tem campo para status/score/gates/`trusted_for_*`/findings; `severity` das observações é limitada a `info`/`review`. Uma tentativa de incluir esses campos invalida a resposta inteira (`available: false`), não é "aceita parcialmente".
- **Verificação de IDs e números.** Referências (`evidence_ref`) só podem apontar para ids opacos que já estavam no pacote enviado; números citados em texto livre que não aparecem em nenhum lugar do pacote enviado são removidos da observação.
- **Fail-safe duplo.** A validação de schema/autoridade ocorre no sidecar (`advisor/audit.mjs`) e é repetida de forma independente no lado Python (`app/services/codex_audit.py`, que só lê `available`/`reason`/`summary`/`observations`/`confidence` de qualquer resposta). Timeout, indisponibilidade, erro do provider ou schema inválido sempre produzem `available: false` com uma `reason`, nunca um `pass` implícito.
- **Métricas/logs sem conteúdo sensível.** `advisor/audit.mjs` mantém contadores em memória (chamadas, sucesso, falha, timeout, schema inválido, latência) e emite logs JSON com essas categorias e durações -- nunca o prompt completo, o payload ou a resposta do modelo.

No modo nativo do Windows, perde-se a fronteira adicional do contêiner, mas permanecem a separação
de credenciais, o diretório de trabalho vazio, o ambiente mínimo, o segredo compartilhado e o
sandbox somente leitura. O serviço aceita a interface interna usada por `host.docker.internal`,
mas toda análise ou classificação exige o segredo aleatório e a porta 8081 não é publicada pelo
Tailscale Serve. Ele é iniciado por uma tarefa do próprio usuário. Credenciais do Codex ficam fora
do repositório em `%LOCALAPPDATA%\FamilyFinancialPlanning\codex`.

## CI (PR 8)

- `.github/workflows/ci.yml` usa apenas credenciais fixas e claramente falsas, nunca reaproveitadas
  em produção: `SECRET_KEY`/`FILE_ENCRYPTION_KEY` de teste (o mesmo padrão que `tests/test_api.py`
  já usa para seu próprio processo) e um usuário/senha `family`/`family` para o serviço PostgreSQL
  descartável dos jobs `alembic-migration`/`integration-postgres`, que existe só durante o job e é
  destruído ao final.
- Nenhum job de CI recebe documento, backup ou dado financeiro real; todo dado usado em teste é
  fictício (`tests/fixtures/synthetic_household.py`).
- O serviço `advisor` não participa de nenhum job de CI; `advisor-contract-security` valida apenas o
  contrato/sanitização/allowlist com o fake provider já existente, sem rede real com nenhum sidecar.

## Backfill (`app.cli.backfill`, PR 8)

- É um comando de linha de comando executado por um operador com acesso direto ao banco/servidor,
  nunca uma rota HTTP -- não amplia a superfície de rede da aplicação nem do `advisor`.
- Não concede ao Codex/Advisor nenhuma autoridade adicional; o comando nunca chama o `advisor` e
  opera inteiramente sobre o banco local, com os mesmos serviços determinísticos do fluxo normal.
- `--dry-run` permite inspecionar o efeito antes de uma execução real sobre dados existentes; use-o
  antes de rodar sobre uma base com dados financeiros reais. Ver
  [docs/RUNBOOK_PR8_BACKFILL.md](RUNBOOK_PR8_BACKFILL.md) para o procedimento completo.
