"""Shared fixtures. Locates the live NMS save dir for read-only verification tests, and
keeps every test away from this machine's real user folders (see the write guard)."""
from __future__ import annotations

import errno
import json
import os
import re
import struct
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from nms_save_vault.core import catalog, formats, meta, msstore, slotmap

# --- the write guard --------------------------------------------------------------------
#
# The owner's real data is strictly read-only, and so is everything of the app's in the
# real user folders (config, cache, logs). Two layers:
#
# 1. every test (but the slow real-data checks, which read the real config on purpose) runs
#    with the user folders pointed at fresh temp folders: the environment the app derives
#    them from (%LOCALAPPDATA%, %APPDATA%, HOME, the XDG folders, the portable install root
#    that holds state.json and accounts.ini);
# 2. an audit hook sees every file write, rename, delete and mkdir of the process and
#    REFUSES one under the real folders (PermissionError: the write never happens), for
#    every test, the slow ones included. The test that tried fails; a try outside any test
#    (a session fixture, its teardown) fails the run at the end.

_REAL_ENV = {name: os.environ.get(name) for name in (
    "LOCALAPPDATA", "APPDATA", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")}
_REAL_HOME = Path.home()
SAFETY_BACKUP = Path(r"C:\Devel\NMS-SaveBackup-SAFETY-2026-06-24")


def _norm(path) -> str:
    try:
        text = os.fsdecode(path)
    except TypeError:
        return ""
    return os.path.normcase(os.path.abspath(text))


def _guarded_roots() -> tuple[str, ...]:
    """The real per-user folders on every OS: Windows' AppData, the XDG folders (Linux,
    with the Steam and Proton saves under ~/.local/share/Steam), macOS' Application
    Support, Flatpak's app data; and the owner's safety backup."""
    home = _REAL_HOME
    roots = [_REAL_ENV["LOCALAPPDATA"], _REAL_ENV["APPDATA"], str(SAFETY_BACKUP),
             _REAL_ENV["XDG_CONFIG_HOME"] or str(home / ".config"),
             _REAL_ENV["XDG_DATA_HOME"] or str(home / ".local" / "share"),
             _REAL_ENV["XDG_CACHE_HOME"] or str(home / ".cache"),
             str(home / ".steam"), str(home / "Library" / "Application Support"),
             str(home / "Library" / "Caches"), str(home / ".var" / "app")]
    return tuple(_norm(r) for r in roots if r)


_GUARDED = _guarded_roots()
#: Where every tmp_path lives (under %LOCALAPPDATA% on Windows).
_ALLOWED = (_norm(tempfile.gettempdir()),)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_VIOLATIONS: list[str] = []          # since the current test began
_UNATTRIBUTED: list[str] = []        # outside any test: reported when the session ends


def _under(path: str, roots) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def real_write_target(event: str, args: tuple) -> str | None:
    """The guarded path an audit event would write, or None."""
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if isinstance(path, int):
            return None
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            mode is None and isinstance(flags, int) and flags & _WRITE_FLAGS)
        targets = [path] if writes else []
    elif event in ("os.rename", "shutil.move"):          # the source goes away too
        targets = list(args[:2])
    elif event in ("shutil.copyfile", "shutil.copytree", "os.link", "os.symlink"):
        targets = list(args[1:2])                         # copying FROM real data is reading
    elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.truncate", "os.utime",
                   "os.chmod", "shutil.rmtree"):
        targets = list(args[:1])
    else:
        return None
    for target in targets:
        path = _norm(target)
        if not path or not _under(path, _GUARDED) or _under(path, _ALLOWED):
            continue
        if path.endswith(".pyc") or f"{os.sep}__pycache__" in path:
            continue                    # Python's own bytecode cache of an installed runtime
        return f"{event}: {path}"
    return None


def _audit(event: str, args: tuple) -> None:
    if event in _AUDITED:
        hit = real_write_target(event, args)
        if hit:
            _VIOLATIONS.append(hit)
            raise PermissionError(errno.EACCES, "the tests never write here", hit)


_AUDITED = frozenset({"open", "os.rename", "shutil.copyfile", "shutil.copytree", "os.link",
                      "os.symlink", "os.remove", "os.rmdir", "os.mkdir", "os.truncate",
                      "os.utime", "os.chmod", "shutil.rmtree", "shutil.move"})
if _GUARDED:
    sys.addaudithook(_audit)


def take_violations() -> list[str]:
    out = list(_VIOLATIONS)
    _VIOLATIONS.clear()
    return out


def start_test() -> None:
    """Tries made since the last test ended (a session or module fixture's setup) belong to
    no test: they are kept for the end of the session, never dropped."""
    _UNATTRIBUTED.extend(take_violations())


def session_violations() -> list[str]:
    """Everything outside a test, the session's teardown included."""
    return _UNATTRIBUTED + take_violations()


def pytest_sessionfinish(session, exitstatus):
    hits = session_violations()
    if hits:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line("write guard: tried to write under the real user folders "
                                "outside any test:", red=True)
            for hit in hits[:20]:
                reporter.write_line(f"  {hit}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _no_real_user_folders(request, tmp_path_factory, monkeypatch):
    """Point the user folders at temp ones (not for the slow real-data checks), and fail the
    test if it tried to write under the real ones all the same."""
    start_test()
    if request.node.get_closest_marker("slow") is None:
        home = tmp_path_factory.mktemp("user")
        monkeypatch.setenv("NMSVAULT_PORTABLE_ROOT", str(home / "install"))
        monkeypatch.setenv("LOCALAPPDATA", str(home / "Local"))
        monkeypatch.setenv("APPDATA", str(home / "Roaming"))
        monkeypatch.setenv("HOME", str(home))
        for name, sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                          ("XDG_CACHE_HOME", ".cache")):
            monkeypatch.setenv(name, str(home / sub))
    yield
    hits = take_violations()
    if hits:
        pytest.fail("tried to write under the real user folders:\n" + "\n".join(hits[:20]))


@pytest.fixture(autouse=True)
def _no_configured_live_folders(monkeypatch):
    """The classic-operation hook names a live folder by the app's configured live folders
    (state.json); a test never reads this machine's configuration. Tests that need some set
    ``recorder._configured_sources`` themselves."""
    from nms_save_vault.core.organizer import recorder
    monkeypatch.setattr(recorder, "_configured_sources", lambda: [], raising=False)


@pytest.fixture(autouse=True)
def _new_vaults_start_old(monkeypatch):
    """Most tests build old-layout vaults with the real operations and upgrade them; a test of
    the new-vault behaviour asks for the ``new_vault_store`` fixture."""
    from nms_save_vault.core.store import versionstore
    monkeypatch.setattr(versionstore, "NEW_VAULTS_USE_STORE", False)


@pytest.fixture
def new_vault_store(monkeypatch):
    from nms_save_vault.core.store import versionstore
    monkeypatch.setattr(versionstore, "NEW_VAULTS_USE_STORE", True)


def find_live_save_dir() -> Path | None:
    """The real Steam save folder, for read-only checks: found from the real %APPDATA%,
    whatever a test has pointed the environment at."""
    appdata = _REAL_ENV["APPDATA"]
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
              ts: int = 0, difficulty: int = 0, length: int = 360,
              fmt: int = formats.META_FORMAT_4) -> bytes:
    """A plaintext Xbox meta at a real length: 360 B (Worlds II), 296 B (Worlds I) or 280 B
    (Waypoint). From 296 B it carries the playthrough id at 0x118 and the save time at
    0x120, as the game writes them; 280 B ends at 0x118 with a one-byte difficulty."""
    if length not in (280, 296, 360):
        raise ValueError(f"no Xbox meta is {length} bytes")
    b = bytearray(length)
    struct.pack_into("<I", b, 0, 4220)                    # base version (below the app's known one)
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
    struct.pack_into("<I", b, 0x124, fmt)
    return bytes(b)


def wgs_account_factory():
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


@pytest.fixture
def make_wgs_account():
    return wgs_account_factory()


# --- synthetic Steam saves ---------------------------------------------------------------


def steam_save(folder: Path, slot: int, member: int, name: str, play: int, ident: int, ts: int,
               *, extra: str = "", data_ident: int | None = None, base_version: int = 0,
               meta_format: int = formats.META_FORMAT_4, payload: bytes | None = None,
               difficulty: int = 2) -> None:
    """Write one Steam save pair (data + encrypted meta) into ``folder``. ``extra`` goes into
    the data, so two saves alike in every meta field still differ in bytes; ``data_ident`` is
    the id the data carries when it differs from the meta's (None: the same; 0: none);
    ``payload`` replaces the generated save JSON (a richer save, as the demo vault builds); ``difficulty`` is the meta's preset number."""
    folder.mkdir(parents=True, exist_ok=True)
    fno = slotmap.slot_file_numbers(slot)[member]
    if data_ident is None:
        id_field = b',"WmU":"0x%X"' % ident
    else:
        id_field = b',"WmU":"0x%X"' % data_ident if data_ident else b""
    if payload is None:
        payload = (b'{"F2P":4737,"<h0":{"Pk4":"' + name.encode("utf-8") + b'"' + id_field + b"}"
                   + (b',"x":"' + extra.encode("utf-8") + b'"' if extra else b"") + b"}")
    (folder / slotmap.data_filename(fno)).write_bytes(chunked(payload))

    plain = bytearray(formats.META_LEN_WORLDS_II)
    struct.pack_into("<I", plain, formats.OFF_HEADER, formats.META_HEADER)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT, meta_format)
    struct.pack_into("<I", plain, formats.OFF_BASE_VERSION, base_version)
    struct.pack_into("<I", plain, formats.OFF_SIZE_DECOMPRESSED, len(payload))
    struct.pack_into("<I", plain, formats.OFF_SIZE_DISK, len(chunked(payload)))
    struct.pack_into("<H", plain, formats.OFF_GAME_MODE, 1)
    struct.pack_into("<Q", plain, formats.OFF_TOTAL_PLAY_TIME, play)
    nm = name.encode("utf-8")
    plain[formats.OFF_SAVE_NAME : formats.OFF_SAVE_NAME + len(nm)] = nm
    struct.pack_into("<I", plain, formats.OFF_DIFFICULTY, difficulty)
    struct.pack_into("<Q", plain, formats.OFF_SLOT_IDENTIFIER, ident)
    struct.pack_into("<I", plain, formats.OFF_TIMESTAMP, ts)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT_TAIL, meta_format)
    ordinal = slotmap.storage_ordinal(fno)
    (folder / slotmap.meta_filename(fno)).write_bytes(meta.encrypt(bytes(plain), ordinal))


# --- the single Tk root for the whole session ------------------------------------------

class Dialogs:
    """Every message box, question and file dialog a test shows - recorded, never displayed.

    A test that expects one claims it with :meth:`take` (and may assert on the messages); any
    that nobody claimed fails the test at its teardown. A real modal dialog waits for a human
    and has been seen on the owner's screen, so nothing can reach Tk's own."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, str, str]] = []     # (kind, title, message)
        self._claimed = 0
        self.expecting = False

    def expect(self) -> "Dialogs":
        """This test drives dialogs: they are recorded (and answered with the safe default)
        instead of raising; :meth:`take` claims them."""
        self.expecting = True
        return self

    def record(self, kind: str, title: str = "", message: str = "") -> None:
        self.shown.append((kind, str(title), str(message)))

    def take(self) -> list[tuple[str, str, str]]:
        """The messages shown so far, claimed (they are expected)."""
        self._claimed = len(self.shown)
        return list(self.shown)

    @property
    def unclaimed(self) -> list[tuple[str, str, str]]:
        return self.shown[self._claimed:]


_DIALOG_DEFAULTS = {
    "showinfo": "ok", "showwarning": "ok", "showerror": "ok",
    "askyesno": False, "askokcancel": False, "askyesnocancel": None, "askretrycancel": False,
    "askquestion": "no", "askstring": None, "askinteger": None, "askfloat": None,
    "askdirectory": "", "askopenfilename": "", "asksaveasfilename": "", "askopenfilenames": (),
}


_REC = Dialogs()


def _install_dialog_stubs() -> None:
    """Replace every tkinter message box / dialog, ``webbrowser.open`` and the nested waits at
    IMPORT time - before the session's App is built and before ``gui`` binds the real functions -
    so nothing, not even a fixture, can show a window a human must close."""
    import tkinter as tk
    import webbrowser
    from tkinter import filedialog, messagebox, simpledialog

    def stub(kind):
        default = _DIALOG_DEFAULTS[kind]

        def shown(*args, **kw):
            title = kw.get("title", args[0] if args else "")
            message = kw.get("message", kw.get("prompt", args[1] if len(args) > 1 else ""))
            _REC.record(kind, title, message)
            if not _REC.expecting:
                raise AssertionError(f"unexpected dialog {kind}: {title!r} {str(message)[:80]!r} - "
                                     "a test must never show a dialog nobody can close "
                                     "(call dialogs.expect() and dialogs.take() if intended)")
            return default
        return shown

    for module in (messagebox, simpledialog, filedialog):
        for kind in _DIALOG_DEFAULTS:
            if hasattr(module, kind):
                setattr(module, kind, stub(kind))

    def browser(url, *args, **kw):
        _REC.record("webbrowser", "open", url)
        if not _REC.expecting:
            raise AssertionError(f"unexpected browser open: {str(url)[:80]!r}")
        return True
    for name in ("open", "open_new", "open_new_tab"):
        setattr(webbrowser, name, browser)

    def never_wait(self, window=None):
        """Fail fast: nothing in a test can close a window a nested wait is waiting for."""
        target = window if window is not None and not isinstance(window, str) else self
        try:
            alive = target.winfo_exists()
        except tk.TclError:
            alive = False
        if not alive:
            return
        raise AssertionError("a test waited on a window nobody can close "
                             f"({target.winfo_class()}); drive or stub the dialog")
    for name in ("wait_window", "wait_visibility", "wait_variable"):
        setattr(tk.Misc, name, never_wait)

    from nms_save_vault import gui          # binds the stubs above, never the real functions
    for name, kind in (("_showinfo", "showinfo"), ("_showwarning", "showwarning"),
                       ("_showerror", "showerror"), ("_askyesno", "askyesno"),
                       ("_askyesnocancel", "askyesnocancel")):
        setattr(gui, name, stub(kind))

    def confirm(master, title, message, check=None, default="yes"):
        _REC.record("confirm", title, message)
        if not _REC.expecting:
            raise AssertionError(f"unexpected confirm dialog: {title!r}")
        return False, False
    gui._confirm = confirm


_install_dialog_stubs()


@pytest.fixture(autouse=True)
def dialogs():
    """The recorder of every dialog a test shows (see :class:`Dialogs`): unexpected ones raise,
    expected ones are claimed with ``expect()`` / ``take()``, unclaimed ones fail the test."""
    _REC.shown.clear()
    _REC._claimed = 0
    _REC.expecting = False
    yield _REC
    left = _REC.unclaimed
    if left:
        pytest.fail("a test showed a dialog nobody claimed (use the `dialogs` fixture's take()): "
                    + "; ".join(f"{k}: {t!r} {m[:80]!r}" for k, t, m in left[:5]))


_GUI_APP: list = []


@pytest.fixture(autouse=True)
def _no_content_index_in_the_app(monkeypatch):
    """The App starts the content index (threads, worker processes) unless told not to; a test
    that wants one builds its own service and closes it."""
    monkeypatch.setenv("NMSVAULT_INDEX", "off")


@pytest.fixture(autouse=True)
def _drain_ui_worker():
    """A job a test left in the UI worker must finish before the test's environment redirect
    is undone: it would otherwise run against the real user folders (the machine id, the
    caches) while the next test is set up or after the last one."""
    yield
    import time

    for app in list(_GUI_APP):
        end = time.monotonic() + 10
        while app.worker.busy and time.monotonic() < end:
            app.update()
            time.sleep(0.005)
        try:            # a test that pointed the App at its own vault leaves its rows behind
            app._populate_backups()
        except Exception:  # noqa: BLE001
            pass


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
    # The App reads and writes its config (the theme, the zoom): never the real one.
    mp.setenv("NMSVAULT_PORTABLE_ROOT", str(tmp_path / "install"))
    mp.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    mp.setenv("APPDATA", str(tmp_path / "Roaming"))
    try:
        application = _build_app(gui, tmp_path)
    finally:
        mp.undo()
    application.withdraw()
    application.update()
    _GUI_APP.append(application)
    yield application
    _GUI_APP.clear()
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
    # No start-up scan: the organizer tests start theirs when they want one.
    return gui.App(vault=vault_root, state_path=tmp_path / "install" / "state.json",
                   scan_on_start=False)
