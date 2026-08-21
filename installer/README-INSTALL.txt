NMS Save Vault — Installation
=============================

What this is
------------
A safe backup, catalog and slot manager for No Man's Sky save files (PC), so you
can keep far more than the game's 15 save slots. It reads Steam saves (read/write)
and Xbox / Game Pass saves (read/write, same platform only).

What's in this zip
------------------
    NMSSaveVault\   the app: a launcher plus the _runtime folder it needs
    install.bat     copies that folder into place and offers shortcuts
    uninstall.bat   removes the app, its config and the shortcuts

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

No Python or other software is required — everything is bundled in the folder.
Installing over an older version keeps your config.

If you would rather not install at all, you can simply run
NMSSaveVault\NMSSaveVault.exe straight from where you extracted the zip.

Keep the folder together
------------------------
NMSSaveVault.exe is an unmodified, digitally signed copy of the official Python
runtime, renamed. Using the genuine signed program is what stops Windows warning
you about an unknown publisher, but it also means the launcher is not a
self-contained file: it only works next to the _runtime folder and the .dll files
that ship beside it. Copying the .exe out on its own will not work.

The Start Menu / Desktop shortcuts and vault.bat all handle this for you.

First run
---------
The app auto-detects your save folders and writes a small config file next to the
program itself:
    %LOCALAPPDATA%\Programs\NMSSaveVault\state.json
It shows your LIVE saves (each Steam account; Xbox accounts too) separately from
your BACKUPS. Use "Rescan" to pick up a new account or backup later.

Your saves are treated as precious: every change auto-snapshots first, and writes
are blocked while the game is running.

Command line
------------
nmsvault.exe, in the same folder, is the command-line version of the same tool
(nmsvault.exe status, nmsvault.exe list, and so on).

Uninstall
---------
Easiest: run  uninstall.bat  — it's placed in the install folder next to the app
(and is also in this zip). It removes the app, its config (state.json) and the
Desktop / Start Menu shortcuts. Your game saves and your backups / vault are NOT
touched. Close the app first, or it can't delete the running program.

Manual alternative (does the same thing by hand):
- Delete the folder:  %LOCALAPPDATA%\Programs\NMSSaveVault
  (this also removes the config, state.json, which lives there).
- Delete any shortcuts you created (Desktop / Start Menu).

Source & license
----------------
GPL-3.0. Source: https://github.com/GoodGuysFree/nms-save-vault
The bundled Python runtime is distributed under the PSF License; its full text is
in PYTHON_LICENSE.txt inside the NMSSaveVault folder.
