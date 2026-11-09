"""What the index must never read: platform account ids, usernames and ownership blocks.

Base ``Owner`` blocks, discovery ``OWS``, pet ``SenderData``, ``UsedDiscoveryOwnersV2`` and the
``LastEditedBy*`` fields hold other people's (and the owner's) platform ids and names. The
index processes none of them (owner decision: no ownership ids), so none can be cached.
:func:`nodes.get` refuses any path through a :data:`DENIED` name; the exceptions are
:func:`owner_platform`, which reads a base's platform code ("ST", "XB", ...) and nothing else,
:func:`owner_time`, which reads the owner block's timestamp and nothing else, and
:func:`rehome`, which gives a base record copied from another save of the same user the owner
of the save it goes into (GGF, 2026-11-09: "these are save files of the same user ... ids stay
inside"): the ids are moved inside the core and never handed out.
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Iterable

from ..hashname import key

DENIED = frozenset({
    "Owner", "OWS", "SenderData", "UsedDiscoveryOwnersV2", "LastEditedById",
    "LastEditedByUsername", "PlatformToken", "UID", "LID", "USN", "BlockList", "RID",
})

# A platform code is a few capitals; anything else in that field is not passed on.
_PLATFORM_CODE = re.compile(r"[A-Z]{2,4}")


class PrivacyError(LookupError):
    """Index code asked for a field on the privacy deny list."""


def check(name: str) -> None:
    if name in DENIED:
        raise PrivacyError(f"the index does not read {name!r} (ownership data)")


def owner_platform(base: object) -> str | None:
    """The platform code of a base's owner (``Owner.PTK``), or None. Never an id or a name."""
    if not isinstance(base, dict):
        return None
    owner = base.get(key("Owner"))
    if not isinstance(owner, dict):
        return None
    code = owner.get(key("PTK"))
    if isinstance(code, str) and _PLATFORM_CODE.fullmatch(code):
        return code
    return None


def owner_time(base: object) -> int | None:
    """When a base's owner block was made (``Owner.TS``, unix seconds), or None. A time, never
    an id or a name; it ties a corvette layout to its corvette and is not kept in the facts."""
    if not isinstance(base, dict):
        return None
    owner = base.get(key("Owner"))
    if not isinstance(owner, dict):
        return None
    ts = owner.get(key("TS"))
    return ts if isinstance(ts, int) and not isinstance(ts, bool) else None


#: The kinds of base a save's own player has: their owner is the player (on every real save,
#: 2026-11-10); the others (``ExternalPlanetBase``, ...) are other players' bases.
OWN_BASES = frozenset({"HomePlanetBase", "FreighterBase", "PlayerShipBase", "PlayerSpaceBase",
                       "PlayerSpaceStationBase"})


def is_base(record: object) -> bool:
    """Whether ``record`` is a base record (it has a type and an owner block)."""
    return isinstance(record, dict) and isinstance(record.get(key("Owner")), dict) \
        and isinstance(record.get(key("BaseType")), dict)


def _kind(record: dict) -> object:
    return record[key("BaseType")].get(key("PersistentBaseTypes"))


#: The owner block's platform code (``PTK``) of each platform's players.
PLATFORM_CODES = {"steam": "ST", "xbox": "XB"}


def save_owner(psds: Iterable[object], prefer: str | None = None) -> dict | None:
    """The owner ids (by key: UID, LID, USN, PTK; not the timestamp TS, which says when a
    record was made) the own bases of the first of ``psds`` (PlayerStateData) that has one
    carry most, those whose platform code is ``prefer`` first; None when none has a base of
    its player's with an id."""
    names = [key(f) for f in ("UID", "LID", "USN", "PTK")]
    for psd in psds:
        bases = psd.get(key("PersistentPlayerBases")) if isinstance(psd, dict) else None
        found: Counter = Counter()
        for record in bases if isinstance(bases, list) else ():
            if not is_base(record) or _kind(record) not in OWN_BASES:
                continue
            block = record[key("Owner")]
            ids = tuple(block.get(n) for n in names)
            if all(isinstance(v, str) for v in ids) and ids[0]:
                found[ids] += 1
        mine = Counter({ids: n for ids, n in found.items() if prefer and ids[3] == prefer})
        if mine or found:
            return dict(zip(names, (mine or found).most_common(1)[0][0]))
    return None


def rehome(record: dict, owner: dict) -> dict:
    """A copy of the base ``record`` whose owner ids are ``owner`` (:func:`save_owner`); when
    it was made stays, and who last edited it, when set, is cleared (the copy is the target
    player's own)."""
    out = copy.deepcopy(record)
    block = out[key("Owner")]
    for name, value in owner.items():
        block[name] = value
    for name in ("LastEditedById", "LastEditedByUsername"):
        if isinstance(out.get(key(name)), str) and out[key(name)]:
            out[key(name)] = ""
    return out
