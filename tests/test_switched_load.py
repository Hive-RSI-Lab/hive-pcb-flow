"""A switched capacitive load against its converter's limit (#1093; class
11 of docs/miss-taxonomy.md, netlist_audit rule 32's switched_load_droop,
the converters rows of reference/source_limits.yaml).

Seeded from pdlc v1: an LM5158 boost (FB divider 120k / 2k, so 61 V) on
VPDLC with about 105 uF, and two DMN10H120SE half-bridges switching the
panel between /OUTA and /OUTB with nothing to limit the edge current. The
panel's capacitance is on no schematic, so the constraints give it
(`switched_loads`). v2 drives the panel from two bucks through 68 uH, the
quiet twin."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets, constraints, table=None):
    parsed = {"components": {r: {"value": v, "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    kw = {} if table is None else {"source_limits": table}
    return netlist_audit.audit(parsed, constraints, None, **kw)


def _boost(conv="LM5158", cout=("100u", "4.7u", "100n", "100n", "100n")):
    """U1 boosting +12V to VPDLC (R2 120k / R3 2k on FB) with COUT."""
    comps = {"U1": conv, "R2": "120K", "R3": "2K", "D3": "B2100"}
    nets = {"+12V": [_m("U1", "3", "BIAS", "power_in")],
            "GND": [_m("U1", "1", "PGND", "power_in"), _m("R3", "2")],
            "/boost/SW": [_m("U1", "14", "SW", "power_out"),
                          _m("D3", "2", "A")],
            "/boost/FB": [_m("U1", "9", "FB", "input"), _m("R2", "2"),
                          _m("R3", "1")],
            "VPDLC": [_m("D3", "1", "K"), _m("R2", "1")]}
    for i, val in enumerate(cout):
        ref = f"C{50 + i}"
        comps[ref] = val
        nets["VPDLC"].append(_m(ref, "1"))
        nets["GND"].append(_m(ref, "2"))
    return comps, nets


def _leg(comps, nets, hi, lo, out, series=None):
    """A half-bridge: HI drain on VPDLC, LO source on GND, drawn as pdlc
    v1 draws it (the low FET's drain pins split over the switch node and
    OUT). SERIES puts a resistor between the switch node and OUT."""
    comps[hi] = comps[lo] = "DMN10H120SE"
    sw = f"Net-({hi}-S)"
    nets["VPDLC"].append(_m(hi, "4", "D"))
    nets[f"unconnected-({hi}-D-Pad2)"] = [_m(hi, "2", "D",
                                             "passive+no_connect")]
    nets[sw] = [_m(hi, "3", "S"), _m(lo, "4", "D")]
    nets["GND"].append(_m(lo, "3", "S"))
    jpin = "6" if out == "OUTA" else "5"
    if series:
        ref, val = series
        comps[ref] = val
        nets[sw] += [_m(lo, "2", "D"), _m(ref, "1")]
        nets[f"/{out}"] = [_m(ref, "2"), _m("J1", jpin)]
    else:
        nets[f"/{out}"] = [_m(lo, "2", "D"), _m("J1", jpin)]


def _pdlc_v1(series=False, **kw):
    comps, nets = _boost(**kw)
    comps["J1"] = "TBP01R1-508-06BE"
    _leg(comps, nets, "Q2", "Q3", "OUTA",
         ("R60", "100") if series else None)
    _leg(comps, nets, "Q4", "Q5", "OUTB",
         ("R61", "100") if series else None)
    return comps, nets


def _cons(c_f, nets=("/OUTA", "/OUTB")):
    return {"switched_loads": [{"nets": list(nets), "capacitance_f": c_f}]}


def _hits(viol):
    return [v for v in viol if v["kind"] == "switched_load_droop"]


def _nj(facts):
    return [n for n in facts.get("not_judged", [])
            if n.startswith("switched_load_droop")]


def test_pdlc_v1_panel_on_the_bare_bridge_is_caught():
    # 300 mA of 100 Hz square wave at 61 V (the v1 README's spec) is
    # 4 C V f, so C = 0.3 / (4 * 61 * 100) = 12.3 uF
    viol, facts = _audit(*_pdlc_v1(), _cons(12.3e-6))
    h = _hits(viol)
    assert len(h) == 1, viol
    h = h[0]
    assert h["severity"] == "error" and h["net"] == "VPDLC"
    assert sorted(h["refs"]) == ["Q2", "Q4", "U1"]
    assert h["rail_v"] == 61.0 and abs(h["droop_pct"] - 21.0) < 0.5
    assert "3.26 A" in h["msg"] and "hiccup" in h["msg"]
    assert facts["switched_loads_checked"] == 1


def test_a_small_load_on_the_same_bridge_is_quiet():
    viol, facts = _audit(*_pdlc_v1(), _cons(1e-6))
    assert _hits(viol) == [] and _nj(facts) == []
    assert facts["switched_loads_checked"] == 1


def test_more_output_capacitance_is_quiet():
    viol, _ = _audit(*_pdlc_v1(cout=("470u",)), _cons(12.3e-6))
    assert _hits(viol) == []


def test_a_series_resistor_under_the_current_limit_is_quiet():
    # 122 V / 200 ohm = 0.61 A, under the LM5158's 3.26 A
    viol, facts = _audit(*_pdlc_v1(series=True), _cons(12.3e-6))
    assert _hits(viol) == [] and _nj(facts) == []


def test_one_leg_only_swings_v_not_2v():
    # 12.3 uF single-ended: 12.3 / 117.3 = 10.5%, just past 10%
    viol, _ = _audit(*_pdlc_v1(), _cons(12.3e-6, nets=("/OUTA",)))
    assert len(_hits(viol)) == 1
    viol, _ = _audit(*_pdlc_v1(), _cons(10e-6, nets=("/OUTA",)))
    assert _hits(viol) == []


def test_the_lm51581_has_half_the_limit():
    viol, _ = _audit(*_pdlc_v1(conv="LM51581"), _cons(12.3e-6))
    assert "1.63 A" in _hits(viol)[0]["msg"]


def test_a_load_driven_through_an_inductor_is_not_judged():
    # pdlc v2: the panel hangs on a buck's output behind 68 uH
    comps, nets = _boost()
    comps.update({"L2": "68u", "C35": "10u", "J2": "TBP04R12"})
    nets["/PDLC_OUT_A"] = [_m("L2", "2"), _m("C35", "1"), _m("J2", "3")]
    nets["/buck/SW"] = [_m("L2", "1")]
    nets["GND"].append(_m("C35", "2"))
    viol, facts = _audit(comps, nets, _cons(12.3e-6, nets=("/PDLC_OUT_A",)))
    assert _hits(viol) == []
    nj = _nj(facts)
    assert len(nj) == 1 and "L2" in nj[0]


def test_a_rail_no_converter_row_covers_is_not_judged():
    viol, facts = _audit(*_pdlc_v1(conv="XYZ123"), _cons(12.3e-6))
    assert _hits(viol) == []
    assert "no converters row" in _nj(facts)[0]


def test_a_load_net_not_in_the_netlist_is_a_missing_net():
    viol, _ = _audit(*_pdlc_v1(), _cons(12.3e-6, nets=("/PANEL",)))
    assert [v["net"] for v in viol if v["kind"] == "missing_net"] \
        == ["/PANEL"]


def test_no_constraint_no_finding():
    viol, facts = _audit(*_pdlc_v1(), {})
    assert _hits(viol) == [] and _nj(facts) == []


def test_an_unverified_converter_row_gives_not_judged_only():
    table = copy.deepcopy(netlist_audit.load_source_limits())
    for r in table["converters"]:
        if r["mpn"] == "LM5158":
            r["verified"] = False
    viol, facts = _audit(*_pdlc_v1(), _cons(12.3e-6), table=table)
    assert _hits(viol) == []
    nj = _nj(facts)
    assert len(nj) == 1 and "not yet" in nj[0]


@pytest.mark.parametrize("need", ["vref", "uv_pct", "ilim_min",
                                  "hiccup_cycles"])
def test_converter_rows_cite_every_number(need):
    for row in netlist_audit.load_source_limits()["converters"]:
        assert row["verified"] is True
        assert need in {f for c in row["cite"] for f in c["fields"]}, \
            (row["mpn"], need)
