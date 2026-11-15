"""netlist_audit's rule 28: off-board inputs against the receiving pin's
absolute maximum ratings (reference/pin_abs_max.yaml), its level and its
clamps (class 3 of docs/miss-taxonomy.md).

The planted faults are the errata corpus's, cut down:
- zxdiag v1.1: a -5V input divided against the system under test's +5V
  into a Pico ADC pin, clamped by a 1N4148 to ground. With the far +5V
  gone the divider gives -5 V; the 1N4148's 1.0 V guaranteed drop lets the
  pin to -1 V against the RP2040's -0.5 V. A Schottky clamp is the fix.
- smart-lcc rev A: TX straight out a cable that also carries +12V, no
  clamp, no series resistor. Its rev B adds zeners.
- fc980c rev1: KEY_STATE from a +5V keyboard cable read by a 3.3 V MCU's
  5 V-tolerant pin with no pull. Its rev2 adds the pull-up.
- glasgow (a false hit, now an info note): DETECT on a +5V port read by
  the ECP5, whose pins can't take 5 V, so the line can only idle at the
  FPGA's own rail, a pull its gateware sets."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402

JST = "Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical"
HDR = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets, kinds="offboard_input_"):
    parsed = {"components": {r: {"value": v[0], "footprint": v[1],
                                 "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[])
    return ([x for x in viol if x["kind"].startswith(kinds)
             and x["kind"] != "offboard_input_weak_pull"], facts)


def _zx(clamp="1N4148"):
    """Z1.1 +5V and Z1.2 -5V from the system under test; R1 10K from +5V
    and R2 10K from -5V meet at DIV on the Pico's GP26; D1 from GND
    (anode) to DIV (cathode). The Pico's 3V3 comes from U2 on board."""
    comps = {"U1": ("Pico", ""), "U2": ("AP2112K-3.3", ""),
             "R1": ("10K", ""), "R2": ("10K", ""), "D1": (clamp, ""),
             "J1": ("Conn_01x03", JST)}
    nets = {"+5V": [_m("J1", "1", "Pin_1"), _m("R1", "1")],
            "-5V": [_m("J1", "2", "-5V"), _m("R2", "1")],
            "GND": [_m("J1", "3"), _m("D1", "2", "A"),
                    _m("U1", "38", "GND", "power_in"),
                    _m("U2", "2", "GND", "power_in")],
            "+3V3": [_m("U1", "36", "3V3", "power_in"),
                     _m("U2", "5", "VOUT", "power_out")],
            "MIN5V_DIV": [_m("R1", "2"), _m("R2", "2"), _m("D1", "1", "K"),
                          _m("U1", "31", "GP26", "bidirectional")]}
    return comps, nets


def test_zx_silicon_clamp_past_the_negative_rating():
    hits, facts = _audit(*_zx())
    got = {(h["kind"], h["net"]) for h in hits}
    assert got == {("offboard_input_clamp", "MIN5V_DIV"),
                   ("offboard_input_rail_absent", "MIN5V_DIV")}
    c = next(h for h in hits if h["kind"] == "offboard_input_clamp")
    assert c["severity"] == "error" and c["absent_rail"] == "+5V"
    assert c["level_v"] == -5.0 and c["held_v"] == -1.0 and c["vmin"] == -0.5
    assert "D1" in c["refs"] and "RP2040" in c["msg"]
    assert facts["offboard_inputs_judged"] >= 1


def test_zx_schottky_clamp_holds_the_low_side():
    # a part not in the diodes table counts as holding the pin; a clamp to
    # ground does nothing for the high side (far -5V gone: R1 takes it to 5 V)
    hits = _audit(*_zx("BAT54S"))[0]
    assert [(h["kind"], h["held_v"]) for h in hits] == [
        ("offboard_input_rail_absent", 5.0)]


def test_zx_no_clamp_is_rail_absent_only():
    comps, nets = _zx()
    del comps["D1"]
    for n in nets.values():
        n[:] = [m for m in n if m["ref"] != "D1"]
    hits = _audit(comps, nets)[0]
    assert [(h["kind"], h["held_v"]) for h in hits] == [
        ("offboard_input_rail_absent", -5.0)]


def test_zx_divider_from_an_on_board_rail_is_quiet():
    # +5V powered here (a regulator's input pin is on it): not off-board
    comps, nets = _zx()
    nets["+5V"].append(_m("U2", "1", "VIN", "power_in"))
    hits = _audit(comps, nets)[0]
    assert all(h.get("absent_rail") != "+5V" for h in hits)
    assert all(h["kind"] != "offboard_input_clamp" for h in hits)


def _lcc(fp=JST, hot=True):
    """J1: +12V (fed to U2), GND, TX straight to the RP2040's GPIO0."""
    comps = {"U1": ("RP2040", ""), "U2": ("AP2112K-3.3", ""),
             "J1": ("280372-1", fp)}
    nets = {"GND": [_m("J1", "2"), _m("U1", "57", "GND", "power_in"),
                    _m("U2", "2", "GND", "power_in")],
            "+3V3": [_m("U1", "1", "IOVDD", "power_in"),
                     _m("U2", "5", "VOUT", "power_out")],
            "TX_TO_CONTROL_BOARD": [_m("J1", "3"),
                                    _m("U1", "2", "GPIO0", "bidirectional")]}
    if hot:
        nets["+12V"] = [_m("J1", "1"), _m("U2", "1", "VIN", "power_in")]
    return comps, nets


def test_lcc_bare_cable_with_a_hot_rail_is_a_warning():
    hits, _ = _audit(*_lcc())
    assert [(h["kind"], h["severity"]) for h in hits] == [
        ("offboard_input_unprotected", "warning")]
    assert "+12V" in hits[0]["msg"] and hits[0]["margin_v"] == 0.5


def test_lcc_zener_on_the_port_is_quiet():
    comps, nets = _lcc()
    comps["D5"] = ("D_Zener_Small", "")
    nets["TX_TO_CONTROL_BOARD"].append(_m("D5", "1", "K"))
    nets["GND"].append(_m("D5", "2", "A"))
    assert _audit(comps, nets)[0] == []


def test_lcc_cable_with_no_rail_above_the_pin_is_quiet():
    # a keypad or matrix cable carries nothing that can push the pin
    assert _audit(*_lcc(hot=False))[0] == []


def test_lcc_pin_header_is_quiet():
    assert _audit(*_lcc(fp=HDR))[0] == []


_READERS = {"stm32": ("STM32G031K8T6", "VDD", "PB15"),
            "rp2040": ("RP2040", "IOVDD", "GPIO1"),
            "ecp5": ("LFE5U-25F-6BG256C", "VCCIO0", "PT18A")}


def _fc(fp=JST, pull=False, hot=True, reader="stm32", vdd_net="+3V3"):
    """J3: +5V to the keyboard (U3's supply), KEY_STATE to a 3.3 V
    reader's I/O pin as an input."""
    part, sup, io = _READERS[reader]
    comps = {"U1": (part, ""), "U2": ("AP2112K-3.3", ""),
             "J3": ("KEYBOARD", fp)}
    nets = {"GND": [_m("J3", "2"), _m("U1", "57", "VSS", "power_in"),
                    _m("U2", "2", "GND", "power_in")],
            vdd_net: [_m("U1", "1", sup, "power_in"),
                      _m("U2", "5", "VOUT", "power_out")],
            "KEY_STATE": [_m("J3", "3"), _m("U1", "3", io, "input")]}
    if hot:
        nets["+5V"] = [_m("J3", "1"), _m("U2", "1", "VIN", "power_in")]
    if pull:
        comps["R6"] = ("10K", "")
        nets["KEY_STATE"].append(_m("R6", "2"))
        nets["+5V"].append(_m("R6", "1"))
    return comps, nets


def test_fc_status_line_with_no_pull_floats():
    hits, _ = _audit(*_fc())
    fl = [h for h in hits if h["kind"] == "offboard_input_floating"]
    assert [(h["net"], h["severity"]) for h in fl] == [("KEY_STATE",
                                                        "warning")]
    assert "+5V" in fl[0]["msg"] and "SPRACK9" in fl[0]["msg"]


def test_fc_reader_rail_not_named_is_taken_as_3v3():
    # fc980c's STM32F401 sits on a net named VDDA, which carries no voltage
    hits, _ = _audit(*_fc(vdd_net="VDDA"))
    fl = [h for h in hits if h["kind"] == "offboard_input_floating"]
    assert [h["severity"] for h in fl] == ["warning"]
    assert "taken as 3.3 V" in fl[0]["msg"]


def test_fc_reader_rated_below_the_cable_rail_is_info():
    # glasgow: DETECT on a +5V port into the ECP5 (3.63 V max), and the
    # RP2040 (IOVDD + 0.5 V): the pull can only be at the reader's own
    # rail, set in gateware or firmware
    for reader in ("ecp5", "rp2040"):
        hits, _ = _audit(*_fc(reader=reader))
        fl = [h for h in hits if h["kind"] == "offboard_input_floating"]
        assert [h["severity"] for h in fl] == ["info"], reader
        assert "gateware or firmware" in fl[0]["msg"]
        assert fl[0]["pull"] == "configurable"


def test_fc_reader_with_no_rating_row_stays_a_warning():
    comps, nets = _fc()
    comps["U1"] = ("XYZ123", "")
    hits, _ = _audit(comps, nets)
    assert [h["severity"] for h in hits
            if h["kind"] == "offboard_input_floating"] == ["warning"]


def test_fc_pull_up_is_the_fix():
    hits, _ = _audit(*_fc(pull=True))
    assert all(h["kind"] != "offboard_input_floating" for h in hits)


def test_fc_cable_at_the_readers_own_level_is_quiet():
    # an internal pull serves a far end that idles at the reader's rail
    hits, _ = _audit(*_fc(hot=False))
    assert all(h["kind"] != "offboard_input_floating" for h in hits)


def test_fc_host_bus_edge_holds_its_own_lines():
    hits, _ = _audit(*_fc(fp="my:ZX_EDGE"))
    assert all(h["kind"] != "offboard_input_floating" for h in hits)


def test_unverified_row_is_reported_not_judged():
    table = netlist_audit.load_pin_abs_max()
    table["receivers"] = [dict(r, verified=False)
                          for r in table["receivers"]]
    comps, nets = _zx()
    parsed = {"components": {r: {"value": v[0], "footprint": v[1],
                                 "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[], pin_abs_max=table)
    assert not [x for x in viol if x["kind"].startswith("offboard_input_c")]
    assert facts["offboard_inputs_judged"] == 0
    assert any("offboard_input" in n for n in facts.get("not_judged", []))
