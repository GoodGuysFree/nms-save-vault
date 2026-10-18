"""Version store B-17: freeing the old backup folders after the fold - the deep check first,
what is skipped, resuming, the listing unchanged, undo of the upgrade still working
(D3 4.5, T21, T19 free part). Fixtures only: nothing here touches a real vault."""
from __future__ import annotations

import hashlib
import json
import os
import shutil

import pytest

from nms_save_vault import cli
from nms_save_vault.core import operations, safety
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.store import free, rollback, versionstore

from .store_helpers import steam_live


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])


@pytest.fixture
def folded(tmp_path):
    """An old-layout vault (full backup, snapshot, extract, an imported copy, a hand-made
    in-place folder and the live folder catalogued in place), upgraded and folded."""
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    full = operations.create_full_backup(vault, live, warnings=[])
    snap = operations.snapshot_live(vault, live, "test")
    extract = operations.extract_slot(vault, live, 2, warnings=[], live_dirs=[live])
    hand = tmp_path / "hand made"
    shutil.copytree(live, hand)
    imported = operations.import_backup(vault, hand, copy_into_vault=True, warnings=[])
    inplace = operations.import_backup(vault, hand, copy_into_vault=False, warnings=[])
    alias = operations.import_backup(vault, live, copy_into_vault=False, warnings=[])
    result = vstore.run_upgrade(vstore.plan_upgrade(vault.root, live_roots=[live]))
    store = vstore.open_store(vault.root, live_roots=[live])
    assert vstore.run_fold(store, live_roots=[live]).fsck_ok
    ids = dict(full=full.id, snap=snap.id, extract=extract.id, imported=imported.id,
               inplace=inplace.id, alias=alias.id)
    entries = {k: vault.load().get(v) for k, v in ids.items()}
    ctx = dict(vault=vault, live=live, entries=entries, store=store, ids=ids, hand=hand,
               copy=vault.root.parent / result.copy_aside)
    return type("World", (), ctx)


def _tree(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _folder(w, key):
    return free._folder_of(w.vault, w.entries[key])


def _listing(w):
    return [(e.id, e.kind, e.ref, e.platform, e.label, e.created, e.slots)
            for e in vstore.list_vault_entries(Vault(w.vault.root).load(), w.store,
                                               live_roots=[w.live])]


MANAGED = ("full", "snap", "extract", "imported")


def test_free_old_folders(folded):
    w = folded
    folders = {k: _folder(w, k) for k in MANAGED}
    before_listing = _listing(w)
    outside = {"hand": _tree(w.hand), "live": _tree(w.live), "copy": _tree(w.copy)}
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    assert sorted(c.id for c in plan.entries) == sorted(w.ids[k] for k in MANAGED)
    assert plan.skipped == () and plan.bytes == sum(c.bytes for c in plan.entries) > 0
    assert plan.copy_aside is not None and plan.copy_aside.found
    assert all(f.is_dir() for f in folders.values())            # planning deletes nothing

    result = vstore.run_free(w.store, plan, live_roots=[w.live])
    assert sorted(result.freed) == sorted(plan.entries and [c.id for c in plan.entries])
    assert result.freed_bytes == plan.bytes and result.skipped == () and not result.leftovers
    assert "every backup is still listed" in result.message
    assert not any(f.exists() for f in folders.values())
    left = {e.id for e in Vault(w.vault.root).load().entries}
    assert left == {w.ids["inplace"], w.ids["alias"]}           # hand-made and live alias stay
    assert _listing(w) == before_listing                         # the listing is unchanged
    assert _tree(w.hand) == outside["hand"] and _tree(w.live) == outside["live"]
    assert _tree(w.copy) == outside["copy"]                      # the copy-aside is not touched
    assert w.store.fsck(deep=True).ok and not vstore.free_pending(w.vault.root)
    again = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    assert again.entries == () and again.bytes == 0
    for key in MANAGED:                                          # still readable from the store
        with w.store.materialized(w.ids[key]) as out:
            assert _tree(out)


def test_a_damaged_object_stops_the_run_before_any_deletion(folded):
    w = folded
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    digest = next(d for e in w.store.entries() for f in e.files if f.inline is None
                  for d in [f.sha256])
    path = w.store.objects.path_for(digest)
    data = bytearray(path.read_bytes())
    data[0] ^= 0xFF
    os.chmod(path, 0o666)
    path.write_bytes(bytes(data))
    with pytest.raises(vstore.FreeError) as stopped:
        vstore.run_free(w.store, plan, live_roots=[w.live])
    assert stopped.value.code == "damaged" and "nothing was deleted" in str(stopped.value)
    assert all(_folder(w, k).is_dir() for k in MANAGED)
    assert {e.id for e in Vault(w.vault.root).load().entries} >= {w.ids[k] for k in MANAGED}


def test_a_folder_changed_or_extended_after_the_fold_is_skipped(folded):
    w = folded
    full, snap = _folder(w, "full"), _folder(w, "snap")
    (full / "notes.txt").write_bytes(b"edited by hand after the fold")
    (snap / "extra.txt").write_bytes(b"something the store lacks")
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    assert dict(plan.skipped).keys() == {w.ids["full"], w.ids["snap"]}
    assert all("after the fold" in why for _k, why in plan.skipped)
    # changed between the plan and the run: the run checks again
    (_folder(w, "extract") / "notes.txt").write_bytes(b"changed after the plan")
    result = vstore.run_free(w.store, plan, live_roots=[w.live])
    assert result.freed == (w.ids["imported"],)
    assert {k for k, _why in result.skipped} >= {w.ids["extract"]}
    assert (full / "notes.txt").read_bytes() == b"edited by hand after the fold"
    assert (snap / "extra.txt").read_bytes() == b"something the store lacks"
    assert (_folder(w, "extract") / "notes.txt").read_bytes() == b"changed after the plan"
    assert not _folder(w, "imported").exists()
    kept = {e.id for e in Vault(w.vault.root).load().entries}
    assert {w.ids["full"], w.ids["snap"], w.ids["extract"]} <= kept
    assert "left as they are" in result.message


def test_it_never_deletes_what_the_store_does_not_hold(folded):
    w = folded
    store_entry = w.store._manifest_path(w.ids["full"])
    store_entry.rename(store_entry.with_suffix(".gone"))
    fresh = vstore.open_store(w.vault.root, live_roots=[w.live])
    plan = vstore.plan_free(Vault(w.vault.root).load(), fresh, live_roots=[w.live])
    assert w.ids["full"] not in {c.id for c in plan.entries}
    assert "no copy" in dict(plan.skipped)[w.ids["full"]]
    assert _folder(w, "full").is_dir()


def test_an_entry_with_the_owners_note_stays_in_the_catalog(folded):
    w = folded
    entry = w.vault.load().get(w.ids["imported"])
    entry.note = "my note"
    w.vault.upsert(entry)
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    vstore.run_free(w.store, plan, live_roots=[w.live])
    assert not _folder(w, "imported").exists()
    kept = Vault(w.vault.root).load().get(w.ids["imported"])
    assert kept is not None and kept.note == "my note"          # the owner's note is not lost
    again = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    assert again.entries == () and again.skipped == ()


def test_a_crash_between_deletions_resumes(folded):
    w = folded
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    calls = []

    def crash_after_the_second_folder(point):
        if point == "free.after_delete":
            calls.append(point)
            if len(calls) == 2:
                raise KeyboardInterrupt("the process is stopped")

    with pytest.raises(KeyboardInterrupt):
        vstore.run_free(w.store, plan, live_roots=[w.live], faults=crash_after_the_second_folder)
    gone = [k for k in MANAGED if not _folder(w, k).exists()]
    assert len(gone) == 2 and vstore.free_pending(w.vault.root)
    record = json.loads((w.vault.root / "store/state/migration.json").read_text("ascii"))
    states = sorted(v["status"] for v in record["free"]["entries"].values())
    assert states == ["freed", "freeing"] and record["free"]["state"] == "freeing"
    # the crashed entry's folder is gone but its catalog entry is still there: never listed twice
    assert len([e for e in _listing(w) if e[0] in {w.ids[k] for k in MANAGED}]) == 4
    resumed = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    assert any(c.resuming for c in resumed.entries)
    result = vstore.run_free(w.store, resumed, live_roots=[w.live])
    assert not any(_folder(w, k).exists() for k in MANAGED) and not result.leftovers
    assert not vstore.free_pending(w.vault.root)
    assert {e.id for e in Vault(w.vault.root).load().entries} == {w.ids["inplace"], w.ids["alias"]}
    assert w.store.fsck(deep=True).ok


def test_a_half_deleted_folder_is_finished_but_not_if_it_grew(folded, monkeypatch):
    w = folded
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    real = free._delete_listed
    touched = []

    def delete_one_file_then_crash(folder, files, dirs):
        touched.append(folder)
        files[0].unlink()
        raise KeyboardInterrupt("the process is stopped")

    monkeypatch.setattr(free, "_delete_listed", delete_one_file_then_crash)
    with pytest.raises(KeyboardInterrupt):
        vstore.run_free(w.store, plan, live_roots=[w.live])
    monkeypatch.setattr(free, "_delete_listed", real)
    half = touched[0]
    started = next(k for k in MANAGED if _folder(w, k) == half)
    (half / "grown.txt").write_bytes(b"added after the crash")
    again = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    assert dict(again.skipped)[w.ids[started]] == "it holds a file the store lacks"
    result = vstore.run_free(w.store, again, live_roots=[w.live])
    assert (half / "grown.txt").read_bytes() == b"added after the crash"     # kept
    assert w.ids[started] not in result.freed
    (half / "grown.txt").unlink()
    final = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    vstore.run_free(w.store, final, live_roots=[w.live])
    assert not half.exists() and not vstore.free_pending(w.vault.root)


def test_a_read_only_file_is_cleared_and_deleted(folded):
    w = folded
    target = next(p for p in sorted(_folder(w, "full").rglob("*")) if p.is_file())
    os.chmod(target, 0o444)
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    result = vstore.run_free(w.store, plan, live_roots=[w.live])
    assert not _folder(w, "full").exists() and not result.leftovers


def test_undoing_the_upgrade_still_works_after_freeing(folded):
    w = folded
    copy_before = _tree(w.copy)
    made = operations.create_full_backup(w.vault, w.live, warnings=[])        # in store mode
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    vstore.run_free(w.store, plan, live_roots=[w.live])
    assert not any(_folder(w, k).exists() for k in MANAGED)

    undo = rollback.plan_rollback(w.vault.root, w.store)
    assert undo.problems == () and set(undo.restore) >= {w.ids[k] for k in MANAGED}
    rollback.run_rollback(undo, w.store)
    assert vstore.vault_mode(w.vault.root) == "legacy"
    entries = {e.id: e for e in Vault(w.vault.root).load().entries}
    copy_vault = Vault(w.copy, read_only=True)
    for key in MANAGED:
        mine = free._folder_of(Vault(w.vault.root), entries[w.ids[key]])
        assert _tree(mine) == _tree(free._folder_of(copy_vault, copy_vault.get(w.ids[key])))
    assert made.id in entries                                                  # store-only entry
    assert _tree(w.copy) == copy_before


def _cli(monkeypatch, w, *argv):
    monkeypatch.setattr(appstate, "load", lambda *a, **k: None)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [w.live])
    return cli.main(["--vault", str(w.vault.root), "store", "free-old-folders", *argv])


def test_the_cli_is_a_dry_run_unless_told_to_delete(folded, monkeypatch, capsys):
    w = folded
    assert _cli(monkeypatch, w) == 0
    out = capsys.readouterr().out
    assert "4 old backup folders" in out and "Nothing was deleted" in out
    assert "pre-upgrade copy" in out and all(_folder(w, k).is_dir() for k in MANAGED)
    assert _cli(monkeypatch, w, "--yes") == 0
    out = capsys.readouterr().out
    assert "Freed" in out and "every backup is still listed" in out
    assert not any(_folder(w, k).exists() for k in MANAGED)
    assert _cli(monkeypatch, w) == 0
    assert "No old backup folder to free" in capsys.readouterr().out


def test_the_cli_stops_on_damage(folded, monkeypatch, capsys):
    w = folded
    digest = next(f.sha256 for e in w.store.entries() for f in e.files if f.inline is None)
    path = w.store.objects.path_for(digest)
    path.write_bytes(path.read_bytes()[:-1])
    assert _cli(monkeypatch, w, "--yes") == 1
    assert "nothing was deleted" in capsys.readouterr().err
    assert all(_folder(w, k).is_dir() for k in MANAGED)


def test_a_file_written_into_a_later_folder_is_checked_before_that_folder_goes(folded):
    w = folded
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    second = plan.entries[1].folder

    def write_while_the_first_is_deleted(p):
        if p.entries_done == 0:
            (second / "written-meanwhile.txt").write_bytes(b"not in the store")

    result = vstore.run_free(w.store, plan, live_roots=[w.live],
                             progress=write_while_the_first_is_deleted)
    assert plan.entries[1].id not in result.freed
    assert (second / "written-meanwhile.txt").read_bytes() == b"not in the store"
    assert plan.entries[1].id in dict(result.skipped)
    assert len(result.freed) == 3


def test_a_file_that_appears_after_the_check_is_never_deleted(folded):
    w = folded
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    target = plan.entries[0].folder

    def appears_after_the_check(point):
        if point == "free.before_delete" and not (target / "late.txt").exists():
            (target / "late.txt").write_bytes(b"late")

    result = vstore.run_free(w.store, plan, live_roots=[w.live], faults=appears_after_the_check)
    assert (target / "late.txt").read_bytes() == b"late"
    assert [k for k, _what in result.leftovers] == [plan.entries[0].id]
    assert plan.entries[0].id not in result.freed
    assert w.vault.load().get(plan.entries[0].id) is not None       # still in the catalog
    assert vstore.free_pending(w.vault.root)


def _make_junction(link, target):
    import subprocess

    if os.name == "nt":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True)
        if done.returncode:
            pytest.skip("this machine does not permit creating junctions")
    else:
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this machine does not permit creating symlinks")


def test_a_folder_holding_a_junction_is_skipped_and_nothing_outside_is_touched(
        folded, tmp_path):
    w = folded
    outside = tmp_path / "outside the vault"
    outside.mkdir()
    (outside / "precious.bin").write_bytes(b"keep me" * 100)
    _make_junction(_folder(w, "full") / "linked", outside)
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])
    assert w.ids["full"] not in {c.id for c in plan.entries}
    assert "link" in dict(plan.skipped)[w.ids["full"]]
    result = vstore.run_free(w.store, plan, live_roots=[w.live])
    assert w.ids["full"] not in result.freed
    assert (outside / "precious.bin").read_bytes() == b"keep me" * 100
    assert (_folder(w, "full") / "linked").exists()


def test_the_cli_says_a_run_is_pending(folded, monkeypatch, capsys):
    w = folded
    plan = vstore.plan_free(Vault(w.vault.root).load(), w.store, live_roots=[w.live])

    def crash(point):
        if point == "free.after_delete":
            raise KeyboardInterrupt("stopped")

    with pytest.raises(KeyboardInterrupt):
        vstore.run_free(w.store, plan, live_roots=[w.live], faults=crash)
    assert _cli(monkeypatch, w) == 0
    assert "stopped before its end" in capsys.readouterr().out
