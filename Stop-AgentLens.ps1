Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot '.local\processes.json'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Host 'No Agent Lens process record.'; return }
# processes.json은 BOM 없는 UTF-8로 기록된다. Windows PowerShell 5.1의 Get-Content는 BOM이 없으면 ANSI로 읽어
# 한글 경로가 깨지고, 그러면 실행 파일 경로 대조가 실패해 우리 프로세스를 'PID 재사용'으로 잘못 판정한다.
$owned = [System.IO.File]::ReadAllText($pidFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
function Get-Prop($object, [string]$name, $default = $null) {
    if ($null -eq $object) { return $default }
    if ($object.PSObject.Properties.Name -contains $name) { return $object.$name }
    return $default
}
foreach ($property in @('app', 'engine')) {
    $entry = Get-Prop $owned $property
    if ($null -eq $entry) { continue }
    $recordedPid = Get-Prop $entry 'pid' 0
    $running = Get-Process -Id $recordedPid -ErrorAction Ignore
    if ($null -eq $running) { Write-Host ("{0}: 기록된 PID {1}가 실행 중이 아닙니다." -f $property, $recordedPid); continue }
    if ($running.Path -ne (Get-Prop $entry 'path' '')) {
        Write-Host ("{0}: PID {1}의 실행 파일이 기록과 다릅니다. PID가 재사용된 것으로 보고 종료하지 않습니다." -f $property, $recordedPid)
        continue
    }
    $recordedStart = Get-Prop $entry 'started' ''
    if ($recordedStart -ne '') {
        $drift = 999
        try { $drift = [math]::Abs(($running.StartTime - [datetime]$recordedStart).TotalSeconds) } catch { }
        if ($drift -gt 3) {
            Write-Host ("{0}: PID {1}의 시작 시각이 기록과 다릅니다. PID가 재사용된 것으로 보고 종료하지 않습니다." -f $property, $recordedPid)
            continue
        }
    }
    if ($property -eq 'app') { & (Join-Path $PSScriptRoot 'scripts\stop-owned-app.ps1') -AppProcessId $running.Id -StartedAt $recordedStart }
    else { Stop-Process -Id $running.Id }
    Start-Sleep -Milliseconds 500
    if (Get-Process -Id $running.Id -ErrorAction Ignore) { Write-Host ("{0}: PID {1}가 아직 종료되지 않았습니다." -f $property, $running.Id) }
    else { Write-Host ("Stopped owned Agent Lens process: {0} (pid {1})" -f $property, $running.Id) }
}
