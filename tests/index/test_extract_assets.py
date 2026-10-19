"""Bases, exocraft bays, settlements, ships, fleet and companions (D4 step 9)."""
from __future__ import annotations

import pytest

from nms_save_vault.core.index import extract
from nms_save_vault.core.savejson import decode_save

from .savebuilder import context_save, encode

CORVETTE = "MODELS/COMMON/SPACECRAFT/BIGGS/BIGGS.SCENE.MBIN"
FIGHTER = "MODELS/COMMON/SPACECRAFT/FIGHTERS/FIGHTER_PROC.SCENE.MBIN"


def base_facts(**psd) -> dict:
    data, _ = encode(context_save(base=psd))
    f = extract.run_extractors(decode_save(data))
    assert f["status"] == "ok", f["errors"]
    return f["contexts"]["base"]


def obj(oid: str, ts: int = 1, user: int = 0) -> dict:
    return {"ObjectID": "^" + oid, "Timestamp": ts, "UserData": user, "Position": [0.0, 1.0, 2.0]}


def base(btype: str, name: str, objects=(), address="0x0000000500000001", position=(1.0, 2.0, 3.0), **extra) -> dict:
    b = {"BaseType": {"PersistentBaseTypes": btype}, "Name": name, "GalacticAddress": address,
         "Position": list(position), "Objects": list(objects), "OriginalBaseVersion": 8,
         "Owner": {"UID": "SENTINEL-UID", "USN": "SENTINEL_USER", "PTK": "ST", "LID": "", "TS": 1}}
    b.update(extra)
    return b


SHIPS = [{"Name": "Explorer One", "Resource": {"Filename": FIGHTER, "Seed": [True, "0xAB"]},
          "Inventory": {"Class": {"InventoryClass": "S"}}},
         {"Name": "", "Resource": {"Filename": ""}},
         {"Name": "Big One", "Resource": {"Filename": CORVETTE, "Seed": "0x1"},
          "Inventory": {"Class": {"InventoryClass": "C"}}}]


def test_own_bases_and_what_is_only_counted():
    bases = [
        base("HomePlanetBase", "Home", [obj("BASE_FLAG"), obj("C_WALL"), obj("C_WALL"), obj("M_FLOOR")]),
        base("FreighterBase", "Freighter", [obj("S_CONTAINER1"), obj("CORRIDOR_SPACE")], address=17),
        base("PlayerShipBase", "Corvette", [obj("B_COCKPIT")], UserData=2),
        base("PlayerShipBase", "Orphan layout", [obj("B_COCKPIT")], UserData=1),
        base("ExternalPlanetBase", "Someone else"),
        base("ExternalPlanetBase", "Someone else 2"),
        base("UITempShipBase", "Scratch"),
        base("BrandNewBaseType", "Future"),
    ]
    b = base_facts(PersistentPlayerBases=bases, ShipOwnership=SHIPS)["bases"]
    assert [x["type"] for x in b["own"]] == ["HomePlanetBase", "FreighterBase", "PlayerShipBase"]
    assert (b["external_cached"], b["orphan_corvette_layouts"]) == (2, 1)
    assert b["unknown_types"] == {"BrandNewBaseType": 1}
    assert b["ptk"] == ["ST"]
    home, freighter, corvette = b["own"]
    assert (home["name"], home["parts"], home["legacy_structures"], home["legacy_freighter"]) == ("Home", 4, 3, 0)
    assert (freighter["parts"], freighter["legacy_freighter"]) == (2, 2)
    assert (freighter["id"], corvette["id"]) == ("freighter", "corvette:2")
    assert home["id"].startswith("HomePlanetBa:") and len(home["id"]) == 25
    assert (home["galaxy"], home["address"], home["orig_version"]) == (5, "0x0000000500000001", 8)
    assert freighter["address"] == "0x0000000000000011" and freighter["galaxy"] == 0


def test_two_bases_on_one_planet_get_different_ids_and_identity_ignores_name():
    a = base("HomePlanetBase", "A", position=(1, 2, 3))
    b = base("HomePlanetBase", "B", position=(9, 9, 9))
    renamed = base("HomePlanetBase", "A renamed", position=(1, 2, 3))
    first = base_facts(PersistentPlayerBases=[a, b])["bases"]["own"]
    again = base_facts(PersistentPlayerBases=[renamed])["bases"]["own"]
    assert first[0]["id"] != first[1]["id"]
    assert again[0]["id"] == first[0]["id"]


def test_addresses_as_string_or_int_are_the_same_base():
    s = base_facts(PersistentPlayerBases=[base("HomePlanetBase", "A", address="0x0000000500000001")])
    i = base_facts(PersistentPlayerBases=[base("HomePlanetBase", "A", address=0x0000000500000001)])
    assert s["bases"]["own"][0]["id"] == i["bases"]["own"][0]["id"]


def test_layout_digest_ignores_object_timestamps_and_user_data_but_not_parts():
    parts = [obj("C_WALL", ts=1, user=0), obj("BASE_FLAG", ts=2, user=5)]
    churned = [obj("BASE_FLAG", ts=99, user=7), obj("C_WALL", ts=50, user=1)]
    grown = parts + [obj("C_WALL")]
    digest = [base_facts(PersistentPlayerBases=[base("HomePlanetBase", "A", p)])["bases"]["own"][0]["layout"]
              for p in (parts, churned, grown)]
    assert digest[0] == digest[1] != digest[2]


def test_exocraft_bays_come_from_loose_objects():
    loose = [obj("GARAGE_M"), obj("GARAGE_SUB"), obj("SUMMON_GARAGE"), obj("GARAGE_M"), obj("BASE_FLAG")]
    assert base_facts(BaseBuildingObjects=loose)["exocraft_bays"] == ["GARAGE_M", "GARAGE_SUB", "SUMMON_GARAGE"]


def test_settlements_claimed_are_named_through_their_seed():
    history = [{"SeedValue": "0x00000000000000AA", "PlayerClaimedTime": 1790000000.0},
               {"SeedValue": 0, "PlayerClaimedTime": 0.0},
               {"SeedValue": "0xBB", "PlayerClaimedTime": 12.5}]
    states = [{"SeedValue": "0xAA", "Name": "First Light"}, {"SeedValue": "0xCC", "Name": "Not mine"},
              {"SeedValue": "0x00000000000000BB", "Name": "<TITLE>Second</>"}]
    s = base_facts(SettlementHistory=history, SettlementStatesV2=states)["settlements"]
    assert s == {"claimed": 2, "names": ["First Light", "Second"]}


def test_ships_skip_empty_slots_and_mark_the_primary():
    ctx = base_facts(ShipOwnership=SHIPS, PrimaryShip=2, ArchivedShipOwnership=[
        {"ArchivedName": "Old", "ArchivedClass": {"ShipClass": "Hauler"},
         "ArchivedInventoryClass": {"InventoryClass": "A"}, "Ownership": {"Resource": {"Filename": FIGHTER}}},
        {"ArchivedName": "Empty", "Ownership": {"Resource": {"Filename": ""}}}])
    assert ctx["ships"] == [
        {"slot": 0, "name": "Explorer One", "model_path": FIGHTER, "cls": "S", "seed": "0x00000000000000AB",
         "primary": False},
        {"slot": 2, "name": "Big One", "model_path": CORVETTE, "cls": "C", "seed": "0x0000000000000001",
         "primary": True},
    ]
    assert ctx["archived_ships"] == [{"name": "Old", "ship_class": "Hauler", "inv_class": "A"}]


def test_freighter_frigates_and_multitools():
    ctx = base_facts(CurrentFreighter={"Filename": "MODELS/COMMON/SPACECRAFT/INDUSTRIAL/CAPITALFREIGHTER.SCENE.MBIN"},
                     PlayerFreighterName="Synth Hauler", FreighterInventory={"Class": {"InventoryClass": "S"}},
                     FleetFrigates=[{}, {}, {}],
                     Multitools=[{"Name": "Staff", "Resource": {"Filename": "MODELS/WEAPONS/STAFF.SCENE.MBIN"},
                                  "Store": {"Class": {"InventoryClass": "S"}}},
                                 {"Name": "", "Resource": {"Filename": ""}}])
    assert ctx["freighter"] == {"has": True, "name": "Synth Hauler", "model": "CAPITALFREIGHTER", "cls": "S"}
    assert ctx["frigates"] == 3
    assert ctx["multitools"] == [{"name": "Staff", "model": "STAFF", "cls": "S"}]


def test_no_freighter():
    ctx = base_facts(CurrentFreighter={"Filename": ""}, PlayerFreighterName="ghost")
    assert ctx["freighter"] == {"has": False, "name": "", "model": "", "cls": None}


def test_companions_eggs_and_slots():
    pets = [{"CustomName": "<PET1>Biscuit</>", "CreatureID": "^FLYINGLIZARD", "Biome": {"Biome": "Lush"},
             "CreatureSeed": [True, "0x10"], "SenderData": {"UID": "SENTINEL"}},
            {"CustomName": "", "CreatureID": "^"},
            {"CustomName": "", "CreatureID": "^QUAD_PET", "Biome": {"Biome": "Toxic"}}]
    ctx = base_facts(Pets=pets, Eggs=[{"CreatureID": "^QUAD_PET"}, {"CreatureID": "^"}],
                     UnlockedPetSlots=[True, True, False, True])
    assert ctx["companions"] == [
        {"slot": 0, "name": "Biscuit", "creature": "FLYINGLIZARD", "biome": "Lush", "seed": "0x0000000000000010"},
        {"slot": 2, "name": "", "creature": "QUAD_PET", "biome": "Toxic", "seed": None},
    ]
    assert (ctx["eggs"], ctx["pet_slots"]) == (1, 3)


@pytest.mark.parametrize("field", ["PersistentPlayerBases", "ShipOwnership", "Pets", "Multitools", "SettlementHistory"])
def test_wrong_shapes_give_empty_facts_not_errors(field):
    ctx = base_facts(**{field: {"not": "a list"}})
    assert ctx["bases"]["own"] == [] and ctx["ships"] == [] and ctx["companions"] == []


# --- review fixes ---------------------------------------------------------------------------

BAD = b"S\xff".decode("utf-8", errors="surrogateescape")
LONG = "X" * 500


def _clean(value: str | None, limit: int = 64) -> None:
    assert value is not None
    assert len(value) <= limit
    value.encode("utf-8")                     # no lone surrogates left


def test_enum_like_strings_from_the_save_are_normalised():
    ctx = base_facts(
        ShipOwnership=[{"Resource": {"Filename": FIGHTER}, "Inventory": {"Class": {"InventoryClass": BAD}}}],
        ArchivedShipOwnership=[{"ArchivedClass": {"ShipClass": LONG}, "ArchivedInventoryClass": {"InventoryClass": BAD},
                                "Ownership": {"Resource": {"Filename": FIGHTER}}}],
        CurrentFreighter={"Filename": FIGHTER}, FreighterInventory={"Class": {"InventoryClass": LONG}},
        Multitools=[{"Resource": {"Filename": "STAFF.SCENE.MBIN"}, "Store": {"Class": {"InventoryClass": BAD}}}],
        Pets=[{"CreatureID": "^QUAD_PET", "Biome": {"Biome": BAD + LONG}}])
    _clean(ctx["ships"][0]["cls"])
    _clean(ctx["archived_ships"][0]["ship_class"])
    _clean(ctx["archived_ships"][0]["inv_class"])
    _clean(ctx["freighter"]["cls"])
    _clean(ctx["multitools"][0]["cls"])
    _clean(ctx["companions"][0]["biome"])
    assert ctx["ships"][0]["cls"] == "S�"


def test_two_layouts_for_one_corvette_slot_and_bases_without_position_get_unique_ids():
    no_pos = {"Position": None}
    bases = [base("PlayerShipBase", "Layout A", UserData=2), base("PlayerShipBase", "Layout B", UserData=2),
             base("HomePlanetBase", "P1", **no_pos), base("HomePlanetBase", "P2", **no_pos),
             base("HomePlanetBase", "Same spot 1"), base("HomePlanetBase", "Same spot 2")]
    own = base_facts(PersistentPlayerBases=bases, ShipOwnership=SHIPS)["bases"]["own"]
    ids = [b["id"] for b in own]
    assert len(ids) == len(set(ids)) == 6
    assert ids[0] == "corvette:2" and ids[1] == "corvette:2#1"
    assert ids[2].endswith("#2") and ids[3].endswith("#3")
    assert ids[5] == ids[4] + "#5"


class CountingList(list):
    iterations = 0

    def __iter__(self):
        CountingList.iterations += 1
        return super().__iter__()


def test_ship_ownership_is_walked_once_per_context():
    from nms_save_vault.core.hashname import hash_name

    data, _ = encode(context_save(base={"ShipOwnership": SHIPS, "Multitools": []},
                                  expedition={"ShipOwnership": SHIPS}))
    decoded = decode_save(data)
    for ctx in ("BaseContext", "ExpeditionContext"):
        psd = decoded.root[hash_name(ctx)][hash_name("PlayerStateData")]
        psd[hash_name("ShipOwnership")] = CountingList(psd[hash_name("ShipOwnership")])
    CountingList.iterations = 0
    facts = extract.run_extractors(decoded)
    assert facts["status"] == "ok"
    assert CountingList.iterations == 2
    for ctx in facts["contexts"].values():
        assert [s["slot"] for s in ctx["ships"]] == [0, 2]
        assert sorted(k for k in ctx["inventories"] if k.startswith("ship:")) == ["ship:0", "ship:2"]
        assert ctx["bases"]["orphan_corvette_layouts"] == 0
