"""Content-addressed objects: ``objects/<aa>/<sha256>``, verbatim bytes written once (D3 2.6).

Invariants (D3 2.13): an object's name is the sha256 of its bytes; every read verifies it; no
code path deletes or modifies an object. A bad object is renamed into ``quarantine/`` and kept.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .. import safety
from .errors import ObjectCorrupt, ObjectMissing, StoreUnavailable

log = logging.getLogger(__name__)

_DIGEST = re.compile(r"[0-9a-f]{64}")
_FANOUT = re.compile(r"[0-9a-f]{2}")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


@dataclass(frozen=True)
class PutResult:
    digest: str
    size: int
    written: bool    # False: deduplicated onto an existing object that re-verified
    healed: bool     # an existing object failed its re-hash and was replaced


class ObjectStore:
    def __init__(self, objects: Path, tmp: Path, quarantine: Path, *,
                 faults: Callable[[str], None] | None = None) -> None:
        self.objects = objects
        self.tmp = tmp
        self.quarantine_dir = quarantine
        self._faults = faults

    def path_for(self, digest: str) -> Path:
        if not is_digest(digest):
            raise ValueError(f"not a sha256 digest: {digest!r}")
        return self.objects / digest[:2] / digest

    def has(self, digest: str) -> bool:
        return self.path_for(digest).is_file()

    def put(self, data: bytes, digest: str) -> PutResult:
        """Store ``data`` under ``digest`` (its sha256). An existing object is re-hashed first:
        a match deduplicates, a mismatch is quarantined and replaced. A new object is written
        to a temp file, fsynced, renamed into place and read back."""
        if sha256(data) != digest:
            raise ValueError("put: the digest is not the sha256 of the bytes")
        path = self.path_for(digest)
        healed = False
        if path.is_file():
            if _hash_file(path) == digest:
                return PutResult(digest, len(data), written=False, healed=False)
            self._quarantine_if_present(digest, "re-hash mismatch on dedupe")
            healed = True
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.tmp / f"{digest}.{os.getpid()}.{secrets.token_hex(4)}.part"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            self._fault("object.after_tmp")
            try:
                os.replace(tmp, path)
            except PermissionError:
                # Windows: another process holds the object open; it wrote the same bytes.
                if path.is_file() and _hash_file(path) == digest:
                    return PutResult(digest, len(data), written=False, healed=healed)
                raise
            safety.fsync_dir(path.parent)
            self._fault("object.after_replace")
        finally:
            _silent_unlink(tmp)
        if _hash_file(path) != digest:
            self._quarantine_if_present(digest, "read-back mismatch")
            raise ObjectCorrupt(digest, "the copy read back differs")
        return PutResult(digest, len(data), written=True, healed=healed)

    def get(self, digest: str, size: int | None = None) -> bytes:
        """The object's bytes, verified: ObjectMissing when absent, ObjectCorrupt when the size
        or the sha256 is wrong. Reads never write: a bad object stays where it is."""
        path = self.path_for(digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectMissing(digest) from exc
        except OSError as exc:
            raise StoreUnavailable(f"The vault is not reachable ({exc.strerror or exc}).") from exc
        if size is not None and len(data) != size:
            raise ObjectCorrupt(digest, f"{len(data)} bytes instead of {size}")
        if sha256(data) != digest:
            raise ObjectCorrupt(digest)
        return data

    def size_of(self, digest: str) -> int | None:
        """The object's size on disk without reading it; None when it is absent."""
        try:
            return self.path_for(digest).stat().st_size
        except FileNotFoundError:
            return None

    def digests(self) -> Iterator[str]:
        """Every object present (names only; nothing is read)."""
        try:
            fans = sorted(self.objects.iterdir())
        except FileNotFoundError:
            return
        for fan in fans:
            if not (fan.is_dir() and _FANOUT.fullmatch(fan.name)):
                continue
            for p in sorted(fan.iterdir()):
                if is_digest(p.name) and p.name.startswith(fan.name) and p.is_file():
                    yield p.name

    def quarantine(self, digest: str, reason: str) -> Path:
        """Rename the object aside to ``quarantine/<sha256>.<n>``; never deleted."""
        src = self.path_for(digest)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        n = 1
        while (self.quarantine_dir / f"{digest}.{n}").exists():
            n += 1
        dest = self.quarantine_dir / f"{digest}.{n}"
        os.rename(src, dest)
        safety.fsync_dir(self.quarantine_dir)
        log.warning("quarantined the store object %s as %s: %s", digest[:12], dest.name, reason)
        return dest

    def _quarantine_if_present(self, digest: str, reason: str) -> None:
        try:
            self.quarantine(digest, reason)
        except FileNotFoundError:
            pass    # another process moved it aside first

    def _fault(self, point: str) -> None:
        if self._faults is not None:
            self._faults(point)


def _hash_file(path: Path) -> str | None:
    try:
        return safety.sha256_file(path)
    except FileNotFoundError:
        return None


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove the store temp file %s: %s", path.name, exc)
