# NMS Save Vault — design

## Verified save-format facts (the foundation)

Confirmed against the reference library `libNOM.io` **and** empirically by decrypting /
decompressing the user's actual files.

### Files per save folder (`%APPDATA%\HelloGames\NMS\st_<steamid>\`)
* `saveN.hg` — save **data**: a stream of chunks `[magic 0xFEEDA1E5][compressed u32]
  [decompressed u32][0]` + LZ4-block payload; chunks decompress to ≤ `0x80000` and
  concatenate into obfuscated JSON. **Compression only — never encrypted → portable.**
* `mf_saveN.hg` — **meta**: fixed 432 bytes (current "Worlds II" format), **XXTEA-encrypted**.
* `accountdata.hg` / `mf_accountdata.hg` — account-level (not a slot).
* `cache/*.DDS` thumbnails, `steam_autocloud.vdf`.

### Slot ↔ file ↔ ordinal
* File number `f` (bare `save.hg` = 1 … `save30.hg` = 30).
* **Slot k** (1-based) = files `f = 2k-1` (member A) and `2k` (member B).
* **Storage ordinal** (XXTEA key input) = `f + 1` (`save.hg`→2 … `save30.hg`→31).
* 15 slots × 2 saves = 30 files. The two members are the manual save + the auto
  restore-point; the game treats the one with the **newer meta timestamp** as current.

### Meta (decrypted) layout — Steam, Worlds
| Offset | Field |
|---|---|
| 0x00 | header `0xEEEEEEBE` (validates key) |
| 0x04 | meta format (`0x7D4` = 2004 / v5.5) |
| 0x38 | SizeDecompressed (u32) |
| 0x3C | SizeDisk (u32) |
| 0x44 | BaseVersion (u32) |
| 0x48 / 0x4A | GameMode / Season (u16) |
| 0x4C | TotalPlayTime (u64) |
| 0x58 | SaveName (128B, NUL-term) |
| 0xD8 | SaveSummary (128B) |
| 0x158 | Difficulty (u32) |
| 0x15C | Slot identifier (8B) |
| 0x164 | **Timestamp** (u32 unix) — load-screen "newest" key |

> Note: `SizeDisk` (0x3C) is **not reliable** — it can be stale after a save editor
> recompresses the data without updating it (observed on `save6.hg`, where the file is
> 619,192 B but the meta records 523,713 B). The data file is a self-describing chunk
> container, so `SizeDisk` is effectively vestigial. The dependable data↔meta tie is
> `SizeDecompressed` (0x38) == the sum of chunk decompressed sizes; validation uses that.

### Meta XXTEA key
`key = [ ((ordinal ^ 0x1422CB8C) <<<13) * 5 + 0xE6546B64,  K1, K2, K3 ]`
where `K1..K3` come from ASCII `"NAESEVADNAYRTNRG"`. 6 rounds for Waypoint/Worlds.
The first key word depends on the slot ordinal ⇒ **the meta is slot-bound**.

## Core consequences for operations
* **Re-slot a save** = copy the data file verbatim + (if slot differs) decrypt the meta
  with the source ordinal and re-encrypt with the destination ordinal. No recompression.
* **Force older→newest** within a slot = decrypt that member's meta, bump `Timestamp`
  (0x164) above its sibling, re-encrypt with the **same** ordinal, set file mtimes. Data
  untouched.

## Architecture
```
src/nms_save_vault/
  core/
    formats.py    constants + meta layout offsets        [done]
    slotmap.py    slot<->file<->ordinal mapping           [done]
    meta.py       XXTEA decrypt/encrypt; field accessors
    lz4_block.py  pure-python LZ4 block decode (deep verify)
    savedir.py    enumerate a folder's slots/members, decode metas
    catalog.py    JSON index of backups; scan/import; dedup by sha256
    operations.py backup/restore/extract/repopulate/promote
    safety.py     game-running check, atomic staged writes, auto-snapshot, op-log/undo
  cli.py          argparse front end
  gui.py          Tkinter app
```

## Storage model (hybrid)
* Existing backups (`Glamdring`, `Anduril`, "Copy before…") are catalogued **in place**.
* New app backups / auto-snapshots / single-slot extracts go in a **managed vault**
  (default: a sibling of `st_…`), recorded in a JSON catalog.

## Safety invariants
1. Never write while `NMS.exe` runs.
2. Auto-snapshot live before every destructive op (keep-last-N).
3. Atomic: stage to temp → validate → swap.
4. Validate: meta header + `SizeDisk == file size` + `SizeDecompressed == Σ chunk sizes`
   + sha256 on copies + re-key round-trip.
5. Dry-run preview; operation log with one-click undo.
6. Steam Cloud: operate with game closed; vault lives outside `st_…`.

## Verification strategy
* **Read-only** unit tests against the real backups (decode every meta → header + ordinal
  + size cross-checks; catalog reproduces known slot names; re-key round-trip A→B→A).
* **Sandbox** integration tests: copy the live folder to a temp dir and exercise all
  write paths there — the real `st_…` is never touched during development.

## Microsoft / Xbox Game Pass ("wgs") format (read-only)

Root: `%LOCALAPPDATA%\Packages\HelloGames.NoMansSky_bs190hzg1sesy\SystemAppData\wgs\<account>\`.
Implemented in `core/msstore.py`, exposed via `savedir.scan_any()`.

* `containers.index` — header `0xE`, little-endian. Length-prefixed **UTF-16** strings
  (process id, account id, save identifiers), an `0x10000000` footer, then one record per
  save: identifier, sync state, a directory **GUID**, last-write (FILETIME) and size.
* Each save's GUID folder holds `container.<n>` (328 bytes: header, blob count, then per
  blob a 128-byte UTF-16 identifier + cloud GUID + **local GUID**) and the blob files,
  named by `GUID.ToString("N").ToUpper()` (= Python `uuid.UUID(bytes_le=...).hex.upper()`).
* Save identifiers map onto the shared model: `Slot{N}Auto`→ member A, `Slot{N}Manual`→
  member B; `AccountData`/`Settings` are not slots.
* **Data blob**: current saves (Worlds 5.0+) use the *same* `0xFEEDA1E5` stream as Steam
  (handled by `lz4_block`); older ones use a single LZ4 block or an `HGSAVEV2` chunk format.
* **Meta blob**: plaintext (NOT XXTEA), MS-specific leading fields then SaveName/Summary as
  **UTF-8** (offsets 0x14 / 0x94). No timestamp is stored in the meta — the save time comes
  from the blob's `containers.index` entry (used as `data_mtime`).

Verified read-only against a real Game Pass install (15 slots decoded with correct names,
play times, summaries and dates) and a synthetic fixture in `tests/test_msstore.py`.

## Manual safety backup
A full file-copy of the live folder was taken before development began:
`C:\Devel\NMS-SaveBackup-SAFETY-2026-06-24\` (215 files, verified byte-count match).
