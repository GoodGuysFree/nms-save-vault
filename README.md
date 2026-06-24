# NMS Save Vault

Safe backup, catalog, and slot management for **No Man's Sky** (PC / Steam) save files —
designed to give you effectively unlimited save slots beyond the game's 15.

## What it does

1. **Full backup / restore** — snapshot the entire live save folder, and restore any
   snapshot (always auto-backing-up the current state first).
2. **Catalog + per-slot operations** — browse every backup and the saves inside it by
   name/mode/play-time/date; lift a single slot aside; and repopulate any live slot from
   any save in any cataloged backup (re-keying the meta when the slot number differs).
   You can also inspect a slot's two saves (manual + auto restore-point) individually and
   force the older one to become the newest.
3. **Import** — register an existing manual backup folder into the catalog.

## Why it's safe

* The save **data** file is copied **verbatim** — never recompressed — so it can't drift.
* Only the small 432-byte **meta** is transformed (XXTEA re-encryption / timestamp edit).
* Every destructive operation auto-snapshots first, writes atomically (stage → validate →
  swap), validates by header + size cross-checks + hashes, and refuses to run while the
  game is open.

See [DESIGN.md](DESIGN.md) for the architecture and the verified save-format details.

## Requirements

* Python 3.10+ (developed on 3.12), standard library only — no runtime dependencies.
* [uv](https://github.com/astral-sh/uv) for environment management (dev).

## Dev setup

```pwsh
uv venv
uv pip install -e ".[dev]"
uv run pytest
```

## Usage

Both front-ends share the same safety-checked core. The live folder and a vault folder
(default: a `_SaveVault` sibling of `st_<id>`) are auto-detected; override with
`--live`/`--vault`.

GUI:

```pwsh
nmsvault-gui        # or: python -m nms_save_vault.gui
```

CLI:

```pwsh
nmsvault status                          # live folder + 15 slots, both saves each
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

## Recommended workflow (Steam Cloud)

No Man's Sky uses Steam Cloud, which syncs the `st_<id>` folder. To avoid cloud conflicts:

1. **Fully close the game** before any write operation (the app blocks writes while
   `NMS.exe` is running).
2. Make your changes (restore / repopulate / promote).
3. Launch the game. If Steam shows a cloud conflict, choose the **local** copy.

The vault lives outside `st_<id>`, so it is never scanned by the game or synced by Steam.

## Status

Working. Core format/crypto and all operations verified against the real save files and
in a temp sandbox (34 tests). A full file-copy safety backup of the live folder was made
before development (`C:\Devel\NMS-SaveBackup-SAFETY-2026-06-24`).
