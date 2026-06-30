"""Discovery + state: live-vs-backup classification, multi-account, and round-trip."""
from __future__ import annotations

import sys
from pathlib import Path

from nms_save_vault.core import discover, state
from nms_save_vault.core.formats import ACCOUNT_META_NAME

ACCT_A = "76561197975032661"
ACCT_B = "76561198000000001"


def test_state_path_is_next_to_frozen_exe(tmp_path, monkeypatch):
    """When frozen (packaged exe), state.json sits beside the executable."""
    exe = tmp_path / "Programs" / "NMSSaveVault" / "NMSSaveVault.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)
    assert state.install_dir() == exe.parent
    assert state.default_state_path() == exe.parent / "state.json"


def test_state_path_from_source_uses_localappdata(tmp_path, monkeypatch):
    """From source (not frozen), config stays out of the tree, in %LOCALAPPDATA%."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert state.default_state_path() == tmp_path / "Local" / "NMSSaveVault" / "state.json"


def _make_steam_dir(parent: Path, name: str) -> Path:
    """A folder that ``looks_like_save_dir`` (has a save file + account meta)."""
    d = parent / name
    d.mkdir(parents=True)
    (d / "mf_save.hg").write_bytes(b"\xe5\xa1\xed\xfe")  # save streaming header-ish
    (d / ACCOUNT_META_NAME).write_bytes(b"\x00" * 16)
    return d


def _make_nms_root(tmp_path: Path) -> Path:
    root = tmp_path / "HelloGames" / "NMS"
    root.mkdir(parents=True)
    return root


def test_canonical_steam_dir_is_live(tmp_path):
    root = _make_nms_root(tmp_path)
    live = _make_steam_dir(root, f"st_{ACCT_A}")
    assert discover.is_canonical_steam_live(live, root)
    assert discover.steam_account_id(live) == ACCT_A


def test_copy_is_not_live(tmp_path):
    root = _make_nms_root(tmp_path)
    copy = _make_steam_dir(root, f"st_{ACCT_A} - Copy")
    assert not discover.is_canonical_steam_live(copy, root)
    # but the embedded account id is still recoverable
    assert discover.steam_account_id(copy) == ACCT_A


def test_two_accounts_yield_two_live_sources(tmp_path):
    root = _make_nms_root(tmp_path)
    _make_steam_dir(root, f"st_{ACCT_A}")
    _make_steam_dir(root, f"st_{ACCT_B}")
    sources = discover.discover_live_sources(root, ms_dirs=[])
    accounts = {s.account for s in sources}
    assert accounts == {ACCT_A, ACCT_B}
    assert all(s.role == state.ROLE_LIVE and s.writable for s in sources)
    assert len({s.id for s in sources}) == 2  # ids are stable + distinct


def test_inplace_backups_exclude_live_and_vault(tmp_path):
    root = _make_nms_root(tmp_path)
    live = _make_steam_dir(root, f"st_{ACCT_A}")
    backup = _make_steam_dir(root, f"st_{ACCT_A} - Copy")
    renamed = _make_steam_dir(root, "MyOldBackup")
    vault = root / "_SaveVault"
    _make_steam_dir(vault, "backups")  # something save-like inside the vault

    found = discover.discover_inplace_backups(root, exclude=[live, vault])
    found_names = {p.name for p in found}
    assert backup.name in found_names
    assert renamed.name in found_names
    assert f"st_{ACCT_A}" not in found_names  # the live dir is not a backup
    # nothing from inside the excluded vault leaks in
    assert all(vault not in p.resolve().parents for p in found)


def test_xbox_source_is_writable(tmp_path):
    root = _make_nms_root(tmp_path)
    xbox = tmp_path / "wgs" / "000901F"
    xbox.mkdir(parents=True)
    (xbox / "containers.index").write_bytes(b"\x0e\x00\x00\x00")
    sources = discover.discover_live_sources(root, ms_dirs=[xbox])
    assert len(sources) == 1
    s = sources[0]
    assert s.platform == state.PLATFORM_XBOX
    assert s.writable is True   # same-platform Xbox writes are supported
    assert s.role == state.ROLE_LIVE


def test_state_round_trip(tmp_path):
    st = discover.bootstrap_state(_make_nms_root(tmp_path), ms_dirs=[])
    st.upsert(
        state.Source(
            id="steam-manual",
            platform=state.PLATFORM_STEAM,
            account=ACCT_B,
            path=str(tmp_path / "manual"),
            label="hand added",
            origin=state.ORIGIN_MANUAL,
        )
    )
    p = tmp_path / "state.json"
    state.save(st, p)
    loaded = state.load(p)
    assert loaded is not None
    assert loaded.get("steam-manual").origin == state.ORIGIN_MANUAL
    assert loaded.vault == st.vault


def test_merge_preserves_manual_and_adds_new(tmp_path):
    root = _make_nms_root(tmp_path)
    _make_steam_dir(root, f"st_{ACCT_A}")
    st = discover.bootstrap_state(root, ms_dirs=[])
    assert len(st.live_sources) == 1

    manual = state.Source(
        id="steam-manual", platform=state.PLATFORM_STEAM, account="x",
        path=str(tmp_path / "m"), origin=state.ORIGIN_MANUAL,
    )
    st.upsert(manual)

    # a second account appears on a later scan
    _make_steam_dir(root, f"st_{ACCT_B}")
    added = discover.merge_live_sources(st, root, ms_dirs=[])
    assert added == 1
    assert st.get("steam-manual").origin == state.ORIGIN_MANUAL  # untouched
    assert {s.account for s in st.sources if s.origin == state.ORIGIN_AUTO} == {ACCT_A, ACCT_B}
