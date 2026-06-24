"""Slot/file/ordinal mapping, anchored to values verified against real save files."""
import pytest

from nms_save_vault.core import slotmap
from nms_save_vault.core.formats import MAX_SAVE_FILES, MAX_SAVE_SLOTS


# (file_no, filename, ordinal, slot, member) tuples confirmed empirically.
KNOWN = [
    (1, "save.hg", 2, 1, 0),
    (2, "save2.hg", 3, 1, 1),
    (3, "save3.hg", 4, 2, 0),
    (17, "save17.hg", 18, 9, 0),
    (18, "save18.hg", 19, 9, 1),
    (29, "save29.hg", 30, 15, 0),
    (30, "save30.hg", 31, 15, 1),
]


@pytest.mark.parametrize("f,name,ordinal,slot,member", KNOWN)
def test_known_mapping(f, name, ordinal, slot, member):
    assert slotmap.data_filename(f) == name
    assert slotmap.meta_filename(f) == "mf_" + name
    assert slotmap.storage_ordinal(f) == ordinal
    assert slotmap.slot_of(f) == slot
    assert slotmap.member_of(f) == member


@pytest.mark.parametrize("f,name,ordinal,slot,member", KNOWN)
def test_parse_filenames(f, name, ordinal, slot, member):
    assert slotmap.parse_data_filename(name) == f
    assert slotmap.parse_meta_filename("mf_" + name) == f


def test_parse_rejects_non_saves():
    for bad in ("accountdata.hg", "mf_accountdata.hg", "save.txt", "notsave2.hg", "cache"):
        assert slotmap.parse_data_filename(bad) is None


def test_file_no_round_trip():
    for f in slotmap.all_file_numbers():
        slot, member = slotmap.slot_of(f), slotmap.member_of(f)
        assert slotmap.file_no(slot, member) == f


def test_slot_coverage():
    seen = set()
    for slot in range(1, MAX_SAVE_SLOTS + 1):
        a, b = slotmap.slot_file_numbers(slot)
        seen.update((a, b))
    assert seen == set(range(1, MAX_SAVE_FILES + 1))


def test_out_of_range():
    with pytest.raises(ValueError):
        slotmap.storage_ordinal(0)
    with pytest.raises(ValueError):
        slotmap.data_filename(MAX_SAVE_FILES + 1)
    with pytest.raises(ValueError):
        slotmap.file_no(MAX_SAVE_SLOTS + 1, 0)


def test_savefileref():
    ref = slotmap.SaveFileRef(17)
    assert (ref.slot, ref.member_label, ref.storage_ordinal) == (9, "A", 18)
    assert ref.data_name == "save17.hg" and ref.meta_name == "mf_save17.hg"
