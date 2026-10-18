"""The gated Steam <-> Xbox transfer (D5 3.5, F6): when it is allowed, and the source pair
converted for the other platform.

OFF by default. The owner's validation runs (T1/T2) switch it on with the environment
variable ``NMSVAULT_CROSS_PLATFORM=1``; it is read from the context's host (D7), never from
``os.environ`` here. While on, a transfer goes only into an empty slot of a folder that has a
full backup younger than 30 minutes. The data is never touched: only the metas are converted
(``convert``), so a transfer is an ordinary Place of the converted pair.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

from .. import lz4_block, meta, msstore, playtime, slotmap
from . import convert
from .context import OpContext
from .pairmeta import SourceMember, SourcePair, facts
from .source import LoadedSource

ENV_GATE = "NMSVAULT_CROSS_PLATFORM"
BACKUP_MAX_AGE = 30 * 60

#: ConvertUnsupported.reason -> the warning code the user sees (D5 3.5.1 G2, G3).
_CODES = {"data_format": "SOURCE_OLD_FORMAT", "data_walk": "SOURCE_OLD_FORMAT",
          "meta_length": "UNSUPPORTED_META", "meta_format": "UNSUPPORTED_META",
          "meta_header": "UNSUPPORTED_META", "GAME_NEWER": "GAME_NEWER"}


def gate_on(ctx: OpContext) -> bool:
    return ctx.host.env.get(ENV_GATE) == "1"


def code_of(exc: convert.ConvertUnsupported) -> str:
    return _CODES.get(exc.reason, convert.CONVERT_UNSUPPORTED)


def adapt(src: LoadedSource, platform: str) -> LoadedSource:
    """``src`` as a source for ``platform``: itself when it already is one, else the same
    data with every member's meta converted. Raises :class:`convert.ConvertUnsupported`."""
    if src.platform == platform:
        return src
    members = {}
    for pos in ("A", "B"):
        m = src.pair.member(pos)
        members[pos] = None if m is None else dataclasses.replace(
            m, meta_plain=_convert_meta(src.platform, m, src.data[m.vid]))
    pair = SourcePair(platform, src.pair.load, members["A"], members["B"])  # type: ignore[arg-type]
    return dataclasses.replace(src, pair=pair)


def _convert_meta(platform: str, m: SourceMember, data: bytes) -> bytes:
    ts = facts(platform, m.meta_plain).meta_ts            # the stamp is set later (F5b)
    if platform == "steam":
        return convert.steam_to_xbox(m.meta_plain, data, ts)
    return convert.xbox_to_steam_plain(m.meta_plain, data, ts)


def source_problems(src: LoadedSource) -> list[str]:
    """Warning codes for a source that may not cross platforms (empty list: it may). G4: every
    member's meta id is set and equals the id in its data."""
    try:
        adapt(src, "xbox" if src.platform == "steam" else "steam")
    except convert.ConvertUnsupported as exc:
        return [code_of(exc)]
    for pos in ("A", "B"):
        m = src.pair.member(pos)
        if m is None:
            continue
        ident, size = _identity(src.platform, m.meta_plain)
        try:
            in_data = playtime._id_from_bytes(src.data[m.vid], size)
        except lz4_block.UnsupportedContainer:
            return ["SOURCE_OLD_FORMAT"]
        if not ident or in_data != ident:
            return ["SOURCE_NO_ID"]
    return []


def _identity(platform: str, plain: bytes) -> tuple[int, int]:
    if platform == "steam":
        info = meta.parse(plain, 0)
        return info.slot_identifier, info.size_decompressed
    fields = msstore.parse_ms_meta(plain)
    return fields["slot_id"], fields["size_field"]


def slot_has_files(folder: Path, platform: str, slot: int) -> bool:
    """Is anything of this slot on disk at all? Empty means NO file: Steam ``saveN.hg`` and
    ``mf_saveN.hg`` of either member, or an Xbox index record of either member (with or
    without blobs). A partial slot is not empty."""
    if platform == "steam":
        names = [n for pos in (0, 1) for n in (slotmap.data_filename(slotmap.file_no(slot, pos)),
                                                slotmap.meta_filename(slotmap.file_no(slot, pos)))]
        return any((folder / n).exists() for n in names)
    wanted = {f"Slot{slot}Auto", f"Slot{slot}Manual"}
    _info, containers = msstore.parse_containers_index(folder)
    return any(c.identifier in wanted for c in containers)


def fresh_backup(ctx: OpContext, platform: str, account_key: str) -> bool:
    """A full backup of this account's folder younger than 30 minutes is in the vault."""
    if ctx.versions is None:
        return False
    now = ctx.clock()
    try:
        entries = ctx.versions.entries()           # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False
    for e in entries:
        if e.kind != "full" or e.platform != platform or e.account_key != account_key:
            continue
        try:
            made = datetime.strptime(e.created_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if 0 <= now - made <= BACKUP_MAX_AGE:
            return True
    return False
