"""core.index.assets: every asset of a save with the key that tells it apart in any version or
save (Savecraft R2), the path an edit names it by, and which one is in use."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from nms_save_vault.core import api, savedir, saveedit, savejson, savepatch
from nms_save_vault.core.index.assets import KINDS, Asset, assets_of, portal_address, state_of
from nms_save_vault.core.testing import obfuscated as obf

SHIP = "MODELS/COMMON/SPACECRAFT/FIGHTERS/FIGHTER_PROC.SCENE.MBIN"
CORVETTE = "MODELS/COMMON/SPACECRAFT/BIGGS/BIGGS.SCENE.MBIN"
TOOL = "MODELS/COMMON/WEAPONS/MULTITOOL/MULTITOOL.SCENE.MBIN"
FREIGHTER = "MODELS/COMMON/SPACECRAFT/INDUSTRIAL/FREIGHTERS/FREIGHTER_PROC.SCENE.MBIN"
MADE = 1_760_000_000                       # a corvette's seed is the second it was made


def inv(cls="A", slots=()):
    return {"Class": {"InventoryClass": cls}, "Slots": list(slots)}


def seed(n):
    return [True, f"0x{n:016X}"]


PSD = {
    "PrimaryShip": 2, "ActiveMultioolIndex": 0, "PrimaryVehicle": 1,
    "ShipOwnership": [
        {"Name": "Swift", "Resource": {"Filename": SHIP, "Seed": seed(0x51)}, "Inventory": inv("S")},
        {"Name": "", "Resource": {"Filename": "", "Seed": seed(0)}, "Inventory": inv()},   # empty
        {"Name": "Hall", "Resource": {"Filename": CORVETTE, "Seed": seed(MADE)},
         "Inventory": inv("C")},
    ],
    "Multitools": [
        {"Name": "Spark", "Resource": {"Filename": TOOL}, "Seed": seed(0x7), "Store": inv("B")},
        {"Name": "", "Resource": {"Filename": ""}, "Seed": seed(0), "Store": inv()},
    ],
    "CurrentFreighter": {"Filename": FREIGHTER, "Seed": seed(0xF1)},
    "CurrentFreighterHomeSystemSeed": seed(0xF2),
    "PlayerFreighterName": "Lark",
    "FreighterInventory": inv("S"),
    "FreighterFleet": [
        {"Resource": {"Filename": ""}},                                   # unused, as in every save seen
        {"Resource": {"Filename": FREIGHTER, "Seed": seed(0xF1)}, "HomeSystemSeed": seed(0xF2)},
        {"Resource": {"Filename": FREIGHTER, "Seed": seed(0xF3)}, "HomeSystemSeed": seed(0xF4),
         "Inventory": inv("B"), "Dismissed": False},
        {"Resource": {"Filename": FREIGHTER, "Seed": seed(0xF5)}, "Dismissed": True},
    ],
    "FleetFrigates": [
        {"ResourceSeed": seed(0xA1), "HomeSystemSeed": seed(0xA2), "CustomName": "Bulwark",
         "FrigateClass": {"FrigateClass": "Combat"}, "InventoryClass": {"InventoryClass": "B"},
         "Race": {"AlienRace": "Traders"}, "TotalNumberOfExpeditions": 4},
    ],
    "SquadronPilots": [
        {"NPCResource": {"Filename": "MODELS/NPC.SCENE.MBIN", "Seed": seed(0xB1)},
         "ShipResource": {"Filename": SHIP, "Seed": seed(0xB2)}, "TraitsSeed": "0xB3",
         "PilotRank": 2},
        {"NPCResource": {"Filename": "", "Seed": seed(0)}, "ShipResource": {"Filename": ""}},
    ],
    "VehicleOwnership": [
        {"Name": "", "Location": 0, "Inventory": inv(), "Inventory_TechOnly": inv(slots=[{}])},
        {"Name": "Mule", "Location": "0x3227000093BE63", "Inventory": inv("C")},
        {"Name": "", "Location": 0, "Inventory": inv(), "Inventory_Cargo": inv(slots=[{}])},
    ],
    "Pets": [
        {"CreatureID": "^CAT", "CreatureSeed": seed(0xC1), "BirthTime": 1_700_000_000,
         "CustomName": "Moss", "Biome": {"Biome": "Lush"},
         "CreatureType": {"CreatureType": "Passive"}},
        {"CreatureID": "^", "CreatureSeed": seed(0)},                     # an empty slot
        {"CreatureID": "^CAT", "CreatureSeed": seed(0xC1), "BirthTime": 1_700_000_500},  # a sibling
    ],
    "Eggs": [{"CreatureID": "^QUAD", "CreatureSeed": seed(0xD1), "BirthTime": 1_710_000_000,
              "SenderData": {"UID": "someone"}}],
    "PersistentPlayerBases": [
        {"BaseType": {"PersistentBaseTypes": "HomePlanetBase"}, "Name": "Farm",
         "GalacticAddress": "0x00400500033FFABC", "Position": [1.0, 2.0, 3.0],
         "Objects": [{"ObjectID": "^WALL"}, {"ObjectID": "^WALL"}]},
        {"BaseType": {"PersistentBaseTypes": "PlayerShipBase"}, "UserData": 9,
         "Owner": {"TS": MADE + 1}, "Objects": [{"ObjectID": "^HULL"}] * 3},
        {"BaseType": {"PersistentBaseTypes": "ExternalPlanetBase"}, "Name": "Someone's"},
    ],
    "TeleportEndpoints": [
        {"UniverseAddress": {"RealityIndex": 0, "GalacticAddress": {
            "VoxelX": -1348, "VoxelY": 3, "VoxelZ": 0x3FF, "SolarSystemIndex": 0x5,
            "PlanetIndex": 4}}, "Position": [1.2, 2.4, 2.6], "TeleporterType": "Base",
         "Name": "Farm", "IsFavourite": True},
    ],
}
SAVE = {"CommonStateData": {"SaveName": "Main"},
        "BaseContext": {"PlayerStateData": PSD},
        "ExpeditionContext": {"PlayerStateData": {"Pets": copy.deepcopy(PSD["Pets"][:1])}}}


def by_kind(found):
    return {k: [a for a in found if a.kind == k and a.context == "base"] for k in KINDS}


def test_every_kind_is_found_in_order_and_empty_slots_are_not():
    found = assets_of(obf(SAVE))
    assert [(a.context, a.kind, a.index) for a in found] == [
        ("base", "ship", 0), ("base", "corvette", 2), ("base", "multitool", 0),
        ("base", "freighter", None), ("base", "freighter", 2), ("base", "frigate", 0),
        ("base", "pilot", 0), ("base", "exocraft", 1), ("base", "exocraft", 2),
        ("base", "companion", 0), ("base", "companion", 2), ("base", "egg", 0),
        ("base", "base", 0), ("base", "location", 0),
        ("expedition", "companion", 0),
    ]


def test_each_asset_reads_its_key_name_model_class_and_use():
    k = by_kind(assets_of(obf(SAVE)))
    (ship,), (corvette,), (tool,) = k["ship"], k["corvette"], k["multitool"]
    assert (ship.key, ship.name, ship.model, ship.cls, ship.current) == (
        f"{SHIP}|0x0000000000000051", "Swift", SHIP, "S", False)
    assert (corvette.name, corvette.current, corvette.facts) == (
        "Hall", True, {"seed": f"0x{MADE:016X}", "layout_parts": 3})
    assert (tool.key, tool.name, tool.cls, tool.current) == (
        f"{TOOL}|0x0000000000000007", "Spark", "B", True)
    current, other = k["freighter"]
    assert (current.key, current.name, current.cls, current.current, current.where[-1]) == (
        f"{FREIGHTER}|0x00000000000000F1|0x00000000000000F2", "Lark", "S", True,
        "CurrentFreighter")
    assert (other.cls, other.current, other.facts["seed"]) == ("B", False, "0x00000000000000F3")
    (frigate,) = k["frigate"]
    assert (frigate.key, frigate.name, frigate.model, frigate.cls, frigate.facts) == (
        "0x00000000000000A1|0x00000000000000A2", "Bulwark", "Combat", "B",
        {"race": "Traders", "expeditions": 4})
    (pilot,) = k["pilot"]
    assert (pilot.key, pilot.model, pilot.facts) == (
        "0x00000000000000B1|0x00000000000000B2|0x00000000000000B3", SHIP,
        {"npc": "MODELS/NPC.SCENE.MBIN", "rank": 2})
    placed, holding = k["exocraft"]
    assert (placed.key, placed.name, placed.model, placed.cls, placed.current) == (
        "slot:1", "Mule", None, "C", True)
    assert (holding.key, holding.current) == ("slot:2", False)


def test_siblings_have_their_own_key_and_a_gifted_egg_s_sender_is_never_read():
    k = by_kind(assets_of(obf(SAVE)))
    first, sibling = k["companion"]
    assert first.key == "0x00000000000000C1|1700000000" != sibling.key
    assert (first.name, first.model, first.facts) == ("Moss", "CAT", {"biome": "Lush",
                                                                       "type": "Passive"})
    (egg,) = k["egg"]
    assert (egg.key, egg.model) == ("0x00000000000000D1|1710000000", "QUAD")
    assert "someone" not in json.dumps([a.facts for a in assets_of(obf(SAVE))])


def test_a_base_is_keyed_as_the_vault_keys_it_and_a_layout_belongs_to_its_corvette():
    k = by_kind(assets_of(obf(SAVE)))
    (farm,) = k["base"]                     # not the corvette layout, not another player's base
    assert (farm.name, farm.model, farm.galaxy, farm.address, farm.facts) == (
        "Farm", "HomePlanetBase", 0, "0x00400500033FFABC", {"parts": 2})
    assert farm.key.startswith("HomePlanetBa:")


def test_a_saved_location_s_address_is_the_one_its_base_has():
    k = by_kind(assets_of(obf(SAVE)))
    (point,), (farm,) = k["location"], k["base"]
    assert (point.name, point.model, point.galaxy, point.facts) == (
        "Farm", "Base", 0, {"favourite": True})
    assert point.address == farm.address == "0x00400500033FFABC"
    assert point.key == "0x00400500033FFABC|Base|1,2,3"


@pytest.mark.parametrize("galaxy, block", [(None, {"VoxelX": 1}), (0, {}), (0, {"VoxelX": 1.5})])
def test_an_address_without_every_part_is_unknown(galaxy, block):
    assert portal_address(galaxy, obf(block)) is None


def test_where_names_each_asset_for_an_edit():
    body = json.dumps(obf(SAVE), separators=(",", ":")).encode()
    for a in assets_of(obf(SAVE)):
        assert json.loads(savepatch.value_bytes(body, a.where)), a.where


def test_a_save_from_before_the_contexts_has_one_context_at_the_root():
    found = assets_of(obf({"PlayerStateData": {"Pets": PSD["Pets"][:1]}}))
    assert [(a.context, a.where) for a in found] == [("base", ("PlayerStateData", "Pets", 0))]


def test_odd_values_read_as_unknown_and_never_stop_the_reading():
    odd = obf({"PlayerStateData": {
        "PrimaryShip": "two", "ShipOwnership": [{"Resource": {"Filename": SHIP, "Seed": "x"}}],
        "FleetFrigates": [7, {"ResourceSeed": None}], "SquadronPilots": "none",
        "VehicleOwnership": [{"Location": True}, {"Location": "0"}],
        "Pets": [{"CreatureID": "^CAT", "BirthTime": 1.5}],
        "TeleportEndpoints": [{"UniverseAddress": {"RealityIndex": "0"}, "Position": "here"}],
    }})
    found = assets_of(odd)
    assert [(a.kind, a.key, a.current) for a in found] == [
        ("ship", f"{SHIP}|", False), ("frigate", "|", None), ("companion", "|", None),
        ("location", "||", None)]


def states(save):
    root = obf(save)
    return {(a.context, a.kind, a.key): state_of(root, a) for a in assets_of(root)}


def changed(edit):
    """The assets whose state differs after ``edit`` changed a copy of SAVE."""
    save = copy.deepcopy(SAVE)
    edit(save["BaseContext"]["PlayerStateData"])
    before, after = states(SAVE), states(save)
    return sorted(k[1] for k in before if k in after and before[k] != after[k])


def test_a_state_is_a_short_digest_and_the_same_for_the_same_save():
    first = states(SAVE)
    assert first == states(copy.deepcopy(SAVE))
    assert all(len(v) == 16 and int(v, 16) >= 0 for v in first.values())


@pytest.mark.parametrize("edit", [
    lambda p: p["ShipOwnership"][0].update(Position=[9, 9, 9], Direction=[0, 1, 0], Location=7),
    lambda p: p["Pets"][0].update(Trust=0.9, Moods=[0.1, 0.2], LastEggTime=5),
    lambda p: p["Multitools"][0].update(PrimaryMode=1),
    lambda p: p["PersistentPlayerBases"][0]["Objects"][0].update(Timestamp=99, Position=[1, 1, 1]),
    lambda p: p["PersistentPlayerBases"][0].update(Owner={"UID": "someone", "TS": 5}),
    lambda p: p["TeleportEndpoints"][0].update(Position=[1.4, 2.1, 2.9], Facing=[1, 0, 0]),
    lambda p: p["VehicleOwnership"][1].update(Location="0x1", Position=[3, 3, 3]),
])
def test_what_changes_by_itself_leaves_every_state_as_it_was(edit):
    assert changed(edit) == []


def charge(p):
    p["ShipOwnership"][0]["Inventory"]["Slots"] = [
        {"Type": {"InventoryType": "Technology"}, "Id": "^HYPERDRIVE", "Amount": 100}]


@pytest.mark.parametrize("edit, kinds", [
    (lambda p: p["ShipOwnership"][0].update(Name="Swifter"), ["ship"]),
    (lambda p: p["ShipOwnership"][0]["Inventory"]["Slots"].append(
        {"Type": {"InventoryType": "Product"}, "Id": "^CASING", "Amount": 3}), ["ship"]),
    (lambda p: p["Pets"][0].update(CustomName="Mossy"), ["companion"]),
    (lambda p: p["PersistentPlayerBases"][0]["Objects"].append({"ObjectID": "^DOOR"}), ["base"]),
    (lambda p: p["PersistentPlayerBases"][0].update(Name="Barn"), ["base"]),
    (lambda p: p["TeleportEndpoints"][0].update(Name="Barn"), ["location"]),
    (lambda p: p["FreighterInventory"]["Slots"].append({"Id": "^FUEL1", "Amount": 5}),
     ["freighter"]),
    (lambda p: p["FleetFrigates"][0].update(TotalNumberOfExpeditions=5), ["frigate"]),
])
def test_a_real_change_makes_a_new_state_for_that_asset_only(edit, kinds):
    assert changed(edit) == kinds


def test_a_technology_s_charge_is_not_a_change_but_its_kind_of_item_is():
    save = copy.deepcopy(SAVE)
    charge(save["BaseContext"]["PlayerStateData"])
    recharged = copy.deepcopy(save)
    recharged["BaseContext"]["PlayerStateData"]["ShipOwnership"][0]["Inventory"]["Slots"][0][
        "Amount"] = 40
    assert states(save) == states(recharged)
    product = copy.deepcopy(save)
    product["BaseContext"]["PlayerStateData"]["ShipOwnership"][0]["Inventory"]["Slots"][0][
        "Type"] = {"InventoryType": "Product"}
    assert states(save) != states(product)


def test_the_reader_is_in_the_public_api():
    assert api.assets_of is assets_of and api.Asset is Asset and api.ASSET_KINDS == KINDS
    assert api.state_of is state_of


@pytest.mark.slow
def test_a_real_save_s_assets_all_read_and_name_their_place(live_save_dir):
    """Read-only: every asset of a real save is where ``where`` says; a save with ships has a
    current one."""
    view = savedir.scan(live_save_dir)
    m = next(s.newest for s in view.occupied_slots if s.newest is not None and s.newest.valid)
    data = Path(m.data_path).read_bytes()
    body = saveedit.body_of(data)
    found = assets_of(savejson.decode_save(data).root)
    assert found
    root = savejson.decode_save(data).root
    for a in found:
        assert savepatch.value_bytes(body, a.where), a.where
        assert len(state_of(root, a)) == 16
    for context in {a.context for a in found}:
        ships = [a for a in found if a.context == context and a.kind in ("ship", "corvette")]
        assert not ships or sum(bool(a.current) for a in ships) == 1, context
