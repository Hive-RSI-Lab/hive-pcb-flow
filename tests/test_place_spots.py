"""#852: place_spots - free spots for parts added after routing (was the
pinned find_spots.py on core and lite-main).

Pure tests build small boards and plant one obstacle each beside its quiet
twin: another net's track or via is kept off, the part's own net is not; a
hole is kept off through both sides, a courtyard only on its own side; a
rule area that keeps parts out is, one that only scopes a rule is not; a
notch and the copper-to-edge rule; a part turned to find_spots' other angle
when that fits at the target, and kept when "turn" is false; an off-centre
origin; each placed part an obstacle for the next; and the refusals.

The slow test works on a copy of pinned core: two routed resistors are given
targets beside a round capacitor's courtyard (find_spots judged its bounding
box), placed, moved there with place_edit --allow-routed, and KiCad's DRC
finds nothing new but the moved parts' silk, which silk_fix then clears,
and the unconnected ends the moves leave.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

import pytest
from shapely import affinity
from shapely.geometry import LineString, Point, box

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "tests"))

import env  # noqa: E402
import kc  # noqa: E402
import place_edit  # noqa: E402
import place_spots  # noqa: E402
import placelib  # noqa: E402
import silk_fix  # noqa: E402
from checklib import CheckError  # noqa: E402
from test_place import _fp, _pad  # noqa: E402

CORE = REPO / "pinned" / "core" / "kicad"
EDGE = placelib.COPPER_EDGE_DEFAULT       # no .kicad_pro beside these boards


def _pcb(tmp_path, body: str, outline: str | None = None,
         name: str = "b") -> Path:
    outline = outline or ('  (gr_rect (start 0 0) (end 30 20) (stroke '
                          '(width 0.1)) (fill no) (layer "Edge.Cuts"))\n')
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
{outline}{body})
""", encoding="utf-8")
    return p


def _track(x0, y0, x1, y1, net, layer="F.Cu", w=0.2):
    return (f'  (segment (start {x0} {y0}) (end {x1} {y1}) (width {w})'
            f' (layer "{layer}") (net "{net}"))\n')


def _via(x, y, net, size=0.6):
    return (f'  (via (at {x} {y}) (size {size}) (drill 0.3)'
            f' (layers "F.Cu" "B.Cu") (net "{net}"))\n')


def _rule_area(flags: str, pts=((13, 8), (17, 8), (17, 12), (13, 12))):
    xy = " ".join(f"(xy {x} {y})" for x, y in pts)
    return (f'  (zone (net 0) (net_name "") (layers "F.Cu") (name "ra")\n'
            f'    (keepout {flags})\n'
            f'    (polygon (pts {xy})))\n')


ALLOWED = ("(tracks allowed) (vias allowed) (pads allowed) "
           "(copperpour allowed) (footprints allowed)")


def _place(pcb, spec, constraints=None, **kw):
    return place_spots.place(placelib.PlaceModel(pcb), spec,
                             constraints or {}, **kw)


def _shape(spot, size):
    """The size box the spot judged, in board frame."""
    w, h = size
    if round(spot["deg"]) % 180:
        w, h = h, w
    cx, cy = spot["center"]
    return box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


NEW = {"near": [15, 10], "size": [2.0, 1.0]}


# ------------------------------------------------------------ free board

def test_free_target_is_the_spot(tmp_path):
    got = _place(_pcb(tmp_path, ""), {"X1": NEW})["X1"]
    assert (got["x"], got["y"], got["deg"]) == (15, 10, 0)
    assert got["from_near_mm"] == 0 and not got["turned"]
    assert got["side"] == "front" and not got["on_board"]


# ------------------------------------------------------------ copper

@pytest.mark.parametrize("item", ["track", "via"])
def test_other_net_copper_kept_off_own_net_not(tmp_path, item):
    body = (_track(5, 10, 25, 10, "A") if item == "track"
            else _via(15, 10, "A"))
    pcb = _pcb(tmp_path, body)
    cu = (LineString([(5, 10), (25, 10)]).buffer(0.1) if item == "track"
          else Point(15, 10).buffer(0.3))
    # planted: a part of another net is moved off the copper by --copper
    other = _place(pcb, {"X1": NEW})["X1"]
    assert other["from_near_mm"] > 0
    assert _shape(other, NEW["size"]).distance(cu) >= place_spots.COPPER
    # and no farther than the next ring out needs
    assert _shape(other, NEW["size"]).distance(cu) < \
        place_spots.COPPER + place_spots.STEP + 0.01
    # twin: the part's own net may sit on it
    own = _place(pcb, {"X1": {**NEW, "nets": ["A"]}})["X1"]
    assert own["from_near_mm"] == 0


def test_back_copper_does_not_block_a_front_part(tmp_path):
    pcb = _pcb(tmp_path, _track(5, 10, 25, 10, "A", layer="B.Cu"))
    assert _place(pcb, {"X1": NEW})["X1"]["from_near_mm"] == 0
    back = _place(pcb, {"X1": {**NEW, "side": "back"}})["X1"]
    assert back["from_near_mm"] > 0 and back["side"] == "back"


# ------------------------------------------------------------ parts, holes

def test_hole_through_both_sides_courtyard_only_its_own(tmp_path):
    # a back-side SMD part under the target: no obstacle to a front part
    smd = _fp("B1", 15, 10, layer="B.Cu", pads=_pad("1", 0, 0, "N",
                                                    layers='"B.Cu"'))
    assert _place(_pcb(tmp_path, smd, name="smd"),
                  {"X1": NEW})["X1"]["from_near_mm"] == 0
    # the same part with a through hole: the hole goes through the board
    hole = ('    (pad "1" thru_hole circle (at 0 0) (size 1.2 1.2) '
            '(drill 0.8) (layers "*.Cu") (net "N"))\n')
    th = _fp("B1", 15, 10, layer="B.Cu", pads=hole, attr="through_hole")
    got = _place(_pcb(tmp_path, th, name="th"), {"X1": NEW})["X1"]
    assert got["from_near_mm"] > 0
    assert _shape(got, NEW["size"]).distance(Point(15, 10).buffer(0.6)) \
        >= place_spots.MARGIN
    # a back part's NPTH peg hole with no copper layer at all, too
    peg = ('    (pad "" np_thru_hole circle (at 0 0) (size 1.2 1.2) '
           '(drill 1.2) (layers "*.Mask"))\n')
    np = _fp("B1", 15, 10, layer="B.Cu", pads=peg, attr="through_hole")
    got = _place(_pcb(tmp_path, np, name="np"), {"X1": NEW})["X1"]
    assert _shape(got, NEW["size"]).distance(Point(15, 10).buffer(0.6)) \
        >= place_spots.MARGIN
    # a front part's courtyard is kept off by --margin
    front = _fp("F1", 15, 10)
    got = _place(_pcb(tmp_path, front, name="fr"), {"X1": NEW})["X1"]
    assert _shape(got, NEW["size"]).distance(box(14, 9, 16, 11)) \
        >= place_spots.MARGIN - 1e-6


def test_parts_placed_in_turn_keep_apart(tmp_path):
    got = _place(_pcb(tmp_path, ""), {"X1": NEW, "X2": NEW, "X3": NEW})
    shapes = [_shape(got[r], NEW["size"]) for r in ("X1", "X2", "X3")]
    assert got["X1"]["from_near_mm"] == 0
    for i in range(3):
        for j in range(i + 1, 3):
            assert shapes[i].distance(shapes[j]) >= place_spots.MARGIN - 1e-6


# ------------------------------------------------------------ rule areas

def test_rule_area_keeping_parts_out_is_kept_off_a_scoping_one_is_not(
        tmp_path):
    keep = _rule_area(ALLOWED.replace("(footprints allowed)",
                                      "(footprints not_allowed)"))
    got = _place(_pcb(tmp_path, keep, name="keep"), {"X1": NEW})["X1"]
    assert not _shape(got, NEW["size"]).intersects(box(13, 8, 17, 12))
    # twin: an area that only scopes a rule (all allowed)
    scope = _rule_area(ALLOWED)
    assert _place(_pcb(tmp_path, scope, name="scope"),
                  {"X1": NEW})["X1"]["from_near_mm"] == 0


def test_rule_area_is_judged_by_its_shape_not_its_bbox(tmp_path):
    # a triangle whose bounding box holds the target, the triangle not
    tri = _rule_area(ALLOWED.replace("(tracks allowed)",
                                     "(tracks not_allowed)"),
                     pts=((12, 6), (18, 6), (18, 8)))
    assert _place(_pcb(tmp_path, tri), {"X1": {**NEW, "near": [14, 8.5]}}
                  )["X1"]["from_near_mm"] == 0


def test_constraints_keepout_is_kept_off(tmp_path):
    pcb = _pcb(tmp_path, "")
    cons = {"placement": {"keepouts": [{"rect": [14, 9, 16, 11],
                                        "reason": "antenna"}]}}
    got = _place(pcb, {"X1": NEW}, cons)["X1"]
    assert not _shape(got, NEW["size"]).intersects(box(14, 9, 16, 11))
    assert _place(pcb, {"X1": NEW})["X1"]["from_near_mm"] == 0


# ------------------------------------------------------------ outline

NOTCHED = "".join(
    f'  (gr_line (start {a} {b}) (end {c} {d}) (stroke (width 0.1))'
    f' (layer "Edge.Cuts"))\n'
    for a, b, c, d in ((0, 0, 20, 0), (20, 0, 20, 8), (20, 8, 30, 8),
                       (30, 8, 30, 20), (30, 20, 0, 20), (0, 20, 0, 0)))


def test_notch_is_off_the_board_its_bbox_is_not(tmp_path):
    pcb = _pcb(tmp_path, "", NOTCHED)
    got = _place(pcb, {"X1": {**NEW, "near": [25, 5]}})["X1"]   # in the notch
    board = box(0, 0, 30, 20).difference(box(20, 0, 30, 8))
    shp = _shape(got, NEW["size"])
    assert board.buffer(-EDGE, join_style="mitre").contains(shp)
    assert got["from_near_mm"] == pytest.approx(
        math.dist(got["center"], (25, 5)), abs=1e-3)
    # twin: just inside the notch's inner edge, it stays
    near = [25, 8 + EDGE + 0.5]
    assert _place(pcb, {"X1": {**NEW, "near": near}}
                  )["X1"]["from_near_mm"] == 0


def test_edge_rule_from_the_project_or_the_flag(tmp_path):
    pcb = _pcb(tmp_path, "")
    near = {"X1": {**NEW, "near": [1.4, 10], "turn": False}}   # 0.4 to edge
    assert _place(pcb, near)["X1"]["from_near_mm"] > 0   # default 0.5 edge
    assert _place(pcb, near, edge=0.3)["X1"]["from_near_mm"] == 0
    pcb.with_suffix(".kicad_pro").write_text(json.dumps({"board": {
        "design_settings": {"rules": {"min_copper_edge_clearance": 0.3}}}}))
    assert _place(pcb, near)["X1"]["from_near_mm"] == 0


# ------------------------------------------------------------ angle, origin

def test_turns_to_the_other_angle_when_that_fits_at_the_target(tmp_path):
    # courtyards 2.0 mm apart: a 2.0 x 1.0 part fits between them only
    # upright - find_spots' other angle for deg 0 is 90
    body = _fp("L1", 13, 10) + _fp("R1", 17, 10)
    pcb = _pcb(tmp_path, body)
    got = _place(pcb, {"X1": NEW})["X1"]
    assert (got["deg"], got["turned"], got["from_near_mm"]) == (90, True, 0)
    # deg 90's other angle is 0, not 180: the part is not flipped end to end
    assert place_spots.Subject("X", {**NEW, "deg": 90}, None).degs() == \
        [90, 0]
    # twin: "turn": false keeps deg 0 and goes farther
    kept = _place(pcb, {"X1": {**NEW, "turn": False}})["X1"]
    assert kept["deg"] == 0 and not kept["turned"]
    assert kept["from_near_mm"] > 0


def test_on_board_part_uses_its_own_shape_and_origin(tmp_path):
    # courtyard 4 x 2 off its origin by (+1, 0); a deg-90 target
    part = _fp("U1", 5, 5, cy=(-1, -1, 3, 1), pads=_pad("1", 0, 0, "S"))
    pcb = _pcb(tmp_path, part + _track(5, 15, 25, 15, "S"))
    got = _place(pcb, {"U1": {"near": [15, 10], "deg": 90}})["U1"]
    assert got["on_board"] and got["center"] == [15, 10]
    # the origin sits where the offset, turned 90, puts it
    fp = placelib.PlaceModel(pcb).footprints["U1"]
    c = affinity.rotate(Point(1, 0), -90, origin=(0, 0))
    assert (got["x"], got["y"]) == pytest.approx((15 - c.x, 10 - c.y))
    assert fp.pos == (5, 5)                       # the board is untouched
    # its own pad net's track is no obstacle, near or not
    got = _place(pcb, {"U1": {"near": [15, 15], "deg": 0}})["U1"]
    assert got["from_near_mm"] == 0


# ------------------------------------------------------------ refusals, CLI

@pytest.mark.parametrize("spec, msg", [
    ({"X1": {"near": [15, 10]}}, "size"),
    ({"X1": {"size": [1, 1]}}, "near"),
    ({"X1": {**NEW, "side": "top"}}, "side must be"),
    ({"X1": {**NEW, "turn": "no"}}, "turn must be"),
    ({"X1": {**NEW, "nets": "A"}}, "nets must be"),
])
def test_bad_spec_refused(tmp_path, spec, msg):
    with pytest.raises(CheckError, match=msg):
        _place(_pcb(tmp_path, ""), spec)


def test_no_spot_within_the_radius_refused(tmp_path):
    pcb = _pcb(tmp_path, _fp("F1", 15, 10, cy=(-14, -9, 14, 9)))
    with pytest.raises(CheckError, match="X1: no free spot within 5"):
        _place(pcb, {"X1": NEW}, max_radius=5)


def test_cli_writes_placements_and_ops(tmp_path):
    part = _fp("U1", 5, 5, pads=_pad("1", 0, 0, "S"))
    pcb = _pcb(tmp_path, part)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"U1": {"near": [15, 10]}, "X1": NEW}))
    out, ops = tmp_path / "pl.json", tmp_path / "ops.json"
    payload, _ = place_spots.run(["--pcb", str(pcb), "--spec", str(spec),
                                  "--out", str(out), "--ops-out", str(ops)])
    assert set(payload["facts"]["spots"]) == {"U1", "X1"}
    pl = json.loads(out.read_text())
    assert set(pl) == {"U1", "X1"}
    assert set(pl["X1"]) == {"x", "y", "deg", "side"}
    doc = json.loads(ops.read_text())
    assert [o["ref"] for o in doc["ops"]] == ["U1"]   # only what is placed
    assert place_edit.validate_ops(doc)
    for bad in (["--step", "0"], ["--margin", "-1"], ["--edge", "nan"]):
        with pytest.raises(CheckError):
            place_spots.run(["--pcb", str(pcb), "--spec", str(spec), *bad])


# ------------------------------------------------------------ slow (KiCad)

ENDS = {"unconnected_items", "track_dangling", "via_dangling"}


def _drc_types(cli, pcb):
    rep = kc.run_drc(cli, pcb, refill=True)
    out = {}
    for v in rep["violations"]:
        out[v["check"]] = out.get(v["check"], 0) + 1
    return out


def test_spots_on_a_copy_of_core_pass_drc(tmp_path):
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad not installed")
    dst = tmp_path / "core"
    shutil.copytree(CORE, dst, ignore=shutil.ignore_patterns("gen"))
    pcb = dst / "core.kicad_pcb"
    before = _drc_types(cli, pcb)
    # R149's target sits by the corner of C311's round courtyard, inside
    # its bounding box - find_spots went 1.8 mm farther
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"R149": {"near": [92.15, 147.09]},
                                "R150": {"near": [101.0, 147.7]}}))
    ops = tmp_path / "ops.json"
    payload, _ = place_spots.run(["--pcb", str(pcb), "--spec", str(spec),
                                  "--ops-out", str(ops)])
    spots = payload["facts"]["spots"]
    c311 = placelib.PlaceModel(pcb).footprints["C311"].judge_extents_abs()
    assert box(*c311.bounds).contains(Point(spots["R149"]["center"]))
    place_edit.run(["--pcb", str(pcb), "--allow-routed", "--ops", str(ops)])
    m = placelib.PlaceModel(pcb)
    for ref, s in spots.items():
        assert m.footprints[ref].pos == pytest.approx((s["x"], s["y"]))
    after = _drc_types(cli, pcb)
    new = {k: n - before.get(k, 0) for k, n in after.items()
           if n > before.get(k, 0)}
    # courtyards, holes, copper and edge are all place_spots judges (as
    # find_spots did); a moved part's silk is silk_fix's, next in the flow
    assert set(new) <= ENDS | {"silk_overlap", "silk_over_copper",
                               "silk_edge_clearance"}, new
    silk_fix.run(["--pcb", str(pcb)])
    after = _drc_types(cli, pcb)
    new = {k: n - before.get(k, 0) for k, n in after.items()
           if n > before.get(k, 0)}
    assert set(new) <= ENDS, new
