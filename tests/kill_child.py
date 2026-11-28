"""The process the kill tests kill (tests/test_kill_safety.py; GGF 2026-11-28: "We cannot
compromise on the user's file safety").

Run as ``python kill_child.py <sandbox> <mode> [n]``. It runs one real operation of the app,
described by ``<sandbox>/spec.json``, on the sandbox's vault and save folder only. Every
step the code names (the live operations' fault points, the store's, and each file replace
and atomic write) is counted:

- ``record``: run to the end and write the steps, in order, to ``<sandbox>/points.json``;
- ``kill_at n``: at the n-th step the process ends itself the way Task Manager's End task
  ends it (TerminateProcess: no ``finally``, no flush, no cleanup);
- ``slow``: a short pause at each step, so a kill from outside at a random moment lands
  anywhere in the operation.

It prints ``READY`` just before the operation starts and ``DONE`` when it ended. It never
looks at the real user folders: the parent runs it with the environment of its test (the
per-user folders pointed into the test's temp folder).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _end_now() -> None:
    """What End task does: the process is gone at once, nothing after this line runs."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), 9)
    os.kill(os.getpid(), 9)


def main() -> int:
    sandbox, mode = Path(sys.argv[1]), sys.argv[2]
    target = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    spec = json.loads((sandbox / "spec.json").read_text(encoding="utf-8"))
    steps: list[str] = []

    def step(name: str) -> None:
        steps.append(name)
        if mode == "kill_at" and len(steps) == target:
            _end_now()
        if mode == "slow":
            time.sleep(0.004)

    from nms_save_vault.core import operations as ops
    from nms_save_vault.core import safety
    from nms_save_vault.core.catalog import Vault
    from nms_save_vault.core.liveops import faults, planner
    from nms_save_vault.core.store import classic as store_classic
    from nms_save_vault.core.store import objects, versionstore

    # every module that bound faults.at by name gets the counting one
    real_at = faults.at

    def at(injector, point: str) -> None:
        step(point)
        real_at(injector, point)
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "at", None) is real_at:
            module.at = at
    versionstore.VersionStore._fault = lambda self, point: step("store:" + point)
    objects.ObjectStore._fault = lambda self, point: step("object:" + point)
    real_replace, real_write = safety.durable_replace, safety.atomic_write_bytes

    def replace(src, dst):
        step("replace:" + Path(dst).name)
        real_replace(src, dst)
        step("replaced:" + Path(dst).name)

    def write(path, data, *a, **k):
        step("write:" + Path(path).name)
        real_write(path, data, *a, **k)
        step("written:" + Path(path).name)
    safety.durable_replace, safety.atomic_write_bytes = replace, write
    for module in list(sys.modules.values()):        # a name bound at import gets it too
        if module is not None and module.__name__.startswith("nms_save_vault"):
            if getattr(module, "atomic_write_bytes", None) is real_write:
                module.atomic_write_bytes = write
    safety.is_game_running = lambda *a, **k: False
    safety._process_names = lambda: ["System", "explorer.exe"]
    planner.XBOX_SYNC_SECONDS = 0

    vault = Vault(sandbox / "vault")
    live = sandbox / spec["live"]
    print("READY", flush=True)
    if spec["op"] == "restore":
        entry = store_classic.find_entry(vault, spec["entry"])
        ops.restore_full(vault, entry, live, allow_game_running=True,
                         restore_accountdata=spec.get("accountdata", False))
    elif spec["op"] == "backup":
        ops.create_full_backup(vault, live, include_cache=False)
    else:
        raise SystemExit(f"unknown op {spec['op']!r}")
    if mode == "record":
        (sandbox / "points.json").write_text(json.dumps(steps), encoding="utf-8")
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
