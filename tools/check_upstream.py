"""Has an upstream project moved since the pins were made? (dev-only; never imported by the app)

Asks the GitHub releases API for the latest releases of the two projects the save format and the
field-name hashes come from - zencq/libNOM.io and monkeyman192/MBINCompiler - and compares them
with the versions recorded in ``tools/upstream.json``. It prints what is new (tag, date, link)
and what to do next (DESIGN.md, "Maintenance after a game update"). It writes nothing unless
asked: ``--record`` stores the newest release of each project in ``upstream.json``.

    python tools/check_upstream.py
    python tools/check_upstream.py --record     # after the maintenance steps passed

Only the standard library is used (``urllib``); ``fetch`` is a parameter so tests never touch the
network.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

RECORD = Path(__file__).resolve().parent / "upstream.json"
API = "https://api.github.com/repos/{repo}/releases?per_page={n}"
PER_PAGE = 10

Fetch = Callable[[str], object]


def fetch_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "nmsvault-check-upstream"})
    with urllib.request.urlopen(request, timeout=20) as response:          # noqa: S310 - https only
        return json.load(response)


def latest_releases(repo: str, fetch: Fetch = fetch_json) -> list[dict]:
    """The repository's newest published releases (drafts left out), newest first:
    ``{"tag", "published" (YYYY-MM-DD), "url", "prerelease"}``."""
    data = fetch(API.format(repo=repo, n=PER_PAGE))
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("draft") or not item.get("tag_name"):
            continue
        out.append({"tag": str(item["tag_name"]), "published": str(item.get("published_at", ""))[:10],
                    "url": str(item.get("html_url", "")), "prerelease": bool(item.get("prerelease"))})
    return sorted(out, key=lambda r: r["published"], reverse=True)


def new_since(recorded: dict | None, releases: list[dict]) -> list[dict]:
    """The releases published after the recorded one (all of them when nothing is recorded)."""
    if not recorded or not recorded.get("tag"):
        return list(releases)
    tags = [r["tag"] for r in releases]
    if recorded["tag"] in tags:
        return releases[:tags.index(recorded["tag"])]
    return [r for r in releases if r["published"] > str(recorded.get("published", ""))]


def check(recorded: dict[str, dict], fetch: Fetch = fetch_json) -> dict[str, list[dict]]:
    """``repo -> new releases`` for every repository in ``recorded``."""
    return {repo: new_since(recorded.get(repo), latest_releases(repo, fetch)) for repo in recorded}


def report(found: dict[str, list[dict]]) -> list[str]:
    lines = []
    for repo, releases in found.items():
        if not releases:
            lines.append(f"{repo}: nothing new")
            continue
        lines.append(f"{repo}: {len(releases)} new")
        lines += [f"  {r['tag']}  {r['published']}{'  (pre-release)' if r['prerelease'] else ''}  {r['url']}"
                  for r in releases]
    if any(found.values()):
        lines.append("Next: nmsvault selftest, tools/gen_lookups.py, then bump the guard "
                     "(core/gameversion.py) - DESIGN.md, Maintenance after a game update.")
    return lines


def main(argv: list[str] | None = None, *, fetch: Fetch = fetch_json, record: Path = RECORD) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--record", action="store_true", help="store the newest release of each project")
    args = ap.parse_args(argv)
    recorded = json.loads(record.read_text(encoding="utf-8"))
    try:
        found = check(recorded, fetch)
    except OSError as exc:
        print(f"could not reach GitHub: {exc}", file=sys.stderr)
        return 2
    for line in report(found):
        print(line)
    if args.record:
        for repo in recorded:
            latest = latest_releases(repo, fetch)
            if latest:
                recorded[repo] = {"tag": latest[0]["tag"], "published": latest[0]["published"]}
        record.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
        print(f"recorded in {record.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
