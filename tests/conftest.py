"""Shared fixtures. Locates the live NMS save dir for read-only verification tests."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nms_save_vault.core import slotmap


def find_live_save_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = Path(appdata) / "HelloGames" / "NMS"
    if not base.is_dir():
        return None
    for candidate in sorted(base.glob("st_*")):
        if candidate.is_dir() and any(candidate.glob("mf_save*.hg")):
            return candidate
    return None


@pytest.fixture(scope="session")
def live_save_dir() -> Path:
    d = find_live_save_dir()
    if d is None:
        pytest.skip("no live NMS save directory found")
    return d


def meta_data_files(d: Path):
    """Yield (file_no, meta_path, data_path) for save members present in ``d``."""
    for f in slotmap.all_file_numbers():
        mp = d / slotmap.meta_filename(f)
        dp = d / slotmap.data_filename(f)
        if mp.exists() and dp.exists():
            yield f, mp, dp
