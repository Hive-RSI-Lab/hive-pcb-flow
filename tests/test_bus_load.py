"""reference/bus_load.yaml and netlist_audit's bus_cap_load rule (#1031).
Seeded from echoplayer-3: r1-rev1 put a CESD3V3AP clamp (120 pF) on every
SDMMC line, which plants the fault, and r1-rev1.1 (the fix) has none, which
must stay quiet."""
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


def _audit(comps, nets, dnp=()):
    parsed = {"components": {r: {"value": v, "libsource": ("", "")}
                             for r, v in comps.items()},
              "nets": nets}
    for r in dnp:
        parsed["components"][r]["dnp"] = True
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[], i2c_addresses=[])
    return [x for x in viol if x["kind"] == "bus_cap_load"], facts


_SD = (("clk", "CLK", "5"), ("cmd", "CMD", "3"), ("d0", "DAT0", "7"),
       ("d1", "DAT1", "8"), ("d2", "DAT2", "1"), ("d3", "DAT3/CD", "2"))


def _echoplayer(clamps=True):
    """echoplayer's card: J3 (Micro_SD_Card_Det1) wired to U6 (STM32H743)
    over /cpu/sdmmc.*, with r1-rev1's CESD3V3AP pairs D1-D3 on the lines
    (D2 clk/cmd, D3 d1/d0, D1 d3/d2) when `clamps`."""
    comps = {"J3": "Micro_SD_Card_Det1", "U6": "STM32H743VIT6"}
    nets: dict[str, list] = {"+3V3": [_m("J3", "4", "VDD", "power_in")],
                             "GND": [_m("J3", "6", "VSS", "power_in")]}
    for i, (line, func, pin) in enumerate(_SD):
        nets[f"/cpu/sdmmc.{line}"] = [
            _m("J3", pin, func, "bidirectional"),
            _m("U6", str(80 + i), f"PC{8 + i}", "bidirectional")]
    if clamps:
        for ref, pair in (("D2", ("clk", "cmd")), ("D3", ("d1", "d0")),
                          ("D1", ("d3", "d2"))):
            comps[ref] = "CESD3V3AP-ES"
            for k, line in enumerate(pair):
                nets[f"/cpu/sdmmc.{line}"].append(_m(ref, str(k + 1)))
            nets["GND"].append(_m(ref, "3"))
    return comps, nets


def test_echoplayer_rev1_clamps_on_sdmmc_are_the_planted_fault():
    hits, facts = _audit(*_echoplayer())
    assert sorted((h["net"], tuple(h["refs"]), h["severity"]) for h in hits) == [
        ("/cpu/sdmmc.clk", ("D2",), "error"),
        ("/cpu/sdmmc.cmd", ("D2",), "error"),
        ("/cpu/sdmmc.d0", ("D3",), "error"),
        ("/cpu/sdmmc.d1", ("D3",), "error"),
        ("/cpu/sdmmc.d2", ("D1",), "error"),
        ("/cpu/sdmmc.d3", ("D1",), "error")]
    h = hits[0]
    assert h["load_pf"] == 120 and h["budget_pf"] == 30 and h["bus"] == "sd"
    assert "AN10911" in h["msg"] and "CESD3V3AP" in h["msg"]
    assert "at least" in h["msg"]
    assert facts["bus_lines_checked"] == 6


def test_echoplayer_rev1_1_with_no_clamps_is_quiet():
    hits, facts = _audit(*_echoplayer(clamps=False))
    assert hits == [] and facts["bus_lines_checked"] == 6


def test_a_dnp_clamp_is_not_fitted():
    # the errata's own fix: D1-D4 left unpopulated
    assert _audit(*_echoplayer(), dnp=("D1", "D2", "D3"))[0] == []


def test_a_capacitor_to_ground_counts_and_one_in_series_does_not():
    comps, nets = _echoplayer(clamps=False)
    comps.update({"C9": "33pF", "C10": "100nF"})
    nets["/cpu/sdmmc.clk"].append(_m("C9", "1"))
    nets["GND"].append(_m("C9", "2"))
    nets["/cpu/sdmmc.cmd"].append(_m("C10", "1"))
    nets["/x"] = [_m("C10", "2")]
    hits, _ = _audit(comps, nets)
    assert [(h["net"], h["refs"], h["load_pf"]) for h in hits] == [
        ("/cpu/sdmmc.clk", ["C9"], 33.0)]


def test_the_mcu_is_not_taken_for_a_card_socket():
    # only J3 has CLK, CMD and DAT0: the MCU's own pins aren't the card
    comps, nets = _echoplayer()
    hits, _ = _audit(comps, nets)
    assert all("U6" not in h["refs"] and "J3" not in h["refs"] for h in hits)


def _i2c(cap):
    comps = {"U1": "MCU", "U2": "SENSOR", "C1": cap}
    nets = {"/SDA": [_m("U1", "1", "PB7/SDA", "bidirectional"),
                     _m("U2", "1", "SDA", "bidirectional"), _m("C1", "1")],
            "/SCL": [_m("U1", "2", "PB6", "bidirectional"),
                     _m("U2", "2", "SCL", "input")],
            "+3V3": [_m("C1", "2"), _m("U2", "3", "VDD", "power_in")]}
    return comps, nets


@pytest.mark.parametrize("cap,sev", [("330pF", None), ("470pF", "warning"),
                                     ("680pF", "error")])
def test_i2c_line_against_400_and_550_pf(cap, sev):
    hits, facts = _audit(*_i2c(cap))
    assert facts["bus_lines_checked"] == 2
    assert [h["severity"] for h in hits] == ([sev] if sev else [])
    if sev:
        assert hits[0]["net"] == "/SDA" and "UM10204" in hits[0]["msg"]


def test_parse_netlist_reads_dnp(tmp_path):
    net = tmp_path / "x.net"
    net.write_text('''(export (version "E")
  (components
    (comp (ref "D1") (value "CESD3V3AP") (footprint "x")
      (property (name "dnp")))
    (comp (ref "D2") (value "CESD3V3AP") (footprint "x")))
  (nets))''')
    comps = netlist_audit.parse_netlist(net)["components"]
    assert comps["D1"].get("dnp") is True and "dnp" not in comps["D2"]


def test_every_row_cites_its_source():
    doc = yaml.safe_load((REPO / "reference" / "bus_load.yaml").read_text())
    for name, b in doc["buses"].items():
        assert b["verified"] is True and b["source"].strip(), name
        assert b["budget_pf"] > 0 and b.get("max_pf", b["budget_pf"]) >= b["budget_pf"]
    for row in doc["parts"]:
        assert row["verified"] is True and row["source"].strip(), row["match"]
        assert row["c_pf"] > 0 and row["match"]
