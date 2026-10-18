# T1 / T2 - owner validation runs (Steam <-> Xbox)

Experimental. The transfer is off unless you start the app with the gate (step 3). Use a
THROWAWAY save only. Background: D5 3.5.3, R9.

What the app does: copies the save data unchanged and converts only the small meta file. It
goes into an EMPTY slot only, and only if the destination folder has a full backup younger than
30 minutes. The app never empties a slot, so a fill has no Undo button (see "Undo").

## Before either run

1. Close No Man's Sky, Steam's sync prompt, and the Xbox app. Wait 60 seconds.
2. Full backup of BOTH live folders (the vault is the copy `D:\VaultCopy`). Run each, then look
   for "created full backup":
   ```pwsh
   uv run nmsvault backup --vault D:\VaultCopy --live "<Steam account folder>" --label "T1 before"
   uv run nmsvault backup --vault D:\VaultCopy --live "<Xbox wgs account folder>" --label "T1 before"
   ```
   Redo the backup of the destination folder if more than 30 minutes pass before step 5.
3. Start the app with the gate:
   ```pwsh
   $env:NMSVAULT_CROSS_PLATFORM=1
   uv run python -c "from nms_save_vault import gui; gui.App(vault=r'D:\VaultCopy').mainloop()"
   ```
4. Make the throwaway save in the SOURCE platform's game: new game, play about 5 minutes, build a
   base computer and 3 parts (more than one base if you can), discover one planet, save at a
   beacon, quit. Note the name, play time and how many bases and discoveries it has.

## T1 - Steam to Xbox

5. In the app: Organizer, pick the throwaway save's latest version, "Move to live...".
6. Under "Into", choose the Xbox account (marked "experimental"). Pick an EMPTY Xbox slot (the
   dialog suggests one). Tick the "Experimental - for validation runs" box and the "another
   account" box. If "Move to live" stays grey, read the red line: usually the backup is older
   than 30 minutes, or the slot is not empty.
7. Move. Wait for "Put ... into Xbox slot N". Do not start the game yet.
8. Start NMS (Game Pass). Check:
   - (a) the slot shows the right name, play time and date;
   - (b) it loads;
   - (c) a base is editable (move a part) and the discovery is listed. Check EVERY base, not
     one: one 2025 Xbox copy kept 2 of 4 bases on the other owner (R9);
   - (d) save at a beacon, quit.
9. Back in the app: rescan. Tell me (GGF) the slot; I check read-only that the base owner, the
   local id and the platform token on bases and discoveries switched to the Xbox identity.
10. Wait for the Xbox app to show the save as synced. Start NMS again: (e) no conflict dialog,
    the save is intact.

Pass = (a) to (e), all bases editable.

## T2 - Xbox to Steam (mirror)

Same, with Steam Cloud ON. Source: a throwaway save made on Xbox. Destination: an EMPTY Steam
slot; back up the Steam folder first. In step 8 start the game through Steam, and add:
(f) quit, relaunch through Steam: the placed save is still there (Steam Cloud accepted it). If
Steam asks which copy to keep, choose the one on this PC and note that it asked.

## If something looks wrong

Stop. Do not play on. Note which check failed and what the game said.

## Undo

A fill into an empty slot has no Undo button (the app never empties a slot). To go back, restore
the destination folder from the move's safety snapshot using the app's whole-folder restore, or
`uv run nmsvault restore <the backup id from step 2>`. With the game closed. The throwaway save
is the only thing lost.
