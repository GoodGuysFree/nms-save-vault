"""``nmsvault-cli selftest``: does this app still understand the saves it is looking at?

Read-only. Every live save (Steam and Xbox) and every vault version is read and decoded, and the
content index's extractors run on the decoded tree in memory - no cache, no organizer store, no
vault file is written. The result is counts only (never an id, a name or a path), so it can be
pasted into a bug report as it is. Run it after a game update (DESIGN.md, "Maintenance after a
game update"); an exit of 1 means something needs an app update.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import gameversion, savedir, savejson
from .hashname import key
from .index import extract
from .organizer import Organizer
from .state import Source

#: The top-level names of a save the app knows (their hashed keys are what a save holds). A key
#: that matches none of these is a field the game added since the pins were made.
PINNED_TOP_LEVEL = ("Version", "Platform", "ActiveContext", "CommonStateData", "BaseContext",
                    "ExpeditionContext", "PlayerStateData", "DiscoveryManagerData",
                    "SpawnStateData", "GameKnowledgeData")
PINNED_KEYS = frozenset(key(name) for name in PINNED_TOP_LEVEL)


@dataclass
class SelfTestReport:
    saves: int = 0                 # distinct saves looked at (live and vault)
    live: int = 0
    vault: int = 0
    decoded_ok: int = 0
    unsupported_container: int = 0
    newer_format: int = 0
    unknown_top_level_saves: int = 0       # saves holding a key that matches no pinned name
    unknown_top_level_keys: int = 0        # distinct such keys over everything
    extractor_errors: int = 0              # saves whose extractors reported an error
    damaged: int = 0                       # data that does not decode at all (not an update issue)
    unreadable: int = 0                    # a meta, an index or a file that does not read (a
                                           # new format looks like this): an update issue
    _unknown: set = field(default_factory=set, repr=False)

    @property
    def needs_update(self) -> bool:
        return bool(self.unsupported_container or self.newer_format or self.unknown_top_level_saves
                    or self.extractor_errors or self.unreadable)

    @property
    def exit_code(self) -> int:
        return 1 if self.needs_update else 0

    def lines(self) -> list[str]:
        """The report: one count per line, nothing else."""
        return [
            f"saves looked at: {self.saves} ({self.live} live, {self.vault} in the vault)",
            f"decoded OK: {self.decoded_ok}",
            f"unsupported container: {self.unsupported_container}",
            f"newer game format: {self.newer_format}",
            f"unknown top-level keys: {self.unknown_top_level_keys} in "
            f"{self.unknown_top_level_saves} saves",
            f"extractor errors: {self.extractor_errors}",
            f"damaged (not an update issue): {self.damaged}",
            f"unreadable (a meta, index or file that does not read): {self.unreadable}",
            "result: needs an app update" if self.needs_update else "result: clean",
        ]


def check_save(report: SelfTestReport, platform: str, data: bytes, *, meta_format: int,
               base_version: int, bare_size_hint: int | None) -> None:
    """One save: decode it, run the extractors in memory, count the outcome."""
    if gameversion.newer(platform, meta_format, base_version):
        report.newer_format += 1                          # still decoded, best effort
    try:
        decoded = savejson.decode_save(data, bare_size_hint=bare_size_hint or None)
    except savejson.SaveDecodeError as exc:
        if exc.kind == "unsupported":
            report.unsupported_container += 1
        else:
            report.damaged += 1
        return
    except Exception:  # noqa: BLE001 - a selftest never stops on one save
        report.extractor_errors += 1
        return
    unknown = {k for k in decoded.root if k not in PINNED_KEYS}
    if unknown:
        report.unknown_top_level_saves += 1
        report._unknown |= unknown
        report.unknown_top_level_keys = len(report._unknown)
    try:
        facts = extract.run_extractors(decoded)
    except Exception:  # noqa: BLE001
        report.extractor_errors += 1
        return
    if facts.get("errors"):
        report.extractor_errors += 1
    elif not unknown:
        report.decoded_ok += 1


def run(vault_root: Path | None, sources: Sequence[Source], *,
        progress: Callable[[int], None] | None = None) -> SelfTestReport:
    """Look at every live save of ``sources`` and every version in ``vault_root``."""
    report = SelfTestReport()
    seen: set[str] = set()
    folders = 0
    for source in sources:
        folder = Path(source.path)
        if not folder.is_dir():
            continue
        folders += 1
        try:
            view = savedir.scan_any(folder)
        except Exception:  # noqa: BLE001 - a damaged or unknown index is a finding, not a crash
            report.unreadable += 1
            continue
        platform = "xbox" if savedir.platform_of(folder) == "xbox" else "steam"
        for slot in view.slots.values():
            for member in slot.members:
                if not member.exists:
                    continue
                if member.info is None or not member.info.valid:
                    report.unreadable += 1            # its meta does not read: not "nothing"
                    continue
                try:
                    data = Path(member.data_path).read_bytes()
                except OSError:
                    report.unreadable += 1
                    continue
                report.saves += 1
                report.live += 1
                seen.add(_sha(data))
                check_save(report, platform, data, meta_format=member.info.meta_format,
                           base_version=member.info.base_version,
                           bare_size_hint=member.info.size_decompressed)
                if progress:
                    progress(report.saves)
    if vault_root is not None and Path(vault_root).is_dir():
        _vault(report, Path(vault_root), seen, progress)
    if folders and report.saves == 0 and not report.unreadable:
        report.unreadable += folders                  # live folders and nothing looked at: not clean
    return report


def _vault(report: SelfTestReport, root: Path, seen: set[str],
           progress: Callable[[int], None] | None) -> None:
    organizer = Organizer(root, [], read_only=True, machine_cache=False,
                          game_running=lambda: None)
    organizer.refresh(record=False, request_facts=False)
    scan, locator = organizer.scan, organizer.locator
    if scan is None or locator is None:
        return
    for vid, info in sorted(scan.versions.items()):
        if vid in seen:
            continue
        try:
            opened = locator.open(vid)
        except Exception:  # noqa: BLE001 - a version that cannot be opened is counted, not shown
            report.unreadable += 1
            continue
        seen.add(vid)
        report.saves += 1
        report.vault += 1
        meta = info.meta
        check_save(report, opened.platform, opened.data,
                   meta_format=meta.meta_format if meta else 0,
                   base_version=meta.base_version if meta else 0,
                   bare_size_hint=meta.size_decompressed if meta else None)
        if progress:
            progress(report.saves)


def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()
