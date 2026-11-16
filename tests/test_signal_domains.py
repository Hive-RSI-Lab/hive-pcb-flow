"""netlist_audit's signal-level rules (lib/signal_domains.py): a control
signal's active level across a connection (class 14 of the gaps list),
and an I/O pin's supply domain against its net's level (class 15).

The planted faults are built from datasheets and general design
knowledge, each with a quiet twin that differs only in the fault:
- a supervisor's active-low reset output into an active-high reset input,
  directly, through a 0 ohm rename and through a TXB0104 channel;
- a regulator's power-good into a shutdown input that asserts high;
- a reset input asserting low with only a pull-down on it;
- a 74HC573's LE (transparent while high, TI SCLS147) tied to ground, a
  74HC595's RCLK (storage clock, TI SCLS041) tied to a rail or held by a
  pull, a 74HC165's ~PL tied low; a latch pin on an unlisted part is
  not_judged;
- a 5 V 74HC595 output into an RP2040 GPIO (IOVDD + 0.5 V abs max, RP2040
  datasheet 5.5.1, pin_abs_max.yaml);
- a 3.3 V RP2040 output into a 5 V 74HC input (VIH 3.15 V at 4.5 V, TI
  SN74HC245 recommended operating conditions);
- an I2C line pulled to 5 V into an RP2040."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402
import signal_domains  # noqa: E402

KINDS = ("polarity_", "idle_pull_asserted", "io_domain_",
         "latch_enable_held", "latch_enable_polarity")

HC_ROW = {"mpn": "SN74HC245", "match": r"^(SN)?74HC\d", "pintypes": "input",
          "vin_max_vdd": 0.0, "vih": 3.15, "vih_vdd": 4.5,
          "source": "TI SN74HC245 SCLS131 6.3", "verified": True}


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets, io_tolerance=None):
    parsed = {"components": {r: {"value": v, "footprint": "",
                                 "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    viol, facts = netlist_audit.audit(
        parsed, {}, None, pin_requirements=[], pin_states=[],
        stock_symbols="", io_tolerance=io_tolerance or [])
    return [x for x in viol if x["kind"].startswith(KINDS)], facts


def _rails(*parts):
    """+3V3, +5V and GND nets with each (ref, pin, rail, func) power pin."""
    nets = {"+3V3": [], "+5V": [], "GND": []}
    for ref, pin, rail, func in parts:
        nets[rail].append(_m(ref, pin, func, "power_in"))
    return nets


# ------------------------------------------------------------ name levels

def test_name_levels_read_every_active_low_marker():
    low = ["~{RESET}", "RST#", "nRST", "NRST", "RESET_N", "RESETB", "/RESET",
           "~RESET", "RST*", "PC6/~{RESET}", "RUN", "~{CS}", "CSN", "CSB"]
    for n in low:
        assert signal_domains.name_level(n)[1] == "low", n
    for n in ("RESET", "RST", "MR", "CS1", "CE2", "EN", "PG", "OE"):
        assert signal_domains.name_level(n)[1] == "high", n


def test_port_pins_and_plain_names_say_nothing():
    for n in ("PA3", "GPIO12", "PB2/~{SS}", "SCK", "D3", "ENB", "NC", "IO5"):
        assert signal_domains.name_level(n) is None, n


def test_inverted_stems_assert_their_kind_low():
    assert signal_domains.name_level("SHDN") == ("enable", "low")
    assert signal_domains.name_level("~{SHDN}") == ("enable", "high")
    assert signal_domains.name_level("FAULT") == ("good", "low")
    assert signal_domains.name_level("BUSY") == ("ready", "low")


# ------------------------------------------------------- polarity_mismatch

def _reset(active_high=True, via=None):
    """U1 TPS3839 supervisor (~{RESET} push-pull out) into U2's reset.
    via: None (one net), 'R' (a 0 ohm link), 'TXB' (a TXB0104 channel)."""
    comps = {"U1": "TPS3839", "U2": "FPGA_X"}
    nets = _rails(("U1", "3", "+3V3", "VDD"), ("U2", "9", "+3V3", "VCC"))
    rin = _m("U2", "5", "RESET" if active_high else "~{RESET}", "input")
    out = _m("U1", "1", "~{RESET}", "output")
    if via is None:
        nets["SYS_RST"] = [out, rin]
    elif via == "R":
        comps["R7"] = "0"
        nets["SV_RST"] = [out, _m("R7", "1")]
        nets["FPGA_RST"] = [_m("R7", "2"), rin]
    else:
        comps["U3"] = "TXB0104"
        nets["+3V3"] += [_m("U3", "1", "VCCA", "power_in"),
                         _m("U3", "14", "VCCB", "power_in")]
        nets["SV_RST"] = [out, _m("U3", "2", "A1", "bidirectional")]
        nets["FPGA_RST"] = [_m("U3", "13", "B1", "bidirectional"), rin]
    return comps, nets


def test_active_low_reset_into_active_high_input_is_an_error():
    hits, facts = _audit(*_reset())
    assert [(h["kind"], h["severity"], h["refs"]) for h in hits] == [
        ("polarity_mismatch", "error", ["U1", "U2"])]
    assert "~{RESET}" in hits[0]["msg"]
    assert facts["polarity_pins_checked"] >= 2


def test_matching_reset_levels_are_quiet():
    hits, _ = _audit(*_reset(active_high=False))
    assert hits == []


def test_mismatch_through_a_zero_ohm_rename():
    hits, _ = _audit(*_reset(via="R"))
    assert [h["kind"] for h in hits] == ["polarity_mismatch"]
    assert "R7" in hits[0]["msg"]
    assert _audit(*_reset(active_high=False, via="R"))[0] == []


def test_mismatch_through_a_translator_channel():
    hits, _ = _audit(*_reset(via="TXB"))
    assert [h["kind"] for h in hits] == ["polarity_mismatch"]
    assert "U3" in hits[0]["msg"]
    assert _audit(*_reset(active_high=False, via="TXB"))[0] == []


def test_an_inverter_is_never_joined():
    comps, nets = _reset()
    comps["U3"] = "74LVC1G04"
    nets["SV_RST"] = [nets.pop("SYS_RST")[0], _m("U3", "2", "A", "input")]
    nets["FPGA_RST"] = [_m("U3", "4", "Y", "output"),
                        _m("U2", "5", "RESET", "input")]
    assert _audit(comps, nets)[0] == []


def test_power_good_into_a_shutdown_input_is_flagged():
    comps = {"U1": "TPS62130", "U2": "LT3045"}
    nets = _rails(("U1", "1", "+5V", "PVIN"), ("U2", "1", "+5V", "IN"))
    nets["PG_3V3"] = [_m("U1", "3", "PG", "open_collector"),
                      _m("U2", "4", "SHDN", "input")]
    hits, _ = _audit(comps, nets)
    assert [(h["kind"], h["severity"]) for h in hits] == [
        ("polarity_mismatch", "warning")]
    nets["PG_3V3"][1] = _m("U2", "4", "~{SHDN}", "input")
    assert _audit(comps, nets)[0] == []


def test_irq_mismatch_is_only_a_warning():
    comps = {"U1": "SENSOR_X", "U2": "CPU_X"}
    nets = _rails(("U1", "1", "+3V3", "VDD"), ("U2", "1", "+3V3", "VDD"))
    nets["IRQ"] = [_m("U1", "2", "INT", "output"),
                   _m("U2", "7", "~{IRQ}", "input")]
    # U1's symbol marks its chip select, so its plain INT asserts high
    nets["SPI_CS"] = [_m("U1", "3", "~{CS}", "input"),
                      _m("U2", "8", "SPI_CS", "output")]
    hits, _ = _audit(comps, nets)
    assert [(h["kind"], h["severity"]) for h in hits] == [
        ("polarity_mismatch", "warning")]


def test_plain_strobes_on_a_part_with_no_bars_are_not_judged():
    # a YMF262-style symbol draws CS, RD and WR with no bars at all
    # (karabas-megabuzz U8): its plain names say nothing about level
    comps = {"U1": "FM_SYNTH", "U2": "CPLD_X"}
    nets = _rails(("U1", "1", "+5V", "VDD"), ("U2", "1", "+5V", "VCC"))
    nets["~{RD}"] = [_m("U1", "2", "RD", "input"),
                     _m("U2", "7", "~{RD}", "output")]
    nets["~{YM_CS}"] = [_m("U1", "3", "CS", "input"),
                        _m("U2", "8", "~{CS}", "output")]
    assert _audit(comps, nets)[0] == []


def test_pins_strapped_to_ground_are_not_a_polarity_line():
    # a ~{CS} and a CE tied to GND are straps (megabuzz U4, U9)
    comps = {"U1": "SWITCH_X", "U2": "SYNTH_X"}
    nets = _rails(("U1", "1", "+3V3", "VCC"), ("U2", "1", "+3V3", "VDD"))
    nets["GND"] += [_m("U1", "2", "CE", "input"),
                    _m("U1", "3", "~{OE}", "input"),
                    _m("U2", "2", "~{CS}", "input"),
                    _m("U2", "3", "OE", "input")]
    assert _audit(comps, nets)[0] == []


def test_multi_function_pin_names_are_not_judged():
    # CY7C68013A RDY0/SLRD meets an FPGA's BUSY (glasgow): firmware
    # picks the role
    assert signal_domains.name_level("RDY0/SLRD") is None
    assert signal_domains.name_level("PC6/~{RESET}") == ("reset", "low")
    comps = {"U1": "FX2_X", "U2": "FPGA_X"}
    nets = _rails(("U1", "1", "+3V3", "VCC"), ("U2", "1", "+3V3", "VCC"))
    nets["RD"] = [_m("U1", "2", "RDY0/SLRD", "input"),
                  _m("U2", "7", "BUSY", "bidirectional")]
    assert _audit(comps, nets)[0] == []


def test_b_suffix_on_a_net_is_not_a_bar():
    # PDLC_PG_B is channel B's power-good, not PG asserting low
    assert signal_domains.net_name_level("/PDLC_PG_B") is None
    assert signal_domains.net_name_level("/PG_N") == ("good", "low")


def test_an_enable_pulled_on_is_an_always_on_strap():
    comps = {"U1": "LDO_X", "R1": "100k"}
    nets = _rails(("U1", "1", "+5V", "VIN"))
    nets["EN"] = [_m("U1", "2", "EN", "input"), _m("R1", "1")]
    nets["+5V"].append(_m("R1", "2"))
    assert _audit(comps, nets)[0] == []


def test_one_parts_own_pins_are_not_compared():
    comps = {"U1": "SRAM_X"}
    nets = _rails(("U1", "28", "+5V", "VCC"))
    nets["SEL"] = [_m("U1", "20", "~{CE}", "input"),
                   _m("U1", "26", "CE2", "input")]
    assert _audit(comps, nets)[0] == []


# ------------------------------------------------------- polarity_net_name

def test_net_named_active_low_on_an_active_high_pin():
    comps = {"U1": "CPU_X", "U2": "PHY_X"}
    nets = _rails(("U1", "1", "+3V3", "VDD"), ("U2", "1", "+3V3", "VDD"))
    nets["~{PHY_RST}"] = [_m("U1", "4", "PA4", "bidirectional"),
                          _m("U2", "9", "RST", "input")]
    hits, _ = _audit(comps, nets)
    assert [h["kind"] for h in hits] == ["polarity_net_name"]
    nets["~{PHY_RST}"][1] = _m("U2", "9", "~{RST}", "input")
    assert _audit(comps, nets)[0] == []


def test_plain_net_names_say_nothing():
    comps = {"U1": "CPU_X", "U2": "PHY_X"}
    nets = _rails(("U1", "1", "+3V3", "VDD"), ("U2", "1", "+3V3", "VDD"))
    nets["PHY_RST"] = [_m("U1", "4", "PA4", "bidirectional"),
                       _m("U2", "9", "~{RST}", "input")]
    assert _audit(comps, nets)[0] == []


# ------------------------------------------------------ idle_pull_asserted

def _pulled(to):
    comps = {"U1": "CODEC_X", "R1": "10k"}
    nets = _rails(("U1", "1", "+3V3", "VDD"))
    nets[to].append(_m("R1", "2"))
    nets["CODEC_RST"] = [_m("U1", "5", "~{RESET}", "input"), _m("R1", "1")]
    return comps, nets


def test_reset_held_asserted_by_its_only_pull():
    hits, _ = _audit(*_pulled("GND"))
    assert [(h["kind"], h["severity"], h["refs"]) for h in hits] == [
        ("idle_pull_asserted", "error", ["U1"])]
    assert _audit(*_pulled("+3V3"))[0] == []


def test_a_driven_reset_is_not_judged_by_its_pull():
    comps, nets = _pulled("GND")
    comps["U2"] = "CPU_X"
    nets["CODEC_RST"].append(_m("U2", "7", "PB3", "bidirectional"))
    assert _audit(comps, nets)[0] == []


def test_a_header_on_the_line_can_drive_it():
    comps, nets = _pulled("GND")
    comps["J1"] = "Conn_01x02"
    nets["CODEC_RST"].append(_m("J1", "1"))
    assert _audit(comps, nets)[0] == []


def test_open_drain_reset_line_with_only_a_pull_down():
    comps, nets = _pulled("GND")
    comps["U2"] = "TPS3839"
    nets["CODEC_RST"].append(_m("U2", "1", "~{RESET}", "open_collector"))
    hits, _ = _audit(comps, nets)
    assert [h["kind"] for h in hits] == ["idle_pull_asserted"]
    assert "open-drain" in hits[0]["msg"]


# ------------------------------------------------------- latch_enable_held

def _latch(value, func, to, pin_type="input"):
    """U1 (VALUE) with its latch pin FUNC on net LATCH; TO is a rail to
    tie it to, 'pull+3V3'/'pullGND' for a lone pull, or 'mcu' for a
    controller output driving it."""
    comps = {"U1": value, "U2": "RP2040", "R1": "10k"}
    nets = _rails(("U1", "16", "+3V3", "VCC"), ("U1", "8", "GND", "GND"),
                  ("U2", "1", "+3V3", "IOVDD"))
    pin = _m("U1", "11", func, pin_type)
    if to in nets:
        nets[to].append(pin)
    else:
        nets["LATCH"] = [pin]
        if to.startswith("pull"):
            nets["LATCH"].append(_m("R1", "1"))
            nets[to[4:]].append(_m("R1", "2"))
        else:
            nets["LATCH"].append(_m("U2", "5", "GPIO5", "bidirectional"))
    return comps, nets


def _kinds(hits):
    return [(h["kind"], h["severity"], h["refs"]) for h in hits]


def test_a_transparent_high_latch_enable_tied_low_never_updates():
    hits, facts = _audit(*_latch("74HC573", "LE", "GND"))
    assert _kinds(hits) == [("latch_enable_held", "error", ["U1"])]
    assert "never update" in hits[0]["msg"]
    assert facts["latch_enables_checked"] == 1


def test_the_same_latch_enable_driven_or_held_transparent_is_quiet():
    assert _audit(*_latch("74HC573", "LE", "mcu"))[0] == []
    # held high, a 573 is a transparent buffer
    assert _audit(*_latch("74HC573", "LE", "+3V3"))[0] == []


def test_a_storage_clock_held_either_way_never_updates():
    for to in ("+3V3", "GND", "pullGND", "pull+3V3"):
        hits, _ = _audit(*_latch("SN74HC595DR", "RCLK", to))
        assert _kinds(hits) == [("latch_enable_held", "error", ["U1"])], to
    assert "only pull" in _audit(*_latch("74HC595", "RCLK", "pullGND"))[
        0][0]["msg"]
    assert _audit(*_latch("74HC595", "RCLK", "mcu"))[0] == []


def test_an_active_low_load_pin_is_read_through_its_bar():
    hits, _ = _audit(*_latch("74HC165", "~{PL}", "GND"))
    assert _kinds(hits) == [("latch_enable_held", "error", ["U1"])]
    hits, _ = _audit(*_latch("74HC165", "SH/~{LD}", "+3V3"))
    assert _kinds(hits) == [("latch_enable_held", "error", ["U1"])]
    assert _audit(*_latch("74HC165", "~{PL}", "mcu"))[0] == []


def test_an_active_low_latch_enable_held_high_never_updates():
    hits, _ = _audit(*_latch("74HC259", "~{LE}", "+3V3"))
    assert _kinds(hits) == [("latch_enable_held", "error", ["U1"])]
    assert _audit(*_latch("74HC259", "~{LE}", "GND"))[0] == []


def test_a_strobe_on_an_unlisted_part_is_not_judged():
    hits, facts = _audit(*_latch("DISPLAY_X", "STB", "GND"))
    assert hits == []
    assert any(n.startswith("latch_enable_held") for n in facts["not_judged"])
    # a latch-named pin someone drives is nothing to judge
    hits, facts = _audit(*_latch("DISPLAY_X", "STB", "mcu"))
    assert not any(n.startswith("latch_enable_held")
                   for n in facts.get("not_judged", []))


def test_other_pins_of_a_latch_are_not_its_enable():
    # a 573's ~OE tied low is the usual always-driving strap
    assert _audit(*_latch("74HC573", "~{OE}", "GND"))[0] == []
    assert _audit(*_latch("74HC595", "SRCLK", "mcu"))[0] == []

# --------------------------------------------------- latch_enable_polarity

def _drive(value, func, drv, inverter=False, drv_type="output"):
    """U1 (VALUE) with its latch pin FUNC on net LATCH, driven by U3 (a
    CPLD that also has a ~{RD} output) through its pin DRV; with INVERTER
    the line goes through U4 (74HC04) pin 1 in, pin 2 out."""
    comps = {"U1": value, "U3": "XC9572", "U4": "74HC04"}
    nets = _rails(("U1", "20", "+3V3", "VCC"), ("U1", "10", "GND", "GND"),
                  ("U3", "1", "+3V3", "VCC"))
    nets["RD_N"] = [_m("U3", "3", "~{RD}", "output")]
    src = _m("U3", "2", drv, drv_type)
    pin = _m("U1", "11", func, "input")
    if inverter:
        nets["STB_N"] = [src, _m("U4", "1", "1A", "input")]
        nets["LATCH"] = [pin, _m("U4", "2", "1Y", "output")]
    else:
        nets["LATCH"] = [pin, src]
    return comps, nets


def test_an_active_low_select_into_a_transparent_high_enable_is_a_fault():
    hits, facts = _audit(*_drive("74HC573", "LE", "~{CS}"))
    assert _kinds(hits) == [("latch_enable_polarity", "error", ["U1"])]
    assert "asserts low" in hits[0]["msg"]
    assert facts["latch_drive_checked"] == 1


def test_the_same_select_through_an_inverter_is_quiet():
    hits, facts = _audit(*_drive("74HC573", "LE", "~{CS}", inverter=True))
    assert hits == []
    assert facts["latch_drive_checked"] == 1


def test_a_matching_level_or_a_port_pin_driver_is_quiet():
    assert _audit(*_drive("74HC573", "LE", "ALE"))[0] == []
    assert _audit(*_drive("74HC259", "~{LE}", "~{WR}"))[0] == []
    assert _audit(*_drive("74HC573", "LE", "GPIO5", drv_type="bidirectional"))[0] == []
    assert _audit(*_drive("74HC273", "LE", "Q0"))[0] == []


def test_a_plain_strobe_into_an_active_low_load_is_a_fault():
    hits, _ = _audit(*_drive("74HC165", "~{PL}", "STB"))
    assert _kinds(hits) == [("latch_enable_polarity", "error", ["U1"])]


def test_an_edge_storage_clock_is_not_judged_for_polarity():
    hits, facts = _audit(*_drive("74HC595", "RCLK", "~{CS}"))
    assert hits == []
    assert facts["latch_drive_checked"] == 0


def test_a_driver_of_unknown_level_is_not_judged():
    comps, nets = _drive("74HC573", "LE", "1")
    nets["LATCH"] = [nets["LATCH"][0], _m("J1", "1", "1", "passive")]
    comps["J1"] = "Conn_01x02"
    hits, facts = _audit(comps, nets)
    assert hits == []
    nj = facts["not_judged"]
    assert any("latch_enable_polarity on LATCH" in x for x in nj), nj


# ---------------------------------------------------------- io domain

def _hc_into_rp2040(hc_rail="+5V", series=None):
    """U1 74HC595 Q output into U2 RP2040 GPIO3, U2's IOVDD on +3V3."""
    comps = {"U1": "74HC595", "U2": "RP2040"}
    nets = _rails(("U1", "16", hc_rail, "VCC"),
                  ("U2", "1", "+3V3", "IOVDD"), ("U2", "2", "+3V3", "IOVDD"))
    q = _m("U1", "15", "QA", "tri_state")
    g = _m("U2", "5", "GPIO3", "bidirectional")
    if series:
        comps["R3"] = series
        nets["QA"] = [q, _m("R3", "1")]
        nets["GP3"] = [_m("R3", "2"), g]
    else:
        nets["QA"] = [q, g]
    return comps, nets


def test_5v_output_into_a_3v3_rp2040_is_an_error():
    hits, facts = _audit(*_hc_into_rp2040())
    assert [(h["kind"], h["severity"], h["refs"]) for h in hits] == [
        ("io_domain_overdrive", "error", ["U1", "U2"])]
    assert "3.80 V" in hits[0]["msg"]
    assert facts["io_domain_pairs_checked"] >= 1


def test_same_rail_is_quiet():
    assert _audit(*_hc_into_rp2040("+3V3"))[0] == []


def test_overdrive_through_a_series_resistor():
    hits, _ = _audit(*_hc_into_rp2040(series="220"))
    assert [h["kind"] for h in hits] == ["io_domain_overdrive"]
    assert "R3" in hits[0]["msg"]


def test_a_divider_sized_resistor_breaks_the_path():
    assert _audit(*_hc_into_rp2040(series="10k"))[0] == []


def _rp2040_into_hc(hc_rail="+5V"):
    comps = {"U1": "RP2040", "U2": "74HC245"}
    nets = _rails(("U1", "1", "+3V3", "IOVDD"), ("U2", "20", hc_rail, "VCC"))
    nets["D0"] = [_m("U1", "5", "GPIO3", "bidirectional"),
                  _m("U2", "2", "A1", "input")]
    return comps, nets


def test_3v3_output_into_a_5v_hc_input_is_a_warning():
    hits, _ = _audit(*_rp2040_into_hc(), io_tolerance=[HC_ROW])
    assert [(h["kind"], h["severity"]) for h in hits] == [
        ("io_domain_underdrive", "warning")]
    assert "3.50 V" in hits[0]["msg"]  # 3.15 * 5 / 4.5
    assert _audit(*_rp2040_into_hc("+3V3"), io_tolerance=[HC_ROW])[0] == []


def test_an_unverified_row_is_not_judged():
    row = dict(HC_ROW, verified=False)
    hits, facts = _audit(*_rp2040_into_hc(), io_tolerance=[row])
    assert hits == []
    assert any("io_domain_underdrive" in n for n in facts["not_judged"])


def test_no_row_overdrive_goes_to_not_judged():
    comps = {"U1": "74HC595", "U2": "SENSOR_X"}
    nets = _rails(("U1", "16", "+5V", "VCC"), ("U2", "1", "+3V3", "VDD"))
    nets["QA"] = [_m("U1", "15", "QA", "tri_state"),
                  _m("U2", "3", "DIN", "input")]
    hits, facts = _audit(comps, nets)
    assert hits == []
    assert any("SENSOR_X" in n and "tolerant" in n
               for n in facts["not_judged"])


def test_a_multi_rail_part_is_not_judged():
    comps, nets = _hc_into_rp2040()
    nets["+5V"].append(_m("U2", "3", "VDD_X", "power_in"))
    nets["+3V3"] = [m for m in nets["+3V3"] if m["pin"] != "2"]
    nets["+5V"].append(_m("U2", "2", "IOVDD", "power_in"))
    assert _audit(comps, nets)[0] == []


def _i2c(pull_rail="+5V"):
    comps = {"U1": "RP2040", "U2": "TMP102", "R1": "4.7k"}
    nets = _rails(("U1", "1", "+3V3", "IOVDD"), ("U2", "4", "+3V3", "V+"))
    nets[pull_rail].append(_m("R1", "2"))
    nets["SDA"] = [_m("U1", "5", "GPIO4", "bidirectional"),
                   _m("U2", "6", "SDA", "bidirectional"), _m("R1", "1")]
    return comps, nets


def test_i2c_pulled_to_5v_into_a_3v3_part_is_an_error():
    hits, _ = _audit(*_i2c())
    assert [(h["kind"], h["severity"], h["refs"]) for h in hits] == [
        ("io_domain_pull_rail", "error", ["R1", "U1"])]
    assert _audit(*_i2c("+3V3"))[0] == []


# ------------------------------------------------- reference/io_tolerance

def test_every_tolerance_row_cites_its_datasheet():
    doc = yaml.safe_load((REPO / "reference" / "io_tolerance.yaml")
                         .read_text(encoding="utf-8"))
    assert doc["receivers"]
    for row in doc["receivers"]:
        assert set(row) <= {"mpn", "match", "verified", "pins", "pintypes",
                            "vin_max", "vin_max_vdd", "vih", "vih_vdd",
                            "vih_frac", "basis", "source", "cite"}, row["mpn"]
        for k in ("match", "pins", "pintypes"):
            if k in row:
                re.compile(row[k])
        assert isinstance(row["verified"], bool), row["mpn"]
        assert re.search(r"\d", row["source"]), row["source"]
        assert all({"doc", "sha256", "page", "quote", "fields"} <= set(c)
                   for c in row["cite"]), row["mpn"]


def test_the_shipped_table_judges_a_74hc_on_5v():
    comps, nets = _rp2040_into_hc()
    parsed = {"components": {r: {"value": v, "footprint": "",
                                 "libsource": ("", "")}
                             for r, v in comps.items()}, "nets": nets}
    viol, _ = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                  pin_states=[], stock_symbols="")
    assert [x["kind"] for x in viol if x["kind"].startswith(KINDS)] == [
        "io_domain_underdrive"]
    comps["U2"] = "74HCT245"
    parsed["components"]["U2"]["value"] = "74HCT245"
    viol, _ = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                  pin_states=[], stock_symbols="")
    assert [x for x in viol if x["kind"].startswith(KINDS)] == []


# ------------------------------------------------ golden mutants' twins

MUTANTS = (("reset_polarity_max810", "polarity_mismatch", "U98"),
           ("io_5v_hc_into_3v3", "io_domain_overdrive", "U96"),
           ("io_3v3_into_5v_hc", "io_domain_underdrive", "U95"))


def test_each_golden_mutant_fires_and_its_twin_is_quiet(tmp_path):
    """The manifest's netlist_audit_signal.yaml mutants on usbbuck4: the
    planted netlist is caught, and the same surgery without the fault (or
    the board as it is) is quiet."""
    import importlib
    sys.path.insert(0, str(REPO / "tests" / "golden" / "mutations"))
    base = REPO / "tests" / "golden" / "netlist" / "usbbuck4.net"
    for name, kind, ref in MUTANTS:
        mod = importlib.import_module(name)
        for fault in (True, False):
            p = tmp_path / f"{name}-{fault}.net"
            p.write_text(mod.netlist(fault), encoding="utf-8")
            payload, _ = netlist_audit.run_audit(p, None, None, None)
            hits = [v for v in payload["violations"]
                    if v["kind"].startswith(KINDS)]
            if fault:
                assert [(h["kind"], ref in h["refs"]) for h in hits] == [
                    (kind, True)], name
            else:
                assert hits == [], (name, hits)
    payload, _ = netlist_audit.run_audit(base, None, None, None)
    assert [v for v in payload["violations"]
            if v["kind"].startswith(KINDS)] == []


def _nameless(value, footprint, to):
    """U1 (VALUE, FOOTPRINT) from a library with no pin names: every pin
    is a bare number. Its pin 15 (a 74x166's ~PE) is on LOAD, tied to TO
    or driven by U2's GPIO ('mcu')."""
    comps, nets = _latch(value, "", to, pin_type="")
    for ms in nets.values():
        for m in ms:
            if m["ref"] == "U1":
                m["pinfunction"], m["pintype"] = "", ""
                if m["pin"] == "11":
                    m["pin"] = "15"
    parsed_fp = {"U1": footprint}
    return comps, nets, parsed_fp


def _audit_fp(comps, nets, fps):
    parsed = {"components": {r: {"value": v, "footprint": fps.get(r, ""),
                                 "libsource": ("Custom", v)}
                             for r, v in comps.items()},
              "nets": nets}
    viol, facts = netlist_audit.audit(
        parsed, {}, None, pin_requirements=[], pin_states=[],
        stock_symbols="", io_tolerance=[])
    return [x for x in viol if x["kind"].startswith(KINDS)], facts


SOIC16 = "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm"


def test_a_nameless_166_in_an_unknown_package_is_listed_not_skipped():
    hits, facts = _audit_fp(*_nameless("74HC166", "Custom:MY_PKG", "GND"))
    assert hits == []
    nj = [n for n in facts["not_judged"] if "latch_enable_held" in n]
    assert len(nj) == 1 and "no pin names" in nj[0] and "U1" in nj[0]
    assert facts["latch_enables_checked"] == 0


def test_a_nameless_166_pe_held_in_soic16_is_a_fault_by_pin_table():
    for to in ("GND", "+3V3", "pull+3V3"):
        hits, facts = _audit_fp(*_nameless("74HC166", SOIC16, to))
        assert _kinds(hits) == [("latch_enable_held", "error", ["U1"])], to
        assert "pin table" in hits[0]["msg"]
        assert facts["latch_enables_checked"] == 1
        assert not [n for n in facts.get("not_judged", []) if "no pin names" in n]


def test_a_nameless_166_pe_driven_in_soic16_is_quiet():
    # gp8b-v50 rev_b: a custom 74HC166, ~PE (pin 15) from U6
    hits, facts = _audit_fp(*_nameless("74HC166", SOIC16, "mcu"))
    assert hits == []
    assert facts["latch_enables_checked"] == 1


def test_a_nameless_part_reads_only_its_family_pin():
    # pin 15 of a 595 is QA, not its storage clock: not checked
    hits, facts = _audit_fp(*_nameless("74HC595", "Package_DIP:DIP-16_W7.62mm",
                                       "GND"))
    assert hits == [] and facts["latch_enables_checked"] == 0
    # a 20-pin package for a 16-pin family is not that pinout
    hits, facts = _audit_fp(*_nameless(
        "74HC166", "Package_DFN_QFN:DHVQFN-16-1EP_2.5x3.5mm_P0.5mm", "GND"))
    assert hits == []
    assert any("no pin names" in n for n in facts.get("not_judged", []))
