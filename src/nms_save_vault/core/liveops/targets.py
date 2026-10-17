"""Target resolution (D5 2.3.6, C29): the only way a live write obtains a folder.

A target names a platform, an account folder and a slot. It resolves only to a folder the
owner configured as a writable live source, outside the vault, of the right platform; the
folder never leaves core (front ends get labels only).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import aliases, formats, msstore, savedir
from ..organizer.model import LiveRef, LiveTarget
from ..organizer.scanner import instance_of
from ..state import ROLE_LIVE, Source
from .context import OpContext
from .errors import TargetError
from .model import CloudMode, ResolvedTarget

#: What an empty Steam account folder must be called to count as one (no saves to tell by).
_STEAM_ACCOUNT_NAME = re.compile(r"st_[0-9]+|DefaultUser")
_PLATFORM_NAMES = {"steam": "Steam", "xbox": "Xbox"}


@dataclass(frozen=True)
class ResolvedAccount:
    """A live account folder resolved for writing. Internal, like ResolvedTarget."""

    account: LiveRef
    source_id: str
    folder: Path
    cloud_mode: CloudMode
    xgs_twin: Path | None
    label: str           # the account's own name when its platform has several, else ""


def resolve_target(ctx: OpContext, target: LiveTarget) -> ResolvedTarget:
    """The folder and facts of one live slot; raises :class:`TargetError`."""
    slot = target.slot
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= formats.MAX_SAVE_SLOTS:
        raise TargetError("BAD_TARGET", f"slot {slot!r} is not 1..{formats.MAX_SAVE_SLOTS}")
    acct = resolve_account(ctx, target.ref)
    label = slot_label(target.platform, slot, acct.label)
    return ResolvedTarget(target, acct.source_id, acct.folder, acct.cloud_mode, acct.xgs_twin,
                          label)


def resolve_account(ctx: OpContext, account: LiveRef) -> ResolvedAccount:
    """The folder of one live account; raises :class:`TargetError`."""
    if account.platform not in _PLATFORM_NAMES:
        raise TargetError("BAD_TARGET", f"no platform {account.platform!r}")
    source = _the_source(ctx.sources, account)
    folder = Path(source.path)
    if not folder.is_dir():
        raise TargetError("FOLDER_MISSING", "the live folder does not exist")
    _outside_vault(folder, ctx.vault.root)
    if account.platform == "xbox":
        _check_xbox(folder)
        cloud: CloudMode = "xbox"
        twin = msstore.xgs_twin(folder)
    else:
        _check_steam(folder)
        cloud = "steam-cloud" if (folder / formats.STEAM_AUTOCLOUD).is_file() else "steam-local"
        twin = None
    if not source.writable:
        raise TargetError("NOT_WRITABLE", "the live source is set read-only")
    return ResolvedAccount(account, source.id, folder, cloud, twin,
                           _account_label(ctx.sources, source, account))


def slot_label(platform: str, slot: int, account_label: str = "") -> str:
    """``Steam slot 7``, or ``Steam slot 7 (<account>)`` when the platform has several."""
    text = f"{_PLATFORM_NAMES[platform]} slot {slot}"
    return f"{text} ({account_label})" if account_label else text


def _live(sources, platform: str) -> list[Source]:
    return [s for s in sources
            if s.role == ROLE_LIVE and s.enabled and _platform(s) == platform]


def _platform(source: Source) -> str:
    return "xbox" if source.platform == "xbox" else "steam"


def _the_source(sources, account: LiveRef) -> Source:
    found = [s for s in _live(sources, account.platform)
             if Path(s.path).name == account.account_key
             and (not account.instance or instance_of(s.path) == account.instance)]
    if not found:
        raise TargetError("UNKNOWN_ACCOUNT", "no enabled live source holds that account")
    if len(found) > 1:
        raise TargetError("AMBIGUOUS", "two live folders share that account name")
    return found[0]


def _outside_vault(folder: Path, vault_root: Path) -> None:
    here, vault = folder.resolve(), Path(vault_root).resolve()
    if here == vault or vault in here.parents:
        raise TargetError("INSIDE_VAULT", "the folder is the vault or inside it")


def _check_xbox(folder: Path) -> None:
    index = folder / "containers.index"
    if not index.is_file():
        raise TargetError("XGS_READ_ONLY", "an Xbox folder without containers.index")
    try:
        msstore.split_index(index.read_bytes())
    except (OSError, ValueError) as exc:
        raise TargetError("BAD_INDEX", f"containers.index does not parse ({exc})") from None
    if folder.parent.name.lower() != "wgs":
        raise TargetError("NOT_A_LIVE_FOLDER", "an Xbox account folder lives under wgs")


def _check_steam(folder: Path) -> None:
    if savedir.platform_of(folder) != "steam":
        raise TargetError("NOT_A_LIVE_FOLDER", "not a Steam save folder")
    if not savedir.looks_like_save_dir(folder) and not _STEAM_ACCOUNT_NAME.fullmatch(folder.name):
        raise TargetError("NOT_A_LIVE_FOLDER", "an empty folder not named like a Steam account")


def _account_label(sources, source: Source, account: LiveRef) -> str:
    """The account's name in the app, only needed when its platform has several. The
    owner's aliases apply; a label holding the account id falls back to "Steam 2"."""
    same = _live(sources, account.platform)
    if len(same) < 2:
        return ""
    label = str(aliases.redact(source.label)) if source.label else ""
    ids = {account.account_key, account.account_key.removeprefix("st_"), source.account} - {""}
    if not label or any(i in label for i in ids):
        label = f"{_PLATFORM_NAMES[account.platform]} {same.index(source) + 1}"
    return label
