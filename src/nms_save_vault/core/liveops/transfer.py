"""The Steam <-> Xbox transfer (D5 3.5, F6): which directions are validated, and the source
pair converted for the other platform.

A direction is open when it has a passing validation record (G1, built in below). The data
is never touched: only the metas are converted (``convert``), so a transfer is an ordinary
Place of the converted pair, into an empty or an occupied slot.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable

from .. import lz4_block, meta, msstore, playtime
from . import convert
from .pairmeta import SourceMember, SourcePair, facts
from .source import LoadedSource



@dataclasses.dataclass(frozen=True)
class ValidationRecord:
    """A direction that passed the owner's run in the real game (D5 3.5.3)."""
    runs: str
    date: str
    meta_format: int
    steam_meta_len: int
    xbox_meta_len: int
    base_version: int                # information: the game build of the run


_RUN_2026_10_18 = dict(date="2026-10-18", meta_format=2004, steam_meta_len=432,
                       xbox_meta_len=360, base_version=4228)
#: G1: (source platform, target platform) -> its passing record. A source meta of another
#: format or length is refused (``source_problems``, and G3 in ``convert``).
VALIDATED = {("steam", "xbox"): ValidationRecord(runs="T1", **_RUN_2026_10_18),
             ("xbox", "steam"): ValidationRecord(runs="T2", **_RUN_2026_10_18)}

#: ConvertUnsupported.reason -> the warning code the user sees (D5 3.5.1 G2, G3).
_CODES = {"data_format": "SOURCE_OLD_FORMAT", "data_walk": "SOURCE_OLD_FORMAT",
          "meta_length": "UNSUPPORTED_META", "meta_format": "UNSUPPORTED_META",
          "meta_header": "UNSUPPORTED_META", "GAME_NEWER": "GAME_NEWER"}


def validated(source_platform: str, target_platform: str, metas: Iterable[bytes] = ()) -> bool:
    """The direction has a passing record and, for every source meta plaintext given, the
    record's meta format and length for the source platform. BaseVersion is information only:
    a newer game is refused by the GAME_NEWER guard."""
    record = VALIDATED.get((source_platform, target_platform))
    if record is None:
        return False
    want_len = record.steam_meta_len if source_platform == "steam" else record.xbox_meta_len
    for plain in metas:
        if len(plain) != want_len:
            return False
        try:
            fmt = (meta.parse(plain, 0).meta_format if source_platform == "steam"
                   else msstore.parse_ms_meta(plain)["meta_format"])
        except ValueError:
            return False
        if fmt != record.meta_format:
            return False
    return True


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
    target = "xbox" if src.platform == "steam" else "steam"
    plains = [m.meta_plain for m in (src.pair.a, src.pair.b) if m is not None]
    if not validated(src.platform, target, plains):
        return ["UNSUPPORTED_META"]
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
