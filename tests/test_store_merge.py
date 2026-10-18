"""Version store B-18: importing a vault that has a version store merges it, once (D3 4.8, T22
merge part). Fixtures only; the source vault is never changed."""
from __future__ import annotations

import hashlib
import shutil

import pytest

from nms_save_vault import cli
from nms_save_vault.core import operations, safety
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.store import classic, versionstore

from .conftest import steam_save
from .store_helpers import BIG, IDENT, steam_live


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(versionstore.discover, "all_live_paths", lambda *a, **k: [])


def _vault(root, name):
    live = steam_live(root / f"NMS-{name}")
    vault = Vault(root / name)
    vault.ensure()
    vstore.create_store(vault.root)
    vault.save()
    return vault, live


def _tree(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _files(store, entry_id):
    entry = store.entry(entry_id)
    return {f.path: store.file_bytes(f) for f in entry.files}


@pytest.fixture
def two(tmp_path):
    src, src_live = _vault(tmp_path, "source")
    dst, dst_live = _vault(tmp_path, "target")
    ids = [operations.create_full_backup(src, src_live, warnings=[]).id]
    steam_save(src_live, 1, 0, "Main", 9000, IDENT, 1_790_100_000, extra=BIG + "later")
    ids.append(operations.snapshot_live(src, src_live, "later").id)
    own = classic.store_of(dst).add_from_folder(      # a name of its own: ids come from the clock
        dst_live, kind="full", label="mine", source={"type": "folder", "name": "n"},
        entry_id="own-backup").id
    return src, dst, ids, own


def test_a_store_vault_is_merged_once(two):
    src, dst, ids, own = two
    before = _tree(src.root)
    result = operations.import_vault(dst, src.root, copy_into_vault=True)
    store = classic.store_of(dst)
    source = classic.open_source(src.root)
    assert sorted(result.changed) == sorted(ids) and result.ok and result.warnings == []
    assert "merged into the version store" in result.detail
    for entry_id in ids:
        assert _files(store, entry_id) == _files(source, entry_id)
        merged = store.entry(entry_id)
        assert merged.origin == {"store_id": source.store_id, "entry_id": entry_id}
        assert merged.created_local == source.entry(entry_id).created_local
    assert store.entry(own).origin is None and store.fsck(deep=True).ok
    assert _tree(src.root) == before                                      # only read
    objects = sorted(store.objects.digests())
    again = operations.import_vault(dst, src.root, copy_into_vault=True)
    assert again.changed == [] and "2 already present" in again.detail    # adds nothing
    assert sorted(store.objects.digests()) == objects
    assert sorted(e.id for e in store.entries()) == sorted([own, *ids])


def test_an_id_with_other_contents_is_imported_as_m1(two, tmp_path):
    src, dst, ids, own = two
    taken = tmp_path / "taken folder"
    shutil.copytree(steam_live(tmp_path / "x"), taken)
    other = tmp_path / "other folder"
    shutil.copytree(taken, other)
    (other / "notes.txt").write_bytes(b"different")
    mine = classic.store_of(dst).add_from_folder(
        taken, kind="imported", label="mine", source={"type": "folder", "name": "t"},
        entry_id="shared-id")
    theirs = classic.store_of(src).add_from_folder(
        other, kind="imported", label="theirs", source={"type": "folder", "name": "o"},
        entry_id="shared-id")
    result = operations.import_vault(dst, src.root, copy_into_vault=True)
    store = classic.store_of(dst)
    assert "shared-id-m1" in result.changed and "shared-id" not in result.changed
    assert any("shared-id" in w and "shared-id-m1" in w for w in result.warnings)
    assert store.entry("shared-id").body_sha256 == mine.body_sha256       # ours is untouched
    assert _files(store, "shared-id-m1") == _files(classic.store_of(src), "shared-id")
    assert store.entry("shared-id-m1").label == theirs.label
    assert operations.import_vault(dst, src.root, copy_into_vault=True).changed == []


def test_a_catalog_id_counts_as_taken(two):
    src, dst, ids, own = two
    folder = dst.backups_dir / ids[0]
    folder.mkdir(parents=True)
    (folder / "x.txt").write_bytes(b"x")
    dst.upsert(dst.make_entry_from_dir(ids[0], "full", "old layout one", folder, managed=True))
    result = operations.import_vault(dst, src.root, copy_into_vault=True)
    assert f"{ids[0]}-m1" in result.changed


def test_merging_back_and_forth_does_not_duplicate(two, tmp_path):
    src, dst, ids, own = two
    operations.import_vault(dst, src.root, copy_into_vault=True)
    back = operations.import_vault(src, dst.root, copy_into_vault=True)
    assert back.changed == [own] and not any("-m" in i for i in back.changed)
    assert operations.import_vault(src, dst.root, copy_into_vault=True).changed == []
    mirror = tmp_path / "mirror"
    shutil.copytree(src.root, mirror)                                      # a USB copy of it
    assert operations.import_vault(src, mirror, copy_into_vault=True).changed == []


def test_a_damaged_source_object_stops_only_its_entry(two):
    src, dst, ids, own = two
    source = classic.store_of(src)
    here = set(classic.store_of(dst).objects.digests())
    victim = next(f for f in source.entry(ids[1]).files
                  if f.inline is None and f.sha256 not in here)          # only the source has it
    path = source.objects.path_for(victim.sha256)
    path.write_bytes(path.read_bytes()[:-1])
    result = operations.import_vault(dst, src.root, copy_into_vault=True)
    store = classic.store_of(dst)
    broken = [i for i in ids if not store.has_entry(i)]
    assert broken == [ids[1]] and store.has_entry(ids[0]) and not result.ok
    assert any("not imported" in w for w in result.warnings)
    assert store.fsck(deep=True).ok                  # nothing half-written was committed


def test_refusals(two, tmp_path):
    src, dst, ids, own = two
    legacy = Vault(tmp_path / "legacy")
    legacy.ensure()
    legacy.save()
    with pytest.raises(operations.OperationError, match="Upgrade the vault first"):
        operations.import_vault(legacy, src.root, copy_into_vault=True)
    with pytest.raises(operations.OperationError, match="copy it in instead"):
        operations.import_vault(dst, src.root, copy_into_vault=False)
    assert not classic.store_of(dst).has_entry(ids[0])
    with pytest.raises(operations.OperationError, match="current vault"):
        operations.import_vault(dst, dst.root, copy_into_vault=True)
    (src.root / "store" / "state" / "upgrade.json").write_text("{}", "ascii")
    with pytest.raises(operations.OperationError, match="being upgraded"):
        operations.import_vault(dst, src.root, copy_into_vault=True)


def test_a_pre_upgrade_copy_is_still_refused(tmp_path):
    live = steam_live(tmp_path / "NMS")
    old = Vault(tmp_path / "old")
    operations.create_full_backup(old, live, warnings=[])
    result = vstore.run_upgrade(vstore.plan_upgrade(old.root, live_roots=[live]))
    copy = old.root.parent / result.copy_aside
    dst, _live = _vault(tmp_path, "target")
    with pytest.raises(operations.OperationError, match="pre-upgrade copy"):
        operations.import_vault(dst, copy, copy_into_vault=True)


def test_the_preview_says_what_is_new(two):
    src, dst, ids, own = two
    pv = operations.preview_vault_import(dst, src.root)
    assert pv["merge"] and pv["total"] == 2 and len(pv["new"]) == 2 and pv["existing"] == []
    operations.import_vault(dst, src.root, copy_into_vault=True)
    pv = operations.preview_vault_import(dst, src.root)
    assert pv["new"] == [] and len(pv["existing"]) == 2


def test_unfolded_folders_of_the_source_come_along(tmp_path):
    live = steam_live(tmp_path / "NMS")
    src = Vault(tmp_path / "src")
    folder_entry = operations.create_full_backup(src, live, warnings=[])        # old layout
    vstore.run_upgrade(vstore.plan_upgrade(src.root, live_roots=[live]))        # not folded
    dst, _live = _vault(tmp_path, "target")
    result = operations.import_vault(dst, src.root, copy_into_vault=True)
    assert result.changed == [folder_entry.id]
    assert classic.store_of(dst).entry(folder_entry.id).origin == {
        "imported_entry": folder_entry.id}


def test_the_cli_imports_a_store_vault(two, monkeypatch, capsys):
    src, dst, ids, own = two
    monkeypatch.setattr(appstate, "load", lambda *a, **k: None)
    assert cli.main(["--vault", str(dst.root), "import", str(src.root), "--copy"]) == 0
    assert "imported 2 new entries" in capsys.readouterr().out
    assert cli.main(["--vault", str(dst.root), "import", str(src.root), "--copy"]) == 0
    assert "imported 0 new entries" in capsys.readouterr().out


def test_a_merged_snapshot_is_never_the_undo_target_of_a_live_folder(tmp_path):
    import time

    live = steam_live(tmp_path / "NMS")                      # the same live path on both "PCs"
    here, _l1 = _vault(tmp_path, "here")
    other, _l2 = _vault(tmp_path, "other")
    mine = operations.snapshot_live(here, live, "mine").id
    time.sleep(1.1)
    theirs = operations.snapshot_live(other, live, "taken elsewhere").id
    assert operations.plan_undo(here, live).snapshot_id == mine
    operations.import_vault(here, other.root, copy_into_vault=True)
    merged = classic.store_of(here).entry(theirs)
    assert merged.op_id is None and "folder" not in merged.source and merged.origin["store_id"]
    assert operations.plan_undo(here, live).snapshot_id == mine


def test_an_object_already_here_is_re_hashed_and_healed_by_a_merge(two):
    src, dst, ids, own = two
    source = classic.store_of(src)
    target = classic.store_of(dst)
    shared = sorted({f.sha256 for f in source.entry(ids[0]).files if f.inline is None}
                    & set(target.objects.digests()))
    assert shared
    path = target.objects.path_for(shared[0])
    bad = bytearray(path.read_bytes())
    bad[0] ^= 0xFF                                           # same size, wrong bytes
    path.write_bytes(bytes(bad))
    result = operations.import_vault(dst, src.root, copy_into_vault=True)
    assert result.ok and sorted(result.changed) == sorted(ids)
    assert target.fsck(deep=True).ok                         # healed from the source's good copy


def test_a_chain_of_merges_adds_nothing_the_second_time(tmp_path):
    a, a_live = _vault(tmp_path, "a")
    b, _l = _vault(tmp_path, "b-usb")
    c, _l2 = _vault(tmp_path, "c")
    x = operations.create_full_backup(a, a_live, warnings=[]).id
    assert operations.import_vault(b, a.root, copy_into_vault=True).changed == [x]
    assert operations.import_vault(c, b.root, copy_into_vault=True).changed == [x]
    preview = operations.preview_vault_import(c, a.root)
    assert preview["new"] == [] and len(preview["existing"]) == 1
    again = operations.import_vault(c, a.root, copy_into_vault=True)
    assert again.changed == [] and again.warnings == []
    assert sorted(e.id for e in classic.store_of(c).entries()) == [x]


def test_the_preview_after_a_round_trip_counts_only_what_is_new(two):
    src, dst, ids, own = two
    operations.import_vault(dst, src.root, copy_into_vault=True)
    operations.import_vault(src, dst.root, copy_into_vault=True)
    preview = operations.preview_vault_import(src, dst.root)
    assert preview["new"] == []
