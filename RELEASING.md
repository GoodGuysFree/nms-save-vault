# Releasing

How to cut a new versioned release of NMS Save Vault. Windows + PowerShell, with `uv` and
the GitHub CLI (`gh`) on PATH.

## 1. Bump the version

Update the version in all three places (keep them identical):

- `VERSION` (repo root) — the canonical marker
- `src/nms_save_vault/__init__.py` → `__version__`
- `pyproject.toml` → `[project] version`

Then in `README.md`: add a row to the **Version history** table and bump the
`NMSSaveVault-Setup-vX.Y.Z.zip` filename mentioned in QuickStart / Install.

## 2. Run the tests

```pwsh
uv run pytest          # or: & .venv\Scripts\python.exe -m pytest -q
```

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
python packaging\build_posix_kit.py all   # -> build\kits\NMSSaveVault-X.Y.Z-<os>-<arch>.tar.gz
```

Runs anywhere, Windows included: the bundled interpreter is repacked archive-to-archive, so
POSIX permissions and symlinks survive a build on a machine that has neither. The runtime is
downloaded from python-build-standalone, checked against the release's published `SHA256SUMS`,
and the build fails if it does not contain a usable Tcl/Tk **8.6** — the same Tk the Windows
kit ships, so a tester's GUI report is about the port and not about a Tk major version the app
has never run on.

Both kits are untested on real hardware; say so in the release notes until that changes.

Stage a versioned copy (the `releases/` folder is gitignored — local staging only):

```pwsh
New-Item -ItemType Directory -Force releases | Out-Null
Copy-Item dist\NMSSaveVault-Setup.zip releases\NMSSaveVault-Setup-vX.Y.Z.zip -Force
```

## 4. Commit & push (no binary)

Commit the version + README changes and push `main`:

```pwsh
git add VERSION pyproject.toml src/nms_save_vault/__init__.py README.md
git commit -m "release: vX.Y.Z"
git push origin main
```

## 5. Publish the GitHub Release (installer as an asset)

```pwsh
gh release create vX.Y.Z `
  releases\NMSSaveVault-Setup-vX.Y.Z.zip `
  -R GoodGuysFree/nms-save-vault `
  --target main `
  --title "vX.Y.Z" `
  --notes "<highlights>"
```

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
