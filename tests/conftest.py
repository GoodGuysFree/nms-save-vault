"""Shared fixtures. Locates the live NMS save dir for read-only verification tests."""
from __future__ import annotations

import json
import os
import re
import struct
import uuid
from pathlib import Path

import pytest

from nms_save_vault.core import catalog, formats, meta, msstore, slotmap


def find_live_save_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = Path(appdata) / "HelloGames" / "NMS"
    if not base.is_dir():
        return None
    for candidate in sorted(base.glob("st_*")):
        if candidate.is_dir() and any(candidate.glob("mf_save*.hg")):
            return candidate
    return None


@pytest.fixture(scope="session")
def live_save_dir() -> Path:
    d = find_live_save_dir()
    if d is None:
        pytest.skip("no live NMS save directory found")
    return d


@pytest.fixture(scope="session")
def live_wgs_dir() -> Path:
    """The real Game Pass wgs account folder, for read-only checks; skips when absent."""
    d = next((p for p in msstore.find_microsoft_save_dirs() if msstore.is_microsoft_save_dir(p)), None)
    if d is None:
        pytest.skip("no live Xbox / Game Pass save directory found")
    return d


def meta_data_files(d: Path):
    """Yield (file_no, meta_path, data_path) for save members present in ``d``."""
    for f in slotmap.all_file_numbers():
        mp = d / slotmap.meta_filename(f)
        dp = d / slotmap.data_filename(f)
        if mp.exists() and dp.exists():
            yield f, mp, dp


# --- synthetic saves: data blobs, Xbox metas, wgs accounts ------------------------------


def lz4_literals(payload: bytes) -> bytes:
    """A literals-only LZ4 block holding ``payload`` (any length)."""
    lit = len(payload)
    if lit < 15:
        return bytes([lit << 4]) + payload
    rest = lit - 15
    cont = bytearray()
    while rest >= 255:
        cont.append(255)
        rest -= 255
    cont.append(rest)
    return bytes([0xF0]) + bytes(cont) + payload


def chunked(payload: bytes) -> bytes:
    """A valid single-chunk 0xFEEDA1E5 save data stream (Steam and current Xbox saves)."""
    block = lz4_literals(payload)
    return struct.pack("<4I", formats.SAVE_MAGIC, len(block), len(payload), 0) + block


def wgs_playthrough_id(identifier: str) -> int:
    """The playthrough id ``make_wgs_account`` gives a save unless told otherwise: one per
    slot (both members of a slot are one run), with the high bytes set so that a read
    stopping at 32 bits shows up. 0 for account-level records."""
    m = re.fullmatch(r"Slot(\d+)(?:Auto|Manual)", identifier)
    return 0xA1B2C3D4_00000000 + int(m.group(1)) if m else 0


def xbox_meta(name: str = "", summary: str = "", *, play: int = 0, size: int = 0, pid: int = 0,
              ts: int = 0, difficulty: int = 0, length: int = 360) -> bytes:
    """A plaintext Xbox meta at a real length: 360 B (Worlds II), 296 B (Worlds I) or 280 B
    (Waypoint). From 296 B it carries the playthrough id at 0x118 and the save time at
    0x120, as the game writes them; 280 B ends at 0x118 with a one-byte difficulty."""
    if length not in (280, 296, 360):
        raise ValueError(f"no Xbox meta is {length} bytes")
    b = bytearray(length)
    struct.pack_into("<I", b, 0, 4700)                    # base version
    struct.pack_into("<H", b, 4, 1)                       # game mode
    struct.pack_into("<Q", b, 8, play)                    # total play time
    struct.pack_into("<I", b, 16, size)                   # decompressed size
    nm, sm = name.encode("utf-8"), summary.encode("utf-8")  # UTF-8, NUL-terminated
    b[20 : 20 + len(nm)] = nm
    b[148 : 148 + len(sm)] = sm
    if length == 280:
        b[0x114] = difficulty
        return bytes(b)
    struct.pack_into("<I", b, 0x114, difficulty)
    struct.pack_into("<Q", b, 0x118, pid)                 # playthrough id
    struct.pack_into("<I", b, 0x120, ts)                  # save time (unix seconds)
    struct.pack_into("<I", b, 0x124, formats.META_FORMAT_4)
    return bytes(b)


@pytest.fixture
def make_wgs_account():
    """Factory building a spec-accurate wgs account folder, for Xbox read/write tests.

    Call ``make_wgs_account(parent, saves)`` where each save is
    ``(identifier, payload_text, name, summary, play, when[, pid[, meta_ts]])``. ``when`` is
    the unix time written into the containers.index FILETIME; ``meta_ts`` is the save time
    in the meta (default ``int(when)``), which is what decides 'newest'; ``pid`` defaults to
    :func:`wgs_playthrough_id`. ``meta_len`` 296 or 280 builds Worlds I or Waypoint metas
    instead of 360-byte ones (280 holds no id or time). ``account_id`` is the account the
    index header names. Returns the account dir.
    """
    def _gb(seed: int) -> bytes:
        return bytes((seed + i) % 256 for i in range(16))

    def _gn(raw: bytes) -> str:
        return uuid.UUID(bytes_le=raw).hex.upper()

    def _lp(s: str) -> bytes:
        return struct.pack("<i", len(s)) + s.encode("utf-16-le")

    def _fixed(s: str, total: int = 128) -> bytes:
        raw = s.encode("utf-16-le")
        return raw + b"\x00" * (total - len(raw))

    def build(parent, saves, name="0000000000000001_29070100B936489ABCE8B9AF3980429C",
              meta_len: int = 360, account_id: str = "acct") -> Path:
        acct = Path(parent) / "wgs" / name
        acct.mkdir(parents=True)
        index = struct.pack("<i", msstore.CONTAINERSINDEX_HEADER)
        index += struct.pack("<q", len(saves))
        index += _lp("proc") + struct.pack("<q", 0) + struct.pack("<i", 2) + _lp(account_id)
        index += struct.pack("<q", msstore.CONTAINERSINDEX_FOOTER)
        for i, (ident, payload, nm, summ, play, when, *extra) in enumerate(saves):
            pid = extra[0] if extra else wgs_playthrough_id(ident)
            meta_ts = extra[1] if len(extra) > 1 else int(when)
            blobdir = acct / _gn(_gb(100 + i))
            blobdir.mkdir()
            data_guid, meta_guid = _gb(1 + i * 2), _gb(2 + i * 2)
            data = chunked(payload.encode())
            (blobdir / _gn(data_guid)).write_bytes(data)
            meta_blob = xbox_meta(nm, summ, play=play, size=len(payload), pid=pid, ts=meta_ts,
                                  length=meta_len)
            (blobdir / _gn(meta_guid)).write_bytes(meta_blob)
            container = struct.pack("<ii", msstore.BLOBCONTAINER_HEADER, 2)
            container += _fixed("data") + _gb(200 + i) + data_guid
            container += _fixed("meta") + _gb(210 + i) + meta_guid
            (blobdir / "container.1").write_bytes(container)
            index += _lp(ident) + _lp("") + _lp("synchex")
            index += bytes([1]) + struct.pack("<i", 2) + _gb(100 + i)
            index += struct.pack("<q", msstore._unix_to_filetime(float(when)))
            index += struct.pack("<q", 0) + struct.pack("<q", len(data))
        (acct / "containers.index").write_bytes(index)
        return acct

    return build


# --- synthetic Steam saves ---------------------------------------------------------------


def steam_save(folder: Path, slot: int, member: int, name: str, play: int, ident: int, ts: int) -> None:
    """Write one Steam save pair (data + encrypted meta) into ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    fno = slotmap.slot_file_numbers(slot)[member]
    payload = b'{"F2P":4737,"<h0":{"Pk4":"' + name.encode("utf-8") + b'","WmU":"0x%X"}}' % ident
    (folder / slotmap.data_filename(fno)).write_bytes(chunked(payload))

    plain = bytearray(formats.META_LEN_WORLDS_II)
    struct.pack_into("<I", plain, formats.OFF_HEADER, formats.META_HEADER)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT, formats.META_FORMAT_4)
    struct.pack_into("<I", plain, formats.OFF_SIZE_DECOMPRESSED, len(payload))
    struct.pack_into("<I", plain, formats.OFF_SIZE_DISK, len(chunked(payload)))
    struct.pack_into("<H", plain, formats.OFF_GAME_MODE, 1)
    struct.pack_into("<Q", plain, formats.OFF_TOTAL_PLAY_TIME, play)
    nm = name.encode("utf-8")
    plain[formats.OFF_SAVE_NAME : formats.OFF_SAVE_NAME + len(nm)] = nm
    struct.pack_into("<I", plain, formats.OFF_DIFFICULTY, 2)
    struct.pack_into("<Q", plain, formats.OFF_SLOT_IDENTIFIER, ident)
    struct.pack_into("<I", plain, formats.OFF_TIMESTAMP, ts)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT_TAIL, formats.META_FORMAT_4)
    ordinal = slotmap.storage_ordinal(fno)
    (folder / slotmap.meta_filename(fno)).write_bytes(meta.encrypt(bytes(plain), ordinal))


# --- the single Tk root for the whole session ------------------------------------------

@pytest.fixture(scope="session")
def gui_app(tmp_path_factory):
    """A real App over a synthetic vault, with no live sources and no window shown.

    Session-scoped on purpose, and the ONLY Tk root the suite may create: this Python's
    Tcl cannot reliably build a second root once one has been destroyed ("Can't find a
    usable tk.tcl"), which made per-test Apps flaky. Tests that mutate it reset it
    afterwards (see test_gui_panes._reset_app); tests needing a widget parent use this App.
    """
    from nms_save_vault import gui
    from nms_save_vault.core import aliases, locations
    from nms_save_vault.core import state as appstate

    tmp_path = tmp_path_factory.mktemp("gui")
    # The patches are only needed while the App reads its config; a session-scoped
    # MonkeyPatch context would stay open for the rest of the run and break unrelated
    # tests, so undo them as soon as construction is done.
    mp = pytest.MonkeyPatch()
    mp.setattr(aliases, "_active", aliases.AliasMap())
    # update_check="off" matters: the App schedules its startup check 300ms after
    # construction, and the default "ask" would pop a modal dialog partway through the
    # suite and hang it. Tests that exercise update checking set the flag themselves.
    mp.setattr(
        appstate,
        "load",
        lambda *a, **k: appstate.AppState(sources=[], vault=None, update_check="off"),
    )
    # Keep the real machine's save folder out of the test: the App falls back to it when
    # the config lists no sources.
    mp.setattr(locations, "default_live_save_dir", lambda: None)
    try:
        application = _build_app(gui, tmp_path)
    finally:
        mp.undo()
    application.withdraw()
    application.update()
    yield application
    application.destroy()


def _build_app(gui, tmp_path):
    vault_root = tmp_path / "vault"
    entries = [
        ("full-steam-20260101-000000", catalog.KIND_FULL, "2026-01-01T00:00:00", "Bravo", 3),
        ("extract-steam-20260301-000000", catalog.KIND_EXTRACT, "2026-03-01T00:00:00", "alpha", 1),
        ("inplace-20260201-000000", catalog.KIND_INPLACE, "2026-02-01T00:00:00", "Charlie", 2),
    ]
    payload = {"version": 1, "entries": []}
    for eid, kind, created, label, nslots in entries:
        payload["entries"].append({
            "id": eid, "kind": kind, "label": label, "path": str(tmp_path / eid),
            "created": created, "managed": kind != catalog.KIND_INPLACE, "note": "",
            "slots": [{
                "slot": i + 1, "occupied": True, "name": f"Save {i + 1}", "newest_label": "B",
                "members": [
                    {"label": "A", "present": True, "name": f"Save {i + 1}", "game_mode": 1,
                     "difficulty": 2, "play_time": 3600, "timestamp": 1_700_000_000,
                     "data_size": 1024 * 1024, "valid": True},
                    {"label": "B", "present": True, "name": f"Save {i + 1}", "game_mode": 1,
                     "difficulty": 3, "play_time": 3660, "timestamp": 1_700_000_600,
                     "data_size": 1024 * 1024, "valid": True},
                ],
            } for i in range(nslots)],
        })
    vault_root.mkdir(parents=True)
    (vault_root / "catalog.json").write_text(json.dumps(payload), "utf-8")
    return gui.App(vault=vault_root)
