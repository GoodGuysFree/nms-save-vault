"""Review 26's findings in the core (R26-4, R26-5), each a test that failed before its fix: a
write open in another vault is told of also when its journal cannot be read, and not while a
write runs through that vault now. Made-up saves only."""
from __future__ import annotations

import sys
import threading

import pytest

from nms_save_vault.core import api, safety
from nms_save_vault.core.liveops import FaultInjector, SimulatedCrash, opjournal
from nms_save_vault.core.organizer.store import liveop_lock_path

from .test_core_api_write import UNITS, _session, _slot, savecraft, world  # noqa: F401


def _stopped_in_other_vault(world, tmp_path, monkeypatch):
    """As the R7-3 test: an app wrote through another vault and stopped part-way (the op
    open there, this machine's marker naming it)."""
    from nms_save_vault.core import store as vstore
    other = tmp_path / "other-vault"
    other.mkdir()
    vstore.create_store(other)
    session = api.open_live(other, live_dirs=[world.live])
    plan = session.plan_edit(_slot(world), UNITS, what="x")
    make = session._make

    def crashing():
        ctx = make()
        ctx.faults = FaultInjector({"swapped:2"})
        return ctx

    monkeypatch.setattr(session, "_make", crashing)
    with pytest.raises(SimulatedCrash):
        session.write(plan, plan.confirmations)
    return other


@pytest.mark.parametrize("how", ["newer", "damaged", "locked"])
def test_r26_4_a_write_open_in_another_vault_whose_journal_cannot_be_read_is_never_told(
        world, tmp_path, monkeypatch, how):
    """_missing_vaults reads the other vault's journal; when that op cannot be read from it
    (lines a newer app wrote, damaged lines, a file another process holds locked) the record
    is None and the marker is skipped as if the op were settled. The marker is still there
    (that vault drops it only when the op ends), so the half-written slot is not told of."""
    other = _stopped_in_other_vault(world, tmp_path, monkeypatch)
    ops = other / "organizer" / opjournal.OPS
    data = ops.read_bytes()
    monkeypatch.setattr(opjournal, "_tails", {})           # read as a new process reads it
    if how == "newer":
        ops.write_bytes(data.replace(b'{"j":1,', b'{"j":2,'))
    elif how == "damaged":
        ops.write_bytes(b"".join(b"{damaged\n" for _ in data.splitlines()))
    else:
        if sys.platform != "win32":
            pytest.skip("a byte-range lock that stops reads is Windows'")
        import msvcrt
        holder = open(ops, "r+b")                           # noqa: SIM115 - held for the test
        msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, len(data))
    try:
        items = _session(world).interrupted()
    finally:
        if how == "locked":
            holder.seek(0)
            msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, len(data))
            holder.close()
    assert len(items) == 1, "the write left open in the other vault is not told of"
    assert items[0].choices == ()


def test_r26_5_a_write_running_now_through_another_vault_is_told_as_interrupted(
        world, tmp_path, monkeypatch):
    """pending_recovery lists nothing of this vault while its live-op lock is held ("it is
    running, not interrupted"), but _missing_vaults never asks the other vault's lock: an op
    being written through it right now (open in its journal, marker written after
    ``writing``) is told as "interrupted ... before playing"."""
    other = _stopped_in_other_vault(world, tmp_path, monkeypatch)
    held, release = threading.Event(), threading.Event()

    def running():                       # the other app, mid-write, holds that vault's lock
        with safety.exclusive_lock(liveop_lock_path(other), timeout=5):
            held.set()
            release.wait(30)

    worker = threading.Thread(target=running, daemon=True)
    worker.start()
    try:
        assert held.wait(10)
        said = [i.text for i in _session(world).interrupted()]
    finally:
        release.set()
        worker.join(10)
    assert not [t for t in said if "interrupted" in t], said
