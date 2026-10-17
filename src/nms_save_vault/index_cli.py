"""``nmsvault index ...``: the content index from the command line (D4 2.12).

    nmsvault index status                       counts by status, stale rows, cache size, policy
    nmsvault index build [--workers N] [--rebuild] [--live-only]
    nmsvault index show <vid-prefix> [--json] [--card]
    nmsvault index changes <child> [<parent>]   parent defaults to the recorded one

Read-only towards the vault and the save folders: the only file written is the per-machine
index cache (and the scanner's own cache). Output goes through the account-alias filter.
"""
from __future__ import annotations

import argparse
import builtins
import json
import sys
import time

from .core import aliases
from .core.catalog import Vault
from .core.index import IndexService, Priority, derive
from .core.index import vaultlink

WAIT_S = 300.0


def _out(*args, **kwargs) -> None:
    builtins.print(*(aliases.redact(str(a)) for a in args), **kwargs)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="index_command", required=True)
    s = sub.add_parser("status", help="counts by status, stale rows, cache size, execution policy")
    s.set_defaults(func=cmd_status)
    s = sub.add_parser("build", help="index every version now, in the foreground")
    s.add_argument("--workers", default="auto", help="worker processes: auto, 1-8, or 0 for off")
    s.add_argument("--rebuild", action="store_true", help="forget every cached fact and index again")
    s.add_argument("--live-only", action="store_true", help="only the saves in the live folders")
    s.set_defaults(func=cmd_build)
    s = sub.add_parser("show", help="the facts of one version")
    s.add_argument("ref", help="the version id, or its first 6+ characters")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--card", action="store_true", help="the full version card")
    s.set_defaults(func=cmd_show)
    s = sub.add_parser("changes", help="what changed against the recorded parent (or a version you name)")
    s.add_argument("child")
    s.add_argument("parent", nargs="?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_changes)


def _setup(args, workers: str | int = "auto"):
    from .core import savedir
    vault = _vault(args)
    sources = vaultlink.default_sources() if not args.live else [
        vaultlink.Source(id="live", platform=savedir.platform_of(args.live) or "steam", account="", path=args.live)]
    return vaultlink.open_service(vault, sources, workers=_workers(workers)) + (vault,)


def _vault(args) -> Vault:
    from pathlib import Path

    from .core import locations
    from .core import state as appstate
    if args.vault:
        root = Path(args.vault)
    else:
        st = appstate.load()
        root = Path(st.vault) if (st and st.vault) else locations.default_vault_dir()
    return Vault(root).load()


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


def _wait_deep(service: IndexService, vid: str, timeout: float = WAIT_S) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end and service.deep(vid) is None:
        if any(e.vid == vid and e.kind == "failed" for e in service.events_since(0)):
            return
        time.sleep(0.05)


def cmd_status(args) -> int:
    service, scan, vault_id, _vault_ = _setup(args)
    try:
        cache = service.cache
        _out(f"Content index: {cache.path if cache and cache.path else '-'}"
             + (" (in memory)" if cache and cache.in_memory else ""))
        if cache is not None:
            _out(f"Cache size: {cache.size_bytes() / 1024:.0f} KB")
        p = service.policy
        _out(f"Execution: {p.mode}, {p.workers} worker{'s' if p.workers != 1 else ''}"
             + (" (one kept for on-demand jobs)" if p.reserve_interactive else ""))
        known = set(scan.versions)
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
    service, scan, _vid, _vault_ = _setup(args, args.workers)
    try:
        if service.dispatcher is None:
            _out("Content index off (--workers 0).")
            return 0
        if args.rebuild:
            service.rebuild()
        live = vaultlink.live_vids(scan)
        wanted = list(dict.fromkeys(live + ([] if args.live_only else list(scan.versions))))
        started = time.monotonic()
        queued = service.request(live, Priority.LIVE)
        if not args.live_only:
            queued += service.request([v for v in scan.versions if v not in set(live)], Priority.BACKFILL)
        p = service.policy
        _out(f"Indexing {queued} of {len(wanted)} versions ({p.mode}, {p.workers} worker"
             f"{'s' if p.workers != 1 else ''}); {len(wanted) - queued} already indexed.")
        last = ""
        while not service.idle():
            line = service.progress().message
            if line != last:
                _out(f"  {line}")
                last = line
            time.sleep(0.5)
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


def _resolve(scan, ref: str) -> str:
    try:
        return vaultlink.resolve(ref, scan.versions)
    except LookupError as exc:
        sys.exit(f"error: {exc}")


def cmd_show(args) -> int:
    service, scan, _vid, _vault_ = _setup(args)
    try:
        vid = _resolve(scan, args.ref)
        service.request([vid], Priority.INTERACTIVE)
        if args.card:
            service.request_deep(vid)
        _wait(service, [vid])
        if args.card:
            _wait_deep(service, vid)
        view = service.view(vid)
        if view is None:
            _out(f"No facts for {vid[:12]}: {service.status(vid)}.")
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
    service, scan, _vid, vault = _setup(args)
    try:
        child = _resolve(scan, args.child)
        edge_kind = None
        if args.parent:
            parent = _resolve(scan, args.parent)
            heading = f"{child[:8]} compared with {parent[:8]} (your choice, not a recorded parent)"
        else:
            found = vaultlink.drawn_parents(vault.root, scan).get(child)
            if found is None:
                _out(f"{child[:8]}: no recorded parent")
                return 0
            parent, edge_kind = found
            heading = f"{parent[:8]} -> {child[:8]} (recorded parent, {edge_kind})"
        service.request([child, parent], Priority.INTERACTIVE)
        _wait(service, [child, parent])
        cs = service.changes(child, parent, edge_kind=edge_kind)
        if cs is None:
            _out(f"Not indexed: {service.status(child)} / {service.status(parent)}")
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
