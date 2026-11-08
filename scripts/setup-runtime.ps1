param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$agentLensRoot = Join-Path $env:LOCALAPPDATA 'AgentLens'
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
    & curl.exe --location --fail --retry 2 --output $partialPath $Url
    if ($LASTEXITCODE -ne 0) { throw 'Download failed.' }
    $actualHash = (Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $SHA256) { throw "SHA256 mismatch: $partialPath" }
    Move-Item -LiteralPath $partialPath -Destination $Destination -Force
}

$archive = Join-Path $downloadDir 'llama-b12110-win-vulkan-x64.zip'
Get-VerifiedFile `
    'https://github.com/ggml-org/llama.cpp/releases/download/b12110/llama-b12110-bin-win-vulkan-x64.zip' `
    $archive `
    '9732a5e48887ca54d0bfe4af4324dc85fd56cd8cda19b52df3542fa6ebe6874f'
if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir 'llama-server.exe'))) {
    Expand-Archive -LiteralPath $archive -DestinationPath $runtimeDir -Force
}
Get-VerifiedFile `
    'https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf?download=true' `
    (Join-Path $modelDir 'Qwen3-4B-Q4_K_M.gguf') `
    '7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5'
Write-Host 'Agent Lens runtime is ready. Model weights: Qwen3-4B Q4_K_M, approximately 2.5 GB.'
