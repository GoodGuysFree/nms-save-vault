"""The version a live operation writes (D5 2.1 "source pair"): its two captured members,
read back verbatim and checked against their version ids, with the meta each was captured
with, its playthrough id, and where it came from.

Three readers: a live slot (another slot of the same platform, or the target itself for a
promote), a folder the vault does not know (the classic adapter, D5 4.1), and everything
else through D1's locator (the version store first).
"""
from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import aliases, meta, msstore, playtime, savedir, slotmap
from ..organizer.locator import Location, VersionUnavailable
from ..organizer.model import (
    EntryRef,
    LiveRef,
    ModelError,
    Platform,
    StoreRef,
    legacy_pid,
    pid_from_id,
    unknown_pid,
)
from ..organizer.scanner import live_refs
from .context import OpContext
from .errors import SourceUnreadable
from .model import PairSource
from .pairmeta import SourceMember, SourcePair, default_load, facts
from .targets import slot_label


@dataclass(frozen=True)
class LoadedSource:
    pair: SourcePair
    data: Mapping[str, bytes]                       # vid -> the data, verified
    name: str                                       # of the loading member
    play_time: int
    saved_at: int                                   # its meta time (0 when it has none)
    game_mode: int
    capture_account: LiveRef | None                 # the live account it was captured from
    where_label: str                                # "vault: <entry label>" / "live: Steam slot 4"

    @property
    def platform(self) -> Platform:
        return self.pair.platform

    @property
    def loading(self) -> SourceMember:
        return self.pair.member(self.pair.load)     # type: ignore[return-value]

    @property
    def pids(self) -> frozenset[str]:
        return frozenset(m.pid for m in (self.pair.a, self.pair.b) if m is not None and m.pid)


def load_source(ctx: OpContext, source: PairSource) -> LoadedSource:
    """Read the source pair; raises :class:`SourceUnreadable` when a member cannot be read
    with its version id."""
    wanted = {pos: vid for pos, vid in (("A", source.vid_a), ("B", source.vid_b)) if vid}
    if not wanted:
        raise SourceUnreadable("the source names no version")
    if source.folder_slot is not None:
        folder, slot = source.folder_slot
        read = _read_folder(Path(folder), slot, wanted,
                            {"kind": "folder", "name": Path(folder).name, "slot": slot})
        platform = _platform_of(Path(folder))
        capture, where = None, "a folder"
    elif source.prefer is not None and source.prefer.kind == "live":
        ref = source.prefer.ref
        folder = _live_folder(ctx, ref)
        read = _read_folder(folder, source.prefer.slot, wanted,
                            {"kind": "live", "platform": ref.platform, "account": ref.account_key,
                             "instance": ref.instance, "slot": source.prefer.slot})
        platform = ref.platform
        capture = LiveRef(ref.platform, ref.account_key)
        where = "live: " + slot_label(ref.platform, source.prefer.slot)
    else:
        read, platform = _read_located(ctx, source, wanted)
        capture, where = _capture_of(ctx, source.prefer, platform)
    model_pids = _model_pids(ctx)
    members = {}
    for pos, (data, plain, origin, slot) in read.items():
        pid = _pid_of(platform, wanted[pos], plain, data, slot, model_pids.get(wanted[pos]))
        members[pos] = SourceMember(pos, wanted[pos], pid, plain, origin)
    a, b = members.get("A"), members.get("B")
    try:
        load = source.load or default_load(platform, a, b)
        pair = SourcePair(platform, load, a, b)
        loading = pair.member(load)
        if loading is None:
            raise SourceUnreadable(f"the source has no member {load} to load")
        f = facts(platform, loading.meta_plain)
    except ValueError as exc:
        raise SourceUnreadable(str(exc)) from exc
    name, mode = _name_mode(platform, loading.meta_plain)
    return LoadedSource(pair, {m.vid: read[p][0] for p, m in members.items()}, name,
                        f.play_time, f.meta_ts, mode, capture, where)


def _read_located(ctx: OpContext, source: PairSource,
                  wanted: Mapping[str, str]) -> tuple[dict, Platform]:
    out, platform = {}, None
    for pos, vid in wanted.items():
        prefer = None
        if source.prefer is not None:
            prefer = dataclasses.replace(source.prefer, vid=vid, member=pos)
        try:
            opened = ctx.locator.open(vid, prefer)
        except VersionUnavailable as exc:
            raise SourceUnreadable(f"version {vid[:8]} cannot be read: {exc}") from exc
        if hashlib.sha256(opened.data).hexdigest() != vid:
            raise SourceUnreadable(f"version {vid[:8]} did not read back as itself")
        platform = platform or opened.platform
        if opened.platform != platform:
            raise SourceUnreadable("the two members come from different platforms")
        out[pos] = (opened.data, opened.meta_plain, opened.location.to_json(),
                    opened.location.slot)
    return out, platform    # type: ignore[return-value]


def _read_folder(folder: Path, slot: int, wanted: Mapping[str, str],
                 origin: Mapping[str, Any]) -> dict:
    """The wanted members of one slot of a save folder: data verified by its id, meta as
    captured (Steam decrypted with the ordinal it is keyed for)."""
    try:
        view = savedir.scan_any(folder)
    except OSError as exc:
        raise SourceUnreadable(f"the folder cannot be read ({exc.strerror})") from exc
    out = {}
    for pos, vid in wanted.items():
        mv = view.slots[slot].members[slotmap.member_index(pos)]
        if not mv.exists or not mv.valid:
            raise SourceUnreadable(f"slot {slot}{pos} is not a readable save")
        try:
            data = Path(mv.data_path).read_bytes()
            raw_meta = Path(mv.meta_path).read_bytes()
        except OSError as exc:
            raise SourceUnreadable(f"slot {slot}{pos} cannot be read ({exc.strerror})") from exc
        if hashlib.sha256(data).hexdigest() != vid:
            raise SourceUnreadable(f"slot {slot}{pos} no longer holds that version")
        plain = raw_meta if mv.xbox is not None or mv.ordinal_used is None else \
            meta.decrypt(raw_meta, mv.ordinal_used)
        out[pos] = (data, plain, dict(origin, member=pos), slot)
    return out


def _live_folder(ctx: OpContext, ref: LiveRef) -> Path:
    for source, r in zip(ctx.sources, live_refs(ctx.sources)):
        if r == ref:
            return Path(source.path)
    raise SourceUnreadable("that live folder is not configured")


def _platform_of(folder: Path) -> Platform:
    return "xbox" if savedir.platform_of(folder) == "xbox" else "steam"


def _capture_of(ctx: OpContext, prefer: Location | None,
                platform: Platform) -> tuple[LiveRef | None, str]:
    """The live account a vault copy was taken from (None when unknown: an import, a
    hand-made folder), and how to name where it is."""
    if prefer is None:
        return None, "vault"
    if isinstance(prefer.ref, StoreRef) and ctx.versions is not None:
        try:
            entry = ctx.versions.entry(prefer.ref.key)
        except (KeyError, ValueError):
            return None, "vault"
        account = None
        if entry.source.get("type") == "live" and entry.source.get("account_key"):
            account = LiveRef(entry.platform, entry.source["account_key"])
        return account, "vault: " + str(aliases.redact(entry.label))
    if isinstance(prefer.ref, EntryRef):
        entry = ctx.vault.get(prefer.ref.entry_id)
        if entry is None:
            return None, "vault"
        account = LiveRef(platform, entry.source) if entry.source else None
        return account, "vault: " + str(aliases.redact(entry.label or entry.kind))
    if isinstance(prefer.ref, LiveRef):
        return (LiveRef(prefer.ref.platform, prefer.ref.account_key),
                "live: " + slot_label(prefer.ref.platform, prefer.slot))
    return None, "vault"


def _model_pids(ctx: OpContext) -> dict[str, str]:
    """D1's playthrough id of every version the read model knows (its line, after any
    owner Combine), so the source is on the line the owner sees."""
    out: dict[str, str] = {}
    model = ctx.readmodel
    if model is None:
        return out
    for line in model.lines():
        for row in model.versions(line.pid):
            out.setdefault(row.head, row.pid)
            if row.partner:
                out.setdefault(row.partner, row.pid)
    return out


def _pid_of(platform: Platform, vid: str, plain: bytes, data: bytes, slot: int,
            model_pid: str | None) -> str:
    """D1 2.2: the id in the meta the version was captured with, else the id in its data;
    for a save with no id, the read model's line (a legacy key fixed when first seen), else
    the legacy key, else unknown. The captured meta comes first: the read model may have
    seen these bytes beside another meta (a slot torn by a crash)."""
    if platform == "steam":
        info = meta.parse(plain, 0)
        ident, name, size = info.slot_identifier, info.save_name, info.size_decompressed
    else:
        fields = msstore.parse_ms_meta(plain)
        ident, name, size = fields["slot_id"], fields["name"], fields["size_field"]
    for candidate in (ident, _data_id(data, size)):
        if candidate:
            try:
                return pid_from_id(candidate)
            except ModelError:
                pass
    if model_pid:
        return model_pid
    return legacy_pid(platform, slot, name) if name else unknown_pid(vid)


def _data_id(data: bytes, size: int) -> int:
    try:
        return playtime._id_from_bytes(data, size) or 0
    except Exception:  # noqa: BLE001 - an id the data does not give is simply not there
        return 0


def _name_mode(platform: Platform, plain: bytes) -> tuple[str, int]:
    if platform == "steam":
        info = meta.parse(plain, 0)
        return info.save_name, info.game_mode
    fields = msstore.parse_ms_meta(plain)
    return fields["name"], fields["game_mode"]
