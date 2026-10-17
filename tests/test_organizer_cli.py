"""The organizer's front: the Organizer facade, the folder locator and the CLI commands
saves, versions, board, note, pin, scan and organizer bootstrap (D1 2.11, 2.13)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from nms_save_vault import cli
from nms_save_vault.core import operations, safety, slotmap
from nms_save_vault.core import state as appstate
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer import Organizer
from nms_save_vault.core.organizer.cache import ScanCache
from nms_save_vault.core.organizer.folders import FolderLocator
from nms_save_vault.core.organizer.locator import (
    LiveMember,
    PreserveFailed,
    VaultLocator,
    VersionUnavailable,
)
from nms_save_vault.core.organizer.model import EntryRef, LiveRef
from nms_save_vault.core.organizer.readmodel import RefError, resolve_save, resolve_version
from nms_save_vault.core.state import AppState, Source

from .conftest import steam_save

ACCOUNT = "st_76561198000000001"
MAIN, OTHER = 0xDDDAD1BDD39F9C72, 0x9E8385F0AB12CD34
PID = "DDDAD1BDD39F9C72"
T0 = 1_790_000_000


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """The per-machine cache under tmp_path, the game known not to run."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    # Organizer binds its default probe at definition time, so patching safety is not enough.
    real_init = Organizer.__init__
    monkeypatch.setattr(Organizer, "__init__", lambda self, *a, **k: real_init(
        self, *a, **{"game_running": lambda: False, **k}))


@pytest.fixture
def world(tmp_path, monkeypatch):
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    live = tmp_path / "NMS" / ACCOUNT
    steam_save(live, 9, 0, "Main Save", 7200, MAIN, T0, extra="v1")
    steam_save(live, 9, 1, "Main Save", 7100, MAIN, T0 - 10, extra="v0")
    steam_save(live, 3, 0, "Other Run", 100, OTHER, T0 - 500, extra="o1")
    operations.create_full_backup(vault, live, warnings=[])
    state = AppState(sources=[Source(id="steam", platform="steam", account=ACCOUNT,
                                     path=str(live), label="Steam (76561198000000001)")],
                     vault=str(vault.root))
    monkeypatch.setattr(appstate, "load", lambda path=None: state)
    return vault, live, state


def _run(capsys, *argv) -> tuple[int, str]:
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def _fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


# --- the facade and the locator -----------------------------------------------


def test_the_organizer_scans_records_and_builds(world):
    vault, live, state = world
    org = Organizer(vault.root, state.live_sources)
    model = org.refresh()
    assert org.observed.written == 0                 # the backup journaled this state already
    assert {line.name for line in model.lines()} == {"Main Save", "Other Run"}
    assert org.needs_bootstrap
    report = org.bootstrap()
    assert report.versions == 3 and not Organizer(vault.root, state.live_sources).needs_bootstrap


def test_the_locator_reads_verified_bytes(world):
    vault, live, state = world
    org = Organizer(vault.root, state.live_sources)
    model = org.refresh(record=False)
    row = model.versions(PID)[0]
    locations = org.locator.locate(row.head)
    assert [loc.kind for loc in locations] == ["entry", "live"]       # the vault copy first
    assert locations[0].durable and not locations[1].durable and org.locator.is_durable(row.head)
    opened = org.locator.open(row.head)
    assert hashlib.sha256(opened.data).hexdigest() == row.head
    assert opened.meta_plain[:4] == bytes.fromhex("beeeeeee")          # decrypted meta header
    assert opened.location == locations[0]
    live_copy = org.locator.open(row.head, prefer=locations[1])
    assert live_copy.location.kind == "live" and isinstance(live_copy.location.ref, LiveRef)
    assert locations[0].to_json() == {"kind": "entry", "entry": locations[0].ref.entry_id,
                                      "slot": 9, "member": "A"}
    # The backup changed under us: its bytes no longer match, so the live copy is read.
    backup = Path(Vault(vault.root).get(locations[0].ref.entry_id).path)
    (backup / slotmap.data_filename(slotmap.slot_file_numbers(9)[0])).write_bytes(b"x")
    assert org.locator.open(row.head).location.kind == "live"
    for f in live.glob("save*.hg"):
        f.write_bytes(b"y")
    with pytest.raises(VersionUnavailable):
        org.locator.open(row.head)
    with pytest.raises(PreserveFailed, match="store-unavailable"):
        org.locator.preserve([LiveMember(None, "A", row.head)], op_id="op-x", reason="test")
    assert VaultLocator([], None).locate(row.head) == []


def test_a_read_only_organizer_writes_nowhere(world, tmp_path):
    import os
    vault, live, state = world
    stale = vault.root / ".catalog.json.tmp_left.part"     # an interrupted save left this
    stale.write_bytes(b"{")
    os.utime(stale, (T0, T0))
    steam_save(live, 4, 0, "New", 5, 0xABC, T0 + 99, extra="new")   # something to journal
    before = _fingerprint(tmp_path)
    org = Organizer(vault.root, state.live_sources, read_only=True)
    model = org.refresh()
    assert model.lines() and org.observed is None
    report = org.bootstrap()
    assert report.dry_run
    assert _fingerprint(tmp_path) == before


# --- the CLI ------------------------------------------------------------------


def test_listings_write_nothing_in_the_vault(world, capsys):
    vault, live, _ = world
    steam_save(live, 9, 0, "Main Save", 7300, MAIN, T0 + 60, extra="played")   # unrecorded
    before = _fingerprint(vault.root)
    code, out = _run(capsys, "saves")
    assert code == 0 and "Main Save" in out and "S9" in out and "Other Run" in out
    for argv in (["versions", "Main Save", "--all"], ["board"], ["saves", "--json"]):
        assert _run(capsys, *argv)[0] == 0
    assert _fingerprint(vault.root) == before


def test_saves_sorts_filters_and_writes_json_without_account_keys(world, capsys):
    _, out = _run(capsys, "saves", "--sort", "play_time")
    assert out.index("Main Save") < out.index("Other Run")
    _, out = _run(capsys, "saves", "--find", "other")
    assert "Other Run" in out and "Main Save" not in out
    _, out = _run(capsys, "saves", "--json")
    data = json.loads(out)
    assert {line["name"] for line in data} == {"Main Save", "Other Run"}
    assert ACCOUNT not in out and "76561198000000001" not in out
    _, out = _run(capsys, "board", "--json")
    assert ACCOUNT not in out and json.loads(out)["sources"][0]["slots"][8]["label"] == "S9"
    assert "76561198000000001" not in out and json.loads(out)["sources"][0]["label"] == "Steam"
    assert "76561198000000001" not in _run(capsys, "board")[1]


def test_versions_and_references(world, capsys):
    vault, live, state = world
    _, out = _run(capsys, "versions", "S9", "--all")
    assert "Main Save: 1 versions" in out and "by date" in out
    model = Organizer(vault.root, state.live_sources).refresh(record=False)
    assert resolve_save(model, "main save") == PID == resolve_save(model, "dddad1")
    head = model.versions(PID)[0].head
    assert resolve_version(model, head[:8]).head == head == resolve_version(model, "S9").head
    assert resolve_version(model, "Main Save@2030-01-01").head == head
    with pytest.raises(RefError, match="no version saved"):
        resolve_version(model, "Main Save@2020-01-01")
    with pytest.raises(RefError, match="holds no save"):
        resolve_save(model, "S5")
    with pytest.raises(SystemExit) as exit_:
        cli.main(["versions", "Nobody"])
    assert "no save file" in str(exit_.value)


def test_an_ambiguous_reference_lists_the_candidates(world):
    vault, live, state = world
    steam_save(live, 4, 0, "Main Save", 10, 0xDDDAD10000000001, T0 - 900, extra="z")
    model = Organizer(vault.root, state.live_sources).refresh(record=False)
    with pytest.raises(RefError) as err:
        resolve_save(model, "Main Save")
    assert len(err.value.candidates) == 2
    with pytest.raises(RefError, match="could be several"):
        resolve_save(model, "dddad1")
    assert resolve_save(model, "DDDAD1B") == PID
    with pytest.raises(RefError, match="no save file"):
        resolve_save(model, "DDD")       # too short to be an id, and no save is called so


def test_note_and_pin(world, capsys):
    vault, _, _ = world
    assert _run(capsys, "note", "S9", "--set", "Main run.")[1].startswith("Note saved (revision 1)")
    _run(capsys, "note", "Main Save", "--append", "Next: Atlas path.")
    _, out = _run(capsys, "note", "S9")
    assert out == "Main run.\nNext: Atlas path.\n"
    _, out = _run(capsys, "note", "S9", "--history")
    assert "revision 1" in out and "revision 2 (now)" in out
    _, out = _run(capsys, "note", "S9", "--label", "before the Atlas", "--lock")
    assert "label 'before the Atlas', locked" in out
    _, out = _run(capsys, "pin", "S9")
    assert "pinned" in out
    _, out = _run(capsys, "saves")
    assert "*Main Save" in out and "Main run." in out
    _run(capsys, "pin", "S9", "--off")
    assert "*Main Save" not in _run(capsys, "saves")[1]


def test_scan_records_what_changed(world, capsys):
    vault, live, _ = world
    steam_save(live, 9, 0, "Main Save", 7300, MAIN, T0 + 60, extra="played")
    journal = vault.root / "organizer" / "journal.jsonl"
    mine = ("journal", ".liveop.lock")         # the record, and the lock it was made under
    others = {k: v for k, v in _fingerprint(vault.root).items() if not any(m in k for m in mine)}
    code, out = _run(capsys, "scan")
    assert code == 0 and "Journaled 1 live slot(s)." in out and "S9" in out
    after = {k: v for k, v in _fingerprint(vault.root).items() if not any(m in k for m in mine)}
    assert after == others and journal.is_file()           # only the journal was written


def test_organizer_bootstrap_from_the_cli(world, capsys):
    vault, _, _ = world
    code, out = _run(capsys, "organizer", "bootstrap", "--dry-run")
    assert code == 0 and out.startswith("Would build the history of 2 save files")
    assert not (vault.root / "organizer" / "lineage.json").exists()
    code, out = _run(capsys, "organizer", "bootstrap")
    assert code == 0 and out.startswith("Built the history")
    assert (vault.root / "organizer" / "lineage.json").is_file()


# --- chunk C review -----------------------------------------------------------


def _stale_temp(vault: Vault) -> Path:
    """What an interrupted catalog save leaves; a writable load deletes it."""
    import os
    stale = vault.root / ".catalog.json.tmp_left.part"
    stale.write_bytes(b"{")
    os.utime(stale, (T0, T0))
    return stale


def test_listings_open_the_vault_read_only(world, capsys, tmp_path):
    vault, _, _ = world
    stale = _stale_temp(vault)
    for argv in (["saves"], ["versions", "S9"], ["board"], ["note", "S9"],
                 ["note", "S9", "--history"], ["organizer", "bootstrap", "--dry-run"]):
        assert _run(capsys, *argv)[0] == 0
        assert stale.exists(), argv
    assert list((tmp_path / "Local").rglob("scan-cache.json"))      # the per-machine cache
    _run(capsys, "scan")                                           # a writer tidies
    assert not stale.exists()


def test_scan_says_why_it_cannot_record(world, capsys):
    vault, _, _ = world
    header = vault.root / "organizer" / "organizer.json"
    data = json.loads(header.read_text(encoding="ascii"))
    data["schema"] = 99
    header.write_text(json.dumps(data), encoding="ascii")
    with pytest.raises(SystemExit) as exit_:
        cli.main(["scan"])
    assert "nothing was recorded" in str(exit_.value) and "read-only" in str(exit_.value)


def test_bootstrap_reasons_show_no_account_folder(world, capsys):
    vault, _, _ = world
    other = "st_76561198000000009"
    folder = vault.backups_dir / "full-steam-20251231-100000"
    steam_save(folder, 9, 0, "Main Save", 10, MAIN, T0 - 9000, extra="old")
    entry = vault.make_entry_from_dir("full-steam-20251231-100000", "full", "", folder,
                                      managed=True, source=other)
    entry.created = "2025-12-31T10:00:00"
    vault.upsert(entry)
    vault.append_oplog({"ts": "2026-01-01T09:00:00", "op": "repopulate_slot", "entry_id": None,
                        "snapshot_id": None, "detail": "st_76561198000000002 slot 2 -> slot 4",
                        "changed": []})
    code, out = _run(capsys, "organizer", "bootstrap", "--dry-run")
    assert code == 0 and "backup full-steam-20251231-100000: taken of another" in out
    assert "not placed - line" in out
    assert "76561198000000009" not in out and "76561198000000002" not in out


def test_a_live_slot_whose_save_has_no_version_row_is_a_ref_error(world, monkeypatch):
    vault, _, state = world
    model = Organizer(vault.root, state.live_sources).refresh(record=False)
    monkeypatch.setattr(model, "versions", lambda pid: ())
    with pytest.raises(RefError, match="S9"):
        resolve_version(model, "S9")


def test_refresh_does_not_journal_while_a_live_write_holds_the_lock(world):
    import threading
    from nms_save_vault.core.organizer.store import liveop_lock_path
    vault, live, state = world
    steam_save(live, 9, 0, "Main Save", 7300, MAIN, T0 + 60, extra="played")
    journal = vault.root / "organizer" / "journal.jsonl"
    before = journal.read_bytes()
    held, release = threading.Event(), threading.Event()

    def writer():
        with safety.exclusive_lock(liveop_lock_path(vault.root), timeout=5):
            held.set()
            release.wait(30)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert held.wait(30)
        org = Organizer(vault.root, state.live_sources)
        org.refresh(record=True)
        assert org.observed.written == 0 and journal.read_bytes() == before
        assert {reason for _, reason in org.observed.skipped} == {"operation in progress"}
    finally:
        release.set()
        thread.join(30)
    org.refresh(record=True)
    assert org.observed.written == 1


def test_a_location_that_cannot_be_read_hands_over_to_the_next(world, monkeypatch):
    from nms_save_vault.core import savedir
    from nms_save_vault.core.organizer import folders
    vault, live, state = world
    org = Organizer(vault.root, state.live_sources)
    model = org.refresh(record=False)
    head = model.versions(PID)[0].head
    entry_loc, live_loc = org.locator.locate(head)
    backup = Path(Vault(vault.root).get(entry_loc.ref.entry_id).path)
    real_scan = savedir.scan_any

    def slot_gone(folder):                     # e.g. an Xbox index that lost the slot
        view = real_scan(folder)
        return dataclasses.replace(view, slots={}) if Path(folder) == backup else view

    monkeypatch.setattr(folders.savedir, "scan_any", slot_gone)
    assert org.locator.open(head).location == live_loc
    monkeypatch.setattr(folders.savedir, "scan_any", real_scan)
    real_decrypt = folders.metacrypt.decrypt
    monkeypatch.setattr(folders.metacrypt, "decrypt",
                        lambda blob, ordinal: real_decrypt(b"", ordinal))     # IndexError
    with pytest.raises(VersionUnavailable, match="entry|live"):
        org.locator.open(head)
    two = VaultLocator([FolderLocator(org.scan, {}), org.locator], None)
    monkeypatch.setattr(folders.metacrypt, "decrypt", real_decrypt)
    assert two.open(head).location == entry_loc


# --- chunk D review -----------------------------------------------------------


def test_refresh_scans_the_vault_unlocked_and_the_live_folders_under_the_lock(world,
                                                                              monkeypatch):
    """A live write waits for the lock: holding it over the whole vault scan would make it
    wait for seconds. Only the second look at the live folders and the record hold it."""
    import contextlib

    from nms_save_vault.core import organizer as org_mod

    vault, live, state = world
    held = []
    real_lock, real_scan = safety.exclusive_lock, org_mod.scan
    calls = []

    @contextlib.contextmanager
    def watched(path, **kw):
        with real_lock(path, **kw) as handle:
            held.append(True)
            try:
                yield handle
            finally:
                held.pop()

    def scan_then_play(vault_arg, *args, **kw):
        calls.append((vault_arg is not None, bool(held)))
        result = real_scan(vault_arg, *args, **kw)
        if len(calls) == 1:                         # the game saves after the vault scan
            steam_save(live, 9, 0, "Main Save", 7400, MAIN, T0 + 90, extra="later")
        return result

    monkeypatch.setattr(safety, "exclusive_lock", watched)
    monkeypatch.setattr(org_mod, "scan", scan_then_play)
    org = Organizer(vault.root, state.live_sources)
    model = org.refresh(record=True)
    assert calls == [(True, False), (False, True)]
    later = hashlib.sha256((live / slotmap.data_filename(
        slotmap.slot_file_numbers(9)[0])).read_bytes()).hexdigest()
    last9 = [r for r in org.store.journal_records() if r["target"]["slot"] == 9][-1]
    assert last9["pair"][0] == later                # what the second look saw is recorded
    assert model.versions(PID)[0].head == later     # and the model shows it


def test_a_lock_that_fails_is_not_reported_as_busy(world, monkeypatch):
    from nms_save_vault.core.organizer.journal import SKIP_LOCK_FAILED

    vault, live, state = world
    steam_save(live, 9, 0, "Main Save", 7300, MAIN, T0 + 60, extra="played")
    before = (vault.root / "organizer" / "journal.jsonl").read_bytes()

    def broken(path, **kw):
        raise PermissionError(13, "read-only vault")

    monkeypatch.setattr(safety, "exclusive_lock", broken)
    org = Organizer(vault.root, state.live_sources)
    org.refresh(record=True)
    assert {reason for _, reason in org.observed.skipped} == {SKIP_LOCK_FAILED}
    assert (vault.root / "organizer" / "journal.jsonl").read_bytes() == before


def test_recording_looks_at_each_live_folder_once(world, monkeypatch):
    from nms_save_vault.core.organizer import scanner

    vault, live, state = world
    looked = []
    real = scanner._Run.scan_live
    monkeypatch.setattr(scanner._Run, "scan_live",
                        lambda self, source, ref: looked.append(ref) or real(self, source, ref))
    org = Organizer(vault.root, state.live_sources)
    org.refresh(record=True)
    assert len(looked) == len(state.live_sources)
    looked.clear()
    org.refresh(record=False)
    assert len(looked) == len(state.live_sources)


def test_a_backup_made_since_the_last_refresh_is_seen(world):
    """The GUI keeps one Organizer: a classic backup in between must not be missed."""
    vault, live, state = world
    org = Organizer(vault.root, state.live_sources)
    first = org.refresh(record=False)
    before = set(org.scan.entries)
    steam_save(live, 4, 0, "New Run", 50, 0x4444, T0 + 300, extra="new")
    entry = operations.create_full_backup(Vault(vault.root), live, warnings=[])
    org.refresh(record=False)
    assert entry.id in org.scan.entries and entry.id not in before
    assert first is not org.model


# --- history and lineage (D1 2.13, phase 4) ------------------------------------


def _two_versions(world):
    """Main Save in slots 9 and 4: two versions that nothing in the record links."""
    vault, live, state = world
    steam_save(live, 4, 0, "Main Save", 9000, MAIN, T0 + 50, extra="later")
    return vault, live


def test_history_shows_nothing_it_was_not_told_and_writes_nothing(world, capsys):
    vault, _ = _two_versions(world)
    before = _fingerprint(vault.root)
    code, out = _run(capsys, "history", "Main Save")
    assert code == 0 and "no recorded history, by date:" in out and "*" not in out
    assert out.count("\n-") == 2 or out.count("-  ") >= 2
    for argv in (["history", "S9", "--key"], ["history", "S9", "--all"], ["history", "S9", "--json"]):
        assert _run(capsys, *argv)[0] == 0
    assert _fingerprint(vault.root) == before


def test_lineage_comes_from_draws_the_link_and_is_validated(world, capsys):
    _two_versions(world)
    _, out = _run(capsys, "lineage", "comes-from", "S4", "S9")
    assert "comes from" in out
    _, out = _run(capsys, "history", "Main Save")
    assert "set by you" in out and "tip" in out and "root" in out and "live S4" in out
    data = json.loads(_run(capsys, "history", "Main Save", "--json")[1])
    assert data["lanes"] == 1 and len(data["edges"]) == 1 and data["edges"][0]["tier"] == "owner"
    with pytest.raises(SystemExit) as err:
        cli.main(["lineage", "set-parent", "S9", "S4"])
    assert "comes after" in str(err.value)
    with pytest.raises(SystemExit) as err:
        cli.main(["lineage", "set-parent", "S4"])
    assert "--none" in str(err.value)
    _run(capsys, "lineage", "set-parent", "S4", "--root")
    assert "no recorded history" in _run(capsys, "history", "Main Save")[1]
    _run(capsys, "lineage", "comes-from", "S4", "S9")
    _run(capsys, "lineage", "unrelated", "S4", "S9")


def test_lineage_combine_and_split(world, capsys):
    _two_versions(world)
    _, out = _run(capsys, "lineage", "combine", "Other Run", "Main Save")
    assert "now part of Main Save" in out
    assert "Other Run" not in _run(capsys, "saves")[1]
    _run(capsys, "lineage", "uncombine", "9E8385F0")
    assert "Other Run" in _run(capsys, "saves")[1]
    _, out = _run(capsys, "lineage", "split", "S4", "--name", "second try")
    assert "1 version(s) are now the save file split:" in out
    assert "second try" in _run(capsys, "saves")[1]
    with pytest.raises(SystemExit):
        cli.main(["lineage", "unsplit", "Nobody"])


def test_lineage_list_undo_and_notes_about_what_was_left_out(world, capsys):
    _two_versions(world)
    _run(capsys, "lineage", "comes-from", "S4", "S9")
    _, out = _run(capsys, "lineage", "list")
    assert "comes from" in out
    cid = out.split()[0]
    assert "undone" in _run(capsys, "lineage", "undo", cid[:12])[1]
    assert "no recorded history" in _run(capsys, "history", "Main Save")[1]
    with pytest.raises(SystemExit):
        cli.main(["lineage", "undo", "cor-nope"])
    _run(capsys, "lineage", "comes-from", "S4", "S9")
    _, out = _run(capsys, "lineage", "unrelated", "S4", "S9")
    assert "taken back" in out
    # an unnamed split is named, and can be reached by its id
    _, out = _run(capsys, "lineage", "split", "S4")
    ref = out.split()[-1]
    assert "Main Save (split 1)" in _run(capsys, "saves")[1]
    assert "Main Save (split 1)" in _run(capsys, "history", ref)[1]
    assert "undone" in _run(capsys, "lineage", "unsplit", ref)[1]
