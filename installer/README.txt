NMS Save Vault
==============

What this is
------------
A safe backup, catalog and slot manager for No Man's Sky save files (PC), so you
can keep far more than the game's 15 save slots. It reads and writes Steam saves,
and reads and writes Xbox / Game Pass saves within Xbox.

Run it
------
1. Extract this whole zip to any folder (do not run from inside the zip).
2. Open the  NMSSaveVault  folder.
3. Double-click  NMSSaveVault.exe

That is all there is to it. No Python, no installation, no administrator rights,
nothing written outside the folder. The app's config (state.json, accounts.ini) is saved next
to the program, so the folder is self-contained: move it, copy it to a USB
stick, or delete it when you are done with it.

What's in this zip
------------------
    NMSSaveVault\   the app -- run NMSSaveVault.exe in here
    install.bat     optional: copies the app into place and adds shortcuts
    uninstall.bat   undoes install.bat
    README.txt      this file

Keep the folder together
------------------------
NMSSaveVault.exe is an unmodified, digitally signed copy of the official Python
runtime, renamed. Using the genuine signed program is what stops Windows warning
you about an unknown publisher, but it also means the launcher is not a
self-contained file: it only works next to the _runtime folder and the .dll files
that ship beside it. Copying the .exe out on its own will not work.

Optional: install it properly
-----------------------------
If you would rather have NMS Save Vault on your Start Menu or Desktop, run
install.bat. It copies the NMSSaveVault folder to

    %LOCALAPPDATA%\Programs\NMSSaveVault

and asks whether to add a Desktop shortcut and/or a Start Menu entry. If you
decline both it leaves a launcher named vault.bat in that folder instead.
Installing over an older version keeps your config.

To undo it, run uninstall.bat -- it is placed in the install folder next to the
app, and is also in this zip. It removes the app, its config (state.json, accounts.ini) and the
shortcuts. Your game saves and your backups / vault are NOT touched. Close the
app first, or it cannot delete the running program.

Note that Windows tags every file that came out of a downloaded zip, and may
prompt about install.bat on that basis alone. Running NMSSaveVault.exe directly
avoids that.

First run
---------
The app auto-detects your save folders and shows your LIVE saves (each Steam
account, and each Xbox / Game Pass account) separately from your BACKUPS. Use
"Rescan" to pick up a new account or backup later.

Your saves are treated as precious: every change auto-snapshots first, and writes
are blocked while the game is running.

Command line
------------
nmsvault.exe, in the same folder, is the command-line version of the same tool
(nmsvault.exe status, nmsvault.exe list, and so on).

Source & license
----------------
GPL-3.0. Source: https://github.com/GoodGuysFree/nms-save-vault
The bundled Python runtime is distributed under the PSF License; its full text is
in PYTHON_LICENSE.txt inside the NMSSaveVault folder.
