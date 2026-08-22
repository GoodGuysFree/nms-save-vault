"""Save-type roles, difficulty naming, and cloud-sync reporting.

The A/B members are not interchangeable: member 0 is the game's periodic auto-save and
member 1 is the restore point it writes when you leave your ship or use a save point /
beacon. libNOM.io derives this positionally (``SaveTypeEnum(CollectionIndex % 2)``), which
is why the Xbox containers are named ``Slot<N>Auto`` / ``Slot<N>Manual``.
"""
from __future__ import annotations

import pytest

from nms_save_vault.core import catalog, formats, slotmap


# --- which member is which ---------------------------------------------------


def test_member_zero_is_the_autosave_and_one_the_restore_point():
    assert slotmap.save_type_label(0) == "Auto-Save"
    assert slotmap.save_type_label(1) == "Restore-Point"


@pytest.mark.parametrize(
    "data_name,slot,expected",
    [
        ("save.hg", 1, "Auto-Save"),        # Slot1Auto
        ("save2.hg", 1, "Restore-Point"),   # Slot1Manual
        ("save17.hg", 9, "Auto-Save"),      # Slot9Auto
        ("save30.hg", 15, "Restore-Point"), # Slot15Manual
    ],
)
def test_file_number_determines_the_role(data_name, slot, expected):
    """The Xbox identifiers are generated from this same index, so the filename alone
    settles which save is which on Steam too."""
    ref = slotmap.SaveFileRef(slotmap.parse_data_filename(data_name))
    assert ref.slot == slot
    assert ref.save_type_label == expected


def test_label_letters_round_trip_to_roles():
    assert slotmap.save_type_label_of("A") == "Auto-Save"
    assert slotmap.save_type_label_of("b") == "Restore-Point"
    assert slotmap.save_type_label_of("") == ""  # nothing stored -> nothing shown


# --- difficulty --------------------------------------------------------------


def test_difficulty_preset_wins_over_the_legacy_game_mode():
    """Post-Waypoint saves all report game mode 1 (Normal); the preset is the real value.
    A save named 'Creative Primary' in the live folder reads difficulty 3 = Creative."""
    assert formats.difficulty_label(3, 1) == "Creative"
    assert formats.difficulty_label(1, 1) == "Custom"
    assert formats.difficulty_label(6, 1) == "Permadeath"


def test_pre_waypoint_save_falls_back_to_the_game_mode():
    """Before Waypoint the game mode *was* the difficulty, and difficulty is left Invalid."""
    assert formats.difficulty_label(formats.DIFFICULTY_INVALID, 2) == "Creative"
    assert formats.difficulty_label(formats.DIFFICULTY_INVALID, 5) == "Permadeath"


def test_nothing_known_yields_empty_not_a_number():
    assert formats.difficulty_label(0, 0) == ""
    assert formats.difficulty_label(99, 99) == ""


# --- catalog summaries -------------------------------------------------------


def test_catalog_entry_cached_before_difficulty_existed_still_reads():
    """Old catalog.json entries have no 'difficulty' key; the default must not crash or
    show a bare 0."""
    m = catalog.MemberSummary(label="A", present=True, game_mode=1)
    assert m.difficulty == 0
    assert m.difficulty_label == "Normal"  # via the legacy fallback
    assert m.save_type_label == "Auto-Save"


def test_catalog_member_reports_its_role_and_difficulty():
    m = catalog.MemberSummary(label="B", present=True, game_mode=1, difficulty=4)
    assert m.save_type_label == "Restore-Point"
    assert m.difficulty_label == "Relaxed"


# --- cloud sync --------------------------------------------------------------


def test_xbox_sync_states_have_names():
    assert formats.MS_SYNC_STATE_NAMES[1] == "Synced"
    assert formats.MS_SYNC_STATE_NAMES[2] == "Modified"
    assert formats.MS_SYNC_STATE_NAMES[5] == "Created"


def test_steam_member_reports_no_per_save_cloud_state(tmp_path):
    """Steam records no per-save cloud state anywhere, so the app must not imply one."""
    from nms_save_vault.core import savedir

    view = savedir.scan(tmp_path)  # empty folder: members exist as placeholders
    member = view.slots[1].a
    assert member.xbox is None
    assert member.cloud_status == ""


def test_steam_cloud_is_detected_at_folder_level(tmp_path):
    from nms_save_vault.core import savedir

    assert savedir.scan(tmp_path).steam_cloud is False
    (tmp_path / formats.STEAM_AUTOCLOUD).write_text("x", "utf-8")
    assert savedir.scan(tmp_path).steam_cloud is True


def test_xbox_member_reports_its_sync_state(tmp_path, make_wgs_account):
    from nms_save_vault.core import msstore

    acct = make_wgs_account(
        tmp_path, [("Slot1Auto", "hello", "S", "sum", 60, 1_700_000_000)]
    )
    view = msstore.scan(acct)
    member = view.slots[1].a
    assert member.exists
    assert member.save_type_label == "Auto-Save"
    # make_wgs_account writes sync state 2 into every container record.
    assert member.cloud_status == "Modified"
