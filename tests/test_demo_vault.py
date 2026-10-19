"""tools/make_demo_vault.py builds a believable fake world and refuses real folders."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from nms_save_vault.core import aliases, discover
from nms_save_vault.core.organizer import Organizer

from tools import make_demo_vault as demo_tool


@pytest.fixture
def short_dir():
    """The wgs folders are deep: pytest's own tmp_path would pass Windows' 260 characters."""
    folder = Path(tempfile.mkdtemp(prefix="nd"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.slow
def test_the_demo_world_has_its_saves_branch_and_notes(short_dir):
    demo = demo_tool.build(short_dir / "w")

    sources = discover.discover_live_sources(demo.target / "Roaming" / "HelloGames" / "NMS", [demo.xbox])
    model = Organizer(demo.vault, sources, read_only=True, game_running=lambda: False).refresh(record=False)
    lines = {line.name: line for line in model.lines()}

    assert set(lines) == {s.name for s in demo_tool.SPECS.values()}
    assert lines["Main Save"].pinned and lines["Main Save"].branch_count >= 1
    assert lines["Main Save"].has_note and lines["Permadeath Run"].has_note
    assert len(lines["Atlas Path"].platforms) == 2                    # Steam and Xbox
    assert lines["First Playthrough"].live == ()                      # only in the vault
    assert [v.label for v in model.versions(lines["Main Save"].pid) if v.label] == ["Before the base experiment"]
    assert not model.problems
    names = set(aliases.load(demo.target / "app" / "accounts.ini").entries.values())
    assert names == {"Main PC", "Living room"}                         # no account id on screen


def test_the_demo_tool_refuses_real_folders(tmp_path, monkeypatch):
    monkeypatch.setitem(demo_tool.REAL_ENV, "APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setitem(demo_tool.REAL_ENV, "LOCALAPPDATA", str(tmp_path / "Local"))
    for bad in (tmp_path / "Roaming" / "HelloGames" / "NMS", tmp_path / "Roaming",
                tmp_path / "Local" / "NMSSaveVault" / "x", tmp_path / "Local" / "Packages"
                / demo_tool.PACKAGE, "D:/VaultCopy/demo"):
        with pytest.raises(SystemExit):
            demo_tool.check_target(bad)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "keep.txt").write_text("x", encoding="ascii")
    with pytest.raises(SystemExit):                                   # not empty, not a demo world
        demo_tool.check_target(tmp_path / "other")
    assert demo_tool.check_target(tmp_path / "new") == tmp_path / "new"


def _board_slot(demo, slot: int):
    sources = discover.discover_live_sources(demo.target / "Roaming" / "HelloGames" / "NMS", [demo.xbox])
    model = Organizer(demo.vault, sources, read_only=True, game_running=lambda: False).refresh(record=False)
    steam = next(s for s in model.live_board().sources if s.label.lower().startswith("steam"))
    return next(s for s in steam.slots if s.label == f"S{slot}")


@pytest.mark.slow
def test_played_leaves_pirate_life_with_no_backup(short_dir):
    lines = []
    plain = demo_tool.build(short_dir / "a")
    played = demo_tool.build(short_dir / "b", played=True, log=lines.append)

    assert _board_slot(plain, 6).vault_copy == "durable"
    slot = _board_slot(played, 6)
    assert slot.vault_copy == "none" and slot.play_time == 40 * 3600
    assert any(line.startswith("--played:") for line in lines)


@pytest.mark.slow
def test_old_vault_is_the_old_layout_with_dated_backups_and_combines_with_played(short_dir):
    from nms_save_vault.core import store as vstore

    lines = []
    demo = demo_tool.build(short_dir / "o", old_vault=True, played=True, log=lines.append)

    assert vstore.vault_mode(demo.vault) == "legacy" and not (demo.vault / "store").exists()
    assert (demo.vault / "catalog.json").is_file()
    folders = sorted(p.name for p in (demo.vault / "backups").iterdir())
    assert len(folders) >= 8 and all(name.startswith("full-") for name in folders)
    assert folders[0].split("-")[-2] == "20260906" or "202609" in folders[0]       # the simulated dates
    assert any(line.startswith("--old-vault:") for line in lines)
    assert _board_slot(demo, 6).vault_copy == "none"
