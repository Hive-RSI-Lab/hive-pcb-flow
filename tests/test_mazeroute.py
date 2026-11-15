"""mazeroute: route_edit's route / route_group / reroute ops and zone_patch's
corridor mode (#926 #927 #928), on three golden boards that each tool must
route (tests/fixtures/mazeroute, rebuilt by make_fixture.py):

  corridor  a via fence with one gap: the corridor must go through it
  reroute   A is walled off by B's track: reroute rips B, routes A, puts B
            back in one piece
  group     three nets through one gap in a wall: greedy order fails at the
            third, PathFinder routes all three

Pure tests (venv only) drive the router. The live tests apply the ops with
KiCad and want no DRC finding on the routed nets (the boards' GND copper is
bare fence and walls: its own findings are not counted).
"""
from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, box

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import env  # noqa: E402
import geom  # noqa: E402
import mazeroute  # noqa: E402
import route_finish as rf  # noqa: E402
import route_edit  # noqa: E402
import zone_patch  # noqa: E402
from checklib import CheckError  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "mazeroute"
B_TRACK = "00000000-0000-0000-0000-0000000002b1"
ABC = [{"net": n, "from": f"{n}1.1", "to": f"{n}2.1"} for n in "ABC"]


def _board(tmp_path, name):
    for src in FIX.glob(f"{name}.*"):
        shutil.copy2(src, tmp_path / src.name)
    return tmp_path / f"{name}.kicad_pcb"


def _bg(name):
    return geom.BoardGeom.from_file(FIX / f"{name}.kicad_pcb")


def _tracks(ops, net=None):
    return [o for o in ops if o["op"] == "add_track"
            and (net is None or o["net"] == net)]


def _gap(ops, rules):
    """The least edge-to-edge distance minus clearance between the added
    tracks of different nets (>= 0 when every pair keeps its clearance)."""
    shapes = [(o["net"], o["layer"], LineString([o["start"], o["end"]])
               .buffer(o["width"] / 2)) for o in _tracks(ops)]
    worst = float("inf")
    for i, (na, la, a) in enumerate(shapes):
        for nb, lb, b in shapes[i + 1:]:
            if na != nb and la == lb:
                worst = min(worst, a.distance(b) - rules.clr(na, nb))
    return worst


# ------------------------------------------------------------ ends

def test_end_specs():
    r = mazeroute.Router(_bg("group"))
    assert set(r.end("A1.1", "A")) == {"F.Cu"}
    assert set(r.end("A1", "A")) == {"F.Cu"}
    assert set(r.end("3,4@B.Cu", "A")) == {"B.Cu"}
    assert set(r.end("3,4", "A")) == set(r.layers)
    assert len(r.end("A1.1|A2.1", "A")["F.Cu"]) == 2


@pytest.mark.parametrize("spec, msg", [
    ("Z9.1", "no such pad"),
    ("B1.1", "is on net 'B'"),
    ("B1", "no pad of B1 on 'A'"),
    ("3,4@In1.Cu", "not a routable layer"),
    ("3,x", "want x,y"),
    ("", "names nothing"),
])
def test_end_refuses(spec, msg):
    with pytest.raises(CheckError, match=msg):
        mazeroute.Router(_bg("group")).end(spec, "A")


# ------------------------------------------------------------ corridor (#926)

def test_corridor_goes_through_the_gap():
    bg = _bg("corridor")
    polys, ops = mazeroute.Router(bg, layers=["F.Cu"]).corridor(
        "A", "P1.1", "P2.1", 0.5)
    poly = polys["F.Cu"]
    assert poly.geom_type == "Polygon"
    for pad in bg.pads_of(net="A"):             # it lands on both pads
        assert poly.intersection(pad.poly).area > 0.1, pad.ref
    # it crosses the fence only in the gap between the vias at y=4.8, 7.2
    cut = poly.intersection(LineString([(10, 0), (10, 10)]))
    assert 5.1 < cut.bounds[1] and cut.bounds[3] < 6.9
    for v in bg.vias_of(net="GND"):
        assert poly.distance(v.poly) > 0.1, v.at
    assert all(o["op"] == "add_track" for o in ops)


def test_zone_patch_corridor_dry_run(tmp_path):
    pcb = _board(tmp_path, "corridor")
    raw = pcb.read_bytes()
    p, _ = zone_patch.run(["--pcb", str(pcb), "--net", "A", "--from", "P1.1",
                           "--to", "P2.1", "--half-width", "0.5",
                           "--dry-run"])
    assert p["status"] == "pass" and p["corridor"] == ["P1.1", "P2.1"]
    xs = [x for x, _ in p["outline"]]
    assert min(xs) < 3 and max(xs) > 17
    assert pcb.read_bytes() == raw


@pytest.mark.parametrize("args, msg", [
    (["--from", "P1.1"], "both --from and --to"),
    (["--from", "P1.1", "--to", "P2.1", "--poly", "0,0 1,0 1,1"],
     "no --poly"),
    (["--from", "P1.1", "--to", "P2.1", "--half-width", "0"], "> 0"),
])
def test_zone_patch_corridor_refuses(tmp_path, args, msg):
    pcb = _board(tmp_path, "corridor")
    with pytest.raises(CheckError, match=msg):
        zone_patch.run(["--pcb", str(pcb), "--net", "A", *args])


# ------------------------------------------------------------ reroute (#927)

def test_plain_route_is_walled_off():
    with pytest.raises(CheckError):
        mazeroute.Router(_bg("reroute")).route("A", "A1.1", "A2.1")


def test_reroute_rips_b_and_puts_it_back_whole():
    r = mazeroute.Router(_bg("reroute"))
    assert len(r.islands("B")) == 1
    removed, ops, _ = r.reroute("A", [14, 14, 16, 16], "A1.1", "A2.1")
    assert removed == [B_TRACK]                # never the GND walls
    assert _tracks(ops, "A") and _tracks(ops, "B")
    assert len(r.islands("A")) == 1 and len(r.islands("B")) == 1
    assert _gap(ops, r.rules) >= -1e-6


def test_reroute_keep_and_one_layer_refuse():
    with pytest.raises(CheckError):
        mazeroute.Router(_bg("reroute")).reroute(
            "A", [14, 14, 16, 16], "A1.1", "A2.1", keep={"B"})
    with pytest.raises(CheckError):
        mazeroute.Router(_bg("reroute"), layers=["F.Cu"]).reroute(
            "A", [14, 14, 16, 16], "A1.1", "A2.1")


def test_reroute_that_fails_leaves_the_board_byte_identical(tmp_path):
    """The router fails in resolve_ops, before KiCad is started."""
    pcb = _board(tmp_path, "reroute")
    raw = pcb.read_bytes()
    with pytest.raises(CheckError):
        route_edit.apply_ops(pcb, route_edit.validate_ops({"version": 1, "ops": [
            {"op": "reroute", "net": "A", "region": [14, 14, 16, 16],
             "from": "A1.1", "to": "A2.1", "layers": ["F.Cu"]}]}))
    assert pcb.read_bytes() == raw


def test_resolve_reroute_writes_guards():
    guards: dict = {}
    flat, spans = route_edit.resolve_ops(_bg("reroute"), [
        {"op": "reroute", "net": "A", "region": [14, 14, 16, 16]}], guards)
    assert guards == {"A": "lt", "B": "le"}
    assert flat[0] == {"op": "remove", "uuid": B_TRACK}
    assert spans == [list(range(len(flat)))]


def test_check_guards():
    bg = _bg("reroute")
    assert route_edit.check_guards(bg, bg, {"B": "le"}) == []
    assert route_edit.check_guards(bg, bg, {"A": "lt"}) == ["A: 2 -> 2 islands"]


# ------------------------------------------------------------ group (#928)

def test_greedy_fails_where_the_group_routes():
    bg = _bg("group")
    r = mazeroute.Router(bg, layers=["F.Cu"])
    with pytest.raises(CheckError):
        for jb in ABC:
            r.route(jb["net"], jb["from"], jb["to"])
    r = mazeroute.Router(bg, layers=["F.Cu"])
    ops = r.group(ABC)
    assert {o["net"] for o in _tracks(ops)} == {"A", "B", "C"}
    assert _gap(ops, r.rules) >= -1e-6
    for n in "ABC":
        assert len(r.islands(n)) == 1, n


def test_ops_in_one_list_route_around_each_other():
    """A route op after a route_group sees the group's tracks."""
    flat, spans = route_edit.resolve_ops(_bg("group"), [
        {"op": "route_group", "jobs": ABC[:2], "layers": ["F.Cu"]},
        {"op": "route", "net": "C", "from": "C1.1", "to": "C2.1",
         "layers": ["F.Cu"]}])
    rules = mazeroute.Router(_bg("group")).rules
    assert _gap(flat, rules) >= -1e-6
    assert {flat[j]["net"] for j in spans[1]} == {"C"}


# ------------------------------------------------------------ validation

@pytest.mark.parametrize("op, msg", [
    ({"op": "route", "net": "A", "from": "A1.1"}, "missing"),
    ({"op": "route", "net": "A", "from": "A1.1", "to": "A2.1", "grid": 2},
     "grid must be"),
    ({"op": "route", "net": "A", "from": "A1.1", "to": "A2.1", "layers": 3},
     "layers must be"),
    ({"op": "route_group", "jobs": []}, "non-empty list"),
    ({"op": "route_group", "jobs": [{"net": "A", "from": "A1.1"}]},
     "needs net, from, to"),
    ({"op": "route_group", "jobs": [dict(ABC[0], via=1)]}, "unknown keys"),
    ({"op": "route_group", "jobs": [dict(ABC[0], width=0)]}, "width must"),
    ({"op": "route_group", "jobs": ABC, "iterations": 0}, "iterations"),
    ({"op": "reroute", "net": "A", "region": [5, 5, 1, 1]}, "region must"),
    ({"op": "reroute", "net": "A", "region": [0, 0, 1, 1], "from": "A1.1"},
     "from and to together"),
    ({"op": "reroute", "net": "A", "region": [0, 0, 1, 1], "keep": [1]},
     "keep must"),
])
def test_validate_refuses(op, msg):
    with pytest.raises(CheckError, match=msg):
        route_edit.validate_ops({"version": 1, "ops": [op]})


def test_apply_refuses_unknown_net_and_layer(tmp_path):
    pcb = _board(tmp_path, "group")
    with pytest.raises(CheckError, match="net 'Z' not on board"):
        route_edit.apply_ops(pcb, [{"op": "route_group", "jobs": [
            {"net": "Z", "from": "A1.1", "to": "A2.1"}]}])
    with pytest.raises(CheckError, match="In1.Cu"):
        route_edit.apply_ops(pcb, [dict(ABC[0], op="route",
                                        layers=["In1.Cu"])])


# ------------------------------------------------------------ DRU bans (#1140)

_FOUR = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal)
    (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (net 0 "") (net 1 "GND") (net 2 "A") (net 3 "B")
"""
_FLOOR = """(version 1)
(rule "aiee_clearance_floor"
\t(constraint clearance (min 0.1500mm))
)
(rule "aiee_hole_to_hole_floor"
\t(constraint hole_to_hole (min 0.5000mm))
)
(rule "aiee_edge_clearance_floor"
\t(constraint edge_clearance (min 0.3000mm))
)
"""
_A_OFF_IN1 = """(rule "aiee_diff_outer_only_A_In1"
\t(layer "In1.Cu")
\t(constraint disallow track)
\t(condition "A.NetName == 'A'")
)
"""
_A_NO_VIA = """(rule "aiee_sw_novia_A"
\t(constraint disallow via)
\t(condition "A.NetName == 'A'")
)
(rule "aiee_sw_layer_A"
\t(constraint disallow track)
\t(condition "A.NetName == 'A' && A.Layer != 'F.Cu'")
)
"""


def _four(tmp_path, dru="", f_gap=False, pro=None, own=False):
    """30 x 20, four layers: A's pads at (5,10), (25,10) and B's at (5,12),
    (25,12) on F.Cu; a GND wall at x=15 on F.Cu and B.Cu from edge to edge
    (f_gap: F.Cu's opens at the board's top edge, y < 1.7), on In2 with a
    gap at y=14, none on In1. The straight way is a via down to In1. pro:
    a .kicad_pro's JSON; own: In1.Cu has the board's own name SIG1."""
    import make_fixture as mf
    body = (mf.pad("A1", 5, 10, "A") + mf.pad("A2", 25, 10, "A")
            + mf.pad("B1", 5, 12, "B") + mf.pad("B2", 25, 12, "B"))
    top = 2.2 if f_gap else 0.5
    body += mf.seg(15, top, 15, 19.5, 1.0, "F.Cu", "GND",
                   "00000000-0000-0000-0000-0000000004f1")
    body += mf.seg(15, 0.5, 15, 19.5, 1.0, "B.Cu", "GND",
                   "00000000-0000-0000-0000-0000000004b1")
    body += mf.seg(15, 0.5, 15, 12.8, 1.0, "In2.Cu", "GND",
                   "00000000-0000-0000-0000-0000000004c1")
    body += mf.seg(15, 15.2, 15, 19.5, 1.0, "In2.Cu", "GND",
                   "00000000-0000-0000-0000-0000000004c2")
    pcb = tmp_path / "four.kicad_pcb"
    head = _FOUR.replace('(4 "In1.Cu" signal)',
                         '(4 "In1.Cu" signal "SIG1")') if own else _FOUR
    pcb.write_text(head + '  (gr_rect (start 0 0) (end 30 20) (stroke'
                   ' (width 0.1)) (fill no) (layer "Edge.Cuts"))\n'
                   + body + ")\n", encoding="utf-8")
    pcb.with_suffix(".kicad_dru").write_text(_FLOOR + dru, encoding="utf-8")
    if pro is not None:
        pcb.with_suffix(".kicad_pro").write_text(json.dumps(pro),
                                                 encoding="utf-8")
    return geom.BoardGeom.from_file(pcb)


def _layers_of(ops, net):
    return {o["layer"] for o in _tracks(ops, net)}


@pytest.fixture
def mf_path():
    sys.path.insert(0, str(FIX))
    yield
    sys.path.remove(str(FIX))


def test_a_track_ban_keeps_the_route_off_the_layer(tmp_path, mf_path):
    """#1140: /USB0_DM went down to In1 though the DRU disallows its
    tracks there; the router now reads the rule and takes In2's gap."""
    free = mazeroute.Router(_four(tmp_path)).route("A", "A1.1", "A2.1")
    assert "In1.Cu" in _layers_of(free, "A")
    r = mazeroute.Router(_four(tmp_path, _A_OFF_IN1))
    assert r.net_layers("A") == ["F.Cu", "In2.Cu", "B.Cu"]
    assert r.net_layers("B") == r.layers
    ops = r.route("A", "A1.1", "A2.1")
    assert _layers_of(ops, "A") == {"F.Cu", "In2.Cu"}
    assert [o for o in ops if o["op"] == "add_via"]  # vias still cross In1
    assert len(r.islands("A")) == 1
    assert r.unread_rules == []
    with pytest.raises(CheckError, match="disallows A's tracks on In1"):
        r.end("20,10@In1.Cu", "A")
    assert set(r.end("20,10@In2.Cu", "A")) == {"In2.Cu"}
    with pytest.raises(CheckError, match="keeps A off In1.Cu"):
        mazeroute.Router(_four(tmp_path, _A_OFF_IN1),
                         layers=["F.Cu", "In1.Cu"]).route("A", "A1.1", "A2.1")


def test_a_via_ban_keeps_the_route_on_its_layer(tmp_path, mf_path):
    """A switch node (rules_gen's sw_novia + sw_layer): no via, F.Cu only,
    round the wall's open end (a margin that takes the window there)."""
    free = mazeroute.Router(_four(tmp_path, f_gap=True), margin=8).route(
        "A", "A1.1", "A2.1")
    assert [o for o in free if o["op"] == "add_via"]
    r = mazeroute.Router(_four(tmp_path, _A_NO_VIA, f_gap=True), margin=8)
    ops = r.route("A", "A1.1", "A2.1")
    assert not [o for o in ops if o["op"] == "add_via"]
    assert _layers_of(ops, "A") == {"F.Cu"}
    assert min(y for o in _tracks(ops) for y in (o["start"][1],
                                                o["end"][1])) < 1.7
    with pytest.raises(CheckError, match="no path"):   # walled: no way
        mazeroute.Router(_four(tmp_path, _A_NO_VIA), margin=8).route(
            "A", "A1.1", "A2.1")


def test_group_with_one_banned_net(tmp_path, mf_path):
    """route_group's grids differ in layers when a net is banned from one:
    each claims cells on the router's layers, not its own grid's."""
    r = mazeroute.Router(_four(tmp_path, _A_OFF_IN1))
    ops = r.group([{"net": "A", "from": "A1.1", "to": "A2.1"},
                   {"net": "B", "from": "B1.1", "to": "B2.1"}])
    assert "In1.Cu" not in _layers_of(ops, "A")
    assert _layers_of(ops, "A") and _layers_of(ops, "B")
    assert _gap(ops, r.rules) >= -1e-6
    for n in "AB":
        assert len(r.islands(n)) == 1, n


def test_a_class_ban_on_the_layers_own_name(tmp_path, mf_path):
    """#1140: a rule may name its nets by class (the .kicad_pro's) and its
    layer by the board's own name for it; the router reads both."""
    dru = _A_OFF_IN1.replace('"In1.Cu"', '"SIG1"').replace(
        "A.NetName == 'A'", "A.NetClass == 'USB'")
    pro = {"net_settings": {
        "classes": [{"name": "Default"}, {"name": "USB"}],
        "netclass_patterns": [{"netclass": "USB", "pattern": "A"}]}}
    bg = _four(tmp_path, dru, pro=pro, own=True)
    assert bg.layer_user_names == {"In1.Cu": "SIG1"}
    r = mazeroute.Router(bg)
    assert r.unread_rules == []
    assert r.net_layers("A") == ["F.Cu", "In2.Cu", "B.Cu"]
    assert r.net_layers("B") == r.layers
    assert _layers_of(r.route("A", "A1.1", "A2.1"), "A") == {
        "F.Cu", "In2.Cu"}


def test_an_unread_rule_is_named_once(tmp_path, mf_path, capsys):
    dru = _A_OFF_IN1.replace("A.NetName == 'A'", "A.Type == 'track'")
    mazeroute._WARNED.discard("aiee_diff_outer_only_A_In1")
    r = mazeroute.Router(_four(tmp_path, dru))
    assert r.unread_rules == ["aiee_diff_outer_only_A_In1"]
    assert r.net_layers("A") == r.layers           # left to DRC
    mazeroute.Router(_four(tmp_path, dru))
    assert capsys.readouterr().err.count("aiee_diff_outer_only_A_In1") == 1


# ------------------------------------------------------------ thermal spokes (#1140)

_ZONE = """  (zone (net "GND") (layer "F.Cu") (uuid "00000000-0000-0000-0000-0000000005a1")
    (hatch edge 0.5) (connect_pads (clearance 0.25)) (min_thickness 0.25)
    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 0.5 9.2) (xy 19.5 9.2) (xy 19.5 10.8) (xy 0.5 10.8)))
{fills})
"""


def _thermal(tmp_path, body=""):
    """20 x 20, F.Cu and B.Cu: G1, a 1 mm GND pad at (10, 10), in a GND
    pour across the board (y 9.2 to 10.8) with a 0.5 mm thermal gap and two
    0.5 mm spokes, left and right, each into its own half of the pour; A's
    pads at (11.4, 15) and (11.4, 5) either side. A's straight way down
    crosses the right spoke's end. `body` adds copper."""
    import make_fixture as mf
    band = box(0.5, 9.2, 19.5, 10.8).difference(
        box(9.5, 9.5, 10.5, 10.5).buffer(0.5))
    fill = band.union(box(10.4, 9.75, 11.2, 10.25)).union(
        box(8.8, 9.75, 9.6, 10.25))
    polys = list(getattr(fill, "geoms", [fill]))
    assert len(polys) == 2
    fills = "".join(
        '    (filled_polygon (layer "F.Cu") (pts '
        + " ".join(f"(xy {x:.4f} {y:.4f})" for x, y in p.exterior.coords[:-1])
        + "))\n" for p in polys)
    body = (mf.pad("G1", 10, 10, "GND") + mf.pad("A1", 11.4, 15, "A")
            + mf.pad("A2", 11.4, 5, "A") + _ZONE.format(fills=fills) + body)
    pcb = mf.write(tmp_path / "thermal.kicad_pcb", body, 20, 20)
    return geom.BoardGeom.from_file(pcb)


def _obs(bg):
    obs = rf.Obstacles(bg, spokes=True)
    obs._thermal_index()
    return obs


def _pad(shape="rect", size=(1.0, 1.0), angle=0.0, **kw):
    return geom.Pad(ref="P", number="1", net="GND", shape=shape, size=size,
                    center=(0.0, 0.0), angle=angle, rratio=0.0,
                    layers=("F.Cu",), **kw)


def test_spoke_end_is_where_kicad_ends_the_spoke():
    """zone_filler.cpp: a spoke runs from the pad's box centre to the box
    grown by gap + 0.005 + min thickness / 2 (0.63 here), along the box's
    own axes; a round pad's box is met on its axis whatever the angle."""
    g = 0.5 + rf.SPOKE_EPS + 0.25 / 2
    rect = _pad(size=(2.0, 1.0))
    (x, y), u = rf.spoke_end(rect, (1.25, 0.0), 0.5, 0.25)
    assert (x, y) == pytest.approx((1.0 + g, 0.0)) and u == pytest.approx((1, 0))
    (x, y), _ = rf.spoke_end(rect, (0.0, -0.75), 0.5, 0.25)
    assert (x, y) == pytest.approx((0.0, -(0.5 + g)))
    (x, y), _ = rf.spoke_end(rect, (1.0, 1.0), 0.5, 0.25)  # meets the top
    assert (x, y) == pytest.approx((0.5 + g, 0.5 + g))
    # turned 90 degrees, the long side runs along y
    (x, y), _ = rf.spoke_end(_pad(size=(2.0, 1.0), angle=90.0), (0.0, 1.25),
                             0.5, 0.25)
    assert (x, y) == pytest.approx((0.0, 1.0 + g))
    (x, y), _ = rf.spoke_end(_pad("circle"), (0.5, 0.5), 0.5, 0.25)
    assert math.hypot(x, y) == pytest.approx(0.5 + g)
    assert rf.spoke_end(rect, (0.0, 0.0), 0.5, 0.25) is None


def test_thermal_spokes_and_keep_outs(tmp_path, mf_path):
    bg = _thermal(tmp_path)
    [(p, z, layer, gap, sw, mt)] = rf.thermal_pads(bg)
    assert (p.ref, layer, gap, sw, mt) == ("G1", "F.Cu", 0.5, 0.5, 0.25)
    live = rf.thermal_spokes(p, z.fills["F.Cu"], gap, sw, mt)
    assert len(live) == 2
    want = box(10.5, 9.75, 11.13, 10.25).union(Point(11.13, 10).buffer(0.25))
    [right] = [s for _, s in live if s.centroid.x > 10]
    assert right.symmetric_difference(want).area < 1e-3
    obs = rf.Obstacles(bg)
    assert obs.spokes_near(box(0, 0, 20, 20)) == []      # off by default
    obs = rf.Obstacles(bg, spokes=True)
    near = obs.spokes_near(box(0, 0, 20, 20))
    assert [(n, ly, c) for n, ly, _, c in near] == [("GND", "F.Cu", 0.25)] * 2


def test_a_route_leaves_a_thermal_pads_spokes(tmp_path, mf_path):
    """#1140 (C221 on the voice board): a route that crosses where a
    spoke ends starves the pad at the refill; with spokes=True the router
    keeps the zone's clearance off it."""
    def worst(spokes):
        bg = _thermal(tmp_path)
        r = mazeroute.Router(bg, layers=["F.Cu"], spokes=spokes)
        ops = r.route("A", "A1.1", "A2.1")
        assert len(r.islands("A")) == 1
        keep = [s for _, _, s, _ in _obs(bg).spokes_near(box(0, 0, 20, 20))]
        return min(LineString([o["start"], o["end"]]).buffer(o["width"] / 2)
                   .distance(s) for o in _tracks(ops, "A") for s in keep)
    assert worst(False) < 0.25
    assert worst(True) >= 0.25 - 1e-6


def test_spoke_count_as_drc_counts_it(tmp_path, mf_path):
    """zones/zc.cpp: spokes into fill that meets other copper of the net,
    then manual tracks from the pad's ring to fill; only when that gives
    none, the spokes into an island the pad alone meets."""
    import make_fixture as mf
    alone = _obs(_thermal(tmp_path))
    assert alone.spoke_count(0) == 2 and alone.short_spokes(["G1"]) == []
    via = mf.via(15, 10, "GND", "00000000-0000-0000-0000-0000000005b1")
    one = _obs(_thermal(tmp_path, via))       # the right half is tied
    assert one.spoke_count(0) == 1
    assert one.short_spokes(["G1"]) == [("G1", "1", "F.Cu", 1)]
    assert one.short_spokes(["A1"]) == []
    hand = mf.seg(10, 10, 11.5, 9.4, 0.25, "F.Cu", "GND",
                  "00000000-0000-0000-0000-0000000005c1")
    two = _obs(_thermal(tmp_path, via + hand))
    assert two.spoke_count(0) == 2 and two.short_spokes(["G1"]) == []
    # a GND track that only passes near the pad is no spoke
    stray = mf.seg(12, 9.5, 13, 9.5, 0.25, "F.Cu", "GND",
                   "00000000-0000-0000-0000-0000000005c2")
    three = _obs(_thermal(tmp_path, via + stray))
    assert three.spoke_count(0) == 1


def test_zone_join():
    z = geom.Zone(net="GND", layers=("F.Cu",), fills={}, zone_id=0,
                  connect=geom.ZONE_CONNECT_THT_THERMAL)
    assert rf.zone_join(_pad(kind="thru_hole"), z) == geom.ZONE_CONNECT_THERMAL
    assert rf.zone_join(_pad(kind="smd"), z) == geom.ZONE_CONNECT_SOLID
    z.connect = geom.ZONE_CONNECT_THERMAL
    assert rf.zone_join(_pad(zone_connect=geom.ZONE_CONNECT_SOLID), z) \
        == geom.ZONE_CONNECT_SOLID
    assert rf.zone_join(_pad(zone_connect=geom.ZONE_CONNECT_NONE), z) \
        == geom.ZONE_CONNECT_NONE
    assert rf.zone_join(_pad(), z) == geom.ZONE_CONNECT_THERMAL


def test_geom_reads_zone_connections(tmp_path, mf_path):
    """A pad's own (zone_connect)/(thermal_*) win over its footprint's; a
    zone's (connect_pads yes|no|thru_hole_only) and (fill (thermal_gap)
    (thermal_bridge_width)) and (min_thickness)."""
    import make_fixture as mf
    fp = ('  (footprint "t:U1" (layer "F.Cu") (at 5 5)\n'
          '    (property "Reference" "U1" (at 0 0 0))\n'
          '    (zone_connect 0) (thermal_gap 0.4)\n'
          '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")'
          ' (net "GND"))\n'
          '    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu")'
          ' (net "GND") (zone_connect 2) (thermal_bridge_width 0.3)'
          ' (thermal_bridge_angle 45)))\n')
    zones = ""
    for i, kw in enumerate(("", "yes ", "no ", "thru_hole_only ")):
        zones += (f'  (zone (net "GND") (layer "F.Cu") (uuid "00000000-0000-'
                  f'0000-0000-0000000006a{i}") (connect_pads {kw}(clearance'
                  ' 0.2)) (min_thickness 0.2) (fill yes (thermal_gap 0.3)'
                  ' (thermal_bridge_width 0.35)) (polygon (pts (xy 1 1)'
                  ' (xy 9 1) (xy 9 9) (xy 1 9))))\n')
    bg = geom.BoardGeom.from_file(mf.write(tmp_path / "zc.kicad_pcb",
                                           fp + zones, 10, 10))
    p1, p2 = sorted(bg.pads_of(ref="U1"), key=lambda p: p.number)
    assert (p1.zone_connect, p1.thermal_gap, p1.spoke_width) == (0, 0.4, None)
    assert (p2.zone_connect, p2.thermal_gap, p2.spoke_width,
            p2.spoke_angle) == (2, 0.4, 0.3, 45)
    assert [z.connect for z in bg.zones_of()] == [
        geom.ZONE_CONNECT_THERMAL, geom.ZONE_CONNECT_SOLID,
        geom.ZONE_CONNECT_NONE, geom.ZONE_CONNECT_THT_THERMAL]
    z = bg.zones_of()[0]
    assert (z.thermal_gap, z.spoke_width, z.min_thickness) == (0.3, 0.35, 0.2)
    # a pad with nothing of its own and no footprint setting: the zone's
    [g] = _thermal(tmp_path).pads_of(ref="G1")
    assert (g.zone_connect, g.thermal_gap, g.spoke_width) == (-1, None, None)


# ------------------------------------------------------------ live (KiCad)

@pytest.fixture
def kicad():
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    return cli


def _signal_drc(cli, pcb) -> list:
    """DRC findings on the signal nets (GND is bare fence and walls) and
    none of the fixture's stand-in footprint library warnings."""
    import kc
    return [v for v in kc.run_drc(cli, pcb)["violations"]
            if v.get("net") != "GND" and v["check"] != "lib_footprint_issues"]


def test_live_route_group_drc_clean(kicad, tmp_path):
    pcb = _board(tmp_path, "group")
    assert len(_signal_drc(kicad, pcb)) == 3       # A, B, C unconnected
    payload, _ = route_edit.run([
        "--pcb", str(pcb), "--op",
        json.dumps({"op": "route_group", "jobs": ABC, "layers": ["F.Cu"]})])
    assert payload["results"][0]["status"] == "added"
    assert _signal_drc(kicad, pcb) == []


def test_live_reroute_drc_clean(kicad, tmp_path):
    pcb = _board(tmp_path, "reroute")
    payload, _ = route_edit.run([
        "--pcb", str(pcb), "--op",
        json.dumps({"op": "reroute", "net": "A", "region": [14, 14, 16, 16],
                    "from": "A1.1", "to": "A2.1"})])
    assert payload["results"][0]["status"] == "rerouted"
    assert B_TRACK not in pcb.read_text(encoding="utf-8")
    assert _signal_drc(kicad, pcb) == []


def test_live_route_and_corridor_drc_clean(kicad, tmp_path):
    pcb = _board(tmp_path, "corridor")
    route_edit.run(["--pcb", str(pcb), "--op", json.dumps(
        {"op": "route", "net": "A", "from": "P1.1", "to": "P2.1"})])
    assert _signal_drc(kicad, pcb) == []

    (tmp_path / "z").mkdir()
    pcb = _board(tmp_path / "z", "corridor")
    p, _ = zone_patch.run(["--pcb", str(pcb), "--net", "A", "--from", "P1.1",
                           "--to", "P2.1", "--half-width", "0.5"])
    assert p["status"] == "pass" and p["fill_mm2"] > 10
    assert _signal_drc(kicad, pcb) == []
