"""The Steam <-> Xbox transfer (D5 3.5, F6) on synthetic Steam and Xbox folders: both
directions are validated, so it is an ordinary Place of the converted pair, into an empty
slot or replacing an occupied one (the evicted pair is kept in the vault first)."""
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
from nms_save_vault.core.liveops import transfer
from nms_save_vault.core.liveops.undo import plan_undo
from nms_save_vault.core.organizer.locator import Location
from nms_save_vault.core.organizer.model import LiveRef

from .conftest import steam_save
from .liveops_support import MAIN, STEAM_ACCOUNT, T0, XBOX_ACCOUNT, live_source, steam_world, tree_hash

STEAM = LiveRef("steam", STEAM_ACCOUNT)
XBOX = LiveRef("xbox", XBOX_ACCOUNT)
XPID = 0xA1B2C3D4_00000001


def codes(warnings) -> set[str]:
    return {w.code for w in warnings}


def _payload(name: str, pid: int, tag: str = "") -> str:
    return '{"<h0":{"Pk4":"%s","WmU":"0x%X"},"tag":"%s"}' % (name, pid, tag)


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Both:
    """A Steam world (slot 1 'Main' two members, slot 2 one member, slot 3 empty) and an Xbox
    account (slot 1 'Xmain' two members, slots 2.. empty)."""

    def __init__(self, tmp_path, make_wgs_account) -> None:
        self.world = steam_world(tmp_path)
        self.acct = make_wgs_account(tmp_path / "SystemAppData", [
            ("AccountData", '{"acct":1}', "", "", 0, T0 + 0.5),
            ("Slot1Auto", _payload("Xmain", XPID, "auto"), "Xmain", "", 600, T0 + 10.5),
            ("Slot1Manual", _payload("Xmain", XPID, "manual"), "Xmain", "", 500, T0 + 5.5)])
        (self.acct.parent.parent / "xgs" / self.acct.name).mkdir(parents=True)
        msstore.mirror_xgs(self.acct)
        old = T0 - 3600
        os.utime(self.acct / "containers.index", (old, old))
        self.world.sources.append(live_source(self.acct, platform_name="xbox"))
        self.ctx = self.world.refresh()

    @property
    def steam_dir(self) -> Path:
        return self.world.live

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
    return Both(tmp_path, make_wgs_account)


def run(b: Both, plan):
    return execute(b.ctx, plan, plan.confirmations)


def test_a_direction_without_a_record_keeps_the_refusal_and_the_locked_account(
        both, monkeypatch):
    monkeypatch.setattr(transfer, "VALIDATED", {("xbox", "steam"): transfer.VALIDATED[
        ("xbox", "steam")]})
    plan = plan_place(both.ctx, both.steam_source(), XBOX.target(2))
    assert "CROSS_PLATFORM_GATED" in codes(plan.blocked) and not plan.targets[0].writes
    assert execute(both.ctx, plan, plan.confirmations).status == "refused"
    picker = eviction_picker(both.ctx, both.steam_source(), STEAM)
    assert {c.account.platform: c.blocked for c in picker.other_accounts}["xbox"] ==         "CROSS_PLATFORM_GATED"
    other = eviction_picker(both.ctx, both.xbox_source(), XBOX)
    assert {c.account.platform: c.blocked for c in other.other_accounts}["steam"] == ""


def test_both_directions_are_offered_with_all_slots_and_no_experimental_note(both):
    picker = eviction_picker(both.ctx, both.steam_source(), STEAM)
    choice = next(c for c in picker.other_accounts if c.account.platform == "xbox")
    assert choice.blocked == "" and codes(choice.warnings) == {"OTHER_ACCOUNT"}
    xbox_slots = eviction_picker(both.ctx, both.steam_source(), XBOX)
    assert len(xbox_slots.candidates) == 15
    assert not any(c.blocked for c in xbox_slots.candidates)
    occupied = xbox_slots.candidates[0]
    assert occupied.kind == "place" and occupied.outcome.startswith("Swap into")


def test_a_cross_platform_place_carries_other_account_even_without_a_capture_account(both):
    src = dataclasses.replace(both.steam_source(), prefer=None)
    plan = plan_place(both.ctx, src, XBOX.target(2))
    assert not plan.blocked and plan.source.platform == "steam"
    assert "OTHER_ACCOUNT" in plan.confirmations
    assert [w.code for w in plan.warnings].count("OTHER_ACCOUNT") == 1


def test_steam_to_xbox_into_an_empty_slot(both):
    steam_before = tree_hash(both.steam_dir)
    src = both.steam_source()
    plan = plan_place(both.ctx, src, XBOX.target(2))
    assert plan.kind == "place_into_empty" and not plan.blocked
    assert "OTHER_ACCOUNT" in plan.confirmations
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
    outcome = run(both, plan_place(both.ctx, both.steam_source(), XBOX.target(2)))
    assert outcome.status == "done" and outcome.undo is None
    plan = plan_undo(both.ctx, outcome.op_id)
    assert "UNDO_WOULD_EMPTY" in codes(plan.blocked)


def test_a_failed_transfer_is_rolled_back_exactly(both):
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
    plan = plan_place(both.ctx, both.steam_source(2), XBOX.target(2))
    assert "SOURCE_NO_ID" in codes(plan.blocked)


def test_a_save_from_a_newer_game_is_refused(both):
    newer = gameversion.KNOWN["steam"].base_version + 1
    steam_save(both.steam_dir, 2, 0, "Second", 600, 0x0123456789ABCDEF, T0 + 10,
               base_version=newer)
    plan = plan_place(both.ctx, both.steam_source(2), XBOX.target(2))
    assert "GAME_NEWER" in codes(plan.blocked)
    assert execute(both.ctx, plan, plan.confirmations).status == "refused"


def test_a_meta_that_cannot_be_converted_is_refused(both):
    """Worlds I (a 296 B Xbox meta) has no certain mapping: refused, not guessed."""
    from .conftest import chunked, xbox_meta
    msstore.write_save(both.acct, "Slot2Auto", chunked(_payload("Old", XPID).encode()),
                       xbox_meta("Old", pid=XPID, ts=T0, length=296, play=5), T0 + 1.5,
                       create_if_missing=True)
    both.ctx = both.world.refresh()
    plan = plan_place(both.ctx, _single(both, 2), STEAM.target(3))
    assert codes(plan.blocked) & {"UNSUPPORTED_META", "CONVERT_UNSUPPORTED", "META_LAYOUT"}


def _single(b: Both, slot: int) -> PairSource:
    view = msstore.scan(b.acct).slots[slot]
    a = _sha(view.a.data_path)
    return PairSource(a, None, "A", Location(a, "live", XBOX, slot, "A", "xbox", False))


# --- recovery of an interrupted transfer ---------------------------------------------------

from nms_save_vault.core.liveops import SimulatedCrash                          # noqa: E402
from nms_save_vault.core.liveops import recovery                                # noqa: E402
from nms_save_vault.core.liveops import steamwrite                              # noqa: E402

STEAM_POINTS = ["after_preparing", "in_preserve", "after_snapshot", "after_writing", "swapped:1",
                "swapped:2", "swapped:3", "swapped:4", "after_validated", "after_oplog",
                "after_record"]
XBOX_POINTS = ["after_preparing", "in_preserve", "after_snapshot", "after_writing",
               "before_index", "after_index", "xgs:Slot2Auto", "xgs:Slot2Manual",
               "after_validated", "after_oplog", "after_record"]


def _contents(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): _sha(p) for p in sorted(root.rglob("*"))
            if p.is_file()}


def _crashed(b: Both, plan, point: str) -> str:
    b.ctx.faults = FaultInjector({point})
    with pytest.raises(SimulatedCrash):
        execute(b.ctx, plan, plan.confirmations)
    b.ctx.faults = None
    b.ctx = b.world.refresh()                       # "the next start"
    (op_id,) = jr.read_ops(b.ctx)
    return op_id


def _post(b: Both, op_id: str) -> dict:
    writing = jr.read_ops(b.ctx)[op_id].last("writing")["targets"][0]
    return {p: jr.member_write_from_json(w) for p, w in writing["post"].items()}


@pytest.mark.parametrize("choice", ["forward", "back"])
@pytest.mark.parametrize("point", STEAM_POINTS)
def test_a_crashed_xbox_to_steam_transfer_recovers_both_ways(both, point, choice):
    before = tree_hash(both.steam_dir)
    plan = plan_place(both.ctx, both.xbox_source(), STEAM.target(3))
    assert not plan.blocked
    op_id = _crashed(both, plan, point)
    items = recovery.pending_recovery(both.ctx)
    if items:
        (item,) = items
        assert item.recommended == "forward" or "back" in item.choices
        if choice not in item.choices:
            pytest.skip("this point offers only the other way")
        outcome = recovery.resolve_recovery(both.ctx, op_id, choice)
        assert outcome.status == ("done" if choice == "forward" else "rolled_back"), outcome
    status = jr.read_ops(both.ctx)[op_id].status
    if status == "abandoned" or choice == "back" and items:
        assert tree_hash(both.steam_dir) == before
    else:
        assert status == "done"
        for pos, mw in _post(both, op_id).items():
            data, meta_name = steamwrite.file_names(3, pos)
            assert _sha(both.steam_dir / data) == mw.vid
            assert _sha(both.steam_dir / meta_name) == mw.meta_disk_sha256
        assert savedir.scan_slot(both.steam_dir, 3).newest.label == "A"
    assert recovery.pending_recovery(both.ctx) == []
    assert not [p for p in both.steam_dir.iterdir() if p.name.startswith(".tmp_")]
    again = plan_place(both.ctx, both.xbox_source(), STEAM.target(3))
    assert "RECOVERY_PENDING" not in codes(again.blocked)


@pytest.mark.parametrize("choice", ["forward", "back"])
@pytest.mark.parametrize("point", XBOX_POINTS)
def test_a_crashed_steam_to_xbox_transfer_recovers_both_ways(both, point, choice):
    root = both.acct.parent.parent
    before = _contents(root)
    plan = plan_place(both.ctx, both.steam_source(), XBOX.target(2))
    assert not plan.blocked
    op_id = _crashed(both, plan, point)
    items = recovery.pending_recovery(both.ctx)
    if items:
        (item,) = items
        if choice not in item.choices:
            pytest.skip("this point offers only the other way")
        outcome = recovery.resolve_recovery(both.ctx, op_id, choice)
        assert outcome.status == ("done" if choice == "forward" else "rolled_back"), outcome
    status = jr.read_ops(both.ctx)[op_id].status
    if status == "abandoned" or choice == "back" and items:
        assert _contents(root) == before
    else:
        assert status == "done"
        view = msstore.scan(both.acct).slots[2]
        assert view.newest.label == "A"
        for pos, mw in _post(both, op_id).items():
            member = view.a if pos == "A" else view.b
            assert _sha(member.data_path) == mw.vid
            assert _sha(member.meta_path) == mw.meta_disk_sha256
        twin = root / "xgs" / both.acct.name
        assert (twin / "Slot2Auto" / "data").read_bytes() == Path(view.a.data_path).read_bytes()
    assert recovery.pending_recovery(both.ctx) == []
    again = plan_place(both.ctx, both.steam_source(), XBOX.target(2))
    assert "RECOVERY_PENDING" not in codes(again.blocked)


def test_a_new_xbox_record_names_itself_once_like_the_current_game(both):
    info, records = msstore.parse_containers_index(both.acct)
    for c in records:                                       # an older tree: doubled identifiers
        c.second_identifier = c.identifier
    (both.acct / "containers.index").write_bytes(msstore.build_containers_index(info, records))
    old = T0 - 3600
    os.utime(both.acct / "containers.index", (old, old))
    plan = plan_place(both.ctx, both.steam_source(), XBOX.target(2))
    assert run(both, plan).status == "done"
    _info, after = msstore.parse_containers_index(both.acct)
    forms = {c.identifier: bool(c.second_identifier) for c in after}
    assert forms["Slot2Auto"] is False and forms["Slot2Manual"] is False
    assert forms["Slot1Auto"] is True                        # an existing record keeps its form


# --- replacing an occupied slot ------------------------------------------------------------


def _kept_vids(b: Both, op_id: str) -> set[str]:
    """The versions the op preserved into the vault before it wrote anything live."""
    writing = jr.read_ops(b.ctx)[op_id].last("writing")["targets"][0]
    out = set()
    for kept in writing["preserved"]:
        loc = jr.location_from_json(kept["location"])
        assert b.ctx.versions.entry(loc.ref.key).kind == "preserve"
        assert b.ctx.locator.open(loc.vid, loc).location == loc
        out.add(loc.vid)
    return out


def test_steam_to_xbox_replaces_an_occupied_slot_and_keeps_the_evicted_pair(both):
    old = both.xbox_source()
    assert old.vid_a != old.vid_b
    src = both.steam_source()
    steam_before = tree_hash(both.steam_dir)
    plan = plan_place(both.ctx, src, XBOX.target(1))
    assert plan.kind == "place" and not plan.blocked
    assert "OTHER_ACCOUNT" in plan.confirmations
    assert plan.targets[0].evictions
    outcome = run(both, plan)
    assert outcome.status == "done", outcome
    assert outcome.undo is not None
    events = [e["event"] for e in jr.read_ops(both.ctx)[outcome.op_id].events]
    assert events == ["preparing", "writing", "validated", "done"]
    assert _kept_vids(both, outcome.op_id) == {old.vid_a, old.vid_b}

    view = msstore.scan(both.acct).slots[1]
    assert _sha(view.a.data_path) == src.vid_a and _sha(view.b.data_path) == src.vid_b
    raw = Path(view.a.meta_path).read_bytes()
    fields = msstore.parse_ms_meta(raw)
    assert len(raw) == 360 and (fields["name"], fields["slot_id"], fields["meta_format"]) == \
        ("Main", MAIN, 2004)
    twin = both.acct.parent.parent / "xgs" / both.acct.name
    assert (twin / "Slot1Auto" / "data").read_bytes() == Path(view.a.data_path).read_bytes()
    assert tree_hash(both.steam_dir) == steam_before


def test_xbox_to_steam_replaces_an_occupied_slot_and_keeps_the_evicted_pair(both):
    old = both.steam_source()
    src = both.xbox_source()
    xbox_before = tree_hash(both.acct.parent.parent)
    plan = plan_place(both.ctx, src, STEAM.target(1))
    assert plan.kind == "place" and not plan.blocked
    assert "OTHER_ACCOUNT" in plan.confirmations
    outcome = run(both, plan)
    assert outcome.status == "done", outcome
    assert _kept_vids(both, outcome.op_id) == {old.vid_a, old.vid_b}

    assert _sha(both.steam_dir / "save.hg") == src.vid_a
    assert _sha(both.steam_dir / "save2.hg") == src.vid_b
    for fno, name in zip(slotmap.slot_file_numbers(1), ("mf_save.hg", "mf_save2.hg")):
        ordinal = slotmap.storage_ordinal(fno)
        raw = (both.steam_dir / name).read_bytes()
        assert len(raw) == 432 and meta.is_valid_for(raw, ordinal)
        info = meta.parse(meta.decrypt(raw, ordinal), ordinal)
        assert (info.save_name, info.slot_identifier) == ("Xmain", XPID)
    assert tree_hash(both.acct.parent.parent) == xbox_before


def test_an_occupied_target_still_needs_the_source_checks(both):
    steam_save(both.steam_dir, 2, 0, "Second", 600, 0x0123456789ABCDEF, T0 + 10,
               data_ident=0x1111)
    plan = plan_place(both.ctx, both.steam_source(2), XBOX.target(1))
    assert "SOURCE_NO_ID" in codes(plan.blocked)
    assert execute(both.ctx, plan, plan.confirmations).status == "refused"


def test_undo_after_a_replace_puts_the_evicted_version_back(both):
    old = both.xbox_source()
    outcome = run(both, plan_place(both.ctx, both.steam_source(), XBOX.target(1)))
    assert outcome.status == "done" and outcome.undo is not None
    both.ctx = both.world.refresh()
    undo = plan_undo(both.ctx, outcome.op_id)
    assert not undo.blocked and undo.kind == "undo"
    assert undo.source.platform == "xbox"                       # a same-platform place
    assert execute(both.ctx, undo, undo.confirmations).status == "done"
    view = msstore.scan(both.acct).slots[1]
    assert (_sha(view.a.data_path), _sha(view.b.data_path)) == (old.vid_a, old.vid_b)


def test_undo_after_a_steam_replace_puts_the_evicted_version_back(both):
    old = both.steam_source()
    outcome = run(both, plan_place(both.ctx, both.xbox_source(), STEAM.target(1)))
    assert outcome.status == "done" and outcome.undo is not None
    both.ctx = both.world.refresh()
    undo = plan_undo(both.ctx, outcome.op_id)
    assert not undo.blocked and undo.source.platform == "steam"
    assert execute(both.ctx, undo, undo.confirmations).status == "done"
    assert (_sha(both.steam_dir / "save.hg"), _sha(both.steam_dir / "save2.hg")) == \
        (old.vid_a, old.vid_b)


STEAM_TARGET_POINTS = ["in_preserve", "after_writing", "swapped:1", "swapped:2", "swapped:3",
                       "swapped:4", "after_validated"]
XBOX_TARGET_POINTS = ["in_preserve", "after_writing", "before_index", "after_index",
                      "xgs:Slot1Auto", "xgs:Slot1Manual", "after_validated"]
REPLACE_CASES = [("steam_to_xbox", p) for p in XBOX_TARGET_POINTS] +     [("xbox_to_steam", p) for p in STEAM_TARGET_POINTS]


@pytest.mark.parametrize("choice", ["forward", "back"])
@pytest.mark.parametrize("direction,point", REPLACE_CASES)
def test_a_crashed_replace_transfer_recovers_both_ways(both, point, choice, direction):
    if direction == "steam_to_xbox":
        src, old, target, root = both.steam_source(), both.xbox_source(), XBOX.target(1),             both.acct.parent.parent
    else:
        src, old, target, root = both.xbox_source(), both.steam_source(), STEAM.target(1),             both.steam_dir
    before = _contents(root)
    plan = plan_place(both.ctx, src, target)
    assert not plan.blocked
    op_id = _crashed(both, plan, point)
    writing = jr.read_ops(both.ctx)[op_id].last("writing")
    if writing is not None:                  # the evicted pair is in the vault before any write
        kept = {jr.location_from_json(k["location"]).vid
                for k in writing["targets"][0]["preserved"]}
        assert kept == {old.vid_a, old.vid_b}
    items = recovery.pending_recovery(both.ctx)
    if items:
        (item,) = items
        if choice not in item.choices:
            pytest.skip("this point offers only the other way")
        outcome = recovery.resolve_recovery(both.ctx, op_id, choice)
        assert outcome.status == ("done" if choice == "forward" else "rolled_back"), outcome
    status = jr.read_ops(both.ctx)[op_id].status
    if status == "abandoned" or choice == "back" and items:
        assert _contents(root) == before
    else:
        assert status == "done"
        for pos, mw in _post(both, op_id).items():
            if target.platform == "steam":
                data, meta_name = steamwrite.file_names(1, pos)
                assert _sha(both.steam_dir / data) == mw.vid
                assert _sha(both.steam_dir / meta_name) == mw.meta_disk_sha256
            else:
                member = getattr(msstore.scan(both.acct).slots[1], pos.lower())
                assert _sha(member.data_path) == mw.vid
                assert _sha(member.meta_path) == mw.meta_disk_sha256
    assert recovery.pending_recovery(both.ctx) == []


def test_a_save_in_an_unvalidated_format_is_refused_by_the_record(both, monkeypatch):
    plan = plan_place(both.ctx, both.steam_source(), XBOX.target(2))
    assert not plan.blocked
    rec = transfer.VALIDATED[("steam", "xbox")]
    monkeypatch.setitem(transfer.VALIDATED, ("steam", "xbox"),
                        dataclasses.replace(rec, meta_format=2003))
    plan = plan_place(both.ctx, both.steam_source(), XBOX.target(2))
    assert "UNSUPPORTED_META" in codes(plan.blocked)


def test_a_steam_save_with_only_xbox_live_still_defaults_to_xbox(both):
    from nms_save_vault.core.organizer.model import StoreRef
    from nms_save_vault.core.store import CaptureScope

    src = both.steam_source()
    entry = both.ctx.versions.add_from_live(both.steam_dir, platform="steam",
                                            account_key=both.steam_dir.name, kind="full",
                                            label="old steam", scope=CaptureScope())
    both.world.sources[:] = [s for s in both.world.sources if s.platform == "xbox"]
    ctx = both.world.refresh()
    where = Location(src.vid_a, "store", StoreRef(entry.id), 1, "A", "steam", True)
    picker = eviction_picker(ctx, PairSource(src.vid_a, src.vid_b, None, where))
    assert picker.account.platform == "xbox"
    assert all(c.blocked == "" for c in picker.candidates)
