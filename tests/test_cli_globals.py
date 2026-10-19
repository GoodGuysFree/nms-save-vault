"""--live and --vault are accepted after ANY subcommand (and its sub-subcommand): the kit's
launcher rejects them as a first argument, so the documented form puts them last."""
from __future__ import annotations

import argparse

import pytest

from nms_save_vault import cli


def _subparsers(parser: argparse.ArgumentParser):
    return next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)


def _leaf_commands(parser=None, prefix=()):
    """Every runnable command line (subcommand chain) with a dummy for each required argument."""
    parser = parser or cli.build_parser()
    subs = _subparsers(parser)
    if subs is None:
        needed = []
        for a in parser._actions:
            if a.option_strings or isinstance(a, argparse._HelpAction):
                if a.required and a.option_strings:
                    needed += [a.option_strings[0], str(a.choices[0]) if a.choices else "1"]
                continue
            if a.nargs in (None, "+") or isinstance(a.nargs, int) and a.nargs > 0:
                count = a.nargs if isinstance(a.nargs, int) else 1
                needed += [str(a.choices[0]) if a.choices else "1"] * count
        yield list(prefix) + needed
        return
    for name, sub in subs.choices.items():
        yield from _leaf_commands(sub, prefix + (name,))


COMMANDS = list(_leaf_commands())


def test_every_subcommand_is_covered():
    top = {c[0] for c in COMMANDS}
    assert top == set(_subparsers(cli.build_parser()).choices)


@pytest.mark.parametrize("argv", COMMANDS, ids=[" ".join(c[:3]) for c in COMMANDS])
def test_the_globals_work_after_the_subcommand(argv):
    for line in (argv + ["--vault", "V", "--live", "L"], argv + ["--live=L", "--vault=V"],
                 [argv[0], "--vault", "V", "--live", "L"] + argv[1:]):
        args = cli.parse_args(line)
        assert (args.vault, args.live) == ("V", "L"), line


def test_the_gui_entry_point_takes_a_vault(monkeypatch):
    from nms_save_vault import gui

    seen = {}

    class FakeApp:
        def __init__(self, **kw):
            seen.update(kw)

        def mainloop(self):
            pass

    monkeypatch.setattr(gui.logfile, "configure", lambda folder: None)
    monkeypatch.setattr(gui, "_enable_dpi_awareness", lambda: None)
    monkeypatch.setattr(gui.aliases, "active", lambda: None)
    monkeypatch.setattr(gui, "App", FakeApp)
    assert gui.main(["--vault", "V"]) == 0 and seen == {"vault": "V"}
    seen.clear()
    assert gui.main([]) == 0 and seen == {}
