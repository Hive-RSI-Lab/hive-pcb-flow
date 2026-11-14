"""reference/pin_leakage.yaml and netlist_audit's pull_vs_leakage and
driver_contention rules (#1026).

Each test plants an errata-corpus fault (glasgow revD0) and its fix
(revD1, which must stay quiet)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402

KINDS = ("pull_vs_leakage", "driver_contention")


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets):
    parsed = {"components": {r: {"value": v, "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[])
    return [x for x in viol if x["kind"] in KINDS], facts


BASE = {"U2": "CY7C68013A-56LTX", "U6": "STM32G031G6Ux", "U8": "LFE5U-25F",
        "U76": "74LVC1G08"}


def _rails():
    return {"+3V3": [_m("U2", "1", "VCC", "power_in")],
            "GND": [_m("U2", "2", "GND", "power_in")]}


def _alert(r9):
    """glasgow nALERT: R9 pull-up, FX2 PA0 input, ECP5 PT11B."""
    nets = _rails()
    nets["+3V3"].append(_m("R9", "1"))
    nets["/~{ALERT}"] = [_m("R9", "2"), _m("U2", "33", "PA0/~{INT0}",
                                           "bidirectional"),
                         _m("U8", "B4", "PT11B", "bidirectional")]
    return {**BASE, "R9": r9}, nets


def test_100k_alert_pullup_against_the_ecp5_pulldown_is_an_error():
    hits, facts = _audit(*_alert("100k"))
    assert [(h["kind"], h["net"], h["severity"]) for h in hits] == [
        ("pull_vs_leakage", "/~{ALERT}", "error")]
    h = hits[0]
    assert h["level_v"] == 0.0 and abs(h["sink_a"] - 170e-6) < 1e-9
    assert "FPGA-DS-02012" in h["msg"] and set(h["refs"]) >= {"R9", "U8"}
    assert 7000 < h["max_pull_ohm"] < 8000
    assert facts["pulls_vs_leakage_checked"] == 1


def test_2k2_alert_pullup_of_revd1_is_quiet():
    assert _audit(*_alert("2.2k"))[0] == []


def test_10k_between_the_thresholds_is_a_warning():
    hits, _ = _audit(*_alert("10k"))      # 3.3 V - 10k * 170 uA = 1.6 V
    assert [h["severity"] for h in hits] == ["warning"]
    assert 1.5 < hits[0]["level_v"] < 1.7


def test_a_push_pull_output_on_the_net_sets_it_not_the_pull():
    comps, nets = _alert("100k")
    comps["U9"] = "SN74LVC1G17"
    nets["/~{ALERT}"].append(_m("U9", "4", "Y", "output"))
    assert _audit(comps, nets)[0] == []


def test_a_net_with_no_listed_pin_is_not_judged():
    comps, nets = _alert("100k")
    comps.update({"U2": "SOMETHING_ELSE", "U8": "OTHER_FPGA"})
    hits, facts = _audit(comps, nets)
    assert hits == [] and facts["pulls_vs_leakage_checked"] == 0


def _mcu_rst(rev):
    """glasgow MCU_nRST. revD0: R26 47k up, FX2 PD3, ECP5 PT13B and the
    STM32 NRST all on one net. revD1: the FX2 side (0 ohm R254) pulled down
    by R255 10k into a 74LVC1G08 input; the gate drives NRST."""
    nets = _rails()
    if rev == "D0":
        nets["+3V3"].append(_m("R26", "1"))
        nets["/MCU_~{RST}"] = [
            _m("R26", "2"), _m("C23", "1"),
            _m("U2", "48", "PD3/FD11", "bidirectional"),
            _m("U6", "5", "PF2", "bidirectional"),
            _m("U8", "D5", "PT13B", "bidirectional"),
            _m("J2", "10", "~{RST}", "open_collector")]
        nets["GND"].append(_m("C23", "2"))
        return {**BASE, "R26": "47k", "C23": "100n", "J2": "SWD"}, nets
    nets["GND"].append(_m("R255", "2"))
    nets["Net-(U2-PD3)"] = [_m("U2", "48", "PD3/FD11", "bidirectional"),
                            _m("R254", "1")]
    nets["/MCU_~{RST}"] = [_m("R254", "2"), _m("R255", "1"),
                           _m("U76", "2", "", "input")]
    nets["/ADC/STM32_NRST"] = [_m("U6", "5", "PF2", "bidirectional"),
                               _m("U76", "4", "", "output")]
    return {**BASE, "R254": "0", "R255": "10k"}, nets


def test_revd0_mcu_rst_is_held_low_and_contended():
    hits, facts = _audit(*_mcu_rst("D0"))
    got = {(h["kind"], h["net"], h["severity"]) for h in hits}
    assert got == {("pull_vs_leakage", "/MCU_~{RST}", "error"),
                   ("driver_contention", "/MCU_~{RST}", "warning")}
    pull = next(h for h in hits if h["kind"] == "pull_vs_leakage")
    assert "internal pull-up 55k" in pull["msg"]   # the STM32's own, weakest
    assert "(not yet re-read)" in pull["msg"]
    cont = next(h for h in hits if h["kind"] == "driver_contention")
    assert cont["refs"] == ["U2", "U8"] and cont["reset_pin"] == "U6.5"
    assert facts["reset_contention_found"] == 1


def test_revd1_gated_reset_is_quiet():
    hits, facts = _audit(*_mcu_rst("D1"))
    assert hits == []
    assert facts["pulls_vs_leakage_checked"] == 1   # judged, 0.15 V is low


def test_zero_ohm_groups_join_nets():
    comps, nets = _mcu_rst("D1")
    groups = netlist_audit._zero_ohm_groups(
        nets, {r: {"value": v} for r, v in comps.items()},
        {"R254": {"1": "Net-(U2-PD3)", "2": "/MCU_~{RST}"},
         "R255": {"1": "/MCU_~{RST}", "2": "GND"}})
    assert ["/MCU_~{RST}", "Net-(U2-PD3)"] in groups


def test_every_row_cites_its_datasheet():
    doc = yaml.safe_load(
        (REPO / "reference" / "pin_leakage.yaml").read_text())
    fields = {"pin", "ipd", "ipu", "leak", "rpu", "vil", "vih", "vil_frac",
              "vih_frac", "reset", "source"}
    for part in doc["parts"]:
        assert part["match"] and part["state"], part["mpn"]
        assert isinstance(part["verified"], bool), part["mpn"]
        re.compile(part["match"])
        for row in part["pins"]:
            assert set(row) <= fields, set(row) - fields
            re.compile(row["pin"])
            assert re.search(r"\d", row["source"]), row["source"]
            assert any(k in row for k in ("ipd", "ipu", "leak", "rpu",
                                          "vil", "vil_frac", "reset"))
            for k in ("ipd", "ipu", "leak"):
                assert k not in row or 0 < float(row[k]) < 0.01
            assert ("vil" in row) == ("vih" in row)
