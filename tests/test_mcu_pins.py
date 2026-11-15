"""reference/mcu_pins.yaml and netlist_audit's MCU pin-function rules
(#1032): mcu_pwm_timer and mcu_input_only_drive; mcu_pwm_pair (#1115).

Each rule is seeded from an errata-corpus defect; each test plants that
board's fault (rev A) and its fix (rev B, which must stay quiet)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402

KINDS = ("mcu_pwm_timer", "mcu_input_only_drive", "mcu_pwm_pair")


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets):
    parsed = {"components": {r: (v if isinstance(v, dict) else
                                 {"value": v, "libsource": ("", "")})
                             for r, v in comps.items()},
              "nets": nets}
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[])
    return [x for x in viol if x["kind"] in KINDS], facts


def _bedlift(pins, mcu="STM32C031K6T6", symbol="STM32C031K6Tx",
             pmode="+3.3V"):
    """Two DRV8874s (U5, U6) whose EN/IN1, PH/IN2 come from MCU U3's
    `pins` (U5 IN1, U5 IN2, U6 IN1, U6 IN2); PMODE on net `pmode`."""
    comps = {"U3": {"value": mcu, "libsource": ("MCU_ST_STM32C0", symbol)},
             "U5": "DRV8874PWPR", "U6": "DRV8874PWPR"}
    nets = {"+3.3V": [_m("U3", "1", "VDD", "power_in")],
            "GND": [_m("U3", "2", "VSS", "power_in")]}
    for (u, func), mp in zip([("U5", "EN/IN1"), ("U5", "PH/IN2"),
                              ("U6", "EN/IN1"), ("U6", "PH/IN2")], pins):
        nets[f"/{u}_{func[-3:]}"] = [_m(u, func[-1], func, "input"),
                                     _m("U3", mp, mp, "bidirectional")]
    for u in ("U5", "U6"):
        nets.setdefault(pmode, []).append(_m(u, "16", "PMODE", "input"))
    return comps, nets


def test_bedlift_v02_inputs_split_over_timers_is_the_planted_fault():
    # v0.2: PB0 (TIM1_CH2N/TIM3_CH3), PA15 (TIM1_CH1), PC6 (TIM3_CH1),
    # PA8: TIM1 lacks a channel for PC6, TIM3 one for PA15
    hits, facts = _audit(*_bedlift(["PB0", "PA15", "PC6", "PA8"]))
    assert [(h["kind"], h["refs"], h["severity"]) for h in hits] == [
        ("mcu_pwm_timer", ["U3", "U5", "U6"], "warning")]
    assert "PA15 (TIM1_CH1)" in hits[0]["msg"]
    assert "SLVSF66A" in hits[0]["msg"]
    assert facts["mcu_pwm_groups_checked"] == 1


def test_bedlift_v1_inputs_on_one_timer_is_quiet():
    # v1: a C051 drawn with a C031 symbol; PA0-PA3 are TIM1_CH1-4 and
    # TIM2_CH1-4. The C031 table would have them too, but the Value
    # decides which part it is.
    hits, facts = _audit(*_bedlift(["PA0", "PA1", "PA2", "PA3"],
                                   mcu="STM32C051C8T6",
                                   symbol="STM32C031C6Tx"))
    assert hits == [] and facts["mcu_pwm_groups_checked"] == 1


def test_the_designers_fix_tim3_on_every_input_is_quiet():
    # bedlift's errata: swap so LIFT_IN2 takes TIM3_CH2, the rest TIM3
    assert _audit(*_bedlift(["PB0", "PB5", "PC6", "PA8"]))[0] == []


def test_complementary_outputs_share_their_channel():
    # PA3 (TIM1_CH1N) and PA0 (TIM1_CH1) are one channel; PA1, PA2 add
    # CH2, CH3 and nothing else is TIM1-only without CH1: no fit
    hits, _ = _audit(*_bedlift(["PA0", "PA3", "PA9", "PA10"]))
    assert hits == []  # PA3 can still take TIM1_CH4
    hits, _ = _audit(*_bedlift(["PA0", "PA15", "PA9", "PA10"]))
    assert [h["kind"] for h in hits] == ["mcu_pwm_timer"]


def test_ph_en_mode_takes_no_pwm_group():
    # PMODE low: PH/EN mode, IN1 is the enable and the rule is off
    comps, nets = _bedlift(["PB0", "PA15", "PC6", "PA8"], pmode="GND")
    assert _audit(comps, nets)[0] == []


def test_a_pwm_input_on_a_pin_with_no_timer_fires():
    hits, _ = _audit(*_bedlift(["PA0", "PA1", "PA2", "PA13"]))
    assert "PA13 (no timer)" in hits[0]["msg"]


def _glasgow(link_to):
    """FX2 U2 with RDY0/SLRD on /RD and CTL0/FLAGA on /FLAGA; flash U9's
    ~{CS} on /CSSPIN, joined over 0R R40 to `link_to`."""
    comps = {"U2": "CY7C68013A-56LTX", "U9": "W25Q32JVZP", "R40": "0R"}
    nets = {"/RD": [_m("U2", "1", "RDY0/SLRD", "input")],
            "/FLAGA": [_m("U2", "29", "CTL0/FLAGA", "output")],
            "/CSSPIN": [_m("U9", "1", "~{CS}", "input"), _m("R40", "1")]}
    nets[link_to].append(_m("R40", "2"))
    return comps, nets


def test_glasgow_revc3_flash_select_on_rdy0_is_the_planted_fault():
    hits, facts = _audit(*_glasgow("/RD"))
    assert [(h["kind"], h["refs"], h["net"]) for h in hits] == [
        ("mcu_input_only_drive", ["U2", "U9"], "/CSSPIN")]
    assert "38-08032" in hits[0]["msg"]
    assert facts["mcu_input_only_parts_checked"] == 1


def test_glasgow_flash_select_on_flaga_is_quiet():
    assert _audit(*_glasgow("/FLAGA"))[0] == []


def test_a_non_0r_link_is_not_a_tie():
    comps, nets = _glasgow("/RD")
    comps["R40"] = "10k"
    assert _audit(comps, nets)[0] == []


def test_an_mcu_with_no_row_is_not_judged():
    hits, facts = _audit(*_bedlift(["PB0", "PA15", "PC6", "PA8"],
                                   mcu="ATSAMD21G18A", symbol="ATSAMD21G"))
    assert hits == [] and facts["mcu_pwm_groups_checked"] == 0


def test_every_row_cites_its_source():
    doc = yaml.safe_load((REPO / "reference" / "mcu_pins.yaml").read_text())
    for row in doc["mcus"] + doc["loads"]:
        assert row["verified"] is True and row["match"], row["mpn"]
        assert row["source"].strip()
        for funcs in (row.get("timers") or {}).values():
            assert all(netlist_audit._TIM_RE.match(f) for f in funcs), funcs
    for row in doc["loads"]:
        assert (row.get("pwm_group") or row.get("pwm_pairs")) \
            and row["why"].strip()
        for pair in row.get("pwm_pairs") or ():
            assert len(pair) == 2, pair


@pytest.mark.parametrize("inputs,want", [
    ([["TIM1_CH1"], ["TIM1_CH1N"]], None),           # one channel, two pins
    ([["TIM1_CH1", "TIM1_CH2"], ["TIM1_CH1"]], "TIM1"),
    ([["TIM3_CH1"], ["TIM1_CH2", "TIM3_CH2"]], "TIM3"),
])
def test_one_timer_matches_channels(inputs, want):
    assert netlist_audit._one_timer([("", i) for i in inputs]) == want


def _bldc(pins, mcu="STM32G431CBT6", links=None):
    """PCB-0018's bldc board: a DRV8300 U301 whose INHA, INLA, INHB, INLB,
    INHC, INLC come from G431 U601's `pins` (None: that input is tied to
    ground). LINKS: {input: 0R resistor value} puts a series resistor
    between the input and the MCU pin."""
    comps = {"U601": {"value": mcu, "libsource": ("aiee", mcu)},
             "U301": {"value": "DRV8300DRGER",
                      "libsource": ("aiee", "DRV8300DRGER_C3655801")}}
    nets = {"+3.3V": [_m("U601", "1", "VDD", "power_in")],
            "GND": [_m("U601", "2", "VSS", "power_in")]}
    for (func, num), mp in zip([("INHA", "22"), ("INLA", "1"),
                                ("INHB", "23"), ("INLB", "2"),
                                ("INHC", "24"), ("INLC", "3")], pins):
        if mp is None:
            nets["GND"].append(_m("U301", num, func))
            continue
        link = (links or {}).get(func)
        if link is None:
            nets[f"/{func}"] = [_m("U301", num, func),
                                _m("U601", mp, mp, "bidirectional")]
        else:
            r = f"R{num}"
            comps[r] = link
            nets[f"/{func}"] = [_m("U301", num, func), _m(r, "1")]
            nets[f"/{func}_MCU"] = [_m(r, "2"),
                                    _m("U601", mp, mp, "bidirectional")]
    return comps, nets


_BLDC = ["PA8", "PB13", "PA9", "PB14", "PA10", "PB15"]  # TIM1 CH1-3/CH1N-3N


def test_bldc_board_tim1_pairs_are_quiet():
    hits, facts = _audit(*_bldc(_BLDC))
    assert hits == [] and facts["mcu_pwm_pairs_checked"] == 1


def test_a_leg_off_its_channels_complement_is_the_planted_fault():
    # INLA and INLB swapped: PA8 (TIM1_CH1) pairs with PB14 (TIM1_CH2N)
    hits, facts = _audit(*_bldc(["PA8", "PB14", "PA9", "PB13", "PA10",
                                 "PB15"]))
    assert [(h["kind"], h["refs"], h["severity"]) for h in hits] == [
        ("mcu_pwm_pair", ["U301", "U601"], "warning")]
    assert "INHA/INLA on PA8/PB14" in hits[0]["msg"]
    assert "SLVSFG5D" in hits[0]["msg"]
    assert facts["mcu_pwm_pairs_checked"] == 1


def test_a_low_side_on_another_timer_fires():
    # INLC on PB11 (TIM2_CH4 only): no CHxN at all
    hits, _ = _audit(*_bldc(_BLDC[:5] + ["PB11"]))
    assert [h["kind"] for h in hits] == ["mcu_pwm_pair"]
    assert "PB11 (TIM1_CH3 | TIM2_CH4)" not in hits[0]["msg"]
    assert "INHC/INLC on PA10/PB11" in hits[0]["msg"]


def test_pairs_split_over_two_timers_fire():
    # legs A, B on TIM1, leg C on TIM8 (PB8 TIM8_CH2, PB4 TIM8_CH2N): each
    # pair is complementary but the legs share no counter
    hits, _ = _audit(*_bldc(_BLDC[:4] + ["PB8", "PB4"]))
    assert [h["kind"] for h in hits] == ["mcu_pwm_pair"]


def test_two_legs_on_one_channel_fire():
    # legs A and B both only fit TIM1 CH1/CH1N (PA8+PB13, PA8 is reused)
    hits, _ = _audit(*_bldc(["PA8", "PB13", "PA8", "PC13", "PA10", "PB15"]))
    assert [h["kind"] for h in hits] == ["mcu_pwm_pair"]


def test_swapped_high_and_low_on_one_channel_is_quiet():
    # CHxN on the high side and CHx on the low side: polarity is the
    # timer's to set, dead time still applies
    assert _audit(*_bldc(["PB13", "PA8"] + _BLDC[2:]))[0] == []


def test_a_0r_link_is_followed_and_a_resistor_is_not():
    planted = ["PA8", "PB14", "PA9", "PB13", "PA10", "PB15"]
    hits, _ = _audit(*_bldc(planted, links={"INLA": "0R"}))
    assert [h["kind"] for h in hits] == ["mcu_pwm_pair"]
    # a 100R series resistor is no tie: with INLA and INLB behind one,
    # legs A and B reach no MCU pin and only leg C is judged
    hits, facts = _audit(*_bldc(planted, links={"INLA": "100R",
                                                "INLB": "100R"}))
    assert hits == [] and facts["mcu_pwm_pairs_checked"] == 1


def test_a_tied_off_input_leaves_its_leg_out():
    hits, facts = _audit(*_bldc(["PA8", None, "PA9", "PB14", "PA10",
                                 "PB15"]))
    assert hits == [] and facts["mcu_pwm_pairs_checked"] == 1
    hits, facts = _audit(*_bldc([None] * 6))
    assert hits == [] and facts["mcu_pwm_pairs_checked"] == 0


def test_an_mcu_with_no_row_leaves_the_driver_unchecked():
    hits, facts = _audit(*_bldc(["PA8", "PB14", "PA9", "PB13", "PA10",
                                 "PB15"], mcu="STM32F103C8T6"))
    assert hits == [] and facts["mcu_pwm_pairs_checked"] == 0


@pytest.mark.parametrize("hi,lo,want", [
    (["TIM1_CH1"], ["TIM1_CH1N"], {("TIM1", 1)}),
    (["TIM1_CH1N"], ["TIM1_CH1"], {("TIM1", 1)}),
    (["TIM1_CH1"], ["TIM1_CH2N"], set()),
    (["TIM1_CH1"], ["TIM1_CH1"], set()),            # no complement
    (["TIM2_CH3", "TIM1_CH2"], ["TIM1_CH2N", "TIM15_CH1"], {("TIM1", 2)}),
])
def test_pair_slots(hi, lo, want):
    assert netlist_audit._pair_slots(hi, lo) == want
