[CmdletBinding()]
param([string]$Destination = (Join-Path $PSScriptRoot '..\artifacts\vendor'))
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tag = 'autobuild-2026-10-22-13-06'
$name = 'ffmpeg-n8.1.3-23-g5c4f5744cd-win64-lgpl-shared-8.1.zip'
$expected = 'c41df73a3f3259e81d5d53e6ce0c4c80b4841a30894fb77954650e445daa6c53'
# BtbN prunes old autobuild releases, so the identical archive is mirrored as a
# ComfyBridge release asset. Both sources are verified against the same SHA-256.
$urls = @(
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/$tag/$name",
    "https://github.com/C10H14N2O5/ComfyBridge/releases/download/vendor-ffmpeg-n8.1.3/$name"
)
$null = New-Item -ItemType Directory -Force -Path $Destination
$root = (Resolve-Path -LiteralPath $Destination).Path
$archive = Join-Path $root $name
if (-not (Test-Path -LiteralPath $archive)) {
    $partial = "$archive.partial"
    foreach ($url in $urls) {
        try {
            Invoke-WebRequest -Uri $url -OutFile $partial
            Move-Item -LiteralPath $partial -Destination $archive
            break
        } catch {
            Write-Warning "Download failed from ${url}: $($_.Exception.Message)"
            Remove-Item -LiteralPath $partial -ErrorAction SilentlyContinue
        }
    }
    if (-not (Test-Path -LiteralPath $archive)) { throw 'Pinned FFmpeg archive is unavailable from all sources.' }
}
$actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw "Archive checksum mismatch. Inspect and remove invalid archive manually: $archive" }
$expanded = Join-Path $root 'ffmpeg-pinned'
if (-not (Test-Path -LiteralPath $expanded)) {
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded
}
$bin = Join-Path $expanded 'ffmpeg-n8.1.3-23-g5c4f5744cd-win64-lgpl-shared-8.1\bin'
if (-not (Test-Path -LiteralPath (Join-Path $bin 'ffmpeg.exe'))) { throw 'Pinned binary directory is incomplete.' }
& (Join-Path $bin 'ffmpeg.exe') -version
if ($LASTEXITCODE -ne 0) { throw 'Pinned FFmpeg cannot start.' }
& (Join-Path $bin 'ffmpeg.exe') -hide_banner -encoders 2>&1 | Select-String 'libmp3lame|pcm_s16le'
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect pinned FFmpeg encoders.' }
Write-Output "Verified FFmpeg bin: $bin"
