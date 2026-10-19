"""Deep detail of one version, decoded on demand when its card opens (D4 2.10.4, C10).

Every own base's part histogram, every inventory's slots and the loose placeables: up to
~100 KB per version, so it is never cached on disk - a version card asks for it, a worker
decodes it, and the service keeps the last few in memory. Ids only (part and item names are
game content not shipped in v1). Bases are identified exactly as the facts identify them.
"""
from __future__ import annotations

from typing import TypedDict

from ..savejson import DecodedSave
from .extract import CtxInput, contexts_of
from .extract.bases import loose_objects, walk_bases
from .extract.inventories import EXOSUIT, FREIGHTER, STORAGE, VEHICLE
from .nodes import get, gid, integer, items, text


class DeepFacts(TypedDict):
    vid: str
    contexts: dict[str, dict]   # ctx -> {"bases": [...], "inventories": {...}, "loose_objects": {...}}


def deep_facts(vid: str, decoded: DecodedSave) -> DeepFacts:
    _save, contexts = contexts_of(decoded.root)
    return {"vid": vid, "contexts": {name: _context(ctx) for name, ctx in contexts.items()}}


def _context(ctx: CtxInput) -> dict:
    bases = [{"id": bid, "name": text(get(base, "Name")), "type": base_type,
              "parts_by_id": dict(sorted(ids.items()))}
             for bid, base, base_type, ids in walk_bases(ctx).own]
    psd = ctx.psd
    inventories = {
        "exosuit": _slots(get(psd, n) for n in EXOSUIT),
        "freighter": _slots(get(psd, n) for n in FREIGHTER),
        "storage": _slots(get(psd, n) for n in STORAGE),
        "vehicles": _slots(get(v, n) for v in items(psd, "VehicleOwnership") for n in VEHICLE),
    }
    for ship in ctx.ships:
        inventories[f"ship:{ship.index}"] = _slots(get(ship.node, n) for n in EXOSUIT)
    for tool in ctx.multitools:
        inventories[f"multitool:{tool.index}"] = _slots([get(tool.node, "Store")])
    return {"bases": bases, "inventories": inventories, "loose_objects": dict(sorted(loose_objects(ctx).items()))}


def _slots(inventories) -> list[dict]:
    out = []
    for inv in inventories:
        for slot in items(inv, "Slots"):
            out.append({"id": gid(get(slot, "Id")), "amount": _num(get(slot, "Amount")),
                        "max": _num(get(slot, "MaxAmount")),
                        "x": integer(get(slot, "Index", "X")), "y": integer(get(slot, "Index", "Y"))})
    return out


def _num(v: object) -> int | float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
