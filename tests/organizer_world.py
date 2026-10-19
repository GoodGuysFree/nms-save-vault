"""A small synthetic organizer world for the UI tests (D6 6): built from real (synthetic)
save files through the real scanner and read model, so the fixture cannot drift from D1.

Two live sources - Steam (slot 1 empty) and Xbox - and a vault:

- "Main Save" (MAIN): live in Steam slot 9, noted and pinned; two versions linked by the
  live journal - #1 backed up, #2 (played on since) not in the vault;
- "Other Run" (OTHER): live in Steam slot 3, never backed up (NO BACKUP YET);
- "Cross Save" (CROSS): Steam slot 5 and Xbox slot 1, the Xbox copy 2h behind;
- "Old Run" (OLD): only in the vault (an old backup);
- "Xbox Only" (XONLY): Xbox slot 2.

All ids are fake; no real account id, name or path is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nms_save_vault.core import operations
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.organizer import Organizer
from nms_save_vault.core.organizer.cache import ScanCache
from nms_save_vault.core.organizer.readmodel import ReadModel
from nms_save_vault.core.state import Source

from .conftest import steam_save

STEAM_ACCOUNT = "st_76561198000000001"
MAIN, OTHER, CROSS, OLD, XONLY = (0xDDDAD1BDD39F9C72, 0x9E8385F0AB12CD34, 0x1111222233334444,
                                  0x5555666677778888, 0x0A0B0C0D0E0F1011)
PIDS = {name: f"{value:016X}" for name, value in
        (("main", MAIN), ("other", OTHER), ("cross", CROSS), ("old", OLD), ("xonly", XONLY))}
T0 = 1_790_000_000


@dataclass
class World:
    vault: Vault
    steam: Path
    xbox: Path
    sources: list[Source]
    organizer: Organizer
    model: ReadModel

    def refresh(self) -> ReadModel:
        self.model = self.organizer.refresh(record=False)
        return self.model


def build_world(tmp_path: Path, make_wgs_account) -> World:
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    steam = tmp_path / "NMS" / STEAM_ACCOUNT
    steam.mkdir(parents=True)
    (steam / "steam_autocloud.vdf").write_text('"steam_autocloud.vdf"\n{\n}\n', encoding="ascii")
    steam_save(steam, 4, 0, "Old Run", 900, OLD, T0 - 90_000, extra="old")
    steam_save(steam, 9, 0, "Main Save", 7200, MAIN, T0, extra="main")
    steam_save(steam, 9, 1, "Main Save", 7100, MAIN, T0 - 60, extra="main-rp")
    steam_save(steam, 5, 0, "Cross Save", 10_800, CROSS, T0 - 300, extra="cross")
    operations.create_full_backup(vault, steam, warnings=[])     # all three in the vault
    _clear(steam, 4)                                               # Old Run: vault only now
    steam_save(steam, 3, 0, "Other Run", 600, OTHER, T0 - 500, extra="other")   # not backed up
    xbox = make_wgs_account(tmp_path / "xbox", [
        ("Slot1Auto", '{"x":"cross-x"}', "Cross Save", "", 3600, T0 - 4000, CROSS),
        ("Slot2Auto", '{"x":"xonly"}', "Xbox Only", "", 1200, T0 - 7000, XONLY),
    ])
    sources = [
        Source(id="steam", platform="steam", account=STEAM_ACCOUNT, path=str(steam),
               label="Steam"),
        Source(id="xbox", platform="xbox", account=xbox.name, path=str(xbox), label="Xbox"),
    ]
    org = Organizer(vault.root, sources, cache=ScanCache(None, None),
                    game_running=lambda: False, clock=lambda: T0 + 3600)
    org.refresh(record=True)
    # The game saves Main Save again: the journal sees it in the live slot, so the two
    # versions are linked ("seen in the live slot") and the new one is not in the vault.
    steam_save(steam, 9, 0, "Main Save", 7800, MAIN, T0 + 600, extra="main-2")
    org.clock = lambda: T0 + 7200
    org.refresh(record=True)
    org.notes.set_save_note(PIDS["main"], "Main run.\nNext: the Atlas path.", base_rev=None)
    org.notes.set_pinned(PIDS["main"], True)
    model = org.refresh(record=False)
    return World(vault, steam, xbox, sources, org, model)


def _clear(folder: Path, slot: int) -> None:
    from nms_save_vault.core import slotmap

    for fno in slotmap.slot_file_numbers(slot):
        for name in (slotmap.data_filename(fno), slotmap.meta_filename(fno)):
            (folder / name).unlink(missing_ok=True)
