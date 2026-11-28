# Releasing

How to cut a new versioned release of NMS Save Vault. Windows + PowerShell, with `uv` and
the GitHub CLI (`gh`) on PATH.

## 0. Check which game is installed

```pwsh
F:\NMS-Unpack\.venv\Scripts\python.exe F:\NMS-Unpack\scripts\installed.py --check
```

This is the owner's game-files workspace. It is read-only towards both game installs. Exit 0
means `F:\NMS-Unpack\builds\INSTALLED.json` is current. Exit 1 means an install changed: run
it again without `--check` (about 2 minutes) to refresh it. The file gives the Steam build, the
Xbox package version and `same_release`, which says whether both installs are the same game
release (the pak contents are compared file by file). A release note about either platform's
game version comes from this file, not from which saves happen to exist.

## 1. Bump the version

Update the version in all three places (keep them identical):

- `VERSION` (repo root) — the canonical marker
- `src/nms_save_vault/__init__.py` → `__version__`
- `pyproject.toml` → `[project] version`

Then in `README.md`: add a row to the **Version history** table and bump the
`NMSSaveVault-Setup-vX.Y.Z.zip` filename mentioned in QuickStart / Install.

Then `CHANGELOG.md`: rename its `## Unreleased` section to `## X.Y.Z - YYYY-MM-DD` and start a
new empty `## Unreleased` above it. Each top-level bullet is one short highlight (at most 60
characters, at most 7 of them), and the indented lines under it add the detail.

And in `src/nms_save_vault/ui/whatsnew.py`: set `NOTES_VERSION` to the new version and copy that
section's top-level bullets, word for word, into `NOTES`. The app pops them up once, the first
time this version starts. `tests/test_whatsnew.py` fails until `NOTES_VERSION` matches, the
bullets match the changelog's, and each one is short. The kits ship `CHANGELOG.md` beside the
program, so commit it before building.

## 2. Run the tests, then the kill tests

Tell the Savecraft agent first, and run nothing while its tests run (GGF 2026-11-28: neither
app's testing may affect the other's). Both runs go on the hidden desktop:

```pwsh
uv run python C:\Devel\NMSSavecraft\tools\hidden_desktop.py uv run pytest -n 12
uv run python C:\Devel\NMSSavecraft\tools\hidden_desktop.py uv run pytest -m killtest -n 12
```

The kill tests (`tests/test_kill_safety.py`, about three minutes) run each restore and backup
in a child process and end it the way Task Manager does, at every step it takes and at random
moments, then check what the next start makes of it: the save folder wholly as it was or
wholly as the operation leaves it, nothing settled in silence, no temporary file left, the
vault whole. They are not part of the everyday run. **They gate the release** (GGF 2026-11-28:
"It needs to run every time before we release. We cannot compromise on the user's file
safety"): a run of all of them that passes writes `build/killtest-pass.json`, the digest of
the package as tested, and `build_core.py` and `make_installer_zip.ps1` refuse while the
package differs from it. `uv run python packaging/release_gate.py check` says where it stands.
A kit only for testing by hand is built with `make_installer_zip.ps1 -TestKit` and is never
released.

## 3. Build the installer kit

```pwsh
& packaging\build_portable.ps1       # -> dist\NMSSaveVault\  (always rebuilt from scratch)
& packaging\make_installer_zip.ps1   # -> dist\NMSSaveVault-Setup.zip
```

`make_installer_zip.ps1` **reuses an existing `dist\NMSSaveVault\` if present**, so always run
`build_portable.ps1` first to pick up code changes. Neither needs a build environment: the
Python runtime is downloaded from python.org and cached under `build\runtime-cache\`, and the
build fails if any part of it is not validly signed by the Python Software Foundation.

## 3b. Build the Linux and macOS kits

```pwsh
python packaging\build_posix_kit.py all   # -> build\kits\NMSSaveVault-vX.Y.Z-<os>-<arch>.tar.gz
```

Runs anywhere, Windows included: the bundled interpreter is repacked archive-to-archive, so
POSIX permissions and symlinks survive a build on a machine that has neither. The runtime is
downloaded from python-build-standalone, checked against the release's published `SHA256SUMS`,
and the build fails if it does not contain a usable Tcl/Tk **8.6** — the same Tk the Windows
kit ships, so a tester's GUI report is about the port and not about a Tk major version the app
has never run on.

Both kits are untested on real hardware; say so in the release notes until that changes.

## 3c. Build the core library wheel

```pwsh
uv run python packaging\build_core.py   # -> build\core\nms_save_vault_core-X.Y.Z-py3-none-any.whl + its sha256
```

The core alone (`nms_save_vault/__init__.py` and `nms_save_vault/core/`), for apps that share
the vault and bundle it (NMS Savecraft). It is reproducible: the same commit builds the same
bytes anywhere, so put the printed SHA-256 in the release notes and an app can pin it. It
refuses to build while `src/nms_save_vault` differs from the commit. If
`core/api.py` lost a name or changed what one means, `API_VERSION` must have gone up
(`tests/test_core_api.py`); say so in the notes.

Stage a versioned copy (the `releases/` folder is gitignored — local staging only):

```pwsh
New-Item -ItemType Directory -Force releases | Out-Null
Copy-Item dist\NMSSaveVault-Setup.zip releases\NMSSaveVault-Setup-vX.Y.Z.zip -Force
Copy-Item build\kits\NMSSaveVault-vX.Y.Z-*.tar.gz releases\ -Force
Copy-Item build\core\nms_save_vault_core-X.Y.Z-py3-none-any.whl releases\ -Force
```

## 4. Merge, commit & push (no binary)

If the work was done on a feature branch, merge it into `main` first (`git switch main`,
`git merge <branch>`), then commit the version + README changes and push `main`:

```pwsh
git add VERSION pyproject.toml src/nms_save_vault/__init__.py README.md
git commit -m "release: vX.Y.Z"
git push origin main
```

## 5. Publish the GitHub Release (installer as an asset)

```pwsh
gh release create vX.Y.Z `
  releases\NMSSaveVault-Setup-vX.Y.Z.zip `
  releases\NMSSaveVault-vX.Y.Z-linux-x86_64.tar.gz `
  releases\NMSSaveVault-vX.Y.Z-macos-arm64.tar.gz `
  releases\nms_save_vault_core-X.Y.Z-py3-none-any.whl `
  -R GoodGuysFree/nms-save-vault `
  --target main `
  --title "vX.Y.Z" `
  --notes "<highlights>"
```

The Windows zip must stay the only `.zip` asset (the in-app updater takes the first one);
the core library is a `.whl` for that reason.
The asset filename becomes the public download name, so keep the `-vX.Y.Z` suffix. The
QuickStart link in the README points at `/releases/latest`, so it always resolves to the
newest release automatically.

## Notes

- Binaries are distributed **only** as GitHub Release assets, never committed — `releases/`
  and `dist/` are gitignored. This keeps the repo lean.
- `NMSSaveVault.exe` is a verbatim renamed copy of the Authenticode-signed CPython
  `pythonw.exe`, so Windows raises no unknown-publisher warning. That is the Python
  runtime's own signature, not a signature over this project's code — keep the release
  notes accurate about the difference.
