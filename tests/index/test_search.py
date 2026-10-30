"""Search over the read model and the content index's stored facts (``core/index/search.py``)."""
from __future__ import annotations

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core.index import search as S

from ..organizer_world import PIDS, build_world
from .test_derive import facts_for


@pytest.fixture
def world(tmp_path, make_wgs_account, monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    return build_world(tmp_path, make_wgs_account)


def _by_field(items) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for item in items:
        out.setdefault(item.field, []).append((item.value, item.detail))
    return out


def test_every_text_the_index_holds_is_named_as_the_views_name_it():
    by = _by_field(S.facts_items(facts_for("rich_base")))
    assert by[S.BASE_GALAXY] == [("10-Eissentam", "Synth Home"), ("10-Eissentam", "Synth Outpost"),
                                 ("21-Loychazinq", "Far Synth")]    # planet bases only
    assert ("Synth Home", "Planet base, 10-Eissentam") in by[S.BASE]
    assert ("Big Synth", "Corvette layout") in by[S.BASE] and ("Orphan", "Corvette layout") not in by[S.BASE]
    assert by[S.GALAXY] == [("10-Eissentam", "current galaxy")]
    assert by[S.SAVE_NAME] == [("Synth Main", "")]
    assert by[S.LOCATION] == [("On Planet (Synthia)", "")]
    assert [name for name, _ in by[S.SHIP]] == ["Synth Runner", "Big Synth"]
    assert [name for name, _ in by[S.ARCHIVED_SHIP]] == ["Old Synth"]
    assert [name for name, _ in by[S.COMPANION]][0] == "Biscuit"
    assert by[S.FREIGHTER] == [("Synth Hauler", "")]
    assert by[S.MULTITOOL] == [("Synth Staff", "")]
    assert by[S.SETTLEMENT] == [("Synth Haven", "")]
    assert S.EXPEDITION not in by                                     # SeasonId 0


def test_an_expedition_and_its_own_galaxy_are_found():
    by = _by_field(S.facts_items(facts_for("base_and_expedition")))
    assert by[S.EXPEDITION] == [("Swarm", "expedition 22")]
    assert ("1-Euclid", "in the expedition") in by[S.GALAXY]
    assert ("10-Eissentam", "current galaxy") in by[S.GALAXY]


def test_which_save_file_has_bases_in_a_galaxy(world):
    model = world.model
    newest = model.versions(PIDS["main"])[0].head
    stored = {newest: facts_for("rich_base")}
    found = S.search(model, "eissentam", field=S.BASE_GALAXY, facts=stored.get)
    assert [(h.pid, h.value, h.detail, h.heads) for h in found.hits] == [
        (PIDS["main"], "10-Eissentam", "Synth Home", (newest,)),
        (PIDS["main"], "10-Eissentam", "Synth Outpost", (newest,))]
    total = sum(len(model.versions(line.pid)) for line in model.lines())
    assert (found.versions, found.unindexed) == (total, total - 1)
    every = S.search(model, "", field=S.BASE_GALAXY, facts=stored.get)     # every value of a field
    assert {h.value for h in every.hits} == {"10-Eissentam", "21-Loychazinq"}


def test_everything_searches_names_notes_and_the_index_together(world):
    model = world.model
    rows = model.versions(PIDS["main"])
    stored = {rows[0].head: facts_for("rich_base")}
    found = S.search(model, "MAIN", facts=stored.get)
    fields = {(h.field, h.value) for h in found.hits}
    assert {(S.SAVE_FILE, "Main Save"), (S.NOTE, "Main run."), (S.SAVE_NAME, "Synth Main")} <= fields
    name = next(h for h in found.hits if (h.field, h.value) == (S.SAVE_NAME, "Main Save"))
    assert name.heads == tuple(r.head for r in rows)           # every version it is in, newest first
    note = S.search(model, "atlas path").hits
    assert [(h.pid, h.field, h.value, h.heads) for h in note] == [
        (PIDS["main"], S.NOTE, "Next: the Atlas path.", ())]
    assert S.search(model, "  ").hits == ()


def test_a_field_of_the_read_model_never_reads_the_index_and_newest_only_reads_one_per_save(world):
    model = world.model

    def never(_vid):
        raise AssertionError("the index must not be read for a label search")

    assert S.search(model, "x", field=S.LABEL, facts=never).unindexed == 0
    seen = []
    result = S.search(model, "run", newest_only=True, facts=lambda vid: seen.append(vid))
    assert sorted(seen) == sorted(line.representative for line in model.lines())
    assert result.versions == len(model.lines()) == result.unindexed
    with pytest.raises(ValueError):
        S.search(model, "x", field="Shoe size")
