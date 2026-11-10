"""api.rehomed: a base record of one save (a corvette's layout) made the target save's player's,
the ids never leaving the core (GGF, 2026-11-09: the saves are one user's; ids stay inside)."""
from __future__ import annotations

import json

import pytest

from nms_save_vault.core import api
from nms_save_vault.core.testing import obfuscated

MINE = {"UID": "1001", "LID": "L1", "USN": "", "PTK": "ST"}
OLDER = {"UID": "1001", "LID": "L0", "USN": "me", "PTK": "ST"}
OTHER = {"UID": "2002", "LID": "L2", "USN": "them", "PTK": "XB"}


def base(kind: str, owner: dict, ts: int = 5, **more) -> dict:
    return {"BaseType": {"PersistentBaseTypes": kind}, "Owner": {**owner, "TS": ts},
            "UserData": 0, "Name": "", "LastEditedById": "", "LastEditedByUsername": "", **more}


def save(*bases: dict, expedition: tuple = ()) -> dict:
    root = {"BaseContext": {"PlayerStateData": {"PersistentPlayerBases": list(bases)}}}
    if expedition:
        root["ExpeditionContext"] = {"PlayerStateData": {"PersistentPlayerBases": list(expedition)}}
    return obfuscated(root)


def readable(raw: bytes) -> dict:
    back = json.loads(raw.decode("utf-8", "surrogateescape"))
    return {k: back[obfuscated({k: 0}).popitem()[0]] for k in
            ("Owner", "UserData", "LastEditedById", "LastEditedByUsername", "Name")}


def owner(record: dict) -> dict:
    return {k: record["Owner"][obfuscated({k: 0}).popitem()[0]] for k in ("UID", "LID", "USN", "PTK", "TS")}


def layout(**more) -> dict:
    return obfuscated(base("PlayerShipBase", OTHER, ts=1790000001, **more))


def test_a_base_from_another_save_takes_the_owner_the_target_s_own_bases_carry_most():
    target = save(base("HomePlanetBase", MINE), base("HomePlanetBase", MINE),
                  base("FreighterBase", OLDER), base("ExternalPlanetBase", OTHER),
                  base("ExternalPlanetBase", OTHER), base("ExternalPlanetBase", OTHER))
    got = readable(api.rehomed(layout(LastEditedById="x9", LastEditedByUsername="y9"), target))
    assert owner(got) == {**MINE, "TS": 1790000001}          # when it was made stays
    assert (got["LastEditedById"], got["LastEditedByUsername"]) == ("", "")
    assert (got["UserData"], got["Name"]) == (0, "")         # the rest as it was


def test_the_owner_of_the_target_s_platform_comes_first():
    """Review M6-5: a cross-save player keeps more bases of their old platform; a copy into a
    Steam save is the Steam player's."""
    target = save(base("HomePlanetBase", OTHER), base("HomePlanetBase", OTHER),
                  base("FreighterBase", MINE))
    assert owner(readable(api.rehomed(layout(), target, platform="steam")))["UID"] == "1001"
    assert owner(readable(api.rehomed(layout(), target, platform="xbox")))["UID"] == "2002"
    assert owner(readable(api.rehomed(layout(), target)))["UID"] == "2002"   # the most, else


def test_the_other_game_of_the_target_gives_the_owner_when_this_one_has_no_base_of_its_player():
    target = save(base("ExternalPlanetBase", OTHER), expedition=(base("HomePlanetBase", OLDER),))
    assert owner(readable(api.rehomed(layout(), target)))["LID"] == "L0"
    assert owner(readable(api.rehomed(layout(), target, "expedition")))["LID"] == "L0"


def test_a_target_with_no_base_of_its_player_is_refused():
    for target in (save(), save(base("ExternalPlanetBase", OTHER)),
                   save(base("HomePlanetBase", {**MINE, "UID": ""}))):
        with pytest.raises(api.EditRefused, match="no base of its player"):
            api.rehomed(layout(), target)


def test_an_app_reads_when_a_base_s_owner_block_was_made_and_nothing_else_of_it():
    record = obfuscated(base("PlayerShipBase", MINE, ts=1790000001))
    assert api.owner_time(record) == 1790000001
    assert api.owner_time(obfuscated({"Name": "x"})) is None
    with pytest.raises(api.PrivacyError):
        api.get(record, "Owner", "TS")                  # the block itself stays closed


def test_only_a_base_record_is_rehomed():
    with pytest.raises(api.EditRefused, match="not a base"):
        api.rehomed(obfuscated({"Name": "x"}), save(base("HomePlanetBase", MINE)))


def from_file(**more) -> dict:
    """A corvette's layout as a file carries it: no owner block, no field of the deny list."""
    record = base("PlayerShipBase", OTHER, ts=1, **more)
    for name in ("Owner", "LastEditedById", "LastEditedByUsername"):
        record.pop(name)
    return obfuscated(record)


def test_a_base_from_a_file_takes_the_owner_block_of_the_target_s_player_made_now():
    """For NMS Savecraft's corvette files (.scv): the file carries no ownership data, so the
    core gives the record the owner block of the target's own base whose owner rehomed takes,
    made at ``made``, and that base's other ownership fields, empty."""
    target = save(base("HomePlanetBase", MINE, LastEditedById="me1"),
                  base("HomePlanetBase", MINE), base("ExternalPlanetBase", OTHER))
    got = readable(api.owned(from_file(), target, made=1790000999))
    assert owner(got) == {**MINE, "TS": 1790000999}
    assert (got["LastEditedById"], got["LastEditedByUsername"]) == ("", "")
    assert (got["UserData"], got["Name"]) == (0, "")
    assert owner(readable(api.owned(from_file(), save(base("HomePlanetBase", OTHER),
                                                      base("FreighterBase", MINE)),
                                    made=7, platform="steam")))["UID"] == "1001"


def test_a_base_from_a_file_with_ownership_data_or_no_type_is_refused():
    target = save(base("HomePlanetBase", MINE))
    for record in (layout(), from_file(Extra={"UID": "x"}), obfuscated({"Name": "x"})):
        with pytest.raises(api.EditRefused):
            api.owned(record, target, made=1)
    with pytest.raises(api.EditRefused, match="no base of its player"):
        api.owned(from_file(), save(base("ExternalPlanetBase", OTHER)), made=1)
