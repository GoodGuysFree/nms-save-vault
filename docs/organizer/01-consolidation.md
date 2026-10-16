# Save-file organizer - consolidation of research

Status: research complete (R1-R8, `research/`). This document fixes the decisions the design
wave builds on, lists the owner forks the designs must present as options, and sets the order
of work. Audience: the design agents and the parent. The owner gets a short summary separately.

## 1. Ground facts (numbers from the owner's real data)

| Fact | Value | Source |
|---|---|---|
| Save copies across vault + live | 1,237 member files (1,177 without the `xgs` mirror and the in-place entry that is the live `wgs` folder) | R7 2.2 |
| Playthrough identities (one line each) | 30 (29 by `SaveUniversalId`, 1 pre-Waypoint Xbox save with no id) | R7 |
| Distinct versions | 195 member files = 106 distinct captured slot pairs | R7 2.4 |
| A and B members of one slot hold the same identity | 587/587 captures; twins (same id in two slots of one capture) 0 | R7 |
| Lines live on Steam and Xbox at once | 3, at different versions; 1 genuinely diverged (Creative Primary) | R7 2.6 |
| Vault size vs distinct content | 3.41 GB vs ~0.5 GB; thumbnail `cache/` copies are 1.77 GB | R7 2.5 |
| Versions with exactly one copy | 73 (16 live-only; 6 only in snapshots that keep-20 pruning will delete) | R7 TL;DR |
| Scan + identity of everything (meta level) | ~1.0 s for 52 folders; hashing all data 1.2-2.7 s | R1 2h, R7 2.9 |
| Full content index of all 195 versions | 93.5 s today; 42 s with the slice-copy decoder fix; 11-24 s with 4 spawn workers; per new version 0.1-1.1 s | R5 F4 |
| Content index size | 4.3 MB sqlite / 1.5 MB JSON for 195 versions | R5 F6 |
| Lineage signals | meta play time + timestamp never decrease on 179/179 known ancestor pairs; meta + 31 core counters + VisitedSystems extension accept 179/179 and find the one real fork; "touch" and editor forks (11 % of steps) are invisible to any signal | R7 2.7 |
| External editor sessions in the record | 3 (same meta time and play time, different bytes) | R7 2.6 |
| Steam Cloud behaviour | overwrites made outside the game stick; deletions come back; 3 app writes in the oplog were reverted | R7 2.6, R8 2e |
| Xbox sync | per save, state in `containers.index`; uploads 10-30 s after the game exits | R8 2e |
| Xbox metas | id at 0x118, save time at 0x120, both unread by the app; 296-B metas lose names; Xbox promote is a no-op | R1, R7 2.3, R8 A5 |

## 2. Decisions (binding for the design wave)

### S1 Identity and versions

- **C1 One line = one playthrough identity** = `CommonStateData.SaveUniversalId` (Steam meta
  0x15C, Xbox meta 0x118 once the parser is fixed; the data copy of the id is a consistency
  check). Pre-Waypoint saves fall back to `platform:slot:name`. The owner can **Combine** two
  lines or **Split** a line by hand (LaunchBox's escape hatch; R4 B1). Names, mode, difficulty
  are attributes, never identity.
- **C2 Cross-platform copies stay on one line.** The line shows every live location (e.g.
  `S2 X4`) and a cross-platform state: same / Steam ahead / Xbox ahead / diverged (R7 3).
- **C3 Branches stay on one line.** Putting an older version live (or a twin into a second
  slot) makes the line's history a tree; each live tip gets its own live badge. The expanded
  line and the history view show the branches.
- **C4 Version identity = sha256 of a member's data file.** Storage and lineage work per member.
  **Display unit = the captured slot pair**, labelled by its newer member (by meta timestamp).
  The meta is an attribute of a version (re-keyed per slot, edited by promote).
- **C5 No-progress versions** (same play time: game "touches", external edits) are folded into
  the neighbouring version with a marker; versions changed outside the game (same meta time and
  play time, different bytes) get an "edited outside the game" flag.
- **C6 "Number of past backups" counts distinct versions,** never copies (575 copies vs 104
  versions in R2's count).

### S2 Content index

- **C7 Extractors use readable field names**; obfuscated keys are computed by a stdlib port of
  MBINCompiler's `HashName` (R5 F1; 1476/1476 keys reproduced). No mapping file ships. A mapping
  file is only for a possible future raw-JSON viewer.
- **C8 The index is derived data**: a pure function of (data sha256, extractor version), stored
  as a rebuildable cache. User-authored data (notes, labels, locks, manual Combine/Split, lineage
  fixes) never lives only in that cache.
- **C9 Facts are per context** (`BaseContext`, `ExpeditionContext`; `ActiveContext` says which
  loads). The base context represents the save; an expedition shows as a badge with its
  progress; an expedition-only save uses its expedition context (R6 3).
- **C10 Index depth, phase 1:** R6's curated catalog (the owner's facts plus the top extras),
  computed in one full decode per version; deeper detail (every base part, inventories) is
  decoded on demand when a version is opened (0.2-1.1 s).
- **C11 Indexing runs in spawn worker processes** (json.loads holds the GIL; threads freeze Tk;
  R5 F5), parent process is the only index writer, 2-4 workers, oversized saves alone,
  cancellable. POSIX kits fall back to one worker until spawn is verified there.
- **C12 Lookup tables are small curated constants** (galaxy names, legacy part ids, ship model
  types, mission final stages, expedition names), versioned by game build, always degrading to
  raw ids ("Expedition #24"). Galaxy names: a 256-name list is embedded (R6 Q6a).
- **C13 "Legacy base parts"** = the game's LEGACY STRUCTURES (69 ids) and LEGACY FREIGHTER
  MODULES (20 ids) build groups; two facts: placed in own bases, and still buildable
  (`KnownProducts`) (R6 2.4).
- **C14 "What changed"** = a fact diff against the lineage parent (never against the previous
  version by time), on ownership-filtered facts, top items first (R5 F7).

### S3 Lineage

- **C15 Three tiers of edges**, each stored with its tier and shown as such:
  1. *recorded* - written by app operations at write time (exact);
  2. *observed* - from a **live journal**: every scan hashes the live members (< 0.1 s) and a
     changed hash becomes an edge (kinds: game, edited outside the game, deleted, reverted by
     cloud);
  3. *inferred* - for history before recording existed: parent = the most-progressed earlier
     version that passes meta (play time, timestamp) monotonicity, then upgraded with content
     signals (31 core counters, VisitedSystems extension) once indexed (R7 2.7). Inferred edges
     are marked as such in the UI and can be corrected by the owner.
- **C16 Binding rule** (R8 4.6): after an operation places V into slot T, later live versions in
  T with the same id and play time >= V's are children of V until the next operation on T.
- **C17 Lineage lives in its own vault store**, not in `oplog.jsonl`. The oplog stays the
  undo/audit log and gains op ids, targets and hashes (R7 2.8 field list).
- **C18 Recording starts in the first phase that ships**, even before the history view exists;
  history that is not recorded cannot be recovered later.

### S4 Notes

- **C19 Two levels plus automatic changes** (R4 2e): a living **save-file note** (one per line,
  editable) and per-version **label + comment**; every new version also stores a **snapshot of
  the save-file note at capture time**, so the owner's wish "keep that note with each backup, so
  a user can see what they changed" holds literally (diff the note between versions). The
  automatic fact diff (C14) is the default description when the owner wrote nothing.
- **C20 Lock / pin** per version: exempt from any pruning; also offered as the eviction-picker's
  "do not suggest" mark for live slots.
- **C21 User data lives in the vault** (portable with it, D11), as JSON written atomically,
  keyed by identity and version hash. `catalog.json` v1 is not extended (older app versions
  crash on unknown member keys and drop unknown entry keys; R1 2a).
- **C22 Notes are stored as Unicode.** The Tk UI on the Linux kit cannot draw beyond Latin-1
  (DESIGN.md "ASCII-only"); there it shows a replacement character. UI strings stay ASCII.

### S5 Live-slot operations

- **C23 R8 section 4 is the operation spec**: `Place(V, T)`, `PlaceIntoEmpty`, `Swap`,
  `Evict`, "older version of a live save" = `Place` into the slot holding its id. Mandatory:
  explicit target (platform, account folder, slot) validated in core; preserve-before-replace
  (durable, hash-verified, never a prunable snapshot); write **both** members; journal intent
  and completion; slot-scoped validation and rollback; organizer operations never touch
  `accountdata`.
- **C24 Undo for organizer operations is slot-scoped and non-destructive** (undo = Place the
  previous members back; preserve what the owner played since). Today's whole-folder undo stays
  only as a "panic restore".
- **C25 Post-launch verification**: the next scan compares live slots with the last operation's
  expected hashes and reports cloud reverts (Steam Cloud, Xbox conflicts).
- **C26 Same platform + same account first.** Cross-platform is a separate, gated phase (F6).

### S6 Vault storage

- **C27 Pruning and deletion become version-aware**: never delete the last copy of a version;
  preserved (evicted) versions, locked versions, branch tips and live versions are exempt.
  This is also a fix for today's risk (6 versions exist only in prunable snapshots).
- **C28 A version locator** (hash -> where its bytes are) is part of the model whatever F3
  chooses; the organizer asks "where is this version", never "which folder".

### S7 UI

See section 4: the shape and technology of the UI are owner forks (F1, F2). Decisions that
hold for every UI option:

- **C29 Every write names its target** (platform, account, slot); the implicit "Active live"
  write target is removed from organizer flows (R2 risks).
- **C30 Move-to-live is one dialog with both ends visible**: entered from a line/version
  ("Move to live...") or from a live slot ("Replace with..."); the eviction picker lists slots
  with what is in them, play time, last played, "has a vault copy", pin; suggests an empty slot,
  else an unpinned slot whose save is already in the vault and played least recently; the
  button names the outcome ("Swap into Steam slot 7"); a non-modal "Done - Undo" follows
  (R4 2d, M3).
- **C31 No decoding on the Tk thread** (R2: 9.7 s freeze today). Long work shows progress
  without blocking.
- **C32 Vocabulary: Live / Vault / Version / Save file.** Storage kinds (full, snapshot,
  extract, in-place) are not primary concepts (R4 C9, Gitless).

## 3. Pre-existing defects (Phase 0 candidates; not organizer features)

Found by R1, R2, R5, R7, R8; all VERIFIED unless noted. They block or distort organizer work:

| # | Defect | Effect today |
|---|---|---|
| P1 | Xbox meta parser checks Steam lengths (`msstore.py:311-339`); id 0x118 / time 0x120 unread; 296-B metas lose names; test fixtures use 432 B | Xbox identity costs a full decode; wrong "newest" member in 37/84 Xbox pairs (5 of 14 live slots); Xbox promote is a no-op |
| P2 | Snapshot pruning is age-based (keep 20) | 6 versions that exist nowhere else will be deleted by future operations |
| P3 | GUI Promote targets the active source, not the clicked row (`gui.py:1539`) | wrong-folder write |
| P4 | Undo takes the newest snapshot of any folder | Xbox undo blocked or wrong; single level; failure gap restores an older snapshot |
| P5 | Clear-slot "already in the vault" matches by slot number | false "safe" (14 of 15 Xbox slots compared against a different save) |
| P6 | One-member source into an occupied slot leaves a mixed slot | the game may load the evicted save |
| P7 | Full restore / undo copy `steam_autocloud.vdf` / `containers.index` across accounts and roll back `accountdata` | cross-account corruption risk; account unlocks rolled back |
| P8 | Decoder: no cap on a chunk's claimed size; HGSAVEV2 claimed but not implemented; per-byte match copy (3-5x slower) | 4 GB allocation on a corrupt file; docs wrong; slow index |
| P9 | `discover --add` catalogues the live Xbox folder as an in-place backup | every Xbox save shows a fake extra backup |
| P10 | Toolbar needs 1,410 px in a 1,000 px window; Copy dialog clips its buttons at zoom 1.2 | Undo and other buttons unreachable at the owner's settings |
| P11 | `packaging/sitecustomize.py:125-126` `-c`/`-m` bypass never fires | `nmsvault.exe -c` runs the CLI parser |
| P12 | Rewriting `containers.index` changes the timestamps of records that were not touched (0 of 7 real indexes survive a parse + rebuild unchanged; D5 P-X1) | every Xbox write today can re-order other slots' members and trigger needless sync |
| P13 | Old app versions (v0.0.9-v0.2.8) treat an unknown catalog entry `kind` so that Restore wipes every other slot plus accountdata (D2, verified on old releases) | any new entry kind is unsafe; preserved copies must be catalogued as `extract` |

## 4. Owner forks (designs present these as options; nothing waits on the owner)

| Fork | Question | Options (designed) | Parent's lean |
|---|---|---|---|
| F1 | UI shape and technology | **T (Tk, R3 T1-A):** organizer built in the existing Tk app with a DPI + theme pass; two shells: (i) organizer as a view beside today's panes, (ii) organizer-first main window with today's panes behind "Backups (classic)"; Tk 9 later. **W (local web UI, R3 T2-A):** stdlib HTTP server on 127.0.0.1 inside the app, organizer in the default browser, the Tk window stays as host and for the classic flows. Not designed: third-party toolkits (break stdlib-only; PySide6 +118 MB), WebView2 via ctypes | R3 leans W (Unicode notes on Linux, graph polish, room for deeper views); T is lower risk and one language |
| F2 | History view style | A linear timeline with restore events; B lane graph (one lane per branch); C hybrid: version table with a narrow lane column + "key versions / all" | C |
| F3 | Vault storage | A keep folder-per-backup, add a version index, version-aware pruning, stop copying `cache/`; B content-addressed version store for new captures, old folders indexed in place, optional one-time migration (3.4 GB -> ~0.5 GB) | A first, B later |
| F4 | Member policy for Place | a: V + its same-id partner (re-time the partner if needed); b: V written to both members | a |
| F5 | Meta timestamp on Place | a: keep V's original time (honest in-game date); b: re-stamp to now | a |
| F6 | Steam <-> Xbox transfer | a: verbatim data + meta conversion, gated behind the owner's test runs T1/T2; c: never (point to Hello Games cross-save) | a, as a later phase |
| F7 | Placing another account's save | a: same person only (known identities in `UsedDiscoveryOwnersV2`); b: allow with a warning; c: block | a |
| F8 | Deletions under Steam Cloud (Evict, undo of a fill into an empty slot) | a: never needed - organizer offers Place/Swap only; b: refuse and show the manual procedure; c: delete, show the procedure, detect resurrection | a + b |
| F9 | Retention | keep everything; version-aware thinning (keep tips, branch points, noted, locked, live); per-save-file limits | keep everything + version-aware (C27) |
| F10 | Order of phases | section 5 | section 5 |

## 5. Order of work (recommended phasing)

| Phase | Content | Why here |
|---|---|---|
| 0 | Defects P1-P11 (P1, P2, P3-P6 first) | P1 corrupts identity/order on Xbox; P2 is live data-loss risk; P3-P6 are the hazards a 30-slot organizer multiplies |
| 1 | Organizer view (read-only): lines by identity, versions as pairs, live markers for 15 + 15 slots, meta-level columns, sort/filter; live journal + lineage store + recorded edges start; notes (save-file + version) | the owner's core ask; cheap (meta scan ~1 s); recording must start early (C18) |
| 2 | Place / Swap (+ Evict per F8) with the eviction picker, slot-scoped undo, post-launch verification | "move to live" - the owner's main action |
| 3 | Content index: facts columns, version card, automatic "what changed" | the owner's "important facts"; independent of phase 2 |
| 4 | History view (recorded + observed + inferred), compare two versions, Combine/Split | needs phases 1-3 data to be useful |
| 5 | Storage per F3-B (if chosen), retention per F9; Steam <-> Xbox transfer per F6 after T1/T2 | big-risk items last |

Phases 2 and 3 are independent; their order can swap.

## 6. Shared vocabulary and interface boundaries (all designs use these)

Designs run in parallel, so they meet at these seams. A design may refine a seam; it must then
say so in a "Deviations from 01-consolidation section 6" paragraph.

| Term | Meaning |
|---|---|
| `pid` | playthrough id: 16 upper-case hex digits of `SaveUniversalId`; fallback `legacy:<platform>:<slot>:<name>`; after an owner Combine, an alias table maps pids to a canonical pid |
| `vid` | version id: sha256 hex of one member's data file exactly as stored (verbatim bytes) |
| member | `A` (auto-save, file 2k-1) or `B` (restore point, file 2k) of a slot |
| capture | one observation of one slot: `{where, slot, A: vid or None, B: vid or None, observed_at}`; `where` is a live target or a vault entry |
| pair | the display unit: the distinct `(vid_A, vid_B)` of a capture, labelled by its newer member |
| live target `T` | `(platform, account_key, slot)`; `account_key` is the live folder's name, kept internally, always passed through `aliases.redact` for display |
| `op_id` | `op-<UTC yyyymmddThhmmssZ>-<4 hex>`; one per user-level operation (a Place with its eviction is one op) |
| edge | `{parent vid, child vid, tier: recorded/observed/inferred, kind, op_id or None, confidence, created}`; kinds include play, restore-older, place, swap, edit (outside the game), touch, cloud-revert, cross-save |
| organizer store | `<vault>/organizer/` - user data and recorded history as JSON, atomic writes (temp + `os.replace`), one lock file; owned by D1 |
| index cache | derived facts per `(vid, extractor_version)`, per-machine sqlite under the app's local data dir, keyed to the vault by a `vault_id` stored in the organizer store; owned by D4 |

Ownership of the seams:

- **D1 (model)** defines the pid/vid/capture/pair/edge/note schemas, the organizer store files,
  the lineage algorithms (journal observation, binding rule, inference), Combine/Split, the
  one-time bootstrap of the existing vault, the **read model** the UIs render (lines, versions,
  live board, history graph) and the abstract `VersionLocator`:
  `locate(vid) -> list[Location]`, `open(vid) -> (data bytes, meta plaintext, platform)`,
  `preserve(member) -> Location` (durable, verified), `is_durable(vid) -> bool`.
- **D2 / D3 (storage A / B)** implement `VersionLocator`, `preserve`, pruning/retention (C27),
  the `cache/` policy and, for D3, the store layout and migration.
- **D4 (index)** consumes `vid` + `VersionLocator.open`; exposes `facts(vid) -> dict or None`
  (per context), `request(vids, priority)`, `changes(child_vid, parent_vid) -> list[str]`.
- **D5 (operations)** implements Place/Swap/Evict/undo/verification on live targets, writes
  op records (journal intent/completion) and calls D1's lineage recording API and the
  locator's `preserve`.
- **UI designs** consume only D1's read model, D4's facts and D5's operation entry points;
  no UI reads save files or chooses write targets itself.

## 6a. Integration notes from the design wave

Requests between designs that the implementation must honour:

- D5 -> D1: `Journal.observe()` skips slots with an operation in progress; `op_outcome` reports
  "confirmed" only after a game session has run.
- D5 -> D4: add a `save.owners` fact (keyed digests, never raw ids) for the F7a same-person check.
- D3 -> D5: oplog lines whose snapshot exists only in the version store use
  `snapshot_id="snapshot-needs-newer-app"` plus `snapshot_store_id` (old apps then refuse undo
  instead of misbehaving).
- D1 deviations (edges are descent only; `owner` tier; batch `preserve`; `Location.external`;
  `LiveRef.instance`; version notes keyed by the pair head) are adopted by D2-D5.

## 7. Design wave

UI facts from R3 that every UI design must respect: Tk 8.6 styles whole rows only and puts
images only in column #0; every new ttk class needs dark-theme mapping (Notebook tabs came out
unreadable); the shipped GUI is DPI-unaware (blurry at 150 %; fix = `SetProcessDpiAwareness(1)`
plus scaling pixel constants); Linux-kit Tk cannot draw non-Latin-1 text (Tk 9 too); under the
pythonw launcher `sys.stderr` is None (log to a file; override `http.server.log_message`); the
POSIX packager ships only `*.py` (static web assets need a packager change or `.py`
embedding); Edge launched with a private profile silently signs into the Windows account -
never script Edge; render HTML with headless Chrome and a throwaway `--user-data-dir`.

Design documents (`designs/`):

| ID | Design | Covers |
|---|---|---|
| D1 | Core data model: identity, versions, version locator interface, lineage store, live journal, inference, notes storage, sidecar layout, schema/versioning, concurrency, migration of the existing vault | S1 S3 S4 (C1-C6, C15-C22, C27-C28) |
| D2 | Storage option A: index over folders | S6 F3-A |
| D3 | Storage option B: content-addressed version store (+ optional migration) | S6 F3-B |
| D4 | Content index engine: decoder fixes, HashName, extractor framework, fact catalog v1, contexts, lookups, worker pool, cache schema, changelog | S2 (C7-C14) |
| D5 | Live operations: Place / PlaceIntoEmpty / Swap / Evict / undo / journal / verification / cloud; F4, F5, F7, F8 as specified alternatives; cross-platform phase (F6) | S5 (C23-C26) |
| D6 | UI option T: Tk organizer, both shells, all screens, three history styles, mockups rendered by real Tk from `mockups/sample-data.json` | S7 F1-T, F2 |
| D7 | UI option W: local web UI, all screens, three history styles, HTML mockups rendered by headless Chrome from the same data | S7 F1-W, F2 |
