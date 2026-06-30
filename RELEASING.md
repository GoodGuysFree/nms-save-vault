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
& packaging\build_exe.ps1            # -> dist\NMSSaveVault.exe  (always rebuilds, --clean)
& packaging\make_installer_zip.ps1   # -> dist\NMSSaveVault-Setup.zip
```

`make_installer_zip.ps1` **reuses an existing `dist\NMSSaveVault.exe` if present**, so always
run `build_exe.ps1` first to pick up code changes. Both scripts use an isolated `.build-venv`
so the dev `.venv` is untouched.

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
- The exe is unsigned, so Windows SmartScreen may warn on first run
  (*More info → Run anyway*).
