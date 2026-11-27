"""state.json load + version migration."""
from __future__ import annotations

import json

from nms_save_vault.core import state


def test_v1_state_migrates_xbox_to_writable(tmp_path):
    """An old v1 config (Xbox saved read-only) heals to writable on load, so existing
    installs gain same-platform Xbox write without a manual rescan."""
    p = tmp_path / "state.json"
    p.write_text(
        '{"version": 1, "vault": null, "sources": ['
        '{"id": "xbox-acc", "platform": "xbox", "account": "acc", "path": "X", "writable": false},'
        '{"id": "steam-1", "platform": "steam", "account": "1", "path": "Y", "writable": true}'
        ']}',
        "utf-8",
    )
    st = state.load(p)
    assert st.get("xbox-acc").writable is True     # healed by the v1 -> v2 migration
    assert st.get("steam-1").writable is True
    assert st.version == state.STATE_VERSION


def test_current_version_preserves_explicit_writable(tmp_path):
    """At the current version no migration runs, so an explicit writable flag is preserved."""
    p = tmp_path / "state.json"
    p.write_text(
        '{"version": ' + str(state.STATE_VERSION) + ', "vault": null, "sources": ['
        '{"id": "xbox-acc", "platform": "xbox", "account": "acc", "path": "X", "writable": false}'
        ']}',
        "utf-8",
    )
    st = state.load(p)
    assert st.get("xbox-acc").writable is False


# --- where the config lives (D2/D7: per-OS, with the portable override winning) ---------


def test_install_dir_prefers_the_portable_root(tmp_path, monkeypatch):
    """A packaged kit keeps its config beside the program, on every platform."""
    monkeypatch.setenv("NMSVAULT_PORTABLE_ROOT", str(tmp_path / "kit"))
    assert state.install_dir() == tmp_path / "kit"


def _save_with_a_vault(tmp_path):
    st = state.AppState()
    st.vault = str(tmp_path / "vault")
    state.save(st)


def test_a_kit_built_in_a_development_checkout_leaves_the_shared_pointer_alone(
        tmp_path, monkeypatch):
    """A smoke run of the built kit in dist must not point other apps at a state.json that
    the next build deletes (GGF 2026-11-27); only the app the user installed may."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setenv("NMSVAULT_PORTABLE_ROOT", str(tmp_path / "repo" / "dist" / "NMSSaveVault"))
    assert state.development_run()
    _save_with_a_vault(tmp_path)
    assert (tmp_path / "repo" / "dist" / "NMSSaveVault" / "state.json").is_file()
    assert not state.pointer_path().exists()


def test_a_run_from_source_in_a_checkout_leaves_the_shared_pointer_alone(monkeypatch):
    monkeypatch.delenv("NMSVAULT_PORTABLE_ROOT", raising=False)
    assert state.development_run()                      # these tests run from the checkout


def test_the_installed_app_names_its_vault_in_the_shared_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv("NMSVAULT_PORTABLE_ROOT", str(tmp_path / "Programs" / "NMSSaveVault"))
    assert not state.development_run()
    _save_with_a_vault(tmp_path)
    pointer = json.loads(state.pointer_path().read_text(encoding="utf-8"))
    assert pointer["vault"] == str(tmp_path / "vault")


def test_install_dir_falls_back_to_the_os_config_dir(tmp_path, monkeypatch):
    from nms_save_vault.core import platform as host_platform

    monkeypatch.delenv("NMSVAULT_PORTABLE_ROOT", raising=False)
    for system, expected in (
        (host_platform.LINUX, tmp_path / ".config" / "NMSSaveVault"),
        (host_platform.MACOS, tmp_path / "Library" / "Application Support" / "NMSSaveVault"),
        (host_platform.WINDOWS, tmp_path / "Local" / "NMSSaveVault"),
    ):
        host = host_platform.Host(system, tmp_path, {"LOCALAPPDATA": str(tmp_path / "Local")})
        monkeypatch.setattr(host_platform.Host, "current", classmethod(lambda cls, h=host: h))
        assert state.install_dir() == expected


def test_font_scale_round_trips_and_survives_a_hand_edited_config():
    st = state.AppState(font_scale=1.3)
    assert state.AppState.from_dict(st.to_dict()).font_scale == 1.3
    assert state.AppState.from_dict({"font_scale": "big"}).font_scale == 1.0
    assert state.AppState.from_dict({}).font_scale == 1.0


def test_a_state_file_with_a_bom_still_loads(tmp_path):
    """An editor that saves UTF-8 with a BOM (older Notepad) must not stop the app from
    starting."""
    p = tmp_path / "state.json"
    p.write_bytes(b"\xef\xbb\xbf" + b'{"version": 2, "vault": null, "sources": []}')
    st = state.load(p)
    assert st is not None and st.sources == []
