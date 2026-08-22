"""Account display names: the filter, its file, and the promise that nothing else leaks.

The point of the feature is negative -- a configured account id must not appear anywhere
in either front-end -- so the CLI tests assert on absence of the real id, not presence of
the alias.
"""
from __future__ import annotations

import argparse

import pytest

from nms_save_vault import cli
from nms_save_vault.core import aliases
from nms_save_vault.core import state as appstate

STEAM_ID = "76561197975032661"
XBOX_ID = "000901F0DD67CC4E_29070100B936489ABCE8B9AF3980429C"


@pytest.fixture
def amap() -> aliases.AliasMap:
    return aliases.AliasMap({STEAM_ID: "Main", XBOX_ID: "Xbox Main"})


# --- the filter --------------------------------------------------------------


def test_bare_id_and_steam_folder_both_redact(amap):
    assert amap.redact(STEAM_ID) == "Main"
    assert amap.redact(f"st_{STEAM_ID}") == "Main"


def test_folder_name_redacts_whole(amap):
    """The st_ prefix must go with the id: matching the bare id first would leave
    'st_Main', which still marks the folder as an account folder."""
    path = rf"C:\Users\me\AppData\Roaming\HelloGames\NMS\st_{STEAM_ID}"
    assert amap.redact(path) == r"C:\Users\me\AppData\Roaming\HelloGames\NMS\Main"


def test_xbox_account_redacts_case_insensitively(amap):
    """containers.index and the folder name disagree on hex case on real installs."""
    assert amap.redact(XBOX_ID.lower()) == "Xbox Main"
    assert amap.redact(f"wgs/{XBOX_ID}/container") == "wgs/Xbox Main/container"


def test_every_occurrence_in_one_string_is_replaced(amap):
    text = f"copy st_{STEAM_ID} to {STEAM_ID} for {XBOX_ID}"
    assert amap.redact(text) == "copy Main to Main for Xbox Main"


def test_unconfigured_account_is_left_alone(amap):
    other = "76561190000000000"
    assert amap.redact(f"st_{other}") == f"st_{other}"


def test_blank_alias_is_not_a_configured_account():
    """An empty name means 'show the real id', not 'replace with nothing'."""
    m = aliases.AliasMap({STEAM_ID: "   "})
    assert m.redact(f"st_{STEAM_ID}") == f"st_{STEAM_ID}"


def test_set_and_clear_take_effect_immediately(amap):
    amap.clear(STEAM_ID)
    assert amap.redact(STEAM_ID) == STEAM_ID
    amap.set(STEAM_ID, "Second")
    assert amap.redact(STEAM_ID) == "Second"
    amap.set(STEAM_ID, "")  # blank clears the entry
    assert amap.entries == {XBOX_ID: "Xbox Main"}


def test_non_strings_pass_through(amap):
    assert amap.redact(7) == 7
    assert amap.redact(None) is None


def test_empty_map_is_a_no_op():
    assert aliases.AliasMap().redact(f"st_{STEAM_ID}") == f"st_{STEAM_ID}"


# --- the file ----------------------------------------------------------------


def test_ini_roundtrip_preserves_account_case(tmp_path, amap):
    """configparser lower-cases option names by default; the Xbox id is upper-case hex and
    must come back byte-identical or it would no longer match the folder on disk."""
    p = aliases.save(amap, tmp_path / "accounts.ini")
    loaded = aliases.load(p)
    assert loaded.entries == {STEAM_ID: "Main", XBOX_ID: "Xbox Main"}
    assert XBOX_ID in p.read_text("utf-8")


def test_missing_file_loads_as_empty(tmp_path):
    assert aliases.load(tmp_path / "nope.ini").entries == {}


def test_malformed_file_raises_rather_than_failing_open(tmp_path):
    """Recovering silently would print the very ids the user asked to hide."""
    p = tmp_path / "accounts.ini"
    p.write_text("this line has no section header\n", "utf-8")
    with pytest.raises(aliases.AliasConfigError):
        aliases.load(p)


# --- the front ends ----------------------------------------------------------


@pytest.fixture
def configured(monkeypatch, tmp_path, amap):
    """An app that knows both accounts and has display names set for both."""
    monkeypatch.setattr(aliases, "_active", amap)
    monkeypatch.setattr(aliases, "default_path", lambda: tmp_path / "accounts.ini")
    st = appstate.AppState(
        sources=[
            appstate.Source(id="steam-x", platform="steam", account=STEAM_ID,
                            path=rf"C:\NMS\st_{STEAM_ID}", label=f"Steam ({STEAM_ID})"),
            appstate.Source(id="xbox-x", platform="xbox", account=XBOX_ID,
                            path=rf"C:\wgs\{XBOX_ID}", label="Xbox / Game Pass"),
        ],
        vault=r"C:\NMS\_SaveVault",
    )
    monkeypatch.setattr(appstate, "load", lambda *a, **k: st)
    return st


def test_cli_sources_shows_no_real_account_id(configured, capsys):
    cli.cmd_sources(argparse.Namespace(rescan=False))
    out = capsys.readouterr().out
    assert STEAM_ID not in out
    assert XBOX_ID not in out
    assert "Main" in out


def test_cli_sources_keeps_its_columns_aligned(configured, capsys):
    """The alias is shorter than the id it replaces, so it has to be substituted before
    the column is padded, not after the row is formatted."""
    cli.cmd_sources(argparse.Namespace(rescan=False))
    rows = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("live")]
    assert len(rows) == 2
    for row in rows:
        assert row.index("C:") == 43  # role 7 + platform 9 + write 7 + account 20


def test_cli_print_filters_every_argument(configured, capsys):
    cli.print("folder", f"st_{STEAM_ID}", 3)
    assert capsys.readouterr().out == "folder Main 3\n"


def test_cli_accounts_command_is_where_the_real_ids_show(configured, capsys):
    """The one documented exception: you cannot configure a name for an id you can't see."""
    cli.cmd_accounts(argparse.Namespace(set=None, clear=None))
    out = capsys.readouterr().out
    assert STEAM_ID in out
    assert XBOX_ID in out


def test_cli_accounts_set_and_clear_persist(configured, tmp_path, capsys):
    cli.cmd_accounts(argparse.Namespace(set=[f"{STEAM_ID}=Renamed"], clear=[XBOX_ID]))
    capsys.readouterr()
    assert aliases.load(tmp_path / "accounts.ini").entries == {STEAM_ID: "Renamed"}


def test_cli_accounts_rejects_a_set_without_an_equals(configured):
    with pytest.raises(SystemExit):
        cli.cmd_accounts(argparse.Namespace(set=["nonsense"], clear=None))


def test_gui_source_caption_drops_the_folder_once_it_repeats_the_label(configured):
    """'Steam (Main)  (Main)' would be the naive result of filtering both halves."""
    from nms_save_vault import gui

    steam, xbox = configured.sources
    assert gui._source_caption(steam) == "Steam (Main)"
    assert gui._source_caption(xbox) == "Xbox / Game Pass  (Xbox Main)"


def test_gui_source_caption_unchanged_without_a_display_name(monkeypatch, configured):
    """With no alias set the row must still read exactly as it did before the feature."""
    from nms_save_vault import gui

    monkeypatch.setattr(aliases, "_active", aliases.AliasMap())
    steam = configured.sources[0]
    assert gui._source_caption(steam) == f"Steam ({STEAM_ID})  (st_{STEAM_ID})"


def test_gui_tree_redacts_row_text_and_values(configured):
    """The tree is where the live-source rows are drawn, so it filters structurally."""
    tk = pytest.importorskip("tkinter")
    from nms_save_vault import gui

    try:
        root = tk.Tk()
    except tk.TclError:  # no display
        pytest.skip("no Tk display available")
    try:
        root.withdraw()
        tree = gui.RedactingTreeview(root, columns=("a",))
        node = tree.insert("", "end", text=f"Steam ({STEAM_ID})  (st_{STEAM_ID})",
                           values=(f"slot 9 from st_{STEAM_ID}",))
        assert tree.item(node, "text") == "Steam (Main)  (Main)"
        assert tree.item(node, "values") == ("slot 9 from Main",)
    finally:
        root.destroy()
