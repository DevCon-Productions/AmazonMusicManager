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
# Result: installer_output\AmazonMusicManager-Setup-1.0.exe

$ErrorActionPreference = "Stop"

Write-Host "[1/2] Building the app with PyInstaller..." -ForegroundColor Cyan
pyinstaller --noconfirm AmazonMusicManager.spec
if (-not (Test-Path "dist\AmazonMusicManager\AmazonMusicManager.exe")) {
    throw "PyInstaller did not produce dist\AmazonMusicManager\AmazonMusicManager.exe"
}

Write-Host "[2/2] Compiling the installer with Inno Setup..." -ForegroundColor Cyan
$iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    # Fall back to ISCC on PATH.
    if (Get-Command ISCC -ErrorAction SilentlyContinue) { $iscc = "ISCC" }
    else { throw "Inno Setup (ISCC.exe) not found. Install Inno Setup 6 from https://jrsoftware.org/isdl.php" }
}
& $iscc installer.iss

Write-Host "Done. Installer is in .\installer_output\" -ForegroundColor Green
