"""The privacy filter (``core/index/privacy.py``): no ownership id or username is read."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from nms_save_vault.core.hashname import hash_name
from nms_save_vault.core.index import extract, nodes, privacy
from nms_save_vault.core.savejson import decode_save

from .savebuilder import context_save, encode

INDEX_SRC = Path(privacy.__file__).parent


def test_the_deny_list_covers_every_ownership_field():
    assert privacy.DENIED >= {"Owner", "OWS", "SenderData", "UsedDiscoveryOwnersV2", "LastEditedById",
                              "LastEditedByUsername", "PlatformToken", "UID", "LID", "USN", "BlockList"}


@pytest.mark.parametrize("name", sorted(privacy.DENIED))
def test_get_refuses_every_denied_name(name):
    with pytest.raises(privacy.PrivacyError):
        nodes.get({hash_name(name): "SENTINEL"}, name)


def _base(ptk):
    owner = {hash_name("UID"): "SENTINEL-UID-000000001", hash_name("USN"): "SENTINEL_USERNAME",
             hash_name("LID"): "SENTINEL-LID", hash_name("TS"): 1}
    if ptk is not None:
        owner[hash_name("PTK")] = ptk
    return {hash_name("Owner"): owner, hash_name("Name"): "Home"}


@pytest.mark.parametrize(("ptk", "expected"), [
    ("ST", "ST"), ("XB", "XB"), ("PS", "PS"), (None, None), ("", None),
    ("SENTINEL_USERNAME", None), ("st", None), (7, None),
])
def test_owner_platform_returns_a_platform_code_only(ptk, expected):
    assert privacy.owner_platform(_base(ptk)) == expected


def test_owner_platform_tolerates_odd_shapes():
    assert privacy.owner_platform(None) is None
    assert privacy.owner_platform({hash_name("Owner"): "x"}) is None


def _denied_literals(path: Path) -> list[tuple[int, str, str | None]]:
    """(line, literal, enclosing function or "DENIED") for every literal naming a denied field."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = child.name
            elif isinstance(child, ast.Assign) and any(getattr(t, "id", None) == "DENIED" for t in child.targets):
                inner = "DENIED"
            else:
                inner = func
            if isinstance(child, ast.Constant) and child.value in privacy.DENIED:
                out.append((child.lineno, child.value, func))
            visit(child, inner)

    visit(tree, None)
    return out


def test_no_index_code_names_a_denied_field_outside_the_privileged_reader():
    offenders = []
    for path in sorted(INDEX_SRC.rglob("*.py")):
        for line, literal, func in _denied_literals(path):
            allowed = path.name == "privacy.py" and func in ("owner_platform", "DENIED")
            if not allowed:
                offenders.append(f"{path.name}:{line} {literal}")
    assert offenders == []


def test_the_literal_scan_finds_a_reader(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text('def f(b):\n    return get(b, "Owner", "UID")\n', encoding="utf-8")
    assert [(lit, func) for _l, lit, func in _denied_literals(bad)] == [("Owner", "f"), ("UID", "f")]


# --- end to end: sentinels in every ownership block never reach the facts --------------------

SENTINELS = ("SENTINEL-UID-000000001", "SENTINEL_USERNAME", "0xDEADBEEFDEADBEEF", "SENTINEL-LID-0002",
             "SENTINEL-TOKEN")


def sentinel_save() -> dict:
    """A save whose every ownership block (Owner, OWS, SenderData, UsedDiscoveryOwnersV2,
    LastEditedBy*, PlatformToken, BlockList) carries sentinel values."""
    owner = {"UID": SENTINELS[0], "USN": SENTINELS[1], "LID": SENTINELS[3], "PTK": "XB", "TS": 1}
    base = {"BaseType": {"PersistentBaseTypes": "HomePlanetBase"}, "Name": "Home", "Owner": owner,
            "LastEditedById": SENTINELS[0], "LastEditedByUsername": SENTINELS[1], "PlatformToken": SENTINELS[4],
            "GalacticAddress": "0x0000000100000002", "Position": [1, 2, 3],
            "Objects": [{"ObjectID": "^C_WALL", "Owner": owner}]}
    external = dict(base, BaseType={"PersistentBaseTypes": "ExternalPlanetBase"})
    psd = {"PersistentPlayerBases": [base, external],
           "Pets": [{"CreatureID": "^QUAD_PET", "CustomName": "",
                     "SenderData": {"UID": SENTINELS[0], "USN": SENTINELS[1]}}],
           "Eggs": [{"CreatureID": "^QUAD_PET", "SenderData": {"UID": SENTINELS[0]}}],
           "BlockList": [{"UID": SENTINELS[0], "USN": SENTINELS[1]}]}
    common = {"SaveName": "S", "TotalPlayTime": 1, "UsedDiscoveryOwnersV2": [owner, {"UID": SENTINELS[2]}]}
    records = [{"DD": {"DT": "Planet", "UA": SENTINELS[2]}, "OWS": owner}]
    return context_save(base=psd, expedition=psd, common=common, discoveries=records)


def test_no_sentinel_reaches_the_facts_or_errors():
    facts = extract.run_extractors(decode_save(encode(sentinel_save())[0]))
    assert facts["status"] == "ok", facts["errors"]
    blob = json.dumps(facts, ensure_ascii=False)
    assert [s for s in SENTINELS if s in blob] == []
    for ctx in facts["contexts"].values():
        assert ctx["bases"]["ptk"] == ["XB"]
        assert ctx["bases"]["external_cached"] == 1
