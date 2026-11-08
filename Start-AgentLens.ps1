param(
    [switch]$NoBrowser,
    [switch]$SamplesOnly,
    [ValidateSet('reference','llama')][string]$Backend='reference',
    [ValidateRange(1024, 65535)][int]$Port=8765
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$agentLensRoot = Join-Path $env:LOCALAPPDATA 'AgentLens'
$runtimeExe = Join-Path $agentLensRoot 'runtime\b12110-vulkan\llama-server.exe'
$modelFile = Join-Path $agentLensRoot 'models\Qwen3-4B-Q4_K_M.gguf'
$localDir = Join-Path $projectDir '.local'
$stdoutLog = Join-Path $localDir 'app.stdout.log'
$stderrLog = Join-Path $localDir 'app.stderr.log'
$appUrl = 'http://127.0.0.1:{0}' -f $Port
[System.IO.Directory]::CreateDirectory($localDir) | Out-Null
[System.IO.Directory]::CreateDirectory((Join-Path $agentLensRoot 'logs')) | Out-Null

function Get-Prop($object, [string]$name, $default = $null) {
    if ($null -eq $object) { return $default }
    if ($object -is [hashtable]) { if ($object.ContainsKey($name)) { return $object[$name] } else { return $default } }
    if ($object.PSObject.Properties.Name -contains $name) { return $object.$name }
    return $default
}
function Get-ExpectedVersion {
    $match = Select-String -LiteralPath (Join-Path $projectDir 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($match) { return $match.Matches[0].Groups[1].Value }
    return 'unknown'
}
function Get-NormalizedPath([string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) { return '' }
    try { return [System.IO.Path]::GetFullPath($path).TrimEnd('\').ToLowerInvariant() } catch { return $path.ToLowerInvariant() }
}
function Get-AppStatus {
    try { return Invoke-RestMethod -Uri ($appUrl + '/api/status') -TimeoutSec 2 } catch { return $null }
}
function Test-SameProject($status) {
    # 0.5.0 이전 앱은 project_root를 보고하지 않는다. 식별할 수 없으면 같은 프로젝트로 간주하지 않는다.
    $reported = Get-Prop $status 'project_root' ''
    if ($reported -eq '') { return $false }
    return (Get-NormalizedPath $reported) -eq (Get-NormalizedPath $projectDir)
}
function Get-PortListener {
    try { $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1 } catch { return $null }
    if ($null -eq $connection) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($connection.OwningProcess)"
    $name = '?'
    if ($process) { $name = $process.Name }
    return @{ pid = $connection.OwningProcess; name = $name }
}
function Test-OwnedProcess($entry) {
    # PID만으로 판단하지 않는다. 실행 파일 경로와 시작 시각까지 기록과 같아야 이 프로젝트가 띄운 프로세스다.
    if ($null -eq $entry) { return $false }
    $running = Get-Process -Id (Get-Prop $entry 'pid' 0) -ErrorAction Ignore
    if ($null -eq $running) { return $false }
    if ($running.Path -ne (Get-Prop $entry 'path' '')) { return $false }
    $recorded = Get-Prop $entry 'started' ''
    if ($recorded -ne '') {
        try { if ([math]::Abs(($running.StartTime - [datetime]$recorded).TotalSeconds) -gt 3) { return $false } } catch { return $false }
    }
    return $true
}
function Get-LogTail {
    if (Test-Path -LiteralPath $stderrLog) { return ((Get-Content -LiteralPath $stderrLog -Tail 20) -join "`n") }
    return ''
}

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
    $expectedVersion = Get-ExpectedVersion
    $owned = @{ engine = $null; app = $null }
    $pidFile = Join-Path $localDir 'processes.json'
    if (Test-Path -LiteralPath $pidFile) {
        try {
            $previous = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
            $owned.engine = Get-Prop $previous 'engine'
            $owned.app = Get-Prop $previous 'app'
        } catch { }
    }
    $engineReady = $false
    try { $engineReady = (Invoke-RestMethod -Uri 'http://127.0.0.1:8088/health' -TimeoutSec 2).status -eq 'ok' } catch { }
    if (-not $SamplesOnly -and $Backend -eq 'llama' -and -not $engineReady) {
        $arguments = @('-m', ('"' + $modelFile + '"'), '--host', '127.0.0.1', '--port', '8088', '--alias', 'qwen3-4b', '-c', '12288', '-np', '1', '-ngl', '99', '-t', '6', '--jinja', '--reasoning', 'off', '--no-webui', '--cors-origins', 'localhost')
        $engine = Start-Process -FilePath $runtimeExe -ArgumentList $arguments -WorkingDirectory $agentLensRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $agentLensRoot 'logs\engine.stdout.log') -RedirectStandardError (Join-Path $agentLensRoot 'logs\engine.stderr.log') -PassThru
        $engineStarted = ''
        try { $engineStarted = $engine.StartTime.ToString('o') } catch { }
        $owned.engine = @{ pid = $engine.Id; path = $runtimeExe; started = $engineStarted }
    }

    # 포트에 이미 떠 있는 것이 무엇인지 먼저 식별한다: 같은 프로젝트·같은 버전·같은 모드의 Agent Lens만 재사용한다.
    $wantedMode = 'live'
    if ($SamplesOnly) { $wantedMode = 'sample' }
    $appReady = $false
    $status = Get-AppStatus
    if ($null -ne $status) {
        $sameProject = Test-SameProject $status
        $sameVersion = (Get-Prop $status 'version' '') -eq $expectedVersion
        $sameMode = ((Get-Prop $status 'mode' '') -eq $wantedMode) -and ($SamplesOnly -or ((Get-Prop $status 'backend' '') -eq $Backend))
        $description = '프로젝트={0}, 버전={1}, 모드={2}/{3}, pid={4}, instance={5}' -f (Get-Prop $status 'project_root' '(0.5.0 이전: 보고 없음)'), (Get-Prop $status 'version' '?'), (Get-Prop $status 'mode' '?'), (Get-Prop $status 'backend' '?'), (Get-Prop $status 'pid' '?'), (Get-Prop $status 'instance' '?')
        # 0.5.0 이전 앱은 식별 정보를 보고하지 않으므로, 이 프로젝트의 기록(processes.json)과 일치하는 프로세스일 때만 우리 것으로 본다.
        $legacyApp = (Get-Prop $status 'project_root' '') -eq ''
        if ($sameProject -and $sameVersion -and $sameMode) {
            $appReady = $true
            Write-Host ('이미 실행 중인 Agent Lens를 사용합니다 ({0}).' -f $description)
        } elseif (($sameProject -or $legacyApp) -and (Test-OwnedProcess $owned.app)) {
            Write-Host ('이 프로젝트가 띄운 다른 설정의 앱을 종료하고 다시 시작합니다 ({0}).' -f $description)
            & (Join-Path $projectDir 'scripts\stop-owned-app.ps1') -AppProcessId (Get-Prop $owned.app 'pid' 0) -StartedAt (Get-Prop $owned.app 'started' '')
            $deadline = (Get-Date).AddSeconds(10)
            while ((Get-Date) -lt $deadline -and $null -ne (Get-AppStatus)) { Start-Sleep -Milliseconds 300 }
            if ($null -ne (Get-AppStatus)) { throw ('포트 {0}의 이전 앱이 종료되지 않았습니다. Stop-AgentLens.ps1을 실행한 뒤 다시 시도하세요.' -f $Port) }
        } else {
            throw ('포트 {0}에 다른 Agent Lens가 실행 중입니다 ({1}). 그 폴더에서 Stop-AgentLens.ps1을 실행하거나 -Port 로 다른 포트를 지정하세요. 이 스크립트는 다른 프로젝트나 기록에 없는 프로세스를 종료하지 않습니다.' -f $Port, $description)
        }
    } else {
        $listener = Get-PortListener
        if ($null -ne $listener) {
            throw ('포트 {0}을 다른 프로그램이 사용 중입니다: {1} (PID {2}). -Port 로 다른 포트를 지정하거나 해당 프로그램을 종료하세요.' -f $Port, $listener.name, $listener.pid)
        }
    }
    if (-not $appReady) {
        $pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
        $webArguments = @('-m', 'local_agent_lab.web', '--backend', $Backend, '--port', "$Port")
        if ($SamplesOnly) { $webArguments += '--samples-only' }
        $appProcess = Start-Process -FilePath $pythonExe -ArgumentList $webArguments -WorkingDirectory $projectDir -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
        # 준비 완료를 기다린다: 이 프로젝트의 앱이 /api/status에 응답할 때까지. 모델 로딩은 응답 뒤에도 계속될 수 있다.
        $deadline = (Get-Date).AddSeconds(90)
        $ready = $null
        while ((Get-Date) -lt $deadline) {
            if ($appProcess.HasExited) { break }
            $probe = Get-AppStatus
            if ($null -ne $probe -and (Test-SameProject $probe)) { $ready = $probe; break }
            Start-Sleep -Milliseconds 500
        }
        if ($null -eq $ready) {
            $tail = Get-LogTail
            if ($appProcess.HasExited) { throw ("앱이 시작 직후 종료됐습니다 (exit code {0}). 로그: {1}`n{2}" -f $appProcess.ExitCode, $stderrLog, $tail) }
            throw ("앱이 90초 안에 응답하지 않았습니다. 로그: {0}`n{1}" -f $stderrLog, $tail)
        }
        $appStarted = ''
        try { $appStarted = $appProcess.StartTime.ToString('o') } catch { }
        $owned.app = @{
            pid = $appProcess.Id; path = $pythonExe; started = $appStarted
            project = $projectDir; version = (Get-Prop $ready 'version' ''); instance = (Get-Prop $ready 'instance' ''); port = $Port
        }
        Write-Host ('Agent Lens {0} 시작 (pid {1}, instance {2}).' -f (Get-Prop $ready 'version' '?'), $appProcess.Id, (Get-Prop $ready 'instance' '?'))
    }
    [System.IO.File]::WriteAllText($pidFile, ($owned | ConvertTo-Json -Depth 4), [System.Text.UTF8Encoding]::new($false))
    Write-Host ('Agent Lens: {0}' -f $appUrl)
    if ($SamplesOnly) { Write-Host '실제 저장 기록을 재생합니다. 모델을 다운로드하거나 불러오지 않습니다.' }
    else { Write-Host '모델을 불러오는 중일 수 있습니다. 화면에서 상태를 확인하세요.' }
    if (-not $NoBrowser) { Start-Process $appUrl }
} finally { Pop-Location }
