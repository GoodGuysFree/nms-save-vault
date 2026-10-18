"""D6 U21-U22: the content index in the Organizer tab - facts columns and their sort, the progress
strip, event polling, the first-run banner, the version card with its deep section on demand,
"what changed" against the drawn parent, and the facts in the Move dialog's panel. A scripted
index stands in for D4's service (its own tests cover the service); the point here is that the
window only ever reads memory on the Tk thread and asks the worker for anything else."""
from __future__ import annotations

import threading
import time
import tkinter as tk
from types import SimpleNamespace

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core.index import Priority
from nms_save_vault.ui.organizer import factsui
from nms_save_vault.ui.organizer import view as V
from nms_save_vault.ui.organizer.table import COLUMNS, BY_KEY, line_values

from .organizer_world import PIDS, build_world


def _wait(app, condition, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


def make_view(vid, *, status="ok", glyphs=None, base_parts=0, legacy=0, ships=0, **kw):
    fields = dict(
        vid=vid, status=status, story=None, story_ending=None, atlas=None, expedition=None,
        current_galaxy=None, glyphs=glyphs, own_bases=0, base_parts=base_parts,
        largest_base=None, legacy_placed=legacy, galaxies_with_bases=(), ships=ships,
        corvettes=0, companions=0, companion_names=(), freighter=None, journey_rank=None,
        systems_discovered=None)
    fields.update(kw)
    numbers = {k: v for k, v in (("glyphs", glyphs), ("base_parts", base_parts),
                                 ("legacy_placed", legacy), ("ships", ships)) if v is not None}
    return SimpleNamespace(numbers=numbers, **fields)


class FakeIndex:
    """A scripted IndexService: memory reads for the Tk thread, a recorded thread for the rest."""

    Priority = Priority
    active = True

    def __init__(self) -> None:
        self.views: dict = {}
        self.cards: dict = {}
        self.deeps: dict = {}
        self.change_sets: dict = {}
        self.events: list = []
        self.progress_value = SimpleNamespace(
            state="idle", queued=0, running=0, done=0, failed=0, done_bytes=0, total_bytes=0,
            eta_s=None, message="Content index up to date")
        self.calls: list[tuple[str, bool]] = []          # (name, on the Tk thread?)
        self.log: list = []

    def _note(self, name: str) -> None:
        self.calls.append((name, threading.current_thread() is threading.main_thread()))

    def view(self, vid):
        self._note("view")
        return self.views.get(vid)

    def card(self, vid):
        self._note("card")
        deep = self.deeps.get(vid)
        base = self.cards.get(vid)
        return None if base is None else SimpleNamespace(deep=deep)

    def changes(self, child, parent, *, edge_kind=None):
        self._note("changes")
        return self.change_sets.get((child, parent))

    def is_milestone(self, child, parent):
        return False

    def request(self, vids, priority):
        self._note("request")
        self.log.append(("request", list(vids), Priority(priority)))
        return len(vids)

    def request_deep(self, vid):
        self._note("request_deep")
        self.log.append(("deep", vid))
        self.deeps[vid] = {"base_names": ["Home", "Outpost"], "ship_class": "S"}
        self.events.append(SimpleNamespace(seq=len(self.events) + 1, kind="deep-ready", vid=vid,
                                           status=None))

    def queue_refresh(self):
        self._note("queue_refresh")
        self.log.append(("refresh",))
        return 0

    def progress(self):
        self._note("progress")
        return self.progress_value

    def events_since(self, seq, limit=500):
        return [e for e in self.events if e.seq > seq]

    def pause(self):
        self.log.append(("pause",))
        self.progress_value = SimpleNamespace(**{**vars(self.progress_value), "state": "paused",
                                                 "message": "Content index paused"})

    def resume(self):
        self.log.append(("resume",))
        self.progress_value = SimpleNamespace(**{**vars(self.progress_value), "state": "running"})

    def cancel(self):
        self.log.append(("cancel",))

    def close(self):
        self.log.append(("close",))

    def push(self, kind, vid=None) -> None:
        self.events.append(SimpleNamespace(seq=len(self.events) + 1, kind=kind, vid=vid,
                                           status=None))


@pytest.fixture
def world(tmp_path, make_wgs_account, monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    return build_world(tmp_path, make_wgs_account)


@pytest.fixture
def index(world):
    fake = FakeIndex()
    main = world.model.line(PIDS["main"])
    rows = world.model.versions(PIDS["main"])
    other = world.model.line(PIDS["other"])
    fake.views[main.representative] = make_view(main.representative, glyphs=12, base_parts=40,
                                                legacy=3, ships=2)
    fake.views[other.representative] = make_view(other.representative, glyphs=4, ships=1)
    old = world.model.line(PIDS["old"])
    fake.views[old.representative] = make_view(old.representative, status="failed")
    for row in rows:
        fake.cards[row.head] = True
    world.organizer.facts = fake
    return fake


@pytest.fixture
def view(gui_app, world, index):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    prefs = {}
    widget = V.OrganizerView(top, worker=gui_app.worker, themed=gui_app.themed, prefs=prefs,
                             save_prefs=lambda: None, make_organizer=lambda: world.organizer)
    widget.pack(fill=tk.BOTH, expand=True)
    widget.rescan()
    _wait(gui_app, lambda: not widget.scanning)
    yield widget
    widget.index_strip.stop()
    if widget.move_dialog is not None and widget.move_dialog.winfo_exists():
        widget.move_dialog.destroy()
    top.destroy()


def _cell(view, pid: str, column: str) -> str:
    iid = view.table._iid_of[pid]
    return view.table.tree.set(iid, column)


# --- the facts columns --------------------------------------------------------------------


def test_the_facts_columns_show_the_number_dots_while_unread_and_unreadable(view, index):
    assert [c.key for c in COLUMNS if c.key in ("glyphs", "parts", "legacy", "ships")] == [
        "glyphs", "parts", "legacy", "ships"]
    assert BY_KEY["glyphs"].sort == "facts:glyphs"
    assert (_cell(view, PIDS["main"], "glyphs"), _cell(view, PIDS["main"], "parts"),
            _cell(view, PIDS["main"], "legacy"), _cell(view, PIDS["main"], "ships")) == (
        "12", "40", "3", "2")
    assert _cell(view, PIDS["cross"], "glyphs") == factsui.NOT_READ      # not read yet
    assert _cell(view, PIDS["old"], "glyphs") == "unreadable"


def test_an_event_updates_the_rows_in_place(view, index, world):
    cross = world.model.line(PIDS["cross"])
    index.views[cross.representative] = make_view(cross.representative, glyphs=9)
    index.push("indexed", cross.representative)
    view.index_strip.poll()
    assert _cell(view, PIDS["cross"], "glyphs") == "9"


def test_without_an_index_the_cells_stay_empty(view):
    values = line_values(view.model.line(PIDS["main"]), None, False)
    assert values["glyphs"] == "" and values["ships"] == ""


def test_sorting_by_a_facts_column_orders_by_the_number_largest_first(view):
    view.table.sort_by("glyphs")
    order = [view.table._pid_of[i] for i in view.table.tree.get_children("")
             if i in view.table._pid_of]
    assert order[:2] == [PIDS["main"], PIDS["other"]] and view.table.sort_key == "facts:glyphs"
    assert view.table.descending
    assert order[-1] in (PIDS["cross"], PIDS["old"], PIDS["xonly"])       # unread sorts last


def test_a_version_row_shows_its_facts_and_the_auto_note_against_the_drawn_parent(view, index, world):
    pid = PIDS["main"]
    rows = world.model.versions(pid)
    child = next(r for r in rows if r.parent is not None)
    index.views[child.head] = make_view(child.head, glyphs=11)
    changes = SimpleNamespace(header="played 2h", items=(SimpleNamespace(text="+1 base"),
                                                         SimpleNamespace(text="+2 glyphs")),
                              top=lambda n=3: (SimpleNamespace(text="+1 base"),
                                               SimpleNamespace(text="+2 glyphs")),
                              lines=lambda n=None: ["played 2h", "+1 base", "+2 glyphs"])
    index.change_sets[(child.head, child.parent)] = changes
    view.table.set_open(pid, True)
    view.table.refresh()
    iid = next(i for i, (p, h) in view.table._version_of.items() if h == child.head)
    assert view.table.tree.set(iid, "glyphs") == "11"
    assert view.table.tree.set(iid, "note").startswith("auto: +1 base; +2 glyphs")
    root = next(r for r in rows if r.parent is None)
    iid = next(i for i, (p, h) in view.table._version_of.items() if h == root.head)
    assert view.table.tree.set(iid, "note") == ""                  # no recorded parent: nothing


# --- the strip, the banner, the events --------------------------------------------------------


def test_the_strip_shows_progress_pauses_cancels_and_hides_when_idle(view, index, gui_app):
    strip = view.index_strip
    index.progress_value = SimpleNamespace(
        state="running", queued=8, running=2, done=14, failed=0, done_bytes=1000,
        total_bytes=4000, eta_s=20.0, message="Indexing 15 of 24 versions")
    strip.poll()
    assert strip.shown and "Indexing 15 of 24 versions - about 20 s left" == strip.message.cget("text")
    assert float(strip.bar.cget("value")) == 1000 and float(strip.bar.cget("maximum")) == 4000
    strip.pause_button.invoke()
    assert ("pause",) in index.log and strip.pause_button.cget("text") == "Resume"
    strip.pause_button.invoke()
    assert ("resume",) in index.log
    strip.cancel_button.invoke()
    assert ("cancel",) in index.log
    index.progress_value = SimpleNamespace(**{**vars(index.progress_value), "state": "idle"})
    strip.poll()
    assert not strip.shown


def test_the_first_run_banner_shows_once_and_dismissing_it_is_remembered(view, index, gui_app):
    assert view.banner is not None and "reading what is inside your saves" in " ".join(
        str(w.cget("text")) for w in view.banner.winfo_children() if hasattr(w, "cget")
        and "text" in w.keys())
    view.banner.winfo_children()[-1].invoke()
    assert view.banner is None and view.prefs["index_banner_done"] is True
    view.rescan()
    _wait(gui_app, lambda: not view.scanning)
    assert view.banner is None                                      # not again


def test_the_refresh_is_queued_in_the_worker_after_the_scan(view, index):
    assert ("refresh",) in index.log
    assert ("queue_refresh", False) in index.calls and ("queue_refresh", True) not in index.calls


# --- the version card and what changed ----------------------------------------------------------


def test_selecting_a_line_loads_its_card_in_the_worker_and_asks_for_it_first(view, index, gui_app):
    main = view.model.line(PIDS["main"])
    view.table.select(PIDS["main"])
    gui_app.update()
    _wait(gui_app, lambda: "Glyphs" in view.detail.text_of("facts"))
    text = view.detail.text_of("facts")
    assert "Glyphs	12 of 16" in text
    assert any(c[0] == "request" and c[1] == [main.representative] and c[2] == Priority.INTERACTIVE
               for c in index.log)
    assert ("card", False) in index.calls and ("card", True) not in index.calls
    assert view.detail.facts_head == main.representative


def test_a_version_with_a_drawn_parent_shows_what_changed_and_one_without_does_not(view, index,
                                                                                 gui_app, world):
    rows = world.model.versions(PIDS["main"])
    child = next(r for r in rows if r.parent is not None)
    root = next(r for r in rows if r.parent is None)
    index.views[child.head] = make_view(child.head, glyphs=11)
    index.views[root.head] = make_view(root.head, glyphs=8)
    index.change_sets[(child.head, child.parent)] = SimpleNamespace(
        header="played 2h", items=(SimpleNamespace(text="+1 base"),),
        top=lambda n=3: (SimpleNamespace(text="+1 base"),),
        lines=lambda n=None: ["played 2h", "+1 base"])
    view.table.select(PIDS["main"], child.head)
    gui_app.update()
    _wait(gui_app, lambda: "What changed" in view.detail.text_of("facts"))
    assert "+1 base" in view.detail.text_of("facts")
    view.table.select(PIDS["main"], root.head)
    gui_app.update()
    _wait(gui_app, lambda: "Glyphs	8 of 16" in view.detail.text_of("facts"))
    assert "What changed" not in view.detail.text_of("facts")       # no recorded parent: absent


def test_the_deep_section_loads_on_demand_only(view, index, gui_app):
    view.table.select(PIDS["main"])
    gui_app.update()
    _wait(gui_app, lambda: "Glyphs" in view.detail.text_of("facts"))
    assert "Deeper detail" not in view.detail.text_of("facts")
    assert view.detail.deep_button.instate(["!disabled"])
    view.detail.deep_button.invoke()
    _wait(gui_app, lambda: any(c[0] == "deep" for c in index.log))
    view.index_strip.poll()                                            # the deep-ready event
    _wait(gui_app, lambda: "Deeper detail" in view.detail.text_of("facts"))
    assert "Home, Outpost" in view.detail.text_of("facts")
    assert view.detail.deep_button.instate(["disabled"])
    assert ("request_deep", False) in index.calls


# --- the Move dialog -----------------------------------------------------------------------------


def test_the_move_dialog_panel_shows_the_facts_and_the_changes_or_their_absence(view, index,
                                                                              gui_app, world):
    main = view.model.line(PIDS["main"])
    card = view._facts_card(PIDS["main"], None, [("card text\n", ())])
    text = "".join(t for t, _ in card)
    assert "Glyphs\t12 of 16" in text and "What changed in this version" in text
    assert text.index("What changed") < text.index("card text")
    other = view._facts_card(PIDS["old"], None, [])
    assert "unreadable" in "".join(t for t, _ in other)
    assert "nothing shown - no recorded parent" in "".join(t for t, _ in other)
    index.views.pop(main.representative)
    assert factsui.NOT_READ in "".join(t for t, _ in view._facts_card(PIDS["main"], None, []))


def test_the_index_is_never_read_for_a_card_on_the_tk_thread(view, index, gui_app):
    for pid in (PIDS["main"], PIDS["cross"], PIDS["other"]):
        view.table.select(pid)
        gui_app.update()
    _wait(gui_app, lambda: not gui_app.worker.busy)
    assert not [c for c in index.calls if c[0] in ("card", "request_deep", "queue_refresh") and c[1]]


def test_a_facts_event_re_sorts_in_memory_without_a_rescan(view, index, world, gui_app, monkeypatch):
    view.table.sort_by("glyphs")
    first = lambda: [view.table._pid_of[i] for i in view.table.tree.get_children("")  # noqa: E731
                     if i in view.table._pid_of]
    assert first()[:2] == [PIDS["main"], PIDS["other"]]
    scans = []
    monkeypatch.setattr(view, "rescan", lambda *a, **k: scans.append(1))
    other = world.model.line(PIDS["other"])
    index.views[other.representative] = make_view(other.representative, glyphs=99)
    index.push("indexed", other.representative)
    view.index_strip.poll()
    assert first()[:2] == [PIDS["other"], PIDS["main"]] and scans == []


def test_a_new_index_service_has_its_events_handed_on_from_seq_one(gui_app):
    root = tk.Toplevel(gui_app)
    root.withdraw()
    got = []
    current = {}

    class Service:
        active = True

        def __init__(self, n):
            self.n = n

        def events_since(self, seq, limit=500):
            return [SimpleNamespace(seq=i, kind="changes", vid="h") for i in range(1, self.n + 1)
                    if i > seq]

        def progress(self):
            return SimpleNamespace(state="idle", queued=0, running=0, done=0, failed=0,
                                   done_bytes=0, total_bytes=0, eta_s=None, message="")

    strip = factsui.IndexStrip(root, provider=lambda: current["svc"], on_events=got.append)
    try:
        current["svc"] = Service(40)
        strip.poll()
        strip.stop()
        got.clear()
        current["svc"] = Service(1)               # the organizer was reopened: a new service
        strip.poll()
        strip.stop()
        assert [e.seq for batch in got for e in batch] == [1]
    finally:
        strip.stop()
        root.destroy()


def test_reopening_the_organizer_closes_the_old_index_service(view, index, world):
    import copy

    closed = []
    index.close = lambda: closed.append(True)
    fresh = copy.copy(world.organizer)
    fresh.facts = FakeIndex()
    view.make_organizer = lambda: fresh
    view.sources_changed()
    view.rescan()
    _wait(view.winfo_toplevel(), lambda: not view.scanning)
    assert closed == [True] and view.organizer is fresh


def test_an_index_that_will_not_close_does_not_fail_the_scan(view, index, world):
    import copy

    def broken():
        raise RuntimeError("stuck")

    index.close = broken
    fresh = copy.copy(world.organizer)
    fresh.facts = FakeIndex()
    view.make_organizer = lambda: fresh
    view.sources_changed()
    view.rescan()
    _wait(view.winfo_toplevel(), lambda: not view.scanning)
    assert view.organizer is fresh and "scan-failed" not in view.alerts
