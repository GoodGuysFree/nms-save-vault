"""The core's public read API: everything another app (NMS Savecraft) may use of the core.

An app that bundles the core imports this module only, never the modules behind it; those
can change in any release. What is here is versioned:

- ``CORE_VERSION`` is the release the core came from (the Vault release tag, without ``v``).
- ``API_VERSION`` goes up when a name here is removed or changes meaning, including a
  function's parameters or a type's fields. Adding keeps it. ``tests/test_core_api.py`` pins
  all of that, so neither happens by accident.

This first API reads only: where the saves and the vault are, the saves on disk and what is in
them, and the vault's stored versions. Writing comes with the edit engine.

Saves are read by readable field names (``get(root, "PlayerStateData", "Units")``); the
obfuscated keys stay inside the core. :func:`get` refuses a path through an ownership field
(platform ids and user names: ``Owner``, ``UID``, ...) with :class:`PrivacyError`. A subtree it
returns, and ``DecodedSave.root``, can still hold them: an app must never show or keep a raw
subtree.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from . import gameversion, locations, savedir, state
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

PLATFORMS = ("steam", "xbox")

__all__ = [
    "API_VERSION", "CORE_VERSION", "PLATFORMS",
    # who is writing (every vault record names its app)
    "VAULT", "AppIdentity", "app_of", "current", "is_stamp", "set_app",
    # where things are
    "default_vault_dir", "live_save_dirs", "vault_dir",
    # the saves on disk
    "MemberView", "SaveDirView", "SlotView", "platform_of", "scan_save_dir",
    # what is in a save
    "CtxInput", "DecodedSave", "SaveDecodeError", "SaveInput", "contexts_of", "decode_save",
    "PrivacyError", "get", "items", "known_game_version", "read_member",
    # the vault's stored versions
    "FileEntry", "StoreEntry", "StoreError", "StoreNotUpgraded", "StoreTooNew",
    "StoreUnavailable", "VaultReader", "open_vault",
]


def vault_dir() -> Path:
    """The vault to share: the folder chosen in NMS Save Vault when its settings name one,
    else :func:`default_vault_dir`."""
    return state.configured_vault() or default_vault_dir()


def default_vault_dir() -> Path:
    """Where NMS Save Vault puts a new vault; an app that starts the vault for a user without
    NMS Save Vault puts it here, so a later install finds it."""
    return locations.default_vault_dir()


def live_save_dirs() -> list[Path]:
    """Every live save folder on this machine: Steam and GOG account folders, then Microsoft
    Store / Game Pass ones."""
    return [*locations.find_live_save_dirs(), *locations.find_microsoft_save_dirs()]


def platform_of(folder: str | Path) -> str:
    """``"steam"`` or ``"xbox"`` (:data:`PLATFORMS`): how the saves in ``folder`` are laid
    out."""
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


def known_game_version(member: MemberView, *, platform: str) -> bool:
    """The save's meta reads and its game version is one this core is tested with. Anything
    else - a newer game, an unreadable or invalid meta - may be shown but never written.
    ``platform`` is the folder's (:func:`platform_of`); another value raises ``ValueError``."""
    if platform not in PLATFORMS:
        raise ValueError(f"platform {platform!r} is not one of {PLATFORMS}")
    if not member.valid:
        return False
    return not gameversion.newer(platform, member.info.meta_format, member.info.base_version)


class VaultReader:
    """The vault's stored versions, to read. Nothing in the vault is written through it, not
    even a probe or a temporary copy."""

    def __init__(self, store: VersionStore) -> None:
        self._store = store

    @property
    def root(self) -> Path:
        return self._store.vault_root

    def entries(self) -> list[StoreEntry]:
        """Every stored entry (a backup, snapshot, import ...), with its files and members."""
        return self._store.entries()

    def entry(self, entry_id: str) -> StoreEntry:
        """One entry by id; ``StoreError`` when there is none."""
        return self._store.entry(entry_id)

    def read_file(self, entry_id: str, path: str) -> bytes:
        """One file of an entry (``path`` as in ``StoreEntry.files``), checked against its
        hash."""
        return self._store.read_file(entry_id, path)


def open_vault(vault_root: str | Path | None = None, *,
               live_roots: Sequence[Path] | None = None) -> VaultReader:
    """Open the vault's stored versions to read. ``vault_root`` defaults to
    :func:`vault_dir`. Raises :class:`StoreUnavailable`, :class:`StoreNotUpgraded` or
    :class:`StoreTooNew` (all :class:`StoreError`)."""
    root = Path(vault_root) if vault_root is not None else vault_dir()
    return VaultReader(VersionStore.open(root, lock=vault_lock(root), live_roots=live_roots,
                                         read_only=True))
