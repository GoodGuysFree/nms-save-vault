"""liveops.steamcloud: the remotecache.vdf reader and its two uses (T-VR3 Steam variant,
the Steam PENDING_UPLOAD hint). Synthetic fixtures only; every path is under tmp_path."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nms_save_vault.core import platform
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops import steamcloud, update_verification
from nms_save_vault.core.liveops.execute import execute
from nms_save_vault.core.liveops.planner import plan_place, plan_promote
from nms_save_vault.core.organizer.model import LiveRef

from .liveops_support import STEAM_ACCOUNT, steam_world

ID64 = int(STEAM_ACCOUNT[3:])
ID32 = ID64 - steamcloud.STEAM_ID64_BASE
LIVE = LiveRef("steam", STEAM_ACCOUNT)
TARGET = LIVE.target(1)
SLOT1 = ("save.hg", "mf_save.hg", "save2.hg", "mf_save2.hg")


def vdf(files: dict[str, tuple[str, int]]) -> str:
    """A remotecache.vdf body: file name -> (sha, syncstate)."""
    out = ['"275850"', "{", '\t"ChangeNumber"\t\t"7"', '\t"OSType"\t\t"0"']
    for name, (sha, state) in files.items():
        out += [f'\t"HelloGames/NMS/{STEAM_ACCOUNT}/{name}"', "\t{", '\t\t"root"\t\t"4"',
                '\t\t"size"\t\t"10"', '\t\t"remotetime"\t\t"123"', f'\t\t"sha"\t\t"{sha}"',
                f'\t\t"syncstate"\t\t"{state}"', "\t}"]
    out.append("}")
    return "\n".join(out) + "\n"


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def install(world, files: dict[str, tuple[str, int]] | None) -> None:
    """A Steam install under the sandbox and a host that finds it."""
    root = world.tmp / "pf" / "Steam"
    if files is not None:
        d = root / "userdata" / str(ID32) / "275850"
        d.mkdir(parents=True, exist_ok=True)
        (d / "remotecache.vdf").write_text(vdf(files), encoding="utf-8")
    elif root.exists():
        (root / "userdata" / str(ID32) / "275850" / "remotecache.vdf").unlink()
    (world.tmp / "pf" / "Steam").mkdir(parents=True, exist_ok=True)
    world.ctx.host = platform.Host(platform.WINDOWS, world.tmp / "home",
                                   {"ProgramFiles(x86)": str(world.tmp / "pf")})


def synced_all(world) -> dict[str, tuple[str, int]]:
    return {n: (sha1(world.live / n), 1) for n in SLOT1}


def codes(plan) -> set[str]:
    return {w.code for w in plan.warnings}


def test_parse_vdf_nesting_escapes_and_comments():
    tree = steamcloud.parse_vdf('"a" { "k" "v\\"q" // note\n "b" { "x" "1" } }\n"c" "d"')
    assert tree == {"a": {"k": 'v"q', "b": {"x": "1"}}, "c": "d"}
    for bad in ('"a" { "k" "v"', '"a" "b" }', '{ "k" "v" }', '"a" { "k" }'):
        with pytest.raises(ValueError):
            steamcloud.parse_vdf(bad)


def test_manifest_entries_and_unreadable_cases(tmp_path):
    d = tmp_path / "userdata" / "5" / "275850"
    d.mkdir(parents=True)
    assert steamcloud.read_manifest(tmp_path, 5) == {}                  # absent
    (d / "remotecache.vdf").write_text(vdf({"save.hg": ("AB" * 20, 1)}), encoding="utf-8")
    (entry,) = steamcloud.read_manifest(tmp_path, 5).values()
    assert entry.sha == "ab" * 20 and entry.synced
    (d / "remotecache.vdf").write_text('"275850" { "x"', encoding="utf-8")
    assert steamcloud.read_manifest(tmp_path, 5) == {}                  # unparsable
    assert steamcloud.accountid32(STEAM_ACCOUNT) == ID32
    assert steamcloud.accountid32("0000000000000001_29070100") is None


def test_steam_upload_confirms_a_placed_pair(tmp_path):
    """T-VR3, Steam variant: Steam's record matching our files is "uploaded"."""
    world = steam_world(tmp_path)
    plan = plan_place(world.ctx, world.polestar(), TARGET)
    out = execute(world.ctx, plan, plan.confirmations)
    world.refresh()
    install(world, {**synced_all(world), "save.hg": ("0" * 40, 1)})      # stale sha: not yet
    assert [i.state for i in update_verification(world.ctx)] == ["pending"]
    install(world, synced_all(world))
    assert update_verification(world.ctx) == []
    rec = jr.read_ops(world.ctx)[out.op_id]
    assert rec.verify_state(TARGET) == "confirmed"
    assert [e for e in rec.events if e["event"] == "verify"][-1]["detail"] == "uploaded"


def test_no_manifest_or_unsynced_entries_give_no_upload_evidence(tmp_path):
    world = steam_world(tmp_path)
    plan = plan_place(world.ctx, world.polestar(), TARGET)
    execute(world.ctx, plan, plan.confirmations)
    world.refresh()
    install(world, None)                                                 # no manifest file
    assert [i.state for i in update_verification(world.ctx)] == ["pending"]
    install(world, {n: (s, 5) for n, (s, _) in synced_all(world).items()})   # syncstate != 1
    assert [i.state for i in update_verification(world.ctx)] == ["pending"]


def test_pending_upload_warning_on_steam(tmp_path):
    world = steam_world(tmp_path)
    install(world, synced_all(world))
    assert "PENDING_UPLOAD" not in codes(plan_promote(world.ctx, TARGET, "B"))
    install(world, {**synced_all(world), "mf_save2.hg": ("0" * 40, 1)})
    assert "PENDING_UPLOAD" in codes(plan_promote(world.ctx, TARGET, "B"))
    install(world, None)
    assert "PENDING_UPLOAD" not in codes(plan_promote(world.ctx, TARGET, "B"))
