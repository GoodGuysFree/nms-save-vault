"""The GUI's log file (D6 1, R3 K6).

The packaged app runs under ``pythonw``: there is no console, ``sys.stdout`` and
``sys.stderr`` are None, and every warning the core logs ("the live folder was not
journaled", "removed a stale temp file") went nowhere. Worse, anything that writes to
``sys.stderr`` itself - a Tk callback's traceback, ``http.server``'s request log - fails.

:func:`configure` sends the app's log records to a small rotating file in the per-machine
folder and, when there is no ``sys.stderr``, gives the process one that writes into that
log. Account ids are redacted on the way in, like everything else the app shows, so a log
can be attached to a bug report as it is.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from ..core import aliases

FILENAME = "nmsvault.log"
MAX_BYTES = 1_000_000
BACKUPS = 2
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

#: The loggers whose records go to the file: the app's own, and Python's warnings.
LOGGER = "nms_save_vault"


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return aliases.redact(super().format(record))


class _LogStream:
    """A text stream for ``sys.stderr`` that turns whole lines into log records."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            if line.strip():
                self.logger.warning("%s", line)
        return len(text)

    def flush(self) -> None:
        if self._pending.strip():
            self.logger.warning("%s", self._pending)
        self._pending = ""

    def isatty(self) -> bool:
        return False


def configure(log_dir: Path, *, level: int = logging.INFO) -> Path | None:
    """Log the app's records to ``log_dir/nmsvault.log`` (rotated at about 1 MB, two old
    files kept). Idempotent. Returns the file, or None when the folder cannot be made -
    the app then runs without a log rather than not at all."""
    path = Path(log_dir) / FILENAME
    logger = logging.getLogger(LOGGER)
    for handler in logger.handlers:
        if getattr(handler, "baseFilename", None) == str(path.resolve()):
            return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True)
    except OSError:
        return None
    handler.setFormatter(RedactingFormatter(FORMAT))
    logger.addHandler(handler)
    logger.setLevel(min(level, logger.level or level))
    logging.captureWarnings(True)
    logging.getLogger("py.warnings").addHandler(handler)
    if sys.stderr is None:          # pythonw: no console to write to
        sys.stderr = _LogStream(logging.getLogger(f"{LOGGER}.stderr"))  # type: ignore[assignment]
    return path
