"""The GUI's log file (D6 1, R3 K6).

The packaged app runs under ``pythonw``: there is no console, ``sys.stdout`` and
``sys.stderr`` are None, and every warning the core logs ("the live folder was not
journaled", "removed a stale temp file") went nowhere. Worse, anything that writes to
``sys.stderr`` itself - a Tk callback's traceback, ``http.server``'s request log - fails.

:func:`configure` sends the app's log records to a small rotating file in the per-machine
folder and, when there is no ``sys.stderr``, gives the process one that writes into that
log. Every line is redacted on the way in - configured account names, every Steam id and
Xbox account folder id whether named or not, and the user's home folder - so a log can be
attached to a bug report as it is. The log is best effort: a failure to write it (a
rotation the OS refuses, a line that cannot be formatted) is counted and dropped, never
raised into the code that logged, and never logged again.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import threading
from pathlib import Path

from ..core import aliases

FILENAME = "nmsvault.log"
MAX_BYTES = 1_000_000
BACKUPS = 2
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

#: The loggers whose records go to the file: the app's own, and Python's warnings.
LOGGER = "nms_save_vault"

#: A SteamID64 (also inside ``st_<id>`` folder names) and an Xbox account folder name
#: (``<xuid>_<title id>``), wherever they appear.
STEAM_ID = re.compile(r"7656119\d{10}")
XBOX_ACCOUNT = re.compile(r"\b[0-9A-Fa-f]{16}_[0-9A-Fa-f]{32}\b")


def _home_pattern(home: Path) -> re.Pattern | None:
    """The home folder however a message spells it: either slash, any case on Windows."""
    text = str(home).rstrip("\\/")
    if len(text) < 3:
        return None
    parts = [re.escape(p) for p in re.split(r"[\\/]+", text) if p]
    prefix = "/" if text.startswith("/") else ""
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile(prefix + r"[\\/]+".join(parts) + r"(?=[\\/]|$|\W)", flags)


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, home: Path | None = None) -> None:
        super().__init__(fmt)
        self._home = _home_pattern(home) if home is not None else None

    def format(self, record: logging.LogRecord) -> str:
        return self.redact(super().format(record))

    def redact(self, text: str) -> str:
        try:
            text = aliases.redact(text)          # configured names first, readable
        except Exception:  # noqa: BLE001 - a broken alias map must not cost the line
            pass
        text = STEAM_ID.sub("<steam id>", text)
        text = XBOX_ACCOUNT.sub("<xbox account>", text)
        if self._home is not None:
            text = self._home.sub("~", text)
        return text


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating file whose failures stay inside it. The stock ``handleError`` prints the
    failure to ``sys.stderr`` - which under pythonw is this very log, so one refused
    rotation became a loop of failures."""

    failures = 0

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging's name
        self.failures += 1


class _LogStream:
    """A text stream for ``sys.stderr`` that turns whole lines into log records. A line
    written while one is being logged (a handler failing, and printing that) is dropped
    instead of being logged in turn."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self._pending = ""
        self._busy = threading.local()

    def write(self, text: str) -> int:
        if getattr(self._busy, "on", False):
            return len(text)
        self._busy.on = True
        try:
            self._pending += text
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                if line.strip():
                    self.logger.warning("%s", line)
        except Exception:  # noqa: BLE001 - writing to stderr never raises
            self._pending = ""
        finally:
            self._busy.on = False
        return len(text)

    def flush(self) -> None:
        if self._pending.strip():
            self.write("\n")
        self._pending = ""

    def isatty(self) -> bool:
        return False


def _same_file(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def configure(log_dir: Path, *, level: int = logging.INFO,
              home: Path | None = None) -> Path | None:
    """Log the app's records to ``log_dir/nmsvault.log`` (rotated at about 1 MB, two old
    files kept). Idempotent. ``home`` (default: the user's home folder) is shown as ``~``.
    Returns the file, or None when the folder cannot be made - the app then runs without a
    log rather than not at all."""
    path = Path(log_dir) / FILENAME
    logger = logging.getLogger(LOGGER)
    for handler in logger.handlers:
        if _same_file(getattr(handler, "baseFilename", ""), path):
            return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = SafeRotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True)
    except OSError:
        return None
    handler.setFormatter(RedactingFormatter(FORMAT, Path.home() if home is None else home))
    logger.addHandler(handler)
    logger.setLevel(min(level, logger.level or level))
    logging.captureWarnings(True)
    logging.getLogger("py.warnings").addHandler(handler)
    if sys.stderr is None:          # pythonw: no console to write to
        sys.stderr = _LogStream(logging.getLogger(f"{LOGGER}.stderr"))  # type: ignore[assignment]
    return path
