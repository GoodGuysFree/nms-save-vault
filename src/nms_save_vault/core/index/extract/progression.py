"""Progression facts per context: mission progress, words learnt, known products (legacy
parts still buildable)."""
from __future__ import annotations

from ..lookups.parts import LEGACY_FREIGHTER, LEGACY_STRUCTURES
from ..nodes import get, gid, integer, items
from . import CtxInput, context_extractor

# Mission chains whose raw progress is kept; completion is judged at read time against the
# lookup tables, so new steps of a known chain need no extractor change (a new prefix does).
MISSION_PREFIXES = (
    "ACT1_STEP", "ACT2_STEP", "ACT3_STEP", "ATLAS", "SENTINELS_", "ROBOMISS_", "WORLDSM", "PURPM",
    "PIRATES", "WATERSTORY", "BIO_SHIP", "OVERSEER", "BASE_UPGRADE", "EPILOGUE_", "ART_", "APOLLO_DEAD",
    "DID_", "A3_CHOICE", "ENABLE_NEXUS", "SETTLEMENT_PRE",
)


@context_extractor("missions", ("missions",))
def missions(ctx: CtxInput, out: dict) -> None:
    progress: dict[str, int] = {}
    for entry in items(ctx.psd, "MissionProgress"):
        mission = gid(get(entry, "Mission"))
        value = integer(get(entry, "Progress"))
        if mission.startswith(MISSION_PREFIXES) and value is not None and value != -1:
            progress[mission] = value
    out["missions"] = {"version": integer(get(ctx.psd, "MissionVersion")),
                       "current": gid(get(ctx.psd, "CurrentMissionID")),
                       "progress": dict(sorted(progress.items()))}

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
