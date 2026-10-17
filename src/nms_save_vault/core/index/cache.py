"""The per-machine facts cache: one sqlite file per vault, derived data only (D4 2.9).

``<user cache>/vaults/<vault_id>/index-v1.sqlite``. Rows are content-addressed by vid, so the
file stays valid for any vault holding the same bytes, and deleting it loses nothing (C8).

Threads: one writer (the service's writer thread) owns the only write connection - sqlite's
``check_same_thread`` holds it to the thread that first wrote. Readers get a connection per
thread and share an LRU of parsed facts; WAL lets them read while the writer commits. Each
result is one transaction, so a killed app loses only what was in flight.

A file that fails to open or its quick check is renamed aside (the newest one kept) and a
fresh one created (``rebuilt``); a directory that cannot be written gives an in-memory cache
for the session (``warning``). The cache refuses to live inside the vault or a save folder.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import Path

from .schema import DECODER_VERSION, EXTRACTOR_VERSION, SCHEMA_VERSION
from .worker import IndexResult

FILENAME = "index-v1.sqlite"
LRU_SIZE = 512
GC_AGE_S = 180 * 24 * 3600
FAILED_STATUSES = ("corrupt", "unsupported", "error")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS version (
  vid                 TEXT PRIMARY KEY CHECK (length(vid) = 64),
  status              TEXT NOT NULL CHECK (status IN ('ok','partial','corrupt','unsupported','error')),
  schema              INTEGER NOT NULL,
  extractor           INTEGER NOT NULL,
  decoder             INTEGER NOT NULL,
  container           TEXT,
  layout              TEXT,
  data_bytes          INTEGER,
  json_bytes          INTEGER,
  decompressed_sha256 TEXT,
  play_time           INTEGER,
  save_name           TEXT,
  pid_in_data         TEXT,
  primary_ctx         TEXT,
  save_facts          TEXT,
  errors              TEXT NOT NULL DEFAULT '[]',
  hash_ms INTEGER, decode_ms INTEGER, parse_ms INTEGER, extract_ms INTEGER,
  indexed_at          INTEGER NOT NULL,
  last_seen           INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS context (
  vid   TEXT NOT NULL REFERENCES version(vid) ON DELETE CASCADE,
  ctx   TEXT NOT NULL CHECK (ctx IN ('base','expedition')),
  facts TEXT NOT NULL,
  glyphs INTEGER, own_bases INTEGER, base_parts INTEGER, ships INTEGER,
  companions INTEGER, quicksilver INTEGER, galaxies_with_bases INTEGER,
  PRIMARY KEY (vid, ctx)
);
CREATE TABLE IF NOT EXISTS failure (
  vid TEXT PRIMARY KEY, attempts INTEGER NOT NULL, last_error TEXT NOT NULL,
  last_at INTEGER NOT NULL, kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS version_extractor ON version (extractor);
CREATE INDEX IF NOT EXISTS version_status ON version (status);
"""


class CacheLocationError(ValueError):
    """The cache path lies inside the vault or a save folder (invariant I1)."""


def cache_file(cache_dir: Path) -> Path:
    """The index file in a vault's per-machine cache folder (``organizer.cache.cache_dir``)."""
    return Path(cache_dir) / FILENAME


def _dumps(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:             # a lone surrogate slipped through: keep it escaped
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class IndexCache:
    def __init__(self, path: Path | None, *, vault_id: str, app_version: str = "",
                 forbidden: Iterable[Path] = (), clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else None
        self.vault_id = vault_id
        self.clock = clock
        self.rebuilt = False
        self.warning: str | None = None
        if self.path is not None:
            _refuse_inside(self.path, forbidden)
        self._memory_uri: str | None = None
        self._local = threading.local()
        self._lru: OrderedDict[str, dict | None] = OrderedDict()
        self._lru_lock = threading.Lock()
        self._writer: sqlite3.Connection | None = None
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._keeper: sqlite3.Connection | None = None
        self._writer_thread: int | None = None
        self._open(app_version)
        self._writer_thread = None              # opening was not the writer thread's job

    # --- opening, recovery --------------------------------------------------------------

    def _open(self, app_version: str) -> None:
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._check_file()
                self._init(app_version)
                return
            except (OSError, sqlite3.OperationalError) as exc:
                self._close_connections()
                self._use_memory(f"Content index kept in memory this session (cache folder not writable: "
                                 f"{type(exc).__name__})")
        else:
            self._use_memory()
        self._init(app_version)

    def _init(self, app_version: str) -> None:
        con = self._write_con()
        with con:
            con.executescript(SCHEMA_SQL)
            now = str(int(self.clock()))
            for k, v in (("schema", str(SCHEMA_VERSION)), ("created", now), ("vault_id", self.vault_id),
                         ("app_version", app_version)):
                con.execute("INSERT OR IGNORE INTO meta (k, v) VALUES (?, ?)", (k, v))
            con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('last_open', ?)", (now,))

    def _check_file(self) -> None:
        if not self.path.exists():
            return
        try:
            con = sqlite3.connect(self.path)
            try:
                ok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            finally:
                con.close()
        except sqlite3.DatabaseError:
            ok = False
        if not ok:
            self._set_aside()

    def _set_aside(self) -> None:
        """Rename a damaged file to ``index-v1.corrupt-<UTC>.sqlite``; keep only the newest."""
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock()))
        stem = self.path.name.removesuffix(".sqlite")
        for old in self.path.parent.glob(f"{stem}.corrupt-*.sqlite"):
            old.unlink(missing_ok=True)
        os.replace(self.path, self.path.with_name(f"{stem}.corrupt-{stamp}.sqlite"))
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        self.rebuilt = True
        self.warning = "Content index rebuilt (cache file was damaged)"

    def _use_memory(self, warning: str | None = None) -> None:
        self._memory_uri = f"file:index-{uuid.uuid4().hex}?mode=memory&cache=shared"
        # A shared in-memory database lives as long as one connection to it is open.
        self._keeper = sqlite3.connect(self._memory_uri, uri=True, check_same_thread=False)
        if warning:
            self.warning = warning

    @property
    def in_memory(self) -> bool:
        return self._memory_uri is not None

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread is off only so close() can close every connection; each one is
        # still used by one thread: readers are per thread, the writer is checked below.
        if self._memory_uri is not None:
            con = sqlite3.connect(self._memory_uri, uri=True, timeout=5, check_same_thread=False)
        else:
            con = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 5000")
        with self._connections_lock:
            self._connections.append(con)
        return con

    def _reader(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = self._connect()
        return con

    def _write_con(self) -> sqlite3.Connection:
        """The one write connection, owned by the first thread that writes after opening."""
        me = threading.get_ident()
        if self._writer is None:
            self._writer = self._connect()
        if self._writer_thread is None:
            self._writer_thread = me
        elif self._writer_thread != me:
            raise RuntimeError("the index cache has one writer thread; route writes through it")
        return self._writer

    def adopt_writer(self) -> None:
        """Hand the write connection to the calling thread (the service's writer thread)."""
        self._writer_thread = threading.get_ident()

    # --- reads --------------------------------------------------------------------------

    def facts(self, vid: str) -> dict | None:
        """The stored :class:`schema.VersionFacts` of ``vid``, or None when not indexed."""
        with self._lru_lock:
            if vid in self._lru:
                self._lru.move_to_end(vid)
                return self._lru[vid]
        facts = self._load(vid)
        with self._lru_lock:
            self._lru[vid] = facts
            while len(self._lru) > LRU_SIZE:
                self._lru.popitem(last=False)
        return facts

    def _load(self, vid: str) -> dict | None:
        con = self._reader()
        row = con.execute("SELECT status, schema, extractor, save_facts, errors, primary_ctx FROM version "
                          "WHERE vid = ?", (vid,)).fetchone()
        if row is None:
            return None
        status, schema, extractor, save_facts, errors, primary = row
        stored = json.loads(save_facts) if save_facts else {}
        contexts = {ctx: json.loads(f) for ctx, f in
                    con.execute("SELECT ctx, facts FROM context WHERE vid = ? ORDER BY ctx", (vid,))}
        return {"schema": schema, "extractor": extractor, "status": status, "errors": json.loads(errors),
                "container": stored.get("container"), "save": stored.get("save"), "contexts": contexts,
                "primary": primary}

    def row(self, vid: str) -> dict | None:
        """The version row's scalar columns (status, versions, timings) for status displays."""
        cur = self._reader().execute(
            "SELECT status, extractor, decoder, data_bytes, json_bytes, indexed_at, hash_ms, decode_ms, "
            "parse_ms, extract_ms FROM version WHERE vid = ?", (vid,))
        names = [d[0] for d in cur.description]
        found = cur.fetchone()
        return dict(zip(names, found)) if found else None

    def indexed(self) -> dict[str, tuple[str, int]]:
        """vid -> (status, extractor version) for every row."""
        return {v: (s, e) for v, s, e in self._reader().execute("SELECT vid, status, extractor FROM version")}

    def counts(self) -> dict[str, int]:
        return dict(self._reader().execute("SELECT status, count(*) FROM version GROUP BY status").fetchall())

    def stale_vids(self) -> list[str]:
        """Rows from an older extractor: still served (marked stale), refreshed at REFRESH."""
        return [v for (v,) in self._reader().execute(
            "SELECT vid FROM version WHERE extractor < ? AND status NOT IN (?, ?, ?) ORDER BY data_bytes DESC",
            (EXTRACTOR_VERSION, *FAILED_STATUSES))]

    def retry_vids(self) -> list[str]:
        """Failed rows from an older decoder: worth one more try with the new one."""
        return [v for (v,) in self._reader().execute(
            "SELECT vid FROM version WHERE decoder < ? AND status IN (?, ?, ?)", (DECODER_VERSION, *FAILED_STATUSES))]

    def failure(self, vid: str) -> tuple[int, str, str] | None:
        """(attempts, kind, last error) of a transient failure, or None."""
        row = self._reader().execute("SELECT attempts, kind, last_error FROM failure WHERE vid = ?", (vid,)).fetchone()
        return tuple(row) if row else None

    def size_bytes(self) -> int:
        if self.path is None or self.in_memory:
            return 0
        return sum(p.stat().st_size for p in (self.path, Path(f"{self.path}-wal")) if p.exists())

    # --- writes (the writer thread only) ------------------------------------------------

    def write_result(self, result: IndexResult) -> None:
        """Store one worker result in one transaction. A ``mismatch`` is never cached."""
        if result.status == "mismatch":
            self.record_failure(result.vid, "mismatch", result.error or "mismatch")
            return
        now = int(self.clock())
        facts = result.facts or {}
        save = facts.get("save") or {}
        container = facts.get("container") or {}
        errors = facts.get("errors") or ([{"extractor": "decode", "error": result.error}] if result.error else [])
        t = result.timings_ms
        con = self._write_con()
        with con:
            con.execute("DELETE FROM version WHERE vid = ?", (result.vid,))
            con.execute(
                "INSERT INTO version (vid, status, schema, extractor, decoder, container, layout, data_bytes, "
                "json_bytes, decompressed_sha256, play_time, save_name, pid_in_data, primary_ctx, save_facts, "
                "errors, hash_ms, decode_ms, parse_ms, extract_ms, indexed_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (result.vid, result.status, facts.get("schema", SCHEMA_VERSION),
                 facts.get("extractor", EXTRACTOR_VERSION), DECODER_VERSION, container.get("kind"),
                 save.get("layout"), result.data_bytes, container.get("json_bytes"),
                 container.get("decompressed_sha256"), save.get("play_time_s"), save.get("save_name"),
                 save.get("pid_in_data"), facts.get("primary"),
                 _dumps({"save": facts.get("save"), "container": facts.get("container")}) if facts else None,
                 _dumps(errors), t.get("hash"), t.get("decode"), t.get("parse"), t.get("extract"), now, now))
            for ctx, cf in (facts.get("contexts") or {}).items():
                own = (cf.get("bases") or {}).get("own") or []
                galaxies = {b.get("galaxy") for b in own if b.get("type") == "HomePlanetBase" and b.get("galaxy") is not None}
                con.execute(
                    "INSERT INTO context (vid, ctx, facts, glyphs, own_bases, base_parts, ships, companions, "
                    "quicksilver, galaxies_with_bases) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (result.vid, ctx, _dumps(cf), cf.get("glyphs"), len(own), sum(b.get("parts", 0) for b in own),
                     len(cf.get("ships") or []), len(cf.get("companions") or []), cf.get("quicksilver"), len(galaxies)))
            con.execute("DELETE FROM failure WHERE vid = ?", (result.vid,))
        self._forget(result.vid)

    def record_failure(self, vid: str, kind: str, error: str) -> int:
        """Count a transient failure (unavailable, mismatch, worker-died, timeout); never facts."""
        now = int(self.clock())
        con = self._write_con()
        with con:
            con.execute(
                "INSERT INTO failure (vid, attempts, last_error, last_at, kind) VALUES (?, 1, ?, ?, ?) "
                "ON CONFLICT(vid) DO UPDATE SET attempts = attempts + 1, last_error = excluded.last_error, "
                "last_at = excluded.last_at, kind = excluded.kind", (vid, error[:200], now, kind))
        return self.failure_attempts(vid, con)

    def failure_attempts(self, vid: str, con: sqlite3.Connection | None = None) -> int:
        row = (con or self._reader()).execute("SELECT attempts FROM failure WHERE vid = ?", (vid,)).fetchone()
        return row[0] if row else 0

    def touch(self, vids: Iterable[str], now: int | None = None) -> None:
        """Mark rows as still known (a request named them), for the age-based clean-up."""
        stamp = int(self.clock() if now is None else now)
        con = self._write_con()
        with con:
            con.executemany("UPDATE version SET last_seen = ? WHERE vid = ?", [(stamp, v) for v in vids])

    def gc(self, max_age_s: int = GC_AGE_S) -> int:
        """Delete rows no request has named for ``max_age_s``; returns how many."""
        cutoff = int(self.clock()) - max_age_s
        con = self._write_con()
        with con:
            gone = [v for (v,) in con.execute("SELECT vid FROM version WHERE last_seen < ?", (cutoff,))]
            con.execute("DELETE FROM version WHERE last_seen < ?", (cutoff,))
        for v in gone:
            self._forget(v)
        return len(gone)

    def clear(self) -> None:
        """Owner "Rebuild content index": every row goes, every version is indexed again."""
        con = self._write_con()
        with con:
            con.execute("DELETE FROM version")
            con.execute("DELETE FROM failure")
        with self._lru_lock:
            self._lru.clear()

    def _forget(self, vid: str) -> None:
        with self._lru_lock:
            self._lru.pop(vid, None)

    def close(self) -> None:
        self._close_connections()
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def _close_connections(self) -> None:
        with self._connections_lock:
            for con in self._connections:
                con.close()
            self._connections.clear()
        self._writer = None
        self._writer_thread = None
        self._local = threading.local()


def _refuse_inside(path: Path, forbidden: Iterable[Path]) -> None:
    target = Path(path).resolve()
    for root in forbidden:
        root = Path(root).resolve()
        if target == root or root in target.parents:
            raise CacheLocationError("the content index cache must not live inside the vault or a save folder")
