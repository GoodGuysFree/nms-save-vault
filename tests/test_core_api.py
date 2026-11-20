"""core.api: the public API another app bundles (NMS Savecraft). Its names, the
parameters of its functions and the fields of the types it hands out are pinned: removing one
or changing what it means is a new API_VERSION, never an accident."""
from __future__ import annotations

import dataclasses
import inspect
import json

import pytest

from nms_save_vault import __version__
from nms_save_vault.core import api, locations, platform, state
from nms_save_vault.core.hashname import key
from nms_save_vault.core.meta import MetaInfo
from nms_save_vault.core.slotmap import SaveFileRef
from nms_save_vault.core.store.manifest import MemberRecord

from .conftest import steam_save
from .store_helpers import fingerprint, new_vault, open_vault_store, steam_live

# Regenerate with _shape() after a deliberate change, and raise API_VERSION when anything was
# removed or changed meaning.
PINNED = {
    "API_VERSION": ("value", "int"),
    "AccountChange": ("fields", ("op_id", "at", "app", "platform", "account_key", "note", "adds",
                                 "removes", "undo_of"), ()),
    "AppChange": ("fields", ("op_id", "kind", "at", "app", "target", "placed", "evicted", "kept",
                             "snapshot", "made", "seq", "undo_of"), ()),
    "Before": ("fields", ("change", "pair", "kept"), ()),
    "KeptCopy": ("fields", ("kind", "entry", "slot", "member"), ()),
    "LiveTarget": ("fields", ("platform", "account_key", "slot", "instance", "at"), ("ref",)),
    "VaultHistory": ("class", {
        "account_changes_by": "(self, app: 'str') -> 'list[AccountChange]'",
        "changes_by": "(self, app: 'str') -> 'list[AppChange]'",
        "made_by": "(self, app: 'str') -> 'set[str]'",
        "touched_by": "(self, app: 'str') -> 'set[str]'",
        "last_before": "(self, app: 'str', *, slot: 'LiveTarget | None' = None, "
                       "playthrough: 'str | None' = None) -> 'Before | None'"}, ()),
    "open_history": ("function", "(vault_root: 'str | Path | None' = None) -> 'VaultHistory'"),
    "playthrough_of": ("function", "(member: 'MemberView') -> 'str | None'"),
    "slot_of": ("function", "(folder: 'str | Path', slot: 'int') -> 'LiveTarget'"),
    "AppIdentity": ("fields", ("name", "version"), ()),
    "CORE_VERSION": ("value", "str"),
    "CtxInput": ("fields", ("name", "psd", "container", "save"), ()),
    "DecodedSave": ("fields", ("root", "container", "chunks", "decompressed_bytes",
                               "decompressed_sha256", "json_bytes", "repaired_escapes"), ()),
    "FileEntry": ("fields", ("path", "size", "sha256", "mtime_ns", "inline", "extra"), ()),
    "MemberView": ("fields", ("ref", "data_path", "meta_path", "exists", "data_size",
                              "data_mtime", "ordinal_used", "info", "note", "xbox",
                              "meta_sha256"),
                   ("cloud_status", "effective_timestamp", "label", "mapped_ordinal", "moved",
                    "save_name", "save_type_label", "slot", "timestamp", "valid")),
    "PLATFORMS": ("value", "tuple"),
    "PrivacyError": ("error", ("LookupError", "Exception")),
    "SaveDecodeError": ("error", ("ValueError", "Exception")),
    "SaveDirView": ("fields", ("path", "slots", "account_present", "steam_cloud",
                               "stray_files", "xbox_index"), ("occupied_slots",)),
    "SaveInput": ("fields", ("root", "common", "layout"), ()),
    "SlotView": ("fields", ("slot", "a", "b"),
                 ("display_name", "members", "newest", "occupied", "present_members")),
    "StoreEntry": ("fields", ("id", "kind", "label", "reason", "op_id", "created_utc",
                              "created_local", "app_version", "platform", "account_key",
                              "source", "scope", "origin", "files", "members", "account", "xbox",
                              "body_sha256", "notes", "absent", "schema", "extra"), ("slots",)),
    "StoreError": ("error", ("OperationError", "Exception")),
    "StoreNotUpgraded": ("error", ("StoreError", "OperationError")),
    "StoreTooNew": ("error", ("StoreError", "OperationError")),
    "StoreUnavailable": ("error", ("StoreError", "OperationError")),
    "VAULT": ("value", "str"),
    "VaultReader": ("class", {"entries": "(self) -> 'list[StoreEntry]'",
                              "entry": "(self, entry_id: 'str') -> 'StoreEntry'",
                              "read_file": "(self, entry_id: 'str', path: 'str') -> 'bytes'"},
                    (("root", "Path"),)),
    "app_of": ("function", "(recorded: 'object') -> 'str'"),
    "contexts_of": ("function", "(root: 'dict') -> 'tuple[SaveInput, dict[str, CtxInput]]'"),
    "current": ("function", "() -> 'AppIdentity'"),
    "decode_save": ("function", "(data: 'bytes', *, bare_size_hint: 'int | None' = None, "
                                "max_decompressed: 'int' = 268435456, "
                                "timings: 'dict[str, int] | None' = None) -> 'DecodedSave'"),
    "default_vault_dir": ("function", "() -> 'Path'"),
    "game_running": ("function", "() -> 'bool | None'"),
    "get": ("function", "(node: 'object', *path: 'str', default: 'object' = None) -> 'object'"),
    "is_stamp": ("function", "(recorded: 'object') -> 'bool'"),
    "items": ("function", "(node: 'object', *path: 'str') -> 'list'"),
    "known_game_version": ("function", "(member: 'MemberView', *, platform: 'str') -> 'bool'"),
    "read_account": ("function", "(folder: 'str | Path') -> 'dict | None'"),
    "owner_time": ("function", "(base: 'object') -> 'int | None'"),
    "owned": ("function", "(entry: 'object', target: 'dict', context: 'str' = 'base', *, "
                          "made: 'int', platform: 'str | None' = None) -> 'bytes'"),
    "rehomed": ("function", "(entry: 'object', target: 'dict', context: 'str' = 'base', *, "
                            "platform: 'str | None' = None) -> 'bytes'"),
    "live_save_dirs": ("function", "() -> 'list[Path]'"),
    "open_vault": ("function", "(vault_root: 'str | Path | None' = None, *, "
                               "live_roots: 'Sequence[Path] | None' = None) -> 'VaultReader'"),
    "platform_of": ("function", "(folder: 'str | Path') -> 'str'"),
    "read_member": ("function", "(member: 'MemberView') -> 'DecodedSave'"),
    "scan_save_dir": ("function", "(folder: 'str | Path') -> 'SaveDirView'"),
    "set_app": ("function", "(name: 'str', version: 'str') -> 'AppIdentity'"),
    "vault_dir": ("function", "() -> 'Path'"),
    "create_vault": ("function", "(vault_root: 'str | Path | None' = None, *, "
                                 "chosen_missing_ok: 'bool' = False) -> 'Path'"),
    "Inventory": ("fields", ("context", "owner", "index", "field", "where", "width", "height",
                             "valid", "special", "inventory_class", "stack_group", "slots"), ()),
    "Slot": ("fields", ("number", "item", "kind", "amount", "max_amount", "x", "y", "damage",
                        "installed"), ()),
    "SpecialSlot": ("fields", ("x", "y", "kind"), ()),
    "inventories_of": ("function", "(root: 'dict') -> 'list[Inventory]'"),
    "ASSET_KINDS": ("value", "tuple"),
    "INDEX_VERSION": ("value", "int"),
    "Asset": ("fields", ("context", "kind", "index", "where", "key", "name", "model", "cls",
                         "current", "galaxy", "address", "facts"), ()),
    "assets_of": ("function", "(root: 'dict') -> 'list[Asset]'"),
    "state_of": ("function", "(root: 'dict', asset: 'Asset') -> 'str'"),
    "vault_stamp": ("function", "(vault_root: 'str | Path | None' = None) -> 'tuple[int, ...]'"),
    # the write side: an edit of a live save, and its undo
    "AddMember": ("fields", ("where", "name", "raw"), ()),
    "Insert": ("fields", ("where", "index", "raw"), ()),
    "Remove": ("fields", ("where",), ()),
    "Replace": ("fields", ("where", "raw"), ()),
    "dumps": ("function", "(value: 'Any') -> 'bytes'"),
    "dumps_named": ("function", "(value: 'Any') -> 'bytes'"),
    # changes an app holds before it writes them
    "Steps": ("class", {}, ()),
    "save_text": ("function", "(data: 'bytes') -> 'bytes'"),
    "edit_text": ("function", "(text: 'bytes', edits: 'Sequence[Replace | Remove | Insert | "
                              "AddMember] | Steps') -> 'bytes'"),
    "parse_text": ("function", "(text: 'bytes') -> 'dict'"),
    "ConfirmationRequired": ("error", ("LiveOpError", "OperationError")),
    "EditRefused": ("error", ("ValueError", "Exception")),
    "LiveOpError": ("error", ("OperationError", "Exception")),
    "PatchRefused": ("error", ("ValueError", "Exception")),
    "TargetError": ("error", ("LiveOpError", "OperationError")),
    "LiveSession": ("class", {
        "refresh": "(self) -> 'None'",
        "plan_edit": "(self, slot: 'LiveTarget', "
                     "edits: 'Sequence[Replace | Remove | Insert | AddMember] | Steps', *, "
                     "what: 'str', expected: 'str | None' = None) -> 'WritePlan'",
        "folder_of": "(self, slot: 'LiveTarget') -> 'Path | None'",
        "plan_duplicate": "(self, source: 'LiveTarget', target: 'LiveTarget', *, what: 'str', "
                          "expected: 'str | None' = None) -> 'WritePlan'",
        "slot_at": "(self, folder: 'str | Path', slot: 'int') -> 'LiveTarget | None'",
        "plan_edits": "(self, edits: 'Sequence[tuple[LiveTarget, Sequence[Replace | Remove | "
                      "Insert | AddMember] | Steps, str | None]]', *, what: 'str', "
                      "slot_whats: 'Sequence[str] | None' = None) -> 'WritePlan'",
        "plan_undo": "(self, op_id: 'str') -> 'WritePlan'",
        "write": "(self, plan: 'WritePlan', confirmed: 'Iterable[str]' = ()) -> 'WriteResult'",
        "interrupted": "(self) -> 'list[Interrupted]'",
        "resolve": "(self, op_id: 'str', choice: 'str', confirmed: 'Iterable[str]' = ()) "
                   "-> 'WriteResult'",
        "plan_unlock": "(self, folder: 'str | Path', unlocks: 'Mapping[str, Sequence[str]]', *, "
                       "what: 'str') -> 'UnlockPlan'",
        "plan_unlock_undo": "(self, op_id: 'str') -> 'UnlockPlan'",
        "write_account": "(self, plan: 'UnlockPlan', confirmed: 'Iterable[str]' = ()) "
                         "-> 'WriteResult'"}, ()),
    "UNLOCK_LISTS": ("value", "tuple"),
    "UnlockPlan": ("class", {}, (("adds", "tuple[tuple[str, str], ...]"),
                                 ("blocked", "tuple[Notice, ...]"),
                                 ("confirmations", "frozenset[str]"),
                                 ("removes", "tuple[tuple[str, str], ...]"),
                                 ("undoes", "str | None"))),
    "Interrupted": ("fields", ("op_id", "app", "text", "recommended", "choices"), ()),
    "Notice": ("fields", ("code", "severity", "text"), ()),
    "SEVERITIES": ("value", "tuple"),
    "WRITE_STATUSES": ("value", "tuple"),
    "WritePlan": ("class", {}, (("blocked", "tuple[Notice, ...]"),
                                ("confirmations", "frozenset[str]"),
                                ("made", "tuple[str, ...]"), ("outcome", "str"),
                                ("undoes", "str | None"),
                                ("warnings", "tuple[Notice, ...]"))),
    "WriteResult": ("fields", ("op_id", "status", "text", "code", "warnings", "undo"), ()),
    "open_live": ("function", "(vault_root: 'str | Path | None' = None, *, "
                              "live_dirs: 'Sequence[str | Path] | None' = None) "
                              "-> 'LiveSession'"),
    # thinning the vault, pins, the size the reminder compares (docs/thinning.md)
    "ThinCandidate": ("fields", ("id", "kind", "created", "created_utc", "label", "saves",
                                 "bytes", "reason"), ()),
    "ThinError": ("error", ("StoreError", "OperationError")),
    "ThinKept": ("fields", ("id", "kind", "created", "created_utc", "label", "saves",
                            "reasons"), ()),
    "ThinPlan": ("fields", ("vault_root", "now", "candidates", "kept", "entries", "bytes",
                            "blocked", "objects", "object_sizes", "manifest_sizes"), ()),
    "ThinProgress": ("fields", ("done", "total", "current"), ()),
    "ThinResult": ("fields", ("thinned", "freed_bytes", "left", "cancelled", "message"), ()),
    "ThinSave": ("fields", ("slot", "name"), ()),
    "ThinnedEntry": ("fields", ("id", "when", "why", "bytes", "kind", "label", "created_utc",
                                "op_id", "done"), ()),
    "plan_thin": ("function", "(vault_root: 'str | Path', *, now: 'datetime | None' = None) "
                              "-> 'ThinPlan'"),
    "run_thin": ("function", "(vault_root: 'str | Path', plan: 'ThinPlan', "
                             "chosen_ids: 'Iterable[str]', *, "
                             "progress: 'Callable[[ThinProgress], None] | None' = None, "
                             "cancel: 'threading.Event | None' = None) -> 'ThinResult'"),
    "finish_thinning": ("function", "(vault_root: 'str | Path') -> 'ThinResult | None'"),
    "thinning_pending": ("function", "(vault_root: 'str | Path') -> 'bool'"),
    "thinned_entries": ("function", "(vault_root: 'str | Path') -> 'dict[str, ThinnedEntry]'"),
    "pin_entry": ("function", "(vault_root: 'str | Path', entry_id: 'str') -> 'None'"),
    "unpin_entry": ("function", "(vault_root: 'str | Path', entry_id: 'str') -> 'None'"),
    "pinned_entries": ("function", "(vault_root: 'str | Path') -> 'frozenset[str]'"),
    "pin_version": ("function", "(vault_root: 'str | Path', vid: 'str', *, "
                                "pair: 'tuple[str | None, str | None] | None' = None, "
                                "pid: 'str | None' = None) -> 'None'"),
    "unpin_version": ("function", "(vault_root: 'str | Path', vid: 'str') -> 'None'"),
    "pinned_versions": ("function", "(vault_root: 'str | Path') -> 'frozenset[str]'"),
    "vault_size": ("function", "(vault_root: 'str | Path') -> 'int'"),
}

# Types the API hands out inside its own (MemberView.ref and .info, StoreEntry.members).
REACHED = {
    MetaInfo: (("ordinal", "header", "meta_format", "size_decompressed", "size_disk",
                "base_version", "game_mode", "season", "total_play_time", "save_name",
                "save_summary", "difficulty", "slot_identifier", "timestamp"),
               ("difficulty_label", "valid")),
    MemberRecord: (("slot", "member", "data", "meta", "vid", "ordinal", "meta_plain_sha256",
                    "pid", "play_time", "meta_ts", "name", "summary", "difficulty", "game_mode",
                    "season", "base_version", "meta_format", "size_decompressed", "valid", "note",
                    "xbox_identifier", "file_time_ns", "extra"), ()),
    SaveFileRef: (("file_no",), ("data_name", "member", "member_label", "meta_name",
                                 "save_type_label", "slot", "storage_ordinal")),
}


def _properties(cls) -> tuple[str, ...]:
    return tuple(sorted(n for n, v in vars(cls).items() if isinstance(v, property)))


def _typed_properties(cls) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((n, str(v.fget.__annotations__.get("return")))
                        for n, v in vars(cls).items() if isinstance(v, property)))


def _shape(obj):
    if inspect.isclass(obj) and dataclasses.is_dataclass(obj):
        return "fields", tuple(f.name for f in dataclasses.fields(obj)), _properties(obj)
    if inspect.isclass(obj) and issubclass(obj, BaseException):
        return "error", tuple(c.__name__ for c in obj.__mro__[1:3])
    if inspect.isclass(obj):
        return "class", {n: str(inspect.signature(v)) for n, v in vars(obj).items()
                         if callable(v) and not n.startswith("_")}, _typed_properties(obj)
    if callable(obj):
        return "function", str(inspect.signature(obj))
    return "value", type(obj).__name__


def test_the_api_is_exactly_the_pinned_names_signatures_and_fields_at_version_1():
    assert sorted(api.__all__) == sorted(PINNED) and len(api.__all__) == len(PINNED)
    assert {name: _shape(getattr(api, name)) for name in api.__all__} == PINNED
    assert api.API_VERSION == 1
    assert api.CORE_VERSION == __version__


def test_the_words_a_write_is_reported_in_are_pinned():
    assert api.SEVERITIES == ("info", "warn", "confirm", "block")
    assert api.WRITE_STATUSES == ("done", "rolled_back", "failed", "refused", "stale")


def test_the_types_reached_through_the_api_are_pinned_too():
    for cls, (fields, properties) in REACHED.items():
        assert tuple(f.name for f in dataclasses.fields(cls)) == fields, cls.__name__
        assert _properties(cls) == properties, cls.__name__


def _save(folder, *, units: int, base_version: int = 4233):
    payload = json.dumps({key("BaseVersion"): base_version,
                          key("PlayerStateData"): {key("Units"): units}}).encode()
    steam_save(folder, 1, 0, "Main", 60, 1, 1_790_000_000, payload=payload,
               base_version=base_version)


def test_a_save_is_found_decoded_and_read_by_readable_names(tmp_path):
    folder = tmp_path / "st_1"
    _save(folder, units=1234)
    before = fingerprint(folder)
    assert api.platform_of(folder) == "steam"
    member = api.scan_save_dir(folder).slots[1].a
    _save_input, contexts = api.contexts_of(api.read_member(member).root)
    assert api.get(contexts["base"].psd, "Units") == 1234
    assert api.items(contexts["base"].psd, "Units") == []
    assert api.known_game_version(member, platform="steam")
    assert fingerprint(folder) == before                        # reading wrote nothing


def test_an_xbox_save_is_found_and_decoded(tmp_path, make_wgs_account):
    payload = json.dumps({key("PlayerStateData"): {key("Units"): 77}})
    folder = make_wgs_account(tmp_path, [("Slot1Auto", payload, "Main", "", 60, 1_790_000_000)])
    assert api.platform_of(folder) == "xbox"
    member = api.scan_save_dir(folder).slots[1].a
    _save_input, contexts = api.contexts_of(api.read_member(member).root)
    assert api.get(contexts["base"].psd, "Units") == 77
    assert api.known_game_version(member, platform="xbox")


def test_a_save_from_a_newer_game_or_with_an_unreadable_meta_is_not_known(tmp_path):
    folder = tmp_path / "st_1"
    _save(folder, units=1, base_version=99_999)
    assert not api.known_game_version(api.scan_save_dir(folder).slots[1].a, platform="steam")
    _save(folder, units=1)
    (folder / "mf_save.hg").write_bytes(b"not a meta")
    assert not api.known_game_version(api.scan_save_dir(folder).slots[1].a, platform="steam")


def test_an_unknown_platform_is_refused_not_waved_through(tmp_path):
    folder = tmp_path / "st_1"
    _save(folder, units=1)
    with pytest.raises(ValueError, match="platform"):
        api.known_game_version(api.scan_save_dir(folder).slots[1].a, platform="Steam")


def test_ownership_fields_cannot_be_read_through_the_api():
    with pytest.raises(api.PrivacyError):
        api.get({}, "PlayerStateData", "Owner")


def test_live_save_dirs_are_steam_and_gog_then_microsoft(monkeypatch, tmp_path):
    monkeypatch.setattr(locations, "find_live_save_dirs", lambda: [tmp_path / "st_1"])
    monkeypatch.setattr(locations, "find_microsoft_save_dirs", lambda: [tmp_path / "wgs"])
    assert api.live_save_dirs() == [tmp_path / "st_1", tmp_path / "wgs"]


def test_the_vault_chosen_in_nms_save_vault_is_the_one_shared(tmp_path, monkeypatch):
    chosen = tmp_path / "D" / "MyVault"
    chosen.mkdir(parents=True)
    local = tmp_path / "local"
    installed = local / "Programs" / "NMSSaveVault" / "state.json"
    installed.parent.mkdir(parents=True)
    host = platform.Host(platform.WINDOWS, tmp_path / "home", {"LOCALAPPDATA": str(local)})
    monkeypatch.setattr(platform.Host, "current", classmethod(lambda cls: host))
    monkeypatch.setattr(state, "default_state_path", lambda: tmp_path / "own" / "state.json")
    assert api.vault_dir() == api.default_vault_dir()           # nothing chosen yet
    installed.write_text(json.dumps({"vault": str(chosen)}), encoding="utf-8")
    assert api.vault_dir() == chosen
    installed.write_text("{damaged", encoding="utf-8")
    assert api.vault_dir() == api.default_vault_dir()           # skipped, never moved aside
    assert installed.read_text(encoding="utf-8") == "{damaged"
    portable = tmp_path / "E" / "PortableVault"               # chosen in a portable kit
    portable.mkdir(parents=True)
    state.save(state.AppState(vault=str(portable)))          # NMS Save Vault saves its settings
    assert state.pointer_path(host).is_file()
    assert api.vault_dir() == portable                        # the pointer comes first
    own = tmp_path / "own" / "state.json"                     # those settings now choose another
    own.write_text(json.dumps({"vault": str(chosen)}), encoding="utf-8")
    assert api.vault_dir() == chosen                          # a stale pointer is passed over


def test_the_vault_opens_to_read_and_nothing_in_it_is_written(tmp_path):
    vault = new_vault(tmp_path)
    added = open_vault_store(vault).add_from_folder(
        steam_live(tmp_path / "in"), kind="imported", label="x",
        source={"type": "folder", "name": "x"})
    before = fingerprint(vault)
    reader = api.open_vault(vault, live_roots=())
    assert not hasattr(reader, "add_from_folder") and not hasattr(reader, "materialized")
    assert reader.root == vault
    assert [e.id for e in reader.entries()] == [added.id] and reader.entry(added.id) == added
    path = added.files[0].path
    assert reader.read_file(added.id, path) == (
        tmp_path / "in" / "st_76561198000000001" / path).read_bytes()
    assert fingerprint(vault) == before


def test_no_vault_is_a_store_error(tmp_path):
    with pytest.raises(api.StoreError):
        api.open_vault(tmp_path / "none", live_roots=())


def _two_backups_of_one_folder(tmp_path):
    vault = new_vault(tmp_path)
    live = steam_live(tmp_path / "in")
    store = open_vault_store(vault)
    made = [store.add_from_folder(live, kind="full", label="", source={"type": "folder",
                                                                       "name": "x"})
            for _ in range(2)]
    return vault, made


def test_the_vault_is_thinned_through_the_api(tmp_path):
    vault, (older, newer) = _two_backups_of_one_folder(tmp_path)
    plan = api.plan_thin(vault)
    assert [c.id for c in plan.candidates] == [older.id] and plan.blocked == ()
    assert plan.candidates[0].reason.endswith("also in a backup that stays")
    assert not api.thinning_pending(vault) and api.finish_thinning(vault) is None
    seen = []
    result = api.run_thin(vault, plan, [older.id], progress=seen.append)
    assert result.thinned == (older.id,) and result.freed_bytes == plan.bytes and seen
    assert [e.id for e in api.open_vault(vault, live_roots=()).entries()] == [newer.id]
    thinned = api.thinned_entries(vault)[older.id]
    assert thinned.done and thinned.bytes == plan.bytes and thinned.kind == "full"
    assert api.vault_size(vault) == open_vault_store(vault).stored_bytes()


def test_pins_round_trip_through_the_api_and_are_refused_while_the_vault_is_read_only(
        tmp_path):
    vault, (older, newer) = _two_backups_of_one_folder(tmp_path)
    vid = older.members[0].vid
    api.pin_entry(vault, older.id)
    api.pin_version(vault, vid)
    assert api.pinned_entries(vault) == {older.id} and vid in api.pinned_versions(vault)
    plan = api.plan_thin(vault)              # the pinned one stays; its twin need not
    assert [c.id for c in plan.candidates] == [newer.id]
    assert [k.id for k in plan.kept] == [older.id]
    assert plan.kept[0].reasons[:2] == ("you pinned it", "it holds a save you pinned")
    api.unpin_entry(vault, older.id)
    api.unpin_version(vault, vid)
    assert api.pinned_entries(vault) == frozenset() == api.pinned_versions(vault)
    notes = vault / "organizer" / "notes.json"
    before = notes.read_bytes()
    for part in ("store", "organizer"):
        path = (vault / "store" / "FORMAT") if part == "store" else (
            vault / "organizer" / "organizer.json")
        saved = path.read_bytes()
        d = json.loads(saved)
        d["write_compat" if part == "store" else "schema"] += 1
        path.write_text(json.dumps(d), encoding="ascii")
        for pin in (lambda: api.pin_entry(vault, older.id), lambda: api.pin_version(vault, vid),
                    lambda: api.unpin_entry(vault, older.id)):
            with pytest.raises(api.StoreTooNew):
                pin()
        path.write_bytes(saved)
    assert notes.read_bytes() == before                     # nothing was written


def test_a_playthrough_and_a_slot_are_named_as_the_history_names_them(tmp_path):
    folder = tmp_path / "st_1"
    _save(folder, units=1)
    member = api.scan_save_dir(folder).slots[1].a
    assert api.playthrough_of(member) == "0000000000000001"       # steam_save ident 1
    assert api.slot_of(folder, 3) == api.LiveTarget("steam", "st_1", 3)
