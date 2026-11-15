"""S11 route_cleanup tests: dangling removal, loop breaking, corner smoothing.

Pure tests exercise the graph/cycle/chamfer math on synthetic segment sets
(no toolchain). The smoke fixture builds a placed-unrouted blinky2
(board_init + place_seed --apply), injects dirt via route_edit.apply_ops
(a dangling GND stub, a pure 4-segment track loop anchored to real pads, a
90-deg corner between two same-net pads in clear space) and runs the real
cleanup end to end.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GOLDEN = REPO / "tests" / "golden"
S7 = REPO / "tests" / "s7_regen"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import env  # noqa: E402
import geom  # noqa: E402
import route_cleanup as rcl  # noqa: E402


def EMPTY(net, layer):
    return Polygon()


def S(uuid, a, b, net="N", layer="F.Cu", width=0.25):
    return rcl.Seg(uuid, net, layer, width, tuple(a), tuple(b))


def V(uuid, at, net="N", layers=("F.Cu", "B.Cu"), size=0.6, drill=0.3):
    return rcl.ViaItem(uuid, net, tuple(at), size, drill, tuple(layers))


def PD(center, net="N", layers=("F.Cu",), half=0.5):
    x, y = center
    return rcl.PadItem(net, tuple(layers), (x, y),
                       box(x - half, y - half, x + half, y + half))


# ============================================================ pure: helpers

def _plan(payload):
    """A dry-run payload as comparable JSON, without its wall-clock
    `generated_at`, which can tick over between two runs."""
    return json.dumps({k: v for k, v in payload.items() if k != "generated_at"},
                      sort_keys=True)


def test_pt_seg_dist():
    assert rcl._pt_seg_dist((0, 1), (0, 0), (2, 0)) == pytest.approx(1.0)
    assert rcl._pt_seg_dist((3, 0), (0, 0), (2, 0)) == pytest.approx(1.0)
    assert rcl._pt_seg_dist((1, 0), (0, 0), (2, 0)) == pytest.approx(0.0)
    # degenerate zero-length segment
    assert rcl._pt_seg_dist((1, 1), (0, 0), (0, 0)) == pytest.approx(
        math.sqrt(2))


# ============================================================ pure: dangling

def test_dangling_stub_cascades_to_fixpoint():
    pads = [PD((0, 0))]
    segs = [S("s1", (0, 0), (2, 0)), S("s2", (2, 0), (4, 0))]
    gone_s, gone_v = rcl.find_dangling(segs, [], pads, EMPTY)
    # s2's far end is free -> removed; that exposes s1 -> removed next round
    assert gone_s == ["s2", "s1"]
    assert gone_v == []


def test_dangling_fill_termination_is_kept():
    pads = [PD((0, 0))]
    segs = [S("s1", (0, 0), (2, 0))]

    def fill(net, layer):
        return box(1.5, -1, 3, 1) if (net, layer) == ("N", "F.Cu") \
            else Polygon()

    gone_s, gone_v = rcl.find_dangling(segs, [], pads, fill)
    assert gone_s == [] and gone_v == []


def test_dangling_foreign_net_does_not_anchor():
    pads = [PD((0, 0), net="A")]
    segs = [S("sa", (0, 0), (2, 0), net="A"),
            S("sb", (2, 0), (4, 0), net="B")]
    gone_s, _ = rcl.find_dangling(segs, [], pads, EMPTY)
    assert sorted(gone_s) == ["sa", "sb"]


def test_dangling_other_layer_does_not_anchor():
    pads = [PD((0, 0))]
    segs = [S("sf", (0, 0), (2, 0), layer="F.Cu"),
            S("sb", (2, 0), (4, 0), layer="B.Cu")]
    gone_s, _ = rcl.find_dangling(segs, [], pads, EMPTY)
    assert sorted(gone_s) == ["sb", "sf"]  # both free without a via


def test_via_joining_two_track_layers_kept():
    pads = [PD((0, 0), layers=("F.Cu",)), PD((4, 0), layers=("B.Cu",))]
    segs = [S("sf", (0, 0), (2, 0), layer="F.Cu"),
            S("sb", (2, 0), (4, 0), layer="B.Cu")]
    vias = [V("v1", (2, 0))]
    gone_s, gone_v = rcl.find_dangling(segs, vias, pads, EMPTY)
    assert gone_s == [] and gone_v == []


def test_stacked_vias_joining_two_track_layers_kept():
    # HDI stack: F.Cu track, F-In1 and In1-In2 micro vias, In2 track
    pads = [PD((0, 0), layers=("F.Cu",)), PD((4, 0), layers=("In2.Cu",))]
    segs = [S("sf", (0, 0), (2, 0), layer="F.Cu"),
            S("si", (2, 0), (4, 0), layer="In2.Cu")]
    vias = [V("v1", (2, 0), layers=("F.Cu", "In1.Cu")),
            V("v2", (2, 0), layers=("In1.Cu", "In2.Cu"))]
    gone_s, gone_v = rcl.find_dangling(segs, vias, pads, EMPTY)
    assert gone_s == [] and gone_v == []


def test_stack_with_a_free_end_is_removed():
    pads = [PD((0, 0), layers=("F.Cu",))]
    segs = [S("sf", (0, 0), (2, 0), layer="F.Cu")]
    vias = [V("v1", (2, 0), layers=("F.Cu", "In1.Cu")),
            V("v2", (2, 0), layers=("In1.Cu", "In2.Cu"))]
    gone_s, gone_v = rcl.find_dangling(segs, vias, pads, EMPTY)
    assert sorted(gone_v) == ["v1", "v2"] and gone_s == ["sf"]


def test_via_with_single_track_and_no_fill_removed():
    pads = [PD((0, 0))]
    segs = [S("s1", (0, 0), (2, 0))]
    vias = [V("v9", (2, 0))]
    gone_s, gone_v = rcl.find_dangling(segs, vias, pads, EMPTY)
    assert gone_v == ["v9"]
    assert gone_s == ["s1"]  # the stub follows once its via is gone


def test_via_in_same_net_fill_kept():
    pads = [PD((0, 0))]
    segs = [S("s1", (0, 0), (2, 0))]
    vias = [V("v9", (2, 0))]

    def fill(net, layer):
        return box(1, -1, 3, 1) if (net, layer) == ("N", "B.Cu") \
            else Polygon()

    gone_s, gone_v = rcl.find_dangling(segs, vias, pads, fill)
    assert gone_s == [] and gone_v == []


def test_isolated_via_removed():
    gone_s, gone_v = rcl.find_dangling([], [V("vx", (9, 9))], [], EMPTY)
    assert gone_v == ["vx"] and gone_s == []


def test_uuidless_items_are_anchors_never_removed():
    pads = [PD((0, 0))]
    segs = [S("", (0, 0), (2, 0))]  # dangling but not removable
    gone_s, gone_v = rcl.find_dangling(segs, [], pads, EMPTY)
    assert gone_s == [] and gone_v == []


# ============================================================ pure: V13 fix

def test_endpoint_inside_wide_trunk_copper_is_not_free():
    """T6/V13 root cause: a stub ending 1.0 mm from a 3.0 mm trunk's
    CENTERLINE sits 0.5 mm inside its copper - connected, never dangling."""
    trunk = S("trunk", (0, 0), (30, 0), width=3.0)
    stub = S("stub", (5, 1.0), (5, 4.0), width=0.3)
    # trunk anchored at both ends, stub far end on a pad
    pads = [PD((0, 0)), PD((30, 0)), PD((5, 4.0))]
    gone_s, _ = rcl.find_dangling([trunk, stub], [], pads, EMPTY)
    assert gone_s == []
    # control: pull the stub past the copper + clearance -> genuinely free
    stub2 = S("stub2", (5, 2.0), (5, 5.0), width=0.3)
    gone_s2, _ = rcl.find_dangling([trunk, stub2], [], pads[:2], EMPTY)
    assert gone_s2 == ["stub2"]


def test_via_and_pad_touch_use_copper_overlap():
    # via barrel (r 0.3) + track half width (0.125): centre distance 0.4
    # overlaps copper -> anchored; the old centerline rule called it free.
    # A B.Cu pad under the via gives it its 2nd contact (via stays).
    pads = [PD((0, 0)), PD((2.4, 0), layers=("B.Cu",))]
    segs = [S("s1", (0, 0), (2, 0), width=0.25)]
    vias = [V("v1", (2.4, 0))]
    gone_s, gone_v = rcl.find_dangling(segs, vias, pads, EMPTY)
    assert gone_s == [] and gone_v == []
    # pad copper within half-width of the endpoint anchors too
    pads2 = [PD((0, 0)), PD((2.6, 0), half=0.5)]   # pad edge at x=2.1
    gone_s2, _ = rcl.find_dangling(segs, [], pads2, EMPTY)
    assert gone_s2 == []


# ============================================================ pure: both ends

def test_short_spur_across_a_track_dangles():
    """KiCad 10 counts a track that touches both ends of another for one
    end only, so a spur shorter than the track is wide dangles though both
    its ends lie in copper (DRC's track_dangling)."""
    pads = [PD((0, 0)), PD((4, 0))]
    segs = [S("s1", (0, 0), (2, 0)), S("s2", (2, 0), (4, 0)),
            S("s3", (2, 0), (2, 0.05))]
    gone_s, _ = rcl.find_dangling(segs, [], pads, EMPTY)
    assert gone_s == ["s3"]


def test_short_jog_between_two_tracks_is_kept():
    """A jog as short as that spur joins two tracks: each counts for the
    jog end it is nearer, so both ends hold."""
    pads = [PD((0, 0)), PD((4, 0.05))]
    segs = [S("s1", (0, 0), (2, 0)), S("s2", (2, 0), (2, 0.05)),
            S("s3", (2, 0.05), (4, 0.05))]
    gone_s, _ = rcl.find_dangling(segs, [], pads, EMPTY)
    assert gone_s == []


def test_spur_end_within_tol_of_the_track_copper_is_kept():
    """Out of the both-ends case a contact gets TOL more than KiCad gives:
    a spur whose far end misses the track's copper by under TOL stays (the
    safe side) and one that misses it by more goes."""
    pads = [PD((0, 0)), PD((4, 0))]
    base = [S("s1", (0, 0), (2, 0)), S("s2", (2, 0), (4, 0))]
    near = base + [S("s3", (2, 0), (2, 0.255))]
    assert rcl.find_dangling(near, [], pads, EMPTY)[0] == []
    far = base + [S("s3", (2, 0), (2, 0.27))]
    assert rcl.find_dangling(far, [], pads, EMPTY)[0] == ["s3"]


def test_stacked_short_spurs_cascade():
    """s4 holds s3's free end until s4 goes; then s3 goes too."""
    pads = [PD((0, 0)), PD((4, 0))]
    segs = [S("s1", (0, 0), (2, 0)), S("s2", (2, 0), (4, 0)),
            S("s3", (2, 0), (2, 0.05)), S("s4", (2, 0.05), (2, 0.1))]
    gone_s, _ = rcl.find_dangling(segs, [], pads, EMPTY)
    assert gone_s == ["s4", "s3"]


# T runs pad to pad; a short stub s has both ends on T's copper, so by
# KiCad's rule T holds only one of them. When s also carries other copper
# that does not touch T, s is the only link and must stay.
_T_PADS = [PD((0, 0)), PD((10, 0))]


def _t_track():
    return S("T", (0, 0), (10, 0), width=0.2)


def test_stub_bridging_track_to_pad_is_kept():
    """Pad P sits 0.1 mm clear of T's copper; s overlaps both. Dropping s
    would make P an island (review F1, case A)."""
    p = PD((4.9, 0.7))
    segs = [_t_track(), S("s", (4.9, 0.15), (5.05, 0), width=0.2)]
    assert not p.poly.intersects(_body_of(segs[0]))
    assert rcl.find_dangling(segs, [], _T_PADS + [p], EMPTY)[0] == []


def test_stub_bridging_track_to_track_is_kept():
    """Track U ends 0.18 mm clear of T's copper and overlaps s's end, so
    s is U's only link to T. Dropping s would cascade into U (review F1,
    case D)."""
    u_pad = rcl.PadItem("N", ("F.Cu",), (4.9, 3.2), box(4.4, 2.9, 5.4, 3.5))
    segs = [_t_track(), S("U", (4.9, 3.0), (4.9, 0.38), width=0.2),
            S("s", (4.9, 0.19), (5.09, 0), width=0.2)]
    assert rcl.find_dangling(segs, [], _T_PADS + [u_pad], EMPTY)[0] == []


def test_stub_bridging_track_to_fill_piece_is_kept():
    """The same holds when the other copper is one polygon of a fill that
    does not reach T; a stub with nothing but T still goes."""
    piece = box(4.4, 0.2, 5.4, 1.2)
    far = box(20, 20, 21, 21)

    def fill(net, layer):
        return MultiPolygon([piece, far]) if net == "N" else Polygon()

    segs = [_t_track(), S("s", (4.9, 0.15), (5.05, 0), width=0.2)]
    assert rcl.find_dangling(segs, [], _T_PADS, fill)[0] == []
    bare = [_t_track(), S("s", (4.9, 0.05), (5.05, 0), width=0.2)]
    assert rcl.find_dangling(bare, [], _T_PADS, EMPTY)[0] == ["s"]


def _body_of(seg):
    return LineString([seg.a, seg.b]).buffer(seg.width / 2.0)


def test_regression_pd_trigger_vbus_stubs_survive():
    """V13 regression fixture: on the frozen pd-trigger route board the
    dangling pass must remove NOTHING, and the two VBUS stubs the old
    centerline model cut (unconnected 0->2 live) must not be targeted by
    any remove op. No toolchain needed (build_plan is pure analysis)."""
    pcb = (REPO / "tests" / "fixtures" / "stages" / "pd_trigger" / "route"
           / "pd-trigger.kicad_pcb")
    bg = geom.BoardGeom.from_file(pcb)
    segs, vias = rcl.parse_items(pcb, bg.copper_layers)
    plan = rcl.build_plan(bg, segs, vias)
    assert plan["dangling_removed"] == 0
    stubs = {"01de587b-2d22-4335-9d71-8d473a2a3c84",
             "e0b8be12-082a-41f9-bafb-41ddd1208bc8"}
    removed = {o["uuid"] for o in plan["ops"] if o["op"] == "remove"}
    assert not (removed & stubs), removed & stubs
    # deterministic plan; the genuine loop still breaks, orphans follow
    plan2 = rcl.build_plan(bg, segs, vias)
    assert json.dumps(plan["ops"], sort_keys=True) == \
        json.dumps(plan2["ops"], sort_keys=True)
    assert plan["loops_broken"] == 1
    assert plan["orphaned_after_loops"] == 2
    assert plan["loop_bridge_vetoed"] == 0


def test_loop_bridge_veto_unmatched_victim_is_vetoed(tmp_path_factory):
    p = _dirty_synthetic(tmp_path_factory, "veto")
    bg = geom.BoardGeom.from_file(p)
    ghost = S("gh", (1.23, 4.56), (7.89, 1.23), net="VCC")
    removable, vetoed = rcl.loop_bridge_veto(bg, [ghost])
    assert removable == [] and vetoed == ["gh"]
    assert rcl.loop_bridge_veto(bg, []) == ([], [])


# ============================================================ pure: loops

RECT = [S("aaa", (0, 0), (6, 0), width=0.4),
        S("bbb", (6, 0), (6, 1), width=0.4),
        S("ccc", (6, 1), (0, 1), width=0.4),
        S("ddd", (0, 1), (0, 0), width=0.4)]


def test_loop_rectangle_breaks_longest():
    removed, loops = rcl.find_loops(RECT, [])
    assert loops == 1
    assert removed == ["ccc"]  # 6 mm ties broken by uuid, max wins


def test_loop_across_layers_through_vias():
    segs = [S("fff", (0, 0), (5, 0), layer="F.Cu"),
            S("zzz", (0, 0), (5, 0), layer="B.Cu")]
    vias = [V("v1", (0, 0)), V("v2", (5, 0))]
    removed, loops = rcl.find_loops(segs, vias)
    assert loops == 1
    assert removed == ["zzz"]  # equal length, uuid tie-break


def test_parallel_paths_to_plane_are_not_loops():
    # two stubs dropping to a plane through vias: the plane is not an edge
    segs = [S("f1", (0, 0), (2, 0)), S("f2", (0, 5), (2, 5))]
    vias = [V("va", (2, 0)), V("vb", (2, 5))]
    removed, loops = rcl.find_loops(segs, vias)
    assert removed == [] and loops == 0


def test_path_through_pad_copper_is_not_a_loop():
    # both tracks end on the same pad at DIFFERENT points: connected only
    # through pad copper -> no pure-track cycle
    segs = [S("p1s", (0, 0), (2, 0)), S("p2s", (0, 0), (2, 0.4))]
    removed, loops = rcl.find_loops(segs, [])
    assert removed == [] and loops == 0


def test_two_independent_loops_both_break():
    far = [S(s.uuid + "2", (s.a[0] + 20, s.a[1]), (s.b[0] + 20, s.b[1]),
             width=0.4) for s in RECT]
    removed, loops = rcl.find_loops(RECT + far, [])
    assert loops == 2
    assert set(removed) == {"ccc", "ccc2"}


# ============================================================ pure: corners

L1 = S("a", (0, 0), (5, 0), width=0.5)
L2 = S("b", (5, 0), (5, 5), width=0.5)


def test_corner_chamfer_math():
    ops, corners = rcl.find_corners([L1, L2], [], [], EMPTY)
    assert len(corners) == 1
    c = corners[0]
    assert c["chamfer_mm"] == pytest.approx(1.0)  # min(5/3, 2*0.5, 1.0)
    assert c["corner"] == [5.0, 0.0]
    removes = [o["uuid"] for o in ops if o["op"] == "remove"]
    assert sorted(removes) == ["a", "b"]
    adds = [o for o in ops if o["op"] == "add_track"]
    assert len(adds) == 3
    ends = {(tuple(o["start"]), tuple(o["end"])) for o in adds}
    assert ((0.0, 0.0), (4.0, 0.0)) in ends       # leg 1 shortened
    assert ((5.0, 5.0), (5.0, 1.0)) in ends       # leg 2 shortened
    assert ((4.0, 0.0), (5.0, 1.0)) in ends       # 45-deg diagonal
    assert all(o["width"] == 0.5 and o["layer"] == "F.Cu"
               and o["net"] == "N" for o in adds)


def test_corner_total_length_shrinks():
    ops, _ = rcl.find_corners([L1, L2], [], [], EMPTY)
    adds = [o for o in ops if o["op"] == "add_track"]
    new_len = sum(math.dist(o["start"], o["end"]) for o in adds)
    assert new_len < L1.length + L2.length - 0.5  # 2c - c*sqrt(2) ~ 0.586


def test_corner_rejections():
    def corners(segs, vias=(), pads=(), foreign=EMPTY):
        return rcl.find_corners(list(segs), list(vias), list(pads),
                                foreign)[1]

    # not ~90 deg
    assert corners([L1, S("b", (5, 0), (9, 4), width=0.5)]) == []
    # width mismatch
    assert corners([L1, S("b", (5, 0), (5, 5), width=0.4)]) == []
    # leg too short (< 3*width)
    assert corners([L1, S("b", (5, 0), (5, 1), width=0.5)]) == []
    # via at the corner
    assert corners([L1, L2], vias=[V("vv", (5, 0))]) == []
    # pad center within 0.05 mm of the corner
    assert corners([L1, L2], pads=[PD((5.0, 0.03))]) == []
    # foreign copper blocks the chamfer corridor
    assert corners([L1, L2],
                   foreign=lambda l, n: box(4.2, -0.3, 5.3, 1.2)) == []
    # a third segment meets the corner -> not a clean elbow
    assert corners([L1, L2, S("t", (5, 0), (8, 3), width=0.5)]) == []
    # another same-net segment attaches inside the chamfered region
    assert corners([L1, L2, S("t", (5, 0.5), (8, 0.5), width=0.5)]) == []


def test_corner_blocked_by_pad_copper_of_any_net():
    # thin legs -> c = 0.4, corridor sticks out past the c-neighbourhood;
    # a foreign pad beyond c but on the diagonal corridor must block it
    a = S("a", (0, 0), (5, 0), width=0.2)
    b = S("b", (5, 0), (5, 5), width=0.2)
    assert rcl.find_corners([a, b], [], [], EMPTY)[1], "control must chamfer"
    pad = PD((4.55, 0.25), net="OTHER", half=0.05)
    assert rcl.find_corners([a, b], [], [pad], EMPTY)[1] == []


def test_corner_segment_used_only_once():
    # zig-zag: b forms 90-deg corners with both a and c; only one chamfer
    a = S("a", (0, 0), (5, 0), width=0.5)
    b = S("b", (5, 0), (5, 5), width=0.5)
    c = S("c", (5, 5), (10, 5), width=0.5)
    ops, corners = rcl.find_corners([a, b, c], [], [], EMPTY)
    assert len(corners) == 1
    assert len([o for o in ops if o["op"] == "remove"]) == 2


def test_corner_keeps_tee_made_by_planned_leg():
    # a snap removed the tee's third arm and planned a new leg from the same
    # joint: the elbow left behind is still a tee, so no chamfer (a chamfer
    # would move the joint and strand the planned leg)
    leg = S("", (5, 0), (5, -5), width=0.5)
    assert rcl.find_corners([L1, L2], [], [], EMPTY)[1], "control"
    assert rcl.find_corners([L1, L2], [], [], EMPTY, attach=[leg])[1] == []
    # a planned leg meeting one bare segment end is not an elbow either
    assert rcl.find_corners([L1], [], [], EMPTY, attach=[leg])[1] == []


# ============================================================ pure: parse + CLI

def _fp(ref, x, y, pads=""):
    return (f'  (footprint "t:{ref}" (layer "F.Cu")\n    (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n{pads})\n')


def _pad(num, x, y, net):
    return (f'    (pad "{num}" smd rect (at {x} {y}) (size 1 1)'
            f' (layers "F.Cu") (net "{net}"))\n')


def _seg(uuid, a, b, width=0.25, layer="F.Cu", net='(net "VCC")'):
    return (f'  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]})'
            f' (width {width}) (layer "{layer}") {net} (uuid "{uuid}"))\n')


def _pcb(tmp_path_factory, name, body, w=60.0, h=40.0):
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "VCC")
  (setup)
  (gr_rect (start 0 0) (end {w} {h}) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path_factory.mktemp(name) / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


def _dirty_synthetic(tmp_path_factory, name):
    """One dangling stub (numeric net ref) + one clean 90-deg corner."""
    body = _fp("U1", 10, 10, pads=_pad("1", 0, 0, "VCC"))
    body += _fp("R1", 20, 10, pads=_pad("1", 0, 0, "VCC"))
    body += _fp("R2", 24, 14, pads=_pad("1", 0, 0, "VCC"))
    body += _seg("aaaa1111", (10, 10), (12, 10), net="(net 1)")  # stub
    body += _seg("bbbb1111", (20, 10), (24, 10))                 # leg 1
    body += _seg("cccc1111", (24, 10), (24, 14))                 # leg 2
    body += ('  (via (at 30 30) (size 0.6) (drill 0.3)'
             ' (layers "F.Cu" "B.Cu") (net 1) (uuid "eeee1111"))\n')
    return _pcb(tmp_path_factory, name, body)


def test_parse_items(tmp_path_factory):
    p = _dirty_synthetic(tmp_path_factory, "parse")
    segs, vias = rcl.parse_items(p, ["F.Cu", "B.Cu"])
    assert [s.uuid for s in segs] == ["aaaa1111", "bbbb1111", "cccc1111"]
    assert all(s.net == "VCC" for s in segs)  # numeric + string forms
    assert segs[0].a == (10.0, 10.0) and segs[0].b == (12.0, 10.0)
    assert segs[0].width == 0.25 and segs[0].layer == "F.Cu"
    assert len(vias) == 1
    v = vias[0]
    assert v.uuid == "eeee1111" and v.net == "VCC"
    assert v.layers == ("F.Cu", "B.Cu") and v.size == 0.6 and v.drill == 0.3


def test_dry_run_plan_and_determinism(tmp_path_factory):
    p = _dirty_synthetic(tmp_path_factory, "dryrun")
    raw = p.read_bytes()
    p1, _ = rcl.run(["--pcb", str(p), "--dry-run"])
    p2, _ = rcl.run(["--pcb", str(p), "--dry-run"])
    assert _plan(p1) == _plan(p2)
    assert p.read_bytes() == raw  # dry-run never touches the board
    assert p1["status"] == "pass" and p1["dry_run"] is True
    # stub removed (via at 30,30 is isolated -> also dangling), corner smoothed
    assert p1["dangling_segments"] == 1
    assert p1["dangling_vias"] == 1
    assert p1["loops_broken"] == 0
    assert p1["corners_smoothed"] == 1
    kinds = [o["op"] for o in p1["ops"]]
    assert kinds.count("remove") == 4 and kinds.count("add_track") == 3
    removed = {o["uuid"] for o in p1["ops"] if o["op"] == "remove"}
    assert removed == {"aaaa1111", "eeee1111", "bbbb1111", "cccc1111"}
    diag = [o for o in p1["ops"] if o["op"] == "add_track"
            and o["start"] == [23.5, 10.0] and o["end"] == [24.0, 10.5]]
    assert diag, p1["ops"]  # c = min(4/3, 0.5, 1.0) = 0.5


def test_no_smooth_flag(tmp_path_factory):
    p = _dirty_synthetic(tmp_path_factory, "nosmooth")
    payload, _ = rcl.run(["--pcb", str(p), "--dry-run", "--no-smooth"])
    assert payload["corners_smoothed"] == 0
    assert all(o["op"] == "remove" for o in payload["ops"])
    assert len(payload["ops"]) == 2  # stub + isolated via only


def test_cli_contract(tmp_path_factory, tmp_path):
    assert rcl.main(["--pcb", "no/such/board.kicad_pcb"]) == 2
    p = _dirty_synthetic(tmp_path_factory, "clicheck")
    rep = tmp_path / "r.json"
    rc = rcl.main(["--pcb", str(p), "--dry-run", "--out-report", str(rep)])
    assert rc == 0
    r = json.loads(rep.read_text("utf-8"))
    assert r["script"] == "route_cleanup" and r["status"] == "pass"
    for k in ("counts", "violations", "dangling_removed", "loops_broken",
              "corners_smoothed", "ops", "drc_before", "drc_after"):
        assert k in r, k


# ============================================================ smoke: corpus

@pytest.fixture(scope="session")
def cli() -> Path:
    c = env.find_kicad_cli()
    if c is None:
        pytest.skip("kicad-cli not installed")
    return c


def _clear(shape, cu, extras, *own):
    z = cu
    for p in own:
        z = z.difference(p.buffer(0.02))
    if shape.intersects(z):
        return False
    return not any(shape.intersects(e) for e in extras)


def _r3(p):
    return (round(p[0], 3), round(p[1], 3))


@pytest.fixture(scope="session")
def dirty_board(cli, tmp_path_factory) -> dict:
    """Placed-unrouted blinky2 with injected dirt (stub, loop, corner)."""
    import board_init
    import place_seed
    import route_edit

    d = tmp_path_factory.mktemp("cleanup")
    rc = board_init.main([
        "--netlist", str(S7 / "blinky2" / "kicad" / "blinky2.net"),
        "--name", "blinky2", "--out", str(d / "kicad"), "--layers", "2",
        "--schematic", str(GOLDEN / "blinky2" / "blinky2.kicad_sch"),
        # the injected dirt runs along the edge, where default GND holes sit
        "--mounting-holes", "0"])
    assert rc == 0
    pcb = d / "kicad" / "blinky2.kicad_pcb"
    for name in ("constraints.json", "decoupling.json"):
        shutil.copy2(GOLDEN / "blinky2" / name, pcb.parent / name)
    payload, _ = place_seed.run([
        "--pcb", str(pcb), "--ops-out", str(d / "seed_ops.json"), "--apply"])
    assert payload["status"] == "pass"

    bg = geom.BoardGeom.from_file(pcb)
    cu = bg.layer_copper("F.Cu")
    inner = bg.outline.buffer(-1.2)
    extras: list = []
    gnd = sorted(bg.pads_of(net="GND", layer="F.Cu"),
                 key=lambda p: p.center)
    assert len(gnd) >= 3

    # ---- 1. dangling stub off a real GND pad, into free space
    stub = None
    for pad in gnd:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            end = _r3((pad.center[0] + 2.5 * dx, pad.center[1] + 2.5 * dy))
            line = LineString([pad.center, end])
            if inner.covers(Point(end)) and _clear(line.buffer(0.5), cu,
                                                   extras, pad.poly):
                stub = (pad, end)
                break
        if stub:
            break
    assert stub, "no clear direction for the dangling stub"
    stub_pad, stub_end = stub
    extras.append(LineString([stub_pad.center, stub_end]).buffer(0.3))

    # ---- 2. pure track loop: 6x1 rectangle anchored at a GND pad, plus a
    # chain from the adjacent corner to a second GND pad
    rect = None
    for p1 in gnd:
        if p1 is stub_pad:
            continue
        P = _r3(p1.center)
        for sx in (1, -1):
            for sy in (1, -1):
                A = (round(P[0] + 6.0 * sx, 3), P[1])
                B = (A[0], round(A[1] + 1.0 * sy, 3))
                C = (P[0], round(P[1] + 1.0 * sy, 3))
                ring = LineString([P, A, B, C, P]).buffer(0.6)
                if ring.within(inner) and _clear(ring, cu, extras, p1.poly):
                    rect = (p1, P, A, B, C)
                    break
            if rect:
                break
        if rect:
            break
    assert rect, "no clear placement for the loop rectangle"
    p1, P, A, B, C = rect
    ring = LineString([P, A, B, C, P]).buffer(0.6)
    extras.append(ring)
    chain_to = None
    others = sorted((p for p in gnd if p is not stub_pad and p is not p1),
                    key=lambda p: math.dist(A, p.center))
    for p2 in others:
        line = LineString([A, p2.center])
        sh = line.buffer(0.4).difference(Point(A).buffer(0.9))
        if _clear(sh, cu, extras, p2.poly):
            chain_to = p2
            break
    if chain_to is None:
        chain_to = others[0]  # accept a crossing chain; assertions hold
    extras.append(LineString([A, chain_to.center]).buffer(0.3))

    # ---- 3. clean 90-deg corner between two same-net pads
    corner = None
    used = {id(stub_pad), id(p1), id(chain_to)}
    all_pads = bg.pads_of(layer="F.Cu")
    for net in ("+3V3", "GND"):
        cand = [p for p in sorted(bg.pads_of(net=net, layer="F.Cu"),
                                  key=lambda p: p.center)
                if id(p) not in used]
        for q in cand:
            for r in cand:
                if q is r:
                    continue
                e = (round(r.center[0], 3), round(q.center[1], 3))
                lx = abs(e[0] - q.center[0])
                ly = abs(e[1] - r.center[1])
                if lx < 2.6 or ly < 2.6 or not inner.covers(Point(e)):
                    continue
                if any(p.poly.distance(Point(e)) <= 1.6 for p in all_pads):
                    continue
                sh = LineString([q.center, e]).buffer(0.8).union(
                    LineString([e, r.center]).buffer(0.8))
                if _clear(sh, cu, extras, q.poly, r.poly):
                    corner = (net, q, r, e)
                    break
            if corner:
                break
        if corner:
            break
    assert corner, "no clear L-path found for the corner fixture"
    cnet, q, r, e = corner

    ops = [
        {"op": "add_track", "start": list(_r3(stub_pad.center)),
         "end": list(stub_end), "width": 0.25, "layer": "F.Cu",
         "net": "GND"},
    ]
    for a, b in ((P, A), (A, B), (B, C), (C, P)):
        ops.append({"op": "add_track", "start": list(a), "end": list(b),
                    "width": 0.4, "layer": "F.Cu", "net": "GND"})
    ops.append({"op": "add_track", "start": list(A),
                "end": list(_r3(chain_to.center)), "width": 0.3,
                "layer": "F.Cu", "net": "GND"})
    ops.append({"op": "add_track", "start": list(_r3(q.center)),
                "end": list(e), "width": 0.25, "layer": "F.Cu", "net": cnet})
    ops.append({"op": "add_track", "start": list(e),
                "end": list(_r3(r.center)), "width": 0.25, "layer": "F.Cu",
                "net": cnet})
    results = route_edit.apply_ops(pcb, ops)
    assert all(x["status"] == "added" for x in results), results
    uuids = [x["uuid"] for x in results]
    return {
        "pcb": pcb,
        "stub": uuids[0],
        "rect": uuids[1:5],
        "chain": uuids[5],
        "corner": uuids[6:8],
        "corner_net": cnet,
    }


@pytest.mark.smoke
def test_cleanup_acceptance_blinky2(dirty_board, tmp_path):
    pcb = dirty_board["pcb"]

    # dry-run twice: identical plans, board untouched
    raw = pcb.read_bytes()
    # --no-snap: this tests the hygiene and corner passes; the off-angle
    # "chain" track is the fixture's keep-me item, and the snap has its own
    # tests.
    p1, _ = rcl.run(["--pcb", str(pcb), "--dry-run", "--no-snap"])
    p2, _ = rcl.run(["--pcb", str(pcb), "--dry-run", "--no-snap"])
    assert _plan(p1) == _plan(p2)
    assert pcb.read_bytes() == raw
    assert p1["dangling_segments"] == 1 and p1["dangling_vias"] == 0
    assert p1["loops_broken"] == 1
    assert p1["corners_smoothed"] == 1
    # T6: after the loop breaks, its two remaining short sides orphan IFF
    # the victim was a middle side (uuid tie-break decides which 6 mm side
    # loses, so both outcomes are legal); orphans are then removed too.
    orphans = p1["orphaned_after_loops"]
    assert orphans in (0, 2)
    planned = {o["uuid"] for o in p1["ops"] if o["op"] == "remove"}
    assert dirty_board["stub"] in planned
    assert len(planned & set(dirty_board["rect"])) == 1 + orphans
    assert set(dirty_board["corner"]) <= planned
    assert dirty_board["chain"] not in planned

    net = dirty_board["corner_net"]
    bg0 = geom.BoardGeom.from_file(pcb)
    n_before = len(bg0.tracks_of(net=net, layer="F.Cu"))
    len_before = sum(t.length for t in bg0.tracks_of(net=net, layer="F.Cu"))

    # the real thing
    rep = tmp_path / "cleanup.json"
    rc = rcl.main(["--pcb", str(pcb), "--no-snap", "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 0 and r["status"] == "pass", r
    # stub + loop side (+ orphaned short sides) + (2 removes + 3 adds)
    assert r["ops_applied"] == 7 + r["orphaned_after_loops"]
    assert r["drc_after"]["unconnected"] <= r["drc_before"]["unconnected"]
    assert r["drc_after"]["errors"] <= r["drc_before"]["errors"]
    assert "dangling_flagged" in r["drc_before"]
    assert r["refilled"] is False  # placed-unrouted blinky2 has no zones

    text = pcb.read_text("utf-8")
    assert dirty_board["stub"] not in text                      # stub gone
    assert sum(u in text for u in dirty_board["rect"]) == \
        3 - r["orphaned_after_loops"]                           # loop broken
    assert dirty_board["chain"] in text                         # chain kept
    assert all(u not in text for u in dirty_board["corner"])    # chamfered

    bg1 = geom.BoardGeom.from_file(pcb)
    tr = bg1.tracks_of(net=net, layer="F.Cu")
    assert len(tr) == n_before + 1          # 2 legs -> 2 short legs + diagonal
    assert sum(t.length for t in tr) < len_before - 0.05  # corner got shorter
