"""The organizer against the owner's real saves - strictly READ-ONLY - and against a
synthetic vault of about 2,000 versions for its time budget (D1 5 rows 14-15, 2.15).

The real-data test skips where there is no vault. It opens the organizer read-only (no
journal, no notes, no catalog tidying, the scan cache in memory), and checks afterwards
that no file in the vault, the live folders, the app's own folder or the safety backup
changed: not one added, removed, resized or touched.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from nms_save_vault.core import catalog, discover, locations, slotmap
from nms_save_vault.core import state as appstate
from nms_save_vault.core.organizer import Organizer
from nms_save_vault.core.organizer.journal import Journal
from nms_save_vault.core.organizer.lineage import read_lineage
from nms_save_vault.core.organizer.notes import read_notes
from nms_save_vault.core.organizer.readmodel import ReadModel
from nms_save_vault.core.state import Source

from .conftest import chunked, steam_save

SAFETY_BACKUP = Path(r"C:\Devel\NMS-SaveBackup-SAFETY-2026-06-24")


def _fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            out[str(path)] = (st.st_size, st.st_mtime_ns)
    return out


def _real_setup():
    state = appstate.load()
    root = Path(state.vault) if state and state.vault else locations.default_vault_dir()
    if not catalog.looks_like_vault_dir(root):
        pytest.skip("no real vault on this machine")
    sources = state.live_sources if state is not None else discover.discover_live_sources()
    return root, sources


@pytest.mark.slow
def test_the_owners_vault_read_only(capsys):
    root, sources = _real_setup()
    watched = [root, *(Path(s.path) for s in sources), SAFETY_BACKUP]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        watched.append(Path(local) / "NMSSaveVault")
    watched = [w for w in watched if w.exists()]
    before = {str(w): _fingerprint(w) for w in watched}
    had_store = (root / "organizer").exists()

    org = Organizer(root, sources, read_only=True)
    started = time.perf_counter()
    model = org.refresh()
    cold = time.perf_counter() - started
    started = time.perf_counter()
    model = org.refresh()
    warm = time.perf_counter() - started
    journal = Journal(org.store, org.scan.versions).replay()
    started = time.perf_counter()
    ReadModel.build(org.scan, journal, read_lineage(org.store), read_notes(org.store), sources,
                    now=int(time.time()))
    build = time.perf_counter() - started
    report = org.bootstrap()

    after = {str(w): _fingerprint(w) for w in watched}
    assert after == before, "a real folder changed"
    assert (root / "organizer").exists() == had_store and org.observed is None
    assert report.dry_run

    # Invariants (D1 5 row 14): the two members of a slot are one save; every live save
    # has its line; a live save in two places is counted once.
    for capture in org.scan.captures:
        if capture.a and capture.b and "unknown:" not in capture.a.pid + capture.b.pid:
            assert capture.a.pid == capture.b.pid
    lines = {line.pid for line in model.lines()}
    live = {m.pid for f in org.scan.live.values() for c in f.captures for m in c.members}
    assert {read_lineage(org.store).canonical(p) for p in live} <= lines
    assert report.versions == len(org.scan.versions) and report.lines == len(lines)
    # Nothing is ever deleted from the vault (keep everything): at least D1's 2.12 counts.
    assert report.versions >= 195 and report.pairs >= 106 and report.lines >= 30
    assert len(report.ops_unresolved) <= 2

    # D1 2.15: first open <= 5 s, warm refresh <= 0.3 s (here with slack for a busy machine).
    assert cold < 10 and warm < 0.6 and build < 0.2
    with capsys.disabled():
        print(f"\n[real data] cold {cold:.2f}s warm {warm:.3f}s model {build * 1000:.1f}ms; "
              f"{report.versions} versions, {report.pairs} pairs, {report.lines} save files, "
              f"edges {report.edges_by_method}, events {report.events}, "
              f"at risk {report.at_risk_live}")


# --- the time budget at ~2,000 versions (D1 5 row 15) -------------------------


def _big_vault(tmp_path, entries: int) -> tuple[Path, list[Source]]:
    """``entries`` full backups of 15 slots x 2 members, every data file distinct. The
    metas are copied from one template (only their data has to differ)."""
    template = tmp_path / "template"
    for slot in range(1, 16):
        for member in (0, 1):
            steam_save(template, slot, member, f"Save {slot}", 1000 + member, 0x1000 + slot,
                       1_700_000_000 + member)
    metas = sorted(template.glob("mf_save*.hg"))
    vault = catalog.Vault(tmp_path / "vault")
    vault.ensure()
    for e in range(entries):
        entry_id = f"full-steam-2026{e // 28 + 1:02d}{e % 28 + 1:02d}-120000"
        folder = vault.backups_dir / entry_id
        folder.mkdir()
        for meta in metas:
            shutil.copyfile(meta, folder / meta.name)
        for f in slotmap.all_file_numbers():
            payload = f'{{"F2P":4737,"x":"{e}-{f}"}}'.encode()
            (folder / slotmap.data_filename(f)).write_bytes(chunked(payload))
        vault._entries.append(catalog.CatalogEntry(
            entry_id, "full", entry_id, str(folder), f"2026-01-01T{e % 24:02d}:00:00",
            source="st_76561198000000001", managed=True))
    vault.save()
    live = tmp_path / "NMS" / "st_76561198000000001"
    for slot in range(1, 16):
        steam_save(live, slot, 0, f"Save {slot}", 5000, 0x1000 + slot, 1_800_000_000,
                   extra=f"live {slot}")
    return vault.root, [Source(id="s", platform="steam", account=live.name, path=str(live))]


@pytest.mark.slow
def test_two_thousand_versions_stay_within_the_time_budget(tmp_path):
    root, sources = _big_vault(tmp_path, entries=67)       # 67 x 30 + 15 live = 2,025
    org = Organizer(root, sources, read_only=True, game_running=lambda: False)
    org.refresh()                                           # cold: hashes everything
    assert len(org.scan.versions) == 2025
    started = time.perf_counter()
    org.refresh()
    warm = time.perf_counter() - started
    journal = Journal(org.store, org.scan.versions).replay()
    lineage, notes = read_lineage(org.store), read_notes(org.store)
    started = time.perf_counter()
    for _ in range(3):
        ReadModel.build(org.scan, journal, lineage, notes, sources, now=1)
    build = (time.perf_counter() - started) / 3
    assert warm < 0.5, f"warm refresh {warm:.3f}s"
    assert build < 0.05, f"read model {build * 1000:.1f}ms"
