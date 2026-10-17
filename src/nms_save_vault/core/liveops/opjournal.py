"""The operation journal (D5 2.7) and the concurrency pieces around it (2.16).

``<vault>/organizer/ops.jsonl`` is the write-ahead record of every live operation: its
intent (``preparing``), the files it may create (``writing``, appended and fsynced before
the first live write), its progress and outcome, and its verification. It is the source of
truth for recovery, undo and verification. Append-only, one ASCII JSON object per line, each
append one short transaction under the organizer's store lock. The reader skips a torn last
line (a crash during an append) and any damaged line, and says so in the log.

Also here: the per-machine marker of an open op (its records may sit on a vault drive that
is not connected at the next start), the machine id that keeps one machine from recovering
another's op in a shared vault, the live-op lock, and the busy targets D1's observer skips.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import msstore, platform, safety, slotmap
from ..organizer.cache import cache_dir
from ..organizer.model import LiveTarget, ModelError, is_op_id, target_from_json, target_to_json
from ..organizer.recorder import LIVEOP_TIMEOUT, LiveOpBusy
from ..organizer.store import liveop_lock_path
from .context import OpContext
from .model import MemberWrite
from .steamwrite import FileState, SteamFileWrite, SteamWritePlan

log = logging.getLogger(__name__)

OPS = "ops.jsonl"
JOURNAL_VERSION = 1
EVENTS = ("preparing", "writing", "validated", "done", "rolled_back", "failed", "abandoned",
          "recovered", "verify")
#: The last lifecycle event (any but ``verify``) is an op's status; these leave it open.
OPEN = frozenset({"preparing", "writing", "validated", "failed"})
ABANDON_REASONS = ("not_started", "preserve_failed", "snapshot_failed", "game_started", "stale",
                   "superseded", "user_left")
_ENVELOPE = ("j", "op_id", "seq", "event", "utc", "machine")

MACHINE_ID_FILE = "machine-id"
MARKERS = "open-ops"
_MACHINE_ID = re.compile(r"m-[0-9a-f]{12}")


class JournalError(ValueError):
    """A journal payload that does not decode (recovery then leaves that op alone)."""


# --- appending -----------------------------------------------------------------------


def ops_path(ctx: OpContext) -> Path:
    return ctx.store.root / OPS


def append(ctx: OpContext, op_id: str, event: str, payload: Mapping[str, Any] | None = None
           ) -> dict:
    """Append one event of ``op_id`` and return it. One transaction under the store lock:
    ``StoreBusy`` when another writer keeps it past the timeout (nothing is appended)."""
    if not is_op_id(op_id) or event not in EVENTS:
        raise ValueError(f"not a journal event: {op_id!r} {event!r}")
    payload = dict(payload or {})
    if set(payload) & set(_ENVELOPE):
        raise ValueError("the envelope keys are assigned by the journal")
    path = ops_path(ctx)
    with ctx.store.transaction():
        seq = 1 + sum(1 for e in read_events(path) if e["op_id"] == op_id)
        record = {"j": JOURNAL_VERSION, "op_id": op_id, "seq": seq, "event": event,
                  "utc": utc_text(ctx.clock()), "machine": ctx.machine_id, **payload}
        line = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        _append_line(path, (line + "\n").encode("ascii"))
    return record


def utc_text(at: float) -> str:
    """``2026-10-16T10:15:03.120Z``."""
    moment = datetime.fromtimestamp(at, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _append_line(path: Path, data: bytes) -> None:
    """One fsynced write; a line torn by an earlier crash is sealed off first, so it can
    never merge with this one."""
    with open(path, "a+b", buffering=0) as f:
        size = f.seek(0, os.SEEK_END)
        if size:
            f.seek(size - 1)
            if f.read(1) != b"\n":
                data = b"\n" + data
        pending = memoryview(data)
        while pending:
            pending = pending[f.write(pending):]
        os.fsync(f.fileno())


# --- reading -------------------------------------------------------------------------


def read_events(path: Path) -> list[dict]:
    """Every readable event in file order. A last line without its newline is an append
    that did not finish (a crash, or one in progress) and is skipped; a damaged line is
    skipped. Both are logged; the file is never rewritten."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return []
    lines = data.split(b"\n")
    tail = lines.pop()
    if tail.strip():
        log.warning("%s: the last line is incomplete (an append did not finish); ignored", OPS)
    events = []
    for number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            log.warning("%s: line %d is damaged and was skipped (%s)", OPS, number, exc)
            continue
        if not _valid_envelope(record):
            log.warning("%s: line %d is not an operation event and was skipped", OPS, number)
            continue
        events.append(record)
    return events


def _valid_envelope(record: object) -> bool:
    return (isinstance(record, dict) and record.get("j") == JOURNAL_VERSION
            and is_op_id(record.get("op_id")) and record.get("event") in EVENTS
            and isinstance(record.get("seq"), int) and isinstance(record.get("utc"), str)
            and isinstance(record.get("machine"), str))


@dataclass(frozen=True)
class OpRecord:
    """One op as its events fold up."""

    op_id: str
    machine: str
    kind: str                              # from `preparing`; "" when that line is lost
    targets: tuple[LiveTarget, ...]
    status: str                            # its last lifecycle event
    started: str                           # utc of its first event
    events: tuple[dict, ...]

    @property
    def open(self) -> bool:
        return self.status in OPEN

    def last(self, event: str) -> dict | None:
        return next((e for e in reversed(self.events) if e["event"] == event), None)

    def verify_state(self, target: LiveTarget) -> str | None:
        """The target's last ``verify`` state; ``pending`` after ``done`` with none yet."""
        for e in reversed(self.events):
            if e["event"] == "verify" and _target_or_none(e.get("target")) == target:
                return e.get("state")
        return "pending" if self.status == "done" else None


def fold(events: Sequence[dict]) -> dict[str, OpRecord]:
    """Events grouped by op, in the order the ops started."""
    grouped: dict[str, list[dict]] = {}
    for e in events:
        grouped.setdefault(e["op_id"], []).append(e)
    return {op_id: _record(op_id, evs) for op_id, evs in grouped.items()}


def _record(op_id: str, events: list[dict]) -> OpRecord:
    lifecycle = [e for e in events if e["event"] != "verify"]
    preparing = next((e for e in events if e["event"] == "preparing"), None)
    writing = next((e for e in events if e["event"] == "writing"), None)
    raw_targets = (preparing or {}).get("targets")
    if raw_targets is None and writing is not None:
        raw_targets = [t.get("target") for t in writing.get("targets", []) if isinstance(t, dict)]
    targets = []
    for raw in raw_targets or []:
        target = _target_or_none(raw)
        if target is None:
            log.warning("%s: op %s names a target that does not decode", OPS, op_id)
        else:
            targets.append(target)
    kind = (preparing or {}).get("kind")
    return OpRecord(op_id, events[0]["machine"], kind if isinstance(kind, str) else "",
                    tuple(targets), lifecycle[-1]["event"] if lifecycle else "verify",
                    events[0]["utc"], tuple(events))


def _target_or_none(raw: object) -> LiveTarget | None:
    try:
        return target_from_json(raw)
    except ModelError:
        return None


def read_ops(ctx: OpContext) -> dict[str, OpRecord]:
    return fold(read_events(ops_path(ctx)))


def open_ops(ctx: OpContext) -> list[OpRecord]:
    """This machine's open ops (another machine's are its own to recover)."""
    return [r for r in read_ops(ctx).values() if r.open and r.machine == ctx.machine_id]


def busy_targets(ctx: OpContext) -> frozenset[LiveTarget]:
    """The live slots an open op of this machine may be writing: D1's observer skips them,
    so a refresh during a write never journals a half-written slot as a game event."""
    return frozenset(t for r in open_ops(ctx) for t in r.targets)


def target_json(target: LiveTarget) -> dict:
    """A target as the journal stores it (D1's codec, shared with the organizer journal)."""
    return target_to_json(target)


# --- the live-op lock ------------------------------------------------------------------


@contextmanager
def liveop_lock(ctx: OpContext, timeout: float = LIVEOP_TIMEOUT) -> Iterator[None]:
    """One live write at a time, organizer and classic alike, across windows, processes and
    machines sharing the vault: the lock D1's classic-write hook takes too. A wait past
    ``timeout`` raises ``LiveOpBusy``. Always taken before the store lock."""
    with ExitStack() as stack:
        try:
            stack.enter_context(safety.exclusive_lock(liveop_lock_path(ctx.store.vault_root),
                                                      timeout=timeout))
        except safety.LockTimeout as exc:
            raise LiveOpBusy() from exc
        yield


# --- markers and the machine id ----------------------------------------------------------


@dataclass(frozen=True)
class Marker:
    op_id: str
    vault_id: str
    vault_root: Path
    labels: tuple[str, ...]
    utc: str


def markers_dir(ctx: OpContext) -> Path:
    return cache_dir(ctx.store.vault_root, ctx.store.vault_id, ctx.host) / MARKERS


def write_marker(ctx: OpContext, op_id: str, labels: Sequence[str]) -> None:
    """Note on this machine that ``op_id`` is open; removed after its terminal event. Holds
    no save data: losing it loses only the hint "connect the vault drive"."""
    marker = {"op_id": op_id, "vault_id": ctx.store.vault_id,
              "vault_root": str(Path(ctx.store.vault_root).resolve()), "labels": list(labels),
              "utc": utc_text(ctx.clock())}
    safety.atomic_write_bytes(markers_dir(ctx) / f"{op_id}.json",
                              json.dumps(marker, ensure_ascii=True).encode("ascii"))


def remove_marker(ctx: OpContext, op_id: str) -> None:
    (markers_dir(ctx) / f"{op_id}.json").unlink(missing_ok=True)


def list_markers(host: platform.Host) -> list[Marker]:
    """Every open-op marker on this machine, for every vault; an unreadable one is logged."""
    out = []
    root = platform.user_cache_dir(host) / "vaults"
    for path in sorted(root.glob(f"*/{MARKERS}/*.json")):
        try:
            d = json.loads(path.read_text("utf-8"))
            marker = Marker(d["op_id"], d["vault_id"], Path(d["vault_root"]),
                            tuple(str(x) for x in d["labels"]), d["utc"])
            if not is_op_id(marker.op_id) or not isinstance(marker.vault_id, str):
                raise ValueError("not a marker")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("open-op marker %s skipped: %s", path.name, exc)
            continue
        out.append(marker)
    return out


def machine_id(host: platform.Host) -> str:
    """This machine's id (``m-`` + 12 hex), created once in the per-user data folder - not
    the cache, which may be cleaned. When it cannot be kept, a fresh one is used for this
    run (ops it opens then look like another machine's: left alone, never mis-recovered)."""
    path = platform.user_data_dir(host) / MACHINE_ID_FILE
    current = _read_machine_id(path)
    if current:
        return current
    new = f"m-{secrets.token_hex(6)}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "x", encoding="ascii") as f:
                f.write(new + "\n")
                f.flush()
                os.fsync(f.fileno())
            return new
        except FileExistsError:
            current = _read_machine_id(path)        # made meanwhile by another process
            if current:
                return current
            safety.atomic_write_bytes(path, (new + "\n").encode("ascii"))   # a damaged one
            return new
    except OSError as exc:
        log.warning("the machine id could not be kept: %s", exc.strerror)
        return new


def _read_machine_id(path: Path) -> str | None:
    try:
        text = path.read_text("ascii").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("the machine id does not read: %s", exc)
        return None
    return text if _MACHINE_ID.fullmatch(text) else None


# --- payload codecs (what recovery needs back from a `writing` event) -----------------------

_GUID = r"[0-9A-F]{32}"
_BLOB_FILE = re.compile(rf"{_GUID}/(?:{_GUID}|container\.[0-9]{{1,3}})")
_DIR = re.compile(_GUID)
_TAG = re.compile(r"[0-9a-f]{4}")


def file_state_to_json(state: FileState | None) -> dict | None:
    return None if state is None else {"sha256": state.sha256, "size": state.size,
                                       "mtime_ns": state.mtime_ns}


def file_state_from_json(value: object) -> FileState | None:
    if value is None:
        return None
    d = _obj(value)
    return FileState(_sha(d.get("sha256")), _uint(d.get("size")), _uint(d.get("mtime_ns")))


def member_write_to_json(mw: MemberWrite) -> dict:
    return mw.to_dict()


def member_write_from_json(value: object) -> MemberWrite:
    d = _obj(value)
    try:
        return MemberWrite(
            position=_position(d["position"]), vid=_sha(d["vid"]),
            pid=_opt(d["pid"], str), play_time=_uint(d["play_time"]),
            meta_ts=_uint(d["meta_ts"]), source_meta_ts=_opt(d["source_meta_ts"], int),
            meta_plain_sha256=_sha(d["meta_plain_sha256"]),
            meta_disk_sha256=_sha(d["meta_disk_sha256"]), disk_time=_number(d["disk_time"]),
            ordinal=_opt(d["ordinal"], int),
            copy_of=None if d["copy_of"] is None else _position(d["copy_of"]),
            origin=_obj(d["origin"]))
    except KeyError as exc:
        raise JournalError(f"a member write lacks {exc}") from None


def steam_plan_to_json(plan: SteamWritePlan) -> dict:
    """The plan without its folder (the target says which): every path is a file name."""
    return {"tag": plan.op_tag,
            "files": [{"name": f.name, "temp": f.temp_name, "sha256": f.sha256, "mtime": f.mtime}
                      for f in plan.files],
            "pre": {name: file_state_to_json(st) for name, st in plan.pre.items()}}


def steam_plan_from_json(value: object, folder: Path) -> SteamWritePlan:
    """Decode and check: only slot file names and this op's temp names, so a damaged
    journal can never point a rollback at another file."""
    d = _obj(value)
    tag = d.get("tag")
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise JournalError("not an op tag")
    files = []
    for f in _list(d.get("files")):
        f = _obj(f)
        name = _slot_file(f.get("name"))
        if f.get("temp") != f".tmp_{tag}_{name}.part":
            raise JournalError(f"not the op's temp name for {name}")
        files.append(SteamFileWrite(name, _sha(f.get("sha256")), _number(f.get("mtime")),
                                    f.get("temp")))
    pre = {_slot_file(name): file_state_from_json(st) for name, st in _obj(d.get("pre")).items()}
    return SteamWritePlan(Path(folder), tuple(files), pre, tag)


def xbox_plan_to_json(plan: msstore.XboxPairPlan) -> dict:
    return {
        "pre_index_sha256": plan.pre_index_sha256,
        "pre_header_b64": _b64(plan.pre_header_raw),
        "pre_records_b64": {k: None if v is None else _b64(v)
                            for k, v in plan.pre_records_raw.items()},
        "new_header_b64": _b64(plan.new_header_raw),
        "new_records_b64": {k: _b64(v) for k, v in plan.new_records_raw.items()},
        "new_files": list(plan.new_files), "new_dirs": list(plan.new_dirs),
        "superseded": list(plan.superseded), "xgs": plan.xgs,
        "blobs": [{"identifier": b.identifier, "dir": b.dir_name, "data": b.data_name,
                   "meta": b.meta_name, "container": b.container_name,
                   "data_cloud": b.data_cloud, "meta_cloud": b.meta_cloud,
                   "data_sha256": b.data_sha256, "meta_sha256": b.meta_sha256,
                   "mtime": b.mtime} for b in plan.blobs],
    }


def xbox_plan_from_json(value: object, folder: Path) -> msstore.XboxPairPlan:
    """Decode and check: only GUID-named blob files and record dirs of the account folder,
    and slot identifiers, so a damaged journal can never point a rollback elsewhere."""
    d = _obj(value)
    blobs = []
    for b in _list(d.get("blobs")):
        b = _obj(b)
        dir_name = _match(_DIR, b.get("dir"))
        blobs.append(msstore.XboxBlobWrite(
            _slot_id(b.get("identifier")), dir_name, _match(_DIR, b.get("data")),
            _match(_DIR, b.get("meta")),
            _match(re.compile(r"container\.[0-9]{1,3}"), b.get("container")),
            _guid_or_empty(b.get("data_cloud")), _guid_or_empty(b.get("meta_cloud")),
            _sha(b.get("data_sha256")), _sha(b.get("meta_sha256")), _number(b.get("mtime"))))
    return msstore.XboxPairPlan(
        folder=Path(folder), pre_index_sha256=_sha(d.get("pre_index_sha256")),
        pre_header_raw=_unb64(d.get("pre_header_b64")),
        pre_records_raw={_slot_id(k): None if v is None else _unb64(v)
                         for k, v in _obj(d.get("pre_records_b64")).items()},
        new_header_raw=_unb64(d.get("new_header_b64")),
        new_records_raw={_slot_id(k): _unb64(v)
                         for k, v in _obj(d.get("new_records_b64")).items()},
        new_files=tuple(_match(_BLOB_FILE, p) for p in _list(d.get("new_files"))),
        new_dirs=tuple(_match(_DIR, p) for p in _list(d.get("new_dirs"))),
        superseded=tuple(_match(_BLOB_FILE, p) for p in _list(d.get("superseded"))),
        xgs=_bool(d.get("xgs")), blobs=tuple(blobs))


def _obj(value: object) -> dict:
    if not isinstance(value, dict):
        raise JournalError("expected an object")
    return value


def _list(value: object) -> list:
    if not isinstance(value, list):
        raise JournalError("expected a list")
    return value


def _uint(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise JournalError(f"not a count: {value!r}")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value >= 0:
        raise JournalError(f"not a time: {value!r}")
    return float(value)


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise JournalError(f"not a flag: {value!r}")
    return value


def _opt(value: object, kind: type) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, kind):
        raise JournalError(f"not a {kind.__name__}: {value!r}")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise JournalError("not a sha256")
    return value


def _position(value: object) -> str:
    if value not in ("A", "B"):
        raise JournalError(f"not a position: {value!r}")
    return value  # type: ignore[return-value]


def _match(pattern: re.Pattern, value: object) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise JournalError(f"not an expected name: {value!r}")
    return value


def _guid_or_empty(value: object) -> str:
    return "" if value == "" else _match(_DIR, value)


def _slot_file(value: object) -> str:
    """A slot's data or meta file name in its canonical spelling."""
    if isinstance(value, str):
        fno = slotmap.parse_data_filename(value) or slotmap.parse_meta_filename(value)
        if fno in slotmap.all_file_numbers() and value in (
                slotmap.data_filename(fno), slotmap.meta_filename(fno)):
            return value
    raise JournalError(f"not a slot file name: {value!r}")


def _slot_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"Slot([1-9]|1[0-5])(Auto|Manual)", value):
        raise JournalError(f"not a slot save identifier: {value!r}")
    return value


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise JournalError("not base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise JournalError(f"not base64: {exc}") from None
