"""#855: set_pad drill / shape / at - a slot or a grown hole on a placed part
(was lite-main's pinned eco_rca.py), with the ring and hole-to-hole check
after it.

Pure tests (venv) cover validation and the hole floors; smoke tests (SWIG
bundled python) apply the ops to the frozen routed pd-trigger board: a slot
lands and reads back, an unnumbered NPTH leg is reachable as pad "", a ring
below the .kicad_pro floor, a hole breaking out of its copper and a hole
moved too near another are refused with the board byte-identical, and the
quiet twins - a move away, a copper-only change beside a part's own close
holes that pre-date the edit - land.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import env  # noqa: E402
import geom  # noqa: E402
import place_edit  # noqa: E402
import placelib  # noqa: E402
from checklib import CheckError  # noqa: E402

PD_DIR = REPO / "tests" / "fixtures" / "stages" / "pd_trigger" / "route"
PD_PCB = PD_DIR / "pd-trigger.kicad_pcb"


def _ops(*ops):
    return place_edit.validate_ops({"version": 1, "ops": list(ops)})


def _pad(pcb, ref, num):
    return [p for p in place_edit._all_pads(geom.BoardGeom.from_file(pcb))
            if p.ref == ref and p.number == num]


# ------------------------------------------------------------ validation

def test_validate_takes_hole_fields():
    assert len(_ops(
        {"op": "set_pad", "ref": "J2", "pad": "2", "shape": "oval",
         "size": [2.5, 3.3], "drill": [1.6, 2.4]},
        {"op": "set_pad", "ref": "J2", "pad": "1", "drill": 1.7},
        {"op": "set_pad", "ref": "J3", "pad": "2", "at": [1.5, 0]},
        {"op": "set_pad", "ref": "J1", "pad": "", "drill": 0.8,
         "size": 0.8})) == 4


@pytest.mark.parametrize("op, msg", [
    ({"shape": "hexagon"}, "shape must be one of"),
    ({"drill": 0}, "drill must be"),
    ({"drill": [1, 2, 3]}, "drill must be"),
    ({"drill": True}, "drill must be"),
    ({"at": [1]}, "at must be"),
    ({"at": [1, 2], "pad": ["1", "2"]}, "at moves one pad"),
    ({"at": [1, 2], "pad": ""}, "at moves one pad"),
    ({"pad": "", "net": "GND"}, "carry no net"),
    ({"pad": 2, "drill": 1}, "pad must be"),
])
def test_validate_refuses_hole_fields(op, msg):
    full = {"op": "set_pad", "ref": "J2", "pad": "1", **op}
    with pytest.raises(CheckError, match=msg):
        _ops(full)


def test_hole_floors_read_the_project(tmp_path):
    assert place_edit._hole_floors(PD_PCB) == (0.1, 0.5)
    lone = tmp_path / "x.kicad_pcb"
    lone.write_text("")
    assert place_edit._hole_floors(lone) == (0.0, 0.25)  # KiCad stock


# ------------------------------------------------------------ smoke (KiCad)

@pytest.fixture
def kicad():
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")


@pytest.fixture
def pd(tmp_path):
    dst = tmp_path / "pdt"
    shutil.copytree(PD_DIR, dst)
    return dst / PD_PCB.name


def _apply(pcb, *ops):
    payload, _ = place_edit.run(["--pcb", str(pcb), "--allow-routed"]
                                + [a for o in ops
                                   for a in ("--op", json.dumps(o))])
    return payload


def test_slot_lands_and_reads_back(kicad, pd):
    # eco_rca's pin: a round 1.6 mm hole made a plated 1.6 x 2.4 slot
    payload = _apply(pd, {"op": "set_pad", "ref": "J2", "pad": "2",
                          "shape": "oval", "size": [2.5, 3.3],
                          "drill": [1.6, 2.4]})
    assert payload["verified"]
    [p] = _pad(pd, "J2", "2")
    assert (p.shape, p.size, p.drill) == ("oval", (2.5, 3.3), (1.6, 2.4))
    assert "(drill oval 1.6 2.4)" in pd.read_text()
    [q] = _pad(pd, "J2", "1")  # its neighbour untouched
    assert (q.shape, q.size) == ("circle", (2.4, 2.4))


def test_unnumbered_legs_grow(kicad, pd):
    # eco_rca's snap-in legs: NPTH pads have no number - pad "" names them
    # (0.8 would bring them within 0.49 mm of two vias: refused)
    payload = _apply(pd, {"op": "set_pad", "ref": "J1", "pad": "",
                          "drill": 0.7, "size": 0.7})
    assert payload["verified"]
    legs = _pad(pd, "J1", "")
    assert len(legs) == 2
    assert all(p.drill == (0.7, 0.7) and p.size == (0.7, 0.7) for p in legs)


def test_at_moves_the_pad_in_the_part_frame(kicad, pd):
    fp = placelib.PlaceModel(pd).footprints["J3"]
    _apply(pd, {"op": "set_pad", "ref": "J3", "pad": "2", "at": [1.5, 0]})
    [p] = _pad(pd, "J3", "2")
    dx, dy = geom._rot(1.5, 0, -fp.angle)
    assert math.dist(p.center, (fp.pos[0] + dx, fp.pos[1] + dy)) < 1e-3
    assert p.drill == pytest.approx((1.1, 1.1), abs=1e-3)  # hole moved too


@pytest.mark.parametrize("op, msg", [
    # (2.4 - 2.3) / 2 = 0.05 mm ring, below the board's 0.1 mm floor
    ({"ref": "J2", "pad": "1", "drill": 2.3}, r"annular ring 0\.050 mm"),
    ({"ref": "J2", "pad": "1", "drill": [1.6, 2.6]}, "breaks out"),
    # J3's pins 2.54 apart, 1.1 holes: pin 2 to 0.2 leaves 0.37 < 0.5
    ({"ref": "J3", "pad": "2", "at": [0.2, 0]},
     r"J3\.2 hole is 0\.370 mm from J3\.1's < min_hole_to_hole 0\.5"),
    # a hole grown, not moved, into its neighbour's floor: J3.1 a 3.4 mm
    # slot along the pin row leaves 2.54 - 1.7 - 0.55 = 0.29 mm
    ({"ref": "J3", "pad": "1", "shape": "oval", "size": [4.0, 1.8],
      "drill": [3.4, 1.1]}, r"J3\.1 hole is 0\.290 mm from J3\.2's"),
])
def test_hole_faults_refused_byte_identical(kicad, pd, op, msg):
    raw = pd.read_bytes()
    with pytest.raises(CheckError, match=msg):
        _apply(pd, {"op": "set_pad", **op})
    assert pd.read_bytes() == raw


def test_new_copper_short_refused(kicad, pd):
    # J3.2 [GND] moved 1.1 mm toward J3.1 [/VAUX]: the holes keep 0.34 mm
    # under a 0.3 floor, but the 1.8 mm pads overlap - KiCad's DRC calls
    # it shorting_items. The short is refused on its own.
    pro = pd.with_suffix(".kicad_pro")
    doc = json.loads(pro.read_text())
    doc["board"]["design_settings"]["rules"]["min_hole_to_hole"] = 0.3
    pro.write_text(json.dumps(doc))
    raw = pd.read_bytes()
    with pytest.raises(CheckError, match=r"J3\.2 \[GND\] copper now touches "
                       r"pad J3\.1 \[/VAUX\]") as e:
        _apply(pd, {"op": "set_pad", "ref": "J3", "pad": "2", "at": [0.17, 0]})
    assert "min_hole_to_hole" not in str(e.value)
    assert pd.read_bytes() == raw


def test_quiet_twins_land(kicad, pd):
    # the same pin moved AWAY from its neighbour; then the near pin's hole
    # moved but no nearer than the floor
    _apply(pd, {"op": "set_pad", "ref": "J3", "pad": "2", "at": [1.5, 0]})
    _apply(pd, {"op": "set_pad", "ref": "J3", "pad": "2", "at": [1.0, 0]})
    [p] = _pad(pd, "J3", "2")
    assert p.drill == pytest.approx((1.1, 1.1), abs=1e-3)


def test_close_holes_the_edit_did_not_make_stay_quiet(kicad, pd):
    # A fab floor of 1.0 mm puts J1's own USB-C holes (0.805 mm apart, the
    # footprint's) below it before any edit: growing a pin's copper, which
    # moves no hole, must still land - and a hole edit that brings no pair
    # closer too. Moving a hole nearer is still refused.
    pro = pd.with_suffix(".kicad_pro")
    doc = json.loads(pro.read_text())
    doc["board"]["design_settings"]["rules"]["min_hole_to_hole"] = 1.0
    pro.write_text(json.dumps(doc))
    holes = place_edit._holes(geom.BoardGeom.from_file(pd))
    j1 = [k for k in holes if k[0] == "J1"]
    assert min(holes[a].distance(holes[b]) for a in j1 for b in j1
               if a != b) < 1.0
    payload = _apply(pd, {"op": "set_pad", "ref": "J1", "pad": "1",
                          "size": [1.1, 2.2]})
    assert payload["verified"]
    _apply(pd, {"op": "set_pad", "ref": "J1", "pad": "1",
                "drill": [0.6, 1.7]})
    raw = pd.read_bytes()
    with pytest.raises(CheckError, match="min_hole_to_hole 1"):
        _apply(pd, {"op": "set_pad", "ref": "J1", "pad": "1",
                    "drill": [0.7, 1.8]})
    assert pd.read_bytes() == raw


def test_smd_pad_drill_refused(kicad, pd):
    raw = pd.read_bytes()
    smd = next(p for p in geom.BoardGeom.from_file(pd).pads_of()
               if p.kind == "smd")
    with pytest.raises(CheckError, match="SMD"):
        _apply(pd, {"op": "set_pad", "ref": smd.ref, "pad": smd.number,
                    "drill": 0.3})
    assert pd.read_bytes() == raw
