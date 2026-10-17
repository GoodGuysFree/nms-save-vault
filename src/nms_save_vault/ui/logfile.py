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
import re
import sys
import threading
from pathlib import Path

from ..core import aliases, safety

FILENAME = "nmsvault.log"
MAX_BYTES = 1_000_000
BACKUPS = 2
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

#: The loggers whose records go to the file: the app's own, and Python's warnings.
LOGGER = "nms_save_vault"

STEAM_ID = aliases.STEAM_ID
XBOX_ACCOUNT = aliases.XBOX_ACCOUNT
_home_pattern = aliases.home_pattern


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
        return aliases.scrub(text, self._home)


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
        self._lock = threading.Lock()          # threads write to stderr at once
        self._busy = threading.local()

    def write(self, text: str) -> int:
        if getattr(self._busy, "on", False):
            return len(text)
        self._busy.on = True
        try:
            with self._lock:
                self._pending += text
                *lines, self._pending = self._pending.split("\n")
            for line in lines:                 # logged outside the lock: a handler is slow
                if line.strip():
                    self.logger.warning("%s", line)
        except Exception:  # noqa: BLE001 - writing to stderr never raises
            pass
        finally:
            self._busy.on = False
        return len(text)

    def flush(self) -> None:
        with self._lock:
            rest, self._pending = self._pending, ""
        if rest.strip():
            self.write(rest + "\n")

    def isatty(self) -> bool:
        return False


def configure(log_dir: Path, *, level: int = logging.INFO,
              home: Path | None = None) -> Path | None:
    """Log the app's records to ``log_dir/nmsvault.log`` (rotated at about 1 MB, two old
    files kept). Idempotent. ``home`` (default: the user's home folder) is shown as ``~``.
    Returns the file, or None when the folder cannot be made - the app then runs without a
    log rather than not at all."""
    path = Path(log_dir) / FILENAME
    logger = logging.getLogger(LOGGER)
    for handler in logger.handlers:
        name = getattr(handler, "baseFilename", None)
        if name and safety.same_path(name, path):
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
