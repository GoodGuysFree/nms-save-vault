"""msstore pair writer: both members of an Xbox slot with one containers.index write, an
exact rollback, and a guard against the Xbox app writing the index meanwhile (D5 2.6.2;
T-X2, T-X3, T-X4, T-X5, crash points before_index / after_index / xgs:<id>)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from nms_save_vault.core import msstore
from nms_save_vault.core.liveops import FaultInjector, SimulatedCrash

from .conftest import chunked, xbox_meta

NOW = 1_792_500_903.0
T0 = 1_790_000_000.1234567          # sub-second FILETIMEs: a float round trip would move them


def account(tmp_path: Path, make_wgs_account, *, xgs: bool = True) -> Path:
    acct = make_wgs_account(tmp_path / "SystemAppData", [
        ("AccountData", '{"acct":1}', "", "", 0, T0),
        ("Slot1Auto", '{"s1a":1}', "Polestar", "", 100, T0 + 10.5),
        ("Slot1Manual", '{"s1m":1}', "Polestar", "", 90, T0 + 5.25),
        ("Slot2Auto", '{"s2a":1}', "Other", "", 50, T0 + 1.75),
    ])
    _set_state(acct, "Slot1Auto", msstore.BLOB_SYNC_SYNCED)
    if xgs:
        (acct.parent.parent / "xgs" / acct.name).mkdir(parents=True)
        msstore.mirror_xgs(acct)
    return acct


def _set_state(acct: Path, identifier: str, state: int) -> None:
    """Edit one record's sync state in place, as the Xbox app does after an upload."""
    info, containers = msstore.parse_containers_index(acct)
    next(c for c in containers if c.identifier == identifier).sync_state = state
    (acct / "containers.index").write_bytes(msstore.build_containers_index(info, containers))


def pair(slot: int = 1, *, tag: str = "main") -> list[msstore.XboxRecordWrite]:
    """'Main Save' for ``slot``: Auto loads (now), Manual is the other member (now - 1)."""
    out = []
    for ident, when, play in ((f"Slot{slot}Manual", NOW - 1, 3600), (f"Slot{slot}Auto", NOW, 3700)):
        body = '{"%s":"%s"}' % (ident, tag)
        out.append(msstore.XboxRecordWrite(
            ident, chunked(body.encode()),
            xbox_meta("Main Save", play=play, size=len(body), pid=0xDDDAD1BDD39F9C72,
                      ts=int(when)), when))
    return out


def payload(writes) -> dict:
    return {w.identifier: (w.data, w.meta) for w in writes}


def records(acct: Path) -> dict[str, bytes]:
    _h, recs, _t = msstore.split_index((acct / "containers.index").read_bytes())
    return dict(recs)


def contents(root: Path) -> dict[str, str]:
    """sha256 of every file under ``root`` (the wgs and xgs trees together)."""
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def by_id(acct: Path) -> dict:
    _info, containers = msstore.parse_containers_index(acct)
    return {c.identifier: c for c in containers}


def test_one_index_write_flips_both_records_and_leaves_the_rest_raw(tmp_path, make_wgs_account,
                                                                   monkeypatch):
    """T-X2 and T-X3."""
    acct = account(tmp_path, make_wgs_account)
    before_raw = (acct / "containers.index").read_bytes()
    header_before, _r, _t = msstore.split_index(before_raw)
    before = records(acct)
    old = by_id(acct)
    writes = pair()
    plan = msstore.prepare_pair(acct, writes)
    assert (acct / "containers.index").read_bytes() == before_raw    # prepare reads only

    index_writes = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: (
        index_writes.append(1) if Path(d).name == "containers.index" else None,
        real_replace(s, d))[1])
    msstore.apply_pair(plan, payload(writes))
    assert len(index_writes) == 1

    after = records(acct)
    assert {k: v for k, v in after.items() if k not in ("Slot1Auto", "Slot1Manual")} == \
        {k: v for k, v in before.items() if k not in ("Slot1Auto", "Slot1Manual")}
    header_after, _r, _t = msstore.split_index((acct / "containers.index").read_bytes())
    proc_len = 4 + 2 * 4                                   # "proc"
    assert header_after[:proc_len] == header_before[:proc_len]           # identity kept
    assert header_after[proc_len + 12:] == header_before[proc_len + 12:]
    now = by_id(acct)
    assert now["Slot1Auto"].sync_state == now["Slot1Manual"].sync_state == msstore.BLOB_SYNC_MODIFIED
    assert now["Slot1Auto"].filetime > now["Slot1Manual"].filetime > old["Slot1Manual"].filetime
    view = msstore.scan(acct).slots[1]
    assert view.a.data_path.read_bytes() == writes[1].data
    assert view.b.meta_path.read_bytes() == writes[0].meta
    assert view.newest.label == "A"
    twin = acct.parent.parent / "xgs" / acct.name
    assert (twin / "Slot1Auto" / "data").read_bytes() == writes[1].data
    for rel in plan.superseded:                            # the old blobs wait for finalize
        assert (acct / rel).is_file()
    msstore.finalize_pair(plan)
    assert not any((acct / rel).exists() for rel in plan.superseded)
    assert msstore.scan(acct).slots[1].a.data_path.read_bytes() == writes[1].data


def test_a_new_save_is_created_and_a_deleted_one_comes_back(tmp_path, make_wgs_account):
    acct = account(tmp_path, make_wgs_account)
    msstore.delete_save(acct, "Slot2Auto", T0 + 100)
    writes = [w for w in pair(2)]
    plan = msstore.prepare_pair(acct, writes)
    assert plan.pre_records_raw["Slot2Manual"] is None and len(plan.new_dirs) == 2
    msstore.apply_pair(plan, payload(writes))
    now = by_id(acct)
    assert now["Slot2Manual"].sync_state == msstore.BLOB_SYNC_CREATED
    assert now["Slot2Auto"].sync_state == msstore.BLOB_SYNC_MODIFIED     # un-deleted
    view = msstore.scan(acct).slots[2]
    assert view.newest.label == "A" and view.b.data_path.read_bytes() == writes[0].data


@pytest.mark.parametrize("crash", [None, "before_index", "after_index", "xgs:Slot{}Manual",
                                   "xgs:Slot{}Auto"])
@pytest.mark.parametrize("slot", [1, 2])     # an existing pair; an existing + a new identifier
def test_rollback_restores_the_index_bytes_and_both_trees(tmp_path, make_wgs_account, crash, slot):
    """T-X4: after a finished write or a crash at any point."""
    acct = account(tmp_path, make_wgs_account)
    root = tmp_path / "SystemAppData"
    before_raw = (acct / "containers.index").read_bytes()
    before = contents(root)
    writes = pair(slot)
    plan = msstore.prepare_pair(acct, writes)
    if crash is None:
        msstore.apply_pair(plan, payload(writes))
    else:
        with pytest.raises(SimulatedCrash):
            msstore.apply_pair(plan, payload(writes), FaultInjector({crash.format(slot)}))
    msstore.rollback_pair(plan)
    assert (acct / "containers.index").read_bytes() == before_raw
    assert contents(root) == before
    assert not [d for d in acct.iterdir() if d.is_dir() and not any(d.iterdir())]


def test_an_index_changed_meanwhile_aborts_before_anything_is_referenced(tmp_path, make_wgs_account):
    """T-X5: the Xbox app records an upload between plan and write."""
    acct = account(tmp_path, make_wgs_account)
    writes = pair()
    plan = msstore.prepare_pair(acct, writes)
    _set_state(acct, "Slot2Auto", msstore.BLOB_SYNC_SYNCED)
    synced_raw = (acct / "containers.index").read_bytes()
    before = contents(tmp_path / "SystemAppData")
    with pytest.raises(msstore.IndexChanged):
        msstore.apply_pair(plan, payload(writes))
    assert (acct / "containers.index").read_bytes() == synced_raw
    assert contents(tmp_path / "SystemAppData") == before          # new blobs removed again
    assert not any((acct / rel).exists() for rel in plan.new_files)


def test_wrong_bytes_and_account_records_are_refused(tmp_path, make_wgs_account):
    acct = account(tmp_path, make_wgs_account, xgs=False)
    before = contents(tmp_path / "SystemAppData")
    writes = pair()
    with pytest.raises(ValueError):
        msstore.prepare_pair(acct, [msstore.XboxRecordWrite("AccountData", b"x", b"y", NOW)])
    with pytest.raises(ValueError):
        msstore.prepare_pair(acct, writes + writes[:1])
    plan = msstore.prepare_pair(acct, writes)
    bad = payload(writes)
    bad["Slot1Auto"] = (b"other bytes", bad["Slot1Auto"][1])
    with pytest.raises(ValueError):
        msstore.apply_pair(plan, bad)
    assert contents(tmp_path / "SystemAppData") == before
    assert plan.xgs is False


def test_filetimes_never_go_back_and_keep_the_loading_member_last(tmp_path, make_wgs_account):
    """A record stamped by a device whose clock ran ahead: the new FILETIMEs still climb
    past it, and the loading member stays strictly newest."""
    acct = account(tmp_path, make_wgs_account)
    info, containers = msstore.parse_containers_index(acct)
    ahead = msstore._unix_to_filetime(NOW + 3600)
    next(c for c in containers if c.identifier == "Slot1Manual").filetime = ahead
    (acct / "containers.index").write_bytes(msstore.build_containers_index(info, containers))
    writes = pair()
    msstore.apply_pair(msstore.prepare_pair(acct, writes), payload(writes))
    now = by_id(acct)
    assert now["Slot1Manual"].filetime == ahead + 1
    assert now["Slot1Auto"].filetime == ahead + 2


# --- review fixes -----------------------------------------------------------------------


def written(tmp_path, make_wgs_account, slot: int = 1):
    acct = account(tmp_path, make_wgs_account)
    writes = pair(slot)
    plan = msstore.prepare_pair(acct, writes)
    return acct, writes, plan


def test_rollback_after_finalize_refuses_and_keeps_the_written_slot(tmp_path, make_wgs_account):
    """Review item 1: the replaced blobs are gone, so the old records would point at
    nothing; the rollback refuses and changes nothing."""
    acct, writes, plan = written(tmp_path, make_wgs_account)
    msstore.apply_pair(plan, payload(writes))
    msstore.finalize_pair(plan)
    after = contents(tmp_path / "SystemAppData")
    with pytest.raises(msstore.RollbackError, match="finalized"):
        msstore.rollback_pair(plan)
    assert contents(tmp_path / "SystemAppData") == after
    assert msstore.scan(acct).slots[1].a.data_path.read_bytes() == writes[1].data


def test_rollback_refuses_an_index_someone_else_changed(tmp_path, make_wgs_account):
    """Review item 2: the index is neither as planned nor as the write left it (the Xbox
    app recorded an upload since): IndexChanged, nothing touched."""
    acct, writes, plan = written(tmp_path, make_wgs_account)
    assert msstore.pair_index_state(plan) == "pre"
    msstore.apply_pair(plan, payload(writes))
    assert msstore.pair_index_state(plan) == "post"
    _set_state(acct, "Slot2Auto", msstore.BLOB_SYNC_SYNCED)
    assert msstore.pair_index_state(plan) == "other"
    after = contents(tmp_path / "SystemAppData")
    with pytest.raises(msstore.IndexChanged):
        msstore.rollback_pair(plan)
    assert contents(tmp_path / "SystemAppData") == after


def xgs_dir(acct: Path, identifier: str) -> Path:
    return acct.parent.parent / "xgs" / acct.name / identifier


def test_xgs_bytes_wgs_does_not_hold_are_never_written_over(tmp_path, make_wgs_account):
    """Review item 3: an xgs-only save, or an xgs copy that differs from its wgs save,
    would be destroyed by the write: the plan refuses."""
    acct = account(tmp_path, make_wgs_account)
    only = xgs_dir(acct, "Slot3Manual")
    only.mkdir()
    (only / "data").write_bytes(b"a save only xgs holds")
    with pytest.raises(msstore.XgsMismatch):
        msstore.prepare_pair(acct, pair(3))
    (xgs_dir(acct, "Slot1Auto") / "data").write_bytes(b"newer than wgs")
    with pytest.raises(msstore.XgsMismatch):
        msstore.prepare_pair(acct, pair(1))
    assert (only / "data").read_bytes() == b"a save only xgs holds"


def test_xgs_states_classify_pre_post_torn_and_other(tmp_path, make_wgs_account):
    """Review item 3: what recovery needs - and a rollback over an xgs copy someone else
    wrote refuses and changes nothing."""
    acct, writes, plan = written(tmp_path, make_wgs_account)
    assert plan.pre_xgs["Slot1Manual"] is not None and all(plan.pre_xgs["Slot1Auto"])
    with pytest.raises(SimulatedCrash):
        msstore.apply_pair(plan, payload(writes), FaultInjector({"xgs:Slot1Manual"}))
    assert msstore.pair_xgs_states(plan) == {"Slot1Manual": "post", "Slot1Auto": "pre"}
    auto = xgs_dir(acct, "Slot1Auto")
    (auto / "data").write_bytes(writes[1].data)                    # data written, meta not
    assert msstore.pair_xgs_states(plan)["Slot1Auto"] == "torn"
    (auto / "meta").write_bytes(b"the game wrote this")
    assert msstore.pair_xgs_states(plan)["Slot1Auto"] == "other"
    after = contents(tmp_path / "SystemAppData")
    with pytest.raises(msstore.RollbackError, match="xgs"):
        msstore.rollback_pair(plan)
    assert contents(tmp_path / "SystemAppData") == after


def test_a_torn_xgs_mirror_rolls_back_exactly(tmp_path, make_wgs_account):
    acct, writes, plan = written(tmp_path, make_wgs_account)
    before = contents(tmp_path / "SystemAppData")
    before_raw = (acct / "containers.index").read_bytes()
    with pytest.raises(SimulatedCrash):
        msstore.apply_pair(plan, payload(writes), FaultInjector({"xgs:Slot1Manual"}))
    (xgs_dir(acct, "Slot1Auto") / "data").write_bytes(writes[1].data)
    msstore.rollback_pair(plan)
    assert contents(tmp_path / "SystemAppData") == before
    assert (acct / "containers.index").read_bytes() == before_raw


def test_planning_reads_no_other_records_blobs(tmp_path, make_wgs_account, monkeypatch):
    """Review item 10: the tree's identifier convention comes from the raw records; only the
    records being written are resolved to their blob files."""
    acct = account(tmp_path, make_wgs_account)
    resolved = []
    real = msstore._resolve_blobs
    monkeypatch.setattr(msstore, "_resolve_blobs", lambda c: (resolved.append(c.identifier),
                                                              real(c))[1])
    msstore.prepare_pair(acct, pair(2))                 # Slot2Auto exists, Slot2Manual is new
    assert resolved == ["Slot2Auto"]
