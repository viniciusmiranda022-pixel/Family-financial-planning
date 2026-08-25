#requires -Version 5.1

$ErrorActionPreference = "Stop"
$env:NO_COLOR = "1"
$taskName = "FamilyFinancialPlanning-CodexAdvisor"

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

function Set-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $prefix = "$Name="
    $lines = New-Object System.Collections.Generic.List[string]
    $updated = $false
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
            $lines.Add("$prefix$Value")
            $updated = $true
        } else {
            $lines.Add($line)
        }
    }
    if (-not $updated) {
        $lines.Add("$prefix$Value")
    }
    [System.IO.File]::WriteAllLines($Path, $lines, (New-Object System.Text.UTF8Encoding($false)))
}

function Wait-AdvisorHealth {
    param([int]$Attempts = 20)

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            return Invoke-RestMethod -Uri "http://127.0.0.1:8081/health" -TimeoutSec 2
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "O consultor não iniciou. Consulte $env:LOCALAPPDATA\FamilyFinancialPlanning\logs\advisor.log"
}

function Invoke-DockerCommand {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 transforma mensagens normais do Docker em
        # NativeCommandError quando ErrorActionPreference está em Stop.
        $ErrorActionPreference = "Continue"
        & $dockerExecutable @Arguments 2>&1 | ForEach-Object { Write-Host $_ }
        return [int]$LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$environmentFile = Join-Path $repositoryRoot ".env"
$advisorDirectory = Join-Path $repositoryRoot "advisor"
$runner = (Resolve-Path (Join-Path $PSScriptRoot "run-codex-advisor.ps1")).Path

if (-not (Test-Path $environmentFile)) {
    throw "Arquivo .env não encontrado. Inicialize o sistema antes de conectar o Codex."
}

$secret = Get-DotEnvValue -Path $environmentFile -Name "ADVISOR_SHARED_SECRET"
if ($secret.Length -lt 32 -or $secret -eq "CHANGE_ME_ADVISOR_SECRET") {
    throw "O segredo interno do consultor não foi configurado. Execute o bootstrap do sistema."
}

$node = Get-Command node.exe -ErrorAction Stop
$npm = Join-Path (Split-Path $node.Source) "npm.cmd"
if (-not (Test-Path $npm)) {
    throw "npm.cmd não foi encontrado ao lado do Node.js."
}
$dockerExecutable = (Get-Command docker.exe -ErrorAction Stop).Source

Write-Host "Instalando o Codex para Windows dentro do diretório isolado do consultor..."
Push-Location $advisorDirectory
try {
    & $npm ci --omit=dev --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) {
        throw "npm ci falhou com código $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$codex = Join-Path $advisorDirectory "node_modules\.bin\codex.cmd"
if (-not (Test-Path $codex)) {
    throw "O executável do Codex não foi instalado em $codex"
}

$runtimeRoot = Join-Path $env:LOCALAPPDATA "FamilyFinancialPlanning"
$codexHome = Join-Path $runtimeRoot "codex"
$sandbox = Join-Path $runtimeRoot "sandbox"
New-Item -ItemType Directory -Force -Path $codexHome, $sandbox | Out-Null
$env:CODEX_HOME = $codexHome

$configFile = Join-Path $codexHome "config.toml"
$config = @"
cli_auth_credentials_store = "file"

[windows]
sandbox = "unelevated"
"@
[System.IO.File]::WriteAllText($configFile, $config, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Verificando a autenticação do ChatGPT..."
& $codex login status
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Quando o código aparecer, abra o endereço exibido e entre com sua conta do ChatGPT."
    Write-Host "Nenhuma chave de API será solicitada."
    & $codex login --device-auth
    if ($LASTEXITCODE -ne 0) {
        throw "A autenticação do Codex não foi concluída."
    }
}

& $codex login status
if ($LASTEXITCODE -ne 0) {
    throw "O Codex não confirmou a autenticação."
}

Set-DotEnvValue -Path $environmentFile -Name "ADVISOR_ENABLED" -Value "true"
Set-DotEnvValue -Path $environmentFile -Name "ADVISOR_URL" -Value "http://host.docker.internal:8081"

$powershell = (Get-Process -Id $PID).Path
$actionArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
$action = New-ScheduledTaskAction -Execute $powershell -Argument $actionArguments
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue |
    Stop-ScheduledTask -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Consultor Codex local do Planejamento Financeiro Familiar" `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

$health = Wait-AdvisorHealth
if (-not $health.ready) {
    throw "O serviço iniciou, mas a autenticação do Codex não foi encontrada."
}

Write-Host "Executando uma validação completa com o Codex..."
$testHeaders = @{ "X-Advisor-Token" = $secret }
$testBody = @{
    question = "Teste técnico de conexão do consultor"
    verdict = "informative"
    local_result = "A conexão foi preparada; apenas confirme o funcionamento."
} | ConvertTo-Json
$testResult = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8081/v1/analyze" `
    -Method Post `
    -Headers $testHeaders `
    -ContentType "application/json; charset=utf-8" `
    -Body $testBody `
    -TimeoutSec 90
if ($testResult.provider -ne "codex" -or $testResult.verdict -ne "informative") {
    throw "O Codex respondeu, mas não preservou o resultado do motor local."
}

Write-Host "Atualizando somente a aplicação para usar o consultor nativo..."
Push-Location $repositoryRoot
try {
    $stopExitCode = Invoke-DockerCommand -Arguments @("compose", "stop", "advisor")
    if ($stopExitCode -ne 0) {
        Write-Warning "O contêiner antigo do advisor não pôde ser parado; a configuração continuará."
    }

    $upExitCode = Invoke-DockerCommand -Arguments @(
        "compose", "up", "-d", "--force-recreate", "app"
    )
    if ($upExitCode -ne 0) {
        throw "Não foi possível recriar o contêiner da aplicação."
    }

    $healthProbe = "from urllib.request import urlopen; print(urlopen('http://host.docker.internal:8081/health', timeout=5).read().decode())"
    $probeExitCode = Invoke-DockerCommand -Arguments @(
        "compose", "exec", "-T", "app", "python", "-c", $healthProbe
    )
    if ($probeExitCode -ne 0) {
        throw "O aplicativo Docker não alcançou o consultor nativo do Windows."
    }

    $null = Invoke-DockerCommand -Arguments @("compose", "ps")
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Codex conectado pelo Windows e configurado para iniciar automaticamente no seu login."
Write-Host "O banco, os documentos e os cálculos financeiros continuam dentro do ambiente local."
