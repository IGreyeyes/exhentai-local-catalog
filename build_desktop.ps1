$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$desktopPython = Join-Path $PSScriptRoot '.venv-desktop\Scripts\python.exe'
$releaseDirectory = Join-Path $PSScriptRoot 'dist\ExCatalog'
foreach ($protectedName in @('data', 'logs', 'backups', 'e-hentai.db.zstd')) {
    if (Test-Path -LiteralPath (Join-Path $releaseDirectory $protectedName)) {
        throw "Release directory contains user data: $protectedName. Move it to a safe location before rebuilding."
    }
}
if (-not (Test-Path -LiteralPath $desktopPython)) {
    & py -3.14 -m venv .venv-desktop
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python 3.14 build environment.' }
}
& $desktopPython -m pip install -r requirements-desktop.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install desktop dependencies.' }
& $desktopPython -m PyInstaller --noconfirm --clean ExCatalog.spec
if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed.' }
foreach ($documentationName in @('README.md', 'DESKTOP.md', 'DEVELOPMENT.md', 'THIRD_PARTY_NOTICES.md')) {
    Copy-Item -LiteralPath $documentationName -Destination $releaseDirectory
}
Compress-Archive -LiteralPath $releaseDirectory -DestinationPath (Join-Path $PSScriptRoot 'dist\ExCatalog-Windows-x64.zip') -Force
Write-Host "Desktop build complete: $releaseDirectory\ExCatalog.exe"
