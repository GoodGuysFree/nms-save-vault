"""Named crash points for the crash-safety tests (D5 5.1). Production passes no injector.

:class:`SimulatedCrash` is a ``BaseException``: no ``except Exception`` in the code under
test can catch it, so it stops the operation exactly where a kill would.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping


class SimulatedCrash(BaseException):
    """The process "died" at the named point."""


class FaultInjector:
    """``crash_at``: points that raise :class:`SimulatedCrash`. ``actions``: callables run
    when their point is reached (e.g. corrupt a file after the swap), before any crash."""

    def __init__(self, crash_at: Iterable[str] = (),
                 actions: Mapping[str, Callable[[], None]] | None = None) -> None:
        self.crash_at = set(crash_at)
        self.actions = dict(actions or {})
        self.seen: list[str] = []

    def at(self, point: str) -> None:
        self.seen.append(point)
        action = self.actions.get(point)
        if action is not None:
            action()
        if point in self.crash_at:
            raise SimulatedCrash(point)


def at(faults: FaultInjector | None, point: str) -> None:
    """Pass ``point`` (no-op without an injector)."""
    if faults is not None:
        faults.at(point)
