"""The organizer's words, shared by the table, the detail pane and the notes window: names,
version numbers and how a version is linked (D6 2.1, C32: Save file, Version, Live, Vault).
Pure functions of the read model; every string is ASCII apart from the game's and the
owner's own text."""
from __future__ import annotations

from ...core.organizer.readmodel import Line, ReadModel, VersionRow

#: How a drawn parent was seen (D1 EdgeMethod); nothing else draws a link.
METHOD_WORDS = {
    "op": "put live by the app",
    "oplog": "put live by the app",
    "journal": "seen in the live slot",
    "capture-sequence": "seen in backups",
    "owner": "set by you",
}


def line_name(line: Line) -> str:
    """The save file's name; a nameless one says what it was called before, or its id."""
    if line.name:
        return line.name
    if line.former_names:
        return f"(unnamed, was {line.former_names[0]})"
    return f"(unnamed {line.pid[-4:]})" if line.identified else "(unnamed)"


def version_numbers(model: ReadModel, pid: str) -> dict[str, int]:
    """``#n`` of every version of a save file: the oldest is #1."""
    rows = model.versions(pid)
    return {row.head: len(rows) - i for i, row in enumerate(rows)}


def find_row(model: ReadModel, pid: str, head: str) -> VersionRow | None:
    return next((r for r in model.versions(pid) if r.head == head), None)


def platform_word(platform: str) -> str:
    return "Xbox" if platform == "xbox" else "Steam"


def slot_words(label: str) -> str:
    """``S9`` -> ``Steam slot 9``; ``X2.4`` -> ``Xbox 2 slot 4``."""
    if not label:
        return ""
    word = "Xbox" if label[0].upper() == "X" else "Steam"
    rest = label[1:]
    if "." in rest:
        folder, slot = rest.split(".", 1)
        return f"{word} {folder} slot {slot}"
    return f"{word} slot {rest}"
