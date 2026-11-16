"""check_si: which nets need signal integrity, and what it asks of them.

Synthetic 4-layer boards (In1 GND, In2 GND) carry one fast net each; the
goldens must come out clean and the si-unterminated-clock mutant caught.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_si  # noqa: E402
import impedance  # noqa: E402

GOLDEN = REPO / "tests" / "golden"

PLANES = "".join(f"""  (zone (net "GND") (layer "{ly}")
    (polygon (pts (xy 0 0) (xy 120 0) (xy 120 40) (xy 0 40)))
    (filled_polygon (layer "{ly}")
      (pts (xy 0 0) (xy 120 0) (xy 120 40) (xy 0 40))))
""" for ly in ("In1.Cu", "In2.Cu"))


def seg(x0, y0, x1, y1, net, w=0.2, layer="F.Cu"):
    return (f'  (segment (start {x0} {y0}) (end {x1} {y1}) (width {w}) '
            f'(layer "{layer}") (net "{net}"))\n')


def fp(ref, x, y, value, pads):
    """pads: [(number, dx, net)]"""
    body = "".join(f'    (pad "{n}" smd rect (at {dx} 0) (size 0.6 0.6) '
                   f'(layers "F.Cu") (net "{net}"))\n' for n, dx, net in pads)
    return (f'  (footprint "t:{ref}" (at {x} {y}) (layer "F.Cu")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'    (property "Value" "{value}" (at 0 0 0) (layer "F.Fab"))\n'
            f'{body}  )\n')


def board(tmp_path, body, cons=None):
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal)
    (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 120 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{PLANES}{body})
"""
    p = tmp_path / "b.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    argv = ["--pcb", str(p)]
    if cons is not None:
        c = tmp_path / "constraints.json"
        c.write_text(json.dumps(cons), encoding="utf-8")
        argv += ["--constraints", str(c)]
    payload, _ = check_si.run(argv)
    return payload


def kinds(payload):
    return sorted((v["kind"], v["net"]) for v in payload["violations"])


# ------------------------------------------------------------ classing

@pytest.mark.parametrize("net,cls", [
    ("/ETH_CLK", "osc_clock"), ("/dac/DAC_MCLK", "osc_clock"),
    ("/MCO", "osc_clock"), ("/I2S_BCK", "clock"), ("/LCD_SCLK", "clock"),
    ("/RMII_TXD0", "rmii"), ("/RMII_CRS_DV", "rmii"), ("/I2S_LRCK", "i2s"),
    ("/I2S_DOUT_MAIN", "i2s"), ("/LCD_MOSI", "spi"), ("/USB_DP", "usb_fs"),
    ("/ethernet/ETH_TXN", "eth_mdi"), ("/I2C_SCL", None), ("/MDC", None),
    ("/mcu/U0TXD", None), ("/DAC_EN", None), ("GND", None),
    # 1000BASE-T pairs A-D (#510), chip and line side
    ("/MDI1_A_P", "eth_mdi"), ("/MDI5_D_N", "eth_mdi"), ("/BI_DA+", "eth_mdi"),
    ("/BI_DD-", "eth_mdi"), ("/eth/TRD0_P", "eth_mdi"), ("/TRD3N", "eth_mdi"),
    ("/TXRX1P_A", "eth_mdi"), ("/TXRX4M_D", "eth_mdi"), ("/MDI0+", "eth_mdi"),
    ("/ports/L1_A_P", "eth_mdi"), ("/DAP", None), ("/L1_A", None),
    ("/MDI1_E_P", None), ("/MDIO", None)])
def test_classify_by_name(net, cls):
    assert check_si.classify(net) == cls


@pytest.mark.parametrize("value,ohm", [
    ("33R", 33.0), ("4R7", 4.7), ("1k", 1000.0), ("0R", 0.0), ("0", 0.0),
    ("22", 22.0), ("2k2", 2200.0), ("49.9", 49.9), (None, None), ("DNP", None)])
def test_ohms(value, ohm):
    assert check_si.ohms(value) == ohm


def test_dac_enable_is_not_an_analog_victim():
    assert check_si.victim_class("/DAC_EN", {}, set()) == "digital"
    assert check_si.victim_class("/dac/DACL_P", {}, set()) == "analog"
    assert check_si.victim_class("+3V3", {}, set()) == "ignore"


# ------------------------------------------------------------ coupling

def test_coupling_falls_with_gap_and_microstrip_fext_is_negative():
    kb = []
    for s in (0.1, 0.2, 0.4, 0.8):
        c = impedance.coupling(impedance.field_microstrip(0.2, 0.2, 0.035, 4.2, s, even=True))
        kb.append(c["kb"])
        assert c["kf"] < 0          # inductive coupling dominates in microstrip
    assert kb == sorted(kb, reverse=True)
    assert 0.05 < kb[1] < 0.2       # s = w: Bogatin's ~5-15% saturated NEXT


def test_coupling_homogeneous_stripline_has_no_fext():
    c = impedance.coupling(impedance.field_stripline(0.15, 0.3, 0.3, 0.0175, 4.2, 4.2,
                                                     0.15, even=True))
    assert abs(c["kC"] - c["kL"]) < 0.01
    assert abs(c["kf"]) < 0.005


# ------------------------------------------------------------ the rules

def clock_line(net, x0=5, x1=95, y=10):
    return seg(x0, y, x1, y, net)


def test_long_mcu_clock_without_series_r_warns(tmp_path):
    body = (fp("U1", 4, 10, "MCU", [("1", 0, "/SPI_CLK")])
            + fp("U2", 96, 10, "FLASH", [("1", 0, "/SPI_CLK")])
            + clock_line("/SPI_CLK", 4, 96))
    got = board(tmp_path, body)
    assert ("si_unterminated_clock", "/SPI_CLK") in kinds(got)
    net = next(n for n in got["nets"] if n["net"] == "/SPI_CLK")
    assert net["transmission_line"] and net["class"] == "clock"
    assert 40 < net["critical_mm"] < 80


def teardrop(net, x, y, layer="F.Cu"):
    """A KiCad teardrop zone: `(attr (teardrop ...))`, filled, on `net`."""
    pts = f"(xy {x} {y - 0.3}) (xy {x + 1} {y}) (xy {x} {y + 0.3})"
    return (f'  (zone (net "{net}") (layer "{layer}") (priority 30001)\n'
            f'    (attr (teardrop (type padvia)))\n'
            f'    (polygon (pts {pts}))\n'
            f'    (filled_polygon (layer "{layer}") (pts {pts})))\n')


def test_teardropped_net_is_still_checked(tmp_path):
    """#633: a teardrop is a zone, and every zone's net was taken for a
    power net and skipped, so a board with teardrops went unchecked."""
    body = (fp("U1", 4, 10, "MCU", [("1", 0, "/SPI_CLK")])
            + fp("U2", 96, 10, "FLASH", [("1", 0, "/SPI_CLK")])
            + clock_line("/SPI_CLK", 4, 96)
            + teardrop("/SPI_CLK", 4, 10) + teardrop("/SPI_CLK", 95, 10))
    got = board(tmp_path, body)
    assert ("si_unterminated_clock", "/SPI_CLK") in kinds(got)
    # a real pour on the net still makes it a power net
    pour = ('  (zone (net "/SPI_CLK") (layer "B.Cu")\n'
            '    (polygon (pts (xy 10 20) (xy 20 20) (xy 20 30)))\n'
            '    (filled_polygon (layer "B.Cu")'
            ' (pts (xy 10 20) (xy 20 20) (xy 20 30))))\n')
    assert ("si_unterminated_clock", "/SPI_CLK") not in kinds(
        board(tmp_path, body + pour))


def test_geom_reads_teardrop_attr():
    import geom
    bg = geom.load_board(REPO / "tests" / "fixtures" / "teardrops"
                         / "led_dropped.kicad_pcb")
    drops = [z for z in bg.zones_of() if z.teardrop]
    assert {z.net for z in drops} == {"GND", "+5V"}
    assert len(drops) == 3
    assert len(drops) < len(bg.zones_of())    # the GND pour is not a drop


def test_short_clock_is_lumped(tmp_path):
    body = (fp("U1", 4, 10, "MCU", [("1", 0, "/SPI_CLK")])
            + fp("U2", 30, 10, "FLASH", [("1", 0, "/SPI_CLK")])
            + clock_line("/SPI_CLK", 4, 30))
    got = board(tmp_path, body)
    assert got["violations"] == []
    assert got["nets"][0]["transmission_line"] is False


def test_series_r_terminates_and_its_value_is_judged(tmp_path):
    def body(value):
        return (fp("U1", 4, 10, "MCU", [("1", 0, "/SPI_CLK_SRC")])
                + fp("R1", 6, 10, value, [("1", -0.5, "/SPI_CLK_SRC"), ("2", 0.5, "/SPI_CLK")])
                + fp("U2", 96, 10, "FLASH", [("1", 0, "/SPI_CLK")])
                + seg(4, 10, 5.5, 10, "/SPI_CLK_SRC") + clock_line("/SPI_CLK", 6.5, 96))
    assert board(tmp_path, body("33R"))["violations"] == []
    got = board(tmp_path, body("0R"))      # a 0-ohm link is no termination
    assert ("si_unterminated_clock", "/SPI_CLK") in kinds(got)
    got = board(tmp_path, body("470R"))
    assert ("si_series_mismatch", "/SPI_CLK") in kinds(got)


def test_oscillator_driver_makes_a_faster_class(tmp_path):
    body = (fp("Y1", 4, 20, "50MHz", [("3", 0, "/X_FEED"), ("4", 1, "+3V3")])
            + fp("U2", 40, 20, "PHY", [("1", 0, "/X_FEED")])
            + seg(4, 20, 40, 20, "/X_FEED"))
    got = board(tmp_path, body, {"si": {"nets": {"/X_FEED": {}}}})
    # named nothing a rule knows, declared in si.nets: a plain declared net
    assert got["nets"][0]["class"] == "declared"
    body = body.replace("/X_FEED", "/REF_CLK")
    got = board(tmp_path, body)
    net = got["nets"][0]
    assert net["class"] == "osc_clock" and net["t_rise_ns"] == 1.0
    assert ("si_unterminated_clock", "/REF_CLK") in kinds(got)   # 36 mm > ~29 mm


def test_crystal_net_is_an_analog_victim_not_a_line(tmp_path):
    body = (fp("Y1", 10, 20, "8MHz", [("1", 0, "/OSC_IN"), ("2", 1, "GND")])
            + seg(10, 20, 60, 20, "/OSC_IN")
            + fp("U1", 4, 10, "MCU", [("1", 0, "/SPI_CLK")])
            + fp("U2", 96, 10, "FLASH", [("1", 0, "/SPI_CLK")])
            + clock_line("/SPI_CLK", 4, 96) + seg(10, 10.35, 90, 10.35, "/OSC_IN"))
    got = board(tmp_path, body, {"si": {"nets": {"/SPI_CLK": {"driver_ohm": 20}}}})
    xtal = next(n for n in got["nets"] if n["net"] == "/OSC_IN")
    assert xtal["class"] == "crystal"
    hit = [v for v in got["violations"] if v["kind"] == "si_crosstalk"]
    assert hit and hit[0]["net"] == "/OSC_IN" and hit[0]["victim_class"] == "analog"
    assert hit[0]["aggressor"] == "/SPI_CLK"


def test_crosstalk_needs_coupling_past_the_budget(tmp_path):
    base = (fp("U1", 4, 10, "MCU", [("1", 0, "/SPI_CLK")])
            + fp("U2", 96, 10, "FLASH", [("1", 0, "/SPI_CLK")])
            + clock_line("/SPI_CLK", 4, 96))
    # a digital neighbour 0.15 mm off for 80 mm: NEXT saturates past 5%
    got = board(tmp_path, base + seg(10, 10.35, 90, 10.35, "/GPIO5"),
                {"si": {"nets": {"/SPI_CLK": {"class": "spi"}}}})
    hit = [v for v in got["violations"] if v["kind"] == "si_crosstalk"]
    assert hit and hit[0]["net"] == "/GPIO5" and hit[0]["next_pct"] > 5
    # the same neighbour 2 mm off: well under
    got = board(tmp_path, base + seg(10, 12.2, 90, 12.2, "/GPIO5"),
                {"si": {"nets": {"/SPI_CLK": {"class": "spi"}}}})
    assert not [v for v in got["violations"] if v["kind"] == "si_crosstalk"]
    # crossing at right angles couples nothing
    got = board(tmp_path, base + seg(50, 5, 50, 15, "/GPIO5", layer="F.Cu"),
                {"si": {"nets": {"/SPI_CLK": {"class": "spi"}}}})
    assert not [v for v in got["violations"] if v["kind"] == "si_crosstalk"]


@pytest.mark.parametrize("net,mate", [
    ("/MDI1_A_P", "/MDI1_A_N"), ("/BI_DA+", "/BI_DA-"), ("/TRD0P", "/TRD0N"),
    ("/TXRX1P_A", "/TXRX1M_A"), ("/ETH_TXP", "/ETH_TXN")])
def test_gigabit_pair_halves(net, mate):
    specs = {net: {}, mate: {}}
    assert check_si.pair_of(net, specs) == mate
    assert check_si.pair_of(mate, specs) == net


def _mdi_pair(p, n, w, gap):
    y2 = 10 + w + gap
    return (fp("U1", 4, 10, "PHY", [("1", 0, p), ("2", 0.8, n)])
            + fp("J1", 96, 10, "RJ45", [("1", 0, p), ("2", 0.8, n)])
            + seg(4, 10, 96, 10, p, w=w) + seg(4, y2, 96, y2, n, w=w))


@pytest.mark.parametrize("p,n", [("/MDI1_A_P", "/MDI1_A_N"),
                                 ("/TXRX1P_A", "/TXRX1M_A")])
def test_gigabit_mdi_pair_held_to_100_ohm(tmp_path, p, n):
    # planted: a Gigabit MDI pair split 2 mm apart is ~170 ohm; named for
    # 1000BASE-T it went unclassed and unchecked (#510)
    got = board(tmp_path, _mdi_pair(p, n, 0.1, 2.0))
    hit = [v for v in got["violations"] if v["kind"] == "si_impedance_target"]
    assert hit and hit[0]["target_ohm"] == 100.0 and hit[0]["zdiff_ohm"] > 150
    # quiet twin: the same pair coupled at 0.2/0.2 mm is classed and passes
    got = board(tmp_path, _mdi_pair(p, n, 0.2, 0.2))
    assert {x["net"]: x["class"] for x in got["nets"]}[p] == "eth_mdi"
    assert got["violations"] == []


def test_impedance_step_and_target(tmp_path):
    body = (fp("U1", 4, 10, "MCU", [("1", 0, "/SPI_CLK")])
            + fp("U2", 116, 10, "FLASH", [("1", 0, "/SPI_CLK")])
            + seg(4, 10, 60, 10, "/SPI_CLK", w=0.1) + seg(60, 10, 116, 10, "/SPI_CLK", w=0.6))
    got = board(tmp_path, body, {"si": {"nets": {"/SPI_CLK": {"class": "spi", "z0_ohm": 30}}}})
    k = kinds(got)
    assert ("si_impedance_step", "/SPI_CLK") in k
    assert ("si_impedance_target", "/SPI_CLK") in k


def test_ignore_and_group_skew(tmp_path):
    body = (fp("U1", 4, 10, "MCU", [("1", 0, "/SPI_CLK")])
            + fp("U2", 96, 10, "FLASH", [("1", 0, "/SPI_CLK")])
            + clock_line("/SPI_CLK", 4, 96))
    assert board(tmp_path, body, {"si": {"ignore": ["/SPI_CLK"]}})["nets"] == []
    body += (fp("U3", 4, 30, "MCU", [("1", 0, "/SPI_MOSI")])
             + fp("U4", 10, 30, "FLASH", [("1", 0, "/SPI_MOSI")]) + seg(4, 30, 10, 30, "/SPI_MOSI"))
    got = board(tmp_path, body, {"si": {"groups": [
        {"name": "spi", "nets": ["/SPI_CLK", "/SPI_MOSI"], "ui_ns": 2.0}]}})
    assert ("si_skew", "/SPI_CLK") in kinds(got)
    assert got["groups"][0]["spread_ps"] > 200


# ------------------------------------------------------------ corpus

@pytest.mark.parametrize("name", ["usbbuck4", "blinky2", "rf4"])
def test_goldens_are_clean(name):
    payload, _ = check_si.run(["--pcb", str(GOLDEN / name / f"{name}.kicad_pcb"),
                               "--constraints", str(GOLDEN / name / "constraints.json")])
    assert payload["violations"] == []


def test_mutant_caught():
    payload, _ = check_si.run(
        ["--pcb", str(GOLDEN / "mutants" / "si-unterminated-clock" / "usbbuck4.kicad_pcb"),
         "--constraints", str(GOLDEN / "usbbuck4" / "constraints.json")])
    assert ("si_unterminated_clock", "/MCO") in kinds(payload)
