# Acesso remoto privado com Tailscale

O Tailscale cria a rede privada usada por Vinicius e Kelly para acessar o sistema fora de casa. Ele não classifica lançamentos e não substitui o login do sistema financeiro.

## Arquitetura

1. O Docker continua executando o sistema localmente na porta `8090`.
2. O Tailscale instalado no Windows publica essa porta somente dentro da tailnet.
3. Cada pessoa usa uma conta individual do Tailscale e um usuário individual no sistema financeiro.
4. Nenhuma porta do roteador é aberta e o recurso público **Funnel** não deve ser habilitado.

## Configuração do computador

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

4. O comando exibirá um endereço HTTPS terminado em `.ts.net`. Esse é o endereço privado do sistema.

Para conferir posteriormente:

```powershell
tailscale serve status
```

Para remover a publicação privada:

```powershell
tailscale serve reset
```

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
