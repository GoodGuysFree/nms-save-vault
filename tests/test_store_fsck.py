"""Version store B-11: fsck quick / deep / repair and ``nmsvault-cli store fsck`` (D3 T24)."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import time

import pytest

from nms_save_vault import cli
from nms_save_vault.core import discover
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.store import StoreEntry

from .store_helpers import STEAM_ACCOUNT, new_vault, open_vault_store, steam_live
from .test_store_capture_xbox import wgs_live

SNAP = vstore.CaptureScope()


@pytest.fixture
def kept(tmp_path):
    """A vault whose store holds a legacy-style folder (backups/<id>) and a live snapshot."""
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    legacy = vault / "backups" / "full-steam-20260101-000000"
    shutil.copytree(live, legacy)
    store = open_vault_store(vault, live_roots=[])
    folded = store.add_from_folder(legacy, kind="full", label="old", entry_id=legacy.name,
                                   source={"type": "folder", "name": live.name},
                                   scope=vstore.CaptureScope(cache="include"))
    snap = store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT,
                               kind="snapshot", label="t", scope=SNAP)
    big = next(f for f in snap.files if f.inline is None and f.path == "save.hg")
    return dict(tmp_path=tmp_path, vault=vault, live=live, legacy=legacy, store=store,
                folded=folded, snap=snap, digest=big.sha256)


def test_a_clean_store_passes(kept):
    store = kept["store"]
    for deep in (False, True):
        report = store.fsck(deep=deep)
        assert report.ok and report.problems == () and report.deep is deep
        assert report.entries == 2 and report.objects == len(list(store.objects.digests()))
        assert (report.unreferenced, report.tmp_leftovers) == (0, 0)
    assert store.damage() == []


def test_missing_and_resized_objects_are_found_quickly_and_listed(kept):
    store, digest = kept["store"], kept["digest"]
    store.objects.path_for(digest).unlink()
    report = store.fsck()
    assert not report.ok
    assert {(p.kind, p.entry_id, p.path) for p in report.problems} == {
        ("missing", kept["folded"].id, "save.hg"), ("missing", kept["snap"].id, "save.hg")}
    assert {(d.entry_id, d.problem) for d in store.damage()} == {
        (kept["folded"].id, "missing"), (kept["snap"].id, "missing")}
    other = next(f.sha256 for f in kept["snap"].files if f.inline is None and f.sha256 != digest)
    store.objects.path_for(other).write_bytes(b"short")
    assert "corrupt" in {p.kind for p in store.fsck().problems}


def test_only_deep_rehashes(kept):
    store, digest = kept["store"], kept["digest"]
    path = store.objects.path_for(digest)
    raw = path.read_bytes()
    path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    assert store.fsck().ok
    deep = store.fsck(deep=True)
    assert {p.kind for p in deep.problems} == {"corrupt"} and not deep.ok
    assert path.read_bytes() != raw                  # fsck without --repair changes nothing


def test_unreferenced_objects_tmp_leftovers_and_bad_manifests_are_reported(kept):
    store = kept["store"]
    store.store_file("x", b"o" * 9000, 0)            # an object no entry uses
    (store.root / "tmp" / "ab.1.cd.part").write_bytes(b"x")
    (store.root / "entries" / "broken-entry.json").write_bytes(b"{}")
    report = store.fsck()
    assert (report.unreferenced, report.tmp_leftovers) == (1, 1)
    assert [(p.kind, p.entry_id) for p in report.problems] == [("manifest", "broken-entry")]
    assert [(d.entry_id, d.problem) for d in store.damage()] == [("broken-entry", "manifest")]


def _write_tampered(store, entry: StoreEntry, new_id: str, **member_changes) -> StoreEntry:
    m = dataclasses.replace(entry.members[0], **member_changes)
    fields = {f.name: getattr(entry, f.name) for f in dataclasses.fields(entry)
              if f.name not in ("id", "members", "body_sha256", "schema", "extra")}
    tampered = StoreEntry.new(id=new_id, members=[m, *entry.members[1:]], **fields)
    store._write_manifest(tampered)
    return tampered


def test_deep_checks_members_and_xbox_indexes(kept, make_wgs_account):
    store = kept["store"]
    _write_tampered(store, kept["snap"], "snapshot-steam-20260101-000001",
                    valid=not kept["snap"].members[0].valid)
    acct = wgs_live(make_wgs_account, kept["tmp_path"] / "xlive")
    xbox = store.add_from_live(acct, platform="xbox", account_key=acct.name, kind="snapshot",
                               label="t", scope=SNAP)
    _write_tampered(store, xbox, "snapshot-xbox-20260101-000001", xbox_identifier="Slot9Auto")
    assert store.fsck().ok
    kinds = {(p.kind, p.entry_id) for p in store.fsck(deep=True).problems}
    assert kinds == {("member", "snapshot-steam-20260101-000001"),
                     ("index", "snapshot-xbox-20260101-000001")}


def test_migration_must_agree_with_the_entries(kept):
    store = kept["store"]
    migration = {"fold": {"entries": {kept["folded"].id: {"status": "verified"},
                                      "full-steam-20250101-000000": {"status": "folded"},
                                      "snapshot-steam-20250101-000000": {"status": "failed"}}}}
    (store.root / "state" / "migration.json").write_text(json.dumps(migration), "utf-8")
    assert [(p.kind, p.entry_id) for p in store.fsck().problems] == [
        ("migration", "full-steam-20250101-000000")]


def test_fsck_reports_and_repairs(kept):
    store, digest = kept["store"], kept["digest"]
    before = sorted(store.objects.digests())
    store.objects.path_for(digest).unlink()          # the legacy folder still has the bytes
    report = store.fsck(repair=True)
    assert report.ok and report.repaired == 2
    assert all(p.repaired for p in report.problems)
    assert sorted(store.objects.digests()) == before
    assert store.objects.get(digest) == (kept["live"] / "save.hg").read_bytes()
    assert store.damage() == []

    path = store.objects.path_for(digest)
    path.write_bytes(b"\x00" * len(path.read_bytes()))
    assert store.fsck(deep=True, repair=True).ok
    assert store.objects.get(digest) == (kept["live"] / "save.hg").read_bytes()
    assert [p.name for p in (store.root / "quarantine").iterdir()] == [f"{digest}.1"]


def test_repair_from_the_copy_aside_or_a_named_folder(kept):
    store, digest, tmp_path = kept["store"], kept["digest"], kept["tmp_path"]
    shutil.rmtree(kept["legacy"])
    store.objects.path_for(digest).unlink()
    assert not store.fsck(repair=True).ok                         # no good copy anywhere
    assert {d.problem for d in store.damage()} == {"missing"}

    elsewhere = tmp_path / "usb"
    shutil.copytree(kept["live"], elsewhere / "some copy")
    assert store.fsck(repair=True, fallbacks=[elsewhere]).ok
    store.objects.path_for(digest).unlink()

    aside = kept["vault"].parent / "vault.pre-store-20261017T101500Z"
    shutil.copytree(kept["live"], aside / "backups" / "x")
    migration = {"copy_aside": {"name": aside.name, "sibling": True, "files": 1, "bytes": 1,
                                "tree_sha256": "0" * 64, "verified_utc": "2026-10-17T10:15:00Z"}}
    (store.root / "state" / "migration.json").write_text(json.dumps(migration), "utf-8")
    assert store.fsck(repair=True).ok


def test_repair_removes_old_temp_leftovers(kept):
    store = kept["store"]
    old = time.time() - 2 * 3600
    stale = store.root / "tmp" / "ab.1.cd.part"
    stale.write_bytes(b"x")
    os.utime(stale, (old, old))
    assert store.fsck().tmp_leftovers == 1
    assert store.fsck(repair=True).tmp_leftovers == 0 and not stale.exists()


def test_a_damaged_format_is_reported(kept):
    store = kept["store"]
    (store.root / "FORMAT").write_text("{", "ascii")
    report = store.fsck()
    assert [p.kind for p in report.problems] == ["format"] and not report.ok


def test_the_cli(kept, monkeypatch, capsys):
    monkeypatch.setattr(appstate, "load", lambda *a, **k: None)
    monkeypatch.setattr(discover, "all_live_paths", lambda *a, **k: [])
    vault, store, digest = kept["vault"], kept["store"], kept["digest"]
    assert cli.main(["--vault", str(vault), "store", "fsck"]) == 0
    out = capsys.readouterr().out
    assert "2 entries" in out and "No damage found." in out and out.isascii()

    store.objects.path_for(digest).unlink()
    shutil.rmtree(kept["legacy"])
    assert cli.main(["--vault", str(vault), "store", "fsck", "--deep"]) == 1
    out = capsys.readouterr().out
    assert "missing" in out and "--repair" in out
    assert cli.main(["--vault", str(vault), "store", "fsck", "--repair",
                     "--from", str(kept["live"])]) == 0
    assert "repaired" in capsys.readouterr().out

    legacy_vault = kept["tmp_path"] / "old vault"
    legacy_vault.mkdir()
    assert cli.main(["--vault", str(legacy_vault), "store", "fsck"]) == 1
    assert "Upgrade the vault first" in capsys.readouterr().err
    assert not (legacy_vault / "store").exists()
