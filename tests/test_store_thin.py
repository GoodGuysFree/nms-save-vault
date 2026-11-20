"""Thinning the vault (docs/thinning.md): the schedule, whole entries, every keep rule, the plan
that writes nothing, the run (objects, re-plan under the lock, interruptions, the record), pins
and the reminder. Made-up vaults only."""
from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nms_save_vault.core import catalog, operations
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import CatalogEntry, Vault
from nms_save_vault.core.liveops.execute import SNAPSHOT_LABELS
from nms_save_vault.core.organizer.notes import Notes
from nms_save_vault.core.organizer.store import OrganizerStore
from nms_save_vault.core.store import rollback, thinning, versionstore

from .conftest import steam_save
from .store_helpers import BIG, STEAM_ACCOUNT, fingerprint, new_vault, open_vault_store

NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)      # a Friday
P1, P2, P3 = 0x0110000100000001, 0x0110000100000002, 0x0110000100000003
SNAP = vstore.CaptureScope()
MACHINE = "m-000000000001"


def at(*when) -> int:
    return int(datetime(*when, tzinfo=timezone.utc).timestamp())


def pid(ident: int) -> str:
    return f"{ident:016X}"


class World:
    """A made-up vault, its entries made one by one."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = new_vault(tmp_path)
        self.live = tmp_path / "live" / STEAM_ACCOUNT
        self.n = 0
        self.ops = 0

    def add(self, *saves, made=None, kind="snapshot", label="auto: place", op_id=None,
            account=None, live=False, name="imported-folder", origin=None, files=None):
        """An entry holding ``saves``, each (slot, ident, ts, tag) as member A. Saves alike in
        ident and tag hold the same data (one version), whatever their time. ``account``:
        (bytes, mtime) of an accountdata.hg to keep with them; ``files``: other files of the
        folder by name (bytes, or None to delete one the saves made)."""
        self.n += 1
        folder = self.live if live else self.tmp / f"src{self.n}" / STEAM_ACCOUNT
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        for slot, ident, ts, tag in saves:
            steam_save(folder, slot, 0, "Main", 100, ident, ts, extra=BIG + tag)
        for file_name, data in (files or {}).items():
            if data is None:
                (folder / file_name).unlink()
            else:
                (folder / file_name).write_bytes(data)
        if account is not None:
            (folder / "accountdata.hg").write_bytes(account[0])
            (folder / "mf_accountdata.hg").write_bytes(b"\x01" * 432)
            os.utime(folder / "accountdata.hg", (account[1], account[1]))
        if made is None:
            made = max([ts for _s, _i, ts, _t in saves] + [account[1] if account else 0]) + 60
        store = open_vault_store(self.root,
                                 clock=lambda: datetime.fromtimestamp(made, timezone.utc))
        if live:
            return store.add_from_live(folder, platform="steam", account_key=STEAM_ACCOUNT,
                                       kind=kind, label=label, scope=SNAP, op_id=op_id)
        return store.add_from_folder(folder, kind=kind, label=label, op_id=op_id,
                                     source={"type": "folder", "name": name}, origin=origin)

    def plan(self, now=NOW):
        return thinning.plan_thin(self.root, now=now)

    def notes(self) -> Notes:
        return Notes(OrganizerStore(self.root, create=True))

    def op(self, kind, status, targets=(), *, done=None, note="", op_id=None):
        """An operation as ops.jsonl records it: preparing, writing (with ``targets``), then
        ``status`` (none for an op still open)."""
        self.ops += 1
        op_id = op_id or f"op-20261119T1000{self.ops:02d}Z-ab{self.ops:02d}"
        events = [("preparing", {"kind": kind, "app": "nms-save-vault test", "targets": [],
                                 "note": note})]
        if targets:
            events.append(("writing", {"targets": list(targets)}))
        if status not in ("preparing", "writing"):
            events.append((status, done or {}))
        path = self.root / "organizer" / "ops.jsonl"
        path.parent.mkdir(exist_ok=True)
        with open(path, "a", encoding="ascii") as f:
            for seq, (event, payload) in enumerate(events, 1):
                f.write(json.dumps({"j": 1, "op_id": op_id, "seq": seq, "event": event,
                                    "utc": "2026-11-19T10:00:00.000Z", "machine": MACHINE,
                                    **payload}) + "\n")
        return op_id


def target(*, snapshot=None, preserved=(), evicted=None):
    """One target of an op's ``writing`` event: the snapshot, the preserve entries (entry id,
    vid) and the evicted A version."""
    return {"target": {"platform": "steam", "account_key": STEAM_ACCOUNT, "slot": 1},
            "snapshot_id": snapshot,
            "preserved": [{"position": "A", "vid": vid, "location": {
                "kind": "store", "entry": entry, "slot": 1, "member": "A", "vid": vid,
                "plat": "steam", "durable": True}} for entry, vid in preserved],
            "pre": {"A": {"vid": evicted, "pid": pid(P1), "name": "Main", "play_time": 1,
                          "meta_ts": 1} if evicted else None, "B": None}}


def ids(items) -> set[str]:
    return {item.id for item in items}


def reasons(plan) -> dict[str, tuple[str, ...]]:
    return {k.id: k.reasons for k in plan.kept}


# --- the schedule -----------------------------------------------------------------------------


def test_the_schedule_keeps_the_last_7_days_then_one_a_day_a_week_a_month(tmp_path):
    w = World(tmp_path)
    w.add((1, P1, at(2026, 11, 13, 13), "a"))            # 6.96 days: every version
    w.add((1, P1, at(2026, 11, 13, 14), "b"))
    w.add((1, P1, at(2026, 11, 13, 11), "c"))            # 7.04 days: Nov 13's one
    w.add((1, P1, at(2026, 11, 12, 11), "d"))            # Nov 12's newest
    day_old = w.add((1, P1, at(2026, 11, 12, 9), "e"))
    w.add((1, P1, at(2026, 10, 22, 12), "f"))            # 29 days: still one per day
    w.add((1, P1, at(2026, 10, 21, 11), "g"))            # 30.04 days: week 43's newest
    week_old = w.add((1, P1, at(2026, 10, 19, 12), "h"))
    w.add((1, P1, at(2026, 10, 10, 12), "i"))            # week 41
    w41_old = w.add((1, P1, at(2026, 10, 8, 12), "j"))
    w.add((1, P1, at(2026, 5, 25, 12), "k"))             # 179 days: still one per week
    w.add((1, P1, at(2026, 5, 20, 12), "l"))             # 184 days: May's one
    w.add((1, P1, at(2026, 3, 20, 12), "m"))             # March
    march_old = w.add((1, P1, at(2026, 3, 3, 12), "n"))
    plan = w.plan()
    assert ids(plan.candidates) == {day_old.id, week_old.id, w41_old.id, march_old.id}
    why = {c.id: c.reason for c in plan.candidates}
    assert why[day_old.id] == "a newer save from that day is kept"
    assert why[week_old.id] == why[w41_old.id] == "a newer save from that week is kept"
    assert why[march_old.id] == "a newer save from that month is kept"
    assert plan.kept == () and plan.blocked == () and plan.entries == 14
    assert [c.saves for c in plan.candidates] == [(thinning.ThinSave(1, "Main"),)] * 4


def test_the_newest_save_of_each_playthrough_is_kept_however_old(tmp_path):
    w = World(tmp_path)
    w.add((2, P2, at(2025, 6, 1, 12), "a"))              # P2's newest, 17 months old
    older = w.add((2, P2, at(2025, 6, 1, 10), "b"))
    w.add((3, 0, at(2025, 2, 1, 12), "c"))               # no playthrough id: its slot's newest
    w.add((4, 0, at(2025, 1, 1, 12), "d"))
    assert ids(w.plan().candidates) == {older.id}


def test_a_noted_or_pinned_save_is_the_one_its_period_keeps(tmp_path):
    w = World(tmp_path)
    w.add((1, P1, at(2026, 11, 19, 12), "now"))
    noted = w.add((1, P1, at(2026, 10, 8, 12), "noted"))         # week 41, the older one
    newer = w.add((1, P1, at(2026, 10, 10, 12), "newer"))
    pinned = w.add((1, P1, at(2026, 3, 3, 12), "pinned"))        # March, the older one
    later = w.add((1, P1, at(2026, 3, 20, 12), "later"))
    notes = w.notes()
    for entry, kw in ((noted, {"comment": "before the Atlas"}), (pinned, {"locked": True})):
        vid = entry.members[0].vid
        notes.set_version_note(vid, pair=(vid, None), pid=pid(P1), **kw)
    plan = w.plan()
    assert ids(plan.candidates) == {newer.id, later.id}
    assert reasons(plan) == {noted.id: (thinning.VERSION_NOTED,),
                             pinned.id: (thinning.VERSION_PINNED,)}


def test_every_noted_save_is_kept_even_when_its_period_has_more_than_one(tmp_path):
    w = World(tmp_path)
    w.add((1, P1, at(2026, 11, 19, 12), "now"))
    first = w.add((1, P1, at(2025, 3, 2, 12), "first"))
    second = w.add((1, P1, at(2025, 3, 9, 12), "second"))
    plain = w.add((1, P1, at(2025, 3, 30, 12), "plain"))         # the month's newest
    notes = w.notes()
    for entry, label in ((first, "the first base"), (second, "")):
        vid = entry.members[0].vid
        notes.set_version_note(vid, pair=(vid, None), pid=pid(P1), label=label,
                               comment="" if label else "kept on purpose")
    plan = w.plan()
    assert ids(plan.candidates) == {plain.id}
    assert set(reasons(plan)) == {first.id, second.id}


def test_account_data_is_thinned_on_the_same_schedule(tmp_path):
    w = World(tmp_path)
    save = (1, P1, at(2026, 11, 19, 12), "same")
    old = w.add(save, account=(b"x" * 9000, at(2026, 10, 8, 12)), made=at(2026, 10, 8, 13))
    w.add(save, account=(b"z" * 9000, at(2026, 10, 10, 12)), made=at(2026, 10, 10, 13))
    w.add(save, account=(b"y" * 9000, at(2026, 11, 19, 12)), made=at(2026, 11, 19, 13))
    plan = w.plan()
    assert [(c.id, c.reason) for c in plan.candidates] == [
        (old.id, "a newer save from that week is kept; every other save in it is also in a "
                 "backup that stays")]


# --- whole entries ----------------------------------------------------------------------------


def test_one_save_worth_keeping_keeps_the_whole_entry(tmp_path):
    w = World(tmp_path)
    w.add((1, P1, at(2026, 11, 19, 12), "now"))
    w.add((1, P1, at(2026, 3, 20, 12), "later"))
    mixed = w.add((1, P1, at(2026, 3, 3, 12), "old"), (2, P2, at(2026, 3, 3, 12), "only"))
    plan = w.plan()
    assert mixed.id not in ids(plan.candidates) and plan.candidates == ()


def test_an_entry_whose_saves_all_stay_in_other_entries_goes(tmp_path):
    w = World(tmp_path)
    saves = ((1, P1, at(2026, 11, 18, 12), "a"), (2, P2, at(2026, 11, 1, 12), "b"))
    older = w.add(*saves, made=at(2026, 11, 18, 13))
    w.add(*saves, made=at(2026, 11, 19, 13))
    plan = w.plan()
    assert [(c.id, c.reason) for c in plan.candidates] == [
        (older.id, "every save in it is also in a backup that stays")]
    shared = sum(fe.size for fe in older.files if fe.inline is None)
    assert plan.candidates[0].bytes == plan.bytes < shared           # its objects stay


# --- every keep rule --------------------------------------------------------------------------


def _twins(w, n, **kw):
    """``n`` entries of the same recent saves, oldest first: all but the newest would go."""
    saves = ((1, P1, at(2026, 11, 18, 12), "twin"),)
    return [w.add(*saves, made=at(2026, 11, 18, 13 + i), **kw) for i in range(n)]


def test_the_snapshot_a_restore_undo_puts_back_and_an_operations_preserve_stay(tmp_path):
    w = World(tmp_path)
    snapshot, preserve, plain, _newest = _twins(w, 4)
    vid = snapshot.members[0].vid
    w.op("panic_restore", "done", [target(snapshot=snapshot.id, preserved=[(snapshot.id, vid)])])
    w.op("place", "done", [target(snapshot=plain.id, preserved=[(preserve.id, vid)])])
    plan = w.plan()
    assert ids(plan.candidates) == {plain.id}                    # a place's own snapshot goes
    assert reasons(plan) == {snapshot.id: (thinning.OP_RESTORE, thinning.OP_PRESERVE),
                             preserve.id: (thinning.OP_PRESERVE,)}


def test_the_versions_an_undo_puts_back_stay(tmp_path):
    w = World(tmp_path)
    w.add((1, P1, at(2026, 11, 19, 12), "now"))
    evicted = w.add((1, P1, at(2026, 3, 3, 12), "evicted"), made=at(2026, 3, 3, 13))
    again = w.add((1, P1, at(2026, 3, 3, 12), "evicted"), made=at(2026, 3, 4, 13))
    later = w.add((1, P1, at(2026, 3, 20, 12), "later"))
    assert ids(w.plan().candidates) == {evicted.id, again.id}    # the schedule alone
    w.op("place", "done", [target(evicted=evicted.members[0].vid)])
    plan = w.plan()
    assert ids(plan.candidates) == {evicted.id}                  # the newest holder stays
    assert reasons(plan) == {again.id: (thinning.OP_UNDO,)} and later.id not in reasons(plan)


def test_the_snapshot_the_next_whole_folder_undo_lands_on_stays(tmp_path):
    w = World(tmp_path)
    saves = ((1, P1, at(2026, 11, 1, 12), "s"),)
    first = w.add(*saves, live=True, made=at(2026, 11, 2, 12))
    second = w.add(*saves, live=True, made=at(2026, 11, 3, 12), op_id="op-20261103T120000Z-cd01")
    full = w.add(*saves, kind="full", label="", made=at(2026, 11, 4, 12))   # the same save
    plan = w.plan()
    assert ids(plan.candidates) == {first.id, full.id}        # the snapshot that stays holds it
    assert reasons(plan) == {second.id: (thinning.UNDO_NEXT,)}
    w.op("place", "abandoned", op_id="op-20261103T120000Z-cd01",
         done={"reason": "not_started"})                     # its op never wrote: not a target
    plan = w.plan()
    assert ids(plan.candidates) == {second.id, full.id}
    assert reasons(plan) == {first.id: (thinning.UNDO_NEXT,)}


def _fold(w, entry_id, *saves):
    """An old-layout backup folded into the store under its own id (its folder stays)."""
    folder = w.root / "backups" / entry_id
    for slot, ident, ts, tag in saves:
        steam_save(folder, slot, 0, "Main", 100, ident, ts, extra=BIG + tag)
    Vault(w.root).upsert(CatalogEntry(id=entry_id, kind="full", label="", path=str(folder),
                                      created="2026-03-03T13:00:00", source=STEAM_ACCOUNT,
                                      managed=True))
    store = open_vault_store(w.root)
    store.add_from_folder(folder, kind="full", label="", entry_id=entry_id,
                          created_local="2026-03-03T13:00:00",
                          source={"type": "folder", "name": STEAM_ACCOUNT},
                          origin={"legacy_entry": entry_id, "legacy_subdir": "backups"})
    path = w.root / "store" / "state" / "migration.json"
    body = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    body.setdefault("fold", {"state": "done", "entries": {}})["entries"][entry_id] = {
        "status": "verified"}
    path.write_text(json.dumps(body), "utf-8")
    return folder


def test_a_folded_backup_whose_old_folder_is_still_there_stays(tmp_path):
    w = World(tmp_path)
    w.add((1, P1, at(2026, 11, 19, 12), "now"))
    w.add((1, P1, at(2026, 3, 20, 12), "later"))
    folder = _fold(w, "full-steam-20260303-130000", (1, P1, at(2026, 3, 3, 12), "old"))
    plan = w.plan()
    assert plan.candidates == ()
    assert reasons(plan) == {"full-steam-20260303-130000": (thinning.OLD_FOLDER,)}
    shutil.rmtree(folder)                                    # freed: the folder is gone
    plan = w.plan()
    assert ids(plan.candidates) == {"full-steam-20260303-130000"}
    result = thinning.run_thin(w.root, plan, ids(plan.candidates))
    assert result.thinned == ("full-steam-20260303-130000",)
    assert Vault(w.root, read_only=True).get("full-steam-20260303-130000") is None
    report = open_vault_store(w.root).fsck()
    assert report.ok, report.problems                        # the fold record is accepted


def test_an_entry_the_owner_named_stays_and_one_the_app_named_does_not(tmp_path):
    """The labels the apps write, listed exactly from the code that writes them; anything else
    is the owner's (review finding 3: "pre-..." and free "slot N from ..." text were taken for
    the app's)."""
    w = World(tmp_path)
    edit = {"made_by": "nms-savecraft 0.0.1", "edit_of": "a" * 64, "what": "rename ship 'Ark'"}
    owners = ["my base before the update", "pre-expedition backup",      # no app wrote "pre-"
              "slot 1 from my old laptop", "auto: my own words", "kept: the good one",
              "edited by me: a note", "GOG20261101-1200"]               # not a GOG folder
    apps = [("auto: place", None), ("auto: swap", None), ("auto: account_edit", None),
            ("auto: pre-restore of full-steam-20260101-000000", None),
            ("auto: pre-promote slot 3B", None), ("auto: pre-clear slot 12", None),
            ("kept: slot 3", None), ("kept: Steam slot 7", None),
            ("kept: Steam slot 7 (second account)", None),
            ("before restoring the whole folder", None), ("Steam20261101-1200", None),
            ("slot 1 from imported-folder", None), ("", None), ("imported-folder", None),
            ("edited by nms-savecraft: rename ship 'Ark'", edit)]
    saves = (1, P1, at(2026, 11, 18, 12), "twin")
    named = [w.add(saves, label=label, made=at(2026, 11, 18, 13, i))
             for i, label in enumerate(owners)]
    made = [w.add(saves, label=label, origin=origin, made=at(2026, 11, 18, 14, i))
            for i, (label, origin) in enumerate(apps)]
    w.add(saves, made=at(2026, 11, 19, 12))
    plan = w.plan()
    assert reasons(plan) == {e.id: (thinning.NAMED,) for e in named}
    assert ids(plan.candidates) == {e.id for e in made}
    assert all(not thinning.user_label(text) for text in SNAPSHOT_LABELS.values())


def test_a_pinned_entry_stays(tmp_path):
    w = World(tmp_path)
    pinned, plain, _newest = _twins(w, 3)
    thinning.pin_entry(w.root, pinned.id)
    plan = w.plan()
    assert ids(plan.candidates) == {plain.id}
    assert reasons(plan) == {pinned.id: (thinning.PINNED,)}


def test_what_an_unfinished_operation_names_stays(tmp_path):
    w = World(tmp_path)
    snapshot, kept, plain, other, _newest = _twins(w, 5)
    open_op = "op-20261119T110000Z-ef01"
    by_id = w.add((1, P1, at(2026, 11, 18, 12), "twin"), made=at(2026, 11, 18, 12, 30),
                  op_id=open_op)                           # a snapshot that op is taking
    w.op("place", "writing", [target(snapshot=snapshot.id, preserved=[(kept.id, "a" * 64)])])
    w.op("place", "preparing", op_id=open_op)
    w.op("keep", "done", done={"entry_id": plain.id})          # the owner kept that copy
    plan = w.plan()
    assert reasons(plan) == {by_id.id: (thinning.OP_UNFINISHED,),
                             snapshot.id: (thinning.OP_UNFINISHED,),
                             kept.id: (thinning.OP_UNFINISHED,),
                             plain.id: (thinning.OP_KEEP,)}
    assert ids(plan.candidates) == {other.id}


def test_the_pre_upgrade_copy_is_never_touched_and_the_upgrade_can_still_be_undone(
        tmp_path, monkeypatch):
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])
    from .store_helpers import steam_live

    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    result = vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    copy = vault.root.parent / result.copy_aside
    first = operations.snapshot_live(vault, live, "pre-clear slot 1")   # as early releases did
    operations.snapshot_live(vault, live, "pre-clear slot 2")
    copy_before, live_before = fingerprint(copy), fingerprint(live)
    plan = thinning.plan_thin(vault.root)
    assert ids(plan.candidates) == {first.id}
    assert thinning.run_thin(vault.root, plan, [first.id]).thinned == (first.id,)
    assert fingerprint(copy) == copy_before and fingerprint(live) == live_before
    store = vstore.open_store(vault.root, live_roots=[live])
    undo = rollback.plan_rollback(vault.root, store)
    assert undo.problems == () and undo.copy_ok


def test_the_plan_writes_nothing(tmp_path):
    w = World(tmp_path)
    pinned, _a, _b = _twins(w, 3)
    thinning.pin_entry(w.root, pinned.id)
    w.op("place", "done", [target(snapshot=pinned.id)])
    before = fingerprint(w.root)
    plan = w.plan()
    assert plan.candidates and fingerprint(w.root) == before


def test_a_damaged_store_is_not_thinned(tmp_path):
    w = World(tmp_path)
    old, _newest = _twins(w, 2)
    store = open_vault_store(w.root)
    store.objects.path_for(next(iter(old.object_digests()))).unlink()
    plan = w.plan()
    assert plan.blocked and "problem" in plan.blocked[0]
    with pytest.raises(vstore.ThinError) as refused:
        thinning.run_thin(w.root, plan, ids(plan.candidates))
    assert refused.value.code == "damaged" and store.has_entry(old.id)


# --- the run ----------------------------------------------------------------------------------


def test_only_objects_no_manifest_names_are_deleted(tmp_path):
    w = World(tmp_path)
    w.add((1, P1, at(2026, 11, 19, 12), "now"), (2, P2, at(2026, 3, 1, 12), "shared"))
    w.add((1, P1, at(2026, 3, 20, 12), "later"), (2, P2, at(2026, 3, 1, 12), "shared"))
    old = w.add((1, P1, at(2026, 3, 3, 12), "old"), (2, P2, at(2026, 3, 1, 12), "shared"),
                made=at(2026, 3, 3, 13))
    store = open_vault_store(w.root)
    orphan = store.store_file("orphan", b"o" * 9000, 0).sha256        # an interrupted capture's
    own = {fe.sha256 for fe in old.files if fe.inline is None and fe.path == "save.hg"}
    shared = {fe.sha256 for fe in old.files if fe.inline is None} - own
    plan = w.plan()
    assert ids(plan.candidates) == {old.id}
    result = thinning.run_thin(w.root, plan, [old.id])
    assert result.thinned == (old.id,) and result.left == () and not result.cancelled
    present = set(store.objects.digests())
    assert not own & present and shared <= present and orphan in present
    assert result.freed_bytes == plan.bytes == plan.candidates[0].bytes
    assert not (w.root / "store" / "entries" / f"{old.id}.json").exists()
    assert open_vault_store(w.root).fsck().ok and not store.has_entry(old.id)
    for entry in open_vault_store(w.root).entries():
        for fe in entry.files:
            assert store.file_bytes(fe)                                 # every one still reads


def test_the_run_re_plans_under_the_lock_and_leaves_an_entry_pinned_since(tmp_path):
    w = World(tmp_path)
    first, second, _newest = _twins(w, 3)
    plan = w.plan()
    assert ids(plan.candidates) == {first.id, second.id}
    thinning.pin_entry(w.root, first.id)                       # in another window, meanwhile
    seen = []
    result = thinning.run_thin(w.root, plan, [first.id, second.id], progress=seen.append)
    assert result.thinned == (second.id,)
    assert result.left == ((first.id, thinning.PINNED),)
    assert open_vault_store(w.root).has_entry(first.id)
    assert seen[0] == thinning.ThinProgress(0, 2, first.id) and seen[-1].done == 2


def test_only_the_rows_left_ticked_go_and_a_stop_leaves_the_rest(tmp_path):
    w = World(tmp_path)
    a, b, c, _newest = _twins(w, 4)
    plan = w.plan()
    assert plan.freed_bytes([a.id]) == plan.candidates[0].bytes
    result = thinning.run_thin(w.root, plan, [a.id, "not-in-the-list"])
    assert result.thinned == (a.id,) and result.left == (("not-in-the-list", thinning.NOT_LISTED),)
    stop = threading.Event()
    stop.set()
    result = thinning.run_thin(w.root, w.plan(), [b.id, c.id], cancel=stop)
    assert result.cancelled and result.thinned == ()
    assert open_vault_store(w.root).has_entry(b.id)


FAULTS = ["thin.after_record", "thin.after_manifest", "thin.after_objects", "thin.after_catalog"]


@pytest.mark.parametrize("point", FAULTS)
def test_an_interrupted_run_is_finished_and_the_store_checks_out(tmp_path, point):
    w = World(tmp_path)
    old, _newest = _twins(w, 2)
    Vault(w.root).upsert(CatalogEntry(id=old.id, kind="snapshot", label="", path="",
                                      created=old.created_local, managed=True))   # a leftover
    plan = w.plan()
    assert ids(plan.candidates) == {old.id}

    def crash(at):
        if at == point:
            raise OSError("the machine went down")

    with pytest.raises(OSError):
        thinning.run_thin(w.root, plan, [old.id], faults=crash)
    assert thinning.thinning_pending(w.root)
    assert not thinning.thinned_entries(w.root)[old.id].done
    done = thinning.finish_thinning(w.root)
    assert done.thinned == (old.id,) and not thinning.thinning_pending(w.root)
    assert thinning.finish_thinning(w.root) is None
    store = open_vault_store(w.root)
    assert not store.has_entry(old.id) and store.fsck().ok
    assert Vault(w.root, read_only=True).get(old.id) is None
    assert not set(old.object_digests()) & set(store.objects.digests()) - set().union(
        *(e.object_digests() for e in store.entries()))
    record = thinning.thinned_entries(w.root)[old.id]
    assert record.done and record.why == "every save in it is also in a backup that stays"
    assert record.bytes == plan.candidates[0].bytes and record.kind == "snapshot"


def test_an_interrupted_run_is_finished_by_the_next_run(tmp_path):
    w = World(tmp_path)
    first, second, _newest = _twins(w, 3)
    plan = w.plan()

    def crash(at):
        if at == "thin.after_manifest":
            raise OSError("down")

    with pytest.raises(OSError):
        thinning.run_thin(w.root, plan, [first.id, second.id], faults=crash)
    result = thinning.run_thin(w.root, w.plan(), [second.id])
    assert result.thinned == (first.id, second.id)
    assert open_vault_store(w.root).fsck().ok and not thinning.thinning_pending(w.root)


def test_a_damaged_thinning_record_stops_the_run_and_is_left_as_it_is(tmp_path):
    w = World(tmp_path)
    old, _newest = _twins(w, 2)
    record = w.root / "store" / "state" / "thinning.json"
    record.write_text("{damaged", "utf-8")
    with pytest.raises(vstore.ThinError) as refused:
        thinning.run_thin(w.root, w.plan(), [old.id])
    assert refused.value.code == "damaged" and record.read_text("utf-8") == "{damaged"
    assert open_vault_store(w.root).has_entry(old.id) and thinning.thinned_entries(w.root) == {}


def test_a_run_is_refused_while_another_window_writes_live_saves(tmp_path):
    from nms_save_vault.core import safety
    from nms_save_vault.core.organizer.store import liveop_lock_path

    w = World(tmp_path)
    old, _newest = _twins(w, 2)
    plan = w.plan()
    (w.root / "organizer").mkdir(exist_ok=True)
    held = threading.Event()
    release = threading.Event()

    def writer():
        with safety.exclusive_lock(liveop_lock_path(w.root)):
            held.set()
            release.wait(10)

    other = threading.Thread(target=writer)
    other.start()
    try:
        held.wait(10)
        with pytest.raises(vstore.ThinError) as refused:
            thinning.run_thin(w.root, plan, [old.id])
        assert refused.value.code == "busy"
    finally:
        release.set()
        other.join()
    assert open_vault_store(w.root).has_entry(old.id)


# --- the review's findings (2026-11-20) -------------------------------------------------------


def _shared_world(tmp_path):
    """A named entry and an old one share an old save of P2; the old one may go."""
    w = World(tmp_path)
    shared = (2, P2, at(2026, 3, 1, 12), "shared")
    w.add((1, P1, at(2026, 11, 19, 12), "now"), (2, P2, at(2026, 11, 19, 12), "p2now"))
    w.add((1, P1, at(2026, 3, 20, 12), "later"), (2, P2, at(2026, 3, 25, 12), "p2later"))
    named = w.add((1, P1, at(2026, 2, 20, 12), "feb"), shared, label="my best run",
                  made=at(2026, 3, 1, 13))
    old = w.add((1, P1, at(2026, 3, 3, 12), "old"), shared, made=at(2026, 3, 3, 13))
    assert ids(w.plan().candidates) == {old.id}
    return w, named, old, named.object_digests() & old.object_digests()


def test_no_object_goes_while_a_manifest_cannot_be_read(tmp_path, monkeypatch):
    """Finding 1: a manifest the listing has but that cannot be read now (a cloud file not on
    this machine, a file held open) names objects nobody sees: nothing may be deleted then."""
    w, named, old, shared = _shared_world(tmp_path)
    real = Path.read_bytes

    def offline(self):
        if self.name == f"{named.id}.json":
            raise PermissionError(13, "The cloud file provider is not running")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", offline)
    versionstore._PARSED.clear()                      # another process: nothing parsed yet
    plan = w.plan()
    assert any("cannot be read" in why for why in plan.blocked)
    report = open_vault_store(w.root).fsck()
    assert [(p.kind, p.entry_id) for p in report.problems] == [("manifest", named.id)]
    with pytest.raises(vstore.ThinError):
        thinning.run_thin(w.root, plan, [old.id])
    assert shared <= set(open_vault_store(w.root).objects.digests())
    monkeypatch.setattr(Path, "read_bytes", real)
    versionstore._PARSED.clear()
    plan = w.plan()
    assert plan.blocked == () and thinning.run_thin(w.root, plan, [old.id]).thinned == (old.id,)
    store = open_vault_store(w.root)
    assert store.fsck().ok and shared <= set(store.objects.digests())


def test_an_interrupted_run_is_not_finished_while_a_manifest_is_damaged(tmp_path):
    """Finding 1: the store is checked before an interrupted run is finished."""
    w, named, old, shared = _shared_world(tmp_path)

    def crash(point):
        if point == "thin.after_manifest":
            raise OSError("down")

    with pytest.raises(OSError):
        thinning.run_thin(w.root, w.plan(), [old.id], faults=crash)
    path = w.root / "store" / "entries" / f"{named.id}.json"
    good = path.read_bytes()
    path.write_bytes(good.replace(b'"label"', b'"lAbel"', 1))          # damaged meanwhile
    versionstore._PARSED.clear()
    for finish in (lambda: thinning.finish_thinning(w.root),
                   lambda: thinning.run_thin(w.root, w.plan(), [])):
        with pytest.raises(vstore.ThinError) as refused:
            finish()
        assert refused.value.code == "damaged"
    assert shared <= set(open_vault_store(w.root).objects.digests())
    assert thinning.thinning_pending(w.root)
    path.write_bytes(good)                                # put back from a copy
    versionstore._PARSED.clear()
    assert thinning.finish_thinning(w.root).thinned == (old.id,)
    store = open_vault_store(w.root)
    assert store.fsck().ok and shared <= set(store.objects.digests())


def test_an_edit_that_kept_its_saves_time_is_the_newer_save(tmp_path):
    """Finding 2: an edit keeps the meta's time (saveedit), so the edited save and the one it
    was made from tie on it; the later capture is the newer save, never the larger sha256."""
    w = World(tmp_path)
    when = at(2026, 9, 10, 12)                           # the slot was last played then
    tags = [f"t{n}" for n in range(40)]
    vids = {t: _data_vid(tmp_path, P1, t) for t in tags}
    original_tag = max(tags, key=vids.get)               # the original sorts last by sha256
    edit_tag = min(tags, key=vids.get)
    original = w.add((1, P1, when, original_tag), made=at(2026, 11, 19, 10), kind="full",
                     label="")
    edited = w.add((1, P1, when, edit_tag), made=at(2026, 11, 19, 11), kind="extract",
                   label="edited by nms-savecraft: rename ship 'Ark'",
                   origin={"made_by": "nms-savecraft 0.0.1", "edit_of": vids[original_tag],
                           "what": "rename ship 'Ark'"})
    plan = w.plan()
    assert edited.id not in ids(plan.candidates)       # the vault's copy of the live save
    assert ids(plan.candidates) == {original.id}


def test_saves_whose_order_cannot_be_told_are_both_kept(tmp_path):
    """Finding 2: the same time, first kept at the same moment: neither is dropped for the
    other."""
    w = World(tmp_path)
    when = at(2026, 9, 10, 12)
    one = w.add((1, P1, when, "one"), made=at(2026, 9, 10, 13), kind="full", label="")
    two = w.add((1, P1, when, "two"), made=at(2026, 9, 10, 13), kind="full", label="")
    assert one.id != two.id and w.plan().candidates == ()


def _data_vid(tmp_path, ident: int, tag: str) -> str:
    import hashlib

    folder = tmp_path / "probe" / tag
    steam_save(folder, 1, 0, "Main", 100, ident, 0, extra=BIG + tag)
    return hashlib.sha256((folder / "save.hg").read_bytes()).hexdigest()


def test_a_save_file_kept_without_its_partner_keeps_its_entry(tmp_path):
    """Finding 4: every file an entry holds counts, not only whole saves: a save file kept
    without its meta (or any other file of the folder) stays while it is in no newer backup.
    A save's own meta and the game's cache do not keep an entry on their own."""
    from nms_save_vault.core import slotmap

    w = World(tmp_path)
    twin = (1, P1, at(2026, 11, 18, 12), "twin")
    lonely_meta = slotmap.meta_filename(slotmap.slot_file_numbers(5)[0])
    odd = w.add(twin, (5, P2, at(2026, 11, 1, 12), "only-here"), made=at(2026, 11, 18, 13),
                files={lonely_meta: None})
    assert {m.slot for m in odd.members} == {1} and odd.notes
    stray = w.add(twin, made=at(2026, 11, 18, 14), files={"notes.txt": b"my own notes"})
    restamped = w.add((1, P1, at(2026, 11, 18, 12, 30), "twin"), made=at(2026, 11, 18, 15))
    w.add(twin, made=at(2026, 11, 19, 12))
    plan = w.plan()
    assert ids(plan.candidates) == {restamped.id}       # its meta differs; the save does not
    assert reasons(plan) == {odd.id: (thinning.OTHER_FILE,), stray.id: (thinning.OTHER_FILE,)}


def test_a_note_or_pin_that_does_not_read_stops_thinning(tmp_path):
    """Finding 5: a damaged pin or note would read as no protection: thinning refuses."""
    w = World(tmp_path)
    old, _newest = _twins(w, 2)
    with OrganizerStore(w.root, create=True).transaction() as tx:
        tx.doc("notes.json")["entries"] = {old.id: {"pinned": "yes", "at": 5}}
    plan = w.plan()
    assert plan.blocked and "notes" in plan.blocked[0]
    with pytest.raises(vstore.ThinError) as refused:
        thinning.run_thin(w.root, plan, [old.id])
    assert refused.value.code == "blocked" and open_vault_store(w.root).has_entry(old.id)
    for broken in ({"versions": {old.members[0].vid: {"label": 7}}},
                   {"save_files": {pid(P1): {"text": "x"}}}, {"entries": ["not a map"]}):
        with OrganizerStore(w.root, create=True).transaction() as tx:
            doc = tx.doc("notes.json")
            for key in ("entries", "versions", "save_files"):
                doc.pop(key, None)
            doc.update(broken)
        assert w.plan().blocked, broken


def test_an_object_held_open_stops_the_run_and_is_finished_at_the_next_start(
        tmp_path, monkeypatch):
    """Finding 7: a file another program holds (Windows) is waited for, then the run stops with
    a ThinError; the entry is finished at the next start."""
    w = World(tmp_path)
    old = w.add((1, P1, at(2026, 3, 3, 12), "old"), made=at(2026, 3, 3, 13))
    w.add((1, P1, at(2026, 3, 20, 12), "later"))
    w.add((1, P1, at(2026, 11, 19, 12), "now"))
    held = open_vault_store(w.root).objects.path_for(next(iter(old.object_digests())))
    real = Path.unlink

    def busy(self, *a, **k):
        if self.name == held.name:
            raise PermissionError(13, "The process cannot access the file", None, 32)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", busy)
    monkeypatch.setattr(thinning, "BUSY_WAIT_SECONDS", 0.05, raising=False)
    with pytest.raises(vstore.ThinError) as refused:
        thinning.run_thin(w.root, w.plan(), [old.id])
    assert refused.value.code == "busy" and thinning.thinning_pending(w.root)
    monkeypatch.setattr(Path, "unlink", real)
    assert thinning.finish_thinning(w.root).thinned == (old.id,)
    store = open_vault_store(w.root)
    assert store.fsck().ok and not store.objects.has(held.name)


@pytest.mark.skipif(os.name != "nt", reason="a file held open blocks its deletion on Windows")
def test_an_object_another_program_reads_stops_the_run_with_a_thin_error(tmp_path, monkeypatch):
    w = World(tmp_path)
    old = w.add((1, P1, at(2026, 3, 3, 12), "old"), made=at(2026, 3, 3, 13))
    w.add((1, P1, at(2026, 3, 20, 12), "later"))
    w.add((1, P1, at(2026, 11, 19, 12), "now"))
    monkeypatch.setattr(thinning, "BUSY_WAIT_SECONDS", 0.05, raising=False)
    held = open_vault_store(w.root).objects.path_for(next(iter(old.object_digests())))
    with open(held, "rb"):                               # a deep check in another window
        with pytest.raises(vstore.ThinError) as refused:
            thinning.run_thin(w.root, w.plan(), [old.id])
    assert refused.value.code == "busy" and thinning.thinning_pending(w.root)
    assert thinning.finish_thinning(w.root).thinned == (old.id,)


# --- pins and the reminder --------------------------------------------------------------------


def test_pins_round_trip_and_are_refused_while_the_vault_is_read_only(tmp_path):
    w = World(tmp_path)
    entry = w.add((1, P1, at(2026, 11, 19, 12), "a"))
    vid = entry.members[0].vid
    thinning.pin_entry(w.root, entry.id)
    thinning.pin_version(w.root, vid)
    assert thinning.pinned_entries(w.root) == {entry.id}
    assert thinning.pinned_versions(w.root) == {vid}
    note = w.notes().version_note(vid)
    assert note.locked and note.pid == pid(P1) and note.pair == (vid, None)
    thinning.unpin_version(w.root, vid)
    thinning.unpin_entry(w.root, entry.id)
    assert thinning.pinned_entries(w.root) == frozenset() == thinning.pinned_versions(w.root)
    with pytest.raises(KeyError):
        thinning.pin_entry(w.root, "full-steam-20200101-000000")
    with pytest.raises(KeyError):
        thinning.pin_version(w.root, "f" * 64)
    with pytest.raises(ValueError):
        thinning.pin_entry(w.root, "../outside")
    fmt = w.root / "store" / "FORMAT"
    body = json.loads(fmt.read_text("ascii"))
    body["write_compat"] = versionstore.STORE_VERSION + 1
    fmt.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="ascii")
    before = fingerprint(w.root / "organizer")
    with pytest.raises(vstore.StoreTooNew):
        thinning.pin_entry(w.root, entry.id)
    with pytest.raises(vstore.StoreTooNew):
        thinning.pin_version(w.root, vid)
    assert fingerprint(w.root / "organizer") == before


def test_the_reminder_size_is_kept_in_the_settings_and_the_vault_size_read_from_manifests(
        tmp_path):
    state = appstate.AppState(thin_reminder_bytes=2 << 30)
    loaded = appstate.AppState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert loaded.thin_reminder_bytes == 2 << 30
    assert appstate.AppState.from_dict({}).thin_reminder_bytes == 0          # off
    for bad in (-1, "2 GB", True, 1.5):
        assert appstate.AppState.from_dict({"thin_reminder_bytes": bad}).thin_reminder_bytes == 0
    w = World(tmp_path)
    _twins(w, 2)
    assert thinning.vault_size(w.root) == open_vault_store(w.root).stored_bytes() > 9000
