"""netlist_audit's offboard_input_weak_pull rule (#1079).

The planted fault is the errata corpus's pdlc v1: its EN terminal (J1.3) is
held only by R4 100k to +12V and drives a PNP's base through R5 20k (a 100n
cap at the base), and the errata says it is "much too sensitive when left
floating". Its fix, v2 (22k to +3.3V, 2.2k and 100n into an MCU pin, an ESD
array on the terminal), must stay quiet. The 10k ceiling is TI SPRACK9's
(4.7k to 10k on a line that may be disconnected); the clamp is Microchip
AN2587's PCB rule 8 (TVS on all external connector I/O pins)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402

KIND = "offboard_input_weak_pull"


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets):
    parsed = {"components": {r: {"value": v, "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[])
    return [x for x in viol if x["kind"] == KIND], facts


def _v1():
    """J1.3 EN: R4 100K to +12V, R5 20K to Q1's base, C7 100n base-GND."""
    nets = {"+12V": [_m("U1", "3", "BIAS", "power_in"), _m("R4", "1"),
                     _m("Q1", "2", "E")],
            "GND": [_m("U1", "2", "GND", "power_in"), _m("J1", "2"),
                    _m("C7", "2"), _m("JP2", "2")],
            "/~{EN}": [_m("J1", "3", "3"), _m("JP2", "1", "A"),
                       _m("R4", "2"), _m("R5", "1")],
            "Net-(Q1-B)": [_m("C7", "1"), _m("Q1", "1", "B"), _m("R5", "2")],
            "Net-(Q1-C)": [_m("Q1", "3", "C"), _m("R6", "1")],
            "/UVLO": [_m("R6", "2"), _m("U1", "6", "EN/UVLO", "input")]}
    return {"U1": "LM5158", "R4": "100K", "R5": "20K", "R6": "100K",
            "C7": "100n", "Q1": "S8550", "J1": "TBP01R1-508-06BE",
            "JP2": "SolderJumper_2_Open"}, nets


def _v2():
    """J6.2 CTRL_EN_IN: R24 22K to +3.3V, D4 ESD array, R28 2.2K to PA2
    with C28 100n to GND."""
    nets = {"+3.3V": [_m("U5", "1", "VDD", "power_in"), _m("R24", "1")],
            "GND": [_m("U5", "8", "VSS", "power_in"), _m("J6", "1"),
                    _m("C28", "2"), _m("D4", "2"), _m("JP1", "1")],
            "/~{CTRL_EN_IN}": [_m("D4", "6", "K5"), _m("J6", "2", "Pin_2"),
                               _m("JP1", "2", "B"), _m("R24", "2"),
                               _m("R28", "2")],
            "/~{CTRL_EN}": [_m("C28", "1"), _m("R28", "1"),
                            _m("U5", "12", "PA2", "bidirectional")]}
    return {"U5": "STM32L151C8T6A", "R24": "22K", "R28": "2.2K",
            "C28": "100n", "D4": "PESD3V3L5UY", "J6": "TBP03R1-350-06BE",
            "JP1": "SolderJumper_2_Open"}, nets


def test_v1_weak_pull_on_en_terminal_is_a_warning():
    hits, facts = _audit(*_v1())
    assert [(h["net"], h["severity"]) for h in hits] == [("/~{EN}",
                                                         "warning")]
    assert hits[0]["refs"] == ["J1", "Q1", "R4", "R5"]
    assert hits[0]["pull_ohm"] == 100e3 and hits[0]["pull_to"] == "+12V"
    assert "SPRACK9" in hits[0]["msg"] and "AN2587" in hits[0]["msg"]
    assert facts["offboard_inputs_checked"] == 1


def test_v2_clamped_input_is_quiet():
    hits, facts = _audit(*_v2())
    assert hits == [] and facts["offboard_inputs_checked"] == 1


def test_v2_without_its_clamp_fires():
    comps, nets = _v2()
    del comps["D4"]
    for n in nets.values():
        n[:] = [m for m in n if m["ref"] != "D4"]
    assert [h["net"] for h in _audit(comps, nets)[0]] == ["/~{CTRL_EN_IN}"]


def test_a_10k_pull_is_stiff_enough():
    comps, nets = _v1()
    comps["R4"] = "10K"
    assert _audit(comps, nets)[0] == []


def test_an_rc_filter_alone_does_not_exempt():
    # v1 already has one (20k into 100n at the base) and still failed
    comps, nets = _v1()
    assert "C7" in comps and _audit(comps, nets)[0]


def test_an_on_board_driver_sets_the_level():
    comps, nets = _v1()
    comps["U9"] = "74LVC1G07"
    nets["/~{EN}"].append(_m("U9", "4", "Y", "open_collector"))
    assert _audit(comps, nets)[0] == []


def test_a_transistor_switch_on_the_net_is_a_driver():
    comps, nets = _v1()
    comps["Q9"] = "2N7002"
    nets["/~{EN}"].append(_m("Q9", "3", "D"))
    assert _audit(comps, nets)[0] == []


def test_a_net_named_as_an_output_is_skipped():
    comps, nets = _v1()
    nets["/CONSOLE_TX"] = nets.pop("/~{EN}")
    assert _audit(comps, nets)[0] == []


def test_a_feedback_divider_on_a_connector_is_not_an_input():
    comps, nets = _v1()
    nets["Net-(Q1-B)"] = [_m("R5", "2"), _m("U1", "9", "FB", "input"),
                          _m("C7", "1")]
    nets["Net-(Q1-C)"] = [_m("R6", "1")]
    assert _audit(comps, nets)[0] == []


def test_no_reader_no_check():
    comps, nets = _v1()
    nets["Net-(Q1-B)"] = [_m("C7", "1"), _m("R5", "2")]
    hits, facts = _audit(comps, nets)
    assert hits == [] and facts["offboard_inputs_checked"] == 0
