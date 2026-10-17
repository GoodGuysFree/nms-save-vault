"""``nmsvault store ...``: the version store's commands (D3 2.11, 4.1). Kept apart from
``cli.py`` so the store's commands grow without touching the classic ones. Nothing here runs
unless asked: the upgrade only by ``nmsvault store upgrade``, after the owner confirms."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import locations
from .core import state as appstate
from .core import store as vstore


def add_commands(sub: argparse._SubParsersAction) -> None:
    s = sub.add_parser("store", help="the vault's version store")
    store_sub = s.add_subparsers(dest="store_command", required=True)
    f = store_sub.add_parser("fsck", help="check the version store (a quick check by default)")
    f.add_argument("--deep", action="store_true",
                   help="also re-read every stored file and check every save in it")
    f.add_argument("--repair", action="store_true",
                   help="store damaged files again from good copies found elsewhere")
    f.add_argument("--from", dest="fallbacks", action="append", default=[], metavar="FOLDER",
                   help="another folder to look in for good copies (repeatable)")
    f.set_defaults(func=cmd_store_fsck)

    u = store_sub.add_parser("upgrade", help="upgrade the vault to the version store (a copy of "
                                             "the vault is made next to it first)")
    u.add_argument("--copy-to", metavar="FOLDER",
                   help="make the copy of the vault in this folder instead of next to it")
    u.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    u.set_defaults(func=cmd_store_upgrade)


def _vault_root(args, state) -> Path:
    if args.vault:
        return Path(args.vault)
    return Path(state.vault) if (state and state.vault) else locations.default_vault_dir()


def _live_paths(state) -> list[Path]:
    from .core import discover

    return [Path(p) for p in discover.all_live_paths(state)]


def cmd_store_upgrade(args) -> int:
    from .core.store import upgrade

    state = appstate.load()
    root = _vault_root(args, state)
    cleaned = upgrade.cleanup_interrupted_upgrade(root)
    if cleaned is not None:
        print(cleaned.message)
    plan = upgrade.plan_upgrade(root, dest_parent=Path(args.copy_to) if args.copy_to else None,
                                live_roots=_live_paths(state))
    for _code, text in plan.problems:
        print(f"error: {text}", file=sys.stderr)
    if plan.problems:
        return 1
    if plan.copy:
        print(f"The vault ({plan.files} files, {plan.bytes / (1 << 30):.1f} GB) is copied to "
              f"{plan.dest.name} first; nothing in it changes before that copy is checked.")
    print("After the upgrade, older versions of NMS Save Vault must not be used on this vault.")
    if not args.yes:
        answer = input("Upgrade the vault now? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing was changed.")
            return 0
    result = upgrade.run_upgrade(plan, progress=_progress)
    print(result.message)
    return 0


def _progress(p) -> None:
    if p.stage == "copy" and p.files_total and (p.files_done == p.files_total
                                                or p.files_done % 500 == 0):
        print(f"  copied {p.files_done} of {p.files_total} files")


def cmd_store_fsck(args) -> int:
    state = appstate.load()
    root = _vault_root(args, state)
    store = vstore.open_store(root, app_state=state)
    for warning in store.warnings:
        print(f"warning: {warning}")
    report = store.fsck(deep=args.deep, repair=args.repair,
                        fallbacks=[Path(f) for f in args.fallbacks])
    print(f"Checked the version store ({'deep' if report.deep else 'quick'}): "
          f"{report.entries} entries, {report.objects} stored files, "
          f"{report.unreferenced} not used by any entry, {report.tmp_leftovers} temporary files.")
    for p in report.problems:
        where = " ".join(x for x in (p.entry_id, p.path) if x)
        print(f"  {p.kind:<9} {where}: {p.detail}" + (" (repaired)" if p.repaired else ""))
    if report.ok:
        print("No damage found." if not report.problems
              else f"All {report.repaired} problems repaired.")
        return 0
    left = len(report.problems) - report.repaired
    print(f"{left} problem(s) remain; they are listed in the vault's damage list."
          + ("" if args.repair else " Try: nmsvault store fsck --repair"))
    return 1
