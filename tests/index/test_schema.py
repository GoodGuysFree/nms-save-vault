"""Fact schema v1 (``core/index/schema.py``) and the synthetic save builder."""
from __future__ import annotations

import copy

import pytest

from nms_save_vault.core import formats
from nms_save_vault.core.hashname import hash_name
from nms_save_vault.core.index import schema
from nms_save_vault.core.savejson import decode_save

from .savebuilder import Raw, context_save, encode, obfuscate


def minimal_facts() -> dict:
    return {
        "schema": 1, "extractor": schema.EXTRACTOR_VERSION, "status": "ok", "errors": [],
        "container": {"kind": "chunked", "chunks": 1, "decompressed_bytes": 10, "decompressed_sha256": "ab",
                      "json_bytes": 9},
        "save": {"layout": "context", "json_version": 4720, "active_context": "Main", "save_name": "S",
                 "play_time_s": 60, "pid_in_data": None, "season": None,
                 "discoveries": {"total": 2, "by_type": {"Planet": 2}}},
        "contexts": {"base": {
            "units": 5, "nexus": False, "stats": {"TIME": 1.5, "MONEY": 3},
            "ships": [{"slot": 0, "name": "A", "model_path": "M", "cls": "S", "seed": None, "primary": True}],
            "missions": {"version": 40, "current": "X", "progress": {"ACT1_STEP1": 1}},
            "inventories": {"exosuit": {"slots": 1, "capacity": 2, "digest": "d"}},
        }},
        "primary": "base",
    }


def test_valid_facts_pass_unchanged():
    facts = minimal_facts()
    out, problems = schema.validate(copy.deepcopy(facts))
    assert problems == []
    assert out == facts


def test_undeclared_keys_are_dropped_and_reported_without_their_name():
    facts = minimal_facts()
    facts["save"]["SENTINEL_KEY"] = "x"
    facts["contexts"]["base"]["ships"][0]["owner"] = {"UID": "SENTINEL"}
    out, problems = schema.validate(facts)
    assert "SENTINEL_KEY" not in out["save"]
    assert "owner" not in out["contexts"]["base"]["ships"][0]
    assert problems == ["facts.save: dropped an undeclared key", "facts.contexts.base.ships[0]: dropped an undeclared key"]
    assert "SENTINEL" not in repr(problems)


def test_a_subtree_where_a_value_belongs_is_dropped():
    facts = minimal_facts()
    facts["contexts"]["base"]["units"] = {"whole": {"subtree": 1}}
    facts["save"]["save_name"] = ["not", "text"]
    facts["contexts"]["base"]["stats"]["DEATHS"] = "7"
    out, problems = schema.validate(facts)
    assert "units" not in out["contexts"]["base"]
    assert "save_name" not in out["save"]
    assert "DEATHS" not in out["contexts"]["base"]["stats"]
    assert len(problems) == 3 and all("expected" in p for p in problems)


def test_bools_are_not_ints_and_nulls_only_where_declared():
    facts = minimal_facts()
    facts["contexts"]["base"]["units"] = True
    facts["contexts"]["base"]["nexus"] = None
    facts["save"]["season"] = None
    out, problems = schema.validate(facts)
    assert "units" not in out["contexts"]["base"] and "nexus" not in out["contexts"]["base"]
    assert out["save"]["season"] is None
    assert len(problems) == 2


def test_season_milestones_are_value_amount_optional_triples():
    facts = minimal_facts()
    facts["save"]["season"] = {"id": 1, "number": 46, "display": 22, "name_key": "K",
                               "milestones": [[150, 150, False], [0.5, 1, True], [1, 2], ["x", 1, False]],
                               "final_reward": False, "special_rewards": 0}
    out, problems = schema.validate(facts)
    assert out["save"]["season"]["milestones"] == [[150, 150, False], [0.5, 1, True]]
    assert len(problems) == 2


def test_typed_dicts_and_the_runtime_spec_declare_the_same_keys():
    for name, (typed, spec) in schema.TYPED.items():
        assert set(typed.__annotations__) == set(spec), name


def test_statuses_and_versions():
    assert schema.STATUSES == ("ok", "partial", "corrupt", "unsupported", "error")
    assert schema.SCHEMA_VERSION == 1
    assert all(isinstance(v, int) for v in (schema.EXTRACTOR_VERSION, schema.DECODER_VERSION))


# --- the save builder -----------------------------------------------------------------------


def test_obfuscate_hashes_every_key_except_raw_ones():
    tree = obfuscate({"PlayerStateData": {"Units": 1, Raw("a0>"): [{"Name": "x"}]}})
    assert tree == {hash_name("PlayerStateData"): {hash_name("Units"): 1, "a0>": [{hash_name("Name"): "x"}]}}


def test_a_built_save_decodes_back_to_its_obfuscated_tree():
    readable = context_save(base={"Units": -1, "SaveSummary": "On Planet"}, expedition={"Units": 5})
    data, hint = encode(readable)
    assert hint is None
    got = decode_save(data)
    assert got.root == obfuscate(readable)
    assert (got.container, got.chunks) == ("chunked", 1)


def test_big_saves_span_several_chunks_and_unframed_saves_carry_a_hint():
    readable = context_save(base={"SaveSummary": "x" * 3000})
    data, _ = encode(readable, chunk=1000)
    assert decode_save(data).chunks >= 3
    data, hint = encode(readable, unframed=True)
    assert not data.startswith(formats.SAVE_STREAMING_HEADER)
    got = decode_save(data, bare_size_hint=hint)
    assert got.container == "unframed" and got.root == obfuscate(readable)


def test_invalid_utf8_can_be_planted_in_a_string():
    bad = b"^PROD\xff".decode("utf-8", errors="surrogateescape")
    data, _ = encode(context_save(base={"KnownProducts": [bad]}))
    assert b"^PROD\xff" in decode_save(data).root[hash_name("BaseContext")][hash_name("PlayerStateData")][
        hash_name("KnownProducts")][0].encode("utf-8", "surrogateescape")


@pytest.mark.parametrize("layout", ["base", "expedition", "both"])
def test_context_layouts(layout):
    kw = {"base": {"Units": 1}} if layout != "expedition" else {}
    if layout != "base":
        kw["expedition"] = {"Units": 2}
    root = decode_save(encode(context_save(**kw))[0]).root
    assert (hash_name("BaseContext") in root) == (layout != "expedition")
    assert (hash_name("ExpeditionContext") in root) == (layout != "base")
