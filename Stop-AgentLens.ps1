Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot '.local\processes.json'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Host 'No Agent Lens process record.'; return }
$owned = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
foreach ($property in @('app', 'engine')) {
    if ($owned.PSObject.Properties.Name -notcontains $property) { continue }
    $entry = $owned.$property
    if ($null -eq $entry) { continue }
    $running = Get-Process -Id $entry.pid -ErrorAction Ignore
    if ($null -ne $running -and $running.Path -eq $entry.path) {
        if ($property -eq 'app') { & (Join-Path $PSScriptRoot 'scripts\stop-owned-app.ps1') -AppProcessId $running.Id }
        else { Stop-Process -Id $running.Id }
        Write-Host "Stopped owned Agent Lens process: $property"
    }
}
