"""Find text anywhere in the vault: save file names, notes, labels and comments, and what the
content index read from each version - bases and the galaxy each one is in, ships, companions,
the freighter, multi-tools, settlements, the expedition, where the player stood.

Read only and format-neutral: the index's facts are read as they are stored (no new table, no
re-index) and named with the same read-time lookups as the views (``derive``). A version the
index has not read yet is searched only for what the read model knows, and is counted, so the
caller can say that the answer may be incomplete.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..organizer.readmodel import Line, ReadModel, VersionRow
from . import derive, lookups

SAVE_FILE, SAVE_NAME, NOTE, LABEL, COMMENT = "Save file", "Save name", "Note", "Label", "Comment"
MODE, LOCATION, GALAXY, EXPEDITION = "Mode", "Location", "Galaxy", "Expedition"
BASE, BASE_GALAXY, SETTLEMENT = "Base", "Base galaxy", "Settlement"
SHIP, ARCHIVED_SHIP, FREIGHTER, MULTITOOL, COMPANION = (
    "Ship", "Archived ship", "Freighter", "Multi-tool", "Companion")

#: Every field, in the order results list them.
FIELDS = (SAVE_FILE, SAVE_NAME, NOTE, LABEL, COMMENT, MODE, EXPEDITION, LOCATION, GALAXY, BASE,
          BASE_GALAXY, SETTLEMENT, SHIP, ARCHIVED_SHIP, FREIGHTER, MULTITOOL, COMPANION)
#: The fields only the content index knows.
INDEX_FIELDS = frozenset({EXPEDITION, GALAXY, BASE, BASE_GALAXY, SETTLEMENT, SHIP, ARCHIVED_SHIP,
                          FREIGHTER, MULTITOOL, COMPANION})
_FROM_FACTS = INDEX_FIELDS | {SAVE_NAME, LOCATION}
PLANET_BASE = "HomePlanetBase"      # the only base type that stays in one galaxy


@dataclass(frozen=True)
class TextItem:
    field: str
    value: str          # what is matched
    detail: str = ""    # shown beside it: a base galaxy's base, a ship's type


@dataclass(frozen=True)
class Hit:
    pid: str
    field: str
    value: str
    detail: str
    heads: tuple[str, ...]      # the versions it is in, newest first; () for the save file itself


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[Hit, ...]
    versions: int               # versions searched
    unindexed: int              # of them, the ones the content index has not read


def _add(out: list[TextItem], field: str, value: object, detail: str = "") -> None:
    if isinstance(value, str) and value.strip():
        out.append(TextItem(field, value.strip(), detail))


def facts_items(facts: Mapping) -> list[TextItem]:
    """Every text the content index holds for one version, named as the views name it."""
    out: list[TextItem] = []
    _add(out, SAVE_NAME, (facts.get("save") or {}).get("save_name"))
    expedition = derive.expedition_of(facts)
    if expedition is not None:
        number = expedition["number"]
        _add(out, EXPEDITION, expedition["name"], f"expedition {number}" if number else "")
    contexts = facts.get("contexts") or {}
    for name, ctx in contexts.items():
        where = "in the expedition" if name == "expedition" and len(contexts) > 1 else ""
        _add(out, GALAXY, lookups.galaxy_label(ctx.get("galaxy")), where or "current galaxy")
        _add(out, LOCATION, ctx.get("summary"), where)
        for base in (ctx.get("bases") or {}).get("own") or []:
            kind = lookups.base_type_label(base.get("type", ""))
            if base.get("type") == PLANET_BASE:
                galaxy = lookups.galaxy_label(base.get("galaxy"))
                _add(out, BASE, base.get("name"), f"{kind}, {galaxy}" if galaxy else kind)
                _add(out, BASE_GALAXY, galaxy, base.get("name") or kind)
            else:
                _add(out, BASE, base.get("name"), kind)
        for town in (ctx.get("settlements") or {}).get("names") or []:
            _add(out, SETTLEMENT, town)
        for ship in ctx.get("ships") or []:
            _add(out, SHIP, derive.ship_display(ship), lookups.ship_type(ship.get("model_path", "")))
        for ship in ctx.get("archived_ships") or []:
            _add(out, ARCHIVED_SHIP, ship.get("name"), lookups.ship_class_name(ship.get("ship_class")) or "")
        freighter = ctx.get("freighter") or {}
        if freighter.get("has"):
            _add(out, FREIGHTER, freighter.get("name") or freighter.get("model"))
        for tool in ctx.get("multitools") or []:
            _add(out, MULTITOOL, tool.get("name"))
        for pet in ctx.get("companions") or []:
            _add(out, COMPANION, derive.companion_display(pet),
                 lookups.creature_type(pet.get("creature", "")) if pet.get("name") else "")
    return out


def line_items(model: ReadModel, line: Line) -> list[TextItem]:
    """What a save file itself carries: its names and its note, line by line."""
    out: list[TextItem] = []
    _add(out, SAVE_FILE, line.name)
    for name in line.former_names:
        _add(out, SAVE_FILE, name, "an earlier name")
    note = model.notes.save_files.get(line.pid)
    for text in (note.text.splitlines() if note is not None else ()):
        _add(out, NOTE, text)
    return out


def version_items(row: VersionRow) -> list[TextItem]:
    """What the read model knows of a version without the content index."""
    out: list[TextItem] = []
    _add(out, SAVE_NAME, row.name)
    _add(out, LOCATION, row.summary)
    _add(out, MODE, row.mode)
    _add(out, MODE, row.difficulty)
    _add(out, LABEL, row.label)
    for text in row.comment.splitlines():
        _add(out, COMMENT, text)
    return out


def search(model: ReadModel, text: str, *, field: str | None = None,
           facts: Callable[[str], Mapping | None] | None = None,
           newest_only: bool = False) -> SearchResult:
    """Every place ``text`` appears (any case, anywhere in the value), one hit per save file,
    field and value with the versions it is in. ``field`` keeps one of :data:`FIELDS` (then an
    empty ``text`` lists every value of it); ``facts`` adds what the content index read
    (``IndexService.facts``: a cache read, so never on a UI thread); ``newest_only`` searches
    the version each save file is shown by (its representative) only."""
    needle = text.strip().casefold()
    if field is not None and field not in FIELDS:
        raise ValueError(field)
    if not needle and field is None:
        return SearchResult((), 0, 0)

    def wanted(item: TextItem) -> bool:
        return (field is None or item.field == field) and needle in item.value.casefold()

    hits: list[Hit] = []
    versions = unindexed = 0
    order = {f: n for n, f in enumerate(FIELDS)}
    for line in model.lines():
        found: dict[TextItem, list[str]] = {}
        for item in line_items(model, line):
            if wanted(item):
                found.setdefault(item, [])
        rows = model.versions(line.pid)
        if newest_only:
            rows = tuple(r for r in rows if r.head == line.representative) or rows[:1]
        for row in rows:
            versions += 1
            items = version_items(row)
            if facts is not None and (field is None or field in _FROM_FACTS):
                stored = facts(row.head)
                if stored is None:
                    unindexed += 1
                else:
                    items += facts_items(stored)
            for item in dict.fromkeys(items):
                if wanted(item):
                    heads = found.setdefault(item, [])
                    if row.head not in heads:
                        heads.append(row.head)
        hits += [Hit(line.pid, i.field, i.value, i.detail, tuple(heads))
                 for i, heads in sorted(found.items(), key=lambda kv: (order[kv[0].field],
                                                                       kv[0].value.casefold()))]
    return SearchResult(tuple(hits), versions, unindexed)
