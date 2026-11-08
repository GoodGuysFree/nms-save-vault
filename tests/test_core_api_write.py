"""core.api's write side (Savecraft): an edit of a live Steam save and its undo through a
LiveSession, which honours NMS Save Vault's settings, the game-running gate and the vault's
lock, and never offers a stale edit again."""
from __future__ import annotations

import hashlib
import json

import pytest

from nms_save_vault.core import api, appinfo, platform, safety, savejson, state
from nms_save_vault.core.hashname import key as K

from .conftest import steam_save
from .liveops_support import MAIN, T0, host_in, steam_world, tree_hash

SLOT1 = ("save.hg", "mf_save.hg", "save2.hg", "mf_save2.hg")
SC = "nms-savecraft"


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
    return w


def _session(w):
    return api.open_live(w.vault_root, live_dirs=[w.live])


def _units(w):
    return savejson.decode_save((w.live / "save.hg").read_bytes()).root.get(K("Units"))


def test_an_edit_and_its_undo_go_through_the_session(world):
    before = {n: (world.live / n).read_bytes() for n in SLOT1}
    session = _session(world)
    slot = api.slot_of(world.live, 1)
    plan = session.plan_edit(slot, [api.AddMember((), "Units", api.dumps(4242))],
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
    assert {n: (world.live / n).read_bytes() for n in ("save.hg", "save2.hg")} == {
        n: before[n] for n in ("save.hg", "save2.hg")}


def test_an_app_that_has_not_named_itself_cannot_open_a_session(world):
    appinfo.set_app(appinfo.VAULT, "1.0")
    with pytest.raises(ValueError, match="set_app"):
        _session(world)


def test_no_vault_no_session(world, tmp_path):
    with pytest.raises(api.StoreUnavailable):
        api.open_live(tmp_path / "nowhere", live_dirs=[world.live])


def test_a_folder_switched_off_in_nms_save_vault_is_never_written(world, tmp_path):
    own = tmp_path / "own" / "state.json"
    own.parent.mkdir()
    for off in ({"enabled": False}, {"writable": False}, {"role": "backup"}):
        source = {"id": "steam-x", "platform": "steam", "path": str(world.live), **off}
        own.write_text(json.dumps({"vault": None, "sources": [source]}), encoding="utf-8")
        live_before = tree_hash(world.live)
        with pytest.raises(api.TargetError) as refused:
            _session(world).plan_edit(api.slot_of(world.live, 1),
                                      [api.AddMember((), "Units", b"1")], what="x")
        assert refused.value.code == "NOT_WRITABLE"
        assert tree_hash(world.live) == live_before


def test_unreadable_vault_settings_stop_the_session(world, tmp_path):
    own = tmp_path / "own" / "state.json"
    own.parent.mkdir()
    own.write_text("{damaged", encoding="utf-8")
    with pytest.raises(ValueError, match="settings"):
        _session(world)
    assert own.read_text(encoding="utf-8") == "{damaged"            # never moved aside


def test_a_slot_the_game_saved_after_the_plan_is_not_written(world):
    session = _session(world)
    plan = session.plan_edit(api.slot_of(world.live, 1), [api.AddMember((), "Units", b"9")],
                             what="x")
    body = b'{"' + K("SaveName").encode() + b'":"Main","m":2}'
    steam_save(world.live, 1, 0, "Main", 7300, MAIN, T0 + 400, payload=body + b"\x00")
    saved = hashlib.sha256((world.live / "save.hg").read_bytes()).hexdigest()
    result = session.write(plan, plan.confirmations)
    assert result.status == "stale" and not hasattr(result, "replan")
    assert hashlib.sha256((world.live / "save.hg").read_bytes()).hexdigest() == saved


def test_nothing_is_written_while_the_game_runs(world, monkeypatch):
    session = _session(world)
    plan = session.plan_edit(api.slot_of(world.live, 1), [api.AddMember((), "Units", b"9")],
                             what="x")
    monkeypatch.setattr(safety, "is_game_running", lambda *a, **k: True)
    session.refresh()
    live_before = tree_hash(world.live)
    result = session.write(plan, plan.confirmations)
    assert (result.status, result.code) == ("refused", "GAME_RUNNING")
    assert tree_hash(world.live) == live_before


def test_an_edit_that_cannot_be_made_is_refused_before_anything_is_kept(world):
    session = _session(world)
    with pytest.raises(api.PatchRefused):
        session.plan_edit(api.slot_of(world.live, 1),
                          [api.Replace(("Nope",), b"1")], what="x")
    with pytest.raises(api.EditRefused, match="change nothing"):
        session.plan_edit(api.slot_of(world.live, 1), [], what="x")
