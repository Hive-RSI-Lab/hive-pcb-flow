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
from types import SimpleNamespace

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


_B_SIZED = {
    "width": """
(rule "aiee_diff_w_B_F"
\t(layer "F.Cu")
\t(constraint track_width (min 0.15mm) (opt 0.17mm) (max 0.19mm))
\t(condition "A.NetName == 'B' && !(A.intersectsCourtyard('B1'))")
)
""",
    "gap": """
(rule "aiee_diff_gap_B"
\t(constraint diff_pair_gap (min 0.18mm) (opt 0.2mm))
\t(condition "A.NetName == 'b*'")
)
""",
    "iso": """
(rule "aiee_iso_B_A"
\t(constraint track_width (min 0.15mm) (max 0.19mm))
\t(condition "A.NetName == 'B' && B.NetName == 'A'")
)
""",
}


@pytest.mark.parametrize("rule", sorted(_B_SIZED))
def test_reroute_never_rips_a_net_a_rule_sizes(tmp_path, rule):
    """#1140: a part move's push ripped the USB pairs, whose impedance
    rules carry an (opt ..) and a (max ..) and a courtyard clause the
    floor reader skips, and put them back at 0.2 mm, alone: 43 track
    widths past the rule's 0.1778 max. A width rule of any shape, or a
    pair's gap rule (KiCad's wildcard, any case), keeps the net now; a
    barrier rule (aiee_iso_*) sizes nothing."""
    pcb = _board(tmp_path, "reroute")
    dru = pcb.with_suffix(".kicad_dru")
    dru.write_text(dru.read_text() + _B_SIZED[rule])
    r = mazeroute.Router(geom.BoardGeom.from_file(pcb))
    assert "B" not in r.rules.net_width
    if rule == "iso":
        assert "B" not in r.keep_nets
        removed, _, _ = r.reroute("A", [14, 14, 16, 16], "A1.1", "A2.1")
        assert removed == [B_TRACK]
        return
    assert r.rules.sized_nets == {"B"} and "B" in r.keep_nets
    with pytest.raises(CheckError):
        r.reroute("A", [14, 14, 16, 16], "A1.1", "A2.1")


def _zone(net, pts, uid, teardrop):
    attr = " (attr (teardrop (type padvia)))" if teardrop else ""
    xy = " ".join(f"(xy {x} {y})" for x, y in pts)
    return (f'  (zone (net "{net}") (net_name "{net}") (layer "F.Cu")'
            f' (uuid "{uid}"){attr}\n    (polygon (pts {xy}))\n'
            f'    (filled_polygon (layer "F.Cu") (pts {xy})))\n')


@pytest.mark.parametrize("teardrop", [True, False])
def test_a_teardrop_is_no_pour_so_reroute_still_rips_its_net(tmp_path,
                                                            teardrop):
    """#1140: KiCad builds a teardrop zone per teardropped track end, and
    the routers took every zone's net for a pour that is never ripped, so
    on a teardropped board almost no signal net could be pushed aside. A
    real pour of B still keeps B."""
    pcb = _board(tmp_path, "reroute")
    text = pcb.read_text()
    zone = _zone("B", [(14.7, 12.4), (15.3, 12.4), (15.1, 12.9),
                       (14.9, 12.9)], "zB", teardrop)
    pcb.write_text(text[:text.rstrip().rfind(")")] + zone + ")\n")
    bg = geom.BoardGeom.from_file(pcb)
    assert [z.teardrop for z in bg.zones_of()] == [teardrop]
    assert ("B" in rf.pour_nets(bg)) is not teardrop
    r = mazeroute.Router(bg)
    assert ("B" in r.keep_nets) is not teardrop
    if teardrop:
        removed, ops, _ = r.reroute("A", [14, 14, 16, 16], "A1.1", "A2.1")
        assert removed == [B_TRACK]
        assert len(r.islands("A")) == 1 and len(r.islands("B")) == 1
    else:
        with pytest.raises(CheckError, match="nothing in"):
            r.reroute("A", [14, 14, 16, 16], "A1.1", "A2.1")


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


# ------------------------------------------------------------ joins (#1140)

def _long(tmp_path, wall=False):
    """100 x 10, two layers: A's pads A1 (2, 4) and A2 (98, 4) joined by a
    0.25 mm track on F.Cu, and A3 at (50, 8) on its own, 3.4 mm above the
    track. wall: a 1 mm GND wall at y=6.5 from x=20 to x=80 on both layers,
    so A3's way to the track goes round an end of the wall, 30 mm off."""
    import make_fixture as mf
    body = (mf.pad("A1", 2, 4, "A") + mf.pad("A2", 98, 4, "A")
            + mf.pad("A3", 50, 8, "A")
            + mf.seg(2, 4, 98, 4, 0.25, "F.Cu", "A",
                     "00000000-0000-0000-0000-0000000006a1"))
    if wall:
        for n, layer in enumerate(("F.Cu", "B.Cu")):
            body += mf.seg(20, 6.5, 80, 6.5, 1.0, layer, "GND",
                           f"00000000-0000-0000-0000-0000000006b{n}")
    return geom.BoardGeom.from_file(mf.write(tmp_path / "long.kicad_pcb",
                                             body, 100, 10))


def _windows(monkeypatch):
    """The windows Router.grid is asked for, in order."""
    wins: list = []
    real = mazeroute.Router.grid

    def grid(self, net, a, b, win, width=None):
        wins.append(win)
        return real(self, net, a, b, win, width)
    monkeypatch.setattr(mazeroute.Router, "grid", grid)
    return wins


def test_join_islands_tries_a_window_round_the_small_island_first(
        tmp_path, mf_path, monkeypatch):
    """A net that spans the board joined a pad to it on a grid of the
    whole board, every try; the first try is now a window round the pad,
    the rest of the net cut to it."""
    r = mazeroute.Router(_long(tmp_path), grid=0.1)
    assert [i[None] for i in r.islands("A")] in ([2, 1], [1, 2])
    wins = _windows(monkeypatch)
    ops = r.join_islands("A", 1)
    assert _tracks(ops, "A") and len(r.islands("A")) == 1
    assert len(wins) == 1
    x0, _, x1, _ = wins[0]
    assert x1 - x0 < 25 and x0 < 50 < x1


def test_join_islands_falls_back_to_the_whole_net(tmp_path, mf_path,
                                                  monkeypatch):
    """When the window round the small island holds no path (the wall),
    the join tries the whole net as before and still joins."""
    r = mazeroute.Router(_long(tmp_path, wall=True), grid=0.1)
    wins = _windows(monkeypatch)
    ops = r.join_islands("A", 1)
    assert _tracks(ops, "A") and len(r.islands("A")) == 1
    assert wins[0][2] - wins[0][0] < 25
    assert wins[-1][0] < 2 and wins[-1][2] > 98
    xs = [x for o in _tracks(ops, "A") for x in (o["start"][0],
                                                 o["end"][0])]
    assert min(xs) < 20 or max(xs) > 80        # round an end of the wall


def _scrap(tmp_path, stub=False):
    """20 x 20, two layers: GND's pad G1 at (5, 10) in a GND zone on F.Cu
    filled in two pieces, x 2 to 8 round G1 and x 14 to 18 with no pad, as
    a move that cuts fill off its pads leaves it. stub: a GND track on the
    second piece, as a rip leaves one."""
    import make_fixture as mf
    pts = lambda x0, x1: " ".join(f"(xy {x} {y})" for x, y in (
        (x0, 7), (x1, 7), (x1, 13), (x0, 13)))
    body = (mf.pad("G1", 5, 10, "GND")
            + '  (zone (net "GND") (net_name "GND") (layer "F.Cu") (uuid'
            ' "00000000-0000-0000-0000-0000000007a1")\n'
            f'    (polygon (pts {pts(2, 18)}))\n'
            f'    (filled_polygon (layer "F.Cu") (pts {pts(2, 8)}))\n'
            f'    (filled_polygon (layer "F.Cu") (pts {pts(14, 18)})))\n')
    if stub:
        body += mf.seg(15, 10, 17, 10, 0.25, "F.Cu", "GND",
                       "00000000-0000-0000-0000-0000000007b1")
    return geom.BoardGeom.from_file(mf.write(tmp_path / "scrap.kicad_pcb",
                                             body, 20, 20))


def test_a_scrap_of_fill_is_no_island_to_join(tmp_path, mf_path):
    """Fill a move cut off from its pads is the refill's to redo (or KiCad's
    island removal's to drop): join_islands left it alone no more than a
    stub, and routed to it, and part_repair's island counts saw a net cut
    in two."""
    r = mazeroute.Router(_scrap(tmp_path))
    assert sorted(i[None] for i in r.islands("GND")) == [0, 1]
    assert [i[None] for i in r.islands("GND", scraps=False)] == [1]
    assert r.join_islands("GND", 1) == []


def test_a_stub_on_a_scrap_still_joins(tmp_path, mf_path):
    """A track left on the scrap is copper a rip left: it still counts, and
    the join routes to it."""
    r = mazeroute.Router(_scrap(tmp_path, stub=True))
    assert sorted(i[None] for i in r.islands("GND", scraps=False)) == [0, 1]
    ops = r.join_islands("GND", 1)
    assert _tracks(ops, "GND") and len(r.islands("GND")) == 1


# V's B.Cu teardrop: from inside its disk, left of the track's end, to the
# track's width at x = 11
_FLARE = [(9.6, 4.8), (10.2, 4.55), (11, 4.875), (11, 5.125), (10.2, 5.45),
          (9.6, 5.2)]


def _via_on_b(tmp_path, teardrop=False):
    """20 x 10, two layers: A's via V at (10, 5), 1 mm across, met on B.Cu
    alone, by a 0.25 mm track from its centre to (16, 5), as a rip of the
    track that met it on F.Cu leaves it; and a stub of A on B.Cu from
    (2, 5) to (4, 5). teardrop: V's B.Cu teardrop too (_FLARE)."""
    import make_fixture as mf
    body = (mf.via(10, 5, "A", "00000000-0000-0000-0000-0000000007c1",
                   size=1.0, drill=0.4)
            + mf.seg(10, 5, 16, 5, 0.25, "B.Cu", "A",
                     "00000000-0000-0000-0000-0000000007c2")
            + mf.seg(2, 5, 4, 5, 0.25, "B.Cu", "A",
                     "00000000-0000-0000-0000-0000000007c3"))
    if teardrop:
        xy = " ".join(f"(xy {x} {y})" for x, y in _FLARE)
        body += ('  (zone (net "A") (net_name "A") (layer "B.Cu")'
                 ' (uuid "00000000-0000-0000-0000-0000000007c4")'
                 ' (attr (teardrop (type padvia)))\n'
                 f'    (polygon (pts {xy}))\n'
                 f'    (filled_polygon (layer "B.Cu") (pts {xy})))\n')
    return geom.BoardGeom.from_file(mf.write(tmp_path / "via1.kicad_pcb",
                                             body, 20, 10))


def test_a_join_ends_past_a_via_met_on_one_layer(tmp_path, mf_path):
    """#1140, the R2A flip on pd_trigger: a rip left a via met on B.Cu
    alone, and the join from the flipped pad ended on the via's disk there,
    short of the track that met it. The via stayed met on one layer, which
    KiCad 10 calls dangling, and the prune could not take it with the route
    hanging on it. Its disk is no end on B.Cu now (still one on F.Cu, where
    a route would meet it on a second layer): the join reaches the track,
    and the via is the prune's to take."""
    r = mazeroute.Router(_via_on_b(tmp_path))
    disk = Point(9.6, 5)        # in V's disk, short of the track's copper
    whole = [i for i in r.islands("A") if "F.Cu" in i]
    ends = [i for i in r.islands("A", ends=True) if "F.Cu" in i]
    assert len(whole) == len(ends) == 1
    assert any(p.covers(disk) for p in whole[0]["B.Cu"])
    assert not any(p.covers(disk) for p in ends[0]["B.Cu"])
    assert any(p.covers(disk) for p in ends[0]["F.Cu"])
    ops = r.join_islands("A", 1)
    assert len(r.islands("A")) == 1
    track = LineString([(10, 5), (16, 5)]).buffer(0.125)
    assert any(LineString([o["start"], o["end"]]).buffer(o["width"] / 2)
               .intersects(track) for o in _tracks(ops, "A")
               if o["layer"] == "B.Cu")


def test_a_join_ends_on_the_track_not_in_the_via_teardrop(tmp_path, mf_path):
    """#1140, the same flip: with V's disk no end on B.Cu, the join ended
    in V's B.Cu teardrop instead, inside the disk and 0.26 mm off the
    track's centre line, so it held the via again. A teardrop is no end (a
    refill makes it again), though it still joins its island: the join's
    centre line reaches the track's copper."""
    bg = _via_on_b(tmp_path, teardrop=True)
    assert [z.teardrop for z in bg.zones_of()] == [True]
    r = mazeroute.Router(bg)
    flare = [Point(9.65, 5), Point(10.3, 4.72)]   # in the teardrop and in
    #                                               V's disk, off the track
    track = LineString([(10, 5), (16, 5)]).buffer(0.125)
    assert not any(track.intersects(p) for p in flare)
    assert all(Point(10, 5).distance(p) < 0.5 for p in flare)
    whole = [i for i in r.islands("A") if "F.Cu" in i]
    ends = [i for i in r.islands("A", ends=True) if "F.Cu" in i]
    assert len(r.islands("A")) == len(r.islands("A", ends=True)) == 2
    assert len(whole) == len(ends) == 1
    for p in flare:
        assert any(q.covers(p) for q in whole[0]["B.Cu"])
        assert not any(q.covers(p) for q in ends[0]["B.Cu"])
    ops = r.join_islands("A", 1)
    assert len(r.islands("A")) == 1
    joins = [o for o in _tracks(ops, "A") if o["layer"] == "B.Cu"]
    assert any(LineString([o["start"], o["end"]]).intersects(track)
               for o in joins)


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


def _thermal(tmp_path, body="", cut=None):
    """20 x 20, F.Cu and B.Cu: G1, a 1 mm GND pad at (10, 10), in a GND
    pour across the board (y 9.2 to 10.8) with a 0.5 mm thermal gap and two
    0.5 mm spokes, left and right, each into its own half of the pour; A's
    pads at (11.4, 15) and (11.4, 5) either side. A's straight way down
    crosses the right spoke's end. `body` adds copper; `cut` (a shape)
    comes out of the fill, as other copper's clearance would."""
    import make_fixture as mf
    band = box(0.5, 9.2, 19.5, 10.8).difference(
        box(9.5, 9.5, 10.5, 10.5).buffer(0.5))
    fill = band.union(box(10.4, 9.75, 11.2, 10.25)).union(
        box(8.8, 9.75, 9.6, 10.25))
    if cut is not None:
        fill = fill.difference(cut)
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
    # mids=True: where each spoke crosses the ring (the pad grown by half
    # the gap), in the same order
    mids = rf.thermal_spokes(p, z.fills["F.Cu"], gap, sw, mt, mids=True)
    assert [(f[0], f[1]) for f in mids] == live
    assert sorted((round(x, 3), round(y, 3)) for _, _, (x, y) in mids) \
        == [(9.25, 10.0), (10.75, 10.0)]
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


def test_a_starved_pad_gets_a_track_on_its_live_spoke(tmp_path, mf_path):
    """#1140 (C210.2 on the voice board): a pad whose pour reaches it by
    one live spoke gets a track of its net from its centre along that
    spoke into the fill, which the spoke model (as DRC) counts as the
    second. The track waives the check, it adds no path, so it is laid
    only on a pad the run did not move whose spokes it did not cut (bg0,
    the board before the run, against bg); each is an entry of waived."""
    import make_fixture as mf
    import part_repair as pr
    via = mf.via(15, 10, "GND", "00000000-0000-0000-0000-0000000005b1")
    bg = _thermal(tmp_path, via)               # the right half is tied
    assert _obs(bg).spoke_count(0) == 1
    zid = "00000000-0000-0000-0000-0000000005a1"
    starved = [("G1", "1", "F.Cu", zid)]
    rules = SimpleNamespace(net_layers=lambda net, lys: list(lys),
                            width_floor=lambda net: 0.101)
    ops, notes, waived = pr.spoke_tracks(bg, bg, starved, rules)
    [op] = ops
    assert (op["op"], op["net"], op["layer"]) == ("add_track", "GND", "F.Cu")
    assert op["start"] == [10.0, 10.0]
    assert op["end"][1] == pytest.approx(10.0)
    assert 11.0 < op["end"][0] < 11.4               # just past the spoke
    assert op["width"] == pytest.approx(0.48)       # in the 0.5 mm spoke
    assert notes == [] and waived == [
        {"pad": "G1.1", "layer": "F.Cu", "zone": zid, "net": "GND",
         "spokes": [1, 1], "track": op}]
    track = mf.seg(*op["start"], *op["end"], op["width"], "F.Cu", "GND",
                   "00000000-0000-0000-0000-0000000005c1")
    assert _obs(_thermal(tmp_path, via + track)).spoke_count(0) == 2
    # the pour held off the pad before the run (no spokes there): a track
    held = _thermal(tmp_path, cut=Point(10, 10).buffer(2.0))
    ops, notes, waived = pr.spoke_tracks(held, bg, starved, rules)
    assert ops == [op] and notes == [] and waived[0]["spokes"] == [0, 1]
    # none for a net the .kicad_dru keeps off the layer, or a pad that is
    # not there: a note each
    banned = SimpleNamespace(net_layers=lambda net, lys: [],
                             width_floor=rules.width_floor)
    ops, notes, waived = pr.spoke_tracks(
        bg, bg, starved + [("G9", "1", "F.Cu", zid)], banned)
    assert ops == [] and waived == []
    assert "keeps GND off F.Cu" in notes[0]
    assert notes[1].startswith("G9.1 on F.Cu starves")
    # both halves tied: no live spoke short, nothing to lay
    left = mf.via(5, 10, "GND", "00000000-0000-0000-0000-0000000005b2")
    both = _thermal(tmp_path, via + left)
    ops, notes, _ = pr.spoke_tracks(both, both, starved, rules)
    assert ops == [] and "the spoke model counts 2" in notes[0]
    # the run cut a spoke (2 before, 1 after): the run starves the pad,
    # and a track would only pass the count
    ops, notes, waived = pr.spoke_tracks(both, bg, starved, rules)
    assert ops == waived == []
    assert "the run cut its spokes from 2 to 1" in notes[0]
    # the run moved the pad, or flipped it (C221): no track either
    pad = mf.pad("G1", 10, 10, "GND")
    for name, body in (("moved", mf.pad("G1", 10, 12, "GND")),
                       ("flipped", pad.replace('"F.Cu"', '"B.Cu"'))):
        bg0 = geom.BoardGeom.from_file(
            mf.write(tmp_path / f"{name}.kicad_pcb", body, 20, 20))
        ops, notes, waived = pr.spoke_tracks(bg0, bg, starved, rules)
        assert ops == waived == [] and "the run moved it" in notes[0], name


def test_a_spoke_track_narrows_past_a_notch(tmp_path, mf_path):
    """#1140 (C210.2 on the voice board): where other copper's clearance
    notches the fill along the live spoke, the track narrows, 0.02 mm a
    try, to the widest that lies in the fill and the pad, down to the
    net's width floor; with none there, no track and a note."""
    import make_fixture as mf
    import part_repair as pr
    via = mf.via(15, 10, "GND", "00000000-0000-0000-0000-0000000005b1")
    # the notch comes down to y 10.193 over the right spoke: a track on
    # its axis fits at 0.38 mm and not at 0.40
    notch = Point(10.9, 10.443).buffer(0.25)
    bg = _thermal(tmp_path, via, cut=notch)
    assert _obs(bg).spoke_count(0) == 1
    zid = "00000000-0000-0000-0000-0000000005a1"
    rules = SimpleNamespace(net_layers=lambda net, lys: list(lys),
                            width_floor=lambda net: 0.101)
    [op], notes, [w] = pr.spoke_tracks(bg, bg, [("G1", "1", "F.Cu", zid)],
                                       rules)
    assert op["width"] == pytest.approx(0.38) and w["track"] is op
    assert 11.0 < op["end"][0] < 11.4 and notes == []
    track = mf.seg(*op["start"], *op["end"], op["width"], "F.Cu", "GND",
                   "00000000-0000-0000-0000-0000000005c1")
    assert _obs(_thermal(tmp_path, via + track, cut=notch)).spoke_count(0) \
        == 2
    # a floor above every width that fits: no track, and the note says so
    thick = SimpleNamespace(net_layers=rules.net_layers,
                            width_floor=lambda net: 0.4)
    ops, notes, waived = pr.spoke_tracks(bg, bg, [("G1", "1", "F.Cu", zid)],
                                         thick)
    assert ops == waived == []
    assert "none fits in the spoke and the fill" in notes[0]


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
