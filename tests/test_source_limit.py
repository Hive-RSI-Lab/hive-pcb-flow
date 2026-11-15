"""A load against its source's limit (class 11 of docs/miss-taxonomy.md,
netlist_audit rule 32, reference/source_limits.yaml).

Each sub-rule gets a planted fault and a quiet twin. rail_over is seeded
from flexgrid v3/v4: a TPS7A03 (200 mA) on +3V3 feeding an
ESP32-S3-WROOM-1, whose datasheet wants a supply that gives 500 mA."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets, table=None):
    parsed = {"components": {r: {"value": v, "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    kw = {} if table is None else {"source_limits": table}
    return netlist_audit.audit(parsed, {}, None, **kw)


_KINDS = {"rail_over", "ldo_heat", "gpio_over", "gpio_total_over",
          "vbus_over"}


def _hits(viol):
    return sorted((v["kind"], v["severity"]) for v in viol
                  if v["kind"] in _KINDS)


def _nj(facts, kind):
    return [n for n in facts.get("not_judged", []) if n.startswith(kind)]


# --------------------------------------------------------------- rail_over

def _ldo(reg="TPS7A0333PDBVR", load="ESP32-S3-WROOM-1-N16R8", vin="+5V",
         extra=None):
    comps = {"U3": reg, "U2": load}
    nets = {vin: [_m("U3", "1", "IN", "power_in"),
                  _m("U3", "3", "EN", "input")],
            "GND": [_m("U3", "2", "GND", "power_in"),
                    _m("U2", "1", "GND", "power_in")],
            "+3V3": [_m("U3", "5", "OUT", "power_out"),
                     _m("U2", "2", "3V3", "power_in")]}
    for ref, val, pin, func in extra or []:
        comps[ref] = val
        nets["+3V3"].append(_m(ref, pin, func, "power_in"))
    return comps, nets


def test_flexgrid_tps7a03_feeding_an_esp32_s3_is_caught():
    viol, facts = _audit(*_ldo())
    assert _hits(viol) == [("rail_over", "error")]
    hit = next(v for v in viol if v["kind"] == "rail_over")
    assert hit["refs"] == ["U3"] and hit["net"] == "+3V3"
    assert hit["limit_a"] == 0.2 and hit["load_a"] == 0.5
    assert facts["source_limits_checked"] >= 1


def test_a_500_ma_ldo_feeding_the_same_module_is_quiet():
    viol, facts = _audit(*_ldo(reg="TLV75533PDBVR"))
    assert _hits(viol) == []
    assert _nj(facts, "rail_over") == []


def test_a_load_with_no_row_gives_not_judged_not_a_guess():
    viol, facts = _audit(*_ldo(reg="TLV75533PDBVR",
                               extra=[("U4", "LAN8720A", "1", "VDDIO")]))
    assert _hits(viol) == []
    nj = _nj(facts, "rail_over")
    assert len(nj) == 1 and "U4 (LAN8720A)" in nj[0]


def test_a_downstream_ldo_passes_its_load_upstream():
    # +5V -> TLV755 (500 mA) -> +3V3 -> TPS7A20 (300 mA) -> +1V8 feeding
    # nothing named; the module on +3V3 is 500 mA: the sum is exactly at
    # the limit, so quiet (strictly over only)
    comps, nets = _ldo(reg="TLV75533PDBVR")
    comps["U5"] = "TPS7A2018PDBVR"
    nets["+3V3"].append(_m("U5", "1", "IN", "power_in"))
    nets["+1V8"] = [_m("U5", "5", "OUT", "power_out")]
    viol, _ = _audit(comps, nets)
    assert _hits(viol) == []
    # a second module behind U5 pushes U3 past 500 mA
    comps["U6"] = "ESP32-C3-WROOM-02"
    nets["+1V8"].append(_m("U6", "2", "3V3", "power_in"))
    viol, _ = _audit(comps, nets)
    over = sorted(v["refs"][0] for v in viol if v["kind"] == "rail_over")
    assert over == ["U3", "U5"]


def test_a_dnp_load_is_not_counted():
    parsed_c, nets = _ldo()
    parsed = {"components": {r: {"value": v, "libsource": ("", "")}
                             for r, v in parsed_c.items()}, "nets": nets}
    parsed["components"]["U2"]["dnp"] = True
    viol, _ = netlist_audit.audit(parsed, {}, None)
    assert _hits(viol) == []


# ---------------------------------------------------------------- ldo_heat

def _led_rail(n, ohms, vf="2.0V", reg="TPS7A2033PDBVR", vin="+12V"):
    comps = {"U1": reg}
    nets = {vin: [_m("U1", "1", "IN", "power_in")],
            "GND": [_m("U1", "2", "GND", "power_in")],
            "+3V3": [_m("U1", "5", "OUT", "power_out")]}
    for k in range(n):
        r, d = f"R{k + 1}", f"D{k + 1}"
        comps[r], comps[d] = ohms, f"LED_red_{vf}"
        nets["+3V3"].append(_m(r, "1"))
        nets[f"/L{k}"] = [_m(r, "2"), _m(d, "2", "A")]
        nets["GND"].append(_m(d, "1", "K"))
    return comps, nets


def test_an_ldo_burning_12v_to_3v3_on_leds_runs_too_hot():
    # 4 x (3.3-2.0)/10 = 520 mA would also be rail_over; use 6 x 39 ohm:
    # 6 x 33 mA = 200 mA, 8.7 V x 0.2 A = 1.74 W x 166.1 = 289 degC
    viol, _ = _audit(*_led_rail(6, "39"))
    assert ("ldo_heat", "error") in _hits(viol)
    assert ("rail_over", "error") not in _hits(viol)


def test_the_same_ldo_from_5v_with_light_leds_is_quiet():
    # 2 x 13 mA from 5 V: 1.7 V x 26 mA x 166.1 = 7 degC rise
    viol, facts = _audit(*_led_rail(2, "100", vin="+5V"))
    assert _hits(viol) == []
    assert _nj(facts, "ldo_heat") == []


def test_heat_with_input_volts_unknown_is_not_judged():
    viol, facts = _audit(*_led_rail(6, "39", vin="VIN"))
    assert ("ldo_heat", "error") not in _hits(viol)
    assert len(_nj(facts, "ldo_heat")) == 1


def test_a_peak_only_load_is_not_heat():
    # the module's 500 mA is a supply-capability figure, not a draw
    viol, facts = _audit(*_ldo(reg="TLV75533PDBVR", vin="+12V"))
    assert _hits(viol) == []


def test_an_led_with_no_vf_is_an_unknown_load():
    viol, facts = _audit(*_led_rail(2, "100", vf="", vin="+5V"))
    assert _hits(viol) == []
    assert any("no Vf" in n for n in _nj(facts, "rail_over"))


# --------------------------------------------------------------- gpio_over

def _mcu(ohms, vf="2.0V", n=1, sink=False):
    comps = {"U1": "ATmega328PB-AU"}
    nets = {"+5V": [_m("U1", "4", "VCC", "power_in")],
            "GND": [_m("U1", "3", "GND", "power_in")]}
    for k in range(n):
        r, d = f"R{k + 1}", f"D{k + 1}"
        comps[r], comps[d] = ohms, f"LED {vf}"
        pin = f"/IO{k}"
        nets[pin] = [_m("U1", str(10 + k), f"PB{k}", "bidirectional"),
                     _m(r, "1")]
        nets[f"/L{k}"] = [_m(r, "2"), _m(d, "1" if sink else "2")]
        if sink:
            nets["+5V"].append(_m(d, "2", "A"))
        else:
            nets["GND"].append(_m(d, "1", "K"))
    return comps, nets


def test_a_pin_driving_an_led_through_47_ohm_is_over_40_ma():
    # (5 - 2.0) / 47 = 64 mA
    viol, _ = _audit(*_mcu("47"))
    assert _hits(viol) == [("gpio_over", "error")]
    hit = next(v for v in viol if v["kind"] == "gpio_over")
    assert hit["pin"] == "PB0" and sorted(hit["refs"]) == ["D1", "R1", "U1"]


def test_a_sinking_pin_is_judged_the_same_way():
    viol, _ = _audit(*_mcu("47", sink=True))
    assert _hits(viol) == [("gpio_over", "error")]


def test_a_330_ohm_led_pin_is_quiet():
    viol, facts = _audit(*_mcu("330"))
    assert _hits(viol) == []
    assert _nj(facts, "gpio_over") == []


def test_many_pins_each_fine_but_over_the_part_total_warn():
    # 8 x (5-2)/100 = 240 mA > 100 mA; each 30 mA < 40 mA
    viol, _ = _audit(*_mcu("100", n=8))
    assert _hits(viol) == [("gpio_total_over", "warning")]


def test_an_led_pin_with_no_vf_that_might_be_over_is_not_judged():
    viol, facts = _audit(*_mcu("100", vf=""))
    assert _hits(viol) == []
    assert len(_nj(facts, "gpio_over")) == 1     # up to 50 mA vs 40


# --------------------------------------------------------------- vbus_over

def _usb(load_n=2, cc=False, pd=None):
    comps = {"J1": "USB_B_Micro", "U1": "TLV75533PDBVR"}
    nets = {"VBUS": [_m("J1", "1", "VBUS", "power_out"),
                     _m("U1", "1", "IN", "power_in")],
            "GND": [_m("J1", "5", "GND", "power_in")],
            "+3V3": [_m("U1", "5", "OUT", "power_out")]}
    if cc:
        comps["J1"] = "USB_C_Receptacle_USB2.0"
        nets["/CC1"] = [_m("J1", "A5", "CC1", "bidirectional")]
    for k in range(load_n):
        ref = f"U{k + 2}"
        comps[ref] = "ESP32-C6-MINI-1"
        nets["+3V3"].append(_m(ref, "3", "3V3", "power_in"))
    if pd:
        comps["U9"] = pd
    return comps, nets


def test_two_modules_behind_a_usb2_port_ask_past_500_ma():
    viol, _ = _audit(*_usb())
    assert ("vbus_over", "error") in _hits(viol)


def test_one_module_behind_a_usb2_port_is_quiet():
    viol, _ = _audit(*_usb(load_n=1))
    assert ("vbus_over", "error") not in _hits(viol)
    assert ("vbus_over", "warning") not in _hits(viol)


def test_type_c_without_pd_is_a_warning():
    viol, _ = _audit(*_usb(cc=True))
    assert ("vbus_over", "warning") in _hits(viol)


def test_a_pd_controller_on_the_board_mutes_vbus():
    viol, _ = _audit(*_usb(cc=True, pd="STUSB4500QTR"))
    assert not any(k == "vbus_over" for k, _ in _hits(viol))


# ------------------------------------------------- unverified: not judged

def test_an_unverified_regulator_row_gives_not_judged_only():
    table = copy.deepcopy(netlist_audit.load_source_limits())
    for r in table["regulators"]:
        if r["mpn"] == "TPS7A03":
            r["verified"] = False
    viol, facts = _audit(*_ldo(), table=table)
    assert _hits(viol) == []
    nj = _nj(facts, "rail_over")
    assert len(nj) == 1 and "not yet" in nj[0]


# ----------------------------------------------------------- the table

@pytest.mark.parametrize("section,need", [
    ("regulators", ("iout_max", "in", "out")),
    ("loads", ("i_supply", "supply")),
    ("gpio", ("pins", "supply"))])
def test_source_rows_are_cited_and_verified(section, need):
    doc = yaml.safe_load((REPO / "reference" / "source_limits.yaml")
                         .read_text())
    assert doc[section]
    for row in doc[section]:
        assert row.get("verified") is True, row["mpn"]
        for k in need:
            assert row.get(k) is not None, (row["mpn"], k)
        # the heat pair comes together or not at all (XC6206: Pd only)
        assert ("tj_max" in row) == ("theta_ja" in row), row["mpn"]
        cited = {f for c in row["cite"] for f in c["fields"]}
        nums = {k for k in ("iout_max", "tj_max", "theta_ja", "i_supply",
                            "pin_max", "total_max") if k in row}
        assert nums <= cited, (row["mpn"], nums - cited)
        assert all(c["page"] and c["quote"] and len(c["sha256"]) == 64
                   for c in row["cite"])


def test_drafter_reads_a_regulator_row():
    import part_rows  # noqa: PLC0415
    pages = ["7.3 Recommended Operating Conditions\n"
             "IOUT Output current 0 200 mA\n"
             "TJ Operating junction temperature –40 125 °C\n",
             "RθJA Junction-to-ambient thermal resistance 179.1 181.9 "
             "201.1 °C/W\n"]
    rows = part_rows._reg_rows(pages, "TPS7A03", "x.pdf", "0" * 64)
    assert len(rows) == 1
    r = rows[0]
    assert (r["iout_max"], r["tj_max"], r["theta_ja"]) == (0.2, 125, 179.1)
    for c in r["cite"]:
        assert part_rows.check_cite(r, c, pages) == []


# ------------------------------------------- #1176: XC6206, XC6220, HT7533

def _ldo3(reg, load, vin="+5V", out_pin="VOUT"):
    # the three-pin SOT-23/SOT-89 LDOs name their pins VIN / VOUT / VSS
    comps = {"U3": reg, "U2": load}
    nets = {vin: [_m("U3", "3", "VIN", "power_in")],
            "GND": [_m("U3", "1", "VSS", "power_in"),
                    _m("U2", "1", "GND", "power_in")],
            "+3V3": [_m("U3", "2", out_pin, "power_out"),
                     _m("U2", "2", "3V3", "power_in")]}
    return comps, nets


def test_an_xc6206_feeding_an_esp32_module_is_caught():
    viol, _ = _audit(*_ldo3("XC6206P332MR", "ESP32-C3-WROOM-02"))
    assert _hits(viol) == [("rail_over", "error")]
    hit = next(v for v in viol if v["kind"] == "rail_over")
    assert hit["limit_a"] == 0.2 and hit["load_a"] == 0.5


def test_an_ht7533_feeding_an_esp32_module_is_caught():
    viol, _ = _audit(*_ldo3("HT7533-1", "ESP32-S3-WROOM-1-N8R8"))
    hit = next(v for v in viol if v["kind"] == "rail_over")
    assert hit["limit_a"] == 0.07 and hit["load_a"] == 0.5


def test_an_xc6220_feeding_the_same_module_is_quiet():
    viol, facts = _audit(*_ldo3("XC6220B331MR", "ESP32-C3-WROOM-02"))
    assert _hits(viol) == []
    assert _nj(facts, "rail_over") == []


def test_a_low_voltage_xc6206_has_no_row():
    # the 1.2-2.9 V types are rated for less than 200 mA: no row, no hit
    viol, facts = _audit(*_ldo3("XC6206P182MR", "ESP32-C3-WROOM-02"))
    assert _hits(viol) == []


def test_xc6206_heat_is_not_judged_without_theta_ja():
    # its datasheet gives package Pd only: 6 V to 3.3 V on 6 LEDs would
    # run hot on any SOT-23 LDO, but the rule says so as not judged
    comps, nets = _led_rail(4, "68", reg="XC6206P332MR", vin="+5V")
    for n in nets.values():
        for m in n:
            if m["ref"] == "U1":
                m["pinfunction"] = {"IN": "VIN", "OUT": "VOUT",
                                    "GND": "VSS"}[m["pinfunction"]]
    viol, facts = _audit(comps, nets)
    assert ("ldo_heat", "error") not in _hits(viol)
    nj = _nj(facts, "ldo_heat")
    assert len(nj) == 1 and "no theta_ja" in nj[0]
