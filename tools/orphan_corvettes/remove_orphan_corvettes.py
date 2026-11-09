"""Remove orphan corvette layouts from Steam save slots. A side utility, not part of the app.

READ README.md IN THIS FOLDER FIRST. This edits save data, which NMS Save Vault itself never does.

The game finds a corvette's PlayerShipBase layout by the corvette's ship slot: the layout's
UserData is that slot (NMS-Unpack's reading of NMS.exe, 2026-11-10). A layout whose UserData
names no corvette is an orphan. This tool first tied layouts to corvettes by time instead (the
ship's Resource.Seed is its creation time and the layout's Owner.TS that time or one second
later); a layout only that rule ties to a corvette is kept unless --include-ts-linked is given.

    uv run --with lz4 python tools/orphan_corvettes/remove_orphan_corvettes.py <st_ folder> <slot>...
            [--write] [--include-ts-linked] [--vault DIR]

Without --write it only reports. With --write: the game must be known closed; each slot to change
is kept in the vault first (the app's own ``keep``), and the vault must then hold every file of
the slot byte for byte; only then is each file replaced, after the new bytes were proven to decode
to the old save minus exactly the removed layouts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

from nms_save_vault import __version__
from nms_save_vault.core import formats, locations, lz4_block, meta, safety
from nms_save_vault.core import state as appstate
from nms_save_vault.core.hashname import key as K
from nms_save_vault.core.store.versionstore import VersionStore, vault_lock

CORVETTE_FOLDER = "/BIGGS/"
CHUNK = formats.SAVE_CHUNK_DECOMPRESSED_MAX
_TOKEN = re.compile(rb'["{}\[\]]')
_STRING_END = re.compile(rb'["\\]')
_SCALAR_END = re.compile(rb"[,}\]\s]")


class Refused(Exception):
    pass


# --- reading the tree ---------------------------------------------------------------------

def g(node, *names):
    for n in names:
        if not isinstance(node, dict):
            return None
        node = node.get(K(n))
    return node


def loads(body: bytes):
    # As the app reads saves: invalid UTF-8 survives as surrogates, raw control chars accepted.
    return json.loads(body.decode("utf-8", errors="surrogateescape"), strict=False)


def seed_of(ship) -> int | None:
    seed = g(ship, "Resource", "Seed")
    try:
        return int(seed[1], 16) if isinstance(seed, list) and len(seed) == 2 else None
    except (TypeError, ValueError):
        return None


def contexts(root: dict):
    for name in ("BaseContext", "ExpeditionContext"):
        psd = g(root, name, "PlayerStateData")
        if isinstance(psd, dict):
            yield name, psd


def orphans_in(psd: dict, include_ts_linked: bool) -> tuple[list[int], list[str]]:
    """Indexes into PersistentPlayerBases to remove, and a line per layout for the report."""
    corvettes = {i: seed_of(s) for i, s in enumerate(g(psd, "ShipOwnership") or [])
                 if CORVETTE_FOLDER in str(g(s, "Resource", "Filename") or "")}
    bases = g(psd, "PersistentPlayerBases") or []
    layouts = [(i, b) for i, b in enumerate(bases) if g(b, "BaseType", "PersistentBaseTypes") == "PlayerShipBase"]

    def claims(seed, b):
        ts = g(b, "Owner", "TS")
        return seed is not None and isinstance(ts, int) and 0 <= ts - seed <= 1

    def slot_of(b):
        ud = g(b, "UserData")
        return ud if isinstance(ud, int) and not isinstance(ud, bool) else None

    for slot in corvettes:
        n = sum(1 for _i, b in layouts if slot_of(b) == slot)
        if n > 1:
            raise Refused(f"the corvette in ship slot {slot} is named by {n} layouts; the game "
                          "uses one, and this tool cannot tell which")
    remove, report = [], []
    for i, b in layouts:
        ud = slot_of(b)
        by_time = next((slot for slot, seed in corvettes.items() if claims(seed, b)), None)
        parts = len(g(b, "Objects") or [])
        if ud in corvettes:
            state = f"kept: layout of the corvette in ship slot {ud}"
        elif by_time is not None and not include_ts_linked:
            state = (f"kept: its UserData names no corvette, but its owner time ties it to the "
                     f"corvette in ship slot {by_time}, the rule this tool used first "
                     "(see --include-ts-linked)")
        else:
            state = "REMOVE: orphan"
            remove.append(i)
        report.append(f"layout #{i} UserData={ud} parts={parts}: {state}")
    return remove, report


# --- byte spans in the JSON text: the edit removes bytes and touches nothing else ---------

def _ws(body: bytes, p: int) -> int:
    while body[p] in b" \t\r\n":
        p += 1
    return p


def _string_end(body: bytes, p: int) -> int:
    """``p`` at the opening quote; the index after the closing quote."""
    p += 1
    while True:
        m = _STRING_END.search(body, p)
        if m is None:
            raise Refused("unterminated string in the save JSON")
        if body[m.start()] == 0x5C:
            p = m.start() + 2
            continue
        return m.start() + 1


def _value_end(body: bytes, p: int) -> int:
    p = _ws(body, p)
    c = body[p]
    if c == 0x22:
        return _string_end(body, p)
    if c in b"{[":
        depth = 0
        while True:
            m = _TOKEN.search(body, p)
            if m is None:
                raise Refused("unbalanced save JSON")
            q, t = m.start(), body[m.start()]
            if t == 0x22:
                p = _string_end(body, q)
                continue
            depth += 1 if t in b"{[" else -1
            if depth == 0:
                return q + 1
            p = q + 1
    m = _SCALAR_END.search(body, p)
    if m is None:
        raise Refused("unterminated value in the save JSON")
    return m.start()


def _member(body: bytes, obj: int, name: str) -> int:
    """Start of the value of key ``name`` (readable) in the object starting at ``obj``."""
    want = K(name)
    p = _ws(body, obj)
    if body[p] != 0x7B:
        raise Refused("expected an object in the save JSON")
    p = _ws(body, p + 1)
    while body[p] != 0x7D:
        k_end = _string_end(body, p)
        k = loads(body[p:k_end])
        p = _ws(body, k_end)
        if body[p] != 0x3A:
            raise Refused("expected ':' in the save JSON")
        v = _ws(body, p + 1)
        if k == want:
            return v
        p = _ws(body, _value_end(body, v))
        if body[p] == 0x2C:
            p = _ws(body, p + 1)
    raise Refused(f"{name} not found in the save JSON")


def _elements(body: bytes, arr: int) -> tuple[int, int, list[tuple[int, int]]]:
    """(index after '[', index of ']', element spans) of the array starting at ``arr``."""
    if body[arr] != 0x5B:
        raise Refused("expected an array in the save JSON")
    p = _ws(body, arr + 1)
    spans = []
    while body[p] != 0x5D:
        e = _value_end(body, p)
        spans.append((p, e))
        p = _ws(body, e)
        if body[p] == 0x2C:
            p = _ws(body, p + 1)
    return arr + 1, p, spans


def without_elements(body: bytes, context: str, drop: list[int]) -> bytes:
    psd = _member(body, _member(body, 0, context), "PlayerStateData")
    open_, close, spans = _elements(body, _member(body, psd, "PersistentPlayerBases"))
    parts = [body[s:e] for s, e in spans]
    if body[open_:close] != b",".join(parts):
        raise Refused("PersistentPlayerBases is not written compactly; not touching it")
    gone = set(drop)
    return body[:open_] + b",".join(p for i, p in enumerate(parts) if i not in gone) + body[close:]


# --- container and meta -------------------------------------------------------------------

def pack(raw: bytes) -> bytes:
    """The game's container: 512 KiB chunks, each a 16-byte header and one LZ4 block."""
    out = bytearray()
    for off in range(0, len(raw), CHUNK):
        piece = raw[off:off + CHUNK]
        import lz4.block            # only to write: the report needs no lz4
        block = lz4.block.compress(piece, mode="default", store_size=False)
        out += struct.pack("<4I", formats.SAVE_MAGIC, len(block), len(piece), 0) + block
    return bytes(out)


def new_meta(old_meta: bytes, ordinal: int, size_dec: int, size_disk: int) -> bytes:
    """The same meta with the two size fields set; everything else, the timestamp too, kept."""
    plain = bytearray(meta.decrypt(old_meta, ordinal))
    struct.pack_into("<I", plain, formats.OFF_SIZE_DECOMPRESSED, size_dec)
    struct.pack_into("<I", plain, formats.OFF_SIZE_DISK, size_disk)
    enc = meta.encrypt(bytes(plain), ordinal)
    if meta.decrypt(enc, ordinal) != bytes(plain):
        raise Refused("the meta did not survive an encrypt/decrypt round trip")
    return enc


# --- one save file ------------------------------------------------------------------------

def stem_of(f: int) -> str:
    return "save" if f == 1 else f"save{f}"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fix_file(folder: Path, f: int, include_ts_linked: bool):
    """(new data, new meta, sha256 of the old data and meta, report lines); new data None when
    there is nothing to remove."""
    stem, ordinal = stem_of(f), f + 1
    data = (folder / f"{stem}.hg").read_bytes()
    mf = (folder / f"mf_{stem}.hg").read_bytes()
    info = meta.parse(meta.decrypt(mf, ordinal), ordinal)
    if info.header != formats.META_HEADER:
        raise Refused(f"{stem}: the meta does not decrypt for this slot")
    if len(mf) != formats.META_LEN_WORLDS_II:
        raise Refused(f"{stem}: meta length {len(mf)} is not the Worlds Part II Steam meta this tool was tested on")
    chunks, _ = lz4_block.walk_chunks(data)
    if not chunks or sum(c.decompressed_size for c in chunks) != info.size_decompressed:
        raise Refused(f"{stem}: data and meta sizes disagree; not touching it")
    raw = lz4_block.decompress(data)
    body, sep, tail = raw.partition(b"\x00")
    if not sep or tail:
        raise Refused(f"{stem}: unexpected bytes after the JSON terminator")
    old = (sha(data), sha(mf))
    root, expected = loads(body), loads(body)

    lines = [f"{stem} (save '{info.save_name}', BaseVersion {info.base_version})"]
    new_body, removed = body, 0
    for cname, psd in contexts(root):
        drop, report = orphans_in(psd, include_ts_linked)
        lines += [f"  [{cname}] {r}" for r in report]
        if drop:
            new_body = without_elements(new_body, cname, drop)
            bases = g(expected, cname, "PlayerStateData", "PersistentPlayerBases")
            for i in sorted(drop, reverse=True):
                del bases[i]
            removed += len(drop)
    if not removed:
        lines.append("  nothing to remove")
        return None, None, old, lines

    if loads(new_body) != expected:
        raise Refused(f"{stem}: the edited JSON is not the original minus the removed layouts")
    new_raw = new_body + b"\x00"
    new_data = pack(new_raw)
    if lz4_block.decompress(new_data) != new_raw:
        raise Refused(f"{stem}: the re-packed data does not decode back to the edited JSON")
    new_mf = new_meta(mf, ordinal, len(new_raw), len(new_data))
    check = meta.parse(meta.decrypt(new_mf, ordinal), ordinal)
    if (check.size_decompressed, check.timestamp, check.save_name) != (len(new_raw), info.timestamp, info.save_name):
        raise Refused(f"{stem}: the rewritten meta does not read back as expected")
    for _cname, psd in contexts(loads(new_body)):
        if orphans_in(psd, include_ts_linked)[0]:
            raise Refused(f"{stem}: orphans are still found after the edit")
    lines.append(f"  removes {removed} layout(s); JSON {len(body):,} -> {len(new_body):,} bytes")
    return new_data, new_mf, old, lines


# --- the vault backup ---------------------------------------------------------------------

def vault_root(given: Path | None) -> Path:
    if given:
        return given
    st = appstate.load()
    return Path(st.vault) if (st and st.vault) else locations.default_vault_dir()


def slot_ref(folder: Path, slot: int) -> str:
    """The slot as the app's CLI names it: a live account is named by its folder name
    (``st_<id>``), configured or not."""
    return f"steam:{folder.name}:{slot}"


def keep_in_vault(folder: Path, vault: Path, slot: int) -> None:
    cmd = [sys.executable, "-m", "nms_save_vault.cli", "--live", str(folder), "--vault", str(vault),
           "keep", slot_ref(folder, slot)]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("  " + (done.stdout.strip() or done.stderr.strip()).replace("\n", "\n  "))
    if done.returncode != 0:
        raise Refused(f"the app could not keep slot {slot} in the vault (exit {done.returncode}): "
                      f"{done.stderr.strip()}")


def vault_holds(vault: Path, wanted: dict[str, bytes]) -> list[str]:
    """Ids of the vault entries holding every one of ``wanted`` (name -> bytes), each read back
    from the vault and compared byte for byte. Empty when any file is missing."""
    store = VersionStore.open(vault, lock=vault_lock(vault), app_version=__version__, read_only=True)
    found: dict[str, set[str]] = {}
    for entry in store.entries():
        for fe in entry.files:
            name = Path(fe.path).name
            if name in wanted and fe.sha256 == sha(wanted[name]) and store.file_bytes(fe) == wanted[name]:
                found.setdefault(entry.id, set()).add(name)
    return [eid for eid, names in found.items() if names == set(wanted)]


# --- main ---------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Remove orphan corvette layouts. Read README.md first.")
    ap.add_argument("folder", type=Path, help="the Steam st_<id> save folder")
    ap.add_argument("slots", type=int, nargs="+", help="slot numbers 1-15")
    ap.add_argument("--write", action="store_true", help="change the saves (default: report only)")
    ap.add_argument("--include-ts-linked", action="store_true",
                    help="also remove layouts whose UserData names no corvette but whose owner time "
                         "ties them to one")
    ap.add_argument("--vault", type=Path, help="the vault folder (default: the app's configured vault)")
    args = ap.parse_args()

    if not args.folder.name.startswith("st_"):
        print("Refused: this tool only handles a Steam save folder (st_<id>).")
        return 2
    if args.write and safety.is_game_running() is not False:
        print("Refused: No Man's Sky is running, or that could not be checked. Close it and try again.")
        return 2
    plans: dict[int, dict[int, tuple]] = {}
    for k in args.slots:
        if not 1 <= k <= 15:
            print(f"Refused: slot {k} is not 1-15.")
            return 2
        for f in (2 * k - 1, 2 * k):
            try:
                new_data, new_mf, old, lines = fix_file(args.folder, f, args.include_ts_linked)
            except (Refused, OSError, ValueError) as exc:
                print(f"Slot {k}, {stem_of(f)}: refused: {exc}\nNothing was written.")
                return 1
            print(f"Slot {k}: " + "\n".join(lines))
            if new_data is not None:
                plans.setdefault(k, {})[f] = (new_data, new_mf, old)
    if not args.write:
        print("\nReport only: nothing was written.")
        return 0
    if not plans:
        print("\nNothing to remove: nothing was written.")
        return 0

    vault = vault_root(args.vault)
    print(f"\nKeeping every slot to change in the vault first ({vault}):")
    for k in plans:
        files = {}
        for f in (2 * k - 1, 2 * k):
            for name in (f"{stem_of(f)}.hg", f"mf_{stem_of(f)}.hg"):
                files[name] = (args.folder / name).read_bytes()
        try:
            keep_in_vault(args.folder, vault, k)
            entries = vault_holds(vault, files)
        except Exception as exc:  # noqa: BLE001 - any failure here means: write nothing
            print(f"Refused: slot {k} is not safely in the vault ({exc}). Nothing was written.")
            return 1
        if not entries:
            print(f"Refused: the vault does not hold every file of slot {k} byte for byte. Nothing was written.")
            return 1
        print(f"  slot {k}: all {len(files)} files are in the vault (entry {entries[-1]})")

    if safety.is_game_running() is not False:
        print("Refused: No Man's Sky started meanwhile. Nothing was written.")
        return 2
    for k, per_file in plans.items():
        for f, (_d, _m, old) in per_file.items():
            stem = stem_of(f)
            now = (sha((args.folder / f"{stem}.hg").read_bytes()), sha((args.folder / f"mf_{stem}.hg").read_bytes()))
            if now != old:
                print(f"Refused: {stem} changed since it was read. Nothing was written.")
                return 1
    for k, per_file in plans.items():
        for f, (new_data, new_mf, _old) in per_file.items():
            stem = stem_of(f)
            for name, payload in ((f"{stem}.hg", new_data), (f"mf_{stem}.hg", new_mf)):
                tmp = args.folder / f"{name}.orphan-tmp"
                tmp.write_bytes(payload)
                if tmp.read_bytes() != payload:
                    tmp.unlink()
                    print(f"Stopped: {name} did not write correctly. Restore slot {k} from the vault (README).")
                    return 1
                os.replace(tmp, args.folder / name)
            print(f"Slot {k}: replaced {stem}.hg and mf_{stem}.hg")
    print("\nDone. Load each changed save in the game and check your corvettes before playing on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
