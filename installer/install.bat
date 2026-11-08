@echo off
setlocal EnableExtensions
title NMS Save Vault - Installer

rem ---------------------------------------------------------------------------
rem  NMS Save Vault installer.
rem  Copies the NMSSaveVault folder next to this script into
rem  %LOCALAPPDATA%\Programs\NMSSaveVault, then offers a Desktop shortcut and/or
rem  a Start Menu entry. If you decline both, it leaves a vault.bat launcher in
rem  the install folder, opens that folder, and tells you to run vault.bat.
rem
rem  The app is a folder, not a single file: the launcher is a renamed copy of
rem  the signed Python runtime and only works beside its _runtime folder.
rem ---------------------------------------------------------------------------

set "SRC=%~dp0"
set "EXE=NMSSaveVault.exe"
set "APPSRC=%~dp0NMSSaveVault"
set "INSTALL_DIR=%LOCALAPPDATA%\Programs\NMSSaveVault"
set "TARGET=%INSTALL_DIR%\%EXE%"
set "ICON=%INSTALL_DIR%\nmsvault.ico"
set "LNK_NAME=NMS Save Vault.lnk"

echo(
echo  ============================================
echo    NMS Save Vault - Installer
echo  ============================================
echo(

if not exist "%APPSRC%\%EXE%" (
    echo  ERROR: the NMSSaveVault folder was not found next to this installer.
    echo  Please extract the WHOLE zip first, then run install.bat again.
    echo(
    pause
    exit /b 1
)

echo  Installing to: %INSTALL_DIR%
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

rem --- clear the old program first --------------------------------------------
rem  A locked exe is how we detect a running app, and a stale _runtime must not be
rem  left to mix with the new one. state.json and accounts.ini live in this folder too
rem  and are deliberately left alone, so upgrading keeps your config.
if exist "%TARGET%" (
    del /f /q "%TARGET%" >nul 2>&1
    if exist "%TARGET%" (
        echo  ERROR: could not replace %EXE% -- the app may be running.
        echo  Close NMS Save Vault, then run install.bat again.
        echo(
        pause
        exit /b 1
    )
)
rem  The command-line launcher was called nmsvault.exe before it became nmsvault-cli.exe.
if exist "%INSTALL_DIR%\nmsvault.exe" del /f /q "%INSTALL_DIR%\nmsvault.exe" >nul 2>&1
if exist "%INSTALL_DIR%\_runtime" rmdir /s /q "%INSTALL_DIR%\_runtime"

xcopy "%APPSRC%" "%INSTALL_DIR%" /e /i /y /q >nul
if errorlevel 1 (
    echo  ERROR: could not copy the app into the install folder.
    echo  Is the app already running? Close it and try again.
    echo(
    pause
    exit /b 1
)

rem --- convenience launcher in the install folder ----------------------------
> "%INSTALL_DIR%\vault.bat" echo @echo off
>>"%INSTALL_DIR%\vault.bat" echo start "" "%%~dp0%EXE%"

rem --- bundle the uninstaller alongside the app so it's always available ------
if exist "%SRC%uninstall.bat" copy /y "%SRC%uninstall.bat" "%INSTALL_DIR%\uninstall.bat" >nul

echo(
set "DESK=Y"
set /p "DESK=  Create a Desktop shortcut?  [Y/n] "
set "MENU=Y"
set /p "MENU=  Create a Start Menu entry?  [Y/n] "
echo(

set "DID_SHORTCUT="

if /i not "%DESK:~0,1%"=="n" (
    call :mklink Desktop
    if not errorlevel 1 ( echo   - Desktop shortcut created. & set "DID_SHORTCUT=1" ) else echo   - Could not create the Desktop shortcut.
)
if /i not "%MENU:~0,1%"=="n" (
    call :mklink Programs
    if not errorlevel 1 ( echo   - Start Menu entry created. & set "DID_SHORTCUT=1" ) else echo   - Could not create the Start Menu entry.
)

echo(
if defined DID_SHORTCUT (
    echo  Done. Start "NMS Save Vault" from your new shortcut.
    echo  ^(Installed at %INSTALL_DIR%^)
    echo  To remove it later, run uninstall.bat in that folder.
    echo(
    pause
    exit /b 0
)

rem --- no shortcuts: leave vault.bat, open the folder, tell the user ----------
echo  No shortcuts created.
echo  A launcher named vault.bat has been placed in the install folder,
echo  which will now open. Double-click vault.bat to start the program.
start "" explorer "%INSTALL_DIR%"
powershell -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show('To start NMS Save Vault, double-click vault.bat in the folder that just opened.' + [Environment]::NewLine + [Environment]::NewLine + 'Folder: ' + $env:INSTALL_DIR, 'NMS Save Vault', 'OK', 'Information')"
exit /b 0

rem ---------------------------------------------------------------------------
:mklink
rem  %~1 = the folder to put the shortcut in: Desktop or Programs (the Start Menu).
rem  Windows is asked where that is, because it moves: OneDrive and folder
rem  redirection take the Desktop away from %USERPROFILE%\Desktop. Paths are passed
rem  via environment variables so spaces/quotes never break the PowerShell command line.
set "LNK_FOLDER=%~1"
powershell -NoProfile -Command "$ErrorActionPreference = 'Stop'; $d = [Environment]::GetFolderPath($env:LNK_FOLDER, 'Create'); if (-not $d) { exit 1 }; $w = New-Object -ComObject WScript.Shell; $s = $w.CreateShortcut((Join-Path $d $env:LNK_NAME)); $s.TargetPath = $env:TARGET; $s.WorkingDirectory = $env:INSTALL_DIR; $s.IconLocation = $(if (Test-Path $env:ICON) { $env:ICON + ',0' } else { $env:TARGET + ',0' }); $s.Description = 'NMS Save Vault'; $s.Save()"
exit /b %errorlevel%
