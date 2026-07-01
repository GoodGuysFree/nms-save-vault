# NMS Save Vault

Safe backup, catalog, and slot management for **No Man's Sky** (PC — Steam and Xbox /
Game Pass; GOG / Epic use the same format) save files — designed to give you effectively
unlimited save slots beyond the game's 15. See [Platform support](#platform-support).

## QuickStart

**Just want to run it? No Python needed.** Download the ready-to-use Windows kit from the
[**latest release**](https://github.com/GoodGuysFree/nms-save-vault/releases/latest) — grab
`NMSSaveVault-Setup-v0.0.5.zip` under **Assets**.

Extract the zip and run **`install.bat`**. Everything (Python + Tkinter) is bundled in the
single `.exe`; the installer offers Desktop / Start-Menu shortcuts. The exe is unsigned, so
Windows SmartScreen may prompt the first time (*More info → Run anyway*). Full details under
[Install](#install-windows-no-python-needed); every version is on the
[Releases](https://github.com/GoodGuysFree/nms-save-vault/releases) page.

## What it does

1. **Full backup / restore** — snapshot the entire live save folder, and restore any
   snapshot (always auto-backing-up the current state first).
2. **Catalog + per-slot operations** — browse every backup and the saves inside it by
   name/mode/play-time/date; lift a single slot aside; and repopulate any live slot from
   any save in any cataloged backup (re-keying the meta when the slot number differs).
   You can also inspect a slot's two saves (manual + auto restore-point) individually and
   force the older one to become the newest.
3. **Import** — register an existing manual backup folder into the catalog, or import an
   entire copied Save Vault folder: it compares that vault's entries with yours and offers
   to copy the new ones in or index them in place (idempotent — re-importing is harmless).

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

## Install (Windows, no Python needed)

Download **`NMSSaveVault-Setup-v0.0.5.zip`** from the
[**Releases**](https://github.com/GoodGuysFree/nms-save-vault/releases) page (under the
release's **Assets**), extract it, and run **`install.bat`**.
It copies the bundled app to `%LOCALAPPDATA%\Programs\NMSSaveVault` and asks whether to
add a Desktop shortcut and/or a Start Menu entry. If you decline both, it leaves a
`vault.bat` launcher in the install folder, opens that folder, and tells you to run it.
Everything (Python + Tkinter) is bundled in the single `.exe` — nothing else to install.
The exe is unsigned, so Windows SmartScreen may prompt the first time (*More info → Run
anyway*).

To remove it, run **`uninstall.bat`** (placed in the install folder, and also in the zip):
it deletes the app, its config, and the shortcuts, leaving your game saves and
backups / vault untouched. No registry entries or admin rights are involved either way.

### Building the distributable yourself

```pwsh
pwsh -ExecutionPolicy Bypass -File packaging\build_exe.ps1          # -> dist\NMSSaveVault.exe
pwsh -ExecutionPolicy Bypass -File packaging\make_installer_zip.ps1 # -> dist\NMSSaveVault-Setup.zip
```

Both use an isolated `.build-venv` (via `uv`) so the dev environment is untouched.

## Requirements

* Python 3.10+ (developed on 3.12), standard library only — no runtime dependencies.
* [uv](https://github.com/astral-sh/uv) for environment management (dev / building the exe).

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
* Each Xbox / Game Pass `wgs` account folder is a **live** source, **read-only**.
* Any *other* save folder under the NMS root — a hand-pasted `st_… - Copy`, a renamed or
  dated folder — is treated as an **in-place backup**, not a live target.

The GUI shows the two groups separately (**● LIVE SAVES** vs **■ BACKUPS**), highlights the
active write target, and badges Xbox folders read-only. Use **Rescan** (GUI) or
`nmsvault sources --rescan` (CLI) to pick up a new account or backup later; your manual
edits to the config are preserved. Discovery is strictly read-only.

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
in a temp sandbox (78 tests). Xbox / Game Pass saves are supported for reading **and**
same-platform writing — verified against a real install (reads) and synthetic `wgs` fixtures
(writes). A full file-copy safety backup of the live folder was made before development
(`C:\Devel\NMS-SaveBackup-SAFETY-2026-06-24`).

## Version history

| Version | Date | Highlights |
|---|---|---|
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
