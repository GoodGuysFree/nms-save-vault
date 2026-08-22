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
    nmsvault import "E:\\Backup\\_SaveVault" [--copy]   # a whole copied vault
    nmsvault discover [--add]
    nmsvault accounts --set 7656119...=Main    # hide the real account ids
    nmsvault undo
    nmsvault verify [live|<entry-id>|<path>]
"""
from __future__ import annotations

import argparse
import builtins
import sys
from datetime import datetime
from pathlib import Path

from .core import aliases, catalog, discover, locations, operations as ops
from .core import savedir, slotmap
from .core import state as appstate
from .core.catalog import Vault

_print = builtins.print


def print(*args, **kwargs) -> None:  # noqa: A001 - deliberate module-wide output filter
    """Print through the account-alias filter.

    Shadowing the builtin is what makes the guarantee structural: every line this
    module emits -- existing or added later -- is filtered, so a configured alias
    cannot leak a real account id. ``cmd_accounts`` calls ``_print`` directly; it is
    the one command meant to show the real ids.
    """
    _print(*(aliases.redact(a) for a in args), **kwargs)


def _die(message: str):
    """Exit with an error message, filtered like everything else the CLI prints."""
    sys.exit(aliases.redact(message))


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
    cloud = "   (Steam Cloud: on)" if view.steam_cloud else ""
    print(f"Save folder: {view.path}   (account data: {'yes' if view.account_present else 'no'}){cloud}")
    print(f"{'Slot':<5}{'Name':<28}{'Difficulty':<11}{'Play Time':<10}{'Newest':<15}{'Saved':<18}Notes")
    print("-" * 100)
    for slot in sorted(view.slots):
        sv = view.slots[slot]
        if not sv.occupied:
            print(f"{slot:<5}{'<empty>':<28}")
            continue
        n = sv.newest
        notes = []
        for m in sv.members:
            if m.exists and m.note:
                notes.append(f"{m.save_type_label}:{m.note}")
        name = sv.display_name
        difficulty = n.info.difficulty_label if (n and n.info) else ""
        play = n.info.total_play_time if (n and n.info) else 0
        print(
            f"{slot:<5}{name[:27]:<28}{difficulty:<11}{_fmt_playtime(play):<10}"
            f"{(n.save_type_label if n else '?'):<15}"
            f"{_fmt_ts(n.effective_timestamp if n else 0):<18}{'; '.join(notes)}"
        )
        # show both members
        for m in sv.members:
            if m.exists:
                tag = "*" if (n and m.label == n.label) else " "
                sync = f"  [cloud: {m.cloud_status}]" if m.cloud_status else ""
                print(
                    f"    {tag}{m.save_type_label:<14}{m.save_name[:24]:<25} "
                    f"{('valid' if m.valid else 'INVALID'):<8} "
                    f"{_fmt_ts(m.effective_timestamp)}{sync}  {m.info.save_summary if m.info else ''}"
                )


# --- resolution helpers ------------------------------------------------------


def _resolve_live(args) -> Path:
    if args.live:
        return Path(args.live)
    # Prefer the active writable source (Steam or Xbox) recorded in the config.
    st = appstate.load()
    if st is not None:
        for s in st.live_sources:
            if s.writable and s.exists:
                return Path(s.path)
    d = locations.default_live_save_dir()
    if d is None:
        _die("error: could not locate a live NMS save folder; pass --live <dir>")
    return d


def _resolve_vault(args) -> Vault:
    if args.vault:
        root = Path(args.vault)
    else:
        st = appstate.load()
        root = Path(st.vault) if (st and st.vault) else locations.default_vault_dir()
    return Vault(root).load()


def _resolve_source(vault: Vault, value: str) -> Path:
    entry = vault.get(value)
    if entry is not None:
        return Path(entry.path)
    p = Path(value)
    if p.is_dir():
        return p
    _die(f"error: source '{value}' is neither a catalog entry id nor a folder")


# --- commands ----------------------------------------------------------------


def cmd_status(args) -> int:
    live = _resolve_live(args)
    vault = _resolve_vault(args)
    running = ops.safety.is_game_running()
    state = {True: "RUNNING (writes blocked)", False: "not running", None: "unknown"}[running]
    print(f"Game: {state}")
    print(f"Vault: {vault.root}   ({len(vault.entries)} catalog entries)")
    _print_view(savedir.scan_any(live))
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
        _print_view(savedir.scan_any(_resolve_live(args)))
        return 0
    vault = _resolve_vault(args)
    entry = vault.get(args.target)
    if entry is None:
        _print_view(savedir.scan_any(_resolve_source(vault, args.target)))
        return 0
    print(f"{entry.id}  [{entry.kind}]  {entry.label}")
    print(f"path: {entry.path}")
    print(f"{'Slot':<5}{'Name':<28}{'Newest':<15}{'Saved'}")
    print("-" * 70)
    for s in entry.slots:
        if s.occupied:
            ts = max((m.timestamp for m in s.members if m.present), default=0)
            newest = slotmap.save_type_label_of(s.newest_label or "") or "?"
            print(f"{s.slot:<5}{s.name[:27]:<28}{newest:<15}{_fmt_ts(ts)}")
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
        _die(f"error: no catalog entry '{args.entry_id}'")
    if entry.kind == catalog.KIND_EXTRACT:
        # A single-slot extract goes back into its own slot; mirroring the live folder
        # onto it would delete every other save.
        res = ops.restore_entry(vault, entry, live, allow_game_running=args.force)
    else:
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
    path = Path(args.path)
    if catalog.looks_like_vault_dir(path):
        res = ops.import_vault(vault, path, copy_into_vault=args.copy)
        _report(res)
        return 0
    entry = ops.import_backup(vault, path, label=args.label or "", copy_into_vault=args.copy)
    print(f"imported '{entry.id}' [{entry.kind}] ({len(entry.occupied_slots)} slots)")
    return 0


def cmd_discover(args) -> int:
    vault = _resolve_vault(args)
    found: list = []
    root = locations.nms_root()
    if root and root.is_dir():
        live_dirs = {p.resolve() for p in locations.find_live_save_dirs()}
        found += ops_discover(root, list(live_dirs) + [vault.root])
    found += locations.find_microsoft_save_dirs()  # Xbox / Game Pass
    if not found:
        print("(no backups found under the Steam NMS root or the Xbox wgs folder)")
        return 0
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
    view = savedir.scan_any(path)
    bad = [
        (sv.slot, m.save_type_label, m.note)
        for sv in view.slots.values()
        for m in sv.present_members
        if not m.valid
    ]
    _print_view(view)
    if bad:
        print("\nINVALID saves:")
        for slot, label, note in bad:
            print(f"  slot {slot} {label}: {note}")
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


def cmd_sources(args) -> int:
    """List the configured live sources; optionally (re)discover and merge new ones."""
    st = appstate.load()
    if st is None:
        st = discover.bootstrap_state()
        appstate.save(st)
        print("Initialised config by auto-discovery.")
    if args.rescan:
        added = discover.merge_live_sources(st)
        appstate.save(st)
        print(f"Rescan: added {added} new live source(s).")
    print(f"Config: {appstate.default_state_path()}")
    print(f"Vault:  {st.vault}\n")
    print(f"{'Role':<7}{'Platform':<9}{'Write':<7}{'Account':<20}Path")
    print("-" * 92)
    for s in st.sources:
        missing = "" if s.exists else "   (MISSING)"
        # Redact before padding: the column width has to be measured on what is shown.
        account = aliases.redact(s.account) or "-"
        print(
            f"{s.role:<7}{s.platform:<9}{('yes' if s.writable else 'no'):<7}"
            f"{account:<20}{s.path}{missing}"
        )
    if not st.sources:
        print("(none found)")
    return 0


def cmd_accounts(args) -> int:
    """List / set the display names that replace the real account ids.

    This is the one command that prints the real identifiers -- it is where they are
    configured, so it uses the unfiltered ``_print``.
    """
    path = aliases.default_path()
    amap = aliases.load(path)

    changed = False
    for pair in args.set or []:
        account, sep, alias = pair.partition("=")
        if not sep or not account.strip():
            _die(f"error: --set expects <account-id>=<display name>, got '{pair}'")
        amap.set(account, alias)
        changed = True
    for account in args.clear or []:
        amap.clear(account)
        changed = True
    if changed:
        aliases.save(amap, path)
        aliases.set_active(amap)

    st = appstate.load()
    known = [(s.platform, s.account) for s in (st.sources if st else []) if s.account]
    seen = {account for _p, account in known}
    known += [("-", account) for account in amap.entries if account not in seen]

    _print(f"Config: {path}" + ("" if path.is_file() else "   (not created yet)"))
    if not known:
        _print("(no accounts known yet -- run 'nmsvault sources' first)")
        return 0
    _print("")
    _print(f"{'Platform':<9}{'Account id':<52}Shows as")
    _print("-" * 92)
    for platform, account in known:
        _print(f"{platform:<9}{account:<52}{amap.alias_for(account) or '(not set)'}")
    _print("")
    _print('Set:   nmsvault accounts --set <account-id>="Display name"')
    _print("Clear: nmsvault accounts --clear <account-id>")
    return 0


# --- argument parser ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nmsvault", description="NMS Save Vault")
    p.add_argument("--live", help="path to the live st_<id> save folder")
    p.add_argument("--vault", help="path to the vault folder")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show the live folder and slot table").set_defaults(func=cmd_status)
    sub.add_parser("list", help="list catalog entries").set_defaults(func=cmd_list)

    s = sub.add_parser("sources", help="list configured live sources (Steam/Xbox accounts)")
    s.add_argument("--rescan", action="store_true", help="re-detect live folders and merge any new ones")
    s.set_defaults(func=cmd_sources)

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

    s = sub.add_parser("import", help="register a save folder, or a whole copied vault, in the catalog")
    s.add_argument("path")
    s.add_argument("--copy", action="store_true", help="copy files into the vault instead of indexing in place")
    s.add_argument("--label", default="")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("discover", help="find existing backups under the NMS root")
    s.add_argument("--add", action="store_true", help="add newly found backups to the catalog (in place)")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("undo", help="undo the last operation (restore its snapshot)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_undo)

    s = sub.add_parser(
        "accounts",
        help="show/set the display names that hide the real Steam/Xbox account ids",
    )
    s.add_argument("--set", action="append", metavar="ID=NAME",
                   help="show NAME wherever account ID would appear (repeatable)")
    s.add_argument("--clear", action="append", metavar="ID",
                   help="drop an account's display name (repeatable)")
    s.set_defaults(func=cmd_accounts)

    s = sub.add_parser("verify", help="scan and validate a folder (live/entry/path)")
    s.add_argument("target", nargs="?")
    s.set_defaults(func=cmd_verify)
    return p


def main(argv=None) -> int:
    # Save names/summaries can contain Unicode the Windows console (cp1252) can't encode.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ops.FeatureNotYetAvailableError as e:
        print(f"coming soon: {e}", file=sys.stderr)
        return 3
    except ops.GameRunningError as e:
        print(f"refused: {e}\n(use --force to override at your own risk)", file=sys.stderr)
        return 2
    except ops.OperationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
