"""Small curated game tables, applied at read time (except :mod:`.parts`, used at extraction).

Every lookup degrades to the raw id ("Galaxy 300", "Expedition #24", a model stem), so a game
update without regenerated tables shows ids, never errors. A table update changes what views
show on the next read; nothing is re-indexed.
"""
from __future__ import annotations

from collections.abc import Mapping

from . import _overrides, galaxies, journey, missions, seasons

LOOKUP_VERSION = "b25729564.1"


def galaxy_name(index: int | None) -> str | None:
    if index is None:
        return None
    if 0 <= index < len(galaxies.GALAXY_NAMES):
        return galaxies.GALAXY_NAMES[index]
    return f"Galaxy {index + 1}"


def galaxy_label(index: int | None) -> str | None:
    """The galaxy as the game numbers it, then its name: "1-Euclid", "10-Eissentam"."""
    if index is None:
        return None
    return f"{index + 1}-{galaxy_name(index)}"


def expedition_name(number: int | None, name_key: str | None = None, display: int | None = None) -> str:
    """The English name: by the save's own name key, else by season number, else a number."""
    if name_key and name_key in seasons.SEASON_NAME_KEYS:
        return seasons.SEASON_NAME_KEYS[name_key]
    if number in seasons.EXPEDITIONS:
        return seasons.EXPEDITIONS[number][1]
    return f"Expedition #{display or number}"


def expedition_number(number: int | None, display: int | None) -> int | None:
    """The number players know (DisplayNumber), from the save or the table."""
    if display:
        return display
    return seasons.EXPEDITIONS.get(number, (None, ""))[0]


def model_stem(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]


def ship_type(model_path: str) -> str:
    """Hauler, Fighter, Corvette...; a fixed reward model by its own table; else the stem."""
    stem = model_stem(model_path)
    if stem in _overrides.SPECIAL_SHIP_MODELS:
        return _overrides.SPECIAL_SHIP_MODELS[stem][0]
    path = "/" + model_path.replace("\\", "/").upper()
    for folder, kind in _overrides.SHIP_FOLDER_TYPES:
        if folder in path:
            return kind
    return stem


def ship_model_name(model_path: str) -> str | None:
    """The fixed name of a reward ship model ("The Wraith"), None for procedural ships."""
    return _overrides.SPECIAL_SHIP_MODELS.get(model_stem(model_path), (None, None))[1]


def is_corvette(model_path: str) -> bool:
    return ship_type(model_path) == "Corvette"


def ship_class_name(enum: str | None) -> str | None:
    if not enum:
        return None
    return _overrides.SHIP_CLASS_NAMES.get(enum, enum)


def journey_rank(points: float | None) -> str | None:
    if points is None or points < 0:
        return None
    ranks = journey.JOURNEY_RANKS
    return ranks[min(int(points) // journey.POINTS_PER_RANK, len(ranks) - 1)]


def creature_type(creature_id: str) -> str:
    return _overrides.CREATURE_TYPES.get(creature_id) or creature_id.replace("_", " ").title()


def base_type_label(base_type: str) -> str:
    return _overrides.BASE_TYPE_LABELS.get(base_type, base_type)


def final_stage(mission: str, mission_version: int | None) -> int | None:
    """The Progress that completes ``mission`` in a save of ``mission_version``: the last
    change point at or below it, else the newest (tables can lag the game by a version)."""
    points = missions.FINAL_STAGES.get(mission)
    if not points:
        return None
    if mission_version is not None:
        fitting = [p for v, p in points if v <= mission_version]
        if fitting:
            return fitting[-1]
    return points[-1][1]


def mission_state(mission: str, progress: Mapping[str, int], mission_version: int | None) -> tuple[str, int | None, int | None]:
    """("done" | "started" | "not started", stage reached, final stage)."""
    stage = progress.get(mission)
    if stage is None:
        return "not started", None, final_stage(mission, mission_version)
    final = final_stage(mission, mission_version)
    if stage == _overrides.DONE_SENTINEL or (final is not None and stage >= final):
        return "done", stage, final
    return "started", stage, final


def chain_status(chain: str, progress: Mapping[str, int], mission_version: int | None) -> tuple[int, int]:
    """(done, total) for one chain of :data:`_overrides.CHAINS`."""
    _label, ids = _overrides.CHAINS[chain]
    done = sum(1 for m in ids if mission_state(m, progress, mission_version)[0] == "done")
    return done, len(ids)


def chain_label(chain: str) -> str:
    return _overrides.CHAINS[chain][0]


def choices(progress: Mapping[str, int]) -> tuple[str, ...]:
    """The story choices made (a choice mission that was started)."""
    return tuple(label for mission, label in _overrides.CHOICES.items() if mission in progress)
