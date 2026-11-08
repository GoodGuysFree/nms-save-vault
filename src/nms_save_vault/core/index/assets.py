"""Every asset of a save, each with the key that tells it apart (Savecraft research R2): ships,
corvettes, multi-tools, the freighter, frigates, squadron pilots, exocraft, companions, eggs,
bases and saved locations.

An asset's ``key`` is the same in every version and every save that holds that asset, and only
there: ``(kind, key)`` equal is the same asset, or an identical copy of it (reward ships, cloned
companions). A list position is never part of it, since the game reorders lists; nor a name,
since players rename things. Exocraft are the exception: the save has one slot per exocraft
type and names none by model, so the slot is the key.

Read as the game keeps them (live Steam and Xbox saves, 2026-11-09): an exocraft slot always
exists, with its inventories and its starting technology, so one counts only once it was
placed in the world (``Location`` set; 60 of 259 slots) or holds something in its other
inventories (never seen without a placement); ``FreighterFleet`` entries were empty in every save checked, and one
is read only when it names a model. Names, game ids and seeds only: what a model, creature or
class is called is game content that the reader does not ship.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field

from ..hashname import key
from .extract import Owned, contexts_of
from .extract.bases import CORVETTE_FOLDER, walk_bases
from .nodes import galaxy_of, get, gid, hex64, integer, items, label, seed, text, u64
from .privacy import DENIED, owner_time

#: Raised whenever what :func:`assets_of`, :func:`state_of` or ``inventories_of`` give for
#: the same save changes (a key, a state, a field read differently): an app that keeps their
#: results reads every save again (``api.INDEX_VERSION``).
INDEX_VERSION = 2

#: Every kind, in the order :func:`assets_of` lists them.
KINDS = ("ship", "corvette", "multitool", "freighter", "frigate", "pilot", "exocraft",
         "companion", "egg", "base", "location")
MODEL_LIMIT = 256

#: What changes in a save by itself and never makes an asset different: where it is and which
#: way it faces, times, a companion's trust and moods, a multi-tool's firing mode, a base's
#: difficulty (it follows the save's). Measured on consecutive vault versions, 2026-11-09.
VOLATILE = ("Timestamp", "LastUpdateTimestamp", "LastSpawnTime", "Position", "Direction",
            "Forward", "Facing", "Up", "At", "Location", "MatrixAt", "MatrixUp", "MatrixPos",
            "ScreenshotAt", "ScreenshotPos", "UserData", "Difficulty", "LastTrustIncreaseTime",
            "LastTrustDecreaseTime", "LastEggTime", "Trust", "Moods", "PrimaryMode",
            "SecondaryMode")
_SKIP = frozenset(key(n) for n in VOLATILE) | frozenset(key(n) for n in DENIED)
_AMOUNT = key("Amount")


@dataclass(frozen=True)
class Asset:
    context: str                        # "base" or "expedition"
    kind: str                           # one of KINDS
    index: int | None                   # its place in its list; None for the current freighter
    where: tuple[str | int, ...]        # its readable path from the save's root
    key: str                            # the same asset in any version or save has this key
    name: str                           # the player's name for it; "" when it has none
    model: str | None                   # what the game's data knows its type by (see Asset.model)
    cls: str | None                     # C, B, A or S
    current: bool | None                # the ship, multi-tool, exocraft in use; None: no such thing
    galaxy: int | None                  # the galaxy it is in (RealityIndex), for one tied to a place
    address: str | None                 # its galactic address, 0x%016X, for one tied to a place
    facts: dict = field(default_factory=dict, hash=False)   # by kind, see the readers below

    # Asset.model is a model path for ships, corvettes, multi-tools, freighters and pilots (their
    # ship), the frigate class, the creature id of a companion or an egg, the base type and the
    # teleporter type; None for exocraft (Asset.index is the vehicle type's place in the game's
    # VehicleType list).


def assets_of(root: dict) -> list[Asset]:
    """Every asset of the decoded save ``root`` (``DecodedSave.root``), context by context, kind
    by kind in :data:`KINDS` order, each list in the save's order."""
    _save, contexts = contexts_of(root)
    out: list[Asset] = []
    for name, ctx in contexts.items():
        base = ("PlayerStateData",) if ctx.container is None else (
            ("BaseContext" if name == "base" else "ExpeditionContext"), "PlayerStateData")
        psd = ctx.psd
        layouts = _corvette_layouts(ctx)
        out += _ships(name, base, psd, ctx.ships, layouts)
        out += _multitools(name, base, psd, ctx.multitools)
        freighters = _freighters(name, base, psd)
        out += freighters
        out += _frigates(name, base, psd)
        out += _pilots(name, base, psd)
        out += _exocraft(name, base, psd)
        out += _creatures(name, base, items(psd, "Pets"), "Pets", "companion")
        out += _creatures(name, base, items(psd, "Eggs"), "Eggs", "egg")
        current = next((f.key for f in freighters if f.current), None)
        out += _bases(name, base, ctx, current)
        out += _locations(name, base, psd)
    order = {k: n for n, k in enumerate(KINDS)}
    contexts_order = {c: n for n, c in enumerate(contexts)}
    return sorted(out, key=lambda a: (contexts_order[a.context], order[a.kind]))


def _digest(value: object) -> str:
    body = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("ascii")).hexdigest()[:16]


def _key(*parts: object) -> str:
    return "|".join("" if p is None else str(p) for p in parts)


def _ships(context: str, base: tuple, psd: dict, ships: tuple[Owned, ...],
           layouts: dict[int, int]) -> list[Asset]:
    """``facts``: ``seed``; a corvette's ``layout_parts``, the parts of the layout it owns, and
    ``layout``, which parts (a digest: the same parts give the same digest)."""
    primary = integer(get(psd, "PrimaryShip"))
    out = []
    for s in ships:
        corvette = CORVETTE_FOLDER in s.model
        ship_seed = seed(get(s.node, "Resource", "Seed"))
        facts: dict = {"seed": ship_seed}
        if corvette:
            parts, digest = layouts.get(s.index, (None, None))
            facts["layout_parts"], facts["layout"] = parts, digest
        out.append(Asset(context, "corvette" if corvette else "ship", s.index,
                         base + ("ShipOwnership", s.index), _key(s.model, ship_seed),
                         text(get(s.node, "Name")), s.model,
                         label(get(s.node, "Inventory", "Class", "InventoryClass")),
                         s.index == primary, None, None, facts))
    return out


def _corvette_layouts(ctx) -> dict[int, tuple[int, str]]:
    """Ship slot -> (part count, digest of which parts) of the layout its corvette owns: by
    ``UserData`` = the slot, or an owner block made in the corvette's seed second or the next
    (``bases.claimed``). Where a part sits is not in the digest; times and user data churn."""
    corvettes = {}
    for s in ctx.ships:
        if CORVETTE_FOLDER in s.model:
            value = seed(get(s.node, "Resource", "Seed"))
            corvettes[s.index] = int(value, 16) if value else None
    out: dict[int, tuple[int, str]] = {}
    if not corvettes:
        return out
    for _bid, node, base_type, ids in walk_bases(ctx).own:
        if base_type != "PlayerShipBase":
            continue
        slot = integer(get(node, "UserData"))
        ts = owner_time(node)
        owner = slot if slot in corvettes else next(
            (i for i, s in corvettes.items() if ts is not None and s is not None and 0 <= ts - s <= 1),
            None)
        if owner is not None and owner not in out:
            out[owner] = (sum(ids.values()), _digest(sorted(ids.items())))
    return out


def _multitools(context: str, base: tuple, psd: dict, tools: tuple[Owned, ...]) -> list[Asset]:
    """``facts``: ``seed``. A save from before multi-tool collections has one tool: the model
    in ``CurrentWeapon``, its slots in ``WeaponInventory`` (as ``inventories_of`` lists it)."""
    active = integer(get(psd, "ActiveMultioolIndex"))     # sic: the game's spelling
    out = []
    if not tools:
        model = text(get(psd, "CurrentWeapon", "Filename"), MODEL_LIMIT)
        if model:
            tool_seed = seed(get(psd, "CurrentWeapon", "Seed"))
            out.append(Asset(context, "multitool", None, base + ("CurrentWeapon",),
                             _key(model, tool_seed), "", model,
                             label(get(psd, "WeaponInventory", "Class", "InventoryClass")),
                             True, None, None, {"seed": tool_seed}))
    for t in tools:
        tool_seed = seed(get(t.node, "Seed"))
        out.append(Asset(context, "multitool", t.index, base + ("Multitools", t.index),
                         _key(t.model, tool_seed), text(get(t.node, "Name")), t.model,
                         label(get(t.node, "Store", "Class", "InventoryClass")),
                         t.index == active, None, None, {"seed": tool_seed}))
    return out


def _freighters(context: str, base: tuple, psd: dict) -> list[Asset]:
    """The current freighter, then any other in ``FreighterFleet``. ``facts``: ``seed``,
    ``home_seed``."""
    out = []
    model = text(get(psd, "CurrentFreighter", "Filename"), MODEL_LIMIT)
    current_key = None
    if model:
        f_seed = seed(get(psd, "CurrentFreighter", "Seed"))
        home = seed(get(psd, "CurrentFreighterHomeSystemSeed"))
        current_key = _key(model, f_seed, home)
        out.append(Asset(context, "freighter", None, base + ("CurrentFreighter",), current_key,
                         text(get(psd, "PlayerFreighterName")), model,
                         label(get(psd, "FreighterInventory", "Class", "InventoryClass")),
                         True, None, None, {"seed": f_seed, "home_seed": home}))
    for index, entry in enumerate(items(psd, "FreighterFleet")):
        model = text(get(entry, "Resource", "Filename"), MODEL_LIMIT)
        if not model or get(entry, "Dismissed") is True:
            continue
        f_seed = seed(get(entry, "Resource", "Seed"))
        home = seed(get(entry, "HomeSystemSeed"))
        k = _key(model, f_seed, home)
        if k == current_key:
            continue
        out.append(Asset(context, "freighter", index, base + ("FreighterFleet", index), k, "",
                         model, label(get(entry, "Inventory", "Class", "InventoryClass")),
                         False, None, None, {"seed": f_seed, "home_seed": home}))
    return out


def _frigates(context: str, base: tuple, psd: dict) -> list[Asset]:
    """``facts``: ``race`` (the AlienRace), ``expeditions`` (how many it has flown)."""
    out = []
    for index, frigate in enumerate(items(psd, "FleetFrigates")):
        if not isinstance(frigate, dict):
            continue
        out.append(Asset(context, "frigate", index, base + ("FleetFrigates", index),
                         _key(seed(get(frigate, "ResourceSeed")), seed(get(frigate, "HomeSystemSeed"))),
                         text(get(frigate, "CustomName")),
                         label(get(frigate, "FrigateClass", "FrigateClass")),
                         label(get(frigate, "InventoryClass", "InventoryClass")), None, None, None,
                         {"race": label(get(frigate, "Race", "AlienRace")),
                          "expeditions": integer(get(frigate, "TotalNumberOfExpeditions"))}))
    return out


def _pilots(context: str, base: tuple, psd: dict) -> list[Asset]:
    """A filled squadron slot names its pilot's model. ``Asset.model`` is the pilot's ship;
    ``facts``: ``npc`` (the pilot's own model path), ``rank``."""
    out = []
    for index, pilot in enumerate(items(psd, "SquadronPilots")):
        npc = text(get(pilot, "NPCResource", "Filename"), MODEL_LIMIT)
        if not npc:
            continue
        out.append(Asset(context, "pilot", index, base + ("SquadronPilots", index),
                         _key(seed(get(pilot, "NPCResource", "Seed")),
                              seed(get(pilot, "ShipResource", "Seed")),
                              seed(get(pilot, "TraitsSeed"))),
                         "", text(get(pilot, "ShipResource", "Filename"), MODEL_LIMIT) or None,
                         None, None, None, None,
                         {"npc": npc, "rank": integer(get(pilot, "PilotRank"))}))
    return out


def _exocraft(context: str, base: tuple, psd: dict) -> list[Asset]:
    """One per used slot: placed in the world (``Location`` set) or holding something."""
    primary = integer(get(psd, "PrimaryVehicle"))
    out = []
    for index, vehicle in enumerate(items(psd, "VehicleOwnership")):
        location = get(vehicle, "Location")
        placed = (isinstance(location, str) and location.strip() not in ("", "0", "0x0")) or \
            (integer(location) not in (None, 0))
        # its technology inventory is filled from the start; only the others tell use
        holds = any(items(vehicle, f, "Slots") for f in ("Inventory", "Inventory_Cargo"))
        if not (placed or holds):
            continue
        out.append(Asset(context, "exocraft", index, base + ("VehicleOwnership", index),
                         f"slot:{index}", text(get(vehicle, "Name")), None,
                         label(get(vehicle, "Inventory", "Class", "InventoryClass")),
                         index == primary, None, None, {}))
    return out


def _creatures(context: str, base: tuple, entries: list, list_name: str, kind: str) -> list[Asset]:
    """Companions (``Pets``) and eggs; an empty slot has no creature id. ``facts``: ``biome``,
    ``type`` (CreatureType). The sender block of a gifted egg is never read."""
    out = []
    for index, pet in enumerate(entries):
        creature = gid(get(pet, "CreatureID"))
        if not creature:
            continue
        out.append(Asset(context, kind, index, base + (list_name, index),
                         _key(seed(get(pet, "CreatureSeed")), integer(get(pet, "BirthTime"))),
                         text(get(pet, "CustomName")), creature, None, None, None, None,
                         {"biome": label(get(pet, "Biome", "Biome")),
                          "type": label(get(pet, "CreatureType", "CreatureType"))}))
    return out


def _bases(context: str, base: tuple, ctx, freighter: str | None) -> list[Asset]:
    """The player's own bases but corvette layouts (they belong to their corvette). The key is
    NMS Save Vault's base id; the freighter base's, ``freighter`` in every save, gets the key
    of the freighter it is on (``freighter``: the current freighter's), so two saves' freighter
    bases are told apart. ``facts``: ``parts``."""
    out = []
    lst = items(ctx.psd, "PersistentPlayerBases")
    positions = {id(node): n for n, node in enumerate(lst)}
    for bid, node, base_type, ids in walk_bases(ctx).own:
        if base_type == "PlayerShipBase":
            continue
        address = u64(get(node, "GalacticAddress"))
        index = positions.get(id(node))
        if base_type == "FreighterBase" and bid == "freighter" and freighter is not None:
            bid = f"freighter:{freighter}"
        out.append(Asset(context, "base", index, base + ("PersistentPlayerBases", index), bid,
                         text(get(node, "Name")), base_type, None, None, galaxy_of(address),
                         hex64(address), {"parts": sum(ids.values())}))
    return out


def portal_address(galaxy: object, universe: object) -> int | None:
    """A ``UniverseAddress.GalacticAddress`` block (voxel and system numbers) as the 64-bit
    address bases store: planet, system, galaxy, Y, Z, X from the top (``nodes.galaxy_of``)."""
    parts = [integer(get(universe, n)) for n in ("VoxelX", "VoxelY", "VoxelZ",
                                                 "SolarSystemIndex", "PlanetIndex")]
    g = integer(galaxy)
    if g is None or any(p is None for p in parts):
        return None
    x, y, z, system, planet = parts
    return ((planet & 0xF) << 52 | (system & 0xFFF) << 40 | (g & 0xFF) << 32
            | (y & 0xFF) << 24 | (z & 0xFFF) << 12 | (x & 0xFFF))


def _locations(context: str, base: tuple, psd: dict) -> list[Asset]:
    """Saved teleport endpoints. ``Asset.model`` is the teleporter type; ``facts``:
    ``favourite``."""
    out = []
    for index, point in enumerate(items(psd, "TeleportEndpoints")):
        if not isinstance(point, dict):
            continue
        galaxy = integer(get(point, "UniverseAddress", "RealityIndex"))
        address = portal_address(galaxy, get(point, "UniverseAddress", "GalacticAddress"))
        kind = label(get(point, "TeleporterType"))
        position = get(point, "Position")
        where = ",".join(str(round(v)) for v in position) if isinstance(position, list) and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in position) else ""
        out.append(Asset(context, "location", index, base + ("TeleportEndpoints", index),
                         _key(hex64(address), kind, where), text(get(point, "Name")), kind, None,
                         None, galaxy, hex64(address),
                         {"favourite": get(point, "IsFavourite") is True}))
    return out


def state_of(root: dict, asset: Asset) -> str:
    """What ``asset`` is like in the decoded save ``root``, as 16 hex digits: two versions that
    give the same digest hold it unchanged. Its place, times and charge are left out
    (:data:`VOLATILE`; a technology's amount is its charge), and every ownership field. A base
    is its name and which parts it has, a saved location its name and whether it is a
    favourite (the rest is its key); the current freighter includes its inventories, which the
    save keeps beside it."""
    node = _at(root, *asset.where)
    if asset.kind == "base":
        value: object = [asset.name, sorted(Counter(
            gid(get(o, "ObjectID")) for o in items(node, "Objects") if isinstance(o, dict)).items())]
    elif asset.kind == "corvette":
        value = [_stable(node), asset.facts.get("layout")]
    elif asset.kind == "location":
        value = [asset.name, asset.facts.get("favourite")]
    elif asset.kind == "freighter" and asset.index is None:
        psd = _at(root, *asset.where[:-1])
        value = [_stable(node), asset.name] + [_stable(get(psd, f)) for f in (
            "FreighterInventory", "FreighterInventory_TechOnly", "FreighterInventory_Cargo")]
    else:
        value = _stable(node)
    body = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("ascii")).hexdigest()[:16]


def _at(root: object, *where: str | int) -> object:
    node = root
    for step in where:
        if isinstance(step, str):
            node = get(node, step)
        else:
            node = node[step] if isinstance(node, list) and 0 <= step < len(node) else None
    return node


def _stable(node: object) -> object:
    if isinstance(node, dict):
        technology = get(node, "Type", "InventoryType") == "Technology"
        return {k: _stable(v) for k, v in node.items()
                if k not in _SKIP and not (technology and k == _AMOUNT)}
    if isinstance(node, list):
        return [_stable(v) for v in node]
    return node
