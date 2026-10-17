"""The curated ``^GLOBAL_STATS`` of each context (D4 2.5.3), ids stored without ``^``."""
from __future__ import annotations

from ..nodes import get, gid, items, number
from . import CtxInput, context_extractor

# Counters that never decrease along a recorded parent step (R7 2.7, re-verified by D4).
CORE_COUNTERS = frozenset("""
AMMO_FIRED APP_SESSIONS COMM_09_DAY DISC_PLANETS DISC_RARE_SYS DISC_SYSTEMS DIST_FLY DIST_JETPACK
DIST_PULSE DIST_WALKED DIST_WARP ESEEN_SYSTEMS EXTREME_WALK ITEMS_TELEPRT LONGEST_LIFE MONEY_EVER
MP_FULL_COUNT MP_FULL_TIME MP_SESSIONS NANITES_EVER PARTS_PLACED PLAY_SESSIONS PORTAL_WARPS
RES_EXTRACTED SHIP_SUMMON STORM_WALK TIME TIMES_IN_SPACE TSEEN_SYSTEMS VISIT_PLANETS WSEEN_SYSTEMS
""".split())
DISPLAY_STATS = frozenset("""
WORDS_LEARNT DISC_CREATURES DISC_FLORA DISC_MINERALS DEATHS SENTINEL_KILLS TRA_STANDING WAR_STANDING
EXP_STANDING BUI_STANDING TGUILD_STAND WGUILD_STAND EGUILD_STAND PIRATE_STAND STATIONS_OWNED
PETS_ADOPTED SHIPS_BOUGHT FRIGATES EXPEDITIONS MONEY JM JM_BANKED ATLAS_PATH ATLAS_LOOPS HOME_REALITY
""".split())
KEPT = CORE_COUNTERS | DISPLAY_STATS


@context_extractor("stats", ("stats",))
def stats(ctx: CtxInput, out: dict) -> None:
    kept: dict[str, int | float] = {}
    for group in items(ctx.psd, "Stats"):
        if gid(get(group, "GroupId")) != "GLOBAL_STATS":
            continue
        for stat in items(group, "Stats"):
            sid = gid(get(stat, "Id"))
            if sid in KEPT:
                value = number(get(stat, "Value"))
                if value is not None:
                    kept[sid] = value
    out["stats"] = dict(sorted(kept.items()))
