<#
Assemble the portable Windows app for NMS Save Vault.

Output: dist\NMSSaveVault\ -- a self-contained folder that runs on a machine with
no Python installed. The two launchers in it are verbatim renamed copies of the
Authenticode-signed pythonw.exe / python.exe published by the Python Software
Foundation, so Windows sees a signed binary it already trusts instead of an
unsigned one-file bundle. Nothing is patched into them (that would void the
signature); the app is dispatched from _runtime\sitecustomize.py instead.

The runtime is downloaded from python.org and cached under build\runtime-cache:
    python-<ver>-embed-amd64.zip   the interpreter, its DLLs and the stdlib
    amd64/tcltk.msi                Tkinter, which the embeddable package omits

Usage:
    pwsh -ExecutionPolicy Bypass -File packaging\build_portable.ps1
Requirements: none beyond Windows + an internet connection on the first run.
#>
#Requires -Version 5
[CmdletBinding()]
param(
    # The interpreter shipped to users. Keep this on the version the project is
    # developed and tested against.
    [string] $PythonVersion = "3.12.10",
    [string] $OutDir
)
$ErrorActionPreference = "Stop"

$pkgDir = $PSScriptRoot
$root   = Split-Path -Parent $pkgDir
if (-not $OutDir) { $OutDir = Join-Path $root "dist\NMSSaveVault" }
$cache  = Join-Path $root "build\runtime-cache"
$work   = Join-Path $root "build\portable-work"

$parts = $PythonVersion.Split(".")
if ($parts.Count -lt 2) { throw "PythonVersion must look like 3.12.10, got '$PythonVersion'." }
$tag = "python$($parts[0])$($parts[1])"      # e.g. python312: the DLL, stdlib zip and ._pth stem

# Extension modules the app never touches; ~1.6 MB of test harness we would
# otherwise ship to every user.
$skipModules = @(
    "_ctypes_test.pyd", "_msi.pyd", "winsound.pyd",
    "_testbuffer.pyd", "_testcapi.pyd", "_testclinic.pyd", "_testconsole.pyd",
    "_testimportmultiple.pyd", "_testinternalcapi.pyd", "_testmultiphase.pyd",
    "_testsinglephase.pyd"
)

function Assert-PsfSigned {
    <#  A build that quietly produced an unsigned launcher would defeat the whole
        point of this packaging, so treat a bad signature as a build failure. #>
    param([string] $Path)
    $sig = Get-AuthenticodeSignature -LiteralPath $Path
    $name = Split-Path $Path -Leaf
    if ($sig.Status -ne "Valid") {
        throw "$name is not validly signed (status: $($sig.Status)). Refusing to package it."
    }
    if ($sig.SignerCertificate.Subject -notmatch "Python Software Foundation") {
        throw "$name is signed by an unexpected publisher: $($sig.SignerCertificate.Subject)"
    }
}

function Get-Cached {
    param([string] $Url, [string] $FileName)
    $path = Join-Path $cache $FileName
    if (Test-Path $path) {
        Write-Host "    cached: $FileName"
    } else {
        Write-Host "    downloading: $Url"
        Invoke-WebRequest -Uri $Url -OutFile $path -UseBasicParsing
    }
    return $path
}

# --- fetch -------------------------------------------------------------------
Write-Host "==> Runtime (Python $PythonVersion)"
New-Item -ItemType Directory -Force $cache | Out-Null
$base    = "https://www.python.org/ftp/python/$PythonVersion"
$embZip  = Get-Cached "$base/python-$PythonVersion-embed-amd64.zip" "python-$PythonVersion-embed-amd64.zip"
$tkMsi   = Get-Cached "$base/amd64/tcltk.msi" "tcltk-$PythonVersion.msi"

# --- unpack ------------------------------------------------------------------
Write-Host "==> Unpacking"
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
$emb = Join-Path $work "embed"
$tk  = Join-Path $work "tcltk"
New-Item -ItemType Directory -Force $emb, $tk | Out-Null
Expand-Archive -Path $embZip -DestinationPath $emb -Force

# /a is an administrative install: it only lays the files out, touching nothing
# on this machine and needing no elevation.
$msi = Start-Process msiexec -ArgumentList "/a", "`"$tkMsi`"", "/qn", "TARGETDIR=`"$tk`"" -Wait -PassThru -NoNewWindow
if ($msi.ExitCode -ne 0) { throw "msiexec failed to extract tcltk.msi (exit $($msi.ExitCode))." }

foreach ($f in @("python.exe", "pythonw.exe", "$tag.dll", "python3.dll")) { Assert-PsfSigned (Join-Path $emb $f) }
foreach ($f in @("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll")) { Assert-PsfSigned (Join-Path $tk "DLLs\$f") }

# --- assemble ----------------------------------------------------------------
Write-Host "==> Assembling $OutDir"
if (Test-Path $OutDir) { Remove-Item -Recurse -Force $OutDir }
$runtime = Join-Path $OutDir "_runtime"
New-Item -ItemType Directory -Force `
    $OutDir, $runtime, (Join-Path $runtime "DLLs"), (Join-Path $runtime "Lib"), (Join-Path $runtime "tcl"), (Join-Path $runtime "app") | Out-Null

# The launchers. Renaming a PE does not touch the bytes the signature covers, so
# both stay validly signed -- and pythonw.exe is what keeps the GUI console-free.
Copy-Item (Join-Path $emb "pythonw.exe") (Join-Path $OutDir "NMSSaveVault.exe")
Copy-Item (Join-Path $emb "python.exe")  (Join-Path $OutDir "nmsvault.exe")
foreach ($f in @("$tag.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll")) {
    Copy-Item (Join-Path $emb $f) (Join-Path $OutDir $f)
}
Copy-Item (Join-Path $emb "LICENSE.txt") (Join-Path $OutDir "PYTHON_LICENSE.txt")
Copy-Item (Join-Path $pkgDir "nmsvault.ico") (Join-Path $OutDir "nmsvault.ico")

# Stdlib and extension modules.
Copy-Item (Join-Path $emb "$tag.zip") $runtime
Get-ChildItem (Join-Path $emb "*") -Include *.pyd |
    Where-Object { $skipModules -notcontains $_.Name } |
    Copy-Item -Destination (Join-Path $runtime "DLLs")
foreach ($f in @("libcrypto-3.dll", "libssl-3.dll", "libffi-8.dll", "sqlite3.dll")) {
    Copy-Item (Join-Path $emb $f) (Join-Path $runtime "DLLs\$f")
}

# Tkinter. _tkinter.pyd loads tcl86t/tk86t from its own directory, and tcl86t
# in turn imports zlib1 -- all four have to land together in DLLs.
foreach ($f in @("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll")) {
    Copy-Item (Join-Path $tk "DLLs\$f") (Join-Path $runtime "DLLs\$f")
}
Copy-Item (Join-Path $tk "Lib\tkinter") (Join-Path $runtime "Lib") -Recurse
foreach ($d in @("tcl8", "tcl8.6", "tk8.6")) {
    Copy-Item (Join-Path $tk "tcl\$d") (Join-Path $runtime "tcl") -Recurse
}
Remove-Item -Recurse -Force (Join-Path $runtime "tcl\tk8.6\demos") -ErrorAction SilentlyContinue

# The app itself: pure Python with a standard-library-only runtime, so there is
# nothing to compile or vendor.
Copy-Item (Join-Path $root "src\nms_save_vault") (Join-Path $runtime "app") -Recurse
Get-ChildItem (Join-Path $runtime "app") -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Copy-Item (Join-Path $pkgDir "sitecustomize.py") (Join-Path $runtime "sitecustomize.py")

# The path file is keyed to the DLL name, not the executable's, which is what
# lets the launchers be renamed. "import site" is what runs sitecustomize.py.
@(
    "_runtime\$tag.zip"
    "_runtime\DLLs"
    "_runtime\Lib"
    "_runtime"
    "_runtime\app"
    "import site"
) | Set-Content -LiteralPath (Join-Path $OutDir "$tag._pth") -Encoding ASCII

# --- verify ------------------------------------------------------------------
Write-Host "==> Verifying the packaged launchers"
foreach ($f in @("NMSSaveVault.exe", "nmsvault.exe")) {
    Assert-PsfSigned (Join-Path $OutDir $f)
    Write-Host "    $f  signed by the Python Software Foundation"
}

Remove-Item -Recurse -Force $work
$files = Get-ChildItem -Recurse -File $OutDir
Write-Host ("==> Built {0} ({1:N1} MB, {2} files)" -f $OutDir, (($files | Measure-Object Length -Sum).Sum / 1MB), $files.Count) -ForegroundColor Green
