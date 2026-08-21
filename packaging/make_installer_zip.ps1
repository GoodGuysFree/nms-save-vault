<#
Assemble the distributable installer zip.

Produces dist\NMSSaveVault-Setup.zip containing:
    NMSSaveVault\           the portable app (launchers + _runtime), self-contained
    README.txt              how to run it; install.bat is the optional path
    install.bat             copies it in + offers shortcuts
    uninstall.bat           removes the app, config, and shortcuts

The app folder is shipped whole rather than flattened into the zip root so that running
NMSSaveVault\NMSSaveVault.exe straight from the extracted zip -- the normal way to use
it -- works without picking the app out of a pile of loose files. install.bat is then
a single copy for anyone who wants shortcuts.

If the app folder is missing it is built first via build_portable.ps1.

Usage:
    pwsh -ExecutionPolicy Bypass -File packaging\make_installer_zip.ps1
#>
#Requires -Version 5
$ErrorActionPreference = "Stop"

$pkgDir    = $PSScriptRoot
$root      = Split-Path -Parent $pkgDir
$distDir   = Join-Path $root "dist"
$appDir    = Join-Path $distDir "NMSSaveVault"
$stageDir  = Join-Path $distDir "installer-stage"
$zipPath   = Join-Path $distDir "NMSSaveVault-Setup.zip"
$installer = Join-Path $root "installer"

if (-not (Test-Path (Join-Path $appDir "NMSSaveVault.exe"))) {
    Write-Host "==> Portable app not found; building it first"
    & (Join-Path $pkgDir "build_portable.ps1")
}

Write-Host "==> Staging installer files"
if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
New-Item -ItemType Directory -Path $stageDir | Out-Null
Copy-Item $appDir $stageDir -Recurse
foreach ($f in @("install.bat", "uninstall.bat", "README.txt")) {
    Copy-Item (Join-Path $installer $f) (Join-Path $stageDir $f)
}

# Running the app from dist\ leaves the builder's own config and byte-code behind;
# neither belongs in someone else's download.
$staged = Join-Path $stageDir "NMSSaveVault"
Remove-Item (Join-Path $staged "state.json") -Force -ErrorAction SilentlyContinue
Get-ChildItem $staged -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

Write-Host "==> Compressing -> $zipPath"
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $zipPath -CompressionLevel Optimal
Remove-Item -Recurse -Force $stageDir

$mb = "{0:N1}" -f ((Get-Item $zipPath).Length / 1MB)
Write-Host "==> Done: $zipPath ($mb MB)" -ForegroundColor Green
