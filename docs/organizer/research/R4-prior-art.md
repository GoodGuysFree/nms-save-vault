# R4 - Prior art and UX patterns

Status: research, wave 1. Feeds S3 (history tree), S4 (notes), S7 (UI); touches S1 and S5.
Date: 2026-10-16.

Evidence tags used below:

* **VERIFIED** - read directly by R4: tool source code, READMEs, CHANGELOGs, screenshots shipped in a
  repo, GitHub issues via `gh`, or primary patch notes.
* **SOURCED** - from a cited web page through a fetch/search tool that returns summarised text. Quotes
  came back marked as verbatim but were not checked character by character. "(snippet)" means only a
  search snippet was available because the page itself refused the fetch.
* **INFERRED** - R4's reasoning from the evidence.

Access limits: reddit.com is refused by both research tools (search and fetch), so community evidence
comes from Steam forums, GitHub issues and tool READMEs instead. nexusmods.com, helpx.adobe.com,
bulbapedia and fandom returned 403/402. Local copies of the downloaded sources and screenshots are in
`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\be828316-f5a0-4419-bd50-12964a376ba0\scratchpad\R4\`
(`dl\`, `readmes\`, `shirha\`).

---

## 1. TL;DR

1. **No NMS tool offers an identity-based view across backups.** NomNom/libNOM.io and goatfungus tie
   backups to the slot position (libNOM: `backup.{platform}.{metaIndex}.{time}.{version}.zip`, keep 3,
   restore only into the same slot). They have no notes and no history view. Users ask for exactly the
   missing pieces: names in the manager, "create a new slot from a backup", a backup that "cannot be
   restored due to the lack of an appropriate slot", and play time to tell copies apart. [VERIFIED]
2. **The closest analog anywhere is LaunchBox 13.27 (May 2026).** It has "save groups" (one save plus
   its history), **Active** vs **Vault** badges, a **No Backup** warning, **Backup History**, **Set As
   Active**, a group name plus a per-version label, and **Combine** / **Make New Save** to fix wrong
   grouping. Its model maps almost 1:1 onto the owner's ask. [SOURCED]
3. **The closest NMS hobby tool is shirha's "NMS Saves Manager" (late 2025).** It shows Pool and Saves
   tables side by side, keeps a `note.txt` per backup and a history per slot. Its author names saves
   like `S04-Corvette-#19`: when a tool only knows slots, players encode the slot and the expedition
   into the in-game name, which is capped at 42 characters. [VERIFIED]
4. **Three history models are proven.**
   * A linear log where a restore is just an event (Figma, Google Docs, LaunchBox).
   * An explicit tree (Vim undotree, Emacs undo-tree, git graphs).
   * A story flowchart (Detroit, Zero Escape).

   Photoshop's default is the anti-pattern: acting from an older state deletes the newer ones.
   [SOURCED]
5. **Readable trees share a set of features.** Newest on top; one lane per branch; nodes labelled with
   human facts rather than ids; a distinct "you are here" or live marker; uneventful runs collapsed
   ("Only show named versions"); a per-node diff; compare any two nodes. undotree does all of this in
   plain ASCII, which fits the app's ASCII-only Tk UI. [VERIFIED/INFERRED]
6. **Notes work best on two levels.** A name or notes for the save-file, plus a label or comment per
   version (LaunchBox, Figma, Google Docs, Ludusavi). Add a lock or star that exempts a version from
   pruning (Ludusavi, Elden Ring SM, Terraria favourites). Ludusavi stores comments inside the backup
   folder, so notes travel with the backup. [VERIFIED/SOURCED]
7. **People rarely annotate every version.** Evidence: Google's "Only show named versions", Figma's
   grouped autosaves, and Variolite's finding that users version informally. An automatic "what
   changed since the parent version", built from the content index, is the realistic default note.
   [SOURCED/INFERRED]
8. **Eviction needs an in-context swap.** When slots are full, offer the swap right there. Pokemon
   offers "send to Boxes or swap", and Xbox Quick Resume asks "which pin you want to replace". BOTW's
   "out of room, and nothing more" is the cited anti-pattern. The evicted item should stay visible
   with a status, one click from coming back (Switch Archive, iOS offload, LaunchBox). [SOURCED]
9. **Safety means sparing confirmations plus undo.** Confirm only consequential actions, name the
   outcome on the button, and always offer undo (NN/g 2018, Raskin 2007). Save managers add an
   automatic backup before restore, verification after it, and hold-to-confirm for deletes.
   [SOURCED/VERIFIED]
10. **The organizer table has a mainstream template: Outlook's conversation view.** It shows one line
    per thread with an expand triangle, newest first, and "Show Messages from Other Folders". Combine
    it with NN/g's four table tasks, visible active filters, columns that can be hidden or shown, and
    a frozen header. ttk.Treeview supports the hierarchy, columns, tags and row images this needs.
    [SOURCED/VERIFIED]
11. **What NMS players face and want:**
    * Slot count rose from 5 to 15 in Frontiers (2021-09-01).
    * In-game rename and a location summary arrived with Waypoint (2022-10-07).
    * To cope, players delete their least-played expedition saves or archive folders by hand.
    * They ask for "a better management system (like importing or exporting)".
    * They keep old saves for legacy base parts.
    * They track bases and ships across saves with separate tools.

    [VERIFIED/SOURCED]
12. **Keep the concepts clean (Gitless, OOPSLA 2016).** Surface three concepts: save-file, version and
    live slot. Keep the storage kinds (full, snapshot, extract, in-place) out of the main view.
    Section 6 has five concepts to mock up. [SOURCED/INFERRED]

---

## 2. Findings

### 2a. NMS-specific tools

| Tool | Per save it shows | History model | Notes | Slot operations |
|---|---|---|---|---|
| NomNom 4.00-7.00 | mode, play time, last save; later name, summary, expedition | backups per slot position, keep 3 | none | delete/copy/move/swap per row; transfer wizard |
| goatfungus editor | slot combobox: slot, mode, name; file, modified, name, summary | backup zips listed in the slot's file dropdown | none | Save As into another slot |
| shirha NMS Saves Manager | slot, modified, runtime, name | per slot, newest first; dedupe by mtime | `note.txt` per pool entry | Pool to Saves, Backup Saves, Delete |
| NMSCoordinates | bases/stations across all saves | zip of whole folder on startup | none | none |
| Atlas Archive / nefilmjp / nmssavetool | none or minimal | folder zips with retention / lock / CLI | none | restore |

**A1 - NomNom Manager screen.** [VERIFIED: repo screenshot from version 4.00.0.31-beta.1, 2022-10-31]

* A "Detected" grid at the top (Platform, Identifier, Open), then Platform and Path.
* A "Slot" grid with columns `#`, mode icon, Game Mode, Total Play Time and Last Save. Each row also
  carries icon-button columns: **Delete, Copy, Move, Swap**.
* Below, a "Save" grid for the selected slot. It lists the slot's saves and its backups as sibling
  rows, with columns Type (Auto/Manual), Game Version, Last Save, Synced, Compatible and Backup (a
  checkbox). Toggles: "Hide Backup", "Hide Incompatible".
* The repository ships no application source (README, CHANGELOG and images only).
* Sources: https://github.com/zencq/NomNom/blob/main/.github/images/manager.png and
  https://github.com/zencq/NomNom

**A2 - How NomNom's list grew, in response to requests.** [VERIFIED: CHANGELOG]

* 2.4.0.6 (2020-08-28): "Slot management: delete, copy, move, swap"; game version on the manager page.
* 3.1.0.16 (2021-06-26): "Copy/Move/Swap is still triggered on slots but the actual action is decided
  for each save".
* 3.5.0.17 (2021-08-25): transfer to another platform or account.
* 3.6.0.19 (2021-10-28): "Manager now shows from which expedition a save is".
* 4.00.0.31 (2022-10-31):
  * "Name and Summary of a save".
  * "Built-in backup recovery".
  * "Option to hide backups and incompatible saves from the list".
  * "Actions to delete/copy/move/swap slots are now available as individual buttons in the list".
  * A standalone Save Transfer wizard.
* 5.20.x (2024-12-01): "Showing saves names in save selection", closing issue #222.
* Takeaway: even the leading editor added names and expedition provenance to its list only after
  users asked.
* Source: https://github.com/zencq/NomNom/blob/main/CHANGELOG.md

**A3 - libNOM.io backup model.** [VERIFIED: source]

* `CreateBackup` writes a zip with `data` and `meta` entries, named
  `backup.{PlatformEnum}.{MetaIndex:D2}.{createdAt}.{GameVersion}.zip`.
* `MaxBackupCount` defaults to **3**. The oldest backups are deleted on each new backup.
* Backups are found by platform plus `MetaIndex`, which is the position of the save within the slot
  layout. A run moved to another slot loses its backups; a different run placed into that slot
  inherits them.
* `RestoreBackup` refuses unless a container with the same `CollectionIndex` exists ("There is no
  IContainer with index ... to restore to").
* There is no label or note field.
* `IPlatform` also exposes Copy, Move, Swap and Delete (singly or as pair lists), and
  `Transfer(TransferData)`. A transfer covers bases with a per-base decision, ByteBeats, discoveries,
  settlements and user identification, and "works similar to copy but with additional ownership
  transfer".
* Files: `libNOM.io/Platform/Platform_FileOperation_Backup.cs`, `Settings/PlatformSettings.cs`,
  `Interfaces/IPlatform.cs` in https://github.com/zencq/libNOM.io

**A4 - What libNOM.io can show cheaply, and what needs a full load.** [VERIFIED: source]

* `IContainer` exposes SaveName (the in-game name, max 42 characters), SaveSummary (up to 127),
  Difficulty, GameVersion, Season, TotalPlayTime, LastWriteTime, SaveType and IsSynced.
* `HasBase`, `HasFreighter` and `HasSettlement` need the JSON loaded (`IsLoaded`).
* `HasActiveExpedition` is true for the seasonal game mode, or when an expedition context exists in
  the loaded JSON.
* Loading strategies:
  * `Empty`.
  * `Hollow` - "All save information loaded but no data".
  * `Current`.
  * `Partial`.
  * `Full`.

  The default is `Empty`.
* NomNom's changelog records a known issue where save metadata was shown wrongly in the Manager
  unless the *Full* loading strategy was used.
* The established pattern is a two-tier list: cheap meta columns first, deep facts on demand.
* Files: `Interfaces/IContainer.cs`, `Container/Container_Property.cs`,
  `Container/Container_Property_Save.cs`, `Enums/LoadingStrategyEnum.cs`, `Global/Constants.cs`.

**A5 - NomNom Save Transfer wizard.** [VERIFIED: `transfer_save.png`]

* Source and Destination panes side by side. Each holds a platform list and a slot list (Game Mode,
  Total Play Time, Last Save). A "Transfer" arrow sits between them.
* Component checkboxes (Bases, ByteBeats, Discoveries, Settlement), plus a per-base list with a
  Transfer checkbox.
* Identity fields (USN/UID/LID/PTK).
* A red warning that already-uploaded bases and discoveries will duplicate.
* Issue #61 on the earlier version: "the copy save button is way too small and it took me a good few
  minutes to realize i had to click it" (https://github.com/zencq/NomNom/issues/61).

**A6 - goatfungus NMSSaveEditor** (2.7k stars). [VERIFIED: README, screenshot, CHANGELOG, issues]

* The Main tab shows:
  * Storage and Save Path.
  * A "Game Slot" combobox, e.g. `Slot 1 - NORMAL - Other save`.
  * A "Save File" combobox holding `save2.hg` or a backup `.zip`.
  * Modified, Save Name, and a Description holding the in-game summary, e.g. "On freighter (...)".
  * Buttons: Reload, Save Changes, Save As.
* Thirteen content tabs follow: Exosuit, Multitool, Ships, Squadron, Freighter, Frigates, Vehicles,
  Companions, Bases & Storage, Settlements, Discovery, Milestones / Reputation, Account.
* Restoring a backup means picking the zip in the slot's Save File dropdown and pressing Save Changes
  (#1080). Users keep asking how: #980 "How do I replace a Save Game with a backup?"; #1080.
* The author, on the slot picker: "It's tricky to display so much information on one small dropdown"
  (#1110).
* Changelog 1.8.0 (2021-09-06): "Expanded number of save file slots from 5 to 15".
* Source: https://github.com/goatfungus/NMSSaveEditor (issues 980, 1080, 1110).

**A7 - Requests and complaints about NMS tools.** [VERIFIED: GitHub issues]

| Issue | Quote | Design signal |
|---|---|---|
| goatfungus #138 (2018) | "Multiple save files and multiple backups can become a bit confusing ... the file's time stamp can mislead you"; asks for Total Play Time "so we can tell which save was played the most regardless of the modified time stamp" | play time is the user's progress proxy |
| goatfungus #1002 (2024) | "There is a backup available that cannot be restored due to the lack of an appropriate slot in the game." | slot-bound restore fails users |
| NomNom #262 (2025) | "It would be perfect if you could add the ability to create a new slot from a backup." | restore anywhere |
| NomNom #222 (2024) | "Display save names in the manager ... recognized by more than the game mode, play time, and date of last save." | names first |
| NomNom #57 (2021) | backups let players "experiment in-game and not having to worry about automatically-created saves overwriting all available save slots, making reversion to the previous state impossible"; maintainer: restoring returns it "to its original slot" | trying things means branching |
| NomNom #205 (2024) | "I have explored 85 galaxies and have over a hundred bases" - wants sort and search by name, galaxy and creation date, plus CSV export | bases by galaxy is a real need |
| goatfungus #508 (2022) | a save copied into another slot with Save As vanished in game (Steam Cloud suspected) | cloud interplay (see R8) |

**A8 - shirha "NMS Saves Manager"**, created 2025-11-10. [VERIFIED: README, `app.py`, screenshots]

* A local Flask web app. Two tables sit side by side: **Pool** (the vault; the newest pool entry per
  slot) and **Saves** (live slots 1-15). Each has Slot, Modified, Runtime and Saves Name columns.
* Buttons: Pool, Saves, History, Pool to Saves, Backup Saves, Note, Delete.
* Each pool entry has a `note.txt`. History lists one slot's pool entries, newest first.
* Pool folders are named `slot_mtime_runtime_savename`. Dedupe is by mtime: "96 games with unique
  mtimes", 248 MB against 10.7 GB of robocopy backups.
* From the README:
  * "NMS currently only allows 15 game saves slots. There are more Expeditions than that plus
    Reduxs."
  * Advises renaming every new save uniquely in game and using "slots 14 and 15 as your scratch pad".
  * Wants "a search by Save Name that lists all the backups you have by that Save Name".
* Real names in its screenshots: `S01-Relics-#18`, `S02-Main`, `S03-Aquarius-Redux-#15`,
  `S04-Corvette-#19`, `S05-Cursed-Redux-#16`, `S06-Hunt-for-Atlantid`, `S07-Main-2`,
  `S08-Liquidators-Redux-#14`, `S09-Titan-#17`, `S11-Hunt-2`, `Breach-#20`, `Unnamed-Save`.
* Source: https://github.com/shirha/nms_saves_manager

**A9 - Other NMS tools.** [VERIFIED: READMEs, unless marked]

* **NMSCoordinates.** Zips the whole save folder on startup and has a Save Manager for those zips.
  Lists base and space-station locations across all saves, with glyphs and portal codes [SOURCED].
  https://github.com/Kevin0M16/NMSCoordinates
* **Atlas Archive.** A folder watcher with debounce. Zips on change, keeps a configurable "Maximum
  number of backups", and has separate triggers for Autosaves, Restore Points and Other changes.
  https://github.com/goremachine/nms-savegame-autobackup
* **nms-save-data-util** (nefilmjp). Backup and restore, plus "lock/unlock", which sets the read-only
  flag "for ignoring auto-save". https://github.com/nefilmjp/nms-save-data-util
* **nmssavetool.** A CLI with `backup`, `backupall` and `restore`.
  https://github.com/matthew-humphrey/nmssavetool
* **NMSA (2026).** "Shows completion health and an exact change preview before writing". It also
  makes a timestamped backup, writes transactionally, verifies by hash and rolls back on failure.
  https://github.com/roysdepo/NMSA
* **nms-tessera.** Pipeline: save, then extract, then facts, then beats, then a "story seed" - a save
  summarised as a narrative. https://github.com/hadsh/nms-tessera
* **NMS-DC.** A standard-library tkinter discovery counter that is cross-save aware via
  `UsedDiscoveryOwnersV2`. https://github.com/Knixee/NMS-DC
* **DNA** (Nexus mod 2953). Catalogues ships, multitools, freighters, frigates and pets across saves
  [SOURCED (snippet)].
* **Caution.** The same GitHub searches returned 2026 repositories with template-like descriptions
  (e.g. "{title} is a feature-rich third-party modification project ..."), a common pattern for
  malware lures. They were not opened. Design agents should not download them.

**A10 - The gap.** [INFERRED from A1-A9]

* Every NMS tool keys history to a slot position or to the file mtime.
* None groups versions by the game's own save identity.
* None carries notes across versions; shirha's notes belong to individual backups.
* None draws a history tree.
* None treats live slots as a scarce resource with eviction.
* NMS Save Vault already has the identity (README v0.2.7, the "Save file" filter). An organizer built
  on it would be a first in this niche.

### 2b. General save managers

**B1 - LaunchBox 13.27 save management**, 2026-05-13. [SOURCED]
Sources: [help article](https://feedback.launchbox-app.com/help/articles/7589345-managing-save-games-and-states),
[changelog](https://feedback.launchbox-app.com/changelog/launchbox-for-windows-1327),
[migration guide](https://feedback.launchbox-app.com/help/articles/5615549-1327-save-management-migration-guide).

* "Here LaunchBox organizes saves into save groups." A group is one save file (or save-state slot)
  together with its whole history.
* "In simple terms, Active is the save you are currently playing on, and Vault is a backup copy
  stored by LaunchBox."
* "If you see the No Backup symbol, it means the current active save does not yet have a matching
  backup in the Vault."
* "Backup History opens the full version history for that save group." "This view shows the current
  active save and the older Vault versions for that save."
* "Set As Active makes the selected Vault save the live save for that game." Per the migration guide,
  restoring makes the version active and moves the previous active save into the same history.
* "Edit Name lets you change two different things":
  * The group name, "the main name for the whole save history" (e.g. "Main Playthrough").
  * "the label for one specific save version" (e.g. "Before Final Boss").
* "Combine With Another Save merges two save groups into one shared save history." A wrongly grouped
  version is split off with "Make New Save".
* "Labels let you add context to a save beyond just the date and time." "backup limits help keep
  your backup history useful without letting it grow forever." There is also a backup-on-close
  option.
* The migration grouped old backups "using things like group IDs, filenames, original filenames,
  hashes, and save metadata". It warned that an unrelated old save may land in the current history,
  so users should review the result.
* **Mapping to the owner's ask** [INFERRED]: save group = save-file line; Active = live;
  Vault version = backup; Set As Active = move to live; group name and version label = save-file
  note and version note; Combine / Make New Save = manual identity correction.
* **What it lacks:** no branching view, one Active per group, no eviction (emulator slots are not
  capped like NMS slots).

**B2 - Ludusavi** (6.4k stars). [VERIFIED: source and docs]

* The restore screen has one row per game. Each row shows:
  * Badges: CUSTOM, ALIAS, FAILED, and an OS badge when the selected backup came from another OS.
  * A comment icon whose tooltip shows the comment, and a lock icon.
  * A backup picker dropdown labelled by timestamp (`YYYY-MM-DDTHH:MM:SS`).
  * A three-dot action menu and the size.
* Shift-click previews and Ctrl-click acts without the menu. The comment editor expands below the
  row.
* Timeline of the relevant features:
  * v0.11.0 (2022-08-20): multiple full and differential backups per game.
  * v0.16.0 (2023-03-18): "In restore mode, you can create a comment on each backup. You can use this
    to keep track of how each backup reflects your game progress."
  * v0.18.0 (2023-05-20): "you can lock a backup so that it is kept indefinitely regardless of your
    retention settings".
  * Later: multi-paragraph comments, undo/redo in the comment field, and a CLI `backups edit`
    command.
* Comment and lock are fields of each backup in the per-game `mapping.yaml` inside the backup folder
  (`src/scan/layout.rs`), so notes travel with the backups.
* Retention keeps N full plus M differential backups; "Locked backups do not count toward the
  retention limits".
* [INFERRED] A dropdown suits "pick a restore point" but cannot show history.
* Source: https://github.com/mtkennerly/ludusavi (`CHANGELOG.md`, `src/gui/game_list.rs`,
  `docs/help/backup-retention.md`).

**B3 - Elden Ring Save Manager, backup manager.** [SOURCED]

* The list shows filename, timestamp, size, description and operation type (fix, edit, manual),
  newest first.
* Details show "a character list with levels", the path and a validity status.
* Restore first backs up the current save (suffix `before_restore`). The confirmation lists "the
  characters that will be replaced", and the result is validated afterwards.
* Starred (locked) backups survive a bulk delete.
* Of all the tools found, this is the closest to "show what is inside before you swap".
* Source: https://elden-ring-save-manager.readthedocs.io/en/main/user-guide/backup-manager/

**B4 - Console homebrew managers.** [VERIFIED: READMEs]

* **Checkpoint** (3DS and Switch): backup folders per title, favourites and filters, and "File-by-file
  verification after restore".
* **JKSV** (Switch): "can require holding to complete destructive operations or be set to make quick
  automatic backups upon restoring backups"; "Just hold A for three seconds" to delete.
* Sources: https://github.com/BernardoGiordano/Checkpoint , https://github.com/J-D-K/JKSV

**B5 - Steam per-game Notes** (client update of 2023-06-14). [SOURCED]

* Multiple rich-text notes per game, synced across devices and pinnable in the overlay.
* Per-item notes are now a mainstream gamer expectation.
* Sources: https://www.pcgamesn.com/steam/update-summer ,
  https://press-start.com.au/news/pcmac/2023/06/15/steams-newest-update-lets-you-write-notes-and-pin-them-to-your-game-screen/

**B6 - Weaker examples.** [SOURCED (snippet)]

* GameSave Manager backs up to a single `.gsba` archive; no per-version presentation was found.
* SaveState (2025) restores from any available backup and keeps N backups per profile.
* Source: https://www.gamingonlinux.com/2025/06/savestate-is-an-open-source-versatile-game-save-backup-manager-now-with-linux-support/page=1/

### 2c. Branching-history visualisation

**C1 - Vim undotree.** [VERIFIED: README, plugin source, screenshot]

* The README describes the owner's exact case: "make some change A, followed by change B, then go
  back to A and make another change C, normally you wouldn't be able to go back to change B because
  the undo history is linear."
* Presentation:
  * A vertical list, newest at the top, with ASCII lanes (`|`, `/`, `\`) like `git log --graph`.
  * `*` marks each node, with a sequence number and a relative time ("4 seconds ago";
    `g:undotree_RelativeTimestamp=1` by default).
  * Markers: `>n<` is the current state, `{n}` the redo target, `[n]` the newest, and `s` / `S` saved
    and last saved.
  * A diff panel under the tree opens automatically (`g:undotree_DiffAutoOpen=1`).
* Proof that a branching history reads fine in plain ASCII.
* Source: https://github.com/mbbill/undotree

**C2 - Emacs undo-tree.** [SOURCED]

* Standard undo "can lose past states as soon as you redo", because it treats history "as a linear
  sequence".
* The visualizer has toggles: `t` for timestamps, `d` for the diff, `s` for selection mode. The
  current node is drawn as `x`.
* Source: https://elpa.gnu.org/packages/undo-tree.html

**C3 - Git GUIs.** [SOURCED]

* VS Code Git Graph:
  * Click a commit for its details and file diffs.
  * "compare any two commits by selecting one, then CTRL/CMD-clicking another".
  * Filter by branch.
  * Show or hide the Date, Author and Commit columns.
  * An "Uncommitted Changes" row appears on the graph.
* GitKraken puts a **WIP** node at the top of the graph for pending changes. 12.3 shows one WIP row
  per worktree (snippet).
* Analogies [INFERRED]: a live slot whose current state is not in the vault yet is the WIP node; the
  same save-file live in two slots is two worktrees.
* Sources: https://marketplace.visualstudio.com/items?itemName=mhutchie.git-graph ,
  https://help.gitkraken.com/start-here/interface

**C4 - Photoshop History panel.** [SOURCED]

* By default, "if you go back in time and then execute another command, you loose [sic] the history
  states" after that point.
* "Allow Non-Linear History", off by default, keeps them. Snapshots stay pinned at the top.
* That default discards the future. The organizer must never behave like that.
* Source: Julieanne Kost, 2021-02-02, https://jkost.com/blog/?p=18541 (Adobe's help page was 403).

**C5 - Figma version history.** [SOURCED]

* An autosave checkpoint every 30 minutes; autosaves are grouped.
* Named versions carry a Title (clipped beyond 25 characters) and a Description (under 140).
* Restore is "a non-destructive action". It adds one checkpoint for the current state and one for the
  restored version.
* A version can be duplicated into a new file.
* Restore is logged as events, so the history stays linear.
* Source: https://help.figma.com/hc/en-us/articles/360038006754-View-a-file-s-version-history

**C6 - Google Docs version history.** [SOURCED]

* "Name this version" (up to 40 named versions per document) and an "Only show named versions"
  toggle.
* Grouped versions sit behind "Expand".
* "Restore this version" and "Make a copy".
* Source: https://support.google.com/docs/answer/190843

**C7 - Time Machine.** [SOURCED]

* A timeline along the right edge: "The currently selected backup is highlighted in red in the
  timeline". Arrows step between backups.
* Restored items return to their original location.
* Source: https://support.apple.com/guide/mac-help/restore-files-mh11422/mac

**C8 - Load menus and branching stories in games.** [SOURCED]

* **Baldur's Gate 3** groups saves under each character (playthrough), newest first. Players are
  advised to give characters distinct names because saves are keyed by name (Steam forum, snippet:
  https://steamcommunity.com/app/1086940/discussions/0/4342103279859683110).
* **Starfield's** Load screen has a "Select Character" filter and a "Show all Saves" tab
  (https://game8.co/games/Starfield/archives/423796,
  https://www.siliconera.com/where-are-saves-stored-in-starfield/).
* **Oblivion mod "Character Specific Saves"**: a character list in the load menu, each character once
  with its total number of saves (Nexus 56352, snippet).
* **Detroit: Become Human**: per-chapter flowcharts of the paths taken. Replaying from a checkpoint and
  saving overwrites the later progress (https://twinfinite.net/?p=353752, snippet).
* **Zero Escape: Virtue's Last Reward**: a flowchart lets the player return to any completed choice
  point and take the other branch
  (https://en.wikipedia.org/wiki/Zero_Escape:_Virtue%27s_Last_Reward).
* How-To Geek, 2025-06-11: "Flowcharts show exactly where a story can diverge, how certain choices
  affect your playthrough, and which scenes you missed."
  (https://tech.yahoo.com/gaming/articles/why-best-visual-novel-feature-160015782.html).
* [INFERRED] Gamers already know two patterns: "group by playthrough, newest first" and "a flowchart
  of branches".

**C9 - Concept design.** [SOURCED]

* Perez De Rosso and Jackson, "Purposes, Concepts, Misfits, and a Redesign of Git" (OOPSLA 2016),
  trace git's difficulty to misfits between concepts and purposes and propose the Gitless redesign.
* Their rule: map concepts to purposes one to one, and keep concepts orthogonal.
* Sources: https://dspace.mit.edu/handle/1721.1/106271 ,
  https://blog.acolyer.org/2016/10/25/purposes-concepts-misfits-and-a-redesign-of-git/

**C10 - What makes a history tree readable for a gamer.** [INFERRED, from C1-C9 and B1]

| Rule | Evidence |
|---|---|
| Newest at the top; time flows one way | undotree, git graphs, BG3, Outlook |
| One lane per branch; a fork is drawn where play resumed from an older version | undotree, Git Graph |
| Label nodes with facts the player recognises: name, play time, date, note, place | NomNom columns, A7 #138, shirha |
| Mark "you are here" and "live" distinctly; show a not-yet-saved live state as its own node | undotree `>n<`, GitKraken WIP, LaunchBox Active / No Backup |
| Collapse runs with nothing notable; show branch points, notes, live and tips by default | Google "Only show named versions", Figma grouping |
| Make a restore visible, never destructive | Figma restore checkpoints; Photoshop default as the anti-pattern |
| Per-node "what changed", and compare any two | undotree diff panel, Emacs `d`, Git Graph Ctrl-click |
| Give fixed things fixed names: save-file, version, live slot | Gitless |

### 2d. Limited-slot analogs: "move to live, choose which live slot to evict"

**D1 - Pokemon party and Boxes.** [SOURCED (snippet)]

* The party of 6 is the live set; the Boxes are the vault.
* With a full party, a newly caught Pokemon "can go to Boxes or swap with one of your current party
  members". The Boxes menu allows swaps at any time.
* Source: https://gamerguides.com/pokemon-scarlet-and-violet/guide/how-to-play/getting-started/how-to-catch-pokemon

**D2 - Pokemon's four-move cap.** [SOURCED (snippet)]

* A Pokemon "must forget one old move for every new move".
* A Move Reminder relearns forgotten moves. In Scarlet and Violet, moves learned by level-up or TM can
  be relearned freely. Making eviction reversible lowers the stakes.
* Source: Bulbapedia "Move" (https://bulbapedia.bulbagarden.net/wiki/Move).

**D3 - Xbox "Pin to Quick Resume"** (March 2022 update). [SOURCED]

* At most two pins. Pinned games drop out only if removed manually or by a mandatory update.
* "If you already have two games pinned, you'll be asked which pin you want to replace."
* Source: https://www.thesixthaxis.com/2022/03/09/xbox-march-2022-update-adds-pin-to-quick-resume-feature-share-button-remapping/

**D4 - The evicted item stays visible.** [SOURCED]

* Switch "Archive Software" keeps the save data and the HOME menu icon; selecting the icon
  redownloads the game (https://en-americas-support.nintendo.com/app/answers/detail/a_id/22333).
* iOS "Offload App" keeps documents and data. A cloud icon marks apps that are not fully installed,
  and a tap reinstalls (https://www.engadget.com/2017-06-07-ios-11-storage-offload-apps.html).
* LaunchBox moves the previous Active save into the history (B1).

**D5 - Anti-pattern: Breath of the Wild with a full weapon inventory.** [SOURCED]

* "it tells you you're out of room, and nothing more".
* The proposed fix: "a context-sensitive 'Swap' option that only appears when the user tries to pick
  up a weapon or open a chest when the weapon inventory is full".
* Source: Edward Moore, Kotaku, 2017-04-03,
  https://kotaku.com/five-ways-nintendo-could-fix-breath-of-the-wilds-clunky-1793957796

**D6 - NMS precedents.** [VERIFIED]

* NomNom has a per-row Swap and a two-pane Transfer wizard (A1, A5).
* NMS Save Vault's copy window already names "both ends", lists "every live slot with what is
  currently in it", and lets either end be changed (README, "What each row can do").
* The eviction picker can grow out of this window rather than start from nothing.

**D7 - Confirm versus undo.** [SOURCED]

* NN/g (Jakob Nielsen, 2018-02-18, https://www.nngroup.com/articles/confirmation-dialog/):
  * "Use a confirmation dialog before committing to actions with serious consequences."
  * "Do not use confirmation dialogs for routine actions."
  * "Be specific and inform users about the consequence of their action."
  * Response options should "summarize what will happen", e.g. "Delete file" / "Keep file" instead of
    Yes / No.
  * "do try your best to offer undo".
* Aza Raskin, "Never Use a Warning When you Mean Undo" (A List Apart, 2007-07-21): habituation makes
  people click warnings away (https://alistapart.com/article/neveruseawarning/).

**D8 - Safety rituals in save managers.** [VERIFIED/SOURCED]

* Back up the current state before a restore: Elden Ring SM `before_restore`, JKSV. NMS Save Vault
  already does this before every write (README).
* List what will be replaced (Elden Ring SM).
* Verify after a restore (Checkpoint).
* Hold to confirm a delete (JKSV).

### 2e. Notes and annotation

**E1 - Two levels of naming and notes.**

| Tool | Per item (whole history) | Per version | Stored |
|---|---|---|---|
| LaunchBox [SOURCED] | group name ("Main Playthrough") | label ("Before Final Boss") | LaunchBox data |
| Ludusavi [VERIFIED] | none | comment + lock | `mapping.yaml` beside the backups |
| Figma / Google Docs [SOURCED] | the file name | title + description / version name | the service |
| shirha [VERIFIED] | the in-game save name | `note.txt` | inside the pool entry folder |
| Steam Notes [SOURCED] | multiple notes per game | none | Steam cloud |

**E2 - Where notes live.** [VERIFIED/INFERRED]

* Ludusavi and shirha store the note beside the backup data, so copying or importing a backup carries
  the note.
* NomNom and goatfungus have no notes.
* The owner wants the note kept "with each backup". Prior art points to storage that travels with the
  version, or to an id that survives Import (README v0.0.5 imports whole vault folders).

**E3 - Carrying notes forward.** [INFERRED, from absence]

* No tool found copies a version note onto later versions automatically.
* Item-level names persist by construction (LaunchBox group name, the in-game save name). Version
  notes stay attached to the version they describe, like git commit messages.
* Carrying forward is therefore an open design choice (Q2).

**E4 - Lightweight markers.** [SOURCED/VERIFIED]

* Locks: Ludusavi. Stars working as locks: Elden Ring SM. Pins: Xbox Quick Resume.
* Terraria favourites are "listed at the front" and "cannot be deleted"
  (https://terraria.wiki.gg/wiki/Characters).
* Finder tags combine colours and names, allow several per item, and filter from the sidebar
  (https://support.apple.com/guide/mac-help/tag-files-and-folders-mchlp15236/mac).
* Lightroom has flags, stars and colour labels
  (https://jkost.com/blog/2024/06/applying-flags-stars-and-color-labels-in-lightroom-classic.html).
* Terraria also shows progress visually: a "Played" icon, and a golden border on post-Moon Lord
  worlds.

**E5 - Auto-generated text.**

* GitHub's automatically generated release notes list merged pull requests, contributors and a
  full-changelog link, grouped into categories by label
  (https://docs.github.com/en/repositories/releasing-projects-on-github/automatically-generated-release-notes)
  [SOURCED].
* NomNom shows a save's expedition provenance [VERIFIED].
* Elden Ring SM's details list characters with levels [SOURCED].
* NMSA shows an "exact change preview" [VERIFIED].
* nms-tessera turns a save into a story [VERIFIED].

**E6 - Annotation behaviour.** [SOURCED/INFERRED]

* Variolite study (CHI 2017): 70 data scientists managed versions informally ("commenting-out code or
  creating many files with similar names"); about 3 in 10 used version control
  (https://hcii.cmu.edu/news/chi-preview-variolite-supporting-exploratory-programming-data-scientists).
* Google's "Only show named versions" and Figma's grouped autosaves exist because most versions go
  unnamed.
* Do not depend on users writing notes. Generate one, and let the user overwrite it.

### 2f. Large sortable grouped tables

**F1 - Outlook conversation view.** [SOURCED]

* Conversations are "identified in the message list by a triangular expand/collapse icon".
* The unread count appears next to the subject.
* The "newest message on top" within a conversation.
* "Show Messages from Other Folders" pulls in copies stored elsewhere.
* Direct analog: one line per save-file, expanding to its versions from any backup or slot.
* Source: https://support.microsoft.com/en-us/office/view-email-messages-by-conversation-0eeec76c-f59b-4834-98e6-05cfdfa9fb07

**F2 - NN/g "Data Tables: Four Major User Tasks"** (Page Laubheimer, 2022-04-03). [SOURCED]

* The four tasks: find records, compare, view or edit a row, take action.
* Filters must be "discoverable, quick, and powerful", with a visible indicator for active filters.
* Hiding and reordering columns should take little effort and should not rely on drag and drop
  alone.
* Freeze header rows and columns.
* Batch actions via row checkboxes; inline per-row actions get crowded.
* Source: https://www.nngroup.com/articles/data-tables/

**F3 - Filter chips.** [VERIFIED]

* "Filter chips use tags or descriptive words to filter content. They can be a good alternative to
  toggle buttons or checkboxes."
* Source: https://github.com/material-components/material-web/blob/main/docs/components/chip.md

**F4 - Status badges in rows.** [VERIFIED/SOURCED]

* Ludusavi: uppercase text badges plus comment and lock icons (B2).
* LaunchBox: Active, Vault and No Backup (B1).
* Terraria: cloud and favourite icons per row (E4).

**F5 - Column sets in existing tools.** [VERIFIED/SOURCED]

* NomNom's slot grid: `#`, mode, play time, last save, then actions.
* Git Graph shows or hides its Date, Author and Commit columns.
* shirha: Slot, Modified, Runtime, Name.

**F6 - Tk feasibility.** [VERIFIED: Python docs and README]

* `ttk.Treeview` "displays a hierarchical collection of items".
* Each item has an `open` flag (show or hide children), tags controlling foreground, background and
  font, an image "displayed to the left of the label", and a heading `command` callback. Sorting is
  implemented inside that callback.
* App constraint: all UI text is ASCII, because the bundled Linux Tk lacks Xft (README). Markers must
  be ASCII text or images.
* Source: https://docs.python.org/3/library/tkinter.ttk.html

### 2g. The NMS community

**G1 - Official patch notes.** [VERIFIED: primary sources]

* Frontiers 3.6, 2021-09-01 (https://www.nomanssky.com/frontiers-update/):
  * "The number of possible save slots has been increased from five to fifteen."
  * "The save select screen now supports scrolling to see additional slots/start a new game."
  * "All existing saves have been automatically backed up."
* Waypoint 4.0, 2022-10-07 (https://www.nomanssky.com/waypoint-update/):
  * "Players may now rename their saves. The option is found in the main menu once the save has been
    loaded."
  * "The most recent player location is now displayed in the save summary."
  * Autosave is now silent during play.

**G2 - Coping with 15 slots.** [SOURCED]

* "Is there a way to get more saves?" (2023-07-18,
  https://steamcommunity.com/app/275850/discussions/0/3812910660672128777/):
  * One player asked "for more slots or preferably a better management system (like importing or
    exporting)".
  * Replies: archive manually by date and time; turn off cloud saves to control backups.
  * One complaint: managing "generically named files" in "7 layers of folders".
* "How does the new Adrift expedition work?" (2024-05-30,
  https://steamcommunity.com/app/275850/discussions/0/4339860450389022525/):
  * "You can only have a maximum of 15 saves" - with 15 used, there is no option to start the
    expedition as a new save.
  * "i had to delete one of my lessor played saves to get my dedicated save for Expedition 13:
    Adrift".

**G3 - How players name saves.** [VERIFIED]

* shirha's names (A8).
* A goatfungus #1110 commenter's slot label: `Slot 1 - NORMAL - Expedition 16 - The Cursed`.
* Pattern: slot, plus purpose or expedition, plus a number. "Unnamed-Save" when never renamed.
* The in-game limit is 42 characters (A4).

**G4 - What players compare and track.** [VERIFIED/SOURCED]

* Play time over modification time (A7 #138); names over mode, play time and date (#222).
* Expedition provenance (A2); bases by galaxy (#205).
* A catalogue of ships and pets across saves (DNA); discovery counts (NMS-DC).
* Legacy base parts kept in old saves:
  * A "Legacy Structures" build tab exists only on older saves (Steam, 2022,
    https://steamcommunity.com/app/275850/discussions/0/3183488724676494174).
  * The old Metal, Concrete and Wood sets can no longer be learned (Steam, 2021,
    https://steamcommunity.com/app/275850/discussions/0/3044985412485835320).

**G5 - Save lifecycle facts that shape the list.** [SOURCED, community-level]

* Expedition saves convert to normal saves when the expedition ends, and players keep playing them,
  so they accumulate
  (https://powerupgaming.co.uk/2021/06/08/you-should-continue-your-expedition-save-once-the-event-is-over-in-no-mans-sky/).
* Cross-save means one run can exist on two platforms. libNOM.io has an
  `IsVersion525TheCursedWithCrossSave` flag [VERIFIED], and README v0.2.6 already folds such copies
  into one run.

**G6 - Evidence gaps.**

* Reddit could not be read.
* No Hello Games statement on future slot counts was found.

---

## 3. Implications for the design

### 3.1 Constraints

* **K1 - Group by identity, with manual repair.** The game's save id groups a line. libNOM's slot
  keying shows what breaks without it. Grouping will sometimes be wrong (LaunchBox's migration
  warning; saves from before Waypoint have no id), so offer **Combine** and **Split**, and say why two
  versions were grouped.
* **K2 - "Live" is where a version is, not a separate list.**
  * Put live badges on the line: platform and slot, e.g. `S3` and `X7`, more than one if needed.
  * Keep a fixed live board too: 15 slots per live source, and there can be several sources (two
    Steam accounts plus Xbox, per README "Auto-configuration").
* **K3 - Never discard.** Putting an older version live starts a branch. The current live state is
  captured first (the app already auto-snapshots) and appears as a version labelled e.g. "saved
  before swap". Contrast C4 (Photoshop) with C5 (Figma).
* **K4 - ASCII only.** Draw the tree as ASCII lanes (C1) or as Canvas lines. Badges use colour tags
  with a text fallback.
* **K5 - No comboboxes for choosing versions.** goatfungus #1110 and #1080 show the cost. A dropdown
  is acceptable only for "pick a restore point" (Ludusavi).
* **K6 - Confirm only the swap.** Name both outcomes on the button, and offer undo afterwards through
  the existing oplog (D7).
* **K7 - Notes travel with versions.** The owner asked for the note to stay "with each backup", and a
  copied or imported vault must keep it (E2).
* **K8 - Two-tier data.** Meta columns appear immediately: name, play time, difficulty, last write,
  summary/location. Facts from the content index fill in as they become available (A4).

### 3.2 Risks

* **R1 - The tree gets heavy.** The parent's notes record 26 distinct slot names and 49 catalog
  entries, and the vault grows from there. Default to a compact history (branch points, noted, live,
  tips) and keep "show all" one click away (C6, C10).
* **R2 - Mis-grouping.** Old saves without an id, renames and cross-save copies all add risk; see
  LaunchBox's "childhood save" warning (B1). Mitigation: K1 plus an explanation of each grouping.
* **R3 - Habituated confirmations** (D7). Mitigation: keep them rare and specific, and back them with
  undo.
* **R4 - Steam Cloud brings cleared slots back** (README 0.2.5). An eviction that empties a slot must
  carry the existing warning flow; R8 owns the details.
* **R5 - Too many near-identical versions.** Every slot holds two saves, autosaves come every few
  minutes, and a snapshot precedes every operation. Mitigation: dedupe by hash and group the runs
  (C5).
* **R6 - Notes desync** if they live only in a central catalog while vault folders are copied or
  imported (K7).
* **R7 - Vocabulary drift.** The UI says "LIVE SAVES" / "BACKUPS" and the catalog has four kinds;
  LaunchBox says Active / Vault. Pick one set of words and use it everywhere (C9).

### 3.3 Opportunities

* **O1 - First in the niche** (A10).
* **O2 - A "No vault copy" badge on live slots** (LaunchBox's No Backup), extending the existing
  clear-slot play-time check.
* **O3 - An automatic "what changed" note** from the diff of the index between a version and its
  parent (E5, E6). It covers the owner's "see what they changed" without requiring typing.
* **O4 - Progress badges on rows** (Terraria's golden border), e.g. "Atlas Path complete" or "has
  legacy parts".
* **O5 - Play time as the history axis.** The README states that play time only goes up within a run.
  A version dated later but with less play time than an earlier one therefore signals a branch (R7
  to validate). A lane chart labelled with play time makes branches obvious. [INFERRED]
* **O6 - Pins for live slots** keep them out of eviction suggestions (Xbox). **Locks for versions**
  keep them out of pruning (Ludusavi).
* **O7 - Compare any two versions** with index facts (Git Graph, undotree).
* **O8 - Reuse the existing two-ended copy window** as the eviction picker (D6).

---

## 4. Open questions and candidate options

The parent forks designs on these questions. A "lean" is R4's INFERRED preference, not a decision.

**Q1 - How is history shown?**

* **A - Linear timeline per save-file.** A restore appears as an event row, "played from <version>"
  (Figma, Docs, LaunchBox). Simple and table-friendly, but branches stay implicit and two lines of
  play interleave.
* **B - Lane graph.** Newest on top, one lane per branch, drawn in ASCII (undotree) or on a Canvas.
  It shows the tree the owner asked for, at the cost of a learning curve and width.
* **C - Hybrid.** Version rows in a table with a narrow lane column on the left (Git Graph), plus a
  "key versions / all" toggle. Treeview cannot draw lines, so this needs a Canvas strip or an ASCII
  column next to it.
* **D - Milestone flowchart** (Detroit). Nodes are milestones reached. Needs the S2 index; a later
  phase.
* Lean: C, with A as the fallback if lanes prove too costly in Tk.

**Q2 - What is the notes model?**

* **A - One note per save-file only.** Simplest, but the owner's "see what changed per backup" is
  lost.
* **B - Notes per version only.** Each new version optionally starts with a copy of its parent's
  note. Detailed, but notes go stale and get duplicated.
* **C - Two levels plus an automatic change list** (LaunchBox + Ludusavi + GitHub). Richest; needs
  S2 for the automatic part.
* Lean: C.
* Sub-question: where notes are stored - the central catalog, a sidecar file per version, or both
  (K7).

**Q3 - What is the organizer layout?**

* **A - One grouped table** with live badges (Outlook).
* **B - Live board and vault table side by side** (shirha, NomNom Transfer, today's two panes).
* **C - Three panes:** live board, organizer table, and a detail pane with history and notes.
* Lean: C on a desktop-width window. B is the smallest step from today's app.

**Q4 - How does "move to live" work?**

* **A - Line first.** Select a save-file or version, choose "Move to live...", then pick the slot to
  evict (Pokemon, Xbox pins).
* **B - Slot first.** Select a live slot, choose "Replace with...", then pick the version (today's
  copy window).
* **C - Drag a line onto a slot,** with a keyboard alternative (NN/g: not drag-and-drop only).
* Lean: A and B as one dialog in which either end can change, as the README's copy window already
  allows; C is optional.
* The picker's default suggestion: an empty slot, else a slot that is unpinned, already has a vault
  copy, and was played least recently. Always show "has a vault copy: yes/no".

**Q5 - What counts as a version?** (S1 coupling)

* **A - Every distinct file state found anywhere,** deduped by hash and including automatic
  snapshots.
* **B - Explicit backups only;** snapshots hidden.
* **C - The display unit:** the slot pair (Auto-Save plus Restore-Point) as one version, or each
  member separately. NomNom shows both members as rows; the app marks the loaded member with `*`.
* Lean: A, grouped as Figma does, displayed at pair level with the loaded member's facts.

**Q6 - How do copies on several platforms or accounts appear?**

* **A - One line with several live badges** (e.g. `S3 X7`).
* **B - One line per platform.**
* Lean: A, consistent with README v0.2.6, which already treats them as one run.

**Q7 - Retention and pruning.**

* **A - Keep everything** (today).
* **B - Per-save-file limits,** with locked versions exempt (Ludusavi, LaunchBox; libNOM keeps 3).
* **C - Smart thinning:** keep branch points, tips, noted, locked and live versions; thin the rest.
* This is the owner's call, since it trades disk space against the risk of losing data.

**Q8 - Vocabulary.**

* **A - Live / Vault / Version / Save file,** matching the app's own name.
* **B - Active / Backup** (LaunchBox, NomNom).
* Lean: A.

**Q9 - Pins and locks.** Offer both, one, or neither (O6).

**Q10 - Depth of the automatic changelog.** A headline (top three changes) or a full fact diff; and
whether it is computed per version or per branch.

**Q11 - UI technology** (coordinate with R3). The prior art with rich history graphs runs on web,
Electron, WPF and iced. Tk can do the table and ASCII lanes; a Canvas lane chart is more work.

---

## 5. Do and don't

**Do**

* Group lines by the game's save identity, and offer Combine and Split (B1, K1).
* Show where each version lives with badges (`S3`, `X7`, Vault), and flag live slots with no vault
  copy (B1).
* Treat play time as the main progress figure and sort by it (A7 #138).
* Make "use an older version" non-destructive and visible: save first, then branch (C4, C5).
* Default to a compact history with a "show all" toggle (C6, C10).
* Offer the swap where the user runs out of room, with the outcome named on the button (D1, D3, D5,
  D7).
* Keep evicted saves visible with a status, one click from coming back (D4).
* Use two-level notes, an automatic change summary, locks and pins (E1-E6).
* Make filters chips with a visible active state; freeze the header; let columns be hidden (F2, F3).
* Provide a keyboard path for every drag interaction (F2).
* Keep every marker ASCII (F6).
* Show the facts inside a save before a swap: names, play time, key facts (B3).

**Don't**

* Bind history to slot positions or mtimes (A3, A8).
* Hide versions inside a combobox (A6).
* Discard the "future" when an older version is used (C4).
* Expose storage kinds (full, snapshot, extract, in-place) as primary concepts (C9).
* Show raw ids, file names or hashes as labels - users dislike "generically named files" (G2).
* Confirm routine actions, or use Yes/No buttons (D7).
* Prune versions that carry notes or locks, or that are branch tips or live, without the user's
  say-so (B2, Q7).
* Rely on users to annotate (E6).
* Put destructive actions as bare icons beside harmless ones in every row (A1 layout; F2 on crowded
  row actions). [INFERRED]
* Download the look-alike "NMS editor" repositories flagged in A9.

---

## 6. UX concepts worth mocking up

The sketches are ASCII on purpose: the app's UI is ASCII-only, and the design wave will draw real
mock-ups. All names, dates, play times and facts in them are illustrative, not the owner's data.

**M1 - Organizer: one grouped table.**
Prior art: Outlook conversation view, LaunchBox save groups, Ludusavi badges, NN/g tables.

```
Filters: [Live] [Steam] [Xbox] [Has note] [Expedition] [Locked]     Find: [__________]
  Save file            Live    Vers  Play     Last played  Difficulty  Note / latest change
> Main                 S2 X2    37   812:40   2026-10-05   Normal      "Atlas done"; +1 base (Euclid)
v ShipsToGive-1400QS   -        12   140:12   2026-09-12   Creative    "gave 3 ships away"
     2026-09-12  140:12  Vault  tip   "gave 3 ships away"
     2026-09-01  131:55  Vault        (auto) +2 ships, -1 frigate
     ... 10 more                                              [show all]
> Relics #18           S1        4     5:10   2025-10-26   Normal      -
Selected: ShipsToGive-1400QS   [Move to live...] [History] [Notes] [Compare]
```

Forks it exercises: Q3, Q5, Q6.

**M2 - Live board: 15 slots per live source.**
Prior art: the NMS slot list, NomNom's slot grid, the Pokemon party, Xbox pins, LaunchBox No Backup.

```
STEAM (Main)                                    XBOX
 1 Relics #18        5:10  2025-10-26  ok        1 (empty)
 2 Main            812:40  2026-10-05  NO COPY   2 Main  812:40 (same save as Steam 2)
 3 Aquarius Redux    4:37  2025-10-27  ok  pin   3 ...
```

It shows the unbacked live state (`NO COPY`), pins, and the same save live on both platforms.

**M3 - Move to live: the eviction picker.**
Prior art: Pokemon "swap with party member", Xbox "which pin to replace", the Kotaku "Swap" fix,
NN/g button labels, today's two-ended copy window.

```
Move to live:  ShipsToGive-1400QS  @ 2026-09-12, 140:12, "gave 3 ships away"
Into: (o) Steam (Main)  ( ) Xbox
 Slot  In it now        Play    Last played   Vault copy   Pin
  14   (empty)                                                     <- suggested (empty)
   7   Main-2           64:45   2025-10-26    yes           -     <- next: oldest unpinned
   2   Main            812:40   2026-10-05    NO            pin
Result: "Main-2" stays in the vault as version 2025-10-26 and can return any time.
[ Swap into Steam slot 7 ]  [ Cancel ]        after the swap: "Done. [Undo]"
```

Forks it exercises: Q4, Q9.

**M4 - History lanes for one save-file.**
Prior art: undotree, Git Graph, Google's "Only show named versions", Time Machine's red "selected"
marker.

```
History: Main     Show: (o) key versions ( ) all 37     Label: (o) date ( ) play time
*      2026-10-05  812:40  LIVE S2 X2  tip    "Atlas done"
*      2026-09-28  790:02  Vault              (auto) +1 base (Euclid)
| *    2026-09-20  702:15  Vault       tip B  "tried permadeath settings"
| *    2026-09-18  700:30  Vault              (auto) difficulty changed
|/
*      2026-09-17  699:58  Vault       lock   "before Atlas finale"
[Compare 2 selected] [Move to live] [Note] [Lock]
```

Forks it exercises: Q1, Q7; opportunity O5.

**M5 - Version card: notes and changes.**
Prior art: LaunchBox name plus label, Ludusavi comment and lock, Figma title plus description,
GitHub automatic notes, Elden Ring SM details.

```
Main  -  version 2026-09-17 (699:58)                         [Lock] [Move to live]
Save-file notes (all versions): "First save. Bases: Euclid x3. Keep legacy wood parts."
This version: label "before Atlas finale"   comment: _______________________
Changes since parent (auto): +2 glyphs (14/16); +1 ship; base parts 2410 -> 2566;
                             milestone reached: Atlas Path 9
Where: Vault (backup 2026-09-17); was live in Steam slot 2 until 2026-09-18
```

Forks it exercises: Q2, Q10; depends on S2 (R5/R6) for the automatic line.
