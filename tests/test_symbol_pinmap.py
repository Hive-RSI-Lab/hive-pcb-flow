"""netlist_audit's symbol pin-map rules (miss class 5, #1086, #1114):
symbol_value_mismatch, symbol_pinmap, bus_order_flipped and mcu_i2c_role,
with scripts/lib/pinmaplib.py.

Each rule plants a fault (the errata board's rev A, or a regulator drawn
with another package's pin map) beside a quiet twin (the fix)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402
import pinmaplib  # noqa: E402

KINDS = ("symbol_value_mismatch", "symbol_pinmap", "bus_order_flipped",
         "mcu_i2c_role")


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets, stock="", libpart_pins=None, kinds=KINDS):
    parsed = {"components": {r: (v if isinstance(v, dict) else
                                 {"value": v, "libsource": ("", "")})
                             for r, v in comps.items()},
              "nets": nets, "libpart_pins": libpart_pins or {}}
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[], stock_symbols=stock)
    return [x for x in viol if x["kind"] in kinds], facts


# ------------------------------------------------ symbol_value_mismatch

def _digit(value, symbol="7Seg 0.2in", fp="Luis:7Seg SMD 0.8in Single",
           ref="U2"):
    """netscore-big's score digit: a 10-pin display on `symbol`."""
    comps = {ref: {"value": value, "footprint": fp,
                   "libsource": ("Luis", symbol)}}
    nets = {f"/SEG{i}": [_m(ref, str(i), f"SEG{i}")] for i in range(1, 11)}
    return comps, nets


def test_netscore_big_08in_digit_on_the_02in_symbol_is_the_planted_fault():
    hits, facts = _audit(*_digit("SMD 0.8in"))
    assert [(h["kind"], h["refs"], h["severity"]) for h in hits] == [
        ("symbol_value_mismatch", ["U2"], "warning")]
    assert "0.2in" in hits[0]["msg"] and "0.8in" in hits[0]["msg"]
    assert facts["symbol_sizes_checked"] == 1


def test_the_fix_a_symbol_of_the_placed_size_is_quiet():
    hits, facts = _audit(*_digit("SMD 0.8in", symbol="7Seg 0.8in"))
    assert hits == [] and facts["symbol_sizes_checked"] == 1


def test_a_value_with_no_size_falls_back_to_the_footprint():
    assert _audit(*_digit("GS-2011AB"))[0] != []
    assert _audit(*_digit("GS-2011AB", fp="Luis:7Seg 0.2in"))[0] == []


def test_a_connector_is_not_judged():
    hits, facts = _audit(*_digit("0.8in", symbol="Conn 0.2in", ref="J1"))
    assert hits == [] and facts["symbol_sizes_checked"] == 0


# ------------------------------------------------ symbol_pinmap

_SOT223 = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
_TO252 = "Package_TO_SOT_SMD:TO-252-2"


def _sym(name, fp, pins, extends=None):
    body = f'(extends "{extends}")' if extends else "".join(
        f'(pin power_in line (at 0 0 0) (length 2.54) (name "{n}") '
        f'(number "{num}"))' for num, n in pins.items())
    return (f'(kicad_symbol_lib (version 20251024) (symbol "{name}" '
            f'(property "Reference" "U") (property "Footprint" "{fp}") '
            f'(symbol "{name}_0_1" {body})))\n'
            if not extends else
            f'(kicad_symbol_lib (version 20251024) (symbol "{name}" {body} '
            f'(property "Footprint" "{fp}")))\n')


@pytest.fixture()
def stock(tmp_path):
    """A stock dir with an LDO whose SOT-223 symbol has GND 1, VO 2, VI 3,
    a derived symbol for the 5 V part, and a Device lib (generic)."""
    lib = tmp_path / "Regulator_Linear.kicad_symdir"
    lib.mkdir()
    (lib / "XYZ1117-33.kicad_sym").write_text(_sym(
        "XYZ1117-33", _SOT223, {"1": "GND", "2": "VO", "3": "VI"}))
    (lib / "XYZ1117-50.kicad_sym").write_text(_sym(
        "XYZ1117-50", _SOT223, {}, extends="XYZ1117-33"))
    dev = tmp_path / "Device.kicad_symdir"
    dev.mkdir()
    (dev / "R.kicad_sym").write_text(_sym("R", "", {"1": "~", "2": "~"}))
    pinmaplib.stock_index.cache_clear()
    return str(tmp_path)


def _ldo(pins, value="XYZ1117-33", fp=_SOT223, mpn=None):
    """U1, drawn with the board's own symbol MyLib:LDO whose pins are
    `pins` {number: name}."""
    comp = {"value": value, "footprint": fp, "libsource": ("MyLib", "LDO")}
    if mpn:
        comp["mpn"] = mpn
    net_of = {"GND": "GND", "VI": "+5V", "VO": "+3V3", "IN": "+5V",
              "OUT": "+3V3"}
    nets: dict = {}
    for num, name in pins.items():
        nets.setdefault(net_of.get(name, f"/N{num}"), []).append(
            _m("U1", num, name, "power_in"))
    return {"U1": comp}, nets, {("MyLib", "LDO"): dict(pins)}


def test_ldo_drawn_with_another_packages_pin_map_is_the_planted_fault(stock):
    # VI and VO swapped, as a TO-252 variant's symbol (IN 2, OUT 3) has
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"})
    hits, facts = _audit(comps, nets, stock, lp)
    assert [(h["kind"], h["refs"]) for h in hits] == [
        ("symbol_pinmap", ["U1"])]
    assert hits[0]["stock"] == "Regulator_Linear:XYZ1117-33"
    assert {p["name"]: (p["placed"], p["stock"]) for p in hits[0]["pins"]} \
        == {"IN": (["2"], ["3"]), "OUT": (["3"], ["2"])}
    assert facts["symbol_pinmaps_checked"] == 1


def test_ldo_with_the_stock_pin_map_is_quiet(stock):
    comps, nets, lp = _ldo({"1": "GND", "2": "VO", "3": "VI", "4": "VO"})
    hits, facts = _audit(comps, nets, stock, lp)
    assert hits == [] and facts["symbol_pinmaps_checked"] == 1


def test_the_other_package_variant_is_not_judged(stock):
    # the same IN 2 / OUT 3 map on a TO-252: right for that variant, and
    # the stock symbol is SOT-223 only, so nothing to compare it with
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"}, fp=_TO252)
    hits, facts = _audit(comps, nets, stock, lp)
    assert hits == [] and facts["symbol_pinmaps_checked"] == 0


def test_a_derived_stock_symbol_takes_its_parents_pins(stock):
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"},
                           value="XYZ1117-50")
    assert [h["stock"] for h in _audit(comps, nets, stock, lp)[0]] == [
        "Regulator_Linear:XYZ1117-50"]


def test_the_mpn_names_the_part_when_the_value_does_not(stock):
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"},
                           value="3V3 LDO", mpn="XYZ1117-33")
    assert len(_audit(comps, nets, stock, lp)[0]) == 1


def test_a_value_naming_no_stock_part_or_no_stock_dir_is_not_judged(stock):
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"},
                           value="MYREG")
    assert _audit(comps, nets, stock, lp)[1]["symbol_pinmaps_checked"] == 0
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"})
    assert _audit(comps, nets, "", lp)[1]["symbol_pinmaps_checked"] == 0


def test_package_family_reads_the_package_token():
    assert pinmaplib.package_family(_SOT223) == "SOT2233"
    assert pinmaplib.package_family(_TO252) == "TO2522"
    assert pinmaplib.same_package("SOT-223", _SOT223)
    assert not pinmaplib.same_package(_TO252, _SOT223)
    assert pinmaplib.same_package("MyLib:Custom", _SOT223)


# ------------------------------------------------ bus_order_flipped

def _counter(unit1_divs):
    """gcm's SN74LV393A U26: 1QA..1QD on CLK_50d<unit1_divs>MHZ, 2QA..2QD
    on d32..d256."""
    comps = {"U26": {"value": "SN74LV393A", "footprint": "SOIC-14",
                     "libsource": ("GCM", "SN74LV393A")}}
    nets = {}
    for (pin, q), d in zip([("3", "1QA"), ("4", "1QB"), ("5", "1QC"),
                            ("6", "1QD")], unit1_divs):
        nets[f"/ClockGenerator/CLK_50d{d}MHZ"] = [_m("U26", pin, q, "output")]
    for (pin, q), d in zip([("11", "2QA"), ("10", "2QB"), ("9", "2QC"),
                            ("8", "2QD")], (32, 64, 128, 256)):
        nets[f"/ClockGenerator/CLK_50d{d}MHZ"] = [_m("U26", pin, q, "output")]
    return comps, nets


def test_gcm_v50_one_counter_unit_wired_in_reverse_is_the_planted_fault():
    hits, facts = _audit(*_counter((16, 8, 4, 2)))
    assert [(h["kind"], h["refs"]) for h in hits] == [
        ("bus_order_flipped", ["U26"])]
    assert "(down)" in hits[0]["msg"] and "(up)" in hits[0]["msg"]
    assert facts["channel_parts_checked"] == 1


def test_gcm_v51_both_units_counting_up_is_quiet():
    hits, facts = _audit(*_counter((2, 4, 8, 16)))
    assert hits == [] and facts["channel_parts_checked"] == 1


def test_channel_nets_with_no_common_count_are_not_judged():
    comps, nets = _counter((2, 4, 8, 16))
    nets = {k.replace("CLK_50d", "TAP_").replace("16MHZ", "X")
            .replace("8MHZ", "Y"): v for k, v in nets.items()}
    assert _audit(comps, nets)[0] == []


# ------------------------------------------------ mcu_i2c_role

def _pico(sda_gpio, scl_gpio, names=("GPIO{}",)):
    """pico-if1's U301 (a Pico on the board's own symbol) with the OLED
    module's SDA on GPIO `sda_gpio` and SCK on GPIO `scl_gpio`."""
    comps = {"U301": {"value": "Pico", "footprint": "MCU_RaspberryPi:Pico",
                      "libsource": ("pico", "Pico")},
             "J5": {"value": "OLED", "footprint": "PinHeader_1x04",
                    "libsource": ("Connector", "Conn_01x04")}}
    pin = {4: "6", 5: "7", 6: "9", 7: "10"}
    nets = {"OLED_SDA": [_m("U301", pin[sda_gpio],
                            names[0].format(sda_gpio), "bidirectional"),
                         _m("J5", "4")],
            "OLED_SCK": [_m("U301", pin[scl_gpio],
                            names[0].format(scl_gpio), "bidirectional"),
                         _m("J5", "3")],
            "GND": [_m("U301", "3", "GND", "power_in"), _m("J5", "1")]}
    return comps, nets


def test_pico_if1_v13_oled_sda_and_sck_crossed_is_the_planted_fault():
    # GPIO4 is I2C0 SDA, GPIO5 I2C0 SCL: v1.3 has SCK on 4, SDA on 5
    hits, facts = _audit(*_pico(sda_gpio=5, scl_gpio=4))
    assert [(h["kind"], h["refs"], h["block"]) for h in hits] == [
        ("mcu_i2c_role", ["U301"], "I2C0")]
    assert "pico-sdk" in hits[0]["msg"]
    assert facts["mcu_i2c_parts_checked"] == 1


def test_pico_if1_v14_oled_on_the_right_pins_is_quiet():
    hits, facts = _audit(*_pico(sda_gpio=4, scl_gpio=5))
    assert hits == [] and facts["mcu_i2c_parts_checked"] == 1


def test_the_pico_symbols_short_gp_names_are_read():
    assert len(_audit(*_pico(5, 4, names=("GP{}",)))[0]) == 1


def test_two_blocks_lines_on_one_bus_are_not_a_crossing():
    # SDA on GPIO6 (I2C1 SDA), SCK on GPIO5 (I2C0 SCL): no one block is
    # crossed (a bit-banged bus, not this rule's)
    assert _audit(*_pico(sda_gpio=6, scl_gpio=5))[0] == []


def test_mcu_i2c_function_maps_pin_names():
    t = {"GPIO4": "I2C0_SDA", "GPIO26": "I2C1_SDA"}
    assert pinmaplib.mcu_i2c_function("GP4", t) == "I2C0_SDA"
    assert pinmaplib.mcu_i2c_function("GPIO26_ADC0", t) == "I2C1_SDA"
    assert pinmaplib.mcu_i2c_function("GPIO40", t) is None


# ------------------------------------------------ #1114: SCL_24 names

def _expanders(scl, sda):
    """Two PCA6416As both strapped low on one bus, their bus pins named
    `scl` and `sda` (SCL_22-style, as some libraries number them)."""
    comps = {"U24": "PCA6416APW", "U25": "PCA6416APW"}
    nets = {"/SCL": [_m(u, "22", scl, "input") for u in ("U24", "U25")],
            "/SDA": [_m(u, "23", sda, "bidirectional")
                     for u in ("U24", "U25")],
            "+3V3": [_m(u, "2", "VDD_I2C", "power_in")
                     for u in ("U24", "U25")],
            "GND": [_m(u, "21", "ADDR", "input") for u in ("U24", "U25")]}
    return comps, nets


@pytest.mark.parametrize("scl,sda", [("SCL", "SDA"), ("SCL_22", "SDA_23")])
def test_numbered_bus_pin_names_still_collide(scl, sda):
    hits, _ = _audit(*_expanders(scl, sda), kinds=("i2c_address_collision",))
    assert [(h["refs"], h["address"]) for h in hits] == [
        (["U24", "U25"], "0x20")]


def test_strip_bus_number_leaves_other_names():
    s = netlist_audit._strip_bus_number
    assert s("SCL_24") == "SCL" and s("SDA_23") == "SDA"
    assert s("GPIO_24") == "GPIO_24" and s("A_1") == "A_1"
