# D2 - Storage option A: index over folders

**Not chosen - superseded by D3 (owner decision F3-B).** The owner chose the version store
(`D3-storage-version-store.md`; `01-consolidation.md` 4a and the owner answers to
`03-design-review.md`). This document keeps only the three pieces other designs reuse:

| Section | Piece | Used by | Phase |
|---|---|---|---|
| 2 | Stop automatic snapshot pruning (P2) | 01 phase 0; D3 relies on it | 0 |
| 3 | `FolderLocator`, the read-only legacy-folder reader, and its durability classes | D1 (phase 1); D3 behind `StoreLocator` until old folders are freed | 1-5 |
| 4 | `Vault.entry_folder()`: resolving entry folders of a moved vault | D1's scanner, `FolderLocator`, D3's fold and `open_entry_folder` | 1 |

Everything else option A designed (preserve as `keep-*` extract folders, retention and rescue,
manifests and intents in `storage.json`, housekeeping, cache compaction, repair-paths, deletion
rules, the old-app compatibility work) is not built and was removed from this document; it is in
git history before 2026-10-17. Author: D2 subagent, 2026-10-16; cut to the reused parts by the
update pass, 2026-10-17. No production code, tests or packaging were changed.

Evidence tags: **VERIFIED** = run by D2 (script named, under `<D2>` =
`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\<session>\scratchpad\D2\`); **SOURCED** = code
`file:line` or a research/design report; **INFERRED** = reasoning from those. The owner's vault and
live folders were only read.

## 1. Option A in one paragraph (historical)

Option A kept a folder per backup, added a version index over the folders, preserved evicted
saves as single-slot extract folders, pruned snapshots version-aware with rescue, and stopped
copying the thumbnail `cache/`. On the owner's habit it grew ~4.5 GB a year against ~0.9 GB for the
version store (D3 2.15). The owner chose the store; the parts of A that survive are below.

## 2. Phase 0: stop automatic snapshot pruning (P2)

**Defect.** `snapshot_live` calls `_prune_snapshots` (`operations.py:144`), which deletes the
oldest managed snapshots beyond `SNAPSHOT_RETENTION = 20` by `created`, across platforms, whole
folders (`operations.py:31`, `148-159`). In the owner's vault 9 versions exist only in snapshots
(VERIFIED, `<D2>\budget.py` over R7's inventory: 6 held by one snapshot, 3 by two or more), so
future operations will delete their only copy.

**Fix** (F9 keep everything; 03 V-10):

- Remove the `_prune_snapshots(vault)` call from `snapshot_live`; delete `_prune_snapshots` and
  `SNAPSHOT_RETENTION`. After this no code deletes a catalog entry or its folder: the only
  `vault.remove` and entry-folder `rmtree` in `src/` are in `_prune_snapshots` (SOURCED, search of
  `src/nms_save_vault`; `operations.py:297` removes only the empty folder of an extract that found
  no saves).
- Tests: replace `test_prune_leaves_unmanaged_snapshots_untouched` and
  `test_prune_snapshots_is_chronological_across_platforms` (`tests/test_operations.py:236-284`)
  with `test_snapshot_live_never_deletes_snapshots`: 25 snapshots over a Steam and an Xbox
  sandbox folder -> all 25 are catalogued and on disk.
- Docs in the same commit: DESIGN.md line 89 ("Auto-snapshot live before every destructive op
  (keep-last-N)") -> keep every snapshot; README's test count.
- Cost until phase 2: every pre-op snapshot stays, Steam 41-52 MB, Xbox 63 MB each (R1 A6). From
  phase 2 snapshots are store entries (median 0 new bytes, D3 2.15) and the fold deduplicates the
  old ones; phase 5 frees the old folders on request.

Size S, one commit, no dependency on D1-D5.

## 3. The legacy-folder reader

`FolderLocator` is the read side of D1's `VersionLocator` over catalog entry folders and live
folders. D1 uses it in phase 1; from phase 2 it is the second reader of D1's `VaultLocator`, behind
D3's `StoreLocator`, for entries that are not in the store (fold failed or pending, hand-made
in-place folders, references into another vault) and for live. It has no `preserve`: preserving
is the store's (D3 2.8).

### 3.1 Durability classes

| Copy | `Location.kind` | `durable` | `external` | Why |
|---|---|---|---|---|
| Live member (Steam `st_<steamid64>`, Xbox `wgs`) | live | no | no | The game overwrites it. The `xgs` mirror is ignored (`wgs` is authoritative) |
| Catalog entry whose resolved folder equals a live folder or lies inside a live root (P9: `inplace-20260624-182102` = the live `wgs` folder) | not reported | - | - | A live alias, not a copy; D3's listing and D1's scanner already drop it |
| Managed entry of any kind (`full`, `snapshot`, `extract`, `imported`) at `<vault>/<subdir>/<id>` whose data file hashes to the vid | entry | **yes** | no | App-created and inside the vault; after section 2 nothing deletes it. Folded entries are listed and located by D3 instead (same id) |
| Hand-made in-place backup folder outside the vault | entry | no | yes | The owner's own folder: the app does not control its lifecycle (the owner, editors and other tools may change or delete it; the catalog's summaries are frozen at creation, R1 A5), its path is machine-specific (D11), and nothing hash-verified it when it was catalogued. Shown "outside the vault" until copied in (D3 4.3) |
| Unmanaged reference into another vault (`import_vault` in place) | entry | no | yes | The other vault owns it |
| Managed entry whose file is missing or no longer hashes to the scanned vid | entry | no | no | Damaged: `open` skips it and it is reported, never counted |

Owner's numbers (VERIFIED, `<D2>\budget.py` over R7's inventory): of 195 versions, 154 have a
durable vault copy once section 2 has landed (145 in full / extract / imported entries plus the 9
only in snapshots), 25 exist only in hand-made in-place folders (some also live) and 16 only live.

### 3.2 Interface

```python
# core/organizer/folders.py
@dataclass(frozen=True)
class LiveSourceInfo:
    """How the reader sees a live folder. Built by D1 from state.json + discovery (DESIGN.md D7:
    injected, never read from the environment here)."""
    platform: Literal["steam", "xbox"]
    account_key: str         # live folder name
    folder: Path
    roots: tuple[Path, ...]  # folders whose subtree is "live" (the wgs and xgs account folders)


class FolderLocator(VersionLocator):
    def __init__(self, vault_root: Path,
                 live_sources: Callable[[], Sequence[LiveSourceInfo]], *,
                 hash_file: Callable[[Path], str] = safety.sha256_file) -> None: ...

    def refresh(self, scan: "ScanResult") -> None:
        """Rebuild the in-memory index from D1's latest ScanResult. No I/O beyond reading
        catalog.json; cheap enough to run on every scan."""

    def locate(self, vid: str) -> list[Location]:
        """Durable entry copies, then external, then live (D1's order)."""

    def is_durable(self, vid: str) -> bool:
        """True when at least one durable Location exists (3.1)."""

    def open(self, vid: str, prefer: Location | None = None) -> OpenedVersion:
        """Read one copy's bytes. Order: prefer (if it still holds vid), then locate() order.
        Each candidate's data is re-hashed; a mismatch skips it and is reported as damaged.
        Steam meta: decrypt with the ordinal of the data file number (file + 1); if that fails,
        meta.decrypt_autodetect (moved copies). Xbox meta: plaintext as stored.
        Raises VersionUnavailable(vid) when no copy verifies."""

    def preserve(self, members, *, op_id, reason):
        raise PreserveFailed("store-unavailable")      # the store preserves (D3 2.8)
```

`refresh` algorithm:

1. Take the catalog entries D1's scan used (phase 1: `vault.entries`; from phase 2: the entries of
   D3's `list_vault_entries` whose `ref` is an `EntryRef`, so folded entries are already
   excluded) and resolve each folder with `Vault.entry_folder()` (section 4).
2. Classify each entry with 3.1: `is_live_alias(resolved)` = `os.path.normcase(os.path.realpath(p))`
   equals a live folder or starts with one of its `roots` (checked again so that nothing here can
   ever treat a live folder as a vault copy); managed and inside the vault root -> durable;
   otherwise external.
3. Map each capture in `scan.captures` to D1's `Location(vid, kind, ref, slot, member, platform,
   durable, external)`.
4. Index: `by_vid: dict[str, list[Location]]` and a `generation` counter bumped on every refresh.

Cost: ~1,200 member copies for the owner's vault (R7 2.2), a dict pass of a few milliseconds
(INFERRED). Hashing is D1's per-machine scan cache (cold 1.2-2.7 s for every copy, R7 2.9 and
R1 2h; warm: stat checks only). `open` re-hashes about 1 ms per MB (R7 2.9); the largest data file
is 6.9 MB (VERIFIED, `budget.py`).

## 4. `Vault.entry_folder()`: entry folders of a moved vault

`catalog.json` v1 stores absolute paths. A vault moved to another folder, drive or machine (D11)
has stale paths for its managed entries. Instead of changing the catalog, the catalog API resolves
managed entries by convention:

```python
# core/catalog.py (platform-neutral; phase 1)
SUBDIR = {KIND_SNAPSHOT: "snapshots", KIND_EXTRACT: "extracts"}   # else "backups"

class Vault:
    def entry_folder(self, entry: CatalogEntry) -> Path | None:
        """Where an entry's files are on this machine. Managed entries resolve by convention
        when their stored path is stale (vault moved, other machine, other OS); in-place
        entries stay absolute per D11. None when the folder is missing."""
        stored = Path(entry.path)
        if entry.managed:
            if stored.is_absolute() and stored.is_dir() and _is_within(stored, self.root):
                return stored
            conventional = self.root / SUBDIR.get(entry.kind, "backups") / entry.id
            if conventional.is_dir():
                return conventional
        return stored if stored.is_dir() else None   # managed outside the vault -> external
```

`_is_within` compares `os.path.normcase(os.path.realpath(...))` prefixes on path-part boundaries.
VERIFIED on the owner's catalog (`<D2>\catalog_layout.py`, read-only): all 41 managed entries sit
exactly at `<vault>/<subdir>/<id>`, the rule `_entry_folder` uses today (`operations.py:845-850`).
D1's scanner, `FolderLocator`, D3's fold and D3's `open_entry_folder` all resolve legacy folders
through this one call; the classic operations switch from `entry.path` to it in the same step.
Hand-made in-place entries keep their absolute path and show as missing on another machine.

## 5. Tests and implementation steps

| Step | Phase | Size | Content | Tests |
|---|---|---|---|---|
| A-0 | 0 | S | section 2 (P2 fix) + DESIGN.md | `test_snapshot_live_never_deletes_snapshots` |
| A-1 | 1 | S | `Vault.entry_folder()`; classic operations use it | `test_rehome_after_vault_move`: a moved sandbox vault resolves every managed entry; in-place entries unchanged |
| A-2 | 1 | M | `LiveSourceInfo`, `FolderLocator` (`refresh`, `locate`, `is_durable`, `open`, `preserve` raising) | D1's locator conformance suite (read part); `test_classification` (live, live alias, managed of each kind including snapshots, hand-made in-place, other-vault reference, managed outside the vault, damaged); `open` from durable, external and live, skipping a copy whose re-hash fails |

All write tests run on `tmp_path` copies or synthetic saves (project rule); fixtures are D1's and
D3's (`steam_pair` / `steam_live`, `wgs_account`, `legacy_vault`).

## Deviations from 01-consolidation section 6

- **D2 no longer implements `preserve`, pruning or the `cache/` policy.** Section 6 gives them to
  "D2 / D3"; D3 owns preserve and `cache/` (kept, deduplicated), and F9 means nothing is pruned.
- **D11 without a catalog change.** Managed entries resolve by convention through
  `Vault.entry_folder()` (section 4).
