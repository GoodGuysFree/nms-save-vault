"""The core's public read API: everything another app (NMS Savecraft) may use of the core.

An app that bundles the core imports this module only, never the modules behind it; those
can change in any release. What is here is versioned:

- ``CORE_VERSION`` is the release the core came from (the Vault release tag, without ``v``).
- ``API_VERSION`` goes up when a name here is removed or changes meaning. Adding a name
  keeps it. ``tests/test_core_api.py`` pins the list, so neither happens by accident.

This first API reads only: where the saves and the vault are, the saves on disk and what is in
them, and the vault's stored versions. Writing comes with the edit engine.

Saves are read by readable field names (``get(root, "PlayerStateData", "Units")``); the
obfuscated keys stay inside the core. :func:`get` refuses the ownership fields (platform ids
and user names: ``Owner``, ``UID``, ...) with :class:`PrivacyError`, so nothing built on this
API can show or keep them.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from . import appinfo, gameversion, locations, savedir
from .appinfo import CORE_VERSION, VAULT, AppIdentity, app_of, current, is_stamp, set_app
from .index.extract import CtxInput, SaveInput, contexts_of
from .index.nodes import PrivacyError, get, items
from .savedir import MemberView, SaveDirView, SlotView
from .savejson import DecodedSave, SaveDecodeError, decode_save
from .store import (
    FileEntry,
    StoreEntry,
    StoreError,
    StoreNotUpgraded,
    StoreTooNew,
    StoreUnavailable,
    VersionStore,
    vault_lock,
)

API_VERSION = 1

__all__ = [
    "API_VERSION", "CORE_VERSION",
    # who is writing (every vault record names its app)
    "VAULT", "AppIdentity", "app_of", "current", "is_stamp", "set_app",
    # where things are
    "default_vault_dir", "live_save_dirs",
    # the saves on disk
    "MemberView", "SaveDirView", "SlotView", "platform_of", "scan_save_dir",
    # what is in a save
    "CtxInput", "DecodedSave", "SaveDecodeError", "SaveInput", "contexts_of", "decode_save",
    "PrivacyError", "get", "items", "newer_than_known", "read_member",
    # the vault's stored versions
    "FileEntry", "StoreEntry", "StoreError", "StoreNotUpgraded", "StoreTooNew",
    "StoreUnavailable", "VersionStore", "open_vault",
]


def default_vault_dir() -> Path:
    """Where NMS Save Vault keeps the vault by default; an app without a vault of its own
    uses this one, so both apps share it."""
    return locations.default_vault_dir()


def live_save_dirs() -> list[Path]:
    """Every live save folder on this machine: Steam and GOG account folders, then Microsoft
    Store / Game Pass ones."""
    return [*locations.find_live_save_dirs(), *locations.find_microsoft_save_dirs()]


def platform_of(folder: str | Path) -> str:
    """``"steam"`` or ``"xbox"``: how the saves in ``folder`` are laid out."""
    return savedir.platform_of(folder)


def scan_save_dir(folder: str | Path) -> SaveDirView:
    """The slots and saves of a save folder of any platform. Only reads."""
    return savedir.scan_any(folder)


def read_member(member: MemberView) -> DecodedSave:
    """Decode one save (a slot's A or B member) from disk. ``root`` of the result holds the
    save's JSON; walk it with :func:`get` and :func:`contexts_of`. Raises ``OSError`` when
    the file cannot be read and :class:`SaveDecodeError` when it is damaged."""
    hint = member.info.size_decompressed if member.info is not None else None
    return decode_save(Path(member.data_path).read_bytes(), bare_size_hint=hint or None)


def newer_than_known(member: MemberView, *, platform: str) -> bool:
    """The save was written by a newer game than this core is tested with: show it, but
    never write it. ``platform`` is the folder's (:func:`platform_of`)."""
    if member.info is None:
        return False
    return gameversion.newer(platform, member.info.meta_format, member.info.base_version)


def open_vault(vault_root: str | Path | None = None, *,
               live_roots: Sequence[Path] | None = None) -> VersionStore:
    """The vault's stored versions, opened to read: ``entries()``, ``entry(id)``,
    ``read_file(id, path)``. Nothing in the vault is written, not even a probe.
    ``vault_root`` defaults to :func:`default_vault_dir`. Raises :class:`StoreUnavailable`,
    :class:`StoreNotUpgraded` or :class:`StoreTooNew` (all :class:`StoreError`)."""
    root = Path(vault_root) if vault_root is not None else default_vault_dir()
    return VersionStore.open(root, lock=vault_lock(root), app_version=appinfo.stamp(),
                             live_roots=live_roots, read_only=True)
