# D6 - UI option T: the organizer in the Tk app

Design wave, UI design D6 (subject S7, owner fork F1 option T; F2 history style). Branch
`design/save-organizer`. Binding inputs: `docs/organizer/01-consolidation.md` (C1-C32, sections 6
and 7), research R2, R3, R4, R6, R7, R8, and the final designs D1 (read model), D4 (content index)
and D5 (live operations). Nothing under `src/`, `tests/` or `packaging/` was changed.

Mockups: `docs/organizer/mockups/tk/*.png`, rendered by real Tk 8.6.15 / ttk widgets (the app's own
`theme.py`), DPI-aware at 150 %, from `mockups/sample-data.json` (the owner's real 30 save files and
110 versions). Notes, pins and the "Vesper Ridge" side branch (#20-#22) are invented sample content;
every image says so in its caption band. Scripts: `mockups/tk/source/` (throwaway, not app code).

Owner decisions taken while this was written (01-consolidation 4a), applied here and in the
mockups: F2 = **C** (version table with a lane column and a key / all toggle); **no guessed tree** -
branches are drawn only for history the app recorded (operations and the live journal), versions
from before that are a flat dated list; F3 = **B** (version store; the vault is copied aside once
and migrated, so every vault copy is durable - no "may be pruned" states); F4 = **a** (a version
goes live with its own restore-point partner); F5 = **b** (the game's save date is stamped "now";
the original date stays in the vault record); F6 = Steam <-> Xbox later, after test runs (shown as
not available yet); F7 = **b** (another account's save is allowed with a warning); F8 = operations
always replace, emptying a slot is not an app action (the app shows the manual steps). F1 (Tk or
web) is still open.

## 0. Owner summary

1. One window: all 30 save files in a sortable table, the 15 Steam + 15 Xbox slots beside it, and the selected save file's facts and notes below (01).
2. A line opens into its versions: branches drawn for what the app recorded, older versions as a dated list; Move to live is one dialog that lists all 15 slots, suggests one and names the result on its button (02, 03).
3. Strengths: native, fast (12k rows in 0.15 s), one language, no local web server; reuses today's theme, tests and look.
4. Limits versus a web UI: a cell cannot have its own colour (warnings are words and row icons), plainer graphics, and notes in Hebrew/CJK/emoji cannot be drawn by the Linux build.
5. Your choice: shell (ii) organizer-first window (recommended) or (i) organizer as a tab beside today's panes (10a, 10b).
6. History is style C as you decided (04c); Tk 9 later can add per-cell colours without a redesign.

## 1. Scope, non-goals, dependencies

**In scope.** The Tk front end of the organizer: both window shells; the organizer view (table,
filter chips, find, column chooser); the live board; the detail pane; the Move to live / Replace
with dialog; the history window (style C); the notes window; the compare window; first-run and
indexing progress; the non-modal "Done - Undo"; verification alerts and the recovery prompt; the
classic view; the DPI, theme and zoom pass; redaction of every displayed string; the UI threading
model; UI tests.

**Non-goals.** Identity, versions, lineage, notes storage and the read model (D1); storage and
pruning (D2/D3); decoding and facts (D4); operation semantics, plans, journal, undo, verification
(D5); the web option (D7); Steam <-> Xbox transfer (F6, later); fixing non-Latin-1 drawing on the
Linux kit (no Tk build can, R3 K4).

**Dependencies.** The UI consumes only these (01-consolidation section 6); it never reads a save
file, a catalog entry or a live folder, and never chooses a write target itself.

| Needs | From | Used for | Phase |
|---|---|---|---|
| `ReadModel.lines()`, `line(pid)`, `sort_lines`, `LineFilter`, `live_board()` (D1 2.10) | D1 | table rows, chips, board | 1 |
| `versions(pid)`, `history(pid, mode)` with `GraphNode.row/lane`, `pass_through`, folding, key mode (D1 2.10.2-2.10.5) | D1 | version rows, lane column, history window | 1 (rows), 4 (lanes) |
| `Notes`: `save_note`, `set_save_note(pid, text, base_rev)` + `NoteConflict`, `note_text_at`, `version_note`, `set_version_note`, `set_pinned` (D1 2.9) | D1 | notes window, pins, labels | 1 |
| `LiveTarget.token` (opaque) and `resolve(ref)` | D1 | everything passed back to D5 | 1 |
| `IndexService.view(vid)`, `card(vid)`, `changes(child, parent)`, `status(vid)`, `request(vids, Priority)`, `request_deep` / `deep`, `progress()`, `events_since(seq)`, `pause/resume/cancel` (D4 2.12) | D4 | facts columns, version card, "what changed", progress | 3 |
| `eviction_picker`, `plan_place`, `plan_swap`, `plan_promote`, `plan_undo`, `plan_panic_restore`, `execute`, `pending_recovery`, `resolve_recovery`, `update_verification`, `verification_items`, `op_history` (D5 2.2; `plan_evict` is not offered, F8) | D5 | every write | 2 |
| `housekeep()` report (damaged copies, rescues) | D2/D3 | start-up alert | 1 |

Prerequisites (phase 0, independent of the organizer): the **DPI pass** (`SetProcessDpiAwareness(1)`
plus scaling every pixel constant, R3 K5); **theme mappings** for `TNotebook`, `TLabelframe`,
`TSeparator` and the new palette keys (3.12); **P10** (the toolbar no longer fits; the menu bar of
3.1 fixes it); **P1** (Xbox meta: without it the board shows the wrong loaded member in 5 of 14 Xbox
slots, D1 risk 5); logging to a file under `pythonw` (R3 K6).

## 2. Information architecture and screens

### 2.1 Concepts, windows and navigation

Four words only (C32): **Save file** (a D1 `Line`), **Version** (a `VersionRow`, the captured
pair), **Live** (a board slot, "Steam slot 7"), **Vault**. Storage kinds (full, snapshot, extract,
store object) never appear as concepts; a version's places are dated labels ("Vault: backup
2026-10-05 17:41").

| Window | Kind | Opens from | Notes |
|---|---|---|---|
| Main window: **Organizer** view | root | start | shell (ii); in shell (i) it is a Notebook tab |
| Main window: **Backups (classic)** view | root | view switch, Ctrl+2 | today's two panes; whole-folder actions |
| Move to live / Replace with | modal Toplevel | Move to live... (Ctrl+M), board right-click, drag onto a slot | one dialog, both ends visible (C30) |
| History | non-modal Toplevel, one | History (Ctrl+H), line menu | follows the main selection unless "Keep this save file" is ticked |
| Notes | non-modal Toplevel, one | Notes... (F2), detail-pane links | edits the selected save file and version |
| Compare | non-modal Toplevel, one | Compare..., Ctrl+click a second version | |
| Recovery | modal | start-up, when D5 `pending_recovery` has a torn op | the only start-up modal (consequential) |
| Alerts | non-modal bars above the table | D5 `verification_items`, D2 housekeep | stay until dismissed |
| Columns, Accounts, Help, About | modal | menus | Accounts is today's `AccountsDialog` |

Selection is the hub: the table selection drives the board highlight, the detail pane, the History
window and the default source of Move to live. Keyboard: Tab cycles chips -> Find -> table ->
board -> detail tabs -> action buttons; arrows move inside each; Enter on a line or version opens
Move to live, on a board slot opens Replace with; Shift+F10 / the menu key opens the context menu;
Ctrl+F focuses Find; Ctrl+1 / Ctrl+2 switch views; Ctrl+plus / Ctrl+minus zoom. Every drag has a
keyboard path (R4 F2): dragging a line or version onto a board slot only pre-fills the dialog.

### 2.2 Screen 01 - the organizer (shell ii)

![01](../mockups/tk/01-organizer.png)
![01 dark](../mockups/tk/01-organizer-dark.png)

**Layout (top to bottom).** Native menu bar (File, Edit, View, Live, Tools, Help); a slim toolbar:
the view switch `[Organizer | Backups (classic)]`, the selection actions `Move to live...`,
`History`, `Compare...`, `Notes...`, and `Rescan` at the right (fits 1,000 px at zoom 1.2, P10);
the chip row; a horizontal `ttk.PanedWindow` with the organizer table on the left (weight 3) and,
on the right, the live board above the detail pane; the status bar.

**Chips** (toggle buttons with counts; groups combine with AND, chips inside a group with OR):
`All 30 | Live 26 | Vault only 4` - `Steam 14 | Xbox 15 | On both 4` - `Has note 6 | Pinned 2 | Not in
vault 8 | Expedition 7`. They map 1:1 to D1 `LineFilter` (`live`, `platform`, `cross_platform`,
`has_note`, `pinned`, `at_risk`, `expedition`); counts come from the unfiltered lines. `Find`
filters by `LineFilter.text` (name, former names, note text, version labels; case-insensitive) as
you type (debounced 150 ms). `Columns...` opens the column chooser.

**Table columns** (default set; widths in logical px; D1 sort key):

| Column | Line row | Version row | Sort key |
|---|---|---|---|
| #0 Save file / version | badge image + name ("(unnamed 9589)", "(unnamed, was Swarm)") | lane image + "#18" ("#1 (restore point only)" for a fork member) | `name` |
| Live (58) | "S9", "S13 X8", "vault" | "S9" or "vault" | `live` |
| Vers. (40) | distinct versions (C6) | - | `versions` |
| Play time (76) | "532h16" (`_fmt_play`) | the version's | `play_time` |
| Last played (112) | local "2026-10-15 23:39" | saved time | `last_played` (default, descending) |
| Mode (72) | difficulty, or "Exped. 23" | the version's | `difficulty` |
| State (126) | see vocabulary below | see below | `risk` |
| Glyphs (48), Parts (56), Legacy (68), Ships (42) | D4 `view(representative)` | D4 `view(head)` | `facts:glyphs` ... |
| Note (rest) | first line of the save-file note | label, else `auto: ` + D4 `changes().top(3)` | `note` |

Optional columns (chooser): Bases, Companions, Galaxies with bases, Story (Artemis), Atlas, base
computer, Quicksilver, Nanites, Journey rank, Frigates, Systems discovered, Game version, Branches,
Platforms. Any D4 numeric (`FactsView.numbers`) can be a column and a sort key.

**State vocabulary** (text, because a cell cannot be coloured in Tk 8.6): line rows - `NOT IN
VAULT` (a live pair with no vault copy), `diverged S/X`, `Steam ahead`, `Xbox ahead`, `vault only`,
`edited` (newest version edited outside the game), `pinned`, `placed 12:04, check pending`,
`reverted by cloud`; version rows - `NOT IN VAULT`, `branch point`, `branch tip`,
`abandoned branch`, `re-saved, no play`, `edited outside game`, `restore reverted`, `incomplete copy`,
`+1 re-save folded`. With the version store (F3-B) the states "snapshot only" and "outside the
vault" of today's folders disappear after migration.

**Badge** (column #0 image, drawn in code): two squares `[S][X]`, filled in the platform colour when
the line is live there, outlined when not; an amber dot when a live pair is not in the vault; a
clock when an operation on it waits for verification (09).

**Live board** (3.2): one column per live source (Steam, Xbox; more columns if more accounts), 15
rows each: slot chip `S9` in the platform colour (outlined and grey when empty), name, `+X8` when
the same save file is live on the other platform, play time, markers (filled amber triangle = not in
vault, pin = pinned, clock = waiting for the next game launch, red = reverted). The selected line's
slots are outlined in the accent colour. Header: "Steam live - 14 of 15 used". A legend lists only
the markers in use.

**Detail pane** (3.3): title, a one-line summary, buttons (`Move to live...` is the default button),
and a `ttk.Notebook` with **Summary**, **Facts**, **Notes**, **Where**. Summary for a line: each live
slot with its state, played/last played/mode/game version, expedition state, platform state,
versions and tips, the save-file note, and "Contents of the newest version" (D4 view, compact).

**Actions.** Click selects; double-click or Right expands a line; Enter = Move to live; right-click
a line: Move to live..., History, Compare with..., Notes..., Pin / Unpin, Combine with... (phase 4),
Copy name; right-click a version: Move to live..., Compare with..., Edit label..., Lock / Unlock,
Show in history, Split off as a new save file... (phase 4). Header click sorts (arrow image in the
heading); a second click reverses. Sort, chips, columns, sash positions and window geometry persist
in `state.json` (`ui` section).

**States.** *Scanning* (first scan ~1-3 s, later < 0.3 s, D1 0): status "Scanning live folders and
the vault..." with an indeterminate bar; the previous model stays on screen. *Empty vault*: the table
shows one muted row "No save files yet - File > Back up live folder now, or Import a backup folder".
*No live folders*: the board shows "No live save folders found - Live > Accounts...". *A source
cannot be read*: its board header shows an amber triangle and the reason (tooltip); its lines stay.
*Facts not read*: "..." cells (07). *Game running*: status "Game: RUNNING - moves wait"; dialogs open
but show D5's `GAME_RUNNING` block. *Errors* from any worker: an alert bar with the redacted message
(never a modal for a background failure).

**Reads / calls.** D1 `lines`, `sort_lines`, `LineFilter`, `live_board`; D4 `view`, `progress`;
D1 `set_pinned`. No D5 call.

### 2.3 Screen 02 - a line expanded

![02](../mockups/tk/02-line-expanded.png)

Expanding a line inserts its versions (D1 `versions(pid)` joined with `history(pid, "default")`).
Each row is one captured pair (C4), labelled by its head. The block has two parts (owner decision,
no guessed tree):

- **Recorded history** first, with lane images in column #0 (F2-C, 3.1.3): only versions joined
  by edges the app recorded (operations) or the live journal observed, plus the version an
  operation started from and the one it replaced. Main Save: #18 -> #23 (put live by the app on
  10-05 17:49, live in S9) and #18 -> #19 (the save it replaced, loaded by mistake and abandoned);
  #11 -> #20-#22 (the side branch, invented, started by an operation on 10-11).
- A separator row "Older, by date - 16 versions from before the app recorded history; listed by
  date, no links are guessed", then the **dated list**: one row per version, newest first, a grey
  dot instead of lanes (live versions keep their platform-coloured dot).

No-progress versions are folded (C5) into their older neighbour: All Parts Building (no recorded
history, so only a dated list) shows #4 (re-saved without play) folded into #3 ("+1 re-save
folded"). Version rows have a tinted group background so the expanded block reads as one unit.

Selecting a version fills the detail pane with the **version card**: where it is ("Vault: backup
2026-10-05 17:41 - not live"); "Comes from #p (put live by the app / seen in the slot)" for a
recorded edge, else "Before it (by date): #17 ... - history before the app recorded it is a dated
list"; "Led to #23, #19 - a branch point" (recorded children only); operation and journal events
(the reverted restore in amber); label and comment; "What changed since #17" (D4 `changes`); and
the contents (D4 `card`; the deep section loads on demand with a spinner after 150 ms, D4 2.10.4).

Keyboard: Right / Left expand and collapse; Up / Down move; Enter = Move to live for that version.
States: a version whose facts are not read shows "..."; an unreadable version shows "unreadable"
in State and cannot be a source (D5 `SOURCE_UNREADABLE`).

Reads: D1 `versions`, `history(pid, "default")`; D4 `view`, `changes`, `card`, `request(...,
INTERACTIVE)`, `request_deep`.

### 2.4 Screen 03 - Move to live (from a version)

![03](../mockups/tk/03-move-to-live.png)

One modal dialog, both ends visible (C30). It opens from a line (source = its newest version), a
version, the history or compare windows, or a drag onto a board slot (target pre-selected).

- **From** card: name, `#2 of 2`, saved, play time, mode, platform, where (D5
  `SourceSummary.where_label`), the version label. `Change...` opens the version picker of 03b.
- **Into**: a segmented `[Steam | Xbox]` account switch from `EvictionPicker.account` and
  `other_accounts`; a blocked account stays visible with its reason ("Xbox: moving saves between
  Steam and Xbox is not available yet", `CROSS_PLATFORM_GATED`). With two Steam accounts the switch
  shows their alias labels; the other account is selectable (F7-b) and its plan carries D5's
  `OTHER_ACCOUNT` confirm warning ("bases may stay owned by that account until you play it here"). Below it, a 15-row `ttk.Treeview` from `candidates`: Slot, In it now,
  Play time, Last played, Vault copy (`yes` / `NO - kept first`), Pin, and a reason column. Row
  tags: suggested (bold accent on a tint, "suggested: empty slot" / "same save file" / "in vault,
  played least recently"), not in vault (amber), pinned (muted, "pinned: not suggested"), empty
  (muted), blocked (disabled; selecting it is refused with the block text).
- **What will happen**: one paragraph built from the plan (`plan_place` for the selected slot,
  computed in a worker on every selection change, < 100 ms): what goes into the slot "with its own
  restore point" (F4-a), "The game will list it as saved now; its original date (2026-02-22 19:05)
  stays in the vault record" (F5-b), what leaves live and that it is in the vault. Then the plan's
  warnings by severity:
  `info` (blue i), `warn` (amber !), `confirm` (a checkbox the owner must tick; the main button stays
  disabled until all are ticked and their codes go into `execute(confirmed=...)`), `block` (red;
  button disabled).
- **Bottom**: optional "Note for this move" (recorded with the operation, request in 8.3), `Cancel`,
  and the default button whose text is `plan.outcome`: "Swap into Steam slot 5", "Put into empty Steam
  slot 1", "Go back to this version in Steam slot 13".

In the image the owner picked slot 5 over the suggested empty slot 1; CrossSaveTest-300qs is also
live on Xbox (D5 `LIVE_ELSEWHERE`), and Steam Cloud is on (`STEAM_CLOUD_OTHER_DEVICE`).

**Execute.** The button calls `execute(ctx, plan, confirmed, progress)` in the worker; the dialog
replaces its buttons with a determinate bar fed by D5 `Progress` (checking, preserving, snapshot,
writing, verifying, recording). Outcome: `done` closes the dialog and shows the Done bar (09);
`stale` shows "Steam slot 5 changed since you opened this. Please check again." and re-renders from
`replan`; `refused` / `rolled_back` / `failed` keep the dialog open with D5's text (2.14) and, for
`failed`, a `Recover...` button. Keyboard: arrows choose the slot, Enter executes when enabled, Esc
cancels. Calls: D5 `eviction_picker`, `plan_place`, `execute`.

### 2.5 Screen 03b - Replace with... (from a slot)

![03b](../mockups/tk/03b-replace-from-slot.png)

The same dialog class entered from the other end: right-click a board slot (or Enter on it) ->
`Replace with...`. **Into** is fixed (`Steam slot 5 - holds Ex-Beachhead-160qs #2 - 7h17 - in the
vault`, `Change...`); **From** is a version picker: an `OrganizerTable` with chips `Not live | Steam
saves | All`, Find, lines expandable to versions; save files that are not live come first; lines of
the other platform are listed muted with "Xbox save - cannot go to Steam yet". Choosing a version
runs `plan_place(source, fixed target)`; the result and the button behave as in 03. The board stays
visible with the slot outlined so the owner sees where the dialog came from. Calls: D5 `plan_place`,
`execute`.

### 2.6 Screen 04c - History (style C, the owner's choice)

![04c](../mockups/tk/04c-history-hybrid.png)

A non-modal window over one save file. Toolbar: `Show: [Key versions 8 | All 23]`, `Compare 2
selected`, `Move to live...`. Body: a `ttk.PanedWindow` with the version table on top and the
selected version's card below.

- The table is the organizer table's version layout (same columns, same rows as 02): the
  **recorded history** with lanes on top (Main Save: the 10-05 rollback fork at #18 with #23 live
  and #19 replaced; the side branch #11 -> #20-#22), then "Older, by date" and the **dated list**.
- `Older versions: [Key versions | All 16]` applies to the dated list: in Key mode only noted or
  locked versions, versions touched by an operation, live versions and the newest and oldest stay;
  each collapsed run becomes a row "#6-#16 - 10 more versions - click to show" with the play time
  it covers; clicking a run unfolds it in place.
- Lanes are D1's (2.10.5) computed over the recorded part only, so 02 and 04c draw the same tree.
  No edge is guessed or drawn dashed (owner decision); a version from before recording shows
  "Before it (by date)" in its card. Restores and reverted operations show in the State column and
  as amber lines in the card. Once the app records an operation on an old version (for example
  "Go back to this version" on #11 of Creative Primary, 08), that version and its future join the
  drawn tree.
- Selecting rows: click selects, Ctrl+click adds a second for Compare; Enter = Move to live.

Reads: D1 `history`, `versions`; D4 `view`, `changes`, `card`. Calls: none (writes go through 03).

**Styles A and B** (linear timeline; separate lane graph on a Canvas) were designed, considered and
rejected by the owner in favour of C; they are not built.

### 2.7 Screen 05 - Notes

![05](../mockups/tk/05-notes.png)

The notes window edits the two levels of C19 for the selected save file:

- **Save-file note**: a `tk.Text` editor (undo, word wrap). `Save note` calls
  `set_save_note(pid, text, base_rev)`; a `NoteConflict` (CLI or another window saved meanwhile)
  shows both texts and offers "Keep mine / Take theirs". The status line says whether the text is
  saved. Below it, **earlier revisions** (D1 keeps every revision).
- **Labels and comments of every version**: a list of all versions with label or comment (italic =
  the automatic "what changed"); clicking one loads it on the right.
- **This version**: `< Older / Newer >` navigation, `Label` entry with a live `37 / 80` counter,
  `Comment` text (<= 4,000), `Lock this version (never pruned, never deleted)`, `Save` ->
  `set_version_note(head, pair, pid, label, comment, locked)`.
- **Save-file note when this version was captured**: D1 `note_text_at(pid, version.note_rev)` diffed
  word by word (`difflib.SequenceMatcher`) against the parent version's snapshot; added text on a
  green tint, removed text struck through on red (Text tags; per-character styling works in
  `tk.Text`). Versions known only from the bootstrap have no snapshot ("no note snapshot - this
  version predates the organizer").
- **What changed in the game since the parent**: D4 `changes(child, parent).lines()`.
- **Where notes are kept**: in the vault (`organizer/notes.json`), never in the save files.

Text is stored as Unicode (C22). Windows Tk draws it; on the Linux kit (no Xft) see 3.14.
Calls: D1 `Notes` only.

### 2.8 Screen 06 - Compare two versions

![06](../mockups/tk/06-compare.png)

Two version cards (A and B) with `Swap A and B` and `Pick other versions...`; an info strip with the
nearest common ancestor from D1's graph ("#11, branch point; A played +6h14, B +3h36 since"); a
`[Differences only 11 | All facts 22]` switch; a fact table (Fact, A, B, Difference; differing rows
tinted - a whole-row tint is allowed in Tk); on the right "Named things that differ" (bases, ships,
named companions only on one side; bases in both with a different size) and "Since #11 on each
side" (the top D4 changes along each branch). Facts come from D4 `view` / `card` of both vids
(requested at `INTERACTIVE`); the field-by-field comparison is presentation logic in the UI, it does
not call `changes()` on non-adjacent versions (D4 rule N1). Calls: none.

### 2.9 Screen 07 - First run and indexing

![07](../mockups/tk/07-first-run-indexing.png)

Meta columns (name, live, versions, play time, last played, mode, state) appear after D1's first
scan; facts columns show "..." until D4 has read that version. A dismissible info banner explains
the first run once ("30 save files and 110 versions found in 1.2 s ... Versions from before today
are listed by date; from now on the app records how they connect ..."). A progress strip shows D4
`progress()`: a determinate bar (`done_bytes / total_bytes`), "14 of 110 versions - 4 worker
processes - about 20 s left - live saves and the save file you select go first", `Pause`, `Cancel`.
Rows update in place as D4 events arrive (3.9); the selected line jumps the queue (`INTERACTIVE`) and
its detail says "Reading now - moved to the front of the queue". Nothing freezes: the table, board
and dialogs work during indexing. When the game is running D4 drops to one worker; the strip says
so. Calls: D4 `request`, `progress`, `events_since`, `pause`, `resume`, `cancel`.

### 2.10 Screen 08 - Cross-platform lines

![08](../mockups/tk/08-cross-platform.png)

With the `On both 4` chip: Creative Primary (`S13 X8`, `NOT IN VAULT; diverged S/X`) and The
Cartographers (`S3 X14`, `Steam ahead`) are expanded. All their history predates recording, so both
are dated lists without drawn links (owner decision): Creative Primary's live versions #12 (Steam
slot 13) and #5 (Xbox slot 8) carry platform-coloured dots, the Xbox restore point #1 is a grey
diamond ("restore point only"). The platform state is a word in the State column and a sentence in
the detail pane ("Steam and Xbox copies went separate ways"), computed by D1 from the two live
heads; no cross-save edge is drawn. Exobiology1-600qs and CrossSaveTest-300qs complete the filter.
The board marks `+X8` on S13 and `+S13` on X8.

The dialog shows what Move to live warns for "Creative Primary #11" (an older Steam version): slot 13
is suggested (same save file; D5 suggests it even when pinned); the button reads "Go back to this
version in Steam slot 13"; warnings: `PLAY_TIME_AHEAD` as a confirm checkbox ("Steam slot 13 has 59
min more play than this version. That newer save (#12) stays in the vault."), `NOT_IN_VAULT`
(info), the other branch on Xbox slot 8 (D5 `LIVE_ELSEWHERE` plus D1's `diverged` state), and the
blocked Xbox account (`CROSS_PLATFORM_GATED`). The detail pane's Summary says "Platforms: Steam and
Xbox copies went separate ways".

### 2.11 Screen 09 - Done, Undo, waiting to confirm

![09](../mockups/tk/09-done-undo.png)

After `execute` returns `done`, no modal appears. A **Done bar** (banner colours) slides in above the
status bar: "Done. Moved 'CrossSaveTest-300qs' into Steam slot 5. 'Ex-Beachhead-160qs' is in the
vault. Waiting for the next game launch to confirm." with `Undo` (default), `Show in history`, `x`.
It stays until dismissed or replaced by the next operation; Edit > Undo (Ctrl+Z) keeps
`UndoOffer.label` ("Undo: put 'Ex-Beachhead-160qs' back into Steam slot 5"). `Undo` calls
`plan_undo(op_id)`; if the plan has warnings or confirmations (`PLAYED_SINCE`, `UNDO_STALE`) it opens
in the Move dialog, else it executes directly. Undoing a fill of an empty slot would need a
deletion, which the app does not do (F8): its Undo opens the "Empty this slot" steps instead.

The rows update incrementally (no full rebuild, no lost expansion): CrossSaveTest-300qs now `S5 X4`,
state `placed 12:04, check pending`, a clock in its badge; Ex-Beachhead-160qs `vault only`; the S5
tile shows the clock; the status bar keeps "Last change 12:04: Swap into Steam slot 5 - checked
after the next game launch". D5 `verification_items` drive this: `pending` -> clock; `confirmed` ->
clock removed (a tick in the operation history); `reverted` / `deleted` / `resurrected` -> an alert
bar ("Steam Cloud put the old save back in Steam slot 5. 'CrossSaveTest-300qs' is safe in the
vault." with `Put it back`, `Show steps`, `Dismiss`).

### 2.12 Screens 10a / 10b - the two shells

![10a](../mockups/tk/10a-shell-tab.png)
![10b](../mockups/tk/10b-shell-organizer-first.png)

- **(i) Organizer as a tab (10a).** Today's window stays: the 14-button toolbar, the "Active live"
  selector, and a `ttk.Notebook` whose first tab holds today's two panes and whose second tab is the
  Organizer view (chips, table, board, detail; no own toolbar, its actions are in its context menus
  and detail buttons). The old toolbar keeps acting on the classic tab only and is disabled while the
  Organizer tab is active.
- **(ii) Organizer first (10b, recommended).** The menu bar and slim toolbar of 01; the view switch
  shows `Backups (classic)`: today's panes for whole-folder backup, restore-all, import and delete,
  with an info strip. There is no "Active live" any more: each classic button asks which live folder
  it writes to (C29); Move to live and slot actions live in the Organizer.

### 2.13 Screens not mocked

| Screen | Content | Calls |
|---|---|---|
| Recovery (modal at start) | one row per `RecoveryItem`: labels, started, state text, choices `Finish the move` (forward) / `Put the slot back` (back) / `Leave it`; the recommended one is the default button; "Close No Man's Sky to finish" when the game runs | D5 `pending_recovery`, `resolve_recovery` |
| Empty this slot (manual steps)... | not an app action (F8): a non-modal window with the slot, what is in it and whether it is in the vault (a `Keep a copy in the vault now` button when it is not), then the numbered manual procedure for Steam Cloud / Xbox; the next scan shows the result | D1 `live_board`; D2/D3 preserve via D5 |
| Swap two slots... | pick the second slot on the board (or a 15-row list); one plan, two targets | `plan_swap`, `execute` |
| Make the other save the one the game loads | board slot menu; hidden when that member already loads (R2 D7) | `plan_promote`, `execute` |
| Operations log | `op_history`, newest first, per slot; Undo on any `done` row; verification ticks | `op_history`, `plan_undo` |
| Restore whole folder from a snapshot... (classic) | today's whole-folder undo as the panic restore, account named, accountdata kept unless ticked | `plan_panic_restore`, `execute` |
| Combine with... / Split off... / This version comes from... | owner corrections (phase 4); pick the other line or parent in an `OrganizerTable` | D1 corrections API |
| Column chooser | checklist of columns, up/down order, "Reset" | - |

## 3. Components in Tk

### 3.1 Organizer table (`ttk.Treeview`)

1. **Widget.** `RedactingTreeview` extended so `item(text=...)` and `set(...)` also pass through
   `aliases.redact` (R2: today only `insert` is guarded). `show="tree headings"`, `selectmode
   "extended"` (Ctrl+click = second version for Compare).
2. **Population.** Lines from `ReadModel.lines()` filtered by `LineFilter` and sorted by
   `sort_lines`; versions are inserted on expand (lazily). Rebuild cost is not an issue (R3: 1,413
   rows 39 ms, 11,954 rows 148 ms), but updates are **incremental**: a new `ReadModel` is diffed by
   pid / head vid and only changed rows get `item()` calls; expansion, selection and scroll position
   are kept by pid / vid (today's refresh loses them, R2 2.2).
3. **Lane images (F2-C).** For the recorded part of a line D1 gives `(row, lane)`, edges and
   `pass_through` (lanes computed over recorded and journal-observed edges only - owner decision).
   The UI converts them per row into segments: `up[lane]` (top half), `down[lane]` (bottom half),
   `diag[(from_lane, to_lane)]` (a branch joining its parent row) and the node glyph (disc; larger
   disc = branch point; ring = tip; platform-coloured disc with a white centre = live). A
   `tk.PhotoImage` per distinct row signature, width `lanes x 13 px + 6`, height **exactly** the
   Treeview `rowheight`, so the images join into continuous lines (VERIFIED: probe and mockups).
   Undrawn pixels stay transparent so zebra, group tint and the selection show through. Rows of the
   dated list get an image of the same width with only a grey dot (platform-coloured when live, a
   diamond for a restore-point-only fork member, three dots for a folded run), so the text column
   stays aligned. The separator row "Older, by date" has its own muted tag. Images are cached by
   signature and regenerated when the zoom or theme changes the row height or palette.
4. **Badges.** Column #0 of a line row: the `[S][X]` squares with a 5x7 bitmap glyph, plus the risk
   dot or the pending clock (3.1.3 rules). Tk 8.6 allows images only in column #0 (R3), so every other
   status is text.
5. **Tags.** `odd` (zebra), `ver` (group tint), `vmuted` (unfolded run rows), `run` (italic muted),
   `pending`, `event`. A tag styles the whole row (R3); no tag sets a font on version rows except
   `run`, so the lane column never leaks a font into other cells (R3's mono-font leak).
6. **Sorting.** Heading command -> `sort_lines(key, descending)`; versions stay newest first inside a
   line. The sorted heading shows an arrow image (heading images work in 8.6).
7. **Tooltips.** Today's `Tooltip`, but the text is built on hover (today 1,771 strings are built per
   refresh): full name and former names, live places, the note, the badge meaning.
8. **Drag.** `<B1-Motion>` beyond 6 px starts an `overrideredirect` ghost (alpha 0.9);
   `winfo_containing` over the board resolves the slot (R3 VERIFIED); the drop opens the Move
   dialog with that target selected.

### 3.2 Live board (`tk.Canvas`)

- Data: D1 `live_board()` (`LiveSource` x `BoardSlot`), merged with D5 `verification_items()` for the
  clock / reverted markers and `busy_targets()`.
- Drawing: per slot a rectangle, the slot chip, name (truncated with "..." to the measured width;
  full name in the tooltip), the other-platform marker, play time, markers (polygons and ovals, no
  image files). About 400 items; redraw on data or theme change only. Items carry tags
  `slot:<token>`; hit-testing by `find_withtag("current")` and `find_overlapping`.
- Interaction: hover tint; click selects that slot's line in the table; right-click menu: `Replace
  with...`, `Swap with another slot...`, `Make the other save the one the game loads`, `Keep a copy
  in the vault now`, `Undo the last change to this slot`, `Show history`, `Operations on this slot`,
  `Empty this slot (manual steps)...`; Enter = Replace with; arrow keys move a focus ring (the canvas
  takes focus; the ring is an outline item).
- More than two live sources (two Steam accounts): one column per source in a horizontally
  scrollable canvas; the header shows the alias label.
- Accessibility: every marker has a text equivalent in the tooltip and in the detail pane.

### 3.3 Detail pane

A `ttk.Frame` with title / summary labels (`anchor="w"`, `fill="x"` so a long name never centres
and clips), an action row (the default button drawn with `default="active"`), and a `ttk.Notebook`
whose pages are read-only `tk.Text` widgets with tags (`h`, `k` key, `warn`, `ok`, `note`, `link`,
`event`, `small`). Links are `tag_bind` clicks ("#17" selects that version). Pages: **Summary**
(line or version card), **Facts** (D4 `card`: every base with parts and galaxy, every ship, companions,
milestone checklist, inventory fill), **Notes** (read view + `Edit...`), **Where** (every location
label, the operation history of this save file).

Dark mode: `TNotebook` and `TNotebook.Tab` get background / foreground / `selected` / `active` maps
and `lightcolor` / `darkcolor` (3.12); without them the tabs were unreadable (R3). Verified in
01-dark.

### 3.4 History window (style C)

`HistoryWindow(tk.Toplevel)`: the version table (3.1) with the recorded tree on top and the dated
list below, a `Key / All` switch for the dated list, a card pane, and `Keep this save file` (stop
following the selection). The tree comes from D1 `history(pid, ...)` restricted to recorded and
journal-observed edges (01-consolidation 4a); the dated list is the rest of `versions(pid)` by
date. Key mode and unfolding runs are pure UI work on that list (no lanes are involved, so nothing
needs recomputing in D1). Zoom changes rebuild the lane images. 23 versions here; R3 measured
2,000-node graphs fine.

### 3.5 Notes editor

`tk.Text` (`undo=True`, `wrap="word"`, `maxundo=-1`), `ttk.Entry` for the label with a counter
bound to `<<Modified>>` / `<KeyRelease>`, a `ttk.Checkbutton` for the lock. Saves go through the
worker (the notes file write takes the organizer store lock). Limits are enforced before calling D1
(label 80, comment 4,000) with the counter turning amber at the limit. The diff view is a read-only
`tk.Text` with `add` / `del` tags. Unicode: see 3.14.

### 3.6 Move to live / Replace with dialog

`MoveDialog(tk.Toplevel)`, modal (`grab_set`, `transient`), sized from content (no fixed geometry:
R2 found the copy dialog clipping its buttons at zoom 1.2), resizable, with a minimum size. Two
modes: `from_version(source)` and `into_slot(target)`. It holds one `EvictionPicker` and the current
`PlacePlan`; it never computes suggestions, outcomes or warnings itself - every text comes from D5.
A `fingerprint` mismatch at execute returns `stale` with a `replan`, which the dialog renders.
Account switch: changing account calls `eviction_picker(ctx, source, account)` again.

### 3.7 Compare window

`CompareWindow(tk.Toplevel)`: two cards (`tk.Frame` with labels), the ancestor strip (D1 graph:
walk both ancestor chains), a fact `ttk.Treeview` (tag `diff` = tint, `same` = muted) built from two
`FactsView`s field by field, and a read-only `tk.Text` for named sets from two `FactsCard`s.

### 3.8 Progress, Done bar and alerts

- **Index progress strip**: a `ttk.Frame` with a determinate `ttk.Progressbar`, the `message`, `ETA`,
  `Pause` / `Cancel`; shown while D4 `progress().state` is `running` or `paused`, hidden when idle;
  the status bar keeps "Contents read: n of m".
- **Done bar**: the existing `Banner.TFrame` style; one at a time; carries the op id.
- **Alert bars**: a stack above the table for D5 `VerifyItem` alerts, D2 housekeep findings and
  worker errors; each with its actions and `Dismiss`; dismissing calls D5 / D2 to record it.
- The only modals are the Move dialog, Recovery, and the confirmations D5 marks `confirm` (inside
  the Move dialog as checkboxes, not as message boxes; R4 D7).

### 3.9 Threading model (C31)

- One Tk thread; **no Tk call from any other thread** (a debug-mode assertion wraps the widget
  helpers).
- **Decoding** happens only in D4's spawn worker processes. The UI polls D4 with
  `root.after(250, poll)`: `events_since(last_seq)` -> refresh only rows whose vid changed
  (`view(vid)` is a < 1 ms cache read) -> update the strip from `progress()` (D4 2.8.6).
- **Other work** (D1 scan and read-model build, D2 housekeep, D5 `eviction_picker` / `plan_*` /
  `execute` / verification, notes writes) runs in one `UiWorker`: a single
  `concurrent.futures.ThreadPoolExecutor(max_workers=1)` (I/O-bound: hashing, metas, file writes; R5:
  threads freeze Tk only for `json.loads`-heavy work, which never happens here). Each job returns a
  value into a `queue.Queue`; `root.after(50, drain)` delivers results to callbacks on the Tk thread.
  D5's `progress` callback also only enqueues.
- Jobs are serialised (one at a time) so a plan never races an execute; a busy job greys the
  dialog's button. A long D1 scan never blocks input: the old model stays on screen.
- This replaces today's `_run_worker` (modal "Please wait" plus a nested `update()` loop, R2 2.2)
  for organizer actions; the classic view keeps it until its flows are re-routed.
- `safety.is_game_running()` (tasklist, 175 ms on the Tk thread today, R2) moves into the worker and
  is refreshed every 10 s and before each plan.

### 3.10 DPI awareness

- `gui.main` calls `ctypes.windll.shcore.SetProcessDpiAwareness(1)` (system-aware) before `tk.Tk()`
  on Windows (R3 K5: Tk then uses scaling 2.0 at 150 %, fonts follow). Per-monitor awareness (2)
  needs `WM_DPICHANGED` handling Tk 8.6 lacks; system-aware is enough for one monitor and degrades
  to Windows' bitmap scaling on a second monitor with another DPI.
- `ui.px(n) = round(n * root.winfo_fpixels("1i") / 96 * zoom)` for **every** pixel constant: column
  widths, paddings, `wraplength`, canvas geometry, image sizes, dialog minimum sizes, sash positions.
  The mockups use exactly this (`mockui.Env.px`).
- Images (badges, lanes, arrows) are generated in code at the current scale; nothing loads a bitmap
  file except the window icon.

### 3.11 Zoom

Today's named-font zoom (0.7-2.0, Ctrl+plus/minus) stays. Additionally: column widths, canvas
geometry and images are re-derived from `px()` (which includes the zoom factor); the Treeview row
height follows the font (exists); lane and badge images are regenerated for the new row height;
dialogs re-measure. Native menus do not zoom (R2) - accepted.

### 3.12 Theming

`theme.py` gains palette keys (values used in the mockups, `mockui.EXTRA`): `steam`, `xbox`,
`warn`, `warn_bg`, `ok`, `muted`, `group_bg`, `zebra`, `lanes[4]`, `danger`,
`pending`, `hl_bg`, `event`, `diff_add`, `diff_del`, `card`, `chip_on`, `slot_empty`; and ttk maps
for `TNotebook`/`TNotebook.Tab`, `TLabelframe`/`TLabelframe.Label`, `TSeparator` (dark), plus the
styles `Card.TFrame`, `Muted.TLabel`, `Title.TLabel`, `Head.TLabel`, `Toast.*`, `Info.*`.
Classic widgets (Canvas, Text, chip Checkbuttons, segmented Radiobuttons, card Frames) register
with a small `ThemeAware` registry; a theme change calls each one's `apply_palette(P)`, regenerates
images and redraws canvases. `tests/test_theme.py` gains contrast pairs for the new keys.
Windows 11 dark title bar: `DwmSetWindowAttribute(hwnd, 20, 1)` on every toplevel in dark mode
(best effort, VERIFIED in 01-dark). The **native menu bar stays light** in dark mode (a Win32 menu;
Tk 8.6 cannot theme it) - visible in 01-dark; the alternative is an in-window bar of
`ttk.Menubutton`s (themed and zoomable, but no Alt-key access without extra bindings). Chips are
classic `tk.Checkbutton(indicatoron=False)` because the vista `Toolbutton` style draws unselected
chips as plain text (VERIFIED probe).

### 3.13 Redaction everywhere

Every string a widget shows passes `aliases.redact` (account ids appear in folder names). Helpers
replace direct widget calls: `RedactingTreeview` (insert, item, set, heading), `rtext(textwidget,
...)`, `rlabel(...)`, `ctext(canvas, ...)`, `rtitle(window, ...)`, plus the existing `_filtered`
message boxes. D1 and D5 already return redacted ASCII texts; redacting again is idempotent. A test
installs a fake alias for a fake account id, renders every view with fixture data containing that
id, and scans all widget texts, canvas texts and window titles for it.

### 3.14 Unicode and the Linux kit

UI strings stay ASCII (`tests/test_ui_text.py`). Data (names, notes, settlement names such as
"10ξρ111χυ Pass" in the owner's Main Save) is any Unicode and draws correctly on
Windows (04c). The Linux kit's Tk has no Xft and draws non-Latin-1 as garbage (R3 K4, Tk 9 too).
Design: on `tk windowingsystem == "x11"` a display filter replaces characters outside Latin-1 with
`?` in every displayed data string (table, board, cards, titles), and the notes editor refuses to
edit a note that contains such characters ("This note contains characters this build cannot show.
Edit it on Windows or with `nmsvault note`.") so saving never destroys text it could not show.

### 3.15 State and preferences

Window geometry, sash positions, columns (set, order, widths), sort, chips, the history mode, and
the last selected pid are stored in `state.json` under `ui` (per machine; not user data, so not in
the vault). Today's fixed `1000x640` start geometry goes; the first start uses 80 % of the work area
with a minimum of 1,000 x 640 logical px.

## 4. Options

### 4.1 Shell (i) or (ii) - owner's choice

| | (i) Organizer as a tab (10a) | (ii) Organizer first (10b, 01) |
|---|---|---|
| What changes | a Notebook around today's panes; the organizer is tab 2 | new main window; today's panes behind `Backups (classic)` |
| Mental models | two: "Active live" + 14 buttons beside an organizer that names targets | one: every write names its target (C29) |
| P10 toolbar | still overflowing | fixed (menu bar + slim toolbar) |
| Risk / size | smallest; the classic code is untouched | larger: the 1,392-line `App` is split into frames (mechanical) |
| Fit with phase 1 | good (read-only tab) | good |
| Fit with phase 2 | Move to live in tab 2 while the old Copy / Undo stay in tab 1 (two ways to write a slot) | one place to write slots |

**Recommendation: (ii)**, built as two frame classes (`OrganizerView`, `ClassicView`) inside a
thin `App` shell, so (i) is only a different shell around the same frames and remains a fallback.

### 4.2 History style (F2) - decided: C

C is built (04c, and the lane column in 02). A (timeline list) and B (separate Canvas lane graph)
were designed, considered and rejected by the owner.

### 4.3 Tk 9 later (R3 T1-B)

CPython 3.14.7+ on Windows ships Tk 9.0.4 (the newer PBS 3.12 runtime does too). Tk 9's treeview
adds per-cell tags (`tag cell add`) with images in data cells, `-striped`, frozen title columns,
cell selection and column separators; Tk 8.7/9 read SVG into photo images. For this design that
means: coloured State and Live cells (no all-caps words needed), the lane image could move into its
own column, built-in zebra, and SVG icons. Costs: a new Python target for the Windows kit, Tk 9
behaviour changes (TIP 714 states), and no fix for Linux fonts. To keep the upgrade cheap, all cell
styling goes through one adapter (`CellStyle` in the table class) that maps state -> text marker on
8.6 and state -> cell tag on 9.

### 4.4 Menu bar: native or themed

Native (recommended): standard Alt access, screen-reader friendly, light in dark mode and does not
zoom. Themed `ttk.Menubutton` bar: dark and zoomable, needs custom Alt bindings. Decide with the
owner only if dark mode matters to him.

## 5. Reuse and replacement map (against `gui.py`, R2 2.2 / 2.7)

| `gui.py` part (lines) | Verdict | In the new UI |
|---|---|---|
| imports, formatters `_fmt_ts`, `_fmt_play`, `_fmt_size`, `_fmt_created` (1-67) | keep | shared by all views |
| `Column`, `LIVE_COLUMNS`, `BACKUP_COLUMNS` (69-91) | keep for the classic view | organizer columns are a new table (3.1) |
| `SlotSource`, help dicts (95-125) | adapt | classic view only |
| `Tooltip` (131-196) | keep, adapt | text built on hover |
| `_source_caption`, `_restore_menu_label`, `_clear_question` (199-247) | adapt | captions keep; clear question replaced by D5 plan texts |
| `_filtered` + `RedactingTreeview` (250-278) | keep, extend | item/set/heading guarded; new redaction helpers (3.13) |
| `App.__init__` / state (281-378) | adapt | thin shell; `active_source_id` removed in shell (ii) |
| layout `_build_widgets` (380-488) | replace | menu bar + toolbar + view frames |
| theme / zoom glue (489-528) | keep, extend | `ThemeAware` registry, image regeneration |
| update check / install (530-740) | keep | unchanged (banner) |
| panes, populate, refresh (752-964) | move to `ClassicView` | organizer has incremental refresh |
| backups sort (966-995) | move to `ClassicView` | organizer sorts via D1 |
| save-file history filter (997-1055) | replace | the organizer table is the save-file view |
| tooltip builders (1057-1153) | adapt | classic view; organizer builds on hover |
| selection + context menus (1155-1300) | replace for organizer; adapt for classic | board and table menus (2.2, 3.2) |
| `_run`, `_busy`, `_run_worker` (1302-1377) | keep for classic until re-routed; replaced by `UiWorker` | 3.9 |
| actions (1379-1673) | adapt | core calls re-routed through D5 (below) |
| `PlaytimeDialog` (1675-1737) | retire | its rows are the organizer lines |
| `AccountsDialog` (1740-1814) | keep | Live > Accounts... |
| `ChooseSourceDialog` (1817-1894) | retire | the version picker of 03b |
| `CopySlotDialog` (1897-2030) | replace | the Move dialog (3.6) |
| `HELP_TEXT` (2033-2187) | rewrite | organizer vocabulary |
| `main` (2190-2209) | keep, extend | DPI call, pythonw logging |

Today's flows (aligned with D5 4.5):

| Today | Becomes |
|---|---|
| Backup live (toolbar) | File > Back up live folder now... (names the folder) |
| Restore (whole backup) | classic: Restore whole folder... = D5 panic restore (account named, accountdata kept) |
| Extract slot | kept in the classic view; in the organizer the vault already has every version, so "Extract" is rarely needed (it remains on the board menu as "Keep a copy in the vault now") |
| Clear slot... | not an app action any more (F8): "Empty this slot (manual steps)..." shows what is in the slot, keeps a vault copy on request, and lists the manual procedure |
| Copy into live slot... / Copy slot N into live slot N / Replace from anywhere | Move to live... / Replace with... (one dialog) |
| Promote | board menu "Make the other save the one the game loads" (`plan_promote` on the clicked slot) |
| Undo (global) | Done bar Undo + per-slot Undo + operations log; the old behaviour survives as "Restore whole folder from a snapshot..." |
| Import... / Discover / Rescan / Refresh | File menu / Rescan button; Refresh merges into Rescan |
| Play time... | retired (the table) |
| "Save file:" history filter | retired (the table + History window) |
| "Active live" selector | removed in shell (ii); kept for the classic tab in shell (i) |
| modal "Done" / "Please wait" | Done bar / inline progress |
| "Proceed anyway (risky)?" | removed for organizer operations (D5 `GAME_RUNNING` blocks; unknown -> confirm checkbox) |

## 6. Test plan

The suite keeps exactly one Tk root per session (`conftest.py:121-158`; R2 / R3 K9), withdrawn.

- **Fixtures.** A synthetic `ReadModel` built by D1's test fixtures: two live sources (Steam 15 with
  one empty slot, Xbox 15), a forked line (a rollback branch and a side branch), a diverged
  cross-platform line, a vault-only line, an unnamed line and a legacy-id line; a fake
  `IndexService` with scripted `events_since` / `progress`; a fake `liveops` recording calls and
  returning canned `EvictionPicker` / `PlacePlan` / `OpOutcome` / `VerifyItem` objects (D5's
  dataclasses, so the fakes cannot drift). No test touches a real vault or live folder.
- **Table**: row values and badge keys per line; chips -> `LineFilter`; Find; sort keys and the
  arrow image; expansion and selection survive an incremental refresh; folded rows and markers.
- **Lane rows** (pure functions): node/edge -> per-row segments for the fixture graphs (a recorded
  rollback fork, an operation-started side branch, two separate recorded trees in one line); the
  split into recorded tree and dated list (no inferred edge ever drawn); key-mode runs of the dated
  list; image height equals `rowheight` at zoom 0.7, 1.0, 2.0.
- **Board**: slot items exist for 30 slots; markers match `BoardSlot` fields; hit test of a point
  returns the right token; the context menu labels; keyboard focus movement.
- **Move dialog**: the suggested row is tagged; selecting a blocked row is refused; the button text
  equals `plan.outcome`; confirm checkboxes gate the button and are passed as `confirmed`; `stale`
  renders the replan; `failed` shows Recover; `done` closes and shows the Done bar; Undo calls
  `plan_undo` with the op id.
- **Notes**: save with `base_rev`; `NoteConflict` path; label limit counter; Linux display filter and
  the edit guard (simulated windowing system).
- **Threading**: a helper asserts every widget helper runs on the main thread; `UiWorker` delivers
  results only through `after`.
- **Redaction**: the fake-alias scan of 3.13 over every view, dialog, canvas text and title.
- **Theme**: contrast pairs for the new palette keys; `TNotebook` maps present in dark.
- **DPI**: `px()` scales with fpixels and zoom (pure function).
- **Performance smoke**: 300 lines / 12,000 version rows populate under 300 ms and an incremental
  refresh of one changed line under 20 ms (R3 measured 148 ms for the full stress build).
- **Screenshot loop** (dev-only, not pixel-asserted): the approach of `mockups/tk/source` - real
  widgets on fixtures, DPI-aware, captured through the DWM frame bounds and `PIL.ImageGrab` on
  Windows (xvfb + `import` on Linux per DESIGN D10) - produces PNGs of every screen for review in
  light and dark at zoom 1.0 and 1.2.
- **Manual checklist** per release: 150 % crisp; zoom 0.7 / 1.2 / 2.0 without clipped buttons; dark
  mode; a machine with only Xbox; an empty vault; first run with indexing; game running.

## 7. Implementation plan

Sizes: S < 200 lines incl. tests, M 200-500, L > 500. One commit per step, `uv run pytest` green.
Phases follow 01-consolidation section 5.

| # | Step | Size | Phase |
|---|---|---|---|
| 1 | DPI awareness in `gui.main` + `px()` helper + scale every existing pixel constant; dialogs size from content (fixes R2 D6) | S | 0 |
| 2 | Theme: new palette keys, `TNotebook`/`TLabelframe`/`TSeparator` maps, dark title bar, contrast tests | S | 0 |
| 3 | Menu bar for the secondary actions; toolbar fits at zoom 1.2 (P10) | S | 0 |
| 4 | pythonw log file handler (R3 K6) | S | 0 |
| 5 | Split `App`: `ClassicView` frame extracted unchanged (mechanical; existing GUI tests green) | M | 1 |
| 6 | UI infrastructure: `UiWorker` (executor + queue + `after` drain), `ThemeAware` registry, redaction helpers (item/set/text/canvas/title), raster module (badges, arrows) | M | 1 |
| 7 | `OrganizerTable` with lines from D1, chips, Find, sort, column chooser, `ui` prefs | M | 1 |
| 8 | `LiveBoard` canvas (read-only: draw, highlight, tooltips, focus) | M | 1 |
| 9 | `DetailPane` (Summary, Where, Notes read view) | M | 1 |
| 10 | Version rows on expand (default mode, fold markers; no lanes yet) | S | 1 |
| 11 | Notes window (save-file note + revisions, version label/comment/lock) and pins | M | 1 |
| 12 | Shell: (ii) main window with view switch (or (i) tab) + incremental refresh + start-up scan in the worker + alerts area | M | 1 |
| 13 | GUI tests with the synthetic read-model fixture; redaction scan | M | 1 |
| 14 | Move dialog from a version / line: picker, plan per selection, warnings by severity, confirmations, execute with progress, outcomes | L | 2 |
| 15 | Replace with... (slot first) + drag onto the board | M | 2 |
| 16 | Done bar, Undo, operations log | M | 2 |
| 17 | Verification markers and alerts; start-up recovery dialog | M | 2 |
| 18 | Board menu: Swap two slots, Promote, Keep a copy in the vault now; the "Empty this slot" steps window | M | 2 |
| 19 | Classic view re-routed: no "Active live"; whole-folder restore = panic restore | M | 2 |
| 20 | D4 integration: progress strip, event polling, facts columns, `facts:` sorting, first-run banner | M | 3 |
| 21 | Version card (D4 `card`), deep section on demand, "what changed" in rows | M | 3 |
| 22 | Lane images in version rows (F2-C) from D1 `history` | M | 4 |
| 23 | History window (Key / All, runs, card pane, follow selection) | M | 4 |
| 24 | Compare window | M | 4 |
| 25 | Note snapshot diff in the notes window | S | 4 |
| 26 | Combine / Split / "comes from" correction dialogs | M | 4 |
| 27 | Steam <-> Xbox account choice enabled when D5 opens F6 | S | 5 |

Phases 2 and 3 are independent; their order can swap (as in 01-consolidation).

## 8. Risks, open questions, deviations

### 8.1 Risks

1. **Looks ceiling** (R3): whole-row styling and images only in column #0. Mitigation: state words,
   badges, row tints, lane images; the `CellStyle` adapter for Tk 9.
2. **Linux Unicode**: non-Latin-1 names and notes cannot be drawn on the Linux kit; mitigated by the
   display filter and the edit guard (3.14), not solved.
3. **`App` refactor** of a 1,392-line class: done as a mechanical extraction first (step 5) behind
   the existing tests.
4. **Incremental refresh bugs** (stale rows): every refresh can fall back to a full rebuild that
   restores expansion and selection by pid / vid; tests cover both paths.
5. **Zoom and DPI**: a missed pixel constant clips text; the screenshot loop at zoom 1.2 and 2.0
   catches it.
6. **Canvas and PhotoImage drawing is aliased** (no anti-aliasing in Tk 8.6 on Windows); acceptable
   at 150 %, plainer than the web option.
7. **Native menus** stay light in dark mode and do not zoom (4.4).

### 8.2 Open questions (owner)

1. Shell (ii) or (i) (4.1) - recommendation (ii).
2. Default columns: the mockups show Glyphs, Parts, Legacy, Ships; Bases, Companions, Galaxies and
   Story are one click away. Which four does the owner want by default?
3. Pins: the mockups pin Main Save and All Parts Building to show the column; the owner pins his own.
4. Drag and drop onto the board: build it (step 15) or rely on the dialog only.

### 8.3 Deviations from 01-consolidation section 6, and requests to sibling designs

No seam is changed: the UI consumes only D1's read model and notes API, D4's index service and D5's
entry points. Small requests:

1. **D5**: `execute(..., note: str = "")` (or a field on the plan) so the optional "Note for this
   move" reaches D5's op record and D1's `OpLineage.note`; today `execute` has no note parameter.
2. **D5**: put the source line's D1 `cross_platform` state into `LIVE_ELSEWHERE.data` (or let the
   UI join it from D1, as the mockup does) so the dialog can say "on another branch (diverged)".
3. **D1**: per 01-consolidation 4a, `history()` should return the drawn tree (recorded and
   journal-observed edges, the version an operation started from, and the pair it replaced) and
   leave every other version to the dated list; the key / all toggle then applies to the dated
   list only, which the UI does itself.
4. UI preferences (`ui` in `state.json`) are per-machine settings, not organizer store data.

## Appendix - mockups and how they were made

| File | Shows |
|---|---|
| `01-organizer.png`, `01-organizer-dark.png` | organizer view, 30 lines, board, Main Save summary; dark theme with the dark title bar and the light native menu bar |
| `02-line-expanded.png` | Main Save expanded (recorded tree with lanes, then the dated list) and All Parts Building (dated list with a folded re-save), #18 card |
| `03-move-to-live.png` | Move to live from CrossSaveTest-300qs #2, slot 5 picked, slot 1 suggested |
| `03b-replace-from-slot.png` | Replace with... from Steam slot 5, picking AF24 #2 |
| `04c-history-hybrid.png` | history style C: recorded tree with lanes, dated list in key mode with folded runs |
| `05-notes.png` | notes window: save-file note, revisions, version label/comment/lock, snapshot diff, automatic changes |
| `06-compare.png` | #23 (main line) vs #22 (invented side branch) |
| `07-first-run-indexing.png` | first run: facts filling in, progress strip, banner |
| `08-cross-platform.png` | Creative Primary (diverged) and The Cartographers (Steam ahead) as dated lists, Move to live warnings |
| `09-done-undo.png` | after a Place: Done bar, pending verification |
| `10a-shell-tab.png`, `10b-shell-organizer-first.png` | shells (i) and (ii) |

Rendering: `uv run --with pillow python docs/organizer/mockups/tk/source/render_all.py` on Windows
(one process per screen; `SetProcessDpiAwareness(1)`, the app's `theme.apply`, window frame from
`DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`, `PIL.ImageGrab`, downscaled to 1,800 px with a
caption band). Data: `mockups/sample-data.json` only; times in the owner's local time (UTC+3); the
storage state is F3-B after migration (every vault copy durable). Not shown in images: hover
tooltips, the drag ghost (R3 verified it), the deep-facts spinner, animations, and the Linux kit
(its Tk would show `?` for the Greek settlement name).
