param(
    [int]$Port = 8090
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command tailscale.exe -ErrorAction SilentlyContinue)) {
    throw "Tailscale nao encontrado. Instale-o no Windows e entre na sua tailnet antes de continuar."
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
} catch {
    throw "O sistema financeiro nao respondeu em http://127.0.0.1:$Port. Inicie o Docker antes de configurar o acesso remoto."
}

if ($health.status -ne "healthy") {
    throw "O sistema respondeu, mas o health check nao esta saudavel."
}

$status = tailscale.exe status --json | ConvertFrom-Json
if ($status.BackendState -ne "Running") {
    throw "O Tailscale nao esta conectado. Abra o aplicativo do Tailscale no Windows e faca login."
}

tailscale.exe serve --bg --https=443 "http://127.0.0.1:$Port"
if ($LASTEXITCODE -ne 0) {
    throw "Nao foi possivel publicar o servico dentro da tailnet."
}

Write-Host ""
Write-Host "Acesso privado configurado. Enderecos ativos:" -ForegroundColor Green
tailscale.exe serve status
Write-Host ""
Write-Host "Use somente o endereco HTTPS mostrado acima. Nao habilite Tailscale Funnel." -ForegroundColor Yellow

