"""reference/pin_states.yaml and netlist_audit's pin_state rule (#1033).

Each row is seeded from an errata-corpus defect; each test plants that
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


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets):
    parsed = {"components": {r: {"value": v, "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[])
    return [x for x in viol if x["kind"] == "pin_state"], facts


def _drv(scs, vref, extra_comps=None, extra_nets=None):
    """A DRV8434S U1 with an MCU U2: SCS on net `scs`, VREF on `vref`."""
    comps = {"U1": "DRV8434SPWPR", "U2": "STM32C051C8T6", "C1": "1u",
             **(extra_comps or {})}
    nets = {"GND": [_m("U1", "13", "GND", "power_in"), _m("C1", "2"),
                    _m("U2", "8", "VSS", "power_in")],
            "+3.3V": [_m("U2", "7", "VDD", "power_in")],
            "/DVDD": [_m("U1", "15", "DVDD", "power_out"), _m("C1", "1")],
            "/SCS": [_m("U2", "15", "PA4", "bidirectional")]}
    for name, ms in (extra_nets or {}).items():
        nets.setdefault(name, []).extend(ms)
    nets.setdefault(scs, []).append(_m("U1", "18", "~{SCS}", "input"))
    nets.setdefault(vref, []).append(_m("U1", "17", "VREF", "input"))
    return comps, nets


def test_drv8434s_scs_on_gnd_is_the_planted_fault():
    hits, facts = _audit(*_drv("GND", "+3.3V"))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("SCS", "GND", "error")]
    assert "SLOSE70" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


def test_drv8434s_scs_driven_by_the_mcu_is_quiet():
    assert _audit(*_drv("/SCS", "+3.3V"))[0] == []


def test_drv8434s_scs_on_its_pullup_alone_fires():
    hits, _ = _audit(*_drv("unconnected-(U1-~{SCS}-Pad18)", "+3.3V"))
    assert [h["pin"] for h in hits] == ["SCS"]
    assert hits[0]["states"] == ["open"]


def test_drv8434s_vref_on_its_own_dvdd_is_the_planted_fault():
    hits, _ = _audit(*_drv("/SCS", "/DVDD"))
    assert [(h["pin"], h["volts"]) for h in hits] == [("VREF", 5.0)]
    assert "own_supply" in hits[0]["states"]


def test_drv8434s_vref_on_a_5v_rail_is_over_its_limit():
    hits, _ = _audit(*_drv("/SCS", "+5V",
                           extra_nets={"+5V": [_m("U2", "1", "VDD5",
                                                  "power_in")]}))
    assert [h["pin"] for h in hits] == ["VREF"]
    assert "above its 3.3 V limit" in hits[0]["msg"]


def test_drv8434s_vref_from_a_dvdd_divider_is_quiet():
    comps, nets = _drv("/SCS", "/VREF", {"R1": "10k", "R2": "22k"},
                       {"/DVDD": [_m("R1", "1")],
                        "/VREF": [_m("R1", "2"), _m("R2", "1")],
                        "GND": [_m("R2", "2")]})
    assert _audit(comps, nets)[0] == []


@pytest.mark.parametrize("value", ["PCA6416APW", "TCA6416APW"])
def test_io_expander_reset_floating_is_the_planted_fault(value):
    comps = {"U24": value}
    nets = {"+3V3": [_m("U24", "24", "VCCP", "power_in")],
            "GND": [_m("U24", "12", "GND", "power_in")],
            "unconnected-(U24-~{RESET}-Pad3)": [
                _m("U24", "3", "~{RESET}", "input+no_connect")]}
    hits, _ = _audit(comps, nets)
    assert [(h["refs"], h["pin"], h["severity"]) for h in hits] == [
        (["U24"], "RESET", "error")]


@pytest.mark.parametrize("pull", [False, True])
def test_io_expander_reset_tied_or_pulled_up_is_quiet(pull):
    comps = {"U24": "TCA6416APW"}
    nets = {"+3V3": [_m("U24", "24", "VCCP", "power_in")],
            "GND": [_m("U24", "12", "GND", "power_in")]}
    if pull:
        comps["R1"] = "10k"
        nets["+3V3"].append(_m("R1", "1"))
        nets["/RESET"] = [_m("R1", "2"), _m("U24", "3", "~{RESET}", "input")]
    else:
        nets["+3V3"].append(_m("U24", "3", "~{RESET}", "input"))
    assert _audit(comps, nets)[0] == []



def _expanders(addr_net, vccp="VDD_P"):
    """glasgow's two 16-bit expanders: I2C side on +3V3, P-ports on +5V5,
    U24 ADDR on GND, U25 ADDR on `addr_net`."""
    comps = {"U24": "PCA6416APW", "U25": "PCA6416APW"}
    nets = {"+3V3": [], "+5V5": [], "GND": []}
    for u in ("U24", "U25"):
        nets["+3V3"] += [_m(u, "2", "VDD_I2C", "power_in"),
                         _m(u, "3", "~{RESET}", "input")]
        nets["+5V5"].append(_m(u, "24", vccp, "power_in"))
        nets["GND"].append(_m(u, "12", "VSS", "power_in"))
    nets["GND"].append(_m("U24", "21", "ADDR", "input"))
    nets.setdefault(addr_net, []).append(_m("U25", "21", "ADDR", "input"))
    return comps, nets


@pytest.mark.parametrize("vccp", ["VDD_P", "VCCP"])
def test_io_expander_addr_on_the_lower_rail_is_the_planted_fault(vccp):
    hits, _ = _audit(*_expanders("+3V3", vccp))
    assert [(h["refs"], h["pin"], h["net"]) for h in hits] == [
        (["U25"], "ADDR", "+3V3")]
    assert "not VDD_P's '+5V5'" in hits[0]["msg"]


@pytest.mark.parametrize("net", ["+5V5", "GND"])
def test_io_expander_addr_on_vccp_or_ground_is_quiet(net):
    assert _audit(*_expanders(net))[0] == []

def _adc(far, value):
    """NAFE71388 U7 with AICOM through R34 (`value`) to net `far`."""
    comps = {"U7": "NAFE71388", "R34": value, "C51": "1n", "J3": "Conn"}
    nets = {"GND": [_m("U7", "1", "AGND", "power_in"), _m("C51", "2")],
            "Net-(U7-AICOM)": [_m("U7", "8", "AICOM", "input"),
                               _m("R34", "1"), _m("C51", "1")]}
    nets.setdefault(far, []).append(_m("R34", "2"))
    if far != "GND":
        nets[far].append(_m("J3", "1"))
    return comps, nets


def test_adc_aicom_on_a_remote_common_is_the_planted_fault():
    hits, _ = _audit(*_adc("/ADC/REMOTE_COM", "2.49k"))
    assert [(h["pin"], h["severity"]) for h in hits] == [("AICOM", "warning")]
    assert "R34 to /ADC/REMOTE_COM" in hits[0]["msg"]


@pytest.mark.parametrize("value", ["2.49k", "0"])
def test_adc_aicom_through_its_resistor_or_0r_to_gnd_is_quiet(value):
    assert _audit(*_adc("GND", value))[0] == []


def _phy(id_net, via_0r=False):
    comps = {"U8": "USB3320C-EZK"}
    nets = {"3V3": [_m("U8", "20", "VDD33", "power_in")],
            "GND": [_m("U8", "33", "GND", "power_in")]}
    if via_0r:
        comps["R13"] = "0"
        nets[id_net].append(_m("R13", "1"))
        nets["/ID"] = [_m("R13", "2"), _m("U8", "23", "ID", "input")]
    else:
        nets[id_net].append(_m("U8", "23", "ID", "input"))
    return comps, nets


@pytest.mark.parametrize("via_0r", [False, True])
def test_usb3320_id_grounded_is_the_planted_fault(via_0r):
    hits, _ = _audit(*_phy("GND", via_0r))
    assert [(h["pin"], h["severity"]) for h in hits] == [("ID", "warning")]


def test_usb3320_id_on_vdd33_is_quiet():
    assert _audit(*_phy("3V3"))[0] == []


def test_a_part_with_no_row_is_not_judged():
    comps = {"U1": "DRV8434XYZ_NOT"}
    nets = {"GND": [_m("U1", "18", "~{SCS}", "input")]}
    assert _audit(comps, nets)[0] == []


def test_every_row_cites_its_datasheet():
    doc = yaml.safe_load((REPO / "reference" / "pin_states.yaml").read_text())
    states = {"open", "ground", "rail", "own_supply", "pulled_up",
              "pulled_down", "driven", "external"}
    for part in doc["parts"]:
        assert part["verified"] is True and part["match"], part["mpn"]
        for rule in part["pins"]:
            assert rule["source"].strip() and rule["why"].strip()
            assert set(rule.get("allow", [])) | set(
                rule.get("forbid", [])) <= states
            assert rule.get("allow") or rule.get("forbid") or "max_v" in rule


# Rows for the ICs of boards no datasheet rule judged before (#1104): each
# test plants a fault on the board's real wiring, and the wiring as built
# stays quiet.

def _tpa(sdz="/SDZ", plimit="/GVDD", gain="/GAIN", value="TPA3118D2DAPR"):
    """PCB-0017's TPA3118D2 U1: SDZ (with FAULTZ) on R2 100k to +12V,
    PLIMIT on its own GVDD, GAIN/SLV on R1 5.6k to GND (master, 20 dB)."""
    comps = {"U1": value, "R1": "5.6k 1%", "R2": "100k 1%",
             "R3": "100k 1%", "C5": "1uF 25V X7R"}
    nets = {"GND": [_m("U1", "9", "GND", "power_in"), _m("R1", "2"),
                    _m("R3", "2"), _m("C5", "2")],
            "+12V": [_m("U1", "17", "AVCC", "power_in"), _m("R2", "2")],
            "/GVDD": [_m("U1", "7", "GVDD", "power_out"), _m("C5", "1")],
            "/SDZ": [_m("R2", "1"), _m("U1", "3", "FAULTZ")],
            "/GAIN": [_m("R1", "1")],
            "/MUTE": [_m("R3", "1"), _m("U1", "12", "MUTE")]}
    nets.setdefault(sdz, []).append(_m("U1", "2", "SDZ"))
    nets.setdefault(plimit, []).append(_m("U1", "6", "PLIMIT"))
    nets.setdefault(gain, []).append(_m("U1", "8", "GAIN/SLV"))
    return comps, nets


@pytest.mark.parametrize("value", ["TPA3118D2DAPR", "TPA3116D2DADR",
                                   "TPA3130D2DAPR"])
def test_tpa3118_sdz_on_gnd_is_the_planted_fault(value):
    hits, facts = _audit(*_tpa(sdz="GND", value=value))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("SDZ", "GND", "warning")]
    assert "SLOS708G" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


def test_tpa3118_plimit_unconnected_is_the_planted_fault():
    net = "unconnected-(U1-PLIMIT-Pad6)"
    hits, facts = _audit(*_tpa(plimit=net))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("PLIMIT", net, "warning")]
    assert "SLOS708G" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


@pytest.mark.parametrize("net", ["GND", "/GVDD"])
def test_tpa3118_gain_slv_with_no_r1_to_gnd_is_the_planted_fault(net):
    hits, facts = _audit(*_tpa(gain=net))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("GAIN/SLV", net, "warning")]
    assert "SLOS708G" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


def test_tpa3118_as_pcb0017_wires_it_is_quiet():
    hits, facts = _audit(*_tpa())
    assert hits == [] and facts["pin_state_parts_checked"] == 1


def _ap64350(rt="/RT", func="RT/CLK"):
    """PCB-0008-B's AP64350SP-13 U1: RT/CLK on R4 200k to GND (500 kHz),
    EN on the R2/R3 divider from +VIN."""
    comps = {"U1": "AP64350SP-13", "R2": "105k 1%", "R3": "24.0k 1%",
             "R4": "200k 1%", "C1": "100nF 50V X7R"}
    nets = {"GND": [_m("U1", "7", "GND", "power_in"),
                    _m("U1", "9", "EP", "power_in"), _m("R3", "2"),
                    _m("R4", "2")],
            "+VIN": [_m("U1", "2", "VIN", "power_in"), _m("R2", "1")],
            "/EN": [_m("U1", "3", "EN"), _m("R2", "2"), _m("R3", "1")],
            "/SW": [_m("U1", "8", "SW", "power_out"), _m("C1", "2")],
            "/BST": [_m("U1", "1", "BST"), _m("C1", "1")],
            "/RT": [_m("R4", "1")]}
    nets.setdefault(rt, []).append(_m("U1", "4", func))
    return comps, nets


@pytest.mark.parametrize("func", ["RT/CLK", "RT"])
def test_ap64350_rt_clk_unconnected_is_the_planted_fault(func):
    net = "unconnected-(U1-RT/CLK-Pad4)"
    hits, facts = _audit(*_ap64350(rt=net, func=func))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("RT/CLK", net, "error")]
    assert "DS41976" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


def test_ap64350_rt_clk_on_gnd_is_the_planted_fault():
    hits, facts = _audit(*_ap64350(rt="GND"))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("RT/CLK", "GND", "error")]
    assert "DS41976" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


def test_ap64350_as_pcb0008b_wires_it_is_quiet():
    hits, facts = _audit(*_ap64350())
    assert hits == [] and facts["pin_state_parts_checked"] == 1


def _ch224k(vdd="/VDD", vbus="/VSENSE", cfg1="/CFG1", extra_comps=None,
            extra_nets=None):
    """PCB-0003's CH224K U1: VDD fed from /VIND by R2A+R2B 510R with C5
    1uF, VBUS sensing /VIND through R1 10k, CFG1 on R3 100k to VDD.
    VDD, VBUS and CFG1 go on the nets named; the rest stays as built."""
    comps = {"U1": "CH224K", "R1": "10k", "R2A": "510R", "R2B": "510R",
             "R3": "100k", "C5": "1uF 50V X5R", "C1A": "10uF 50V X5R",
             "F1": "Polyfuse", "SW1": "SW_DIP_x03", **(extra_comps or {})}
    nets = {"GND": [_m("U1", "11", "GND", "power_in"), _m("C5", "2"),
                    _m("C1A", "2"), _m("J1", "A1", "GND")],
            "VBUS": [_m("J1", "A4", "VBUS"), _m("C1A", "1"), _m("F1", "1")],
            "/VIND": [_m("F1", "2"), _m("R1", "1"), _m("R2A", "1")],
            "/R2_MID": [_m("R2A", "2"), _m("R2B", "1")],
            "/VDD": [_m("R2B", "2"), _m("C5", "1"), _m("R3", "1")],
            "/VSENSE": [_m("R1", "2")],
            "/CFG1": [_m("R3", "2"), _m("SW1", "1")],
            "/CC1": [_m("J1", "A5", "CC1"), _m("U1", "7", "CC1")],
            "/CC2": [_m("J1", "B5", "CC2"), _m("U1", "6", "CC2")]}
    for name, ms in (extra_nets or {}).items():
        nets.setdefault(name, []).extend(ms)
    nets.setdefault(vdd, []).append(_m("U1", "1", "VDD", "power_in"))
    nets.setdefault(vbus, []).append(_m("U1", "8", "VBUS"))
    nets.setdefault(cfg1, []).append(_m("U1", "9", "CFG1"))
    return comps, nets


@pytest.mark.parametrize("net", ["VBUS", "GND",
                                 "unconnected-(U1-VBUS-Pad8)"])
def test_ch224k_vbus_without_its_series_resistor_is_the_planted_fault(net):
    # "VBUS": straight on the bus, which a regulator's VIN makes a rail
    hits, facts = _audit(*_ch224k(
        vbus=net, extra_comps={"U2": "AP2112K-3.3"},
        extra_nets={"VBUS": [_m("U2", "1", "VIN", "power_in")]}))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("VBUS", net, "error")]
    assert "CH224 datasheet V2.1" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


def test_ch224k_vdd_on_a_5v_rail_is_the_planted_fault():
    hits, facts = _audit(*_ch224k(
        vdd="+5V", extra_comps={"U2": "AP2112K-5.0"},
        extra_nets={"+5V": [_m("U2", "5", "VOUT", "power_out")]}))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("VDD", "+5V", "error")]
    assert "above its 3.6 V limit" in hits[0]["msg"]
    assert "CH224 datasheet V2.1" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


def test_ch224k_cfg1_on_a_12v_rail_is_the_planted_fault():
    hits, facts = _audit(*_ch224k(
        cfg1="+12V", extra_comps={"U2": "LM7812"},
        extra_nets={"+12V": [_m("U2", "3", "VO", "power_out")]}))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("CFG1", "+12V", "error")]
    assert "above its 8 V limit" in hits[0]["msg"]
    assert "CH224 datasheet V2.1" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


def test_ch224k_as_pcb0003_wires_it_is_quiet():
    hits, facts = _audit(*_ch224k())
    assert hits == [] and facts["pin_state_parts_checked"] == 1
    # and on 3.3 V rails by name, under both limits
    hits, facts = _audit(*_ch224k(
        vdd="+3V3", cfg1="+3V3", extra_comps={"U2": "AP2112K-3.3"},
        extra_nets={"+3V3": [_m("U2", "5", "VOUT", "power_out")]}))
    assert hits == [] and facts["pin_state_parts_checked"] == 1


def _ads8326(cs="/CS", nin="/AGND_SENSE", ref="VREF", cs_func="~{CS}/SHDN",
             extra_comps=None, extra_nets=None):
    """PCB-0014's ADS8326 U1: REF on the ADR4520 U2's VREF, -IN to GND
    through R8 0 ohm (R5 200K from the attenuator), CS/SHDN from J2.
    REF, -IN and CS/SHDN go on the nets named; the rest stays as built."""
    comps = {"U1": "ADS8326IB", "U2": "ADR4520B", "R4": "200K 0.02% 10ppm/C",
             "R5": "200K 0.02% 10ppm/C", "R6": "49.9 ohm 1%",
             "R7": "10 ohm 1%", "R8": "0 ohm 1%", "C2": "100nF 16V X7R",
             "C3": "47uF 16V X7R", "C5": "2.2uF 10V X7R",
             "C7": "1nF 50V C0G/NP0", "J2": "1x6P 2.54mm male header",
             **(extra_comps or {})}
    nets = {"GND": [_m("U1", "4", "GND", "power_in"),
                    _m("U2", "4", "GND", "power_in"), _m("R8", "2"),
                    _m("C2", "2"), _m("C3", "2"), _m("C5", "2"),
                    _m("C7", "2")],
            "+3V3": [_m("U2", "2", "VIN", "power_in"), _m("R7", "1"),
                     _m("J2", "1")],
            "VDD_ADC": [_m("R7", "2"), _m("C2", "1"),
                        _m("U1", "8", "VDD", "power_in")],
            "VREF": [_m("U2", "6", "VOUT", "power_out"), _m("C3", "1"),
                     _m("C5", "1")],
            "/ATT_C": [_m("R4", "2"), _m("R5", "1")],
            "/AGND_SENSE": [_m("R5", "2"), _m("R8", "1")],
            "/AIN_ADC": [_m("R6", "2"), _m("C7", "1"),
                         _m("U1", "2", "+IN")],
            "/CS": [_m("J2", "3")],
            "/SCLK": [_m("J2", "4"), _m("U1", "7", "DCLOCK")],
            "/DOUT": [_m("J2", "5"), _m("U1", "6", "DOUT")]}
    for name, ms in (extra_nets or {}).items():
        nets.setdefault(name, []).extend(ms)
    nets.setdefault(ref, []).append(_m("U1", "1", "REF"))
    nets.setdefault(nin, []).append(_m("U1", "3", "-IN"))
    nets.setdefault(cs, []).append(_m("U1", "5", cs_func))
    return comps, nets


@pytest.mark.parametrize("cs_func", ["~{CS}/SHDN", "~{CS}"])
@pytest.mark.parametrize("net", ["GND", "+3V3",
                                 "unconnected-(U1-~{CS}/SHDN-Pad5)"])
def test_ads8326_cs_shdn_not_driven_is_the_planted_fault(net, cs_func):
    hits, facts = _audit(*_ads8326(cs=net, cs_func=cs_func))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("CS/SHDN", net, "error")]
    assert "SBAS343C" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


def test_ads8326_minus_in_on_a_3v3_rail_is_the_planted_fault():
    hits, facts = _audit(*_ads8326(nin="+3V3"))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("-IN", "+3V3", "error")]
    assert "above its 0.5 V limit" in hits[0]["msg"]
    assert "SBAS343C" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


@pytest.mark.parametrize("net", ["GND", "unconnected-(U1-REF-Pad1)"])
def test_ads8326_ref_grounded_or_open_is_the_planted_fault(net):
    hits, facts = _audit(*_ads8326(ref=net))
    assert [(h["pin"], h["net"], h["severity"]) for h in hits] == [
        ("REF", net, "error")]
    assert "SBAS343C" in hits[0]["msg"]
    assert facts["pin_state_parts_checked"] == 1


def test_ads8326_as_pcb0014_wires_it_is_quiet():
    hits, facts = _audit(*_ads8326())
    assert hits == [] and facts["pin_state_parts_checked"] == 1
