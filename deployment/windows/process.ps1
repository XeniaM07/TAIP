# Native processes receive an argument vector, never a shell command string.
function ConvertTo-TaipWindowsArgument {
    param([AllowEmptyString()][string]$Value)

    # CommandLineToArgvW / Microsoft C runtime quoting, including a final slash.
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $slashes++
        } elseif ($character -eq '"') {
            [void]$builder.Append(('\' * (2 * $slashes + 1)))
            [void]$builder.Append('"')
            $slashes = 0
        } else {
            if ($slashes -gt 0) { [void]$builder.Append(('\' * $slashes)) }
            [void]$builder.Append($character)
            $slashes = 0
        }
    }
    if ($slashes -gt 0) { [void]$builder.Append(('\' * (2 * $slashes))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-TaipProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [AllowEmptyCollection()][string[]]$Arguments = @(),
        [Parameter(Mandatory=$true)][ValidateRange(1, 86400)][int]$TimeoutSeconds,
        [string]$WorkingDirectory,
        [hashtable]$Environment = @{}
    )

    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $FilePath
    $start.Arguments = (@($Arguments | ForEach-Object { ConvertTo-TaipWindowsArgument -Value $_ }) -join ' ')
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $start.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    if ($WorkingDirectory) { $start.WorkingDirectory = $WorkingDirectory }
    foreach ($name in $Environment.Keys) {
        if ($null -eq $Environment[$name]) {
            $start.EnvironmentVariables.Remove([string]$name)
        } else {
            $start.EnvironmentVariables[[string]$name] = [string]$Environment[$name]
        }
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $started = $false
    try {
        try { $started = $process.Start() } catch {
            throw "Could not start prerequisite process '$([IO.Path]::GetFileName($FilePath))'. Check that it is installed and accessible."
        }
        if (-not $started) { throw 'Could not start prerequisite process.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $finished = $process.WaitForExit($TimeoutSeconds * 1000)
        if ($finished) {
            $remaining = [Math]::Max(0, [int]($TimeoutSeconds * 1000 - $clock.ElapsedMilliseconds))
            $finished = [System.Threading.Tasks.Task]::WaitAll(
                [System.Threading.Tasks.Task[]]@($stdout, $stderr), $remaining
            )
        }
        if (-not $finished) {
            # Kill only this Process instance. Never stop Docker/WSL or other apps.
            if (-not $process.HasExited) {
                try { $process.Kill() } catch { }
                [void]$process.WaitForExit(2000)
            }
            throw "Prerequisite process '$([IO.Path]::GetFileName($FilePath))' timed out after $TimeoutSeconds seconds. Retry after checking Docker Desktop or the installer."
        }
        return [pscustomobject]@{
            ExitCode = [int]$process.ExitCode
            StdOut = [string]$stdout.Result
            StdErr = [string]$stderr.Result
        }
    } finally {
        $clock.Stop()
        $process.Dispose()
    }
}
