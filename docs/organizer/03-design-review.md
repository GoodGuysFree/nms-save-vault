# Save-file organizer - design review

Reviewer V1, 2026-10-16, branch `design/save-organizer`. Read: `.agent/rules/consolidated.md`,
`00`, `01` (incl. 4a, 6a), `02`, designs D1-D7, the Tk mockups, and code where a claim needed
checking. Review only: this is the only file written.

## 1. Verdict

The set covers every element of the owner's ask and is well evidenced (measured numbers, crash
matrices, old-app experiments). It is **not ready to implement**: the owner decisions of 4a and
02 reached D6 but not the core designs D1, D3, D4, D5. Two blockers: the vault copy-aside that
conditions storage B is designed nowhere, and no design says who provides preserve-before-replace
in phase 2 under B. Nine majors are decision drift (guessed history, F5/F7/F8 defaults, old-app
machinery) plus two seams (store entries invisible to D1 and the classic tab; no cross-design
tests). Counts: 2 blocker, 9 major, 11 minor.

### 1.1 Fit to the ask

| Element of the ask | Where | Status |
|---|---|---|
| One view of every save file, vault and live, whatever backup it is in | D1 2.10.1 `Line`; D6 01 | covered |
| Sort; basic information per line | D1 `sort_lines`, meta fields; D4 `FactsView`; D6 columns | covered |
| Mark live, 15 Steam + 15 Xbox, split and identified | D1 `LiveBadge`, `LiveBoard`; D6 Live column, one/two live lists, several Steam accounts | covered |
| All versions as one line, count, expandable | D1 `version_count` (C6, distinct versions); D6 02 | covered |
| Pick a non-live line, "move to live", choose the slot to evict | D5 `eviction_picker`, `plan_place`; D6 03 | covered |
| Same for any backup of a live or non-live line | D5 restore-older; D6 03b | covered |
| Detailed note per save file kept with each backup, see what changed | D1 2.9 + journal `note_revs`; D6 05 | covered from phase 1 on; existing versions have no note snapshot (unavoidable) |
| History tree after trying an older backup | D1 journal + recorder; D6 04c | covered for recorded history; weakened by V-03..V-05 |
| Facts: milestones, glyphs, base parts, legacy parts, ships (names, types), companions, galaxies with bases | D4 2.5, `FactsView` | covered |
| Deeper indexing later; multi-phase | D4 schema + `EXTRACTOR_VERSION`, deep decode; 01 section 5 | covered; phase 2 open (V-02) |

Weakened: the Steam/Xbox state of a save live on both (V-04) and the automatic "what changed" for
the 195 existing versions (V-05).

## 2. Findings (most severe first)

### V-01 blocker - The vault copy-aside before the store upgrade is not designed

- **Location:** D3 4.1 (step 1 starts by creating `store/` and the barrier), 3.1, 3.4, 6 (B-1,
  B-12); 01 4a F3. Only D6 (header) and D7 mention the copy.
- **Problem:** the owner accepted B on the condition that the whole vault is copied aside once
  before the upgrade, so a failed upgrade can be rolled back. No design says when (first store
  write or fold), where the copy goes, the free-space check (3.4 GB for the owner), how it is
  verified, what happens when it fails, or what "rollback" restores.
- **Consequence:** D3 as written upgrades with no rollback. A naive rollback after store mode has
  run loses every store-only entry written since, including preserved (evicted) live saves that
  exist nowhere else. Reclaim (M2) then removes the vault's accidental redundancy (D3 risk 1).
- **Fix:** add a "pre-upgrade copy" step to D3: before the first write that creates `store/`;
  free space >= vault size + 1 GB; copy to a sibling `<vault>.pre-store-<UTC>`; sha256-verify
  every file; record path and result in `store/state/migration.json`; any failure leaves the vault
  untouched and store mode off. Rollback is offered only while no store entry exists that the copy
  lacks; afterwards newer entries are exported (D3 4.3 export) before restoring. Only the owner
  deletes the copy; reclaim (M2) is refused unless the copy exists or a deep fsck just passed.
  Tests: verified copy, low-space refusal, crash mid-copy, rollback round trip on a sandbox vault.

### V-02 blocker - Phase 2 has no preserve provider under storage B

- **Location:** 01 section 5 (phase 5 "Storage per F3-B"); D5 1.3, 2.1 (`durable` = "never a
  snapshot", D2 decision 3), 2.6.1 (rollback pre-bytes from a `copy2` folder snapshot), 2.8
  (preserve notes, `storage_after_operation` retention hook), 6 phase 2 header ("needs ... D2 2.1
  `preserve`"); D1 1 dependency table; D3 3.4.
- **Problem:** move to live must preserve the evicted pair durably before the first live write
  (C23, D5 I2). The only phase-2 preserve designed is D2's `keep-*` extract folders (storage A,
  not chosen). D3 recommends shipping its append-only core with phase 2 "if the owner picks B";
  01 still puts B in phase 5; nobody decided.
- **Consequence:** phase 2 cannot be planned. Either an A-style preserve is built and thrown away
  in phase 5 (D3 3.4 "code written twice"), or B's on-disk format lands in phase 2 without the
  copy-aside (V-01) and without the phase being re-planned.
- **Fix:** owner question Q1. Recommended: D3's append-only core (B-1..B-8, B-10, B-11; not B-9)
  ships with phase 2 together with the copy-aside; fold/reclaim stay in phase 5. Then re-point D5
  at the store (`StoreLocator.preserve`, snapshots via `add_from_live(kind="snapshot")`, rollback
  pre-bytes via `read_file`, store snapshots count as durable, no retention hook) and update 01
  section 5, D1 dependencies and D3 3.4.

### V-03 major - D1 still designs and recommends guessed history; the "drawn edge" rule is undefined

- **Location:** D1 0 item 5, 2.2 (tier `inferred`), 2.8, 2.10.2 (flag `inferred`), 2.12 (GUI text
  "30 more links guessed"), 2.14 item 8, 3 O-D1-1 (recommends a), 5 row 8, 6 steps 13-14; D4
  2.4.3 `lineage` extractor, 2.6, 2.9.2 `lineage` table, 2.12 `lineage_signals`, 5.8; 01 C15 tier 3.
- **Problem:** 4a: no guessed tree; versions from before recording are a flat dated list. D1 still
  builds the meta/content classifier and recommends showing guesses. It also tags the 130
  bootstrap "capture-sequence" edges (consecutive app-made backups of one live slot) as
  `inferred`, while D6 04c draws one of them (Main Save #18 -> #19, "seen in the slot") and lists
  the rest "by date". D4 extracts and caches lineage signals (31 counters, VisitedSystems) whose
  only consumer is the rejected classifier.
- **Consequence:** implementers build the rejected inference, or draw an arbitrary subset of
  bootstrap edges; 02, 04c and CLI `history` disagree on what is linked.
- **Fix:** D1: delete 2.8, `infer.py`, O-D1-1, steps 13-14 and the "guessed" wording; `history()`
  returns drawn edges only = recorded (`op`, `oplog`) + observed (`journal`), plus capture-sequence
  if the owner says so (Q3); everything else is the dated list (also D6 8.3 request 3). D4: drop
  the `lineage` extractor, table and `lineage_signals` (keep `pid_in_data`, `play_time_s`).

### V-04 major - The Steam/Xbox state needs guessed ancestry

- **Location:** D1 2.10.1 (`cross_platform` by ancestor test; no parent chain -> `unknown`), 2.8
  (cross-save edges come only from the classifier), 2.12 ("4 cross-save"); 01 C2; D6 2.2 State
  vocabulary, 2.9; mockups 01 ("Steam ahead"), 03 ("Steam is ahead"), 08 ("the copies diverged").
- **Problem:** the Steam and Xbox copies of one save are joined only by inferred `cross-save`
  edges (journal edges never cross targets). Without inference D1 returns `unknown` for every
  cross-platform line, while D6 and the approved mockups show "Steam ahead" / "diverged S/X" for
  lines whose history is a dated list.
- **Consequence:** C2's state silently becomes "unknown" for all 3 owner saves live on both
  platforms, or the rejected classifier comes back through this label.
- **Fix:** owner question Q4. Recommended: compare the two live heads without ancestry: "same"
  (same pair), else "Steam copy has +Xh more play" / "Xbox copy has +Xh more play"; "diverged"
  only when recorded/observed edges prove two branches. Update D1 2.10.1, C2, D6 and the mockups.

### V-05 major - "What changed" for versions without a parent contradicts C14 and D4 N1

- **Location:** D6 2.4 item 4 ("or the dated predecessor before recording"), 2.5 right panel
  ("WHAT CHANGED SINCE ... previous version by date"), 2.2 Note column; mockups 03 ("continued on
  Steam from the Xbox copy (cross-save)") and 04c; versus 01 C14 and D4 2.11.1 ("the index never
  guesses one"), N1.
- **Problem:** with no parent D4 returns nothing; D6 instead diffs against the previous version by
  date, which D4 rejected on real data (by-time order reported 8 bases "gone" then "new", R5 F7).
  The 03 mockup prints a cross-save header only inference can produce.
- **Consequence:** the default description (C19) of most existing versions is empty or misleading
  where branches interleave (Main Save, Creative Primary).
- **Fix:** owner question Q5 (with Q3, which decides how many existing versions get a parent).
  Recommended: show "no recorded parent" and no diff; if a by-date diff is wanted, label it
  "compared with the previous backup by date". One rule in D4 2.11 and D6; fix the mockup texts.

### V-06 major - Dropping all old-app machinery reopens a destructive Undo in older app versions

- **Location:** 01 4a F3 and 6a item 3 (still lists the barrier); D3 2.9, 3.5, 4.2; D5 2.13.1 I12,
  4.2; code `src/nms_save_vault/core/operations.py:893-907`.
- **Problem:** released `undo_last` walks the oplog backwards, skips records whose snapshot is not
  in the catalog, and runs `restore_full(..., mirror=True)` on the next older snapshot (VERIFIED in
  code). In store mode the pre-op snapshot is a store entry, not a catalog entry. Without D3's
  barrier, an older app's Undo on the upgraded vault rolls all 15 slots and accountdata back to an
  older snapshot, and mirror deletes slots absent from it.
- **Consequence:** "older versions no longer work" becomes "older versions silently destroy live
  progress"; portable kits of older versions stay on disk, and a USB vault can meet one on another PC.
- **Fix:** owner question Q2. Recommended: keep only the barrier (one catalog entry + the oplog
  `snapshot_id` convention; VERIFIED X1/X2 on v0.0.9-v0.2.8) as a safety guard; drop shadows,
  per-entry stubs, unfold/downgrade, V0-V2 and the old-app harness. Reword 4a and 6a.

### V-07 major - D5 still empties slots, against F8

- **Location:** D5 2.2 `Policies.steam_cloud_delete="refuse_guided"`; 2.4 (Evict; Swap with one
  empty side deletes the vacated slot; undo of PlaceIntoEmpty = Evict); 2.11; 3.4 (recommends b,
  incl. "Steam Cloud is off - clear now"; Xbox and Steam-local deletes "allowed under every
  option"); 4.1 (`clear_slot` -> `plan_evict`); 4.3 (panic restore deletes slots absent from the
  snapshot on Xbox/Steam-local); 4.5 ("Free this slot...", CLI `clear`, `evict`); I9; T-F4,
  T-F8a-c; step 2.10.
- **Problem:** 4a F8 and 02: operations always replace; the app never empties a slot itself;
  emptying shows the manual procedure. D6 follows it (`plan_evict` not offered); D5 does not.
- **Consequence:** deletion paths ship that the owner rejected; under Steam Cloud deleted files
  come back (R8 2e) and the slot returns in an unexpected state.
- **Fix:** remove Evict and `steam_cloud_delete`; Swap with an empty side -> blocked ("use Move to
  live"); undo of a fill -> blocked with the manual steps (as D6 9 already shows); panic restore
  leaves absent slots alone and lists them; CLI `clear` prints the procedure; add an info code
  (e.g. `EMPTY_SLOT_MAY_REFILL`) so D6's empty-slot text comes from D5 (D6 3.6); replace T-F4/T-F8
  with an invariant test "no organizer path deletes a live save file, except rollback of files the
  op created".

### V-08 major - D5's F5 and F7 defaults contradict the owner

- **Location:** D5 0 item 5; 2.2 `Policies(timestamp="keep", cross_account="same_person")`; 3.2
  (recommends a); 3.3 (recommends a, owner identities, Accounts "this is also me"); 6 phase 3.1;
  2.11 (undo puts the evicted pair back with "in-game date ... exactly as before"); D4 2.4.5
  `owner_digests`, 2.12 `owner_status`, 3 O14.
- **Problem:** 4a F5 = b (stamp now), F7 = b (allow with a warning). Undo under F5b is unspecified.
- **Consequence:** the core ships the opposite of the decided behaviour, and the same-person
  machinery (owner identities, keyed HMAC digests of platform ids, a per-cache secret) is built for
  a rule nobody wants.
- **Fix:** `Policies` = now / warn; 3.2 and 3.3 record the decision; undo also stamps now; F7b =
  `OTHER_ACCOUNT` confirm whenever the capture account differs from the target; drop phase 3.1 and
  D4 `owner_digests` / `owner_status` / O14 unless the owner picks Q6-b.

### V-09 major - Store entries are invisible to D1's scanner and to the classic tab

- **Location:** D1 2.5 step 2 ("Entries = `vault.entries`", catalog only), 2.12 (bootstrap maps
  catalog entries); D3 3.5 (shadow entries were the listing), 4.1 step 6 ("the organizer reads
  store entries ... from the manifests", no D1 interface); D6 2.11 (classic tab "unchanged",
  reads the catalog).
- **Problem:** with shadows dropped (4a), nothing lists store entries except D3's own API. D1 has
  a `StoreRef` type but no scan step for it; the classic Backups pane and CLI `list` / `restore` /
  `undo` read `catalog.json`.
- **Consequence:** every backup, snapshot and preserve made in store mode is missing from D1
  captures (versions show NOT IN VAULT while D3's `is_durable` says yes; the journal loses timeline
  evidence) and from the classic tab and CLI.
- **Fix:** D1 2.5 adds store entries as a capture source (one capture per occupied slot,
  `where=StoreRef(id)`, `observed_at=created_utc`, exact); 2.12 uses store snapshots and fulls as
  backfill evidence; define one "vault entries" API (catalog + store) for the classic view and CLI.

### V-10 major - The phase-0 fix for P2 is over-built for "keep everything"

- **Location:** 01 section 5 phase 0; D2 2.9, 3.3, 6 steps 0.1-0.3 (slot-entry writer, retention
  planner with rescue, trash); D5 6 ("P2 is D2's step 0.2"); code `operations.py:144, 148-159`.
- **Problem:** P2 (age pruning can delete the only copy of 6-9 versions) is the live data-loss
  risk. Under F9 + B the simplest correct fix is to stop pruning (`snapshot_live` no longer calls
  `_prune_snapshots`). D2's planner, rescue entries and per-account N exist to limit storage A's
  disk use and become legacy under B.
- **Consequence:** the most urgent fix waits on ~M+M+S of code that phase 5 replaces.
- **Fix:** phase 0 P2 = remove the prune call, test "no snapshot is ever deleted", state the cost
  (41-63 MB per live write, R1 A6) until the store dedupes snapshots. Keep from D2 only what B
  reads: the `FolderLocator` read side and durability classes for legacy folders until fold.

### V-11 major - The risky path is never tested across designs

- **Location:** D5 5.1 (`FakeLocator`, fake `LineageRecorder`); D3 5 (store alone); D1 5 (synthetic
  journal records); D6 6 (fake liveops).
- **Problem:** Place -> D3 preserve -> D5 journal/oplog -> D1 `record_op` / `observe` -> replay
  (edges, `op_outcome`) -> D5 verification -> crash recovery spans three designs, each tested only
  against fakes of the others.
- **Consequence:** seam bugs such as V-12 or the lock rules of V-13 show up only on real saves.
- **Fix:** an integration suite with synthetic Steam and Xbox live folders, the real D1 store, D3
  store and D5: place, swap, promote, undo, crash points C1-C14 with forward/back, a simulated
  cloud revert; assert live tree, store entries, `ops.jsonl`, oplog, journal edges and
  `op_outcome` together; run in CI.

### V-12 minor - 6a's D5 -> D1 requests are missing, and the busy-target rule would hide D5's own verify scan

- **Location:** 01 6a item 1; D1 2.6.2, 2.6.6 ("confirmed = the next observation holds the placed
  pair"); D5 2.7.2 (open = last event `writing`/`validated`/`failed`), 2.8 (`observe(src="verify")`
  runs before `done`), 2.12.1, 2.16.
- **Problem:** D1 skips no busy targets and confirms immediately. If D1 skipped
  `busy_targets()`, D5's post-write `observe(src="verify")` would be skipped too (its op is still
  open, last event `validated`).
- **Consequence:** the post-write state is not journaled at the op; D1 and D5 disagree on
  "confirmed".
- **Fix:** `observe(scan, *, src, skip)` with D5 passing busy targets minus its own op on
  `src="verify"`; D1 2.6.6 adopts D5 2.12.1 (or leaves "confirmed" to D5 only).

### V-13 minor - Lock rules disagree

- **Location:** D1 2.3 (lock held for milliseconds; `LockTimeout` -> organizer read-only; per-process
  `RLock` re-entrant); D3 2.13 and Deviations (same lock held for a whole add or fold entry); D5
  2.16 ("D1's lock is not re-entrant").
- **Problem / consequence:** a slow-media capture or fold entry in one process can push the other
  process's organizer into read-only mode for the session; re-entrancy is stated both ways.
- **Fix:** D3 holds the lock only for the manifest commit (the long hold exists for GC, which
  keep-everything does not need); a `LockTimeout` fails one transaction with a retry notice, not
  the session; state re-entrancy once in D1.

### V-14 minor - D1's FactsProvider does not match D4's IndexService

- **Location:** D1 2.10 (`sort_value`, `request(vids, priority: int)`; `build()` asks priority 0,
  then 1); D4 2.10.1 (`Priority`: 0 INTERACTIVE, 1 LIVE, 2 TIP), 2.12 (no `sort_value`;
  `changes -> ChangeSet | None`; `is_milestone -> bool | None`).
- **Consequence:** every read-model build queues all representatives at INTERACTIVE and takes the
  reserved worker an opened version card needs; `facts:<name>` sorting has no implementation.
- **Fix:** D1 uses D4's `Priority` (TIP for representatives, BACKFILL for other heads), sorts via
  `FactsView.numbers`, and takes D4's return types.

### V-15 minor - Model fields referenced but not defined

- **Location:** D2 1 and 2.5.3, D3 2.3 use `MetaSnap.meta_sha`; D1 2.2 `MetaSnap` has none. D1
  2.11 `Location.slot` "None for a store object" vs D3 Deviations (store locations carry slot and
  member).
- **Fix:** add `meta_sha` (sha256 of the meta plaintext) to `MetaSnap` - needed now because F5b
  gives every placed version a meta-only variant; adopt D3's store `Location` in D1 2.11.

### V-16 minor - D6 calls APIs nobody provides

- **Location:** D6 2.12 and 3.2 ("Keep a copy in the vault now" -> "D2/D3 preserve via D5"); 8.3
  request 1 (`execute(note=...)`); 1 dependency table (`housekeep()`, a D2 API).
- **Fix:** D5 adds a vault-only "keep a copy" entry point (plan + execute, no live write) and a
  `note` argument; D6 alerts read D3 fsck / `state/damage.json` instead of `housekeep()`.

### V-17 minor - D6 assumes the post-migration vault in every phase

- **Location:** D6 header ("every vault copy is durable - no 'may be pruned' states"), 2.2 State
  vocabulary, Appendix ("storage state is F3-B after migration").
- **Problem:** fold is phase 5; until then D1 reports outside-vault copies (the hand-made in-place
  backup folders) and, unless V-10 lands, snapshot-only copies.
- **Fix:** keep "outside the vault" (and "only in auto-snapshots" if pruning survives) until fold.

### V-18 minor - Web-option residue in D1-D6

- **Location:** D4 2.8.6 (web endpoints), 2.4.6 (`hex64` "JavaScript"), 6 step 17 ("web endpoints
  (D7)"); D5 1.3, 2.2 ("web UI option W"), 6 step 2.15 ("until D6/D7 land"); D1 2.10 ("JSON-safe
  for a web UI").
- **Problem:** none is a functional dependency (checked by search); they invite HTTP work. "D7" in
  D2/D4/D5 often means DESIGN.md D7 (environment as arguments), which stays.
- **Fix:** delete the web bullets and step 17's web half; keep `to_dict()` for `--json`.

### V-19 minor - 01-consolidation contradicts its own 4a

- **Location:** 01 6a item 3 (barrier), C2, C14, C15 tier 3, section 5 phase 0 ("P1-P11": omits
  P12, which D5 makes a prerequisite of every Xbox operation; P13 depends on Q2), phase 5 "(if
  chosen)", section 4 "lean" column.
- **Note:** with inference gone, phase 4 depends only on phases 1-2 (recorded edges), and with F7b
  phase 2 no longer depends on phase 3; the stated order stays coherent.
- **Fix:** see section 3.

### V-20 minor - D3 keeps deletion machinery that "keep everything" does not need

- **Location:** D3 2.11 (trash, purge, tombstones, GC), 2.12, 3.2 R-B/R-C, 4.3 unfold/downgrade, 6
  B-9, B-13 (shadows for reclaimed entries), B-14, B-15, B-17; tests T9-T11, T16-T19.
- **Problem:** under F9 the only deletion is reclaim of folded legacy folders (M2). D3 itself ranks
  "a bug in GC, retention or reclaim deletes many versions" as its second risk.
- **Fix:** scope D3 phase 5 to fold, reclaim, store merge and fsck; mark the rest "not planned".

### V-21 minor - The suggested empty slot is labelled as at risk

- **Location:** D5 2.3.4 (empty slot suggested first); D6 2.4 notes column; mockup 03 ("Suggested:
  empty - may be refilled by Steam Cloud").
- **Problem:** R8 says overwrites stick under Steam Cloud; the refill risk applies while a slot
  stays empty, not after a Place into it. The row tells the owner his suggestion is risky.
- **Fix:** word it "emptied outside the app - checked after the next launch"; D5 verification
  (2.12) catches a refill; confirm once in an owner run.

### V-22 minor - Classic writes in phases 0-1 are not serialised with each other

- **Location:** D5 2.16 (live-op lock only inside `execute`); D1 2.6.7 (phase-1 hook); D6 2.11
  (classic tab writes until step 19).
- **Problem:** GUI and CLI (or two app instances) can write one live folder at once until phase 2
  re-routes the classic ops; the phase-1 hook journals both. Pre-existing, cheap to close.
- **Fix:** phase 1 wraps `repopulate_slot`, `restore_full`, `clear_slot`, `promote_member` and
  `undo_last` in the same `.liveop.lock`.

## 3. Required document updates

**01-consolidation**
- [ ] 4a F3: point at D3's copy-aside step; record the Q2 outcome.
- [ ] Section 5: phase 0 = P1, P2 (stop pruning, V-10), P3-P8, P12; P13 per Q2; phase 2 names the
      preserve provider and the copy-aside (Q1); phase 5 = fold/reclaim/merge + F6.
- [ ] 6a: item 1 marked done in D1 (V-12); item 3 per Q2; add the V-09 seam.
- [ ] C2, C14, C15, C19: align with "no guessed tree" (V-03..V-05).
- [ ] Add the cross-design integration suite (V-11).

**D1-core-model**
- [ ] Remove 0 item 5, 2.8, `infer.py`, O-D1-1, steps 13-14, 2.12 phase-4 text, 2.14 item 8, test row 8 (V-03).
- [ ] `history()` = drawn edges only, rest dated (V-03; D6 8.3 request 3).
- [ ] 2.10.1 `cross_platform` without ancestry (V-04).
- [ ] 2.5 / 2.12: store entries as a capture source (V-09).
- [ ] 2.6.2 `skip` parameter; 2.6.6 "confirmed" semantics (V-12).
- [ ] 2.3: lock timeout per transaction, re-entrancy; replace the `storage.json` (D2) row (V-13).
- [ ] 2.2 `MetaSnap.meta_sha`; 2.11 store `Location` (V-15).
- [ ] 2.10 `FactsProvider` aligned with D4 (V-14).
- [ ] 1 dependencies: preserve per Q1, P2 per V-10; 2.6.7 hook takes `.liveop.lock` (V-22).

**D2-storage-index-over-folders**
- [ ] Header: not chosen; only `FolderLocator` (read side) and durability classes are used, as B's
      legacy reader until fold. Mark preserve, manifests, retention/rescue, cache compaction,
      repair-paths and 4.2 old-app work as superseded (V-02, V-10).

**D3-storage-version-store**
- [ ] Add the copy-aside step, rollback rule and tests (V-01).
- [ ] 3.4 and 6: record Q1.
- [ ] Remove shadows, stubs, `compat.py`, V0-V2, unfold/downgrade, B-9 (or reduce to the barrier
      per Q2), T16-T18, and the owner-summary bullet on older versions (V-06).
- [ ] 2.13: lock only for the commit (V-13); phase 5 scope (V-20).
- [ ] Listing interface for D1 and the classic view (V-09); record Q7.

**D4-content-index**
- [ ] Drop the `lineage` extractor, table, `lineage_signals`, 5.8 (V-03).
- [ ] Drop `owner_digests`, `owner_status`, O14 unless Q6-b (V-08).
- [ ] 2.11: the rule for versions without a parent (V-05); remove web bullets (V-18).

**D5-live-operations**
- [ ] 0 item 5, 2.2 `Policies`, 3.2, 3.3, 2.11 undo stamp, phase 3.1 (V-08).
- [ ] Remove Evict and every deletion path; add the empty-slot info code; new invariant test (V-07).
- [ ] Re-point from D2 to D3: 1.3, 2.1, 2.6.1, 2.7.2 example, 2.7.3, 2.8, 4.2, I12, K5, K6, step
      2.16, T-O1, T-O2, phase-2 "needs" (V-02, V-06).
- [ ] Keep-a-copy entry point and `note` argument (V-16); integration suite (V-11); web residue (V-18).

**D6-ui-tk**
- [ ] 2.2 Note column, 2.4 item 4, 2.5 right panel: what-changed rule (V-05).
- [ ] State vocabulary: Steam/Xbox words per Q4; outside-vault states until fold (V-04, V-17).
- [ ] Mockups 01, 03, 04c, 08: "Steam ahead", "diverged", "cross-save", by-date "what changed",
      the #19 link per Q3, the empty-slot row wording (V-04, V-05, V-21).
- [ ] Dependencies: `housekeep` -> D3 fsck; keep-a-copy; `note` (V-16).

**D7-ui-web**
- [ ] Header: not chosen (F1 = Tk), kept for reference; nothing in D1-D6 depends on it.

## 4. Questions for the owner

| # | Question | Options | Recommendation |
|---|---|---|---|
| Q1 | When does the version store start? | (a) with phase 2: new backups, snapshots and copies of evicted saves go to the store; old folders untouched; the vault is copied aside first; folding old backups and freeing space in phase 5. (b) in phase 5: phase 2 keeps evicted saves as plain folders (storage-A code), store and copy-aside later | **(a)**: one way to keep evicted saves instead of two; safety snapshots cost ~0 MB instead of ~50 MB per move; the copy is taken before anything has moved |
| Q2 | Older app versions after the upgrade | (a) as decided, no protection: an older version's Undo restores an older whole-folder snapshot (all 15 slots + account data; verified in today's code). (b) keep one guard entry so an older version's Undo refuses (tested on v0.0.9-v0.2.8); nothing else of the compatibility work | **(b)** |
| Q3 | Links for backups made before recording starts | (a) only the app's own move log (the 2026-10-05 rollback). (b) also consecutive app-made backups of the same live slot (130 links; the same kind of evidence as the live journal, only less often; a rollback played over between two backups looks straight). (c) none | **(b)**, labelled "seen in backups": observation, not a guess, and it gives most existing versions a parent and a "what changed" |
| Q4 | Steam vs Xbox state of a save live on both | (a) compare the two live copies by play time ("Steam copy has 3h12 more play"); "diverged" only when recorded history proves it. (b) allow one marked estimate for this label. (c) show nothing | **(a)** |
| Q5 | "What changed" for a version with no known parent | (a) show nothing ("no recorded parent"). (b) compare with the previous backup by date, labelled as such (can report bases gone and back across branches) | **(a)** |
| Q6 | Warning for another account's save (F7b) | (a) one warning whenever the save comes from another account. (b) also detect "someone else's save", which needs hashed ownership ids in the index | **(a)**: simpler, no ownership data processed |
| Q7 | Store details still open in D3 | fold old backups into the store after the copy-aside (M1, +0.5 GB, deletes nothing); free space (M2, 3.4 -> ~0.5 GB) automatically or on request; thumbnail `cache/`: keep deduplicated (+139 MB) or drop; hand-made in-place backup folders: offer to copy in (+73 MB) | **M1 automatic, M2 on request after a deep check; keep thumbnails; offer the copy-in, default off** |
