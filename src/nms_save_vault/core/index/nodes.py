"""Reading the obfuscated save tree by readable field names, and normalising what comes out.

``get(psd, "UniverseAddress", "RealityIndex")`` hashes each name to its save key
(:mod:`hashname`) and walks dicts; anything missing or of the wrong shape gives the default,
never an exception. A name on the privacy deny list raises :class:`privacy.PrivacyError`.

The normalisers turn the save's loose typing into stable fact values: every string valid
Unicode, game ids without their ``^``, 64-bit values as ``0x%016X`` strings (exact in JSON).
"""
from __future__ import annotations

import re

from ..hashname import key
from .privacy import DENIED, PrivacyError, check

__all__ = ["get", "items", "key", "PrivacyError", "DENIED", "u32", "u64", "hex64", "galaxy_of",
           "text", "gid", "seed", "number", "integer"]

_MASK64 = (1 << 64) - 1
_MARKUP = re.compile(r"<[^<>]*>")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_REPLACEMENT = chr(0xFFFD)   # spelt as chr(): src/ literals stay ASCII (tests/test_ui_text.py)


def get(node: object, *path: str, default: object = None) -> object:
    """Walk ``node`` by readable names; ``default`` when a step is missing or not a dict."""
    for name in path:
        check(name)               # the whole path, even where the walk would stop early
    for name in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key(name))
        if node is None:
            return default
    return node


def items(node: object, *path: str) -> list:
    """:func:`get` that returns ``[]`` unless the value is a list."""
    value = get(node, *path)
    return value if isinstance(value, list) else []


def integer(v: object) -> int | None:
    """An int (bools are not), else None."""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def u32(v: object) -> int | None:
    """Unsigned 32-bit: ``Units`` is stored signed, so -1 means 4,294,967,295."""
    i = integer(v)
    return None if i is None else i & 0xFFFFFFFF


def u64(v: object) -> int | None:
    """A 64-bit value given as ``"0x.."`` (or decimal) string or as int, masked to 64 bits."""
    if isinstance(v, str):
        s = v.strip()
        try:
            i = int(s[2:], 16) if s[:2].lower() == "0x" else int(s, 10)
        except ValueError:
            return None
        return i & _MASK64
    i = integer(v)
    return None if i is None else i & _MASK64


def hex64(v: object) -> str | None:
    i = u64(v)
    return None if i is None else f"0x{i:016X}"


def galaxy_of(address: object) -> int | None:
    """The galaxy index (bits 32-39) of a base's ``GalacticAddress``."""
    i = u64(address)
    return None if i is None else (i >> 32) & 0xFF


def text(v: object, limit: int = 128) -> str:
    """Player or game text for a fact: markup stripped, invalid bytes as U+FFFD, trimmed."""
    if not isinstance(v, str):
        return ""
    s = _SURROGATE.sub(_REPLACEMENT, _MARKUP.sub("", v)).strip()
    return s[:limit]


def gid(v: object) -> str:
    """A game id without its leading ``^`` (``^ACT1_STEP1`` -> ``ACT1_STEP1``)."""
    if isinstance(v, str) and v.isascii() and "<" not in v:
        return v.strip()[:128].removeprefix("^")    # the common case, ~15,000 times per save
    return text(v).removeprefix("^")


def seed(v: object) -> str | None:
    """A seed stored as ``[bool, "0x.."]`` or ``"0x.."``, as ``0x%016X``."""
    if isinstance(v, list) and len(v) == 2:
        v = v[1]
    return hex64(v)


def number(value: object) -> int | float | None:
    """A stat's ``Value`` block as a number: ``{IntValue: n}`` or ``{FloatValue: x}``; the game
    writes ``{}`` for zero."""
    if not isinstance(value, dict):
        return None
    i = integer(get(value, "IntValue"))
    if i is not None:
        return i
    f = get(value, "FloatValue")
    if isinstance(f, (int, float)) and not isinstance(f, bool):
        return f
    return 0
