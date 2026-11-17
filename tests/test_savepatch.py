"""core.savepatch (CL4): format-preserving edits of a save's JSON, each proven by a re-parse.
Bytes outside an edit never change; an edit that is not exactly what it says is refused."""
from __future__ import annotations

import json

import pytest

from nms_save_vault.core import lz4_block, savepatch as sp
from nms_save_vault.core.hashname import key as K

R, I, A, X = sp.Replace, sp.Insert, sp.AddMember, sp.Remove
PSD = ("PlayerStateData",)


def save(**psd) -> bytes:
    return json.dumps({K("PlayerStateData"): {K(k): v for k, v in psd.items()}},
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def tree(body: bytes):
    return json.loads(body.decode("utf-8", errors="surrogateescape"), strict=False)


def psd(body: bytes) -> dict:
    return tree(body)[K("PlayerStateData")]


def test_a_replaced_value_changes_those_bytes_only():
    body = save(Units=5, Nanites=7, SaveName="Main")
    new = sp.apply(body, [R(PSD + ("Units",), b"1234567")])
    start, end = sp.span(body, PSD + ("Units",))
    assert new == body[:start] + b"1234567" + body[end:]
    assert psd(new)[K("Units")] == 1234567 and psd(new)[K("Nanites")] == 7


def test_unknown_keys_spacing_and_odd_text_elsewhere_survive_byte_for_byte():
    body = ('{ "zZz" : [1, 2.50, "t\tab"],\n  "' + K("PlayerStateData") + '" : { "'
            + K("Units") + '" : 5 , "q!" : "éé" } }').encode("utf-8")
    new = sp.apply(body, [R(PSD + ("Units",), b"6")])
    assert new == body.replace(b": 5 ,", b": 6 ,")


@pytest.mark.parametrize("drop, left", [
    ([0], [2, 3, 4]), ([1], [1, 3, 4]), ([3], [1, 2, 3]), ([0, 1, 2, 3], []),
    ([1, 2], [1, 4]), ([2, 3], [1, 2]), ([0, 2], [2, 4]), ([0, 1, 3], [3]),
])
def test_removing_any_set_of_elements_keeps_the_array_well_formed(drop, left):
    for body in (save(Ships=[1, 2, 3, 4]),
                 ('{"' + K("PlayerStateData") + '": {"' + K("Ships") + '": [ 1, 2 ,3,\n 4 ]}}')
                 .encode()):
        new = sp.apply(body, [X(PSD + ("Ships", i)) for i in drop])
        assert psd(new)[K("Ships")] == left


def test_removing_object_members_first_middle_last_and_only():
    body = save(A=1, B=2, C=3)
    for name, rest in (("A", "BC"), ("B", "AC"), ("C", "AB")):
        new = sp.apply(body, [X(PSD + (name,))])
        assert list(psd(new)) == [K(c) for c in rest]
    assert psd(sp.apply(save(A=1), [X(PSD + ("A",))])) == {}


def test_insertions_and_new_members():
    body = save(Ships=[1, 2], Empty=[], Obj={})
    new = sp.apply(body, [I(PSD + ("Ships",), 0, b"0"), I(PSD + ("Ships",), 1, b"1.5"),
                          I(PSD + ("Ships",), 2, b"3"), I(PSD + ("Empty",), 0, b'"x"'),
                          A(PSD + ("Obj",), "Units", b"9"), A(PSD, "Nanites", b"10")])
    got = psd(new)
    assert got[K("Ships")] == [0, 1, 1.5, 2, 3] and got[K("Empty")] == ["x"]
    assert got[K("Obj")] == {K("Units"): 9} and got[K("Nanites")] == 10


def test_edits_name_places_in_the_original_even_when_they_shift_each_other():
    body = save(Ships=[{"n": "a"}, {"n": "b"}, {"n": "c"}, {"n": "d"}])
    new = sp.apply(body, [X(PSD + ("Ships", 0)), R(PSD + ("Ships", 2), b'{"n":"C"}'),
                          I(PSD + ("Ships",), 4, b'{"n":"e"}')])
    assert psd(new)[K("Ships")] == [{"n": "b"}, {"n": "C"}, {"n": "d"}, {"n": "e"}]


def test_steps_are_made_one_after_another_each_proven():
    body = save(Ships=[{"n": "a"}, {"n": "b"}, {"n": "c"}])
    steps = sp.Steps([[X(PSD + ("Ships", 0))],                     # b, c
                      [R(PSD + ("Ships", 0), b'{"n":"B"}')],       # the first is b by now
                      []])
    new = sp.apply_steps(body, steps)
    assert psd(new)[K("Ships")] == [{"n": "B"}, {"n": "c"}]
    assert new == sp.apply(sp.apply(body, steps[0]), steps[1])
    with pytest.raises(sp.PatchRefused):
        sp.apply_steps(body, sp.Steps([[R(PSD + ("Ships", 5), b"1")]]))


def test_a_value_copied_from_another_save_arrives_as_that_save_wrote_it():
    source = b'{"' + K("PlayerStateData").encode() + b'":{"' + K("Units").encode() + b'":1.50E+2}}'
    raw = sp.value_bytes(source, PSD + ("Units",))
    assert raw == b"1.50E+2"
    assert b'"' + K("Units").encode() + b'":1.50E+2' in sp.apply(save(Units=1), [R(PSD + ("Units",), raw)])


@pytest.mark.parametrize("edits, why", [
    ([R(PSD + ("Nope",), b"1")], "no field 'Nope'"),
    ([R(PSD + ("Ships", 9), b"1")], "the array has 2 elements"),
    ([R(PSD + ("Ships", "x"), b"1")], "not an object"),
    ([R(PSD + ("Units", 0), b"1")], "expected an object or an array"),
    ([I(PSD + ("Units",), 0, b"1")], "expected an object or an array"),
    ([A(PSD + ("Ships",), "Units", b"1")], "added to an object only"),
    ([A(PSD, "Units", b"1")], "has 'Units' already"),
    ([R(PSD + ("Ships", 0), b"1"), X(PSD + ("Ships", 0))], "touch the same bytes"),
    ([I(PSD + ("Ships",), 1, b"7"), I(PSD + ("Ships",), 1, b"8")], "touch the same bytes"),
    ([R(PSD + ("Units",), b"{oops")], "not JSON"),
    ([R((), b"{}")], "not the whole save"),
    ([X(())], "cannot be removed"),
    ([R(PSD + (True,), b"1")], "field names and indices"),
])
def test_an_edit_that_cannot_be_made_exactly_is_refused(edits, why):
    with pytest.raises(sp.PatchRefused, match=why):
        sp.apply(save(Units=5, Ships=[1, 2]), edits)


def test_the_re_parse_catches_an_edit_that_changed_more_than_it_said(monkeypatch):
    real = sp._splice

    def sloppy(body, edit, cache):
        start, end, raw = real(body, edit, cache)
        return start, end + 1, raw                  # eats the byte after the value

    monkeypatch.setattr(sp, "_splice", sloppy)
    with pytest.raises(sp.PatchRefused):
        sp.apply(save(Units=5, Nanites=[1, 2]), [R(PSD + ("Units",), b"6")])


def test_raw_control_characters_and_non_ascii_text_are_read_as_the_game_wrote_them():
    body = save(SaveName="café", Units=1).replace(b'"caf', b'"\tcaf')
    new = sp.apply(body, [R(PSD + ("SaveName",), sp.dumps("été"))])
    assert psd(new)[K("SaveName")] == "été" and b"\xc3\xa9t\xc3\xa9" in new


def test_no_edits_is_the_same_bytes():
    body = save(Units=1)
    assert sp.apply(body, []) is body



@pytest.mark.parametrize("raw, why", [
    (b'"Ma\x00in"', "NUL"),                                  # the game ends a save at a NUL
    (b'1,"' + K("Nanites").encode() + b'":3', "not JSON"),     # a second member smuggled in
    (b"1,2", "more than one JSON value"),
    (b'1]  ,[2', "not JSON"),
    (b'"x\\\ty"', "not JSON"),                                # needs the escape repair
])
def test_a_raw_value_that_would_break_or_smuggle_into_the_save_is_refused(raw, why):
    with pytest.raises(sp.PatchRefused, match=why):
        sp.apply(save(Units=5, Nanites=2), [R(PSD + ("Units",), raw)])


def test_a_save_with_a_field_twice_in_one_object_is_not_touched():
    body = save(Units=5).replace(b"}}", b',"' + K("Units").encode() + b'":6}}')
    with pytest.raises(sp.PatchRefused, match="twice"):
        sp.apply(body, [A(PSD, "Nanites", b"1")])


def test_the_proof_compares_member_order_and_value_types():
    assert sp._canonical({"a": True}) != sp._canonical({"a": 1})
    assert sp._canonical({"a": 1}) != sp._canonical({"a": 1.0})
    assert sp._canonical({"a": 1, "b": 2}) != sp._canonical({"b": 2, "a": 1})


def test_text_with_undecodable_bytes_is_written_back_as_it_was():
    assert sp.dumps("ab\udcff") == b'"ab\xff"'
    body = save(Units=1).replace(b"}}", b',"' + K("SaveName").encode() + b'":"ab\xff"}}')
    name = tree(body)[K("PlayerStateData")][K("SaveName")]
    new = sp.apply(body, [R(PSD + ("SaveName",), sp.dumps(name + "c"))])
    assert b'"ab\xffc"' in new


def test_odd_names_are_refused_as_edits_not_as_crashes():
    with pytest.raises(sp.PatchRefused, match="not a field name"):
        sp.apply(save(Units=1), [R(PSD + ("x" * 200,), b"1")])
    body = save(Units=1).replace(b"{", b'{"\x01":0,', 1)          # a raw control char in a key
    assert psd(sp.apply(body, [R(PSD + ("Units",), b"2")]))[K("Units")] == 2


def test_many_edits_of_one_big_container_read_it_once():
    import time

    body = save(Ships=[{"n": i, "pad": "x" * 200} for i in range(4000)])
    started = time.perf_counter()
    new = sp.apply(body, [X(PSD + ("Ships", i)) for i in range(0, 4000, 2)])
    assert time.perf_counter() - started < 10
    assert [s["n"] for s in psd(new)[K("Ships")]] == list(range(1, 4000, 2))


@pytest.mark.slow
def test_a_real_save_takes_an_edit_and_changes_only_those_bytes(live_save_dir):
    """Read-only: one real save's JSON, its Units raised by one through the patcher."""
    path = next(iter(sorted(live_save_dir.glob("save*.hg"))))
    body = lz4_block.decompress(path.read_bytes()).split(b"\x00", 1)[0]
    root = tree(body)
    where = (("BaseContext", "PlayerStateData", "Units") if K("BaseContext") in root
             else ("PlayerStateData", "Units"))
    old = sp.value_bytes(body, where)
    new = sp.apply(body, [R(where, str(int(old) + 1).encode())])
    start, end = sp.span(body, where)
    assert new[:start] == body[:start] and new[start + len(str(int(old) + 1)):] == body[end:]


@pytest.mark.parametrize("cut", [b'{"' + K("PlayerStateData").encode() + b'":',
                                 b'{"' + K("PlayerStateData").encode() + b'":{"' + K("Units").encode() + b'":1',
                                 b""])
def test_a_body_cut_short_is_refused_not_a_crash(cut):
    with pytest.raises(sp.PatchRefused):
        sp.apply(cut, [sp.Replace(("PlayerStateData", "Units"), b"2")])


# --- a value's end found at C speed (review R32, GGF 2026-11-17: a drag took seconds) ---------

def _slow_end(body: bytes, p: int) -> int:
    """The byte-by-byte scan, as the patcher had it: the reference the fast one must match."""
    return sp._scan_end(body, p)


ODD_VALUES = [
    b'{"a":1,"b":[1,2,{"c":"]}"}],"d":"x\\"}"}',               # brackets and quotes in strings
    b'[["\\\\"],"\\\\\\"",{}]',                                # escaped backslashes and quotes
    '{"n":"\u00dcn\u00efc\u00f8d\u00e9 \u2713 \u65e5\u672c","m":[1.5e3,-2,true,false,null]}'.encode(),
    b'{"r":"raw\x01\x1f control","t":"tab\there"}',          # raw control characters
    b'{"bad":"an \\x escape the JSON reader refuses","k":[1]}',  # a bad escape the game wrote
    b'{"lone":"\xff\xfe not UTF-8 at all","z":{}}',          # undecodable bytes in a string
    b'{"nan":NaN,"inf":[Infinity,-Infinity]}',               # what the game may write
    b'[' * 3000 + b']' * 3000,                               # deeper than the reader recurses
    b'{"x":[1,2,3]} trailing',                               # a value followed by more text
]


@pytest.mark.parametrize("body", ODD_VALUES)
def test_a_value_s_end_is_the_same_found_fast_or_byte_by_byte(body):
    assert sp._value_end(body, 0) == _slow_end(body, 0)


def test_a_value_s_end_inside_a_big_save_is_the_same_fast_or_byte_by_byte():
    inner = [b'{"k%d":[%d,"s\\"%d",{"z":"]"}]}' % (n, n, n) for n in range(2000)]
    body = b'{"big":[' + b",".join(inner) + b'],"tail":{"a":"b"}}'
    box = sp._read_container(body, 0)
    for _start, value, end in box.items:
        assert end == _slow_end(body, value)


def test_a_field_twice_is_still_refused_with_its_name():
    with pytest.raises(sp._Duplicate) as caught:
        sp._loads(b'{"a":1,"b":2,"a":3}', repair=False)
    assert caught.value.args == ("a",)
    assert sp._loads(b'{"a":1,"b":{"a":2}}', repair=False) == {"a": 1, "b": {"a": 2}}
