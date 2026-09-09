<#
.SYNOPSIS
    Rollback for scripts/setup-tailscale.ps1: removes the Tailscale Serve
    HTTPS mapping and verifies it is gone. Does not touch Docker, the
    database, documents, or any financial fact -- it only clears the
    Tailscale daemon's own proxy configuration.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\disable-tailscale-proxy.ps1
#>
param()

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'lib/TailscaleProxy.psm1') -Force

Assert-TailscaleInstalled
Disable-PrivateHttpsProxy

$state = Get-TailscaleServeState
Assert-ProxyDisabled -ServeState $state

Write-Host 'Proxy HTTPS privado desativado. Nenhum dado, documento ou fato financeiro foi alterado.' -ForegroundColor Green
