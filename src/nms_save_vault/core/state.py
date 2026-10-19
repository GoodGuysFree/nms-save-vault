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
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import platform

log = logging.getLogger(__name__)

STATE_VERSION = 2  # v2: Xbox sources are writable (same-platform). See _migrate().

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


def _float_or(value, fallback: float) -> float:
    """state.json is hand-editable, so a non-numeric value must not stop the app."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


@dataclass
class AppState:
    """The whole on-disk configuration."""

    sources: list[Source] = field(default_factory=list)
    vault: str | None = None
    version: int = STATE_VERSION
    # UI preferences. "system" follows the Windows light/dark setting.
    theme: str = "system"
    # Font zoom factor (Ctrl+/Ctrl-); 1.0 is the platform's own font size.
    font_scale: float = 1.0
    # Update checking: "ask" until the user answers, then "on" or "off". This is the only
    # thing that makes the app contact the network, so it stays off until asked.
    update_check: str = "ask"
    update_last_check: str = ""  # ISO date of the last check; throttles to once a day
    # Per-machine window preferences of the GUI (D6 3.15): last tab, live-area mode, sort,
    # columns, ... A plain dict the GUI owns; this module only keeps it.
    ui: dict = field(default_factory=dict)

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
            "theme": self.theme,
            "font_scale": self.font_scale,
            "update_check": self.update_check,
            "update_last_check": self.update_last_check,
            "ui": self.ui,
            "sources": [asdict(s) for s in self.sources],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AppState":
        state = cls(
            sources=[_source_from_dict(s) for s in d.get("sources", [])],
            vault=d.get("vault"),
            version=d.get("version", STATE_VERSION),
            theme=d.get("theme", "system"),
            font_scale=_float_or(d.get("font_scale"), 1.0),
            update_check=d.get("update_check", "ask"),
            update_last_check=d.get("update_last_check", ""),
            ui=d["ui"] if isinstance(d.get("ui"), dict) else {},
        )
        _migrate(state)
        return state


def _migrate(state: "AppState") -> None:
    """Heal configs written by older app versions, in place, on load.

    v1 -> v2: same-platform Xbox writes are now supported, but v1 saved every Xbox source as
    read-only (``writable=False``). Flip them so existing installs gain the feature on the
    next launch without a manual rescan."""
    if state.version < 2:
        for s in state.sources:
            if s.platform == PLATFORM_XBOX:
                s.writable = True
    state.version = STATE_VERSION


def install_dir() -> Path:
    """The directory the app runs from -- where the config is kept.

    The packaged app is a portable runtime whose launcher is a renamed copy of the
    signed CPython executable, so there is no ``sys.frozen`` to test. Its bootstrap
    (``packaging/sitecustomize.py``) exports ``NMSVAULT_PORTABLE_ROOT``, the folder
    holding that launcher (the install dir, e.g.
    ``%LOCALAPPDATA%\\Programs\\NMSSaveVault``), so the config sits next to the
    program and is portable with it. When running from source we keep the config out
    of the source tree, in a dedicated per-user folder.
    """
    portable_root = os.environ.get("NMSVAULT_PORTABLE_ROOT")
    if portable_root:
        return Path(portable_root)
    return platform.user_config_dir()


def default_state_path() -> Path:
    """``state.json`` lives in the install directory (next to the executable)."""
    return install_dir() / "state.json"


_notices: list[str] = []


def take_notices() -> list[str]:
    """Plain messages about what load() had to do (a damaged state.json put aside); each is
    returned once, for the front end to show."""
    out, _notices[:] = list(_notices), []
    return out


def load(path: str | Path | None = None) -> AppState | None:
    """Load state, or ``None`` if the file does not exist yet (first run) or could not be read:
    a damaged state.json is moved aside (``state.json.corrupt-<time>``, never deleted), said in
    :func:`take_notices`, and the app starts as on a first run."""
    p = Path(path) if path else default_state_path()
    if not p.is_file():
        return None
    try:
        raw = p.read_bytes()
    except OSError as exc:
        from .catalog import OperationError

        raise OperationError(f"The settings file ({p.name}) cannot be read "
                             f"({exc.strerror or type(exc).__name__}); it was not changed.") from exc
    try:
        # utf-8-sig: a state.json saved by an editor that adds a BOM still loads.
        return AppState.from_dict(json.loads(raw.decode("utf-8-sig")))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:    # incl. bad UTF-8
        aside = p.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        n = 2
        while aside.exists():
            aside = p.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
            n += 1
        log.warning("state.json could not be read (%s: %s); kept as %s", type(exc).__name__, exc,
                    aside.name)
        try:
            os.replace(p, aside)
        except OSError as move_error:
            log.warning("could not put the damaged state.json aside: %s", move_error)
            _blocked.add(_key(p))       # the defaults must never be saved over it
            _notices.append("Your settings file (state.json) could not be read, and it could not "
                            "be moved aside either, so the app started with its defaults and "
                            "will not save any settings this session. The file was not changed.")
            return None
        _notices.append(f"Your settings file (state.json) could not be read. It was kept as "
                        f"{aside.name} next to it, and the app started with its defaults and "
                        "the save folders it found.")
        return None


_blocked: set[str] = set()      # state files that must not be written this session


def _key(p: Path) -> str:
    return os.path.normcase(os.path.abspath(p))


def save(state: AppState, path: str | Path | None = None) -> Path:
    """Write state atomically: temp file, fsync, replace, creating the folder if needed."""
    from . import safety

    p = Path(path) if path else default_state_path()
    if _key(p) in _blocked:
        log.warning("not saving %s: the damaged file could not be put aside", p.name)
        return p
    safety.atomic_write_bytes(p, json.dumps(state.to_dict(), indent=2).encode("utf-8"),
                              temp_prefix=p.name + ".tmp_")
    return p
