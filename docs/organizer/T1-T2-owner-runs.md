# T1 / T2 - owner validation runs (Steam <-> Xbox)

Experimental. The transfer is off unless you start the app with the gate (step 4). Background:
D5 3.5.3, R9.

What the app does: copies the save data unchanged, converts only the small meta file, and puts
the result into an EMPTY slot (no file of that slot at all) of a folder that has a full backup
younger than 30 minutes. The source is only read: use a small, unimportant existing save.
T1 = Steam to Xbox (the target slot is on Xbox). T2 = Xbox to Steam (the target slot is on Steam).

## Before either run

1. Close No Man's Sky, Steam's sync prompt and the Xbox app. Wait 60 seconds.
2. Back up both live folders. List your Steam folders first and set `$steam` to the account
   you play on (there may be more than one `st_` folder):
   ```pwsh
   cd C:\Devel\NMSVault
   Get-ChildItem "$env:APPDATA\HelloGames\NMS" -Directory -Filter "st_*" | Select-Object Name
   $steam = "$env:APPDATA\HelloGames\NMS\st_<your Steam id>"
   $xbox = (Get-ChildItem "$env:LOCALAPPDATA\Packages\HelloGames.NoMansSky_bs190hzg1sesy\SystemAppData\wgs" -Directory -Filter "*_*" | Select-Object -First 1).FullName
   uv run nmsvault backup --vault D:\VaultCopy --live $steam --label "T1 before"
   uv run nmsvault backup --vault D:\VaultCopy --live $xbox --label "T1 before"
   ```
   Then start the app normally (`uv run nmsvault-gui --vault D:\VaultCopy`), open the save you
   will delete on the TARGET platform and use "Keep a copy in the vault now". Close the app.
3. Free a slot IN THE GAME on the TARGET platform (Xbox for T1, Steam for T2): in the game's
   own load menu delete that save. Close the game. Redo the backup command for the target
   folder (it must be under 30 minutes old when you move).
4. Start the app with the gate, in its own shell (close the shell afterwards so the gate does
   not stay on):
   ```pwsh
   cd C:\Devel\NMSVault
   $env:NMSVAULT_CROSS_PLATFORM=1
   uv run nmsvault-gui --vault D:\VaultCopy
   ```
5. Click Rescan and confirm the target slot shows as empty.

## T1 - Steam to Xbox

6. In the app: Organizer, pick the small source save's latest version, "Move to live...".
7. Under "Into", choose the Xbox account (marked "experimental"). Pick the EMPTY slot. Tick the
   "Experimental - for validation runs" box and the "another account" box. If "Move to live"
   stays grey, read the red line: usually the backup is older than 30 minutes, or the slot
   is not empty.
8. Move. Wait for "Put ... into Xbox slot N". Do not start the game yet.
9. Start NMS (Game Pass). Check:
   - (a) the slot shows the right name and play time (the date reads "now" by design; the
     original date stays in the vault record);
   - (b) it loads;
   - (c) a base is editable (move a part) and a discovery is listed. Check EVERY base, not one:
     one 2025 Xbox copy kept 2 of 4 bases on the other owner (R9);
   - (d) save at a beacon, quit.
10. Rescan in the app. Tell me (GGF) the slot; I check read-only that the base owner, the local
    id and the platform token on bases and discoveries switched to the Xbox identity.
11. Wait for the Xbox app to show the save as synced. Start NMS again: (e) no conflict dialog,
    the save is intact.

Pass = (a) to (e), all bases editable.

## T2 - Xbox to Steam (mirror)

Same steps with Steam as the target and Steam Cloud ON; the source is a small save on Xbox. In
step 9 start the game through Steam, and add: (f) quit, relaunch through Steam: the placed save
is still there (Steam Cloud accepted it). If Steam asks which copy to keep, choose the one on
this PC and note that it asked.

## If something looks wrong

Stop. Do not play on. Note which check failed and what the game said.

## Undo

The app never empties a slot, so a transfer has no Undo button. To undo it: close the
game, then in the game's own load menu on the target platform delete the transferred
save, exactly as in step 3. The vault keeps the copy you made in step 3 and the backups; the
source save was never touched.
