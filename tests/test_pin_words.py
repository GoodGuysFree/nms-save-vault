"""A pinned version says "pinned" everywhere (GGF 2026-11-20, "yes" to renaming lock to pin):
the Notes window, the Organizer table, the detail pane and Compare, as History's Pin / Unpin and
the Backups list do; the CLI too (``note --pin``; ``--lock`` still works)."""
from __future__ import annotations

import pytest

from nms_save_vault.core import safety
from nms_save_vault.ui.organizer import compare, table
from nms_save_vault.ui.organizer import detail as D

from .organizer_world import PIDS, build_world


@pytest.fixture
def world(tmp_path, make_wgs_account, monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    w = build_world(tmp_path, make_wgs_account)
    oldest = w.model.versions(PIDS["main"])[-1]
    w.organizer.notes.set_version_note(oldest.head, pair=oldest.pair, pid=PIDS["main"],
                                       locked=True)
    w.refresh()
    return w


def _plain(segments) -> str:
    return "".join(text for text, _ in segments)


def _rows(world):
    return world.model.versions(PIDS["main"])                 # newest first


def test_the_detail_pane_says_pinned(world):
    summary = _plain(D.line_summary(world.model, PIDS["main"]))
    assert "1 pinned" in summary and "locked" not in summary.lower()
    card = _plain(D.version_card(world.model, PIDS["main"], _rows(world)[-1].head))
    assert "Pinned\tthinning never deletes it" in card and "lock" not in card.lower()


def test_the_table_says_pinned(world):
    state = table.version_values(_rows(world)[-1], {})["state"]
    assert "pinned" in state and "lock" not in state


def test_compare_says_pinned(world):
    newest, oldest = _rows(world)
    facts = {f.name: (f.a, f.b) for f in compare.compare_facts(world.model, PIDS["main"],
                                                                newest, oldest)}
    assert facts.get("Pinned") == ("no", "yes") and "Locked" not in facts
