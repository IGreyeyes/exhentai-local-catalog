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
& $desktopPython -X utf8 verify_release.py
if ($LASTEXITCODE -ne 0) { throw 'Source privacy check failed. No release was built.' }
& $desktopPython -m PyInstaller --noconfirm --clean ExCatalog.spec
if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed.' }
foreach ($documentationName in @('README.md', 'DESKTOP.md', 'DEVELOPMENT.md', 'THIRD_PARTY_NOTICES.md', 'RELEASE_NOTES.md')) {
    Copy-Item -LiteralPath $documentationName -Destination $releaseDirectory
}
& $desktopPython -X utf8 package_desktop.py
if ($LASTEXITCODE -ne 0) { throw 'Could not create the public desktop archive.' }
$releasePackage = Join-Path $PSScriptRoot 'dist\ExCatalog-Windows-x64.zip'
& $desktopPython -X utf8 verify_release.py --package $releasePackage
if ($LASTEXITCODE -ne 0) { throw 'Packaged client privacy check failed. Do not upload this package.' }
$releaseHash = (Get-FileHash -LiteralPath $releasePackage -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $PSScriptRoot 'dist\SHA256SUMS.txt') -Value "$releaseHash  ExCatalog-Windows-x64.zip" -Encoding ascii
Copy-Item -LiteralPath 'RELEASE_NOTES.md' -Destination (Join-Path $PSScriptRoot 'dist\RELEASE_NOTES.md')
Write-Host "Desktop build complete: $releaseDirectory\ExCatalog.exe"
