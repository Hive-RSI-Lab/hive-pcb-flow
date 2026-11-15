"""Datasheet application circuits (#1158): reference/pin_requirements.yaml
rows read from a part's typical-application figure and pin table, and
reference/pin_states.yaml must-connect pins.

Each rule gets a planted fault and a quiet twin. The AD8232 rows are
seeded from healthmonitor HM-Rev1 (SW tied to IAOUT; REFIN with one 10 M
to the supply) against HM-Rev2 (C28 4.7 uF IAOUT-SW; a 10 M / 10 M / 100 n
divider)."""
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
import part_rows  # noqa: E402


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets, specs=None):
    parsed = {"components": {r: {"value": v, "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    kw = {} if specs is None else {"pin_requirements": specs}
    viol, facts = netlist_audit.audit(parsed, {}, None, **kw)
    return viol, facts


def _parts(viol):
    return sorted((v["severity"], v["refs"][0], v["part"]) for v in viol
                  if v["kind"] == "pin_part_missing")


# ------------------------------------------------------------------ AD8232

def _ad(sw="tied", refin="rail_only", driven=False):
    """An AD8232 U1 on +3V3: SW tied to IAOUT / through C2 / open; REFIN
    with R1 to +VS only / a full divider R1 R2 C1 / driven by U2."""
    comps = {"U1": "AD8232ACPZ-R7", "R1": "10M"}
    nets = {
        "+3V3": [_m("U1", "17", "+VS", "power_in"), _m("U1", "4", "FR", "input"),
                 _m("R1", "1")],
        "GND": [_m("U1", "14", "GND_1", "power_in"),
                _m("U1", "15", "GND_2", "power_in")],
        "/REFIN": [_m("U1", "18", "REFIN", "input"), _m("R1", "2")],
        "/IAOUT": [_m("U1", "9", "IAOUT", "output")],
    }
    if sw == "tied":
        nets["/IAOUT"].append(_m("U1", "6", "SW", "passive"))
    elif sw == "cap":
        comps["C2"] = "4.7u"
        nets["/IAOUT"].append(_m("C2", "1"))
        nets["/SW"] = [_m("U1", "6", "SW", "passive"), _m("C2", "2")]
    else:
        nets["unconnected-(U1-SW-Pad6)"] = [_m("U1", "6", "SW", "passive")]
    if refin == "divider":
        comps.update({"R2": "10M", "C1": "100n"})
        nets["/REFIN"] += [_m("R2", "1"), _m("C1", "1")]
        nets["GND"] += [_m("R2", "2"), _m("C1", "2")]
    if driven:
        comps["U2"] = "REF3012"
        nets["/REFIN"].append(_m("U2", "2", "OUT", "power_out"))
    return comps, nets


def test_ad8232_hm_rev1_sw_tied_and_half_a_divider_is_caught():
    viol, _ = _audit(*_ad())
    assert _parts(viol) == [("error", "U1", "C2"),
                            ("error", "U1", "R_REFIN_BOT"),
                            ("warning", "U1", "C_REFIN")]
    tied = [v for v in viol if v.get("part") == "C2"][0]
    assert tied["tied"] is True and tied["net"] == "/IAOUT"


def test_ad8232_hm_rev2_is_quiet():
    viol, _ = _audit(*_ad(sw="cap", refin="divider"))
    assert _parts(viol) == []


def test_ad8232_one_pole_design_leaves_sw_open():
    # Figure 62: a single-pole high-pass leaves SW unconnected
    viol, _ = _audit(*_ad(sw="open", refin="divider"))
    assert _parts(viol) == []


def test_ad8232_refin_driven_by_a_reference_needs_no_divider():
    # "If there is already a reference voltage available, it can be driven
    # into the REFIN input to eliminate these two 10 MOhm resistors."
    viol, _ = _audit(*_ad(sw="cap", driven=True))
    assert _parts(viol) == []


# ---------------------------------------------------- bootstrap capacitors

@pytest.mark.parametrize("mpn,bst", [("TPS563201DDCR", "VBST"),
                                     ("TPS561201DDCR", "VBST"),
                                     ("AP64350SP-13", "BST")])
@pytest.mark.parametrize("cap,want", [
    (None, [("error", "U1", "CBST")]),     # planted: no bootstrap cap
    ("10n", [("error", "U1", "CBST")]),    # off value, one E3 step out
    ("100n", []),                          # quiet twin
    ("0.1u", [])])
def test_buck_bootstrap_capacitor(mpn, bst, cap, want):
    comps = {"U1": mpn, "L1": "4.7u"}
    nets = {"/SW": [_m("U1", "2", "SW", "power_out"), _m("L1", "1")],
            "/BST": [_m("U1", "6", bst, "input")],
            "GND": [_m("U1", "1", "GND", "power_in")]}
    if cap:
        comps["C1"] = cap
        nets["/SW"].append(_m("C1", "2"))
        nets["/BST"].append(_m("C1", "1"))
    viol, _ = _audit(comps, nets)
    assert _parts(viol) == want


# -------------------------------------------------- LDO output capacitor

@pytest.mark.parametrize("cap,want", [
    (None, [("error", "U1", "COUT")]),
    ("100n", [("error", "U1", "COUT")]),   # under 0.47 uF
    ("1u", []), ("10u", [])])
def test_tps7a20_output_capacitor(cap, want):
    comps = {"U1": "TPS7A2033PDBVR"}
    nets = {"+3V3": [_m("U1", "5", "OUT", "power_out")],
            "GND": [_m("U1", "2", "GND", "power_in")],
            "+5V": [_m("U1", "1", "IN", "power_in"),
                    _m("U1", "3", "EN", "input")]}
    if cap:
        comps["C1"] = cap
        nets["+3V3"].append(_m("C1", "1"))
        nets["GND"].append(_m("C1", "2"))
    viol, _ = _audit(comps, nets)
    assert _parts(viol) == want


# ---------------------------------------------------- must-connect EN pin

@pytest.mark.parametrize("en,fault", [("open", True), ("rail", False)])
def test_tlv62569_en_must_not_float(en, fault):
    comps = {"U1": "TLV62569DBVR"}
    nets = {"+5V": [_m("U1", "4", "VIN", "power_in")],
            "GND": [_m("U1", "2", "GND", "power_in")]}
    if en == "open":
        nets["unconnected-(U1-EN-Pad1)"] = [_m("U1", "1", "EN", "input")]
    else:
        nets["+5V"].append(_m("U1", "1", "EN", "input"))
    viol, _ = _audit(comps, nets, specs=[])
    hits = [v for v in viol if v["kind"] == "pin_state"
            and v["refs"] == ["U1"]]
    assert bool(hits) is fault
    if fault:
        assert hits[0]["pin"] == "EN"


# ------------------------------------------------- unverified: not judged

def test_an_unverified_application_row_gives_not_judged_only():
    specs = copy.deepcopy(netlist_audit.load_pin_requirements())
    for s in specs:
        if s["mpn"] == "AD8232":
            s["verified"] = False
    viol, facts = _audit(*_ad(), specs=specs)
    assert _parts(viol) == []
    nj = [n for n in facts["not_judged"] if n.startswith("pin_part_missing")]
    assert len(nj) == 3 and all("not yet" in n for n in nj)


# ----------------------------------------------------------- the table

def test_application_rows_are_cited_and_verified():
    doc = yaml.safe_load((REPO / "reference" / "pin_requirements.yaml")
                         .read_text())
    for part in doc["parts"]:
        if part["mpn"] == "MCF8316D":
            continue  # predates cites (#1100)
        assert part.get("verified") is True
        for r in part["requires"]:
            assert r["cite"] and all(c["page"] and c["quote"]
                                     for c in r["cite"])
            assert r.get("when") in (None, "connected", "undriven")
            assert r.get("severity", "error") in ("error", "warning")


def test_value_field_is_checked_against_the_quote():
    pages = ["A 0.1-µF ceramic capacitor must be connected between the "
             "VBST to SW pin for proper operation."]
    cite = {"page": 1, "quote": pages[0], "fields": ["value"]}
    assert part_rows.check_cite({"value": "100n"}, cite, pages) == []
    assert part_rows.check_cite({"value": "0.1u"}, cite, pages) == []
    assert part_rows.check_cite({"value": "1u"}, cite, pages)


@pytest.mark.parametrize("text,want", [
    ("Connect 0.1-µF capacitor between VBST and SW pins.",
     ("VBST", "SW", "0.1u")),
    ("A 100nF capacitor is recommended from BST to SW to power the "
     "high-side driver.", ("BST", "SW", "100n")),
    ("A 0.1-µF ceramic capacitor must be connected between the VBST to "
     "SW pin", ("VBST", "SW", "0.1u"))])
def test_drafter_reads_a_pin_to_pin_capacitor_sentence(text, want):
    rows = part_rows._pin_parts([text], "x.pdf", "0" * 64)
    assert [(r["pin"], r["to"], r["value"]) for r in rows] == [want]
    assert rows[0]["cite"][0]["quote"] in text


def test_drafter_ignores_a_capacitor_with_no_pins():
    assert part_rows._pin_parts(
        ["For most applications, 4.7-μF input capacitor is sufficient."],
        "x.pdf", "0" * 64) == []
