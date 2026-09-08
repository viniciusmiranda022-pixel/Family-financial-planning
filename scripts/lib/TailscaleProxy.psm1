<#
.SYNOPSIS
    Deterministic building blocks for the automated Tailscale Serve HTTPS
    proxy (docs/WORK_ORDER_AUTOMATED_HTTPS_PROXY.md, docs/TAILSCALE.md,
    docs/SECURITY.md).

.DESCRIPTION
    This module contains no top-level side effects: every function that
    shells out to the `tailscale` CLI takes an injectable `-Runner`
    scriptblock (default: `Invoke-TailscaleCli`, which runs the real
    binary). That lets tests/tailscale_proxy/run_tests.ps1 exercise every
    prerequisite check, the idempotent apply, the post-apply safety
    verification, and rollback against a fake in-memory CLI -- without a
    real tailnet, credential, or network access, per the Work Order's
    testing constraints.

    Every thrown/printed message is a static, generic string. None of them
    interpolate raw CLI output, environment variables, or file contents, so
    a leaked auth key or hostname can never reach a log or error message.
#>

Set-StrictMode -Version Latest

# Ports that must never appear as a Tailscale Serve target. PostgreSQL
# (`db`, compose.yaml) and the Advisor sidecar (`advisor/server.mjs`,
# ADVISOR_PORT default 8081, docs/SECURITY.md) are internal-only; this list
# is the automated proxy's equivalent of the "não publicar" rule.
$script:ForbiddenProxyPorts = @(5432, 8081)

function Invoke-TailscaleCli {
    <#
    .SYNOPSIS
        Real Tailscale CLI runner. Captures stdout+stderr and the exit code
        without ever throwing on a non-zero exit -- callers decide what a
        failure means for their step.
    #>
    param(
        [Parameter(Mandatory)] [string[]] $CliArgs,
        [string] $CommandName = 'tailscale.exe'
    )
    $output = & $CommandName @CliArgs 2>&1
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output   = ($output | Out-String).Trim()
    }
}

function Test-TailscaleInstalled {
    param([string] $CommandName = 'tailscale.exe')
    return [bool](Get-Command $CommandName -ErrorAction SilentlyContinue)
}

function Assert-TailscaleInstalled {
    param([string] $CommandName = 'tailscale.exe')
    if (-not (Test-TailscaleInstalled -CommandName $CommandName)) {
        throw 'Tailscale nao encontrado. Instale-o e entre na sua tailnet antes de continuar.'
    }
}

function Resolve-AppPort {
    <#
    .SYNOPSIS
        Resolves the local port the application actually listens on.

    .DESCRIPTION
        compose.yaml maps `${APP_PORT:-8080}` to the container; the
        operator's real `.env` is the only source of truth for which port
        is actually in use on a given machine, so this reads APP_PORT from
        there instead of hardcoding a guess. Falls back to compose.yaml's
        own default (8080) when `.env` does not exist yet or does not set
        APP_PORT -- never fails on a missing file. Never returns or prints
        anything else from `.env`.
    #>
    param(
        [string] $EnvFilePath,
        [int] $DefaultPort = 8080
    )
    if ($EnvFilePath -and (Test-Path -LiteralPath $EnvFilePath -PathType Leaf)) {
        $match = Get-Content -LiteralPath $EnvFilePath -ErrorAction SilentlyContinue |
            Select-String -Pattern '^\s*APP_PORT\s*=\s*(\d+)\s*$' |
            Select-Object -Last 1
        if ($match) {
            return [int]$match.Matches[0].Groups[1].Value
        }
    }
    return $DefaultPort
}

function Test-AppHealthy {
    param(
        [Parameter(Mandatory)] [string] $HealthUrl,
        [int] $TimeoutSec = 5
    )
    try {
        $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec $TimeoutSec
    } catch {
        return $false
    }
    return $health.status -eq 'healthy'
}

function Assert-AppHealthy {
    param(
        [Parameter(Mandatory)] [string] $HealthUrl,
        [int] $TimeoutSec = 5
    )
    if (-not (Test-AppHealthy -HealthUrl $HealthUrl -TimeoutSec $TimeoutSec)) {
        throw 'O sistema financeiro nao respondeu com saude OK. Inicie/verifique o Docker antes de configurar o acesso remoto.'
    }
}

function Get-TailscaleBackendState {
    param([scriptblock] $Runner = ${function:Invoke-TailscaleCli})
    $result = & $Runner @('status', '--json')
    if ($result.ExitCode -ne 0) {
        throw 'Nao foi possivel consultar o status do Tailscale.'
    }
    try {
        $status = $result.Output | ConvertFrom-Json
    } catch {
        throw 'Resposta inesperada do Tailscale ao consultar status.'
    }
    return $status.BackendState
}

function Assert-TailscaleConnected {
    param([scriptblock] $Runner = ${function:Invoke-TailscaleCli})
    $state = Get-TailscaleBackendState -Runner $Runner
    if ($state -ne 'Running') {
        throw 'O Tailscale nao esta conectado. Abra o aplicativo do Tailscale e faca login antes de continuar.'
    }
}

function Publish-PrivateHttpsProxy {
    <#
    .SYNOPSIS
        Idempotently converges Tailscale Serve to exactly one HTTPS (443)
        mapping to the local application, and nothing else.

    .DESCRIPTION
        Always resets the existing Serve configuration before reapplying
        the single desired mapping. `tailscale serve reset` clears every
        previously configured mount (official behavior); applying it on
        every run -- instead of only applying the new mapping on top of
        whatever is already there -- is what makes repeated execution
        converge to the same state rather than accumulate mounts or leave
        a stray target (e.g. from a prior manual test) exposed. This never
        touches Tailscale Funnel, which is a separate, never-invoked
        command family.
    #>
    param(
        [Parameter(Mandatory)] [int] $Port,
        [scriptblock] $Runner = ${function:Invoke-TailscaleCli}
    )
    $reset = & $Runner @('serve', 'reset')
    if ($reset.ExitCode -ne 0) {
        throw 'Nao foi possivel limpar a configuracao anterior do Tailscale Serve.'
    }
    $apply = & $Runner @('serve', '--bg', '--https=443', "http://127.0.0.1:$Port")
    if ($apply.ExitCode -ne 0) {
        throw 'Nao foi possivel publicar o servico dentro da tailnet.'
    }
}

function Get-TailscaleServeState {
    param([scriptblock] $Runner = ${function:Invoke-TailscaleCli})
    $result = & $Runner @('serve', 'status', '--json')
    if ($result.ExitCode -ne 0) {
        throw 'Nao foi possivel consultar o estado do Tailscale Serve.'
    }
    try {
        return $result.Output | ConvertFrom-Json
    } catch {
        throw 'Resposta inesperada do Tailscale Serve ao consultar o estado.'
    }
}

function Assert-ProxyStateSafe {
    <#
    .SYNOPSIS
        Verifies the acceptance criteria from
        docs/WORK_ORDER_AUTOMATED_HTTPS_PROXY.md after an apply: correct
        (and only) target, no Funnel, no unexpected exposure.
    #>
    param(
        [Parameter(Mandatory)] $ServeState,
        [Parameter(Mandatory)] [int] $ExpectedPort,
        [int[]] $ForbiddenPorts = $script:ForbiddenProxyPorts
    )

    $webEntries = @()
    if ($ServeState.Web) { $webEntries = @($ServeState.Web.PSObject.Properties) }
    if ($webEntries.Count -ne 1) {
        throw "Estado inesperado do Tailscale Serve: esperado exatamente um endereco publicado, encontrado(s) $($webEntries.Count)."
    }

    $handlers = @($webEntries[0].Value.Handlers.PSObject.Properties)
    $expectedTarget = "http://127.0.0.1:$ExpectedPort"
    $matchesTarget = $handlers | Where-Object { $_.Value.Proxy -eq $expectedTarget }
    if (-not $matchesTarget -or $handlers.Count -ne 1) {
        throw 'Estado inesperado do Tailscale Serve: o alvo publicado nao corresponde exatamente a aplicacao esperada.'
    }

    foreach ($forbidden in $ForbiddenPorts) {
        if ($handlers | Where-Object { $_.Value.Proxy -match ":$forbidden(\D|$)" }) {
            throw "Estado inseguro do Tailscale Serve: a porta interna $forbidden nao pode ser publicada."
        }
    }

    $tcpPorts = @()
    if ($ServeState.TCP) { $tcpPorts = @($ServeState.TCP.PSObject.Properties.Name) }
    $unexpectedTcp = $tcpPorts | Where-Object { $_ -ne '443' }
    if ($unexpectedTcp) {
        throw "Estado inseguro do Tailscale Serve: porta(s) TCP inesperada(s) publicada(s): $($unexpectedTcp -join ', ')."
    }

    if ($ServeState.AllowFunnel) {
        $funnelEnabled = @($ServeState.AllowFunnel.PSObject.Properties) | Where-Object { $_.Value }
        if ($funnelEnabled) {
            throw 'Tailscale Funnel esta habilitado. Este proxy exige Funnel desabilitado; execute "tailscale funnel 443 off" e investigue antes de prosseguir.'
        }
    }
}

function Disable-PrivateHttpsProxy {
    <#
    .SYNOPSIS
        Rollback: removes the Tailscale Serve HTTPS mapping. Never touches
        Docker, the database, documents, or any financial fact -- it only
        clears a network-proxy configuration on the Tailscale daemon.
    #>
    param([scriptblock] $Runner = ${function:Invoke-TailscaleCli})
    $reset = & $Runner @('serve', 'reset')
    if ($reset.ExitCode -ne 0) {
        throw 'Nao foi possivel desativar a configuracao do Tailscale Serve.'
    }
}

function Assert-ProxyDisabled {
    param([Parameter(Mandatory)] $ServeState)
    $webEntries = @()
    if ($ServeState.Web) { $webEntries = @($ServeState.Web.PSObject.Properties) }
    $tcpEntries = @()
    if ($ServeState.TCP) { $tcpEntries = @($ServeState.TCP.PSObject.Properties) }
    if ($webEntries.Count -gt 0 -or $tcpEntries.Count -gt 0) {
        throw 'A desativacao do Tailscale Serve nao convergiu: ainda ha configuracao publicada.'
    }
}

Export-ModuleMember -Function `
    Invoke-TailscaleCli, `
    Test-TailscaleInstalled, `
    Assert-TailscaleInstalled, `
    Resolve-AppPort, `
    Test-AppHealthy, `
    Assert-AppHealthy, `
    Get-TailscaleBackendState, `
    Assert-TailscaleConnected, `
    Publish-PrivateHttpsProxy, `
    Get-TailscaleServeState, `
    Assert-ProxyStateSafe, `
    Disable-PrivateHttpsProxy, `
    Assert-ProxyDisabled
