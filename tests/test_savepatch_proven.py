"""A batch of edits proven once on a body is not proven again on the same body (2026-11-23,
GGF: "Apply All (10 changes) in 9.1 seconds is way too long"): an app proves each change as it
holds it, and the write made the same proofs again on the same bytes, a full parse of each
edit's container twice, up to 1.3 s a batch on a real save. The splice is still made from the
bytes each time, and taken as proven only when its result hashes to the one proven before;
anything else is proven in full, as always."""
from __future__ import annotations

import json

import pytest

from nms_save_vault.core import savepatch as sp
from nms_save_vault.core.hashname import key as K

R = sp.Replace
PSD = ("PlayerStateData",)


def save(**psd) -> bytes:
    return json.dumps({K("PlayerStateData"): {K(k): v for k, v in psd.items()}},
                      separators=(",", ":")).encode("utf-8")


@pytest.fixture(autouse=True)
def fresh():
    sp.forget_proven()
    yield
    sp.forget_proven()


def _proofs(monkeypatch) -> list:
    calls = []
    real = sp._loads
    monkeypatch.setattr(sp, "_loads", lambda text, **k: calls.append(len(text)) or real(text, **k))
    return calls


def test_the_same_batch_on_the_same_bytes_is_proven_once(monkeypatch):
    body = save(Units=5, Nanites=7)
    calls = _proofs(monkeypatch)
    first = sp.apply(body, [R(PSD + ("Units",), b"99")])
    proofs = len(calls)
    again = sp.apply(body, [R(PSD + ("Units",), b"99")])
    assert again == first and proofs > 0 and len(calls) == proofs


def test_steps_written_after_being_held_are_not_proven_again(monkeypatch):
    body = save(Units=5, Nanites=7)
    held = body
    batches = [[R(PSD + ("Units",), b"6")], [R(PSD + ("Nanites",), b"8")]]
    for batch in batches:                       # as an app holds each change
        held = sp.apply(held, batch)
    calls = _proofs(monkeypatch)
    assert sp.apply_steps(body, sp.Steps(batches)) == held   # as the write makes them all
    assert calls == []


def test_another_body_or_batch_is_proven_in_full(monkeypatch):
    body = save(Units=5, Nanites=7)
    sp.apply(body, [R(PSD + ("Units",), b"99")])
    calls = _proofs(monkeypatch)
    sp.apply(save(Units=4, Nanites=7), [R(PSD + ("Units",), b"99")])
    assert calls
    calls.clear()
    sp.apply(body, [R(PSD + ("Units",), b"98")])
    assert calls


def test_a_splice_that_comes_out_otherwise_is_proven_and_refused(monkeypatch):
    body = save(Units=5, Nanites=[1, 2])
    sp.apply(body, [R(PSD + ("Units",), b"6")])          # proven, remembered
    real = sp._splice

    def sloppy(body, edit, cache):
        start, end, raw = real(body, edit, cache)
        return start, end + 1, raw                  # eats the byte after the value

    monkeypatch.setattr(sp, "_splice", sloppy)
    with pytest.raises(sp.PatchRefused):
        sp.apply(body, [R(PSD + ("Units",), b"6")])


def test_what_is_remembered_is_bounded():
    for n in range(sp.PROVEN_MAX + 40):
        sp.apply(save(Units=n), [R(PSD + ("Units",), b"1")])
    assert len(sp._PROVEN) <= sp.PROVEN_MAX
