"""The organizer's front: the Organizer facade, the folder locator and the CLI commands
saves, versions, board, note, pin, scan and organizer bootstrap (D1 2.11, 2.13)."""
from __future__ import annotations

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
    others = {k: v for k, v in _fingerprint(vault.root).items() if "journal" not in k}
    code, out = _run(capsys, "scan")
    assert code == 0 and "Journaled 1 live slot(s)." in out and "S9" in out
    after = {k: v for k, v in _fingerprint(vault.root).items() if "journal" not in k}
    assert after == others and journal.is_file()           # only the journal was written


def test_organizer_bootstrap_from_the_cli(world, capsys):
    vault, _, _ = world
    code, out = _run(capsys, "organizer", "bootstrap", "--dry-run")
    assert code == 0 and out.startswith("Would build the history of 2 save files")
    assert not (vault.root / "organizer" / "lineage.json").exists()
    code, out = _run(capsys, "organizer", "bootstrap")
    assert code == 0 and out.startswith("Built the history")
    assert (vault.root / "organizer" / "lineage.json").is_file()
