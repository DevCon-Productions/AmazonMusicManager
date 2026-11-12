# build_installer.ps1 — build the Amazon Music Manager Windows installer end-to-end.
#
# Prerequisites (one-time):
#   pip install -r requirements.txt
#   pip install pyinstaller
#   Install Inno Setup 6:  https://jrsoftware.org/isdl.php
#
# Run from the project root:
#   powershell -ExecutionPolicy Bypass -File build_installer.ps1
#
# Result: installer_output\AmazonMusicManager-Setup-<version>.exe
# The version comes from version.py (APP_VERSION).

$ErrorActionPreference = "Stop"

$verLine = Select-String -Path "version.py" -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $verLine) { throw "Could not read APP_VERSION from version.py" }
$version = $verLine.Matches[0].Groups[1].Value
Write-Host "Building version $version" -ForegroundColor Cyan

Write-Host "[1/2] Building the app with PyInstaller..." -ForegroundColor Cyan
pyinstaller --noconfirm AmazonMusicManager.spec
if (-not (Test-Path "dist\AmazonMusicManager\AmazonMusicManager.exe")) {
    throw "PyInstaller did not produce dist\AmazonMusicManager\AmazonMusicManager.exe"
}

Write-Host "[2/2] Compiling the installer with Inno Setup..." -ForegroundColor Cyan
# ISCC.exe can live in Program Files (machine install) or LOCALAPPDATA
# (winget's per-user install), so check both, then fall back to PATH.
$isccCandidates = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    if (Get-Command ISCC -ErrorAction SilentlyContinue) { $iscc = "ISCC" }
    else { throw "Inno Setup (ISCC.exe) not found. Install Inno Setup 6 from https://jrsoftware.org/isdl.php (or: winget install JRSoftware.InnoSetup)" }
}
& $iscc "/DAppVersion=$version" installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit $LASTEXITCODE)" }

Write-Host "Done. Installer is in .\installer_output\" -ForegroundColor Green
