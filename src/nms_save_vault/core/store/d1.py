"""D1's locator interface (``core/organizer/locator.py``, D1 2.11) as the store uses it: the
store implements D1's ``VersionLocator`` and raises D1's ``VersionUnavailable`` and
``PreserveFailed``, so D1's ``VaultLocator`` falls through to its next reader on them."""
from __future__ import annotations

from ..organizer.locator import (
    LiveMember,
    Location,
    OpenedVersion,
    PreserveFailed,
    VaultLocator,
    VersionLocator,
    VersionUnavailable,
)

__all__ = ["LiveMember", "Location", "OpenedVersion", "PreserveFailed", "VaultLocator",
           "VersionLocator", "VersionUnavailable"]
