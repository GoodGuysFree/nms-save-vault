"""The organizer's words, shared by the table, the detail pane and the notes window: names,
version numbers and how a version is linked (D6 2.1, C32: Save file, Version, Live, Vault).
Pure functions of the read model; every string is ASCII apart from the game's and the
owner's own text."""
from __future__ import annotations

from collections import defaultdict

from ...core import aliases
from ...core.liveops.planner import TEXTS
from ...core.organizer.model import platform_letter, platform_name, store_of
from ...core.organizer.readmodel import Line, ReadModel, VersionRow

#: The warning on a version only the live folder holds, and what it means (hover text).
NO_BACKUP = "NO BACKUP YET"
NO_BACKUP_TIP = ("This version exists only in the live save folder. No backup in the vault "
                 "holds it yet. Back up (or let a move keep it) so it is not lost if the "
                 "slot is overwritten.")

#: Owner-approved hover and status-bar texts for words that are not obvious. Static, so
#: no names can be in them (they still pass the redaction filter when shown).
NOT_LINKED_TIP = ("No recorded move or backup links these versions, so they are listed by "
                  "date. Running the organizer bootstrap links versions that backups saw.")
OUTSIDE_ONLY_TIP = ("Kept only in a folder you made yourself that the app catalogues in "
                    "place - not copied into the vault.")
PROMOTE_TIP = "Make the slot's other save the one the game loads."
PIN_TIP = "A pinned save is never suggested as the one to replace when a move needs a slot."
VAULT_UPGRADE_TIP = ("One-time conversion to the new storage. Your vault is copied aside "
                     "first, so it can be undone.")
FREE_OLD_TIP = ("Delete the old backup folders that are now in the version store, after a full "
                "check. Every backup stays listed. Asks first.")
SLOT_MARKERS_TIP = ("* = the save the game loads. / moved = the file was moved here from "
                    "another slot.")

#: Pins against thinning (docs/thinning.md Q2): a backup in the Backups list, a version in
#: History (the version note's lock). The mark follows a pinned row's name.
PIN_LABEL = "Pin (thinning never deletes it)"
UNPIN_LABEL = "Unpin"
PIN_MARK = "  (pinned)"

#: Apps that share the vault, by the name their records carry (core.appinfo), as people know
#: them. An app not listed is shown by its record name.
APP_NAMES = {"nms-savecraft": "NMS Savecraft"}
APP_EDITED_TIP = ("Another app that shares this vault made this version, or the game saved it "
                  "from one that app made.")


def app_name(app: str) -> str:
    return APP_NAMES.get(app, app)


def edited_by_words(apps) -> str:
    """'edited by NMS Savecraft' (CL1.6); empty when no other app edited it."""
    return f"edited by {', '.join(app_name(a) for a in apps)}" if apps else ""


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


#: A store's name (core.organizer.model.Store): GOG is counted on its own, not as Steam.
STORE_WORDS = {"steam": "Steam", "gog": "GOG", "xbox": "Xbox"}


def platform_word(platform: str) -> str:
    """A platform's or a store's name: ``Steam``, ``GOG`` or ``Xbox``."""
    return STORE_WORDS.get(platform, "Steam")


def stores_word(item) -> str:
    """``Steam``, ``GOG``, ``Steam, GOG``: the stores of a line or version row."""
    stores = item.stores or getattr(item, "platforms", None) or (item.platform,)
    return ", ".join(platform_word(s) for s in stores)


def store_colour(palette, platform: str, account_key: str = "") -> str:
    """The colour of a live folder's (or store's) chips: Steam blue, GOG purple, Xbox green."""
    return palette[store_of(platform, account_key) if platform != "gog" else "gog"]


#: A live badge's first letter (S9, G9, X2.4) and the kind of account it names.
BADGE_WORDS = {"S": "Steam", "G": "GOG", "X": "Xbox"}


def slot_words(label: str) -> str:
    """``S9`` -> ``Steam slot 9``; ``G9`` -> ``GOG slot 9``; ``X2.4`` -> ``Xbox 2 slot 4``."""
    if not label:
        return ""
    word = BADGE_WORDS.get(label[0].upper(), "Steam")
    rest = label[1:]
    if "." in rest:
        folder, slot = rest.split(".", 1)
        return f"{word} {folder} slot {slot}"
    return f"{word} slot {rest}"


def tip_name(tip) -> str:
    """The kind of account of a platform tip: ``Steam``, ``GOG`` or ``Xbox`` (the tips are
    compared by save format, so the Steam-format tip may be GOG's)."""
    return platform_name(tip.target.platform, tip.target.account_key)


def tips_letters(line: Line) -> str:
    """``S/X`` or ``G/X``: the letters of a line's platform tips."""
    return "/".join(platform_letter(t.target.platform, t.target.account_key)
                    for t in line.platform_tips)


#: What a placeholder reads as when a code is shown without the slot or save it was made for.
GENERAL = {"slot": "the slot", "name": "the save", "was": "the save", "twin": "another slot",
           "where": "another slot", "ahead": "more", "played": "more", "slots": "some slots"}


def plain(code: str) -> str:
    """The plain words for a planner block or warning code (the planner's own table, ``TEXTS``):
    the screen never shows a raw code, and an unknown one reads as a general sentence."""
    entry = TEXTS.get(code)
    if entry is None:
        return "This is not possible right now."
    text = entry[1].format_map(defaultdict(str, GENERAL)).strip()
    return str(aliases.redact(text or "This slot may fill again after the move."))
