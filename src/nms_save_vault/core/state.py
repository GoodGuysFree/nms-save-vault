"""Persistent app configuration: the *live* save sources the app manages.

``state.json`` records the live save folders the app knows about -- one entry per
Steam account and per Xbox / Game Pass account -- each tagged with its platform,
the account it belongs to, whether it is writable, and a human label. Backups are
*not* listed here; they are tracked by the catalog (see ``catalog.py``). Keeping
the two apart means the UI can cleanly show "these are your live saves" separately
from "these are your backups".

Discovery (``discover.py``) builds the initial state on first run; thereafter this
file is authoritative and is only refreshed when the user asks for a rescan, which
merges newly found sources without clobbering anything the user has edited.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_VERSION = 1

ROLE_LIVE = "live"

PLATFORM_STEAM = "steam"
PLATFORM_XBOX = "xbox"

ORIGIN_AUTO = "auto"      # added by discovery; a rescan may refresh it
ORIGIN_MANUAL = "manual"  # added/edited by the user; a rescan never touches it


@dataclass
class Source:
    """A live save folder the app manages."""

    id: str
    platform: str
    account: str
    path: str
    label: str = ""
    role: str = ROLE_LIVE
    writable: bool = True
    origin: str = ORIGIN_AUTO
    enabled: bool = True

    @property
    def exists(self) -> bool:
        return bool(self.path) and Path(self.path).is_dir()


def _source_from_dict(d: dict) -> Source:
    return Source(
        id=d["id"],
        platform=d["platform"],
        account=d.get("account", ""),
        path=d["path"],
        label=d.get("label", ""),
        role=d.get("role", ROLE_LIVE),
        writable=d.get("writable", True),
        origin=d.get("origin", ORIGIN_AUTO),
        enabled=d.get("enabled", True),
    )


@dataclass
class AppState:
    """The whole on-disk configuration."""

    sources: list[Source] = field(default_factory=list)
    vault: str | None = None
    version: int = STATE_VERSION

    # --- lookups -------------------------------------------------------------

    @property
    def live_sources(self) -> list[Source]:
        return [s for s in self.sources if s.role == ROLE_LIVE and s.enabled]

    def get(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def upsert(self, source: Source) -> None:
        self.sources = [s for s in self.sources if s.id != source.id]
        self.sources.append(source)

    def remove(self, source_id: str) -> None:
        self.sources = [s for s in self.sources if s.id != source_id]

    # --- (de)serialization ---------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "vault": self.vault,
            "sources": [asdict(s) for s in self.sources],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AppState":
        return cls(
            sources=[_source_from_dict(s) for s in d.get("sources", [])],
            vault=d.get("vault"),
            version=d.get("version", STATE_VERSION),
        )


def install_dir() -> Path:
    """The directory the app runs from -- where the config is kept.

    For the packaged one-file executable this is the folder that contains the
    ``.exe`` (the install dir, e.g. ``%LOCALAPPDATA%\\Programs\\NMSSaveVault``), so
    the config sits next to the program and is portable with it. ``sys.executable``
    points at the real exe, not the temporary one-file unpack dir. When running from
    source we keep the config out of the source tree, in a dedicated per-user folder.
    """
    if getattr(sys, "frozen", False):  # running inside a PyInstaller bundle
        return Path(sys.executable).resolve().parent
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home()
    return base / "NMSSaveVault"


def default_state_path() -> Path:
    """``state.json`` lives in the install directory (next to the executable)."""
    return install_dir() / "state.json"


def load(path: str | Path | None = None) -> AppState | None:
    """Load state, or ``None`` if the file does not exist yet (first run)."""
    p = Path(path) if path else default_state_path()
    if not p.is_file():
        return None
    return AppState.from_dict(json.loads(p.read_text("utf-8")))


def save(state: AppState, path: str | Path | None = None) -> Path:
    """Write state atomically (temp + replace), creating the folder if needed."""
    p = Path(path) if path else default_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2), "utf-8")
    os.replace(tmp, p)
    return p
