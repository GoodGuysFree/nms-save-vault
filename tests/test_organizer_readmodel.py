"""organizer.readmodel: lines, version rows, the live board and events, as the organizer
view and the CLI show them (D1 2.8, 2.10)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from nms_save_vault.core import operations, slotmap
from nms_save_vault.core import store as vstore
from nms_save_vault.core.store import classic as store_classic
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer import scanner
from nms_save_vault.core.organizer.cache import ScanCache
from nms_save_vault.core.organizer.journal import Journal
from nms_save_vault.core.organizer.lineage import read_lineage
from nms_save_vault.core.organizer.model import LiveTarget
from nms_save_vault.core.organizer.notes import Notes, read_notes
from nms_save_vault.core.organizer.readmodel import (
    LineFilter,
    ReadModel,
    filter_lines,
    sort_lines,
)
from nms_save_vault.core.organizer.store import LINEAGE, OrganizerStore
from nms_save_vault.core.state import Source

from .conftest import steam_save


@pytest.fixture(autouse=True)
def game_not_running(monkeypatch):
    from nms_save_vault.core import safety
    monkeypatch.setattr(safety, "is_game_running", lambda: False)

ACCOUNT = "st_76561198000000001"
MAIN = 0xDDDAD1BDD39F9C72
OTHER = 0x9E8385F0AB12CD34
PID, OTHER_PID = "DDDAD1BDD39F9C72", "9E8385F0AB12CD34"
T0 = 1_790_000_000
NOW = T0 + 10_000


class World:
    """A vault, live folders and the organizer store, observed like a refresh does."""

    def __init__(self, tmp_path) -> None:
        self.tmp = tmp_path
        self.vault = Vault(tmp_path / "vault")
        self.vault.ensure()
        self.steam = tmp_path / "NMS" / ACCOUNT
        self.steam.mkdir(parents=True)
        self.sources = [Source(id="steam", platform="steam", account=ACCOUNT,
                               path=str(self.steam), label="Main account")]
        self.store = OrganizerStore(self.vault.root, create=True)

    def add_xbox(self, make_wgs_account, saves) -> Path:
        wgs = self.tmp / "xbox" / "wgs"
        if wgs.exists():
            shutil.rmtree(wgs)
        acct = make_wgs_account(self.tmp / "xbox", saves)
        self.sources = [s for s in self.sources if s.platform != "xbox"] + [
            Source(id="xbox", platform="xbox", account="acct", path=str(acct))]
        return acct

    def refresh(self) -> ReadModel:
        result = scanner.scan(Vault(self.vault.root), self.sources, ScanCache(None, None),
                              game_running=False)
        journal = Journal(self.store, result.versions)
        journal.observe(result, src="scan", skip=frozenset())
        self.result = result
        return ReadModel.build(result, journal.replay(), read_lineage(self.store),
                               read_notes(self.store), self.sources, now=NOW)


def _vid(folder: Path, slot: int, member: int = 0) -> str:
    import hashlib
    path = folder / slotmap.data_filename(slotmap.slot_file_numbers(slot)[member])
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- lines and version rows ---------------------------------------------------


def test_a_save_file_line_with_its_versions_and_live_badges(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 5000, MAIN, T0, extra="v1")
    steam_save(w.steam, 3, 0, "Main Save", 4000, MAIN, T0 - 50, extra="twin")
    steam_save(w.steam, 5, 0, "Other Run", 100, OTHER, T0 - 900, extra="o1")
    operations.create_full_backup(w.vault, w.steam, warnings=[])   # journals the first look
    first = _vid(w.steam, 9)
    steam_save(w.steam, 9, 0, "Main Save", 7200, MAIN, T0 + 100, extra="v2")   # played on
    model = w.refresh()

    main = model.line(PID)
    assert (main.name, main.identified, main.play_time) == ("Main Save", True, 7200)
    assert [b.label for b in main.live] == ["S3", "S9"]
    assert [b.behind_by for b in main.live] == [3200, 0]
    assert main.representative == _vid(w.steam, 9)
    assert {"live", "twin"} <= main.flags and "at-risk" in main.flags   # v2: no vault copy yet
    rows = model.versions(PID)
    assert [r.play_time for r in rows] == [7200, 5000, 4000]              # newest first
    newest, older = rows[0], rows[1]
    assert newest.parent == first and newest.parent_method == "journal"
    assert newest.in_tree and older.in_tree and {"tip", "live"} <= newest.flags
    assert "root" in older.flags and older.children == 1
    assert [s.label for s in older.locations] == ["Vault: backup " + _date(older.locations[0])]
    assert older.durable_copies == 1 and newest.durable_copies == 0
    assert rows[2].parent is None and not rows[2].in_tree                # listed by date
    other = model.line(OTHER_PID)
    assert "at-risk" not in other.flags and other.version_count == 1


def test_a_gog_folder_beside_steam_has_its_own_badges_and_name(tmp_path):
    w = World(tmp_path)
    gog = tmp_path / "NMS" / "DefaultUser"
    w.sources.append(Source(id="gog", platform="steam", account="", path=str(gog)))
    steam_save(w.steam, 9, 0, "Main Save", 5000, MAIN, T0)
    steam_save(gog, 4, 0, "Other Run", 100, OTHER, T0 - 900)
    model = w.refresh()

    assert [b.label for b in model.line(PID).live] == ["S9"]
    assert [b.label for b in model.line(OTHER_PID).live] == ["G4"]
    board = model.live_board()
    assert [s.label for s in board.sources] == ["Main account", "GOG"]
    assert board.sources[1].slots[3].label == "G4"
    assert [s.label for s in model.versions(OTHER_PID)[0].locations] == ["Live: GOG slot 4"]


def test_one_save_live_in_steam_and_gog_is_live_elsewhere_not_a_twin(tmp_path):
    w = World(tmp_path)
    gog = tmp_path / "NMS" / "DefaultUser"
    w.sources.append(Source(id="gog", platform="steam", account="", path=str(gog)))
    steam_save(w.steam, 9, 0, "Main Save", 5000, MAIN, T0, extra="a")
    steam_save(gog, 4, 0, "Main Save", 4000, MAIN, T0 - 900, extra="b")
    model = w.refresh()

    assert "twin" not in model.line(PID).flags
    board = model.live_board()
    s9, g4 = board.sources[0].slots[8], board.sources[1].slots[3]
    assert (g4.twin, g4.other_platform) == (False, (s9.target,))
    assert (s9.twin, s9.other_platform) == (False, (g4.target,))


def _date(summary) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(summary.location.seen_at, timezone.utc).strftime("%Y-%m-%d")


def test_a_version_whose_bytes_are_gone_stays_listed(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0, extra="v1")
    w.refresh()
    gone = _vid(w.steam, 9)
    steam_save(w.steam, 9, 0, "Main Save", 200, MAIN, T0 + 10, extra="v2")   # overwritten
    model = w.refresh()
    line = model.line(PID)
    assert (line.version_count, line.gone_count) == (1, 1)
    row = next(r for r in model.versions(PID) if r.head == gone)
    assert "gone" in row.flags and row.locations == () and row.play_time == 100


def test_notes_pins_and_locks_show_on_the_line(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0, extra="v1")
    model = w.refresh()
    head = model.versions(PID)[0]
    notes = Notes(w.store)
    notes.set_save_note(PID, "\n  Atlas path next  \nthen the wood parts", base_rev=None)
    notes.set_pinned(PID, True)
    notes.set_version_note(head.head, pair=head.pair, pid=PID, label="before Atlas", locked=True)
    model = w.refresh()
    line = model.line(PID)
    assert line.note_preview == "Atlas path next" and line.has_note and line.pinned
    assert line.locked_count == 1 and {"pinned", "has-note"} <= line.flags
    row = model.versions(PID)[0]
    assert row.label == "before Atlas" and row.locked and {"locked", "noted"} <= row.flags


def test_combined_save_files_are_one_line(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0, extra="v1")
    steam_save(w.steam, 4, 0, "Main (copy)", 50, OTHER, T0 - 5, extra="o1")
    with w.store.transaction() as tx:
        tx.doc(LINEAGE)["aliases"] = [{"pid": OTHER_PID, "into": PID, "at": 1}]
    model = w.refresh()
    assert [line.pid for line in model.lines()] == [PID]
    line = model.line(PID)
    assert line.raw_pids == (OTHER_PID, PID) and line.former_names == ("Main (copy)",)


# --- Steam and Xbox copies (D1 2.8) -------------------------------------------


def test_the_lagging_copy_is_behind_and_equal_play_is_in_step(tmp_path, make_wgs_account):
    w = World(tmp_path)
    steam_save(w.steam, 2, 0, "Main Save", 9000, MAIN, T0 + 50, extra="s1")
    w.add_xbox(make_wgs_account, [("Slot4Auto", '{"x":"x1"}', "Main Save", "", 8000, T0, MAIN)])
    line = w.refresh().line(PID)
    assert line.cross_platform == "xbox-behind" and "cross-platform" in line.flags
    steam_tip, xbox_tip = line.platform_tips
    assert (steam_tip.behind_by, xbox_tip.behind_by) == (0, 1000)
    assert [b.label for b in line.live] == ["S2", "X4"]
    w.add_xbox(make_wgs_account, [("Slot4Auto", '{"x":"x2"}', "Main Save", "", 9000, T0, MAIN)])
    assert w.refresh().line(PID).cross_platform == "same"


def test_both_copies_played_since_they_were_in_step_are_two_branches(tmp_path, make_wgs_account):
    w = World(tmp_path)
    steam_save(w.steam, 2, 0, "Main Save", 9000, MAIN, T0, extra="s1")
    w.add_xbox(make_wgs_account, [("Slot4Auto", '{"x":"x1"}', "Main Save", "", 9000, T0, MAIN)])
    assert w.refresh().line(PID).cross_platform == "same"            # the sync point
    steam_save(w.steam, 2, 0, "Main Save", 9500, MAIN, T0 + 100, extra="s2")
    w.add_xbox(make_wgs_account, [("Slot4Auto", '{"x":"x2"}', "Main Save", "", 9200, T0 + 90, MAIN)])
    line = w.refresh().line(PID)
    assert line.cross_platform == "diverged" and "diverged" in line.flags
    assert [t.behind_by for t in line.platform_tips] == [0, 0]


def test_without_a_sync_point_the_copies_never_diverge(tmp_path, make_wgs_account):
    w = World(tmp_path)
    steam_save(w.steam, 2, 0, "Main Save", 9000, MAIN, T0, extra="s1")
    w.add_xbox(make_wgs_account, [("Slot4Auto", '{"x":"x1"}', "Main Save", "", 8000, T0, MAIN)])
    w.refresh()
    steam_save(w.steam, 2, 0, "Main Save", 9500, MAIN, T0 + 100, extra="s2")
    w.add_xbox(make_wgs_account, [("Slot4Auto", '{"x":"x2"}', "Main Save", "", 8500, T0 + 90, MAIN)])
    assert w.refresh().line(PID).cross_platform == "xbox-behind"


# --- the live board -----------------------------------------------------------


def test_the_board_suggests_which_slot_to_free(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 1, 0, "Main Save", 100, MAIN, T0, extra="v1")       # backed up, newer
    steam_save(w.steam, 2, 0, "Other Run", 50, OTHER, T0 - 500, extra="o1")  # backed up, older
    operations.create_full_backup(w.vault, w.steam, warnings=[])
    steam_save(w.steam, 3, 0, "Fresh", 10, 0xABC, T0 - 1000, extra="f1")   # no copy: oldest,
                                                                            # yet after the safe
    steam_save(w.steam, 4, 0, "Pinned", 10, 0xDEF, T0 - 900, extra="p1")
    steam_save(w.steam, 6, 0, "Half", 10, 0x123, T0, extra="h1")
    steam_save(w.steam, 6, 1, "Half", 9, 0x123, T0 - 1, extra="h2")
    (w.steam / slotmap.meta_filename(slotmap.slot_file_numbers(6)[1])).unlink()   # half a pair
    Notes(w.store).set_pinned("0000000000000DEF", True)
    board = w.refresh().live_board()
    (source,) = board.sources
    assert source.label == "Main account" and source.steam_cloud is False and len(source.slots) == 15
    slots = {s.target.slot: s for s in source.slots}
    assert (slots[1].state, slots[1].vault_copy, slots[1].label) == ("partial", "durable", "S1")
    assert slots[3].vault_copy == "none" and slots[4].pinned and slots[5].state == "empty"
    assert slots[6].state == "unreadable"                     # never treated as a free slot
    ranked = sorted((s for s in source.slots if s.suggest_rank is not None),
                    key=lambda s: s.suggest_rank)
    order = [s.target.slot for s in ranked]
    assert order[:11] == [5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 2]   # empty, then the older safe
    assert order[11:] == [1, 3] and slots[4].suggest_rank is None and slots[6].suggest_rank is None
    assert slots[1].cloud == "cloud off" and slots[2].name == "Other Run"


def test_an_operation_waiting_for_the_game_and_a_recent_event(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 9, 0, "Main Save", 100, MAIN, T0, extra="v1")
    steam_save(w.steam, 3, 0, "Main Save", 50, MAIN, T0 - 10, extra="v0")
    vstore.create_store(w.vault.root)               # live operations need the version store
    backup = operations.create_full_backup(w.vault, w.steam, warnings=[])
    with store_classic.entry_folder(w.vault, backup) as folder:
        operations.repopulate_slot(w.vault, folder, 3, w.steam, 9)
    model = w.refresh()
    slot9 = model.live_board().sources[0].slots[8]
    assert slot9.pending_op is not None and slot9.pending_op.startswith("op-")
    events = model.events(PID)
    assert events[0].kind == "restore_older"
    assert events[0].text == "Put live by the app in Steam slot 9"
    steam_save(w.steam, 7, 0, "Main Save", 60, MAIN, T0 + 1, extra="v7")
    w.refresh()
    for f in (w.steam / slotmap.data_filename(13), w.steam / slotmap.meta_filename(13)):
        f.unlink()
    model = w.refresh()
    slot7 = model.live_board().sources[0].slots[6]
    assert slot7.state == "empty" and slot7.last_event is not None
    assert slot7.last_event.kind == "deleted"
    assert model.events(PID)[0].text == "Deleted outside the app: Steam slot 7"


# --- sorting, filtering and the JSON form -------------------------------------


def _three_lines(tmp_path):
    w = World(tmp_path)
    steam_save(w.steam, 1, 0, "beta", 300, 0x1, T0 + 2, extra="b")
    steam_save(w.steam, 2, 0, "Alpha", 100, 0x2, T0 + 3, extra="a")
    vault_only = tmp_path / "old"
    steam_save(vault_only, 3, 0, "Gamma", 200, 0x3, T0 + 1, extra="g")
    operations.import_backup(w.vault, vault_only, warnings=[])
    Notes(w.store).set_save_note("0000000000000003", "find the portal", base_rev=None)
    return w.refresh()


def test_sort_and_filter(tmp_path):
    lines = _three_lines(tmp_path).lines()
    assert [line.name for line in sort_lines(lines)] == ["Alpha", "beta", "Gamma"]   # last played
    assert [line.name for line in sort_lines(lines, "name")] == ["Alpha", "beta", "Gamma"]
    assert [line.name for line in sort_lines(lines, "play_time")] == ["beta", "Gamma", "Alpha"]
    assert [line.name for line in sort_lines(lines, "play_time", descending=False)] == [
        "Alpha", "Gamma", "beta"]
    assert [line.name for line in sort_lines(lines, "live")][-1] == "Gamma"
    assert [line.name for line in filter_lines(lines, LineFilter(live=False))] == ["Gamma"]
    assert [line.name for line in filter_lines(lines, LineFilter(text="PORTAL"))] == ["Gamma"]
    assert [line.name for line in filter_lines(lines, LineFilter(has_note=True, live=True))] == []
    with pytest.raises(ValueError):
        sort_lines(lines, "colour")


def test_the_json_form_has_tokens_and_no_account_key(tmp_path):
    model = _three_lines(tmp_path)
    data = model.to_dict({"lines": model.lines(), "board": model.live_board(),
                          "versions": model.versions("0000000000000001")})
    text = json.dumps(data, ensure_ascii=True)
    assert ACCOUNT not in text and "76561198000000001" not in text and str(tmp_path) not in text
    badge = data["lines"][0]["live"][0]["target"] if data["lines"][0]["live"] else \
        data["lines"][1]["live"][0]["target"]
    assert badge.startswith("steam:0:")
    target = model.target_of(badge)
    assert isinstance(target, LiveTarget) and model.token(target) == badge
    assert model.target_of("steam:7:1") is None and model.target_of("xbox:0:1") is None


# --- folding (D1 2.10.4 rule 4) ---------------------------------------------------------------


def test_a_re_save_without_play_folds_into_its_parent(tmp_path):
    world = World(tmp_path)
    steam_save(world.steam, 9, 0, "Main", 100, MAIN, T0, extra="v1")
    world.refresh()
    v1 = _vid(world.steam, 9)
    steam_save(world.steam, 9, 0, "Main", 100, MAIN, T0 + 60, extra="v2")    # re-saved, no play
    world.refresh()
    v2 = _vid(world.steam, 9)
    model = world.refresh()
    assert next(r for r in model.versions(PID) if r.head == v2).folded_into is None   # live
    steam_save(world.steam, 9, 0, "Main", 300, MAIN, T0 + 120, extra="v3")   # played on
    model = world.refresh()
    rows = {r.head: r for r in model.versions(PID)}
    assert rows[v2].parent == v1 and rows[v2].parent_kind == "touch"
    assert rows[v2].folded_into == v1 and rows[v1].folded_into is None
    v3 = _vid(world.steam, 9)
    assert rows[v3].folded_into is None


def test_a_dated_re_save_folds_into_the_older_one_without_a_link(tmp_path):
    world = World(tmp_path)
    for n, extra in enumerate(("a", "b", "c")):
        folder = tmp_path / f"copy-{n}"
        steam_save(folder, 2, 0, "Old", 500 if n < 2 else 900, OTHER, T0 + n * 60, extra=extra)
        operations.import_backup(world.vault, folder, warnings=[])
    model = world.refresh()
    rows = model.versions(OTHER_PID)                     # newest first
    assert [r.in_tree for r in rows] == [False, False, False]
    newest, middle, oldest = rows
    assert newest.folded_into is None                    # more play: its own row
    assert middle.folded_into == oldest.head and middle.parent is None
    assert oldest.folded_into is None
