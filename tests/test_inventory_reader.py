"""core.index.inventory (CL7): every inventory of a save, slot by slot, with the path an edit
names it by. The active multi-tool's mirror (WeaponInventory) is never listed twice."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nms_save_vault.core import api, savedir, saveedit, savejson, savepatch
from nms_save_vault.core.hashname import key as K
from nms_save_vault.core.index.inventory import Inventory, SpecialSlot, inventories_of


def obf(node):
    """A save written with readable names, keyed as the game keys it."""
    if isinstance(node, dict):
        return {K(k): obf(v) for k, v in node.items()}
    if isinstance(node, list):
        return [obf(v) for v in node]
    return node


def slot(item, kind, amount, most, x, y, damage=0.0, installed=True):
    return {"Type": {"InventoryType": kind}, "Id": item, "Amount": amount, "MaxAmount": most,
            "DamageFactor": damage, "FullyInstalled": installed, "AddedAutomatically": False,
            "Index": {"X": x, "Y": y}}


def inv(*slots, special=(), width=10, height=6, cls="S"):
    return {"Slots": list(slots), "ValidSlotIndices": [{"X": x, "Y": 0} for x in range(3)],
            "Class": {"InventoryClass": cls}, "StackSizeGroup": {"InventoryStackSizeGroup": "Default"},
            "BaseStatValues": [], "SpecialSlots": [
                {"Type": {"InventorySpecialSlotType": t}, "Index": {"X": x, "Y": y}}
                for t, x, y in special],
            "Width": width, "Height": height, "IsCool": False, "Name": "", "Version": 1,
            "NumSlotsFromTech": 0}


OXYGEN = slot("^OXYGEN", "Substance", 250, 9999, 0, 0)
TOOL = inv(slot("^LASER", "Technology", 1, 1, 0, 0))
PSD = {
    "Inventory": inv(OXYGEN, slot("^NAV_DATA", "Product", 9, 40, 1, 0)),
    "Inventory_TechOnly": inv(slot("^JET1", "Technology", 1, 1, 0, 0, damage=0.5,
                                   installed=False), special=[("TechBonus", 0, 0)]),
    "Inventory_Cargo": inv(),
    "ShipOwnership": [
        {"Resource": {"Filename": "MODELS/SHIP.SCENE.MBIN"}, "Inventory": inv(OXYGEN),
         "Inventory_TechOnly": inv(), "Inventory_Cargo": inv()},
        {"Resource": {"Filename": ""}, "Inventory": inv(OXYGEN)},          # an empty ship slot
    ],
    "Multitools": [{"Resource": {"Filename": "MODELS/TOOL.SCENE.MBIN"}, "Store": TOOL}],
    "WeaponInventory": TOOL,                                  # the active multi-tool's mirror
    "ShipInventory": inv(OXYGEN),                             # an old field
    "VehicleOwnership": [{"Inventory": inv(), "Inventory_TechOnly": inv()}],
    "FreighterInventory": inv(slot("^FUEL1", "Substance", 5, 9999, 2, 0)),
    "Chest3Inventory": inv(OXYGEN),
    "GraveInventory": inv(OXYGEN),
}
SAVE = {
    "CommonStateData": {"SaveName": "Main"},
    "BaseContext": {"PlayerStateData": PSD},
    "ExpeditionContext": {"PlayerStateData": {"Inventory": inv(OXYGEN)}},
}


def test_every_inventory_is_listed_once_in_order_with_its_place():
    found = inventories_of(obf(SAVE))
    assert [(i.context, i.owner, i.index, i.field) for i in found] == [
        ("base", "exosuit", None, "Inventory"), ("base", "exosuit", None, "Inventory_TechOnly"),
        ("base", "exosuit", None, "Inventory_Cargo"),
        ("base", "ship", 0, "Inventory"), ("base", "ship", 0, "Inventory_TechOnly"),
        ("base", "ship", 0, "Inventory_Cargo"),
        ("base", "multitool", 0, "Store"),
        ("base", "exocraft", 0, "Inventory"), ("base", "exocraft", 0, "Inventory_TechOnly"),
        ("base", "freighter", None, "FreighterInventory"),
        ("base", "storage", None, "Chest3Inventory"),
        ("base", "grave", None, "GraveInventory"),
        ("expedition", "exosuit", None, "Inventory"),
    ]
    oxygen = [(i.owner, i.field) for i in found for s in i.slots if s.item == "OXYGEN"]
    assert len(oxygen) == 5                   # not the empty ship's, the mirror's or the old field's


def test_a_slot_and_an_inventory_read_as_the_save_holds_them():
    exosuit, tech = inventories_of(obf(SAVE))[:2]
    assert exosuit.where == ("BaseContext", "PlayerStateData", "Inventory")
    assert (exosuit.width, exosuit.height, exosuit.inventory_class, exosuit.stack_group) == (
        10, 6, "S", "Default")
    assert exosuit.valid == ((0, 0), (1, 0), (2, 0)) and exosuit.special == ()
    oxygen, nav = exosuit.slots
    assert (oxygen.number, oxygen.item, oxygen.kind, oxygen.amount, oxygen.max_amount,
            oxygen.x, oxygen.y, oxygen.damage, oxygen.installed) == (
        0, "OXYGEN", "Substance", 250, 9999, 0, 0, 0.0, True)
    assert (nav.number, nav.x, nav.kind) == (1, 1, "Product")
    (jet,) = tech.slots
    assert (jet.damage, jet.installed) == (0.5, False)
    assert tech.special == (SpecialSlot(0, 0, "TechBonus"),)


def test_where_names_the_inventory_for_an_edit():
    body = json.dumps(obf(SAVE), separators=(",", ":")).encode()
    for found in inventories_of(obf(SAVE)):
        held = json.loads(savepatch.value_bytes(body, found.where))
        assert len(held[K("Slots")]) == len(found.slots), found.where


def test_a_save_from_before_the_contexts_has_one_context_at_the_root():
    found = inventories_of(obf({"PlayerStateData": {"Inventory": inv(OXYGEN)}}))
    assert [(i.context, i.where) for i in found] == [
        ("base", ("PlayerStateData", "Inventory"))]


def test_odd_values_read_as_unknown_and_never_stop_the_reading():
    odd = {"Id": 7, "Amount": "many", "MaxAmount": True, "Index": "here", "Type": [],
           "DamageFactor": "x", "FullyInstalled": 1}
    root = obf({"PlayerStateData": {"Inventory": {"Slots": [odd], "SpecialSlots": [{}],
                                                  "ValidSlotIndices": [{"X": 1}], "Width": "w"}}})
    (found,) = inventories_of(root)
    (s,) = found.slots
    assert (s.amount, s.max_amount, s.x, s.y, s.kind, s.damage, s.installed) == (
        None, None, None, None, None, None, None)
    assert found.valid == () and found.width is None and found.special == (
        SpecialSlot(None, None, None),)


def test_the_reader_is_in_the_public_api():
    assert api.inventories_of is inventories_of and api.Inventory is Inventory


@pytest.mark.slow
def test_a_real_save_s_inventories_all_read_and_name_their_place(live_save_dir):
    """Read-only: every listed inventory of a real save is where ``where`` says, with all its
    slots; the mirror of the active multi-tool holds nothing the list lacks."""
    view = savedir.scan(live_save_dir)
    m = next(s.newest for s in view.occupied_slots if s.newest is not None and s.newest.valid)
    data = Path(m.data_path).read_bytes()
    body = saveedit.body_of(data)
    root = savejson.decode_save(data).root
    found = inventories_of(root)
    assert found and any(i.owner == "exosuit" for i in found)
    for i in found:
        held = json.loads(savepatch.value_bytes(body, i.where).decode("utf-8", "surrogateescape"),
                          strict=False)
        assert len(held.get(K("Slots"), [])) == len(i.slots), i.where
    _save, contexts = api.contexts_of(root)
    for name, ctx in contexts.items():
        mirror = {(api.get(s, "Id"), api.get(s, "Amount"))
                  for s in api.items(ctx.psd, "WeaponInventory", "Slots")}
        tools = [{("^" + s.item, s.amount) for s in i.slots}
                 for i in found if i.context == name and i.owner == "multitool"]
        assert not mirror or any(mirror <= t for t in tools), name
