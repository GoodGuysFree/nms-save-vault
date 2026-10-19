# Owner review of the UI mockups

Mockups compared: the Tk (D6) and web (D7) mockups, side by side (mockups removed after the design was implemented; see the app).

## Decisions

- **F1 = Tk** (D6). Where the web version's layout is better, it is rebuilt in Tk.
- **Shell = 10a**: the organizer is a tab beside today's live/backups panes, switched by tabs
  under the Tk menu bar; the app remembers the last view the user was in and opens there.
- Web-only questions (browser tab vs app window) are moot.

## Per screen

| Screen | Owner's comment | Change to D6 |
|---|---|---|
| 01 Organizer | Prefers the web's `>` expand marker over the two boxes at the left of each Tk row. Prefers the Tk vertical live lists on the right over the web's horizontal slot tiles. | Replace the column-#0 platform boxes with a single `>` / `v` expand marker (platform stays visible in the Live column). Keep the vertical live lists. |
| 02 One save file expanded | Tk version is right. While one save file is expanded, de-emphasise the others. | Muted (duller) row style for every other line while a line is expanded. |
| 03 Move to live | The display of what will happen and the outcome text on the button are crucial; the Tk mockup gets them right. Add the web version's "Your pick" summary before the apply button. The dialog should look like the Replace dialog below, with colours and highlighting that emphasise the differences. | Keep the outcome panel and button text; add a "Your pick" summary; share one layout with 03b; colour-code what goes out vs what comes in. |
| 03b Replace from a live slot | Wants the web layout in Tk: the boxes showing what goes where, and the "What you are putting in" panel on the right. In general the dialogs should follow the web version. | Rebuild both move dialogs on the web layout in Tk. Rename the source filter chip "Not live" to "Vault only" (it means save files not in any live slot; the owner found "Not live 4" unclear). |
| 04c History | Prefers the web layout. | Rebuild the history view on the web layout, in Tk. |
| 05 Notes | Format is fine. | None. |
| 06 Compare | Good idea; the Tk version is fine. | None. |
| 07 First run | Fine. | None. |
| 08 Steam and Xbox copies | Tk is fine, but the live area must not show an empty Steam or Xbox column for someone with only one, and some users have several Steam accounts. | The live area gets two modes: show one live source (with a selector), or two side by side (the user picks which two). Works with any number of Steam accounts plus Xbox. |
| 09 After a move | Tk is fine. | None. |
| 10 Window shell | Likes the Tk menu with tabs that switch between the organizer and the classic backups. Chooses 10a; the app remembers the last mode. | As in Decisions. |

## Missing from the mockups

- **Warning when a slot is left empty**: Steam Cloud or Xbox sync may fill it again. Show it
  wherever a live slot is empty (the app never empties a slot itself, F8; the user may have
  done it by hand) and in the manual-procedure text.
