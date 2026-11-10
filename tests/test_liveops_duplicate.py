"""liveops.duplicate: a save copied into an empty slot as a new playthrough (NMS-Unpack
response to Savecraft's query 6, part A, exe 185034): a new non-zero random
``SaveUniversalId``, the same in the save and in both metas of the slot (0x15C; on load the
meta's wins), ``MultiplayerLobbyID`` 0 in every context, nothing else changed. Placed as any
version: kept in the vault first, journalled, undone back to an empty slot."""
from __future__ import annotations

import struct

import pytest

from nms_save_vault.core import api, appinfo, formats, meta, savejson, slotmap
from nms_save_vault.core.hashname import key as K
from nms_save_vault.core.liveops import duplicate as dup
from nms_save_vault.core.liveops import plan_undo
from nms_save_vault.core.liveops.duplicate import plan_duplicate
from nms_save_vault.core.liveops.edit import EditRefused
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.organizer.model import LiveRef

from .conftest import steam_save
from .liveops_support import MAIN, SECOND, STEAM_ACCOUNT, T0, steam_world, tree_hash

LIVE = LiveRef("steam", STEAM_ACCOUNT)
SLOT5 = ("save9.hg", "mf_save9.hg", "save10.hg", "mf_save10.hg")


@pytest.fixture(autouse=True)
def savecraft():
    previous = appinfo.set_app("nms-savecraft", "0.1.0")
    try:
        yield
    finally:
        appinfo.set_app(previous.name, previous.version)


def body(lobby: bytes = b'"0x1A2B3C4D5E6F7081"', expedition: bool = True) -> bytes:
    psd = b'{"' + K("MultiplayerLobbyID").encode() + b'":' + lobby + b',"u":1}'
    ctx = b'{"' + K("PlayerStateData").encode() + b'":' + psd + b"}"
    common = (b'{"' + K("SaveName").encode() + b'":"Main","' + K("SaveUniversalId").encode()
              + b'":"0x%X"}' % MAIN)
    out = (b'{"' + K("CommonStateData").encode() + b'":' + common + b',"'
           + K("BaseContext").encode() + b'":' + ctx)
    if expedition:
        out += b',"' + K("ExpeditionContext").encode() + b'":' + ctx.replace(lobby, b"0")
    return out + b"}"


def world(tmp_path, payload=None):
    w = steam_world(tmp_path)
    for member, play, ts in ((0, 7200, T0 + 100), (1, 7100, T0 + 50)):
        steam_save(w.live, 1, member, "Main", play, MAIN, ts,
                   payload=(payload or body()) + b"\x00")
    w.refresh()
    return w


def slot_ids(folder, slot):
    out = []
    for member in (0, 1):
        fno = slotmap.slot_file_numbers(slot)[member]
        blob = (folder / slotmap.meta_filename(fno)).read_bytes()
        plain = meta.decrypt(blob, slotmap.storage_ordinal(fno))
        out.append(struct.unpack_from("<Q", plain, formats.OFF_SLOT_IDENTIFIER)[0])
    return out


def test_a_save_goes_into_an_empty_slot_as_a_new_playthrough(tmp_path):
    w = world(tmp_path)
    before = tree_hash(w.live)
    source_before = {n: (w.live / n).read_bytes() for n in ("save.hg", "save2.hg")}
    plan = plan_duplicate(w.ctx, LIVE.target(1), LIVE.target(5), what="duplicate 'Main'")
    # neither "another slot holds it" nor "emptied outside the app": the slot was empty
    assert not {"TWIN_LIVE", "EMPTY_SLOT"} & {x.code for x in plan.warnings}
    outcome = execute(w.ctx, plan, plan.confirmations)
    assert outcome.status == "done", outcome
    assert tree_hash(w.live) != before
    assert {n: (w.live / n).read_bytes() for n in source_before} == source_before

    root = savejson.decode_save((w.live / "save9.hg").read_bytes()).root
    new = root[K("CommonStateData")][K("SaveUniversalId")]
    assert new.startswith("0x") and new == new.upper().replace("0X", "0x")
    value = int(new, 16)
    assert value not in (0, MAIN, SECOND) and new == f"0x{value:X}"
    assert slot_ids(w.live, 5) == [value, value]                    # both metas: on load, they win
    for ctx in ("BaseContext", "ExpeditionContext"):
        assert root[K(ctx)][K("PlayerStateData")][K("MultiplayerLobbyID")] == 0
    original = savejson.decode_save(source_before["save.hg"]).root
    root[K("CommonStateData")][K("SaveUniversalId")] = f"0x{MAIN:X}"
    original[K("BaseContext")][K("PlayerStateData")][K("MultiplayerLobbyID")] = 0
    assert root == original                                         # nothing else changed

    staged = [e for e in w.ctx.versions.entries() if e.reason == "edit"]
    assert [e.label for e in staged] == ["edited by nms-savecraft: duplicate 'Main'"]
    assert plan.made and plan.made[0] in {m.vid for m in staged[0].members}


def test_its_undo_is_refused_as_every_undo_that_would_empty_a_slot(tmp_path):
    """The app never empties a slot (it says how to, in the game); the copy stays."""
    w = world(tmp_path)
    plan = plan_duplicate(w.ctx, LIVE.target(1), LIVE.target(5), what="duplicate")
    outcome = execute(w.ctx, plan, plan.confirmations)
    undo = plan_undo(w.ctx, outcome.op_id)
    after = tree_hash(w.live)
    result = execute(w.ctx, undo, undo.confirmations)
    assert (result.status, result.code) == ("refused", "UNDO_WOULD_EMPTY")
    assert tree_hash(w.live) == after and all((w.live / n).exists() for n in SLOT5)


def test_a_lobby_already_0_and_a_save_with_one_context_change_only_the_id(tmp_path):
    w = world(tmp_path, body(lobby=b"0", expedition=False))
    plan = plan_duplicate(w.ctx, LIVE.target(1), LIVE.target(5), what="duplicate")
    assert execute(w.ctx, plan, plan.confirmations).status == "done"
    root = savejson.decode_save((w.live / "save9.hg").read_bytes()).root
    assert K("ExpeditionContext") not in root
    assert root[K("BaseContext")][K("PlayerStateData")][K("MultiplayerLobbyID")] == 0


def test_the_new_id_is_never_0_nor_one_a_slot_of_that_account_uses(tmp_path, monkeypatch):
    w = world(tmp_path)
    rolls = iter([0, MAIN, SECOND, 0x42])
    monkeypatch.setattr(dup.secrets, "randbits", lambda bits: next(rolls))
    plan = plan_duplicate(w.ctx, LIVE.target(1), LIVE.target(5), what="duplicate")
    assert execute(w.ctx, plan, plan.confirmations).status == "done"
    assert slot_ids(w.live, 5) == [0x42, 0x42]


@pytest.mark.parametrize("source, target, match", [
    (1, 2, "empty"),                 # slot 2 holds a save
    (1, 1, "empty"),
    (9, 5, "no save"),               # nothing to copy
])
def test_a_slot_that_is_not_empty_or_a_source_with_no_save_is_refused(tmp_path, source, target,
                                                                        match):
    w = world(tmp_path)
    live_before, entries = tree_hash(w.live), len(w.ctx.versions.entries())
    with pytest.raises(EditRefused, match=match):
        plan_duplicate(w.ctx, LIVE.target(source), LIVE.target(target), what="duplicate")
    assert tree_hash(w.live) == live_before and len(w.ctx.versions.entries()) == entries


def test_a_save_with_no_playthrough_id_is_refused(tmp_path):
    w = world(tmp_path, body().replace(K("SaveUniversalId").encode(), b"zzz"))
    with pytest.raises(EditRefused, match="playthrough id"):
        plan_duplicate(w.ctx, LIVE.target(1), LIVE.target(5), what="duplicate")


def test_an_xbox_slot_is_refused(tmp_path):
    w = world(tmp_path)
    with pytest.raises(EditRefused, match="Steam"):
        plan_duplicate(w.ctx, LiveRef("xbox", "x").target(1), LIVE.target(5), what="x")


def test_the_api_plans_it_for_an_app(tmp_path):
    w = world(tmp_path)
    session = api.LiveSession(lambda: w.ctx)
    plan = session.plan_duplicate(api.slot_of(w.live, 1), api.slot_of(w.live, 5),
                                  what="duplicate")
    assert plan.made and session.write(plan, plan.confirmations).status == "done"


def test_r29_1_a_save_put_into_the_target_while_the_copy_was_made_is_never_replaced(
        tmp_path, monkeypatch):
    w = world(tmp_path)
    real = dup._stage

    def stage_then_a_sync_fills_slot_5(*a, **k):
        out = real(*a, **k)
        steam_save(w.live, 5, 0, "Synced", 10, 0x77, T0 + 500)
        return out
    monkeypatch.setattr(dup, "_stage", stage_then_a_sync_fills_slot_5)
    with pytest.raises(EditRefused, match="empty"):
        plan_duplicate(w.ctx, LIVE.target(1), LIVE.target(5), what="duplicate")


def test_r29_3_a_slot_holding_part_of_a_save_is_not_empty(tmp_path):
    w = world(tmp_path)
    (w.live / "save9.hg").write_bytes(b"left over")
    w.refresh()
    with pytest.raises(EditRefused, match="empty"):
        plan_duplicate(w.ctx, LIVE.target(1), LIVE.target(5), what="duplicate")
    assert (w.live / "save9.hg").read_bytes() == b"left over"
