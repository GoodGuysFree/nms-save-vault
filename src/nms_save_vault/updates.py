"""Check GitHub for a newer release.

This is the only part of the app that touches the network, so it is opt-in: the first run
asks, the answer is stored in ``state.json`` as ``update_check`` (``ask`` / ``on`` /
``off``), and nothing is contacted until the answer is ``on``. Checks are throttled to
once a day.

Nothing is downloaded or installed here -- the app only tells the user a newer version
exists and offers to open the releases page in their browser.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date

from . import __version__

# Values for AppState.update_check
ASK = "ask"
ON = "on"
OFF = "off"

API_LATEST = "https://api.github.com/repos/GoodGuysFree/nms-save-vault/releases/latest"
RELEASES_PAGE = "https://github.com/GoodGuysFree/nms-save-vault/releases/latest"

TIMEOUT_SECONDS = 6
_USER_AGENT = f"NMSSaveVault/{__version__} (+{RELEASES_PAGE})"  # GitHub rejects requests without one

_NUMBER = re.compile(r"\d+")


class UpdateCheckError(Exception):
    """The check could not be completed (offline, rate-limited, bad response, ...)."""


@dataclass(frozen=True)
class Release:
    version: str
    page_url: str
    name: str = ""


def parse_version(text: str) -> tuple[int, ...]:
    """``"v0.0.10"`` -> ``(0, 0, 10)``. Non-numeric junk is ignored rather than fatal."""
    return tuple(int(n) for n in _NUMBER.findall(text or ""))


def is_newer(candidate: str, current: str = __version__) -> bool:
    """True if ``candidate`` is a strictly higher version than ``current``.

    Compared component-wise as integers, so 0.0.10 correctly beats 0.0.9 (string
    comparison would not).
    """
    a, b = parse_version(candidate), parse_version(current)
    if not a or not b:
        return False  # unparseable: never nag on a guess
    length = max(len(a), len(b))
    return a + (0,) * (length - len(a)) > b + (0,) * (length - len(b))


def due(last_check: str, today: date | None = None) -> bool:
    """True if a check has not yet run today. A blank or unparseable date means 'due'."""
    today = today or date.today()
    try:
        return date.fromisoformat(last_check) < today
    except (TypeError, ValueError):
        return True


def fetch_latest(url: str = API_LATEST, timeout: int = TIMEOUT_SECONDS) -> Release:
    """Ask GitHub for the latest release. Raises :class:`UpdateCheckError` on any failure."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UpdateCheckError(f"GitHub returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateCheckError(f"could not reach GitHub: {exc}") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateCheckError(f"unexpected response from GitHub: {exc}") from exc

    tag = payload.get("tag_name") or payload.get("name") or ""
    if not tag:
        raise UpdateCheckError("GitHub returned a release with no version tag")
    return Release(
        version=tag.lstrip("vV"),
        page_url=payload.get("html_url") or RELEASES_PAGE,
        name=payload.get("name") or "",
    )


def check(current: str = __version__, url: str = API_LATEST) -> Release | None:
    """The newer release, or ``None`` if this build is already current."""
    latest = fetch_latest(url)
    return latest if is_newer(latest.version, current) else None
