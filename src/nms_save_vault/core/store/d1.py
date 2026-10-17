"""D1's locator interface (``core/organizer/locator.py``, D1 2.11) as the store uses it.

The store implements D1's ``VersionLocator``; its errors are D1's ``VersionUnavailable`` and
``PreserveFailed``, so D1's ``VaultLocator`` falls through to its next reader on them. When this
code runs without D1's module, the same interface comes from :mod:`._d1_fallback`, a verbatim
copy; once D1's module is present it is used and the copy is unused.
"""
from __future__ import annotations

try:
    from ..organizer.locator import (
        LiveMember,
        Location,
        OpenedVersion,
        PreserveFailed,
        VaultLocator,
        VersionLocator,
        VersionUnavailable,
    )
except ImportError:
    from ._d1_fallback import (  # noqa: F401
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
