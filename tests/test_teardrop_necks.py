"""#1320: the neck (#1138) as a shared helper. A route that ends a track too
wide for the drop on a via with teardrops on narrows it from the via out
past where the drop ends: teardroplib neck_ops / neck_board, route_finish's
joins (neck_jobs), part_repair's late peel (_late_necks). Each planted
fault (a wide join) has a quiet twin (a narrow join, a via without
teardrops, a via the plan takes off) that stays as it is."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import LineString

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import geom  # noqa: E402
import part_repair as pr  # noqa: E402
import route_finish as rf  # noqa: E402
import teardroplib as tdl  # noqa: E402

# a 0.254 mm via: KiCad 10.0.6 makes a drop only for a track under
# 0.9 x 0.254 = 0.229 mm; it ends 0.127 mm past the via's edge
AT = round(0.127 + 0.127 + tdl.NECK_PAST, 4)


def _via(at=(0.0, 0.0), net="A", td=True, uuid=None):
    v = {"op": "add_via", "net": net, "at": list(at), "size": 0.254,
         "drill": 0.15}
    if td:
        v["teardrops"] = {}
    if uuid:
        v["uuid"] = uuid
    return v


def _tr(a, b, w=0.3, net="A", layer="F.Cu"):
    return {"op": "add_track", "net": net, "layer": layer, "width": w,
            "start": list(a), "end": list(b)}


# ------------------------------------------------------------- neck_ops

def test_neck_ops_narrow_a_routes_track_on_a_board_drop_via():
    """Planted: a 0.3 mm track of the route ends on a board via with
    teardrops on: it narrows to 0.2 out to AT and the rest is split off at
    0.3. Quiet: a 0.2 mm track into it (takes a drop already), a track of
    another net there, and a via whose net the route has no track of stay
    as they are; the board vias are not in ops after."""
    vias = [_via(uuid="v1"), _via((9, 9), net="C", uuid="v2")]
    ops = [_tr((0, 0), (1, 0)),
           _tr((0, 0), (0, 1), w=0.2, layer="B.Cu"),
           _tr((0, 0), (-1, 0), net="B", layer="In1.Cu")]
    asked = []

    def width_of(v, p):
        asked.append((v["uuid"], p["filter_ratio"]))
        return [0.2]

    necks = tdl.neck_ops(ops, vias, width_of)
    assert asked == [("v1", 0.9)]
    assert ops == [_tr((0, 0), (AT, 0), w=0.2),
                   _tr((0, 0), (0, 1), w=0.2, layer="B.Cu"),
                   _tr((0, 0), (-1, 0), net="B", layer="In1.Cu"),
                   _tr((AT, 0), (1, 0))]
    assert necks == [{"via": [0.0, 0.0], "net": "A", "layer": "F.Cu",
                      "width": 0.2, "was": 0.3,
                      "length": pytest.approx(AT)}]
    # run again: nothing is too wide now
    assert tdl.neck_ops(ops, vias, width_of) == []


def test_neck_ops_neck_on_the_routes_own_teardropped_via_only():
    """A via of the route that carries "teardrops" necks its joins as a
    board drop via does; one without them (quiet twin) does not."""
    ops = [_via((3, 0), net="C"), _tr((3, 0), (4, 0), net="C"),
           _via((6, 0), net="D", td=False), _tr((6, 0), (7, 0), net="D")]
    necks = tdl.neck_ops(ops, [], lambda v, p: [0.15])
    assert [(n["net"], n["width"]) for n in necks] == [("C", 0.15)]
    assert ops[1] == _tr((3, 0), (3 + AT, 0), w=0.15, net="C")
    assert ops[3] == _tr((6, 0), (7, 0), net="D")
    assert ops[4:] == [_tr((3 + AT, 0), (4, 0), net="C")]


def test_neck_ops_with_no_width_a_drop_takes_change_nothing():
    """The net's floor forbids every width the drop takes: the track stays
    (the gate names the drop it loses)."""
    ops = [_tr((0, 0), (1, 0))]
    assert tdl.neck_ops(ops, [_via(uuid="v1")], lambda v, p: []) == []
    assert ops == [_tr((0, 0), (1, 0))]


def test_neck_ops_stop_at_board_copper():
    """A walk ends where board copper of the net meets the joint: the
    track past it keeps its width."""
    ops = [_tr((0, 0), (0.2, 0)), _tr((0.2, 0), (1, 0))]
    necks = tdl.neck_ops(ops, [_via(uuid="v1")], lambda v, p: [0.2],
                         lambda net, layer, pt: pt == (0.2, 0.0))
    assert ops == [_tr((0, 0), (0.2, 0), w=0.2), _tr((0.2, 0), (1, 0))]
    assert necks[0]["length"] == pytest.approx(0.2)


# ----------------------------------------------------------- neck_board

def _gtrack(uuid, a, b, w=0.3, net="A", layer="F.Cu"):
    return SimpleNamespace(uuid=uuid, shape=LineString([a, b]), width=w,
                           net=net, layer=layer)


def test_neck_board_lays_a_narrowed_board_track_again():
    """Tracks already on the board (route_auto's): one too wide is removed
    and laid again narrowed, with its far piece; one narrow enough is not
    touched. With nothing to narrow, no ops."""
    t1 = _gtrack("t1", (0, 0), (1, 0))
    t2 = _gtrack("t2", (0, 0), (0, 1), w=0.2, layer="B.Cu")
    ops, necks = tdl.neck_board([t1, t2], [_via(uuid="v1")],
                                lambda v, p: [0.2])
    assert ops == [{"op": "remove", "uuid": "t1"},
                   _tr((0.0, 0.0), (AT, 0), w=0.2),
                   _tr((AT, 0), (1.0, 0.0))]
    assert [(n["layer"], n["width"], n["was"]) for n in necks] \
        == [("F.Cu", 0.2, 0.3)]
    assert tdl.neck_board([t2], [_via(uuid="v1")],
                          lambda v, p: [0.2]) == ([], [])


# ------------------------------------------------------------ neck_jobs

HDR = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (net 0 "") (net 1 "GND") (net 2 "+3V3")
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
"""
TD_ON = (" (teardrops (best_length_ratio 0.5) (max_length 1)"
         " (best_width_ratio 1) (max_width 2) (curved_edges no)"
         " (filter_ratio 0.9) (enabled yes) (allow_two_segments yes)"
         " (prefer_zone_connections yes))")


def _bvia(x, y, net, uuid, td):
    return (f'  (via (at {x} {y}) (size 0.254) (drill 0.15)'
            f' (layers "F.Cu" "B.Cu") (net "{net}") (uuid "{uuid}")'
            f'{TD_ON if td else ""})\n')


def _jobs_board(tmp_path):
    p = tmp_path / "t.kicad_pcb"
    p.write_text(HDR + _bvia(10, 10, "+3V3", "v-td", True)
                 + _bvia(20, 10, "GND", "v-off", False) + ")\n",
                 encoding="utf-8")
    return p


def _jobs():
    return [{"status": "routed", "net": "+3V3",
             "ops": [_tr((10, 10), (15, 10), w=0.3, net="+3V3")]},
            {"status": "routed", "net": "GND",
             "ops": [_tr((20, 10), (25, 10), w=0.3, net="GND")]},
            {"status": "failed", "net": "+3V3",
             "ops": [_tr((10, 10), (10, 15), w=0.3, net="+3V3")]}]


def test_route_finish_necks_a_join_too_wide_for_a_board_vias_drop(
        tmp_path):
    """Planted fault: a routed +3V3 join ends 0.3 mm on a 0.254 mm via
    with teardrops on, where KiCad makes no drop over 0.229 mm: neck_jobs
    narrows it to the net's 0.2 mm out to AT and names the neck on the
    job. Quiet twins: the GND join on a via with teardrops off, and a
    failed job, stay as they are."""
    pcb = _jobs_board(tmp_path)
    bg = geom.BoardGeom.from_file(pcb)
    rules = rf.board_rules(pcb, bg)
    jobs = _jobs()
    out = rf.neck_jobs(pcb, bg, rules, jobs)
    w = rules.width("+3V3")
    assert tdl.takes_drop(w, 0.254, dict(tdl.ITEM_PARAMS))
    assert jobs[0]["ops"] == [
        _tr((10, 10), (10 + AT, 10), w=w, net="+3V3"),
        _tr((10 + AT, 10), (15, 10), w=0.3, net="+3V3")]
    assert [(n["net"], n["width"], n["was"]) for n in out] \
        == [("+3V3", w, 0.3)]
    assert jobs[0]["necks"] == out
    assert jobs[1]["ops"] == _jobs()[1]["ops"] and "necks" not in jobs[1]
    assert jobs[2]["ops"] == _jobs()[2]["ops"] and "necks" not in jobs[2]


def test_route_finish_leaves_a_join_on_a_via_the_plan_takes_off(tmp_path):
    """Quiet twin: the plan rips the teardropped via (rip), so no drop is
    built on it and the join keeps its width; a board with no teardrop
    setting at all is not read further."""
    pcb = _jobs_board(tmp_path)
    bg = geom.BoardGeom.from_file(pcb)
    rules = rf.board_rules(pcb, bg)
    jobs = _jobs()
    assert rf.neck_jobs(pcb, bg, rules, jobs, rip=["v-td"]) == []
    assert jobs == _jobs()
    plain = tmp_path / "p.kicad_pcb"
    plain.write_text(HDR + _bvia(10, 10, "+3V3", "v-td", False) + ")\n",
                     encoding="utf-8")
    assert rf.board_drop_vias(plain, geom.BoardGeom.from_file(plain)) == []


def test_route_finish_neck_takes_the_width_the_via_had_a_drop_on(tmp_path):
    """The board's own track into the via that takes a drop (0.21 mm)
    comes before the net's 0.2 mm: the neck keeps as much copper as the
    drop allows."""
    p = tmp_path / "t.kicad_pcb"
    p.write_text(HDR + _bvia(10, 10, "+3V3", "v-td", True)
                 + '  (segment (start 10 10) (end 10 5) (width 0.21)'
                 ' (layer "F.Cu") (net "+3V3") (uuid "s1"))\n)\n',
                 encoding="utf-8")
    bg = geom.BoardGeom.from_file(p)
    rules = rf.board_rules(p, bg)
    jobs = _jobs()[:1]
    out = rf.neck_jobs(p, bg, rules, jobs)
    assert rules.width("+3V3") == 0.2
    assert [n["width"] for n in out] == [0.21]
    assert jobs[0]["ops"][0]["width"] == 0.21


# ------------------------------------------------- part_repair late peel

def test_late_necks_lay_a_narrowed_route_of_the_run_again():
    """A via that took its teardrops in a peel after the refill (fresh)
    necks its joins: the run's track on the board is removed and laid
    again narrowed, its far piece laid; the position of each add op names
    its kept index. A via that had its teardrops all along (its joins
    necked before the route, or not too wide) and a track in skip are
    left alone."""
    kept = [_via(), _tr((0, 0), (1, 0)),
            _via((5, 0)), _tr((5, 0), (6, 0)),
            _tr((0, 0), (0, -1), layer="B.Cu")]
    td_src = ["late", None, "early", None, None]
    made = {"u-via": 0, "u-t1": 1, "u-via2": 2, "u-t3": 3, "u-t4": 4}
    ops, necks, at = pr._late_necks(kept, td_src, {0}, made, {4},
                                    lambda i, p: [0.2], None)
    assert ops == [{"op": "remove", "uuid": "u-t1"},
                   _tr((0, 0), (AT, 0), w=0.2),
                   _tr((AT, 0), (1, 0))]
    assert at == {1: 1, 2: 5}
    assert kept[3] == _tr((5, 0), (6, 0))
    assert kept[4] == _tr((0, 0), (0, -1), layer="B.Cu")
    assert td_src == ["late", None, "early", None, None, None]
    assert [(n["layer"], n["width"], n["ops"]) for n in necks] \
        == [("F.Cu", 0.2, [1])]
    assert pr.neck_lengths(necks, kept, set())[0]["length"] \
        == pytest.approx(AT)
