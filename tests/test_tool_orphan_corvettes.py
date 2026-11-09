"""tools/orphan_corvettes: which corvette layouts the remover treats as orphans. The game finds a
corvette's layout by the corvette's ship slot, in the layout's ``UserData`` (NMS-Unpack exe
round, section 5); the owner-time rule the tool used first is only a hint now."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from nms_save_vault.core.testing import obfuscated

_PATH = Path(__file__).resolve().parents[1] / "tools" / "orphan_corvettes" / "remove_orphan_corvettes.py"
_spec = importlib.util.spec_from_file_location("remove_orphan_corvettes", _PATH)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

CORVETTE = "MODELS/COMMON/SPACECRAFT/BIGGS/BIGGS.SCENE.MBIN"
SEED = 1_760_000_000


def corvette(seed=SEED):
    return {"Resource": {"Filename": CORVETTE, "Seed": [True, f"0x{seed:X}"]}}


def layout(user_data, ts=0):
    return {"BaseType": {"PersistentBaseTypes": "PlayerShipBase"}, "UserData": user_data,
            "Owner": {"TS": ts}, "Objects": [{}]}


def psd(ships, bases):
    return obfuscated({"ShipOwnership": ships, "PersistentPlayerBases": bases})


def test_a_layout_is_the_corvette_s_when_its_user_data_names_the_corvette_s_slot():
    remove, report = tool.orphans_in(psd([{}, corvette()], [layout(1, ts=5)]), False)
    assert remove == [] and "ship slot 1" in report[0]


def test_a_layout_naming_no_corvette_is_an_orphan_unless_its_owner_time_ties_it_to_one():
    ships = [corvette(), corvette(SEED + 100)]
    bases = [layout(0), layout(1, ts=SEED + 100), layout(4), layout(7, ts=SEED + 1)]
    remove, report = tool.orphans_in(psd(ships, bases), False)
    assert remove == [2]                                   # UserData 4: no corvette, no time
    assert "owner time" in report[3] and "--include-ts-linked" in report[3]
    assert tool.orphans_in(psd(ships, bases), True)[0] == [2, 3]


def test_two_layouts_naming_one_corvette_s_slot_are_refused():
    with pytest.raises(tool.Refused, match="ship slot 0"):
        tool.orphans_in(psd([corvette()], [layout(0), layout(0)]), False)


def test_a_corvette_with_no_layout_naming_its_slot_does_not_stop_the_report():
    remove, _report = tool.orphans_in(psd([corvette()], [layout(3, ts=SEED)]), False)
    assert remove == []                                    # kept by time, reported
