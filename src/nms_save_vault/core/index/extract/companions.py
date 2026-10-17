"""Companions, eggs and unlocked companion slots. Gifted eggs carry the sender's platform ids
(``SenderData``); that block is never read."""
from __future__ import annotations

from ..nodes import get, gid, items, seed, text
from . import CtxInput, context_extractor


@context_extractor("companions", ("companions", "eggs", "pet_slots"))
def companions(ctx: CtxInput, out: dict) -> None:
    pets = []
    for slot, pet in enumerate(items(ctx.psd, "Pets")):
        creature = gid(get(pet, "CreatureID"))
        if not creature:
            continue                      # "^": an empty slot
        biome = get(pet, "Biome", "Biome")
        pets.append({"slot": slot, "name": text(get(pet, "CustomName")), "creature": creature,
                     "biome": biome if isinstance(biome, str) else None,
                     "seed": seed(get(pet, "CreatureSeed"))})
    out["companions"] = pets
    out["eggs"] = sum(1 for e in items(ctx.psd, "Eggs") if gid(get(e, "CreatureID")))
    out["pet_slots"] = sum(1 for unlocked in items(ctx.psd, "UnlockedPetSlots") if unlocked is True)
