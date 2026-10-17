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

from ...hashname import key
from ..lookups.parts import LEGACY_FREIGHTER, LEGACY_STRUCTURES, OWN_BASE_TYPES
from ..nodes import galaxy_of, get, gid, hex64, integer, items, seed, text, u64
from ..privacy import owner_platform
from . import CtxInput, context_extractor

_OBJECT_ID = key("ObjectID")      # read once per placed part: up to ~15,000 per save
IGNORED_TYPES = frozenset({"UITempShipBase"})
CORVETTE_FOLDER = "/BIGGS/"       # corvette ship models live under MODELS/.../BIGGS/


def corvette_slots(psd: dict) -> set[int]:
    return {i for i, s in enumerate(items(psd, "ShipOwnership"))
            if CORVETTE_FOLDER in (_str(get(s, "Resource", "Filename")))}


def _str(v: object) -> str:
    return v if isinstance(v, str) else ""


def _digest(value: object, n: int) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()[:n]


def _base_id(base_type: str, base: dict, address: int | None) -> str:
    # The freighter base and corvette layouts move with their vehicle, so their address
    # changes every save; two planet bases can share a planet, so position is part of it.
    if base_type == "FreighterBase":
        return "freighter"
    if base_type == "PlayerShipBase":
        return f"corvette:{integer(get(base, 'UserData'))}"
    return f"{base_type[:12]}:" + _digest([hex64(address), get(base, "Position")], 12)


@context_extractor("bases", ("bases", "exocraft_bays"))
def bases(ctx: CtxInput, out: dict) -> None:
    psd = ctx.psd
    corvettes = corvette_slots(psd)
    own: list[dict] = []
    external = orphans = 0
    unknown: Counter = Counter()
    platforms: set[str] = set()
    for base in items(psd, "PersistentPlayerBases"):
        base_type = get(base, "BaseType", "PersistentBaseTypes")
        if not isinstance(base_type, str) or base_type in IGNORED_TYPES:
            continue
        if base_type not in OWN_BASE_TYPES:
            if base_type.startswith("External"):
                external += 1
            else:
                unknown[text(base_type, 64)] += 1
            continue
        if base_type == "PlayerShipBase" and integer(get(base, "UserData")) not in corvettes:
            orphans += 1
            continue
        ids = Counter(gid(o.get(_OBJECT_ID)) for o in items(base, "Objects") if isinstance(o, dict))
        address = u64(get(base, "GalacticAddress"))
        own.append({
            "id": _base_id(base_type, base, address),
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
    out["bases"] = {"own": own, "external_cached": external, "orphan_corvette_layouts": orphans,
                    "unknown_types": dict(sorted(unknown.items())), "ptk": sorted(platforms)}
    loose = (gid(o.get(_OBJECT_ID)) for o in items(psd, "BaseBuildingObjects") if isinstance(o, dict))
    out["exocraft_bays"] = sorted({i for i in loose if i.startswith("GARAGE") or i == "SUMMON_GARAGE"})


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
