param([int]$AppProcessId)
$appInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$AppProcessId"
if (-not $appInfo -or $appInfo.CommandLine -notlike '*local_agent_lab.web*') { return }
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
