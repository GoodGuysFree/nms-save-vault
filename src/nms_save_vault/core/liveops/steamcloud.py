"""Steam's own record of what it has uploaded (D5 2.12.2): ``remotecache.vdf``.

Read-only and optional. It gives "uploaded" evidence for verification and the Steam
``PENDING_UPLOAD`` hint. Absent or unparsable means no evidence, never an error. Nothing
read here is displayed: the path and the file keys hold the account id.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .. import platform

STEAM_ID64_BASE = 76561197960265728
SYNCED = 1                              # ``syncstate`` of a file Steam has uploaded
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*')
_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


@dataclass(frozen=True)
class ManifestEntry:
    size: int
    sha: str                            # lower-case SHA-1 of the file Steam last synced
    syncstate: int
    remotetime: int

    @property
    def synced(self) -> bool:
        return self.syncstate == SYNCED


def parse_vdf(text: str) -> dict:
    """Valve's text key-values format as nested dicts (values are strings). Raises
    ``ValueError`` on unbalanced braces; a repeated key keeps its last value."""
    root: dict = {}
    stack = [root]
    key: str | None = None
    for m in _TOKEN.finditer(text):
        if m.group(1) is not None:                         # a quoted string
            word = re.sub(r"\\(.)", lambda e: _ESCAPES.get(e.group(1), e.group(1)), m.group(1))
            if key is None:
                key = word
            else:
                stack[-1][key] = word
                key = None
        elif m.group(2) == "{":
            if key is None:
                raise ValueError("block without a key")
            child: dict = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        elif m.group(2) == "}":
            if len(stack) == 1 or key is not None:
                raise ValueError("unbalanced braces")
            stack.pop()
    if len(stack) != 1 or key is not None:
        raise ValueError("unbalanced braces")
    return root


def accountid32(account_key: str) -> int | None:
    """``st_<steamid64>`` -> the 32-bit account id of Steam's ``userdata`` folder."""
    m = re.fullmatch(r"st_(\d{17})", account_key)
    if not m:
        return None
    value = int(m.group(1)) - STEAM_ID64_BASE
    return value if value > 0 else None


def read_manifest(steam_root: Path, account32: int) -> dict[str, ManifestEntry]:
    """Every file Steam tracks for the game under ``steam_root``, by the key Steam uses
    (``HelloGames/NMS/<account>/<file>``). ``{}`` when the file is absent or unreadable."""
    path = steam_root / "userdata" / str(account32) / platform.STEAM_APP_ID / "remotecache.vdf"
    try:
        tree = parse_vdf(path.read_text("utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    body = next((v for v in tree.values() if isinstance(v, dict)), {})
    out = {}
    for key, node in body.items():
        if not isinstance(node, dict):
            continue
        try:
            out[key] = ManifestEntry(int(node.get("size", "0")), node.get("sha", "").lower(),
                                     int(node.get("syncstate", "0")),
                                     int(node.get("remotetime", "0")))
        except ValueError:
            continue
    return out


def steam_install_roots(host: platform.Host) -> list[Path]:
    """Steam installs to look in: the ones ``platform.steam_roots`` finds, plus the Windows
    default places."""
    roots = list(platform.steam_roots(host))
    if host.system == platform.WINDOWS:
        for var in ("ProgramFiles(x86)", "ProgramFiles"):
            base = host.env.get(var)
            if base and (Path(base) / "Steam").is_dir():
                roots.append(Path(base) / "Steam")
    seen, out = set(), []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def account_manifest(host: platform.Host, account_key: str) -> dict[str, ManifestEntry]:
    """The manifest of one Steam account, keyed by bare file name; ``{}`` when unknown."""
    id32 = accountid32(account_key)
    if id32 is None:
        return {}
    for root in steam_install_roots(host):
        manifest = read_manifest(root, id32)
        if manifest:
            return {k.rsplit("/", 1)[-1]: e for k, e in manifest.items()
                    if k.rsplit("/", 2)[-2:-1] == [account_key]}
    return {}


def upload_state(manifest: Mapping[str, ManifestEntry], folder: Path,
                 names: list[str]) -> str | None:
    """``"uploaded"`` when every file is tracked, synced and byte-identical (SHA-1) to what
    Steam holds; ``"pending"`` when any differs or is untracked; None without a manifest."""
    if not manifest or not names:
        return None
    for name in names:
        entry = manifest.get(name)
        try:
            digest = hashlib.sha1((folder / name).read_bytes()).hexdigest()
        except OSError:
            return None
        if entry is None or not entry.synced or entry.sha != digest:
            return "pending"
    return "uploaded"
