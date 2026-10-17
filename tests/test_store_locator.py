"""Version store B-7: StoreLocator locate / open / is_durable and damage records (D3 T10, read
part; T6 open part)."""
from __future__ import annotations

import hashlib
import itertools
from datetime import timedelta

import pytest

from nms_save_vault.core import formats, meta
from nms_save_vault.core import store as vstore
from nms_save_vault.core.organizer.model import EntryRef, StoreRef
from nms_save_vault.core.store.d1 import Location, VaultLocator, VersionUnavailable

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
