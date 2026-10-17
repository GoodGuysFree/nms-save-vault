"""Reproduce the content index's performance numbers (D4 2.14) on copies of save data files.

Dev-only. Point it at a folder holding COPIES of save data files (never a live save folder
or the vault itself); every file is one version. It measures, with no other load:

  decode   LZ4 + JSON of every file on one core (the floor of a full index)
  index    a full first index through IndexService with N spawn workers, into a temp cache
  reads    view() and changes() latency on the warm cache

    python tools/bench_index.py COPIES_DIR [--workers 4] [--manifest manifest.json]

A manifest (a JSON list of {"file", "container", "size_decompressed"}) gives unframed Xbox
blocks their size; without one such files report as corrupt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import struct
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from nms_save_vault.core import savejson  # noqa: E402
from nms_save_vault.core.index import IndexService, Priority  # noqa: E402
from nms_save_vault.core.index.pool import ExecutionPolicy  # noqa: E402


class CopiesLocator:
    def __init__(self, files: dict[str, tuple[Path, int | None]]) -> None:
        self.files = files

    def open(self, vid: str, prefer=None):
        path, hint = self.files[vid]
        if hint is None:
            return SimpleNamespace(data=path.read_bytes(), meta_plain=b"", platform="steam")
        meta = bytearray(360)                      # an unframed Xbox block: its size from the meta
        struct.pack_into("<I", meta, 0x10, hint)
        return SimpleNamespace(data=path.read_bytes(), meta_plain=bytes(meta), platform="xbox")


def load(folder: Path, manifest: Path | None) -> dict[str, tuple[Path, int | None]]:
    hints: dict[str, int] = {}
    if manifest:
        for e in json.loads(manifest.read_text(encoding="utf-8")):
            if e.get("container") != "chunked" and e.get("size_decompressed"):
                hints[e["file"]] = e["size_decompressed"]
    out = {}
    for path in sorted(p for p in folder.iterdir() if p.is_file() and p.suffix != ".json"):
        out[hashlib.sha256(path.read_bytes()).hexdigest()] = (path, hints.get(path.name))
    return out


def pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="content index benchmark on copies")
    ap.add_argument("copies", type=Path)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--skip-decode", action="store_true")
    args = ap.parse_args(argv)
    files = load(args.copies, args.manifest)
    print(f"{len(files)} versions, {sum(p.stat().st_size for p, _h in files.values()) / 1e6:.0f} MB of data")

    if not args.skip_decode:
        t0 = time.perf_counter()
        json_bytes = 0
        per: list[float] = []
        for path, hint in files.values():
            t = time.perf_counter()
            try:
                json_bytes += savejson.decode_save(path.read_bytes(), bare_size_hint=hint).json_bytes
            except savejson.SaveDecodeError:
                pass
            per.append(time.perf_counter() - t)
        print(f"decode, one core: {time.perf_counter() - t0:.1f} s for {json_bytes / 1e6:.0f} MB of JSON; "
              f"per version median {statistics.median(per) * 1000:.0f} ms, p90 {pct(per, 0.9) * 1000:.0f} ms, "
              f"max {max(per) * 1000:.0f} ms")

    hints = {vid: h for vid, (_p, h) in files.items()}
    with tempfile.TemporaryDirectory() as tmp:
        locator = CopiesLocator(files)
        policy = ExecutionPolicy("processes", args.workers, args.workers >= 3)
        service = IndexService(cache_path=Path(tmp) / "index-v1.sqlite", vault_id="bench", locator=locator,
                               policy=policy, game_running=lambda: False,
                               size_hint=lambda vid: hints.get(vid))
        t0 = time.perf_counter()
        first = None
        service.request(list(files), Priority.BACKFILL)
        while not service.idle():
            if first is None and service.progress().done:
                first = time.perf_counter() - t0
            time.sleep(0.02)
        service.flush()
        wall = time.perf_counter() - t0
        rows = [service.cache.row(v) for v in files]
        statuses: dict[str, int] = {}
        for r in rows:
            statuses[r["status"] if r else "missing"] = statuses.get(r["status"] if r else "missing", 0) + 1
        totals = [sum((r[k] or 0) for k in ("decode_ms", "parse_ms", "extract_ms")) for r in rows if r]
        extract = [r["extract_ms"] or 0 for r in rows if r]
        print(f"index, {args.workers} workers: {wall:.1f} s wall, first result {first or 0:.1f} s; {statuses}")
        print(f"  per version median {statistics.median(totals):.0f} ms, p90 {pct(totals, 0.9):.0f} ms, "
              f"max {max(totals):.0f} ms; extraction max {max(extract)} ms")
        vids = list(files)
        t = time.perf_counter()
        for v in vids:
            service.view(v)
        cold = (time.perf_counter() - t) / len(vids) * 1000
        t = time.perf_counter()
        for v in vids:
            service.view(v)
        warm = (time.perf_counter() - t) / len(vids) * 1000
        t = time.perf_counter()
        pairs = list(zip(vids, vids[1:]))
        for a, b in pairs:
            service.changes(b, a)
        diff = (time.perf_counter() - t) / max(len(pairs), 1) * 1000
        print(f"reads: view() {cold:.2f} ms cold, {warm:.2f} ms warm; changes() {diff:.2f} ms")
        service.close()
        size = sum(f.stat().st_size for f in Path(tmp).iterdir())
        print(f"cache file after close: {size / 1e6:.2f} MB ({size / max(len(files), 1) / 1024:.1f} KB per version)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
