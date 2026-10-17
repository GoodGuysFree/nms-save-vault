"""Fact schema v1: what the index stores per save version, and the whitelist that enforces it.

Facts are raw (ids, counts, indices, digests); names and milestone completion are derived at
read time (``derive.py``) so a lookup-table update never forces a re-index. The TypedDicts
document the shapes; :data:`SPEC` is the same schema as data, and :func:`validate` keeps only
what it declares: an undeclared key or a value of the wrong type is dropped and reported, so
an extractor that copies a whole save subtree by mistake cannot leak it into the cache.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

SCHEMA_VERSION = 1       # layout of the facts below
EXTRACTOR_VERSION = 1    # bumped by any change to extractor output (rows go stale, re-queued)
DECODER_VERSION = 1      # bumped when the decoder learns a format (failed rows re-queued)

Status = Literal["ok", "partial", "corrupt", "unsupported", "error"]
STATUSES: tuple[str, ...] = ("ok", "partial", "corrupt", "unsupported", "error")
CONTEXTS: tuple[str, ...] = ("base", "expedition")


class ExtractorError(TypedDict):
    extractor: str                       # "base.ships"
    error: str                           # "<Type>: <message>", <= 200 chars, no paths or values


class ContainerFacts(TypedDict):
    kind: Literal["chunked", "unframed"]
    chunks: int
    decompressed_bytes: int
    decompressed_sha256: str
    json_bytes: int


class SeasonFacts(TypedDict):
    id: int | None                       # SeasonData.SeasonId (0 = no expedition)
    number: int | None                   # SeasonNumber
    display: int | None                  # DisplayNumber
    name_key: str                        # "UI_SEASON_22_NAME", resolved at read time
    milestones: list[list[float | bool]] # [[value, amount, optional], ...]
    final_reward: bool
    special_rewards: int


class SaveFacts(TypedDict):
    layout: Literal["context", "pre-context"]
    json_version: int | None
    active_context: str | None
    save_name: str
    play_time_s: int | None
    pid_in_data: str | None              # SaveUniversalId as 0x%016X
    season: SeasonFacts | None
    discoveries: dict                    # {"total": int, "by_type": {type: int}}


class OwnBase(TypedDict):
    id: str
    name: str
    type: str
    galaxy: int | None
    address: str | None
    parts: int
    legacy_structures: int
    legacy_freighter: int
    orig_version: int | None
    layout: str


class Ship(TypedDict):
    slot: int
    name: str
    model_path: str
    cls: str | None
    seed: str | None
    primary: bool


class ContextFacts(TypedDict, total=False):
    game_mode: int | None
    timestamp: int | None
    difficulty: dict
    galaxy: int | None
    summary: str
    units: int | None
    nanites: int | None
    quicksilver: int | None
    glyphs: int | None
    nexus: bool
    title: str
    teleporters: int
    known_tech: int
    known_products: int
    legacy_known: dict
    words: dict
    missions: dict
    stats: dict
    bases: dict
    exocraft_bays: list[str]
    ships: list[Ship]
    archived_ships: list[dict]
    companions: list[dict]
    eggs: int
    pet_slots: int
    freighter: dict
    frigates: int
    multitools: list[dict]
    settlements: dict
    inventories: dict


class VersionFacts(TypedDict):
    schema: int
    extractor: int
    status: Status
    errors: list[ExtractorError]
    container: ContainerFacts | None
    save: SaveFacts | None
    contexts: dict[str, ContextFacts]
    primary: Literal["base", "expedition"] | None


# --- the same schema as data -------------------------------------------------------------


@dataclass(frozen=True)
class Leaf:
    kind: Literal["int", "num", "str", "bool"]
    nullable: bool = False


@dataclass(frozen=True)
class ListOf:
    item: Any


@dataclass(frozen=True)
class MapOf:
    """A dict with free (string) keys, every value of one shape."""
    value: Any


@dataclass(frozen=True)
class TupleOf:
    items: tuple


@dataclass(frozen=True)
class Nullable:
    spec: Any


INT, NUM, STR, BOOL = Leaf("int"), Leaf("num"), Leaf("str"), Leaf("bool")
INT_N, STR_N, BOOL_N = Leaf("int", True), Leaf("str", True), Leaf("bool", True)

CONTAINER_SPEC = {"kind": STR, "chunks": INT, "decompressed_bytes": INT, "decompressed_sha256": STR,
                  "json_bytes": INT}
SEASON_SPEC = {"id": INT_N, "number": INT_N, "display": INT_N, "name_key": STR,
               "milestones": ListOf(TupleOf((NUM, NUM, BOOL))), "final_reward": BOOL, "special_rewards": INT}
SAVE_SPEC = {
    "layout": STR, "json_version": INT_N, "active_context": STR_N, "save_name": STR,
    "play_time_s": INT_N, "pid_in_data": STR_N, "season": Nullable(SEASON_SPEC),
    "discoveries": {"total": INT, "by_type": MapOf(INT)},
}
OWN_BASE_SPEC = {"id": STR, "name": STR, "type": STR, "galaxy": INT_N, "address": STR_N, "parts": INT,
                 "legacy_structures": INT, "legacy_freighter": INT, "orig_version": INT_N, "layout": STR}
SHIP_SPEC = {"slot": INT, "name": STR, "model_path": STR, "cls": STR_N, "seed": STR_N, "primary": BOOL}
CONTEXT_SPEC = {
    "game_mode": INT_N, "timestamp": INT_N,
    "difficulty": {"preset": STR_N, "easiest": STR_N, "hardest": STR_N, "death": STR_N,
                   "crafting_free": BOOL_N, "locked": BOOL_N},
    "galaxy": INT_N, "summary": STR, "units": INT_N, "nanites": INT_N, "quicksilver": INT_N,
    "glyphs": INT_N, "nexus": BOOL, "title": STR, "teleporters": INT, "known_tech": INT,
    "known_products": INT, "legacy_known": {"structures": INT, "freighter": INT},
    "words": {"gek": INT, "vykeen": INT, "korvax": INT, "atlas": INT, "autophage": INT},
    "missions": {"version": INT_N, "current": STR, "progress": MapOf(INT)},
    "stats": MapOf(NUM),
    "bases": {"own": ListOf(OWN_BASE_SPEC), "external_cached": INT, "orphan_corvette_layouts": INT,
              "unknown_types": MapOf(INT), "ptk": ListOf(STR)},
    "exocraft_bays": ListOf(STR),
    "ships": ListOf(SHIP_SPEC),
    "archived_ships": ListOf({"name": STR, "ship_class": STR_N, "inv_class": STR_N}),
    "companions": ListOf({"slot": INT, "name": STR, "creature": STR, "biome": STR_N, "seed": STR_N}),
    "eggs": INT, "pet_slots": INT,
    "freighter": {"has": BOOL, "name": STR, "model": STR, "cls": STR_N},
    "frigates": INT,
    "multitools": ListOf({"name": STR, "model": STR, "cls": STR_N}),
    "settlements": {"claimed": INT, "names": ListOf(STR)},
    "inventories": MapOf({"slots": INT, "capacity": INT, "digest": STR}),
}
SPEC = {
    "schema": INT, "extractor": INT, "status": STR,
    "errors": ListOf({"extractor": STR, "error": STR}),
    "container": Nullable(CONTAINER_SPEC), "save": Nullable(SAVE_SPEC),
    "contexts": MapOf(CONTEXT_SPEC), "primary": STR_N,
}
TYPED = {"VersionFacts": (VersionFacts, SPEC), "SaveFacts": (SaveFacts, SAVE_SPEC),
         "ContainerFacts": (ContainerFacts, CONTAINER_SPEC), "SeasonFacts": (SeasonFacts, SEASON_SPEC),
         "ContextFacts": (ContextFacts, CONTEXT_SPEC), "OwnBase": (OwnBase, OWN_BASE_SPEC),
         "Ship": (Ship, SHIP_SPEC)}

_DROP = object()


def validate(facts: dict) -> tuple[dict, list[str]]:
    """``facts`` reduced to what :data:`SPEC` declares, and one line per thing dropped.

    Dropping never invents a value: a missing key stays missing. Problems name the path
    and the type found, never the value (the value may be exactly what must not leak).
    """
    problems: list[str] = []
    out = _check(facts, SPEC, "facts", problems)
    return ({} if out is _DROP else out), problems


def _check(value: Any, spec: Any, path: str, problems: list[str]) -> Any:
    if isinstance(spec, Nullable):
        return None if value is None else _check(value, spec.spec, path, problems)
    if isinstance(spec, Leaf):
        if value is None and spec.nullable:
            return None
        if _leaf_ok(spec.kind, value):
            return value
        problems.append(f"{path}: expected {spec.kind}, got {type(value).__name__}")
        return _DROP
    if isinstance(spec, ListOf):
        if not isinstance(value, list):
            problems.append(f"{path}: expected list, got {type(value).__name__}")
            return _DROP
        kept = [_check(v, spec.item, f"{path}[{i}]", problems) for i, v in enumerate(value)]
        return [v for v in kept if v is not _DROP]
    if isinstance(spec, TupleOf):
        if not isinstance(value, (list, tuple)) or len(value) != len(spec.items):
            problems.append(f"{path}: expected {len(spec.items)} items")
            return _DROP
        kept = [_check(v, s, f"{path}[{i}]", problems) for i, (v, s) in enumerate(zip(value, spec.items))]
        return _DROP if any(v is _DROP for v in kept) else kept
    if isinstance(spec, MapOf):
        if not isinstance(value, dict):
            problems.append(f"{path}: expected object, got {type(value).__name__}")
            return _DROP
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                problems.append(f"{path}: dropped a non-string key")
                continue
            # Map keys come from the save (stat ids, container names...): never in a problem.
            checked = _check(v, spec.value, f"{path}[key]", problems)
            if checked is not _DROP:
                out[k] = checked
        return out
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            problems.append(f"{path}: expected object, got {type(value).__name__}")
            return _DROP
        out = {}
        for k, v in value.items():
            if k not in spec:
                problems.append(f"{path}: dropped an undeclared key")   # the key itself may be data
                continue
            checked = _check(v, spec[k], f"{path}.{k}", problems)
            if checked is not _DROP:
                out[k] = checked
        return out
    raise TypeError(f"bad schema node at {path}: {spec!r}")


def _leaf_ok(kind: str, v: Any) -> bool:
    if isinstance(v, bool):
        return kind == "bool"
    if kind == "int":
        return isinstance(v, int)
    if kind == "num":
        return isinstance(v, (int, float))
    if kind == "str":
        return isinstance(v, str)
    return False
