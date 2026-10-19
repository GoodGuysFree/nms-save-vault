# R3 - UI technology options for the save-file organizer

Research wave 1, topic R3 (feeds S7). Branch `design/save-organizer`, 2026-10-16. Research only:
no production code, nothing under `src/`, `tests/`, `packaging/` changed.

Evidence tags: **VERIFIED** = measured or tested here, with the command in Appendix A;
**SOURCED** = cited (file:line or URL); **INFERRED** = reasoning, not tested.

All prototypes are throwaway code in the session scratchpad, outside the repo (written `<scratch>/R3/`
below). Every prototype ran on **synthetic data**: no save file, vault entry or app config of the
owner was read or written. The screenshots were removed from the repo (they showed real save names); see the app.

---

## 1. TL;DR

1. Both a **Tk organizer (T1)** and a **local web UI on the standard library (T2)** can do every
   organizer need on today's runtime. Both were built as throwaway prototypes (42 save-files /
   1,413 versions / 200-node history graph) and stress-tested (300 / 11,954 / 2,000).
2. The kit runtime is CPython 3.12.10 + Tcl/Tk 8.6.15 on Windows and python-build-standalone (PBS)
   3.12.12 + Tk 8.6.14 on Linux/macOS. It includes sqlite3 3.49.1 (FTS5, JSON). `http.server` and
   `multiprocessing` both work from the real signed launcher, and `webbrowser` imports (VERIFIED;
   `webbrowser.open` was deliberately not run against the owner's browser).
3. **T1 is fast but hits a looks ceiling.** A 12k-row tree populates in 148 ms, a 2k-node Canvas
   graph draws in 48 ms, and in-app drag-and-drop works without tkdnd. But Tk 8.6 has no per-cell
   styling (a tag styles the whole row) and allows an image only in column #0. Every new ttk widget
   needs dark-theme work: the Notebook tabs came out unreadable.
4. **The ASCII rule cannot protect user data.** The Linux kit's Tk has no Xft, both the shipped
   8.6.14 and the newer PBS Tk 9.0.4 (VERIFIED). So notes and ship / companion / base names
   outside Latin-1 garble on Linux under any Tk option. Windows Tk renders CJK, Hebrew (in RTL
   order) and emoji (VERIFIED).
5. **The shipped GUI is DPI-unaware**, so Windows stretches it into a blurry bitmap at 150 %. The
   launcher manifest has no `dpiAware`, and awareness measured from the real launcher is 0. The
   fix is `SetProcessDpiAwareness(1)` plus scaling every pixel constant
  . This applies with or without the organizer.
6. **T2 works on the stdlib and is secure enough.** It uses ThreadingHTTPServer, JSON and
   hand-written JS/SVG. All 13 security checks passed (VERIFIED), covering: token -> HttpOnly
   SameSite=Strict cookie; Host / Origin / Sec-Fetch-Site checks; a required custom header;
   JSON-only POST; no CORS; strict CSP. The server starts in 12 ms and exits about 6 s after the
   last page closes (VERIFIED).
7. **Gotcha:** under the GUI launcher (pythonw) `sys.stderr` is None, and `http.server`'s default
   `log_message` then kills every response (`RemoteDisconnected`). It must be overridden (VERIFIED).
8. **T2 is fast at real scale** (VERIFIED): the SVG graph draws 200 nodes in 11 ms and 2,000 in
   62 ms; 242 table rows render in 26 ms. A naive full re-render of 12k rows takes about 1 s, so
   large histories need lazy expansion or virtualization.
9. **Side effect during this research:** Edge launched with a private `--user-data-dir` (for an
   app window or headless screenshots) silently signed into the Windows user's Microsoft account
   and started sync (VERIFIED). The profiles were deleted. A T2 app-window mode needs a
   sign-in-free route proven first; the default browser avoids the problem.
10. The POSIX kit packager ships only `*.py` (`packaging/build_posix_kit.py:137-147`). Static web
    assets therefore need a packager change or must be embedded as `.py` modules. The Windows
    packager already copies the package folder recursively.
11. **T3 third-party toolkits break the stdlib guarantee and bloat the kit.** PySide6 adds 118 MB
    compressed on Windows, against a 14.6 MB Setup zip today. wxPython has no Linux wheels on
    PyPI. pywebview and Toga need pythonnet + .NET on Windows and system GTK/WebKit on Linux.
    Flet downloads its client on first run. WebView2 via ctypes is feasible but not sensible.
12. **Recommendation:** full designs with mockups for **T1-A** (Tk organizer + DPI/theme pass) and
    **T2-A** (local web UI hosted by the app, opened in the default browser, Tk kept as host).
    Lean T2-A if Unicode notes, graph polish and Linux matter most; lean T1-A if one language and
    no local listener matter most. Drop T3 and WebView2-ctypes.

---

## 2. Findings

### 2.1 Constraints

**K1. Runtime guarantee (SOURCED).** The runtime is standard-library only. `pyproject.toml` has
`dependencies = []` ("Core is intentionally standard-library only").
`.agent/rules/consolidated.md:111` says "Keep the runtime standard-library only ... a design
guarantee", and DESIGN.md D6 says the same. Under D13 (GitHub Releases only) the self-contained
kit is the only way users get the app, so whatever the UI needs must be inside that kit. The README
says the update check "is the only thing the app uses the network for" (`README.md:329`). A
loopback server (T2) would need that sentence reworded.

**K2. Windows kit (SOURCED `packaging/build_portable.ps1:24,78-79,94-95,106-139`; VERIFIED
probe).** The kit is assembled from:
- the python.org embeddable 3.12.10 amd64 zip;
- `tcltk.msi` from the same release, which supplies Tkinter;
- PSF-signed `pythonw.exe` / `python.exe`, renamed to `NMSSaveVault.exe` / `nmsvault-cli.exe`;
- the stdlib inside `python312.zip`;
- the app copied recursively into `_runtime/app`.

The probe ran inside the built kit (`NMSVAULT_NO_BOOTSTRAP=1`, Appendix A) and in the dev venv,
with identical results:

| Item | Result |
|---|---|
| Python | 3.12.10 (MSC v.1943 AMD64) |
| Tcl/Tk | 8.6.15 |
| sqlite3 | 3.49.1, compile options FTS3/FTS4/FTS5, JSON (`json_extract` works), RTREE, MATH_FUNCTIONS, THREADSAFE=1 |
| Imports OK | tkinter, ttk, sqlite3, multiprocessing, concurrent.futures, http.server, socketserver, webbrowser, ssl, ctypes, asyncio, secrets, hmac, wsgiref, xml.etree, zipfile, urllib.request, html, mimetypes |
| Extension DLLs present | `_sqlite3.pyd` + `sqlite3.dll`, `_multiprocessing.pyd`, `_socket.pyd`, `select.pyd`, `_ssl.pyd`, `_ctypes.pyd` (+ libffi), `_asyncio.pyd`, `_overlapped.pyd`, `_tkinter.pyd` + `tcl86t/tk86t.dll` |
| Size | `dist/NMSSaveVault-Setup.zip` 14,564,481 B (14.6 MB); unpacked kit about 31 MB |

**K3. POSIX kits (SOURCED `packaging/build_posix_kit.py:32-57`; VERIFIED tar scan).** These use
PBS `3.12.12+20251217` install_only_stripped. Tk 8.6 is pinned on purpose: PBS releases from
20260203 onward link 3.12 against Tk 9.0. The v0.2.8 tarballs are 32.3 MB (Linux) and 16.7 MB
(macOS). `app_sources()` packs only `*.py` files (`build_posix_kit.py:137-147`), so any non-Python
asset is silently left out of Linux/macOS kits.

**K4. The ASCII rule versus data (SOURCED DESIGN.md:200-216, `tests/test_ui_text.py`; VERIFIED).**
- **Linux, shipped Tk.** An in-memory scan of the shipped Linux runtime found `libtk8.6.so`
  (Tk 8.6.14, 2.9 MB). It links only libc, libdl, libm and libpthread, and contains no `Xft` or
  `FcPattern` strings.
- **Linux, newer PBS.** The newer cached runtime `3.12.14+20260901` ships Tk **9.0.4**
  (`libtcl9tk9.0.so`, 3.1 MB, which also needs libicuuc and libnotify). It also has no Xft. So a
  Tk 9 bump would not fix non-Latin-1 text on the Linux kit.
- **Windows.** Tk 8.6.15 drew CJK, Hebrew (correct right-to-left visual order), em dash, ellipsis,
  bullet/square and a non-BMP emoji (monochrome) in a `tk.Text`
 .
- **What the test covers.** `test_ui_text.py` polices only the app's own string literals. Save
  names, notes, and ship / companion / base names are user data, which the organizer will show far
  more of. This is R2's Q9 (notes charset).

**K5. DPI (VERIFIED; R2 found the same).**
- Both kit launchers carry the stock CPython manifest: `asInvoker`, `longPathAware`,
  common-controls 6. There is **no `dpiAware` / `dpiAwareness`**, and `src/` never calls
  `SetProcessDpiAwareness` (grep).
- Launched like a double-click from a copy of the kit, the process reports
  `GetAwarenessFromDpiAwarenessContext` = 0 (unaware). Tk reports scaling 1.334 at 96 dpi on this
  3840x2160 display at 150 % (144 dpi).
- With awareness on, Tk switches to scaling 2.0 by itself. Point-sized fonts follow, but pixel
  constants (column widths, paddings, `wraplength`, image sizes) do not, so each needs multiplying
  by `winfo_fpixels('1i') / 96`.
- The (removed) DPI comparison screenshot showed the same view three ways: unaware (blurry); aware only (crisp,
  but the date column truncates and the button falls off); aware with scaled constants (correct).

**K6. pythonw has no standard streams (VERIFIED, kit clone launched the way Explorer launches
it).**
- `sys.stdout` and `sys.stderr` are None.
- A ThreadingHTTPServer with the default handler fails every request with
  `RemoteDisconnected('Remote end closed connection without response')`.
- Cause: `BaseHTTPRequestHandler.log_message` writes to `sys.stderr` inside `send_response`.
- Overriding `log_message` gives 200 OK.
- Launched from a shell, the same exe inherits pipes and works, which hides the bug in casual
  testing.

**K7. multiprocessing in the kit (VERIFIED).**
- From the real `NMSSaveVault.exe`, a spawn `Pool(2).map` returned correct results in 90-121 ms.
- Children start as `NMSSaveVault.exe -I -c "from multiprocessing.spawn import spawn_main; ..."
  --multiprocessing-fork`, and the bootstrap's `--multiprocessing-fork` bypass
  (`packaging/sitecustomize.py:126`) lets them through.
- So CPU-heavy indexing (R5) can run outside the UI's GIL under either T1 or T2.
- Side issue (VERIFIED): the `-c` / `-m` bypass at `sitecustomize.py:125-127` never fires.
  - At `sitecustomize` time `sys.argv == ['-c']`, so `nmsvault-cli.exe -c "..."` starts the CLI
    instead.
  - `sys.orig_argv` has the real command line.
  - Any design that spawns `sys.executable -c ...` would launch the app. Use multiprocessing or
    `NMSVAULT_NO_BOOTSTRAP=1` instead.

**K8. Platforms (SOURCED DESIGN.md D1-D13).**
- D3: Xbox stays Windows-only, so the 15 Xbox slots exist only on Windows.
- D4: POSIX kits keep the Windows shape.
- D10: the Linux GUI ships only if the screenshot loop (xvfb) works in its first session.
- The verification ladder wants UI screenshots as CI artifacts (T1/T3 rungs).
- macOS uses the aqua Tk. `theme.system_prefers_dark()` is Windows-registry only today
  (`theme.py:205-224`).

**K9. GUI tests (SOURCED `tests/conftest.py:121-158`).**
- The suite allows exactly **one** Tk root per session ("Can't find a usable tk.tcl" on a
  second root), withdrawn and never shown.
- Tests call handlers and inspect widgets, with no pixel checks. There are 110 uses of `gui_app`:
  `test_gui_actions` has 25 tests, `test_gui_panes` 20, `test_theme` 28.
- Tk needs a display, so Linux CI needs xvfb.
- `App.__init__` loads state and calls `vault.ensure()`, which writes (`gui.py:298-302`).
  Organizer tests must keep using the synthetic-vault fixture.

### 2.2 T1 - Tkinter/ttk evolved

**Prototype.** `<scratch>/R3/tk_proto.py` is 947 lines: about 200 lines of synthetic data, about
150 lines of measurement and screenshots, the rest UI. It reuses the app's own `theme.apply`
(dark) and runs DPI-aware at 150 %. It contains:
- a toolbar and filter;
- a 2x15 live-slot board;
- a grouped Treeview: save-file rows with a badge image and a Live column, version rows with
  ASCII lane art;
- a Notebook (Facts / Notes) above a Canvas history graph;
- a move-to-live dialog and a drag ghost.

(Screenshots removed.)

| Need | Tk 8.6 mechanism | Result | Limit |
|---|---|---|---|
| Grouped, sortable table; one line per save-file, expandable to versions | Treeview parent/child items. A heading command moves top-level rows and children ride along (the pattern `gui.py:968-995` already uses). Filter by `detach`/`reattach` | VERIFIED, see the perf table below | No per-cell style: a tag's font and colour apply to the **whole row**. The monospace lane-art tag leaked into every column. No zebra option: stripe with tags and re-tag after every sort or filter. No frozen columns |
| Status badges (live in Steam 4 / Xbox 2) | Item image in column #0: a two-segment "S \| X" pill drawn pixel by pixel into a PhotoImage, no files, no fonts. Plus a "Steam 4 + Xbox 2" text column and a row colour tag | VERIFIED | One image per row, only in #0. Data cells hold plain text only |
| Live-slot board (15 + 15) | Grid of `tk.Frame` / `tk.Label` cells | VERIFIED | Classic widgets ignore ttk styles, so palette colours are passed by hand |
| Version-history DAG, clickable nodes | Canvas items tagged per version. `tag_bind` for click and hover, `find_closest`, `scale("all")` plus a named font for zoom, `scrollregion` plus wheel. Lineage highlight recolours by tag | VERIFIED | We write the lane layout ourselves (true for every technology). No SVG icons in 8.6. No CSS-like styling |
| Master-detail split | `ttk.PanedWindow` (already used, `gui.py:441`) | VERIFIED | - |
| Notes editor | `tk.Text` with undo, word wrap, and tags for version headers and the current version | VERIFIED, including Unicode on Windows | Classic widget, themed by hand. The Linux kit cannot draw non-Latin-1 typed text (K4) |
| Move to live, then pick the slot to evict | Toplevel with an Account radio and a 15-slot Treeview, target preselected | VERIFIED | - |
| In-app drag-and-drop without tkdnd | `<B1-Motion>` opens an `overrideredirect` ghost Toplevel (alpha 0.9). `root.winfo_containing(x, y)` maps the pointer to a slot cell | VERIFIED: the hit test resolved "Steam slot 7" at the cell centre | In-app only. OS-level drag from Explorer would need tkdnd or OLE (not needed) |
| Facts detail panel | Grid of `ttk.Label` pairs with `wraplength` | VERIFIED | `wraplength` is in pixels, so it needs DPI scaling (K5) |
| DPI scaling | See K5 | VERIFIED | Every pixel constant in `gui.py` needs the factor |
| Dark mode | `theme.apply` restyles ttk; row-tag colours re-applied | VERIFIED for Treeview, Button, Combobox, Entry, Radiobutton | Every **new** ttk class must be mapped. `TNotebook` tabs were unreadable in dark (Facts/Notes tabs in `R3-tk-organizer.png`) |

**Measured performance** (VERIFIED). Timings run `update_idletasks()` so they include Tk layout
and drawing; DPI-aware at 150 %.

| Operation | Base: 42 files, 1,413 version rows, 200-node DAG (5 lanes) | Stress: 300 files, 11,954 rows, 2,000-node DAG (7 lanes) |
|---|---|---|
| Create Tk root | 44 ms | 44 ms |
| Populate whole tree | 39 ms | 148 ms |
| Sort top level (avg / max) | 23.5 / 23.9 ms | 26.7 / 28.9 ms |
| Filter / clear filter | 23.2 / 23.8 ms | 26.5 / 27.0 ms |
| Expand all / collapse all | 20.8 / 24.5 ms | 21.4 / 27.4 ms |
| Draw history graph | 7.9 ms (599 items) | 48.0 ms (5,999 items) |
| Hit test (`find_closest`) | 7.7 us | 48.3 us |
| Zoom (`scale` + font) | 48 ms | 92 ms |
| Select a version (facts + notes + lineage highlight + tree sync) | 55 ms | 175 ms |

For scale: the real vault has 49 catalog entries and 26 distinct slot names (parent scratchpad), so
expect tens of save-files with at most hundreds of versions. Both techs have more than 10x headroom.

**Easy versus hard while building it (VERIFIED by doing).**
- **Easy:** grouping, sort and filter; Canvas drawing and hit-testing; the drag ghost; dialogs;
  reusing `theme.py`.
- **Hard or ugly:**
  - whole-row styling;
  - lane art in a Treeview (crude);
  - theming classic widgets (Text, Canvas, Frame, Label) by hand;
  - every pixel constant after the DPI fix;
  - no rounded corners, shadows, transitions or rich typography;
  - Unicode on Linux.

**Tk 9 path (SOURCED).**
- **What ships Tk 9.0.4.** CPython **3.14.7+** on Windows and the macOS installer
  ([gh-124111](https://github.com/python/cpython/issues/124111), closed 2026-08-03; the 3.12 and
  3.13 Windows builds stay on 8.6), and the newer PBS 3.12 runtime (VERIFIED, K4).
- **What Tk 9.0's treeview adds**
  ([man page](https://manpages.debian.org/trixie/tk9.0-doc/ttk_treeview.3tk.en.html)):
  - `-striped` / `-stripedbackground`;
  - `-titlecolumns` / `-titleitems` (frozen columns and rows);
  - `cellselection` and `-selecttype cell`;
  - per-cell tags (`tag cell add`), whose `-image` puts images in **data cells**;
  - column `-separator`.
- **SVG.** Tk 8.7/9 reads SVG into photo images via nanosvg
  ([TIP 507](https://core.tcl-lang.org/tips/doc/trunk/tip/507.md)).
- **Costs.**
  - Moving the Windows kit from 3.12 to 3.14 changes the development target.
  - Tk 9.x behaviour changes, e.g. TIP 714 (targets Tk 9.1): the treeview stops assigning the
    `user1`/`user2` states that indicator images keyed on, and adds `open`/`leaf` instead
    ([TIP 714](https://core.tcl-lang.org/tips/doc/main/tip/719.md)).
  - The Linux font problem stays (K4).

### 2.3 T2 - Local web UI on the standard library

**Proof.** `<scratch>/R3/web/` holds `server.py` (300 lines) and static `index.html` (54), `app.css`
(81) and `app.js` (378): vanilla JS, no framework, no CDN, same synthetic data.

The server is a `ThreadingHTTPServer` on `127.0.0.1`, port 0. It serves:
- a JSON API: `/api/savefiles`, `/api/savefile?sid=`, `/api/board`, a `/api/move-plan` dry run,
  `/api/note`, `/api/metrics`;
- a held-open `/api/events` stream for lifecycle;
- the static files.

The page builds the table with `createElement` / `textContent` only. It uses HTML5 drag-and-drop
onto slot cells, a `<dialog>` for move-to-live, and an SVG graph with viewBox zoom and pan.

(Screenshots removed.)

**Security: 13/13 cases pass** (VERIFIED, `drive.py` driving `http.client` against the live
server).

| Case | Expected | Got | Server reason |
|---|---|---|---|
| Page without session | 403 | 403 | session |
| Launch URL with wrong token | 403 | 403 | token |
| Right token, rebinding `Host: evil.example:<port>` | 403 | 403 | host |
| Right token | 303 + cookie | 303, `HttpOnly; SameSite=Strict; Path=/` | - |
| API with cookie but no custom header | 403 | 403 | header |
| API, DNS-rebinding Host | 403 | 403 | host |
| API, foreign `Origin` | 403 | 403 | origin |
| API, `Sec-Fetch-Site: cross-site` | 403 | 403 | fetch-site |
| CORS preflight from a foreign origin | 403, no ACAO | 403, ACAO absent | preflight |
| CSRF-style simple POST (no cookie, no header) | 403 | 403 | origin |
| POST with session but `text/plain` | 403 | 403 | content-type |
| Legitimate JSON POST (dry-run plan) | 200 | 200 | - |
| Legitimate API GET | 200 | 200 | - |

**Response headers** (VERIFIED):
- CSP `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`;
- `X-Content-Type-Options: nosniff`;
- `Cache-Control: no-store`;
- `Referrer-Policy: no-referrer`.

The Host check follows Jupyter's DNS-rebinding defence: `allow_remote_access=False` returns 403 on
a non-local Host
([Jupyter Server config](https://jupyter-server.readthedocs.io/en/latest/other/full-config.html)).

**Security points the proof does not fully solve.**
- **Cookies are not isolated by port.** Any other server on 127.0.0.1 that the browser visits
  receives our cookie ([RFC 6265 section 8.5](https://www.rfc-editor.org/rfc/rfc6265#section-8.5);
  SOURCED). So the cookie must never authorize alone. In the proof the custom header is the
  constant `X-NMSV: 1`; production should use a **per-session header secret** (INFERRED).
- **Token on the command line.** `webbrowser.open(url_with_token)` puts the token in the browser's
  command line, which other local users can read on Linux via `/proc` unless `hidepid` is set
  (INFERRED). Jupyter launches through a **redirect file** for exactly this reason
  (`ServerApp.use_redirect_file`, same Jupyter page; SOURCED).
- **Save-derived strings are untrusted in a browser.** Names and notes from a shared save could
  carry markup. The proof renders only via `textContent`, behind the CSP above.
- **Firewall.** Binding 127.0.0.1 only should not trigger a Windows Firewall prompt (INFERRED;
  none was observed).
- **Writes.** Writes must go through the same core safety: game-running check, snapshot, atomic
  swap, oplog. Add one process-wide write lock and keep a dry-run endpoint first, as
  `/api/move-plan` does.

**Lifecycle** (VERIFIED). The page holds `/api/events` open. When the last stream drops, the
server exits after a grace period (4 s in the proof), so reloads survive. A hard idle timeout covers
a page that never connects.
- Stream dropped: server exited after **6.33 s**.
- Edge app window closed (WM_CLOSE): server exited after **6.19 s**.
- Production also needs: single-instance (port/lock file in the state dir); a visible host to Quit
  or Reopen, since pythonw has no console; and a fresh launch token for each reopen.

**pythonw.** The proof overrides `log_message` (K6). App logging in general must go to a file or
handler, not stderr.

**Performance** (VERIFIED). Real clock, Edge 155 `--app` window, devicePixelRatio 1.5. Headless
`--virtual-time-budget` reads `performance.now()` as 0, so it was not used for timing.

| Operation | 42 files | Stress: 300 files |
|---|---|---|
| Server start (inside process / including Python start) | 12 ms / 165 ms | - |
| Fetch `/api/savefiles` (7 KB) / `/api/board` (1.8 KB) | 12.2 / 1.3 ms | 9.8 / 1.7 ms |
| Fetch one save-file's versions (200 versions = 63 KB) | 45 ms | 46 ms |
| Render collapsed list | 18.7 ms (42 rows) | 30.9 ms (300 rows) |
| Render with the big save-file open | 26 ms (242 rows) | 148 ms (2,300 rows) |
| Sort (naive full re-render) | 22 ms | 141 ms |
| SVG history graph | 11.2 ms (603 elements) | 61.9 ms (6,003 elements) |
| Select a version (facts, notes, lineage highlight) | 1.4 ms | 2.2 ms |
| Open all 12,254 rows (render / to next frame) | - | 773 ms / 1,030 ms |
| DOMContentLoaded | 151 ms | 148 ms |

Two notes on these numbers:
- A Python client opening a **new** TCP connection per request measured 15-17 ms median. The
  browser's keep-alive numbers above are the realistic ones.
- The Tk Treeview handles 12k rows better (148 ms). A web table needs lazy expansion or row
  virtualization beyond a few thousand rows.

**Packaging** (SOURCED from the packagers):
- **Windows** needs no change: `_runtime/app` is a recursive copy (`build_portable.ps1:136`).
- **POSIX** needs `app_sources()` to include the assets (one glob), or the assets embedded as
  Python string modules.
- **Size.** The proof's assets total 26 KB, against 14.6 MB today.
- **Offline.** No CDN; the page must work offline.

**Unicode** (VERIFIED on Windows; INFERRED on Linux):
- The browser rendered CJK, Hebrew, em dash, ellipsis, bullets and an emoji in the notes probe
 .
- On Linux the browser draws with the desktop's fonts (fontconfig), so data strings are no longer
  limited by the kit's Tk.
- `test_ui_text.py` scans only `.py` literals, so static assets can stay UTF-8.

**SVG graph** (VERIFIED): viewBox zoom and pan; hover and lineage highlight via CSS classes; click
via `data-vid`. It looks clearly better than the Canvas version at equal effort (compare
the web and Tk screenshots (removed)).

**Testing.**
- The API, security and lifecycle tests are plain pytest + `http.client`; `drive.py` is a
  template. No display is needed (VERIFIED).
- Keep JS thin: the server computes lanes and facts, the page renders. E2E needs a browser via
  dev-only tooling.
- A headless Chromium screenshot loop gives the Linux visual check (D10 / ladder T1, T3) without
  xvfb + Tk (INFERRED). On a personal Windows machine see the Edge caveat below.

**Window options for T2.**
- **Default browser via `webbrowser.open`.** Uses the user's own profile, opens a tab, and creates
  no new profile, so no sign-in side effect (INFERRED).
- **Edge/Chrome `--app` with a private `--user-data-dir`.** Gives a chromeless window that looks
  like a desktop app. It was ready in 0.61 s with Edge already
  resident, and closing the window ends the server (VERIFIED). **But on Windows a fresh Edge
  profile, headless included, was implicitly signed in to the OS Microsoft account and began
  syncing** (VERIFIED in all four throwaway profiles):
  - the popup named the account;
  - `Preferences` held an account entry and `consented_to_sync`;
  - `Sync Data` and `Login Data` (up to 663 KB) were pulled down.

  Candidate fixes, all untested: `--inprivate`; the `ImplicitSignInEnabled` / `BrowserSignin`
  policies, which are machine/user-wide and invasive
  ([Edge policy](https://learn.microsoft.com/deployedge/microsoft-edge-browser-policies/implicitsigninenabled));
  or preferring Chrome when present.
- **WebView2 via ctypes (Windows-only).** The runtime is present here (155.0.4283.45, VERIFIED via
  the EdgeUpdate registry key) and preinstalled on all Windows 11
  ([Microsoft](https://learn.microsoft.com/microsoft-edge/webview2/concepts/evergreen-vs-fixed-version)).
  - The API is COM. It needs `WebView2Loader.dll` from the `Microsoft.Web.WebView2` NuGet package
    (BSD-style licence, binary only;
    [Eclipse IP review](https://gitlab.eclipse.org/eclipsefdn/emo-team/iplab/-/issues/14072)), or
    a hand-rewritten loader.
  - It also needs hand-written ctypes vtables for the environment, controller and webview objects,
    plus COM callback objects for the async completion handlers, plus integration with the Tk
    message loop. That is several hundred lines of brittle code (INFERRED).
  - It is Windows-only with no Linux/macOS story. pywebview already wraps WebView2, through
    pythonnet (a third-party dependency).
  - **Not sensible** next to the two options above.

**JS and licences** (SOURCED / INFERRED).
- Hand-written vanilla JS was enough for the proof (378 lines).
- If libraries are vendored: Apache-2.0 is GPLv3-compatible but not GPLv2-compatible, and it
  subsumes Expat (MIT), BSD and ISC
  ([FSF license list](https://www.gnu.org/licenses/license-list.html),
  [compatibility](https://www.gnu.org/licenses/license-compatibility.html)).
- Keep each licence header and a NOTICE file.
- Treat EPL-2.0 libraries such as elkjs with care: compatible only with a GPL secondary-licence
  designation (INFERRED).
- The gnu.org pages returned HTTP 429 during this research, so the verdicts come from search
  excerpts.

**Coexistence with the CLI and the Tk app** (INFERRED). The CLI is unchanged in all three:
1. **Tk host.** An "Open organizer" button starts the server thread inside the Tk process, which
   shares the core and the write lock. Closing Tk ends the server, and the old panes can stay
   until the organizer supersedes them.
2. **`nmsvault-cli ui` subcommand.** A console process that stops on Ctrl+C. Natural on Linux/macOS.
3. **Web UI as the main window.** The GUI launcher opens the web UI directly and keeps a tiny Tk
   status window (Quit / Reopen) as the visible handle.

**Effort indicator** (VERIFIED by counting): at proof level, the web proof is about 810 lines and
the Tk prototype about 950 lines for comparable coverage, both including measurement code.

### 2.4 T3 - Third-party toolkits (brief)

Wheel sizes are compressed, latest release on PyPI on 2026-10-16 (VERIFIED, PyPI JSON API).
Licences are as declared on PyPI.

| Toolkit | Windows | Linux x86_64 | macOS arm64 | Licence | Notes |
|---|---|---|---|---|---|
| PySide6 6.12 (Essentials + Addons + shiboken6) | 78.3 + 38.6 + 1.2 MB | 80.4 + 56.5 + 0.3 MB | 110.6 + 86.4 + 0.5 MB | LGPL-3.0 / GPL | Highest native ceiling: QTreeView models, QGraphicsView DAG. Linux needs system xcb libraries (INFERRED). The kit would need a pip-install build step |
| wxPython 4.3.2 | 18.8 MB | **no wheel on PyPI** | 18.2 MB | LGPL-2.0+ with wxWindows exception | Requires numpy. Linux wheels come per distro from wxPython's own index or a source build |
| pywebview 6.2.1 | 0.5 MB + pythonnet 3.2.1 (MIT) + clr_loader (cffi) | pure, but needs system GTK + WebKit2 (PyGObject) or Qt | pure + pyobjc | BSD-3 | Windows: WinForms via pythonnet, which needs .NET and WebView2. Linux needs system libraries that a portable kit cannot carry |
| Dear PyGui 2.3.1 | 1.8 MB | 2.6 MB | 1.9 MB | MIT | GPU immediate-mode (D3D11/OpenGL). Non-native look, weak accessibility |
| Toga 0.5.7 | pure + pythonnet + pillow + `dotnet-WebView2.WinForms` | GTK via PyGObject + pycairo (system) | rubicon-objc | BSD-3 | 0.x maturity; same system-library problem on Linux |
| Flet 1.0.4 | 0.6 MB + `flet-desktop` (0 MB) | same | same | Apache-2.0 | The Flutter client is **downloaded from GitHub at first run** ([Flet docs](https://docs.flet.dev/contributing)), which conflicts with "update check is the only network use". `flet build windows` needs VS 2022 |
| Kivy 2.3.1 | 4.4 MB + sdl2 4.0 + angle 8.4 MB | 22.0 MB | 10.6 MB | MIT | Non-native look, SDL2 |

What every T3 option shares (INFERRED):
- It **breaks the stdlib-only guarantee** (K1), and with it D6's spirit. It needs owner sign-off.
- It adds a pip build step and per-platform wheels to packaging that today is just "download the
  signed runtime and copy".
- It means licence notices and larger downloads.
- Trust is unchanged: the launcher stays the PSF-signed exe, and extra DLLs are loaded, not
  launched.

**PySide6 is the only one whose ceiling clearly beats T2, at the highest cost.**

### 2.5 Side issues found (outside R3's question)

- The GUI is DPI-unaware and therefore blurry at 150 % (K5, also R2 Q8). The fix is independent of
  the organizer.
- `packaging/sitecustomize.py:125-127`: the `-c` / `-m` bypass never fires (K7).
- `theme.py` maps no `TNotebook` (or `TLabelframe`, `TSpinbox`, `TSeparator`). This matters as
  soon as a new widget class appears.
- Residue from this research on the owner's account (VERIFIED cause; effect INFERRED):
  - up to four extra Edge sync profiles or devices;
  - `127.0.0.1` history entries titled "NMS Save Vault - Organizer (R3 web proof)".

  The local throwaway profiles were deleted. The owner may want to check Edge Settings > Profiles
  > Sync.

---

## 3. Implications for the design

**Constraints.**
- T1 and T2 both keep the stdlib-only runtime. T3 needs owner sign-off.
- **Any Tk organizer:** non-Latin-1 data (notes, names) garbles on the Linux kit, Tk 9 included.
  Either pick a notes charset (R2 Q9) or choose T2.
- **Any Tk UI, including today's:** needs DPI awareness plus pixel-constant scaling, and dark-theme
  mapping for every new ttk class.
- **Any pythonw process:** has no stdout/stderr. Log to a file; `http.server` logging must be
  overridden.
- **POSIX packager:** ships `.py` only (K3).
- **Tests:** one Tk root per session (K9). T2's server side is testable without a display.
- **T2:** the README's "only network use" sentence (`README.md:329`) must account for a loopback
  listener.

**Risks.**
- **T2 security.** Port-shared cookies, token exposure on the command line, XSS from save strings,
  CSRF, rebinding. Each mitigation is shown above; the per-session header secret and the redirect
  file are still to do.
- **T2 lifecycle and UX.** Tab versus app window; quit semantics; single instance.
- **T2 app window.** The Edge private-profile sign-in problem.
- **T1.** The polish ceiling and whole-row styling. Tk 9 would mean a Python 3.14 move.
- **Both.** Concurrent writers if two front-ends write. Use one process-wide write lock, or let
  only one front-end write.

**Opportunities.**
- sqlite3 with FTS5 and JSON is already in every kit: index storage plus full-text search over
  notes and facts (R5).
- multiprocessing works from the real launcher, so indexing can run without freezing either UI.
- T2 brings SVG, CSS (dark mode via `prefers-color-scheme` on every OS), Unicode, and headless
  browser screenshots for the Linux loop.
- T1 reuses `gui.py` / `theme.py` and the existing tests, with Tk 9 features available later.

**Scale.** Realistic organizer sizes (tens of save-files, hundreds of versions) sit far below the
stress points measured for both technologies.

---

## 4. Decision matrix and recommendation

Options:
- **T1-A:** organizer built in the existing Tk app (Tk 8.6), with the DPI and theme pass.
- **T1-B:** T1-A plus a runtime bump to CPython 3.14.7+ / Tk 9.0.4 on all kits.
- **T2-A:** local stdlib web UI hosted by the app process, opened in the default browser; the Tk
  window stays as host and for the legacy panes.
- **T2-B:** T2-A in a chromeless Edge/Chrome `--app` window.
- **T2-C:** WebView2 through ctypes.
- **T3:** PySide6, as the strongest third-party option.

Scores run 1-5, where 5 is best: least effort, lowest risk, highest ceiling, best fit, best
readiness, easiest testing.

| | Effort | Risk | UX ceiling | Fit with guarantees | Linux/macOS readiness | Testability |
|---|---|---|---|---|---|---|
| T1-A | 4 | 5 | 2 | 5 | 2 (ASCII / fonts; xvfb loop) | 4 (existing harness; Canvas is introspectable) |
| T1-B | 3 | 3 | 3 | 4 (Python target moves) | 2 (fonts unchanged) | 4 |
| T2-A | 3 | 3 | 5 | 4 (stdlib; new loopback listener) | 5 (any browser) | 4 (API without a display; JS needs a browser) |
| T2-B | 3 | 2 (profile / sign-in) | 5 | 4 | 3 (needs a Chromium-family browser) | 4 |
| T2-C | 1 | 1 | 5 | 3 (vendored loader DLL) | 1 (Windows only) | 2 |
| T3 | 2 | 2 | 5 | 1 (breaks stdlib-only) | 3 (wx: no Linux wheels; Qt fine) | 3 |

**Recommendation.** Give **T1-A** and **T2-A** full designs with mockups; they are the real owner
fork. Fold T2-B into T2-A's design as a window-mode variant, gated on a sign-in-free launch being
proven. Fold T1-B into T1-A's design as a later upgrade path. Document but do not design T2-C and
T3.

Between the two, **lean T2-A**, for three reasons:
- The owner's must-haves are detailed notes (Unicode), a readable history tree, many facts, and
  room for "deeper indexing for future features". T2's ceiling and Unicode freedom fit them best.
- It removes the Linux Tk font problem and gives the Linux screenshot loop a headless-browser path.
- It keeps the stdlib guarantee. The security and lifecycle unknowns were retired here with about
  300 lines of stdlib Python.

T1-A is the better choice if the owner values:
- one language (no JS);
- no local listener;
- the smallest change to a codebase whose tests and theming are Tk-shaped.

**Do regardless of the choice:** the DPI fix (K5), and log routing for pythonw (K6).

---

## 5. Open questions and candidate options (forks for the parent)

| # | Question | Options and trade-offs |
|---|---|---|
| Q1 | UI technology for the organizer | **T1-A Tk**: lowest risk, Unicode-limited on Linux, modest looks. **T2-A web in the default browser**: highest ceiling and Unicode, but JS plus localhost security. **Hybrid**: T2-A organizer with Tk kept for today's flows, an incremental path. T3 only with explicit sign-off to break the stdlib guarantee |
| Q2 | Notes and data charset (with R2 Q9) | Unicode everywhere: fine under T2, garbled on the Linux kit under T1. Latin-1 only: Linux-safe under T1, no emoji or CJK. ASCII: safest, poorest for notes |
| Q3 | Window style if T2 | Default-browser tab: no side effects, less app-like. `--app` window: app-like and gives a clean "window closed" signal, but needs a sign-in-free Edge launch (`--inprivate` or policy, both untested) or Chrome. Tk host window plus tab |
| Q4 | Runtime bump for Tk 9 (if T1) | Stay on 3.12 / Tk 8.6 (no per-cell styles). Move the Windows kit to CPython 3.14.7+ and PBS to Tk 9.0.4: per-cell tags and images, striping, frozen columns, SVG, but a new Python target and Tk 9 behaviour changes, and Linux fonts are still unfixed |
| Q5 | DPI awareness now | Ship the fix now: crisp, but every pixel constant must be scaled and the window looks bigger. Defer it to the UI restructure |
| Q6 | T2 hosting and lifecycle | Server thread inside the Tk app: shares the lock, and closing Tk ends both. `nmsvault-cli ui` console subcommand. Web-first launcher with a small Tk status window. Single-instance via a port/lock file in the state dir |
| Q7 | JS approach if T2 | Hand-written vanilla (proof: 378 lines) versus vendored permissive libraries (MIT/BSD/ISC/Apache-2.0 with notices). No CDN in either case |
| Q8 | JS testing if T2 | API-only pytest plus thin JS. Dev-only browser E2E (e.g. Playwright, which downloads a browser). Headless-browser screenshots in CI (fine on runners; avoid private Edge profiles on personal machines) |
| Q9 | Packaging assets if T2 | Extend `build_posix_kit.app_sources()` to ship `web/*`, or embed assets as `.py` string modules (no packager change, but the ASCII test then covers them) |

---

## Appendix A - Reproduction

`<scratch>` is the session scratchpad. Paths below are relative to it unless they start with the
repo. Set `PYTHONUTF8=1` everywhere.

- **Kit runtime probe (K2):**
  `NMSVAULT_NO_BOOTSTRAP=1 TCL_LIBRARY=<kit>/_runtime/tcl/tcl8.6 TK_LIBRARY=<kit>/_runtime/tcl/tk8.6 dist/NMSSaveVault/nmsvault-cli.exe -B R3/kitprobe.py`.
  The same script also ran under `uv run python`.
- **Launcher manifest (K5):** search the bytes of `dist/NMSSaveVault/NMSSaveVault.exe` for
  `<assembly` ... `</assembly>`.
- **Kit clone (K5-K7).**
  1. Copy `dist/NMSSaveVault` to `R3/kitclone/`.
  2. Replace `_runtime/app/nms_save_vault` with the dummy `probe.py`, `gui.py` and `cli.py`.
  3. Set `LOCALAPPDATA` / `APPDATA` to `R3/fake_*`, because the bootstrap writes
     `startup-error.txt` there on failure.
  4. Run PowerShell `Start-Process <clone>/NMSSaveVault.exe -PassThru` and `WaitForExit`.
  5. Read the results in `R3/kit_gui_probe_detached.json`, plus `site_argv*.txt` from an
     instrumented copy of the clone's `sitecustomize`.
- **PBS Tk/Xft scan (K3, K4):**
  `uv run python -I R3/pbs_tk_scan.py build/runtime-cache/cpython-3.12.12+20251217-...stripped.tar.gz`,
  then `R3/pbs_tk_scan2.py` on `cpython-3.12.14+20260901-...tar.gz`. Members are read in memory;
  nothing is extracted.
- **Tk prototype (2.2).**
  - `uv run python R3/tk_proto.py --stress [--files 300 --big 2000 --maxv 64] --metrics out.json`
  - `uv run --with pillow python R3/tk_proto.py --auto --shots <dir>`
  - Results: `R3/tk_metrics_{base,stress,auto}.json`.
- **DPI comparison:**
  `uv run --with pillow python R3/dpi_compare.py --mode {unaware|aware-raw|aware-scaled} --out ...`,
  then `R3/dpi_combine.py`.
- **Web proof (2.3).** `uv run --with pillow python R3/web/drive.py` runs the security suite,
  headless screenshots, real-clock metrics and lifecycle, writing `R3/web_results.json`.
  **Do not re-run it as-is on a personal machine:** it launches Edge with fresh profiles, which
  sign into the OS Microsoft account. `R3/web/check_profiles.py` reports sign-in state without
  printing personal data.
- **PyPI sizes (2.4):** `uv run python -I R3/pypi_sizes.py`, output in `R3/pypi_sizes.md`; and
  `R3/pypi_files.py <pkg>...`.

## Appendix B - Images

(screenshots removed from the repo; they showed real save names and backup ids)
