"""Windows bootstrap contracts; installers and Docker are always simulated."""

import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "deployment" / "windows"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "Windows PowerShell process contracts require Windows")
class WindowsPrerequisiteTests(unittest.TestCase):
    def run_ps(self, body, *, prerequisites=False, timeout=25):
        module = WINDOWS / ("prerequisites.ps1" if prerequisites else "process.ps1")
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "Set-StrictMode -Version Latest\n"
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            f". {literal(module)}\n" + body
        )
        completed = subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand",
             base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout.splitlines()[-1])

    def test_process_preserves_argument_boundaries_unicode_and_empty_arguments(self):
        arguments = ["", "white space", 'double"quote', "trailing slash \\",
                     'slashes\\\\"quote', "apostrophe'$,; &()", "București"]
        encoded = base64.b64encode(json.dumps(arguments).encode()).decode()
        result = self.run_ps(
            f"$values = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')) | ConvertFrom-Json\n"
            "$code = 'import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=True))'\n"
            f"$result = Invoke-TaipProcess -FilePath {literal(sys.executable)} -Arguments (@('-c', $code) + @($values)) -TimeoutSeconds 5\n"
            "$result | ConvertTo-Json -Compress"
        )
        self.assertEqual(result["ExitCode"], 0)
        self.assertEqual(json.loads(result["StdOut"]), arguments)
        self.assertEqual(result["StdErr"], "")

    def test_process_applies_working_directory_environment_and_exit_code(self):
        with TemporaryDirectory(prefix="taip process '") as directory:
            result = self.run_ps(
                "$env:TAIP_TEST_REMOVE = 'inherited'\n"
                "$code = 'import json,os,sys; print(json.dumps([os.getcwd(),os.getenv(\"TAIP_TEST_SET\"),os.getenv(\"TAIP_TEST_REMOVE\")])); print(\"diagnostic\",file=sys.stderr); sys.exit(7)'\n"
                f"Invoke-TaipProcess -FilePath {literal(sys.executable)} -Arguments @('-c', $code) -TimeoutSeconds 5 -WorkingDirectory {literal(directory)} -Environment @{{ TAIP_TEST_SET='new value'; TAIP_TEST_REMOVE=$null }} | ConvertTo-Json -Compress"
            )
            self.assertEqual(result["ExitCode"], 7)
            self.assertEqual(json.loads(result["StdOut"]), [directory, "new value", None])
            self.assertEqual(result["StdErr"].strip(), "diagnostic")

    def test_process_timeout_is_bounded_and_has_safe_diagnostic(self):
        started = time.monotonic()
        result = self.run_ps(
            "try {\n"
            f"  Invoke-TaipProcess -FilePath {literal(sys.executable)} -Arguments @('-c', 'import time; time.sleep(30)', 'SYNTHETIC_PRIVATE_ARGUMENT') -TimeoutSeconds 1 | Out-Null\n"
            "  @{ Failed=$false } | ConvertTo-Json -Compress\n"
            "} catch { @{ Failed=$true; Message=$_.Exception.Message } | ConvertTo-Json -Compress }"
        )
        self.assertTrue(result["Failed"])
        self.assertIn("timed out", result["Message"].lower())
        self.assertNotIn("SYNTHETIC_PRIVATE_ARGUMENT", result["Message"])
        self.assertLess(time.monotonic() - started, 12)

    def test_docker_invocation_pins_context_and_removes_inherited_overrides(self):
        result = self.run_ps(
            "function Invoke-TaipProcess { param($FilePath,$Arguments,$TimeoutSeconds,$WorkingDirectory,$Environment)\n"
            " [pscustomobject]@{ File=$FilePath; Args=$Arguments; Timeout=$TimeoutSeconds; Directory=$WorkingDirectory; Environment=$Environment } }\n"
            "Invoke-TaipDocker -Docker ([pscustomobject]@{Executable='docker.exe'; Context='desktop-linux'}) -Arguments @('compose','up','--detach') -TimeoutSeconds 19 -WorkingDirectory 'C:\\test space' | ConvertTo-Json -Compress -Depth 5",
            prerequisites=True,
        )
        self.assertEqual(result["Args"], ["--context", "desktop-linux", "compose", "up", "--detach"])
        self.assertEqual(result["Timeout"], 19)
        for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY",
                     "DOCKER_CERT_PATH", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES"):
            self.assertIn(name, result["Environment"])
            self.assertIsNone(result["Environment"][name])

    def test_generated_settings_override_inherited_provider_and_taip_settings_only_in_child(self):
        result = self.run_ps(
            "$env:TAIP_PUBLIC_ORIGIN='https://synthetic-wrong.invalid'\n"
            "$env:GEMINI_MODEL='synthetic-inherited-model'\n"
            "$env:GROQ_SCHEMA='synthetic-inherited-schema'\n"
            "$env:OPENROUTER_API_KEY='synthetic-private-value'\n"
            "$env:OTHER_USER_SETTING='ordinary-user-setting'\n"
            "$script:NativeProcess = ${function:Invoke-TaipProcess}\n"
            "function Invoke-TaipProcess { param($FilePath,$Arguments,$TimeoutSeconds,$WorkingDirectory,$Environment)\n"
            "$code = 'import json,os; print(json.dumps({k:os.getenv(k) for k in [\"TAIP_PUBLIC_ORIGIN\",\"GEMINI_MODEL\",\"GROQ_SCHEMA\",\"OPENROUTER_API_KEY\",\"OTHER_USER_SETTING\"]}))'\n"
            f"& $script:NativeProcess -FilePath {literal(sys.executable)} -Arguments @('-c',$code) -TimeoutSeconds $TimeoutSeconds -Environment $Environment }}\n"
            "$child = Invoke-TaipDocker -Docker ([pscustomobject]@{Executable='docker.exe'; Context='desktop-linux'}) -Arguments @('info') -TimeoutSeconds 5\n"
            "@{Child=($child.StdOut | ConvertFrom-Json); ParentOrigin=$env:TAIP_PUBLIC_ORIGIN; ParentKeyPresent=($env:OPENROUTER_API_KEY -eq 'synthetic-private-value')} | ConvertTo-Json -Compress -Depth 5",
            prerequisites=True,
        )
        self.assertEqual(result["Child"], {"TAIP_PUBLIC_ORIGIN": None, "GEMINI_MODEL": None,
                                          "GROQ_SCHEMA": None, "OPENROUTER_API_KEY": None,
                                          "OTHER_USER_SETTING": "ordinary-user-setting"})
        self.assertEqual(result["ParentOrigin"], "https://synthetic-wrong.invalid")
        self.assertTrue(result["ParentKeyPresent"])

    def test_existing_linux_desktop_is_reused_without_starting_or_installing(self):
        result = self.run_docker_scenario("ready")
        self.assertEqual(result["Docker"], {"Executable": "docker.exe", "Context": "desktop-linux"})
        self.assertFalse(any(call[0] == "winget.exe" for call in result["Calls"]))
        self.assertFalse(any("start" in call for call in result["Calls"]))
        self.assertFalse(any("use" in call for call in result["Calls"]))

    def test_remote_context_and_host_do_not_target_a_remote_engine(self):
        result = self.run_docker_scenario("remote")
        info_calls = [call for call in result["Calls"] if "info" in call]
        self.assertTrue(info_calls)
        self.assertTrue(all(call[1:3] == ["--context", "desktop-linux"] for call in info_calls))
        self.assertFalse(any("use" in call for call in result["Calls"]))

    def test_existing_stopped_desktop_uses_bounded_cli_start(self):
        result = self.run_docker_scenario("stopped")
        starts = [call for call in result["Calls"] if "start" in call]
        self.assertEqual(len(starts), 1)
        self.assertIn("desktop", starts[0])
        self.assertIn("--timeout", starts[0])
        self.assertIn("180", starts[0])
        self.assertTrue(result["Docker"])

    def test_legacy_desktop_fallback_starts_hidden(self):
        result = self.run_docker_scenario("legacy")
        self.assertEqual(result["Started"]["WindowStyle"], "Hidden")
        self.assertEqual(result["Started"]["FilePath"], "C:\\Docker Desktop.exe")
        self.assertTrue(result["Docker"])

    def test_missing_docker_never_installs_when_disabled(self):
        result = self.run_docker_scenario("missing", allow_install=False)
        self.assertIn("Docker Desktop", result["Error"])
        self.assertFalse(any(call[0] == "winget.exe" for call in result["Calls"]))

    def test_install_uses_exact_package_and_no_reboot_flags(self):
        result = self.run_docker_scenario("install")
        installs = [call for call in result["Calls"] if call[0] == "winget.exe"]
        self.assertEqual(installs, [["winget.exe", "install", "--id", "Docker.DockerDesktop", "--exact",
                                    "--source", "winget", "--silent", "--accept-package-agreements",
                                    "--accept-source-agreements", "--disable-interactivity"]])
        self.assertTrue(result["Docker"])

    def test_reboot_install_codes_stop_with_actionable_restart_message(self):
        for code in (-1978334967, -1978334966, -1978334965, 3010, 1641):
            with self.subTest(code=code):
                result = self.run_docker_scenario("reboot", installer_code=code)
                self.assertIn("restart Windows", result["Error"])
                self.assertFalse(any("start" in call for call in result["Calls"]))

    def test_missing_winget_attempts_registration_then_gives_official_fallback(self):
        result = self.run_docker_scenario("no-winget")
        self.assertEqual(result["Registered"], "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe")
        self.assertIn("https://docs.docker.com/desktop/setup/install/windows-install/", result["Error"])

    def test_wsl_or_unavailable_daemon_failure_is_bounded_and_actionable(self):
        result = self.run_docker_scenario("wsl")
        self.assertIn("WSL", result["Error"])
        self.assertTrue("restart" in result["Error"].lower())
        self.assertLessEqual(len([call for call in result["Calls"] if "info" in call]), 100)

    def test_only_remote_contexts_never_receive_daemon_commands(self):
        result = self.run_docker_scenario("only-remote")
        self.assertIn("local Linux engine", result["Error"])
        self.assertFalse(any("info" in call for call in result["Calls"]))
        self.assertFalse(any("use" in call for call in result["Calls"]))

    def test_windows_daemon_is_not_reported_as_ready(self):
        result = self.run_docker_scenario("windows")
        self.assertIsNone(result["Docker"])
        self.assertIn("Linux containers", result["Error"])
        self.assertEqual(len([call for call in result["Calls"] if "start" in call]), 1)
        self.assertLessEqual(len([call for call in result["Calls"] if "info" in call]), 91)

    def test_hung_daemon_is_retried_only_a_bounded_number_of_times(self):
        result = self.run_docker_scenario("hung")
        self.assertIsNone(result["Docker"])
        self.assertIn("180 seconds", result["Error"])
        self.assertLessEqual(len([call for call in result["Calls"] if "info" in call]), 91)

    def test_installation_discovery_supports_both_desktop_locations_without_path(self):
        with TemporaryDirectory(prefix="taip docker install ") as directory:
            base = Path(directory)
            for root in (base / "Program Files" / "Docker" / "Docker",
                         base / "LocalAppData" / "Programs" / "DockerDesktop"):
                with self.subTest(root=root):
                    (root / "resources" / "bin").mkdir(parents=True)
                    (root / "resources" / "bin" / "docker.exe").touch()
                    (root / "Docker Desktop.exe").touch()
                    result = self.run_ps(
                        f"$env:ProgramFiles={literal(base / 'Program Files')}\n"
                        f"$env:LOCALAPPDATA={literal(base / 'LocalAppData')}\n"
                        "function Get-Command { param($Name,$ErrorAction) return $null }\n"
                        "Get-TaipDockerInstallation | ConvertTo-Json -Compress",
                        prerequisites=True,
                    )
                    self.assertEqual(Path(result["Executable"]), root / "resources" / "bin" / "docker.exe")
                    self.assertEqual(Path(result["DesktopExecutable"]), root / "Docker Desktop.exe")
                    shutil.rmtree(root)

    def test_process_captures_large_stdout_and_stderr_without_deadlocking(self):
        result = self.run_ps(
            "$code = 'import sys; sys.stdout.write(\"o\"*100000); sys.stderr.write(\"e\"*100000)'\n"
            f"Invoke-TaipProcess -FilePath {literal(sys.executable)} -Arguments @('-c', $code) -TimeoutSeconds 5 | ConvertTo-Json -Compress"
        )
        self.assertEqual(result["StdOut"], "o" * 100000)
        self.assertEqual(result["StdErr"], "e" * 100000)

    def test_timeout_stops_its_child_and_leaves_an_unrelated_process_running(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: other.terminate() if other.poll() is None else None)
        with TemporaryDirectory(prefix="taip process pid ") as directory:
            pid_path = Path(directory) / "child.pid"
            child_code = f"import pathlib,os,time; pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(30)"
            result = self.run_ps(
                "try {\n"
                f" Invoke-TaipProcess -FilePath {literal(sys.executable)} -Arguments @('-c', {literal(child_code)}) -TimeoutSeconds 1 | Out-Null\n"
                "} catch { }\n"
                f"$childPid = [int](Get-Content -LiteralPath {literal(pid_path)})\n"
                "@{ Alive=($null -ne (Get-Process -Id $childPid -ErrorAction SilentlyContinue)) } | ConvertTo-Json -Compress"
            )
            self.assertFalse(result["Alive"])
            self.assertIsNone(other.poll())
        other.terminate()
        other.wait(timeout=5)

    def run_docker_scenario(self, scenario, *, allow_install=True, installer_code=0):
        return self.run_ps(
            f"$script:Scenario={literal(scenario)}; $script:InstallCode={installer_code}\n"
            "$script:Calls = New-Object System.Collections.Generic.List[object]\n"
            "$script:Installed=$false; $script:Started=$null; $script:Running=$false; $script:Registered=$null\n"
            "$env:DOCKER_HOST='tcp://remote.invalid:2375'; $env:DOCKER_CONTEXT='remote'\n"
            "function Get-TaipDockerInstallation {\n"
            " if ($script:Scenario -in @('missing','install','reboot','no-winget') -and -not $script:Installed) { return $null }\n"
            " [pscustomobject]@{ Executable='docker.exe'; DesktopExecutable='C:\\Docker Desktop.exe' } }\n"
            "function Get-Command { param($Name,$ErrorAction)\n"
            " if ($Name -like '*winget*' -and $script:Scenario -ne 'no-winget') { return [pscustomobject]@{Source='winget.exe'} }\n"
            " return $null }\n"
            "function Add-AppxPackage { param([switch]$RegisterByFamilyName,$MainPackage,$ErrorAction) $script:Registered=$MainPackage }\n"
            "function Start-Process { param($FilePath,$WindowStyle,$ErrorAction) $script:Started=@{FilePath=$FilePath; WindowStyle=$WindowStyle}; $script:Running=$true }\n"
            "function Start-Sleep { param($Seconds,$Milliseconds) }\n"
            "function Invoke-TaipProcess { param($FilePath,$Arguments,$TimeoutSeconds,$WorkingDirectory,$Environment)\n"
            " $script:Calls.Add(@($FilePath)+@($Arguments))\n"
            " $out=''; $err=''; $code=0\n"
            " if ($FilePath -eq 'winget.exe') { $script:Installed=$true; $code=$script:InstallCode }\n"
            " elseif ($Arguments -contains 'ls') { if ($script:Scenario -eq 'only-remote') {$out='remote'} else {$out=\"remote`ndefault`ndesktop-linux\"} }\n"
            " elseif ($Arguments -contains 'inspect') {\n"
            "   if ($Arguments -contains 'remote') {$out='tcp://remote.invalid:2375'} else {$out='npipe:////./pipe/dockerDesktopLinuxEngine'} }\n"
            " elseif ($Arguments -contains 'info') {\n"
            "   if ($script:Scenario -eq 'hung') { throw 'simulated command timeout' }\n"
            "   elseif ($script:Scenario -eq 'windows') {$out='windows'}\n"
            "   elseif ($script:Scenario -in @('stopped','legacy','install') -and -not $script:Running) { $code=1; $err='daemon unavailable' }\n"
            "   elseif ($script:Scenario -eq 'wsl') {$code=1; $err='WSL needs kernel update; restart Windows'}\n"
            "   else {$out='linux'} }\n"
            " elseif ($Arguments -contains 'start') {\n"
            "   if ($script:Scenario -eq 'legacy') { $code=1; $err=\"unknown command: desktop\" }\n"
            "   elseif ($script:Scenario -eq 'wsl') {$code=1; $err='WSL needs kernel update; restart Windows'}\n"
            "   else {$script:Running=$true} }\n"
            " [pscustomobject]@{ExitCode=$code; StdOut=$out; StdErr=$err} }\n"
            f"try {{ $docker=Ensure-TaipDocker -AllowInstall ${str(allow_install).lower()}; $errorText=$null }} catch {{ $docker=$null; $errorText=$_.Exception.Message }}\n"
            "@{Docker=$docker; Error=$errorText; Calls=@($script:Calls.ToArray()); Started=$script:Started; Registered=$script:Registered} | ConvertTo-Json -Compress -Depth 7",
            prerequisites=True,
        )


if __name__ == "__main__":
    unittest.main()
