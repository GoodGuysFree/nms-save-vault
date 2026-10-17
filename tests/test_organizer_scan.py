"""organizer.scanner and organizer.cache: captures, the version registry, pid rules, the
live check, and the per-machine cache that keeps a warm refresh cheap."""
from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nms_save_vault.core import formats, meta, operations, savedir, slotmap
from nms_save_vault.core.catalog import KIND_INPLACE, Vault
from nms_save_vault.core.organizer import cache as cache_mod
from nms_save_vault.core.organizer import scanner
from nms_save_vault.core.organizer.cache import ScanCache
from nms_save_vault.core.organizer.model import EntryRef, LiveRef
from nms_save_vault.core.platform import WINDOWS, Host
from nms_save_vault.core.state import Source

from .conftest import steam_save

ACCOUNT = "st_76561198000000001"
MAIN = 0xDDDAD1BDD39F9C72
T0 = 1_790_000_000


def _vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.ensure()
    return v


def _live(tmp_path, name: str = ACCOUNT) -> Path:
    folder = tmp_path / "NMS" / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _source(folder: Path, platform: str = "steam") -> Source:
    return Source(id=f"{platform}-{folder.name}", platform=platform, account=folder.name,
                  path=str(folder))


def _scan(vault, sources, cache=None, **kw):
    kw.setdefault("game_running", False)
    return scanner.scan(vault, sources, cache or ScanCache(None, None), **kw)


def _data(folder: Path, slot: int, member: int) -> Path:
    return folder / slotmap.data_filename(slotmap.slot_file_numbers(slot)[member])


def _vid(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _kinds(result) -> list[str]:
    return [p.kind for p in result.problems]


# --- live folders -------------------------------------------------------------


def test_a_live_folder_gives_fifteen_captures_with_the_loaded_member(tmp_path):
    live = _live(tmp_path)
    steam_save(live, 1, 0, "Main", 7200, MAIN, T0 + 100, extra="a")   # A saved later: loads
    steam_save(live, 1, 1, "Main", 7100, MAIN, T0, extra="b")
    steam_save(live, 3, 1, "Main", 7000, MAIN, T0 - 50, extra="c")
    result = _scan(_vault(tmp_path), [_source(live)])

    ref = LiveRef("steam", ACCOUNT)
    folder = result.live[ref]
    assert folder.readable and folder.writable and folder.unstable == frozenset()
    assert [c.slot for c in folder.captures] == list(range(1, 16))
    slot1, slot2, slot3 = folder.captures[:3]
    assert slot1.pair == (_vid(_data(live, 1, 0)), _vid(_data(live, 1, 1)))
    assert slot1.head_member == "A" and slot1.head.meta.play_time == 7200
    assert (slot1.where, slot1.src, slot1.time_quality) == (ref, "scan", "exact")
    assert slot2.pair == (None, None) and slot2.head is None        # empty is an observation
    assert slot3.head_member == "B" and slot3.a is None
    a = slot1.a
    assert (a.pid, a.pid_src, a.platform) == ("DDDAD1BDD39F9C72", "meta", "steam")
    assert (a.meta.meta_ts, a.meta.ts_src, a.meta.name, a.meta.valid) == (T0 + 100, "meta", "Main", True)
    fno = slotmap.slot_file_numbers(1)[0]
    plain = meta.decrypt((live / slotmap.meta_filename(fno)).read_bytes(),
                         slotmap.storage_ordinal(fno))
    assert a.meta.meta_sha == hashlib.sha256(plain).hexdigest()
    assert len(result.versions) == 3 and len(result.pairs) == 2
    assert result.pairs[slot1.pair].head == slot1.a.vid


def test_the_head_is_savedir_newest_even_where_play_time_says_otherwise(tmp_path):
    live = _live(tmp_path)
    steam_save(live, 2, 0, "Main", 9000, MAIN, T0, extra="a")          # more play, older
    steam_save(live, 2, 1, "Main", 100, MAIN, T0 + 1, extra="b")
    capture = _scan(_vault(tmp_path), [_source(live)]).live[LiveRef("steam", ACCOUNT)].captures[1]
    assert capture.head_member == savedir.scan_slot(live, 2).newest.label == "B"


def test_a_save_that_is_not_whole_is_shown_but_marked_unstable(tmp_path):
    live = _live(tmp_path)
    steam_save(live, 4, 0, "Main", 7200, MAIN, T0, extra="whole")
    steam_save(live, 5, 0, "Main", 7200, MAIN, T0, extra="torn")
    data = _data(live, 5, 0)
    data.write_bytes(data.read_bytes()[:-3])        # the game was still writing it
    cache = ScanCache(None, None)
    for _ in range(2):          # the second scan answers from the cache: still unstable
        result = _scan(_vault(tmp_path), [_source(live)], cache)
        folder = result.live[LiveRef("steam", ACCOUNT)]
        assert folder.unstable == frozenset({5})
        assert folder.captures[4].a is not None     # shown
        assert ("member-unstable", 5) in [(p.kind, p.slot) for p in result.problems]


@pytest.mark.parametrize("running, kind", [(True, "game-running"), (None, "game-unknown")])
def test_the_game_state_is_reported(tmp_path, running, kind):
    result = _scan(_vault(tmp_path), [], game_running=running)
    assert kind in _kinds(result) and result.game_running is running


def test_a_missing_live_folder_is_a_problem_not_a_failure(tmp_path):
    gone = tmp_path / "NMS" / ACCOUNT
    result = _scan(_vault(tmp_path), [_source(gone)])
    folder = result.live[LiveRef("steam", ACCOUNT)]
    assert not folder.readable and folder.captures == ()
    assert "folder-missing" in _kinds(result)


def test_two_live_folders_with_one_name_get_instances(tmp_path):
    one = tmp_path / "prefix-a" / ACCOUNT
    two = tmp_path / "prefix-b" / ACCOUNT
    for folder in (one, two):
        steam_save(folder, 1, 0, "Main", 10, MAIN, T0)
    other = _live(tmp_path, "st_76561198000000002")
    refs = list(_scan(_vault(tmp_path), [_source(one), _source(two), _source(other)]).live)
    assert refs[0].instance != refs[1].instance and len(refs[0].instance) == 8
    assert refs[2] == LiveRef("steam", "st_76561198000000002", "")


def test_an_xbox_live_folder(tmp_path, make_wgs_account):
    acct = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"x":"auto"}', "Xbox Save", "", 3600, T0 + 10),
        ("Slot1Manual", '{"x":"manual"}', "Xbox Save", "", 3500, T0),
    ])
    result = _scan(_vault(tmp_path), [_source(acct, "xbox")])
    capture = result.live[LiveRef("xbox", acct.name)].captures[0]
    assert capture.head_member == "A" and capture.a.platform == "xbox"
    assert capture.a.pid_src == "meta" and capture.a.xbox_sync == 2
    assert capture.a.meta.meta_sha == hashlib.sha256(
        savedir.scan_any(acct).slots[1].a.meta_path.read_bytes()).hexdigest()


def test_a_new_meta_beside_the_same_data_is_checked_again(tmp_path):
    """The cached verdict belongs to the data and its meta together."""
    live = _live(tmp_path)
    steam_save(live, 4, 0, "Main", 7200, MAIN, T0)
    cache = ScanCache(None, None)
    assert _scan(_vault(tmp_path), [_source(live)], cache).live[
        LiveRef("steam", ACCOUNT)].unstable == frozenset()
    fno = slotmap.slot_file_numbers(4)[0]
    meta_path, ordinal = live / slotmap.meta_filename(fno), slotmap.storage_ordinal(fno)
    plain = bytearray(meta.decrypt(meta_path.read_bytes(), ordinal))
    struct.pack_into("<I", plain, formats.OFF_SIZE_DECOMPRESSED, 999_999)   # no longer ties
    meta_path.write_bytes(meta.encrypt(bytes(plain), ordinal))
    result = _scan(_vault(tmp_path), [_source(live)], cache)
    assert result.live[LiveRef("steam", ACCOUNT)].unstable == frozenset({4})


def test_a_live_save_that_cannot_be_read_is_unstable_not_absent(tmp_path, monkeypatch):
    live = _live(tmp_path)
    steam_save(live, 1, 0, "Main", 10, MAIN, T0, extra="half")
    steam_save(live, 2, 0, "Main", 10, MAIN, T0, extra="locked")
    steam_save(live, 3, 0, "Main", 10, MAIN, T0, extra="fine")
    (live / slotmap.meta_filename(slotmap.slot_file_numbers(1)[0])).unlink()   # data, no meta
    locked = _data(live, 2, 0)
    real_read = Path.read_bytes

    def read_bytes(self):
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    folder = _scan(_vault(tmp_path), [_source(live)]).live[LiveRef("steam", ACCOUNT)]
    assert folder.unstable == frozenset({1, 2})


def test_an_xbox_record_without_its_blob_is_unstable(tmp_path, make_wgs_account):
    acct = make_wgs_account(tmp_path, [("Slot1Auto", '{"x":1}', "Xbox Save", "", 10, T0)])
    for blob in [p for p in acct.rglob("*") if p.is_file() and p.parent != acct
                 and p.name != "container.1"]:
        blob.unlink()
    folder = _scan(_vault(tmp_path), [_source(acct, "xbox")]).live[LiveRef("xbox", acct.name)]
    assert 1 in folder.unstable and folder.captures[0].a is None


# --- pid rules ----------------------------------------------------------------


def test_without_a_meta_id_the_id_comes_from_the_data_once(tmp_path, monkeypatch):
    live = _live(tmp_path)
    steam_save(live, 1, 0, "Main", 10, 0, T0, data_ident=MAIN)
    decoded = []
    real = scanner.playtime._id_from_bytes
    monkeypatch.setattr(scanner.playtime, "_id_from_bytes",
                        lambda raw, size: decoded.append(1) or real(raw, size))
    cache = ScanCache(None, None)
    for _ in range(2):
        a = _scan(_vault(tmp_path), [_source(live)], cache).captures[0].a
        assert (a.pid, a.pid_src) == ("DDDAD1BDD39F9C72", "data")
    assert decoded == [1]           # the id of given bytes never changes: looked up once


@pytest.mark.parametrize("failure", ["unsupported", "unreadable"])
def test_a_data_id_that_could_not_be_looked_up_is_not_cached(tmp_path, monkeypatch, caplog,
                                                              failure):
    live = _live(tmp_path)
    steam_save(live, 1, 0, "Main", 10, 0, T0, data_ident=MAIN)

    def broken(raw, size):
        if failure == "unsupported":
            raise scanner.lz4_block.UnsupportedContainer("HGSAVEV2 is not supported")
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(scanner.playtime, "_id_from_bytes", broken)
    cache = ScanCache(None, None)
    with caplog.at_level("WARNING"):
        a = _scan(_vault(tmp_path), [_source(live)], cache).captures[0].a
    assert a.pid_src == "legacy" and cache.data_id(a.vid) is None
    assert "could not" in caplog.text and a.vid[:8] in caplog.text
    monkeypatch.undo()
    again = _scan(_vault(tmp_path), [_source(live)], cache).captures[0].a
    assert (again.pid, again.pid_src) == ("DDDAD1BDD39F9C72", "data")   # looked again


def test_a_live_only_legacy_save_keeps_its_key_when_it_moves(tmp_path):
    live = _live(tmp_path)
    steam_save(live, 3, 0, "Voyagers", 10, 0, T0, data_ident=0)
    path = tmp_path / "scan-cache.json"
    first = _scan(_vault(tmp_path), [_source(live)], ScanCache.load(path, None)).captures[2].a
    assert first.pid == "legacy:steam:3:Voyagers"
    for f in (_data(live, 3, 0), live / slotmap.meta_filename(slotmap.slot_file_numbers(3)[0])):
        f.unlink()
    steam_save(live, 7, 0, "Voyagers", 10, 0, T0, data_ident=0)       # the same bytes, slot 7
    moved = _scan(_vault(tmp_path), [_source(live)], ScanCache.load(path, None)).captures[6].a
    assert moved.vid == first.vid and moved.pid == "legacy:steam:3:Voyagers"


def test_a_save_with_no_id_anywhere_keeps_its_first_slot_key(tmp_path):
    """A pre-Waypoint save backed up from slot 3, now live in slot 7: one line."""
    vault = _vault(tmp_path)
    old = tmp_path / "old-backup"
    steam_save(old, 3, 0, "Voyagers", 10, 0, T0, data_ident=0)
    entry = operations.import_backup(vault, old, warnings=[])
    entry.created = "2026-01-01T10:00:00"
    vault.upsert(entry)
    live = _live(tmp_path)
    steam_save(live, 7, 0, "Voyagers", 10, 0, T0, data_ident=0)
    result = _scan(vault, [_source(live)])
    live_obs = result.live[LiveRef("steam", ACCOUNT)].captures[6].a
    assert (live_obs.pid, live_obs.pid_src) == ("legacy:steam:3:Voyagers", "legacy")
    assert result.versions[live_obs.vid].raw_pid == "legacy:steam:3:Voyagers"


def test_an_unreadable_meta_without_a_data_id_is_unknown(tmp_path):
    live = _live(tmp_path)
    steam_save(live, 2, 0, "Main", 10, MAIN, T0, data_ident=0)
    meta_path = live / slotmap.meta_filename(slotmap.slot_file_numbers(2)[0])
    meta_path.write_bytes(b"\x00" * len(meta_path.read_bytes()))
    result = _scan(_vault(tmp_path), [_source(live)])
    a = result.captures[1].a
    assert a.pid == "unknown:" + a.vid[:16] and a.pid_src == "unknown"
    assert "member-unreadable" in _kinds(result)
    assert 2 in result.live[LiveRef("steam", ACCOUNT)].unstable


# --- vault entries ------------------------------------------------------------


def test_entries_give_captures_by_kind(tmp_path):
    vault = _vault(tmp_path)
    live = _live(tmp_path)
    steam_save(live, 1, 0, "Main", 7200, MAIN, T0 + 100, extra="a")
    steam_save(live, 2, 0, "Other", 50, 0xBEEF, T0 + 5, extra="o")
    full = operations.create_full_backup(vault, live, warnings=[])
    extract = operations.extract_slot(vault, live, 2, warnings=[])
    hand_made = tmp_path / "my-copy"
    steam_save(hand_made, 1, 0, "Main", 7000, MAIN, T0 - 500, extra="h")
    steam_save(hand_made, 4, 0, "Main", 6000, MAIN, T0 - 900, extra="i")
    inplace = operations.import_backup(vault, hand_made, warnings=[])
    assert inplace.kind == KIND_INPLACE

    result = _scan(vault, [_source(live)])
    entries = result.entries
    created = int(datetime.fromisoformat(full.created).astimezone(timezone.utc).timestamp())
    full_scan = entries[full.id]
    assert len(full_scan.captures) == 15 and full_scan.created == created
    assert {(c.where, c.observed_at, c.time_quality, c.src) for c in full_scan.captures} == {
        (EntryRef(full.id), created, "exact", "entry")}
    assert [c.slot for c in entries[extract.id].captures] == [2]
    assert entries[extract.id].captures[0].time_quality == "estimated"
    hand = entries[inplace.id].captures
    assert len(hand) == 15 and {c.observed_at for c in hand} == {T0 - 500}   # newest save inside
    assert hand[0].time_quality == "estimated"

    main = result.versions[_vid(_data(live, 1, 0))]          # live and in the full backup
    assert sorted((l.entry_kind, l.managed) for l in main.locations) == [
        ("full", True), ("live", False)]
    assert main.first_seen == (created, -1)                   # the backup came first
    assert main.roles == frozenset("A")


def test_an_entry_that_is_the_live_folder_is_skipped(tmp_path):
    vault = _vault(tmp_path)
    live = _live(tmp_path)
    steam_save(live, 1, 0, "Main", 10, MAIN, T0)
    alias = operations.import_backup(vault, live, warnings=[])      # P9: catalogued in place
    result = _scan(vault, [_source(live)])
    assert alias.id not in result.entries
    assert ("entry-is-live", EntryRef(alias.id)) in [(p.kind, p.where) for p in result.problems]
    assert len(result.versions[_vid(_data(live, 1, 0))].locations) == 1   # counted once


def test_a_missing_entry_folder_is_reported(tmp_path):
    vault = _vault(tmp_path)
    copy = tmp_path / "copy"
    steam_save(copy, 1, 0, "Main", 10, MAIN, T0)
    entry = operations.import_backup(vault, copy, warnings=[])
    for f in copy.iterdir():
        f.unlink()
    copy.rmdir()
    result = _scan(vault, [])
    assert entry.id not in result.entries and "folder-missing" in _kinds(result)


# --- the cache ----------------------------------------------------------------


def test_a_warm_scan_hashes_nothing_and_rescans_no_entry(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    live = _live(tmp_path)
    steam_save(live, 1, 0, "Main", 7200, MAIN, T0, extra="a")
    backup = operations.create_full_backup(vault, live, warnings=[])
    cache_file = tmp_path / "cache" / "scan-cache.json"
    first = _scan(vault, [_source(live)], ScanCache.load(cache_file, "v1"))

    hashed, scanned = [], []
    real_hash, real_scan = scanner.safety.sha256_bytes, scanner.savedir.scan_any
    monkeypatch.setattr(scanner.safety, "sha256_bytes", lambda b: hashed.append(1) or real_hash(b))
    monkeypatch.setattr(scanner.savedir, "scan_any", lambda p: scanned.append(Path(p)) or real_scan(p))
    warm = _scan(vault, [_source(live)], ScanCache.load(cache_file, "v1"))   # a new process
    assert hashed == [] and scanned == [live]
    assert warm.captures == first.captures and warm.versions == first.versions

    steam_save(live, 1, 0, "Main", 7300, MAIN, T0 + 60, extra="played")   # the game saved
    newer = _scan(vault, [_source(live)], ScanCache.load(cache_file, "v1"))
    assert len(hashed) == 1 and newer.captures[-15].a.meta.play_time == 7300
    assert backup.id in newer.entries

    hashed.clear()
    _scan(vault, [_source(live)], ScanCache.load(cache_file, "v1"), deep=True)
    assert len(hashed) == 2         # --rehash: the backup copy and the live save


def test_a_thumbnail_does_not_make_an_entry_look_changed(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    live = _live(tmp_path)
    steam_save(live, 1, 0, "Main", 7200, MAIN, T0)
    (live / "cache").mkdir()
    (live / "cache" / "thumb.dds").write_bytes(b"thumb")
    backup = operations.create_full_backup(vault, live, warnings=[])
    cache = ScanCache(None, None)
    _scan(vault, [], cache)
    folder = Path(backup.path)
    (folder / "cache" / "thumb.dds").write_bytes(b"another thumb")
    scanned = []
    real_scan = scanner.savedir.scan_any
    monkeypatch.setattr(scanner.savedir, "scan_any", lambda p: scanned.append(1) or real_scan(p))
    _scan(vault, [], cache)
    assert scanned == []
    data = folder / slotmap.data_filename(1)
    os.utime(data, ns=(data.stat().st_atime_ns, data.stat().st_mtime_ns + 10**9))
    _scan(vault, [], cache)
    assert scanned == [1]


def test_the_cache_file(tmp_path):
    path = tmp_path / "c" / "scan-cache.json"
    cache = ScanCache.load(path, "v1")
    cache.put_file(Path("/x/save.hg"), 10, 20, "a" * 64, True, "m" * 64)
    cache.put_data_id("a" * 64, 0xDDDAD1BDD39F9C72)
    cache.put_data_id("b" * 64, 0)
    cache.put_legacy_pid("a" * 64, "legacy:steam:3:Voyagers")
    cache.save()
    raw = path.read_bytes()
    assert raw.isascii() and json.loads(raw)["vault_id"] == "v1"
    again = ScanCache.load(path, "v1")
    assert again.file(Path("/x/save.hg"), 10, 20) == ("a" * 64, True, "m" * 64)
    assert again.legacy_pid("a" * 64) == "legacy:steam:3:Voyagers"
    assert again.file(Path("/x/save.hg"), 10, 21) is None             # touched: hash again
    assert again.data_id("a" * 64) == 0xDDDAD1BDD39F9C72 and again.data_id("b" * 64) == 0
    assert again.data_id("c" * 64) is None
    assert ScanCache.load(path, "another vault").file(Path("/x/save.hg"), 10, 20) is None
    path.write_bytes(b"{not json")
    assert ScanCache.load(path, "v1").file(Path("/x/save.hg"), 10, 20) is None   # rebuilt

    memory = ScanCache(None, "v1")
    memory.put_file(Path("/y"), 1, 2, "a" * 64, None, "")
    memory.save()                   # --no-cache: never written anywhere
    assert memory.file(Path("/y"), 1, 2) == ("a" * 64, None, "")


@pytest.mark.parametrize("bad", ["zz", "0x12", "-5", "00012", ""])
def test_a_damaged_data_id_starts_the_cache_afresh(tmp_path, bad):
    path = tmp_path / "scan-cache.json"
    cache = ScanCache.load(path, "v1")
    cache.put_file(Path("/x/save.hg"), 10, 20, "a" * 64, True, "")
    cache.put_data_id("a" * 64, 0x1234)
    cache.save()
    data = json.loads(path.read_text(encoding="ascii"))
    data["ids"]["a" * 64] = bad
    path.write_text(json.dumps(data), encoding="ascii")
    again = ScanCache.load(path, "v1")
    assert again.data_id("a" * 64) is None                       # never int("zz", 16)
    assert again.file(Path("/x/save.hg"), 10, 20) is None


def test_cache_problems_are_logged_without_paths(tmp_path, caplog):
    unreadable = tmp_path / "secret-folder-name" / "scan-cache.json"
    unreadable.mkdir(parents=True)                              # a folder where the file goes
    with caplog.at_level("WARNING"):
        ScanCache.load(unreadable, "v1")
        blocked = tmp_path / "secret-file" / "scan-cache.json"
        blocked.parent.write_text("x", encoding="ascii")        # its folder is a file
        failing = ScanCache(blocked, "v1")
        failing.put_data_id("a" * 64, 1)
        failing.save()
    assert "not read" in caplog.text and "not saved" in caplog.text
    assert "secret" not in caplog.text and str(tmp_path) not in caplog.text


def test_paths_that_are_gone_are_dropped_after_a_full_scan(tmp_path):
    vault = _vault(tmp_path)
    copy = tmp_path / "copy"
    steam_save(copy, 1, 0, "Main", 10, MAIN, T0)
    entry = operations.import_backup(vault, copy, warnings=[])
    path = tmp_path / "scan-cache.json"
    _scan(vault, [], ScanCache.load(path, None))
    stored = json.loads(path.read_text(encoding="ascii"))
    assert entry.id in stored["entries"] and len(stored["files"]) == 1
    vault.remove(entry.id)
    _scan(vault, [], ScanCache.load(path, None))
    stored = json.loads(path.read_text(encoding="ascii"))
    assert stored["entries"] == {} and stored["files"] == {}


def test_the_cache_lives_per_machine_keyed_by_vault(tmp_path):
    host = Host(WINDOWS, tmp_path, {"LOCALAPPDATA": str(tmp_path / "Local")})
    base = tmp_path / "Local" / "NMSSaveVault" / "cache" / "vaults"
    assert cache_mod.cache_dir(tmp_path / "vault", "ab" * 16, host) == base / ("ab" * 16)
    no_store = cache_mod.cache_dir(tmp_path / "vault", None, host)
    digest = hashlib.sha256(str((tmp_path / "vault").resolve()).encode("utf-8")).hexdigest()
    assert no_store == base / f"nostore-{digest[:16]}"


# --- progress and cancelling --------------------------------------------------


def test_progress_and_cancel(tmp_path):
    vault = _vault(tmp_path)
    live = _live(tmp_path)
    steam_save(live, 1, 0, "Main", 10, MAIN, T0)
    operations.create_full_backup(vault, live, warnings=[])
    seen = []
    _scan(vault, [_source(live)], progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 2), (2, 2)]
    stop = threading.Event()
    stop.set()
    with pytest.raises(scanner.ScanCancelled):
        _scan(vault, [_source(live)], cancel=stop)
