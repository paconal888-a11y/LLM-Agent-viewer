param(
    [string]$OutputPath = (Join-Path $PSScriptRoot '..\reports\environment.json')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$osInfo = Get-CimInstance Win32_OperatingSystem
$cpuInfo = @(Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors)
$gpuInfo = @(Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion)
$projectRoot = Split-Path $PSScriptRoot -Parent
$driveId = [System.IO.Path]::GetPathRoot($projectRoot).TrimEnd('\')
$diskInfo = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$driveId'"
$installedTools = foreach ($toolName in @('git', 'uv', 'codex', 'ollama', 'lms')) {
    $toolCommand = Get-Command $toolName -ErrorAction Ignore
    if ($null -ne $toolCommand) {
        [ordered]@{ name = $toolName; path = $toolCommand.Source }
    }
}
$knownInstalls = foreach ($candidate in @(
    "$env:LOCALAPPDATA\Programs\LM Studio\LM Studio.exe",
    "$env:LOCALAPPDATA\LM-Studio\LM Studio.exe",
    "$env:USERPROFILE\.lmstudio\bin\lms.exe",
    "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
)) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidate }
}
$report = [ordered]@{
    captured_at = (Get-Date).ToString('o')
    os = $osInfo.Caption
    os_version = $osInfo.Version
    ram_gib = [math]::Round($osInfo.TotalVisibleMemorySize / 1MB, 1)
    free_ram_gib = [math]::Round($osInfo.FreePhysicalMemory / 1MB, 1)
    cpu = $cpuInfo
    gpu = $gpuInfo
    disk_free_gib = [math]::Round($diskInfo.FreeSpace / 1GB, 1)
    installed_tools = @($installedTools)
    inference_apps_at_known_paths = @($knownInstalls)
    notes = @(
        'GPU name strings do not prove dedicated VRAM capacity or actual GPU offload.',
        'Intel Arc 140V uses shared system memory; benchmark available memory and GPU use separately.',
        'Application detection checks PATH and known paths, not every possible installation.'
    )
}
$destination = [System.IO.Path]::GetFullPath($OutputPath)
[System.IO.Directory]::CreateDirectory((Split-Path $destination -Parent)) | Out-Null
$json = $report | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText($destination, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
Write-Output $json
