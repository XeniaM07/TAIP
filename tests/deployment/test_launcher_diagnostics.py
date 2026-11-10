"""Failed native builds remain diagnosable without exposing provider credentials."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deployment" / "windows" / "start.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


@unittest.skipUnless(POWERSHELL, "PowerShell is required for launcher diagnostics")
class LauncherDiagnosticsTests(unittest.TestCase):
    def exercise(self, body):
        with TemporaryDirectory(prefix="taip diagnostics '") as temporary:
            private = Path(temporary)
            script = (
                "$ErrorActionPreference='Stop'\n"
                "$ProgressPreference='SilentlyContinue'\n"
                "Set-StrictMode -Version Latest\n"
                "[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)\n"
                ". $env:TAIP_TEST_SCRIPT\n"
                "$result = & {\n" + body + "\n} 6>$null\n"
                "ConvertTo-Json -InputObject $result -Depth 8 -Compress\n"
            )
            result = subprocess.run(
                [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-EncodedCommand",
                 base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                env={**os.environ, "TAIP_TEST_SCRIPT": str(SCRIPT),
                     "TAIP_TEST_REPOSITORY": str(ROOT), "TAIP_TEST_PRIVATE": str(private)},
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(result.stderr, result.stderr)
            return json.loads(result.stdout)

    def test_failed_compose_build_saves_both_native_streams_and_redacts_provider_keys(self):
        result = self.exercise(r"""
function Ensure-TaipDocker { param($AllowInstall) [pscustomobject]@{Executable='fake';Context='desktop-linux'} }
function Get-TaipLaunchConfiguration {
    param($DesktopDirectory,$RequestedMode,$Interactive)
    [pscustomobject]@{Mode='live'; Values=@{GEMINI_API_KEY='synthetic-provider-private-value'}}
}
function Get-TaipLaunchPorts { param($PrivateDirectory) [pscustomobject]@{HttpPort=8080;HttpsPort=8443} }
function Initialize-TaipDeployment { param($Docker,$RepositoryRoot,$PrivateDirectory,$Ports) }
function Write-TaipProviderConfiguration { param($Configuration,$PrivateDirectory) }
function Get-TaipComposeArguments { param($RepositoryRoot,$PrivateDirectory,$Configuration) return ,@('compose','-p','synthetic-diagnostics') }
function Invoke-TaipDocker {
    param($Docker,$Arguments,$TimeoutSeconds,$WorkingDirectory)
    if ($Arguments -contains 'up') {
        return [pscustomobject]@{ExitCode=1;StdOut='native-build-stdout: step failed';StdErr='native-build-stderr: synthetic-provider-private-value'}
    }
    if ($Arguments -contains 'logs') {
        return [pscustomobject]@{ExitCode=0;StdOut='runtime-container-diagnostic';StdErr=''}
    }
    throw 'Unexpected Docker operation.'
}
try {
    Start-TaipApplication -RepositoryRoot $env:TAIP_TEST_REPOSITORY -PrivateDirectory $env:TAIP_TEST_PRIVATE -DesktopDirectory $env:TAIP_TEST_PRIVATE -AllowInstall $false -Interactive $false -OpenBrowser $false | Out-Null
    $message='unexpected-success'
} catch { $message=$_.Exception.Message }
@{Message=$message; Log=[IO.File]::ReadAllText((Join-Path $env:TAIP_TEST_PRIVATE 'launcher.log'))}
""")
        self.assertIn("Docker could not build or start", result["Message"])
        self.assertIn("native-build-stdout: step failed", result["Log"])
        self.assertIn("native-build-stderr:", result["Log"])
        self.assertIn("runtime-container-diagnostic", result["Log"])
        self.assertIn("[redacted]", result["Log"])
        self.assertNotIn("synthetic-provider-private-value", result["Log"])

    def test_native_failure_is_saved_even_when_container_logs_are_unavailable(self):
        result = self.exercise(r"""
function Invoke-TaipDocker { param($Docker,$Arguments,$TimeoutSeconds) throw 'synthetic daemon unavailable' }
$configuration=[pscustomobject]@{Values=@{GROQ_API_KEY='synthetic-groq-private-value'}}
Save-TaipLauncherDiagnostics -Docker ([pscustomobject]@{}) -ComposeArguments @('compose') -PrivateDirectory $env:TAIP_TEST_PRIVATE -Configuration $configuration -AdditionalOutput 'build failure: synthetic-groq-private-value'
[IO.File]::ReadAllText((Join-Path $env:TAIP_TEST_PRIVATE 'launcher.log'))
""")
        self.assertIn("build failure:", result)
        self.assertIn("[redacted]", result)
        self.assertNotIn("synthetic-groq-private-value", result)

    def test_saved_diagnostics_respect_utf8_byte_limit_and_preserve_latest_failure(self):
        result = self.exercise(r"""
function Invoke-TaipDocker { param($Docker,$Arguments,$TimeoutSeconds) [pscustomobject]@{ExitCode=0;StdOut='runtime-current';StdErr=''} }
$configuration=[pscustomobject]@{Values=@{OPENROUTER_API_KEY='synthetic-openrouter-private-value'}}
$output=(([string][char]0x0103)*50000) + "`nlast-native-build-error synthetic-openrouter-private-value"
Save-TaipLauncherDiagnostics -Docker ([pscustomobject]@{}) -ComposeArguments @('compose') -PrivateDirectory $env:TAIP_TEST_PRIVATE -Configuration $configuration -AdditionalOutput $output
$path=Join-Path $env:TAIP_TEST_PRIVATE 'launcher.log'
@{Bytes=([IO.File]::ReadAllBytes($path).Length); Text=[IO.File]::ReadAllText($path)}
""")
        self.assertLessEqual(result["Bytes"], 65536)
        self.assertIn("last-native-build-error", result["Text"])
        self.assertIn("runtime-current", result["Text"])
        self.assertIn("[redacted]", result["Text"])
        self.assertNotIn("synthetic-openrouter-private-value", result["Text"])
        self.assertNotIn("\ufffd", result["Text"])


if __name__ == "__main__":
    unittest.main()
