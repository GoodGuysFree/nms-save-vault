"""Reading the save tree by readable names, and the normalisers (``core/index/nodes.py``)."""
from __future__ import annotations

import pytest

from nms_save_vault.core.hashname import hash_name
from nms_save_vault.core.index import nodes
from nms_save_vault.core.index.nodes import get, items


def tree(**readable):
    """A save subtree with readable keys hashed the way the game writes them."""
    def conv(v):
        if isinstance(v, dict):
            return {hash_name(k): conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v
    return conv(readable)


def test_get_walks_readable_names_through_their_keys():
    psd = tree(UniverseAddress={"RealityIndex": 9, "GalacticAddress": {"VoxelX": -3}})
    assert get(psd, "UniverseAddress", "RealityIndex") == 9
    assert get(psd, "UniverseAddress", "GalacticAddress", "VoxelX") == -3
    assert psd == {hash_name("UniverseAddress"): {hash_name("RealityIndex"): 9,
                                                  hash_name("GalacticAddress"): {hash_name("VoxelX"): -3}}}


def test_get_never_raises_on_missing_or_odd_shapes():
    psd = tree(Units=5, ShipOwnership=[1, 2])
    assert get(psd, "Nanites") is None
    assert get(psd, "Nanites", default=0) == 0
    assert get(psd, "Units", "Deeper") is None          # an int is not walked into
    assert get(psd, "ShipOwnership", "Name") is None    # nor a list
    assert get(None, "Units") is None
    assert get(psd) is psd


def test_items_is_a_list_or_empty():
    psd = tree(ShipOwnership=[{"Name": "a"}], Units=3)
    assert items(psd, "ShipOwnership") == [{hash_name("Name"): "a"}]
    assert items(psd, "Units") == []
    assert items(psd, "Missing") == []


@pytest.mark.parametrize("path", [("Owner",), ("UsedDiscoveryOwnersV2",), ("Owner", "PTK"), ("Objects", "UID")])
def test_a_denied_name_anywhere_in_a_path_raises(path):
    with pytest.raises(nodes.PrivacyError):
        get({}, *path)


def test_u32_reads_signed_units_as_unsigned():
    assert nodes.u32(-1) == 4_294_967_295
    assert nodes.u32(1234) == 1234
    assert nodes.u32(True) is None and nodes.u32("5") is None and nodes.u32(None) is None


def test_u64_and_hex64_take_strings_and_ints_alike():
    assert nodes.u64("0x0000000A00000001") == nodes.u64(0x0000000A00000001) == 0xA00000001
    assert nodes.u64("0XFF") == 255 and nodes.u64("42") == 42
    assert nodes.u64(-1) == (1 << 64) - 1
    assert nodes.u64("0xZZ") is None and nodes.u64(1.5) is None and nodes.u64(False) is None
    assert nodes.hex64(10) == "0x000000000000000A"
    assert nodes.hex64("0x1A") == "0x000000000000001A"
    assert nodes.hex64(None) is None


def test_galaxy_is_bits_32_to_39_of_a_base_address():
    assert nodes.galaxy_of("0x0009_0012_3456_789A".replace("_", "")) == 0x12
    assert nodes.galaxy_of(0xFF_FF00_0000_0000) == 0
    assert nodes.galaxy_of(0x01_0000_0000) == 1
    assert nodes.galaxy_of(None) is None


def test_text_strips_markup_and_replaces_invalid_bytes():
    assert nodes.text("<PET1>The Hangar</>") == "The Hangar"
    assert nodes.text("  Main Save  ") == "Main Save"
    broken = b"Ba\xffse".decode("utf-8", errors="surrogateescape")
    assert nodes.text(broken) == "Ba�se"
    assert nodes.text("\ud800x") == "�x"
    assert nodes.text("x" * 300) == "x" * 128
    assert nodes.text("abcdef", limit=3) == "abc"
    assert nodes.text(None) == "" and nodes.text(7) == ""
    nodes.text(broken).encode("utf-8")                   # valid for sqlite TEXT


def test_gid_drops_one_leading_caret():
    assert nodes.gid("^ACT1_STEP1") == "ACT1_STEP1"
    assert nodes.gid("T_WALLM_H") == "T_WALLM_H"
    assert nodes.gid(None) == ""


def test_seed_takes_pairs_and_bare_strings():
    assert nodes.seed([True, "0x1F"]) == "0x000000000000001F"
    assert nodes.seed("0x1F") == "0x000000000000001F"
    assert nodes.seed([False]) is None and nodes.seed(None) is None


def test_number_reads_int_float_and_the_empty_zero():
    assert nodes.number(tree(IntValue=489)) == 489
    assert nodes.number(tree(FloatValue=85770.07)) == 85770.07
    assert nodes.number({}) == 0
    assert nodes.number(tree(IntValue=-1)) == -1
    assert nodes.number(None) is None and nodes.number(3) is None
