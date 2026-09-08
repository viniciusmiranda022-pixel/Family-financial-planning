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
        [string] $BackendState = 'Running',
        # Simulates a daemon that (mis)behaves and turns Funnel on for our
        # own mapping when applying -- used only to deterministically
        # trigger Assert-ProxyStateSafe's post-apply failure path without
        # a real tailnet.
        [switch] $EnableFunnelOnApply
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
            '^serve --https=443 off$' {
                $key = @($state.Web.Keys) | Where-Object { $_ -match ':443$' } | Select-Object -First 1
                if ($key) { $state.Web.Remove($key) }
                if ($state.TCP.Contains('443')) { $state.TCP.Remove('443') }
                if ($key -and $state.AllowFunnel.Contains($key)) { $state.AllowFunnel.Remove($key) }
                return [pscustomobject]@{ ExitCode = 0; Output = '' }
            }
            '^serve --bg --https=443 (.+)$' {
                $target = $Matches[1]
                $state.Web['fake.ts.net:443'] = @{ Handlers = @{ '/' = @{ Proxy = $target } } }
                $state.TCP['443'] = @{ HTTPS = $true }
                $state.AllowFunnel['fake.ts.net:443'] = [bool]$EnableFunnelOnApply
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

function New-TestMarkerPath {
    <#
    .SYNOPSIS
        A fresh, isolated path for the ownership marker
        (Get/Set/Clear-ManagedProxyTarget), one per test -- so tests never
        read or write scripts/lib/.tailscale-proxy-state.json (the real,
        gitignored, per-machine marker) and never see each other's state.
    #>
    Join-Path ([System.IO.Path]::GetTempPath()) ("tailscale-proxy-marker-$([guid]::NewGuid()).json")
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
# Ownership marker (Get/Set/Clear-ManagedProxyTarget)
# ---------------------------------------------------------------------
Invoke-Test 'Get-ManagedProxyTarget retorna $null quando o arquivo de marcador nao existe' {
    $marker = New-TestMarkerPath
    Assert-True (-not (Get-ManagedProxyTarget -MarkerPath $marker)) 'sem marcador, deveria retornar $null'
}

Invoke-Test 'Set-ManagedProxyTarget grava e Get-ManagedProxyTarget le o mesmo valor' {
    $marker = New-TestMarkerPath
    try {
        Set-ManagedProxyTarget -MarkerPath $marker -Target 'http://127.0.0.1:8080'
        Assert-True ((Get-ManagedProxyTarget -MarkerPath $marker) -eq 'http://127.0.0.1:8080') `
            'deveria ler exatamente o valor gravado'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Clear-ManagedProxyTarget remove o marcador e Get-ManagedProxyTarget volta a retornar $null' {
    $marker = New-TestMarkerPath
    Set-ManagedProxyTarget -MarkerPath $marker -Target 'http://127.0.0.1:8080'
    Clear-ManagedProxyTarget -MarkerPath $marker
    Assert-True (-not (Get-ManagedProxyTarget -MarkerPath $marker)) 'apos limpar, deveria retornar $null'
}

Invoke-Test 'Get-ManagedProxyTarget trata um marcador corrompido como ausente, sem lancar excecao' {
    $marker = New-TestMarkerPath
    try {
        Set-Content -LiteralPath $marker -Value 'isto nao e json valido {{{'
        Assert-NotThrows { Get-ManagedProxyTarget -MarkerPath $marker } 'um marcador corrompido nao deveria lancar excecao'
        Assert-True (-not (Get-ManagedProxyTarget -MarkerPath $marker)) 'um marcador corrompido deveria ser tratado como ausente'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Assert-Serve443OwnedOrEmpty retorna $null quando a porta 443 esta vazia' {
    $emptyState = New-ServeStateFixture @{ Web = @{}; TCP = @{}; AllowFunnel = @{} }
    $marker = New-TestMarkerPath
    Assert-True (-not (Assert-Serve443OwnedOrEmpty -ServeState $emptyState -MarkerPath $marker)) `
        'porta 443 vazia deveria retornar $null, sem lancar excecao'
}

Invoke-Test 'Assert-Serve443OwnedOrEmpty retorna o alvo quando a 443 corresponde exatamente ao registro local' {
    $marker = New-TestMarkerPath
    try {
        Set-ManagedProxyTarget -MarkerPath $marker -Target 'http://127.0.0.1:8080'
        $ownState = New-ServeStateFixture @{
            Web = @{ 'app.ts.net:443' = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:8080' } } } }
            TCP = @{ '443' = @{ HTTPS = $true } }
        }
        $result = Assert-Serve443OwnedOrEmpty -ServeState $ownState -MarkerPath $marker
        Assert-True ($result -eq 'http://127.0.0.1:8080') 'deveria reconhecer e retornar o alvo proprio'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------------
# Idempotent apply + post-apply verification
# ---------------------------------------------------------------------
Invoke-Test 'Publish-PrivateHttpsProxy converge ao mesmo estado quando executado duas vezes' {
    $fake = New-FakeTailscaleRunner
    $marker = New-TestMarkerPath
    try {
        Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker
        $firstState = Get-TailscaleServeState -Runner $fake.Runner
        Assert-NotThrows { Assert-ProxyStateSafe -ServeState $firstState -ExpectedPort 8080 } 'primeira aplicacao deveria ser segura'

        Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker
        $secondState = Get-TailscaleServeState -Runner $fake.Runner
        Assert-NotThrows { Assert-ProxyStateSafe -ServeState $secondState -ExpectedPort 8080 } 'segunda aplicacao deveria permanecer segura'

        Assert-True (($firstState | ConvertTo-Json -Depth 10) -eq ($secondState | ConvertTo-Json -Depth 10)) `
            'o estado publicado nao deveria mudar entre execucoes repetidas (idempotencia)'
        Assert-True ($fake.State.Web.Count -eq 1) `
            'nao deveria haver mapeamentos duplicados apos duas execucoes'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Publish-PrivateHttpsProxy falha fechado diante de publicacao 443 pre-existente nao reconhecida e nao remove nada (BLOQUEIO DE MERGE #1)' {
    $fake = New-FakeTailscaleRunner
    $marker = New-TestMarkerPath
    try {
        # Simula uma configuracao manual anterior expondo a porta do PostgreSQL --
        # esta automacao nao tem contrato normativo que a autorize a apagar uma
        # publicacao pre-existente que nao reconhece como propria, mesmo que ela
        # pareca insegura; a alternativa de menor risco e falhar fechado.
        $fake.State.Web['stray.ts.net:443'] = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:5432' } } }
        $fake.State.TCP['443'] = @{ HTTPS = $true }

        Assert-Throws { Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker } `
            'uma publicacao 443 pre-existente nao reconhecida deveria bloquear a aplicacao'

        Assert-True (-not ($fake.State.CallLog -match '^serve (--https=443 off|--bg)')) `
            'nada deveria ter sido mutado (nem off, nem apply) quando a configuracao 443 nao e reconhecida'
        Assert-True ($fake.State.Web.Contains('stray.ts.net:443')) `
            'a publicacao estranha pre-existente deveria permanecer intacta'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Publish-PrivateHttpsProxy falha fechado mesmo com formato limpo quando nao ha registro local correspondente (nao adivinha propriedade so pela forma)' {
    $fake = New-FakeTailscaleRunner
    $marker = New-TestMarkerPath
    try {
        # Formato identico ao que esta automacao produziria -- mas sem
        # nenhum marcador local dizendo que fomos nos que a criamos. Uma
        # forma "limpa" sozinha nao prova propriedade.
        $fake.State.Web['fake.ts.net:443'] = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:9000' } } }
        $fake.State.TCP['443'] = @{ HTTPS = $true }

        Assert-Throws { Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker } `
            'formato limpo sem registro local correspondente ainda deveria ser tratado como nao reconhecido'
        Assert-True (-not ($fake.State.CallLog -match '^serve (--https=443 off|--bg)')) 'nada deveria ter sido mutado'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Publish-PrivateHttpsProxy falha fechado quando a porta 443 sofreu drift em relacao ao registro local (edicao manual apos um run anterior)' {
    $fake = New-FakeTailscaleRunner
    $marker = New-TestMarkerPath
    try {
        Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker
        # Alguem reconfigurou manualmente a 443 para outro alvo depois do
        # nosso ultimo apply, sem passar por esta automacao.
        $fake.State.Web['fake.ts.net:443'].Handlers.'/'.Proxy = 'http://127.0.0.1:12345'

        Assert-Throws { Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker } `
            'drift entre o registro local e o estado real da porta 443 deveria bloquear, nao ser sobrescrito silenciosamente'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Publish-PrivateHttpsProxy nao toca mapeamentos de outras portas/hosts do mesmo no (BLOQUEIO DE MERGE #1)' {
    $fake = New-FakeTailscaleRunner
    $marker = New-TestMarkerPath
    try {
        # Simula um servico completamente nao relacionado, publicado por outra
        # ferramenta na mesma tailnet/no, em uma porta diferente de 443.
        $fake.State.Web['fake.ts.net:8443'] = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:9999' } } }
        $fake.State.TCP['8443'] = @{ HTTPS = $true }
        $fake.State.AllowFunnel['fake.ts.net:8443'] = $true

        Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker
        $state = Get-TailscaleServeState -Runner $fake.Runner
        Assert-NotThrows { Assert-ProxyStateSafe -ServeState $state -ExpectedPort 8080 } `
            'a publicacao desta aplicacao na porta 443 deveria ser considerada segura mesmo com outro servico na 8443'

        Assert-True ($fake.State.Web['fake.ts.net:8443'].Handlers.'/'.Proxy -eq 'http://127.0.0.1:9999') `
            'o mapeamento de outra porta/servico nao deveria ser alterado'
        Assert-True ($fake.State.TCP.Contains('8443')) 'a porta TCP de outro servico nao deveria ser removida'
        Assert-True ($fake.State.AllowFunnel['fake.ts.net:8443'] -eq $true) `
            'o Funnel de outro servico, em outra porta, nao e responsabilidade desta automacao'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Publish-PrivateHttpsProxy propaga falha do CLI ao aplicar sem mascarar' {
    $failingRunner = { param([string[]] $CliArgs) [pscustomobject]@{ ExitCode = 1; Output = 'falhou' } }
    $marker = New-TestMarkerPath
    try {
        Assert-Throws { Publish-PrivateHttpsProxy -Port 8080 -Runner $failingRunner -MarkerPath $marker } `
            'uma falha ao aplicar deveria propagar como excecao, nunca sucesso silencioso'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Publish-PrivateHttpsProxy propaga falha do CLI ao remover a publicacao anterior sem mascarar' {
    $fake = New-FakeTailscaleRunner
    $marker = New-TestMarkerPath
    try {
        Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker
        $failingOffRunner = {
            param([string[]] $CliArgs)
            if (($CliArgs -join ' ') -match '^serve --https=443 off$') {
                return [pscustomobject]@{ ExitCode = 1; Output = 'falhou' }
            }
            & $fake.Runner $CliArgs
        }.GetNewClosure()
        Assert-Throws { Publish-PrivateHttpsProxy -Port 9090 -Runner $failingOffRunner -MarkerPath $marker } `
            'uma falha ao remover a publicacao anterior deveria propagar como excecao'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------------
# Set-VerifiedPrivateHttpsProxy: apply + verify, transactional on failure
# ---------------------------------------------------------------------
Invoke-Test 'Set-VerifiedPrivateHttpsProxy aplica e verifica com sucesso no caminho feliz' {
    $fake = New-FakeTailscaleRunner
    $marker = New-TestMarkerPath
    try {
        Assert-NotThrows { Set-VerifiedPrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker } `
            'o caminho feliz nao deveria lancar excecao'
        $state = Get-TailscaleServeState -Runner $fake.Runner
        Assert-NotThrows { Assert-ProxyStateSafe -ServeState $state -ExpectedPort 8080 } `
            'o estado final deveria ser seguro'
        Assert-True ((Get-ManagedProxyTarget -MarkerPath $marker) -eq 'http://127.0.0.1:8080') `
            'o registro local deveria refletir o alvo publicado com sucesso'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Set-VerifiedPrivateHttpsProxy reverte apenas a porta 443 quando a pos-verificacao falha, sem apagar outro servico (BLOQUEIO DE MERGE #2)' {
    $fake = New-FakeTailscaleRunner -EnableFunnelOnApply
    $marker = New-TestMarkerPath
    try {
        # Um servico nao relacionado, em outra porta, deve sobreviver ao rollback.
        $fake.State.Web['fake.ts.net:8443'] = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:9999' } } }
        $fake.State.TCP['8443'] = @{ HTTPS = $true }

        Assert-Throws { Set-VerifiedPrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker } `
            'a pos-condicao (Funnel habilitado) deveria falhar e propagar'

        Assert-True (-not (Get-Serve443WebEntry -ServeState (Get-TailscaleServeState -Runner $fake.Runner))) `
            'apos a falha de pos-condicao, a porta 443 desta aplicacao nao deveria continuar publicada (sem exposicao nova)'
        Assert-True ($fake.State.Web['fake.ts.net:8443'].Handlers.'/'.Proxy -eq 'http://127.0.0.1:9999') `
            'o rollback nao deveria remover o mapeamento de outro servico em outra porta'
        Assert-True ($fake.State.TCP.Contains('8443')) 'o rollback nao deveria remover a porta TCP de outro servico'
        Assert-True (-not (Get-ManagedProxyTarget -MarkerPath $marker)) `
            'o registro local deveria ser limpo apos o rollback, para nao reivindicar propriedade de nada'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
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

Invoke-Test 'Assert-ProxyStateSafe rejeita a porta 5432 (PostgreSQL) exposta via TCP, mesmo que a 443 propria esteja correta' {
    $unsafeState = New-ServeStateFixture @{
        Web         = @{ 'app.ts.net:443' = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:8080' } } } }
        TCP         = @{ '443' = @{ HTTPS = $true }; '5432' = @{ HTTPS = $false } }
        AllowFunnel = @{ 'app.ts.net:443' = $false }
    }
    Assert-Throws { Assert-ProxyStateSafe -ServeState $unsafeState -ExpectedPort 8080 } `
        'a porta interna do PostgreSQL nunca pode estar publicada pelo Tailscale Serve'
}

Invoke-Test 'Assert-ProxyStateSafe rejeita a porta 443 configurada sem HTTPS (TCP puro)' {
    $unsafeState = New-ServeStateFixture @{
        Web         = @{ 'app.ts.net:443' = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:8080' } } } }
        TCP         = @{ '443' = @{ HTTPS = $false } }
        AllowFunnel = @{ 'app.ts.net:443' = $false }
    }
    Assert-Throws { Assert-ProxyStateSafe -ServeState $unsafeState -ExpectedPort 8080 } `
        'a porta 443 deveria exigir terminacao HTTPS, nao um repasse TCP puro'
}

Invoke-Test 'Assert-ProxyStateSafe aceita a publicacao propria mesmo com outro servico coexistindo em outra porta (BLOQUEIO DE MERGE #1)' {
    $safeState = New-ServeStateFixture @{
        Web         = @{
            'app.ts.net:443'  = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:8080' } } }
            'app.ts.net:8443' = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:9999' } } }
        }
        TCP         = @{ '443' = @{ HTTPS = $true }; '8443' = @{ HTTPS = $true } }
        AllowFunnel = @{ 'app.ts.net:443' = $false; 'app.ts.net:8443' = $true }
    }
    Assert-NotThrows { Assert-ProxyStateSafe -ServeState $safeState -ExpectedPort 8080 } `
        'esta automacao nao e proprietaria de outras portas/servicos do mesmo no e nao deveria rejeita-las'
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
    $marker = New-TestMarkerPath
    try {
        Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker
        Disable-PrivateHttpsProxy -Runner $fake.Runner -MarkerPath $marker
        $state = Get-TailscaleServeState -Runner $fake.Runner
        Assert-NotThrows { Assert-ProxyDisabled -ServeState $state } 'apos o rollback nao deveria sobrar nenhuma publicacao'
        Assert-True (-not (Get-ManagedProxyTarget -MarkerPath $marker)) 'o registro local deveria ser limpo apos o rollback'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Disable-PrivateHttpsProxy e idempotente quando ja nao ha nada publicado na 443' {
    $fake = New-FakeTailscaleRunner
    $marker = New-TestMarkerPath
    try {
        Assert-NotThrows { Disable-PrivateHttpsProxy -Runner $fake.Runner -MarkerPath $marker } `
            'desativar quando ja esta desativado nao deveria lancar excecao'
        Assert-True (-not ($fake.State.CallLog -contains 'serve --https=443 off')) `
            'nao deveria tentar remover algo que ja nao existe'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Disable-PrivateHttpsProxy remove apenas a porta 443 e preserva outros servicos do mesmo no (BLOQUEIO DE MERGE #1)' {
    $fake = New-FakeTailscaleRunner
    $marker = New-TestMarkerPath
    try {
        Publish-PrivateHttpsProxy -Port 8080 -Runner $fake.Runner -MarkerPath $marker
        $fake.State.Web['fake.ts.net:8443'] = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:9999' } } }
        $fake.State.TCP['8443'] = @{ HTTPS = $true }

        Disable-PrivateHttpsProxy -Runner $fake.Runner -MarkerPath $marker

        Assert-True (-not $fake.State.Web.Contains('fake.ts.net:443')) 'a porta 443 desta aplicacao deveria ter sido removida'
        Assert-True ($fake.State.Web['fake.ts.net:8443'].Handlers.'/'.Proxy -eq 'http://127.0.0.1:9999') `
            'o rollback nao deveria remover o mapeamento de outro servico em outra porta'
        Assert-True ($fake.State.TCP.Contains('8443')) 'o rollback nao deveria remover a porta TCP de outro servico'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Disable-PrivateHttpsProxy falha fechado diante de configuracao 443 nao reconhecida e nao remove nada' {
    $fake = New-FakeTailscaleRunner
    $marker = New-TestMarkerPath
    try {
        # Duas rotas sob a mesma porta 443 nao e um formato que esta automacao produz.
        $fake.State.Web['fake.ts.net:443'] = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:8080' }; '/admin' = @{ Proxy = 'http://127.0.0.1:9000' } } }
        $fake.State.TCP['443'] = @{ HTTPS = $true }

        Assert-Throws { Disable-PrivateHttpsProxy -Runner $fake.Runner -MarkerPath $marker } `
            'uma configuracao 443 em formato nao reconhecido nao deveria ser removida pelo rollback'
        Assert-True ($fake.State.Web.Contains('fake.ts.net:443')) 'a configuracao nao reconhecida deveria permanecer intacta'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Disable-PrivateHttpsProxy falha fechado quando a porta 443 nao corresponde ao registro local, mesmo em formato limpo' {
    $fake = New-FakeTailscaleRunner
    $marker = New-TestMarkerPath
    try {
        # Formato limpo, mas nenhum registro local -- pode ser um servico
        # nao relacionado que por acaso usa o mesmo padrao simples.
        $fake.State.Web['fake.ts.net:443'] = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:9000' } } }
        $fake.State.TCP['443'] = @{ HTTPS = $true }

        Assert-Throws { Disable-PrivateHttpsProxy -Runner $fake.Runner -MarkerPath $marker } `
            'sem um registro local correspondente, o rollback nao deveria assumir propriedade e remover'
        Assert-True ($fake.State.Web.Contains('fake.ts.net:443')) 'a configuracao nao comprovadamente propria deveria permanecer intacta'
    } finally { Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue }
}

Invoke-Test 'Assert-ProxyDisabled rejeita estado que ainda publica algo' {
    $stillPublished = New-ServeStateFixture @{
        Web = @{ 'app.ts.net:443' = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:8080' } } } }
        TCP = @{ '443' = @{ HTTPS = $true } }
    }
    Assert-Throws { Assert-ProxyDisabled -ServeState $stillPublished } `
        'nao deveria aceitar um rollback incompleto como desativado'
}

Invoke-Test 'Assert-ProxyDisabled aceita quando outro servico, em outra porta, continua publicado' {
    $stateWithUnrelatedService = New-ServeStateFixture @{
        Web = @{ 'app.ts.net:8443' = @{ Handlers = @{ '/' = @{ Proxy = 'http://127.0.0.1:9999' } } } }
        TCP = @{ '8443' = @{ HTTPS = $true } }
    }
    Assert-NotThrows { Assert-ProxyDisabled -ServeState $stateWithUnrelatedService } `
        'a desativacao desta aplicacao nao deveria exigir que outros servicos do no tambem estejam vazios'
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
