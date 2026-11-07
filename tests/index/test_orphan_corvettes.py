"""Orphan corvette layouts: which layout a corvette owns (by time, else by UserData), the count
in the facts and the view, and the words the Organizer shows."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from nms_save_vault.core.hashname import hash_name
from nms_save_vault.core.index import derive, extract, privacy
from nms_save_vault.core.savejson import decode_save

from ..conftest import steam_save
from ..liveops_support import T0, steam_world
from .savebuilder import context_save, encode

CORVETTE = "MODELS/COMMON/SPACECRAFT/BIGGS/BIGGS.SCENE.MBIN"
FIGHTER = "MODELS/COMMON/SPACECRAFT/FIGHTERS/FIGHTER_PROC.SCENE.MBIN"
MADE_A, MADE_B = 1_756_659_119, 1_756_824_620      # the corvettes' seeds: when each was made


def _layout(user_data: int, ts: int, parts: int, btype: str = "PlayerShipBase") -> dict:
    return {"BaseType": {"PersistentBaseTypes": btype}, "Name": "Default", "UserData": user_data,
            "GalacticAddress": 0, "Position": [0.0, 0.0, 0.0],
            "Objects": [{"ObjectID": "^B_HAB"} for _ in range(parts)],
            "Owner": {"UID": "SYNTH-UID", "USN": "", "LID": "", "PTK": "ST", "TS": ts}}


def _psd(layouts: list[dict]) -> dict:
    return {"ShipOwnership": [
                {"Name": "Runner", "Resource": {"Filename": FIGHTER, "Seed": [True, "0x00ABCDEF"]}},
                {"Name": "", "Resource": {"Filename": CORVETTE, "Seed": [True, f"0x{MADE_A:X}"]}},
                {"Name": "Anvil", "Resource": {"Filename": CORVETTE, "Seed": f"0x{MADE_B:016X}"}},
                {"Name": "", "Resource": {"Filename": ""}}],
            "PersistentPlayerBases": layouts}


def _bases(layouts: list[dict], **kw) -> dict:
    data, hint = encode(context_save(base=_psd(layouts), **kw))
    facts = extract.run_extractors(decode_save(data, bare_size_hint=hint))
    assert facts["status"] == "ok", facts["errors"]
    return facts


def test_a_corvette_owns_the_layout_made_in_its_seed_second_whatever_its_user_data():
    # The real case: both corvettes' layouts carry UserData 4 and 5, slots that hold no corvette.
    facts = _bases([_layout(4, MADE_A + 1, 48), _layout(5, MADE_B, 85)])
    bases = facts["contexts"]["base"]["bases"]
    assert bases["orphan_corvette_layouts"] == 0
    assert sorted(b["parts"] for b in bases["own"]) == [48, 85]


def test_a_layout_no_corvette_owns_by_time_or_by_slot_is_an_orphan():
    facts = _bases([_layout(4, MADE_A, 48), _layout(9, 1_758_306_734, 26), _layout(3, MADE_B + 2, 19),
                    _layout(4444444444, MADE_B, 173, btype="UITempShipBase")])
    bases = facts["contexts"]["base"]["bases"]
    assert bases["orphan_corvette_layouts"] == 2            # UserData 9 and 3: empty slots, wrong times
    assert [b["parts"] for b in bases["own"]] == [48]        # the editor scratch is not a base


def test_a_layout_whose_user_data_is_a_corvette_slot_is_kept():
    facts = _bases([_layout(4, MADE_A, 48), _layout(5, MADE_B, 85), _layout(2, 1, 10)])
    assert facts["contexts"]["base"]["bases"]["orphan_corvette_layouts"] == 0


def test_no_owner_id_or_name_reaches_the_facts():
    blob = repr(_bases([_layout(9, 1, 26)]))
    assert "SYNTH-UID" not in blob


def test_the_view_counts_orphans_in_every_context():
    facts = {"schema": 1, "extractor": 2, "status": "ok", "primary": "base",
             "contexts": {"base": {"bases": {"orphan_corvette_layouts": 1}},
                          "expedition": {"bases": {"orphan_corvette_layouts": 2}}}}
    assert derive.view("ab" * 32, facts).orphan_corvettes == 3
    assert derive.view("ab" * 32, dict(facts, contexts={})).orphan_corvettes == 0


@pytest.mark.parametrize(("ts", "expected"), [(1_756_659_120, 1_756_659_120), (True, None), ("1", None),
                                              (None, None), (1.5, None)])
def test_owner_time_returns_a_time_only(ts, expected):
    owner = {hash_name("UID"): "SENTINEL", hash_name("TS"): ts}
    assert privacy.owner_time({hash_name("Owner"): owner}) == expected


def test_owner_time_tolerates_odd_shapes():
    assert privacy.owner_time(None) is None
    assert privacy.owner_time({hash_name("Owner"): "x"}) is None


# --- the Organizer --------------------------------------------------------------------------

def test_the_organizer_says_a_save_has_orphans_only_from_fresh_facts(tmp_path):
    from nms_save_vault.ui.organizer import detail, factsui, table

    world = steam_world(tmp_path)
    steam_save(world.live, 1, 0, "Main", 7200, 0xDDDAD1BDD39F9C72, T0 + 100, extra="main-a" + world.live.name)
    world.refresh()
    model = world.ctx.readmodel
    line = model.lines()[0]
    row = model.versions(line.pid)[0]
    fresh, stale = SimpleNamespace(stale=False, orphan_corvettes=2), SimpleNamespace(stale=True, orphan_corvettes=2)

    assert factsui.ORPHAN_WORDS in table.state_words(line, fresh)
    assert factsui.ORPHAN_WORDS not in table.state_words(line, stale)
    assert factsui.ORPHAN_WORDS not in table.state_words(line)
    assert factsui.ORPHAN_WORDS in table.version_values(row, {}, fresh, True)["state"]
    assert "2 corvette layouts that no corvette owns" in detail._version_state(dataclasses.replace(row, facts=fresh))
    assert "corvette layout" not in detail._version_state(dataclasses.replace(row, facts=None))
