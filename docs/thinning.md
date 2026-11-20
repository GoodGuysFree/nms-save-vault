# Thinning the vault: design (to-do 21)

GGF (2026-11-17): "Do we have a way to cull very old backups from the vault? Or a way to at least
dilute the older backups? Mark specific ones as pinned, so they never get culled?" and "I think
vault culling, thinning and pinning should probably be a core feature, exposed as both a
NMSSaveVault feature and SC feature." Decided 2026-11-20 (Q1-Q5 below, GGF's words quoted).

This document is the specification for the core (`nms_save_vault.core`), then the two apps.
Deleting is the one thing the vault never did by itself: everything here errs toward keeping.

## Decisions

**Q1, the schedule.** For each playthrough, by the time each version was saved:

| Age | Kept |
|---|---|
| the last 7 days | every version |
| 7 to 30 days | one per day |
| 1 to 6 months | one per week |
| older | one per month |

Always kept, whatever its age: the newest version of each playthrough; a pinned version or entry;
a version with a note; anything an undo or a Savecraft change still needs. GGF: "Keep that
schedule, but if the user wrote a note, keep it anyway (can count for the one per <period>
calculation)." A noted (or pinned) version is the one its period keeps when there is one.

**Q2, pins.** A pin keeps a version (one save of one slot at one moment) or an entry (a whole
backup: every save in it). Set with right-click Pin / Unpin in NMS Save Vault's Backups list and
History, and in Savecraft's History page; shown with a pin mark. A version pin is the existing
version-note lock (`organizer/notes.py`, `locked`: "the owner's keep mark"). An entry pin is new.

**Q3, what goes: whole entries only.** An entry is deleted only when every save in it may go by
the schedule. One save worth keeping keeps the whole entry. Manifests are never rewritten. An
entry with a label the user wrote counts like a note: it is always kept.

**Q4, when: only when asked, with a reminder.** "B. And we must show what we're going to
delete." A **Thin the vault...** action plans first and shows every entry it would delete; nothing
is deleted until the user confirms. A status-bar reminder appears when the vault passes a size the
user sets (Settings).

**Q5, where and how it shows.** NMS Save Vault: the button by the Backups list, the reminder size
in Settings. Savecraft: the same on its History page; the core does the work. The list: one row
per entry to delete (date, kind, its saves, the space it frees, why it goes), every row ticked,
untick to keep; "Delete N entries (frees X GB)" and Cancel (Esc).

## The core

### Planning (read only)

`plan_thin(vault_root, *, now=None) -> ThinPlan`. Nothing is written.

1. **Versions.** Every member of every store entry is a version of a playthrough: `(pid, vid)`,
   saved at `meta_ts`. A vid may be held by several entries (the store deduplicates content).
   Members without a pid group by slot and platform account. The account files (`accountdata`)
   are versions of their account, on the same schedule.
2. **What the schedule keeps.** Per playthrough, sort its versions by `meta_ts`; keep every
   version of the last 7 days; in each older day, week (ISO) or month bucket, keep one: a pinned
   or noted one when there is one, else the newest of the bucket. Always keep the newest version
   of each playthrough.
3. **What must stay, whatever the schedule:**
   - every version locked or noted (`notes.json` versions: `locked`, or a non-empty label or
     comment);
   - every entry pinned (new: see Pins) or labelled by the user (an empty label, or one the app
     wrote, is not the user's: `auto: ...`, `pre-...`, the labels the apps write; list them
     exactly in code, from the code that writes them);
   - every entry an operation names: a snapshot an op's undo restores (`ops.jsonl` events'
     `snapshot_id`) and an op's preserve;
   - every vid an operation that can still be undone needs (its evicted and kept versions,
     `organizer/locator.py`), unless another entry that stays holds it;
   - the snapshots NMS Save Vault's whole-folder Undo walks back through (`liveops/panic.py`
     candidates, `_not_undo_targets`): never delete a snapshot that would change where the next
     Undo lands;
   - entries recorded in `store/state/migration.json` fold records (`store/fsck.py`
     `_check_migration` fails when they are missing): skipped, reported as such, unless the
     run also records them as thinned there in a way fsck accepts (see Recording);
   - the pre-upgrade copy and anything `store rollback` needs (`rollback.py`).
4. **Entries.** An entry may go when none of the above holds for it and each of its versions
   is either not kept by the schedule or held by another entry that stays (iterate to a fixed
   point: deciding one entry stays can make another's version unneeded, never the reverse).
5. **Space.** For each entry that may go, the bytes freed are the objects only it references
   (counted against every entry that stays), plus its manifest.

`ThinPlan`: the candidates (id, kind, created time, label, its saves as slot/name, bytes freed,
the reason in plain words: "a newer one that week is kept"), the entries kept that the schedule
alone would drop (with why: pinned, noted, needed by an undo, a fold record), the totals.

### Running

`run_thin(vault_root, plan, chosen_ids, *, progress=None, cancel=None) -> ThinResult`.

- Re-plan under the vault lock first: an entry chosen that may no longer go (a new op, a new pin,
  a new note since the plan) is left and reported.
- Write `store/state/thinning.json` before touching anything: each entry `thinning`, then
  `thinned`. An interrupted run resumes or finishes at the next start, as `store/free.py` does.
- Delete the manifests, then the objects no remaining manifest references (counted again under
  the lock), then the catalog entries for them if the catalog lists any.
- Never touch a live folder, the pre-upgrade copy, an object another entry references, or a
  file outside the store.
- After the run, `fsck` (quick) must pass: covered by tests.

### Recording

Thinned entries are recorded (`store/state/thinning.json`: id, when, why, bytes) so the History
and Backups lists can say "thinned on <date>" for an operation whose snapshot went, and so fsck
accepts a fold record whose entry was thinned (`_check_migration`).

### Pins

- Version pin: the version-note lock (`Notes.set_version_note(..., locked=True)`); exposed in the
  API as pin/unpin a version. A lock keeps its note's whole pair (the head and its partner).
  Unpin takes off only the version's own lock (the note it heads); a save kept only by its
  partner's lock is refused (ValueError: unpin that one), never unlocked behind its back.
- Entry pin: new, kept in the organizer's `notes.json` (`entries: {id: {"pinned": true, ...}}`),
  journaled and conflict-safe like notes.
- API: `pin_version`, `unpin_version`, `pin_entry`, `unpin_entry`, `pinned_entries()`,
  `pinned_versions()`; each refused while the vault is read-only.

### The reminder

`app_state` keeps the size the user set (bytes; 0 = off). `vault_size(vault_root)` reports the
store's size from the manifests (`VersionStore.stored_bytes`), so the status bar does not walk
the objects.

### As built (core, `store/thinning.py`)

Where this design was silent, the choice that keeps more:

- Periods by each version's own age: under 30 days by day, under 183 days by ISO week, then by
  month, in the time zone of `now`. A version's time is the newest `meta_ts` any entry records
  for it; a version without one stays. An account's data file is dated by its file time. Of
  versions of one time (an edit keeps the meta's time) the one the vault first kept later is
  the newer; versions alike in both all count as the newest (each stays).
- Every other file an entry holds counts on its own (a save kept without its partner, a file
  of the folder the game does not know): one copy of each stays. A save's own files, the
  account's, the Xbox index and the game's thumbnail cache do not count apart.
- A version that stays keeps the newest entry holding it, unless an entry that stays anyway
  holds it (no smallest-cover search). Versions in a pinned, named or noted entry count as the
  owner's pick for their period.
- The labels the apps write are listed exactly, from the code that writes them (`auto: <op
  kind>`, the early `auto: pre-restore of ...` and its kin, `kept: ...`, the whole-restore
  label, and, checked against the entry, `slot N from <its folder>`, an import's folder name,
  the offered backup label, `edited by <app>: <what>`); any other label is the owner's.
- Old-layout catalog entries: a store entry the catalog also lists stays while its old folder is
  in the vault, while it is a copy-in of the owner's own folder, or when the catalog entry has
  a note; a leftover catalog entry goes with its store entry.
- Operations: an open op (and one recovered but not closed, or left part-way by the owner)
  keeps everything it names, and every entry carrying its op id. Of a finished op: a
  whole-folder restore's snapshot, the preserves and kept copies, a keep op's copy, the
  versions it evicted, an unlock's earlier account data. A place's own safety snapshot may go.
- The run refuses: a store the quick check finds damaged (checked before an interrupted run is
  finished too), a manifest that is damaged or cannot be read just now (checked again under
  the lock before each entry and before its objects go), a journal, notes file or format of a
  newer app, notes that do not parse or a single note or pin that does not read, a thinning
  record that does not read, another window holding the live-op or upgrade lock. A file held
  open by another program is waited for 2 s, then the run stops (ThinError) and the entry is
  finished at the next start. Only the thinned entries' own objects are deleted;
  objects nobody named before (an interrupted capture's) stay. A commit checks its objects under
  the vault lock, so a capture racing a run commits nothing rather than a broken entry.
- The object store keeps no delete method; the one deletion of an object is in thinning.
- `app_state` is `AppState.thin_reminder_bytes`; a hand-edited value that is not a size is off.

## The apps (after the core)

- **NMS Save Vault:** Thin the vault... by the Backups list (plan in a worker thread, the list
  window, run with progress); Pin / Unpin on right-click in Backups and History, the pin mark;
  the reminder size in Settings and the status-bar line; `nmsvault-cli store thin [--dry-run]`.
- **Savecraft:** the same action on the History page through the API; Pin / Unpin there.

### As built (NMS Save Vault)

- **The list window** (`ui/thinning.py`), from the button right of "Save file:" above the
  Backups list and from Tools: plans in the UI worker; one ticked row per entry that goes (date,
  kind, its saves, the space it frees alone, why); a click or Space unticks; below, the entries
  kept that the schedule alone would drop, with why. "Delete N entries (frees X)" counts the
  ticked rows with `ThinPlan.freed_bytes`. A blocked plan, or one that cannot be made (an
  old-layout vault), says why and offers no Delete. The run goes in the worker with a progress
  bar and Stop (between entries); then each row reads deleted or kept (and why) and the window
  says what went and what was left. Esc and Cancel close it, never while it deletes. The
  Backups list and the status bar read the vault again after a run.
- **Pins:** right-click a backup: "Pin (thinning never deletes it)" / "Unpin"; a pinned row reads
  "<id>  (pinned)". In History the version note's lock is the pin: its button reads Pin /
  Unpin, a right-click on a version offers the same, and a pinned row's note starts
  "(pinned)". Both go through the core's pin functions in the worker, so a read-only vault
  refuses them; the refusal is said (a message box; the Organizer's alert bar for History).
- **The reminder:** Tools > Settings... holds the size in GB (0 or empty: off). While it is on,
  the listing worker reads `vault_size`; past the size, the status bar starts with "The vault
  holds X, past the Y you set: Thin the vault... by the Backups list shows what could go."
- **At start**, and when another vault is chosen, `finish_thinning` runs in the worker when a
  run is pending; it says what it finished, and a failure is said, never fatal.
- **"Thinned on <date>"** is in the operations log (History of this slot..., Show in history):
  the record names an entry's operation, not its versions, so a move whose backup went reads
  "(its backup was thinned on <date>)".
- **CLI** `store thin`: prints what goes and what stays against the schedule, then y/N (`--yes`
  skips it, `--dry-run` stops before it). A blocked plan exits 1, and so does a declined prompt,
  as the other store commands do. A pending run is finished first (not on `--dry-run`).

## Tests the core needs (made-up vaults only)

- the schedule: buckets at 7 days, 30 days, 6 months; the newest of each playthrough kept; a
  noted or pinned version chosen as its bucket's one; a version with a note older than the
  schedule kept;
- whole entries: an entry with one kept save stays; an entry whose saves are all held by other
  entries that stay goes;
- every keep rule above, one test each (op snapshot, undo vids, Undo walk, fold records,
  labelled entry, pinned entry, the pre-upgrade copy);
- objects: only unreferenced objects are deleted; a shared object stays;
- a run re-plans under the lock and leaves an entry pinned since the plan;
- an interrupted run (faults at each point) resumes and ends with fsck passing;
- the plan writes nothing (fingerprint the vault before and after);
- the API pins round-trip and are refused read-only; the pinned-API names pinned in
  `tests/test_core_api.py`.
