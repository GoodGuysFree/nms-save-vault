"""A full restore (and so undo) keeps what belongs to the live folder's own account (P7).

* ``steam_autocloud.vdf`` names the Steam account: it is never copied from a backup.
* An Xbox backup of another account would write that account's containers.index into this
  one: it is refused.
* The account data (unlocks, settings) stays unless the caller asks to roll it back.
* A live folder that could only be partly mirrored is reported, never hidden.

All on synthetic Steam folders and wgs accounts in a temp sandbox.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from nms_save_vault import cli
from nms_save_vault.core import formats, msstore, savedir
from nms_save_vault.core import operations as ops
from nms_save_vault.core.catalog import Vault

from .conftest import chunked, steam_save, xbox_meta

ACCOUNT_FILES = (formats.ACCOUNT_DATA_NAME, formats.ACCOUNT_META_NAME)


def _steam_account(parent: Path, name: str, account: bytes, vdf: str | None) -> Path:
    live = parent / name
    for slot in (1, 2):
        steam_save(live, slot, 0, f"{name} run {slot}", 3600 * slot, 0x1000 + slot, 1_700_000_000 + slot)
    (live / formats.ACCOUNT_DATA_NAME).write_bytes(account)
    (live / formats.ACCOUNT_META_NAME).write_bytes(account[::-1])
    if vdf is not None:
        (live / formats.STEAM_AUTOCLOUD).write_text(vdf, encoding="utf-8")
    return live


def _account_files(live: Path) -> dict[str, bytes]:
    return {n: (live / n).read_bytes() for n in ACCOUNT_FILES}


def _slot_files(live: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in live.glob("*.hg") if p.name not in ACCOUNT_FILES}


# --- Steam ---------------------------------------------------------------------------------


def test_a_restore_never_brings_in_another_accounts_vdf(tmp_path):
    a = _steam_account(tmp_path, "st_1", b"account A", vdf='"accountid" "1"')
    b = _steam_account(tmp_path, "st_2", b"account B", vdf='"accountid" "2"')
    c = _steam_account(tmp_path, "st_3", b"account C", vdf=None)
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, a, include_cache=False)

    ops.restore_full(vault, backup, b, allow_game_running=True)
    ops.restore_full(vault, backup, c, allow_game_running=True)

    assert (b / formats.STEAM_AUTOCLOUD).read_text("utf-8") == '"accountid" "2"'
    assert not (c / formats.STEAM_AUTOCLOUD).exists()
    assert _slot_files(b) == _slot_files(a)                    # the saves did come across


def test_a_restore_keeps_the_live_account_data_unless_asked(tmp_path):
    a = _steam_account(tmp_path, "st_1", b"account A", vdf=None)
    b = _steam_account(tmp_path, "st_2", b"account B", vdf=None)
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, a, include_cache=False)
    own = _account_files(b)

    ops.restore_full(vault, backup, b, allow_game_running=True)
    assert _account_files(b) == own

    ops.restore_full(vault, backup, b, allow_game_running=True, restore_accountdata=True)
    assert _account_files(b) == _account_files(a)
    assert vault.read_oplog()[-1]["with_accountdata"] is True

    ops.undo_last(vault, b, allow_game_running=True)           # undoing that restore
    assert _account_files(b) == own                            # puts the account data back too


def test_the_cli_rolls_account_data_back_only_with_the_flag(tmp_path):
    a = _steam_account(tmp_path, "st_1", b"account A", vdf=None)
    b = _steam_account(tmp_path, "st_2", b"account B", vdf=None)
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, a, include_cache=False)
    own = _account_files(b)
    base = ["--live", str(b), "--vault", str(vault.root), "restore", backup.id, "--force"]

    assert cli.main(base) == 0 and _account_files(b) == own
    assert cli.main(base + ["--with-accountdata"]) == 0 and _account_files(b) == _account_files(a)


def test_a_backup_without_account_data_never_deletes_the_live_one(tmp_path):
    b = _steam_account(tmp_path, "st_2", b"account B", vdf=None)
    bare = tmp_path / "bare"
    steam_save(bare, 1, 0, "bare run", 100, 0x77, 1_700_000_000)
    vault = Vault(tmp_path / "vault")
    backup = ops.import_backup(vault, bare, copy_into_vault=True)
    own = _account_files(b)

    ops.restore_full(vault, backup, b, allow_game_running=True, restore_accountdata=True)

    assert _account_files(b) == own and not savedir.scan(b).slots[2].occupied


# --- Xbox ------------------------------------------------------------------------------------


@pytest.fixture
def xbox_pair(make_wgs_account, tmp_path):
    """Two Xbox accounts, X and Y, each with account data and two saves."""
    saves = [
        ("AccountData", '{"acct":1}', "", "", 0, 500),
        ("Slot1Auto", '{"a":1}', "One", "", 100, 1000),
        ("Slot2Auto", '{"b":2}', "Two", "", 200, 2000),
    ]
    x = make_wgs_account(tmp_path / "x", saves, name="0000000000000001_XXXX", account_id="account-x")
    y = make_wgs_account(tmp_path / "y", saves, name="0000000000000002_YYYY", account_id="account-y")
    return x, y


def test_an_xbox_backup_of_another_account_is_refused(xbox_pair, tmp_path):
    x, y = xbox_pair
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, x, include_cache=False)
    index_before = (y / "containers.index").read_bytes()

    with pytest.raises(ops.ForeignAccountError, match="another Xbox account"):
        ops.restore_full(vault, backup, y, allow_game_running=True)

    assert (y / "containers.index").read_bytes() == index_before
    assert [e.id for e in vault.entries] == [backup.id]          # not even a snapshot taken


def _account_record_and_data(acct: Path) -> tuple[bytes, bytes]:
    records = dict(msstore.split_index((acct / "containers.index").read_bytes())[1])
    _info, containers = msstore.parse_containers_index(acct)
    data = next(c for c in containers if c.identifier == "AccountData").data_file.read_bytes()
    return records["AccountData"], data


def test_an_xbox_restore_keeps_the_live_account_data_unless_asked(xbox_pair, tmp_path):
    _x, y = xbox_pair
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, y, include_cache=False)
    msstore.write_save(y, "AccountData", chunked(b'{"acct":2}'), xbox_meta(length=360), 3000.0)
    ops.clear_slot(vault, y, 1, allow_game_running=True)
    own_record, own_data = _account_record_and_data(y)

    ops.restore_full(vault, backup, y, allow_game_running=True)

    assert savedir.scan_any(y).slots[1].occupied                  # the saves are back
    assert _account_record_and_data(y) == (own_record, own_data)  # the account data is not

    ops.restore_full(vault, backup, y, allow_game_running=True, restore_accountdata=True)
    assert _account_record_and_data(y)[1] == chunked(b'{"acct":1}')


def test_an_xbox_restore_that_cannot_replace_a_folder_says_so(xbox_pair, tmp_path, monkeypatch, caplog):
    _x, y = xbox_pair
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, y, include_cache=False)
    msstore.write_save(y, "Slot1Auto", chunked(b'{"a":9}'), xbox_meta("Changed", ts=1500), 1500.0)
    locked = next(c for c in msstore.parse_containers_index(y)[1] if c.identifier == "Slot1Auto").directory
    real_rmtree = ops.shutil.rmtree

    def rmtree_locked(path, *args, **kwargs):
        if Path(path) == locked:
            raise PermissionError(f"in use: {Path(path).name}")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(ops.shutil, "rmtree", rmtree_locked)
    with caplog.at_level(logging.ERROR, logger=ops.__name__):
        with pytest.raises(ops.WrittenButInvalidError, match="only partly restored") as failed:
            ops.restore_full(vault, backup, y, allow_game_running=True)
    monkeypatch.undo()

    assert locked.name in str(failed.value)
    assert vault.read_oplog()[-1]["snapshot_id"] == failed.value.result.snapshot_id
    assert any(locked.name in r.getMessage() for r in caplog.records)


def test_a_damaged_live_index_does_not_block_a_restore_or_undo(xbox_pair, tmp_path):
    """Restore and undo are how a broken folder is repaired: a live containers.index that
    cannot be read is replaced, with a warning, when the backup was taken of this folder."""
    x, y = xbox_pair
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, y, include_cache=False)
    foreign = ops.create_full_backup(vault, x, include_cache=False)
    ops.clear_slot(vault, y, 1, allow_game_running=True)
    index = y / "containers.index"
    index.write_bytes(index.read_bytes()[:-30])            # a write cut off mid-record

    with pytest.raises(ops.OperationError, match="cannot be confirmed to be the same account"):
        ops.restore_full(vault, foreign, y, allow_game_running=True)
    result = ops.restore_full(vault, backup, y, allow_game_running=True)

    assert any("live save index was damaged" in w for w in result.warnings)
    assert savedir.scan_any(y).slots[1].occupied

    ops.clear_slot(vault, y, 2, allow_game_running=True)
    index.write_bytes(index.read_bytes()[:-30])
    result = ops.undo_last(vault, y, allow_game_running=True)   # puts slot 2 back
    assert any("live save index was damaged" in w for w in result.warnings)
    assert savedir.scan_any(y).slots[2].occupied


def test_a_live_folder_without_account_data_gets_the_backups(tmp_path):
    a = _steam_account(tmp_path, "st_1", b"account A", vdf=None)
    b = _steam_account(tmp_path, "st_2", b"account B", vdf=None)
    for name in ACCOUNT_FILES:
        (b / name).unlink()
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, a, include_cache=False)

    result = ops.restore_full(vault, backup, b, allow_game_running=True)

    assert _account_files(b) == _account_files(a)
    assert not any("account data" in w for w in result.warnings)


def test_skipping_different_account_data_is_reported(tmp_path):
    a = _steam_account(tmp_path, "st_1", b"account A", vdf=None)
    b = _steam_account(tmp_path, "st_2", b"account B", vdf=None)
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, a, include_cache=False)

    result = ops.restore_full(vault, backup, b, allow_game_running=True)
    assert any("account data (unlocks, settings) was not restored" in w for w in result.warnings)

    same = ops.create_full_backup(vault, b, include_cache=False)  # identical account data
    result = ops.restore_full(vault, same, b, allow_game_running=True)
    assert not any("account data" in w for w in result.warnings)


def test_an_xbox_account_without_account_data_gets_the_backups(xbox_pair, tmp_path):
    _x, y = xbox_pair
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, y, include_cache=False)
    msstore.delete_save(y, "AccountData", 3000.0)

    ops.restore_full(vault, backup, y, allow_game_running=True)

    assert _account_record_and_data(y)[1] == chunked(b'{"acct":1}')


def test_restored_xbox_records_are_fresh_local_writes(make_wgs_account, tmp_path):
    """Every restored record carries the restore's own time (never an older one than it had)
    and is flagged for upload, so the Xbox app cannot take the cloud copy back; a slot's two
    records keep their order."""
    live = make_wgs_account(tmp_path, [
        ("Slot1Auto", '{"a":1}', "Run", "", 100, 1000, 0, 0),
        ("Slot1Manual", '{"b":2}', "Run", "", 90, 2000, 0, 0),
    ], meta_len=360)
    for ident in ("Slot1Auto", "Slot1Manual"):            # 280-byte metas: the index orders them
        path = next(c for c in msstore.parse_containers_index(live)[1] if c.identifier == ident).meta_file
        path.write_bytes(xbox_meta("Run", length=280))
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, live, include_cache=False)
    info, containers = msstore.parse_containers_index(Path(backup.path))
    for c in containers:
        c.sync_state = msstore.BLOB_SYNC_SYNCED             # as uploaded when it was taken
    (Path(backup.path) / "containers.index").write_bytes(msstore.build_containers_index(info, containers))
    future = msstore._unix_to_filetime(time.time() + 50_000)
    msstore.write_save(live, "Slot1Auto", chunked(b'{"a":9}'), xbox_meta("Run", length=280), time.time() + 50_000)
    started = msstore._unix_to_filetime(time.time())

    ops.restore_full(vault, backup, live, allow_game_running=True)

    after = {c.identifier: c for c in msstore.parse_containers_index(live)[1]}
    assert all(c.sync_state == msstore.BLOB_SYNC_MODIFIED for c in after.values())
    assert after["Slot1Auto"].filetime > future             # never back from what it had
    assert after["Slot1Manual"].filetime > after["Slot1Auto"].filetime >= started
    assert savedir.scan_any(live).slots[1].newest.label == "B"


# --- re-review: a whole copy, one index write, xgs, unidentifiable folders, case ----------------


def test_a_pair_split_by_a_write_during_the_copy_is_caught(tmp_path, monkeypatch):
    """The game rewrites the data file after it was copied but before the meta was: each
    copy is fine on its own, the pair is not. Re-stat'ing every source at the end catches it."""
    live = _steam_account(tmp_path, "st_1", b"account", vdf=None)
    vault = Vault(tmp_path / "vault")
    real_copy = ops.safety.copy_and_hash
    copied: list[Path] = []

    def copy_then_game_writes(src, dst, *args, **kwargs):
        digest = real_copy(src, dst, *args, **kwargs)
        copied.append(Path(src))
        if len(copied) == 2:
            with open(copied[0], "ab") as f:
                f.write(b"later")
        return digest

    monkeypatch.setattr(ops.safety, "copy_and_hash", copy_then_game_writes)
    with pytest.raises(ops.SourceChangedError, match="changed while it was being copied"):
        ops.snapshot_live(vault, live, reason="x")
    monkeypatch.undo()
    assert vault.entries == []


def test_the_restored_xbox_index_is_written_once_after_the_folders(xbox_pair, tmp_path, monkeypatch):
    """If composing or writing the new index fails, the live index is still the old one: it
    never names an AccountData folder that was not copied."""
    _x, y = xbox_pair
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, y, include_cache=False)
    msstore.write_save(y, "AccountData", chunked(b'{"acct":2}'), xbox_meta(length=360), 3000.0)
    index_before = (y / "containers.index").read_bytes()

    def no_index(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ops.msstore, "restored_index", no_index)
    with pytest.raises(ops.WrittenButInvalidError, match="only partly restored"):
        ops.restore_full(vault, backup, y, allow_game_running=True)
    monkeypatch.undo()

    assert (y / "containers.index").read_bytes() == index_before
    account = next(c for c in msstore.parse_containers_index(y)[1] if c.identifier == "AccountData")
    assert account.directory.is_dir() and account.data_file.read_bytes() == chunked(b'{"acct":2}')
    assert vault.read_oplog()[-1]["op"] == "restore_full"


def test_the_logged_changes_include_the_xgs_mirror(xbox_pair, tmp_path):
    _x, y = xbox_pair
    twin = y.parent.parent / "xgs" / y.name
    for c in msstore.parse_containers_index(y)[1]:
        (twin / c.identifier).mkdir(parents=True)
        (twin / c.identifier / "data").write_bytes(c.data_file.read_bytes())
        (twin / c.identifier / "meta").write_bytes(c.meta_file.read_bytes())
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, y, include_cache=False)
    msstore.write_save(y, "Slot1Auto", chunked(b'{"a":9}'), xbox_meta("Changed", ts=1500), 1500.0)

    ops.restore_full(vault, backup, y, allow_game_running=True)

    assert "xgs/Slot1Auto" in vault.read_oplog()[-1]["changed"]


def test_a_damaged_live_index_never_deletes_what_it_cannot_identify(xbox_pair, tmp_path):
    _x, y = xbox_pair
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, y, include_cache=False)
    unknown = y / ("F" * 32)
    unknown.mkdir()
    (unknown / "container.1").write_bytes(b"?")
    index = y / "containers.index"
    index.write_bytes(index.read_bytes()[:-30])

    result = ops.restore_full(vault, backup, y, allow_game_running=True)

    assert unknown.is_dir() and (unknown / "container.1").read_bytes() == b"?"
    assert any("could not be identified" in w for w in result.warnings)


def test_folder_names_match_whatever_their_case(xbox_pair, tmp_path):
    """The kept AccountData folder is spelt in lower case on disk: still recognised, still
    kept -- not deleted as unknown, not replaced by the backup's."""
    _x, y = xbox_pair
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, y, include_cache=False)
    msstore.write_save(y, "AccountData", chunked(b'{"acct":2}'), xbox_meta(length=360), 3000.0)
    folder = next(c for c in msstore.parse_containers_index(y)[1] if c.identifier == "AccountData").directory
    folder.rename(folder.with_name("tmp-rename"))
    folder.with_name("tmp-rename").rename(folder.with_name(folder.name.lower()))

    ops.restore_full(vault, backup, y, allow_game_running=True)

    assert _account_record_and_data(y)[1] == chunked(b'{"acct":2}')


def test_restore_entry_passes_the_account_data_choice_on(tmp_path):
    a = _steam_account(tmp_path, "st_1", b"account A", vdf=None)
    b = _steam_account(tmp_path, "st_2", b"account B", vdf=None)
    vault = Vault(tmp_path / "vault")
    backup = ops.create_full_backup(vault, a, include_cache=False)

    ops.restore_entry(vault, backup, b, allow_game_running=True, restore_accountdata=True)

    assert _account_files(b) == _account_files(a)


@pytest.mark.parametrize(("known_live", "advice"), [(True, "Close the game"), (False, "source folder")])
def test_an_extract_explains_a_vanished_file_by_where_it_came_from(tmp_path, monkeypatch, known_live, advice):
    live = _steam_account(tmp_path, "st_1", b"account", vdf=None)
    vault = Vault(tmp_path / "vault")

    def gone(src, dst, *args, **kwargs):
        Path(src).unlink()
        raise FileNotFoundError(2, "No such file or directory", str(src))

    monkeypatch.setattr(ops.safety, "copy_and_hash", gone)
    with pytest.raises(ops.SourceChangedError, match=advice):
        ops.extract_slot(vault, live, 1, live_dirs=[live] if known_live else [])
