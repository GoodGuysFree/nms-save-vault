"""Discovery + state: live-vs-backup classification, multi-account, and round-trip."""
from __future__ import annotations

import shutil
from pathlib import Path

from nms_save_vault import cli
from nms_save_vault.core import discover, locations, state
from nms_save_vault.core import operations as ops
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.formats import ACCOUNT_META_NAME

from .conftest import steam_save

ACCT_A = "76561198000000002"
ACCT_B = "76561198000000001"


def test_state_path_is_next_to_packaged_launcher(tmp_path, monkeypatch):
    """In the packaged app, state.json sits beside the launcher, so it travels with it.

    The launcher is a renamed copy of the signed CPython exe, so there is no frozen
    flag to key off; the portable runtime exports NMSVAULT_PORTABLE_ROOT instead.
    """
    install = tmp_path / "Programs" / "NMSSaveVault"
    install.mkdir(parents=True)
    monkeypatch.setenv("NMSVAULT_PORTABLE_ROOT", str(install))
    assert state.install_dir() == install
    assert state.default_state_path() == install / "state.json"


def test_state_path_from_source_uses_localappdata(tmp_path, monkeypatch):
    """From source (no portable root), config stays out of the tree, in %LOCALAPPDATA%."""
    monkeypatch.delenv("NMSVAULT_PORTABLE_ROOT", raising=False)
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


def test_gog_default_user_is_a_live_source_next_to_steam(tmp_path):
    root = _make_nms_root(tmp_path)
    _make_steam_dir(root, f"st_{ACCT_A}")
    gog = _make_steam_dir(root, "DefaultUser")
    sources = {Path(s.path).name: s for s in discover.discover_live_sources(root, ms_dirs=[])}
    assert set(sources) == {f"st_{ACCT_A}", "DefaultUser"}
    found = sources["DefaultUser"]
    assert (found.id, found.platform, found.account, found.label) == \
        ("steam-DefaultUser", state.PLATFORM_STEAM, "", "GOG")
    assert found.role == state.ROLE_LIVE and found.writable
    assert discover.is_canonical_gog_live(gog, root)


def test_gog_copies_empty_folders_and_nested_ones_are_not_live(tmp_path):
    root = _make_nms_root(tmp_path)
    (root / "DefaultUser").mkdir()                  # empty: nothing says it holds saves
    copy = _make_steam_dir(root, "DefaultUser - Copy")
    nested = _make_steam_dir(root / "old", "DefaultUser")
    assert discover.discover_live_sources(root, ms_dirs=[]) == []
    assert not discover.is_canonical_gog_live(copy, root)
    assert not discover.is_canonical_gog_live(nested, root)
    found = discover.discover_inplace_backups(root, live=[])
    assert sorted(p.name for p in found) == ["DefaultUser", "DefaultUser - Copy"]
    assert nested in found and copy in found


def test_the_gog_folder_is_never_catalogued_as_a_backup_of_itself(tmp_path):
    root = _make_nms_root(tmp_path)
    _make_steam_dir(root, "DefaultUser")
    assert discover.discover_inplace_backups(root, live=[]) == []


def test_live_save_dirs_list_gog_after_the_steam_accounts(tmp_path, monkeypatch):
    root = _make_nms_root(tmp_path)
    gog = _make_steam_dir(root, "DefaultUser")
    steam = _make_steam_dir(root, f"st_{ACCT_A}")
    monkeypatch.setattr(locations, "nms_roots", lambda: [root])
    assert locations.find_live_save_dirs() == [steam, gog]


def test_inplace_backups_exclude_live_and_vault(tmp_path):
    root = _make_nms_root(tmp_path)
    live = _make_steam_dir(root, f"st_{ACCT_A}")
    backup = _make_steam_dir(root, f"st_{ACCT_A} - Copy")
    renamed = _make_steam_dir(root, "MyOldBackup")
    vault = root / "_SaveVault"
    _make_steam_dir(vault, "backups")  # something save-like inside the vault

    found = discover.discover_inplace_backups(root, live=[live], exclude=[vault])
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


# --- P9: a live folder is never catalogued as a backup of itself -------------------------------


def _xbox_with_twin(tmp_path: Path, make_wgs_account) -> tuple[Path, Path]:
    """A wgs account folder and its xgs mirror, as the game lays them out."""
    xbox = make_wgs_account(tmp_path / "SystemAppData", [("Slot1Auto", '{"a":1}', "Run", "", 100, 1000)])
    twin = tmp_path / "SystemAppData" / "xgs" / xbox.name
    (twin / "Slot1Auto").mkdir(parents=True)
    (twin / "Slot1Auto" / "data").write_bytes(b"x")
    return xbox, twin


def test_live_folders_their_xgs_twin_and_anything_inside_are_never_backups(tmp_path, make_wgs_account):
    root = _make_nms_root(tmp_path)
    _make_steam_dir(root, f"st_{ACCT_A}")                 # the game's own by name: no candidate
    _make_steam_dir(root, "MyOldBackup")
    second_live = _make_steam_dir(root, "Second live")
    empty_live = root / "Live, no saves on top"
    empty_live.mkdir()
    inside = _make_steam_dir(empty_live, "pasted inside")
    xbox, twin = _xbox_with_twin(tmp_path, make_wgs_account)

    live = discover.live_folders([second_live, empty_live, xbox])
    assert discover.is_inside(twin, live) and discover.is_inside(inside, live)

    search = discover.find_backups(root, live=live)
    assert [p.name for p in search.backups] == ["MyOldBackup"]
    assert {p.name for p in search.skipped_live} == {"Second live", inside.name}


def test_all_live_paths_counts_disabled_sources_and_what_is_on_disk(tmp_path, monkeypatch):
    root = _make_nms_root(tmp_path)
    canonical = _make_steam_dir(root, f"st_{ACCT_A}")
    off = state.Source(id="steam-manual", platform=state.PLATFORM_STEAM, account="",
                       path=str(tmp_path / "manual"), origin=state.ORIGIN_MANUAL, enabled=False)
    monkeypatch.setattr(locations, "nms_roots", lambda: [root])

    paths = discover.all_live_paths(state.AppState(sources=[off]), ms_dirs=[])

    assert {Path(p) for p in paths} == {Path(off.path), canonical}


def test_live_folders_are_resolved_by_the_rule_itself(tmp_path, make_wgs_account):
    """Callers may pass live sources in any form; the xgs twin comes with them."""
    root = _make_nms_root(tmp_path)
    second_live = _make_steam_dir(root, "Second live")
    xbox, twin = _xbox_with_twin(tmp_path, make_wgs_account)

    assert discover.is_live_folder(twin, [xbox.parent / ".." / xbox.parent.name / xbox.name])
    search = discover.find_backups(root, live=[root / ".." / root.name / second_live.name])
    assert search.backups == [] and [p.name for p in search.skipped_live] == ["Second live"]


def test_exclude_holds_for_every_root_even_as_a_one_shot_iterable(tmp_path, monkeypatch):
    first = _make_nms_root(tmp_path / "a")
    second = _make_nms_root(tmp_path / "b")
    vault = second / "_SaveVault"
    _make_steam_dir(vault, "backups")
    monkeypatch.setattr(locations, "nms_roots", lambda: [first, second])

    assert discover.find_backups(live=[], exclude=(p for p in [vault])).backups == []


def _discover_env(tmp_path: Path, monkeypatch, make_wgs_account) -> tuple[Path, Path, Path]:
    """An NMS root with a live Steam folder and one backup, a live Xbox account, and an empty
    vault elsewhere; the CLI sees only these."""
    root = _make_nms_root(tmp_path)
    steam_save(root / f"st_{ACCT_A}", 1, 0, "Live", 100, 0x1, 1000)
    backup = root / "Backup June"
    steam_save(backup, 1, 0, "Old", 50, 0x1, 900)
    xbox, _twin = _xbox_with_twin(tmp_path, make_wgs_account)
    monkeypatch.setattr(locations, "nms_roots", lambda: [root])
    monkeypatch.setattr(locations, "find_microsoft_save_dirs", lambda: [xbox])
    monkeypatch.setattr(cli.appstate, "load", lambda *a, **k: None)
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    return backup, xbox, vault.root


def test_discover_add_catalogues_backups_but_never_a_live_folder(tmp_path, monkeypatch, make_wgs_account, capsys):
    backup, xbox, vault_root = _discover_env(tmp_path, monkeypatch, make_wgs_account)

    assert cli.main(["--vault", str(vault_root), "discover", "--add"]) == 0

    out = capsys.readouterr().out
    assert [Path(e.path).resolve() for e in Vault(vault_root).load().entries] == [backup.resolve()]
    assert str(xbox) not in out
    assert "skipped" not in out                 # a normal machine: nothing surprising to report


def test_discover_says_what_it_searched_when_it_finds_nothing(tmp_path, monkeypatch, make_wgs_account, capsys):
    backup, _xbox, vault_root = _discover_env(tmp_path, monkeypatch, make_wgs_account)
    shutil.rmtree(backup)

    assert cli.main(["--vault", str(vault_root), "discover"]) == 0

    out = capsys.readouterr().out
    assert f"(no backups found under {backup.parent}; Xbox folders hold only live saves" in out


def test_discover_notes_an_old_entry_that_is_a_live_folder_and_leaves_it(tmp_path, monkeypatch, make_wgs_account, capsys):
    _backup, xbox, vault_root = _discover_env(tmp_path, monkeypatch, make_wgs_account)
    alias = ops.import_backup(Vault(vault_root).load(), xbox, label="live by mistake", copy_into_vault=False)

    assert cli.main(["--vault", str(vault_root), "discover"]) == 0

    err = capsys.readouterr().err                       # a note, not a result
    assert f"note: catalog entry '{alias.id}' ({alias.path}) is or lies inside a live save folder" in err
    assert alias.id in [e.id for e in Vault(vault_root).load().entries]


def test_discover_never_catalogues_a_disabled_live_source(tmp_path, monkeypatch, make_wgs_account, capsys):
    """A live folder the user switched off in the app is still the game's own folder."""
    backup, _xbox, vault_root = _discover_env(tmp_path, monkeypatch, make_wgs_account)
    elsewhere = backup.parent / "Live elsewhere"
    steam_save(elsewhere, 1, 0, "Live", 100, 0x1, 1000)
    off = state.Source(id="steam-manual", platform=state.PLATFORM_STEAM, account="", path=str(elsewhere),
                       origin=state.ORIGIN_MANUAL, enabled=False)
    monkeypatch.setattr(cli.appstate, "load", lambda *a, **k: state.AppState(sources=[off]))

    assert cli.main(["--vault", str(vault_root), "discover", "--add"]) == 0

    assert [Path(e.path).resolve() for e in Vault(vault_root).load().entries] == [backup.resolve()]
    assert "skipped 1 live save folder (not backups)" in capsys.readouterr().out


def test_a_vault_inside_a_live_folder_gets_one_note_not_one_per_entry(tmp_path, monkeypatch, make_wgs_account, capsys):
    backup, _xbox, _vault_root = _discover_env(tmp_path, monkeypatch, make_wgs_account)
    vault = Vault(backup.parent / f"st_{ACCT_A}" / "vault")
    vault.ensure()
    for label in ("one", "two"):
        ops.import_backup(vault, backup, label=label, copy_into_vault=True)

    assert cli.main(["--vault", str(vault.root), "discover"]) == 0

    notes = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("note:")]
    assert len(notes) == 1 and "the vault" in notes[0] and "is or lies inside a live save folder" in notes[0]
