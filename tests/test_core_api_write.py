"""core.api's write side (Savecraft): an edit of a live Steam save and its undo through a
LiveSession, which honours NMS Save Vault's settings (whatever path reaches a folder, re-read
at every refresh), the game-running gate, the vault's lock and interrupted writes, and never
offers a stale edit again."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

import pytest

from nms_save_vault.core import api, appinfo, liveops, meta, platform, safety, savejson, slotmap
from nms_save_vault.core import state
from nms_save_vault.core.hashname import key as K
from nms_save_vault.core.liveops import FaultInjector, SimulatedCrash, opjournal
from nms_save_vault.core.organizer import store as orgstore
from nms_save_vault.core.store import versionstore

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


def _crashed_edit(world, monkeypatch):
    """A session whose edit stopped half-way (one member swapped in), the slot left torn."""
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
    return session


def test_an_interrupted_edit_in_a_folder_switched_off_since_waits_for_it(world, monkeypatch):
    before = {n: (world.live / n).read_bytes() for n in SLOT1}
    session = _crashed_edit(world, monkeypatch)
    _switch_off(world, world.live, writable=False)
    session.refresh()
    torn = tree_hash(world.live)
    (item,) = session.interrupted()
    assert (item.recommended, item.choices) == ("none", ())
    assert "read-only" in item.text and "can be written again" in item.text
    assert "NOT_WRITABLE" not in item.text
    out = liveops.resolve_recovery(session._now(), item.op_id, "leave")
    assert (out.status, out.code) == ("refused", "TARGET_UNAVAILABLE")
    assert tree_hash(world.live) == torn
    world.own.unlink()                                      # switched on again
    session.refresh()
    (item,) = session.interrupted()
    assert "back" in item.choices
    assert session.resolve(item.op_id, "back").status == "rolled_back"
    assert {n: (world.live / n).read_bytes() for n in SLOT1} == before    # all four, exactly


def test_an_interrupted_edit_with_a_damaged_record_can_be_left(world, monkeypatch):
    session = _crashed_edit(world, monkeypatch)
    ctx = session._now()
    (rec,) = [r for r in opjournal.read_ops(ctx).values() if r.open]
    line = {"j": opjournal.JOURNAL_VERSION, "op_id": rec.op_id, "seq": len(rec.events) + 1,
            "event": "writing", "utc": "2026-11-08T00:00:00.000Z", "machine": rec.machine,
            "targets": [{"bogus": 1}]}
    with open(opjournal.ops_path(ctx), "a", encoding="ascii") as f:
        f.write(json.dumps(line) + "\n")
    session.refresh()
    torn = tree_hash(world.live)
    (item,) = session.interrupted()
    assert item.choices == ("leave",) and "its record is damaged" in item.text
    assert session.resolve(item.op_id, "leave").status == "rolled_back"
    assert session.interrupted() == [] and tree_hash(world.live) == torn
    session.plan_edit(_slot(world), UNITS, what="y")          # the folder is usable again


# --- CL8: an app can tell cheaply that another one changed the vault -----------------------


def test_the_vault_stamp_moves_with_keeps_writes_undos_and_damage_never_with_a_look(world):
    session = _session(world)
    session.refresh()
    quiet = api.vault_stamp(world.vault_root)
    session.refresh()                      # looking records nothing new: no refresh ping-pong
    api.open_vault(world.vault_root, live_roots=()).entries()
    assert api.vault_stamp(world.vault_root) == quiet
    plan = session.plan_edit(_slot(world), UNITS, what="x")       # the edit is kept first
    kept = api.vault_stamp(world.vault_root)
    assert kept != quiet
    done = session.write(plan, plan.confirmations)
    written = api.vault_stamp(world.vault_root)
    assert written != kept
    undo = session.plan_undo(done.op_id)
    session.write(undo, undo.confirmations)
    undone = api.vault_stamp(world.vault_root)
    assert undone != written
    damage = versionstore.store_root(world.vault_root) / versionstore.STATE / "damage.json"
    damage.write_text("{}", encoding="utf-8")            # another app found a damaged file
    assert api.vault_stamp(world.vault_root) != undone


def test_the_vault_stamp_of_a_folder_that_is_no_vault_is_still_a_stamp(tmp_path):
    assert api.vault_stamp(tmp_path / "nowhere") == api.vault_stamp(tmp_path / "nowhere")


# --- CL10: a core older than the vault's format never writes to it ------------------------


NEWER_PARTS = ["store", "organizer", "journal"]


def _newer_core_changed(vault_root, part):
    """What a newer core leaves behind: a store format that needs it to write, an organizer
    schema it made, or op-journal records of a version this core does not know."""
    folder = orgstore.store_root(vault_root)
    if part == "store":
        path = versionstore.store_root(vault_root) / versionstore.FORMAT_NAME
        d = json.loads(path.read_text(encoding="ascii"))
        d["write_compat"] = versionstore.STORE_VERSION + 1
        path.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n", encoding="ascii")
    elif part == "organizer":
        path = folder / orgstore.HEADER
        d = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {
            "kind": orgstore.KINDS[orgstore.HEADER], "vault_id": "0" * 32}
        d["schema"] = orgstore.SCHEMA + 1
        folder.mkdir(exist_ok=True)
        path.write_text(json.dumps(d), encoding="utf-8")
    else:
        line = {"j": opjournal.JOURNAL_VERSION + 1, "op_id": "op-20991231T000000Z-0000",
                "seq": 1, "event": "preparing", "utc": "2099-12-31T00:00:00Z", "machine": "m"}
        folder.mkdir(exist_ok=True)
        with open(folder / opjournal.OPS, "a", encoding="ascii") as f:
            f.write(json.dumps(line) + "\n")


@pytest.mark.parametrize("part", NEWER_PARTS)
def test_a_vault_a_newer_core_changed_is_not_edited(world, part):
    _newer_core_changed(world.vault_root, part)
    live_before, entries = tree_hash(world.live), _entries(world)
    with pytest.raises(api.EditRefused, match="newer version"):
        _session(world).plan_edit(_slot(world), UNITS, what="x")
    assert tree_hash(world.live) == live_before and _entries(world) == entries


@pytest.mark.parametrize("part", NEWER_PARTS)
def test_a_plan_made_before_a_newer_core_changed_the_vault_is_not_written(world, part):
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    _newer_core_changed(world.vault_root, part)
    live_before = tree_hash(world.live)
    result = session.write(plan, plan.confirmations)
    assert (result.status, result.code) == ("refused", "VAULT_TOO_NEW")
    assert tree_hash(world.live) == live_before


@pytest.mark.parametrize("part", NEWER_PARTS)
def test_a_context_held_while_a_newer_core_changed_the_vault_writes_nothing(world, part):
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    ctx = session._now()                    # NMS Save Vault's window keeps its context open
    _newer_core_changed(world.vault_root, part)
    live_before = tree_hash(world.live)
    out = liveops.execute(ctx, plan._plan, frozenset(plan.confirmations))
    assert (out.status, out.code) == ("refused", "VAULT_TOO_NEW")
    assert tree_hash(world.live) == live_before


@pytest.mark.parametrize("part", NEWER_PARTS)
def test_an_edit_is_not_undone_in_a_vault_a_newer_core_changed(world, part):
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    done = session.write(plan, plan.confirmations)
    _newer_core_changed(world.vault_root, part)
    undo = session.plan_undo(done.op_id)
    assert "VAULT_TOO_NEW" in {n.code for n in undo.blocked}
    live_before = tree_hash(world.live)
    assert session.write(undo, undo.confirmations).code == "VAULT_TOO_NEW"
    assert tree_hash(world.live) == live_before


@pytest.mark.parametrize("part", NEWER_PARTS)
@pytest.mark.parametrize("call", ["update", "dismiss"])
def test_verification_records_nothing_in_a_vault_a_newer_core_changed(world, part, call):
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    done = session.write(plan, plan.confirmations)
    later = lambda: time.time() + 10 ** 8           # long after: the pending check expires
    if call == "dismiss":                           # an expired check to dismiss
        ctx = session._now()
        ctx.clock = later
        liveops.update_verification(ctx)
    _newer_core_changed(world.vault_root, part)
    session.refresh()
    ctx = session._now()
    ctx.clock = later
    ops_file = opjournal.ops_path(ctx)
    before = ops_file.read_bytes()
    if call == "update":
        liveops.update_verification(ctx)
    else:
        (target,) = opjournal.read_ops(ctx)[done.op_id].targets
        liveops.dismiss_verification(ctx, done.op_id, target)
    assert ops_file.read_bytes() == before


def test_a_full_restore_repairs_no_xbox_index_in_a_vault_a_newer_core_changed(
        tmp_path, make_wgs_account):
    from nms_save_vault.core import operations as ops
    from nms_save_vault.core import store as vstore
    from nms_save_vault.core.catalog import Vault

    live = make_wgs_account(tmp_path / "live", [("Slot1Auto", '{"a":1}', "Keep", "", 1, 100)])
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    vstore.create_store(vault.root)
    backup = ops.create_full_backup(vault, live, include_cache=False)
    _newer_core_changed(vault.root, "journal")
    index = live / "containers.index"
    index.write_bytes(index.read_bytes()[:-30])            # damaged: a restore would repair it
    live_before = tree_hash(live)
    with pytest.raises(ops.OperationError, match="newer version"):
        ops.restore_full(vault, backup, live, allow_game_running=True)
    assert tree_hash(live) == live_before and not (vault.root / "damaged-index").exists()


def test_a_vault_only_a_newer_core_can_read_opens_no_session(world):
    path = versionstore.store_root(world.vault_root) / versionstore.FORMAT_NAME
    d = json.loads(path.read_text(encoding="ascii"))
    d["read_compat"] = d["write_compat"] = versionstore.STORE_VERSION + 1
    path.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n", encoding="ascii")
    with pytest.raises(api.StoreTooNew):
        _session(world)


@pytest.mark.parametrize("part", NEWER_PARTS)
def test_an_interrupted_edit_is_not_resolved_in_a_vault_a_newer_core_changed(
        world, monkeypatch, part):
    session = _crashed_edit(world, monkeypatch)
    _newer_core_changed(world.vault_root, part)
    session.refresh()
    torn = tree_hash(world.live)
    (item,) = session.interrupted()
    assert (item.recommended, item.choices) == ("none", ()) and "newer version" in item.text
    ctx = session._now()
    ops_file = opjournal.ops_path(ctx)
    before = ops_file.read_bytes()
    for choice in ("forward", "back", "leave"):     # Vault's own windows call the core directly
        out = liveops.resolve_recovery(ctx, item.op_id, choice)
        assert (out.status, out.code) == ("refused", "VAULT_TOO_NEW")
    assert tree_hash(world.live) == torn and ops_file.read_bytes() == before


def test_two_extra_folders_of_one_name_open_and_neither_is_written(world, tmp_path):
    _switch_off(world, tmp_path / "elsewhere" / "st_other", enabled=True)
    twin = tmp_path / "copy" / world.live.name
    twin.mkdir(parents=True)
    session = api.open_live(world.vault_root, live_dirs=[world.live, twin])  # not Vault's names
    live_before = tree_hash(world.live)
    with pytest.raises(api.TargetError) as refused:       # one name, two folders: ambiguous
        session.plan_edit(_slot(world), UNITS, what="x")
    assert refused.value.code == "AMBIGUOUS" and tree_hash(world.live) == live_before


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


def test_a_new_object_is_written_by_readable_names(world):
    """dumps_named: an app inserts a whole new object (an inventory slot) without knowing the
    save's keys; the hashed id's bytes come back as they were."""
    hashed = "^" + bytes.fromhex("8080B7B5DD85").decode("utf-8", "surrogateescape")
    raw = api.dumps_named({"Id": hashed, "Amount": 5, "Index": {"X": 1, "Y": 0}, "Tags": [{"Type": "a"}]})
    held = json.loads(raw.decode("utf-8", "surrogateescape"))
    assert held == {K("Id"): hashed, K("Amount"): 5, K("Index"): {K("X"): 1, K("Y"): 0},
                    K("Tags"): [{K("Type"): "a"}]}
    assert raw.startswith(b'{"' + K("Id").encode() + b'":"^\x80\x80\xb7\xb5\xdd\x85"')
    with pytest.raises(api.PatchRefused):
        api.dumps_named({"x" * 300: 1})


def test_a_session_says_which_folder_it_writes_for_a_slot(world, tmp_path):
    """Two folders can share a name; an app checks the one written is the one it read."""
    session = _session(world)
    assert session.folder_of(_slot(world)) == world.live
    elsewhere = tmp_path / "elsewhere" / world.live.name
    elsewhere.mkdir(parents=True)
    assert session.folder_of(api.slot_of(elsewhere, 1)) == world.live      # not "elsewhere"
    assert session.folder_of(api.slot_of(tmp_path / "st_76561198999999999", 1)) is None


def test_a_marker_that_cannot_be_written_never_stops_a_write(world, monkeypatch):
    """The open-operation marker is only a hint ("connect the vault drive"): a disk that
    refuses it must not leave the write open and the folder blocked."""
    real = safety.atomic_write_bytes

    def refuse_markers(path, data, **kw):
        if "marker" in str(path).lower():
            raise PermissionError(13, "Access is denied")
        return real(path, data, **kw)
    monkeypatch.setattr(safety, "atomic_write_bytes", refuse_markers)
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="units")
    assert session.write(plan, plan.confirmations).status == "done"
    assert session.interrupted() == [] and _units(world) == 9
    again = session.plan_edit(_slot(world), [api.Replace(("Units",), b"10")], what="units")
    assert not again.blocked


def test_a_write_waits_while_it_cannot_be_told_whether_the_game_runs(world, monkeypatch):
    """NMS Save Vault asks its user then; an app's write is refused (Savecraft S11)."""
    monkeypatch.setattr(safety, "is_game_running", lambda *a, **k: None)
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="units")
    assert [n.code for n in plan.blocked] == ["GAME_STATE_UNKNOWN"]
    assert "GAME_STATE_UNKNOWN" not in plan.confirmations
    result = session.write(plan, {"GAME_STATE_UNKNOWN", *plan.confirmations})
    assert result.status != "done" and _units(world) is None



def test_settling_a_stopped_write_waits_too_while_the_game_cannot_be_checked(world, monkeypatch):
    """resolve() is a live write as well: an app may not agree for the user that the game is
    closed (Savecraft S11)."""
    session = _crashed_edit(world, monkeypatch)
    torn = {n: (world.live / n).read_bytes() for n in SLOT1}
    (item,) = [i for i in session.interrupted() if "back" in i.choices]
    monkeypatch.setattr(safety, "is_game_running", lambda *a, **k: None)
    session.refresh()
    result = session.resolve(item.op_id, "back", {"GAME_STATE_UNKNOWN"})
    assert result.status == "refused" and result.code == "GAME_STATE_UNKNOWN"
    assert {n: (world.live / n).read_bytes() for n in SLOT1} == torn


def test_an_edit_made_from_an_older_version_is_refused(world):
    """The edits name places in the version the app read: when the slot loads another one
    now (the game or another app saved), they would mean other things there."""
    session = _session(world)
    read = hashlib.sha256((world.live / "save.hg").read_bytes()).hexdigest()
    assert not session.plan_edit(_slot(world), UNITS, what="x", expected=read).blocked
    with pytest.raises(api.EditRefused, match="changed since it was read"):
        session.plan_edit(_slot(world), UNITS, what="x", expected="0" * 64)



def test_the_expected_version_is_hex_of_any_case_and_nothing_else(world):
    session = _session(world)
    read = hashlib.sha256((world.live / "save.hg").read_bytes()).hexdigest()
    assert not session.plan_edit(_slot(world), UNITS, what="x", expected=read.upper()).blocked
    for bad in ("", "xyz", read[:-1], read + "0"):
        with pytest.raises(ValueError):
            session.plan_edit(_slot(world), UNITS, what="x", expected=bad)


def test_dumps_named_refuses_a_name_that_is_not_text():
    for bad in (5, True, None, 1.5):
        with pytest.raises(api.PatchRefused):
            api.dumps_named({bad: 1})



def test_an_undo_names_the_change_it_undid(world):
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    done = session.write(plan, plan.confirmations)
    undo = session.plan_undo(done.op_id)
    back = session.write(undo, undo.confirmations)
    changes = {c.op_id: c for c in api.open_history(world.vault_root).changes_by(SC)}
    assert changes[back.op_id].undo_of == done.op_id and changes[done.op_id].undo_of is None


def _json_other(world):
    """The other account's slot 3 holding a save of real JSON, as the game writes it."""
    from .liveops_support import POLESTAR
    for member, play, ts in ((0, 4000, T0 + 300), (1, 3900, T0 + 200)):
        body = b'{"' + K("SaveName").encode() + b'":"Polestar","m":%d}' % member
        steam_save(world.other, 3, member, "Polestar", play, POLESTAR, ts, payload=body + b"\x00")


def _units_of(folder, slot):
    name = slotmap.data_filename(slotmap.slot_file_numbers(slot)[0])
    return savejson.decode_save((folder / name).read_bytes()).root.get(K("Units"))


def test_two_saves_are_changed_as_one_write_and_undone_together(world):
    """CL6: a move between saves takes from one and puts into the other in one operation."""
    _json_other(world)
    session = api.open_live(world.vault_root, live_dirs=[world.live, world.other])
    a, b = _slot(world), api.slot_of(world.other, 3)
    plan = session.plan_edits([(a, UNITS, None), (b, [api.AddMember((), "Units", b"7")], None)],
                              what="move units")
    assert not plan.blocked, plan.blocked
    assert len(plan.made) == 2
    result = session.write(plan, plan.confirmations)
    assert result.status == "done", result
    assert (_units_of(world.live, 1), _units_of(world.other, 3)) == (9, 7)
    changes = api.open_history(world.vault_root).changes_by(SC)       # one per slot
    assert {c.op_id for c in changes} == {result.op_id} and len(changes) == 2
    undo = session.plan_undo(result.op_id)
    assert not undo.blocked, undo.blocked
    assert session.write(undo, undo.confirmations).status == "done"
    assert (_units_of(world.live, 1), _units_of(world.other, 3)) == (None, None)


def test_a_two_save_edit_that_cannot_be_made_on_one_side_writes_neither(world):
    _json_other(world)
    session = api.open_live(world.vault_root, live_dirs=[world.live, world.other])
    before = tree_hash(world.live), tree_hash(world.other)
    with pytest.raises(api.PatchRefused):
        session.plan_edits([(_slot(world), UNITS, None),
                            (api.slot_of(world.other, 3), [api.Replace(("NoSuchField",), b"1")], None)],
                           what="x")
    with pytest.raises(api.EditRefused, match="named twice"):
        session.plan_edits([(_slot(world), UNITS, None), (_slot(world), UNITS, None)], what="x")
    assert (tree_hash(world.live), tree_hash(world.other)) == before


def test_crash_during_stops_a_write_where_named_and_the_next_session_finds_it(world):
    """core.testing.crash_during, for an app's crash tests: the write dies at the point
    named, unrecorded, and the next session finishes it (written and checked already)."""
    from nms_save_vault.core import testing
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="units")
    with testing.crash_during(session, "after_validated"):
        with pytest.raises(testing.SimulatedCrash, match="after_validated"):
            session.write(plan, plan.confirmations)
    assert api.open_history(world.vault_root).changes_by(SC) == []
    assert _session(world).interrupted() == []
    (done,) = api.open_history(world.vault_root).changes_by(SC)
    assert done.target.slot == 1


def test_a_failed_two_save_write_over_two_accounts_can_still_be_put_back(world, monkeypatch):
    """Review CL6-3: slot 1 of two accounts in one write. Validation fails, then so does the
    rollback. Recovery tells each account's files apart (they were keyed by slot number
    alone, so one account's own output read as a save the game wrote, and only "leave" was
    offered)."""
    from nms_save_vault.core.liveops import FaultInjector, steamwrite
    from .liveops_support import POLESTAR
    for member, play, ts in ((0, 4000, T0 + 300), (1, 3900, T0 + 200)):
        body = b'{"' + K("SaveName").encode() + b'":"Polestar","m":%d}' % member
        steam_save(world.other, 1, member, "Polestar", play, POLESTAR, ts, payload=body + b"\x00")
    before = tree_hash(world.live), tree_hash(world.other)
    session = api.open_live(world.vault_root, live_dirs=[world.live, world.other])
    plan = session.plan_edits([(_slot(world), UNITS, None),
                               (api.slot_of(world.other, 1), [api.AddMember((), "Units", b"7")], None)],
                              what="two accounts")
    hit = []

    def corrupt():
        hit.append(1)
        if len(hit) == 2:               # the second slot starts: the first is written; break it
            (world.live / "save.hg").write_bytes(b"broken")
    make = session._make

    def faulty():
        ctx = make()
        ctx.faults = FaultInjector(actions={"swapped:1": corrupt})
        return ctx
    monkeypatch.setattr(session, "_make", faulty)
    real = steamwrite.rollback
    monkeypatch.setattr(steamwrite, "rollback",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(5, "held open")))
    out = session.write(plan, plan.confirmations)
    assert out.status == "failed", out
    monkeypatch.setattr(steamwrite, "rollback", real)
    monkeypatch.setattr(session, "_make", make)
    again = api.open_live(world.vault_root, live_dirs=[world.live, world.other])
    (item,) = again.interrupted()
    assert "back" in item.choices, item
    assert again.resolve(item.op_id, "back").status == "rolled_back"
    assert (tree_hash(world.live), tree_hash(world.other)) == before


def test_each_save_of_a_two_save_write_can_say_what_was_done_to_it(world):
    """Review CL6-4: the source of a move is not "moved in from another save"."""
    _json_other(world)
    session = api.open_live(world.vault_root, live_dirs=[world.live, world.other])
    plan = session.plan_edits([(_slot(world), UNITS, None),
                               (api.slot_of(world.other, 3), [api.AddMember((), "Units", b"7")], None)],
                              what="move units", slot_whats=["move units out", "move units in"])
    labels = {m.vid: e.origin["what"] for e in api.open_vault(world.vault_root).entries()
              if isinstance(getattr(e, "origin", None), dict) and "edit_of" in e.origin
              for m in e.members}
    assert [labels[vid] for vid in plan.made] == ["move units out", "move units in"]   # per slot
    with pytest.raises(api.EditRefused, match="one text for each"):
        session.plan_edits([(_slot(world), UNITS, None)], what="x", slot_whats=["a", "b"])
    with pytest.raises(api.EditRefused, match="one text for each"):        # not split into letters
        session.plan_edits([(_slot(world), UNITS, None),
                            (api.slot_of(world.other, 3), UNITS, None)], what="x", slot_whats="ab")


def test_an_edit_never_asks_about_a_second_copy_of_its_playthrough(world):
    """Review CL6-5: an edit, or a move between two saves of one playthrough, makes no new
    copy of it; "a second copy makes two separate branches" is not asked."""
    for member, play, ts in ((0, 7000, T0 + 90), (1, 6900, T0 + 40)):
        body = b'{"' + K("SaveName").encode() + b'":"Main","m":%d}' % member
        steam_save(world.live, 2, member, "Main", play, MAIN, ts, payload=body + b"\x00")
    session = _session(world)
    one = session.plan_edit(_slot(world), UNITS, what="units")
    both = session.plan_edits([(_slot(world), UNITS, None),
                               (api.slot_of(world.live, 2), [api.AddMember((), "Units", b"7")], None)],
                              what="move units")
    for plan in (one, both):
        assert "TWIN_LIVE" not in plan.confirmations
        assert all(n.code != "TWIN_LIVE" for n in plan.warnings)
    assert session.write(both, both.confirmations).status == "done"


def test_a_two_save_write_gone_stale_names_both_saves(world):
    """Review CL6-5: the save that changed may be the second one; the text does not blame
    the first."""
    _json_other(world)
    session = api.open_live(world.vault_root, live_dirs=[world.live, world.other])
    plan = session.plan_edits([(_slot(world), UNITS, None),
                               (api.slot_of(world.other, 3), [api.AddMember((), "Units", b"7")], None)],
                              what="move units")
    from .liveops_support import POLESTAR
    body = b'{"' + K("SaveName").encode() + b'":"Polestar","m":9}'
    steam_save(world.other, 3, 0, "Polestar", 5000, POLESTAR, T0 + 900, payload=body + b"\x00")
    out = session.write(plan, plan.confirmations)
    assert out.status == "stale"
    assert "slot 1" in out.text and "slot 3" in out.text, out.text


def test_crash_during_says_so_when_the_write_never_reached_its_point(world):
    """Review F-7: a misspelt point would let the write go through for real."""
    from nms_save_vault.core import testing
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="units")
    with pytest.raises(AssertionError, match="never reached 'after_validatd'"):
        with testing.crash_during(session, "after_validatd"):
            session.write(plan, plan.confirmations)


def test_a_failed_write_s_files_are_kept_under_both_keys_while_its_slot_is_unique(tmp_path):
    """Review F-1: a release before the account was in the key reads "slot/name"; it is
    written too when no other slot of the op has that number (else it would be ambiguous)."""
    import importlib
    from types import SimpleNamespace

    from nms_save_vault.core.organizer.model import LiveTarget
    execute = importlib.import_module("nms_save_vault.core.liveops.execute")

    def wt(account, slot):
        return SimpleNamespace(steam=SimpleNamespace(pre={"save.hg": None}), folder=tmp_path,
                               rt=SimpleNamespace(target=LiveTarget("steam", account, slot)))
    one = execute.settled_files([wt("st_1", 1), wt("st_1", 3)])
    assert one["1/save.hg"] is None and one["3/save.hg"] is None
    assert one[execute.settled_key(LiveTarget("steam", "st_1", 1), "save.hg")] is None
    two = execute.settled_files([wt("st_1", 1), wt("st_2", 1)])
    assert "1/save.hg" not in two and len(two) == 2


def test_a_damaged_settled_record_is_never_taken_for_the_op_s_own_output():
    """Review F-2: a record whose "settled" is not a mapping (hand-made, damaged) neither
    raises nor matches."""
    from nms_save_vault.core.liveops import recovery
    from nms_save_vault.core.organizer.model import LiveTarget
    target = LiveTarget("steam", "st_1", 1)
    for bad in (["1/save.hg"], "1/save.hg", 5):
        assert recovery._settled_as(bad, target, "save.hg", None) is False
    assert recovery._settled_as({"1/save.hg": None}, target, "save.hg", None) is True   # old key
    assert recovery._settled_as({}, target, "save.hg", None) is False


def test_the_undo_of_a_two_save_write_writes_the_saves_in_reverse_order(world):
    """Review F (Savecraft): a move writes the save it lands in first, so its undo writes the
    save it came from first. An undo that dies between the two then leaves the asset in
    both saves, never in neither."""
    _json_other(world)
    session = api.open_live(world.vault_root, live_dirs=[world.live, world.other])
    a, b = _slot(world), api.slot_of(world.other, 3)
    plan = session.plan_edits([(b, [api.AddMember((), "Units", b"7")], None), (a, UNITS, None)],
                              what="move units")
    done = session.write(plan, plan.confirmations)
    undo = session.plan_undo(done.op_id)
    order = [(t.target.account_key, t.target.slot) for t in plan._plan.targets]
    assert [(t.target.account_key, t.target.slot) for t in undo._plan.targets] == order[::-1]
    assert session.write(undo, undo.confirmations).status == "done"
    assert (_units_of(world.live, 1), _units_of(world.other, 3)) == (None, None)


# --- milestone review 2 (1547391) ---------------------------------------------------------


def _progress(folder, slot, name, play, ident, units):
    import time
    body = b'{"' + K("SaveName").encode() + b'":"%s","m":0,"%s":%d}' % (
        name.encode(), K("Units").encode(), units)
    steam_save(folder, slot, 0, name, play, ident, int(time.time()) + 600, payload=body + b"\x00")


def test_no_value_a_save_cannot_hold_is_written():
    """M2-2, M2-10: NaN and infinity are not JSON, and a lone surrogate is not text; each is
    refused as a patch, never written."""
    for value in (float("nan"), 1e400, float("-inf"), "\ud800"):
        with pytest.raises(api.PatchRefused):
            api.dumps(value)
    with pytest.raises(api.PatchRefused):
        api.dumps_named({"Name": "\ud800"})


def test_a_raw_value_that_is_not_json_is_refused(world):
    session = _session(world)
    for raw in (b"-Infinity", b"NaN", b"Infinity"):
        with pytest.raises(api.PatchRefused):
            session.plan_edit(_slot(world), [api.AddMember((), "Units", raw)], what="units")


@pytest.mark.parametrize("what", ["other_playthrough", "played_more"])
def test_a_slot_that_changes_while_the_edit_is_made_is_never_written_over(world, monkeypatch, what):
    """M2-1: the save the edit was made from must still be the one the slot loads when the
    plan is made; else the edit of the old one would replace the new one."""
    import hashlib
    import importlib
    from .liveops_support import POLESTAR
    editmod = importlib.import_module("nms_save_vault.core.liveops.edit")
    real = editmod.rebuild

    def slow(*args, **kwargs):
        out = real(*args, **kwargs)
        if what == "other_playthrough":         # NMS Save Vault put another save there meanwhile
            _progress(world.live, 1, "Polestar", 4000, POLESTAR, 1)
        else:
            _progress(world.live, 1, "Main", 7200 + 3600, MAIN, 5)
        return out
    monkeypatch.setattr(editmod, "rebuild", slow)
    session = _session(world)
    data = (world.live / "save.hg").read_bytes()
    with pytest.raises(api.EditRefused, match="changed while the edit was made"):
        session.plan_edit(_slot(world), UNITS, what="units",
                          expected=hashlib.sha256(data).hexdigest())


def test_a_write_s_crash_points_are_passed_in_the_order_crash_during_gives():
    """M2-8: the order the docstring of core.testing.crash_during lists."""
    from nms_save_vault.core import testing
    doc = testing.crash_during.__doc__
    order = ["after_preparing", "in_preserve", "after_snapshot", "after_writing", "staged:",
             "swapped:", "after_validated", "after_oplog", "after_record"]
    places = [doc.index(f'"{p}') for p in order]
    assert places == sorted(places)


def test_the_crash_points_come_in_that_order(world):
    import importlib
    faults = importlib.import_module("nms_save_vault.core.liveops.faults")
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="units")
    make, seen = session._make, []

    def recording():
        ctx = make()
        ctx.faults = faults.FaultInjector()
        seen.append(ctx.faults)
        return ctx
    session._make = recording
    try:
        assert session.write(plan, plan.confirmations).status == "done"
    finally:
        session._make = make
    points = seen[0].seen
    order = ["after_preparing", "in_preserve", "after_snapshot", "after_writing", "staged:1",
             "swapped:1", "after_validated", "after_oplog", "after_record"]
    assert [points.index(p) for p in order] == sorted(points.index(p) for p in order)


def test_an_undo_over_two_played_saves_tells_of_both(world):
    """M2-5: one notice per save played since, not one for the first only."""
    from .liveops_support import POLESTAR
    _json_other(world)
    session = api.open_live(world.vault_root, live_dirs=[world.live, world.other])
    a, b = _slot(world), api.slot_of(world.other, 3)
    plan = session.plan_edits([(b, [api.AddMember((), "Units", b"7")], None), (a, UNITS, None)],
                              what="move units")
    done = session.write(plan, plan.confirmations)
    _progress(world.live, 1, "Main", 7200 + 3600, MAIN, 111)
    _progress(world.other, 3, "Polestar", 4000 + 18000, POLESTAR, 222)
    session.refresh()
    undo = session.plan_undo(done.op_id)
    played = [n.text for n in undo.warnings if n.code == "PLAYED_SINCE"]
    assert len(played) == 2 and any("slot 1" in t for t in played) and any("slot 3" in t for t in played)


def test_a_vault_that_cannot_record_the_write_is_a_refusal_not_an_error(world, monkeypatch):
    """M2-6: an OSError while the write is journaled (the vault drive full or gone) is a
    WriteResult, and nothing live changed."""
    import importlib
    opjournal = importlib.import_module("nms_save_vault.core.liveops.opjournal")
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="units")
    before = tree_hash(world.live)
    real = opjournal._write_line
    for event in (b'"event":"preparing"', b'"event":"writing"'):
        def full(path, data, event=event):
            if event in data:
                raise OSError(28, "No space left on device")
            return real(path, data)
        monkeypatch.setattr(opjournal, "_write_line", full)
        out = session.write(plan, plan.confirmations)
        assert out.status == "refused" and "could not record" in out.text, out
        assert tree_hash(world.live) == before
    monkeypatch.setattr(opjournal, "_write_line", real)


def test_a_busy_vault_while_settling_a_stopped_write_is_a_refusal(world, monkeypatch):
    """M2-7: the organizer's busy error is not one an app can catch through the API."""
    import importlib
    from nms_save_vault.core import testing
    opjournal = importlib.import_module("nms_save_vault.core.liveops.opjournal")
    orgstore = importlib.import_module("nms_save_vault.core.organizer.store")
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="units")
    with testing.crash_during(session, "swapped:2"):
        with pytest.raises(testing.SimulatedCrash):
            session.write(plan, plan.confirmations)
    again = _session(world)
    (item,) = again.interrupted()
    real = opjournal.append

    def busy(ctx, op_id, event, payload=None):
        if event == "recovered":
            raise orgstore.StoreBusy()
        return real(ctx, op_id, event, payload)
    monkeypatch.setattr(opjournal, "append", busy)
    out = again.resolve(item.op_id, item.recommended)
    assert out.status == "refused" and out.code == "BUSY", out
    monkeypatch.setattr(opjournal, "append", real)
    assert _session(world).resolve(item.op_id, item.recommended).status in ("done", "rolled_back")


def test_a_written_op_that_cannot_be_finished_can_be_put_back(world, monkeypatch):
    """M2-4: all files written, then finishing fails: "back" is offered, as its text says."""
    import importlib
    from nms_save_vault.core import testing
    recovery = importlib.import_module("nms_save_vault.core.liveops.recovery")
    errors = importlib.import_module("nms_save_vault.core.liveops.errors")
    before = tree_hash(world.live)
    session = _session(world)
    plan = session.plan_edit(_slot(world), UNITS, what="units")
    with testing.crash_during(session, "swapped:4"):
        with pytest.raises(testing.SimulatedCrash):
            session.write(plan, plan.confirmations)

    def unreadable(ctx, wt):
        raise errors.SourceUnreadable("version cannot be read")
    monkeypatch.setattr(recovery, "_rederive", unreadable)
    again = _session(world)
    (item,) = again.interrupted()
    assert item.choices == ("forward", "back") and item.recommended == "forward"
    assert again.resolve(item.op_id, "forward").status == "refused"
    assert again.resolve(item.op_id, "back").status == "rolled_back"
    assert tree_hash(world.live) == before
