#requires -Version 5.1

$ErrorActionPreference = "Stop"

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $prefix = "$Name="
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
            return $line.Substring($prefix.Length).Trim()
        }
    }
    return ""
}

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$environmentFile = Join-Path $repositoryRoot ".env"
if (-not (Test-Path $environmentFile)) {
    throw "Arquivo .env não encontrado em $repositoryRoot"
}

$node = Get-Command node.exe -ErrorAction Stop
$nodePath = $node.Source
$runtimeRoot = Join-Path $env:LOCALAPPDATA "FamilyFinancialPlanning"
$codexHome = Join-Path $runtimeRoot "codex"
$sandbox = Join-Path $runtimeRoot "sandbox"
$logs = Join-Path $runtimeRoot "logs"
$logFile = Join-Path $logs "advisor.log"

New-Item -ItemType Directory -Force -Path $codexHome, $sandbox, $logs | Out-Null
if ((Test-Path $logFile) -and (Get-Item $logFile).Length -gt 5MB) {
    Move-Item -Force $logFile "$logFile.1"
}

# Docker Desktop reaches the Windows host through host.docker.internal. The
# shared secret is still required for every analysis/classification request.
$env:ADVISOR_HOST = "0.0.0.0"
$env:ADVISOR_PORT = "8081"
$env:ADVISOR_SANDBOX_DIR = $sandbox
$env:ADVISOR_SHARED_SECRET = Get-DotEnvValue -Path $environmentFile -Name "ADVISOR_SHARED_SECRET"
$env:CODEX_HOME = $codexHome
$env:CODEX_MODEL = Get-DotEnvValue -Path $environmentFile -Name "CODEX_MODEL"
$env:CODEX_TIMEOUT_MS = Get-DotEnvValue -Path $environmentFile -Name "CODEX_TIMEOUT_MS"

if ([string]::IsNullOrWhiteSpace($env:ADVISOR_SHARED_SECRET)) {
    throw "ADVISOR_SHARED_SECRET não está configurado no .env"
}
if ([string]::IsNullOrWhiteSpace($env:CODEX_TIMEOUT_MS)) {
    $env:CODEX_TIMEOUT_MS = "70000"
}

Set-Location (Join-Path $repositoryRoot "advisor")
& $nodePath "server.mjs" *>> $logFile
exit $LASTEXITCODE
