"""check_ddr_timing (hive-hwde #487): DDR setup/hold, tDQSS and write-
leveling budget from routed flight times and reference/ddr_timing.yaml."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "tests"))
import check_ddr_timing as cdt  # noqa: E402
from test_length_groups import _pcb, fp, seg  # noqa: E402

pytest.importorskip("scipy")

TABLE = cdt.load_table()
MICRON = cdt.find_row(TABLE["drams"], "MT41K256M16TW-107:P")
TCK800 = 2500.0
CAL_HALF = TCK800 / 256 / 2          # MMDC write delay-line step / 2


# ------------------------------------------------------------ table rows

def test_rows_found():
    assert MICRON["name"].startswith("Micron")
    sam = cdt.find_row(TABLE["drams"], "K4B4G1646E-BYMA")
    assert sam and sam["speeds"] is MICRON["speeds"] and sam["verified"] is False
    imx = cdt.find_row(TABLE["controllers"], "MCIMX6Y2CVM08AB")
    assert imx["out_skew_ps"] == 100 and imx["max_rate_mts"] == 800
    assert cdt.find_row(TABLE["drams"], "W25Q128") is None


# Micron 4Gb_DDR3L Rev. P Table 58 prints, beside each base value, the same
# requirement "VREF @ 1 V/ns" (note 20). base + delta(1 V/ns, 2 V/ns) +
# (level - VREF)/slew must reproduce every one of them to the ps.
VREF_ROWS = [  # (rate, which, level, setup_vref, hold_vref)
    (800, "data", "AC160", 250, 250), (1066, "data", "AC160", 200, 200),
    (800, "data", "AC135", 275, 250),
    (1333, "data", "AC135", 180, 165), (1600, "data", "AC135", 160, 145),
    (800, "addr", "AC160", 375, 375), (1066, "addr", "AC160", 300, 300),
    (1333, "addr", "AC160", 240, 240), (1600, "addr", "AC160", 220, 220),
    (800, "addr", "AC135", 500, 375), (1066, "addr", "AC135", 425, 300),
    (1333, "addr", "AC135", 340, 240), (1600, "addr", "AC135", 320, 220),
]


@pytest.mark.parametrize("rate,which,level,s,h", VREF_ROWS)
def test_vref_referenced_rows_reproduced(rate, which, level, s, h):
    r = cdt.input_req(MICRON, rate, which, level, 1.0, 2.0)
    assert r["setup"] == pytest.approx(s, abs=1e-9)
    assert r["hold"] == pytest.approx(h, abs=1e-9)
    assert not r["clamped"]


def test_datasheet_inconsistency_kept_visible():
    """Table 58 prints tDS(AC135) DDR3L-1066 "VREF @ 1 V/ns" as 250 ps, but
    its own base (90 ps, also Table 65) + 135 mV / 1 V/ns is 225 ps; every
    other row agrees with the formula. The check uses the base (225)."""
    r = cdt.input_req(MICRON, 1066, "data", "AC135", 1.0, 2.0)
    assert r["setup"] == 225


def test_missing_level_is_an_error():
    with pytest.raises(cdt.CheckError):
        cdt.input_req(MICRON, 1600, "data", "AC160", 1.0, 2.0)  # not specified


# ------------------------------------------------------------ derating

DS = MICRON["derating"]["tDS_tDH"]["AC160"]
IS = MICRON["derating"]["tIS_tIH"]["AC160"]


@pytest.mark.parametrize("dq,dqs,ds,dh", [
    (1.0, 2.0, 0, 0), (2.0, 4.0, 80, 45), (1.5, 1.8, 61, 38),
    (0.8, 1.4, 21, 17), (0.6, 1.0, 32, 30), (0.4, 1.0, 0, 5),
    (0.9, 3.0, -1, -3), (0.7, 1.2, 27, 21)])
def test_derating_cells_exact(dq, dqs, ds, dh):
    """Table 66 cells read back exactly at their own slews."""
    d = cdt.derate(DS, dq, dqs)
    assert (d["setup"], d["hold"]) == (ds, dh) and not d["clamped"]


def test_derating_bilinear():
    # DQ 0.85 V/ns, DQS 1.5 V/ns: the mean of (0.9,1.6)=(15,13),
    # (0.9,1.4)=(23,21), (0.8,1.6)=(13,9), (0.8,1.4)=(21,17)
    d = cdt.derate(DS, 0.85, 1.5)
    assert d["setup"] == pytest.approx(18.0) and d["hold"] == pytest.approx(15.0)
    # along one axis: tIS @ CK 2.0, CA 1.25 V/ns = halfway 1.0 -> 1.5 rows
    d = cdt.derate(IS, 1.25, 2.0)
    assert d["setup"] == pytest.approx(26.5) and d["hold"] == pytest.approx(15.0)


def test_derating_clamps():
    d = cdt.derate(IS, 2.5, 2.0)            # faster than the table
    assert d["clamped"] and (d["setup"], d["hold"]) == (80, 45)
    d = cdt.derate(DS, 2.0, 1.0)            # shaded (unsupported) cell
    assert d["clamped"] and d["cells"] != [(2.0, 1.0)]


# ------------------------------------------------------------ boards

def _board(tmp_path, long_dq_mm=0.0, dqs_extra_mm=0.0, name="ddr"):
    """U1 (controller) at x=5, U2 (DRAM) at x=45: a byte lane DQ0-3 + DQS
    pair, the CK pair and A0-A3, each a straight 40 mm F.Cu run. DQ2 gets a
    U-shaped detour long_dq_mm longer; the DQS pair dqs_extra_mm."""
    nets = ["DRAM_D0", "DRAM_D1", "DRAM_D2", "DRAM_D3",
            "DRAM_SDQS0_P", "DRAM_SDQS0_N", "DRAM_SDCLK0_P", "DRAM_SDCLK0_N",
            "DRAM_A0", "DRAM_A1", "DRAM_A2", "DRAM_A3"]
    body = fp("U1", 5, 5, [(str(i + 1), 0, i * 2.0, n) for i, n in enumerate(nets)])
    body += fp("U2", 45, 5, [(str(i + 1), 0, i * 2.0, n) for i, n in enumerate(nets)])
    for i, n in enumerate(nets):
        y = 5 + i * 2.0
        extra = long_dq_mm if n == "DRAM_D2" else (
            dqs_extra_mm if n.startswith("DRAM_SDQS0") else 0.0)
        if extra:
            h = extra / 2
            body += seg(5, y, 20, y, n) + seg(20, y, 20, y + h, n)
            body += seg(20, y + h, 30, y + h, n) + seg(30, y + h, 30, y, n)
            body += seg(30, y, 45, y, n)
        else:
            body += seg(5, y, 45, y, n)
    return _pcb(tmp_path, body, name=name, four=True)


LGROUPS = [
    {"name": "CK", "nets": ["DRAM_SDCLK0_*"], "tol_mm": 0.127},
    {"name": "BYTE0", "nets": ["DRAM_D[0-3]"], "ref_nets": ["DRAM_SDQS0_*"],
     "tol_mm": 0.635},
    {"name": "DQS0", "nets": ["DRAM_SDQS0_*"], "tol_mm": 0.254},
    {"name": "ADDR_CMD", "nets": ["DRAM_A*"], "ref_nets": ["DRAM_SDCLK0_*"],
     "window_mm": [-5.08, 0]},
]


def _cons(tmp_path, **ddr):
    block = {"controller": "U1", "dram": "U2",
             "controller_part": "MCIMX6Y2CVM08AB",
             "dram_part": "MT41K256M16TW-107", "rate_mts": 800,
             "groups": [{"name": "BYTE0", "kind": "dq"},
                        {"name": "ADDR_CMD", "kind": "addr"},
                        {"name": "DQS0", "kind": "dqs", "clock": "CK"}]}
    block.update(ddr)
    p = tmp_path / "constraints.json"
    p.write_text(json.dumps({"length_groups": LGROUPS, "ddr": block}),
                 encoding="utf-8")
    return p


def _run(pcb, cons):
    payload, _ = cdt.run(["--pcb", str(pcb), "--constraints", str(cons)])
    return payload, {g["name"]: g for g in payload["groups"]}


def test_hand_budget_matched_board(tmp_path):
    """linux-computer's case, every net matched: the i.MX 6ULL + DDR3L-800
    budget by hand.
      DQ  setup = UI/2 625 - tDS(VREF) 250 - tSKD 100 - step/2 4.88 = 270.1
      DQ  hold  = 625 - tDH(VREF) 250 - 100 - 4.88               = 270.1
      CA  setup = tCK/2 1250 - tIS(VREF) 375 - 100                = 775
      CA  hold  = 1250 - tIH(VREF) 375 - 100                      = 775
      tDQSS levelled = 0.25 tCK 625 - 100 - 4.88                  = 520.1
      read eye = tQH 0.38 tCK 950 - tDQSQ 200 - spread 0          = 750"""
    p, g = _run(_board(tmp_path), _cons(tmp_path))
    assert p["status"] == "pass", p["violations"]
    assert g["BYTE0"]["setup_ps"] == pytest.approx(625 - 250 - 100 - CAL_HALF, abs=0.06)
    assert g["BYTE0"]["hold_ps"] == pytest.approx(625 - 250 - 100 - CAL_HALF, abs=0.06)
    assert g["ADDR_CMD"]["setup_ps"] == pytest.approx(775, abs=0.06)
    assert g["ADDR_CMD"]["hold_ps"] == pytest.approx(775, abs=0.06)
    assert g["DQS0"]["tdqss_levelled_ps"] == pytest.approx(625 - 100 - CAL_HALF, abs=0.06)
    assert g["BYTE0"]["read_eye_ps"] == pytest.approx(750, abs=0.06)
    # every term is listed
    names = [t["term"] for t in g["BYTE0"]["setup_terms"]]
    assert any("DRAM setup" in n for n in names) and any("SSN" in n for n in names)
    assert len(g["BYTE0"]["dram_terms"]) == 6


def _skew_ps(g, net):
    return g["BYTE0"]["skew_ps"][net]


def test_long_dq_at_800_calibrated_is_inside_the_budget(tmp_path):
    """A DQ 15 mm long costs ~90 ps of flight. At DDR3L-800 with MMDC write
    calibration the lane is re-centred, so setup only loses half of it and
    stays positive: the timing is fine, and the HDG's +/-0.635 mm rule
    (check_length_groups) is what flags this board, not the budget."""
    p, g = _run(_board(tmp_path, long_dq_mm=15.0), _cons(tmp_path))
    s = _skew_ps(g, "DRAM_D2")
    assert 70 < s < 120
    assert g["BYTE0"]["worst_late"] == "DRAM_D2"
    assert g["BYTE0"]["setup_ps"] == pytest.approx(
        625 - 250 - 100 - CAL_HALF - s / 2, abs=0.2)
    assert g["BYTE0"]["setup_ps"] > 0
    assert not [v for v in p["violations"] if v["kind"].startswith("ddr_setup")]
    assert g["BYTE0"]["uncalibrated_setup_ps"] == pytest.approx(
        625 - 250 - 100 - s, abs=0.2)


def test_long_dq_goes_negative_at_1600(tmp_path):
    """Planted fault: the same 15 mm long DQ at DDR3L-1600 (AC135) on a
    controller with 100 ps output skew and no write calibration:
      setup = 312.5 - tDS(VREF) 160 - 100 - skew(~90) < 0."""
    cons = _cons(tmp_path, rate_mts=1600, controller_part="GENERIC",
                 input_levels={"data": "AC135", "addr": "AC135"},
                 controller_terms={"out_skew_ps": 100,
                                   "write_calibration": False,
                                   "write_leveling": True,
                                   "leveling_range_tck": [0, 1]})
    p, g = _run(_board(tmp_path, long_dq_mm=15.0), cons)
    bad = [v for v in p["violations"] if v["kind"] == "ddr_setup_violation"]
    assert len(bad) == 1 and bad[0]["net"] == "DRAM_D2"
    s = _skew_ps(g, "DRAM_D2")
    assert bad[0]["margin_ps"] == pytest.approx(312.5 - 160 - 100 - s, abs=0.2)
    assert bad[0]["margin_ps"] < 0
    # matched board at the same terms passes
    p2, _ = _run(_board(tmp_path, name="ok"), cons)
    assert not [v for v in p2["violations"] if v["kind"] == "ddr_setup_violation"]


def test_dqs_longer_than_ck_is_out_of_leveling_range(tmp_path):
    """MMDC leveling only delays DQS (HDG: DQS <= Clock(min)); a DQS pair
    10 mm longer than CK needs a negative delay. The 6ULL range is not
    re-read from the RM, so it is a warning."""
    p, g = _run(_board(tmp_path, dqs_extra_mm=10.0), _cons(tmp_path))
    lv = [v for v in p["violations"] if v["kind"] == "ddr_leveling_out_of_range"]
    assert lv and lv[0]["severity"] == "warning"
    assert g["DQS0"]["leveling_margin_ps"] < 0


def test_no_leveling_judges_tdqss_unlevelled(tmp_path):
    cons = _cons(tmp_path, controller_terms={"write_leveling": False})
    p, g = _run(_board(tmp_path, dqs_extra_mm=10.0), cons)
    need = abs(min(g["DQS0"]["need_ps"].values()))
    assert g["DQS0"]["tdqss_unlevelled_ps"] == pytest.approx(625 - 100 - need, abs=0.2)
    # 10 mm (~60 ps) still inside 0.25 tCK at 800 MT/s
    assert not [v for v in p["violations"] if v["kind"] == "ddr_dqss_violation"]


def test_rate_over_controller(tmp_path):
    cons = _cons(tmp_path, rate_mts=1066)
    p, _ = _run(_board(tmp_path), cons)
    assert [v for v in p["violations"] if v["kind"] == "ddr_rate_over"]


def test_unknown_dram_warns_and_judges_nothing(tmp_path):
    p, _ = _run(_board(tmp_path), _cons(tmp_path, dram_part="XYZ123"))
    assert [v["kind"] for v in p["violations"]] == ["ddr_part_unknown"]
    assert p["groups"] == []


def test_no_ddr_block_passes(tmp_path):
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"length_groups": LGROUPS}), encoding="utf-8")
    p, _ = cdt.run(["--pcb", str(_board(tmp_path)), "--constraints", str(c)])
    assert p["status"] == "pass" and p["judged"] is False


@pytest.mark.parametrize("bad", [
    {"rate_mts": 0}, {"groups": []}, {"groups": [{"name": "BYTE0", "kind": "x"}]},
    {"groups": [{"name": "DQS0", "kind": "dqs"}]}, {"bogus": 1},
    {"groups": [{"name": "NOPE", "kind": "dq"}]},
    {"controller_terms": {"skew": 1}}])
def test_malformed_block_stops(tmp_path, bad):
    with pytest.raises(cdt.CheckError):
        _run(_board(tmp_path), _cons(tmp_path, **bad))


def test_comment_keys_pass_and_typos_still_stop(tmp_path):
    """#1097: `_` comment keys on the ddr block and on a group pass, as
    constraints_lint passes them; a misspelled key still stops."""
    groups = [{"name": "BYTE0", "kind": "dq", "_why": "lane 0"},
              {"name": "ADDR_CMD", "kind": "addr"},
              {"name": "DQS0", "kind": "dqs", "clock": "CK"}]
    _run(_board(tmp_path), _cons(tmp_path, _why="from the SoC guide",
                                 groups=groups))
    with pytest.raises(cdt.CheckError, match="unknown"):
        _run(_board(tmp_path), _cons(tmp_path, rate_mt=800))


def test_unrouted_net_not_judged(tmp_path):
    pcb = _board(tmp_path)
    lines = pcb.read_text().splitlines(keepends=True)
    pcb.write_text("".join(ln for ln in lines
                           if not (ln.lstrip().startswith("(segment")
                                   and '"DRAM_D1"' in ln)))
    p, g = _run(pcb, _cons(tmp_path))
    assert [v for v in p["violations"] if v["kind"] == "ddr_unrouted"]
    assert g["BYTE0"]["judged"] is False
