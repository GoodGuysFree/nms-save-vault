"""Deep detail on demand (``core/index/deep.py``, ``IndexService.request_deep`` / ``deep``)."""
from __future__ import annotations

import hashlib
import json

from nms_save_vault.core.index import Priority, extract
from nms_save_vault.core.index.deep import deep_facts
from nms_save_vault.core.index.pool import ExecutionPolicy
from nms_save_vault.core.index.worker import IndexJob, run_job
from nms_save_vault.core.savejson import decode_save

from .fixtures import FIXTURES
from .savebuilder import encode
from .test_cli import run, world  # noqa: F401 - the CLI fixture
from .test_pool import Rig
from .test_privacy import SENTINELS, sentinel_save
from .test_service import SpyLocator, make, save_bytes, settle, wait_for  # noqa: F401 - fixtures


def rich() -> tuple[str, object]:
    readable, kw = FIXTURES["rich_base"]()
    data, _hint = encode(readable, **kw)
    return hashlib.sha256(data).hexdigest(), decode_save(data)


def test_deep_detail_of_bases_inventories_and_loose_objects():
    vid, decoded = rich()
    deep = deep_facts(vid, decoded)
    assert deep["vid"] == vid and list(deep["contexts"]) == ["base"]
    base = deep["contexts"]["base"]
    facts = extract.run_extractors(decoded)
    assert [b["id"] for b in base["bases"]] == [b["id"] for b in facts["contexts"]["base"]["bases"]["own"]]
    home = base["bases"][0]
    assert home["name"] == "Synth Home" and home["type"] == "HomePlanetBase"
    assert home["parts_by_id"] == {"BASE_FLAG": 1, "C_WALL": 2, "M_FLOOR": 1, "W_ROOF": 1}
    exosuit = base["inventories"]["exosuit"]
    assert [(s["id"], s["amount"], s["x"]) for s in exosuit] == [("FUEL1", 20, 0), ("CARBON", 250, 1), ("LAUNCHER", 1, 0)]
    assert sorted(k for k in base["inventories"] if ":" in k) == ["multitool:0", "ship:0", "ship:2"]
    assert base["loose_objects"] == {"BASE_FLAG": 1, "GARAGE_M": 1, "SUMMON_GARAGE": 1}
    json.dumps(deep)


def test_deep_detail_reads_no_ownership_data():
    data = encode(sentinel_save())[0]
    deep = deep_facts(hashlib.sha256(data).hexdigest(), decode_save(data))
    blob = json.dumps(deep, ensure_ascii=False)
    assert [s for s in SENTINELS if s in blob] == []


def test_a_deep_job_returns_detail_not_facts():
    vid, _decoded = rich()
    readable, kw = FIXTURES["rich_base"]()
    r = run_job(IndexJob(vid, "deep", encode(readable, **kw)[0]))
    assert r.status == "ok" and r.facts is None and r.kind == "deep"
    assert r.deep["contexts"]["base"]["bases"][0]["parts_by_id"]["C_WALL"] == 2


def test_facts_and_deep_jobs_of_one_version_queue_apart():
    rig = Rig(workers=2)
    rig.d.submit(["v1"], Priority.BACKFILL)
    assert rig.d.submit(["v1"], Priority.INTERACTIVE, kind="deep") == 1
    rig.settle()
    jobs = {(j.vid, j.kind) for j, _d, _f in rig.ex.jobs}
    assert jobs == {("v1", "deep"), ("v1", "facts")}
    assert rig.ex.started[0] == "v1"                         # the deep job went first (INTERACTIVE)


def test_the_service_decodes_on_demand_keeps_five_and_never_persists(make):
    loc = SpyLocator()
    vids = [loc.add(save_bytes(i)) for i in range(6)]
    svc = make(loc)
    svc.request_deep(vids[0])                                # not indexed yet: facts come too
    wait_for(lambda: svc.deep(vids[0]) is not None)
    settle(svc)
    assert svc.status(vids[0]) == "ok"
    assert ("deep-ready", vids[0]) in [(e.kind, e.vid) for e in svc.events_since(0)]
    card = svc.card(vids[0])
    assert card.deep["contexts"]["base"]["bases"] == []
    for v in vids[1:]:
        svc.request_deep(v)
        wait_for(lambda v=v: svc.deep(v) is not None)
    assert svc.deep(vids[0]) is None and all(svc.deep(v) for v in vids[1:])   # an LRU of five
    svc.close()
    again = make(loc)
    assert again.deep(vids[5]) is None and again.card(vids[5]).deep is None


def test_off_means_no_deep_either(make):
    loc = SpyLocator()
    vid = loc.add(save_bytes(1))
    svc = make(loc, ExecutionPolicy("off", 0, False))
    svc.request_deep(vid)
    assert svc.deep(vid) is None and loc.threads == []


def test_the_cli_card_shows_the_deep_section(world, capsys):  # noqa: F811 - the fixture
    assert run(world, "show", world["new"][:10], "--card") == 0
    assert "[base, decoded on demand]" in capsys.readouterr().out
