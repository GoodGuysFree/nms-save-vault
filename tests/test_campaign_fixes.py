"""Bugs found by running the demo campaign: stale "not indexed yet" in History and Compare,
a raw dict in the deep detail, a duplicated Where block after a move, tiny sizes shown as 0.0 GB,
and a menu that does not exist in two messages."""
from __future__ import annotations

import shutil
import tempfile
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

from nms_save_vault.core import discover, platform
from nms_save_vault.core.operations import OperationError
from nms_save_vault.core.store import upgrade
from nms_save_vault.ui.organizer import compare as C
from nms_save_vault.ui.organizer import factsui
from nms_save_vault.ui.organizer import history as H

from .test_ui_history import PID, _event, _long
from .test_ui_history_review import host  # noqa: F401


def test_human_size_never_rounds_a_small_vault_to_zero():
    assert upgrade.human_size(3 << 30) == "3.0 GB"
    assert upgrade.human_size(5 << 20) == "5.0 MB"
    assert upgrade.human_size(2048) == "2 KB"
    assert upgrade.human_size(0) == "0 KB"
    from nms_save_vault.ui.organizer import vaultwin
    assert vaultwin._gb(5 << 20) == "5.0 MB"


def test_the_messages_name_a_menu_that_exists(tmp_path):
    text = upgrade._done_text({"name": "x", "bytes": 5 << 20}, 0)
    assert "Vault menu" not in text and "Tools > Vault upgrade... > Undo the vault upgrade" in text
    note = upgrade._note_text("v", {"verified_utc": "t", "files": 1, "bytes": 1})
    assert "Tools > Vault upgrade..." in note and "Vault >" not in note


def test_importing_a_pre_upgrade_copy_names_the_real_menu():
    from nms_save_vault.core import operations
    import inspect
    src = inspect.getsource(operations)
    assert "Vault menu" not in src and "Tools > Vault upgrade... > Undo the vault upgrade" in src


def test_deep_detail_has_no_python_repr():
    card = SimpleNamespace(deep={"vid": "abc", "contexts": {"base": {
        "bases": [{"id": "1", "name": "Home", "type": "x", "parts_by_id": {"A": 2, "B": 3}}],
        "inventories": {"exosuit": [{"id": "X"}, {"id": "Y"}]},
        "loose_objects": {"Z": 4}}}, "ship_class": "S", "base_names": ["Home", "Outpost"]})
    rows = dict(factsui.card_lines(card))
    assert rows["Context base"] == "1 base (5 parts), 2 inventory slots, 4 loose objects"
    assert rows["Base names"] == "Home, Outpost" and "Vid" not in rows
    assert not any("{" in v or "[" in v or "'" in v for v in rows.values())


class _Service:
    """A fake index: ``ready`` is what view() answers; changes() is not needed here."""

    def __init__(self, ready: bool):
        self.ready = ready
        self.views = {}

    def view(self, vid):
        return self.views.get(vid) if self.ready else None

    def changes(self, *a, **k):
        return None

    def become_ready(self, heads):
        self.ready = True
        self.views = {h: SimpleNamespace(own_bases=3, base_parts=900, ships=2, companions=1,
                                         ship_names=(), companion_names=()) for h in heads}


def test_history_contents_follow_a_facts_service_that_becomes_ready(host, gui_app, tmp_path):
    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    service = _Service(ready=False)
    win = H.HistoryWindow(host, model=w.refresh(), themed=gui_app.themed, pid=PID, head=f,
                          facts=lambda: service)
    try:
        assert "not indexed yet" in win.panel_text()
        service.become_ready([f])
        win.on_index_events([_event("indexed", f)])
        text = win.panel_text()
        assert "not indexed yet" not in text and "3 (900 parts)" in text
    finally:
        win.destroy()


def test_history_contents_when_the_service_was_ready_before_the_window(host, gui_app, tmp_path):
    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    service = _Service(ready=False)
    service.become_ready([f])
    win = H.HistoryWindow(host, model=w.refresh(), themed=gui_app.themed, pid=PID, head=f,
                          facts=lambda: service)
    try:
        assert "not indexed yet" not in win.panel_text() and "3 (900 parts)" in win.panel_text()
    finally:
        win.destroy()


def test_compare_follows_a_facts_service_that_becomes_ready(host, gui_app, tmp_path):
    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    service = _Service(ready=False)
    win = C.CompareWindow(host, model=w.refresh(), themed=gui_app.themed, pid=PID, a=f, b=c,
                          facts=lambda: service)
    gui_app.update()
    try:
        assert win.note.cget("text") == "content facts: not indexed yet for A and B"
        service.become_ready([f, c])
        win.on_index_events([_event("indexed", f)])
        assert win.note.cget("text") == ""
        win.set_only_differences(False)
        assert dict((r[0], r[1]) for r in win.table())["Bases"] == "3"
    finally:
        win.destroy()


def test_compare_when_the_service_was_ready_before_the_window(host, gui_app, tmp_path):
    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    service = _Service(ready=False)
    service.become_ready([f, c])
    win = C.CompareWindow(host, model=w.refresh(), themed=gui_app.themed, pid=PID, a=f, b=c,
                          facts=lambda: service)
    gui_app.update()
    try:
        assert win.note.cget("text") == ""
    finally:
        win.destroy()


def test_a_version_placed_into_steam_is_listed_once_with_each_place_once():
    from tools import make_demo_vault as demo_tool
    from nms_save_vault.core.liveops import PairSource
    from nms_save_vault.core.liveops.context import context_for
    from nms_save_vault.core.liveops.execute import execute
    from nms_save_vault.core.liveops.planner import plan_place
    from nms_save_vault.core.organizer import Organizer
    from nms_save_vault.core.organizer.model import LiveRef

    from nms_save_vault.core import aliases

    before_aliases = aliases.active()
    root = Path(tempfile.mkdtemp(prefix="nd"))
    try:
        demo = demo_tool.build(root / "w")
        assert aliases.active() is before_aliases        # a library build leaves the process alone
        sources = discover.discover_live_sources(
            demo.target / "Roaming" / "HelloGames" / "NMS", [demo.xbox])
        org = Organizer(demo.vault, sources, game_running=lambda: False)
        model = org.refresh(record=False)
        line = next(x for x in model.lines() if x.name == "Waypoint Wanderer")
        row = model.versions(line.pid)[0]
        host = platform.Host(platform.WINDOWS, root / "home",
                             {"LOCALAPPDATA": str(demo.target / "Local")})
        ctx = context_for(org, host=host, machine_id="m-1", app_version="t",
                          game_probe=lambda: False)
        steam = next(s for s in sources if s.platform == "steam")
        plan = plan_place(ctx, PairSource(row.pair[0], row.pair[1], None, None),
                          LiveRef("steam", Path(steam.path).name).target(9))
        assert execute(ctx, plan, plan.confirmations).status == "done"
        after = Organizer(demo.vault, sources, game_running=lambda: False).refresh(record=False)
        rows = after.versions(line.pid)
        assert len({r.head for r in rows}) == len(rows)
        labels = [loc.label for loc in rows[0].locations]
        assert len(labels) == len(set(labels)) and "Live: Steam slot 9" in labels
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _world(root):
    from tools import make_demo_vault as demo_tool
    from nms_save_vault.core.liveops.context import context_for
    from nms_save_vault.core.organizer import Organizer

    demo = demo_tool.build(root / "w")
    sources = discover.discover_live_sources(demo.target / "Roaming" / "HelloGames" / "NMS", [demo.xbox])
    org = Organizer(demo.vault, sources, game_running=lambda: False)
    model = org.refresh(record=False)
    host = platform.Host(platform.WINDOWS, root / "home", {"LOCALAPPDATA": str(demo.target / "Local")})
    ctx = context_for(org, host=host, machine_id="m-1", app_version="t", game_probe=lambda: False)
    return demo, sources, model, ctx


def test_a_promoted_member_keeps_its_version_live():
    from nms_save_vault.core.liveops.execute import execute
    from nms_save_vault.core.liveops.planner import plan_promote
    from nms_save_vault.core.organizer import Organizer
    from nms_save_vault.core.organizer.model import LiveRef

    root = Path(tempfile.mkdtemp(prefix="nd"))
    try:
        demo, sources, model, ctx = _world(root)
        steam = next(s for s in sources if s.platform == "steam")
        ref = LiveRef("steam", Path(steam.path).name)
        line = next(x for x in model.lines() if x.name == "Pirate Life")
        assert any(r.live_in for r in model.versions(line.pid))
        plan = plan_promote(ctx, ref.target(6), "B")
        assert not plan.blocked, plan.blocked
        assert execute(ctx, plan, plan.confirmations).status == "done"
        after = Organizer(demo.vault, sources, game_running=lambda: False).refresh(record=False)
        assert any(r.live_in for r in after.versions(line.pid))
        assert "live" in {f for r in after.versions(line.pid) for f in r.flags}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_two_same_day_entries_are_two_places():
    from nms_save_vault.core.organizer.model import EntryRef, RawLocation
    from nms_save_vault.core.organizer.readmodel import LocationSummary, _distinct

    def loc(entry):
        raw = RawLocation(EntryRef(entry), 1, "A", "snapshot", True, 1)
        return LocationSummary("vault", "Vault: safety snapshot 2026-10-18", raw)

    assert len(_distinct([loc("e1"), loc("e2"), loc("e1")])) == 2


def test_the_history_table_fills_parts_and_bases_when_the_index_finishes(host, gui_app, tmp_path):
    w, (a, b, c, d, e, f), _ = _long(tmp_path)
    service = _Service(ready=False)
    win = H.HistoryWindow(host, model=w.refresh(), themed=gui_app.themed, pid=PID, head=f,
                          facts=lambda: service)
    try:
        iid = win._iid_of_head[f]
        assert win.tree.set(iid, "parts") == "" and win.tree.set(iid, "bases") == ""
        service.become_ready([f])
        win.on_index_events([_event("indexed", f)])
        assert win.tree.set(iid, "parts") == "900" and win.tree.set(iid, "bases") == "3"
    finally:
        win.destroy()
