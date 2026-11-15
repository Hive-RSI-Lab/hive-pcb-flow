"""S9 acceptance tests: placement seed, metrics, edit ops.

Plan S9 accept criteria:
  - seed placement of golden board 2 from scratch: legal (no overlaps,
    connectors on edges), metrics JSON emitted
  - an op list applies and is idempotent; re-application/failure rolls back

Pure tests (placelib model/transforms/clusters/legality/metrics, seed
algorithm, op validation) run with no toolchain and are unmarked; tests that
drive the SWIG worker or kicad-cli carry `smoke`. The edit path is SWIG
bundled python (V4/V8 decision recorded in PROGRESS S9).
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GOLDEN = REPO / "tests" / "golden"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import checklib  # noqa: E402
import env  # noqa: E402
import placelib  # noqa: E402
import place_edit  # noqa: E402
import place_metrics  # noqa: E402
import place_seed  # noqa: E402
from checklib import CheckError  # noqa: E402


# ---- synthetic boards ----------------------------------------------------

def _fp(ref: str, x: float, y: float, angle: float = 0.0,
        layer: str = "F.Cu", pads: str = "", courtyard: str | None = "rect",
        cy: tuple = (-1.0, -1.0, 1.0, 1.0), attr: str | None = "smd",
        locked: bool = False) -> str:
    at = f"(at {x} {y} {angle})" if angle else f"(at {x} {y})"
    crt = "F.CrtYd" if layer.startswith("F.") else "B.CrtYd"
    court = ""
    if courtyard == "rect":
        court = (f'    (fp_rect (start {cy[0]} {cy[1]}) (end {cy[2]} {cy[3]})'
                 f' (stroke (width 0.05)) (fill no) (layer "{crt}"))\n')
    elif courtyard == "lines":
        x1, y1, x2, y2 = cy
        court = "".join(
            f'    (fp_line (start {a} {b}) (end {c} {d})'
            f' (stroke (width 0.05)) (layer "{crt}"))\n'
            for a, b, c, d in ((x1, y1, x2, y1), (x2, y1, x2, y2),
                              (x2, y2, x1, y2), (x1, y2, x1, y1)))
    lock = "    (locked yes)\n" if locked else ""
    att = f"    (attr {attr})\n" if attr else ""
    return (f'  (footprint "t:{ref}" (layer "{layer}")\n'
            f'    {at}\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            f'{lock}{att}{court}{pads})\n')


def _pad(num: str, x: float, y: float, net: str | None = None,
         size: float = 0.6, kind: str = "smd rect",
         layers: str = '"F.Cu"') -> str:
    n = f' (net "{net}")' if net else ""
    return (f'    (pad "{num}" {kind} (at {x} {y}) (size {size} {size})'
            f' (layers {layers}){n})\n')


def _pcb(tmp_path_factory, name: str, body: str, w: float = 30.0,
         h: float = 20.0) -> Path:
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end {w} {h}) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path_factory.mktemp(name) / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


def _model(tmp_path_factory, name, body, **kw) -> placelib.PlaceModel:
    return placelib.PlaceModel(_pcb(tmp_path_factory, name, body, **kw))


# ============================================================ pure: parsing

def test_parse_footprint_fields(tmp_path_factory):
    body = _fp("U1", 10, 5, 90, pads=_pad("1", -0.95, 0, "GND"),
               attr="smd", cy=(-2, -1, 2, 1))
    body += _fp("H1", 2, 2, attr="board_only exclude_from_pos_files",
                courtyard=None)
    body += _fp("C1", 20, 8, locked=True, pads=_pad("1", 0.5, 0, "VCC"))
    m = _model(tmp_path_factory, "parse", body)
    u1 = m.footprints["U1"]
    assert u1.pos == (10.0, 5.0) and u1.angle == 90.0 and u1.side == "front"
    assert u1.pads[0].local == (-0.95, 0.0) and u1.pads[0].net == "GND"
    assert not u1.courtyard_missing
    h1 = m.footprints["H1"]
    assert "board_only" in h1.attrs and not h1.is_movable
    assert h1.courtyard_missing
    c1 = m.footprints["C1"]
    assert c1.locked and not c1.is_movable
    assert len(m.movable()) == 1


def test_pad_abs_transform_matches_s3_probe(tmp_path_factory):
    # LEARNINGS [geometry][kicad]: fp (104.8,105.5,90), pad local (-0.95,0)
    # -> pcbnew GetPosition (104.8,106.45)
    body = _fp("C10", 104.8, 105.5, 90, pads=_pad("1", -0.95, 0, "GND"))
    m = _model(tmp_path_factory, "xform", body, w=200, h=200)
    (_num, _net, x, y) = m.footprints["C10"].pad_centers_abs()[0]
    assert (round(x, 2), round(y, 2)) == (104.8, 106.45)


def test_courtyard_rect_and_lines_and_rotation(tmp_path_factory):
    body = _fp("A1", 5, 5, 0, cy=(-2, -1, 2, 1))
    body += _fp("A2", 15, 5, 90, courtyard="lines", cy=(-2, -1, 2, 1))
    m = _model(tmp_path_factory, "court", body)
    a1 = m.footprints["A1"].extents_abs()
    assert a1.bounds == pytest.approx((3, 4, 7, 6))
    a2 = m.footprints["A2"].extents_abs()  # rotated 90: 4x2 -> 2x4
    assert a2.bounds == pytest.approx((14, 3, 16, 7))
    assert a1.area == pytest.approx(8.0)
    assert a2.area == pytest.approx(8.0)


def test_place_center_asymmetric(tmp_path_factory):
    # courtyard offset from origin: center-based placement must compensate
    body = _fp("J1", 0, 0, 0, cy=(1.0, -1.0, 5.0, 1.0))
    m = _model(tmp_path_factory, "asym", body)
    j1 = m.footprints["J1"]
    for ang in (0.0, 90.0, 180.0, 270.0):
        j1.place_center((15.0, 10.0), ang)
        assert j1.center_abs() == pytest.approx((15.0, 10.0))
        assert j1.extents_abs().centroid.coords[0] == pytest.approx((15.0, 10.0))


def test_back_side_parsed_literally(tmp_path_factory):
    body = _fp("B1", 5, 5, layer="B.Cu", pads=_pad("1", 0.5, 0, "N",
                                                   layers='"B.Cu"'))
    m = _model(tmp_path_factory, "back", body)
    assert m.footprints["B1"].side == "back"
    assert not m.footprints["B1"].courtyard_missing  # B.CrtYd found


# ------------------------------------------------- per-pad rotation (T6)

def _rot_pad(num, x, y, rot, w, h, net="N"):
    return (f'    (pad "{num}" smd rect (at {x} {y} {rot}) (size {w} {h})'
            f' (layers "F.Cu") (net "{net}"))\n')


def test_pad_rotation_swaps_box(tmp_path_factory):
    """Ladder rows 69+110: fp angle 0, pad (at 0 0 90) (size 0.7 1.25) must
    yield a 1.25 mm-wide box (was 0.7 - under-reporting 0.275 mm/side)."""
    body = _fp("Q1", 10, 10, courtyard=None,
               pads=_rot_pad("1", 0, 0, 90, 0.7, 1.25))
    m = _model(tmp_path_factory, "rotswap", body)
    q1 = m.footprints["Q1"]
    assert q1.pads[0].rot == 90.0
    x0, y0, x1, y1 = q1._pad_box_local().bounds
    assert x1 >= 1.25 / 2 + 0.25 - 1e-9      # swapped width + margin
    assert y1 == pytest.approx(0.7 / 2 + 0.25)


def test_pad_rotation_cumulative_convention(tmp_path_factory):
    """File pad angles are CUMULATIVE with the fp angle (corpus-verified:
    every rotated 2-pad part carries pad ROT == fp angle): fp 90 + pad
    file-angle 90 = relative 0 -> NO swap."""
    body = _fp("Q2", 10, 10, 90, courtyard=None,
               pads=_rot_pad("1", 0, 0, 90, 0.7, 1.25))
    m = _model(tmp_path_factory, "rotrel", body)
    q2 = m.footprints["Q2"]
    assert q2.pads[0].rot == 0.0
    x0, y0, x1, y1 = q2._pad_box_local().bounds
    assert x1 == pytest.approx(0.7 / 2 + 0.25)   # unswapped
    assert y1 == pytest.approx(1.25 / 2 + 0.25)


def test_pad_box_invariant_under_place_center(tmp_path_factory):
    """FpPad.rot is stored RELATIVE, so rotating the model via place_center
    must not corrupt the local pad box (the file-absolute reading would)."""
    body = _fp("Q3", 10, 10, courtyard=None,
               pads=_rot_pad("1", 0, 0, 90, 0.7, 1.25))
    m = _model(tmp_path_factory, "rotinv", body)
    q3 = m.footprints["Q3"]
    before = q3._pad_box_local().bounds
    q3.place_center((15.0, 10.0), 90.0)
    assert q3._pad_box_local().bounds == before


# ============================================================ pure: clusters

DECOUP = {"associations": [
    {"cap": "C1", "ic": "U1", "pin": "1", "rail": "VCC"},
    {"cap": "C9", "ic": "U9", "pin": "1", "rail": "VCC"},   # U9 not on board
]}
PLACEMENT = {"edges": [{"ref": "J1", "edge": "left"}],
             "groups": [{"name": "x", "anchor": "Y1", "members": ["C2"]}]}


def _cluster_board(tmp_path_factory):
    body = _fp("U1", 15, 10, pads=_pad("1", -1.5, 0, "VCC"), cy=(-2, -2, 2, 2))
    body += _fp("C1", 25, 15, pads=_pad("1", -0.5, 0, "VCC")
                + _pad("2", 0.5, 0, "GND"))
    body += _fp("Y1", 5, 10, pads=_pad("1", -0.6, 0, "XI"))
    body += _fp("C2", 25, 5, pads=_pad("1", -0.5, 0, "XI"))
    body += _fp("J1", 5, 5, pads=_pad("1", 0, 0.6, "VCC"))
    body += _fp("C9", 27, 18, pads=_pad("1", -0.5, 0, "VCC"))
    body += _fp("H1", 2, 18, attr="board_only", courtyard=None)
    return _model(tmp_path_factory, "clust", body)


def test_build_clusters(tmp_path_factory):
    m = _cluster_board(tmp_path_factory)
    clusters, warnings = placelib.build_clusters(m, DECOUP, PLACEMENT)
    by = {c.anchor: c for c in clusters}
    assert set(by) == {"U1", "Y1", "J1", "C9"}  # C9 unclaimed: singleton
    assert [s.ref for s in by["U1"].satellites] == ["C1"]
    assert by["U1"].satellites[0].target_pin == ("U1", "1")
    assert [s.ref for s in by["Y1"].satellites] == ["C2"]
    assert by["J1"].edge == {"ref": "J1", "edge": "left"}
    assert any("U9" in w for w in warnings)  # missing anchor warned, not fatal


def test_cluster_double_claim_first_wins(tmp_path_factory):
    m = _cluster_board(tmp_path_factory)
    dec = {"associations": [
        {"cap": "C1", "ic": "U1", "pin": "1", "rail": "VCC"},
        {"cap": "C1", "ic": "Y1", "pin": "1", "rail": "XI"},
    ]}
    clusters, warnings = placelib.build_clusters(m, dec, None)
    by = {c.anchor: c for c in clusters}
    assert [s.ref for s in by["U1"].satellites] == ["C1"]
    assert not by["Y1"].satellites
    assert any("already satellite" in w for w in warnings)


def test_edge_for_satellite_pins_cluster(tmp_path_factory):
    m = _cluster_board(tmp_path_factory)
    dec = {"associations": [{"cap": "C1", "ic": "U1", "pin": "1",
                             "rail": "VCC"}]}
    pl = {"edges": [{"ref": "C1", "edge": "right"}]}
    clusters, warnings = placelib.build_clusters(m, dec, pl)
    u1 = next(c for c in clusters if c.anchor == "U1")
    assert u1.edge and u1.edge["ref"] == "C1"
    assert any("satellite" in w for w in warnings)


# ============================================================ pure: legality

def test_courtyard_overlap_flagged(tmp_path_factory):
    body = _fp("A1", 10, 10) + _fp("A2", 11, 10)  # 2x2 courtyards, 1mm apart
    m = _model(tmp_path_factory, "olap", body)
    v = placelib.legality_violations(m, None)
    kinds = {x["kind"] for x in v}
    assert "courtyard_overlap" in kinds
    ov = next(x for x in v if x["kind"] == "courtyard_overlap")
    assert ov["refs"] == ["A1", "A2"] and ov["severity"] == "error"
    assert ov["overlap_mm2"] == pytest.approx(2.0, abs=0.05)


def test_opposite_sides_do_not_collide_unless_through(tmp_path_factory):
    body = _fp("F1", 10, 10) + _fp("B1", 10, 10, layer="B.Cu")
    m = _model(tmp_path_factory, "sides", body)
    assert not any(x["kind"] == "courtyard_overlap"
                   for x in placelib.legality_violations(m, None))
    body2 = _fp("F1", 10, 10) + _fp(
        "T1", 10, 10, layer="B.Cu",
        pads=_pad("1", 0, 0, "N", kind="thru_hole circle", layers='"*.Cu"'))
    m2 = _model(tmp_path_factory, "sides2", body2)
    assert any(x["kind"] == "courtyard_overlap"
               for x in placelib.legality_violations(m2, None))


def test_outline_and_edge_rules(tmp_path_factory):
    body = _fp("A1", 29.5, 10)          # courtyard 28.5..30.5: pokes out
    body += _fp("J1", 1.0, 10)          # on left edge
    body += _fp("J2", 15, 10)           # declared right but mid-board
    body += _fp("H1", 29.8, 18, attr="board_only")  # fixed: exempt
    m = _model(tmp_path_factory, "edges", body)
    pl = {"edges": [{"ref": "J1", "edge": "left"},
                    {"ref": "J2", "edge": "right"}]}
    v = placelib.legality_violations(m, pl)
    kinds = {(x["kind"], tuple(x["refs"])) for x in v}
    assert ("outside_outline", ("A1",)) in kinds
    assert ("edge_violation", ("J2",)) in kinds
    assert not any(r == ("J1",) for _k, r in kinds)   # legal edge part
    assert not any(r == ("H1",) for _k, r in kinds)   # board_only exempt


def _rule_area(name: str, footprints: str) -> str:
    return (f'  (zone (net 0) (net_name "") (layers "F.Cu") (name "{name}")\n'
            f'    (keepout (tracks allowed) (vias allowed) (pads allowed)\n'
            f'      (copperpour allowed) (footprints {footprints}))\n'
            f'    (polygon (pts (xy 8 8) (xy 12 8) (xy 12 12) (xy 8 12))))\n')


def test_rule_area_scoping_only_is_not_a_keepout(tmp_path_factory):
    # a rule area that only scopes a track-width rule (all flags allowed)
    # must not flag parts inside it
    body = _fp("A1", 10, 10) + _rule_area("sig_branch_X", "allowed")
    m = _model(tmp_path_factory, "ra_allowed", body)
    v = placelib.legality_violations(m, {})
    assert not any(x["kind"] == "keepout_violation" for x in v)


def test_rule_area_forbidding_footprints_is_a_keepout(tmp_path_factory):
    body = _fp("A1", 10, 10) + _rule_area("no_parts", "not_allowed")
    m = _model(tmp_path_factory, "ra_forbid", body)
    v = placelib.legality_violations(m, {})
    assert any(x["kind"] == "keepout_violation" and x["refs"] == ["A1"]
               for x in v)


def test_keepout_and_missing_courtyard(tmp_path_factory):
    body = _fp("A1", 10, 10) + _fp("N1", 25, 10, courtyard=None,
                                   pads=_pad("1", 0, 0, "X"))
    m = _model(tmp_path_factory, "keep", body)
    pl = {"keepouts": [{"rect": [9, 9, 11, 11], "reason": "antenna"}]}
    v = placelib.legality_violations(m, pl)
    kinds = {x["kind"] for x in v}
    assert "keepout_violation" in kinds
    warn = next(x for x in v if x["kind"] == "courtyard_missing")
    assert warn["severity"] == "warning" and warn["refs"] == ["N1"]


# ============================================================ pure: metrics

def test_hpwl_hand_computed(tmp_path_factory):
    body = _fp("A1", 5, 5, pads=_pad("1", 0, 0, "N1"))
    body += _fp("A2", 15, 10, pads=_pad("1", 0, 0, "N1"))
    body += _fp("A3", 25, 5, pads=_pad("1", 0, 0, "N2"))  # single-pad: skip
    m = _model(tmp_path_factory, "hpwl", body)
    h = placelib.hpwl(m)
    assert h["by_net"] == {"N1": pytest.approx(15.0)}
    assert h["total_mm"] == pytest.approx(15.0)


def test_crossings_and_congestion(tmp_path_factory):
    # N1 spans left-right, N2 spans top-bottom through the same middle: cross
    body = _fp("A1", 5, 10, pads=_pad("1", 0, 0, "N1"))
    body += _fp("A2", 25, 10, pads=_pad("1", 0, 0, "N1"))
    body += _fp("B1", 15, 2, pads=_pad("1", 0, 0, "N2"))
    body += _fp("B2", 15, 18, pads=_pad("1", 0, 0, "N2"))
    m = _model(tmp_path_factory, "cross", body)
    c = placelib.crossings(m)
    assert c["count"] == 1
    assert c["pairs"][0]["nets"] == ["N1", "N2"]
    g = placelib.congestion(m, cell_mm=2.0)
    assert g["cols"] == 15 and g["rows"] == 10
    assert g["max"] >= 2  # both nets pass through the middle cell


def test_parallel_nets_do_not_cross(tmp_path_factory):
    body = _fp("A1", 5, 5, pads=_pad("1", 0, 0, "N1"))
    body += _fp("A2", 25, 5, pads=_pad("1", 0, 0, "N1"))
    body += _fp("B1", 5, 15, pads=_pad("1", 0, 0, "N2"))
    body += _fp("B2", 25, 15, pads=_pad("1", 0, 0, "N2"))
    m = _model(tmp_path_factory, "par", body)
    assert placelib.crossings(m)["count"] == 0


# ============================================================ pure: ops

def test_validate_ops_good():
    ops = place_edit.validate_ops({"version": 1, "ops": [
        {"op": "place", "ref": "C1", "x": 1.0, "y": 2.0, "deg": 90,
         "side": "front"},
        {"op": "move", "ref": "C2", "x": 0, "y": 0},
        {"op": "rotate", "ref": "C3", "deg": 45.5},
        {"op": "flip", "ref": "C4", "side": "back"},
        {"op": "lock", "ref": "C5", "locked": True},
    ]})
    assert len(ops) == 5


@pytest.mark.parametrize("doc", [
    {"ops": [{"op": "move", "ref": "C1", "x": 1, "y": 1}]},   # no version
    {"version": 1, "ops": []},
    {"version": 1, "ops": [{"op": "teleport", "ref": "C1"}]},
    {"version": 1, "ops": [{"op": "move", "ref": "C1", "x": 1}]},
    {"version": 1, "ops": [{"op": "move", "ref": "C1", "x": 1, "y": 1,
                            "deg": 5}]},                      # unknown key
    {"version": 1, "ops": [{"op": "move", "ref": "C1", "x": float("nan"),
                            "y": 1}]},
    {"version": 1, "ops": [{"op": "flip", "ref": "C1", "side": "top"}]},
    {"version": 1, "ops": [{"op": "lock", "ref": "C1", "locked": "yes"}]},
])
def test_validate_ops_rejects(doc):
    with pytest.raises(CheckError):
        place_edit.validate_ops(doc)


def test_validate_silk_clear_ops_good():
    """silk_clear takes only `ref`, with optional `layer` (F/B.SilkS, default
    F.SilkS) and boolean `only_offboard`; it moves nothing."""
    ops = place_edit.validate_ops({"version": 1, "ops": [
        {"op": "silk_clear", "ref": "U1"},                       # layer defaults
        {"op": "silk_clear", "ref": "U2", "layer": "B.SilkS"},
        {"op": "silk_clear", "ref": "U3", "layer": "F.SilkS",
         "only_offboard": True},
    ]})
    assert len(ops) == 3
    assert place_edit._expected_state(ops) == {}   # nothing to re-verify


@pytest.mark.parametrize("op", [
    {"op": "silk_clear"},                                        # no ref
    {"op": "silk_clear", "ref": "U1", "layer": "F.Cu"},
    {"op": "silk_clear", "ref": "U1", "layer": "F.Fab"},
    {"op": "silk_clear", "ref": "U1", "only_offboard": "yes"},
    {"op": "silk_clear", "ref": "U1", "x": 1.0},                 # unknown key
])
def test_validate_silk_clear_ops_reject(op):
    with pytest.raises(CheckError):
        place_edit.validate_ops({"version": 1, "ops": [op]})


def _fp_with_courtyard(ref, cx, cy, courtyard, pads):
    return placelib.Footprint(
        ref=ref, fpid="t:X", pos=(cx, cy), angle=0, side="front",
        attrs=frozenset(), locked=False, pads=pads, courtyard_local=courtyard)


def test_effective_courtyard_expands_to_pad_field():
    """S14 (major): a courtyard SMALLER than the pad field (EasyEDA LQFP body
    rect) must expand to cover pads+0.25 - courtyard-only legality passed a
    board with 9 shorting pad pairs."""
    from shapely.geometry import box as _box
    pads = [placelib.FpPad("1", "A", (-4.85, 0.0), (0.3, 1.2), False),
            placelib.FpPad("2", "B", (4.85, 0.0), (0.3, 1.2), False)]
    body = _box(-3.5, -3.5, 3.5, 3.5)         # body-only courtyard
    fp = _fp_with_courtyard("U1", 0, 0, body, pads)
    ext = fp.extents_local()
    assert ext.bounds[0] <= -4.85 - 0.15 - 0.25 + 1e-9   # covers pad 1 + margin
    assert ext.bounds[2] >= 4.85 + 0.15 + 0.25 - 1e-9
    # a PROPER courtyard (contains pads) is returned exactly, no inflation
    proper = _box(-5.5, -1.5, 5.5, 1.5)
    fp2 = _fp_with_courtyard("U2", 0, 0, proper, pads)
    assert fp2.extents_local().equals(proper)


def test_effective_courtyard_catches_pad_overlap():
    """Two body-courtyard parts whose PADS collide must now be a
    courtyard_overlap violation (was invisible pre-fix)."""
    from shapely.geometry import box as _box
    pads = [placelib.FpPad("1", "A", (-4.85, 0.0), (0.3, 1.2), False),
            placelib.FpPad("2", "B", (4.85, 0.0), (0.3, 1.2), False)]
    body = _box(-3.5, -3.5, 3.5, 3.5)
    a = _fp_with_courtyard("U1", 100.0, 100.0, body, pads)
    b = _fp_with_courtyard("U2", 108.0, 100.0, body, pads)  # pads interleave
    # bodies 8mm apart (no body overlap) but pad fields (reach +-5.25) collide
    ea = placelib.affinity.translate(a.extents_local(), 100.0, 100.0)
    eb = placelib.affinity.translate(b.extents_local(), 108.0, 100.0)
    assert ea.intersects(eb)


def _stock_0402(ref, x, y, courtyard="kicad"):
    """KiCad C_0402_1005Metric: pads +-0.48, 0.56 x 0.62; courtyard drawn
    0.15 mm beyond the pad field (+-0.91 x +-0.46)."""
    from shapely.geometry import box as _box
    pads = [placelib.FpPad("1", "A", (-0.48, 0.0), (0.56, 0.62), False),
            placelib.FpPad("2", "B", (0.48, 0.0), (0.56, 0.62), False)]
    cy = {"kicad": _box(-0.91, -0.46, 0.91, 0.46),
          "body": _box(-0.5, -0.25, 0.5, 0.25),     # EasyEDA body-only
          None: None}[courtyard]
    return _fp_with_courtyard(ref, x, y, cy, pads)


def _legal_model(*fps):
    from types import SimpleNamespace
    from shapely.geometry import box as _box
    return SimpleNamespace(footprints={f.ref: f for f in fps},
                           outline=_box(0, 0, 50, 50), rule_areas=[])


def _overlaps(*fps, placement=None):
    return [x for x in placelib.legality_violations(_legal_model(*fps),
                                                    placement)
            if x["kind"] == "courtyard_overlap"]


def test_stock_0402_pair_with_clear_courtyards_is_quiet():
    """#1062: two stock 0402s 0.3 mm pad to pad, drawn courtyards touching
    but clear - KiCad DRC passes them; the 0.25 mm pad grow flagged them."""
    a = _stock_0402("C1", 10.0, 10.0)
    b = _stock_0402("C2", 10.0 + 1.52 + 0.30, 10.0)
    assert a.courtyard_holds_pads()
    assert _overlaps(a, b) == []
    # the same pair under an explicit 0.25 mm grow is still flagged, and the
    # message says it is the pad fields, not the courtyards
    ov = _overlaps(a, b, placement={"pad_margin_mm": 0.25})
    assert len(ov) == 1 and ov[0]["drawn_courtyards_clear"] is True
    assert ov[0]["pad_gap_mm"] == pytest.approx(0.30, abs=0.01)
    assert ov[0]["msg"].startswith("pad fields too close C1/C2")


def test_diagonal_pair_judged_by_true_distance():
    """#1062: the square grow reached 0.25 mm in x AND y at a corner, so a
    diagonal pair 0.537 mm apart corner to corner (0.38 in x and y) was
    flagged at a 0.25 mm grow. Round corners judge the true distance."""
    a = _stock_0402("C1", 10.0, 10.0, courtyard=None)
    b = _stock_0402("C2", 10.0 + 1.52 + 0.38, 10.0 + 0.62 + 0.38,
                    courtyard=None)
    assert _overlaps(a, b) == []
    # 0.35 mm corner to corner is inside 2 x 0.25: still caught
    c = _stock_0402("C3", 10.0 + 1.52 + 0.25, 10.0 + 0.62 + 0.25,
                    courtyard=None)
    assert len(_overlaps(a, c)) == 1


def test_real_courtyard_overlap_still_flagged():
    a = _stock_0402("C1", 10.0, 10.0)
    b = _stock_0402("C2", 10.0 + 1.72, 10.0)     # courtyards overlap 0.1 mm
    ov = _overlaps(a, b)
    assert len(ov) == 1 and ov[0]["msg"].startswith("courtyard overlap")
    assert "drawn_courtyards_clear" not in ov[0]


def test_body_only_courtyard_keeps_pad_grow():
    """#438 kept: an EasyEDA courtyard smaller than its pads is no proper
    courtyard, so its pads still grow 0.25 mm and a 0.3 mm pad gap is
    flagged."""
    a = _stock_0402("C1", 10.0, 10.0, courtyard="body")
    b = _stock_0402("C2", 10.0 + 1.52 + 0.30, 10.0, courtyard="body")
    assert not a.courtyard_holds_pads()
    ov = _overlaps(a, b)
    assert len(ov) == 1 and ov[0]["drawn_courtyards_clear"] is True
    assert ov[0]["pad_margin_mm"] == [0.25, 0.25]


def test_validate_text_ops_good():
    ops = place_edit.validate_ops({"version": 1, "ops": [
        {"op": "add_text", "text": "+5V", "x": 10.0, "y": 20.0,
         "layer": "F.SilkS", "deg": 0, "size": 1.0, "thickness": 0.15},
        {"op": "move_text", "ref": "U1", "field": "reference",
         "x": 30.0, "y": 12.0, "deg": 90},
        {"op": "remove_text", "text": "K", "x": 10.0, "y": 20.0,
         "layer": "F.SilkS"},
    ]})
    assert len(ops) == 3


@pytest.mark.parametrize("doc", [
    {"version": 1, "ops": [{"op": "add_text", "text": "X", "x": 1, "y": 1,
                            "layer": "F.Cu"}]},        # copper layer banned
    {"version": 1, "ops": [{"op": "add_text", "text": "  ", "x": 1, "y": 1,
                            "layer": "F.SilkS"}]},     # blank text
    {"version": 1, "ops": [{"op": "add_text", "text": "X", "x": 1, "y": 1,
                            "layer": "F.SilkS", "size": 0}]},
    {"version": 1, "ops": [{"op": "move_text", "ref": "U1", "field": "lcsc",
                            "x": 1, "y": 1}]},         # only reference|value
    {"version": 1, "ops": [{"op": "move_text", "field": "value",
                            "x": 1, "y": 1}]},         # missing ref
    {"version": 1, "ops": [{"op": "remove_text", "text": "K", "x": 1, "y": 1,
                            "layer": "F.Cu"}]},        # copper layer banned
    {"version": 1, "ops": [{"op": "remove_text", "text": "K", "x": 1, "y": 1,
                            "layer": "F.SilkS", "size": 1.0}]},  # no options
])
def test_validate_text_ops_rejects(doc):
    with pytest.raises(CheckError):
        place_edit.validate_ops(doc)


def test_validate_set_text_ops_good():
    ops = place_edit.validate_ops({"version": 1, "ops": [
        {"op": "set_text", "ref": "C1", "field": "reference", "hide": True},
        {"op": "set_text", "ref": "C2", "field": "reference",
         "size": 0.8, "thickness": 0.15},
        {"op": "set_text", "ref": "C3", "field": "value", "layer": "B.SilkS"},
        # a field sent to a fab layer is not printed: no silk floor there
        {"op": "set_text", "ref": "C4", "field": "reference",
         "layer": "F.Fab", "size": 0.5, "thickness": 0.08},
    ]})
    assert len(ops) == 4


def test_silk_text_floor_matches_checkers():
    import check_silk
    size, stroke = place_edit.silk_text_floor()
    assert size == check_silk.MIN_TEXT_H == 0.8
    assert stroke == 0.15          # JLC min_silk_width_mm > MIN_TEXT_TH


@pytest.mark.parametrize("op", [
    {"op": "set_text", "ref": "C1", "field": "reference"},   # no option
    {"op": "set_text", "ref": "C1", "field": "lcsc", "hide": True},
    {"op": "set_text", "field": "reference", "hide": True},  # missing ref
    {"op": "set_text", "ref": "C1", "field": "reference", "hide": "yes"},
    {"op": "set_text", "ref": "C1", "field": "reference", "layer": "F.Cu"},
    {"op": "set_text", "ref": "C1", "field": "reference", "size": 0.6},
    {"op": "set_text", "ref": "C1", "field": "reference", "size": 0.7,
     "layer": "B.SilkS"},                                     # floor on B too
    {"op": "set_text", "ref": "C1", "field": "reference",
     "thickness": 0.12},                                      # fab width floor
    {"op": "set_text", "ref": "C1", "field": "reference", "size": True},
    {"op": "set_text", "ref": "C1", "field": "reference", "hide": True,
     "x": 1.0},                                               # unknown key
])
def test_validate_set_text_ops_reject(op):
    with pytest.raises(CheckError):
        place_edit.validate_ops({"version": 1, "ops": [op]})


def test_verify_set_text_reports_mismatch():
    got = {"x": 0, "y": 0, "deg": 0, "layer": "F.SilkS", "hidden": False,
           "size_x": 1.0, "size_y": 1.0, "thickness": 0.15}
    op = {"op": "set_text", "ref": "C1", "field": "reference", "hide": True,
          "size": 0.8, "layer": "B.SilkS", "thickness": 0.2}
    assert len(place_edit._verify_set_text(op, got)) == 4
    assert place_edit._verify_set_text(op, None) == [
        "set_text C1.reference: field not found in saved board"]


def test_expected_state_skips_text_ops():
    want = place_edit._expected_state([
        {"op": "move", "ref": "C1", "x": 1.0, "y": 2.0},
        {"op": "add_text", "text": "T", "x": 0, "y": 0, "layer": "F.SilkS"},
        {"op": "move_text", "ref": "U1", "field": "reference", "x": 3, "y": 4},
    ])
    assert want == {"C1": {"x": 1.0, "y": 2.0}}


def test_expected_state_folds_ops():
    want = place_edit._expected_state([
        {"op": "move", "ref": "C1", "x": 1.0, "y": 2.0},
        {"op": "rotate", "ref": "C1", "deg": 90.0},
        {"op": "flip", "ref": "C1", "side": "back"},
        {"op": "move", "ref": "C1", "x": 5.0, "y": 6.0},
    ])
    assert want == {"C1": {"x": 5.0, "y": 6.0, "deg": 90.0, "side": "back"}}


def test_apply_ops_missing_ref_rolls_back(tmp_path_factory):
    p = _pcb(tmp_path_factory, "rollback", _fp("C1", 10, 10))
    before = p.read_bytes()
    with pytest.raises(CheckError, match="ZZ9"):
        place_edit.apply_ops(p, [{"op": "move", "ref": "C1", "x": 1, "y": 1},
                                 {"op": "move", "ref": "ZZ9", "x": 1, "y": 1}])
    assert p.read_bytes() == before


# ------------------------------------- routed-board guard (T6, ladder 130)

_SEG = ('  (segment (start 5 5) (end 10 5) (width 0.25) (layer "F.Cu")'
        ' (net 0))\n')
_RULE_AREA = ('  (zone (net 0) (net_name "") (layer "F.Cu") (name "keep")\n'
              '    (keepout (tracks not_allowed) (vias not_allowed))\n'
              '    (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))\n')


def test_board_routed_scan(tmp_path_factory):
    clean = _pcb(tmp_path_factory, "brclean", _fp("C1", 10, 10) + _RULE_AREA)
    assert not place_edit.board_routed(clean)   # (vias not_allowed) exempt
    routed = _pcb(tmp_path_factory, "brrouted", _fp("C1", 10, 10) + _SEG)
    assert place_edit.board_routed(routed)


def _ops_file(tmp_path, ops):
    f = tmp_path / "ops.json"
    f.write_text(json.dumps({"version": 1, "ops": ops}), encoding="utf-8")
    return f


def test_footprint_op_on_routed_board_refused(tmp_path_factory, tmp_path,
                                              monkeypatch):
    """A courtyard-legal move on a routed board shorted a board while both
    P6 oracles stayed green (LEARNINGS 2026-07-30) - refuse without the
    flag, BEFORE any worker runs."""
    p = _pcb(tmp_path_factory, "guardref", _fp("C1", 10, 10) + _SEG)
    monkeypatch.setattr(place_edit, "apply_ops",
                        lambda *a, **k: pytest.fail("worker must not run"))
    f = _ops_file(tmp_path, [{"op": "move", "ref": "C1", "x": 1, "y": 1}])
    assert place_edit.main(["--pcb", str(p), "--ops", str(f)]) == 2


def test_text_ops_exempt_from_routed_guard(tmp_path_factory, tmp_path,
                                           monkeypatch):
    p = _pcb(tmp_path_factory, "guardtxt", _fp("C1", 10, 10) + _SEG)
    called = {}
    monkeypatch.setattr(place_edit, "apply_ops",
                        lambda pcb, ops, **k: called.setdefault("ops", ops) or [])
    f = _ops_file(tmp_path, [{"op": "move_text", "ref": "C1",
                              "field": "reference", "x": 9.0, "y": 8.0}])
    payload, _ = place_edit.run(["--pcb", str(p), "--ops", str(f)])
    assert called["ops"]                      # silk sweep went through
    assert "routed_board" not in payload


def test_allow_routed_flag_applies_and_warns(tmp_path_factory, tmp_path,
                                             monkeypatch):
    p = _pcb(tmp_path_factory, "guardflag", _fp("C1", 10, 10) + _SEG)
    monkeypatch.setattr(place_edit, "apply_ops", lambda pcb, ops, **k: [])
    f = _ops_file(tmp_path, [{"op": "move", "ref": "C1", "x": 1, "y": 1}])
    payload, _ = place_edit.run(["--pcb", str(p), "--ops", str(f),
                                 "--allow-routed"])
    assert payload["routed_board"] is True
    assert any("drc_routed" in w for w in payload["warnings"])


# ============================================================ pure: seed

def test_facing_rel_points_rail_pad_at_pin():
    fp = placelib.Footprint(
        ref="C1", fpid="t:C", pos=(0, 0), angle=0, side="front",
        attrs=frozenset(), locked=False,
        pads=[placelib.FpPad("1", "VCC", (-0.5, 0.0), (0.6, 0.6), False),
              placelib.FpPad("2", "GND", (0.5, 0.0), (0.6, 0.6), False)],
        courtyard_local=None)
    anchor = placelib.Footprint(
        ref="U1", fpid="t:U", pos=(0, 0), angle=0, side="front",
        attrs=frozenset(), locked=False,
        pads=[placelib.FpPad("1", "VCC", (-2.0, 0.0), (0.6, 0.6), False)],
        courtyard_local=None)
    sat = placelib.Satellite("C1", ("U1", "1"))
    # pin is to the LEFT of the satellite -> rail pad (local -x) faces left
    assert place_seed._facing_rel(anchor, fp, sat, (-1.0, 0.0)) == 0.0
    # pin to the RIGHT -> rotate 180
    assert place_seed._facing_rel(anchor, fp, sat, (1.0, 0.0)) == 180.0
    # pin BELOW (file +y) -> rail pad down: R(-90) maps (-1,0)->(0,-1)... pick
    rel = place_seed._facing_rel(anchor, fp, sat, (0.0, 1.0))
    assert rel in (90.0, 270.0)
    got = placelib._rot(-1.0, 0.0, -rel)
    assert got[1] == pytest.approx(1.0)  # rail pad direction points +y


def test_auto_edge_rot_asymmetric():
    # pads at local -x side, body center at origin: mating dir = +x
    fp = placelib.Footprint(
        ref="J1", fpid="t:J", pos=(0, 0), angle=0, side="front",
        attrs=frozenset(), locked=False,
        pads=[placelib.FpPad("1", "A", (-3.0, -1.0), (1, 1), False),
              placelib.FpPad("2", "B", (-3.0, 1.0), (1, 1), False)],
        courtyard_local=None)
    from shapely.geometry import box
    fp.courtyard_local = box(-4, -2, 2, 2)
    # want body (+x local) pointing off the left edge (-x abs): theta = 180
    assert place_seed.auto_edge_rot(fp, "left") == 180.0
    assert place_seed.auto_edge_rot(fp, "right") == 0.0
    # top edge outward (0,-1): R(-t)m = o -> t = ang(m)-ang(o) = 0-(-90) = 90
    assert place_seed.auto_edge_rot(fp, "top") == 90.0


def _seed_board(tmp_path_factory, name="seedb"):
    body = _fp("U1", 25, 15, pads=_pad("1", -2.5, 0, "VCC")
               + _pad("2", 2.5, 0, "IO"), cy=(-3, -3, 3, 3))
    body += _fp("C1", 35, 3, pads=_pad("1", -0.5, 0, "VCC")
                + _pad("2", 0.5, 0, "GND"))
    body += _fp("J1", 33, 12, pads=_pad("1", 0, -1, "IO")
                + _pad("2", 0, 1, "GND"), cy=(-2, -2, 4, 2))
    body += _fp("R1", 4, 3, pads=_pad("1", -0.5, 0, "IO")
                + _pad("2", 0.5, 0, "GND"))
    return _pcb(tmp_path_factory, name, body, w=40.0, h=30.0)


SEED_DEC = {"associations": [{"cap": "C1", "ic": "U1", "pin": "1",
                              "rail": "VCC"}]}
SEED_CON = {"placement": {"edges": [{"ref": "J1", "edge": "right"}]}}


def test_seed_synthetic_legal_and_deterministic(tmp_path_factory):
    p = _seed_board(tmp_path_factory)
    ops1, viol1, facts1, model1 = place_seed.seed(p, SEED_CON, SEED_DEC,
                                                  1.0, 0.8)
    assert not viol1
    assert placelib.legality_violations(model1, SEED_CON["placement"]) == []
    # C1 ended adjacent to U1 pin 1 (satellite lock)
    u1 = model1.footprints["U1"]
    c1 = model1.footprints["C1"]
    pin = u1.to_abs((-2.5, 0.0))
    assert math.dist(c1.center_abs(), pin) < 4.0
    # J1 on right edge
    j1e = model1.footprints["J1"].extents_abs()
    assert j1e.distance(placelib.edge_line(model1.outline, "right")) \
        <= placelib.EDGE_TOL
    # deterministic: identical ops on a re-run
    ops2, _v, _f, _m = place_seed.seed(p, SEED_CON, SEED_DEC, 1.0, 0.8)
    assert ops1 == ops2
    # every movable footprint got an op
    assert {o["ref"] for o in ops1} == {"U1", "C1", "J1", "R1"}



# ---- #431: satellites of a fixed anchor, and of a group member

def _cap(ref, x, y, net):
    return _fp(ref, x, y, pads=_pad("1", -0.5, 0, net)
               + _pad("2", 0.5, 0, "GND"), cy=(-0.9, -0.5, 0.9, 0.5))


def _fixed_anchor_board(tmp_path_factory, locked):
    # U1 hand-placed; its caps sit on board_init's shelf, off the board.
    # X1 (fixed) sits exactly where C1's natural slot is.
    body = _fp("U1", 20, 15, locked=locked, pads=_pad("1", -2.5, 0, "VCC")
               + _pad("2", 2.5, 0, "VDD"), cy=(-3, -3, 3, 3))
    body += _cap("C1", 45, 3, "VCC") + _cap("C2", 45, 6, "VDD")
    body += _fp("X1", 15.2, 15, locked=True, cy=(-1.5, -1.5, 1.5, 1.5))
    body += _fp("R1", 4, 3, pads=_pad("1", -0.5, 0, "VCC")
                + _pad("2", 0.5, 0, "GND"))
    return _pcb(tmp_path_factory, f"fixa{int(locked)}", body, w=40.0, h=30.0)


DEC_U1 = {"associations": [
    {"cap": "C1", "ic": "U1", "pin": "1", "rail": "VCC"},
    {"cap": "C2", "ic": "U1", "pin": "2", "rail": "VDD"}]}


@pytest.mark.parametrize("locked", [True, False])
def test_seed_places_satellites_of_a_fixed_anchor(tmp_path_factory, locked):
    p = _fixed_anchor_board(tmp_path_factory, locked)
    con = {"placement": {} if locked else {"fixed": ["U1"]}}
    ops, viol, facts, model = place_seed.seed(p, con, DEC_U1, 1.0, 0.8)
    assert not viol, viol
    assert placelib.legality_violations(model, con["placement"]) == []
    u1 = model.footprints["U1"]
    assert u1.pos == (20.0, 15.0)
    for cap, pin in (("C1", (-2.5, 0.0)), ("C2", (2.5, 0.0))):
        c = model.footprints[cap]
        assert model.outline.contains(c.extents_abs())
        assert math.dist(c.center_abs(), u1.to_abs(pin)) < 5.0, cap
    refs = {o["ref"] for o in ops}
    assert {"C1", "C2", "R1"} <= refs and "U1" not in refs
    assert any(c.get("fixed") and c["anchor"] == "U1"
               for c in facts["clusters"])
    # deterministic
    assert place_seed.seed(p, con, DEC_U1, 1.0, 0.8)[0] == ops


def test_fixed_clusters_only_on_request(tmp_path_factory):
    m = placelib.PlaceModel(_fixed_anchor_board(tmp_path_factory, True))
    plain, _w = placelib.build_clusters(m, DEC_U1, {})
    assert {c.anchor for c in plain} == {"R1"}
    full, _w = placelib.build_clusters(m, DEC_U1, {}, with_fixed=True)
    u1 = next(c for c in full if c.anchor == "U1")
    assert u1.fixed and [s.ref for s in u1.satellites] == ["C1", "C2"]


def test_fixed_part_is_never_a_satellite(tmp_path_factory):
    m = placelib.PlaceModel(_fixed_anchor_board(tmp_path_factory, False))
    clusters, _w = placelib.build_clusters(m, DEC_U1, {"fixed": ["C1"]})
    u1 = next(c for c in clusters if c.anchor == "U1")
    assert [s.ref for s in u1.satellites] == ["C2"]


def _nested_board(tmp_path_factory):
    body = _fp("U1", 10, 10, pads=_pad("1", -2.5, 0, "A"), cy=(-3, -3, 3, 3))
    body += _fp("U2", 30, 20, pads=_pad("1", -1.5, 0, "VCC")
                + _pad("2", 1.5, 0, "A"), cy=(-2, -2, 2, 2))
    body += _cap("C3", 45, 3, "VCC") + _cap("C5", 45, 6, "A")
    return _pcb(tmp_path_factory, "nest", body, w=40.0, h=30.0)


NEST_DEC = {"associations": [{"cap": "C3", "ic": "U2", "pin": "1",
                              "rail": "VCC"}]}
NEST_PL = {"groups": [{"name": "core", "anchor": "U1", "members": ["U2"]},
                      {"name": "u2", "anchor": "U2", "members": ["C5"]}]}


def test_nested_satellites_fold_into_the_top_cluster(tmp_path_factory):
    m = placelib.PlaceModel(_nested_board(tmp_path_factory))
    clusters, warnings = placelib.build_clusters(m, NEST_DEC, NEST_PL)
    assert [c.anchor for c in clusters] == ["U1"]
    sats = {s.ref: s for s in clusters[0].satellites}
    assert list(sats) == ["U2", "C3", "C5"]       # parents first
    assert sats["U2"].parent is None
    assert sats["C3"].parent == "U2" and sats["C3"].target_pin == ("U2", "1")
    assert sats["C5"].parent == "U2"
    assert not any("itself a satellite" in w for w in warnings)


def test_seed_slots_a_nested_decoupler_at_its_parents_pin(tmp_path_factory):
    p = _nested_board(tmp_path_factory)
    con = {"placement": NEST_PL}
    _ops, viol, _f, model = place_seed.seed(p, con, NEST_DEC, 1.0, 0.8)
    assert not viol, viol
    assert placelib.legality_violations(model, NEST_PL) == []
    u2, c3 = model.footprints["U2"], model.footprints["C3"]
    assert math.dist(c3.center_abs(), u2.to_abs((-1.5, 0.0))) < 6.0
    assert math.dist(u2.center_abs(), model.footprints["U1"].center_abs()) < 9


def test_satellite_loop_is_refused(tmp_path_factory):
    m = placelib.PlaceModel(_nested_board(tmp_path_factory))
    pl = {"groups": [{"name": "a", "anchor": "U1", "members": ["U2"]},
                     {"name": "b", "anchor": "U2", "members": ["U1"]}]}
    clusters, warnings = placelib.build_clusters(m, {}, pl)
    assert "U1" in {c.anchor for c in clusters}
    assert any("loop" in w for w in warnings)


def test_seed_hpwl_improves_on_spread_board(tmp_path_factory):
    p = _seed_board(tmp_path_factory)
    _ops, _viol, facts, _model = place_seed.seed(p, SEED_CON, SEED_DEC,
                                                 1.0, 0.8)
    assert facts["hpwl_seed_mm"] < facts["hpwl_before_mm"]


def test_seed_board_too_small_errors(tmp_path_factory):
    p = _pcb(tmp_path_factory, "tiny", _fp("A1", 1, 1), w=1.0, h=1.0)
    with pytest.raises(CheckError, match="too small"):
        place_seed.seed(p, {"placement": {}}, {}, 1.0, 0.8)


def test_net_weights():
    w = place_seed.net_weights({"power": [{"net": "VBUS"}]},
                               {"associations": [{"rail": "+3V3",
                                                  "gnd": "GND"}]})
    assert w("GND") == 0.2 and w("VBUS") == 0.5 and w("+3V3") == 0.5
    assert w("/SIG") == 1.0


# ============================================================ corpus (hermetic)

@pytest.mark.parametrize("board", ["blinky2", "usbbuck4", "rf4"])
def test_goldens_legality_clean(board):
    m = placelib.PlaceModel(GOLDEN / board / f"{board}.kicad_pcb")
    cons = json.loads((GOLDEN / board / "constraints.json").read_text("utf-8"))
    assert placelib.legality_violations(m, cons.get("placement")) == []


def test_usbbuck4_model_parses_all():
    m = placelib.PlaceModel(GOLDEN / "usbbuck4" / "usbbuck4.kicad_pcb")
    assert len(m.footprints) == 23
    assert not any(f.courtyard_missing for f in m.footprints.values())
    h = placelib.hpwl(m)
    assert 400 < h["total_mm"] < 500  # golden hand placement ~452.6


def test_place_metrics_clean_on_golden():
    payload, _ = place_metrics.run(
        ["--pcb", str(GOLDEN / "usbbuck4" / "usbbuck4.kicad_pcb")])
    assert payload["status"] == "pass"
    assert payload["counts"]["total"] == 0
    met = payload["metrics"]
    assert met["counts"]["footprints"] == 23
    assert len(met["decoupling"]) == 8
    for f in met["decoupling"]:
        assert f["manhattan_mm"] < 25
    assert met["hpwl"]["total_mm"] > 0
    assert met["congestion"]["max"] >= 1
    assert met["utilization"]["ratio"] > 0.05


def test_place_metrics_flags_moved_decoupler(tmp_path_factory):
    # decoupler-moved mutant: C1 15.7 mm from U1.48 -> distance violation
    payload, _ = place_metrics.run(
        ["--pcb", str(GOLDEN / "mutants" / "decoupler-moved"
                      / "blinky2.kicad_pcb"),
         "--constraints", str(GOLDEN / "blinky2" / "constraints.json"),
         "--decoupling", str(GOLDEN / "blinky2" / "decoupling.json")])
    assert payload["status"] == "violations"
    kinds = {v["kind"] for v in payload["violations"]}
    assert kinds == {"decoupler_distance"}
    v = next(x for x in payload["violations"]
             if x["kind"] == "decoupler_distance")
    assert "C1" in v["refs"]


def test_place_metrics_declared_limit_fails_gate(tmp_path):
    """U20: the place gate fails a board violating its OWN declared
    max_dist_mm - the declared limit is the error threshold, class defaults
    apply only where nothing is declared."""
    fix = REPO / "tests" / "fixtures" / "bb_adc"
    dec = json.loads((fix / "decoupling.json").read_text("utf-8"))
    for a in dec["associations"]:
        if a["cap"] == "C2":
            a["max_dist_mm"] = 1.0     # C2 measures 1.97 mm on the board
    meta = tmp_path / "decoupling.json"
    meta.write_text(json.dumps(dec), encoding="utf-8")
    payload, _ = place_metrics.run(
        ["--pcb", str(fix / "bb-adc.kicad_pcb"),
         "--constraints", str(fix / "constraints.json"),
         "--decoupling", str(meta)])
    v = [x for x in payload["violations"]
         if x["kind"] == "decoupler_distance" and "C2" in x["refs"]]
    assert v and v[0]["severity"] == "error"
    assert "decoupler_distance" in payload["coverage"]["failed"]
    # the board's real sidecar is satisfied: no distance violations at all
    payload2, _ = place_metrics.run(
        ["--pcb", str(fix / "bb-adc.kicad_pcb"),
         "--constraints", str(fix / "constraints.json"),
         "--decoupling", str(fix / "decoupling.json")])
    assert [x for x in payload2["violations"]
            if x["kind"] == "decoupler_distance"] == []


def test_place_gate_wired():
    import gate
    gates = gate.load_gates(gate.DEFAULT_GATES)
    assert "place" in gates
    assert gates["place"]["tool"] == "place"
    report = gate.run_report_for_gate(
        gates["place"], GOLDEN / "usbbuck4" / "usbbuck4.kicad_pcb")
    res = gate.evaluate("place", gates["place"], report)
    assert res["status"] == "pass"


def test_cluster_violations_knows_placement_kinds():
    import cluster_violations
    for kind in ("courtyard_overlap", "outside_outline", "edge_violation",
                 "keepout_violation", "seed_unplaced"):
        assert cluster_violations.FIXER_HINTS[kind] == "placement"


# ============================================================ smoke: live SWIG

@pytest.fixture(scope="session")
def cli() -> Path:
    c = env.find_kicad_cli()
    if c is None:
        pytest.skip("kicad-cli not installed")
    return c


@pytest.fixture(scope="session")
def edit_board(cli, tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("editboard")
    p = d / "usbbuck4.kicad_pcb"
    shutil.copy2(GOLDEN / "usbbuck4" / "usbbuck4.kicad_pcb", p)
    (d / "usbbuck4.kicad_pro").write_text('{"meta": {"filename": "x"}}',
                                          encoding="utf-8")
    return p


OPS = [{"op": "place", "ref": "C1", "x": 150.0, "y": 140.0, "deg": 45.0},
       {"op": "rotate", "ref": "C2", "deg": 270.0},
       {"op": "flip", "ref": "C3", "side": "back"},
       {"op": "lock", "ref": "C4", "locked": True}]


@pytest.mark.smoke
def test_edit_applies_and_is_idempotent(edit_board):
    pro = edit_board.with_suffix(".kicad_pro")
    pro_before = pro.read_bytes()
    results = place_edit.apply_ops(edit_board, OPS)
    m = placelib.PlaceModel(edit_board)
    assert m.footprints["C1"].pos == (150.0, 140.0)
    assert m.footprints["C1"].angle == pytest.approx(45.0)
    assert m.footprints["C3"].side == "back"
    assert m.footprints["C4"].locked
    assert place_edit._angdiff(m.footprints["C2"].angle, 270.0) < 0.05
    assert len(results) == 4
    # idempotent: re-apply -> identical parsed placement state
    state1 = {r: (f.pos, f.angle, f.side, f.locked)
              for r, f in placelib.PlaceModel(edit_board).footprints.items()}
    place_edit.apply_ops(edit_board, OPS)
    state2 = {r: (f.pos, f.angle, f.side, f.locked)
              for r, f in placelib.PlaceModel(edit_board).footprints.items()}
    assert state1 == state2
    # the real .kicad_pro was never clobbered by the SWIG save
    assert pro.read_bytes() == pro_before


@pytest.mark.smoke
def test_text_ops_apply_verify_idempotent(edit_board):
    """S14 (V17): add_text creates board-frame silk text (no duplicate on
    re-apply); move_text repositions a refdes field; both verified by the
    driver's independent sexpdata parse."""
    ops = [
        {"op": "add_text", "text": "+5V", "x": 152.0, "y": 141.0,
         "layer": "F.SilkS", "size": 1.0, "thickness": 0.15},
        {"op": "move_text", "ref": "C1", "field": "reference",
         "x": 151.0, "y": 138.5, "deg": 0},
    ]
    place_edit.apply_ops(edit_board, ops)
    gr, fields = place_edit._parse_board_texts(edit_board)
    hits = [t for t in gr if t["text"] == "+5V" and t["layer"] == "F.SilkS"]
    assert len(hits) == 1
    assert hits[0]["x"] == pytest.approx(152.0, abs=1e-3)
    got = fields[("C1", "reference")]
    assert got["x"] == pytest.approx(151.0, abs=1e-3)
    assert got["y"] == pytest.approx(138.5, abs=1e-3)
    # idempotent re-apply: still exactly ONE text, same positions
    place_edit.apply_ops(edit_board, ops)
    gr2, fields2 = place_edit._parse_board_texts(edit_board)
    assert len([t for t in gr2 if t["text"] == "+5V"]) == 1
    assert fields2[("C1", "reference")]["x"] == pytest.approx(151.0, abs=1e-3)


@pytest.mark.smoke
def test_remove_text_relocates_gr_text(edit_board):
    """remove_text + add_text is the relocation path for a board gr_text
    (add_text alone matches on the TARGET position and can never move one).
    Absent -> no-op, so the pair stays idempotent."""
    place_edit.apply_ops(edit_board, [
        {"op": "add_text", "text": "K", "x": 150.0, "y": 140.0,
         "layer": "F.SilkS", "size": 1.0, "thickness": 0.15}])
    ops = [
        {"op": "remove_text", "text": "K", "x": 150.0, "y": 140.0,
         "layer": "F.SilkS"},
        {"op": "add_text", "text": "K", "x": 153.5, "y": 140.0,
         "layer": "F.SilkS", "size": 1.0, "thickness": 0.15},
    ]
    place_edit.apply_ops(edit_board, ops)
    gr, _ = place_edit._parse_board_texts(edit_board)
    hits = [t for t in gr if t["text"] == "K" and t["layer"] == "F.SilkS"]
    assert len(hits) == 1
    assert hits[0]["x"] == pytest.approx(153.5, abs=1e-3)
    place_edit.apply_ops(edit_board, ops)          # idempotent re-apply
    gr2, _ = place_edit._parse_board_texts(edit_board)
    assert len([t for t in gr2 if t["text"] == "K"]) == 1


@pytest.mark.smoke
def test_set_text_hides_shrinks_and_relayers(edit_board):
    """set_text: the fix for a refdes with no legal spot (dense 0603 rows).
    Hide, shrink to the floor and move to back silk (mirrored), each verified
    by the driver's own parse; re-apply is a no-op."""
    import check_silk
    import sexpdata
    ops = [
        {"op": "set_text", "ref": "C9", "field": "reference", "hide": True},
        {"op": "set_text", "ref": "C7", "field": "reference",
         "size": 0.8, "thickness": 0.15},
        {"op": "set_text", "ref": "C8", "field": "reference",
         "layer": "B.SilkS"},
    ]
    results = place_edit.apply_ops(edit_board, ops)
    assert results[0]["hidden"] is True
    assert results[2]["layer"] == "B.SilkS"
    _, fields = place_edit._parse_board_texts(edit_board)
    assert fields[("C9", "reference")]["hidden"] is True
    assert fields[("C7", "reference")]["size_y"] == pytest.approx(0.8)
    assert fields[("C7", "reference")]["hidden"] is False
    assert fields[("C8", "reference")]["layer"] == "B.SilkS"
    text = edit_board.read_text(encoding="utf-8")
    i = text.index('(property "Reference" "C8"')
    assert "(justify mirror)" in text[i:text.index("(property", i + 10)]
    # check_silk lists the hidden refdes for the assembly drawing
    off = {d["ref"]: d["why"]
           for d in check_silk.refdes_off_silk(sexpdata.loads(text))}
    assert off.get("C9") == "hidden" and "C7" not in off
    place_edit.apply_ops(edit_board, ops)              # idempotent
    _, again = place_edit._parse_board_texts(edit_board)
    assert again[("C9", "reference")]["hidden"] is True
    # un-hide and back to front silk restores the label
    place_edit.apply_ops(edit_board, [
        {"op": "set_text", "ref": "C9", "field": "reference", "hide": False},
        {"op": "set_text", "ref": "C8", "field": "reference",
         "layer": "F.SilkS"}])
    _, back = place_edit._parse_board_texts(edit_board)
    assert back[("C9", "reference")]["hidden"] is False
    assert back[("C8", "reference")]["layer"] == "F.SilkS"


@pytest.mark.smoke
def test_move_text_on_rotated_footprint(edit_board):
    """The transform verify must hold for a ROTATED parent footprint (local
    property coords + absolute stored angle)."""
    place_edit.apply_ops(edit_board, [
        {"op": "rotate", "ref": "C5", "deg": 90.0},
        {"op": "move_text", "ref": "C5", "field": "value",
         "x": 149.0, "y": 139.0, "deg": 90.0},
    ])
    _, fields = place_edit._parse_board_texts(edit_board)
    got = fields[("C5", "value")]
    assert got["x"] == pytest.approx(149.0, abs=1e-3)
    assert got["y"] == pytest.approx(139.0, abs=1e-3)
    assert place_edit._angdiff(got["deg"], 90.0) < 0.05


@pytest.mark.smoke
def test_worker_failure_saves_nothing(edit_board, cli, tmp_path):
    # bypass the driver's ref pre-flight to exercise the worker's own guard
    bp = env.find_kicad_python(cli)
    staged = tmp_path / "w.kicad_pcb"
    shutil.copy2(edit_board, staged)
    before = staged.read_bytes()
    job = tmp_path / "job.json"
    job.write_text(json.dumps({
        "board": str(staged), "out": str(staged),
        "ops": [{"op": "move", "ref": "NOPE", "x": 1, "y": 1}]}),
        encoding="utf-8")
    cp = subprocess.run([str(bp), str(SCRIPTS / "lib" / "place_swig.py"),
                         str(job)], capture_output=True, text=True,
                        timeout=180)
    assert cp.returncode == 3
    assert json.loads(cp.stdout.strip().splitlines()[-1])["ok"] is False
    assert staged.read_bytes() == before


@pytest.fixture(scope="session")
def init_board(cli, tmp_path_factory) -> Path:
    """usbbuck4 netlist -> board_init, the from-scratch seed input."""
    import board_init
    import kc
    d = tmp_path_factory.mktemp("seedinit")
    net = d / "usbbuck4.net"
    kc.export_netlist(cli, GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch", net)
    rc = board_init.main([
        "--netlist", str(net), "--name", "usbbuck4",
        "--out", str(d / "kicad"), "--layers", "4", "--mounting-holes", "4"])
    assert rc == 0
    return d / "kicad" / "usbbuck4.kicad_pcb"


@pytest.mark.smoke
def test_seed_acceptance_usbbuck4(init_board, tmp_path):
    """Plan S9 acceptance: seed golden board 2 from scratch -> legal."""
    ops_out = tmp_path / "seed_ops.json"
    payload, _ = place_seed.run([
        "--pcb", str(init_board),
        "--constraints", str(GOLDEN / "usbbuck4" / "constraints.json"),
        "--decoupling", str(GOLDEN / "usbbuck4" / "decoupling.json"),
        "--ops-out", str(ops_out), "--apply"])
    assert payload["status"] == "pass", payload["violations"]
    assert payload["applied"] is True
    assert payload["hpwl_applied_mm"] < payload["hpwl_before_mm"]

    ops = json.loads(ops_out.read_text("utf-8"))["ops"]
    assert {o["ref"] for o in ops} == {
        f.ref for f in placelib.PlaceModel(init_board).movable()}

    cons = json.loads(
        (GOLDEN / "usbbuck4" / "constraints.json").read_text("utf-8"))
    model = placelib.PlaceModel(init_board)
    assert placelib.legality_violations(model, cons["placement"]) == []
    for e in cons["placement"]["edges"]:
        ext = model.footprints[e["ref"]].extents_abs()
        line = placelib.edge_line(model.outline, e["edge"])
        assert ext.distance(line) <= placelib.EDGE_TOL, e
    # satellites landed near their pins (decoupler distances in class)
    payload_m, _ = place_metrics.run([
        "--pcb", str(init_board),
        "--constraints", str(GOLDEN / "usbbuck4" / "constraints.json"),
        "--decoupling", str(GOLDEN / "usbbuck4" / "decoupling.json")])
    assert payload_m["status"] == "pass"
    for f in payload_m["metrics"]["decoupling"]:
        assert f["manhattan_mm"] < 10.0, f


@pytest.mark.smoke
def test_seed_ops_deterministic_on_corpus(init_board, tmp_path):
    outs = []
    for i in (1, 2):
        ops_out = tmp_path / f"ops{i}.json"
        place_seed.run([
            "--pcb", str(init_board),
            "--constraints", str(GOLDEN / "usbbuck4" / "constraints.json"),
            "--decoupling", str(GOLDEN / "usbbuck4" / "decoupling.json"),
            "--ops-out", str(ops_out)])
        outs.append(ops_out.read_text("utf-8"))
    assert outs[0] == outs[1]


@pytest.mark.smoke
def test_seed_and_metrics_fast(init_board):
    import time
    t0 = time.time()
    place_seed.seed(init_board,
                    json.loads((GOLDEN / "usbbuck4"
                                / "constraints.json").read_text("utf-8")),
                    json.loads((GOLDEN / "usbbuck4"
                                / "decoupling.json").read_text("utf-8")),
                    1.0, 0.8)
    place_metrics.collect(init_board, None, None, 2.0)
    assert time.time() - t0 < 30.0


# ---- designed edge overhang (rf-term-150w regression) -------------------

RF_TERM = REPO / "tests" / "fixtures" / "rf_term_edge"


def _rf_term_seed():
    con = json.loads((RF_TERM / "constraints.json").read_text("utf-8"))
    return place_seed.seed(RF_TERM / "rf_term_edge.kicad_pcb", con, {},
                           1.0, 0.8), con


def test_edge_snap_lets_designed_overhang_hang_off():
    """J1's SMA barrel courtyard runs 12 mm past its origin by design: the
    seed must align its pads (edge_margin inside the top edge) and let the
    barrel hang off, not pull the whole courtyard on-board onto R1."""
    (_ops, _v, _f, model), con = _rf_term_seed()
    minx, miny, maxx, maxy = model.outline.bounds
    j1 = model.footprints["J1"]
    # designer: y 24.81; the old courtyard-flush snap put it at 32.51
    assert abs(j1.pos[1] - 24.81) < 0.6
    assert j1.extents_abs().bounds[1] < miny - 5.0          # barrel off-board
    pad_edge = min(j1.to_abs(p.local)[1] - p.size[1] / 2 for p in j1.pads)
    assert pad_edge - miny >= 0.8 + 0.25 - 1e-6              # margin kept
    on = j1.extents_abs().intersection(model.outline).area
    assert on >= placelib.ON_BOARD_MIN * j1.extents_abs().area
    # J1's satellite C1 is slid inboard, fully on the board
    c1 = model.footprints["C1"].extents_abs()
    assert c1.difference(model.outline).area <= placelib.EPS_AREA
    # the suppressed case: R1's courtyard does not reach past its pads, so
    # it stays courtyard-flush with the bottom edge, less the 0.05 mm its
    # pad copper needs against the board's 0.3 mm copper-to-edge rule
    r1 = model.footprints["R1"].extents_abs()
    assert maxy - r1.bounds[3] == pytest.approx(0.05, abs=1e-6)
    # J1, C1 and R1 are legal against the outline and each other (the
    # mounting holes are the free legalizer's, not the edge snap's)
    viol = placelib.legality_violations(model, con["placement"])
    assert not [v for v in viol
                if set(v.get("refs", [])) <= {"J1", "C1", "R1"}], viol


def test_perimeter_slot_starts_at_courtyard_not_circumradius():
    """An elongated anchor's circumradius is far past its side: C1 sits
    beside J1's body, not 6.5 mm off it."""
    (_ops, _v, _f, model), _con = _rf_term_seed()
    gap = (model.footprints["C1"].extents_abs().bounds[0]
           - model.footprints["J1"].extents_abs().bounds[2])
    # the slot starts at C1's own side, not its circumradius (was 1.6 mm)
    assert 0.0 <= gap < 0.5


def test_pos_is_edge_fraction_and_all_three_holes_fit():
    """Schema: "pos 0..1 along the edge"; rf-term's constraints mean the
    part's origin (the SMA pin, R1's tab) at that fraction. With it, and the
    M3 holes allowed as close to the edge as their copper may go, the seed
    finds the legal three-hole layout the shipped board has."""
    (_ops, viol, _f, model), con = _rf_term_seed()
    minx, miny, maxx, maxy = model.outline.bounds
    for ref in ("J1", "R1"):
        assert model.footprints[ref].pos[0] == pytest.approx(
            minx + 0.375 * (maxx - minx), abs=1e-6)
    assert not viol
    assert not placelib.legality_violations(model, con["placement"])
    cu = placelib.copper_edge_mm(model)
    assert cu == pytest.approx(0.3)             # the board's own rule
    for ref in ("H1", "H2", "H3"):
        fp = model.footprints[ref]
        assert fp.is_mechanical
        for p in fp.pads:
            x, y = fp.to_abs(p.local)
            hx, hy = p.size[0] / 2, p.size[1] / 2
            gap = min(x - hx - minx, y - hy - miny, maxx - x - hx,
                      maxy - y - hy)
            assert gap >= cu - 1e-6, (ref, gap)
    # the suppressed case: an assembled part is not mechanical, and a
    # rule looser than the courtyard's own 0.25 mm asks for no keep
    assert not model.footprints["C1"].is_mechanical
    assert model.footprints["H1"].edge_keep(0.3) == pytest.approx(0.05)
    assert model.footprints["H1"].edge_keep(0.2) == 0.0


# ============================================ back-side decoupling under a BGA
# hw-lab linux-computer (i.MX6ULL BGA-289): 40 decouplers could not ring the
# 14 mm package on top (15 courtyard overlaps); IMX6ULLHDG 3.2.1 puts them
# on the bottom, under their balls.

def _bga_pads(n: int = 8, pitch: float = 0.8, nets=None) -> str:
    out = ""
    for r in range(n):
        for c in range(n):
            num = f"{'ABCDEFGHJKLMNPRT'[r]}{c + 1}"
            net = (nets or {}).get(num, f"S{num}")
            out += _pad(num, (c - (n - 1) / 2) * pitch,
                        (r - (n - 1) / 2) * pitch, net, size=0.4)
    return out


def _bga_board(tmp_path_factory, name="bgab", ncaps=12):
    rails = {"C3": "V1", "D4": "V1", "E5": "V2", "F6": "V2", "C6": "V3",
             "F3": "V3", "D5": "V1", "E4": "V2", "B2": "V3", "G7": "V1",
             "B7": "V2", "G2": "V3"}
    body = _fp("U1", 20, 15, pads=_bga_pads(nets=rails),
               cy=(-3.6, -3.6, 3.6, 3.6))
    dec = []
    for i, (ball, net) in enumerate(sorted(rails.items())[:ncaps]):
        ref = f"C{i + 1}"
        body += _fp(ref, 2 + 2.5 * i, 27, pads=_pad("1", -0.48, 0, net,
                                                    size=0.5)
                    + _pad("2", 0.48, 0, "GND", size=0.5),
                    cy=(-0.93, -0.47, 0.93, 0.47))
        dec.append({"cap": ref, "ic": "U1", "pin": ball, "rail": net})
    return _pcb(tmp_path_factory, name, body, w=40.0, h=30.0), \
        {"associations": dec}


def test_is_area_array():
    def fp(pads):
        return placelib.Footprint(ref="U", fpid="t:U", pos=(0, 0), angle=0,
                                  side="front", attrs=frozenset(),
                                  locked=False, pads=pads,
                                  courtyard_local=None)
    grid = [placelib.FpPad(f"{r}{c}", None, (c * 0.8, r * 0.8), (0.4, 0.4),
                           False) for r in range(8) for c in range(8)]
    assert place_seed.is_area_array(fp(grid))
    # a QFN-48: 12 pads a side and one exposed pad: perimeter, not a grid
    qfn = []
    for i in range(12):
        t = -2.75 + i * 0.5
        qfn += [placelib.FpPad(f"a{i}", None, (t, -3.5), (0.25, 0.8), False),
                placelib.FpPad(f"b{i}", None, (t, 3.5), (0.25, 0.8), False),
                placelib.FpPad(f"c{i}", None, (-3.5, t), (0.8, 0.25), False),
                placelib.FpPad(f"d{i}", None, (3.5, t), (0.8, 0.25), False)]
    qfn.append(placelib.FpPad("EP", None, (0, 0), (4, 4), False))
    assert not place_seed.is_area_array(fp(qfn))
    assert not place_seed.is_area_array(fp(grid[:30]))


def test_seed_puts_bga_decouplers_under_it_on_the_back(tmp_path_factory):
    p, dec = _bga_board(tmp_path_factory)
    ops, viol, facts, model = place_seed.seed(p, {"placement": {}}, dec,
                                              1.0, 0.8)
    assert not viol
    assert placelib.legality_violations(model, {}) == []
    u1 = model.footprints["U1"]
    room = u1.extents_abs().buffer(place_seed.BACKSIDE_REACH_MM + 1e-6,
                                   join_style=2)
    caps = [model.footprints[a["cap"]] for a in dec["associations"]]
    for c, a in zip(caps, dec["associations"]):
        assert c.side == "back", c.ref
        assert room.contains(c.extents_abs()), c.ref
        ball = [q for q in u1.pads if q.number == a["pin"]][0]
        bx, by = u1.to_abs(ball.local)
        assert math.dist(c.center_abs(), (bx, by)) < 4.0, c.ref
    # no two back-side caps overlap
    for i, a in enumerate(caps):
        for b in caps[i + 1:]:
            assert a.extents_abs().intersection(b.extents_abs()).area \
                <= placelib.EPS_AREA
    assert {o["side"] for o in ops if o["ref"].startswith("C")} == {"back"}
    ops2, _v, _f, _m = place_seed.seed(p, {"placement": {}}, dec, 1.0, 0.8)
    assert ops == ops2


def test_seed_backside_decoupling_off_keeps_front(tmp_path_factory):
    p, dec = _bga_board(tmp_path_factory, "bgaoff", ncaps=4)
    con = {"placement": {"backside_decoupling": False}}
    ops, viol, _f, model = place_seed.seed(p, con, dec, 1.0, 0.8)
    assert {model.footprints[f"C{i}"].side for i in range(1, 5)} == {"front"}
    assert not viol


def test_seed_side_pin_keeps_a_cap_on_its_side(tmp_path_factory):
    p, dec = _bga_board(tmp_path_factory, "bgapin", ncaps=4)
    con = {"placement": {"sides": [{"ref": "C2", "side": "front"}]}}
    _ops, viol, _f, model = place_seed.seed(p, con, dec, 1.0, 0.8)
    assert model.footprints["C2"].side == "front"
    assert model.footprints["C1"].side == "back"
    assert not viol


def test_anneal_inherits_back_side_slots_under_a_bga(tmp_path_factory):
    import place_anneal
    p, dec = _bga_board(tmp_path_factory, "bgainh", ncaps=6)
    _ops, _viol, _f, model = place_seed.seed(p, {"placement": {}}, dec,
                                             1.0, 0.8)
    clusters, _w = placelib.build_clusters(model, dec, {})
    c = [c for c in clusters if c.anchor == "U1"][0]
    warnings, reslotted = [], []
    slots = place_anneal._inherit_slots(model, c, dec, warnings, reslotted)
    assert reslotted == [], warnings
    assert set(slots) == {f"C{i}" for i in range(1, 7)}


def test_pad_margin_and_artwork_in_legality(tmp_path_factory):
    """constraints placement.pad_margin_mm lowers the overlap test's pad margin
    for an imported layout; artwork (no pads, not assembled) never overlaps;
    what isn't assembled may overhang the outline. Case: hw-lab eda-crossover."""
    def part(ref, x, y=10, attr="smd", pad=True):
        body = _pad("1", 0, 0) if pad else ""
        return (f'(footprint "L:{ref}" (layer "F.Cu") (at {x} {y}) '
                f'(property "Reference" "{ref}" (at 0 0 0) (layer "F.Fab")) (attr {attr}) {body})\n')
    off = "smd exclude_from_pos_files exclude_from_bom"
    body = (part("R1", 10) + part("R2", 11.0)            # pad boxes 1 mm wide: 0 gap apart
            + part("LOGO1", 10.5, attr="board_only exclude_from_pos_files exclude_from_bom", pad=False)
            + part("J1", 0.0, attr=off))
    m = _model(tmp_path_factory, "padm", body)
    v = placelib.legality_violations(m, None)
    assert any(x["kind"] == "courtyard_overlap" and x["refs"] == ["R1", "R2"] for x in v)
    assert not any("LOGO1" in (x.get("refs") or []) for x in v)
    assert not any(x["kind"] == "outside_outline" for x in v)
    m = _model(tmp_path_factory, "padm2", body)
    v = placelib.legality_violations(m, {"pad_margin_mm": 0.0})
    assert not any(x["kind"] == "courtyard_overlap" and x["refs"] == ["R1", "R2"] for x in v)
