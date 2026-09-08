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

function Restore-PriorServe443Target {
    <#
    .SYNOPSIS
        Best-effort restoration of a previously-live, self-owned port-443
        target after a failed apply or a failed post-apply safety check.
        Always throws: with a message saying the prior publication was
        restored AND proven live, or a manual-intervention message when
        restoration failed or could not be proven. Never returns normally,
        so the original failure this automation was already unwinding from
        can never be masked as success.

    .DESCRIPTION
        Assumes the caller has already ensured port 443 does not currently
        hold the target that just failed (either it was never applied, or
        it was already removed with the same granular
        `tailscale serve --https=443 off` used everywhere else in this
        module) -- this function only reapplies PriorTarget and never
        issues an `off` itself, so it never risks turning off a mapping
        that belongs to an unrelated service.

        A zero exit code from the reapply command is not, by itself,
        treated as proof: the daemon can report success while the live
        state ends up divergent (wrong target, or Funnel enabled). So after
        a zero exit code this re-reads Get-TailscaleServeState and holds it
        to the exact same Assert-ProxyStateSafe check (via -ExpectedTarget)
        that a normal apply must pass -- a rollback cannot be verified to a
        weaker standard than the apply it is reverting. Only when that
        live-state check also passes is the marker rewritten and success
        declared; otherwise this falls through to the same
        manual-intervention failure used when the reapply command itself
        fails, and the marker is cleared rather than claiming an
        unobserved target.
    #>
    param(
        [Parameter(Mandatory)] [scriptblock] $Runner,
        [Parameter(Mandatory)] [string] $MarkerPath,
        [Parameter(Mandatory)] [string] $PriorTarget,
        [Parameter(Mandatory)] [string] $FailureMessage
    )
    $reapply = & $Runner @('serve', '--bg', '--https=443', $PriorTarget)
    if ($reapply.ExitCode -eq 0) {
        $restoredLive = $false
        try {
            $restoredState = Get-TailscaleServeState -Runner $Runner
            Assert-ProxyStateSafe -ServeState $restoredState -ExpectedTarget $PriorTarget
            $restoredLive = $true
        } catch {
            $restoredLive = $false
        }
        if ($restoredLive) {
            Set-ManagedProxyTarget -MarkerPath $MarkerPath -Target $PriorTarget
            throw "$FailureMessage; a publicacao anterior ($PriorTarget) foi restaurada e comprovada pelo estado vivo do Tailscale Serve."
        }
    }

    # Either the reapply command itself failed, or it reported success
    # (exit 0) but the live state does not prove PriorTarget is actually
    # published (HTTPS, no Funnel, no other handler). In both cases the
    # prior state cannot be proven live, so the marker is cleared rather
    # than left pointing at a target that may not actually be published --
    # consistent with every other failure path in this module never
    # claiming ownership of state it has not verified.
    Clear-ManagedProxyTarget -MarkerPath $MarkerPath
    throw "$FailureMessage e a restauracao da publicacao anterior ($PriorTarget) tambem falhou. Intervencao manual necessaria: verifique agora com 'tailscale serve status' e reaplique manualmente a publicacao anterior se preciso."
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

        If a self-owned mapping existed before this call and the new
        apply fails, this is a transactional reapplication, not a bare
        removal: Restore-PriorServe443Target puts the previous, working
        target back on port 443 (and its marker) before propagating the
        original failure, so a failed reapply never leaves a previously
        healthy proxy dark. When there was no prior mapping, a failed
        apply simply leaves port 443 empty, as before.
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
        if ($priorTarget) {
            # Port 443 was already turned off above; put back exactly what
            # was live before this call instead of leaving a previously
            # working proxy dark because the reapply failed.
            Restore-PriorServe443Target -Runner $Runner -MarkerPath $MarkerPath -PriorTarget $priorTarget `
                -FailureMessage 'Nao foi possivel publicar o servico dentro da tailnet'
        }
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

    .DESCRIPTION
        Accepts exactly one of -ExpectedPort (the normal apply path, which
        always targets 127.0.0.1) or -ExpectedTarget (a full
        "http://127.0.0.1:<port>" string). Restore-PriorServe443Target uses
        -ExpectedTarget so a rollback is held to this exact same live-state
        proof as a normal apply, instead of a second, weaker policy.
    #>
    param(
        [Parameter(Mandatory)] $ServeState,
        [int] $ExpectedPort,
        [string] $ExpectedTarget,
        [int[]] $ForbiddenPorts = $script:ForbiddenProxyPorts
    )

    $havePort = $PSBoundParameters.ContainsKey('ExpectedPort')
    if ($havePort -eq [bool]$ExpectedTarget) {
        throw 'Assert-ProxyStateSafe requer exatamente um entre -ExpectedPort e -ExpectedTarget.'
    }
    $expectedTarget = if ($ExpectedTarget) { $ExpectedTarget } else { "http://127.0.0.1:$ExpectedPort" }

    $entry = Get-Serve443WebEntry -ServeState $ServeState
    if (-not $entry) {
        throw 'Estado inesperado do Tailscale Serve: nenhum endereco HTTPS (443) publicado para esta aplicacao.'
    }

    # Reuses the same structural rule this automation's own mapping is
    # defined by (Test-Serve443CleanRootMapping: exactly one handler, at
    # "/", proxying to loopback) instead of a second, looser definition --
    # a handler mounted at any other path (e.g. "/algum-prefixo") must be
    # rejected even when its Proxy value happens to equal expectedTarget,
    # because the web surface actually published would not be the
    # application's expected root (BLOQUEIO DE MERGE #4).
    if (-not (Test-Serve443CleanRootMapping -WebEntry $entry)) {
        throw 'Estado inesperado do Tailscale Serve: o alvo publicado na porta 443 nao corresponde exatamente a aplicacao esperada.'
    }
    $handlers = @($entry.Value.Handlers.PSObject.Properties)
    if ($handlers[0].Value.Proxy -ne $expectedTarget) {
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
        first removes the port-443 mapping this call just created (the
        same granular `--https=443 off`, never a node-wide reset) so no
        new exposure from this application is left behind; then, if a
        self-owned mapping was already live and working before this call,
        it is restored via Restore-PriorServe443Target instead of leaving
        443 empty. It never masks the original failure as success.
    #>
    param(
        [Parameter(Mandatory)] [int] $Port,
        [scriptblock] $Runner = ${function:Invoke-TailscaleCli},
        [string] $MarkerPath = (Get-DefaultProxyMarkerPath)
    )

    $priorState = Get-TailscaleServeState -Runner $Runner
    $priorTarget = Assert-Serve443OwnedOrEmpty -ServeState $priorState -MarkerPath $MarkerPath

    Publish-PrivateHttpsProxy -Port $Port -Runner $Runner -MarkerPath $MarkerPath
    try {
        $state = Get-TailscaleServeState -Runner $Runner
        Assert-ProxyStateSafe -ServeState $state -ExpectedPort $Port
    } catch {
        $originalMessage = $_.Exception.Message
        $off = & $Runner @('serve', '--https=443', 'off')
        if ($off.ExitCode -ne 0) {
            # The target this call just applied already failed its safety
            # post-condition (Assert-ProxyStateSafe), and the emergency
            # removal meant to undo it also failed -- so the live port-443
            # state is now proven unsafe/divergent AND still holds the
            # marker Publish-PrivateHttpsProxy wrote on the earlier
            # successful apply. Clear it before surfacing the incident:
            # this automation must never keep asserting ownership of a
            # state it explicitly failed to validate, or a later run's
            # Assert-Serve443OwnedOrEmpty fail-closed gate could treat this
            # unproven state as its own and silently build on it
            # (BLOQUEIO DE MERGE #5). Never attempts `serve reset` or a
            # second `off` here -- only the local marker is touched.
            Clear-ManagedProxyTarget -MarkerPath $MarkerPath
            throw "Pos-condicao de seguranca falhou apos publicar o proxy, e a remocao de emergencia da porta 443 tambem falhou. Verifique manualmente agora com 'tailscale serve status'. Causa original: $originalMessage"
        }
        if ($priorTarget) {
            # A previously working, self-owned publication existed before
            # this call. Put it back instead of leaving 443 empty, so a
            # failed reapplication never regresses a healthy proxy.
            Restore-PriorServe443Target -Runner $Runner -MarkerPath $MarkerPath -PriorTarget $priorTarget `
                -FailureMessage "Pos-condicao de seguranca falhou apos publicar o proxy. Causa original: $originalMessage"
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
    Restore-PriorServe443Target, `
    Publish-PrivateHttpsProxy, `
    Assert-ProxyStateSafe, `
    Set-VerifiedPrivateHttpsProxy, `
    Disable-PrivateHttpsProxy, `
    Assert-ProxyDisabled
