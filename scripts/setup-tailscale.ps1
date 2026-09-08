<#
.SYNOPSIS
    Idempotently publishes the local application behind Tailscale Serve
    HTTPS, verifies the result, and fails closed on any missing
    prerequisite. See docs/TAILSCALE.md and docs/SECURITY.md.

.PARAMETER Port
    Local port the application listens on. Defaults to APP_PORT read from
    `.env` (falling back to compose.yaml's own default, 8080) when not
    given explicitly.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup-tailscale.ps1
#>
param(
    [int] $Port = 0
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'lib/TailscaleProxy.psm1') -Force

if ($Port -le 0) {
    $envPath = Join-Path $PSScriptRoot '..\.env'
    $Port = Resolve-AppPort -EnvFilePath $envPath
}

Assert-TailscaleInstalled
Assert-AppHealthy -HealthUrl "http://127.0.0.1:$Port/health"
Assert-TailscaleConnected

Set-VerifiedPrivateHttpsProxy -Port $Port

Write-Host ''
Write-Host 'Acesso privado configurado e verificado. Enderecos ativos:' -ForegroundColor Green
tailscale.exe serve status
Write-Host ''
Write-Host 'Tailscale Funnel permanece desabilitado; nenhuma porta publica foi criada.' -ForegroundColor Yellow
Write-Host 'Use somente o endereco HTTPS mostrado acima.' -ForegroundColor Yellow
