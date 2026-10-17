"""What the index must never read: platform account ids, usernames and ownership blocks.

Base ``Owner`` blocks, discovery ``OWS``, pet ``SenderData``, ``UsedDiscoveryOwnersV2`` and the
``LastEditedBy*`` fields hold other people's (and the owner's) platform ids and names. The
index processes none of them (owner decision: no ownership ids), so none can be cached.
:func:`nodes.get` refuses any path through a :data:`DENIED` name; the one exception is
:func:`owner_platform`, which reads a base's platform code ("ST", "XB", ...) and nothing else.
"""
from __future__ import annotations

import re

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
