"""The Steam slot writer (D5 2.6.1).

``prepare`` reads the slot and plans every file, writing nothing; the journal records that
plan before ``stage`` creates the first file. ``stage`` writes each new file as
``.tmp_<tag>_<name>.part`` beside the live ones (same volume, so the swap is atomic),
fsynced and read back. ``swap`` renames them in: the other position's data and meta first,
the loading member's meta last, each followed by its planned mtime. ``rollback`` puts back
every touched name exactly (bytes and ``mtime_ns``) or removes a file the op created.

Nothing here deletes a save file except that rollback (D5 I9): a slot is only overwritten.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import safety, slotmap
from .errors import LiveChanged, RollbackError, WriteError
from .faults import FaultInjector, at
from .model import MemberWrite, Position

log = logging.getLogger(__name__)

#: ``os.replace`` onto a file another process holds open fails on Windows (the Steam client
#: or a virus scan can hold a save briefly), so each rename is retried this often.
RETRIES = 5
RETRY_DELAY = 0.2


@dataclass(frozen=True)
class FileState:
    sha256: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class SteamFileWrite:
    name: str                             # "save13.hg" / "mf_save13.hg"
    sha256: str
    mtime: float
    temp_name: str                        # ".tmp_<tag>_save13.hg.part" (in the target folder)


@dataclass(frozen=True)
class SteamWritePlan:
    folder: Path
    files: tuple[SteamFileWrite, ...]     # in swap order
    pre: Mapping[str, FileState | None]   # every name the op touches; None = absent
    op_tag: str                           # names the op's temporary files


def op_tag(op_id: str) -> str:
    """The short tag in an op's temporary file names: the op id's random suffix."""
    return op_id[-4:]


def file_names(slot: int, position: Position) -> tuple[str, str]:
    """(data, meta) file names of one position of a slot."""
    fno = slotmap.file_no(slot, slotmap.member_index(position))
    return slotmap.data_filename(fno), slotmap.meta_filename(fno)


def file_state(path: Path) -> FileState | None:
    try:
        st = path.stat()
        digest = safety.sha256_file(path)
    except FileNotFoundError:
        return None
    return FileState(digest, st.st_size, st.st_mtime_ns)


def payload_of(writes: Sequence[tuple[MemberWrite, bytes, bytes]], slot: int) -> dict[str, bytes]:
    """The bytes ``stage`` writes, by file name, from the same writes ``prepare`` took."""
    out: dict[str, bytes] = {}
    for mw, data, meta_disk in writes:
        data_name, meta_name = file_names(slot, mw.position)
        out[data_name], out[meta_name] = data, meta_disk
    return out


def prepare(folder: Path, writes: Sequence[tuple[MemberWrite, bytes, bytes]], slot: int,
            tag: str) -> SteamWritePlan:
    """Plan the files of one slot from (member write, data, meta file bytes). Reads only."""
    folder = Path(folder)
    if len({mw.position for mw, _d, _m in writes}) != len(writes):
        raise ValueError("a position is written once per operation")
    files: list[SteamFileWrite] = []
    # The member written latest (the loading one, stamped now) is swapped in last.
    for mw, data, meta_disk in sorted(writes, key=lambda w: w[0].disk_time):
        if _sha(data) != mw.vid or _sha(meta_disk) != mw.meta_disk_sha256:
            raise ValueError(f"the bytes for position {mw.position} are not the planned ones")
        for name, digest in zip(file_names(slot, mw.position), (mw.vid, mw.meta_disk_sha256)):
            files.append(SteamFileWrite(name, digest, mw.disk_time, f".tmp_{tag}_{name}.part"))
    pre = {f.name: file_state(folder / f.name) for f in files}
    return SteamWritePlan(folder, tuple(files), pre, tag)


def stage(plan: SteamWritePlan, payload: Mapping[str, bytes],
          faults: FaultInjector | None = None) -> None:
    """Write every new file under its temporary name, fsynced and read back. Nothing the
    game or Steam looks at changes. A failure removes what was staged."""
    try:
        for i, f in enumerate(plan.files, 1):
            data = payload.get(f.name)
            if data is None or _sha(data) != f.sha256:
                raise WriteError(f"the bytes to stage for {f.name} are not the planned ones")
            temp = plan.folder / f.temp_name
            with open(temp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if safety.sha256_file(temp) != f.sha256:
                raise WriteError(f"{f.name} did not read back as staged")
            at(faults, f"staged:{i}")
    except OSError as exc:
        sweep(plan.folder, plan.op_tag)
        raise WriteError(f"staging failed: {exc.strerror}") from exc
    except WriteError:
        sweep(plan.folder, plan.op_tag)
        raise


def swap(plan: SteamWritePlan, faults: FaultInjector | None = None) -> None:
    """Rename the staged files in, in plan order, each followed by its mtime. First the
    slot is checked against ``plan.pre``: when the game or another tool wrote it since the
    plan, raise :class:`LiveChanged` with nothing written (the staged files are removed).
    On a rename that keeps failing, raise :class:`WriteError` naming what was already
    swapped; the caller then rolls the slot back."""
    changed = [name for name, pre in plan.pre.items() if file_state(plan.folder / name) != pre]
    if changed:
        sweep(plan.folder, plan.op_tag)
        raise LiveChanged("the slot changed since it was planned: " + ", ".join(changed))
    swapped: list[str] = []
    for i, f in enumerate(plan.files, 1):
        target = plan.folder / f.name
        try:
            _replace(plan.folder / f.temp_name, target)
            swapped.append(f.name)
            os.utime(target, (f.mtime, f.mtime))
        except OSError as exc:
            raise WriteError(f"{f.name} could not be replaced: {exc.strerror}",
                             swapped=swapped) from exc
        at(faults, f"swapped:{i}")
    safety.fsync_dir(plan.folder)          # the renames are durable before the next event


def rollback(plan: SteamWritePlan, pre_bytes: Mapping[str, bytes]) -> None:
    """Put every name of the plan back as ``pre`` says: its pre-op bytes and ``mtime_ns``,
    or no file when there was none (a file this op created). ``pre_bytes`` needs only the
    names whose content changed; each is checked against its recorded sha256 before any
    file is touched, and when one is missing or wrong nothing is changed. The op's
    temporary files are removed. Raises :class:`RollbackError` if any name is not exactly
    as before afterwards."""
    problems: dict[str, str] = {}
    for name, pre in plan.pre.items():
        now = file_state(plan.folder / name)
        if pre is None or (now is not None and (now.sha256, now.size) == (pre.sha256, pre.size)):
            continue
        data = pre_bytes.get(name)
        if data is None:
            problems[name] = "no pre-op bytes"
        elif _sha(data) != pre.sha256:
            problems[name] = "the pre-op bytes do not match the recorded hash"
    if problems:
        raise RollbackError("nothing was changed, the slot cannot be put back: "
                            + "; ".join(f"{n}: {why}" for n, why in problems.items()))
    for name, pre in plan.pre.items():
        try:
            _restore(plan.folder / name, pre, pre_bytes.get(name), plan.op_tag)
        except (OSError, RollbackError) as exc:
            problems[name] = getattr(exc, "strerror", None) or str(exc)
    sweep(plan.folder, plan.op_tag)
    safety.fsync_dir(plan.folder)
    for name, pre in plan.pre.items():
        if name not in problems and file_state(plan.folder / name) != pre:
            problems[name] = "not as before"
    if problems:
        raise RollbackError("the slot could not be put back: "
                            + "; ".join(f"{n}: {why}" for n, why in problems.items()))


def sweep(folder: Path, tag: str) -> list[str]:
    """Remove the op's temporary files from ``folder``; returns their names."""
    removed = []
    for path in sorted(Path(folder).glob(f".tmp_{tag}_*.part")):
        if path.is_file():
            path.unlink()
            removed.append(path.name)
    return removed


def _restore(path: Path, pre: FileState | None, data: bytes | None, tag: str) -> None:
    now = file_state(path)
    if pre is None:
        if now is not None:
            path.unlink()
        return
    if now is None or (now.sha256, now.size) != (pre.sha256, pre.size):
        if data is None:
            raise RollbackError("no pre-op bytes")
        if _sha(data) != pre.sha256:
            raise RollbackError("the pre-op bytes do not match the recorded hash")
        safety.atomic_write_bytes(path, data, temp_prefix=f".tmp_{tag}_")
    if path.stat().st_mtime_ns != pre.mtime_ns:
        os.utime(path, ns=(pre.mtime_ns, pre.mtime_ns))


def _replace(src: Path, dst: Path) -> None:
    for attempt in range(RETRIES + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == RETRIES:
                raise
            log.info("%s is held open; retrying the rename", dst.name)
            time.sleep(RETRY_DELAY)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
