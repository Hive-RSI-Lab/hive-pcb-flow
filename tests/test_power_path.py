"""netlist_audit's power-path orientation rules (#1085).

Seeded from datasheets, not corpus boards: TI SLVA139 (a reverse-current
FET's body diode points along the normal current flow, out of the source)
and Microchip AN1149 Figs 5-6 (a P-channel load-sharing FET between the
battery and a USB-fed system rail), and ABLIC S-8261 Rev.5.5_00 Figure 10
(VSS on the cell's -, VM through R2 to the pack's - EB-)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets, kind):
    parsed = {"components": {r: (v if isinstance(v, dict) else
                                 {"value": v, "libsource": ("", "")})
                             for r, v in comps.items()},
              "nets": nets}
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[])
    return [x for x in viol if x["kind"] == kind], facts


def _load_share(drain, source, fet="AO3401A"):
    """AN1149 Fig 5: VBUS -> diode -> VSYS; a P-FET between VBAT and VSYS,
    its gate on VBUS. DRAIN / SOURCE say where the FET's pins land."""
    nets = {
        "VBUS": [_m("J1", "1", "VBUS", "passive"), _m("D1", "2", "A")],
        "VSYS": [_m("D1", "1", "K"), _m("U2", "1", "VIN", "power_in")],
        "VBAT": [_m("BT1", "1", "+")],
        "GND": [_m("BT1", "2", "-"), _m("U2", "2", "GND", "power_in"),
                _m("J1", "5", "GND")],
        "/GATE": [_m("Q1", "1", "G"), _m("R1", "1")],
    }
    nets[drain].append(_m("Q1", "3", "D"))
    nets[source].append(_m("Q1", "2", "S"))
    comps = {"J1": "USB_C", "D1": "SS14", "U2": "AP2112K-3.3",
             "BT1": "LiPo", "R1": "100k",
             "Q1": {"value": fet, "libsource": ("Transistor_FET", "AO3401A"),
                    "libdesc": "-4.0A Id, -30V Vds, P-Channel MOSFET, "
                               "SOT-23"}}
    return comps, nets


def test_load_sharing_pfet_with_its_diode_into_the_battery_warns():
    # source on VBAT, drain on VSYS: the diode (D->S) runs VSYS -> VBAT,
    # so the USB-fed rail charges the cell with the FET off
    hits, facts = _audit(*_load_share("VSYS", "VBAT"), "power_path_body_diode")
    assert [(h["net"], h["from_net"]) for h in hits] == [("VBAT", "VSYS")]
    assert facts["power_path_fets_checked"] == 1


def test_load_sharing_pfet_the_right_way_round_is_quiet():
    # AN1149: drain on the battery, source on the system rail
    hits, facts = _audit(*_load_share("VBAT", "VSYS"), "power_path_body_diode")
    assert hits == [] and facts["power_path_fets_checked"] == 1


def test_high_side_switch_to_a_load_only_rail_is_quiet():
    # a PMOS load switch, source on the battery: the diode points from the
    # load back into the cell, but nothing else feeds the load rail
    comps, nets = _load_share("VSYS", "VBAT")
    nets["VSYS"] = [m for m in nets["VSYS"] if m["ref"] != "D1"]
    nets["VBUS"] = [m for m in nets["VBUS"] if m["ref"] != "D1"]
    del comps["D1"]
    hits, _ = _audit(comps, nets, "power_path_body_diode")
    assert hits == []


def test_back_to_back_pair_is_skipped():
    comps, nets = _load_share("VSYS", "VBAT")
    nets["VBAT"] = [m for m in nets["VBAT"] if m["ref"] != "Q1"]
    nets["Net-(Q1-D)"] = [_m("Q1", "3", "D"), _m("Q2", "3", "D")]
    nets["VSYS"] = [m for m in nets["VSYS"] if m["ref"] != "Q1"] + []
    nets["VBAT"].append(_m("Q2", "2", "S"))
    nets["VSYS"].append(_m("Q1", "2", "S"))
    nets["/GATE"].append(_m("Q2", "1", "G"))
    comps["Q2"] = dict(comps["Q1"])
    hits, facts = _audit(comps, nets, "power_path_body_diode")
    assert hits == []


def test_nmos_channel_from_the_value_and_unknown_channel_skipped():
    comps, nets = _load_share("VBAT", "VSYS")
    comps["Q1"] = {"value": "Q_NMOS_GSD", "libsource": ("", "")}
    # N: diode S -> D, so S on VSYS and D on VBAT points into the battery
    hits, _ = _audit(comps, nets, "power_path_body_diode")
    assert [h["net"] for h in hits] == ["VBAT"]
    comps["Q1"] = {"value": "XYZ123", "libsource": ("", "")}
    hits, facts = _audit(comps, nets, "power_path_body_diode")
    assert hits == [] and facts["power_path_fets_checked"] == 0


def _protector(swapped):
    """S-8261 Figure 10: VDD to the cell's + through R1, VSS on the cell's
    -, DO/CO on the two N-FETs, VM through R2 to EB-. The load's ground is
    EB- (pack -); SWAPPED puts the load's ground on the cell's - instead."""
    load = [_m("U2", "2", "GND", "power_in"), _m("U3", "4", "GND", "power_in")]
    nets = {
        "VBAT": [_m("BT1", "1", "+"), _m("R1", "1"),
                 _m("U2", "1", "VIN", "power_in")],
        "/VDD": [_m("R1", "2"), _m("U1", "5", "VDD", "power_in")],
        "/CELL-": [_m("BT1", "2", "-"), _m("U1", "6", "VSS", "power_in"),
                   _m("Q1", "2", "S")] + (load if swapped else []),
        "/DO": [_m("U1", "1", "DO", "output"), _m("Q1", "1", "G")],
        "/CO": [_m("U1", "3", "CO", "output"), _m("Q2", "1", "G")],
        "/MID": [_m("Q1", "3", "D"), _m("Q2", "3", "D")],
        "/VM": [_m("U1", "2", "VM", "input"), _m("R2", "1")],
        "GND": [_m("R2", "2"), _m("Q2", "2", "S")]
        + ([] if swapped else load),
    }
    comps = {"U1": "S-8261ABJMD", "U2": "AP2112K-3.3", "U3": "ESP32",
             "BT1": "LiPo", "R1": "470", "R2": "2k", "Q1": "FS8205",
             "Q2": "FS8205"}
    return comps, nets


def test_protector_wired_as_figure_10_is_quiet():
    hits, facts = _audit(*_protector(False), "protector_orientation")
    assert hits == [] and facts["battery_protectors_checked"] == 1


def test_protector_with_the_load_ground_on_vss_warns():
    hits, _ = _audit(*_protector(True), "protector_orientation")
    assert [(h["net"], h["pack_net"]) for h in hits] == [("/CELL-", "GND")]
    assert hits[0]["refs"] == ["U1", "U2", "U3"]


def test_parse_netlist_reads_the_symbol_description(tmp_path):
    p = tmp_path / "x.net"
    p.write_text(
        '(export (version "E") (components (comp (ref "Q1") (value "AO3401A")'
        ' (libsource (lib "Transistor_FET") (part "AO3401A")'
        ' (description "-4.0A Id, -30V Vds, P-Channel MOSFET, SOT-23"))))'
        ' (nets))', encoding="utf-8")
    comp = netlist_audit.parse_netlist(p)["components"]["Q1"]
    assert netlist_audit._fet_channel(comp) == "P"
