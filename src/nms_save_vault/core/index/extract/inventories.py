"""Inventory digests per container kind: filled slots, capacity and a short content hash.

Inventories are not stored, but the digest makes item changes (an edit outside the game, a
trade) visible to "what changed" (D4 2.5.5).
"""
from __future__ import annotations

import hashlib
import json

from ..nodes import get, items
from . import CtxInput, context_extractor

# Field names spelt out (not built), so the pin test can check every one of them.
STORAGE = (
    "Chest1Inventory", "Chest2Inventory", "Chest3Inventory", "Chest4Inventory", "Chest5Inventory",
    "Chest6Inventory", "Chest7Inventory", "Chest8Inventory", "Chest9Inventory", "Chest10Inventory",
    "ChestMagicInventory", "ChestMagic2Inventory", "CookingIngredientsInventory", "RocketLockerInventory",
    "FishPlatformInventory", "FishBaitBoxInventory", "FoodUnitInventory", "CorvetteStorageInventory",
)
EXOSUIT = ("Inventory", "Inventory_TechOnly", "Inventory_Cargo")
FREIGHTER = ("FreighterInventory", "FreighterInventory_TechOnly", "FreighterInventory_Cargo")
VEHICLE = ("Inventory", "Inventory_TechOnly")


def digest(inventories: list) -> dict:
    rows = []
    capacity = 0
    for inv in inventories:
        if not isinstance(inv, dict):
            continue
        capacity += len(items(inv, "ValidSlotIndices"))
        for slot in items(inv, "Slots"):
            amount, most = get(slot, "Amount"), get(slot, "MaxAmount")
            rows.append([_raw(get(slot, "Id")), _raw(amount), _raw(most),
                         _raw(get(slot, "Index", "X")), _raw(get(slot, "Index", "Y"))])
    # Hashed as given: ensure_ascii escapes the lone surrogates of invalid UTF-8 in procedural
    # ids, so two different damaged ids still digest differently. Only the digest leaves here.
    rows.sort(key=json.dumps)
    blob = json.dumps(rows, ensure_ascii=True).encode("ascii")
    return {"slots": len(rows), "capacity": capacity, "digest": hashlib.sha256(blob).hexdigest()[:12]}


def _raw(v: object) -> object:
    return v if isinstance(v, (int, float, str)) or v is None else repr(type(v))


@context_extractor("inventories", ("inventories",))
def inventories(ctx: CtxInput, out: dict) -> None:
    psd = ctx.psd
    inv = {
        "exosuit": digest([get(psd, n) for n in EXOSUIT]),
        "freighter": digest([get(psd, n) for n in FREIGHTER]),
        "storage": digest([get(psd, n) for n in STORAGE]),
        "vehicles": digest([get(v, n) for v in items(psd, "VehicleOwnership") for n in VEHICLE]),
    }
    for ship in ctx.ships:
        inv[f"ship:{ship.index}"] = digest([get(ship.node, n) for n in EXOSUIT])
    for tool in ctx.multitools:
        inv[f"multitool:{tool.index}"] = digest([get(tool.node, "Store")])
    out["inventories"] = inv
