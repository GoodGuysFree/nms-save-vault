"""Update checking: version comparison, throttling, the GitHub call, and the GUI flow.

No test here touches the network -- ``urlopen`` and ``updates.check`` are always replaced.
"""
from __future__ import annotations

import json
import urllib.error
from datetime import date, timedelta

import pytest

from nms_save_vault import updates
from nms_save_vault.core import state as appstate


# --- version comparison ------------------------------------------------------


def test_parse_version_ignores_a_leading_v_and_junk():
    assert updates.parse_version("v0.0.10") == (0, 0, 10)
    assert updates.parse_version("0.1.2") == (0, 1, 2)
    assert updates.parse_version("") == ()


def test_ten_is_newer_than_nine():
    """The trap: as text, "0.0.10" sorts BELOW "0.0.9"."""
    assert updates.is_newer("0.0.10", "0.0.9")
    assert not updates.is_newer("0.0.9", "0.0.10")


@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("0.0.9", "0.0.8", True),
        ("v0.0.9", "0.0.8", True),
        ("0.0.8", "0.0.8", False),   # same version is not an update
        ("0.0.7", "0.0.8", False),
        ("0.1.0", "0.0.99", True),
        ("1.0.0", "0.9.9", True),
        ("0.1", "0.1.0", False),     # shorter is padded, not treated as greater
        ("0.1.1", "0.1", True),
    ],
)
def test_is_newer(candidate, current, expected):
    assert updates.is_newer(candidate, current) is expected


def test_unparseable_versions_never_nag():
    assert not updates.is_newer("", "0.0.8")
    assert not updates.is_newer("banana", "0.0.8")
    assert not updates.is_newer("0.0.9", "")


# --- throttling --------------------------------------------------------------


def test_check_is_due_once_a_day():
    today = date(2026, 8, 22)
    assert not updates.due(today.isoformat(), today)
    assert updates.due((today - timedelta(days=1)).isoformat(), today)


def test_never_checked_or_corrupt_date_is_due():
    assert updates.due("")
    assert updates.due("not-a-date")
    assert updates.due(None)


# --- the GitHub call ---------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(payload: dict, captured: dict | None = None):
    def urlopen(request, timeout=None):
        if captured is not None:
            captured["headers"] = request.headers
            captured["url"] = request.full_url
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    return urlopen


def test_fetch_latest_reads_the_tag_and_page(monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _fake_urlopen({"tag_name": "v0.0.9", "html_url": "https://example/rel/9", "name": "v0.0.9"}),
    )
    release = updates.fetch_latest()
    assert release.version == "0.0.9"  # the v is stripped
    assert release.page_url == "https://example/rel/9"


def test_fetch_latest_sends_a_user_agent(monkeypatch):
    """GitHub rejects API requests that do not identify themselves."""
    captured: dict = {}
    monkeypatch.setattr(
        updates.urllib.request, "urlopen", _fake_urlopen({"tag_name": "v0.0.9"}, captured)
    )
    updates.fetch_latest()
    assert "User-agent" in captured["headers"] or "User-Agent" in captured["headers"]


def test_fetch_latest_falls_back_to_the_releases_page(monkeypatch):
    monkeypatch.setattr(updates.urllib.request, "urlopen", _fake_urlopen({"tag_name": "v0.0.9"}))
    assert updates.fetch_latest().page_url == updates.RELEASES_PAGE


@pytest.mark.parametrize(
    "boom",
    [
        urllib.error.HTTPError("u", 403, "rate limited", {}, None),
        urllib.error.URLError("offline"),
        TimeoutError("slow"),
        OSError("no route"),
    ],
)
def test_network_failures_become_one_error_type(monkeypatch, boom):
    def urlopen(request, timeout=None):
        raise boom

    monkeypatch.setattr(updates.urllib.request, "urlopen", urlopen)
    with pytest.raises(updates.UpdateCheckError):
        updates.fetch_latest()


def test_garbage_response_is_an_error_not_a_crash(monkeypatch):
    def urlopen(request, timeout=None):
        return _FakeResponse(b"<html>not json</html>")

    monkeypatch.setattr(updates.urllib.request, "urlopen", urlopen)
    with pytest.raises(updates.UpdateCheckError):
        updates.fetch_latest()


def test_release_without_a_tag_is_an_error(monkeypatch):
    monkeypatch.setattr(updates.urllib.request, "urlopen", _fake_urlopen({"html_url": "x"}))
    with pytest.raises(updates.UpdateCheckError):
        updates.fetch_latest()


def test_check_returns_nothing_when_current(monkeypatch):
    monkeypatch.setattr(updates.urllib.request, "urlopen", _fake_urlopen({"tag_name": "v0.0.8"}))
    assert updates.check(current="0.0.8") is None
    assert updates.check(current="0.0.9") is None  # a dev build ahead of the release


def test_check_returns_the_release_when_newer(monkeypatch):
    monkeypatch.setattr(updates.urllib.request, "urlopen", _fake_urlopen({"tag_name": "v0.1.0"}))
    assert updates.check(current="0.0.8").version == "0.1.0"


# --- persistence -------------------------------------------------------------


def test_preferences_round_trip(tmp_path):
    p = tmp_path / "state.json"
    st = appstate.AppState(sources=[], update_check=updates.ON, update_last_check="2026-08-22")
    appstate.save(st, p)
    loaded = appstate.load(p)
    assert loaded.update_check == updates.ON
    assert loaded.update_last_check == "2026-08-22"


def test_older_config_has_not_been_asked_yet(tmp_path):
    """An install that predates this feature must get the prompt, not a silent check."""
    p = tmp_path / "state.json"
    p.write_text('{"version": 2, "vault": null, "sources": []}', "utf-8")
    assert appstate.load(p).update_check == updates.ASK


# --- the GUI flow ------------------------------------------------------------


@pytest.fixture
def quiet_app(gui_app, monkeypatch):
    """The shared App with state saving stubbed, restored afterwards."""
    monkeypatch.setattr(gui_app, "_save_state_quietly", lambda: None)
    before = (gui_app.state.update_check, gui_app.state.update_last_check)
    yield gui_app
    gui_app.state.update_check, gui_app.state.update_last_check = before
    gui_app._hide_banner()


def test_startup_does_nothing_when_the_user_said_no(quiet_app, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the network must not be touched when checking is off")

    monkeypatch.setattr(updates, "check", boom)
    monkeypatch.setattr(updates.urllib.request, "urlopen", boom)
    quiet_app.state.update_check = updates.OFF
    quiet_app._startup_update_check()


def test_startup_respects_the_daily_throttle(quiet_app, monkeypatch):
    calls = []
    monkeypatch.setattr(quiet_app, "_check_for_updates", lambda **kw: calls.append(kw))

    quiet_app.state.update_check = updates.ON
    quiet_app.state.update_last_check = date.today().isoformat()
    quiet_app._startup_update_check()
    assert calls == []  # already checked today

    quiet_app.state.update_last_check = (date.today() - timedelta(days=1)).isoformat()
    quiet_app._startup_update_check()
    assert calls == [{"quiet": True}]


def test_first_run_asks_and_records_the_answer(quiet_app, monkeypatch):
    from nms_save_vault import gui

    monkeypatch.setattr(quiet_app, "_check_for_updates", lambda **kw: None)
    monkeypatch.setattr(gui, "_askyesno", lambda *a, **k: True)
    quiet_app.state.update_check = updates.ASK
    quiet_app._startup_update_check()
    assert quiet_app.state.update_check == updates.ON

    monkeypatch.setattr(gui, "_askyesno", lambda *a, **k: False)
    quiet_app.state.update_check = updates.ASK
    quiet_app._startup_update_check()
    assert quiet_app.state.update_check == updates.OFF


def test_a_newer_release_shows_the_banner(quiet_app):
    quiet_app._update_check_done(updates.Release("9.9.9", "https://example/rel"), quiet=True)
    assert "9.9.9" in quiet_app.banner_var.get()
    assert quiet_app.banner.winfo_ismapped() or quiet_app.banner.winfo_manager()
    assert quiet_app._release_url == "https://example/rel"


def test_being_up_to_date_shows_no_banner(quiet_app):
    quiet_app._update_check_done(None, quiet=True)
    assert not quiet_app.banner.winfo_manager()


def test_a_failed_startup_check_stays_silent(quiet_app, monkeypatch):
    from nms_save_vault import gui

    shown = []
    monkeypatch.setattr(gui, "_showwarning", lambda *a, **k: shown.append(a))
    quiet_app._update_check_done(updates.UpdateCheckError("offline"), quiet=True)
    assert shown == []  # a startup check failing offline must not nag
    assert not quiet_app.banner.winfo_manager()


def test_a_failed_manual_check_reports_the_reason(quiet_app, monkeypatch):
    from nms_save_vault import gui

    shown = []
    monkeypatch.setattr(gui, "_showwarning", lambda *a, **k: shown.append(a))
    quiet_app._update_check_done(updates.UpdateCheckError("offline"), quiet=False)
    assert shown and "offline" in shown[0][1]


def test_a_completed_check_records_the_date(quiet_app):
    quiet_app.state.update_last_check = ""
    quiet_app._update_check_done(None, quiet=True)
    assert quiet_app.state.update_last_check == date.today().isoformat()
