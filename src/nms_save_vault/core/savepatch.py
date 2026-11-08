"""Format-preserving edits of a save's JSON text, each proven by a re-parse (CL4).

An editor never re-serialises a save: whatever the game wrote, and every key this core does
not know, stays byte for byte. An edit names a place by readable field names and array
indices (``("BaseContext", "PlayerStateData", "Units")``) and replaces, removes or inserts the
bytes of one value there; everything outside those bytes is untouched by construction.

:func:`apply` then parses the edited text and compares it with the original tree with exactly
the intended changes made to it in memory. Anything else - an edit that hit the wrong place,
broke the syntax, or changed more than it said - raises :class:`PatchRefused` and nothing is
returned. The text is the save's JSON without its NUL terminator (``savejson`` decodes it).

Values are JSON text (``raw``): an edit copying something from another save passes that
save's own bytes (:func:`value_bytes` of a span), so it arrives exactly as the game wrote it;
:func:`dumps` turns a Python value into compact JSON for new values.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Union

from .hashname import key
from .savejson import SaveDecodeError, _parse

Step = Union[str, int]          # a readable field name, or an array index
Where = Sequence[Step]

_TOKEN = re.compile(rb'["{}\[\]]')
_STRING_END = re.compile(rb'["\\]')
_SCALAR_END = re.compile(rb"[,}\]\s]")
_WS = b" \t\r\n"


class PatchRefused(ValueError):
    """An edit that cannot be made exactly, or whose result is not what it said."""


# --- the edits ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Replace:
    """The value at ``where`` becomes ``raw``."""

    where: Where
    raw: bytes


@dataclass(frozen=True)
class Remove:
    """The array element or object member at ``where`` goes, with its separator."""

    where: Where


@dataclass(frozen=True)
class Insert:
    """``raw`` becomes element ``index`` of the array at ``where`` (``index`` = its length:
    appended)."""

    where: Where
    index: int
    raw: bytes


@dataclass(frozen=True)
class AddMember:
    """The object at ``where`` gains member ``name`` (a readable field name) = ``raw``; it
    must not have it yet."""

    where: Where
    name: str
    raw: bytes


Edit = Union[Replace, Remove, Insert, AddMember]


def dumps(value: Any) -> bytes:
    """A Python value as compact JSON text, the way the game writes JSON (UTF-8, no spaces)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --- finding bytes -------------------------------------------------------------------------


def _ws(body: bytes, p: int) -> int:
    n = len(body)
    while p < n and body[p] in _WS:
        p += 1
    return p


def _string_end(body: bytes, p: int) -> int:
    """``p`` at an opening quote: the index after the closing quote."""
    p += 1
    while True:
        m = _STRING_END.search(body, p)
        if m is None:
            raise PatchRefused("an unterminated string in the save JSON")
        if body[m.start()] == 0x5C:
            p = m.start() + 2
            continue
        return m.start() + 1


def _value_end(body: bytes, p: int) -> int:
    """``p`` at a value's first byte: the index after its last."""
    c = body[p]
    if c == 0x22:
        return _string_end(body, p)
    if c in b"{[":
        depth = 0
        while True:
            m = _TOKEN.search(body, p)
            if m is None:
                raise PatchRefused("unbalanced save JSON")
            q, t = m.start(), body[m.start()]
            if t == 0x22:
                p = _string_end(body, q)
                continue
            depth += 1 if t in b"{[" else -1
            if depth == 0:
                return q + 1
            p = q + 1
    m = _SCALAR_END.search(body, p)
    return len(body) if m is None else m.start()


@dataclass(frozen=True)
class _Container:
    open_: int                          # the index after '{' or '['
    close: int                          # the index of '}' or ']'
    items: tuple[tuple[int, int, int], ...]   # (item start, value start, value end)
    keys: tuple[str | None, ...]        # an object's obfuscated keys; None for an array


def _container(body: bytes, p: int) -> _Container:
    c = body[p]
    if c not in b"{[":
        raise PatchRefused("expected an object or an array in the save JSON")
    is_obj = c == 0x7B
    end_char = 0x7D if is_obj else 0x5D
    q = _ws(body, p + 1)
    items, keys = [], []
    while body[q] != end_char:
        start = q
        name = None
        if is_obj:
            k_end = _string_end(body, q)
            name = json.loads(body[q:k_end].decode("utf-8", errors="surrogateescape"))
            q = _ws(body, k_end)
            if body[q] != 0x3A:
                raise PatchRefused("expected ':' in the save JSON")
            q = _ws(body, q + 1)
        v_end = _value_end(body, q)
        items.append((start, q, v_end))
        keys.append(name)
        q = _ws(body, v_end)
        if body[q] == 0x2C:
            q = _ws(body, q + 1)
        elif body[q] != end_char:
            raise PatchRefused("expected ',' in the save JSON")
    return _Container(p + 1, q, tuple(items), tuple(keys))


def _locate(body: bytes, where: Where) -> tuple[int, int, _Container | None, int | None]:
    """(value start, value end, the container holding it, its item number there) of the
    value at ``where``; the root has no container."""
    start, end = _ws(body, 0), None
    parent, item = None, None
    for step in where:
        box = _container(body, start)
        if isinstance(step, bool) or not isinstance(step, (str, int)):
            raise PatchRefused(f"a place is named by field names and indices, not {step!r}")
        if isinstance(step, str):
            if body[start] != 0x7B:
                raise PatchRefused(f"{step!r}: not an object here")
            want = key(step)
            if want not in box.keys:
                raise PatchRefused(f"no field {step!r} here")
            item = box.keys.index(want)
        else:
            if body[start] != 0x5B:
                raise PatchRefused(f"[{step}]: not an array here")
            if not 0 <= step < len(box.items):
                raise PatchRefused(f"[{step}]: the array has {len(box.items)} elements")
            item = step
        parent = box
        _, start, end = box.items[item]
    if end is None:
        end = _value_end(body, start)
    return start, end, parent, item


def span(body: bytes, where: Where) -> tuple[int, int]:
    """The bytes ``[start, end)`` of the value at ``where``."""
    start, end, _, _ = _locate(body, where)
    return start, end


def value_bytes(body: bytes, where: Where) -> bytes:
    """The value at ``where`` exactly as the save holds it: to copy it into another save."""
    start, end = span(body, where)
    return body[start:end]


# --- applying ------------------------------------------------------------------------------


def _removals(body: bytes, removes: list[Remove]) -> list[tuple[int, int, bytes]]:
    """The byte ranges that remove items, per container: each run of neighbouring items goes
    with one separator, so any set of items of one container can go at once."""
    groups: dict[tuple, tuple[_Container, set[int]]] = {}
    for edit in removes:
        if not edit.where:
            raise PatchRefused("the save itself cannot be removed")
        _, _, box, item = _locate(body, edit.where)
        groups.setdefault(tuple(edit.where[:-1]), (box, set()))[1].add(item)
    out = []
    for box, items in groups.values():
        n = len(box.items)
        if len(items) == n:
            out.append((box.open_, box.close, b""))
            continue
        run_start = None
        for i in range(n + 1):
            if i < n and i in items:
                run_start = i if run_start is None else run_start
                continue
            if run_start is not None:
                last = i - 1
                if i < n:            # the run and the separator after it
                    out.append((box.items[run_start][0], box.items[i][0], b""))
                else:                # a run at the end: the separator before it, and the run
                    out.append((box.items[run_start - 1][2], box.items[last][2], b""))
                run_start = None
    return out


def _splice(body: bytes, edit: Edit) -> tuple[int, int, bytes]:
    """The byte range an edit (not a removal) replaces, and what with."""
    if isinstance(edit, Replace):
        if not edit.where:
            raise PatchRefused("replace the save's fields, not the whole save")
        start, end, _, _ = _locate(body, edit.where)
        return start, end, edit.raw
    target, _, _, _ = _locate(body, edit.where)
    box = _container(body, target)
    if isinstance(edit, Insert):
        if body[target] != 0x5B:
            raise PatchRefused("an element is inserted into an array only")
        if not 0 <= edit.index <= len(box.items):
            raise PatchRefused(f"[{edit.index}]: the array has {len(box.items)} elements")
        if not box.items:
            return box.open_, box.close, edit.raw
        if edit.index < len(box.items):
            at = box.items[edit.index][0]
            return at, at, edit.raw + b","
        at = box.items[-1][2]
        return at, at, b"," + edit.raw
    if isinstance(edit, AddMember):
        if body[target] != 0x7B:
            raise PatchRefused("a member is added to an object only")
        name = key(edit.name)
        if name in box.keys:
            raise PatchRefused(f"the object has {edit.name!r} already")
        member = dumps(name) + b":" + edit.raw
        if not box.items:
            return box.open_, box.close, member
        at = box.items[-1][2]
        return at, at, b"," + member
    raise PatchRefused(f"not an edit: {edit!r}")


def _node(tree: Any, steps: Where) -> Any:
    for step in steps:
        tree = tree[key(step) if isinstance(step, str) else step]
    return tree


def _expected(tree: Any, edit: Edit, parse) -> None:
    """Make ``edit`` to the parsed tree in memory: what the text must read as afterwards."""
    where = list(edit.where)
    if isinstance(edit, Insert):
        _node(tree, where).insert(edit.index, parse(edit.raw))
    elif isinstance(edit, AddMember):
        _node(tree, where)[key(edit.name)] = parse(edit.raw)
    else:
        parent, last = _node(tree, where[:-1]), where[-1]
        name = key(last) if isinstance(last, str) else last
        if isinstance(edit, Replace):
            parent[name] = parse(edit.raw)
        else:
            del parent[name]


def _parse_value(raw: bytes) -> Any:
    try:
        root, _ = _parse(b'{"v":' + raw + b"}")
    except SaveDecodeError:
        raise PatchRefused(f"a value is not JSON: {raw[:40]!r}") from None
    return root["v"]


def _tree_order(edits: list[Edit]) -> list[Edit]:
    """The order to make the edits in memory, where each names its place in the original:
    edits that shift nothing first, then removals and insertions deepest first and, in one
    array, from the highest index down (a removal before an insertion at the same index)."""
    def rank(edit: Edit) -> tuple:
        if isinstance(edit, (Replace, AddMember)):
            return (0,)
        where = list(edit.where)
        index = edit.index if isinstance(edit, Insert) else where[-1]
        depth = len(where) + (1 if isinstance(edit, Insert) else 0)
        return (1, -depth, -index if isinstance(index, int) else 0,
                0 if isinstance(edit, Remove) else 1)
    return sorted(edits, key=rank)


def apply(body: bytes, edits: Iterable[Edit]) -> bytes:
    """``body`` with every edit made, proven: the result parses, and reads as the original
    tree with exactly those edits made. Each edit names its place in the original text; edits
    must not touch the same bytes, and two insertions may not share one point (make them in
    one :class:`Insert` per index, or in two calls). Raises :class:`PatchRefused` otherwise,
    and returns nothing."""
    edits = list(edits)
    if not edits:
        return body
    removes = [e for e in edits if isinstance(e, Remove)]
    splices = _removals(body, removes) + [_splice(body, e) for e in edits
                                          if not isinstance(e, Remove)]
    splices.sort(key=lambda s: (s[0], s[1]))
    for (s1, e1, _), (s2, e2, _) in zip(splices, splices[1:]):
        if s2 < e1 or s1 == e1 == s2 == e2:
            raise PatchRefused("two edits touch the same bytes")
    out, at = bytearray(), 0
    for start, end, raw in splices:
        out += body[at:start]
        out += raw
        at = end
    out += body[at:]
    new = bytes(out)
    try:
        expected, _ = _parse(body)
        for edit in _tree_order(edits):
            _expected(expected, edit, _parse_value)
        got, _ = _parse(new)
    except SaveDecodeError as exc:
        raise PatchRefused(f"the edited save does not parse: {exc}") from None
    except (KeyError, IndexError, TypeError) as exc:
        raise PatchRefused(f"an edit does not fit the save's tree ({type(exc).__name__})") from None
    if got != expected:
        raise PatchRefused("the edited save is not the original with exactly these edits")
    return new
