"""What's new in this release: shown once, the first time a version starts, on a new install
and after an update alike.

At each release set ``NOTES_VERSION`` to the new version and write ``NOTES`` for it (RELEASING.md
step 1); tests/test_whatsnew.py fails until they match ``__version__``. The notes are code, not a
data file, because the Linux and macOS kits pack only the ``.py`` files."""
from __future__ import annotations

#: Where state.json's ``ui`` preferences keep the last version whose notes were shown.
SEEN_KEY = "whats_new_seen"

NOTES_VERSION = "0.5.0"

NOTES = """\
GOG
- The GOG edition of No Man's Sky is supported, live and in the vault. Its saves are found \
by themselves.
- GOG is counted on its own, never as Steam: its slots read G1 to G15 (in purple), its \
backups are named full-gog-..., and the Organizer has a GOG filter beside Steam and Xbox. \
"On both" is now "On several".
- A save live on Steam and on GOG shows each copy's progress, for example "GOG behind 2h".
- Moving a save between Steam and GOG asks first, as between two accounts. If GOG Galaxy \
asks at the next launch which copy to keep, keep the one on this PC.
"""


#: Help's entry that shows the notes again.
MENU_LABEL = "What's new in this version"

#: What Help shows when this version has no notes (a build between releases).
NO_NOTES = "There are no notes for this version."


def notes_for(version: str) -> str | None:
    """This release's notes, or None when none were written for ``version``."""
    if NOTES_VERSION != version or not NOTES.strip():
        return None
    return NOTES.strip()


def to_show(prefs: dict, version: str) -> str | None:
    """The notes to pop up now, or None: once per version, and only notes written for it."""
    if prefs.get(SEEN_KEY) == version:
        return None
    return notes_for(version)
