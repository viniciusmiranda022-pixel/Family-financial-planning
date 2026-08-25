# Central inteligente e consultor com Codex

## O que funciona localmente

- interpretação inicial de frases como “gastei R$ 150 com combustível ontem”;
- OCR de fotos e PDFs com Tesseract;
- transcrição de áudio com Whisper executado no computador;
- leitura de CSV, OFX, extratos, faturas, boletos, comprovantes e holerites;
- cálculo de teto, fluxo de caixa, projeção, reserva e obrigações;
- prévia editável e confirmação antes de criar qualquer registro;
- consultor financeiro determinístico como modo de contingência.

Fotos, áudios e documentos originais são criptografados no volume local. O sistema bloqueia o mesmo arquivo pelo SHA-256 e verifica possíveis lançamentos repetidos antes da confirmação.

## Onde o Codex participa

O Codex é opcional e tem duas funções delimitadas:

1. sugerir categoria para uma frase ambígua quando as regras locais têm baixa confiança;
2. explicar em linguagem natural o resultado calculado pelo consultor local.

O contêiner `advisor` não possui `DATABASE_URL`, volume do PostgreSQL nem volume dos documentos. A aplicação envia para ele apenas a pergunta, um resumo financeiro agregado e o veredito determinístico. O Codex não pode mudar o veredito, criar lançamentos ou executar pagamentos.

Se o Codex estiver desconectado ou indisponível, a captura e o consultor continuam usando as regras locais.

## Autenticação usando a assinatura do ChatGPT no Windows

Quando o Windows alcança a OpenAI, mas a rede virtual do Docker/WSL é filtrada, execute somente o
consultor no host. O aplicativo continua no Docker e chama o serviço pelo endereço especial
`host.docker.internal`; o PostgreSQL e os documentos não são expostos ao processo nativo.

Antes, abra **ChatGPT > Configurações > Segurança** e habilite o login por código de
dispositivo para o Codex CLI. A OpenAI exige essa autorização antes de gerar o código.

```powershell
cd C:\Users\ViniciusMiranda\Family-financial-planning
powershell -ExecutionPolicy Bypass -File .\scripts\setup-codex-windows.ps1
```

O instalador usa o Node.js já presente, instala a versão fixada do Codex no diretório `advisor`,
exibe o código de dispositivo, configura uma tarefa no logon do Windows e valida a comunicação do
contêiner `app` com o serviço. Credenciais e logs ficam em
`%LOCALAPPDATA%\FamilyFinancialPlanning`, fora do Git.

Para conferir o serviço:

```powershell
Invoke-RestMethod http://127.0.0.1:8081/health
Get-ScheduledTask FamilyFinancialPlanning-CodexAdvisor
```

## Autenticação em Docker/Linux

Se o próprio contêiner consegue acessar `https://auth.openai.com`, execute no Linux/WSL:

```bash
sh ./scripts/setup-codex.sh
```

O comando exibe um código para autenticação por dispositivo. Entre com a conta que possui a
assinatura do ChatGPT. Não configure `OPENAI_API_KEY`: uso de API possui cobrança separada e não é
necessário nesta arquitetura.

Para conferir o login posteriormente:

```bash
docker compose --profile container-advisor exec advisor codex login status
```

Para desconectar, use:

```bash
docker compose --profile container-advisor run --rm --no-deps advisor codex logout
```

## Limites e revisão humana

- OCR e transcrição podem errar valor, data ou favorecido;
- classificações automáticas são sugestões;
- nenhum item é gravado antes da confirmação do usuário;
- uma recomendação do consultor depende da integridade dos lançamentos cadastrados;
- o consultor não substitui aconselhamento profissional e não inicia operações financeiras.

## Referências oficiais

- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Autenticação do Codex](https://learn.chatgpt.com/docs/auth)
- [Modo não interativo](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Permissões e sandbox](https://learn.chatgpt.com/docs/permissions)
