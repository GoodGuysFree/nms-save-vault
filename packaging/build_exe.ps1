<#
Build a self-contained, one-file Windows GUI executable for NMS Save Vault.

Output: dist\NMSSaveVault.exe  (bundles Python + Tkinter; no install needed on the
target machine). Uses an isolated build venv so the dev .venv is left untouched.

Usage (from anywhere):
    pwsh -ExecutionPolicy Bypass -File packaging\build_exe.ps1
Requirements: uv on PATH (https://github.com/astral-sh/uv).
#>
#Requires -Version 5
$ErrorActionPreference = "Stop"

$pkgDir  = $PSScriptRoot
$root    = Split-Path -Parent $pkgDir
$venv    = Join-Path $root ".build-venv"
$py      = Join-Path $venv "Scripts\python.exe"
$distDir = Join-Path $root "dist"
$workDir = Join-Path $root "build\pyinstaller"
$specDir = Join-Path $root "build"
$icon    = Join-Path $pkgDir "nmsvault.ico"   # optional; used only if present

Write-Host "==> Creating isolated build venv: $venv"
if (-not (Test-Path $py)) { & uv venv $venv }

Write-Host "==> Installing PyInstaller + the package into the build venv"
& uv pip install --python $py --quiet "pyinstaller>=6" $root

$pyiArgs = @(
    "--noconfirm", "--clean",
    "--onefile", "--windowed",
    "--name", "NMSSaveVault",
    "--distpath", $distDir,
    "--workpath", $workDir,
    "--specpath", $specDir
)
if (Test-Path $icon) {
    $pyiArgs += @("--icon", $icon)
    $pyiArgs += @("--add-data", "$icon;.")   # bundle it so the window icon works too
}
$pyiArgs += (Join-Path $pkgDir "nmsvault_gui.py")

Write-Host "==> Running PyInstaller"
& (Join-Path $venv "Scripts\pyinstaller.exe") @pyiArgs

$exe = Join-Path $distDir "NMSSaveVault.exe"
if (Test-Path $exe) {
    $mb = "{0:N1}" -f ((Get-Item $exe).Length / 1MB)
    Write-Host "==> Built $exe ($mb MB)" -ForegroundColor Green
} else {
    throw "Build finished but $exe was not produced."
}
