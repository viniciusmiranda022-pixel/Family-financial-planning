# Acesso remoto privado com Tailscale

O Tailscale cria a rede privada usada por Vinicius e Kelly para acessar o sistema fora de casa. Ele não classifica lançamentos e não substitui o login do sistema financeiro.

## Arquitetura

1. O Docker continua executando o sistema localmente na porta configurada em `APP_PORT` (`.env`; `8080` por padrão em `.env.example`/`compose.yaml`).
2. O Tailscale instalado no Windows publica essa porta somente dentro da tailnet.
3. Cada pessoa usa uma conta individual do Tailscale e um usuário individual no sistema financeiro.
4. Nenhuma porta do roteador é aberta e o recurso público **Funnel** não deve ser habilitado.

## Configuração automatizada (Fase 4)

`scripts/setup-tailscale.ps1` automatiza e verifica esse estado: ele falha explicitamente se o
Tailscale não estiver instalado/conectado ou se a aplicação não responder saudável, reaplica a
publicação HTTPS de forma idempotente (repetir a execução converge para o mesmo estado, sem
duplicar regras) e, ao final, verifica que o endereço publicado na porta 443 aponta para a
aplicação, que essa porta está corretamente configurada como HTTPS e que o Tailscale Funnel
continua desabilitado nela. Se o novo apply falhar ou a pós-verificação falhar depois dele, a
reaplicação é tratada como transacional: quando já existia uma publicação própria funcionando antes
da execução, o script restaura exatamente esse alvo anterior (e o registro local correspondente) em
vez de deixar a porta 443 vazia; quando não havia publicação anterior, a porta 443 termina vazia e
sem registro local, como antes. Se mesmo a restauração falhar, o script para com uma mensagem
explícita de intervenção manual em vez de presumir sucesso — nunca deixa uma exposição nova sem
verificação, nem finge que um estado anterior foi recuperado sem confirmar.

A automação só mexe na porta 443 (a única superfície que esta aplicação usa) e nunca executa
`tailscale serve reset`, que apagaria configurações de Serve/Funnel de qualquer outro serviço
eventualmente publicado pelo mesmo nó Tailscale. Ela também nunca assume, só pela forma, que uma
publicação já existente na porta 443 é sua: mantém um registro local não sensível (apenas a URL de
destino, nunca versionado — veja `.gitignore`) do que ela mesma publicou da última vez, e só reaplica
por cima quando o estado atual da porta 443 bate exatamente com esse registro. Qualquer outra coisa
já publicada na 443 (de outro serviço, de uma configuração manual anterior, ou com drift em relação
ao registro local) faz o script falhar explicitamente sem alterar nada — resolva manualmente com
`tailscale serve status` antes de tentar novamente. Detalhes de implementação e das garantias
testadas em `scripts/lib/TailscaleProxy.psm1` e `tests/tailscale_proxy/run_tests.ps1`.

1. Instale o Tailscale no Windows e entre na conta que será proprietária da tailnet.
2. Inicie o sistema pelo WSL:

   ```bash
   cd /mnt/c/Users/ViniciusMiranda/Family-financial-planning
   docker compose up -d --build
   ```

3. Abra o PowerShell na pasta do projeto e execute:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\setup-tailscale.ps1
   ```

   O script lê `APP_PORT` do `.env` automaticamente; para apontar para outra porta manualmente, use
   `-Port <numero>`. Executar novamente (após reiniciar o Docker, trocar de rede etc.) é seguro e
   esperado: o script reconverge para o mesmo estado.

4. O comando exibirá um endereço HTTPS terminado em `.ts.net`. Esse é o endereço privado do sistema.

Para conferir posteriormente:

```powershell
tailscale serve status
```

## Desativação (rollback)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\disable-tailscale-proxy.ps1
```

Remove somente a publicação HTTPS desta aplicação na porta 443 (nunca `tailscale serve reset`,
que apagaria outros serviços eventualmente publicados pelo mesmo nó Tailscale) e verifica que nada
restou nela. Assim como o script de configuração, ele também falha explicitamente em vez de remover
uma configuração na porta 443 que não reconheça como sua. Isso apenas remove a configuração de proxy
do próprio Tailscale; não altera o Docker, o banco de dados, os documentos ou qualquer fato
financeiro. Para religar o acesso privado depois, execute `setup-tailscale.ps1` novamente.

## Acesso da Kelly

1. No console administrativo do Tailscale, convide o e-mail da Kelly para a tailnet.
2. Kelly aceita o convite com a própria conta; contas do Tailscale não devem ser compartilhadas.
3. Instale o aplicativo Tailscale no celular dela e conecte-o à mesma tailnet.
4. Na aba **Acessos** do sistema financeiro, crie o usuário individual da Kelly.
5. Ela abre o endereço `.ts.net` e entra com o usuário próprio do sistema financeiro.

O controle é realizado pela identidade e pelo dispositivo registrado no Tailscale, não pelo MAC address. Se o celular for perdido, remova o dispositivo no console do Tailscale e desative o usuário na aba **Acessos**.

## Referências oficiais

- [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)
- [Instalação no Windows](https://tailscale.com/docs/install/windows)
- [Convites de usuários](https://tailscale.com/docs/features/sharing/how-to/invite-any-user)
