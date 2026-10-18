"""What changed (``core/index/changes.py``): one test per noise rule N1-N16, headers, milestones."""
from __future__ import annotations

import copy

from nms_save_vault.core.index import changes, lookups
from nms_save_vault.core.index.changes import changes as diff
from nms_save_vault.core.index.lookups import _overrides

FIGHTER = "MODELS/COMMON/SPACECRAFT/FIGHTERS/FIGHTER_PROC.SCENE.MBIN"
WRAITH = "MODELS/COMMON/SPACECRAFT/S-CLASS/BIOPARTS/BIOFIGHTER.SCENE.MBIN"


def base(bid, name, parts, *, btype="HomePlanetBase", galaxy=0, layout=None, legacy=0):
    return {"id": bid, "name": name, "type": btype, "galaxy": galaxy, "address": None, "parts": parts,
            "legacy_structures": legacy, "legacy_freighter": 0, "orig_version": 8, "layout": layout or f"L{parts}"}


def ctx(**kw):
    c = {"glyphs": 16, "units": 1000, "nanites": 100, "quicksilver": 50, "galaxy": 0, "frigates": 2,
         "difficulty": {"preset": "Normal", "death": "ItemGrave"}, "words": {"gek": 1},
         "missions": {"version": 40, "current": "", "progress": {}}, "stats": {"DISC_SYSTEMS": 10, "JM": 10},
         "bases": {"own": [], "external_cached": 0, "orphan_corvette_layouts": 0, "unknown_types": {}, "ptk": []},
         "ships": [], "companions": [], "freighter": {"has": False}, "settlements": {"claimed": 0, "names": []},
         "inventories": {}}
    c.update(kw)
    return c


def facts(play=3600, name="Synth", season=None, active="Main", **contexts):
    contexts = contexts or {"base": ctx()}
    return {"schema": 1, "extractor": 1, "status": "ok", "errors": [], "container": None,
            "save": {"save_name": name, "play_time_s": play, "active_context": active,
                     "season": season or {"id": 0, "number": 1, "display": 0, "name_key": "", "milestones": []}},
            "contexts": contexts, "primary": "base" if "base" in contexts else "expedition"}


def kinds(cs) -> list[str]:
    return [c.kind for c in cs.items]


def test_n1_the_given_parent_is_the_only_comparison():
    p, c = facts(play=0), facts(play=7200)
    cs = diff(c, p, child_vid="c" * 64, parent_vid="p" * 64)
    assert (cs.child, cs.parent) == ("c" * 64, "p" * 64)
    assert diff(c, p).header == diff(copy.deepcopy(c), copy.deepcopy(p)).header == "+2.0 h played"


def test_n2_other_players_bases_and_orphan_layouts_are_not_changes():
    p = facts()
    c = facts(base=ctx(bases={"own": [], "external_cached": 9, "orphan_corvette_layouts": 3, "unknown_types": {},
                              "ptk": []}))
    assert kinds(diff(c, p)) == []


def test_n3_moving_vehicles_and_rematched_bases_are_not_gone_and_new():
    p = facts(base=ctx(bases={"own": [base("freighter", "F", 50, btype="FreighterBase"),
                                      base("HomePlanetBa:aaa", "Home", 100, galaxy=3)]}))
    c = facts(base=ctx(bases={"own": [base("freighter", "F", 50, btype="FreighterBase"),
                                      base("HomePlanetBa:bbb", "Home", 100, galaxy=3)]}))
    assert kinds(diff(c, p)) == []


def test_n4_only_parts_and_layout_count_not_object_churn():
    same = facts(base=ctx(bases={"own": [base("b1", "Home", 100, layout="X")]}))
    assert kinds(diff(copy.deepcopy(same), same)) == []
    moved = facts(base=ctx(bases={"own": [base("b1", "Home", 100, layout="Y")]}))
    assert kinds(diff(moved, same)) == ["base.minor"]


def test_n5_small_deltas_fold_and_the_three_largest_others_show():
    p = facts(base=ctx(bases={"own": [base(f"b{i}", f"B{i}", 100) for i in range(7)]}))
    grown = [base("b0", "B0", 101), base("b1", "B1", 102), base("b2", "B2", 110), base("b3", "B3", 130),
             base("b4", "B4", 90), base("b5", "B5", 105), base("b6", "B6", 100)]
    cs = diff(facts(base=ctx(bases={"own": grown})), p)
    texts = [c.text for c in cs.items]
    assert texts[:3] == ["base 'B3' parts +30", "base 'B2' parts +10", "base 'B4' parts -10"]
    assert "1 more base changed" in texts
    assert "minor edits in 2 bases (+3 parts)" in texts


def test_n6_discoveries_come_from_the_monotone_stats():
    p = facts(base=ctx(stats={"DISC_SYSTEMS": 10, "DISC_PLANETS": 3, "DISC_CREATURES": 1}))
    c = facts(base=ctx(stats={"DISC_SYSTEMS": 44, "DISC_PLANETS": 6, "DISC_CREATURES": 15}))
    assert [x.text for x in diff(c, p).items] == ["discovered +34 systems, +3 planets, +14 creatures"]


def test_n7_a_resave_is_one_header_line():
    p = facts()
    c = facts(base=ctx(units=5))
    cs = diff(c, p, edge_kind="touch")
    assert (cs.header, cs.items) == ("no progress (re-saved)", ())


def season(number, display, key, values=(), amounts=(), final=False):
    return {"id": number, "number": number, "display": display, "name_key": key,
            "milestones": [[v, a, False] for v, a in zip(values, amounts)], "final_reward": final}


def test_n8_a_replaced_expedition_is_one_line():
    p = facts(season=season(45, 21, "UI_SEASON_21_NAME"), base=ctx(), expedition=ctx(units=1))
    c = facts(season=season(46, 22, "UI_SEASON_22_NAME"), base=ctx(), expedition=ctx(units=999, glyphs=3))
    cs = diff(c, p)
    assert [x.text for x in cs.items] == ["new expedition: Swarm (replaces Remnant)"]
    same_number = facts(season=season(46, 22, "UI_SEASON_22_NAME"), base=ctx(stats={"TIME": 100.0}),
                        expedition=ctx(stats={"TIME": 5.0}, units=999))
    before = facts(season=season(46, 22, "UI_SEASON_22_NAME"), base=ctx(stats={"TIME": 90.0}),
                   expedition=ctx(stats={"TIME": 50.0}))
    assert kinds(diff(same_number, before)) == ["expedition.replaced"]       # expedition time fell


def test_n9_an_expedition_converted_into_a_normal_save():
    p = facts(active="Season", expedition=ctx())
    c = facts(active="Main", base=ctx())
    assert kinds(diff(c, p)) == ["expedition.converted"]                     # no context switch line


def test_n10_editor_sessions_and_inventory_weights():
    inv_a = {"exosuit": {"slots": 3, "capacity": 10, "digest": "a"}}
    inv_b = {"exosuit": {"slots": 4, "capacity": 10, "digest": "b"}}
    p = facts(play=1000, base=ctx(inventories=inv_a))
    edited = diff(facts(play=1000, base=ctx(inventories=inv_b)), p, edge_kind="edit")
    assert edited.header == "edited outside the game"
    assert [(x.text, x.weight) for x in edited.items] == [("inventory changed: exosuit (+1 slot)", 75)]
    played = diff(facts(play=9000, base=ctx(inventories=inv_b)), p, edge_kind="play")
    assert played.items[0].weight == 8


def test_n11_currencies_in_one_line_quicksilver_first_units_compact():
    p = facts(base=ctx(units=1000, nanites=100, quicksilver=50))
    c = facts(base=ctx(units=2_629_509, nanites=3_918, quicksilver=1_550))
    assert [x.text for x in diff(c, p).items] == ["quicksilver +1,500; nanites +3,818; units +2.6M"]
    small = facts(base=ctx(units=19_000, nanites=100, quicksilver=50))
    assert [x.text for x in diff(small, p).items] == ["units +18,000"]


def test_n12_difficulty_and_death_in_one_line():
    p = facts(base=ctx(difficulty={"preset": "Custom", "death": "None"}))
    c = facts(base=ctx(difficulty={"preset": "Normal", "death": "ItemGrave"}))
    assert [(x.text, x.weight) for x in diff(c, p).items] == [("difficulty Custom -> Normal (death: None -> ItemGrave)", 30)]
    d = facts(base=ctx(difficulty={"preset": "Custom", "death": "ItemGrave"}))
    assert [x.text for x in diff(d, p).items] == ["death consequences None -> ItemGrave"]


def ship(slot, name, seed, model=FIGHTER, cls="S"):
    return {"slot": slot, "name": name, "model_path": model, "cls": cls, "seed": seed, "primary": False}


def test_n13_ships_by_seed_and_model_renames_apart_several_collapse():
    p = facts(base=ctx(ships=[ship(0, "Old Name", "0x1"), ship(1, "Gone", "0x2")]))
    c = facts(base=ctx(ships=[ship(0, "New Name", "0x1", cls="A"), ship(2, "", "0x3"), ship(3, "W", "0x4", WRAITH)]))
    texts = [x.text for x in diff(c, p).items]
    assert "2 new ships (Fighter, The Wraith)" in texts
    assert "ship 'Gone' gone (Fighter)" in texts
    assert "ship 'New Name' class S -> A" in texts
    assert "ship 'Old Name' renamed 'New Name'" in texts
    one = facts(base=ctx(ships=[ship(0, "Old Name", "0x1"), ship(1, "Gone", "0x2"), ship(5, "", "0x9", WRAITH)]))
    assert [x.text for x in diff(one, p).items] == ["new ship 'The Wraith' (The Wraith, S)"]


def test_n14_inventories_grouped_by_kind_with_the_net_slot_change():
    def inv(**containers):
        return {k: {"slots": s, "capacity": 50, "digest": d} for k, (s, d) in containers.items()}

    p = facts(play=0, base=ctx(inventories={**inv(exosuit=(10, "a"), storage=(5, "s")),
                                            "ship:0": {"slots": 20, "capacity": 50, "digest": "x"},
                                            "ship:1": {"slots": 20, "capacity": 50, "digest": "y"}}))
    c = facts(play=0, base=ctx(inventories={**inv(exosuit=(33, "b"), storage=(5, "s")),
                                            "ship:0": {"slots": 4, "capacity": 50, "digest": "x2"},
                                            "ship:1": {"slots": 4, "capacity": 50, "digest": "y2"}}))
    assert [x.text for x in diff(c, p).items] == ["inventory changed: exosuit (+23 slots), 2 ships (-32 slots)"]


def test_n15_stats_that_go_down_are_never_reported():
    p = facts(base=ctx(stats={"DISC_SYSTEMS": 50, "EXP_STANDING": 30, "MEDITATION": 9}))
    c = facts(base=ctx(stats={"DISC_SYSTEMS": 40, "EXP_STANDING": 10, "MEDITATION": 1}))
    assert kinds(diff(c, p)) == []


def test_n16_items_sorted_by_weight_then_magnitude_top_three():
    p = facts(base=ctx(glyphs=2, bases={"own": []}))
    c = facts(base=ctx(glyphs=16, quicksilver=60, bases={"own": [base("a", "Small", 10), base("b", "Big", 200)]}))
    cs = diff(c, p)
    assert [x.kind for x in cs.items] == ["base.new", "base.new", "galaxy.first_base", "glyphs", "currencies"]
    assert cs.items[0].text == "new base 'Big' (200 parts, Euclid)"
    assert len(cs.top()) == 3 and cs.lines(2) == [cs.header, cs.items[0].text, cs.items[1].text]


def test_headers_per_edge_kind_and_play_time():
    p = facts(play=1000)
    assert diff(facts(play=1000 + 9720), p, edge_kind="play").header == "+2.7 h played"
    assert diff(facts(play=1000), p, edge_kind="unknown").header == "no progress (re-saved)"
    assert diff(facts(play=1240), p, edge_kind="play").header == "+4 min played, nothing indexed changed"
    assert diff(facts(play=1240, base=ctx(glyphs=1)), p).header == "+4 min played"
    assert diff(facts(play=1000), p, edge_kind="edit").header == "edited outside the game"


def test_milestones_and_the_key_version_rule():
    final = {m: lookups.final_stage(m, 40) for m in _overrides.CHAINS["artemis"][1]}
    p = facts(base=ctx(missions={"version": 40, "current": "", "progress": dict(list(final.items())[:29])}))
    c = facts(base=ctx(missions={"version": 40, "current": "", "progress": final}))
    cs = diff(c, p)
    assert [(x.kind, x.text) for x in cs.items] == [("milestone.complete", "Artemis Path completed")]
    assert changes.is_milestone(cs)
    step = facts(base=ctx(missions={"version": 40, "current": "", "progress": dict(list(final.items())[:5])}))
    first = facts(base=ctx(missions={"version": 40, "current": "", "progress": dict(list(final.items())[:4])}))
    assert [x.text for x in diff(step, first).items] == ["Artemis Path 4 -> 5 of 30"]
    assert not changes.is_milestone(diff(facts(base=ctx(glyphs=3)), facts()))


def test_expedition_progress_start_and_items_marked_by_context():
    s1 = season(46, 22, "UI_SEASON_22_NAME", (1, 0, 0), (1, 1, 1))
    s2 = season(46, 22, "UI_SEASON_22_NAME", (1, 1, 0), (1, 1, 1))
    p = facts(season=s1, base=ctx(), expedition=ctx(glyphs=0))
    c = facts(season=s2, base=ctx(), expedition=ctx(glyphs=2))
    texts = [x.text for x in diff(c, p).items]
    assert texts == ["[expedition] glyphs 0 -> 2 of 16", "expedition Swarm: milestones 1 -> 2 of 3"]
    started = diff(facts(season=s1, base=ctx(), expedition=ctx()), facts())
    assert [x.text for x in started.items] == ["expedition started: Swarm"]


def test_change_sets_are_json_safe():
    import json
    cs = diff(facts(base=ctx(glyphs=1)), facts())
    assert json.loads(json.dumps(cs.to_dict()))["items"][0]["kind"] == "glyphs"


def test_a_new_ship_of_unknown_class_leaves_the_class_out():
    p = facts(base=ctx())
    c = facts(base=ctx(ships=[ship(0, "", "0x1", cls=None)]))
    assert [x.text for x in diff(c, p).items] == ["new ship 'Fighter' (Fighter)"]


def test_a_new_freighter_of_unknown_class_leaves_the_class_out():
    p = facts(base=ctx())
    c = facts(base=ctx(freighter={"has": True, "name": "Hauler", "cls": None}))
    assert [x.text for x in diff(c, p).items] == ["freighter acquired 'Hauler'"]
