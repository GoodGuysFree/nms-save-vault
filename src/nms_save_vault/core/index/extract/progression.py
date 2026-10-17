"""Progression facts per context: words learnt, known products (legacy parts still buildable)."""
from __future__ import annotations

from ..lookups.parts import LEGACY_FREIGHTER, LEGACY_STRUCTURES
from ..nodes import get, gid, items
from . import CtxInput, context_extractor

# KnownWordGroups[].Races is indexed by the game's race enum; the facts keep the five that
# have words to learn (0 Gek, 1 Vy'keen, 2 Korvax, 4 Atlas, 8 Autophage).
WORD_RACES = {0: "gek", 1: "vykeen", 2: "korvax", 4: "atlas", 8: "autophage"}


@context_extractor("words", ("words",))
def words(ctx: CtxInput, out: dict) -> None:
    per = dict.fromkeys(WORD_RACES.values(), 0)
    for group in items(ctx.psd, "KnownWordGroups"):
        races = get(group, "Races")
        if not isinstance(races, list):
            continue
        for index, race in WORD_RACES.items():
            if index < len(races) and races[index] is True:
                per[race] += 1
    out["words"] = per


@context_extractor("products", ("known_products", "legacy_known"))
def products(ctx: CtxInput, out: dict) -> None:
    known = {gid(p) for p in items(ctx.psd, "KnownProducts") if isinstance(p, str)}
    out["known_products"] = len(known)
    out["legacy_known"] = {"structures": len(known & LEGACY_STRUCTURES),
                           "freighter": len(known & LEGACY_FREIGHTER)}
