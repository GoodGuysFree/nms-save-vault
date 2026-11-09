"""The core's public API: everything another app (NMS Savecraft) may use of the core.

An app that bundles the core imports this module only, never the modules behind it; those
can change in any release. What is here is versioned:

- ``CORE_VERSION`` is the release the core came from (the Vault release tag, without ``v``).
- ``API_VERSION`` goes up when a name here is removed or changes meaning, including a
  function's parameters or a type's fields. Adding keeps it. ``tests/test_core_api.py`` pins
  all of that, so neither happens by accident.

It reads where the saves and the vault are, the saves on disk and what is in them (every
inventory slot by slot: :func:`inventories_of`; every asset with the key that tells it apart
across versions and saves: :func:`assets_of`), the
vault's stored versions, and which versions an app made (the last version before it changed
a slot). It writes one way only: an edit of a live Steam save, and its undo, through a
:class:`LiveSession` - the same preserve, journal, check, rollback and undo as every write NMS
Save Vault makes, under the same lock, so the two apps never write at once.

Saves are read by readable field names (``get(root, "PlayerStateData", "Units")``); the
obfuscated keys stay inside the core. :func:`get` refuses a path through an ownership field
(platform ids and user names: ``Owner``, ``UID``, ...) with :class:`PrivacyError`. A subtree it
returns, and ``DecodedSave.root``, can still hold them: an app must never show or keep a raw
subtree.
"""
from __future__ import annotations

import logging
import os
import typing
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from . import discover, gameversion, locations, safety, savedir, state
from . import platform as hostmod
from .appinfo import CORE_VERSION, VAULT, AppIdentity, app_of, current, is_stamp, set_app
from .index.extract import CtxInput, SaveInput, contexts_of
from .index.assets import INDEX_VERSION
from .index.assets import KINDS as ASSET_KINDS
from .index.assets import Asset, assets_of, state_of
from .index.inventory import Inventory, Slot, SpecialSlot, inventories_of
from .index.nodes import PrivacyError, get, items
from .liveops import context_for, execute, pending_recovery, plan_undo, resolve_recovery
from .liveops.classic import _ad_hoc
from .liveops.context import OpContext
from .liveops.edit import plan_edit
from .liveops.errors import ConfirmationRequired, LiveOpError, TargetError
from .liveops.targets import resolve_target
from .liveops.model import OpOutcome, PlacePlan, Severity
from .catalog import Vault
from .liveops.opjournal import OPS, machine_id, read_ops
from .organizer import Organizer, provenance
from .organizer import store as orgstore
from .store import versionstore
from .store.damage import DAMAGE_JSON
from .organizer.journal import Journal
from .organizer.model import LiveTarget, ModelError, pid_from_id
from .organizer.provenance import AppChange, Before, KeptCopy
from .organizer.store import OrganizerStore
from .saveedit import EditRefused
from .savedir import MemberView, SaveDirView, SlotView
from .savejson import DecodedSave, SaveDecodeError, decode_save
from .savepatch import AddMember, Insert, PatchRefused, Remove, Replace, dumps, dumps_named
from .store import (
    FileEntry,
    StoreEntry,
    StoreError,
    StoreNotUpgraded,
    StoreTooNew,
    StoreUnavailable,
    VersionStore,
    vault_lock,
)

API_VERSION = 1

PLATFORMS = ("steam", "xbox")

__all__ = [
    "API_VERSION", "CORE_VERSION", "PLATFORMS",
    # who is writing (every vault record names its app)
    "VAULT", "AppIdentity", "app_of", "current", "is_stamp", "set_app",
    # where things are, whether the game runs, and whether another app changed the vault
    "create_vault", "default_vault_dir", "game_running", "live_save_dirs", "vault_dir",
    "vault_stamp",
    # the saves on disk
    "MemberView", "SaveDirView", "SlotView", "platform_of", "scan_save_dir",
    # what is in a save
    "CtxInput", "DecodedSave", "SaveDecodeError", "SaveInput", "contexts_of", "decode_save",
    "PrivacyError", "get", "items", "known_game_version", "playthrough_of", "read_member",
    "Inventory", "Slot", "SpecialSlot", "inventories_of",
    "ASSET_KINDS", "Asset", "INDEX_VERSION", "assets_of", "state_of",
    # the vault's stored versions
    "FileEntry", "StoreEntry", "StoreError", "StoreNotUpgraded", "StoreTooNew",
    "StoreUnavailable", "VaultReader", "open_vault",
    # which versions an app made, and the last one before its change
    "AppChange", "Before", "KeptCopy", "LiveTarget", "VaultHistory", "open_history",
    "slot_of",
    # an edit of a live save, and its undo
    "AddMember", "Insert", "Remove", "Replace", "dumps", "dumps_named",
    "ConfirmationRequired", "EditRefused", "LiveOpError", "PatchRefused", "TargetError",
    "Interrupted", "LiveSession", "Notice", "SEVERITIES", "WRITE_STATUSES", "WritePlan",
    "WriteResult", "open_live",
]

log = logging.getLogger(__name__)


def vault_dir() -> Path:
    """The vault to share: the folder chosen in NMS Save Vault when its settings name one -
    even when it cannot be found now (a drive not connected: then nothing is written until it
    is back, never into another vault) - else :func:`default_vault_dir`."""
    return state.chosen_vault() or default_vault_dir()


def default_vault_dir() -> Path:
    """Where NMS Save Vault puts a new vault; an app that starts the vault for a user without
    NMS Save Vault puts it here, so a later install finds it."""
    return locations.default_vault_dir()


def create_vault(vault_root: str | Path | None = None) -> Path:
    """Start the vault for a user without NMS Save Vault (CL9): at ``vault_root`` (default
    :func:`vault_dir`, which is :func:`default_vault_dir` until NMS Save Vault chooses one),
    laid out as NMS Save Vault lays out a vault it creates, so a later install of it takes the
    vault as its own. A vault already there is left as it is. Returns the vault's folder.
    Raises :class:`StoreNotUpgraded` for a vault of NMS Save Vault's older layout (only NMS
    Save Vault upgrades one, when its user asks) and :class:`StoreUnavailable` when the folder
    cannot be made."""
    root = Path(vault_root) if vault_root is not None else vault_dir()
    chosen = state.chosen_vault()
    if chosen is not None and not chosen.is_dir():
        raise StoreUnavailable("The vault chosen in NMS Save Vault cannot be found: connect "
                               "the drive it is on. No other vault is started meanwhile.")
    if versionstore.vault_mode(root) != "legacy":
        return root                       # in store mode already (or being upgraded by Vault)
    existed = root.exists()
    if existed and not root.is_dir():
        raise StoreUnavailable("The vault folder cannot be made here: a file has its name.")
    try:
        empty = not existed or not any(root.iterdir())
    except OSError as exc:
        raise StoreUnavailable(f"The vault folder cannot be read "
                               f"({exc.strerror or type(exc).__name__}).") from exc
    if not empty:
        # NMS Save Vault's rule: only a missing or completely empty folder becomes a new vault
        raise StoreNotUpgraded("That folder is not empty and holds no vault Savecraft can use. "
                               "Open it in NMS Save Vault, or choose an empty folder; nothing "
                               "was changed.")
    store = versionstore.store_root(root)
    made = [p for p in (store, orgstore.store_root(root)) if not p.exists()]
    try:
        root.mkdir(parents=True, exist_ok=True)
        started = versionstore.start_new_vault(root)
    except OSError as exc:
        _undo_new_vault(root, existed, made)        # only what this call made
        raise StoreUnavailable(f"The vault folder cannot be made here "
                               f"({exc.strerror or type(exc).__name__}).") from exc
    if versionstore.vault_mode(root) == "store":
        return root                                 # this call's, or another app's just now
    if not started:
        # another app is starting this vault (it holds the lock or got there first): what it
        # made is never removed
        raise StoreUnavailable("The vault is being started by another app just now; try again "
                               "in a moment.")
    raise StoreUnavailable("The vault could not be started here.")


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _undo_new_vault(root: Path, existed: bool, made: list[Path]) -> None:
    """A vault this call was starting failed part-way: remove the folders it made (the store,
    the lock's folder) and the vault folder when this call made it and it is empty again, so a
    retry finds what it found. Nothing another app made is touched."""
    import shutil
    try:
        for path in made:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        if not existed and root.is_dir() and not any(root.iterdir()):
            root.rmdir()
    except OSError:
        log.warning("a vault that failed to start could not be cleared away")


def live_save_dirs() -> list[Path]:
    """Every live save folder on this machine: Steam and GOG account folders, then Microsoft
    Store / Game Pass ones."""
    return [*locations.find_live_save_dirs(), *locations.find_microsoft_save_dirs()]


def game_running() -> bool | None:
    """Whether No Man's Sky runs on this machine now: True, False, or None when it cannot be
    told. While it is not False, an app shows the saves but changes nothing (a write is
    refused then anyway)."""
    return safety.is_game_running()


def vault_stamp(vault_root: str | Path | None = None) -> tuple[int, ...]:
    """A token that changes whenever anything is recorded in the vault at ``vault_root``
    (default :func:`vault_dir`) by any app: a version kept or removed, an operation and its
    checks, the history, notes, damage found, the format. A few file stats, no reading: poll
    it every few seconds and when the window gets focus, and refresh when it differs.
    Compare tokens with ``==`` only; what is in one can change in any release."""
    root = Path(vault_root) if vault_root is not None else vault_dir()
    organizer = root / orgstore.DIRNAME
    store = versionstore.store_root(root)
    catalog = Vault(root, read_only=True)
    paths = (store / versionstore.FORMAT_NAME, store / versionstore.ENTRIES,
             store / versionstore.STATE / DAMAGE_JSON,
             organizer / orgstore.HEADER, organizer / orgstore.JOURNAL, organizer / orgstore.NOTES,
             organizer / orgstore.LINEAGE, organizer / OPS, catalog.catalog_path,
             catalog.oplog_path)
    out: list[int] = []
    for path in paths:
        try:
            st = os.stat(safety.long_path(path))
            out += (st.st_size, st.st_mtime_ns, st.st_ino)
        except OSError:
            out += (-1, -1, -1)
    return tuple(out)


def platform_of(folder: str | Path) -> str:
    """``"steam"`` or ``"xbox"`` (:data:`PLATFORMS`): how the saves in ``folder`` are laid
    out."""
    return savedir.platform_of(folder)


def scan_save_dir(folder: str | Path) -> SaveDirView:
    """The slots and saves of a save folder of any platform. Only reads."""
    return savedir.scan_any(folder)


def read_member(member: MemberView) -> DecodedSave:
    """Decode one save (a slot's A or B member) from disk. ``root`` of the result holds the
    save's JSON; walk it with :func:`get` and :func:`contexts_of`. Raises ``OSError`` when
    the file cannot be read and :class:`SaveDecodeError` when it is damaged."""
    hint = member.info.size_decompressed if member.info is not None else None
    return decode_save(Path(member.data_path).read_bytes(), bare_size_hint=hint or None)


def known_game_version(member: MemberView, *, platform: str) -> bool:
    """The save's meta reads and its game version is one this core is tested with. Anything
    else - a newer game, an unreadable or invalid meta - may be shown but never written.
    ``platform`` is the folder's (:func:`platform_of`); another value raises ``ValueError``."""
    if platform not in PLATFORMS:
        raise ValueError(f"platform {platform!r} is not one of {PLATFORMS}")
    if not member.valid:
        return False
    return not gameversion.newer(platform, member.info.meta_format, member.info.base_version)


def playthrough_of(member: MemberView) -> str | None:
    """The save's playthrough id as the vault's history keys it (16 hex digits), from its
    meta; None when the meta does not carry one."""
    if not member.valid or not member.info.slot_identifier:
        return None
    try:
        return pid_from_id(member.info.slot_identifier)
    except ModelError:
        return None


def slot_of(folder: str | Path, slot: int) -> LiveTarget:
    """How the vault's history names slot ``slot`` (1-15) of the live save folder ``folder``.
    Its ``account_key`` is the folder's name, which holds the platform account id: never show
    it."""
    return LiveTarget(platform_of(folder), Path(folder).name, slot)


class VaultReader:
    """The vault's stored versions, to read. Nothing in the vault is written through it, not
    even a probe or a temporary copy."""

    def __init__(self, store: VersionStore) -> None:
        self._store = store

    @property
    def root(self) -> Path:
        return self._store.vault_root

    def entries(self) -> list[StoreEntry]:
        """Every stored entry (a backup, snapshot, import ...), with its files and members."""
        return self._store.entries()

    def entry(self, entry_id: str) -> StoreEntry:
        """One entry by id; ``StoreError`` when there is none."""
        return self._store.entry(entry_id)

    def read_file(self, entry_id: str, path: str) -> bytes:
        """One file of an entry (``path`` as in ``StoreEntry.files``), checked against its
        hash."""
        return self._store.read_file(entry_id, path)


def open_vault(vault_root: str | Path | None = None, *,
               live_roots: Sequence[Path] | None = None) -> VaultReader:
    """Open the vault's stored versions to read. ``vault_root`` defaults to
    :func:`vault_dir`. Raises :class:`StoreUnavailable`, :class:`StoreNotUpgraded` or
    :class:`StoreTooNew` (all :class:`StoreError`)."""
    root = Path(vault_root) if vault_root is not None else vault_dir()
    return VaultReader(VersionStore.open(root, lock=vault_lock(root), live_roots=live_roots,
                                         read_only=True))


class VaultHistory:
    """What every app did to the live slots, as the vault's journal records it, to read.
    Apps are named as :func:`set_app` names them (``"nms-savecraft"``, ``"nms-save-vault"``).
    A snapshot: open it again to see later changes."""

    def __init__(self, state) -> None:
        self._state = state

    def changes_by(self, app: str) -> list[AppChange]:
        """Every operation ``app`` ran on a live slot that went through, oldest first."""
        return provenance.changes_by(self._state, app)

    def made_by(self, app: str) -> set[str]:
        """The versions (sha256 of a save's data) ``app``'s operations wrote new content into:
        an edit's output, never a copy."""
        return provenance.made_by(self._state, app)

    def touched_by(self, app: str) -> set[str]:
        """The versions ``app`` made and every version the game saved from one of them."""
        return provenance.touched_by(self._state, app)

    def last_before(self, app: str, *, slot: LiveTarget | None = None,
                    playthrough: str | None = None) -> Before | None:
        """The newest pair of a playthrough before ``app`` changed it, that ``app`` had not
        touched, and where the vault kept it: of ``playthrough`` (:func:`playthrough_of`); of
        the playthrough ``slot`` holds now (:func:`slot_of`), else what that slot held before
        ``app`` wrote it; or the newest of all. None when there is none."""
        return provenance.last_before(self._state, app, target=slot, pid=playthrough)


def open_history(vault_root: str | Path | None = None) -> VaultHistory:
    """Read the vault's journal. Nothing is written. ``vault_root`` defaults to
    :func:`vault_dir`; a vault without a journal has an empty history."""
    root = Path(vault_root) if vault_root is not None else vault_dir()
    if not root.is_dir():
        raise StoreUnavailable("The vault is not reachable.")
    return VaultHistory(Journal(OrganizerStore(root, create=False, read_only=True), {}).replay())


# --- an edit of a live save, and its undo ---------------------------------------------------

#: How much a :class:`Notice` matters: ``"confirm"``, the user must agree first; ``"block"``,
#: the write cannot be made.
SEVERITIES: tuple[str, ...] = typing.get_args(Severity)

#: What a write can end as (:attr:`WriteResult.status`).
WRITE_STATUSES: tuple[str, ...] = typing.get_args(typing.get_type_hints(OpOutcome)["status"])


@dataclass(frozen=True)
class Notice:
    """One thing a plan or a write tells the user: a stable ``code``, how much it matters
    (one of :data:`SEVERITIES`) and one plain sentence to show."""

    code: str
    severity: str
    text: str


def _notices(warnings) -> tuple[Notice, ...]:
    return tuple(Notice(w.code, w.severity, w.text) for w in warnings)


#: What NMS Save Vault asks its user about but an app's write is refused for: an app writes
#: only while the game is known to be closed (Savecraft S11).
_APP_BLOCKS = frozenset({"GAME_STATE_UNKNOWN"})


class WritePlan:
    """A write planned and not yet made. Show :attr:`outcome` and :attr:`warnings`, ask the
    user about each of :attr:`confirmations`, then hand the plan back to
    :meth:`LiveSession.write` as it is."""

    def __init__(self, plan: PlacePlan) -> None:
        self._plan = plan

    @property
    def outcome(self) -> str:
        """What the write does, in one line."""
        return self._plan.outcome

    @property
    def warnings(self) -> tuple[Notice, ...]:
        return _notices(w for w in self._plan.warnings if w.code not in _APP_BLOCKS)

    @property
    def confirmations(self) -> frozenset[str]:
        """The codes of the warnings the user must agree to; pass those they agreed to as
        ``confirmed``."""
        return frozenset(self._plan.confirmations) - _APP_BLOCKS

    @property
    def blocked(self) -> tuple[Notice, ...]:
        """Why the write cannot be made; empty when it can. Whether the game runs must be
        known: when it cannot be told, the write is blocked."""
        return _notices((*self._plan.blocked,
                         *(w for w in self._plan.warnings if w.code in _APP_BLOCKS)))

    @property
    def made(self) -> tuple[str, ...]:
        """The versions with new content the write puts in the slot (an edit's output)."""
        return self._plan.made

    @property
    def undoes(self) -> str | None:
        """The operation this plan undoes, or None."""
        return self._plan.undo_of


@dataclass(frozen=True)
class WriteResult:
    """What a write did; ``status`` is one of :data:`WRITE_STATUSES`: ``"done"``;
    ``"refused"`` (nothing was written: ``code`` says why, e.g. ``GAME_RUNNING`` or ``BUSY``);
    ``"stale"`` (the slot changed since the plan, the game or another app saved: nothing was
    written, plan the edit again from the slot as it is now); ``"rolled_back"`` (the write
    failed and the slot is as it was); ``"failed"``. ``op_id`` names the operation, to undo
    it (:meth:`LiveSession.plan_undo`)."""

    op_id: str | None
    status: str
    text: str
    code: str | None
    warnings: tuple[Notice, ...]
    undo: str | None              # what its undo would do, when it can be undone


@dataclass(frozen=True)
class Interrupted:
    """A write that stopped part-way (the app or the machine went down): until it is
    finished or put back, no write starts on that save folder. ``recommended`` is one of
    ``choices`` (``"forward"``: finish it, ``"back"``: put the slot back, ``"leave"``), or
    ``"none"`` when there is nothing to choose here (``text`` says why)."""

    op_id: str
    app: str                      # the app that made it (:func:`set_app`'s name)
    text: str
    recommended: str
    choices: tuple[str, ...]


def _app_of_op(rec) -> str:
    """The app that started an operation, as its ``preparing`` event names it; ``unknown``
    when the journal does not say (never taken for this app)."""
    preparing = next((e for e in rec.events if e.get("event") == "preparing"), None) \
        if rec is not None else None
    return app_of(preparing.get("app") if preparing is not None else None)


def _result(out: OpOutcome) -> WriteResult:
    return WriteResult(out.op_id, out.status, out.outcome, out.code, _notices(out.warnings),
                       out.undo.label if out.undo is not None else None)


class LiveSession:
    """Writes to the live saves, made as NMS Save Vault makes them: what the slot held is kept
    in the vault first, the write is journaled and checked, rolled back when it fails, and
    can be undone. One write at a time across every app sharing the vault; never while the
    game runs. Every record names the app :func:`set_app` named. Made by :func:`open_live`."""

    def __init__(self, make: Callable[[], OpContext]) -> None:
        self._make = make
        self._ctx: OpContext | None = make()

    def refresh(self) -> None:
        """See the saves, the vault and NMS Save Vault's settings as they are now.
        :meth:`write` does it after every write; call it when the game or another app may
        have written since."""
        self._ctx = None
        self._ctx = self._make()

    def _now(self) -> OpContext:
        if self._ctx is None:                   # a refresh after a write did not go through
            self.refresh()
        return self._ctx

    def plan_edit(self, slot: LiveTarget, edits: Sequence[Replace | Remove | Insert | AddMember],
                  *, what: str) -> WritePlan:
        """The plan to put the save ``slot`` (:func:`slot_of`) loads back with ``edits`` made,
        each proven: the edited save reads back as the original with exactly those changes.
        Every field name in an edit is a readable one. ``what`` says what the edit does, for
        the vault's label ("rename ship 'Ark'"). The edited version is kept in the vault before
        this returns. Raises :class:`EditRefused` (a save this core does not edit: an Xbox
        slot, a newer game, an old container; or one that cannot be written now: the game
        running, an interrupted write waiting), :class:`PatchRefused` (an edit that cannot
        be made exactly), :class:`TargetError` (a slot no write may use) or a
        :class:`StoreError` (the vault busy with another app's write, or unreachable)."""
        return WritePlan(plan_edit(self._now(), slot, list(edits), what=what))

    def folder_of(self, slot: LiveTarget) -> Path | None:
        """The live save folder this session writes for ``slot``; None when it writes none.
        Two folders can share a name (a Steam folder NMS Save Vault lists and another found
        on this machine), and a slot is named by its folder's name: before it writes, an app
        checks that this is the folder it read the save from."""
        try:
            return Path(resolve_target(self._now(), slot).folder)
        except TargetError:
            return None

    def plan_undo(self, op_id: str) -> WritePlan:
        """The plan that puts back what this app's operation ``op_id`` replaced; a plan that
        says why (:attr:`WritePlan.blocked`) when it cannot be undone (an unknown ``op_id``
        too). Raises :class:`EditRefused` for an operation another app made."""
        ctx = self._now()
        rec = read_ops(ctx).get(op_id)
        if rec is not None and _app_of_op(rec) != current().name:
            raise EditRefused("that operation was not made by this app; undo it in the app "
                              "that made it")
        return WritePlan(plan_undo(ctx, op_id))

    def write(self, plan: WritePlan, confirmed: Iterable[str] = ()) -> WriteResult:
        """Make ``plan``'s write. Raises :class:`ConfirmationRequired` when ``confirmed`` lacks
        a code of :attr:`WritePlan.confirmations`, and :class:`TargetError` for a slot no write
        may use; every other result is a :class:`WriteResult`. NMS Save Vault's settings are
        read again first, as for every write it makes itself."""
        self.refresh()
        held = [w for w in plan._plan.warnings if w.code in _APP_BLOCKS]
        if held:            # an app writes only with the game known to be closed
            return WriteResult("", "refused", held[0].text, held[0].code, (), None)
        out = execute(self._ctx, plan._plan, frozenset(confirmed) - _APP_BLOCKS)
        self._after_write()
        return _result(out)

    def interrupted(self) -> list[Interrupted]:
        """Writes that stopped part-way and wait for a choice (those that can be finished
        without one are finished here). Another machine's are listed too, with no choices:
        they are resolved on that machine. Empty while a write runs."""
        ctx = self._now()
        ops = read_ops(ctx)
        out = []
        for item in pending_recovery(ctx):
            out.append(Interrupted(item.op_id, _app_of_op(ops.get(item.op_id)), item.text,
                                   item.recommended,
                                   tuple(item.choices)))
        self._after_write()
        return out

    def resolve(self, op_id: str, choice: str, confirmed: Iterable[str] = ()) -> WriteResult:
        """Finish (``"forward"``), put back (``"back"``) or leave (``"leave"``) an
        :class:`Interrupted` write, as the user chose: one of its ``choices``, else
        ``ValueError``."""
        ctx = self._now()
        offered = next((i.choices for i in pending_recovery(ctx) if i.op_id == op_id), ())
        if choice not in offered:
            raise ValueError(f"{choice!r} is not offered for that write")
        # never agreed for the user: an app writes only with the game known to be closed
        out = resolve_recovery(ctx, op_id, choice, frozenset(confirmed) - _APP_BLOCKS)
        self._after_write()
        return _result(out)

    def _after_write(self) -> None:
        try:
            self.refresh()
        except Exception:  # noqa: BLE001 - the write's result stands; the next call retries
            log.warning("the live session could not be refreshed after a write", exc_info=True)
            self._ctx = None


def _key(path: str | Path) -> str:
    """One spelling per folder: links, junctions, short names and substituted drives
    resolved, case folded."""
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.abspath(path))


def _sources(live_dirs: Sequence[Path] | None, host: hostmod.Host) -> list[state.Source]:
    """The live folders a session uses. With NMS Save Vault's settings: its live folders as
    it lists them (so both apps name every slot alike), then the extra folders it lacks
    (``live_dirs``, else the ones found on this machine). A folder any of its settings files
    switches off, marks read-only or keeps as a backup source is only read."""
    settings = state.vault_settings(host)
    configured = [s for st in settings for s in st.sources]
    off = {_key(s.path) for s in configured
           if not s.enabled or not s.writable or s.role != state.ROLE_LIVE}
    base = list(settings[0].live_sources) if settings else []
    extra = ([_ad_hoc(Path(d)) for d in live_dirs] if live_dirs is not None
             else list(discover.discover_live_sources()))
    known = {_key(s.path) for s in base}
    vaults_names = {(s.platform == "xbox", Path(s.path).name.lower()) for s in base}
    out = list(base)
    for s in extra:
        if _key(s.path) in known:
            continue
        if (s.platform == "xbox", Path(s.path).name.lower()) in vaults_names:
            # a second folder of one name would rename the slots of NMS Save Vault's folder
            if live_dirs is None:
                continue                # found on this machine: NMS Save Vault does not use it
            raise ValueError(f"the save folder {Path(s.path).name} cannot be used: NMS Save "
                             "Vault uses another folder of that name")
        known.add(_key(s.path))
        out.append(s)
    return [replace(s, writable=False) if _key(s.path) in off else s for s in out]


def open_live(vault_root: str | Path | None = None, *,
              live_dirs: Sequence[str | Path] | None = None) -> LiveSession:
    """A session that writes to the live saves through the vault at ``vault_root`` (default
    :func:`vault_dir`). ``live_dirs``: the save folders to use besides those NMS Save Vault's
    settings list; default every one found on this machine. Call :func:`set_app` first: a
    write is refused to an app that has not named itself. Raises :class:`StoreUnavailable`,
    :class:`StoreNotUpgraded` or :class:`StoreTooNew` when there is no vault there to write
    through, and ``ValueError`` when NMS Save Vault's settings cannot be read (what they
    switch off is then not known) or a folder of ``live_dirs`` has the name of another folder
    they list. The settings are read again at every refresh."""
    if current().name == VAULT:
        raise ValueError("name the app with set_app() before writing")
    root = Path(vault_root) if vault_root is not None else vault_dir()
    VersionStore.open(root, lock=vault_lock(root), live_roots=(), read_only=True)
    host = hostmod.Host.current()
    dirs = None if live_dirs is None else [Path(d) for d in live_dirs]
    mid = machine_id(host)

    def make() -> OpContext:
        organizer = Organizer(root, _sources(dirs, host), host=host,
                              game_running=safety.is_game_running)
        return context_for(organizer, host=host, machine_id=mid,
                           game_probe=safety.is_game_running)

    return LiveSession(make)
