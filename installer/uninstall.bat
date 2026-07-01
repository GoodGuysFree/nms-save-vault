@echo off
setlocal EnableExtensions
title NMS Save Vault - Uninstaller

rem ---------------------------------------------------------------------------
rem  NMS Save Vault uninstaller.
rem  Removes the app + its config from %LOCALAPPDATA%\Programs\NMSSaveVault and
rem  deletes the Desktop / Start Menu shortcuts. It NEVER touches your game saves
rem  or your backups / vault. Runs per-user, no admin rights, no registry.
rem
rem  install.bat copies this script into the install folder, so it can delete the
rem  very folder it lives in; the (goto) trick below releases the file handle so
rem  that self-deletion succeeds.
rem ---------------------------------------------------------------------------

set "INSTALL_DIR=%LOCALAPPDATA%\Programs\NMSSaveVault"
set "EXE=NMSSaveVault.exe"
set "DESKTOP_LNK=%USERPROFILE%\Desktop\NMS Save Vault.lnk"
set "STARTMENU_LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\NMS Save Vault.lnk"

echo(
echo  ============================================
echo    NMS Save Vault - Uninstaller
echo  ============================================
echo(
echo  This removes the app and its config from:
echo      %INSTALL_DIR%
echo  and deletes the Desktop / Start Menu shortcuts.
echo(
echo  Your game saves and your backups / vault are NOT touched.
echo(

set "GO=N"
set /p "GO=  Uninstall NMS Save Vault now?  [y/N] "
if /i not "%GO:~0,1%"=="y" (
    echo(
    echo  Cancelled. Nothing was removed.
    echo(
    pause
    exit /b 0
)
echo(

rem --- remove shortcuts (ignore if absent) -----------------------------------
if exist "%DESKTOP_LNK%"   ( del /f /q "%DESKTOP_LNK%"   >nul 2>&1 & echo   - Desktop shortcut removed. )
if exist "%STARTMENU_LNK%" ( del /f /q "%STARTMENU_LNK%" >nul 2>&1 & echo   - Start Menu entry removed. )

rem --- delete the app exe first: this also detects a running app (locked exe) -
if exist "%INSTALL_DIR%\%EXE%" (
    del /f /q "%INSTALL_DIR%\%EXE%" >nul 2>&1
    if exist "%INSTALL_DIR%\%EXE%" (
        echo(
        echo  ERROR: could not remove %EXE% -- the app may be running.
        echo  Close NMS Save Vault, then run this uninstaller again.
        echo(
        pause
        exit /b 1
    )
)

echo   - Removing %INSTALL_DIR%
echo(
echo  Done. NMS Save Vault has been uninstalled.
echo  ^(Your saves and backups were left untouched.^)
echo(

rem --- remove the install folder (state.json, vault.bat, and this script) -----
if /i "%~dp0"=="%INSTALL_DIR%\" (
    rem running from inside the folder we must delete: release our own handle first
    pause
    (goto) 2>nul & rmdir /s /q "%INSTALL_DIR%"
) else (
    rmdir /s /q "%INSTALL_DIR%" >nul 2>&1
    pause
)
exit /b 0
