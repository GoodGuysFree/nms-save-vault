"""An account's account data (``accountdata.hg``): settings and the rewards unlocked for the
whole account. Only read here.

The game writes it as plain JSON text ending in one NUL, not in LZ4 chunks as a save (a real
Steam account file, 2026-11-10: its meta, ``mf_accountdata.hg`` with storage ordinal 1, gives
one size for the data and the file). A chunked one is read too. An Xbox account keeps its
account data in a container, which is not read yet.
"""
from __future__ import annotations

from pathlib import Path

from . import formats, savejson


def decode(data: bytes) -> dict:
    """The JSON root (obfuscated keys) of account data. Raises ``SaveDecodeError``."""
    if data.lstrip()[:1] == b"{":
        root = savejson.parse_text(data.split(b"\x00", 1)[0])
    else:
        root = savejson.decode_save(data).root
    if not isinstance(root, dict):
        raise savejson.SaveDecodeError("corrupt", "account data is not a JSON object")
    return root


def read(folder: str | Path, platform: str = "steam") -> dict | None:
    """The account data of the save folder ``folder``; None when there is none to read (no
    file, unreadable data, an Xbox account)."""
    if platform != "steam":
        return None
    try:
        return decode((Path(folder) / formats.ACCOUNT_DATA_NAME).read_bytes())
    except (OSError, savejson.SaveDecodeError, ValueError):
        return None
