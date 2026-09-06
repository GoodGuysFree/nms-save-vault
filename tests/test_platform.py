"""Per-OS location resolution.

These run on any machine: the resolvers take their system, home and environment as
arguments (D7), so the Linux and macOS trees below are built in ``tmp_path`` and checked
from whatever platform the suite happens to be running on.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nms_save_vault.core import locations
from nms_save_vault.core import platform as host_platform
from nms_save_vault.core.platform import LINUX, MACOS, WINDOWS, Host


def host(system: str, home: Path, **env: str) -> Host:
    return Host(system=system, home=Path(home), env=env)


def make_save_dir(path: Path) -> Path:
    """A folder that ``savedir.looks_like_save_dir`` will accept."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "mf_save.hg").write_bytes(b"")
    return path


def proton_nms(library: Path, appdata: tuple[str, ...] = ("AppData", "Roaming")) -> Path:
    """The NMS folder inside ``library``'s Proton prefix for app 275850."""
    return library.joinpath(
        "steamapps", "compatdata", "275850", "pfx", "drive_c", "users", "steamuser",
        *appdata, "HelloGames", "NMS",
    )


# --- system detection ---------------------------------------------------------


@pytest.mark.parametrize(
    "sys_platform, expected",
    [
        ("win32", WINDOWS),
        ("cygwin", LINUX),  # os.name is "posix" there
        ("darwin", MACOS),
        ("linux", LINUX),
        ("freebsd14", LINUX),  # unknown platforms take the POSIX path, not a crash
    ],
)
def test_current_system_maps_sys_platform(sys_platform, expected):
    assert host_platform.current_system(sys_platform) == expected


# --- our own directories ------------------------------------------------------


def test_config_and_data_dirs_follow_xdg_on_linux(tmp_path):
    h = host(LINUX, tmp_path)
    assert host_platform.user_config_dir(h) == tmp_path / ".config" / "NMSSaveVault"
    assert host_platform.user_data_dir(h) == tmp_path / ".local" / "share" / "NMSSaveVault"

    overridden = host(
        LINUX, tmp_path,
        XDG_CONFIG_HOME=str(tmp_path / "cfg"),
        XDG_DATA_HOME=str(tmp_path / "data"),
    )
    assert host_platform.user_config_dir(overridden) == tmp_path / "cfg" / "NMSSaveVault"
    assert host_platform.user_data_dir(overridden) == tmp_path / "data" / "NMSSaveVault"


def test_relative_xdg_values_are_ignored(tmp_path):
    """The spec says a relative value is invalid; honouring one would scatter the vault."""
    h = host(LINUX, tmp_path, XDG_DATA_HOME="relative/path")
    assert host_platform.user_data_dir(h) == tmp_path / ".local" / "share" / "NMSSaveVault"


def test_config_dir_on_macos_and_windows(tmp_path):
    mac = host(MACOS, tmp_path)
    assert host_platform.user_config_dir(mac) == (
        tmp_path / "Library" / "Application Support" / "NMSSaveVault"
    )

    win = host(WINDOWS, tmp_path, LOCALAPPDATA=str(tmp_path / "Local"))
    assert host_platform.user_config_dir(win) == tmp_path / "Local" / "NMSSaveVault"
    # No LOCALAPPDATA (a stripped environment) still resolves somewhere sane.
    assert host_platform.user_config_dir(host(WINDOWS, tmp_path)) == tmp_path / "NMSSaveVault"


# --- Windows ------------------------------------------------------------------


def test_windows_root_comes_from_appdata(tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    nms = appdata / "HelloGames" / "NMS"
    make_save_dir(nms / "st_1")
    h = host(WINDOWS, tmp_path, APPDATA=str(appdata))

    assert host_platform.primary_nms_root(h) == nms
    assert host_platform.nms_roots(h) == [nms]


def test_windows_without_appdata_has_no_root(tmp_path):
    h = host(WINDOWS, tmp_path)
    assert host_platform.primary_nms_root(h) is None
    assert host_platform.nms_roots(h) == []


# --- macOS --------------------------------------------------------------------


def test_macos_finds_the_plain_root_and_a_sandbox_container(tmp_path):
    plain = tmp_path / "Library" / "Application Support" / "HelloGames" / "NMS"
    make_save_dir(plain / "st_1")
    container = tmp_path.joinpath(
        "Library", "Containers", "com.hellogames.nomanssky",
        "Data", "Library", "Application Support", "HelloGames", "NMS",
    )
    make_save_dir(container / "st_1")

    h = host(MACOS, tmp_path)
    assert host_platform.primary_nms_root(h) == plain
    assert host_platform.nms_roots(h) == [plain, container]


def test_macos_root_is_reported_before_the_game_has_run(tmp_path):
    """primary_nms_root is existence-independent; nms_roots is not."""
    h = host(MACOS, tmp_path)
    assert host_platform.primary_nms_root(h) is not None
    assert host_platform.nms_roots(h) == []


# --- Linux: libraryfolders.vdf ------------------------------------------------


def test_parse_libraryfolders_modern_format():
    text = """
    "libraryfolders"
    {
        "0"
        {
            "path"        "/home/amit/.local/share/Steam"
            "label"       ""
            "apps"
            {
                "275850"      "18000000000"
            }
        }
        "1"
        {
            "path"        "/run/media/mmcblk0p1"
            "label"       "SD Card"
        }
    }
    """
    assert host_platform.parse_libraryfolders(text) == [
        Path("/home/amit/.local/share/Steam"),
        Path("/run/media/mmcblk0p1"),
    ]


def test_parse_libraryfolders_old_format_and_windows_escapes():
    """Steam before 2021 wrote bare numbered entries, and escapes backslashes."""
    text = """
    "LibraryFolders"
    {
        "TimeNextStatsReport"     "1234567890"
        "ContentStatsID"          "-1111111111111111111"
        "1"                       "D:\\\\SteamLibrary"
    }
    """
    assert host_platform.parse_libraryfolders(text) == [Path(r"D:\SteamLibrary")]


def test_parse_libraryfolders_ignores_the_apps_block():
    """app id -> byte count is digits on both sides and must not read as a folder."""
    text = '"apps" { "275850" "18000000000" "1174180" "92000000000" }'
    assert host_platform.parse_libraryfolders(text) == []


# --- Linux: Proton prefixes ---------------------------------------------------


def test_linux_finds_every_steam_library(tmp_path):
    home = tmp_path / "home"
    native = home / ".local" / "share" / "Steam"
    flatpak = home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam"
    sdcard = tmp_path / "run" / "media" / "mmcblk0p1"

    for library in (native, flatpak, sdcard):
        make_save_dir(proton_nms(library) / "st_1")

    # Only the native install knows about the SD card; nothing under home points at it.
    (native / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "%s" } }' % sdcard.as_posix(), "utf-8"
    )

    roots = host_platform.nms_roots(host(LINUX, home))
    assert len(roots) == 3
    assert set(roots) == {proton_nms(native), proton_nms(flatpak), proton_nms(sdcard)}


def test_linux_accepts_the_old_wine_application_data_name(tmp_path):
    home = tmp_path / "home"
    native = home / ".local" / "share" / "Steam"
    legacy = proton_nms(native, appdata=("Application Data",))
    make_save_dir(legacy / "st_1")

    assert host_platform.nms_roots(host(LINUX, home)) == [legacy]


def test_linux_symlinked_steam_roots_are_not_double_counted(tmp_path):
    """~/.steam/steam and ~/.local/share/Steam are normally the same folder."""
    home = tmp_path / "home"
    native = home / ".local" / "share" / "Steam"
    make_save_dir(proton_nms(native) / "st_1")

    dot_steam = home / ".steam"
    dot_steam.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(native, dot_steam / "steam", target_is_directory=True)
        os.symlink(native, dot_steam / "root", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not permit creating symlinks")

    assert len(host_platform.steam_roots(host(LINUX, home))) == 1
    assert host_platform.nms_roots(host(LINUX, home)) == [proton_nms(native)]


def test_linux_without_steam_finds_nothing(tmp_path):
    assert host_platform.nms_roots(host(LINUX, tmp_path)) == []
    assert host_platform.primary_nms_root(host(LINUX, tmp_path)) is None


def test_unreadable_libraryfolders_does_not_stop_discovery(tmp_path):
    """One bad file must not take down the whole scan."""
    home = tmp_path / "home"
    native = home / ".local" / "share" / "Steam"
    make_save_dir(proton_nms(native) / "st_1")
    # A directory where the file should be: read_text raises, discovery continues.
    (native / "steamapps" / "libraryfolders.vdf").mkdir(parents=True)

    assert host_platform.nms_roots(host(LINUX, home)) == [proton_nms(native)]


# --- the locations facade -----------------------------------------------------


@pytest.fixture
def as_host(monkeypatch):
    """Pin ``Host.current()`` so the no-argument locations helpers can be tested."""

    def use(h: Host) -> None:
        monkeypatch.setattr(Host, "current", classmethod(lambda cls: h))

    return use


def test_find_live_save_dirs_spans_every_root(tmp_path, as_host):
    home = tmp_path / "home"
    native = home / ".local" / "share" / "Steam"
    flatpak = home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam"
    first = make_save_dir(proton_nms(native) / "st_1")
    second = make_save_dir(proton_nms(flatpak) / "st_2")
    # A folder that is not a save dir is ignored even though the name matches.
    (proton_nms(native) / "st_3").mkdir()

    as_host(host(LINUX, home))
    assert locations.find_live_save_dirs() == [first, second]
    assert locations.default_live_save_dir() == first


def test_vault_default_is_unchanged_on_windows(tmp_path, as_host):
    appdata = tmp_path / "AppData" / "Roaming"
    as_host(host(WINDOWS, tmp_path, APPDATA=str(appdata)))
    assert locations.default_vault_dir() == appdata / "HelloGames" / "NMS" / "_SaveVault"

    as_host(host(WINDOWS, tmp_path))
    assert locations.default_vault_dir() == tmp_path / "_SaveVault"


def test_vault_default_stays_out_of_the_proton_prefix(tmp_path, as_host):
    """D2: Steam deletes and recreates prefixes, so the vault must not live in one."""
    as_host(host(LINUX, tmp_path))
    vault = locations.default_vault_dir()
    assert vault == tmp_path / ".local" / "share" / "NMSSaveVault" / "Vault"
    assert "compatdata" not in str(vault)


def test_vault_default_on_macos(tmp_path, as_host):
    as_host(host(MACOS, tmp_path))
    assert locations.default_vault_dir() == (
        tmp_path / "Library" / "Application Support" / "NMSSaveVault" / "Vault"
    )
