"""Certificate trust uses mocked store/process boundaries, never the real store."""

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


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell is required for launcher certificates")
class LauncherCertificateTests(unittest.TestCase):
    def exercise(self, scenario):
        with TemporaryDirectory(prefix="taip synthetic certificate '") as temporary:
            script = r"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
Set-StrictMode -Version Latest
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
. $env:TAIP_TEST_SCRIPT
$scenario=$env:TAIP_TEST_SCENARIO
$certificatePath=Join-Path $env:TAIP_TEST_PRIVATE 'caddy-data\caddy\pki\authorities\local\root.crt'
[void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($certificatePath))
$rsa=[Security.Cryptography.RSACng]::new(2048)
try {
    $request=[Security.Cryptography.X509Certificates.CertificateRequest]::new('CN=Synthetic TAIP Local CA',$rsa,[Security.Cryptography.HashAlgorithmName]::SHA256,[Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $isCA=($scenario -ne 'not-ca')
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($isCA,$false,0,$true))
    $notBefore=[DateTimeOffset]::UtcNow.AddDays(-3)
    $notAfter=[DateTimeOffset]::UtcNow.AddDays(3)
    if ($scenario -eq 'expired') { $notAfter=[DateTimeOffset]::UtcNow.AddDays(-2) }
    if ($scenario -eq 'future') { $notBefore=[DateTimeOffset]::UtcNow.AddDays(3); $notAfter=[DateTimeOffset]::UtcNow.AddDays(6) }
    if ($scenario -eq 'not-self-issued') {
        $issuer=[Security.Cryptography.X509Certificates.X500DistinguishedName]::new('CN=Synthetic Other Issuer')
        $signer=[Security.Cryptography.X509Certificates.X509SignatureGenerator]::CreateForRSA($rsa,[Security.Cryptography.RSASignaturePadding]::Pkcs1)
        $certificate=$request.Create($issuer,$signer,$notBefore,$notAfter,[byte[]]@(1,2,3))
    } else { $certificate=$request.CreateSelfSigned($notBefore,$notAfter) }
    try {
        [IO.File]::WriteAllBytes($certificatePath,$certificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Cert))
        $expectedThumbprint=$certificate.Thumbprint
    } finally { $certificate.Dispose() }
} finally { $rsa.Dispose() }
$script:Trusted=($scenario -eq 'already-trusted')
$script:Calls=New-Object System.Collections.Generic.List[object]
$script:StorePaths=New-Object System.Collections.Generic.List[string]
function Test-Path {
    param($LiteralPath,$PathType)
    if ($LiteralPath -like 'Cert:\CurrentUser\Root\*') {
        $script:StorePaths.Add($LiteralPath)
        return $script:Trusted
    }
    return Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -PathType Leaf
}
function Import-Certificate { throw 'The certificate cmdlet must not be used.' }
function Invoke-TaipProcess {
    param($FilePath,$Arguments,$TimeoutSeconds)
    $script:Calls.Add([pscustomobject]@{File=$FilePath;Args=$Arguments;Timeout=$TimeoutSeconds})
    if ($scenario -eq 'native-error') { return [pscustomobject]@{ExitCode=5;StdOut='native certificate store denied';StdErr='synthetic trust failure'} }
    if ($scenario -ne 'not-imported') { $script:Trusted=$true }
    return [pscustomobject]@{ExitCode=0;StdOut='synthetic import complete';StdErr=''}
}
$message=$null
try { Ensure-TaipCertificate -PrivateDirectory $env:TAIP_TEST_PRIVATE -TimeoutSeconds 1 6>$null } catch { $message=$_.Exception.Message }
@{Error=$message;Calls=@($script:Calls.ToArray());StorePaths=@($script:StorePaths.ToArray());Thumbprint=$expectedThumbprint;CertificatePath=$certificatePath} | ConvertTo-Json -Compress -Depth 6
"""
            completed = subprocess.run(
                [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-EncodedCommand",
                 base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                env={**os.environ, "TAIP_TEST_SCRIPT": str(SCRIPT),
                     "TAIP_TEST_PRIVATE": temporary, "TAIP_TEST_SCENARIO": scenario},
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertFalse(completed.stderr, completed.stderr)
            return json.loads(completed.stdout)

    def test_untrusted_ca_uses_bounded_native_current_user_import_and_verifies_thumbprint(self):
        result = self.exercise("import")
        self.assertIsNone(result["Error"])
        self.assertEqual(len(result["Calls"]), 1)
        call = result["Calls"][0]
        self.assertEqual(Path(call["File"]).name.lower(), "certutil.exe")
        self.assertEqual(call["Args"], ["-user", "-addstore", "Root", result["CertificatePath"]])
        self.assertGreater(call["Timeout"], 0)
        self.assertLessEqual(call["Timeout"], 60)
        expected = "Cert:\\CurrentUser\\Root\\" + result["Thumbprint"]
        self.assertEqual(result["StorePaths"], [expected, expected])

    def test_existing_exact_certificate_is_not_imported_again(self):
        result = self.exercise("already-trusted")
        self.assertIsNone(result["Error"])
        self.assertEqual(result["Calls"], [])
        self.assertEqual(result["StorePaths"], ["Cert:\\CurrentUser\\Root\\" + result["Thumbprint"]])

    def test_native_import_error_is_actionable_and_not_reported_as_success(self):
        result = self.exercise("native-error")
        self.assertIn("certificate", result["Error"].lower())
        self.assertIn("5", result["Error"])
        self.assertIn("synthetic trust failure", result["Error"])
        self.assertEqual(len(result["Calls"]), 1)

    def test_successful_exit_without_exact_trust_entry_still_fails(self):
        result = self.exercise("not-imported")
        self.assertIsNotNone(result["Error"])
        self.assertIn("certificate", result["Error"].lower())
        self.assertEqual(len(result["StorePaths"]), 2)

    def test_invalid_ca_or_validity_period_never_reaches_import(self):
        for scenario in ("expired", "future", "not-ca", "not-self-issued"):
            with self.subTest(scenario=scenario):
                result = self.exercise(scenario)
                self.assertIsNotNone(result["Error"], "Invalid certificate must be rejected before import")
                self.assertIn("invalid", result["Error"].lower())
                self.assertEqual(result["Calls"], [])
                self.assertEqual(result["StorePaths"], [])


if __name__ == "__main__":
    unittest.main()
