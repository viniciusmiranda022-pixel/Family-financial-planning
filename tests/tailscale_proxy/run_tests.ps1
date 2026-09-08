<#
.SYNOPSIS
    Deterministic tests for scripts/lib/TailscaleProxy.psm1 and the two
    scripts built on it (docs/WORK_ORDER_AUTOMATED_HTTPS_PROXY.md).

.DESCRIPTION
    No real tailnet, Tailscale binary, or external network access is used.
    The `tailscale` CLI is replaced by an in-memory fake runner (a
    scriptblock injected via each function's `-Runner` parameter) that
    behaves like the real `tailscale serve`/`status` contract closely
    enough to prove idempotency and drift-rejection; only the HTTP health
    check binds a real loopback listener, which needs no network egress.

    Run with: pwsh -File tests/tailscale_proxy/run_tests.ps1
    Exits 1 if any assertion fails.
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$moduleRoot = Join-Path $PSScriptRoot '..\..\scripts\lib\TailscaleProxy.psm1'
Import-Module $moduleRoot -Force

$script:PassCount = 0
$script:FailCount = 0

function Invoke-Test {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [scriptblock] $Test
    )
    try {
        & $Test
        Write-Host "PASS: $Name" -ForegroundColor Green
        $script:PassCount++
    } catch {
        Write-Host "FAIL: $Name -- $($_.Exception.Message)" -ForegroundColor Red
        $script:FailCount++
    }
}

function Assert-True {
    param([bool] $Condition, [string] $Message)
    if (-not $Condition) { throw "Assert-True falhou: $Message" }
}

function Assert-Throws {
    param([scriptblock] $ScriptBlock, [string] $Message)
    $threw = $false
    try { & $ScriptBlock } catch { $threw = $true }
    if (-not $threw) { throw "Assert-Throws falhou (nao lancou excecao): $Message" }
}

function Assert-NotThrows {
    param([scriptblock] $ScriptBlock, [string] $Message)
    try { & $ScriptBlock } catch { throw "Assert-NotThrows falhou ($($_.Exception.Message)): $Message" }
}

# ---------------------------------------------------------------------
# Fake Tailscale CLI: an in-memory model of `tailscale status`/`serve`.
# ---------------------------------------------------------------------
function New-FakeTailscaleRunner {
    param(
        [string] $BackendState = 'Running'
    )
    $state = [pscustomobject]@{
        Web         = [ordered]@{}
        TCP         = [ordered]@{}
        AllowFunnel = [ordered]@{}
        CallLog     = New-Object System.Collections.Generic.List[string]
    }

    $runner = {
        param([string[]] $CliArgs)
        $state.CallLog.Add(($CliArgs -join ' '))

        switch -Regex ($CliArgs -join ' ') {
            '^status --json$' {
                return [pscustomobject]@{
                    ExitCode = 0
                    Output   = (@{ BackendState = $BackendState } | ConvertTo-Json)
                }
            }
            '^serve reset$' {
                $state.Web = [ordered]@{}
                $state.TCP = [ordered]@{}
                $state.AllowFunnel = [ordered]@{}
                return [pscustomobject]@{ ExitCode = 0; Output = '' }
            }
            '^serve --bg --https=443 (.+)$' {
                $target = $Matches[1]
                $state.Web['fake.ts.net:443'] = @{ Handlers = @{ '/' = @{ Proxy = $target } } }
                $state.TCP['443'] = @{ HTTPS = $true }
                $state.AllowFunnel['fake.ts.net:443'] = $false
                return [pscustomobject]@{ ExitCode = 0; Output = '' }
            }
            '^serve status --json$' {
                $payload = @{
                    Web         = $state.Web
                    TCP         = $state.TCP
                    AllowFunnel = $state.AllowFunnel
                } | ConvertTo-Json -Depth 10
                return [pscustomobject]@{ ExitCode = 0; Output = $payload }
            }
            default {
                return [pscustomobject]@{ ExitCode = 1; Output = 'comando fake nao reconhecido' }
            }
        }
    }.GetNewClosure()

    [pscustomobject]@{ Runner = $runner; State = $state }
}

function New-ServeStateFixture {
    <#
    .SYNOPSIS
        Builds a serve-state test fixture shaped exactly like what
        Get-TailscaleServeState returns in production: a JSON round-trip,
        so nested maps are PSCustomObjects (with keys as properties), not
        plain Hashtables -- matching real `ConvertFrom-Json` output rather
        than PowerShell's `@{}` literal shape.
    #>
    param([Parameter(Mandatory)] [hashtable] $Shape)
    return ($Shape | ConvertTo-Json -Depth 10 | ConvertFrom-Json)
}

# ---------------------------------------------------------------------
# Prerequisites: fail-closed
# ---------------------------------------------------------------------
Invoke-Test 'Assert-TailscaleInstalled lanca erro quando o binario nao existe' {
    Assert-Throws { Assert-TailscaleInstalled -CommandName 'definitely-not-a-real-tailscale-binary-xyz123' } `
        'deveria falhar quando o comando tailscale nao esta no PATH'
}

Invoke-Test 'Test-TailscaleInstalled retorna true para um comando existente' {
    # pwsh certamente existe no PATH do runner de CI (job roda com shell: pwsh).
    Assert-True (Test-TailscaleInstalled -CommandName 'pwsh') 'pwsh deveria ser encontrado'
}

Invoke-Test 'Assert-TailscaleConnected lanca erro quando o backend nao esta rodando' {
    $fake = New-FakeTailscaleRunner -BackendState 'Stopped'
    Assert-Throws { Assert-TailscaleConnected -Runner $fake.Runner } `
        'deveria falhar quando BackendState != Running'
}

Invoke-Test 'Assert-TailscaleConnected nao lanca erro quando o backend esta rodando' {
    $fake = New-FakeTailscaleRunner -BackendState 'Running'
    Assert-NotThrows { Assert-TailscaleConnected -Runner $fake.Runner } `
        'nao deveria falhar quando BackendState == Running'
}

# ---------------------------------------------------------------------
# Health check: a real loopback HttpListener answers while
# Test-AppHealthy (the function under test, using its normal blocking
# Invoke-RestMethod call) runs concurrently on its own runspace via
# PowerShell.BeginInvoke/EndInvoke -- .NET Task.Run cannot host a
# PowerShell scriptblock directly (no Runspace on a raw thread-pool
# thread), so this is the correct way to run a cmdlet concurrently
# in-process. No external network access is used.
# ---------------------------------------------------------------------
function Get-AppHealthyResultAgainstFakeServer {
    param(
        [Parameter(Mandatory)] [string] $ResponseBody,
        [int] $TimeoutSec = 2
    )
    $port = Get-Random -Minimum 20000 -Maximum 40000
    $listener = New-Object System.Net.HttpListener
    $listener.Prefixes.Add("http://127.0.0.1:$port/")
    $listener.Start()
    try {
        $contextTask = $listener.GetContextAsync()

        $ps = [PowerShell]::Create()
        $ps.AddScript({
            param($ModulePath, $Url, $TimeoutSec)
            Import-Module $ModulePath -Force
            Test-AppHealthy -HealthUrl $Url -TimeoutSec $TimeoutSec
        }).AddArgument($moduleRoot).AddArgument("http://127.0.0.1:$port/health").AddArgument($TimeoutSec) | Out-Null
        $asyncResult = $ps.BeginInvoke()

        if ($contextTask.Wait(5000)) {
            $context = $contextTask.Result
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($ResponseBody)
            $context.Response.ContentType = 'application/json'
            $context.Response.ContentLength64 = $bytes.Length
            $context.Response.OutputStream.Write($bytes, 0, $bytes.Length)
            $context.Response.OutputStream.Close()
        }

        if (-not $asyncResult.AsyncWaitHandle.WaitOne(5000)) {
            throw 'Test-AppHealthy nao retornou a tempo durante o teste.'
        }
        $result = $ps.EndInvoke($asyncResult)
        $ps.Dispose()
        return [bool]$result[0]
    } finally {
        $listener.Stop()
        $listener.Close()
    }
}

Invoke-Test 'Test-AppHealthy retorna false quando nada esta escutando na porta' {
    $result = Test-AppHealthy -HealthUrl 'http://127.0.0.1:39217/health' -TimeoutSec 2
    Assert-True (-not $result) 'porta fechada deveria ser tratada como nao saudavel, sem lancar excecao'
}

Invoke-Test 'Test-AppHealthy retorna true quando o corpo reporta status healthy' {
    $result = Get-AppHealthyResultAgainstFakeServer -ResponseBody '{"status":"healthy"}'
    Assert-True ($result -eq $true) 'status healthy deveria resultar em $true'
}

Invoke-Test 'Test-AppHealthy retorna false quando o corpo reporta status degradado' {
    $result = Get-AppHealthyResultAgainstFakeServer -ResponseBody '{"status":"degraded"}'
    Assert-True ($result -eq $false) 'status diferente de healthy deveria resultar em $false'
}

# ---------------------------------------------------------------------
# Resolve-AppPort
# ---------------------------------------------------------------------
Invoke-Test 'Resolve-AppPort usa o padrao quando o arquivo .env nao existe' {
    $port = Resolve-AppPort -EnvFilePath (Join-Path ([System.IO.Path]::GetTempPath()) 'nao-existe.env') -DefaultPort 8080
    Assert-True ($port -eq 8080) 'deveria retornar o valor padrao'
}

Invoke-Test 'Resolve-AppPort le APP_PORT de um .env real' {
    $tmp = New-TemporaryFile
    Set-Content -LiteralPath $tmp -Value @('SECRET_KEY=should-not-be-read', 'APP_PORT=9091', 'OTHER=1')
    try {
        $port = Resolve-AppPort -EnvFilePath $tmp -DefaultPort 8080
        Assert-True ($port -eq 9091) 'deveria ler o valor configurado em .env'
    } finally {
        Remove-Item -LiteralPath $tmp -Force
    }
}

# ---------------------------------------------------------------------
# Idempotent apply + post-apply verification
# ---------------------------------------------------------------------
Invoke-Test 'Publish-PrivateHttpsProxy converge ao mesmo estado quando executado duas vezes' {
    $fake = New-FakeTailscaleRunner
    Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner
    $firstState = Get-TailscaleServeState -Runner $fake.Runner
    Assert-NotThrows { Assert-ProxyStateSafe -ServeState $firstState -ExpectedPort 8080 } 'primeira aplicacao deveria ser segura'

    Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner
    $secondState = Get-TailscaleServeState -Runner $fake.Runner
    Assert-NotThrows { Assert-ProxyStateSafe -ServeState $secondState -ExpectedPort 8080 } 'segunda aplicacao deveria permanecer segura'

    Assert-True (($firstState | ConvertTo-Json -Depth 10) -eq ($secondState | ConvertTo-Json -Depth 10)) `
        'o estado publicado nao deveria mudar entre execucoes repetidas (idempotencia)'
    Assert-True ($fake.State.Web.Count -eq 1) `
        'nao deveria haver mapeamentos duplicados apos duas execucoes'
}

Invoke-Test 'Publish-PrivateHttpsProxy remove uma exposicao anterior indevida (drift)' {
    $fake = New-FakeTailscaleRunner
    # Simula uma configuracao manual anterior expondo a porta do PostgreSQL.
    $fake.State.Web['stray.ts.net:443'] = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:5432' } } }
    $fake.State.TCP['443'] = @{ HTTPS = $true }

    Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner
    $state = Get-TailscaleServeState -Runner $fake.Runner

    Assert-NotThrows { Assert-ProxyStateSafe -ServeState $state -ExpectedPort 8080 } `
        'apos reset+apply nao deveria sobrar nenhuma exposicao anterior'
}

Invoke-Test 'Publish-PrivateHttpsProxy propaga falha do CLI sem mascarar' {
    $failingRunner = { param([string[]] $CliArgs) [pscustomobject]@{ ExitCode = 1; Output = 'falhou' } }
    Assert-Throws { Publish-PrivateHttpsProxy -Port 8080 -Runner $failingRunner } `
        'uma falha no reset/apply deveria propagar como excecao, nunca sucesso silencioso'
}

# ---------------------------------------------------------------------
# Assert-ProxyStateSafe: unsafe states must be rejected
# ---------------------------------------------------------------------
Invoke-Test 'Assert-ProxyStateSafe rejeita Tailscale Funnel habilitado' {
    $unsafeState = New-ServeStateFixture @{
        Web         = @{ 'app.ts.net:443' = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:8080' } } } }
        TCP         = @{ '443' = @{ HTTPS = $true } }
        AllowFunnel = @{ 'app.ts.net:443' = $true }
    }
    Assert-Throws { Assert-ProxyStateSafe -ServeState $unsafeState -ExpectedPort 8080 } `
        'Funnel habilitado deveria ser rejeitado'
}

Invoke-Test 'Assert-ProxyStateSafe rejeita porta TCP inesperada (ex.: PostgreSQL exposto via TCP)' {
    $unsafeState = New-ServeStateFixture @{
        Web         = @{ 'app.ts.net:443' = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:8080' } } } }
        TCP         = @{ '443' = @{ HTTPS = $true }; '5432' = @{ HTTPS = $false } }
        AllowFunnel = @{ 'app.ts.net:443' = $false }
    }
    Assert-Throws { Assert-ProxyStateSafe -ServeState $unsafeState -ExpectedPort 8080 } `
        'uma porta TCP alem de 443 deveria ser rejeitada'
}

Invoke-Test 'Assert-ProxyStateSafe rejeita alvo do Advisor publicado' {
    $unsafeState = New-ServeStateFixture @{
        Web         = @{ 'app.ts.net:443' = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:8081' } } } }
        TCP         = @{ '443' = @{ HTTPS = $true } }
        AllowFunnel = @{ 'app.ts.net:443' = $false }
    }
    Assert-Throws { Assert-ProxyStateSafe -ServeState $unsafeState -ExpectedPort 8080 } `
        'publicar a porta do Advisor (8081) deveria ser rejeitado mesmo que ExpectedPort seja outro'
}

Invoke-Test 'Assert-ProxyStateSafe aceita o estado correto' {
    $safeState = New-ServeStateFixture @{
        Web         = @{ 'app.ts.net:443' = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:8080' } } } }
        TCP         = @{ '443' = @{ HTTPS = $true } }
        AllowFunnel = @{ 'app.ts.net:443' = $false }
    }
    Assert-NotThrows { Assert-ProxyStateSafe -ServeState $safeState -ExpectedPort 8080 } `
        'o unico mapeamento esperado, sem Funnel, deveria ser aceito'
}

# ---------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------
Invoke-Test 'Disable-PrivateHttpsProxy remove a publicacao e a verificacao confirma' {
    $fake = New-FakeTailscaleRunner
    Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner
    Disable-PrivateHttpsProxy -Runner $fake.Runner
    $state = Get-TailscaleServeState -Runner $fake.Runner
    Assert-NotThrows { Assert-ProxyDisabled -ServeState $state } 'apos o rollback nao deveria sobrar nenhuma publicacao'
}

Invoke-Test 'Assert-ProxyDisabled rejeita estado que ainda publica algo' {
    $stillPublished = New-ServeStateFixture @{
        Web = @{ 'app.ts.net:443' = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:8080' } } } }
        TCP = @{ '443' = @{ HTTPS = $true } }
    }
    Assert-Throws { Assert-ProxyDisabled -ServeState $stillPublished } `
        'nao deveria aceitar um rollback incompleto como desativado'
}

# ---------------------------------------------------------------------
# No real secret/host literal ships in the automation source.
# ---------------------------------------------------------------------
Invoke-Test 'Os scripts e o modulo nao contem segredo ou IP nao-loopback hardcoded' {
    $filesToScan = @(
        (Join-Path $PSScriptRoot '..\..\scripts\lib\TailscaleProxy.psm1'),
        (Join-Path $PSScriptRoot '..\..\scripts\setup-tailscale.ps1'),
        (Join-Path $PSScriptRoot '..\..\scripts\disable-tailscale-proxy.ps1')
    )
    $secretPatterns = @('tskey-', 'AUTHKEY', 'BEGIN PRIVATE KEY', 'FILE_ENCRYPTION_KEY', 'SECRET_KEY=')
    $ipv4Pattern = '\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b'

    foreach ($file in $filesToScan) {
        $content = Get-Content -LiteralPath $file -Raw
        foreach ($pattern in $secretPatterns) {
            Assert-True ($content -notmatch [regex]::Escape($pattern)) "$file nao deveria conter '$pattern'"
        }
        $ipMatches = [regex]::Matches($content, $ipv4Pattern) | ForEach-Object { $_.Value } | Select-Object -Unique
        foreach ($ip in $ipMatches) {
            Assert-True ($ip -eq '127.0.0.1') "$file contem um IP nao-loopback hardcoded: $ip"
        }
    }
}

# ---------------------------------------------------------------------
Write-Host ''
Write-Host "Resultado: $($script:PassCount) passaram, $($script:FailCount) falharam." -ForegroundColor Cyan
if ($script:FailCount -gt 0) { exit 1 }
exit 0
