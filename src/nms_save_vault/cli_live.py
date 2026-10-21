"""``nmsvault-cli place | swap | keep | undo | ops | recover | clear | panic-restore``: the live
operations (D5 4.5). Kept apart from ``cli.py`` like ``cli_store.py``. Every command prints the
plan's warnings and the outcome text from core; a non-interactive run needs ``--yes`` plus
``--confirm CODE`` for each confirmation the plan asks.

Targets are written ``steam:7``, ``gog:7``, ``xbox:4``, or ``steam:<account>:7`` when a platform
has more than one live account; sources are ``--from <entry-id> --src-slot N [--member A|B]``,
``--from live:steam:4`` or ``--from vid:<prefix>``.
"""
from __future__ import annotations

import argparse
import builtins
import json
import os
import re
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
from .core.liveops import opjournal
from .core.liveops.opjournal import machine_id
from .core.liveops.planner import _live_pair
from .core.liveops.slots import read_account
from .core.liveops.targets import resolve_account
from .core.organizer import Organizer
from .core.organizer.model import LiveRef, LiveTarget, platform_name
from .core.organizer.scanner import live_refs

_print = builtins.print


def print(*args, **kwargs) -> None:  # noqa: A001 - every line goes through the alias filter
    _print(*(aliases.redact(a) for a in args), **kwargs)


def _die(message: str):
    sys.exit(aliases.redact(message))


def _err(message: str) -> None:
    _print(aliases.redact(message), file=sys.stderr)


_TARGET_WORDS = {
    "NOT_A_LIVE_FOLDER": "that folder is not a No Man's Sky save folder",
    "FOLDER_MISSING": "the live folder does not exist (check --live)",
}


def explain(exc: BaseException) -> str:
    """A failure as one plain sentence: what went wrong and with which file, never a raw code
    or a Windows error number."""
    from .core.liveops.errors import TargetError

    if isinstance(exc, TargetError):
        return _TARGET_WORDS.get(exc.code, exc.detail)
    name = getattr(exc, "filename", None)
    if isinstance(exc, FileNotFoundError):
        return (f"'{name}' was not found - check the path, and that the drive is connected"
                if name else "a file or folder was not found - check the path and the drive")
    if isinstance(exc, PermissionError):
        return f"no permission to use '{name}'" if name else "no permission to use a file"
    if isinstance(exc, OSError):
        why = exc.strerror or str(exc)
        return f"{why} ('{name}')" if name else why
    if isinstance(exc, EOFError):
        return "it needs an answer, and there is no terminal to ask (add --yes)"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):    # raised reading a file
        return f"a file it reads is damaged and cannot be understood ({exc})"
    if isinstance(exc, (ValueError, KeyError)):
        return f"Something went wrong: {exc}. Run with NMSVAULT_DEBUG=1 for details."
    return str(exc)


_MENU_WORDS = (("Tools > Vault upgrade...", "'nmsvault-cli store upgrade'"),
               ("Tools > Vault upgrade", "'nmsvault-cli store upgrade'"),
               ("Undo the vault upgrade", "'nmsvault-cli store rollback'"))


def cli_words(text: str) -> str:
    """Core texts written for the window name its menus; on the command line say the command."""
    for menu, command in _MENU_WORDS:
        text = text.replace(menu, command)
    return text


def debugging() -> bool:
    """NMSVAULT_DEBUG=1 (or true / yes) shows the full trace instead of the one-line error."""
    return os.environ.get("NMSVAULT_DEBUG", "").strip().lower() in ("1", "true", "yes")


_SLOT_HELP = ("the live slot: steam:7 or S7, gog:7 or G7, xbox:4 or X4 (S2.7 / "
              "steam:<account>:7 when a platform has several accounts)")


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
    s.add_argument("target", metavar="TARGET", help=_SLOT_HELP)
    s.add_argument("--from", dest="source", required=True,
                   help="entry id (with --src-slot), live:steam:4, or vid:<prefix>")
    s.add_argument("--src-slot", type=int, help="the slot of the entry that holds the save "
                                                "(with an entry id)")
    s.add_argument("--member", choices=["A", "B", "a", "b"],
                   help="the save in the source that must load")
    common(s)
    s.set_defaults(func=cmd_place)

    s = sub.add_parser("swap", help="exchange two occupied live slots")
    s.add_argument("first", metavar="TARGET", help=_SLOT_HELP)
    s.add_argument("second", metavar="TARGET", help=_SLOT_HELP)
    common(s)
    s.set_defaults(func=cmd_swap)

    s = sub.add_parser("keep", help="copy a live slot into the vault, changing nothing live")
    s.add_argument("target", metavar="TARGET", help=_SLOT_HELP)
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.add_argument("--confirm", action="append", default=[], metavar="CODE",
                   help="answer a confirmation the plan asks (repeatable)")
    s.add_argument("--force", action="store_true",
                   help="go on when it cannot be checked whether the game is running "
                        "(a running game always refuses)")
    s.set_defaults(func=cmd_keep)

    s = sub.add_parser("undo", help="undo the newest move of a live slot (or one move, or "
                                    "restore the whole folder)")
    s.add_argument("--to", metavar="TARGET", help="the live slot, written as above "
                                                  "(default: the only one moved)")
    s.add_argument("--op", metavar="OP_ID", help="undo this move instead of the newest")
    s.add_argument("--whole-folder", action="store_true",
                   help="restore the newest snapshot of the whole live folder (panic restore)")
    s.add_argument("--with-accountdata", action="store_true",
                   help="with --whole-folder: also roll the account data back")
    common(s)
    s.set_defaults(func=cmd_undo)

    s = sub.add_parser("ops", help="the moves made to a live slot, newest first")
    s.add_argument("target", metavar="TARGET", help=_SLOT_HELP)
    s.set_defaults(func=cmd_ops)

    s = sub.add_parser("recover", help="list, or finish, moves that were interrupted")
    s.add_argument("--op", metavar="OP_ID", help="the interrupted move to finish (default: list them)")
    s.add_argument("--choice", choices=["forward", "back", "leave"],
                   help="finish it, put it back, or leave it as it is")
    s.add_argument("--force", action="store_true",
                   help="go on when it cannot be checked whether the game is running "
                        "(a running game always refuses)")
    s.set_defaults(func=cmd_recover)

    s = sub.add_parser("clear", help="how to empty a live slot yourself (the app never does)")
    s.add_argument("target", metavar="TARGET", help=_SLOT_HELP + "; a bare number means "
                                                    "that slot of the live folder")
    s.set_defaults(func=cmd_clear)

    s = sub.add_parser("panic-restore", help="put a whole snapshot or full backup of the live "
                                             "folder back, slot by slot")
    s.add_argument("entry_id", nargs="?", help="default: the newest snapshot of this folder")
    s.add_argument("--with-accountdata", action="store_true",
                   help="also roll the account data (unlocks, settings) back")
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
        classic.refuse_disabled(live)
        if not any(classic._key(s.path) == classic._key(live) for s in sources):
            sources.append(classic._ad_hoc(live))
    try:
        org = Organizer(root, sources, read_only=False)
        host = hostmod.Host.current()
        from . import __version__

        return context_for(org, host=host, machine_id=machine_id(host),
                           app_version=f"nms-save-vault {__version__}")
    except (OSError, ValueError, RuntimeError) as exc:
        _die(f"error: the vault cannot be opened for live operations: {explain(exc)}")


_BADGE = re.compile(r"([SGX])(\d+)(?:\.(\d+))?", re.IGNORECASE)
_KINDS = {"S": "Steam", "G": "GOG", "X": "Xbox"}           # badge letter -> kind of account
_WORDS = {"steam": "Steam", "gog": "GOG", "xbox": "Xbox"}  # typed word -> kind of account


def badge_ref(text: str) -> str:
    """``steam:7`` / ``gog:7`` / ``xbox:4`` as the organizer commands write a live slot
    (``S7`` / ``G7`` / ``X4``); anything else is returned as it is."""
    parts = text.strip().split(":")
    if len(parts) == 2 and parts[0].lower() in _WORDS and parts[1].isdigit():
        return f"{_WORDS[parts[0].lower()][0]}{parts[1]}"
    return text


def parse_target(ctx, text: str) -> LiveTarget:
    """``steam:7``, ``gog:7``, ``xbox:4``, ``steam:<account>:7``, or the organizer's own ``S7``,
    ``G7``, ``X4``, ``S2.7`` (the second Steam account's slot 7)."""
    text = text.strip()
    badge = _BADGE.fullmatch(text)
    if badge:
        kind = _KINDS[badge.group(1).upper()]
        refs = _refs_of(ctx, kind)
        if badge.group(3):
            number, slot = int(badge.group(2)), int(badge.group(3))
            if not 1 <= number <= len(refs):
                _die(f"error: there is no {kind} account {number}: {len(refs)} live "
                     f"{kind} account{'' if len(refs) == 1 else 's'}")
            refs = [refs[number - 1]]
        else:
            slot = int(badge.group(2))
    else:
        parts = text.split(":")
        if len(parts) not in (2, 3) or parts[0] not in _WORDS or not parts[-1].isdigit():
            _die(f"error: {text!r} is not a slot: write steam:7 or S7, gog:7 or G7, xbox:4 or "
                 "X4, steam:<account>:7")
        kind, slot = _WORDS[parts[0]], int(parts[-1])
        refs = _refs_of(ctx, kind)
        if len(parts) == 3:
            refs = [r for r in refs if parts[1] in (r.account_key, aliases.redact(r.account_key))
                    or r.account_key.startswith(parts[1])]
    if not refs:
        _die(f"error: no live {kind} account matches {text!r}")
    if len(refs) > 1:
        _die(f"error: {len(refs)} live {kind} accounts: write {kind.lower()}:<account>:{slot} "
             f"or {kind[0]}<n>.{slot}")
    if not 1 <= slot <= 15:
        _die("error: a slot is 1 to 15")
    return refs[0].target(slot)


def _refs_of(ctx, kind: str) -> list:
    """The enabled live accounts of one kind (Steam, GOG or Xbox), in source order."""
    return [r for r in live_refs(ctx.sources) if platform_name(r.platform, r.account_key) == kind]


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
    if plan.blocked:                           # a refusal is an error: stderr, not a result
        texts = list(dict.fromkeys(cli_words(w.text) for w in plan.blocked))
        if plan.outcome and cli_words(plan.outcome) not in texts:
            _err(cli_words(plan.outcome))
        for text in texts:
            _err(f"blocked: {text}")
        return
    print(plan.outcome)
    asked = plan.confirmations
    for w in plan.warnings:
        tag = f"   (to go on without asking: --confirm {w.code})" if w.code in asked else ""
        print(f"  {w.severity}: {w.text}{tag}")
    for tp in plan.targets[:3]:
        for e in tp.evictions:
            if e.name:
                if plan.kind == "keep":
                    print(f"  keeps: '{e.name}' in {tp.label}")
                else:
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
        said = {w.code: w.text for w in plan.warnings}
        codes = "; ".join(f"--confirm {c}" + (f" (for: {said[c]})" if c in said else "")
                          for c in sorted(plan.confirmations - confirmed))
        _die(f"error: this needs your confirmation: add {codes}")
    if args.force:
        confirmed.add("GAME_STATE_UNKNOWN")
    try:
        outcome = execute(ctx, plan, confirmed)
    except ConfirmationRequired as exc:
        _die(f"error: this needs your confirmation: {', '.join(sorted(exc.codes))}")
    except LiveOpError as exc:
        _die(f"error: {explain(exc)}")
    print(f"{outcome.status}: {outcome.outcome}")
    for w in outcome.warnings:
        print(f"  {w.severity}: {w.text}")
    if outcome.status == "stale" and outcome.replan is not None:
        print("  It changed since the plan; run the command again to see the new plan.")
    if outcome.undo is not None and outcome.op_id:
        print(f"  ({outcome.undo.label}: nmsvault-cli undo --op {outcome.op_id})")
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
        if args.op not in opjournal.read_ops(ctx):
            _die(f"error: there is no move with the id {args.op} (see: nmsvault-cli ops <slot>)")
        return _run(ctx, plan_undo(ctx, args.op), args)
    target = parse_target(ctx, args.to) if args.to else _only_moved(ctx)
    rows = [r for r in op_history(ctx, target) if r.status == "done" and r.kind != "keep"
            and r.undo_of is None]
    if not rows:
        _die(f"error: no move of slot {target.slot} can be undone")
    if rows[0].kind == "panic_restore":
        _die(f"error: the newest move of slot {target.slot} put a whole snapshot back, so "
             "undoing it changes several slots. If that is what you want, run: "
             "nmsvault-cli undo --whole-folder")
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
    text = args.target if (":" in args.target or _BADGE.fullmatch(args.target.strip())) else None
    if text is None:
        if not args.target.isdigit():
            _die("error: write the slot as steam:7 or S7, gog:7 or G7, xbox:4 or X4")
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
        print(f"  First keep a copy in the vault:  nmsvault-cli keep {text or target.platform + ':' + str(target.slot)}")
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
        _die("error: more than one live account: write the slot as steam:7 or S7, gog:7 or G7, "
             "xbox:4 or X4")
    return refs[0]


def cmd_panic_restore(args) -> int:
    return _panic(_context(args), args, args.entry_id)


def _panic(ctx, args, entry_id: str | None) -> int:
    ref = _live_ref(ctx, args)
    if entry_id is None:                 # fixed now: the restore takes a fresh snapshot
        found = panic.candidates(ctx, ref)
        entry_id = found[0].id if found else None
    else:
        known = {c.id for c in panic.candidates(ctx, ref)}
        from .core.store import listing

        if entry_id not in known and listing.get_vault_entry(
                ctx.vault, ctx.versions, entry_id, live_roots=[]) is None:
            _die(f"error: there is no snapshot or backup '{entry_id}' of this folder")
    plan = panic.plan_panic_restore(ctx, ref, entry_id,
                                    restore_accountdata=getattr(args, "with_accountdata", False))
    code = _run(ctx, plan, args)
    if code == 0 and getattr(args, "with_accountdata", False):
        panic._restore_accountdata(ctx, plan, ref, entry_id)
    return code
