NMS Save Vault — Installation
=============================

What this is
------------
A safe backup, catalog and slot manager for No Man's Sky save files (PC), so you
can keep far more than the game's 15 save slots. It reads Steam saves (read/write)
and Xbox / Game Pass saves (read-only).

Install
-------
1. Extract this whole zip to any folder (do not run from inside the zip).
2. Double-click  install.bat
3. Answer the two prompts:
      - Create a Desktop shortcut?   [Y/n]
      - Create a Start Menu entry?   [Y/n]
   - If you say yes to either, start the app from that shortcut afterwards.
   - If you say NO to both, the installer leaves a launcher named  vault.bat  in
     the install folder, opens that folder, and shows a note. Double-click
     vault.bat any time to start the program.

The app installs to:
    %LOCALAPPDATA%\Programs\NMSSaveVault\NMSSaveVault.exe

No Python or other software is required — everything is bundled in the .exe.

First run
---------
The app auto-detects your save folders and writes a small config file:
    %APPDATA%\HelloGames\NMS\NMSSaveVault\state.json
It shows your LIVE saves (each Steam account; Xbox accounts read-only) separately
from your BACKUPS. Use "Rescan" to pick up a new account or backup later.

Your saves are treated as precious: every change auto-snapshots first, writes are
blocked while the game is running, and Xbox saves are never written to.

Windows SmartScreen
-------------------
The .exe is not code-signed, so SmartScreen may warn the first time. Choose
"More info" -> "Run anyway". (You can inspect/build it yourself from the source.)

Uninstall
---------
- Delete the folder:  %LOCALAPPDATA%\Programs\NMSSaveVault
- Delete any shortcuts you created (Desktop / Start Menu).
- Optional: delete the config at %APPDATA%\HelloGames\NMS\NMSSaveVault
  (this does NOT touch your game saves or your backups/vault).

Source & license
----------------
GPL-3.0. Source: https://github.com/GoodGuysFree/nms-save-vault
