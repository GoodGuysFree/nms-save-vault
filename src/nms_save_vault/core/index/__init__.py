"""The content index: per-version facts decoded from save data, cached per machine
(docs/organizer/designs/D4-content-index.md).

The public API is re-exported here; front-ends use :class:`IndexService`.
"""
from __future__ import annotations

from .changes import Change, ChangeSet
from .derive import FactsCard, FactsView
from .pool import ExecutionPolicy, Priority
from .service import IndexEvent, IndexProgress, IndexService

__all__ = ["Change", "ChangeSet", "ExecutionPolicy", "FactsCard", "FactsView", "IndexEvent", "IndexProgress",
           "IndexService", "Priority"]
