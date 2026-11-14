"""S11 acceptance tests: stitch_vias (pad stitch, area grid, via fence).

Pure tests exercise the pitch formula (values pinned), candidate generation
(ring/grid/fence) and the clearance/hole checkers on synthetic shapely
scenes plus hand-written .kicad_pcb fixtures (dry-run needs no toolchain).
Tests that build the blinky2+pour corpus board and apply vias through the
SWIG edit path carry `smoke`.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, Polygon, box

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GOLDEN = REPO / "tests" / "golden"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import env  # noqa: E402
import geom  # noqa: E402
import kc  # noqa: E402
import route_edit  # noqa: E402
import stitch_vias  # noqa: E402


# ============================================================ pure: pitch

def test_pitch_pinned_values():
    p, t = stitch_vias.rise_pitch_mm({})
    assert t == 1.0
    assert p == pytest.approx(4.283, abs=0.001)   # plan-pinned default
    p, _t = stitch_vias.rise_pitch_mm(None)
    assert p == pytest.approx(4.283, abs=0.001)


def test_pitch_takes_fastest_rise_and_clamps():
    p, t = stitch_vias.rise_pitch_mm({"high_speed": [
        {"net": "a", "t_rise_ns": 2.0}, {"net": "b", "t_rise_ns": 0.5},
        {"net": "c"}]})
    assert t == 0.5
    assert p == pytest.approx(2.141, abs=0.001)
    p, _ = stitch_vias.rise_pitch_mm(
        {"high_speed": [{"net": "a", "t_rise_ns": 4.0}]})
    assert p == 15.0                                # upper clamp
    p, _ = stitch_vias.rise_pitch_mm(
        {"high_speed": [{"net": "a", "t_rise_ns": 0.2}]})
    assert p == 2.0                                 # lower clamp


def test_fence_pitch_clamp():
    p, _ = stitch_vias.rise_pitch_mm(
        {"high_speed": [{"net": "a", "t_rise_ns": 4.0}]},
        stitch_vias.FENCE_CLAMP)
    assert p == 10.0
    p, _ = stitch_vias.rise_pitch_mm({}, stitch_vias.FENCE_CLAMP)
    assert p == pytest.approx(4.283, abs=0.001)


# ============================================================ pure: candidates

def test_ring_candidates_deterministic_order():
    c = stitch_vias.ring_candidates((10, 10), 0.0)
    assert len(c) == 24
    assert c[0] == (10.65, 10.0)                    # nearest, outward first
    assert c == stitch_vias.ring_candidates((10, 10), 0.0)
    for x, y in c[:8]:                              # first ring radius 0.65
        assert math.hypot(x - 10, y - 10) == pytest.approx(0.65, abs=0.002)
    c90 = stitch_vias.ring_candidates((10, 10), 90.0)
    assert c90[0] == (10.0, 10.65)


def test_ring_candidates_start_at_a_big_pad_edge():
    """A 3 x 3 mm pad reaches 1.5 mm from its centre: rings measured from the
    centre (0.65 mm) put the via inside it. From the edge, the first ring
    puts the drill edge on the pad edge, the others step out by the same
    0.25 mm as before."""
    big = box(8.5, 8.5, 11.5, 11.5)
    c = stitch_vias.ring_candidates((10, 10), 0.0, pad_poly=big)
    assert c[0] == (11.65, 10.0)
    assert c[8] == (11.9, 10.0) and c[16] == (12.15, 10.0)
    for x, y in c:
        assert big.distance(Point(x, y)) >= 0.15 - 1e-3, (x, y)
    # a 1.0 mm pad keeps its 0.65 mm ring square to its sides; the 45 deg
    # spots, whose drill sat on the pad's corner, step out past it
    small = box(9.5, 9.5, 10.5, 10.5)
    cs = stitch_vias.ring_candidates((10, 10), 0.0, pad_poly=small)
    assert cs[0] == (10.65, 10.0) and cs[3] == (10.0, 10.65)
    for x, y in cs:
        assert small.distance(Point(x, y)) >= 0.15 - 1e-3, (x, y)


def test_dry_run_keeps_the_drill_off_a_big_pad(tmp_path):
    """LEARNINGS: a stitch via inside its own big pad is an open barrel
    under the paste (JLC economy PCBA neither fills nor plugs)."""
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "GND", size=3.0))
    body += _zone(1, "GND", "B.Cu", 1, 1, 29, 29)
    pcb = _board(tmp_path, "bigpad", body)
    payload, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--dry-run", "--pads-only"])
    vias = [op for op in payload["ops"] if op["op"] == "add_via"]
    assert len(vias) == 1
    pad = box(8.5, 8.5, 11.5, 11.5)
    assert pad.distance(Point(vias[0]["at"])) >= 0.15 - 1e-6
    # the disc still bites the pad, so no stub track is needed
    assert Point(vias[0]["at"]).buffer(0.3).intersects(pad)
    assert [op for op in payload["ops"] if op["op"] == "add_track"] == []


def test_outward_deg():
    assert stitch_vias.outward_deg((11, 10), (10, 10)) == pytest.approx(0.0)
    assert stitch_vias.outward_deg((10, 11), (10, 10)) == pytest.approx(90.0)
    assert stitch_vias.outward_deg((10, 10), (10, 10)) == 0.0  # lone pad


def test_grid_candidates_inside_sorted_keepouts_margin():
    fill = box(0, 0, 20, 20)
    pts = stitch_vias.grid_candidates(fill, fill, [], 5.0)
    assert pts == sorted(pts)
    assert len(pts) == 16
    assert pts[0] == (2.5, 2.5) and pts[-1] == (17.5, 17.5)
    # keepout excludes its quadrant
    pts2 = stitch_vias.grid_candidates(fill, fill, [box(0, 0, 10, 10)], 5.0)
    assert len(pts2) == 12
    assert all(not (x < 10 and y < 10) for x, y in pts2)
    # a sliver thinner than 2x the edge margin yields nothing
    sliver = box(0, 0, 20, 1.8)
    assert stitch_vias.grid_candidates(sliver, sliver, [], 5.0) == []
    assert stitch_vias.grid_candidates(Polygon(), fill, [], 5.0) == []


def test_fence_points_offsets_and_pitch():
    line = LineString([(0, 0), (10, 0)])
    pts = stitch_vias.fence_points(line, 0.2, 2.0)  # default offset 0.7
    assert len(pts) == 10                            # 5 samples x 2 sides
    assert pts[0] == (1.0, 0.7) and pts[1] == (1.0, -0.7)
    assert sorted({x for x, _y in pts}) == [1.0, 3.0, 5.0, 7.0, 9.0]
    assert all(abs(abs(y) - 0.7) < 1e-9 for _x, y in pts)
    # vertical track -> horizontal offsets
    ptsv = stitch_vias.fence_points(LineString([(5, 0), (5, 10)]), 0.2, 2.0,
                                    offset=1.0)
    assert all(abs(abs(x - 5) - 1.0) < 1e-9 for x, _y in ptsv)
    # segment shorter than pitch -> midpoint pair
    ptss = stitch_vias.fence_points(LineString([(0, 0), (1, 0)]), 0.3, 5.0)
    assert ptss == [(0.5, 0.9), (0.5, -0.9)]         # offset 2*0.3+0.3


# ============================================================ pure: scene

def _scene(clearance=0.2, holes=()):
    foreign = box(9, 9, 11, 11)   # non-target copper on F.Cu only

    def fof(layer, net):
        return foreign if layer == "F.Cu" else Polygon()

    sc = stitch_vias.Scene(["F.Cu", "B.Cu"], fof, box(0, 0, 20, 20),
                           clearance)
    # T6: holes are DRILL EXTENTS; (x, y) shorthand = a standard 0.3 drill
    # (same verdicts as the old 0.5 mm centre floor for standard vias)
    sc.holes = [Point(x, y).buffer(0.15, quad_segs=16) for x, y in holes]
    return sc


def test_scene_via_check_reasons():
    sc = _scene(holes=[(5.0, 5.0)])
    r = 0.3
    assert sc.via_check(10.0, 10.0, r, "GND", 0.5) == "foreign_copper"
    assert sc.via_check(11.3, 10.0, r, "GND", 0.5) == "foreign_copper"
    assert sc.via_check(5.4, 5.0, r, "GND", 0.5) == "hole_to_hole"
    assert sc.via_check(0.3, 10.0, r, "GND", 0.5) == "edge"
    assert sc.via_check(25.0, 10.0, r, "GND", 0.5) == "edge"  # outside
    assert sc.via_check(5.6, 5.0, r, "GND", 0.5) is None
    assert sc.via_check(12.0, 10.0, r, "GND", 0.5) is None


def test_scene_sees_committed_copper():
    sc = _scene()
    r = 0.3
    sc.commit_via(12.0, 10.0, r, "GND")
    # new drill enforces 0.5 mm centre spacing for everyone
    assert sc.via_check(12.3, 10.0, r, "GND", 0.5) == "hole_to_hole"
    # new copper is foreign to OTHER nets only
    assert sc.via_check(12.6, 10.0, r, "+3V3", 0.5) == "foreign_copper"
    assert sc.via_check(14.0, 10.0, r, "+3V3", 0.5) is None
    assert sc.via_check(13.0, 10.0, r, "GND", 0.5) is None


def test_scene_track_corridor():
    sc = _scene()
    # corridor half-width = 0.2/2 + 0.2 clearance = 0.3; foreign starts y=9
    assert sc.track_ok((9, 8.6), (11, 8.6), 0.2, "GND", "F.Cu") is True
    assert sc.track_ok((9, 8.75), (11, 8.75), 0.2, "GND", "F.Cu") is False
    assert sc.track_ok((9, 8.75), (11, 8.75), 0.2, "GND", "B.Cu") is True
    # flat caps: the corridor ends where the track ends (a round cap would
    # poke past x=8.8 into the box at x=9)
    assert sc.track_ok((7, 10), (8.8, 10), 0.2, "GND", "F.Cu") is True
    # committed foreign copper blocks the corridor too
    sc.commit_track((2, 5), (8, 5), 0.2, "SIG", "F.Cu")
    assert sc.track_ok((2, 5.2), (8, 5.2), 0.2, "GND", "F.Cu") is False


# ============================================================ pure: drill floor

def test_hole_floor_is_drill_edge_aware():
    """T6 (P7A-4/P7B-4): the floor is edge-to-edge against real drill
    extents. A 1.0 mm drill 0.5 mm away passed the old centre-only model
    with the holes overlapping 0.1 mm."""
    sc = _scene()
    sc.holes.append(Point(5, 5).buffer(0.5, quad_segs=16))  # 1.0 mm drill
    r = 0.3
    assert sc.via_check(5.5, 5.0, r, "GND", 0.5) == "hole_to_hole"
    # edge gap 0.2 + new drill r 0.15 -> centre must be >= 0.85 from centre
    assert sc.via_check(5.84, 5.0, r, "GND", 0.5) == "hole_to_hole"
    assert sc.via_check(5.86, 5.0, r, "GND", 0.5) is None
    # two standard 0.3 drills at 0.5 mm centres still accept exactly
    sc2 = _scene(holes=[(5.0, 5.0)])
    assert sc2.via_check(5.5, 5.0, r, "GND", 0.5) is None
    assert sc2.via_check(5.49, 5.0, r, "GND", 0.5) == "hole_to_hole"


def test_slot_drill_extents_are_seen():
    """A slot (oval) drill rejects along its long axis where the centre
    model accepted (0.9 mm centre distance >= 0.5 passed before)."""
    sc = _scene()
    # 0.6 x 2.0 slot centred at (5, 5), long axis vertical
    sc.holes.append(LineString([(5, 4.3), (5, 5.7)]).buffer(0.3,
                                                            quad_segs=16))
    r = 0.3
    assert sc.via_check(5.0, 6.2, r, "GND", 0.5) == "hole_to_hole"  # 0.2 edge
    assert sc.via_check(5.0, 6.6, r, "GND", 0.5) is None            # 0.6 edge
    assert sc.via_check(5.9, 5.0, r, "GND", 0.5) is None            # short axis


def test_build_scene_reads_pad_drills_with_rotation(tmp_path):
    """geom.Pad.drill_poly reaches the scene: a rotated THT oval pad's slot
    extents reject candidates along the slot axis (LEARNINGS 819/827 - the
    pd-trigger case shipped 0.12 mm hole-edge spacing DRC-green)."""
    body = _fp("J9", 15, 15,
               '    (pad "1" thru_hole oval (at 0 0 90) (size 1.6 3.2)'
               ' (drill oval 0.8 2.4) (layers "*.Cu") (net "SIG"))\n')
    body += _zone(1, "GND", "B.Cu", 1, 1, 29, 29)
    pcb = _board(tmp_path, "slotpad", body)
    bg = geom.BoardGeom.from_file(pcb)
    sc = stitch_vias.build_scene(bg, 0.2)
    r = 0.3
    # slot spans x 13.8..16.2 after the -angle rotation; hole edge at
    # 16.32 is 0.12 mm out -> rejected (old centre model: 1.62 >= 0.5 ok)
    assert sc.via_check(16.32, 15.0, r, "GND", 0.5) == "hole_to_hole"
    # >= 0.35 mm from the slot edge is drill-legal again (16.2 + 0.35)
    assert sc.via_check(16.56, 15.0, r, "GND", 0.5) != "hole_to_hole"
    # far from copper AND drill: fully legal
    assert sc.via_check(19.0, 15.0, r, "GND", 0.5) is None


def test_dry_run_respects_tht_drill_edges(tmp_path):
    """End-to-end: no planned via may land inside the edge floor of a big
    THT drill (the class KiCad DRC cannot see - LEARNINGS 819)."""
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "GND"))
    body += _fp("J1", 12, 10,
                '    (pad "1" thru_hole circle (at 0 0) (size 2.0 2.0)'
                ' (drill 1.2) (layers "*.Cu") (net "SIG"))\n')
    body += _zone(1, "GND", "B.Cu", 1, 1, 29, 29)
    pcb = _board(tmp_path, "bigdrill", body)
    payload, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--dry-run", "--pitch", "8"])
    assert payload["status"] == "pass"
    drill_edge = Point(12, 10).buffer(0.6)
    for op in payload["ops"]:
        assert op["op"] == "add_via"
        # centre >= edge_gap(0.2) + new drill r (0.15) from the drill edge
        assert drill_edge.distance(Point(op["at"])) >= 0.35 - 1e-9, op


# ============================================================ pure: CLI fixtures

PCB_HDR = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (net 0 "") (net 1 "GND") (net 2 "SIG") (net 3 "+3V3")
"""


def _fp(ref: str, x: float, y: float, pads: str) -> str:
    return (f'  (footprint "t:{ref}" (layer "F.Cu")\n'
            f'    (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            f'{pads})\n')


def _pad(num: str, x: float, y: float, net: str, size: float = 1.0) -> str:
    return (f'    (pad "{num}" smd rect (at {x} {y}) (size {size} {size})'
            f' (layers "F.Cu") (net "{net}"))\n')


def _seg(x1, y1, x2, y2, w, layer, net) -> str:
    return (f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width {w})'
            f' (layer "{layer}") (net "{net}"))\n')


def _zone(net_id: int, net_name: str, layer: str, x1, y1, x2, y2) -> str:
    pts = f"(xy {x1} {y1}) (xy {x2} {y1}) (xy {x2} {y2}) (xy {x1} {y2})"
    return (f'  (zone (net {net_id}) (net_name "{net_name}")'
            f' (layer "{layer}")\n'
            f'    (connect_pads (clearance 0.3)) (min_thickness 0.25)\n'
            f'    (polygon (pts {pts}))\n'
            f'    (filled_polygon (layer "{layer}") (pts {pts})))\n')


def _board(tmp_path: Path, name: str, body: str, size: float = 30.0) -> Path:
    text = (PCB_HDR
            + f'  (gr_rect (start 0 0) (end {size} {size})'
            f' (stroke (width 0.1)) (fill no) (layer "Edge.Cuts"))\n'
            + body + ")\n")
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


def _basic_board(tmp_path: Path) -> Path:
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "GND"))
    body += _fp("R1", 24, 24, _pad("1", 0, 0, "SIG"))
    body += _zone(1, "GND", "B.Cu", 1, 1, 29, 29)
    return _board(tmp_path, "basic", body)


# ============================================================ pure: stitch CLI

def test_npth_holes_are_drills_too(tmp_path):
    """A copper-less NPTH hole (geom.npth) is a drill to keep clear of."""
    body = _fp("J1", 15, 15,
               '    (pad "" np_thru_hole circle (at 0 0) (size 1.0 1.0)'
               ' (drill 1.0) (layers "*.Mask"))\n')
    body += _zone(1, "GND", "B.Cu", 1, 1, 29, 29)
    pcb = _board(tmp_path, "npth", body)
    bg = geom.BoardGeom.from_file(pcb)
    assert [p.drill for p in bg.npth] == [(1.0, 1.0)]
    assert not bg.pads_of(ref="J1")
    sc = stitch_vias.build_scene(bg, 0.2, hole_gap=0.5)
    r = 0.3
    # hole edge at 15.5; legal from 15.5 + 0.5 + 0.15
    assert sc.via_check(16.14, 15.0, r, "GND", 0.5) == "hole_to_hole"
    assert sc.via_check(16.16, 15.0, r, "GND", 0.5) is None


def test_dry_run_pad_and_area_stitch(tmp_path):
    pcb = _basic_board(tmp_path)
    raw = pcb.read_bytes()
    payload, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--dry-run", "--pitch", "5"])
    assert payload["status"] == "pass"
    assert payload["mode"] == "stitch"
    assert payload["pitch_mm"] == 5.0
    ops = payload["ops"]
    assert ops and ops[0] == {"op": "add_via", "at": [10.65, 10.0],
                              "size": 0.6, "drill": 0.3, "net": "GND"}
    gnd = payload["nets"]["GND"]
    assert gnd["plane_layers"] == ["B.Cu"]
    assert gnd["pads"] == {"requested": 1, "placed": 1, "skipped": 0}
    # a single B.Cu plane means grid vias would dangle -> all rejected
    assert gnd["area"]["placed"] == 0
    assert gnd["area"]["rejected"].get("single_layer_contact", 0) >= 10
    assert payload["placed"] == gnd["pads"]["placed"] + gnd["area"]["placed"]
    assert payload["placed"] == len(ops)             # all vias, no tracks
    # no via may sit inside the foreign SIG pad + clearance
    sig_pad = box(23.5, 23.5, 24.5, 24.5)
    for op in ops:
        assert sig_pad.distance(Point(op["at"])) >= 0.3 + 0.2 - 1e-9
    # ops validate against the route_edit schema
    route_edit.validate_ops({"version": 1, "ops": ops})
    # dry run never touches the board
    assert pcb.read_bytes() == raw


def test_dry_run_deterministic(tmp_path):
    pcb = _basic_board(tmp_path)
    p1, _ = stitch_vias.run(["--pcb", str(pcb), "--dry-run", "--pitch", "5"])
    p2, _ = stitch_vias.run(["--pcb", str(pcb), "--dry-run", "--pitch", "5"])
    assert json.dumps(p1["ops"]) == json.dumps(p2["ops"])


def test_already_stitched_pad_is_skipped(tmp_path):
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "GND"))
    body += _zone(1, "GND", "B.Cu", 1, 1, 29, 29)
    body += ('  (via (at 10.65 10) (size 0.6) (drill 0.3)'
             ' (layers "F.Cu" "B.Cu") (net "GND"))\n')
    pcb = _board(tmp_path, "stitched", body)
    payload, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--dry-run", "--pitch", "8"])
    gnd = payload["nets"]["GND"]
    assert gnd["pads"]["requested"] == 0
    assert {"ref": "U1.1", "reason": "already_stitched",
            "net": "GND"} in payload["skipped"]
    assert not payload["violations"]


def test_net_without_plane_reported_not_error(tmp_path):
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "GND"))
    body += _fp("R1", 24, 24, _pad("1", 0, 0, "SIG"))
    pcb = _board(tmp_path, "noplane", body)
    rc = stitch_vias.main(["--pcb", str(pcb), "--dry-run"])
    assert rc == 0
    payload, _ = stitch_vias.run(["--pcb", str(pcb), "--dry-run"])
    assert payload["status"] == "pass"
    assert payload["placed"] == 0 and payload["ops"] == []
    assert "no plane" in payload["nets"]["GND"]["note"]


def test_area_grid_on_two_layer_fill(tmp_path):
    # GND pours on BOTH layers: grid vias bond both -> placed at pitch
    body = _zone(1, "GND", "F.Cu", 1, 1, 29, 29)
    body += _zone(1, "GND", "B.Cu", 1, 1, 29, 29)
    pcb = _board(tmp_path, "areagrid", body)
    payload, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--dry-run", "--pitch", "5"])
    gnd = payload["nets"]["GND"]
    assert sorted(gnd["plane_layers"]) == ["B.Cu", "F.Cu"]
    assert gnd["pads"]["requested"] == 0
    assert gnd["area"]["candidates"] == 36           # 6 x 6 at pitch 5
    assert gnd["area"]["placed"] == 36 == payload["placed"]
    ats = [tuple(op["at"]) for op in payload["ops"]]
    assert ats == sorted(ats)                        # grid order by (x, y)
    # --max-vias caps the grid deterministically
    p2, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--dry-run", "--pitch", "5", "--max-vias", "10"])
    assert p2["nets"]["GND"]["area"]["placed"] == 10
    assert p2["nets"]["GND"]["area"]["capped"] is True
    assert p2["ops"] == payload["ops"][:10]


def test_multi_net_power_first_gnd_last(tmp_path):
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "GND"))
    body += _fp("U2", 10, 20, _pad("1", 0, 0, "+3V3"))
    body += _zone(1, "GND", "B.Cu", 1, 1, 29, 14)
    body += _zone(3, "+3V3", "B.Cu", 1, 16, 29, 29)
    pcb = _board(tmp_path, "multinet", body)
    payload, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--dry-run", "--pitch", "6",
         "--nets", "GND,+3V3"])
    # GND processed last even though listed first
    assert list(payload["nets"].keys()) == ["+3V3", "GND"]
    assert payload["nets"]["+3V3"]["pads"]["placed"] == 1
    assert payload["nets"]["GND"]["pads"]["placed"] == 1
    net_order = [op["net"] for op in payload["ops"]]
    assert net_order.index("+3V3") < net_order.index("GND")


def test_pads_only_skips_the_area_grid(tmp_path):
    """--pads-only (#923 point 1): the pad vias, none of the grid."""
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "GND"))
    body += _zone(1, "GND", "In1.Cu", 1, 1, 29, 29)
    body += _zone(1, "GND", "B.Cu", 1, 1, 29, 29)
    pcb = tmp_path / "padsonly.kicad_pcb"
    pcb.write_text(PCB4_HDR + '  (gr_rect (start 0 0) (end 30 30) (stroke'
                   ' (width 0.1)) (fill no) (layer "Edge.Cuts"))\n'
                   + body + ")\n", encoding="utf-8")
    full, _ = stitch_vias.run(["--pcb", str(pcb), "--dry-run", "--pitch", "4"])
    assert full["nets"]["GND"]["area"]["placed"] > 0   # the grid has room
    payload, _ = stitch_vias.run(["--pcb", str(pcb), "--dry-run", "--pitch",
                                  "4", "--pads-only"])
    gnd = payload["nets"]["GND"]
    assert payload["pads_only"] is True
    assert gnd["pads"]["placed"] == 1
    assert gnd["area"]["candidates"] == 0 and gnd["area"]["placed"] == 0
    assert payload["ops"] == full["ops"][:1]


PCB4_HDR = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal)
    (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (net 0 "") (net 1 "GND") (net 2 "SIG") (net 3 "+3V3") (net 4 "SW")
  (net 5 "+5V") (net 6 "+1V8")
"""


def _power_board(tmp_path: Path) -> tuple[Path, Path]:
    """4 layers: +3V3 has an In2 plane, SW (a switch node) an In1 patch,
    +5V only an F.Cu pour, +1V8 no pour at all. U9 stands in for a BGA."""
    body = _fp("U1", 6, 6, _pad("1", 0, 0, "+3V3") + _pad("2", 0, 3, "SW")
               + _pad("3", 0, 6, "+5V") + _pad("4", 0, 9, "+1V8"))
    body += _fp("U9", 20, 6, _pad("1", 0, 0, "+3V3"))
    body += _zone(3, "+3V3", "In2.Cu", 1, 1, 29, 29)
    body += _zone(4, "SW", "In1.Cu", 4, 7, 8, 11)
    body += _zone(5, "+5V", "F.Cu", 10, 20, 14, 24)
    text = (PCB4_HDR + '  (gr_rect (start 0 0) (end 30 30) (stroke (width'
            ' 0.1)) (fill no) (layer "Edge.Cuts"))\n' + body + ")\n")
    pcb = tmp_path / "power.kicad_pcb"
    pcb.write_text(text, encoding="utf-8")
    cons = tmp_path / "constraints.json"
    cons.write_text(json.dumps({"power": [
        {"net": "+5V", "current_a": 1.0},
        {"net": "SW", "current_a": 2.0, "switch_node": True},
        {"net": "+3V3", "current_a": 1.0},
        {"net": "+1V8", "current_a": 0.5},
        {"net": "NOT_ON_BOARD", "current_a": 1.0}]}), encoding="utf-8")
    return pcb, cons


def test_power_plane_nets_are_inner_plane_power_only(tmp_path):
    """Only a power net with an inner plane counts: a switch node, an
    outer-only pour, no pour and an absent net are all left out."""
    pcb, cons = _power_board(tmp_path)
    bg = geom.BoardGeom.from_file(pcb)
    c = json.loads(cons.read_text())
    assert stitch_vias.power_plane_nets(c, bg) == ["+3V3"]
    # planted fault: drop the switch_node flag and SW's inner patch counts
    c["power"][1].pop("switch_node")
    assert stitch_vias.power_plane_nets(c, bg) == ["SW", "+3V3"]
    assert stitch_vias.power_plane_nets({}, bg) == []
    assert stitch_vias.power_plane_nets(None, bg) == []


def test_nets_power_stitches_power_pads_and_skips_refs(tmp_path):
    pcb, cons = _power_board(tmp_path)
    payload, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--constraints", str(cons), "--dry-run",
         "--nets", "power", "--pads-only", "--skip-refs", "U9"])
    assert list(payload["nets"]) == ["+3V3"]
    p3 = payload["nets"]["+3V3"]
    assert p3["pads"] == {"requested": 1, "placed": 1, "skipped": 0}
    vias = [op for op in payload["ops"] if op["op"] == "add_via"]
    assert len(vias) == 1 and vias[0]["net"] == "+3V3"
    assert math.hypot(vias[0]["at"][0] - 6, vias[0]["at"][1] - 6) < 1.5
    # without --skip-refs the stand-in BGA's pad is stitched too
    p2, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--constraints", str(cons), "--dry-run",
         "--nets", "power", "--pads-only"])
    assert p2["nets"]["+3V3"]["pads"]["placed"] == 2
    # no inner-plane power net at all: an empty pass, not an error
    cons.write_text(json.dumps({"power": [{"net": "+5V"}]}))
    p3, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--constraints", str(cons), "--dry-run",
         "--nets", "power"])
    assert p3["status"] == "pass" and p3["ops"] == [] and p3["nets"] == {}


def test_stitch_impossible_violation_and_strict_gate(tmp_path):
    # SIG ring boxes the GND pad in: no ring candidate can clear it
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "GND"))
    w = 0.6
    body += _seg(9.0, 8.7, 9.0, 11.3, w, "F.Cu", "SIG")
    body += _seg(11.0, 8.7, 11.0, 11.3, w, "F.Cu", "SIG")
    body += _seg(8.7, 9.0, 11.3, 9.0, w, "F.Cu", "SIG")
    body += _seg(8.7, 11.0, 11.3, 11.0, w, "F.Cu", "SIG")
    body += _zone(1, "GND", "B.Cu", 1, 1, 29, 29)
    pcb = _board(tmp_path, "boxed", body)
    payload, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--dry-run", "--pitch", "8"])
    assert payload["status"] == "pass"               # advisory by default
    assert [v["kind"] for v in payload["violations"]] == ["stitch_impossible"]
    v = payload["violations"][0]
    assert v["severity"] == "warning" and v["net"] == "GND"
    assert v["refs"] == ["U1"]
    assert {"ref": "U1.1", "reason": "no_clear_spot",
            "net": "GND"} in payload["skipped"]
    assert payload["nets"]["GND"]["area"]["placed"] == 0  # one-plane board
    assert stitch_vias.main(
        ["--pcb", str(pcb), "--dry-run", "--pitch", "8"]) == 0
    assert stitch_vias.main(
        ["--pcb", str(pcb), "--dry-run", "--pitch", "8", "--strict"]) == 1


def test_track_connected_pad_is_not_stitch_impossible(tmp_path):
    """S14: a boxed-in pad that already carries a same-net TRACK is connected
    to its net elsewhere - advisory skip, never stitch_impossible (both live
    P7 runs false-positived on track-connected LQFP GND pins)."""
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "GND"))
    w = 0.6
    body += _seg(9.0, 8.7, 9.0, 11.3, w, "F.Cu", "SIG")
    body += _seg(11.0, 8.7, 11.0, 11.3, w, "F.Cu", "SIG")
    body += _seg(8.7, 9.0, 11.3, 9.0, w, "F.Cu", "SIG")
    body += _seg(8.7, 11.0, 11.3, 11.0, w, "F.Cu", "SIG")
    # the GND pad carries its own track (router already connected it)
    body += _seg(10.0, 10.0, 10.6, 10.6, 0.2, "F.Cu", "GND")
    body += _zone(1, "GND", "B.Cu", 1, 1, 29, 29)
    pcb = _board(tmp_path, "boxedtrk", body)
    payload, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--dry-run", "--pitch", "8"])
    assert payload["status"] == "pass"
    assert payload["violations"] == []               # no stitch_impossible
    assert {"ref": "U1.1", "reason": "track_connected_no_local_spot",
            "net": "GND"} in payload["skipped"]


# ============================================================ pure: fence CLI

def test_fence_dry_run(tmp_path):
    pcb = _basic_board(tmp_path)
    body_extra = _seg(5, 15, 25, 15, 0.3, "F.Cu", "SIG")
    pcb.write_text(pcb.read_text(encoding="utf-8").replace(
        "  (zone", body_extra + "  (zone", 1), encoding="utf-8")
    payload, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--fence-net", "SIG", "--fence-pitch", "2",
         "--dry-run"])
    assert payload["mode"] == "fence"
    assert payload["fence_net"] == "SIG" and payload["via_net"] == "GND"
    assert payload["pitch_mm"] == 2.0
    ops = payload["ops"]
    assert len(ops) == 20 and payload["placed"] == 20
    for op in ops:
        assert op["op"] == "add_via" and op["net"] == "GND"
        assert abs(op["at"][1] - 15) == pytest.approx(0.9)  # 2*0.3 + 0.3
    p2, _ = stitch_vias.run(
        ["--pcb", str(pcb), "--fence-net", "SIG", "--fence-pitch", "2",
         "--dry-run"])
    assert json.dumps(ops) == json.dumps(p2["ops"])


def test_fence_net_without_tracks_is_error(tmp_path):
    pcb = _basic_board(tmp_path)
    rc = stitch_vias.main(
        ["--pcb", str(pcb), "--fence-net", "SIG", "--dry-run"])
    assert rc == 2


def test_missing_board_exits_2():
    assert stitch_vias.main(["--pcb", "no/such/board.kicad_pcb"]) == 2


def test_unknown_net_exits_2(tmp_path):
    pcb = _basic_board(tmp_path)
    assert stitch_vias.main(
        ["--pcb", str(pcb), "--nets", "NOPE", "--dry-run"]) == 2


# ============================================================ smoke: corpus

@pytest.fixture(scope="session")
def cli() -> Path:
    c = env.find_kicad_cli()
    if c is None:
        pytest.skip("kicad-cli not installed")
    return c


@pytest.fixture(scope="session")
def pour_board(cli, tmp_path_factory) -> Path:
    """blinky2 netlist -> board_init -> place_seed --apply -> B.Cu GND pour
    (the S10-style placed-unrouted fixture with a plane). Never mutated by
    tests: mutating tests copy the whole kicad dir first."""
    import board_init
    import place_seed
    import routelib
    bp = env.find_kicad_python(cli)
    if bp is None:
        pytest.skip("KiCad bundled python not found")
    d = tmp_path_factory.mktemp("stitchpour")
    rc = board_init.main([
        "--netlist", str(REPO / "tests" / "s7_regen" / "blinky2" / "kicad"
                         / "blinky2.net"),
        "--name", "blinky2", "--out", str(d / "kicad"), "--layers", "2",
        # the fence test draws along the edge, where default GND holes sit
        "--mounting-holes", "0"])
    assert rc == 0
    pcb = d / "kicad" / "blinky2.kicad_pcb"
    for name in ("constraints.json", "decoupling.json"):
        shutil.copy2(GOLDEN / "blinky2" / name, pcb.parent / name)
    payload, _ = place_seed.run([
        "--pcb", str(pcb), "--ops-out", str(d / "seed_ops.json"), "--apply"])
    assert payload["status"] == "pass"
    # B.Cu GND pour over the whole outline; SWIG saves a default .kicad_pro
    # next to the saved board, so stage and move only the .kicad_pcb back.
    bg = geom.BoardGeom.from_file(pcb)
    minx, miny, maxx, maxy = bg.outline.bounds
    stage = d / "zstage"
    stage.mkdir()
    staged = stage / pcb.name
    shutil.copy2(pcb, staged)
    routelib.run_worker(bp, {
        "verb": "add_zones", "board": str(staged), "out": str(staged),
        "zones": [{"net": "GND", "layer": "B.Cu",
                   "rect": [minx, miny, maxx, maxy]}]}, stage)
    os.replace(staged, pcb)
    kc.run_drc(cli, pcb, refill=True, save_board=True)
    bg2 = geom.BoardGeom.from_file(pcb)
    assert bg2.zone_fill("GND", "B.Cu").area > 100.0
    return pcb


def _copy_board(pcb: Path, dst: Path) -> Path:
    for src in list(pcb.parent.glob(pcb.stem + ".*")) + [
            pcb.parent / "constraints.json", pcb.parent / "decoupling.json"]:
        if src.is_file() and not src.name.endswith(".lck"):
            shutil.copy2(src, dst / src.name)
    return dst / pcb.name


def _non_unconnected(report: dict) -> Counter:
    return Counter(v["check"] for v in report["violations"]
                   if v["source"] != "unconnected")


@pytest.mark.smoke
def test_live_gnd_pad_stitch_applies_clean(pour_board, cli, tmp_path):
    """ACCEPTANCE: pad stitching places vias through route_edit + refill and
    the post-refill DRC gains no non-unconnected violations."""
    board = _copy_board(pour_board, tmp_path)
    before = geom.BoardGeom.from_file(board)
    n_gnd_before = len(before.vias_of(net="GND"))
    base = _non_unconnected(kc.run_drc(cli, board))
    rep = tmp_path / "stitch.json"
    rc = stitch_vias.main([
        "--pcb", str(board), "--nets", "GND",
        "--constraints", str(board.parent / "constraints.json"),
        "--out-report", str(rep)])
    assert rc == 0
    r = json.loads(rep.read_text("utf-8"))
    assert r["status"] == "pass"
    assert r["applied"] is True and r["refilled"] is True
    assert r["pitch_mm"] == pytest.approx(4.283, abs=0.001)  # 1 ns default
    gnd = r["nets"]["GND"]
    assert gnd["pads"]["placed"] >= 1, gnd
    assert gnd["pads"]["requested"] == (gnd["pads"]["placed"]
                                        + gnd["pads"]["skipped"])
    assert r["placed"] == gnd["pads"]["placed"] + gnd["area"]["placed"]
    after_bg = geom.BoardGeom.from_file(board)
    assert len(after_bg.vias_of(net="GND")) == n_gnd_before + r["placed"]
    # clearance respected: DRC shows nothing new beyond unconnected items
    after = _non_unconnected(kc.run_drc(cli, board))
    assert not (after - base), dict(after - base)


@pytest.mark.smoke
def test_live_dry_run_deterministic_and_readonly(pour_board):
    raw = pour_board.read_bytes()
    p1, _ = stitch_vias.run(["--pcb", str(pour_board), "--dry-run"])
    p2, _ = stitch_vias.run(["--pcb", str(pour_board), "--dry-run"])
    assert p1["ops"], "expected stitch ops on the pour fixture"
    assert json.dumps(p1["ops"]) == json.dumps(p2["ops"])
    assert pour_board.read_bytes() == raw


@pytest.mark.smoke
def test_live_via_fence(pour_board, cli, tmp_path):
    board = _copy_board(pour_board, tmp_path)
    bg = geom.BoardGeom.from_file(board)
    minx, _miny, maxx, maxy = bg.outline.bounds
    net = "/OSC_IN" if "/OSC_IN" in bg.nets else sorted(
        n for n in bg.nets if n != "GND")[0]
    y = round(maxy - 2.5, 3)
    x1, x2 = round(minx + 4, 3), round(min(minx + 24, maxx - 4), 3)
    route_edit.apply_ops(board, [{
        "op": "add_track", "start": [x1, y], "end": [x2, y],
        "width": 0.25, "layer": "F.Cu", "net": net}])
    rep = tmp_path / "fence.json"
    rc = stitch_vias.main([
        "--pcb", str(board), "--fence-net", net, "--fence-pitch", "2",
        "--out-report", str(rep)])
    assert rc == 0
    r = json.loads(rep.read_text("utf-8"))
    assert r["mode"] == "fence" and r["via_net"] == "GND"
    assert r["placed"] >= 4, r["fence"]
    for op in r["ops"]:
        assert op["net"] == "GND"
        assert abs(op["at"][1] - y) == pytest.approx(0.8, abs=0.005)
    bg2 = geom.BoardGeom.from_file(board)
    assert len(bg2.vias_of(net="GND")) >= r["placed"]


def test_board_hole_to_hole_rule_is_edge_to_edge(tmp_path):
    # JLC's 0.5 mm hole_to_hole (rules_gen's dru) is drill edge to edge:
    # two 0.3 mm drills need 0.8 mm centre to centre, not 0.5
    pcb = _board(tmp_path, "h2h", "")
    pcb.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "aiee_hole_to_hole_floor"\n'
        '\t(constraint hole_to_hole (min 0.5000mm))\n)\n', encoding="utf-8")
    assert stitch_vias.board_hole_to_hole(pcb) == 0.5
    sc = stitch_vias.Scene(["F.Cu", "B.Cu"], lambda layer, net: Point(-9, -9),
                           None, 0.2, hole_gap=0.5)
    sc.holes = [Point(10, 10).buffer(0.15)]
    assert not sc.hole_ok(10.6, 10)
    assert sc.hole_ok(10.81, 10)
    (tmp_path / "h2h.kicad_dru").unlink()
    assert stitch_vias.board_hole_to_hole(pcb) == 0.25
