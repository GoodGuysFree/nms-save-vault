"""Read-time views over raw facts: the flat :class:`FactsView` row and the :class:`FactsCard`.

Facts hold ids and counts; everything that needs a game table (names, milestone completion,
ship types, journey rank) is computed here, on read, from the current lookups - so a table
update never forces a re-index. Both views are frozen and JSON-safe through ``to_dict()``.
All text built here is ASCII; names from the save (bases, ships, companions) are kept as typed.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from . import lookups
from .lookups import _overrides
from .schema import EXTRACTOR_VERSION

STORY_CHAIN = "artemis"
ATLAS_CHAIN = "atlas"


def _ascii(s: str) -> str:
    return s.encode("ascii", "replace").decode("ascii")


@dataclass(frozen=True)
class FactsView:
    vid: str
    status: str
    stale: bool
    primary: str | None
    active: str | None
    save_name_in_data: str
    pid_in_data: str | None
    story: tuple[int, int] | None
    story_ending: str | None
    atlas: tuple[int, int] | None
    chains: Mapping[str, tuple[int, int]]
    glyphs: int | None
    own_bases: int
    base_parts: int
    planet_bases: int
    planet_base_parts: int
    largest_base: tuple[str, int] | None
    galaxies_with_bases: tuple[tuple[str, int], ...]
    legacy_placed: int
    legacy_placed_bases: int
    legacy_buildable: tuple[int, int]
    ships: int
    corvettes: int
    archived_ships: int
    ship_names: tuple[str, ...]
    companions: int
    companion_names: tuple[str, ...]
    eggs: int
    expedition: Mapping[str, object] | None
    current_galaxy: str | None
    journey_rank: str | None
    quicksilver: int | None
    nanites: int | None
    units: int | None
    freighter: str | None
    frigates: int
    words: int
    deaths: int | None
    systems_discovered: int | None
    last_played: int | None
    numbers: Mapping[str, float] = field(default_factory=dict)
    chips: tuple[str, ...] = ()
    lookups: str = lookups.LOOKUP_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))


@dataclass(frozen=True)
class FactsCard:
    vid: str
    status: str
    stale: bool
    primary: str | None
    active: str | None
    save: Mapping[str, object]
    expedition: Mapping[str, object] | None
    contexts: Mapping[str, Mapping[str, object]]
    errors: tuple[Mapping[str, str], ...]
    deep: Mapping[str, object] | None = None      # on-demand detail (deep.DeepFacts), when decoded
    lookups: str = lookups.LOOKUP_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


# --- helpers over raw context facts ------------------------------------------------------


def _ctx(facts: Mapping, name: str | None) -> Mapping:
    return facts.get("contexts", {}).get(name, {}) if name else {}


def _active(facts: Mapping) -> str | None:
    active = (facts.get("save") or {}).get("active_context")
    if active == "Season" and "expedition" in facts.get("contexts", {}):
        return "expedition"
    if active == "Main" and "base" in facts.get("contexts", {}):
        return "base"
    return facts.get("primary")


def _missions(ctx: Mapping) -> tuple[Mapping[str, int], int | None]:
    m = ctx.get("missions") or {}
    return m.get("progress") or {}, m.get("version")


def ship_display(ship: Mapping) -> str:
    """The ship's own name, else its fixed reward name, else its type."""
    model = ship.get("model_path", "")
    return ship.get("name") or lookups.ship_model_name(model) or lookups.ship_type(model)


def companion_display(pet: Mapping) -> str:
    return pet.get("name") or lookups.creature_type(pet.get("creature", ""))


def expedition_of(facts: Mapping) -> dict[str, object] | None:
    season = (facts.get("save") or {}).get("season")
    if not season or not season.get("id"):
        return None                              # SeasonId 0: no expedition
    milestones = season.get("milestones") or []
    return {
        "name": lookups.expedition_name(season.get("number"), season.get("name_key"), season.get("display")),
        "number": lookups.expedition_number(season.get("number"), season.get("display")),
        "done": sum(1 for v, a, _opt in milestones if v >= a),
        "total": len(milestones),
        "final_reward": bool(season.get("final_reward")),
        "active": _active(facts) == "expedition",
    }


def view(vid: str, facts: Mapping) -> FactsView:
    """The flat row for lists and ``facts:<name>`` sorting, from the primary context."""
    primary = facts.get("primary")
    ctx = _ctx(facts, primary)
    save = facts.get("save") or {}
    progress, mission_version = _missions(ctx)
    chains = {k: lookups.chain_status(k, progress, mission_version) for k in _overrides.CHAINS} if ctx else {}
    story = chains.get(STORY_CHAIN)
    atlas = chains.get(ATLAS_CHAIN)
    ending = next((label for m, label in (("EPILOGUE_NEWGAL", "new galaxy"), ("EPILOGUE_STAY", "stayed"))
                   if m in progress), None)

    bases = (ctx.get("bases") or {}).get("own") or []
    planet = [b for b in bases if b.get("type") == "HomePlanetBase"]
    largest = max(bases, key=lambda b: b.get("parts", 0), default=None)
    per_galaxy = Counter(b.get("galaxy") for b in planet if b.get("galaxy") is not None)
    galaxies = tuple((lookups.galaxy_label(g) or "", n)
                     for g, n in sorted(per_galaxy.items(), key=lambda kv: (-kv[1], kv[0])))
    legacy = [b.get("legacy_structures", 0) + b.get("legacy_freighter", 0) for b in bases]
    known = ctx.get("legacy_known") or {}

    ships = ctx.get("ships") or []
    ordered = sorted(ships, key=lambda s: (not s.get("primary"), s.get("slot", 0)))
    pets = ctx.get("companions") or []
    stats = ctx.get("stats") or {}
    freighter = ctx.get("freighter") or {}
    words = ctx.get("words") or {}

    fields: dict[str, Any] = dict(
        vid=vid, status=facts.get("status", "error"), stale=facts.get("extractor", 0) < EXTRACTOR_VERSION,
        primary=primary, active=_active(facts),
        save_name_in_data=save.get("save_name", ""), pid_in_data=save.get("pid_in_data"),
        story=story, story_ending=ending, atlas=atlas, chains=chains,
        glyphs=ctx.get("glyphs"),
        own_bases=len(bases), base_parts=sum(b.get("parts", 0) for b in bases),
        planet_bases=len(planet), planet_base_parts=sum(b.get("parts", 0) for b in planet),
        largest_base=(largest.get("name", ""), largest.get("parts", 0)) if largest else None,
        galaxies_with_bases=galaxies,
        legacy_placed=sum(legacy), legacy_placed_bases=sum(1 for n in legacy if n),
        legacy_buildable=(known.get("structures", 0), known.get("freighter", 0)),
        ships=len(ships), corvettes=sum(1 for s in ships if lookups.is_corvette(s.get("model_path", ""))),
        archived_ships=len(ctx.get("archived_ships") or []),
        ship_names=tuple(ship_display(s) for s in ordered),
        companions=len(pets), companion_names=tuple(companion_display(p) for p in pets),
        eggs=ctx.get("eggs", 0),
        expedition=expedition_of(facts),
        current_galaxy=lookups.galaxy_label(ctx.get("galaxy")),
        journey_rank=lookups.journey_rank(stats.get("JM")),
        quicksilver=ctx.get("quicksilver"), nanites=ctx.get("nanites"), units=ctx.get("units"),
        freighter=(f"{freighter.get('name') or freighter.get('model')}"
                   + (f" ({freighter['cls']})" if freighter.get("cls") else "")
                   if freighter.get("has") else None),
        frigates=ctx.get("frigates", 0), words=sum(words.values()),
        deaths=_int(stats.get("DEATHS")), systems_discovered=_int(stats.get("DISC_SYSTEMS")),
        last_played=ctx.get("timestamp"),
    )
    return FactsView(**fields, numbers=_numbers(fields), chips=_chips(fields))


def _int(v: object) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# Fields whose value is a pair or a mapping: the number they sort by.
def _pair_number(name: str, value: object) -> float | None:
    if name in ("story", "atlas", "largest_base") and isinstance(value, tuple):
        return value[0] if name != "largest_base" else value[1]
    if name == "legacy_buildable" and isinstance(value, tuple):
        return value[0] + value[1]
    if name == "galaxies_with_bases" and isinstance(value, tuple):
        return len(value)
    if name == "expedition" and isinstance(value, Mapping):
        return value.get("done")
    return None


NUMBER_FIELDS = (
    "story", "atlas", "glyphs", "own_bases", "base_parts", "planet_bases", "planet_base_parts", "largest_base",
    "galaxies_with_bases", "legacy_placed", "legacy_placed_bases", "legacy_buildable", "ships", "corvettes",
    "archived_ships", "companions", "eggs", "expedition", "quicksilver", "nanites", "units", "frigates", "words",
    "deaths", "systems_discovered", "last_played",
)


def _numbers(f: Mapping[str, Any]) -> dict[str, float]:
    """Every numeric field by its name, for ``facts:<name>`` sorting."""
    numbers: dict[str, float] = {}
    for name in NUMBER_FIELDS:
        value = f[name]
        n = value if isinstance(value, (int, float)) and not isinstance(value, bool) else _pair_number(name, value)
        if n is not None:
            numbers[name] = n
    return numbers


def _chips(f: Mapping[str, Any]) -> tuple[str, ...]:
    """Short ASCII milestone badges for list rows (owner decision O8)."""
    out: list[str] = []
    story, atlas = f["story"], f["atlas"]
    if story and story[0]:
        out.append(f"Story {story[0]}/{story[1]}")
    if atlas and atlas[0]:
        out.append(f"Atlas {atlas[0]}/{atlas[1]}")
    for chain, label in (("sentinel", "Sentinel"), ("autophage", "Autophage")):
        done, total = f["chains"].get(chain, (0, 0))
        if total and done == total:
            out.append(label)
    e = f["expedition"]
    if e:
        out.append(f"Exp {e['number'] or '?'} {e['name']} {e['done']}/{e['total']}")
    if f["glyphs"]:
        out.append(f"Glyphs {f['glyphs']}")
    if f["legacy_placed"]:
        out.append("Legacy parts")
    if any(f["legacy_buildable"]):
        out.append("Legacy buildable")
    return tuple(_ascii(c) for c in out)


def fleet_text(v: FactsView) -> str:
    """The owner's ship cell (O3): "12 (3 corvettes) +5 archived"."""
    text = str(v.ships)
    if v.corvettes:
        text += f" ({v.corvettes} corvette{'s' if v.corvettes != 1 else ''})"
    if v.archived_ships:
        text += f" +{v.archived_ships} archived"
    return text


# --- the card ---------------------------------------------------------------------------------


def card(vid: str, facts: Mapping, deep: Mapping | None = None) -> FactsCard:
    save = facts.get("save") or {}
    contexts = {name: _context_card(ctx) for name, ctx in (facts.get("contexts") or {}).items()}
    season = save.get("season") or {}
    expedition = expedition_of(facts)
    if expedition is not None:
        expedition = dict(expedition, milestones=[
            {"value": v, "amount": a, "optional": o, "done": v >= a} for v, a, o in season.get("milestones") or []])
    return FactsCard(
        vid=vid, status=facts.get("status", "error"), stale=facts.get("extractor", 0) < EXTRACTOR_VERSION,
        primary=facts.get("primary"), active=_active(facts),
        save={"name": save.get("save_name", ""), "play_time_s": save.get("play_time_s"),
              "layout": save.get("layout"), "discoveries": save.get("discoveries") or {}},
        expedition=expedition, contexts=contexts,
        errors=tuple(facts.get("errors") or ()),
        deep=deep,
    )


def _context_card(ctx: Mapping) -> dict[str, object]:
    progress, version = _missions(ctx)
    bases = (ctx.get("bases") or {}).get("own") or []
    stats = ctx.get("stats") or {}
    return {
        "bases": [{"name": b.get("name", ""), "type": lookups.base_type_label(b.get("type", "")),
                   "galaxy": lookups.galaxy_label(b.get("galaxy")), "parts": b.get("parts", 0),
                   "legacy_structures": b.get("legacy_structures", 0),
                   "legacy_freighter": b.get("legacy_freighter", 0),
                   "older_format": (b.get("orig_version") or 8) < 8} for b in bases],
        "planet_base_parts": sum(b.get("parts", 0) for b in bases if b.get("type") == "HomePlanetBase"),
        "ships": [{"name": ship_display(s), "type": lookups.ship_type(s.get("model_path", "")),
                   "class": s.get("cls"), "primary": bool(s.get("primary"))} for s in ctx.get("ships") or []],
        "archived_ships": [{"name": a.get("name", ""), "type": lookups.ship_class_name(a.get("ship_class")),
                            "class": a.get("inv_class")} for a in ctx.get("archived_ships") or []],
        "companions": [{"name": companion_display(p), "creature": lookups.creature_type(p.get("creature", "")),
                        "named": bool(p.get("name"))} for p in ctx.get("companions") or []],
        "eggs": ctx.get("eggs", 0),
        "milestones": {
            chain: {"label": lookups.chain_label(chain),
                    "steps": [_step(m, progress, version) for m in _overrides.CHAINS[chain][1]]}
            for chain in _overrides.CHAINS
        },
        "choices": list(lookups.choices(progress)),
        "difficulty": ctx.get("difficulty") or {},
        "freighter": ctx.get("freighter") or {"has": False},
        "frigates": ctx.get("frigates", 0),
        "multitools": ctx.get("multitools") or [],
        "settlements": ctx.get("settlements") or {"claimed": 0, "names": []},
        "exocraft_bays": ctx.get("exocraft_bays") or [],
        "words": ctx.get("words") or {},
        "stats": {STAT_LABELS[k]: stats[k] for k in STAT_LABELS if k in stats},
        "journey_rank": lookups.journey_rank(stats.get("JM")),
        "galaxy": lookups.galaxy_label(ctx.get("galaxy")),
        "inventories": {k: {"slots": i.get("slots", 0), "capacity": i.get("capacity", 0)}
                        for k, i in (ctx.get("inventories") or {}).items()},
    }


def _step(mission: str, progress: Mapping[str, int], version: int | None) -> dict[str, object]:
    state, stage, final = lookups.mission_state(mission, progress, version)
    text = state if state != "started" else (f"stage {stage} of {final}" if final is not None else f"stage {stage}")
    return {"mission": mission, "state": state, "text": text}


STAT_LABELS = {
    "DISC_SYSTEMS": "Systems discovered", "DISC_PLANETS": "Planets discovered", "DISC_CREATURES": "Creatures discovered",
    "DISC_FLORA": "Flora discovered", "DISC_MINERALS": "Minerals discovered", "WORDS_LEARNT": "Words learnt",
    "DEATHS": "Deaths", "SENTINEL_KILLS": "Sentinels destroyed", "PORTAL_WARPS": "Portal warps",
    "DIST_WARP": "Warps", "SHIPS_BOUGHT": "Ships bought", "PETS_ADOPTED": "Companions adopted",
    "STATIONS_OWNED": "Stations owned", "EXPEDITIONS": "Expeditions", "TRA_STANDING": "Gek standing",
    "WAR_STANDING": "Vy'keen standing", "EXP_STANDING": "Korvax standing", "BUI_STANDING": "Autophage standing",
    "PIRATE_STAND": "Outlaw standing", "ATLAS_LOOPS": "Atlas resets", "MONEY_EVER": "Units earned",
    "NANITES_EVER": "Nanites earned", "TIME": "Time played (s)",
}
