@echo off
setlocal EnableExtensions
title NMS Save Vault - Installer

rem ---------------------------------------------------------------------------
rem  NMS Save Vault installer.
rem  Copies the app to %LOCALAPPDATA%\Programs\NMSSaveVault, then offers a
rem  Desktop shortcut and/or a Start Menu entry. If you decline both, it leaves
rem  a vault.bat launcher in the install folder, opens that folder, and tells
rem  you to run vault.bat.
rem ---------------------------------------------------------------------------

set "SRC=%~dp0"
set "EXE=NMSSaveVault.exe"
set "INSTALL_DIR=%LOCALAPPDATA%\Programs\NMSSaveVault"
set "TARGET=%INSTALL_DIR%\%EXE%"
set "ICON=%INSTALL_DIR%\nmsvault.ico"
set "DESKTOP_LNK=%USERPROFILE%\Desktop\NMS Save Vault.lnk"
set "STARTMENU_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "STARTMENU_LNK=%STARTMENU_DIR%\NMS Save Vault.lnk"

echo(
echo  ============================================
echo    NMS Save Vault - Installer
echo  ============================================
echo(

if not exist "%SRC%%EXE%" (
    echo  ERROR: %EXE% was not found next to this installer.
    echo  Please extract the WHOLE zip first, then run install.bat again.
    echo(
    pause
    exit /b 1
)

echo  Installing to: %INSTALL_DIR%
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
copy /y "%SRC%%EXE%" "%TARGET%" >nul
if errorlevel 1 (
    echo  ERROR: could not copy %EXE% into the install folder.
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

rem --- keep the app icon on disk so shortcuts point straight at it ------------
if exist "%SRC%nmsvault.ico" copy /y "%SRC%nmsvault.ico" "%ICON%" >nul

echo(
set "DESK=Y"
set /p "DESK=  Create a Desktop shortcut?  [Y/n] "
set "MENU=Y"
set /p "MENU=  Create a Start Menu entry?  [Y/n] "
echo(

set "DID_SHORTCUT="

if /i not "%DESK:~0,1%"=="n" (
    call :mklink "%DESKTOP_LNK%"
    if not errorlevel 1 ( echo   - Desktop shortcut created. & set "DID_SHORTCUT=1" )
)
if /i not "%MENU:~0,1%"=="n" (
    if not exist "%STARTMENU_DIR%" mkdir "%STARTMENU_DIR%"
    call :mklink "%STARTMENU_LNK%"
    if not errorlevel 1 ( echo   - Start Menu entry created. & set "DID_SHORTCUT=1" )
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
rem  %~1 = full path of the .lnk to create. Paths are passed via environment
rem  variables so spaces/quotes never break the PowerShell command line.
set "LNK=%~1"
powershell -NoProfile -Command "$w = New-Object -ComObject WScript.Shell; $s = $w.CreateShortcut($env:LNK); $s.TargetPath = $env:TARGET; $s.WorkingDirectory = $env:INSTALL_DIR; $s.IconLocation = $(if (Test-Path $env:ICON) { $env:ICON + ',0' } else { $env:TARGET + ',0' }); $s.Description = 'NMS Save Vault'; $s.Save()"
exit /b %errorlevel%
