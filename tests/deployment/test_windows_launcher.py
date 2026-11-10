"""One-click orchestration uses synthetic state and mocked external boundaries."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deployment/windows/start.ps1"
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is unavailable")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "The one-click orchestration script must exist")
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.private = self.base / "private state ' space"
        self.desktop = self.base / "Desktop"
        self.desktop.mkdir()

    def exercise(self, code):
        script = self.base / "exercise.ps1"
        script.write_text(
            "$ErrorActionPreference='Stop'\n"
            "[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)\n"
            "try { . $env:TAIP_TEST_SCRIPT\n$result = & {\n" + code +
            "\n} 6>$null\nConvertTo-Json -InputObject $result -Depth 12 -Compress\n"
            "} catch { ConvertTo-Json -InputObject @{Error=$_.Exception.Message} -Compress }\n",
            encoding="utf-8-sig",
        )
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            env={**os.environ, "TAIP_TEST_SCRIPT": str(SCRIPT), "TAIP_TEST_ROOT": str(ROOT),
                 "TAIP_TEST_PRIVATE": str(self.private), "TAIP_TEST_DESKTOP": str(self.desktop),
                 "TAIP_TEST_PYTHON": sys.executable},
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.stderr, result.stderr)
        return json.loads(result.stdout.lstrip("\ufeff"))

    def test_entrypoint_uses_its_own_directory_without_installing_host_python(self):
        source = (ROOT / "Start-TAIP.cmd").read_text(encoding="utf-8")
        self.assertIn('%~dp0deployment\\windows\\start.ps1', source)
        self.assertIn("-NoProfile", source)
        self.assertNotIn("python", source.lower())
        result = self.exercise("'loaded-without-starting'")
        self.assertEqual(result, "loaded-without-starting")

    def test_existing_ports_are_reused_and_corrupt_state_is_not_replaced(self):
        self.private.mkdir()
        env = self.private / "public.env"
        env.write_text("TAIP_HTTP_PORT=18080\nTAIP_HTTPS_PORT=18443\nTAIP_DOMAIN=localhost\nTAIP_BIND_ADDRESS=127.0.0.1\n", encoding="utf-8")
        result = self.exercise("Get-TaipLaunchPorts -PrivateDirectory $env:TAIP_TEST_PRIVATE")
        self.assertEqual((result["HttpPort"], result["HttpsPort"]), (18080, 18443))
        env.write_text("TAIP_HTTP_PORT=oops\n", encoding="utf-8")
        result = self.exercise("Get-TaipLaunchPorts -PrivateDirectory $env:TAIP_TEST_PRIVATE")
        self.assertIn("Error", result)
        self.assertEqual(env.read_text(), "TAIP_HTTP_PORT=oops\n")

    def test_bootstrap_runs_official_python_container_without_network_or_host_python(self):
        result = self.exercise(r"""
$script:Captured = @()
function Invoke-TaipDocker {
    param($Docker, $Arguments, $TimeoutSeconds, $WorkingDirectory)
    $script:Captured += ,@($Arguments)
    if ($Arguments[0] -eq 'run') {
        & $env:TAIP_TEST_PYTHON "$env:TAIP_TEST_ROOT/src/taip/deployment/setup_public.py" --directory $env:TAIP_TEST_PRIVATE --environment-directory $env:TAIP_TEST_PRIVATE --reuse-existing | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Synthetic bootstrap failed.' }
    }
    [pscustomobject]@{ExitCode=0;StdOut='';StdErr=''}
}
$docker=[pscustomobject]@{Executable='fake-docker';Context='desktop-linux'}
$ports=[pscustomobject]@{HttpPort=8080;HttpsPort=8443}
Initialize-TaipDeployment -Docker $docker -RepositoryRoot $env:TAIP_TEST_ROOT -PrivateDirectory $env:TAIP_TEST_PRIVATE -Ports $ports
$first=[IO.File]::ReadAllText("$env:TAIP_TEST_PRIVATE/access-token.txt")
Initialize-TaipDeployment -Docker $docker -RepositoryRoot $env:TAIP_TEST_ROOT -PrivateDirectory $env:TAIP_TEST_PRIVATE -Ports $ports
@{Calls=$script:Captured;Preserved=($first -eq [IO.File]::ReadAllText("$env:TAIP_TEST_PRIVATE/access-token.txt"))}
""")
        self.assertNotIn("Error", result)
        calls = [c for c in result["Calls"] if c[0] == "run"]
        self.assertEqual(len(calls), 2)
        self.assertIn("none", calls[0])
        self.assertIn("--read-only", calls[0])
        self.assertIn("python:3.12-slim", calls[0])
        self.assertIn("--environment-directory", calls[0])
        self.assertIn("--reuse-existing", calls[0])
        self.assertTrue(result["Preserved"])

    def test_compose_label_changes_for_key_rotation_and_unchanged_config_is_stable(self):
        self.private.mkdir()
        result = self.exercise(r"""
$docker=[pscustomobject]@{Executable='fake';Context='desktop-linux'}
$config=[pscustomobject]@{Mode='mock';Fingerprint=('a'*64);Values=@{}}
$first=Get-TaipComposeArguments -RepositoryRoot $env:TAIP_TEST_ROOT -PrivateDirectory $env:TAIP_TEST_PRIVATE -Configuration $config
$path="$env:TAIP_TEST_PRIVATE/launcher.compose.yaml"
$time=[IO.File]::GetLastWriteTimeUtc($path)
$again=Get-TaipComposeArguments -RepositoryRoot $env:TAIP_TEST_ROOT -PrivateDirectory $env:TAIP_TEST_PRIVATE -Configuration $config
$stable=($time -eq [IO.File]::GetLastWriteTimeUtc($path))
$config.Fingerprint='b'*64
$last=Get-TaipComposeArguments -RepositoryRoot $env:TAIP_TEST_ROOT -PrivateDirectory $env:TAIP_TEST_PRIVATE -Configuration $config
@{Stable=$stable;Text=[IO.File]::ReadAllText($path);Args=$last}
""")
        self.assertNotIn("Error", result)
        self.assertTrue(result["Stable"])
        self.assertIn("b" * 64, result["Text"])
        self.assertNotIn("compose.public.ai.yaml", " ".join(result["Args"]))

    def test_readiness_uses_only_health_and_runtime_gets(self):
        result = self.exercise(r"""
$script:Paths=@()
function Invoke-TaipReadOnlyRequest {
    param($Uri,$AccessToken)
    $script:Paths += $Uri
    if ($Uri.EndsWith('/api/health')) { return [pscustomobject]@{status='ok'} }
    return [pscustomobject]@{mode='live'}
}
Wait-TaipReadiness -Origin 'https://localhost:8443' -AccessToken ('x'*64) -ExpectedMode 'live' -TimeoutSeconds 1
$script:Paths
""")
        self.assertEqual(result, ["https://localhost:8443/api/health", "https://localhost:8443/api/runtime"])

    def test_wrong_runtime_mode_fails_boundedly_without_generation(self):
        result = self.exercise(r"""
function Invoke-TaipReadOnlyRequest {
    param($Uri,$AccessToken)
    if ($Uri.EndsWith('/api/health')) { return [pscustomobject]@{status='ok'} }
    [pscustomobject]@{mode='mock'}
}
Wait-TaipReadiness -Origin 'https://localhost:8443' -AccessToken ('x'*64) -ExpectedMode 'live' -TimeoutSeconds 1 -RetryIntervalMilliseconds 10
""")
        self.assertIn("Error", result)
        self.assertIn("ready", result["Error"].lower())

    def test_explicit_mock_never_reads_a_real_desktop_configuration(self):
        result = self.exercise(r"""
function Get-TaipKeyConfiguration { throw 'must not read' }
Get-TaipLaunchConfiguration -DesktopDirectory $env:TAIP_TEST_DESKTOP -RequestedMode 'Mock' -Interactive $false
""")
        self.assertEqual(result["Mode"], "mock")
        self.assertEqual(result["Values"], {})

    def test_bad_fingerprint_cannot_inject_generated_yaml(self):
        self.private.mkdir()
        result = self.exercise(r"""
$config=[pscustomobject]@{Mode='mock';Fingerprint="a`nservices:";Values=@{}}
Get-TaipComposeArguments -RepositoryRoot $env:TAIP_TEST_ROOT -PrivateDirectory $env:TAIP_TEST_PRIVATE -Configuration $config
""")
        self.assertIn("Error", result)
        self.assertFalse((self.private / "launcher.compose.yaml").exists())

    def test_full_live_launch_reuses_state_and_rotates_only_effective_configuration(self):
        self.desktop.joinpath("keys").write_text(
            "GEMINI_API_KEY=synthetic-gemini-key\nGROQ_API_KEY=synthetic-groq-key\n"
            "OPENROUTER_API_KEY=synthetic-openrouter-key\nGEMINI_MODEL=vendor/gemini\n"
            "GROQ_MODEL=vendor/groq\nOPENROUTER_MODEL=vendor/qwen\n", encoding="utf-8",
        )
        result = self.exercise(r"""
$script:Calls=@(); $script:Modes=@(); $script:BrowserCalls=0
function Ensure-TaipDocker { param($AllowInstall) [pscustomobject]@{Executable='fake';Context='desktop-linux'} }
function Ensure-TaipCertificate { param($PrivateDirectory) }
function Wait-TaipReadiness { param($Origin,$AccessToken,$ExpectedMode) $script:Modes += $ExpectedMode }
function Open-TaipApplication { param($Origin,$PrivateDirectory) $script:BrowserCalls++ }
function Invoke-TaipDocker {
    param($Docker,$Arguments,$TimeoutSeconds,$WorkingDirectory)
    $script:Calls += ,@($Arguments)
    if ($Arguments[0] -eq 'run') {
        $http=$Arguments[[array]::IndexOf($Arguments,'--http-port')+1]
        $https=$Arguments[[array]::IndexOf($Arguments,'--https-port')+1]
        & $env:TAIP_TEST_PYTHON "$env:TAIP_TEST_ROOT/src/taip/deployment/setup_public.py" --directory $env:TAIP_TEST_PRIVATE --environment-directory $env:TAIP_TEST_PRIVATE --reuse-existing --http-port $http --https-port $https | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Synthetic bootstrap failed.' }
    }
    [pscustomobject]@{ExitCode=0;StdOut='';StdErr=''}
}
$parameters=@{RepositoryRoot=$env:TAIP_TEST_ROOT;PrivateDirectory=$env:TAIP_TEST_PRIVATE;DesktopDirectory=$env:TAIP_TEST_DESKTOP;AllowInstall=$false;Interactive=$false;OpenBrowser=$false}
$first=Start-TaipApplication @parameters
$token=[IO.File]::ReadAllText("$env:TAIP_TEST_PRIVATE/access-token.txt")
$label=[IO.File]::ReadAllText("$env:TAIP_TEST_PRIVATE/launcher.compose.yaml")
$time=[IO.File]::GetLastWriteTimeUtc("$env:TAIP_TEST_PRIVATE/launcher.compose.yaml")
$again=Start-TaipApplication @parameters
$stable=($time -eq [IO.File]::GetLastWriteTimeUtc("$env:TAIP_TEST_PRIVATE/launcher.compose.yaml"))
$path="$env:TAIP_TEST_DESKTOP/keys"
[IO.File]::WriteAllText($path,[IO.File]::ReadAllText($path).Replace('synthetic-gemini-key','synthetic-rotated-key'))
$last=Start-TaipApplication @parameters
@{First=$first;Again=$again;Last=$last;Stable=$stable;TokenPreserved=($token -eq [IO.File]::ReadAllText("$env:TAIP_TEST_PRIVATE/access-token.txt"));Rotated=($label -ne [IO.File]::ReadAllText("$env:TAIP_TEST_PRIVATE/launcher.compose.yaml"));Calls=$script:Calls;Modes=$script:Modes;BrowserCalls=$script:BrowserCalls}
""")
        self.assertNotIn("Error", result)
        self.assertEqual(result["First"]["Mode"], "live")
        self.assertEqual(result["First"]["Origin"], result["Again"]["Origin"])
        self.assertTrue(result["Stable"])
        self.assertTrue(result["TokenPreserved"])
        self.assertTrue(result["Rotated"])
        self.assertEqual(result["Modes"], ["live"] * 3)
        self.assertEqual(result["BrowserCalls"], 0)
        self.assertEqual(sum(call[0] == "compose" and "up" in call for call in result["Calls"]), 3)
        for call in result["Calls"]:
            self.assertNotIn("extract", call)
            self.assertNotIn("solve", call)
            self.assertNotIn("synthetic-gemini-key", " ".join(call))

    def test_failure_diagnostics_redact_provider_keys(self):
        self.private.mkdir()
        result = self.exercise(r"""
function Invoke-TaipDocker { param($Docker,$Arguments,$TimeoutSeconds) [pscustomobject]@{ExitCode=0;StdOut='failure synthetic-sensitive-key';StdErr=''} }
$config=[pscustomobject]@{Values=@{GEMINI_API_KEY='synthetic-sensitive-key'}}
Save-TaipLauncherDiagnostics -Docker ([pscustomobject]@{}) -ComposeArguments @('compose') -PrivateDirectory $env:TAIP_TEST_PRIVATE -Configuration $config
[IO.File]::ReadAllText("$env:TAIP_TEST_PRIVATE/launcher.log")
""")
        self.assertNotIn("synthetic-sensitive-key", result)
        self.assertIn("[redacted]", result)

    def test_port_selection_never_returns_the_excluded_port(self):
        result = self.exercise(r"""
$first=Get-TaipFreePort -Preferred 0
$second=Get-TaipFreePort -Preferred $first -Exclude $first
@{First=$first;Second=$second}
""")
        self.assertNotEqual(result["First"], result["Second"])

    def test_login_window_includes_username_and_address_without_url_credentials(self):
        self.private.mkdir()
        (self.private / "access-token.txt").write_text("x" * 64, encoding="utf-8")
        result = self.exercise(r"""
$script:Windows=@()
function Start-Process { param($FilePath,$ArgumentList,$WindowStyle) $script:Windows += ,@($FilePath,$ArgumentList,$WindowStyle) }
Open-TaipApplication -Origin 'https://localhost:8443' -PrivateDirectory $env:TAIP_TEST_PRIVATE
@{Login=[IO.File]::ReadAllText("$env:TAIP_TEST_PRIVATE/login.txt");Windows=$script:Windows}
""")
        self.assertNotIn("Error", result)
        self.assertIn("taip", result["Login"])
        self.assertIn("https://localhost:8443", result["Login"])
        self.assertIn("x" * 64, result["Login"])
        self.assertEqual(result["Windows"][-1][0], "https://localhost:8443")
