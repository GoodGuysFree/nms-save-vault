# Changelog

What changed in each release of NMS Save Vault. Each bullet is a highlight, word for word what
the app shows the first time that version starts (Help > What's new in this version); the
indented lines under a bullet add detail. Releases before 0.5.0 are in the README's
[Version history](README.md#version-history).

## Unreleased

- Fixed: other apps lost the vault after a test build.
  A copy of the app run from a development folder no longer changes the file that tells
  other apps (such as an editor sharing the vault) where the vault's settings are. If that
  file names settings that were deleted, the installed app's settings are used, and with
  none the message says the settings file is missing and where it was expected, instead of
  asking to connect a drive.

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
