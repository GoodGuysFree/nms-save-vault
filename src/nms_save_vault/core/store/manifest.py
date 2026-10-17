"""Store entry manifests: one immutable, self-checking JSON file per store entry (D3 2.4).

Canonical form: ``json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)``
as ASCII, with no floats anywhere, so parsing and re-serializing reproduces the bytes.
``body_sha256`` is the sha256 of the canonical form of every other key. Keys this version does
not know (written by a newer app) are kept and written back unchanged, so the checksum holds.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Literal, get_args

from ..formats import MAX_SAVE_SLOTS
from ..organizer.model import Member, Platform
from .errors import EntryCorrupt
from .objects import is_digest, sha256

ENTRY_FORMAT = "nmsvault-entry"
ENTRY_SCHEMA = 1

EntryKind = Literal["full", "snapshot", "extract", "imported", "preserve"]
ENTRY_KINDS: tuple[str, ...] = get_args(EntryKind)
PLATFORMS: tuple[str, ...] = get_args(Platform)
MEMBERS: tuple[str, ...] = get_args(Member)
INDEX_KINDS = ("verbatim", "rebuilt")

MAX_ENTRY_ID = 128
_ENTRY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,%d}" % (MAX_ENTRY_ID - 1))
_PID = re.compile(r"[0-9A-F]{16}")
_BAD_PATH_CHARS = frozenset('\\:\0')

Json = dict[str, Any]


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def is_entry_id(value: object) -> bool:
    return isinstance(value, str) and _ENTRY_ID.fullmatch(value) is not None and ".." not in value


def is_safe_path(value: object) -> bool:
    """A posix path relative to the captured folder that cannot leave it."""
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    if any(c in _BAD_PATH_CHARS for c in value):
        return False
    return all(part not in ("", ".", "..") for part in value.split("/"))


@dataclass(frozen=True)
class FileEntry:
    path: str                    # posix, relative to the captured folder
    size: int
    sha256: str
    mtime_ns: int
    inline: bytes | None = None  # the payload when it is kept in the manifest
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> Json:
        out: Json = dict(self.extra)
        out.update({"path": self.path, "size": self.size, "sha256": self.sha256,
                    "mtime_ns": self.mtime_ns})
        if self.inline is not None:
            out["inline"] = base64.b64encode(self.inline).decode("ascii")
        return out

    @staticmethod
    def from_json(value: object) -> "FileEntry":
        d = _obj(value, "file")
        path = _get(d, "path", str, "file")
        if not is_safe_path(path):
            raise EntryCorrupt(f"file: unsafe path {path!r}")
        size, digest = _uint(d, "size", "file"), _get(d, "sha256", str, "file")
        if not is_digest(digest):
            raise EntryCorrupt(f"file {path}: 'sha256' is not a sha256")
        inline = None
        if "inline" in d:
            try:
                inline = base64.b64decode(_get(d, "inline", str, "file"), validate=True)
            except binascii.Error as exc:
                raise EntryCorrupt(f"file {path}: inline payload is not base64") from exc
            if len(inline) != size or sha256(inline) != digest:
                raise EntryCorrupt(f"file {path}: inline payload does not match its size and sha256")
        known = {"path", "size", "sha256", "mtime_ns", "inline"}
        return FileEntry(path, size, digest, _int(d, "mtime_ns", "file"), inline,
                         {k: v for k, v in d.items() if k not in known})


@dataclass(frozen=True)
class MemberRecord:
    slot: int
    member: Member
    data: str                    # FileEntry.path of the data file / blob
    meta: str
    vid: str
    ordinal: int | None          # Steam: the ordinal the meta decrypts with; Xbox: None
    meta_plain_sha256: str | None
    pid: str | None              # from the meta; D1 applies its fallback chain when None
    play_time: int
    meta_ts: int
    name: str
    summary: str
    difficulty: int
    game_mode: int
    season: int
    base_version: int
    meta_format: int
    size_decompressed: int
    valid: bool
    note: str = ""
    xbox_identifier: str | None = None
    file_time_ns: int = 0        # the data file's time as savedir reads it (Xbox: index time)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> Json:
        out: Json = dict(self.extra)
        out.update({
            "data": self.data, "meta": self.meta, "vid": self.vid, "ordinal": self.ordinal,
            "meta_plain_sha256": self.meta_plain_sha256, "pid": self.pid,
            "play_time": self.play_time, "meta_ts": self.meta_ts, "name": self.name,
            "summary": self.summary, "difficulty": self.difficulty, "game_mode": self.game_mode,
            "season": self.season, "base_version": self.base_version,
            "meta_format": self.meta_format, "size_decompressed": self.size_decompressed,
            "valid": self.valid, "note": self.note, "file_time_ns": self.file_time_ns})
        if self.xbox_identifier is not None:
            out["identifier"] = self.xbox_identifier
        return out

    @staticmethod
    def from_json(slot: int, member: str, value: object) -> "MemberRecord":
        what = f"slot {slot}{member}"
        d = _obj(value, what)
        vid = _get(d, "vid", str, what)
        if not is_digest(vid):
            raise EntryCorrupt(f"{what}: 'vid' is not a sha256")
        ordinal = _opt(d, "ordinal", int, what)
        if ordinal is not None and not 1 <= ordinal <= 2 * MAX_SAVE_SLOTS + 1:
            raise EntryCorrupt(f"{what}: ordinal {ordinal} out of range")
        meta_sha = _opt(d, "meta_plain_sha256", str, what)
        if meta_sha is not None and not is_digest(meta_sha):
            raise EntryCorrupt(f"{what}: 'meta_plain_sha256' is not a sha256")
        pid = _opt(d, "pid", str, what)
        if pid is not None and (not _PID.fullmatch(pid) or pid == "0" * 16):
            raise EntryCorrupt(f"{what}: 'pid' is not a playthrough id")
        known = {"data", "meta", "vid", "ordinal", "meta_plain_sha256", "pid", "play_time",
                 "meta_ts", "name", "summary", "difficulty", "game_mode", "season",
                 "base_version", "meta_format", "size_decompressed", "valid", "note",
                 "identifier", "file_time_ns"}
        return MemberRecord(
            slot=slot, member=member, data=_get(d, "data", str, what),  # type: ignore[arg-type]
            meta=_get(d, "meta", str, what), vid=vid, ordinal=ordinal, meta_plain_sha256=meta_sha,
            pid=pid, play_time=_uint(d, "play_time", what), meta_ts=_uint(d, "meta_ts", what),
            name=_get(d, "name", str, what), summary=_get(d, "summary", str, what),
            difficulty=_uint(d, "difficulty", what), game_mode=_uint(d, "game_mode", what),
            season=_uint(d, "season", what), base_version=_int(d, "base_version", what),
            meta_format=_uint(d, "meta_format", what),
            size_decompressed=_uint(d, "size_decompressed", what),
            valid=_get(d, "valid", bool, what), note=_get(d, "note", str, what),
            xbox_identifier=_opt(d, "identifier", str, what),
            file_time_ns=_int(d, "file_time_ns", what),
            extra={k: v for k, v in d.items() if k not in known})


_TOP_KEYS = ("format", "schema", "id", "kind", "label", "reason", "op_id", "created_utc",
             "created_local", "app_version", "platform", "account_key", "source", "scope",
             "origin", "files", "slots", "account", "xbox", "notes")


@dataclass(frozen=True)
class StoreEntry:
    id: str
    kind: EntryKind
    label: str
    reason: str
    op_id: str | None
    created_utc: str
    created_local: str
    app_version: str
    platform: Platform
    account_key: str | None
    source: Mapping[str, Any]
    scope: Mapping[str, Any]
    origin: Mapping[str, Any] | None
    files: tuple[FileEntry, ...]
    members: tuple[MemberRecord, ...]
    account: Mapping[str, str]
    xbox: Mapping[str, Any] | None
    body_sha256: str
    notes: tuple[str, ...] = ()  # capture diagnostics, e.g. a save file kept without its partner
    schema: int = ENTRY_SCHEMA   # a newer writer's schema number is kept as read
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, **fields: Any) -> "StoreEntry":
        """Build an entry, sorting its files and members and computing ``body_sha256``. The
        result is the entry parsed back from its own bytes by the strict loader, so the store
        never commits a manifest it could not load (raises EntryCorrupt instead)."""
        fields["files"] = tuple(sorted(fields["files"], key=lambda f: f.path))
        fields["members"] = tuple(sorted(fields["members"], key=lambda m: (m.slot, m.member)))
        fields["notes"] = tuple(fields.get("notes", ()))
        draft = cls(body_sha256="", **fields)
        entry = cls(body_sha256=sha256(canonical(draft.body())), **fields)
        return StoreEntry.from_bytes(entry.to_bytes())

    def body(self) -> Json:
        """Every key of the manifest except ``body_sha256``."""
        slots: dict[str, dict[str, Json]] = {}
        for m in self.members:
            slots.setdefault(str(m.slot), {})[m.member] = m.to_json()
        body: Json = dict(self.extra)
        body.update({
            "format": ENTRY_FORMAT, "schema": self.schema, "id": self.id, "kind": self.kind,
            "label": self.label, "reason": self.reason, "op_id": self.op_id,
            "created_utc": self.created_utc, "created_local": self.created_local,
            "app_version": self.app_version, "platform": self.platform,
            "account_key": self.account_key, "source": dict(self.source),
            "scope": dict(self.scope), "origin": None if self.origin is None else dict(self.origin),
            "files": [f.to_json() for f in self.files], "slots": slots,
            "account": dict(self.account), "xbox": None if self.xbox is None else dict(self.xbox),
            "notes": list(self.notes)})
        return body

    def to_bytes(self) -> bytes:
        body = self.body()
        if sha256(canonical(body)) != self.body_sha256:
            raise ValueError(f"entry {self.id}: body_sha256 does not match its content")
        return canonical(dict(body, body_sha256=self.body_sha256))

    @staticmethod
    def from_bytes(raw: bytes) -> "StoreEntry":
        """Parse and check a manifest; raises EntryCorrupt on anything wrong with it."""
        try:
            obj = json.loads(raw.decode("ascii"), parse_float=_no_float, parse_constant=_no_float,
                             object_pairs_hook=_no_duplicates)
        except (UnicodeDecodeError, ValueError) as exc:
            raise EntryCorrupt(f"manifest does not parse: {exc}") from exc
        d = _obj(obj, "manifest")
        if canonical(d) != raw:
            raise EntryCorrupt("manifest is not in canonical form")
        claimed = d.pop("body_sha256", None)
        if not isinstance(claimed, str) or sha256(canonical(d)) != claimed:
            raise EntryCorrupt("manifest checksum does not match")
        if d.get("format") != ENTRY_FORMAT:
            raise EntryCorrupt("not an entry manifest")
        schema = d.get("schema")
        if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
            raise EntryCorrupt("manifest schema is not a version number")
        what = "manifest"
        entry_id = _get(d, "id", str, what)
        if not is_entry_id(entry_id):
            raise EntryCorrupt(f"bad entry id {entry_id!r}")
        files = tuple(FileEntry.from_json(f) for f in _get(d, "files", list, what))
        members = []
        for slot_key, by_member in _obj(_get(d, "slots", dict, what), "slots").items():
            if not slot_key.isdigit() or not 1 <= int(slot_key) <= MAX_SAVE_SLOTS \
                    or str(int(slot_key)) != slot_key:
                raise EntryCorrupt(f"bad slot key {slot_key!r}")
            for member, rec in _obj(by_member, f"slot {slot_key}").items():
                if member not in MEMBERS:
                    raise EntryCorrupt(f"bad member key {member!r}")
                members.append(MemberRecord.from_json(int(slot_key), member, rec))
        entry = StoreEntry(
            id=entry_id, kind=_choice(d, "kind", ENTRY_KINDS, what),
            label=_get(d, "label", str, what), reason=_get(d, "reason", str, what),
            op_id=_opt(d, "op_id", str, what), created_utc=_get(d, "created_utc", str, what),
            created_local=_get(d, "created_local", str, what),
            app_version=_get(d, "app_version", str, what),
            platform=_choice(d, "platform", PLATFORMS, what),
            account_key=_opt(d, "account_key", str, what),
            source=_get(d, "source", dict, what), scope=_get(d, "scope", dict, what),
            origin=_opt(d, "origin", dict, what), files=files,
            members=tuple(sorted(members, key=lambda m: (m.slot, m.member))),
            account=_get(d, "account", dict, what), xbox=_opt(d, "xbox", dict, what),
            body_sha256=claimed, notes=_notes(d), schema=schema,
            extra={k: v for k, v in d.items() if k not in _TOP_KEYS})
        _check(entry)
        return entry

    # --- lookups ----------------------------------------------------------------------------

    @cached_property
    def _by_path(self) -> dict[str, FileEntry]:
        return {f.path: f for f in self.files}

    def file(self, path: str) -> FileEntry:
        try:
            return self._by_path[path]
        except KeyError:
            raise KeyError(f"entry {self.id} has no file {path!r}") from None

    def has_file(self, path: str) -> bool:
        return path in self._by_path

    def member(self, slot: int, member: Member) -> MemberRecord | None:
        return next((m for m in self.members if m.slot == slot and m.member == member), None)

    def object_digests(self) -> frozenset[str]:
        """The objects this entry references (its non-inline files)."""
        return frozenset(f.sha256 for f in self.files if f.inline is None)

    @property
    def slots(self) -> tuple[int, ...]:
        return tuple(sorted({m.slot for m in self.members}))


def _check(entry: StoreEntry) -> None:
    """Cross-field rules every entry obeys."""
    paths = [f.path for f in entry.files]
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        raise EntryCorrupt(f"entry {entry.id}: files are not sorted and unique")
    seen = set()
    for m in entry.members:
        if (m.slot, m.member) in seen:
            raise EntryCorrupt(f"entry {entry.id}: slot {m.slot}{m.member} twice")
        seen.add((m.slot, m.member))
        if not (entry.has_file(m.data) and entry.has_file(m.meta)):
            raise EntryCorrupt(f"entry {entry.id}: slot {m.slot}{m.member} names a file it lacks")
        if entry.file(m.data).sha256 != m.vid:
            raise EntryCorrupt(f"entry {entry.id}: slot {m.slot}{m.member} vid is not its data hash")
    for key, path in entry.account.items():
        if not isinstance(path, str) or not entry.has_file(path):
            raise EntryCorrupt(f"entry {entry.id}: account file {key!r} is not in the entry")
    if entry.xbox is not None:
        index, kind = entry.xbox.get("index"), entry.xbox.get("index_kind")
        if not isinstance(index, str) or not entry.has_file(index) or kind not in INDEX_KINDS:
            raise EntryCorrupt(f"entry {entry.id}: bad xbox index record")


# --- validation helpers -----------------------------------------------------------------------


def _notes(d: Mapping[str, Any]) -> tuple[str, ...]:
    notes = _get(d, "notes", list, "manifest")
    if not all(isinstance(n, str) for n in notes):
        raise EntryCorrupt("manifest: a note is not text")
    return tuple(notes)


def _no_float(text: str) -> Any:
    raise ValueError(f"a float or constant ({text}) has no place in a manifest")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _obj(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EntryCorrupt(f"{what}: expected an object")
    return value


def _get(d: Mapping[str, Any], key: str, kind: type, what: str) -> Any:
    if key not in d:
        raise EntryCorrupt(f"{what}: missing {key!r}")
    value = d[key]
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise EntryCorrupt(f"{what}: {key!r} is not a {kind.__name__}")
    return value


def _opt(d: Mapping[str, Any], key: str, kind: type, what: str) -> Any:
    if d.get(key) is None:
        return None
    return _get(d, key, kind, what)


def _int(d: Mapping[str, Any], key: str, what: str) -> int:
    return _get(d, key, int, what)


def _uint(d: Mapping[str, Any], key: str, what: str) -> int:
    value = _get(d, key, int, what)
    if value < 0:
        raise EntryCorrupt(f"{what}: {key!r} is negative")
    return value


def _choice(d: Mapping[str, Any], key: str, choices: tuple[str, ...], what: str) -> Any:
    value = _get(d, key, str, what)
    if value not in choices:
        raise EntryCorrupt(f"{what}: {key!r} is not one of {list(choices)}")
    return value
