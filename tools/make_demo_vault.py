"""Build a complete FAKE No Man's Sky world and a vault with six weeks of history in it, for
screenshots, demos and manual testing. Nothing here reads or writes a real folder.

    uv run --extra dev python tools/make_demo_vault.py C:\\Devel\\NMSDemo

It makes, under the folder you name (which must be empty or a previous demo world):

    Roaming/HelloGames/NMS/st_76561198000000001   the fake Steam live folder (slots 1-8)
    Local/Packages/.../SystemAppData/wgs/<acct>   the fake Xbox / Game Pass live folder
    Vault                                         the vault, built by the app's own backup
                                                  code with a simulated clock (6 weeks)
    app/state.json                                the app's configuration (light theme)

and prints the environment variables that make the app (run from source or from a kit) see
only those folders. The saves are real container files (LZ4 stream, encrypted meta, wgs
containers) built by the test helpers, with invented names, bases, ships and progress.

Refuses a target inside (or holding) the real HelloGames folders, the Packages folder, the
app's own %LOCALAPPDATA% folder, or D:\\VaultCopy.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The test helpers are the one place the save formats are written (they need pytest, a dev
# extra); the real user folders are noted now, before anything redirects the environment.
from tests.conftest import steam_save, wgs_account_factory                       # noqa: E402
from tests.index import fixtures as fx                                           # noqa: E402
from tests.index.savebuilder import context_save, payload                        # noqa: E402

REAL_ENV = {k: os.environ.get(k, "") for k in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME")}
MARKER = ".nms-demo-world"
STEAM_ACCOUNT = "st_76561198000000001"
XBOX_ACCOUNT = "0000000000000001_29070100B936489ABCE8B9AF3980429C"
PACKAGE = "HelloGames.NoMansSky_bs190hzg1sesy"
#: The world's last day; every file and every vault entry is dated relative to it.
END = datetime(2026, 10, 18, 21, 30, tzinfo=timezone.utc)
SPAN_DAYS = 42
START = END.timestamp() - SPAN_DAYS * 86400
BACKUP_DAYS = (3, 10, 17, 24, 31, 33, 38, 41)
MISSION_VERSION = 40


def day_time(day: int, hour: int = 20) -> int:
    return int(START + day * 86400 + hour * 3600)


# --- refusing real folders --------------------------------------------------------------


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def forbidden_roots() -> list[str]:
    roots = [r"D:\VaultCopy"]
    appdata, local = REAL_ENV["APPDATA"], REAL_ENV["LOCALAPPDATA"]
    if appdata:
        roots.append(os.path.join(appdata, "HelloGames"))
    if local:
        roots += [os.path.join(local, "Packages", PACKAGE), os.path.join(local, "NMSSaveVault")]
    return [_norm(r) for r in roots]


def check_target(target: Path) -> Path:
    """The resolved target, or SystemExit when it is (or holds) a real folder or is not
    empty and not an earlier demo world."""
    target = Path(os.path.abspath(target))
    here = _norm(target)
    if os.path.dirname(here) == here:
        raise SystemExit("refusing a drive root")
    for real in forbidden_roots():
        if here == real or here.startswith(real + os.sep) or real.startswith(here + os.sep):
            raise SystemExit(f"refusing {target}: it is, holds or lies inside a real NMS / app folder")
    if target.exists() and any(target.iterdir()) and not (target / MARKER).is_file():
        raise SystemExit(f"refusing {target}: not empty and not a demo world made by this tool")
    return target


# --- the simulated clock ----------------------------------------------------------------


class Clock:
    """Seconds since the epoch, set by the timeline; the app's clocks read it."""

    def __init__(self) -> None:
        self.t = float(START)

    def __call__(self) -> float:
        return self.t

    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.t, timezone.utc)


# --- what a save holds ------------------------------------------------------------------

SHIP_MODELS = {
    "fighter": fx.FIGHTER,
    "hauler": "MODELS/COMMON/SPACECRAFT/DROPSHIPS/DROPSHIP_PROC.SCENE.MBIN",
    "explorer": "MODELS/COMMON/SPACECRAFT/SCIENTIFIC/SCIENTIFIC_PROC.SCENE.MBIN",
    "solar": "MODELS/COMMON/SPACECRAFT/SAILSHIP/SAILSHIP_PROC.SCENE.MBIN",
    "exotic": "MODELS/COMMON/SPACECRAFT/S-CLASS/S-CLASS_PROC.SCENE.MBIN",
    "shuttle": "MODELS/COMMON/SPACECRAFT/SHUTTLE/SHUTTLE_PROC.SCENE.MBIN",
    "corvette": fx.CORVETTE,
}
PART_IDS = ("CUBEROOM", "LIGHT_BASIC", "PLANTER", "BUILDDOOR", "TELEPORTER", "STORAGE_BOX", "GLASS_PANEL")
LEGACY_PART_IDS = ("C_WALL", "C_FLOOR", "M_FLOOR", "W_ROOF")
WORD_RACES = (0, 1, 2, 4, 8)          # the five races with words to learn (extract/progression)
MILESTONE_AMOUNTS = (1, 1, 3, 5, 1, 10, 2, 1, 5, 20, 1, 3, 10, 1)


@dataclass
class Spec:
    key: str
    pid: int
    name: str
    difficulty: int = 2                     # meta preset: 2 Normal, 3 Creative, 5 Survival, 6 Permadeath
    preset: str = "Normal"
    death: str = "ItemGrave"
    galaxy: int = 0
    summary: str = "On Planet (Oradon)"
    story: int = 0                          # Artemis Path steps done at the end (of 30)
    atlas: int = 0                          # Atlas Path steps done at the end (of 11)
    chains: dict[str, int] = field(default_factory=dict)
    ending: str | None = None
    bases: list[tuple] = field(default_factory=list)   # (name, type, galaxy, final parts, appears at t)
    ships: list[tuple] = field(default_factory=list)   # (name, kind, class)
    pets: list[tuple] = field(default_factory=list)    # (name, creature, biome)
    eggs: int = 0
    pet_slots: int = 3
    freighter: tuple | None = None                     # (name, class, frigates)
    words: dict[int, int] = field(default_factory=dict)
    jm: int = 5                             # journey milestone points (5 per rank)
    units: int = 1_000_000
    nanites: int = 20_000
    quicksilver: int = 2_000
    discoveries: int = 40
    glyphs: int = 16
    deaths: int = 0
    settlement: str | None = None
    legacy: bool = False                    # an old save: some pre-Foundation parts still placed
    expedition: int | None = None           # a SeasonNumber: the save's active context is the expedition


@dataclass(frozen=True)
class Stage:
    day: int
    hours: float
    t: float                                # progress through the save's life, 0..1
    variant: str = ""                       # "B": the rebuilt-base branch of Main Save
    hour_of_day: int = 20


def scaled(final: int, t: float, floor: float = 0.3) -> int:
    return int(round(final * (floor + (1 - floor) * t)))


def _chain(ids: tuple[str, ...], done: int) -> list[dict]:
    from nms_save_vault.core.index import lookups

    return [{"Mission": "^" + m, "Progress": lookups.final_stage(m, MISSION_VERSION)}
            for m in ids[:done]]


def _psd(spec: Spec, st: Stage, *, expedition: bool = False) -> dict:
    from nms_save_vault.core.index.lookups import _overrides

    t = st.t
    chains = _overrides.CHAINS
    progress: list[dict] = []
    story = min(30, int(round(spec.story * t)))
    progress += _chain(chains["artemis"][1], story)
    progress += _chain(chains["atlas"][1], int(round(spec.atlas * t)))
    for name, final in spec.chains.items():
        progress += _chain(chains[name][1], int(round(final * t)))
    if spec.ending and t >= 0.9:
        progress.append({"Mission": "^" + spec.ending, "Progress": 0})
    current = f"ACT{min(3, story // 13 + 1)}_STEP{story % 13 + 1}" if story < 30 else "ACT3_STEP4"

    bases = []
    for index, (bname, btype, galaxy, final, appears) in enumerate(spec.bases):
        if t < appears:
            continue
        parts = max(6, scaled(final, t))
        if st.variant == "B" and index == 0:
            parts = parts * 2 // 3
        bases.append(_base(bname, btype, galaxy, parts, index, spec.legacy))
    if st.variant == "B":
        bases.append(_base("Harbor Point Redux", "HomePlanetBase", 0, 140 + int(t * 120), 9, False))

    ships = [{"Name": n, "Resource": {"Filename": SHIP_MODELS[kind], "Seed": [True, "0x%016X" % (0xA0 + i)]},
              "Inventory": dict(fx._inv(("ROCKET1", 5), capacity=20), Class={"InventoryClass": cls}),
              "Inventory_TechOnly": fx._inv(("SHIPJUMP1", 1), capacity=6)}
             for i, (n, kind, cls) in enumerate(spec.ships)]
    pets = [{"CustomName": n, "CreatureID": "^" + creature, "Biome": {"Biome": biome},
             "CreatureSeed": [True, "0x%X" % (0x10 + i)]}
            for i, (n, creature, biome) in enumerate(spec.pets)]
    groups = max(spec.words.values(), default=0)
    word_groups = []
    for g in range(groups):
        races = [False] * 9
        for race, count in spec.words.items():
            races[race] = g < scaled(count, t, 0.2)
        word_groups.append({"Races": races})

    stats = fx._stats(TIME=float(st.hours * 3600), MONEY=spec.units, DEATHS=spec.deaths,
                      DISC_SYSTEMS=scaled(spec.discoveries // 3 + 3, t), JM=scaled(spec.jm, t, 0.1),
                      WORDS_LEARNT=sum(1 for g in word_groups for r in g["Races"] if r))
    psd = fx._psd(
        TimeStamp=day_time(st.day, st.hour_of_day),
        UniverseAddress={"RealityIndex": spec.galaxy}, SaveSummary=spec.summary,
        Units=scaled(spec.units, t), Nanites=scaled(spec.nanites, t), Specials=scaled(spec.quicksilver, t),
        KnownPortalRunes=(1 << min(16, max(1, scaled(spec.glyphs, t)))) - 1,
        BannerTitleId="^T_EXPLORER", TeleportEndpoints=[{}] * (2 + int(t * 8)),
        KnownTech=["^LAUNCHER", "^SCAN1", "^HYPERDRIVE"] + ["^T%d" % i for i in range(int(t * 30))],
        DifficultyState={"Preset": {"DifficultyPresetType": spec.preset},
                         "EasiestUsedPreset": {"DifficultyPresetType": spec.preset},
                         "HardestUsedPreset": {"DifficultyPresetType": spec.preset},
                         "Settings": {"SettingsLocked": spec.preset == "Permadeath",
                                      "CraftingIsFree": spec.preset == "Creative",
                                      "DeathConsequences": {"DeathConsequencesDifficulty": spec.death}}},
        KnownWordGroups=word_groups,
        KnownProducts=["^FUEL1", "^GARAGE_FREIGHT"] + (["^C_WALL", "^C_FLOOR", "^M_ROOF"] if spec.legacy else []),
        MissionVersion=MISSION_VERSION, CurrentMissionID="^" + current, MissionProgress=progress,
        Stats=stats, PrimaryShip=0, ShipOwnership=ships, ArchivedShipOwnership=[],
        PersistentPlayerBases=bases,
        BaseBuildingObjects=[fx._obj("GARAGE_M"), fx._obj("SUMMON_GARAGE")],
        Pets=pets, Eggs=[{"CreatureID": "^QUAD_PET"}] * spec.eggs,
        UnlockedPetSlots=[True] * spec.pet_slots + [False],
        Multitools=[{"Name": "Atlas Staff" if i else "Kestrel", "Resource": {"Filename": fx.STAFF},
                     "Store": dict(fx._inv(("LASER", 1)), Class={"InventoryClass": "S"})} for i in range(2)],
        SettlementHistory=[], SettlementStatesV2=[],
    )
    if spec.settlement and t >= 0.5:
        psd["SettlementHistory"] = [{"SeedValue": "0x00000000000000AA", "PlayerClaimedTime": 1_789_000_000.0}]
        psd["SettlementStatesV2"] = [{"SeedValue": "0xAA", "Name": spec.settlement}]
    if spec.freighter:
        name, cls, frigates = spec.freighter
        psd["CurrentFreighter"] = {"Filename": fx.FREIGHTER}
        psd["PlayerFreighterName"] = name
        psd["FreighterInventory"] = dict(fx._inv(("FUEL1", 100)), Class={"InventoryClass": cls})
        psd["FleetFrigates"] = [{}] * scaled(frigates, t, 0.4)
    else:
        for k in ("CurrentFreighter", "PlayerFreighterName", "FreighterInventory"):
            psd.pop(k)
        psd["FleetFrigates"] = []
    if expedition:
        psd["PersistentPlayerBases"] = psd["PersistentPlayerBases"][:1]
        psd["Units"] = 40_000 + int(t * 900_000)
    return psd


def _base(name: str, btype: str, galaxy: int, parts: int, index: int, legacy: bool) -> dict:
    ids = PART_IDS + LEGACY_PART_IDS if legacy else PART_IDS
    objects = [fx._obj("BASE_FLAG")] + [fx._obj(ids[i % len(ids)]) for i in range(parts - 1)]
    address = "0x%016X" % ((galaxy << 32) | (0x123 + index * 0x1111))
    return fx._base(btype, name, objects, address, [10.0 + index * 31, 20.0, 30.0])


def render(spec: Spec, st: Stage) -> bytes:
    """The save's JSON (obfuscated keys, NUL-terminated), as the game would write it."""
    common = {"SaveName": spec.name, "TotalPlayTime": int(st.hours * 3600),
              "SaveUniversalId": "0x%016X" % spec.pid}
    base_psd = _psd(spec, st)
    discoveries = [{"DD": {"DT": ("SolarSystem", "Planet", "Planet", "Animal", "Flora")[i % 5]}}
                   for i in range(scaled(spec.discoveries, st.t))]
    if spec.expedition:
        total = len(MILESTONE_AMOUNTS)
        done = int(total * st.t)
        common["SeasonData"] = {
            "SeasonId": spec.expedition, "SeasonNumber": spec.expedition, "DisplayNumber": 14,
            "SeasonName": "^UI_SEASON_%d_NAME" % spec.expedition,
            "Stages": [{"Milestones": [{"Amount": a, "IsOptional": False} for a in MILESTONE_AMOUNTS]}]}
        common["SeasonState"] = {
            "MilestoneValues": [a if i < done else 0 for i, a in enumerate(MILESTONE_AMOUNTS)],
            "HasCollectedFinalReward": False}
        tree = context_save(base=base_psd, expedition=_psd(spec, st, expedition=True), common=common,
                            active="Season", discoveries=discoveries)
    else:
        tree = context_save(base=base_psd, common=common, discoveries=discoveries)
    return payload(tree)


# --- the cast ---------------------------------------------------------------------------

PIDS = {
    "main": 0x7A1C0DE5B3F40911, "perma": 0x3C5E8A1F90D2B746, "creative": 0x51B7D3E9A0C48F12,
    "expedition": 0x2F90A6C4E1D85B37, "freighter": 0x6E4D1B8A5C9F2073, "pirate": 0x1D8F3A5B7E2C6490,
    "atlas": 0x4B2E9C7D1A6F8035, "survival": 0x08A3F5D2C9E17B64, "old": 0x5C6A2E8F0B4D9317,
    "wanderer": 0x73D1B5F9A2E40C86,
}

SPECS: dict[str, Spec] = {s.key: s for s in (
    Spec("main", PIDS["main"], "Main Save", legacy=True, story=30, atlas=11, ending="EPILOGUE_NEWGAL", jm=47,
         bases=[("Harbor Point", "HomePlanetBase", 0, 640, 0.0), ("Crystal Annex", "HomePlanetBase", 0, 310, 0.3),
                ("Windfall Outpost", "HomePlanetBase", 0, 120, 0.5), ("Skyhold", "FreighterBase", 0, 180, 0.4)],
         ships=[("Silver Heron", "fighter", "S"), ("Starling", "hauler", "A"), ("Wanderer's Rest", "explorer", "S"),
                ("Dawn Sail", "solar", "B")],
         pets=[("Biscuit", "FLYINGLIZARD", "Lush"), ("Pixel", "CAT", "Dead"), ("Moss", "SIXLEGCOW", "Lush")],
         eggs=1, pet_slots=4, freighter=("Distant Hope", "S", 9), words={0: 24, 1: 18, 2: 22, 4: 12, 8: 6},
         units=1_850_000_000, nanites=96_000, quicksilver=14_000, discoveries=620, glyphs=16, deaths=11,
         settlement="Kestrel Landing", chains={"sentinel": 5, "titan": 4, "base_computer": 12}),
    Spec("perma", PIDS["perma"], "Permadeath Run", difficulty=6, preset="Permadeath", death="Permadeath",
         summary="In Space Station", story=14, jm=12, deaths=0,
         bases=[("Last Stand", "HomePlanetBase", 0, 90, 0.0)],
         ships=[("Long Shot", "shuttle", "B"), ("Second Wind", "fighter", "A")],
         pets=[("Lucky", "TREX", "Scorched")], words={0: 6, 1: 4, 2: 5}, units=210_000_000, nanites=14_000,
         discoveries=80, glyphs=8),
    Spec("creative", PIDS["creative"], "Creative Builds", difficulty=3, preset="Creative", death="Nothing",
         summary="On Planet (Ilvara Prime)", story=9, jm=21, galaxy=3,
         bases=[("Glass Cathedral", "HomePlanetBase", 3, 760, 0.0), ("Sky Garden", "HomePlanetBase", 3, 540, 0.2),
                ("The Long Hall", "HomePlanetBase", 3, 410, 0.4), ("Spire Forty", "HomePlanetBase", 3, 280, 0.6)],
         ships=[("Crystal Pearl", "exotic", "S")], pets=[("Pebble", "ROBOTANTELOPE", "Barren")],
         words={0: 12, 1: 12, 2: 12, 4: 12, 8: 12}, units=900_000_000, nanites=60_000, discoveries=150, glyphs=16),
    Spec("expedition", PIDS["expedition"], "Expedition: Liquidators", story=3, jm=8, expedition=27,
         summary="On Planet (Tessera)", bases=[("Camp Meridian", "HomePlanetBase", 0, 60, 0.0)],
         ships=[("Gutter Runner", "fighter", "C")], pets=[], words={0: 3, 1: 2}, units=3_000_000, nanites=1_500,
         discoveries=60, glyphs=6),
    Spec("freighter", PIDS["freighter"], "Freighter Base", story=22, jm=31, summary="In Freighter",
         bases=[("Cargo Bay Nine", "FreighterBase", 0, 520, 0.0), ("Landing Pad", "HomePlanetBase", 0, 70, 0.0)],
         ships=[("Iron Gull", "hauler", "S"), ("Patchwork", "fighter", "B")],
         pets=[("Gizmo", "ROBO_PET", "Dead"), ("Pudding", "BONECOW", "Frozen")], freighter=("Leviathan IV", "S", 14),
         words={0: 15, 1: 15, 2: 10, 4: 6}, units=640_000_000, nanites=44_000, discoveries=240, glyphs=14, deaths=4),
    Spec("pirate", PIDS["pirate"], "Pirate Life", story=18, jm=26, summary="On Planet (Skarn)",
         chains={"pirates": 3, "sentinel": 3}, galaxy=9,
         bases=[("Black Flag Cove", "HomePlanetBase", 9, 210, 0.0)],
         ships=[("Jolly Roger", "exotic", "S"), ("Dinghy", "shuttle", "C")],
         pets=[("Bosun", "GRUNT", "Scorched")], freighter=("The Salty Dog", "A", 8),
         words={0: 9, 1: 14, 2: 7}, units=420_000_000, nanites=33_000, discoveries=190, glyphs=12, deaths=17),
    Spec("atlas", PIDS["atlas"], "Atlas Path", story=26, atlas=7, jm=19, summary="On Planet (Veyra)",
         bases=[("Station Zero", "HomePlanetBase", 0, 160, 0.0)], ships=[("Lantern", "explorer", "A")],
         pets=[("Nova", "FLYINGSNAKE", "Lush")], words={0: 8, 1: 8, 2: 8, 4: 10}, units=330_000_000,
         nanites=28_000, discoveries=130, glyphs=14),
    Spec("survival", PIDS["survival"], "Survival Test", difficulty=5, preset="Survival", death="ItemGrave",
         summary="On Planet (Quillon)", story=4, jm=3, bases=[("Shack", "HomePlanetBase", 0, 25, 0.0)],
         ships=[("Rustbucket", "fighter", "C")], words={0: 2}, units=900_000, nanites=1_200, discoveries=25, glyphs=4),
    Spec("old", PIDS["old"], "First Playthrough", legacy=True, story=30, atlas=11, jm=40, summary="On Planet (Haldor)",
         bases=[("Old Hearth", "HomePlanetBase", 0, 380, 0.0)], ships=[("Founder", "fighter", "S")],
         pets=[("Rex", "TREX", "Scorched")], freighter=("Old Faithful", "B", 6),
         words={0: 20, 1: 20, 2: 20, 4: 11, 8: 5}, units=1_200_000_000, nanites=70_000, discoveries=400, glyphs=16),
    Spec("wanderer", PIDS["wanderer"], "Waypoint Wanderer", story=10, jm=14, summary="In Space Station", galaxy=1,
         bases=[("Roadside", "HomePlanetBase", 1, 85, 0.0)], ships=[("Drifter", "solar", "A")],
         pets=[("Dot", "FLYINGBEETLE", "Toxic")], words={0: 5, 1: 5, 2: 4}, units=75_000_000, nanites=9_000,
         discoveries=70, glyphs=8),
)}

# Steam slot per save (slot 9 is the First Playthrough, cleared on day 5 to make room).
STEAM_SLOTS = {"main": 1, "perma": 2, "creative": 3, "expedition": 4, "freighter": 5, "pirate": 6,
               "atlas": 7, "survival": 8, "old": 9}
XBOX_IDENT = {"atlas": "Slot1Auto", "wanderer": "Slot2Auto"}

STEAM_PLAN: dict[str, dict[int, Stage]] = {
    "main": {3: Stage(3, 62, 0.40), 10: Stage(10, 90, 0.60), 17: Stage(17, 118, 0.80),
             24: Stage(24, 141, 0.92), 33: Stage(33, 128, 0.88, "B"), 38: Stage(38, 150, 1.0, "B")},
    "perma": {3: Stage(3, 8, 0.25), 10: Stage(10, 19, 0.5), 17: Stage(17, 31, 0.8), 24: Stage(24, 41, 1.0)},
    "creative": {10: Stage(10, 20, 0.35), 17: Stage(17, 33, 0.55), 24: Stage(24, 47, 0.8), 38: Stage(38, 61, 1.0)},
    "expedition": {17: Stage(17, 5, 0.25), 24: Stage(24, 14, 0.5), 33: Stage(33, 26, 0.75), 41: Stage(41, 33, 0.9)},
    "freighter": {3: Stage(3, 44, 0.6), 17: Stage(17, 58, 0.8), 24: Stage(24, 71, 1.0)},
    "pirate": {24: Stage(24, 16, 0.4), 33: Stage(33, 29, 0.7), 41: Stage(41, 38, 1.0)},
    "atlas": {10: Stage(10, 25, 0.5), 17: Stage(17, 40, 0.7), 24: Stage(24, 52, 0.85), 38: Stage(38, 61, 1.0)},
    "survival": {38: Stage(38, 6, 0.5), 41: Stage(41, 12, 1.0)},
    "old": {3: Stage(3, 90, 1.0)},
}
XBOX_PLAN: dict[str, dict[int, Stage]] = {
    "atlas": {10: Stage(10, 24, 0.5, hour_of_day=18), 24: Stage(24, 52, 0.85, hour_of_day=18),
              38: Stage(38, 59, 0.97, hour_of_day=18)},
    "wanderer": {10: Stage(10, 15, 0.4), 24: Stage(24, 22, 0.65), 38: Stage(38, 30, 1.0)},
}
CLEAR_OLD_DAY = 5
RESTORE_DAY = 31        # Main Save goes back to its day-17 version in the live slot
BRANCH_POINT = 17
LABELS = {3: "first backup", 24: "before the update", 41: "latest"}


# --- writing the live folders ------------------------------------------------------------


def _stamp(folder: Path, when: int) -> None:
    for p in folder.rglob("*"):
        if p.is_file():
            os.utime(p, (when, when))


def write_steam(folder: Path, key: str, st: Stage) -> None:
    """Both members of the slot: the game's newest save and the one just before it."""
    spec = SPECS[key]
    slot = STEAM_SLOTS[key]
    ts = day_time(st.day, st.hour_of_day)
    before = Stage(st.day, st.hours - 0.04, st.t * 0.985, st.variant, st.hour_of_day)
    for member, stage, saved in ((0, st, ts), (1, before, ts - 60)):
        steam_save(folder, slot, member, spec.name, int(stage.hours * 3600), spec.pid, saved,
                   payload=render(spec, stage), difficulty=spec.difficulty)


def clear_slot(folder: Path, slot: int) -> None:
    from nms_save_vault.core import slotmap

    for fno in slotmap.slot_file_numbers(slot):
        for name in (slotmap.data_filename(fno), slotmap.meta_filename(fno)):
            (folder / name).unlink(missing_ok=True)


def write_xbox(parent: Path, current: dict[str, Stage], when: int) -> Path:
    """The wgs account folder from scratch (the game rewrites it as a whole)."""
    account = parent / "wgs" / XBOX_ACCOUNT
    if account.exists():
        shutil.rmtree(account)
    saves = []
    for key, st in current.items():
        spec = SPECS[key]
        data = render(spec, st).decode("utf-8", "surrogateescape")
        saved = day_time(st.day, st.hour_of_day)
        saves.append((XBOX_IDENT[key], data, spec.name, spec.summary, int(st.hours * 3600), saved, spec.pid))
    account = wgs_account_factory()(parent, saves)
    _stamp(account, when)
    return account


# --- the build --------------------------------------------------------------------------


@dataclass
class Demo:
    target: Path
    steam: Path
    xbox: Path
    vault: Path
    env: dict[str, str]
    saves: list[str]


def environment(target: Path) -> dict[str, str]:
    """Not USERPROFILE: a Microsoft Store Python cannot start the index's worker processes
    when it is redirected, and the app finds nothing there on Windows."""
    home = target / "home"
    return {
        "NMSVAULT_PORTABLE_ROOT": str(target / "app"),
        "APPDATA": str(target / "Roaming"),
        "LOCALAPPDATA": str(target / "Local"),
        "HOME": str(home),
    }


@contextlib.contextmanager
def redirected(env: dict[str, str]):
    """The process sees only the fake folders while the vault is built."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def build(target: Path, *, log=lambda msg: None) -> Demo:
    target = check_target(target)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / MARKER).write_text("a fake NMS world made by tools/make_demo_vault.py\n", encoding="ascii")
    env = environment(target)
    for key in ("home", "app"):
        (target / key).mkdir()
    with redirected(env):
        return _build(target, env, log)


ACCOUNT_NAMES = {"steam": "Main PC", "xbox": "Living room"}


def _name_accounts(state) -> None:
    """Display names for the demo accounts, through the app's own alias file (what
    ``nmsvault accounts --set`` writes), so no account id shows in the window."""
    from nms_save_vault.core import aliases

    amap = aliases.load()
    for source in state.sources:
        name = ACCOUNT_NAMES.get(source.platform)
        if name and source.account:
            amap.set(source.account, name)
    aliases.save(amap)
    aliases.set_active(amap)


def _build(target: Path, env: dict[str, str], log) -> Demo:
    from nms_save_vault.core import discover, operations, state as appstate, store as vstore
    from nms_save_vault.core.catalog import Vault
    from nms_save_vault.core.organizer import Organizer, recorder
    from nms_save_vault.core.organizer.cache import ScanCache
    from nms_save_vault.core.store import classic

    nms_root = target / "Roaming" / "HelloGames" / "NMS"
    steam = nms_root / STEAM_ACCOUNT
    xbox_parent = target / "Local" / "Packages" / PACKAGE / "SystemAppData"
    vault_root = target / "Vault"
    steam.mkdir(parents=True)
    (steam / "steam_autocloud.vdf").write_text('"steam_autocloud.vdf"\n{\n}\n', encoding="ascii")

    clock = Clock()
    vstore.create_store(vault_root, clock=clock.dt)
    vault = Vault(vault_root)
    xbox: Path | None = None            # the first Xbox save appears on day 10
    last_steam: dict[str, Stage] = {}
    last_xbox: dict[str, Stage] = {}
    written: dict[tuple[str, int], Stage] = {}
    state = appstate.AppState(vault=str(vault_root), theme="light", update_check="off",
                              ui={"index_banner_done": True})
    appstate.save(state)
    state_ready = False

    recorder_defaults = recorder.observe_folder.__defaults__
    # The journal stamps what it sees with the clock observe_folder is given (a default).
    recorder.observe_folder.__defaults__ = (clock,)
    try:
        for day in range(0, SPAN_DAYS):
            clock.t = day_time(day)
            changed_steam = changed_xbox = False
            if day == CLEAR_OLD_DAY:
                clear_slot(steam, STEAM_SLOTS["old"])
                last_steam.pop("old", None)
                changed_steam = True
            for key, plan in STEAM_PLAN.items():
                st = plan.get(day)
                if st:
                    write_steam(steam, key, st)
                    last_steam[key] = st
                    written[(key, day)] = st
                    changed_steam = True
            if day == RESTORE_DAY:
                write_steam(steam, "main", written[("main", BRANCH_POINT)])
                last_steam["main"] = written[("main", BRANCH_POINT)]
                changed_steam = True
            for key, plan in XBOX_PLAN.items():
                st = plan.get(day)
                if st:
                    last_xbox[key] = st
                    changed_xbox = True
            if changed_steam:
                _stamp(steam, int(clock.t))
            if changed_xbox:
                xbox = write_xbox(xbox_parent, last_xbox, int(clock.t))
            if not state_ready and xbox is not None:
                state.sources = discover.discover_live_sources(nms_root, [xbox])
                appstate.save(state)
                state_ready = True
            if day in BACKUP_DAYS:
                label = LABELS.get(day, "")
                classic._opened[vault] = vstore.open_store(vault_root, app_state=state, clock=clock.dt)
                for folder in [steam] + ([xbox] if xbox is not None else []):
                    entry = operations.create_full_backup(vault, folder, label=label, warnings=[])
                    log(f"day {day:2d}  backup {entry.id} of {folder.name}")
        # The journal and the notes
        state.sources = discover.discover_live_sources(nms_root, [xbox])
        appstate.save(state)
        _name_accounts(state)
        organizer = Organizer(vault_root, state.live_sources, cache=ScanCache(None, None),
                              game_running=lambda: False, clock=clock)
        log(_annotate(organizer, clock))
    finally:
        recorder.observe_folder.__defaults__ = recorder_defaults
    names = [s.name for s in SPECS.values()]
    return Demo(target, steam, xbox, vault_root, env, names)


def _annotate(organizer, clock: Clock) -> str:
    """Notes, a version label and the pin, written through the app's own notes API."""
    clock.t = day_time(SPAN_DAYS - 1)
    model = organizer.refresh(record=True)
    by_name = {}
    for line in model.lines():
        by_name[line.name] = line
    pid = lambda key: by_name[SPECS[key].name].pid                                       # noqa: E731
    organizer.notes.set_save_note(
        pid("main"), "Main run.\nBranched at 118 h: the rebuilt Harbor Point is the experiment.\n"
                     "Next: finish the base computer archive.", base_rev=None)
    organizer.notes.set_pinned(pid("main"), True)
    organizer.notes.set_save_note(pid("perma"), "Permadeath. One life - do not fly into the sun.", base_rev=None)
    organizer.notes.set_save_note(pid("creative"), "Screenshot builds for the channel.", base_rev=None)
    versions = model.versions(pid("main"))
    point = next(v for v in versions if v.play_time == 118 * 3600 and v.platform == "steam")
    organizer.notes.set_version_note(point.head, pair=point.pair, pid=point.pid,
                                     label="Before the base experiment",
                                     comment="Last version both branches share.", locked=True)
    organizer.refresh(record=False)
    return "notes: Main Save (pinned, labelled version 118 h), Permadeath Run, Creative Builds"


# --- command line -----------------------------------------------------------------------


def powershell_env(env: dict[str, str]) -> str:
    return "\n".join(f"$env:{k} = '{v}'" for k, v in env.items())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target", help="an empty folder (or an earlier demo world) to build the fake world in")
    args = ap.parse_args(argv)
    demo = build(Path(args.target), log=print)
    print(f"\nDemo world ready in {demo.target}")
    print("Run the app against it (PowerShell):\n")
    print(powershell_env(demo.env))
    print(f"uv run python -m nms_save_vault.gui --vault '{demo.vault}'   # or: nmsvault ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
