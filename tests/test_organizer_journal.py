"""organizer.journal: observing live slots, and replaying the records into edges, events and
operation outcomes (D1 2.6). Every edge must be evidence; nothing is inferred."""
from __future__ import annotations

import json

import pytest

from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer import journal as jr
from nms_save_vault.core.organizer import scanner
from nms_save_vault.core.organizer.cache import ScanCache
from nms_save_vault.core.organizer.journal import Journal, obs_fields
from nms_save_vault.core.organizer.model import (
    LiveRef,
    LiveTarget,
    MemberObs,
    MetaSnap,
    target_to_json,
)
from nms_save_vault.core.organizer.store import JOURNAL, NOTES, OrganizerStore
from nms_save_vault.core.state import Source

from .conftest import steam_save

ACCOUNT = "st_76561198000000001"
PID = "DDDAD1BDD39F9C72"
OTHER = "9E8385F0AB12CD34"
T9 = LiveTarget("steam", ACCOUNT, 9)
T7 = LiveTarget("steam", ACCOUNT, 7)


def V(n: int) -> str:
    return f"{n:064x}"


def mobs(vid: str, member: str = "A", *, play: int, ts: int, pid: str = PID) -> MemberObs:
    snap = MetaSnap(play, ts, "meta", "Main", "", 2, 1, 0, 4220, 2004, 100, 50, "", True, float(ts))
    src = "legacy" if pid.startswith("legacy:") else "meta"
    return MemberObs(member, vid, pid, src, "steam", snap)


class Book:
    """Writes journal records the way the observer, the recorder and the bootstrap do."""

    def __init__(self, tmp_path) -> None:
        self.store = OrganizerStore(tmp_path, create=True)
        self.described: set[str] = set()

    def obs(self, target: LiveTarget, a: MemberObs | None = None, b: MemberObs | None = None,
            *, at: int, src: str = "scan") -> None:
        members = {m.vid: m for m in (a, b) if m is not None and m.vid not in self.described}
        self.described.update(members)
        with self.store.transaction() as tx:
            tx.append(obs_fields(src, target, (a.vid if a else None, b.vid if b else None),
                                 members, {}), at=at)

    def op(self, target: LiveTarget, placed, evicted=None, *, at: int, op_id: str,
           status: str = "done", src: str = "app", kind: str = "restore_older") -> None:
        with self.store.transaction() as tx:
            tx.append({"t": "op", "src": src, "op_id": op_id, "kind": kind,
                       "target": target_to_json(target), "pair": list(placed),
                       "evicted": list(evicted) if evicted else None, "source": None,
                       "preserved": {}, "status": status, "undo_of": None, "note": "",
                       "app": "test"}, at=at)

    def replay(self, registry=None):
        return Journal(self.store, registry or {}).replay()


def _edges(state):
    return [(e.parent, e.child, e.tier, e.kind, e.method, e.op_id) for e in state.edges]


def _events(state, *kinds):
    return [e for e in state.events if not kinds or e.kind in kinds]


OP1 = "op-20261016T080055Z-3fa2"
OP2 = "op-20261016T090000Z-0b0b"


# --- edges from observations --------------------------------------------------


def test_play_touch_and_edit_steps_seen_in_the_slot(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=100, ts=1000), at=1000)
    book.obs(T9, mobs(V(2), play=160, ts=2000), at=2000)       # played
    book.obs(T9, mobs(V(3), play=160, ts=2100), at=2100)       # re-saved after a load
    book.obs(T9, mobs(V(4), play=160, ts=2100), at=2200)       # changed outside the game
    state = book.replay()
    assert _edges(state) == [
        (V(1), V(2), "observed", "play", "journal", None),
        (V(2), V(3), "observed", "touch", "journal", None),
        (V(3), V(4), "observed", "edit", "journal", None),
    ]
    assert [e.kind for e in state.events] == ["first-seen", "changed", "changed", "changed"]
    assert state.targets[T9].pair == (V(4), None) and state.targets[T9].by_op is None


def test_two_new_members_chain_by_time(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=100, ts=1000), mobs(V(2), "B", play=90, ts=900), at=1000)
    book.obs(T9, mobs(V(4), play=300, ts=3000), mobs(V(3), "B", play=200, ts=2000), at=3000)
    assert _edges(book.replay())[:2] == [
        (V(1), V(3), "observed", "play", "journal", None),
        (V(3), V(4), "observed", "play", "journal", None),    # the later save from the earlier
    ]


def test_backfill_steps_are_seen_in_backups(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=100, ts=1000), at=1000, src="backfill")
    book.obs(T9, mobs(V(2), play=200, ts=2000), at=2000, src="backfill")
    assert _edges(book.replay()) == [(V(1), V(2), "observed", "play", "capture-sequence", None)]


def test_a_different_save_in_the_slot_is_replaced_not_a_child(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=100, ts=1000), at=1000)
    book.obs(T9, mobs(V(2), play=500, ts=2000, pid=OTHER), at=2000)
    state = book.replay()
    assert state.edges == ()
    assert [(e.kind, e.vids) for e in _events(state, "replaced")] == [("replaced", (V(2),))]


def test_less_play_is_never_a_child(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=500, ts=1000), at=1000)
    book.obs(T9, mobs(V(2), play=100, ts=2000), at=2000)
    assert book.replay().edges == ()


def test_a_legacy_save_moved_to_another_slot_stays_compatible(tmp_path):
    book = Book(tmp_path)
    old = mobs(V(1), play=100, ts=1000, pid="legacy:steam:3:Voyagers")
    book.obs(T7, old, at=1000)
    book.obs(T7, old, mobs(V(2), "B", play=200, ts=2000, pid="legacy:steam:7:Voyagers"), at=2000)
    assert _edges(book.replay()) == [(V(1), V(2), "observed", "play", "journal", None)]


def test_twins_make_a_fork(tmp_path):
    """One version live in two slots, played on in both: two children of one parent."""
    book = Book(tmp_path)
    t3 = LiveTarget("steam", ACCOUNT, 3)
    base = mobs(V(1), play=100, ts=1000)
    book.obs(T9, base, at=1000)
    book.obs(t3, base, at=1000)
    book.obs(T9, mobs(V(2), play=200, ts=2000), at=2000)
    book.obs(t3, mobs(V(3), play=150, ts=2100), at=2100)
    assert sorted((e.parent, e.child) for e in book.replay().edges) == [(V(1), V(2)), (V(1), V(3))]


def test_equal_play_and_time_chain_in_one_deterministic_way(tmp_path):
    """An editor's change and the next save in one observation: the newest candidate wins."""
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=100, ts=1000), at=1000)
    book.obs(T9, mobs(V(5), play=100, ts=1000), mobs(V(6), "B", play=100, ts=1000), at=2000)
    first = _edges(book.replay())
    assert first == [(V(1), V(5), "observed", "edit", "journal", None),
                     (V(5), V(6), "observed", "edit", "journal", None)]
    assert _edges(book.replay()) == first


# --- events -------------------------------------------------------------------


def test_deleted_then_resurrected_by_the_cloud(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=100, ts=1000), at=1000)
    book.obs(T9, at=2000)                                   # deleted outside the app
    book.obs(T9, mobs(V(1), play=100, ts=1000), at=3000)    # Steam Cloud put it back
    state = book.replay()
    assert [e.kind for e in state.events] == ["first-seen", "deleted", "resurrected"]
    assert state.edges == ()


def test_an_old_version_coming_back_is_returned_without_an_edge(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=100, ts=1000), at=1000)
    book.obs(T9, mobs(V(2), play=200, ts=2000), at=2000)
    book.obs(T9, mobs(V(1), play=100, ts=1000), at=3000)    # an old copy put back by hand
    book.obs(T9, mobs(V(3), play=300, ts=4000), at=4000)
    state = book.replay()
    assert [(e.kind, e.vids) for e in _events(state, "returned")] == [("returned", (V(1),))]
    assert _edges(state) == [(V(1), V(2), "observed", "play", "journal", None),
                             (V(1), V(3), "observed", "play", "journal", None)]


def test_a_partial_slot(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=100, ts=1000), mobs(V(2), "B", play=90, ts=900), at=1000)
    book.obs(T9, mobs(V(1), play=100, ts=1000), at=2000)
    assert [e.kind for e in book.replay().events] == ["first-seen", "partial"]


# --- operations ---------------------------------------------------------------


def test_the_first_save_after_an_operation_is_recorded_and_confirms_it(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=500, ts=1000), at=1000)
    book.obs(T7, mobs(V(2), play=300, ts=900), at=1000)          # describes the version to place
    book.op(T9, (V(2), None), (V(1), None), at=1100, op_id=OP1)
    assert book.replay().ops[(OP1, T9)].outcome == "pending"
    book.obs(T9, mobs(V(3), play=400, ts=2000), at=2000)          # the game saved on top
    book.obs(T9, mobs(V(4), play=450, ts=3000), at=3000)
    state = book.replay()
    assert _edges(state)[-2:] == [(V(2), V(3), "recorded", "play", "op", OP1),
                                  (V(3), V(4), "observed", "play", "journal", None)]
    assert state.ops[(OP1, T9)].outcome == "confirmed"
    assert [(e.kind, e.op_id) for e in _events(state, "confirmed")] == [("confirmed", OP1)]


def test_a_legacy_oplog_operation_is_recorded_with_its_method(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=500, ts=1000), at=1000, src="backfill")
    book.obs(T7, mobs(V(2), play=300, ts=900), at=1000, src="backfill")
    book.op(T9, (V(2), None), (V(1), None), at=1100, op_id="legacy-17", src="oplog",
            kind="repopulate")
    book.obs(T9, mobs(V(3), play=400, ts=2000), at=2000, src="backfill")
    assert _edges(book.replay())[-1] == (V(2), V(3), "recorded", "play", "oplog", "legacy-17")


def test_a_cloud_revert_after_an_operation(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=500, ts=1000), at=1000)
    book.obs(T7, mobs(V(2), play=300, ts=900), at=1000)
    book.op(T9, (V(2), None), (V(1), None), at=1100, op_id=OP1)
    book.obs(T9, mobs(V(1), play=500, ts=1000), at=2000)          # the evicted save came back
    state = book.replay()
    assert state.ops[(OP1, T9)].outcome == "reverted"
    assert [(e.kind, e.op_id) for e in _events(state, "reverted")] == [("reverted", OP1)]
    assert [e for e in state.edges if e.child == V(1)] == []


def test_something_else_after_an_operation_is_unexpected(tmp_path):
    book = Book(tmp_path)
    book.obs(T7, mobs(V(2), play=300, ts=900), at=1000)
    book.op(T9, (V(2), None), None, at=1100, op_id=OP1, kind="place_into_empty")
    book.obs(T9, mobs(V(8), play=900, ts=2000, pid=OTHER), at=2000)
    state = book.replay()
    assert state.ops[(OP1, T9)].outcome == "unexpected"
    assert {e.kind for e in state.events if e.target == T9} == {"replaced", "unexpected", "changed"}


def test_a_failed_operation_binds_nothing(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=100, ts=1000), at=1000)
    book.obs(T7, mobs(V(2), play=300, ts=900), at=1000)
    book.op(T9, (V(2), None), (V(1), None), at=1100, op_id=OP1, status="failed")
    book.obs(T9, mobs(V(3), play=150, ts=2000), at=2000)
    state = book.replay()
    assert _edges(state) == [(V(1), V(3), "observed", "play", "journal", None)]
    assert (OP1, T9) not in state.ops


def test_a_later_operation_supersedes_and_a_repeat_record_is_ignored(tmp_path):
    book = Book(tmp_path)
    book.obs(T7, mobs(V(2), play=300, ts=900), mobs(V(3), "B", play=200, ts=800), at=1000)
    book.op(T9, (V(2), None), None, at=1100, op_id=OP1)
    book.op(T9, (V(2), None), None, at=1101, op_id=OP1)          # idempotent on (op, target)
    book.op(T9, (V(3), None), (V(2), None), at=1200, op_id=OP2)
    state = book.replay()
    assert state.ops[(OP1, T9)].outcome == "superseded"
    assert state.ops[(OP2, T9)].outcome == "pending"
    assert state.targets[T9].by_op == OP2


def test_a_repeated_operation_record_does_not_bind_the_slot_again(tmp_path):
    book = Book(tmp_path)
    book.obs(T7, mobs(V(2), play=300, ts=900), at=1000)
    book.op(T9, (V(2), None), None, at=1100, op_id=OP1)
    book.obs(T9, mobs(V(3), play=400, ts=2000), at=2000)
    book.op(T9, (V(2), None), None, at=2100, op_id=OP1)          # a retried record_op
    state = book.replay()
    assert state.ops[(OP1, T9)].outcome == "confirmed"
    assert state.targets[T9].pair == (V(3), None) and state.targets[T9].by_op is None


# --- observing scans ----------------------------------------------------------


def _live(tmp_path):
    folder = tmp_path / "NMS" / ACCOUNT
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _scan(tmp_path, folder, *, cache=None, running=False):
    vault = Vault(tmp_path / "vault")
    source = Source(id="s1", platform="steam", account=ACCOUNT, path=str(folder))
    return scanner.scan(vault, [source], cache or ScanCache(None, None), game_running=running)


def test_observe_writes_only_what_changed_and_describes_each_vid_once(tmp_path):
    live = _live(tmp_path)
    steam_save(live, 9, 0, "Main", 100, 0xDDDAD1BDD39F9C72, 1000, extra="a")
    store = OrganizerStore(tmp_path / "vault", create=True)
    scan = _scan(tmp_path, live)
    first = Journal(store, scan.versions).observe(scan, src="scan", skip=frozenset())
    assert first.written == 15 and first.skipped == ()
    assert {e.kind for e in first.events} == {"first-seen"}
    again = Journal(store, scan.versions).observe(_scan(tmp_path, live), src="scan",
                                                  skip=frozenset())
    assert again.written == 0

    steam_save(live, 9, 1, "Main", 200, 0xDDDAD1BDD39F9C72, 2000, extra="b")
    scan = _scan(tmp_path, live)
    third = Journal(store, scan.versions).observe(scan, src="scan", skip=frozenset())
    assert third.written == 1 and [e.kind for e in third.events] == ["changed"]
    records = store.journal_records()
    last = records[-1]
    assert last["src"] == "scan" and last["target"]["slot"] == 9
    assert list(last["m"]) == [scan.captures[8].b.vid]          # only the new vid is described
    assert len(Journal(store, scan.versions).replay().edges) == 1


def test_observe_skips_busy_unstable_and_running(tmp_path):
    live = _live(tmp_path)
    steam_save(live, 9, 0, "Main", 100, 0xDDDAD1BDD39F9C72, 1000, extra="a")
    steam_save(live, 5, 0, "Main", 100, 0xDDDAD1BDD39F9C72, 1000, extra="torn")
    torn = live / "save9.hg"
    torn.write_bytes(torn.read_bytes()[:-2])                     # slot 5 A
    store = OrganizerStore(tmp_path / "vault", create=True)
    t9 = LiveTarget("steam", ACCOUNT, 9)

    scan = _scan(tmp_path, live, running=True)
    result = Journal(store, scan.versions).observe(scan, src="scan", skip=frozenset())
    assert result.written == 0 and {r for _, r in result.skipped} == {jr.SKIP_GAME}

    scan = _scan(tmp_path, live)
    result = Journal(store, scan.versions).observe(scan, src="scan", skip=frozenset({t9}))
    assert (t9, jr.SKIP_BUSY) in result.skipped
    assert (LiveTarget("steam", ACCOUNT, 5), jr.SKIP_UNSTABLE) in result.skipped
    assert result.written == 13
    later = Journal(store, scan.versions).observe(scan, src="scan", skip=frozenset())
    assert later.written == 1                                    # journaled at the next refresh
    assert store.journal_records()[-1]["target"]["slot"] == 9


def test_a_verify_observe_journals_its_own_targets_only(tmp_path):
    """D5's post-write check passes the busy targets minus its own op's."""
    live = _live(tmp_path)
    steam_save(live, 9, 0, "Main", 100, 0xDDDAD1BDD39F9C72, 1000, extra="a")
    steam_save(live, 3, 0, "Main", 100, 0xDDDAD1BDD39F9C72, 1000, extra="b")
    store = OrganizerStore(tmp_path / "vault", create=True)
    others = frozenset(LiveTarget("steam", ACCOUNT, s) for s in range(1, 16) if s != 9)
    scan = _scan(tmp_path, live)
    result = Journal(store, scan.versions).observe(scan, src="verify", skip=others)
    assert result.written == 1
    assert [(r["src"], r["target"]["slot"]) for r in store.journal_records()] == [("verify", 9)]


def test_an_unreadable_folder_is_never_recorded_as_deleted(tmp_path):
    live = _live(tmp_path)
    steam_save(live, 9, 0, "Main", 100, 0xDDDAD1BDD39F9C72, 1000)
    store = OrganizerStore(tmp_path / "vault", create=True)
    scan = _scan(tmp_path, live)
    Journal(store, scan.versions).observe(scan, src="scan", skip=frozenset())
    gone = tmp_path / "NMS" / "renamed"
    live.rename(gone)
    scan = _scan(tmp_path, live)
    result = Journal(store, scan.versions).observe(scan, src="scan", skip=frozenset())
    assert result.written == 0 and len(result.skipped) == 15
    assert {r for _, r in result.skipped} == {jr.SKIP_UNREADABLE}
    assert _events(Journal(store, {}).replay(), "deleted") == []


def test_a_new_version_snapshots_its_save_file_note(tmp_path):
    live = _live(tmp_path)
    steam_save(live, 9, 0, "Main", 100, 0xDDDAD1BDD39F9C72, 1000)
    store = OrganizerStore(tmp_path / "vault", create=True)
    with store.transaction() as tx:
        tx.doc(NOTES)["save_files"] = {PID: {"text": "Atlas path next", "rev": 3}}
    scan = _scan(tmp_path, live)
    journal = Journal(store, scan.versions)
    journal.observe(scan, src="scan", skip=frozenset())
    vid = scan.captures[8].a.vid
    assert journal.replay().note_rev == {vid: 3}
    assert journal.replay().metas[vid].pid == PID


def test_a_read_only_organizer_records_nothing_and_says_so(tmp_path):
    live = _live(tmp_path)
    steam_save(live, 9, 0, "Main", 100, 0xDDDAD1BDD39F9C72, 1000)
    store = OrganizerStore(tmp_path / "vault", create=True, read_only=True)
    scan = _scan(tmp_path, live)
    result = Journal(store, scan.versions).observe(scan, src="scan", skip=frozenset())
    assert result.written == 0 and {r for _, r in result.skipped} == {jr.SKIP_READ_ONLY}
    assert not (tmp_path / "vault" / "organizer").exists()


def test_records_that_do_not_decode_are_skipped_and_reported(tmp_path):
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=100, ts=1000), at=1000)
    with open(tmp_path / "organizer" / JOURNAL, "ab") as f:
        bad = {"t": "obs", "id": "x-9", "seq": 9, "at": 1500, "src": "guess",
               "target": target_to_json(T9), "pair": [V(7), None], "m": {}, "note_revs": {},
               "entry": None, "q": "exact"}
        f.write((json.dumps(bad) + "\n").encode("ascii"))
    book.obs(T9, mobs(V(2), play=200, ts=2000), at=2000)
    state = book.replay()
    assert _edges(state) == [(V(1), V(2), "observed", "play", "journal", None)]
    assert any("x-9" in p for p in state.problems)


def test_metas_come_from_the_scan_when_the_journal_has_none(tmp_path):
    """Versions placed by an operation may be described only by the scan's registry."""
    book = Book(tmp_path)
    book.obs(T9, mobs(V(1), play=500, ts=1000), at=1000)
    book.op(T9, (V(2), None), (V(1), None), at=1100, op_id=OP1)
    book.obs(T9, mobs(V(3), play=400, ts=2000), at=2000)
    blind = book.replay()
    assert blind.edges == ()                                       # V(2) unknown: no evidence
    assert _events(blind, "replaced") == []                        # ... and no accusation either
    meta = mobs(V(2), play=300, ts=900).meta
    registry = {V(2): scanner.VersionInfo(V(2), PID, "meta", "steam", meta, 900,
                                          frozenset("A"), (900, -1), ())}
    assert _edges(book.replay(registry)) == [(V(2), V(3), "recorded", "play", "op", OP1)]
