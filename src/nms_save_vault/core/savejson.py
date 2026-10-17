"""Save data bytes -> the parsed (still obfuscated) JSON tree, losslessly.

The data file is an LZ4 container (:mod:`lz4_block`) holding UTF-8 JSON terminated by a NUL,
with stale bytes after it. Two things make a plain ``json.loads`` unsafe here:

* procedural ids inside the JSON can hold bytes that are not valid UTF-8 (0-260 bytes per
  real save). A strict decode fails and ``errors="replace"`` loses them, so the text is
  decoded with ``surrogateescape``: every byte survives as a lone surrogate and can be
  turned back into the exact original bytes.
* some saves carry a backslash followed by a raw TAB, LF or CR, which JSON rejects as an
  invalid escape. libNOM repairs that into ``\\t`` / ``\\n`` / ``\\r`` (Extensions/Span.cs);
  the same repair is applied here, only when the plain parse failed for that reason.

Every failure is a :class:`SaveDecodeError` with a ``kind`` the caller can show without
inspecting messages: ``corrupt``, ``unsupported`` (HGSAVEV2) or ``too-large``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from . import lz4_block

DecodeErrorKind = Literal["corrupt", "unsupported", "too-large"]

_BAD_ESCAPE = re.compile(rb"\\([\t\n\r])")
_ESCAPED = {b"\t": rb"\t", b"\n": rb"\n", b"\r": rb"\r"}


class SaveDecodeError(ValueError):
    """A save whose data cannot be turned into a JSON tree."""

    def __init__(self, kind: DecodeErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: DecodeErrorKind = kind


@dataclass(frozen=True)
class DecodedSave:
    root: dict[str, Any]                  # obfuscated keys, as parsed
    container: Literal["chunked", "unframed"]
    chunks: int
    decompressed_bytes: int
    decompressed_sha256: str              # of the JSON text up to the NUL: identical content in a
                                          # re-encoded container, or with other stale bytes, hashes alike
    json_bytes: int                       # up to the first NUL
    repaired_escapes: int                 # libNOM escape repairs applied (0 in real saves so far)


def decode_save(data: bytes, *, bare_size_hint: int | None = None,
                max_decompressed: int = lz4_block.MAX_SAVE_DECOMPRESSED) -> DecodedSave:
    """Decode a save data file into its JSON root.

    ``bare_size_hint`` is the decompressed size from the Xbox meta, needed only for an
    old unframed (pre-Waypoint) block, which carries no size of its own. Sizes are checked
    against ``max_decompressed`` (and :data:`lz4_block.MAX_BARE_BLOCK`) before anything is
    allocated.
    """
    if data.startswith(lz4_block.HGSAVEV2_HEADER):
        raise SaveDecodeError("unsupported", lz4_block.HGSAVEV2_UNSUPPORTED)
    chunks, _consumed = lz4_block.walk_chunks(data)
    if chunks:
        total = sum(c.decompressed_size for c in chunks)
        if total > max_decompressed:
            raise SaveDecodeError("too-large", f"the save claims {total} bytes, more than {max_decompressed}")
    elif bare_size_hint:
        if bare_size_hint > min(max_decompressed, lz4_block.MAX_BARE_BLOCK):
            raise SaveDecodeError("too-large", f"the save claims {bare_size_hint} bytes, more than an unframed save can hold")
    try:
        raw = lz4_block.decompress(data, bare_size_hint if not chunks else None)
    except lz4_block.UnsupportedContainer as exc:
        raise SaveDecodeError("unsupported", str(exc)) from None
    except ValueError as exc:
        raise SaveDecodeError("corrupt", str(exc)) from None

    body = raw.split(b"\x00", 1)[0]
    root, repaired = _parse(body)
    return DecodedSave(
        root=root,
        container="chunked" if chunks else "unframed",
        chunks=len(chunks),
        decompressed_bytes=len(raw),
        decompressed_sha256=hashlib.sha256(body).hexdigest(),
        json_bytes=len(body),
        repaired_escapes=repaired,
    )


def _parse(body: bytes) -> tuple[dict[str, Any], int]:
    repaired = 0
    try:
        root = _loads(body)
    except json.JSONDecodeError as exc:
        if "Invalid \\escape" not in exc.msg:
            raise _damaged(exc) from None
        body, repaired = _BAD_ESCAPE.subn(lambda m: _ESCAPED[m.group(1)], body)
        try:
            root = _loads(body)
        except (ValueError, RecursionError) as again:
            raise _damaged(again) from None
    except (ValueError, RecursionError) as exc:
        raise _damaged(exc) from None
    if not isinstance(root, dict):
        raise SaveDecodeError("corrupt", f"the save JSON is a {type(root).__name__}, not an object")
    return root, repaired


def _damaged(exc: BaseException) -> SaveDecodeError:
    """Any parse failure -- bad syntax, an integer past Python's digit limit, absurd nesting --
    as one 'corrupt' error. Only a JSONDecodeError's own message (no save text) is kept."""
    if isinstance(exc, json.JSONDecodeError):
        return SaveDecodeError("corrupt", f"the save JSON is damaged: {exc.msg} at byte {exc.pos}")
    if isinstance(exc, RecursionError):
        return SaveDecodeError("corrupt", "the save JSON is nested too deeply")
    return SaveDecodeError("corrupt", f"the save JSON cannot be read ({type(exc).__name__})")


def _loads(body: bytes) -> Any:
    # strict=False: raw control characters inside strings are accepted, as the game's own
    # reader (and libNOM's) accepts them.
    return json.loads(body.decode("utf-8", errors="surrogateescape"), strict=False)
