"""sim_si / lib/coupledline: crosstalk by coupled-line simulation (#497).

Known answers: Bogatin's closed forms on a long weakly coupled line, FEXT -> 0
in a homogeneous stripline, even/odd decomposition into two uncoupled lines,
three engines on one deck, and the terminations check_si's estimate leaves out.
Tests that run a simulator skip when there's no IIC-OSIC-TOOLS tree.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "tests"))
import check_si  # noqa: E402
import coupledline as cl  # noqa: E402
import geom  # noqa: E402
import impedance  # noqa: E402
import sim_si  # noqa: E402
import solvers  # noqa: E402
from test_check_si import PLANES, fp, seg  # noqa: E402,F401

need_ngspice = pytest.mark.skipif(solvers.tool_path("ngspice") is None, reason="no ngspice")
need_xyce = pytest.mark.skipif(solvers.tool_path("xyce") is None, reason="no xyce")
need_fields = pytest.mark.skipif(not impedance.SOLVER_OK, reason="no scipy")

# weak coupling by construction: Cm/C = 0.1, Lm/L = 0.1 and 0.2
HOMOG = {"L": [[3.2e-7, 3.2e-8], [3.2e-8, 3.2e-7]],
         "C": [[1.28e-10, -1.28e-11], [-1.28e-11, 1.28e-10]]}
INHOM = {"L": [[3.2e-7, 6.4e-8], [6.4e-8, 3.2e-7]],
         "C": [[1.28e-10, -1.28e-11], [-1.28e-11, 1.28e-10]]}


def matched_deck(lc, length_m, t_r, v=1.0, rload=None):
    cf = cl.closed_form(lc, length_m, t_r)
    z = rload or cf["z0"]
    d = cl.Deck()
    d.ramp("s", v, 0.2e-9, t_r)
    d.r("s", "an", z)
    d.r("af", 0, z)
    d.r("vn", 0, z)
    d.r("vf", 0, z)
    d.coupled("an", "vn", "af", "vf", lc, length_m)
    d.probes = ["an", "af", "vn", "vf"]
    d.tstop = 0.2e-9 + t_r + 3 * length_m / cf["v_m_s"]
    d.tstep = t_r / 50
    return d, cf


# ------------------------------------------------------------ arithmetic

def test_closed_form_by_hand():
    # the issue's formulas: kb = (kC + kL)/4, kf = (kC - kL)/2 with v = 1/sqrt(LC)
    cf = cl.closed_form(INHOM, 0.2, 200e-12)
    assert cf["kC"] == pytest.approx(0.1)
    assert cf["kL"] == pytest.approx(0.2)
    assert cf["kb"] == pytest.approx(0.075)
    assert cf["kf"] == pytest.approx(-0.05)
    assert cf["v_m_s"] == pytest.approx(1 / math.sqrt(3.2e-7 * 1.28e-10))  # 1.56e8 m/s
    assert cf["z0"] == pytest.approx(50.0)
    assert cf["l_sat_m"] == pytest.approx(cf["v_m_s"] * 200e-12 / 2)
    assert cf["fext"] == pytest.approx(-0.05 * 0.2 / (cf["v_m_s"] * 200e-12))


def test_modes_of_a_symmetric_pair_are_even_and_odd():
    m = cl.modes(INHOM)
    L, C = np.array(INHOM["L"]), np.array(INHOM["C"])
    ze = math.sqrt((L[0, 0] + L[0, 1]) / (C[0, 0] + C[0, 1]))
    zo = math.sqrt((L[0, 0] - L[0, 1]) / (C[0, 0] - C[0, 1]))
    # T normalises C_m = 1, so "z" is in that scale: compare the delays and shapes
    T = m["T"]
    assert abs(abs(T[0, 0]) - abs(T[1, 0])) < 1e-9  # (1, +-1) shapes
    delays = sorted(m["delay_s_m"])
    assert delays == pytest.approx(sorted([math.sqrt((L[0, 0] + L[0, 1]) * (C[0, 0] + C[0, 1])),
                                           math.sqrt((L[0, 0] - L[0, 1]) * (C[0, 0] - C[0, 1]))]))
    assert ze > zo


def test_modes_survive_a_homogeneous_line():
    # L C is a multiple of 1 here, where a plain eig gives T = 1: not modes
    m = cl.modes(HOMOG)
    T, C, L = m["T"], np.array(HOMOG["C"]), np.array(HOMOG["L"])
    assert T.T @ C @ T == pytest.approx(np.eye(2), abs=1e-9)
    Lm = np.linalg.inv(T) @ L @ np.linalg.inv(T).T
    assert abs(Lm[0, 1]) < 1e-12 * abs(Lm[0, 0]) + 1e-20


@need_fields
def test_lc_full_mesh_matches_even_odd():
    # the two-sided mesh for unequal widths, on a section the mirror mesh can do
    a = impedance.lc_microstrip(0.2, 0.2, 0.4, 0.2, 0.035, 4.3, None)
    b = impedance._lc_unequal(impedance._pair_traces(0.2, 0.2, 0.4, 0.2, 0.035),
                              ((0.0, 0.2, 4.3),), (), None)
    for m in ("L", "C"):
        assert np.array(a[m]) == pytest.approx(np.array(b[m]), rel=1e-6)


@need_fields
def test_lc_agrees_with_the_estimates_coefficients():
    r = impedance.field_microstrip(0.2, 0.2, 0.035, 4.3, 0.4, None, even=True)
    k = impedance.coupling(r)
    cf = cl.closed_form(impedance.lc_from_modes(r), 0.1, 200e-12)
    assert cf["kb"] == pytest.approx(k["kb"], rel=1e-6)
    assert cf["kf"] == pytest.approx(k["kf"], rel=1e-6)


@need_fields
def test_lc_unequal_widths():
    lc = impedance.lc_microstrip(0.1, 0.4, 0.3, 0.2, 0.035, 4.3, None)
    assert lc["C"][0][1] == lc["C"][1][0] < 0
    # the wider line has more C and less L
    assert lc["C"][1][1] > lc["C"][0][0]
    assert lc["L"][1][1] < lc["L"][0][0]
    wide = impedance.lc_stripline(0.1, 0.2, 0.3, 0.2, 0.2, 0.035, 4.3, 4.3)
    # homogeneous: L C = (er / c^2) 1, whatever the widths
    prod = np.array(wide["L"]) @ np.array(wide["C"])
    assert prod == pytest.approx(np.eye(2) * 4.3 / impedance.C0 ** 2, rel=2e-3, abs=1e-20)


def test_deck_text_t_lines_dont_multiply_breakpoints():
    d = cl.Deck()
    d.line("a", "b", 50.0, 1e-10)
    assert "REL=100" in d.text("modal", "ngspice")
    assert "REL" not in d.text("modal", "xyce")


def test_deck_text_cpl_never_gets_a_vanishing_r():
    d, _ = matched_deck(INHOM, 0.1, 500e-12)
    txt = d.text("cpl", "ngspice")
    r = re.search(r"R=(\S+) 0 (\S+) ", txt)
    assert float(r.group(1)) >= cl.CPL_MIN_R and float(r.group(2)) >= cl.CPL_MIN_R
    with pytest.raises(ValueError):
        d.text("cpl", "xyce")


# ------------------------------------------------------------ known answers

@need_ngspice
@pytest.mark.parametrize("mode", ["modal", "cpl"])
def test_saturated_next_and_fext_match_bogatin(mode):
    """Long, weakly coupled, matched both ends: launched V = 0.5."""
    d, cf = matched_deck(INHOM, 0.3, 200e-12)
    assert cf["l_sat_m"] < 0.1 * 0.3 * 2  # long enough to saturate
    r = d.run(mode)
    assert cl.peak(r["vn"]) == pytest.approx(0.5 * cf["kb"], rel=0.05)
    assert cl.peak(r["vf"]) == pytest.approx(0.5 * cf["fext"], rel=0.05)
    assert cl.peak(r["af"]) == pytest.approx(0.5, rel=0.02)


@need_ngspice
@need_fields
def test_bogatin_on_a_solved_microstrip():
    lc = impedance.lc_microstrip(0.2, 0.2, 0.4, 0.2, 0.035, 4.3, None)
    d, cf = matched_deck(lc, 0.2, 200e-12)
    r = d.run("modal")
    assert cl.peak(r["vn"]) == pytest.approx(0.5 * cf["kb"], rel=0.05)
    assert cl.peak(r["vf"]) == pytest.approx(0.5 * cf["fext"], rel=0.05)
    assert cf["fext"] < 0  # inductive coupling wins in microstrip


@need_ngspice
@need_fields
def test_homogeneous_stripline_has_no_fext():
    lc = impedance.lc_stripline(0.15, 0.15, 0.2, 0.3, 0.3, 0.035, 4.3, 4.3)
    d, cf = matched_deck(lc, 0.2, 200e-12)
    assert abs(cf["kf"]) < 2e-3
    r = d.run("modal")
    assert abs(cl.peak(r["vf"])) < 0.05 * abs(cl.peak(r["vn"]))
    assert cl.peak(r["vn"]) == pytest.approx(0.5 * cf["kb"], rel=0.05)


@need_ngspice
@need_xyce
def test_engines_on_one_deck():
    # the modal deck, unchanged, in both engines; CPL against Xyce within 1%
    d, _ = matched_deck(INHOM, 0.3, 200e-12)
    ng, xy, cpl = d.run("modal"), d.run("modal", "xyce"), d.run("cpl")
    for p in ("vn", "vf"):
        assert cl.peak(ng[p]) == pytest.approx(cl.peak(xy[p]), rel=0.01)
        assert cl.peak(cpl[p]) == pytest.approx(cl.peak(xy[p]), rel=0.01)


@need_ngspice
def test_even_odd_decomposition():
    """Same termination on all four ends, so the modes don't mix: the pair is
    two uncoupled lines at Z_even, Z_odd driven by half the source each."""
    lc, length, t_r, r_end = INHOM, 0.1, 300e-12, 50.0
    L, C = np.array(lc["L"]), np.array(lc["C"])
    d, _ = matched_deck(lc, length, t_r, rload=r_end)
    pair = d.run("modal")
    d2 = cl.Deck()
    for name, z, td in (("e", math.sqrt((L[0, 0] + L[0, 1]) / (C[0, 0] + C[0, 1])),
                         length * math.sqrt((L[0, 0] + L[0, 1]) * (C[0, 0] + C[0, 1]))),
                        ("o", math.sqrt((L[0, 0] - L[0, 1]) / (C[0, 0] - C[0, 1])),
                         length * math.sqrt((L[0, 0] - L[0, 1]) * (C[0, 0] - C[0, 1])))):
        d2.ramp(f"s{name}", 0.5, 0.2e-9, t_r)
        d2.r(f"s{name}", f"near{name}", r_end)
        d2.line(f"near{name}", f"far{name}", z, td)
        d2.r(f"far{name}", 0, r_end)
    d2.probes = ["neare", "fare", "nearo", "faro"]
    d2.tstop, d2.tstep = d.tstop, d.tstep
    ev = d2.run("modal")
    for a, b, e, o in (("an", "vn", "neare", "nearo"), ("af", "vf", "fare", "faro")):
        t = pair["time"]
        ve = np.interp(t, ev["time"], ev[e])
        vo = np.interp(t, ev["time"], ev[o])
        assert np.abs(pair[a] - (ve + vo)).max() < 0.005
        assert np.abs(pair[b] - (ve - vo)).max() < 0.005


@need_ngspice
def test_cpl_disagreement_falls_back_to_modal(monkeypatch):
    ends = {"x": {"node": "v0", "receiver": True}}

    def fake(deck, ends, mode, engine, timeout=0):
        return {"x": 0.5 if mode == "cpl" else 0.1}
    monkeypatch.setattr(sim_si, "_peaks", fake)
    got = sim_si._run_engines(None, ends, "auto")
    assert got["peaks"]["x"] == 0.1 and "disagreed" in got["engines"]["used"]


@need_ngspice
def test_ladder_with_real_terminations():
    """A third way to the same waveform: series R, C load, unequal stubs."""
    d = cl.Deck()
    d.ramp("s", 3.3, 0.1e-9, 1e-9)
    d.r("s", "a0", 53.0)
    d.line("a0", "a1", 50.0, 60e-12)
    d.coupled("a1", "v1", "a2", "v2", INHOM, 0.08)
    d.line("a2", "a3", 50.0, 200e-12)
    d.r("a3", 0, 1e6)
    d.c("a3", 0, 3e-12)
    d.r("v0", 0, 20.0)
    d.line("v0", "v1", 50.0, 100e-12)
    d.line("v2", "v3", 50.0, 40e-12)
    d.r("v3", 0, 1e6)
    d.c("v3", 0, 5e-12)
    d.probes = ["v0", "v3"]
    d.tstop, d.tstep = 6e-9, 20e-12
    m, lad = d.run("modal"), d.run("ladder")
    for p in d.probes:
        assert cl.peak(m[p]) == pytest.approx(cl.peak(lad[p]), rel=0.02)


# ------------------------------------------------------------ on a board

def write_board(tmp_path, body):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal)
    (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 120 40) (stroke (width 0.1)) (fill no) (layer "Edge.Cuts"))
{PLANES}{body})
""", encoding="utf-8")
    return p


def two_lines(gap=0.15, w=0.2, v_x0=6.0, v_x1=94.0, extra=""):
    y = 10 + w + gap
    return (fp("U1", 4, 10, "MCU", [("1", 0, "/CLK_A")])
            + fp("U2", 96, 10, "RX", [("1", 0, "/CLK_A")])
            + seg(4, 10, 96, 10, "/CLK_A", w=w)
            + fp("U3", v_x0, y, "X", [("1", 0, "/V")])
            + fp("U4", v_x1, y, "Y", [("1", 0, "/V")])
            + seg(v_x0, y, v_x1, y, "/V", w=w) + extra)


def load(tmp_path, body):
    bg = geom.load_board(write_board(tmp_path, body))
    return bg, check_si.LineModel(bg)


SPECS = {"/CLK_A": {"t_rise_ns": 1.0, "driver": "U1", "swing_v": 3.3},
         "/V": {"driver": "U3"}}


@need_fields
def test_sections_from_the_copper(tmp_path):
    bg, lm = load(tmp_path, two_lines())
    pa = sim_si.NetPath(bg, lm, "/CLK_A", *sim_si.terminal_pads(bg, "/CLK_A"))
    pv = sim_si.NetPath(bg, lm, "/V", *sim_si.terminal_pads(bg, "/V"))
    assert pa.length == pytest.approx(92.0)
    assert pv.length == pytest.approx(88.0)
    secs = sim_si.coupled_sections(lm, pa, pv, 1e-9)
    assert len(secs) == 1
    assert secs[0]["gap"] == pytest.approx(0.15)
    # only where the victim really is abreast: not past its ends
    assert secs[0]["a1"] - secs[0]["a0"] == pytest.approx(88.0, abs=0.5)


@need_fields
def test_merge_averages_lc_by_length():
    x = {"L": [[1.0, 0.2], [0.2, 1.0]], "C": [[2.0, -0.2], [-0.2, 2.0]]}
    y = {"L": [[1.0, 0.4], [0.4, 1.0]], "C": [[2.0, -0.4], [-0.4, 2.0]]}
    m = sim_si._merge_lc(x, 3.0, y, 1.0)
    assert m["L"][0][1] == pytest.approx(0.25)
    assert m["C"][0][1] == pytest.approx(-0.25)


def test_a_short_isolated_run_keeps_its_mutual_totals():
    s = {"a0": 0.0, "a1": 1.0, "lc": {"L": [[1.0, 0.4], [0.4, 1.0]],
                                       "C": [[2.0, -0.4], [-0.4, 2.0]]}}
    sim_si._stretch_short(s, 4.0)
    assert s["model_mm"] == 4.0
    assert s["lc"]["L"][0][1] * 4.0 == pytest.approx(0.4)
    assert s["lc"]["L"][0][0] == 1.0


@need_ngspice
@need_fields
def test_matched_board_pair_matches_the_formula(tmp_path):
    """A series R making the source Z0, and a far-end parallel R = Z0, on both
    nets: the launched step is swing/2 and the near end sees kb of it."""
    bg, lm = load(tmp_path, "")
    z0 = lm.line("F.Cu", 0.2)["z0"]
    r = f"{z0:.1f}"
    body = (fp("U1", 2, 10, "MCU", [("1", 0, "/D")])
            + fp("R1", 4, 10, "0", [("1", -0.5, "/D"), ("2", 0.5, "/CLK_A")])
            + seg(4.5, 10, 96, 10, "/CLK_A")
            + fp("U2", 96, 10, "RX", [("1", 0, "/CLK_A")])
            + fp("R2", 96, 10, r, [("1", 0, "/CLK_A"), ("2", 1, "GND")])
            + seg(4.5, 10.75, 96, 10.75, "/V")
            + fp("U3", 4.5, 10.75, "X", [("1", 0, "/V")])
            + fp("U4", 96, 10.75, "Y", [("1", 0, "/V")])
            + fp("R3", 96, 10.75, r, [("1", 0, "/V"), ("2", 1, "GND")]))
    (tmp_path / "m").mkdir()
    bg, lm = load(tmp_path / "m", body)
    specs = {"/CLK_A": {"t_rise_ns": 0.2, "driver": "R1", "driver_ohm": z0, "swing_v": 1.0,
                        "c_in_pf": 0.001},
             "/V": {"driver": "U3", "driver_ohm": z0, "c_in_pf": 0.001}}
    got = sim_si.simulate_pair(bg, lm, "/CLK_A", "/V", specs, {}, {"GND"})
    lc = sim_si.section_lc(lm, "F.Cu", 0.2, 0.2, 0.55)
    cf = cl.closed_form(lc, 0.0915, 0.2e-9)
    assert got["coupled_mm"] == pytest.approx(91.5, abs=0.5)
    assert got["ends"]["U3.1"]["peak_mv"] / 1000 == pytest.approx(0.5 * cf["kb"], rel=0.05)
    far = [e["peak_mv"] for n, e in got["ends"].items() if n != "U3.1"]
    assert max(far, key=abs) / 1000 == pytest.approx(0.5 * cf["fext"], rel=0.05)


@need_ngspice
@need_fields
def test_series_r_divides_and_open_far_end_doubles(tmp_path):
    bg, lm = load(tmp_path, two_lines(gap=0.3))
    base = sim_si.simulate_pair(bg, lm, "/CLK_A", "/V", SPECS, {}, frozenset())
    # 80 ohm more at the driver: less launched, slower into the load
    more = dict(SPECS, **{"/CLK_A": dict(SPECS["/CLK_A"], driver_ohm=100.0)})
    damped = sim_si.simulate_pair(bg, lm, "/CLK_A", "/V", more, {}, frozenset())
    assert abs(damped["noise_v"]) < abs(base["noise_v"])
    # a high-Z receiver end sees more than the victim's driven end
    assert abs(base["ends"]["U4.1"]["peak_mv"]) > abs(base["ends"]["U3.1"]["peak_mv"])
    assert base["ends"]["U4.1"]["receiver"] and not base["ends"]["U3.1"]["receiver"]


@need_ngspice
@need_fields
def test_unknown_driver_takes_the_worst_orientation(tmp_path):
    bg, lm = load(tmp_path, two_lines(gap=0.3))
    known = sim_si.simulate_pair(bg, lm, "/CLK_A", "/V", SPECS, {}, frozenset())
    specs = {"/CLK_A": {"t_rise_ns": 1.0}, "/V": {}}
    worst = sim_si.simulate_pair(bg, lm, "/CLK_A", "/V", specs, {}, frozenset())
    assert worst["driver_assumed"] == {"/CLK_A": True, "/V": True}
    assert worst["noise_v"] >= known["noise_v"] - 1e-6


def test_a_tap_inside_a_section_moves_to_its_end():
    assert sim_si._clear_of(12.0, [(10.0, 30.0)]) == 10.0
    assert sim_si._clear_of(28.0, [(30.0, 10.0)]) == 30.0
    assert sim_si._clear_of(5.0, [(10.0, 30.0)]) == 5.0


@need_ngspice
@need_fields
def test_a_receiver_mid_section_sees_the_noise(tmp_path):
    # a third pad tapping the victim halfway along the coupled run
    body = two_lines(gap=0.3, extra=fp("U5", 50, 10.5, "Z", [("1", 0, "/V")]))
    bg, lm = load(tmp_path, body)
    got = sim_si.simulate_pair(bg, lm, "/CLK_A", "/V", SPECS, {}, frozenset())
    assert abs(got["ends"]["U5.1"]["peak_mv"]) > 1.0


@need_ngspice
@need_fields
def test_no_parallel_run_is_not_simulated(tmp_path):
    bg, lm = load(tmp_path, two_lines(gap=30.0))
    got = sim_si.simulate_pair(bg, lm, "/CLK_A", "/V", SPECS, {}, frozenset())
    assert "not_simulated" in got


@need_ngspice
@need_fields
def test_cli(tmp_path):
    p = write_board(tmp_path, two_lines())
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"si": {"nets": SPECS}}))
    out = tmp_path / "r.json"
    assert sim_si.main(["--pcb", str(p), "--xtalk", "--aggressor", "/CLK_A", "--victim", "/V",
                        "--constraints", str(c), "--out", str(out)]) == 0 \
        or json.loads(out.read_text())["violations"]
    res = json.loads(out.read_text())
    assert res["xtalk"]["engines"]["used"] == "ngspice cpl"
    assert res["violations"][0]["kind"] == "si_crosstalk_sim"


# ------------------------------------------------------------ check_si's hook

def check(tmp_path, body, si=None, extra=()):
    p = write_board(tmp_path, body)
    c = tmp_path / "constraints.json"
    c.write_text(json.dumps({"si": si or {}}))
    return check_si.run(["--pcb", str(p), "--constraints", str(c), *extra])[0]


@need_ngspice
@need_fields
def test_check_si_reports_estimate_vs_simulated(tmp_path):
    got = check(tmp_path, two_lines(), {"nets": {"/CLK_A": {"class": "clock", "driver": "U1"}}})
    pairs = got["crosstalk_sim"]["pairs"]
    assert pairs[0]["aggressor"] == "/CLK_A" and pairs[0]["victim"] == "/V"
    assert pairs[0]["estimate_mv"] > 0 and pairs[0]["noise_mv"] > 0
    assert pairs[0]["sim_over_estimate"] == pytest.approx(
        pairs[0]["noise_mv"] / pairs[0]["estimate_mv"], rel=1e-2)
    assert [v for v in got["violations"] if v["kind"] == "si_crosstalk_sim"]


@need_fields
def test_check_si_says_when_the_solver_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(solvers, "tool_path", lambda *a, **k: None)
    got = check(tmp_path, two_lines(), {"nets": {"/CLK_A": {"class": "clock", "driver": "U1"}}})
    assert "not found" in got["crosstalk_sim"]["not_run"]
    assert got["crosstalk_sim"]["pairs_waiting"]
    assert not [v for v in got["violations"] if v["kind"] == "si_crosstalk_sim"]


@need_fields
def test_check_si_sim_off(tmp_path):
    got = check(tmp_path, two_lines(), {"nets": {"/CLK_A": {"class": "clock"}}},
                ("--xtalk-sim", "0"))
    assert got["crosstalk_sim"] == {"not_run": "off"}


@need_ngspice
@need_fields
def test_check_si_retries_a_timed_out_deck(tmp_path, monkeypatch):
    """#1377: a deck that runs past the base limit on a loaded machine gets one
    retry at DECK_RETRY_S, and the simulated finding survives."""
    real, seen = sim_si._peaks, []

    def slow_first(deck, ends, mode, engine, timeout=sim_si.DECK_TIMEOUT_S):
        seen.append(timeout)
        if timeout < sim_si.DECK_RETRY_S:
            raise solvers.SolverTimeout(f"{engine} ran past {timeout:.0f} s")
        return real(deck, ends, mode, engine, timeout)

    monkeypatch.setattr(sim_si, "_peaks", slow_first)
    got = check(tmp_path, two_lines(), {"nets": {"/CLK_A": {"class": "clock", "driver": "U1"}}})
    pair, = got["crosstalk_sim"]["pairs"]
    assert "ran past" in pair["retried"] and pair["noise_mv"] > 0
    assert seen[0] == sim_si.DECK_TIMEOUT_S and sim_si.DECK_RETRY_S in seen
    assert [v for v in got["violations"] if v["kind"] == "si_crosstalk_sim"]
    assert not [n for n in got.get("not_judged", []) if "crosstalk not simulated" in n]


@need_fields
def test_check_si_names_a_pair_it_could_not_simulate(tmp_path, monkeypatch):
    """#1377, the quiet twin: a deck that runs past the retry too is no
    finding, and the report says so in not_judged instead of dropping it."""
    monkeypatch.setattr(sim_si, "available", lambda *a, **k: None)

    def always_slow(deck, ends, mode, engine, timeout=sim_si.DECK_TIMEOUT_S):
        raise solvers.SolverTimeout(f"{engine} ran past {timeout:.0f} s")

    monkeypatch.setattr(sim_si, "_peaks", always_slow)
    got = check(tmp_path, two_lines(), {"nets": {"/CLK_A": {"class": "clock", "driver": "U1"}}})
    pair, = got["crosstalk_sim"]["pairs"]
    assert "ran past 120 s" in pair["not_simulated"] and "ran past 30 s" in pair["retried"]
    assert not [v for v in got["violations"] if v["kind"] == "si_crosstalk_sim"]
    nj, = [n for n in got["not_judged"] if "crosstalk not simulated" in n]
    assert nj.startswith("/CLK_A -> /V:") and "ran past 120 s" in nj


# ------------------------------------------------------------ planted, pinned

PINNED = REPO / "pinned" / "lite-main" / "kicad" / "lite-main.kicad_pcb"


def plant(tmp_path, offset):
    """lite-main with a new analog victim beside /I2S_BCLK's 31 mm B.Cu run."""
    text = PINNED.read_text(encoding="utf-8")
    bg = geom.load_board(PINNED)
    run = max((t for t in bg.tracks_of("/I2S_BCLK") if t.layer == "B.Cu"), key=lambda t: t.length)
    (x0, y0), (x1, y1) = run.shape.coords[0], run.shape.coords[-1]
    nx, ny = -(y1 - y0) / run.length, (x1 - x0) / run.length
    off = offset + run.width
    a = (x0 + nx * off, y0 + ny * off)
    b = (x1 + nx * off, y1 + ny * off)
    net = "/AUDIO_TAP"
    item = (f'(segment (start {a[0]:.3f} {a[1]:.3f}) (end {b[0]:.3f} {b[1]:.3f}) (width 0.2) '
            f'(layer "B.Cu") (net "{net}"))\n')
    pads = "".join(
        f'(footprint "t:{r}" (at {x:.3f} {y:.3f}) (layer "B.Cu")\n'
        f'  (property "Reference" "{r}" (at 0 0 0) (layer "B.SilkS"))\n'
        f'  (property "Value" "X" (at 0 0 0) (layer "B.Fab"))\n'
        f'  (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "B.Cu") (net "{net}")))\n'
        for r, (x, y) in (("TPA1", a), ("TPA2", b)))
    i = text.rstrip().rfind(")")
    m = re.search(r'\(net (\d+) "', text)
    decl = ""
    if m:
        top = max(int(n) for n in re.findall(r'\(net (\d+) "', text))
        decl = f'(net {top + 1} "{net}")\n'
        item = item.replace(f'(net "{net}")', f"(net {top + 1})")
        pads = pads.replace(f'(net "{net}")', f'(net {top + 1} "{net}")')
    out = tmp_path / "planted.kicad_pcb"
    out.write_text(text[:i] + decl + pads + item + text[i:], encoding="utf-8")
    names = sorted(n for n in bg.nets if n and n != "/I2S_BCLK")
    return out, names


@need_ngspice
@need_fields
def test_planted_victim_beside_a_fast_clock(tmp_path):
    """A victim moved beside a fast aggressor must flag; moved out, clean."""
    def run(offset):
        d = tmp_path / str(offset)
        d.mkdir()
        p, names = plant(d, offset)
        c = d / "c.json"
        c.write_text(json.dumps({"si": {"ignore": names, "nets": {
            "/I2S_BCLK": {"class": "i2s", "driver": "U5"},
            "/AUDIO_TAP": {"driver": "TPA1", "victim": "analog"}}}}))
        return check_si.run(["--pcb", str(p), "--constraints", str(c)])[0]
    near = run(0.3)
    hit = [v for v in near["violations"]
           if v["kind"] == "si_crosstalk_sim" and v["net"] == "/AUDIO_TAP"]
    assert hit and hit[0]["aggressor"] == "/I2S_BCLK"
    far = run(25.0)
    assert not [v for v in far["violations"] if v["kind"].startswith("si_crosstalk")
                and v["net"] == "/AUDIO_TAP"]


def test_pair_target_takes_stats_from_coupling_stats_tuple(monkeypatch):
    """coupling_stats returns (unc_p, unc_n, stats): a routed pair must not crash check_si."""
    import check_diffpair
    import check_si

    seen = {}

    def impedance(bg, p, n, gap):
        seen["gap"] = gap
        return {"zdiff_ohm": 90.0}

    monkeypatch.setattr(check_diffpair, "coupling_stats",
                        lambda bg, p, n, m, breakout=None: ([], [], {"nominal_gap_mm": 0.2}))
    monkeypatch.setattr(check_diffpair, "pair_impedance", impedance)
    assert check_si.pair_target(None, "P", "N", {"zdiff": 90}, {}, [0, 0]) == []
    assert seen["gap"] == 0.2
