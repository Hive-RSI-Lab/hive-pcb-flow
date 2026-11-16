"""sim_board.py (ngspice on a board's own netlist) and lib/engcalc.py.

A small synthetic board carries one planted fault per finding the script
makes: a 100 ohm 0603 across +5V (overpower), an FB divider set 10 % off
its rail, a 0402 10 Mohm bleeder on +60V (over its 50 V Umax at 0.36 mW),
a 470 ohm I2C pull-up (too strong for 3 mA), an LED a GPIO drives
at ~26 mA, a 10 V X5R cap on 9 V (DC bias), a P-FET whose gate a GPIO pulls
to ground from 20 V, an enable RC that settles below 0.7 VDD - and one RC
enable that is fine. The engine tests run wherever the KiCad-bundled ngspice
is (bin/hwde test exports it); they skip elsewhere.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import engcalc  # noqa: E402
import sim_board  # noqa: E402
from lib import env  # noqa: E402

# ref, value, footprint, {pin: (net, pinfunction)}
BOARD = [
    ("U1", "TPS54302", "SOT-23-6", {"1": ("GND", "GND"), "3": ("+12V", "VIN"),
                                    "4": ("/FB5", "FB"), "2": ("/SW", "SW")}),
    ("R1", "100k", "R_0402_1005Metric", {"1": ("+5V", ""), "2": ("/FB5", "")}),
    ("R2", "15k", "R_0402_1005Metric", {"1": ("/FB5", ""), "2": ("GND", "")}),
    ("R3", "100R", "R_0603_1608Metric", {"1": ("+5V", ""), "2": ("GND", "")}),
    ("U2", "MCU", "QFN-32", {"1": ("+3V3", "VDD"), "2": ("GND", "GND"),
                             "3": ("/SDA", "SDA"), "4": ("/SCL", "SCL"),
                             "5": ("/LED", "IO5"), "6": ("/EN", "EN"),
                             "7": ("/GATE_PULL", "IO7")}),
    ("R4", "470", "R_0402_1005Metric", {"1": ("+3V3", ""), "2": ("/SDA", "")}),
    ("R5", "4.7k", "R_0402_1005Metric", {"1": ("+3V3", ""), "2": ("/SCL", "")}),
    # the bus's peer: it leaves the board here
    ("J1", "Conn_01x02", "PinHeader_1x02_P2.54mm_Vertical",
     {"1": ("/SDA", ""), "2": ("/SCL", "")}),
    ("R6", "22", "R_0402_1005Metric", {"1": ("/LED", ""), "2": ("/LED_A", "")}),
    ("D1", "red", "LED_0603_1608Metric", {"1": ("GND", "K"),
                                          "2": ("/LED_A", "A")}),
    ("R7", "10k", "R_0402_1005Metric", {"1": ("+3V3", ""), "2": ("/EN", "")}),
    ("C1", "1uF", "C_0402_1005Metric", {"1": ("/EN", ""), "2": ("GND", "")}),
    ("U3", "PHY", "QFN-24", {"1": ("+3V3", "VDD"), "2": ("GND", "GND"),
                             "3": ("/NRST", "NRST")}),
    ("R8", "10k", "R_0402_1005Metric", {"1": ("+3V3", ""), "2": ("/NRST", "")}),
    ("R9", "10k", "R_0402_1005Metric", {"1": ("/NRST", ""), "2": ("GND", "")}),
    ("C2", "100nF", "C_0402_1005Metric", {"1": ("/NRST", ""), "2": ("GND", "")}),
    ("C3", "10uF 10V", "C_0805_2012Metric", {"1": ("+9V", ""), "2": ("GND", "")}),
    ("Q1", "AO3401", "SOT-23", {"1": ("/GATE", "G"), "2": ("+20V", "S"),
                                "3": ("/LOAD", "D")}),
    ("R10", "100k", "R_0402_1005Metric", {"1": ("+20V", ""), "2": ("/GATE", "")}),
    ("R11", "1k", "R_0402_1005Metric", {"1": ("/GATE", ""),
                                        "2": ("/GATE_PULL", "")}),
    ("R12", "1k", "R_0402_1005Metric", {"1": ("/LOAD", ""), "2": ("GND", "")}),
    ("L1", "10uH", "L_1210", {"1": ("+3V3", ""), "2": ("+3V3A", "")}),
    ("R13", "330", "R_0402_1005Metric", {"1": ("+3V3A", ""), "2": ("GND", "")}),
    ("R14", "10M", "R_0402_1005Metric", {"1": ("+60V", ""), "2": ("GND", "")}),
]


def write_net(path: Path, board=BOARD) -> Path:
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


# ---------------------------------------------------------------- engcalc

@pytest.mark.parametrize("text,want", [
    ("4k7", 4700), ("2.2k", 2200), ("100R", 100), ("0R", 0), ("2R2", 2.2),
    ("1M", 1e6), ("100nF", 1e-7), ("2.2u", 2.2e-6), ("10uH 8A", 1e-5),
    ("470uF/50V", 4.7e-4), ("22pF", 22e-12), ("49.9R", 49.9), ("1meg", 1e6),
    ("red", None), ("", None), (None, None), ("C_*", None)])
def test_parse_value(text, want):
    got = engcalc.parse_value(text)
    assert got == pytest.approx(want) if want is not None else got is None


def test_parse_ratings():
    assert engcalc.parse_volts("10uF 25V") == 25
    assert engcalc.parse_volts("470uF/50V") == 50
    assert engcalc.parse_watts("100mW") == pytest.approx(0.1)
    assert engcalc.parse_watts("1/4W") == pytest.approx(0.25)
    assert engcalc.parse_watts("R_0402_.1W") == pytest.approx(0.1)
    assert engcalc.parse_tolerance("±1%") == pytest.approx(0.01)


@pytest.mark.parametrize("fp,code", [
    ("Resistor_SMD:R_0603_1608Metric", "0603"), ("aiee:R0402", "0402"),
    ("x:C_1005Metric", "0402"), ("aiee:SOT-23-6", None), ("", None)])
def test_package_code(fp, code):
    assert engcalc.package_code(fp) == code


def test_worked_examples():
    # SY8089 0.6 V, 150k/33k -> 3.327 V (the Lite board's +3V3)
    fb = engcalc.fb_vout(0.6, 150e3, 33e3, 0.01, 0.02)
    assert fb["nom"] == pytest.approx(3.3273, abs=1e-4)
    assert fb["min"] < fb["nom"] < fb["max"]
    # 10k / 1u to 70 %: 1.204 tau
    assert engcalc.rc_cross_s(0.01, 0.7) == pytest.approx(0.01204, abs=1e-5)
    assert engcalc.rc_corner_hz(10e3, 1e-6) == pytest.approx(15.915, abs=1e-3)
    i2c = engcalc.i2c_pullup(3.3, 2200, 50, 400)
    assert i2c["rmin"] == pytest.approx(966.7, abs=0.1)
    assert i2c["tr_ns"] == pytest.approx(93.2, abs=0.1)
    assert i2c["ok"]
    assert not engcalc.i2c_pullup(3.3, 470, 50, 400)["ok"]
    assert engcalc.crystal_load_pf(18, 18) == pytest.approx(13.0)
    assert engcalc.ldo_tj(5, 3.3, 0.3, 180, 40)["tj_c"] == pytest.approx(131.8)
    assert engcalc.holdup_s(470e-6, 20, 12, 0.5) == pytest.approx(7.52e-3)
    # D/CRCW e3: 0603 Umax 75 V, critical R = 75^2 / 0.1 = 56.25 kOhm
    assert engcalc.resistor_umax_v("R_0603_1608Metric") == 75
    assert engcalc.resistor_umax_v("R_2512_6332Metric") == 500
    assert 75 ** 2 / engcalc.resistor_rating_w("R_0603") == pytest.approx(
        56250)
    # thick film: flat to 70 C, 0 at 155 C; 85 C -> 0.82 (R-RAT-2)
    assert engcalc.resistor_derate(25) == engcalc.resistor_derate(70) == 1
    assert engcalc.resistor_derate(85) == pytest.approx(0.8235, abs=1e-4)
    assert engcalc.resistor_derate(160) == 0
    # Littelfuse 881 at 70 C: k 0.90, so 1.5 A needs 1.5/(0.75*0.9) = 2.22 A
    assert engcalc.fuse_rerate(70) == pytest.approx(0.90)
    assert engcalc.fuse_rerate(0) == 1.0
    assert 1.5 / (engcalc.FUSE_LOAD_FRACTION * engcalc.fuse_rerate(70)) \
        == pytest.approx(2.222, abs=1e-3)
    # a 1.1 A (23 C) PPTC holds about 0.75 A at 70 C
    assert 1.1 * engcalc.pptc_hold_factor(70) == pytest.approx(0.748)
    assert engcalc.pptc_hold_factor(23) == 1.0
    assert engcalc.pptc_hold_factor(-40) > 1.4


@pytest.mark.parametrize("text,want", [
    ("-40℃~+85℃", (-40, 85, False)), ("-55℃~+155℃", (-55, 155, False)),
    ("-40℃~+125℃@(Tj)", (-40, 125, True)),
    ("-40℃~+125℃@(TA/TJ)", (-40, 125, False)),
    ("-40°C ~ 125°C", (-40, 125, False)), ("0 to 70 C", (0, 70, False)),
    ("25V", None), ("", None), (None, None)])
def test_parse_temp_range(text, want):
    assert engcalc.parse_temp_range(text) == want


def test_parse_amps():
    assert engcalc.parse_amps("750 mA") == pytest.approx(0.75)
    assert engcalc.parse_amps("1.1A") == pytest.approx(1.1)
    assert engcalc.parse_amps("6A 32V") == 6
    assert engcalc.parse_amps("32V") is None
    assert engcalc.led_color("KT-0603", "Red") == "red"
    assert engcalc.led_color("yellow-green") == "yellow-green"


# ---------------------------------------------------- board model (no engine)

def test_board_model(tmp_path):
    net = write_net(tmp_path / "b.net")
    board = sim_board.load_board(net)
    rails = sim_board.resolve_rails(board, {}, None, None, {})
    assert rails["+5V"][0] == 5.0 and rails["+3V3"] == (3.3, "net name")
    model = sim_board.build_model(board, {}, rails)
    kinds = {e["ref"]: e["kind"] for e in model["elems"]}
    assert kinds["D1"] == "D" and kinds["Q1"] == "Q" and kinds["C1"] == "C"
    d1 = next(e for e in model["elems"] if e["ref"] == "D1")
    assert d1["type"] == "led" and d1["a"] == "/LED_A"
    q1 = next(e for e in model["elems"] if e["ref"] == "Q1")
    assert q1["type"] == "pmos" and q1["roles"]["G"] == "/GATE"
    assert {u["ref"] for u in model["unmodelled"]} == {"U1", "U2", "U3",
                                                        "J1"}
    io, od = sim_board.io_nets(board, rails, model)
    assert io == {"/SDA": 3.3, "/SCL": 3.3, "/LED": 3.3, "/GATE_PULL": 3.3}
    assert "/EN" not in io and not od
    # +3V3 and +3V3A share a DC node through L1: one source only
    rails["+3V3A"] = (3.2, "net name")
    srcs = sim_board.rail_sources(rails, model)
    assert srcs["+3V3A"] == "+3V3"
    deck = sim_board.circuit(model, rails, "op", io)
    assert "VR_P3V3 P3V3 0 DC 3.3" in deck and "VR_P3V3A" not in deck
    assert "RIO_N_LED IO_N_LED N_LED 25" in deck and deck.endswith(".end\n")


# A dual NPN (SOT-363, E1 B1 C2 E2 B2 C1): unit A switches /LOAD_A from
# /DRV_A, unit B /LOAD_B from /DRV_B. Each unit's role spans two nets.
DUAL_PINS = {"1": "GND", "2": "/DRV_A", "6": "/LOAD_A",
             "4": "GND", "5": "/DRV_B", "3": "/LOAD_B"}
DUAL_FN = {"1": "E", "2": "B", "6": "C", "4": "E", "5": "B", "3": "C"}


@pytest.mark.parametrize("value,fn", [
    ("MMDT3904", DUAL_FN),                                  # E/B/C repeated
    ("BC847BS", {"1": "E1", "2": "B1", "6": "C1",           # numbered units
                 "4": "E2", "5": "B2", "3": "C2"}),
    ("MBT3904DW", {k: "" for k in DUAL_PINS}),               # blank: by MPN
])
def test_dual_bjt_is_two_units(tmp_path, value, fn):
    board = [("Q1", value, "SOT-363",
              {pin: (net, fn[pin]) for pin, net in DUAL_PINS.items()}),
             ("R1", "1k", "R_0402", {"1": ("+5V", ""), "2": ("/LOAD_A", "")}),
             ("R2", "1k", "R_0402", {"1": ("+5V", ""), "2": ("/LOAD_B", "")})]
    b = sim_board.load_board(write_net(tmp_path / "d.net", board))
    model = sim_board.build_model(b, {}, {})
    assert model["unmodelled"] == []
    q = {e["ref"]: e for e in model["elems"] if e["kind"] == "Q"}
    assert set(q) == {"Q1A", "Q1B"}
    assert q["Q1A"]["roles"] == {"E": "GND", "B": "/DRV_A", "C": "/LOAD_A"}
    assert q["Q1B"]["roles"] == {"E": "GND", "B": "/DRV_B", "C": "/LOAD_B"}
    assert all(e["type"] == "npn" and e["comp"] == "Q1" for e in q.values())
    assert q["Q1A"]["dc"].startswith("QQ1A N_LOAD_A N_DRV_A 0 Q_Q1A")


def test_dual_unknown_or_complementary_stays_open(tmp_path):
    for value, fn in (("XYZ123", {k: "" for k in DUAL_PINS}),
                      ("BC847BPN", DUAL_FN)):
        board = [("Q1", value, "SOT-363",
                  {pin: (net, fn[pin]) for pin, net in DUAL_PINS.items()})]
        b = sim_board.load_board(write_net(tmp_path / "d.net", board))
        model = sim_board.build_model(b, {}, {})
        assert [u["ref"] for u in model["unmodelled"]] == ["Q1"]


def test_dual_nfet_units(tmp_path):
    pins = {"1": ("GND", "S"), "2": ("/G1", "G"), "6": ("/D1", "D"),
            "4": ("GND", "S"), "5": ("/G2", "G"), "3": ("/D2", "D")}
    b = sim_board.load_board(write_net(tmp_path / "f.net", [
        ("Q2", "2N7002DW", "SOT-363", pins)]))
    q = {e["ref"]: e for e in sim_board.build_model(b, {}, {})["elems"]}
    assert q["Q2A"]["roles"] == {"S": "GND", "G": "/G1", "D": "/D1"}
    assert q["Q2B"]["type"] == "nmos" and q["Q2B"]["roles"]["D"] == "/D2"


def test_norm_fn():
    assert sim_board.norm_fn("EN_3", "3") == ["EN"]
    assert sim_board.norm_fn("~{RESET}", "1") == ["RESET"]
    assert sim_board.norm_fn("CFG2/SCL_2", "2") == ["CFG2", "SCL"]
    # STM32 port pin named with its alternate (#465)
    assert sim_board.norm_fn("PC14-OSC32_IN", "3") == ["PC14", "OSC32_IN"]
    assert sim_board.norm_fn("PB8-BOOT0", "60") == ["PB8", "BOOT0"]
    assert sim_board.norm_fn("USB_D-", "4") == ["USB_D-"]
    assert sim_board.norm_fn("IN-", "2") == ["IN-"]


def test_vref_table_entries_are_well_formed():
    rows = sim_board.load_vrefs()
    assert rows
    for r in rows:
        assert 0.1 < r["vref"] < 2.0 and 0 < r.get("tol", 0.02) < 0.1
        assert r.get("source") and "verified" in r


# ------------------------------------------------------------------ engine

@pytest.fixture(scope="module")
def report(tmp_path_factory):
    if env.find_ngspice_dll() is None:
        pytest.skip("no ngspice shared library (KiCad bundle or "
                    "HWDE_NGSPICE_DLL)")
    net = write_net(tmp_path_factory.mktemp("sb") / "b.net")
    out = net.with_suffix(".json")
    cp = subprocess.run([sys.executable, str(SCRIPTS / "sim_board.py"),
                         "--net", str(net), "--out", str(out)],
                        capture_output=True, text=True, timeout=300)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def _kinds(rep, kind):
    return [v for v in rep["violations"] if v["kind"] == kind]


def test_engine_resistor_overpower(report):
    (v,) = [v for v in _kinds(report, "resistor_overpower")
            if v["refs"] == ["R3"]]
    assert v["severity"] == "error" and v["power_w"] == pytest.approx(0.25,
                                                                       rel=1e-3)


def test_engine_resistor_overvoltage(report):
    # 60 V across an 0402 (Umax 50 V): 0.36 mW passes the power check
    (v,) = _kinds(report, "resistor_overvoltage")
    assert v["refs"] == ["R14"] and v["severity"] == "error"
    assert v["v_across"] == pytest.approx(60) and v["umax_v"] == 50
    assert not any(x["refs"] == ["R14"]
                   for x in _kinds(report, "resistor_overpower"))


def test_engine_resistor_power_derated_at_ambient(tmp_path):
    if env.find_ngspice_dll() is None:
        pytest.skip("no ngspice shared library")
    # an 0603 1.5k across +10V: 67 mW, a warning at 25 C (over half of
    # 0.1 W), over its rating at 100 C, where it holds 0.1 x 55/85 = 65 mW
    net = write_net(tmp_path / "d.net", [
        ("R1", "1.5k", "R_0603_1608Metric", {"1": ("+10V", ""),
                                             "2": ("GND", "")})])
    got = {}
    for amb in (None, 100):
        cons = tmp_path / f"c{amb}.json"
        cons.write_text(json.dumps({"ambient_c": amb} if amb else {}))
        out = tmp_path / f"r{amb}.json"
        subprocess.run([sys.executable, str(SCRIPTS / "sim_board.py"),
                        "--net", str(net), "--constraints", str(cons),
                        "--out", str(out)], capture_output=True, text=True,
                       timeout=300)
        rep = json.loads(out.read_text(encoding="utf-8"))
        (got[amb],) = _kinds(rep, "resistor_overpower")
    assert got[None]["severity"] == "warning"
    assert got[100]["severity"] == "error"
    assert got[100]["rating_w"] == pytest.approx(0.1 * 55 / 85)
    assert "derated" in got[100]["msg"]


def test_engine_fb_divider(report):
    (v,) = _kinds(report, "fb_divider_mismatch")
    # 0.596 * (1 + 100k/15k) = 4.57 V against +5V: -8.6 %
    assert v["severity"] == "error" and v["refs"] == ["R1", "R2", "U1"]
    assert v["vout"]["nom"] == pytest.approx(4.569, abs=1e-3)
    # #847 re-read the TPS5430x Vref in its datasheet, so it is no longer
    # an unknown
    assert not any(u["ref"] == "U1" and "Vref" in u["what"]
                   for u in report["known_unknowns"])


def test_engine_i2c(report):
    (v,) = _kinds(report, "i2c_pullup_strong")
    assert v["refs"] == ["R4"]
    scl = next(r for r in report["i2c"] if r["nets"] == ["/SCL"])
    assert scl["ok"] and scl["cb_assumed"]


def test_engine_led_and_gpio(report):
    led = report["leds"][0]
    assert led["current_a"]["default"] < 1e-6
    i = led["current_a"]["io_high"]
    assert 0.02 < i < 0.04   # (3.3 - ~1.9) / (22 + 25 + RS)
    assert [v["refs"] for v in _kinds(report, "led_overcurrent")] == [["D1"]]
    assert any(v["net"] == "/LED" for v in _kinds(report, "gpio_overcurrent"))


def test_engine_cap_dc_bias_and_mosfet(report):
    assert _kinds(report, "cap_dc_bias") == []      # no dielectric known
    (q,) = report["mosfets"]
    # GATE_PULL low through 25 + 1k against 100k to +20V: Vgs ~ -19.8 V
    assert q["vgs"]["io_low"] == pytest.approx(-19.8, abs=0.3)
    (v,) = _kinds(report, "mosfet_vgs")
    assert v["severity"] == "warning" and v["scenario"] == "io_low"


def test_engine_enables(report):
    en = {r["net"]: r for r in report["enables"]}
    assert en["/EN"]["t_cross_s"] == pytest.approx(0.012, rel=0.1)
    assert en["/NRST"]["final_v"] == pytest.approx(1.65, abs=0.01)
    (v,) = _kinds(report, "enable_never_high")
    assert v["net"] == "/NRST"


def test_engine_linked_rails_do_not_fight(report):
    assert abs(report["rail_currents_a"]["+3V3"]) < 0.05
    assert "+3V3A" not in report["rail_currents_a"]


# A PD-fed P-FET load switch: 33k VBUS->gate, 100k gate->GPIO. Driven on
# (GPIO low) its Vsg is VBUS x 33/133: 5.2 V at the 21 V nominal, 2.2 V at
# the 9 V lowest contract - under the 2.5 V Vgs(th) max.
PD_BOARD = [
    ("U1", "MCU", "QFN-32", {"1": ("+3V3", "VDD"), "2": ("GND", "GND"),
                             "3": ("/PVDD_EN", "IO3")}),
    ("Q1", "AO4485", "SOIC-8", {"1": ("/GATE", "G"), "2": ("VBUS", "S"),
                                "3": ("PVDD", "D")}),
    ("R1", "33k", "R_0402_1005Metric", {"1": ("VBUS", ""), "2": ("/GATE", "")}),
    ("R2", "100k", "R_0402_1005Metric", {"1": ("/GATE", ""),
                                         "2": ("/PVDD_EN", "")}),
    ("R3", "1k", "R_0603_1608Metric", {"1": ("PVDD", ""), "2": ("GND", "")}),
]


def _pd_report(tmp_path, attrs, min_v=9.0):
    (tmp_path / "kicad").mkdir()
    write_net(tmp_path / "kicad" / "pd.net", PD_BOARD)
    volts = {"net": "VBUS", "voltage": 21}
    if min_v is not None:
        volts["min"] = min_v
    (tmp_path / "kicad" / "constraints.json").write_text(json.dumps(
        {"voltages": [volts]}), encoding="utf-8")
    (tmp_path / "parts").mkdir()
    (tmp_path / "parts" / "parts.json").write_text(json.dumps({"parts": [
        {"mpn": "AO4485", "refdes": ["Q1"], "attributes": [
            {"name": k, "value": v} for k, v in attrs.items()]}]}),
        encoding="utf-8")
    out = tmp_path / "sim.json"
    subprocess.run([sys.executable, str(SCRIPTS / "sim_board.py"),
                    "--workspace", str(tmp_path), "--out", str(out)],
                   capture_output=True, text=True, timeout=300)
    return json.loads(out.read_text(encoding="utf-8"))


RDSON = {"RDS(on)": "12.5mΩ@10V、16mΩ@4.5V"}


def test_fet_drive_attrs():
    part = {"attrs": {"Gate Threshold Voltage (Vgs(th))": "-1~-2.5V",
                      **RDSON}}
    assert sim_board.fet_vth_max(part)[0] == 2.5
    assert sim_board.fet_rdson_vgs(part) == 4.5
    part["attrs"]["Vgs(th) max"] = "3V"
    assert sim_board.fet_vth_max(part) == (3.0, "parts.json max")
    assert sim_board.fet_vth_max(None) == (None, "")


def test_rail_mins():
    rails = {"VBUS": (21.0, "constraints"), "+3V3": (3.3, "net name")}
    cons = {"voltages": [{"net": "VBUS", "voltage": 21, "min": 9},
                         {"net": "+3V3", "voltage": 3.3, "min": 3.3}]}
    assert sim_board.rail_mins(rails, cons, {}) == {"VBUS": 9.0}
    assert sim_board.rail_mins(rails, None, {"+3V3": 3.0}) == {"+3V3": 3.0}
    with pytest.raises(sim_board.checklib.CheckError):
        sim_board.rail_mins(rails, None, {"PVDD": 5.0})


@pytest.fixture
def need_engine():
    if env.find_ngspice_dll() is None:
        pytest.skip("no ngspice shared library (KiCad bundle or "
                    "HWDE_NGSPICE_DLL)")


def test_engine_underdriven_pvdd_switch(tmp_path, need_engine):
    rep = _pd_report(tmp_path, {"Vgs(th) max": "-2.5V", **RDSON})
    (q,) = rep["mosfets"]
    assert q["on_vgs"]["io_low"] == pytest.approx(5.21, abs=0.05)
    assert q["on_vgs"]["io_low@min"] == pytest.approx(2.23, abs=0.05)
    (v,) = _kinds(rep, "mosfet_underdrive")
    assert v["severity"] == "error" and v["scenario"] == "io_low@min"
    assert v["refs"] == ["Q1"] and rep["status"] == "violations"
    assert rep["rail_min_v"] == {"VBUS": 9.0}


def test_engine_underdrive_rdson_and_nominal(tmp_path, need_engine):
    # only LCSC's bare Threshold figure (1.5 V): 2.2 V clears it but not
    # the 4.5 V RDS(on) point - a warning
    rep = _pd_report(tmp_path, {"Gate Threshold Voltage (Vgs(th))": "1.5V",
                                **RDSON})
    (v,) = _kinds(rep, "mosfet_underdrive")
    assert v["severity"] == "warning" and v["scenario"] == "io_low@min"


def test_engine_no_underdrive_without_min(tmp_path, need_engine):
    rep = _pd_report(tmp_path, {"Vgs(th) max": "2.5V", **RDSON}, min_v=None)
    assert _kinds(rep, "mosfet_underdrive") == []
    assert "rail_min_v" not in rep


def test_fb_divider_series_bottom_chain(tmp_path):
    """A bottom of two resistors in series (FB -> R2 -> mid -> R3 -> GND)
    reads as one bottom of R2 + R3, not '1 top / 0 bottom'."""
    board = [
        ("U4", "TPS54302", "SOT-23-6", {"1": ("GND", "GND"),
                                        "3": ("+12V", "VIN"),
                                        "4": ("/B1V375_FB", "FB")}),
        ("R1", "100k", "R_0402_1005Metric", {"1": ("+5V", ""),
                                             "2": ("/B1V375_FB", "")}),
        ("R2", "10k", "R_0402_1005Metric", {"1": ("/B1V375_FB", ""),
                                            "2": ("/MID", "")}),
        ("R3", "5k", "R_0402_1005Metric", {"1": ("/MID", ""),
                                           "2": ("GND", "")}),
    ]
    b = sim_board.load_board(write_net(tmp_path / "b.net", board))
    rails = sim_board.resolve_rails(b, {}, None, None, {})
    model = sim_board.build_model(b, {}, rails)
    vios, unknowns, facts = [], [], {}
    sim_board.regulator_findings(b, {}, rails, model, {"vref": {}}, vios,
                                 unknowns, facts)
    assert not any("plain divider" in u["why"] for u in unknowns)
    (row,) = facts["regulators_fb"]
    assert row["r_top"] == "R1" and row["r_bot"] == "R2+R3"
    assert row["out_net"] == "+5V"
    assert row["ratio"] == pytest.approx(1 + 100 / 15)
    (v,) = vios
    assert v["refs"] == ["R1", "R2", "R3", "U4"]


@pytest.mark.parametrize("mpn, top, bot, rail, volts, how, kinds", [
    # a voice board's U102 (4.99 V) and U201 (3.32 V) under their rails'
    # constraints worst cases, 4.9 % and 4.4 % above the dividers' nominals
    ("TPS56637RPAR", "73.2k", "10k", "+5V", 5.25, "constraints", []),
    ("TLV62595DMQR", "453k", "100k", "+3V3", 3.47, "constraints", []),
    # the same 5.25 V given with --rail is a set point, 4.9 % off
    ("TPS56637RPAR", "73.2k", "10k", "+5V", 5.25, "--rail",
     [("fb_divider_mismatch", "warning")]),
    # a 3.32 V divider under +5V's worst case is not that rail
    ("TLV62595DMQR", "453k", "100k", "+5V", 5.25, "constraints",
     [("fb_divider_mismatch", "error")])])
def test_fb_divider_constraints_voltage_is_a_worst_case(tmp_path, mpn, top,
                                                        bot, rail, volts,
                                                        how, kinds):
    """constraints `voltages` give a rail's worst case (constraints_schema.md),
    not its set point: one from the divider's lowest output to 10 % over its
    nominal is that rail, as check_ratings judges it."""
    board = [
        ("U1", mpn, "QFN-16", {"1": ("GND", "GND"), "2": ("+9V", "VIN"),
                               "3": ("/FB", "FB")}),
        ("R1", top, "R_0402_1005Metric", {"1": (rail, ""), "2": ("/FB", "")}),
        ("R2", bot, "R_0402_1005Metric", {"1": ("/FB", ""), "2": ("GND", "")}),
    ]
    b = sim_board.load_board(write_net(tmp_path / "b.net", board))
    cons = {"voltages": [{"net": rail, "voltage": volts}]}
    rails = (sim_board.resolve_rails(b, {}, cons, None, {})
             if how == "constraints" else
             sim_board.resolve_rails(b, {}, None, None, {rail: volts}))
    assert rails[rail] == (volts, how)
    model = sim_board.build_model(b, {}, rails)
    vios, unknowns, facts = [], [], {}
    sim_board.regulator_findings(b, {}, rails, model, {"vref": {}}, vios,
                                 unknowns, facts)
    (row,) = facts["regulators_fb"]
    assert row["vref_verified"] and row["declared_source"] == how
    assert [(v["kind"], v["severity"]) for v in vios] == kinds


# ------------------------------------- SoC pads, pin modes, pin ratings

# An i.MX-style SoC whose pads carry their default function: ENET2_TX_EN
# (muxed to a GPIO) drives a status LED; DRAM/USB pads and an ADC-named net
# stay undriven. An MCU's PB11 sits on a peak detector (an ADC input the
# board's pin_modes names). An INA240 on VS = 3.3 V measures a 12 V rail;
# a gate driver's HB rides above its VDD; a 2 R + 2.2 uF snubber's mid node
# on a sheet is named after VBUS but is no rail.
SOC_BOARD = [
    ("U5", "MCIMX6Y2CVM08AB", "BGA-289", {
        "N13": ("+3V3", "VDD_HIGH_IN"), "A1": ("GND", "GND"),
        "B15": ("/LED_STATUS", "ENET2_TX_EN_B15"),
        "F17": ("/U4TX", "UART4_TX_DATA_F17"),
        "G6": ("/DQ0", "DRAM_DATA00_G6"),
        "U14": ("/USB_DP", "USB_OTG1_DP_U14"),
        "L17": ("/ADC_1V35", "GPIO1_IO03_L17")}),
    ("R20", "470", "R_0402_1005Metric", {"1": ("/LED_STATUS", ""),
                                         "2": ("/LED_K", "")}),
    ("D20", "red", "LED_0603_1608Metric", {"1": ("GND", "K"),
                                           "2": ("/LED_K", "A")}),
    ("R21", "10k", "R_0402_1005Metric", {"1": ("/U4TX", ""),
                                         "2": ("GND", "")}),
    ("R22", "240", "R_0402_1005Metric", {"1": ("/DQ0", ""),
                                         "2": ("GND", "")}),
    ("R23", "10k", "R_0402_1005Metric", {"1": ("+1V35", ""),
                                         "2": ("/ADC_1V35", "")}),
    ("R24", "10k", "R_0402_1005Metric", {"1": ("/ADC_1V35", ""),
                                         "2": ("GND", "")}),
    ("U6", "STM32G474RBT3", "LQFP-64", {"1": ("+3V3", "VDD"),
                                        "2": ("GND", "VSS"),
                                        "30": ("/SWPK_A", "PB11")}),
    ("C20", "100pF", "C_0402_1005Metric", {"1": ("/SWPK_A", ""),
                                           "2": ("GND", "")}),
    ("R25", "10", "R_0402_1005Metric", {"1": ("/SWPK_A", ""),
                                        "2": ("GND", "")}),
    ("U7", "INA240A2DR", "SOIC-8", {"5": ("+3V3", "VS"), "2": ("GND", "GND"),
                                    "8": ("/SHUNT_P", "IN+"),
                                    "1": ("/SHUNT_N", "IN–")}),
    ("R26", "0.01", "R_2512_6332Metric", {"1": ("+12V", ""),
                                          "2": ("/SHUNT_P", "")}),
    ("R27", "0.01", "R_2512_6332Metric", {"1": ("/SHUNT_P", ""),
                                          "2": ("/SHUNT_N", "")}),
    ("R28", "10k", "R_0603_1608Metric", {"1": ("/SHUNT_N", ""),
                                         "2": ("GND", "")}),
    ("U8", "UCC27211", "SOIC-8", {"1": ("+3V3", "VDD"), "7": ("GND", "VSS"),
                                  "2": ("/HB", "HB")}),
    ("R29", "1k", "R_0402_1005Metric", {"1": ("+12V", ""), "2": ("/HB", "")}),
    ("R30", "2", "R_1206_3216Metric", {"1": ("+12V", ""),
                                       "2": ("/charger/CHG_VBUS_RC", "")}),
    ("C21", "2.2uF", "C_0805_2012Metric", {"1": ("/charger/CHG_VBUS_RC", ""),
                                           "2": ("GND", "")}),
]


def test_soc_pads_and_pin_modes(tmp_path):
    b = sim_board.load_board(write_net(tmp_path / "s.net", SOC_BOARD))
    rails = sim_board.resolve_rails(b, {}, None, None, {})
    assert "/charger/CHG_VBUS_RC" not in rails and "+12V" in rails
    model = sim_board.build_model(b, {}, rails)
    skipped = []
    io, od = sim_board.io_nets(b, rails, model, None, skipped)
    assert {"/LED_STATUS", "/U4TX", "/SWPK_A"} <= set(io)
    assert not {"/DQ0", "/USB_DP", "/ADC_1V35"} & set(io)
    assert skipped == [{"ref": "U5", "pin": "L17", "net": "/ADC_1V35",
                        "why": "analog net name"}]
    modes = sim_board.pin_modes({"pin_modes": {"U6": {"PB11": "analog"}}},
                                ["U5.ADC_1V35=io", "U5.F17=nc"])
    assert modes == {"U6": {"PB11": "off"}, "U5": {"ADC_1V35": "io",
                                                   "F17": "off"}}
    modes["U5"]["GPIO1_IO03"] = "io"
    io, _ = sim_board.io_nets(b, rails, model, modes)
    assert "/SWPK_A" not in io and "/U4TX" not in io and "/ADC_1V35" in io
    for bad in (["U5.F17"], ["U5=io"], ["U5.F17=sideways"]):
        with pytest.raises(sim_board.checklib.CheckError):
            sim_board.pin_modes(None, bad)
    for fn in ("ENET2_TX_EN", "UART4_TX_DATA", "GPIO1_IO03", "LCD_DATA05",
               "SD1_CMD", "GPIO0_A4_D"):
        assert sim_board.SOC_PAD_FN.match(fn), fn
    for fn in ("DRAM_DATA00", "USB_OTG1_DP", "SDA", "CAN_H", "VDD_SOC_IN"):
        assert not (sim_board.SOC_PAD_FN.match(fn)
                    and not sim_board.SOC_NOT_IO.match(fn)), fn


def test_pin_rating():
    table = sim_board.load_pin_ratings()
    c = {"ref": "U7", "value": "INA240A2DR"}
    lim, src = sim_board.pin_rating(c, {"fn": ["IN–"]}, None, table)
    assert lim == 80 and "SBOS662" in src
    bq = {"ref": "U301", "value": "x"}
    part = {"mpn": "BQ76952PFBR", "attrs": {}}
    assert sim_board.pin_rating(bq, {"fn": ["PDSG"]}, part, table)[0] == 85
    assert sim_board.pin_rating(bq, {"fn": ["REGIN"]}, part, table) == \
        (None, "")
    csa = {"ref": "U9", "value": "XYZ123"}
    part = {"mpn": "XYZ123", "attrs": {"Common Mode Voltage": "-4V~75V"}}
    assert sim_board.pin_rating(csa, {"fn": ["IN+"]}, part, table)[0] == 75
    assert sim_board.pin_rating(csa, {"fn": ["HB"]}, None, table)[0] == \
        "switch_node"


# Shunt inputs ride the bus, not VS (#521): each pin form KiCad's own
# symbols and boards use (Vin+ on INA226/233, bare + on INA240, per-channel
# IN+2 on INA3221, SENSE1+ on PAC19xx, +IN on NCS21x and AD82xx, Vin+ on
# AD8211, -INS on LTC6102, RS+ on MAX4080/471, SENSE+ on LTC4151) reads its
# own rating; a control or supply pin of the same part does not.
@pytest.mark.parametrize("value,fn,lim", [
    ("INA3221AIRGVR", "IN+2", 26), ("INA3221", "IN3-", 26),
    ("INA3221AIRGVR", "VPU", 26), ("INA3221AIRGVR", "SCL", None),
    ("INA233AIDGSR", "VIN+", 36), ("INA233AIDGSR", "VBUS", 36),
    ("INA226AIDGSR", "VIN-", 36), ("INA240A1D", "+", 80),
    ("INA240A1D", "-", 80), ("INA4181A1", "IN2+", 26),
    ("INA2180A1", "IN1-", 26), ("INA260AIPWR", "VIN+", 36),
    ("INA234AIYBJR", "IN-", 28), ("PAC1934T-I/JQ", "SENSE1+", 32),
    ("PAC1934T-I/JQ", "SENSE4-", 32), ("PAC1934T-I/JQ", "VDD", None),
    ("INA200AIDR", "V_{IN+", 80), ("INA225AIDGSR", "IN+", 36),
    ("INA253A1IPW", "IS-", 80), ("INA233AIDGSR", "SDA", None),
    ("PAC1954T-E/4MX", "SENSE3+", 32), ("PAC1944T-E/4MX", "SENSE1+", 9),
    ("NCS210RSQT2G", "+IN", 26), ("NCV214RMUTAG", "-IN", 26),
    ("NCS210RSQT2G", "REF", None),
    ("AD8202YRZ", "+IN", 28), ("AD8203YRZ", "-IN", 30),
    ("AD8205YRZ", "+IN", 65), ("AD8208WBRZ", "-IN", 45),
    ("AD8210YRZ", "+IN", 65), ("AD8211YRJZ", "VIN+", 65),
    ("AD8213YRMZ", "+IN1", 65), ("AD8212YRMZ", "VSNS", 65),
    ("AD8216WYRZ", "-IN", 65), ("AD8217BRMZ", "+IN", 80),
    ("AD8218BRMZ", "-IN", 80), ("AD8219WBRMZ", "+IN", 80),
    ("AD8218BRMZ", "VS", None), ("LTC6102CMS8", "-INS", 60),
    ("LTC6102HVCMS8", "+IN", 100), ("LTC6102CMS8", "V-", None),
    ("LTC4151CMS", "SENSE+", 80), ("LTC4151CMS", "ADIN", None),
    ("MAX4080SASA+", "RS-", 76), ("MAX4081TAUA+", "RS+", 76),
    ("MAX471CSA", "RS+", 36), ("MAX472ESA", "SENSE-", 36),
    ("MAX472ESA", "VCC", None), ("PAC1942T-E/J6CX", "SENSE2-", 9),
    ("PAC1941T-E/J6CX", "VDD", None),
])
def test_current_sense_pin_ratings(value, fn, lim):
    table = sim_board.load_pin_ratings()
    got, src = sim_board.pin_rating({"ref": "U1", "value": value},
                                    {"fn": [fn]}, None, table)
    assert got == lim, (value, fn, src)
    if lim is not None:
        assert "not re-read" not in src


# An INA3221 on a 3.3 V VS measuring a 5 V rail is at spec (IN+/IN- to 26 V
# regardless of VS); an INA219 on a 28 V rail is over its 26 V and flags.
CSA_BOARD = [
    ("U1", "INA3221AIRGVR", "VQFN-16", {
        "3": ("+3V3", "VS"), "2": ("GND", "GND"),
        "6": ("/SNS2_P", "IN+2"), "7": ("/SNS2_N", "IN-2")}),
    ("R1", "0.01", "R_2512_6332Metric", {"1": ("+5V", ""),
                                         "2": ("/SNS2_P", "")}),
    ("R2", "0.01", "R_2512_6332Metric", {"1": ("/SNS2_P", ""),
                                         "2": ("/SNS2_N", "")}),
    ("R3", "10k", "R_0603_1608Metric", {"1": ("/SNS2_N", ""),
                                        "2": ("GND", "")}),
    ("U2", "INA219AIDCNR", "SOT-23-8", {
        "4": ("+3V3", "VS"), "3": ("GND", "GND"),
        "1": ("/SNS3_P", "IN+"), "2": ("/SNS3_N", "IN-")}),
    ("R4", "0.01", "R_2512_6332Metric", {"1": ("+28V", ""),
                                         "2": ("/SNS3_P", "")}),
    ("R5", "0.01", "R_2512_6332Metric", {"1": ("/SNS3_P", ""),
                                         "2": ("/SNS3_N", "")}),
    ("R6", "10k", "R_0603_1608Metric", {"1": ("/SNS3_N", ""),
                                        "2": ("GND", "")}),
]


def test_engine_current_sense_inputs(tmp_path, need_engine):
    (tmp_path / "kicad").mkdir()
    write_net(tmp_path / "kicad" / "c.net", CSA_BOARD)
    out = tmp_path / "sim.json"
    subprocess.run([sys.executable, str(SCRIPTS / "sim_board.py"),
                    "--workspace", str(tmp_path), "--out", str(out)],
                   capture_output=True, text=True, timeout=300)
    rep = json.loads(out.read_text(encoding="utf-8"))
    hits = [v for v in rep["violations"] if v["kind"] == "input_above_supply"]
    assert sorted(v["refs"][0] for v in hits) == ["U2", "U2"]
    assert all(v["rating"] == 26 and "SBOS448" in json.dumps(v) for v in hits)


def test_underdrive_at_rdson_vgs_is_at_spec():
    e = {"ref": "Q501", "type": "nmos", "roles": {"G": "/G"}}
    part = {"attrs": {"Vgs(th) max": "1V", "RDS(on)": "20mΩ@2.5V"}}
    vios, unknowns, row = [], [], {}
    sim_board.underdrive(e, part, {"io_high": 2.497}, row, vios, unknowns)
    assert vios == []
    sim_board.underdrive(e, part, {"io_high": 2.3}, row, vios, unknowns)
    assert [v["kind"] for v in vios] == ["mosfet_underdrive"]


def test_engine_soc_board(tmp_path, need_engine):
    (tmp_path / "kicad").mkdir()
    write_net(tmp_path / "kicad" / "s.net", SOC_BOARD)
    (tmp_path / "kicad" / "constraints.json").write_text(json.dumps(
        {"pin_modes": {"U6": {"PB11": "analog"}}}), encoding="utf-8")
    out = tmp_path / "sim.json"
    subprocess.run([sys.executable, str(SCRIPTS / "sim_board.py"),
                    "--workspace", str(tmp_path), "--out", str(out)],
                   capture_output=True, text=True, timeout=300)
    rep = json.loads(out.read_text(encoding="utf-8"))
    (led,) = rep["leds"]
    assert led["current_a"]["io_high"] == pytest.approx(3e-3, rel=0.25)
    assert _kinds(rep, "led_dark") == []
    assert _kinds(rep, "gpio_overcurrent") == []      # PB11 not driven
    assert _kinds(rep, "input_above_supply") == []    # INA240, HB
    assert _kinds(rep, "resistor_overpower") == []    # no 12 V on R30's end
    assert any(u["what"] == "/charger/CHG_VBUS_RC not a rail"
               for u in rep["known_unknowns"])
    assert any(u["ref"] == "U8" and u["what"].startswith("pin 2 (HB)")
               for u in rep["known_unknowns"])


# A USB-PD sink fed from VBUS_IN (21 V) whose GPIOs run from its own 3.3 V
# regulator (VDDD, only decoupled): its FAULT drives a 220 R + red LED. At
# its highest supply the io scenarios would push ~77 mA through a pin the
# datasheet gives 4 mA. A USB hub's SDA/SCL pins double as config straps
# (NON_REM1, CFG_SEL0) held by pull-downs: no I2C bus once pin_modes says
# input.
PDSINK_BOARD = [
    ("U101", "CYPD3177-24LQXQ", "QFN-24", {
        "1": ("VBUS_IN", "VBUS_IN"), "23": ("/VDDD", "VDDD"),
        "25": ("GND", "GND"), "9": ("/FAULT", "FAULT")}),
    ("C101", "1uF", "C_0402_1005Metric", {"1": ("/VDDD", ""),
                                          "2": ("GND", "")}),
    ("R109", "220", "R_0402_1005Metric", {"1": ("/FAULT", ""),
                                          "2": ("/FAULT_A", "")}),
    ("D104", "red", "LED_0603_1608Metric", {"1": ("GND", "K"),
                                            "2": ("/FAULT_A", "A")}),
    ("U701", "USB2514B", "QFN-36", {
        "5": ("+3V3", "VDD33"), "37": ("GND", "GND"),
        "22": ("/HUB_NON_REM1", "SDA/SMBDATA/NON_REM1"),
        "24": ("/HUB_CFG_SEL0", "SCL/SMBCLK/CFG_SEL0")}),
    ("R701", "10k", "R_0402_1005Metric", {"1": ("/HUB_NON_REM1", ""),
                                          "2": ("GND", "")}),
    ("R702", "10k", "R_0402_1005Metric", {"1": ("/HUB_CFG_SEL0", ""),
                                          "2": ("GND", "")}),
]
PDSINK_VOLTS = [{"net": "VBUS_IN", "voltage": 21}]
HUB_STRAPS = {"U701": {"22": "input", "24": "input"}}


def _pdsink(tmp_path, volts=PDSINK_VOLTS):
    b = sim_board.load_board(write_net(tmp_path / "pd.net", PDSINK_BOARD))
    rails = sim_board.resolve_rails(b, {}, {"voltages": volts}, None, {})
    return b, rails, sim_board.build_model(b, {}, rails)


def test_io_supply_table_rows_are_well_formed():
    rows = sim_board.load_io_supply()
    assert rows
    for r in rows:
        assert r["match"] and r["supply_pins"] and r["source"].strip()
        assert isinstance(r.get("verified"), bool)
        assert r.get("volts") is None or 0.8 <= float(r["volts"]) <= 5.5


def test_io_supply_from_table(tmp_path):
    b, rails, model = _pdsink(tmp_path)
    assert rails["VBUS_IN"][0] == 21 and "/VDDD" not in rails
    # no row: the old answer, FAULT driven at VBUS
    io, od = sim_board.io_nets(b, rails, model)
    assert io["/FAULT"] == 21 and "/FAULT" in od
    unknowns = []
    spec = sim_board.io_supply_specs(b, {}, rails, {},
                                     sim_board.load_io_supply(), unknowns)
    assert spec["U101"]["pins"] == ["23"] and spec["U101"]["volts"] == 3.3
    assert "002-25383" in spec["U101"]["source"] and unknowns == []
    io, od = sim_board.io_nets(b, rails, model, None, None, spec)
    assert io["/FAULT"] == 3.3 and "/FAULT" in od
    # the MPN in parts.json matches too; a declared VDDD rail wins over
    # the row's volts
    b, rails, model = _pdsink(tmp_path, PDSINK_VOLTS + [
        {"net": "/VDDD", "voltage": 3.0}])
    parts = {"U101": {"mpn": "CYPD3177-24LQXQT"}}
    b["comps"]["U101"]["value"] = "PD_SINK"
    spec = sim_board.io_supply_specs(b, parts, rails, {},
                                     sim_board.load_io_supply(), [])
    assert sim_board.ic_io_supply(b["comps"]["U101"], rails,
                                  spec["U101"]) == 3.0
    assert sim_board.io_nets(b, rails, model, None, None,
                             spec)[0]["/FAULT"] == 3.0


def test_io_supply_row_that_does_not_fit(tmp_path):
    b, rails, _ = _pdsink(tmp_path)
    yml = tmp_path / "io_supply.yaml"
    for pins, volts, why in (("^VCCIO$", "3.3", "no pin matches"),
                             ("^VDDD$", "null", "on no rail")):
        yml.write_text(f"parts:\n  - match: '^CYPD3177'\n"
                       f"    supply_pins: '{pins}'\n    volts: {volts}\n"
                       f"    source: test\n    verified: false\n",
                       encoding="utf-8")
        unknowns = []
        assert sim_board.io_supply_specs(
            b, {}, rails, {}, sim_board.load_io_supply(yml), unknowns) == {}
        (u,) = unknowns
        assert u["ref"] == "U101" and why in u["why"], u


def test_io_supply_pinned(tmp_path):
    pinned = sim_board.io_supply_pinned({"io_supply": {"U101": "VDDD"}},
                                        ["U101=23"])
    assert pinned == {"U101": ("23", "--io-supply")}
    assert sim_board.io_supply_pinned(None) == {}
    for cons, cli in (({"io_supply": {"U101": ""}}, None),
                      ({"io_supply": ["U101"]}, None),
                      (None, ["U101"]), (None, ["=VDDD"]), (None, ["U1="])):
        with pytest.raises(sim_board.checklib.CheckError):
            sim_board.io_supply_pinned(cons, cli)
    b, rails, model = _pdsink(tmp_path, PDSINK_VOLTS + [
        {"net": "/VDDD", "voltage": 3.3}])
    spec = sim_board.io_supply_specs(
        b, {}, rails, {"U101": ("vddd", "constraints io_supply")}, [], [])
    assert spec["U101"] == {"pins": ["23"], "volts": None,
                            "source": "constraints io_supply"}
    assert sim_board.io_nets(b, rails, model, None, None,
                             spec)[0]["/FAULT"] == 3.3
    b, rails, _ = _pdsink(tmp_path)
    for ref, key, msg in (("U9", "VDDD", "no IC U9"),
                          ("R109", "1", "no IC R109"),
                          ("U101", "VCCQ", "has no pin VCCQ"),
                          ("U101", "VDDD", "/VDDD, not a rail")):
        with pytest.raises(sim_board.checklib.CheckError, match=msg):
            sim_board.io_supply_specs(b, {}, rails,
                                      {ref: (key, "--io-supply")}, [], [])


def test_i2c_strap_pins_sharing_the_name(tmp_path):
    b, rails, model = _pdsink(tmp_path)
    opts = {"i2c_cb_pf": None, "i2c_khz": 400}
    vios, facts = [], {}
    sim_board.i2c_findings(b, rails, model, opts, vios, [], facts)
    # pulled down with no pull-up: straps, not judged (#511; before it, one
    # IC and no connector made them ports with no peer, #1063)
    assert vios == [] and facts["i2c"] == []
    assert sorted(n for x in facts["i2c_straps"] for n in x["nets"]) == \
        ["/HUB_CFG_SEL0", "/HUB_NON_REM1"]
    vios = []
    sim_board.i2c_findings(b, rails, model, {**opts, "pin_modes":
                           sim_board.pin_modes({"pin_modes": HUB_STRAPS},
                                               None)}, vios, [], facts)
    assert vios == [] and facts["i2c"] == []


def test_engine_pd_sink_fault_led(tmp_path, need_engine):
    (tmp_path / "kicad").mkdir()
    net = write_net(tmp_path / "kicad" / "pd.net", PDSINK_BOARD)
    (tmp_path / "kicad" / "constraints.json").write_text(json.dumps(
        {"voltages": PDSINK_VOLTS, "pin_modes": HUB_STRAPS}),
        encoding="utf-8")
    out = tmp_path / "sim.json"
    subprocess.run([sys.executable, str(SCRIPTS / "sim_board.py"),
                    "--workspace", str(tmp_path), "--out", str(out)],
                   capture_output=True, text=True, timeout=300)
    rep = json.loads(out.read_text(encoding="utf-8"))
    (led,) = rep["leds"]
    # (3.3 - ~1.9) / (220 + 25 + RS), not (21 - ~2) / ...
    assert 4e-3 < led["current_a"]["io_high"] < 7e-3
    for kind in ("led_overcurrent", "resistor_overpower",
                 "i2c_pullup_missing"):
        assert _kinds(rep, kind) == [], kind
    # 5.6 mA sourced is past the 4 mA IOH the CYPD3177's high level is
    # specified at (reference/gpio_drive.yaml), not the flat 20 mA (#1063)
    (g,) = {v["scenario"]: v for v in _kinds(rep, "gpio_overcurrent")
            }.values()
    assert g["refs"] == ["U101"] and g["limit_a"] == 0.004
    assert "IOH" in g["msg"] and "002-25383" in g["msg"]
    (s,) = rep["io_supply"]
    assert (s["ref"], s["pins"], s["volts"]) == ("U101", ["23"], 3.3)
    cp = subprocess.run([sys.executable, str(SCRIPTS / "sim_board.py"),
                         "--net", str(net), "--io-supply", "U101"],
                        capture_output=True, text=True, timeout=300)
    assert cp.returncode == 2 and "want REF=PIN" in cp.stdout


# --- #1063: per-pin drive limits, and an I2C bus needs a peer ------------

def test_gpio_drive_table_rows_are_well_formed():
    rows = sim_board.load_gpio_drive()
    assert rows
    for r in rows:
        assert r["match"] and r["source"].strip()
        assert isinstance(r.get("verified"), bool)
        for k in ("ioh_a", "iol_a"):
            assert r.get(k) is None or 0 < float(r[k]) <= 0.1


DRIVE_BOARD = [
    ("U1", "MCU", "QFN-24", {"1": ("+3V3", "VDD"), "2": ("GND", "GND"),
                             "3": ("/LED", "IO3")}),
    ("R1", "1k", "R_0402_1005Metric", {"1": ("/LED", ""),
                                       "2": ("/LED_A", "")}),
    ("D1", "red", "LED_0603_1608Metric", {"1": ("GND", "K"),
                                          "2": ("/LED_A", "A")})]


def _drive_run(tmp_path, mcu, r_led):
    """(gpio_overcurrent findings, unknowns) for an IC pin driving an LED
    through r_led from 3.3 V."""
    rows = [(r, mcu if r == "U1" else v, f,
             p) if r != "R1" else (r, r_led, f, p)
            for r, v, f, p in DRIVE_BOARD]
    net = write_net(tmp_path / f"d{r_led}.net", rows)
    out = net.with_suffix(".json")
    subprocess.run([sys.executable, str(SCRIPTS / "sim_board.py"),
                    "--net", str(net), "--out", str(out)],
                   capture_output=True, text=True, timeout=300)
    rep = json.loads(out.read_text(encoding="utf-8"))
    return _kinds(rep, "gpio_overcurrent"), rep["known_unknowns"]


def test_gpio_limit_from_table_quiet_and_fault(tmp_path, need_engine):
    # quiet: a CYPD3177 pin sourcing ~1.4 mA is inside its 4 mA IOH
    got, unknowns = _drive_run(tmp_path, "CYPD3177-24LQXQ", "1k")
    assert got == []
    assert not any(u["what"] == "GPIO drive limit" for u in unknowns)
    # fault kept: ~5.6 mA is past it, though under the flat 20 mA
    got, _ = _drive_run(tmp_path, "CYPD3177-24LQXQ", "220")
    (v,) = {x["scenario"]: x for x in got if x["scenario"] == "io_high"
            }.values()
    assert v["refs"] == ["U1"] and v["limit_a"] == 0.004
    assert "IOH" in v["msg"]


def test_gpio_limit_without_row_assumed_and_said(tmp_path, need_engine):
    # no row: ~11 mA passes the flat 20 mA, and the report says it assumed
    got, unknowns = _drive_run(tmp_path, "SOME_MCU", "100")
    assert got == []
    (u,) = [u for u in unknowns if u["what"] == "GPIO drive limit"]
    assert u["ref"] == "U1" and "20 mA" in u["why"]
    # fault kept: ~30 mA is still flagged against it
    got, _ = _drive_run(tmp_path, "SOME_MCU", "22")
    assert got and all(v["limit_source"] == "assumed" and "20 mA" in v["msg"]
                       for v in got)


def _i2c_board(tmp_path, extra, r_pull="2.2k"):
    rows = [("U1", "SOC", "QFN-24", {"1": ("+3V3", "VDD"), "2": ("GND", "GND"),
                                     "3": ("/I2C0_SDA", "SDA"),
                                     "4": ("/I2C0_SCL", "SCL")}),
            ("R1", r_pull, "R_0402_1005Metric", {"1": ("+3V3", ""),
                                                 "2": ("/I2C0_SDA", "")}),
            ("R2", r_pull, "R_0402_1005Metric", {"1": ("+3V3", ""),
                                                 "2": ("/I2C0_SCL", "")})]
    b = sim_board.load_board(write_net(tmp_path / "i.net", rows + extra))
    rails = sim_board.resolve_rails(
        b, {}, {"voltages": [{"net": "+3V3", "voltage": 3.3}]}, None, {})
    model = sim_board.build_model(b, {}, rails)
    vios, unknowns, facts = [], [], {}
    sim_board.i2c_findings(b, rails, model, {"i2c_cb_pf": None,
                                             "i2c_khz": 400},
                           vios, unknowns, facts)
    return vios, unknowns, facts


PEER = ("U2", "SENSOR", "DFN-8", {"1": ("+3V3", "VDD"), "2": ("GND", "GND"),
                                  "3": ("/I2C0_SDA", "SDA"),
                                  "4": ("/I2C0_SCL", "SCL")})


def test_i2c_one_ic_net_is_not_a_bus(tmp_path):
    # quiet: an SoC port pulled up with nothing else on it, even with a
    # pull-up too strong for a bus
    vios, unknowns, facts = _i2c_board(tmp_path, [], "330")
    assert vios == [] and facts["i2c"] == []
    assert [x["ic"] for x in facts["i2c_no_peer"]] == ["U1", "U1"]
    (u,) = [u for u in unknowns if u["what"] == "I2C port with no peer"]
    assert u["ref"] == "U1" and "unused or off-board" in u["why"]
    # and with no pull-up at all
    rows = [PEER[:3] + ({"1": ("+3V3", "VDD"), "2": ("GND", "GND"),
                         "3": ("/X_SDA", "SDA")},)]
    vios, _, _ = _i2c_board(tmp_path, rows)
    assert [v for v in vios if v["net"] == "/X_SDA"] == []


def test_i2c_bus_with_a_peer_still_judged(tmp_path):
    # fault kept: a second IC makes it a bus, and 330 ohm is too strong
    vios, _, facts = _i2c_board(tmp_path, [PEER], "330")
    assert {v["kind"] for v in vios} == {"i2c_pullup_strong"}
    assert facts["i2c_no_peer"] == []
    # fault kept: a bus with a peer and no pull-up
    rows = [PEER, ("U3", "SENSOR", "DFN-8", {
        "1": ("+3V3", "VDD"), "2": ("GND", "GND"), "3": ("/B_SDA", "SDA")})]
    rows[0] = PEER[:3] + ({**PEER[3], "5": ("/B_SDA", "SDA")},)
    vios, _, _ = _i2c_board(tmp_path, rows)
    assert [v["kind"] for v in vios if v["net"] == "/B_SDA"] == \
        ["i2c_pullup_missing"]


def test_i2c_off_board_bus_judged_with_capacitance_unknown(tmp_path):
    j = ("J5", "Conn_01x04", "PinHeader_1x04", {"1": ("/I2C0_SDA", ""),
                                                "2": ("/I2C0_SCL", "")})
    vios, unknowns, facts = _i2c_board(tmp_path, [j], "330")
    assert {v["kind"] for v in vios} == {"i2c_pullup_strong"}
    assert all(r["off_board"] == ["J5"] for r in facts["i2c"])
    assert any(u["what"] == "I2C bus capacitance off the board"
               and "J5" in u["why"] for u in unknowns)



# --- #511: an SDA/SCL pin held low by a pull-down is a strap, not a bus ---

def _hub_strap(tmp_path, extra, down="100k"):
    """USB2514B-style: U5's SCL/SMBCLK/CFG_SEL0 on /HUB_CFG_SEL0, pulled to
    GND through R5, plus whatever extra puts on the net."""
    hub = ("U5", "USB2514B", "QFN-36", {
        "1": ("+3V3", "VDD33"), "2": ("GND", "GND"),
        "24": ("/HUB_CFG_SEL0", "SCL/SMBCLK/CFG_SEL0")})
    rows = [hub]
    if down:
        rows.append(("R5", down, "R_0402_1005Metric",
                     {"1": ("/HUB_CFG_SEL0", ""), "2": ("GND", "")}))
    return _i2c_board(tmp_path, rows + extra)


GPIO = ("U6", "MCU", "QFN-24", {"1": ("+3V3", "VDD"), "2": ("GND", "GND"),
                                "7": ("/HUB_CFG_SEL0", "PA5")})
TP = ("TP5", "TestPoint", "TestPoint_Pad_D1.0mm",
      {"1": ("/HUB_CFG_SEL0", "1")})


@pytest.mark.parametrize("extra", [[GPIO], [TP], [GPIO, TP]],
                         ids=["gpio", "testpoint", "both"])
def test_i2c_strap_with_a_second_ic_or_test_point_is_quiet(tmp_path, extra):
    # quiet twin: on main the GPIO (or test point) made this a "bus" and it
    # fired i2c_pullup_missing on the hub's strap
    vios, unknowns, facts = _hub_strap(tmp_path, extra)
    assert [v for v in vios if v["net"] == "/HUB_CFG_SEL0"] == []
    (x,) = facts["i2c_straps"]
    assert x["nets"] == ["/HUB_CFG_SEL0"] and x["pulldowns"] == ["R5"]
    (u,) = [u for u in unknowns if u["what"] == "I2C pin used as a strap"]
    assert "R5" in u["why"] and "not judged" in u["why"]


def test_i2c_two_sda_scl_pins_with_no_pull_up_still_fire(tmp_path):
    # planted fault kept: a pull-down does not make a real bus a strap when
    # two SDA/SCL pins share it and nothing pulls it up
    peer = ("U7", "SENSOR", "DFN-8", {"1": ("+3V3", "VDD"), "2": ("GND", "GND"),
                                      "3": ("/HUB_CFG_SEL0", "SCL")})
    vios, _, facts = _hub_strap(tmp_path, [peer])
    assert [v["kind"] for v in vios if v["net"] == "/HUB_CFG_SEL0"] == \
        ["i2c_pullup_missing"]
    assert facts["i2c_straps"] == []


def test_i2c_no_pull_down_and_a_gpio_still_fires(tmp_path):
    # fault kept: with no pull-down nothing says strap, and a hub SCL shared
    # with a GPIO and no pull-up is still judged as before
    vios, _, facts = _hub_strap(tmp_path, [GPIO], down=None)
    assert [v["kind"] for v in vios if v["net"] == "/HUB_CFG_SEL0"] == \
        ["i2c_pullup_missing"]
    assert facts["i2c_straps"] == []


# --- #1232: cap_dc_bias warns on a rail's stated floor, not a flat 50 % ---

def _cap_row(ref, c_nom, keeps_low, rail="+5V", vdc=6.0, rating=10.0,
             diel="X5R"):
    return {"ref": ref, "net": rail, "rail": rail, "vdc": vdc,
            "rating_v": rating, "dielectric": diel,
            "class2": diel.startswith("X"), "c_nom": c_nom,
            "c_eff": c_nom * (keeps_low + 0.1),
            "c_eff_low": c_nom * keeps_low, "fraction": keeps_low + 0.1,
            "curve": "generic"}


FLOOR_5V = {"+5V": {"min_c": 10e-6, "refs": [],
                    "source": "constraints power[].min_cout"}}


def test_cap_dc_bias_planted_floor_missed_warns():
    # planted: two 10 uF parts keeping 40 % on the low curve give 8 uF,
    # under the rail's stated 10 uF effective floor
    vios, unknowns = [], []
    sim_board.cap_dc_bias_findings(
        [_cap_row("C1", 10e-6, 0.4), _cap_row("C2", 10e-6, 0.4)],
        FLOOR_5V, vios, unknowns)
    assert [(v["refs"], v["severity"]) for v in vios] == [
        (["C1"], "warning"), (["C2"], "warning")]
    assert "constraints power[].min_cout" in vios[0]["msg"]
    assert vios[0]["floor_source"] == "constraints power[].min_cout"
    assert unknowns == []


def test_cap_dc_bias_quiet_twin_floor_held_is_info():
    # quiet twin: the same parts and DC, three of them, hold the floor
    # (12 uF low): the 50 % screen still notes them, never as a warning
    vios, unknowns = [], []
    sim_board.cap_dc_bias_findings(
        [_cap_row(f"C{i}", 10e-6, 0.4) for i in (1, 2, 3)],
        FLOOR_5V, vios, unknowns)
    assert {v["severity"] for v in vios} == {"info"}
    assert all("holds its" in v["msg"] for v in vios)
    assert unknowns == []


def test_cap_dc_bias_no_floor_is_info_and_not_judged():
    # the old flat line warned here (50 % of rating, under half nominal);
    # with no floor stated it is info and the rail is listed as not judged
    vios, unknowns = [], []
    sim_board.cap_dc_bias_findings(
        [_cap_row("C1", 10e-6, 0.4), _cap_row("C2", 10e-6, 0.4),
         _cap_row("C3", 1e-6, 0.9, vdc=1.0)], {}, vios, unknowns)
    assert [(v["refs"], v["severity"]) for v in vios] == [
        (["C1"], "info"), (["C2"], "info")]
    (u,) = unknowns
    assert u["what"] == "effective-capacitance floor" and "+5V" in u["why"]


def test_cap_dc_bias_class1_and_unrated_parts_count_toward_the_floor():
    # a C0G part adds to the rail's total but is never itself screened;
    # with it the rail holds its floor, so no warning
    vios, unknowns = [], []
    rows = [_cap_row("C1", 10e-6, 0.4), _cap_row("C2", 10e-6, 0.4),
            _cap_row("C3", 3e-6, 1.0, diel="C0G", rating=None)]
    sim_board.cap_dc_bias_findings(rows, FLOOR_5V, vios, unknowns)
    assert {v["severity"] for v in vios} == {"info"}
    assert all(v["refs"] != ["C3"] for v in vios)


def test_run_takes_the_floor_from_constraints(tmp_path, monkeypatch):
    # run() hands analyze the rail floors mlcc_dcbias would use
    net = write_net(tmp_path / "f.net", [
        ("C1", "10u", "C_0805_2012Metric", {"1": ("+5V", ""),
                                            "2": ("GND", "")})])
    cons = tmp_path / "constraints.json"
    cons.write_text(json.dumps({
        "voltages": [{"net": "+5V", "voltage": 5.0}],
        "power": [{"net": "+5V", "min_cout": 4.7e-6}]}), encoding="utf-8")
    seen = {}

    def stop(board, parts, rails, opts):
        seen.update(opts)
        raise sim_board.checklib.CheckError("stop")

    monkeypatch.setattr(sim_board.env, "find_ngspice_dll",
                        lambda: tmp_path / "libngspice.so")
    monkeypatch.setattr(sim_board, "analyze", stop)
    sim_board.main(["--net", str(net), "--constraints", str(cons),
                    "--out", str(tmp_path / "r.json")])
    assert seen["cap_floors"]["+5V"]["min_c"] == 4.7e-6


# #1261: an error turns into not_judged with a reason, never a crash.
# RC1/C11 is a series RC with a 0 F cap (the planted shape: lofence's "0nF"),
# RC2/C12 the quiet twin with a real cap; Y1 sits on two 0 F load caps.
RC_BOARD = [
    ("U1", "MCU", "QFN-32", {"1": ("+3V3", "VDD"), "2": ("GND", "GND"),
                             "3": ("/A", "IO3"), "4": ("/B", "IO4"),
                             "5": ("/XI", "XI"), "6": ("/XO", "XO")}),
    ("R21", "10k", "R_0402_1005Metric", {"1": ("/A", ""), "2": ("/AF", "")}),
    ("C11", "0nF", "C_0402_1005Metric", {"1": ("/AF", ""), "2": ("GND", "")}),
    ("R22", "10k", "R_0402_1005Metric", {"1": ("/B", ""), "2": ("/BF", "")}),
    ("C12", "100nF", "C_0402_1005Metric", {"1": ("/BF", ""),
                                           "2": ("GND", "")}),
    ("Y1", "16MHz", "Crystal_SMD_3225-4Pin_3.2x2.5mm",
     {"1": ("/XI", ""), "3": ("/XO", "")}),
    ("C13", "0nF", "C_0402_1005Metric", {"1": ("/XI", ""), "2": ("GND", "")}),
    ("C14", "0nF", "C_0402_1005Metric", {"1": ("/XO", ""), "2": ("GND", "")}),
]


def _rc_facts(tmp_path):
    board = sim_board.load_board(str(write_net(tmp_path / "rc.net",
                                               RC_BOARD)))
    rails = {"+3V3": (3.3, "name")}
    model = sim_board.build_model(board, {}, rails)
    facts, not_judged = {}, []
    sim_board.passive_facts(board, rails, model, facts, {}, not_judged)
    return facts, not_judged


def test_passive_facts_zero_cap_is_no_rc_network(tmp_path):
    facts, _ = _rc_facts(tmp_path)
    rs = {r["r"] for r in facts["rc_networks"]}
    assert "R21" not in rs          # planted: 0 F, no corner, no crash
    assert "R22" in rs              # quiet twin: 10k/100n still a low-pass
    row = next(r for r in facts["rc_networks"] if r["r"] == "R22")
    assert row["fc_hz"] == pytest.approx(159.15, rel=1e-3)


def test_passive_facts_zero_load_cap_crystal_not_judged(tmp_path):
    facts, not_judged = _rc_facts(tmp_path)
    assert facts["crystals"] == []
    assert any(s.startswith("Y1:") and "0 F" in s for s in not_judged)


def _fake_dll(monkeypatch, tmp_path):
    monkeypatch.setattr(sim_board.env, "find_ngspice_dll",
                        lambda: tmp_path / "libngspice.so")


def test_engine_failure_is_not_judged_not_a_crash(tmp_path, monkeypatch,
                                                   capsys):
    # identidem's shape: ngspice leaves no vectors. The run completes, the
    # passes that need no operating point keep their facts.
    def no_vectors(decks, dll, timeout):
        raise sim_board.checklib.CheckError(
            "engine: circuit 0: NgSpiceCommandError: Error: There are no "
            "vectors currently active.")

    _fake_dll(monkeypatch, tmp_path)
    monkeypatch.setattr(sim_board, "run_engine", no_vectors)
    net = write_net(tmp_path / "rc.net", RC_BOARD)
    out = tmp_path / "r.json"
    rc = sim_board.main(["--net", str(net), "--out", str(out)])
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 0 and rep["status"] == "pass"
    assert any("no vectors" in s and "not judged" in s
               for s in rep["not_judged"])
    assert {r["r"] for r in rep["rc_networks"]} == {"R22"}


def test_nothing_to_simulate_skips_the_engine(tmp_path, monkeypatch):
    # a connector-only board: no deck goes to ngspice at all
    def boom(*_a):
        raise AssertionError("engine run on an empty deck")

    _fake_dll(monkeypatch, tmp_path)
    monkeypatch.setattr(sim_board, "run_engine", boom)
    net = write_net(tmp_path / "j.net", [
        ("J1", "Conn_01x02", "PinHeader_1x02_P2.54mm_Vertical",
         {"1": ("+3V3", ""), "2": ("GND", "")})])
    out = tmp_path / "r.json"
    assert sim_board.main(["--net", str(net), "--out", str(out)]) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert "nothing_to_simulate" not in rep
    assert "no passive or modelled device" in rep["nothing_to_judge"]
    assert "not_judged" not in rep


def test_engine_rc_board_runs_clean(tmp_path, need_engine):
    # the quiet twin of the engine failure: the same board through ngspice
    net = write_net(tmp_path / "rc.net", RC_BOARD)
    out = tmp_path / "r.json"
    sim_board.main(["--net", str(net), "--out", str(out)])
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["status"] != "error"
    assert not any("not simulated" in s for s in rep.get("not_judged", []))
    assert "node_volts" in rep


def test_load_parts_matches_an_mpn_row_and_a_refdes_row_wins(tmp_path):
    # #1260: a row with no refdes reaches every comp whose netlist MPN is
    # the same part (case, spaces, dashes and dots aside); a refdes row
    # naming the comp wins; a row matching nothing is listed
    net = tmp_path / "m.net"
    net.write_text("""(export (version "E") (components
 (comp (ref "C1") (value "10uF") (footprint "C_0603")
  (fields (field (name "MPN") "CL10A106KP8NNNC")))
 (comp (ref "C2") (value "10uF") (footprint "C_0603")
  (property (name "MPN") (value "CL10A106KP8NNNC")))
 (comp (ref "C3") (value "10uF") (footprint "C_0603")
  (property (name "MPN") (value "CL10A106KP8NNNC"))))
 (nets (net (code "1") (name "+3V3")
  (node (ref "C1") (pin "1")) (node (ref "C2") (pin "1"))
  (node (ref "C3") (pin "1")))
 (net (code "2") (name "GND")
  (node (ref "C1") (pin "2")) (node (ref "C2") (pin "2"))
  (node (ref "C3") (pin "2")))))
""", encoding="utf-8")
    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / "parts.json").write_text(json.dumps({"parts": [
        {"mpn": "cl10a106kp8-nnnc", "attributes": [
            {"name": "Voltage Rated", "value": "10V"}]},
        {"mpn": "OTHER", "refdes": ["C3"], "attributes": [
            {"name": "Voltage Rated", "value": "25V"}]},
        {"mpn": "NOPE123", "attributes": []}]}))
    board = sim_board.load_board(net)
    assert board["comps"]["C2"]["mpn"] == "CL10A106KP8NNNC"
    missed: list = []
    rows = sim_board.load_parts(parts, board, missed)
    assert rows["C1"]["matched_by"] == "mpn"
    assert rows["C2"]["attrs"]["Voltage Rated"] == "10V"
    assert rows["C3"]["mpn"] == "OTHER" and "matched_by" not in rows["C3"]
    assert [m["mpn"] for m in missed] == ["NOPE123"]
    # without the board, only refdes rows are known (old callers)
    assert set(sim_board.load_parts(parts)) == {"C3"}


# An STM32 whose LED hangs off PC14-OSC32_IN, the way its KiCad symbol
# names the pin (#465). Through 10 R the pin is asked for ~140 mA; through
# 1k (the quiet twin) it lights the LED inside the pin's limit. PG10-NRST
# is the reset pin: an LED there is not driven as a GPIO.
def _stm32_alt(r_led):
    return [
        ("U9", "STM32G031K8T6", "LQFP-32", {
            "4": ("+3V3", "VDD"), "5": ("GND", "VSS"),
            "2": ("/LED", "PC14-OSC32_IN"), "6": ("/NRST", "PG10-NRST")}),
        ("R40", r_led, "R_0402_1005Metric", {"1": ("/LED", ""),
                                             "2": ("/LED_A", "")}),
        ("D40", "red", "LED_0603_1608Metric", {"1": ("GND", "K"),
                                               "2": ("/LED_A", "A")}),
        ("R41", "1k", "R_0402_1005Metric", {"1": ("/NRST", ""),
                                            "2": ("/RST_A", "")}),
        ("D41", "red", "LED_0603_1608Metric", {"1": ("GND", "K"),
                                               "2": ("/RST_A", "A")}),
    ]


def _run_sim(tmp_path, board):
    (tmp_path / "kicad").mkdir()
    write_net(tmp_path / "kicad" / "s.net", board)
    out = tmp_path / "sim.json"
    subprocess.run([sys.executable, str(SCRIPTS / "sim_board.py"),
                    "--workspace", str(tmp_path), "--out", str(out)],
                   capture_output=True, text=True, timeout=300)
    return json.loads(out.read_text(encoding="utf-8"))


def test_stm32_alt_suffix_pin_driven_planted(tmp_path, need_engine):
    rep = _run_sim(tmp_path, _stm32_alt("10"))
    assert any(v["net"] == "/LED" for v in _kinds(rep, "gpio_overcurrent"))
    assert not any(v["net"] == "/NRST"
                   for v in _kinds(rep, "gpio_overcurrent"))


def test_stm32_alt_suffix_pin_quiet_twin(tmp_path, need_engine):
    rep = _run_sim(tmp_path, _stm32_alt("1k"))
    assert _kinds(rep, "gpio_overcurrent") == []
    led = next(x for x in rep["leds"] if x["ref"] == "D40")
    assert led["current_a"]["io_high"] > 5e-4
    rst = [x for x in rep["leds"] if x["ref"] == "D41"]
    assert all(x["current_a"]["io_high"] < 1e-6 for x in rst)
