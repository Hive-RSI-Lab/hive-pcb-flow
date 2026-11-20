"""#529: pairs between escaped BGAs.

- lib/krt_launch.py --via-joint-ends: a dog-bone via joint is not a stub tip
  (KRT picked the via deep in the ball field over the In2 stub end);
- route_critical: a two-pad termination across P/N is detached (it taps the
  pair; as a third terminal KRT routed legs from the pads), every routed
  rung is judged by check_diffpair and a rung with an uncoupled/impedance
  error is not kept, bare BGA balls are named in the failure;
- check_diffpair: copper inside the pair's ball field is breakout, not
  uncoupled run;
- route_escape: pairs with both balls in one field go to KRT as
  --diff-pairs at the pair's inner-layer gap; a DRC rip only removes the
  net's escape at the field where the error is.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import bgalib  # noqa: E402
import check_diffpair  # noqa: E402
import geom  # noqa: E402
import krt_launch  # noqa: E402
import route_critical as rc  # noqa: E402
import route_escape as re_  # noqa: E402


def seg(x1, y1, x2, y2, layer):
    return SimpleNamespace(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                           layer=layer)


# ------------------------------------------------------------ krt_launch

def test_via_joint_is_not_a_free_end():
    # ball (0,0) -F.Cu-> via (0.4,0.4) -In2-> tip (-2,0.4)
    segs = [seg(0, 0, 0.4, 0.4, "F.Cu"), seg(0.4, 0.4, -2.0, 0.4, "In2.Cu")]

    def krt(segments, pads, *a, **kw):   # KRT's per-layer answer
        return [(0.4, 0.4, "F.Cu"), (0.4, 0.4, "In2.Cu"), (-2.0, 0.4, "In2.Cu")]

    assert krt_launch.via_joint_free_ends(krt)(segs, []) == [(-2.0, 0.4, "In2.Cu")]


def test_only_joints_keeps_krt_answer():
    segs = [seg(0, 0, 1, 0, "F.Cu"), seg(1, 0, 1, 0, "B.Cu")]

    def krt(segments, pads, *a, **kw):
        return [(1, 0, "F.Cu")]

    assert krt_launch.via_joint_free_ends(krt)(segs, []) == [(1, 0, "F.Cu")]


def test_launcher_patches_free_ends(tmp_path):
    """krt_launch.py --via-joint-ends against a stand-in KRT: the script
    imports the patched function from connectivity."""
    (tmp_path / "connectivity.py").write_text(
        "def find_stub_free_ends(segments, pads):\n"
        "    return [(0.4, 0.4, 'F.Cu'), (-2.0, 0.4, 'In2.Cu')]\n",
        encoding="utf-8")
    (tmp_path / "route_x.py").write_text(
        "import json\nfrom types import SimpleNamespace as S\n"
        "from connectivity import find_stub_free_ends as f\n"
        "segs = [S(start_x=0, start_y=0, end_x=0.4, end_y=0.4, layer='F.Cu'),\n"
        "        S(start_x=0.4, start_y=0.4, end_x=-2.0, end_y=0.4, layer='In2.Cu')]\n"
        "print(json.dumps(f(segs, [])))\n", encoding="utf-8")
    cp = subprocess.run([sys.executable, str(SCRIPTS / "lib" / "krt_launch.py"),
                         "--via-joint-ends", "route_x.py"], cwd=tmp_path,
                        capture_output=True, text=True, check=True)
    assert cp.stdout.strip() == '[[-2.0, 0.4, "In2.Cu"]]'


# ------------------------------------------------------------ boards

def _fp(ref, x, y, pads):
    return (f'  (footprint "t:{ref}" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n{pads}  )\n')


def _pad(num, x, y, net=None, size=0.35, shape="circle"):
    n = f'\n      (net "{net}")' if net else ""
    return (f'    (pad "{num}" smd {shape}\n      (at {x} {y})\n'
            f'      (size {size} {size})\n      (layers "F.Cu"){n}\n'
            f'      (uuid "u-{num}-{net or "x"}")\n    )\n')


def _seg(x1, y1, x2, y2, net, layer="F.Cu", w=0.1, uid="s"):
    return (f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) '
            f'(layer "{layer}") (net "{net}") (uuid "{uid}"))\n')


def _pcb(tmp_path, body, name="b"):
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 60 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


def _bga_pads(pnet="/CK_P", nnet="/CK_N", n=5, pitch=0.8):
    """n x n ball field; balls A1 (P) and B1 (N) are the pair."""
    out = []
    for r in range(n):
        for c in range(n):
            num = f"{chr(65 + r)}{c + 1}"
            net = pnet if num == "A1" else nnet if num == "B1" else f"/S{r}{c}"
            out.append(_pad(num, c * pitch, r * pitch, net))
    return "".join(out)


def test_termination_across_the_pair_is_detached(tmp_path):
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "/CK_P", 0.4, "rect")
               + _pad("2", 0, 0.5, "/CK_N", 0.4, "rect"))
    body += _fp("U2", 40, 10, _pad("1", 0, 0, "/CK_P", 0.4, "rect")
                + _pad("2", 0, 0.5, "/CK_N", 0.4, "rect"))
    body += _fp("R5", 44, 10, _pad("1", -0.5, 0, "/CK_P", 0.4, "rect")
                + _pad("2", 0.5, 0, "/CK_N", 0.4, "rect"))
    # a pull-up on P alone is a stub pad, not a termination
    body += _fp("R6", 20, 20, _pad("1", -0.5, 0, "/CK_P", 0.4, "rect")
                + _pad("2", 0.5, 0, "+3V3", 0.4, "rect"))
    bg = geom.BoardGeom.from_file(_pcb(tmp_path, body))
    assert rc.termination_pads(bg, "/CK_P", "/CK_N") == [
        ("R5", "/CK_P"), ("R5", "/CK_N")]


def test_termination_kept_when_it_is_a_trunk_end(tmp_path):
    # only one other terminal: the R is where the pair ends
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "/CK_P", 0.4, "rect")
               + _pad("2", 0, 0.5, "/CK_N", 0.4, "rect"))
    body += _fp("R5", 44, 10, _pad("1", -0.5, 0, "/CK_P", 0.4, "rect")
                + _pad("2", 0.5, 0, "/CK_N", 0.4, "rect"))
    bg = geom.BoardGeom.from_file(_pcb(tmp_path, body))
    assert rc.termination_pads(bg, "/CK_P", "/CK_N") == []


def test_bare_bga_balls_named(tmp_path):
    body = _fp("U1", 10, 10, _bga_pads())
    body += _seg(10, 10, 9, 10, "/CK_P")       # A1 escaped on F.Cu
    bg = geom.BoardGeom.from_file(_pcb(tmp_path, body))
    assert rc.bare_bga_balls(bg, "/CK_P", "/CK_N") == ["U1.B1"]


# ------------------------------------------------------------ rung verdict

def test_rung_verdict_rejects_uncoupled_keeps_skew(tmp_path, monkeypatch):
    seen = {}

    def fake(bg, spec):
        seen.update(spec)
        return ([{"severity": "error", "kind": "diffpair_skew", "msg": "skew"},
                 {"severity": "warning", "kind": "diffpair_uncoupled", "msg": "w"},
                 {"severity": "error", "kind": "diffpair_uncoupled", "msg": "unc",
                  "layer": None}],
                {"skew_mm": 3.0, "uncoupled_p_mm": 9.0, "other": 1})

    monkeypatch.setattr(rc.check_diffpair, "check_pair", fake)
    monkeypatch.setattr(rc.geom.BoardGeom, "from_file", staticmethod(lambda p: None))
    bad, check = rc.rung_verdict(tmp_path / "x.kicad_pcb",
                                 {"p": "/A_P", "n": "/A_N", "max_skew_mm": 0.1},
                                 0.25712, 100)
    assert [b["kind"] for b in bad] == ["diffpair_uncoupled"]
    assert check == {"skew_mm": 3.0, "uncoupled_p_mm": 9.0}
    assert seen == {"p": "/A_P", "n": "/A_N", "gap_mm": 0.2571,
                    "impedance_ohm": 100, "max_skew_mm": 0.1}


def _ladder_ctx(tmp_path, monkeypatch, verdicts):
    """A route_diff_item run whose KRT always 'couples' the pair and whose
    rung verdicts come from `verdicts` in order."""
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "/A_P", 0.4, "rect")
               + _pad("2", 0, 0.5, "/A_N", 0.4, "rect"))
    body += _fp("U2", 40, 10, _pad("1", 0, 0, "/A_P", 0.4, "rect")
                + _pad("2", 0, 0.5, "/A_N", 0.4, "rect"))
    staged = _pcb(tmp_path, body, "staged")
    calls = []

    def fake_krt(krt, script, st, out, extra, *a, **kw):
        calls.append(extra[extra.index("--track-width") + 1])
        out.write_text(st.read_text(encoding="utf-8")
                       + f"\n;rung {len(calls)}\n", encoding="utf-8")
        return {"routed_diff_pairs": ["/A"], "pair_reports": [
            {"p_net": "/A_P", "n_net": "/A_N", "outcome": "coupled",
             "pair": "/A"}]}

    it = iter(verdicts)
    monkeypatch.setattr(rc, "run_krt", fake_krt)
    monkeypatch.setattr(rc, "rung_verdict", lambda *a, **k: next(it))
    ctx = {"staged": staged, "work": tmp_path, "krt": tmp_path,
           "floors": dict(rc.KICAD_DEFAULTS), "fab_file": tmp_path / "f",
           "grid_step": 0.05, "timeout_s": 10}
    return ctx, staged, calls


def test_rejected_rung_retries_then_keeps(tmp_path, monkeypatch):
    bad = [{"kind": "diffpair_uncoupled", "msg": "15 mm uncoupled"}]
    ctx, staged, calls = _ladder_ctx(tmp_path, monkeypatch,
                                     [(bad, {"uncoupled_p_mm": 15}),
                                      ([], {"uncoupled_p_mm": 1})])
    fact, v = rc.route_diff_item(ctx, {"p": "/A_P", "n": "/A_N",
                                       "impedance_ohm": 100})
    assert v is None and fact["ladder_rung"] == 2
    assert fact["rejected_rungs"][0]["rung"] == 1
    assert staged.read_text().endswith(";rung 2\n")


def test_every_rung_rejected_keeps_nothing(tmp_path, monkeypatch):
    bad = [{"kind": "diffpair_uncoupled", "msg": "15 mm uncoupled"}]
    ctx, staged, calls = _ladder_ctx(tmp_path, monkeypatch,
                                     [(bad, {}), (bad, {})])
    before = staged.read_text()
    fact, v = rc.route_diff_item(ctx, {"p": "/A_P", "n": "/A_N",
                                       "impedance_ohm": 100})
    assert fact is None and v["kind"] == "critical_pair_rejected"
    assert len(v["rejected_rungs"]) == 2 and "15 mm uncoupled" in v["msg"]
    assert staged.read_text() == before


def test_krt_timeout_fails_only_that_pair(tmp_path, monkeypatch):
    ctx, staged, calls = _ladder_ctx(tmp_path, monkeypatch, [])
    before = staged.read_text()

    def timed_out(*a, **k):
        raise rc.KrtTimeout("KRT route_diff.py timed out after 10s")

    monkeypatch.setattr(rc, "run_krt", timed_out)
    fact, v = rc.route_diff_item(ctx, {"p": "/A_P", "n": "/A_N",
                                       "impedance_ohm": 100})
    assert fact is None and v["kind"] == "critical_route_failed"
    assert v["reason"] == "krt-timeout"
    assert staged.read_text() == before


def test_run_krt_timeout_raises_krt_timeout(tmp_path, monkeypatch):
    def fake_run(args, **kw):
        raise rc.subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    with pytest.raises(rc.KrtTimeout):
        rc.run_krt(tmp_path, "route.py", tmp_path / "b.kicad_pcb",
                   tmp_path / "o.kicad_pcb", [], dict(rc.KICAD_DEFAULTS),
                   tmp_path / "f", 0.05, 1)


# ------------------------------------------------------------ check_diffpair

def _breakout_board(tmp_path):
    """Pair balls A1/B1 escape left out of a 5x5 field 0.8 apart (2 mm
    each), converge and run coupled 17 mm at 0.25 mm pitch to a 2-pad part."""
    body = _fp("U1", 30, 10, _bga_pads())
    body += _seg(30, 10, 28.0, 10, "/CK_P", uid="p1")
    body += _seg(30, 10.8, 28.0, 10.8, "/CK_N", uid="n1")
    body += _seg(28.0, 10, 27.6, 10.275, "/CK_P", uid="p2")
    body += _seg(28.0, 10.8, 27.6, 10.525, "/CK_N", uid="n2")
    body += _seg(27.6, 10.275, 10, 10.275, "/CK_P", uid="p3")
    body += _seg(27.6, 10.525, 10, 10.525, "/CK_N", uid="n3")
    body += _fp("J1", 10, 10.4, _pad("1", 0, -0.125, "/CK_P", 0.2, "rect")
                + _pad("2", 0, 0.125, "/CK_N", 0.2, "rect"))
    return _pcb(tmp_path, body)


def test_breakout_in_the_ball_field_is_not_uncoupled(tmp_path):
    bg = geom.BoardGeom.from_file(_breakout_board(tmp_path))
    region = check_diffpair.breakout_region(bg, "/CK_P", "/CK_N")
    assert region is not None
    unc_p, unc_n, stats = check_diffpair.coupling_stats(
        bg, "/CK_P", "/CK_N", 0.75, region)
    # an edge ball: one pitch past the ball bbox is breakout
    assert stats["breakout_p_mm"] == pytest.approx(0.8, abs=0.11)
    assert stats["breakout_n_mm"] == pytest.approx(0.8, abs=0.11)
    # without the region the same copper reads as uncoupled
    unc_p0, unc_n0, _ = check_diffpair.coupling_stats(bg, "/CK_P", "/CK_N", 0.75)
    assert unc_p0 > 1.0
    assert unc_p == pytest.approx(unc_p0 - stats["breakout_p_mm"], abs=0.11)
    assert unc_n == pytest.approx(unc_n0 - stats["breakout_n_mm"], abs=0.11)


def test_no_breakout_region_without_balls(tmp_path):
    body = _fp("U1", 10, 10, _pad("1", 0, 0, "/CK_P", 0.4, "rect")
               + _pad("2", 0, 0.5, "/CK_N", 0.4, "rect"))
    bg = geom.BoardGeom.from_file(_pcb(tmp_path, body))
    assert check_diffpair.breakout_region(bg, "/CK_P", "/CK_N") is None


# ------------------------------------------------------------ route_escape

def _bga(nets, ref="U1", bbox=(0, 0, 4, 4)):
    return bgalib.Bga(ref=ref, layer="F.Cu", pitch=0.8, rows=6, cols=6,
                      pad_d=0.35, bbox=bbox, balls=len(nets),
                      nets={str(i): n for i, n in enumerate(nets)})


def test_escape_pairs_both_balls_and_inner_gap():
    b = _bga(["/CK_P", "/CK_N", "/DQS_P", "/D0"])
    specs = [{"p": "/CK_P", "n": "/CK_N"}, {"p": "/DQS_P", "n": "/DQS_N"}]
    geo = {"/CK_P": {"In2.Cu": {"width_mm": 0.0889, "gap_mm": 0.1681},
                     "F.Cu": {"width_mm": 0.1, "gap_mm": 0.2}}}
    pairs, gap = re_.escape_pairs(b, specs, geo, ["F.Cu", "In2.Cu", "B.Cu"])
    assert pairs == [["/CK_P", "/CK_N"]] and gap == 0.1681
    assert re_.escape_pairs(b, specs, {}, ["F.Cu", "B.Cu"]) == (
        [["/CK_P", "/CK_N"]], None)


def test_fanout_args_carry_pairs():
    b = _bga(["/CK_P", "/CK_N"])
    a = re_.fanout_args(b, {"method": "dogbone", "track_width": 0.0889,
                            "clearance": 0.1, "via_size": 0.5,
                            "via_drill": 0.2}, ["F.Cu", "In2.Cu"],
                        Path("f.txt"), 0.05, pairs=[["/CK_P", "/CK_N"]],
                        pair_gap=0.1681)
    assert a[:5] == ["--diff-pairs", "/CK_P", "/CK_N", "--diff-pair-gap",
                     "0.1681"]
    assert "--diff-pairs" not in re_.fanout_args(
        b, {"method": "dogbone", "track_width": 0.0889, "clearance": 0.1,
            "via_size": 0.5, "via_drill": 0.2}, ["F.Cu"], Path("f"), 0.05)


def _drc(errors):
    return {"violations": [
        {"severity": "error", "check": "clearance",
         "items": [{"msg": f"Track [{net}] on F.Cu", "pos": list(pos)}]}
        for net, pos in errors]}


def test_error_fields_localise_the_rip():
    u1 = _bga(["/CK_P"], "U1", (0, 0, 4, 4))
    u2 = _bga(["/CK_P"], "U2", (30, 0, 34, 4))
    after = _drc([("/CK_P", (2, 6)), ("/X", (50, 50)), ("/X", (2, 2))])
    where = re_.error_fields({"violations": []}, after, {"/CK_P", "/X"},
                             [u1, u2])
    assert where == {"/CK_P": {"U1"}, "/X": None}


def test_rip_in_fields_keeps_the_other_field(tmp_path, monkeypatch):
    body = _fp("U1", 2, 2, "") + _fp("U2", 32, 2, "")
    body += _seg(2, 2, 2, 5, "/CK_P", uid="a")
    body += _seg(32, 2, 32, 5, "/CK_P", uid="b")
    pcb = _pcb(tmp_path, body)
    removed = []
    monkeypatch.setattr(re_.rc.route_edit, "apply_ops",
                        lambda p, ops: removed.extend(o["uuid"] for o in ops))
    u1 = _bga(["/CK_P"], "U1", (0, 0, 4, 4))
    u2 = _bga(["/CK_P"], "U2", (30, 0, 34, 4))
    n = re_.rip_in_fields(pcb, set(), {"/CK_P": {"U1"}}, [u1, u2])
    assert n == 1 and removed == ["a"]
    removed.clear()
    assert re_.rip_in_fields(pcb, set(), {"/CK_P": None}, [u1, u2]) == 2


# ------------------------------------------------------------ krt_launch #552/#547

def test_launcher_raises_hole_to_hole(tmp_path):
    (tmp_path / "routing_defaults.py").write_text(
        "HOLE_TO_HOLE_CLEARANCE = 0.20\n", encoding="utf-8")
    (tmp_path / "route_x.py").write_text(
        "from routing_defaults import HOLE_TO_HOLE_CLEARANCE as H\nprint(H)\n",
        encoding="utf-8")
    for arg, want in (("0.5", "0.5"), ("0.1", "0.2")):
        cp = subprocess.run([sys.executable,
                             str(SCRIPTS / "lib" / "krt_launch.py"),
                             "--hole-to-hole", arg, "route_x.py"],
                            cwd=tmp_path, capture_output=True, text=True,
                            check=True)
        assert cp.stdout.strip() == want


def test_escape_open_edits_match_pinned_krt():
    import env
    krt = env.find_krt()
    if krt is None:
        pytest.skip("needs KiCadRoutingTools")
    src = (Path(krt) / "bga_fanout" / "underpad.py").read_text(encoding="utf-8")
    out = krt_launch.escape_open_source(src)
    assert out.count("_hw_inb(") == 3 and out.count("_hw_shut(") == 3
    compile(out, "underpad.py", "exec")
    with pytest.raises(SystemExit):
        krt_launch.escape_open_source("def heur(): pass\n")
    # a new escape path (one more A* call) fails loudly, not unsteered
    with pytest.raises(SystemExit, match="escape call sites"):
        krt_launch.escape_open_source(src + "\n_ = astar(0)\n")


@pytest.mark.parametrize("pre_import", [False, True])
def test_launcher_hole_to_hole_reaches_bound_defaults(tmp_path, pre_import):
    """0.24's check_drc binds hole_to_hole_clearance=HOLE_TO_HOLE_CLEARANCE
    as a default at definition: the patch must reach it even when the module
    was imported before the patch ran (review of the 0.24 launcher)."""
    (tmp_path / "routing_defaults.py").write_text(
        "HOLE_TO_HOLE_CLEARANCE = 0.20\n", encoding="utf-8")
    (tmp_path / "drc_x.py").write_text(
        "import routing_defaults as defaults\n"
        "from routing_defaults import HOLE_TO_HOLE_CLEARANCE\n"
        "def run_drc(pcb, hole_to_hole_clearance: float = "
        "defaults.HOLE_TO_HOLE_CLEARANCE, *, other=0.20):\n"
        "    return hole_to_hole_clearance, other\n"
        "class Checker:\n"
        "    def check(self, *, hole_to_hole_clearance=HOLE_TO_HOLE_CLEARANCE):\n"
        "        return hole_to_hole_clearance\n", encoding="utf-8")
    (tmp_path / "route_x.py").write_text(
        "import drc_x\n"
        "print(drc_x.run_drc(None), drc_x.Checker().check(),"
        " drc_x.HOLE_TO_HOLE_CLEARANCE)\n", encoding="utf-8")
    code = ("import sys; sys.path.insert(0, '.'); "
            + ("import drc_x; " if pre_import else "")
            + "import krt_launch; "
            "krt_launch.main(['--hole-to-hole', '0.3', 'route_x.py'])")
    cp = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                        env={**os.environ, "PYTHONPATH": str(SCRIPTS / "lib")},
                        capture_output=True, text=True, check=True)
    # other=0.20 is not the hole-to-hole arg and stays
    assert cp.stdout.strip() == "(0.3, 0.2) 0.3 0.3"


def test_escape_open_helpers():
    import textwrap
    from types import SimpleNamespace as S
    ns = {"fp_pads": [S(net_name="/A", net_id=1), S(net_name="/B", net_id=2)],
          "HWDE_OPEN": {"/A": ["left", "up"]},
          "bx0": 0, "by0": 0, "bx1": 10, "by1": 10,
          "in_bbox": lambda x, y: 0 <= x <= 10 and 0 <= y <= 10}
    exec(textwrap.dedent(krt_launch._HW_HELPERS), ns)
    inb, shut, dists = ns["_hw_inb"], ns["_hw_shut"], ns["_hw_dists"]
    # /A: past the left/top edge is the goal, past right/bottom is not
    assert not inb(-1, 5, 1) and not inb(5, -1, 1)
    assert inb(11, 5, 1) and inb(5, 11, 1)
    # an unsteered net keeps KRT's bbox
    assert not inb(11, 5, 2) and inb(5, 5, 2)
    p1, p2 = S(net_id=1), S(net_id=2)
    assert shut(p1, (1.0, 0.0)) and shut(p1, (0.0, 1.0))
    assert not shut(p1, (-1.0, 0.0)) and not shut(p1, (0.0, -1.0))
    assert not shut(p2, (1.0, 0.0))
    d = dists(p1, 1.0, 2.0, 3.0, 4.0)
    assert d[1] == 2.0 and d[3] == 4.0 and d[0] > 1e5 and d[2] > 1e5
    assert dists(p2, 1.0, 2.0, 3.0, 4.0) == (1.0, 2.0, 3.0, 4.0)
