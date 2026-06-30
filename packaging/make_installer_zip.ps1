<#
Assemble the distributable installer zip.

Produces dist\NMSSaveVault-Setup.zip containing:
    NMSSaveVault.exe        (the self-contained app)
    install.bat             (copies it in + offers shortcuts)
    README-INSTALL.txt

If the exe is missing it builds it first via build_exe.ps1.

Usage:
    pwsh -ExecutionPolicy Bypass -File packaging\make_installer_zip.ps1
#>
#Requires -Version 5
$ErrorActionPreference = "Stop"

$pkgDir    = $PSScriptRoot
$root      = Split-Path -Parent $pkgDir
$distDir   = Join-Path $root "dist"
$exe       = Join-Path $distDir "NMSSaveVault.exe"
$stageDir  = Join-Path $distDir "installer-stage"
$zipPath   = Join-Path $distDir "NMSSaveVault-Setup.zip"
$installer = Join-Path $root "installer"

if (-not (Test-Path $exe)) {
    Write-Host "==> NMSSaveVault.exe not found; building it first"
    & (Join-Path $pkgDir "build_exe.ps1")
}

Write-Host "==> Staging installer files"
if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
New-Item -ItemType Directory -Path $stageDir | Out-Null
Copy-Item $exe                                   (Join-Path $stageDir "NMSSaveVault.exe")
Copy-Item (Join-Path $installer "install.bat")   (Join-Path $stageDir "install.bat")
Copy-Item (Join-Path $installer "README-INSTALL.txt") (Join-Path $stageDir "README-INSTALL.txt")

Write-Host "==> Compressing -> $zipPath"
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $zipPath -CompressionLevel Optimal
Remove-Item -Recurse -Force $stageDir

$mb = "{0:N1}" -f ((Get-Item $zipPath).Length / 1MB)
Write-Host "==> Done: $zipPath ($mb MB)" -ForegroundColor Green
