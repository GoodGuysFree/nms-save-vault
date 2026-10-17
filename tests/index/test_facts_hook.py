"""D1 step 13, the facts hook: the read model's representative facts, ``facts:<name>``
sorting, requests by D4 priority, and D4's milestone rule for key versions (D4 2.6)."""
from __future__ import annotations

import dataclasses
import shutil
from types import SimpleNamespace

from nms_save_vault.core.index import IndexService, Priority
from nms_save_vault.core.index.pool import ExecutionPolicy
from nms_save_vault.core.organizer import Organizer
from nms_save_vault.core.organizer.readmodel import FactsProvider, sort_lines
from nms_save_vault.core.state import Source

from .test_cli import save, steam_pair, world  # noqa: F401 - the fixture
from .test_service import wait_for


class SpyFacts:
    """A FactsProvider that answers from dictionaries and records every request."""

    def __init__(self, views=None, milestones=()) -> None:
        self.views = dict(views or {})
        self.milestones = set(milestones)
        self.requests: list[tuple[Priority, list[str]]] = []
        self.asked: list[tuple[str, str]] = []

    def view(self, vid):
        return self.views.get(vid)

    def changes(self, child, parent, *, edge_kind=None):
        return None

    def is_milestone(self, child, parent):
        self.asked.append((child, parent))
        return (child, parent) in self.milestones

    def request(self, vids, priority):
        self.requests.append((Priority(priority), list(vids)))
        return len(vids)

    def tier(self, priority):
        return [v for p, vids in self.requests if p == priority for v in vids]


def sources(world) -> list[Source]:  # noqa: F811
    return [Source(id="live", platform="steam", account="", path=str(world["live"]))]


def test_the_index_service_is_the_read_models_facts_provider(tmp_path):
    svc = IndexService(cache_path=tmp_path / "index-v1.sqlite", vault_id="v1", locator=None,
                       policy=ExecutionPolicy("off", 0, False))
    assert isinstance(svc, FactsProvider) and isinstance(SpyFacts(), FactsProvider)


def test_lines_and_versions_carry_their_facts_and_the_index_is_asked_by_tier(world):  # noqa: F811
    spy = SpyFacts(views={world["new"]: SimpleNamespace(numbers={"glyphs": 3}),
                          world["old"]: SimpleNamespace(numbers={"glyphs": 1})})
    org = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False, facts=spy)
    model = org.refresh(record=False)
    (line,) = model.lines()
    assert line.representative == world["new"] and line.facts is spy.views[world["new"]]
    rows = {r.head: r for r in model.versions(line.pid)}
    assert rows[world["old"]].facts is spy.views[world["old"]]
    assert spy.tier(Priority.LIVE) == [world["new"]]
    assert world["new"] in spy.tier(Priority.TIP)
    assert spy.tier(Priority.BACKFILL) == [world["old"]]
    assert spy.tier(Priority.NEW) == []

    # An older copy of the same save turns up in the backup (its slot 2): a version first seen now.
    older = steam_pair(world["vault"] / "full-1", 2, save(0, 0, "Synth Main", 1800), name="Synth Main",
                       play=1800, ts=1_789_000_000, ident=0x5EED0000 + 1)
    org.refresh(record=False)
    assert spy.tier(Priority.NEW) == [older]
    assert spy.tier(Priority.BACKFILL) == [world["old"]]    # backfill is asked once per session


def test_sorting_by_a_fact_puts_lines_without_it_last(world):  # noqa: F811
    org = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False)
    (line,) = org.refresh(record=False).lines()
    a = dataclasses.replace(line, pid="a", name="Alpha", facts=SimpleNamespace(numbers={"glyphs": 2}))
    b = dataclasses.replace(line, pid="b", name="Bravo", facts=SimpleNamespace(numbers={"glyphs": 16}))
    c = dataclasses.replace(line, pid="c", name="Charlie", facts=None)
    d = dataclasses.replace(line, pid="d", name="Delta", facts=SimpleNamespace(numbers={}))
    assert [x.name for x in sort_lines([c, a, d, b], "facts:glyphs")] == ["Bravo", "Alpha", "Charlie", "Delta"]
    assert [x.name for x in sort_lines([c, a, d, b], "facts:glyphs", descending=False)] == \
        ["Alpha", "Bravo", "Charlie", "Delta"]


def _chain(world):  # noqa: F811
    """Three versions of one live slot, each journaled: A -> B -> C (B is a plain middle node)."""
    live, vault = world["live"], world["vault"]
    writable = Organizer(vault, sources(world), machine_cache=False)
    writable.refresh(record=True)
    a = world["new"]
    keep = vault / "keep"
    keep.mkdir()
    b = steam_pair(live, 1, save(6, 0b111, "Synth Main", 9500), name="Synth Main", play=9500, ts=1_790_150_000)
    shutil.copytree(live, keep / "b")
    Organizer(vault, sources(world), machine_cache=False).refresh(record=True)
    c = steam_pair(live, 1, save(7, 0b111, "Synth Main", 10000), name="Synth Main", play=10000, ts=1_790_160_000)
    Organizer(vault, sources(world), machine_cache=False).refresh(record=True)
    return a, b, c


def test_a_milestone_makes_a_middle_version_key(world):  # noqa: F811
    a, b, c = _chain(world)
    plain = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False,
                      facts=SpyFacts()).refresh(record=False)
    (line,) = [ln for ln in plain.lines() if ln.representative == c]
    assert {r.head: r.key for r in plain.versions(line.pid)}[b] is False
    spy = SpyFacts(milestones={(b, a)})
    marked = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False,
                       facts=spy).refresh(record=False)
    assert {r.head: r.key for r in marked.versions(line.pid)}[b] is True
    assert (b, a) in spy.asked


def test_with_the_real_index_the_line_shows_its_facts(world, tmp_path):  # noqa: F811
    svc = IndexService(cache_path=tmp_path / "index-v1.sqlite", vault_id="v1", locator=None,
                       policy=ExecutionPolicy("thread", 1, False), game_running=lambda: False)
    org = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False, facts=svc)
    svc.locator = SimpleNamespace(open=lambda vid, prefer=None: org.locator.open(vid, prefer))
    try:
        org.refresh(record=False)                           # facts are requested ...
        wait_for(lambda: svc.idle() and svc.flush() and svc.status(world["new"]) == "ok")
        (line,) = org.refresh(record=False).lines()         # ... and shown by the next build
        assert line.facts.glyphs == 3 and line.facts.numbers["glyphs"] == 3
        assert sort_lines([line], "facts:glyphs") == (line,)
    finally:
        svc.close()
