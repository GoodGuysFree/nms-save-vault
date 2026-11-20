"""Review R34 (2026-11-20), the core's part. Made-up saves and account files in temp folders
only.

R34-2: an unlock undone already was planned for undo again, so after the same id came back
by a later unlock, undoing the first one took out the later one's id.
R34-3: an app's account writes are in the vault's op journal but not in the history an app
reads (``changes_by`` lists slot ops only), so an app whose own record of one was lost cannot
tell it went through.
R34-4: a manifest this store wrote itself and that was replaced on disk since kept being
served by that store (another store saw it damaged)."""
from __future__ import annotations

import os

from nms_save_vault.core import api
from nms_save_vault.core import store as vstore

from .store_helpers import STEAM_ACCOUNT, new_vault, open_vault_store, steam_live
from .test_core_api_account import give_account, unlocked
from .test_core_api_write import _session, savecraft, world  # noqa: F401 - fixtures


def test_an_unlock_undone_already_is_not_undone_again(world):
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _session(world)
    a = session.write_account(session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]},
                                                  what="unlock A"))
    assert a.status == "done", a.text
    undo = session.plan_unlock_undo(a.op_id)
    assert session.write_account(undo, undo.confirmations).status == "done"
    b = session.write_account(session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]},
                                                  what="unlock B"))
    assert b.status == "done", b.text
    again = session.plan_unlock_undo(a.op_id)           # A was undone already
    assert [x.code for x in again.blocked] == ["UNLOCK_UNDONE"] and again.removes == ()
    assert "undone already" in again.blocked[0].text
    assert session.write_account(again, again.confirmations).status != "done"
    assert unlocked(world.live, "UnlockedSeasonRewards") == ["^S1", "^S2"]     # B's stays
    later = session.plan_unlock_undo(b.op_id)           # B itself still can be
    assert not later.blocked and later.removes == (("UnlockedSeasonRewards", "^S2"),)


def test_an_app_reads_its_account_writes_and_their_undos_in_the_history(world):
    from nms_save_vault.core import appinfo
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _session(world)
    a = session.write_account(session.plan_unlock(
        world.live, {"UnlockedSeasonRewards": ["^S2"], "UnlockedSpecials": ["^X1"]},
        what="unlock 2 rewards"))
    assert a.status == "done", a.text
    undo = session.plan_unlock_undo(a.op_id)
    u = session.write_account(undo, undo.confirmations)
    assert u.status == "done", u.text
    history = api.open_history(world.vault_root)
    assert history.changes_by(appinfo.current().name) == []          # no slot op
    made = history.account_changes_by(appinfo.current().name)
    assert [c.op_id for c in made] == [a.op_id, u.op_id]
    first, second = made
    assert (first.platform, first.account_key) == ("steam", world.live.name)
    assert first.adds == (("UnlockedSeasonRewards", "^S2"), ("UnlockedSpecials", "^X1"))
    assert first.removes == () and first.undo_of is None and first.note == "unlock 2 rewards"
    assert second.undo_of == a.op_id and second.adds == ()
    assert set(second.removes) == set(first.adds)
    assert first.at > 0 and second.at >= first.at
    assert history.account_changes_by("nms-save-vault") == []        # another app's: none


def test_an_account_write_that_did_not_go_through_is_not_in_the_history(world):
    from nms_save_vault.core import appinfo
    give_account(world.live, UnlockedSeasonRewards=["S1"])
    session = _session(world)
    plan = session.plan_unlock(world.live, {"UnlockedSeasonRewards": ["^S2"]}, what="unlock")
    give_account(world.live, UnlockedSeasonRewards=["S1", "S5"])     # changed since: stale
    assert session.write_account(plan).status == "stale"
    assert api.open_history(world.vault_root).account_changes_by(appinfo.current().name) == []


def test_a_manifest_this_store_wrote_and_that_was_replaced_on_disk_is_read_again(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault)
    made = [store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT,
                                kind="snapshot", label=f"n{i}", scope=vstore.CaptureScope())
            for i in range(3)]
    last = made[-1]                                     # not the first: each one counts
    path = vault / "store" / "entries" / f"{last.id}.json"
    tmp = path.with_suffix(".tmpx")
    tmp.write_bytes(path.read_bytes()[:-1])             # damaged, put in place by a rename
    os.replace(tmp, path)
    other = open_vault_store(vault)
    assert not other.has_entry(last.id)
    assert not store.has_entry(last.id)
    assert f"{last.id}.json" in dict(store.corrupt_manifests)
    assert all(store.has_entry(e.id) for e in made[:-1])
