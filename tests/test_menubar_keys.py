"""The in-window menu bar's Left / Right between menus (the logic; a posted menu is a modal
loop on Windows, so nothing here posts one)."""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")

from nms_save_vault import theme  # noqa: E402
from nms_save_vault.ui.menubar import MenuStrip  # noqa: E402


class FakeHook:
    """The strip's side of a key hook, without any Windows."""

    def __init__(self):
        self.in_submenu = self.on_cascade = False
        self.step = 0
        self.installed = 0

    def install(self):
        self.installed += 1

    def remove(self):
        step, self.step = self.step, 0
        return step


@pytest.fixture
def strip(gui_app):
    model = tk.Menu(gui_app, tearoff=False)
    for label in ("One", "Two", "Three"):
        sub = tk.Menu(model, tearoff=False)
        sub.add_command(label="Item", command=lambda: None)
        inner = tk.Menu(sub, tearoff=False)
        inner.add_command(label="Deep", command=lambda: None)
        sub.add_cascade(label="More", menu=inner)
        model.add_cascade(label=label, menu=sub, underline=0)
    bar = MenuStrip(gui_app, model, key_hook=FakeHook())
    # A real post is a modal loop: stand in for tk::MbPost
    tcl = gui_app.tk
    args, body = tcl.eval("info args tk::MbPost"), tcl.eval("info body tk::MbPost")
    tcl.eval("set ::mbposts {}; proc tk::MbPost {args} {lappend ::mbposts $args}")
    bar.posts = lambda: list(tcl.splitlist(tcl.eval("set ::mbposts")))
    yield bar
    tcl.call("proc", "tk::MbPost", args, body)
    tcl.eval("unset ::mbposts")
    bar.destroy()
    model.destroy()


def _run_after(strip, ms=40):
    strip.after(ms, strip.quit)
    strip.mainloop()


def test_post_installs_the_hook_around_the_post_only_and_moves_on(strip):
    hook = strip._hook
    seen = []
    orig = strip.post
    strip.post = lambda b: (seen.append(b), orig(b))[1]
    # the popup "returns" having been told Left
    strip.tk.eval("set ::tk::Priv(postedMb) {}")
    hook.step = -1
    orig_remove = hook.remove
    strip.post(strip.buttons[0])
    _run_after(strip)
    assert hook.installed >= 1
    assert seen[0] is strip.buttons[0]
    assert seen[1] is strip.buttons[2]                   # wrapped round to the last
    assert strip.posts() == [str(strip.buttons[0]), str(strip.buttons[2])]
    assert orig_remove() == 0                            # consumed
    hook.step = 1
    strip.post(strip.buttons[2])
    _run_after(strip)
    assert seen[-1] is strip.buttons[0]


def test_the_hook_is_removed_first_even_when_the_post_fails(strip):
    hook = strip._hook
    order = []
    hook.remove = lambda: order.append("remove") or 0
    strip.tk.eval("proc tk::MbPost {args} {error boom}")
    with pytest.raises(tk.TclError):
        strip.post(strip.buttons[0])
    assert order == ["remove"] and hook.installed == 1
    strip.tk.eval("set ::mbposts {}; proc tk::MbPost {args} {lappend ::mbposts $args}")
    hook.install = lambda: (_ for _ in ()).throw(RuntimeError("no hook"))
    with pytest.raises(RuntimeError):                    # install itself failing
        strip.post(strip.buttons[0])
    assert order == ["remove", "remove"]


def test_select_events_tell_the_hook_what_is_open(strip):
    hook = strip._hook
    top = strip.dropdown(strip.buttons[0])
    inner = top.nametowidget(top.entrycget(1, "menu"))
    strip._posted = strip.buttons[0]
    strip._on_select(SimpleNamespace(widget=inner))      # initialised, nothing highlighted
    assert not hook.in_submenu
    inner.activate(0)
    strip._on_select(SimpleNamespace(widget=inner))      # an open submenu on a plain item
    assert hook.in_submenu and not hook.on_cascade
    top.activate(1)                                      # the cascade entry of the dropdown
    strip._on_select(SimpleNamespace(widget=top))
    assert not hook.in_submenu and hook.on_cascade
    top.activate(0)
    strip._on_select(SimpleNamespace(widget=top))
    assert not hook.on_cascade


def test_the_strips_watchers_go_with_it(gui_app, strip):
    assert not gui_app.bind_all("<<MenuSelect>>")        # nothing global to go stale


@pytest.mark.skipif(sys.platform != "win32", reason="the hook is Windows only")
class TestMenuKeyHook:
    def _msg(self, message, wparam):
        msg = theme._msg_struct()()
        msg.message, msg.wParam = message, wparam
        return msg

    @pytest.fixture
    def hook(self, monkeypatch):
        hook = theme.MenuKeyHook()
        ended = []
        monkeypatch.setattr(hook, "_end_menu", lambda: ended.append(1))
        hook.ended = ended
        return hook

    def _feed(self, hook, message, wparam, code=theme._MSGF_MENU):
        msg = self._msg(message, wparam)
        return hook.handle(code, 0, ctypes.addressof(msg))

    def test_left_and_right_end_the_popup_and_record_the_step(self, hook):
        assert self._feed(hook, theme._WM_KEYDOWN, theme._VK_LEFT)
        assert hook.remove() == -1 and hook.ended == [1]
        assert self._feed(hook, theme._WM_KEYDOWN, theme._VK_RIGHT)
        assert hook.remove() == 1

    def test_other_keys_messages_and_codes_pass_on(self, hook):
        assert not self._feed(hook, theme._WM_KEYDOWN, 0x28)             # Down
        assert not self._feed(hook, 0x0101, theme._VK_LEFT)              # key up
        assert not self._feed(hook, theme._WM_KEYDOWN, theme._VK_LEFT, code=0)
        assert hook.remove() == 0 and hook.ended == []

    def test_cascades_keep_their_own_keys(self, hook):
        hook.in_submenu = True                      # Left closes the open submenu itself
        assert not self._feed(hook, theme._WM_KEYDOWN, theme._VK_LEFT)
        hook.on_cascade = True                      # Right opens the cascade itself
        assert not self._feed(hook, theme._WM_KEYDOWN, theme._VK_RIGHT)
        hook.on_cascade = False                     # Right on a plain item inside moves on
        assert self._feed(hook, theme._WM_KEYDOWN, theme._VK_RIGHT)
        assert hook.remove() == 1

    def test_the_msg_struct_is_built_once(self):
        assert theme._msg_struct() is theme._msg_struct()

    def test_installed_only_between_install_and_remove(self, hook):
        hook.install()
        assert hook._handle
        hook.remove()
        assert hook._handle is None
