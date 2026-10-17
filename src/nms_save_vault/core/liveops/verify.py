"""Post-launch verification (D5 2.12): a placed save can be undone outside the app (Steam
Cloud offers its copy at the next launch, Xbox sync or a console session does the same), so
every ``done`` op is looked at again per target until the evidence settles it.

Evidence, all read-only: D1's outcome of the op (it saw a child of the placed pair, the
evicted pair back, a later op, something unexpected), what the slot holds now, the Xbox
record sync state, and the game sessions this app has seen (``game-seen.json`` in the
per-machine cache of the vault, written whenever the game probe sees the game running).
A change of state is journaled as a ``verify`` event; nothing live is written.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from .. import aliases, msstore, safety
from ..organizer.cache import cache_dir
from ..organizer.model import LiveTarget, target_from_json
from ..organizer.store import StoreBusy as OrganizerBusy
from ..organizer.store import StoreReadOnly
from . import opjournal, steamcloud
from .context import OpContext
from .errors import TargetError
from .model import VerifyItem
from .slots import SlotNow, read_account
from .steamwrite import file_names
from .targets import resolve_account, resolve_target
from .undo import _labels, _local

log = logging.getLogger(__name__)

GAME_SEEN = "game-seen.json"
EXPIRE_SECONDS = 30 * 86400
FINAL = ("confirmed", "superseded", "expired", "replaced")      # nothing more to learn
ALERTS = ("reverted", "deleted")                                # shown until dismissed

TEXTS = {
    "pending": "Waiting for the next game launch to confirm.",
    "reverted": "{who} put the old save back in {label}. '{name}' is safe in the vault.",
    "deleted": "{label} is empty again. '{name}' is safe in the vault.",
}


# --- the game-seen recorder ------------------------------------------------------------------


def game_seen_path(ctx: OpContext) -> Path:
    return cache_dir(ctx.store.vault_root, ctx.store.vault_id, ctx.host) / GAME_SEEN


def note_game_state(ctx: OpContext, running: bool | None) -> None:
    """Keep the moment the game was last seen running; a probe that sees it not running (or
    cannot tell) changes nothing. Never raises: this is a by-product of looking."""
    if running is not True:
        return
    path = game_seen_path(ctx)
    try:
        data = json.dumps({"last_seen_running": opjournal.utc_text(ctx.clock())})
        safety.atomic_write_bytes(path, data.encode("ascii"))
    except OSError as exc:
        log.info("the game session was not recorded: %s", exc.strerror)


def last_seen_running(ctx: OpContext) -> float | None:
    """When (epoch seconds) this app last saw the game running; None when never or unknown."""
    try:
        raw = json.loads(game_seen_path(ctx).read_text(encoding="ascii"))
        return opjournal.parse_utc(raw["last_seen_running"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def recording_probe(probe: Callable[[], bool | None], ctx_of: Callable[[], OpContext]
                    ) -> Callable[[], bool | None]:
    """``probe`` that also notes every sighting of the running game (for ``context_for``)."""
    def watched() -> bool | None:
        running = probe()
        note_game_state(ctx_of(), running)
        return running
    return watched


# --- the states ------------------------------------------------------------------------------


def update_verification(ctx: OpContext, events: Iterable[object] = ()) -> list[VerifyItem]:
    """Called after every D1 observe (a refresh, the app start, the CLI ``scan``). For each
    (op, target) of this machine whose verify state is pending or an undismissed alert:
    derive the state from the evidence; on a change append a ``verify`` event. Returns the
    items the UI shows (alerts and pending badges). ``events`` (D1's journal events) is
    accepted for the caller's convenience; the evidence is read from the lineage itself."""
    note_game_state(ctx, ctx.game_probe())
    seen = last_seen_running(ctx)
    now_ts = ctx.clock()
    items: list[VerifyItem] = []
    for rec in list(opjournal.read_ops(ctx).values()):
        if rec.status != "done" or rec.machine != ctx.machine_id:
            continue
        done_at = _done_at(rec)
        info = _targets(rec)
        for target in rec.targets:
            last = _last_verify(rec, target)
            state = last["state"] if last else "pending"
            if (last and last.get("dismissed")) or state in FINAL:
                continue
            facts = info.get(target)
            if facts is None:
                continue
            detail = (last or {}).get("detail", "")
            if state == "pending":
                state, detail, observed = _derive(ctx, rec, target, facts, seen, done_at)
                if state == "pending" and done_at is not None and \
                        now_ts - done_at > EXPIRE_SECONDS:
                    state, detail = "expired", ""
                if state != "pending":
                    _journal(ctx, rec.op_id, target, state, detail, observed)
            if state == "pending" or state in ALERTS:
                items.append(_item(ctx, rec, target, facts, state, detail))
    return items


def dismiss_verification(ctx: OpContext, op_id: str, target: LiveTarget) -> None:
    """The owner dismissed an alert: kept in the history, shown no more."""
    rec = opjournal.read_ops(ctx).get(op_id)
    last = None if rec is None else _last_verify(rec, target)
    if last is None:
        return
    opjournal.append(ctx, op_id, "verify", {
        "target": opjournal.target_json(target), "state": last["state"],
        "detail": last.get("detail", ""), "dismissed": True})


def _derive(ctx: OpContext, rec: opjournal.OpRecord, target: LiveTarget, facts: dict,
            seen: float | None, done_at: float | None) -> tuple[str, str, dict]:
    """The state of a pending (op, target) from the evidence."""
    try:
        slot = read_account(ctx, resolve_account(ctx, target.ref)).slots[target.slot]
    except (TargetError, KeyError, OSError):
        return "pending", "", {}
    observed = {"vid_a": slot.pair[0], "vid_b": slot.pair[1]}
    outcome = ctx.lineage.op_outcome(rec.op_id, target)
    if outcome == "confirmed":
        return "confirmed", "played", observed
    if outcome == "superseded":
        return "superseded", "", observed
    if outcome == "reverted":
        return "reverted", "", observed
    if outcome == "unexpected":
        return ("deleted" if not slot.present else "replaced"), "", observed
    placed, evicted = facts["placed"], facts["evicted"]
    if slot.pair == placed:
        if _uploaded(slot, target, ctx):
            return "confirmed", "uploaded", observed
        if seen is not None and done_at is not None and seen > done_at:
            return "confirmed", "unchanged", observed
        return "pending", "", observed
    if not slot.present:
        return "deleted", "", observed
    if evicted != (None, None) and slot.pair == evicted:
        return "reverted", "", observed
    return "pending", "", observed         # D1 has not classified the change yet


def _uploaded(slot: SlotNow, target: LiveTarget, ctx: OpContext | None = None) -> bool:
    """Xbox: every record of the pair went from Modified/Created to Synced. Steam (when the
    Steam client's remotecache.vdf is found): every file of the pair is tracked, synced and
    byte-identical (SHA-1) to what Steam holds."""
    if target.platform == "xbox":
        return bool(slot.present) and             all(m.sync_state == msstore.BLOB_SYNC_SYNCED for m in slot.present)
    if target.platform == "steam" and ctx is not None and slot.present:
        try:
            folder = resolve_account(ctx, target.ref).folder
        except TargetError:
            return False
        names = [n for m in slot.present for n in file_names(target.slot, m.position)]
        manifest = steamcloud.account_manifest(ctx.host, target.ref.account_key)
        return steamcloud.upload_state(manifest, folder, names) == "uploaded"
    return False


# --- the items -------------------------------------------------------------------------------


def _item(ctx: OpContext, rec: opjournal.OpRecord, target: LiveTarget, facts: dict, state: str,
          detail: str) -> VerifyItem:
    label = _labels(rec)[rec.targets.index(target)]
    name = facts["name"] or "<unnamed>"
    steam_cloud = False
    try:
        rt = resolve_target(ctx, target)
        label, steam_cloud = rt.label, rt.cloud_mode == "steam-cloud"
    except TargetError:
        pass
    who = "Steam Cloud" if target.platform == "steam" else "Xbox sync"
    text = TEXTS[state].format(who=who, label=label, name=name)
    actions: tuple = ()
    if state == "reverted":
        actions = ("place_again", "show_steps", "dismiss") if steam_cloud else \
            ("place_again", "dismiss")
    elif state == "deleted":
        actions = ("place_again", "dismiss")
    return VerifyItem(rec.op_id, target, label, state, detail,  # type: ignore[arg-type]
                      str(aliases.redact(text)), actions, _local(rec.started))


def _targets(rec: opjournal.OpRecord) -> dict:
    """Per target: the placed pair, the evicted pair and the placed save's name, from the
    write-ahead record."""
    writing = rec.last("writing")
    out = {}
    for t in (writing or {}).get("targets", ()):
        try:
            target = target_from_json(t["target"])
            post = {p: v["vid"] for p, v in t["post"].items()}
            pre = {p: (d or {}).get("vid") for p, d in t["pre"].items()}
            out[target] = {"placed": (post.get("A"), post.get("B")),
                           "evicted": (pre.get("A"), pre.get("B")), "name": t.get("name", "")}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _last_verify(rec: opjournal.OpRecord, target: LiveTarget) -> dict | None:
    for e in reversed(rec.events):
        if e["event"] == "verify" and opjournal._target_or_none(e.get("target")) == target:
            return e
    return None


def _done_at(rec: opjournal.OpRecord) -> float | None:
    done = rec.last("done")
    return None if done is None else opjournal.parse_utc(done["utc"])


def _journal(ctx: OpContext, op_id: str, target: LiveTarget, state: str, detail: str,
             observed: dict) -> None:
    try:
        opjournal.append(ctx, op_id, "verify", {
            "target": opjournal.target_json(target), "state": state, "detail": detail,
            "observed": observed})
    except (OSError, OrganizerBusy, StoreReadOnly) as exc:
        log.info("the verification of %s was not journaled: %s", op_id, exc)
