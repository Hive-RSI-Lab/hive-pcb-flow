"""S5 acceptance tests: verification suite part 2 + check orchestration.

Plan S5 accept criteria, arbitrated by tests/golden/manifest.yaml:
  - full mutant manifest coverage: every mutant caught by its designated check
        -> test_full_manifest_coverage (via verify_all), test_mutant_*
  - goldens clean under every S5 check and the merged runner
        -> test_golden_clean_*, test_verify_all_goldens_clean
  - verify_all.py summary schema stable and documented
        -> test_verify_all_schema

Pure tests exercise the algorithms on synthetic boards without any toolchain;
corpus tests are also toolchain-free (committed boards + pure-venv geometry).
Only the timing test carries the `smoke` marker.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from shapely.geometry import box

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GOLDEN = REPO / "tests" / "golden"
PYTHON = sys.executable
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_creepage  # noqa: E402
from _perf import assert_under  # noqa: E402
import check_decoupling  # noqa: E402
import check_diffpair  # noqa: E402
import check_pdn  # noqa: E402
import check_ratings  # noqa: E402
import check_route_style  # noqa: E402
import check_silk  # noqa: E402
import check_thermal  # noqa: E402
import checklib  # noqa: E402
import cluster_violations  # noqa: E402
import geom  # noqa: E402
import verify_all  # noqa: E402

MANIFEST = yaml.safe_load((GOLDEN / "manifest.yaml").read_text(encoding="utf-8"))
BOARDS = list(MANIFEST["golden_boards"])
S5_CHECKS = ["check_diffpair", "check_silk", "check_creepage",
             "check_thermal", "check_pdn"]
# checks that exist by end of S5 (dfm_check is S12)
BUILT_CHECKS = {"check_return_path", "check_current", "check_decoupling",
                "check_diffpair", "check_silk", "check_creepage",
                "check_thermal", "check_route_style", "check_pdn",
                "check_ratings", "check_mating"}


def board_path(name: str) -> Path:
    return GOLDEN / name / f"{name}.kicad_pcb"


def mutant_path(mutant: str) -> Path:
    board = MANIFEST["mutants"][mutant]["board"]
    return GOLDEN / "mutants" / mutant / f"{board}.kicad_pcb"


def mutant_input(mutant: str, name: str) -> Path:
    """The mutant's own sidecar (constraints.json, decoupling.json, parts/)
    when its dir holds one, else the golden's."""
    own = GOLDEN / "mutants" / mutant / name
    if own.exists():
        return own
    return GOLDEN / golden_of(mutant) / name


def golden_of(mutant: str) -> str:
    return MANIFEST["mutants"][mutant]["board"]


# ---- synthetic board helpers (mirror test_checks.py) ----------------------

def _board(tmp_path_factory, name: str, body: str) -> geom.BoardGeom:
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start -2 -2) (end 24 12) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path_factory.mktemp(name) / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return geom.load_board(p)


# ============================================================ pure: creepage

def test_ipc2221_clearance_table():
    c = check_creepage.clearance_mm
    # external (outer): band boundaries
    assert c(15, True) == 0.10
    assert c(30, True) == 0.10
    assert c(31, True) == 0.60
    assert c(100, True) == 0.60
    assert c(150, True) == 0.60
    assert c(170, True) == 1.25
    assert c(300, True) == 1.25
    assert c(500, True) == 2.50
    # internal
    assert c(50, False) == 0.10
    assert c(150, False) == 0.20
    assert c(500, False) == 0.25
    # > 500 V linear
    assert c(1000, True) == pytest.approx(2.50 + 0.005 * 500)   # 5.0
    assert c(1000, False) == pytest.approx(0.25 + 0.0025 * 500)  # 1.5


def test_creepage_hv_pair_flagged(tmp_path_factory):
    body = ('  (segment (start 2 2) (end 18 2) (width 0.5) (layer "F.Cu") '
            '(net "HV"))\n'
            '  (segment (start 2 2.8) (end 18 2.8) (width 0.5) (layer "F.Cu") '
            '(net "GND"))\n')
    bg = _board(tmp_path_factory, "hv", body)
    vmap = {"HV": 100.0, "GND": 0.0}
    vs, facts = check_creepage.check_net(bg, "HV", 100.0, vmap)
    assert len(vs) == 1
    v = vs[0]
    assert v["net"] == "HV" and v["other_net"] == "GND"
    assert v["required_mm"] == 0.60 and v["spacing_mm"] == pytest.approx(0.30)


def test_creepage_below_30v_ignored(tmp_path_factory):
    body = ('  (segment (start 2 2) (end 18 2) (width 0.5) (layer "F.Cu") '
            '(net "A"))\n'
            '  (segment (start 2 2.8) (end 18 2.8) (width 0.5) (layer "F.Cu") '
            '(net "B"))\n')
    bg = _board(tmp_path_factory, "lv", body)
    vs, _ = check_creepage.check_net(bg, "A", 20.0, {"A": 20.0, "B": 0.0})
    assert vs == []          # 20 V difference is below the 30 V threshold


# ============================================================ pure: thermal

def test_theta_ja_monotonic_and_saturates():
    t = check_thermal.theta_ja
    assert t(0, False) == pytest.approx(174.0)             # bare footprint
    # area clamps at ~1 in^2 (A_SAT), so theta bottoms at theta(A_SAT) ~ 74 C/W
    # for 1 oz - which matches the LM3940 datasheet (74 C/W at 1 in^2)
    assert t(1e9, False) == pytest.approx(t(check_thermal.A_SAT_MM2, False))
    assert t(1e9, False) == pytest.approx(74.0, abs=1.0)
    assert 55.0 < t(1e9, False) < 174.0
    vals = [t(a, False) for a in range(0, 700, 20)]
    assert all(b <= a for a, b in zip(vals, vals[1:]))     # decreasing
    assert t(300, True) < t(300, False)                    # planes spread better


def test_thermal_area_and_via_flags(tmp_path_factory):
    # one dissipating part; GND copper is only a small pad island
    body = ('  (footprint "t:U" (at 10 6) (layer "F.Cu")\n'
            '    (property "Reference" "U9" (at 0 0 0) (layer "F.SilkS"))\n'
            '    (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu") '
            '(net "GND")))\n')
    bg = _board(tmp_path_factory, "hot", body)
    vs, facts = check_thermal.check_part(
        bg, {"ref": "U9", "net": "GND", "power_w": 1.5, "dt_c": 40})
    assert any(v["kind"] == "thermal_area" for v in vs)
    assert facts["rise_c"] > 40


def test_thermal_low_power_passes(tmp_path_factory):
    body = ('  (footprint "t:U" (at 10 6) (layer "F.Cu")\n'
            '    (property "Reference" "U9" (at 0 0 0) (layer "F.SilkS"))\n'
            '    (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu") '
            '(net "GND")))\n'
            '  (zone (net "GND") (layer "F.Cu")\n'
            '    (polygon (pts (xy 0 0) (xy 22 0) (xy 22 10) (xy 0 10)))\n'
            '    (filled_polygon (layer "F.Cu")\n'
            '      (pts (xy 0 0) (xy 22 0) (xy 22 10) (xy 0 10))))\n')
    bg = _board(tmp_path_factory, "cool", body)
    vs, facts = check_thermal.check_part(
        bg, {"ref": "U9", "net": "GND", "power_w": 0.2, "dt_c": 40})
    assert [v for v in vs if v["kind"] == "thermal_area"] == []


def _thermal_2l_board(tmp_path_factory, name, *, vias="", fp_vias="",
                      pours=("F.Cu", "B.Cu"), half=15.0):
    """40 x 40 mm 2-layer board: a 3 x 3 mm exposed pad U9 at (20, 20), GND
    pours 2*half square (30 x 30 mm) on `pours`, plus optional board vias /
    footprint PTH."""
    lo, hi = 20 - half, 20 + half
    sq = f"(xy {lo} {lo}) (xy {hi} {lo}) (xy {hi} {hi}) (xy {lo} {hi})"
    zones = "".join(
        f'  (zone (net "GND") (layer "{lyr}")\n'
        f'    (polygon (pts {sq}))\n'
        f'    (filled_polygon (layer "{lyr}")\n'
        f'      (pts {sq})))\n'
        for lyr in pours)
    text = f"""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)) (setup)
  (gr_rect (start 0 0) (end 40 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
  (footprint "t:U" (at 20 20) (layer "F.Cu")
    (property "Reference" "U9" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 3 3) (layers "F.Cu") (net "GND"))
{fp_vias})
{vias}{zones})
"""
    p = tmp_path_factory.mktemp(name) / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return geom.load_board(p)


def _via_grid(fmt: str, base: float = 0.0) -> str:
    """4 x 4 grid at 0.9 mm pitch centred on (base, base), one `fmt` each."""
    offs = (-1.35, -0.45, 0.45, 1.35)
    return "".join(fmt.format(x=base + dx, y=base + dy)
                   for dx in offs for dy in offs)


AMP_2L = {"ref": "U9", "net": "GND", "power_w": 1.5, "dt_c": 85}


def test_via_barrel_resistance():
    # 0.3 mm drill, 18 um plating, 1.6 mm FR4: ~230 C/W per unfilled via
    assert check_thermal.via_r_cw(0.3) == pytest.approx(232, rel=0.02)
    assert check_thermal.via_r_cw(0.0) == math.inf
    assert check_thermal.via_array_r_cw([]) == math.inf
    assert check_thermal.via_array_r_cw([0.3] * 16) == pytest.approx(
        check_thermal.via_r_cw(0.3) / 16)
    cap = check_thermal.VIA_BENEFIT_CAP
    assert check_thermal.via_array_r_cw([0.3] * (cap + 50)) == pytest.approx(
        check_thermal.via_array_r_cw([0.3] * cap))
    # no vias -> the network is the top pour alone
    assert check_thermal.theta_2l_network(645, 645, math.inf) == \
        pytest.approx(check_thermal.theta_ja(645, False))


def test_thermal_2l_via_array_and_pour_meets_budget(tmp_path_factory):
    """A PowerPAD amp at 1.5 W with 16 vias into a back pour fits 85 C."""
    vias = _via_grid('  (via (at {x:.2f} {y:.2f}) (size 0.6) (drill 0.3) '
                     '(layers "F.Cu" "B.Cu") (net "GND"))\n', base=20.0)
    bg = _thermal_2l_board(tmp_path_factory, "amp2l", vias=vias)
    vs, facts = check_thermal.check_part(bg, AMP_2L)
    assert facts["vias_near_part"] == 16
    assert facts["theta_area_cw"] == pytest.approx(73.8, abs=0.5)  # old floor
    assert facts["theta_ja"] < 85 / 1.5
    assert vs == [], json.dumps(vs)


def test_thermal_2l_footprint_via_pads_count(tmp_path_factory):
    """Thermal vias drawn as PTH pads in the footprint count like vias."""
    fp = _via_grid('    (pad "1" thru_hole circle (at {x} {y}) (size 0.6 0.6) '
                   '(drill 0.3) (layers "*.Cu") (net "GND"))\n')
    bg = _thermal_2l_board(tmp_path_factory, "amp2lfp", fp_vias=fp)
    vs, facts = check_thermal.check_part(bg, AMP_2L)
    assert facts["vias_near_part"] == 16
    assert facts["theta_ja"] < 85 / 1.5
    assert vs == [], json.dumps(vs)


@pytest.mark.parametrize("pours", [(), ("F.Cu", "B.Cu")])
def test_thermal_2l_no_vias_still_fails(tmp_path_factory, pours):
    """Without vias the back pour is unreachable: bare pad or pours, it fails."""
    bg = _thermal_2l_board(tmp_path_factory, "amp2lbare", pours=pours)
    vs, facts = check_thermal.check_part(bg, AMP_2L)
    assert facts["vias_near_part"] == 0
    assert facts["theta_ja"] == pytest.approx(facts["theta_area_cw"])
    assert any(v["kind"] == "thermal_area" for v in vs)
    assert any(v["kind"] == "thermal_vias" for v in vs)


THERMAL_VIA = ('  (via (at {x:.2f} {y:.2f}) (size 0.6) (drill 0.3) '
               '(layers "F.Cu" "B.Cu") (net "GND"))\n')


def test_thermal_2l_vias_without_back_pour_get_no_credit(tmp_path_factory):
    """Vias that end on their own lands (no back pour) carry heat nowhere."""
    vias = _via_grid(THERMAL_VIA, base=20.0)
    bg = _thermal_2l_board(tmp_path_factory, "amp2lnoback", vias=vias,
                           pours=("F.Cu",))
    vs, facts = check_thermal.check_part(bg, AMP_2L)
    assert facts["vias_near_part"] == 16
    assert facts["back_pour_reached"] is False
    assert facts["theta_ja"] == pytest.approx(facts["theta_area_cw"])
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "add thermal vias into a back-side GND pour" in err[0]["msg"]


def test_thermal_2l_too_few_vias_says_add_vias(tmp_path_factory):
    """One via into the back pour helps a little; the fix is more vias."""
    vias = THERMAL_VIA.format(x=20.0, y=20.0)
    bg = _thermal_2l_board(tmp_path_factory, "amp2lonevia", vias=vias)
    vs, facts = check_thermal.check_part(bg, AMP_2L)
    assert facts["back_pour_reached"] is True
    assert facts["theta_ja"] < facts["theta_area_cw"]       # credited
    assert facts["theta_best_2l_cw"] * 1.5 < 85             # fixable on 2L
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "add thermal vias into a back-side GND pour" in err[0]["msg"]
    assert "1 thermal via(s)" in err[0]["msg"]


def test_thermal_2l_over_power_fails_despite_vias(tmp_path_factory):
    """3 W into 16 vias + full pours: credited, still over, and past the best
    2-layer case, so the remedy is not 'more vias'."""
    vias = _via_grid(THERMAL_VIA, base=20.0)
    bg = _thermal_2l_board(tmp_path_factory, "amp2lhot", vias=vias)
    vs, facts = check_thermal.check_part(bg, dict(AMP_2L, power_w=3.0))
    assert facts["theta_ja"] < facts["theta_area_cw"]       # vias credited
    assert facts["theta_best_2l_cw"] == pytest.approx(38.5, abs=1.0)
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "even the best 2-layer case" in err[0]["msg"]
    assert "move to 4 layers" in err[0]["msg"]


def test_thermal_2l_full_via_array_small_pours_says_grow(tmp_path_factory):
    """A full via array into small pours: the fix is copper, not vias."""
    offs = [i * 0.65 - 1.625 for i in range(6)]   # 6 x 6 at 0.65 mm
    vias = "".join(THERMAL_VIA.format(x=20 + dx, y=20 + dy)
                   for dx in offs for dy in offs)
    bg = _thermal_2l_board(tmp_path_factory, "amp2lsmall", vias=vias,
                           half=4.0)            # 8 x 8 mm both sides
    vs, facts = check_thermal.check_part(bg, dict(AMP_2L, power_w=1.2))
    assert facts["vias_near_part"] == check_thermal.VIA_BENEFIT_CAP
    assert facts["back_pour_reached"] is True
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "grow the top and back GND pours" in err[0]["msg"]


# ------------------------------------------- thermal: forced air and ratings

HOT_PAD = ('  (footprint "t:U" (at 10 6) (layer "F.Cu")\n'
           '    (property "Reference" "U9" (at 0 0 0) (layer "F.SilkS"))\n'
           '    (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu") '
           '(net "GND")))\n')
XAL1010 = {"rise_c": 40, "current_a": 24.5, "dcr_ohm": 0.0028,
           "source": "Coilcraft XAL1010-222 Irms (40 C rise), DCR"}


def test_airflow_factor_is_the_least_scba017d_improvement():
    f = check_thermal.airflow_factor
    assert f(0) == 1.0
    # worst row of SCBA017D Tables 3-5 (14-pin QFN, JESD51-7)
    assert f(150) == pytest.approx(46.0 / 52.5)
    assert f(250) == pytest.approx(42.9 / 52.5)
    assert f(500) == pytest.approx(40.5 / 52.5)
    assert f(2000) == f(500)                       # no extrapolation
    assert f(250) < f(200) < f(150)                # linear between anchors
    vals = [f(x) for x in range(0, 800, 25)]
    assert all(b <= a for a, b in zip(vals, vals[1:]))
    with pytest.raises(checklib.CheckError):
        f(-1)


def test_entry_airflow_units_and_conflicts():
    e = check_thermal.entry_airflow_lfm
    assert e({"ref": "U9"}) == 0.0
    assert e({"ref": "U9", "airflow_lfm": 300}) == 300.0
    assert e({"ref": "U9", "airflow_m_s": 1.0}) == pytest.approx(196.85)
    with pytest.raises(checklib.CheckError, match="not both"):
        e({"ref": "U9", "airflow_lfm": 300, "airflow_m_s": 1.0})
    with pytest.raises(checklib.CheckError, match="number"):
        e({"ref": "U9", "airflow_m_s": "fast"})


def test_thermal_airflow_derates_and_still_air_is_unchanged(tmp_path_factory):
    bg = _board(tmp_path_factory, "hotair", HOT_PAD)
    entry = {"ref": "U9", "net": "GND", "power_w": 1.5, "dt_c": 40}
    vs0, still = check_thermal.check_part(bg, entry)
    assert "airflow_lfm" not in still and "theta_still_air_cw" not in still
    vs, facts = check_thermal.check_part(bg, dict(entry, airflow_lfm=500))
    assert facts["theta_still_air_cw"] == pytest.approx(still["theta_ja"])
    assert facts["theta_ja"] == pytest.approx(
        still["theta_ja"] * check_thermal.airflow_factor(500), rel=1e-3)
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "at 500 LFM" in err[0]["msg"]      # still over 40 C


def test_thermal_2l_best_case_scales_with_airflow(tmp_path_factory):
    """2.8 W into 16 vias: past the still-air best 2L case, within it at
    500 LFM, so the remedy turns from 'move to 4 layers' to 'more vias'."""
    vias = _via_grid(THERMAL_VIA, base=20.0)
    bg = _thermal_2l_board(tmp_path_factory, "amp2lair", vias=vias)
    vs, _ = check_thermal.check_part(bg, dict(AMP_2L, power_w=2.8))
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "even the best 2-layer case" in err[0]["msg"]
    vs, _ = check_thermal.check_part(bg, dict(AMP_2L, power_w=2.8,
                                              airflow_lfm=500))
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "add thermal vias into a back-side GND pour" in \
        err[0]["msg"]


def test_thermal_rating_replaces_the_copper_model(tmp_path_factory):
    """PCB-0026 L201: XAL1010-222 at 1.1 W is ~26 C by its own rating, where
    the area curve said ~58 C/W; no via array is asked of a rated part."""
    bg = _board(tmp_path_factory, "ind", HOT_PAD)
    entry = {"ref": "U9", "net": "GND", "power_w": 1.1, "dt_c": 40,
             "rating": XAL1010}
    vs, facts = check_thermal.check_part(bg, entry)
    assert facts["theta_rated_cw"] == pytest.approx(23.8, abs=0.05)
    assert facts["rise_c"] == pytest.approx(26.2, abs=0.1)
    assert facts["theta_model_cw"] > 40 / 1.1          # the model would fail
    assert vs == [], json.dumps(vs)
    vs, facts = check_thermal.check_part(bg, dict(entry, airflow_m_s=1.0))
    assert facts["theta_ja"] < facts["theta_rated_cw"]


def test_thermal_rating_over_budget_says_so(tmp_path_factory):
    bg = _board(tmp_path_factory, "indhot", HOT_PAD)
    vs, facts = check_thermal.check_part(
        bg, {"ref": "U9", "net": "GND", "power_w": 2.0, "dt_c": 40,
             "rating": XAL1010})
    assert facts["rise_c"] == pytest.approx(47.6, abs=0.1)
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "by its own rating (40 C at 24.5 A)" in err[0]["msg"]
    assert "pick a part rated for more current" in err[0]["msg"]
    assert [v for v in vs if v["kind"] == "thermal_vias"] == []


@pytest.mark.parametrize("rating,match", [
    ({k: v for k, v in XAL1010.items() if k != "source"}, "source"),
    (dict(XAL1010, dcr_ohm=0), "> 0"),
    (dict(XAL1010, current_a="lots"), "numeric"),
    ({"source": "x", "rise_c": 40}, "numeric"),
    ("40 C at 24.5 A", "source"),
])
def test_thermal_rating_must_be_complete(tmp_path_factory, rating, match):
    bg = _board(tmp_path_factory, "indbad", HOT_PAD)
    with pytest.raises(checklib.CheckError, match=match):
        check_thermal.check_part(bg, {"ref": "U9", "net": "GND",
                                      "power_w": 1.0, "rating": rating})


LMG2100 = {"theta_ja_c_w": 29,
           "theta_source": "LMG2100R044 SNOSDF9B 5.4 Thermal Information"}


def test_thermal_datasheet_theta_replaces_model_and_takes_airflow(
        tmp_path_factory):
    """PCB-0026 U201: 3.0 W on the datasheet's 29 C/W is 87 C, over 70 in
    still air; airflow scales the datasheet figure, nothing else does."""
    bg = _board(tmp_path_factory, "dsth", HOT_PAD)
    entry = {"ref": "U9", "net": "GND", "power_w": 3.0, "dt_c": 70,
             "min_vias": 0, **LMG2100}
    vs, facts = check_thermal.check_part(bg, entry)
    assert facts["theta_ja"] == 29.0 and facts["theta_datasheet_cw"] == 29.0
    assert facts["theta_model_cw"] > 29.0
    assert facts["rise_c"] == pytest.approx(87.0)
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "datasheet theta_JA (29 C/W, JEDEC board)" in err[0]["msg"]
    assert "add a heatsink, or add forced air" in err[0]["msg"]
    vs, facts = check_thermal.check_part(bg, dict(entry, airflow_m_s=2.0))
    assert facts["theta_still_air_cw"] == 29.0
    assert facts["rise_c"] == pytest.approx(
        87.0 * check_thermal.airflow_factor(2.0 * 196.85))
    assert [v for v in vs if v["kind"] == "thermal_area"] == []


@pytest.mark.parametrize("extra,match", [
    ({"theta_ja_c_w": 29}, "theta_source"),
    ({**LMG2100, "theta_ja_c_w": "low"}, "number"),
    ({**LMG2100, "theta_ja_c_w": 0}, "> 0"),
    ({**LMG2100, "rating": XAL1010}, "not both"),
])
def test_thermal_datasheet_theta_must_be_sourced(tmp_path_factory, extra,
                                                 match):
    bg = _board(tmp_path_factory, "dsbad", HOT_PAD)
    with pytest.raises(checklib.CheckError, match=match):
        check_thermal.check_part(bg, {"ref": "U9", "net": "GND",
                                      "power_w": 1.0, **extra})


# ============================================================ pure: pdn

def test_pdn_no_bulk_and_undecoupled():
    bg = None  # check_rail only reads bg.nets for on_board; use a stub
    class _BG:
        nets = {"+3V3", "VDDA"}
    assocs = [{"cap": "C1", "rail": "+3V3", "value": "100nF"}]
    vs, facts = check_pdn.check_rail(_BG(), "+3V3", 0.4, assocs)
    assert vs[0]["kind"] == "pdn_no_bulk" and facts["bulk_count"] == 0
    vs2, facts2 = check_pdn.check_rail(_BG(), "VDDA", 0.1, assocs)
    assert vs2[0]["kind"] == "pdn_undecoupled" and facts2["cap_count"] == 0


def test_pdn_bulk_plus_ceramic_clean():
    class _BG:
        nets = {"+3V3"}
    assocs = [{"cap": "C1", "rail": "+3V3", "value": "100nF"},
              {"cap": "C2", "rail": "+3V3", "value": "10uF"}]
    vs, facts = check_pdn.check_rail(_BG(), "+3V3", 0.4, assocs)
    assert vs == []
    assert facts["bulk_count"] == 1 and facts["ceramic_count"] == 1


def test_pdn_multitoken_values_count_as_bulk():
    """S14: real-world value strings ('10uF 25V X5R', '22uF 16V tantalum')
    must parse - pdn_no_bulk false-positived on every such bulk cap."""
    class _BG:
        nets = {"+3V3"}
    assocs = [{"cap": "C1", "rail": "+3V3", "value": "100nF 50V X7R"},
              {"cap": "C5", "rail": "+3V3", "value": "22uF 16V tantalum"},
              {"cap": "C11", "rail": "+3V3", "value": "10uF 25V X5R"}]
    vs, facts = check_pdn.check_rail(_BG(), "+3V3", 0.3, assocs)
    assert vs == []
    assert facts["bulk_count"] == 2 and facts["ceramic_count"] == 1


def test_parse_farads_multitoken():
    p = check_decoupling.parse_farads
    assert p("10uF 25V X5R") == pytest.approx(10e-6)
    assert p("22uF 16V tantalum") == pytest.approx(22e-6)
    assert p("100nF 50V X7R") == pytest.approx(100e-9)
    assert p("22pF 50V NP0") == pytest.approx(22e-12)
    # must NOT match voltage/dielectric tokens when no farad token exists
    assert p("25V X7R") is None
    assert p("tantalum") is None
    # plain forms keep their old meaning
    assert p("10uF") == pytest.approx(10e-6)
    assert p(1e-6) == pytest.approx(1e-6)


def test_pdn_width_only_entry_skipped(tmp_path):
    """S14: power entries with "pdn": false (declared only for rules_gen trace
    width - e.g. the raw-input stub before a reverse-polarity diode) are
    skipped by the inventory, not flagged pdn_undecoupled."""
    class _BG:
        nets = {"/VIN"}
    # direct run() path: build minimal files
    import json as _json
    cons = {"power": [{"net": "/VIN", "current_a": 0.3, "pdn": False}]}
    dec = {"associations": []}
    (tmp_path / "c.json").write_text(_json.dumps(cons), encoding="utf-8")
    (tmp_path / "d.json").write_text(_json.dumps(dec), encoding="utf-8")
    pcb = GOLDEN / "blinky2" / "blinky2.kicad_pcb"
    payload, _ = check_pdn.run([
        "--pcb", str(pcb), "--constraints", str(tmp_path / "c.json"),
        "--decoupling", str(tmp_path / "d.json")])
    assert payload["status"] == "pass"
    assert payload["violations"] == []
    assert payload["checked"][0]["skipped"].startswith("pdn:false")


# ============================================================ pure: diffpair

def test_discover_pairs():
    d = check_diffpair.discover_pairs
    assert d({"/USB_DP", "/USB_DM"}) == [("/USB_DP", "/USB_DM")]
    assert d({"/DIFF_P", "/DIFF_N"}) == [("/DIFF_P", "/DIFF_N")]
    assert d({"/CLK+", "/CLK-"}) == [("/CLK+", "/CLK-")]
    assert d({"+3V3", "GND", "/SCK", "/MISO", "/MOSI"}) == []
    # both ordered positive-first
    assert d({"/HS_N", "/HS_P"}) == [("/HS_P", "/HS_N")]


def _diffpair_body(n_track: str) -> str:
    return (
        '  (footprint "t:J" (at 0 5) (layer "F.Cu")\n'
        '    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at 0 -0.15) (size 0.4 0.4) (layers "F.Cu") (net "/D_P"))\n'
        '    (pad "2" smd rect (at 0 0.15) (size 0.4 0.4) (layers "F.Cu") (net "/D_N")))\n'
        '  (footprint "t:U" (at 18 5) (layer "F.Cu")\n'
        '    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at 0 -0.15) (size 0.4 0.4) (layers "F.Cu") (net "/D_P"))\n'
        '    (pad "2" smd rect (at 0 0.15) (size 0.4 0.4) (layers "F.Cu") (net "/D_N")))\n'
        '  (segment (start 0 4.85) (end 18 4.85) (width 0.25) (layer "F.Cu") (net "/D_P"))\n'
        + n_track)


PAIR_OK = _diffpair_body(
    '  (segment (start 0 5.15) (end 18 5.15) (width 0.25) (layer "F.Cu") (net "/D_N"))\n')
PAIR_MEANDER = _diffpair_body(
    '  (segment (start 0 5.15) (end 6 5.15) (width 0.25) (layer "F.Cu") (net "/D_N"))\n'
    '  (segment (start 6 5.15) (end 6 9) (width 0.25) (layer "F.Cu") (net "/D_N"))\n'
    '  (segment (start 6 9) (end 12 9) (width 0.25) (layer "F.Cu") (net "/D_N"))\n'
    '  (segment (start 12 9) (end 12 5.15) (width 0.25) (layer "F.Cu") (net "/D_N"))\n'
    '  (segment (start 12 5.15) (end 18 5.15) (width 0.25) (layer "F.Cu") (net "/D_N"))\n')


def test_diffpair_clean_pair(tmp_path_factory):
    bg = _board(tmp_path_factory, "pairok", PAIR_OK)
    vs, facts = check_diffpair.check_pair(bg, {"p": "/D_P", "n": "/D_N"})
    assert vs == []
    assert facts["skew_mm"] == pytest.approx(0.0, abs=1e-6)
    assert facts["branch_free"] is True
    assert facts["uncoupled_p_mm"] == pytest.approx(0.0, abs=0.2)


def test_diffpair_meander_uncoupled(tmp_path_factory):
    bg = _board(tmp_path_factory, "pairmeander", PAIR_MEANDER)
    vs, facts = check_diffpair.check_pair(bg, {"p": "/D_P", "n": "/D_N"})
    unc = [v for v in vs if v["kind"] == "diffpair_uncoupled"]
    assert len(unc) == 1 and unc[0]["severity"] == "error"
    assert facts["uncoupled_n_mm"] > 5.0


def test_diffpair_branch_free_excludes_stub(tmp_path_factory):
    """A series/pull stub on one net must not inflate its trunk length."""
    stub = ('  (segment (start 0 5.15) (end 18 5.15) (width 0.25) (layer "F.Cu") (net "/D_N"))\n'
            '  (segment (start 9 4.85) (end 9 1) (width 0.25) (layer "F.Cu") (net "/D_P"))\n')
    bg = _board(tmp_path_factory, "pairstub", _diffpair_body(stub))
    _, facts = check_diffpair.check_pair(bg, {"p": "/D_P", "n": "/D_N"})
    # D_P trunk is the 18 mm run, NOT 18 + 3.85 mm stub
    assert facts["length_p_mm"] == pytest.approx(18.0, abs=0.1)
    assert facts["branch_free"] is True


def test_diffpair_via_asymmetry(tmp_path_factory):
    body = PAIR_OK + \
        '  (via (at 9 4.85) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "/D_P"))\n'
    bg = _board(tmp_path_factory, "pairvia", body)
    vs, _ = check_diffpair.check_pair(bg, {"p": "/D_P", "n": "/D_N"})
    assert any(v["kind"] == "diffpair_via_asymmetry" for v in vs)


# ============================================================ pure: silk

def test_silk_over_pad_center_rule(tmp_path_factory):
    body = ('  (footprint "t:D" (at 10 6) (layer "F.Cu")\n'
            '    (property "Reference" "D1" (at 0 -2 0) (layer "F.SilkS"))\n'
            '    (pad "1" smd roundrect (at 0 0) (size 1 1.4) '
            '(roundrect_rratio 0.25) (layers "F.Cu") (net "K")))\n'
            '  (gr_text "XX" (at 10 6 0) (layer "F.SilkS") '
            '(effects (font (size 1 1) (thickness 0.15))))\n')
    bg = _board(tmp_path_factory, "silkhit", body)
    root = __import__("sexpdata").loads(bg.path.read_text(encoding="utf-8"))
    vs = check_silk.run_checks(bg, check_silk.parse_silk(root))
    assert any(v["kind"] == "silk_over_pad" and "D1" in v["refs"] for v in vs)


def test_silk_beside_pad_clean(tmp_path_factory):
    body = ('  (footprint "t:D" (at 10 6) (layer "F.Cu")\n'
            '    (property "Reference" "D1" (at 0 -2 0) (layer "F.SilkS"))\n'
            '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "K")))\n'
            '  (gr_text "R1" (at 15 6 0) (layer "F.SilkS") '
            '(effects (font (size 1 1) (thickness 0.15))))\n')
    bg = _board(tmp_path_factory, "silkclear", body)
    root = __import__("sexpdata").loads(bg.path.read_text(encoding="utf-8"))
    vs = check_silk.run_checks(bg, check_silk.parse_silk(root))
    assert [v for v in vs if v["kind"] == "silk_over_pad"] == []


def test_silk_legibility(tmp_path_factory):
    body = ('  (gr_text "tiny" (at 10 6 0) (layer "F.SilkS") '
            '(effects (font (size 0.5 0.5) (thickness 0.15))))\n')
    bg = _board(tmp_path_factory, "silktiny", body)
    root = __import__("sexpdata").loads(bg.path.read_text(encoding="utf-8"))
    vs = check_silk.run_checks(bg, check_silk.parse_silk(root))
    assert any(v["kind"] == "silk_illegible" for v in vs)


def test_silk_rotated_refdes_no_false_positive(tmp_path_factory):
    """A vertical refdes tucked beside tiny pads (the rf4 C15 case) must be
    read with the absolute text angle so its box clears the pads."""
    # fp rotated -90: pad locals (+/-0.775, 0) become a vertical column at
    # x=fp_x; the ref local (0,-1.43,270) puts vertical text 1.43 mm to the
    # side. Reading the text angle as absolute (270) keeps its box off the pads;
    # (mis)adding the fp rotation would swing it horizontal, across them.
    body = ('  (footprint "t:C" (at 10 6 -90) (layer "F.Cu")\n'
            '    (property "Reference" "C15" (at 0 -1.43 270) (layer "F.SilkS") '
            '(effects (font (size 1 1) (thickness 0.15))))\n'
            '    (pad "1" smd roundrect (at -0.775 0) (size 0.9 0.95) '
            '(roundrect_rratio 0.25) (layers "F.Cu") (net "A"))\n'
            '    (pad "2" smd roundrect (at 0.775 0) (size 0.9 0.95) '
            '(roundrect_rratio 0.25) (layers "F.Cu") (net "B")))\n')
    bg = _board(tmp_path_factory, "c15", body)
    root = __import__("sexpdata").loads(bg.path.read_text(encoding="utf-8"))
    vs = check_silk.run_checks(bg, check_silk.parse_silk(root))
    assert [v for v in vs if v["kind"] == "silk_over_pad"] == []


def _phase_conn(tmp_path_factory, name, nets, label=None):
    pads = "".join(f'    (pad "{i}" thru_hole circle (at {i * 2.5} 0) '
                   f'(size 1.8 1.8) (drill 1) (layers "*.Cu") (net "{n}"))\n'
                   for i, n in enumerate(nets, 1))
    body = ('  (footprint "t:Conn" (at 5 5) (layer "F.Cu")\n'
            '    (property "Reference" "J8" (at 0 -3 0) (layer "F.SilkS"))\n'
            + pads + '  )\n')
    if label:
        body += (f'  (gr_text "{label}" (at 10 8.5 0) (layer "F.SilkS") '
                 '(effects (font (size 1 1) (thickness 0.15))))\n')
    bg = _board(tmp_path_factory, name, body)
    root = __import__("sexpdata").loads(bg.path.read_text(encoding="utf-8"))
    return [v for v in check_silk.check_pin_labels(bg, check_silk.parse_silk(root))
            if v["kind"] == "pin_labels_missing"]


def test_silk_pin_labels_missing_on_phase_connector(tmp_path_factory):
    """#886 planted fault: minuet J8 carries the fan motor's three phases
    with no A/B/C on silk; any cable order fits."""
    vs = _phase_conn(tmp_path_factory, "ph", ["/fan/A", "/fan/B", "/fan/C"])
    assert len(vs) == 1 and vs[0]["refs"] == ["J8"]
    assert vs[0]["phases"] == ["A", "B", "C"]
    # U/V/W behind a prefix is the same fault
    assert _phase_conn(tmp_path_factory, "uvw",
                       ["/m/PH_U", "/m/PH_V", "/m/PH_W"])


def test_silk_pin_labels_present_or_not_phases(tmp_path_factory):
    nets = ["/fan/A", "/fan/B", "/fan/C"]
    # labelled one by one, or run together, beside the pins: clean
    assert _phase_conn(tmp_path_factory, "lab", nets, "A B C") == []
    assert _phase_conn(tmp_path_factory, "lab2", nets, "ABC") == []
    # a label naming only some phases does not count
    assert _phase_conn(tmp_path_factory, "lab3", nets, "A B")
    # a two-pin power input is not a phase connector (oasis J1, digits J3)
    assert _phase_conn(tmp_path_factory, "pwr", ["+18V", "GND"]) == []
    assert _phase_conn(tmp_path_factory, "sig",
                       ["/SDA", "/SCL", "/VCC"]) == []


def _silk_just_hits(tmp_path_factory, name, x, justify, side="F"):
    eff = f" (justify {justify})" if justify else ""
    body = (f'  (footprint "t:J" (at 10 6) (layer "{side}.Cu")\n'
            f'    (property "Reference" "J1" (at 0 -3 0) (layer "{side}.SilkS"))\n'
            f'    (pad "1" smd rect (at 0 0) (size 1 1) (layers "{side}.Cu") (net "K")))\n'
            f'  (gr_text "LONGLABEL" (at {x} 6 0) (layer "{side}.SilkS") '
            f'(effects (font (size 1 1) (thickness 0.15)){eff}))\n')
    bg = _board(tmp_path_factory, name, body)
    root = __import__("sexpdata").loads(bg.path.read_text(encoding="utf-8"))
    vs = check_silk.run_checks(bg, check_silk.parse_silk(root))
    return [v for v in vs if v["kind"] == "silk_over_pad" and "J1" in v["refs"]]


def test_silk_text_justification(tmp_path_factory):
    """#427: a left-justified label starts AT its anchor; boxing it as centred
    put half its width over a pad it never touches."""
    # anchor 1 mm right of the pad centre: left-justified runs away from it
    assert _silk_just_hits(tmp_path_factory, "jl", 11, "left") == []
    # the same text centred there straddles the pad
    assert _silk_just_hits(tmp_path_factory, "jc", 11, None)
    # right-justified, anchored left of the pad: clear; anchored right: hits
    assert _silk_just_hits(tmp_path_factory, "jr", 9, "right") == []
    assert _silk_just_hits(tmp_path_factory, "jr2", 11, "right")
    # bottom-side mirrored text: left-justify runs to the LEFT in board coords
    assert _silk_just_hits(tmp_path_factory, "jm", 9, "left mirror", "B") == []
    assert _silk_just_hits(tmp_path_factory, "jm2", 11, "left mirror", "B")


def test_silk_justify_parse():
    import sexpdata
    eff = sexpdata.loads('(effects (font (size 1 1)) (justify right top mirror))')
    assert check_silk._justify(eff) == (-1, -1)
    assert check_silk._justify(sexpdata.loads('(effects (font (size 1 1)))')) == (0, 0)


# ============================================================ pure: cluster

def test_cluster_by_net_kind_region():
    violations = [
        {"source": "check_return_path", "severity": "error",
         "kind": "corridor_void", "net": "/MCO", "pos": [141.0, 123.0]},
        {"source": "check_return_path", "severity": "error",
         "kind": "corridor_void", "net": "/MCO", "pos": [141.3, 123.2]},
        {"source": "check_current", "severity": "error",
         "kind": "undersized_track", "net": "+3V3", "pos": [60.0, 40.0]},
        {"source": "check_pdn", "severity": "warning",
         "kind": "pdn_no_bulk", "net": "VDDA", "pos": None},
    ]
    clusters = cluster_violations.cluster(violations, radius=5.0)
    assert len(clusters) == 3          # two /MCO merge by region; others alone
    mco = next(c for c in clusters if c["net"] == "/MCO")
    assert mco["count"] == 2 and mco["fixer"] == "plane"
    assert mco["region"]["bbox"] is not None
    # errors sort before warnings
    assert clusters[0]["severity"] == "error"
    assert clusters[-1]["severity"] == "warning"


def test_cluster_far_apart_split():
    violations = [
        {"source": "c", "severity": "error", "kind": "corridor_void",
         "net": "GND", "pos": [10, 10]},
        {"source": "c", "severity": "error", "kind": "corridor_void",
         "net": "GND", "pos": [80, 80]},
    ]
    clusters = cluster_violations.cluster(violations, radius=5.0)
    assert len(clusters) == 2          # same net+kind, but far apart


# ============================================================ corpus: goldens

@pytest.mark.parametrize("board", BOARDS)
def test_golden_clean_diffpair(board):
    payload, _ = check_diffpair.run(["--pcb", str(board_path(board))])
    assert payload["status"] == "pass", json.dumps(payload["violations"])


@pytest.mark.parametrize("board", BOARDS)
def test_golden_clean_silk(board):
    payload, _ = check_silk.run(["--pcb", str(board_path(board))])
    if board == "rf4":
        # T6: the refdes-attribution metric found a TRUE instance in this
        # golden - C14's refdes sits 4.14 mm beyond its own pads and 0.50 mm
        # from C1 (the EasyEDA (0,-4) blanket-offset class the check was
        # built for). The golden predates the check and is generator-owned
        # (fixing it means regenerating the S1 corpus), so the finding is
        # pinned EXACTLY: any second finding, severity change or kind change
        # still trips this test. Zero FALSE positives on goldens holds.
        vs = payload["violations"]
        assert [v["kind"] for v in vs] == ["silk_misattributed"], \
            json.dumps(vs)
        assert vs[0]["refs"] == ["C14"] and vs[0]["nearest_ref"] == "C1"
        assert vs[0]["severity"] == "warning"
    else:
        assert payload["status"] == "pass", json.dumps(payload["violations"])


@pytest.mark.parametrize("board", BOARDS)
def test_golden_clean_creepage(board):
    payload, _ = check_creepage.run(
        ["--pcb", str(board_path(board)),
         "--constraints", str(GOLDEN / board / "constraints.json")])
    assert payload["status"] == "pass", json.dumps(payload["violations"])


@pytest.mark.parametrize("board", BOARDS)
def test_golden_clean_thermal(board):
    payload, _ = check_thermal.run(
        ["--pcb", str(board_path(board)),
         "--constraints", str(GOLDEN / board / "constraints.json")])
    assert payload["status"] == "pass", json.dumps(payload["violations"])


@pytest.mark.parametrize("board", BOARDS)
def test_golden_clean_pdn(board):
    payload, _ = check_pdn.run(
        ["--pcb", str(board_path(board)),
         "--constraints", str(GOLDEN / board / "constraints.json"),
         "--decoupling", str(GOLDEN / board / "decoupling.json")])
    assert payload["status"] == "pass", json.dumps(payload["violations"])


GOLDEN_KNOWN_WARNINGS = {
    "blinky2": {"check_connector_fit": 2},
    "rf4": {"check_silk": 1, "check_wicking": 3, "check_connector_fit": 1},
    "usbbuck4": {"check_wicking": 1, "check_connector_fit": 2,
                 "check_body_clash": 1},
}


def test_verify_all_goldens_clean(tmp_path):
    for board in BOARDS:
        summary, _ = verify_all.run(
            ["--pcb", str(board_path(board)),
             "--constraints", str(GOLDEN / board / "constraints.json"),
             "--decoupling", str(GOLDEN / board / "decoupling.json"),
             "--reports-dir", str(tmp_path / board)])
        # the known warnings, nothing else: rf4's C14 silk_misattributed
        # (see test_golden_clean_silk); open vias in pads (#475): rf4's J1
        # edge-SMA ground pads (top and bottom) and U1 pad 1, usbbuck4's
        # Y1 pad 4; KiCad's stock 1.0 mm header drill under IPC-2222 (#474:
        # tht_hole_ipc2222, one per header: blinky2 J1 J2, rf4 J2,
        # usbbuck4 J2 J3)
        known = GOLDEN_KNOWN_WARNINGS.get(board, {})
        by_check = {k: v for k, v in summary["counts"]["by_check"].items()
                    if v}
        assert by_check == known, json.dumps(summary["counts"])
        assert summary["counts"]["by_severity"] == (
            {"warning": sum(known.values())} if known else {}), \
            json.dumps(summary["counts"])
        # every built check actually ran (not skipped) on a golden
        for name in ("check_return_path", "check_current", "check_decoupling",
                     *S5_CHECKS):
            expect = "violations" if (board, name) == ("rf4", "check_silk") \
                else "pass"
            # a golden with no diff pair or antenna has nothing to judge (#591)
            got = summary["checks"][name]["status"]
            assert got == expect or got == "nothing_to_judge", name
        assert {"check_return_path", "check_current", "check_decoupling",
                "check_courtyard"} \
            <= set(summary["coverage"]["passed"])


# ============================================================ corpus: mutants

def test_mutant_diffpair_skew_caught():
    m = MANIFEST["mutants"]["diffpair-skew"]
    board = m["board"]
    payload, _ = check_diffpair.run(
        ["--pcb", str(mutant_path("diffpair-skew")),
         "--constraints", str(mutant_input("diffpair-skew",
                                           "constraints.json"))])
    assert payload["status"] == "violations"
    exp = m["expect"]
    hits = [v for v in payload["violations"]
            if v["kind"] == "diffpair_uncoupled"
            and set(v["pair"]) == set(exp["pair"])]
    assert len(hits) == 1, json.dumps(payload["violations"])
    assert hits[0]["uncoupled_mm"] >= exp["min_skew_mm"]
    assert hits[0]["severity"] == "error"


def test_mutant_silk_over_pad_caught():
    m = MANIFEST["mutants"]["silk-over-pad"]
    payload, _ = check_silk.run(["--pcb", str(mutant_path("silk-over-pad"))])
    assert payload["status"] == "violations"
    exp = m["expect"]
    hits = [v for v in payload["violations"]
            if v["kind"] == "silk_over_pad" and exp["ref"] in v["refs"]]
    assert len(hits) == 1, json.dumps(payload["violations"])
    v = hits[0]
    assert math.hypot(v["pos"][0] - exp["pos"][0],
                      v["pos"][1] - exp["pos"][1]) < 0.5


def _catch(mutant: str, violations: list[dict]) -> list[dict]:
    """Violations matching EVERY key of the mutant's manifest `expect`
    (kind, net == v.net, ref in v.refs, pos within 1 mm, layer)."""
    exp = MANIFEST["mutants"][mutant]["expect"]
    hits = []
    for v in violations:
        if v.get("kind") != exp["kind"]:
            continue
        if "net" in exp and v.get("net") != exp["net"]:
            continue
        if "ref" in exp and exp["ref"] not in v.get("refs", []):
            continue
        if "layer" in exp and v.get("layer") != exp["layer"]:
            continue
        if "pos" in exp and (v.get("pos") is None or math.hypot(
                v["pos"][0] - exp["pos"][0], v["pos"][1] - exp["pos"][1]) > 1.0):
            continue
        hits.append(v)
    return hits


def test_mutant_hv_rail_spacing_caught():
    payload, _ = check_creepage.run(
        ["--pcb", str(mutant_path("hv-rail-spacing")),
         "--constraints", str(mutant_input("hv-rail-spacing",
                                           "constraints.json"))])
    assert payload["status"] == "violations"
    hits = _catch("hv-rail-spacing", payload["violations"])
    assert len(hits) == 1, json.dumps(payload["violations"])
    assert hits[0]["spacing_mm"] < hits[0]["required_mm"]


def test_mutant_ldo_thermal_starved_caught():
    payload, _ = check_thermal.run(
        ["--pcb", str(mutant_path("ldo-thermal-starved")),
         "--constraints", str(mutant_input("ldo-thermal-starved",
                                           "constraints.json"))])
    hits = _catch("ldo-thermal-starved", payload["violations"])
    assert len(hits) == 1, json.dumps(payload["violations"])
    assert hits[0]["rise_c"] > hits[0]["dt_allowed_c"]


def test_mutant_swdio_off_grid_caught():
    payload, _ = check_route_style.run(
        ["--pcb", str(mutant_path("swdio-off-grid"))])
    hits = _catch("swdio-off-grid", payload["violations"])
    assert len(hits) == 1, json.dumps(payload["violations"])
    assert hits[0]["style"] == "angle"
    assert len(hits[0]["items"]) == 2          # both dogleg legs


def test_mutant_rail_cap_missing_caught():
    payload, _ = check_pdn.run(
        ["--pcb", str(mutant_path("rail-cap-missing")),
         "--constraints", str(mutant_input("rail-cap-missing",
                                           "constraints.json")),
         "--decoupling", str(mutant_input("rail-cap-missing",
                                          "decoupling.json"))])
    hits = _catch("rail-cap-missing", payload["violations"])
    assert len(hits) == 1, json.dumps(payload["violations"])
    assert hits[0]["severity"] == "error" and hits[0]["cap_count"] == 0


def test_mutant_cap_undervoltage_caught():
    payload, _ = check_ratings.run(
        ["--pcb", str(mutant_path("cap-undervoltage")),
         "--parts", str(mutant_input("cap-undervoltage", "parts"))])
    hits = _catch("cap-undervoltage", payload["violations"])
    assert len(hits) == 1, json.dumps(payload["violations"])
    assert hits[0]["severity"] == "error" and hits[0]["limit_v"] == 4


@pytest.mark.parametrize("mutant", sorted(MANIFEST["mutants"]))
def test_non_target_mutants_stay_clean(mutant):
    """Each S5 check must not fire on mutants it does not own (no cross-talk)."""
    target = MANIFEST["mutants"][mutant]["check"]
    board = golden_of(mutant)
    for script, mod in (("check_diffpair", check_diffpair),
                        ("check_silk", check_silk),
                        ("check_creepage", check_creepage),
                        ("check_thermal", check_thermal),
                        ("check_route_style", check_route_style),
                        ("check_pdn", check_pdn)):
        if script == target:
            continue
        argv = ["--pcb", str(mutant_path(mutant))]
        if script in ("check_creepage", "check_thermal"):
            argv += ["--constraints",
                     str(mutant_input(mutant, "constraints.json"))]
        elif script == "check_pdn":
            argv += ["--constraints",
                     str(mutant_input(mutant, "constraints.json")),
                     "--decoupling",
                     str(mutant_input(mutant, "decoupling.json"))]
        payload, _ = mod.run(argv)
        if script == "check_silk" and board == "rf4":
            # rf4-derived mutants inherit the golden's one known C14
            # silk_misattributed warning (see test_golden_clean_silk);
            # pinned exactly so real cross-talk still trips
            vs = payload["violations"]
            assert [v["kind"] for v in vs] == ["silk_misattributed"] \
                and vs[0]["refs"] == ["C14"], \
                f"{script} fired on {mutant}: {json.dumps(vs)}"
            continue
        assert payload["status"] == "pass", \
            f"{script} fired on {mutant}: {json.dumps(payload['violations'])}"


def test_full_manifest_coverage(tmp_path):
    """Every mutant whose designated check exists by S5 is caught by that check
    when the whole suite runs (cpl-rotation -> dfm_check is S12's to catch)."""
    for mutant, m in MANIFEST["mutants"].items():
        target = m["check"]
        if target not in BUILT_CHECKS:
            continue
        board = m["board"]
        argv = ["--pcb", str(mutant_path(mutant)),
                "--constraints", str(mutant_input(mutant, "constraints.json")),
                "--decoupling", str(mutant_input(mutant, "decoupling.json")),
                "--reports-dir", str(tmp_path / mutant)]
        parts = GOLDEN / "mutants" / mutant / "parts"
        if parts.is_dir():          # check_ratings' input (goldens have none)
            argv += ["--parts", str(parts)]
        summary, _ = verify_all.run(argv)
        assert summary["checks"][target]["status"] == "violations", \
            f"{mutant}: {target} did not catch it"
        assert any(v.get("source") == target for v in summary["violations"])


def test_cpl_rotation_deferred_to_dfm():
    """cpl-rotation is dfm_check's (S12); it must not be caught by any S5 check
    (documents the coverage gap so it is not silently 'passing')."""
    assert MANIFEST["mutants"]["cpl-rotation"]["check"] == "dfm_check"
    assert "dfm_check" not in BUILT_CHECKS


# ============================================================ verify_all schema

def test_verify_all_schema(tmp_path):
    summary, _ = verify_all.run(
        ["--pcb", str(board_path("usbbuck4")),
         "--constraints", str(GOLDEN / "usbbuck4" / "constraints.json"),
         "--decoupling", str(GOLDEN / "usbbuck4" / "decoupling.json"),
         "--reports-dir", str(tmp_path)])
    for key in ("script", "board", "status", "counts", "checks", "violations"):
        assert key in summary
    assert summary["script"] == "verify_all"
    assert set(summary["counts"]) >= {"total", "by_severity", "by_source",
                                      "by_check"}
    # every check has an entry with a stable shape
    for name in ("check_return_path", *S5_CHECKS):
        entry = summary["checks"][name]
        assert set(entry) >= {"status", "counts", "report"}
    # per-check report files were written
    assert (tmp_path / "summary.json").exists()
    assert (tmp_path / "check_silk.json").exists()


def test_verify_all_skips_missing_inputs(tmp_path):
    """Without decoupling/constraints, the checks needing them are SKIPPED, not
    errored; board-only checks still run."""
    summary, _ = verify_all.run(
        ["--pcb", str(board_path("blinky2")),
         "--reports-dir", str(tmp_path)])
    assert summary["checks"]["check_decoupling"]["status"] == "skipped"
    assert summary["checks"]["check_pdn"]["status"] == "skipped"
    assert summary["checks"]["check_silk"]["status"] == "pass"
    # blinky2 has no differential pair: it ran, with nothing to judge (#591)
    assert summary["checks"]["check_diffpair"]["status"] == "nothing_to_judge"
    assert "check_diffpair" in summary["coverage"]["nothing_to_judge"]


# ============================================================ CLI contract

def test_cli_exit_codes_and_out(tmp_path):
    # clean board -> exit 0
    proc = subprocess.run(
        [PYTHON, str(SCRIPTS / "check_diffpair.py"),
         "--pcb", str(board_path("usbbuck4")), "--out", str(tmp_path / "d.json")],
        capture_output=True, text=True)
    assert proc.returncode == 0
    payload = json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))
    assert payload["script"] == "check_diffpair" and payload["status"] == "pass"
    # mutant -> exit 1 with normalized schema
    proc = subprocess.run(
        [PYTHON, str(SCRIPTS / "check_silk.py"),
         "--pcb", str(mutant_path("silk-over-pad"))],
        capture_output=True, text=True)
    assert proc.returncode == 1
    v = json.loads(proc.stdout)["violations"][0]
    for key in ("check", "severity", "pos", "layer", "net", "refs", "msg",
                "source", "items"):
        assert key in v
    # cluster_violations exits 1 when clusters present
    summ = tmp_path / "s.json"
    summ.write_text(json.dumps({"violations": [
        {"source": "c", "severity": "error", "kind": "x", "net": "N",
         "pos": [1, 1]}]}), encoding="utf-8")
    proc = subprocess.run(
        [PYTHON, str(SCRIPTS / "cluster_violations.py"), "--input", str(summ)],
        capture_output=True, text=True)
    assert proc.returncode == 1
    # bad input -> exit 2
    proc = subprocess.run(
        [PYTHON, str(SCRIPTS / "check_creepage.py"),
         "--pcb", str(board_path("rf4")), "--constraints", str(tmp_path / "no.json")],
        capture_output=True, text=True)
    assert proc.returncode == 2


# ============================================================ regressions
# (bugs found by the S5 adversarial review; each test locks a fix)

def test_discover_pairs_conservative():
    """H/L and lone single-letter suffixes are NOT differential pairs."""
    d = check_diffpair.discover_pairs
    assert d({"/VREFH", "/VREFL"}) == []
    assert d({"/ADDR_H", "/ADDR_L"}) == []
    assert d({"/USB_DP", "/USB_DM"}) == [("/USB_DP", "/USB_DM")]   # still works
    assert d({"/LVDS_P", "/LVDS_N"}) == [("/LVDS_P", "/LVDS_N")]


def test_diffpair_unrouted_half_no_nan(tmp_path_factory):
    """One net pad-only (unrouted) -> reported not-routed, no NaN in the JSON."""
    bg = _board(tmp_path_factory, "unrouted", _diffpair_body(""))  # no D_N track
    vs, facts = check_diffpair.check_pair(bg, {"p": "/D_P", "n": "/D_N"})
    assert vs == [] and facts["routed"] is False
    json.dumps(facts, allow_nan=False)          # would raise on NaN tokens


def test_diffpair_absent_pair_warns_not_aborts(tmp_path):
    cons = tmp_path / "c.json"
    cons.write_text(json.dumps({"diff_pairs": [
        {"p": "/USB_DP", "n": "/USB_DM"},
        {"p": "/GHOST_P", "n": "/GHOST_N"}]}), encoding="utf-8")
    payload, _ = check_diffpair.run(
        ["--pcb", str(board_path("usbbuck4")), "--constraints", str(cons)])
    assert payload["status"] == "violations"     # warning, not exit-2 error
    kinds = {v["kind"] for v in payload["violations"]}
    assert "diffpair_missing_net" in kinds
    assert payload["checked"], "the valid pair was still evaluated"


def test_diffpair_empty_pairs_no_autodiscovery(tmp_path):
    cons = tmp_path / "c.json"
    cons.write_text(json.dumps({"diff_pairs": []}), encoding="utf-8")
    payload, _ = check_diffpair.run(
        ["--pcb", str(board_path("usbbuck4")), "--constraints", str(cons)])
    assert payload["status"] == "pass" and payload["checked"] == []


def test_diffpair_single_layer_no_crash(tmp_path_factory):
    text = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (25 "Edge.Cuts" user)) (setup)
  (gr_rect (start -2 -2) (end 24 12) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
""" + _diffpair_body(
        '  (segment (start 0 5.15) (end 18 5.15) (width 0.25) (layer "F.Cu") (net "/D_N"))\n') + ")\n"
    p = tmp_path_factory.mktemp("single") / "s.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    bg = geom.load_board(p)
    vs, facts = check_diffpair.check_pair(bg, {"p": "/D_P", "n": "/D_N"})
    assert facts["skew_ps"] is not None          # eps fell back to FR4, no crash


def test_creepage_absent_voltage_net_skipped(tmp_path):
    cons = tmp_path / "c.json"
    cons.write_text(json.dumps({"voltages": [{"net": "/GHOST", "voltage": 400}]}),
                    encoding="utf-8")
    payload, _ = check_creepage.run(
        ["--pcb", str(board_path("blinky2")), "--constraints", str(cons)])
    assert payload["status"] == "pass"           # skipped, not exit-2
    assert "/GHOST" in payload["skipped_absent_nets"]


def test_thermal_distant_pour_not_counted(tmp_path_factory):
    """A big same-net pour far from the part is not its heatsink (local reach)."""
    text = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)) (setup)
  (gr_rect (start -2 -2) (end 62 12) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
  (footprint "t:U" (at 10 6) (layer "F.Cu")
    (property "Reference" "U9" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "GND")))
  (zone (net "GND") (layer "F.Cu")
    (polygon (pts (xy 40 0) (xy 58 0) (xy 58 10) (xy 40 10)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 40 0) (xy 58 0) (xy 58 10) (xy 40 10)))))
"""
    p = tmp_path_factory.mktemp("distant") / "d.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    bg = geom.load_board(p)
    vs, facts = check_thermal.check_part(
        bg, {"ref": "U9", "net": "GND", "power_w": 1.0, "dt_c": 40})
    # the 180 mm^2 pour is >25 mm away -> excluded; only the 1 mm^2 pad counts
    assert facts["area_mm2"] < 10
    assert any(v["kind"] == "thermal_area" for v in vs)


def test_pdn_micro_sign_parsed():
    p = check_decoupling.parse_farads
    assert p("10µF") == pytest.approx(1e-5)   # micro sign
    assert p("10μF") == pytest.approx(1e-5)   # Greek mu

    class _BG:
        nets = {"+3V3"}
    vs, facts = check_pdn.check_rail(
        _BG(), "+3V3", 0.4, [{"cap": "C1", "rail": "+3V3", "value": "10µF"}])
    assert facts["bulk_count"] == 1 and vs == []   # not mis-read as no-bulk


def test_cluster_missing_source_no_crash():
    clusters = cluster_violations.cluster(
        [{"severity": "error", "kind": "x", "net": "N", "pos": [1, 1]}], 5.0)
    assert len(clusters) == 1 and clusters[0]["checks"] == []


def test_verify_all_no_stale_report(tmp_path):
    """A check that ERRORS this run must not be reported as pass from a stale
    report file left by a previous run."""
    # a power entry for a net the board lacks -> check_current exits 2 (an
    # unfilled zone no longer does: it's judged on a refill, or noted)
    board = tmp_path / "stale.kicad_pcb"
    board.write_text("""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)) (setup)
  (gr_rect (start 0 0) (end 20 10) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
  (segment (start 1 1) (end 9 1) (width 0.25) (layer "F.Cu") (net "+3V3")))
""", encoding="utf-8")
    cons = tmp_path / "constraints.json"
    cons.write_text(json.dumps({"power": [{"net": "+5V", "current_a": 0.4}]}),
                    encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "check_current.json").write_text(
        json.dumps({"script": "check_current", "status": "pass",
                    "counts": {"total": 0}, "violations": []}), encoding="utf-8")
    summary, _ = verify_all.run(
        ["--pcb", str(board), "--constraints", str(cons),
         "--reports-dir", str(reports)])
    assert summary["checks"]["check_current"]["status"] == "error"


# ================================================ T6: refdes attribution (P8B-5)

_MISATTR_BODY = """  (footprint "t:R" (at 5 5) (layer "F.Cu")
    (property "Reference" "R1" (at 7 0 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "A")))
  (footprint "t:R" (at 12 5) (layer "F.Cu")
    (property "Reference" "R2" (at 0 -1.2 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "B")))
"""


def test_silk_misattributed_fires(tmp_path_factory):
    """R1's refdes sits over R2's pads (5.4 mm beyond its own): flagged with
    the attribution extras; R2's tight refdes stays clean."""
    bg = _board(tmp_path_factory, "misattr", _MISATTR_BODY)
    payload, _ = check_silk.run(["--pcb", str(bg.path)])
    vs = [v for v in payload["violations"] if v["kind"] == "silk_misattributed"]
    assert len(vs) == 1, json.dumps(payload["violations"])
    v = vs[0]
    assert v["severity"] == "warning"
    assert v["refs"] == ["R1"]
    assert v["nearest_ref"] == "R2"
    assert v["offset_mm"] > 1.0 and v["nearest_mm"] < 1.0
    assert "move_text" in v["msg"] and "set_text" in v["msg"]
    assert payload["checked"][0]["refdes_checked"] == 2
    assert payload["refdes_off_silk"] == []


def test_silk_misattributed_far_but_unambiguous_clean(tmp_path_factory):
    """A refdes far from its part with NOTHING nearby is not misattributed
    (attribution beats closeness - the carrier sweep's rule)."""
    body = """  (footprint "t:R" (at 5 5) (layer "F.Cu")
    (property "Reference" "R1" (at 0 -3 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "A")))
"""
    bg = _board(tmp_path_factory, "farlone", body)
    payload, _ = check_silk.run(["--pcb", str(bg.path)])
    assert not [v for v in payload["violations"]
                if v["kind"] == "silk_misattributed"]


def test_silk_misattributed_hidden_ref_skipped(tmp_path_factory):
    body = _MISATTR_BODY.replace(
        '(property "Reference" "R1" (at 7 0 0) (layer "F.SilkS")',
        '(property "Reference" "R1" (at 7 0 0) (layer "F.SilkS") (hide yes)')
    bg = _board(tmp_path_factory, "hiddenref", body)
    payload, _ = check_silk.run(["--pcb", str(bg.path)])
    assert not [v for v in payload["violations"]
                if v["kind"] == "silk_misattributed"]
    # hidden is not silently dropped: the report lists it (as a fact, not a
    # violation, so the status stays pass) for the assembly drawing
    assert payload["status"] == "pass"
    assert payload["refdes_off_silk"] == [{"ref": "R1", "why": "hidden"}]
    assert payload["checked"][0]["refdes_off_silk"] == 1


def test_refdes_off_silk_lists_fab_layer_and_skips_shown(tmp_path_factory):
    body = _MISATTR_BODY.replace(
        '(property "Reference" "R1" (at 7 0 0) (layer "F.SilkS")',
        '(property "Reference" "R1" (at 0 0 0) (layer "F.Fab")').replace(
        '(property "Reference" "R2" (at 0 -1.2 0) (layer "F.SilkS")',
        '(property "Reference" "R2" (at 0 -1.2 0) (layer "F.SilkS") '
        '(hide no)')
    bg = _board(tmp_path_factory, "fabref", body)
    payload, _ = check_silk.run(["--pcb", str(bg.path)])
    assert payload["refdes_off_silk"] == [{"ref": "R1", "why": "on F.Fab"}]


def test_silk_misattributed_routes_to_silk_domain():
    assert cluster_violations.FIXER_HINTS["silk_misattributed"] == "silk"


# ============================================ T6: constraints twin drift (P8B-6)

_DRIFT_BOARD = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)) (setup)
  (gr_rect (start 0 0) (end 20 10) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
  (segment (start 1 1) (end 9 1) (width 0.5) (layer "F.Cu") (net "+3V3"))
)
"""


def _drift_ws(tmp_path, kicad_obj, arch_obj):
    ws = tmp_path / "ws"
    (ws / "kicad").mkdir(parents=True)
    board = ws / "kicad" / "b.kicad_pcb"
    board.write_text(_DRIFT_BOARD, encoding="utf-8")
    cons = ws / "kicad" / "constraints.json"
    cons.write_text(json.dumps(kicad_obj), encoding="utf-8")
    if arch_obj is not None:
        (ws / "architecture").mkdir()
        (ws / "architecture" / "constraints.json").write_text(
            arch_obj if isinstance(arch_obj, str) else json.dumps(arch_obj),
            encoding="utf-8")
    return board, cons


def test_constraints_drift_warning(tmp_path):
    obj = {"power": [{"net": "+3V3", "current_a": 0.2}]}
    stale = {"power": [{"net": "+3V3", "current_a": 1.5}]}
    board, cons = _drift_ws(tmp_path, obj, stale)
    summary, _ = verify_all.run(
        ["--pcb", str(board), "--constraints", str(cons),
         "--reports-dir", str(tmp_path / "r")])
    drift = [v for v in summary["violations"]
             if v["kind"] == "constraints_drift"]
    assert len(drift) == 1, json.dumps(summary["counts"])
    assert drift[0]["severity"] == "warning"     # surfaces at H4, never fails
    assert "reconcile" in drift[0]["msg"]
    assert summary["counts"]["by_check"]["verify_all"] == 1
    assert summary["status"] == "violations"


def test_constraints_drift_formatting_only_ok(tmp_path):
    """Parsed-object compare: whitespace/key-order drift is NOT a finding."""
    obj = {"power": [{"net": "+3V3", "current_a": 0.2}], "z": 1}
    twin_text = json.dumps({"z": 1, "power": [
        {"current_a": 0.2, "net": "+3V3"}]}, indent=4)
    board, cons = _drift_ws(tmp_path, obj, twin_text)
    summary, _ = verify_all.run(
        ["--pcb", str(board), "--constraints", str(cons),
         "--reports-dir", str(tmp_path / "r")])
    assert not [v for v in summary["violations"]
                if v["kind"] == "constraints_drift"]


def test_constraints_drift_no_twin_ok(tmp_path):
    obj = {"power": [{"net": "+3V3", "current_a": 0.2}]}
    board, cons = _drift_ws(tmp_path, obj, None)
    summary, _ = verify_all.run(
        ["--pcb", str(board), "--constraints", str(cons),
         "--reports-dir", str(tmp_path / "r")])
    assert not [v for v in summary["violations"]
                if v["kind"] == "constraints_drift"]
    assert summary["status"] == "pass"


def test_constraints_drift_unreadable_twin_warns(tmp_path):
    obj = {"power": [{"net": "+3V3", "current_a": 0.2}]}
    board, cons = _drift_ws(tmp_path, obj, "{not json")
    summary, _ = verify_all.run(
        ["--pcb", str(board), "--constraints", str(cons),
         "--reports-dir", str(tmp_path / "r")])
    drift = [v for v in summary["violations"]
             if v["kind"] == "constraints_drift"]
    assert len(drift) == 1 and drift[0]["severity"] == "warning"


# ============================================================ smoke: timing

@pytest.mark.smoke
def test_verify_all_rf4_under_30s(tmp_path):
    t0 = time.perf_counter()
    geom._CACHE.clear()
    verify_all.run(
        ["--pcb", str(board_path("rf4")),
         "--constraints", str(GOLDEN / "rf4" / "constraints.json"),
         "--decoupling", str(GOLDEN / "rf4" / "decoupling.json"),
         "--reports-dir", str(tmp_path)])
    elapsed = time.perf_counter() - t0
    assert_under(elapsed, 30.0, "verify_all on rf4")


# ------------------------------------------- thermal: copper past 1 in^2

def test_fin_reproduces_th5_and_extends_the_curve():
    """The fin model (emc-thermal TH-5): a 2 mm source on 1 oz still gains
    ~3x from 645 mm2 to an unbounded sheet; anchored to the curve's slope,
    copper keeps helping past A_SAT, flattening near ~2100 mm2."""
    fin = check_thermal.fin_theta
    assert fin(2.0, 645, 0.035) == pytest.approx(92, rel=0.02)
    assert fin(2.0, math.pi * 2000.0 ** 2, 0.035) == pytest.approx(32, rel=0.02)
    sp = check_thermal.spread_model(9.0, 0.035, False)
    sat = check_thermal.A_SAT_MM2
    th = sp["theta"]
    assert th(sat) == pytest.approx(check_thermal.theta_ja(sat, False))
    assert th(sat + 1e-3) == pytest.approx(th(sat), abs=0.01)  # continuous
    vals = [th(a) for a in (645, 1000, 2000, 5000, 1e5)]
    assert all(b < a for a, b in zip(vals, vals[1:]))
    assert sp["sheet_cw"] == pytest.approx(50, abs=3)
    assert sp["sat_mm2"] == pytest.approx(2100, rel=0.1)
    assert th(sp["sat_mm2"]) == pytest.approx(sp["sheet_cw"] * 1.1, rel=0.01)
    # thicker copper spreads further, so it saturates later and lower
    sp2 = check_thermal.spread_model(9.0, 0.070, False)
    assert sp2["sat_mm2"] > sp["sat_mm2"]


def test_thermal_2l_past_best_case_says_grow_when_spreading_meets(
        tmp_path_factory):
    """2.5 W, 85 C: over the screen's best 2-layer case (~38 C/W, pours
    capped at 1 in^2) but not the spreading model's (~29 C/W with ~2100 mm2
    pours), so the remedy names the bigger pour; pass/fail is unchanged."""
    vias = _via_grid(THERMAL_VIA, base=20.0)
    bg = _thermal_2l_board(tmp_path_factory, "amp2lgrow", vias=vias)
    vs, facts = check_thermal.check_part(bg, dict(AMP_2L, power_w=2.5))
    assert facts["theta_best_2l_spread_cw"] == pytest.approx(29, abs=1.5)
    assert facts["area_far_mm2"] == pytest.approx(900, rel=0.02)
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "even the best 2-layer case" in err[0]["msg"]
    assert "or grow both GND pours" in err[0]["msg"]
    assert "the screen credits copper only to 645 mm2" in err[0]["msg"]


def test_thermal_2l_big_pour_already_saturated_no_grow(tmp_path_factory):
    """Pours already past the saturation radius: no 'grow' advice."""
    vias = _via_grid(THERMAL_VIA, base=20.0)
    bg = _thermal_2l_board(tmp_path_factory, "amp2lbig", vias=vias, half=30.0)
    vs, facts = check_thermal.check_part(bg, dict(AMP_2L, power_w=2.5))
    assert facts["area_far_mm2"] >= 0.98 * facts["copper_sat_mm2"]
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "or grow" not in err[0]["msg"]


def test_thermal_over_even_a_sheet_no_grow(tmp_path_factory):
    """3 W into 85 C is past even the spreading model's big pours."""
    vias = _via_grid(THERMAL_VIA, base=20.0)
    bg = _thermal_2l_board(tmp_path_factory, "amp2lsheet", vias=vias)
    vs, _ = check_thermal.check_part(bg, dict(AMP_2L, power_w=3.0))
    err = [v for v in vs if v["kind"] == "thermal_area"]
    assert err and "or grow" not in err[0]["msg"]
