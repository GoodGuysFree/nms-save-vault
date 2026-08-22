"""Installing an update: what is downloaded, what is refused, and what gets swapped.

Nothing here touches the network, the real install folder, or PowerShell -- every one of
those is replaced. The one thing that is exercised for real is the file work: unpacking a
zip and the shape of the batch that does the swap.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from nms_save_vault import updates


# --- where a download is allowed to come from --------------------------------


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://github.com/GoodGuysFree/nms-save-vault/releases/download/v1/a.zip", True),
        ("https://objects.githubusercontent.com/blob/1", True),
        ("https://release-assets.githubusercontent.com/x", True),
        ("http://github.com/a.zip", False),          # plain HTTP is never acceptable
        ("https://github.evil.com/a.zip", False),    # a suffix trick on the host
        ("https://githubusercontent.com.evil/x", False),
        ("https://example.com/a.zip", False),
        ("", False),
        ("not a url", False),
    ],
)
def test_only_github_over_https_is_trusted(url, ok):
    assert updates.is_trusted_url(url) is ok


def test_download_refuses_an_untrusted_url(tmp_path):
    """The URL comes out of a JSON document, so it is data, not a decision this app made."""
    with pytest.raises(updates.UpdateInstallError):
        updates.download("https://example.com/evil.zip", tmp_path / "x.zip")


def test_a_redirect_off_github_is_refused():
    handler = updates._TrustedRedirect()
    with pytest.raises(updates.UpdateInstallError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://example.com/x.zip")


# --- reading the release ------------------------------------------------------


def _payload(**extra):
    return {"tag_name": "v9.9.9", "html_url": "https://github.com/o/r/releases/9", **extra}


def _fake_urlopen(payload):
    import json

    class Response:
        def read(self):
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return lambda request, timeout=None: Response()


def test_fetch_latest_picks_up_the_zip_asset(monkeypatch):
    url = "https://github.com/o/r/releases/download/v9.9.9/NMSSaveVault-Setup-v9.9.9.zip"
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _fake_urlopen(_payload(assets=[{"name": "NMSSaveVault-Setup-v9.9.9.zip",
                                        "browser_download_url": url, "size": 1234}])),
    )
    release = updates.fetch_latest()
    assert release.asset_url == url
    assert release.asset_name.endswith(".zip")
    assert release.asset_size == 1234
    assert release.has_asset


def test_a_release_with_no_zip_offers_no_install(monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _fake_urlopen(_payload(assets=[{"name": "notes.txt", "browser_download_url": "https://github.com/x"}])),
    )
    release = updates.fetch_latest()
    assert not release.has_asset
    assert updates.can_install(release)[0] is False


def test_an_asset_hosted_elsewhere_is_ignored(monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _fake_urlopen(_payload(assets=[{"name": "app.zip",
                                        "browser_download_url": "https://cdn.example.com/app.zip"}])),
    )
    assert not updates.fetch_latest().has_asset


# --- can this build replace itself? ------------------------------------------


def _fake_install(root: Path, version: str = "9.9.9") -> Path:
    """A folder shaped like the packaged app: launcher + _runtime/app/nms_save_vault."""
    root.mkdir(parents=True, exist_ok=True)
    (root / updates.LAUNCHER).write_bytes(b"MZ fake")
    pkg = root / "_runtime" / "app" / "nms_save_vault"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    return root


def test_portable_root_needs_the_whole_layout(tmp_path):
    good = _fake_install(tmp_path / "installed")
    assert updates.portable_root(str(good)) == good
    assert updates.portable_root("") is None
    assert updates.portable_root(str(tmp_path / "nope")) is None

    # A launcher on its own is not an install -- there is nothing to replace.
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / updates.LAUNCHER).write_bytes(b"MZ")
    assert updates.portable_root(str(bare)) is None


def test_running_from_source_says_so_plainly(monkeypatch):
    monkeypatch.delenv(updates.ROOT_ENV, raising=False)
    ok, why = updates.can_install()
    assert ok is False
    assert "source checkout" in why


# --- unpacking ----------------------------------------------------------------


def _release_zip(path: Path, version: str = "9.9.9", *, folder: str = updates.APP_FOLDER) -> Path:
    """A stand-in for the published installer zip: the app folder plus loose installer files."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{folder}/{updates.LAUNCHER}", "MZ fake")
        z.writestr(f"{folder}/_runtime/app/nms_save_vault/__init__.py", f'__version__ = "{version}"\n')
        z.writestr("install.bat", "@echo off\n")
        z.writestr("README.txt", "hello\n")
    return path


def test_unpack_returns_the_app_folder(tmp_path):
    app = updates.unpack(_release_zip(tmp_path / "r.zip"), tmp_path / "out")
    assert app.name == updates.APP_FOLDER
    assert (app / updates.LAUNCHER).is_file()
    assert updates.staged_version(app) == "9.9.9"


def test_a_zip_without_the_app_folder_is_refused(tmp_path):
    app = _release_zip(tmp_path / "r.zip", folder="SomethingElse")
    with pytest.raises(updates.UpdateInstallError, match="NMS Save Vault release"):
        updates.unpack(app, tmp_path / "out")


def test_a_corrupt_zip_is_an_error_not_a_crash(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip at all")
    with pytest.raises(updates.UpdateInstallError):
        updates.unpack(bad, tmp_path / "out")


@pytest.mark.parametrize("evil", ["../escaped.txt", "/abs.txt", "C:/abs.txt", "a/../../up.txt"])
def test_a_zip_that_writes_outside_the_folder_is_refused(tmp_path, evil):
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(evil, "pwned")
    with pytest.raises(updates.UpdateInstallError, match="unsafe path"):
        updates.unpack(path, tmp_path / "out")


def test_staged_version_of_a_tree_without_one_is_blank(tmp_path):
    (tmp_path / "empty").mkdir()
    assert updates.staged_version(tmp_path / "empty") == ""


# --- signature verification ---------------------------------------------------


def _powershell_says(monkeypatch, stdout: str, stderr: str = "", capture: dict | None = None):
    class Done:
        def __init__(self):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = 0

    def run(cmd, *a, **k):
        if capture is not None:
            capture["cmd"] = cmd
        return Done()

    monkeypatch.setattr(updates.subprocess, "run", run)


def test_a_psf_signed_launcher_passes(monkeypatch, tmp_path):
    _powershell_says(monkeypatch, "Valid\nCN=Python Software Foundation, O=Python Software Foundation\n")
    updates.verify_signature(tmp_path / "x.exe")  # must not raise


def test_the_check_does_not_inherit_another_powershells_variables(monkeypatch, tmp_path):
    """Regression: started from a PowerShell 7 session, the PS7 variables reached Windows
    PowerShell 5.1, which then could not load the module Get-AuthenticodeSignature lives in
    and returned nothing -- refusing every genuine release. Verified by hand: with those
    variables the check fails, without them it accepts the real signed launcher."""
    monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
    monkeypatch.setenv("PSExecutionPolicyPreference", "Bypass")
    monkeypatch.setenv("POWERSHELL_DISTRIBUTION_CHANNEL", "MSI")
    monkeypatch.setenv("PATH", "kept")

    env = updates._verify_env(tmp_path / "x.exe")

    assert not [k for k in env if k.upper().startswith("PS") or "POWERSHELL" in k.upper()]
    assert env["PATH"] == "kept"  # everything else is passed through
    assert env["NMSVAULT_VERIFY_PATH"] == str(tmp_path / "x.exe")


def test_the_check_bypasses_the_execution_policy(monkeypatch, tmp_path):
    """Regression: without this, PowerShell cannot autoload Microsoft.PowerShell.Security
    under the default Restricted policy, Get-AuthenticodeSignature is not found, and every
    single update is refused. It failed closed, but it failed on every genuine release."""
    seen: dict = {}
    _powershell_says(monkeypatch, "Valid\nCN=Python Software Foundation\n", capture=seen)
    updates.verify_signature(tmp_path / "x.exe")
    assert "-ExecutionPolicy" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("-ExecutionPolicy") + 1] == "Bypass"


def test_an_unsigned_launcher_is_refused(monkeypatch, tmp_path):
    _powershell_says(monkeypatch, "NotSigned\n\n")
    with pytest.raises(updates.UpdateInstallError, match="not validly signed"):
        updates.verify_signature(tmp_path / "x.exe")


def test_a_check_that_could_not_run_says_so_rather_than_judging_the_file(monkeypatch, tmp_path):
    """"unknown signature" reads like a verdict on the download; it is not."""
    _powershell_says(monkeypatch, "", stderr="the module could not be loaded")
    with pytest.raises(updates.UpdateInstallError, match="could not be checked"):
        updates.verify_signature(tmp_path / "x.exe")


def test_a_launcher_signed_by_someone_else_is_refused(monkeypatch, tmp_path):
    _powershell_says(monkeypatch, "Valid\nCN=Somebody Else Ltd\n")
    with pytest.raises(updates.UpdateInstallError, match="unexpected publisher"):
        updates.verify_signature(tmp_path / "x.exe")


def test_powershell_being_unavailable_is_an_error_not_a_pass(monkeypatch, tmp_path):
    """Failing open here would let an unverified binary through."""
    def boom(*a, **k):
        raise OSError("powershell not found")

    monkeypatch.setattr(updates.subprocess, "run", boom)
    with pytest.raises(updates.UpdateInstallError):
        updates.verify_signature(tmp_path / "x.exe")


# --- preparing the whole thing ------------------------------------------------


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """A full prepare() against local files: no network, no PowerShell, no real install."""
    install = _fake_install(tmp_path / "installed", version="0.0.1")
    source = _release_zip(tmp_path / "source.zip", version="9.9.9")

    def fake_download(url, dest, progress=None, timeout=None):
        Path(dest).write_bytes(source.read_bytes())
        if progress:
            progress(len(source.read_bytes()), len(source.read_bytes()))
        return Path(dest)

    monkeypatch.setattr(updates, "download", fake_download)
    monkeypatch.setattr(updates, "verify_signature", lambda exe: None)
    release = updates.Release(
        "9.9.9", "https://github.com/o/r/releases/9",
        asset_url="https://github.com/o/r/releases/download/v9.9.9/a.zip",
    )
    return release, install, tmp_path / "work"


def test_prepare_stages_and_writes_an_updater(staged):
    release, install, work = staged
    result = updates.prepare(release, install, workdir=work, pid=4242)
    assert result.version == "9.9.9"
    assert result.script.is_file() and result.script.suffix == ".cmd"
    assert (result.app_dir / updates.LAUNCHER).is_file()
    # Nothing in the install folder has been touched yet.
    assert updates.staged_version(install) == "0.0.1"


def test_prepare_reports_progress(staged):
    release, install, work = staged
    seen = []
    updates.prepare(release, install, workdir=work, pid=4242, progress=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1][0] > 0


def test_a_download_that_is_not_the_promised_version_is_refused(staged, monkeypatch):
    """A mismatched build is either the wrong asset or a corrupt download; either way it
    must not be installed."""
    release, install, work = staged
    wrong = updates.Release(
        "8.8.8", "https://github.com/o/r/releases/8",
        asset_url="https://github.com/o/r/releases/download/v8.8.8/a.zip",
    )
    with pytest.raises(updates.UpdateInstallError, match="says it is version 9.9.9"):
        updates.prepare(wrong, install, workdir=work, pid=4242)


def test_prepare_verifies_the_signature_before_writing_the_script(staged, monkeypatch):
    release, install, work = staged
    monkeypatch.setattr(
        updates, "verify_signature",
        lambda exe: (_ for _ in ()).throw(updates.UpdateInstallError("bad signature")),
    )
    with pytest.raises(updates.UpdateInstallError, match="bad signature"):
        updates.prepare(release, install, workdir=work, pid=4242)
    assert not list(work.parent.glob("*.cmd"))


def test_prepare_refuses_a_release_with_nothing_to_download(staged):
    _, install, work = staged
    bare = updates.Release("9.9.9", "https://github.com/o/r/releases/9")
    with pytest.raises(updates.UpdateInstallError):
        updates.prepare(bare, install, workdir=work, pid=4242)


# --- the updater script -------------------------------------------------------


@pytest.fixture
def script(tmp_path):
    return updates.updater_script(
        pid=4242,
        src=tmp_path / "work" / "unpacked" / "NMSSaveVault",
        dst=tmp_path / "installed",
        work=tmp_path / "work",
        log=tmp_path / "u.log",
    )


def test_the_script_waits_for_the_app_to_exit_before_touching_anything(script):
    """The whole reason the swap is handed off: the running app holds its own files."""
    wait = script.index(":wait")
    swap = script.index("xcopy")
    assert wait < swap
    # Windows will not delete the image of a running process, so a delete that fails is
    # proof the app is still up. Checked this way because it can only fail safe -- see
    # test_the_wait_cannot_fail_open below.
    assert 'del /f /q "%DST%\\%EXE%"' in script
    assert 'if not exist "%DST%\\%EXE%" goto ready' in script


def test_the_wait_cannot_fail_open(script):
    """Regression: the first version asked tasklist whether the process was alive, through
    a pipe. When the pipe could not run, the check reported "not running" and the script
    happily swapped the files underneath a live app. Nothing in the wait may depend on a
    helper program answering correctly."""
    wait = script[script.index(":wait"):script.index(":ready")]
    for fragile in ("tasklist", "find ", "|", "wmic", "powershell"):
        assert fragile not in wait, f"the wait must not depend on {fragile!r}"


def test_the_launcher_is_backed_up_before_it_is_used_as_the_gate(script):
    """The gate deletes the launcher, so it has to be restorable if a later step fails."""
    backup = script.index('copy /y "%DST%\\%EXE%" "%BACKUP%"')
    assert backup < script.index(":wait")
    after_ready = script[script.index(":ready"):]
    assert after_ready.count('copy /y "%BACKUP%" "%DST%\\%EXE%"') >= 2  # both failure paths


def test_the_script_moves_the_old_runtime_aside_rather_than_deleting_it(script):
    assert 'ren "%DST%\\_runtime" "_runtime.old"' in script
    # ... and puts it back if the copy fails.
    rollback = script.index(":rollback")
    assert 'ren "%DST%\\_runtime.old" "_runtime"' in script[rollback:]


def test_the_script_relaunches_the_app_on_every_path(script):
    """Success, rollback, and a failed rename all have to leave the user with a program."""
    assert script.count('start "" "%DST%\\%EXE%"') >= 3


def test_the_script_never_names_the_config_files(script):
    """state.json and accounts.ini are not in the zip and must not be copied or removed;
    the only mention of them is the comment saying so."""
    body = "\n".join(line for line in script.splitlines() if not line.strip().startswith("rem"))
    assert "state.json" not in body
    assert "accounts.ini" not in body


def test_the_script_gives_up_rather_than_swapping_a_running_app(script):
    give_up = script.index("timed out waiting")
    swap = script.index("xcopy")
    assert give_up < swap, "the timeout branch must come before any file is touched"
    assert "goto cleanup" in script


# --- tidying up ---------------------------------------------------------------


def test_clean_temp_removes_old_work_but_keeps_this_run_and_recent_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(updates.tempfile, "gettempdir", lambda: str(tmp_path))
    old_dir = tmp_path / f"{updates.TEMP_PREFIX}111"
    old_dir.mkdir()
    (old_dir / "junk.txt").write_text("x", encoding="utf-8")
    old_script = tmp_path / f"{updates.TEMP_PREFIX}111.cmd"
    old_script.write_text("@echo off", encoding="utf-8")
    fresh_log = tmp_path / f"{updates.TEMP_PREFIX}111.log"
    fresh_log.write_text("what happened", encoding="utf-8")
    mine = tmp_path / f"{updates.TEMP_PREFIX}999"
    mine.mkdir()
    unrelated = tmp_path / "something-else"
    unrelated.mkdir()

    updates.clean_temp(pid=999)

    assert not old_dir.exists()
    assert not old_script.exists()
    assert fresh_log.exists(), "the log is what a failed install told the user to read"
    assert mine.exists()
    assert unrelated.exists()


def test_clean_temp_eventually_takes_the_logs_too(tmp_path, monkeypatch):
    monkeypatch.setattr(updates.tempfile, "gettempdir", lambda: str(tmp_path))
    stale = tmp_path / f"{updates.TEMP_PREFIX}111.log"
    stale.write_text("old news", encoding="utf-8")
    updates.clean_temp(pid=999, keep_logs_for=-1)
    assert not stale.exists()


def test_clean_temp_survives_an_unreadable_temp_dir(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(updates.Path, "glob", boom)
    updates.clean_temp(pid=1)  # must not raise


# --- the GUI side -------------------------------------------------------------


def test_the_banner_hides_install_when_it_would_not_work(gui_app, monkeypatch):
    """Running from source there is nothing to replace, so only the download page is
    offered -- a button that can only apologise is worse than no button."""
    monkeypatch.setattr(updates, "can_install", lambda release=None: (False, "from source"))
    gui_app._show_update_banner(updates.Release("9.9.9", "https://example/rel"))
    assert not gui_app.install_button.winfo_manager()
    gui_app._hide_banner()


def test_the_banner_offers_install_when_it_would_work(gui_app, monkeypatch):
    monkeypatch.setattr(updates, "can_install", lambda release=None: (True, ""))
    gui_app._show_update_banner(updates.Release("9.9.9", "https://example/rel"))
    assert gui_app.install_button.winfo_manager()
    gui_app._hide_banner()


def test_declining_the_confirmation_downloads_nothing(gui_app, monkeypatch):
    from nms_save_vault import gui

    monkeypatch.setattr(updates, "can_install", lambda release=None: (True, ""))
    monkeypatch.setattr(gui, "_askyesno", lambda *a, **k: False)
    monkeypatch.setattr(updates, "prepare", lambda *a, **k: pytest.fail("must not download"))
    monkeypatch.setattr(updates, "launch", lambda *a, **k: pytest.fail("must not install"))
    gui_app._release = updates.Release(
        "9.9.9", "https://example/rel", asset_url="https://github.com/a.zip"
    )
    gui_app.on_install_update()


def test_install_without_a_check_first_says_so(gui_app, monkeypatch):
    from nms_save_vault import gui

    shown = []
    monkeypatch.setattr(gui, "_showinfo", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(updates, "launch", lambda *a, **k: pytest.fail("must not install"))
    if hasattr(gui_app, "_release"):
        del gui_app._release
    gui_app.on_install_update()
    assert shown


def test_the_window_title_carries_the_version(gui_app):
    from nms_save_vault import __version__

    assert __version__ in gui_app.title()
