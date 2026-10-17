"""The version store wired into D1 (D3 B-7, D1 step 12): the scanner and the bootstrap read
store entries from their manifests through the one listing, and the Organizer's VaultLocator
reads the store first and preserves into it."""
from __future__ import annotations

import dataclasses
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from nms_save_vault.core import discover, operations, safety
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer import Organizer
from nms_save_vault.core.organizer.locator import LiveMember, PreserveFailed
from nms_save_vault.core.organizer.model import EntryRef, LiveTarget, StoreRef
from nms_save_vault.core.state import Source

from .store_helpers import STEAM_ACCOUNT, steam_live

SNAP = vstore.CaptureScope()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """The per-machine cache under tmp_path, the game known not to run, no real live folders."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(discover, "all_live_paths", lambda *a, **k: [])


@pytest.fixture
def world(tmp_path):
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    full = operations.create_full_backup(vault, live, label="classic", warnings=[])
    sources = [Source(id="steam", platform="steam", account=STEAM_ACCOUNT, path=str(live))]
    return vault, live, full, sources


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _upgrade(vault, live, full):
    """What the upgrade and the fold will do (B-12, B-13): a store, the old backup folded
    under its id, then a new snapshot made in store mode."""
    vstore.create_store(vault.root)
    store = vstore.open_store(vault.root, live_roots=[live])
    store.add_from_folder(vault.backups_dir / full.id, kind="full", label=full.label,
                          source={"type": "folder", "name": live.name}, entry_id=full.id,
                          created_local=full.created, scope=vstore.CaptureScope(cache="include"),
                          origin={"legacy_entry": full.id, "legacy_subdir": "backups"})
    return store, store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT,
                                      kind="snapshot", label="auto: t", scope=SNAP)


def _same(captures_a, captures_b) -> bool:
    """Captures equal but for where they were seen (and float noise in file times)."""
    def norm(c):
        members = tuple(dataclasses.replace(m, meta=dataclasses.replace(
            m.meta, file_time=round(m.meta.file_time, 3))) for m in c.members)
        return (c.slot, members, c.observed_at, c.time_quality, c.src, c.head_member)
    return [norm(c) for c in captures_a] == [norm(c) for c in captures_b]


def test_a_folded_entry_scans_as_its_folder_did_and_store_entries_are_scanned(world):
    vault, live, full, sources = world
    before = Organizer(vault.root, sources, machine_cache=False)
    before.refresh(record=False)
    assert before.version_store is None
    folder_scan = before.scan.entries[full.id]

    _store, snap = _upgrade(vault, live, full)
    org = Organizer(vault.root, sources, machine_cache=False)
    org.refresh(record=False)
    assert org.version_store is not None
    folded = org.scan.entries[full.id]
    assert {c.where for c in folded.captures} == {StoreRef(full.id)}
    assert _same(folded.captures, folder_scan.captures)
    assert (folded.kind, folded.managed, folded.created) == ("full", True, folder_scan.created)
    assert org.scan.entries[snap.id].captures[0].where == StoreRef(snap.id)
    vid = _sha(live / "save.hg")
    kinds = {(type(loc.where).__name__, loc.entry_kind) for loc in org.scan.versions[vid].locations}
    assert ("EntryRef", "full") not in kinds                      # never scanned twice
    assert {("StoreRef", "full"), ("StoreRef", "snapshot"), ("LiveRef", "live")} <= kinds


def test_the_vault_locator_reads_the_store_first_and_preserves_into_it(world):
    vault, live, full, sources = world
    _upgrade(vault, live, full)
    org = Organizer(vault.root, sources, machine_cache=False)
    org.refresh(record=False)
    vid = _sha(live / "save.hg")
    located = org.locator.locate(vid)
    assert located[0].kind == "store" and located[-1].kind == "live"
    assert org.locator.open(vid).location.kind == "store"
    assert org.locator.is_durable(vid)
    target = LiveTarget("steam", live.name, 1)
    kept = org.locator.preserve([LiveMember(target, "A", vid)], op_id="op-1", reason="place")
    assert kept[0].kind == "store" and org.version_store.entry(kept[0].ref.key).kind == "preserve"


def test_a_legacy_vault_has_no_preserver(world):
    vault, live, _full, sources = world
    org = Organizer(vault.root, sources, machine_cache=False)
    org.refresh(record=False)
    target = LiveTarget("steam", live.name, 1)
    with pytest.raises(PreserveFailed, match="store-unavailable"):
        org.locator.preserve([LiveMember(target, "A", _sha(live / "save.hg"))],
                             op_id="op-1", reason="r")
    assert all(loc.kind != "store" for loc in org.locator.locate(_sha(live / "save.hg")))


def test_a_read_only_organizer_writes_nothing_in_the_store(world):
    vault, live, full, sources = world
    _upgrade(vault, live, full)
    tmp = vault.root / "store" / "tmp"
    org = Organizer(vault.root, sources, read_only=True)
    assert org.version_store.read_only_reason == "asked"
    org.refresh(record=False)
    assert list(tmp.iterdir()) == []
    with pytest.raises(PreserveFailed):
        org.locator.preserve([LiveMember(LiveTarget("steam", live.name, 1), "A",
                                         _sha(live / "save.hg"))], op_id="op-1", reason="r")


def test_the_bootstrap_reads_store_entries_too(tmp_path):
    from nms_save_vault.core.organizer.store import OrganizerStore

    live = steam_live(tmp_path / "NMS")
    root = tmp_path / "vault"
    Vault(root).ensure()
    OrganizerStore(root, create=True)
    vstore.create_store(root)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    store = vstore.open_store(root, live_roots=[live], clock=lambda: yesterday)
    snap = store.add_from_live(live, platform="steam", account_key=STEAM_ACCOUNT,
                               kind="snapshot", label="auto: t", scope=SNAP)
    sources = [Source(id="steam", platform="steam", account=STEAM_ACCOUNT, path=str(live))]
    report = Organizer(root, sources, machine_cache=False).bootstrap(dry_run=True)
    assert report.journal_records >= 1            # "seen in backups": the store snapshot
    assert snap.id not in {entry_id for entry_id, _why in report.entries_skipped}
    assert EntryRef(snap.id) not in {p.where for p in report.problems}
