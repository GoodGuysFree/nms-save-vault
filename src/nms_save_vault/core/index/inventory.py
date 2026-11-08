"""Every inventory of a save, slot by slot (CL7): where it is, its shape and what it holds.

Item ids only, without their leading ``^`` as the index keeps them; names, icons and limits
are game content that the reader does not ship. ``where`` is the readable path of an
inventory from the save's root, so an edit (``savepatch``) can name it, and a slot's
``number`` is its place in that inventory's ``Slots``.

Read as the game keeps them (live Steam saves, 2026-11-08): the exosuit, freighter and
storage inventories are fields of ``PlayerStateData``; each filled ship of ``ShipOwnership``
has three, each filled multi-tool its ``Store``, each exocraft two. ``WeaponInventory`` is a
copy of the active multi-tool's ``Store`` (equal in every context checked), so it is listed
only in a save without a filled multi-tool (one from before multi-tool collections, where it
is the tool's own); ``ShipInventory`` is an old field. An item is never counted twice.
"""
from __future__ import annotations

from dataclasses import dataclass

from .extract import contexts_of
from .extract.inventories import EXOSUIT, FREIGHTER, STORAGE, VEHICLE
from .nodes import get, gid, integer, items, label

#: Inventories that hold what the player lost: the grave left at a death.
GRAVE = ("GraveInventory",)


@dataclass(frozen=True)
class Slot:
    number: int                         # its place in the inventory's ``Slots``
    item: str                           # the item id, without ``^``
    kind: str | None                    # Substance, Product or Technology, as the save says
    amount: int | float | None
    max_amount: int | float | None
    x: int | None
    y: int | None
    damage: float | None                # 0.0 when whole
    installed: bool | None              # a technology fully installed


@dataclass(frozen=True)
class SpecialSlot:
    x: int | None
    y: int | None
    kind: str | None                    # TechBonus (supercharged), BlockedByBrokenTech, ...


@dataclass(frozen=True)
class Inventory:
    context: str                        # "base" or "expedition"
    owner: str                          # exosuit, ship, multitool, exocraft, freighter, storage, grave
    index: int | None                   # the ship's, multi-tool's or exocraft's place in its list
    field: str                          # the save's name for it: Inventory_Cargo, Chest3Inventory, ...
    where: tuple[str | int, ...]        # its readable path from the save's root
    width: int | None
    height: int | None
    valid: tuple[tuple[int, int], ...]  # the slots it has (x, y)
    special: tuple[SpecialSlot, ...]
    inventory_class: str | None         # C, B, A, S
    stack_group: str | None
    slots: tuple[Slot, ...]


def inventories_of(root: dict) -> list[Inventory]:
    """Every inventory of the decoded save ``root`` (``DecodedSave.root``), context by context,
    in a fixed order: exosuit, ships, multi-tools, exocraft, freighter, storage, grave."""
    _save, contexts = contexts_of(root)
    out: list[Inventory] = []
    for name, ctx in contexts.items():
        base = ("PlayerStateData",) if ctx.container is None else (
            ("BaseContext" if name == "base" else "ExpeditionContext"), "PlayerStateData")
        psd = ctx.psd

        def add(owner: str, index: int | None, node: object, field: str,
                where: tuple[str | int, ...]) -> None:
            if isinstance(node, dict):
                out.append(_inventory(name, owner, index, field, base + where, node))

        for field in EXOSUIT:
            add("exosuit", None, get(psd, field), field, (field,))
        for ship in ctx.ships:
            for field in EXOSUIT:
                add("ship", ship.index, get(ship.node, field), field,
                    ("ShipOwnership", ship.index, field))
        for tool in ctx.multitools:
            add("multitool", tool.index, get(tool.node, "Store"), "Store",
                ("Multitools", tool.index, "Store"))
        if not ctx.multitools:          # a save from before multi-tool collections: its one tool
            add("multitool", None, get(psd, "WeaponInventory"), "WeaponInventory",
                ("WeaponInventory",))
        for index, vehicle in enumerate(items(psd, "VehicleOwnership")):
            for field in VEHICLE:
                add("exocraft", index, get(vehicle, field), field,
                    ("VehicleOwnership", index, field))
        for field in FREIGHTER:
            add("freighter", None, get(psd, field), field, (field,))
        for field in STORAGE:
            add("storage", None, get(psd, field), field, (field,))
        for field in GRAVE:
            add("grave", None, get(psd, field), field, (field,))
    return out


def _inventory(context: str, owner: str, index: int | None, field: str,
               where: tuple[str | int, ...], node: dict) -> Inventory:
    return Inventory(
        context, owner, index, field, where,
        integer(get(node, "Width")), integer(get(node, "Height")),
        tuple((x, y) for x, y in (_xy(v) for v in items(node, "ValidSlotIndices"))
              if x is not None and y is not None),
        tuple(SpecialSlot(*_xy(get(s, "Index")), label(get(s, "Type", "InventorySpecialSlotType")))
              for s in items(node, "SpecialSlots")),
        label(get(node, "Class", "InventoryClass")),
        label(get(node, "StackSizeGroup", "InventoryStackSizeGroup")),
        tuple(_slot(n, s) for n, s in enumerate(items(node, "Slots"))))


def _slot(number: int, slot: object) -> Slot:
    damage, installed = get(slot, "DamageFactor"), get(slot, "FullyInstalled")
    return Slot(number, gid(get(slot, "Id")), label(get(slot, "Type", "InventoryType")),
                _num(get(slot, "Amount")), _num(get(slot, "MaxAmount")),
                *_xy(get(slot, "Index")),
                float(damage) if isinstance(damage, (int, float)) and not isinstance(damage, bool)
                else None,
                installed if isinstance(installed, bool) else None)


def _xy(node: object) -> tuple[int | None, int | None]:
    return integer(get(node, "X")), integer(get(node, "Y"))


def _num(v: object) -> int | float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
