"""reference/io_levels.yaml and netlist_audit's io_overvoltage rule (#1029).

The planted fault is the errata corpus's megabuzz revA (the YMF262 OPL3 on
+5V into Spartan-6 IO_ pins through 27 ohm only); its fix, revA1 (the OPL3
on +3V3), must stay quiet."""
from __future__ import annotations

import re
import sys
from pathlib import Path

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
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[])
    return [x for x in viol if x["kind"] == "io_overvoltage"], facts


def _megabuzz(rail="+5V", series="27", divider=None):
    """U8 YMF262 DOAB -> R14 -> YM_DATA -> U1 XC6SLX25 IO_ pin; DIVIDER
    adds R99 of that value from YM_DATA to GND."""
    comps = {"U8": "YMF262-M", "U1": "XC6SLX25-FTG256"}
    nets = {"+3V3": [_m("U1", "A1", "VCCO_3", "power_in")],
            "GND": [_m("U8", "2", "VSS", "power_in"),
                    _m("U1", "A2", "GND", "power_in")]}
    nets.setdefault(rail, []).append(_m("U8", "1", "VDD", "power_in"))
    if series is None:
        nets["YM_DATA"] = [_m("U8", "20", "DOAB", "output"),
                           _m("U1", "N9", "IO_L37N_M3DQ1_3", "bidirectional")]
    else:
        comps["R14"] = series
        nets["Net-(U8-DOAB)"] = [_m("U8", "20", "DOAB", "output"),
                                 _m("R14", "1")]
        nets["YM_DATA"] = [_m("R14", "2"),
                           _m("U1", "N9", "IO_L37N_M3DQ1_3", "bidirectional")]
    if divider:
        comps["R99"] = divider
        nets["YM_DATA"].append(_m("R99", "1"))
        nets["GND"].append(_m("R99", "2"))
    return comps, nets


def test_reva_opl3_at_5v_through_27_ohm_is_an_error():
    hits, facts = _audit(*_megabuzz())
    assert [(h["net"], h["severity"]) for h in hits] == [("YM_DATA", "error")]
    h = hits[0]
    assert h["refs"] == ["R14", "U1", "U8"]
    assert h["level_v"] == 4.0 and h["vin_max"] == 3.95
    assert "DS162" in h["msg"] and "YMF262" in h["msg"]
    assert "(not yet re-read)" not in h["msg"]
    assert facts["io_drive_pins_checked"] == 1


def test_a1_opl3_on_3v3_is_quiet():
    hits, facts = _audit(*_megabuzz(rail="+3V3"))
    assert hits == [] and facts["io_drive_pins_checked"] == 1


def test_a_direct_join_is_an_error_too():
    hits, _ = _audit(*_megabuzz(series=None))
    assert [h["net"] for h in hits] == ["YM_DATA"]
    assert "directly" in hits[0]["msg"]


def test_a_divider_to_ground_that_brings_the_high_down_is_quiet():
    # 4 V * 2k / (1k + 2k) = 2.67 V
    assert _audit(*_megabuzz(series="1k", divider="2k"))[0] == []


def test_a_divider_too_weak_still_fires():
    # 4 V * 100k / (1k + 100k) = 3.96 V
    hits, _ = _audit(*_megabuzz(series="1k", divider="100k"))
    assert len(hits) == 1 and 3.95 < hits[0]["level_v"] < 3.97
    assert "R99 100k to GND" in hits[0]["msg"]


def test_parts_not_in_the_table_are_not_judged():
    comps, nets = _megabuzz()
    comps["U8"] = "SOME_5V_PART"
    hits, facts = _audit(comps, nets)
    assert hits == [] and facts["io_drive_pins_checked"] == 0


def test_a_driver_supply_on_an_unnamed_rail_is_not_judged():
    comps, nets = _megabuzz()
    nets["Net-(U8-VDD)"] = nets.pop("+5V")
    hits, facts = _audit(comps, nets)
    assert hits == [] and facts["io_drive_pins_checked"] == 0


def test_every_row_cites_its_datasheet():
    doc = yaml.safe_load((REPO / "reference" / "io_levels.yaml").read_text())
    for row in doc["drivers"]:
        assert set(row) <= {"mpn", "match", "verified", "supply", "pins",
                            "voh_drop", "source", "cite"}, row["mpn"]
        for k in ("match", "supply", "pins"):
            re.compile(row[k])
        assert 0 <= float(row["voh_drop"]) < 2
    for row in doc["receivers"]:
        assert set(row) <= {"mpn", "match", "verified", "pins", "vin_max",
                            "source", "cite"}, row["mpn"]
        for k in ("match", "pins"):
            re.compile(row[k])
        assert 1 < float(row["vin_max"]) < 6
    for row in doc["drivers"] + doc["receivers"]:
        assert isinstance(row["verified"], bool), row["mpn"]
        assert re.search(r"\d", row["source"]), row["source"]


def test_the_count_does_not_overwrite_other_facts():
    _, facts = _audit(*_megabuzz())
    assert facts["mcu_input_only_parts_checked"] == 0
