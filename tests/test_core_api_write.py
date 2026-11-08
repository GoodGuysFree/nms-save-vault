"""core.api's write side (Savecraft): an edit of a live Steam save and its undo through a
LiveSession, which honours NMS Save Vault's settings (whatever path reaches a folder, re-read
at every refresh), the game-running gate, the vault's lock and interrupted writes, and never
offers a stale edit again."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

from nms_save_vault.core import api, appinfo, meta, platform, safety, savejson, slotmap, state
from nms_save_vault.core.hashname import key as K
from nms_save_vault.core.liveops import FaultInjector, SimulatedCrash

from .conftest import steam_save
from .liveops_support import MAIN, T0, host_in, steam_world, tree_hash

SLOT1 = ("save.hg", "mf_save.hg", "save2.hg", "mf_save2.hg")
SC = "nms-savecraft"
UNITS = [api.AddMember((), "Units", b"9")]


@pytest.fixture
def savecraft():
    previous = appinfo.set_app(SC, "0.1.0")
    try:
        yield
    finally:
        appinfo.set_app(previous.name, previous.version)


@pytest.fixture
def world(tmp_path, monkeypatch, savecraft):
    """The test world with slot 1 written as the game writes it, this machine's folders and
    NMS Save Vault's settings inside the sandbox, and the game not running."""
    w = steam_world(tmp_path)
    for member, play, ts in ((0, 7200, T0 + 100), (1, 7100, T0 + 50)):
        body = b'{"' + K("SaveName").encode() + b'":"Main","m":%d}' % member
        steam_save(w.live, 1, member, "Main", play, MAIN, ts, payload=body + b"\x00")
    host = host_in(tmp_path)
    monkeypatch.setattr(platform.Host, "current", classmethod(lambda cls: host))
    monkeypatch.setattr(state, "default_state_path", lambda: tmp_path / "own" / "state.json")
    monkeypatch.setattr(safety, "is_game_running", lambda *a, **k: False)
    w.own = tmp_path / "own" / "state.json"
    w.own.parent.mkdir()
    return w


def _session(w, live=None):
    return api.open_live(w.vault_root, live_dirs=[live or w.live])


def _slot(w):
    return api.slot_of(w.live, 1)


def _units(w):
    return savejson.decode_save((w.live / "save.hg").read_bytes()).root.get(K("Units"))


def _entries(w):
    return len(api.open_vault(w.vault_root, live_roots=()).entries())


def _switch_off(w, path, **off):
    source = {"id": "steam-x", "platform": "steam", "path": str(path), **off}
    w.own.write_text(json.dumps({"vault": None, "sources": [source]}), encoding="utf-8")


def _meta_words_changed(old: bytes, new: bytes, file_no: int) -> set[int]:
    o = slotmap.storage_ordinal(file_no)
    a, b = meta.decrypt(old, o), meta.decrypt(new, o)
    return {i - i % 4 for i in range(len(a)) if a[i] != b[i]}


def test_an_edit_and_its_undo_go_through_the_session(world):
    before = {n: (world.live / n).read_bytes() for n in SLOT1}
    session = _session(world)
    plan = session.plan_edit(_slot(world), [api.AddMember((), "Units", api.dumps(4242))],
                             what="add units")
    assert not plan.blocked and plan.undoes is None and len(plan.made) == 1
    assert (world.live / "save.hg").read_bytes() == before["save.hg"]      # planned, not made
    result = session.write(plan, plan.confirmations)
    assert result.status == "done", result
    assert _units(world) == 4242 and result.undo
    history = api.open_history(world.vault_root)
    assert history.made_by(SC) == set(plan.made)
    assert [c.op_id for c in history.changes_by(SC)] == [result.op_id]

    undo = session.plan_undo(result.op_id)
    assert undo.undoes == result.op_id and not undo.blocked
    assert session.write(undo, undo.confirmations).status == "done"
    after = {n: (world.live / n).read_bytes() for n in SLOT1}
    assert {n: after[n] for n in ("save.hg", "save2.hg")} == {
        n: before[n] for n in ("save.hg", "save2.hg")}
    # the metas are the originals but for the save time every write sets (0x164)
    assert _meta_words_changed(before["mf_save.hg"], after["mf_save.hg"],
                               slotmap.file_no(1, 0)) <= {0x164}
    assert _meta_words_changed(before["mf_save2.hg"], after["mf_save2.hg"],
                               slotmap.file_no(1, 1)) <= {0x164}


def test_an_app_that_has_not_named_itself_cannot_open_a_session(world):
    appinfo.set_app(appinfo.VAULT, "1.0")
    with pytest.raises(ValueError, match="set_app"):
        _session(world)


@pytest.mark.parametrize("where", ["nowhere", "live"])
def test_a_session_needs_a_vault_and_writes_nothing_where_there_is_none(world, tmp_path, where):
    root = tmp_path / "nowhere" if where == "nowhere" else world.live
    live_before = tree_hash(world.live)
    with pytest.raises(api.StoreError):
        api.open_live(root, live_dirs=[world.live])
    assert tree_hash(world.live) == live_before and not (world.live / "organizer").exists()


@pytest.mark.parametrize("off", [{"enabled": False}, {"writable": False}, {"role": "backup"}])
def test_a_folder_switched_off_in_nms_save_vault_is_never_written(world, off):
    _switch_off(world, world.live, **off)
    live_before = tree_hash(world.live)
    with pytest.raises(api.TargetError) as refused:
        _session(world).plan_edit(_slot(world), UNITS, what="x")
    assert refused.value.code == "NOT_WRITABLE"
    assert tree_hash(world.live) == live_before


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
@pytest.mark.parametrize("settings_side", ["real", "junction"])
def test_a_switched_off_folder_reached_through_a_junction_is_not_written(world, tmp_path,
                                                                         settings_side):
    import _winapi

    link = tmp_path / "elsewhere" / world.live.name
    link.parent.mkdir()
    _winapi.CreateJunction(str(world.live), str(link))
    real_spelling = str(world.live).upper() + os.sep        # case and a trailing separator too
    _switch_off(world, real_spelling if settings_side == "real" else link, enabled=False)
    live_before = tree_hash(world.live)
    session = _session(world, link if settings_side == "real" else world.live)
    with pytest.raises(api.TargetError) as refused:
        session.plan_edit(api.slot_of(link if settings_side == "real" else world.live, 1),
                          UNITS, what="x")
    assert refused.value.code == "NOT_WRITABLE"
    assert tree_hash(world.live) == live_before


def test_a_folder_switched_off_during_a_session_is_not_written_after_a_refresh(world):
    session = _session(world)
    _switch_off(world, world.live, writable=False)
    session.refresh()
    with pytest.raises(api.TargetError):
        session.plan_edit(_slot(world), UNITS, what="x")


def test_a_folder_switched_off_after_the_plan_is_not_written(world):
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    _switch_off(world, world.live, writable=False)              # no refresh by the app
    live_before = tree_hash(world.live)
    with pytest.raises(api.TargetError):
        session.write(plan, plan.confirmations)
    assert tree_hash(world.live) == live_before


def test_the_settings_a_pointer_names_come_first_only_while_they_choose_its_vault(
        world, tmp_path):
    host = platform.Host.current()
    vault_a, vault_b = tmp_path / "VaultA", tmp_path / "VaultB"
    vault_a.mkdir()
    vault_b.mkdir()
    named = tmp_path / "kit" / "state.json"
    named.parent.mkdir()
    pointer = state.pointer_path(host)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"vault": str(vault_a), "state": str(named)}),
                       encoding="utf-8")
    world.own.write_text(json.dumps({"vault": None, "theme": "own"}), encoding="utf-8")
    for chooses, first in ((vault_a, "kit"), (vault_b, "own")):
        named.write_text(json.dumps({"vault": str(chooses), "theme": "kit"}), encoding="utf-8")
        assert [s.theme for s in state.vault_settings(host)][0] == first

@pytest.mark.parametrize("damaged", ["state", "pointer"])
def test_unreadable_vault_settings_stop_the_session(world, damaged):
    if damaged == "state":
        path = world.own
    else:
        path = state.pointer_path(platform.Host.current())
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{damaged", encoding="utf-8")
    with pytest.raises(ValueError, match="settings"):
        _session(world)
    assert path.read_text(encoding="utf-8") == "{damaged"            # never moved aside


def test_settings_the_pointer_names_that_are_gone_stop_the_session(world, tmp_path):
    pointer = state.pointer_path(platform.Host.current())
    pointer.parent.mkdir(parents=True, exist_ok=True)
    gone = tmp_path / "unplugged" / "state.json"        # e.g. a portable kit on a removed drive
    pointer.write_text(json.dumps({"vault": str(world.vault_root), "state": str(gone)}),
                       encoding="utf-8")
    live_before = tree_hash(world.live)
    with pytest.raises(ValueError, match="cannot be found"):
        _session(world)
    assert tree_hash(world.live) == live_before


def test_a_folder_with_the_name_of_one_nms_save_vault_uses_is_refused(world, tmp_path):
    elsewhere = tmp_path / "copy" / world.live.name
    elsewhere.mkdir(parents=True)
    _switch_off(world, elsewhere, enabled=True)           # Vault lists another folder so named
    with pytest.raises(ValueError, match="another folder of that name"):
        _session(world)


def test_a_slot_the_game_saved_after_the_plan_is_not_written(world):
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    body = b'{"' + K("SaveName").encode() + b'":"Main","m":2}'
    steam_save(world.live, 1, 0, "Main", 7300, MAIN, T0 + 400, payload=body + b"\x00")
    saved = tree_hash(world.live)
    for _ in range(2):                                   # and not on a second try either
        assert session.write(plan, plan.confirmations).status == "stale"
        assert tree_hash(world.live) == saved


@pytest.mark.parametrize("probe", [True, False, None])
def test_an_app_can_tell_whether_the_game_runs(monkeypatch, probe):
    monkeypatch.setattr(safety, "is_game_running", lambda: probe)
    assert api.game_running() is probe


def test_nothing_is_written_while_the_game_runs(world, monkeypatch):
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    monkeypatch.setattr(safety, "is_game_running", lambda *a, **k: True)
    session.refresh()
    live_before, entries = tree_hash(world.live), _entries(world)
    result = session.write(plan, plan.confirmations)
    assert (result.status, result.code) == ("refused", "GAME_RUNNING")
    with pytest.raises(api.EditRefused, match="running"):      # nor planned, nor kept
        session.plan_edit(_slot(world), UNITS, what="y")
    assert tree_hash(world.live) == live_before and _entries(world) == entries


def test_an_edit_that_cannot_be_made_is_refused_before_anything_is_kept(world):
    session = _session(world)
    entries = _entries(world)
    with pytest.raises(api.PatchRefused):
        session.plan_edit(_slot(world), [api.Replace(("Nope",), b"1")], what="x")
    with pytest.raises(api.EditRefused, match="change nothing"):
        session.plan_edit(_slot(world), [], what="x")
    assert _entries(world) == entries


def test_a_meta_written_for_another_slot_is_not_edited(world):
    ordinal = slotmap.storage_ordinal(slotmap.file_no(1, 0))
    other = slotmap.storage_ordinal(slotmap.file_no(5, 0))
    mf = world.live / "mf_save.hg"          # moved by hand: perhaps another save's meta
    mf.write_bytes(meta.re_key(mf.read_bytes(), ordinal, other))
    live_before, entries = tree_hash(world.live), _entries(world)
    with pytest.raises(api.EditRefused, match="another slot"):
        _session(world).plan_edit(_slot(world), UNITS, what="x")
    assert tree_hash(world.live) == live_before and _entries(world) == entries


def test_only_this_app_s_operations_are_undone_here(world):
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    done = session.write(plan, plan.confirmations)
    appinfo.set_app("another-app", "1.0")
    with pytest.raises(api.EditRefused, match="not made by this app"):
        session.plan_undo(done.op_id)
    assert session.plan_undo("no-such-op").blocked              # says why, as any undo

def test_an_interrupted_edit_is_listed_and_put_back(world, monkeypatch):
    before = {n: (world.live / n).read_bytes() for n in ("save.hg", "save2.hg")}
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    make = session._make

    def crashing():                         # the context the write runs in dies mid-write
        ctx = make()
        ctx.faults = FaultInjector({"swapped:2"})
        return ctx

    monkeypatch.setattr(session, "_make", crashing)
    with pytest.raises(SimulatedCrash):
        session.write(plan, plan.confirmations)
    monkeypatch.setattr(session, "_make", make)
    session.refresh()
    with pytest.raises(api.EditRefused, match="interrupted"):      # nothing new starts on it
        session.plan_edit(_slot(world), UNITS, what="y")
    (item,) = session.interrupted()
    assert item.app == SC and "back" in item.choices and item.recommended in item.choices
    for wrong in ("Back", "none", "sideways"):
        with pytest.raises(ValueError, match="not offered"):
            session.resolve(item.op_id, wrong)
    result = session.resolve(item.op_id, "back")
    assert result.status == "rolled_back" and session.interrupted() == []
    assert {n: (world.live / n).read_bytes() for n in before} == before


def test_an_interrupted_edit_in_a_folder_switched_off_since_offers_nothing(world, monkeypatch):
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    make = session._make

    def crashing():
        ctx = make()
        ctx.faults = FaultInjector({"swapped:2"})
        return ctx

    monkeypatch.setattr(session, "_make", crashing)
    with pytest.raises(SimulatedCrash):
        session.write(plan, plan.confirmations)
    monkeypatch.setattr(session, "_make", make)
    _switch_off(world, world.live, writable=False)
    session.refresh()
    torn = tree_hash(world.live)
    (item,) = session.interrupted()
    assert (item.recommended, item.choices) == ("none", ())
    assert "cannot be finished or put back now" in item.text and "read-only" in item.text
    with pytest.raises(ValueError, match="not offered"):
        session.resolve(item.op_id, "leave")
    assert tree_hash(world.live) == torn


def test_a_write_s_result_survives_a_refresh_that_fails(world, monkeypatch):
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    make = session._make
    calls = []

    def flaky():                            # the refresh after the write fails
        calls.append(1)
        if len(calls) == 2:
            raise OSError("the vault is busy")
        return make()

    monkeypatch.setattr(session, "_make", flaky)
    result = session.write(plan, plan.confirmations)
    assert result.status == "done" and result.op_id and _units(world) == 9
    assert session.plan_undo(result.op_id).undoes == result.op_id     # refreshed on next use
