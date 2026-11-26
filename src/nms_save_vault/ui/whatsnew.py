"""What's new in this release: shown once, the first time a version starts, on a new install
and after an update alike.

At each release set ``NOTES_VERSION`` to the new version and copy the version's highlights from
CHANGELOG.md into ``NOTES`` (RELEASING.md step 1); tests/test_whatsnew.py fails until they match
``__version__`` and the changelog. The notes are code, not a data file, because the app reads no
data files at run time and the Linux and macOS kits pack only the ``.py`` files."""
from __future__ import annotations

#: Where state.json's ``ui`` preferences keep the last version whose notes were shown.
SEEN_KEY = "whats_new_seen"

NOTES_VERSION = "0.5.1"

#: The highlights, one short line each: word for word the top-level bullets of this version in
#: CHANGELOG.md, which adds the detail (GGF 2026-11-26).
NOTES = """\
- Xbox saves from the 7.15 update are supported.
- A new save folder is found at startup and offered.
- Fixed: the Organizer's Live buttons crept right.
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
