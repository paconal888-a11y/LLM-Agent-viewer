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
from unittest import mock

from local_agent_lab.paths import data_home, reference_model_path
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

    def test_cmd_entry_points_do_not_inherit_a_powershell_7_module_path(self):
        for name in ("Start-AgentLens.cmd", "Stop-AgentLens.cmd"):
            with self.subTest(script=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertLess(text.index("setlocal"), text.index('set "PSModulePath="'))
                self.assertLess(text.index('set "PSModulePath="'), text.index("powershell -NoProfile"))

    def test_data_home_follows_the_override(self):
        with mock.patch.dict(os.environ, {"AGENT_LENS_HOME": "D:/agent-lens-data"}):
            self.assertEqual(data_home(), Path("D:/agent-lens-data"))
            self.assertEqual(
                reference_model_path(),
                Path("D:/agent-lens-data/models/Qwen3-0.6B"),
            )

    def test_setup_honours_the_same_override_as_the_launcher(self):
        for script in (LAUNCHER, SETUP):
            with self.subTest(script=script.name):
                self.assertIn(
                    "if ($env:AGENT_LENS_HOME) { $agentLensRoot = $env:AGENT_LENS_HOME }",
                    script.read_text(encoding="utf-8-sig"),
                )

    def test_curl_runs_outside_stop_preference(self):
        text = SETUP.read_text(encoding="utf-8-sig")
        guarded = re.search(
            r"\$ErrorActionPreference = 'Continue'\s+try \{[\s\S]*?& curl\.exe", text
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
                # 콘솔에서 시작한 Windows PowerShell과 같은 조건. PowerShell 7이 물려준 모듈 경로를 지운다.
                **{k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"},
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

    def find_packaged(self, *required):
        script = r"""
Invoke-Expression (Import-ScriptFunction $env:PROBE_LAUNCHER 'Find-PackagedDataHome')
$found = Find-PackagedDataHome @($env:PROBE_REQUIRED -split ';')
if ($found) { "found=$found" } else { 'found=none' }
"""
        return self.powershell(
            script,
            LOCALAPPDATA=str(self.root),
            PROBE_REQUIRED=";".join(required),
        ).stdout.strip()

    def test_finds_runtime_stored_in_an_app_package_without_touching_it(self):
        required = ("runtime\\llama-server.exe", "models\\model.gguf")
        partial = self.root / "Packages" / "A.Partial" / "LocalCache" / "Local" / "AgentLens"
        (partial / "runtime").mkdir(parents=True)
        (partial / "runtime" / "llama-server.exe").write_bytes(b"x")
        self.assertEqual(self.find_packaged(*required), "found=none")
        complete = self.root / "Packages" / "B.Complete" / "LocalCache" / "Local" / "AgentLens"
        (complete / "runtime").mkdir(parents=True)
        (complete / "models").mkdir()
        (complete / "runtime" / "llama-server.exe").write_bytes(b"exe")
        (complete / "models" / "model.gguf").write_bytes(b"gguf")
        before = sorted(str(p) for p in self.root.rglob("*"))
        # 임시 폴더가 8.3 짧은 이름이면 PowerShell이 긴 이름으로 돌려준다.
        found = self.find_packaged(*required).removeprefix("found=")
        self.assertEqual(Path(found).resolve(), complete.resolve())
        after = [p for p in sorted(str(p) for p in self.root.rglob("*")) if "probe.ps1" not in p]
        self.assertEqual([p for p in before if "probe.ps1" not in p], after)

    def test_no_packages_folder_means_nothing_is_found(self):
        self.assertEqual(self.find_packaged("models\\model.gguf"), "found=none")

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
