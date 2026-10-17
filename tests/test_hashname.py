"""HashName (``core/hashname.py``): readable field name -> obfuscated save key.

The pin file holds name/key pairs copied from MBINCompiler's mapping (an independent
source), including every name the index code reads. A misspelt name would hash to a key
that never occurs and silently yield nothing, so the coverage test below makes every
name the index code passes to ``get`` / ``items`` / ``key`` appear in the pins.
"""
from __future__ import annotations

import ast
import importlib
import json
import string
from pathlib import Path

import pytest

from nms_save_vault.core import hashname
from nms_save_vault.core.hashname import hash_name, key, spooky_short

PINS = Path(__file__).parent / "data" / "hashname_pins.json"
INDEX_SRC = Path(hashname.__file__).parent / "index"
ALPHABET = set(string.digits + ":;<=>?@" + string.ascii_uppercase + string.ascii_lowercase[:25])
# The node accessors whose arguments are readable field names, and where those start.
ACCESSORS = {"get": 1, "items": 1, "key": 0}


def pins() -> list[tuple[str, str]]:
    return [tuple(p) for p in json.loads(PINS.read_text(encoding="utf-8"))["pairs"]]


def accessor_names(source: str, constants: dict | None = None) -> tuple[set[str], list[str]]:
    """(names, unresolved) for every field-name argument of a node accessor: ``get(n, "A")``,
    ``items(n, "A")``, ``key("A")``, ``nodes.get(...)``.

    An argument may be a literal, a module constant (looked up in ``constants``, the
    imported module's values), or a loop / comprehension variable over a literal tuple or a
    module constant. Anything else is reported as unresolved, so no name the index reads
    can bypass the pins. Attribute calls on other objects (``some_dict.get("x")``) and
    ``*path`` pass-throughs do not count.
    """
    constants = constants or {}
    tree = ast.parse(source)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    names: set[str] = set()
    unresolved: list[str] = []

    def strings(expr) -> list[str] | None:
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            vals = [e.value for e in expr.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            return vals if len(vals) == len(expr.elts) else None
        if isinstance(expr, ast.Name) and expr.id in constants:
            value = constants[expr.id]
            if isinstance(value, str):
                return [value]
            if isinstance(value, (tuple, list, set, frozenset)) and all(isinstance(v, str) for v in value):
                return list(value)
        return None

    def binding(var: ast.Name):
        node = var
        while node in parent:
            node = parent[node]
            loops = [node] if isinstance(node, ast.For) else getattr(node, "generators", [])
            for loop in loops:
                if isinstance(loop.target, ast.Name) and loop.target.id == var.id:
                    return loop.iter
        return var                       # not a loop variable: maybe a module constant

    def passes_through(var: ast.Name) -> bool:
        """A loop over the enclosing function's ``*path``: the accessor forwarding its
        caller's names (``nodes.get`` itself), checked where those names are written."""
        it = binding(var)
        node = var
        while node in parent:
            node = parent[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                vararg = node.args.vararg
                return isinstance(it, ast.Name) and vararg is not None and it.id == vararg.arg
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            name = fn.id
        elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id in ("nodes", "hashname"):
            name = fn.attr
        else:
            continue
        if name not in ACCESSORS:
            continue
        for arg in node.args[ACCESSORS[name]:]:
            if isinstance(arg, ast.Starred) or (isinstance(arg, ast.Name) and passes_through(arg)):
                continue
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.add(arg.value)
                continue
            found = strings(binding(arg)) if isinstance(arg, ast.Name) else None
            if found is None:
                unresolved.append(f"line {arg.lineno}: {ast.unparse(arg)}")
            else:
                names.update(found)
    return names, unresolved


def _module_of(path: Path):
    rel = path.relative_to(INDEX_SRC).with_suffix("").parts
    parts = ["nms_save_vault", "core", "index", *rel]
    if parts[-1] == "__init__":
        parts.pop()
    return importlib.import_module(".".join(parts))


def index_names() -> tuple[set[str], list[str]]:
    names: set[str] = set()
    unresolved: list[str] = []
    for path in sorted(INDEX_SRC.rglob("*.py")) if INDEX_SRC.is_dir() else []:
        found, missing = accessor_names(path.read_text(encoding="utf-8"), vars(_module_of(path)))
        names |= found
        unresolved += [f"{path.name} {m}" for m in missing]
    return names, unresolved


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


def test_the_coverage_scan_finds_literals_constants_and_loop_variables():
    src = "\n".join([
        'NAMES = ("Chest1Inventory", "Chest2Inventory")',
        'get(ps, "UniverseAddress", "RealityIndex")',
        'items(ps, "ShipOwnership")',
        'nodes.get(b, "Owner")',
        'K = key("ObjectID")',
        'entry.get("not-a-field")',
        'print("Nope")',
        '[get(ps, n) for n in NAMES]',
        'for m in ("Inventory", "Inventory_TechOnly"):',
        '    get(v, m)',
        'get(ps, FIELD)',
        'def f(node, *path):',
        '    return get(node, *path)',
        'def g(node, *path):',
        '    for name in path:',
        '        node = node.get(key(name))',
    ])
    names, unresolved = accessor_names(src, {"NAMES": ("Chest1Inventory", "Chest2Inventory"), "FIELD": "SaveName"})
    assert names == {"UniverseAddress", "RealityIndex", "ShipOwnership", "Owner", "ObjectID", "Chest1Inventory",
                     "Chest2Inventory", "Inventory", "Inventory_TechOnly", "SaveName"}
    assert unresolved == []


def test_a_name_from_anywhere_else_is_reported_unresolved():
    src = "\n".join([
        'def f(state, which):',
        '    return get(state, which, "DifficultyPresetType")',
        'get(x, "A" + "B")',
    ])
    names, unresolved = accessor_names(src)
    assert names == {"DifficultyPresetType"}
    assert sorted(unresolved) == ["line 2: which", "line 3: 'A' + 'B'"]
def test_every_name_the_index_reads_is_pinned_and_resolvable():
    names, unresolved = index_names()
    assert unresolved == []
    assert {"Chest10Inventory", "CorvetteStorageInventory", "FreighterInventory_Cargo"} <= names
    pinned = {n for n, _k in pins()}
    assert sorted(names - pinned) == []


def test_the_names_the_index_reads_have_distinct_keys():
    names = sorted(index_names()[0])
    keys = [hash_name(n) for n in names]
    assert len(set(keys)) == len(keys)


def test_the_docstring_does_not_claim_21_bit_slices_and_masking_would_be_wrong():
    assert "21-bit slices" not in hashname.__doc__

    def masked(name: str) -> str:
        h1, _ = spooky_short(name.encode("utf-8"), hashname.SEED, hashname.SEED)
        out = ""
        for shift in (0, 21, 42):
            ch = ((h1 >> shift) & 0x1FFFFF) % 68 + ord("0")
            out += chr(ch + 6 if ch > ord("Z") else ch)
        return out

    assert sum(masked(n) != k for n, k in pins()) > len(pins()) // 2
