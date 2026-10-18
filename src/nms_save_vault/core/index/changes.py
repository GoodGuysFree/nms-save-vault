"""What changed between a version and its recorded parent (D4 2.11, C14).

The parent is always D1's drawn parent (recorded, observed, "seen in backups" or the
owner's); this module never picks one, and a version without one gets no change set (N1).
Facts are compared like for like (base with base, expedition with expedition) and filtered by
the noise rules N1-N16, each found on real lineage steps: bases matched by identity, then by
(type, name, galaxy) (N3); part counts and layouts, not churning object fields (N4); 1-2 part
edits folded (N5); discoveries from monotone stats (N6); and so on.

Template text is ASCII; names inside quotes are the player's own and kept as typed.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import lookups
from .derive import companion_display, ship_display
from .lookups import _overrides

MILESTONE_WEIGHT = 85            # an item at or above this marks a key version
ONE_SESSION_S = 360              # under 6 minutes of play is not a play session
MINOR_PARTS = 2                  # N5: a base that changed by this many parts or fewer is "minor"
TOP_BASES = 3


@dataclass(frozen=True)
class Change:
    kind: str
    ctx: str                      # "base" | "expedition" | "save"
    weight: int
    text: str
    data: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ChangeSet:
    child: str
    parent: str
    header: str
    items: tuple[Change, ...]

    def top(self, n: int = 3) -> tuple[Change, ...]:
        return self.items[:n]

    def lines(self, n: int | None = None) -> list[str]:
        """The header, then the item texts (all, or the first ``n``)."""
        chosen = self.items if n is None else self.items[:n]
        return [self.header] + [c.text for c in chosen]

    def to_dict(self) -> dict[str, object]:
        return {"child": self.child, "parent": self.parent, "header": self.header,
                "items": [{"kind": c.kind, "ctx": c.ctx, "weight": c.weight, "text": c.text, "data": dict(c.data)}
                          for c in self.items]}


def is_milestone(cs: ChangeSet) -> bool:
    return any(c.weight >= MILESTONE_WEIGHT for c in cs.items)


def changes(child: Mapping, parent: Mapping, *, edge_kind: str | None = None,
            child_vid: str = "", parent_vid: str = "") -> ChangeSet:
    csave, psave = child.get("save") or {}, parent.get("save") or {}
    dt = (csave.get("play_time_s") or 0) - (psave.get("play_time_s") or 0)
    if edge_kind == "touch":
        # N7: the game re-saved (the A/B members of one save, written seconds apart).
        return ChangeSet(child_vid, parent_vid, "no progress (re-saved)", ())
    out = _Collector(primary=child.get("primary"))
    cctx, pctx = child.get("contexts") or {}, parent.get("contexts") or {}

    if csave.get("save_name") != psave.get("save_name") and (csave.get("save_name") or psave.get("save_name")):
        out.add("save.rename", "save", 15, f"renamed '{psave.get('save_name', '')}' -> '{csave.get('save_name', '')}'")

    converted = set(pctx) == {"expedition"} and "base" in cctx                # N9
    replaced = _expedition_items(out, child, parent, cctx, pctx, converted)  # N8 and friends
    if converted:
        out.add("expedition.converted", "save", 100, "expedition converted into a normal save")
    elif _active(child) != _active(parent) and _active(child) and _active(parent):
        out.add("context.switch", "save", 60, "now loads into the expedition" if _active(child) == "expedition"
                else "now loads into the base game")

    for ctx in ("base", "expedition"):
        if ctx not in cctx or ctx not in pctx:
            continue
        if ctx == "expedition" and (replaced or converted):
            continue
        _context_items(out, ctx, cctx[ctx], pctx[ctx], dt)

    items = tuple(sorted(out.items, key=lambda c: (-c.weight, -float(c.data.get("magnitude", 0)))))
    return ChangeSet(child_vid, parent_vid, _header(edge_kind, dt, bool(items)), items)


def _header(edge_kind: str | None, dt: int, has_items: bool) -> str:
    if edge_kind == "edit":
        return "edited outside the game"
    if abs(dt) >= ONE_SESSION_S:
        return f"{dt / 3600:+.1f} h played"
    if not has_items:
        return "no progress (re-saved)" if dt == 0 else f"{dt / 60:+.0f} min played, nothing indexed changed"
    return "same play time" if dt == 0 else f"{dt / 60:+.0f} min played"


class _Collector:
    def __init__(self, primary: str | None) -> None:
        self.primary = primary
        self.items: list[Change] = []

    def add(self, kind: str, ctx: str, weight: int, text: str, **data: object) -> None:
        if ctx == "expedition" and self.primary != "expedition":
            text = "[expedition] " + text
        self.items.append(Change(kind, ctx, weight, text, data))


def _active(facts: Mapping) -> str | None:
    active = (facts.get("save") or {}).get("active_context")
    return {"Main": "base", "Season": "expedition"}.get(active or "")


def _season(facts: Mapping) -> Mapping | None:
    s = (facts.get("save") or {}).get("season")
    return s if s and s.get("id") else None


def _exp_name(season: Mapping) -> str:
    return lookups.expedition_name(season.get("number"), season.get("name_key"), season.get("display"))


def _done(season: Mapping) -> tuple[int, int]:
    ms = season.get("milestones") or []
    return sum(1 for v, a, _o in ms if v >= a), len(ms)


def _stat(ctx: Mapping, name: str) -> float | None:
    v = (ctx.get("stats") or {}).get(name)
    return v if isinstance(v, (int, float)) else None


def _expedition_items(out: _Collector, child, parent, cctx, pctx, converted: bool) -> bool:
    cs, ps = _season(child), _season(parent)
    if converted or cs is None:
        return False
    if ps is None:
        out.add("expedition.start", "save", 100, f"expedition started: {_exp_name(cs)}")
        return True
    time_fell = False
    if "expedition" in cctx and "expedition" in pctx:
        ce, pe = _stat(cctx["expedition"], "TIME"), _stat(pctx["expedition"], "TIME")
        cb = _stat(cctx.get("base", {}), "TIME")
        pb = _stat(pctx.get("base", {}), "TIME")
        time_fell = ce is not None and pe is not None and ce < pe and not (cb is not None and pb is not None and cb < pb)
    if cs.get("number") != ps.get("number") or time_fell:
        out.add("expedition.replaced", "save", 100, f"new expedition: {_exp_name(cs)} (replaces {_exp_name(ps)})")
        return True
    (a, total), (b, _) = _done(ps), _done(cs)
    finished = bool(cs.get("final_reward")) and not ps.get("final_reward")
    if finished or (total and b == total and a < total):
        out.add("expedition.complete", "save", 100, f"expedition {_exp_name(cs)} completed")
    elif b != a:
        out.add("expedition.progress", "save", 45,
                f"expedition {_exp_name(cs)}: milestones {a} -> {b} of {total}", magnitude=abs(b - a))
    return False


def _context_items(out: _Collector, ctx: str, c: Mapping, p: Mapping, dt: int) -> None:
    _milestones(out, ctx, c, p)
    _bases(out, ctx, c, p)
    _ships(out, ctx, c, p)
    _companions(out, ctx, c, p)
    _simple(out, ctx, c, p)
    _inventories(out, ctx, c, p, dt)


def _milestones(out: _Collector, ctx: str, c: Mapping, p: Mapping) -> None:
    cm, pm = c.get("missions") or {}, p.get("missions") or {}
    for chain in _overrides.CHAINS:
        a, n = lookups.chain_status(chain, pm.get("progress") or {}, pm.get("version"))
        b, _ = lookups.chain_status(chain, cm.get("progress") or {}, cm.get("version"))
        label = lookups.chain_label(chain)
        if b > a and b == n:
            out.add("milestone.complete", ctx, 100, f"{label} completed", chain=chain, magnitude=b - a)
        elif b > a:
            out.add("milestone.progress", ctx, 90, f"{label} {a} -> {b} of {n}", chain=chain, magnitude=b - a)
        elif b < a:
            out.add("milestone.regress", ctx, 95, f"{label} {a} -> {b} of {n}", chain=chain, magnitude=a - b)


def _own(facts: Mapping) -> list[dict]:
    return list((facts.get("bases") or {}).get("own") or [])


def _base_label(base: Mapping) -> str:
    if base.get("type") == "FreighterBase":
        return "freighter"
    if base.get("type") == "PlayerShipBase":
        return f"corvette layout {str(base.get('id', '')).split(':', 1)[-1]}"
    return f"base '{base.get('name', '')}'"


def match_bases(c: list[dict], p: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """(matched pairs, new, gone): by identity, then unmatched ones by (type, name, galaxy) (N3)."""
    pid = {b["id"]: b for b in p}
    pairs = [(b, pid.pop(b["id"])) for b in c if b["id"] in pid]
    matched_ids = {b["id"] for b, _ in pairs}
    new = [b for b in c if b["id"] not in matched_ids]
    gone = list(pid.values())
    by_key: dict[tuple, list[dict]] = {}
    for b in gone:
        by_key.setdefault((b.get("type"), b.get("name"), b.get("galaxy")), []).append(b)
    still_new = []
    for b in new:
        candidates = by_key.get((b.get("type"), b.get("name"), b.get("galaxy")))
        if candidates:
            old = candidates.pop(0)
            gone.remove(old)
            pairs.append((b, old))
        else:
            still_new.append(b)
    return pairs, still_new, gone


def _bases(out: _Collector, ctx: str, c: Mapping, p: Mapping) -> None:
    cb, pb = _own(c), _own(p)
    pairs, new, gone = match_bases(cb, pb)
    for b in new:
        galaxy = lookups.galaxy_name(b.get("galaxy")) or "?"
        out.add("base.new", ctx, 90, f"new base '{b.get('name', '')}' ({b.get('parts', 0)} parts, {galaxy})",
                magnitude=b.get("parts", 0))
    for b in gone:
        out.add("base.gone", ctx, 88, f"base '{b.get('name', '')}' gone ({b.get('parts', 0)} parts)",
                magnitude=b.get("parts", 0))
    before = {b.get("galaxy") for b in pb if b.get("type") == "HomePlanetBase"}
    for g in sorted({b.get("galaxy") for b in cb if b.get("type") == "HomePlanetBase"} - before - {None}):
        out.add("galaxy.first_base", ctx, 85, f"first base in {lookups.galaxy_name(g)}")
    deltas = []
    minor = []
    for now, was in pairs:
        if now.get("name") != was.get("name") and now.get("type") == "HomePlanetBase":
            out.add("rename", ctx, 20, f"base '{was.get('name', '')}' renamed '{now.get('name', '')}'")
        d = now.get("parts", 0) - was.get("parts", 0)
        if d == 0 and now.get("layout") == was.get("layout"):
            continue
        (minor if abs(d) <= MINOR_PARTS else deltas).append((now, d))
    deltas.sort(key=lambda bd: -abs(bd[1]))
    for now, d in deltas[:TOP_BASES]:
        out.add("base.parts", ctx, 50, f"{_base_label(now)} parts {d:+d}", magnitude=abs(d))
    if len(deltas) > TOP_BASES:
        rest = len(deltas) - TOP_BASES
        out.add("base.more", ctx, 49, f"{rest} more base{'s' if rest != 1 else ''} changed")
    if minor:
        k = sum(d for _b, d in minor)
        out.add("base.minor", ctx, 15, f"minor edits in {len(minor)} base{'s' if len(minor) != 1 else ''} "
                f"({k:+d} part{'s' if abs(k) != 1 else ''})")
    legacy = (sum(b.get("legacy_structures", 0) + b.get("legacy_freighter", 0) for b in pb),
              sum(b.get("legacy_structures", 0) + b.get("legacy_freighter", 0) for b in cb))
    if legacy[0] != legacy[1]:
        out.add("legacy.placed", ctx, 60, f"legacy parts placed {legacy[0]} -> {legacy[1]}")


def _ship_key(s: Mapping) -> tuple:
    return (s.get("seed"), s.get("model_path"))


def _kind(s: Mapping) -> str:
    model = s.get("model_path", "")
    return lookups.ship_model_name(model) or lookups.ship_type(model)


def _ships(out: _Collector, ctx: str, c: Mapping, p: Mapping) -> None:
    cs, ps = list(c.get("ships") or []), list(p.get("ships") or [])
    remaining = Counter(_ship_key(s) for s in ps)
    by_key: dict[tuple, list[dict]] = {}
    for s in ps:
        by_key.setdefault(_ship_key(s), []).append(s)
    new = []
    for s in cs:
        key = _ship_key(s)
        if remaining[key] > 0:
            remaining[key] -= 1
            old = by_key[key].pop(0)
            if s.get("cls") != old.get("cls") and s.get("cls") and old.get("cls"):
                out.add("ship.class", ctx, 55, f"ship '{ship_display(s)}' class {old['cls']} -> {s['cls']}")
            if s.get("name") != old.get("name") and old.get("name"):
                out.add("rename", ctx, 20, f"ship '{old.get('name')}' renamed '{s.get('name', '')}'")
        else:
            new.append(s)
    gone = [s for key, left in by_key.items() for s in left]
    if len(new) == 1:
        s = new[0]
        out.add("ship.new", ctx, 80, f"new ship '{ship_display(s)}' ({', '.join(filter(None, (_kind(s), s.get('cls'))))})")
    elif new:
        out.add("ship.new", ctx, 80, f"{len(new)} new ships ({', '.join(_kind(s) for s in new)})", magnitude=len(new))
    if len(gone) == 1:
        s = gone[0]
        out.add("ship.gone", ctx, 78, f"ship '{ship_display(s)}' gone ({_kind(s)})")
    elif gone:
        out.add("ship.gone", ctx, 78, f"{len(gone)} ships gone ({', '.join(_kind(s) for s in gone)})",
                magnitude=len(gone))
    cf, pf = c.get("freighter") or {}, p.get("freighter") or {}
    if cf.get("has") and not pf.get("has"):
        out.add("freighter.new", ctx, 80, f"freighter acquired '{cf.get('name') or cf.get('model', '')}' "
                f"({cf.get('cls') or '?'})")


def _pet_key(pet: Mapping) -> tuple:
    return (pet.get("creature"), pet.get("seed"))


def _companions(out: _Collector, ctx: str, c: Mapping, p: Mapping) -> None:
    cp, pp = list(c.get("companions") or []), list(p.get("companions") or [])
    old = {}
    for pet in pp:
        old.setdefault(_pet_key(pet), []).append(pet)
    for pet in cp:
        same = old.get(_pet_key(pet))
        if same:
            was = same.pop(0)
            if was.get("name") and not pet.get("name"):
                out.add("rename", ctx, 20, f"companion '{was['name']}' name cleared")
            elif pet.get("name") != was.get("name"):
                out.add("rename", ctx, 20, f"companion '{companion_display(was)}' renamed '{pet.get('name', '')}'")
            continue
        if pet.get("name"):
            out.add("companion.new", ctx, 70, f"new companion '{pet['name']}'")
        else:
            out.add("companion.new", ctx, 70, f"new companion ({lookups.creature_type(pet.get('creature', ''))})")
    for left in old.values():
        for pet in left:
            out.add("companion.gone", ctx, 68, f"companion '{companion_display(pet)}' gone")
    cset, pset = c.get("settlements") or {}, p.get("settlements") or {}
    if (cset.get("claimed") or 0) > (pset.get("claimed") or 0):
        fresh = [n for n in cset.get("names") or [] if n not in (pset.get("names") or [])]
        for name in fresh or [""]:
            out.add("settlement.claimed", ctx, 70, f"settlement claimed '{name}'" if name else "settlement claimed")


def _plus(n: float) -> str:
    return f"{int(n):+,}"


def _units(n: float) -> str:
    return f"{n / 1e6:+.1f}M" if abs(n) >= 1e6 else _plus(n)


def _simple(out: _Collector, ctx: str, c: Mapping, p: Mapping) -> None:
    if c.get("glyphs") is not None and p.get("glyphs") is not None and c["glyphs"] != p["glyphs"]:
        out.add("glyphs", ctx, 60, f"glyphs {p['glyphs']} -> {c['glyphs']} of 16")
    ranks = [lookups.journey_rank(_stat(x, "JM")) for x in (p, c)]
    if ranks[0] and ranks[1] and ranks[0] != ranks[1]:
        out.add("journey", ctx, 40, f"journey rank {ranks[0]} -> {ranks[1]}")
    money = []
    for key, label, fmt in (("quicksilver", "quicksilver", _plus), ("nanites", "nanites", _plus), ("units", "units", _units)):
        a, b = p.get(key), c.get(key)
        if isinstance(a, int) and isinstance(b, int) and a != b:
            money.append(f"{label} {fmt(b - a)}")
    if money:
        out.add("currencies", ctx, 33, "; ".join(money))
    cd, pd = c.get("difficulty") or {}, p.get("difficulty") or {}
    if cd.get("preset") != pd.get("preset") and cd.get("preset") and pd.get("preset"):
        out.add("difficulty", ctx, 30, f"difficulty {pd['preset']} -> {cd['preset']} "
                f"(death: {pd.get('death') or '?'} -> {cd.get('death') or '?'})")
    elif cd.get("death") != pd.get("death") and cd.get("death") and pd.get("death"):
        out.add("difficulty", ctx, 30, f"death consequences {pd['death']} -> {cd['death']}")
    if isinstance(c.get("frigates"), int) and isinstance(p.get("frigates"), int) and c["frigates"] != p["frigates"]:
        out.add("frigates", ctx, 25, f"frigates {p['frigates']} -> {c['frigates']}")
    words = sum((c.get("words") or {}).values()) - sum((p.get("words") or {}).values())
    if words > 0:
        out.add("words", ctx, 22, f"words learnt +{words}", magnitude=words)
    found = []
    for stat, noun in (("DISC_SYSTEMS", "systems"), ("DISC_PLANETS", "planets"), ("DISC_CREATURES", "creatures"),
                       ("DISC_FLORA", "flora"), ("DISC_MINERALS", "minerals")):
        a, b = _stat(p, stat), _stat(c, stat)
        if a is not None and b is not None and b > a:          # N6, N15: only monotone counters, upward
            found.append(f"+{int(b - a)} {noun}")
    if found:
        out.add("discoveries", ctx, 21, "discovered " + ", ".join(found))
    if c.get("galaxy") is not None and p.get("galaxy") is not None and c["galaxy"] != p["galaxy"]:
        out.add("galaxy.current", ctx, 12, f"galaxy {lookups.galaxy_name(p['galaxy'])} -> "
                f"{lookups.galaxy_name(c['galaxy'])}")


_KINDS = (("exosuit", "exosuit"), ("freighter", "freighter"), ("storage", "storage"), ("vehicles", "vehicles"),
          ("ship:", "ship"), ("multitool:", "multi-tool"))


def _kind_of(container: str) -> str:
    return next((label for prefix, label in _KINDS if container.startswith(prefix)), container)


def _inventories(out: _Collector, ctx: str, c: Mapping, p: Mapping, dt: int) -> None:
    ci, pi = c.get("inventories") or {}, p.get("inventories") or {}
    groups: dict[str, list[int]] = {}
    for name in sorted(set(ci) | set(pi)):
        now, was = ci.get(name) or {}, pi.get(name) or {}
        if now.get("digest") == was.get("digest"):
            continue
        groups.setdefault(_kind_of(name), []).append(now.get("slots", 0) - was.get("slots", 0))
    if not groups:
        return
    parts = []
    for label, deltas in groups.items():
        net = sum(deltas)
        name = label if len(deltas) == 1 or label not in ("ship", "multi-tool") else f"{len(deltas)} {label}s"
        if net:
            name += f" ({net:+d} slot{'s' if abs(net) != 1 else ''})"
        parts.append(name)
    weight = 75 if abs(dt) < ONE_SESSION_S else 8                 # N10: without play it is the story
    out.add("inventory", ctx, weight, "inventory changed: " + ", ".join(parts))
