"""tools/check_upstream.py with a mocked network: what is new since the recorded versions."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "check_upstream", Path(__file__).resolve().parent.parent / "tools" / "check_upstream.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

LIB = "zencq/libNOM.io"
MBIN = "monkeyman192/MBINCompiler"


def release(tag, day, *, pre=False, draft=False):
    return {"tag_name": tag, "published_at": f"{day}T10:00:00Z", "html_url": f"https://example.test/{tag}",
            "prerelease": pre, "draft": draft}


class Net:
    def __init__(self, data):
        self.data, self.urls = data, []

    def __call__(self, url):
        self.urls.append(url)
        for repo, releases in self.data.items():
            if f"/repos/{repo}/" in url:
                return releases
        raise OSError("no such repo")


@pytest.fixture
def record(tmp_path):
    path = tmp_path / "upstream.json"
    path.write_text(json.dumps({LIB: {"tag": "0.14.2", "published": "2025-09-06"},
                                MBIN: {"tag": "v7.07.0-pre1", "published": "2026-10-08"}}), encoding="utf-8")
    return path


def test_the_recorded_release_is_the_cut_off():
    releases = tool.latest_releases(LIB, Net({LIB: [release("0.14.0", "2025-02-09"), release("0.15.0", "2026-11-01"),
                                                    release("0.14.2", "2025-09-06"), release("x", "2026-12-01", draft=True)]}))
    assert [r["tag"] for r in releases] == ["0.15.0", "0.14.2", "0.14.0"]           # newest first, no draft
    assert [r["tag"] for r in tool.new_since({"tag": "0.14.2", "published": "2025-09-06"}, releases)] == ["0.15.0"]
    assert tool.new_since({"tag": "0.15.0", "published": "x"}, releases) == []
    assert len(tool.new_since(None, releases)) == 3                                  # nothing recorded: all new
    assert [r["tag"] for r in tool.new_since({"tag": "gone", "published": "2025-09-06"}, releases)] == ["0.15.0"]


def test_it_prints_what_is_new_and_what_to_do_next(record, capsys):
    net = Net({LIB: [release("0.14.2", "2025-09-06")],
               MBIN: [release("v7.08.0", "2026-11-05", pre=True), release("v7.07.0-pre1", "2026-10-08", pre=True)]})
    assert tool.main([], fetch=net, record=record) == 0
    out = capsys.readouterr().out
    assert f"{LIB}: nothing new" in out and f"{MBIN}: 1 new" in out
    assert "v7.08.0  2026-11-05  (pre-release)  https://example.test/v7.08.0" in out
    assert "nmsvault-cli selftest" in out and "Maintenance after a game update" in out
    assert all("api.github.com/repos/" in u for u in net.urls)


def test_nothing_new_prints_no_next_steps_and_changes_nothing(record, capsys):
    before = record.read_bytes()
    net = Net({LIB: [release("0.14.2", "2025-09-06")], MBIN: [release("v7.07.0-pre1", "2026-10-08")]})
    assert tool.main([], fetch=net, record=record) == 0
    assert "Next:" not in capsys.readouterr().out and record.read_bytes() == before


def test_record_stores_the_newest_of_each(record):
    net = Net({LIB: [release("0.15.0", "2026-11-01")], MBIN: [release("v8", "2026-11-09")]})
    tool.main(["--record"], fetch=net, record=record)
    assert json.loads(record.read_text(encoding="utf-8")) == {
        LIB: {"tag": "0.15.0", "published": "2026-11-01"}, MBIN: {"tag": "v8", "published": "2026-11-09"}}


def test_a_network_failure_is_reported_not_raised(record, capsys):
    def down(url):
        raise OSError("offline")
    assert tool.main([], fetch=down, record=record) == 2
    assert "could not reach GitHub" in capsys.readouterr().err


def test_the_app_never_imports_the_tool_and_the_recorded_file_is_valid():
    root = Path(__file__).resolve().parent.parent
    import re
    importing = re.compile(r"^\s*(?:import|from)\s+\S*check_upstream", re.M)
    assert not any(importing.search(p.read_text(encoding="utf-8")) for p in (root / "src").rglob("*.py"))
    recorded = json.loads((root / "tools" / "upstream.json").read_text(encoding="utf-8"))
    assert set(recorded) == {LIB, MBIN} and all(v["tag"] and v["published"] for v in recorded.values())
