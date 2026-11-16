"""T2 regression tests: netconn shared connectivity graph + check_diffpair.

Locks the LEARNINGS 2026-07-29 [check_diffpair][gates] fixes:
  - the per-net graph now connects through vias, pads and overlapping
    same-layer end caps (the 0.1414 / 0.2229 mm lumina-carrier cases), so a
    sub-0.3 mm endpoint mismatch no longer turns "skew" into total copper;
  - an open trunk is a diffpair_open_trunk WARNING, not a silent
    branch_free:false;
  - matched_terminals pairs SAME-REF pads with no distance cap (magjack
    RXP/RXN pads sit 4.58 mm apart), window overridable via "term_pair_mm".
Plus unit coverage for netconn: join rules, Dijkstra, bridge (cut-edge)
finding on multigraphs and zone-paralleled segments.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_diffpair  # noqa: E402
import geom  # noqa: E402
import netconn  # noqa: E402


# ---- synthetic board helpers (mirrors test_checks.py; outline adjustable so
# ---- fixtures can carry the exact LEARNINGS coordinates, x ~96 / y ~82) ----

def _board(tmp_path_factory, name: str, body: str,
           outline=(0, 0, 20, 10)) -> geom.BoardGeom:
    x0, y0, x1, y1 = outline
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path_factory.mktemp(name) / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return geom.load_board(p)


def seg(x1, y1, x2, y2, net="N", layer="F.Cu", w=0.25) -> str:
    return (f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) '
            f'(layer "{layer}") (net "{net}"))\n')


def via(x, y, net="N", size=0.6, drill=0.3) -> str:
    return (f'  (via (at {x} {y}) (size {size}) (drill {drill}) '
            f'(layers "F.Cu" "B.Cu") (net "{net}"))\n')


def pt(x, y):
    return ("pt", (round(x, 3), round(y, 3)))


# ============================================================ netconn: joins

def test_netconn_coincident_snap_same_layer(tmp_path_factory):
    body = seg(1, 5, 5, 5) + seg(5, 5, 9, 5)
    bg = _board(tmp_path_factory, "snapsame", body)
    g = netconn.build(bg, "N")
    assert netconn.shortest_path_len(g, pt(1, 5), pt(9, 5)) == pytest.approx(8.0)


def test_netconn_coincident_snap_cross_layer(tmp_path_factory):
    # exact-coincident endpoints connect even across layers (snap is
    # layer-agnostic - preserves the pre-netconn behavior)
    body = seg(1, 5, 5, 5, layer="F.Cu") + seg(5, 5, 9, 5, layer="B.Cu")
    bg = _board(tmp_path_factory, "snapcross", body)
    g = netconn.build(bg, "N")
    assert netconn.shortest_path_len(g, pt(1, 5), pt(9, 5)) == pytest.approx(8.0)


def test_netconn_cap_overlap_joins_lumina_gap(tmp_path_factory):
    # /ETH_TXN's B.Cu break: (93.95, 83.1851) vs (93.80, 83.35) = 0.2229 mm
    # apart, 0.26 mm tracks -> (w1+w2)/2 + tol = 0.31, caps overlap, connected
    body = (seg(88, 83.5, 93.95, 83.1851, layer="B.Cu", w=0.26)
            + seg(93.80, 83.35, 100, 83.5, layer="B.Cu", w=0.26))
    bg = _board(tmp_path_factory, "capjoin", body, outline=(80, 76, 105, 95))
    g = netconn.build(bg, "N")
    L = netconn.shortest_path_len(g, pt(88, 83.5), pt(100, 83.5))
    assert L == pytest.approx(12.383, abs=0.01)   # segA + 0.2229 join + segB


def test_netconn_cap_overlap_not_across_layers(tmp_path_factory):
    body = (seg(88, 83.5, 93.95, 83.1851, layer="B.Cu", w=0.26)
            + seg(93.80, 83.35, 100, 83.5, layer="F.Cu", w=0.26))
    bg = _board(tmp_path_factory, "capxlayer", body, outline=(80, 76, 105, 95))
    g = netconn.build(bg, "N")
    assert netconn.shortest_path_len(g, pt(88, 83.5), pt(100, 83.5)) is None


def test_netconn_cap_overlap_distance_cap(tmp_path_factory):
    # 0.4 mm gap > (0.26 + 0.26)/2 + 0.05 = 0.31 -> caps do not overlap
    body = seg(1, 5, 5, 5, w=0.26) + seg(5.4, 5, 9, 5, w=0.26)
    bg = _board(tmp_path_factory, "capfar", body)
    g = netconn.build(bg, "N")
    assert netconn.shortest_path_len(g, pt(1, 5), pt(9, 5)) is None


VIA_MISMATCH = (seg(90, 81.85, 96.15, 81.85, net="/ETH_RXP", layer="F.Cu")
                + seg(96.05, 81.95, 98, 82, net="/ETH_RXP", layer="B.Cu"))


def test_netconn_via_join_endpoint_mismatch(tmp_path_factory):
    # the 0.1414 mm lumina case: F.Cu end at the via center, B.Cu start
    # 0.1414 mm away - joined only through the via disk
    body = VIA_MISMATCH + via(96.15, 81.85, net="/ETH_RXP")
    bg = _board(tmp_path_factory, "viajoin", body, outline=(80, 76, 105, 95))
    g = netconn.build(bg, "/ETH_RXP")
    L = netconn.shortest_path_len(g, pt(90, 81.85), pt(98, 82))
    assert L == pytest.approx(8.242, abs=0.01)    # 6.15 + 0 + 0.1414 + 1.9506
    # both segments are still cut edges of the joined chain
    assert netconn.bridge_tracks(g) == {0, 1}


def test_netconn_no_via_disconnected(tmp_path_factory):
    bg = _board(tmp_path_factory, "novia", VIA_MISMATCH,
                outline=(80, 76, 105, 95))
    g = netconn.build(bg, "/ETH_RXP")
    assert netconn.shortest_path_len(g, pt(90, 81.85), pt(98, 82)) is None


def test_netconn_pad_join_and_pad_node(tmp_path_factory):
    body = ('  (footprint "t:U" (at 5 5) (layer "F.Cu")\n'
            '    (property "Reference" "U9" (at 0 0 0) (layer "F.SilkS"))\n'
            '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "N"))\n'
            '    (pad "2" smd rect (at 0 3) (size 1 1) (layers "F.Cu") (net "M")))\n'
            + seg(5.3, 5.2, 9, 5))
    bg = _board(tmp_path_factory, "padjoin", body)
    g = netconn.build(bg, "N")
    pn = netconn.pad_node(g, bg.pads_of(net="N")[0])
    assert pn == ("pad", "U9", "1")
    # endpoint (5.3, 5.2) inside the 1x1 pad -> joined at weight |ep - center|
    L = netconn.shortest_path_len(g, pn, pt(9, 5))
    assert L == pytest.approx(4.066, abs=0.01)    # 0.3606 + 3.7054
    # a pad on another net is not in this graph
    assert netconn.pad_node(g, bg.pads_of(net="M")[0]) is None


def test_netconn_zero_length_segment_skipped(tmp_path_factory):
    body = seg(3, 3, 3, 3) + seg(1, 5, 5, 5) + seg(5, 5, 9, 5)
    bg = _board(tmp_path_factory, "zerolen", body)
    g = netconn.build(bg, "N")
    assert 0 not in g.tracks                       # zero-length: no edge
    assert netconn.bridge_tracks(g) == {1, 2}


# ============================================================ netconn: bridges

def test_netconn_bridges_chain(tmp_path_factory):
    body = seg(2, 2, 8, 2) + seg(8, 2, 8, 6) + seg(8, 6, 12, 6)
    bg = _board(tmp_path_factory, "brchain", body)
    assert netconn.bridge_tracks(netconn.build(bg, "N")) == {0, 1, 2}


LOOP = (seg(2, 2, 8, 2) + seg(2, 2, 2, 6) + seg(2, 6, 8, 6) + seg(8, 6, 8, 2))


def test_netconn_bridges_loop(tmp_path_factory):
    bg = _board(tmp_path_factory, "brloop", LOOP)
    assert netconn.bridge_tracks(netconn.build(bg, "N")) == set()


def test_netconn_bridges_loop_with_tail(tmp_path_factory):
    bg = _board(tmp_path_factory, "brtail", LOOP + seg(8, 2, 12, 2))
    assert netconn.bridge_tracks(netconn.build(bg, "N")) == {4}


def test_netconn_bridges_parallel_duplicate_edges(tmp_path_factory):
    # two segments between the SAME node pair are parallel edges - neither is
    # a bridge (multi-edge identity is by edge_id, not by neighbor node)
    body = seg(2, 2, 8, 2, w=0.25) + seg(2, 2, 8, 2, w=0.4)
    bg = _board(tmp_path_factory, "brdup", body)
    assert netconn.bridge_tracks(netconn.build(bg, "N")) == set()


def test_netconn_zone_parallel_not_bridge(tmp_path_factory):
    body = (seg(2, 5, 8, 5)
            + '  (zone (net "N") (layer "F.Cu")\n'
              '    (polygon (pts (xy 1 4) (xy 9 4) (xy 9 6) (xy 1 6)))\n'
              '    (filled_polygon (layer "F.Cu")\n'
              '      (pts (xy 1 4) (xy 9 4) (xy 9 6) (xy 1 6))))\n')
    bg = _board(tmp_path_factory, "brzone", body)
    # the pour parallels the segment: with zones it is NOT a cut edge
    assert netconn.bridge_tracks(netconn.build(bg, "N", include_zones=True)) == set()
    assert netconn.bridge_tracks(netconn.build(bg, "N")) == {0}


# ============================================================ check_diffpair
# fixture 1: exact LEARNINGS geometry - /ETH_RXP F.Cu escape ends (96.15,
# 81.85), via there, B.Cu continuation starts (96.05, 81.95) [0.1414 mm],
# plus a 3.9 mm test stub so total copper != trunk; /ETH_RXN contiguous.

def _rx_body(with_via: bool) -> str:
    body = (
        '  (footprint "t:U" (at 82 82) (layer "F.Cu")\n'
        '    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net "/ETH_RXP"))\n'
        '    (pad "2" smd rect (at 0 0.6) (size 0.5 0.5) (layers "F.Cu") (net "/ETH_RXN")))\n'
        '  (footprint "t:J" (at 98 82) (layer "F.Cu")\n'
        '    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" thru_hole circle (at 0 0) (size 1.5 1.5) (drill 0.9) (layers "*.Cu") (net "/ETH_RXP"))\n'
        '    (pad "2" thru_hole circle (at 0 2) (size 1.5 1.5) (drill 0.9) (layers "*.Cu") (net "/ETH_RXN")))\n'
        + seg(82, 82, 90, 81.9, net="/ETH_RXP")
        + seg(90, 81.9, 96.15, 81.85, net="/ETH_RXP")
        + seg(90, 81.9, 90, 78, net="/ETH_RXP")            # test-point stub
        + seg(96.05, 81.95, 98, 82, net="/ETH_RXP", layer="B.Cu")
        + seg(82, 82.6, 96, 82.6, net="/ETH_RXN")
        + seg(96, 82.6, 98, 84, net="/ETH_RXN"))
    if with_via:
        body += via(96.15, 81.85, net="/ETH_RXP")
    return body


def test_diffpair_via_mismatch_true_trunk_skew(tmp_path_factory):
    bg = _board(tmp_path_factory, "rxvia", _rx_body(True),
                outline=(78, 76, 104, 96))
    vs, facts = check_diffpair.check_pair(bg, {"p": "/ETH_RXP", "n": "/ETH_RXN"})
    assert facts["branch_free"] is True
    # TRUE trunk skew (16.243 vs 16.441), NOT total-copper (would be ~3.56)
    assert facts["skew_mm"] == pytest.approx(0.198, abs=0.01)
    assert facts["length_p_mm"] == pytest.approx(16.243, abs=0.01)
    kinds = {v["kind"] for v in vs}
    assert "diffpair_skew" not in kinds
    assert "diffpair_open_trunk" not in kinds
    assert kinds == {"diffpair_via_asymmetry"}     # 1 via on P, 0 on N


def test_diffpair_via_removed_open_trunk(tmp_path_factory):
    bg = _board(tmp_path_factory, "rxnovia", _rx_body(False),
                outline=(78, 76, 104, 96))
    vs, facts = check_diffpair.check_pair(bg, {"p": "/ETH_RXP", "n": "/ETH_RXN"})
    assert facts["branch_free"] is False
    open_vs = [v for v in vs if v["kind"] == "diffpair_open_trunk"]
    assert len(open_vs) == 1 and len(vs) == 1
    v = open_vs[0]
    assert v["severity"] == "warning" and v["net"] == "/ETH_RXP"
    assert v["pair"] == ["/ETH_RXP", "/ETH_RXN"]
    assert v["fallback_length_mm"] == pytest.approx(20.001, abs=0.01)
    assert "U1.1" in v["msg"] and "J1.1" in v["msg"]


# fixture 2: /ETH_TXN's 0.2229 mm B.Cu end-cap gap (0.26 mm tracks), no via -
# connected through the cap overlap, so the pair stays branch_free.

def test_diffpair_cap_gap_connected(tmp_path_factory):
    body = (
        '  (footprint "t:U" (at 88 83) (layer "F.Cu")\n'
        '    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" thru_hole circle (at 0 0) (size 0.8 0.8) (drill 0.4) (layers "*.Cu") (net "/ETH_TXP"))\n'
        '    (pad "2" thru_hole circle (at 0 1) (size 0.8 0.8) (drill 0.4) (layers "*.Cu") (net "/ETH_TXN")))\n'
        '  (footprint "t:J" (at 100 83) (layer "F.Cu")\n'
        '    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" thru_hole circle (at 0 0) (size 0.8 0.8) (drill 0.4) (layers "*.Cu") (net "/ETH_TXP"))\n'
        '    (pad "2" thru_hole circle (at 0 1) (size 0.8 0.8) (drill 0.4) (layers "*.Cu") (net "/ETH_TXN")))\n'
        + seg(88, 83, 100, 83, net="/ETH_TXP", layer="B.Cu", w=0.26)
        + seg(88, 84, 93.95, 83.1851, net="/ETH_TXN", layer="B.Cu", w=0.26)
        + seg(93.80, 83.35, 100, 84, net="/ETH_TXN", layer="B.Cu", w=0.26))
    bg = _board(tmp_path_factory, "txcap", body, outline=(80, 76, 105, 95))
    vs, facts = check_diffpair.check_pair(bg, {"p": "/ETH_TXP", "n": "/ETH_TXN"})
    assert facts["branch_free"] is True
    assert facts["skew_mm"] == pytest.approx(0.462, abs=0.01)
    assert vs == []


# fixture 3: magjack spacing - J1's P/N pads 4.58 mm apart (LPJG0926HENL
# RXP/RXN), U1's 0.5 mm apart. Same-ref pairing makes J1 a matched terminal,
# so the trunk includes the connector escape the old window never measured.

MAGJACK = (
    '  (footprint "t:U" (at 82 84) (layer "F.Cu")\n'
    '    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
    '    (pad "1" smd rect (at 0 0) (size 0.4 0.4) (layers "F.Cu") (net "/MDI_P"))\n'
    '    (pad "2" smd rect (at 0 0.5) (size 0.4 0.4) (layers "F.Cu") (net "/MDI_N")))\n'
    '  (footprint "t:J" (at 96 84) (layer "F.Cu")\n'
    '    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))\n'
    '    (pad "1" thru_hole circle (at 0 0) (size 1.5 1.5) (drill 0.9) (layers "*.Cu") (net "/MDI_P"))\n'
    '    (pad "2" thru_hole circle (at 0 4.58) (size 1.5 1.5) (drill 0.9) (layers "*.Cu") (net "/MDI_N")))\n'
    # cross-ref singles 3.0 mm apart (exercise the term_pair_mm window)
    '  (footprint "t:RP" (at 85 90) (layer "F.Cu")\n'
    '    (property "Reference" "RP1" (at 0 0 0) (layer "F.SilkS"))\n'
    '    (pad "1" smd rect (at 0 0) (size 0.4 0.4) (layers "F.Cu") (net "/MDI_P")))\n'
    '  (footprint "t:RN" (at 88 90) (layer "F.Cu")\n'
    '    (property "Reference" "RN1" (at 0 0 0) (layer "F.SilkS"))\n'
    '    (pad "1" smd rect (at 0 0) (size 0.4 0.4) (layers "F.Cu") (net "/MDI_N")))\n'
    + seg(82, 84, 96, 84, net="/MDI_P")
    + seg(82, 84.5, 92, 84.5, net="/MDI_N")
    + seg(92, 84.5, 92, 88.58, net="/MDI_N")               # connector escape
    + seg(92, 88.58, 96, 88.58, net="/MDI_N"))


def test_diffpair_magjack_same_ref_terminals(tmp_path_factory):
    bg = _board(tmp_path_factory, "magjack", MAGJACK, outline=(78, 78, 102, 94))
    term = check_diffpair.matched_terminals(bg, "/MDI_P", "/MDI_N")
    assert {pp.ref for pp, nn in term} == {"U1", "J1"}     # J1 matched: 4.58 mm
    j1 = next((pp, nn) for pp, nn in term if pp.ref == "J1")
    assert math.hypot(j1[0].center[0] - j1[1].center[0],
                      j1[0].center[1] - j1[1].center[1]) == pytest.approx(4.58)

    vs, facts = check_diffpair.check_pair(
        bg, {"p": "/MDI_P", "n": "/MDI_N", "max_uncoupled_mm": 20})
    assert facts["branch_free"] is True                    # old window: False
    # trunk now includes the 8.08 mm J1 escape (old behavior never saw it)
    assert facts["length_n_mm"] == pytest.approx(18.08, abs=0.01)
    assert facts["skew_mm"] == pytest.approx(4.08, abs=0.01)
    assert not any(v["kind"] == "diffpair_open_trunk" for v in vs)


def test_diffpair_term_window_override(tmp_path_factory):
    bg = _board(tmp_path_factory, "magwin", MAGJACK, outline=(78, 78, 102, 94))
    # default 2.5 mm window: the 3.0 mm cross-ref pair RP1/RN1 stays unmatched
    assert len(check_diffpair.matched_terminals(bg, "/MDI_P", "/MDI_N")) == 2
    # widened window picks it up; same-ref pairs are unaffected either way
    term = check_diffpair.matched_terminals(bg, "/MDI_P", "/MDI_N", 3.5)
    assert len(term) == 3
    assert {pp.ref for pp, nn in term} == {"U1", "J1", "RP1"}
    # spec key "term_pair_mm" reaches check_pair; same-ref pairing has NO cap,
    # so even a tiny window keeps U1/J1 matched and the trunk closed
    vs, facts = check_diffpair.check_pair(
        bg, {"p": "/MDI_P", "n": "/MDI_N", "max_uncoupled_mm": 20,
             "term_pair_mm": 0.1})
    assert facts["branch_free"] is True
    assert facts["length_n_mm"] == pytest.approx(18.08, abs=0.01)


# ============================================================ impedance as drawn

def _straight_pair(tmp_path_factory, name, w=0.3, pitch=0.6):
    body = (seg(2, 5, 18, 5, net="/D_P", w=w)
            + seg(2, 5 + pitch, 18, 5 + pitch, net="/D_N", w=w))
    return _board(tmp_path_factory, name, body)


def test_diffpair_impedance_reported_and_on_target(tmp_path_factory):
    bg = _straight_pair(tmp_path_factory, "zok")
    _, facts = check_diffpair.check_pair(bg, {"p": "/D_P", "n": "/D_N"})
    z = facts["impedance"]
    assert z["signal_layer"] == "F.Cu" and z["model"] == "coated microstrip"
    assert z["width_mm"] == 0.3 and z["gap_mm"] == pytest.approx(0.3, abs=1e-3)
    # 2-layer 1.6 mm board: the plane is far, so Zdiff is well above 90
    assert 100 < z["zdiff_ohm"] < 200
    vs, _ = check_diffpair.check_pair(bg, {"p": "/D_P", "n": "/D_N",
                                           "impedance_ohm": z["zdiff_ohm"] * 1.05})
    assert "diffpair_impedance" not in {v["kind"] for v in vs}   # 5% < 10%


def test_diffpair_impedance_miss_is_flagged(tmp_path_factory):
    bg = _straight_pair(tmp_path_factory, "zmiss")
    _, facts = check_diffpair.check_pair(bg, {"p": "/D_P", "n": "/D_N"})
    zd = facts["impedance"]["zdiff_ohm"]
    vs, _ = check_diffpair.check_pair(bg, {"p": "/D_P", "n": "/D_N",
                                           "impedance_ohm": zd / 1.15})
    hit = [v for v in vs if v["kind"] == "diffpair_impedance"]
    assert len(hit) == 1 and hit[0]["severity"] == "warning"
    vs, _ = check_diffpair.check_pair(bg, {"p": "/D_P", "n": "/D_N", "impedance_ohm": 90})
    hit = [v for v in vs if v["kind"] == "diffpair_impedance"]
    assert len(hit) == 1 and hit[0]["severity"] == "error"


# ---- DC Kelvin / low-frequency sense pairs (raised-hwde-model-gaps-pcb0026 c)

def _skewed_pair(tmp_path_factory, name, extra=""):
    """P short and straight; N runs 3 mm away and detours: skewed and uncoupled."""
    def fp(ref, x):
        return (f'  (footprint "t:{ref}" (at {x} 2) (layer "F.Cu")\n'
                f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
                '    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net "/ISNS_P"))\n'
                '    (pad "2" smd rect (at 0 4) (size 0.5 0.5) (layers "F.Cu") (net "/ISNS_N")))\n')
    body = (fp("R1", 2) + fp("U1", 18)
            + seg(2, 2, 18, 2, net="/ISNS_P")
            + seg(2, 6, 2, 10, net="/ISNS_N") + seg(2, 10, 18, 10, net="/ISNS_N")
            + seg(18, 10, 18, 6, net="/ISNS_N") + extra)
    return _board(tmp_path_factory, name, body, outline=(0, 0, 20, 14))


def _kinds(vs):
    return {v["kind"] for v in vs}


def test_diffpair_plain_pair_still_gets_skew_and_uncoupled(tmp_path_factory):
    bg = _skewed_pair(tmp_path_factory, "skp")
    vs, facts = check_diffpair.check_pair(
        bg, {"p": "/ISNS_P", "n": "/ISNS_N", "gap_mm": 0.2})
    assert {"diffpair_skew", "diffpair_uncoupled"} <= _kinds(vs)
    assert facts["kind"] == "diff"


@pytest.mark.parametrize("kind", ["sense", "kelvin", "DC_Sense", "low_frequency"])
def test_diffpair_sense_kind_skips_skew_uncoupled_impedance(tmp_path_factory, kind):
    bg = _skewed_pair(tmp_path_factory, "sk" + kind.lower())
    vs, facts = check_diffpair.check_pair(
        bg, {"p": "/ISNS_P", "n": "/ISNS_N", "gap_mm": 0.2, "kind": kind,
             "max_skew_mm": 3, "max_uncoupled_mm": 3, "impedance_ohm": 90})
    assert _kinds(vs) == set()
    assert facts["kind"] == "sense" and facts["skew_mm"] > 3   # still reported


def test_diffpair_sense_keeps_via_asymmetry_and_open_trunk(tmp_path_factory):
    bg = _skewed_pair(tmp_path_factory, "skv", extra=via(10, 10, net="/ISNS_N"))
    vs, _ = check_diffpair.check_pair(
        bg, {"p": "/ISNS_P", "n": "/ISNS_N", "kind": "sense"})
    assert "diffpair_via_asymmetry" in _kinds(vs)


def test_diffpair_sense_optional_routed_together_limit(tmp_path_factory):
    bg = _skewed_pair(tmp_path_factory, "sku")
    spec = {"p": "/ISNS_P", "n": "/ISNS_N", "gap_mm": 0.2, "kind": "sense"}
    assert "diffpair_uncoupled" not in _kinds(check_diffpair.check_pair(bg, spec)[0])
    vs, _ = check_diffpair.check_pair(bg, {**spec, "max_sense_uncoupled_mm": 3})
    assert "diffpair_uncoupled" in _kinds(vs)


def test_diffpair_sense_name_default_is_narrow():
    f = check_diffpair.is_sense_name
    assert f("/ISNS_P", "/ISNS_N") and f("/power/ISENSE_P", "/power/ISENSE_N")
    assert f("/CSA1_P", "/CSA1_N") and f("/SHUNT+", "/SHUNT-")
    for p, n in (("/USB_DP", "/USB_DM"), ("/VSNS_P", "/VSNS_N"),
                 ("/ETH_TX_P", "/ETH_TX_N"), ("/LVDS0_P", "/LVDS0_N"),
                 ("/CLK_P", "/CLK_N")):
        assert not f(p, n), p


def test_diffpair_resolve_pairs_sense_default_only_when_discovered(tmp_path_factory):
    bg = _skewed_pair(tmp_path_factory, "skr")
    specs = check_diffpair.resolve_pairs(bg, {})
    assert specs == [{"p": "/ISNS_P", "n": "/ISNS_N", "kind": "sense"}]
    # an explicit entry classifies itself: no name default applied to it
    ex = check_diffpair.resolve_pairs(bg, {"diff_pairs": [{"p": "/ISNS_P", "n": "/ISNS_N"}]})
    assert "kind" not in ex[0]


def test_diffpair_stripline_sits_on_the_core():
    """#663: an inner pair between a thin core and a thick prepreg is solved
    with the trace on the core, not on the thicker dielectric."""
    import impedance
    if not impedance.SOLVER_OK:
        pytest.skip("field solver needs numpy/scipy")
    cu = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    st = geom.Stackup(cu, 1.6, [
        {"above": "F.Cu", "below": "In1.Cu", "height": 0.11, "epsilon_r": 3.23, "kind": "prepreg"},
        {"above": "In1.Cu", "below": "In2.Cu", "height": 0.10, "epsilon_r": 3.66, "kind": "core"},
        {"above": "In2.Cu", "below": "B.Cu", "height": 1.20, "epsilon_r": 3.23, "kind": "prepreg"},
    ], {c: 0.0175 for c in cu}, assumed=False, source="test")

    class BG:
        copper_layers = cu
        stackup = st

    z, model = check_diffpair.layer_zdiff(BG, "In2.Cu", 0.127, 0.24)
    assert model == "stripline"
    on_core = impedance.field_stripline(0.127, 0.10, 1.20, 0.0175, 3.66, 3.23, 0.24)["zdiff"]
    on_thick = impedance.field_stripline(0.127, 1.20, 0.10, 0.0175, 3.23, 3.66, 0.24)["zdiff"]
    assert z == pytest.approx(on_core) and abs(z - on_thick) > 3


# ============================================ netconn: T-tap on a short trunk
# #1336: a tap end on a short trunk's body is within cap reach of BOTH trunk
# ends; rule b joined it to each, a loop beside the trunk that isn't in the
# copper, so the trunk lost its bridge and check_current judged it at the
# rail budget. The issue's numbers: 0.6 mm trunk 1.12 mm long, 0.56 mm tap
# from its centreline 0.52 mm from one end (reach 0.63 mm to both).

def _t_tap(tap_y: float) -> str:
    return (seg(39.0, 47.7, 39.0, 46.58, layer="B.Cu", w=0.6)
            + seg(39.0, tap_y, 39.7, tap_y, layer="B.Cu", w=0.56)
            + seg(39.0, 47.7, 39.0, 49.0, layer="B.Cu", w=0.6)
            + seg(39.0, 46.58, 39.0, 45.0, layer="B.Cu", w=0.6))


def test_netconn_t_tap_on_short_trunk_keeps_bridge(tmp_path_factory):
    """Planted fault: the tap reaches both trunk caps; the trunk (edge 0)
    must still be a bridge, and the tap joins the nearer end only."""
    bg = _board(tmp_path_factory, "ttap", _t_tap(47.18),
                outline=(30, 40, 50, 55))
    g = netconn.build(bg, "N")
    assert 0 in netconn.bridge_tracks(g)
    assert netconn.shortest_path_len(g, pt(39.7, 47.18), pt(39.0, 47.7)) \
        == pytest.approx(0.7 + 0.52, abs=0.01)


def test_netconn_t_tap_reaching_one_cap_unchanged(tmp_path_factory):
    """Quiet twin: a tap in reach of one trunk cap only joins it, as before,
    and the trunk is a bridge either way."""
    bg = _board(tmp_path_factory, "ttap1", _t_tap(47.4),
                outline=(30, 40, 50, 55))
    g = netconn.build(bg, "N")
    assert 0 in netconn.bridge_tracks(g)
    assert netconn.shortest_path_len(g, pt(39.7, 47.4), pt(39.0, 45.0)) \
        == pytest.approx(0.7 + 0.3 + 1.12 + 1.58, abs=0.01)


def test_netconn_end_on_a_short_tracks_end_skips_its_far_end(tmp_path_factory):
    """An end coinciding with one end of a short track (rule a) is not also
    joined to its far end through cap reach: the short track stays a bridge."""
    body = (seg(1, 5, 5, 5, w=0.6) + seg(5, 5, 5.5, 5, w=0.6)
            + seg(5.5, 5, 9, 5, w=0.6))
    bg = _board(tmp_path_factory, "shortend", body)
    g = netconn.build(bg, "N")
    assert netconn.bridge_tracks(g) == {0, 1, 2}

# ============================================================ tied contacts (#634)
# A USB-C receptacle's row B7 A6 A7 B6 at 0.5 mm: D+ is A6 and B6, D- A7 and
# B7. Each pair is tied across the row through B.Cu and two vias, D+ by 3 mm,
# D- by 4 mm. A plug meets one row: the other contact pair is a stub.

def _usbc_body(tie_n=True):
    pad = ('    (pad "{}" smd rect (at {} 0) (size 0.3 1.0) (layers "F.Cu") '
           '(net "{}"))\n')
    body = (
        '  (footprint "t:USBC" (at 100 80) (layer "F.Cu")\n'
        '    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))\n'
        + pad.format("B7", -0.75, "/USB_D-") + pad.format("A6", -0.25, "/USB_D+")
        + pad.format("A7", 0.25, "/USB_D-") + pad.format("B6", 0.75, "/USB_D+")
        + '  )\n'
        '  (footprint "t:U" (at 100 90) (layer "F.Cu")\n'
        '    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
        + pad.format("1", -0.25, "/USB_D+") + pad.format("2", 0.25, "/USB_D-")
        + '  )\n'
        + seg(99.75, 90, 99.75, 80, net="/USB_D+")
        + seg(100.25, 90, 100.25, 80, net="/USB_D-")
        # D+ tie, A6 -> B6: 1 + 1 + 1 mm
        + seg(99.75, 80, 99.75, 79, net="/USB_D+") + via(99.75, 79, net="/USB_D+")
        + seg(99.75, 79, 100.75, 79, net="/USB_D+", layer="B.Cu")
        + via(100.75, 79, net="/USB_D+") + seg(100.75, 79, 100.75, 80, net="/USB_D+"))
    if tie_n:   # D- tie, A7 -> B7: 1.5 + 1 + 1.5 mm
        body += (seg(100.25, 80, 100.25, 78.5, net="/USB_D-")
                 + via(100.25, 78.5, net="/USB_D-")
                 + seg(100.25, 78.5, 99.25, 78.5, net="/USB_D-", layer="B.Cu")
                 + via(99.25, 78.5, net="/USB_D-")
                 + seg(99.25, 78.5, 99.25, 80, net="/USB_D-"))
    return body


def test_diffpair_usbc_tie_rows(tmp_path_factory):
    bg = _board(tmp_path_factory, "usbc", _usbc_body(), outline=(95, 75, 105, 95))
    term = check_diffpair.matched_terminals(bg, "/USB_D+", "/USB_D-")
    ties = check_diffpair.contact_ties(bg, "/USB_D+", "/USB_D-", term)
    # rows by pad name, not nearest pad: A6's nearest D- pads are A7 and B7
    assert {r: [(a.number, b.number) for a, b in w] for r, w in ties.items()} \
        == {"J1": [("A6", "A7"), ("B6", "B7")]}


def test_diffpair_usbc_tie_per_orientation(tmp_path_factory):
    bg = _board(tmp_path_factory, "usbc2", _usbc_body(), outline=(95, 75, 105, 95))
    vs, facts = check_diffpair.check_pair(
        bg, {"p": "/USB_D+", "n": "/USB_D-", "max_uncoupled_mm": 20})
    assert facts["branch_free"] is False
    a, b = facts["tied_contacts"]
    assert a["contacts"] == ["J1.A6/A7"] and b["contacts"] == ["J1.B6/B7"]
    # plug meets row A: both trunks 10 mm, the B row hangs off as stubs
    assert a["length_p_mm"] == pytest.approx(10.0, abs=0.01)
    assert a["skew_mm"] == pytest.approx(0.0, abs=0.01)
    assert a["stub_p_mm"] == pytest.approx(3.0, abs=0.05)
    assert a["stub_n_mm"] == pytest.approx(4.0, abs=0.05)
    # row B: the ties are in the trunk, and their 1 mm difference is skew
    assert b["length_p_mm"] == pytest.approx(13.0, abs=0.05)
    assert b["length_n_mm"] == pytest.approx(14.0, abs=0.05)
    assert b["skew_mm"] == pytest.approx(1.0, abs=0.05)
    assert b["stub_p_mm"] == pytest.approx(0.0, abs=0.05)
    # the pair is judged at its worst way
    assert facts["skew_mm"] == pytest.approx(1.0, abs=0.05)
    assert facts["length_n_mm"] == pytest.approx(14.0, abs=0.05)
    assert all(v["branch_free"] is False for v in vs if "branch_free" in v)


def test_diffpair_usbc_tie_skew_judged_at_worst_way(tmp_path_factory):
    bg = _board(tmp_path_factory, "usbc3", _usbc_body(), outline=(95, 75, 105, 95))
    vs, facts = check_diffpair.check_pair(
        bg, {"p": "/USB_D+", "n": "/USB_D-", "max_uncoupled_mm": 20,
             "max_skew_mm": 0.5})
    assert "diffpair_skew" in _kinds(vs)        # row B: 1 mm > 0.5 mm


def test_diffpair_usbc_untied_row_is_not_a_way(tmp_path_factory):
    # D- tied on one pad only (B7 left unrouted): a way through B7 leaves the
    # D- trunk open, so skew is not judged rather than read off one row
    bg = _board(tmp_path_factory, "usbc4", _usbc_body(tie_n=False),
                outline=(95, 75, 105, 95))
    vs, facts = check_diffpair.check_pair(
        bg, {"p": "/USB_D+", "n": "/USB_D-", "max_uncoupled_mm": 20})
    assert facts["branch_free"] is False
    assert "skew_mm" not in facts and "tied_contacts" not in facts
    assert any("tied on J1" in s for s in facts["not_judged"])
    assert "diffpair_skew" not in _kinds(vs)


def test_diffpair_unread_ref_is_no_tie(tmp_path_factory):
    # a KiCad 6 board's refs read as "?" (#1429): every such part shares that
    # ref, so two unrelated parts must not read as one part's tied contacts
    body = _usbc_body().replace('"Reference" "J1"', '"Reference" "?"') \
        .replace('"Reference" "U1"', '"Reference" "?"')
    bg = _board(tmp_path_factory, "usbc5", body, outline=(95, 75, 105, 95))
    term = check_diffpair.matched_terminals(bg, "/USB_D+", "/USB_D-")
    assert check_diffpair.contact_ties(bg, "/USB_D+", "/USB_D-", term) == {}
