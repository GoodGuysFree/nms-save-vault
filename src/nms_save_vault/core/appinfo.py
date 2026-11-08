"""Which application is writing to the vault.

Every record the vault keeps - a store entry's manifest, a live-operation journal line, the
organizer's header and lineage, the index cache - carries an ``app`` stamp, ``"<name>
<version>"``. NMS Save Vault and NMS Savecraft share one vault, so the stamp is how a version
made because of the editor is told apart from one made by the app, and how the last version
before an editor change is found.

The stamp comes from here and nowhere else. A front-end that is not NMS Save Vault calls
:func:`set_app` once at startup, before it opens a vault; everything stamped after that names
it. Without that call the core is NMS Save Vault's.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from .. import __version__ as CORE_VERSION

VAULT = "nms-save-vault"
UNKNOWN = "unknown"

_NAME = re.compile(r"[a-z][a-z0-9-]{1,40}")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,40}")


@dataclass(frozen=True)
class AppIdentity:
    name: str
    version: str

    def __str__(self) -> str:
        return f"{self.name} {self.version}"


_lock = threading.Lock()
_current = AppIdentity(VAULT, CORE_VERSION)


def set_app(name: str, version: str) -> AppIdentity:
    """Name the application for everything stamped from now on; returns the previous one.

    ``name`` is a lowercase product id (``nms-savecraft``), ``version`` its release; a space in
    either would make the stamp ambiguous, so both are checked."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"app name {name!r} must be a lowercase id like 'nms-savecraft'")
    if not _VERSION.fullmatch(version):
        raise ValueError(f"app version {version!r} must be a release like '0.1.0'")
    global _current
    with _lock:
        previous, _current = _current, AppIdentity(name, version)
    return previous


def current() -> AppIdentity:
    with _lock:
        return _current


def stamp() -> str:
    """The ``app`` value to record now: ``"<name> <version>"``."""
    return str(current())


def app_of(recorded: object) -> str:
    """The application a recorded stamp names.

    Records from before the stamp named its app hold NMS Save Vault's bare version (``"0.3.6"``)
    and read as NMS Save Vault; anything unreadable reads as ``unknown`` - never as another
    app."""
    if not isinstance(recorded, str) or not recorded.strip():
        return UNKNOWN
    head, _, tail = recorded.strip().partition(" ")
    if not tail:
        return VAULT if head[:1].isdigit() else UNKNOWN
    return head if _NAME.fullmatch(head) else UNKNOWN
