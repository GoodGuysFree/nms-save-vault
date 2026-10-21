"""Identifiers and value types of the save-file organizer, with their JSON codecs.

Pure: no file access, no clock unless one is passed, and nothing from the organizer store,
so the version store and the live operations can build on it. The JSON forms are the ones
the organizer writes (journal records, scan cache): plain objects, lists, strings and ints,
with short keys for the meta fields every record repeats. Decoders validate everything and
raise :class:`ModelError` on anything malformed.
"""
from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, get_args

from ..formats import GOG_ACCOUNT_DIR, MAX_SAVE_SLOTS

Pid = str   # playthrough id: one line of the organizer (pid_kind lists the forms)
Vid = str   # version id: lower-case hex sha256 of one member's data file as stored
Platform = Literal["steam", "xbox"]
Member = Literal["A", "B"]           # A = auto-save (file 2k-1), B = restore point (file 2k)
PairKey = tuple[Vid | None, Vid | None]
ObsOrder = tuple[int, int]           # (at, seq): replay order of the journal

PidKind = Literal["id", "legacy", "unknown", "split"]
PidSource = Literal["meta", "data", "legacy", "unknown", "owner"]
TsSource = Literal["meta", "index", "mtime"]
TimeQuality = Literal["exact", "estimated"]
CaptureSrc = Literal["scan", "entry", "op"]
EntryKind = Literal["live", "full", "snapshot", "extract", "imported", "inplace", "preserve"]
EdgeTier = Literal["recorded", "observed", "owner"]
EdgeKind = Literal["play", "touch", "edit", "unknown"]
EdgeMethod = Literal["op", "oplog", "journal", "capture-sequence", "owner"]
EventKind = Literal["first-seen", "changed", "deleted", "partial", "reverted", "confirmed",
                    "unexpected", "resurrected", "returned", "replaced", "unstable"]

EMPTY_PAIR: PairKey = (None, None)

_ID_PID = re.compile(r"[0-9A-F]{16}")
_LEGACY_PID = re.compile(r"legacy:(steam|xbox):([1-9][0-9]?):(.*)", re.DOTALL)
_UNKNOWN_PID = re.compile(r"unknown:[0-9a-f]{16}")
_SPLIT_N = re.compile(r"[1-9][0-9]*")
_VID = re.compile(r"[0-9a-f]{64}")
_INSTANCE = re.compile(r"(?:[0-9a-f]{8})?")
_OP_ID = re.compile(r"op-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4}|legacy-[0-9]+")


class ModelError(ValueError):
    """Malformed organizer data: an id, or a JSON record that does not decode."""


# --- identifiers --------------------------------------------------------------


def pid_from_id(value: int) -> Pid:
    """The pid of a non-zero ``SaveUniversalId`` (the u64 a meta or the save data carries)."""
    if not 0 < value < 1 << 64:
        raise ModelError(f"not a playthrough id: {value}")
    return f"{value:016X}"


def legacy_pid(platform: Platform, slot: int, name: str) -> Pid:
    """A save with no id anywhere (pre-Waypoint). Fixed when its version is first registered,
    so the save keeps its line after a move to another slot. An unnamed save ends in ``:``,
    which no named save can."""
    _check_platform(platform)
    _check_slot(slot)
    return f"legacy:{platform}:{slot}:{name}"


def unknown_pid(vid: Vid) -> Pid:
    """A save whose meta is unreadable and whose data holds no id."""
    _check_vid(vid)
    return f"unknown:{vid[:16]}"


def split_pid(raw: Pid, n: int) -> Pid:
    """The n-th line the owner split off ``raw``."""
    if pid_kind(raw) == "split" or not _is_uint(n) or n < 1:
        raise ModelError(f"cannot split {raw!r} as #{n}")
    return f"split:{raw}:{n}"


def pid_kind(pid: str) -> PidKind:
    """Which form ``pid`` has; raises :class:`ModelError` when it has none."""
    if isinstance(pid, str):
        if _ID_PID.fullmatch(pid) and pid != "0" * 16:   # 0 means "no id" in a meta
            return "id"
        legacy = _LEGACY_PID.fullmatch(pid)
        if legacy and int(legacy[2]) <= MAX_SAVE_SLOTS:
            return "legacy"
        if _UNKNOWN_PID.fullmatch(pid):
            return "unknown"
        if pid.startswith("split:"):
            raw, _, n = pid[len("split:"):].rpartition(":")
            if _SPLIT_N.fullmatch(n) and not raw.startswith("split:") and is_pid(raw):
                return "split"
    raise ModelError(f"not a playthrough id: {pid!r}")


def is_pid(value: object) -> bool:
    try:
        pid_kind(value)  # type: ignore[arg-type]
    except ModelError:
        return False
    return True


def split_source(pid: Pid) -> Pid | None:
    """The raw pid a split line came from; None for any other pid."""
    if pid_kind(pid) != "split":
        return None
    return pid[len("split:"):].rpartition(":")[0]


def is_identified(pid: Pid) -> bool:
    """True when the line rests on the game's own id (a split of such a line included)."""
    return pid_kind(split_source(pid) or pid) == "id"


def pids_compatible(a: Pid, b: Pid) -> bool:
    """May a version of ``b`` descend from one of ``a``? The same pid, or two legacy pids with
    the same platform and name (a pre-Waypoint save moved to another slot). Unnamed saves
    have nothing in common but the slot, so two of them match only as the same pid."""
    if a == b:
        return True
    la, lb = _LEGACY_PID.fullmatch(a), _LEGACY_PID.fullmatch(b)
    return bool(la and lb and la[3]) and (la[1], la[3]) == (lb[1], lb[3])


def is_vid(value: object) -> bool:
    return isinstance(value, str) and _VID.fullmatch(value) is not None


def new_op_id(clock: Callable[[], float] = time.time) -> str:
    """``op-<UTC yyyymmddThhmmssZ>-<4 hex>``: one per user-level operation."""
    return _stamped_id("op", clock())


def new_correction_id(clock: Callable[[], float] = time.time) -> str:
    """``cor-<UTC yyyymmddThhmmssZ>-<4 hex>``: one per owner correction."""
    return _stamped_id("cor", clock())


def is_op_id(value: object) -> bool:
    """An app operation id, or ``legacy-<n>`` for a line of the old oplog."""
    return isinstance(value, str) and _OP_ID.fullmatch(value) is not None


def _stamped_id(prefix: str, at: float) -> str:
    stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{secrets.token_hex(2)}"


# --- value types --------------------------------------------------------------


@dataclass(frozen=True)
class LiveRef:
    """A live account folder."""

    platform: Platform
    account_key: str    # the folder's name (st_<steamid64> or the Xbox account); never shown raw
    instance: str = ""  # "" normally; sha1(resolved parent path)[:8] only when two live folders
                        # of one platform share a name on this machine (two Proton prefixes)

    def target(self, slot: int) -> LiveTarget:
        return LiveTarget(self.platform, self.account_key, slot, self.instance)


@dataclass(frozen=True)
class EntryRef:
    """A catalog entry folder, of any kind."""

    entry_id: str


@dataclass(frozen=True)
class StoreRef:
    """A version-store entry; a folded catalog entry keeps its id as the key."""

    key: str


SourceRef = LiveRef | EntryRef | StoreRef


@dataclass(frozen=True)
class LiveTarget:
    """One live slot: where a write lands and what the journal observes."""

    platform: Platform
    account_key: str
    slot: int           # 1..15
    instance: str = ""

    @property
    def ref(self) -> LiveRef:
        return LiveRef(self.platform, self.account_key, self.instance)


def platform_name(platform: str, account_key: str = "") -> str:
    """A live account's platform as the owner knows it: "Xbox", "Steam", or "GOG" for the
    Steam-format folder GOG writes (formats.GOG_ACCOUNT_DIR)."""
    if platform == "xbox":
        return "Xbox"
    return "GOG" if account_key == GOG_ACCOUNT_DIR else "Steam"


def platform_letter(platform: str, account_key: str = "") -> str:
    """The first letter of :func:`platform_name`: a live slot's badge reads S7, G7 or X7."""
    return platform_name(platform, account_key)[0]


def sync_name(platform: str, account_key: str = "") -> str:
    """What can put a save back into a live folder behind the app's back."""
    return {"Xbox": "Xbox sync", "Steam": "Steam Cloud"}.get(
        platform_name(platform, account_key), "Cloud sync")


@dataclass(frozen=True)
class MetaSnap:
    """What the organizer keeps of a meta: enough to show a version whose bytes are gone."""

    play_time: int      # seconds
    meta_ts: int        # save time, UTC unix: the meta's own (Steam 0x164, Xbox 0x120), else
                        # the Xbox index or file time as ts_src says; 0 when none is known
    ts_src: TsSource
    name: str
    summary: str
    difficulty: int     # raw preset id (formats.DIFFICULTY_NAMES)
    game_mode: int      # 6 = Seasonal (expedition)
    season: int
    base_version: int
    meta_format: int
    size_decompressed: int
    data_size: int
    meta_sha: str       # sha256 hex of the meta plaintext; "" when unreadable. One vid can have
                        # several metas (a Place re-stamps the time, F5b; promote edits it)
    valid: bool         # the meta decoded with the right header (savedir MemberView.valid)
    file_time: float    # the data file's mtime (Xbox: its containers.index time) exactly as
                        # savedir reads it; orders a slot's members when one lacks a meta time


@dataclass(frozen=True)
class MemberObs:
    member: Member
    vid: Vid
    pid: Pid
    pid_src: PidSource
    platform: Platform
    meta: MetaSnap
    xbox_sync: int | None = None    # containers.index sync state (Xbox live only)


@dataclass(frozen=True)
class Capture:
    """One observation of one slot; an empty slot is an observation too."""

    where: SourceRef
    slot: int
    a: MemberObs | None
    b: MemberObs | None
    observed_at: int    # UTC unix
    time_quality: TimeQuality
    src: CaptureSrc
    head_member: Member | None   # the member the game loads: savedir's SlotView.newest when
                                 # the slot was read, else head_of(a, b); None when empty

    @property
    def pair(self) -> PairKey:
        return (self.a.vid if self.a else None, self.b.vid if self.b else None)

    @property
    def head(self) -> MemberObs | None:
        return {"A": self.a, "B": self.b}.get(self.head_member)  # type: ignore[arg-type]

    @property
    def members(self) -> tuple[MemberObs, ...]:
        return tuple(m for m in (self.a, self.b) if m is not None)


@dataclass(frozen=True)
class RawLocation:
    """Where the scanner saw a vid."""

    where: SourceRef
    slot: int
    member: Member
    entry_kind: EntryKind
    managed: bool       # catalog entry flag; True for store entries; False for live and in-place
    seen_at: int        # capture time (UTC)


@dataclass(frozen=True)
class VersionInfo:
    """Registry entry, one per vid (derived; rebuilt every refresh)."""

    vid: Vid
    raw_pid: Pid
    pid_src: PidSource
    platform: Platform
    meta: MetaSnap                      # from the earliest observation
    meta_ts_last: int                   # newest meta_ts seen (promote / re-stamp raise it)
    roles: frozenset[Member]            # seen as A, B or both
    first_seen: ObsOrder
    locations: tuple[RawLocation, ...]  # where the bytes are now; empty = gone


@dataclass(frozen=True)
class PairInfo:
    pair: PairKey
    head: Vid
    head_member: Member
    partner: Vid | None
    raw_pid: Pid
    first_seen: ObsOrder
    seen_in: tuple[tuple[SourceRef, int], ...]  # (where, slot) of every capture of this pair


@dataclass(frozen=True)
class Edge:
    """Game-state descent: the child's state was produced from the parent's. Always evidence;
    nothing is inferred."""

    parent: Vid
    child: Vid
    tier: EdgeTier
    kind: EdgeKind
    method: EdgeMethod
    op_id: str | None   # the operation that put `parent` into the slot (binding), if any
    created: int        # UTC unix of the evidence (observation or op time; correction time)
    target: LiveTarget | None = None    # slot where the step was seen (journal, capture-sequence)


@dataclass(frozen=True)
class JournalEvent:
    kind: EventKind
    target: LiveTarget
    at: int
    op_id: str | None
    before: PairKey | None
    after: PairKey | None
    vids: tuple[Vid, ...] = ()   # the members the event is about


# --- rules on the values ------------------------------------------------------


def head_of(a: MemberObs | None, b: MemberObs | None) -> MemberObs | None:
    """The member the game loads, for data read back without its slot: the same rule as
    ``savedir.SlotView.newest``. Members with a valid meta are the candidates (else every
    present one); one clock orders them all - the meta save time when every candidate has
    one, else the file time; a tie goes to A. Play time plays no part."""
    present = [m for m in (a, b) if m is not None]
    candidates = [m for m in present if m.meta.valid] or present
    if not candidates:
        return None
    if all(m.meta.ts_src == "meta" for m in candidates):
        return max(candidates, key=lambda m: m.meta.meta_ts)   # max keeps the first: A
    return max(candidates, key=lambda m: m.meta.file_time)


def edge_kind(parent: MetaSnap, child: MetaSnap) -> EdgeKind:
    """What one step of descent was, from the meta deltas (the bytes are known to differ).
    Save times are compared only on one clock: a meta time against a file time says nothing."""
    if child.play_time > parent.play_time:
        return "play"
    if child.play_time == parent.play_time and child.ts_src == parent.ts_src:
        if child.meta_ts > parent.meta_ts:
            return "touch"      # the game re-saved after a load
        if child.meta_ts == parent.meta_ts:
            return "edit"       # changed outside the game
    return "unknown"            # only an owner correction can link these


def version_order(info: VersionInfo) -> tuple[int, int, ObsOrder, Vid]:
    """The strict total order of versions wherever a "before/after" is needed."""
    return (info.meta.meta_ts, info.meta.play_time, info.first_seen, info.vid)


def pair_str(pair: PairKey) -> str:
    """``<A or ->/<B or ->``."""
    return "/".join(v or "-" for v in pair)


def pair_from_str(text: str) -> PairKey:
    parts = text.split("/")
    if len(parts) != 2:
        raise ModelError(f"not a pair: {text!r}")
    return _pair_from_list([None if p == "-" else p for p in parts], "pair")


# --- JSON codecs --------------------------------------------------------------


def target_to_json(t: LiveTarget) -> dict[str, Any]:
    return {"platform": t.platform, "account": t.account_key, "instance": t.instance,
            "slot": t.slot}


def target_from_json(value: object) -> LiveTarget:
    d = _obj(value, "target")
    return LiveTarget(_platform(d, "target"), _name(d, "account", "target"), _slot(d, "target"),
                      _instance(d, "target"))


def source_to_json(ref: SourceRef) -> dict[str, Any]:
    if isinstance(ref, LiveRef):
        return {"kind": "live", "platform": ref.platform, "account": ref.account_key,
                "instance": ref.instance}
    if isinstance(ref, EntryRef):
        return {"kind": "entry", "entry": ref.entry_id}
    return {"kind": "store", "entry": ref.key}


def source_from_json(value: object) -> SourceRef:
    d = _obj(value, "source")
    kind = _choice(d, "kind", ("live", "entry", "store"), "source")
    if kind == "live":
        return LiveRef(_platform(d, "source"), _name(d, "account", "source"),
                       _instance(d, "source"))
    entry = _name(d, "entry", "source")
    return EntryRef(entry) if kind == "entry" else StoreRef(entry)


def pair_to_json(pair: PairKey) -> list[Vid | None]:
    return list(pair)


def pair_from_json(value: object) -> PairKey:
    if not isinstance(value, list):
        raise ModelError("pair: expected a list")
    return _pair_from_list(value, "pair")


def meta_to_json(m: MetaSnap) -> dict[str, Any]:
    return {"pt": m.play_time, "ts": m.meta_ts, "ts_src": m.ts_src, "name": m.name,
            "summary": m.summary, "diff": m.difficulty, "mode": m.game_mode,
            "season": m.season, "bv": m.base_version, "fmt": m.meta_format,
            "dsize": m.size_decompressed, "size": m.data_size, "msha": m.meta_sha,
            "valid": m.valid, "ft": m.file_time}


def meta_from_json(value: object, what: str = "meta") -> MetaSnap:
    d = _obj(value, what)
    meta_sha = _get(d, "msha", str, what)
    if meta_sha and not is_vid(meta_sha):     # the same lower-case sha256 hex form
        raise ModelError(f"{what}: 'msha' is not a sha256")
    return MetaSnap(
        play_time=_uint(d, "pt", what), meta_ts=_uint(d, "ts", what),
        ts_src=_choice(d, "ts_src", _TS_SOURCES, what),
        name=_get(d, "name", str, what), summary=_get(d, "summary", str, what),
        difficulty=_uint(d, "diff", what), game_mode=_uint(d, "mode", what),
        season=_uint(d, "season", what), base_version=_uint(d, "bv", what),
        meta_format=_uint(d, "fmt", what), size_decompressed=_uint(d, "dsize", what),
        data_size=_uint(d, "size", what), meta_sha=meta_sha,
        valid=_get(d, "valid", bool, what), file_time=_file_time(d, what),
    )


def member_to_json(m: MemberObs, *, with_vid: bool = True) -> dict[str, Any]:
    """A member observation. The journal keys its members by vid, so it leaves ``vid`` out."""
    out: dict[str, Any] = {"vid": m.vid} if with_vid else {}
    out.update({"member": m.member, "pid": m.pid, "pid_src": m.pid_src, "plat": m.platform})
    out.update(meta_to_json(m.meta))
    if m.xbox_sync is not None:
        out["sync"] = m.xbox_sync
    return out


def member_from_json(value: object, *, vid: Vid | None = None) -> MemberObs:
    """Decode a member; ``vid`` is given when the record keys members by vid."""
    d = _obj(value, "member")
    if vid is None:
        vid = _get(d, "vid", str, "member")
    elif d.get("vid", vid) != vid:
        raise ModelError(f"member: 'vid' disagrees with its key {vid!r}")
    _check_vid(vid)
    pid = _get(d, "pid", str, "member")
    if not is_pid(pid):
        raise ModelError(f"member: not a playthrough id: {pid!r}")
    pid_src = _choice(d, "pid_src", _PID_SOURCES, "member")
    if pid_src not in _PID_SOURCES_OF_KIND[pid_kind(pid)]:
        raise ModelError(f"member: pid_src {pid_src!r} does not fit {pid!r}")
    sync = d.get("sync")
    if sync is not None and not _is_uint(sync):
        raise ModelError("member: 'sync' is not a sync state")
    return MemberObs(
        member=_choice(d, "member", _MEMBERS, "member"), vid=vid, pid=pid, pid_src=pid_src,
        platform=_choice(d, "plat", _PLATFORMS, "member"),
        meta=meta_from_json(d, "member"), xbox_sync=sync,
    )


def capture_to_json(c: Capture) -> dict[str, Any]:
    return {"where": source_to_json(c.where), "slot": c.slot,
            "a": member_to_json(c.a) if c.a else None,
            "b": member_to_json(c.b) if c.b else None,
            "at": c.observed_at, "q": c.time_quality, "src": c.src, "head": c.head_member}


def capture_from_json(value: object) -> Capture:
    d = _obj(value, "capture")
    where = source_from_json(_get(d, "where", dict, "capture"))
    members: dict[str, MemberObs] = {}
    for key, expected in (("a", "A"), ("b", "B")):
        if key not in d:
            raise ModelError(f"capture: missing {key!r}")
        if d[key] is None:
            continue
        m = member_from_json(d[key])
        if m.member != expected:
            raise ModelError(f"capture: member {m.member} stored as {key!r}")
        if isinstance(where, LiveRef) and m.platform != where.platform:
            raise ModelError(f"capture: a {m.platform} save in a {where.platform} folder")
        members[expected] = m
    if "head" not in d:
        raise ModelError("capture: missing 'head'")
    head = d["head"]
    if (head is None) != (not members) or (head is not None and head not in members):
        raise ModelError(f"capture: head {head!r} is not a member of the slot")
    return Capture(
        where=where, slot=_slot(d, "capture"), a=members.get("A"), b=members.get("B"),
        observed_at=_uint(d, "at", "capture"),
        time_quality=_choice(d, "q", _TIME_QUALITIES, "capture"),
        src=_choice(d, "src", _CAPTURE_SRCS, "capture"), head_member=head,
    )


# --- validation helpers -------------------------------------------------------

_PLATFORMS = frozenset(get_args(Platform))
_MEMBERS = frozenset(get_args(Member))
_PID_SOURCES = frozenset(get_args(PidSource))
_TS_SOURCES = frozenset(get_args(TsSource))
_TIME_QUALITIES = frozenset(get_args(TimeQuality))
_CAPTURE_SRCS = frozenset(get_args(CaptureSrc))
_PID_SOURCES_OF_KIND: dict[str, frozenset[str]] = {
    "id": frozenset({"meta", "data"}), "legacy": frozenset({"legacy"}),
    "unknown": frozenset({"unknown"}), "split": frozenset({"owner"}),
}


def _obj(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ModelError(f"{what}: expected an object")
    return value


def _get(d: Mapping[str, Any], key: str, kind: type, what: str) -> Any:
    if key not in d:
        raise ModelError(f"{what}: missing {key!r}")
    value = d[key]
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ModelError(f"{what}: {key!r} is not a {kind.__name__}")
    return value


def _is_uint(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _uint(d: Mapping[str, Any], key: str, what: str) -> int:
    value = _get(d, key, int, what)
    if value < 0:
        raise ModelError(f"{what}: {key!r} is negative")
    return value


def _choice(d: Mapping[str, Any], key: str, choices: Any, what: str) -> Any:
    value = _get(d, key, str, what)
    if value not in choices:
        raise ModelError(f"{what}: {key!r} is not one of {sorted(choices)}")
    return value


def _platform(d: Mapping[str, Any], what: str) -> Platform:
    return _choice(d, "platform", _PLATFORMS, what)


def _slot(d: Mapping[str, Any], what: str) -> int:
    slot = _get(d, "slot", int, what)
    _check_slot(slot)
    return slot


def _name(d: Mapping[str, Any], key: str, what: str) -> str:
    """A folder or entry name: used to find a folder, so never empty and never a path (no
    separator, drive or stream colon, NUL, ``.`` or ``..``)."""
    value = _get(d, key, str, what)
    if value in ("", ".", "..") or any(c in value for c in "/\\:\0"):
        raise ModelError(f"{what}: {key!r} is not a folder name")
    return value


def _instance(d: Mapping[str, Any], what: str) -> str:
    value = _get(d, "instance", str, what)
    if not _INSTANCE.fullmatch(value):
        raise ModelError(f"{what}: 'instance' is not '' or 8 hex digits")
    return value


def _file_time(d: Mapping[str, Any], what: str) -> float:
    value = d.get("ft")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value >= 0:
        raise ModelError(f"{what}: 'ft' is not a file time")
    return float(value)


def _pair_from_list(items: list, what: str) -> PairKey:
    if len(items) != 2:
        raise ModelError(f"{what}: expected two members")
    for v in items:
        if v is not None:
            _check_vid(v)
    return (items[0], items[1])


def _check_vid(vid: object) -> None:
    if not is_vid(vid):
        raise ModelError(f"not a version id: {vid!r}")


def _check_platform(platform: object) -> None:
    if platform not in _PLATFORMS:
        raise ModelError(f"not a platform: {platform!r}")


def _check_slot(slot: object) -> None:
    if not (isinstance(slot, int) and not isinstance(slot, bool) and 1 <= slot <= MAX_SAVE_SLOTS):
        raise ModelError(f"not a slot: {slot!r}")
