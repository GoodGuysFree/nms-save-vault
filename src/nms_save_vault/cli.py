"""Command-line front end for NMS Save Vault.

Examples:
    nmsvault status
    nmsvault list
    nmsvault show <entry-id>          # or: show live
    nmsvault backup --label "before X"
    nmsvault restore <entry-id>
    nmsvault extract 9 --label "main"
    nmsvault clear 9 [--yes]
    nmsvault repopulate --from <entry-id|path> --src-slot 9 --to-slot 3
    nmsvault promote --slot 9 --member A
    nmsvault import "D:\\some\\st_backup" [--copy]
    nmsvault import "E:\\Backup\\_SaveVault" [--copy]   # a whole copied vault
    nmsvault discover [--add]
    nmsvault accounts --set 7656119...=Main    # hide the real account ids
    nmsvault undo
    nmsvault verify [live|<entry-id>|<path>]

  The organizer (every save file, its versions and history):
    nmsvault saves [--sort play_time] [--live-only] [--find TEXT] [--json]
    nmsvault versions "Main Save" [--all]     # or a live slot: S9, X4
    nmsvault board
    nmsvault note "Main Save" --set "Next: Atlas path"
    nmsvault note S9 --label "before the Atlas" --lock
    nmsvault pin S9 [--off]
    nmsvault scan                             # record what the live slots hold now
    nmsvault organizer bootstrap [--dry-run]  # read in the history of an existing vault
"""
from __future__ import annotations

import argparse
import builtins
import json
import sys
from datetime import datetime
from pathlib import Path

from .core import aliases, catalog, discover, locations, operations as ops
from .core import playtime, savedir, slotmap
from .core import state as appstate
from .core.catalog import Vault
from .core.organizer import Organizer
from .core.organizer.notes import NoteConflict
from .core.organizer.readmodel import (
    SORT_KEYS,
    LineFilter,
    RefError,
    filter_lines,
    resolve_save,
    resolve_version,
    sort_lines,
)
from .core.organizer.store import StoreBusy, StoreReadOnly

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
    warns: list[str] = []
    entry = ops.create_full_backup(vault, live, label=args.label or "", include_cache=not args.no_cache, warnings=warns)
    print(f"created full backup '{entry.id}' ({len(entry.occupied_slots)} slots) at {entry.path}")
    _print_warnings(warns)
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
        res = ops.restore_entry(
            vault, entry, live, allow_game_running=args.force, restore_accountdata=args.with_accountdata
        )
    else:
        res = ops.restore_full(
            vault, entry, live, mirror=not args.no_mirror, allow_game_running=args.force,
            restore_accountdata=args.with_accountdata,
        )
    _report(res)
    return 0


def cmd_extract(args) -> int:
    vault = _resolve_vault(args)
    source = _resolve_source(vault, args.source) if args.source else _resolve_live(args)
    warns: list[str] = []
    st = appstate.load()
    live_dirs = [Path(s.path) for s in st.live_sources] if st is not None else []
    if not args.source:
        live_dirs.append(source)                     # the live folder itself
    entry = ops.extract_slot(vault, source, args.slot, label=args.label or "", warnings=warns, live_dirs=live_dirs)
    print(f"extracted slot {args.slot} -> '{entry.id}' at {entry.path}")
    _print_warnings(warns)
    return 0


def cmd_playtime(args) -> int:
    """Total play time across the live folders and the vault, one row per playthrough."""
    vault = _resolve_vault(args)
    st = appstate.load()
    live = [s.path for s in st.live_sources if s.exists] if st else [str(_resolve_live(args))]
    report = playtime.collect(vault, live)

    print(f"{'Play time':>10}  {'Platform':<12}{'Copies':>7}  Save")
    print("-" * 78)
    for p in report.playthroughs:
        mark = "" if p.identified else "  (by name)"
        print(
            f"{ops.format_duration(p.play_time):>10}  {','.join(p.platforms):<12}{p.copies:>7}  "
            f"{aliases.redact(p.name)}{mark}"
        )
    print()
    print(
        f"{len(report.playthroughs)} playthroughs in {report.saves} save files across "
        f"{report.folders} folders (live + vault), totalling "
        f"{ops.format_duration(report.total_play_time)} of play time."
    )
    if report.unidentified:
        n, u = report.unidentified, report.unsupported
        if u:
            print(
                f"{n} of them {'is' if n == 1 else 'are'} counted by name, so two runs with the same "
                "name would merge: no save id could be read, as a save predates the id the game now "
                "writes or is in the Xbox HGSAVEV2 container this version cannot read "
                f"({u} save file{'' if u == 1 else 's'})."
            )
        else:
            print(
                f"{n} of them {'is' if n == 1 else 'are'} counted by name: {'it predates' if n == 1 else 'they predate'} "
                "the save id the game now writes, so two runs with the same name would merge."
            )
    return 0


def cmd_clear(args) -> int:
    live = _resolve_live(args)
    vault = _resolve_vault(args)
    st = appstate.load()
    others = [Path(s.path) for s in st.live_sources] if st is not None else []
    plan = ops.plan_clear_slot(vault, live, args.slot, other_live_dirs=others)
    if not plan.occupied:
        print(f"slot {args.slot} is already empty; nothing to do")
        return 0

    print(f"Clearing live slot {plan.slot} of {Path(live).name} would delete:")
    print(f"  {plan.live_name}   ({ops.format_duration(plan.live_play_time)} play time)")
    if plan.entry_id is not None:
        print(
            f"Best copy in the vault: {plan.entry_id} [{plan.entry_kind}] "
            f"made {plan.entry_created}, {ops.format_duration(plan.backup_play_time)} play time"
        )
        if plan.identical:
            print("  (an identical copy: the vault holds this very save)")
    for note in plan.notes:
        print(f"  note: {note}")
    if plan.warning:
        print(f"\nWARNING: {plan.warning}")
    if plan.cloud_warning:
        print(f"\nSTEAM CLOUD: {plan.cloud_warning}")
    if not args.yes and not _confirm(f"Clear slot {plan.slot}?"):
        print("cancelled")
        return 0

    res = ops.clear_slot(vault, live, args.slot, allow_game_running=args.force)
    _report(res)
    return 0


def _confirm(question: str) -> bool:
    """Ask on the terminal. A non-interactive run must pass --yes rather than be assumed
    to have agreed, so every way stdin can fail to produce an answer -- redirected from
    nowhere (EOFError), closed (ValueError), detached (OSError), interrupted -- answers no.
    """
    try:
        answer = input(f"{question} [y/N] ")
    except (EOFError, KeyboardInterrupt, ValueError, OSError):
        return False
    return answer.strip().lower() in ("y", "yes")


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
    warns: list[str] = []
    entry = ops.import_backup(vault, path, label=args.label or "", copy_into_vault=args.copy, warnings=warns)
    print(f"imported '{entry.id}' [{entry.kind}] ({len(entry.occupied_slots)} slots)")
    _print_warnings(warns)
    return 0


def cmd_discover(args) -> int:
    vault = _resolve_vault(args)
    live = discover.live_folders(discover.all_live_paths(appstate.load()))
    search = discover.find_backups(live=live, exclude=[vault.root])
    found = search.backups
    if not found:
        where = ", ".join(str(r) for r in search.roots) or "any NMS save folder (none found)"
        print(f"(no backups found under {where}; Xbox folders hold only live saves and are not searched)")
    known = {Path(e.path).resolve() for e in vault.entries}
    for d in found:
        new = d.resolve() not in known
        print(f"{'[+]' if new else '   '} {d}")
        if new and args.add:
            warns: list[str] = []
            entry = ops.import_backup(vault, d, label=d.name, copy_into_vault=False, warnings=warns)
            print(f"      added as {entry.id}")
            _print_warnings(warns)
    if search.skipped_live:
        n = len(search.skipped_live)
        print(f"skipped {n} live save folder{'' if n == 1 else 's'} (not backups)")
    vault_is_live, aliased = discover.entries_in_live_folders(vault, live)
    if vault_is_live:
        print(
            f"note: the vault {vault.root} is or lies inside a live save folder; its entries "
            "are left as they are."
        )
    for e in aliased:
        print(
            f"note: catalog entry '{e.id}' ({e.path}) is or lies inside a live save folder, "
            "not a backup; it is left as it is."
        )
    return 0


def cmd_undo(args) -> int:
    live = _resolve_live(args)
    vault = _resolve_vault(args)
    st = appstate.load()
    others = [Path(s.path) for s in st.live_sources] if st is not None else []
    res = ops.undo_last(
        vault, live, allow_game_running=args.force, acknowledge_damage=args.accept_damaged_log,
        other_live_dirs=others, restore_accountdata=args.with_accountdata,
    )
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


def _print_warnings(warnings: list[str]) -> None:
    for w in warnings:
        print(f"  warning: {w}")


def _report(res: ops.OpResult) -> None:
    print(f"{res.op}: {'OK' if res.ok else 'FAILED'} - {res.detail}")
    if res.changed:
        print(f"  changed: {', '.join(res.changed)}")
    _print_warnings(res.warnings)
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


# --- the organizer -----------------------------------------------------------

_METHOD_WORDS = {"journal": "seen in the slot", "capture-sequence": "seen in backups",
                 "op": "put live by the app", "oplog": "put live by the app",
                 "owner": "set by you"}


def _organizer(args, *, writable: bool = False) -> Organizer:
    """The organizer over the configured vault. Only the commands that write (scan, note and
    pin changes, bootstrap) open it ``writable``; everything else opens it read-only, so not
    even the catalog is tidied (D1 2.14.2) - the per-machine scan cache is still kept."""
    st = appstate.load()
    if args.vault:
        root = Path(args.vault)
    else:
        root = Path(st.vault) if (st and st.vault) else locations.default_vault_dir()
    sources = st.live_sources if st is not None else discover.discover_live_sources()
    keep_cache = not getattr(args, "no_cache", False)
    org = Organizer(root, sources, read_only=not writable, machine_cache=keep_cache)
    for problem in org.problems:
        print(f"note: {problem}", file=sys.stderr)
    return org


def _model(args, *, writable: bool = False):
    """Listings look and never record (D1 O-D1-3): only the per-machine cache is written."""
    org = _organizer(args, writable=writable)
    return org, org.refresh(record=False)


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=1))


def _resolve(fn, model, ref):
    try:
        return fn(model, ref)
    except RefError as exc:
        _die(f"error: {exc}")


def cmd_saves(args) -> int:
    _, model = _model(args)
    wanted = LineFilter(live=True if args.live_only else None, platform=args.platform,
                        text=args.find or "")
    lines = sort_lines(filter_lines(model.lines(), wanted), args.sort)
    if args.json:
        _print_json(model.to_dict(lines))
        return 0
    if not lines:
        print("(no save files found)")
        return 0
    print(f"{'Name':<30}{'Live':<12}{'Versions':<10}{'Play':<9}{'Last played':<18}"
          f"{'Difficulty':<12}Note")
    print("-" * 110)
    for line in lines:
        live = ",".join(b.label for b in line.live) or "-"
        versions = f"{line.version_count}" + (f"+{line.gone_count}" if line.gone_count else "")
        mark = "*" if line.pinned else " "
        print(f"{mark}{line.name[:28]:<29}{live:<12}{versions:<10}"
              f"{_fmt_playtime(line.play_time):<9}{_fmt_ts(line.last_played):<18}"
              f"{line.difficulty[:11]:<12}{line.note_preview[:40]}")
    return 0


def cmd_versions(args) -> int:
    _, model = _model(args)
    pid = _resolve(resolve_save, model, args.save)
    rows = model.versions(pid)
    if not args.all:
        rows = tuple(r for r in rows if r.key)
    if args.json:
        _print_json(model.to_dict(rows))
        return 0
    line = model.line(pid)
    shown = "" if args.all else f" ({len(rows)} key versions; --all for every one)"
    print(f"{line.name}: {line.version_count} versions{shown}")
    print(f"{'Saved':<18}{'Play':<9}{'Version':<10}{'Parent':<22}{'Where':<34}Notes")
    print("-" * 110)
    for r in rows:
        parent = _METHOD_WORDS.get(r.parent_method or "", "") if r.parent else "by date"
        where = r.locations[0].label if r.locations else ("gone" if "gone" in r.flags else "-")
        flags = sorted(r.flags & {"live", "tip", "fork", "edited", "locked", "placed", "evicted"})
        notes = " ".join(filter(None, [r.label, ",".join(flags)]))
        print(f"{_fmt_ts(r.saved_at):<18}{_fmt_playtime(r.play_time):<9}{r.head[:8]:<10}"
              f"{parent:<22}{where[:33]:<34}{notes}")
    return 0


def cmd_board(args) -> int:
    _, model = _model(args)
    board = model.live_board()
    if args.json:
        _print_json(model.to_dict(board))
        return 0
    for source in board.sources:
        cloud = {True: "  (Steam Cloud: on)", False: "", None: ""}[source.steam_cloud]
        print(f"{source.label}{cloud}")
        print(f"  {'Slot':<7}{'Save':<30}{'Play':<9}{'Saved':<18}{'In vault':<15}Free first")
        for slot in source.slots:
            name = {"empty": "<empty>", "unreadable": "<cannot be read>"}.get(slot.state,
                                                                                slot.name)
            pin = " (pinned)" if slot.pinned else ""
            rank = "" if slot.suggest_rank is None else str(slot.suggest_rank + 1)
            print(f"  {slot.label:<7}{(name + pin)[:29]:<30}{_fmt_playtime(slot.play_time):<9}"
                  f"{_fmt_ts(slot.saved_at):<18}{slot.vault_copy:<15}{rank}")
    return 0


def cmd_note(args) -> int:
    version_flags = (args.label is not None or args.comment is not None or args.lock
                     or args.unlock)
    writes = version_flags or args.set is not None or args.append is not None
    org, model = _model(args, writable=writes)
    try:
        if version_flags:
            row = _resolve(resolve_version, model, args.ref)
            note = org.notes.set_version_note(
                row.head, pair=row.pair, pid=row.pid, label=args.label, comment=args.comment,
                locked=True if args.lock else (False if args.unlock else None))
            state = "locked" if note.locked else "not locked"
            print(f"Version {row.head[:8]}: label {note.label!r}, {state}")
            return 0
        pid = _resolve(resolve_save, model, args.ref)
        current = org.notes.save_note(pid)
        if args.history:
            for rev in (current.history if current else ()):
                print(f"--- revision {rev.rev}, {_fmt_ts(rev.at)}")
                print(rev.text)
            if current and current.rev:
                print(f"--- revision {current.rev} (now), {_fmt_ts(current.at)}")
                print(current.text)
            return 0
        if args.set is None and args.append is None:
            print(current.text if current and current.text else "(no note)")
            return 0
        before = current.text if current else ""
        text = args.set if args.set is not None else "\n".join(filter(None, [before, args.append]))
        saved = org.notes.set_save_note(pid, text, base_rev=current.rev if current else None)
        print(f"Note saved (revision {saved.rev}).")
        return 0
    except (NoteConflict, StoreBusy, StoreReadOnly, ValueError) as exc:
        _die(f"error: {exc}")


def cmd_pin(args) -> int:
    org, model = _model(args, writable=True)
    pid = _resolve(resolve_save, model, args.save)
    try:
        org.notes.set_pinned(pid, not args.off)
    except (StoreBusy, StoreReadOnly) as exc:
        _die(f"error: {exc}")
    state = "no longer pinned" if args.off else "pinned: never suggested for eviction"
    print(f"{model.line(pid).name}: {state}")
    return 0


def cmd_scan(args) -> int:
    org = _organizer(args, writable=True)
    try:
        model = org.refresh(record=True)
    except (StoreBusy, StoreReadOnly) as exc:
        _die(f"error: {exc}")
    observed = org.observed
    if observed is None:
        why = "; ".join(org.problems) or "the vault cannot be written"
        _die(f"error: nothing was recorded - the organizer is read-only here ({why})")
    labels = {slot.target: slot.label for src in model.live_board().sources for slot in src.slots}
    print(f"Journaled {observed.written} live slot(s).")
    for event in observed.events:
        if event.kind != "first-seen":
            print(f"  {labels.get(event.target, '?'):<6}{event.kind}")
    reasons: dict[str, int] = {}
    for _, reason in observed.skipped:
        reasons[reason] = reasons.get(reason, 0) + 1
    for reason, count in sorted(reasons.items()):
        print(f"  not recorded ({reason}): {count} slot(s)")
    return 0


def cmd_organizer_bootstrap(args) -> int:
    org = _organizer(args, writable=not args.dry_run)
    try:
        report = org.bootstrap(dry_run=args.dry_run)
    except (StoreBusy, StoreReadOnly) as exc:
        _die(f"error: {exc}")
    edges = report.edges_by_method
    print(("Would build" if report.dry_run else "Built") +
          f" the history of {report.lines} save files from {report.folders_scanned} folders "
          f"({report.versions} versions) in {report.seconds:.1f} s.")
    print(f"  links seen in backups: {edges.get('capture-sequence', 0)}; put live by the app: "
          f"{edges.get('oplog', 0) + edges.get('op', 0)}; branch points: {len(report.forks)}")
    print(f"  journal records: {report.journal_records}; old log lines placed: "
          f"{report.ops_resolved}, not placed: {len(report.ops_unresolved)}")
    print(f"  live saves with no copy in the vault: {report.at_risk_live}")
    for line, reason in report.ops_unresolved:
        print(f"  not placed - {line}: {reason}")
    for entry, reason in report.entries_skipped:
        print(f"  backup {entry}: {reason}")
    return 0


# --- argument parser ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nmsvault", description="NMS Save Vault")
    p.add_argument("--live", help="path to the live st_<id> save folder")
    p.add_argument("--vault", help="path to the vault folder")
    sub = p.add_subparsers(dest="command", required=True)
    from . import cli_store
    cli_store.add_commands(sub)

    sub.add_parser("status", help="show the live folder and slot table").set_defaults(func=cmd_status)
    sub.add_parser("list", help="list catalog entries").set_defaults(func=cmd_list)
    sub.add_parser(
        "playtime", help="total play time across live saves and the vault, deduplicated"
    ).set_defaults(func=cmd_playtime)

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
    s.add_argument(
        "--with-accountdata", action="store_true",
        help="also roll the account data (unlocks, settings) back to the backup's",
    )
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("extract", help="extract a single slot aside")
    s.add_argument("slot", type=int)
    s.add_argument("--from", dest="source", help="source entry id or folder (default: live)")
    s.add_argument("--label", default="")
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("clear", help="empty a live slot (deletes both of its saves)")
    s.add_argument("slot", type=int)
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    s.add_argument("--force", action="store_true", help="proceed even if the game seems to be running")
    s.set_defaults(func=cmd_clear)

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

    s = sub.add_parser("undo", help="undo the last operation on the live folder (restore its snapshot)")
    s.add_argument("--force", action="store_true")
    s.add_argument(
        "--accept-damaged-log", action="store_true",
        help="undo even though a newer line of the undo log is damaged (it may record a change this skips)",
    )
    s.add_argument(
        "--with-accountdata", action="store_true",
        help="also roll the account data (unlocks, settings) back to the snapshot's",
    )
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

    s = sub.add_parser("saves", help="every save file, live and in the vault, one line each")
    s.add_argument("--sort", choices=SORT_KEYS, default="last_played")
    s.add_argument("--live-only", action="store_true", help="only save files that are live")
    s.add_argument("--platform", choices=["steam", "xbox"])
    s.add_argument("--find", help="text in the name, a former name or the note")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-cache", action="store_true", help="hash everything; keep no cache")
    s.set_defaults(func=cmd_saves)

    s = sub.add_parser("versions", help="the versions of one save file")
    s.add_argument("save", help="its name, 6+ hex digits of its id, or a live slot like S9")
    s.add_argument("--all", action="store_true", help="every version, not only the key ones")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-cache", action="store_true")
    s.set_defaults(func=cmd_versions)

    s = sub.add_parser("board", help="the 15 slots of every live folder")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-cache", action="store_true")
    s.set_defaults(func=cmd_board)

    s = sub.add_parser("note", help="show or write the note of a save file or a version")
    s.add_argument("ref", help="a save file, or with --label/--comment/--lock a version")
    group = s.add_mutually_exclusive_group()
    group.add_argument("--set", help="replace the save file's note")
    group.add_argument("--append", help="add a line to the save file's note")
    group.add_argument("--history", action="store_true", help="every revision of the note")
    s.add_argument("--label", help="a version's label")
    s.add_argument("--comment", help="a version's comment")
    lock = s.add_mutually_exclusive_group()
    lock.add_argument("--lock", action="store_true", help="mark the version to keep")
    lock.add_argument("--unlock", action="store_true")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("pin", help="never suggest a save file's slot for another save")
    s.add_argument("save")
    s.add_argument("--off", action="store_true")
    s.set_defaults(func=cmd_pin)

    s = sub.add_parser("scan", help="record what the live slots hold now")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("organizer", help="organizer upkeep")
    org_sub = s.add_subparsers(dest="organizer_command", required=True)
    b = org_sub.add_parser("bootstrap", help="read in the history of an existing vault")
    b.add_argument("--dry-run", action="store_true", help="compute and report; write nothing")
    b.set_defaults(func=cmd_organizer_bootstrap)
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
