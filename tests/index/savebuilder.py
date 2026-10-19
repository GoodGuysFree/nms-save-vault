"""Synthetic saves for the index tests, built from readable field names.

``obfuscate`` hashes every key the way the game writes it; ``encode`` serialises the tree,
terminates it with a NUL and wraps it in a valid container: 0xFEEDA1E5 chunks of literal-only
LZ4 blocks, or one unframed block (an old Xbox save) whose size the caller passes as a hint.
A string may carry lone surrogates (``b"..".decode("utf-8", "surrogateescape")``) to put
invalid UTF-8 bytes into the data, as real procedural ids sometimes have.

``Raw(key)`` keeps a key exactly as given (an unknown or legacy key).
"""
from __future__ import annotations

import json
import struct
from typing import Any

from nms_save_vault.core import formats
from nms_save_vault.core.hashname import hash_name

from ..conftest import lz4_literals


class Raw(str):
    """A key written verbatim instead of hashed."""


def obfuscate(node: Any) -> Any:
    if isinstance(node, dict):
        return {(str(k) if isinstance(k, Raw) else hash_name(k)): obfuscate(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [obfuscate(v) for v in node]
    return node


def payload(readable: dict, *, tail: bytes = b"") -> bytes:
    text = json.dumps(obfuscate(readable), ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8", errors="surrogateescape") + b"\x00" + tail


def encode(readable: dict, *, chunk: int = formats.SAVE_CHUNK_DECOMPRESSED_MAX, unframed: bool = False,
           tail: bytes = b"") -> tuple[bytes, int | None]:
    """(data bytes, size hint): the hint is set only for an unframed save."""
    body = payload(readable, tail=tail)
    if unframed:
        return lz4_literals(body), len(body)
    out = bytearray()
    for i in range(0, len(body), chunk):
        part = body[i : i + chunk]
        block = lz4_literals(part)
        out += struct.pack("<4I", formats.SAVE_MAGIC, len(block), len(part), 0) + block
    return bytes(out), None


def context_save(*, base: dict | None = None, expedition: dict | None = None, common: dict | None = None,
                 version: int = 4720, active: str = "Main", base_mode: int = 1, expedition_mode: int = 6,
                 discoveries: list | None = None, extra: dict | None = None) -> dict:
    """A current (context-layout) save: ``base`` / ``expedition`` are PlayerStateData trees."""
    root: dict = {"Version": version, "Platform": "Win|Final", "ActiveContext": active,
                  "CommonStateData": common if common is not None else {"SaveName": "Synthetic", "TotalPlayTime": 3600}}
    if base is not None:
        root["BaseContext"] = {"GameMode": base_mode, "PlayerStateData": base}
    if expedition is not None:
        root["ExpeditionContext"] = {"GameMode": expedition_mode, "PlayerStateData": expedition}
    root["DiscoveryManagerData"] = {"DiscoveryData-v1": {"Store": {"Record": discoveries or []}}}
    root.update(extra or {})
    return root


def pre_context_save(psd: dict, *, version: int = 4135, discoveries: list | None = None) -> dict:
    """A pre-Waypoint save: PlayerStateData at the top, no contexts, no CommonStateData."""
    return {"Version": version, "Platform": "Win|Final", "PlayerStateData": psd,
            "DiscoveryManagerData": {"DiscoveryData-v1": {"Store": {"Record": discoveries or []}}}}
