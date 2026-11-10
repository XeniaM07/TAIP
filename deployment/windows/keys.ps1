# Provider credentials are data. Never evaluate their contents or log their values.

function Get-TaipProviderFieldNames {
    return @(
        'GEMINI_API_KEY', 'GROQ_API_KEY', 'OPENROUTER_API_KEY',
        'GEMINI_MODEL', 'GROQ_MODEL', 'OPENROUTER_MODEL'
    )
}

function Get-TaipProviderModelDefaults {
    # Exact project defaults; availability and charges depend on each account.
    return @{
        GEMINI_MODEL = 'gemini-3.5-flash-lite'
        GROQ_MODEL = 'openai/gpt-oss-120b'
        OPENROUTER_MODEL = 'qwen/qwen3.7-flash'
    }
}

function Get-TaipKeyFingerprint {
    param([string]$Mode, [hashtable]$Values)

    $canonical = New-Object System.Text.StringBuilder
    [void]$canonical.Append("MODE=$Mode`n")
    if ($Mode -eq 'live') {
        foreach ($name in (Get-TaipProviderFieldNames)) {
            [void]$canonical.Append($name).Append('=').Append($Values[$name]).Append("`n")
        }
    }
    $encoding = New-Object System.Text.UTF8Encoding($false, $true)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash($encoding.GetBytes($canonical.ToString()))
        return [System.BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Assert-TaipProviderValue {
    param([string]$Name, [string]$Value)

    if ($Value -match '[\x00-\x1F\x7F-\x9F]' -or $Value -cne $Value.Trim()) {
        throw "Invalid value for $Name."
    }
    $maximum = 512
    if ($Name.EndsWith('_API_KEY')) { $maximum = 4096 }
    $encoding = New-Object System.Text.UTF8Encoding($false, $true)
    try { $length = $encoding.GetByteCount($Value) }
    catch { throw "Invalid value for $Name." }
    if ($length -gt $maximum) { throw "Value is too long for $Name." }
}

function Get-TaipKeyConfiguration {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$DesktopDirectory)

    try {
        $directory = [System.IO.Path]::GetFullPath($DesktopDirectory)
        $path = [System.IO.Path]::Combine($directory, 'keys')
        $fallback = [System.IO.Path]::Combine($directory, 'keys.txt')
    }
    catch { throw 'The Desktop key directory is invalid.' }
    if ([System.IO.Directory]::Exists($path)) { throw 'The Desktop keys entry must be a regular file.' }
    if (-not [System.IO.File]::Exists($path)) {
        if ([System.IO.Directory]::Exists($fallback)) { throw 'The Desktop keys.txt entry must be a regular file.' }
        if ([System.IO.File]::Exists($fallback)) { $path = $fallback }
        else {
            return [PSCustomObject]@{
                Mode = 'missing'; Path = $path; Values = @{}
                Fingerprint = (Get-TaipKeyFingerprint -Mode 'missing' -Values @{})
            }
        }
    }

    # Bound the read itself, including when another process replaces a file mid-read.
    $buffer = New-Object byte[] 32769
    $length = 0
    $stream = $null
    try {
        $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        while ($length -lt $buffer.Length) {
            $count = $stream.Read($buffer, $length, $buffer.Length - $length)
            if ($count -eq 0) { break }
            $length += $count
        }
    }
    catch { throw 'Cannot read the Desktop keys file. Check its permissions and try again.' }
    finally { if ($null -ne $stream) { $stream.Dispose() } }
    if ($length -gt 32768) { throw 'The Desktop keys file exceeds 32 KiB.' }
    $encoding = New-Object System.Text.UTF8Encoding($false, $true)
    try { $text = $encoding.GetString($buffer, 0, $length) }
    catch { throw 'The Desktop keys file must contain valid UTF-8 text.' }
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    if ($text -match '[\x00-\x09\x0B\x0C\x0E-\x1F\x7F-\x9F]') {
        throw 'The Desktop keys file contains a forbidden control character.'
    }

    $fields = @(Get-TaipProviderFieldNames)
    $seen = @{}
    $values = @{}
    $requestedMode = ''
    foreach ($line in [System.Text.RegularExpressions.Regex]::Split($text, '\r\n|\n|\r')) {
        $trimmed = $line.Trim()
        if ($trimmed.Length -eq 0 -or $trimmed.StartsWith('#')) { continue }
        $separator = $trimmed.IndexOf('=')
        if ($separator -le 0) { throw 'Invalid Desktop keys line. Use NAME=value.' }
        $name = $trimmed.Substring(0, $separator).Trim()
        if ($name -cne 'MODE' -and $fields -cnotcontains $name) {
            throw 'The Desktop keys file contains an unknown field.'
        }
        if ($seen.ContainsKey($name)) { throw "Duplicate field $name in Desktop keys." }
        $seen[$name] = $true
        $value = $trimmed.Substring($separator + 1).Trim()
        if ($value.Length -ge 2) {
            $first = $value[0]
            if (($first -eq [char]34 -or $first -eq [char]39) -and $value[$value.Length - 1] -eq $first) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        if ($name -ceq 'MODE') {
            if ($value -cne 'mock' -and $value -cne 'live') { throw 'MODE must be mock or live.' }
            $requestedMode = $value
        }
        else {
            Assert-TaipProviderValue -Name $name -Value $value
            $values[$name] = $value
        }
    }

    $keyFields = @($fields | Where-Object { $_.EndsWith('_API_KEY') })
    $missing = @($keyFields | Where-Object { -not $values.ContainsKey($_) -or $values[$_].Length -eq 0 })
    $present = @($keyFields | Where-Object { $values.ContainsKey($_) -and $values[$_].Length -gt 0 })
    $mode = 'mock'
    if ($requestedMode -ne 'mock') {
        if ($missing.Count -eq 0) {
            $mode = 'live'
            $defaults = Get-TaipProviderModelDefaults
            foreach ($name in $defaults.Keys) {
                if (-not $values.ContainsKey($name) -or $values[$name].Length -eq 0) {
                    $values[$name] = $defaults[$name]
                }
            }
        }
        elseif ($requestedMode -eq 'live' -or $present.Count -gt 0) {
            throw ('Complete all three provider API keys or set MODE=mock. Missing fields: ' + ($missing -join ', ') + '.')
        }
    }
    return [PSCustomObject]@{
        Mode = $mode; Path = $path; Values = $values
        Fingerprint = (Get-TaipKeyFingerprint -Mode $mode -Values $values)
    }
}

function New-TaipKeyTemplate {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $defaults = Get-TaipProviderModelDefaults
    $text = @(
        '# TAIP provider configuration: keep this file private.',
        '# Leave the three keys blank for mock mode, or fill all three for live mode.',
        '# Optional MODE=mock forces mock mode; MODE=live requires all three keys.',
        '# Live requests may incur provider charges. Startup never performs generation.',
        'GEMINI_API_KEY=', 'GROQ_API_KEY=', 'OPENROUTER_API_KEY=',
        '',
        '# Optional model overrides; uncomment a line to change its project default.',
        '# Availability and pricing depend on your provider accounts.',
        ('# GEMINI_MODEL=' + $defaults['GEMINI_MODEL']),
        ('# GROQ_MODEL=' + $defaults['GROQ_MODEL']),
        ('# OPENROUTER_MODEL=' + $defaults['OPENROUTER_MODEL']), ''
    ) -join "`n"
    $encoding = New-Object System.Text.UTF8Encoding($false, $true)
    $stream = $null
    try {
        if ([System.IO.File]::Exists($Path)) { return }
        # CreateNew also protects against overwriting a file created after the check.
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $bytes = $encoding.GetBytes($text)
        $stream.Write($bytes, 0, $bytes.Length)
    }
    catch {
        if ($null -eq $stream -and [System.IO.File]::Exists($Path)) { return }
        throw 'Cannot create the Desktop keys template. Check the Desktop directory permissions.'
    }
    finally { if ($null -ne $stream) { $stream.Dispose() } }
}

function Write-TaipPrivateTextIfChanged {
    param([string]$Path, [string]$Text)

    $encoding = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        $bytes = $encoding.GetBytes($Text)
        if ([System.IO.File]::Exists($Path)) {
            $existing = [System.IO.File]::ReadAllBytes($Path)
            $same = $existing.Length -eq $bytes.Length
            if ($same) {
                for ($index = 0; $index -lt $bytes.Length; $index++) {
                    if ($existing[$index] -ne $bytes[$index]) { $same = $false; break }
                }
            }
            if ($same) { return }
        }
        [System.IO.File]::WriteAllBytes($Path, $bytes)
    }
    catch { throw 'Cannot write the private provider configuration. Check its directory permissions.' }
}

function ConvertTo-TaipProviderDotenvValue {
    param([string]$Value)
    return "'" + $Value.Replace("'", "\'") + "'"
}

function Write-TaipProviderConfiguration {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Configuration,
        [Parameter(Mandatory = $true)][string]$PrivateDirectory
    )

    if ($Configuration.Mode -eq 'mock' -or $Configuration.Mode -eq 'missing') { return '' }
    if ($Configuration.Mode -ne 'live') { throw 'Provider configuration has an invalid mode.' }
    foreach ($name in (Get-TaipProviderFieldNames)) {
        if (-not ($Configuration.Values[$name] -is [string]) -or $Configuration.Values[$name].Length -eq 0) {
            throw "Provider configuration requires $name."
        }
        Assert-TaipProviderValue -Name $name -Value $Configuration.Values[$name]
    }
    try {
        $directory = [System.IO.Path]::GetFullPath($PrivateDirectory)
        if ($directory -match '[\x00-\x1F\x7F-\x9F]') { throw 'Invalid directory.' }
        $keyDirectory = [System.IO.Path]::Combine($directory, 'provider-keys')
        [void][System.IO.Directory]::CreateDirectory($keyDirectory)
        $environmentPath = [System.IO.Path]::Combine($directory, 'providers.env')
    }
    catch { throw 'Cannot create the private provider directory. Check its path and permissions.' }
    $lines = @('# Private provider file paths and exact model IDs. No credential values.')
    foreach ($provider in @('GEMINI', 'GROQ', 'OPENROUTER')) {
        $keyPath = [System.IO.Path]::Combine($keyDirectory, $provider.ToLowerInvariant() + '-api-key.txt')
        Write-TaipPrivateTextIfChanged -Path $keyPath -Text $Configuration.Values[$provider + '_API_KEY']
        $lines += $provider + '_API_KEY_FILE=' + (ConvertTo-TaipProviderDotenvValue -Value $keyPath.Replace('\', '/'))
        $lines += $provider + '_MODEL=' + (ConvertTo-TaipProviderDotenvValue -Value $Configuration.Values[$provider + '_MODEL'])
    }
    Write-TaipPrivateTextIfChanged -Path $environmentPath -Text (($lines -join "`n") + "`n")
    return $environmentPath
}
