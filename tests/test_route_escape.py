"""#325: BGA escape (route_escape + lib/bgalib).

Pure tests (grid detection, the escape budget, the DRU rule and its
placement, floors, plane layers, via-in-pad detection, KRT arguments, the
--plan CLI, rules_gen's copy of the rule) need no toolchain. The slow test
fans a synthetic 6 x 6 0.8 mm BGA out live with KRT and grades it with
kicad-cli: every ball escaped, no new DRC error.
"""
from __future__ import annotations

import json
import math
import sys
import uuid
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import bgalib  # noqa: E402
import cluster_violations  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import route_escape as re_  # noqa: E402
import rules_gen  # noqa: E402
import teardroplib as tdl  # noqa: E402

# JLC 6-layer 1 oz row (reference/jlc_capabilities.yaml)
JLC6 = {"track_width": 0.0889, "clearance": 0.0889, "via_diameter": 0.45,
        "via_drill": 0.2, "annular": 0.15, "hole_clearance": 0.25}
PRO6 = {"board": {"design_settings": {"rules": {
    "min_clearance": 0.0889, "min_track_width": 0.0889,
    "min_via_diameter": 0.45, "min_through_hole_diameter": 0.2,
    "min_via_annular_width": 0.15, "min_hole_to_hole": 0.5,
    "min_copper_edge_clearance": 0.3}}},
    "net_settings": {"classes": [{"name": "Default", "clearance": 0.125,
                                  "track_width": 0.2, "via_diameter": 0.5,
                                  "via_drill": 0.2}]},
    "meta": {"filename": "b.kicad_pro", "version": 3}}


# ---- synthetic boards ----------------------------------------------------

def _pad(num, x, y, net, size, shape="circle", w=None):
    w = w or size
    return (f'    (pad "{num}" smd {shape}\n      (at {x:.4f} {y:.4f})\n'
            f'      (size {w} {size})\n'
            f'      (layers "F.Cu" "F.Mask" "F.Paste")\n'
            f'      (net "{net}")\n      (uuid "u-{num}-{net}")\n    )\n')


def _fp(ref, x, y, pads, half, rot=0):
    # A fixed UUID per ref: without one KiCad draws a random UUID per load
    # and saves footprints sorted by it, so every route_escape/route_auto
    # save would shuffle the footprint order a bench routes in (#1412).
    hx, hy = half
    fid = uuid.uuid5(uuid.NAMESPACE_URL, f"pcb-flow-test/{ref}")
    return (f'  (footprint "t:{ref}" (layer "F.Cu") (at {x} {y} {rot})\n'
            f'    (uuid "{fid}")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'    (fp_rect (start {-hx} {-hy}) (end {hx} {hy}) '
            f'(stroke (width 0.05)) (fill no) (layer "F.CrtYd"))\n'
            f'{pads}  )\n')


def _grid(n, pitch, pad, prefix="S", skip=()):
    out = []
    for r in range(n):
        for c in range(n):
            if (r, c) in skip:
                continue
            x, y = (c - (n - 1) / 2) * pitch, (r - (n - 1) / 2) * pitch
            out.append((f"{chr(65 + r)}{c + 1}", x, y, f"/{prefix}{r}_{c}"))
    return out


def board(tmp_path, *, n=6, pitch=0.8, pad=0.35, rot=0, extra="",
          name="b", zone_layer=None, pro=True, connector=True,
          layers=4, skip=()) -> Path:
    balls = _grid(n, pitch, pad, skip=skip)
    u1 = "".join(_pad(num, x, y, net, pad) for num, x, y, net in balls)
    half = (n - 1) / 2 * pitch + 0.8
    body = _fp("U1", 20.0, 15.0, u1, (half, half), rot)
    if connector:
        jp = "".join(_pad(str(k + 1), (k % 18) * 1.0, (k // 18) * 2.0, net,
                          1.2, "rect", 0.6)
                     for k, (_, _, _, net) in enumerate(balls))
        body += _fp("J1", 11.5, 24.0, jp, (18, 2.5))
    if zone_layer:
        body += (f'  (zone (net "GND") (layer "{zone_layer}") (uuid "z1")\n'
                 '    (polygon (pts (xy 1 1) (xy 39 1) (xy 39 39) (xy 1 39)))\n'
                 '  )\n')
    body += extra
    inner = " ".join(f'({2 * i + 2} "In{i}.Cu" signal)'
                     for i in range(1, layers - 1))
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) {inner}
    (2 "B.Cu" signal) (13 "F.Paste" user) (1 "F.Mask" user)
    (5 "F.SilkS" user "F.Silkscreen") (31 "F.CrtYd" user "F.Courtyard")
    (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 40 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    if pro:
        p.with_suffix(".kicad_pro").write_text(json.dumps(PRO6),
                                                encoding="utf-8")
    return p


def _qfn(n_side=8, pitch=0.5) -> str:
    """A perimeter-only pad ring (QFN-like): not a ball grid."""
    pads = []
    k = 0
    span = (n_side - 1) / 2 * pitch
    for i in range(n_side):
        o = (i - (n_side - 1) / 2) * pitch
        for x, y in ((o, -span - 0.5), (o, span + 0.5),
                     (-span - 0.5, o), (span + 0.5, o)):
            k += 1
            pads.append(_pad(str(k), x, y, f"/Q{k}", 0.25))
    return _fp("U9", 32.0, 32.0, "".join(pads), (3, 3))


def test_synthetic_footprints_carry_a_fixed_uuid_per_ref():
    # #1412: a fixed UUID keeps the footprint order of a saved bench board
    ids = {r: _fp(r, 0, 0, "", (1, 1)).split('(uuid "')[1].split('"')[0]
           for r in ("U1", "U2", "J1", "J2", "C1")}
    assert ids["U1"] == _fp("U1", 5, 5, "", (2, 2)).split('(uuid "')[1][:36]
    assert len(set(ids.values())) == len(ids)
    for v in ids.values():
        assert str(uuid.UUID(v)) == v


# ============================================================ detection

def test_finds_ball_grid_not_qfn(tmp_path):
    bg = geom.BoardGeom.from_file(board(tmp_path, extra=_qfn()))
    found = bgalib.find_bgas(bg)
    assert [b.ref for b in found] == ["U1"]
    b = found[0]
    assert (b.rows, b.cols, b.balls) == (6, 6, 36)
    assert b.pitch == pytest.approx(0.8)
    assert b.pad_d == pytest.approx(0.35)
    assert b.layer == "F.Cu"


def test_finds_rotated_grid(tmp_path):
    bg = geom.BoardGeom.from_file(board(tmp_path, rot=30))
    (b,) = bgalib.find_bgas(bg)
    assert b.pitch == pytest.approx(0.8, abs=1e-3)
    assert (b.rows, b.cols) == (6, 6)


def test_coarse_grid_is_not_a_bga(tmp_path):
    bg = geom.BoardGeom.from_file(board(tmp_path, pitch=2.54, pad=1.0))
    assert bgalib.find_bgas(bg) == []


def test_refs_filter(tmp_path):
    bg = geom.BoardGeom.from_file(board(tmp_path))
    assert bgalib.find_bgas(bg, ["U7"]) == []
    assert [b.ref for b in bgalib.find_bgas(bg, ["U1"])] == ["U1"]


def _qfn_ep(n_side=12, pitch=0.5, ep=5.0) -> str:
    """A real QFN: oblong leads on all four sides (the lattice comes out at
    0.25 mm) and a big exposed pad in the middle, which used to pass as an
    interior ball (hive-hwde issue: route_escape planned escapes for QFNs)."""
    pads = []
    k = 0
    span = 3.0
    for i in range(n_side):
        o = (i - (n_side - 1) / 2) * pitch
        for x, y, w, h in ((o, -span, 0.25, 0.8), (o, span, 0.25, 0.8),
                           (-span, o, 0.8, 0.25), (span, o, 0.8, 0.25)):
            k += 1
            pads.append(_pad(str(k), x, y, f"/Q{k}", h, shape="roundrect",
                             w=w))
    pads.append(_pad(str(k + 1), 0.0, 0.0, "/GND", ep, shape="rect"))
    return _fp("U8", 32.0, 8.0, "".join(pads), (3.6, 3.6))


def test_qfn_with_exposed_pad_is_not_a_bga(tmp_path):
    bg = geom.BoardGeom.from_file(board(tmp_path, extra=_qfn_ep()))
    assert [b.ref for b in bgalib.find_bgas(bg)] == ["U1"]


def test_depopulated_bga_is_still_found(tmp_path):
    """A ball field with its centre depopulated (a ring two balls deep plus
    a centre island) is still a BGA."""
    skip = ({(r, c) for r in range(2, 6) for c in range(2, 6)}
            - {(3, 3), (3, 4), (4, 3), (4, 4)})
    pads = "".join(_pad(num, x, y, net, 0.35) for num, x, y, net
                   in _grid(8, 0.8, 0.35, prefix="D", skip=skip))
    bg = geom.BoardGeom.from_file(board(tmp_path, extra=_fp(
        "U5", 30.0, 30.0, pads, (3.5, 3.5))))
    assert "U5" in [b.ref for b in bgalib.find_bgas(bg)]


# ============================================================ budget

def _bga(pitch, pad):
    return bgalib.Bga("U1", "F.Cu", pitch, 10, 10, pad, (0, 0, 1, 1), 100)


def test_budget_08mm_on_jlc_6layer_is_dogbone():
    """i.MX6ULL MAPBGA-289: 0.8 mm, 0.32 mm lands. The 6-layer floor via
    (0.45/0.2) breaks the row's own 0.15 annular ring, so 0.5; clearance =
    hole clearance 0.25 less the 0.15 ring = 0.1."""
    ep = bgalib.escape_params(_bga(0.8, 0.32), JLC6)
    assert ep["via_size"] == pytest.approx(0.5)
    assert ep["via_drill"] == pytest.approx(0.2)
    assert ep["clearance"] == pytest.approx(0.1)
    assert ep["track_width"] == pytest.approx(0.0889)
    assert ep["method"] == "dogbone" and ep["dogbone_ok"]
    b = ep["budget"]
    assert b["street"]["have"] == pytest.approx(0.3)
    assert b["street"]["need"] == pytest.approx(0.2889)
    assert b["gap"]["have"] == pytest.approx(0.8 / math.sqrt(2), abs=1e-4)
    assert b["gap"]["need"] == pytest.approx(0.16 + 0.25 + 0.1)


def test_budget_05mm_emmc_cannot_dogbone():
    ep = bgalib.escape_params(_bga(0.5, 0.25), JLC6)
    assert not ep["dogbone_ok"]
    assert ep["method"] == "channel"
    assert not ep["budget"]["gap"]["ok"]
    assert "via-in-pad" in ep["why"]


def test_budget_wide_hole_clearance_raises_clearance():
    ep = bgalib.escape_params(_bga(1.0, 0.45), {**JLC6, "hole_clearance": 0.3})
    assert ep["clearance"] == pytest.approx(0.15)


# ============================================================ DRU rule

def test_dru_rule_matches_rules_gen_render():
    r = rules_gen.Rule(bgalib.rule_name("U100"), "clearance", 0.1,
                       condition=bgalib.rule_condition("U100"))
    assert bgalib.dru_rule("U100", 0.1) == r.render() + "\n"


def test_ensure_rules_idempotent_replace_and_header():
    t, ch = bgalib.ensure_rules("", [("U1", 0.1)])
    assert t.startswith("(version 1)\n") and ch == ["aiee_bga_escape_U1"]
    t2, ch2 = bgalib.ensure_rules(t, [("U1", 0.1)])
    assert t2 == t and ch2 == []
    t3, ch3 = bgalib.ensure_rules(t, [("U1", 0.12)])
    assert ch3 == ["aiee_bga_escape_U1"]
    assert t3.count('(rule "aiee_bga_escape_U1"') == 1
    assert "0.1200mm" in t3 and "0.1000mm" not in t3


def test_ensure_rules_stays_before_hv_rules():
    hv = ('(version 1)\n(rule "aiee_clearance_floor"\n\t(constraint '
          'clearance (min 0.0889mm))\n)\n(rule "aiee_hv_48v_VIN"\n\t'
          '(constraint clearance (min 0.6000mm))\n\t(condition '
          '"A.NetName == \'VIN\'")\n)\n')
    t, _ = bgalib.ensure_rules(hv, [("U1", 0.1)])
    assert t.index("aiee_bga_escape_U1") < t.index("aiee_hv_48v_VIN")
    pkg = hv.replace("aiee_hv_48v_VIN", "aiee_pkg_rating_U3")
    t, _ = bgalib.ensure_rules(pkg, [("U1", 0.1)])
    assert t.index("aiee_bga_escape_U1") < t.index("aiee_pkg_rating_U3")
    assert t.index("aiee_clearance_floor") < t.index("aiee_bga_escape_U1")


def test_rules_gen_writes_the_escape_rule(tmp_path):
    bg = geom.BoardGeom.from_file(board(tmp_path))
    cap = {"min_trace_width_mm": 0.0889, "min_clearance_mm": 0.0889,
           "min_via_diameter_mm": 0.45, "min_via_drill_mm": 0.2,
           "min_annular_ring_mm": 0.15}
    rules, facts = rules_gen.bga_rules(bg, cap)
    assert [r.name for r in rules] == ["aiee_bga_escape_U1"]
    assert facts[0]["clearance_mm"] == pytest.approx(0.1)
    assert facts[0]["method"] == "dogbone"
    # route_escape's writer sees rules_gen's rule as its own
    text = "(version 1)\n" + "\n".join(r.render() for r in rules) + "\n"
    assert bgalib.ensure_rules(text, [("U1", 0.1)])[1] == []


# ============================================================ helpers

def test_escape_floors_from_pro(tmp_path):
    p = board(tmp_path).with_suffix(".kicad_pro")
    fl = re_.escape_floors(p)
    assert fl["clearance"] == pytest.approx(0.0889)   # fab floor, not 0.125
    assert fl["annular"] == pytest.approx(0.15)
    assert fl["hole_to_hole"] == pytest.approx(0.5)
    assert fl["hole_clearance"] == pytest.approx(0.25)  # KiCad default
    assert re_.escape_floors(None)["clearance"] == pytest.approx(0.2)


def test_escape_layers_skip_inner_plane(tmp_path):
    bg = geom.BoardGeom.from_file(board(tmp_path, zone_layer="In1.Cu"))
    (b,) = bgalib.find_bgas(bg)
    layers, planes = re_.escape_layers(bg, b)
    assert planes == ["In1.Cu"]
    assert layers == ["F.Cu", "In2.Cu", "B.Cu"]
    # an outer pour is a fill, not a plane
    bg2 = geom.BoardGeom.from_file(board(tmp_path, zone_layer="B.Cu",
                                         name="c"))
    assert re_.escape_layers(bg2, bgalib.find_bgas(bg2)[0])[1] == []
    assert re_.escape_layers(bg, b, ["F.Cu", "X.Cu"])[0] == ["F.Cu"]


def test_via_in_pad_nets_counts_only_new_vias_in_own_pads(tmp_path):
    vias = ('  (via (at 18 13) (size 0.5) (drill 0.2) (layers "F.Cu" "B.Cu")'
            ' (net "/S0_0") (uuid "v-new"))\n'
            '  (via (at 18.8 13) (size 0.5) (drill 0.2) (layers "F.Cu" "B.Cu")'
            ' (net "/S0_1") (uuid "v-old"))\n'
            '  (via (at 18.4 13.4) (size 0.5) (drill 0.2) (layers "F.Cu" '
            '"B.Cu") (net "/S0_0") (uuid "v-gap"))\n')
    bg = geom.BoardGeom.from_file(board(tmp_path, extra=vias))
    (b,) = bgalib.find_bgas(bg)
    assert re_.via_in_pad_nets(bg, b, {"v-new", "v-gap"}) == {"/S0_0": 1}


def test_fanout_args_pin_floor_and_never_touch_drc_settings(tmp_path):
    b = _bga(0.8, 0.32)
    ep = bgalib.escape_params(b, JLC6)
    a = re_.fanout_args(b, ep, ["F.Cu", "In2.Cu"], tmp_path / "f.txt", 0.05)
    assert "--no-fix-drc-settings" in a and "--fab-overrides" in a
    assert a[a.index("--escape-method") + 1] == "dogbone"
    assert a[a.index("--via-size") + 1] == "0.5"
    i = a.index("--layers")
    assert a[i + 1:i + 3] == ["F.Cu", "In2.Cu"]
    fab = re_.fab_overrides(tmp_path, ep, {"hole_to_hole": 0.5,
                                           "edge_clearance": 0.3})
    txt = fab.read_text()
    assert "via_diameter = 0.5" in txt and "hole_to_hole = 0.5" in txt


def test_plan_cli_reports_without_krt(tmp_path, capsys):
    pcb = board(tmp_path)
    before = pcb.read_bytes()
    rc = re_.main(["--pcb", str(pcb), "--plan"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["status"] == "pass"
    (b,) = out["facts"]["bgas"]
    assert b["ref"] == "U1" and b["method"] == "dogbone"
    assert b["via_size"] == pytest.approx(0.5)
    assert pcb.read_bytes() == before
    assert not (tmp_path / "route_escape").exists()


def test_no_bga_is_a_pass(tmp_path, capsys):
    pcb = board(tmp_path, pitch=2.54, pad=1.0)
    assert re_.main(["--pcb", str(pcb)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["facts"]["note"] == "no ball grid on this board"


def test_unknown_ref_is_an_error(tmp_path, capsys):
    assert re_.main(["--pcb", str(board(tmp_path)), "--refs", "U7"]) == 2


def _power_board(tmp_path):
    """The 6 x 6 field with ball A1 on +3V3, an In2 +3V3 plane, and U5 (a
    regulator stand-in) with a +3V3 pin; a switch node SW on U5 too."""
    pts = "(pts (xy 1 1) (xy 39 1) (xy 39 39) (xy 1 39))"
    extra = (_fp("U5", 32.0, 8.0, _pad("1", 0, 0, "+3V3", 0.6, "rect")
                 + _pad("2", 0, 2, "SW", 0.6, "rect"), (1, 2))
             + f'  (zone (net "+3V3") (layer "In2.Cu") (uuid "z3")\n'
               f'    (polygon {pts})\n'
               f'    (filled_polygon (layer "In2.Cu") {pts}))\n'
             + f'  (zone (net "SW") (layer "In1.Cu") (uuid "z4")\n'
               f'    (polygon {pts})\n'
               f'    (filled_polygon (layer "In1.Cu") {pts}))\n')
    pcb = board(tmp_path, extra=extra, connector=False)
    pcb.write_text(pcb.read_text().replace('(net "/S0_0")', '(net "+3V3")'))
    cons = tmp_path / "constraints.json"
    cons.write_text(json.dumps({"power": [
        {"net": "+3V3", "current_a": 1.0},
        {"net": "SW", "current_a": 1.0, "switch_node": True}]}))
    return pcb, cons


def test_power_first_vias_the_power_pins_not_the_field(tmp_path):
    """#923 point 1: the power pin off the field gets its plane via before
    the escape; the ball grid and the switch node are left alone."""
    pcb, cons = _power_board(tmp_path)
    bg = geom.BoardGeom.from_file(pcb)
    refs = [b.ref for b in bgalib.find_bgas(bg)]
    assert refs == ["U1"]
    assert "+3V3" in {p.net for p in bg.pads_of(ref="U1")}
    before = pcb.read_bytes()
    ops, fact = re_.power_first_ops(pcb, cons, refs)
    assert pcb.read_bytes() == before                 # dry: board untouched
    vias = [o for o in ops if o["op"] == "add_via"]
    assert fact["nets"] == ["+3V3"] and fact["placed"] == len(vias) == 1
    (v,) = vias
    assert v["net"] == "+3V3"
    assert math.hypot(v["at"][0] - 32.0, v["at"][1] - 8.0) < 1.5   # at U5.1
    # planted fault: without the skip the field's +3V3 ball is stitched too
    ops2, _ = re_.power_first_ops(pcb, cons, [])
    assert len([o for o in ops2 if o["op"] == "add_via"]) == 2
    assert re_.power_first_ops(pcb, None, refs) == (
        [], {"placed": 0, "note": "no constraints.json"})


def _back_cap(ref, x, y, n1, n2):
    """An 0402 on B.Cu at (x, y), pads 0.8 mm apart along x."""
    pads = "".join(
        f'    (pad "{k}" smd rect (at {dx} 0) (size 0.5 0.5)\n'
        f'      (layers "B.Cu" "B.Mask") (net "{net}") (uuid "u-{ref}-{k}"))\n'
        for k, dx, net in ((1, -0.4, n1), (2, 0.4, n2)))
    return (f'  (footprint "t:{ref}" (layer "B.Cu") (at {x} {y} 0)\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "B.SilkS"))\n'
            f'{pads}  )\n')


def test_pad_under_ball_is_a_placement_fault(tmp_path):
    """#994 (haos-voice U301 over C305/C306): a far-side cap whose pads sit
    under balls of other nets leaves those balls no via site at all; the
    place gate's place_metrics flags it. Planted: C1's pads (/CAPA, /CAPB)
    straight under E5 and E6, four rings deep in a 10 x 10 field (at
    this board's 0.089 mm tracks the outer four rings could leave on the
    surface and are not judged). Quiet twin: the
    same part where each pad takes its ball's net (a decoupler's via in
    pad)."""
    import place_metrics
    # E5 and E6 of the 10 x 10, 0.8 mm field centred on (20, 15)
    planted = board(tmp_path, name="bad", n=10,
                    extra=_back_cap("C1", 20.0, 14.6, "/CAPA", "/CAPB"))
    v, hits = place_metrics.under_ball(planted)
    errs = {x["ball"]: x for x in v if x["severity"] == "error"}
    assert set(errs) == {"U1.E5", "U1.E6"}, v
    assert all(x["kind"] == "pad_under_ball" and x["sites_free"] == 0
               and "C1" in x["refs"] for x in errs.values())
    over = {(x["ball"], x["pad_net"]) for x in v
            if x["severity"] == "error" and x["pad"].startswith("C1")
            and "under U1 ball" in x["msg"]}
    assert ("U1.E5", "/CAPA") in over and ("U1.E6", "/CAPB") in over
    payload, _ = place_metrics.run(["--pcb", str(planted)])
    assert any(x.get("kind") == "pad_under_ball"
               for x in payload["violations"])
    assert "ball_via_sites" in payload["coverage"]["failed"]

    quiet = board(tmp_path, name="ok", n=10,
                  extra=_back_cap("C1", 20.0, 14.6, "/S4_4", "/S4_5"))
    v, hits = place_metrics.under_ball(quiet)
    assert v == [], v
    # it still covers other balls' dog-bone spots, each with sites to spare
    assert hits and all(h["sites_free"] and not h["over_ball"]
                        for h in hits)
    # and a board with no ball field has nothing to judge
    assert place_metrics.under_ball(board(
        tmp_path, name="none", n=2, connector=False)) == ([], [])


# ============================================================ live (slow)

def test_live_escape_synthetic_bga(tmp_path):
    if env.find_kicad_cli() is None or env.find_krt() is None:
        pytest.skip("needs kicad-cli and KiCadRoutingTools")
    pcb = board(tmp_path)
    out = tmp_path / "r.json"
    rc = re_.main(["--pcb", str(pcb), "--out-report", str(out)])
    rep = json.loads(out.read_text())
    assert rc == 0, rep
    f = rep["facts"]
    (b,) = f["bgas"]
    assert b["method"] == "dogbone"
    assert b["krt"]["escaped"] == 36
    assert b["kept_nets"] == 36 and b["left_for_routers"] == []
    assert f["drc_delta"]["new"] == {}
    assert f["board_updated"] and f["dru_rules"] == ["aiee_bga_escape_U1"]
    dru = pcb.with_suffix(".kicad_dru").read_text()
    assert dru.startswith("(version 1)") and "aiee_bga_escape_U1" in dru
    bg = geom.BoardGeom.from_file(pcb)
    assert bg.vias_of() and bg.tracks_of()
    # no via sits in a ball (dog-bone, not via-in-pad)
    (g,) = bgalib.find_bgas(bg)
    assert re_.via_in_pad_nets(bg, g, {v.uuid for v in bg.vias_of()}) == {}
    # 0.8 mm dog-bone vias have ring to spare: no teardrop setting
    assert b["teardrops"] == 0


def test_live_power_first_lands_before_the_escape(tmp_path):
    """#923 point 1 live: U5's +3V3 pin gets its plane via, DRC-clean, and
    the field still escapes in full around it."""
    if env.find_kicad_cli() is None or env.find_krt() is None:
        pytest.skip("needs kicad-cli and KiCadRoutingTools")
    pcb, cons = _power_board(tmp_path)
    out = tmp_path / "r.json"
    rc = re_.main(["--pcb", str(pcb), "--constraints", str(cons),
                   "--out-report", str(out)])
    rep = json.loads(out.read_text())
    assert rc == 0, rep
    f = rep["facts"]
    assert f["power_first"]["placed"] == 1, f["power_first"]
    assert "dropped_for_drc" not in f["power_first"]
    assert f["drc_delta"]["new"] == {} and f["board_updated"]
    bg = geom.BoardGeom.from_file(pcb)
    near = [v for v in bg.vias_of(net="+3V3")
            if math.hypot(v.at[0] - 32.0, v.at[1] - 8.0) < 1.5]
    assert len(near) == 1
    (b,) = f["bgas"]
    assert b["kept_nets"] >= 1 and b["left_for_routers"] == []


# ---- #552 via-in-pad clashes, wide nets; #547 escape toward the partner ----

def test_open_sides_face_the_partner():
    assert re_.open_sides(-3.0, 0.2) == ("left",)
    assert re_.open_sides(-2.0, -2.0) == ("left", "up")
    assert re_.open_sides(0.1, 4.0) == ("down",)
    assert re_.open_sides(0.0, 0.0) == ()


def test_partner_sides_from_nearest_off_part_pad(tmp_path):
    bg = geom.BoardGeom.from_file(board(tmp_path))     # J1 sits below U1
    (b,) = bgalib.find_bgas(bg)
    s = re_.partner_sides(bg, b, set(b.nets.values()))
    assert len(s) == 36 and all("down" in v for v in s.values())
    assert all("up" not in v for v in s.values())
    bg2 = geom.BoardGeom.from_file(board(tmp_path, name="c", connector=False))
    (b2,) = bgalib.find_bgas(bg2)
    assert re_.partner_sides(bg2, b2, set(b2.nets.values())) == {}


def test_fanout_nets_hold_wide_nets_and_keep_plane_balls_out():
    b = bgalib.Bga(ref="U1", layer="F.Cu", pitch=0.8, rows=3, cols=3,
                   pad_d=0.32, bbox=(0, 0, 1.6, 1.6), balls=9,
                   nets={"A1": "/S", "A2": "VBUS", "A3": "GND", "B1": "GND",
                         "B2": "GND", "B3": "GND", "C1": "GND", "C2": "GND",
                         "C3": "/T"})
    ok, held = re_.fanout_nets(b, {"VBUS": 0.32, "/T": 0.0889}, 0.0889)
    assert ok == ["/S", "/T"] and held == ["VBUS"]


def test_toward_nets_pairs_and_length_groups():
    cons = {"length_groups": [{"name": "B0", "nets": ["DRAM_D[0-1]"],
                               "ref_nets": ["DRAM_SDQS0_P"]}, "junk"]}
    specs = [{"p": "/USB_P", "n": "/USB_N"}]
    nets = {"/DRAM_D0", "/DRAM_D1", "/DRAM_D2", "/DRAM_SDQS0_P", "/USB_P",
            "/USB_N"}
    assert re_.toward_nets(cons, nets, specs) == [
        "/DRAM_D0", "/DRAM_D1", "/DRAM_SDQS0_P", "/USB_N", "/USB_P"]


def _via(x, y, net, uid, size=0.45, drill=0.2):
    return (f'  (via (at {x} {y}) (size {size}) (drill {drill}) '
            f'(layers "F.Cu" "B.Cu") (net "{net}") (uuid "{uid}"))\n')


def test_vip_conflicts_drop_the_in_pad_ball_not_its_neighbour(tmp_path):
    """A1 (/S0_0) at (18, 13): its via-in-pad is 0.566 mm from the gap via
    between A1/A2/B1/B2 (/S1_1's dog-bone): 0.366 mm hole to hole < 0.5."""
    extra = _via(18, 13, "/S0_0", "vip") + _via(18.4, 13.4, "/S1_1", "gap")
    bg = geom.BoardGeom.from_file(board(tmp_path, extra=extra))
    (b,) = bgalib.find_bgas(bg)
    assert re_.vip_conflicts(bg, b, {"vip", "gap"}, 0.5, 0.125) == {"/S0_0"}
    # old via-in-pad copper is not this run's
    assert re_.vip_conflicts(bg, b, {"gap"}, 0.5, 0.125) == set()
    # at KRT's own 0.2 hole-to-hole the pair is legal
    assert re_.vip_conflicts(bg, b, {"vip", "gap"}, 0.2, 0.1) == set()


def test_vip_conflicts_floor_via_grazing_a_stub(tmp_path):
    """A 0.45 via in a 0.35 pad bulges past it: a foreign F.Cu stub threaded
    past the pad is 0.06 mm from the via."""
    seg = ('  (segment (start 18.33 12.5) (end 18.33 13.5) (width 0.0889) '
           '(layer "F.Cu") (net "/S0_1") (uuid "s1"))\n')
    extra = _via(18, 13, "/S0_0", "vip") + seg
    bg = geom.BoardGeom.from_file(board(tmp_path, extra=extra))
    (b,) = bgalib.find_bgas(bg)
    assert re_.vip_conflicts(bg, b, {"vip"}, 0.5, 0.125) == {"/S0_0"}
    bg2 = geom.BoardGeom.from_file(board(tmp_path, name="c",
                                         extra=_via(18, 13, "/S0_0", "vip")))
    (b2,) = bgalib.find_bgas(bg2)
    assert re_.vip_conflicts(bg2, b2, {"vip"}, 0.5, 0.125) == set()



# ---- #552: via-in-pad at 0.8 mm gets the fab's smallest filled via ---------

def test_pad_via_only_where_the_escape_via_overhangs_the_pad():
    """0.45 in a 0.32 pad at 0.8 mm on 6 layers: the 0.25/0.15 filled via.
    Not on 4 layers, not where the floor via fits the pad, not at 0.5 mm
    (fine_via's field), not where it would itself overhang."""
    pv = bgalib.pad_via(_bga(0.8, 0.32), 6, JLC_ROW, {6, 8}, 0.45)
    assert (pv["via_diameter"], pv["via_drill"]) == (0.25, 0.15)
    assert bgalib.pad_via(_bga(0.8, 0.32), 4, JLC_ROW, {4, 6}, 0.45) is None
    assert bgalib.pad_via(_bga(0.8, 0.5), 6, JLC_ROW, {6}, 0.45) is None
    assert bgalib.pad_via(_bga(0.5, 0.25), 6, JLC_ROW, {6}, 0.45) is None
    assert bgalib.pad_via(_bga(0.8, 0.2), 6, JLC_ROW, {6}, 0.45) is None
    assert bgalib.pad_via(_bga(0.8, 0.32), 6, {}, {6}, 0.45) is None
    # fine_via still answers its own 0.5 mm field from the same floors
    assert bgalib.fine_via(_bga(0.5, 0.25), 6, JLC_ROW, {6}) == \
        bgalib.smallest_filled_via(6, JLC_ROW, {6})


def test_shrink_pad_vias_swaps_only_new_through_vias_in_own_pads(tmp_path):
    vip, old, gap = (UID % k for k in (5521, 5522, 5523))  # pcbnew keeps
    extra = (_via(18, 13, "/S0_0", vip) + _via(18.8, 13, "/S0_1", old)
             + _via(18.4, 13.4, "/S1_1", gap))          # only real uuids
    pcb = board(tmp_path, pad=0.32, layers=6, extra=extra)
    bg = geom.BoardGeom.from_file(pcb)
    (b,) = bgalib.find_bgas(bg)
    pv = bgalib.pad_via(b, 6, JLC_ROW, {6}, 0.45)
    assert re_.shrink_pad_vias(pcb, bg, b, {vip, gap}, pv) == 1
    bg2 = geom.BoardGeom.from_file(pcb)
    sizes = {(v.net, v.at): (round(v.diameter, 3), round(v.drill, 3))
             for v in bg2.vias_of()}
    assert sizes == {("/S0_0", (18.0, 13.0)): (0.25, 0.15),
                     ("/S0_1", (18.8, 13.0)): (0.45, 0.2),
                     ("/S1_1", (18.4, 13.4)): (0.45, 0.2)}
    (b2,) = bgalib.find_bgas(bg2)
    # the shrunk via clears the gap via at the fine floor: no clash left
    new = {v.uuid for v in bg2.vias_of() if v.net != "/S0_1"}
    assert re_.vip_conflicts(bg2, b2, new, pv["hole_to_hole"], 0.1) == set()


@pytest.mark.parametrize("layers,via,rule", [
    (6, (0.25, 0.15), True),        # route_escape shrank it: keep its rule
    (6, None, False),               # nothing shrunk: board floors only
    (6, (0.45, 0.2), False),        # a floor via in the pad needs no rule
    (4, (0.25, 0.15), False)])      # 4 layers never get the small via
def test_rules_gen_keeps_the_pad_via_rule_while_shrunk_vias_stand(
        tmp_path, layers, via, rule):
    extra = _via(18, 13, "/S0_0", "vip", *via) if via else ""
    bg = geom.BoardGeom.from_file(board(tmp_path, pad=0.32, layers=layers,
                                        extra=extra))
    cap = {"min_trace_width_mm": 0.0889, "min_clearance_mm": 0.0889,
           "min_via_diameter_mm": 0.45, "min_via_drill_mm": 0.2,
           "min_annular_ring_mm": 0.15, **JLC_ROW}
    rules, (f,) = rules_gen.bga_rules(bg, cap)
    assert ("aiee_bga_fine_via_U1" in [r.name for r in rules]) is rule
    assert f["method"] != "underpad"        # still the dog-bone field
    if rule:
        assert f["pad_via_mm"] == [0.25, 0.15]
        (b,) = bgalib.find_bgas(bg)
        pv = bgalib.pad_via(b, 6, cap, {6}, 0.45)
        text = "(version 1)\n" + "\n".join(r.render() for r in rules) + "\n"
        # route_escape's writer sees rules_gen's text as its own
        assert bgalib.ensure_rules(text, [("U1", f["clearance_mm"], pv)])[1] \
            == []


@pytest.mark.parametrize("layers,pad,flag,pv", [
    (6, 0.32, "--allow-via-in-pad", True),
    (6, 0.32, None, True),          # 6 layers keep in-pad vias by default
    (4, 0.32, "--allow-via-in-pad", False),
    (6, 0.5, "--allow-via-in-pad", False),
    (6, 0.32, "--no-via-in-pad", False)])
def test_plan_names_the_pad_via_and_its_cost(tmp_path, capsys, layers, pad,
                                             flag, pv):
    pcb = board(tmp_path, n=8, pad=pad, layers=layers)
    args = ["--pcb", str(pcb), "--plan", "--method", "underpad"]
    assert re_.main(args + ([flag] if flag else [])) == 0
    (b,) = json.loads(capsys.readouterr().out)["facts"]["bgas"]
    assert bool(b.get("pad_via")) is pv
    if pv:
        assert b["pad_via"]["via_mm"] == [0.25, 0.15]
        assert b["pad_via"]["rule"] == "aiee_bga_fine_via_U1"
        assert "paid via" in b["pad_via"]["fab_cost"]
        assert "fine_via" not in b      # the 0.8 mm plan is not #1252's


def _pad_via_run(tmp_path, monkeypatch=None):
    if env.find_kicad_cli() is None or env.find_krt() is None:
        pytest.skip("needs kicad-cli and KiCadRoutingTools")
    if monkeypatch:     # the planted fault: main's no-shrink path
        monkeypatch.setattr(re_, "shrink_pad_vias", lambda *a, **k: 0)
    pcb = board(tmp_path, n=8, pad=0.32, layers=6)
    out = tmp_path / "r.json"
    rc = re_.main(["--pcb", str(pcb), "--allow-via-in-pad", "--method",
                   "underpad", "--out-report", str(out)])
    rep = json.loads(out.read_text())
    assert rc in (0, 1), rep
    (b,) = rep["facts"]["bgas"]
    return pcb, rep["facts"], b


def test_live_pad_via_0_8mm_keeps_every_ball(tmp_path):
    """The quiet twin: an 8x8 0.8 mm field with 0.32 pads on 6 layers,
    underpad with --allow-via-in-pad. Every kept in-pad via is the filled
    0.25/0.15 one, none clashes with KRT's stubs, all 64 balls escape and
    DRC is clean under the courtyard's fine rule."""
    pcb, f, b = _pad_via_run(tmp_path)
    vip = b["via_in_pad"]
    assert vip["kept"] and vip["shrunk"] == vip["vias"] >= 4
    assert not vip.get("clashing") and b["left_for_routers"] == []
    assert b["kept_nets"] == 64
    assert f["drc_delta"]["new"] == {}
    assert "aiee_bga_fine_via_U1" in pcb.with_suffix(".kicad_dru").read_text()
    bg = geom.BoardGeom.from_file(pcb)
    (g,) = bgalib.find_bgas(bg)
    new = {v.uuid for v in bg.vias_of()}
    assert {(round(v.diameter, 3), round(v.drill, 3))
            for v in re_.in_pad_vias(bg, g, new)} == {(0.25, 0.15)}
    assert vip["filled"] == vip["vias"]


def test_live_fault_unshrunk_pad_vias_clash(tmp_path, monkeypatch):
    """The planted fault: with the shrink taken out (main's path) the 0.45
    vias overhang the 0.32 pads onto KRT's stubs; balls clash and are left
    for the routers. Shows the quiet twin's 64 come from the shrink."""
    pcb, f, b = _pad_via_run(tmp_path, monkeypatch)
    assert b["via_in_pad"].get("clashing")
    assert b["kept_nets"] < 64
    assert set(b["via_in_pad"]["clashing"]) <= set(b["left_for_routers"])
    bg = geom.BoardGeom.from_file(pcb)
    for net in b["via_in_pad"]["clashing"]:     # the rip leaves no stubs
        assert not bg.vias_of(net) and not bg.tracks_of(net)


# ---- #979: a starved thermal names the pad, never the track ---------------

CAP = ('  (footprint "t:C1" (layer "B.Cu") (at 18.4 13.4)\n'
       '    (property "Reference" "C1" (at 0 0 0) (layer "B.SilkS"))\n'
       '    (pad "2" smd rect (at 0 0) (size 0.3 0.3) (layers "B.Cu")\n'
       '      (net "GND") (uuid "c1-2"))\n  )\n')
STARVED = "Pad 2 [GND] of C1 on B.Cu"
UID = "00000000-0000-4000-8000-%012d"


def _starved_drc(staged, cut="/S0_0", also=None):
    """A DRC that finds C1's GND pad starved while a via of net `cut` (or,
    cut=None, always) is on the board: one spoke of two left."""
    def drc():
        bg = geom.BoardGeom.from_file(staged)
        vs = []
        if cut is None or any(v.net == cut for v in bg.vias_of()):
            vs.append({"check": "starved_thermal", "severity": "error",
                       "items": [{"msg": "Zone [GND] on B.Cu, priority 0",
                                  "pos": [1, 1]},
                                 {"msg": STARVED, "pos": [18.4, 13.4]}]})
        return {"violations": vs + (also or [])}
    return drc


def test_escape_rips_the_net_that_starved_a_thermal_and_keeps_the_rest(
        tmp_path):
    """#979's planted fault: an escape via of /S0_0 beside a 2-spoke GND pad
    under the field cuts a spoke; DRC names only GND. That one net is
    ripped and the rest of the escape stays (it was all thrown away)."""
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("the rip edits the board through KiCad's python")
    # gap-site vias clear of every ball (KiCad keeps real uuids only)
    extra = (CAP + _via(18.4, 13.95, "/S0_0", UID % 1)
             + _via(20.0, 15.0, "/S2_2", UID % 2)
             + ('  (segment (start 18.4 13.4) (end 17.5 13.4) (width 0.1) '
                f'(layer "F.Cu") (net "/S0_1") (uuid "{UID % 3}"))\n'))
    pcb = board(tmp_path, extra=extra)
    (b,) = bgalib.find_bgas(geom.BoardGeom.from_file(pcb))
    after, delta, rounds = re_.grade_and_rip(
        pcb, set(), {"violations": []}, [b], _starved_drc(pcb))
    assert not delta["new"]
    assert [r["nets"] for r in rounds] == [["/S0_0"]]
    assert rounds[0]["starving_thermals"] == ["/S0_0"]
    assert rounds[0]["fields"] == {"/S0_0": ["U1"]}
    bg = geom.BoardGeom.from_file(pcb)
    assert {v.net for v in bg.vias_of()} == {"/S2_2"}
    assert {t.net for t in bg.tracks_of()} == {"/S0_1"}
    assert re_.unexplained_starving({"violations": []}, after, delta, pcb,
                                    set()) == {}


def test_a_starved_thermal_no_new_copper_explains_keeps_the_escape(
        tmp_path):
    """#979: C1's pad starves with no new copper next to it (the cap nudge
    or the refill cut it). Nothing is ripped; the pad is reported and the
    escape kept. With any other new error left it is thrown away."""
    pcb = board(tmp_path, extra=CAP + _via(20.0, 15.0, "/S2_2", UID % 2))
    (b,) = bgalib.find_bgas(geom.BoardGeom.from_file(pcb))
    none = {"violations": []}
    after, delta, rounds = re_.grade_and_rip(
        pcb, set(), none, [b], _starved_drc(pcb, cut=None))
    assert rounds == [] and delta["new"] == {"starved_thermal": 1}
    assert re_.unexplained_starving(none, after, delta, pcb, set()) == {
        STARVED: (18.4, 13.4)}
    # the pad was starved before the run: not this escape's
    assert re_.unexplained_starving(after, after, delta, pcb, set()) == {}
    short = {"check": "shorting_items", "severity": "error", "items": []}
    after, delta, _ = re_.grade_and_rip(
        pcb, set(), none, [b], _starved_drc(pcb, cut=None, also=[short]))
    assert re_.unexplained_starving(none, after, delta, pcb, set()) == {}

def test_live_escape_kept_when_a_far_thermal_starves(tmp_path, monkeypatch):
    """#979 through run(): the refill starves a GND pad far from any escape
    copper. The escape is kept and swapped in, and the pad is reported as
    an escape_starved_thermal error, not a CheckError that drops it all."""
    if env.find_kicad_cli() is None or env.find_krt() is None:
        pytest.skip("needs kicad-cli and KiCadRoutingTools")
    far = CAP.replace("18.4 13.4", "36 36").replace("C1", "C9")
    pcb = board(tmp_path, extra=far)
    msg = STARVED.replace("C1", "C9")
    real = re_.grade_and_rip

    def starving(staged, old_uuids, before, bgas, drc):
        def drc2():
            rep = drc()
            rep["violations"].append(
                {"check": "starved_thermal", "severity": "error",
                 "items": [{"msg": "Zone [GND] on B.Cu", "pos": [1, 1]},
                           {"msg": msg, "pos": [36, 36]}]})
            return rep
        return real(staged, old_uuids, before, bgas, drc2)
    monkeypatch.setattr(re_, "grade_and_rip", starving)
    out = tmp_path / "r.json"
    re_.main(["--pcb", str(pcb), "--out-report", str(out)])
    rep = json.loads(out.read_text())
    f = rep["facts"]
    assert f["board_updated"] and f["bgas"][0]["kept_nets"] == 36
    (v,) = [v for v in rep["violations"]
            if v.get("kind") == "escape_starved_thermal"]
    assert v["severity"] == "error" and msg in v["msg"]
    # the plane fixer gives the spokes back, not a human review
    assert cluster_violations.cluster([v], 5.0)[0]["fixer"] == "plane"
    assert geom.BoardGeom.from_file(pcb).vias_of()


def test_live_escape_toward_partner(tmp_path):
    """--toward-partner: J1 sits below U1, so no escape leaves by the top."""
    if env.find_kicad_cli() is None or env.find_krt() is None:
        pytest.skip("needs kicad-cli and KiCadRoutingTools")
    pcb = board(tmp_path)
    (tmp_path / "constraints.json").write_text(json.dumps(
        {"length_groups": [{"name": "all", "nets": ["S*"]}]}),
        encoding="utf-8")
    out = tmp_path / "r.json"
    rc = re_.main(["--pcb", str(pcb), "--toward-partner",
                   "--out-report", str(out)])
    rep = json.loads(out.read_text())
    assert rc in (0, 1), rep
    f = rep["facts"]
    (b,) = f["bgas"]
    assert sum(b["toward_partner"].values()) == 36
    assert "up" not in "+".join(b["toward_partner"])
    assert f["drc_delta"]["new"] == {}
    bg = geom.BoardGeom.from_file(pcb)
    (g,) = bgalib.find_bgas(bg)
    x0, y0, x1, _ = g.bbox
    pts = [pt for t in bg.tracks_of() for pt in t.shape.coords]
    # nothing crosses the top edge: KRT's field edge is half a pitch past
    # the ball centres, a left/right escape may run in that rim
    rim = g.pitch / 2 + 0.05
    assert pts and not [(x, y) for x, y in pts
                        if y < y0 - rim and x0 - rim <= x <= x1 + rim]
    # the closed top costs gap sites: the rest fall back to via-in-pad,
    # stripped without --allow-via-in-pad, and are left for the routers
    assert b["kept_nets"] >= 12
    assert b["kept_nets"] + len(b["left_for_routers"]) == 36


# ---- #325 fault tests: a planted bad escape is ripped, the board stays clean

def _plant_after_fanout(monkeypatch, text):
    """Run the real KRT fan-out, then add `text` (a via or a track the
    escape must not keep) to the staged board once, as if KRT had laid it."""
    real = re_.fan_one
    done = []

    def fan(ctx, *a, **kw):
        out = real(ctx, *a, **kw)
        if not done:
            staged = ctx["staged"]
            body = staged.read_text(encoding="utf-8").rstrip()
            assert body.endswith(")")
            staged.write_text(body[:-1] + text + ")\n", encoding="utf-8")
            done.append(1)
        return out
    monkeypatch.setattr(re_, "fan_one", fan)


def _faulted_run(tmp_path, monkeypatch, text, uid):
    if env.find_kicad_cli() is None or env.find_krt() is None:
        pytest.skip("needs kicad-cli and KiCadRoutingTools")
    pcb = board(tmp_path)
    _plant_after_fanout(monkeypatch, text)
    out = tmp_path / "r.json"
    rc = re_.main(["--pcb", str(pcb), "--out-report", str(out)])
    rep = json.loads(out.read_text())
    assert rc in (0, 1), rep
    f = rep["facts"]
    (b,) = f["bgas"]
    # caught: a new DRC error, ripped, nothing new left on the board
    (rd, *_) = f["drc_delta"]["rip_rounds"]
    assert rd["new_errors"]
    assert f["drc_delta"]["new"] == {}
    bg = geom.BoardGeom.from_file(pcb)
    assert uid not in {x.uuid for x in list(bg.tracks_of())
                       + list(bg.vias_of())}
    return b, rd


def test_live_fault_via_on_a_neighbours_pad_is_ripped(tmp_path, monkeypatch):
    """A2's dog-bone (/S0_1) run the wrong way: its stub from A2 (18.8, 13)
    to a via dropped on A1's pad (/S0_0, at 18, 13). DRC's short names the
    via's and the stub's net; that net is ripped and left for the routers,
    and the rest of the field keeps its escape. (A lone via on a pad is
    not this fault: KiCad gives it the pad's net on load.)"""
    uid, sid = UID % 901, UID % 903
    stub = ('  (segment (start 18.8 13) (end 18 13) (width 0.1) '
            f'(layer "F.Cu") (net "/S0_1") (uuid "{sid}"))\n')
    b, rd = _faulted_run(tmp_path, monkeypatch,
                         _via(18, 13, "/S0_1", uid, 0.5) + stub, uid)
    assert "/S0_1" in rd["nets"] and len(rd["nets"]) <= 3
    assert "/S0_1" in b["ripped_by_drc"] and "/S0_1" in b["left_for_routers"]
    assert b["kept_nets"] >= 33


def test_live_fault_track_between_two_pads_is_ripped(tmp_path, monkeypatch):
    """A 0.3 mm /S5_5 track run down between A1 and A2 (0.45 mm apart edge
    to edge): 0.075 mm a side, under the field's 0.0889 floor. DRC names
    the track's net; it is ripped and the rest of the field kept."""
    uid = UID % 902
    seg = ('  (segment (start 18.4 11.8) (end 18.4 13.0) (width 0.3) '
           f'(layer "F.Cu") (net "/S5_5") (uuid "{uid}"))\n')
    b, rd = _faulted_run(tmp_path, monkeypatch, seg, uid)
    assert "/S5_5" in rd["nets"] and len(rd["nets"]) <= 3
    assert "/S5_5" in b["ripped_by_drc"]
    assert b["kept_nets"] >= 33


# ---- #325: via-in-pad follows the fab's price for filled, capped vias

def test_free_filled_via_layers_reads_the_price_table():
    free = re_.free_filled_via_layers()
    assert 6 in free and 4 not in free
    assert free <= re_.filled_via_layers()


@pytest.mark.parametrize("n,allow,forbid,keep", [
    (6, False, False, True),     # POFV free on 6+ layers: kept
    (4, False, False, False),    # priced on 4 layers: stripped
    (4, True, False, True),      # paid for on purpose
    (6, False, True, False),     # refused on purpose
])
def test_via_in_pad_choice(n, allow, forbid, keep):
    got, why = re_.via_in_pad_choice(n, allow, forbid, {6, 8})
    assert got is keep and why


def test_via_in_pad_choice_flags_conflict():
    with pytest.raises(re_.CheckError):
        re_.via_in_pad_choice(6, True, True, {6})


# ---- #1252: a fine field on a stack that fills vias free gets the fab's
# smallest via, filled, in the pad; a 4-layer board never does

JLC_ROW = {"abs_min_via_diameter_mm": 0.25, "abs_min_via_drill_mm": 0.15,
           "abs_min_via_hole_to_hole_mm": 0.2}


def test_fine_via_only_for_fine_fields_on_free_fill_stacks():
    fv = bgalib.fine_via(_bga(0.5, 0.25), 6, JLC_ROW, {6, 8})
    assert fv == {"via_diameter": 0.25, "via_drill": 0.15, "annular": 0.05,
                  "hole_to_hole": 0.2, "hole_clearance": 0.2}
    assert bgalib.fine_via(_bga(0.5, 0.25), 8, JLC_ROW, {6, 8})
    # never on 4 layers, even if the table ever priced its fill at 0
    assert bgalib.fine_via(_bga(0.5, 0.25), 4, JLC_ROW, {4, 6}) is None
    assert bgalib.fine_via(_bga(0.5, 0.25), 6, JLC_ROW, {8}) is None
    assert bgalib.fine_via(_bga(0.65, 0.3), 6, JLC_ROW, {6}) is None
    assert bgalib.fine_via(_bga(0.5, 0.25), 6, {}, {6}) is None
    assert bgalib.fine_via(_bga(0.5, 0.25), 6, None, {6}) is None


def test_fine_via_budget_clearance_is_hole_clearance_less_ring():
    fv = bgalib.fine_via(_bga(0.5, 0.25), 6, JLC_ROW, {6})
    ep = bgalib.escape_params(_bga(0.5, 0.25), {**JLC6, **fv})
    assert ep["via_size"] == pytest.approx(0.25)
    assert ep["via_drill"] == pytest.approx(0.15)
    assert ep["clearance"] == pytest.approx(0.15)


def test_the_real_jlc_rows_give_0_25_0_15_from_6_layers():
    assert bgalib.free_fill_layers({"options": {"via_fill_epoxy": {
        4: 30.0, 6: 0, 8: 0.0}}}) == {6, 8}
    assert bgalib.free_fill_layers({}) == set()
    for n in (6, 8):
        fv = bgalib.fine_via(_bga(0.5, 0.25), n, re_.fine_cap_row(n),
                             re_.free_filled_via_layers())
        assert (fv["via_diameter"], fv["via_drill"]) == (0.25, 0.15)
    assert bgalib.fine_via(_bga(0.5, 0.25), 4, re_.fine_cap_row(4),
                           re_.free_filled_via_layers()) is None


def test_fine_rule_text_and_ensure_rules():
    fv = bgalib.fine_via(_bga(0.5, 0.25), 6, JLC_ROW, {6})
    r = bgalib.fine_rule("U2", fv)
    assert r.startswith('(rule "aiee_bga_fine_via_U2"\n')
    for c, v in (("via_diameter", "0.2500"), ("hole_size", "0.1500"),
                 ("annular_width", "0.0500"), ("hole_to_hole", "0.2000"),
                 ("hole_clearance", "0.2000")):
        assert f"(constraint {c} (min {v}mm))" in r
    assert bgalib.rule_condition("U2") in r
    t, ch = bgalib.ensure_rules("", [("U2", 0.15, fv)])
    assert ch == ["aiee_bga_escape_U2", "aiee_bga_fine_via_U2"]
    assert bgalib.ensure_rules(t, [("U2", 0.15, fv)]) == (t, [])
    # no fine via: only the clearance rule, as before
    t0, ch0 = bgalib.ensure_rules("", [("U2", 0.15, None)])
    assert ch0 == ["aiee_bga_escape_U2"] and "fine_via" not in t0


@pytest.mark.parametrize("layers,fine", [(6, True), (4, False)])
def test_rules_gen_writes_the_fine_via_rule_on_6_layers_only(tmp_path,
                                                             layers, fine):
    bg = geom.BoardGeom.from_file(board(tmp_path, pitch=0.5, pad=0.25,
                                        layers=layers))
    cap = {"min_trace_width_mm": 0.0889, "min_clearance_mm": 0.0889,
           "min_via_diameter_mm": 0.45, "min_via_drill_mm": 0.2,
           "min_annular_ring_mm": 0.15, **JLC_ROW}
    rules, facts = rules_gen.bga_rules(bg, cap)
    names = [r.name for r in rules]
    assert ("aiee_bga_fine_via_U1" in names) is fine
    assert (facts[0]["method"] == "underpad") is fine
    if fine:
        fv = bgalib.fine_via(bgalib.find_bgas(bg)[0], 6, cap, {6})
        text = "(version 1)\n" + "\n".join(r.render() for r in rules) + "\n"
        # route_escape's writer sees rules_gen's rules as its own
        assert bgalib.ensure_rules(text, [("U1", 0.15, fv)])[1] == []


@pytest.mark.parametrize("layers,fine", [(6, True), (4, False)])
def test_plan_picks_the_fine_via_from_layers_and_pitch(tmp_path, capsys,
                                                       layers, fine):
    pcb = board(tmp_path, pitch=0.5, pad=0.25, layers=layers)
    assert re_.main(["--pcb", str(pcb), "--plan"]) == 0
    (b,) = json.loads(capsys.readouterr().out)["facts"]["bgas"]
    assert ("fine_via" in b) is fine
    if fine:
        assert b["method"] == "underpad"
        assert (b["via_size"], b["via_drill"]) == (0.25, 0.15)
        assert b["fine_via"]["rule"] == "aiee_bga_fine_via_U1"
    else:       # the 4-layer plan is what main made of it
        assert b["method"] != "underpad" and b["via_size"] >= 0.45
    # a coarse field on 6 layers keeps its dog-bone
    pcb8 = board(tmp_path, name="c", layers=6)
    assert re_.main(["--pcb", str(pcb8), "--plan"]) == 0
    (b8,) = json.loads(capsys.readouterr().out)["facts"]["bgas"]
    assert b8["method"] == "dogbone" and "fine_via" not in b8


def _fine_run(tmp_path, monkeypatch=None, text=None):
    if env.find_kicad_cli() is None or env.find_krt() is None:
        pytest.skip("needs kicad-cli and KiCadRoutingTools")
    pcb = board(tmp_path, pitch=0.5, pad=0.25, layers=6)
    if text:
        _plant_after_fanout(monkeypatch, text)
    out = tmp_path / "r.json"
    rc = re_.main(["--pcb", str(pcb), "--out-report", str(out)])
    rep = json.loads(out.read_text())
    assert rc in (0, 1), rep
    (b,) = rep["facts"]["bgas"]
    return pcb, rep["facts"], b


def test_live_fine_via_escape_6_layer_quiet(tmp_path):
    """The quiet twin: a 0.5 mm field on 6 layers escapes through filled
    0.25/0.15 vias in its pads, DRC-clean against the board's own 0.45 mm
    via floor (the courtyard rule lets the fine via pass there only)."""
    pcb, f, b = _fine_run(tmp_path)
    assert b["method"] == "underpad" and "fine_via" in b
    assert f["drc_delta"]["new"] == {}
    assert not b.get("ripped_by_drc")
    assert b["kept_nets"] >= 30
    dru = pcb.with_suffix(".kicad_dru").read_text()
    assert "aiee_bga_fine_via_U1" in dru
    # the courtyard copper rule keeps the field's own clearance: a wider one
    # (hole clearance minus the thin ring) is one Freerouting can't see and
    # its netclass-clearance wires broke it (#1252 6-layer bench)
    assert b["fine_via"]["rule_clearance"] < b["clearance"]
    assert bgalib.dru_rule("U1", b["fine_via"]["rule_clearance"]) in dru
    bg = geom.BoardGeom.from_file(pcb)
    (g,) = bgalib.find_bgas(bg)
    new = {v.uuid for v in bg.vias_of()}
    assert sum(re_.via_in_pad_nets(bg, g, new).values()) >= 4
    assert {(round(v.diameter, 3), round(v.drill, 3))
            for v in bg.vias_of()} == {(0.25, 0.15)}
    # the 0.05 mm ring is under JLC's 0.05 + 0.075 wander: every fine via
    # carries KiCad's teardrop setting, so the routers' inner-layer joins
    # get drops at the next refill (dfm_ring_breakout)
    tb = tdl.read(pcb, tdl.config(None))
    vias = [it for it in tb.items if it.kind == "via"]
    assert vias and all(it.enabled for it in vias)
    assert b["teardrops"] == len(vias)


def test_breakout_vias_only_under_the_ring_limit():
    """0.25/0.15 (ring 0.05) is under 0.05 + 0.075; a 0.45/0.2 via (0.125,
    on the limit) and 0.6/0.3 are not; a via with no uuid can't be named."""
    v = [geom.Via("/a", (0, 0), 0.25, 0.15, ("F.Cu", "B.Cu"), "u1"),
         geom.Via("/b", (1, 0), 0.45, 0.2, ("F.Cu", "B.Cu"), "u2"),
         geom.Via("/c", (2, 0), 0.6, 0.3, ("F.Cu", "B.Cu"), "u3"),
         geom.Via("/d", (3, 0), 0.25, 0.15, ("F.Cu", "B.Cu"), None)]
    assert [x.uuid for x in re_.breakout_vias(v)] == ["u1"]


def test_live_fault_fine_via_outside_the_courtyard_is_ripped(tmp_path,
                                                             monkeypatch):
    """The planted fault: a 0.25/0.15 via of /S5_5 dropped outside U1's
    courtyard, where the board's 0.45 mm via floor holds. The fine rule
    must not reach it: DRC names /S5_5, it is ripped, the field kept."""
    uid = UID % 904
    pcb, f, b = _fine_run(tmp_path, monkeypatch,
                          _via(28, 15, "/S5_5", uid, 0.25, 0.15))
    (rd, *_) = f["drc_delta"]["rip_rounds"]
    assert "/S5_5" in rd["nets"] and len(rd["nets"]) <= 3
    assert f["drc_delta"]["new"] == {}
    assert "/S5_5" in b["ripped_by_drc"]
    bg = geom.BoardGeom.from_file(pcb)
    assert uid not in {v.uuid for v in bg.vias_of()}
    assert b["kept_nets"] >= 29


# ---- row 139: enclosed balls out through a gate ----------------------------

RING_B = [(r, c) for r in range(1, 5) for c in range(1, 5)
          if r in (1, 4) or c in (1, 4)]
INNER = [(2, 2), (2, 3), (3, 2), (3, 3)]


def _ball_xy(r, c, pitch=0.5):
    return 20 + (c - 2.5) * pitch, 15 + (r - 2.5) * pitch


def _fenced(tmp_path, skip=()):
    """The 6x6 0.5 mm field on 6 layers after KRT: every ring-B ball has its
    filled 0.25/0.15 via in the pad. Through vias 0.5 apart leave 0.35 mm
    hole to hole and no lane a 0.0889 track can take (it needs 0.489)."""
    extra = "".join(_via(*_ball_xy(r, c), f"/S{r}_{c}", UID % (100 + i),
                         0.25, 0.15)
                    for i, (r, c) in enumerate(RING_B) if (r, c) not in skip)
    pcb = board(tmp_path, pitch=0.5, pad=0.25, layers=6, extra=extra,
                skip=skip)
    bg = geom.BoardGeom.from_file(pcb)
    (b,) = bgalib.find_bgas(bg)
    n = len(bg.copper_layers)
    floors = re_.escape_floors(pcb.with_suffix(".kicad_pro"))
    fv = bgalib.fine_via(b, n, re_.fine_cap_row(n),
                         re_.free_filled_via_layers())
    ep = bgalib.escape_params(b, {**floors, **fv})
    ep.update(fine_via=fv,
              rule_clearance=bgalib.escape_params(b, floors)["clearance"])
    layers, _ = re_.escape_layers(bg, b, None)
    return bg, b, ep, layers


def _inner_nets():
    return {f"/S{r}_{c}" for r, c in INNER}


def test_gate_trade_lets_the_enclosed_2x2_out(tmp_path):
    """The planted fault (row 139): KRT left the inner 2x2 unescaped, fenced
    by the ring-B vias. One ring-B ball per side gives up its via (the
    gate); both balls beside it jog through on an inner layer, each with a
    filled via in its own pad: 4 balls out for 2 traded."""
    bg, b, ep, layers = _fenced(tmp_path)
    pl = re_.gate_trade_plan(bg, b, ep, layers, _inner_nets(), set(), set())
    assert sorted(pl["escaped"]) == sorted(_inner_nets())
    assert pl["not_escaped"] == {}
    assert len(pl["traded"]) == 2
    ring_b = {f"/S{r}_{c}" for r, c in RING_B}
    assert set(pl["traded"]) <= ring_b
    assert sorted(n for v in pl["traded"].values() for n in v) == \
        sorted(_inner_nets())
    assert all(ly not in ("F.Cu", "B.Cu") for ly in pl["layer"].values())
    vias = [o for o in pl["ops"] if o["op"] == "add_via"]
    assert len(vias) == 4 and {(o["size"], o["drill"]) for o in vias} == \
        {(0.25, 0.15)}
    # every new track keeps the field's clearance to the vias that stay
    paths = [o for o in pl["ops"] if o["op"] == "add_path"]
    kept = [v for v in bg.vias_of() if v.net not in pl["traded"]]
    for o in paths:
        line = LineString(o["points"]).buffer(o["width"] / 2)
        for v in kept:
            if v.net != o["net"]:
                assert line.distance(v.poly) >= ep["rule_clearance"] - 1e-6
                assert line.distance(Point(v.at).buffer(v.drill / 2)) >= \
                    ep["fine_via"]["hole_clearance"] - 1e-6


def test_gate_trade_never_trades_a_protected_ball(tmp_path):
    """Pairs and held wide nets are never a gate: with all of ring B
    protected the enclosed balls stay unescaped, each with its reason."""
    bg, b, ep, layers = _fenced(tmp_path)
    ring_b = {f"/S{r}_{c}" for r, c in RING_B}
    pl = re_.gate_trade_plan(bg, b, ep, layers, _inner_nets(), ring_b, set())
    assert pl["escaped"] == [] and pl["ops"] == [] and pl["traded"] == {}
    assert set(pl["not_escaped"]) == _inner_nets()


def test_gate_trade_quiet_when_nothing_is_enclosed(tmp_path):
    """The quiet twin: no inner 2x2 (depopulated), and a ring-A ball KRT
    left is no enclosed ball: no gate is traded, nothing is added."""
    bg, b, ep, layers = _fenced(tmp_path, skip=INNER)
    for unesc in (set(), {"/S0_0"}):
        pl = re_.gate_trade_plan(bg, b, ep, layers, unesc, set(), set())
        assert pl["traded"] == {} and pl["ops"] == []
        assert pl["escaped"] == [] and pl["not_escaped"] == {}


def test_gate_trade_quiet_when_krt_escaped_them(tmp_path):
    """The quiet twin on the full field: the inner balls are there but KRT
    escaped them (none unescaped): the trade is only for a ball with no
    other way out."""
    bg, b, ep, layers = _fenced(tmp_path)
    pl = re_.gate_trade_plan(bg, b, ep, layers, set(), set(), set())
    assert pl["traded"] == {} and pl["ops"] == []


def test_live_gate_trade_escapes_the_enclosed_2x2(tmp_path):
    """The planted fault, live: KRT leaves the synthetic field's inner 2x2
    fenced by its ring-B vias. The gate trade lets all four out on an inner
    layer, DRC-clean, for two ring-B balls left to the routers (34 kept, not
    32); their in-pad vias are filled and teardropped like the rest."""
    pcb, f, b = _fine_run(tmp_path)
    ee = b["enclosed_escape"]
    assert sorted(ee["escaped"]) == sorted(_inner_nets())
    assert len(ee["traded"]) == 2 and "via in pad" in ee["fab_cost"]
    assert f["drc_delta"]["new"] == {} and not b.get("ripped_by_drc")
    assert b["kept_nets"] >= 34
    assert sorted(b["left_for_routers"]) == sorted(ee["traded"])
    bg = geom.BoardGeom.from_file(pcb)
    inner = {_ball_xy(r, c) for r, c in INNER}
    got = [v for v in bg.vias_of()
           if (round(v.at[0], 3), round(v.at[1], 3)) in inner]
    assert len(got) == 4
    tb = tdl.read(pcb, tdl.config(None))
    assert sum(1 for it in tb.items if it.kind == "via") == b["teardrops"]
    assert b["via_in_pad"]["filled"] >= 12 - 2 + 4
