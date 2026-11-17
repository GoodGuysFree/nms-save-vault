"""Review R32a of the save patcher c868987..a5e4e4f (2026-11-17), every finding proven by a
probe that failed: a field twice on the way to an edit (the patcher edited the first copy,
the JSON reader shows the last, and the proof of the changed part did not see it); a body
changed in place read through an old text view; span and value_bytes keeping the text view;
a bool or an unhashable step let through by the step cache."""
from __future__ import annotations

import json
import random

import pytest

from nms_save_vault.core import savepatch as sp
from nms_save_vault.core.hashname import key as K
from nms_save_vault.core.savejson import parse_text

R, I, A, X = sp.Replace, sp.Insert, sp.AddMember, sp.Remove
PSD = ("PlayerStateData",)


def k(name: str) -> bytes:
    return b'"' + K(name).encode() + b'"'


def tree(body: bytes):
    return json.loads(bytes(body).decode("utf-8", errors="surrogateescape"), strict=False)


# --- R32a-1: a field twice on the way to the part (CRITICAL) ------------------------------

def test_a_field_twice_at_the_root_on_the_way_to_an_edit_is_refused():
    """PlayerStateData twice: the reader (last wins) shows the second copy, the patcher found
    the first. c868987 refused this save; so must the patch of the changed part only."""
    body = b"{" + k("PlayerStateData") + b":{" + k("Units") + b":1}," + \
        k("PlayerStateData") + b":{" + k("Units") + b":2}}"
    with pytest.raises(sp.PatchRefused, match="twice"):
        sp.apply(body, [R(PSD + ("Units",), b"9")])


def test_a_field_twice_above_an_inventory_slot_is_refused():
    inv1 = b"{" + k("Slots") + b":[{" + k("Amount") + b":1}]}"
    inv2 = b"{" + k("Slots") + b":[{" + k("Amount") + b":5}]}"
    body = b"{" + k("PlayerStateData") + b":{" + k("Inventory") + b":" + inv1 + b"," + \
        k("Inventory") + b":" + inv2 + b"}}"
    with pytest.raises(sp.PatchRefused, match="twice"):
        sp.apply(body, [R(PSD + ("Inventory", "Slots", 0, "Amount"), b"99")])


def test_an_insert_under_a_field_twice_is_refused():
    body = b"{" + k("PlayerStateData") + b":{" + k("Ships") + b":[1]," + k("Ships") + b":[2]}}"
    with pytest.raises(sp.PatchRefused, match="twice"):
        sp.apply(body, [I(PSD + ("Ships",), 0, b"7")])


def test_a_value_copied_from_under_a_field_twice_is_refused():
    """value_bytes copies an asset into another save: the first copy is not what the app
    showed either."""
    body = b"{" + k("PlayerStateData") + b":{" + k("Units") + b":1}," + \
        k("PlayerStateData") + b":{" + k("Units") + b":2}}"
    with pytest.raises(sp.PatchRefused, match="twice"):
        sp.value_bytes(body, PSD + ("Units",))


def test_a_field_twice_off_the_way_is_left_alone():
    """GGF (2026-11-17): only the changed area is proven; a sibling is not read whole."""
    body = b"{" + k("PlayerStateData") + b":{" + k("Units") + b":1}," + \
        k("Other") + b":{" + k("Aa") + b":1," + k("Aa") + b":2}}"
    assert sp.apply(body, [R(PSD + ("Units",), b"9")]) == body.replace(b":1}", b":9}", 1)


def test_a_save_without_a_field_twice_on_the_way_is_patched_as_before():
    body = (b"{" + k("PlayerStateData") + b": {" + k("Units") + b":1, " + k("Ships") +
            b":[1,2]}, " + k("ExpeditionContext") + b":{" + k("PlayerStateData") + b":{" +
            k("Units") + b":3}}}")
    out = sp.apply(body, [R(PSD + ("Units",), b"9"), I(PSD + ("Ships",), 2, b"3")])
    assert tree(out)[K("PlayerStateData")] == {K("Units"): 9, K("Ships"): [1, 2, 3]}
    assert tree(out)[K("ExpeditionContext")][K("PlayerStateData")][K("Units")] == 3


NAMES = ["Aa", "Bb", "Cc", "Dd", "Ee"]
SCALARS = [b"0", b"-0", b"1", b"1.5", b"1e5", b"true", b"null", b'""', b'"q\\"uote"',
           b'"\\u00e9"', '"café"'.encode("utf-8"), b'"^\x80\xb7\xff"', b'"br]}[{,"']
RAWS = [b"0", b"7", b'"x"', b"{}", b"[]", b"[1,2]", b"null", b'{"' + K("Aa").encode() + b'":1}']


def _gen(r, depth):
    roll = r.random()
    if depth <= 0 or roll < 0.35:
        return ("s", r.choice(SCALARS))
    if roll < 0.65:
        return ("a", [_gen(r, depth - 1) for _ in range(r.randint(0, 4))])
    names = r.sample(NAMES, r.randint(0, 4))
    if names and r.random() < 0.3:
        names.insert(r.randint(0, len(names)), r.choice(names))      # a field twice
    return ("o", [(n, _gen(r, depth - 1)) for n in names])


def _ser(node, r):
    kind, val = node
    space = lambda: r.choice([b"", b"", b" ", b"\r\n"])              # noqa: E731
    if kind == "s":
        return val
    if kind == "a":
        return b"[" + b",".join(space() + _ser(v, r) for v in val) + b"]"
    return b"{" + b",".join(space() + k(n) + b":" + _ser(v, r) for n, v in val) + b"}"


def _paths(node, here=()):
    yield here, node
    kind, val = node
    for step, v in (enumerate(val) if kind == "a" else val if kind == "o" else ()):
        yield from _paths(v, here + (step,))


def _edit(r, root):
    every = list(_paths(root))
    p, node = r.choice(every)
    pick = r.random()
    if pick < 0.4:
        return R(p, r.choice(RAWS)) if p else A((), r.choice(NAMES), r.choice(RAWS))
    if pick < 0.6:
        return X(p) if p else R(("Aa",), b"1")
    arrays = [(q, n) for q, n in every if n[0] == "a"]
    objects = [(q, n) for q, n in every if n[0] == "o"]
    if pick < 0.8 and arrays:
        q, n = r.choice(arrays)
        return I(q, r.randint(0, len(n[1])), r.choice(RAWS))
    q, n = r.choice(objects)
    return A(q, r.choice(NAMES), r.choice(RAWS))


def _reads_as_made(body, edits, new):
    """``new`` reads (as savejson reads a save: last wins) as ``body`` with ``edits`` made."""
    whole = parse_text(body)
    try:
        for edit in sp._tree_order(list(edits)):
            sp._expected(whole, edit, sp._parse_value)
    except (KeyError, IndexError, TypeError, AttributeError, sp.PatchRefused):
        return False
    return sp._canonical(parse_text(new)) == sp._canonical(whole)


@pytest.mark.parametrize("seed", range(4))
def test_every_edit_accepted_in_a_save_with_fields_twice_reads_as_made(seed):
    r = random.Random(3200 + seed)
    wrong, accepted = [], 0
    for _ in range(1500):
        root = _gen(r, 4)
        if root[0] != "o":
            root = ("o", [("Aa", root), ("Bb", _gen(r, 3))])
        body = _ser(root, r)
        edits = [_edit(r, root) for _ in range(r.choice([1, 1, 2, 3]))]
        try:
            new = sp.apply(body, edits)
        except sp.PatchRefused:
            continue
        accepted += 1
        if not _reads_as_made(body, edits, new):
            wrong.append((body, edits))
    assert accepted > 100
    assert not wrong, f"{len(wrong)} accepted edits do not read as made, e.g. {wrong[0]!r:.300}"


# --- R32a-2/3: the text view --------------------------------------------------------------

def _stale_pair():
    """Two bodies of one length: Bb.Zz is 5 in the first, 7 in the second, at other offsets."""
    a, b, q, z = (K(n) for n in ("Aa", "Bb", "Qq", "Zz"))
    new = ('{"%s":{"%s":1,"%s":{"%s":5}},"%s":{"%s":7}}' % (a, q, b, z, b, z)).encode()
    comma = new.index(b",", 7)
    old_aa = b"[1," + b"2" * (comma - 7 - 4) + b"]"
    old = b'{"' + a.encode() + b'":' + old_aa + new[comma:comma + 16] + b"}"
    tree(old)
    return old, new


def test_a_body_changed_in_place_is_not_read_through_its_old_text():
    old, new = _stale_pair()
    body = bytearray(old)
    assert sp.value_bytes(body, ("Bb", "Zz")) == b"5"
    body[:] = new
    assert sp.value_bytes(body, ("Bb", "Zz")) == b"7"


def test_an_edit_of_a_body_changed_in_place_lands_where_it_says():
    old, new = _stale_pair()
    body = bytearray(old)
    sp.span(body, ("Bb", "Zz"))
    body[:] = new
    out = sp.apply(body, [R(("Bb", "Zz"), b"9")])
    assert tree(out)[K("Bb")][K("Zz")] == 9
    assert tree(out)[K("Aa")][K("Bb")][K("Zz")] == 5


def test_span_and_value_bytes_do_not_keep_the_text_of_a_save():
    body = b"{" + k("PlayerStateData") + b":{" + k("Ships") + b":[1,2]," + k("Units") + b":1}}"
    sp._VIEW = None
    sp.span(body, PSD + ("Units",))
    assert sp._VIEW is None
    sp.value_bytes(body, PSD + ("Units",))
    assert sp._VIEW is None


# --- R32a-4: steps the cache must not let through ------------------------------------------

def test_a_bool_step_is_refused_even_after_the_same_index_was_walked():
    ships = b"[{" + k("Aa") + b":1," + k("Bb") + b":1},{" + k("Aa") + b":2," + k("Bb") + b":2}]"
    body = b"{" + k("PlayerStateData") + b":{" + k("Ships") + b":" + ships + b"}}"
    with pytest.raises(sp.PatchRefused, match="not True"):
        sp.apply(body, [R(PSD + ("Ships", 1, "Aa"), b"7"), R(PSD + ("Ships", True, "Bb"), b"8")])


def test_an_unhashable_step_is_refused_not_a_crash():
    body = b"{" + k("PlayerStateData") + b":{" + k("Units") + b":1}}"
    with pytest.raises(sp.PatchRefused):
        sp.apply(body, [R(PSD + (["Units"],), b"2")])
