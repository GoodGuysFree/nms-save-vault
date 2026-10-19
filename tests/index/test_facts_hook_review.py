"""Review of the facts hook (D1 step 13): drawn parents, the per-machine "seen" marker, a
replaced provider, nothing blocking the UI thread, NaN, exact counts, no index import."""
from __future__ import annotations

import ast
import dataclasses
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from nms_save_vault.core import organizer as organizer_pkg
from nms_save_vault.core.index import IndexService, Priority
from nms_save_vault.core.index import service as service_mod
from nms_save_vault.core.index import vaultlink
from nms_save_vault.core.index.pool import ExecutionPolicy
from nms_save_vault.core.organizer import Organizer
from nms_save_vault.core.organizer import readmodel as readmodel_mod
from nms_save_vault.core.organizer.facts import SEEN_FILENAME, facts_targets
from nms_save_vault.core.organizer.journal import Journal
from nms_save_vault.core.organizer.readmodel import sort_lines

from .test_cli import run, save, steam_pair, world  # noqa: F401 - the fixture
from .test_facts_hook import SpyFacts, _chain, sources
from .test_service import wait_for


def test_the_milestone_is_asked_on_the_drawn_parent(world, monkeypatch):  # noqa: F811
    a, b, c = _chain(world)
    drawn = "d" * 64
    original = readmodel_mod._Builder._display_parent

    def display(vid, parent, candidates):
        return drawn if vid == b else original(vid, parent, candidates)

    monkeypatch.setattr(readmodel_mod._Builder, "_display_parent", staticmethod(display))
    spy = SpyFacts(milestones={(b, drawn)})
    model = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False,
                      facts=spy).refresh(record=False)
    rows = {r.head: r for line in model.lines() for r in model.versions(line.pid)}
    assert rows[b].parent == drawn and rows[b].key is True
    assert (b, drawn) in spy.asked and (b, a) not in spy.asked


def test_versions_new_since_the_last_run_go_at_new(world):  # noqa: F811
    first = SpyFacts()
    Organizer(world["vault"], sources(world), read_only=True, machine_cache=True, facts=first).refresh(record=False)
    assert first.tier(Priority.BACKFILL) == [world["old"]] and first.tier(Priority.NEW) == []
    markers = list((world["tmp"] / "localappdata").rglob(SEEN_FILENAME))
    assert len(markers) == 1 and not list(world["vault"].rglob(SEEN_FILENAME))   # per machine, not in the vault
    older = steam_pair(world["vault"] / "full-1", 2, save(0, 0, "Synth Main", 1800), name="Synth Main",
                       play=1800, ts=1_789_000_000, ident=0x5EED0000 + 1)
    second = SpyFacts()                                     # the next run of the app
    Organizer(world["vault"], sources(world), read_only=True, machine_cache=True, facts=second).refresh(record=False)
    assert second.tier(Priority.NEW) == [older]
    assert second.tier(Priority.BACKFILL) == [world["old"]]


def test_a_replaced_provider_starts_afresh(world):  # noqa: F811
    org = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False, facts=SpyFacts())
    org.refresh(record=False)
    assert isinstance(type(org).facts, property)
    replacement = SpyFacts()
    org.facts = replacement
    org.refresh(record=False)
    assert replacement.tier(Priority.BACKFILL) == [world["old"]]


def test_reading_facts_never_touches_the_database_or_computes_on_the_caller(world, tmp_path, monkeypatch):  # noqa: F811
    loc = SimpleNamespace()
    svc = IndexService(cache_path=tmp_path / "index-v1.sqlite", vault_id="v1", locator=loc,
                       policy=ExecutionPolicy("thread", 1, False), game_running=lambda: False)
    org = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False, facts=svc)
    loc.open = lambda vid, prefer=None: org.locator.open(vid, prefer)
    try:
        org.refresh(record=False)
        wait_for(lambda: svc.idle() and svc.flush() and svc.view(world["new"]) is not None)
        threads: set[str] = set()
        real_changes = service_mod.changes_mod.changes

        def spying_changes(*args, **kwargs):
            threads.add(threading.current_thread().name)
            return real_changes(*args, **kwargs)

        monkeypatch.setattr(service_mod.changes_mod, "changes", spying_changes)
        monkeypatch.setattr(svc.cache, "facts", lambda vid: (_ for _ in ()).throw(AssertionError("sqlite read")))
        assert svc.view(world["new"]) is not None              # from memory only
        assert svc.is_milestone(world["new"], world["old"]) is None   # not known yet: asked off-thread
        monkeypatch.undo()
        wait_for(lambda: svc.is_milestone(world["new"], world["old"]) is not None)
        assert threads <= {"index-write"}
        assert ("milestone", world["new"]) in [(e.kind, e.vid) for e in svc.events_since(0)]
    finally:
        svc.close()


def test_drawn_parents_come_from_the_read_model_without_another_replay(world, monkeypatch):  # noqa: F811
    a, b, c = _chain(world)
    org = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False)
    model = org.refresh(record=False)
    calls = {"n": 0}
    real = Journal.replay

    def counting(self):
        calls["n"] += 1
        return real(self)

    monkeypatch.setattr(Journal, "replay", counting)
    parents = vaultlink.drawn_parents(org)
    assert calls["n"] == 0
    rows = {r.head: r for line in model.lines() for r in model.versions(line.pid)}
    assert parents[c] == (rows[c].parent, rows[c].parent_kind) == (b, "play")
    assert parents[b] == (a, "play") and a not in parents


def test_nan_sorts_as_missing(world):  # noqa: F811
    org = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False)
    (line,) = org.refresh(record=False).lines()
    a = dataclasses.replace(line, pid="a", name="Alpha", facts=SimpleNamespace(numbers={"units": float("nan")}))
    b = dataclasses.replace(line, pid="b", name="Bravo", facts=SimpleNamespace(numbers={"units": 5}))
    c = dataclasses.replace(line, pid="c", name="Charlie", facts=SimpleNamespace(numbers={"units": 9}))
    assert [x.name for x in sort_lines([a, b, c], "facts:units")] == ["Charlie", "Bravo", "Alpha"]
    assert [x.name for x in sort_lines([a, b, c], "facts:units", descending=False)] == ["Bravo", "Charlie", "Alpha"]


def test_request_facts_returns_the_newly_queued_count(world, capsys):  # noqa: F811
    spy = SpyFacts()
    org = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False, facts=spy)
    org.refresh(record=False, request_facts=False)
    assert spy.requests == []
    assert org.request_facts() == 2                            # the live head and the backup
    assert run(world, "build", "--workers", "1") == 0
    assert "Indexing 2 of 2 versions" in capsys.readouterr().out
    assert run(world, "build", "--workers", "1") == 0
    out = capsys.readouterr().out
    assert "Indexing 0 of 2 versions" in out and "2 already indexed or queued" in out


def test_the_cli_counts_stale_versions_moved_up_from_refresh(world, capsys, monkeypatch):  # noqa: F811
    assert run(world, "build", "--workers", "1") == 0
    capsys.readouterr()
    from nms_save_vault.core.index import cache as cache_mod
    monkeypatch.setattr(cache_mod, "EXTRACTOR_VERSION", cache_mod.EXTRACTOR_VERSION + 1)
    monkeypatch.setattr(service_mod, "EXTRACTOR_VERSION", service_mod.EXTRACTOR_VERSION + 1)
    # stale rows are queued at REFRESH at start; the build moves them up and must count them
    assert run(world, "build", "--workers", "1") == 0
    out = capsys.readouterr().out
    assert "Indexing 2 of 2 versions" in out and "0 already indexed or queued" in out


def test_the_cli_organizer_is_built_with_the_index_attached(world, capsys):  # noqa: F811
    assert run(world, "build", "--workers", "1") == 0
    capsys.readouterr()
    service, org = vaultlink.open_service(world["vault"], sources(world), workers=1)
    try:
        assert org.facts is service
        (line,) = org.model.lines()
        assert line.facts is not None and line.facts.glyphs == 3
    finally:
        service.close()


def test_one_helper_names_what_to_ask_for_first(world, monkeypatch, capsys):  # noqa: F811
    org = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False)
    org.refresh(record=False)
    live, tips = facts_targets(org.scan, org.model)
    assert live == [world["new"]] and tips == []               # the representative is live
    from nms_save_vault import index_cli
    calls = []

    def spy(scan, model):
        calls.append(1)
        return facts_targets(scan, model)

    monkeypatch.setattr(index_cli, "facts_targets", spy)
    assert run(world, "build", "--workers", "1", "--live-only") == 0
    assert calls, "the CLI must use the shared helper"


def test_the_service_starts_its_jobs_only_after_the_first_scan(world, monkeypatch):  # noqa: F811
    assert run(world, "build", "--workers", "1") == 0
    from nms_save_vault.core.index import cache as cache_mod
    monkeypatch.setattr(cache_mod, "EXTRACTOR_VERSION", cache_mod.EXTRACTOR_VERSION + 1)
    unscanned = []
    real_open = vaultlink.OrganizerLocator.open

    def open_(self, vid, prefer=None):
        if self.organizer.locator is None:
            unscanned.append(vid)
        return real_open(self, vid, prefer)

    real_refresh = Organizer.refresh

    def slow_refresh(self, *a, **kw):
        time.sleep(0.4)                                         # the scan takes a while
        return real_refresh(self, *a, **kw)

    monkeypatch.setattr(vaultlink.OrganizerLocator, "open", open_)
    monkeypatch.setattr(Organizer, "refresh", slow_refresh)
    service, org = vaultlink.open_service(world["vault"], sources(world), workers=1)
    try:
        wait_for(lambda: service.idle() and service.flush())
        assert unscanned == [] and service.cache.failures() == {}
    finally:
        service.close()


def test_changes_is_a_memory_read_computed_on_the_writer(world, tmp_path, monkeypatch):  # noqa: F811
    loc = SimpleNamespace()
    svc = IndexService(cache_path=tmp_path / "index-v1.sqlite", vault_id="v1", locator=loc,
                       policy=ExecutionPolicy("thread", 1, False), game_running=lambda: False)
    org = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False, facts=svc)
    loc.open = lambda vid, prefer=None: org.locator.open(vid, prefer)
    try:
        org.refresh(record=False)
        wait_for(lambda: svc.idle() and svc.flush() and svc.view(world["old"]) is not None)
        threads = set()
        real = service_mod.changes_mod.changes
        monkeypatch.setattr(service_mod.changes_mod, "changes",
                            lambda *a, **k: (threads.add(threading.current_thread().name), real(*a, **k))[1])
        real_facts = svc.cache.facts
        svc.cache.facts = lambda vid: (_ for _ in ()).throw(AssertionError("sqlite read"))
        assert svc.changes(world["new"], world["old"]) is None      # asked off-thread, not computed here
        svc.cache.facts = real_facts
        wait_for(lambda: svc.changes(world["new"], world["old"]) is not None)
        assert threads == {"index-write"}
    finally:
        svc.close()


def test_drawn_parents_cover_pair_partners():
    row = SimpleNamespace(head="h", partner="p", parent="x", parent_kind="play")
    model = SimpleNamespace(lines=lambda: [SimpleNamespace(pid=1)], versions=lambda pid: [row])
    assert vaultlink.drawn_parents(SimpleNamespace(model=model)) == {"h": ("x", "play"), "p": ("x", "play")}


def test_the_relation_of_a_skipped_ancestor_is_not_the_direct_edges_kind(world, monkeypatch):  # noqa: F811
    a, b, c = _chain(world)
    drawn = "d" * 64
    original = readmodel_mod._Builder._display_parent
    monkeypatch.setattr(readmodel_mod._Builder, "_display_parent", staticmethod(
        lambda vid, parent, candidates: drawn if vid == b else original(vid, parent, candidates)))
    model = Organizer(world["vault"], sources(world), read_only=True, machine_cache=False).refresh(record=False)
    rows = {r.head: r for line in model.lines() for r in model.versions(line.pid)}
    assert rows[b].parent == drawn and rows[b].parent_kind is None and rows[b].parent_tier is None
    assert rows[c].parent == b and rows[c].parent_kind == "play"


def test_the_seen_marker_is_not_saved_when_the_index_is_off(world):  # noqa: F811
    svc = IndexService(cache_path=None, vault_id="v1", locator=None, policy=ExecutionPolicy("off", 0, False))
    org = Organizer(world["vault"], sources(world), read_only=True, machine_cache=True, facts=svc)
    org.refresh(record=False)
    assert not list((world["tmp"] / "localappdata").rglob(SEEN_FILENAME))


def test_the_organizer_never_imports_the_index():
    root = Path(organizer_pkg.__file__).parent
    found = []
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and "index" in node.module.split("."):
                found.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Import) and any("index" in a.name.split(".") for a in node.names):
                found.append(f"{path.name}:{node.lineno}")
    assert found == []
