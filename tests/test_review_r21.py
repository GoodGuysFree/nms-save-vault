"""Review 21's finding in the core (R21-4), a test that failed before its fix. Made-up folders
and saves only."""
from __future__ import annotations

from dataclasses import replace

import pytest

from nms_save_vault.core import aliases, state
from nms_save_vault.core.liveops.targets import resolve_target, same_folder
from nms_save_vault.core.organizer.model import LiveTarget, target_from_json, target_to_json

from .liveops_support import STEAM_ACCOUNT, live_source, make_ctx
from .test_liveops_targets import steam_dir


@pytest.fixture(autouse=True)
def no_aliases(monkeypatch):
    monkeypatch.setattr(aliases, "_active", aliases.AliasMap())


def test_r21_4_a_name_alone_journaled_with_its_place_keeps_its_folder_beside_the_vault_s(tmp_path):
    """Review R8-4's rule: a name alone an op journaled with where its folder was (``at``)
    names that folder once two folders share the name. _the_source applies ``at`` only when
    the name alone matches two folders; a folder NMS Save Vault lists now (named by the name
    alone) beside the op's own folder, which an app brings (named by its path), takes the op:
    its undo, and its history, go to the vault's folder (a copy, after a re-pointed setting)."""
    written = steam_dir(tmp_path / "prefix1")          # the folder the op wrote
    other = steam_dir(tmp_path / "prefix2")            # the vault lists this one now
    then = make_ctx(tmp_path, [live_source(written)])
    journaled = target_from_json(target_to_json(
        resolve_target(then, LiveTarget("steam", STEAM_ACCOUNT, 3)).target))
    assert journaled.at and not journaled.instance
    now = make_ctx(tmp_path, [live_source(other),
                              replace(live_source(written), origin=state.ORIGIN_APP)])
    assert resolve_target(now, journaled).folder == written
    assert not same_folder(now, journaled.ref, LiveTarget("steam", STEAM_ACCOUNT, 3).ref)
