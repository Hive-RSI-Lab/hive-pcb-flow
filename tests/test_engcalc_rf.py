"""lib/engcalc.py's RF budget calcs and their eng_calc subcommands.

Each formula is pinned to a hand-worked number: the Friis cascade (NF and
IIP3), kTB sensitivity, a square-law detector's ideal limit, the
charge-pump loop filter (design then analyse returns the fc and phase
margin asked for), and line wavelength with microstrip dispersion.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import engcalc  # noqa: E402
import eng_calc  # noqa: E402


def test_friis_two_stage_hand_worked():
    # F = 1.585 + (10 - 1) / 10 = 2.485 -> 3.954 dB
    r = engcalc.friis_cascade([{"name": "lna", "gain_db": 10, "nf_db": 2},
                               {"name": "mix", "gain_db": 0, "nf_db": 10}])
    assert r["nf_db"] == pytest.approx(10 * math.log10(10 ** 0.2 + 0.9))
    assert r["gain_db"] == pytest.approx(10)
    assert r["te_k"] == pytest.approx(290 * (10 ** 0.2 + 0.9 - 1))
    assert sum(s["share"] for s in r["stages"]) == pytest.approx(1)
    assert "iip3_dbm" not in r


def test_friis_loss_ahead_adds_its_loss_to_nf():
    # a 3 dB loss before a 2 dB LNA: NF = 5 dB exactly
    r = engcalc.friis_cascade([{"gain_db": -3, "nf_db": 3},
                               {"gain_db": 20, "nf_db": 2}])
    assert r["nf_db"] == pytest.approx(5)


def test_friis_iip3():
    one = engcalc.friis_cascade([{"gain_db": 10, "nf_db": 2, "oip3_dbm": 20}])
    assert one["iip3_dbm"] == pytest.approx(10)
    # two stages, each IIP3 10 dBm, the first with 10 dB gain: the second's
    # input-referred IIP3 is 0 dBm, so 1/IIP3 = 1/10 + 1/1 mW -> -0.414 dBm
    two = engcalc.friis_cascade([{"gain_db": 10, "nf_db": 2, "oip3_dbm": 20},
                                 {"gain_db": 0, "nf_db": 6, "oip3_dbm": 10}])
    assert two["iip3_dbm"] == pytest.approx(10 * math.log10(1 / 1.1))


def test_friis_rejects_negative_nf():
    with pytest.raises(ValueError):
        engcalc.friis_cascade([{"gain_db": 10, "nf_db": -1}])


def test_rx_sensitivity():
    assert engcalc.noise_floor_dbm(1) == pytest.approx(-173.975, abs=1e-3)
    r = engcalc.rx_sensitivity(4, 1e6, 10)
    assert r["noise_floor_dbm"] == pytest.approx(-173.975 + 60 + 4, abs=1e-3)
    assert r["sensitivity_dbm"] == pytest.approx(r["noise_floor_dbm"] + 10)


def test_detector_ideal_limit():
    # beta = 1 / (2 Is) = 166.7 kV/W; Rv = n Vt / Is
    r = engcalc.diode_detector(1e6, i_s=3e-6, n=1.0, t_k=300)
    vt = engcalc.K_BOLTZMANN * 300 / 1.602176634e-19
    assert r["responsivity_v_w"] == pytest.approx(1 / 6e-6)
    assert r["rv_ohm"] == pytest.approx(vt / 3e-6)
    vn = math.sqrt(4 * engcalc.K_BOLTZMANN * 300 * r["rv_ohm"])
    assert r["nep_w_rthz"] == pytest.approx(vn / r["responsivity_v_w"])
    assert r["tss_dbm"] - r["mds_dbm"] == pytest.approx(10 * math.log10(2.5))
    assert r["responsivity_ideal"]


def test_detector_datasheet_load_and_amp_noise():
    base = engcalc.diode_detector(1e6, 5000, 5000)
    loaded = engcalc.diode_detector(1e6, 5000, 5000, r_load=5000,
                                    en_v_rthz=10e-9)
    assert loaded["responsivity_v_w"] == pytest.approx(2500)
    assert loaded["mds_dbm"] > base["mds_dbm"] + 3      # worse, both effects
    # MDS = NEP sqrt(B): 5 dB per decade of video bandwidth
    wide = engcalc.diode_detector(1e8, 5000, 5000)
    assert wide["mds_dbm"] - base["mds_dbm"] == pytest.approx(10)
    with pytest.raises(ValueError):
        engcalc.diode_detector(1e6)


@pytest.mark.parametrize("fc,pm", [(100e3, 50), (10e3, 45), (1e6, 60),
                                   (50e3, 70)])
def test_pll_design_round_trips(fc, pm):
    d = engcalc.pll_loop_design(5e-3, 100e6, 2400, fc, pm)
    a = engcalc.pll_loop_analyse(5e-3, 100e6, 2400, d["r_ohm"],
                                 d["c_series_f"], d["c_shunt_f"])
    assert a["fc_hz"] == pytest.approx(fc, rel=1e-6)
    assert a["phase_margin_deg"] == pytest.approx(pm, abs=1e-6)
    # the zero sits below fc and the pole above, symmetric in log
    assert a["zero_hz"] * a["pole_hz"] == pytest.approx(fc * fc, rel=1e-6)
    assert a["f3db_hz"] > fc


def test_pll_analyse_flags_fast_loop():
    a = engcalc.pll_loop_analyse(5e-3, 100e6, 2400, 1e3, 10e-9, 1e-9,
                                 f_ref=200e3)
    assert a["fref_over_fc"] < 10 and not a["fc_ok"]
    with pytest.raises(ValueError):
        engcalc.pll_loop_design(5e-3, 100e6, 2400, 1e5, 95)


def test_line_wavelength():
    r = engcalc.line_wavelength(10e9, 4.0, length_mm=14.9896229)
    assert r["lambda0_mm"] == pytest.approx(29.9792458)
    assert r["lambdag_mm"] == pytest.approx(14.9896229)
    assert r["quarter_wave_mm"] == pytest.approx(14.9896229 / 4)
    assert r["phase_deg"] == pytest.approx(360)
    assert r["delay_ps_per_mm"] == pytest.approx(2 / 0.299792458)
    with pytest.raises(ValueError):
        engcalc.line_wavelength(10e9, 0.5)


def test_microstrip_dispersion_limits():
    e0 = engcalc.microstrip_eeff_static(0.3, 0.2, 4.4)
    assert 1 < e0 < 4.4
    assert engcalc.microstrip_dispersion(4.4, e0, 50, 0.2, 1) \
        == pytest.approx(e0)
    assert engcalc.microstrip_dispersion(4.4, e0, 50, 0.2, 1e15) \
        == pytest.approx(4.4, rel=1e-3)
    e24 = engcalc.microstrip_dispersion(4.4, e0, 50, 0.2, 24e9)
    assert e0 < e24 < 4.4


def run(capsys, *argv):
    code = eng_calc.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_cli_rf(capsys):
    code, out = run(capsys, "cascade", "--stage", "lna:10:2",
                    "--stage", "mix:0:10", "--bw", "1M", "--snr", "10")
    assert code == 0 and out["result"]["rx"]["sensitivity_dbm"] < -90
    code, out = run(capsys, "rxsens", "--nf", "4", "--bw", "1M")
    assert code == 0
    code, out = run(capsys, "detector", "--bw", "1M", "--is", "3u")
    assert code == 0 and out["result"]["responsivity_ideal"]
    code, out = run(capsys, "pll", "--icp", "5mA", "--kvco", "100M",
                    "--n", "2400", "--fc", "100k", "--pm", "50")
    assert out["result"]["check"]["phase_margin_deg"] == pytest.approx(50)
    code, out = run(capsys, "line", "--f", "24G", "--er", "4.4",
                    "--w", "0.3", "--h", "0.2", "--length", "5")
    assert code == 0 and out["result"]["eps_eff"] \
        > out["result"]["eps_eff_static"]
    code, out = run(capsys, "line", "--f", "24G", "--eeff", "3")
    assert out["result"]["eps_eff_from"] == "given"


@pytest.mark.parametrize("argv", [
    ("cascade", "--stage", "lna:10"),
    ("pll", "--icp", "5m", "--kvco", "100M", "--n", "10"),
    ("line", "--f", "24G"),
    ("line", "--f", "24G", "--er", "4.4", "--w", "0.3"),
    ("detector", "--bw", "1M"),
])
def test_cli_rf_errors(capsys, argv):
    code, out = run(capsys, *argv)
    assert code == 2 and out["status"] == "error"


@pytest.mark.parametrize("c1,c2,stray", [("18", "18", "4"),
                                         ("18p", "18pF", "4p"),
                                         ("18e-12", "0.018n", "4")])
def test_cli_crystal_bare_numbers_are_pf(capsys, c1, c2, stray):
    code, out = run(capsys, "crystal", "--c1", c1, "--c2", c2,
                    "--stray", stray)
    assert code == 0 and out["result"]["cl_pf"] == pytest.approx(13.0)


def test_cli_i2c_bare_cb_is_pf(capsys):
    _, bare = run(capsys, "i2c", "--vdd", "3.3", "--rp", "2k2", "--cb", "50")
    _, unit = run(capsys, "i2c", "--vdd", "3.3", "--rp", "2k2", "--cb", "50p")
    assert bare["result"] == unit["result"]


@pytest.mark.parametrize("text,kind,want", [
    ("0805,C0.1", "C", 1e-7), ("0603 100n", "C", 1e-7), ("C0.1", "C", 1e-7),
    ("0.1", "C", 1e-7), ("10", "C", 1e-5), ("100n", "C", 1e-7),
    ("0805", "C", None), ("TBD", "C", None), (None, "C", None),
    ("0402,10k", "R", 1e4), ("10", "R", 10.0), ("4k7", None, 4700.0)])
def test_part_value_reads_bom_forms(text, kind, want):
    got = engcalc.part_value(text, kind)
    assert got == (pytest.approx(want) if want is not None else None)
