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

No modo nativo do Windows, perde-se a fronteira adicional do contêiner, mas permanecem a separação
de credenciais, o diretório de trabalho vazio, o ambiente mínimo, o segredo compartilhado e o
sandbox somente leitura. O serviço aceita a interface interna usada por `host.docker.internal`,
mas toda análise ou classificação exige o segredo aleatório e a porta 8081 não é publicada pelo
Tailscale Serve. Ele é iniciado por uma tarefa do próprio usuário. Credenciais do Codex ficam fora
do repositório em `%LOCALAPPDATA%\FamilyFinancialPlanning\codex`.
