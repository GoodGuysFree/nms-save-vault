"""Real kills of real operations, and what the next start makes of them (GGF 2026-11-28: "It
needs to run every time before we release. We cannot compromise on the user's file safety").

Each case builds a sandbox (a vault and a Steam save folder in the test's temp folder), runs
one operation of the app in a child process (tests/kill_child.py) and ends that process the
way Task Manager's End task does: at every step the operation names, one run per step (the
steps are found by a first run to the end), and at random moments. After each kill the next
start is played in this process, as the app plays it: the interrupted operation is found,
what can be settled is settled and said, what cannot is offered, and the owner's choice is
applied. Then:

- the save folder is wholly as it was before the operation, or wholly as the operation
  leaves it (never a mix), and exactly as it was after "put it back", exactly as the
  operation leaves it after "finish it";
- nothing is left open in the journal, and nothing is settled in silence;
- no temporary file is left in the save folder;
- the vault's store checks clean (every listed backup whole).

Run with the rest of the suite, and on its own before every release (RELEASING.md step 2):
``-m killtest``. A kill is not a power cut (Windows still writes what it holds in memory):
the write-through of every replace (tests/test_safety_durable.py) is what covers the rest.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nms_save_vault.core import formats, msstore
from nms_save_vault.core import operations as ops
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.liveops import account, classic, recovery
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops.recovery import pending_recovery, resolve_recovery, take_settled
from nms_save_vault.core.store import classic as store_classic

from .conftest import chunked, steam_save, wgs_account_factory, xbox_meta

pytestmark = pytest.mark.killtest

CHILD = Path(__file__).with_name("kill_child.py")
ACCOUNT = "st_76561198000000001"
XBOX = "0000000000000001_29070100B936489ABCE8B9AF3980429C"
DATA, META = formats.ACCOUNT_DATA_NAME, formats.ACCOUNT_META_NAME
RANDOM_KILLS = 12          # per case, besides one at every step
CHILD_TIMEOUT = 120


# --- the sandboxes -----------------------------------------------------------------------------


def _steam_folder(live: Path, tag: str, slots=(1, 2, 3), account: bool = True) -> None:
    for slot in slots:
        for member in (0, 1):
            steam_save(live, slot, member, f"{tag} run {slot}{'AB'[member]}", 3600 * slot + member,
                       0x1000 + slot, 1_700_000_000 + 100 * slot + member, extra=f"{tag}-{slot}")
    if account:
        (live / DATA).write_bytes(f"{tag} account data".encode())
        (live / META).write_bytes(f"{tag} account meta".encode())


_XBOX_SAVES = [("Slot1Auto", '{"a":1}', "One", "", 100, 1000),
               ("Slot1Manual", '{"a":2}', "One b", "", 101, 1001),
               ("Slot2Auto", '{"b":1}', "Two", "", 200, 2000)]


def _xbox_sandbox(root: Path, case: str) -> Path:
    """An Xbox account with the game's second copy (xgs), a vault holding a backup of it, and
    the account changed since. ``xbox-restore-no-account``: the live account has no account
    data, the backup has (the restore gives it the backup's)."""
    make = wgs_account_factory()
    acct_rec = [("AccountData", '{"acct":1}', "", "", 0, 500)]
    vault = Vault(root / "vault")
    vault.ensure()
    vstore.create_store(vault.root)
    if case == "xbox-restore-no-account":
        bk = make(root / "bk", acct_rec + _XBOX_SAVES, name=XBOX)
        live = make(root / "Packages", _XBOX_SAVES, name=XBOX)
        backup = ops.import_backup(vault, bk, label="bk")
    else:
        live = make(root / "Packages", acct_rec + _XBOX_SAVES, name=XBOX)
        twin = live.parent.parent / "xgs" / live.name
        _mirror(live, twin)
        backup = ops.create_full_backup(vault, live, include_cache=False)
    twin = live.parent.parent / "xgs" / live.name
    _mirror(live, twin)
    for n, ident in enumerate(("Slot1Auto", "Slot2Auto", "Slot3Auto")):   # changed since
        msstore.write_save(live, ident, chunked(f'{{"new":{n}}}'.encode()),
                           xbox_meta(f"New {ident}", ts=5000 + n), 5000.0 + n,
                           create_if_missing=True)
    spec = {"live": f"Packages/wgs/{XBOX}", "tree": "Packages", "xbox": True,
            "entry": backup.id, "op": "restore"}
    (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return root


def _mirror(live: Path, twin: Path) -> None:
    for c in msstore.parse_containers_index(live)[1]:
        (twin / c.identifier).mkdir(parents=True, exist_ok=True)
        (twin / c.identifier / "data").write_bytes(c.data_file.read_bytes())
        (twin / c.identifier / "meta").write_bytes(c.meta_file.read_bytes())


def _sandbox(root: Path, case: str) -> Path:
    if case.startswith("xbox"):
        return _xbox_sandbox(root, case)
    return _steam_sandbox(root, case)


def _steam_sandbox(root: Path, case: str) -> Path:
    """A vault (store mode, but for ``legacy-backup``) holding a full backup, and a save folder
    changed since, so a restore changes every slot (and the account data)."""
    live = root / "NMS" / ACCOUNT
    _steam_folder(live, "old")
    vault = Vault(root / "vault")
    vault.ensure()
    if case != "legacy-backup":
        vstore.create_store(vault.root)
    backup = ops.create_full_backup(vault, live, include_cache=False)
    _steam_folder(live, "new", slots=(1, 2, 3, 4))           # the folder changed since
    if case == "restore-into-no-account":
        (live / DATA).unlink()
        (live / META).unlink()
    spec = {"live": f"NMS/{ACCOUNT}", "tree": f"NMS/{ACCOUNT}", "entry": backup.id}
    spec.update({
        "restore": {"op": "restore"},
        "restore-account": {"op": "restore", "accountdata": True},
        "restore-into-no-account": {"op": "restore"},
        "backup": {"op": "backup"},
        "legacy-backup": {"op": "backup"},
    }[case])
    (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return root


def _files(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _xbox_state(packages: Path) -> dict[str, str]:
    """What an Xbox account holds, by meaning (its blob and container names change on every
    write): each record's data and meta, the game's second copy of each, and any blob folder
    the index does not list. A file that is not read as any of these shows as itself."""
    wgs = next((packages / "wgs").iterdir())
    out: dict[str, str] = {}
    try:
        _info, containers = msstore.parse_containers_index(wgs)
    except (OSError, ValueError) as exc:
        return {"index": f"unreadable: {exc}"}
    held = set()
    for c in containers:
        held.add(c.dir_guid)
        for part, f in (("data", c.data_file), ("meta", c.meta_file)):
            out[f"wgs/{c.identifier}/{part}"] = (hashlib.sha256(f.read_bytes()).hexdigest()
                                                if f is not None and f.is_file() else "missing")
    for p in wgs.iterdir():
        if p.is_dir() and p.name not in held:
            out[f"wgs/unlisted/{p.name}"] = "unlisted"
        elif p.is_file() and p.name != "containers.index":
            out[f"wgs/{p.name}"] = hashlib.sha256(p.read_bytes()).hexdigest()
    twin = packages / "xgs" / wgs.name
    for p in sorted(twin.rglob("*")) if twin.is_dir() else ():
        if p.is_file():
            out["xgs/" + p.relative_to(twin).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _state(sandbox: Path) -> dict[str, str]:
    spec = json.loads((sandbox / "spec.json").read_text(encoding="utf-8"))
    tree = sandbox / spec["tree"]
    return _xbox_state(tree) if spec.get("xbox") else _files(tree)


def _run_child(sandbox: Path, mode: str, n: int = 0) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHILD), str(sandbox), mode, str(n)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=CHILD_TIMEOUT, env=dict(os.environ))


def _reference(tmp_path: Path, case: str) -> tuple[Path, dict, dict, list[str]]:
    """The sandbox, the folder before and after the operation run to its end, and its steps."""
    base = _sandbox(tmp_path / "base", case)
    before = _state(base)
    ref = tmp_path / "ref"
    shutil.copytree(base, ref)
    done = _run_child(ref, "record")
    assert done.returncode == 0 and "DONE" in done.stdout, done.stderr[-3000:]
    after = _state(ref)
    steps = json.loads((ref / "points.json").read_text(encoding="utf-8"))
    assert steps, "the operation named no step"
    return base, before, after, steps


# --- the next start, and the checks ---------------------------------------------------------


def _next_start(sandbox: Path, case: str, choice: str) -> dict:
    """What the app does when it starts again, then the owner's ``choice`` ("back" or
    "forward") for whatever it offers. Returns what happened, for the checks."""
    vault = Vault(sandbox / "vault")
    live = sandbox / json.loads((sandbox / "spec.json").read_text(encoding="utf-8"))["live"]
    out = {"said": [], "offered": [], "chosen": [], "removed": ""}
    out["removed"] = ops.incomplete_notice(vault.root)
    if case == "legacy-backup":
        return out
    ctx, _ref = classic.open_context(vault, live)
    # A restore's account data, journaled ahead of it, is one operation with it (offered or
    # said with it); counted on its own only when its restore never started.
    ops_now = jr.read_ops(ctx)
    open_before = [r for r in ops_now.values() if r.open and not (
        account.is_follower(r) and recovery._lead_of(ops_now, r) is not None)]
    items = pending_recovery(ctx)
    out["said"] = take_settled(ctx)
    out["offered"] = [(i.kind, i.state, tuple(i.choices)) for i in items]
    assert len(items) + len(out["said"]) >= len(open_before), (
        f"settled in silence: open {[r.kind for r in open_before]}, offered "
        f"{out['offered']}, said {out['said']}")
    for item in items:
        assert {"back", "forward"} & set(item.choices), f"only {item.choices} offered: {item}"
        way = choice if choice in item.choices else next(
            c for c in ("back", "forward") if c in item.choices)
        res = resolve_recovery(ctx, item.op_id, way)
        assert res.status in ("done", "rolled_back"), (way, res)
        out["chosen"].append(way)
    out["said"] += take_settled(ctx)            # what settled with the owner's choice
    ctx, _ref = classic.open_context(vault, live)
    assert [r for r in jr.read_ops(ctx).values() if r.open] == [], "left open"
    assert pending_recovery(ctx) == [] and take_settled(ctx) == []
    if ctx.versions is not None:
        report = ctx.versions.fsck()
        assert report.ok, report.problems
    return out


def _check(sandbox: Path, case: str, before: dict, after: dict, happened: dict, where: str):
    now = _state(sandbox)
    temp = [n for n in now if ".tmp_" in n or n.endswith(".part")]
    assert not temp, f"{where}: temporary files left: {temp}"
    if case in ("backup", "legacy-backup"):
        assert now == before, f"{where}: a backup changed the save folder"
        vault = Vault(sandbox / "vault")
        if case == "legacy-backup":
            listed = {e.id for e in vault.entries}
            for d in (vault.backups_dir, vault.snapshots_dir):
                for folder in d.iterdir():
                    if folder.is_file() and folder.name.startswith(".tmp_"):
                        continue        # a cut-off write's temp file: swept once it is old
                    assert folder.name in listed, f"{where}: an unlisted folder {folder.name}"
            return
        return
    def differ(other: dict) -> list[str]:
        return sorted(k for k in set(now) | set(other) if now.get(k) != other.get(k))
    if now not in (before, after):
        raise AssertionError(f"{where}: neither as it was (differs in {differ(before)}) nor as "
                             f"the restore leaves it (differs in {differ(after)}); "
                             f"{happened['offered']} {happened['said']}")
    if "back" in happened["chosen"] and now != before:
        raise AssertionError(f"{where}: put back, but not as it was: {differ(before)}")
    if "forward" in happened["chosen"] and now != after:
        raise AssertionError(f"{where}: finished, but not as the restore leaves it: "
                             f"{differ(after)}")


CASES = ["restore", "restore-account", "restore-into-no-account", "backup", "legacy-backup",
         "xbox-restore", "xbox-restore-no-account"]


@pytest.mark.parametrize("case", CASES)
def test_a_kill_at_every_step_is_recovered(tmp_path, case):
    base, before, after, steps = _reference(tmp_path, case)
    failures = []
    for n in range(1, len(steps) + 1):
        for choice in ("back", "forward"):
            run = tmp_path / f"k{n}{choice[0]}"
            shutil.copytree(base, run)
            killed = _run_child(run, "kill_at", n)
            assert "DONE" not in killed.stdout, f"step {n} ({steps[n - 1]}) was not reached"
            try:
                happened = _next_start(run, case, choice)
                _check(run, case, before, after, happened, f"killed at {steps[n - 1]!r} (#{n})")
            except AssertionError as exc:
                failures.append(f"#{n} {steps[n - 1]} [{choice}]: {str(exc)[:400]}")
            shutil.rmtree(run, ignore_errors=True)
    assert not failures, f"{len(failures)} of {2 * len(steps)} kills:\n" + "\n".join(failures[:15])


@pytest.mark.parametrize("case", CASES)
def test_a_kill_at_a_random_moment_is_recovered(tmp_path, case):
    base, before, after, _steps = _reference(tmp_path, case)
    timed = tmp_path / "timed"
    shutil.copytree(base, timed)
    start = time.monotonic()
    assert _run_child(timed, "slow").returncode == 0
    span = time.monotonic() - start
    rng = random.Random(f"{case}-{os.getpid()}-{time.time_ns()}")
    failures = []
    for k in range(RANDOM_KILLS):
        run = tmp_path / f"r{k}"
        shutil.copytree(base, run)
        child = subprocess.Popen([sys.executable, str(CHILD), str(run), "slow"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 encoding="utf-8", env=dict(os.environ))
        assert child.stdout.readline().strip() == "READY"
        delay = rng.uniform(0, span)
        time.sleep(delay)
        child.kill()                          # TerminateProcess on Windows: End task
        child.communicate(timeout=CHILD_TIMEOUT)
        choice = rng.choice(("back", "forward"))
        try:
            happened = _next_start(run, case, choice)
            _check(run, case, before, after, happened, f"killed after {delay:.3f} s")
        except AssertionError as exc:
            failures.append(f"{delay:.3f} s [{choice}]: {str(exc)[:400]}")
        shutil.rmtree(run, ignore_errors=True)
    assert not failures, "\n".join(failures[:10])
