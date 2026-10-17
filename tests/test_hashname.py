"""HashName (``core/hashname.py``): readable field name -> obfuscated save key.

The pin file holds name/key pairs copied from MBINCompiler's mapping (an independent
source), including every name the index code reads. A misspelt name would hash to a key
that never occurs and silently yield nothing, so the coverage test below makes every
name the index code passes to ``get`` / ``items`` / ``key`` appear in the pins.
"""
from __future__ import annotations

import ast
import json
import string
from pathlib import Path

import pytest

from nms_save_vault.core import hashname
from nms_save_vault.core.hashname import hash_name, key, spooky_short

PINS = Path(__file__).parent / "data" / "hashname_pins.json"
INDEX_SRC = Path(hashname.__file__).parent / "index"
ALPHABET = set(string.digits + ":;<=>?@" + string.ascii_uppercase + string.ascii_lowercase[:25])
# The node accessors whose string arguments are readable field names.
ACCESSORS = {"get", "items", "key", "has"}


def pins() -> list[tuple[str, str]]:
    return [tuple(p) for p in json.loads(PINS.read_text(encoding="utf-8"))["pairs"]]


def accessor_names(source: str) -> set[str]:
    """Every string literal passed to a node accessor: ``get(n, "A", "B")``, ``key("A")``,
    ``nodes.get(...)``. Attribute calls on other objects (``some_dict.get("x")``) do not
    count."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            name = fn.id
        elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id in ("nodes", "hashname"):
            name = fn.attr
        else:
            continue
        if name in ACCESSORS:
            out.update(a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str))
    return out


def index_names() -> set[str]:
    names: set[str] = set()
    for path in sorted(INDEX_SRC.rglob("*.py")) if INDEX_SRC.is_dir() else []:
        names |= accessor_names(path.read_text(encoding="utf-8"))
    return names


def test_every_pinned_pair_hashes_to_its_key():
    pairs = pins()
    assert len(pairs) > 200
    wrong = [(n, k, hash_name(n)) for n, k in pairs if hash_name(n) != k]
    assert wrong == []


def test_the_pins_exercise_every_branch_of_the_short_hash():
    lengths = {len(n.encode("utf-8")) for n, _k in pins()}
    assert any(n >= 32 for n in lengths)                       # a whole 32-byte block
    assert any(n > 15 and n % 32 >= 16 for n in lengths)       # the extra 16-byte block
    tails = {(n % 32 - 16 if n > 15 and n % 32 >= 16 else n % 32) for n in lengths}
    assert 0 in tails and tails & set(range(1, 8)) and tails & set(range(8, 16))


def test_the_known_collision_and_legacy_names_are_pinned():
    pairs = dict(pins())
    assert pairs["ShipShield"] == pairs["AllowedToBePurpleWithoutAccess"] == "NE3"
    assert (pairs["BuildingType"], pairs["ThirdPersonShip"], pairs["ActiveSpaceBattleLevel"]) == ("a0>", "cid", "bQN")


@pytest.mark.parametrize("name", ["", "a", "PlayerStateData", "x" * 31, "y" * 32, "z" * 100, "été", "Q" * 191])
def test_keys_are_three_characters_of_the_save_alphabet(name):
    k = hash_name(name)
    assert len(k) == 3 and set(k) <= ALPHABET


def test_names_of_192_bytes_or_more_are_refused():
    hash_name("n" * 191)
    with pytest.raises(ValueError):
        hash_name("n" * 192)
    with pytest.raises(ValueError):
        spooky_short(b"\x00" * 200, 1, 2)


def test_key_is_cached_hash_name():
    assert key("KnownPortalRunes") == hash_name("KnownPortalRunes") == "vrS"
    assert key.cache_info().currsize >= 1


def test_the_coverage_scan_finds_accessor_literals_only():
    src = (
        'get(ps, "UniverseAddress", "RealityIndex")\n'
        'items(ps, "ShipOwnership")\n'
        'nodes.get(b, "Owner")\n'
        'K = key("ObjectID")\n'
        'entry.get("not-a-field")\n'
        'print("Nope")\n'
    )
    assert accessor_names(src) == {"UniverseAddress", "RealityIndex", "ShipOwnership", "Owner", "ObjectID"}


def test_every_name_the_index_reads_is_pinned():
    pinned = {n for n, _k in pins()}
    assert sorted(index_names() - pinned) == []


def test_the_names_the_index_reads_have_distinct_keys():
    names = sorted(index_names())
    keys = [hash_name(n) for n in names]
    assert len(set(keys)) == len(keys)
