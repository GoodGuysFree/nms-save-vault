"""``nmsvault selftest``: read-only, counts only, exit 1 when an app update is needed.
Synthetic data in tmp folders; nothing real is read."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from nms_save_vault import cli
from nms_save_vault.core import gameversion, operations, safety, selftest, slotmap
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.index import extract
from nms_save_vault.core.state import Source

from .conftest import steam_save
from .index.savebuilder import Raw, context_save, encode

IDENT = 0x0110000100000001


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setattr(safety, "is_game_running", lambda: False)


def _save(folder: Path, slot: int, data: bytes | None = None, **meta) -> None:
    """A synthetic Steam save pair in ``folder``; ``data`` replaces the data file."""
    steam_save(folder, slot, 0, "Synthetic", 3600, IDENT + slot, 1_790_000_000 + slot,
               extra=str(slot), **meta)
    if data is not None:
        (folder / slotmap.data_filename(slotmap.slot_file_numbers(slot)[0])).write_bytes(data)


def clean_bytes(**kw) -> bytes:
    return encode(context_save(base={"Units": 100}, **kw))[0]


@pytest.fixture
def world(tmp_path):
    live = tmp_path / "NMS" / "st_76561198000000001"
    _save(live, 1, clean_bytes())
    vault = Vault(tmp_path / "vault")
    operations.create_full_backup(vault, live, warnings=[])
    _save(live, 1, clean_bytes(version=4741))              # the live save moved on: 2 versions
    source = Source(id="steam", platform="steam", account=live.name, path=str(live))
    return type("W", (), dict(live=live, vault=vault, source=source, tmp=tmp_path))


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns,
                                      hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*")) if p.is_file()}


def run(world) -> selftest.SelfTestReport:
    return selftest.run(world.vault.root, [world.source])


def test_a_clean_world_is_clean_and_exits_zero(world):
    report = run(world)
    assert report.saves == 2 and report.live == 1 and report.vault == 1
    assert report.decoded_ok == 2 and not report.needs_update and report.exit_code == 0
    assert report.lines()[-1] == "result: clean"


def test_it_writes_nothing_anywhere(world):
    before = {k: tree(r) for k, r in (("tmp", world.tmp),)}
    run(world)
    assert tree(world.tmp) == before["tmp"]               # vault, live, caches, state: unchanged


def test_the_report_has_counts_only_no_ids_or_names(world):
    text = "\n".join(run(world).lines())
    assert not re.search(r"[0-9a-fA-F]{12,}", text) and "Synthetic" not in text
    assert "st_7656" not in text and str(world.tmp) not in text
    for line in text.splitlines():
        assert re.fullmatch(r"[A-Za-z ()\-]+: \d+( .*)?|result: .*", line)


def test_a_newer_game_format_is_counted_and_asks_for_an_update(world):
    _save(world.live, 2, clean_bytes(version=4800),
          base_version=gameversion.KNOWN["steam"].base_version + 1)
    report = run(world)
    assert report.newer_format == 1 and report.decoded_ok == 3 and report.exit_code == 1


def test_an_unknown_top_level_key_is_counted(world):
    _save(world.live, 2, encode(context_save(base={"Units": 1}, extra={Raw("zzz"): 1}))[0])
    report = run(world)
    assert report.unknown_top_level_keys == 1 and report.unknown_top_level_saves == 1
    assert report.exit_code == 1


def test_an_unsupported_container_is_counted(world):
    _save(world.live, 2, b"\xe5\xa1\xed\xfeHGSAVEV2" + b"\x00" * 64)
    from nms_save_vault.core import lz4_block

    _save(world.live, 3, lz4_block.HGSAVEV2_HEADER + b"\x00" * 64)
    report = run(world)
    assert report.unsupported_container >= 1 and report.exit_code == 1


def test_data_that_does_not_decode_is_damaged_not_an_update_issue(world):
    _save(world.live, 2, b"\xe5\xa1\xed\xfe" + b"\x01" * 40)
    report = run(world)
    assert report.damaged >= 1 and report.exit_code == 0


def test_an_extractor_error_is_counted(world, monkeypatch):
    def boom(decoded, extractors=None):
        raise RuntimeError("extractor broke")
    monkeypatch.setattr(extract, "run_extractors", boom)
    report = run(world)
    assert report.extractor_errors == 2 and report.exit_code == 1


def test_the_command_prints_counts_and_returns_the_exit_code(world, capsys):
    code = cli.main(["--vault", str(world.vault.root), "--live", str(world.live), "selftest"])
    out = capsys.readouterr().out
    assert code == 0 and "decoded OK: 2" in out and out.strip().endswith("result: clean")
    _save(world.live, 2, clean_bytes(version=4800),
          base_version=gameversion.KNOWN["steam"].base_version + 1)
    assert cli.main(["--vault", str(world.vault.root), "--live", str(world.live), "selftest"]) == 1
