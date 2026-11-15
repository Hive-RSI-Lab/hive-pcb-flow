"""part_repair: move, rotate or re-pad parts on a routed board and mend the
copper round them (rip, place, rejoin, gate).

The fast tests run the pieces on a synthetic part U1 with four pads in a row
and the copper shapes a rip has to tell apart: a chain through a via (net A),
a junction (B), a track that leaves the window (C) and a T onto a track's
middle (D); the gates on made-up DRC results; and join_net's push on a fake
router. The live tests work on copies of the routed pd_trigger fixture: the
gates on real DRC of a dangling stub that moves; R6 re-padded by a renumber
that swaps its two nets (applied), moved, rotated and flipped, each replayed
from its op files on a fresh copy (copper and fills); R6 swapped to a larger
package; a renumber whose joins must push other nets aside; a join that
fails and a board that got worse (nothing written, the board untouched); a
board that changed during the run (not replaced); and an error (reported in
DIR).
"""
from __future__ import annotations

import dataclasses
import json
import shlex
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import shapely
from shapely.geometry import Point, box

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import env  # noqa: E402
import geom  # noqa: E402
import mazeroute  # noqa: E402
import part_repair as pr  # noqa: E402
from checklib import CheckError  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "stages" / "pd_trigger"
ROUTE = FIX / "route"
R0805 = FIX / "lib" / "aiee.pretty" / "R0805.kicad_mod"
WIN = box(-5, -3, 6, 3)


def _seg(a, b, net, uid, layer="F.Cu", width=0.25):
    return (f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width '
            f'{width}) (layer "{layer}") (net "{net}") (uuid "{uid}"))\n')


def _board(tmp_path, body: str, name="b") -> Path:
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text('(kicad_pcb (version 20240108) (generator "t") '
                 '(general (thickness 1.6))\n(layers (0 "F.Cu" signal) '
                 '(31 "B.Cu" signal) (44 "Edge.Cuts" user))\n' + body + ")\n",
                 encoding="utf-8")
    return p


U1 = ('(footprint "T:U4" (layer "F.Cu") (at 0 0) (uuid "fU1")'
      ' (property "Reference" "U1")\n'
      + "".join(f' (pad "{n}" smd rect (at {x} 0) (size 1 1) (layers "F.Cu")'
                f' (net "{net}"))\n'
                for n, x, net in (("1", -2, "A"), ("2", 0, "B"),
                                  ("3", 2, "C"), ("4", 4, "D")))
      + ")\n")
COPPER = (
    # A: pad 1 -> a0 -> a1 -> via vA -> a2 -> a3 (a3 leaves the window)
    _seg((-2, 0), (-2, -2), "A", "a0") + _seg((-2, -2), (-3, -2), "A", "a1")
    + '(via (at -3 -2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
      ' (net "A") (uuid "vA"))\n'
    + _seg((-3, -2), (-4, -2), "A", "a2", "B.Cu")
    + _seg((-4, -2), (-6, -2), "A", "a3", "B.Cu")
    # B: pad 2 -> b0, which forks into b1 and b2
    + _seg((0, 0), (0, 2), "B", "b0") + _seg((0, 2), (0.6, 2), "B", "b1")
    + _seg((0, 2), (-0.6, 2), "B", "b2")
    # C: pad 3 -> c0 -> c1, which runs out of the window
    + _seg((2, 0), (2, 2.5), "C", "c0") + _seg((2, 2.5), (2, 4), "C", "c1")
    # D: pad 4 -> d0, which ends in the middle of d1
    + _seg((4, 0), (4, -2), "D", "d0") + _seg((3, -2), (5, -2), "D", "d1"))


@pytest.fixture
def u1(tmp_path):
    pcb = _board(tmp_path, U1 + COPPER)
    return pcb, geom.BoardGeom.from_file(pcb)


def _item(bg, uid):
    return next(it for it in bg.tracks_of() + bg.vias_of() if it.uuid == uid)


def _ripped(bg, uid, keep=()):
    rip = pr.Rip(bg, WIN, keep)
    rip.add(_item(bg, uid), "test")
    return set(rip.items)


def test_rip_follows_a_chain_to_the_first_stop(u1):
    _, bg = u1
    assert _ripped(bg, "a0") == {"a0", "a1"}      # stops at the via
    assert _ripped(bg, "b0") == {"b0"}            # a junction
    assert _ripped(bg, "c0") == {"c0"}            # c1 leaves the window
    assert _ripped(bg, "d0") == {"d0"}            # a T onto d1's middle
    # a via takes its tracks in the window on both layers, and their chains
    assert _ripped(bg, "vA") == {"vA", "a1", "a0", "a2"}


def test_rip_refuses_keep_nets_and_uuidless_copper(u1, tmp_path):
    _, bg = u1
    with pytest.raises(CheckError, match="A is --keep"):
        _ripped(bg, "a0", keep={"A"})
    pcb = _board(tmp_path, U1 + '(segment (start -2 0) (end -2 -2) (width'
                 ' 0.25) (layer "F.Cu") (net "A"))\n', name="nouuid")
    bg2 = geom.BoardGeom.from_file(pcb)
    with pytest.raises(CheckError, match="has no uuid"):
        pr.Rip(bg2, WIN, ()).add(bg2.tracks_of()[0], "test")


def test_seeds_are_copper_left_on_the_wrong_pads(u1):
    _, bg = u1
    old = {"U1": bg.pads_of(ref="U1")}
    assert pr.seeds(bg, old, old) == []
    turn = {"A": "B", "B": "C", "C": "D", "D": "A"}
    new = {"U1": [dataclasses.replace(p, net=turn[p.net]) for p in old["U1"]]}
    assert {it.uuid for it in pr.seeds(bg, old, new)} \
        == {"a0", "b0", "c0", "d0"}


def test_windows_cover_old_and_new_pads():
    old = {"U1": [SimpleNamespace(poly=box(0, 0, 1, 1))], "U2": []}
    new = {"U1": [SimpleNamespace(poly=box(5, 0, 6, 1))]}
    wins = pr.windows(old, new, 2)
    assert set(wins) == {"U1"}
    assert wins["U1"].bounds == (-2, -2, 8, 3)


def _v(source, check, net=None, severity="error"):
    return {"source": source, "check": check, "severity": severity,
            "net": net}


def test_gates_name_what_got_worse():
    clr = _v("drc", "clearance")
    unc = _v("unconnected", "unconnected_items", "A")
    drc0 = {"violations": [clr, unc]}
    assert pr.gates(drc0, drc0, {"A": [1, 1]}) == []
    assert pr.gates(drc0, {"violations": []}, {"A": [2, 1]}) == []
    assert pr.gates(drc0, drc0, {"A": [1, 1], "B": [1, 2]}) \
        == ["B: 1 -> 2 islands"]
    assert pr.gates(drc0, {"violations": [clr, clr, unc]}, {}) \
        == ["DRC drc:clearance: 1 -> 2"]
    moved = _v("unconnected", "unconnected_items", "B")
    assert pr.gates(drc0, {"violations": [clr, moved]}, {}) \
        == ["unconnected B: 0 -> 1"]
    assert pr.gates(drc0, {"violations": [clr, unc, moved]}, {}) \
        == ["unconnected B: 0 -> 1", "unconnected: 1 -> 2"]


def _vat(source, check, net, *pts, severity="error"):
    items = [{"msg": f"item {i}", "pos": list(pt), "uuid": f"u{i}-{pt[0]}"}
             for i, pt in enumerate(pts)]
    return {"source": source, "check": check, "severity": severity,
            "net": net, "layer": "F.Cu", "msg": f"{check} here",
            "pos": items[0]["pos"] if items else None, "items": items}


def test_new_violations_name_what_the_gate_counts():
    old = _vat("drc", "track_dangling", "A", (1, 1))
    clr = _vat("drc", "clearance", "A", (2, 2), (2, 3))
    unc = _vat("unconnected", "unconnected_items", "A", (0, 0), (0, 1))
    drc0 = {"violations": [old, clr, unc]}
    # the same violations, one moved by less than 1e-3: none is new
    nudged = _vat("drc", "track_dangling", "A", (1.0004, 1))
    assert pr.new_violations(drc0, {"violations": [nudged, clr, unc]}) == []
    # a check whose count did not grow is not listed, though one moved
    moved = _vat("drc", "clearance", "A", (2, 2.5), (2, 3))
    assert pr.new_violations(drc0, {"violations": [old, moved, unc]}) == []
    new = _vat("drc", "track_dangling", "B", (5, 5))
    warn = _vat("drc", "silk_overlap", None, (3, 3), severity="warning")
    excl = _vat("drc", "clearance", "C", (9, 9), severity="exclusion")
    unc_b = _vat("unconnected", "unconnected_items", "B", (4, 4), (4, 5))
    drc1 = {"violations": [old, new, clr, warn, excl, unc, unc_b]}
    got = pr.new_violations(drc0, drc1)
    assert [(v["key"], v["net"]) for v in got] == [
        ("drc:track_dangling", "B"), ("drc:silk_overlap", None),
        ("unconnected", "B")]
    assert got[0]["items"] == [{"msg": "item 0", "pos": [5, 5],
                                "uuid": "u0-5"}]
    # every line the gate writes has its violations here
    assert pr.gates(drc0, drc1, {}) == [
        "DRC drc:silk_overlap: 0 -> 1", "DRC drc:track_dangling: 1 -> 2",
        "unconnected B: 0 -> 1", "unconnected: 1 -> 2"]

    # REPORT.md lists them: where, net, the items' uuids, and a cut
    p = {"status": "violations", "board": "b.kicad_pcb", "refs": ["U1"],
         "ops": 1, "applied": False, "worse": pr.gates(drc0, drc1, {}),
         "new_violations": got}
    md = pr.report_md(p)
    assert "- drc:track_dangling at (5.000, 5.000) on F.Cu, net B:" \
        " track_dangling here (items u0-5)" in md
    assert md.index("Worse than before") < md.index("New violations")
    p["new_violations"] = got[:1] * (pr.SHOWN + 2)
    md = pr.report_md(p)
    assert md.count("- drc:track_dangling at") == pr.SHOWN
    assert "- and 2 more in part_repair.json" in md
    # KiCad puts a starved_thermal marker at the zone's position, which may
    # be far from the pad that starves: the line names the pad where it is
    st = _vat("drc", "starved_thermal", "GND", (4.5, 9.5), (34.025, 51.5))
    st["items"][1]["msg"] = "Pad 2 [GND] of C221 on B.Cu"
    p["new_violations"] = pr.new_violations(drc0, {"violations": [st]})
    assert "- drc:starved_thermal at (4.500, 9.500) on F.Cu, net GND:" \
        " starved_thermal here (items u0-4.5, u1-34.025); Pad 2 [GND] of" \
        " C221 on B.Cu at (34.025, 51.500)" in pr.report_md(p)


def test_dangling_prunes_only_free_copper_in_the_window(u1):
    pcb, bg = u1
    nets = {"B", "C"}
    assert set(pr._dangling(pcb, bg, nets, win=WIN)) == {"b0", "b1", "b2"}
    # with no window, c1 (outside it) may go, and c0 behind it
    assert set(pr._dangling(pcb, bg, nets)) \
        == {"b0", "b1", "b2", "c0", "c1"}
    # an anchor stays and holds what leads to it
    assert set(pr._dangling(pcb, bg, nets, win=WIN, anchors={"b2"})) == {"b1"}
    # so does copper that meets a new route
    add = {"op": "add_track", "net": "B", "layer": "F.Cu", "width": 0.25,
           "start": [-0.3, 1.5], "end": [-0.3, 2.5]}
    assert set(pr._dangling(pcb, bg, nets, win=WIN, adds=[add])) == {"b1"}
    # copper that is gone leaves what it held free
    assert set(pr._dangling(pcb, bg, nets, win=WIN, gone={"c1"})) \
        == {"b0", "b1", "b2", "c0"}


def _add(net, a, b):
    return {"op": "add_track", "net": net, "layer": "F.Cu", "width": 0.25,
            "start": list(a), "end": list(b)}


def test_dangling_prunes_the_stubs_behind_a_new_junction(u1):
    """A route that lands where b0 forks meets b1 and b2 at that end only,
    and b0 holds it there to pad 2: the two stubs go, b0 stays."""
    pcb, bg = u1
    add = _add("B", (0, 2), (0, 2.8))
    assert set(pr._dangling(pcb, bg, {"B"}, win=WIN, adds=[add])) \
        == {"b1", "b2"}


def test_dangling_keeps_the_copper_a_new_route_hangs_on(u1, tmp_path):
    """A route that lands on b1's free end alone hangs on b1, so b1 and b0
    stay. One that lands where d1 ends at a dead stub d2 hangs on d1 (d0's
    T onto it leads to pad 4): once d2 would go too, nothing at that end
    holds the route, so d1 and d0 stay (and d2 with them)."""
    pcb, bg = u1
    add = _add("B", (0.6, 2), (0.6, 2.8))
    assert set(pr._dangling(pcb, bg, {"B"}, win=WIN, adds=[add])) == {"b2"}
    pcb2 = _board(tmp_path, U1 + COPPER
                  + _seg((5, -2), (5.5, -2.5), "D", "d2"), name="d2")
    bg2 = geom.BoardGeom.from_file(pcb2)
    assert set(pr._dangling(pcb2, bg2, {"D"}, win=WIN)) \
        == {"d0", "d1", "d2"}
    add = _add("D", (5, -2), (5, -2.8))
    assert pr._dangling(pcb2, bg2, {"D"}, win=WIN, adds=[add]) == []


PATCH_A = ('(zone (net "A") (net_name "A") (layer "F.Cu") (uuid "zA")'
           ' (name "PATCH_A")\n (polygon (pts (xy -3 -1) (xy -1 -1) (xy -1 1)'
           ' (xy -3 1)))\n (filled_polygon (layer "F.Cu") (pts (xy -3 -1)'
           ' (xy -1 -1) (xy -1 1) (xy -3 1))))\n')


def test_patch_notes_name_a_patch_the_move_leaves_behind(tmp_path):
    bg = geom.BoardGeom.from_file(_board(tmp_path, U1 + PATCH_A))
    old = {"U1": bg.pads_of(ref="U1")}
    assert pr.patch_notes(bg, old, old) == []
    away = {"U1": [dataclasses.replace(p, center=(p.center[0] + 10,
                                                  p.center[1]))
                   for p in old["U1"]]}
    notes = pr.patch_notes(bg, old, away)
    assert len(notes) == 1
    assert notes[0].startswith("zone PATCH_A on F.Cu met U1.1")
    assert "zone_patch --net A --layer F.Cu" in notes[0]


def test_load_ops_takes_footprint_ops_only(tmp_path, monkeypatch):
    with pytest.raises(CheckError, match="footprint ops only"):
        pr.load_ops(None, [json.dumps({"op": "set_dnp", "ref": "R6",
                                       "dnp": True})])
    monkeypatch.chdir(tmp_path)
    ops = pr.load_ops(None, [json.dumps(
        {"op": "swap_fp", "ref": "R6", "footprint": "lib/R.kicad_mod"})])
    assert ops[0]["footprint"] == str((tmp_path / "lib" / "R.kicad_mod")
                                      .resolve())


def test_run_refuses_bad_arguments(tmp_path):
    pcb = _board(tmp_path, U1)
    op = json.dumps({"op": "rotate", "ref": "U1", "deg": 90})
    with pytest.raises(CheckError, match="--margin must be more than 0"):
        pr.run(["--pcb", str(pcb), "--op", op, "--out", str(tmp_path / "o"),
                "--margin", "0"])
    for out in (tmp_path, tmp_path.parent):
        with pytest.raises(CheckError, match="directory of its own"):
            pr.run(["--pcb", str(pcb), "--op", op, "--out", str(out)])
    with pytest.raises(CheckError, match="board not found"):
        pr.run(["--pcb", str(tmp_path / "none.kicad_pcb"), "--op", op,
                "--out", str(tmp_path / "o")])


def test_gates_fail_on_a_new_violation_near_the_repair():
    """A repair that clears one violation and makes another of the same
    check leaves every count as it was: the gate fails on the new one when
    it, or one of its items, lies in the repair area."""
    old = _vat("drc", "track_dangling", "A", (10, 10))
    new = _vat("drc", "track_dangling", "B", (1, 1))
    drc0, drc1 = {"violations": [old]}, {"violations": [new]}
    near = box(0, 0, 2, 2)
    line = "DRC drc:track_dangling: 1 new near the repair (none like it before)"
    assert pr.gates(drc0, drc1, {}) == []            # no area: counts only
    assert pr.gates(drc0, drc1, {}, near) == [line]
    assert pr.gates(drc0, drc1, {}, box(5, 5, 6, 6)) == []
    assert [(v["key"], v["net"]) for v in pr.new_violations(
        drc0, drc1, near)] == [("drc:track_dangling", "B")]
    # one item in the area is enough, wherever the violation itself is
    far = _vat("drc", "track_dangling", "B", (8, 8), (1, 1))
    assert pr.gates(drc0, {"violations": [far]}, {}, near) == [line]
    # the base one's items, moved by the refill (same uuids): not new; one
    # nearby with other items is
    at_old = box(9, 9, 11, 11)
    moved = _vat("drc", "track_dangling", "A", (10, 10.5))
    assert pr.gates(drc0, {"violations": [moved]}, {}, at_old) == []
    other = _vat("drc", "track_dangling", "A", (10.5, 10))
    assert pr.gates(drc0, {"violations": [other]}, {}, at_old) == [line]
    # unconnected items (their own lines) and exclusions are not counted
    unc0 = _vat("unconnected", "unconnected_items", "A", (10, 10), (10, 11))
    unc1 = _vat("unconnected", "unconnected_items", "A", (1, 1), (1, 1.5))
    excl = _vat("drc", "clearance", "B", (1, 1), severity="exclusion")
    assert pr.gates({"violations": [old, unc0]},
                    {"violations": [old, unc1, excl]}, {}, near) == []


def test_repair_area_is_the_windows_and_round_the_changed_copper():
    via = {"op": "add_via", "net": "A", "at": [20, 0], "size": 0.6,
           "drill": 0.3}
    gone = SimpleNamespace(poly=box(10, 10, 11, 11))
    area = pr.repair_area(box(0, 0, 1, 1), [_add("A", (5, 0), (6, 0)), via],
                          [gone])
    assert all(area.contains(Point(p)) for p in
               ((0.5, 0.5), (5.5, 1.1), (20, 1.25), (11.9, 10.5)))
    assert not any(area.intersects(Point(p)) for p in
                   ((1.5, 0.5), (5.5, 1.2), (20, 1.35), (12.1, 10.5)))


def test_touch_shorts_name_other_net_copper_on_the_pads(u1, tmp_path):
    """Other-net copper on a part's pads would join the nets when KiCad
    saves: each piece is named, unless it touched them on the base board
    too (_shorted)."""
    _, bg = u1
    assert pr.touch_shorts(bg, ["U1"]) == []
    pcb = _board(tmp_path, U1 + COPPER + _seg((-2.3, 0), (-2.3, 1.5), "C", "x1")
                 + '(via (at 0 0.3) (size 0.6) (drill 0.3) (layers "F.Cu"'
                   ' "B.Cu") (net "D") (uuid "xv"))\n', name="x")
    bg = geom.BoardGeom.from_file(pcb)
    assert pr.touch_shorts(bg, ["U1"]) == ["U1.1 (A) <- C track x1",
                                           "U1.2 (B) <- D via xv"]
    before = pr._shorted(bg, {"U1": bg.pads_of(ref="U1")})
    assert before == {("U1", "x1"), ("U1", "xv")}
    assert pr.touch_shorts(bg, ["U1"], before) == []
    assert pr.touch_shorts(bg, ["U1"], {("U1", "x1")}) \
        == ["U1.2 (B) <- D via xv"]


class FakeRouter:
    """join_net's view of mazeroute.Router. The net's islands are boxes on
    F.Cu. join_islands scribbles on the router's state (join_net must undo
    that when it fails) and fails until a push has run, or always when
    blocked. Each reroute pushes in one box: it fails when the box is in
    `stuck`, else joins the first island the box meets."""

    def __init__(self, islands, stuck=(), blocked=False):
        self.obs = SimpleNamespace(hidden={"h0"}, added=["a0"])
        self._conn = "c0"
        self.isl, self.stuck, self.blocked = list(islands), set(stuck), blocked
        self.pushes = 0
        self.calls: list = []

    def islands(self, net):
        return [{"F.Cu": [b], None: 1} for b in self.isl]

    def join_islands(self, net, want, near=None, width=None):
        self.calls.append(("join", want, width))
        self.obs.hidden.add("scrap")
        self.obs.added.append("scrap")
        self._conn = "scrap"
        if self.blocked or not self.pushes:
            raise CheckError("blocked")
        self.isl = self.isl[:want]
        return [{"op": "join"}]

    def reroute(self, net, region, keep=()):
        self.calls.append(("reroute", tuple(region), tuple(keep)))
        if tuple(region) in self.stuck:
            raise CheckError(f"no room in {list(region)}")
        self.isl.remove(next(b for b in self.isl if b.intersects(box(*region))))
        self.pushes += 1
        self.obs.hidden.add(f"u{self.pushes}")
        self.obs.added.append(f"push {self.pushes}")
        return [f"u{self.pushes}"], [{"op": "push", "n": self.pushes}], \
            {"V": self.pushes}


I1, I2, I3 = box(0, 0, 1, 1), box(10, 0, 11, 1), box(20, 0, 21, 1)
W1, W2, W3 = box(-1, -1, 2, 2), box(9, -1, 12, 2), box(19, -1, 22, 2)
FAR = box(50, 50, 51, 51)


def test_join_net_pushes_one_window_at_a_time_in_order():
    """When neither width joins the net, join_net pushes other nets aside in
    each box that one of its islands meets, in the order given (the part's
    own windows first), each with that box's bounds: it skips a box no
    island meets and one whose push fails, stops once the net is down to
    `want`, then joins what is left."""
    r = FakeRouter([I1, I2, I3], stuck=[W3.bounds])
    ops, uuids, how, maxes = pr.join_net(r, "N", 1, None, 0.3, ("K",),
                                         [FAR, W3, W2, W1, W3])
    assert r.calls == [("join", 1, 0.3), ("join", 1, None),
                       ("reroute", W3.bounds, ("K",)),
                       ("reroute", W2.bounds, ("K",)),
                       ("reroute", W1.bounds, ("K",)), ("join", 1, None)]
    assert ops == [{"op": "push", "n": 1}, {"op": "push", "n": 2},
                   {"op": "join"}]
    assert (uuids, how, maxes) == (["u1", "u2"], "pushed other nets aside",
                                   {"V": 2})
    # a net the plain join takes needs no push
    r = FakeRouter([I1, I2])
    r.pushes = 1
    assert pr.join_net(r, "N", 1, None, 0.3, (), [W1]) \
        == ([{"op": "join"}], [], "width 0.3", {})


def test_join_net_leaves_the_router_as_it_was_when_nothing_works():
    held = ({"h0"}, ["a0"], "c0")
    for r, boxes, why in (
            (FakeRouter([I1, I2]), [FAR], "no window meets its islands"),
            (FakeRouter([I1, I2], stuck=[W1.bounds]), [W1],
             "no room in [-1.0, -1.0, 2.0, 2.0]")):
        with pytest.raises(CheckError) as e:
            pr.join_net(r, "N", 1, None, 0.3, (), boxes)
        assert str(e.value) == ("N: width 0.3: blocked; rule width: blocked;"
                                f" push: {why}")
        assert (r.obs.hidden, r.obs.added, r._conn) == held
    # a push that works and a join after it that fails are undone too
    r = FakeRouter([I1, I2, I3], blocked=True)
    with pytest.raises(CheckError) as e:
        pr.join_net(r, "N", 1, None, None, (), [W1, W2])
    assert str(e.value) == "N: rule width: blocked; push: blocked"
    assert r.pushes == 2
    assert (r.obs.hidden, r.obs.added, r._conn) == held


# ------------------------------------------------------------------ live

@pytest.fixture
def kicad():
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    return cli


RENUMBER = {"op": "renumber", "ref": "R6",
            "pads": [{"at": [26.25, 50.3], "number": "2"},
                     {"at": [27.75, 50.3], "number": "1"}]}


def _copy(tmp_path, name) -> Path:
    work = tmp_path / name
    shutil.copytree(ROUTE, work)
    return work / "pd-trigger.kicad_pcb"


def _pads(pcb, ref="R6"):
    return sorted((p.number, p.net, round(p.center[0], 3))
                  for p in geom.BoardGeom.from_file(pcb).pads_of(ref=ref))


def _copper(pcb):
    bg = geom.BoardGeom.from_file(pcb)
    tracks = sorted((t.net, t.layer, round(t.width, 4), tuple(
        (round(x, 4), round(y, 4)) for x, y in t.shape.coords))
        for t in bg.tracks_of())
    vias = sorted((v.net, round(v.at[0], 4), round(v.at[1], 4),
                   round(v.diameter, 4)) for v in bg.vias_of())
    pads = sorted((p.ref, p.number, p.net or "", round(p.center[0], 4),
                   round(p.center[1], 4)) for p in bg.pads_of())
    return tracks, vias, pads


def _fills(pcb) -> dict:
    """(net, layer) -> the union of the zones' fills there."""
    got: dict = {}
    for z in geom.BoardGeom.from_file(pcb).zones_of():
        for ly, polys in z.fills.items():
            got.setdefault((z.net, ly), []).extend(polys)
    return {k: shapely.union_all(v) for k, v in got.items()}


def _same_fills(a, b) -> bool:
    fa, fb = _fills(a), _fills(b)
    empty = shapely.Polygon()
    return all(fa.get(k, empty).symmetric_difference(fb.get(k, empty)).area
               < 0.01 for k in set(fa) | set(fb))


def _replay_cmds(p, fresh) -> None:
    """Run the report's replay lines, as a shell would split them, on fresh
    in place of the board they name."""
    import place_edit
    import route_edit
    mains = {"route_edit": route_edit.main, "place_edit": place_edit.main}
    for line in p["replay"]:
        argv = shlex.split(line)
        assert argv[0] == "hwde" and argv[2] == "--pcb", line
        argv[3] = str(fresh)
        assert mains[argv[1]](argv[2:]) == 0, line


def _report(out) -> dict:
    return json.loads((out / "part_repair.json").read_text(encoding="utf-8"))


def test_live_renumber_r6_applies_and_replays(kicad, tmp_path):
    pcb = _copy(tmp_path, "kicad")
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(RENUMBER),
                    "--out", str(out), "--apply"]) == 0
    p = json.loads((out / "part_repair.json").read_text(encoding="utf-8"))
    assert p["status"] == "pass" and p["applied"]
    assert p["ripped"]["tracks"] > 0
    assert all(b <= a for a, b in p["islands"].values()), p["islands"]
    assert ("2", "/HV_B", 26.25) in _pads(pcb)
    assert ("1", "/ZBIAS", 27.75) in _pads(pcb)
    assert p["op_files"] == [pr.RIP_FILE, pr.PLACE_FILE, pr.ROUTE_FILE]
    assert not pcb.with_name(f".{pcb.name}.part_repair").exists()

    # the replay lines, run on a fresh copy, make the same copper and fills
    fresh = _copy(tmp_path, "fresh")
    _replay_cmds(p, fresh)
    assert _copper(fresh) == _copper(pcb)
    assert _same_fills(fresh, pcb)


def test_live_swap_fp_r6_to_0805_leaves_the_board(kicad, tmp_path):
    pcb = _copy(tmp_path, "kicad")
    before = pcb.read_bytes()
    out = tmp_path / "out"
    op = {"op": "swap_fp", "ref": "R6", "footprint": str(R0805),
          "fpid": "aiee:R0805"}
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(op),
                    "--out", str(out)]) == 0
    p = json.loads((out / "part_repair.json").read_text(encoding="utf-8"))
    assert p["status"] == "pass" and not p["applied"]
    assert pcb.read_bytes() == before
    assert _pads(out / "work" / pcb.name) \
        == [("1", "/ZBIAS", 26.0), ("2", "/HV_B", 28.0)]
    assert (out / "REPORT.md").read_text(encoding="utf-8") \
        .startswith("# part_repair: pass")
    assert all(b <= a for a, b in p["islands"].values()), p["islands"]


def test_live_a_net_that_will_not_join_writes_nothing(kicad, tmp_path,
                                                      monkeypatch):
    def no_way(router, net, *a, **k):
        raise CheckError(f"{net}: no way through (test)")
    monkeypatch.setattr(pr, "join_net", no_way)
    pcb = _copy(tmp_path, "kicad")
    before = pcb.read_bytes()
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(RENUMBER),
                    "--out", str(out), "--apply"]) == 1
    p = json.loads((out / "part_repair.json").read_text(encoding="utf-8"))
    assert p["status"] == "violations" and not p["applied"]
    assert p["failed"] and all("no way through" in f for f in p["failed"])
    assert pcb.read_bytes() == before
    assert not any((out / f).exists()
                   for f in (pr.RIP_FILE, pr.PLACE_FILE, pr.ROUTE_FILE))
    assert "Would not join" in (out / "REPORT.md").read_text(
        encoding="utf-8")


def test_live_a_worse_board_writes_nothing_and_names_why(kicad, tmp_path,
                                                         monkeypatch):
    fake = {"source": "drc", "check": "track_dangling", "severity": "warning",
            "pos": [27.0, 50.3], "layer": "F.Cu", "net": "/HV_B",
            "msg": "Track has an unconnected end",
            "items": [{"msg": "Track [/HV_B] on F.Cu", "pos": [27.0, 50.3],
                       "uuid": "test-uuid"}]}
    real = pr.gates

    def gates(drc0, drc1, isl, area=None):  # the repaired board's DRC
        drc1["violations"].append(fake)        # finds one more
        return real(drc0, drc1, isl, area)
    monkeypatch.setattr(pr, "gates", gates)
    pcb = _copy(tmp_path, "kicad")
    before = pcb.read_bytes()
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(RENUMBER),
                    "--out", str(out), "--apply"]) == 1
    p = json.loads((out / "part_repair.json").read_text(encoding="utf-8"))
    assert p["status"] == "violations" and not p["applied"]
    assert any(w.startswith("DRC drc:track_dangling: ") for w in p["worse"])
    assert "DRC drc:track_dangling: 1 new near the repair (none like it" \
        " before)" in p["worse"]
    assert [v["items"][0]["uuid"] for v in p["new_violations"]] \
        == ["test-uuid"]
    assert pcb.read_bytes() == before
    assert not any((out / f).exists()
                   for f in (pr.RIP_FILE, pr.PLACE_FILE, pr.ROUTE_FILE))
    md = (out / "REPORT.md").read_text(encoding="utf-8")
    assert "Worse than before" in md and "(items test-uuid)" in md


STUB_A = {"op": "add_track", "net": "/D3_A", "layer": "F.Cu", "width": 0.25,
          "start": [49.3, 55.6], "end": [50.5, 55.6]}
STUB_B = {"op": "add_track", "net": "/R2_MID", "layer": "F.Cu",
          "width": 0.25, "start": [49.48, 29.0], "end": [50.68, 29.0]}


def test_live_gates_see_a_dangling_stub_that_moved(kicad, tmp_path):
    """Real DRC: a stub on one net goes, one on another net comes, so the
    track_dangling count stays. The gate fails when the new stub is in the
    repair area, and not when only the old one is."""
    import kc
    import route_edit
    drc = []
    for name, op in (("a", STUB_A), ("b", STUB_B)):
        pcb = _copy(tmp_path, name)
        route_edit.apply_ops(pcb, [op])
        drc.append(kc.run_drc(kicad, pcb, refill=True, save_board=True))
    a, b = drc
    whole = box(0, 0, 100, 100)
    line = "DRC drc:track_dangling: 1 new near the repair (none like it before)"
    assert pr.gates(a, a, {}, whole) == []
    assert pr.gates(a, b, {}) == []
    assert pr.gates(a, b, {}, whole) == [line]
    assert pr.gates(a, b, {}, box(48, 27, 52, 31)) == [line]
    assert pr.gates(a, b, {}, box(48, 54, 52, 57)) == []
    nv = pr.new_violations(a, b, box(48, 27, 52, 31))
    assert [(v["key"], v["net"]) for v in nv] \
        == [("drc:track_dangling", "/R2_MID")]
    assert nv[0]["pos"] == pytest.approx([49.48, 29.0], abs=1e-3)


MOVE = {"op": "move", "ref": "R6", "x": 27, "y": 50.6}


def test_live_move_r6_replays_from_the_place_file(kicad, tmp_path):
    """A move small enough that R6's copper still meets its pads needs no
    rip and no route: the place file alone, refilled, replays it. The pour
    round R6 is gated too."""
    pcb = _copy(tmp_path, "kicad")
    before = pcb.read_bytes()
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(MOVE),
                    "--out", str(out)]) == 0
    p = _report(out)
    assert p["status"] == "pass" and not p["applied"]
    assert pcb.read_bytes() == before
    assert p["op_files"] == [pr.PLACE_FILE]
    assert len(p["replay"]) == 1 and p["replay"][0].endswith(
        " --allow-routed --refill")
    assert "GND" in p["islands"]
    assert all(b <= a for a, b in p["islands"].values()), p["islands"]
    work = out / "work" / pcb.name
    ys = {round(q.center[1], 3)
          for q in geom.BoardGeom.from_file(work).pads_of(ref="R6")}
    assert ys == {50.6}
    fresh = _copy(tmp_path, "fresh")
    _replay_cmds(p, fresh)
    assert _copper(fresh) == _copper(work)
    assert _same_fills(fresh, work)


@pytest.mark.parametrize("op, want", [
    ({"op": "rotate", "ref": "R6", "deg": 90},
     {("1", "F.Cu", 27.0, 51.05), ("2", "F.Cu", 27.0, 49.55)}),
    # The kit flips top to bottom (placelib), so pads on the part's
    # centre line keep x and y and only change side.
    ({"op": "flip", "ref": "R6", "side": "back"},
     {("1", "B.Cu", 26.25, 50.3), ("2", "B.Cu", 27.75, 50.3)}),
], ids=["rotate", "flip"])
def test_live_rotate_and_flip_r6_rejoin_and_replay(kicad, tmp_path, op,
                                                   want):
    pcb = _copy(tmp_path, "kicad")
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(op),
                    "--out", str(out)]) == 0
    p = _report(out)
    assert p["status"] == "pass", p.get("worse") or p.get("failed")
    assert all(b <= a for a, b in p["islands"].values()), p["islands"]
    work = out / "work" / pcb.name
    pads = geom.BoardGeom.from_file(work).pads_of(ref="R6")
    assert {(q.number, q.layers[0] if q.layers else None,
             round(q.center[0], 3), round(q.center[1], 3))
            for q in pads} == want
    fresh = _copy(tmp_path, "fresh")
    _replay_cmds(p, fresh)
    assert _copper(fresh) == _copper(work)
    assert _same_fills(fresh, work)


def test_live_a_join_that_must_push_other_nets_aside(kicad, tmp_path,
                                                     monkeypatch):
    """A router whose joins are blocked until a push has run: each of R6's
    nets is joined by pushing other nets aside in R6's window, the copper
    it pushed aside is replaced, what that left hanging is pruned, and the
    replay makes the same copper."""
    class Pushy(mazeroute.Router):
        block = True

        def join_islands(self, *a, **k):
            if self.block:
                raise CheckError(f"{a[0]}: blocked (test)")
            return super().join_islands(*a, **k)

        def reroute(self, *a, **k):
            self.block = False
            return super().reroute(*a, **k)
    monkeypatch.setattr(mazeroute, "Router", Pushy)
    pcb = _copy(tmp_path, "kicad")
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(RENUMBER),
                    "--out", str(out)]) == 0
    p = _report(out)
    assert p["status"] == "pass", p.get("worse") or p.get("failed")
    assert p["pushed"] and p["pruned"]
    assert "pushed other nets aside" in {j.get("how")
                                         for j in p["joins"].values()}
    assert all(b <= a for a, b in p["islands"].values()), p["islands"]
    fresh = _copy(tmp_path, "fresh")
    _replay_cmds(p, fresh)
    assert _copper(fresh) == _copper(out / "work" / pcb.name)


def test_live_a_board_changed_during_the_run_is_not_replaced(
        kicad, tmp_path, monkeypatch):
    pcb = _copy(tmp_path, "kicad")
    before = pcb.read_bytes()
    real = pr._replay

    def replay(*a):     # someone saves the board while the run checks it
        with pcb.open("ab") as f:
            f.write(b"\n")
        return real(*a)
    monkeypatch.setattr(pr, "_replay", replay)
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(MOVE),
                    "--out", str(out), "--apply"]) == 2
    p = _report(out)
    assert p["status"] == "error" and not p["applied"]
    assert "changed while part_repair ran" in p["error"]
    assert (out / pr.PLACE_FILE).exists()
    assert not pcb.with_name(f".{pcb.name}.part_repair").exists()
    assert pcb.read_bytes() == before + b"\n"


def test_live_an_error_is_reported_in_the_out_dir(kicad, tmp_path,
                                                  monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(pr, "windows", boom)
    pcb = _copy(tmp_path, "kicad")
    before = pcb.read_bytes()
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(MOVE),
                    "--out", str(out), "--apply"]) == 2
    p = _report(out)
    assert p["status"] == "error" and p["error"] == "RuntimeError: boom"
    md = (out / "REPORT.md").read_text(encoding="utf-8")
    assert md.startswith("# part_repair: error")
    assert "Error: RuntimeError: boom" in md
    assert pcb.read_bytes() == before
