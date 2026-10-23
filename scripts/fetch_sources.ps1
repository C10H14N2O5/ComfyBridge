[CmdletBinding()]
param([string]$Destination = (Join-Path $PSScriptRoot '..\artifacts\vendor\sources'))
$ErrorActionPreference = 'Stop'
$null = New-Item -ItemType Directory -Force -Path $Destination
$root = (Resolve-Path -LiteralPath $Destination).Path
$downloads = @{
    'ffmpeg-5c4f5744cd60943125a78db692b728210102f255.tar.gz' = 'https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/5c4f5744cd60943125a78db692b728210102f255';
    'ffmpeg-builds-937868360a21d711ec94a33f5591d345ad9b7754.tar.gz' = 'https://codeload.github.com/BtbN/FFmpeg-Builds/tar.gz/937868360a21d711ec94a33f5591d345ad9b7754';
    'GPL-3.0.txt' = 'https://raw.githubusercontent.com/FFmpeg/FFmpeg/5c4f5744cd60943125a78db692b728210102f255/COPYING.GPLv3'
}
$expected = @{
    'ffmpeg-5c4f5744cd60943125a78db692b728210102f255.tar.gz' = 'a34b707c67140a37ddd11c5475fa09135301a7c667f1d78c5702d544bba6ce77';
    'ffmpeg-builds-937868360a21d711ec94a33f5591d345ad9b7754.tar.gz' = '9ee4b55c6bf1ea2f9ee50a63bac8887532117ce9f7e7b0e2842a533b2d5f046a';
    'GPL-3.0.txt' = '8ceb4b9ee5adeddE47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903'.ToLowerInvariant()
}
foreach ($name in $downloads.Keys) {
    $target = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $target)) { Invoke-WebRequest -Uri $downloads[$name] -OutFile $target }
    $hash = Get-FileHash -LiteralPath $target -Algorithm SHA256
    if ($hash.Hash.ToLowerInvariant() -ne $expected[$name]) { throw "Source checksum mismatch: $name" }
    $hash | Select-Object Hash,Path
}
