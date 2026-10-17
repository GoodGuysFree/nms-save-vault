"""``nmsvault store ...``: the version store's commands (D3 2.11). Kept apart from ``cli.py``
so the store's commands grow without touching the classic ones."""
from __future__ import annotations

import argparse
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


def cmd_store_fsck(args) -> int:
    state = appstate.load()
    if args.vault:
        root = Path(args.vault)
    else:
        root = Path(state.vault) if (state and state.vault) else locations.default_vault_dir()
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
