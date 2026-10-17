"""A verbatim copy of D1's ``core/organizer/locator.py`` (only its import of the model is
spelled from here), used by :mod:`.d1` when D1's module is absent. Never edit it on its own:
D1's module is the source.

Where a version's bytes are (D1 2.11, C28): the organizer asks "where is this version",
never "which folder".

``VersionLocator`` is the interface the read model, the content index and the live
operations use; ``VaultLocator`` puts the readers together (phase 1: the folders the scan
saw; from phase 2 the version store first) with the one provider of ``preserve``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..organizer.model import LiveTarget, Member, Platform, SourceRef, Vid, source_to_json


class VersionUnavailable(Exception):
    """No location of the version could be read with the right bytes."""


class PreserveFailed(Exception):
    """A live save could not be made durable in the vault; the live write must not run."""


@dataclass(frozen=True)
class Location:
    vid: Vid
    kind: Literal["live", "entry", "store"]
    ref: SourceRef            # LiveRef / EntryRef(entry id) / StoreRef(store entry id)
    slot: int                 # the slot in the live folder or entry that holds the bytes
    member: Member            # the meta (and its Steam ordinal) belongs to that slot and member
    platform: Platform
    durable: bool             # a kept vault copy: a managed catalog entry or a store entry
    external: bool = False    # in a hand-made in-place folder: kept by the owner, not by us

    def to_json(self) -> dict[str, Any]:
        """The form operation records keep (never a path)."""
        return {**source_to_json(self.ref), "slot": self.slot, "member": self.member}


@dataclass(frozen=True)
class OpenedVersion:
    data: bytes               # verbatim; sha256 == vid (checked)
    meta_plain: bytes         # Steam: decrypted with the location's ordinal; Xbox: as stored
    platform: Platform
    meta_format: int
    location: Location        # where the bytes were read


@dataclass(frozen=True)
class LiveMember:
    target: LiveTarget
    member: Member
    expected_vid: Vid         # refuse if the live file no longer hashes to this


class VersionLocator(ABC):
    @abstractmethod
    def locate(self, vid: Vid) -> list[Location]:
        """Every known location: store, then durable catalog entries, external, live."""

    @abstractmethod
    def open(self, vid: Vid, prefer: Location | None = None) -> OpenedVersion:
        """Read the bytes (``prefer`` first, else in ``locate`` order) and check their hash.
        Raises VersionUnavailable."""

    @abstractmethod
    def preserve(self, members: Sequence[LiveMember], *, op_id: str,
                 reason: str) -> list[Location]:
        """Make each member durable in the vault. Raises PreserveFailed."""

    @abstractmethod
    def is_durable(self, vid: Vid) -> bool:
        """True when at least one durable vault copy exists."""


class VaultLocator(VersionLocator):
    """The one locator handed to the content index, the live operations and the read model."""

    def __init__(self, readers: Sequence[VersionLocator],
                 preserver: VersionLocator | None) -> None:
        self.readers = list(readers)
        self.preserver = preserver

    def locate(self, vid: Vid) -> list[Location]:
        return [loc for reader in self.readers for loc in reader.locate(vid)]

    def open(self, vid: Vid, prefer: Location | None = None) -> OpenedVersion:
        failures = []
        for reader in self.readers:
            try:
                return reader.open(vid, prefer)
            except VersionUnavailable as exc:
                failures.append(str(exc))
        raise VersionUnavailable(f"version {vid[:8]}: " + ("; ".join(failures) or "nowhere"))

    def preserve(self, members: Sequence[LiveMember], *, op_id: str,
                 reason: str) -> list[Location]:
        if self.preserver is None:
            raise PreserveFailed("store-unavailable")
        return self.preserver.preserve(members, op_id=op_id, reason=reason)

    def is_durable(self, vid: Vid) -> bool:
        return any(reader.is_durable(vid) for reader in self.readers)
