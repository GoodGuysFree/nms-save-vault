"""Extractor registry: decoded save -> :class:`schema.VersionFacts`.

Save extractors run once per save; context extractors once per context (``base``,
``expedition``; a pre-context save has only ``base``). Each call is isolated: an exception
blanks that extractor's fields, records ``{"extractor": "<ctx>.<name>", "error": ...}`` and
makes the status ``partial``; every other fact stands. The error names the exception type
and the line in the extractor, never its message, which could carry save values.
"""
from __future__ import annotations

import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import nodes, schema
from ..nodes import get

ErrorList = list[dict]


@dataclass(frozen=True)
class SaveInput:
    root: dict                         # parsed root (obfuscated keys)
    common: dict | None                # CommonStateData, None in a pre-context save
    layout: Literal["context", "pre-context"]


@dataclass(frozen=True)
class CtxInput:
    name: Literal["base", "expedition"]
    psd: dict                          # PlayerStateData of the context
    container: dict | None             # BaseContext / ExpeditionContext; None pre-context
    save: SaveInput


@dataclass(frozen=True)
class Extractor:
    name: str
    scope: Literal["save", "context"]
    fields: tuple[str, ...]            # output keys it owns (blanked when it fails)
    fn: Callable[..., list[str] | None]


REGISTRY: list[Extractor] = []


def _register(scope: Literal["save", "context"], name: str, fields: tuple[str, ...]):
    def deco(fn: Callable[..., None]) -> Callable[..., None]:
        REGISTRY.append(Extractor(name, scope, tuple(fields), fn))
        return fn
    return deco


def save_extractor(name: str, fields: tuple[str, ...]):
    """Register ``fn(save: SaveInput, out: dict) -> list[str] | None``; returned strings are
    notes about a degraded (kept, but incomplete) fact and make the status ``partial``."""
    return _register("save", name, fields)


def context_extractor(name: str, fields: tuple[str, ...]):
    """Register ``fn(ctx: CtxInput, out: dict) -> list[str] | None`` (see save_extractor)."""
    return _register("context", name, fields)


def contexts_of(root: dict) -> tuple[SaveInput, dict[str, CtxInput]]:
    """The save's layout and its contexts (C9)."""
    psd = get(root, "PlayerStateData")
    if isinstance(psd, dict):
        save = SaveInput(root, None, "pre-context")
        return save, {"base": CtxInput("base", psd, None, save)}
    common = get(root, "CommonStateData")
    save = SaveInput(root, common if isinstance(common, dict) else None, "context")
    out: dict[str, CtxInput] = {}
    for name, container_name in (("base", "BaseContext"), ("expedition", "ExpeditionContext")):
        container = get(root, container_name)
        inner = get(container, "PlayerStateData")
        if isinstance(inner, dict):
            out[name] = CtxInput(name, inner, container, save)
    return save, out


def run_extractors(decoded, extractors: Iterable[Extractor] | None = None) -> schema.VersionFacts:
    """Facts of one decoded save (:class:`savejson.DecodedSave`)."""
    registry = list(REGISTRY if extractors is None else extractors)
    errors: ErrorList = []
    save, contexts = contexts_of(decoded.root)
    save_out: dict = {"layout": save.layout}
    for ex in registry:
        if ex.scope == "save":
            _run(ex, save, save_out, ex.name, errors)
    ctx_out: dict[str, dict] = {}
    for name, ctx in contexts.items():
        out: dict = {}
        for ex in registry:
            if ex.scope == "context":
                _run(ex, ctx, out, f"{name}.{ex.name}", errors)
        ctx_out[name] = out
    if save.layout == "context" and not contexts:
        errors.append({"extractor": "layout", "error": "unknown layout: no context holds PlayerStateData"})
    facts = {
        "schema": schema.SCHEMA_VERSION,
        "extractor": schema.EXTRACTOR_VERSION,
        "status": "ok",
        "errors": errors,
        "container": {
            "kind": decoded.container, "chunks": decoded.chunks,
            "decompressed_bytes": decoded.decompressed_bytes,
            "decompressed_sha256": decoded.decompressed_sha256, "json_bytes": decoded.json_bytes,
        },
        "save": save_out,
        "contexts": ctx_out,
        "primary": "base" if "base" in ctx_out else ("expedition" if ctx_out else None),
    }
    clean, problems = schema.validate(facts)
    clean.setdefault("errors", [])
    clean["errors"] += [{"extractor": "schema", "error": p[:200]} for p in problems]
    clean["status"] = "partial" if clean["errors"] else "ok"
    return clean


def _run(ex: Extractor, arg, out: dict, label: str, errors: ErrorList) -> None:
    try:
        notes = ex.fn(arg, out)
    except Exception as exc:  # noqa: BLE001 - one extractor must not sink the version
        for f in ex.fields:
            out.pop(f, None)
        errors.append({"extractor": label, "error": describe(exc)})
        return
    errors.extend({"extractor": label, "error": n[:200]} for n in notes or ())


def describe(exc: BaseException) -> str:
    """``"<Type> at <module>.py:<line>"`` - where in our code, never the message (save data)."""
    frames = traceback.extract_tb(exc.__traceback__)
    where = ""
    if frames:
        last = frames[-1]
        where = f" at {Path(last.filename).name}:{last.lineno}"
    return f"{type(exc).__name__}{where}"[:200]


# Registration order is output order; imported last so the decorators above exist.
from . import save as _save  # noqa: E402,F401
from . import progression as _progression  # noqa: E402,F401
from . import stats as _stats  # noqa: E402,F401
from . import bases as _bases  # noqa: E402,F401
from . import fleet as _fleet  # noqa: E402,F401
from . import companions as _companions  # noqa: E402,F401
from . import inventories as _inventories  # noqa: E402,F401

__all__ = ["SaveInput", "CtxInput", "Extractor", "REGISTRY", "save_extractor", "context_extractor",
           "contexts_of", "run_extractors", "describe", "nodes"]
