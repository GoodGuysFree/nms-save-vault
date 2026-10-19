"""organizer.bootstrap: history from before recording, from the app's own backups ("seen in
backups") and its old op log ("put live by the app"); nothing guessed (D1 2.12)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nms_save_vault.core import slotmap
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer.bootstrap import bootstrap
from nms_save_vault.core.organizer.cache import ScanCache
from nms_save_vault.core.organizer.journal import Journal, obs_fields
from nms_save_vault.core.organizer.model import LiveTarget
from nms_save_vault.core.organizer.store import LINEAGE, OrganizerStore
from nms_save_vault.core.state import Source

from .conftest import steam_save

ACCOUNT = "st_76561198000000001"
MAIN = 0xDDDAD1BDD39F9C72
T9 = LiveTarget("steam", ACCOUNT, 9)
T3 = LiveTarget("steam", ACCOUNT, 3)

# One playthrough's versions: (play, meta time); the data bytes are named by the key.
VERSIONS = {"v1": (100, 1000), "v2": (200, 2000), "v25": (250, 2500), "v3": (300, 3000),
            "v4": (150, 5000), "other": (50, 900)}


def _put(folder: Path, slot: int, version: str) -> None:
    play, ts = VERSIONS[version]
    steam_save(folder, slot, 0, "Main", play, MAIN, ts, extra=version)


def _vid(version: str, tmp_path) -> str:
    scratch = tmp_path / "vid" / version
    _put(scratch, 1, version)
    return hashlib.sha256((scratch / slotmap.data_filename(1)).read_bytes()).hexdigest()


def _entry(vault: Vault, kind: str, stamp: str, created: str, slots: dict[int, str],
           source: str = ACCOUNT) -> str:
    entry_id = f"{kind}-steam-{stamp}"
    folder = (vault.backups_dir if kind == "full" else vault.snapshots_dir) / entry_id
    folder.mkdir(parents=True)
    for slot, version in slots.items():
        _put(folder, slot, version)
    entry = vault.make_entry_from_dir(entry_id, kind, entry_id, folder, managed=True,
                                      source=source)
    entry.created = created
    vault.upsert(entry)
    return entry_id


def _oplog(vault: Vault, ts: str, op: str, detail: str, **extra) -> None:
    vault.append_oplog({"ts": ts, "op": op, "entry_id": None, "snapshot_id": None,
                        "detail": detail, "changed": [], **extra})


def _history(tmp_path):
    """Main Save in slot 9, backed up three times as it was played, then rolled back to its
    first backup by the app and played on from there - a branch. Slot 3 was cleared by the
    app and the cloud put the save back."""
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    b1 = _entry(vault, "full", "20260101-100000", "2026-01-01T10:00:00", {9: "v1", 3: "other"})
    _entry(vault, "full", "20260101-110000", "2026-01-01T11:00:00", {9: "v2", 3: "other"})
    _entry(vault, "full", "20260101-120000", "2026-01-01T12:00:00", {9: "v3", 3: "other"})
    snap = _entry(vault, "snapshot", "20260101-130000", "2026-01-01T13:00:00",
                  {9: "v3", 3: "other"})
    _oplog(vault, "2026-01-01T13:00:00", "repopulate_slot", f"{b1} slot 9 -> slot 9",
           snapshot_id=snap)
    _entry(vault, "snapshot", "20260101-140000", "2026-01-01T14:00:00", {9: "v1", 3: "other"})
    _oplog(vault, "2026-01-01T14:00:00", "clear_slot", "slot 3")
    _entry(vault, "full", "20260101-150000", "2026-01-01T15:00:00", {9: "v4", 3: "other"})
    _oplog(vault, "2026-01-01T15:30:00", "full_backup", "")              # places nothing
    live = tmp_path / "NMS" / ACCOUNT
    _put(live, 9, "v4")
    _put(live, 3, "other")
    return vault, live


def _run(vault, live, *, now: int = 1_800_000_000, **kw):
    store = OrganizerStore(vault.root, create=False)
    source = Source(id="s", platform="steam", account=ACCOUNT, path=str(live))
    report = bootstrap(vault, store, [source], ScanCache(None, None),
                       game_running=kw.pop("game_running", lambda: False), clock=lambda: now,
                       **kw)
    return store, report


def test_the_owners_shape_seen_in_backups_and_put_live_by_the_app(tmp_path):
    vault, live = _history(tmp_path)
    store, report = _run(vault, live)
    v = {k: _vid(k, tmp_path) for k in VERSIONS}
    state = Journal(store, {}).replay()
    edges = {(e.parent, e.child): (e.tier, e.method, e.kind) for e in state.edges}
    assert edges == {
        (v["v1"], v["v2"]): ("observed", "capture-sequence", "play"),
        (v["v2"], v["v3"]): ("observed", "capture-sequence", "play"),
        (v["v1"], v["v4"]): ("recorded", "oplog", "play"),        # played on after the rollback
    }
    assert report.edges_by_method == {"capture-sequence": 2, "oplog": 1}
    assert report.ops_resolved == 2 and report.ops_unresolved == ()
    assert report.forks == (("Main", "1970-01-01"),)            # v1: two children
    reverted = [e for e in state.events if e.kind == "reverted"]
    assert [(e.target, e.op_id) for e in reverted] == [(T3, "legacy-2")]   # the cloud
    assert state.ops[("legacy-1", T9)].outcome == "confirmed"
    assert (report.versions, report.lines, report.legacy_lines) == (5, 1, 0)

    records = store.journal_records()
    assert {r["src"] for r in records} == {"backfill", "oplog"}  # the live state was seen already
    ops = [r for r in records if r["t"] == "op"]
    assert [(r["op_id"], r["kind"], r["target"]["slot"]) for r in ops] == [
        ("legacy-1", "repopulate", 9), ("legacy-2", "clear", 3)]
    assert ops[0]["evicted"] == [v["v3"], None] and ops[1]["pair"] == [None, None]
    assert report.journal_records == len(records)
    block = store.document(LINEAGE)["bootstrap"]
    assert block["done_at"] == 1_800_000_000 and block["algo"] == 1


def test_a_dry_run_writes_nothing_and_reports_the_same(tmp_path):
    vault, live = _history(tmp_path)
    _, dry = _run(vault, live, dry_run=True)
    assert not (vault.root / "organizer").exists()
    _, real = _run(vault, live)
    assert dry.dry_run and not real.dry_run
    assert (dry.edges_by_method, dry.events, dry.forks, dry.journal_records) == (
        real.edges_by_method, real.events, real.forks, real.journal_records)


def test_a_second_run_adds_only_what_is_new(tmp_path):
    vault, live = _history(tmp_path)
    store, first = _run(vault, live)
    before = store.journal_records()
    _, again = _run(vault, live)
    assert store.journal_records() == before and again.journal_records == 0
    # Old backups imported later (made before the first run) are backfilled then, in place.
    _entry(vault, "full", "20260101-103000", "2026-01-01T10:30:00", {9: "v1", 3: "other"})
    late = _entry(vault, "full", "20260101-113000", "2026-01-01T11:30:00",
                  {9: "v25", 3: "other"})
    _, third = _run(vault, live, now=1_900_000_000)
    added = store.journal_records()[len(before):]
    assert third.journal_records == len(added) == 1      # the 10:30 copy changed nothing
    assert added[0]["entry"] == late
    v = {k: _vid(k, tmp_path) for k in ("v2", "v25", "v3")}
    chain = {(e.parent, e.child) for e in Journal(store, {}).replay().edges}
    assert {(v["v2"], v["v25"]), (v["v25"], v["v3"])} <= chain
    assert (v["v2"], v["v3"]) not in chain
    assert store.document(LINEAGE)["bootstrap"]["done_at"] == 1_800_000_000


def test_backups_and_log_lines_after_recording_began_are_left_to_the_journal(tmp_path):
    vault, live = _history(tmp_path)
    store = OrganizerStore(vault.root, create=True)
    from datetime import datetime, timezone
    start = int(datetime.fromisoformat("2026-01-01T12:30:00").astimezone(timezone.utc).timestamp())
    with store.transaction() as tx:     # the live journal began at 12:30
        tx.append(obs_fields("scan", LiveTarget("steam", ACCOUNT, 1), (None, None), {}, {}),
                  at=start)
    _, report = _run(vault, live)
    records = store.journal_records()
    assert not [r for r in records if r["t"] == "op"]                 # 13:00 and 14:00 lines
    entries = {r["entry"] for r in records if r["src"] == "backfill"}
    assert entries and all(e < "full-steam-20260101-123000" for e in entries
                           if e.startswith("full")) and not any(
        e.startswith("snapshot") for e in entries)
    assert report.ops_resolved == 0


def test_lines_and_backups_that_cannot_be_placed_are_reported(tmp_path):
    vault, live = _history(tmp_path)
    _oplog(vault, "2026-01-01T09:00:00", "repopulate_slot", "gone-folder slot 2 -> slot 4",
           target=str(live))                         # names its folder: only the source fails
    _oplog(vault, "2026-01-01T09:00:01", "clear_slot", "slot banana")
    _entry(vault, "full", "20251231-100000", "2025-12-31T10:00:00", {9: "v1"},
           source="st_76561198000000009")
    second = tmp_path / "NMS" / "st_76561198000000002"
    _put(second, 1, "v1")
    store = OrganizerStore(vault.root, create=False)
    sources = [Source(id="s", platform="steam", account=ACCOUNT, path=str(live)),
               Source(id="t", platform="steam", account="b", path=str(second))]
    report = bootstrap(vault, store, sources, ScanCache(None, None), game_running=lambda: False)
    reasons = dict(report.ops_unresolved)
    assert "0 vault entries match the folder it copied from" in reasons[
        "line 4: repopulate_slot"]
    assert reasons["line 5: clear_slot"] == "detail not understood"
    # With two Steam live folders a line that names none cannot be placed either.
    assert "2 steam live folders" in reasons["line 1: repopulate_slot"]
    skipped = dict(report.entries_skipped)
    assert skipped["full-steam-20251231-100000"].startswith("entry-unmapped")


def test_an_only_live_folder_takes_a_backup_of_another_name_with_a_note(tmp_path):
    vault, live = _history(tmp_path)
    _entry(vault, "full", "20251231-100000", "2025-12-31T10:00:00", {9: "v1"},
           source="st_76561198000000009")
    _, report = _run(vault, live)
    assert "mapped to the only steam live folder" in dict(report.entries_skipped)[
        "full-steam-20251231-100000"]


def test_at_risk_live_slots_are_counted(tmp_path):
    vault, live = _history(tmp_path)
    steam_save(live, 5, 0, "New", 10, 0xABC, 7000, extra="live only")
    _, report = _run(vault, live)
    assert report.at_risk_live == 1


def test_a_full_restore_binds_every_slot_and_an_old_undo_is_one_operation(tmp_path):
    vault, live = _history(tmp_path)
    b2 = "full-steam-20260101-110000"
    _oplog(vault, "2026-01-01T15:10:00", "restore_full", "", entry_id=b2)
    # The undo format before undo_of: a restore_full of the snapshot, then an undo naming it.
    snap = "snapshot-steam-20260101-140000"
    _oplog(vault, "2026-01-01T15:20:00", "restore_full", "", entry_id=snap)
    _oplog(vault, "2026-01-01T15:20:00", "undo", "", snapshot_id=snap)
    store, report = _run(vault, live)
    ops = [r for r in store.journal_records() if r["t"] == "op"]
    by_line = {}
    for r in ops:
        by_line.setdefault(r["op_id"], []).append(r)
    assert sorted(r["target"]["slot"] for r in by_line["legacy-4"]) == list(range(1, 16))
    assert {r["kind"] for r in by_line["legacy-4"]} == {"restore_full"}
    assert {r["kind"] for r in by_line["legacy-5"]} == {"undo"} and "legacy-6" not in by_line
    assert report.ops_resolved == 4 and report.ops_unresolved == ()


def test_hand_made_copies_are_not_timeline_evidence(tmp_path):
    """An owner's copy of unknown time is a place a version is kept, not an observation."""
    vault, live = _history(tmp_path)
    copy = tmp_path / "my copy"
    _put(copy, 9, "v25")
    entry = vault.make_entry_from_dir("inplace-20260101-113000", "inplace", "my copy", copy,
                                      managed=False, source=ACCOUNT)
    entry.created = "2026-01-01T11:30:00"
    vault.upsert(entry)
    store, _ = _run(vault, live)
    assert "inplace-20260101-113000" not in {r.get("entry") for r in store.journal_records()}
    v25 = _vid("v25", tmp_path)
    assert all(v25 not in (e.parent, e.child) for e in Journal(store, {}).replay().edges)



def test_the_live_slots_are_journaled_as_they_are_when_the_bootstrap_ends(tmp_path, monkeypatch):
    """The game may save while the deep scan runs: the final look is a fresh one."""
    from nms_save_vault.core.organizer import bootstrap as boot
    vault, live = _history(tmp_path)
    real_scan = boot.scan
    calls = []

    def scan_then_play(*args, **kwargs):
        result = real_scan(*args, **kwargs)
        if not calls:                               # the deep scan: the game saves after it
            _put(live, 9, "v25")
        calls.append(1)
        return result

    monkeypatch.setattr(boot, "scan", scan_then_play)
    store, _ = _run(vault, live)
    last9 = [r for r in store.journal_records() if r["target"]["slot"] == 9][-1]
    assert last9["src"] == "scan" and last9["pair"][0] == _vid("v25", tmp_path)


def test_done_at_is_the_cutoff_the_run_used(tmp_path):
    from datetime import datetime, timezone
    vault, live = _history(tmp_path)
    store = OrganizerStore(vault.root, create=True)
    start = int(datetime.fromisoformat("2026-01-01T12:30:00").astimezone(timezone.utc).timestamp())
    with store.transaction() as tx:
        tx.append(obs_fields("scan", LiveTarget("steam", ACCOUNT, 1), (None, None), {}, {}),
                  at=start)
    _run(vault, live, now=1_900_000_000)
    assert store.document(LINEAGE)["bootstrap"]["done_at"] == start


def test_a_write_the_hook_recorded_is_not_imported_again(tmp_path):
    """Matched by what it did - slot, saves, time - not by its id."""
    from datetime import datetime, timezone
    from nms_save_vault.core.organizer.journal import op_fields
    vault, live = _history(tmp_path)
    when = int(datetime.fromisoformat("2026-01-01T13:00:00").astimezone(timezone.utc).timestamp())
    store = OrganizerStore(vault.root, create=True)
    with store.transaction() as tx:     # the hook's record, a minute after the log line
        tx.append(op_fields("app", "op-20260101T130100Z-abcd", "repopulate", T9,
                            (_vid("v1", tmp_path), None), None), at=when + 60)
    store2, _ = _run(vault, live)
    repopulates = [r for r in store2.journal_records()
                   if r["t"] == "op" and r["kind"] == "repopulate"]
    assert [r["src"] for r in repopulates] == ["app"]


def test_restoring_a_one_slot_extract_whole_emptied_the_other_slots(tmp_path):
    """Old versions let a one-slot extract be restored as a full backup: the mirror emptied
    every other slot. The operation says so, so the empty slots that follow are expected
    and not 'deleted outside the app'."""
    vault, live = _history(tmp_path)
    extract = "extract-steam-20260101-133000"
    folder = vault.extracts_dir / extract
    _put(folder, 9, "v3")
    entry = vault.make_entry_from_dir(extract, "extract", extract, folder, managed=True,
                                      source=ACCOUNT, slots=[9])
    entry.created = "2026-01-01T13:30:00"
    vault.upsert(entry)
    _oplog(vault, "2026-01-01T13:40:00", "restore_full", "", entry_id=extract)
    _entry(vault, "snapshot", "20260101-134500", "2026-01-01T13:45:00", {9: "v3"})
    store, report = _run(vault, live)
    ops = [r for r in store.journal_records() if r["t"] == "op" and r["kind"] == "restore_full"]
    assert sorted(r["target"]["slot"] for r in ops) == list(range(1, 16))
    assert next(r for r in ops if r["target"]["slot"] == 3)["pair"] == [None, None]
    assert "deleted" not in report.events


# --- chunk C review -----------------------------------------------------------


def test_the_game_is_asked_again_before_the_live_slots_are_journaled(tmp_path):
    """The game started during the deep scan: the final look journals nothing."""
    vault, live = _history(tmp_path)
    answers = iter([False, True])
    store, _ = _run(vault, live, game_running=lambda: next(answers))
    assert next(answers, "asked twice") == "asked twice"
    assert [r for r in store.journal_records() if r["src"] == "scan"] == []
    assert any(r["src"] == "backfill" for r in store.journal_records())


def test_reasons_never_name_an_account_folder(tmp_path):
    """A live folder's name carries the account id: no reason repeats it."""
    vault, live = _history(tmp_path)
    _entry(vault, "full", "20251231-100000", "2025-12-31T10:00:00", {9: "v1"},
           source="st_76561198000000009")
    _oplog(vault, "2026-01-01T09:00:00", "repopulate_slot",
           "st_76561198000000002 slot 2 -> slot 4")
    _, report = _run(vault, live)
    reasons = [r for _, r in report.entries_skipped + report.ops_unresolved]
    assert len(reasons) == 2
    assert not any("7656119800000000" in r for r in reasons)
