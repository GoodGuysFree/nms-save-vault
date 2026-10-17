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

Since 2026 the game also keeps a plain mirror beside wgs, with the same blob bytes:
    ...\\SystemAppData\\xgs\\<accountfolder>\\<identifier>\\{data,meta}
Every wgs write here is mirrored into it (when the game has created it), so the two never
disagree about a save the vault changed. An account with xgs but no wgs is read-only.

Current Xbox data blobs decompress with the shared
``lz4_block`` code; older variants (single LZ4 block, or the "HGSAVEV2" chunk format) are
also handled.

Attribution: the wgs/containers.index format and offsets were learned from libNOM.io's
``PlatformMicrosoft`` by Christian Engelhardt (zencq),
https://github.com/zencq/libNOM.io, licensed GPL-3.0. See LICENSE and README.md credits.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import formats, lz4_block, safety
from .meta import MetaInfo
from .savedir import MemberView, SaveDirView, SlotView
from .slotmap import SaveFileRef, file_no as _file_no

# containers.index / blob container constants
CONTAINERSINDEX_HEADER = 0xE
CONTAINERSINDEX_FOOTER = 0x10000000
BLOBCONTAINER_HEADER = 0x4
BLOB_IDENTIFIER_LENGTH = 0x80  # 128 bytes (UTF-16, 64 chars)
BLOBCONTAINER_TOTAL_LENGTH = 4 + 4 + 2 * (BLOB_IDENTIFIER_LENGTH + 2 * 0x10)  # 328
# Each containers.index record ends in [ext 1][sync state 4][dir guid 16][last write 8]
# [reserved 8][total size 8], after its three length-prefixed strings.
RECORD_TAIL_LENGTH = 45

# MicrosoftBlobSyncStateEnum (per save) and MicrosoftIndexSyncStateEnum (whole index).
BLOB_SYNC_SYNCED = 1
BLOB_SYNC_MODIFIED = 2
SYNC_STATE_DELETED = 3          # == MicrosoftBlobSyncStateEnum.Deleted
BLOB_SYNC_CREATED = 5
INDEX_SYNC_MODIFIED = 2

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

# MS meta lengths (libNOM.io PlatformMicrosoft.cs:22-25). Not the Steam ones in ``formats``:
# the plaintext Microsoft meta has no header or hash area, so every era is shorter.
MS_META_LEN_WAYPOINT = 0x118   # 280: names, difficulty as a single byte
MS_META_LEN_WORLDS_I = 0x128   # 296: + u32 difficulty, playthrough id, save time, format
MS_META_LEN_WORLDS_II = 0x168  # 360: + 64-byte difficulty tag


def microsoft_root() -> Path | None:
    # The wgs store is a Windows package layout with no Linux or macOS equivalent, so this
    # is gated on the OS rather than on LOCALAPPDATA happening to be unset there (D3).
    if os.name != "nt":
        return None
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    return Path(local) / "Packages" / "HelloGames.NoMansSky_bs190hzg1sesy" / "SystemAppData" / "wgs"


def find_microsoft_save_dirs() -> list[Path]:
    """Account folders under wgs that contain a containers.index, plus any xgs account
    folder that has no wgs twin (read-only fallback)."""
    root = microsoft_root()
    out: list[Path] = []
    if not root:
        return out
    if root.is_dir():
        if (root / "containers.index").is_file():
            out.append(root)
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "containers.index").is_file():
                out.append(child)
    xgs_root = root.parent / "xgs"
    if xgs_root.is_dir():
        for child in sorted(xgs_root.iterdir()):
            if is_xgs_save_dir(child) and not is_microsoft_save_dir(root / child.name):
                out.append(child)
    return out


def is_microsoft_save_dir(path: str | Path) -> bool:
    return (Path(path) / "containers.index").is_file()


def is_xgs_save_dir(path: str | Path) -> bool:
    """An xgs account folder: under ``xgs`` and holding at least one ``<identifier>/data``."""
    p = Path(path)
    return (
        p.parent.name.lower() == "xgs"
        and p.is_dir()
        and any((c / "data").is_file() for c in p.iterdir() if c.is_dir())
    )


def xgs_twin(folder: str | Path) -> Path | None:
    """The xgs mirror of a wgs account folder, or None if the game has not created one."""
    folder = Path(folder)
    if folder.parent.name.lower() != "wgs":
        return None
    twin = folder.parent.parent / "xgs" / folder.name
    return twin if twin.is_dir() else None


def _slot_member(identifier: str) -> tuple[int, int] | None:
    """``"Slot{N}Auto"`` -> (N, 0), ``"Slot{N}Manual"`` -> (N, 1); None for anything else."""
    m = re.fullmatch(r"Slot(\d+)(Auto|Manual)", identifier)
    if not m or not 1 <= int(m.group(1)) <= formats.MAX_SAVE_SLOTS:
        return None
    return int(m.group(1)), 0 if m.group(2) == "Auto" else 1


@dataclass
class XboxBlobInfo:
    """The wgs on-disk identity of one save, captured read-only so a future writer can
    rewrite it in place (see the Xbox read-write study). ``None`` on Steam members."""

    identifier: str              # e.g. "Slot3Manual" / "AccountData"
    directory: Path              # <wgs>/<DIRGUID>
    dir_guid: str                # 32-hex "N" form of the save's directory GUID
    extension: int               # current container.<n> number
    sync_state: int              # MicrosoftBlobSyncStateEnum: 1=Synced 2=Modified 3=Deleted 5=Created
    sync_time: str               # the index "sync hex" string (preserved verbatim)
    has_second_identifier: bool  # identifier written twice (pre-Worlds-5.50)
    last_write: float            # unix epoch seconds (from the index FILETIME)
    blob_container_file: Path | None = None  # the container.<n> file actually read
    data_local_guid: str | None = None       # 32-hex names of the on-disk blob files
    meta_local_guid: str | None = None
    data_cloud_guid: str | None = None        # cloud GUIDs preserved inside container.<n>
    meta_cloud_guid: str | None = None


@dataclass
class XboxIndexInfo:
    """containers.index header: account/process identity and global state. ``None`` for
    Steam folders."""

    process_id: str
    account_id: str
    sync_state: int
    filetime: int                # the index's last write as stored (100 ns ticks since 1601)
    container_count: int
    trailer: bytes = b""         # any bytes after the last record, written back verbatim

    @property
    def last_write(self) -> float:
        """``filetime`` as unix epoch seconds."""
        return _filetime_to_unix(self.filetime)


@dataclass
class _Container:
    identifier: str
    directory: Path
    sync_state: int
    extension: int
    # The record's last write as stored (100 ns ticks since 1601): a float of seconds cannot
    # hold it, and rewriting from one moved other saves' times. Required, so no record is
    # ever written with an accidental 0 (= 1601).
    filetime: int
    size_disk: int
    data_file: Path | None = None
    meta_file: Path | None = None
    # --- retained for a future writer (non-lossy scan) ---
    dir_guid: str = ""
    second_identifier: str = ""
    sync_time: str = ""
    blob_container_file: Path | None = None
    data_local_guid: str | None = None
    meta_local_guid: str | None = None
    data_cloud_guid: str | None = None
    meta_cloud_guid: str | None = None
    # --- the record as stored, so an untouched record is written back byte for byte ---
    reserved: bytes = bytes(8)
    raw: bytes = b""
    parsed: tuple = field(default=(), repr=False, compare=False)

    @property
    def last_write(self) -> float:
        """``filetime`` as unix epoch seconds."""
        return _filetime_to_unix(self.filetime)


def _record_fields(c: _Container) -> tuple:
    """Everything a record serialises; a record whose fields still equal what was parsed is
    written back as its raw bytes."""
    return (
        c.identifier, c.second_identifier, c.sync_time, c.extension, c.sync_state,
        c.dir_guid, c.filetime, c.reserved, c.size_disk,
    )


def _read_lp_string(b: bytes, off: int) -> tuple[str, int]:
    """Length-prefixed UTF-16LE string: [int char-count][chars]. Returns (str, new_off).
    Raises ``ValueError`` when the count is negative or runs past the end of ``b``."""
    if not 0 <= off <= len(b) - 4:
        raise ValueError(f"containers.index: no string length at offset {off}, past the end")
    n = struct.unpack_from("<i", b, off)[0]
    end = off + 4 + 2 * n
    if n < 0 or end > len(b):
        raise ValueError(f"containers.index: the string at offset {off} claims an impossible length {n}")
    return b[off + 4 : end].decode("utf-16-le"), end


def _guid_name(raw16: bytes) -> str:
    """16 GUID bytes -> .NET 'N' format (32 uppercase hex), matching on-disk blob names."""
    return uuid.UUID(bytes_le=bytes(raw16)).hex.upper()


def _filetime_to_unix(filetime: int) -> float:
    if filetime <= 0:
        return 0.0
    return filetime / 1e7 - 11644473600.0


def split_index(raw: bytes) -> tuple[bytes, list[tuple[str, bytes]], bytes]:
    """Cut a ``containers.index`` into its header (the bytes after the record count, up to
    the first record), each record's raw bytes keyed by identifier, and any trailing bytes.
    Decodes only what it takes to find the boundaries; :func:`join_index` puts the pieces
    back byte for byte."""
    if len(raw) < 12 or struct.unpack_from("<i", raw, 0)[0] != CONTAINERSINDEX_HEADER:
        raise ValueError("bad containers.index header")
    count = struct.unpack_from("<q", raw, 4)[0]
    # A record is at least three empty strings and its fixed tail.
    if not 0 <= count <= (len(raw) - 12) // (3 * 4 + RECORD_TAIL_LENGTH):
        raise ValueError(f"containers.index claims {count} records, impossible for its size")
    _proc, off = _read_lp_string(raw, 12)
    _acct, off = _read_lp_string(raw, off + 12)  # after [last write 8][sync state 4]
    if off + 8 > len(raw) or struct.unpack_from("<q", raw, off)[0] != CONTAINERSINDEX_FOOTER:
        raise ValueError("bad containers.index footer")
    off += 8
    header = raw[12:off]
    records: list[tuple[str, bytes]] = []
    for _ in range(count):
        start = off
        identifier, off = _read_lp_string(raw, off)
        _second, off = _read_lp_string(raw, off)
        _sync, off = _read_lp_string(raw, off)
        off += RECORD_TAIL_LENGTH
        if off > len(raw):
            raise ValueError("containers.index ends inside a record")
        records.append((identifier, raw[start:off]))
    # Bytes past the last record are not described by the format; they are kept as they are.
    return header, records, raw[off:]


def join_index(header: bytes, records: list[tuple[str, bytes]], trailer: bytes = b"") -> bytes:
    """Inverse of :func:`split_index`; the record count is taken from ``records``."""
    out = struct.pack("<i", CONTAINERSINDEX_HEADER) + struct.pack("<q", len(records)) + header
    return out + b"".join(rec for _identifier, rec in records) + trailer


def parse_containers_index(folder: Path) -> tuple[XboxIndexInfo, list[_Container]]:
    folder = Path(folder)
    header, records, trailer = split_index((folder / "containers.index").read_bytes())
    proc, off = _read_lp_string(header, 0)       # process identifier
    # [last write 8][sync state 4] then account identifier
    index_filetime = struct.unpack_from("<q", header, off)[0]
    index_sync_state = struct.unpack_from("<i", header, off + 8)[0]
    acct, _off = _read_lp_string(header, off + 12)

    containers = [_parse_record(rec, folder) for _identifier, rec in records]
    info = XboxIndexInfo(
        process_id=proc,
        account_id=acct,
        sync_state=index_sync_state,
        filetime=index_filetime,
        container_count=len(records),
        trailer=trailer,
    )
    return info, containers


def _parse_record(rec: bytes, folder: Path) -> _Container:
    identifier, off = _read_lp_string(rec, 0)    # save identifier 1
    second, off = _read_lp_string(rec, off)      # save identifier 2 (unused since 5.50)
    sync, off = _read_lp_string(rec, off)        # sync hex
    # [ext 1][sync state 4][dir guid 16][last mod 8][reserved 8][total size 8]
    extension = rec[off]
    sync_state = struct.unpack_from("<i", rec, off + 1)[0]
    dir_guid = _guid_name(rec[off + 5 : off + 21])
    filetime = struct.unpack_from("<q", rec, off + 21)[0]
    size_disk = struct.unpack_from("<q", rec, off + 37)[0]

    c = _Container(
        identifier=identifier,
        directory=folder / dir_guid,
        sync_state=sync_state,
        extension=extension,
        filetime=filetime,
        size_disk=int(size_disk),
        dir_guid=dir_guid,
        second_identifier=second,
        sync_time=sync,
        reserved=rec[off + 29 : off + 37],
        raw=rec,
    )
    c.parsed = _record_fields(c)
    if c.directory.is_dir() and c.sync_state != SYNC_STATE_DELETED:
        _resolve_blobs(c)
    return c


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
            cloud_name = _guid_name(b[off : off + 16])
            local_name = _guid_name(b[off + 16 : off + 32])
            off += 32
            fpath = c.directory / local_name
            if ident == "data":
                c.data_file, c.data_local_guid, c.data_cloud_guid = fpath, local_name, cloud_name
            elif ident == "meta":
                c.meta_file, c.meta_local_guid, c.meta_cloud_guid = fpath, local_name, cloud_name
        if c.data_file is not None and c.data_file.is_file():
            c.blob_container_file = cf
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


def _has_save_time(meta_len: int) -> bool:
    """Worlds-era layout (difficulty u32, playthrough id, save time, format): 296 or 360
    bytes, and anything longer, which is taken to extend the 360-byte layout."""
    return meta_len == MS_META_LEN_WORLDS_I or meta_len >= MS_META_LEN_WORLDS_II


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
    if L >= MS_META_LEN_WAYPOINT:
        out["name"] = _ms_string(disk, MS_OFF_NAME)
        out["summary"] = _ms_string(disk, MS_OFF_SUMMARY)
    if _has_save_time(L):
        out["difficulty"] = _u(disk, MS_OFF_DIFFICULTY, "<I", 4)
        out["slot_id"] = _u(disk, MS_OFF_SLOT_ID, "<Q", 8)
        out["timestamp"] = _u(disk, MS_OFF_TIMESTAMP, "<I", 4)
        out["meta_format"] = _u(disk, MS_OFF_META_FORMAT, "<I", 4)
    elif L == MS_META_LEN_WAYPOINT:
        out["difficulty"] = disk[MS_OFF_DIFFICULTY]
    return out


def set_ms_meta_timestamp(meta_bytes: bytes, ts: int) -> bytes:
    """Set the save time (u32 at 0x120) of a Worlds-era MS meta (296 or 360 bytes).

    Raises ``ValueError`` for any other length: only these two layouts are known well enough
    to write into (a longer meta is still read, see :func:`parse_ms_meta`), and handing the
    input back unchanged would let promote report a change it never made.
    """
    if len(meta_bytes) not in (MS_META_LEN_WORLDS_I, MS_META_LEN_WORLDS_II):
        raise ValueError(f"an Xbox meta of {len(meta_bytes)} bytes has no save time to set")
    b = bytearray(meta_bytes)
    struct.pack_into("<I", b, MS_OFF_TIMESTAMP, int(ts) & 0xFFFFFFFF)
    return bytes(b)


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


def _xbox_info(c: _Container) -> XboxBlobInfo:
    return XboxBlobInfo(
        identifier=c.identifier,
        directory=c.directory,
        dir_guid=c.dir_guid,
        extension=c.extension,
        sync_state=c.sync_state,
        sync_time=c.sync_time,
        has_second_identifier=bool(c.second_identifier),
        last_write=c.last_write,
        blob_container_file=c.blob_container_file,
        data_local_guid=c.data_local_guid,
        meta_local_guid=c.meta_local_guid,
        data_cloud_guid=c.data_cloud_guid,
        meta_cloud_guid=c.meta_cloud_guid,
    )


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
    mv.xbox = _xbox_info(c)  # retained on every Xbox member (present, missing, or deleted)
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
        index_info, containers = parse_containers_index(folder)
    except Exception:  # noqa: BLE001 - return an empty view rather than raise during a scan
        return SaveDirView(path=folder, slots=slots, account_present=False)

    for c in containers:
        if c.identifier == "AccountData":
            account_present = bool(c.data_file and c.data_file.is_file())
            continue
        _place_member(slots, c)

    return SaveDirView(path=folder, slots=slots, account_present=account_present, xbox_index=index_info)


def _place_member(slots: dict[int, SlotView], c: _Container) -> MemberView | None:
    parsed = _slot_member(c.identifier)
    if parsed is None:
        return None
    slot, member = parsed
    mv = _build_member(slot, member, c)
    if member == 0:
        slots[slot].a = mv
    else:
        slots[slot].b = mv
    return mv


def scan_xgs(folder: str | Path) -> SaveDirView:
    """Scan an xgs account folder (no containers.index) into the shared model. Read-only:
    members carry no ``xbox`` identity, so the wgs write paths refuse them. With no index
    FILETIME, saves whose meta has no timestamp fall back to the data file's mtime."""
    folder = Path(folder)
    slots = {k: SlotView(slot=k, a=_empty_member(k, 0), b=_empty_member(k, 1)) for k in range(1, formats.MAX_SAVE_SLOTS + 1)}
    for d in sorted(p for p in folder.iterdir() if p.is_dir()):
        c = _Container(
            identifier=d.name, directory=d, sync_state=0, extension=0, filetime=0,
            size_disk=0, data_file=d / "data", meta_file=d / "meta",
        )
        mv = _place_member(slots, c)
        if mv is not None:
            mv.xbox = None
            if mv.exists:
                mv.note = "Xbox save (xgs, read-only)"
    account_present = (folder / "AccountData" / "data").is_file()
    return SaveDirView(path=folder, slots=slots, account_present=account_present)


# --- serialization (inverse of the parsers; groundwork for the wgs writer) --------------
#
# These rebuild the exact byte layouts the readers above consume, so a future Xbox writer
# can round-trip a save back to disk. Pure functions, no I/O; verified by round-trip tests.


def _build_lp_string(s: str) -> bytes:
    """Inverse of ``_read_lp_string``: [int UTF-16 code-unit count][UTF-16LE chars].

    The count is code units, NOT Python code points: an astral char (e.g. an emoji) is one
    ``str`` element but two UTF-16 units, and the reader consumes ``2 * count`` bytes."""
    data = s.encode("utf-16-le")
    return struct.pack("<i", len(data) // 2) + data


def _guid_bytes_from_name(name: str) -> bytes:
    """Inverse of ``_guid_name``: a 32-hex 'N' string -> 16 GUID bytes (little-endian)."""
    return uuid.UUID(hex=name).bytes_le


def _unix_to_filetime(unix: float) -> int:
    """Inverse of ``_filetime_to_unix`` (Windows FILETIME: 100ns ticks since 1601-01-01)."""
    if unix <= 0:
        return 0
    return int(round((unix + 11644473600.0) * 1e7))


def _fixed_utf16(s: str, total: int = BLOB_IDENTIFIER_LENGTH) -> bytes:
    """A UTF-16LE string in a fixed-size, NUL-padded field (the blob identifiers)."""
    raw = s.encode("utf-16-le")
    if len(raw) > total:
        raise ValueError(f"{s!r} encodes to {len(raw)} bytes, exceeds the {total}-byte field")
    return raw + b"\x00" * (total - len(raw))


def build_blob_container(data_local: str, meta_local: str, data_cloud: str = "", meta_cloud: str = "") -> bytes:
    """Serialize a ``container.<n>`` blob-container file (328 bytes): the data/meta blob
    identifiers and their cloud + local GUIDs. The *local* GUID names the on-disk blob."""

    def _guid(name: str) -> bytes:
        return _guid_bytes_from_name(name) if name else b"\x00" * 16

    out = struct.pack("<ii", BLOBCONTAINER_HEADER, 2)
    out += _fixed_utf16("data") + _guid(data_cloud) + _guid(data_local)
    out += _fixed_utf16("meta") + _guid(meta_cloud) + _guid(meta_local)
    if len(out) != BLOBCONTAINER_TOTAL_LENGTH:  # invariant guard (never expected to fire)
        raise ValueError(f"blob container is {len(out)} bytes, expected {BLOBCONTAINER_TOTAL_LENGTH}")
    return out


def _build_container_record(c: _Container) -> bytes:
    """One ``containers.index`` record: the raw bytes it was read from when nothing in it has
    changed, else serialised from its fields (FILETIME as the stored integer)."""
    if c.raw and _record_fields(c) == c.parsed:
        return c.raw
    out = _build_lp_string(c.identifier)
    out += _build_lp_string(c.second_identifier)
    out += _build_lp_string(c.sync_time)
    out += bytes([c.extension & 0xFF])
    out += struct.pack("<i", c.sync_state)
    out += _guid_bytes_from_name(c.dir_guid)
    out += struct.pack("<q", c.filetime)
    out += c.reserved
    out += struct.pack("<q", int(c.size_disk))
    return out


def build_containers_index(info: XboxIndexInfo, records: list[_Container]) -> bytes:
    """Serialize a complete ``containers.index``: header from ``info``, then the records.

    Records the caller did not change go back byte for byte (P-X1: rewriting them from
    floats moved other saves' FILETIMEs and perturbed their sync). The record ``count`` is
    taken from ``records``, so adding or removing a save stays consistent."""
    header = _build_lp_string(info.process_id)
    header += struct.pack("<q", info.filetime)
    header += struct.pack("<i", info.sync_state)
    header += _build_lp_string(info.account_id)
    header += struct.pack("<q", CONTAINERSINDEX_FOOTER)
    return join_index(
        header, [(c.identifier, _build_container_record(c)) for c in records], info.trailer
    )


# --- writer (wgs save mutation; Phase 2b) ----------------------------------------------
#
# Mutating a wgs save mirrors libNOM's PlatformMicrosoft: write the data + meta blobs under
# freshly-rotated GUIDs, write the next container.<n>, rewrite containers.index LAST (with
# sync states bumped so the Xbox app/game picks up the change), then delete the superseded
# files. Per-file writes are atomic; the operations layer snapshots first for crash/undo
# recovery. No cross-platform conversion here -- data/meta bytes are written verbatim.


def _new_guid_name() -> str:
    """A fresh random GUID in the 32-hex uppercase 'N' form used for on-disk blob names."""
    return uuid.uuid4().hex.upper()


def _new_container(folder: Path, identifier: str, when: float, *, double_identifier: bool = False) -> _Container:
    """An index record for a brand-new save: fresh directory GUID, Created sync state.

    ``double_identifier`` mirrors the tree's convention: pre-Worlds-5.50 saves write the
    identifier twice in containers.index, 5.50+ writes it once."""
    dir_name = _new_guid_name()
    return _Container(
        identifier=identifier,
        directory=Path(folder) / dir_name,
        sync_state=BLOB_SYNC_CREATED,
        extension=0,            # _stage_new_blobs bumps to 1 on the first write
        filetime=_unix_to_filetime(when),
        size_disk=0,
        dir_guid=dir_name,
        second_identifier=identifier if double_identifier else "",
        sync_time="",
    )


def _stage_new_blobs(c: _Container, data_bytes: bytes, meta_bytes: bytes, when: float) -> list[Path]:
    """Write new data/meta blobs + the next ``container.<n>`` for ``c`` and point ``c`` at
    them. Returns the now-superseded files to delete *after* the index is rewritten, so a
    crash before the index write leaves the old, still-referenced files intact."""
    superseded = [p for p in (c.data_file, c.meta_file, c.blob_container_file) if p is not None]

    new_ext = (c.extension % 255) + 1            # 0->1->2..->255->1 (never 0 after first write)
    data_local = _new_guid_name()
    meta_local = _new_guid_name()
    cdir = c.directory

    safety.atomic_write_bytes(cdir / data_local, data_bytes)   # mkdir's the dir as needed
    safety.atomic_write_bytes(cdir / meta_local, meta_bytes)
    container_file = cdir / f"container.{new_ext}"
    safety.atomic_write_bytes(
        container_file,
        build_blob_container(data_local, meta_local, c.data_cloud_guid or "", c.meta_cloud_guid or ""),
    )
    if when > 0:                                 # when<=0 is the "unknown time" sentinel; leave fs mtime
        safety.set_file_mtime(cdir / data_local, when)  # match the game stamping blob mtimes
        safety.set_file_mtime(cdir / meta_local, when)

    c.extension = new_ext
    c.data_local_guid, c.meta_local_guid = data_local, meta_local
    c.data_file, c.meta_file = cdir / data_local, cdir / meta_local
    c.blob_container_file = container_file
    c.size_disk = len(data_bytes) + len(meta_bytes)

    # Belt-and-suspenders: fresh random GUIDs never collide with the old paths, but make
    # sure we never schedule a just-written file for deletion.
    keep = {c.data_file, c.meta_file, container_file}
    return [p for p in superseded if p not in keep]


def _require_time(when: float) -> None:
    if when <= 0:  # a 0 FILETIME reads as 1601-01-01
        raise ValueError(f"a save write needs a real time, not {when!r}")


def _stamp(c: _Container, when: float, created: bool) -> None:
    """Set a changed record's time to ``when``, but never back from what it was: a record
    moving back in time looks older than a copy the sync already has."""
    c.filetime = max(_unix_to_filetime(when), 0 if created else c.filetime + 1)


def _write_containers_index_file(folder: Path, info: XboxIndexInfo, records: list[_Container]) -> None:
    safety.atomic_write_bytes(Path(folder) / "containers.index", build_containers_index(info, records))


def write_save(
    folder: str | Path,
    identifier: str,
    data_bytes: bytes,
    meta_bytes: bytes,
    when: float,
    *,
    create_if_missing: bool = False,
) -> None:
    """Write ``data_bytes`` + ``meta_bytes`` for the save named ``identifier`` (e.g.
    ``"Slot3Manual"``) into a wgs account ``folder``: rotate blob GUIDs, write the next
    ``container.<n>``, then rewrite ``containers.index`` with bumped sync states and
    ``when`` (unix seconds) as the last-write time. Set ``create_if_missing`` to allocate a
    new container directory for a save that does not exist yet. Bytes are written verbatim
    (no Steam<->Xbox conversion). Raises ``ValueError`` if the save is missing and
    ``create_if_missing`` is False."""
    folder = Path(folder)
    _require_time(when)
    info, containers = parse_containers_index(folder)
    target = next((c for c in containers if c.identifier == identifier), None)
    created = target is None
    if target is None:
        if not create_if_missing:
            raise ValueError(f"no save '{identifier}' in {folder} (pass create_if_missing to add it)")
        # Mirror the tree's identifier convention (pre-5.50 saves write the id twice).
        double_id = any(c.second_identifier for c in containers)
        target = _new_container(folder, identifier, when, double_identifier=double_id)
        containers.append(target)

    stale = _stage_new_blobs(target, data_bytes, meta_bytes, when)

    _stamp(target, when, created)
    if target.sync_state != BLOB_SYNC_CREATED:
        # Synced/Deleted/Modified all become Modified on a content write; a brand-new save
        # stays Created. Writing into a Deleted record must un-delete it, not leave it
        # flagged for the Xbox app's cloud garbage-collection.
        target.sync_state = BLOB_SYNC_MODIFIED
    info.sync_state = INDEX_SYNC_MODIFIED
    info.filetime = max(info.filetime, target.filetime)

    _write_containers_index_file(folder, info, containers)

    for p in stale:                              # old blobs/container, now unreferenced
        try:
            p.unlink()
        except OSError:
            pass

    twin = xgs_twin(folder)
    if twin is not None:
        _xgs_write(twin, identifier, data_bytes, meta_bytes)


def delete_save(folder: str | Path, identifier: str, when: float) -> bool:
    """Remove the save named ``identifier`` (e.g. ``"Slot3Manual"``) from a wgs account
    ``folder``. Returns False if there is no such live save.

    Mirrors what the Xbox app itself does: the ``containers.index`` record is KEPT and
    flagged ``Deleted`` -- dropping the record outright would leave the cloud copy with no
    instruction to remove it -- and only then are the blob files discarded. Index first, so
    a crash mid-delete leaves an index that no longer claims blobs that are already gone.
    ``write_save`` un-deletes such a record, so the slot can be refilled later.
    """
    folder = Path(folder)
    _require_time(when)
    info, containers = parse_containers_index(folder)
    target = next((c for c in containers if c.identifier == identifier), None)
    if target is None or target.sync_state == SYNC_STATE_DELETED:
        return False

    blob_dir = target.directory
    target.sync_state = SYNC_STATE_DELETED
    _stamp(target, when, created=False)
    target.size_disk = 0
    target.data_file = target.meta_file = target.blob_container_file = None
    target.data_local_guid = target.meta_local_guid = None
    info.sync_state = INDEX_SYNC_MODIFIED
    info.filetime = max(info.filetime, target.filetime)
    _write_containers_index_file(folder, info, containers)

    # Only ever a GUID-named child of the account folder; never recurse anywhere else.
    if blob_dir.is_dir() and blob_dir.parent == folder:
        shutil.rmtree(blob_dir, ignore_errors=True)

    twin = xgs_twin(folder)
    if twin is not None:
        _xgs_delete(twin, identifier)
    return True


def mark_modified(folder: str | Path) -> None:
    """Flip every Synced record (and the global index) to Modified so the Xbox app re-syncs
    a tree that was restored/edited wholesale (e.g. a full Xbox restore)."""
    folder = Path(folder)
    info, containers = parse_containers_index(folder)
    for c in containers:
        if c.sync_state == BLOB_SYNC_SYNCED:
            c.sync_state = BLOB_SYNC_MODIFIED
    info.sync_state = INDEX_SYNC_MODIFIED
    _write_containers_index_file(folder, info, containers)


# --- xgs mirror --------------------------------------------------------------------------
#
# xgs holds the same data/meta bytes as wgs under plain <identifier>/{data,meta} names. The
# game writes it (seen for AccountData) and may load from it, so a wgs-only write could
# leave the game a stale copy. Writes go wgs first (the vault's snapshot covers it), then xgs.


def _xgs_identifier_dir(twin: Path, identifier: str) -> Path:
    # The identifier comes from containers.index; it becomes a path component here.
    if not re.fullmatch(r"[A-Za-z0-9]+", identifier):
        raise ValueError(f"refusing unexpected save identifier {identifier!r} as an xgs folder name")
    return twin / identifier


def _xgs_write(twin: Path, identifier: str, data_bytes: bytes, meta_bytes: bytes) -> None:
    d = _xgs_identifier_dir(twin, identifier)
    for name, payload in (("data", data_bytes), ("meta", meta_bytes)):
        safety.atomic_write_bytes(d / name, payload)
        if (d / name).read_bytes() != payload:
            raise ValueError(f"xgs {identifier}/{name} does not match what was written")


def _xgs_delete(twin: Path, identifier: str) -> None:
    d = _xgs_identifier_dir(twin, identifier)
    if d.is_dir():
        shutil.rmtree(d)


def mirror_xgs(folder: str | Path) -> list[str]:
    """Make the xgs mirror match every save in a wgs account ``folder`` (after a wholesale
    restore). Only differing saves are rewritten; slot folders with no live wgs save are
    removed; AccountData and anything unrecognised is never deleted. Returns what changed."""
    folder = Path(folder)
    twin = xgs_twin(folder)
    if twin is None:
        return []
    _, containers = parse_containers_index(folder)
    live = {
        c.identifier: c
        for c in containers
        if c.sync_state != SYNC_STATE_DELETED
        and c.data_file is not None and c.data_file.is_file()
        and c.meta_file is not None and c.meta_file.is_file()
    }
    changed: list[str] = []
    for ident, c in live.items():
        data_bytes, meta_bytes = c.data_file.read_bytes(), c.meta_file.read_bytes()
        d = _xgs_identifier_dir(twin, ident)
        current = [(d / n).read_bytes() if (d / n).is_file() else None for n in ("data", "meta")]
        if current != [data_bytes, meta_bytes]:
            _xgs_write(twin, ident, data_bytes, meta_bytes)
            changed.append(f"xgs/{ident}")
    for d in sorted(twin.iterdir()):
        if d.is_dir() and _slot_member(d.name) is not None and d.name not in live:
            _xgs_delete(twin, d.name)
            changed.append(f"-xgs/{d.name}")
    return changed
