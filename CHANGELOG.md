# Changelog

What changed in each release of NMS Save Vault. Each bullet is a highlight, word for word what
the app shows the first time that version starts (Help > What's new in this version); the
indented lines under a bullet add detail. Releases before 0.5.0 are in the README's
[Version history](README.md#version-history).

## Unreleased

## 0.6.0 - 2026-11-28

- Fixed: other apps lost the vault after a test build.
  A copy of the app run from a development folder no longer changes the file that tells
  other apps (such as an editor sharing the vault) where the vault's settings are. If that
  file names settings that were deleted, the installed app's settings are used, and with
  none the message says the settings file is missing and where it was expected, instead of
  asking to connect a drive.
- Fixed: every restore step now survives a power cut.
  The account data a restore puts back (Steam and Xbox), and the repair of a damaged Xbox
  save index, are now recorded before they are written: if the power goes or the program
  is killed half-way, the next start finishes them or puts them back. Every file the app
  replaces is on the disk before it goes on (Windows writes it through; Linux and macOS sync
  the folder).
- An interrupted restore is always settled, and said.
  A restore stopped by a crash, End task or a power cut is finished or put back at the next
  start, its account data with it (Steam and Xbox), and the app says what it did. Before
  each release, every restore and backup is killed at every step it takes, and at random
  moments, by an automatic test that checks the saves come out whole.
- A cut-off copy into an old vault is removed and said.
  In a vault not yet upgraded to the version store, a backup cut off by a power cut left a
  half-written folder behind; it is now found at the next start, removed, and named.
- The waiting pointer shows in every window of the app.
  Dialogs and the History, Search and Notes windows show it while their work runs, and a
  few slow actions that showed nothing (sorting, Find, deeper detail, a save file's
  history, the update download) now show it too.

## 0.5.1 - 2026-11-26

- Xbox saves from the 7.15 update are supported.
  - The Xbox game (7.15.0) is not the Steam build, so the app waited for an Xbox save from it;
    it writes the same save version as Steam's 7.15.
- A new save folder is found at startup and offered.
  - A GOG install or another Steam account that the settings do not list yet is offered once.
    Adding it makes a backup of it at once; if you say no you are not asked again, and Live >
    Rescan live folders adds it any time.
- Fixed: the Organizer's Live buttons crept right.
  - Every click of One / Two, and every refresh, added another 10-pixel gap.

## 0.5.0 - 2026-11-26

- GOG is supported, live and in the vault.
  - The GOG edition keeps its saves in the DefaultUser folder beside the Steam ones, in the
    same format; the app finds it by itself.
- GOG is counted on its own, never as Steam.
  - Its slots read G1 to G15 in purple, its backups are named full-gog-..., and the Organizer
    has a GOG filter beside Steam and Xbox ("On both" is now "On several").
- A save on Steam and GOG shows each copy's progress.
  - For example "GOG behind 2h", as Steam and Xbox copies already did.
- Moving a save between Steam and GOG asks first.
  - As between two accounts. If GOG Galaxy asks at the next launch which copy to keep, keep the
    one on this PC.
- GOG's game version is checked on its own.
  - A save from a newer game than GOG's is not put into the GOG folder.
