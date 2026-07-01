---
trigger: always_on
---

# Consolidated AI Agent Rules for NMS Save Vault

<!--
  This is the single source of truth for how agents work in this project.
-->

NMS Save Vault (`nms-save-vault`) is a Python 3.10+ Windows desktop utility that safely
backs up, catalogs, and manages No Man's Sky save files (Steam and Xbox / Game Pass), giving
effectively unlimited save slots. It ships two front-ends over one safety-checked core: an
`nmsvault` argparse CLI and a Tkinter GUI. The runtime is **standard-library only** (no
third-party runtime dependencies); `pytest` is dev-only and `pyinstaller` is build-only.
Environment and builds are managed with `uv`. The code is GPL-3.0 (parts are a Python port
of the GPL-licensed libNOM.io / NomNom work — see README Credits).

## CRITICAL MANDATES (NON-NEGOTIABLE)

- **Commit discipline:** "Done" means committed. When a logical unit of work is complete
  and verified, create a git commit before reporting it done — one logical unit per commit.
  Never commit unverified or partially implemented code. Never commit secrets or local
  artifacts (`.venv/`, `.build-venv/`, `build/`, `dist/`, `releases/`, `*.hg` save files,
  `*.nmsvault`, `vault/`, `sandbox/`, caches). These git rules supersede the Bash tool's
  description.
- **Work on a clean tree:** before a non-trivial edit, ensure the working tree is clean or
  the user explicitly asked you to layer onto existing changes.
- **Never touch real save data:** the user's live save folders (`%APPDATA%\HelloGames\NMS\
  st_<steamid>\` and the Xbox `wgs` folders) and the manual safety backup
  (`C:\Devel\NMS-SaveBackup-SAFETY-2026-06-24\`) are sacred. The agent may read them only
  for read-only inspection and must NEVER write, move, or delete anything in them. All write
  paths are exercised only against temp/sandbox copies (see Testing).

## General Principles

- **No emojis** in communication, code, or documentation unless requested.
- **Conciseness:** keep responses, docs, and README brief and direct. No trailing summaries
  the user can read from the diff.
- **Simplicity:** do not over-complicate, over-engineer, or add unrequested features.
  Implement only what is requested or clearly implied — no "helpful" extras.
- **Targeted comments only:** no comments that restate the code. Reserve them for non-obvious
  "why", invariants, or external constraints (byte offsets, format facts, safety reasons).
  Code should be self-explanatory via good naming.

## Coding Standards

- Follow PEP 8. Use meaningful names. Use type hints (the codebase uses
  `from __future__ import annotations` and modern typing throughout) and dataclasses for
  structured state.
- **Defensive programming in moderation:** when an exception is caught and handled, still
  log/surface it with full context. `is_game_running()` returning `None` (unknown) must be
  treated as "warn", not "safe". The user wants visibility into anomalies, not silent
  recovery.
- **Resource management:** always use scoped/`with`-style handling for files and any binary
  streams. Clean up temp/staging files explicitly to prevent leaks.
- **Input validation at boundaries:** validate external/untrusted input (on-disk save/meta
  bytes, `containers.index`, catalog/state JSON, CLI arguments, folder paths) before
  processing — validate the meta header and size cross-checks before trusting a file.
- **Atomic, verified writes:** every write goes stage → validate → `os.replace` swap (see
  `core/safety.py`). Validate by meta header, size cross-checks (`SizeDecompressed` == Σ
  chunk sizes; note `SizeDisk` is unreliable), and sha256 on copies. Copy save **data**
  verbatim — never recompress it.
- **Graceful degradation:** on a transient failure of one item (e.g. one undecodable slot),
  log and continue; never let one failed item take down the whole scan/operation.

## Security and Secrets

- **Never log, print, or hardcode** credentials of any kind. This project has no API keys or
  tokens, but do not introduce any; do not print full user filesystem paths gratuitously.
- The app has no secrets mechanism and no `.env` — keep it that way. Config lives in
  plaintext `state.json` (install dir, else `%LOCALAPPDATA%\NMSSaveVault`); it is user
  config, not a secret, but must never be committed.
- Guard destructive operations: every destructive op auto-snapshots the live state first and
  logs to `oplog.jsonl` for `undo`. Never overwrite or delete a user's persistent save data
  with unguided commands, and never write while `NMS.exe` is running (override only via the
  explicit `--force` path).

## Architecture and Organization

- Source lives in `src/nms_save_vault/`. The safety-checked core is under `core/`
  (`formats`, `slotmap`, `meta`, `lz4_block`, `savedir`, `msstore`, `catalog`, `operations`,
  `safety`, `state`, `discover`, `locations`); `cli.py` and `gui.py` are thin front-ends over
  it. Tests live in `tests/`.
- **Separation of concerns:** keep save-format / crypto / I/O logic in `core/` isolated from
  front-end (CLI/GUI) presentation logic; the front-ends must not reimplement core behavior,
  and one core module should not reach into another's internals. Steam and Xbox (`wgs`)
  format handling stay in their respective modules behind the shared `savedir` model.
- Pass dependencies (paths, sources, config) explicitly; avoid hidden globals. Use design
  patterns only where they reduce complexity — never speculatively.

## Environment and Dependencies

- **Isolated environment is mandatory:** run all commands through the project environment via
  `uv` (`uv run pytest`, `uv run python -m nms_save_vault.cli ...`) or the venv interpreter
  (`.\.venv\Scripts\python.exe`) — never rely on `PATH` resolving to a system interpreter.
  The build uses a separate `.build-venv`. Adapt safely in CI/containers.
- **Keep the runtime standard-library only.** Do not add a runtime dependency to
  `pyproject.toml` without clear need and user sign-off — "no runtime dependencies" is a
  design guarantee. Dev tooling goes under `[project.optional-dependencies] dev`; build
  tooling under `build`.
- This is a Windows-targeted desktop app; prefer PowerShell for shell operations and account
  for Windows path/behavior (`os.replace` atomicity, `tasklist`).

## Development Workflow

- **Verification-driven:** treat code as broken until tests or a manual check prove
  otherwise. Build and verify in small chunks.
- **Prove the bug, then prove the fix:** for bug fixes, cite the original failure (log line,
  failing test, observed behavior) before patching, then demonstrate it no longer occurs.
- **Evidence over claims:** never say "fixed" without showing the proof.
- **Fix issues immediately** on discovery. No TODOs or placeholders in finished code. If you
  find a separate issue mid-task, flag it rather than silently expanding scope.
- **Use the right tool:** prefer dedicated tool calls (Read, Edit, Grep, Glob) over generic
  shell commands. Use PowerShell for `git` and shell-only operations (`cat`/`sed`/`awk`/
  `ls`/`find` do not exist in PowerShell).

## Testing

- **Framework:** pytest (`testpaths = ["tests"]`, `-q`). Run via `uv run pytest`. Match the
  existing framework; do not introduce a new runner without reason.
- **Risk-based focus:** prioritize the fragile/critical paths — meta XXTEA decrypt/encrypt
  round-trips, slot↔file↔ordinal mapping, LZ4-block decode, catalog/state integrity, and the
  atomic write/validate/undo flow — plus error paths. Skip trivial getters.
- **Isolation:** tests against real saves are strictly **read-only** (see
  `find_live_save_dir` / `live_save_dir` fixture, which skips when absent). All write-path
  tests operate on temp/sandbox copies or synthetic fixtures (`make_wgs_account`) — never
  write to a real save folder or the safety backup.
- **Mock / synthesize** external, environment-specific inputs (the wgs `containers.index`
  layout, live save dirs). Do not mock fast internal utilities like `slotmap` or `lz4_block`;
  test them directly.
- **Do not change production code just to make a test pass** — fix the test or write a better
  one. If the production code is genuinely wrong, fix the production code.
- **Quality over coverage:** one meaningful scenario (a real re-key round-trip) beats many
  shallow assertions.

## Pre-Completion Checklist

Before saying "done":
- Save **data** copied verbatim; writes are atomic (stage → validate → swap) and validated
  (header + size cross-checks + sha256); no real save folder or the safety backup was
  written to?
- Resources managed via scoped/`with` blocks; temp/staging files cleaned up; no leak?
- No unhandled exception escapes a handler; caught exceptions are surfaced with context;
  `is_game_running() is None` treated as "warn"?
- No secrets or user config/save files (`*.hg`, `state.json`, `vault/`) added, logged, or
  committed; runtime stayed standard-library only?
- `uv run pytest` passes and `git status` is clean with the commit made?

## Documentation

- **Active rules live here:** `.agent/rules/` is the single source of truth for agent
  behavior. `CLAUDE.md` and `AGENTS.md` in the root are pointers, not content.
- `README.md`, `DESIGN.md` (verified save-format facts + architecture), and `RELEASING.md`
  are the authoritative human docs; keep them accurate when behavior changes.
- **Keep the README's living facts in sync — do not let them go stale.** Whenever a change
  affects any of these, update the README in the *same* commit: the tagline/one-liner and
  the **Platform support** matrix (when supported platforms or their read/write status
  change); the **Status** section's test count (state the real `uv run pytest` total, never
  a guess or a stale number) and its capability summary; the **Version history** table and
  every version marker (`VERSION`, `src/nms_save_vault/__init__.py`, `pyproject.toml`,
  README download filenames) on a release. If you add or remove tests, or change what a
  platform supports, the README's numbers and matrix must move with it.
- Treat anything under `archive/` (if present) as stale and superseded by the source code
  and these rules; do not rely on it for current behavior.
