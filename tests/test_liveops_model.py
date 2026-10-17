"""liveops model, errors, faults and context (D5 2.1, 2.2): the API's value types are frozen,
JSON-safe through ``to_dict`` and never carry a path."""
from __future__ import annotations

import dataclasses
import json
import time

import pytest

from nms_save_vault.core import operations, safety
from nms_save_vault.core.liveops import (
    ConfirmationRequired,
    Estimate,
    FaultInjector,
    MemberWrite,
    OpContext,
    OpOutcome,
    PairSource,
    PlacePlan,
    Policies,
    ResolvedTarget,
    SimulatedCrash,
    SourceSummary,
    TargetError,
    TargetPlan,
    UndoOffer,
    Warning,
)
from nms_save_vault.core.liveops import faults as faults_mod
from nms_save_vault.core.organizer.locator import Location
from nms_save_vault.core.organizer.model import EntryRef, LiveTarget

ACCOUNT = "st_76561198000000001"
T7 = LiveTarget("steam", ACCOUNT, 7)


def V(n: int) -> str:
    return f"{n:064x}"


def _write(position: str, vid: str) -> MemberWrite:
    return MemberWrite(position=position, vid=vid, pid="DDDAD1BDD39F9C72", play_time=3600,
                       meta_ts=1_792_500_903, source_meta_ts=1_791_500_000,
                       meta_plain_sha256=V(9), meta_disk_sha256=V(10), disk_time=1_792_500_903.0,
                       ordinal=14, copy_of=None,
                       origin={"kind": "entry", "entry": "full-steam-20261005-174129",
                               "slot": 9, "member": position})


def _plan() -> PlacePlan:
    played = Warning("PLAY_TIME_AHEAD", "confirm", "Steam slot 7 has 3h12m more play.",
                     {"seconds": 11520, "slot": 7})
    return PlacePlan(
        plan_id="p1", kind="restore_older",
        targets=(TargetPlan(T7, "Steam slot 7", "steam-cloud", "occupied",
                            (_write("A", V(1)), _write("B", V(2))), ()),),
        source=SourceSummary("Main Save", "DDDAD1BDD39F9C72", V(1), V(2), "A", 3600,
                             1_791_500_000, 1, "steam", "vault: full backup 2026-10-05"),
        undo_of=None, warnings=(played,), confirmations=frozenset({"TWIN_LIVE", "PLAY_TIME_AHEAD"}),
        blocked=(), outcome="Go back to this version in Steam slot 7", fingerprint="f" * 64,
        estimate=Estimate(0, 0, 4_000_000, 0.8), created_at=1_792_500_900.5,
    )


def test_plan_to_dict_is_plain_json_and_survives_a_round_trip():
    plan = _plan()
    d = plan.to_dict()
    assert json.loads(json.dumps(d, ensure_ascii=True, allow_nan=False)) == d
    target = d["targets"][0]
    assert target["target"] == {"platform": "steam", "account": ACCOUNT, "instance": "", "slot": 7}
    assert [w["position"] for w in target["writes"]] == ["A", "B"]
    assert d["confirmations"] == ["PLAY_TIME_AHEAD", "TWIN_LIVE"]       # sorted, a list
    assert d["warnings"][0]["data"] == {"seconds": 11520, "slot": 7}
    assert d["source"]["where_label"] == "vault: full backup 2026-10-05"
    outcome = OpOutcome("op-20261016T101502Z-3fa2", "stale", "", (), UndoOffer("op-x", "Undo"),
                        "n/a", None, replan=plan)
    assert outcome.to_dict()["replan"] == d


def test_to_dict_never_carries_a_path_and_renders_locations(tmp_path):
    folder = tmp_path / "deep" / "backup-folder"
    loc = Location(V(3), "entry", EntryRef("full-steam-20261005-174129"), 9, "A", "steam", True)
    d = PairSource(V(3), None, "A", prefer=loc, folder_slot=(folder, 9)).to_dict()
    assert d["folder_slot"] == ["backup-folder", 9]
    assert str(tmp_path) not in json.dumps(d)
    assert d["prefer"] == {"vid": V(3), "kind": "entry", "entry": "full-steam-20261005-174129",
                           "slot": 9, "member": "A"}


def test_value_types_are_frozen_and_the_resolved_target_stays_internal(tmp_path):
    plan = _plan()
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.kind = "place"  # type: ignore[misc]
    rt = ResolvedTarget(T7, "steam-1", tmp_path, "steam-local", None, "Steam slot 7")
    assert not hasattr(rt, "to_dict")
    with pytest.raises(TypeError):
        Warning("X", "info", "t", {"raw": b"bytes"}).to_dict()


def test_a_simulated_crash_cannot_be_caught_by_the_code_under_test():
    calls = []
    faults = FaultInjector(crash_at={"swapped:2"}, actions={"swapped:2": lambda: calls.append(1)})
    faults.at("swapped:1")

    def guarded():
        try:
            faults.at("swapped:2")
        except Exception:  # noqa: BLE001 - what a careless handler would do
            pytest.fail("a simulated crash must not be an Exception")

    with pytest.raises(SimulatedCrash):
        guarded()
    assert calls == [1] and faults.seen == ["swapped:1", "swapped:2"]
    faults_mod.at(None, "swapped:2")                    # production: no injector, no effect


def test_errors_are_operation_errors_with_stable_codes():
    exc = ConfirmationRequired(["TWIN_LIVE", "OTHER_ACCOUNT"])
    assert isinstance(exc, operations.OperationError)
    assert exc.codes == frozenset({"TWIN_LIVE", "OTHER_ACCOUNT"})
    assert TargetError("UNKNOWN_ACCOUNT", "no source").text == "That live slot is not available."
    assert TargetError("XGS_READ_ONLY", "no index").text == "This Xbox folder can only be read."


def test_context_defaults_ask_the_real_world_and_policies_keep_transfers_shut():
    ctx = OpContext(vault=None, versions=None, store=None, locator=None, lineage=None,
                    readmodel=None, sources=[], policies=Policies(), host=None,
                    app_version="nms-save-vault test", machine_id="m-0123456789ab")
    assert ctx.game_probe is safety.is_game_running and ctx.clock is time.time
    assert ctx.faults is None
    assert Policies().cross_platform == "off"
    with pytest.raises(dataclasses.FrozenInstanceError):
        Policies().cross_platform = "validated"  # type: ignore[misc]
