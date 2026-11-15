"""part_repair: move, rotate or re-pad parts on a routed board and mend the
copper round them (rip, place, rejoin, gate).

The fast tests run the pieces on a synthetic part U1 with four pads in a row
and the copper shapes a rip has to tell apart: a chain through a via (net A),
a junction (B), a track that leaves the window (C) and a T onto a track's
middle (D); the gates on made-up DRC results; join_net's push on a fake
router; and the teardrops a new via takes from a removed one and the drops
the check finds lost (#1138), on made-up vias. The live tests work on copies of the routed pd_trigger fixture: the
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
from shapely.geometry import LineString, Point, box

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import env  # noqa: E402
import geom  # noqa: E402
import mazeroute  # noqa: E402
import part_repair as pr  # noqa: E402
import placelib  # noqa: E402
import teardroplib as tdl  # noqa: E402
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


def _add(net, a, b):
    return {"op": "add_track", "net": net, "layer": "F.Cu", "width": 0.25,
            "start": list(a), "end": list(b)}


def _fp(ref, x, y, net, size=0.4):
    """A one-pad part: copper a route can end on that holds."""
    return (f'(footprint "T:P" (layer "F.Cu") (at {x} {y}) (uuid "f{ref}")'
            f' (property "Reference" "{ref}")\n (pad "1" smd rect (at 0 0)'
            f' (size {size} {size}) (layers "F.Cu") (net "{net}")))\n')


def _pour(net, x0, y0, x1, y1, uid, teardrop=False):
    pts = f"(xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})"
    attr = " (attr (teardrop (type padvia)))" if teardrop else ""
    return (f'(zone (net "{net}") (net_name "{net}") (layer "F.Cu")'
            f' (uuid "{uid}"){attr}\n (polygon (pts {pts}))\n'
            f' (filled_polygon (layer "F.Cu") (pts {pts})))\n')


def _u1(tmp_path, extra, name):
    pcb = _board(tmp_path, U1 + COPPER + extra, name=name)
    return pcb, geom.BoardGeom.from_file(pcb)


def test_dangling_peels_dead_chains_from_the_seed(u1):
    """#1140: what KiCad's DRC calls dangling goes, one piece at a time as
    each removal leaves the next end free: from the copper that meets the
    seed out along its chain, wherever that leads."""
    pcb, bg = u1
    nets = {"B", "C"}
    gone, cut = pr._dangling(pcb, bg, nets, seed=WIN)
    assert set(gone) == {"b0", "b1", "b2", "c0", "c1"} and cut == []
    # the stubs at the fork go before the track they leave free
    assert gone.index("b0") > max(gone.index("b1"), gone.index("b2"))
    assert set(pr._dangling(pcb, bg, nets)[0]) == set(gone)
    # c1's free end alone in the seed takes c0 behind it, out of the seed
    assert pr._dangling(pcb, bg, nets, seed=box(1.5, 3.5, 2.5, 4.5)) \
        == (["c1", "c0"], [])
    # a seed with nothing dead in it takes nothing, though a3 dangles
    assert pr._dangling(pcb, bg, {"A"}, seed=box(-3.5, -2.5, -2.5, -1.5)) \
        == ([], [])
    # an anchor stays and holds what leads to it
    assert set(pr._dangling(pcb, bg, nets, seed=WIN, anchors={"b2"})[0]) \
        == {"b1", "c0", "c1"}
    # copper that is gone leaves what it held free
    assert pr._dangling(pcb, bg, {"C"}, seed=WIN, gone={"c1"}) == (["c0"], [])


def test_dangling_takes_a_via_met_on_one_layer(u1):
    """KiCad's via_dangling: a via that its net meets on one layer only
    dangles. With a2 and a3 gone, vA has a1 on F.Cu alone: it goes, then a1
    and a0. All of A dangles (a3 runs off to nothing): out from a3's end."""
    pcb, bg = u1
    assert pr._dangling(pcb, bg, {"A"}, seed=WIN, gone={"a2", "a3"}) \
        == (["vA", "a1", "a0"], [])
    assert pr._dangling(pcb, bg, {"A"}, seed=WIN) \
        == (["a3", "a2", "vA", "a1", "a0"], [])


U2D = _fp("U2", 4.6, -4, "D") + _seg((4.6, -2), (4.6, -4), "D", "d4")
AROUND = (_seg((4.4, 0), (6, 0), "D", "d8a") + _seg((6, 0), (6, -4), "D", "d8b")
          + _seg((6, -4), (4.6, -4), "D", "d8c"))


def test_dangling_keeps_a_dead_end_that_is_a_nets_only_link(tmp_path):
    """d1's two ends meet nothing, but d0 (from pad 4) and d4 (to U2's pad)
    meet its middle: without d1, D falls in two, so it stays, cut back to
    the two joints (#1140). With another way round from pad 4 to U2, d1
    goes, and d4 and d0 after it."""
    pcb, bg = _u1(tmp_path, U2D, "link")
    cuts = {}
    assert pr._dangling(pcb, bg, {"D"}, seed=WIN, cuts=cuts) == ([], [])
    was, now = cuts.pop("d1")
    assert not cuts and (was.a, was.b) == ((3, -2), (5, -2))
    assert (now.uuid, now.layer, now.width, now.a, now.b) \
        == ("d1", "F.Cu", 0.25, (4, -2), (4.6, -2))
    assert pr._dangling(pcb, bg, {"D"}, seed=WIN) == ([], [])
    # what a run leaves: dangling on the board as it is, cut back or not
    assert pr._dead_on(pcb, bg, {"D"}) == {"d1"}
    pcb, bg = _u1(tmp_path, U2D + AROUND, "loop")
    assert pr._dangling(pcb, bg, {"D"}, seed=WIN, cuts=cuts) \
        == (["d1", "d4", "d0"], [])
    assert cuts == {}


def test_dangling_cuts_back_a_track_whose_partner_went(tmp_path):
    """#1140 (R7, C216-C224): the run pushed c0 aside, so c1's end where c0
    met it is dead, but a route of the run tees into c1, which alone joins
    it to P6: c1 is cut back to the tee, as KiCad would leave no tail."""
    pcb, bg = _u1(tmp_path, _fp("P6", 2, 4, "C") + _fp("P7", 3, 3, "C"),
                  "tee")
    tee = _add("C", (3, 3), (2, 3))
    cuts = {}
    assert pr._dangling(pcb, bg, {"C"}, seed=WIN, adds=[tee], cuts=cuts) \
        == ([], [])
    assert cuts == {}               # c0 holds both of c1's ends
    assert pr._dangling(pcb, bg, {"C"}, seed=WIN, adds=[tee], cuts=cuts,
                        gone={"c0"}) == ([], [])
    was, now = cuts.pop("c1")
    assert not cuts and (was.a, was.b, now.a, now.b) \
        == ((2, 2.5), (2, 4), (2, 3), (2, 4))


def test_dangling_cuts_the_routers_tail_past_its_joint(tmp_path):
    """A route of the run from U2's pad that runs on past d1 to nothing is
    the only link from U2 to D: it is cut back to d1 (by its add's index),
    and d1, which it alone joins to d0, to the two joints."""
    pcb, bg = _u1(tmp_path, _fp("U2", 4.6, -4, "D"), "over")
    over = _add("D", (4.6, -4), (4.6, -1))
    cuts = {}
    assert pr._dangling(pcb, bg, {"D"}, seed=WIN, adds=[over], cuts=cuts) \
        == ([], [])
    assert sorted(cuts) == ["+0", "d1"]
    assert (cuts["+0"][1].a, cuts["+0"][1].b) == ((4.6, -4), (4.6, -2))
    assert (cuts["d1"][1].a, cuts["d1"][1].b) == ((4, -2), (4.6, -2))


def test_set_ends_and_the_report_name_a_cut():
    """A cut board track keeps its uuid: set_point moves the ends that
    changed. The report counts the cuts."""
    Seg = pr.route_cleanup.Seg
    was = Seg("t1", "D", "F.Cu", 0.25, (3.0, -2.0), (5.0, -2.0))
    assert pr._set_ends("t1", was, Seg("t1", "D", "F.Cu", 0.25, (4.0, -2.0),
                                       (5.0, -2.0))) \
        == [{"op": "set_point", "uuid": "t1", "which": "start",
             "to": [4.0, -2.0]}]
    assert [o["which"] for o in pr._set_ends("t1", was, Seg(
        "t1", "D", "F.Cu", 0.25, (4.0, -2.0), (4.6, -2.0)))] \
        == ["start", "end"]
    note = pr._cut_note("t1", "D", "F.Cu", ((3, -2), (5, -2)),
                        ((4, -2), (4.6, -2)))
    assert note == {"uuid": "t1", "net": "D", "layer": "F.Cu",
                    "from": [3, -2, 5, -2], "to": [4, -2, 4.6, -2]}
    p = {"status": "pass", "board": "b", "refs": ["U1"], "ops": 1,
         "applied": True, "shortened": [note]}
    assert "Cut back 1 track with a dead end" in pr.report_md(p)


def test_dangling_trims_the_routers_dead_tails(u1):
    """An add is copper like any other: one with an end on nothing is
    trimmed (its index comes back), and what it alone held goes after it."""
    pcb, bg = u1
    tail = _add("B", (0, 1), (1, 1))      # off b0's middle to nothing
    gone, cut = pr._dangling(pcb, bg, {"B"}, seed=WIN, adds=[tail])
    assert set(gone) == {"b0", "b1", "b2"} and cut == [0]


def test_dangling_prunes_the_stubs_behind_a_new_junction(tmp_path):
    """A route from a pad that lands where b0 forks meets b1 and b2 at that
    end only, and b0 holds it there to pad 2: the two stubs go, b0 stays."""
    pcb, bg = _u1(tmp_path, _fp("P1", 0, 2.8, "B"), "fork")
    add = _add("B", (0, 2), (0, 2.8))
    gone, cut = pr._dangling(pcb, bg, {"B"}, seed=WIN, adds=[add])
    assert set(gone) == {"b1", "b2"} and cut == []


def test_dangling_keeps_the_copper_a_new_route_hangs_on(tmp_path):
    """A route that lands on b1's free end hangs on b1, so b1 and b0 stay.
    A dead stub d2 off d1's end goes with d1 and d0 (d1's other end is
    free); with a route from a pad onto that end, d2 goes and d1 stays: it
    is all that joins the route to d0 and pad 4."""
    pcb, bg = _u1(tmp_path, _fp("P1", 0.6, 2.8, "B"), "b1")
    add = _add("B", (0.6, 2), (0.6, 2.8))
    assert pr._dangling(pcb, bg, {"B"}, seed=WIN, adds=[add]) == (["b2"], [])
    d2 = _seg((5, -2), (5.5, -2.5), "D", "d2")
    pcb, bg = _u1(tmp_path, d2, "d2")
    assert pr._dangling(pcb, bg, {"D"}, seed=WIN) == (["d2", "d1", "d0"], [])
    pcb, bg = _u1(tmp_path, d2 + _fp("P2", 5, -2.8, "D"), "d2p")
    add = _add("D", (5, -2), (5, -2.8))
    assert pr._dangling(pcb, bg, {"D"}, seed=WIN, adds=[add]) == (["d2"], [])


def test_dangling_counts_pours_not_teardrops(tmp_path):
    """A B pour under b1's free end holds b1, and b0 behind it; a teardrop
    there does not: the refill makes teardrops again from what is left."""
    pcb, bg = _u1(tmp_path, _pour("B", 0.4, 1.8, 1.0, 2.2, "zB"), "pour")
    assert pr._dangling(pcb, bg, {"B"}, seed=WIN) == (["b2"], [])
    pcb, bg = _u1(tmp_path, _pour("B", 0.4, 1.8, 1.0, 2.2, "tB", True), "td")
    assert [z.teardrop for z in bg.zones_of()] == [True]
    assert set(pr._dangling(pcb, bg, {"B"}, seed=WIN)[0]) \
        == {"b0", "b1", "b2"}


def _bpour(net, x0, y0, x1, y1, uid):
    pts = f"(xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})"
    return (f'(zone (net "{net}") (net_name "{net}") (layer "B.Cu")'
            f' (uuid "{uid}")\n (polygon (pts {pts}))\n'
            f' (filled_polygon (layer "B.Cu") (pts {pts})))\n')


VIA_P = ('(via (at 0 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
         ' (net "P") (uuid "vP"))\n')


def test_peel_keeps_board_copper_whose_pour_the_refill_cut_back(tmp_path):
    """#1140: a via in U9's pad met P's B.Cu pour on the base board; on the
    work board a new via of Q cuts that pour back from it, so it meets the
    pad alone and dangles. It was live design copper: it stays, with a
    note on what cut the pour, and the gate counts what that costs. A via
    of the run's own, or one whose track the run took, goes."""
    part = _fp("U9", 0, 0, "P", 0.8)
    base = _board(tmp_path, part + VIA_P + _bpour("P", -2, -2, 2, 2, "zP")
                  + _seg((0, 0), (-1.5, 0), "P", "tP", "B.Cu"), "base")
    work = _board(tmp_path, part + VIA_P + _bpour("P", 0.6, -2, 2, 2, "zP"),
                  "work")
    bg0, bg1 = (geom.BoardGeom.from_file(p) for p in (base, work))
    seed = box(-1, -1, 1, 1)
    assert pr._dangling(work, bg1, {"P"}, seed=seed) == (["vP"], [])
    add = {"op": "add_via", "net": "Q", "at": [0.0, 0.9], "size": 0.4,
           "drill": 0.2}
    tail = (" the B.Cu pour of P back from via vP at (0.0, 0.0) on U9.1: the"
            " via stays (it met the pour on the base board) and the gate"
            " counts what that costs")
    # tP on the base board too: the run took what vP hung on, so it goes
    anchors, notes = set(), []
    assert pr._peel(work, bg0, bg1, {"P"}, made={}, adds_on=[add],
                    refs=[], anchors=anchors, notes=notes, seed=seed) \
        == (["vP"], [], {})
    assert anchors == set() and notes == []
    base = _board(tmp_path, part + VIA_P + _bpour("P", -2, -2, 2, 2, "zP"),
                  "base2")
    bg0 = geom.BoardGeom.from_file(base)
    assert pr._peel(work, bg0, bg1, {"P"}, made={}, adds_on=[add],
                    refs=[], anchors=anchors, notes=notes, seed=seed) \
        == ([], [], {})
    assert anchors == {"vP"}
    assert notes == ["the new via of Q at (0.0, 0.9) cuts" + tail]
    # no add of another net near: the refill did it
    notes = []
    assert pr._peel(work, bg0, bg1, {"P"}, made={}, adds_on=[], refs=[],
                    anchors=set(), notes=notes, seed=seed) == ([], [], {})
    assert notes == ["the refill cuts" + tail]
    # a via the run made goes
    assert pr._peel(work, bg0, bg1, {"P"}, made={"vP": 0}, adds_on=[add],
                    refs=[], anchors=set(), notes=[], seed=seed) \
        == (["vP"], [], {})


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
    assert pr.patch_notes(bg, old, away, uuids=True) == [("zA", notes[0])]


def test_orphan_patches_find_a_patch_left_joining_nothing(tmp_path):
    """#1140 (the C216-C224 job on the voice board): a PATCH_<net> zone
    that met copper of its net before the run and meets none after it is
    isolated copper, removed in the route ops with a note in place of the
    early one; one a track of its net still meets stays, as does a zone
    not named PATCH_ or one with no uuid."""
    bg0 = geom.BoardGeom.from_file(_board(tmp_path, U1 + PATCH_A, "b0"))
    assert pr.orphan_patches(bg0, bg0) == []
    away = U1.replace("(at 0 0)", "(at 10 0)", 1)
    bg1 = geom.BoardGeom.from_file(_board(tmp_path, away + PATCH_A, "b1"))
    [(z, pads)] = pr.orphan_patches(bg0, bg1)
    assert (z.uuid, z.name, z.net, pads) == ("zA", "PATCH_A", "A", ["U1.1"])
    held = _seg((-2, 0), (-6, 0), "A", "h0")
    bg2 = geom.BoardGeom.from_file(_board(tmp_path, away + PATCH_A + held,
                                          "b2"))
    assert pr.orphan_patches(bg0, bg2) == []
    for other in (PATCH_A.replace('"PATCH_A"', '"POUR_A"'),
                  PATCH_A.replace(' (uuid "zA")', "")):
        b0 = geom.BoardGeom.from_file(_board(tmp_path, U1 + other, "c0"))
        b1 = geom.BoardGeom.from_file(_board(tmp_path, away + other, "c1"))
        assert pr.orphan_patches(b0, b1) == []
    early = pr.patch_notes(bg1, {"U1": bg0.pads_of(ref="U1")},
                           {"U1": bg1.pads_of(ref="U1")})
    gone, marks = [], []
    payload = {"notes": ["kept", *early], "removed_zones": []}
    pr._drop_patch(z, pads, gone, marks, payload, early)
    assert gone == ["zA"]
    assert marks[0].contains(box(-3, -1, -1, 1).buffer(0.99))
    assert payload["removed_zones"] == [
        {"uuid": "zA", "name": "PATCH_A", "net": "A", "layers": ["F.Cu"],
         "met": ["U1.1"]}]
    assert payload["notes"][0] == "kept" and len(payload["notes"]) == 2
    assert payload["notes"][1].startswith(
        "zone PATCH_A on F.Cu joins no copper of A after the run, where it"
        " met U1.1 before: removed")


def _starved(ref, num, zone, *, layer="B.Cu", pos=(1, 1), pth=False,
             zlayers="B.Cu", said=True):
    """A starved_thermal as kc.run_drc reads it from KiCad 10.0.6: the
    message names the layer (said), the items the zone and the pad."""
    pad = (f"PTH pad {num} [GND] of {ref}" if pth
           else f"Pad {num} [GND] of {ref} on {layer}")
    msg = "Thermal relief connection to zone incomplete" + (
        f" (layer {layer}; zone min spoke count 2; actual 1)" if said
        else "")
    return {"source": "drc", "check": "starved_thermal", "severity": "error",
            "net": "GND", "layer": layer, "msg": msg,
            "pos": [0, 0], "items": [
                {"msg": f"Zone [GND] on {zlayers}, priority 0",
                 "pos": [0, 0], "uuid": zone},
                {"msg": pad, "pos": list(pos), "uuid": f"p-{ref}-{num}"}]}


def test_starved_pads_name_the_pad_its_layer_and_zone():
    """#1140: KiCad names a starved thermal's zone and pad in its items
    and the layer in its message; only those the run made count. A zone's
    text lists its layers and a PTH pad's names none, so the layer is the
    message's, else the pad's, by the board's canonical name; else None."""
    old = _starved("C210", "2", "zG", pos=(5, 5))
    new = _starved("C221", "1", "zG", pos=(9, 9))
    clr = _vat("drc", "clearance", "A", (2, 2), (2, 3))
    assert pr.starved_pads({"violations": [old]},
                           {"violations": [old, new, clr]}) \
        == [("C221", "1", "B.Cu", "zG")]
    smd = _starved("C9", "2", "zM", zlayers="F.Cu and B.Cu")
    pth = _starved("J1", "1", "zG", pth=True,
                   zlayers="F.Cu, In1.Cu and B.Cu")
    inner = _starved("J2", "3", "zM", pth=True, layer="SIG1",
                     zlayers="F.Cu, SIG1 and B.Cu")
    mute = _starved("J3", "1", "zG", pth=True, said=False,
                    zlayers="F.Cu and B.Cu")
    bare = _starved("C8", "1", "zG", said=False)
    named = _starved("C5", "1", "zN")
    named["items"][0]["msg"] = "Zone 'PATCH_GND' [GND] on B.Cu, priority 2"
    assert pr.starved_pads(
        {"violations": []},
        {"violations": [smd, pth, inner, mute, bare, named]},
        {"SIG1": "In1.Cu"}) == [
        ("C9", "2", "B.Cu", "zM"), ("J1", "1", "B.Cu", "zG"),
        ("J2", "3", "In1.Cu", "zM"), ("J3", "1", None, "zG"),
        ("C8", "1", "B.Cu", "zG"), ("C5", "1", "B.Cu", "zN")]


def test_layer_names_map_a_renamed_layer_to_its_own(tmp_path):
    """#1140: DRC names a layer by the board's name for it; layer_names
    maps that back to the canonical one."""
    pcb = tmp_path / "named.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20260206) (generator "test")\n'
        '  (general (thickness 1.6))\n'
        '  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal "SIG1")\n'
        '    (2 "B.Cu" signal) (25 "Edge.Cuts" user))\n'
        '  (setup)\n  (net 0 "")\n)\n')
    names = pr.layer_names(geom.BoardGeom.from_file(pcb))
    assert names["SIG1"] == "In1.Cu" and "F.Cu" not in names


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


UNREAD_DRU = ('(version 1)\n(rule "odd_F" (layer "F.Cu")'
              ' (constraint disallow track)'
              ' (condition "A.Type == \'track\'"))\n')


class _PastTheStop(Exception):
    """run_drc was called: the run got past the unread-rule stop."""


def test_an_unread_disallow_rule_stops_the_run_unless_allowed(tmp_path,
                                                                monkeypatch):
    pcb = _board(tmp_path, U1)
    pcb.with_suffix(".kicad_dru").write_text(UNREAD_DRU, encoding="utf-8")
    monkeypatch.setattr(pr.env, "find_kicad_cli", lambda: Path("kicad-cli"))
    monkeypatch.setattr(pr.env, "find_kicad_python",
                        lambda cli: Path("python"))

    def drc(*a, **k):
        raise _PastTheStop()
    monkeypatch.setattr(pr.kc, "run_drc", drc)
    op = json.dumps({"op": "rotate", "ref": "U1", "deg": 90})
    out = tmp_path / "o"
    with pytest.raises(CheckError, match="odd_F.*--allow-unread-rules"):
        pr.run(["--pcb", str(pcb), "--op", op, "--out", str(out)])
    p = _report(out)
    assert (p["status"], p["unread_dru_rules"], p["allow_unread_rules"]) \
        == ("error", ["odd_F"], False)
    assert "odd_F have a condition the router doesn't read, so the run" \
        " stopped." in (out / "REPORT.md").read_text(encoding="utf-8")
    with pytest.raises(_PastTheStop):
        pr.run(["--pcb", str(pcb), "--op", op, "--out", str(tmp_path / "o2"),
                "--allow-unread-rules"])
    p = _report(tmp_path / "o2")
    assert (p["unread_dru_rules"], p["allow_unread_rules"]) \
        == (["odd_F"], True)
    # an error after the stop is not the stop's: the report says the run
    # went on
    assert "routed anyway and left them to DRC" in (
        tmp_path / "o2" / "REPORT.md").read_text(encoding="utf-8")


def test_made_maps_each_adds_uuid_to_its_index_and_names_the_dups():
    ops = [{"op": "delete_track"}] + [{"op": "add_track"}] * 3 \
        + [{"op": "add_via"}]
    results = [{"status": "ok"}, {"uuid": "t1", "status": "ok"},
               {"status": "exists"}, {"status": "ok"},
               {"uuid": "v1", "status": "ok"}]
    assert pr._made(ops, results, 1) == ({"t1": 0, "v1": 3}, {1})
    # results that stop short made nothing past their end
    assert pr._made(ops, results[:2], 1) == ({"t1": 0}, set())


# ------------------------------------------------------------ teardrops (#1138)

_FULL = {"enabled": True, "length_ratio": 0.6, "max_length_mm": 1.2,
         "width_ratio": 0.9, "max_width_mm": 1.8, "filter_ratio": 0.8,
         "curved": True, "two_tracks": False, "prefer_zones": True}


@pytest.mark.parametrize("p, want", [
    (None, None),
    ({"enabled": False, "length_ratio": 0.6}, None),
    ({"enabled": True}, {}),
    (_FULL, {k: _FULL[k] for k in tdl.ITEM_PARAMS}),
    # a value route_edit would refuse is left out: the default stands;
    # a zero is KiCad's own (max_length 0: no cap) and is copied
    ({"enabled": True, "length_ratio": 0, "max_length_mm": -1,
      "width_ratio": float("nan"), "max_width_mm": True,
      "filter_ratio": "0.9", "curved": 1, "two_tracks": "yes",
      "prefer_zones": True}, {"length_ratio": 0.0, "prefer_zones": True}),
])
def test_td_params_are_add_vias_teardrops(p, want):
    got = pr._td_params(p)
    assert got == want
    if got:
        assert all(isinstance(v, bool) == isinstance(tdl.ITEM_PARAMS[k], bool)
                   for k, v in got.items())


def test_lands_on_is_an_add_route_edit_would_answer_exists():
    vias = [SimpleNamespace(uuid="v1", net="A", at=(1.0, 2.0),
                            layers=("F.Cu", "B.Cu")),
            SimpleNamespace(uuid="v2", net="A", at=(7.0, 2.0),
                            layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))]
    op = {"op": "add_via", "net": "A", "at": [1.0005, 2.0]}
    assert pr._lands_on(op, vias) == "v1"
    assert pr._lands_on(dict(op, at=[7.0, 2.0], top="F.Cu", bot="B.Cu"),
                        vias) == "v2"
    assert pr._lands_on(dict(op, at=[1.002, 2.0]), vias) is None
    assert pr._lands_on(dict(op, net="B"), vias) is None
    assert pr._lands_on(dict(op, bot="In1.Cu"), vias) is None


def test_restore_teardrops_gives_each_new_via_its_nets_nearest():
    lost = [SimpleNamespace(uuid="b", net="A", at=(0.0, 0.0)),
            SimpleNamespace(uuid="a", net="A", at=(4.0, 0.0)),
            SimpleNamespace(uuid="f", net="A", at=(10.0, 0.0)),
            SimpleNamespace(uuid="c", net="B", at=(9.0, 9.0)),
            SimpleNamespace(uuid="d", net="C", at=(1.0, 1.0)),
            SimpleNamespace(uuid="e", net="C", at=(2.0, 2.0))]
    on = {"enabled": True, "length_ratio": 0.5}
    params = {"b": dict(on, length_ratio=0.6), "a": on, "c": on,
              "d": dict(on, enabled=False), "e": None,
              "f": dict(on, enabled=False)}
    work = [SimpleNamespace(uuid="w1", net="A", at=(5.0, 5.0),
                            layers=("F.Cu", "B.Cu")),
            SimpleNamespace(uuid="w2", net="A", at=(10.0, 0.0),
                            layers=("F.Cu", "B.Cu"))]
    adds = [{"op": "add_via", "net": "A", "at": [1.0, 0.0]},
            # as far from a as from b: the lower uuid
            {"op": "add_via", "net": "A", "at": [2.0, 0.0]},
            {"op": "add_track", "net": "A", "start": [0, 0], "end": [1, 0]},
            # net C lost vias, none with teardrops on
            {"op": "add_via", "net": "C", "at": [1.0, 1.0]},
            # on a via already there: route_edit makes nothing, and the
            # via there is noted (landed) for the check
            {"op": "add_via", "net": "A", "at": [5.0, 5.0]},
            {"op": "add_via", "net": "B", "at": [0.0, 9.0]},
            # the nearest lost via (f) had them off, though a had them on
            {"op": "add_via", "net": "A", "at": [8.0, 0.0]},
            # on a via already there whose nearest lost via had them off:
            # nothing to note
            {"op": "add_via", "net": "A", "at": [10.0, 0.0]}]
    landed: dict = {}
    src = pr.restore_teardrops(adds, lost, params, work, landed)
    assert src == ["b", "a", None, None, None, "c", None, None]
    assert landed == {"w1": "a"}
    assert adds[0]["teardrops"] == {"length_ratio": 0.6}
    assert adds[1]["teardrops"] == adds[5]["teardrops"] == {
        "length_ratio": 0.5}
    assert [("teardrops" in o) for o in adds] == [
        True, True, False, False, False, True, False, False]
    # each op gets its own copy
    adds[1]["teardrops"]["curved"] = True
    assert adds[5]["teardrops"] == {"length_ratio": 0.5}
    # with no vias passed (those the list removes left out), the add at
    # w1 makes a new via and takes a's
    add = {"op": "add_via", "net": "A", "at": [5.0, 5.0]}
    assert pr.restore_teardrops([add], lost, params, []) == ["a"]
    assert add["teardrops"] == {"length_ratio": 0.5}


def test_late_teardrops_give_the_re_prunes_losses_to_vias_made_without():
    """A via the re-prune after the route takes off passes its teardrops
    to the new vias on its net that took none yet, when it is the nearest
    the run took off (gone: every one so far): kept and td_src set in
    place, and the add_vias to send again returned with every setting
    spelled out (route_edit keeps a via's own value for a key left out)."""
    on = {"enabled": True, "length_ratio": 0.6}
    gone = [SimpleNamespace(uuid="g", net="A", at=(0.0, 0.0)),
            SimpleNamespace(uuid="h", net="B", at=(9.0, 9.0))]
    params = {"g": on, "h": dict(on, enabled=False)}
    kept = [{"op": "add_via", "net": "A", "at": [1.0, 0.0]},
            {"op": "add_track", "net": "A", "start": [0, 0], "end": [1, 0]},
            # took teardrops before the route
            {"op": "add_via", "net": "A", "at": [2.0, 0.0],
             "teardrops": {"length_ratio": 0.5}},
            # going now: skip
            {"op": "add_via", "net": "A", "at": [3.0, 0.0]},
            # net B's gone via had them off
            {"op": "add_via", "net": "B", "at": [9.0, 8.0]},
            # net C lost no via
            {"op": "add_via", "net": "C", "at": [4.0, 0.0]},
            # not made (the route did not apply it): not in made
            {"op": "add_via", "net": "A", "at": [5.0, 0.0]}]
    td_src = [None, None, "x", None, None, None, None]
    made = {"u0": 0, "u1": 1, "u2": 2, "u3": 3, "u4": 4, "u5": 5}
    again = pr.late_teardrops(kept, made, td_src, gone, params, {3})
    full = dict(tdl.ITEM_PARAMS, length_ratio=0.6)
    assert again == [dict(kept[0], teardrops=full)]
    assert kept[0]["teardrops"] == full
    assert td_src == ["g", None, "x", None, None, None, None]
    assert kept[2]["teardrops"] == {"length_ratio": 0.5}
    assert [("teardrops" in o) for o in kept] == [
        True, False, True, False, False, False, False]
    # a copy: the op sent again does not share kept's dict
    again[0]["at"] = [7.0, 7.0]
    assert kept[0]["at"] == [1.0, 0.0]
    # nothing left to give: none sent again
    assert pr.late_teardrops(kept, made, td_src, gone, params, {3}) == []
    # out of skip, the op going no more takes them too
    assert pr.late_teardrops(kept, made, td_src, [], params, set()) == []
    # a nearer via the run took off with them off: it takes none
    near = gone + [SimpleNamespace(uuid="i", net="A", at=(3.5, 0.0))]
    assert pr.late_teardrops(kept, made, td_src, near,
                             dict(params, i=None), set()) == []
    assert [o["at"] for o in pr.late_teardrops(
        kept, made, td_src, gone, params, set())] == [[3.0, 0.0]]


def test_takes_drop_and_drop_length_read_an_items_settings():
    """KiCad 10.0.6 makes no drop for a track at or over filter_ratio x
    the via's size (0.9 x 0.254 = 0.2286 mm), and lays it over
    length_ratio x the size past the via's edge, capped by max_length_mm
    (0 sets no cap); a key left out reads as ITEM_PARAMS."""
    assert tdl.takes_drop(0.2, 0.254, {})
    assert not tdl.takes_drop(0.229, 0.254, {})
    assert not tdl.takes_drop(0.3, 0.254, {"filter_ratio": 0.9})
    assert not tdl.takes_drop(0.2, 0.254, {"max_width_mm": 0.2})
    assert tdl.takes_drop(0.25, 0.3, {})
    assert tdl.drop_length(0.254, {}) == pytest.approx(0.127)
    assert tdl.drop_length(4.0, {}) == pytest.approx(1.0)
    assert tdl.drop_length(4.0, {"max_length_mm": 0}) == pytest.approx(2.0)
    assert tdl.drop_length(0.5, {"length_ratio": 0.8,
                                 "max_length_mm": "x"}) == pytest.approx(0.4)


@pytest.mark.parametrize("widths, wide, floor, want", [
    # the widest the removed via's drops took that takes one here
    ([0.2, 0.15, 0.22], 0.3, 0.1, 0.22),
    ([0.25, 0.2], 0.3, 0.1, 0.2),
    # none of them: the net's width, then its floor
    ([], 0.2, 0.1, 0.2),
    ([0.25], 0.3, 0.12, 0.12),
    # never under the floor
    ([0.1], 0.3, 0.15, 0.15),
    # no width at or over the floor takes a drop: leave it to the gate
    ([0.2], 0.3, 0.25, None),
])
def test_neck_width_is_the_widest_a_drop_takes(widths, wide, floor, want):
    assert pr.neck_width(0.254, dict(tdl.ITEM_PARAMS), widths, wide,
                         floor) == want


def test_neck_tracks_narrow_a_track_too_wide_for_a_new_vias_drop():
    """A track too wide for the drop on a new via that took teardrops
    narrows from its end in the via to NECK_PAST past where the drop ends
    (0.127 mm past the 0.254 mm via's edge), walking on through the one
    track that goes on from its far end; the rest keeps its width, split
    off with its orientation, td_src growing with it. Tracks already
    narrow, of another net, cut back by the prune, or on a via that took
    no teardrops stay as they are, and a walk stops at a branch."""
    via = {"op": "add_via", "net": "A", "at": [0.0, 0.0], "size": 0.254,
           "drill": 0.15, "teardrops": {}}

    def tr(layer, a, b, w=0.3, net="A"):
        return {"op": "add_track", "net": net, "layer": layer,
                "width": w, "start": list(a), "end": list(b)}

    kept = [via,
            tr("F.Cu", (0, 0), (1, 0)),            # 1: via at its start
            tr("B.Cu", (2, 0), (0, 0)),            # 2: via at its end
            tr("In1.Cu", (0, 0), (0, 0.1)),        # 3: wholly inside,
            tr("In1.Cu", (0, 0.1), (0, 1.0)),      # 4: walked on to
            tr("In2.Cu", (0, 0), (0, 0.2)),        # 5: then a branch
            tr("In2.Cu", (0, 0.2), (1, 0.2)),      # 6
            tr("In2.Cu", (0, 0.2), (-1, 0.2)),     # 7
            tr("In3.Cu", (0, 0), (0, -1), w=0.2),  # 8: narrow already
            tr("In4.Cu", (0, 0), (0, -1), net="B"),  # 9: another net
            tr("In5.Cu", (0, 0), (0, -1)),         # 10: cut back
            {"op": "add_via", "net": "A", "at": [5.0, 0.0], "size": 0.254,
             "drill": 0.15},                        # 11: took none
            tr("F.Cu", (5, 0), (6, 0))]             # 12
    td_src = ["s"] + [None] * (len(kept) - 1)
    asked = []

    def width_of(i, p):
        asked.append((i, p["filter_ratio"]))
        return 0.2

    n = len(kept)
    out = pr.neck_tracks(kept, td_src, {10: ((), ())}, width_of)
    assert asked == [(0, 0.9)]
    at = round(0.127 + 0.127 + pr.NECK_PAST, 4)
    assert kept[1] == tr("F.Cu", (0, 0), (at, 0), w=0.2)
    assert kept[2] == tr("B.Cu", (at, 0), (0, 0), w=0.2)
    assert kept[3] == tr("In1.Cu", (0, 0), (0, 0.1), w=0.2)
    assert kept[4] == tr("In1.Cu", (0, 0.1), (0, at), w=0.2)
    assert kept[5] == tr("In2.Cu", (0, 0), (0, 0.2), w=0.2)
    assert kept[n:] == [tr("F.Cu", (at, 0), (1, 0)),
                        tr("B.Cu", (2, 0), (at, 0)),
                        tr("In1.Cu", (0, at), (0, 1.0))]
    assert kept[6:13] == [
        tr("In2.Cu", (0, 0.2), (1, 0.2)),
        tr("In2.Cu", (0, 0.2), (-1, 0.2)),
        tr("In3.Cu", (0, 0), (0, -1), w=0.2),
        tr("In4.Cu", (0, 0), (0, -1), net="B"),
        tr("In5.Cu", (0, 0), (0, -1)), kept[11],
        tr("F.Cu", (5, 0), (6, 0))]
    assert td_src == ["s"] + [None] * (len(kept) - 1)
    assert len(td_src) == n + 3
    assert [(o["layer"], o["width"], o["was"], o["length"]) for o in out] \
        == [("F.Cu", 0.2, 0.3, pytest.approx(at)),
            ("B.Cu", 0.2, 0.3, pytest.approx(at)),
            ("In1.Cu", 0.2, 0.3, pytest.approx(at)),
            ("In2.Cu", 0.2, 0.3, pytest.approx(0.2))]
    assert {(o["net"], tuple(o["via"])) for o in out} == {("A", (0.0, 0.0))}
    # run again: nothing in the via's copper is too wide now
    assert pr.neck_tracks(kept, td_src, {10: ((), ())}, width_of) == []
    assert asked == [(0, 0.9)]
    # no width to narrow to: nothing changes, the gate names the loss
    kept2 = [dict(via), tr("F.Cu", (0, 0), (1, 0))]
    src2 = ["s", None]
    assert pr.neck_tracks(kept2, src2, {}, lambda i, p: None) == []
    assert kept2[1] == tr("F.Cu", (0, 0), (1, 0)) and src2 == ["s", None]


@pytest.mark.parametrize("params, want", [
    ([], pr.TD_READ),
    ([None, {"enabled": False, "max_length_mm": 9.0}], pr.TD_READ),
    ([{"enabled": True}], pr.TD_READ),
    ([{"enabled": True, "max_length_mm": 0.5}], pr.TD_READ),
    ([None, {"enabled": True, "max_length_mm": 2}], 5.0),
    ([{"enabled": True, "max_length_mm": 2.0},
      {"enabled": True, "max_length_mm": 4.0}], 10.0),
    # a value that is not a finite number is not read
    ([{"enabled": True, "max_length_mm": True},
      {"enabled": True, "max_length_mm": "9"},
      {"enabled": True, "max_length_mm": float("nan")},
      {"enabled": True, "max_length_mm": float("inf")}], pr.TD_READ),
])
def test_td_read_reaches_past_the_longest_drop(params, want):
    assert pr.td_read(params) == pytest.approx(want)


def _via(uid, at, on=True, joins=None, held=(), drops=None):
    return tdl.Item("via", "A", at, Point(at).buffer(0.3), ("F.Cu", "B.Cu"),
                    0.15, 0.6, on, uuid=uid, joins=dict(joins or {}),
                    held=set(held), drops=dict(drops or {}))


def test_teardrop_loss_names_the_drops_the_run_lost():
    both = {"F.Cu": 1, "B.Cu": 1}
    b0 = SimpleNamespace(items=[
        _via("s1", (0, 0), joins=both, drops=both),
        _via("s2", (1, 0), joins=both, drops={"B.Cu": 1}),
        _via("k2", (2, 0), joins=both, drops=both),
        _via("k3", (3, 0), joins=both, drops=both),
        _via("k4", (4, 0), joins=both, drops={"B.Cu": 1}),
        _via("k5", (50, 50), joins=both, drops=both),
        _via("k6", (6, 0), joins=both, drops=both),
        _via("k7", (7, 0), on=False, joins=both)])
    b1 = SimpleNamespace(items=[
        _via("n1", (0, 1), joins=both, drops=both),
        _via("n2", (2, 1), joins=both, drops={"F.Cu": 1}),
        _via("n3", (3, 1), joins=both, drops={"B.Cu": 1}),
        _via("n4", (4, 1), on=False, joins=both),
        _via("k2", (2, 0), joins=both, drops={"B.Cu": 1}),
        _via("k3", (3, 0), on=False, joins=both),
        _via("k4", (4, 0), joins=both, drops={"B.Cu": 1}),
        _via("k5", (50, 50), joins=both),
        # a pour holds its F.Cu side now: KiCad makes the drop there too,
        # so its loss counts
        _via("k6", (6, 0), joins=both, held={"F.Cu"}, drops={"B.Cu": 1}),
        _via("k7", (7, 0), on=False, joins=both)])
    new_src = {"n1": "s1", "n2": "s1", "n3": "s2", "n4": "s1",
               "gone": "s1"}  # the re-prune took "gone"
    lost, short = pr.teardrop_loss(b0, b1, new_src, box(-10, -10, 10, 10))
    took = "on the via it took its teardrops from"
    assert [(e["via"], e["layers"], e["why"]) for e in lost] == [
        ("n2", ["B.Cu"], "on B.Cu 0 of 1 tracks into it have a drop, against"
         f" 1 {took}: 1 without one"),
        ("n4", [], "has its teardrops off"),
        ("k2", ["F.Cu"], "on F.Cu 0 of 1 tracks into it have a drop, against"
         " 1 before: 1 without one"),
        ("k3", [], "had teardrops on and has them off"),
        ("k6", ["F.Cu"], "on F.Cu 0 of 1 tracks into it have a drop, against"
         " 1 before: 1 without one")]
    # n3's via had no drop on F.Cu either: no loss, but a join without one
    assert [(e["via"], e["layers"], e["why"]) for e in short] == [
        ("n3", ["F.Cu"], "on F.Cu 0 of 1 tracks into it have a drop, against"
         f" 0 {took}: 1 without one")]
    assert lost[0]["net"] == "A" and lost[0]["at"] == [2.0, 1.0]
    assert pr._td_line(lost[0]) == (
        f"A via at (2.000, 1.000) on B.Cu 0 of 1 tracks into it have a drop,"
        f" against 1 {took}: 1 without one")


def test_teardrop_loss_judges_a_new_via_by_the_drops_not_the_gaps():
    """The via the new one took its teardrops from had two joins on F.Cu
    and one drop (one join without); the new via has one join and no drop.
    Its gaps (1) are no more than the old one's, but the old via's one
    drop is gone: lost."""
    b0 = SimpleNamespace(items=[
        _via("s", (0, 0), joins={"F.Cu": 2}, drops={"F.Cu": 1})])
    b1 = SimpleNamespace(items=[_via("n", (0, 1), joins={"F.Cu": 1})])
    lost, short = pr.teardrop_loss(b0, b1, {"n": "s"}, box(-1, -1, 1, 1))
    assert short == [] and [(e["via"], e["layers"], e["why"])
                            for e in lost] == [
        ("n", ["F.Cu"], "on F.Cu 0 of 1 tracks into it have a drop, against"
         " 1 on the via it took its teardrops from: 1 without one")]
    # fewer tracks into it than the old via had drops: each needs one
    b1 = SimpleNamespace(items=[
        _via("n", (0, 1), joins={"F.Cu": 1}, drops={"F.Cu": 1})])
    b0 = SimpleNamespace(items=[
        _via("s", (0, 0), joins={"F.Cu": 3}, drops={"F.Cu": 3})])
    assert pr.teardrop_loss(b0, b1, {"n": "s"},
                            box(-1, -1, 1, 1)) == ([], [])


def _band(x, w):
    """A drop's band round a track down from the via at (x, 0)."""
    return ((LineString([(x, 0), (x, -2)]), w / 2 + tdl.BAND_MM),)


def _cvia(uid, size, cands, tips=None, at=(0, 0)):
    """A via read() filled: cands layer -> [(width, band)], tips layer ->
    [Point]; joins and drops follow from them at its size."""
    lim = tdl._limits(tdl.ITEM_PARAMS)
    tips = {k: list(v) for k, v in (tips or {}).items()}
    joins = {k: n for k, v in cands.items()
             if (n := sum(1 for w, _ in v if not tdl.too_wide(w, size, lim)))}
    return tdl.Item("via", "A", at, Point(at).buffer(size / 2),
                    ("F.Cu", "B.Cu"), 0.15, size, True, uuid=uid,
                    joins=joins, drops={k: len(v) for k, v in tips.items()},
                    lim=lim, cands=dict(cands), tips=tips)


def test_teardrop_loss_judges_a_smaller_via_at_the_size_it_replaced():
    """A 0.46 mm track got a drop on the 0.6 mm via the run removed
    (0.46 < 0.9 x 0.6); on the 0.5 mm via that took its teardrops it is
    too wide for one (0.46 >= 0.9 x 0.5), so KiCad makes none: lost, with
    the reason, though the new via's own count wants none."""
    wide, thin = (0.46, _band(0, 0.46)), (0.2, _band(0.1, 0.2))
    b0 = SimpleNamespace(items=[
        _cvia("s", 0.6, {"F.Cu": [wide]}, {"F.Cu": [Point(0, -0.9)]})])
    small = _cvia("n", 0.5, {"F.Cu": [wide]})
    assert small.joins == {} and small.missing(in_pours=True) == []
    lost, short = pr.teardrop_loss(b0, SimpleNamespace(items=[small]),
                                   {"n": "s"}, box(-1, -1, 1, 1))
    assert short == [] and [(e["via"], e["layers"], e["why"])
                            for e in lost] == [
        ("n", ["F.Cu"], "on F.Cu 0 of 1 tracks into it have a drop, against"
         " 1 on the via it took its teardrops from (a 0.46 mm track): a 0.46"
         " mm track is too wide for a drop on a 0.5 mm via")]
    # with its drop, or on a track thin enough for both, nothing is lost
    for it in (_cvia("n", 0.5, {"F.Cu": [wide]}, {"F.Cu": [Point(0, -0.8)]}),
               _cvia("n", 0.5, {"F.Cu": [thin]}, {"F.Cu": [Point(0.1, -1)]})):
        assert pr.teardrop_loss(b0, SimpleNamespace(items=[it]),
                                {"n": "s"}, box(-1, -1, 1, 1)) == ([], [])


@pytest.mark.parametrize("why, said", [
    ("grazes", "grazes the copper"),
    ("crosses", "crosses it off the centre")])
def test_teardrop_loss_names_a_track_kicad_drops_on_at_no_width(why, said):
    """The run moved the track into a kept via so that it grazes the
    copper (or crosses it off the centre): KiCad makes it no drop at any
    width, and the drop the via had is lost."""
    a = (0.2, _band(0, 0.2))
    b0 = SimpleNamespace(items=[
        _cvia("k", 0.6, {"F.Cu": [a]}, {"F.Cu": [Point(0, -1)]})])
    k1 = _cvia("k", 0.6, {})
    k1.grazes = {"F.Cu": [(0.2, why)]}
    assert k1.into("F.Cu") == 1 and k1.answered("F.Cu") == 0 \
        and k1.deficit("F.Cu") == 0
    lost, short = pr.teardrop_loss(b0, SimpleNamespace(items=[k1]), {},
                                   box(-1, -1, 1, 1))
    assert short == [] and [(e["via"], e["layers"], e["why"])
                            for e in lost] == [
        ("k", ["F.Cu"], "on F.Cu 0 of 1 tracks into it have a drop, against"
         f" 1 before (a 0.2 mm track): a 0.2 mm track {said}")]


def test_teardrop_loss_names_a_graze_by_a_track_too_wide_for_the_via():
    """C216-C224 on haos-voice: the removed 0.254 mm +2V5 via had its F.Cu
    drop on a 0.2 mm track through it; the route ends a 0.3 mm track on
    the centre of the new 0.254 mm via. Its chord (0.254) is short of the
    width, a graze to KiCad, but the width alone stops the drop (0.3 >=
    0.9 x 0.254): named too wide, with the width the old drop was on."""
    thin = (0.2, _band(0, 0.2))
    b0 = SimpleNamespace(items=[
        _cvia("s", 0.254, {"F.Cu": [thin]}, {"F.Cu": [Point(0, -0.4)]})])
    n = _cvia("n", 0.254, {})
    n.grazes = {"F.Cu": [(0.3, "grazes")]}
    lost, short = pr.teardrop_loss(b0, SimpleNamespace(items=[n]),
                                   {"n": "s"}, box(-1, -1, 1, 1))
    assert short == [] and [(e["via"], e["layers"], e["why"])
                            for e in lost] == [
        ("n", ["F.Cu"], "on F.Cu 0 of 1 tracks into it have a drop, against"
         " 1 on the via it took its teardrops from (a 0.2 mm track): a 0.3"
         " mm track is too wide for a drop on a 0.254 mm via")]
    # a graze narrow enough for a drop keeps its own reason
    n.grazes = {"F.Cu": [(0.2, "grazes")]}
    assert pr.teardrop_loss(b0, SimpleNamespace(items=[n]), {"n": "s"},
                            box(-1, -1, 1, 1))[0][0]["why"].endswith(
        "(a 0.2 mm track): a 0.2 mm track grazes the copper")


def test_teardrop_loss_names_a_via_a_new_one_landed_on_with_them_off():
    """The route's add_via landed on a via already there (route_edit made
    nothing), standing in for a removed via with teardrops on: with that
    via's own off, its drops are lost. Once only: a kept via told already
    is not told again."""
    b0 = SimpleNamespace(items=[
        _via("s", (0, 0), joins={"F.Cu": 1}, drops={"F.Cu": 1}),
        _via("k", (2, 0), joins={"F.Cu": 1}, drops={"F.Cu": 1})])
    b1 = SimpleNamespace(items=[
        _via("w", (1, 0), on=False, joins={"F.Cu": 1}),
        _via("k", (2, 0), on=False, joins={"F.Cu": 1}),
        _via("x", (3, 0), joins={"F.Cu": 1}, drops={"F.Cu": 1})])
    lost, short = pr.teardrop_loss(b0, b1, {}, box(-5, -5, 5, 5),
                                   {"w": "s", "k": "s", "x": "s",
                                    "gone": "s"})
    assert short == [] and [(e["via"], e["why"]) for e in lost] == [
        ("k", "had teardrops on and has them off"),
        ("w", "has its teardrops off, and the route's via for a removed one"
         " with them on (s) landed on it: route_edit add_via on it with"
         ' "teardrops": {} turns them on')]
    assert pr.teardrop_loss(b0, b1, {}, box(-5, -5, 5, 5)) == (lost[:1], [])


def test_teardrop_loss_counts_drops_per_track_not_per_layer():
    """A kept via whose F.Cu drop count holds (2) but whose two drops now
    both lie on one track (a track through it got extra ones) lost the
    other track's drop."""
    a, b = (0.2, _band(0, 0.2)), (0.2, _band(0.25, 0.2))
    tip_a, tip_a2, tip_b = Point(0, -1), Point(0.02, -0.9), Point(0.25, -1)
    b0 = SimpleNamespace(items=[
        _cvia("k", 0.6, {"F.Cu": [a, b]}, {"F.Cu": [tip_a, tip_b]})])
    b1 = SimpleNamespace(items=[
        _cvia("k", 0.6, {"F.Cu": [a, b]}, {"F.Cu": [tip_a, tip_a2]})])
    assert b1.items[0].drops == b0.items[0].drops == {"F.Cu": 2}
    lost, short = pr.teardrop_loss(b0, b1, {}, box(-1, -1, 1, 1))
    assert short == [] and [(e["via"], e["layers"], e["why"])
                            for e in lost] == [
        ("k", ["F.Cu"], "on F.Cu 1 of 2 tracks into it have a drop, against"
         " 2 before (0.2 and 0.2 mm tracks): a 0.2 mm track has none")]


def test_report_says_how_many_new_vias_took_teardrops():
    p = {"status": "pass", "board": "b.kicad_pcb", "refs": ["C1"],
         "ops": 1, "applied": False,
         "teardrops": {"restored": 2, "from": {}, "lost": [], "short": []}}
    md = pr.report_md(p)
    assert ("Teardrops: 2 new via(s) took them from the vias the run"
            " removed.") in md
    p["teardrops"] = {"restored": 0, "lost": [{"via": "x"}], "short": []}
    assert ("Teardrops: 0 new via(s) took them from the vias the run"
            " removed; 1 via(s) short of drops the base board had (below)."
            ) in pr.report_md(p)
    p["teardrops"] = {"restored": 0, "lost": [], "short": []}
    assert "Teardrops:" not in pr.report_md(p)


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

    def islands(self, net, scraps=True):
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


def test_live_a_cut_applies_and_replays(kicad, tmp_path):
    """#1140: the set_point ops part_repair writes for a cut (a board track
    kept, its dead end cut back) apply as its run applies them, and replay
    from its ops file. A sweep's R7 rot180 failed here: route_edit took
    set_point from its own move_via only, so the run's apply raised after
    the board was swapped in and the replay refused the op."""
    import route_edit

    pcb = _copy(tmp_path, "kicad")
    t = min((t for t in geom.BoardGeom.from_file(pcb).tracks_of()
             if t.uuid and len(t.shape.coords) == 2 and t.shape.length > 1),
            key=lambda t: t.uuid)
    a, b = (tuple(q) for q in t.shape.coords)
    Seg = pr.route_cleanup.Seg
    q = (round(a[0] + 0.75 * (b[0] - a[0]), 4),
         round(a[1] + 0.75 * (b[1] - a[1]), 4))
    ops = pr._set_ends(t.uuid, Seg(t.uuid, t.net, t.layer, t.width, a, b),
                       Seg(t.uuid, t.net, t.layer, t.width, a, q))
    assert [r["status"] for r in pr.route_edit.apply_ops(pcb, ops)] \
        == ["changed"]
    fresh = _copy(tmp_path, "fresh")
    pr._write_ops(tmp_path / pr.ROUTE_FILE, ops)
    assert route_edit.main(["--pcb", str(fresh), "--ops",
                            str(tmp_path / pr.ROUTE_FILE)]) == 0
    assert _copper(fresh) == _copper(pcb) != _copper(ROUTE / pcb.name)


def test_live_renumber_r6_applies_and_replays(kicad, tmp_path,
                                              monkeypatch):
    pcb = _copy(tmp_path, "kicad")
    out = tmp_path / "out"
    loads: list = []

    def load(path):
        loads.append(path)
        return placelib.PlaceModel(path)

    monkeypatch.setattr(pr, "placelib", SimpleNamespace(PlaceModel=load))
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(RENUMBER),
                    "--out", str(out), "--apply"]) == 0
    # no clearance rule held to a footprint: no board model (#1163)
    assert loads == []
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


def test_live_a_net_that_fails_joins_again_first(kicad, tmp_path,
                                                 monkeypatch):
    """A net that won't join in the parts' order (as when a join before it
    took its room) gets a second pass from the board after the rips, with
    it first. The first pass's joins are undone, not kept: the replay of
    the second makes the same copper."""
    real = pr.join_net
    calls = []

    def second_fails(router, net, *a, **k):
        calls.append(net)
        if len(calls) == 2:
            raise CheckError(f"{net}: no room (test)")
        return real(router, net, *a, **k)
    monkeypatch.setattr(pr, "join_net", second_fails)
    pcb = _copy(tmp_path, "kicad")
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(RENUMBER),
                    "--out", str(out)]) == 0
    p = _report(out)
    assert p["status"] == "pass", p.get("worse") or p.get("failed")
    a, b = calls[:2]
    again = calls[calls.index(b, 2):]       # the second pass, b first
    assert a in again, calls                # a's first join was undone
    assert p["join_passes"] == [{"first": [], "failed": [b]},
                                {"first": [b], "failed": []}]
    assert any("joins ran again" in n for n in p["notes"])
    assert "failed" not in {j["how"] for j in p["joins"].values()}
    assert all(y <= x for x, y in p["islands"].values()), p["islands"]
    fresh = _copy(tmp_path, "fresh")
    _replay_cmds(p, fresh)
    assert _copper(fresh) == _copper(out / "work" / pcb.name)


def _quarter_in(s, end):
    """route_cleanup.Seg s with its `end` end ("a" or "b") moved a quarter
    of the way along it."""
    p, q = (s.a, s.b) if end == "b" else (s.b, s.a)
    at = (round(p[0] + 0.75 * (q[0] - p[0]), 4),
          round(p[1] + 0.75 * (q[1] - p[1]), 4))
    return dataclasses.replace(s, **{end: at})


def test_live_the_late_prune_folds_each_pass_into_the_ops(kicad, tmp_path,
                                                          monkeypatch):
    """#1140: what each re-prune after a refill finds goes into the ops.
    A dead route of the run leaves its add op (trimmed), a dead board track
    is removed (pruned), and a cut shortens a route's add op or moves a
    board track's end (shortened), from where the track was before, also
    when a later pass cuts it again or removes it. The replay makes the
    work board. The peels are made up (the R6 job leaves none) and the gate
    is left out (a made-up removal can split a net)."""
    import route_cleanup
    pcb = _copy(tmp_path, "kicad")
    cu = list(geom.BoardGeom.from_file(pcb).copper_layers)
    base = {s.uuid for s in route_cleanup.parse_items(pcb, cu)[0]}
    real = pr._dangling
    seen: list = []     # the board's tracks as each late pass found them
    pick: dict = {}

    def late(board, bg, nets, *, seed=None, cuts=None, **k):
        if "adds" in k or seed is None:     # the first prune, _dead_on
            return real(board, bg, nets, seed=seed, cuts=cuts, **k)
        segs = {s.uuid: s for s in route_cleanup.parse_items(board, cu)[0]}
        seen.append(segs)
        if len(seen) == 1:
            runs = sorted(u for u, s in segs.items()
                          if u not in base and s.length > 0.2)
            olds = sorted(u for u, s in segs.items()
                          if u in base and s.net in nets and s.length > 1)
            assert len(runs) >= 2 and len(olds) >= 3, (runs, olds)
            pick.update(r1=runs[0], r2=runs[1], b1=olds[0], b2=olds[1],
                        b3=olds[2])
            for u in (pick["r1"], pick["b2"]):
                cuts[u] = (segs[u], _quarter_in(segs[u], "b"))
            return [pick["b1"]], []
        if len(seen) == 2:
            for u in (pick["r1"], pick["b3"]):
                cuts[u] = (segs[u], _quarter_in(segs[u], "a"))
            return [pick["r2"], pick["b2"]], []
        return [], []
    monkeypatch.setattr(pr, "_dangling", late)
    monkeypatch.setattr(pr, "gates", lambda *a, **k: [])
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(RENUMBER),
                    "--out", str(out)]) == 0
    p = _report(out)
    assert p["status"] == "pass" and len(seen) == 3
    first, last = seen[0], seen[1]
    r1, r2, b1, b2, b3 = (pick[k] for k in ("r1", "r2", "b1", "b2", "b3"))

    def ends(s):
        return [pr._r(v) for q in (s.a, s.b) for v in q]
    assert p["pruned"][-2:] == [b1, b2]
    assert [(o["start"], o["end"]) for o in p["trimmed"]][-1:] \
        == [(list(first[r2].a), list(first[r2].b))]
    rows = {(r["uuid"], tuple(r["from"])): r["to"] for r in p["shortened"]}
    assert (b2, tuple(ends(first[b2]))) not in rows      # cut, then gone
    assert rows[(b3, tuple(ends(first[b3])))] \
        == ends(_quarter_in(last[b3], "a"))
    r1_now = _quarter_in(last[r1], "a")                  # cut twice
    assert rows[("", tuple(ends(first[r1])))] == ends(r1_now)
    ops = json.loads((out / pr.ROUTE_FILE).read_text(
        encoding="utf-8"))["ops"]
    assert {o["uuid"] for o in ops if o["op"] == "remove"} >= {b1, b2}
    assert not any(o.get("uuid") == b2 for o in ops if o["op"] != "remove")
    assert [o["which"] for o in ops if o.get("uuid") == b3] == ["start"]
    adds = [(o["start"], o["end"]) for o in ops if o["op"] == "add_track"]
    assert (list(r1_now.a), list(r1_now.b)) in adds
    assert (list(first[r2].a), list(first[r2].b)) not in adds
    work = out / "work" / pcb.name
    now = {s.uuid: s for s in route_cleanup.parse_items(work, cu)[0]}
    assert not {b1, b2, r2} & set(now)
    assert (now[b3].a, now[b3].b) == (_quarter_in(last[b3], "a").a,
                                      last[b3].b)
    assert (now[r1].a, now[r1].b) == (r1_now.a, r1_now.b)
    fresh = _copy(tmp_path, "fresh")
    _replay_cmds(p, fresh)
    assert _copper(fresh) == _copper(work)


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
