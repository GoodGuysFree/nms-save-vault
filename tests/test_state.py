"""state.json load + version migration."""
from __future__ import annotations

from nms_save_vault.core import state


def test_v1_state_migrates_xbox_to_writable(tmp_path):
    """An old v1 config (Xbox saved read-only) heals to writable on load, so existing
    installs gain same-platform Xbox write without a manual rescan."""
    p = tmp_path / "state.json"
    p.write_text(
        '{"version": 1, "vault": null, "sources": ['
        '{"id": "xbox-acc", "platform": "xbox", "account": "acc", "path": "X", "writable": false},'
        '{"id": "steam-1", "platform": "steam", "account": "1", "path": "Y", "writable": true}'
        ']}',
        "utf-8",
    )
    st = state.load(p)
    assert st.get("xbox-acc").writable is True     # healed by the v1 -> v2 migration
    assert st.get("steam-1").writable is True
    assert st.version == state.STATE_VERSION


def test_current_version_preserves_explicit_writable(tmp_path):
    """At the current version no migration runs, so an explicit writable flag is preserved."""
    p = tmp_path / "state.json"
    p.write_text(
        '{"version": ' + str(state.STATE_VERSION) + ', "vault": null, "sources": ['
        '{"id": "xbox-acc", "platform": "xbox", "account": "acc", "path": "X", "writable": false}'
        ']}',
        "utf-8",
    )
    st = state.load(p)
    assert st.get("xbox-acc").writable is False
