"""core.testing: saves made up from readable names, for the tests of an app bundling the core
(NMS Savecraft). Its two names are pinned like the public API's, and what it writes reads back
through that API as a valid save."""
from __future__ import annotations

import inspect

from nms_save_vault.core import api, gameversion, testing
from nms_save_vault.core.hashname import key

PINNED = {
    "SimulatedCrash": "class",
    "crash_during": "(session, point: 'str', nth: 'int' = 1) -> 'Iterator[None]'",
    "game_closed": "() -> 'Iterator[None]'",
    "obfuscated": "(node: 'object') -> 'object'",
    "write_account": "(folder: 'str | Path', root: 'dict', *, saved_at: 'int' = 1790000000) "
                     "-> 'Path'",
    "write_steam_save": (
        "(folder: 'str | Path', slot: 'int', root: 'dict', *, member: 'int' = 0, "
        "name: 'str' = '', play_time: 'int' = 0, playthrough: 'int' = 4660, "
        "saved_at: 'int' = 1790000000, base_version: 'int' = "
        f"{gameversion.KNOWN['steam'].base_version}) -> 'Path'"),
}


def test_the_testing_names_and_signatures_are_pinned():
    assert sorted(testing.__all__) == sorted(PINNED)
    assert {n: "class" if isinstance(getattr(testing, n), type)
            else str(inspect.signature(getattr(testing, n))) for n in testing.__all__} == PINNED


def test_readable_names_become_the_game_s_keys():
    assert testing.obfuscated({"PlayerStateData": [{"Units": 5}]}) == {
        key("PlayerStateData"): [{key("Units"): 5}]}


def test_a_written_save_reads_back_through_the_api(tmp_path):
    root = {"CommonStateData": {"SaveName": "Made Up"},
            "BaseContext": {"PlayerStateData": {"Units": 1234}}}
    for member in (0, 1):
        testing.write_steam_save(tmp_path / "st_1", 3, root, member=member, name="Made Up",
                                 play_time=3600 + member, playthrough=0xABCD,
                                 saved_at=1_790_000_000 + member)
    view = api.scan_save_dir(tmp_path / "st_1")
    slot = view.slots[3]
    assert slot.a.valid and slot.b.valid and slot.newest is slot.b
    m = slot.newest
    assert (m.save_name, m.info.total_play_time) == ("Made Up", 3601)
    assert api.known_game_version(m, platform="steam")
    decoded = api.read_member(m).root
    assert api.get(decoded, "BaseContext", "PlayerStateData", "Units") == 1234
    assert api.playthrough_of(m) is not None


def test_a_hashed_technology_id_is_written_exactly(tmp_path):
    """A hashed id is six bytes that are not UTF-8, held as lone surrogates; the save keeps the
    bytes, as the game writes them."""
    hashed = "^" + bytes.fromhex("8080B7B5DD85").decode("utf-8", "surrogateescape")
    root = {"PlayerStateData": {"Inventory": {"Slots": [{"Id": hashed}]}}}
    folder = tmp_path / "st_76561198000000001"
    testing.write_steam_save(folder, 1, root)
    member = api.scan_save_dir(folder).slots[1].newest
    (inv,) = api.inventories_of(api.read_member(member).root)
    assert inv.slots[0].item.encode("utf-8", "surrogateescape") == bytes.fromhex("8080B7B5DD85")


def test_inside_game_closed_the_core_takes_the_game_for_closed(monkeypatch):
    from nms_save_vault.core import safety

    monkeypatch.setattr(safety, "is_game_running", lambda *a, **k: True)
    assert api.game_running() is True
    with testing.game_closed():
        assert api.game_running() is False
    assert api.game_running() is True


def test_an_account_file_written_for_a_test_reads_back_and_takes_unlocks(tmp_path):
    testing.write_account(tmp_path, {"UserSettingsData": {"UnlockedSpecials": ["^A"]}})
    assert api.get(api.read_account(tmp_path), "UserSettingsData", "UnlockedSpecials") == ["^A"]
