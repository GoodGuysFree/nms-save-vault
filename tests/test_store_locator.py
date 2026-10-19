"""Version store B-7: StoreLocator locate / open / is_durable and damage records (D3 T10, read
part; T6 open part)."""
from __future__ import annotations

import hashlib
import itertools
from datetime import timedelta

import pytest

from nms_save_vault.core import formats, meta, msstore
from nms_save_vault.core import store as vstore
from nms_save_vault.core.organizer.model import EntryRef, LiveTarget, StoreRef
from nms_save_vault.core.store.d1 import (
    LiveMember,
    Location,
    PreserveFailed,
    VaultLocator,
    VersionUnavailable,
)

from .conftest import steam_save
from .store_helpers import (
    BIG,
    FIXED_NOW,
    IDENT,
    STEAM_ACCOUNT,
    new_vault,
    open_vault_store,
    steam_live,
)
from .test_store_capture_xbox import wgs_live

SNAP = vstore.CaptureScope()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _ticking_clock():
    ticks = itertools.count()
    return lambda: FIXED_NOW + timedelta(minutes=next(ticks))


def _snap(store, live):
    return store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT,
                               kind="snapshot", label="t", scope=SNAP)


def _two_snapshots(tmp_path):
    """Slot 1 A kept twice with the same data and two metas (the save time changed)."""
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault, clock=_ticking_clock())
    first = _snap(store, live)
    steam_save(live, 1, 0, "Main", 7200, IDENT, 1_790_000_500, extra=BIG)
    second = _snap(store, live)
    vid = _sha((live / "save.hg").read_bytes())
    return vault, live, store, vstore.StoreLocator(store, live_dir=lambda t: live), vid, first, second


def test_locate_lists_every_entry_newest_first(tmp_path):
    _vault, _live, store, loc, vid, first, second = _two_snapshots(tmp_path)
    assert loc.locate(vid) == [
        Location(vid, "store", StoreRef(second.id), 1, "A", "steam", durable=True),
        Location(vid, "store", StoreRef(first.id), 1, "A", "steam", durable=True)]
    assert store.vid_refs(vid) == ((second.id, 1, "A"), (first.id, 1, "A"))
    assert loc.locate("0" * 64) == []


def test_open_verifies_and_picks_the_meta_of_the_location(tmp_path):
    _vault, live, _store, loc, vid, first, second = _two_snapshots(tmp_path)
    newest = loc.open(vid)
    assert newest.data == (live / "save.hg").read_bytes()
    assert newest.location.ref == StoreRef(second.id)
    assert (newest.platform, newest.meta_format) == ("steam", formats.META_FORMAT_4)
    assert newest.meta_plain == meta.decrypt((live / "mf_save.hg").read_bytes(), 2)
    ts = lambda plain: int.from_bytes(plain[formats.OFF_TIMESTAMP:formats.OFF_TIMESTAMP + 4],  # noqa: E731
                                      "little")
    assert ts(newest.meta_plain) == 1_790_000_500
    older = loc.locate(vid)[1]
    assert ts(loc.open(vid, prefer=older).meta_plain) == 1_790_000_000
    elsewhere = Location(vid, "entry", EntryRef("full-steam-20260101-000000"), 1, "A", "steam", True)
    assert loc.open(vid, prefer=elsewhere).location.ref == StoreRef(second.id)


def test_open_decrypts_a_moved_meta_with_its_recorded_ordinal(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    (live / "save5.hg").write_bytes((live / "save.hg").read_bytes())
    (live / "mf_save5.hg").write_bytes((live / "mf_save.hg").read_bytes())
    store = open_vault_store(vault)
    entry = _snap(store, live)
    loc = vstore.StoreLocator(store, live_dir=lambda t: live)
    moved = next(x for x in loc.locate(entry.member(3, "A").vid) if x.slot == 3)
    opened = loc.open(moved.vid, prefer=moved)
    assert opened.location == moved
    assert opened.meta_plain == meta.decrypt((live / "mf_save.hg").read_bytes(), 2)


def test_open_of_an_xbox_version_returns_the_meta_as_stored(tmp_path, make_wgs_account):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    store = open_vault_store(vault)
    entry = store.add_from_live(acct, platform="xbox", account_key=acct.name, kind="snapshot",
                                label="t", scope=SNAP)
    m = entry.member(1, "B")
    opened = vstore.StoreLocator(store, live_dir=lambda t: acct).open(m.vid)
    assert opened.platform == "xbox" and opened.meta_format == m.meta_format
    assert opened.data == (acct / m.data).read_bytes()
    assert opened.meta_plain == (acct / m.meta).read_bytes()


def test_a_damaged_object_is_recorded_and_not_durable_once_gone(tmp_path):
    vault, _live, store, loc, vid, first, second = _two_snapshots(tmp_path)
    path = store.objects.path_for(vid)
    good = path.read_bytes()
    path.write_bytes(good[:-1] + bytes([good[-1] ^ 1]))
    with pytest.raises(VersionUnavailable):
        loc.open(vid)
    with pytest.raises(VersionUnavailable):
        loc.open(vid, prefer=loc.locate(vid)[0])
    records = {(r.entry_id, r.path, r.problem): r for r in store.damage()}
    assert set(records) == {(second.id, "save.hg", "corrupt"), (first.id, "save.hg", "corrupt")}
    assert records[(second.id, "save.hg", "corrupt")].count == 2
    assert all(r.digest == vid for r in records.values())
    assert path.read_bytes() != good                  # reads never write store data
    assert loc.is_durable(vid)                        # there, with its size
    path.unlink()
    assert not loc.is_durable(vid)
    with pytest.raises(VersionUnavailable):
        loc.open(vid)
    assert {r.problem for r in store.damage() if r.entry_id == second.id} == {"missing"}


def test_an_unreadable_steam_meta_is_unavailable_but_not_damage(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    (live / "mf_save2.hg").write_bytes(b"\x00" * 432)
    store = open_vault_store(vault)
    entry = _snap(store, live)
    loc = vstore.StoreLocator(store, live_dir=lambda t: live)
    with pytest.raises(VersionUnavailable, match="unreadable"):
        loc.open(entry.member(1, "B").vid)
    assert store.damage() == []
    assert loc.is_durable(entry.member(1, "B").vid)


def test_d1s_vault_locator_composes_with_the_store(tmp_path):
    _vault, live, store, loc, vid, _first, second = _two_snapshots(tmp_path)
    vault_locator = VaultLocator([loc], loc)
    assert vault_locator.open(vid).location.ref == StoreRef(second.id)
    assert vault_locator.is_durable(vid) and not vault_locator.is_durable("1" * 64)
    with pytest.raises(VersionUnavailable):
        vault_locator.open("1" * 64)
    assert issubclass(vstore.ObjectMissing, VersionUnavailable)
    assert issubclass(vstore.ObjectCorrupt, VersionUnavailable)


# --- preserve (B-8) ------------------------------------------------------------------------


def _target(slot, platform="steam", account=STEAM_ACCOUNT):
    return LiveTarget(platform, account, slot)


def _steam_locator(tmp_path, **kw):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault, clock=_ticking_clock(), **kw)
    return vault, live, store, vstore.StoreLocator(store, live_dir=lambda t: live)


def test_preserve_contract(tmp_path):
    _vault, live, store, loc = _steam_locator(tmp_path)
    vid_a = _sha((live / "save.hg").read_bytes())
    kept = loc.preserve([LiveMember(_target(1), "A", vid_a)], op_id="op-20261016T111500Z-ab12",
                        reason="place")
    entry = store.entry(kept[0].ref.key)
    assert kept == [Location(vid_a, "store", StoreRef(entry.id), 1, "A", "steam", durable=True)]
    assert entry.id.startswith("preserve-steam-") and entry.kind == "preserve"
    assert (entry.label, entry.op_id, entry.reason) == ("kept: slot 1 (Main)",
                                                       "op-20261016T111500Z-ab12", "place")
    assert [f.path for f in entry.files] == ["mf_save.hg", "mf_save2.hg", "save.hg", "save2.hg"]
    assert [(m.slot, m.member) for m in entry.members] == [(1, "A"), (1, "B")]   # both members
    assert loc.open(vid_a, prefer=kept[0]).data == (live / "save.hg").read_bytes()
    assert loc.is_durable(vid_a)


def test_preserve_is_written_every_time_and_keeps_the_input_order(tmp_path):
    _vault, live, store, loc = _steam_locator(tmp_path)
    vid_a = _sha((live / "save.hg").read_bytes())
    vid_b = _sha((live / "save2.hg").read_bytes())
    vid_2 = _sha((live / "save3.hg").read_bytes())
    first = loc.preserve([LiveMember(_target(1), "A", vid_a)], op_id="op-1", reason="r")
    objects = sorted(store.objects.digests())
    kept = loc.preserve([LiveMember(_target(2), "A", vid_2), LiveMember(_target(1), "B", vid_b),
                         LiveMember(_target(1), "A", vid_a)], op_id="op-2", reason="r")
    assert [(k.vid, k.slot, k.member) for k in kept] == [(vid_2, 2, "A"), (vid_b, 1, "B"),
                                                        (vid_a, 1, "A")]
    assert kept[1].ref == kept[2].ref != first[0].ref != kept[0].ref
    assert sorted(store.objects.digests()) == objects           # deduplicated, still recorded
    assert len([e for e in store.entries() if e.kind == "preserve"]) == 3


def test_preserve_refuses_a_slot_that_changed(tmp_path):
    _vault, live, store, loc = _steam_locator(tmp_path)
    with pytest.raises(vstore.PreserveMismatch, match="changed"):
        loc.preserve([LiveMember(_target(1), "A", "0" * 64)], op_id="op-1", reason="r")
    assert isinstance(vstore.PreserveMismatch("x"), PreserveFailed)
    assert store.entries() == []
    with pytest.raises(PreserveFailed):
        loc.preserve([LiveMember(_target(9), "A", "0" * 64)], op_id="op-1", reason="r")


def test_preserve_of_an_xbox_slot(tmp_path, make_wgs_account):
    vault = new_vault(tmp_path)
    acct = wgs_live(make_wgs_account, tmp_path / "live")
    store = open_vault_store(vault)
    loc = vstore.StoreLocator(store, live_dir=lambda t: acct)
    slot1 = msstore.scan(acct).slots[1]
    vid_b = _sha(slot1.b.data_path.read_bytes())
    kept = loc.preserve([LiveMember(_target(1, "xbox", acct.name), "B", vid_b)],
                        op_id="op-1", reason="r")
    entry = store.entry(kept[0].ref.key)
    assert entry.id.startswith("preserve-xbox-") and entry.xbox["index_kind"] == "rebuilt"
    assert [(m.slot, m.member) for m in entry.members] == [(1, "A"), (1, "B")]
    assert loc.open(vid_b, prefer=kept[0]).meta_plain == slot1.b.meta_path.read_bytes()


def test_preserve_rereads_what_it_kept(tmp_path):
    _vault, live, store, loc = _steam_locator(tmp_path)
    vid_a = _sha((live / "save.hg").read_bytes())

    def medium_garbles_it(at):
        if at == "entry.before_replace":
            path = store.objects.path_for(vid_a)
            raw = path.read_bytes()
            path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))

    store._faults = medium_garbles_it
    with pytest.raises(PreserveFailed, match="damaged"):
        loc.preserve([LiveMember(_target(1), "A", vid_a)], op_id="op-1", reason="r")
    assert [(r.path, r.problem) for r in store.damage()] == [("save.hg", "corrupt")]


def test_space_needed_for_preserve(tmp_path):
    _vault, live, store, loc = _steam_locator(tmp_path)
    vid_a = _sha((live / "save.hg").read_bytes())
    vid_b = _sha((live / "save2.hg").read_bytes())
    both = [LiveMember(_target(1), "A", vid_a), LiveMember(_target(1), "B", vid_b)]
    sizes = (live / "save.hg").stat().st_size + (live / "save2.hg").stat().st_size
    assert loc.space_needed_for_preserve(both) == sizes + 64 * 1024
    loc.preserve(both, op_id="op-1", reason="r")
    assert loc.space_needed_for_preserve(both) == 64 * 1024
    assert loc.space_needed_for_preserve(both[:1]) == (live / "save2.hg").stat().st_size + 64 * 1024
    assert loc.space_needed_for_preserve([LiveMember(_target(9), "A", vid_a)]) == 64 * 1024
