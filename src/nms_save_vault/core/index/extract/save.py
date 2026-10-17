"""Save-level facts (name, play time, expedition, discoveries) and each context's basics."""
from __future__ import annotations

from collections import Counter

from ..nodes import get, gid, hex64, integer, items, text, u32
from . import CtxInput, SaveInput, context_extractor, save_extractor


@save_extractor("save_basics", ("json_version", "active_context", "save_name", "play_time_s", "pid_in_data"))
def save_basics(save: SaveInput, out: dict) -> None:
    root = save.root
    out["json_version"] = integer(get(root, "Version"))
    active = get(root, "ActiveContext")
    out["active_context"] = active if isinstance(active, str) else None
    # Pre-context saves keep name and play time in PlayerStateData itself.
    src = save.common if save.common is not None else get(root, "PlayerStateData")
    out["save_name"] = text(get(src, "SaveName"))
    play = get(src, "TotalPlayTime")
    out["play_time_s"] = int(play) if isinstance(play, (int, float)) and not isinstance(play, bool) else None
    pid = get(save.common, "SaveUniversalId")
    out["pid_in_data"] = hex64(pid) if pid else None


@save_extractor("season", ("season",))
def season(save: SaveInput, out: dict) -> list[str] | None:
    data = get(save.common, "SeasonData")
    if not isinstance(data, dict):
        out["season"] = None
        return None
    state = get(save.common, "SeasonState")
    values = [v if _num(v) else 0 for v in items(state, "MilestoneValues")]
    # The targets are embedded: SeasonData.Stages[].Milestones[] align one to one with
    # SeasonState.MilestoneValues; done = value >= Amount.
    targets = [m for stage in items(data, "Stages") for m in items(stage, "Milestones")]
    notes = None
    if len(targets) == len(values):
        amounts = [(get(m, "Amount"), bool(get(m, "IsOptional"))) for m in targets]
        milestones = [[v, a if _num(a) else 1, opt] for v, (a, opt) in zip(values, amounts)]
    else:
        milestones = [[v, 1, False] for v in values]
        notes = ["season: milestone targets missing"]
    out["season"] = {
        "id": integer(get(data, "SeasonId")),
        "number": integer(get(data, "SeasonNumber")),
        "display": integer(get(data, "DisplayNumber")),
        "name_key": gid(get(data, "SeasonName")),
        "milestones": milestones,
        "final_reward": bool(get(state, "HasCollectedFinalReward")),
        "special_rewards": len(items(save.common, "EarnedSeasonSpecialRewards")),
    }
    return notes


def _num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@save_extractor("discoveries", ("discoveries",))
def discoveries(save: SaveInput, out: dict) -> None:
    # Counts only: the store is capped and evicts old records, so it is never diffed (N6).
    records = items(save.root, "DiscoveryManagerData", "DiscoveryData-v1", "Store", "Record")
    by_type = Counter(text(t, 64) for r in records if isinstance(t := get(r, "DD", "DT"), str))
    out["discoveries"] = {"total": len(records), "by_type": dict(sorted(by_type.items()))}


@context_extractor("ctx_basics", ("game_mode", "timestamp", "galaxy", "summary", "units", "nanites",
                                  "quicksilver", "glyphs", "nexus", "title", "teleporters", "known_tech"))
def ctx_basics(ctx: CtxInput, out: dict) -> None:
    psd = ctx.psd
    out["game_mode"] = integer(get(ctx.container, "GameMode"))
    out["timestamp"] = integer(get(psd, "TimeStamp"))
    out["galaxy"] = integer(get(psd, "UniverseAddress", "RealityIndex"))
    out["summary"] = text(get(psd, "SaveSummary"))
    out["units"] = u32(get(psd, "Units"))
    out["nanites"] = u32(get(psd, "Nanites"))
    out["quicksilver"] = u32(get(psd, "Specials"))
    runes = integer(get(psd, "KnownPortalRunes"))
    out["glyphs"] = bin(runes & 0xFFFF).count("1") if runes is not None else None
    out["nexus"] = bool(get(psd, "HasAccessToNexus"))
    out["title"] = gid(get(psd, "BannerTitleId"))
    out["teleporters"] = len(items(psd, "TeleportEndpoints"))
    out["known_tech"] = len(items(psd, "KnownTech"))


@context_extractor("difficulty", ("difficulty",))
def difficulty(ctx: CtxInput, out: dict) -> None:
    state = get(ctx.psd, "DifficultyState")

    def preset(name: str) -> str | None:
        v = get(state, name, "DifficultyPresetType")
        return v if isinstance(v, str) else None

    death = get(state, "Settings", "DeathConsequences", "DeathConsequencesDifficulty")
    free = get(state, "Settings", "CraftingIsFree")
    locked = get(state, "Settings", "SettingsLocked")
    out["difficulty"] = {
        "preset": preset("Preset"), "easiest": preset("EasiestUsedPreset"), "hardest": preset("HardestUsedPreset"),
        "death": death if isinstance(death, str) else None,
        "crafting_free": free if isinstance(free, bool) else None,
        "locked": locked if isinstance(locked, bool) else None,
    }
