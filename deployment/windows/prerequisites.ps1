. (Join-Path $PSScriptRoot 'process.ps1')

function Get-TaipDockerEnvironment {
    # Explicit per-command context and Compose files take priority over the caller.
    $environment = @{
        DOCKER_HOST=$null; DOCKER_CONTEXT=$null; DOCKER_TLS=$null
        DOCKER_TLS_VERIFY=$null; DOCKER_CERT_PATH=$null
        COMPOSE_FILE=$null; COMPOSE_PROJECT_NAME=$null; COMPOSE_PROFILES=$null
        COMPOSE_ENV_FILES=$null; COMPOSE_PATH_SEPARATOR=$null
    }
    # Enumerate names only. Never copy inherited provider keys into generated state
    # or mutate the user's environment; explicit launcher env files own this child.
    foreach ($name in [System.Environment]::GetEnvironmentVariables().Keys) {
        if ([string]$name -match '^(TAIP_|GEMINI_|GROQ_|OPENROUTER_)') {
            $environment[[string]$name] = $null
        }
    }
    return $environment
}

function Invoke-TaipDocker {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)]$Docker,
        [Parameter(Mandatory=$true)][AllowEmptyCollection()][string[]]$Arguments,
        [Parameter(Mandatory=$true)][int]$TimeoutSeconds,
        [string]$WorkingDirectory
    )
    if (-not $Docker.Executable -or -not $Docker.Context -or $Docker.Context -match '[\x00-\x20]') {
        throw 'Docker requires a validated local Docker Desktop context.'
    }
    $parameters = @{
        FilePath=[string]$Docker.Executable
        Arguments=(@('--context', [string]$Docker.Context) + $Arguments)
        TimeoutSeconds=$TimeoutSeconds
        Environment=(Get-TaipDockerEnvironment)
    }
    if ($WorkingDirectory) { $parameters.WorkingDirectory = $WorkingDirectory }
    return Invoke-TaipProcess @parameters
}

function Get-TaipDockerInstallation {
    $directories = @()
    if ($env:ProgramFiles) { $directories += (Join-Path $env:ProgramFiles 'Docker\Docker') }
    if ($env:LOCALAPPDATA) { $directories += (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop') }
    $executable = $null
    $desktop = $null
    foreach ($directory in $directories) {
        $candidate = Join-Path $directory 'resources\bin\docker.exe'
        if (-not $executable -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { $executable = $candidate }
        $candidate = Join-Path $directory 'Docker Desktop.exe'
        if (-not $desktop -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { $desktop = $candidate }
    }
    if (-not $executable) {
        $command = Get-Command 'docker.exe' -ErrorAction SilentlyContinue
        if ($command -and $command.Source) { $executable = $command.Source }
    }
    if (-not $executable) { return $null }
    return [pscustomobject]@{ Executable=$executable; DesktopExecutable=$desktop }
}

function Get-TaipLocalDockerContext {
    param([Parameter(Mandatory=$true)][string]$Executable)
    $environment = Get-TaipDockerEnvironment
    $listing = Invoke-TaipProcess -FilePath $Executable -Arguments @('context', 'ls', '--format', '{{.Name}}') -TimeoutSeconds 15 -Environment $environment
    if ($listing.ExitCode -ne 0) { return $null }
    $names = @($listing.StdOut -split '\r?\n' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $ordered = @('desktop-linux', 'default') + @($names | Where-Object { $_ -notin @('desktop-linux', 'default') })
    foreach ($name in $ordered) {
        if ($names -notcontains $name -or $name -match '[\x00-\x20]') { continue }
        $inspection = Invoke-TaipProcess -FilePath $Executable -Arguments @('context', 'inspect', $name, '--format', '{{.Endpoints.docker.Host}}') -TimeoutSeconds 15 -Environment $environment
        if ($inspection.ExitCode -ne 0) { continue }
        # Reject remote TCP, SSH and named pipes referring to another machine.
        if ($inspection.StdOut.Trim() -match '^npipe:/{2,4}\./pipe/[^/\\\s]+$') {
            return [pscustomobject]@{ Executable=$Executable; Context=$name }
        }
    }
    return $null
}

function Install-TaipDockerDesktop {
    $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
    if (-not $winget) {
        try {
            Add-AppxPackage -RegisterByFamilyName -MainPackage 'Microsoft.DesktopAppInstaller_8wekyb3d8bbwe' -ErrorAction Stop | Out-Null
        } catch { }
        $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
    }
    if (-not $winget) {
        throw 'Windows App Installer (winget) is unavailable. Install Docker Desktop using the official Windows instructions: https://docs.docker.com/desktop/setup/install/windows-install/ . Then run Start-TAIP.cmd again.'
    }
    Write-Host 'Installing Docker Desktop. Windows may request administrator approval.'
    $result = Invoke-TaipProcess -FilePath $winget.Source -Arguments @(
        'install', '--id', 'Docker.DockerDesktop', '--exact', '--source', 'winget', '--silent',
        '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity'
    ) -TimeoutSeconds 1200
    if ($result.ExitCode -in @(-1978334967, -1978334966, -1978334965, 3010, 1641)) {
        throw 'Docker Desktop installation requires a Windows restart. Save your work, restart Windows, then run Start-TAIP.cmd again.'
    }
    if ($result.ExitCode -ne 0) {
        throw "Docker Desktop installation did not complete (exit code $($result.ExitCode)). Check Windows administrator approval and install prerequisites at https://docs.docker.com/desktop/setup/install/windows-install/ . If Windows requests a restart, restart Windows and run Start-TAIP.cmd again."
    }
}

function Test-TaipDockerLinux {
    param([Parameter(Mandatory=$true)]$Docker)
    $result = Invoke-TaipDocker -Docker $Docker -Arguments @('info', '--format', '{{.OSType}}') -TimeoutSeconds 15
    return ($result.ExitCode -eq 0 -and $result.StdOut.Trim() -eq 'linux')
}

function Ensure-TaipDocker {
    [CmdletBinding()]
    param([bool]$AllowInstall = $true)

    $installation = Get-TaipDockerInstallation
    if (-not $installation) {
        if (-not $AllowInstall) { throw 'Docker Desktop is not installed. Install it and run Start-TAIP.cmd again.' }
        Install-TaipDockerDesktop
        $installation = Get-TaipDockerInstallation
        if (-not $installation) {
            throw 'Docker Desktop installation finished but its CLI is unavailable. Finish Docker Desktop setup, restart Windows if requested, then run Start-TAIP.cmd again.'
        }
    }
    $docker = Get-TaipLocalDockerContext -Executable $installation.Executable
    if ($docker) {
        try { if (Test-TaipDockerLinux -Docker $docker) { return $docker } } catch { }
    }
    Write-Host 'Starting Docker Desktop and waiting for its local Linux engine...'
    try {
        $start = Invoke-TaipProcess -FilePath $installation.Executable -Arguments @('desktop', 'start', '--timeout', '180') -TimeoutSeconds 195 -Environment (Get-TaipDockerEnvironment)
    } catch {
        throw 'Docker Desktop startup timed out or could not start. Open Docker Desktop, complete its setup, and enable the WSL 2 backend. If Windows requests administrator approval or a restart, complete that step and run Start-TAIP.cmd again.'
    }
    if ($start.ExitCode -ne 0) {
        if (($start.StdErr + $start.StdOut) -match '(?i)unknown (command|flag)|not a docker command|not recognized') {
            if (-not $installation.DesktopExecutable) {
                throw 'This Docker CLI cannot start Docker Desktop. Open Docker Desktop manually, select Linux containers with the WSL 2 backend, and run Start-TAIP.cmd again.'
            }
            Start-Process -FilePath $installation.DesktopExecutable -WindowStyle Hidden -ErrorAction Stop | Out-Null
        } else {
            throw 'Docker Desktop could not start its Linux engine. Open Docker Desktop and check the WSL 2 setup, virtualization, and any administrator approval. If requested, update WSL and restart Windows, then run Start-TAIP.cmd again.'
        }
    }
    # A finite attempt count and short command timeouts bound even an unresponsive daemon.
    $clock = [Diagnostics.Stopwatch]::StartNew()
    for ($attempt = 0; $attempt -lt 90 -and $clock.Elapsed.TotalSeconds -lt 180; $attempt++) {
        try {
            if (-not $docker) { $docker = Get-TaipLocalDockerContext -Executable $installation.Executable }
            if ($docker -and (Test-TaipDockerLinux -Docker $docker)) { return $docker }
        } catch { }
        Start-Sleep -Seconds 2
    }
    throw 'Docker Desktop did not provide a local Linux engine within 180 seconds. Open Docker Desktop, select Linux containers and the WSL 2 backend, and complete any WSL update or administrator approval. Restart Windows if requested, then run Start-TAIP.cmd again. Remote Docker contexts are not supported.'
}
