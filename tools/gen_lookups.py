"""Regenerate the content index's game tables from an unpacked game dump (dev-only).

Reads the dump's metadata index (one sqlite table ``props(file, entry, path, value)`` over
every .mbin, e.g. ``F:/NMS-Unpack/work/meta-<build>.sqlite``) and rewrites the generated
modules in ``src/nms_save_vault/core/index/lookups/``:

  parts.py      legacy base part ids (build-menu groups BASIC_LEGACY / FREIGHT_LEGACY)
  missions.py   final-stage change points of every mission in _overrides.CHAINS / CHOICES
  seasons.py    expedition number -> (display number, English name); season name keys
  journey.py    journey rank names and points per rank
  galaxies.py   the 256 galaxy names (the game names only a few; pass the community list
                with --galaxies, else the current names are kept)

Curated data (chain membership, ship types, creature names) lives in ``_overrides.py`` and is
never written here. Every name must be ASCII (the UI draws through fonts that cannot show
anything else); a non-ASCII name is an error, never written.

    python tools/gen_lookups.py --meta F:/NMS-Unpack/work/meta-25729564.sqlite --build 25729564
    python tools/gen_lookups.py --check      # exit 1 if the modules are not generator output
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from nms_save_vault.core.index.lookups import _overrides  # noqa: E402

OUT = ROOT / "src" / "nms_save_vault" / "core" / "index" / "lookups"
PARTS_TABLE = "metadata/reality/tables/basebuildingobjectstable.mbin"
MISSION_TABLES = "metadata/simulation/missions/tables/"
SKIPPED_MISSION_TABLES = ("debugmissiontable.mbin", "deprecatedmissiontable.mbin")
SEASONS_TABLE = "metadata/reality/tables/historicalseasondatatable.mbin"
JOURNEY_TABLE = "metadata/gamestate/stats/journeymilestonetable.mbin"
LEGACY_GROUPS = {"structures": "BASIC_LEGACY", "freighter": "FREIGHT_LEGACY"}
GENERATED = ("parts.py", "missions.py", "seasons.py", "journey.py", "galaxies.py")


@dataclass
class Tables:
    build: str
    date: str
    legacy_structures: list[str] = field(default_factory=list)
    legacy_freighter: list[str] = field(default_factory=list)
    final_stages: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    expeditions: dict[int, tuple[int, str]] = field(default_factory=dict)
    season_names: dict[str, str] = field(default_factory=dict)
    journey_ranks: list[str] = field(default_factory=list)
    points_per_rank: int = 5
    galaxies: list[str] = field(default_factory=list)


# --- reading the dump ------------------------------------------------------------------------


def wanted_missions() -> list[str]:
    ids = [m for _label, chain in _overrides.CHAINS.values() for m in chain]
    return sorted(set(ids) | set(_overrides.CHOICES))


def read_dump(meta: Path, build: str, galaxies: list[str], date: str) -> Tables:
    con = sqlite3.connect(f"file:{Path(meta).as_posix()}?mode=ro", uri=True)
    try:
        t = Tables(build=build, date=date, galaxies=list(galaxies))
        groups: dict[str, set[str]] = {}
        for entry, value in con.execute("SELECT entry, value FROM props WHERE file = ? AND path = ? ORDER BY id",
                                        (PARTS_TABLE, "Objects.Groups.Groups.Group")):
            groups.setdefault(value, set()).add(entry)
        t.legacy_structures = sorted(groups.get(LEGACY_GROUPS["structures"], ()))
        t.legacy_freighter = sorted(groups.get(LEGACY_GROUPS["freighter"], ()))
        t.final_stages = _final_stages(con, wanted_missions())
        english = _english(con)
        t.season_names = {k: english[k] for k in sorted(english) if re.fullmatch(r"UI_SEASON_\w+_NAME", k)}
        for row in _records(con, SEASONS_TABLE, "SeasonName"):
            number, display = int(row["SeasonNumber"]), int(row["DisplayNumber"])
            t.expeditions[number] = (display, english.get(row["SeasonName"], row["SeasonName"]))
        ranks = _records(con, JOURNEY_TABLE, "JourneyMilestoneId")
        t.journey_ranks = [english.get(r["JourneyMilestoneTitleLower"], r["JourneyMilestoneId"]).title() for r in ranks]
        points = [int(r["PointsToUnlock"]) for r in ranks]
        t.points_per_rank = points[1] - points[0] if len(points) > 1 else 5
        if points != [i * t.points_per_rank for i in range(len(points))]:
            raise ValueError(f"journey points are not evenly spaced: {points}")
        return t
    finally:
        con.close()


def _final_stages(con: sqlite3.Connection, ids: list[str]) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    marks = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT file, entry, path, value FROM props WHERE file LIKE ? AND entry IN ({marks}) "
        "AND path LIKE 'Missions.FinalStageVersions.FinalStageVersions.%' ORDER BY id",
        (MISSION_TABLES + "%", *ids))
    pending: dict[tuple[str, str], int] = {}
    seen_in: dict[str, str] = {}
    for file, entry, path, value in rows:
        if file.endswith(SKIPPED_MISSION_TABLES) or seen_in.setdefault(entry, file) != file:
            continue
        if path.endswith(".Version"):
            pending[(file, entry)] = int(value)
        else:
            out.setdefault(entry, []).append((pending.pop((file, entry)), int(value)))
    return {k: change_points(v) for k, v in sorted(out.items())}


def change_points(stages: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Keep only the versions where the final stage changes (ACT1_STEP13: 27, 46, 49, 50, 54)."""
    kept: list[tuple[int, int]] = []
    for version, progress in sorted(stages):
        if not kept or kept[-1][1] != progress:
            kept.append((version, progress))
    return kept


def _english(con: sqlite3.Connection) -> dict[str, str]:
    return {entry: value for entry, value in con.execute(
        "SELECT entry, value FROM props WHERE file LIKE 'language/%english.mbin' AND path = 'Table.English' ORDER BY id")}


def _records(con: sqlite3.Connection, file: str, first: str) -> list[dict[str, str]]:
    """Rows of a table whose entries carry no id: a new record starts at its ``first`` field."""
    out: list[dict[str, str]] = []
    for path, value in con.execute("SELECT path, value FROM props WHERE file = ? ORDER BY id", (file,)):
        name = path.rsplit(".", 1)[-1]
        if name == first:
            out.append({})
        if out:
            out[-1].setdefault(name, value)
    return out


# --- writing modules -------------------------------------------------------------------------


def _header(t: Tables, what: str, source: str) -> str:
    return (f'"""{what}\n\nGenerated by tools/gen_lookups.py from game build {t.build} on {t.date}\n'
            f'({source}).\nGenerated - do not edit; curated changes go in _overrides.py.\n"""\n')


def _s(value: str) -> str:
    if not value.isascii():
        raise ValueError(f"non-ASCII name refused: {value!r}")
    return json.dumps(value)


def _wrap(parts: list[str], indent: str = "    ", width: int = 100) -> str:
    lines: list[str] = []
    line = indent
    for p in parts:
        piece = p + ","
        if len(line) + len(piece) + 1 > width and line.strip():
            lines.append(line.rstrip())
            line = indent
        line += piece + " "
    if line.strip():
        lines.append(line.rstrip())
    return "\n".join(lines)


def render(t: Tables) -> dict[str, str]:
    if len(t.galaxies) != 256:
        raise ValueError(f"expected 256 galaxy names, got {len(t.galaxies)}")
    files: dict[str, str] = {}
    files["parts.py"] = (
        _header(t, "Base part ids used at extraction time (a change here bumps EXTRACTOR_VERSION).",
                "basebuildingobjectstable.mbin, build-menu groups BASIC_LEGACY and FREIGHT_LEGACY")
        + "\nLEGACY_STRUCTURES = frozenset({\n" + _wrap([_s(x) for x in t.legacy_structures]) + "\n})\n"
        + "\nLEGACY_FREIGHTER = frozenset({\n" + _wrap([_s(x) for x in t.legacy_freighter]) + "\n})\n")
    stages = [f"    {_s(k)}: ({''.join(f'({v}, {p}), ' for v, p in pts).rstrip(', ')}{',' if len(pts) == 1 else ''}),"
              for k, pts in sorted(t.final_stages.items())]
    files["missions.py"] = (
        _header(t, "Mission final stages: (MissionVersion, final Progress) at each change point.",
                "metadata/simulation/missions/tables/*.mbin, Missions.FinalStageVersions")
        + "\nFINAL_STAGES: dict[str, tuple[tuple[int, int], ...]] = {\n" + "\n".join(stages) + "\n}\n")
    expeditions = [f"    {n}: ({d}, {_s(name)})," for n, (d, name) in sorted(t.expeditions.items())]
    names = [f"    {_s(k)}: {_s(v)}," for k, v in sorted(t.season_names.items())]
    files["seasons.py"] = (
        _header(t, "Expedition names.", "historicalseasondatatable.mbin and the English language tables")
        + "\n# SeasonNumber -> (display number, English name)\nEXPEDITIONS: dict[int, tuple[int, str]] = {\n"
        + "\n".join(expeditions) + "\n}\n"
        + "\n# SeasonName key -> English name (covers remixed expeditions the table above lacks)\n"
        + "SEASON_NAME_KEYS: dict[str, str] = {\n" + "\n".join(names) + "\n}\n")
    files["journey.py"] = (
        _header(t, "Journey milestone ranks.", "journeymilestonetable.mbin and the English language tables")
        + "\nJOURNEY_RANKS = (\n" + _wrap([_s(x) for x in t.journey_ranks]) + "\n)\n"
        + f"POINTS_PER_RANK = {t.points_per_rank}\n")
    files["galaxies.py"] = (
        _header(t, "The 256 galaxy names, by RealityIndex.", "game names for the first galaxies plus the community list")
        + "\nGALAXY_NAMES = (\n" + _wrap([_s(x) for x in t.galaxies]) + "\n)\n")
    return files


def tables_from_modules() -> Tables:
    """The committed modules' data, so a test can check they are exactly generator output."""
    from nms_save_vault.core.index.lookups import galaxies, journey, missions, parts, seasons

    m = re.search(r"game build (\S+) on (\S+)", parts.__doc__ or "")
    if not m:
        raise ValueError("parts.py has no generator header")
    return Tables(
        build=m.group(1), date=m.group(2).rstrip("."),
        legacy_structures=sorted(parts.LEGACY_STRUCTURES), legacy_freighter=sorted(parts.LEGACY_FREIGHTER),
        final_stages={k: list(v) for k, v in missions.FINAL_STAGES.items()},
        expeditions=dict(seasons.EXPEDITIONS), season_names=dict(seasons.SEASON_NAME_KEYS),
        journey_ranks=list(journey.JOURNEY_RANKS), points_per_rank=journey.POINTS_PER_RANK,
        galaxies=list(galaxies.GALAXY_NAMES),
    )


def write(files: dict[str, str], out: Path = OUT) -> None:
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8", newline="\n")


def check(out: Path = OUT) -> list[str]:
    """Generated modules that differ from what the generator writes for their own data."""
    files = render(tables_from_modules())
    return [n for n, text in files.items()
            if (out / n).read_text(encoding="utf-8").replace("\r\n", "\n") != text]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--meta", type=Path, help="the dump's metadata index (sqlite)")
    ap.add_argument("--build", help="game build number the dump came from")
    ap.add_argument("--galaxies", type=Path, help="JSON list of the 256 galaxy names (default: keep current)")
    ap.add_argument("--check", action="store_true", help="only verify the modules are generator output")
    args = ap.parse_args(argv)
    if args.check:
        stale = check()
        print("generated modules up to date" if not stale else f"not generator output: {', '.join(stale)}")
        return 1 if stale else 0
    if not args.meta or not args.build:
        ap.error("--meta and --build are required (or --check)")
    galaxies = (json.loads(args.galaxies.read_text(encoding="utf-8")) if args.galaxies
                else tables_from_modules().galaxies)
    tables = read_dump(args.meta, args.build, galaxies, datetime.date.today().isoformat())
    missing = [m for m in wanted_missions() if m not in tables.final_stages and m not in _overrides.CHOICES]
    if missing:
        print(f"warning: no final stages in the dump for {', '.join(missing)}")
    write(render(tables))
    print(f"wrote {', '.join(GENERATED)} from build {args.build}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
