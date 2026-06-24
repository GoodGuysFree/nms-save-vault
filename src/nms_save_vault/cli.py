"""Command-line front end for NMS Save Vault.

Examples:
    nmsvault status
    nmsvault list
    nmsvault show <entry-id>          # or: show live
    nmsvault backup --label "before X"
    nmsvault restore <entry-id>
    nmsvault extract 9 --label "main"
    nmsvault repopulate --from <entry-id|path> --src-slot 9 --to-slot 3
    nmsvault promote --slot 9 --member A
    nmsvault import "D:\\some\\st_backup" [--copy]
    nmsvault discover [--add]
    nmsvault undo
    nmsvault verify [live|<entry-id>|<path>]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .core import locations, operations as ops
from .core import savedir, slotmap
from .core.catalog import Vault


# --- formatting helpers ------------------------------------------------------


def _fmt_playtime(seconds: int) -> str:
    if not seconds:
        return "-"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h{m:02d}"


def _fmt_ts(unix: int) -> str:
    if not unix:
        return "-"
    try:
        return datetime.fromtimestamp(unix).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(unix)


def _print_view(view: savedir.SaveDirView) -> None:
    print(f"Save folder: {view.path}   (account data: {'yes' if view.account_present else 'no'})")
    print(f"{'Slot':<5}{'Name':<28}{'Mode':<5}{'Play':<8}{'Newest':<7}{'Saved':<18}Notes")
    print("-" * 88)
    for slot in sorted(view.slots):
        sv = view.slots[slot]
        if not sv.occupied:
            print(f"{slot:<5}{'<empty>':<28}")
            continue
        n = sv.newest
        notes = []
        for m in sv.members:
            if m.exists and m.note:
                notes.append(f"{m.label}:{m.note}")
        name = sv.display_name
        mode = n.info.game_mode if (n and n.info) else ""
        print(
            f"{slot:<5}{name[:27]:<28}{str(mode):<5}{_fmt_playtime(n.timestamp and n.info.total_play_time):<8}"
            f"{(n.label if n else '?'):<7}{_fmt_ts(n.timestamp if n else 0):<18}{'; '.join(notes)}"
        )
        # show both members
        for m in sv.members:
            if m.exists:
                tag = "*" if (n and m.label == n.label) else " "
                print(
                    f"    {tag}{m.label}  {m.save_name[:24]:<25} {('valid' if m.valid else 'INVALID'):<8} "
                    f"{_fmt_ts(m.timestamp)}  {m.info.save_summary if m.info else ''}"
                )


# --- resolution helpers ------------------------------------------------------


def _resolve_live(args) -> Path:
    if args.live:
        return Path(args.live)
    d = locations.default_live_save_dir()
    if d is None:
        sys.exit("error: could not locate a live NMS save folder; pass --live <dir>")
    return d


def _resolve_vault(args) -> Vault:
    root = Path(args.vault) if args.vault else locations.default_vault_dir()
    return Vault(root).load()


def _resolve_source(vault: Vault, value: str) -> Path:
    entry = vault.get(value)
    if entry is not None:
        return Path(entry.path)
    p = Path(value)
    if p.is_dir():
        return p
    sys.exit(f"error: source '{value}' is neither a catalog entry id nor a folder")


# --- commands ----------------------------------------------------------------


def cmd_status(args) -> int:
    live = _resolve_live(args)
    vault = _resolve_vault(args)
    running = ops.safety.is_game_running()
    state = {True: "RUNNING (writes blocked)", False: "not running", None: "unknown"}[running]
    print(f"Game: {state}")
    print(f"Vault: {vault.root}   ({len(vault.entries)} catalog entries)")
    _print_view(savedir.scan(live))
    return 0


def cmd_list(args) -> int:
    vault = _resolve_vault(args)
    entries = sorted(vault.entries, key=lambda e: e.id)
    if not entries:
        print("(catalog is empty; use 'backup', 'import' or 'discover --add')")
        return 0
    print(f"{'ID':<26}{'Kind':<10}{'Slots':<6}{'Label'}")
    print("-" * 80)
    for e in entries:
        print(f"{e.id:<26}{e.kind:<10}{len(e.occupied_slots):<6}{e.label}")
    return 0


def cmd_show(args) -> int:
    if args.target == "live":
        _print_view(savedir.scan(_resolve_live(args)))
        return 0
    vault = _resolve_vault(args)
    entry = vault.get(args.target)
    if entry is None:
        _print_view(savedir.scan(_resolve_source(vault, args.target)))
        return 0
    print(f"{entry.id}  [{entry.kind}]  {entry.label}")
    print(f"path: {entry.path}")
    print(f"{'Slot':<5}{'Name':<28}{'Newest':<7}{'Saved'}")
    print("-" * 70)
    for s in entry.slots:
        if s.occupied:
            ts = max((m.timestamp for m in s.members if m.present), default=0)
            print(f"{s.slot:<5}{s.name[:27]:<28}{(s.newest_label or '?'):<7}{_fmt_ts(ts)}")
    return 0


def cmd_backup(args) -> int:
    live = _resolve_live(args)
    vault = _resolve_vault(args)
    entry = ops.create_full_backup(vault, live, label=args.label or "", include_cache=not args.no_cache)
    print(f"created full backup '{entry.id}' ({len(entry.occupied_slots)} slots) at {entry.path}")
    return 0


def cmd_restore(args) -> int:
    live = _resolve_live(args)
    vault = _resolve_vault(args)
    entry = vault.get(args.entry_id)
    if entry is None:
        sys.exit(f"error: no catalog entry '{args.entry_id}'")
    res = ops.restore_full(vault, entry, live, mirror=not args.no_mirror, allow_game_running=args.force)
    _report(res)
    return 0


def cmd_extract(args) -> int:
    vault = _resolve_vault(args)
    source = _resolve_source(vault, args.source) if args.source else _resolve_live(args)
    entry = ops.extract_slot(vault, source, args.slot, label=args.label or "")
    print(f"extracted slot {args.slot} -> '{entry.id}' at {entry.path}")
    return 0


def cmd_repopulate(args) -> int:
    live = _resolve_live(args)
    vault = _resolve_vault(args)
    source = _resolve_source(vault, args.source) if args.source else live
    res = ops.repopulate_slot(vault, source, args.src_slot, live, args.to_slot, allow_game_running=args.force)
    _report(res)
    return 0


def cmd_promote(args) -> int:
    live = _resolve_live(args)
    vault = _resolve_vault(args)
    member = {"A": 0, "B": 1}[args.member.upper()]
    res = ops.promote_member(vault, live, args.slot, member, allow_game_running=args.force)
    _report(res)
    return 0


def cmd_import(args) -> int:
    vault = _resolve_vault(args)
    entry = ops.import_backup(vault, Path(args.path), label=args.label or "", copy_into_vault=args.copy)
    print(f"imported '{entry.id}' [{entry.kind}] ({len(entry.occupied_slots)} slots)")
    return 0


def cmd_discover(args) -> int:
    vault = _resolve_vault(args)
    root = locations.nms_root()
    if root is None:
        sys.exit("error: could not locate the NMS root folder")
    live_dirs = {p.resolve() for p in locations.find_live_save_dirs()}
    exclude = list(live_dirs) + [vault.root]
    found = ops_discover(root, exclude)
    known = {Path(e.path).resolve() for e in vault.entries}
    for d in found:
        new = d.resolve() not in known
        print(f"{'[+]' if new else '   '} {d}")
        if new and args.add:
            entry = ops.import_backup(vault, d, label=d.name, copy_into_vault=False)
            print(f"      added as {entry.id}")
    return 0


def cmd_undo(args) -> int:
    live = _resolve_live(args)
    vault = _resolve_vault(args)
    res = ops.undo_last(vault, live, allow_game_running=args.force)
    _report(res)
    return 0


def cmd_verify(args) -> int:
    target = args.target or "live"
    if target == "live":
        path = _resolve_live(args)
    else:
        vault = _resolve_vault(args)
        path = _resolve_source(vault, target)
    view = savedir.scan(path)
    bad = [(sv.slot, m.label, m.note) for sv in view.slots.values() for m in sv.present_members if not m.valid]
    _print_view(view)
    if bad:
        print("\nINVALID members:")
        for slot, label, note in bad:
            print(f"  slot {slot}{label}: {note}")
        return 1
    print("\nall present saves valid.")
    return 0


def ops_discover(root, exclude):
    from .core.catalog import discover_save_dirs

    return discover_save_dirs(root, exclude=exclude)


def _report(res: ops.OpResult) -> None:
    print(f"{res.op}: {'OK' if res.ok else 'FAILED'} - {res.detail}")
    if res.changed:
        print(f"  changed: {', '.join(res.changed)}")
    for w in res.warnings:
        print(f"  warning: {w}")
    if res.snapshot_id:
        print(f"  (undo available: nmsvault undo  -> restores snapshot {res.snapshot_id})")


# --- argument parser ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nmsvault", description="NMS Save Vault")
    p.add_argument("--live", help="path to the live st_<id> save folder")
    p.add_argument("--vault", help="path to the vault folder")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show the live folder and slot table").set_defaults(func=cmd_status)
    sub.add_parser("list", help="list catalog entries").set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="show slots of an entry (or 'live')")
    s.add_argument("target")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("backup", help="full backup of the live folder")
    s.add_argument("--label", default="")
    s.add_argument("--no-cache", action="store_true", help="exclude the cache/ thumbnails")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("restore", help="restore a backup into the live folder")
    s.add_argument("entry_id")
    s.add_argument("--no-mirror", action="store_true", help="add/overwrite only; keep extra live slots")
    s.add_argument("--force", action="store_true", help="proceed even if the game seems to be running")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("extract", help="extract a single slot aside")
    s.add_argument("slot", type=int)
    s.add_argument("--from", dest="source", help="source entry id or folder (default: live)")
    s.add_argument("--label", default="")
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("repopulate", help="write a slot from a backup into a live slot")
    s.add_argument("--from", dest="source", help="source entry id or folder (default: live)")
    s.add_argument("--src-slot", type=int, required=True)
    s.add_argument("--to-slot", type=int, required=True)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_repopulate)

    s = sub.add_parser("promote", help="force a slot member to be the newest")
    s.add_argument("--slot", type=int, required=True)
    s.add_argument("--member", choices=["A", "B", "a", "b"], required=True)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_promote)

    s = sub.add_parser("import", help="register a manual backup folder in the catalog")
    s.add_argument("path")
    s.add_argument("--copy", action="store_true", help="copy into the vault instead of indexing in place")
    s.add_argument("--label", default="")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("discover", help="find existing backups under the NMS root")
    s.add_argument("--add", action="store_true", help="add newly found backups to the catalog (in place)")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("undo", help="undo the last operation (restore its snapshot)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_undo)

    s = sub.add_parser("verify", help="scan and validate a folder (live/entry/path)")
    s.add_argument("target", nargs="?")
    s.set_defaults(func=cmd_verify)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ops.GameRunningError as e:
        print(f"refused: {e}\n(use --force to override at your own risk)", file=sys.stderr)
        return 2
    except ops.OperationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
