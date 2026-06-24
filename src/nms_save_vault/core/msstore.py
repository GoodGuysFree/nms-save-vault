"""Reader for Microsoft Store / Xbox Game Pass NMS saves (the "wgs" container format).

Layout (per libNOM.io's PlatformMicrosoft, verified against the spec):
    %LOCALAPPDATA%\\Packages\\HelloGames.NoMansSky_bs190hzg1sesy\\SystemAppData\\wgs\\
        <accountfolder>\\
            containers.index                 -- index of all saves (containers)
            <DIRGUID>\\                       -- one folder per save (GUID, "N" format)
                container.<n>                -- lists the data/meta blob GUIDs
                <DATAGUID>                   -- save data  (same 0xFEEDA1E5 stream as Steam)
                <METAGUID>                   -- meta (plaintext; NOT XXTEA like Steam)

Save identifiers map onto the same slot/member model used elsewhere:
    "Slot{N}Auto"   -> slot N, member A     (auto-save)
    "Slot{N}Manual" -> slot N, member B     (manual save / restore point)
    "AccountData", "Settings" -> account-level (not a slot)

This module is read-only. Current Xbox data blobs decompress with the shared
``lz4_block`` code; older variants (single LZ4 block, or the "HGSAVEV2" chunk format) are
also handled.
"""
from __future__ import annotations

import os
import struct
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import formats, lz4_block
from .meta import MetaInfo
from .savedir import MemberView, SaveDirView, SlotView
from .slotmap import SaveFileRef, file_no as _file_no

# containers.index / blob container constants
CONTAINERSINDEX_HEADER = 0xE
CONTAINERSINDEX_FOOTER = 0x10000000
BLOBCONTAINER_HEADER = 0x4
BLOB_IDENTIFIER_LENGTH = 0x80  # 128 bytes (UTF-16, 64 chars)
BLOBCONTAINER_TOTAL_LENGTH = 4 + 4 + 2 * (BLOB_IDENTIFIER_LENGTH + 2 * 0x10)  # 328
SYNC_STATE_DELETED = 3

HGSAVEV2_HEADER = b"HGSAVEV2\x00"

# MS meta field offsets (plaintext; differ from Steam)
MS_OFF_BASE_VERSION = 0x00   # u32
MS_OFF_GAME_MODE = 0x04      # u16
MS_OFF_SEASON = 0x06         # u16
MS_OFF_TOTAL_PLAY_TIME = 0x08  # u64
MS_OFF_SIZE = 0x10           # u32 (decompressed, current/old; compressed in the Omega..Worlds era)
MS_OFF_NAME = 0x14           # 128 bytes
MS_OFF_SUMMARY = 0x94        # 148 -> 0x94, 128 bytes
MS_OFF_DIFFICULTY = 0x114    # 276
MS_OFF_SLOT_ID = 0x118       # 280, u64 (Worlds)
MS_OFF_TIMESTAMP = 0x120     # 288, u32 (Worlds)
MS_OFF_META_FORMAT = 0x124   # 292, u32 (Worlds)


def microsoft_root() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    return Path(local) / "Packages" / "HelloGames.NoMansSky_bs190hzg1sesy" / "SystemAppData" / "wgs"


def find_microsoft_save_dirs() -> list[Path]:
    """Account folders under wgs that contain a containers.index."""
    root = microsoft_root()
    out: list[Path] = []
    if not root or not root.is_dir():
        return out
    if (root / "containers.index").is_file():
        out.append(root)
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "containers.index").is_file():
            out.append(child)
    return out


def is_microsoft_save_dir(path: str | Path) -> bool:
    return (Path(path) / "containers.index").is_file()


@dataclass
class _Container:
    identifier: str
    directory: Path
    sync_state: int
    extension: int
    last_write: float  # unix epoch seconds
    size_disk: int
    data_file: Path | None = None
    meta_file: Path | None = None


def _read_lp_string(b: bytes, off: int) -> tuple[str, int]:
    """Length-prefixed UTF-16LE string: [int char-count][chars]. Returns (str, new_off)."""
    n = struct.unpack_from("<i", b, off)[0]
    end = off + 4 + 2 * n
    return b[off + 4 : end].decode("utf-16-le"), end


def _guid_name(raw16: bytes) -> str:
    """16 GUID bytes -> .NET 'N' format (32 uppercase hex), matching on-disk blob names."""
    return uuid.UUID(bytes_le=bytes(raw16)).hex.upper()


def _filetime_to_unix(filetime: int) -> float:
    if filetime <= 0:
        return 0.0
    return filetime / 1e7 - 11644473600.0


def parse_containers_index(folder: Path) -> list[_Container]:
    folder = Path(folder)
    b = (folder / "containers.index").read_bytes()
    if struct.unpack_from("<i", b, 0)[0] != CONTAINERSINDEX_HEADER:
        raise ValueError("bad containers.index header")
    count = struct.unpack_from("<q", b, 4)[0]

    _proc, off = _read_lp_string(b, 12)          # process identifier
    # [last write 8][sync state 4] then account identifier
    _acct, off = _read_lp_string(b, off + 12)
    if struct.unpack_from("<q", b, off)[0] != CONTAINERSINDEX_FOOTER:
        raise ValueError("bad containers.index footer")
    off += 8

    containers: list[_Container] = []
    for _ in range(count):
        c, off = _parse_blob_container_index(b, off, folder)
        containers.append(c)
    return containers


def _parse_blob_container_index(b: bytes, off: int, folder: Path) -> tuple[_Container, int]:
    identifier, off = _read_lp_string(b, off)    # save identifier 1
    _second, off = _read_lp_string(b, off)       # save identifier 2 (unused since 5.50)
    _sync, off = _read_lp_string(b, off)         # sync hex
    # [ext 1][sync state 4][dir guid 16][last mod 8][empty 8][total size 8]
    extension = b[off]
    sync_state = struct.unpack_from("<i", b, off + 1)[0]
    dir_guid = _guid_name(b[off + 5 : off + 21])
    last_write = _filetime_to_unix(struct.unpack_from("<q", b, off + 21)[0])
    size_disk = struct.unpack_from("<q", b, off + 37)[0]
    off += 45

    c = _Container(
        identifier=identifier,
        directory=folder / dir_guid,
        sync_state=sync_state,
        extension=extension,
        last_write=last_write,
        size_disk=int(size_disk),
    )
    if c.directory.is_dir() and c.sync_state != SYNC_STATE_DELETED:
        _resolve_blobs(c)
    return c, off


def _resolve_blobs(c: _Container) -> None:
    preferred = c.directory / f"container.{c.extension}"
    candidates = [preferred] if preferred.is_file() else sorted(
        c.directory.glob("container.*"), key=lambda p: p.name, reverse=True
    )
    for cf in candidates:
        b = cf.read_bytes()
        if len(b) != BLOBCONTAINER_TOTAL_LENGTH or struct.unpack_from("<i", b, 0)[0] != BLOBCONTAINER_HEADER:
            continue
        n_blobs = struct.unpack_from("<i", b, 4)[0]
        off = 8
        for _ in range(n_blobs):
            ident = b[off : off + BLOB_IDENTIFIER_LENGTH].decode("utf-16-le").split("\x00", 1)[0]
            off += BLOB_IDENTIFIER_LENGTH
            # [cloud guid 16][local guid 16] -- the local (second) guid names the on-disk file
            local = b[off + 16 : off + 32]
            off += 32
            fpath = c.directory / _guid_name(local)
            if ident == "data":
                c.data_file = fpath
            elif ident == "meta":
                c.meta_file = fpath
        if c.data_file is not None and c.data_file.is_file():
            break


def _u(b: bytes, off: int, fmt: str, size: int) -> int:
    return struct.unpack_from(fmt, b, off)[0] if off + size <= len(b) else 0


def _ms_string(b: bytes, off: int, length: int = 128) -> str:
    # The meta SaveName/SaveSummary are UTF-8, NUL-terminated (same as Steam's meta).
    # (Only the containers.index strings are UTF-16.)
    if off >= len(b):
        return ""
    raw = b[off : off + length]
    nul = raw.find(b"\x00")
    if nul >= 0:
        raw = raw[:nul]
    return raw.decode("utf-8", errors="replace")


def parse_ms_meta(disk: bytes) -> dict:
    """Parse a Microsoft (plaintext) meta blob into the common field set."""
    L = len(disk)
    out = {
        "base_version": _u(disk, MS_OFF_BASE_VERSION, "<I", 4),
        "game_mode": _u(disk, MS_OFF_GAME_MODE, "<H", 2),
        "season": _u(disk, MS_OFF_SEASON, "<H", 2),
        "total_play_time": _u(disk, MS_OFF_TOTAL_PLAY_TIME, "<Q", 8),
        "size_field": _u(disk, MS_OFF_SIZE, "<I", 4),
        "name": "",
        "summary": "",
        "difficulty": 0,
        "slot_id": 0,
        "timestamp": 0,
        "meta_format": 0,
    }
    if out["season"] == 0xFFFF:
        out["season"] = 0
    if L >= formats.META_LEN_WAYPOINT:  # 280+: names present
        out["name"] = _ms_string(disk, MS_OFF_NAME)
        out["summary"] = _ms_string(disk, MS_OFF_SUMMARY)
    if L in (formats.META_LEN_WORLDS_I, formats.META_LEN_WORLDS_II):  # 296 / 360
        out["difficulty"] = _u(disk, MS_OFF_DIFFICULTY, "<I", 4)
        out["slot_id"] = _u(disk, MS_OFF_SLOT_ID, "<Q", 8)
        out["timestamp"] = _u(disk, MS_OFF_TIMESTAMP, "<I", 4)
        out["meta_format"] = _u(disk, MS_OFF_META_FORMAT, "<I", 4)
    elif L == formats.META_LEN_WAYPOINT:
        out["difficulty"] = disk[MS_OFF_DIFFICULTY] if MS_OFF_DIFFICULTY < L else 0
    return out


def _data_decompressed_size(data: bytes, fallback: int) -> int:
    """Total decompressed size from the data container, or the meta fallback."""
    try:
        if data[:4] == formats.SAVE_STREAMING_HEADER:
            return lz4_block.stats(data).total_decompressed
    except Exception:  # noqa: BLE001
        pass
    return fallback


def _empty_member(slot: int, member: int) -> MemberView:
    ref = SaveFileRef(_file_no(slot, member))
    return MemberView(ref=ref, data_path=Path(), meta_path=Path(), exists=False)


def _build_member(slot: int, member: int, c: _Container) -> MemberView:
    ref = SaveFileRef(_file_no(slot, member))
    data_file = c.data_file
    meta_file = c.meta_file
    exists = bool(data_file and data_file.is_file() and meta_file and meta_file.is_file())
    mv = MemberView(
        ref=ref,
        data_path=data_file or (c.directory / "data"),
        meta_path=meta_file or (c.directory / "meta"),
        exists=exists,
    )
    if not exists:
        mv.note = "Xbox save: blob missing" if c.sync_state != SYNC_STATE_DELETED else "Xbox save: deleted"
        return mv
    data = data_file.read_bytes()
    fields = parse_ms_meta(meta_file.read_bytes())
    mv.data_size = len(data)
    mv.data_mtime = c.last_write or data_file.stat().st_mtime
    mv.ordinal_used = None  # not Steam-keyed
    mv.info = MetaInfo(
        ordinal=ref.storage_ordinal,
        header=formats.META_HEADER,  # synthesised so MemberView.valid works for display
        meta_format=fields["meta_format"] or formats.META_FORMAT_4,
        size_decompressed=_data_decompressed_size(data, fields["size_field"]),
        size_disk=len(data),
        base_version=fields["base_version"],
        game_mode=fields["game_mode"],
        season=fields["season"],
        total_play_time=fields["total_play_time"],
        save_name=fields["name"],
        save_summary=fields["summary"],
        difficulty=fields["difficulty"],
        slot_identifier=fields["slot_id"],
        timestamp=fields["timestamp"],
    )
    mv.note = "Xbox/Microsoft save"
    return mv


def scan(folder: str | Path) -> SaveDirView:
    """Scan a Microsoft 'wgs' account folder into the shared SaveDirView model."""
    folder = Path(folder)
    slots = {k: SlotView(slot=k, a=_empty_member(k, 0), b=_empty_member(k, 1)) for k in range(1, formats.MAX_SAVE_SLOTS + 1)}
    account_present = False
    try:
        containers = parse_containers_index(folder)
    except Exception:  # noqa: BLE001 - return an empty view rather than raise during a scan
        return SaveDirView(path=folder, slots=slots, account_present=False)

    for c in containers:
        ident = c.identifier
        if ident == "AccountData":
            account_present = bool(c.data_file and c.data_file.is_file())
            continue
        if not ident.startswith("Slot"):
            continue
        digits = "".join(ch for ch in ident if ch.isdigit())
        if not digits:
            continue
        slot = int(digits)
        if not 1 <= slot <= formats.MAX_SAVE_SLOTS:
            continue
        member = 1 if ident.endswith("Manual") else 0  # Auto -> A, Manual -> B
        mv = _build_member(slot, member, c)
        if member == 0:
            slots[slot].a = mv
        else:
            slots[slot].b = mv

    return SaveDirView(path=folder, slots=slots, account_present=account_present)
