"""What's new in this release: shown once, the first time a version starts, on a new install
and after an update alike.

At each release set ``NOTES_VERSION`` to the new version and write ``NOTES`` for it (RELEASING.md
step 1); tests/test_whatsnew.py fails until they match ``__version__``. The notes are code, not a
data file, because the Linux and macOS kits pack only the ``.py`` files."""
from __future__ import annotations

#: Where state.json's ``ui`` preferences keep the last version whose notes were shown.
SEEN_KEY = "whats_new_seen"

NOTES_VERSION = "0.4.3"

NOTES = """\
It shows when it is working
- When something takes a moment, the pointer says so: the hourglass while the window \
waits, and the arrow with the hourglass while work goes on in the background and the \
window still answers. On a slow computer you no longer wonder whether a click was taken.

Safer writes
- A file the vault writes is tried again for a moment when Windows holds it open (an \
antivirus scan, the search indexer, Steam Cloud) instead of failing.
- A vault upgrade cut off right as it started no longer leaves a vault that will not open.
- A backup made while the vault is being thinned no longer fails as "damaged".
- Restoring the account data writes its two files together, or neither.
- A journal line cut off by a crash can no longer leave two records with the same number.
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
