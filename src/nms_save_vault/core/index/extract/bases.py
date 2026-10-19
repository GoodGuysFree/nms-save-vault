"""Own bases (identity, parts, legacy parts, layout digest), exocraft bays and settlements.

Only the player's own base types count (``lookups.parts.OWN_BASE_TYPES``). Other players'
``External*`` bases are cached by the game in the same list and only counted;
``UITempShipBase`` (the corvette editor's scratch) is ignored; a corvette layout whose
``UserData`` ship slot holds no corvette is an orphan and only counted. Owner blocks are read
through :func:`privacy.owner_platform` alone (the platform code, never an id).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field

from ...hashname import key
from ..lookups._overrides import OWN_BASE_TYPES
from ..lookups.parts import LEGACY_FREIGHTER, LEGACY_STRUCTURES
from ..nodes import galaxy_of, get, gid, hex64, integer, items, seed, text, u64
from ..privacy import owner_platform
from . import CtxInput, context_extractor

_OBJECT_ID = key("ObjectID")      # read once per placed part: up to ~15,000 per save
IGNORED_TYPES = frozenset({"UITempShipBase"})
CORVETTE_FOLDER = "/BIGGS/"       # corvette ship models live under MODELS/.../BIGGS/


def corvette_slots(ctx: CtxInput) -> set[int]:
    return {s.index for s in ctx.ships if CORVETTE_FOLDER in s.model}


def _digest(value: object, n: int) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()[:n]


def _base_id(base_type: str, base: dict, address: int | None, index: int) -> str:
    # The freighter base and corvette layouts move with their vehicle, so their address
    # changes every save; two planet bases can share a planet, so position is part of it.
    if base_type == "FreighterBase":
        return "freighter"
    if base_type == "PlayerShipBase":
        return f"corvette:{integer(get(base, 'UserData'))}"
    position = get(base, "Position")
    if not isinstance(position, list):
        # No position to tell bases on one planet apart: fall back to the list index.
        return f"{base_type[:12]}:" + _digest([hex64(address), None], 12) + f"#{index}"
    return f"{base_type[:12]}:" + _digest([hex64(address), position], 12)


def unique_id(wanted: str, index: int, used: set[str]) -> str:
    """``wanted``, or (when this save already has a base by that id: two layouts for one
    corvette slot, two bases at one spot) ``wanted#<list index>``."""
    bid = wanted
    while bid in used:
        bid = f"{bid}#{index}"
    used.add(bid)
    return bid


@dataclass
class BaseWalk:
    """One pass over ``PersistentPlayerBases``: the own bases with their identity, and what was
    only counted. Shared by the facts extractor and the on-demand deep decode."""
    own: list[tuple[str, dict, str, Counter]] = field(default_factory=list)   # (id, base, type, ObjectIDs)
    external: int = 0
    orphans: int = 0
    unknown: Counter = field(default_factory=Counter)


def walk_bases(ctx: CtxInput) -> BaseWalk:
    walk = BaseWalk()
    corvettes = corvette_slots(ctx)
    used: set[str] = set()
    for index, base in enumerate(items(ctx.psd, "PersistentPlayerBases")):
        base_type = get(base, "BaseType", "PersistentBaseTypes")
        if not isinstance(base_type, str) or base_type in IGNORED_TYPES:
            continue
        if base_type not in OWN_BASE_TYPES:
            if base_type.startswith("External"):
                walk.external += 1
            else:
                walk.unknown[text(base_type, 64)] += 1
            continue
        if base_type == "PlayerShipBase" and integer(get(base, "UserData")) not in corvettes:
            walk.orphans += 1
            continue
        ids = Counter(gid(o.get(_OBJECT_ID)) for o in items(base, "Objects") if isinstance(o, dict))
        address = u64(get(base, "GalacticAddress"))
        walk.own.append((unique_id(_base_id(base_type, base, address, index), index, used), base, base_type, ids))
    return walk


def loose_objects(ctx: CtxInput) -> Counter:
    """Placeables outside any base (``BaseBuildingObjects``: garages, beacons, save points)."""
    return Counter(gid(o.get(_OBJECT_ID)) for o in items(ctx.psd, "BaseBuildingObjects") if isinstance(o, dict))


@context_extractor("bases", ("bases", "exocraft_bays"))
def bases(ctx: CtxInput, out: dict) -> None:
    walk = walk_bases(ctx)
    own: list[dict] = []
    platforms: set[str] = set()
    for bid, base, base_type, ids in walk.own:
        address = u64(get(base, "GalacticAddress"))
        own.append({
            "id": bid,
            "name": text(get(base, "Name")),
            "type": base_type,
            "galaxy": galaxy_of(address),
            "address": hex64(address),
            "parts": sum(ids.values()),
            "legacy_structures": sum(n for i, n in ids.items() if i in LEGACY_STRUCTURES),
            "legacy_freighter": sum(n for i, n in ids.items() if i in LEGACY_FREIGHTER),
            "orig_version": integer(get(base, "OriginalBaseVersion")),
            # Object timestamps and user data churn every save; the histogram does not.
            "layout": _digest(sorted(ids.items()), 16),
        })
        code = owner_platform(base)
        if code:
            platforms.add(code)
    out["bases"] = {"own": own, "external_cached": walk.external, "orphan_corvette_layouts": walk.orphans,
                    "unknown_types": dict(sorted(walk.unknown.items())), "ptk": sorted(platforms)}
    out["exocraft_bays"] = sorted({i for i in loose_objects(ctx) if i.startswith("GARAGE") or i == "SUMMON_GARAGE"})


@context_extractor("settlements", ("settlements",))
def settlements(ctx: CtxInput, out: dict) -> None:
    claimed = [h for h in items(ctx.psd, "SettlementHistory") if _positive(get(h, "PlayerClaimedTime"))]
    seeds = {s for h in claimed if (s := seed(get(h, "SeedValue"))) is not None}
    names = [text(get(s, "Name")) for s in items(ctx.psd, "SettlementStatesV2")
             if seed(get(s, "SeedValue")) in seeds]
    out["settlements"] = {"claimed": len(claimed), "names": names}


def _positive(v: object) -> bool:
    # PlayerClaimedTime is a float in real saves (0.0 = never claimed).
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
