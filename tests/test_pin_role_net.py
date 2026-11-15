"""netlist_audit's rules 29-31: a pin whose role doesn't fit its net
(class 6 of docs/miss-taxonomy.md).

The planted faults are the errata corpus's, cut down:
- flexgrid v4 rev A: the USBLC6-2SC6's GND pin (2) on VBUS and its VBUS
  pin (5) on ground. Rev B puts them back.
- netscore rev A: the FS312MH protector's GND (the cell's -) on the
  board's ground and BAT- (the load's -) on the cell's -BATT. Rev B swaps
  them.
- fc980c rev1: the SWDIO header pin on the STM32F401's PA15, not PA13.
  Rev2 moves it to PA13."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402

KINDS = ("pin_role_net", "protector_cell_side", "debug_pin")


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets):
    parsed = {"components": {r: {"value": v, "footprint": "",
                                 "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[], stock_symbols="")
    return [x for x in viol if x["kind"].startswith(KINDS)], facts


def _usb(swapped=True):
    """U5 USBLC6-2SC6 between J4's USB-C and U2's D+/D-."""
    comps = {"U5": "USBLC6-2SC6", "U2": "ESP32-S3", "J4": "USB_C"}
    gnd, vbus = ("5", "2") if swapped else ("2", "5")
    nets = {"VBUS": [_m("J4", "A4", "VBUS"),
                     _m("U5", vbus, "GND" if swapped else "VBUS")],
            "GND": [_m("J4", "A1", "GND"),
                    _m("U5", gnd, "VBUS" if swapped else "GND"),
                    _m("U2", "41", "GND", "power_in")],
            "D+": [_m("J4", "A6", "D+"), _m("U5", "1", "I/O1"),
                   _m("U2", "14", "IO20", "bidirectional")]}
    return comps, nets


def test_flexgrid_esd_ground_pin_on_vbus_is_an_error():
    hits, facts = _audit(*_usb())
    assert [(h["kind"], h["severity"], h["refs"]) for h in hits] == [
        ("pin_role_net", "error", ["U5"])]
    roles = {(p["function"], p["net"]) for p in hits[0]["pins"]}
    assert roles == {("GND", "VBUS"), ("VBUS", "GND")}
    assert facts["pin_role_parts_checked"] >= 2


def test_flexgrid_rev_b_is_quiet():
    assert _audit(*_usb(swapped=False))[0] == []


def test_supply_pin_on_ground_alone_is_a_warning():
    comps = {"U1": "TPS7A02", "U2": "MCU"}
    nets = {"GND": [_m("U1", "1", "VIN", "power_in"),
                    _m("U1", "2", "GND", "power_in"),
                    _m("U2", "3", "VSS", "power_in")],
            "+3V3": [_m("U1", "4", "OUT", "power_out"),
                     _m("U2", "4", "VDD", "power_in")]}
    hits = _audit(comps, nets)[0]
    assert [(h["severity"], h["refs"]) for h in hits] == [
        ("warning", ["U1"])]


def test_rtc_vbat_grounded_is_info():
    comps = {"U3": "PCF8563"}
    nets = {"GND": [_m("U3", "4", "VSS", "power_in"),
                    _m("U3", "7", "VBAT", "power_in")],
            "+3V3": [_m("U3", "8", "VDD", "power_in")]}
    hits = _audit(comps, nets)[0]
    assert [h["severity"] for h in hits] == ["info"]


def test_connectors_and_test_points_are_not_judged():
    # a header's pin name is the drawer's label, not a part's role
    comps = {"J1": "Conn_01x02", "TP1": "TestPoint"}
    nets = {"+5V": [_m("J1", "1", "GND"), _m("TP1", "1", "GND")]}
    assert _audit(comps, nets)[0] == []


def _bms(swapped=True):
    """BMS1 FS312MH on the cell BAT1; the load's ground is GND."""
    comps = {"BMS1": "FS312MH", "BAT1": "Battery_Cell", "U1": "ESP32",
             "R37": "1k"}
    cell, load = ("BAT-", "GND") if swapped else ("GND", "BAT-")
    nets = {"-BATT": [_m("BAT1", "1", "-"), _m("BMS1", "3" if swapped
                                               else "1", cell, "input")],
            "GND": [_m("BMS1", "1" if swapped else "3", load, "input"),
                    _m("U1", "1", "GND", "power_in"), _m("R37", "1")],
            "Net-(BMS1-CS)": [_m("BMS1", "4", "CS", "input"),
                              _m("R37", "2")],
            "+BATT": [_m("BAT1", "2", "+")]}
    return comps, nets


def test_netscore_protector_sides_swapped_is_a_warning():
    hits, facts = _audit(*_bms())
    assert [(h["kind"], h["severity"], h["net"]) for h in hits] == [
        ("protector_cell_side", "warning", "-BATT")]
    assert hits[0]["cell_net"] == "GND"
    assert facts["protector_cell_sides_checked"] == 1


def test_netscore_rev_b_is_quiet():
    assert _audit(*_bms(swapped=False))[0] == []


def _swd(dio="PA15", mcu="STM32F401CBUx", extra=None):
    """J1 a 4-pin SWD header (generic Pin_n names) on nets named SWDIO and
    SWCLK; U2 the MCU."""
    comps = {"U2": mcu, "J1": "Conn_01x04"}
    nets = {"SWDIO": [_m("J1", "3", "Pin_3"),
                      _m("U2", "38", dio, "bidirectional")],
            "SWCLK": [_m("J1", "4", "Pin_4"),
                      _m("U2", "37", "PA14", "bidirectional")],
            "GND": [_m("J1", "2", "Pin_2"),
                    _m("U2", "35", "VSS", "power_in")]}
    for net, ms in (extra or {}).items():
        nets.setdefault(net, []).extend(ms)
    return comps, nets


def test_fc980c_swdio_on_the_wrong_pin_is_an_error():
    hits, facts = _audit(*_swd())
    assert [(h["kind"], h["severity"], h["net"]) for h in hits] == [
        ("debug_pin_wrong", "error", "SWDIO")]
    assert hits[0]["want_pin"] == "PA13" and "PA15" in hits[0]["msg"]
    assert facts["debug_mcus_checked"] == 1


def test_fc980c_rev2_is_quiet():
    assert _audit(*_swd(dio="PA13"))[0] == []


def test_swd_pin_used_as_gpio_with_no_header_is_a_warning():
    comps = {"U2": "STM32G031K8T6", "D1": "LED"}
    nets = {"LED": [_m("U2", "24", "PA14-BOOT0", "bidirectional"),
                    _m("D1", "2", "A")],
            "GND": [_m("U2", "4", "VSS", "power_in"), _m("D1", "1", "K")]}
    hits = _audit(comps, nets)[0]
    assert [(h["kind"], h["signal"]) for h in hits] == [
        ("debug_pins_reused", "SWCLK")]
    assert "BOOT0" in hits[0]["msg"]


def test_swd_pin_through_a_resistor_to_a_header_is_quiet():
    comps = {"U2": "STM32G031K8T6", "R1": "22", "J1": "Conn_01x04"}
    nets = {"Net-(U2-PA14)": [_m("U2", "24", "PA14-BOOT0"), _m("R1", "1")],
            "Net-(J1-Pin_4)": [_m("R1", "2"), _m("J1", "4", "Pin_4")]}
    comps["R1"] = "22"
    hits = _audit(comps, nets)[0]
    assert hits == []


def test_mcu_with_no_debug_row_is_not_judged():
    hits, facts = _audit(*_swd(mcu="ATSAMD21G18A"))
    assert hits == [] and facts["debug_mcus_checked"] == 0


def test_pin_named_for_its_debug_function_is_right():
    # glasgow's G031 symbol names its pins SYS_SWDIO; other symbols write
    # PA13(SWDIO), or PA15/PA14-BOOT0 for two pads bonded to one pin
    for dio, clk in (("SYS_SWDIO", "SYS_SWCLK"),
                     ("PA13(SWDIO)", "PA14(SWCLK)"),
                     ("PA13", "PA15/PA14-BOOT0")):
        comps, nets = _swd(dio=dio, mcu="STM32G031G6Ux")
        nets["SWCLK"][1]["pinfunction"] = clk
        assert _audit(comps, nets)[0] == [], (dio, clk)
