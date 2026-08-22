"""Shared fixtures. Locates the live NMS save dir for read-only verification tests."""
from __future__ import annotations

import json
import os
import struct
import uuid
from pathlib import Path

import pytest

from nms_save_vault.core import catalog, formats, msstore, slotmap


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


def meta_data_files(d: Path):
    """Yield (file_no, meta_path, data_path) for save members present in ``d``."""
    for f in slotmap.all_file_numbers():
        mp = d / slotmap.meta_filename(f)
        dp = d / slotmap.data_filename(f)
        if mp.exists() and dp.exists():
            yield f, mp, dp


# --- synthetic Microsoft / Xbox 'wgs' account fixture ----------------------------------


def _wgs_data_blob(payload: bytes) -> bytes:
    """A valid single-chunk 0xFEEDA1E5 stream (current Worlds format). payload <= 15 bytes."""
    block = bytes([len(payload) << 4]) + payload          # LZ4 literals-only block
    return struct.pack("<4I", formats.SAVE_MAGIC, len(block), len(payload), 0) + block


def _wgs_meta(name: str, summary: str, play: int, size: int) -> bytes:
    """A plaintext MS meta blob. Timestamp field is 0 (as on real saves): the authoritative
    last-write time lives in containers.index, so effective time is driven by the FILETIME."""
    b = bytearray(formats.META_LEN_WORLDS_II)             # 360
    struct.pack_into("<I", b, 0, 4700)                    # base version
    struct.pack_into("<H", b, 4, 1)                       # game mode
    struct.pack_into("<Q", b, 8, play)                    # total play time
    struct.pack_into("<I", b, 16, size)                   # decompressed size
    nm, sm = name.encode("utf-8"), summary.encode("utf-8")
    b[20 : 20 + len(nm)] = nm
    b[148 : 148 + len(sm)] = sm
    struct.pack_into("<I", b, 288, 0)                     # timestamp = 0 (real-save behaviour)
    struct.pack_into("<I", b, 292, formats.META_FORMAT_4)
    return bytes(b)


@pytest.fixture
def make_wgs_account():
    """Factory building a spec-accurate wgs account folder, for Xbox read/write tests.

    Call ``make_wgs_account(parent, saves)`` where each save is
    ``(identifier, payload_text, name, summary, play, when)`` and ``when`` is the unix time
    written into the containers.index FILETIME (drives 'newest'). Returns the account dir.
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

    def build(parent, saves, name="0000000000000001_29070100B936489ABCE8B9AF3980429C") -> Path:
        acct = Path(parent) / "wgs" / name
        acct.mkdir(parents=True)
        index = struct.pack("<i", msstore.CONTAINERSINDEX_HEADER)
        index += struct.pack("<q", len(saves))
        index += _lp("proc") + struct.pack("<q", 0) + struct.pack("<i", 2) + _lp("acct")
        index += struct.pack("<q", msstore.CONTAINERSINDEX_FOOTER)
        for i, (ident, payload, nm, summ, play, when) in enumerate(saves):
            blobdir = acct / _gn(_gb(100 + i))
            blobdir.mkdir()
            data_guid, meta_guid = _gb(1 + i * 2), _gb(2 + i * 2)
            data = _wgs_data_blob(payload.encode())
            (blobdir / _gn(data_guid)).write_bytes(data)
            (blobdir / _gn(meta_guid)).write_bytes(_wgs_meta(nm, summ, play, len(payload)))
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
    mp.setattr(appstate, "load", lambda *a, **k: appstate.AppState(sources=[], vault=None))
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
