[CmdletBinding()]
param([switch]$SkipTests, [string]$ReleaseName = ('ComfyBridge-0.4.1-windows-x64-' + (Get-Date -Format 'yyyyMMdd-HHmmss')), [string]$LegacyRelease = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($ReleaseName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]+$') { throw 'ReleaseName must be a simple directory name.' }
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run uv sync --frozen --python 3.13 first.' }
if (-not $SkipTests) {
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Regression tests failed; release aborted.' }
}
& (Join-Path $PSScriptRoot 'fetch_ffmpeg.ps1')
& (Join-Path $PSScriptRoot 'fetch_sources.ps1')
$stamp = [guid]::NewGuid().ToString('N')
$build = Join-Path $root "build\$stamp"
$distRoot = Join-Path $root 'dist'
$destination = Join-Path $distRoot $ReleaseName
if (Test-Path -LiteralPath $destination) { throw 'Release already exists; choose a new name rather than overwriting.' }
$null = New-Item -ItemType Directory -Force -Path $build,$distRoot
& $python -m PyInstaller --noconfirm --onedir --console --name ComfyBridge --paths (Join-Path $root 'src') --distpath $build --workpath (Join-Path $build 'work') --specpath $build --collect-submodules uvicorn --collect-submodules rich --collect-data certifi (Join-Path $root 'src\comfybridge\__main__.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
# Copy only the generated package. No arbitrary deletion or move of an existing release.
Copy-Item -LiteralPath (Join-Path $build 'ComfyBridge') -Destination $destination -Recurse
$bin = Join-Path $destination 'bin'
$licenses = Join-Path $destination 'licenses'
$sources = Join-Path $destination 'third-party-sources'
$null = New-Item -ItemType Directory -Force -Path $bin,$licenses,$sources
$vendor = Join-Path $root 'artifacts\vendor\ffmpeg-pinned\ffmpeg-n8.1.3-23-g5c4f5744cd-win64-lgpl-shared-8.1'
Copy-Item -LiteralPath (Join-Path $vendor 'bin\ffmpeg.exe'),(Join-Path $vendor 'bin\ffprobe.exe') -Destination $bin
Get-ChildItem -LiteralPath (Join-Path $vendor 'bin') -Filter '*.dll' | Copy-Item -Destination $bin
Copy-Item -LiteralPath (Join-Path $vendor 'LICENSE.txt') -Destination (Join-Path $licenses 'FFMPEG-LGPL-3.0.txt')
Copy-Item -LiteralPath (Join-Path $root 'artifacts\vendor\sources\GPL-3.0.txt') -Destination $licenses
Get-ChildItem -LiteralPath (Join-Path $root 'artifacts\vendor\sources') -Filter '*.tar.gz' | Copy-Item -Destination $sources
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'ffmpeg-manifest.json') -Destination $licenses
& $python (Join-Path $PSScriptRoot 'collect_licenses.py') (Join-Path $licenses 'python')
if ($LASTEXITCODE -ne 0) { throw 'License collection failed.' }
foreach ($name in 'README.md','ARCHITECTURE.md','CHANGELOG.md','todo.md','pyproject.toml','uv.lock','.python-version','.gitignore') {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $destination
}
Copy-Item -LiteralPath (Join-Path $root 'examples'),(Join-Path $root 'docs') -Destination $destination -Recurse
foreach ($sourceName in 'src','tests','scripts') {
    $sourceRoot = Join-Path $root $sourceName
    foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -File -Recurse | Where-Object { $_.Extension -in '.py','.ps1','.json','.md' -and $_.FullName -notmatch '[\\/]__pycache__[\\/]' }) {
        $relative = [IO.Path]::GetRelativePath($root, $file.FullName)
        $target = Join-Path $destination $relative
        $null = New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target)
        Copy-Item -LiteralPath $file.FullName -Destination $target
    }
}
$buildInfo = @{
    product='ComfyBridge'; version='0.4.1'; built_at=(Get-Date -Format 'o');
    uv_lock_sha256=(Get-FileHash -LiteralPath (Join-Path $root 'uv.lock') -Algorithm SHA256).Hash.ToLowerInvariant();
    ffmpeg_archive_sha256='c41df73a3f3259e81d5d53e6ce0c4c80b4841a30894fb77954650e445daa6c53';
    notes='Unsigned offline installer payload. No production API key, user data, reference audio or model weights included.'
}
$buildInfo | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $destination 'BUILD-INFO.json') -Encoding utf8
& (Join-Path $destination 'ComfyBridge.exe') --help
if ($LASTEXITCODE -ne 0) { throw 'Packaged CLI smoke failed.' }
& (Join-Path $bin 'ffmpeg.exe') -version
if ($LASTEXITCODE -ne 0) { throw 'Packaged audio dependency smoke failed.' }
# Lifecycle leases intentionally persist beside an installed executable. Exercise a
# private copy so runtime coordination state never enters the release payload.
$smokePackage = Join-Path $build 'smoke-package'
Copy-Item -LiteralPath $destination -Destination $smokePackage -Recurse
& $python (Join-Path $PSScriptRoot 'packaged_smoke.py') $smokePackage
if ($LASTEXITCODE -ne 0) { throw 'Packaged TCP/SDK/audio/lifecycle smoke failed; no installer will be produced.' }
Copy-Item -LiteralPath (Join-Path $root 'artifacts\packaged-smoke.json') -Destination (Join-Path $destination 'docs\PACKAGED-SMOKE.json')
$hashes = foreach ($file in Get-ChildItem -LiteralPath $destination -File -Recurse) {
    $relative = [IO.Path]::GetRelativePath($destination, $file.FullName).Replace('\','/')
    '{0}  {1}' -f (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $relative
}
$hashes | Set-Content -LiteralPath (Join-Path $destination 'SHA256SUMS.txt') -Encoding utf8
Write-Output "RELEASE_DIRECTORY=$destination"
$setupArgs = @((Join-Path $PSScriptRoot 'build_setup.py'), $destination)
if ($LegacyRelease) { $setupArgs += @('--legacy-release', (Resolve-Path -LiteralPath $LegacyRelease).Path) }
& $python @setupArgs
if ($LASTEXITCODE -ne 0) { throw 'Offline Setup build or installer smoke failed; no final installer will be published.' }
