# D6 - UI option T: the organizer in the Tk app

Design wave, UI design D6 (subject S7, owner fork F1 option T; F2 history style). Branch
`design/save-organizer`. Binding inputs: `docs/organizer/01-consolidation.md` (C1-C32, sections 6
and 7), research R2, R3, R4, R6, R7, R8, and the final designs D1 (read model), D3 (version store),
D4 (content index) and D5 (live operations). Nothing under `src/`, `tests/` or `packaging/` was changed.

Mockups: `docs/organizer/mockups/tk/*.png`, rendered by real Tk 8.6.15 / ttk widgets (the app's own
`theme.py`), DPI-aware at 150 %, from `mockups/sample-data.json` (the owner's real 30 save files and
110 versions). Notes, pins and the "Vesper Ridge" side branch (#20-#22) are invented sample content;
every image says so in its caption band. Scripts: `mockups/tk/source/` (throwaway, not app code).

Owner decisions (01-consolidation 4a, including the owner's answers to the design review),
applied here and in the mockups: F2 = **C** (version table with a lane column and a key / all
toggle); **no guessed link anywhere** - versions are joined only by recorded moves, by the live
journal and by "seen in backups" links (two consecutive app-made backups of the same live slot);
every other version is a flat dated list; a version with no recorded parent shows no "what changed"
("no recorded parent"); F3 = **B** from phase 2 (the vault is copied aside first, then the app's old
backup folders are folded in; before that upgrade they are listed in place); F9 = keep everything
(pruning stops in phase 0, so no "may be pruned" state); hand-made in-place backup folders stay
"outside the vault" until the owner copies them in (default off); F4 = **a** (a version goes live
with its own restore-point partner); F5 = **b** (the game's save date is stamped "now"; the original
date stays in the vault record); F6 = Steam <-> Xbox later, after test runs (shown as not available
yet); F7 = **b** (one warning whenever the save comes from another account); F8 = operations always
replace and the app never empties a slot (it explains the manual steps and warns that Steam Cloud /
Xbox sync may fill an empty slot again). A save live on Steam and Xbox: each platform's copy is its
own tip; the one with less play is "behind"; "two branches" only when the live journal shows that
both progressed.

**F1 = Tk**, and the owner's screen-by-screen review (`docs/organizer/02-ui-review.md`, binding) is
applied: shell 10a (tabs under the menu bar, the app reopens the last tab); a `>` / `v` expand
marker instead of the platform boxes; other lines muted while one is expanded; the move dialogs
(03, 03b) and the history view (04c) rebuilt in Tk on the web mockups' layout; a live area that
shows one live folder or two chosen ones (08b, 08c); the empty-slot refill warning; the source chip
"Vault only". Every mockup was re-rendered.

Update pass (2026-10-17): the review items for D6 (`03-design-review.md` V-04, V-05, V-16, V-17,
V-18, V-21) and the owner's answers Q3-Q6 are applied, the calls point at the interfaces D1, D3, D4
and D5 publish after that pass, and all 15 mockups were re-rendered from the same data.

## 0. Owner summary

1. The organizer is a tab beside today's live/backups panes; the app opens in the tab you used last (10a).
2. All 30 save files in one sortable table with a `>` to open each; your live slots in vertical lists on the right - one live folder or two you choose (01, 08b, 08c).
3. Move to live and Replace use one dialog: what comes in (green) and what goes out (amber), all 15 slots, "What you are putting in", the outcome text, "Your pick", and a button that names the result (03, 03b).
4. History: lines only where the app saw the link (its own moves, the live slot, consecutive backups - "seen in backups"); everything else is a dated list; nothing is guessed (02, 04c).
5. A save live on Steam and Xbox shows which copy is behind, by play time (01, 08).
6. The app never empties a slot; an empty live slot warns that Steam Cloud or Xbox sync may fill it again (01, 08b).
7. Tk limits that stay: no per-cell colours, plainer graphics, and the Linux build cannot draw non-Latin notes; Tk 9 later adds per-cell colours.

## 1. Scope, non-goals, dependencies

**In scope.** The Tk front end of the organizer: the window shell (10a; 10b kept as the documented
alternative); the organizer view (table, filter chips, find, column chooser); the live area (one or
two live folders); the detail pane; the Move to live / Replace
with dialog; the history window (style C); the notes window; the compare window; first-run and
indexing progress; the non-modal "Done - Undo"; verification alerts and the recovery prompt; the
classic view; the DPI, theme and zoom pass; redaction of every displayed string; the UI threading
model; UI tests.

**Non-goals.** Identity, versions, lineage, notes storage and the read model (D1); storage, the
vault upgrade and its listing (D3); decoding and facts (D4); operation semantics, plans, journal,
undo, verification (D5); a browser UI (D7, not chosen); Steam <-> Xbox transfer (F6, later); fixing
non-Latin-1 drawing on the Linux kit (no Tk build can, R3 K4).

**Dependencies.** The UI consumes only these (01-consolidation section 6); it never reads a save
file, a catalog entry or a live folder, and never chooses a write target itself.

| Needs | From | Used for | Phase |
|---|---|---|---|
| `ReadModel.lines()`, `line(pid)`, `sort_lines`, `LineFilter`, `live_board()`; `Line.cross_platform` (`n/a` / `same` / `steam-behind` / `xbox-behind` / `diverged`) and `Line.platform_tips` (`behind_by`) (D1 2.10) | D1 | table rows, chips, board, platform state | 1 |
| `versions(pid)` (`VersionRow.in_tree`, `key`), `history(pid, mode)`: the drawn tree only (recorded, journal-observed and "seen in backups" edges) with `GraphNode.row/lane`, `pass_through`, folding, key mode, plus `HistoryGraph.dated` (D1 2.10.2-2.10.5) | D1 | version rows, lane column, history window | 1 (rows), 4 (lanes) |
| `Notes`: `save_note`, `set_save_note(pid, text, base_rev)` + `NoteConflict`, `note_text_at`, `version_note`, `set_version_note`, `set_pinned` (D1 2.9) | D1 | notes window, pins, labels | 1 |
| `LiveTarget.token` (opaque) and `resolve(ref)` | D1 | everything passed back to D5 | 1 |
| `IndexService.view(vid)`, `card(vid)`, `changes(child, parent, *, edge_kind)` (only for a drawn parent; none -> not called, "no recorded parent"), `status(vid)`, `request(vids, Priority)`, `request_deep` / `deep`, `progress()`, `events_since(seq)`, `pause/resume/cancel` (D4 2.12) | D4 | facts columns, version card, "what changed", progress | 3 |
| `eviction_picker`, `plan_place`, `plan_swap`, `plan_promote`, `plan_undo`, `plan_panic_restore`, `plan_keep` (keep a copy in the vault, no live write), `empty_slot_guide` (manual steps, refill warning), `execute(ctx, plan, confirmed, progress, *, note)`, `busy_targets`, `pending_recovery`, `resolve_recovery`, `update_verification`, `verification_items`, `op_history` (D5 2.2; nothing that empties a slot exists, F8) | D5 | every write | 2 |
| `store_status(vault_root)` (vault mode, copy-aside, fold progress, damage from `store/state/damage.json`); `list_vault_entries`, `open_entry_folder` for the classic tab; the upgrade, its undo, "Copy into the vault" for hand-made folders, "Free old folders" (D3) | D3 | alerts, classic Backups pane, vault upgrade window | 2 (5 for freeing) |

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
| Main window, tab **Organizer** | root | tab under the menu bar, Ctrl+1 | the organizer (01) |
| Main window, tab **Live saves and backups** | root | tab under the menu bar, Ctrl+2 | today's window content: 14-button toolbar, "Active live", two panes (10a) |
| Move to live / Replace with | modal Toplevel | Move to live... (Ctrl+M), board right-click, drag onto a slot | one dialog, both ends visible (C30) |
| History | non-modal Toplevel, one | History (Ctrl+H), line menu | follows the main selection unless "Keep this save file" is ticked |
| Notes | non-modal Toplevel, one | Notes... (F2), detail-pane links | edits the selected save file and version |
| Compare | non-modal Toplevel, one | Compare..., Ctrl+click a second version | |
| Recovery | modal | start-up, when D5 `pending_recovery` has a torn op | the only start-up modal (consequential) |
| Alerts | non-modal bars above the table | D5 `verification_items`, D3 `store_status` | stay until dismissed |
| Columns, Accounts, Help, About | modal | menus | Accounts is today's `AccountsDialog` |

Selection is the hub: the table selection drives the board highlight, the detail pane, the History
window and the default source of Move to live. Keyboard: Tab cycles chips -> Find -> table ->
board -> detail tabs -> action buttons; arrows move inside each; Enter on a line or version opens
Move to live, on a board slot opens Replace with; Shift+F10 / the menu key opens the context menu;
Ctrl+F focuses Find; Ctrl+1 / Ctrl+2 switch tabs; Ctrl+plus / Ctrl+minus zoom. Every drag has a
keyboard path (R4 F2): dragging a line or version onto a board slot only pre-fills the dialog. The
app stores the last tab in `state.json` (`ui.last_view`) and opens there.

### 2.2 Screen 01 - the organizer tab

![01](../mockups/tk/01-organizer.png)
![01 dark](../mockups/tk/01-organizer-dark.png)

**Layout (top to bottom).** Native menu bar (File, Edit, View, Live, Tools, Help); a `ttk.Notebook`
whose tabs sit directly under the menu bar: `Live saves and backups | Organizer` (shell 10a). The
Organizer tab holds: a slim toolbar with the selection actions `Move to live...`, `History`,
`Compare...`, `Notes...` and `Rescan` at the right; the chip row; a horizontal `ttk.PanedWindow`
with the organizer table on the left (weight 3) and, on the right, the live area above the detail
pane. The status bar sits below the tabs.

**Chips** (toggle buttons with counts; groups combine with AND, chips inside a group with OR):
`All 30 | Live 26 | Vault only 4` - `Steam 14 | Xbox 15 | On both 4` - `Has note 6 | Pinned 2 | Not in
vault 8 | Expedition 7`. They map 1:1 to D1 `LineFilter` (`live`, `platform`, `cross_platform`,
`has_note`, `pinned`, `at_risk`, `expedition`); counts come from the unfiltered lines. `Find`
filters by `LineFilter.text` (name, former names, note text, version labels; case-insensitive) as
you type (debounced 150 ms). `Columns...` opens the column chooser.

**Table columns** (default set; widths in logical px; D1 sort key):

| Column | Line row | Version row | Sort key |
|---|---|---|---|
| #0 Save file / version | `>` / `v` expand marker + name ("(unnamed 9589)", "(unnamed, was Swarm)") | lane image + "#18" ("#1 (restore point only)" for a fork member) | `name` |
| Live (58) | "S9", "S13 X8", "vault" | "S9" or "vault" | `live` |
| Vers. (40) | distinct versions (C6) | - | `versions` |
| Play time (76) | "532h16" (`_fmt_play`) | the version's | `play_time` |
| Last played (112) | local "2026-10-15 23:39" | saved time | `last_played` (default, descending) |
| Mode (72) | difficulty, or "Exped. 23" | the version's | `difficulty` |
| State (126) | see vocabulary below | see below | `risk` |
| Glyphs (48), Parts (56), Legacy (68), Ships (42) | D4 `view(representative)` | D4 `view(head)` | `facts:glyphs` ... |
| Note (rest) | first line of the save-file note | label, else `auto: ` + D4 `changes().top(3)` against the drawn parent; empty when there is no recorded parent | `note` |

Optional columns (chooser): Bases, Companions, Galaxies with bases, Story (Artemis), Atlas, base
computer, Quicksilver, Nanites, Journey rank, Frigates, Systems discovered, Game version, Branches,
Platforms. Any D4 numeric (`FactsView.numbers`) can be a column and a sort key.

**State vocabulary** (text, because a cell cannot be coloured in Tk 8.6): line rows - `NOT IN
VAULT` (a live pair with no vault copy), `Xbox behind 6h33` / `Steam behind 0h40` (D1
`cross_platform` `xbox-behind` / `steam-behind` with `PlatformTip.behind_by`: the two live copies
compared by play time, no ancestry), `two branches S/X` (D1 `diverged`: only when the live journal
shows that both copies progressed), `vault only`, `edited` (newest version edited outside the game),
`pinned`, `placed 12:04, check pending`, `reverted by cloud`; version rows - `NOT IN VAULT`,
`only outside the vault` (held only by a hand-made in-place folder), `branch point`, `branch tip`,
`abandoned branch`, `re-saved, no play`, `edited outside game`, `restore reverted`, `incomplete copy`,
`+1 re-save folded`. Branch point and tip count drawn edges only. Pruning stops in phase 0, so no
version is ever "only in auto-snapshots"; "outside the vault" stays in every phase for versions that
only hand-made in-place folders hold, until the owner copies those folders in (D3, default off).

**Expand marker** (owner review): one `>` per line, `v` when open, drawn in code as the line row's
column-#0 image (3.1.4). Where a save file is live is the Live column ("S9", "S13 X8", "vault");
"not in vault" and "check pending" are words in State. No platform boxes.

**Live area** (3.2), vertical lists as before (the owner kept them over the web's tiles):
`Live: [One | Two]` plus one combobox per shown list. *Two* (01, 08c) shows two live folders side by
side, the owner picks which two (any Steam account or Xbox); *One* (08b) shows a single wide list
with last played and the inline warnings. Only configured live folders are offered, so a user
without Xbox never sees an empty Xbox column; with a single live folder the area shows that list
and no selector. Each list has 15 rows: slot chip `S9` in the platform colour (outlined and grey
when empty), name, `+X8` when the same save file is live on the other platform, play time,
markers (filled amber triangle = not in vault, pin = pinned, clock = waiting for the next game
launch, red = reverted, blue cloud = empty slot). The selected line's slots are outlined in the
accent colour. Header: "Steam live - 14 of 15 used". The legend lists only the markers in use.

**Empty slot** (owner review): an empty slot (Steam slot 1 here; emptied by hand on 2026-09-27)
carries the cloud marker, its tooltip says "Steam Cloud may put a save back into it at the next game
start (the app never empties a slot ...)" (shown in 01), the one-list mode prints the sentence in
the row (08b), and selecting it shows the slot card in the detail pane: "This slot is empty. Steam
Cloud may fill it again: ...", what was there last, how to keep it empty, how to use it (08b). The
text is D5's `EMPTY_SLOT_MAY_REFILL`; the same sentence opens the manual steps (D5
`empty_slot_guide`, 2.12). The move dialog does not repeat it: a Place overwrites the empty slot and
D5's check after the next launch catches a refill, so the dialog's row reads "emptied outside the
app - checked after the next launch" (D5 `EMPTY_SLOT`, review V-21).

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
Each row is one captured pair (C4), labelled by its head. The block has two parts (owner decision:
no guessed link):

- **Linked history** first, with lane images in column #0 (F2-C, 3.1.3): only versions joined by a
  drawn edge - a move the app recorded (its operations, the old oplog), a change the live journal
  observed, or "seen in backups" (two consecutive app-made backups of the same live slot, owner
  answer Q3; D1's capture-sequence edges). Main Save: #18 -> #23 (put live by the app on 10-05
  17:49, live in S9) and #18 -> #19 (the save it replaced, loaded by mistake and abandoned); #11 ->
  #20 (an operation on 10-11) -> #21 -> #22 (the side branch, invented, seen in the live slot);
  #6 -> ... -> #19 seen in backups of Steam slot 9.
- A separator row "Not linked, by date - 5 versions; no recorded move or app backup links them; no
  links are guessed", then the **dated list** (D1 `HistoryGraph.dated`): one row per version, newest
  first, a grey dot instead of lanes (live versions keep their platform-coloured dot). Main Save
  #1-#5 are known only from hand-made in-place folder copies, whose order is unknown.

No-progress versions are folded (C5) into their older neighbour: All Parts Building (one chain seen
in backups) shows #4 (re-saved without play) folded into #3 ("+1 re-save folded"). Version rows
have a tinted group background so the expanded block reads as one unit, and
while any line is expanded every line that is not expanded is drawn muted (tag `dim`, grey text;
owner review) - Main Save XBox, (unnamed 9589) and Egg Gifting Xbox in the image.

Selecting a version fills the detail pane with the **version card**: where it is ("Vault: backup
2026-10-05 17:41 - not live"; "Outside the vault: hand-made folder '...'" for a version only such a
folder holds); "Comes from #17 (put live by the app / seen in the live slot / seen in backups)" for
a drawn edge, else "Comes from: no recorded parent - not linked; listed by date, nothing to compare
with"; "Led to #23, #19 - a branch point" (drawn children only); operation and journal events (the
reverted restore in amber); label and comment; "What changed since #17" (D4 `changes`, only with a
drawn parent - otherwise the section is absent, owner answer Q5); and the contents (D4 `card`; the
deep section loads on demand with a spinner after 150 ms, D4 2.10.4).

Keyboard: Right / Left expand and collapse; Up / Down move; Enter = Move to live for that version.
States: a version whose facts are not read shows "..."; an unreadable version shows "unreadable"
in State and cannot be a source (D5 `SOURCE_UNREADABLE`).

Reads: D1 `versions`, `history(pid, "default")`; D4 `view`, `changes`, `card`, `request(...,
INTERACTIVE)`, `request_deep`.

### 2.4 Screens 03 and 03b - the move dialog (Move to live, Replace with)

![03](../mockups/tk/03-move-to-live.png)
![03b](../mockups/tk/03b-replace-from-slot.png)

One modal dialog class with one layout (owner review: "the dialogs should follow the web
version"), entered from either end (C30). **Move to live...** (03) starts from a version: a line
(its newest version), a version row, the history or compare windows, or a drag onto a board slot
(that slot pre-picked); the slot is chosen in the dialog. **Replace with...** (03b) starts from a
live slot (board right-click or Enter); the version is chosen in the dialog.

Layout, top to bottom:

1. **Title and one-line purpose**: "Move to live - Pick the live slot. Whatever is in it goes to
   the vault first, so nothing is lost." / "Replace what is in Steam slot 5 - Pick any version from
   the vault. The save in the slot now goes to the vault first."
2. **What goes where** - two boxes and an arrow. Left, tinted **green** ("in"): `PUTTING IN`, name
   and `#n`, version date, play time, mode, where it is in the vault (D5
   `SourceSummary.where_label`). Right, tinted **amber** ("out"): `STEAM SLOT 5 NOW - GOES TO THE
   VAULT`, the occupant, its play time and last played, and "already in the vault - stays there,
   ready to come back" or "not in the vault yet - copied there first". Both boxes update with every
   pick. Green and amber are used for the same meaning everywhere in the dialog (the picked slot
   row is amber because its save goes out, the picked version row is green because it comes in,
   and the `OUT` / `IN` tags of "Your pick").
3. **The picker** (left column):
   - 03: `Into [Steam | Xbox (locked)]` from `EvictionPicker.account` / `other_accounts` (a blocked
     account stays visible with its reason: "Moving saves between Steam and Xbox comes in a later
     version"; with two Steam accounts the switch lists their alias labels and the other account is
     selectable with D5's one `OTHER_ACCOUNT` confirm warning whenever the save comes from another
     account, F7-b; no ownership ids are read); a plan-level info strip (D5
     `STEAM_CLOUD_OTHER_DEVICE`: "Steam Cloud is on. If Steam asks at the next launch which copy to
     keep, choose the one on this PC."); then the **15 slots** (D5 `candidates`): a radio image in
     column #0, Slot, In it now (pinned marked), Play time, Last played, In the vault (`yes` / `NO`),
     and a notes column: `Suggested` / `Suggested: same save file` (green, bold), `Your pick`,
     `copied to the vault first`, `pinned - not suggested`, `also live in X14`, `59 min more play`;
     an empty slot reads `empty (emptied outside the app)` and `checked after the next launch` (D5
     `EMPTY_SLOT`). Restoring an older version groups the rows: SAME
     SAVE FILE first, then OTHER STEAM SLOTS ("a second live copy makes two branches"), the rest
     folded behind "> 8 more slots" (08).
   - 03b: a Find entry; chips **`Vault only 4`** (owner review; was "Not live"), `Older versions of
     Ex-Beachhead-160qs 1`, `Live elsewhere 14`; a grouped tree: VAULT ONLY - 4 SAVE FILES (lines with
     `>` markers, the open one shows its versions with radio images and their automatic
     descriptions), OLDER VERSIONS OF EX-BEACHHEAD-160QS, LIVE ELSEWHERE - 14 STEAM SAVE FILES ("a
     second live copy makes two branches"), and "> 10 more. Xbox-only save files are not listed:
     moving saves between Steam and Xbox comes later."
4. **What you are putting in** (right column, 340 px): name and `#n`, play time, saved, platform,
   then Difficulty, Expedition, Where now, Glyphs, Bases, Largest base, Legacy parts, Galaxies with
   bases, Ships (D4 `view` of the source; "..." while not read), and "What changed in this version"
   (D4 `changes` against its drawn parent; without one: "nothing shown - no recorded parent", as for
   CrossSaveTest-300qs #2 in 03).
5. **What will happen** (kept exactly as the owner approved): one paragraph from the plan
   (`plan_place` for the current pick, computed in the worker on every change, < 100 ms): what goes
   into the slot "with its own restore point" (F4-a), "The game will list it as saved now; its
   original date (...) stays in the vault record" (F5-b), what leaves live and that it is in the
   vault; then the plan's warnings by severity: `info` (blue i), `warn` (amber !), `confirm` (a
   checkbox the owner must tick; the main button stays disabled until all are ticked and their codes
   go into `execute(confirmed=...)`), `block` (red; button disabled - for example
   `VAULT_UPGRADE_NEEDED` with a link to the vault upgrade window while the vault is not yet a
   version store, or `SWAP_EMPTY_SIDE`).
6. **Your pick** (from the web layout): one line summarising the choice - "Steam slot 5 (the
   suggestion was the empty slot 1)", `OUT` Ex-Beachhead-160qs -> vault (already there), `IN`
   CrossSaveTest-300qs #2 -> Steam slot 5.
7. **Footer**: optional "Note for this move" (D5 `execute(..., note=)`, recorded with the operation), the
   default button whose text is `plan.outcome` (kept exactly: "Swap into Steam slot 5", "Put into
   empty Steam slot 1", "Go back to this version in Steam slot 13"), `Cancel`.

In 03 the owner picked slot 5 over the suggested empty slot 1; CrossSaveTest-300qs is also live on
Xbox (D5 `LIVE_ELSEWHERE`, stated with the play times only: its Steam copy is not live, so D1 has no
platform state for it). In 03b the slot is fixed and the owner picks (unnamed, was Swarm) #2.

**Execute.** The button calls `execute(ctx, plan, confirmed, progress)` in the worker; the dialog
replaces its buttons with a determinate bar fed by D5 `Progress` (checking, preserving, snapshot,
writing, verifying, recording). Outcome: `done` closes the dialog and shows the Done bar (09);
`stale` shows "Steam slot 5 changed since you opened this. Please check again." and re-renders from
`replan`; `refused` / `rolled_back` / `failed` keep the dialog open with D5's text (2.14) and, for
`failed`, a `Recover...` button. Keyboard: arrows choose the slot or version, Enter executes when
enabled, Esc cancels, Ctrl+F focuses Find (03b). Calls: D5 `eviction_picker`, `plan_place`,
`execute(..., note=)`; D4 `view`, `changes` for the right column.

### 2.5 Screen 04c - History (style C on the web layout)

![04c](../mockups/tk/04c-history-hybrid.png)

A non-modal window over one save file, laid out like the web mockup (owner review):

- **Header**: name, live badge (`S9`), "23 versions - 3 branch tips - 532h16 played", and a legend
  (recorded move = thick line, seen in the live slot or in backups = thin line, live dot, vault
  ring, grey dot = not linked: by date).
- **Left card**: `[Key versions 10 | All 23]` and the sentence "Two versions are linked only where
  the app saw it: its own moves (since 2026-10-05), the live slot, or two consecutive app backups of
  the same slot ('seen in backups'). The rest is listed by date - no link is guessed." Then the
  table: lane column (images in column #0), Saved, Play time, Where, **How it is linked** (`recorded
  move` bold, `seen in the slot`, `seen in backups`, `first linked`, `by date`, `first seen`), Parts,
  Bases, Label / what changed. Two section rows: LINKED HISTORY (recorded moves since 2026-10-05, the
  live slot, consecutive app backups of slot 9; 3 branch tips) with Main Save's 10-05 rollback fork
  at #18 (#23 live S9, #19 replaced), the side branch from #11 (#20-#22, invented) and the chain seen
  in backups down to #6, key mode folding its runs ("> 6 more versions - 2026-09-16 to 2026-09-26
  (529h20 - 530h20) - click to show"); then NOT LINKED - BY DATE (5 versions seen only in hand-made
  folder copies, whose order is unknown; first and newest shown, a folded run of 3).
- **Branch tips** below the table: one row per branch in its lane colour (main line, Vesper Ridge
  side branch, abandoned re-save) with tip date, play time, where, "from <date>, n versions since",
  and `Move to live...` / `Compare with live` (or "live now").
- **Right panel**: the selected version - date and play time, "Steam - the game loads the auto-save
  - Normal - locked", the label as a yellow callout, `Move to live...`, `Compare...`, `Label`,
  `Unlock`, a "2 branches continue from here" callout, WHAT CHANGED SINCE #17 ("its parent, seen in
  backups"; for a version with no recorded parent: "nothing shown - no recorded parent"), EVENTS
  (the reverted 17:48 restore in amber, the 17:49 restore), WHERE IT IS, CONTENTS (D4 facts).

Lanes are D1's (2.10.5) over the drawn edges only, so 02 and 04c draw the same tree; no edge is
guessed or dashed. Once the app records an operation on a dated version (for example "Go back to
this version" on Main Save #3), that version and its future join the drawn tree.
Keyboard: Up / Down version, Left / Right branch, C compare, M move to live, K key versions / all;
Ctrl+click adds a second version for Compare. Reads: D1 `history`, `versions`; D4 `view`, `changes`,
`card`. Calls: none (writes go through 2.4).

**Styles A and B** (linear timeline; separate lane graph on a Canvas) were designed, considered and
rejected by the owner in favour of C; they are not built.

### 2.6 Screen 05 - Notes

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
- **What changed in the game since the parent**: D4 `changes(child, parent).lines()` for the drawn
  parent; a version with no recorded parent shows nothing here, and the list on the left shows no
  automatic text for it.
- **Where notes are kept**: in the vault (`organizer/notes.json`), never in the save files.

Text is stored as Unicode (C22). Windows Tk draws it; on the Linux kit (no Xft) see 3.14.
Calls: D1 `Notes` only.

### 2.7 Screen 06 - Compare two versions

![06](../mockups/tk/06-compare.png)

Two version cards (A and B) with `Swap A and B` and `Pick other versions...`; an info strip with the
nearest common ancestor over D1's drawn edges ("#11, branch point; A played +6h14, B +3h36 since";
"no common ancestor recorded" when the two are not linked); a
`[Differences only 11 | All facts 22]` switch; a fact table (Fact, A, B, Difference; differing rows
tinted - a whole-row tint is allowed in Tk); on the right "Named things that differ" (bases, ships,
named companions only on one side; bases in both with a different size) and "Since #11 on each
side" (the top D4 changes along each branch). Facts come from D4 `view` / `card` of both vids
(requested at `INTERACTIVE`); the field-by-field comparison is presentation logic in the UI, it does
not call `changes()` on non-adjacent versions (D4 rule N1). Calls: none.

### 2.8 Screen 07 - First run and indexing

![07](../mockups/tk/07-first-run-indexing.png)

Meta columns (name, live, versions, play time, last played, mode, state) appear after D1's first
scan; facts columns show "..." until D4 has read that version. A dismissible info banner explains
the first run once ("30 save files and 110 versions found in 1.2 s ... Versions in consecutive
backups of the same slot are linked ('seen in backups'); the rest from before today are listed by
date; from now on the app records how they connect ..."). A progress strip shows D4
`progress()`: a determinate bar (`done_bytes / total_bytes`), "14 of 110 versions - 4 worker
processes - about 20 s left - live saves and the save file you select go first", `Pause`, `Cancel`.
Rows update in place as D4 events arrive (3.9); the selected line jumps the queue (`INTERACTIVE`) and
its detail says "Reading now - moved to the front of the queue". Nothing freezes: the table, board
and dialogs work during indexing. When the game is running D4 drops to one worker; the strip says
so. Calls: D4 `request`, `progress`, `events_since`, `pause`, `resume`, `cancel`.

### 2.9 Screens 08, 08b, 08c - Steam and Xbox, the live area

![08](../mockups/tk/08-cross-platform.png)
![08b](../mockups/tk/08b-live-one-source.png)
![08c](../mockups/tk/08c-live-pick-two.png)

**08.** With the `On both 4` chip: Creative Primary (`S13 X8`, `NOT IN VAULT; Xbox behind 6h33`) and
The Cartographers (`S3 X14`, `Xbox behind 2h35`) are expanded. Creative Primary #3-#12 are linked as
seen in backups of Steam slot 13; its Xbox copy #5 (live in Xbox slot 8), #2 and the Xbox restore
point #1 (a grey diamond, "restore point only") form the dated list: nothing recorded joins the two
platforms, so no cross-save link is drawn. The platform state is a word in the State column and a
sentence in the detail pane ("the Xbox copy is behind (6h33 less play than the Steam copy); each
platform's copy is its own tip"), from D1 `Line.cross_platform` / `platform_tips`, which compare the
two live copies by play time without ancestry; "two branches S/X" appears only when the live journal
shows that both copies progressed. The move dialog (2.4) for "Creative Primary #11" (an older Steam
version): slot 13 suggested (same save file; D5 suggests it even when pinned), rows grouped SAME SAVE
FILE / OTHER STEAM SLOTS, the amber box shows #12 going to the vault ("not in the vault yet - copied
there first"), the button reads "Go back to this version in Steam slot 13"; warnings:
`PLAY_TIME_AHEAD` as a confirm checkbox, `NOT_IN_VAULT` (info), the Xbox copy (`LIVE_ELSEWHERE` with
`data["cross_platform"]`: "the Xbox copy is behind and is its own tip"), and the blocked Xbox account
(`CROSS_PLATFORM_GATED`).

**08b - one live folder.** `Live: [One]` with a source combobox. One wide list: slot, name,
other-platform marker, play time, last played, markers; an empty slot prints "Steam Cloud may put a
save back here at the next game start" in its row. Selected here: the empty Steam slot 1, whose
detail card explains the refill risk, what was there last ((unnamed 893F) #2, gone 2026-09-27
without a vault operation), how to keep it empty, and how to use it. With only one configured live
folder the area shows this list without `One | Two` and without a selector.

**08c - two live folders.** `Live: [Two]` with one combobox per list; the open list shows every
configured live folder (Steam, an invented second Steam account to illustrate several accounts,
Xbox). The two lists can be any two (two Steam accounts, Steam + Xbox). The choice persists
(`ui.live_mode`, `ui.live_sources`).

### 2.10 Screen 09 - Done, Undo, waiting to confirm

![09](../mockups/tk/09-done-undo.png)

After `execute` returns `done`, no modal appears. A **Done bar** (banner colours) slides in above the
status bar: "Done. Moved 'CrossSaveTest-300qs' into Steam slot 5. 'Ex-Beachhead-160qs' is in the
vault. Waiting for the next game launch to confirm." with `Undo` (default), `Show in history`, `x`.
It stays until dismissed or replaced by the next operation; Edit > Undo (Ctrl+Z) keeps
`UndoOffer.label` ("Undo: put 'Ex-Beachhead-160qs' back into Steam slot 5"). `Undo` calls
`plan_undo(op_id)`; if the plan has warnings or confirmations (`PLAYED_SINCE`, `UNDO_STALE`) it opens
in the Move dialog, else it executes directly. Undoing a fill of an empty slot would need a
deletion, which the app never does (F8): D5 blocks it (`UNDO_WOULD_EMPTY`) and the Undo opens the
"Empty this slot" steps instead (`empty_slot_guide`).

The rows update incrementally (no full rebuild, no lost expansion): CrossSaveTest-300qs now `S5 X4`,
state `placed 12:04, check pending`; Ex-Beachhead-160qs `vault only`; the S5 row of the live list
shows the clock; the status bar keeps "Last change 12:04: Swap into Steam slot 5 - checked
after the next game launch". D5 `verification_items` drive this: `pending` -> clock; `confirmed` ->
clock removed (a tick in the operation history); `reverted` / `deleted` -> an alert
bar ("Steam Cloud put the old save back in Steam slot 5. 'CrossSaveTest-300qs' is safe in the
vault." with `Put it back`, `Show steps`, `Dismiss`).

### 2.11 Screens 10a / 10b - the shell

![10a](../mockups/tk/10a-shell-tab.png)
![10b](../mockups/tk/10b-shell-organizer-first.png)

- **10a, chosen (default).** One main window: the native menu bar, then a `ttk.Notebook` with the
  tabs `Live saves and backups | Organizer` directly under it, and the status bar at the bottom. The
  first tab is today's window content (14-button toolbar inside the tab, the "Active live" selector -
  it keeps meaning "target of these classic buttons" only - and the two panes), with one change:
  `Clear slot...` becomes `Empty slot (steps)...` (the manual steps, F8). From phase 2 its Backups
  pane lists D3 `list_vault_entries` (old folders and store entries alike) and its restore and undo
  run on `open_entry_folder`, so nothing the store holds is invisible there (review V-09). The second
  tab is the organizer (01). The app remembers the last tab (`ui.last_view`) and opens there; Ctrl+1
  / Ctrl+2 and View > Organizer / Live saves and backups switch. 10a shows the classic tab selected.
- **10b, alternative (not chosen).** An organizer-first window with a `[Organizer | Backups
  (classic)]` view switch and no "Active live". Kept as an image only (re-rendered without a
  "Delete backup" button: the vault keeps everything).

### 2.12 Screens not mocked

| Screen | Content | Calls |
|---|---|---|
| Recovery (modal at start) | one row per `RecoveryItem`: labels, started, state text, choices `Finish the move` (forward) / `Put the slot back` (back) / `Leave it`; the recommended one is the default button; "Close No Man's Sky to finish" when the game runs | D5 `pending_recovery`, `resolve_recovery` |
| Empty this slot (manual steps)... | the app never empties a slot (F8): a non-modal window with the slot, what is in it and whether it is in the vault (a `Keep a copy in the vault now` button when it is not), the warning that Steam Cloud / Xbox sync may fill the slot again, then the numbered manual steps for Steam Cloud / Xbox; the next scan shows the result | D5 `empty_slot_guide(ctx, target)` (`occupied`, `durable`, `refill`, `steps`); `plan_keep` + `execute` for the copy |
| Keep a copy in the vault now | board slot menu: copies the slot's pair into the vault, no live write | D5 `plan_keep`, `execute` |
| Swap two slots... | pick the second slot on the board (or a 15-row list); one plan, two targets; with an empty side D5 blocks it ("use Move to live", `SWAP_EMPTY_SIDE`) | `plan_swap`, `execute` |
| Make the other save the one the game loads | board slot menu; hidden when that member already loads (R2 D7) | `plan_promote`, `execute` |
| Operations log | `op_history`, newest first, per slot; Undo on any `done` row; verification ticks | `op_history`, `plan_undo` |
| Restore whole folder from a snapshot... (classic) | today's whole-folder undo as the panic restore, account named, accountdata kept unless ticked; slots absent from the snapshot are left alone and listed | `plan_panic_restore`, `execute` |
| Upgrade the vault... (phase 2) | the one-time upgrade to the version store: free-space check, the copy aside (path, size, verified), then the automatic fold of the app's old folders with progress; "Undo the vault upgrade" while D3 allows it; until it ran, organizer moves show `VAULT_UPGRADE_NEEDED` | D3 upgrade and rollback, `store_status` |
| Copy into the vault... (hand-made folders) | lists hand-made in-place backup folders (outside the vault) with their size; copies the ticked ones in; default off, never automatic | D3 copy-in, `store_status` |
| Free old folders... (phase 5) | on request only, after a deep check: frees the space of folded old folders; the copy aside is never touched by the app | D3 reclaim, `store_status` |
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
3. **Lane images (F2-C).** For the drawn part of a line D1 gives `(row, lane)`, edges and
   `pass_through` (lanes computed over recorded, journal-observed and seen-in-backups edges only -
   owner decision).
   The UI converts them per row into segments: `up[lane]` (top half), `down[lane]` (bottom half),
   `diag[(from_lane, to_lane)]` (a branch joining its parent row) and the node glyph (disc; larger
   disc = branch point; ring = tip; platform-coloured disc with a white centre = live). A
   `tk.PhotoImage` per distinct row signature, width `lanes x 13 px + 6`, height **exactly** the
   Treeview `rowheight`, so the images join into continuous lines (VERIFIED: probe and mockups).
   Undrawn pixels stay transparent so zebra, group tint and the selection show through. Rows of the
   dated list get an image of the same width with only a grey dot (platform-coloured when live, a
   diamond for a restore-point-only fork member, three dots for a folded run), so the text column
   stays aligned. The separator row "Not linked, by date" has its own muted tag. Images are cached by
   signature and regenerated when the zoom or theme changes the row height or palette.
4. **Expand marker** (owner review). A line row's column-#0 image is a `>` (closed) or `v` (open)
   chevron drawn in code; `<<TreeviewOpen>>` / `<<TreeviewClose>>` swap it, and a click on the image
   (`identify("element", x, y) == "image"`) toggles the line. The built-in +/- indicator is removed
   from the `Treeview.Item` layout. VERIFIED: a style image element keyed on the `user1` (open)
   state drew `>` on open rows on this Tk 8.6.15, so the state route is not used. Lines get one
   placeholder child until expanded, so collapsed lines stay openable. Tk 8.6 allows images only in
   column #0 (R3), so every other status is text.
5. **Tags.** `odd` (zebra), `ver` (group tint), `dim` (lines not expanded while another line is,
   grey text), `sep` (the "Not linked, by date" separator), `run` (italic muted), `pending`, `event`.
   A tag styles the whole row (R3); no tag sets a font on version rows except
   `run`, so the lane column never leaks a font into other cells (R3's mono-font leak).
6. **Sorting.** Heading command -> `sort_lines(key, descending)`; versions stay newest first inside a
   line. The sorted heading shows an arrow image (heading images work in 8.6).
7. **Tooltips.** Today's `Tooltip`, but the text is built on hover (today 1,771 strings are built per
   refresh): full name and former names, live places, the note, the marker meaning.
8. **Drag.** `<B1-Motion>` beyond 6 px starts an `overrideredirect` ghost (alpha 0.9);
   `winfo_containing` over the board resolves the slot (R3 VERIFIED); the drop opens the Move
   dialog with that target selected.

### 3.2 Live area (`tk.Canvas` lists)

- Modes (owner review): `One` = one wide list of the live folder picked in a combobox (adds last
  played and the inline empty-slot sentence); `Two` = two lists side by side, each with its own
  combobox. The comboboxes list exactly the configured live sources (`LiveBoard.sources`: any number
  of Steam accounts, by alias label, plus Xbox when present). One configured source -> one list, no
  selector. No column is ever drawn for a platform the user does not have. Mode and sources persist
  in `ui.live_mode` / `ui.live_sources`.
- Data: D1 `live_board()` (`LiveSource` x `BoardSlot`), merged with D5 `verification_items()` for the
  clock / reverted markers and `busy_targets()`.
- Empty slots: cloud marker, tooltip and (one-list mode) inline sentence "Steam Cloud / Xbox sync may
  put a save back here at the next game start" (D5 `EMPTY_SLOT_MAY_REFILL`); the legend entry "empty:
  Steam Cloud / Xbox sync may fill it again"; the slot card of 2.2.
- Drawing: per slot a rectangle, the slot chip, name (truncated with "..." to the measured width;
  full name in the tooltip), the other-platform marker, play time, markers (polygons and ovals, no
  image files). About 400 items; redraw on data or theme change only. Items carry tags
  `slot:<token>`; hit-testing by `find_withtag("current")` and `find_overlapping`.
- Interaction: hover tint; click selects that slot's line in the table; right-click menu: `Replace
  with...`, `Swap with another slot...`, `Make the other save the one the game loads`, `Keep a copy
  in the vault now` (D5 `plan_keep`), `Undo the last change to this slot`, `Show history`,
  `Operations on this slot`, `Empty this slot (manual steps)...` (D5 `empty_slot_guide`; no menu
  item empties a slot); Enter = Replace with; arrow keys move a focus ring (the canvas
  takes focus; the ring is an outline item).
- More than two live sources (several Steam accounts): never more than two lists at once; the
  comboboxes pick which; each list header shows the alias label.
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

### 3.4 History window (style C, web layout)

`HistoryWindow(tk.Toplevel)` with the layout of 2.5: header with legend (Canvas), a left card
(`tk.Frame`) with the `Key / All` switch, the explanation, the table (3.1 rendering, a "How it is
linked" column, `sec` section rows) and the branch-tips grid (`tk.Label` lane tags in the lane
colours, buttons), and a right panel (callouts as tinted `tk.Frame`s, a read-only `tk.Text` for
changes, events, where and contents); `Keep this save file` stops following the selection. The
tree is D1 `history(pid, mode)`: drawn edges only (recorded, journal-observed, seen in backups;
01-consolidation 4a); the dated list is `HistoryGraph.dated`. Key mode uses D1's `VersionRow.key`
and run nodes in the tree; on the dated list the UI folds runs itself (no lanes are involved there).
Zoom changes rebuild the lane images. 23 versions here; R3 measured 2,000-node graphs fine.

### 3.5 Notes editor

`tk.Text` (`undo=True`, `wrap="word"`, `maxundo=-1`), `ttk.Entry` for the label with a counter
bound to `<<Modified>>` / `<KeyRelease>`, a `ttk.Checkbutton` for the lock. Saves go through the
worker (the notes file write takes the organizer store lock). Limits are enforced before calling D1
(label 80, comment 4,000) with the counter turning amber at the limit. The diff view is a read-only
`tk.Text` with `add` / `del` tags. Unicode: see 3.14.

### 3.6 Move dialog (Move to live / Replace with)

`MoveDialog(tk.Toplevel)`, modal (`grab_set`, `transient`), sized from content (no fixed geometry:
R2 found the copy dialog clipping its buttons at zoom 1.2), resizable, with a minimum size. Built
bottom-up so the footer, "Your pick" and "What will happen" always stay visible and the picker
takes the remaining height. Parts:

- `EndBox` x 2 (`tk.Frame` with a 2 px coloured border; `in_bg`/`in_fg` green, `out_bg`/`out_fg`
  amber) plus a Canvas arrow.
- `SlotPicker` (03): the account switch (segmented radio buttons; a blocked account is drawn
  disabled with its reason), the plan info strip, and a `ttk.Treeview` of the 15 `SlotCandidate`s
  with a radio `PhotoImage` in column #0 (images only in #0 in Tk 8.6), tags `chosen` (amber tint,
  bold), `sugg` (green bold), `muted`, `risk` (amber text), group rows for restore-older.
- `VersionPicker` (03b): Find entry, chips, a grouped `ttk.Treeview` (`grp` rows, lines with `>` /
  `v` images, versions with radio images; tag `chosen` = green tint).
- `PuttingInPanel`: a read-only `tk.Text` with the D4 facts of the source and its D4 changes.
- `OutcomePanel` ("What will happen"): the plan paragraph and severity rows (Canvas icon + Label,
  `ttk.Checkbutton` for `confirm`).
- `PickStrip` ("Your pick"): one line of Labels with `OUT` / `IN` tags in the two colours.
- Footer: note `ttk.Entry`, the outcome-named default button, Cancel.

It holds one `EvictionPicker` and the current `PlacePlan`; it never computes suggestions, outcomes
or warnings itself - every text comes from D5 (the two boxes and "Your pick" are assembled from the
plan's `source`, `targets[].evictions` and `outcome`). A `fingerprint` mismatch at execute returns
`stale` with a `replan`, which the dialog renders. Changing account calls
`eviction_picker(ctx, source, account)` again.

### 3.7 Compare window

`CompareWindow(tk.Toplevel)`: two cards (`tk.Frame` with labels), the ancestor strip (D1 graph:
walk both ancestor chains), a fact `ttk.Treeview` (tag `diff` = tint, `same` = muted) built from two
`FactsView`s field by field, and a read-only `tk.Text` for named sets from two `FactsCard`s.

### 3.8 Progress, Done bar and alerts

- **Index progress strip**: a `ttk.Frame` with a determinate `ttk.Progressbar`, the `message`, `ETA`,
  `Pause` / `Cancel`; shown while D4 `progress().state` is `running` or `paused`, hidden when idle;
  the status bar keeps "Contents read: n of m".
- **Done bar**: the existing `Banner.TFrame` style; one at a time; carries the op id.
- **Alert bars**: a stack above the table for D5 `VerifyItem` alerts, D3 `store_status` findings
  (damage, a missing copy aside, fold problems) and worker errors; each with its actions and
  `Dismiss`; dismissing calls D5 / D3 to record it.
- The only modals are the Move dialog, Recovery, and the confirmations D5 marks `confirm` (inside
  the Move dialog as checkboxes, not as message boxes; R4 D7).

### 3.9 Threading model (C31)

- One Tk thread; **no Tk call from any other thread** (a debug-mode assertion wraps the widget
  helpers).
- **Decoding** happens only in D4's spawn worker processes. The UI polls D4 with
  `root.after(250, poll)`: `events_since(last_seq)` -> refresh only rows whose vid changed
  (`view(vid)` is a < 1 ms cache read) -> update the strip from `progress()` (D4 2.8.6).
- **Other work** (D1 scan and read-model build, D3 `store_status`, D5 `eviction_picker` / `plan_*` /
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
- Images (chevrons, radio buttons, lanes, arrows) are generated in code at the current scale; nothing loads a bitmap
  file except the window icon.

### 3.11 Zoom

Today's named-font zoom (0.7-2.0, Ctrl+plus/minus) stays. Additionally: column widths, canvas
geometry and images are re-derived from `px()` (which includes the zoom factor); the Treeview row
height follows the font (exists); lane and chevron images are regenerated for the new row height;
dialogs re-measure. Native menus do not zoom (R2) - accepted.

### 3.12 Theming

`theme.py` gains palette keys (values used in the mockups, `mockui.EXTRA`): `steam`, `xbox`,
`warn`, `warn_bg`, `ok`, `muted`, `group_bg`, `zebra`, `lanes[4]` (main line blue, second branch
purple, third orange, as in the web mockups), `danger`, `pending`, `hl_bg`, `event`, `diff_add`,
`diff_del`, `card`, `chip_on`, `slot_empty`, `dim`, `callout_bg`, and the in/out pair of the move
dialog `in_bg`/`in_fg` (green) and `out_bg`/`out_fg` (amber); and ttk maps
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

UI strings stay ASCII (`tests/test_ui_text.py`). Data (names, notes, settlement names such as the
one with Greek letters in the owner's Main Save) is any Unicode and draws correctly on
Windows (04c). The Linux kit's Tk has no Xft and draws non-Latin-1 as garbage (R3 K4, Tk 9 too).
Design: on `tk windowingsystem == "x11"` a display filter replaces characters outside Latin-1 with
`?` in every displayed data string (table, board, cards, titles), and the notes editor refuses to
edit a note that contains such characters ("This note contains characters this build cannot show.
Edit it on Windows or with `nmsvault note`.") so saving never destroys text it could not show.

### 3.15 State and preferences

The last tab (`last_view`), window geometry, sash positions, columns (set, order, widths), sort,
chips, the live-area mode and sources (`live_mode`, `live_sources`), the history mode, and the last
selected pid are stored in `state.json` under `ui` (per machine; not user data, so not in
the vault). Today's fixed `1000x640` start geometry goes; the first start uses 80 % of the work area
with a minimum of 1,000 x 640 logical px.

## 4. Options

### 4.1 Shell - decided: 10a

The owner chose 10a (`02-ui-review.md`): the organizer is a tab beside today's panes, the tabs sit
under the menu bar, and the app opens in the last tab used. The organizer-first window (10b) stays
documented as an alternative only. Built as two frame classes (`OrganizerView`, `ClassicView`)
inside a thin `App` shell holding the menu bar, the Notebook and the status bar, so the shell can
change later without touching either view.

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
| `App.__init__` / state (281-378) | adapt | thin shell (menu bar, Notebook, status bar); `active_source_id` stays, used by the classic tab only |
| layout `_build_widgets` (380-488) | move to `ClassicView` (toolbar, Active live, panes) | the shell adds the menu bar and the `Live saves and backups / Organizer` tabs |
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
| Extract slot | kept in the classic view; in the organizer the vault already has every version, so "Extract" is rarely needed (it remains on the board menu as "Keep a copy in the vault now", D5 `plan_keep`) |
| Clear slot... | the app never empties a slot (F8): "Empty this slot (manual steps)..." (both tabs) shows what is in the slot, keeps a vault copy on request, and lists the manual steps (D5 `empty_slot_guide`) |
| Copy into live slot... / Copy slot N into live slot N / Replace from anywhere | Move to live... / Replace with... (one dialog) |
| Promote | board menu "Make the other save the one the game loads" (`plan_promote` on the clicked slot) |
| Undo (global) | Done bar Undo + per-slot Undo + operations log; the old behaviour survives as "Restore whole folder from a snapshot..." |
| Import... / Discover / Rescan / Refresh | File menu / Rescan button; Refresh merges into Rescan |
| Play time... | retired (the table) |
| "Save file:" history filter | retired (the table + History window) |
| "Active live" selector | kept inside the classic tab for its own buttons; never used by the organizer (C29) |
| modal "Done" / "Please wait" | Done bar / inline progress |
| "Proceed anyway (risky)?" | removed for organizer operations (D5 `GAME_RUNNING` blocks; unknown -> confirm checkbox) |

## 6. Test plan

The suite keeps exactly one Tk root per session (`conftest.py:121-158`; R2 / R3 K9), withdrawn.

- **Fixtures.** A synthetic `ReadModel` built by D1's test fixtures: two live sources (Steam 15 with
  one empty slot, Xbox 15), a forked line (a rollback branch, a side branch and a chain seen in
  backups), a cross-platform line whose Xbox copy is behind and one with journal-proven two
  branches, a version held only by a hand-made in-place folder, a vault-only line, an unnamed line
  and a legacy-id line; a fake
  `IndexService` with scripted `events_since` / `progress`; a fake `liveops` recording calls and
  returning canned `EvictionPicker` / `PlacePlan` / `OpOutcome` / `VerifyItem` objects (D5's
  dataclasses, so the fakes cannot drift). No test touches a real vault or live folder.
- **Table**: row values per line; the `>` / `v` image follows `<<TreeviewOpen>>` / `<<TreeviewClose>>`
  and a click on it toggles; other lines get `dim` while one is expanded; chips -> `LineFilter`
  (including `Vault only`); Find; sort keys and the arrow image; expansion and selection survive an
  incremental refresh; folded rows and markers.
- **Lane rows** (pure functions): node/edge -> per-row segments for the fixture graphs (a recorded
  rollback fork, an operation-started side branch, a chain seen in backups, two separate trees in
  one line); the split into the drawn tree (`VersionRow.in_tree`) and `HistoryGraph.dated`; key-mode
  runs in both; image height equals `rowheight` at zoom 0.7, 1.0, 2.0.
- **No recorded parent**: such a version shows "no recorded parent", no automatic text in the Note
  column, the notes window or the move dialog, and the fake `IndexService.changes` is never called
  for it.
- **Never empties a slot**: no menu item, button or dialog path exists whose plan deletes a live
  file (the fake liveops offers no such call); the empty-slot row of the move dialog shows D5's
  `EMPTY_SLOT` text and the board shows `EMPTY_SLOT_MAY_REFILL`.
- **Live area**: one / two modes with fixtures of one source (Xbox only: no Steam column drawn),
  two sources and three sources (two Steam accounts + Xbox); combobox values = configured sources;
  slot items per shown list; markers match `BoardSlot` fields; an empty slot shows the cloud marker,
  the tooltip text and (one mode) the inline sentence; hit test of a point returns the right token;
  context menu labels; keyboard focus movement; `ui.live_mode` / `live_sources` round-trip.
- **Shell**: the Notebook opens on `ui.last_view`; switching tabs stores it; Ctrl+1 / Ctrl+2.
- **Move dialog**: both entry modes build the same layout; the in/out boxes and "Your pick" follow
  each pick; the suggested row is tagged; selecting a blocked row is refused; the button text
  equals `plan.outcome`; confirm checkboxes gate the button and are passed as `confirmed`; `stale`
  renders the replan; `failed` shows Recover; `done` closes and shows the Done bar; Undo calls
  `plan_undo` with the op id; `VAULT_UPGRADE_NEEDED` blocks the button and links the upgrade window;
  the note reaches `execute(..., note=)`.
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
| 3 | Menu bar for the secondary actions (classic toolbar fits at zoom 1.2, P10) | S | 0 |
| 4 | pythonw log file handler (R3 K6) | S | 0 |
| 5 | Shell 10a: `App` = menu bar + `ttk.Notebook` (`Live saves and backups`, `Organizer`) + status bar; today's content moved unchanged into `ClassicView` (mechanical; existing GUI tests green); `ui.last_view` | M | 1 |
| 6 | UI infrastructure: `UiWorker` (executor + queue + `after` drain), `ThemeAware` registry, redaction helpers (item/set/text/canvas/title), raster module (chevrons, radio, lanes, arrows) | M | 1 |
| 7 | `OrganizerTable` with lines from D1, `>` / `v` chevrons, muted other lines, chips (incl. Vault only), Find, sort, column chooser, `ui` prefs | M | 1 |
| 8 | Live area: one / two modes with source comboboxes over the configured sources; canvas lists (read-only: draw, highlight, tooltips, focus); empty-slot marker, tooltip and slot card | M | 1 |
| 9 | `DetailPane` (Summary, Where, Notes read view) | M | 1 |
| 10 | Version rows on expand (default mode, fold markers; no lanes yet) | S | 1 |
| 11 | Notes window (save-file note + revisions, version label/comment/lock) and pins | M | 1 |
| 12 | Organizer tab assembled: toolbar, incremental refresh, start-up scan in the worker, alerts area | M | 1 |
| 13 | GUI tests with the synthetic read-model fixture; redaction scan | M | 1 |
| 14 | Move dialog (web mockup's layout): in/out boxes, slot picker, "What you are putting in", outcome panel, "Your pick", outcome-named button; plan per pick, warnings by severity, confirmations, execute with progress, outcomes | L | 2 |
| 15 | Replace with... on the same dialog (version picker with Vault only / Older versions / Live elsewhere) + drag onto the live lists | M | 2 |
| 16 | Done bar, Undo, operations log | M | 2 |
| 17 | Verification markers and alerts (D5 `verification_items`, D3 `store_status`); start-up recovery dialog | M | 2 |
| 18 | Board menu: Swap two slots, Promote, Keep a copy in the vault now (`plan_keep`); the "Empty this slot" steps window (`empty_slot_guide`) | M | 2 |
| 19 | Classic tab re-routed: its writes name their target (Active live pre-selects it); `Clear slot...` -> the steps window; Backups pane from D3 `list_vault_entries`; whole-folder restore = panic restore | M | 2 |
| 20 | Vault upgrade window (free-space check, copy aside, fold progress, Undo the vault upgrade) and "Copy into the vault" for hand-made folders (default off) | M | 2 |
| 21 | D4 integration: progress strip, event polling, facts columns, `facts:` sorting, first-run banner | M | 3 |
| 22 | Version card (D4 `card`), deep section on demand, "what changed" in rows (drawn parent only) | M | 3 |
| 23 | Lane images in version rows (F2-C) from D1 `history` | M | 4 |
| 24 | History window on the web mockup's layout (legend, How it is linked, section rows, key / all runs, branch tips, version panel) | M | 4 |
| 25 | Compare window | M | 4 |
| 26 | Note snapshot diff in the notes window | S | 4 |
| 27 | Combine / Split / "comes from" correction dialogs | M | 4 |
| 28 | Steam <-> Xbox account choice enabled when D5 opens F6; "Free old folders" window (D3 reclaim) | S | 5 |

Phases 2 and 3 are independent; their order can swap (as in 01-consolidation).

## 8. Risks, open questions, deviations

### 8.1 Risks

1. **Looks ceiling** (R3): whole-row styling and images only in column #0. Mitigation: state words,
   row tints, chevron / radio / lane images, tinted frames in dialogs; the `CellStyle` adapter for
   Tk 9.
2. **Linux Unicode**: non-Latin-1 names and notes cannot be drawn on the Linux kit; mitigated by the
   display filter and the edit guard (3.14), not solved.
3. **`App` refactor** of a 1,392-line class: done as a mechanical extraction first (step 5) behind
   the existing tests.
4. **Incremental refresh bugs** (stale rows): every refresh can fall back to a full rebuild that
   restores expansion and selection by pid / vid; tests cover both paths.
5. **Zoom and DPI**: a missed pixel constant clips text; the screenshot loop at zoom 1.2 and 2.0
   catches it.
6. **Canvas and PhotoImage drawing is aliased** (no anti-aliasing in Tk 8.6 on Windows); acceptable
   at 150 %, plainer than the web mockups.
7. **Native menus** stay light in dark mode and do not zoom (4.4).

### 8.2 Open questions (owner)

1. Default columns: the mockups show Glyphs, Parts, Legacy, Ships; Bases, Companions, Galaxies and
   Story are one click away. Which four does the owner want by default?
2. Pins: the mockups pin Main Save and All Parts Building to show the column; the owner pins his own.
3. Drag and drop onto the live lists: build it (step 15) or rely on the dialogs only.
4. Default live-area mode for a user with two or more live folders: Two (as mocked) or One.

### 8.3 Deviations from 01-consolidation section 6, and requests to sibling designs

No seam is changed: the UI consumes only D1's read model and notes API, D3's status and listing,
D4's index service and D5's entry points. The requests of the first pass are adopted by the sibling
designs:

1. **D5** `execute(ctx, plan, confirmed, progress=None, *, note="")`: the optional "Note for this
   move" reaches D5's op record and D1's `OpLineage.note`.
2. **D5** puts D1's `Line.cross_platform` into `LIVE_ELSEWHERE.data["cross_platform"]`, so the
   dialog can say "the Xbox copy is behind".
3. **D1** `history()` returns the drawn tree only (recorded, journal-observed and seen-in-backups
   edges, plus the pairs an operation placed or replaced) and `HistoryGraph.dated` for every other
   version; `VersionRow.in_tree` and `key` mark the rows.
4. **D5** `plan_keep` (keep a copy in the vault, no live write) and `empty_slot_guide` (the manual
   steps), with the info codes `EMPTY_SLOT` and `EMPTY_SLOT_MAY_REFILL` (review V-07, V-16, V-21);
   **D3** `store_status` replaces D2's `housekeep()` for the alerts (V-16).
5. UI preferences (`ui` in `state.json`) are per-machine settings, not organizer store data.

## Appendix - mockups and how they were made

| File | Shows |
|---|---|
| `01-organizer.png`, `01-organizer-dark.png` | Organizer tab of shell 10a: 30 lines with `>` markers, live lists Steam + Xbox, the empty-slot tooltip on Steam slot 1, Main Save summary; dark theme with the dark title bar and the light native menu bar |
| `02-line-expanded.png` | Main Save expanded (linked history with lanes - recorded moves, the live slot, seen in backups - then the dated list) and All Parts Building (one chain seen in backups, a folded re-save), other lines muted, #18 card |
| `03-move-to-live.png` | the move dialog from CrossSaveTest-300qs #2: in/out boxes, 15 slots (empty slot 1 suggested, "checked after the next launch"; slot 5 picked), What you are putting in ("no recorded parent"), What will happen, Your pick, "Swap into Steam slot 5" |
| `03b-replace-from-slot.png` | the same dialog entered from Steam slot 5: version picker (Vault only / Older versions / Live elsewhere), picking AF24 #2 |
| `04c-history-hybrid.png` | history on the web mockup's layout, key versions: linked tree with lanes and folded runs, the dated list of versions seen only in hand-made folders, branch tips, version panel |
| `05-notes.png` | notes window: save-file note, revisions, version label/comment/lock, snapshot diff, automatic changes |
| `06-compare.png` | #23 (main line) vs #22 (invented side branch) |
| `07-first-run-indexing.png` | first run: facts filling in, progress strip, banner |
| `08-cross-platform.png` | Creative Primary and The Cartographers (both "Xbox behind"), linked where app backups saw them, the rest by date; the move dialog going back to an older version (same-save-file group, confirm, gate) |
| `08b-live-one-source.png` | live area, one live folder with selector; empty Steam slot 1 selected (inline warning + slot card) |
| `08c-live-pick-two.png` | live area, two side by side; the source list open (with an invented second Steam account) |
| `09-done-undo.png` | after a Place: Done bar, pending verification |
| `10a-shell-tab.png` | shell 10a (chosen): tabs under the menu bar, classic tab shown (`Clear slot...` became the manual steps) |
| `10b-shell-organizer-first.png` | the alternative organizer-first shell (not chosen) |

Rendering: `uv run --with pillow python docs/organizer/mockups/tk/source/render_all.py` on Windows
(one process per screen; `SetProcessDpiAwareness(1)`, the app's `theme.apply`, window frame from
`DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`, `PIL.ImageGrab`, downscaled to 1,800 px with a
caption band). Data: `mockups/sample-data.json` only, read as `mockups/sample-data.md` says (no
inferred edge drawn; hand-made folders outside the vault); times in the owner's local time (UTC+3);
the vault is shown after the phase-2 upgrade with pruning stopped (every app-made copy durable). Tooltips and the open combobox
list in 01 and 08c are real Tk windows captured with the main window. Not shown in images: the drag
ghost (R3 verified it), the deep-facts spinner, animations, and the Linux kit (its Tk would show `?`
for the Greek settlement name).
