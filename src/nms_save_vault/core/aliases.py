"""Display aliases for account identifiers.

The save folders are named after the account that owns them -- ``st_<steamid64>`` for
Steam, ``<xuid>_<titleid>`` for Xbox / Game Pass -- so the raw account id would otherwise
appear in folder names, paths, source labels and operation details all over both
front-ends. This module lets the user map each account id to a display name and hands the
front-ends one filter, :func:`redact`, that they put every user-visible string through.

The mapping lives in a plain ``accounts.ini`` next to ``state.json`` (see
:func:`default_path`)::

    [accounts]
    76561197975032661 = Main
    000901F0DD67CC4E_29070100B936489ABCE8B9AF3980429C = Xbox

Only *display* is affected. ``state.json``, ``catalog.json`` and every path the app opens
keep the real identifiers -- they are data, not presentation.

A malformed ``accounts.ini`` raises rather than being swallowed: recovering by returning an
empty map would silently fail *open* and print the very ids the user asked to hide.
"""
from __future__ import annotations

import configparser
import os
import re
from pathlib import Path

from .state import install_dir

SECTION = "accounts"
FILENAME = "accounts.ini"

_HEADER = """\
# NMS Save Vault -- account display names.
#
# Each line maps a real account identifier to the name shown in the app instead.
# Steam accounts are the 17-digit steamid64 from the st_<id> folder; Xbox / Game Pass
# accounts are the wgs folder name. Editing this file by hand is fine; the app also
# writes it from Accounts... in the GUI and `nmsvault accounts --set` in the CLI.

"""


class AliasConfigError(Exception):
    """``accounts.ini`` could not be parsed."""


class AliasMap:
    """Account id -> display name, with a compiled filter over arbitrary text."""

    def __init__(self, entries: dict[str, str] | None = None):
        self._entries: dict[str, str] = dict(entries or {})
        self._recompile()

    # --- content -------------------------------------------------------------

    @property
    def entries(self) -> dict[str, str]:
        return dict(self._entries)

    def alias_for(self, account: str) -> str:
        return self._entries.get(account, "")

    def set(self, account: str, alias: str) -> None:
        """Set an alias, or drop the entry when ``alias`` is blank."""
        account = account.strip()
        if not account:
            return
        if alias.strip():
            self._entries[account] = alias.strip()
        else:
            self._entries.pop(account, None)
        self._recompile()

    def clear(self, account: str) -> None:
        self._entries.pop(account.strip(), None)
        self._recompile()

    # --- the filter ----------------------------------------------------------

    def _recompile(self) -> None:
        pairs: list[tuple[str, str]] = []
        for account, alias in self._entries.items():
            alias = alias.strip()
            if not alias:
                continue
            for pattern in _patterns_for(account):
                pairs.append((pattern, alias))
        # Longest first: an alternation matches its first viable branch, so "st_<id>" must
        # be tried before the bare "<id>" or the folder would redact to "st_Alias".
        pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
        self._lookup = {pattern.lower(): alias for pattern, alias in pairs}
        self._rx = (
            re.compile("|".join(re.escape(p) for p, _ in pairs), re.IGNORECASE)
            if pairs
            else None
        )

    def redact(self, text):
        """Replace every configured account id in ``text`` with its display name.

        Non-string values pass through untouched so callers can filter mixed argument
        lists without type-checking at each site.
        """
        if self._rx is None or not isinstance(text, str) or not text:
            return text
        return self._rx.sub(lambda m: self._lookup[m.group(0).lower()], text)


def _patterns_for(account: str) -> list[str]:
    """The on-screen spellings of one account id: the id itself, plus the Steam folder
    name that wraps it (so the whole ``st_<id>`` reads as the alias, not ``st_Alias``)."""
    account = account.strip()
    if not account:
        return []
    patterns = [account]
    if not account.lower().startswith("st_"):
        patterns.append(f"st_{account}")
    return patterns


# --- persistence -------------------------------------------------------------


def default_path() -> Path:
    """``accounts.ini`` sits beside ``state.json`` in the install directory."""
    return install_dir() / FILENAME


def load(path: str | Path | None = None) -> AliasMap:
    """Load the alias map, or an empty one when the file does not exist yet."""
    p = Path(path) if path else default_path()
    if not p.is_file():
        return AliasMap()
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # account ids are case-sensitive hex; do not lower-case them
    try:
        parser.read_string(p.read_text("utf-8"))
    except (configparser.Error, UnicodeDecodeError) as exc:
        raise AliasConfigError(f"{p}: {exc}") from exc
    return AliasMap(dict(parser[SECTION]) if parser.has_section(SECTION) else {})


def save(amap: AliasMap, path: str | Path | None = None) -> Path:
    """Write the alias map atomically (temp + replace), creating the folder if needed."""
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{account} = {alias}" for account, alias in sorted(amap.entries.items())]
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(_HEADER + f"[{SECTION}]\n" + "\n".join(lines) + "\n", "utf-8")
    os.replace(tmp, p)
    return p


# --- process-wide active map -------------------------------------------------
#
# Redaction is a cross-cutting presentation concern: it applies to every string either
# front-end emits, so threading a map through every print and widget would add noise at
# hundreds of call sites for no gain. The map is loaded once and refreshed explicitly
# through reload() when the user edits it.

_active: AliasMap | None = None


def active() -> AliasMap:
    global _active
    if _active is None:
        _active = load()
    return _active


def set_active(amap: AliasMap) -> None:
    global _active
    _active = amap


def reload(path: str | Path | None = None) -> AliasMap:
    """Re-read the file (after the user edits the aliases) and make it active."""
    set_active(load(path))
    return active()


def redact(text):
    """Filter one user-visible value through the active alias map."""
    return active().redact(text)
