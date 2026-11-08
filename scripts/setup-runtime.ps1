param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$agentLensRoot = Join-Path $env:LOCALAPPDATA 'AgentLens'
if ($env:AGENT_LENS_HOME) { $agentLensRoot = $env:AGENT_LENS_HOME }
$downloadDir = Join-Path $agentLensRoot 'downloads'
$modelDir = Join-Path $agentLensRoot 'models'
$runtimeDir = Join-Path $agentLensRoot 'runtime\b12110-vulkan'
[System.IO.Directory]::CreateDirectory($downloadDir) | Out-Null
[System.IO.Directory]::CreateDirectory($modelDir) | Out-Null

function Get-VerifiedFile([string]$Url, [string]$Destination, [string]$SHA256) {
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        $cachedHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($cachedHash -eq $SHA256) { Write-Host "Verified cached file: $Destination"; return }
    }
    Get-Command curl.exe -ErrorAction Stop | Out-Null
    $partialPath = $Destination + '.part'
    Write-Host "Downloading: $Url"
    # Windows PowerShell 5.1 turns curl's progress on stderr into error records when the caller redirects
    # stream 2 (2> or 2>&1). Under 'Stop' that would abort the download, so success is judged by the exit code.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $resuming = Test-Path -LiteralPath $partialPath
        $options = @('--location', '--fail', '--retry', '2', '--output', $partialPath)
        # 끊긴 내려받기는 이어서 받는다. 이어 받기가 실패하면 처음부터 다시 받는다.
        if ($resuming) { & curl.exe @options --continue-at - $Url } else { & curl.exe @options $Url }
        if ($resuming -and $LASTEXITCODE -ne 0) {
            Remove-Item -LiteralPath $partialPath -Force
            & curl.exe @options $Url
        }
    } finally { $ErrorActionPreference = $previousPreference }
    if ($LASTEXITCODE -ne 0) { throw 'Download failed.' }
    $actualHash = (Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $SHA256) {
        Remove-Item -LiteralPath $partialPath -Force
        throw "SHA256 mismatch: $partialPath"
    }
    Move-Item -LiteralPath $partialPath -Destination $Destination -Force
}

$archive = Join-Path $downloadDir 'llama-b12110-win-vulkan-x64.zip'
Get-VerifiedFile `
    'https://github.com/ggml-org/llama.cpp/releases/download/b12110/llama-b12110-bin-win-vulkan-x64.zip' `
    $archive `
    '9732a5e48887ca54d0bfe4af4324dc85fd56cd8cda19b52df3542fa6ebe6874f'
if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir 'llama-server.exe'))) {
    Expand-Archive -LiteralPath $archive -DestinationPath $runtimeDir -Force
    if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir 'llama-server.exe'))) { throw "llama-server.exe not found after extracting: $archive" }
}
Get-VerifiedFile `
    'https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/bc640142c66e1fdd12af0bd68f40445458f3869b/Qwen3-4B-Q4_K_M.gguf?download=true' `
    (Join-Path $modelDir 'Qwen3-4B-Q4_K_M.gguf') `
    '7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5'
Write-Host 'Agent Lens runtime is ready. Model weights: Qwen3-4B Q4_K_M, approximately 2.5 GB.'
