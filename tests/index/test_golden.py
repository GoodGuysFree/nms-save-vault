"""Golden facts for the synthetic fixtures, missions and inventories (D4 step 10).

``NMSVAULT_REGEN_GOLDEN=1`` rewrites ``golden/*.json`` from the current extractors; review the
diff by hand before committing a regenerated file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nms_save_vault.core.hashname import hash_name
from nms_save_vault.core.index import extract
from nms_save_vault.core.savejson import decode_save

from .fixtures import FIXTURES
from .savebuilder import context_save, encode

GOLDEN = Path(__file__).parent / "golden"
REGEN = os.environ.get("NMSVAULT_REGEN_GOLDEN") == "1"


def facts_for(name: str, extractors=None) -> dict:
    readable, kw = FIXTURES[name]()
    data, hint = encode(readable, **kw)
    return extract.run_extractors(decode_save(data, bare_size_hint=hint), extractors)


def canonical(facts: dict) -> str:
    return json.dumps(facts, ensure_ascii=True, sort_keys=True, indent=1) + "\n"


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_facts_match_the_golden_file(name):
    facts = facts_for(name)
    path = GOLDEN / f"{name}.json"
    if REGEN:
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(canonical(facts), encoding="utf-8", newline="\n")
    assert canonical(facts) == path.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_a_failing_extractor_leaves_every_other_fact_as_golden(name):
    golden = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))

    def boom(ctx, out):
        out["ships"] = ["half written"]
        raise RuntimeError("injected")

    broken = [extract.Extractor(e.name, e.scope, e.fields, boom) if e.name == "ships" else e
              for e in extract.REGISTRY]
    facts = facts_for(name, broken)
    assert facts["status"] == "partial"
    assert {e["extractor"] for e in facts["errors"]} == {f"{c}.ships" for c in golden["contexts"]}
    for ctx, cf in golden["contexts"].items():
        expected = {k: v for k, v in cf.items() if k not in ("ships", "archived_ships")}
        assert facts["contexts"][ctx] == expected
    assert facts["save"] == golden["save"] and facts["container"] == golden["container"]


def test_the_golden_files_cover_every_extractor_field():
    seen_ctx: set[str] = set()
    seen_save: set[str] = set()
    for name in FIXTURES:
        golden = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
        assert golden["status"] == "ok", name
        seen_save |= set(golden["save"])
        for cf in golden["contexts"].values():
            seen_ctx |= set(cf)
    for ex in extract.REGISTRY:
        assert set(ex.fields) <= (seen_save if ex.scope == "save" else seen_ctx), ex.name


def test_the_rich_fixture_shows_the_hard_cases():
    base = json.loads((GOLDEN / "rich_base.json").read_text(encoding="utf-8"))["contexts"]["base"]
    b = base["bases"]
    assert (b["external_cached"], b["orphan_corvette_layouts"]) == (1, 1)
    planet = [x for x in b["own"] if x["type"] == "HomePlanetBase"]
    assert len({x["address"] for x in planet[:2]}) == 1 and len({x["id"] for x in planet[:2]}) == 2
    assert base["units"] == 4_294_967_295
    assert planet[0]["name"] == "Synth Home" and base["ships"][0]["name"] == "Synth Runner"
    assert base["legacy_known"] == {"structures": 3, "freighter": 2}
    assert base["settlements"] == {"claimed": 1, "names": ["Synth Haven"]}


# --- missions and inventories ------------------------------------------------------------------


def ctx_facts(**psd) -> dict:
    data, _ = encode(context_save(base=psd))
    f = extract.run_extractors(decode_save(data))
    assert f["status"] == "ok", f["errors"]
    return f["contexts"]["base"]


def test_missions_keep_raw_progress_of_known_chains_only():
    m = ctx_facts(MissionVersion=40, CurrentMissionID="^ACT2_STEP3", MissionProgress=[
        {"Mission": "^ACT1_STEP14", "Progress": 3}, {"Mission": "^ACT2_STEP3", "Progress": 0},
        {"Mission": "^EXPLORE_LOG", "Progress": 5}, {"Mission": "^PIRATES_1", "Progress": -1},
        {"Mission": "^ATLAS_PATH2", "Progress": 2147483647}, {"Mission": "^ART_DEAD", "Progress": "x"}])["missions"]
    assert m == {"version": 40, "current": "ACT2_STEP3",
                 "progress": {"ACT1_STEP14": 3, "ACT2_STEP3": 0, "ATLAS_PATH2": 2147483647}}


def inv(*slots, capacity=4):
    return {"ValidSlotIndices": list(range(capacity)),
            "Slots": [{"Id": i, "Amount": a, "MaxAmount": 10, "Index": {"X": x, "Y": y}} for i, a, x, y in slots]}


def test_inventory_digests_count_slots_and_change_with_content_only():
    one = ctx_facts(Inventory=inv(("^FUEL1", 3, 0, 0), ("^CARBON", 5, 1, 0)), Inventory_Cargo=inv(capacity=2))
    same_reordered = ctx_facts(Inventory=inv(("^CARBON", 5, 1, 0), ("^FUEL1", 3, 0, 0)), Inventory_Cargo=inv(capacity=2))
    more = ctx_facts(Inventory=inv(("^FUEL1", 4, 0, 0), ("^CARBON", 5, 1, 0)), Inventory_Cargo=inv(capacity=2))
    e1, e2, e3 = (f["inventories"]["exosuit"] for f in (one, same_reordered, more))
    assert (e1["slots"], e1["capacity"]) == (2, 6)
    assert e1["digest"] == e2["digest"] != e3["digest"]
    assert len(e1["digest"]) == 12


def test_inventories_per_container_kind_ship_and_multitool():
    fighter = "MODELS/COMMON/SPACECRAFT/FIGHTERS/FIGHTER_PROC.SCENE.MBIN"
    f = ctx_facts(ShipOwnership=[{"Resource": {"Filename": fighter}, "Inventory": inv(("^ROCKET1", 1, 0, 0))},
                                 {"Resource": {"Filename": ""}},
                                 {"Resource": {"Filename": fighter}}],
                  Multitools=[{"Resource": {"Filename": "STAFF.SCENE.MBIN"}, "Store": inv(("^LASER", 1, 0, 0))}],
                  Chest3Inventory=inv(("^FERRITE", 9, 0, 0)), VehicleOwnership=[{"Inventory": inv(("^FUEL1", 1, 0, 0))}])
    inventories = f["inventories"]
    assert sorted(inventories) == ["exosuit", "freighter", "multitool:0", "ship:0", "ship:2", "storage", "vehicles"]
    assert inventories["ship:0"]["slots"] == 1 and inventories["ship:2"]["slots"] == 0
    assert inventories["storage"]["slots"] == 1 and inventories["vehicles"]["slots"] == 1


def test_damaged_ids_still_digest_apart():
    a = b"^PROC\xff".decode("utf-8", errors="surrogateescape")
    b = b"^PROC\xfe".decode("utf-8", errors="surrogateescape")
    da = ctx_facts(Inventory=inv((a, 1, 0, 0)))["inventories"]["exosuit"]["digest"]
    db = ctx_facts(Inventory=inv((b, 1, 0, 0)))["inventories"]["exosuit"]["digest"]
    assert da != db


def test_inventory_digests_hold_no_item_ids():
    f = ctx_facts(Inventory=inv(("^SECRET_ITEM_ID", 1, 0, 0)))
    assert "SECRET_ITEM_ID" not in json.dumps(f)
    assert hash_name("Inventory")      # the readable name is what the extractor addresses
