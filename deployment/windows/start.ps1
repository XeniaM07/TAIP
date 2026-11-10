param(
    [ValidateSet('Auto', 'Mock', 'Live')][string]$Mode = 'Auto',
    [switch]$NoInstall,
    [switch]$NoBrowser,
    [switch]$NonInteractive
)

. (Join-Path $PSScriptRoot 'prerequisites.ps1')
. (Join-Path $PSScriptRoot 'keys.ps1')

function Get-TaipFreePort {
    param([int]$Preferred, [int]$Exclude = 0)
    $listener = $null
    try {
        $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Preferred)
        $listener.Start()
        $port = [int]$listener.LocalEndpoint.Port
        if ($port -ne $Exclude) { return $port }
    } catch {
        if ($Preferred -eq 0) { throw 'Cannot reserve a localhost port.' }
    } finally { if ($listener) { $listener.Stop() } }
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        $port = Get-TaipFreePort -Preferred 0
        if ($port -ne $Exclude) { return $port }
    }
    throw 'Cannot find two distinct localhost ports.'
}

function Get-TaipLaunchPorts {
    param([Parameter(Mandatory=$true)][string]$PrivateDirectory)
    $path = Join-Path $PrivateDirectory 'public.env'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        $http = Get-TaipFreePort -Preferred 8080
        $https = Get-TaipFreePort -Preferred 8443 -Exclude $http
        return [pscustomobject]@{HttpPort=$http; HttpsPort=$https}
    }
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($path)) {
        if ($line -match '^TAIP_(HTTP_PORT|HTTPS_PORT|DOMAIN|BIND_ADDRESS)=(.*)$') {
            if ($values.ContainsKey($matches[1])) { throw 'The saved launcher configuration is invalid. Preserve its files and check public.env.' }
            $values[$matches[1]] = $matches[2]
        }
    }
    foreach ($name in @('HTTP_PORT', 'HTTPS_PORT')) {
        if ($values[$name] -notmatch '^\d{1,5}$' -or [int]$values[$name] -lt 1 -or [int]$values[$name] -gt 65535) {
            throw 'The saved launcher ports are invalid. Preserve its files and check public.env.'
        }
    }
    if ($values.DOMAIN -ne 'localhost' -or $values.BIND_ADDRESS -ne '127.0.0.1' -or $values.HTTP_PORT -eq $values.HTTPS_PORT) {
        throw 'The launcher supports a saved localhost-only configuration with distinct ports.'
    }
    return [pscustomobject]@{HttpPort=[int]$values.HTTP_PORT; HttpsPort=[int]$values.HTTPS_PORT}
}

function Assert-TaipPrivateDirectory {
    param([string]$Path)
    $cursor = New-Object IO.DirectoryInfo([IO.Path]::GetFullPath($Path))
    while ($cursor) {
        if (Test-Path -LiteralPath (Join-Path $cursor.FullName '.git')) {
            throw 'Launcher credentials and data must be outside Git worktrees.'
        }
        $cursor = $cursor.Parent
    }
}

function Initialize-TaipDeployment {
    param($Docker, [string]$RepositoryRoot, [string]$PrivateDirectory, $Ports)
    Assert-TaipPrivateDirectory -Path $PrivateDirectory
    [void][IO.Directory]::CreateDirectory($PrivateDirectory)
    $image = Invoke-TaipDocker -Docker $Docker -Arguments @('image', 'inspect', 'python:3.12-slim') -TimeoutSeconds 20
    if ($image.ExitCode -ne 0) {
        Write-Host 'Downloading the official Python bootstrap image (no host Python installation)...'
        $pull = Invoke-TaipDocker -Docker $Docker -Arguments @('pull', 'python:3.12-slim') -TimeoutSeconds 600
        if ($pull.ExitCode -ne 0) { throw 'Cannot download the Python bootstrap image. Check Docker networking and retry.' }
    }
    $source = $RepositoryRoot.Replace('\', '/')
    $private = $PrivateDirectory.Replace('\', '/')
    $arguments = @(
        'run', '--rm', '--pull=never', '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--tmpfs', '/tmp', '--user', '1000:1000',
        '--volume', ($source + ':/taip-source:ro'),
        '--volume', ($private + ':/taip-private'),
        'python:3.12-slim', 'python', '/taip-source/src/taip/deployment/setup_public.py',
        '--directory', '/taip-private', '--environment-directory', $private,
        '--reuse-existing', '--http-port', [string]$Ports.HttpPort,
        '--https-port', [string]$Ports.HttpsPort
    )
    $result = Invoke-TaipDocker -Docker $Docker -Arguments $arguments -TimeoutSeconds 90 -WorkingDirectory $RepositoryRoot
    if ($result.ExitCode -ne 0) {
        # The helper prints only known field/file names, never credential values.
        $detail = $result.StdErr.Trim()
        if ($detail.Length -gt 1500) { $detail = $detail.Substring($detail.Length - 1500) }
        throw "Private deployment setup failed. Existing data was preserved. $detail"
    }
}

function Get-TaipLaunchConfiguration {
    param([string]$DesktopDirectory, [string]$RequestedMode = 'Auto', [bool]$Interactive = $true)
    if ($RequestedMode -eq 'Mock') {
        return [pscustomobject]@{Mode='mock';Path='';Values=@{};Fingerprint=(Get-TaipKeyFingerprint -Mode 'mock' -Values @{})}
    }
    $configuration = Get-TaipKeyConfiguration -DesktopDirectory $DesktopDirectory
    if ($configuration.Mode -eq 'missing') {
        New-TaipKeyTemplate -Path $configuration.Path
        Write-Host 'A file named keys was created on your Desktop. Fill the three API keys for real AI; leave them blank for the demo. Model overrides are optional.'
        if ($Interactive) {
            Start-Process -FilePath 'notepad.exe' -ArgumentList (ConvertTo-TaipWindowsArgument $configuration.Path) -WindowStyle Normal | Out-Null
            [void](Read-Host 'Save and close the keys file, then press Enter here')
        }
        $configuration = Get-TaipKeyConfiguration -DesktopDirectory $DesktopDirectory
    }
    if ($RequestedMode -eq 'Live' -and $configuration.Mode -ne 'live') {
        throw 'Live AI requires all three API keys in the Desktop keys file. Exact model overrides are optional.'
    }
    return $configuration
}

function Get-TaipProjectName {
    param([string]$PrivateDirectory)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($PrivateDirectory).ToLowerInvariant())
        $hash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
        return 'taip-desktop-' + $hash.Substring(0, 12)
    } finally { $sha.Dispose() }
}

function Get-TaipComposeArguments {
    param([string]$RepositoryRoot, [string]$PrivateDirectory, $Configuration)
    if ($Configuration.Mode -notin @('mock', 'live') -or $Configuration.Fingerprint -cnotmatch '^[a-f0-9]{64}$') {
        throw 'The launcher configuration fingerprint is invalid.'
    }
    $overlay = Join-Path $PrivateDirectory 'launcher.compose.yaml'
    $content = "services:`n  core:`n    labels:`n      org.taip.launcher.configuration: '$($Configuration.Fingerprint)'`n"
    Write-TaipPrivateTextIfChanged -Path $overlay -Text $content
    $arguments = @('compose', '--project-directory', $RepositoryRoot, '-p', (Get-TaipProjectName $PrivateDirectory),
        '--env-file', (Join-Path $PrivateDirectory 'public.env'))
    if ($Configuration.Mode -eq 'live') { $arguments += @('--env-file', (Join-Path $PrivateDirectory 'providers.env')) }
    $arguments += @('-f', (Join-Path $RepositoryRoot 'compose.public.yaml'))
    if ($Configuration.Mode -eq 'live') { $arguments += @('-f', (Join-Path $RepositoryRoot 'compose.public.ai.yaml')) }
    $arguments += @('-f', $overlay)
    return ,$arguments
}

function Ensure-TaipCertificate {
    param([string]$PrivateDirectory, [int]$TimeoutSeconds = 60)
    $path = Join-Path $PrivateDirectory 'caddy-data\caddy\pki\authorities\local\root.crt'
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        if ($clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) { throw 'Caddy did not create its local HTTPS certificate in time.' }
        Start-Sleep -Milliseconds 500
    }
    $certificate = New-Object Security.Cryptography.X509Certificates.X509Certificate2($path)
    try {
        $ca = @($certificate.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.19' })
        if ($ca.Count -ne 1 -or -not $ca[0].CertificateAuthority -or $certificate.Subject -ne $certificate.Issuer -or $certificate.NotAfter -le (Get-Date)) {
            throw 'The generated local HTTPS certificate is invalid.'
        }
        $storePath = 'Cert:\CurrentUser\Root\' + $certificate.Thumbprint
        if (-not (Test-Path -LiteralPath $storePath)) {
            Write-Host 'Trusting this local Caddy certificate for the current Windows user...'
            Import-Certificate -FilePath $path -CertStoreLocation 'Cert:\CurrentUser\Root' -ErrorAction Stop | Out-Null
        }
    } finally { $certificate.Dispose() }
}

function Invoke-TaipReadOnlyRequest {
    param([string]$Uri, [string]$AccessToken)
    $address = [Uri]$Uri
    if ($address.Scheme -ne 'https' -or $address.Host -ne 'localhost' -or $address.UserInfo) {
        throw 'Readiness checks require local HTTPS without URL credentials.'
    }
    $request = [Net.HttpWebRequest]::Create($address)
    $request.Method = 'GET'
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = 5000
    $request.ReadWriteTimeout = 5000
    $basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes('taip:' + $AccessToken))
    $request.Headers['Authorization'] = 'Basic ' + $basic
    $response = $null
    $reader = $null
    try {
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200 -or $response.ContentLength -gt 1048576) { throw 'Unexpected readiness response.' }
        $reader = New-Object IO.StreamReader($response.GetResponseStream())
        $text = $reader.ReadToEnd()
        if ($text.Length -gt 1048576) { throw 'Readiness response is too large.' }
        return ConvertFrom-Json -InputObject $text
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
    }
}

function Wait-TaipReadiness {
    param([string]$Origin, [string]$AccessToken, [string]$ExpectedMode,
          [int]$TimeoutSeconds = 60, [int]$RetryIntervalMilliseconds = 1000)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    do {
        try {
            $health = Invoke-TaipReadOnlyRequest -Uri ($Origin + '/api/health') -AccessToken $AccessToken
            $runtime = Invoke-TaipReadOnlyRequest -Uri ($Origin + '/api/runtime') -AccessToken $AccessToken
            if ($health.status -eq 'ok' -and $runtime.mode -eq $ExpectedMode) { return }
        } catch { }
        if ($clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) { break }
        Start-Sleep -Milliseconds $RetryIntervalMilliseconds
    } while ($true)
    throw "The application was not ready in $TimeoutSeconds seconds. Check the container diagnostics. No AI generation was requested."
}

function Save-TaipLauncherDiagnostics {
    param($Docker, [string[]]$ComposeArguments, [string]$PrivateDirectory, $Configuration)
    try {
        $logs = Invoke-TaipDocker -Docker $Docker -Arguments ($ComposeArguments + @('logs', '--no-color', '--tail', '40', 'core', 'gateway', 'redis', 'web', 'caddy')) -TimeoutSeconds 30
        $text = $logs.StdErr + "`n" + $logs.StdOut
        if ($Configuration -and $Configuration.Values) {
            foreach ($name in @('GEMINI_API_KEY', 'GROQ_API_KEY', 'OPENROUTER_API_KEY')) {
                $value = [string]$Configuration.Values[$name]
                if ($value) { $text = $text.Replace($value, '[redacted]') }
            }
        }
        if ($text.Length -gt 65536) { $text = $text.Substring($text.Length - 65536) }
        [IO.File]::WriteAllText((Join-Path $PrivateDirectory 'launcher.log'), $text, (New-Object Text.UTF8Encoding($false)))
        Write-Host ('Diagnostics: ' + (Join-Path $PrivateDirectory 'launcher.log'))
    } catch { Write-Host 'Container diagnostics were unavailable.' }
}

function Open-TaipApplication {
    param([string]$Origin, [string]$PrivateDirectory)
    $password = [IO.File]::ReadAllText((Join-Path $PrivateDirectory 'access-token.txt')).Trim()
    $loginPath = Join-Path $PrivateDirectory 'login.txt'
    $login = "TAIP`r`nAddress: $Origin`r`nUsername: taip`r`nApplication password: $password`r`n`r`nThis is the application login, not a provider API key.`r`n"
    Write-TaipPrivateTextIfChanged -Path $loginPath -Text $login
    Start-Process -FilePath 'notepad.exe' -ArgumentList (ConvertTo-TaipWindowsArgument $loginPath) -WindowStyle Normal | Out-Null
    Start-Process -FilePath $Origin -WindowStyle Normal | Out-Null
}

function Start-TaipApplication {
    param([string]$RepositoryRoot, [string]$PrivateDirectory, [string]$DesktopDirectory,
          [ValidateSet('Auto','Mock','Live')][string]$RequestedMode = 'Auto',
          [bool]$AllowInstall = $true, [bool]$Interactive = $true, [bool]$OpenBrowser = $true)
    if (-not (Test-Path -LiteralPath (Join-Path $RepositoryRoot 'compose.public.yaml') -PathType Leaf)) {
        throw 'Start-TAIP.cmd must remain inside the complete TAIP project.'
    }
    if (-not $PrivateDirectory) { $PrivateDirectory = Join-Path $env:LOCALAPPDATA 'TAIP\launcher' }
    if (-not $DesktopDirectory) { $DesktopDirectory = [Environment]::GetFolderPath('DesktopDirectory') }
    $PrivateDirectory = [IO.Path]::GetFullPath($PrivateDirectory)
    Assert-TaipPrivateDirectory -Path $PrivateDirectory
    $mutex = New-Object Threading.Mutex($false, ('Local\' + (Get-TaipProjectName $PrivateDirectory)))
    $owned = $false
    $docker = $null
    $compose = $null
    $configuration = $null
    try {
        try { $owned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
        if (-not $owned) { throw 'TAIP is already starting. Use the existing launcher window.' }
        Write-Host '[1/6] Checking Docker Desktop and its local Linux engine...'
        $docker = Ensure-TaipDocker -AllowInstall $AllowInstall
        Write-Host '[2/6] Reading the Desktop keys file...'
        $configuration = Get-TaipLaunchConfiguration -DesktopDirectory $DesktopDirectory -RequestedMode $RequestedMode -Interactive $Interactive
        Write-Host ('AI mode: ' + $configuration.Mode + '. Startup never sends a prompt to a provider.')
        Write-Host '[3/6] Preparing reusable private deployment state...'
        $ports = Get-TaipLaunchPorts -PrivateDirectory $PrivateDirectory
        Initialize-TaipDeployment -Docker $docker -RepositoryRoot $RepositoryRoot -PrivateDirectory $PrivateDirectory -Ports $ports
        [void](Write-TaipProviderConfiguration -Configuration $configuration -PrivateDirectory $PrivateDirectory)
        $compose = Get-TaipComposeArguments -RepositoryRoot $RepositoryRoot -PrivateDirectory $PrivateDirectory -Configuration $configuration
        Write-Host '[4/6] Building and starting containers. The first download can take several minutes...'
        $up = Invoke-TaipDocker -Docker $docker -Arguments ($compose + @('up', '--build', '-d', '--wait', '--wait-timeout', '180')) -TimeoutSeconds 1800 -WorkingDirectory $RepositoryRoot
        if ($up.ExitCode -ne 0) { throw 'Docker could not build or start every service. Check launcher.log for the failing step.' }
        Write-Host '[5/6] Checking local HTTPS and application readiness...'
        Ensure-TaipCertificate -PrivateDirectory $PrivateDirectory
        $origin = 'https://localhost:' + $ports.HttpsPort
        $token = [IO.File]::ReadAllText((Join-Path $PrivateDirectory 'access-token.txt')).Trim()
        if ($token -cnotmatch '^[A-Za-z0-9_-]{64}$') { throw 'The saved application access token is invalid.' }
        Wait-TaipReadiness -Origin $origin -AccessToken $token -ExpectedMode $configuration.Mode
        Write-Host ('[6/6] Ready: ' + $origin)
        Write-Host 'The login details are in the local text window opened for you.'
        if ($OpenBrowser) { Open-TaipApplication -Origin $origin -PrivateDirectory $PrivateDirectory }
        return [pscustomobject]@{Origin=$origin;Mode=$configuration.Mode;PrivateDirectory=$PrivateDirectory;ProjectName=(Get-TaipProjectName $PrivateDirectory)}
    } catch {
        if ($docker -and $compose) { Save-TaipLauncherDiagnostics -Docker $docker -ComposeArguments $compose -PrivateDirectory $PrivateDirectory -Configuration $configuration }
        throw
    } finally {
        if ($owned) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    try {
        $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
        $result = Start-TaipApplication -RepositoryRoot $root -RequestedMode $Mode -AllowInstall (-not $NoInstall) -Interactive (-not $NonInteractive) -OpenBrowser (-not $NoBrowser)
        exit 0
    } catch {
        Write-Host ('TAIP could not start: ' + $_.Exception.Message) -ForegroundColor Red
        exit 1
    }
}
