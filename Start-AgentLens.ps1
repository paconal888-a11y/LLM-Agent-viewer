param([switch]$NoBrowser, [switch]$SamplesOnly, [ValidateSet('reference','llama')][string]$Backend='reference')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$agentLensRoot = Join-Path $env:LOCALAPPDATA 'AgentLens'
$runtimeExe = Join-Path $agentLensRoot 'runtime\b12110-vulkan\llama-server.exe'
$modelFile = Join-Path $agentLensRoot 'models\Qwen3-4B-Q4_K_M.gguf'
$localDir = Join-Path $projectDir '.local'
[System.IO.Directory]::CreateDirectory($localDir) | Out-Null
[System.IO.Directory]::CreateDirectory((Join-Path $agentLensRoot 'logs')) | Out-Null

if (-not $SamplesOnly -and $Backend -eq 'llama' -and (-not (Test-Path -LiteralPath $runtimeExe) -or -not (Test-Path -LiteralPath $modelFile))) {
    & (Join-Path $projectDir 'scripts\setup-runtime.ps1')
}
Push-Location -LiteralPath $projectDir
try {
    if ($SamplesOnly) { & uv sync --python 3.12 --inexact --locked --system-certs }
    elseif ($Backend -eq 'reference') { & uv sync --python 3.12 --extra reference --extra integrations --locked --system-certs }
    else { & uv sync --python 3.12 --extra integrations --inexact --locked --system-certs }
    if ($LASTEXITCODE -ne 0) { throw 'Python environment setup failed.' }
    if (-not $SamplesOnly -and $Backend -eq 'reference') {
        & (Join-Path $projectDir '.venv\Scripts\python.exe') (Join-Path $projectDir 'scripts\setup_reference.py')
        if ($LASTEXITCODE -ne 0) { throw 'Reference model setup failed.' }
    }
    $owned = @{ engine = $null; app = $null }
    $pidFile = Join-Path $localDir 'processes.json'
    if (Test-Path -LiteralPath $pidFile) {
        try {
            $previous = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
            if ($previous.PSObject.Properties.Name -contains 'engine') { $owned.engine = $previous.engine }
            if ($previous.PSObject.Properties.Name -contains 'app') { $owned.app = $previous.app }
        } catch { }
    }
    $engineReady = $false
    try { $engineReady = (Invoke-RestMethod -Uri 'http://127.0.0.1:8088/health' -TimeoutSec 2).status -eq 'ok' } catch { }
    if (-not $SamplesOnly -and $Backend -eq 'llama' -and -not $engineReady) {
        $arguments = @('-m', ('"' + $modelFile + '"'), '--host', '127.0.0.1', '--port', '8088', '--alias', 'qwen3-4b', '-c', '12288', '-np', '1', '-ngl', '99', '-t', '6', '--jinja', '--reasoning', 'off', '--no-webui', '--cors-origins', 'localhost')
        $engine = Start-Process -FilePath $runtimeExe -ArgumentList $arguments -WorkingDirectory $agentLensRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $agentLensRoot 'logs\engine.stdout.log') -RedirectStandardError (Join-Path $agentLensRoot 'logs\engine.stderr.log') -PassThru
        $owned.engine = @{ pid = $engine.Id; path = $runtimeExe }
    }
    $appReady = $false
    try {
        $appStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/status' -TimeoutSec 2
        $appReady = if ($SamplesOnly) { $appStatus.mode -eq 'sample' } else { $appStatus.mode -ne 'sample' -and $appStatus.backend -eq $Backend }
        if (-not $appReady -and $owned.app) {
            $oldApp = Get-CimInstance Win32_Process -Filter "ProcessId=$($owned.app.pid)"
            if ($oldApp -and $oldApp.CommandLine -like '*local_agent_lab.web*') { & (Join-Path $projectDir 'scripts\stop-owned-app.ps1') -AppProcessId $owned.app.pid }
        }
    } catch { }
    if (-not $appReady) {
        $pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
        $webArguments = @('-m', 'local_agent_lab.web','--backend',$Backend)
        if ($SamplesOnly) { $webArguments += '--samples-only' }
        $appProcess = Start-Process -FilePath $pythonExe -ArgumentList $webArguments -WorkingDirectory $projectDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $localDir 'app.stdout.log') -RedirectStandardError (Join-Path $localDir 'app.stderr.log') -PassThru
        $owned.app = @{ pid = $appProcess.Id; path = $pythonExe }
    }
    $owned | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $pidFile -Encoding utf8
    Write-Host 'Agent Lens: http://127.0.0.1:8765'
    if ($SamplesOnly) { Write-Host '실제 저장 기록을 재생합니다. 모델을 다운로드하거나 불러오지 않습니다.' }
    else { Write-Host '모델을 불러오는 중일 수 있습니다. 화면에서 상태를 확인하세요.' }
    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8765' }
} finally { Pop-Location }
