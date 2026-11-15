"""hwde's power-path checks: check_power_path (current through each
pad-track-via chain), lib/soa + soa_check (a switch's turn-on SOA as a
junction temperature), lib/pdsource + sim_inrush (load switches against a
USB PD source) and lib/mlcc + mlcc_dcbias (MLCC DC bias).

Each was done by hand in hifi-receiver-2's first-spin power review
(docs/review-2/power.md, POWER-1..11); the worked numbers here are that
review's, so a change to a model that moves them away from what the
reviewer found shows up. The sim_inrush engine test runs wherever the
KiCad-bundled ngspice is (bin/hwde test exports it); it skips elsewhere.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import check_power_path  # noqa: E402
import geom  # noqa: E402
import mlcc  # noqa: E402
import pdsource  # noqa: E402
import sim_inrush  # noqa: E402
import soa  # noqa: E402
from lib import env  # noqa: E402


def _cli(script: str, *args) -> tuple[int, dict]:
    cp = subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                        capture_output=True, text=True, timeout=600)
    return cp.returncode, json.loads(cp.stdout)


# ------------------------------------------------------------------ mlcc

@pytest.mark.parametrize("c,pkg,rated,vdc,lo,hi", [
    # review-2 readings: 10u/10V 0603 at 5.2 V ~ 50 %; 2 x 22u/25V 0805 at
    # 5.2 V ~ 22 uF; 10u/50V 1206 at 28 V ~ 3-4 uF; 1u/10V 0402 3.3 V ~ 70 %
    (10e-6, "0603", 10, 5.2, 0.30, 0.65),
    (22e-6, "0805", 25, 5.2, 0.40, 0.70),
    (10e-6, "1206", 50, 28.0, 0.20, 0.45),
    (1e-6, "0402", 10, 3.3, 0.60, 0.90)])
def test_mlcc_generic_curve_matches_review(c, pkg, rated, vdc, lo, hi):
    r = mlcc.c_eff(c, vdc, footprint=f"C_{pkg}", value="", rated_v=rated,
                   table=[])
    assert r["curve"] == "generic" and r["kind"] == "class2"
    assert lo < r["fraction"] < hi
    assert r["c_eff_low"] < r["c_eff"] < c


def test_mlcc_kinds_temperature_and_table():
    assert mlcc.classify("100pF", "C_0402")["kind"] == "class1"
    assert mlcc.classify("10uF X7R", "C_0805")["dielectric"] == "X7R"
    assert mlcc.classify("470uF", "CP_Elec_8x10")["kind"] == "electrolytic"
    assert mlcc.c_eff(470e-6, 20, footprint="CP_Elec_8x10")["c_eff"] == 470e-6
    assert mlcc.classify("1uF", "C_0603")["assumed"]
    hot = mlcc.c_eff(10e-6, 0, footprint="C_0805", text="X5R", temp_c=85,
                     table=[])
    assert hot["fraction"] == pytest.approx(0.85)
    row = {"match": "^GRM21BR61E226", "curve": [[0, 1.0], [5, 0.5],
                                                 [10, 0.3]], "source": "t"}
    r = mlcc.c_eff(22e-6, 7.5, footprint="C_0805", mpn="GRM21BR61E226ME44",
                   table=[row])
    assert r["fraction"] == pytest.approx(0.4) and r["curve"].startswith(
        "table")


# ------------------------------------------------------------------- soa

def test_soa_zth_and_hotswap_energy():
    row = soa.find("AO4485")
    assert row and not row["generic"]
    assert soa.zth(row, 1e-3) == pytest.approx(2.5)
    assert soa.zth(row, 1e-2) == pytest.approx(6.25)
    assert soa.zth(row, 0.25e-3) == pytest.approx(1.25)       # sqrt(t)
    assert soa.soa_power(row, 10e-3) == pytest.approx(20.0)
    ts, ps = soa.hotswap_profile(940e-6, 28, t_ramp=10e-3)
    res = soa.junction(row, ts, ps)
    assert res["energy_j"] == pytest.approx(0.5 * 940e-6 * 28 ** 2, rel=1e-3)
    assert soa.find("", "XYZ123", "Package_SO:SOIC-8")["generic"]
    assert soa.find("", "XYZ123", "") is None


@pytest.mark.parametrize("c,v,ramp,sev", [
    (940e-6, 28, 10e-3, "error"),     # POWER-2: core, 940 uF in ~10 ms
    (690e-6, 20, 5.75e-3, "warning"),  # POWER-4: Lite, marginal
    (940e-6, 28, 60e-3, None),        # the review's core fix: ~60 ms
    (690e-6, 20, 35e-3, None)])       # the review's Lite fix: ~28 ms 10-90
def test_soa_review_cases(c, v, ramp, sev):
    row = soa.find("AO4485")
    ts, ps = soa.hotswap_profile(c, v, t_ramp=ramp)
    got, _why = soa.judge(row, soa.junction(row, ts, ps))
    assert got == sev


def test_soa_check_cli(tmp_path):
    rc, rep = _cli("soa_check.py", "--fet", "AO4485", "--hotswap", "--c",
                   "940u", "--v", "28", "--t-ramp", "10m")
    assert rc == 1
    (v,) = rep["violations"]
    assert v["kind"] == "fet_soa" and v["severity"] == "error"
    csv = tmp_path / "p.csv"
    csv.write_text("t,vds,id\n0,20,0.1\n0.001,10,0.1\n0.002,0,0.1\n",
                   encoding="utf-8")
    rc, rep = _cli("soa_check.py", "--fet", "AO4485", "--csv", str(csv))
    assert rc == 0 and rep["result"]["energy_j"] == pytest.approx(0.002,
                                                                   rel=0.01)
    rc, rep = _cli("soa_check.py", "--fet", "AO4485", "--at", "10m")
    assert rep["soa_power_w"] == pytest.approx(20.0)


# -------------------------------------------------------------- pdsource

def test_pd_scenarios_are_well_formed():
    scs = pdsource.scenarios(20.0)
    names = [s["name"] for s in scs]
    assert names == ["attach", "step_0.5", "step_3", "step_30", "fw_on",
                     "hard_reset"]
    for sc in scs:
        for key in ("src", "fw", "pg", "discharge"):
            pts = pdsource._edges(sc[key])
            assert all(b[0] > a[0] for a, b in zip(pts, pts[1:])), (sc, key)
        assert sc["src"][-1][0] <= sc["tstop"]
    step = scs[3]
    t0, t1 = step["src"][-2][0], step["src"][-1][0]
    assert (20.0 - 5.0) / ((t1 - t0) * 1e3) == pytest.approx(30.0)
    lines = pdsource.source_lines("vbus", step)
    assert any(ln.startswith("DPD ") for ln in lines)   # cannot sink


# ------------------------------------------------------ check_power_path

def _board(tmp_path, body: str) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 30 10) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
  (footprint "t:J" (at 2 5) (layer "F.Cu")
    (property "Reference" "J1" (at 0 0 0)) (property "Value" "USB-C" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu") (net "VBUS")))
  (footprint "t:Q" (at 28 5) (layer "F.Cu")
    (property "Reference" "Q1" (at 0 0 0)) (property "Value" "AO4485" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu") (net "VBUS")))
  (footprint "t:C" (at 15 8) (layer "F.Cu")
    (property "Reference" "C1" (at 0 0 0)) (property "Value" "10u" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "VBUS")))
{body})
""", encoding="utf-8")
    return p


# J1 -> 0.25 mm F.Cu -> two vias -> 2 mm B.Cu -> two vias -> 3 mm F.Cu -> Q1
CHAIN = """  (segment (start 2 5) (end 10 5) (width 0.25) (layer "F.Cu") (net "VBUS"))
  (segment (start 2 5) (end 10 5.6) (width 0.25) (layer "F.Cu") (net "VBUS"))
  (via (at 10 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VBUS"))
  (via (at 10 5.6) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VBUS"))
  (segment (start 10 5) (end 20 5) (width 2) (layer "B.Cu") (net "VBUS"))
  (segment (start 10 5.6) (end 10 5) (width 2) (layer "B.Cu") (net "VBUS"))
  (via (at 20 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VBUS"))
  (segment (start 20 5) (end 28 5) (width 3) (layer "F.Cu") (net "VBUS"))
"""


def test_power_path_bottleneck_is_the_min_cut(tmp_path):
    bg = geom.BoardGeom.from_file(_board(tmp_path, CHAIN))
    r = check_power_path.path_flow(bg, "VBUS", "J1", "Q1", 10, None, 0.02)
    one_via = check_power_path.via_amps(0.3, 0.02, 10)
    two_tracks = 2 * check_power_path.amps_for_width(0.25, 0.035, 10)
    # the two 0.25 mm tracks (~1 A) are narrower than one 0.3 mm via
    assert r["amps"] == pytest.approx(min(two_tracks, one_via), rel=1e-6)
    assert one_via == pytest.approx(1.06, abs=0.05)
    # amps_for_width inverts check_current's required width
    import check_current
    for i in (0.4, 1.0, 3.0, 8.0):
        w = check_current.required_width_mm(i, 10, 0.035)
        assert check_power_path.amps_for_width(w, 0.035, 10) == \
            pytest.approx(i, rel=1e-6)


def test_power_path_cli_judges_budget_and_paths(tmp_path):
    pcb = _board(tmp_path, CHAIN)
    cons = tmp_path / "constraints.json"
    cons.write_text(json.dumps({"power": [{"net": "VBUS", "current_a": 0.5}],
                                "power_paths": []}), encoding="utf-8")
    rc, rep = _cli("check_power_path.py", "--pcb", str(pcb),
                   "--constraints", str(cons))
    # the budget path Q1 -> J1 (a transistor is the likelier source; C1 is
    # not a power part) passes 0.5 A
    assert rc == 0, rep
    assert [(p["from"], p["to"]) for p in rep["paths"]] == [("Q1", "J1")]
    rc, rep = _cli("check_power_path.py", "--pcb", str(pcb),
                   "--path", "VBUS:J1:Q1:5")
    assert rc == 1
    (v,) = rep["violations"]
    assert v["kind"] == "path_capacity" and v["severity"] == "error"
    assert {t["width_mm"] for t in v["neck_tracks"]} == {0.25}


def _fp(ref, val, x, y, *pads):
    rows = "".join(f'    (pad "{n}" smd rect (at {dx} 0) (size 2 2) (layers "F.Cu") '
                   f'(net "{net}"))\n' for n, dx, net in pads)
    return (f'  (footprint "t:X" (at {x} {y}) (layer "F.Cu")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0)) '
            f'(property "Value" "{val}" (at 0 0 0))\n{rows}  )\n')


def _track(net, x0, x1, w):
    return (f'  (segment (start {x0} 5) (end {x1} 5) (width {w}) '
            f'(layer "F.Cu") (net "{net}"))\n')


def _series_board(tmp_path, extra="", neck=0.2):
    # J2 =wide= R1 (2m) =neck= L1 (the list only names the J2 -> R1 side)
    p = tmp_path / "s.kicad_pcb"
    p.write_text(f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 60 20) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{_fp("J2", "XT60", 4, 5, ("1", 0, "OUT"))}
{_fp("R1", "2m", 20, 5, ("1", -2, "OUT"), ("2", 2, "IL"))}
{_fp("L1", "4.7uH", 34, 5, ("1", -2, "IL"), ("2", 2, "SW"))}
{_track("OUT", 4, 18, 14)}{_track("IL", 22, 32, neck)}
{extra})
""", encoding="utf-8")
    return p


def _plan(pcb, paths=None, power=None):
    bg = geom.BoardGeom.from_file(pcb)
    cons = {"power_paths": paths or [{"net": "OUT", "from": "J2", "to": "R1",
                                      "current_a": 12.5}],
            "power": power or []}
    return bg, check_power_path.planned_paths(bg, cons, [], 30.0)


def test_declared_current_carries_on_through_a_series_part(tmp_path):
    """hc-power: "+12V from R104", and IL_A from L10 to R104 was 0.2 mm at 12.5 A."""
    pcb = _series_board(tmp_path)
    _, plan = _plan(pcb)
    hop = [p for p in plan if p["origin"].startswith("series")]
    assert [(p["net"], p["from"], p["to"], p["current_a"]) for p in hop] == [
        ("IL", "R1", "L1", 12.5)]
    assert not hop[0]["derived"]
    cons = pcb.parent / "c.json"
    cons.write_text(json.dumps({"power_paths": [
        {"net": "OUT", "from": "J2", "to": "R1", "current_a": 12.5, "dt_c": 30}]}))
    rc, rep = _cli("check_power_path.py", "--pcb", str(pcb), "--constraints", str(cons))
    assert rc == 1, rep
    (v,) = [v for v in rep["violations"] if v["net"] == "IL"]
    assert v["severity"] == "error" and {t["width_mm"] for t in v["neck_tracks"]} == {0.2}
    rc, rep = _cli("check_power_path.py", "--pcb", str(pcb), "--constraints", str(cons),
                   "--no-series")
    assert rc == 0, rep


def test_a_sense_tap_does_not_end_the_walk(tmp_path):
    tap = _fp("R2", "0R", 26, 12, ("1", -2, "IL"), ("2", 2, "IL_K")) \
        + _fp("U1", "INA240", 34, 12, ("1", 0, "IL_K"))
    _, plan = _plan(_series_board(tmp_path, tap))
    assert [(p["net"], p["to"]) for p in plan if p["origin"].startswith("series")] \
        == [("IL", "L1")]


def test_fan_out_stops_the_walk_and_a_twin_shares_the_current(tmp_path):
    fan = _fp("L2", "4.7uH", 26, 12, ("1", -2, "IL"), ("2", 2, "SW2"))
    _, plan = _plan(_series_board(tmp_path, fan))
    assert not [p for p in plan if p["origin"].startswith("series")]
    twin = _fp("R3", "2m", 20, 12, ("1", -2, "OUT"), ("2", 2, "IL"))
    _, plan = _plan(_series_board(tmp_path, twin))
    assert [(p["net"], p["current_a"]) for p in plan
            if p["origin"].startswith("series")] == [("IL", 6.25)]


def test_a_rail_budget_does_not_carry_on(tmp_path):
    """A budget is the whole rail's, not what one branch passes."""
    bg = geom.BoardGeom.from_file(_series_board(tmp_path))
    plan = check_power_path.planned_paths(
        bg, {"power": [{"net": "OUT", "current_a": 12.5}]}, [], 30.0)
    assert plan and not [p for p in plan if p["origin"].startswith("series")]


def test_power_path_via_on_a_tracks_body_joins_it(tmp_path):
    # J1 -> F.Cu A -> two vias on A's and B's bodies -> B.Cu B -> one via
    # on B's and C's bodies, off their centrelines -> F.Cu C -> Q1;
    # no via sits at a track's end
    body = """  (segment (start 2 5) (end 14 5) (width 2) (layer "F.Cu") (net "VBUS"))
  (segment (start 10 5) (end 20 5) (width 2) (layer "B.Cu") (net "VBUS"))
  (segment (start 17 5) (end 28 5) (width 2) (layer "F.Cu") (net "VBUS"))
  (via (at 12 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VBUS"))
  (via (at 13 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VBUS"))
  (via (at 18 5.6) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VBUS"))
"""
    bg = geom.BoardGeom.from_file(_board(tmp_path, body))
    r = check_power_path.path_flow(bg, "VBUS", "J1", "Q1", 10, 0.5, 0.02)
    assert r["amps"] == pytest.approx(0.5)  # the lone via is the cut; the pair adds up
    assert [v["at"] for v in r["neck_vias"]] == [[18, 5.6]]
    # a via whose pad only grazes a track's edge is not joined to it
    grazing = body.replace("(at 18 5.6)", "(at 18 6.2)")
    bg = geom.BoardGeom.from_file(_board(tmp_path, grazing))
    r = check_power_path.path_flow(bg, "VBUS", "J1", "Q1", 10, 0.5, 0.02)
    assert r["amps"] == 0.0


def test_power_path_vias_and_overlapping_pours(tmp_path):
    # J1 -> F.Cu pour -> 1 via -> two overlapping B.Cu pours -> via -> Q1
    body = """  (zone (net "VBUS") (layer "F.Cu")
    (polygon (pts (xy 1 4) (xy 8 4) (xy 8 6) (xy 1 6)))
    (filled_polygon (layer "F.Cu") (pts (xy 1 4) (xy 8 4) (xy 8 6) (xy 1 6))))
  (zone (net "VBUS") (layer "B.Cu")
    (polygon (pts (xy 6 4) (xy 16 4) (xy 16 6) (xy 6 6)))
    (filled_polygon (layer "B.Cu") (pts (xy 6 4) (xy 16 4) (xy 16 6) (xy 6 6))))
  (zone (net "VBUS") (layer "B.Cu")
    (polygon (pts (xy 15 4) (xy 25 4) (xy 25 6) (xy 15 6)))
    (filled_polygon (layer "B.Cu") (pts (xy 15 4) (xy 25 4) (xy 25 6) (xy 15 6))))
  (via (at 7 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VBUS"))
  (via (at 24 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VBUS"))
  (segment (start 24 5) (end 28 5) (width 3) (layer "F.Cu") (net "VBUS"))
"""
    bg = geom.BoardGeom.from_file(_board(tmp_path, body))
    r = check_power_path.path_flow(bg, "VBUS", "J1", "Q1", 10, 2.0, 0.02)
    assert r["amps"] == pytest.approx(2.0)          # one via per transition
    assert len(r["neck_vias"]) == 1
    r = check_power_path.path_flow(bg, "VBUS", "J1", "C1", 10, 2.0, 0.02)
    assert r["amps"] == 0.0                         # C1's pad is off the pours


# ------------------------------------------------------------ sim_inrush

def write_net(path: Path, board) -> Path:
    comps, nets = [], {}
    for ref, value, fp, pins in board:
        comps.append(f'(comp (ref "{ref}") (value "{value}") '
                     f'(footprint "{fp}"))')
        for pin, (net, fn) in pins.items():
            nets.setdefault(net, []).append(
                f'(node (ref "{ref}") (pin "{pin}") (pinfunction "{fn}") '
                f'(pintype "passive"))')
    body = "\n".join(f'(net (code "{i}") (name "{n}") {" ".join(nd)})'
                     for i, (n, nd) in enumerate(sorted(nets.items()), 1))
    path.write_text(f'(export (version "E") (components {" ".join(comps)}) '
                    f'(nets {body}))\n', encoding="utf-8")
    return path


# The Lite PVDD switch as built (POWER-3/4): AO4485 with its gate pulled to
# VBUS by 100k, a 100n Miller cap gate -> PVDD (through 1k), firmware
# pulling the gate down through 47k and an NPN, 470u + 220u of bulk.
LITE_SWITCH = [
    ("J1", "USB-C", "USB_C_Receptacle", {"1": ("VBUS", "VBUS"),
                                         "2": ("GND", "GND")}),
    ("C1", "10uF 50V X5R", "C_1206_3216Metric", {"1": ("VBUS", ""),
                                                 "2": ("GND", "")}),
    ("Q1", "AO4485", "SOIC-8", {"1": ("VBUS", "S"), "2": ("/PSW_G", "G"),
                                "3": ("PVDD", "D")}),
    ("R5", "100k", "R_0402", {"1": ("VBUS", ""), "2": ("/PSW_G", "")}),
    ("R15", "1k", "R_0402", {"1": ("/PSW_G", ""), "2": ("/MIL", "")}),
    ("C2", "100nF 50V X7R", "C_0603", {"1": ("/MIL", ""), "2": ("PVDD", "")}),
    ("R6", "47k", "R_0402", {"1": ("/PSW_G", ""), "2": ("/PULL", "")}),
    ("Q4", "MMBT3904", "SOT-23", {"1": ("/B", "B"), "2": ("GND", "E"),
                                  "3": ("/PULL", "C")}),
    ("R7", "10k", "R_0402", {"1": ("/EN", ""), "2": ("/B", "")}),
    ("R8", "100k", "R_0402", {"1": ("/EN", ""), "2": ("GND", "")}),
    ("U2", "MCU", "QFN-32", {"1": ("+3V3", "VDD"), "2": ("GND", "GND"),
                             "3": ("/EN", "IO5")}),
    ("C58", "470uF", "CP_Elec_10x10", {"1": ("PVDD", ""), "2": ("GND", "")}),
    ("C59", "220uF", "CP_Elec_8x10", {"1": ("PVDD", ""), "2": ("GND", "")}),
    ("R16", "22k", "R_0402", {"1": ("PVDD", ""), "2": ("GND", "")}),
]


def test_inrush_finds_the_switch_and_entry(tmp_path):
    import sim_board
    net = write_net(tmp_path / "b.net", LITE_SWITCH)
    board = sim_board.load_board(net)
    rails = sim_board.resolve_rails(board, {}, None, None,
                                    {"VBUS": 20.0})
    model = sim_board.build_model(board, {}, rails)
    sws = sim_inrush.find_switches(board, {}, rails, model, soa.load())
    assert [(s["ref"], s["in"], s["out"]) for s in sws] == \
        [("Q1", "VBUS", "PVDD")]
    assert sws[0]["bulk_f"] == pytest.approx(690e-6)
    assert sim_inrush.entry_net(board, model, sws, None) == "VBUS"


@pytest.fixture(scope="module")
def inrush(tmp_path_factory):
    if env.find_ngspice_dll() is None:
        pytest.skip("no ngspice shared library (KiCad bundle or "
                    "HWDE_NGSPICE_DLL)")
    d = tmp_path_factory.mktemp("inrush")
    net = write_net(d / "b.net", LITE_SWITCH)
    out = d / "r.json"
    cp = subprocess.run([sys.executable, str(SCRIPTS / "sim_inrush.py"),
                         "--net", str(net), "--rail", "VBUS=20",
                         "--slews", "3,30", "--window-ms", "150",
                         "--out", str(out)],
                        capture_output=True, text=True, timeout=900)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def _scenario(rep, name):
    return next(s for s in rep["scenarios"] if s["scenario"] == name)


def test_inrush_reproduces_the_lite_review(inrush):
    # POWER-3: the Miller cap turns Q1 on at the 30 V/ms step, firmware off:
    # ~20 A from the source (the review's bench: 20.6 A, PVDD 15.6 V)
    st = _scenario(inrush, "step_30")
    assert 12 < st["src_peak_a"] < 30
    assert st["switches"]["Q1"]["out_peak_v"] > 10
    kinds = {(v["kind"], v["scenario"], v["severity"])
             for v in inrush["violations"]}
    assert ("pd_overcurrent", "step_30", "error") in kinds
    assert ("switch_on_fw_off", "step_30", "error") in kinds
    # POWER-4: firmware on, 690 uF in ~5 ms: ~0.14 J, Tj near or over max
    on = _scenario(inrush, "fw_on")["switches"]["Q1"]
    assert 0.10 < on["energy_j"] < 0.20
    assert on["tj_peak_c"] > 125
    assert 2e-3 < on["rise_10_90_s"] < 10e-3
    assert any(v["kind"] == "fet_soa" and v["scenario"] == "fw_on"
               for v in inrush["violations"])
    # Vth corners of the fitted model run on the fastest step
    assert {s["scenario"] for s in inrush["scenarios"]} >= {
        "step_30_Q1_vth1.7", "step_30_Q1_vth2.5"}


def test_inrush_review_fix_passes(tmp_path):
    """The review's Lite fix (R6 220k, 2.2u gate-source hold-off) clears
    the step and keeps turn-on inside the SOA."""
    if env.find_ngspice_dll() is None:
        pytest.skip("no ngspice shared library")
    board = [r for r in LITE_SWITCH if r[0] != "R6"] + [
        ("R6", "220k", "R_0402", {"1": ("/PSW_G", ""), "2": ("/PULL", "")}),
        ("C60", "2.2uF 25V X5R", "C_0603", {"1": ("VBUS", ""),
                                            "2": ("/PSW_G", "")})]
    net = write_net(tmp_path / "b.net", board)
    out = tmp_path / "r.json"
    subprocess.run([sys.executable, str(SCRIPTS / "sim_inrush.py"), "--net",
                    str(net), "--rail", "VBUS=20", "--slews", "30",
                    "--window-ms", "250", "--out", str(out)],
                   capture_output=True, text=True, timeout=900)
    rep = json.loads(out.read_text(encoding="utf-8"))
    bad = [v for v in rep["violations"] if v["severity"] == "error"
           and v["scenario"] in ("step_30", "fw_on")]
    assert bad == [], bad
    assert _scenario(rep, "step_30")["src_peak_a"] < 3.0


# ------------------------------------------------------------ hot-plug

# bms-4s's charger entry (#430): a USB-C VBUS straight onto a BQ25798's
# VBUS/VAC pins with ceramics only and the data sheet's 2 R + 2.2 uF RC -
# no load switch, so the switch scenarios have nothing to judge.
CERAMIC_ENTRY = [
    ("J1", "USB-C", "USB_C_Receptacle", {"1": ("VBUS", "VBUS"),
                                         "2": ("GND", "GND")}),
    ("C1", "10uF 50V X5R", "C_1206_3216Metric", {"1": ("VBUS", ""),
                                                 "2": ("GND", "")}),
    ("C2", "10uF 50V X5R", "C_1206_3216Metric", {"1": ("VBUS", ""),
                                                 "2": ("GND", "")}),
    ("C3", "1uF 50V X5R", "C_0603_1608Metric", {"1": ("VBUS", ""),
                                                "2": ("GND", "")}),
    ("R1", "2", "R_0603", {"1": ("VBUS", ""), "2": ("/SNUB", "")}),
    ("C4", "2.2uF 50V X5R", "C_0805_2012Metric", {"1": ("/SNUB", ""),
                                                  "2": ("GND", "")}),
    ("U1", "BQ25798", "QFN-29", {"2": ("VBUS", "VBUS"), "8": ("VBUS", "VAC2"),
                                 "9": ("VBUS", "VAC1"), "20": ("GND", "GND"),
                                 "11": ("/STAT", "STAT")}),
    ("U2", "XYZ123", "SOT-23-6", {"1": ("VBUS", "VIN"), "2": ("GND", "GND")}),
]


def test_tvs_card_has_its_clamp_resistance(tmp_path):
    import sim_board
    board = sim_board.load_board(write_net(tmp_path / "t.net", [
        ("D1", "SMAJ24A", "D_SMA", {"1": ("VBUS", "K"), "2": ("GND", "A")})]))
    e = {"ref": "D1", "bv": 24 * 1.11}
    card = sim_inrush.tvs_card(e, board, {})
    # SMAJ24A: Vc 38.9 V at Ipp 10.3 A, Vbr 26.7 V: RS ~1.2 ohm
    rs = float(card.split("RS=")[1].rstrip(")"))
    assert 1.0 < rs < 1.4
    assert sim_inrush.tvs_card({"ref": "D1", "bv": None}, board, {}) is None


def test_pin_abs_max_from_the_table(tmp_path):
    import sim_board
    board = sim_board.load_board(write_net(tmp_path / "b.net",
                                           CERAMIC_ENTRY))
    rated, unrated = sim_inrush.pin_abs_max(board, {}, None, {"VBUS"})
    assert {(r["ref"], r["fn"], r["max_v"]) for r in rated} == {
        ("U1", "VBUS", 30.0), ("U1", "VAC1", 30.0), ("U1", "VAC2", 30.0)}
    assert [(u["ref"], u["fn"]) for u in unrated] == [("U2", "VIN")]


def _hotplug(tmp_path, board):
    if env.find_ngspice_dll() is None:
        pytest.skip("no ngspice shared library")
    net = write_net(tmp_path / "b.net", board)
    out = tmp_path / "r.json"
    subprocess.run([sys.executable, str(SCRIPTS / "sim_inrush.py"), "--net",
                    str(net), "--rail", "VBUS=20", "--out", str(out)],
                   capture_output=True, text=True, timeout=900)
    return json.loads(out.read_text(encoding="utf-8"))


def test_hotplug_rings_a_ceramic_entry_over_abs_max(tmp_path):
    rep = _hotplug(tmp_path, CERAMIC_ENTRY)
    assert rep["switches"] == []
    hits = [v for v in rep["violations"] if v["kind"] == "hotplug_overvoltage"]
    assert [(v["refs"], v["net"]) for v in hits] == [(["U1"], "VBUS")]
    assert all(v["severity"] == "error" for v in hits)
    # ad09d's hand deck: 2 R + 2.2 uF rings 20 V to 32-33 V
    peak = max(r["peak_v"]["VBUS"] for r in rep["hotplug"]["runs"])
    assert 30 < peak < 38
    assert any(k["ref"] == "U2" and k["what"] == "pin abs max"
               for k in rep["known_unknowns"])


def test_hotplug_damped_entry_passes(tmp_path):
    # bms-4s as built: 0.5 R + 47 uF polymer across VBUS and an SMAJ24A
    board = [r for r in CERAMIC_ENTRY if r[0] not in ("R1", "C4")] + [
        ("R1", "0.5", "R_2512", {"1": ("VBUS", ""), "2": ("/SNUB", "")}),
        ("C4", "47uF/35V polymer", "CP_Elec_6.3x5.8",
         {"1": ("/SNUB", ""), "2": ("GND", "")}),
        ("D1", "SMAJ24A", "D_SMA", {"1": ("VBUS", "K"), "2": ("GND", "A")})]
    rep = _hotplug(tmp_path, board)
    assert not [v for v in rep["violations"]
                if v["kind"] == "hotplug_overvoltage"]
    peak = max(r["peak_v"]["VBUS"] for r in rep["hotplug"]["runs"])
    assert 20 < peak < 27


def test_bsc010n04ls_row():
    t = soa.load()
    row = soa.find("BSC010N04LSATMA1", "", "", t)
    assert row and not row.get("generic") and row["tj_max"] == 175
    # diagram 3: ~0.86 A at 40 V for 1 ms -> ~34 W
    assert 25 < soa.soa_power(row, 1e-3) < 45
    assert soa.find("BSC010N04LS6ATMA1", "", "", t) is None


def test_soa_scales_to_the_pulse_vds():
    # #453: BSC010N04LS's zth is its 40 V SOA; at 20 V the part takes twice
    # the power, below the 2 V knee no more than at 2 V
    row = soa.find("BSC010N04LS")
    assert soa.vds_scale(row, 40) == pytest.approx(1.0)
    assert soa.vds_scale(row, 20) == pytest.approx(0.5)
    assert soa.vds_scale(row, 0.5) == pytest.approx(2.0 / 40)
    ts = [k * 1e-5 for k in range(101)]
    ps = [30.0] * len(ts)
    at40 = soa.junction(row, ts, ps, vds=[40.0] * len(ts))
    at20 = soa.junction(row, ts, ps, vds=[20.0] * len(ts))
    assert at40["vds_scaled"] and not soa.junction(row, ts, ps)["vds_scaled"]
    assert at20["tj_peak_c"] - 25 == pytest.approx(
        (at40["tj_peak_c"] - 25) / 2, rel=1e-6)
    assert at20["energy_j"] == at40["energy_j"]
    # a row without soa_vds ignores vds
    ao = soa.find("AO4485")
    assert soa.junction(ao, ts, ps, vds=[5.0] * len(ts))["tj_peak_c"] == \
        soa.junction(ao, ts, ps)["tj_peak_c"]


def test_soa_check_hotswap_uses_the_ramp_vds():
    rc, rep = _cli("soa_check.py", "--fet", "BSC010N04LS", "--hotswap",
                   "--c", "470u", "--v", "16.8", "--t-ramp", "2m")
    assert rep["result"]["vds_scaled"] is True
    # unscaled (40 V basis) this ramp read 256 C
    assert rep["result"]["tj_peak_c"] < 175, rep["result"]
    _rc, at = _cli("soa_check.py", "--fet", "BSC010N04LS", "--at", "1m",
                   "--vds", "20")
    assert at["soa_power_w"] == pytest.approx(2 * 150 / 4.4, rel=1e-3)


# ------------------------------------------------------- mlcc_dcbias floor

def _mlcc_board(tmp_path, cap=("22uF 10V X5R", "C_0805_2012Metric"),
                ldo=None):
    """+3V3 (named rail, 3.3 V) with one cap; ldo=(value, cap) adds an
    LDO from +3V3 onto +1V8 with that one output cap."""
    board = [("J1", "CONN", "Conn", {"1": ("+3V3", "VCC"),
                                     "2": ("GND", "GND")}),
             ("C1", cap[0], cap[1], {"1": ("+3V3", ""), "2": ("GND", "")})]
    if ldo:
        board += [("U9", ldo[0], "X2SON-4", {"4": ("+3V3", "IN"),
                                             "2": ("GND", "GND"),
                                             "1": ("+1V8", "OUT")}),
                  ("C9", ldo[1][0], ldo[1][1], {"1": ("+1V8", ""),
                                                "2": ("GND", "")})]
    return write_net(tmp_path / "b.net", board)


def _mlcc(*args):
    import mlcc_dcbias
    payload, _ = mlcc_dcbias.run([*map(str, args)])
    return payload


def _kinds(p, sev=None):
    return [(v["kind"], v["severity"]) for v in p["violations"]
            if sev is None or v["severity"] == sev]


def test_mlcc_floor_from_constraints_catches_the_issue_case(tmp_path):
    # #1067: 15 uF effective asked; one 22u 0805 X5R 10V at 3.3 V keeps
    # ~15.1 uF typical, ~12.5 uF low: below on the low curve, no --min-c
    net = _mlcc_board(tmp_path)
    cons = tmp_path / "c.json"
    cons.write_text(json.dumps({"power": [
        {"net": "+3V3", "current_a": 0.5, "voltage": 3.3,
         "min_cout": 15e-6}]}))
    p = _mlcc("--net", net, "--constraints", cons)
    assert ("rail_c_below_min", "warning") in _kinds(p)
    assert p["floors"]["+3V3"]["source"].startswith("constraints")
    t = p["rail_totals"]["+3V3"]
    assert 14e-6 < t["c_eff"] < 16e-6 and t["c_eff_low"] < 15e-6
    # the twin: a 10 uF floor holds on the low curve; no finding
    cons.write_text(json.dumps({"power": [
        {"net": "+3V3", "current_a": 0.5, "voltage": 3.3,
         "min_cout": 10e-6}]}))
    p = _mlcc("--net", net, "--constraints", cons)
    assert not _kinds(p, "error") and not _kinds(p, "warning")


def test_mlcc_floor_from_the_regulator_row(tmp_path):
    bad = _mlcc_board(tmp_path, ldo=("TPS7A2018PDQNR",
                                     ("0.47uF 6.3V X5R", "C_0201_0603Metric")))
    p = _mlcc("--net", bad)
    errs = [v for v in p["violations"] if v["kind"] == "rail_c_below_min"]
    assert errs and errs[0]["severity"] == "error"
    assert "U9" in errs[0]["refs"] and errs[0]["net"] == "+1V8"
    good = _mlcc_board(tmp_path, ldo=("TPS7A2018PDQNR",
                                      ("1uF 6.3V X5R", "C_0402_1005Metric")))
    p = _mlcc("--net", good)
    assert not _kinds(p, "error") and not _kinds(p, "warning")
    assert "+1V8" in p["floors"]


def test_mlcc_no_floor_is_not_judged_and_50pct_is_info(tmp_path):
    # an LDO with no pin_requirements row: its rail is not judged; a part
    # under the generic 50 % line with no floor is info, not a warning
    net = _mlcc_board(tmp_path, cap=("10uF 6.3V X5R", "C_0402_1005Metric"),
                      ldo=("XYZ123LDO", ("1uF 6.3V X5R",
                                          "C_0402_1005Metric")))
    p = _mlcc("--net", net)
    assert any(s.startswith("+1V8: fed by U9") for s in p["not_judged"])
    assert not _kinds(p, "warning") and not _kinds(p, "error")
    assert ("cap_dc_bias_low", "info") in _kinds(p)


def test_mlcc_min_c_overrides_and_run_contract(tmp_path):
    import mlcc_dcbias
    net = _mlcc_board(tmp_path)
    payload, out = mlcc_dcbias.run(["--net", str(net), "--min-c",
                                    "+3V3=20u", "--out", "x.json"])
    assert out == "x.json"
    assert payload["floors"]["+3V3"]["source"] == "--min-c"
    assert ("rail_c_below_min", "error") in _kinds(payload)


def test_constraints_lint_knows_min_cout(tmp_path):
    import constraints_lint
    f = tmp_path / "constraints.json"
    f.write_text(json.dumps({"power": [
        {"net": "+3V3", "current_a": 0.5, "voltage": 3.3,
         "min_cout": 15e-6}]}))
    vios, _ = constraints_lint.lint_file(f)
    assert vios == []


def test_ldo_mutant_twin_is_quiet(tmp_path):
    sys.path.insert(0, str(REPO / "tests" / "golden" / "mutations"))
    import ldo_cout_derated_below_min as m
    for quiet, bad in ((False, True), (True, False)):
        net = tmp_path / f"q{int(quiet)}.net"
        net.write_text(m.netlist(quiet=quiet), encoding="utf-8")
        p = _mlcc("--net", net)
        hit = [v for v in p["violations"] if v["kind"] == "rail_c_below_min"
               and "U97" in v["refs"]]
        assert bool(hit) is bad


def test_mlcc_floor_on_a_rail_of_unknown_dc_is_not_judged():
    # glasgow revD0/D1 VDDA (corpus sweep): U19 TPS7A20 states 0.47 uF,
    # 25 uF nominal sits on the rail but its DC is not named. That is
    # not_judged, not a missing-capacitor error; a rail with no cap is.
    import mlcc_dcbias
    caps = [{"ref": "C24", "nets": ["GND", "VDDA"], "vdc": None,
             "c_nom": 4.7e-6, "kind": "class2"}]
    fl = {"VDDA": {"min_c": 0.47e-6, "source": "U19 row", "refs": ["U19"]}}
    skipped = []
    vios, _ = mlcc_dcbias.findings(caps, fl, skipped)
    assert vios == [] and skipped and skipped[0].startswith("VDDA:")
    vios, _ = mlcc_dcbias.findings([], fl, [])
    assert [(v["kind"], v["severity"]) for v in vios] == [
        ("rail_c_below_min", "error")]
