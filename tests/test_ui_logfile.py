"""The GUI's log file: under pythonw (no console) warnings and tracebacks are kept in a
rotating file in the per-machine folder, with account ids redacted (D6 U4, R3 K6)."""
from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

import pytest

from nms_save_vault.core import aliases
from nms_save_vault.ui import logfile

ACCOUNT = "st_76561198000000001"


@pytest.fixture
def clean_logger():
    """Each test starts with the app's logger as the suite found it, and leaves it so."""
    logger = logging.getLogger(logfile.LOGGER)
    warn_logger = logging.getLogger("py.warnings")
    before = (list(logger.handlers), logger.level, list(warn_logger.handlers))
    yield logger
    for handler in logger.handlers[len(before[0]):]:
        handler.close()
    logger.handlers[:] = before[0]
    logger.setLevel(before[1])
    warn_logger.handlers[:] = before[2]
    logging.captureWarnings(False)


def _flush(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        handler.flush()


def test_warnings_from_the_core_land_in_the_file(tmp_path, clean_logger):
    path = logfile.configure(tmp_path / "logs")
    assert path == tmp_path / "logs" / logfile.FILENAME
    logging.getLogger("nms_save_vault.core.catalog").warning("removed %s", "a stale temp file")
    _flush(clean_logger)
    text = path.read_text(encoding="utf-8")
    assert "WARNING nms_save_vault.core.catalog: removed a stale temp file" in text


def test_configuring_twice_adds_one_handler(tmp_path, clean_logger):
    before = len(clean_logger.handlers)
    logfile.configure(tmp_path)
    logfile.configure(tmp_path)
    assert len(clean_logger.handlers) == before + 1


def test_account_ids_are_redacted_in_the_log(tmp_path, clean_logger, monkeypatch):
    names = aliases.AliasMap()
    names.set(ACCOUNT, "Steam main")
    monkeypatch.setattr(aliases, "_active", names)
    path = logfile.configure(tmp_path)
    logging.getLogger("nms_save_vault.gui").warning("could not read C:/x/%s/save.hg", ACCOUNT)
    _flush(clean_logger)
    text = path.read_text(encoding="utf-8")
    assert "Steam main" in text and "76561198000000001" not in text


def test_without_a_console_stderr_writes_into_the_log(tmp_path, clean_logger, monkeypatch):
    """pythonw: sys.stderr is None, so a Tk callback's traceback (written to stderr) was
    lost and http.server's request log raised."""
    monkeypatch.setattr(sys, "stderr", None)
    path = logfile.configure(tmp_path)
    assert sys.stderr is not None and not sys.stderr.isatty()
    sys.stderr.write("Traceback (most recent call last):\n  File \"x\", line 1\nBoom")
    sys.stderr.flush()
    _flush(clean_logger)
    text = path.read_text(encoding="utf-8")
    assert "Traceback (most recent call last):" in text and "Boom" in text


def test_a_console_keeps_its_stderr(tmp_path, clean_logger):
    console = sys.stderr
    logfile.configure(tmp_path)
    assert sys.stderr is console


def test_python_warnings_are_kept_too(tmp_path, clean_logger):
    path = logfile.configure(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("the op log could not be written", UserWarning)
    for handler in logging.getLogger("py.warnings").handlers:
        handler.flush()
    assert "the op log could not be written" in path.read_text(encoding="utf-8")


def test_an_unwritable_folder_means_no_log_not_no_app(tmp_path, clean_logger):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert logfile.configure(blocker / "logs") is None


def test_the_file_rotates(tmp_path, clean_logger, monkeypatch):
    monkeypatch.setattr(logfile, "MAX_BYTES", 2_000)
    path = logfile.configure(tmp_path)
    log = logging.getLogger("nms_save_vault.test")
    for n in range(200):
        log.warning("line %03d %s", n, "x" * 40)
    _flush(clean_logger)
    assert (tmp_path / f"{logfile.FILENAME}.1").is_file()
    assert not (tmp_path / f"{logfile.FILENAME}.3").exists()
    assert path.stat().st_size <= 2_000


# --- chunk D review ----------------------------------------------------------------


def test_every_steam_and_xbox_id_and_the_home_folder_are_redacted(tmp_path, clean_logger,
                                                                   monkeypatch):
    """Named or not: an id without an alias is still an id."""
    monkeypatch.setattr(aliases, "_active", aliases.AliasMap())
    home = tmp_path / "Users" / "someone"
    path = logfile.configure(tmp_path / "logs", home=home)
    xbox = "0000000000000001_29070100B936489ABCE8B9AF3980429C"
    logging.getLogger("nms_save_vault.x").warning(
        "copy %s/AppData/Roaming/HelloGames/NMS/st_76561198000000002/save.hg and %s/wgs/%s",
        home, str(home).upper(), xbox)
    _flush(clean_logger)
    text = path.read_text(encoding="utf-8")
    assert "76561198000000002" not in text and xbox not in text
    assert str(home) not in text and str(home).upper() not in text
    assert "~/AppData/Roaming/HelloGames/NMS/st_<steam id>/save.hg" in text.replace("\\", "/")
    assert "<xbox account>" in text


def test_a_traceback_through_stderr_is_redacted(tmp_path, clean_logger, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    path = logfile.configure(tmp_path, home=tmp_path / "home")
    sys.stderr.write(f'  File "{tmp_path / "home" / "x.py"}", line 3, st_76561198000000003\n')
    _flush(clean_logger)
    text = path.read_text(encoding="utf-8")
    assert "76561198000000003" not in text and str(tmp_path / "home") not in text


def test_a_failing_handler_never_loops_or_raises(tmp_path, clean_logger, monkeypatch):
    """pythonw: the stock handleError prints to stderr, which is the log itself."""
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    path = logfile.configure(tmp_path)
    (handler,) = [h for h in clean_logger.handlers if getattr(h, "baseFilename", None)]

    def refuse(record):
        raise PermissionError(13, "the log is open in another program")

    monkeypatch.setattr(handler, "shouldRollover", refuse)
    logging.getLogger("nms_save_vault.y").warning("one")         # must simply return
    sys.stderr.write("a line on stderr\n")
    assert handler.failures >= 2

    def broken(text):
        raise RuntimeError("redaction failed")

    monkeypatch.setattr(handler, "shouldRollover", lambda record: False)
    monkeypatch.setattr(handler.formatter, "redact", broken)
    logging.getLogger("nms_save_vault.y").warning("two")
    assert handler.failures >= 3
    assert path.parent.is_dir()


def test_a_stderr_line_written_while_logging_is_dropped(tmp_path, clean_logger, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    logfile.configure(tmp_path)
    stream = sys.stderr
    calls = []

    class Echo(logging.Handler):            # a handler that prints what it handles
        def emit(self, record):
            calls.append(record.getMessage())
            stream.write(f"echo: {record.getMessage()}\n")

    echo = Echo()
    clean_logger.addHandler(echo)
    try:
        stream.write("hello\n")
    finally:
        clean_logger.removeHandler(echo)
    assert calls == ["hello"]                 # the echo was not logged again


def test_the_same_file_spelled_differently_is_one_handler(tmp_path, clean_logger):
    before = len(clean_logger.handlers)
    logfile.configure(tmp_path / "logs")
    logfile.configure(tmp_path / "logs" / ".." / "logs")
    if sys.platform == "win32":
        logfile.configure(Path(str(tmp_path / "logs").upper()))
    assert len(clean_logger.handlers) == before + 1


@pytest.mark.skipif(sys.platform != "win32", reason="a junction is a Windows folder link")
def test_a_folder_reached_through_a_junction_gets_one_handler(tmp_path, clean_logger):
    """``baseFilename`` is the absolute path as given; comparing it with a resolved one
    took the same folder for another when the path went through a junction."""
    import _winapi

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _winapi.CreateJunction(str(real), str(link))
    before = len(clean_logger.handlers)
    logfile.configure(link / "logs")
    logfile.configure(link / "logs")
    assert len(clean_logger.handlers) == before + 1
