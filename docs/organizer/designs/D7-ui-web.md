# D7 - UI option W: the organizer as a local web page

Design wave, UI design D7 (subject S7, owner fork F1 option W; F2 decided). Branch
`design/save-organizer`. Binding inputs: `01-consolidation.md` (C1-C32, sections 6 and 7),
research R2-R4 and R6-R8, and the core designs D1 (read model), D4 (facts), D5 (operations);
D2/D3 for storage wording. Nothing under `src/`, `tests/`, `packaging/`, `README.md` or `DESIGN.md`
was changed. The mockups are throwaway design artifacts in `docs/organizer/mockups/web/`.

Evidence tags: **VERIFIED** = rendered or run by D7 (named below); **SOURCED** = read in a
design, research report or the code; **INFERRED** = reasoning. Live sources are shown as
"Steam" and "Xbox"; no account id or user path appears in this document or its images.

Owner decisions received during the design (01-consolidation section 4a; binding here):
**F2 = C** (version table with a lane column and a key / all toggle); **no guessed tree** -
versions from before recording started are a flat dated list, branches are drawn only from
recorded and observed history; **F3 = version store** (D3; the vault is copied aside once before
the upgrade); **F4 a** (the version goes live with its own restore-point partner); **F5 b** (the
placed save is stamped "now"; the original date stays in the vault record); **F6** Steam <-> Xbox
transfer last, after the owner's test runs; **F7 b** (another account's save is allowed with a
warning); **F8** organizer operations always replace, emptying a slot only shows the manual
steps; D4's index display defaults. **F1 (Tk or web) is still open**; this document is the web
side of that choice. D6 designs the Tk side with the same screen names.

## 0. Owner summary

1. Option W opens the organizer as a page in your normal browser; the small app window stays for start/quit, status and today's backup tools.
2. One table of all 30 save files under a 15 + 15 live-slot board, a side panel with facts and notes, "Move to live" as a side sheet that keeps the board in view.
3. Strengths vs Tk: richer and crisper layout, real branch graphs, any language in notes (also on Linux), easy screenshots and tests.
4. Costs vs Tk: a private local page server (this PC only, locked down), about 2,600 lines of JavaScript beside the Python, two windows instead of one.
5. Your choices are built in: history C (branches only from recorded history, older versions by date), "saved today" dates, replace-only moves; open choice: a normal browser tab now (my pick), an app window only after a test run on your PC.

## 1. Scope, non-goals, dependencies

**In scope.** Every organizer screen as a web page: the save-file table with live markers and
the live board (phase 1), notes (phase 1), move to live / replace / undo / verification /
recovery (phase 2), facts columns, version card, indexing progress and compare (phase 3),
history with branches and corrections (phase 4). The in-process HTTP server, its security,
lifecycle, packaging and tests. The small Tk host window and what happens to today's Tk flows.

**Non-goals.** No core behaviour: identity, versions, lineage and notes storage are D1; facts
are D4; every live write, plan, warning text, undo and verification rule is D5; storage is D3
(owner's choice). No third-party runtime (stdlib only, K1), no CDN, no framework, no build step.
No remote access of any kind: the page is reachable from this PC only.

**Dependencies (the UI reads and calls only these, 01-consolidation section 6).**

| Needs | From | Used for | Phase |
|---|---|---|---|
| `ReadModel.lines()/line()/versions()/live_board()/history()/resolve()`, all `to_dict()` | D1 2.10 | table, board, expanded line, history | 1, 4 |
| `LiveTarget.token` (opaque `"<platform>:<source index>:<slot>"`) | D1 2.10 | every target the page sends back; the browser never sees an account key | 1 |
| `Notes` API (`save_note`, `set_save_note(base_rev)`, `note_text_at`, `set_version_note`, `set_pinned`) | D1 2.9 | notes, labels, locks, pins | 1 |
| A monotonic `generation` per rebuilt read model (request, section 8) | D1 | change detection, refetch | 1 |
| `IndexService.view/card/changes/request/progress/events_since/pause/resume/cancel` | D4 2.12 | facts, card, what changed, compare, indexing banner | 3 |
| `eviction_picker`, `plan_*`, `execute`, `op_history`, `verification_items`, `pending_recovery`, `resolve_recovery`, `to_dict()` everywhere | D5 2.2 | move to live, replace, undo, activity | 2 |
| D5 live-op lock (`.liveop.lock`, 10 s -> `BUSY`) and "block app exit while `execute` runs" | D5 2.16 | one write at a time across browser, Tk and CLI | 2 |

## 2. Information architecture and screens

### 2.1 Structure and navigation

One page, four views, addressed by the URL hash so Back/Forward and reload work:

| View | Hash | Content |
|---|---|---|
| Save files (home) | `#/files` (+ `?sel=<pid>` / `<vid>`, `&open=<pid>`) | board, filter chips, the table, the detail pane |
| History | `#/history/<pid>` | style C: version table with lanes, key / all, branch tips, version card |
| Notes | `#/notes/<pid>` (+ `?v=<vid>`) | save-file note editor, version label / comment / lock, note snapshots |
| Compare | `#/compare/<vid>/<vid>` | facts side by side, differences only |
| Activity | `#/activity` | operations (undo), verification alerts, interrupted operations |

Overlays: the Move-to-live sheet and the Replace sheet (over Save files or History), toasts, the
indexing banner, the "app closed" overlay. The top bar is the same on every view: brand, view
tabs (Save files with the line count, History, Activity with a count of open items), search,
the game state pill ("No Man's Sky: not running", checked by the server every 15 s while a tab
is visible), Scan, theme.

Vocabulary is C32 only: Live, Vault, Version, Save file. Storage kinds never appear: with the
version store (F3) a version is shown as "Vault: since <first captured>", plus "first captured
by a backup / an automatic snapshot / an imported folder" as a muted detail.

Every screen below was rendered from the owner's real data (`mockups/sample-data.json`,
read-only export) by the mockup renderer: `docs/organizer/mockups/web/render.py` runs headless
Google Chrome 155 with a throwaway profile (VERIFIED, 12 PNGs, 1600x1000). Things the sample data
does not hold and the mockups invent are marked ILLUSTRATIVE: pins on Main Save and All Parts
Building, a lock on `9e4b2f00`, the note revision history, the edit in progress, and the date
recording "started" (2026-10-05 17:41, the backup that opens the first fully recorded operation,
the rollback), which decides what is drawn as a tree.

### 2.2 Save files (01, 01-dark)

![Save files, light](../mockups/web/01-organizer.png)

![Save files, dark](../mockups/web/01-organizer-dark.png)

**Layout, top to bottom.** Top bar (46 px). Live board (2 rows x 15 cells, 58 px each). Filter
chips. Then the table (flexible width) beside the detail pane (384 px). Status bar (28 px). All
30 lines fit without scrolling at 1600x1000 (23-px rows).

**Live board.** Row heads "Steam 14/15 used, Cloud on" and "Xbox 15/15 used, synced". A cell:
slot badge (`S9`, Steam blue / `X8`, Xbox green, plus the letter so colour is never the only
cue), the name the game shows in that slot (2 lines, unnamed = "(unnamed) 9589"), play time and
date. Markers: red corner = not in the vault yet (8 today: S2 S9 S11 S13 S15, X1 X2 X12); pin;
clock = waiting to confirm (09); history icon = an older version of its save file (X4 X8 X9
X14); flag = expedition in progress (X12). Empty slot = dashed "empty". Selecting a line
outlines its cells (01: S12 for All Parts Building). Cell actions (click, Enter, context menu):
"Replace with..." (03b), "Show save file", "Undo the last move here" (when D5 offers one), and
"How to empty this slot" - a help panel with the manual steps (F8: emptying a slot is not an app
action; under Steam Cloud a deleted save comes back, R8). Dropping a line or version on a cell
opens Move to live with that slot chosen.

**Chips** (counts from the current read model): All 30, Live 26, Not live 4, Steam 19, Xbox
15, On both 4, Not backed up 8 (amber), Has note 6, Expedition 1, Pinned 2; one chip group at
a time is "on" (accent fill, NN/g's visible active filter). Right side: Columns (show/hide,
order; saved in the app's settings), Expand all / Collapse all. Search (`/`) matches names,
former names, notes, version labels and, from phase 3, base, ship and companion names (D4
facts) - D1 `LineFilter.text` plus a D4 facts match.

**Table columns** (treegrid; header click sorts, `aria-sort` shown; default Last played,
newest first, D1's default): caret; Save file (name, "(unnamed)" + 4-hex id chip, "was
<former name>", pin icon); Live (badges, outlined = older version, red dot = not in the vault,
fork icon = diverged, swap icon = one platform ahead); Versions (distinct versions, C6; fork
icon + tip count when the line has branches: Main Save "23, 3"); Play time; Last played;
Difficulty (flag badge for an active expedition); Glyphs, Bases, Parts, Ships, Companions,
Legacy (phase 3 facts; Legacy = parts placed, "(89)" = none placed but all buildable, R6 option
(a)+(b)); Note / what changed last (the save-file note, else D4's automatic change summary of
the newest version, italic).

**Detail pane, line mode.** Name, id, platforms, version count, play time; a status callout
("Live in Steam slot 12 - the game loads the restore point; also in the vault" / "Not in the
vault yet - copied there before anything replaces it" / "In the vault only"); buttons Move to
live... (primary), History, Compare, Pin (toggle); Save-file note (Edit); Contents of the
newest version (D4 `card`): difficulty, expedition with progress and state ("Our Journey
Continues (Expedition 23) 3/48, paused - the main game loads"), where now, glyphs, bases by
type and parts, largest base, legacy parts placed / buildable, galaxies with bases, ships ("7 (3
corvettes) - primary Iron Vulture" with names and types; archived ships counted when present),
companions (custom names, else the creature type - D4 defaults), milestone chips, journey rank and words,
currencies, freighter, settlements (Unicode names such as "10ξρ111χυ Pass" render as stored);
Where it is (live slots, "Vault: since ...").

**Keyboard.** `/` find; Up/Down rows; Right/Left expand/collapse; Enter focuses the detail
pane; `M` Move to live; `H` history; `N` note; `P` pin; Ctrl+click a second row = compare;
Esc clears selection. The board is a grid: arrow keys move, Enter opens the cell menu.

**States.** Loading: the table skeleton for at most one frame (the read model arrives in about
12 ms, R3). Empty vault and no live saves: "No saves found yet. Check Accounts in the app
window" with a button that raises the host window. Read-only vault (D1 2.3): an amber banner
"The vault is read-only (<reason>). Everything is shown; nothing can be changed." and every
write control disabled with that reason as its tooltip. Scan problems (`ScanProblem`): one
banner line per kind ("Xbox folder missing"). Server gone: full-page overlay (2.13).

**Reads / calls.** `GET /api/session`, `/api/lines`, `/api/board`, `/api/lines/{pid}`,
`/api/versions/{vid}/card`; `PUT /api/lines/{pid}/pin`; `POST /api/scan`.

### 2.3 A line expanded (02)

![Main Save expanded](../mockups/web/02-line-expanded.png)

Main Save expanded in the table: its 23 versions (default mode: D1 folding of touches and
outside edits, C5; nothing folds here because the one touch is noted), newest first, each a
captured pair labelled by its newer member (C4). The caret column becomes the lane gutter with
the same rule as the history view (owner decision, 2.6): lanes and lines only for what was
recorded (the 10-05 rollback, the Vesper Ridge side branch, the abandoned 10-05 re-save; 3 lanes,
recorded moves thick); every version from before recording started is a grey dot with no line, so
the list below the fork reads as a dated list, not a guessed chain. Version rows reuse the line
columns, so facts change visibly down the list (parts 13,728 -> 15,016). Flags: fork, touch
(re-saved without play), edited outside the game, lock, "syn" for the invented sample branch.
Selected: `9e4b2f00`, the fork point.

**Detail pane, version mode.** Save file + "version"; date and play time; platform, which member
the game loads, difficulty; the label (note box); Move to live..., Compare..., Label, Lock; a
fork callout listing the branches that continue from it; "What changed since <previous>" with
how the two are linked ("recorded by the app", "seen by the app in the live slot", or "the
previous version by date (before recording started)"); Events in user words ("Putting it live
in Steam slot 9 did not stick: Steam Cloud brought the old save back", 10-05 17:48); Where it
is; Contents (D4 card); the version id in small mono text for CLI references.

Reads: `GET /api/lines/{pid}/versions?mode=default` (rows + lanes), `/api/versions/{vid}`,
`/api/versions/{vid}/card`. Keyboard: Right on a line opens it, Down walks into its versions.

### 2.4 Move to live, entered from a version (03)

![Move to live](../mockups/web/03-move-to-live.png)

A sheet from the right (1000 px), not a centred modal: the board stays visible and live above
it, so both ends are on screen (C30). The table behind is dimmed lightly but readable, the
source row selected.

- **From / Into cards.** From: "FreshStart, version 2026-04-28 08:16, 2h03, Custom, Vault: since
  ..." Into: "Steam slot 5, now Ex-Beachhead-160qs 7h17, played 2026-02-22 19:02, already in the
  vault - stays there, ready to come back".
- **Into account** (segmented): Steam on; Xbox disabled with a lock and "Moving saves between
  Steam and Xbox comes in a later version" (`CROSS_PLATFORM_GATED`, F6).
- **Plan warnings** as callouts (D5 `EvictionPicker.warnings`, e.g. `STEAM_CLOUD_OTHER_DEVICE`).
- **The 15 slots** (`SlotCandidate`s, slot order): radio, slot, what is in it now, play time,
  last played, In the vault (yes / NO), and a notes column for `suggest_reason`
  ("Suggested: empty") and short warning texts ("copied to the vault first", "pinned - not
  suggested", "also live in X14"). The suggested slot (S1, empty) has a green bar and the board
  cell a green dashed outline; the user's pick (S5) is highlighted in both places.
- **Footer**: the outcome in words ("Ex-Beachhead-160qs leaves Steam slot 5 and stays in the
  vault (already there) - one click brings it back. FreshStart goes in with its own restore
  point; the game lists it as saved today (the vault keeps 2026-04-28 08:16)." - F4 a and F5 b),
  Cancel, and the primary button with D5's outcome text ("Swap into Steam slot 5"). Confirmation
  codes (`confirm` severity) appear as a checkbox inside the chosen row (08); the button stays
  disabled until each is ticked; the codes go back in `confirmed`.
- **Another account** (F7 b): when more than one Steam account is configured, the account switch
  lists them; choosing another person's account shows D5's `OTHER_ACCOUNT` / `OTHER_PERSON`
  confirmation ("Bases may stay owned by the other account until you play it here") instead of a
  block. The owner's PC has one Steam account, so no mockup shows it.

Keyboard: the sheet traps focus; Up/Down or the slot number keys choose; Space ticks a
confirmation; Enter runs the button; Esc closes. Clicking a board cell also chooses that slot.

Flow and calls: open = `POST /api/move/picker {source}` (D5 `eviction_picker`, < 50 ms + game
probe); choosing a slot = `POST /api/move/plan {kind: "place", source, target}` (D5 `plan_place`,
read-only, 30-80 ms) which fills the footer; the button = `POST /api/move/execute {plan_id,
confirmed}` -> a job; progress steps (checking, preserving, snapshot, writing, verifying,
recording) show in the button ("Writing... 4/6") and the board cell; the result is the toast of
09. `stale` (the slot changed since the plan) re-renders the sheet with D5's `replan` and a
one-line notice; `refused` shows the block text in the footer; `BUSY` says "Another operation is
still running" (classic restore in the app window or another app instance).

### 2.5 Replace with..., entered from a live slot (03b)

![Replace with](../mockups/web/03b-replace-from-slot.png)

Same sheet, the other entry point (C30): the target is fixed (Steam slot 5), the user picks the
version. Left: the two ends, a search box, chips (Not live 4, Older versions of
Ex-Beachhead-160qs 1, Live elsewhere 16 Steam save files), and a grouped tree of lines with
expandable versions (radio per version). The 893F line shows its note as a warning row ("Permadeath
try - died. Parked for the video, do not restore."). "Live elsewhere" warns that a second live
copy makes two branches (`TWIN_LIVE`); Xbox-only save files are not listed until F6. Right:
"What you are putting in" - the chosen version's facts and its automatic changes (R4: show the
facts before a swap). Footer and button as in 03. Calls: `GET /api/lines`,
`/api/lines/{pid}/versions`, `/api/versions/{vid}/card`, then `plan` and `execute` as above.

### 2.6 History, style C (04c)

![History, style C](../mockups/web/04c-history-hybrid.png)

The owner chose C, and decided that history from before recording started is **not** drawn as a
guessed tree. The view therefore has two sections in one table:

1. **Recorded by this app** (since recording started; 2026-10-05 in the mockup): versions linked
   by an edge the app recorded (an operation: tier `recorded`) or observed itself in a live slot
   after it started journaling (tier `observed` from the live journal, D1 2.6), plus the older
   versions those branches start from ("branch starts here"). Main Save: the live `f073ea37`
   recorded from `9e4b2f00` (the 10-05 rollback), the abandoned re-save `b9c8b750` seen in the
   slot, and the Vesper Ridge side branch `syn-1..3` recorded from `6536c83c`: 3 lanes.
2. **Before recording started - by date**: every other version as a flat list, newest first, a
   grey dot instead of a node and no connecting line, "by date" in the link column. D1 still
   keeps its bootstrap and inferred edges (they feed "what changed" and the cross-platform
   state), but the UI never draws them as a tree.

Header: back to Save files, the line name, live badges, counts, and the legend: **recorded move**
(thick line), **seen in the live slot** (thin line), live (filled node), in the vault (hollow
node), **before recording: by date** (grey dot). Tier `owner` (a correction) draws like recorded,
with "set by you" in the link column.

**Key versions / All** toggle (`K`): the recorded section is always shown whole; in the dated
section, key = live, noted, locked, first version, first after a platform change or a rename, and
D4 `is_milestone` (D1 2.10.4); the rest fold into rows such as "10 more versions, 2026-06-23 to
2026-09-22 (520h43 - 529h55) - click to show". Main Save shows 10 of 23 (the mockup uses "new
station base" and "expedition started" as the milestone stand-in).

Columns: lane gutter (14 px per lane), Saved, Play time, Where (live badge / Vault chip / "not in
vault"), flags, How it is linked (recorded move / seen in the slot / branch starts here / by date
/ first seen), Parts, Bases, Label / what changed. Below the table, **Branch tips**: one row per
recorded branch (main line, Vesper Ridge side branch, abandoned re-save) with tip date, play time,
where, "from <start>, n versions since", and Move to live... / Compare with live. The right pane is
the version card of 2.3.

Phase 4 corrections (C15, D1 2.7.3) live in the version card's "..." menu: "This came from...
(set the previous version)" - which turns a dated row into a drawn branch with an `owner` edge -,
"Not related to its parent", "Combine with another save file...", "Split this version off".
Calls: `GET /api/lines/{pid}/history?mode=key|all`, the version endpoints, `POST /api/lineage/...`.

Keyboard: Up/Down versions, Left/Right jump between branches, `K` key / all, `C` compare with the
selected one, `M` move to live.

### 2.7 Notes (05)

![Notes](../mockups/web/05-notes.png)

Left, **save-file note** (one per line, C19): an editor (plain text, Unicode as typed: em dash,
arrow, Greek in the mockup), unsaved-changes badge, Save note (Ctrl+Enter) / Discard (Esc),
revision list (rev 3 editing now, rev 2 current, rev 1), the version labels on this save file
(6 of 23), and notes on other save files. Saving sends `base_rev`; a 409 `NoteConflict` (the
CLI or another tab saved meanwhile) shows both texts and offers "Keep mine" (re-save on the new
base) or "Use theirs".

Right, **one version** (f073ea37, live S9): Label (<= 80 characters), Comment, Lock checkbox;
**the save-file note when this version was saved** (rev 2) as a word diff against the parent
version's snapshot (rev 1: "Main." + "Never experiment here - copy to a free slot first."),
computed on the server with `difflib`; **what changed (automatic)** from D4 against the lineage
parent; and a table "The save-file note on each version" (which revision each recent version
carries; ILLUSTRATIVE). This is the owner's "keep that note with each backup, so a user can see
what they changed" made literal (C19, D1 2.9 `note_revs`). Versions known only from the
bootstrap have no snapshot and say so.

Calls: `GET/PUT /api/lines/{pid}/note`, `GET /api/versions/{vid}` (label, comment, lock,
`note_rev`, snapshot diff), `PUT /api/versions/{vid}/note`.

### 2.8 Compare two versions (06)

![Compare](../mockups/web/06-compare.png)

Main Save's side-branch tip `syn-3` against the live `f073ea37`; both continue from
`6536c83c` (2026-09-10 10:47): side branch +3h36, main line +6h14. Groups: Progress, Bases
(counts, parts, bases only on one side - "+ Vesper Ridge Retreat (planet, 540 parts, Euclid)"
vs four stations - and the same base at another size), Ships and companions (Hollow Lantern vs
Iron Vulture; Biscuit), Currencies, and a short "same on both sides" group. "Only differences"
on by default (`D`), Swap sides (`S`). The right pane names the common ancestor (D1 graph), what
each side did since, and next steps (Move left to live..., note on the left). Entered by
Ctrl+click of a second version anywhere, the History "Compare with live" buttons, or dragging
one version onto another. Calls: `GET /api/compare?a=&b=` = D4 `card(a)`, `card(b)`,
`changes(b, a)` plus D1's common ancestor; D4 queues both at `INTERACTIVE` when not indexed
(the page shows the 202 state of 2.9).

### 2.9 First run: facts being read (07)

![First run](../mockups/web/07-first-run-indexing.png)

The first scan finishes in about 3 s (D1 2.15); the table, board and meta columns (name, live,
versions, play time, last played, difficulty) are complete at once. A banner shows D4's
progress: "Reading what is inside your saves: 17 of 195 versions - live saves first, then the
vault. 4 background workers - about 20 s left. Everything else works meanwhile.", a progress
bar, Pause and Cancel, and the first-scan summary ("30 save files, 110 versions, 52 folders
(2.9 s)"; "History is recorded from now on; older versions are listed by date" - the owner's
no-guessed-tree decision, said once at the start). Rows whose representative version is
not indexed yet show grey skeleton bars in the facts columns and in the automatic-change
column (that text also comes from D4); rows fill in as `index` events arrive (live lines first,
D4 priority LIVE / TIP). The selected unindexed line's pane says "Being read - queued: 9
versions ahead of it" and shows the meta facts it already has. The Tk host's status line shows
the same counter. Calls: `GET /api/index/progress`, `POST /api/index {action}`, the event stream.

### 2.10 Steam and Xbox (08)

![Cross-platform](../mockups/web/08-cross-platform.png)

Chip "On both" (4 lines) with a column "Steam and Xbox" instead of the facts: Creative Primary
"Diverged - different progress" (amber fork icon), The Cartographers "Steam ahead by 2h35",
CrossSaveTest-300qs "Steam ahead by 40 min", Exobiology1-600qs "Steam ahead by 23 min" (D1
`Line.cross_platform` and `LiveBadge.behind_by`; the state is worked out from play time and
progress, so the words stay plain - "different progress", "ahead by" - and no tree is drawn
for it). Creative Primary is expanded: each version says Steam or Xbox; its history is from
before recording started, so it is a dated list of grey dots (the Xbox restore point `13981763`,
R7's real fork point, is a grey diamond); only the 10-08 step on Steam S13, seen by the app in the
live slot, has a line. The board outlines all seven live slots of these lines.

The sheet is the Move-to-live for Creative Primary's vault version 2026-10-04 09:01 (44h13):
- Into Xbox: disabled, "Steam <-> Xbox moves come later (after your two test runs)".
- Plan warning (fork icon): "This save file is also live in Xbox slot 8 with different progress
  (Steam and Xbox went separate ways, judged by play time and progress). Moving this Steam version
  does not touch the Xbox copy." (`LIVE_ELSEWHERE` plus the cross-platform state); info: "To
  continue on Xbox from Steam, use Hello Games cross-save in the game; the vault keeps both
  branches."
- Slots grouped: **Same save file** first - S13, suggested (`same_line`), button "Go back to this
  version in Steam slot 13", with the `PLAY_TIME_AHEAD` confirmation inline ("Steam slot 13 has
  59 min more play than this version (the 10-08 save, 45h12, not in the vault yet). It is copied
  to the vault first and stays there as its own branch." + checkbox); then **Other Steam slots -
  a second live copy of this save file (two branches)**, 6 shown, "8 more slots". The grouping is
  UI-only over D5's 15 candidates. Footer: "Steam slot 13 goes back to 2026-10-04 09:01 (44h13),
  listed in the game as saved today. The 10-08 save becomes a branch tip in the vault." (F5 b).

### 2.11 Done - Undo, and waiting to confirm (09)

![Done and undo](../mockups/web/09-done-undo.png)

After `execute` returns `done`: a non-modal toast (C30) "Done - FreshStart is in Steam slot 5,
dated today in the game. Ex-Beachhead-160qs is in the vault. The slot is checked again after
your next game session." with Undo (D5 `UndoOffer`, a new `plan_undo` + `execute`, slot-scoped,
C24; an undo of a move into an empty slot would need a deletion, so per F8 it shows the manual
steps instead) and Details (opens
Activity). The toast stays 15 s, longer while hovered; Undo stays available in Activity and in
the board cell menu. State updates in place, nothing collapses (fixes R2's state loss): FreshStart's
badge `S5` is dashed with a clock, the S5 cell is dashed accent with a clock ("waiting to
confirm") and today's date (F5 b; the version keeps its 2026-04-28 date in the table and the
vault), Ex-Beachhead-160qs now shows "Vault", Activity has a count, and the status bar says
"1 move waiting to be confirmed (Steam slot 5)". The pane's callout explains D5's `pending`
state: "after you play, the next scan checks that Steam Cloud kept this save. If it brought the
old one back, you will see it here with a one-click fix" (`reverted` -> "Put it back").

**Activity view** (no mockup; same table styles): D5 `op_history` rows (when, what, status,
verification, Undo for the newest done op per target), verification alerts with their actions
(`place_again`, `show_steps`, `dismiss`), and interrupted operations from `pending_recovery`
with D5's recommended choice first (Finish / Put back / Leave). An interrupted operation also
opens a banner on every view until resolved; new moves on that account are blocked
(`RECOVERY_PENDING`). Calls: `GET /api/ops`, `/api/verification`, `/api/recovery`, the matching
POSTs.

### 2.12 The host window and the browser (10)

![Host window and browser](../mockups/web/10-host.png)

The left window is real Tk (VERIFIED: Tk 8.6 with the app's own `theme.py`, light, DPI-aware,
captured with `PrintWindow`; content is a mockup). Sections: **Organizer** (state "Open in your
browser - 1 tab connected", Open organizer, Stop organizer); **Status** (game, Steam and Xbox
slot use and "not in the vault yet" counts, vault counts and indexing, pending checks);
**Classic tools** (Back up live now..., Backups (classic view), Restore a backup..., Import...,
Panic restore..., Accounts..., Check for updates, Help); "Open the organizer when the app
starts" (default on), Theme, Quit. The five notes on the image are the lifecycle and safety
rules of 3.5-3.6.

### 2.13 States common to every view

| State | Shown as |
|---|---|
| App closed / server gone | full-page overlay "NMS Save Vault was closed. Start it again, then click Open organizer." The stream's `bye` event shows it at once; a failed reconnect shows it after 3 tries. |
| Game running | top-bar pill red "No Man's Sky: running"; every write button disabled with "Close No Man's Sky first" (D5 `GAME_RUNNING` is the authority; `None` = amber "could not check" and D5's confirmation) |
| A write in progress | the triggering button shows the step; other write buttons disabled with "Another operation is running"; reads keep working |
| Read-only vault | banner + disabled writes (2.2) |
| Request error | toast with the server's ASCII text and Copy details; the page never shows a stack trace |
| Stale view | none: every view refetches on the `model` event, keeping selection, expansion and scroll |

## 3. Architecture

### 3.1 Module layout

```
src/nms_save_vault/webui/
  __init__.py   WebUI facade: start(app_ctx) -> WebUI; .open_browser(); .stop(); .status()
  server.py     OrganizerServer(ThreadingHTTPServer) on 127.0.0.1:0, Handler, static files,
                headers, request guard, JSON envelope, log routing
  security.py   tokens, session cookie, per-session header secret, Host/Origin/Sec-Fetch-Site
                checks, CSP and header constants (pure functions, unit-tested)
  api.py        endpoint table: parse + validate input, call D1/D4/D5, return to_dict() JSON
  events.py     EventBus (fan-out, sequence numbers, ring of 500) + the stream endpoint
  jobs.py       JobRunner: scan (coalesced), execute and recovery (D5), with progress events
  launch.py     redirect file, webbrowser.open, single instance (webui.json + host secret)
  static/       index.html, css/app.css, js/*.js (ES modules), no build step
```

The package imports `core` (D1 `core.organizer`, D4 `core.index`, D5 `core.liveops`) and
`state`; `core` never imports it. `gui.py` imports only the facade. `cli.py` gets `nmsvault ui`.
Python string literals stay ASCII (`tests/test_ui_text.py` scans the new `.py` files too).

### 3.2 Server and threads

- `ThreadingHTTPServer(("127.0.0.1", 0), Handler)`, `daemon_threads = True`,
  `protocol_version = "HTTP/1.1"` (keep-alive; R3's numbers assume it), request socket timeout
  30 s except the event stream. `serve_forever(poll_interval=0.2)` on one daemon thread.
- **Model holder.** The read side holds one immutable `ModelSnapshot(generation, readmodel,
  board, problems)`; a refresh builds a new one off-thread and swaps the reference under a
  `threading.Lock` (D1 `ReadModel` objects are frozen). Handlers read the current reference and
  never block on a refresh. Every read response carries `generation`.
- **Jobs.** `JobRunner` threads: refresh/scan (one at a time; a request during a scan marks it
  dirty and runs one more), `execute` / recovery (D5, which takes its own live-op lock; a second
  one gets D5's `BUSY` -> HTTP 409), and the D4 coordinator, which D4 owns (`IndexService`;
  spawn workers, C11). The server never decodes saves (C31).
- **Tk bridge.** Server threads never call Tk. The host polls a `queue.SimpleQueue` with
  `root.after(100, ...)` for status lines, "raise the window" and settings writes; the server
  reads host state through a small lock-protected status object.
- **Shutdown** (host quit): refuse while a D5 `execute` or recovery job runs (D5: "block app exit
  while execute runs"; the host says "A move is finishing..." and retries); otherwise publish
  `bye`, `server.shutdown()` from the Tk thread via a helper thread, close streams, stop D4
  (`IndexService.close()`), join with a 2-s timeout, delete `webui.json` and pending redirect files.

### 3.3 Endpoints

All JSON (`application/json; charset=utf-8`, `ensure_ascii=True`). Success: `{"generation": n,
"data": ...}`. Error: `{"error": {"code": "...", "text": "..."}}` with 400 bad input, 403 security,
404 unknown id, 409 conflict (`BUSY`, `STALE`, `NOTE_CONFLICT`, `GAME_RUNNING`), 423 read-only
vault, 500 internal (logged with a traceback; the text is generic). Ids: `pid` percent-encoded
in paths (legacy ids contain `:`), `vid` 64 hex, targets as D1 tokens; every id is validated
against the current model before use.

| Method, path | Calls | Returns | Phase |
|---|---|---|---|
| `GET /` (`?k=` once) | 3.4 | 303 + cookie, then `index.html` with the session meta tag | 1 |
| `GET /static/<file>` | whitelist from `importlib.resources` | asset | 1 |
| `GET /api/session` | app version, settings, sources (token, label, platform, writable, cloud), phase flags | session | 1 |
| `GET /api/events` | `EventBus` | event stream (3.3.1) | 1 |
| `GET /api/lines?sort=&desc=&filter=` | `ReadModel.lines()`, `sort_lines`, `LineFilter` | `[Line]` + chip counts | 1 |
| `GET /api/board` | `live_board()` + D5 `verification_items()` (badges) | `LiveBoard` + `[VerifyItem]` | 1 |
| `GET /api/lines/{pid}` | `line(pid)`, `Notes.save_note(pid)` | line + note | 1 |
| `GET /api/lines/{pid}/versions?mode=default\|all` | `versions(pid)` joined with `history(pid, mode)` lanes | rows + lanes | 1 |
| `GET /api/lines/{pid}/history?mode=key\|default\|all` | `history(pid, mode)` | `HistoryGraph` | 4 |
| `GET /api/versions/{vid}` | row, `version_note`, events, `note_text_at` + `difflib` against the parent's snapshot | version detail | 1 |
| `GET /api/versions/{vid}/card` | D4 `card(vid)`; if not indexed: `request([vid], INTERACTIVE)` | `FactsCard` or 202 `{status, queued_ahead}` | 3 |
| `GET /api/versions/{vid}/changes?vs={vid}` | D4 `changes(vid, vs)` | `ChangeSet` | 3 |
| `GET /api/compare?a=&b=` | D4 `card` x2, `changes(b, a)`; D1 common ancestor | compare | 3 |
| `GET /api/index/progress`, `GET /api/index/events?since=` | D4 `progress()`, `events_since()` (D4 2.8 names) | progress / events | 3 |
| `POST /api/index {action: pause\|resume\|cancel}` | D4 | progress | 3 |
| `POST /api/scan` | D1 refresh + D5 `update_verification` (job) | 202 `{job}` | 1 |
| `PUT /api/lines/{pid}/note {text, base_rev}` | `set_save_note` | note / 409 `{current}` | 1 |
| `PUT /api/versions/{vid}/note {pair, label?, comment?, locked?}` | `set_version_note` | version note | 1 |
| `PUT /api/lines/{pid}/pin {pinned}` | `set_pinned` | `{pinned}` | 1 |
| `POST /api/move/picker {source, account?}` | D5 `eviction_picker` | `EvictionPicker` | 2 |
| `POST /api/move/plan {kind, source?, target?, targets?, op_id?, position?}` | D5 `plan_place/swap/promote/undo` (no evict: F8, emptying a slot is a help panel) | `PlacePlan` (cached by `plan_id`, 10 min) | 2 |
| `POST /api/move/execute {plan_id, confirmed}` | D5 `execute` in a job | 202 `{job}`; result `OpOutcome` | 2 |
| `GET /api/jobs/{id}` | `JobRunner` | `{state, step, done, total, result}` | 2 |
| `GET /api/ops?target=&limit=` | D5 `op_history` | `[OpSummary]` | 2 |
| `GET /api/verification`, `POST /api/verification/{op_id} {target, action}` | D5 | `[VerifyItem]` | 2 |
| `GET /api/recovery`, `POST /api/recovery/{op_id} {choice}` | D5 `pending_recovery`, `resolve_recovery` (job) | items / job | 2 |
| `POST /api/lineage/{action}` | D1 corrections, Combine / Split | updated graph | 4 |
| `PUT /api/prefs {...}` | host -> `state.json` key `organizer_ui` | prefs | 1 |
| `POST /api/host/raise` | host queue | 204 | 1 |

Example (`/api/board`, one slot, D1 `BoardSlot.to_dict()` verbatim, values from the sample):

```json
{"target": "steam:0:9", "label": "S9", "state": "occupied", "pid": "DDDAD1BDD39F9C72",
 "name": "Main Save (S2)", "head": "f073ea37...", "partner": "54cfd111...", "member_loaded": "B",
 "play_time": 1916200, "saved_at": 1792096746, "vault_copy": "none", "pinned": true,
 "twin": false, "other_platform": [], "cloud": "cloud on", "pending_op": null,
 "last_event": null, "suggest_rank": null}
```

`PlacePlan`, `EvictionPicker`, `OpOutcome`, `VerifyItem` travel the same way (D5 2.2); the page
reads `outcome`, `warnings[].text`, `confirmations` and `blocked` and never builds its own
warning text. Plans stay on the server; the page sends back only `plan_id` and the confirmation
codes, so a page cannot forge a plan.

#### 3.3.1 Events

`GET /api/events` is read with `fetch()` and a stream reader (not `EventSource`, which cannot
send the session header). Body in `text/event-stream` framing:

```text
event: hello      data: {"generation": 41, "seq": 1020}
event: model      data: {"generation": 42, "changed": ["lines", "board"], "pids": ["DDDAD1BDD39F9C72"]}
event: job        data: {"job": "j-7", "kind": "execute", "step": "preserving", "done": 2, "total": 6}
event: index      data: {"done": 17, "total": 195, "eta_s": 20, "vids": ["..."]}
event: game       data: {"running": false, "checked": "2026-10-16T09:04:00Z"}
event: verify     data: {"items": [ ... VerifyItem ... ]}
event: bye        data: {"reason": "app-closing"}
: ping            (every 15 s)
```

On reconnect the page sends `?since=<seq>`; the bus replays from its ring (500 events) or sends
`model` with the current generation when the gap is too big. The open streams are the presence
signal (host status "1 tab connected"; `nmsvault ui` shutdown). Fallback when a local security
product buffers streams: the page switches to polling `GET /api/events?since=&poll=1` every
second (same payloads).

### 3.4 Security

R3 proved 13 checks on the stdlib (VERIFIED there: 13/13). Production keeps all of them and adds
the open points R3 named.

| # | Check | Rule |
|---|---|---|
| 1 | Bind | `127.0.0.1` only (never `0.0.0.0`, never IPv6 any); random port (`0`) |
| 2 | Launch token | 256-bit `secrets.token_urlsafe(32)`, single use, expires 120 s after minting; every "Open organizer" mints a new one |
| 3 | No token on a command line | launch through a **redirect file** (Jupyter's `use_redirect_file`): `user_cache_dir/webui/open-<random>.html` with a meta refresh to `/?k=<token>`, user-only permissions, deleted when redeemed or after 120 s; `webbrowser.open(<file uri>)` |
| 4 | Session | `/?k=` -> 303 to `/` with `Set-Cookie: nmsv-<port>=<session>; HttpOnly; SameSite=Strict; Path=/` (the redirect drops the token from history) |
| 5 | Host header | exactly `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding; Jupyter's rule) |
| 6 | Origin | when present, must equal this origin |
| 7 | Sec-Fetch-Site | when present, `same-origin` or `none` |
| 8 | Per-session header secret | every `/api/` request carries `X-NMSV-Session: <secret>`; the secret is put in the served page as `<meta name="nmsv-session">` only after the cookie check. Cookies are not isolated by port (RFC 6265 8.5), so the cookie never authorizes alone |
| 9 | Bodies | `POST`/`PUT` only with `Content-Type: application/json`, at most 64 KB, valid UTF-8 JSON objects; unknown fields rejected |
| 10 | No CORS | `OPTIONS` -> 403; no `Access-Control-*` header ever |
| 11 | CSP | `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; require-trusted-types-for 'script'; trusted-types 'none'` |
| 12 | Other headers | `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin`, `X-Frame-Options: DENY` |
| 13 | Output | save names, notes and every server text are set with `textContent` / `createElement` only; no `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `new Function`, inline handlers or `style` attributes (styles through classes or the CSSOM). Trusted Types (11) makes Chromium refuse HTML sinks outright; a lint test enforces the list for every browser |
| 14 | Static files | fixed whitelist; no directory listing, no path joins from the URL |
| 15 | Limits | at most 4 event streams and 32 concurrent requests per server; header size limits of `http.server` |
| 16 | Logs | method, path without query string, status, ms; never the cookie, token, header secret or a user path |
| 17 | Host endpoints | `/host/ping`, `/host/open` (single instance, 3.5) require `X-NMSV-Host: <host secret>` from `webui.json`, reject any request with an `Origin` header, and do nothing else |

Writes go through D5 and D1 exactly as from the CLI or Tk: game check, preserve, journal,
validation, live-op lock; the web layer adds no write path of its own. A process of the same
user can still read the host secret and the cookie store: that is the same trust boundary as
the vault files themselves (INFERRED; documented, not mitigated further). Binding the loopback
address showed no Windows Firewall prompt in R3 (INFERRED there).

### 3.5 Lifecycle

| Event | Behaviour |
|---|---|
| App start (GUI launcher) | the host window appears as today; the server starts lazily (12 ms, R3) when "Open the organizer when the app starts" is on (default) or on Open organizer |
| Open organizer | mint a token, write the redirect file, `webbrowser.open`; an existing tab keeps working (the session lasts for the server's lifetime) |
| Second app launch | the new process reads `webui.json` (`{pid, port, host_secret, started}` in the per-machine data dir, user-only), calls `/host/ping`, then `/host/open`: the running app raises its window and opens a fresh tab; the new process exits. Stale file (pid gone or no answer) -> ignored and replaced |
| Tab closed | nothing stops; the host shows "No tab open" and Open organizer brings it back |
| Tab reloaded / Back | same session cookie, same server; the hash restores the view |
| Host quit | refused while D5 `execute` / recovery runs; else `bye` event, page overlay, server down, redirect files and `webui.json` removed |
| Crash | no server survives the process (daemon threads); an open tab shows the overlay after 3 reconnect tries |
| Sleep / resume | the stream reconnects with `since`; one refresh |
| `nmsvault ui` (no Tk) | console process: prints "Organizer opened in your browser. Press Ctrl+C to stop."; exits 10 s after the last stream closes, or after 2 min if no tab ever connects (R3 measured the same rule at 6.3 s with a 4-s grace) |

### 3.6 pythonw and logging

Under the GUI launcher (`pythonw`), `sys.stdout` and `sys.stderr` are `None`, and the default
`BaseHTTPRequestHandler.log_message` makes every response fail (R3 K6, VERIFIED). The handler
overrides `log_message` and `log_error` to send records to `logging.getLogger("nmsvault.webui")`,
and the app configures a rotating file handler in the per-machine data dir (1 MB x 3) for all
loggers at start. Every caught exception is logged with its context (project rule); the client
gets the generic text.

### 3.7 Packaging and the README

- **Windows kit**: `build_portable.ps1` copies the package tree recursively, so
  `webui/static/` ships without change (R3, SOURCED `build_portable.ps1:136`); the kit test checks
  `index.html` is present.
- **POSIX kits**: `build_posix_kit.app_sources()` packs only `*.py` (R3 K3). Change it to also
  pack `webui/static/**` with an extension allowlist (`.html`, `.css`, `.js`, `.svg`) and add a
  test that the tarball contains them. Rejected: embedding the assets as generated `.py` modules
  (a generator step, two copies of every file, and the ASCII test would scan escaped blobs).
- Assets are read with `importlib.resources.files("nms_save_vault.webui") / "static"`, which
  works in the dev venv and both kits.
- **README** (`README.md:329` says the update check is the only network use). New text: "The
  organizer opens as a page in your browser. It is served by the app itself on your own PC
  (127.0.0.1), only while the app runs, and nothing else can reach it. The update check is still
  the only connection to the internet." Changed in the step that ships the server.

### 3.8 Front end

Vanilla JavaScript ES modules (`<script type="module" src="/static/js/main.js">`, allowed by
`script-src 'self'`), no framework, no build step, no third-party code.

| Module | Job | Lines (est.) |
|---|---|---|
| `main.js` | boot (session meta, `/api/session`), hash router, global keys | 150 |
| `api.js` | fetch wrapper (session header, JSON, error mapping), event stream reader + reconnect + poll fallback | 220 |
| `dom.js` | `h()`, `sv()`, icons (inline SVG paths), text-only helpers | 90 |
| `format.js` | play time `532h16`, local dates `YYYY-MM-DD HH:MM`, numbers | 70 |
| `store.js` | current generation, selection, expansion, filters, prefs sync | 140 |
| `views/board.js`, `views/lines.js`, `views/detail.js` | board, treegrid with lanes, panes | 900 |
| `views/move.js` | move and replace sheets, toast, job progress | 400 |
| `views/history.js` | style C, key / all, branch tips, corrections menu | 330 |
| `views/notes.js`, `views/compare.js`, `views/activity.js`, `views/indexing.js` | the other views | 600 |
| `css/app.css` | tokens (light / dark), layout, components | 600 |

About 2,600 lines of JS and 600 of CSS, ~110 KB unminified (served locally; no compression
needed). Measure for this estimate: the mockup renderer (`mockups/web/app.js`, 1,351 lines;
`app.css`, 484) already draws every screen from real data with the D1 lane algorithm, without
the API, state and interaction plumbing. The server computes everything that needs judgement
(sorting rules, lanes, key versions, warnings, diffs); the page renders, so JS stays thin.

**Settings.** The page has no browser-side storage that matters: the port changes every launch,
so `localStorage` (per origin) would reset; column choices, density, sort and chips persist in
`state.json` under `organizer_ui` through `PUT /api/prefs`, written by the Tk thread (the one
`state.json` writer).

**Theme.** CSS variables for light and dark; `prefers-color-scheme` when the app's theme is
"System", otherwise the server sets `data-theme` (the app's own Theme setting, shared with Tk).
Accent and live colours match `theme.PALETTES` (`#0a64c8` / `#5a9fd4`, `#0a6b2f` / `#5fd58a`); a
test compares them. `forced-colors` (Windows high contrast) uses system colours. Zoom: the app's
`font_scale` sets the root font size; browser zoom works too.

### 3.9 Unicode and fonts

Data strings are Unicode end to end (C22): JSON transport `ensure_ascii=True`, the browser draws
with the OS fonts (fontconfig on Linux), so notes and names in any script work on every kit,
which no Tk option can do on the Linux kit (R3 K4). Font stack: "Segoe UI Variable Text", "Segoe
UI", system-ui, -apple-system, "Noto Sans", Roboto, sans-serif; tabular numbers in tables. User
text gets `dir="auto"` (Hebrew notes read right to left inside an English UI). UI strings stay
ASCII, as in Tk and the CLI, so all front ends say the same words; only data is Unicode.

### 3.10 Accessibility and keyboard

Roles: `treegrid` (lines and versions, `aria-expanded`, `aria-level`, `aria-selected`),
`grid` (board), `dialog` with `aria-modal` and a focus trap (sheets), `radiogroup` (slot list),
`status` / `aria-live="polite"` (toast, progress), `progressbar`. Every action has a key (2.2-2.8)
and every drag a keyboard path (R4 F2). Focus ring always visible. Meaning never by colour alone:
S / X letters, "NO", icons with titles. Contrast AA in both themes (test on the CSS tokens like
`test_theme.py` does for Tk). Layout works from 1100 CSS px; below that the detail pane becomes
a drawer and the board cells show slot and play time only.

## 4. Options

### 4.1 History style (F2) - decided: C

The owner chose C (2.6), with branches drawn only from recorded and observed history and a flat
dated list for everything before recording started. Considered and rejected by the owner: **A**,
a linear timeline with restore events (a game-like list; a branch becomes "continues from <date>"
text, and the two sides of a fork interleave by date), and **B**, a separate lane graph (branches
at a glance, but a second view without the table's columns).

### 4.2 Browser tab or app window

| | Default-browser tab (recommended now) | Chromeless app window |
|---|---|---|
| How | `webbrowser.open(<redirect file>)` | the user's Chromium browser with `--app=<redirect file>` and **the user's own default profile** (no `--user-data-dir`) |
| Side effects | none (no new profile, R3) | none expected with the user's profile; **never** Edge or Chrome with a fresh private profile (R3: a fresh Edge profile silently signed into the Microsoft account and started sync) |
| Looks | a tab among others | an app-like window with its own taskbar entry |
| Close signal | stream drop (tab) | stream drop (window) |
| Works with | any browser, every OS | Chrome / Edge / Brave only; the executable must be found (registry / PATH) |
| Gate | - | an owner test run on his PC: open, close, reopen; check that no profile, sign-in or sync prompt appears |

Recommendation: tab in phases 1-4; the app window as a setting after the gate passes. Scripted
headless Chrome stays a test tool only (CI and mockups, throwaway profile, deleted).

### 4.3 Hosting

| Variant | What | Use |
|---|---|---|
| (a) Server thread in the Tk app (recommended) | `webui.start()` from the host window; one process shares the core, caches, D4 workers and D5's lock | Windows default; the GUI launcher |
| (b) `nmsvault ui` subcommand (recommended as well) | console process without Tk; same `webui.start()`; stops after the last tab | Linux / macOS users, development, tests |
| (c) Web-first launcher with a tiny Tk status window | (a) with the compact host of 10 as the start window | phase 2+ shell of (a) (section 5) |

## 5. Today's Tk flows and the host window

**Phase 1**: today's main window stays the start window. It gains an Organizer button (first in
the toolbar) and a status-bar item ("Organizer: open in your browser"); the toolbar overflow
(P10) is fixed in the same step by moving rarely used buttons into a menu. The organizer opens
at start when the setting is on.

**Phase 2+** (once Move to live works in the page): the start window becomes the compact host of
10; today's two panes move behind "Backups (classic view)" (same `App` code in a `Toplevel`).

| Today (R2 2.3) | Phase 1 | Phase 2+ |
|---|---|---|
| Backup live | classic | classic (host); a page button needs a D5/D2 entry point (section 8) |
| Restore (full) | classic | "Panic restore..." (D5 4.3) in the host |
| Extract slot | classic | classic (a vault copy of a live slot; the page does not need it: every move preserves first) |
| Clear slot | classic | page and host: "How to empty this slot" - the manual steps only (F8: organizer operations always replace) |
| Copy into live slot / Replace from anywhere | classic | page: Move to live / Replace with (D5 `plan_place`) |
| Promote | classic (P3 fixed) | page: version card "Make this one load" (D5 `plan_promote`) |
| Undo | classic | page: per-move Undo (D5 `plan_undo`); host: "Panic restore..." |
| Import | classic | classic (host) |
| Play time... | superseded by the page | removed |
| Save file filter / history combo | superseded | removed with the classic panes' filter |
| Accounts | classic (host; the only screen with real ids, by design) | same |
| Check for updates, Help | host | host; the page links to Help |
| Theme, zoom | shared settings | shared settings |

Reuse: `theme.py` styles the host unchanged (the 10 capture uses it as is); its palette is the
source of the page's accent and live colours. `aliases.redact` stays the only path from ids to
text (in core; the page receives redacted labels). `Tooltip`, `RedactingTreeview`, the update
check and `AccountsDialog` stay in the classic code. New Tk code: the host frame and the bridge,
about 200-300 lines. The DPI fix (R3 K5) should land with the host (the 10 capture is DPI-aware).

## 6. Test plan

| Area | Tests | Runs |
|---|---|---|
| Security | R3's 13 cases plus: per-session header required (constant header refused), token single use and expiry, redirect file permissions and deletion, cookie name per port, body size and content type, unknown JSON fields, path traversal on `/static/`, no `Access-Control-*` header on any response, CSP and other headers on every response including errors, host endpoints refuse `Origin` and a wrong host secret, no secret or query string in log records | pytest + `http.client`, no display |
| API | golden JSON per endpoint on a synthetic vault (D1 `org_vault` fixture) with fake D4 / D5 seams: shapes, `generation`, 404 / 409 / 423 mapping, `NOTE_CONFLICT`, plan cache expiry, `execute` -> job -> `OpOutcome`, `stale` -> replan, D5 `BUSY` while a fake Tk write holds the live-op lock | pytest |
| Events | ordering, `since` replay, gap -> `model`, ping, presence counts, `bye` on stop, poll fallback | pytest |
| Lifecycle | start/stop from a fake host; quit refused during execute; `nmsvault ui` exits after the grace; single-instance handshake and stale `webui.json` | pytest (subprocess for `ui`) |
| pythonw | server with `sys.stderr = None` (monkeypatched) answers 200 (regression for R3 K6) | pytest |
| Static | whitelist matches the files; POSIX `app_sources()` includes `webui/static/**`; JS lint scan: forbidden sinks (3.4 #13) and any `http://` / `https://` URL other than none | pytest |
| Theme | CSS accent / live tokens equal `theme.PALETTES`; contrast of text tokens >= 4.5:1 in both themes | pytest |
| Browser smoke | headless Chrome (throwaway profile, never Edge) loads the page against the fixture server; `--dump-dom` checks rows, board cells and no error overlay | CI on GitHub runners; skipped when Chrome is absent |
| Screenshots | the same harness writes PNGs per view as CI artifacts (ladder rungs T1/T3 for Linux without xvfb, D10) | CI |
| Tk host | the existing single `gui_app` root (K9): host buttons call the facade (mocked), status pump, close guard | pytest with the display (Windows), xvfb in Linux CI |

JS unit tests are not planned: the logic that needs tests lives in Python (D1 sort, lanes, key
versions; D5 plans; diffs). A dev-only Node runner would add a toolchain for little gain.

## 7. Implementation plan

Sizes: S < 200 lines with tests, M 200-500, L > 500. Each step is one commit with its tests
green. Phases per 01-consolidation section 5.

| # | Step | Size | Phase |
|---|---|---|---|
| 0 | Prerequisites elsewhere: P1 (Xbox meta), D1 phase-1 read model and notes; logging to a file under pythonw; DPI awareness for Tk (R3 K5) | - | 0-1 |
| 1 | `webui/security.py`: tokens, cookie, header secret, Host/Origin/Sec-Fetch-Site checks, header constants | S | 1 |
| 2 | `webui/server.py`: server, handler, static whitelist, JSON envelope, log routing; the security test suite | M | 1 |
| 3 | `webui/events.py` + stream endpoint + poll fallback | S | 1 |
| 4 | `webui/launch.py`: redirect file, browser open, `webui.json`, `/host/*` | M | 1 |
| 5 | `webui/api.py` read endpoints (session, lines, board, line, versions, version detail, scan job) on D1 | M | 1 |
| 6 | Front end: modules, router, board, lines treegrid with lane gutter, detail pane, chips, search, keyboard | L | 1 |
| 7 | Notes: endpoints, editor, labels / comment / lock, pin, snapshot diff | M | 1 |
| 8 | Host integration in `gui.py` (Organizer button, status, close guard, bridge, P10 menu) + `nmsvault ui` | M | 1 |
| 9 | Packaging: POSIX static glob + test, Windows kit check; README sentence | S | 1 |
| 10 | CI: JS lint, headless-Chrome smoke, screenshot artifacts | S | 1 |
| 11 | Move API: picker, plan cache, execute jobs with progress, ops, verification, recovery | M | 2 |
| 12 | Move and Replace sheets, drag to board, Done - Undo toast, Activity view, recovery banner, "How to empty this slot" help panel (F8) | L | 2 |
| 13 | Compact host window (10) and "Backups (classic view)" | M | 2 |
| 14 | Facts endpoints (card, changes, index progress / events / control) | S | 3 |
| 15 | Facts columns, version card, skeleton states, indexing banner, facts search | M | 3 |
| 16 | Compare view | M | 3 |
| 17 | History style C view (key / all, branch tips), versions endpoint lanes | M | 4 |
| 18 | Corrections UI (set parent, not related, Combine, Split) | M | 4 |
| 19 | Account switch unlocks Xbox targets when F6 opens (after the owner's test runs) | S | 5 |
| 20 | Optional: app-window setting after the gate (4.2) | S | later |

Phase 1 is about 10 commits (1 L, 6 M, 3 S); the whole option about 20.

## 8. Risks and open questions

| # | Risk / question | Mitigation or owner |
|---|---|---|
| 1 | A local web surface is new attack surface | 3.4 (17 rules), tests in CI; same-user processes remain trusted, as with the vault files |
| 2 | Two languages to maintain (JS beside Python) | thin JS (server computes), no framework, lint + smoke tests; about 2,600 lines |
| 3 | Two windows (host + browser) may feel clunky | organizer opens at start; compact host in phase 2; app window later (4.2) |
| 4 | Some security software buffers or blocks localhost streams | poll fallback (3.3.1) |
| 5 | Old browsers | needs a current Chromium, Firefox or Safari (ES modules, fetch streams, `color-mix`); the page says so when a feature is missing |
| 6 | Browser shortcut clashes | single keys only when focus is not in a text field; no Ctrl combinations except Ctrl+Enter in editors |
| 7 | Deadlocks between the page, Tk and CLI writers | all live writes go through D5's live-op lock; D1 store lock only per transaction (D5 2.16) |
| 8 | `webbrowser` opens an unusual handler on some Linux desktops | the host shows "Copy a link" (mints a fresh token each time) |
| 9 | Open: should "Back up live now" also be a page button? | needs a D5 or D3 entry point that captures live members into the vault with the same guards; until then it stays in the host |
| 10 | Open: F1 itself (Tk or web) | owner |

**Deviations from 01-consolidation section 6.** None in substance; the web UI consumes only
D1's read model and notes API, D4's index service and D5's entry points. Refinements and
requests:

1. Targets cross the page boundary only as D1's opaque `LiveTarget.token`; account keys never
   reach the browser (stricter than "passed through `aliases.redact` for display").
2. Request to D1: a monotonic `generation` on each rebuilt read model, and `versions(pid)` rows
   joined with the history lanes for the expanded line (the join is D1 2.10's option C data).
   Per the owner's "no guessed tree", `HistoryGraph` lanes and display edges must come only from
   edges of tier `recorded`, `owner`, or `observed` by the live journal; versions whose parent
   edge is a bootstrap `capture-sequence` or a classifier edge are returned as unlinked rows (a
   `flat` row kind) in time order, while D1 keeps those edges internally for "what changed" and
   the cross-platform state. The mockup implements exactly this rule (`recordedGraph` in
   `mockups/web/app.js`).
3. Request to D4: `changes(a, b)` accepted for any two versions of one line (Compare), not only
   a version and its lineage parent; D4 2.8 already names the web progress and events endpoints
   used here.
4. D5 objects are serialised verbatim with `to_dict()`; the page relies on D5's texts and codes
   and adds only the grouping of 2.10 (same save file first), which is presentation.
