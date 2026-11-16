"""route_finish: the maze finisher's pure parts (no KiCad): the rules it
reads from a board, its islands, and the routes it plans around copper."""
from __future__ import annotations

import json
import math
import sys
import types
from array import array
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import LineString, Point, box

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import geom  # noqa: E402
import route_auto  # noqa: E402
import route_edit  # noqa: E402
import route_finish as rf  # noqa: E402

HDR = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (net 0 "") (net 1 "GND") (net 2 "SIG") (net 3 "+3V3")
"""

DRU = """(version 1)
(rule "aiee_clearance_floor"
	(constraint clearance (min 0.1500mm))
)
(rule "aiee_hole_to_hole_floor"
	(constraint hole_to_hole (min 0.5000mm))
)
(rule "aiee_edge_clearance_floor"
	(constraint edge_clearance (min 0.3000mm))
)
(rule "aiee_pwr_width_SIG"
	(constraint track_width (min 0.3000mm))
	(condition "A.NetName == 'SIG' && A.Type == 'track'")
)
"""


def _pad(ref, x, y, net, size=1.0):
    return (f'  (footprint "t:{ref}" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            f'    (pad "1" smd rect (at 0 0) (size {size} {size})'
            f' (layers "F.Cu") (net "{net}")))\n')


def _seg(x1, y1, x2, y2, w, layer, net, uuid=None):
    u = f' (uuid "{uuid}")' if uuid else ""
    return (f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width {w})'
            f' (layer "{layer}") (net "{net}"){u})\n')


def _zone(net_id, net, layer, x1, y1, x2, y2):
    pts = f"(xy {x1} {y1}) (xy {x2} {y1}) (xy {x2} {y2}) (xy {x1} {y2})"
    return (f'  (zone (net {net_id}) (net_name "{net}") (layer "{layer}")\n'
            f'    (connect_pads (clearance 0.3)) (min_thickness 0.25)\n'
            f'    (polygon (pts {pts}))\n'
            f'    (filled_polygon (layer "{layer}") (pts {pts})))\n')


def _board(tmp_path, body, size=30.0, dru=DRU):
    p = tmp_path / "t.kicad_pcb"
    p.write_text(HDR + f'  (gr_rect (start 0 0) (end {size} {size})'
                 ' (stroke (width 0.1)) (fill no) (layer "Edge.Cuts"))\n'
                 + body + ")\n", encoding="utf-8")
    if dru:
        p.with_suffix(".kicad_dru").write_text(dru, encoding="utf-8")
    return p


def _pair(net, a, b, amsg="Pad 1 [SIG] of U1 on F.Cu",
          bmsg="Pad 1 [SIG] of U2 on F.Cu"):
    return {"net": net, "a": {"msg": amsg, "pos": list(a)},
            "b": {"msg": bmsg, "pos": list(b)}}


def _plan(pcb, pairs, **kw):
    bg = geom.BoardGeom.from_file(pcb)
    rules = rf.board_rules(pcb, bg)
    return rules, rf.plan_routes(bg, rules, pairs, bg.copper_layers, **kw)


def _tracks(job, layer=None):
    return [op for op in job["ops"] if op["op"] == "add_track"
            and (layer is None or op["layer"] == layer)]


# ------------------------------------------------------------------ rules

def test_rules_come_from_the_board(tmp_path):
    body = _pad("U1", 5, 15, "SIG")
    body += ('  (via (at 3 3) (size 0.7) (drill 0.35) (layers "F.Cu" "B.Cu")'
             ' (net "GND"))\n') * 1
    pcb = _board(tmp_path, body)
    rules = rf.board_rules(pcb, geom.BoardGeom.from_file(pcb))
    assert rules.clearance == 0.15            # the .kicad_dru baseline
    assert rules.hole_to_hole == 0.5
    assert rules.edge_clearance == 0.3
    assert (rules.via_size, rules.via_drill) == (0.7, 0.35)  # the board's via
    assert rules.width("SIG") == 0.301        # its rules_gen width, rounded up
    assert rules.width("OTHER") == 0.2
    assert rules.clr("SIG", "GND") == 0.15 + rules.margin


VIA_DRU = """(version 1)
(rule "aiee_via_drill_floor"
	(constraint hole_size (min 0.1270mm))
	(condition "A.Type == 'via'")
)
(rule "aiee_via_diameter_floor"
	(constraint via_diameter (min 0.2540mm))
	(condition "A.Type == 'via'")
)
"""
ANNULAR_DRU = VIA_DRU + """(rule "aiee_annular_floor"
	(constraint annular_width (min 0.0635mm))
	(condition "A.Type == 'via'")
)
"""


def _via_floor(tmp_path, dru, pro_annular=None):
    pcb = _board(tmp_path, _pad("U1", 5, 15, "SIG"), dru=dru)
    rules = {"min_via_diameter": 0.254, "min_through_hole_diameter": 0.127}
    if pro_annular is not None:
        rules["min_via_annular_width"] = pro_annular
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(
        {"board": {"design_settings": {"rules": rules}}}), encoding="utf-8")
    return rf.board_rules(pcb, geom.BoardGeom.from_file(pcb)).via_floor


def test_via_floor_reads_the_projects_annular_ring(tmp_path):
    """#981: the .kicad_pro's 0.0635 mm ring lets the 0.127 drill take a
    0.254 via; the stock 0.1 mm ring sized it 0.327 and left balls out."""
    assert _via_floor(tmp_path, VIA_DRU, pro_annular=0.0635) == (0.254, 0.127)
    # neither file sets a ring: KiCad's 0.1 mm stock one
    assert _via_floor(tmp_path, VIA_DRU) == (0.327, 0.127)
    # a ring wider than the floor via's sizes it up
    assert _via_floor(tmp_path, VIA_DRU, pro_annular=0.1) == (0.327, 0.127)


def test_via_floor_reads_a_via_only_dru_rule(tmp_path):
    """#981: rules_gen writes the fab's via floors on `A.Type == 'via'`;
    they are board floors, not conditioned rules to skip."""
    assert _via_floor(tmp_path, ANNULAR_DRU) == (0.254, 0.127)
    # the DRU's ring wins over a narrower one in the project
    wide = ANNULAR_DRU.replace("0.0635mm", "0.1000mm")
    assert _via_floor(tmp_path, wide, pro_annular=0.0635) == (0.327, 0.127)
    # a ring on some vias only is not a board floor: the project's holds
    some = VIA_DRU + """(rule "aiee_annular_X"
	(constraint annular_width (min 0.1000mm))
	(condition "A.Type == 'via' && A.NetName == 'X'")
)
"""
    assert _via_floor(tmp_path, some, pro_annular=0.0635) == (0.254, 0.127)

# ----------------------------------------------------------------- routes

def test_routes_around_a_wall_on_one_layer(tmp_path):
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    body += _seg(15, 0.8, 15, 20, 0.25, "F.Cu", "+3V3")
    pcb = _board(tmp_path, body)
    # vias priced high: the way round the wall's end is the cheaper one
    rules, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))],
                        via_cost_mm=30, margin=8)
    job = jobs[0]
    assert job["status"] == "routed" and job["vias"] == 0
    wall = LineString([(15, 0.8), (15, 20)])
    for op in _tracks(job):
        assert op["width"] == 0.301
        seg = LineString([op["start"], op["end"]])
        assert seg.distance(wall) >= 0.125 + op["width"] / 2 + 0.15 - 1e-6
        dx = op["end"][0] - op["start"][0]
        dy = op["end"][1] - op["start"][1]
        ang = math.degrees(math.atan2(dy, dx)) % 45
        assert min(ang, 45 - ang) < 1e-6            # 0/45/90 only
    route_edit.validate_ops({"version": 1, "ops": job["ops"]})


def _tie(ref, at, n1, n2):
    """A net tie whose bridge runs 10 mm either side of it, along local x."""
    return (f'  (footprint "t:{ref}" (layer "F.Cu") (at {at})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            '    (fp_poly (pts (xy -10 -0.25) (xy 10 -0.25) (xy 10 0.25) (xy -10 0.25))\n'
            '      (stroke (width 0) (type solid)) (fill yes) (layer "F.Cu"))\n'
            f'    (pad "1" smd circle (at -10 0) (size 0.5 0.5) (layers "F.Cu") (net "{n1}"))\n'
            f'    (pad "2" smd circle (at 10 0) (size 0.5 0.5) (layers "F.Cu") (net "{n2}")))\n')


def test_a_net_tie_bridge_is_a_wall_to_other_nets(tmp_path):
    # rotated 90: the bridge stands across the straight line from U1 to U2
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    body += _tie("NT1", "15 11 90", "GND", "+3V3")
    pcb = _board(tmp_path, body)
    bg = geom.BoardGeom.from_file(pcb)
    wall = bg.fp_copper[0]["shape"]
    assert wall.bounds == pytest.approx((14.75, 1, 15.25, 21))
    _, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))], via_cost_mm=30, margin=8)
    job = jobs[0]
    assert job["status"] == "routed"
    for op in _tracks(job, "F.Cu"):
        seg = LineString([op["start"], op["end"]])
        assert seg.distance(wall) >= op["width"] / 2 + 0.15 - 1e-6


def test_a_net_tie_bridge_does_not_block_its_own_nets(tmp_path):
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    body += _tie("NT1", "15 11 90", "SIG", "GND")
    pcb = _board(tmp_path, body)
    _, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))], via_cost_mm=30, margin=8)
    assert jobs[0]["status"] == "routed"
    assert jobs[0]["vias"] == 0
    assert sum(math.dist(op["start"], op["end"]) for op in _tracks(jobs[0])) < 21


def test_no_via_in_an_own_smd_pad(tmp_path):
    # a wall leaves only B.Cu; the vias must sit off both 2 mm SIG pads
    body = _pad("U1", 5, 15, "SIG", size=2.0) + _pad("U2", 25, 15, "SIG",
                                                       size=2.0)
    body += _seg(15, 0.1, 15, 29.9, 0.25, "F.Cu", "+3V3")
    pcb = _board(tmp_path, body)
    rules, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))])
    job = jobs[0]
    assert job["status"] == "routed" and job["vias"] == 2
    keep = rules.via_drill / 2 + rf.VIA_PAD_KEEP_MM
    for op in job["ops"]:
        if op["op"] == "add_via":
            x, y = op["at"]
            dx = min(abs(x - 5), abs(x - 25)) - 1.0
            dy = abs(y - 15) - 1.0
            assert max(dx, dy) >= keep - 0.05, op["at"]


def test_via_in_pad_refs_take_a_via_in_their_pads(tmp_path):
    # approved filled via-in-pad: U1's pad, ringed in on F.Cu, may hold the
    # via; U2's still may not
    body = _pad("U1", 5, 15, "SIG", size=2.0) + _pad("U2", 25, 15, "SIG",
                                                       size=2.0)
    ring = [(3.6, 13.6), (6.4, 13.6), (6.4, 16.4), (3.6, 16.4), (3.6, 13.6)]
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        body += _seg(x1, y1, x2, y2, 0.25, "F.Cu", "+3V3")
    pcb = _board(tmp_path, body)
    bg = geom.BoardGeom.from_file(pcb)
    rules = rf.board_rules(pcb, bg)
    shut = rf.plan_routes(bg, rules, [_pair("SIG", (5, 15), (25, 15))],
                          bg.copper_layers, rip_board=False)[0]
    assert shut["status"] != "routed"
    rules.via_in_pad_refs = frozenset({"U1"})
    job = rf.plan_routes(bg, rules, [_pair("SIG", (5, 15), (25, 15))],
                         bg.copper_layers, rip_board=False)[0]
    assert job["status"] == "routed" and job["vias"] >= 1
    vias = [op["at"] for op in job["ops"] if op["op"] == "add_via"]
    keep = rules.via_drill / 2 + rf.VIA_PAD_KEEP_MM
    assert any(abs(x - 5) < 1.0 and abs(y - 15) < 1.0 for x, y in vias), vias
    json.dumps(rf._rules_facts(rules))   # the report stays JSON
    for x, y in vias:
        if abs(x - 25) < 5:
            assert max(abs(x - 25) - 1.0, abs(y - 15) - 1.0) >= keep - 0.05


def _mpad(ref, x, y, net, size, margin=None):
    m = f" (solder_mask_margin {margin})" if margin is not None else ""
    return (f'  (footprint "t:{ref}" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            f'    (pad "1" smd rect (at 0 0) (size {size} {size})'
            f' (layers "F.Cu" "F.Mask") (net "{net}"){m}))\n')


def _ringed(tmp_path, margin=None, setup="(setup)"):
    """U1 walled in on F.Cu by a +3V3 ring 0.4 mm off its 2 mm pad: the
    route has to drop to B.Cu close beside both pads."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    body = _mpad("U1", 5, 15, "SIG", 2.0, margin) \
        + _mpad("U2", 25, 15, "SIG", 2.0, margin)
    ring = [(2.6, 12.6), (7.4, 12.6), (7.4, 17.4), (2.6, 17.4), (2.6, 12.6)]
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        body += _seg(x1, y1, x2, y2, 0.25, "F.Cu", "+3V3")
    pcb = _board(tmp_path, body)
    if setup != "(setup)":
        pcb.write_text(pcb.read_text("utf-8").replace("(setup)", setup, 1),
                       encoding="utf-8")
    rules, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))])
    job = jobs[0]
    assert job["status"] == "routed", job
    vias = [op for op in job["ops"] if op["op"] == "add_via"]
    text = pcb.read_text("utf-8").rstrip()[:-1]
    for v in vias:
        text += (f'  (via (at {v["at"][0]} {v["at"][1]}) (size {v["size"]})'
                 f' (drill {v["drill"]}) (layers "F.Cu" "B.Cu")'
                 f' (net "SIG"))\n')
    pcb.write_text(text + ")\n", encoding="utf-8")
    import check_wicking
    vios, _ = check_wicking.check(pcb)
    return rules, vias, vios


def test_a_via_keeps_its_hole_out_of_a_pad_mask_opening(tmp_path):
    """#636: route_finish kept a via 0.1 mm off its own net's pad copper,
    but a pad whose mask opens 0.2 mm wider than its copper then held the
    via's hole in the opening (check_wicking wick_via_in_pad, as on
    twin-side). Planted: margin 0.2, the via moves out and the check is
    quiet. Twin: no margin, the via sits where it always did."""
    rules, vias, vios = _ringed(tmp_path / "m", margin=0.2)
    assert vios == [], vios
    assert rules.pad_via_keep("U2", "1", 0.3, 0.3) == pytest.approx(0.45)
    near2 = max(v["at"][0] for v in vias)
    assert 24.0 - near2 >= 0.45 - 0.05
    rules0, vias0, vios0 = _ringed(tmp_path / "q")
    assert vios0 == []
    assert rules0.mask is None          # nothing to read: the fast path
    assert rules0.pad_via_keep("U2", "1", 0.3, 0.3) == pytest.approx(0.25)
    assert max(v["at"][0] for v in vias0) > near2


def test_an_untented_via_keeps_a_mask_dam_from_a_pad(tmp_path):
    """#636: with vias untented, the via's own opening must keep a full
    dam from the pad's (wick_via_mask_web). Planted: untented, the keep
    grows to the via's ring plus the dam, and the check is quiet. Twin:
    the same board tented keeps the plain hole keep."""
    open_ = "(setup (tenting (front no) (back no)))"
    rules, vias, vios = _ringed(tmp_path / "u", setup=open_)
    assert vios == [], vios
    assert rules.mask["open"] == {"F": True, "B": True}
    keep = rules.pad_via_keep("U2", "1", 0.3, 0.3)
    assert keep > 0.3
    rules_t, _, vios_t = _ringed(
        tmp_path / "t", setup="(setup (tenting (front yes) (back yes)))")
    assert vios_t == []
    assert rules_t.pad_via_keep("U2", "1", 0.3, 0.3) == pytest.approx(0.25)


def test_a_route_that_needs_a_via_in_a_pad_names_the_pad(tmp_path):
    """#636: a route that only goes through with a via in a pad fails, as
    #912 has it (via-in-pad is a paid fab option), but says which pad and
    how to allow it. Twin: a net walled in on both layers fails with no
    pad named."""
    body = _pad("U1", 5, 15, "SIG", size=2.0) + _pad("U2", 25, 15, "SIG",
                                                       size=2.0)
    ring = [(3.6, 13.6), (6.4, 13.6), (6.4, 16.4), (3.6, 16.4), (3.6, 13.6)]
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        body += _seg(x1, y1, x2, y2, 0.25, "F.Cu", "+3V3")
    (tmp_path / "p").mkdir()
    (tmp_path / "q").mkdir()
    pcb = _board(tmp_path / "p", body)
    _, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))], rip_board=False)
    job = jobs[0]
    assert job["status"] == "failed"
    assert job["via_in_pad"] == ["U1 pad 1"]
    assert "--via-in-pad-refs U1" in job["reason"]
    assert not [op for op in job.get("ops") or () if op["op"] == "add_via"]
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        body += _seg(x1, y1, x2, y2, 0.25, "B.Cu", "+3V3")
    pcb = _board(tmp_path / "q", body)
    _, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))], rip_board=False)
    assert jobs[0]["status"] == "failed"
    assert "via_in_pad" not in jobs[0]
    assert "via-in-pad" not in jobs[0]["reason"]


def test_via_in_pad_refs_is_a_routing_flag():
    import argparse
    ap = argparse.ArgumentParser()
    rf.add_routing_args(ap)
    assert ap.parse_args(["--via-in-pad-refs", "U1,U2"]).via_in_pad_refs \
        == "U1,U2"
    assert "via_in_pad_refs" in rf.ROUTING_FLAGS


def test_vias_under_a_full_wall(tmp_path):
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    body += _seg(15, 0.1, 15, 29.9, 0.25, "F.Cu", "+3V3")
    pcb = _board(tmp_path, body)
    _, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))])
    job = jobs[0]
    assert job["status"] == "routed" and job["vias"] == 2
    assert _tracks(job, "B.Cu")
    for op in job["ops"]:
        if op["op"] == "add_via":
            assert abs(op["at"][0] - 15) >= 0.3 + 0.125 + 0.15 - 1e-6


def test_no_path_is_reported_not_forced(tmp_path):
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    body += _seg(15, 0.1, 15, 29.9, 0.25, "F.Cu", "+3V3")
    body += _seg(15, 0.1, 15, 29.9, 0.25, "B.Cu", "+3V3")
    pcb = _board(tmp_path, body)
    _, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))])
    assert jobs[0]["status"] == "failed"
    assert "ops" not in jobs[0]


def test_search_stopped_by_node_limit_says_so(tmp_path):
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    pcb = _board(tmp_path, body)
    _, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))], max_nodes=5)
    assert jobs[0]["status"] == "failed"
    assert "--max-nodes" in jobs[0]["reason"]


def test_search_weight_still_finds_the_route(tmp_path):
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    pcb = _board(tmp_path, body)
    _, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))], search_weight=3.0)
    assert jobs[0]["status"] == "routed"


def test_later_routes_keep_clear_of_earlier_ones(tmp_path):
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    body += _pad("U3", 15, 5, "GND") + _pad("U4", 15, 25, "GND")
    pcb = _board(tmp_path, body)
    pairs = [_pair("SIG", (5, 15), (25, 15)),
             _pair("GND", (15, 5), (15, 25), "Pad 1 [GND] of U3 on F.Cu",
                   "Pad 1 [GND] of U4 on F.Cu")]
    _, jobs = _plan(pcb, pairs)
    assert all(j["status"] == "routed" for j in jobs)
    sig = [LineString([o["start"], o["end"]]) for o in _tracks(jobs[0], "F.Cu")]
    for o in _tracks(jobs[1], "F.Cu"):
        g = LineString([o["start"], o["end"]])
        assert min(g.distance(s) for s in sig) >= 0.301 / 2 + 0.1 + 0.15 - 1e-6


def test_a_pour_piece_is_joined_by_a_via(tmp_path):
    # KiCad names a zone by its outline corner, not by the piece cut off;
    # the piece is found as an island and joined to the largest one
    body = _zone(1, "GND", "B.Cu", 1, 1, 29, 29)
    body += _zone(1, "GND", "F.Cu", 10, 10, 13, 13)
    pcb = _board(tmp_path, body)
    zmsg = "Zone [GND] on F.Cu, priority 0"
    _, jobs = _plan(pcb, [_pair("GND", (1, 1), (1, 1), zmsg,
                                "Zone [GND] on B.Cu, priority 0")])
    job = jobs[0]
    assert job["status"] == "routed"
    vias = [op for op in job["ops"] if op["op"] == "add_via"]
    assert len(vias) == 1
    assert 10 < vias[0]["at"][0] < 13 and 10 < vias[0]["at"][1] < 13


def test_islands_join_through_vias(tmp_path):
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    body += _seg(5, 15, 10, 15, 0.3, "F.Cu", "SIG")
    body += _seg(10, 15, 20, 15, 0.3, "B.Cu", "SIG")
    body += _seg(20, 15, 25, 15, 0.3, "F.Cu", "SIG")
    body += ('  (via (at 20 15) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
             ' (net "SIG"))\n')
    pcb = _board(tmp_path, body)
    bg = geom.BoardGeom.from_file(pcb)
    assert len(rf.net_islands(bg, "SIG")) == 2      # no via at x=10 yet
    body += ('  (via (at 10 15) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
             ' (net "SIG"))\n')
    pcb = _board(tmp_path, body)
    bg = geom.BoardGeom.from_file(pcb)
    isl = rf.net_islands(bg, "SIG")
    assert len(isl) == 1
    _, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))])
    assert jobs[0]["status"] == "skipped"


def test_pick_island_follows_the_layer_named():
    from shapely.geometry import box
    isl = [{"F.Cu": [box(0, 0, 1, 1)]}, {"B.Cu": [box(0, 0, 1, 1)]}]
    assert rf.pick_island(isl, (0.5, 0.5), "Track [X] on B.Cu") == 1
    assert rf.pick_island(isl, (0.5, 0.5), "Track [X] on F.Cu") == 0


def test_path_to_ops_vias_at_layer_changes():
    gr = rf.Grid(x0=0, y0=0, nx=10, ny=10, g=0.1, layers=["F.Cu", "B.Cu"],
                 blk=[], vblk=b"", via=(0.6, 0.3), cost=[], in_a=[], in_b=[],
                 a_plane=b"", b_plane=b"")
    rules = rf.Rules(clearance=0.1, hole_clearance=0.25, hole_to_hole=0.5,
                     edge_clearance=0.3, min_width=0.2, via_size=0.6,
                     via_drill=0.3)
    path = [(0, 0, 0), (1, 0, 0), (2, 0, 0), (2, 0, 1), (2, 1, 1), (3, 2, 1),
            (3, 2, -1)]
    ops = rf.path_to_ops(gr, path, "SIG", 0.2, rules)
    tracks = [o for o in ops if o["op"] == "add_track"]
    vias = [o["at"] for o in ops if o["op"] == "add_via"]
    assert [(t["layer"], t["start"], t["end"]) for t in tracks] == [
        ("F.Cu", [0.0, 0.0], [0.2, 0.0]),
        ("B.Cu", [0.2, 0.0], [0.2, 0.1]),
        ("B.Cu", [0.2, 0.1], [0.3, 0.2])]
    assert vias == [[0.2, 0.0], [0.3, 0.2]]


# ------------------------------------------------------- route_auto hooks

def test_krt_narrow_tracks_on_power_nets_are_found(tmp_path):
    body = _seg(5, 5, 10, 5, 0.2, "F.Cu", "SIG", "u-narrow")  # KRT: under 0.3
    body += _seg(5, 8, 10, 8, 0.35, "F.Cu", "SIG", "u-wide")
    body += _seg(5, 11, 10, 11, 0.2, "F.Cu", "GND", "u-gnd")  # no width rule
    pcb = _board(tmp_path, body)
    bg = geom.BoardGeom.from_file(pcb)
    narrow = route_auto._narrow_new_tracks(pcb, {"SIG": 0.3}, set())
    assert narrow == ["u-narrow"]
    assert route_auto._narrow_new_tracks(
        pcb, {"SIG": 0.3}, {t.uuid for t in bg.tracks_of()}) == []


def test_a_walled_in_pad_rips_the_signal_track_in_the_way(tmp_path):
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    body += _seg(15, 0.1, 15, 29.9, 0.25, "F.Cu", "+3V3", "u-wf")
    body += _seg(15, 0.1, 15, 29.9, 0.25, "B.Cu", "+3V3", "u-wb")
    pcb = _board(tmp_path, body)
    pairs = [_pair("SIG", (5, 15), (25, 15))]
    _, jobs = _plan(pcb, pairs)
    assert jobs[0]["status"] == "failed"            # only with rip_board
    rip: list = []
    _, jobs = _plan(pcb, pairs, rip_board=True, ripped_out=rip)
    job = jobs[0]
    assert job["status"] == "routed"
    assert job["ripped_nets"] == ["+3V3"]
    assert rip and set(rip) <= {"u-wf", "u-wb"} and len(rip) == 1
    assert job["vias"] == 0 or len(rip) == 1        # one layer crossed


def test_a_pad_walled_in_by_five_nets_waits_for_a_wider_rip(tmp_path):
    """#1374 (the 6-layer bench's U2 /B5_2, boxed in by six nets): five
    signal walls on both layers are one more than the default rip budget,
    so the job fails with rip_over_budget 5; a budget of 6 routes it,
    ripping all five. Quiet twin: when one wall is a net never ripped no
    budget frees it, and no rip_over_budget is set."""
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    walls = [f"W{i}" for i in range(5)]
    for i, w in enumerate(walls):
        x = 11 + 2 * i
        body += _seg(x, 0.1, x, 29.9, 0.25, "F.Cu", w, f"u-{w}f")
        body += _seg(x, 0.1, x, 29.9, 0.25, "B.Cu", w, f"u-{w}b")
    pcb = _board(tmp_path, body)
    pairs = [_pair("SIG", (5, 15), (25, 15))]
    _, jobs = _plan(pcb, pairs, rip_board=True, ripped_out=[])
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["rip_refused"] == walls
    assert jobs[0]["rip_over_budget"] == 5
    rip: list = []
    _, jobs = _plan(pcb, pairs, rip_board=True, ripped_out=rip,
                    rip_board_nets=6)
    assert jobs[0]["status"] == "routed"
    assert jobs[0]["ripped_nets"] == walls
    _, jobs = _plan(pcb, pairs, rip_board=True, ripped_out=[],
                    rip_board_nets=6, keep_nets_extra={"W2"})
    assert jobs[0]["status"] == "failed"
    assert "rip_over_budget" not in jobs[0]


def test_power_and_pour_copper_is_never_ripped(tmp_path):
    body = _pad("U1", 5, 15, "+3V3") + _pad("U2", 25, 15, "+3V3")
    # SIG carries a .kicad_dru width rule: sized by rule, never ripped
    body += _seg(15, 0.1, 15, 29.9, 0.35, "F.Cu", "SIG", "u-wf")
    body += _seg(15, 0.1, 15, 29.9, 0.35, "B.Cu", "SIG", "u-wb")
    pcb = _board(tmp_path, body)
    rip: list = []
    _, jobs = _plan(pcb, [_pair("+3V3", (5, 15), (25, 15),
                                "Pad 1 [+3V3] of U1 on F.Cu",
                                "Pad 1 [+3V3] of U2 on F.Cu")],
                    rip_board=True, ripped_out=rip)
    assert jobs[0]["status"] == "failed"         # SIG stays a wall
    assert "ripped_nets" not in jobs[0]
    assert rip == []


def test_a_diff_pair_is_never_ripped(tmp_path):
    """#613: a rip-up took pair copper and rerouted it single-ended, DRC clean."""
    body = _pad("U1", 5, 15, "A") + _pad("U2", 25, 15, "A")
    body += _seg(15, 0.1, 15, 29.9, 0.2, "F.Cu", "D_P", "u-wf")
    body += _seg(15, 0.1, 15, 29.9, 0.2, "B.Cu", "D_P", "u-wb")
    pcb = _board(tmp_path, body)
    (pcb.parent / "constraints.json").write_text(
        '{"diff_pairs": [{"p": "D_P", "n": "D_N"}]}')
    assert rf.pair_nets(pcb) == {"D_P", "D_N"}
    rip: list = []
    _, jobs = _plan(pcb, [_pair("A", (5, 15), (25, 15),
                                "Pad 1 [A] of U1 on F.Cu",
                                "Pad 1 [A] of U2 on F.Cu")],
                    rip_board=True, ripped_out=rip,
                    keep_nets_extra=rf.pair_nets(pcb))
    assert jobs[0]["status"] == "failed"
    assert "ripped_nets" not in jobs[0]
    assert rip == []


def test_a_dru_layer_ban_holds_in_plan_routes(tmp_path):
    """#1140: under a full F.Cu wall SIG goes by B.Cu; a DRU rule that
    keeps it off B.Cu leaves no way, and one that keeps it off both
    layers fails it for that reason without a search."""
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    body += _seg(15, 0.1, 15, 29.9, 0.25, "F.Cu", "+3V3")
    off = ('(rule "off"\n\t(layer "{ly}")\n\t(constraint disallow track)\n'
           "\t(condition \"A.NetName == 'SIG'\")\n)\n")
    pcb = _board(tmp_path, body, dru=DRU + off.format(ly="B.Cu"))
    rules, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))])
    assert rules.net_layers("SIG", ["F.Cu", "B.Cu"]) == ["F.Cu"]
    assert rules.net_layers("+3V3", ["F.Cu", "B.Cu"]) == ["F.Cu", "B.Cu"]
    assert jobs[0]["status"] == "failed"
    assert not [op for op in jobs[0].get("ops", ())
                if op.get("layer") == "B.Cu"]
    facts = json.loads(json.dumps(rf._rules_facts(rules)))
    assert facts["bans"]["track"] == [
        {"nets": ["SIG"], "not": [], "classes": [], "not_classes": [],
         "layers": ["B.Cu"]}]
    pcb = _board(tmp_path, body, dru=DRU + off.format(ly="F.Cu")
                 + off.format(ly="B.Cu"))
    _, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))])
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["reason"] == ("the .kicad_dru keeps its tracks off every"
                                 " routable layer")


def test_rules_facts_of_an_hdi_board_are_json():
    """#477: route_finish crashed writing its report on an HDI build, after
    routing, because Rules.spans holds Span objects."""
    import json
    from types import SimpleNamespace
    import hdilib
    import route_finish
    span = hdilib.Span("micro", "F.Cu", "In1.Cu", 0, 1, 0.1, 0.25)
    rules = SimpleNamespace(spans=[span], grid=0.1, net_width={"A": 0.2})
    facts = route_finish._rules_facts(rules)
    assert json.loads(json.dumps(facts))["spans"][0]["kind"] == "micro"
    assert "net_width" not in facts


def test_a_pass_at_the_drc_cap_counts_its_routes():
    """KiCad reports at most 499 unconnected items: on a bigger board the total
    stays 499 while connections close, and every pass was thrown away."""
    from route_finish import DRC_UNCONNECTED_CAP as cap, _pass_helped
    routed, failed = [{"status": "routed"}], [{"status": "failed"}]
    assert _pass_helped(cap, cap, routed)
    assert not _pass_helped(cap, cap, failed)
    assert _pass_helped(40, 39, failed)
    assert not _pass_helped(40, 40, routed)  # below the cap the count is real


def test_a_short_run_between_vias_moves_to_the_next_layer():
    """hw-lab hdi-bga: a buried via and a microvia 0.14 mm apart on one net, under
    JLC's 0.13 mm hole spacing, which KiCad's DRC reported only some of the time."""
    from route_finish import merge_close_vias
    path = [(0, 0, 0), (5, 0, 0), (5, 0, 1), (6, 1, 1), (6, 1, 2), (9, 1, 2)]
    out = merge_close_vias(path, 3.0)
    assert [q for q in out if q[2] == 1] == [(5, 0, 1)]
    assert out[out.index((5, 0, 1)) + 1:] == [(5, 0, 2), (6, 1, 2), (6, 1, 2), (9, 1, 2)]
    # long runs, plane drops and the ends of the path stay as they are
    long = [(0, 0, 0), (0, 0, 1), (9, 0, 1), (9, 0, 2)]
    assert merge_close_vias(long, 3.0) == long
    drop = [(0, 0, 0), (0, 0, -1)]
    assert merge_close_vias(drop, 3.0) == drop
    # hw-lab hdi-bga: the search never looked at the moved via's cell. With no span
    # there, the move fell back to a through via on the heater and a coupon via.
    assert merge_close_vias(path, 3.0, lambda run, k2: False) == path


def test_a_keepout_inside_a_footprint_is_a_wall(tmp_path):
    """DRC enforces it, so a route through it only comes back rejected."""
    pts = "(xy 10 0.5) (xy 20 0.5) (xy 20 20) (xy 10 20)"
    keepout = (f'    (zone (net 0) (net_name "") (layer "F.Cu") (name "PATCH")\n'
               f'      (keepout (tracks not_allowed) (vias allowed) (pads allowed)'
               f' (copperpour not_allowed) (footprints allowed))\n'
               f'      (polygon (pts {pts})))\n')
    array = (f'  (footprint "t:AE1" (layer "F.Cu") (at 15 10)\n'
             f'    (property "Reference" "AE1" (at 0 0 0))\n{keepout}  )\n')
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG") + array
    pcb = _board(tmp_path, body)
    bg = geom.BoardGeom.from_file(pcb)
    assert [ra["name"] for ra in bg.footprint_rule_areas] == ["PATCH"]
    assert bg.rule_areas == []
    _, jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))], via_cost_mm=30, margin=8)
    assert jobs[0]["status"] == "routed" and jobs[0]["vias"] == 0
    area = box(10, 0.5, 20, 20)
    for op in _tracks(jobs[0]):
        assert LineString([op["start"], op["end"]]).distance(area) > 0


def test_a_large_margin_grid_stops_at_the_board():
    """Memory goes by the square of the window, and nothing routes off the board."""
    board = box(20, 20, 130, 92)
    assert rf._on_board((-180, -180, 330, 292), board) == (19, 19, 131, 93)
    assert rf._on_board((50, 50, 60, 60), board) == (50, 50, 60, 60)
    assert rf._on_board((-180, -180, 330, 292), None) == (-180, -180, 330, 292)
    assert rf._on_board((200, 200, 210, 210), board) == (200, 200, 210, 210)


def test_c_search_finds_the_same_path_as_python(monkeypatch):
    """route_astar.c is only a faster astar: same nodes, costs and tie-breaks."""
    if rf._load_astar() is None:
        pytest.skip("no C compiler")
    nx, ny, K = 60, 40, 2
    for seed in range(20):
        rng = np.random.default_rng(seed)
        blk = [(rng.random((ny, nx)) < 0.25) for _ in range(K)]
        in_a = [np.zeros((ny, nx), bool) for _ in range(K)]
        in_b = [np.zeros((ny, nx), bool) for _ in range(K)]
        in_a[0][20, 2] = True
        in_b[1][20, 57] = True
        for k in range(K):
            blk[k][in_a[k] | in_b[k]] = False
        def by(x):
            return x.ravel().astype(np.uint8).tobytes()
        zero = bytes(nx * ny)
        gr = types.SimpleNamespace(
            nx=nx, ny=ny, layers=["F.Cu", "B.Cu"], span_moves=None, gave_up=False,
            blk=[by(b) for b in blk], vblk=by(rng.random((ny, nx)) < 0.3),
            cost=[array("d", (1.0 + rng.random((ny, nx))).ravel().tobytes()) for _ in range(K)],
            in_a=[by(x) for x in in_a], in_b=[by(x) for x in in_b],
            a_plane=zero, b_plane=zero)
        fast = rf.astar(gr, (57, 20), search_weight=1.5)
        monkeypatch.setattr(rf, "_load_astar", lambda: None)
        slow = rf.astar(gr, (57, 20), search_weight=1.5)
        monkeypatch.undo()
        assert fast == slow


# ------------------------------------------------------- neck widths (#924)

NECK_DRU = DRU + """(rule "aiee_pwr_width_+3V3"
	(constraint track_width (min 1.0000mm))
	(condition "A.NetName == '+3V3' && A.Type == 'track'")
)
(rule "aiee_pwr_neck_3V3_U1"
	(constraint track_width (min 0.2500mm))
	(condition "A.NetName == '+3V3' && A.Type == 'track' && A.intersectsCourtyard('U1')")
)
"""


def _fine_pitch(ref, x, y, crtyd=1.2):
    """Five 0.25 x 0.8 pads at 0.5 pitch, the middle one +3V3, the rest GND:
    a 1 mm track can't leave the middle pad between its neighbours."""
    pads = "".join(
        f'    (pad "{n}" smd rect (at {(n - 3) * 0.5} 0) (size 0.25 0.8)'
        f' (layers "F.Cu") (net "{"+3V3" if n == 3 else "GND"}"))\n'
        for n in range(1, 6))
    return (f'  (footprint "t:{ref}" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            f'    (fp_rect (start -1.6 -{crtyd}) (end 1.6 {crtyd})'
            ' (stroke (width 0.05)) (layer "F.CrtYd"))\n'
            + pads + "  )\n")


def _neck_board(tmp_path, dru):
    body = _fine_pitch("U1", 10, 15) + _pad("U2", 10, 5, "+3V3", size=1.5)
    return _board(tmp_path, body, dru=dru)


def _neck_pair():
    return _pair("+3V3", (10, 15), (10, 5),
                 amsg="Pad 3 [+3V3] of U1 on F.Cu",
                 bmsg="Pad 1 [+3V3] of U2 on F.Cu")


def test_neck_rules_are_read_apart_from_the_net_floor(tmp_path):
    import route_critical as rc
    dru = tmp_path / "t.kicad_dru"
    dru.write_text(NECK_DRU, encoding="utf-8")
    assert rc.dru_net_floors(dru, "track_width") == {"SIG": 0.3, "+3V3": 1.0}
    assert rc.dru_neck_rules(dru) == [
        {"net": "+3V3", "ref": "U1", "min_mm": 0.25}]
    assert rc.dru_neck_rules(None) == []


def test_without_a_neck_rule_the_fine_pitch_pad_is_boxed_in(tmp_path):
    """The planted fault of #924: the net's full width everywhere."""
    pcb = _neck_board(tmp_path, NECK_DRU.split('(rule "aiee_pwr_neck')[0])
    rules, jobs = _plan(pcb, [_neck_pair()], grid=0.05, margin=4)
    assert not rules.necks
    assert jobs[0]["status"] != "routed"


def test_a_power_pad_leaves_its_courtyard_at_the_neck_width(tmp_path):
    pcb = _neck_board(tmp_path, NECK_DRU)
    rules, jobs = _plan(pcb, [_neck_pair()], grid=0.05, margin=4)
    assert set(rules.necks) == {"+3V3"} and set(rules.necks["+3V3"]) == {"F.Cu"}
    job = jobs[0]
    assert job["status"] == "routed", job
    tracks = _tracks(job)
    widths = {op["width"] for op in tracks}
    assert widths == {0.25, 1.001}
    zone = rules.necks["+3V3"]["F.Cu"][0][0]
    crtyd = box(8.4, 13.8, 11.6, 16.2)
    for op in tracks:
        seg = LineString([op["start"], op["end"]])
        if op["width"] == 0.25:
            # DRC's neck rule: the track meets the courtyard; and it is a
            # pad exit, inside check_current's 1 mm bound
            assert seg.intersects(crtyd)
            assert seg.distance(zone) <= 0.05 + 1e-6
        else:
            # the full width only where the full width was cleared
            assert not seg.intersects(zone.buffer(-0.05))
    neck_len = sum(LineString([o["start"], o["end"]]).length
                   for o in tracks if o["width"] == 0.25)
    assert neck_len <= 1.0
    route_edit.validate_ops({"version": 1, "ops": job["ops"]})


def test_straights_split_where_the_neck_ends():
    gr = rf.Grid(x0=0, y0=0, nx=10, ny=10, g=0.1, layers=["F.Cu"],
                 blk=[], vblk=b"", via=(0.6, 0.3), cost=[], in_a=[], in_b=[],
                 a_plane=b"", b_plane=b"",
                 neck={(0, 0): 0.2, (0, 1): 0.2})
    run = [(i, 0, 0) for i in range(5)] + [(4, 1, 0)]
    got = [(a[:2], b[:2], w) for a, b, w in rf._straights(gr, run, 0.8)]
    # the step off the last neck cell is still the neck's
    assert got == [((0, 0), (2, 0), 0.2), ((2, 0), (4, 0), 0.8),
                   ((4, 0), (4, 1), 0.8)]


# ------------------------------------------------------- fine pitch (#923)

BGA_DRU = """(version 1)
(rule "aiee_clearance_floor"
	(constraint clearance (min 0.0750mm))
)
(rule "aiee_track_width_floor"
	(constraint track_width (min 0.0762mm))
)
"""


def _bga(ref, x, y):
    """A 3 x 3 corner of a 0.5 mm-pitch BGA, 0.25 mm pads: the middle ball
    +3V3, its ring GND. Every way out runs between two ring balls 0.25 mm
    apart."""
    pads = "".join(
        f'    (pad "{3 * r + c + 1}" smd rect (at {(c - 1) * 0.5} {(r - 1) * 0.5})'
        f' (size 0.25 0.25) (layers "F.Cu")'
        f' (net "{"+3V3" if (r, c) == (1, 1) else "GND"}"))\n'
        for r in range(3) for c in range(3))
    return (f'  (footprint "t:{ref}" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            + pads + "  )\n")


def _bga_plan(tmp_path):
    body = _bga("U1", 10, 15) + _pad("U2", 10, 5, "+3V3", size=1.5)
    pcb = _board(tmp_path, body, dru=BGA_DRU)
    pcb.with_suffix(".kicad_pro").write_text(json.dumps({"board": {
        "design_settings": {"rules": {"min_track_width": 0.0762}}}}),
        encoding="utf-8")
    return _plan(pcb, [_pair("+3V3", (10, 15), (10, 5),
                              amsg="Pad 5 [+3V3] of U1 on F.Cu",
                              bmsg="Pad 1 [+3V3] of U2 on F.Cu")])


def test_the_fine_rung_takes_a_ball_out_between_its_neighbours(tmp_path):
    """#923 point 3: at the default grid and clearance margin a 0.077 mm
    floor track needs 0.077 + 2 x (0.075 + 0.02) = 0.267 mm between two
    balls 0.25 mm apart. The fine rung's margin lets it through."""
    rules, jobs = _bga_plan(tmp_path)
    assert rules.margin == 0.02 and rules.track_floor == 0.0762
    job = jobs[0]
    assert job["status"] == "routed", job
    tracks = _tracks(job)
    assert {op["width"] for op in tracks} == {rules.width_floor("+3V3")}
    # every run keeps DRC's clearance (not the margin) off the GND balls
    balls = [box(10 + c * 0.5 - 0.125, 15 + r * 0.5 - 0.125,
                 10 + c * 0.5 + 0.125, 15 + r * 0.5 + 0.125)
             for r in (-1, 0, 1) for c in (-1, 0, 1) if (r, c) != (0, 0)]
    for op in tracks:
        seg = LineString([op["start"], op["end"]])
        for b in balls:
            assert seg.distance(b) - op["width"] / 2 >= 0.075 - 1e-6
    route_edit.validate_ops({"version": 1, "ops": job["ops"]})


def test_at_the_coarse_margin_the_ball_is_boxed_in(tmp_path, monkeypatch):
    """The planted fault: the fine rung at the global 0.02 mm margin."""
    monkeypatch.setattr(rf, "FINE_MARGIN_MM", 0.02)
    _, jobs = _bga_plan(tmp_path)
    assert jobs[0]["status"] != "routed"


# ----------------------------------------------- #682: pair and courtyard rules

HV_DRU = DRU + """(rule "aiee_hv_SIG"
	(constraint clearance (min 0.5000mm))
	(condition "A.NetName == 'SIG'")
)
"""

HV_WAIVER = """(rule "aiee_hv_waived_SIG_GND"
	(constraint clearance (min 0.1500mm))
	(condition "A.NetName == 'SIG' && B.NetName == 'GND'")
)
"""

HV_COURT = """(rule "aiee_pkg_rating_U1"
	(constraint clearance (min 0.1200mm))
	(condition "A.intersectsCourtyard('U1') && B.intersectsCourtyard('U1') && A.Type != 'Zone' && B.Type != 'Zone'")
)
"""


def _hv_part(ref, x, y):
    """_fine_pitch with the middle pad SIG: 0.25 mm from its GND
    neighbours, inside SIG's 0.5 mm HV clearance."""
    return _fine_pitch(ref, x, y).replace('"+3V3"', '"SIG"')


def _hv_board(tmp_path, dru):
    # a +3V3 pad beside the way out, which SIG's 0.5 mm still holds off
    body = (_hv_part("U1", 10, 15) + _pad("U2", 10, 5, "SIG")
            + _pad("U3", 11.6, 10, "+3V3"))
    return _board(tmp_path, body, dru=dru)


def _hv_pair():
    return _pair("SIG", (10, 15), (10, 5), amsg="Pad 3 [SIG] of U1 on F.Cu",
                 bmsg="Pad 1 [SIG] of U2 on F.Cu")


def _gaps(job, bg, net):
    """min gap of the job's tracks to each other net's pads."""
    out: dict = {}
    for op in _tracks(job):
        seg = LineString([op["start"], op["end"]]).buffer(op["width"] / 2)
        for p in rf.Obstacles(bg).items:
            if p.kind != "pad" or p.net in (net, ""):
                continue
            d = seg.distance(p.shape) - p.half
            out[p.net] = min(out.get(p.net, 9.0), d)
    return out


@pytest.mark.parametrize("extra", [HV_WAIVER, HV_COURT],
                         ids=["waiver", "courtyard"])
def test_a_pair_or_courtyard_rule_frees_an_hv_fine_pitch_escape(tmp_path,
                                                                extra):
    """The planted fault of #682: the HV value read net-wide walls the pad
    in; the later waiver or package rule DRC reads lets it out, and the
    route holds that rule's value, no less, and 0.5 mm to +3V3."""
    pcb = _hv_board(tmp_path, HV_DRU + extra)
    rules, jobs = _plan(pcb, [_hv_pair()], grid=0.05, margin=4)
    job = jobs[0]
    assert job["status"] == "routed", job
    assert rules.net_clr("SIG", "GND") == 0.5 and rules.pair is not None
    bg = geom.BoardGeom.from_file(pcb)
    gaps = _gaps(job, bg, "SIG")
    low = 0.15 if extra is HV_WAIVER else 0.12
    assert gaps["GND"] >= low - 1e-6 and gaps["GND"] < 0.5
    assert gaps.get("+3V3", 9.0) >= 0.5 - 1e-6
    route_edit.validate_ops({"version": 1, "ops": job["ops"]})


def test_an_hv_pad_with_no_waiver_is_walled_in(tmp_path):
    """The quiet twin: no rule lowers SIG to GND, so the pad stays failed,
    and the reason says it is walled in, by what, at what value."""
    pcb = _hv_board(tmp_path, HV_DRU)
    rules, jobs = _plan(pcb, [_hv_pair()], grid=0.05, margin=4)
    assert rules.pair is None
    job = jobs[0]
    assert job["status"] == "failed" and job.get("walled"), job
    assert job["reason"] == ("no path: U1 pad 3 walled in: SIG held 0.5 mm"
                             " (rule aiee_hv_SIG) from GND"), job["reason"]
    assert "via_in_pad" not in job and "rip_uuids" not in job


def test_a_pad_boxed_in_by_tracks_is_not_walled_in(tmp_path):
    """A pad held only by tracks that may move is no wall: the old reason."""
    body = _pad("U1", 10, 15, "SIG", size=0.4) + _pad("U2", 10, 5, "SIG")
    for y in (14.2, 15.8):
        body += _seg(8, y, 12, y, 0.3, "F.Cu", "GND", uuid=f"g{y}")
    for x in (9.2, 10.8):
        body += _seg(x, 13, x, 17, 0.3, "F.Cu", "GND", uuid=f"h{x}")
    one = HDR.replace('(2 "B.Cu" signal) ', "")
    p = tmp_path / "t.kicad_pcb"
    p.write_text(one + '  (gr_rect (start 0 0) (end 30 30) (stroke (width'
                 ' 0.1)) (fill no) (layer "Edge.Cuts"))\n' + body + ")\n",
                 encoding="utf-8")
    p.with_suffix(".kicad_dru").write_text(DRU, encoding="utf-8")
    rules, jobs = _plan(p, [_pair("SIG", (10, 15), (10, 5))], grid=0.1,
                        margin=4)
    job = jobs[0]
    assert job["status"] != "routed"
    assert not job.get("walled") and "walled" not in job["reason"]


def test_pair_rules_resolve_the_last_rule_that_holds(tmp_path):
    pcb = _hv_board(tmp_path, HV_DRU + HV_WAIVER + HV_COURT)
    bg = geom.BoardGeom.from_file(pcb)
    rules = rf.board_rules(pcb, bg)
    pr = rules.pair
    obs = rf.Obstacles(bg)
    pads = {(it.ref, it.number): it for it in obs.items if it.kind == "pad"}
    gnd, v33 = pads[("U1", "2")], pads[("U3", "1")]
    out = rf.Probe("SIG", "track", (), {"F.Cu"})
    inside = rf.Probe("SIG", "track", pr.courts_of(pads[("U1", "3")]),
                      {"F.Cu"})
    # the waiver: SIG to GND anywhere; the courtyard rule, last, wins inside
    assert pr.value(out, gnd) == (0.15, "aiee_hv_waived_SIG_GND")
    assert pr.value(inside, gnd) == (0.12, "aiee_pkg_rating_U1")
    # the +3V3 pad is outside U1's courtyard: the package rule can't hold
    assert pr.value(inside, v33) == (0.5, None)
    # no rule for SIG to +3V3: the net-wide HV value
    assert pr.value(out, v33) == (0.5, None)
    assert rules.item_clr(out, v33) == pytest.approx(0.5 + rules.margin)
    # a via is no track: the waiver names no type, so it holds for one too
    via = rf.Probe("SIG", "via", (), {"F.Cu", "B.Cu"})
    assert pr.value(via, gnd)[0] == 0.15


# ------------------------- #1484: a BGA escape's courtyard clearance rule

COURT_DRU = """(version 1)
(rule "aiee_bga_escape_U1"
	(constraint clearance (min 0.1000mm))
	(condition "A.intersectsCourtyard('U1') || B.intersectsCourtyard('U1')")
)
"""


def _slot_part(ref, x, y, gap):
    """SIG pad 1 boxed in by GND walls, a `gap` slot out of the right wall,
    all on a 4 x 4 courtyard."""
    walls = [(0, -0.6, 2.6, 0.3), (0, 0.6, 2.6, 0.3), (-1.15, 0, 0.3, 1.5)]
    h = (1.5 - gap) / 2
    walls += [(1.15, -(gap + h) / 2, 0.3, h), (1.15, (gap + h) / 2, 0.3, h)]
    pads = '    (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu")' \
        ' (net "SIG"))\n'
    pads += "".join(
        f'    (pad "{n}" smd rect (at {wx} {wy}) (size {sx} {sy})'
        f' (layers "F.Cu") (net "GND"))\n'
        for n, (wx, wy, sx, sy) in enumerate(walls, start=2))
    return (f'  (footprint "t:{ref}" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            '    (fp_rect (start -2 -2) (end 2 2)'
            ' (stroke (width 0.05)) (layer "F.CrtYd"))\n'
            + pads + "  )\n")


def _slot_plan(tmp_path, dru):
    # 0.2 + 2 * 0.12 = 0.44 fits the 0.5 slot; 0.2 + 2 * 0.22 does not
    pcb = _board(tmp_path, _slot_part("U1", 10, 15, 0.5)
                 + _pad("U2", 17, 15, "SIG", size=0.5), dru=dru)
    bg = geom.BoardGeom.from_file(pcb)
    rules = rf.board_rules(pcb, bg)
    pair = _pair("SIG", (10, 15), (17, 15))
    return bg, rf.plan_routes(bg, rules, [pair], ["F.Cu"],
                              grid=0.05, margin=4), rules


def test_without_the_courtyard_rule_the_slot_is_shut(tmp_path):
    _, jobs, _ = _slot_plan(tmp_path, "(version 1)\n")
    assert jobs[0]["status"] != "routed"


def test_a_bga_escape_courtyard_rule_routes_through_the_slot(tmp_path):
    """#1484: the BGA escape's 0.1 mm courtyard rule on a signal net with no
    HV rating, which the maze held at the 0.2 netclass, so U1's ring-2 balls
    stayed unrouted; #682's per-item rules read it."""
    bg, jobs, _ = _slot_plan(tmp_path, COURT_DRU)
    job = jobs[0]
    assert job["status"] == "routed", job
    gnd = [p.poly for p in bg.pads_of("GND", "F.Cu")]
    near = min(LineString([op["start"], op["end"]]).buffer(
        op["width"] / 2).distance(g) for op in _tracks(job) for g in gnd)
    assert 0.1 <= near < 0.2
    route_edit.validate_ops({"version": 1, "ops": job["ops"]})


def test_rules_facts_with_a_courtyard_rule_are_json(tmp_path):
    """#1501: a Rules carrying #682's PairRules still writes a JSON report;
    route_auto lost a 19-minute route to this at the very end."""
    _, _, rules = _slot_plan(tmp_path, COURT_DRU)
    assert rules.pair is not None
    facts = json.loads(json.dumps(rf._rules_facts(rules)))
    assert facts["pair"] == ["aiee_bga_escape_U1"]
