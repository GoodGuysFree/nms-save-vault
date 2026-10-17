"""Lookup tables (``core/index/lookups``) and their generator ``tools/gen_lookups.py``.

The generator never runs against game data here: it is fed a tiny synthetic metadata index,
and the committed modules are checked to be exactly what it writes for their own data.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from nms_save_vault.core.index import lookups
from nms_save_vault.core.index.lookups import _overrides, galaxies, journey, missions, parts, seasons

TOOL = Path(__file__).resolve().parents[2] / "tools" / "gen_lookups.py"


@pytest.fixture(scope="module")
def gen():
    name = "gen_lookups_under_test"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


# --- the tables are consistent ---------------------------------------------------------------


def test_every_chain_mission_has_final_stages_and_tables_have_their_sizes():
    for _label, ids in _overrides.CHAINS.values():
        assert all(missions.FINAL_STAGES.get(m) for m in ids)
    assert len(_overrides.CHAINS) == 11
    assert len(galaxies.GALAXY_NAMES) == 256 and len(set(galaxies.GALAXY_NAMES)) == 256
    assert len(journey.JOURNEY_RANKS) == 12 and journey.POINTS_PER_RANK == 5
    assert (len(parts.LEGACY_STRUCTURES), len(parts.LEGACY_FREIGHTER)) == (69, 20)
    assert len(seasons.EXPEDITIONS) == 23 and seasons.EXPEDITIONS[46] == (22, "Swarm")
    assert len(_overrides.SHIP_CLASS_NAMES) == 11


def test_change_points_are_ordered_and_minimal():
    for points in missions.FINAL_STAGES.values():
        versions = [v for v, _p in points]
        assert versions == sorted(versions)
        assert all(a[1] != b[1] for a, b in zip(points, points[1:]))


# --- read-time helpers and their fallbacks ---------------------------------------------------


def test_galaxy_names_and_fallback():
    assert lookups.galaxy_name(0) == "Euclid" and lookups.galaxy_name(9) == "Eissentam"
    assert lookups.galaxy_name(300) == "Galaxy 301"
    assert lookups.galaxy_name(None) is None


def test_expedition_names_by_key_then_number_then_a_number():
    assert lookups.expedition_name(46, "UI_SEASON_22_NAME") == "Swarm"
    assert lookups.expedition_name(13, "UI_SEASON_5_NAME") == "Exobiology"       # a remix: not in the table
    assert lookups.expedition_name(46, "UI_UNKNOWN") == "Swarm"
    assert lookups.expedition_name(99, "UI_UNKNOWN", 24) == "Expedition #24"
    assert lookups.expedition_number(46, 0) == 22 and lookups.expedition_number(99, 0) is None


@pytest.mark.parametrize(("path", "kind", "name"), [
    ("MODELS/COMMON/SPACECRAFT/BIGGS/BIGGS.SCENE.MBIN", "Corvette", None),
    ("MODELS/COMMON/SPACECRAFT/DROPSHIPS/DROPSHIP_PROC.SCENE.MBIN", "Hauler", None),
    ("MODELS/COMMON/SPACECRAFT/S-CLASS/BIOPARTS/BIOSHIP_PROC.SCENE.MBIN", "Living ship", None),
    ("MODELS/COMMON/SPACECRAFT/S-CLASS/S-CLASS_PROC.SCENE.MBIN", "Exotic", None),
    ("MODELS/COMMON/SPACECRAFT/FIGHTERS/SPOOKSHIP.SCENE.MBIN", "Explorer", "Boundary Herald"),
    ("MODELS/COMMON/SPACECRAFT/FIGHTERS/WRACER.SCENE.MBIN", "Exotic", "Starborn Runner"),
    ("MODELS/COMMON/SPACECRAFT/NEWFOLDER/MYSTERY.SCENE.MBIN", "MYSTERY", None),
])
def test_ship_types_from_folder_or_reward_model(path, kind, name):
    assert lookups.ship_type(path) == kind
    assert lookups.ship_model_name(path) == name


def test_other_fallbacks():
    assert lookups.ship_class_name("Royal") == "Exotic" and lookups.ship_class_name("NewEnum") == "NewEnum"
    assert lookups.creature_type("FLYINGLIZARD") == "Flying lizard"
    assert lookups.creature_type("NEW_BEAST") == "New Beast"
    assert lookups.journey_rank(87) == "Galileo" and lookups.journey_rank(7) == "Pathfinder"
    assert lookups.journey_rank(None) is None and lookups.journey_rank(-1) is None
    assert lookups.base_type_label("HomePlanetBase") == "Planet base"
    assert lookups.base_type_label("NewBaseType") == "NewBaseType"


def test_final_stage_follows_the_save_mission_version():
    assert missions.FINAL_STAGES["ACT1_STEP13"] == ((1, 27), (10, 46), (11, 49), (15, 50), (22, 54))
    assert lookups.final_stage("ACT1_STEP13", 9) == 27
    assert lookups.final_stage("ACT1_STEP13", 10) == 46
    assert lookups.final_stage("ACT1_STEP13", 40) == 54        # tables end below current saves
    assert lookups.final_stage("ACT1_STEP13", None) == 54
    assert lookups.final_stage("ACT1_STEP13", 0) == 54         # below every point: the newest
    assert lookups.final_stage("NOT_A_MISSION", 40) is None


def test_mission_states():
    progress = {"ACT1_STEP13": 54, "ACT1_STEP12": 3, "ATLAS1": 2147483647, "MYSTERY": 4}
    assert lookups.mission_state("ACT1_STEP13", progress, 40) == ("done", 54, 54)
    assert lookups.mission_state("ACT1_STEP12", progress, 40) == ("started", 3, 7)
    assert lookups.mission_state("ATLAS1", progress, 40)[0] == "done"
    assert lookups.mission_state("ACT1_STEP1", progress, 40) == ("not started", None, 7)
    assert lookups.mission_state("MYSTERY", progress, 40) == ("started", 4, None)


def test_chains_reproduce_the_r6_counts_on_a_matching_progress():
    """R6 2.1: Artemis 30/30, Atlas 11/11, base computer 12/21 on a finished playthrough."""
    def finished(ids):
        return {m: lookups.final_stage(m, 40) for m in ids}

    progress = finished(_overrides.CHAINS["artemis"][1]) | finished(_overrides.CHAINS["atlas"][1])
    progress |= finished(_overrides.CHAINS["base_computer"][1][:12])
    progress["BASE_UPGRADE13"] = 0
    assert lookups.chain_status("artemis", progress, 40) == (30, 30)
    assert lookups.chain_status("atlas", progress, 40) == (11, 11)
    assert lookups.chain_status("base_computer", progress, 40) == (12, 21)
    assert lookups.chain_status("titan", progress, 40) == (0, 4)
    assert lookups.choices({"EPILOGUE_STAY": 0, "ART_SIM": 1}) == ("stayed", "Artemis simulated")


# --- the generator ---------------------------------------------------------------------------


def test_the_committed_modules_are_exactly_generator_output(gen):
    assert gen.check() == []


def test_the_generator_refuses_non_ascii_names(gen):
    tables = gen.tables_from_modules()
    tables.galaxies[3] = "Hesperius Dimensión"
    with pytest.raises(ValueError, match="non-ASCII"):
        gen.render(tables)
    tables = gen.tables_from_modules()
    tables.galaxies.pop()
    with pytest.raises(ValueError, match="256"):
        gen.render(tables)


def _synthetic_dump(path: Path) -> None:
    rows = []

    def add(file, entry, path_, value):
        rows.append((file, entry, path_, value))

    add(gen_parts := "metadata/reality/tables/basebuildingobjectstable.mbin", "C_WALL", "Objects.Groups.Groups.Group", "BASIC_LEGACY")
    add(gen_parts, "C_WALL", "Objects.Groups.Groups.Group", "PLANET_TECH")
    add(gen_parts, "S_CONTAINER0", "Objects.Groups.Groups.Group", "FREIGHT_LEGACY")
    add(gen_parts, "BASE_FLAG", "Objects.Groups.Groups.Group", "PLANET_TECH")
    core = "metadata/simulation/missions/tables/coremissiontable.mbin"
    for version, progress in ((1, 27), (3, 27), (10, 46), (11, 49), (12, 49)):
        add(core, "ACT1_STEP13", "Missions.FinalStageVersions.FinalStageVersions.Version", str(version))
        add(core, "ACT1_STEP13", "Missions.FinalStageVersions.FinalStageVersions.Progress", str(progress))
    add("metadata/simulation/missions/tables/deprecatedmissiontable.mbin", "ATLAS1",
        "Missions.FinalStageVersions.FinalStageVersions.Version", "1")
    add("metadata/simulation/missions/tables/deprecatedmissiontable.mbin", "ATLAS1",
        "Missions.FinalStageVersions.FinalStageVersions.Progress", "99")
    seasons_table = "metadata/reality/tables/historicalseasondatatable.mbin"
    for key, number, display in (("UI_SEASON_1_NAME", "1", "1"), ("UI_SEASON_22_NAME", "46", "22")):
        add(seasons_table, "", "Table.Table.SeasonName", key)
        add(seasons_table, "", "Table.Table.SeasonNumber", number)
        add(seasons_table, "", "Table.Table.DisplayNumber", display)
    j = "metadata/gamestate/stats/journeymilestonetable.mbin"
    for i, title in enumerate(("APOLLO", "PATHFINDER")):
        add(j, "", "JourneyMilestoneTable.JourneyMilestoneTable.JourneyMilestoneId", f"JOURNEY0{i}")
        add(j, "", "JourneyMilestoneTable.JourneyMilestoneTable.PointsToUnlock", str(i * 5))
        add(j, "", "JourneyMilestoneTable.JourneyMilestoneTable.JourneyMilestoneTitleLower", f"JM_0{i}_L")
        add("language/nms_loc1_english.mbin", f"JM_0{i}_L", "Table.English", title.title())
    add("language/nms_loc9_english.mbin", "UI_SEASON_22_NAME", "Table.English", "Swarm")
    add("language/nms_loc9_english.mbin", "UI_SEASON_1_NAME", "Table.English", "The Pioneers")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE props (id INTEGER PRIMARY KEY, file TEXT, template TEXT, entry TEXT, path TEXT, "
                "name TEXT, value TEXT)")
    con.executemany("INSERT INTO props (file, entry, path, name, value) VALUES (?, ?, ?, '', ?)", rows)
    con.commit()
    con.close()


def test_read_dump_on_a_synthetic_index(gen, tmp_path):
    db = tmp_path / "meta.sqlite"
    _synthetic_dump(db)
    t = gen.read_dump(db, "1234", ["G"] * 256, "2026-01-01")
    assert (t.legacy_structures, t.legacy_freighter) == (["C_WALL"], ["S_CONTAINER0"])
    assert t.final_stages == {"ACT1_STEP13": [(1, 27), (10, 46), (11, 49)]}     # deprecated table skipped
    assert t.expeditions == {1: (1, "The Pioneers"), 46: (22, "Swarm")}
    assert t.season_names == {"UI_SEASON_1_NAME": "The Pioneers", "UI_SEASON_22_NAME": "Swarm"}
    assert (t.journey_ranks, t.points_per_rank) == (["Apollo", "Pathfinder"], 5)
    files = gen.render(t)
    assert set(files) == set(gen.GENERATED)
    assert "game build 1234 on 2026-01-01" in files["missions.py"]
    namespace: dict = {}
    exec(compile(files["missions.py"], "missions.py", "exec"), namespace)
    assert namespace["FINAL_STAGES"] == {"ACT1_STEP13": ((1, 27), (10, 46), (11, 49))}
