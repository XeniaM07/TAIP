"""Windows browser handoff boundaries are simulated, without network or secrets."""

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


MOCK_TRANSPORT = r"""
$script:ResponseText='{"code":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}'
$script:ResponseLength=-1; $script:Status=200; $script:ResponseType='application/json'
$script:TransportError=$false; $script:DisposeError=$false; $script:FactoryCalls=0; $script:ResponseDisposed=$false; $script:Aborted=$false
$script:ResponseStream=$null; $script:CapturedRequest=$null
function New-TaipHttpRequest {
    param($Uri)
    $script:FactoryCalls++
    $request=[pscustomobject]@{RequestUri=[string]$Uri;Method='';ContentLength=-1;Proxy='inherited-proxy';AllowAutoRedirect=$true;Timeout=0;ReadWriteTimeout=0;Headers=@{};Accept=''}
    $request | Add-Member -MemberType ScriptMethod -Name GetResponse -Value {
        if ($script:TransportError) { throw 'upstream response synthetic-sensitive-response xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' }
        $script:ResponseStream=[IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes($script:ResponseText))
        $response=[pscustomobject]@{StatusCode=$script:Status;ContentLength=$script:ResponseLength;ContentType=$script:ResponseType}
        $response | Add-Member -MemberType ScriptMethod -Name GetResponseStream -Value { return $script:ResponseStream }
        $response | Add-Member -MemberType ScriptMethod -Name Dispose -Value { $script:ResponseDisposed=$true; if ($script:DisposeError) { throw 'synthetic-sensitive-response' } }
        return $response
    }
    $request | Add-Member -MemberType ScriptMethod -Name Abort -Value { $script:Aborted=$true }
    $script:CapturedRequest=$request
    return $request
}
"""


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell is required for launcher login")
class LauncherBrowserLoginTests(unittest.TestCase):
    def exercise(self, body):
        with TemporaryDirectory(prefix="taip browser handoff '") as temporary:
            script = (
                "$ErrorActionPreference='Stop'\n$ProgressPreference='SilentlyContinue'\n"
                "Set-StrictMode -Version Latest\n"
                "[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)\n"
                ". $env:TAIP_TEST_SCRIPT\n$result=& {\n" + body + "\n} 6>$null\n"
                "ConvertTo-Json -InputObject $result -Depth 8 -Compress\n"
            )
            completed = subprocess.run(
                [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                 "Bypass", "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                env={**os.environ, "TAIP_TEST_SCRIPT": str(SCRIPT), "TAIP_TEST_PRIVATE": temporary},
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertFalse(completed.stderr, completed.stderr)
            return json.loads(completed.stdout)

    def test_mint_posts_empty_body_with_bearer_and_bounded_secure_transport(self):
        result = self.exercise(MOCK_TRANSPORT + r"""
$code=New-TaipBrowserLaunchCode -Origin 'https://localhost:8443/' -AccessToken ('x'*64)
@{Code=$code;Uri=$script:CapturedRequest.RequestUri;Method=$script:CapturedRequest.Method;Length=$script:CapturedRequest.ContentLength;Proxy=$script:CapturedRequest.Proxy;Redirects=$script:CapturedRequest.AllowAutoRedirect;Timeout=$script:CapturedRequest.Timeout;ReadTimeout=$script:CapturedRequest.ReadWriteTimeout;Authorization=$script:CapturedRequest.Headers['Authorization'];Disposed=$script:ResponseDisposed;Calls=$script:FactoryCalls}
""")
        self.assertEqual(result["Code"], "b" * 43)
        self.assertEqual(result["Uri"], "https://localhost:8443/_launcher/code")
        self.assertEqual(result["Method"], "POST")
        self.assertEqual(result["Length"], 0)
        self.assertEqual(result["Authorization"], "Bearer " + "x" * 64)
        self.assertIsNone(result["Proxy"])
        self.assertFalse(result["Redirects"])
        self.assertEqual(result["Timeout"], 10000)
        self.assertEqual(result["ReadTimeout"], 10000)
        self.assertTrue(result["Disposed"])
        self.assertEqual(result["Calls"], 1)

    def test_invalid_origins_are_rejected_before_transport_and_never_echoed(self):
        result = self.exercise(MOCK_TRANSPORT + r"""
$origins=@('http://localhost:8443','https://evil.invalid','https://127.0.0.1:8443','https://localhost:8443/path','https://localhost:8443/../','https://localhost:8443?private=value','https://localhost:8443/#private=value','https://user:private@localhost:8443','https://localhost:0')
$messages=@(foreach ($origin in $origins) {
    try { New-TaipBrowserLaunchCode -Origin $origin -AccessToken ('x'*64) | Out-Null; 'unexpected-success' } catch { $_.Exception.Message }
})
@{Messages=$messages;Calls=$script:FactoryCalls}
""")
        self.assertEqual(result["Calls"], 0)
        self.assertEqual(set(result["Messages"]), {"Could not create an automatic browser login. Run Start-TAIP.cmd again."})
        self.assertNotIn("unexpected-success", result["Messages"])
        self.assertNotIn("private", " ".join(result["Messages"]))

    def test_invalid_application_credential_is_rejected_before_transport(self):
        result = self.exercise(MOCK_TRANSPORT + r"""
$messages=@(foreach ($token in @('synthetic-short-private', (('x'*64)+"`n"), (('x'*63)+' '))) {
    try { New-TaipBrowserLaunchCode -Origin 'https://localhost:8443' -AccessToken $token | Out-Null; 'unexpected-success' } catch { $_.Exception.Message }
})
@{Messages=$messages;Calls=$script:FactoryCalls}
""")
        self.assertEqual(result["Calls"], 0)
        self.assertEqual(set(result["Messages"]), {"Could not create an automatic browser login. Run Start-TAIP.cmd again."})
        self.assertNotIn("synthetic-short-private", " ".join(result["Messages"]))

    def test_malformed_or_oversized_code_responses_fail_without_echoing_body(self):
        result = self.exercise(MOCK_TRANSPORT + r"""
$payloads=@('not-json synthetic-sensitive-response','{}','{"code":123}','{"code":[]}', ('{"code":"'+('b'*42)+'"}'), ('{"code":"'+('b'*44)+'"}'), ('{"code":"'+('b'*42)+'!"}'), ('{"code":"'+('b'*43)+'\n"}'))
$messages=@(foreach ($payload in $payloads) {
    $script:ResponseText=$payload
    try { New-TaipBrowserLaunchCode -Origin 'https://localhost:8443' -AccessToken ('x'*64) | Out-Null; 'unexpected-success' } catch { $_.Exception.Message }
})
@{Messages=$messages;Calls=$script:FactoryCalls}
""")
        self.assertEqual(result["Calls"], 8)
        self.assertEqual(set(result["Messages"]), {"Could not create an automatic browser login. Run Start-TAIP.cmd again."})
        self.assertNotIn("unexpected-success", result["Messages"])
        self.assertNotIn("synthetic-sensitive-response", " ".join(result["Messages"]))

    def test_response_body_is_limited_by_bytes_even_without_content_length(self):
        result = self.exercise(MOCK_TRANSPORT + r"""
$script:ResponseText='{"code":"'+('b'*43)+'","extra":"'+('a'*5000)+'"}'
try { New-TaipBrowserLaunchCode -Origin 'https://localhost:8443' -AccessToken ('x'*64) | Out-Null; $message='unexpected-success' } catch { $message=$_.Exception.Message }
@{Message=$message;Disposed=$script:ResponseDisposed;Aborted=$script:Aborted}
""")
        self.assertNotEqual(result["Message"], "unexpected-success")
        self.assertTrue(result["Disposed"])
        self.assertTrue(result["Aborted"])

    def test_non_success_redirect_and_declared_oversize_are_rejected(self):
        result = self.exercise(MOCK_TRANSPORT + r"""
$messages=@(foreach ($status in @(302,401,503)) {
    $script:Status=$status
    try { New-TaipBrowserLaunchCode -Origin 'https://localhost:8443' -AccessToken ('x'*64) | Out-Null; 'unexpected-success' } catch { $_.Exception.Message }
})
$script:Status=200; $script:ResponseLength=4097
try { New-TaipBrowserLaunchCode -Origin 'https://localhost:8443' -AccessToken ('x'*64) | Out-Null; $messages+='unexpected-success' } catch { $messages+=$_.Exception.Message }
@{Messages=$messages;Calls=$script:FactoryCalls;Disposed=$script:ResponseDisposed}
""")
        self.assertEqual(result["Calls"], 4)
        self.assertEqual(set(result["Messages"]), {"Could not create an automatic browser login. Run Start-TAIP.cmd again."})
        self.assertNotIn("unexpected-success", result["Messages"])
        self.assertTrue(result["Disposed"])

    def test_response_reading_stops_at_total_ten_second_deadline(self):
        result = self.exercise(MOCK_TRANSPORT + r"""
Update-TypeData -TypeName System.Diagnostics.Stopwatch -MemberType ScriptProperty -MemberName ElapsedMilliseconds -Value { 10001 } -Force
try { New-TaipBrowserLaunchCode -Origin 'https://localhost:8443' -AccessToken ('x'*64) | Out-Null; $message='unexpected-success' } catch { $message=$_.Exception.Message }
@{Message=$message;Aborted=$script:Aborted;Disposed=$script:ResponseDisposed}
""")
        self.assertNotEqual(result["Message"], "unexpected-success")
        self.assertTrue(result["Aborted"])
        self.assertTrue(result["Disposed"])

    def test_transport_failure_does_not_echo_response_or_application_token(self):
        result = self.exercise(MOCK_TRANSPORT + r"""
$script:TransportError=$true
try { New-TaipBrowserLaunchCode -Origin 'https://localhost:8443' -AccessToken ('x'*64) | Out-Null; $message='unexpected-success' } catch { $message=$_.Exception.Message }
@{Message=$message;Calls=$script:FactoryCalls;Aborted=$script:Aborted}
""")
        self.assertEqual(result["Calls"], 1)
        self.assertNotIn("synthetic-sensitive-response", result["Message"])
        self.assertNotIn("x" * 64, result["Message"])
        self.assertTrue(result["Aborted"])

    def test_response_cleanup_does_not_disclose_error_details(self):
        result = self.exercise(MOCK_TRANSPORT + r"""
$script:DisposeError=$true
$code=New-TaipBrowserLaunchCode -Origin 'https://localhost:8443' -AccessToken ('x'*64)
@{Code=$code;Disposed=$script:ResponseDisposed}
""")
        self.assertEqual(result["Code"], "b" * 43)
        self.assertTrue(result["Disposed"])

    def test_invalid_json_stays_private_when_caller_allows_nonterminating_errors(self):
        result = self.exercise(MOCK_TRANSPORT + r"""
$ErrorActionPreference='Continue'
$script:ResponseText='not-json synthetic-sensitive-response'
try { New-TaipBrowserLaunchCode -Origin 'https://localhost:8443' -AccessToken ('x'*64) | Out-Null; $message='unexpected-success' } catch { $message=$_.Exception.Message }
@{Message=$message}
""")
        self.assertEqual(result["Message"], "Could not create an automatic browser login. Run Start-TAIP.cmd again.")

    def test_browser_error_never_echoes_ephemeral_fragment_code(self):
        result = self.exercise(r"""
[IO.File]::WriteAllText((Join-Path $env:TAIP_TEST_PRIVATE 'access-token.txt'),('x'*64))
function New-TaipBrowserLaunchCode { param($Origin,$AccessToken) return ('b'*43) }
function Start-Process { param($FilePath,$WindowStyle) throw ('Could not open '+$FilePath) }
try { Open-TaipApplication -Origin 'https://localhost:8443' -PrivateDirectory $env:TAIP_TEST_PRIVATE; $message='unexpected-success' } catch { $message=$_.Exception.Message }
@{Message=$message;LoginExists=(Test-Path -LiteralPath (Join-Path $env:TAIP_TEST_PRIVATE 'login.txt'))}
""")
        self.assertNotEqual(result["Message"], "unexpected-success")
        self.assertNotIn("b" * 43, result["Message"])
        self.assertNotIn("x" * 64, result["Message"])
        self.assertFalse(result["LoginExists"])


if __name__ == "__main__":
    unittest.main()
