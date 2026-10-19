# Save-file organizer - consolidation of research

Status: research complete (R1-R8, `research/`); owner decisions (4a) and the design review
(`03-design-review.md`) applied. This document fixes the decisions the designs build on, records
the owner's choices and sets the order of work. Audience: the implementers and the parent.

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
  `S2 X4`). Each platform's live copy is its own tip (owner, 4a Q4): "in step" (same play time),
  "Steam/Xbox copy N h behind" (the lagging copy), or "two branches" only when the journal saw
  both progress since they were last in step. No link between the platforms is guessed (D1 2.8).
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
- **C14 "What changed"** = a fact diff against the lineage parent (an edge of C15; never the
  previous version by time), on ownership-filtered facts, top items first (R5 F7). A version
  without such a parent shows nothing ("no recorded parent"; owner, 4a Q5).

### S3 Lineage

- **C15 Edges are evidence only**, each stored with its tier and method and shown as such:
  1. *recorded* - written by app operations at write time (exact), and the app's legacy oplog
     resolved once at bootstrap ("put live by the app");
  2. *observed* - from the **live journal**: every scan hashes the live members (< 0.1 s) and a
     changed hash becomes an edge ("seen in the slot"); and, once at bootstrap, consecutive
     app-made backups of the same live slot ("seen in backups"; owner, 4a Q3);
  3. *owner* - the owner's own "set parent" correction.
  Nothing is inferred (owner, 4a): a version none of these reaches is listed by date, without a
  link. Deletions and cloud reverts are journal events, not edges (D1 2.2).
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
  automatic fact diff (C14) is the default description when the owner wrote nothing and the
  version has a lineage parent.
- **C20 Pin and lock.** A pin per save file: the eviction picker does not suggest its slot for
  another save. A lock per version: the owner's "keep" mark, shown and treated as a key version
  (nothing is pruned under F9).
- **C21 User data lives in the vault** (portable with it, D11), as JSON written atomically in
  `<vault>/organizer/`, keyed by identity and version hash. `catalog.json` v1 is not extended.
- **C22 Notes are stored as Unicode.** The Tk UI on the Linux kit cannot draw beyond Latin-1
  (DESIGN.md "ASCII-only"); there it shows a replacement character. UI strings stay ASCII.

### S5 Live-slot operations

- **C23 R8 section 4 is the operation spec**: `Place(V, T)`, `PlaceIntoEmpty`, `Swap`, "older
  version of a live save" = `Place` into the slot holding its id. Operations always replace; the
  app never empties a slot (F8). Mandatory: explicit target (platform, account folder, slot)
  validated in core; preserve-before-replace (a durable, hash-verified store entry); write
  **both** members; journal intent and completion; slot-scoped validation and rollback;
  organizer operations never touch `accountdata`.
- **C24 Undo for organizer operations is slot-scoped and non-destructive** (undo = Place the
  previous members back; preserve what the owner played since). Today's whole-folder undo stays
  only as a "panic restore".
- **C25 Post-launch verification**: the next scan compares live slots with the last operation's
  expected hashes and reports cloud reverts (Steam Cloud, Xbox conflicts).
- **C26 Same platform first.** A save from another account of the same platform is allowed
  with one warning (F7b; no ownership ids are read or stored). Cross-platform is a separate,
  gated phase (F6).

### S6 Vault storage

- **C27 Keep everything (F9)**: nothing is pruned; phase 0 removes today's snapshot pruning
  (P2), which threatens 6 versions that exist only in snapshots. The only deletion is freeing
  old backup folders after they were folded into the store, on the owner's request, after a
  deep check (D3, phase 5).
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
| P13 | Old app versions (v0.0.9-v0.2.8) treat an unknown catalog entry `kind` so that Restore wipes every other slot plus accountdata (D2, verified on old releases) | moot: users do not go back to older app versions (4a Q2), and new entries live in the store, not the catalog; not fixed |

## 4. Owner forks

The forks the designs presented; every one is decided (4a).

| Fork | Question | Options that were designed |
|---|---|---|
| F1 | UI shape and technology | T: Tk organizer in the existing app (D6); W: local web UI (D7) |
| F2 | History view style | A linear timeline; B lane graph; C hybrid version table with a lane column + "key / all" |
| F3 | Vault storage | A folder-per-backup plus a version index (D2); B content-addressed version store (D3) |
| F4 | Member policy for Place | a: V + its same-id partner, re-timed; b: V written to both members |
| F5 | Meta timestamp on Place | a: keep V's original time; b: stamp now |
| F6 | Steam <-> Xbox transfer | a: verbatim data + meta conversion, gated behind the owner's test runs T1/T2; c: never |
| F7 | Placing another account's save | a: same person only; b: allow with a warning; c: block |
| F8 | Emptying a slot under Steam Cloud | a: never needed (replace only); b: show the manual procedure; c: delete and detect resurrection |
| F9 | Retention | keep everything; version-aware thinning; per-save-file limits |
| F10 | Order of phases | section 5 |

## 4a. Owner decisions (2026-10-16)

| Fork | Decision |
|---|---|
| F1 UI | **Tk (D6)**, with selected elements of the web design carried over - see `02-ui-review.md` |
| F2 history style | **C hybrid** (version table + lane column + key/all toggle) |
| F3 storage | **B version store (D3), from phase 2.** Before the first store write the whole vault is copied aside once and verified (D3 copy-aside step), so a failed upgrade can be rolled back (D3 rollback rule). Older app versions are not supported afterwards (Q2): no compatibility machinery (no shadow or barrier entries, no `extract` cataloguing, no oplog barrier) |
| F4 member policy | **a** - the version plus its own same-id partner, re-timed so the chosen one loads |
| F5 meta timestamp | **b** - stamp "now" (the original time stays in the vault record) |
| F6 Steam <-> Xbox | **a**, last phase, after the owner's test runs T1/T2 |
| F7 other accounts | **b** - allow, with a warning (bases may not be editable until the game re-owns them) |
| F8 emptying a slot | organizer operations always **replace**; the app never empties a slot; emptying shows the manual procedure, and an empty slot warns that Steam Cloud or Xbox sync may fill it again |
| F9 retention | **keep everything**; a version's last copy is never deleted |
| F10 order | as section 5 (defects first) |
| Pre-feature history (D1) | **no guessed tree**: the tree is drawn only from recorded and observed history, including the "seen in backups" links (Q3); every other version is listed by date |
| Index display (D4) | D4's defaults accepted (legacy placed + buildable badges; ships "12 (3 corvettes) +5 archived"; companion custom name else creature type; built-in name tables; while the game runs index live saves only, one worker) |

### Owner answers to the design review (2026-10-17, `03-design-review.md` section 4)

| Q | Answer |
|---|---|
| Q1 version store start | **With phase 2**: copy the vault aside first; from then on new backups, snapshots and evicted saves go to the store; old folders are folded in automatically, freed only on request |
| Q2 older app versions | **No guard.** After the upgrade, users cannot and should not go back to older app versions |
| Q3 pre-recording links | **"Seen in backups"**: consecutive app backups of the same live slot are linked as observed history |
| Q4 save live on Steam and Xbox | Owner's model: Hello Games cross-save replicates it and the latest usually wins, but a platform not opened for a long time lags; if both vary, each platform effectively becomes its own branch. Show each platform's copy as its own tip: "behind" when it lags, two branches when both progressed |
| Q5 "what changed" without a known parent | show nothing ("no recorded parent") |
| Q6 other-account warning | one warning whenever the save comes from another account; no ownership ids processed |
| Q7 store details | fold old backups automatically after the copy-aside; free old folders only on request after a deep check; keep thumbnails deduplicated (139 MB); offer to copy hand-made in-place backup folders into the store, default off |
| Names | The owner's machine names must never appear in the product, docs or mockups |

## 5. Order of work (recommended phasing)

| Phase | Content | Why here |
|---|---|---|
| 0 | Defects P1, P2, P3-P8, P12 first; P9-P11 alongside; P13 moot (4a Q2). P2 = stop pruning: `snapshot_live` no longer calls `_prune_snapshots`, a test asserts no snapshot is ever deleted; costs 41-63 MB per live write (R1 A6) until phase 2 stores snapshots deduplicated | P1 corrupts identity/order on Xbox; P2 is the live data-loss risk; P3-P8 are the hazards a 30-slot organizer multiplies; P12 is a prerequisite of every Xbox write |
| 1 | Organizer view (read-only): lines by identity, versions as pairs, live markers for 15 + 15 slots, Steam/Xbox tips, meta-level columns, sort/filter; live journal, lineage store, bootstrap ("seen in backups" + legacy oplog); notes (save-file + version); classic live writes serialised by the live-op lock and journaled (D1 2.6.7) | the owner's core ask; cheap (meta scan ~1 s); recording must start early (C18) |
| 2 | Vault upgrade to the version store (D3): copy-aside, store format, automatic fold of old backups; from then every new backup, snapshot and preserved save is a store entry and `StoreLocator.preserve` is the preserve provider; the entry listing serves the organizer, the classic tab and the CLI; hand-made in-place folders can be copied in (owner action, default off). Then Place / Swap with the eviction picker (always replace, F8), keep-a-copy, slot-scoped undo, post-launch verification | "move to live" - the owner's main action; one way to keep evicted saves, built once |
| 3 | Content index: facts columns, version card, automatic "what changed" for versions with a lineage parent | the owner's "important facts"; independent of phase 2 |
| 4 | History view (recorded and observed links, dated list for the rest), compare two versions, owner corrections, Combine/Split | needs phases 1-2; phase 3 adds milestones and "what changed" to it |
| 5 | Free old folders (on the owner's request after a deep check), merge of store-mode vaults on import; Steam <-> Xbox transfer per F6 after T1/T2 | big-risk items last |

Phases 2 and 3 are independent; their order can swap. With F7b, phase 2 needs nothing from
phase 3.

**Cross-design integration suite** (from phase 2, in CI): synthetic Steam and Xbox live folders,
the real D1 store, D3 store and D5 operations; place, swap, promote, undo, keep-a-copy, crash
points C1-C14 with forward and backward recovery, and a simulated cloud revert; assert together
the live tree, store entries, `ops.jsonl`, the oplog, journal edges and events, and
`op_outcome`. Each design's own suite still uses fakes of the others.

## 6. Shared vocabulary and interface boundaries (all designs use these)

Designs run in parallel, so they meet at these seams. A design may refine a seam; it must then
say so in a "Deviations from 01-consolidation section 6" paragraph.

| Term | Meaning |
|---|---|
| `pid` | playthrough id: 16 upper-case hex digits of `SaveUniversalId`; fallback `legacy:<platform>:<slot>:<name>`; after an owner Combine, an alias table maps pids to a canonical pid |
| `vid` | version id: sha256 hex of one member's data file exactly as stored (verbatim bytes) |
| member | `A` (auto-save, file 2k-1) or `B` (restore point, file 2k) of a slot |
| capture | one observation of one slot: `{where, slot, A: vid or None, B: vid or None, observed_at}`; `where` is a live target, a catalog entry or a store entry |
| pair | the display unit: the distinct `(vid_A, vid_B)` of a capture, labelled by its newer member |
| live target `T` | `(platform, account_key, slot)`; `account_key` is the live folder's name, kept internally, always passed through `aliases.redact` for display |
| `op_id` | `op-<UTC yyyymmddThhmmssZ>-<4 hex>`; one per user-level operation (a Place with its eviction is one op) |
| edge | game-state descent `{parent vid, child vid, tier: recorded/observed/owner, method: op/oplog/journal/capture-sequence/owner, kind: play/touch/edit/unknown, op_id or None, created, target}`; never inferred. Place, swap and undo are operation kinds; deletions and cloud reverts are journal events (D1 2.2) |
| organizer store | `<vault>/organizer/` - user data and recorded history as JSON, atomic writes (temp + `os.replace`), the store lock and D5's live-op lock; owned by D1 |
| version store | `<vault>/store/` - content-addressed objects and one manifest per backup, snapshot, extract, import or preserve; owned by D3 |
| index cache | derived facts per `(vid, extractor_version)`, per-machine sqlite under the app's local data dir, keyed to the vault by a `vault_id` stored in the organizer store; owned by D4 |

Ownership of the seams:

- **D1 (model)** defines the pid/vid/capture/pair/edge/note schemas, the organizer store files
  and its lock, the lineage algorithms (journal observation, binding rule, bootstrap backfill),
  the Steam/Xbox tip comparison, Combine/Split and owner corrections, the **read model** the UI
  and CLI render (lines, versions, live board, history graph and dated list, events), the
  abstract `VersionLocator` - `locate(vid) -> list[Location]`, `open(vid, prefer) ->
  OpenedVersion`, `preserve(members, *, op_id, reason) -> list[Location]` (durable, verified),
  `is_durable(vid) -> bool` - and the composite `VaultLocator` handed to D4 and D5.
- **D3 (storage B, chosen)** implements `StoreLocator` (including `preserve`), the store layout,
  the copy-aside, fold, rollback, fsck, the entry listing `list_vault_entries` (catalog + store,
  for D1's scanner, the classic tab and the CLI) and the `cache/` policy. D2's `FolderLocator`
  read side serves catalog folders until fold; the rest of D2 (storage A) is not built.
- **D4 (index)** consumes `vid` + `VaultLocator.open`; its `IndexService` implements D1's
  `FactsProvider`: `view(vid) -> FactsView | None`, `changes(child, parent) -> ChangeSet | None`,
  `is_milestone(child, parent)`, `request(vids, Priority)`.
- **D5 (operations)** implements Place/Swap/undo/keep-a-copy/verification on live targets (no
  slot is ever emptied, F8), writes op records (journal intent/completion), holds the live-op
  lock, reports `busy_targets`, and calls D1's `LineageRecorder` and the locator's `preserve`.
- **The UI (D6)** consumes only D1's read model and notes API, D4's facts and D5's entry
  points; it never reads save files or chooses write targets itself.

## 6a. Integration notes from the design wave

Agreements between designs that the implementation must honour (all written into the designs):

- **D5 -> D1, journal** (D1 2.6.2, 2.6.6): `observe(scan, *, src, skip)` takes a required
  `skip`; refreshes pass D5's `busy_targets`, D5's own post-write check passes busy targets minus
  its own op. `op_outcome` is `confirmed` only when a recorded child of the placed pair is seen;
  an unchanged placed pair stays `pending`, and D5 adds its game-session and upload evidence.
- **D1 <-> D3, entries**: one listing, `list_vault_entries` (D3), feeds D1's scanner, the classic
  tab and the CLI; a store entry yields D1 captures from its manifest (no file read); fold and
  copy-in keep entry ids, so journal references stay valid.
- **D3 -> D5, store**: preserve = `StoreLocator.preserve` (a `preserve` store entry); pre-op
  snapshots are store entries; the oplog `snapshot_id` is the store entry id. No barrier or
  other old-app protection (4a Q2).
- **D4 -> D1, facts**: D1 calls only the `FactsProvider` subset of `IndexService`; `FactsView`
  carries `pid_in_data` for D1's id check. No lineage signals and no ownership digests are
  extracted (no inference; F7b reads no ownership ids).
- **Locks**: the store lock (D1, `<vault>/organizer/.lock`) is held per transaction or per D3
  manifest commit, is not re-entrant, and a timeout fails only that transaction. The live-op
  lock (D5, `<vault>/organizer/.liveop.lock`) covers every live write, including classic ones
  from phase 1 (D1 2.6.7); it is always taken before the store lock.
- D1 deviations (edges are descent only; `owner` tier; batch `preserve`; `Location.external`;
  `LiveRef.instance`; version notes keyed by the pair head) are adopted by D3-D5.

## 7. Design wave

UI facts from R3 that the Tk UI must respect: Tk 8.6 styles whole rows only and puts images
only in column #0; every new ttk class needs dark-theme mapping (Notebook tabs came out
unreadable); the shipped GUI is DPI-unaware (blurry at 150 %; fix = `SetProcessDpiAwareness(1)`
plus scaling pixel constants); Linux-kit Tk cannot draw non-Latin-1 text (Tk 9 too); under the
pythonw launcher `sys.stderr` is None (log to a file).

Design documents (`designs/`):

| ID | Design | Covers | Status |
|---|---|---|---|
| D1 | Core data model: identity, versions, version locator interface, lineage store, live journal, Steam/Xbox tips, notes storage, schema, concurrency, bootstrap of the existing vault | S1 S3 S4 (C1-C6, C15-C22, C27-C28) | to build |
| D2 | Storage option A: index over folders | S6 F3-A | not chosen; only the P2 fix and the `FolderLocator` read side are built |
| D3 | Storage option B: content-addressed version store, copy-aside, fold | S6 F3-B | to build (phase 2, phase 5) |
| D4 | Content index engine: decoder fixes, HashName, extractor framework, fact catalog v1, contexts, lookups, worker pool, cache schema, changelog | S2 (C7-C14) | to build |
| D5 | Live operations: Place / PlaceIntoEmpty / Swap / undo / keep-a-copy / journal / verification / cloud; F4a, F5b, F7b, F8 as decided; cross-platform phase (F6) | S5 (C23-C26) | to build |
| D6 | Tk organizer, shell 10a, history style C, mockups rendered by real Tk from sample data (mockups removed) | S7 F1-T, F2 | to build |
| D7 | Local web UI | S7 F1-W | not chosen (F1 = Tk); kept for reference, nothing depends on it |
