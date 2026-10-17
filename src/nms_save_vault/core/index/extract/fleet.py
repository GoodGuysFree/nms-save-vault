"""Ships (owned and archived), the freighter, frigates and multi-tools."""
from __future__ import annotations

from ..nodes import get, integer, items, seed, text
from . import CtxInput, context_extractor

MODEL_PATH_LIMIT = 256


def _class(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def model_stem(path: str) -> str:
    """``MODELS/COMMON/SPACECRAFT/BIGGS/BIGGS.SCENE.MBIN`` -> ``BIGGS``."""
    return path.replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]


@context_extractor("ships", ("ships", "archived_ships"))
def ships(ctx: CtxInput, out: dict) -> None:
    primary = integer(get(ctx.psd, "PrimaryShip"))
    owned = []
    for slot, ship in enumerate(items(ctx.psd, "ShipOwnership")):
        model = text(get(ship, "Resource", "Filename"), MODEL_PATH_LIMIT)
        if not model:
            continue                      # an empty fleet slot
        owned.append({"slot": slot, "name": text(get(ship, "Name")), "model_path": model,
                      "cls": _class(get(ship, "Inventory", "Class", "InventoryClass")),
                      "seed": seed(get(ship, "Resource", "Seed")), "primary": slot == primary})
    out["ships"] = owned
    out["archived_ships"] = [
        {"name": text(get(a, "ArchivedName")),
         "ship_class": _class(get(a, "ArchivedClass", "ShipClass")),
         "inv_class": _class(get(a, "ArchivedInventoryClass", "InventoryClass"))}
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
        "cls": _class(get(psd, "FreighterInventory", "Class", "InventoryClass")) if freighter_model else None,
    }
    out["frigates"] = len(items(psd, "FleetFrigates"))
    out["multitools"] = [
        {"name": text(get(m, "Name")), "model": model_stem(model),
         "cls": _class(get(m, "Store", "Class", "InventoryClass"))}
        for m in items(psd, "Multitools")
        if (model := text(get(m, "Resource", "Filename"), MODEL_PATH_LIMIT))
    ]
