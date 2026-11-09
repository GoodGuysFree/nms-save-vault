# Orphan corvette remover (side utility)

> **WARNING: THIS TOOL EDITS YOUR SAVE FILES.**
>
> NMS Save Vault itself never changes what is inside a save; it only copies saves around.
> This tool is different. It opens a save, deletes data from it and writes it back. If it is
> wrong about your save, **you can lose corvettes, corvette interiors, or the whole save.**
>
> - It is **experimental and unsupported**. It is **not** part of the app and is not in the
>   release download.
> - The rule it uses was read from the game's program (NMS.exe, 2026-11) and checked against
>   one player's saves. **Hello Games has not documented it.** A game update can change it at
>   any time without notice.
> - It was tested on copies of one player's real saves, in a sandbox. That is all the testing
>   it has had.
> - **There is no warranty of any kind** (GPL-3.0, sections 15 and 16). You use it at your own
>   risk. Nobody can recover a save you did not back up.
>
> If you are not comfortable restoring a save from a backup by yourself, **do not use this
> tool.** If you only want to get rid of a corvette, giving it away to an NPC in the game is
> the safe way.

## What problem it addresses

A corvette's interior is not stored with the ship. It is stored as a separate "base" record
(type `PlayerShipBase`) in the save's list of bases. Save editors that delete a corvette by
removing only the ship can leave that base record behind. Players have reported trouble
after that, for example a new corvette suddenly picking up the old, deleted one's interior.
This tool finds such left-behind records ("orphans") and removes them.

The NMS Save Vault Organizer shows **"orphan corvette layouts"** on a save that has them. That
notice is information only. The app does not change the save.

## How it decides what an orphan is

- An interior record's `UserData` field holds the number of the ship slot of the corvette it
  belongs to. That is how the game itself finds a corvette's interior (read from the game's
  program, 2026-11).
- An interior record whose `UserData` names **no slot that holds a corvette** is an orphan.

The tool **refuses to touch a save at all** when two interior records name one corvette's
slot: the game uses one of them, and guessing which would be dangerous.

### The records it keeps by default: tied to a corvette by time

The first version of this tool tied an interior record to a corvette by time instead: a
corvette's seed is the Unix time it was created, and its interior record's owner timestamp is
that second or the next one. That held for 36 of 36 corvettes on the account it was developed
on, but it is not what the game uses. A record whose `UserData` names no corvette but whose
time ties it to one is **kept by default**, and the report says so. `--include-ts-linked`
removes it as well. **Do not use that flag** unless you have checked in the game that the
corvette it names still has its full interior without it.

## What it does NOT do

- **Xbox / Game Pass saves:** not handled at all.
- **GOG and other non-Steam folders:** refused. Only a Steam `st_<number>` folder is accepted.
- Older save formats: refused. Only the current (Worlds Part II era, 432-byte meta) Steam
  format is accepted, the only one it was tested on.
- Hidden or extra parts inside a corvette that still exists: not looked at.
- **Cross-save and cloud copies:** the tool only changes the files on this PC. A copy in Steam
  Cloud or in the Hello Games cross-save cloud can still hold the old save (see below).

## Before you run it

1. **Close No Man's Sky.** The tool refuses to write while the game runs, or when it cannot
   tell whether the game runs.
2. **Have NMS Save Vault set up with a vault.** Before anything is written, the tool asks the
   app to keep every slot it is about to change in your vault. It then reads each file back
   from the vault and compares it byte for byte with the file on disk. If any of that fails,
   **nothing is written.**
3. **Make your own extra copy anyway.** Copy the whole save folder somewhere outside the NMS
   folders. The vault is the tool's backup; your own copy is yours. The save folder is
   `%APPDATA%\HelloGames\NMS\st_<your Steam id>`.
4. **Steam Cloud:** after the change, Steam will upload the changed files the next time it
   syncs. If Steam ever reports a cloud conflict for No Man's Sky, stop and think before you
   pick a side: the cloud side may be the old save.
5. **Hello Games cross-save:** if you use cross-save, the save in that cloud is not changed by
   this tool. Uploading or downloading a cross-save afterwards can bring the orphans back.

## How to run it

It runs from a source checkout of this repository, with [uv](https://github.com/astral-sh/uv).
The first run downloads the `lz4` package, which this tool needs to write the game's
compressed format. The app itself does not use it.

**Step 1 - report only.** Nothing is written. Read every line.

```
uv run --with lz4 python tools/orphan_corvettes/remove_orphan_corvettes.py "%APPDATA%\HelloGames\NMS\st_<id>" 11 13
```

The numbers are slot numbers (1-15), as the Organizer shows them (S11 is slot 11). Both saves
of each slot (the auto-save and the restore point) are checked. Each interior record gets a
line saying whether it is kept, and why, or removed.

**Step 2 - write.** Close the game, then run the same command with `--write` added:

```
uv run --with lz4 python tools/orphan_corvettes/remove_orphan_corvettes.py "%APPDATA%\HelloGames\NMS\st_<id>" 11 13 --write
```

With `--write` the tool runs these steps in order, and stops at the first one that fails:

1. It checks that the game is closed.
2. It works out the edited save in memory and proves the result. The new save must decode to
   exactly the old save minus the removed records, and nothing else may differ. The re-packed
   file must decode back to it, and the file's header must still read correctly.
3. It keeps each slot to change in your vault, using the app's own `keep`.
4. It reads every file of the slot back from the vault and compares it byte for byte.
5. It checks that the game is still closed and that no file changed since step 2.
6. It writes each new file next to the old one, reads it back, and only then swaps it in.

`--vault <folder>` uses a vault other than the app's configured one.

## After running it

- Load **each** changed save in the game. Check that every corvette you own is still there and
  has its full interior **before you play on or save.**
- Check the Organizer. The changed saves show up as new versions, marked "edited outside the
  game". The versions from before the change are in the vault.

## How to undo it

The versions from before the change are in your vault, in an entry labelled `kept: ...`. The
tool prints the entry id.

- In the Organizer, pick the version from before the change and put it back into its slot.
- From the command line: `nmsvault-cli place --from <entry id> --src-slot <slot> S<slot>`.

The app refuses to put back a save written by a **newer game version than the app knows**.
After a game update, update NMS Save Vault first. Failing that, copy the files back by hand from
your own extra copy, with the game closed: `saveN.hg` and `mf_saveN.hg` for both saves of the
slot.

## License

Part of NMS Save Vault, GPL-3.0. **No warranty.** See the repository's LICENSE.
