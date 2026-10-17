"""``store/state/damage.json``: files of store entries that could not be read back intact
(D3 2.6.3, 2.7, 2.11). Records are added or updated, never removed by the app; the owner sees
them in the vault status and ``fsck --repair`` works from them."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from .. import safety

log = logging.getLogger(__name__)

DAMAGE_JSON = "damage.json"
DAMAGE_FORMAT = "nmsvault-damage"
Problem = Literal["missing", "corrupt", "manifest"]


@dataclass(frozen=True)
class DamageRecord:
    entry_id: str
    path: str               # the file in the entry ("" for a damaged manifest)
    digest: str             # the sha256 the file should have ("" for a manifest)
    problem: Problem
    detail: str             # ASCII, for the owner
    first_seen_utc: str
    last_seen_utc: str
    count: int = 1

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.entry_id, self.path, self.digest)


def damage_path(store_root: Path) -> Path:
    return store_root / "state" / DAMAGE_JSON


def read_damage(store_root: Path) -> list[DamageRecord]:
    """The records; an absent file is none, an unreadable one is logged and read as none."""
    path = damage_path(store_root)
    try:
        d = json.loads(path.read_bytes().decode("utf-8"))
        if d.get("format") != DAMAGE_FORMAT:
            raise ValueError("not a damage record file")
        return [DamageRecord(**r) for r in d["records"]]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("the store's damage list cannot be read: %s", exc)
        return []


def merge(records: list[DamageRecord], new: DamageRecord) -> list[DamageRecord]:
    """``records`` with ``new`` added, or its earlier record updated (problem, detail, last
    seen, count)."""
    out = []
    found = False
    for r in records:
        if r.key == new.key and not found:
            found = True
            r = replace(r, problem=new.problem, detail=new.detail,
                        last_seen_utc=new.last_seen_utc, count=r.count + 1)
        out.append(r)
    return out if found else out + [new]


def write_damage(store_root: Path, records: list[DamageRecord]) -> None:
    body = {"format": DAMAGE_FORMAT, "version": 1, "records": [asdict(r) for r in records]}
    raw = json.dumps(body, indent=1, sort_keys=True, ensure_ascii=True).encode("ascii")
    safety.atomic_write_bytes(damage_path(store_root), raw, temp_prefix=".damage.tmp_")
