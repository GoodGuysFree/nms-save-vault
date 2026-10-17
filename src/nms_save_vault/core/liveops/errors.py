"""Exceptions of the live operations (D5 2.14).

``execute`` reports every operational result as an ``OpOutcome``; these are raised only
for caller errors and for a writer that could not finish (the executor then rolls back).
All derive from ``operations.OperationError``, so today's front-end handlers keep working.
"""
from __future__ import annotations

from collections.abc import Iterable

from .. import msstore
from ..operations import OperationError

TARGET_UNAVAILABLE = "That live slot is not available."
XGS_READ_ONLY_TEXT = "This Xbox folder can only be read."


class LiveOpError(OperationError):
    """Base of every live-operation error."""


class TargetError(LiveOpError):
    """A live target that no write may use. ``code`` is stable, ``detail`` says why for the
    log (never a path or an account id), ``text`` is what the user is told."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    @property
    def text(self) -> str:
        return XGS_READ_ONLY_TEXT if self.code == "XGS_READ_ONLY" else TARGET_UNAVAILABLE


class ConfirmationRequired(LiveOpError):
    """``execute`` was called without echoing every confirmation code of the plan."""

    def __init__(self, codes: Iterable[str]) -> None:
        self.codes = frozenset(codes)
        super().__init__("confirmation required: " + ", ".join(sorted(self.codes)))


class WriteError(LiveOpError):
    """A slot writer stopped part-way. ``swapped`` names the live files it had already
    replaced; the caller restores the slot with the writer's exact rollback."""

    def __init__(self, message: str, *, swapped: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.swapped = tuple(swapped)


class LiveChanged(LiveOpError):
    """The live slot changed after it was planned (the game or another tool wrote it). The
    writer wrote nothing; the slot must not be rolled back, it holds someone else's save."""


class RollbackError(LiveOpError, msstore.RollbackError):
    """A rollback cannot put the slot back exactly as it was. Also an
    ``msstore.RollbackError``, so one handler catches the Steam and the Xbox refusals."""
