param(
    [switch]$NoBrowser,
    [switch]$SamplesOnly,
    [ValidateSet('reference','llama')][string]$Backend='reference',
    [ValidateRange(1024, 65535)][int]$Port=8765
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
function Find-PackagedDataHome([string[]]$RequiredFiles) {
    # MSIX로 설치된 앱(Codex 등) 안에서 이 런처를 실행하면 %LOCALAPPDATA% 쓰기가 그 앱의 저장소
    # (%LOCALAPPDATA%\Packages\<앱>\LocalCache\Local)로 가상화되어 일반 콘솔에서는 파일이 없는 것처럼 보인다.
    # 이미 받은 런타임·모델을 다시 내려받지 않도록 그 위치를 찾아 그대로 쓴다. 복사·이동·삭제는 하지 않는다.
    $packages = Join-Path $env:LOCALAPPDATA 'Packages'
    if (-not (Test-Path -LiteralPath $packages)) { return $null }
    foreach ($package in Get-ChildItem -LiteralPath $packages -Directory -ErrorAction SilentlyContinue) {
        $candidate = Join-Path $package.FullName 'LocalCache\Local\AgentLens'
        $complete = $true
        foreach ($file in $RequiredFiles) {
            if (-not (Test-Path -LiteralPath (Join-Path $candidate $file))) { $complete = $false; break }
        }
        if ($complete) { return $candidate }
    }
    return $null
}
$agentLensRoot = Join-Path $env:LOCALAPPDATA 'AgentLens'
if ($env:AGENT_LENS_HOME) { $agentLensRoot = $env:AGENT_LENS_HOME }
$requiredFiles = @()
if (-not $SamplesOnly) {
    if ($Backend -eq 'llama') { $requiredFiles = @('runtime\b12110-vulkan\llama-server.exe', 'models\Qwen3-4B-Q4_K_M.gguf') }
    else { $requiredFiles = @('models\Qwen3-0.6B\model.safetensors') }
}
if (@($requiredFiles | Where-Object { -not (Test-Path -LiteralPath (Join-Path $agentLensRoot $_)) }).Count -gt 0) {
    $packagedHome = Find-PackagedDataHome $requiredFiles
    if ($packagedHome) {
        $agentLensRoot = $packagedHome
        $env:AGENT_LENS_HOME = $packagedHome
        Write-Host ('앱 패키지 저장소에 있는 기존 런타임·모델을 그대로 사용합니다: {0}' -f $packagedHome)
    }
}
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
function Invoke-Native([string]$FilePath, [string[]]$Arguments) {
    # Windows PowerShell 5.1은 호출자가 이 스크립트에 2> 또는 2>&1을 붙이면 네이티브 명령의 stderr 줄을 오류 레코드로 바꾼다.
    # 'Stop'에서는 uv의 정상 진행 메시지("Resolved 53 packages")만으로 스크립트가 끝나므로 실행 구간만 Continue로 두고,
    # 성공 여부는 호출 뒤의 $LASTEXITCODE로 판정한다. 없는 명령은 지금처럼 바로 오류로 끝낸다.
    Get-Command $FilePath -ErrorAction Stop | Out-Null
    $ErrorActionPreference = 'Continue'
    & $FilePath @Arguments
}

if (-not $SamplesOnly -and $Backend -eq 'llama' -and (-not (Test-Path -LiteralPath $runtimeExe) -or -not (Test-Path -LiteralPath $modelFile))) {
    & (Join-Path $projectDir 'scripts\setup-runtime.ps1')
}
Push-Location -LiteralPath $projectDir
try {
    if ($SamplesOnly) { Invoke-Native 'uv' @('sync', '--python', '3.12', '--inexact', '--locked', '--system-certs') }
    elseif ($Backend -eq 'reference') { Invoke-Native 'uv' @('sync', '--python', '3.12', '--extra', 'reference', '--extra', 'integrations', '--locked', '--system-certs') }
    else { Invoke-Native 'uv' @('sync', '--python', '3.12', '--extra', 'integrations', '--inexact', '--locked', '--system-certs') }
    if ($LASTEXITCODE -ne 0) { throw 'Python environment setup failed.' }
    if (-not $SamplesOnly -and $Backend -eq 'reference') {
        Invoke-Native (Join-Path $projectDir '.venv\Scripts\python.exe') @((Join-Path $projectDir 'scripts\setup_reference.py'))
        if ($LASTEXITCODE -ne 0) { throw 'Reference model setup failed.' }
    }
    $expectedVersion = Get-ExpectedVersion
    $owned = @{ engine = $null; app = $null }
    $pidFile = Join-Path $localDir 'processes.json'
    if (Test-Path -LiteralPath $pidFile) {
        try {
            # BOM 없는 UTF-8 기록을 Windows PowerShell 5.1에서도 올바르게 읽는다(Get-Content는 ANSI로 읽어 한글 경로가 깨진다).
            $previous = [System.IO.File]::ReadAllText($pidFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
            $owned.engine = Get-Prop $previous 'engine'
            $owned.app = Get-Prop $previous 'app'
        } catch { }
    }
    $engineReady = $false
    try { $engineReady = (Invoke-RestMethod -Uri 'http://127.0.0.1:8088/health' -TimeoutSec 2).status -eq 'ok' } catch { }
    if (-not $SamplesOnly -and $Backend -eq 'llama' -and -not $engineReady -and (Test-OwnedProcess $owned.engine)) {
        # 이 프로젝트가 띄운 엔진이 아직 모델을 읽는 중(/health 503)이면 두 번째 엔진을 띄우지 않고 기다린다.
        Write-Host '이 프로젝트가 시작한 llama.cpp 엔진이 준비될 때까지 기다립니다 (최대 120초).'
        $engineDeadline = (Get-Date).AddSeconds(120)
        while ((Get-Date) -lt $engineDeadline -and -not $engineReady) {
            Start-Sleep -Seconds 2
            try { $engineReady = (Invoke-RestMethod -Uri 'http://127.0.0.1:8088/health' -TimeoutSec 2).status -eq 'ok' } catch { }
            if (-not (Test-OwnedProcess $owned.engine)) { break }
        }
        if (-not $engineReady) { throw ('기록된 엔진(pid {0})이 120초 안에 준비되지 않았습니다. 로그: {1}' -f (Get-Prop $owned.engine 'pid' '?'), (Join-Path $agentLensRoot 'logs\engine.stderr.log')) }
    }
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
