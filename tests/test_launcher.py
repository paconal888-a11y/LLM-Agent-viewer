"""런처의 네이티브 명령 호출이 Windows PowerShell 5.1의 오류 스트림 변환에 끊기지 않는지 확인한다.

실제 Windows PowerShell과 자식 프로세스를 실행하지만 uv·모델 다운로드는 하지 않는다.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from local_agent_lab.trace import ROOT

POWERSHELL = shutil.which("powershell") if os.name == "nt" else None
LAUNCHER = ROOT / "Start-AgentLens.ps1"
SETUP = ROOT / "scripts" / "setup-runtime.ps1"
EXTRACT = r"""
function Import-ScriptFunction([string]$Path, [string]$Name) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $Name }, $true)
    if ($null -eq $definition) { throw "function not found: $Name" }
    return $definition.Extent.Text
}
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
"""
STDERR_CODE = "import sys; print(1, file=sys.stderr); sys.exit(int(sys.argv[1]))"


class LauncherSourceTests(unittest.TestCase):
    def test_every_native_call_in_the_launcher_is_guarded(self):
        text = LAUNCHER.read_text(encoding="utf-8-sig")
        self.assertNotRegex(text, r"(?m)&\s+uv\b")
        self.assertNotRegex(text, r"(?m)&\s+\(Join-Path \$projectDir '\.venv")
        self.assertGreaterEqual(len(re.findall(r"Invoke-Native\s+'uv'", text)), 3)

    def test_curl_runs_outside_stop_preference(self):
        text = SETUP.read_text(encoding="utf-8-sig")
        guarded = re.search(
            r"\$ErrorActionPreference = 'Continue'\s+try \{ & curl\.exe", text
        )
        self.assertIsNotNone(guarded, "curl 호출이 Continue 구간 밖에 있습니다.")


@unittest.skipUnless(POWERSHELL, "Windows PowerShell 전용 동작")
class NativeStderrTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def powershell(self, body, **env):
        script = self.root / "probe.ps1"
        script.write_text(EXTRACT + body, encoding="ascii")
        return subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={
                **os.environ,
                "PROBE_PYTHON": sys.executable,
                "PROBE_CODE": STDERR_CODE,
                "PROBE_LAUNCHER": str(LAUNCHER),
                "PROBE_SETUP": str(SETUP),
                **env,
            },
            timeout=60,
        )

    def test_direct_call_is_what_the_guard_protects_against(self):
        # 대조군: 보호 없이 호출하면 정상 stderr만으로 'Stop' 스크립트가 끝난다(5.1의 실제 동작).
        result = self.powershell(
            r"""
function Probe { & $env:PROBE_PYTHON -c $env:PROBE_CODE 0; "after-exit=$LASTEXITCODE" }
Probe 2>&1 | ForEach-Object { "$_" }
"""
        )
        self.assertNotIn("after-exit=", result.stdout)

    def test_invoke_native_survives_redirected_error_stream(self):
        for code in (0, 3):
            with self.subTest(exit_code=code):
                result = self.powershell(
                    r"""
Invoke-Expression (Import-ScriptFunction $env:PROBE_LAUNCHER 'Invoke-Native')
function Probe { Invoke-Native $env:PROBE_PYTHON @('-c', $env:PROBE_CODE, $env:PROBE_EXIT); "after-exit=$LASTEXITCODE" }
Probe 2>&1 | ForEach-Object { "$_" }
""",
                    PROBE_EXIT=str(code),
                )
                self.assertIn(f"after-exit={code}", result.stdout, result.stderr)

    def test_invoke_native_still_reports_a_missing_command(self):
        result = self.powershell(
            r"""
Invoke-Expression (Import-ScriptFunction $env:PROBE_LAUNCHER 'Invoke-Native')
Invoke-Native 'agent-lens-no-such-command' @('x')
"after-call"
"""
        )
        self.assertNotIn("after-call", result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_curl_progress_does_not_abort_verified_download(self):
        # 네트워크 없이 file:// 주소로 실제 curl.exe를 실행해 stderr 진행 표시를 만든다.
        source = self.root / "payload.bin"
        source.write_bytes(b"agent lens" * 4096)
        destination = self.root / "copied.bin"
        result = self.powershell(
            r"""
Invoke-Expression (Import-ScriptFunction $env:PROBE_SETUP 'Get-VerifiedFile')
function Probe { Get-VerifiedFile $env:PROBE_URL $env:PROBE_DESTINATION $env:PROBE_SHA; "copied=" + (Test-Path -LiteralPath $env:PROBE_DESTINATION) }
Probe 2>&1 | ForEach-Object { "$_" }
""",
            PROBE_URL=source.as_uri(),
            PROBE_DESTINATION=str(destination),
            PROBE_SHA=hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        self.assertIn("copied=True", result.stdout, result.stderr)
        self.assertEqual(destination.read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
