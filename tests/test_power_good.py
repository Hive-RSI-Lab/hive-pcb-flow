"""netlist_audit's power_good_unused rule (#1030).

The planted fault is the errata corpus's pdlc v1 (the LM5158 PGOOD lights an
LED and gates nothing); its fix, v2 (PGOOD into a 74LVC1G08 and an MCU
input), must stay quiet."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets):
    parsed = {"components": {r: {"value": v, "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[])
    return [x for x in viol if x["kind"] == "power_good_unused"], facts


def _rails():
    return {"+12V": [_m("U1", "1", "VIN", "power_in")],
            "+3.3V": [], "GND": [_m("U1", "2", "GND", "power_in")]}


def _v1():
    """PGOOD -> R1 2.2k -> D2 (LED) cathode; the anode on a rail."""
    nets = _rails()
    nets["PGOOD"] = [_m("U1", "4", "PGOOD", "output"), _m("R1", "2")]
    nets["Net-(D2-K)"] = [_m("R1", "1"), _m("D2", "1", "K")]
    nets["+12V"].append(_m("D2", "2", "A"))
    return {"U1": "LM5158", "R1": "2.2K", "D2": "RED"}, nets


def _v2():
    """PGOOD pulled up to 3.3 V into a gate and an MCU pin."""
    nets = _rails()
    nets["+3.3V"].append(_m("R6", "1"))
    nets["/HV_PG"] = [_m("U4", "4", "PGOOD", "output"), _m("R6", "2"),
                      _m("U3", "1", "", "input"),
                      _m("U5", "41", "PB5", "bidirectional")]
    return {"U4": "LM5158", "R6": "22K", "U3": "74LVC1G08",
            "U5": "STM32L151C8T6A"}, nets


def test_v1_pgood_that_only_lights_an_led_is_a_warning():
    hits, facts = _audit(*_v1())
    assert [(h["net"], h["severity"]) for h in hits] == [("PGOOD", "warning")]
    assert hits[0]["refs"] == ["D2", "R1", "U1"]   # v() sorts them
    assert hits[0]["pg_pin"] == "U1.4"
    assert facts["power_good_pins_checked"] == 1


def test_v2_pgood_into_a_gate_and_an_mcu_is_quiet():
    hits, facts = _audit(*_v2())
    assert hits == [] and facts["power_good_pins_checked"] == 1


def test_a_reader_one_series_resistor_on_counts():
    comps, nets = _v1()
    comps["U9"] = "LM5108"
    nets["Net-(D2-K)"].append(_m("U9", "3", "EN", "input"))
    assert _audit(comps, nets)[0] == []


def test_a_pgood_to_a_connector_may_be_read_off_board():
    comps, nets = _v1()
    comps["J1"] = "Conn_01x02"
    nets["PGOOD"].append(_m("J1", "1"))
    assert _audit(comps, nets)[0] == []


def test_an_unconnected_pgood_is_a_choice():
    comps, nets = _v1()
    nets["unconnected-(U1-PGOOD-Pad4)"] = [nets.pop("PGOOD")[0]]
    del nets["Net-(D2-K)"]
    hits, facts = _audit({"U1": "LM5158"}, nets)
    assert hits == [] and facts["power_good_pins_checked"] == 0


def test_mcu_gpio_named_pg_n_is_not_a_power_good():
    nets = _rails()
    nets["/LED"] = [_m("U5", "10", "PG5", "bidirectional"), _m("R3", "1")]
    nets["Net-(D3-A)"] = [_m("R3", "2"), _m("D3", "2", "A")]
    hits, facts = _audit({"U5": "STM32F407VG", "R3": "1k", "D3": "LED"},
                         nets)
    assert hits == [] and facts["power_good_pins_checked"] == 0


def test_wired_or_pgoods_with_no_reader_still_fire():
    comps, nets = _v1()
    comps["U2"] = "TPS62130"
    nets["PGOOD"].append(_m("U2", "7", "PG", "open_collector"))
    hits, _ = _audit(comps, nets)
    assert len(hits) == 2 and {h["pg_pin"] for h in hits} == {"U1.4", "U2.7"}
