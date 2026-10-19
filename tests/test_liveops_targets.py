"""liveops.targets: a write target resolves only to a configured, writable live folder of
its platform outside the vault (D5 2.3.6, C29; T-T1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from nms_save_vault.core import aliases
from nms_save_vault.core.liveops import TargetError
from nms_save_vault.core.liveops.targets import resolve_account, resolve_target
from nms_save_vault.core.organizer.model import LiveRef, LiveTarget
from nms_save_vault.core.organizer.scanner import instance_of

from .conftest import steam_save
from .liveops_support import (
    STEAM_ACCOUNT,
    STEAM_ACCOUNT_2,
    XBOX_ACCOUNT,
    live_source,
    make_ctx,
    tree_hash,
)


@pytest.fixture(autouse=True)
def no_aliases(monkeypatch):
    monkeypatch.setattr(aliases, "_active", aliases.AliasMap())


def steam_dir(parent: Path, name: str = STEAM_ACCOUNT, *, cloud: bool = False) -> Path:
    folder = parent / name
    steam_save(folder, 7, 0, "Main", 3600, 0xDDDAD1BDD39F9C72, 1_790_000_000)
    if cloud:
        (folder / "steam_autocloud.vdf").write_text('"steam_autocloud.vdf"\n{\n}\n', "utf-8")
    return folder


def code_of(ctx, target) -> str:
    with pytest.raises(TargetError) as info:
        resolve_target(ctx, target)
    assert str(info.value.args[0]).isascii()
    return info.value.code


def test_a_configured_steam_folder_resolves_with_its_cloud_mode(tmp_path):
    cloud = steam_dir(tmp_path / "NMS", cloud=True)
    local = tmp_path / "NMS" / STEAM_ACCOUNT_2
    local.mkdir()                                   # an empty account folder: named like one
    ctx = make_ctx(tmp_path, [live_source(cloud), live_source(local)])
    before = tree_hash(tmp_path / "NMS")

    rt = resolve_target(ctx, LiveTarget("steam", STEAM_ACCOUNT, 7))
    assert (rt.folder, rt.cloud_mode, rt.xgs_twin) == (cloud, "steam-cloud", None)
    assert rt.source_id == ctx.sources[0].id
    assert resolve_target(ctx, LiveTarget("steam", STEAM_ACCOUNT_2, 1)).cloud_mode == "steam-local"
    assert tree_hash(tmp_path / "NMS") == before    # resolving reads only


def test_slots_platforms_and_unknown_accounts_are_refused(tmp_path):
    folder = steam_dir(tmp_path / "NMS")
    off = steam_dir(tmp_path / "Other", STEAM_ACCOUNT_2)
    ctx = make_ctx(tmp_path, [live_source(folder), live_source(off, enabled=False)])
    assert code_of(ctx, LiveTarget("steam", STEAM_ACCOUNT, 0)) == "BAD_TARGET"
    assert code_of(ctx, LiveTarget("steam", STEAM_ACCOUNT, 16)) == "BAD_TARGET"
    assert code_of(ctx, LiveTarget("ps5", STEAM_ACCOUNT, 1)) == "BAD_TARGET"  # type: ignore[arg-type]
    assert code_of(ctx, LiveTarget("xbox", STEAM_ACCOUNT, 1)) == "UNKNOWN_ACCOUNT"
    assert code_of(ctx, LiveTarget("steam", "st_76561198000000099", 1)) == "UNKNOWN_ACCOUNT"
    assert code_of(ctx, LiveTarget("steam", STEAM_ACCOUNT_2, 1)) == "UNKNOWN_ACCOUNT"   # disabled


def test_two_folders_of_one_name_need_the_instance_tag(tmp_path):
    first = steam_dir(tmp_path / "prefix1")
    second = steam_dir(tmp_path / "prefix2")
    ctx = make_ctx(tmp_path, [live_source(first), live_source(second)])
    assert code_of(ctx, LiveTarget("steam", STEAM_ACCOUNT, 3)) == "AMBIGUOUS"
    rt = resolve_target(ctx, LiveTarget("steam", STEAM_ACCOUNT, 3, instance_of(second)))
    assert rt.folder == second
    assert rt.label == "Steam slot 3 (Steam 2)"     # several Steam accounts: say which


def test_the_vault_and_folders_inside_it_are_never_targets(tmp_path):
    vault = tmp_path / "vault"
    inside = steam_dir(vault / "backups")
    ctx = make_ctx(tmp_path, [live_source(inside)], vault_root=vault)
    assert code_of(ctx, LiveTarget("steam", STEAM_ACCOUNT, 1)) == "INSIDE_VAULT"
    as_vault = steam_dir(tmp_path)                  # the configured folder IS the vault
    ctx = make_ctx(tmp_path, [live_source(as_vault)], vault_root=as_vault)
    assert code_of(ctx, LiveTarget("steam", STEAM_ACCOUNT, 1)) == "INSIDE_VAULT"


def test_folders_that_are_not_live_steam_folders_are_refused(tmp_path):
    odd = tmp_path / "NMS" / "notes"
    odd.mkdir(parents=True)
    gone = tmp_path / "NMS" / STEAM_ACCOUNT_2
    ro = steam_dir(tmp_path / "RO")
    ctx = make_ctx(tmp_path, [live_source(odd), live_source(gone),
                              live_source(ro, writable=False)])
    assert code_of(ctx, LiveTarget("steam", "notes", 1)) == "NOT_A_LIVE_FOLDER"
    assert code_of(ctx, LiveTarget("steam", STEAM_ACCOUNT_2, 1)) == "FOLDER_MISSING"
    assert code_of(ctx, LiveTarget("steam", STEAM_ACCOUNT, 1)) == "NOT_WRITABLE"


def test_xbox_folders_need_a_wgs_index_and_report_their_twin(tmp_path, make_wgs_account):
    acct = make_wgs_account(tmp_path / "SystemAppData",
                            [("Slot4Auto", '{"a":1}', "Main", "", 60, 1_790_000_000.0)])
    twin = tmp_path / "SystemAppData" / "xgs" / XBOX_ACCOUNT / "Slot4Auto"
    twin.mkdir(parents=True)
    (twin / "data").write_bytes(b"x")
    xgs_only = tmp_path / "Other" / "xgs" / "0000000000000002_ABC"
    (xgs_only / "Slot1Auto").mkdir(parents=True)
    (xgs_only / "Slot1Auto" / "data").write_bytes(b"x")
    stray = make_wgs_account(tmp_path / "Copy" / "nested",
                             [("Slot1Auto", '{"a":1}', "Main", "", 60, 1_790_000_000.0)],
                             name="0000000000000003_DEF")
    moved = stray.parent.parent / "copied"          # a wgs account copied out of wgs
    stray.rename(moved)
    ctx = make_ctx(tmp_path, [live_source(acct, platform_name="xbox"),
                              live_source(xgs_only, platform_name="xbox", writable=False),
                              live_source(moved, platform_name="xbox")])

    rt = resolve_target(ctx, LiveTarget("xbox", XBOX_ACCOUNT, 4))
    assert (rt.cloud_mode, rt.xgs_twin) == ("xbox", twin.parent)
    assert rt.label == "Xbox slot 4 (Xbox 1)"
    assert code_of(ctx, LiveTarget("xbox", xgs_only.name, 1)) == "XGS_READ_ONLY"
    with pytest.raises(TargetError) as info:
        resolve_target(ctx, LiveTarget("xbox", xgs_only.name, 1))
    assert info.value.text == "This Xbox folder can only be read."
    assert code_of(ctx, LiveTarget("xbox", "copied", 1)) == "NOT_A_LIVE_FOLDER"
    (acct / "containers.index").write_bytes(b"\x01\x02")
    assert code_of(ctx, LiveTarget("xbox", XBOX_ACCOUNT, 4)) == "BAD_INDEX"


def test_account_labels_use_the_owners_alias_and_never_an_account_id(tmp_path, monkeypatch):
    one = steam_dir(tmp_path / "a")
    two = steam_dir(tmp_path / "b", STEAM_ACCOUNT_2)
    ctx = make_ctx(tmp_path, [live_source(one, label="Steam (76561198000000001)"),
                              live_source(two, label="Laptop")])
    assert resolve_target(ctx, LiveTarget("steam", STEAM_ACCOUNT, 2)).label == \
        "Steam slot 2 (Steam 1)"
    assert resolve_target(ctx, LiveTarget("steam", STEAM_ACCOUNT_2, 2)).label == \
        "Steam slot 2 (Laptop)"
    alias = aliases.AliasMap()
    alias.set("76561198000000001", "Main PC")
    monkeypatch.setattr(aliases, "_active", alias)
    assert resolve_account(ctx, LiveRef("steam", STEAM_ACCOUNT)).label == "Steam (Main PC)"
    alone = make_ctx(tmp_path, [live_source(one)])
    assert resolve_target(alone, LiveTarget("steam", STEAM_ACCOUNT, 2)).label == "Steam slot 2"
