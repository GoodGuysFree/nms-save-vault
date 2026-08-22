# NMS Save Vault

Safe backup, catalog, and slot management for **No Man's Sky** (PC — Steam and Xbox /
Game Pass; GOG / Epic use the same format) save files — designed to give you effectively
unlimited save slots beyond the game's 15. See [Platform support](#platform-support).

## QuickStart

**Just want to run it? No Python needed.** Download the ready-to-use Windows kit from the
[**latest release**](https://github.com/GoodGuysFree/nms-save-vault/releases/latest) — grab
`NMSSaveVault-Setup-v0.1.0.zip` under **Assets**.

Extract the zip, open the `NMSSaveVault` folder and run **`NMSSaveVault.exe`**. That is the
whole thing — Python and Tkinter are bundled, nothing is installed, and because the app
starts through a signed copy of the official Python runtime rather than an unsigned custom
`.exe`, Windows should not warn about an unknown publisher. Want Desktop / Start-Menu
shortcuts? `install.bat` in the zip adds them. Full details under
[Run it](#run-it-windows-no-python-needed); every version is on the
[Releases](https://github.com/GoodGuysFree/nms-save-vault/releases) page.

## What it does

1. **Full backup / restore** — snapshot the entire live save folder, and restore any
   snapshot (always auto-backing-up the current state first).
2. **Catalog + per-slot operations** — browse every backup and the saves inside it by
   name/mode/play-time/date; lift a single slot aside; and repopulate any live slot from
   any save in any cataloged backup (re-keying the meta when the slot number differs).
   You can also inspect a slot's two saves (the Auto-Save and the Restore-Point)
   individually and force either one to become the newest.
3. **Import** — register an existing manual backup folder into the catalog, or import an
   entire copied Save Vault folder: it compares that vault's entries with yours and offers
   to copy the new ones in or index them in place (idempotent — re-importing is harmless).
4. **Account display names** — give each account a name of your choosing and the app shows
   that instead of the real id everywhere, so a screenshot or a screen-share never exposes
   your `st_<steamid64>` or Xbox `<xuid>_<titleid>`. See
   [Account display names](#account-display-names).
5. **Know what you are looking at** — each slot's two saves are named for what they are
   (**Auto-Save** vs **Restore-Point**), the difficulty preset is shown by name, Xbox saves
   report their cloud sync state, and hovering any row explains it. See
   [The two saves in every slot](#the-two-saves-in-every-slot).

## Why it's safe

* The save **data** file is copied **verbatim** — never recompressed — so it can't drift.
* Only the small 432-byte **meta** is transformed (XXTEA re-encryption / timestamp edit).
* Every destructive operation auto-snapshots first, writes atomically (stage → validate →
  swap), validates by header + size cross-checks + hashes, and refuses to run while the
  game is open.

See [DESIGN.md](DESIGN.md) for the architecture and the verified save-format details.

## Platform support

| Platform | Status |
|---|---|
| **Steam** (Windows) | **Supported and tested** — the primary target. |
| **GOG.com** (Windows) | **Should work — untested.** GOG uses the *identical* Steam save format, just in a `DefaultUser` folder instead of `st_<steamid>`. |
| **Epic Games Store** (Windows) | **Should work — untested.** Same as GOG: Epic and GOG share the exact same `DefaultUser` folder and save format. |
| **Microsoft Store / Xbox Game Pass** (Windows) | **Supported** — read *and* same-platform write; see [Xbox / Game Pass](#xbox--game-pass-read-and-write). |
| **Steam Deck / Linux** (Proton) | **Not yet supported.** The save format is the same, but this is a Windows desktop app and the saves live inside a Proton prefix. |
| **macOS** (native or via Steam) | **Not yet supported.** Different OS; the Windows app can't reach `~/Library/Application Support/HelloGames/NMS`. |

**GOG & Epic — testers wanted.** The on-disk files are byte-for-byte the same Steam format
this tool already reads and writes, so everything *should* just work. But nobody has confirmed
it on a real GOG or Epic install yet, and auto-discovery does not yet recognise the
`DefaultUser` folder as a live source — for now, point commands at it explicitly, e.g.
`nmsvault status --live "%AppData%\HelloGames\NMS\DefaultUser"`. **If you play on GOG or Epic,
we'd love your help:** try it against a *copy* of your save first, then
[open an issue or discussion](https://github.com/GoodGuysFree/nms-save-vault/issues) with how it
went. Both success testimonials and bug reports move these from "untested" to officially supported.

**Linux / Steam Deck and macOS are not supported yet, and help is welcome.** If you'd like to
work on a Proton-aware path finder, a Linux/macOS build, or just test on those platforms, please
[open an issue](https://github.com/GoodGuysFree/nms-save-vault/issues) — contributions and
volunteers are very welcome.

## Run it (Windows, no Python needed)

Download **`NMSSaveVault-Setup-v0.1.0.zip`** from the
[**Releases**](https://github.com/GoodGuysFree/nms-save-vault/releases) page (under the
release's **Assets**) and extract it. Open the `NMSSaveVault` folder and run
**`NMSSaveVault.exe`** — that is all. Everything (Python + Tkinter) is bundled, nothing is
installed, and the config (`state.json`, `accounts.ini`) is written beside the program, so the folder is
self-contained: move it, put it on a USB stick, delete it to be rid of it. Beside the GUI
launcher sits `nmsvault.exe`, the same tool on the command line (see [Usage](#usage)).

### Optional: shortcuts and a tidy install folder

Would rather have it on the Start Menu? Run **`install.bat`** from the zip. It copies the
`NMSSaveVault` folder to `%LOCALAPPDATA%\Programs\NMSSaveVault` and asks whether to add a
Desktop shortcut and/or a Start Menu entry; if you decline both it leaves a `vault.bat`
launcher there instead. Installing over an older version keeps your config. To undo it, run
**`uninstall.bat`** — it deletes the app, its config, and the shortcuts, leaving your game
saves and backups / vault untouched. No registry entries or admin rights either way.

Windows tags everything that came out of a downloaded zip, and can prompt about a `.bat` on
that basis alone. Running `NMSSaveVault.exe` directly avoids that entirely.

### Why there's no unknown-publisher warning

`NMSSaveVault.exe` is a verbatim renamed copy of the Authenticode-signed `pythonw.exe`
published by the Python Software Foundation, so Windows starts a signed binary it already
trusts instead of an unsigned custom one. Renaming a file does not affect its signature —
check for yourself:

```pwsh
Get-AuthenticodeSignature .\NMSSaveVault\NMSSaveVault.exe
# Status: Valid   SignerCertificate: CN=Python Software Foundation, ...
```

Nothing is patched into it — that would void the signature — so the app is dispatched from
`_runtime\sitecustomize.py`, which Python imports during startup. The trade-off is that the
launcher is not self-contained: it only works beside its `_runtime` folder and the `.dll`
files that ship with it. `install.bat`'s shortcuts use the app's own icon, so Python's shows
only on the file in the folder itself.

This is the genuine article being trusted, not this project's code being vouched for: it
removes the warning, but signing *this* app would need a paid certificate.

### Building the distributable yourself

```pwsh
pwsh -ExecutionPolicy Bypass -File packaging\build_portable.ps1     # -> dist\NMSSaveVault\
pwsh -ExecutionPolicy Bypass -File packaging\make_installer_zip.ps1 # -> dist\NMSSaveVault-Setup.zip
```

`build_portable.ps1` downloads the official runtime from python.org (the embeddable package,
plus `tcltk.msi` for Tkinter, which that package omits), caches both under
`build\runtime-cache\`, refuses to package anything not validly signed by the Python Software
Foundation, and lays the result out around a copy of `src\nms_save_vault`. Nothing needs to be
installed to build it.

## Requirements

* Python 3.10+ (developed on 3.12), standard library only — no runtime dependencies.
* [uv](https://github.com/astral-sh/uv) for environment management (dev and tests); building
  the distributable needs nothing beyond Windows and an internet connection.

## Dev setup

```pwsh
uv venv
uv pip install -e ".[dev]"
uv run pytest
```

## Auto-configuration

On first run the app **auto-detects** every save folder on the PC and records them in a
small config file, `state.json`, kept **in the install directory next to the executable**
(so the config is portable with the program; from source it falls back to
`%LOCALAPPDATA%\NMSSaveVault`):

* Each canonical `st_<steamid64>` folder directly under the NMS root is a **live** source
  (writable). Two Steam accounts → two live sources.
* Each Xbox / Game Pass `wgs` account folder is a **live** source, writable within Xbox.
* Any *other* save folder under the NMS root — a hand-pasted `st_… - Copy`, a renamed or
  dated folder — is treated as an **in-place backup**, not a live target.

The GUI shows the two groups separately (**● LIVE SAVES** vs **■ BACKUPS**), highlights the
active write target, and badges Xbox folders read-only. Use **Rescan** (GUI) or
`nmsvault sources --rescan` (CLI) to pick up a new account or backup later; your manual
edits to the config are preserved. Discovery is strictly read-only.

## Account display names

Save folders are named after the account that owns them, so the raw id would otherwise show
up in folder names, paths, source labels and operation messages throughout both front-ends.
Map each account to a name of your choosing and the app shows only that name — everywhere:

* **GUI:** **Accounts…** on the toolbar (or right-click any row → *Account display names…*).
* **CLI:** `nmsvault accounts` lists them; `nmsvault accounts --set <id>="Main"` sets one and
  `--clear <id>` removes it.

Those two places are the *only* ones that still show the real identifiers — they are where
you configure them. The mapping is a plain `accounts.ini` beside `state.json`, safe to edit
by hand:

```ini
[accounts]
76561197975032661 = Main
000901F0DD67CC4E_29070100B936489ABCE8B9AF3980429C = Xbox
```

Clearing a name shows the real id again. This is **display only** — `state.json`,
`catalog.json` and every path the app opens keep the real identifiers, so nothing about how
saves are found or written changes. If `accounts.ini` cannot be parsed the app stops with an
error rather than falling back to showing the ids you asked it to hide.

## The two saves in every slot

Every save slot holds **two** saves, and they are not interchangeable — the game keeps them
apart so that neither overwrites the other:

| | What writes it |
|---|---|
| **Auto-Save** | The game, by itself, every few minutes. |
| **Restore-Point** | You: leaving your ship, or using a save point, a save beacon, or a point-of-interest save. |

**Either can be the newer one.** Exit your ship a minute after an auto-save and the
Restore-Point is ahead; play a long stretch in your ship and the Auto-Save is. The app marks
the one the game will actually load with `*`, and **Promote** forces the other to be it.

Which file is which is fixed by position, not guesswork: `save.hg`, `save3.hg`, `save5.hg` …
are auto-saves and `save2.hg`, `save4.hg` … are restore points. The reference library
`libNOM.io` derives it the same way (`SaveType = SaveTypeEnum(CollectionIndex % 2)`), which
is why the Xbox containers are named `Slot<N>Auto` / `Slot<N>Manual` on disk.

**Difficulty** is the save's difficulty preset — Normal, Creative, Custom, Relaxed, Survival,
Permadeath. (Saves written before the Waypoint update stored this as a *game mode* instead,
so for those the old value is shown. Waypoint made difficulty freely configurable and the
game has written "Normal" into the old field for every save ever since, which is why it is no
longer worth showing on its own.)

**Cloud sync.** Xbox / Game Pass records a sync state per save (`Synced`, `Modified`,
`Created`), shown in the Status column. Steam has no per-save equivalent — Steam Cloud syncs
the whole folder, and `steam_autocloud.vdf` holds only an account id — so a Steam folder is
reported as cloud-enabled at folder level and no per-save state is invented.

## Appearance and updates

**Theme.** The dropdown at the top right offers **Light**, **Dark**, or **System**, which
follows your Windows light/dark setting. Your choice is remembered in `state.json`. (Light
keeps the native Windows widget styling the app has always had; dark switches to a
fully-colourable widget theme, because the native one ignores colour settings.)

**Update checks are opt-in.** The first run asks whether the app may check for new versions.
If you say yes, it asks GitHub **once a day, on startup**, whether a newer release exists and
shows a bar at the top when there is one, with a button to open the download page. Nothing is
downloaded or installed for you, and nothing about you or your saves is sent — this is the
only thing the app uses the network for. Right-click anywhere → **Check for updates…** to
check immediately, or to turn the daily check back on. The answer lives in `state.json` as
`update_check` (`ask` / `on` / `off`).

## Usage

Both front-ends share the same safety-checked core. The live folder and a vault folder
(default: a `_SaveVault` sibling of `st_<id>`) come from the config (or are auto-detected);
override with `--live`/`--vault`.

GUI:

```pwsh
nmsvault-gui        # or: python -m nms_save_vault.gui
```

CLI:

```pwsh
nmsvault status                          # live folder + 15 slots, both saves each
nmsvault sources [--rescan]              # configured live sources (Steam/Xbox accounts)
nmsvault list                            # catalog entries
nmsvault discover --add                  # find existing backups, add them in place
nmsvault backup --label "before update"  # full snapshot into the vault
nmsvault extract 9 --label "main"        # lift slot 9 aside
nmsvault repopulate --from <id|folder> --src-slot 9 --to-slot 3   # re-keys the meta
nmsvault promote --slot 9 --member B     # force B (the restore-point) to be newest
nmsvault restore <entry-id>              # mirror the live folder to a backup
nmsvault undo                            # restore the last auto-snapshot
nmsvault verify [live|<id>|<folder>]
nmsvault accounts [--set <id>=NAME] [--clear <id>]   # hide the real account ids
```

Every write first checks the game is closed (override `--force`), auto-snapshots the
live state, writes atomically, validates, and logs to `oplog.jsonl` for `undo`.

## Xbox / Game Pass (read **and write**)

Microsoft Store / Xbox Game Pass saves (the `wgs` container format under
`%LOCALAPPDATA%\Packages\HelloGames.NoMansSky_bs190hzg1sesy\SystemAppData\wgs`) are fully
supported: `discover` finds them, and `verify` / `import` / the GUI show their slots, names,
play times and summaries just like Steam saves. Point any command at the account folder:

```pwsh
nmsvault verify "<...>\SystemAppData\wgs\<accountfolder>"
nmsvault discover --add        # also catalogs the Xbox folder if present
```

**Same-platform writes are supported** — backup, restore, per-slot extract / repopulate, and
promote all work within Xbox, exactly as for Steam (every write auto-snapshots first, so
`undo` works). The wgs writer rotates the blob GUIDs, rewrites `containers.index` with the
correct sync state, and copies the save data verbatim — it follows the layout used by
[libNOM.io](https://github.com/zencq/libNOM.io) (see Credits).

**Cross-platform transfer (Steam ↔ Xbox) is not yet supported** and is gated with a "coming
soon" notice: the obfuscated save body carries a platform field and the two platforms use
different meta formats, so a faithful transfer needs a conversion step that isn't built yet.

> First time writing to a real Game Pass save? Close the game, and keep the auto-snapshot
> (the app makes one before every write) — or take a full backup first.

## Recommended workflow (Steam Cloud)

No Man's Sky uses Steam Cloud, which syncs the `st_<id>` folder. To avoid cloud conflicts:

1. **Fully close the game** before any write operation (the app blocks writes while
   `NMS.exe` is running).
2. Make your changes (restore / repopulate / promote).
3. Launch the game. If Steam shows a cloud conflict, choose the **local** copy.

The vault lives outside `st_<id>`, so it is never scanned by the game or synced by Steam.

## Status

Working. Core format/crypto and all operations are verified against the real save files and
in a temp sandbox (196 tests). Xbox / Game Pass saves are supported for reading **and**
same-platform writing — verified against a real install (reads) and synthetic `wgs` fixtures
(writes). A full file-copy safety backup of the live folder was made before development
(`C:\Devel\NMS-SaveBackup-SAFETY-2026-06-24`).

## Version history

| Version | Date | Highlights |
|---|---|---|
| **0.1.0** | 2026-08-22 | **First milestone release.** Identical code to 0.0.10 — this is the 0.0.x line promoted to a round number now that the app knows what it is showing you and the last data-losing edge has been closed. Rolling up everything since 0.0.7: **account display names** (0.0.8) so a screenshot never exposes your `st_<steamid64>` or Xbox `<xuid>_<titleid>`; the two saves in each slot **named for what they are** — Auto-Save vs Restore-Point — plus a real **Difficulty** column, per-save **Xbox cloud sync state**, and hover tooltips throughout; **live saves and backups split into two panes** with sortable backup columns; a **Light / Dark / System theme**; an **opt-in update check** (0.0.9); and the fix for **restoring a single-slot extract deleting every other save** (0.0.10). |
| **0.0.10** | 2026-08-22 | **Fix: restoring a single-slot extract wiped every other save.** An extract is one slot lifted aside, but Restore ran it through the *full* restore path, which mirrors — so every live file not in the one-slot extract, including `accountdata.hg`, was deleted, and the folder was left holding only that slot. Restoring an extract now puts that slot back into **its own slot** and touches nothing else. The dangerous call is refused in the core, not merely avoided by the UI, so no front-end can reach it. The menu and the confirmation now say which of the two things will happen — a backup replaces the folder, an extract restores one slot — and a full restore states how many slots it holds and that anything not in it is removed. **Undo now explains itself:** it used to answer every failure with "no undoable operation found in the op log"; it now distinguishes nothing-done-yet, nothing-to-undo (Backup / Extract / Import only add to the vault and never change live saves), and a snapshot that has gone missing. |
| **0.0.9** | 2026-08-22 | **You can tell what you are looking at.** A slot's two saves are no longer "A" and "B": they are named for what the game actually uses them for — the periodic **Auto-Save** and the **Restore-Point** written when you leave your ship or use a save point / beacon / POI save. Either can be the newer one, and the app marks whichever the game will load. The **Mode** column, which printed `1` on every row because Waypoint retired that field, is now **Difficulty** and shows the real preset by name (Normal, Creative, Custom, Relaxed, Survival, Permadeath; pre-Waypoint saves fall back to the old game mode). Xbox saves report their **cloud sync state**; Steam has no per-save equivalent, so it is reported at folder level only. "Play" is now "Play Time". **Live saves and backups are now two separate panes** with a draggable divider — the backups pane has its own columns and every heading sorts (dates chronologically, slot counts numerically), newest-first by default. **Hover any row** for what it is, its difficulty, play time, where you were, its file, size, integrity and cloud state. Plus a **Light / Dark / System theme** that follows Windows, and an **opt-in update check** that asks first, then looks at GitHub once a day and shows a bar when a newer release exists — nothing is downloaded or installed, and it is the only network access in the app. |
| **0.0.8** | 2026-08-22 | **Account display names — hide your Steam / Xbox account id.** Save folders are named after the account that owns them, so `st_<steamid64>` and the Xbox `<xuid>_<titleid>` used to show up in folder names, paths, source labels and operation messages all over both front-ends — awkward for a screenshot, a bug report or a screen-share. Map each account to a name of your choosing (**Accounts…** in the GUI, `nmsvault accounts --set` in the CLI) and the app shows only that name, everywhere. Those two screens are the only ones that still show the real ids, since you cannot name an id you cannot see. The mapping is a plain `accounts.ini` beside `state.json`, safe to edit by hand; clearing a name shows the real id again. Display only — `state.json`, `catalog.json` and every path the app opens keep the real identifiers, so nothing about how saves are found or written changes. Upgrading keeps your config. |
| **0.0.7** | 2026-08-21 | **No more "unknown publisher" warning, and no installation needed.** The distributable is no longer a one-file PyInstaller build. It ships as a folder whose launcher is a verbatim renamed copy of the Authenticode-signed `pythonw.exe` published by the Python Software Foundation, so Windows starts a binary it already trusts instead of an unsigned custom `.exe` (renaming does not affect a signature — check it yourself with `Get-AuthenticodeSignature`). Extract the zip and run `NMSSaveVault.exe`; `install.bat` is now optional, for Desktop / Start Menu shortcuts. The command-line `nmsvault.exe` ships alongside it, so installed users get the CLI too. The build downloads the official runtime from python.org and refuses to package anything not validly signed, so there is no build dependency left at all. No change to what the app does; upgrading keeps your config. |
| **0.0.6** | 2026-07-01 | **Fix: extract a single slot from Xbox / Game Pass saves.** `extract_slot` only understood Steam filenames, so extracting a slot from a `wgs` folder always failed with "slot N has no saves to extract"; it now has an Xbox variant that copies the slot's blobs into a self-contained mini-`wgs` folder in the vault, so it catalogs and repopulates like any Xbox source. (Extract was the only operation missing an Xbox code path.) |
| **0.0.5** | 2026-07-01 | **Import a whole Save Vault directory.** Point Import at a copied `_SaveVault` folder and it compares that vault's entries with yours (by entry id) and offers to copy the new ones into your vault (self-contained) or index them in place (referencing that folder) — idempotent, so re-importing is harmless. In-place-imported entries are unmanaged, and snapshot pruning now only touches snapshots this vault owns, so importing another vault in place can never delete its files. Works in the GUI (Import shows new/existing counts, then copy vs in-place) and the CLI (`nmsvault import <vault-dir> [--copy]`). |
| **0.0.4** | 2026-07-01 | Installer-kit improvements (no changes to the app itself): added an uninstaller (`uninstall.bat`) that removes the app, its config (`state.json`), and the Desktop / Start Menu shortcuts while leaving your game saves and backups / vault untouched — no registry entries, no admin rights; `install.bat` drops it into the install folder so it's always available. The Desktop / Start Menu shortcuts now use the app icon directly (the `.ico` is installed on disk and referenced explicitly, instead of relying on the exe's embedded icon index). |
| **0.0.3** | 2026-07-01 | Reliability fixes: auto-snapshot pruning is now chronological, so it can no longer delete the snapshot that an `undo` needs (it previously grouped by platform when both Steam and Xbox were in use); the game-running check no longer flashes a console window on every action in the windowed build; and every write operation (not just backup) now shows a modal "please wait" dialog while it works. |
| **0.0.2** | 2026-06-30 | **Xbox / Game Pass saves are now read-write** for same-platform operations (backup, restore, per-slot repopulate, promote) — the wgs writer rotates blob GUIDs and rewrites `containers.index` with correct sync states, following the libNOM.io layout; every write auto-snapshots so `undo` works. Steam↔Xbox transfer remains gated ("coming soon"). |
| **0.0.1** | 2026-06-30 | First public release. Full backup / restore; catalog with per-slot extract / repopulate (meta re-keyed across slots) / promote; manual-backup import; multi-account auto-config (Steam live read-write, Xbox / Game Pass read-only); Tkinter GUI + `nmsvault` CLI; one-file Windows installer kit. Xbox→Steam transfer is gated ("coming soon"). |

Each version's installer kit is attached to its
[GitHub Release](https://github.com/GoodGuysFree/nms-save-vault/releases).

## License

This project is licensed under the **GNU General Public License v3.0** (see
[`LICENSE`](LICENSE)). It is GPL because parts of it are derived from GPL-3.0 code (see
Credits below); GPL-3.0's copyleft therefore applies to the whole work.

## Credits & attribution

The No Man's Sky save format was understood with the help of, and parts of this code are
derived from, the excellent open-source work of **Christian Engelhardt (zencq)**:

- **NomNom** — the most complete NMS save editor; the project that motivated this tool.
  <https://github.com/zencq/NomNom>
- **libNOM.io** — the .NET save read/write library NomNom is built on. Our meta
  encryption/decryption (`core/meta.py`) is a Python **port** of its
  `DecryptMetaStorageEntry` / `EncryptMeta`, and our Microsoft/Xbox reader
  (`core/msstore.py`) plus format constants (`core/formats.py`) follow its documented
  layout. <https://github.com/zencq/libNOM.io>
- **libNOM.map** — the JSON key (de)obfuscation mappings.
  <https://github.com/zencq/libNOM.map>
- Author: zencq — <https://github.com/zencq>

The save format itself (byte offsets, magic numbers, the XXTEA key derivation, the
slot model) is factual information; XXTEA/TEA is public domain. The reused **expression**
(the C# routines above) is what makes this a derivative work, hence the GPL-3.0 license.

The rest of the code — `lz4_block` (a from-spec implementation of the public LZ4 block
format by Yann Collet), `savedir`, `catalog`, `operations`, `safety`, `slotmap`,
`locations`, the CLI and the Tkinter GUI — is original to this project. No third-party
code is vendored, and there are no runtime dependencies (Python standard library only;
`pytest` is a dev-only tool).
