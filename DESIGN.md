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
* 15 slots × 2 saves = 30 files. The two members have **fixed, non-interchangeable roles**:
  member A (`f = 2k-1`) is the periodic **auto-save**, member B (`f = 2k`) is the
  **restore point** the game writes when you leave your ship or use a save point / beacon /
  POI save. Hello Games separated them so neither overwrites the other, so **either can be
  the newer one**. libNOM.io derives the same thing positionally —
  `SaveType = (SaveTypeEnum)(CollectionIndex % 2)`, `Identifier = $"Slot{n}{SaveType}"` —
  which is why the Xbox containers are literally named `Slot<N>Auto` / `Slot<N>Manual`.
  The game treats whichever has the **newer meta timestamp** as current.

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
    safety.py     game-running check, atomic staged writes, auto-snapshot, op-log/undo, inter-process lock
    aliases.py    account-id -> display-name filter (accounts.ini); display only
    organizer/    save-file organizer (docs/organizer/designs/D1-core-model.md)
      model.py    ids, value types and their JSON codecs (pure)
      store.py    <vault>/organizer/: header, documents, journal, store lock, read-only mode
      cache.py    per-machine scan cache (hashes, entry captures, data ids); never in the vault
      scanner.py  captures of every live slot and vault entry; version and pair registry
      journal.py  live journal: observe (obs records), replay -> edges, events, op outcomes
      recorder.py op records; the phase-1 hook: classic live writes under the live-op lock
      bootstrap.py history from before recording: backups (seen in backups) + old oplog
      notes.py    save-file notes with every revision, pins, version labels/comments/locks
      lineage.py  lineage.json: owner corrections, Combine aliases, the bootstrap record
      readmodel.py lines, version rows, live board, events, sort/filter; JSON with tokens
      locator.py  where a version's bytes are (VersionLocator, VaultLocator, Location)
      folders.py  FolderLocator: the read side over live and catalog folders
      __init__.py Organizer facade: scan, record, build the model, bootstrap
  ui/             Tk building blocks for the views (docs/organizer/designs/D6-ui-tk.md)
    dpi.py        DPI awareness, px() for every pixel constant, first-start size, dialog fit
  theme.py        light/dark/system palettes (incl. the organizer keys) + ttk restyling,
                  dark title bar (front-end)
  updates.py      opt-in GitHub release check + in-place self-update (the only network access)
  cli.py          argparse front end
  gui.py          Tkinter app (two panes: live saves / backups)
```

## Storage model (hybrid)
* Existing backups (hand-made in-place backup folders, "Copy before…") are catalogued **in place**.
* New app backups / auto-snapshots / single-slot extracts go in a **managed vault**
  (default: a sibling of `st_…`), recorded in a JSON catalog.

## Safety invariants
1. Never write while `NMS.exe` runs.
2. Auto-snapshot live before every destructive op; every snapshot is kept.
3. Atomic: stage to temp → validate → swap.
4. Validate: meta header + `SizeDisk == file size` + `SizeDecompressed == Σ chunk sizes`
   + sha256 on copies + re-key round-trip.
5. Dry-run preview; operation log with one-click undo.
6. Steam Cloud: operate with game closed; vault lives outside `st_…`. Live writes are
   stamped with the write time under Cloud, so Steam never judges its copy the newer one;
   deletions cannot be propagated at all and the clear plan says how to do it by hand.
7. A restore (and so undo) keeps the live account's own identity: `steam_autocloud.vdf` is
   never copied, an Xbox backup of another account is refused, and the account data
   (unlocks, settings) stays unless the user asks to roll it back (or there is none live).
   A restored Xbox record is a fresh local write: its index time is now, never older.

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
  Writes edit it at the record level: records an operation does not change (and any bytes
  after the last record) are written back byte for byte, and FILETIMEs stay integers (a
  float of seconds cannot hold 100 ns ticks; rebuilding from floats moved other saves' times
  and perturbed their sync). A changed record's FILETIME is its local write time, now, and
  never moves back from its own or its slot sibling's; the header time never moves back.
* Each save's GUID folder holds `container.<n>` (328 bytes: header, blob count, then per
  blob a 128-byte UTF-16 identifier + cloud GUID + **local GUID**) and the blob files,
  named by `GUID.ToString("N").ToUpper()` (= Python `uuid.UUID(bytes_le=...).hex.upper()`).
* Save identifiers map onto the shared model: `Slot{N}Auto`→ member A, `Slot{N}Manual`→
  member B; `AccountData`/`Settings` are not slots.
* **Data blob**: current saves (Worlds 5.0+) use the *same* `0xFEEDA1E5` stream as Steam
  (handled by `lz4_block`); the oldest are one bare LZ4 block (also decoded). Game versions
  4.52-4.99 use an `HGSAVEV2` container, which is recognised but **not** decoded: the
  decoder refuses it as unsupported rather than misread it.
* **Meta blob**: plaintext (NOT XXTEA), 280 B (Waypoint), 296 B (Worlds I) or 360 B
  (Worlds II) — not the Steam lengths. MS-specific leading fields, then SaveName/Summary as
  **UTF-8** (offsets 0x14 / 0x94) and the difficulty at 0x114 (one byte at 280 B, u32 from
  296 B). From 296 B: playthrough id (u64 0x118), save time (u32 0x120), meta format (u32
  0x124); 360 B adds a 64-byte difficulty tag at 0x128. The `containers.index` FILETIME is
  only the local write or sync time (used as `data_mtime`). Newest member: the later meta
  save time when both have one, else the later FILETIME for both — never one clock against
  the other; promote moves both clocks. Verified 2026-10-17 on a real install: all 30 metas
  (22 of 360 B, 8 of 296 B) carry a non-zero id and time, and the id equals the save data's
  own in all 28 saves whose data holds one.

Verified read-only against a real Game Pass install (15 slots decoded with correct names,
play times, summaries and dates) and a synthetic fixture in `tests/test_msstore.py`.

### The `xgs` mirror

Since 2026 the game also keeps `SystemAppData\xgs\<account>\<identifier>\{data,meta}` beside
`wgs` — no index, no GUIDs, plain names. Verified 2026-10-02 on a real install: all 62 `xgs`
files were byte-identical to a `wgs` blob, and the game wrote `AccountData` to `xgs` on launch.
Which copy the game *loads* from is not yet proven, so:

* Every `wgs` write (`write_save` / `delete_save`) is mirrored into `xgs` when that folder
  exists; a full restore (and so `undo`) re-syncs it with `mirror_xgs`. Slot folders with no
  live `wgs` save are removed; `AccountData` and unknown folders are never deleted.
* An account with `xgs` but no `wgs` is scanned by `scan_xgs` and is read-only: it has no
  `containers.index` to carry the Xbox app's sync state. Times come from the meta, else the
  file mtime.

## Cross-platform support (Linux and macOS)

**Status: decided, not yet implemented.** The save *format* work is zero — every platform
writes the identical Steam-format `save*.hg` / `mf_save*.hg` pair into an `st_<steamid64>`
folder, so `formats`, `meta`, `lz4_block`, `slotmap`, `savedir`, `catalog` and `operations`
are already platform-neutral. Measured: with `APPDATA`, `LOCALAPPDATA`, `USERPROFILE` and
`NMSVAULT_PORTABLE_ROOT` cleared the suite is 235 passed / 31 skipped / 0 failed, and all 31
skips share one cause (`tests/conftest.py` hardcodes `APPDATA`).

### Save roots per platform

| Platform | Root |
|---|---|
| Windows (Steam) | `%APPDATA%\HelloGames\NMS\st_<steamid64>\` |
| Windows (Xbox) | `%LOCALAPPDATA%\Packages\HelloGames.NoMansSky_bs190hzg1sesy\SystemAppData\wgs\` |
| macOS (native) | `~/Library/Application Support/HelloGames/NMS/st_<steamid64>/` |
| Linux (Proton) | `<steam-library>/steamapps/compatdata/275850/pfx/drive_c/users/steamuser/AppData/Roaming/HelloGames/NMS/st_<steamid64>/` |

Linux notes: there is no native NMS build, only Proton. Older/other Wine prefixes use
`users/steamuser/Application Data/…` — glob both. Steam roots to probe: `~/.steam/steam`,
`~/.steam/root`, `~/.local/share/Steam`, `~/.var/app/com.valvesoftware.Steam/data/Steam`
(Flatpak), `~/snap/steam/common/.local/share/Steam` — several symlink to each other, so
dedupe by `Path.resolve()`. Extra libraries (second drive, Steam Deck SD card) are listed in
`<steam-root>/steamapps/libraryfolders.vdf` and must be parsed. macOS App Store builds are
sandboxed and may sit under `~/Library/Containers/<bundle-id>/Data/…` — probe by glob, never
hardcode a bundle id.

### Decisions

| # | Decision |
|---|---|
| D1 | Discovery becomes multi-root: `nms_roots() -> list[Path]`, deduped by `Path.resolve()`. `nms_root()` survives only as "the primary root" for the Windows vault default. |
| D2 | The vault never lives inside a Proton prefix (Steam can delete and recreate one). Linux defaults to `$XDG_DATA_HOME/NMSSaveVault/vault`, macOS to `~/Library/Application Support/NMSSaveVault/Vault`; Windows is unchanged. |
| D3 | Xbox / Game Pass stays Windows-only. Gate `msstore` on `os.name == "nt"` explicitly, not on `LOCALAPPDATA` happening to be unset. |
| D4 | Self-update stays Windows-only **for now**; the update *check* ships everywhere. Wanted on all three eventually, so POSIX kits must keep the Windows layout's shape (launcher + `_runtime/app` beside it, `NMSVAULT_PORTABLE_ROOT` exported) and `updates.portable_root()` then works unchanged. On POSIX the swap is easier — you can rename over a running binary. |
| D5 | `is_game_running()` may never return a wrong `False`; a false negative would let a write proceed into a live save folder. The `True`/`False`/`None` contract, `None` meaning warn, is what keeps the safety guarantee. |
| D6 | The runtime stays standard-library only: a hand-rolled `libraryfolders.vdf` reader, `subprocess` for `pgrep`, `plistlib` for the macOS appearance setting. No `psutil`, no `vdf`. Dev-only tooling under `tools/` may use anything. |
| D7 | **Every resolver takes its environment as an argument.** No function reads `os.environ` or `Path.home()` at the point of use; both arrive as parameters with real defaults. This is what lets macOS and Linux path logic be tested from a Windows desk. |
| D8 | macOS game detection returns `True` or `None`, never `False`, until a real Mac confirms the process name. A match is trustworthy; a non-match is "unknown, warn". |
| D9 | macOS ships as "should work — untested", in the same words the README already uses for GOG and Epic. |
| D10 | Linux v1 ships CLI and GUI together. Tripwire: if the screenshot loop (below, T3) does not work on the real box within its first session, split the GUI to v2 rather than spend console time on it. |
| D11 | `catalog.json` stores **managed** entries' paths relative to the vault root, so a vault on a USB drive or NAS opens in place from either OS. In-place entries stay absolute — they point outside the vault and are genuinely machine-specific — and surface as missing elsewhere. Needs a catalog version bump and migration. |
| D12 | GOG / Epic `DefaultUser` discovery is in scope, folded into the multi-root loop. |
| D13 | **No PyPI — GitHub Releases only.** Consequence: the self-contained kits are the *only* way anyone on Linux or macOS gets the app, so packaging is release-blocking rather than optional polish, and a source checkout is the interim. |

### Verification ladder

Push every check as far down this ladder as it will go; only the last two need the Linux box.

| Tier | Where | Covers |
|---|---|---|
| T0 | A Windows desk, injected env + synthetic home trees | All path resolution for all three platforms, multi-root dedupe, `libraryfolders.vdf` parsing, config/vault locations, catalog portability |
| T1 | GitHub Actions (`ubuntu`/`macos`/`windows-latest`) | Imports and runs on a real kernel, `tkinter` loads, GUI-logic suite, packaging build, UI screenshots as artifacts |
| T2 | SSH to a Linux box, no display | Real Proton prefix, real `libraryfolders.vdf`, full backup → restore → repopulate → undo on real saves |
| T3 | SSH + `xvfb-run` and a small WM | The GUI under a real X server with the distro's fonts and ttk theme, captured as PNGs |
| T4 | Physically at the console | Only: launching NMS so detection has something to detect, and the real desktop's dark-mode setting |

T4 collapses to zero trips if the box has `sshd`, auto-login, and an **X11** (not Wayland)
session — `DISPLAY=:0` over SSH then reaches the real desktop.

### The UI is ASCII-only

The Linux kit bundles python-build-standalone's Tcl/Tk, which is built **without Xft,
fontconfig or Xrender** — `libtk8.6.so` references none of them and falls back to legacy
X11 core fonts advertising ISO8859-1. Those fonts cannot draw anything outside that
charset, so em dashes, ellipses and the `●` / `■` pane markers came out as garbage on a
real Linux desktop (reported against v0.1.2 as
`(nothing chosen yet <garbage> press Change<garbage>)`).

So every string the app can draw is plain ASCII: `-` for dashes, `...` for ellipses, `*`
and `#` for the pane markers, `^` / `v` for sort arrows. `tests/test_ui_text.py` enforces
it across `src/`; docstrings and comments are exempt because they are never drawn. That
test parses with `ast` rather than `tokenize` on purpose — since PEP 701 an f-string is no
longer a single `STRING` token, so a token-level scan silently misses every f-string.

A Tk with Xft would render the original characters correctly, but the kit cannot rely on
one being present, and ASCII costs almost nothing.

### Deliberately unchanged

Xbox / Game Pass (no `wgs` store exists off Windows) and in-place self-update (see D4).
`updates.can_install()` already refuses off Windows with a readable reason, and the update
check itself is plain `urllib` that works everywhere.

## Manual safety backup
A full file-copy of the live folder was taken before development began:
`C:\Devel\NMS-SaveBackup-SAFETY-2026-06-24\` (215 files, verified byte-count match).
