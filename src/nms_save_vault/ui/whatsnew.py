"""What's new in this release: shown once, the first time a version starts, on a new install
and after an update alike.

At each release set ``NOTES_VERSION`` to the new version and write ``NOTES`` for it (RELEASING.md
step 1); tests/test_whatsnew.py fails until they match ``__version__``. The notes are code, not a
data file, because the Linux and macOS kits pack only the ``.py`` files."""
from __future__ import annotations

#: Where state.json's ``ui`` preferences keep the last version whose notes were shown.
SEEN_KEY = "whats_new_seen"

NOTES_VERSION = "0.4.2"

NOTES = """\
Filter the backups
- Above the backups, "Type" lists only one type of backup (full, snapshot, ...) and "Pin" \
only the pinned or unpinned ones. Both combine with "Save file".

What's new
- This window: the first time a new version starts, it shows what changed. \
Help > What's new in this version shows it again.

Updates
- Installing an update no longer opens a console window. The version you are updating \
from runs the install, so you will see this from the next update on.
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
