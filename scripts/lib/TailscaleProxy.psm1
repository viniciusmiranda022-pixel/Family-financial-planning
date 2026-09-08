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

function Get-Serve443WebEntry {
    <#
    .SYNOPSIS
        Returns the single Web entry (if any) whose key is this node's own
        "<hostname>:443", or $null when nothing is published there.

    .DESCRIPTION
        A Tailscale node has exactly one hostname, so at most one Web key
        can ever end in ":443". Every function that inspects or mutates
        this application's HTTPS mapping goes through this helper instead
        of indexing into $ServeState.Web directly, so it is impossible to
        accidentally read or touch a Web entry that belongs to a different
        port (and therefore, potentially, a different, unrelated service
        on the same Tailscale node).
    #>
    param([Parameter(Mandatory)] $ServeState)
    $candidates443 = @()
    if ($ServeState.Web) {
        $candidates443 = @(@($ServeState.Web.PSObject.Properties) | Where-Object { $_.Name -match ':443$' })
    }
    if ($candidates443.Count -gt 1) {
        throw 'Estado ambiguo do Tailscale Serve: mais de um endereco publicado na porta 443.'
    }
    if ($candidates443.Count -eq 1) { return $candidates443[0] }
    return $null
}

function Test-Serve443CleanRootMapping {
    <#
    .SYNOPSIS
        True when a port-443 Web entry has exactly the shape this
        automation itself would have produced: a single handler mounted at
        "/" proxying to some loopback port. Anything else (extra paths,
        extra handlers, a non-loopback target) is treated as not
        recognizably ours.
    #>
    param([Parameter(Mandatory)] $WebEntry)
    $handlers = @($WebEntry.Value.Handlers.PSObject.Properties)
    return ($handlers.Count -eq 1 -and $handlers[0].Name -eq '/' -and $handlers[0].Value.Proxy -match '^http://127\.0\.0\.1:\d+$')
}

function Get-DefaultProxyMarkerPath {
    # Local, non-secret ownership record (just a target URL string) used by
    # Assert-Serve443OwnedOrEmpty. Lives next to this module so it resolves
    # the same way regardless of the caller's working directory, and is
    # gitignored (.gitignore: TailscaleProxy state marker).
    Join-Path $PSScriptRoot '.tailscale-proxy-state.json'
}

function Get-ManagedProxyTarget {
    <#
    .SYNOPSIS
        Reads the proxy target this automation itself last published, or
        $null if there is no marker or it cannot be parsed. Never throws --
        an unreadable/corrupt marker is treated exactly like "no marker",
        which the fail-closed ownership gate below still handles safely.
    #>
    param([Parameter(Mandatory)] [string] $MarkerPath)
    if (-not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) { return $null }
    try {
        $data = Get-Content -LiteralPath $MarkerPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        return $data.managedTarget
    } catch {
        return $null
    }
}

function Set-ManagedProxyTarget {
    param(
        [Parameter(Mandatory)] [string] $MarkerPath,
        [Parameter(Mandatory)] [string] $Target
    )
    $dir = Split-Path -Parent $MarkerPath
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    (@{ managedTarget = $Target } | ConvertTo-Json -Compress) | Set-Content -LiteralPath $MarkerPath -Encoding utf8
}

function Clear-ManagedProxyTarget {
    param([Parameter(Mandatory)] [string] $MarkerPath)
    if (Test-Path -LiteralPath $MarkerPath -PathType Leaf) {
        Remove-Item -LiteralPath $MarkerPath -Force
    }
}

function Assert-Serve443OwnedOrEmpty {
    <#
    .SYNOPSIS
        Fail-closed ownership gate. Returns the current proxy target string
        when port 443 already holds exactly the mapping this automation's
        own marker recorded creating, or $null when port 443 is completely
        unpublished. Throws -- touching nothing -- for anything else.

    .DESCRIPTION
        Tailscale Serve config carries no per-entry ownership metadata, and
        this repository has no normative contract making it the exclusive
        owner of every handler on the node (see the Work Order review:
        "nao ha contrato normativo dizendo que este repositorio e
        proprietario exclusivo de todos os handlers Serve do no"). A shape
        check alone ("a single root-path proxy") cannot tell this
        automation's own prior mapping apart from an unrelated one that
        happens to look the same -- the marker written by
        Set-ManagedProxyTarget after every successful apply is what makes
        ownership provable rather than guessed: the live port-443 target
        must match the recorded one exactly. This gate only ever inspects
        port 443 -- the one surface docs/TAILSCALE.md documents this
        application as owning -- and never reads or touches any other
        port, hostname mapping, or Funnel setting on the node.
    #>
    param(
        [Parameter(Mandatory)] $ServeState,
        [string] $MarkerPath = (Get-DefaultProxyMarkerPath)
    )

    $webEntry = Get-Serve443WebEntry -ServeState $ServeState
    $tcp443 = $null
    if ($ServeState.TCP) { $tcp443 = $ServeState.TCP.PSObject.Properties | Where-Object { $_.Name -eq '443' } }

    if (-not $webEntry -and -not $tcp443) {
        return $null
    }

    $liveTarget = $null
    if ($webEntry -and (Test-Serve443CleanRootMapping -WebEntry $webEntry)) {
        $liveTarget = $webEntry.Value.Handlers.'/'.Proxy
    }
    $markerTarget = Get-ManagedProxyTarget -MarkerPath $MarkerPath

    if (-not $webEntry -or -not $tcp443 -or -not $liveTarget -or -not $markerTarget -or $liveTarget -ne $markerTarget) {
        throw 'Ja existe configuracao na porta 443 do Tailscale Serve que esta automacao nao reconhece como propria (nenhum registro local corresponde a ela). Nada foi alterado; verifique manualmente com "tailscale serve status" antes de continuar.'
    }
    return $liveTarget
}

function Publish-PrivateHttpsProxy {
    <#
    .SYNOPSIS
        Idempotently converges Tailscale Serve's port-443 mapping to the
        local application, and nothing else on the node.

    .DESCRIPTION
        Never calls `tailscale serve reset` (which clears every mount and
        Funnel setting on the node, not just this application's). Instead:
        Assert-Serve443OwnedOrEmpty first proves port 443 is either
        unpublished or already holds exactly this automation's own shape;
        only then is the existing port-443 mapping removed with the
        granular `tailscale serve --https=443 off` (documented to remove
        only that single port's mapping, leaving every other port/handler
        untouched) before the desired mapping is (re)applied. Any other
        Web/TCP/Funnel entry already on the node -- belonging to an
        unrelated service -- is never inspected or mutated.
    #>
    param(
        [Parameter(Mandatory)] [int] $Port,
        [scriptblock] $Runner = ${function:Invoke-TailscaleCli},
        [string] $MarkerPath = (Get-DefaultProxyMarkerPath)
    )
    $priorState = Get-TailscaleServeState -Runner $Runner
    $priorTarget = Assert-Serve443OwnedOrEmpty -ServeState $priorState -MarkerPath $MarkerPath

    if ($priorTarget) {
        $off = & $Runner @('serve', '--https=443', 'off')
        if ($off.ExitCode -ne 0) {
            throw 'Nao foi possivel remover a publicacao HTTPS anterior na porta 443 antes de reaplicar.'
        }
    }

    $target = "http://127.0.0.1:$Port"
    $apply = & $Runner @('serve', '--bg', '--https=443', $target)
    if ($apply.ExitCode -ne 0) {
        throw 'Nao foi possivel publicar o servico dentro da tailnet.'
    }
    Set-ManagedProxyTarget -MarkerPath $MarkerPath -Target $target
}

function Assert-ProxyStateSafe {
    <#
    .SYNOPSIS
        Verifies the acceptance criteria from
        docs/WORK_ORDER_AUTOMATED_HTTPS_PROXY.md after an apply: this
        application's own port-443 mapping is correct (and only that), no
        Funnel on it, and PostgreSQL/Advisor are unreachable through
        Tailscale Serve regardless of who configured it.
    #>
    param(
        [Parameter(Mandatory)] $ServeState,
        [Parameter(Mandatory)] [int] $ExpectedPort,
        [int[]] $ForbiddenPorts = $script:ForbiddenProxyPorts
    )

    $entry = Get-Serve443WebEntry -ServeState $ServeState
    if (-not $entry) {
        throw 'Estado inesperado do Tailscale Serve: nenhum endereco HTTPS (443) publicado para esta aplicacao.'
    }

    $handlers = @($entry.Value.Handlers.PSObject.Properties)
    $expectedTarget = "http://127.0.0.1:$ExpectedPort"
    $matchesTarget = $handlers | Where-Object { $_.Value.Proxy -eq $expectedTarget }
    if (-not $matchesTarget -or $handlers.Count -ne 1) {
        throw 'Estado inesperado do Tailscale Serve: o alvo publicado na porta 443 nao corresponde exatamente a aplicacao esperada.'
    }

    # Read-only safety net across the whole Serve surface -- never mutated --
    # because PostgreSQL/Advisor must never be reachable through Tailscale
    # Serve regardless of which mapping (ours or not) would expose them.
    $allWebHandlers = @()
    if ($ServeState.Web) {
        foreach ($web in $ServeState.Web.PSObject.Properties) {
            if ($web.Value.Handlers) { $allWebHandlers += @($web.Value.Handlers.PSObject.Properties) }
        }
    }
    $allTcpPorts = @()
    if ($ServeState.TCP) { $allTcpPorts = @($ServeState.TCP.PSObject.Properties.Name) }
    foreach ($forbidden in $ForbiddenPorts) {
        if ($allWebHandlers | Where-Object { $_.Value.Proxy -match ":$forbidden(\D|$)" }) {
            throw "Estado inseguro do Tailscale Serve: a porta interna $forbidden nao pode ser publicada."
        }
        if ($allTcpPorts -contains "$forbidden") {
            throw "Estado inseguro do Tailscale Serve: a porta interna $forbidden nao pode ser publicada."
        }
    }

    $tcp443 = $null
    if ($ServeState.TCP) { $tcp443 = $ServeState.TCP.PSObject.Properties | Where-Object { $_.Name -eq '443' } }
    if (-not $tcp443 -or -not $tcp443.Value.HTTPS) {
        throw 'Estado inseguro do Tailscale Serve: a porta 443 nao esta configurada como HTTPS.'
    }

    if ($ServeState.AllowFunnel) {
        $funnelOn443 = @($ServeState.AllowFunnel.PSObject.Properties) | Where-Object { $_.Name -match ':443$' -and $_.Value }
        if ($funnelOn443) {
            throw 'Tailscale Funnel esta habilitado para esta publicacao. Este proxy exige Funnel desabilitado; execute "tailscale funnel 443 off" e investigue antes de prosseguir.'
        }
    }
}

function Set-VerifiedPrivateHttpsProxy {
    <#
    .SYNOPSIS
        Applies Publish-PrivateHttpsProxy and verifies the result with
        Assert-ProxyStateSafe. If the post-apply verification fails, this
        removes only the port-443 mapping this call just created (the same
        granular `--https=443 off`, never a node-wide reset) so no new
        exposure from this application is left behind, then re-throws --
        it never masks the original failure as success.
    #>
    param(
        [Parameter(Mandatory)] [int] $Port,
        [scriptblock] $Runner = ${function:Invoke-TailscaleCli},
        [string] $MarkerPath = (Get-DefaultProxyMarkerPath)
    )

    Publish-PrivateHttpsProxy -Port $Port -Runner $Runner -MarkerPath $MarkerPath
    try {
        $state = Get-TailscaleServeState -Runner $Runner
        Assert-ProxyStateSafe -ServeState $state -ExpectedPort $Port
    } catch {
        $originalMessage = $_.Exception.Message
        $off = & $Runner @('serve', '--https=443', 'off')
        if ($off.ExitCode -ne 0) {
            throw "Pos-condicao de seguranca falhou apos publicar o proxy, e a remocao de emergencia da porta 443 tambem falhou. Verifique manualmente agora com 'tailscale serve status'. Causa original: $originalMessage"
        }
        Clear-ManagedProxyTarget -MarkerPath $MarkerPath
        throw "Pos-condicao de seguranca falhou apos publicar o proxy; a publicacao na porta 443 foi removida e nenhuma exposicao nova desta aplicacao foi deixada. Causa original: $originalMessage"
    }
}

function Disable-PrivateHttpsProxy {
    <#
    .SYNOPSIS
        Rollback: removes only this application's port-443 Tailscale Serve
        mapping. Never touches Docker, the database, documents, or any
        financial fact, and -- like Publish-PrivateHttpsProxy -- never
        calls `tailscale serve reset`, so any other port/hostname mapping
        already on the node is left exactly as it was.
    #>
    param(
        [scriptblock] $Runner = ${function:Invoke-TailscaleCli},
        [string] $MarkerPath = (Get-DefaultProxyMarkerPath)
    )

    $state = Get-TailscaleServeState -Runner $Runner
    $priorTarget = Assert-Serve443OwnedOrEmpty -ServeState $state -MarkerPath $MarkerPath
    if (-not $priorTarget) {
        Clear-ManagedProxyTarget -MarkerPath $MarkerPath
        return
    }

    $off = & $Runner @('serve', '--https=443', 'off')
    if ($off.ExitCode -ne 0) {
        throw 'Nao foi possivel desativar a configuracao do Tailscale Serve na porta 443.'
    }
    Clear-ManagedProxyTarget -MarkerPath $MarkerPath
}

function Assert-ProxyDisabled {
    param([Parameter(Mandatory)] $ServeState)
    if (Get-Serve443WebEntry -ServeState $ServeState) {
        throw 'A desativacao do Tailscale Serve nao convergiu: a porta 443 ainda esta publicada.'
    }
    $tcp443 = $null
    if ($ServeState.TCP) { $tcp443 = $ServeState.TCP.PSObject.Properties | Where-Object { $_.Name -eq '443' } }
    if ($tcp443) {
        throw 'A desativacao do Tailscale Serve nao convergiu: a porta 443 ainda esta publicada.'
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
    Get-TailscaleServeState, `
    Get-Serve443WebEntry, `
    Test-Serve443CleanRootMapping, `
    Get-DefaultProxyMarkerPath, `
    Get-ManagedProxyTarget, `
    Set-ManagedProxyTarget, `
    Clear-ManagedProxyTarget, `
    Assert-Serve443OwnedOrEmpty, `
    Publish-PrivateHttpsProxy, `
    Assert-ProxyStateSafe, `
    Set-VerifiedPrivateHttpsProxy, `
    Disable-PrivateHttpsProxy, `
    Assert-ProxyDisabled
