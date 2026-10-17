"""Ships (owned and archived), the freighter, frigates and multi-tools."""
from __future__ import annotations

from ..lookups import model_stem
from ..nodes import get, integer, items, label, seed, text
from . import MODEL_PATH_LIMIT, CtxInput, context_extractor


@context_extractor("ships", ("ships", "archived_ships"))
def ships(ctx: CtxInput, out: dict) -> None:
    primary = integer(get(ctx.psd, "PrimaryShip"))
    out["ships"] = [
        {"slot": s.index, "name": text(get(s.node, "Name")), "model_path": s.model,
         "cls": label(get(s.node, "Inventory", "Class", "InventoryClass")),
         "seed": seed(get(s.node, "Resource", "Seed")), "primary": s.index == primary}
        for s in ctx.ships
    ]
    out["archived_ships"] = [
        {"name": text(get(a, "ArchivedName")),
         "ship_class": label(get(a, "ArchivedClass", "ShipClass")),
         "inv_class": label(get(a, "ArchivedInventoryClass", "InventoryClass"))}
        for a in items(ctx.psd, "ArchivedShipOwnership")
        if text(get(a, "Ownership", "Resource", "Filename"), MODEL_PATH_LIMIT)
    ]


@context_extractor("fleet", ("freighter", "frigates", "multitools"))
def fleet(ctx: CtxInput, out: dict) -> None:
    psd = ctx.psd
    freighter_model = text(get(psd, "CurrentFreighter", "Filename"), MODEL_PATH_LIMIT)
    out["freighter"] = {
        "has": bool(freighter_model),
        "name": text(get(psd, "PlayerFreighterName")) if freighter_model else "",
        "model": model_stem(freighter_model) if freighter_model else "",
        "cls": label(get(psd, "FreighterInventory", "Class", "InventoryClass")) if freighter_model else None,
    }
    out["frigates"] = len(items(psd, "FleetFrigates"))
    out["multitools"] = [
        {"name": text(get(m.node, "Name")), "model": model_stem(m.model),
         "cls": label(get(m.node, "Store", "Class", "InventoryClass"))}
        for m in ctx.multitools
    ]
