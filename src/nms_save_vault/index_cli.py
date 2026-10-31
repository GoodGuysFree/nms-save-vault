"""``nmsvault-cli index ...``: the content index from the command line (D4 2.12).

    nmsvault-cli index status                       counts by status, stale rows, cache size, policy
    nmsvault-cli index build [--workers N] [--rebuild] [--live-only]
    nmsvault-cli index show <vid-prefix> [--json] [--card]
    nmsvault-cli index changes <child> [<parent>]   parent defaults to the recorded one

Read-only towards the vault and the save folders: the organizer is opened read-only, so the
only files written are the per-machine index cache and scan cache. Versions are named as
everywhere else (a live slot like S9, 8+ hex digits, ``<save>@YYYY-MM-DD``) or by 6+ hex
digits of their id. Output goes through the account-alias filter.
"""
from __future__ import annotations

import argparse
import builtins
import json
import sys
import time

from pathlib import Path

from .core import aliases, safety
from .core.index import IndexService, Priority, derive
from .core.index import vaultlink
from .core.organizer import Organizer
from .core.organizer.facts import facts_targets
from .core.organizer.readmodel import RefError, resolve_version

WAIT_S = 300.0
STUCK_S = 15.0           # nothing can start this long: say why and stop


def _out(*args, **kwargs) -> None:
    builtins.print(*(aliases.redact(str(a)) for a in args), **kwargs)


def _err(*args) -> None:
    """A warning or a failure: stderr, so a script reading the result does not take it for one."""
    _out(*args, file=sys.stderr)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="index_command", required=True)
    s = sub.add_parser("status", help="counts by status, stale rows, cache size, execution policy")
    s.set_defaults(func=cmd_status)
    s = sub.add_parser("build", help="index every version now, in the foreground")
    s.add_argument("--workers", default="auto", help="worker processes: auto, 1-8, or 0 for off")
    s.add_argument("--rebuild", action="store_true", help="forget every cached fact and index again")
    s.add_argument("--live-only", action="store_true", help="only the saves in the live folders")
    s.add_argument("--timeout", type=float, default=3600.0, help="give up after this many seconds")
    s.set_defaults(func=cmd_build)
    s = sub.add_parser("show", help="the facts of one version")
    s.add_argument("ref", help="a version: S9, 8+ hex digits, <save>@YYYY-MM-DD, or 6+ hex digits")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--card", action="store_true", help="the full version card")
    s.set_defaults(func=cmd_show)
    s = sub.add_parser("changes", help="what changed against the recorded parent (or a version you name)")
    s.add_argument("child", help="the version to explain (same forms as 'show')")
    s.add_argument("parent", nargs="?", help="compare with this version instead of the recorded parent")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_changes)


def _setup(args, workers: str | int = "auto") -> tuple[IndexService, Organizer]:
    from .core import savedir
    sources = vaultlink.default_sources() if not args.live else [
        vaultlink.Source(id="live", platform=savedir.platform_of(args.live) or "steam", account="", path=args.live)]
    return vaultlink.open_service(_vault_root(args), sources, workers=_workers(workers), game_running=_game_running)


def _game_running() -> bool:
    """For an explicit command-line build, "cannot tell" is allowed, with a warning; a game
    that is known to run still holds background versions back."""
    running = safety.is_game_running()
    if running is None:
        global _warned_unknown
        if not _warned_unknown:
            _err("warning: could not tell whether the game is running; indexing anyway")
            _warned_unknown = True
        return False
    return running


_warned_unknown = False


def _vault_root(args) -> Path:
    from .core import locations
    from .core import state as appstate
    if args.vault:
        return Path(args.vault)
    st = appstate.load()
    return Path(st.vault) if (st and st.vault) else locations.default_vault_dir()


def _workers(value: str | int) -> str | int:
    if isinstance(value, int) or value in ("auto", "off"):
        return value
    try:
        return int(value)
    except ValueError:
        sys.exit(f"error: --workers takes auto, off or a number, not {value!r}")


def _wait(service: IndexService, vids: list[str], timeout: float = WAIT_S) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if all(service.status(v) not in ("queued", "running", "not-indexed") for v in vids) and service.flush():
            return
        time.sleep(0.05)


def _await_changes(service: IndexService, child: str, parent: str, edge_kind: str | None,
                   timeout: float = WAIT_S):
    """The change set, once the writer thread has worked it out (None: not indexed)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        cs = service.changes(child, parent, edge_kind=edge_kind)
        gone = [v for v in (child, parent) if service.view(v) is None and service.status(v) not in ("queued", "running")]
        if cs is not None or gone:
            return cs
        time.sleep(0.02)
    return None


def _wait_deep(service: IndexService, vid: str, timeout: float = WAIT_S) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end and service.deep(vid) is None:
        if any(e.vid == vid and e.kind == "failed" for e in service.events_since(0)):
            return
        time.sleep(0.05)


def cmd_status(args) -> int:
    service, org = _setup(args)
    try:
        cache = service.cache
        _out(f"Content index: {cache.path if cache and cache.path else '-'}"
             + (" (in memory)" if cache and cache.in_memory else ""))
        if cache is not None:
            _out(f"Cache size: {cache.size_bytes() / 1024:.0f} KB")
        p = service.policy
        _out(f"Execution: {p.mode}, {p.workers} worker{'s' if p.workers != 1 else ''}"
             + (" (one kept for on-demand jobs)" if p.reserve_interactive else ""))
        known = set(org.scan.versions)
        counts: dict[str, int] = {}
        for vid in known:
            st = service.status(vid)
            counts[st] = counts.get(st, 0) + 1
        _out(f"Versions known: {len(known)}")
        for st in ("ok", "partial", "stale", "corrupt", "unsupported", "error", "unreadable", "not-indexed", "queued"):
            if counts.get(st):
                _out(f"  {st:<12}{counts[st]:>6}")
        if cache is not None and cache.warning:
            _out(cache.warning)
    finally:
        service.close()
    return 0


def cmd_build(args) -> int:
    service, org = _setup(args, args.workers)
    try:
        if service.dispatcher is None:
            _out("Content index off (--workers 0).")
            return 0
        if args.rebuild:
            service.rebuild()
        live, _tips = facts_targets(org.scan, org.model)
        wanted = live if args.live_only else list(org.scan.versions)
        started = time.monotonic()
        if args.live_only:
            queued = service.request(live, Priority.LIVE)
        else:
            queued = org.request_facts()    # D4's tiers: live, representatives and tips, new, the rest
        p = service.policy
        _out(f"Indexing {queued} of {len(wanted)} versions ({p.mode}, {p.workers} worker"
             f"{'s' if p.workers != 1 else ''}); {len(wanted) - queued} already indexed or queued.")
        stuck = _follow(service, args.timeout)
        if stuck:
            _err(stuck)
            return 2
        service.flush()
        counts: dict[str, int] = {}
        for vid in wanted:
            st = service.status(vid)
            counts[st] = counts.get(st, 0) + 1
        summary = ", ".join(f"{n} {st}" for st, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        _out(f"Done in {time.monotonic() - started:.1f} s: {summary}.")
    finally:
        service.close()
    return 0


def _follow(service: IndexService, timeout: float) -> str | None:
    """Print progress until the queue is empty. Returns why it stopped early, or None."""
    end = time.monotonic() + timeout
    last = ""
    blocked_since: float | None = None
    while not service.idle():
        p = service.progress()
        if p.message != last:
            _out(f"  {p.message}")
            last = p.message
        if p.state == "error":
            return f"Stopped: {p.message}. Facts indexed so far are kept."
        if p.state == "paused":
            return "Stopped: the content index is paused."
        if p.running == 0 and p.queued > 0:
            blocked_since = blocked_since or time.monotonic()
            if time.monotonic() - blocked_since >= STUCK_S:
                if service.dispatcher is not None and service.dispatcher.playing:
                    return (f"Stopped: the game is running, so {p.queued} older versions wait until it is closed "
                            "(live saves are indexed; use --live-only, or close the game and build again).")
                return f"Stopped: {p.queued} versions could not start."
        else:
            blocked_since = None
        if time.monotonic() >= end:
            return f"Stopped after {timeout:.0f} s: {p.queued} versions still queued."
        time.sleep(0.2)
    service.flush()                                       # the last results may fail to store
    p = service.progress()
    if p.state == "error":
        return f"Stopped: {p.message}. Facts indexed so far are kept."
    return None


def _resolve(org: Organizer, ref: str) -> str:
    """A version by the read model's references; else 6+ hex digits of any scanned version."""
    from .cli_live import badge_ref

    ref = badge_ref(ref)
    try:
        row = resolve_version(org.model, ref)
        prefix = ref.strip().lower()
        return next((v for v in row.pair if v and v.startswith(prefix)), row.head)
    except RefError as model_error:
        try:
            return vaultlink.resolve(ref, org.scan.versions)
        except LookupError:
            sys.exit(f"error: {model_error}")


def cmd_show(args) -> int:
    service, org = _setup(args)
    try:
        vid = _resolve(org, args.ref)
        service.request([vid], Priority.INTERACTIVE)
        if args.card:
            service.request_deep(vid)
        _wait(service, [vid])
        if args.card:
            _wait_deep(service, vid)
        view = service.view(vid)
        if view is None:
            _err(f"No facts for {vid[:12]}: {service.status(vid)}.")
            return 1
        if args.json:
            payload = service.card(vid).to_dict() if args.card else view.to_dict()
            _out(json.dumps(payload, ensure_ascii=False, indent=1))
        elif args.card:
            _print_card(service.card(vid))
        else:
            _print_view(view)
    finally:
        service.close()
    return 0


def cmd_changes(args) -> int:
    service, org = _setup(args)
    try:
        child = _resolve(org, args.child)
        edge_kind = None
        if args.parent:
            parent = _resolve(org, args.parent)
            heading = f"{child[:8]} compared with {parent[:8]} (your choice, not a recorded parent)"
        else:
            found = vaultlink.drawn_parents(org).get(child)
            if found is None:
                _out(f"{child[:8]}: no recorded parent")
                return 0
            parent, edge_kind = found
            relation = f"recorded parent, {edge_kind}" if edge_kind else "recorded ancestor"
            heading = f"{parent[:8]} -> {child[:8]} ({relation})"
        service.request([child, parent], Priority.INTERACTIVE)
        _wait(service, [child, parent])
        cs = _await_changes(service, child, parent, edge_kind)
        if cs is None:
            _err(f"Not indexed: {service.status(child)} / {service.status(parent)}")
            return 1
        if args.json:
            _out(json.dumps(cs.to_dict(), ensure_ascii=False, indent=1))
            return 0
        _out(heading)
        for line in cs.lines():
            _out(f"  {line}")
    finally:
        service.close()
    return 0


def _print_view(v: derive.FactsView) -> None:
    _out(f"Version {v.vid[:12]}  ({v.status}{', stale' if v.stale else ''})")
    _out(f"  Save name: {v.save_name_in_data or '-'}   primary: {v.primary}   loads: {v.active}")
    if v.chips:
        _out(f"  {' | '.join(v.chips)}")
    if v.story:
        _out(f"  Story {v.story[0]}/{v.story[1]}" + (f" ({v.story_ending})" if v.story_ending else "")
             + (f"   Atlas {v.atlas[0]}/{v.atlas[1]}" if v.atlas else ""))
    _out(f"  Bases: {v.own_bases} own, {v.base_parts} parts; planet bases {v.planet_bases} "
         f"({v.planet_base_parts} parts)" + (f"; largest '{v.largest_base[0]}' ({v.largest_base[1]})"
                                              if v.largest_base else ""))
    if v.galaxies_with_bases:
        _out("  Galaxies with bases: " + ", ".join(f"{g} {n}" for g, n in v.galaxies_with_bases))
    _out(f"  Legacy parts: {v.legacy_placed} placed in {v.legacy_placed_bases} bases; buildable "
         f"{v.legacy_buildable[0]} of 69 structures, {v.legacy_buildable[1]} of 20 freighter modules")
    _out(f"  Ships: {derive.fleet_text(v)}" + (f" - {', '.join(v.ship_names)}" if v.ship_names else ""))
    _out(f"  Companions: {v.companions}" + (f" - {', '.join(v.companion_names)}" if v.companion_names else "")
         + (f"; {v.eggs} eggs" if v.eggs else ""))
    if v.expedition:
        e = v.expedition
        _out(f"  Expedition {e['number'] or '?'} {e['name']}: {e['done']}/{e['total']} milestones"
             + (" (loads into it)" if e["active"] else ""))
    _out(f"  Units {v.units}, nanites {v.nanites}, quicksilver {v.quicksilver}; galaxy {v.current_galaxy}; "
         f"journey {v.journey_rank}; freighter {v.freighter or '-'}, frigates {v.frigates}; words {v.words}")


def _print_card(card: derive.FactsCard) -> None:
    _out(f"Version {card.vid[:12]}  ({card.status})  {card.save.get('name') or '-'}")
    if card.expedition:
        e = card.expedition
        _out(f"Expedition {e['number'] or '?'} {e['name']}: {e['done']}/{e['total']}")
    for name, ctx in card.contexts.items():
        _out(f"[{name}]")
        for chain in ctx["milestones"].values():
            done = sum(1 for s in chain["steps"] if s["state"] == "done")
            if done or any(s["state"] == "started" for s in chain["steps"]):
                _out(f"  {chain['label']}: {done}/{len(chain['steps'])}")
        for b in ctx["bases"]:
            _out(f"  base '{b['name']}' {b['type']}, {b['galaxy']}, {b['parts']} parts"
                 + (f", {b['legacy_structures'] + b['legacy_freighter']} legacy" if b['legacy_structures'] or
                    b['legacy_freighter'] else ""))
        for s in ctx["ships"]:
            _out(f"  ship '{s['name']}' {s['type']} {s['class'] or ''}{' (primary)' if s['primary'] else ''}")
        for c in ctx["companions"]:
            _out(f"  companion '{c['name']}'")
        for label, value in ctx["stats"].items():
            _out(f"  {label}: {value}")
    for e in card.errors:
        _out(f"  error in {e['extractor']}: {e['error']}")
    for name, ctx in (card.deep or {}).get("contexts", {}).items():
        _out(f"[{name}, decoded on demand]")
        for b in ctx["bases"]:
            top = sorted(b["parts_by_id"].items(), key=lambda kv: -kv[1])[:5]
            _out(f"  base '{b['name']}': " + ", ".join(f"{part} x{n}" for part, n in top))
        for container, slots in ctx["inventories"].items():
            if slots:
                _out(f"  {container}: {len(slots)} slots filled")
        if ctx["loose_objects"]:
            _out(f"  loose placeables: {sum(ctx['loose_objects'].values())}")
