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

## Status

Early development. Core format/crypto verified against real save files.
