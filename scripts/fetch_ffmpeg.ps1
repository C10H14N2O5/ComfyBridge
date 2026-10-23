[CmdletBinding()]
param([string]$Destination = (Join-Path $PSScriptRoot '..\artifacts\vendor'))
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tag = 'autobuild-2026-10-22-13-06'
$name = 'ffmpeg-n8.1.3-23-g5c4f5744cd-win64-lgpl-shared-8.1.zip'
$expected = 'c41df73a3f3259e81d5d53e6ce0c4c80b4841a30894fb77954650e445daa6c53'
$url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/$tag/$name"
$null = New-Item -ItemType Directory -Force -Path $Destination
$root = (Resolve-Path -LiteralPath $Destination).Path
$archive = Join-Path $root $name
if (-not (Test-Path -LiteralPath $archive)) {
    Invoke-WebRequest -Uri $url -OutFile $archive
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
