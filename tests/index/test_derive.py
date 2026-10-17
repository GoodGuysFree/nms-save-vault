"""Read-time views (``core/index/derive.py``): FactsView, chips, numbers and the FactsCard."""
from __future__ import annotations

import dataclasses
import json

from nms_save_vault.core.index import derive, extract, lookups, schema
from nms_save_vault.core.savejson import decode_save

from .fixtures import FIXTURES
from .savebuilder import encode

VID = "ab" * 32


def facts_for(name: str) -> dict:
    readable, kw = FIXTURES[name]()
    data, hint = encode(readable, **kw)
    facts = extract.run_extractors(decode_save(data, bare_size_hint=hint))
    for ctx in facts["contexts"].values():
        progress = ctx.get("missions", {}).get("progress", {})
        if "ACT1_STEP1" in progress:
            progress["ACT1_STEP1"] = 7          # the fixture's 6 is one short of the version-40 final stage
    return facts


def test_the_owner_facts_of_a_rich_base_save():
    v = derive.view(VID, facts_for("rich_base"))
    assert (v.vid, v.status, v.stale, v.primary, v.active) == (VID, "ok", False, "base", "base")
    assert (v.save_name_in_data, v.pid_in_data) == ("Synth Main", "0x5EED00000000000A")
    assert v.story == (1, 30) and v.atlas == (0, 11)          # ACT1_STEP1 at its final stage; ATLAS_PATH1 is no chain id
    assert v.story_ending == "new galaxy"                    # EPILOGUE_NEWGAL started = chosen
    assert v.glyphs == 16
    assert (v.own_bases, v.base_parts) == (5, 13)             # the orphan corvette layout is not counted
    assert (v.planet_bases, v.planet_base_parts) == (3, 8)
    assert v.largest_base == ("Synth Home", 5)
    assert v.galaxies_with_bases == (("Eissentam", 2), ("Loychazinq", 1))     # galaxy 9 and 20
    assert (v.legacy_placed, v.legacy_placed_bases, v.legacy_buildable) == (6, 2, (3, 2))
    assert (v.ships, v.corvettes, v.archived_ships) == (2, 1, 1)
    assert v.ship_names == ("Synth Runner", "Big Synth")      # primary first
    assert (v.companions, v.companion_names, v.eggs) == (2, ("Biscuit", "Quad"), 1)
    assert v.freighter == "Synth Hauler (S)" and v.frigates == 2
    assert (v.quicksilver, v.nanites, v.units) == (1500, 2500, 4_294_967_295)
    assert (v.words, v.deaths, v.systems_discovered, v.last_played) == (6, 3, 12, 1_790_000_000)
    assert v.current_galaxy == "Eissentam"
    assert v.expedition is None                              # SeasonId 0
    assert v.lookups == lookups.LOOKUP_VERSION


def test_chips_are_ascii_badges():
    v = derive.view(VID, facts_for("rich_base"))
    assert v.chips == ("Story 1/30", "Glyphs 16", "Legacy parts", "Legacy buildable")
    e = derive.view(VID, facts_for("base_and_expedition"))
    assert "Exp 22 Swarm 2/3" in e.chips
    assert all(c.isascii() for c in v.chips + e.chips)


def test_an_expedition_carries_name_number_and_progress_against_its_targets():
    v = derive.view(VID, facts_for("base_and_expedition"))
    assert v.primary == "base" and v.active == "expedition"
    assert v.expedition == {"name": "Swarm", "number": 22, "done": 2, "total": 3, "final_reward": False,
                            "active": True}
    solo = derive.view(VID, facts_for("expedition_only"))
    assert solo.primary == "expedition" and solo.expedition["active"] is True


def test_every_numeric_field_is_in_numbers_under_its_name():
    v = derive.view(VID, facts_for("rich_base"))
    numeric = {f.name for f in dataclasses.fields(v)
               if isinstance(getattr(v, f.name), (int, float)) and not isinstance(getattr(v, f.name), bool)}
    assert numeric <= set(v.numbers)
    assert v.numbers["story"] == 1 and v.numbers["largest_base"] == 5 and v.numbers["legacy_buildable"] == 5
    assert v.numbers["galaxies_with_bases"] == 2
    e = derive.view(VID, facts_for("base_and_expedition"))
    assert e.numbers["expedition"] == 2


def test_views_are_json_safe_and_mark_stale_rows():
    facts = facts_for("rich_base")
    v = derive.view(VID, facts)
    round_trip = json.loads(json.dumps(v.to_dict()))
    assert round_trip["story"] == [1, 30] and round_trip["chains"]["artemis"] == [1, 30]
    old = dict(facts, extractor=schema.EXTRACTOR_VERSION - 1)
    assert derive.view(VID, old).stale is True


def test_a_failed_version_still_gives_a_view():
    v = derive.view(VID, {"schema": 1, "extractor": schema.EXTRACTOR_VERSION, "status": "corrupt", "errors": [],
                          "container": None, "save": None, "contexts": {}, "primary": None})
    assert v.status == "corrupt" and v.story is None and v.own_bases == 0 and v.chips == ()


def test_fleet_text_is_the_owner_cell():
    v = derive.view(VID, facts_for("rich_base"))
    assert derive.fleet_text(v) == "2 (1 corvette) +1 archived"
    many = dataclasses.replace(v, ships=12, corvettes=3, archived_ships=5)
    assert derive.fleet_text(many) == "12 (3 corvettes) +5 archived"


def test_the_card_has_checklists_names_and_errors():
    facts = facts_for("rich_base")
    facts["errors"] = [{"extractor": "base.ships", "error": "ValueError at fleet.py:1"}]
    c = derive.card(VID, facts).to_dict()
    json.dumps(c)
    base = c["contexts"]["base"]
    steps = base["milestones"]["artemis"]["steps"]
    assert steps[0] == {"mission": "ACT1_STEP1", "state": "done", "text": "done"}
    assert steps[4] == {"mission": "ACT1_STEP5", "state": "started", "text": "stage 2 of 38"}
    assert steps[5]["text"] == "not started"
    assert base["choices"] == ["new galaxy"]
    assert [b["type"] for b in base["bases"]][:2] == ["Planet base", "Planet base"]
    assert base["bases"][0]["galaxy"] == "Eissentam"
    assert base["ships"][1] == {"name": "Big Synth", "type": "Corvette", "class": None, "primary": False}
    assert base["archived_ships"] == [{"name": "Old Synth", "type": "Hauler", "class": "A"}]
    assert base["companions"][1] == {"name": "Quad", "creature": "Quad", "named": False}
    assert base["stats"]["Deaths"] == 3 and base["inventories"]["exosuit"] == {"slots": 3, "capacity": 32}
    assert c["errors"] == [{"extractor": "base.ships", "error": "ValueError at fleet.py:1"}]
    e = derive.card(VID, facts_for("base_and_expedition")).to_dict()["expedition"]
    assert e["milestones"][1] == {"value": 40.0, "amount": 150.0, "optional": False, "done": False}
