param([int]$AppProcessId, [string]$StartedAt = '')
$appInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$AppProcessId"
if (-not $appInfo -or $appInfo.CommandLine -notlike '*local_agent_lab.web*') { return }
if ($StartedAt -ne '') {
    # 기록된 시작 시각과 다르면 같은 PID를 받은 다른 프로세스다. 종료하지 않는다.
    try {
        if ([math]::Abs(($appInfo.CreationDate - [datetime]$StartedAt).TotalSeconds) -gt 3) { return }
    } catch { return }
}
function Stop-OwnedTree($processInfo) {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($processInfo.ProcessId)"
    foreach ($child in $children) {
        if ($child.CreationDate -ge $processInfo.CreationDate) { Stop-OwnedTree $child }
    }
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($processInfo.ProcessId)"
    if ($current -and $current.CreationDate -eq $processInfo.CreationDate) {
        Stop-Process -Id $processInfo.ProcessId -ErrorAction SilentlyContinue
    }
}
Stop-OwnedTree $appInfo
