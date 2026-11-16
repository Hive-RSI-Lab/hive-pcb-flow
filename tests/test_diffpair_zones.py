"""A diff pair drawn as two zones is judged, not skipped (#890).

hw-lab zcoupon draws its controlled-impedance pairs as zone strips with no
tracks; check_diffpair reported "pair not fully routed; coupling not
evaluated". Now each layer both nets fill is sampled along the coupled run:
width per side, edge gap, Zdiff as drawn, and a side narrowed against its
partner. Planted faults: the same pair drawn as tracks gives the same Zdiff;
one side narrowed over a stretch is found.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import check_diffpair  # noqa: E402
from test_t2_diffpair import _board, _straight_pair  # noqa: E402

SPEC = {"p": "/D_P", "n": "/D_N", "gap_mm": 0.3}


def _zone(net, pts, layer="F.Cu"):
    xy = " ".join(f"(xy {x} {y})" for x, y in pts)
    return (f'  (zone (net "{net}") (layer "{layer}")\n'
            f'    (polygon (pts {xy}))\n'
            f'    (filled_polygon (layer "{layer}") (pts {xy})))\n')


def _strip(net, y0, w, notch=None):
    """A 2-18 mm strip of width w from y0; notch=(x0, x1, w2) narrows it
    from its far side to w2 there."""
    if notch is None:
        return _zone(net, [(2, y0), (18, y0), (18, y0 + w), (2, y0 + w)])
    x0, x1, w2 = notch
    sgn = 1 if net.endswith("P") else -1     # P sits below N
    far = y0 if sgn > 0 else y0 + w
    cut = far + sgn * (w - w2)
    near = y0 + w if sgn > 0 else y0
    return _zone(net, [(2, near), (18, near), (18, far), (x1, far), (x1, cut),
                       (x0, cut), (x0, far), (2, far)])


def _zone_pair(tmp_path_factory, name, notch=None):
    # P 4.85-5.15, N 5.45-5.75: _straight_pair's 0.3 mm tracks on 5.0 and
    # 5.6 centres (edge gap 0.3) drawn as zones instead
    body = (_strip("/D_P", 4.85, 0.3)
            + _strip("/D_N", 5.45, 0.3, notch))
    return _board(tmp_path_factory, name, body)


def test_a_zone_pair_is_judged_like_its_tracks(tmp_path_factory):
    bg = _zone_pair(tmp_path_factory, "zp")
    vs, facts = check_diffpair.check_pair(bg, SPEC)
    assert facts.get("routed") is not False and facts["drawn_as"] == "zones"
    row = facts["layers"]["F.Cu"]
    assert row["width_p_mm"] == pytest.approx(0.3, abs=1e-3)
    assert row["width_n_mm"] == pytest.approx(0.3, abs=1e-3)
    assert row["gap_mm"] == pytest.approx(0.3, abs=1e-3)
    assert row["coupled_mm"] > 14
    # the same pair drawn as tracks solves to the same Zdiff
    _, tf = check_diffpair.check_pair(
        _straight_pair(tmp_path_factory, "zt"), SPEC)
    assert row["zdiff_ohm"] == pytest.approx(tf["impedance"]["zdiff_ohm"],
                                             rel=0.01)
    assert vs == []
    # a target it misses is flagged, as for tracks
    vs, _ = check_diffpair.check_pair(bg, {**SPEC, "impedance_ohm": 90})
    assert [v["kind"] for v in vs] == ["diffpair_impedance"]
    assert vs[0]["drawn_as"] == "zones" and vs[0]["layer"] == "F.Cu"


def test_one_side_narrowed_is_found(tmp_path_factory):
    bg = _zone_pair(tmp_path_factory, "zn", notch=(6, 12, 0.2))
    vs, facts = check_diffpair.check_pair(bg, SPEC)
    (v,) = [v for v in vs if v["kind"] == "diffpair_width_imbalance"]
    assert v["net"] == "/D_N" and v["width_n_mm"] == pytest.approx(0.2, abs=1e-3)
    assert v["stretch_mm"] == pytest.approx(6, abs=0.3)
    assert facts["layers"]["F.Cu"]["min_width_n_mm"] == pytest.approx(0.2,
                                                                       abs=1e-3)
    # a notch shorter than the stretch limit is the fab floor's, not this
    vs, _ = check_diffpair.check_pair(
        _zone_pair(tmp_path_factory, "zs", notch=(6, 6.5, 0.2)), SPEC)
    assert "diffpair_width_imbalance" not in {v["kind"] for v in vs}
    # inside the limit is balanced
    vs, _ = check_diffpair.check_pair(
        _zone_pair(tmp_path_factory, "zk", notch=(6, 12, 0.28)), SPEC)
    assert vs == []


def test_a_sense_zone_pair_is_held_to_no_impedance(tmp_path_factory):
    bg = _zone_pair(tmp_path_factory, "zsn")
    vs, facts = check_diffpair.check_pair(
        bg, {**SPEC, "impedance_ohm": 50, "kind": "sense"})
    assert vs == [] and facts["kind"] == "sense"


def test_run_counts_a_zone_pair_as_judged(tmp_path_factory):
    bg = _zone_pair(tmp_path_factory, "zr")
    payload, _ = check_diffpair.run(["--pcb", str(bg.path)])
    assert not any("not fully routed" in s for s in payload["not_judged"])
    assert payload["checked"][0]["drawn_as"] == "zones"


# ---------------------------------------------- length, uncoupled, vias (#1000)
# A zone strip's length is area / width where its outline agrees, so a pair
# drawn as zones is held to the same skew, uncoupled and via limits as one
# drawn as tracks. Planted faults: a longer P strip, a P detour away from
# N, a via on P only. Quiet twin: the matched straight pair, with only the
# phase left not judged.

def _strip_from(net, line, w=0.3, layer="F.Cu"):
    """A zone strip of width w along the centreline `line` (flat ends,
    mitred bends)."""
    from shapely.geometry import LineString
    poly = LineString(line).buffer(w / 2, cap_style=2, join_style=2)
    return _zone(net, [(round(x, 4), round(y, 4))
                       for x, y in list(poly.exterior.coords)[:-1]], layer)


def _kinds(vs):
    return [v["kind"] for v in vs]


def test_matched_zone_pair_is_quiet_with_only_phase_not_judged(
        tmp_path_factory):
    vs, facts = check_diffpair.check_pair(_zone_pair(tmp_path_factory, "zq"),
                                          SPEC)
    assert vs == []
    assert facts["length_p_mm"] == pytest.approx(16, abs=0.05)
    assert facts["length_n_mm"] == pytest.approx(16, abs=0.05)
    assert facts["skew_mm"] == pytest.approx(0, abs=0.05)
    assert facts["uncoupled_p_mm"] == pytest.approx(0, abs=0.05)
    assert facts["vias_p"] == facts["vias_n"] == 0
    assert facts["not_judged"] == [check_diffpair.PHASE_ZONES]


def test_longer_p_strip_is_skew(tmp_path_factory):
    body = _strip("/D_P", 4.85, 0.3) + _zone(
        "/D_N", [(2, 5.45), (16.5, 5.45), (16.5, 5.75), (2, 5.75)])
    bg = _board(tmp_path_factory, "zsk", body)
    vs, facts = check_diffpair.check_pair(bg, {**SPEC, "max_skew_mm": 1.0})
    assert _kinds(vs) == ["diffpair_skew"]
    v = vs[0]
    assert v["drawn_as"] == "zones" and v["severity"] == "warning"
    assert v["skew_mm"] == pytest.approx(1.5, abs=0.05)
    assert facts["skew_mm"] == pytest.approx(1.5, abs=0.05)


def test_p_detour_strip_is_uncoupled(tmp_path_factory):
    body = (_strip_from("/D_P", [(2, 5), (8, 5), (8, 2.5), (12, 2.5),
                                 (12, 5), (18, 5)])
            + _strip("/D_N", 5.45, 0.3))
    bg = _board(tmp_path_factory, "zud", body)
    vs, facts = check_diffpair.check_pair(bg, {**SPEC, "max_skew_mm": 10})
    assert _kinds(vs) == ["diffpair_uncoupled"]
    v = vs[0]
    assert v["net"] == "/D_P" and v["drawn_as"] == "zones"
    # 4 mm across the bottom and two 2.5 mm legs, less the stretch of each
    # leg still inside coupling_max of N
    assert 7 < v["uncoupled_mm"] < 9
    assert facts["length_p_mm"] == pytest.approx(21, abs=0.1)
    assert facts["uncoupled_n_mm"] < 4.5    # N's bare stretch over the gap


def test_via_on_one_side_only_is_asymmetric(tmp_path_factory):
    from test_t2_diffpair import via
    body = (_strip("/D_P", 4.85, 0.3) + _strip("/D_N", 5.45, 0.3)
            + via(10, 5.0, net="/D_P", size=0.3, drill=0.15))
    bg = _board(tmp_path_factory, "zva", body)
    vs, facts = check_diffpair.check_pair(bg, SPEC)
    assert _kinds(vs) == ["diffpair_via_asymmetry"]
    assert (facts["vias_p"], facts["vias_n"]) == (1, 0)


def test_zone_not_one_strip_leaves_skew_not_judged(tmp_path_factory):
    # N is a strip with a 1 mm-square tab: area/width and outline disagree
    body = _strip("/D_P", 4.85, 0.3) + _zone(
        "/D_N", [(2, 5.45), (18, 5.45), (18, 5.75), (10, 5.75), (10, 6.75),
                 (9, 6.75), (9, 5.75), (2, 5.75)])
    bg = _board(tmp_path_factory, "znj", body)
    vs, facts = check_diffpair.check_pair(bg, {**SPEC, "max_skew_mm": 0.1})
    assert "diffpair_skew" not in _kinds(vs)
    assert "skew_mm" not in facts and facts["length_n_mm"] is None
    assert facts["not_judged"][0].startswith("skew: /D_N on F.Cu is not one "
                                             "width")
    payload, _ = check_diffpair.run(["--pcb", str(bg.path)])
    assert any(s.startswith("/D_P//D_N: skew:") for s in payload["not_judged"])


# ---------------------------------------------- a mixed pair (#1000)
# Tracks from U1 into a pair of zone strips and out to J1: the trunk closes
# through the strips at their length, no open-trunk warning, skew judged.

def _mixed(tmp_path_factory, name, n_exit=()):
    from test_t2_diffpair import seg
    fps = (
        '  (footprint "t:U" (at 1 5) (layer "F.Cu")\n'
        '    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net "/D_P"))\n'
        '    (pad "2" smd rect (at 0 0.6) (size 0.3 0.3) (layers "F.Cu") (net "/D_N")))\n'
        '  (footprint "t:J" (at 19 5) (layer "F.Cu")\n'
        '    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net "/D_P"))\n'
        '    (pad "2" smd rect (at 0 0.6) (size 0.3 0.3) (layers "F.Cu") (net "/D_N")))\n')
    body = (fps
            + seg(1, 5, 6.5, 5, net="/D_P", w=0.3)
            + seg(1, 5.6, 6.5, 5.6, net="/D_N", w=0.3)
            + _zone("/D_P", [(6, 4.85), (14, 4.85), (14, 5.15), (6, 5.15)])
            + _zone("/D_N", [(6, 5.45), (14, 5.45), (14, 5.75), (6, 5.75)])
            + seg(13.5, 5, 19, 5, net="/D_P", w=0.3))
    pts = [(13.5, 5.6), *n_exit, (19, 5.6)]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        body += seg(x0, y0, x1, y1, net="/D_N", w=0.3)
    return _board(tmp_path_factory, name, body)


def test_mixed_pair_closes_its_trunk_through_the_strips(tmp_path_factory):
    vs, facts = check_diffpair.check_pair(_mixed(tmp_path_factory, "zm"),
                                          SPEC)
    assert vs == []
    assert facts["branch_free"] is True
    assert facts["trunk_through_zone"] == ["/D_P", "/D_N"]
    # 5.5 mm of track each end into an 8 mm strip
    assert facts["length_p_mm"] == pytest.approx(19, abs=0.1)
    assert facts["skew_mm"] == pytest.approx(0, abs=0.05)
    assert "F.Cu" in facts["zones"]
    assert facts["not_judged"] == [check_diffpair.PHASE_ZONES]
    assert "phase" not in facts


def test_mixed_pair_with_a_long_n_exit_is_skew(tmp_path_factory):
    # N leaves its strip on a jog to J1 1.8 mm longer than P's straight
    # exit: the trunk through the strips sees it, as it would on tracks
    vs, facts = check_diffpair.check_pair(
        _mixed(tmp_path_factory, "zms", n_exit=[(16, 8)]),
        {**SPEC, "max_skew_mm": 1, "max_uncoupled_mm": 20})
    assert "diffpair_open_trunk" not in _kinds(vs)
    (v,) = [v for v in vs if v["kind"] == "diffpair_skew"]
    assert v["skew_mm"] == pytest.approx(1.81, abs=0.1)


def test_pours_named_like_a_pair_leave_uncoupled_not_judged(
        tmp_path_factory):
    # two supply pours that happen to be named _P/_N (a converter's LED
    # outputs, as on a training board): area over width is no length, so
    # neither skew nor uncoupled length is read from them
    body = (_zone("/D_P", [(2, 1), (18, 1), (18, 5.15), (9, 5.15), (9, 3),
                           (2, 3)])
            + _zone("/D_N", [(2, 5.45), (18, 5.45), (18, 9), (12, 9),
                             (12, 6.5), (2, 6.5)]))
    bg = _board(tmp_path_factory, "zpour", body)
    vs, facts = check_diffpair.check_pair(bg, SPEC)
    assert "diffpair_uncoupled" not in _kinds(vs)
    assert "diffpair_skew" not in _kinds(vs)
    assert "uncoupled_p_mm" not in facts and "skew_mm" not in facts
    nj = facts["not_judged"]
    assert nj[0].startswith("skew: /D_P on F.Cu is not one width")
    assert nj[1].startswith("uncoupled length: /D_P on F.Cu is not one width")
