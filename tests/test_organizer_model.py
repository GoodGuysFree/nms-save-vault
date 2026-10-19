"""organizer.model: ids, the rules on value types, and the JSON forms the store writes."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nms_save_vault.core import formats, savedir
from nms_save_vault.core.meta import MetaInfo
from nms_save_vault.core.organizer import model as m
from nms_save_vault.core.organizer.model import (
    Capture,
    EntryRef,
    LiveRef,
    LiveTarget,
    MemberObs,
    MetaSnap,
    ModelError,
    StoreRef,
)
from nms_save_vault.core.slotmap import SaveFileRef, slot_file_numbers

V1, V2, V3 = "1" * 64, "2" * 64, "a" * 64
ACCOUNT = "st_76561198000000001"
PID = "DDDAD1BDD39F9C72"


def snap(play: int = 3600, ts: int = 1_790_000_000, **kw) -> MetaSnap:
    fields = dict(play_time=play, meta_ts=ts, ts_src="meta", name="Main Save",
                  summary="On freighter", difficulty=2, game_mode=1, season=0,
                  base_version=4220, meta_format=2004, size_decompressed=18_000_000,
                  data_size=8_000_000, meta_sha="b" * 64, valid=True,
                  file_time=1_790_000_003.25)
    fields.update(kw)
    return MetaSnap(**fields)


def obs(member: str, vid: str, *, play: int = 3600, ts: int = 1_790_000_000,
        pid: str = PID, platform: str = "steam", xbox_sync: int | None = None,
        **meta) -> MemberObs:
    return MemberObs(member, vid, pid, "meta", platform, snap(play, ts, **meta), xbox_sync)


def capture(where, slot, a, b, at=1_792_137_600, q="exact", src="scan") -> Capture:
    return Capture(where, slot, a, b, at, q, src, m.head_of(a, b) and m.head_of(a, b).member)


def roundtrip(encoded: object) -> object:
    """Through the store's on-disk form: pure ASCII JSON."""
    text = json.dumps(encoded, ensure_ascii=True, separators=(",", ":"))
    assert text.isascii()
    return json.loads(text)


# --- identifiers --------------------------------------------------------------


def test_pid_forms():
    assert m.pid_from_id(0xDDDAD1BDD39F9C72) == PID
    assert m.pid_from_id(0x1F) == "000000000000001F"  # always 16 upper-case digits
    for bad in (0, 1 << 64, -5):
        with pytest.raises(ModelError):
            m.pid_from_id(bad)
    legacy = m.legacy_pid("xbox", 13, "Voyagers")
    assert legacy == "legacy:xbox:13:Voyagers"
    assert m.unknown_pid(V3) == "unknown:" + "a" * 16
    assert m.split_pid(PID, 1) == "split:DDDAD1BDD39F9C72:1"

    assert [m.pid_kind(p) for p in (PID, legacy, m.unknown_pid(V3),
                                    m.split_pid(legacy, 2))] == [
        "id", "legacy", "unknown", "split"]
    assert m.split_source(m.split_pid(legacy, 2)) == legacy
    assert m.is_identified(PID) and m.is_identified("split:DDDAD1BDD39F9C72:3")
    assert not m.is_identified(legacy) and not m.is_identified(m.split_pid(legacy, 1))

    for bad in ("dddad1bdd39f9c72", "DDDAD1BD", "legacy:ps5:1:x", "legacy:steam:16:x",
                "legacy:steam:0:x", "unknown:XYZ", "split:DDDAD1BDD39F9C72:0",
                "split:split:DDDAD1BDD39F9C72:1:1", "", None):
        assert not m.is_pid(bad), bad
    with pytest.raises(ModelError):
        m.split_pid(m.split_pid(PID, 1), 1)


def test_the_all_zero_id_is_no_pid():
    """0 is what a meta holds when it carries no id; it must never become a line."""
    assert not m.is_pid("0" * 16)
    with pytest.raises(ModelError):
        m.split_pid("0" * 16, 1)


@pytest.mark.parametrize("n", [True, 1.0, "1", 0])
def test_split_numbers_are_real_positive_ints(n):
    with pytest.raises(ModelError):
        m.split_pid(PID, n)


def test_an_unnamed_legacy_save_cannot_meet_one_named_like_the_marker():
    unnamed = m.legacy_pid("steam", 2, "")
    named = m.legacy_pid("steam", 2, "<unnamed>")
    assert unnamed == "legacy:steam:2:" and m.pid_kind(unnamed) == "legacy"
    assert unnamed != named
    assert not m.pids_compatible(unnamed, named)
    assert m.split_source(m.split_pid(unnamed, 1)) == unnamed


def test_two_unnamed_legacy_saves_are_not_one_save():
    """With no name, nothing but the slot ties them: only the same pid matches."""
    assert m.pids_compatible("legacy:steam:3:", "legacy:steam:3:")
    assert not m.pids_compatible("legacy:steam:3:", "legacy:steam:7:")


def test_pids_compatible_follows_a_legacy_save_across_slots():
    assert m.pids_compatible(PID, PID)
    assert not m.pids_compatible(PID, "9E8385F0AB12CD34")
    # A pre-Waypoint save moved from slot 3 to slot 7 keeps its line.
    assert m.pids_compatible("legacy:steam:3:Voyagers", "legacy:steam:7:Voyagers")
    assert not m.pids_compatible("legacy:steam:3:Voyagers", "legacy:xbox:3:Voyagers")
    assert not m.pids_compatible("legacy:steam:3:Voyagers", "legacy:steam:3:Other")
    assert not m.pids_compatible("legacy:steam:3:Voyagers", PID)


def test_op_and_correction_ids_carry_the_utc_time():
    op = m.new_op_id(lambda: 1_792_137_655)
    assert re.fullmatch(r"op-20261016T080055Z-[0-9a-f]{4}", op)
    assert re.fullmatch(r"cor-20261016T080055Z-[0-9a-f]{4}",
                        m.new_correction_id(lambda: 1_792_137_655))
    assert m.is_op_id(op) and m.is_op_id("legacy-17")
    assert not m.is_op_id("op-20261016T080055Z-3FA2") and not m.is_op_id("cor-x")


def test_vids_are_lower_case_sha256_hex():
    assert m.is_vid(V3)
    assert not m.is_vid("A" * 64) and not m.is_vid("a" * 63) and not m.is_vid(None)


# --- the head rule ------------------------------------------------------------

SLOT = 9
MS = 1_790_000_000.0   # a file time


def _member(index: int, spec):
    """One member as savedir sees it and as the organizer keeps it. ``spec`` is
    ``(meta save time or 0, file time, meta valid, play time)`` or None for absent."""
    ref = SaveFileRef(slot_file_numbers(SLOT)[index])
    if spec is None:
        return savedir.MemberView(ref, Path("d"), Path("m"), exists=False), None
    ts, file_time, valid, play = spec
    info = MetaInfo(ordinal=ref.storage_ordinal,
                    header=formats.META_HEADER if valid else 0, meta_format=2004,
                    size_decompressed=0, size_disk=0, base_version=0, game_mode=1, season=0,
                    total_play_time=play, save_name="", save_summary="", difficulty=0,
                    slot_identifier=0, timestamp=ts)
    view = savedir.MemberView(ref, Path("d"), Path("m"), exists=True, data_mtime=file_time,
                              info=info)
    kept = obs("AB"[index], (V1, V2)[index], play=play, ts=ts or int(file_time),
               ts_src="meta" if ts else "mtime", valid=valid, file_time=file_time)
    return view, kept


@pytest.mark.parametrize("a, b, expected", [
    ((200, MS, True, 1), (300, MS, True, 1), "B"),          # later meta time
    ((300, MS, True, 1), (300, MS, True, 1), "A"),          # a tie picks A
    ((300, MS, True, 1), (200, MS + 9, True, 1), "A"),      # meta clock, not the file times
    ((0, MS + 5, True, 1), (300, MS, True, 1), "A"),        # one lacks a meta time: file clock
    ((0, MS, True, 1), (300, MS + 5, True, 1), "B"),
    ((0, MS, True, 1), (0, MS, True, 1), "A"),              # equal file times: A
    ((900, MS + 9, False, 1), (300, MS, True, 1), "B"),     # an invalid meta is skipped
    ((900, MS, False, 1), (300, MS + 1, False, 1), "A"),    # all invalid: all present compete
    ((300, MS, True, 9999), (301, MS, True, 1), "B"),       # play time plays no part
    ((300, MS, True, 1), (300, MS, True, 9999), "A"),
    (None, (300, MS, True, 1), "B"),
    (None, None, None),
])
def test_head_of_agrees_with_savedir_newest(a, b, expected):
    (view_a, kept_a), (view_b, kept_b) = _member(0, a), _member(1, b)
    newest = savedir.SlotView(SLOT, view_a, view_b).newest
    head = m.head_of(kept_a, kept_b)
    assert (newest.label if newest else None) == expected
    assert (head.member if head else None) == expected


def test_a_capture_reports_the_head_it_was_given():
    a, b = obs("A", V1, ts=100), obs("B", V2, ts=200)
    # The scanner passes SlotView.newest; the capture never re-derives it.
    chosen = Capture(LiveRef("steam", ACCOUNT), SLOT, a, b, 300, "exact", "scan", "A")
    assert chosen.head is a and chosen.pair == (V1, V2)
    empty = Capture(LiveRef("steam", ACCOUNT), 4, None, None, 300, "exact", "scan", None)
    assert empty.pair == m.EMPTY_PAIR and empty.head is None and empty.members == ()


# --- other rules --------------------------------------------------------------


@pytest.mark.parametrize("parent, child, kind", [
    (snap(100, 10), snap(160, 20), "play"),
    (snap(100, 10), snap(100, 20), "touch"),     # re-saved after a load
    (snap(100, 10), snap(100, 10), "edit"),      # same time and play, different bytes
    (snap(100, 10), snap(90, 20), "unknown"),    # less play: never a step of descent
    (snap(100, 20), snap(100, 10), "unknown"),
    # A meta time against a file time: no touch and no edit can be read from them.
    (snap(100, 10), snap(100, 20, ts_src="mtime"), "unknown"),
    (snap(100, 10, ts_src="index"), snap(100, 10), "unknown"),
    (snap(100, 10, ts_src="index"), snap(160, 5), "play"),
])
def test_edge_kind_from_the_meta_deltas(parent, child, kind):
    assert m.edge_kind(parent, child) == kind


def test_version_order_breaks_ties_by_first_seen_then_vid():
    def info(vid, first_seen, ts=100, play=5):
        return m.VersionInfo(vid, PID, "meta", "steam", snap(play, ts), ts,
                             frozenset("A"), first_seen, ())

    ordered = sorted([info(V2, (50, 1)), info(V1, (50, 1)), info(V3, (40, 9)),
                      info(V3, (1, 1), ts=99)], key=m.version_order)
    assert [(i.vid, i.first_seen) for i in ordered] == [
        (V3, (1, 1)), (V3, (40, 9)), (V1, (50, 1)), (V2, (50, 1))]


def test_pair_string_form():
    assert m.pair_str((V1, None)) == V1 + "/-"
    assert m.pair_from_str(V1 + "/-") == (V1, None)
    assert m.pair_from_str("-/-") == m.EMPTY_PAIR
    for bad in ("-", V1, "x/-", "-/-/-"):
        with pytest.raises(ModelError):
            m.pair_from_str(bad)


# --- JSON codecs --------------------------------------------------------------


def test_capture_round_trips_through_ascii_json():
    steam = capture(LiveRef("steam", ACCOUNT), 9, obs("A", V1, ts=100), obs("B", V2, ts=200))
    xbox = capture(LiveRef("xbox", "0009000000000001", "4be1c2d3"), 4, None,
                   obs("B", V3, platform="xbox", xbox_sync=2))
    entry = capture(EntryRef("full-steam-20261005-174129"), 2, None, None, 1_791_000_000,
                    "estimated", "entry")
    store = capture(StoreRef("preserve-steam-20261016-080054"), 9, obs("A", V1), None,
                    1_792_137_654, "exact", "entry")
    for c in (steam, xbox, entry, store):
        assert m.capture_from_json(roundtrip(m.capture_to_json(c))) == c


def test_names_and_notes_text_survive_as_unicode():
    name = "שמירה 旅 \U0001f680"   # Hebrew, CJK, emoji
    member = MemberObs("A", V1, PID, "meta", "steam", snap(name=name, summary="été"))
    assert m.member_from_json(roundtrip(m.member_to_json(member))) == member


def test_the_journal_member_form_is_keyed_by_vid():
    """The member fields of a journal obs record (D1 2.6.1): short keys, vid outside."""
    record = {"member": "B", "pid": PID, "pid_src": "meta", "plat": "steam",
              "pt": 1916188, "ts": 1791924000, "ts_src": "meta", "name": "Main Save (S2)",
              "summary": "...", "diff": 2, "mode": 1, "season": 0, "bv": 4220, "fmt": 2004,
              "dsize": 18812345, "size": 8812345, "msha": "", "valid": True,
              "ft": 1791924003.25}
    member = m.member_from_json(record, vid=V2)
    assert (member.vid, member.member, member.meta.play_time, member.meta.data_size,
            member.meta.size_decompressed, member.meta.file_time, member.xbox_sync) == (
        V2, "B", 1916188, 8812345, 18812345, 1791924003.25, None)
    assert m.member_to_json(member, with_vid=False) == record
    assert m.member_from_json(dict(record, vid=V2), vid=V2) == member   # agreeing copy is fine
    with pytest.raises(ModelError, match="disagrees"):
        m.member_from_json(dict(record, vid=V1), vid=V2)


def test_target_and_source_forms():
    target = LiveTarget("steam", ACCOUNT, 9)
    assert m.target_to_json(target) == {"platform": "steam", "account": ACCOUNT,
                                        "instance": "", "slot": 9}
    assert m.target_from_json(roundtrip(m.target_to_json(target))) == target
    assert target.ref.target(9) == target
    assert m.source_to_json(EntryRef("full-steam-20261005-174129")) == {
        "kind": "entry", "entry": "full-steam-20261005-174129"}
    assert m.source_from_json({"kind": "store", "entry": "x1"}) == StoreRef("x1")
    assert m.pair_from_json(roundtrip(m.pair_to_json((None, V1)))) == (None, V1)


def _capture_json() -> dict:
    return m.capture_to_json(capture(LiveRef("steam", ACCOUNT), 9, obs("A", V1),
                                     obs("B", V2)))


@pytest.mark.parametrize("corrupt", [
    lambda d: d.update(slot=0),
    lambda d: d.update(slot=16),
    lambda d: d.update(slot=True),                       # a bool is not a slot number
    lambda d: d.update(q="guessed"),
    lambda d: d.pop("b"),
    lambda d: d.pop("head"),
    lambda d: d.update(head="C"),
    lambda d: d.update(b=None, head="B"),                # the head must be present
    lambda d: d.update(a=None, b=None),                  # an empty slot has no head
    lambda d: d.update(where={"kind": "folder", "entry": "x"}),
    lambda d: d["where"].update(platform="ps5"),
    lambda d: d["where"].update(account="..\\elsewhere"),
    lambda d: d["where"].update(account=""),
    lambda d: d["where"].update(account=".."),
    lambda d: d["where"].update(account="X:"),          # a drive, not a folder name
    lambda d: d["where"].update(account="st_1:stream"),  # an NTFS stream
    lambda d: d["where"].update(account="st_1\x00"),
    lambda d: d["where"].update(instance="ABCDEF12"),    # '' or 8 lower-case hex digits
    lambda d: d["where"].update(instance="4be1c2d"),
    lambda d: d["where"].update(platform="xbox"),        # steam members in an xbox folder
    lambda d: d["a"].update(vid="A" * 64),               # upper-case: not a vid
    lambda d: d["a"].update(member="B"),                 # B stored in the A position
    lambda d: d["a"].update(pid="dddad1bdd39f9c72"),
    lambda d: d["a"].update(pid="0" * 16),
    lambda d: d["a"].update(pid_src="guess"),
    lambda d: d["a"].update(pid_src="legacy"),           # an id pid did not come from a name
    lambda d: d["a"].update(pid="legacy:steam:9:Main", pid_src="meta"),
    lambda d: d["a"].update(pt=-1),
    lambda d: d["a"].update(pt=True),
    lambda d: d["a"].update(ts="1790000000"),
    lambda d: d["a"].update(msha="nope"),
    lambda d: d["a"].pop("msha"),                        # every record we write has it
    lambda d: d["a"].update(valid=1),
    lambda d: d["a"].update(ft=-1.0),
    lambda d: d["a"].update(ft=float("nan")),
    lambda d: d["a"].update(sync=-2),
    lambda d: d["a"].pop("dsize"),
    lambda d: d.update(a=[]),
])
def test_malformed_captures_are_refused(corrupt):
    data = _capture_json()
    corrupt(data)
    with pytest.raises(ModelError):
        m.capture_from_json(data)


def test_an_entry_capture_may_hold_either_platform():
    """Only a live folder fixes the platform; an entry's own members say what they are."""
    c = capture(EntryRef("imported-20260101-000000"), 3, obs("A", V1, platform="xbox"), None)
    assert m.capture_from_json(roundtrip(m.capture_to_json(c))) == c


def test_malformed_pairs_and_targets_are_refused():
    for bad in ([V1], [V1, V2, None], (V1, V2), [V1, "x"], "-/-"):
        with pytest.raises(ModelError):
            m.pair_from_json(bad)
    with pytest.raises(ModelError):
        m.target_from_json({"platform": "steam", "account": ACCOUNT, "slot": 3})  # no instance
    with pytest.raises(ModelError):
        m.target_from_json({"platform": "steam", "account": ACCOUNT, "slot": 3,
                            "instance": "xyz"})
