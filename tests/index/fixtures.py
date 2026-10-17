"""Synthetic saves for the golden-facts tests: every case of D4 5.3, in four saves.

All names and values are made up. Expected facts live beside them in ``golden/<name>.json``;
``NMSVAULT_REGEN_GOLDEN=1`` rewrites those files (review the diff before committing).
"""
from __future__ import annotations

from .savebuilder import context_save, pre_context_save

FIGHTER = "MODELS/COMMON/SPACECRAFT/FIGHTERS/FIGHTER_PROC.SCENE.MBIN"
CORVETTE = "MODELS/COMMON/SPACECRAFT/BIGGS/BIGGS.SCENE.MBIN"
FREIGHTER = "MODELS/COMMON/SPACECRAFT/INDUSTRIAL/CAPITALFREIGHTER.SCENE.MBIN"
STAFF = "MODELS/COMMON/WEAPONS/MULTITOOL/STAFF.SCENE.MBIN"
BAD_PRODUCT = b"^PROC_ITEM\xff#1".decode("utf-8", errors="surrogateescape")


def _obj(oid: str, ts: int = 1_790_000_000) -> dict:
    return {"ObjectID": "^" + oid, "Timestamp": ts, "UserData": 0, "Position": [0.0, 0.0, 0.0]}


def _base(btype: str, name: str, objects: list, address, position, **extra) -> dict:
    b = {"BaseType": {"PersistentBaseTypes": btype}, "Name": name, "GalacticAddress": address,
         "Position": position, "Objects": objects, "OriginalBaseVersion": 8,
         "Owner": {"UID": "SYNTH-UID-1", "USN": "synth_user", "LID": "", "PTK": "ST", "TS": 1}}
    b.update(extra)
    return b


def _inv(*slots, capacity: int = 10) -> dict:
    return {"ValidSlotIndices": [{"X": i, "Y": 0} for i in range(capacity)],
            "Slots": [{"Id": "^" + sid, "Amount": amount, "MaxAmount": 100, "Index": {"X": x, "Y": 0}}
                      for x, (sid, amount) in enumerate(slots)]}


def _stats(**values) -> list:
    stats = [{"Id": "^" + k, "Value": ({"FloatValue": v} if isinstance(v, float) else {"IntValue": v} if v else {})}
             for k, v in values.items()]
    return [{"GroupId": "^GLOBAL_STATS", "Stats": stats}]


def _psd(**extra) -> dict:
    psd = {
        "TimeStamp": 1_790_000_000, "UniverseAddress": {"RealityIndex": 9}, "SaveSummary": "On Planet (Synthia)",
        "Units": -1, "Nanites": 2500, "Specials": 1500, "KnownPortalRunes": 0xFFFF, "HasAccessToNexus": True,
        "BannerTitleId": "^T_SYNTH", "TeleportEndpoints": [{}, {}, {}], "KnownTech": ["^LAUNCHER", "^SCAN1"],
        "DifficultyState": {"Preset": {"DifficultyPresetType": "Normal"},
                            "EasiestUsedPreset": {"DifficultyPresetType": "Creative"},
                            "HardestUsedPreset": {"DifficultyPresetType": "Normal"},
                            "Settings": {"SettingsLocked": False, "CraftingIsFree": False,
                                         "DeathConsequences": {"DeathConsequencesDifficulty": "ItemGrave"}}},
        "KnownWordGroups": [{"Races": [True, True, True, False, True, False, False, False, True]},
                            {"Races": [True, False, False, False, False, False, False, False, False]}],
        "KnownProducts": ["^C_WALL", "^C_FLOOR", "^M_ROOF", "^S_CONTAINER0", "^GARAGE_FREIGHT", "^FUEL1", BAD_PRODUCT],
        "MissionVersion": 40, "CurrentMissionID": "^ACT1_STEP5",
        "MissionProgress": [{"Mission": "^ACT1_STEP1", "Progress": 6}, {"Mission": "^ACT1_STEP5", "Progress": 2},
                            {"Mission": "^ATLAS_PATH1", "Progress": 1}, {"Mission": "^TUTORIAL", "Progress": 9},
                            {"Mission": "^SENTINELS_1", "Progress": -1}, {"Mission": "^EPILOGUE_NEWGAL", "Progress": 0}],
        "Stats": _stats(TIME=7200.5, MONEY=1_000_000, DEATHS=3, DISC_SYSTEMS=12, ATLAS_PATH=0, CARGO_SPECIAL=4),
        "PrimaryShip": 0,
        "ShipOwnership": [
            {"Name": "<TITLE>Synth Runner</>", "Resource": {"Filename": FIGHTER, "Seed": [True, "0x0000000000ABCDEF"]},
             "Inventory": _inv(("ROCKET1", 5), capacity=8), "Inventory_TechOnly": _inv(("SHIPJUMP1", 1), capacity=4),
             "Inventory_Cargo": _inv(capacity=0)},
            {"Name": "", "Resource": {"Filename": ""}},
            {"Name": "Big Synth", "Resource": {"Filename": CORVETTE, "Seed": "0x0000000000000042"},
             "Inventory": _inv(("LAND1", 50))},
        ],
        "ArchivedShipOwnership": [{"ArchivedName": "Old Synth", "ArchivedClass": {"ShipClass": "Hauler"},
                                   "ArchivedInventoryClass": {"InventoryClass": "A"},
                                   "Ownership": {"Resource": {"Filename": FIGHTER}}}],
        "PersistentPlayerBases": [
            _base("HomePlanetBase", "<B1>Synth Home</>",
                  [_obj("BASE_FLAG"), _obj("C_WALL"), _obj("C_WALL"), _obj("M_FLOOR"), _obj("W_ROOF")],
                  "0x0000000900000123", [10.0, 20.0, 30.0]),
            _base("HomePlanetBase", "Synth Outpost", [_obj("BASE_FLAG"), _obj("CUBEROOM")],
                  0x0000000900000123, [99.0, 20.0, 30.0]),
            _base("HomePlanetBase", "Far Synth", [_obj("BASE_FLAG")], "0x0000001400000777", [1.0, 1.0, 1.0]),
            _base("FreighterBase", "Freighter", [_obj("S_CONTAINER0"), _obj("CORRIDOR_SPACE"), _obj("U_PARAGON")],
                  "0x0000000900000555", [0.0, 0.0, 0.0]),
            _base("PlayerShipBase", "Big Synth", [_obj("B_COCKPIT"), _obj("B_HAB")], 0, [0.0, 0.0, 0.0], UserData=2),
            _base("PlayerShipBase", "Orphan", [_obj("B_COCKPIT")], 0, [0.0, 0.0, 0.0], UserData=1),
            _base("ExternalPlanetBase", "Someone Else", [_obj("BASE_FLAG")], 5, [0.0, 0.0, 0.0]),
            _base("UITempShipBase", "Scratch", [], 0, [0.0, 0.0, 0.0]),
        ],
        "BaseBuildingObjects": [_obj("GARAGE_M"), _obj("SUMMON_GARAGE"), _obj("BASE_FLAG")],
        "Pets": [{"CustomName": "<PET1>Biscuit</>", "CreatureID": "^FLYINGLIZARD", "Biome": {"Biome": "Lush"},
                  "CreatureSeed": [True, "0x10"]},
                 {"CustomName": "", "CreatureID": "^"},
                 {"CustomName": "", "CreatureID": "^QUAD_PET", "Biome": {"Biome": "Toxic"}, "CreatureSeed": "0x11"}],
        "Eggs": [{"CreatureID": "^QUAD_PET"}, {"CreatureID": "^"}],
        "UnlockedPetSlots": [True, True, True, False],
        "CurrentFreighter": {"Filename": FREIGHTER}, "PlayerFreighterName": "Synth Hauler",
        "FreighterInventory": dict(_inv(("FUEL1", 100)), Class={"InventoryClass": "S"}),
        "FleetFrigates": [{}, {}],
        "Multitools": [{"Name": "Synth Staff", "Resource": {"Filename": STAFF},
                        "Store": dict(_inv(("LASER", 1)), Class={"InventoryClass": "S"})}],
        "SettlementHistory": [{"SeedValue": "0x00000000000000AA", "PlayerClaimedTime": 1_789_000_000.0},
                              {"SeedValue": 0, "PlayerClaimedTime": 0.0}],
        "SettlementStatesV2": [{"SeedValue": "0xAA", "Name": "Synth Haven"}, {"SeedValue": "0xBB", "Name": "Other"}],
        "Inventory": _inv(("FUEL1", 20), ("CARBON", 250), capacity=24),
        "Inventory_TechOnly": _inv(("LAUNCHER", 1), capacity=8),
        "Chest1Inventory": _inv(("FERRITE", 9999)),
        "VehicleOwnership": [{"Inventory": _inv(("FUEL1", 3)), "Inventory_TechOnly": _inv()}],
    }
    psd.update(extra)
    return psd


DISCOVERIES = [{"DD": {"DT": "SolarSystem"}}, {"DD": {"DT": "Planet"}}, {"DD": {"DT": "Planet"}},
               {"DD": {"DT": "Animal"}}]


def rich_base() -> tuple[dict, dict]:
    common = {"SaveName": "Synth Main", "TotalPlayTime": 360_000, "SaveUniversalId": "0x5EED00000000000A",
              "SeasonData": {"SeasonId": 0, "SeasonNumber": 1, "DisplayNumber": 0, "SeasonName": "^UI_SEASON_1_NAME"},
              "SeasonState": {"MilestoneValues": []}}
    return context_save(base=_psd(), common=common, discoveries=DISCOVERIES), {}


def _season_common(name: str) -> dict:
    return {"SaveName": name, "TotalPlayTime": 50_000, "SaveUniversalId": "0x5EED00000000000B",
            "SeasonData": {"SeasonId": 46, "SeasonNumber": 46, "DisplayNumber": 22, "SeasonName": "^UI_SEASON_22_NAME",
                           "Stages": [{"Milestones": [{"Amount": 1.0, "IsOptional": False},
                                                      {"Amount": 150.0, "IsOptional": False}]},
                                      {"Milestones": [{"Amount": 3.0, "IsOptional": True}]}]},
            "SeasonState": {"MilestoneValues": [1.0, 40.0, 3.0], "HasCollectedFinalReward": False},
            "EarnedSeasonSpecialRewards": ["^RS_SYNTH"]}


def base_and_expedition() -> tuple[dict, dict]:
    exp = _psd(Units=12_345, Specials=200, KnownPortalRunes=0b11, PersistentPlayerBases=[], ShipOwnership=[],
               UniverseAddress={"RealityIndex": 0}, MissionProgress=[], Pets=[], FleetFrigates=[])
    root = context_save(base=_psd(), expedition=exp, common=_season_common("Synth Expedition"), active="Season",
                        discoveries=DISCOVERIES)
    return root, {}


def expedition_only() -> tuple[dict, dict]:
    exp = _psd(PersistentPlayerBases=[], Stats=_stats(TIME=100.0))
    return context_save(expedition=exp, common=_season_common("Synth Solo"), active="Season"), {}


def pre_context() -> tuple[dict, dict]:
    psd = _psd(SaveName="Synth Old", TotalPlayTime=12_000, DifficultyState=None, KnownWordGroups=None)
    return pre_context_save(psd, discoveries=DISCOVERIES[:1]), {"unframed": True}


FIXTURES = {"rich_base": rich_base, "base_and_expedition": base_and_expedition,
            "expedition_only": expedition_only, "pre_context": pre_context}
