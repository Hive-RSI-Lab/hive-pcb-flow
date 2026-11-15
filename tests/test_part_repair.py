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
import placelib  # noqa: E402
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
