"""``nmsvault place | swap | keep | undo | ops | recover | clear | panic-restore``: the live
operations (D5 4.5). Kept apart from ``cli.py`` like ``cli_store.py``. Every command prints the
plan's warnings and the outcome text from core; a non-interactive run needs ``--yes`` plus
``--confirm CODE`` for each confirmation the plan asks.

Targets are written ``steam:7``, ``xbox:4``, or ``steam:<account>:7`` when a platform has more
than one live account; sources are ``--from <entry-id> --src-slot N [--member A|B]``,
``--from live:steam:4`` or ``--from vid:<prefix>``.
"""
from __future__ import annotations

import argparse
import builtins
import sys
from pathlib import Path

from .core import aliases, discover, locations, platform as hostmod
from .core import state as appstate
from .core.liveops import (
    ConfirmationRequired,
    LiveOpError,
    PairSource,
    PlacePlan,
    empty_slot_guide,
    execute,
    op_history,
    pending_recovery,
    plan_keep,
    plan_place,
    plan_swap,
    plan_undo,
    resolve_recovery,
)
from .core.liveops import classic, context_for, panic
from .core.liveops.opjournal import machine_id
from .core.liveops.planner import _live_pair
from .core.liveops.slots import read_account
from .core.liveops.targets import resolve_account
from .core.organizer import Organizer
from .core.organizer.model import LiveRef, LiveTarget
from .core.organizer.scanner import live_refs

_print = builtins.print


def print(*args, **kwargs) -> None:  # noqa: A001 - every line goes through the alias filter
    _print(*(aliases.redact(a) for a in args), **kwargs)


def _die(message: str):
    sys.exit(aliases.redact(message))


def add_commands(sub: argparse._SubParsersAction) -> None:
    def common(s, *, confirm: bool = True) -> None:
        s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        s.add_argument("--confirm", action="append", default=[], metavar="CODE",
                       help="answer a confirmation the plan asks (repeatable)")
        s.add_argument("--force", action="store_true",
                       help="go on when it cannot be checked whether the game is running "
                            "(a running game always refuses)")

    s = sub.add_parser("place", help="put a save into a live slot (what it replaces is kept "
                                     "in the vault first)")
    s.add_argument("target", metavar="TARGET", help="the live slot: steam:7, xbox:4")
    s.add_argument("--from", dest="source", required=True,
                   help="entry id (with --src-slot), live:steam:4, or vid:<prefix>")
    s.add_argument("--src-slot", type=int)
    s.add_argument("--member", choices=["A", "B", "a", "b"],
                   help="the save in the source that must load")
    common(s)
    s.set_defaults(func=cmd_place)

    s = sub.add_parser("swap", help="exchange two occupied live slots")
    s.add_argument("first", metavar="TARGET")
    s.add_argument("second", metavar="TARGET")
    common(s)
    s.set_defaults(func=cmd_swap)

    s = sub.add_parser("keep", help="copy a live slot into the vault, changing nothing live")
    s.add_argument("target", metavar="TARGET")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--confirm", action="append", default=[])
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_keep)

    s = sub.add_parser("undo", help="undo the newest move of a live slot (or one move, or "
                                    "restore the whole folder)")
    s.add_argument("--to", metavar="TARGET", help="the live slot (default: the only one moved)")
    s.add_argument("--op", metavar="OP_ID", help="undo this move instead of the newest")
    s.add_argument("--whole-folder", action="store_true",
                   help="restore the newest snapshot of the whole live folder (panic restore)")
    s.add_argument("--with-accountdata", action="store_true",
                   help="with --whole-folder: also roll the account data back")
    common(s)
    s.set_defaults(func=cmd_undo)

    s = sub.add_parser("ops", help="the moves made to a live slot, newest first")
    s.add_argument("target", metavar="TARGET")
    s.set_defaults(func=cmd_ops)

    s = sub.add_parser("recover", help="list, or finish, moves that were interrupted")
    s.add_argument("--op", metavar="OP_ID")
    s.add_argument("--choice", choices=["forward", "back", "leave"])
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_recover)

    s = sub.add_parser("clear", help="how to empty a live slot yourself (the app never does)")
    s.add_argument("target", metavar="TARGET", help="the live slot (a number means the "
                                                    "live folder's slot)")
    s.set_defaults(func=cmd_clear)

    s = sub.add_parser("panic-restore", help="put a whole snapshot or full backup of the live "
                                             "folder back, slot by slot")
    s.add_argument("entry_id", nargs="?", help="default: the newest snapshot of this folder")
    s.add_argument("--with-accountdata", action="store_true")
    common(s)
    s.set_defaults(func=cmd_panic_restore)


# --- the context and the words ---------------------------------------------------------------


def _context(args):
    state = appstate.load()
    if args.vault:
        root = Path(args.vault)
    else:
        root = Path(state.vault) if (state and state.vault) else locations.default_vault_dir()
    sources = list(state.live_sources if state is not None else discover.discover_live_sources())
    if getattr(args, "live", None):
        live = Path(args.live)
        if not any(Path(s.path) == live for s in sources):
            sources.append(classic._ad_hoc(live))
    try:
        org = Organizer(root, sources, read_only=False)
        host = hostmod.Host.current()
        from . import __version__

        return context_for(org, host=host, machine_id=machine_id(host),
                           app_version=f"nms-save-vault {__version__}")
    except (OSError, ValueError, RuntimeError) as exc:
        _die(f"error: the vault cannot be opened for live operations ({exc})")


def parse_target(ctx, text: str) -> LiveTarget:
    """``steam:7``, ``xbox:4`` or ``steam:<account>:7``."""
    parts = text.split(":")
    if len(parts) not in (2, 3) or parts[0] not in ("steam", "xbox") or not parts[-1].isdigit():
        _die(f"error: {text!r} is not a slot: write steam:7, xbox:4 or steam:<account>:7")
    platform, slot = parts[0], int(parts[-1])
    refs = [r for r in live_refs(ctx.sources) if r.platform == platform]
    if len(parts) == 3:
        refs = [r for r in refs if parts[1] in (r.account_key, aliases.redact(r.account_key))
                or r.account_key.startswith(parts[1])]
    if not refs:
        _die(f"error: no live {platform} account matches {text!r}")
    if len(refs) > 1:
        _die(f"error: {len(refs)} live {platform} accounts: write {platform}:<account>:{slot}")
    if not 1 <= slot <= 15:
        _die("error: a slot is 1 to 15")
    return refs[0].target(slot)


def _source(ctx, args) -> PairSource:
    spec = args.source
    if spec.startswith("live:"):
        target = parse_target(ctx, spec[len("live:"):])
        slot = read_account(ctx, resolve_account(ctx, target.ref)).slots[target.slot]
        if not slot.present:
            _die(f"error: {spec} holds no save")
        return _live_pair(target, slot)
    if spec.startswith("vid:"):
        from .core.organizer.readmodel import RefError, resolve_version

        try:
            row = resolve_version(ctx.readmodel, spec[len("vid:"):])
        except RefError as exc:
            _die(f"error: {exc}")
        a, b = row.pair
        return PairSource(a, b, args.member.upper() if args.member else None, None)
    if args.src_slot is None:
        _die("error: --src-slot is needed with an entry id")
    entry_ref = spec
    from .core.store import listing

    entry = listing.get_vault_entry(ctx.vault, ctx.versions, entry_ref, live_roots=[])
    if entry is None:
        _die(f"error: no catalog entry '{entry_ref}'")
    pairs = panic.entry_pairs(ctx, entry_ref, None)
    pair = pairs.get(args.src_slot)
    if pair is None:
        _die(f"error: slot {args.src_slot} of '{entry_ref}' holds no save")
    if args.member:
        pair = PairSource(pair.vid_a, pair.vid_b, args.member.upper(), pair.prefer)
    return pair


def _show(plan: PlacePlan) -> None:
    print(plan.outcome)
    for w in plan.blocked:
        print(f"  blocked: {w.text}")
    for w in plan.warnings:
        print(f"  {w.severity}: {w.text}")
    for tp in plan.targets[:3]:
        for e in tp.evictions:
            if e.name:
                print(f"  replaces: '{e.name}' in {tp.label}"
                      f"{'' if e.durable_before else ' (kept in the vault first)'}")
                break


def _run(ctx, plan: PlacePlan, args) -> int:
    _show(plan)
    if plan.blocked:
        return 2
    confirmed = set(args.confirm)
    if not args.yes:
        if not _confirm("Go on?"):
            print("cancelled")
            return 1
        confirmed |= set(plan.confirmations)
    elif plan.confirmations - confirmed:
        codes = ", ".join(f"--confirm {c}" for c in sorted(plan.confirmations - confirmed))
        _die(f"error: this needs your confirmation: add {codes}")
    if args.force:
        confirmed.add("GAME_STATE_UNKNOWN")
    try:
        outcome = execute(ctx, plan, confirmed)
    except ConfirmationRequired as exc:
        _die(f"error: this needs your confirmation: {', '.join(sorted(exc.codes))}")
    except LiveOpError as exc:
        _die(f"error: {exc}")
    print(f"{outcome.status}: {outcome.outcome}")
    for w in outcome.warnings:
        print(f"  {w.severity}: {w.text}")
    if outcome.status == "stale" and outcome.replan is not None:
        print("  It changed since the plan; run the command again to see the new plan.")
    if outcome.undo is not None and outcome.op_id:
        print(f"  ({outcome.undo.label}: nmsvault undo --op {outcome.op_id})")
    return 0 if outcome.status == "done" else 1


def _confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except (EOFError, KeyboardInterrupt, ValueError, OSError):
        return False
    return answer.strip().lower() in ("y", "yes")


# --- the commands -----------------------------------------------------------------------------


def cmd_place(args) -> int:
    ctx = _context(args)
    plan = plan_place(ctx, _source(ctx, args), parse_target(ctx, args.target))
    return _run(ctx, plan, args)


def cmd_swap(args) -> int:
    ctx = _context(args)
    plan = plan_swap(ctx, parse_target(ctx, args.first), parse_target(ctx, args.second))
    return _run(ctx, plan, args)


def cmd_keep(args) -> int:
    ctx = _context(args)
    plan = plan_keep(ctx, parse_target(ctx, args.target))
    args.yes = True
    return _run(ctx, plan, args)


def cmd_undo(args) -> int:
    ctx = _context(args)
    if args.whole_folder:
        return _panic(ctx, args, None)
    if args.op:
        return _run(ctx, plan_undo(ctx, args.op), args)
    target = parse_target(ctx, args.to) if args.to else _only_moved(ctx)
    rows = [r for r in op_history(ctx, target) if r.status == "done" and r.kind != "keep"
            and r.undo_of is None]
    if not rows:
        _die(f"error: no move of {target.slot} can be undone")
    return _run(ctx, plan_undo(ctx, rows[0].op_id), args)


def _only_moved(ctx) -> LiveTarget:
    seen = []
    for ref in live_refs(ctx.sources):
        for slot in range(1, 16):
            t = ref.target(slot)
            if any(r.status == "done" and r.kind != "keep" for r in op_history(ctx, t)):
                seen.append(t)
    if len(seen) != 1:
        _die("error: say which slot with --to steam:7 (or use --op OP_ID)")
    return seen[0]


def cmd_ops(args) -> int:
    ctx = _context(args)
    rows = op_history(ctx, parse_target(ctx, args.target))
    if not rows:
        print("no moves recorded for that slot")
    for r in rows:
        undo = f"  (undo of {r.undo_of})" if r.undo_of else ""
        print(f"{r.when}  {r.op_id}  {r.status:<11}{r.verify:<10}{r.text}{undo}")
    return 0


def cmd_recover(args) -> int:
    ctx = _context(args)
    items = pending_recovery(ctx)
    if args.op is None:
        if not items:
            print("nothing to recover")
        for i in items:
            print(f"{i.op_id}  {i.state:<13}recommended: {i.recommended:<8}"
                  f"choices: {', '.join(i.choices) or '-'}")
            print(f"  {i.text}")
        return 0
    item = next((i for i in items if i.op_id == args.op), None)
    if item is None:
        _die(f"error: {args.op} is not waiting to be recovered")
    choice = args.choice or item.recommended
    if choice not in item.choices:
        _die(f"error: choose one of: {', '.join(item.choices)}")
    confirmed = {"GAME_STATE_UNKNOWN"} if args.force else set()
    outcome = resolve_recovery(ctx, args.op, choice, confirmed)
    print(f"{outcome.status}: {outcome.outcome}")
    return 0 if outcome.status in ("done", "rolled_back") else 1


def cmd_clear(args) -> int:
    ctx = _context(args)
    text = args.target if ":" in args.target else None
    if text is None:
        if not args.target.isdigit():
            _die("error: write the slot as steam:7 or xbox:4")
        live = _live_ref(ctx, args)
        target = live.target(int(args.target))
    else:
        target = parse_target(ctx, text)
    guide = empty_slot_guide(ctx, target)
    print(f"The app never empties a slot. To empty {guide.label} yourself:")
    if not guide.occupied:
        print("  it is already empty.")
        return 0
    if not guide.durable:
        print(f"  First keep a copy in the vault:  nmsvault keep {text or target.platform + ':' + str(target.slot)}")
    if guide.refill is not None:
        print(f"  Note: {guide.refill.text}")
    for n, step in enumerate(guide.steps, 1):
        print(f"  {n}. {step}")
    return 0


def _live_ref(ctx, args) -> LiveRef:
    refs = live_refs(ctx.sources)
    if getattr(args, "live", None):
        for source, ref in zip(ctx.sources, refs):
            if Path(source.path) == Path(args.live):
                return ref
    if len(refs) != 1:
        _die("error: more than one live account: write the slot as steam:7 or xbox:4")
    return refs[0]


def cmd_panic_restore(args) -> int:
    return _panic(_context(args), args, args.entry_id)


def _panic(ctx, args, entry_id: str | None) -> int:
    ref = _live_ref(ctx, args)
    if entry_id is None:                 # fixed now: the restore takes a fresh snapshot
        found = panic.candidates(ctx, ref)
        entry_id = found[0].id if found else None
    plan = panic.plan_panic_restore(ctx, ref, entry_id,
                                    restore_accountdata=getattr(args, "with_accountdata", False))
    code = _run(ctx, plan, args)
    if code == 0 and getattr(args, "with_accountdata", False):
        panic._restore_accountdata(ctx, plan, ref, entry_id)
    return code
