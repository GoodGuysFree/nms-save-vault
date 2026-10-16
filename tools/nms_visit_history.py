"""Extract the "systems visited" history from every No Man's Sky save on this PC.

Read-only. Reuses the project's verified save-decoder (core.lz4_block / core.savedir /
core.locations) to turn each Steam and Xbox / Game Pass save into its obfuscated JSON, then
pulls out the three location-history structures the game keeps and decodes their packed
64-bit galactic addresses into human coordinates + portal codes.

What the game actually stores per save (all under PlayerStateData):
  * VisitedSystems      - a ring buffer of up to 512 galactic addresses (address only).
  * TeleportEndpoints   - named teleporter targets (bases / space stations) with full
                          coordinates, the galaxy index, and the place name.
  * TerrainEditData     - the up-to-256 systems where you reshaped terrain.
And under DiscoveryManagerData: Store.Record - every thing you have discovered
  (systems, planets, fauna, flora, minerals) with a discovery timestamp and discoverer.

A galactic address carries only *where* a system is (voxel X/Y/Z, system index, planet
index) - never its star colour, economy or planet count, which the engine regenerates from
the address. Planet count can be *inferred* by counting discovered Planet records per system.

Usage:
    python tools/nms_visit_history.py            # print a report for every save
    python tools/nms_visit_history.py --csv OUT  # also write per-system CSVs into OUT/

Nothing is ever written back to a save folder.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# Reuse the project's read-only core (verified LZ4 / wgs decoders).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from nms_save_vault.core import lz4_block, locations, savedir  # noqa: E402

# --- obfuscated JSON keys we need (libNOM.map names in comments) -------------------------
# Only the handful of keys this tool reads, so it stays self-contained (no mapping file).
K_BASECONTEXT = "vLc"          # BaseContext (wraps PlayerStateData in these saves)
K_PLAYERSTATE = "6f="          # PlayerStateData
K_VISITED = "nwS"              # VisitedSystems: list[u64 galactic address]
K_TELEPORTS = "nlG"            # TeleportEndpoints
K_TERRAIN = "=o:"              # TerrainEditData
K_TERRAIN_ADDRS = "r:j"        # TerrainEditData.GalacticAddresses
K_DISCOVERY = "fDu"            # DiscoveryManagerData
K_DISCOVERY_V1 = "ETO"         # DiscoveryData-v1
K_STORE = "OsQ"               # Store
K_RECORD = "?fB"              # Record (list of discovery records)
K_DD = "8P3"                  # DD
K_DT = "<Dn"                  # DT (discovery type: SolarSystem / Planet / Animal / ...)
# TeleportEndpoint sub-keys (obfuscated -> readable), for a local de-obfuscation of just
# that subtree. Verified by reverse lookup against libNOM.map.
TP_KEYS = {
    "yhJ": "UniverseAddress", "oZw": "GalacticAddress", "Iis": "RealityIndex",
    "dZj": "VoxelX", "IyE": "VoxelY", "uXE": "VoxelZ",
    "vby": "SolarSystemIndex", "jsv": "PlanetIndex",
    "NKm": "Name", "iAF": "TeleporterType",
}


def to_int(x) -> int:
    return int(x, 16) if isinstance(x, str) else int(x)


def _sign(v: int, width: int) -> int:
    v &= (1 << width) - 1
    return v - (1 << width) if v & (1 << (width - 1)) else v


@dataclass(frozen=True)
class System:
    voxel_x: int
    voxel_y: int
    voxel_z: int
    system_index: int
    planet_index: int
    portal: str
    raw: int

    @property
    def is_filler(self) -> bool:
        # Unused ring-buffer slots decode to the all-ones sentinel (max voxels).
        return (self.voxel_x, self.voxel_y, self.voxel_z) == (2047, 127, 2047)


def decode_address(addr: int) -> System:
    """Decode a packed 64-bit galactic address.

    Verified bit layout (100% of 3311 real entries in range; SolarSystemIndex max 1065
    matches the TeleportEndpoints ground truth, and a teleport address round-trips exactly):
        bits  0-11  VoxelX            (signed 12-bit)
        bits 12-19  VoxelY            (signed 8-bit)
        bits 20-31  VoxelZ            (signed 12-bit)
        bits 32-43  SolarSystemIndex  (12-bit)
        bits 44-47  PlanetIndex       (4-bit)
    Portal / glyph code is the same fields regrouped as P SSS YY ZZZ XXX (the game's
    12-glyph coordinate the user reads in-game)."""
    x = _sign(addr, 12)
    y = _sign(addr >> 12, 8)
    z = _sign(addr >> 20, 12)
    s = (addr >> 32) & 0xFFF
    p = (addr >> 44) & 0xF
    portal = f"{p:X}{s:03X}{(addr >> 12) & 0xFF:02X}{(addr >> 20) & 0xFFF:03X}{addr & 0xFFF:03X}"
    return System(x, y, z, s, p, portal, addr)


def _player_state(obj: dict) -> dict:
    if K_PLAYERSTATE in obj:
        return obj[K_PLAYERSTATE]
    return obj.get(K_BASECONTEXT, {}).get(K_PLAYERSTATE, {})


def _deob_local(node, keymap):
    if isinstance(node, dict):
        return {keymap.get(k, k): _deob_local(v, keymap) for k, v in node.items()}
    if isinstance(node, list):
        return [_deob_local(v, keymap) for v in node]
    return node


@dataclass
class SaveReport:
    platform: str
    slot: int
    name: str
    visited: list[System]
    teleports: list[dict]      # {'name','type','galaxy','system'}
    terrain_systems: int
    discovery_counts: dict


def analyze(obj: dict) -> SaveReport | None:
    ps = _player_state(obj)
    if not ps:
        return None
    visited = [decode_address(to_int(a)) for a in (ps.get(K_VISITED) or [])]

    teleports = []
    for t in ps.get(K_TELEPORTS) or []:
        td = _deob_local(t, TP_KEYS)
        ua = td.get("UniverseAddress", {})
        ga = ua.get("GalacticAddress", {})
        teleports.append({
            "name": td.get("Name", ""),
            "type": td.get("TeleporterType", ""),
            "galaxy": ua.get("RealityIndex"),
            "system": ga,
        })

    terrain = ps.get(K_TERRAIN, {}).get(K_TERRAIN_ADDRS, []) or []
    terrain_systems = len({str(a) for a in terrain})

    counts: Counter = Counter()
    dm = obj.get(K_DISCOVERY, {}).get(K_DISCOVERY_V1, {})
    store = dm.get(K_STORE, {})
    records = store.get(K_RECORD, []) if isinstance(store, dict) else []
    for r in records:
        dt = r.get(K_DD, {}).get(K_DT)
        if dt:
            counts[dt] += 1

    return SaveReport("", 0, "", visited, teleports, terrain_systems, dict(counts))


def decode_save(member) -> dict | None:
    data = Path(member.data_path).read_bytes()
    raw = lz4_block.decompress(data)              # obfuscated JSON bytes
    import json
    txt = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    return json.loads(txt)


def iter_saves():
    for d in locations.find_live_save_dirs():
        yield "steam", d
    for d in locations.find_microsoft_save_dirs():
        yield "xbox", d


def print_report(rep: SaveReport) -> None:
    real = [s for s in rep.visited if not s.is_filler]
    print(f"\n{'='*70}")
    print(f"[{rep.platform}] slot {rep.slot:2d}  \"{rep.name}\"")
    galaxies = sorted({t['galaxy'] for t in rep.teleports if t['galaxy'] is not None})
    print(f"  VisitedSystems : {len(real)} real (+{len(rep.visited)-len(real)} empty slots), "
          f"cap 512, {len({s.raw for s in real})} distinct")
    print(f"  TeleportEndpoints: {len(rep.teleports)} named  | TerrainEdit systems: {rep.terrain_systems}"
          + (f"  | galaxies seen: {galaxies}" if galaxies else ""))
    if rep.discovery_counts:
        tot = sum(rep.discovery_counts.values())
        parts = ", ".join(f"{k}:{v}" for k, v in sorted(rep.discovery_counts.items(), key=lambda kv: -kv[1]))
        print(f"  Discoveries    : {tot} records ({parts})")
    for s in real[:5]:
        print(f"      visited  portal {s.portal}  voxel({s.voxel_x},{s.voxel_y},{s.voxel_z}) "
              f"sys#{s.system_index} planet#{s.planet_index}")
    for t in rep.teleports[:5]:
        g = t["system"]
        print(f"      teleport [{t['type']}] {t['name']!r}  sys#{g.get('SolarSystemIndex')} "
              f"voxel({g.get('VoxelX')},{g.get('VoxelY')},{g.get('VoxelZ')})")


def write_csv(out: Path, rep: SaveReport) -> None:
    out.mkdir(parents=True, exist_ok=True)
    fn = out / f"{rep.platform}_slot{rep.slot:02d}_visited.csv"
    with fn.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["portal", "voxel_x", "voxel_y", "voxel_z", "system_index", "planet_index", "raw_u64"])
        for s in rep.visited:
            if s.is_filler:
                continue
            w.writerow([s.portal, s.voxel_x, s.voxel_y, s.voxel_z, s.system_index, s.planet_index, s.raw])


def main() -> None:
    # Save names can contain characters outside the console's legacy code page.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="Extract NMS visited-systems history (read-only).")
    ap.add_argument("--csv", metavar="DIR", help="also write per-save visited-systems CSVs here")
    args = ap.parse_args()
    out = Path(args.csv) if args.csv else None

    for platform, folder in iter_saves():
        view = savedir.scan_any(folder)
        for slotview in sorted(view.occupied_slots, key=lambda s: s.slot):
            newest = slotview.newest
            if newest is None or not newest.exists:
                continue
            try:
                obj = decode_save(newest)
            except Exception as exc:  # noqa: BLE001 - one bad slot must not stop the scan
                print(f"[{platform}] slot {slotview.slot:2d}: skipped ({exc})")
                continue
            rep = analyze(obj)
            if rep is None:
                continue
            rep.platform, rep.slot, rep.name = platform, slotview.slot, slotview.display_name
            print_report(rep)
            if out:
                write_csv(out, rep)


if __name__ == "__main__":
    main()
