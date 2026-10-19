"""liveops.crosssave: the accountdata CrossSaves flag (D5 2.3.3 CROSS_SAVE_ON). Synthetic
account data built with the test builders; every path is under tmp_path."""
from __future__ import annotations

import pytest

from nms_save_vault.core import hashname
from nms_save_vault.core.liveops import crosssave
from nms_save_vault.core.liveops.planner import eviction_picker, plan_place
from nms_save_vault.core.organizer.model import LiveRef

from .conftest import chunked
from .liveops_support import STEAM_ACCOUNT, steam_world

KEY = hashname.key("CrossSaves")
LIVE = LiveRef("steam", STEAM_ACCOUNT)


def account_data(world, payload: bytes | None) -> None:
    path = world.live / "accountdata.hg"
    if payload is None:
        path.unlink()
    else:
        path.write_bytes(chunked(payload))


def warn_codes(world) -> set[str]:
    return {w.code for w in plan_place(world.ctx, world.polestar(), LIVE.target(3)).warnings}


@pytest.mark.parametrize("payload, expected", [
    (b'{"%s":true}' % KEY.encode(), True),
    (b'{"%s":false}' % KEY.encode(), False),
    (b'{"UD":{"%s":1}}' % KEY.encode(), True),            # nested, 0/1 integers
    (b'{"%s":"yes"}' % KEY.encode(), None),                # not a flag
    (b'{"other":true}', None),                            # no such field
])
def test_the_flag_is_read_from_decoded_account_data(tmp_path, payload, expected):
    world = steam_world(tmp_path)
    account_data(world, payload)
    assert crosssave.cross_save_on(world.live, "steam") is expected


def test_unknown_when_unreadable_missing_or_xbox(tmp_path):
    world = steam_world(tmp_path)
    assert crosssave.cross_save_on(world.live, "steam") is None        # "account data": not a save
    account_data(world, None)
    assert crosssave.cross_save_on(world.live, "steam") is None
    account_data(world, b'{"%s":true}' % KEY.encode())
    assert crosssave.cross_save_on(world.live, "xbox") is None


def test_cross_save_on_warns_in_plans_and_the_picker(tmp_path):
    world = steam_world(tmp_path)
    assert "CROSS_SAVE_ON" not in warn_codes(world)                    # unknown: no warning
    account_data(world, b'{"%s":false}' % KEY.encode())
    assert "CROSS_SAVE_ON" not in warn_codes(world)
    assert eviction_picker(world.ctx, world.polestar(), LIVE).cloud.cross_save_on is False
    account_data(world, b'{"%s":true}' % KEY.encode())
    warning = next(w for w in plan_place(world.ctx, world.polestar(), LIVE.target(3)).warnings
                   if w.code == "CROSS_SAVE_ON")
    assert warning.severity == "warn" and "cross-save is on" in warning.text
    picker = eviction_picker(world.ctx, world.polestar(), LIVE)
    assert picker.cloud.cross_save_on is True
    assert "CROSS_SAVE_ON" in {w.code for w in picker.warnings}
