"""route_cleanup pass 3 (off-angle snap) on small synthetic boards.

Pure tests drive find_snaps with a hand-built SnapEnv (no board file); the
CLI tests run route_cleanup --dry-run on a written .kicad_pcb. No toolchain.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_route_style as crs  # noqa: E402
import route_cleanup as rcl  # noqa: E402

from test_route_cleanup import PD, S, V, _fp, _pad, _seg  # noqa: E402

# the one off-angle segment most tests snap: 4 x 1 mm, 14 deg off the grid.
# Its doglegs bend at (1, 1) (diagonal_first) and (3, 0) (axis_first).
A, B = (0.0, 0.0), (4.0, 1.0)


def _env(*obstacles, clearance=0.2, hard=None, hard_floor=0.0, layer="F.Cu"):
    """A SnapEnv with no outline: obstacles are (net, geometry) on `layer`."""
    obs = {layer: [(n, g, f"o{i}") for i, (n, g) in enumerate(obstacles)]}
    return rcl.SnapEnv(obs, {}, clearance, None, None, (), hard, hard_floor)


def _blob(x, y, net="F", r=0.1):
    return (net, Point(x, y).buffer(r))


def _snap(segs, env, vias=(), pads=(), arcs=(), skip=frozenset()):
    return rcl.find_snaps(list(segs), list(vias), list(pads), list(arcs), env,
                          skip)


def _tracks(ops):
    return [o for o in ops if o["op"] == "add_track"]


def _chain_of(ops):
    """The added legs as one ordered point chain."""
    pts = [tuple(_tracks(ops)[0]["start"])]
    for o in _tracks(ops):
        assert tuple(o["start"]) == pts[-1], ops
        pts.append(tuple(o["end"]))
    return pts


# ============================================================ routes

def test_snap_paths_geometry():
    got = dict(rcl.snap_paths(A, B))
    assert got == {"diagonal_first": [(1.0, 1.0)], "axis_first": [(3.0, 0.0)],
                   "axis_z": [(1.5, 0.0), (2.5, 1.0)],
                   "diagonal_z": [(0.5, 0.5), (3.5, 0.5)]}
    for pts in got.values():
        path = [A, *pts, B]
        assert not any(crs.off_angle(p, q) for p, q in zip(path, path[1:]))


def test_dogleg_order_chosen_by_clearance():
    seg = S("s1", A, B)
    # nothing near: the diagonal-first dogleg (first of a tie)
    ops, snaps, left, _ = _snap([seg], _env())
    assert [s["bend"] for s in snaps] == ["diagonal_first"] and not left
    # a foreign blob at the (1, 1) bend pushes it to axis-first ...
    ops, snaps, left, _ = _snap([seg], _env(_blob(1.0, 1.3)))
    assert snaps[0]["bend"] == "axis_first"
    assert _chain_of(ops) == [A, (3.0, 0.0), B]
    # ... and one at the (3, 0) bend back to diagonal-first
    ops, snaps, left, _ = _snap([seg], _env(_blob(3.0, -0.3)))
    assert snaps[0]["bend"] == "diagonal_first"
    assert _chain_of(ops) == [A, (1.0, 1.0), B]


def test_z_route_when_both_doglegs_block():
    env = _env(_blob(1.0, 1.3), _blob(3.0, -0.3))
    ops, snaps, left, _ = _snap([S("s1", A, B)], env)
    assert not left
    assert snaps[0]["bend"] in ("axis_z", "diagonal_z")
    chain = _chain_of(ops)
    assert len(chain) == 4 and chain[0] == A and chain[-1] == B
    assert not any(crs.off_angle(p, q) for p, q in zip(chain, chain[1:]))


def test_blocked_segment_is_left_with_reason():
    # every dogleg and Z bend has foreign copper beside it
    env = _env(_blob(1.0, 1.3, "X"), _blob(3.0, -0.3, "X"),
               _blob(1.5, -0.3, "X"), _blob(3.5, 0.2, "X"))
    ops, snaps, left, merged = _snap([S("s1", A, B)], env)
    assert ops == [] and snaps == [] and merged == 0
    assert [(x["uuid"], x["reason"], x["by"]) for x in left] == \
        [("s1", "blocked", "X")]


def test_on_grid_segments_untouched():
    segs = [S("h", (0, 0), (3, 0)), S("d", (3, 0), (5, 2)),
            S("v", (5, 2), (5, 6)), S("tiny", (5, 6), (5.03, 6.02))]
    assert _snap(segs, _env()) == ([], [], [], 0)


def test_legs_keep_endpoints_width_layer_net():
    seg = S("s1", (2, 2), (9, 4), net="VBUS", layer="B.Cu", width=0.5)
    ops, snaps, _, _ = _snap([seg], _env(layer="B.Cu"))
    assert ops[0] == {"op": "remove", "uuid": "s1"}
    legs = _tracks(ops)
    assert len(legs) == 2
    assert {(o["width"], o["layer"], o["net"]) for o in legs} == \
        {(0.5, "B.Cu", "VBUS")}
    chain = _chain_of(ops)
    assert chain[0] == (2.0, 2.0) and chain[-1] == (9.0, 4.0)
    assert snaps[0]["path"][0] == [2.0, 2.0] and snaps[0]["path"][-1] == [9.0, 4.0]


def test_same_net_copper_does_not_block_foreign_does():
    seg = S("s1", A, B)
    same = _env(("N", LineString([(0, 0.3), (4, 0.3)]).buffer(0.1)))
    assert _snap([seg], same)[1]
    # a foreign track across every route blocks
    wall = _env(("X", LineString([(2, -1), (2, 2)]).buffer(0.1)))
    assert _snap([seg], wall)[2][0]["reason"] == "blocked"


def test_replaced_foreign_segment_stops_blocking():
    """A foreign off-angle segment in the way is snapped first; its old
    copper (by uuid) no longer counts against the next segment."""
    other = S("x1", (0.0, 1.3), (4.0, 2.3), net="X")   # parallel, 0.3 above
    me = S("s1", A, B)
    obs = [("X", LineString([other.a, other.b]).buffer(0.125), "x1")]
    env = rcl.SnapEnv({"F.Cu": obs}, {}, 0.2)
    ops, snaps, left, _ = _snap([other, me], env)
    assert not left
    assert {s["uuid"] for s in snaps} == {"x1", "s1"}


# ============================================================ tee split

def _joint_pieces(extra_segs=(), vias=(), pads=()):
    seg = S("s1", (0, 0), (8, 2))
    ops, snaps, left, _ = _snap([seg, *extra_segs], _env(), vias=vias,
                                pads=pads)
    assert not left
    return _chain_of(ops), snaps[0]


def test_tee_split_at_track_end():
    chain, snap = _joint_pieces([S("t", (4, 1), (4, 5))])
    assert (4.0, 1.0) in chain and "+" in snap["bend"]
    assert chain[0] == (0.0, 0.0) and chain[-1] == (8.0, 2.0)


def test_tee_split_at_via_centre():
    chain, _ = _joint_pieces(vias=[V("v", (4, 1))])
    assert (4.0, 1.0) in chain


def test_tee_split_at_pad_centre():
    chain, _ = _joint_pieces(pads=[PD((4, 1))])
    assert (4.0, 1.0) in chain


def test_overlap_without_anchor_does_not_split():
    seg = S("s1", (0, 0), (8, 2))
    crossing = S("t", (4, -1), (4, 3))          # crosses, no end on s1
    pad = PD((4, 1.6))                           # copper reaches s1, centre off
    assert rcl._tee_joints(seg, [seg, crossing], [], [pad], ()) == []
    chain, snap = _joint_pieces([crossing], pads=[pad])
    assert "+" not in snap["bend"] and len(chain) == 3


def test_joint_at_end_cap_is_not_a_tee():
    seg = S("s1", (0, 0), (8, 2))
    near_end = S("t", (0.05, 0.01), (0.05, 3))   # inside s1's end cap
    assert rcl._tee_joints(seg, [seg, near_end], [], [], ()) == []


# ============================================================ skips

def test_skip_nets_are_left_kept():
    ops, snaps, left, _ = _snap([S("s1", A, B, net="RF")], _env(),
                                skip=frozenset({"RF"}))
    assert ops == [] and snaps == []
    assert [(x["uuid"], x["reason"]) for x in left] == [("s1", "kept_net")]


# ============================================================ clearance

def test_legal_truth_table():
    # full clearance: always legal
    assert rcl._legal(0.0, 0.0, {"G": 0.0}, {"G": -1.0})
    # under the netclass, no closer than before, meets the DRC rule: legal
    assert rcl._legal(-0.05, 0.02, {"G": -0.05}, {"G": -0.05})
    # closer than the replaced copper, even though DRC would pass: refused
    assert not rcl._legal(-0.06, 0.01, {"G": -0.06}, {"G": -0.05})
    # no closer than before, but under what DRC enforces: refused
    assert not rcl._legal(-0.05, -0.001, {"G": -0.05}, {"G": -0.05})


def test_legal_judges_each_net_against_its_own_before():
    # G was at -0.05 and stays there; H was at -0.01 and comes to -0.04.
    # The worst gap did not grow, but the route closes in on H: refused.
    before = {"G": -0.05, "H": -0.01}
    assert not rcl._legal(-0.05, 0.01, {"G": -0.05, "H": -0.04}, before)
    assert rcl._legal(-0.05, 0.01, {"G": -0.05, "H": -0.01}, before)
    # a net the replaced copper was clear of may not go under the netclass
    assert not rcl._legal(-0.05, 0.01, {"G": -0.05, "K": -0.001},
                          {"G": -0.05})
    assert rcl._legal(-0.05, 0.01, {"G": -0.05, "K": 0.3}, {"G": -0.05})


def test_snap_refuses_closing_in_on_a_second_tight_net():
    """G sits 0.15 under A-B's start (under the 0.2 netclass, over a 0.127
    DRU rule); H is a blob well clear of A-B. diagonal_first keeps G as
    close as before but passes H at 0.17 (also under the netclass);
    axis_first keeps G as before and stays clear of H. Judged on the single
    worst gap both pass and diagonal_first wins the tie; per net only
    axis_first is legal."""
    env = _tight_env(hard={})
    n, g = _blob(2.0, 1.4, net="H")
    env.obstacles["F.Cu"].append((n, g, "h"))
    seg = S("s1", A, B)
    items = env.obstacles["F.Cu"]
    trees = {"F.Cu": (STRtree([g for _n, g, _u in items]), items)}
    _m, _w, _h, before = rcl._path_margin(seg, [], env, trees, [], set())
    m, _w, hard, per = rcl._path_margin(seg, [(1.0, 1.0)], env, trees, [],
                                        set())
    assert hard >= 0 and m >= min(before.values()) - 1e-6
    assert -0.05 < per["H"] < 0 and per["H"] < before.get("H", 0.0)
    assert not rcl._legal(m, hard, per, before)
    ops, snaps, left, _ = _snap([seg], env)
    assert snaps and snaps[0]["bend"] == "axis_first", (snaps, left)


def _tight_env(hard):
    """A foreign track 0.15 mm (edge to edge) below the start of A-B: under
    the 0.2 netclass, over a 0.127 unconditioned DRU rule."""
    wall = ("G", LineString([(-1, -0.4), (5, -0.4)]).buffer(0.125))
    return _env(wall, clearance=0.2, hard=hard, hard_floor=0.127)


def test_hard_clearance_fallback_accepts_route_no_closer():
    ops, snaps, left, _ = _snap([S("s1", A, B)], _tight_env(hard={}))
    assert not left and snaps
    assert snaps[0]["spare_mm"] < 0     # under the netclass, as before


def test_without_dru_rule_the_tight_segment_stays():
    ops, snaps, left, _ = _snap([S("s1", A, B)], _tight_env(hard=None))
    assert ops == [] and left[0]["reason"] == "blocked"


def test_hard_clearance_refuses_a_route_closer_than_before():
    # second wall the axis-first leg (y = 0) would pass at 0.14 mm: still
    # over 0.127, but closer than the replaced copper's 0.15 -> refused
    env = _tight_env(hard={})
    env.obstacles["F.Cu"].append(
        ("G", LineString([(2.5, -0.39), (3.5, -0.39)]).buffer(0.125), "w2"))
    seg = S("s1", A, B)
    items = env.obstacles["F.Cu"]
    trees = {"F.Cu": (STRtree([g for _n, g, _u in items]), items)}
    before = rcl._path_margin(seg, [], env, trees, [], set())[3]
    m, _why, hard, per = rcl._path_margin(seg, [(3.0, 0.0)], env, trees, [],
                                          set())
    assert hard >= 0 and m < before["G"]
    assert not rcl._legal(m, hard, per, before)
    ops, snaps, left, _ = _snap([seg], env)
    assert snaps[0]["bend"] == "diagonal_first"


# ============================================================ jog merge

def test_merge_jogs_removes_a_snap_made_jog():
    """s1 -> off-angle s2 (0.1 mm sidestep) -> s3: either dogleg of s2 makes
    a needless jog, so the merge redraws s1 + the legs as one dogleg from
    the pad at s1's far end."""
    segs = [S("s1", (0, 0), (5, 0)), S("s2", (5, 0), (7, 0.1)),
            S("s3", (7, 0.1), (12, 0.1))]
    pads = [PD((0, 0), half=0.3), PD((12, 0.1), half=0.3)]
    ops, snaps, left, merged = _snap(segs, _env(), pads=pads)
    assert merged == 1 and not left
    removed = {o["uuid"] for o in ops if o["op"] == "remove"}
    assert removed == {"s1", "s2"}
    chain = _chain_of(ops)
    assert chain[0] == (0.0, 0.0) and chain[-1] == (7.0, 0.1)
    final = [(tuple(o["start"]), tuple(o["end"]), 0.25, str(i))
             for i, o in enumerate(_tracks(ops))]
    final.append(((7.0, 0.1), (12.0, 0.1), 0.25, "s3"))
    assert crs.find_jogs(final, [p.poly for p in pads]) == []


# ============================================================ CLI

def _board(tmp_path, body, nets):
    head = "\n".join(f'  (net {i} "{n}")' for i, n in enumerate(nets, 1))
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (net 0 "")
{head}
  (setup)
  (gr_rect (start 0 0) (end 60 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path / "snap.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


def _cli_board(tmp_path):
    nets = ["VCC", "USB_DP", "USB_DM", "SIG"]
    body = _fp("U1", 10, 10, pads=_pad("1", 0, 0, "VCC"))
    body += _seg("stub0001", (10, 10), (12, 10))            # dangling stub
    body += _seg("vcc00001", (20, 10), (24, 11))
    body += _seg("dp000001", (20, 20), (24, 21), net='(net "USB_DP")')
    body += _seg("dm000001", (20, 22), (24, 23), net='(net "USB_DM")')
    body += _seg("sig00001", (20, 30), (24, 31), net='(net "SIG")')
    return _board(tmp_path, body, nets)


def test_cli_snap_only_skips_hygiene_and_diff_pairs(tmp_path):
    p = _cli_board(tmp_path)
    raw = p.read_bytes()
    pay, _ = rcl.run(["--pcb", str(p), "--dry-run", "--snap-only"])
    assert p.read_bytes() == raw
    assert pay["dangling_removed"] == 0 and pay["corners_smoothed"] == 0
    assert {s["uuid"] for s in pay["snaps"]} == {"vcc00001", "sig00001"}
    assert {(x["uuid"], x["reason"]) for x in pay["off_angle_left"]} == \
        {("dp000001", "kept_net"), ("dm000001", "kept_net")}
    removed = {o["uuid"] for o in pay["ops"] if o["op"] == "remove"}
    assert removed == {"vcc00001", "sig00001"}


def test_cli_keep_net(tmp_path):
    p = _cli_board(tmp_path)
    pay, _ = rcl.run(["--pcb", str(p), "--dry-run", "--snap-only",
                      "--keep-net", "SIG"])
    assert {s["uuid"] for s in pay["snaps"]} == {"vcc00001"}
    assert ("sig00001", "kept_net") in {
        (x["uuid"], x["reason"]) for x in pay["off_angle_left"]}


def test_cli_declared_diff_pairs_replace_name_pairing(tmp_path):
    """A constraints diff_pairs list, even [], is the whole set: USB_DP/DM
    named like a pair are snapped like any net (speaker A_SPK_L+/- on the
    hi-fi core board were left off-angle and failed route_style)."""
    p = _cli_board(tmp_path)
    cons = tmp_path / "constraints.json"
    cons.write_text('{"diff_pairs": []}', encoding="utf-8")
    pay, _ = rcl.run(["--pcb", str(p), "--dry-run", "--snap-only",
                      "--constraints", str(cons)])
    assert {s["uuid"] for s in pay["snaps"]} == {
        "vcc00001", "dp000001", "dm000001", "sig00001"}
    # no diff_pairs key: name pairing still keeps them
    cons.write_text('{}', encoding="utf-8")
    pay, _ = rcl.run(["--pcb", str(p), "--dry-run", "--snap-only",
                      "--constraints", str(cons)])
    assert {s["uuid"] for s in pay["snaps"]} == {"vcc00001", "sig00001"}


def test_cli_no_snap_and_snap_only_exclusive(tmp_path):
    p = _cli_board(tmp_path)
    pay, _ = rcl.run(["--pcb", str(p), "--dry-run", "--no-snap"])
    assert pay["off_angle_snapped"] == 0 and pay["dangling_removed"] >= 1
    assert rcl.main(["--pcb", str(p), "--dry-run", "--snap-only",
                     "--no-snap"]) == 2


# ============================================================ hole clearance

def _drill_env(hole):
    """_tight_env plus an NPTH drill (r 0.3) whose edge is 0.35 mm above
    diagonal_first's y = 1 run: 0.22 mm spare edge to edge, over the 0.2
    netclass and the 0.127 DRU rule, under a 0.25 hole clearance."""
    env = _tight_env(hard={})
    env.hole = hole
    env.obstacles["F.Cu"].append(
        ("", Point(2.0, 1.65).buffer(0.3), rcl.DRILL))
    return env


def test_hard_check_keeps_hole_clearance_from_a_drill():
    env = _drill_env(0.25)
    seg = S("s1", A, B)
    items = env.obstacles["F.Cu"]
    trees = {"F.Cu": (STRtree([g for _n, g, _u in items]), items)}
    _m, _w, hard, per = rcl._path_margin(seg, [(1.0, 1.0)], env, trees, [],
                                         set())
    assert hard < 0 and per["<no net>"] < 0
    ops, snaps, left, _ = _snap([seg], env)
    assert snaps[0]["bend"] == "axis_first"
    # the same drill with no hole rule does not stop diagonal_first
    ops, snaps, left, _ = _snap([seg], _drill_env(0.0))
    assert snaps[0]["bend"] == "diagonal_first"


def test_snap_env_tags_drills_and_reads_hole_clearance(tmp_path):
    body = ('  (footprint "t:H1" (layer "F.Cu") (at 30 20)\n'
            '    (property "Reference" "H1" (at 0 0 0))\n'
            '    (pad "" np_thru_hole circle (at 0 0) (size 1 1) (drill 1)'
            ' (layers "*.Cu" "*.Mask")))\n'
            '  (via (at 40 20) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
            ' (net "VCC") (uuid "via00001"))\n')
    p = _board(tmp_path, body, ["VCC"])
    p.with_suffix(".kicad_pro").write_text(
        '{"board": {"design_settings": {"rules": '
        '{"min_hole_clearance": 0.3}}}}', encoding="utf-8")
    env = rcl.snap_env(rcl.geom.BoardGeom.from_file(p))
    drills = [(n, round(g.centroid.x, 3)) for n, g, u in env.obstacles["F.Cu"]
              if u == rcl.DRILL]
    assert sorted(drills) == [("", 30.0), ("VCC", 40.0)]
    assert env.obstacles["B.Cu"] and env.hole == 0.3
    p.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "h"\n\t(constraint hole_clearance (min 0.35mm))\n'
        ')\n', encoding="utf-8")
    assert rcl.snap_env(rcl.geom.BoardGeom.from_file(p)).hole == 0.35


# ============================================================ DRU fallback

def _dru_rules(tmp_path, *rules, scoped=True, pro_floor=None):
    pcb = tmp_path / "r.kicad_pcb"
    text = "(version 1)\n" + "".join(
        f'(rule "r{i}"\n\t(constraint clearance (min {mm}mm))\n'
        + (f'\t(condition "{cond}")\n' if cond else "") + ")\n"
        for i, (mm, cond) in enumerate(rules))
    pcb.with_suffix(".kicad_dru").write_text(text, encoding="utf-8")
    if pro_floor is not None:  # the netclass floor, else KiCad's 0.2 mm
        pcb.with_suffix(".kicad_pro").write_text(json.dumps(
            {"net_settings": {"classes": [
                {"name": "Default", "clearance": pro_floor}]}}),
            encoding="utf-8")
    return rcl._rules_clearance(pcb, ["VCC", "SIG"], scoped=scoped)


def test_unconditioned_dru_clearance_enables_fallback(tmp_path):
    _pn, _f, _e, hard, hard_floor, hole = _dru_rules(tmp_path, (0.127, None))
    assert hard == {} and hard_floor == 0.127 and hole == 0.25


def test_conditioned_clearance_rule_takes_the_maximum(tmp_path):
    # PCB-0020-A's shape: the floor rule plus a same-footprint rule at the
    # floor leaves the fallback at the floor
    got = _dru_rules(tmp_path, (0.127, None), (
        0.127, "A.memberOfFootprint('J1') && B.memberOfFootprint('J1')"))
    assert got[3] == {} and got[4] == 0.127
    # a stricter conditioned rule raises it everywhere
    got = _dru_rules(tmp_path, (0.127, None), (0.3, "A.NetName == 'VCC'"))
    assert got[3] == {} and got[4] == 0.3
    # a weaker one changes nothing
    got = _dru_rules(tmp_path, (0.2, None), (0.1, "A.NetName == 'VCC'"))
    assert got[4] == 0.2
    # only a conditioned rule still counts
    got = _dru_rules(tmp_path, (0.15, "A.NetName == 'VCC'"))
    assert got[3] == {} and got[4] == 0.15


def test_unscoped_floor_leaves_footprint_rules_out(tmp_path):
    # the voice board's shape (#1146): a floor, package ratings held to
    # J801's pads and U702's courtyard, a BGA escape rule at U301, and a
    # via rule that holds anywhere
    rules = ((0.0762, None),
             (0.19, "A.memberOfFootprint('J801') && "
                    "B.memberOfFootprint('J801')"),
             (0.14, "A.intersectsCourtyard('U702') && "
                    "B.intersectsCourtyard('U702') && "
                    "A.Type != 'Zone' && B.Type != 'Zone'"),
             (0.0889, "A.intersectsCourtyard('U301') || "
                      "B.intersectsCourtyard('U301')"),
             (0.1, "A.Type == 'via'"))
    got = _dru_rules(tmp_path, *rules, pro_floor=0.0762)
    assert got[1] == 0.19 and got[4] == 0.19
    got = _dru_rules(tmp_path, *rules, pro_floor=0.0762, scoped=False)
    # the floor drops to the via rule; the hard fallback stays the maximum
    assert got[1] == 0.1 and got[4] == 0.19


def test_no_clearance_rule_keeps_fallback_off(tmp_path):
    pcb = tmp_path / "n.kicad_pcb"
    pcb.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "h"\n\t(constraint hole_clearance '
        '(min 0.3mm))\n)\n', encoding="utf-8")
    assert rcl._rules_clearance(pcb, ["VCC"])[3] is None


# ============================================================ constraints

def _cons_board(tmp_path):
    nets = ["FOO_A", "FOO_B", "LM1", "HS1", "HS2"]
    body = ""
    for i, n in enumerate(nets):
        y = 5 + 5 * i
        body += _seg(f"s{i:07d}", (20, y), (24, y + 1), net=f'(net "{n}")')
    return _board(tmp_path, body, nets)


def test_constraints_keep_declared_pair_and_matched_nets(tmp_path):
    p = _cons_board(tmp_path)
    pay, _ = rcl.run(["--pcb", str(p), "--dry-run", "--snap-only"])
    # FOO_A/FOO_B is no pair by name: without constraints all five snap
    assert pay["off_angle_snapped"] == 5
    cons = tmp_path / "constraints.json"
    cons.write_text(json.dumps({
        "diff_pairs": [{"p": "FOO_A", "n": "FOO_B"}],
        "length_match": [{"nets": ["LM1"], "tol_mm": 1.0}],
        "high_speed": [{"net": "HS1", "impedance_ohm": 50},
                       {"net": "HS2", "reference": "GND"}]}),
        encoding="utf-8")
    pay, _ = rcl.run(["--pcb", str(p), "--dry-run", "--snap-only",
                      "--constraints", str(cons)])
    kept = {x["net"] for x in pay["off_angle_left"]
            if x["reason"] == "kept_net"}
    assert kept == {"FOO_A", "FOO_B", "LM1", "HS1"}
    assert {s["net"] for s in pay["snaps"]} == {"HS2"}


def test_constraint_keep_nets_reads_every_key():
    got = rcl.constraint_keep_nets({
        "diff_pairs": [{"p": "P", "n": "N"}, {"p": "X"}],
        "length_match": [{"nets": ["L1", "L2"]}, "junk"],
        "rf": [{"net": "ANT"}],
        "high_speed": [{"net": "Z", "impedance_ohm": 90}, {"net": "Q"}]})
    assert got == {"P", "N", "X", "L1", "L2", "ANT", "Z"}


def test_constraint_keep_nets_length_groups_globs():
    cons = {"length_groups": [{"nets": ["DRAM_D[0-1]"], "ref_net": "/DQS",
                               "not_longer_than": ["CK*"], "tol_mm": 1}]}
    assert rcl.constraint_keep_nets(cons) == {"DRAM_D[0-1]", "/DQS", "CK*"}
    board = ["/DRAM_D0", "/DRAM_D1", "/DRAM_D2", "/DQS", "/CK_P", "/CK_N"]
    assert rcl.constraint_keep_nets(cons, board) == {
        "/DRAM_D0", "/DRAM_D1", "/DQS", "/CK_P", "/CK_N"}


def test_constraints_unreadable_is_an_error(tmp_path):
    p = _cons_board(tmp_path)
    assert rcl.main(["--pcb", str(p), "--dry-run", "--snap-only",
                     "--constraints", str(tmp_path / "none.json")]) == 2


def test_dejog_merges_a_router_jog_with_no_snap():
    """A grid router's jog (all three on the 45 grid, no snapped leg) is
    left by plain pass 3 and merged by dejog; a kept net's is left."""
    segs = [S("s1", (0, 0), (5, 0)), S("s2", (5, 0), (5.1, 0.1)),
            S("s3", (5.1, 0.1), (12, 0.1))]
    pads = [PD((0, 0), half=0.3), PD((12, 0.1), half=0.3)]
    assert _snap(segs, _env(), pads=pads)[3] == 0
    ops, snaps, left, merged = rcl.find_snaps(segs, [], pads, [], _env(),
                                              dejog=True)
    assert merged == 1 and not snaps and not left
    assert {o["uuid"] for o in ops if o["op"] == "remove"} == {"s1", "s2",
                                                               "s3"}
    final = [(tuple(o["start"]), tuple(o["end"]), 0.25, str(i))
             for i, o in enumerate(_tracks(ops))]
    assert final[0][0] == (0.0, 0.0) and final[-1][1] == (12.0, 0.1)
    assert crs.find_jogs(final, [p.poly for p in pads]) == []
    net = segs[0].net
    assert rcl.find_snaps(segs, [], pads, [], _env(), frozenset({net}),
                          dejog=True)[3] == 0


def test_dejog_leaves_a_jog_round_an_obstacle():
    """Foreign copper where the straight route would run: no merge."""
    segs = [S("s1", (0, 0), (5, 0)), S("s2", (5, 0), (5.1, 0.1)),
            S("s3", (5.1, 0.1), (12, 0.1))]
    pads = [PD((0, 0), half=0.3), PD((12, 0.1), half=0.3)]
    env = _env(_blob(8, -0.35, r=0.1))
    assert rcl.find_snaps(segs, [], pads, [], env, dejog=True)[3] == 0
