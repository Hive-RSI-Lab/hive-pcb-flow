"""A stale PATCH_<net> zone from anywhere is named (#1424, #1145's fixes 2-3).

route_edit names a patch whose last via an edit takes out (#1145), but a
patch left by a hand edit, another tool or an older board went through
DRC, the fresh-fill check (geom._empty_by_refill lets a zone KiCad's
refill leaves empty through) and part_repair (patch_notes skipped an empty
fill) unflagged. geom.stale_patches() finds them; the drc_routed gate lists
them beside DRC's findings (gate.patch_findings) and part_repair's notes
name one that fills nothing.
Fixture: pinned core, PATCH_PVDD on F.Cu with its 11 PVDD vias.
  planted  the patch with its vias gone (info, the gate still passes on it);
           the patch with nothing filled (a warning drc_routed fails on, and
           a part_repair note); live: rip the 11 vias and refill, and a
           patch drawn on another net's pad that a refill leaves empty
  twin     the board as it is; one of the 11 vias left; a via of another
           net in the patch; a PATCH_ zone named for another net; a
           teardrop: nothing named
"""
from __future__ import annotations

import copy
import dataclasses
import shutil
import sys
from pathlib import Path

import pytest
from shapely.geometry import Point, box

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import geom  # noqa: E402
import gate  # noqa: E402
import part_repair  # noqa: E402

CORE = REPO / "pinned" / "core" / "kicad"
PCB = CORE / "core.kicad_pcb"
PVDD_BOX = box(53.6, 165.8, 59.6, 171.2)
PATCH = "0d93d1f6-8e21-4b83-8550-03803c10845a"     # PATCH_PVDD, F.Cu


@pytest.fixture(scope="module")
def base():
    return geom.BoardGeom.from_file(PCB)


@pytest.fixture
def bg(base):
    """A copy of the core board whose zone and via lists a test may edit."""
    b = copy.copy(base)
    b._zones = list(base._zones)
    b._vias = list(base._vias)
    return b


def _pvdd(bg) -> list:
    return [v for v in bg._vias if v.net == "PVDD"
            and PVDD_BOX.contains(Point(v.at))]


def _patch(bg):
    return next(z for z in bg._zones if z.uuid == PATCH)


def _swap(bg, z, **kw):
    bg._zones = [dataclasses.replace(z, **kw) if o is z else o
                 for o in bg._zones]


def _named(bg) -> list:
    return [(p["uuid"], p["no_via"], p["empty"]) for p in bg.stale_patches()]


def test_twin_the_board_as_it_is_names_nothing(bg):
    assert len(_pvdd(bg)) == 11
    assert _named(bg) == []
    assert gate.patch_findings(bg) == []
    assert not [n for n in part_repair.patch_notes(bg, {}, {})
                if "fills nothing" in n]


def test_planted_vias_gone_is_named_as_info(bg):
    gone = {id(v) for v in _pvdd(bg)}
    bg._vias = [v for v in bg._vias if id(v) not in gone]
    assert _named(bg) == [(PATCH, True, [])]
    f, = gate.patch_findings(bg)
    assert (f["check"], f["severity"], f["net"], f["layer"], f["zone"]) == \
        ("patch_no_via", "info", "PVDD", "F.Cu", PATCH)
    assert PVDD_BOX.contains(Point(f["pos"]))
    # drc_routed fails on error and warning only: info rides along
    res = gate.evaluate("drc_routed", {"fail_severities": ["error",
                                                          "warning"]},
                        {"violations": [f]})
    assert res["status"] == "pass" and res["warnings"] == [f]


def test_twin_one_via_left_or_another_nets_via(bg):
    keep = _pvdd(bg)[0]
    gone = {id(v) for v in _pvdd(bg)[1:]}
    bg._vias = [v for v in bg._vias if id(v) not in gone]
    assert _named(bg) == []
    # the last via renetted: a GND via in the patch holds nothing for it
    bg._vias = [dataclasses.replace(v, net="GND") if v is keep else v
                for v in bg._vias]
    assert _named(bg) == [(PATCH, True, [])]


def test_planted_empty_fill_is_a_warning_and_a_note(bg):
    z = _patch(bg)
    _swap(bg, z, fills={})
    assert _named(bg) == [(PATCH, False, ["F.Cu"])]
    f, = gate.patch_findings(bg)
    assert (f["check"], f["severity"], f["layer"]) == \
        ("stale_patch", "warning", "F.Cu")
    region = geom.patch_region(z.outline)
    assert f"--region {region} --remove" in f["msg"]
    x0, y0, x1, y1 = map(float, region.split(","))
    assert all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in z.outline)
    res = gate.evaluate("drc_routed", {"fail_severities": ["error",
                                                          "warning"]},
                        {"violations": [f]})
    assert res["status"] == "fail" and res["failing"] == [f]
    notes = part_repair.patch_notes(bg, {}, {}, uuids=True)
    assert [(u, "fills nothing" in n) for u, n in notes] == [(PATCH, True)]
    assert f"--region {region} --remove" in notes[0][1]


def test_mutant_old_patch_notes_skips_the_empty_fill(bg):
    """The old loop ran over z.fills, which has no key for a layer with no
    fill: the empty patch was never even looked at."""
    _swap(bg, _patch(bg), fills={})
    assert [ly for z in bg.zones_of() if z.uuid == PATCH
            for ly in z.fills] == []
    assert part_repair.patch_notes(bg, {}, {}) != []


def test_twin_other_zones_are_not_patches(bg):
    z = _patch(bg)
    # named for another net, a teardrop, or not a PATCH_ at all
    for kw in ({"name": "PATCH_GND"}, {"teardrop": True},
               {"name": "PVDD_POUR"}):
        b = copy.copy(bg)
        b._zones = list(bg._zones)
        _swap(b, z, fills={}, **kw)
        assert _named(b) == [], kw


# ------------------------------------------------------------ live (KiCad)

@pytest.fixture
def kicad():
    import env
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    return cli


def _copy(tmp: Path) -> Path:
    for f in CORE.iterdir():
        if f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            shutil.copy2(f, tmp / f.name)
    return tmp / PCB.name


@pytest.mark.slow
def test_live_rip_and_refill_names_the_patch(tmp_path, kicad):
    import kc
    import route_edit
    pcb = _copy(tmp_path)
    route_edit.apply_ops(pcb, [{"op": "remove", "uuid": v.uuid}
                               for v in _pvdd(geom.BoardGeom.from_file(PCB))])
    kc.run_drc(kicad, pcb, refill=True, save_board=True)
    after = geom.BoardGeom.from_file(pcb)
    after.assert_fresh()
    assert _named(after) == [(PATCH, True, [])]


@pytest.mark.slow
def test_live_empty_patch_passes_fresh_and_fails_the_gate(tmp_path, kicad):
    """A PATCH_GND drawn inside a PVDD pad fills nothing after a refill:
    assert_fresh lets it through (KiCad's refill leaves it empty too),
    drc_routed now fails on it, and the zone_patch command it names takes
    it."""
    import env
    import kc
    import routelib
    import zone_patch
    pcb = _copy(tmp_path)
    b0 = geom.BoardGeom.from_file(pcb)
    pad = next(p for p in b0.pads_of(net="PVDD", layer="F.Cu")
               if p.poly.area > 1.0)
    c = pad.poly.centroid
    ring = [[c.x - 0.3, c.y - 0.3], [c.x + 0.3, c.y - 0.3],
            [c.x + 0.3, c.y + 0.3], [c.x - 0.3, c.y + 0.3]]
    bp = env.find_kicad_python(kicad)
    routelib.run_worker(bp, {
        "verb": "add_zones", "board": str(pcb), "out": str(pcb),
        "zones": [{"net": "GND", "layer": "F.Cu", "poly": ring,
                   "priority": 5, "clearance": 0.2, "min_width": 0.25,
                   "connect": "solid", "name": "PATCH_GND"}],
        "remove": []}, tmp_path)
    kc.run_drc(kicad, pcb, refill=True, save_board=True)
    bg = geom.load_board(pcb, refresh=True)
    bg.assert_fresh()               # empty by refill: not stale
    got = [p for p in bg.stale_patches() if p["name"] == "PATCH_GND"]
    assert [(p["no_via"], p["empty"]) for p in got] == [(True, ["F.Cu"])]
    # drc_routed as gates.yaml has it, less parity (no schematic here)
    g = gate.load_gates(REPO / "reference" / "gates.yaml")["drc_routed"]
    g = dict(g, drc_options=dict(g["drc_options"], parity=False))
    rep = gate.run_report_for_gate(g, pcb)
    mine = [v for v in rep["violations"] if v["check"] == "stale_patch"]
    assert len(mine) == 1 and mine[0]["severity"] == "warning"
    assert rep["counts"]["by_source"].get("gate") == 1
    # the fix the finding names works
    region = got[0]["box"]
    payload, _ = zone_patch.run(["--pcb", str(pcb), "--net", "GND",
                                 "--layer", "F.Cu", "--region", region,
                                 "--remove"])
    assert payload["removed"] == 1
    assert [p for p in geom.load_board(pcb, refresh=True).stale_patches()
            if p["name"] == "PATCH_GND"] == []
