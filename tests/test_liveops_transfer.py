"""The gated Steam <-> Xbox transfer (D5 3.5, F6) on synthetic Steam and Xbox folders: off by
default, one gate (NMSVAULT_CROSS_PLATFORM=1) opens it, only into an empty slot of a folder
with a fresh full backup, and then it is an ordinary Place of the converted pair."""
from __future__ import annotations

import dataclasses
import hashlib
import os
import struct
from pathlib import Path

import pytest

from nms_save_vault.core import formats, gameversion, meta, msstore, savedir, slotmap
from nms_save_vault.core.liveops import FaultInjector, PairSource, eviction_picker, execute
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops.planner import plan_place
from nms_save_vault.core.liveops.transfer import ENV_GATE
from nms_save_vault.core.liveops.undo import plan_undo
from nms_save_vault.core.organizer.locator import Location
from nms_save_vault.core.organizer.model import LiveRef
from nms_save_vault.core.store import CaptureScope

from .conftest import steam_save
from .liveops_support import MAIN, STEAM_ACCOUNT, T0, XBOX_ACCOUNT, live_source, steam_world, tree_hash

STEAM = LiveRef("steam", STEAM_ACCOUNT)
XBOX = LiveRef("xbox", XBOX_ACCOUNT)
XPID = 0xA1B2C3D4_00000001


def codes(warnings) -> set[str]:
    return {w.code for w in warnings}


def _payload(name: str, pid: int) -> str:
    return '{"<h0":{"Pk4":"%s","WmU":"0x%X"}}' % (name, pid)


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Both:
    """A Steam world (slot 1 'Main' two members, slot 2 one member, slot 3 empty) and an Xbox
    account (slot 1 'Xmain' two members, slots 2.. empty), the gate on or off."""

    def __init__(self, tmp_path, make_wgs_account, *, gate: bool) -> None:
        self.world = steam_world(tmp_path)
        self.acct = make_wgs_account(tmp_path / "SystemAppData", [
            ("AccountData", '{"acct":1}', "", "", 0, T0 + 0.5),
            ("Slot1Auto", _payload("Xmain", XPID), "Xmain", "", 600, T0 + 10.5),
            ("Slot1Manual", _payload("Xmain", XPID), "Xmain", "", 500, T0 + 5.5)])
        (self.acct.parent.parent / "xgs" / self.acct.name).mkdir(parents=True)
        msstore.mirror_xgs(self.acct)
        old = T0 - 3600
        os.utime(self.acct / "containers.index", (old, old))
        self.world.sources.append(live_source(self.acct, platform_name="xbox"))
        self.gate(gate)

    def gate(self, on: bool) -> None:
        env = dict(self.world.host.env)
        env.pop(ENV_GATE, None)
        if on:
            env[ENV_GATE] = "1"
        self.world.host = dataclasses.replace(self.world.host, env=env)
        self.ctx = self.world.refresh()

    @property
    def steam_dir(self) -> Path:
        return self.world.live

    def backup(self, folder: Path, platform: str):
        entry = self.ctx.versions.add_from_live(folder, platform=platform,
                                                account_key=folder.name, kind="full",
                                                label="before the run", scope=CaptureScope())
        self.ctx = self.world.refresh()
        return entry

    def steam_source(self, slot: int = 1) -> PairSource:
        files = {1: ("save.hg", "save2.hg"), 2: ("save3.hg", None)}[slot]
        a = _sha(self.steam_dir / files[0])
        b = _sha(self.steam_dir / files[1]) if files[1] else None
        return PairSource(a, b, "A", Location(a, "live", STEAM, slot, "A", "steam", False))

    def xbox_source(self, slot: int = 1) -> PairSource:
        view = msstore.scan(self.acct).slots[slot]
        a, b = _sha(view.a.data_path), _sha(view.b.data_path)
        return PairSource(a, b, view.newest.label,
                          Location(a, "live", XBOX, slot, "A", "xbox", False))


@pytest.fixture
def both(tmp_path, make_wgs_account):
    return Both(tmp_path, make_wgs_account, gate=True)


def run(b: Both, plan):
    return execute(b.ctx, plan, plan.confirmations)


def test_gate_off_keeps_the_refusal_and_the_locked_account(tmp_path, make_wgs_account):
    b = Both(tmp_path, make_wgs_account, gate=False)
    b.backup(b.acct, "xbox")
    plan = plan_place(b.ctx, b.steam_source(), XBOX.target(2))
    assert "CROSS_PLATFORM_GATED" in codes(plan.blocked) and not plan.targets[0].writes
    assert execute(b.ctx, plan, plan.confirmations).status == "refused"
    picker = eviction_picker(b.ctx, b.steam_source(), STEAM)
    assert {c.account.platform: c.blocked for c in picker.other_accounts}["xbox"] == \
        "CROSS_PLATFORM_GATED"
    other = eviction_picker(b.ctx, b.xbox_source(), XBOX)
    assert {c.account.platform: c.blocked for c in other.other_accounts}["steam"] == \
        "CROSS_PLATFORM_GATED"


def test_gate_on_offers_the_other_platform_as_experimental(both):
    picker = eviction_picker(both.ctx, both.steam_source(), STEAM)
    choice = next(c for c in picker.other_accounts if c.account.platform == "xbox")
    assert choice.blocked == "" and "CROSS_PLATFORM_EXPERIMENTAL" in codes(choice.warnings)


def test_it_needs_a_fresh_full_backup_of_the_destination(both):
    plan = plan_place(both.ctx, both.steam_source(), XBOX.target(2))
    assert codes(plan.blocked) == {"CROSS_NEEDS_BACKUP"}
    both.backup(both.steam_dir, "steam")                       # the wrong folder does not count
    plan = plan_place(both.ctx, both.steam_source(), XBOX.target(2))
    assert "CROSS_NEEDS_BACKUP" in codes(plan.blocked)
    both.backup(both.acct, "xbox")
    plan = plan_place(both.ctx, both.steam_source(), XBOX.target(2))
    assert not plan.blocked


def test_a_non_empty_target_is_refused(both):
    both.backup(both.acct, "xbox")
    both.backup(both.steam_dir, "steam")
    plan = plan_place(both.ctx, both.steam_source(), XBOX.target(1))
    assert "CROSS_TARGET_NOT_EMPTY" in codes(plan.blocked)
    assert execute(both.ctx, plan, plan.confirmations).status == "refused"
    plan = plan_place(both.ctx, both.xbox_source(), STEAM.target(1))
    assert "CROSS_TARGET_NOT_EMPTY" in codes(plan.blocked)
    picker = eviction_picker(both.ctx, both.steam_source(), XBOX)
    assert picker.candidates[0].blocked == "CROSS_TARGET_NOT_EMPTY"
    assert picker.suggested_slot == 2 and picker.candidates[1].blocked == ""


def test_steam_to_xbox_into_an_empty_slot(both):
    both.backup(both.acct, "xbox")
    steam_before = tree_hash(both.steam_dir)
    src = both.steam_source()
    plan = plan_place(both.ctx, src, XBOX.target(2))
    assert plan.kind == "place_into_empty" and not plan.blocked
    assert {"CROSS_PLATFORM_EXPERIMENTAL", "OTHER_ACCOUNT"} <= plan.confirmations
    outcome = run(both, plan)
    assert outcome.status == "done", outcome

    view = msstore.scan(both.acct).slots[2]
    assert view.newest.label == "A"
    assert _sha(view.a.data_path) == src.vid_a and _sha(view.b.data_path) == src.vid_b
    fields = msstore.parse_ms_meta(Path(view.a.meta_path).read_bytes())
    assert (fields["name"], fields["slot_id"], fields["meta_format"], fields["difficulty"]) == \
        ("Main", MAIN, 2004, 2)
    assert len(Path(view.a.meta_path).read_bytes()) == 360
    assert view.a.xbox.sync_state == msstore.BLOB_SYNC_CREATED
    assert tree_hash(both.steam_dir) == steam_before                  # the source is not touched
    events = [e["event"] for e in jr.read_ops(both.ctx)[outcome.op_id].events]
    assert events == ["preparing", "writing", "validated", "done"]
    assert [r for r in both.ctx.vault.read_oplog()][-1]["status"] == "done"
    assert both.ctx.versions.entry(
        [r for r in both.ctx.vault.read_oplog()][-1]["snapshot_id"]).kind == "snapshot"


def test_xbox_to_steam_into_an_empty_slot(both):
    both.backup(both.steam_dir, "steam")
    xbox_before = tree_hash(both.acct.parent.parent)
    src = both.xbox_source()
    plan = plan_place(both.ctx, src, STEAM.target(3))
    assert plan.kind == "place_into_empty" and not plan.blocked
    outcome = run(both, plan)
    assert outcome.status == "done", outcome

    slot = savedir.scan_slot(both.steam_dir, 3)
    assert slot.newest.label == "A"
    assert _sha(both.steam_dir / "save5.hg") == src.vid_a
    assert _sha(both.steam_dir / "save6.hg") == src.vid_b
    for fno, name in zip(slotmap.slot_file_numbers(3), ("mf_save5.hg", "mf_save6.hg")):
        ordinal = slotmap.storage_ordinal(fno)
        raw = (both.steam_dir / name).read_bytes()
        assert len(raw) == 432 and meta.is_valid_for(raw, ordinal)
        info = meta.parse(meta.decrypt(raw, ordinal), ordinal)
        assert (info.save_name, info.slot_identifier, info.meta_format) == ("Xmain", XPID, 2004)
    assert tree_hash(both.acct.parent.parent) == xbox_before


def test_a_fill_cannot_be_undone_because_a_slot_is_never_emptied(both):
    both.backup(both.acct, "xbox")
    outcome = run(both, plan_place(both.ctx, both.steam_source(), XBOX.target(2)))
    assert outcome.status == "done" and outcome.undo is None
    plan = plan_undo(both.ctx, outcome.op_id)
    assert "UNDO_WOULD_EMPTY" in codes(plan.blocked)


def test_a_failed_transfer_is_rolled_back_exactly(both):
    both.backup(both.acct, "xbox")
    root = both.acct.parent.parent
    before = {p.relative_to(root).as_posix(): _sha(p) for p in sorted(root.rglob("*"))
              if p.is_file()}

    def corrupt():
        Path(msstore.scan(both.acct).slots[2].a.data_path).write_bytes(b"not the save")

    plan = plan_place(both.ctx, both.steam_source(), XBOX.target(2))
    both.ctx.faults = FaultInjector(actions={"after_index": corrupt})
    outcome = execute(both.ctx, plan, plan.confirmations)
    assert outcome.status == "rolled_back", outcome
    after = {p.relative_to(root).as_posix(): _sha(p) for p in sorted(root.rglob("*"))
             if p.is_file()}
    assert after == before


def test_a_save_without_a_matching_id_is_refused(both):
    steam_save(both.steam_dir, 2, 0, "Second", 600, 0x0123456789ABCDEF, T0 + 10,
               data_ident=0x1111)                                   # the data names another id
    both.backup(both.acct, "xbox")
    plan = plan_place(both.ctx, both.steam_source(2), XBOX.target(2))
    assert "SOURCE_NO_ID" in codes(plan.blocked)


def test_a_save_from_a_newer_game_is_refused(both):
    newer = gameversion.KNOWN["steam"].base_version + 1
    steam_save(both.steam_dir, 2, 0, "Second", 600, 0x0123456789ABCDEF, T0 + 10,
               base_version=newer)
    both.backup(both.acct, "xbox")
    plan = plan_place(both.ctx, both.steam_source(2), XBOX.target(2))
    assert "GAME_NEWER" in codes(plan.blocked)
    assert execute(both.ctx, plan, plan.confirmations).status == "refused"


def test_a_meta_that_cannot_be_converted_is_refused(both):
    """Worlds I (a 296 B Xbox meta) has no certain mapping: refused, not guessed."""
    from .conftest import chunked, xbox_meta
    msstore.write_save(both.acct, "Slot2Auto", chunked(_payload("Old", XPID).encode()),
                       xbox_meta("Old", pid=XPID, ts=T0, length=296, play=5), T0 + 1.5,
                       create_if_missing=True)
    both.backup(both.steam_dir, "steam")
    both.ctx = both.world.refresh()
    plan = plan_place(both.ctx, _single(both, 2), STEAM.target(3))
    assert codes(plan.blocked) & {"UNSUPPORTED_META", "CONVERT_UNSUPPORTED", "META_LAYOUT"}


def _single(b: Both, slot: int) -> PairSource:
    view = msstore.scan(b.acct).slots[slot]
    a = _sha(view.a.data_path)
    return PairSource(a, None, "A", Location(a, "live", XBOX, slot, "A", "xbox", False))
