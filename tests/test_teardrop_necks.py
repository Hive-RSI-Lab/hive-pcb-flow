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


# ------------------------------------------- #1320 points 3-5: graze, grow

@pytest.mark.parametrize("size,floor,p,want", [
    (0.254, 0.25, {}, 0.28),       # 0.9 x 0.28 = 0.252 > 0.25; 0.27 is not
    (0.254, 0.3, {}, 0.34),
    (0.254, 0.35, {}, None),       # wants 0.39: over 1.5 x 0.254
    (0.254, 0.3, {"max_width_mm": 0.3}, None),   # max_width forbids it
    (0.6, 0.3, {}, 0.61),          # a step over the size it has
])
def test_grow_size_is_the_smallest_step_the_floor_takes_a_drop_on(
        size, floor, p, want):
    p = dict(tdl.ITEM_PARAMS, **p)
    got = tdl.grow_size(size, p, floor)
    assert got == (pytest.approx(want) if want else None)
    if got:
        assert tdl.takes_drop(floor, got, p)
        assert not tdl.takes_drop(floor, round(got - tdl.GROW_STEP, 4), p) \
            or round(got - tdl.GROW_STEP, 4) <= size


def test_neck_ops_neck_a_narrow_track_that_grazes_the_via():
    """Planted (#1320 point 4): a 0.2 mm track narrow enough for the drop
    ends 0.08 mm off the centre of a 0.254 mm via, so its line crosses a
    0.192 mm chord: a graze, KiCad makes no drop. It necks to the widest
    width under its own that clears the chord (0.15). Quiet twins: the
    same track through the centre (a 0.249 mm chord, a drop) and a graze
    with no narrower width to go to stay as they are."""
    assert tdl.grazes_on(0.2, tdl.chord_of((0, 0.08), (1, 0.08), (0, 0),
                                           0.254))
    ops = [_tr((0, 0.08), (1, 0.08), w=0.2)]
    necks = tdl.neck_ops(ops, [_via(uuid="v1")], lambda v, p: [0.2, 0.15])
    assert [(n["width"], n["was"]) for n in necks] == [(0.15, 0.2)]
    assert ops[0]["width"] == 0.15 and ops[1]["width"] == 0.2
    assert ops[0]["start"] == [0, 0.08] and ops[1]["end"] == [1, 0.08]
    assert ops[0]["end"] == ops[1]["start"]
    assert 0.25 < ops[0]["end"][0] < 0.35

    centred = [_tr((0, 0), (1, 0), w=0.2)]
    assert tdl.neck_ops(centred, [_via(uuid="v1")],
                        lambda v, p: [0.2, 0.15]) == []
    assert centred == [_tr((0, 0), (1, 0), w=0.2)]
    stuck = [_tr((0, 0.08), (1, 0.08), w=0.2)]
    assert tdl.neck_ops(stuck, [_via(uuid="v1")], lambda v, p: [0.2]) == []
    assert stuck == [_tr((0, 0.08), (1, 0.08), w=0.2)]


def _floor_widths(floor):
    """width_of for a net whose floor is floor: what neck_widths gives the
    via at the size it has now."""
    def width_of(i, p, kept):
        return tdl.neck_widths(float(kept[i]["size"]), p, [], floor, floor)
    return width_of


def test_neck_tracks_grow_a_via_whose_floor_forbids_every_drop_width():
    """Planted (#1320 point 5): a 0.3 mm net floor on a 0.254 mm via, whose
    drop takes only under 0.229 mm: no width is left, so the via grows to
    what grow gives (0.34), the 0.3 mm track takes its drop there as it is
    and the entry names the growth (neck_lengths keeps it, no ops). Quiet
    twin: grow says no room (None): the via keeps its size, no entry."""
    def run(grow_to):
        kept = [_via(), _tr((0, 0), (1, 0))]
        asked = []

        def grow(i, p):
            asked.append(i)
            return grow_to
        wo = _floor_widths(0.3)
        necks = tdl.neck_tracks(kept, ["td", None], set(),
                                lambda i, p: wo(i, p, kept), None, grow)
        return kept, necks, asked

    kept, necks, asked = run(0.34)
    assert asked == [0]
    assert kept[0]["size"] == 0.34 and kept[1] == _tr((0, 0), (1, 0))
    assert necks == [{"via": [0.0, 0.0], "net": "A", "layer": "F.Cu",
                      "width": 0.3, "was": 0.3, "length": 0.0, "ops": [],
                      "grown": [0.254, 0.34]}]
    assert tdl.neck_lengths(necks, kept, set())[0]["grown"] == [0.254, 0.34]

    kept, necks, asked = run(None)
    assert asked == [0] and necks == []
    assert kept == [_via(), _tr((0, 0), (1, 0))]


def test_a_grown_via_that_still_gets_no_drop_keeps_its_size():
    """The via grows, but its only track ends 0.1 mm off the centre: at
    0.34 mm it grazes (a 0.275 mm chord under 0.3) and no width under its
    own is over the floor, so no drop comes of it: the via goes back to
    its size and there is no entry."""
    kept = [_via(), _tr((0, 0.1), (1, 0.1))]
    wo = _floor_widths(0.3)
    necks = tdl.neck_tracks(kept, ["td", None], set(),
                            lambda i, p: wo(i, p, kept), None,
                            lambda i, p: 0.34)
    assert necks == [] and kept == [_via(), _tr((0, 0.1), (1, 0.1))]


def test_a_via_grows_once_and_then_necks_its_other_tracks():
    """Two tracks on the via: the first (0.3, the floor) takes its drop
    once the via grows to 0.34; the second (0.4) then necks to 0.3 at the
    grown size. Both entries carry the growth."""
    kept = [_via(), _tr((0, 0), (1, 0)), _tr((0, 0), (0, 1), w=0.4)]
    calls = []

    def grow(i, p):
        calls.append(i)
        return 0.34
    wo = _floor_widths(0.3)
    necks = tdl.neck_tracks(kept, ["td", None, None], set(),
                            lambda i, p: wo(i, p, kept), None, grow)
    assert calls == [0] and kept[0]["size"] == 0.34
    assert sorted((n["width"], n["was"], n["grown"]) for n in necks) == [
        (0.3, 0.3, [0.254, 0.34]), (0.3, 0.4, [0.254, 0.34])]
    assert kept[2]["width"] == 0.3 and kept[3]["width"] == 0.4


def _room_rules():
    return rf.Rules(clearance=0.1, hole_clearance=0.25, hole_to_hole=0.5,
                    edge_clearance=0.3, min_width=0.2, via_size=0.6,
                    via_drill=0.3, margin=0.02)


def _room_board(tracks=(), outline=None):
    return SimpleNamespace(
        copper_layers=["F.Cu", "B.Cu"], outline=outline,
        tracks_of=lambda: list(tracks), vias_of=lambda: [],
        pads_of=lambda: [])


def _btrack(net, y, uuid="t", layer="F.Cu", w=0.2):
    s = LineString([(-1, y), (1, y)])
    return SimpleNamespace(net=net, layer=layer, uuid=uuid, width=w,
                           poly=s.buffer(w / 2))


def test_via_room_planted_other_net_copper_and_quiet_twins():
    """A via at 0 growing from 0.254 to 0.34 mm needs 0.17 + 0.12 mm to an
    other net's copper edge. Planted: a B track whose edge is 0.27 mm off
    (fine at 0.254, not at 0.34), on the board or as an add of the run,
    and the board edge 0.4 mm off. Quiet: the same track of the via's own
    net, one the run takes off (going) or on no layer of a blind via."""
    rules, v = _room_rules(), _via()
    # 0.27 mm from the centre to the track's edge, which is 0.1 mm wide
    near = _btrack("B", 0.37)
    assert pr._via_room(v, 0.254, rules, _room_board([near]), [v], 0,
                        set(), {})
    assert not pr._via_room(v, 0.34, rules, _room_board([near]), [v], 0,
                            set(), {})
    assert pr._via_room(v, 0.34, rules, _room_board([_btrack("A", 0.37)]),
                        [v], 0, set(), {})
    assert pr._via_room(v, 0.34, rules, _room_board([near]), [v], 0,
                        {"t"}, {})
    blind = dict(v, top="B.Cu", bot="B.Cu")
    assert pr._via_room(blind, 0.34, rules, _room_board([near]), [blind],
                        0, set(), {})
    add = _tr((-1, 0.37), (1, 0.37), w=0.2, net="B")
    assert not pr._via_room(v, 0.34, rules, _room_board(), [v, add], 0,
                            set(), {})
    assert pr._via_room(v, 0.34, rules, _room_board(), [v, dict(
        add, net="A")], 0, set(), {})
    from shapely.geometry import box
    assert not pr._via_room(v, 0.34, rules, _room_board(
        outline=box(-5, -0.4, 5, 5)), [v], 0, set(), {})
    assert pr._via_room(v, 0.34, rules, _room_board(
        outline=box(-5, -5, 5, 5)), [v], 0, set(), {})


def _live_board(tmp_path, groups):
    """A board with one 0.6 mm via (teardrops on) per group at (5 + 8k, 5)
    and the group's add_track ops (relative to the via) on F.Cu."""
    nets = "".join(f' (net {k + 1} "N{k}")' for k in range(len(groups)))
    body = ""
    for k, ops in enumerate(groups):
        ox = 5 + 8 * k
        body += (f'  (via (at {ox} 5) (size 0.6) (drill 0.3)'
                 f' (layers "F.Cu" "B.Cu") (net "N{k}") (uuid "v{k}")'
                 f'{TD_ON})\n')
        for j, o in enumerate(ops):
            (ax, ay), (bx, by) = o["start"], o["end"]
            body += (f'  (segment (start {ox + ax:.4f} {5 + ay:.4f})'
                     f' (end {ox + bx:.4f} {5 + by:.4f}) (width {o["width"]})'
                     f' (layer "F.Cu") (net "N{k}") (uuid "t{k}_{j}"))\n')
    p = tmp_path / "live.kicad_pcb"
    p.write_text(HDR.replace('(net 1 "GND") (net 2 "+3V3")', nets)
                 + body + ")\n", encoding="utf-8")
    return p


def test_live_a_neck_short_of_the_drop_keeps_it_and_a_branch_necks_alone(
        tmp_path):
    """#1320 point 3, pinned on KiCad 10.0.6 (a 0.6 mm via, drops under
    0.54 mm). A neck that stops short of where the drop ends, at a branch
    or before a wider track, keeps the drop (KiCad lays it on over the
    wider copper): one drop each, and neck_ops leaves them be. A narrow
    stub wholly inside the via that branches into wide tracks (planted)
    gets no drop; neck_ops necks each branch as a join of its own, and
    then KiCad makes the two drops. The same board with the branches
    left wide (quiet twin) still has none."""
    import env
    import kc
    if env.find_kicad_cli() is None:
        pytest.skip("kicad-cli not found")
    via = dict(_via(uuid="v"), size=0.6, drill=0.3)
    short_branch = [_tr((0, 0), (0.35, 0), w=0.2),
                    _tr((0.35, 0), (2, 1), w=0.6),
                    _tr((0.35, 0), (2, -1), w=0.6)]
    short_wide = [_tr((0, 0), (0.35, 0), w=0.2),
                  _tr((0.35, 0), (3, 0), w=0.6)]
    inside = [_tr((0, 0), (0.1, 0), w=0.2), _tr((0.1, 0), (2, 1), w=0.6),
              _tr((0.1, 0), (2, -1), w=0.6)]
    groups = [[dict(o) for o in g] for g in (short_branch, short_wide,
                                              inside, inside)]
    assert tdl.neck_ops(groups[0], [via], lambda v, p: [0.2]) == []
    assert tdl.neck_ops(groups[1], [via], lambda v, p: [0.2]) == []
    necks = tdl.neck_ops(groups[2], [via], lambda v, p: [0.2])
    assert [(n["width"], n["was"]) for n in necks] == [(0.2, 0.6)]
    assert [o["width"] for o in groups[2]] == [0.2, 0.2, 0.2, 0.6, 0.6]
    pcb = _live_board(tmp_path, groups)
    kc.run_drc(env.find_kicad_cli(), pcb, refill=True, save_board=True)
    drops = {it.net: sum(it.drops.values())
             for it in tdl.read(pcb).items if it.kind == "via"}
    assert drops == {"N0": 1, "N1": 1, "N2": 2, "N3": 0}


def test_report_names_a_grown_via_on_its_neck_line_only():
    """REPORT.md's Necks line says the via grew, old size to new, only on
    an entry that carries grown; a plain neck line says nothing of it."""
    neck = {"net": "A", "layer": "F.Cu", "via": (1.0, 2.0), "was": 0.3,
            "width": 0.2, "length": 0.4}
    p = {"status": "ok", "board": "b.kicad_pcb", "refs": ["U1"], "ops": 1,
         "applied": True,
         "necks": [dict(neck, grown=[0.45, 0.5]), dict(neck, net="B")]}
    md = pr.report_md(p)
    assert "- A on F.Cu at (1.000, 2.000): 0.300 mm to 0.200 mm for" \
        " 0.400 mm (via grown 0.450 to 0.500 mm)" in md
    assert "- B on F.Cu at (1.000, 2.000): 0.300 mm to 0.200 mm for" \
        " 0.400 mm\n" in md
    assert md.count("via grown") == 1
