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

    f = store_sub.add_parser("fold", help="read the old backup folders into the store (after the "
                                          "upgrade; resumes a fold that stopped)")
    f.set_defaults(func=cmd_store_fold)

    c = store_sub.add_parser("copy-in", help="copy backup folders you made by hand into the "
                                             "vault (the folders are not changed)")
    c.add_argument("entry_ids", nargs="*", metavar="ID", help="the in-place entries to copy in")
    c.add_argument("--all", action="store_true", help="every one not copied in yet")
    c.set_defaults(func=cmd_store_copy_in)

    r = store_sub.add_parser("rollback", help="undo the vault upgrade: back to the old layout "
                                              "with everything made since")
    r.add_argument("--copy", metavar="FOLDER", help="where the pre-upgrade copy is, when it is "
                                                     "not next to the vault any more")
    r.add_argument("--keep-upgrade", action="store_true",
                   help="after an interrupted undo: stay upgraded instead of finishing it")
    r.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    r.set_defaults(func=cmd_store_rollback)


def _vault_root(args, state) -> Path:
    from .core.store import upgrade

    root = Path(args.vault) if args.vault else (
        Path(state.vault) if (state and state.vault) else locations.default_vault_dir())
    notice = upgrade.startup_notice(root)       # an interrupted upgrade: its own leftovers go
    if notice:
        print(f"note: {notice}", file=sys.stderr)
    return root


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
    if result.fold_waiting_for_space:
        return 0
    return _fold(root, state)


def cmd_store_fold(args) -> int:
    state = appstate.load()
    return _fold(_vault_root(args, state), state)


def cmd_store_copy_in(args) -> int:
    from .core import aliases

    state = appstate.load()
    store = vstore.open_store(_vault_root(args, state), app_state=state)
    waiting = vstore.not_copied_in(store)
    ids = [e.id for e in waiting] if args.all else list(args.entry_ids)
    if not ids:
        if not waiting:
            print("Every backup folder you made by hand is in the vault already.")
            return 0
        print("Backup folders you made by hand, outside the vault (copy them in with "
              "'nmsvault store copy-in ID ...' or '--all'):")
        for e in waiting:
            print(f"  {e.id:<34}{aliases.redact(e.label)}")
        return 0
    report = vstore.copy_in(store, ids)
    for entry_id, why in report.skipped:
        print(f"  skipped {entry_id}: {why}")
    print(report.message)
    return 1 if report.failed else 0


def cmd_store_rollback(args) -> int:
    from .core.store import rollback

    state = appstate.load()
    root = _vault_root(args, state)
    interrupted = rollback.rollback_interrupted(root)
    if args.keep_upgrade:
        if not interrupted:
            print("No undo of the upgrade was interrupted; nothing to do.")
            return 0
        rollback.keep_upgrade(root)
        print("The vault stays upgraded.")
        return 0
    store = vstore.VersionStore.open(root, lock=vstore.vault_lock(root),
                                     app_version=_version(), app_state=state,
                                     for_rollback=interrupted)
    plan = rollback.plan_rollback(root, store, copy=Path(args.copy) if args.copy else None)
    for _code, text in plan.problems:
        print(f"error: {text}", file=sys.stderr)
    if plan.problems:
        return 1
    print(f"{len(plan.export)} backups made since the upgrade are written out as folders and "
          f"{len(plan.restore)} old folders are put back from the copy; the store is then moved "
          "next to the vault.")
    if not args.yes:
        answer = input("Undo the vault upgrade now? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing was changed.")
            return 0
    result = rollback.run_rollback(plan, store)
    print(result.message)
    return 0


def _version() -> str:
    from . import __version__

    return __version__


def _fold(root: Path, state) -> int:
    from .core.store import migrate

    store = vstore.open_store(root, app_state=state)
    report = migrate.run_fold(store, progress=lambda p: print(
        f"  reading {p.current} ({p.entries_done + 1} of {p.entries_total})"))
    print(report.message)
    if report.fsck_ok is False:
        print("warning: the check after the fold found damage: run nmsvault store fsck")
    return 0 if not report.failed and report.fsck_ok is not False else 1


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
