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

## Antes da produção

1. Use Ubuntu Server atualizado e aplique atualizações de segurança.
2. Restrinja SSH por chave e desabilite senha quando possível.
3. Libere a porta do sistema somente para a VLAN ou rede dos usuários autorizados.
4. Coloque proxy reverso HTTPS antes de acesso por VPN, Wi-Fi não confiável ou redes distintas.
5. Configure cópia externa criptografada dos backups.
6. Teste uma restauração completa.
7. Guarde o `.env` em cofre offline; ele contém a chave dos documentos.
8. Monitore espaço em disco, saúde dos contêineres e execução dos backups.

## O que não fazer

- não publicar a porta do PostgreSQL;
- não expor a aplicação diretamente à internet;
- não armazenar senha de internet banking;
- não sincronizar a pasta de dados com serviço público sem criptografia adicional;
- não enviar `.env`, dump ou documento em chamados ou issues;
- não considerar um backup no mesmo disco como recuperação de desastre.

## HTTPS

O MVP inicia em HTTP para uma LAN controlada. Para uso real com múltiplos dispositivos, adicione Caddy ou Nginx com certificado interno e altere `COOKIE_SECURE=true`.
