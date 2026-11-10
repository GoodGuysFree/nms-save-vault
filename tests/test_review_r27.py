"""Review 27's finding in the core (R27-7), a test that failed before its fix: a write open in a
vault no longer at its place is not told as held by the vault there. Made-up saves only."""
from __future__ import annotations

import shutil

from nms_save_vault.core import api
from nms_save_vault.core.liveops import opjournal

from .test_core_api_write import UNITS, _session, _slot, savecraft, world  # noqa: F401
from .test_review_r26 import _stopped_in_other_vault


def test_r27_7_a_write_open_in_a_vault_no_longer_at_its_path_is_told_as_held_by_the_vault_there(
        world, tmp_path, monkeypatch):
    """_missing_vaults takes the vault at the marker's path for the marker's vault: it never
    compares that vault's id with the marker's. Since c618ca8 an op its (readable) journal
    does not hold is told as "its records are in that vault. Open that vault ... to finish it"
    (rec None was skipped before). When another vault stands at that path (a vault made anew
    there, a drive letter that now names another drive), that vault has no record of the op:
    opening it settles nothing (its own look-over tells the same, since the marker is not its
    own), so the message is wrong and is told on every start, in every vault, for ever."""
    other = _stopped_in_other_vault(world, tmp_path, monkeypatch)
    from nms_save_vault.core import store as vstore
    shutil.rmtree(other)                       # another vault is made at that path
    other.mkdir()
    vstore.create_store(other)
    monkeypatch.setattr(opjournal, "_tails", {})          # read as a new process reads it
    # the user opens the vault at that path, as the text says: its own look-over tells it
    there = [i.text for i in api.open_live(other, live_dirs=[world.live]).interrupted()]
    here = [i.text for i in _session(world).interrupted()]
    wrong = [t for t in here + there if "in that vault" in t]
    assert not wrong, (here, there)


def test_r27_3_a_base_made_another_save_s_player_s_takes_no_server_id():
    """NMS-Unpack response 3, 2.4: RID is an uploaded base's server id; a copy sets it empty
    (as PlatformToken: empty on every own base)."""
    from nms_save_vault.core.testing import obfuscated

    from .test_core_api_owners import MINE, OTHER, base, readable, save
    record = obfuscated(base("HomePlanetBase", OTHER, RID="5f2c9a0d", PlatformToken="tok"))
    raw = api.rehomed(record, save(base("HomePlanetBase", MINE)))
    back = __import__("json").loads(raw.decode("utf-8", "surrogateescape"))
    assert back[obfuscated({"RID": 0}).popitem()[0]] == ""
    assert back[obfuscated({"PlatformToken": 0}).popitem()[0]] == ""
    assert readable(raw)["Name"] == ""
