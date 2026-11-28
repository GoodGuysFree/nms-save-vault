"""The core's must-fix findings of the 2026-11-23 data-corruption audit
(docs/CORRUPTION_AUDIT_REPORT.md): H-02 a transient sharing violation no longer fails an
atomic write; H-03 a vault upgrade killed before its marker no longer wedges the vault; H-04 a
capture that raced a thinning no longer fails as "damaged"; M-07 a journal record that lost
only its newline is counted, never given the same seq again; H-01 (part) the account files are
written together or not at all."""
from __future__ import annotations

import json
import os

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core import store as vstore
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.organizer.store import JOURNAL, OrganizerStore

from .store_helpers import new_vault, open_vault_store, steam_live
from .liveops_support import make_ctx
from .test_liveops_opjournal import OP1, T7, preparing


# --- H-02: atomic_write_bytes retries a transient sharing violation --------------------------

def _flaky_replace(monkeypatch, fails: int) -> list:
    real = safety.durable_replace
    calls = []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) <= fails:
            raise PermissionError(32, "The process cannot access the file")
        return real(src, dst)
    monkeypatch.setattr(safety, "durable_replace", replace)
    monkeypatch.setattr(safety, "REPLACE_RETRY", True)
    monkeypatch.setattr(safety, "REPLACE_PAUSE", 0.001)
    return calls


def test_an_atomic_write_waits_out_a_reader_holding_the_file(tmp_path, monkeypatch):
    path = tmp_path / "notes.json"
    path.write_bytes(b"old")
    calls = _flaky_replace(monkeypatch, fails=3)
    safety.atomic_write_bytes(path, b"new")
    assert path.read_bytes() == b"new" and len(calls) == 4
    assert not list(tmp_path.glob("*.part"))


def test_an_atomic_write_still_fails_when_the_file_stays_held(tmp_path, monkeypatch):
    path = tmp_path / "notes.json"
    path.write_bytes(b"old")
    _flaky_replace(monkeypatch, fails=10_000)
    with pytest.raises(PermissionError):
        safety.atomic_write_bytes(path, b"new")
    assert path.read_bytes() == b"old" and not list(tmp_path.glob("*.part"))


# --- H-01 (part): files written together are all written or all put back -------------------

def test_files_written_together_are_put_back_when_one_fails(tmp_path, monkeypatch):
    data, meta = tmp_path / "accountdata.hg", tmp_path / "mf_accountdata.hg"
    data.write_bytes(b"data-old")
    meta.write_bytes(b"meta-old")
    real = safety.atomic_write_bytes

    def write(path, payload, *a, **k):
        if path == meta and payload == b"meta-new":
            raise PermissionError(5, "Access is denied")
        return real(path, payload, *a, **k)
    monkeypatch.setattr(safety, "atomic_write_bytes", write)
    with pytest.raises(PermissionError):
        safety.write_files_together({data: b"data-new", meta: b"meta-new"})
    assert (data.read_bytes(), meta.read_bytes()) == (b"data-old", b"meta-old")


def test_files_written_together_a_new_one_is_removed_again(tmp_path, monkeypatch):
    data, meta = tmp_path / "accountdata.hg", tmp_path / "mf_accountdata.hg"
    meta.write_bytes(b"meta-old")
    real = safety.atomic_write_bytes

    def write(path, payload, *a, **k):
        if path == meta:
            raise PermissionError(5, "Access is denied")
        return real(path, payload, *a, **k)
    monkeypatch.setattr(safety, "atomic_write_bytes", write)
    with pytest.raises(PermissionError):
        safety.write_files_together({data: b"data-new", meta: b"meta-new"})
    assert not data.exists() and meta.read_bytes() == b"meta-old"


def test_the_account_restore_is_one_journaled_account_op():
    """Superseded H-01's write_files_together: a power-out between the two files must be found
    at the next start too (GGF 2026-11-28; tests/test_liveops_account_restore.py)."""
    from nms_save_vault.core.liveops import panic
    assert "write_account_files" in panic._restore_accountdata.__code__.co_names


# --- H-03: an upgrade killed before its marker leaves an empty store skeleton -----------------

@pytest.fixture
def legacy_vault(tmp_path, monkeypatch):
    from nms_save_vault.core import operations
    from nms_save_vault.core.catalog import Vault
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(vstore.versionstore.discover, "all_live_paths", lambda *a, **k: [])
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    return vault, live


def test_an_upgrade_killed_right_after_making_the_store_folder_is_cleaned_up(legacy_vault):
    vault, live = legacy_vault
    (vault.root / "store" / "state").mkdir(parents=True)      # killed before upgrade.json
    assert vstore.vault_mode(vault.root) == "legacy"
    cleaned = vstore.cleanup_interrupted_upgrade(vault.root)
    assert cleaned is not None and "interrupted" in cleaned.message
    assert not (vault.root / "store").exists()
    plan = vstore.plan_upgrade(vault.root, live_roots=[live])
    assert not [code for code, _text in plan.problems if code == "damaged"]


def test_a_store_folder_holding_anything_is_never_removed(legacy_vault):
    vault, _live = legacy_vault
    (vault.root / "store" / "state").mkdir(parents=True)
    (vault.root / "store" / "objects").mkdir()
    (vault.root / "store" / "objects" / "x").write_bytes(b"?")
    assert vstore.cleanup_interrupted_upgrade(vault.root) is None
    assert (vault.root / "store" / "objects" / "x").is_file()


# --- H-04: a capture that raced a thinning is taken again, not failed as damaged --------------

def test_a_capture_whose_shared_object_was_thinned_meanwhile_is_taken_again(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "live")
    store = open_vault_store(vault, live_roots=[])
    first = store.add_from_folder(live, kind="full", label="one", source={"type": "test"})
    shared = next(f for f in first.files if f.inline is None)
    thinned = []

    def verify(_records):          # between the objects put and the commit: a thinning
        if not thinned:
            store.objects.path_for(shared.sha256).unlink()
            thinned.append(shared.sha256)
    second = store.add_from_folder(live, kind="full", label="two", source={"type": "test"},
                                   verify=verify)
    assert thinned
    for f in second.files:
        if f.inline is None:
            assert store.objects.size_of(f.sha256) == f.size


# --- M-07: a record that lost only its newline is counted ------------------------------------

def test_an_organizer_record_without_its_newline_is_counted_not_reused(tmp_path):
    store = OrganizerStore(tmp_path, create=True)
    with store.transaction() as tx:
        tx.append({"t": "obs"})
        tx.append({"t": "obs"})
    journal = tmp_path / "organizer" / JOURNAL
    prefix = store.vault_id[:8]
    whole = {"t": "obs", "id": f"{prefix}-3", "seq": 3, "at": 1_790_000_000}
    with open(journal, "ab") as f:
        f.write(json.dumps(whole, separators=(",", ":")).encode())    # crashed before "\n"
    reader = OrganizerStore(tmp_path, create=False)
    with reader.transaction() as tx:
        tx.append({"t": "op"})
    fresh = OrganizerStore(tmp_path, create=False)
    seqs = [r["seq"] for r in fresh.journal_records()]
    ids = [r["id"] for r in fresh.journal_records()]
    assert seqs == [1, 2, 3, 4] and len(set(ids)) == 4
    assert not any("damaged" in p for p in fresh.problems)


def test_an_op_event_without_its_newline_is_counted_not_reused(tmp_path):
    ctx = make_ctx(tmp_path)
    preparing(ctx, OP1, T7)
    path = jr.ops_path(ctx)
    torn = {"j": 1, "op_id": OP1, "seq": 2, "event": "abandoned", "utc": "2026-11-23T00:00:00Z",
            "machine": ctx.machine_id, "reason": "not_started"}
    with open(path, "ab") as f:
        f.write(json.dumps(torn, separators=(",", ":")).encode())     # crashed before "\n"
    jr.append(ctx, OP1, "abandoned", {"reason": "not_started"})
    events = jr.read_events(path)
    assert [e["seq"] for e in events] == [1, 2, 3]
