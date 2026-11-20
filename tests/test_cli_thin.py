"""``nmsvault-cli store thin [--dry-run] [--yes]`` (docs/thinning.md "The apps"): the plan is
printed first (what goes, why, the space; what stays that the schedule alone would drop), then
y/N unless --yes; a blocked plan exits non-zero and deletes nothing. Made-up vaults only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nms_save_vault import cli
from nms_save_vault.core import discover
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.store import thinning

from .test_store_thin import P1, P2, World, at


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(appstate, "load", lambda *a, **k: None)
    monkeypatch.setattr(discover, "all_live_paths", lambda *a, **k: [])
    return thin_world(tmp_path)


def thin_world(tmp_path) -> World:
    """Two full backups of one month long ago (the older may go), each playthrough's newest, and
    two backups of one old save of which the older is pinned. Dated against the real clock: the
    apps plan for now."""
    w = World(tmp_path)
    recent = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
    full = {"kind": "full", "label": ""}
    w.add((1, P1, recent, "now"), **full)
    w.old = w.add((1, P1, at(2024, 3, 3, 12), "old"), **full)
    w.add((1, P1, at(2024, 3, 20, 12), "kept"), **full)
    w.add((2, P2, recent, "two"), **full)
    twin = (2, P2, at(2024, 5, 5, 12), "twin")
    w.pinned = w.add(twin, made=at(2024, 5, 5, 13), **full)
    w.add(twin, made=at(2024, 5, 5, 14), **full)
    thinning.pin_entry(w.root, w.pinned.id)
    w.planned = thinning.plan_thin(w.root)
    return w


def run(capsys, w, *args):
    code = cli.main(["--vault", str(w.root), "store", "thin", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def entries(w) -> set[str]:
    return {e.id for e in vstore.open_store(w.root).entries()}


def test_the_world_has_backups_to_thin_and_one_pinned(world):
    plan = world.planned
    assert world.old.id in {c.id for c in plan.candidates} and not plan.blocked
    assert {k.id: k.reasons for k in plan.kept} == {world.pinned.id: (thinning.PINNED,)}


def test_a_dry_run_prints_the_plan_and_deletes_nothing(world, capsys):
    plan, before = world.planned, entries(world)
    code, out, err = run(capsys, world, "--dry-run")
    assert code == 0 and err == ""
    n = len(plan.candidates)
    assert f"{n} backups can go, freeing" in out
    for c in plan.candidates:
        line = next(x for x in out.splitlines() if c.created.replace("T", " ")[:16] in x)
        assert c.reason in line and "full backup" in line
        assert f"{c.bytes / 1024:.0f} KB" in line                  # made-up saves are small
        assert ", ".join(f"slot {s.slot} {s.name}" for s in c.saves) in line
    assert "Kept although the schedule would let them go (1):" in out
    when = world.pinned.created_local.replace("T", " ")[:16]
    kept = next(x for x in out.splitlines() if when in x)
    assert "you pinned it" in kept and "slot 2 Main" in kept
    assert "Nothing was deleted" in out and out.isascii()
    assert entries(world) == before


def test_without_yes_it_asks_and_no_keeps_everything(world, capsys, monkeypatch):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "n")
    before = entries(world)
    code, out, _err = run(capsys, world)
    n = len(world.planned.candidates)
    assert code == 1 and f"{n} backups can go" in out    # the list comes before the question
    assert asked and asked[0].startswith(f"Delete {n} entries (frees ")
    assert asked[0].endswith("[y/N] ")
    assert "Nothing was deleted." in out and entries(world) == before


def test_yes_at_the_question_deletes_what_was_listed(world, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    code, out, _err = run(capsys, world)
    gone = {c.id for c in world.planned.candidates}
    assert code == 0 and f"{len(gone)} backups deleted" in out
    assert not gone & entries(world) and world.pinned.id in entries(world)
    assert vstore.open_store(world.root).fsck().ok


def test_yes_skips_the_question(world, capsys, monkeypatch):
    def never(prompt):
        raise AssertionError("asked although --yes was given")
    monkeypatch.setattr("builtins.input", never)
    code, out, _err = run(capsys, world, "--yes")
    assert code == 0 and "backups deleted" in out and world.old.id not in entries(world)


def test_with_nothing_to_go_it_says_so(world, capsys):
    assert run(capsys, world, "--yes")[0] == 0
    code, out, _err = run(capsys, world, "--yes")
    assert code == 0 and "Nothing to thin" in out


def test_a_blocked_plan_exits_non_zero_and_deletes_nothing(world, capsys, monkeypatch):
    store = vstore.open_store(world.root)
    digest = next(f.sha256 for e in store.entries() for f in e.files if f.inline is None)
    path = store.objects.path_for(digest)
    path.write_bytes(path.read_bytes()[:-1])
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    before = entries(world)
    code, out, err = run(capsys, world, "--yes")
    assert code == 1 and "problem(s)" in err and "nothing is thinned" in err
    assert "Delete" not in out and entries(world) == before


def test_an_interrupted_run_is_finished_first(world, capsys):
    def crash(point):
        if point == "thin.after_manifest":
            raise KeyboardInterrupt("stopped")
    with pytest.raises(KeyboardInterrupt):
        thinning.run_thin(world.root, world.planned, [world.old.id], faults=crash)
    code, out, _err = run(capsys, world, "--dry-run")
    assert code == 0 and "stopped before its end" in out and "without --dry-run" in out
    assert thinning.thinning_pending(world.root)
    code, out, _err = run(capsys, world, "--yes")
    assert code == 0 and "stopped before its end" in out and "1 backup deleted" in out
    assert not thinning.thinning_pending(world.root) and vstore.open_store(world.root).fsck().ok


def test_a_vault_of_the_old_layout_is_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(appstate, "load", lambda *a, **k: None)
    monkeypatch.setattr(discover, "all_live_paths", lambda *a, **k: [])
    old = tmp_path / "old vault"
    old.mkdir()
    code = cli.main(["--vault", str(old), "store", "thin", "--dry-run"])
    assert code == 1 and "Upgrade the vault first" in capsys.readouterr().err
    assert not (old / "store").exists()
