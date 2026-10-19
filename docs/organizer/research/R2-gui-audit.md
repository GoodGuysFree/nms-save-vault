# R2 - Current GUI audit, user flows, screenshots

Research for the save-file organizer (subject S7). Read-only with respect to the owner's data:
every screenshot and measurement below comes from the real app (v0.2.8 source, this branch)
running against **copies** in a sandbox. Tags: **VERIFIED** (measured/tested here),
**SOURCED** (cited), **INFERRED** (reasoned from code).

## 1. TL;DR

1. The GUI is **location-centric**: two `ttk.Treeview` panes (LIVE: folder > slot > save;
   BACKUPS: backup > slot > save) in one module, `gui.py` 2209 lines (1964 non-blank). A
   "save-file" exists only as the *Save file* filter and as rows of the *Play time* dialog
   (`playtime.Playthrough`) - that model is the natural seed for one-line-per-save-file.
2. Owner's real data through the real app: **30 save-files**, 29 occupied live slots (14 Steam +
   15 Xbox), **3 save-files live in two slots at once** (Steam + Xbox), 4 vault-only. The vault
   holds **575 slot copies but only 104 distinct versions**: the UI's "37 backups" for
   Exobiology1-600qs is 3 versions.
3. List rendering is not a problem: 1,771 backup rows populate in 28 ms, linear to 17,710 rows in
   238 ms; sorting 0.1 ms. **Blocking work on the Tk thread is**: save-file report 9.7 s cold /
   0.73 s warm (UI frozen), `refresh()` 0.2 s (85 % is `tasklist`), ~1 s while the filter is on.
4. **Three verified flow defects**: Promote writes to the *active* folder, not the clicked row's
   (`gui.py:1539`); Undo restores the newest snapshot of *any* folder into the active one (Xbox
   active -> "Coming soon"); Clear-slot's "newest copy in the vault" matches by **slot number** -
   14 of 15 Xbox slots are compared against a different Steam save.
5. The implicit **"Active live" write target** (Backup live, Restore, Undo, Promote, "Copy slot N
   into live slot N") is the structural hazard behind (4); confirmations do not name the target.
   With 30 live slots on two platforms an organizer should make every write name its target.
6. At the owner's real settings (packaged v0.2.8, light, **zoom 1.2**, default 1000x640) only
   **8 of 14 toolbar buttons are visible** (Undo, Rescan, Discover, Refresh, Accounts, Help are
   off-window; the bar needs 1410 px even at zoom 1.0), and the **Copy dialog hides its Copy /
   Cancel buttons** (needs 648 px in a fixed 540).
7. "Move to live" exists only as a chain of separate tools (Extract, Clear, Copy into live slot,
   Undo): 8-15 clicks, 2-3 modal waits, 2-3 "Done" boxes. The owner's oplog shows exactly that
   rotation (7 extracts, 3 clears, 7 repopulates, 17 full backups since 2026-06-30).
8. Dialogs show **locations, not content**: copy source = "slot 10 of full-steam-20261005-174129"
   (no save name); the chooser lists 51 folders by id; the slot list lacks play time, "has a
   vault copy" and version freshness - exactly what an eviction choice needs.
9. **No user notes exist.** The `note` fields in `catalog`/`savedir` are scan diagnostics
   ("Xbox/Microsoft save"); the only user text is the backup *label* (owner typed "ForVideo",
   "Steam20260822-1606-Vid").
10. Reusable as-is: `theme.py` (457 lines), `Tooltip`, `RedactingTreeview` + filtered
    messageboxes, `AccountsDialog`, update banner/installer (about 23 % of `gui.py`); adaptable:
    action handlers, worker, copy dialog (about 34 %); an organizer-centric UI replaces about
    **42 %** (layout, populate, menus, history filter, chooser, help).
11. A catalog entry (`inplace-20260624-182102`) **is the live Xbox folder** indexed "in place":
    it shows a stale 14-slot summary and is counted as one of the "6 backups" of every Xbox save.
12. 15 screenshots (removed from the repo; they showed the owner's real vault) (light, dark, expanded, tooltip, 3 context menus, copy and
    choose-source dialogs, clear confirmation, save-file filter, play time).

## 2. Findings

### 2.1 Method, sandbox, owner environment

- **Sandbox (VERIFIED).** Under the R2 work dir (`%LOCALAPPDATA%\Temp\claude\...\scratchpad\R2\`,
  scripts in `scripts\`): copies of the live Steam folder (minus `*.DDS`), the live Xbox `wgs`
  account and its `xgs` twin, the whole vault minus `*.dds` and the id cache (2,245 files,
  1.63 GB; file list, sizes and a 40-file sha256 sample identical to the source), and the 7
  in-place Steam folders. The catalog was rewritten so all 49 entries point into the sandbox.
  The sandbox was reached through `subst R:` so that no profile path (and, with
  `accounts.ini` aliasing both accounts to "Steam" / "Xbox", no account id) is ever drawn.
  Environment: `APPDATA`, `LOCALAPPDATA`, `NMSVAULT_PORTABLE_ROOT`, `USERPROFILE`, `HOME`, `TEMP`,
  `TMP` all on `R:\`; `update_check=off`.
- **Proof before launch (VERIFIED).** `scripts\r2env.py` resolved state path, alias path, NMS
  root(s), default vault, discovered Steam/Xbox live dirs, the xgs twin, all 49 catalog paths,
  discovered in-place backups, temp dir and home: every one inside the sandbox ("PROOF: PASS",
  saved as `proof.txt`). The GUI driver re-runs the proof in-process and exits if it fails.
- **Driving (VERIFIED).** `scripts\gui_driver.py` constructs the real `gui.App`, calls its own
  handlers (`_on_right_click`, `on_copy_slot`, `on_clear`, `on_playtime`, combobox post) and
  captures with Pillow. No dialog was ever confirmed (menus closed with Escape, message boxes
  answered No, dialogs cancelled). No process was left running.
- **First copy attempt failed on MAX_PATH** (`LongPathsEnabled=0` on this machine; sandbox path
  + wgs GUID folders > 260 chars). Fixed by working through `R:\`. Any future store layout that
  nests Xbox blob folders deeply under a long root will hit the same limit (VERIFIED).
- **Owner's real GUI setup (VERIFIED, read-only).** The packaged app in
  `%LOCALAPPDATA%\Programs\NMSSaveVault` (v0.2.8) keeps its own `state.json`: theme light,
  `font_scale` 1.2, update check on, aliases set for both accounts. `%LOCALAPPDATA%\NMSSaveVault\state.json`
  (the source-run config) has no UI keys. Screenshots therefore use zoom 1.2.
- **Display (VERIFIED).** The app is DPI-unaware: Tk reports 96 DPI and a 2560x1440 screen,
  while captures of the 1000x640 window are 1502x1008 - the owner's display runs at 150 % and
  Windows bitmap-scales the app.

### 2.2 Structure of `gui.py` and `theme.py`

| Lines | Part | Content |
|---|---|---|
| 1-67 | imports, formatters | `_fmt_ts`, `_fmt_play` ("29h01"), `_fmt_size`, `_fmt_created`, `_icon_path` |
| 69-128 | column specs, `SlotSource`, help dicts | `LIVE_COLUMNS` (5), `BACKUP_COLUMNS` (6), `ALL_SAVES`, tooltip colours |
| 131-196 | `Tooltip` | per-tree hover window, 450 ms delay, text precomputed per row |
| 199-247 | helpers | `_source_caption`, `_extract_slot_number`, `_restore_menu_label`, `_clear_question` |
| 250-278 | redaction | `_filtered` messagebox wrappers, `RedactingTreeview.insert` |
| 281-1673 | `App(tk.Tk)` | state/source combo 326-378, layout 380-488, theme/zoom 489-528, update check/install 530-740, panes 752-783, populate 785-964, sort 966-995, save-file history 997-1055, tooltip text 1057-1153, selection + context menus 1155-1300, worker 1302-1377, actions 1379-1673 |
| 1675-1737 | `PlaytimeDialog` | flat table: play time, platforms, copies, name |
| 1740-1814 | `AccountsDialog` | the only screen that shows real ids (by design) |
| 1817-1894 | `ChooseSourceDialog` | tree of every live folder and backup, slots as children |
| 1897-2030 | `CopySlotDialog` | "Copy from" + live folder combo + 15-slot list of current occupants |
| 2033-2187 | `HELP_TEXT` | 155-line help |
| 2190-2209 | `main` | aliases fail-closed, `App().mainloop()`; argv is ignored |

**Layout (VERIFIED, screenshots).** Top: a non-wrapping row of 14 `ttk.Button`s
(`gui.py:385-401`). Below: "Active live:" combobox (writable sources) and "Theme:" combobox
(403-421). Optional update banner (431-439). A vertical `ttk.PanedWindow` with LIVE SAVES
(weight 3) and BACKUPS (weight 2) (441-459); above BACKUPS the "Save file:" filter combobox,
width 80, `postcommand` refill (461-470). Bottom: status bar - active live, source count, vault
path, game state (423-426, 836-841). Fixed start geometry 1000x640 (`gui.py:285`), not
persisted.

**Rows (VERIFIED).** LIVE: one root per source (badge "- ACTIVE" / "- read-only (Xbox)", tags
active/live/readonly), occupied slots (name, difficulty, play time, saved), and the two saves
(Auto-Save / Restore-Point, "*" on the one the game loads; status valid/INVALID/moved/cloud)
(`_add_view`, 873-921). BACKUPS: one root per catalog entry (id, kind, slot count, label,
created), slots and saves from the catalog's cached summaries - no disk access (`_add_entry`,
923-964). Backup slot rows show no play time (933); top-level rows leave Difficulty and Play
Time empty (857-865).

**Row model and selection (VERIFIED).** Meaning lives in `_meta[(str(tree), iid)]` dicts with
`type` in {live, entry, slot, member} (787-797; keyed per tree after a past collision bug).
Selecting in one pane clears the other (777-783); the toolbar acts on the last-selected row
(`_selected`, 1157-1163). Slot-granular copying: a save row resolves to its slot (1165-1180).

**Context menus (VERIFIED, screenshots).** Built per click (1182-1300):

| Row | Entries |
|---|---|
| live folder | Set as active live target / Back up this folder now... / Copy a save into one of its slots... / Undo the last change to live saves (read-only folder: Import this Xbox folder as a backup) |
| live slot | Replace live slot N with a save from anywhere... / Copy live slot N into another live slot... / Extract live slot N to the vault / Clear live slot N (delete its saves)... / Show this save's history |
| live save | the slot's entries plus "Make the Auto-Save/Restore-Point the one the game loads (promote)" |
| backup | Restore all of '<id>' into live (replaces other slots), or for a one-slot extract "Put slot N back into live slot N" + "Copy slot N into a different live slot..." |
| backup slot / save | Copy slot N into live slot N / Copy slot N into a live slot... / Extract slot N to the vault as its own backup / Show this save's history |
| every row | Refresh / Account display names... / Check for updates... / Help |

**Worker / threading model (VERIFIED, code).** Writes go through `_run` (1304-1324) ->
`_run_worker` (1355-1377): a modal "Please wait" `Toplevel` with an indeterminate bar
(`_busy`, 1335-1353, grab, close disabled), the core call on a daemon thread, and the Tk thread
spinning `self.update(); time.sleep(0.02)` until it ends (a nested event loop). Errors come
back in a holder dict: `GameRunningError` -> "Proceed anyway (risky)?" and a forced re-run;
`FeatureNotYetAvailableError` -> "Coming soon"; `OperationError` -> "Operation failed"; anything
else -> `repr`. Success -> full `refresh()` then a modal "Done" (with snapshot id for undo).
The update check uses the cleaner pattern (thread + `self.after(0, ...)`, 577-590). Everything
else runs **on the Tk thread**: `refresh`, the save-file report (`_playthrough_report`, watch
cursor, 999-1009), Discover (synchronous `import_backup` loop, 1617-1629), Rescan, and every
dialog's folder scans.

**Refresh flow (VERIFIED, code + timing).** `refresh()` (799-841) deletes both trees, clears
`_meta` and the save-file report, rescans every live source, repopulates backups from the
catalog, refreshes the source combo and runs `tasklist` for the game check. It runs at startup,
after every write, on Refresh, on active-source change and after Accounts. Consequences: tree
expansion, selection and scroll position are lost after every operation (only the active
source is reopened); with a save-file filter active the report is recomputed on every refresh.

**Sorting (VERIFIED).** Backups pane only, top-level rows only (966-995); default newest first;
"saved" sorts by ISO `created`, "slots" numerically, the rest as lower-cased cell text, so the
empty Difficulty / Play Time headings sort nothing. The live pane cannot be sorted.

**Save-file (history) filter (VERIFIED).** `playtime.collect` folds every copy onto the game's
playthrough id (meta 0x15C, or the first data chunk on Xbox) across renames, slots and
platforms. The combo lists every playthrough with a backup as
"Name - was: Old  (platforms, play time, N backups)" (1019-1042); choosing one keeps only the
backups holding it and only the matching slot(s), opened (843-871). Right-click "Show this
save's history" does the same (1048-1055). It shows **copies, not versions**, ordered by backup
creation, with snapshots mixed in (seen in the removed history screenshot: the same 2026-10-02 22:50
version three times in a row).

**Tooltips (VERIFIED).** Text is built at insert time for every row (1,771 strings per refresh)
- source, slot, save, backup entry, backup slot/save variants (1059-1153). Tooltip colours are
a module-level dict mutated by the theme (127-128, 506-508).

**Redaction (VERIFIED, code).** Structural for tree rows (`RedactingTreeview.insert`, 266-278)
and message boxes (`_filtered`, 250-263); everything else is redacted call by call (status bar,
source captions, history labels, copy-dialog source, tooltips). `Treeview.item(text=...)`,
`.set()`, labels, canvas text and window titles are not covered by the structural guard -
unused for ids today. `main` refuses to start if `accounts.ini` is malformed (fail closed,
2190-2203).

**Zoom (VERIFIED).** Ctrl+plus/minus (all key spellings, `bind_all`, 482-487) resize Tk's named
fonts 0.7-2.0 in 0.1 steps (`theme.py:44-46, 124-133`); tree row height follows the font
(`theme.py:257-262`); saved quietly (515-528). Native popup menus do not scale (menu text is
smaller than the tree at 1.2 in the screenshots); fixed dialog geometries do not grow
(section 2.6).

**Theming (VERIFIED).** Light keeps the native `vista` theme on Windows; dark (and Linux)
switches to `clam` with full state maps for buttons, comboboxes, entries, scrollbars, headings
(`theme.py:241-425`); row tag colours and tooltip colours are swapped per palette
(`gui.py:491-508`); the Help `Text` is coloured by hand (1662-1668); combobox listboxes and
menus via the option database. `tests/test_theme.py` checks contrast pairs. The Windows title
bar stays light in dark mode.

**ASCII-only UI (SOURCED: `DESIGN.md:200-216`; VERIFIED: `tests/test_ui_text.py:49-80`).**
Every non-docstring string literal under `src/` must be ASCII (AST scan, so f-strings are
covered), because the Linux kit's Tk has no Xft and draws ISO8859-1 only. The rule does **not**
cover data: save names (and future notes) can be any Unicode. On this Windows build an emoji and
accented text round-trip unchanged through `Entry`, `Text` and `Treeview` (Python 3.12.10,
Tk 8.6.15; VERIFIED, `scripts\measure_more.py`); the Linux kit would draw non-Latin-1 as
garbage.

**GUI tests (VERIFIED, collection).** 412 tests in the suite; `test_gui_actions.py` 26,
`test_gui_panes.py` 20, plus GUI parts of `test_theme.py` (60) and `test_updates.py` (34);
`test_ui_text.py` 21 (one per source file). One session-scoped `App` over a 3-entry synthetic
catalog with no live sources, withdrawn window, ops monkeypatched (`conftest.py:121-187`); only
one Tk root per session works on this Python (`conftest.py:125-128`). Covered: menu labels and
what they invoke, per-tree row metadata, sorting, history filter, tooltips, selection
exclusivity, restore labels, copy/clear wiring. Not covered: Xbox or two-source rows (hence
D1/D2 below), dialogs' layout, `_run_worker`, refresh state loss, zoom vs fixed geometry.
Guard against real config writes is per test (`monkeypatch` of `appstate.save`), not
environmental.

### 2.3 User flows, step by step

Clicks count presses only (Enter/typing excluded); "Done" is the modal result box every write
ends with. Target = the folder written.

| Flow | Steps | Clicks | Confirmation | Target |
|---|---|---|---|---|
| Backup (full) | Backup live -> label prompt OK -> Done | 3 | label prompt | active live (right-click folder: any, 4 clicks) |
| Restore full | select backup root -> Restore -> Yes -> Done | 4 | "Any live save NOT in the backup is REMOVED" | active live, not named |
| Extract slot | (expand) select slot or save -> Extract slot -> Done | 3 (+1-2) | none | row's folder -> new vault entry |
| Copy into live, same slot no. | expand backup -> right-click slot -> "Copy slot N into live slot N" -> Yes -> Done | 5 | shows occupant name | active live |
| Copy into live, chosen slot | expand -> right-click -> "...into a live slot..." -> (folder combo 0-2) -> pick slot -> Copy -> Done | 6-8 | the dialog | dialog's folder |
| Fill a live slot from anywhere | right-click live slot -> "Replace ... from anywhere..." -> Change... -> expand folder -> pick slot -> Use this slot -> Copy -> Done | 8 + scrolling | the dialog | clicked folder |
| Promote | expand slot -> right-click save -> "Make ... (promote)" -> Done | 4 | none | **active** live (D1) |
| Clear slot | select live slot -> Clear slot... -> Yes -> Done | 4 | play-time gap + Steam Cloud text | row's folder |
| Undo | Undo -> Yes -> Done | 3 | "Undo the last operation ...?" (says neither what nor where) | active live + newest snapshot of any folder (D2) |
| Import | Import... -> pick folder -> Yes (copy) / No (index in place) -> Done | 3 + navigation | Yes/No | vault |
| Play time | Play time... -> Close | 2 | - | - (freezes 9.7 s cold / 0.7 s warm) |
| Filter by save file | Save file combo -> pick (reset: 2 more) | 2 | - | backups pane (first open freezes) |
| ... from a row | right-click slot -> "Show this save's history" | 2 | - | backups pane |

Composite, the organizer's core action today - "put vault save X into a full live set and keep
the evicted one": Extract the occupant (3) + Copy into live (5-8) = **8-11 clicks, 2 modal
waits, 2 Done boxes**; the owner's own rotation in the oplog (extract -> clear -> copy back) is
12-15 clicks. Any write while the game runs adds a "Proceed anyway (risky)?" step.

**Real usage (VERIFIED, copied `oplog.jsonl`, 42 records 2026-06-30..10-05).** full_backup 17,
extract_slot 7, repopulate_slot 7, restore_full 4 (2 of them inside undos), clear_slot 3,
undo 2, import_vault 1, promote_member 1. Two repopulates of the same slot 59 s apart
(2026-10-05 17:48 / 17:49).

**Friction points relevant to the organizer vision** (one line each):

1. No save-file object in the main view; versions of one save are spread over 30-37 backup roots.
2. Copies are counted as history ("34 backups" = 5 versions for SHIPSTOGIVE-1400QS); identical versions repeat in the filtered list.
3. Implicit active target for 5 actions; the confirmation never names the folder; Xbox is offered as a target for Steam sources and fails only at the end ("Coming soon", `gui.py:1915`).
4. Eviction is unsupported as a decision: the copy dialog's slot list shows name + date only - no play time, no "safe to evict" (vault has this version), no live twin on the other platform.
5. Sources are named by location ("slot 10 of full-steam-20261005-174129"); the chooser shows 51 roots by id in catalog order, names only after expanding.
6. Every write ends in a modal "Done"; chaining evict + copy multiplies modals and waits.
7. State loss after each op (tree rebuilt collapsed, selection and scroll reset).
8. Long freezes on first use of the save-file filter or Play time (9.7 s cold), ~1 s per refresh while filtered.
9. The live pane shows about 5 slot rows at the default size; 30 live slots need a different form (a board).
10. Unnamed saves: 7 live Xbox slots show "<unnamed>"; 2 of them are named Steam saves under the same game id (borrowable names).
11. Only folder-level labels exist for user text; no per-save, per-version note.

### 2.4 Screenshots

The 15 screenshots (main window light/dark, expanded rows, tooltip, context menus, copy and chooser dialogs, clear confirmation, history filter, play time) were removed because they showed the owner's real vault; the observations are in section 2.

### 2.5 Capacity and performance (VERIFIED, `gui_run.json`, `measure_more.py`)

Owner's PC, Windows 11, Python 3.12.10, Tk 8.6.15, files in the OS cache ("cold" = no id cache).

| Measure | Value |
|---|---|
| Rows per refresh | live 89 (2 + 29 slots + 58 saves), backups **1,771** (49 + 575 + 1,147) |
| `App()` incl. first refresh | 636 ms |
| `refresh()` | 193-251 ms, of which `tasklist` game check **175 ms** (Tk thread) |
| `scan_any` per live folder | 17-21 ms (Steam 14 slots, Xbox 15) |
| `_populate_backups` | 28 ms (49 entries); 49 ms (98); 94 ms (196); **238 ms (490 entries, 17,710 rows)** |
| sort backups | 0.1 ms |
| save-file report, no id cache | **9,741 ms**, UI frozen (180 Xbox first-chunk decodes) |
| save-file report, id cache present | 733 ms, UI frozen |
| `refresh()` with the filter on | **916-1,007 ms** |
| report size | 30 save-files, 1,207 saves in 51 folders, 1 unidentified (pre-Waypoint), 4 cross-platform |
| chooser rows | 655 |
| history combo | 31 items, longest label 83 chars |
| fit at zoom 1.0 / 1.2 | toolbar needs 1,410 / 1,720 px (window 1,000); copy dialog 605x549 / 605x648 (fixed 700x540); play time 703x390 / 780x463 (fixed 720x460) |

Read: a Treeview list of a few hundred lines with version children is cheap; the cost to design
around is decoding (the report, and far more so the content index of S2) and the per-refresh
process spawn.

### 2.6 Defects found while auditing (VERIFIED unless tagged)

| # | Defect | Evidence | Effect |
|---|---|---|---|
| D1 | Promote ignores the clicked row's folder | `gui.py:1528-1542` passes `self.live_dir`; sandbox: Xbox slot 1 Auto-Save row with Steam active -> `promote_member(<Steam folder>, 1, A)` (`verify_flows.py`) | error on an empty Steam slot, or silently promotes the wrong save |
| D2 | Undo ignores which folder its snapshot came from | `gui.py:1631-1640`, `operations.py:881-907`; sandbox, Xbox active: picks `snapshot-steam-20261005-174949`, real call raises "Transferring saves between Steam and Xbox / Game Pass is coming soon." with no write | Xbox undo blocked by a later Steam op; with two Steam accounts it would mirror account A's snapshot into B (INFERRED); a second Undo re-restores the same snapshot - effectively one level (INFERRED, record order at `operations.py:904-905`) |
| D3 | Clear-slot "newest copy in the vault" = newest entry holding the same slot **number** | `operations.py:647-668`; sandbox: 14/15 live Xbox slots compared against a different Steam save; Steam 14/14 correct | wrong play-time gap, false warnings and false "safe" |
| D4 | Live Xbox folder catalogued as an in-place backup | `inplace-20260624-182102` path = live wgs account; its summary is from 2026-06-24 (14 slots); `playtime` lists it among the 6 "backups" of every Xbox save (`xbox_sources.py`) | inflated counts; a "backup" that changes with play (restore onto itself is a guarded no-op, `operations.py:529-531`) |
| D5 | Toolbar does not fit | 1,410 px needed at zoom 1.0 (1,720 at 1.2) in a 1,000 px window | Undo/Refresh/Accounts/Help/Rescan/Discover unreachable without resizing |
| D6 | Fixed-size dialogs clip | see 2.5; screenshot | Copy/Cancel invisible at the owner's zoom (double-click on a slot still works) |
| D7 | Promote offered on the save already loaded | `gui.py:1256-1262`; screenshot | needless snapshot + write |
| D8 | Empty sortable columns | `gui.py:857-865`, `976-986` | clicking Difficulty / Play Time on backups does nothing |

### 2.7 Reuse assessment for a restructure (line counts INFERRED from the map in 2.2)

| Verdict | Parts | Lines |
|---|---|---|
| Keep as-is | imports/formatters, `Tooltip`, `_filtered` + `RedactingTreeview`, theme/zoom glue, update check + install, `AccountsDialog`, `main`; all of `theme.py` | ~508 of 2,209 (23 %) + 457 |
| Adapt | caption/clear-question helpers, App state, tooltip text builders, `_run`/`_busy`/`_run_worker` (fine for short writes, not for indexing), action handlers (core calls stay, targeting rewritten), `CopySlotDialog` (becomes the evict/move dialog) | ~748 (34 %) |
| Replace | column specs, layout, panes/selection sync, populate, sorting, history filter, context menus, `PlaytimeDialog` (its rows become the main list), `ChooseSourceDialog` (the list is the chooser), `HELP_TEXT` | ~922 (42 %) |

Tangles to undo: one 1,392-line `App` class mixing state, layout, population, menus and actions;
row meaning in untyped `_meta` dicts; the "active live" global used as an implicit argument;
full rebuild on every refresh; Tk-thread compute. Core calls are cleanly separated
(`ops.*`, `playtime.collect`, `savedir.scan_any`) - the front-end/core boundary is sound.

## 3. Implications for the design

**Constraints**
- Tk/ttk only, one Tk root per process (tests), standard library only. A `Treeview` comfortably
  holds the organizer list (30 save-files, 104 versions today; 17,710 rows measured at 238 ms);
  a lineage graph is not a Treeview job (a `Canvas`; R3 decides).
- Every new text path must go through `aliases.redact`; today only tree inserts and message
  boxes are structurally guarded - an organizer adds labels, cards, canvas text.
- No fixed geometries; layouts must survive zoom 0.7-2.0 and narrow windows; native menus will
  not zoom.
- Anything that decodes saves must leave the Tk thread (the report alone freezes 9.7 s cold);
  the `thread + after()` pattern of the update check is the template, not `_run_worker`.
- String literals ASCII-only; user notes are data - their charset is a decision (Q9).

**Risks**
- Carrying the implicit "active live" target into "move to live" repeats D1/D2 at 30-slot scale.
- Showing copies as history (575 vs 104) makes "number of past backups" meaningless.
- Eviction safety computed by slot number (D3) would mislead exactly at the eviction prompt.
- In-place entries that are live folders (D4) will look like versions unless classified.
- The rebuild-on-refresh model loses organizer state (expanded lines, selection) after each
  operation; the organizer needs incremental updates or state restore.
- Modal "Done" per write makes multi-step moves heavy; snapshot-per-write multiplies vault size.
- The GUI test harness has no two-platform, identity-bearing fixture; the organizer needs one.

**Opportunities**
- `playtime.Playthrough` already folds copies by game id across renames, slots and platforms
  (30 rows for the owner, with live locations and former names) - a ready line model to refine
  under S1.
- Names can be borrowed across platforms by id (unnamed Xbox twins of named Steam saves).
- `plan_clear_slot`'s idea ("what would be lost") is the right eviction preview once it is
  identity- and version-based.
- Theme, zoom, redaction, tooltip, update and accounts infrastructure carry over unchanged.
- The copy dialog's "both ends visible" principle is a good base for move-to-live.

## 4. Open questions and candidate options

| # | Question | Options and trade-offs |
|---|---|---|
| Q1 | Shape of the main window | (a) add an Organizer view beside today's two panes - least risk, two mental models; (b) organizer list + 30-slot live board as the main window, today's panes behind an "Advanced / backups" view - one model, biggest rewrite (~42 % of gui.py); (c) organizer in its own window - isolates the work, splits attention |
| Q2 | "Active live" | (a) keep as the single write target - simplest, keeps D1/D2-class risk; (b) remove: every write names its target slot (board) - safest, more UI; (c) keep only as the default pre-selection inside dialogs |
| Q3 | What "past backups" counts | (a) copies (today); (b) distinct versions (575 -> 104); (c) distinct versions plus branch structure (needs S3 lineage) |
| Q4 | Long-running work | (a) modal wait for writes + background indexer with progress in the status bar; (b) one job queue panel for all work; (c) index on demand only (per selected line) |
| Q5 | Feedback after a write | modal "Done" (today) vs non-modal status line / toast with an Undo link |
| Q6 | Undo in the UI | global last op (today) vs per-slot / per-folder undo vs a browsable list of snapshots ("put any earlier state back") |
| Q7 | D1-D3 | fix now on `main` as small independent bugfixes vs fold into the redesign (D3 matters for any Xbox clear today) |
| Q8 | Window / zoom | persist geometry and a minimum size; toolbar -> menu bar or overflow; opt into DPI awareness for crisp 150 % rendering (R3) |
| Q9 | Notes charset | Unicode (fine on Windows, garbled on the Linux kit) vs Latin-1 (Linux-safe, no emoji/CJK) vs ASCII |
| Q10 | Live folder catalogued in place (D4) | hide it, mark it "live" and exclude it from versions, or offer to remove the entry |
| Q11 | Save-files live on both platforms (3 today) | one line with two live markers vs one line per platform copy; how to show divergence between the twins |
