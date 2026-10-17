"""Extractor framework and the first extractors: save basics, season, discoveries, context
basics, difficulty, words, products, stats (D4 step 8)."""
from __future__ import annotations

import pytest

from nms_save_vault.core.index import extract, schema
from nms_save_vault.core.savejson import decode_save

from .savebuilder import context_save, encode, pre_context_save


def facts_of(readable: dict, **kw) -> dict:
    data, hint = encode(readable, **kw)
    return extract.run_extractors(decode_save(data, bare_size_hint=hint))


def psd(**fields) -> dict:
    base = {"TimeStamp": 1_790_000_000, "UniverseAddress": {"RealityIndex": 9}, "SaveSummary": "On Planet",
            "Units": 1000, "Nanites": 20, "Specials": 300, "KnownPortalRunes": 0b1011, "HasAccessToNexus": True,
            "BannerTitleId": "^T_EXPD8", "TeleportEndpoints": [{}, {}], "KnownTech": ["^A", "^B", "^C"]}
    base.update(fields)
    return base


def test_a_base_only_save_has_one_context_and_save_facts():
    common = {"SaveName": "Synth Main", "TotalPlayTime": 7200, "SaveUniversalId": "0xA1B2C3D400000001"}
    f = facts_of(context_save(base=psd(), common=common, version=4740))
    assert f["status"] == "ok" and f["errors"] == []
    assert f["schema"] == 1 and f["extractor"] == schema.EXTRACTOR_VERSION
    assert f["primary"] == "base" and list(f["contexts"]) == ["base"]
    assert f["save"]["layout"] == "context"
    assert (f["save"]["json_version"], f["save"]["active_context"]) == (4740, "Main")
    assert (f["save"]["save_name"], f["save"]["play_time_s"]) == ("Synth Main", 7200)
    assert f["save"]["pid_in_data"] == "0xA1B2C3D400000001"
    assert f["container"]["kind"] == "chunked" and f["container"]["chunks"] == 1


def test_context_basics():
    f = facts_of(context_save(base=psd(Units=-1)))
    b = f["contexts"]["base"]
    assert b["game_mode"] == 1 and b["timestamp"] == 1_790_000_000 and b["galaxy"] == 9
    assert b["summary"] == "On Planet"
    assert (b["units"], b["nanites"], b["quicksilver"]) == (4_294_967_295, 20, 300)
    assert b["glyphs"] == 3 and b["nexus"] is True and b["title"] == "T_EXPD8"
    assert (b["teleporters"], b["known_tech"]) == (2, 3)


def test_glyphs_count_only_the_sixteen_rune_bits():
    f = facts_of(context_save(base=psd(KnownPortalRunes=0x1FFFF)))
    assert f["contexts"]["base"]["glyphs"] == 16


@pytest.mark.parametrize(("kw", "contexts", "primary"), [
    ({"base": "b", "expedition": "e"}, ["base", "expedition"], "base"),
    ({"expedition": "e"}, ["expedition"], "expedition"),
])
def test_contexts_and_primary(kw, contexts, primary):
    f = facts_of(context_save(**{k: psd() for k in kw}))
    assert list(f["contexts"]) == contexts and f["primary"] == primary
    if "expedition" in kw:
        assert f["contexts"]["expedition"]["game_mode"] == 6


def test_a_pre_context_save_reads_name_and_play_time_from_player_state():
    f = facts_of(pre_context_save(psd(SaveName="Old Save", TotalPlayTime=99)), unframed=True)
    assert f["save"]["layout"] == "pre-context"
    assert (f["save"]["save_name"], f["save"]["play_time_s"], f["save"]["season"]) == ("Old Save", 99, None)
    assert f["save"]["pid_in_data"] is None
    assert list(f["contexts"]) == ["base"] and f["contexts"]["base"]["game_mode"] is None
    assert f["container"]["kind"] == "unframed"


def test_a_save_with_no_context_is_partial_with_a_layout_error():
    f = facts_of(context_save())
    assert f["status"] == "partial" and f["contexts"] == {} and f["primary"] is None
    assert [e["extractor"] for e in f["errors"]] == ["layout"]


def season_common(values, amounts=None, optional=()):
    amounts = amounts if amounts is not None else [1] * len(values)
    stages = [{"Milestones": [{"Amount": a, "IsOptional": i in optional} for i, a in enumerate(amounts)]}]
    return {"SaveName": "Exp", "TotalPlayTime": 1,
            "SeasonData": {"SeasonId": 46, "SeasonNumber": 46, "DisplayNumber": 22, "SeasonName": "^UI_SEASON_22_NAME",
                           "Stages": stages},
            "SeasonState": {"MilestoneValues": values, "HasCollectedFinalReward": True},
            "EarnedSeasonSpecialRewards": ["^A", "^B"]}


def test_season_milestones_carry_their_embedded_targets():
    f = facts_of(context_save(expedition=psd(), common=season_common([150, 0, 1.0], [150, 150, 1], optional=(2,))))
    s = f["save"]["season"]
    assert (s["id"], s["number"], s["display"], s["name_key"]) == (46, 46, 22, "UI_SEASON_22_NAME")
    assert s["milestones"] == [[150, 150, False], [0, 150, False], [1.0, 1, True]]
    assert (s["final_reward"], s["special_rewards"]) == (True, 2)
    assert f["status"] == "ok"


def test_misaligned_targets_fall_back_to_amount_one_and_say_so():
    f = facts_of(context_save(expedition=psd(), common=season_common([150, 0], [150])))
    assert f["save"]["season"]["milestones"] == [[150, 1, False], [0, 1, False]]
    assert f["status"] == "partial"
    assert f["errors"] == [{"extractor": "season", "error": "season: milestone targets missing"}]


def test_discoveries_are_counted_by_type():
    recs = [{"DD": {"DT": "Planet"}}, {"DD": {"DT": "Planet"}}, {"DD": {"DT": "SolarSystem"}}, {"DD": {}}]
    f = facts_of(context_save(base=psd(), discoveries=recs))
    assert f["save"]["discoveries"] == {"total": 4, "by_type": {"Planet": 2, "SolarSystem": 1}}


def test_difficulty():
    state = {"Preset": {"DifficultyPresetType": "Custom"}, "EasiestUsedPreset": {"DifficultyPresetType": "Creative"},
             "HardestUsedPreset": {"DifficultyPresetType": "Normal"},
             "Settings": {"SettingsLocked": False, "CraftingIsFree": True,
                          "DeathConsequences": {"DeathConsequencesDifficulty": "ItemGrave"}}}
    f = facts_of(context_save(base=psd(DifficultyState=state)))
    assert f["contexts"]["base"]["difficulty"] == {"preset": "Custom", "easiest": "Creative", "hardest": "Normal",
                                                   "death": "ItemGrave", "crafting_free": True, "locked": False}


def test_active_context_and_difficulty_labels_are_normalised():
    bad = b"Cus\xfftom".decode("utf-8", errors="surrogateescape")
    long = "N" * 400
    state = {"Preset": {"DifficultyPresetType": bad}, "EasiestUsedPreset": {"DifficultyPresetType": long},
             "HardestUsedPreset": {"DifficultyPresetType": ""},
             "Settings": {"DeathConsequences": {"DeathConsequencesDifficulty": "<X>" + bad}}}
    root = context_save(base=psd(DifficultyState=state))
    root["ActiveContext"] = bad + long
    f = facts_of(root)
    d = f["contexts"]["base"]["difficulty"]
    assert d["preset"] == "Cus�tom" and d["death"] == "Cus�tom"
    assert d["easiest"] == "N" * 64 and d["hardest"] is None
    active = f["save"]["active_context"]
    assert len(active) == 64 and active.startswith("Cus�tom")
    for v in (d["preset"], d["death"], active):
        v.encode("utf-8")


def test_words_count_per_race_in_the_game_order():
    groups = [{"Races": [True, False, False, False, True, False, False, False, True]},
              {"Races": [True, True, True, False, False]},
              {"Races": "odd"}]
    f = facts_of(context_save(base=psd(KnownWordGroups=groups)))
    assert f["contexts"]["base"]["words"] == {"gek": 2, "vykeen": 1, "korvax": 1, "atlas": 1, "autophage": 1}


def test_known_products_and_legacy_parts_still_buildable():
    bad = b"^PR\xffOD".decode("utf-8", errors="surrogateescape")
    known = ["^C_WALL", "^M_FLOOR", "^W_ROOF", "^S_CONTAINER3", "^NOT_LEGACY", "^C_WALL", bad]
    f = facts_of(context_save(base=psd(KnownProducts=known)))
    b = f["contexts"]["base"]
    assert b["known_products"] == 6
    assert b["legacy_known"] == {"structures": 3, "freighter": 1}


def test_curated_stats_only_without_carets():
    group = {"GroupId": "^GLOBAL_STATS", "Stats": [
        {"Id": "^TIME", "Value": {"FloatValue": 69332.77}}, {"Id": "^MONEY", "Value": {"IntValue": 489}},
        {"Id": "^ATLAS_PATH", "Value": {}}, {"Id": "^CARGO_SPECIAL", "Value": {"IntValue": 5}}]}
    other = {"GroupId": "^PLANET_STATS", "Stats": [{"Id": "^TIME", "Value": {"IntValue": 1}}]}
    f = facts_of(context_save(base=psd(Stats=[other, group])))
    assert f["contexts"]["base"]["stats"] == {"ATLAS_PATH": 0, "MONEY": 489, "TIME": 69332.77}


def test_a_failing_extractor_blanks_only_its_fields(monkeypatch):
    readable = context_save(base=psd(), expedition=psd())
    data, _ = encode(readable)
    good = extract.run_extractors(decode_save(data))

    def boom(ctx, out):
        out["units"] = 1
        raise ValueError("SENTINEL value from the save")

    broken = [e if e.name != "ctx_basics" else extract.Extractor(e.name, e.scope, e.fields, boom)
              for e in extract.REGISTRY]
    f = extract.run_extractors(decode_save(data), broken)
    assert f["status"] == "partial"
    assert [e["extractor"] for e in f["errors"]] == ["base.ctx_basics", "expedition.ctx_basics"]
    assert f["errors"][0]["error"].startswith("ValueError at test_extract_basics.py:")
    assert "SENTINEL" not in repr(f)
    fields = next(e for e in extract.REGISTRY if e.name == "ctx_basics").fields
    for ctx in ("base", "expedition"):
        expected = {k: v for k, v in good["contexts"][ctx].items() if k not in fields}
        assert f["contexts"][ctx] == expected
    assert f["save"] == good["save"]


def test_odd_shapes_degrade_to_none_not_errors():
    weird = psd(Units="lots", UniverseAddress=[1], KnownPortalRunes=None, TeleportEndpoints={}, KnownTech=7,
                SaveSummary={"x": 1}, DifficultyState="?", KnownWordGroups={}, KnownProducts=None, Stats={})
    f = facts_of(context_save(base=weird, common={"SaveName": 5, "TotalPlayTime": "x", "SaveUniversalId": 0}))
    assert f["status"] == "ok"
    b = f["contexts"]["base"]
    assert (b["units"], b["galaxy"], b["glyphs"], b["teleporters"], b["known_tech"], b["summary"]) == (None, None, None, 0, 0, "")
    assert (f["save"]["save_name"], f["save"]["play_time_s"], f["save"]["pid_in_data"]) == ("", None, None)


def test_every_extractor_field_is_declared_in_the_schema():
    for ex in extract.REGISTRY:
        spec = schema.SAVE_SPEC if ex.scope == "save" else schema.CONTEXT_SPEC
        assert set(ex.fields) <= set(spec), ex.name
