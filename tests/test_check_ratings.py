"""check_ratings - every pin's datasheet rating against the net it sits on.

Known answers: tests/fixtures/ratings/blinky is PCB-0001-A_stm32-blinky (P10,
shipped) reduced to refs, LCSC fields and pad nets, with its parts dir and
power tree; overvolt is the same board with U1 pad 10 (PA0, "VIN on any
other pin" = 4.0 V max) moved onto +5V. The small synthetic workspaces
below each build their own board and parts and assert both the case that
fires and the one that must not. Pure Python: no KiCad run.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIX = ROOT / "tests" / "fixtures" / "ratings"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import check_ratings as cr  # noqa: E402
import datasheet_extract  # noqa: E402
import verify_all  # noqa: E402
from _boards import board_path, need_board  # noqa: E402


def _run(ws: Path, constraints: Path | None = None) -> dict:
    argv = ["--pcb", str(ws / "kicad" / "board.kicad_pcb")]
    if constraints:
        argv += ["--constraints", str(constraints)]
    payload, _ = cr.run(argv)
    return payload


def _findings(p):
    """Everything but the note that a rating was compared with no datasheet
    read, which any attribute-rated part draws (#781)."""
    return [v for v in p["violations"]
            if v["kind"] != "rating_no_datasheet_read"]


def _errors(p: dict) -> list[dict]:
    return [v for v in p["violations"] if v["severity"] == "error"]


# ------------------------------------------------------------ known answers

def test_overvoltage_fixture_has_exactly_the_planted_error():
    p = _run(FIX / "overvolt")
    errs = _errors(p)
    assert len(errs) == 1, errs
    e = errs[0]
    assert e["kind"] == "rating_over_voltage"
    assert e["pin"] == "U1.10" and e["net"] == "+5V"
    assert e["limit_v"] == pytest.approx(4.0)
    assert "any other pin" in e["rating"]


def test_clean_shipped_board_has_no_error_and_says_what_it_could_not_rate():
    p = _run(FIX / "blinky")
    assert _errors(p) == []
    assert p["pins_checked"] > 0
    unrated = [v for v in p["violations"] if v["kind"] == "rating_unrated"]
    # never a silent pass: the bare passives are named, as warnings, and the
    # regulator's self-driven VOUT is named as covered by the supply rule
    assert unrated
    assert any("VOUT" in c for c in p["pins_covered_by_rule"])
    assert all(v["severity"] == "warning" for v in unrated)
    # the rails came from the power tree and the regulator, not guessed
    assert p["net_volts"]["+3V3"] == {"v": 3.3, "src": "power_tree"}


def test_real_shipped_board_passes():
    need_board("PCB-0001-A_stm32-blinky")
    ws = board_path("PCB-0001-A_stm32-blinky")
    payload, _ = cr.run(["--pcb", str(next((ws / "kicad").glob("*.kicad_pcb"))),
                         "--constraints", str(ws / "kicad" / "constraints.json")])
    assert _errors(payload) == []


# ------------------------------------------------------------ parsing

@pytest.mark.parametrize("value,param,rec,want", [
    ("-0.3 to 4.0", "VDD", False, (-0.3, 4.0)),
    ("VSS-0.3 to VDD+4.0", "VIN on FT", False, (-0.3, ("VDD", 4.0))),
    ("+/-20", "VGS", False, (-20.0, 20.0)),
    (-30, "VDS (max)", False, (-30.0, None)),     # P-channel: a floor
    (0.4, "VOL output low", False, (None, None)),  # a characteristic
    (200, "BVDSS_min", False, (None, None)),       # abs 'min' above 0
    (6.5, "VCC supply (min -0.5)", False, (-0.5, 6.5)),  # TI min column
    (18.5, "VBR at 1 mA (min 16.7)", False, (None, None)),  # breakdown min
    # IRF640's own VDSS row: the 'min' is its note's breakdown figure (#810)
    (200, "Vdss drain-to-source voltage (headline p.1; V(BR)Dss 200 V min at "
          "Vgs=0 V, Id=250 uA, p.2)", False, (None, 200.0)),
    (4.5, "VIN 4.5 V min", False, (None, None)),   # quiet twin: the row's own min
    (200, "V(BR)DSS breakdown (min)", False, (None, None)),
    (2.2, "operating range min", True, (2.2, None)),
    ("3.0 / 3.3 / 3.6", "VDD33 recommended", True, (3.0, 3.6)),
    ("-0.5 to (VS) + 0.5", "ALERT", False, (-0.5, ("VS", 0.5))),
])
def test_parse_bounds(value, param, rec, want):
    lo, hi = cr.parse_bounds(value, param, 1.0, rec)
    for got, exp in ((lo, want[0]), (hi, want[1])):
        if isinstance(exp, float):
            assert got == pytest.approx(exp)
        elif isinstance(exp, tuple):
            assert got[0] == exp[0] and got[1] == pytest.approx(exp[1])
        else:
            assert got is None


@pytest.mark.parametrize("net,v", [
    ("+3V3", 3.3), ("3V3", 3.3), ("+12V", 12.0), ("-5V", -5.0),
    ("/VCC_5V", 5.0), ("VBUS", 5.0), ("GND", 0.0), ("/pwr/V48_RTN", 0.0),
    ("/I2C_SDA", None), ("/SW", None), ("/power/1V35", 1.35),
    ("/power/PG_1V35", None), ("/ADC_1V35", None), ("/VBUS_J1", 5.0),
    ("/usb/VBUS_J2", 5.0), ("/VBUS_SENSE", None), ("/3V3_EN", None)])
def test_volts_from_name(net, v):
    assert cr.volts_from_name(net) == v


def test_regulator_mpn_volts():
    assert cr.reg_out_volts("AMS1117-3.3") == 3.3
    assert cr.reg_out_volts("L78L33ACUTR") == 3.3
    assert cr.reg_out_volts("L7805") == 5.0
    assert cr.reg_out_volts("TPS563201DDCR") is None
    # review #32: two digits are whole volts, and 78 only counts as the
    # MPN's own family prefix
    assert cr.reg_out_volts("L7815CV") == 15.0
    assert cr.reg_out_volts("L7818CV") == 18.0
    assert cr.reg_out_volts("MC78M05CDTRKG") == 5.0
    assert cr.reg_out_volts("HT7850") is None
    assert cr.reg_out_volts("TPS78233DDCR") is None
    assert cr.reg_out_volts("L7852CV") == 5.2
    assert cr.reg_out_volts("L7885CV") == 8.5


@pytest.mark.parametrize("cell,v", [
    ("3.3", [3.3]), ("5.0 / 4.7", [5.0, 4.7]), ("-5", [-5.0]),
    ("4.5-5.5", [5.5]), ("4.5 to 5.5 V", [5.5]), ("1.8 \u00b15%", [1.8]),
    ("3.3 +/-0.1", [3.3]), ("12 (5%)", [12.0]),
    ("4.5V-5.5V", [5.5]), ("3.0V\u20133.6V", [3.6]), ("4.5v to 5.5v", [5.5])])
def test_power_tree_cell_volts(cell, v):
    assert cr.cell_volts(cell) == v


def test_power_tree_range_and_tolerance_cells(tmp_path):
    tree = ("| Rail | Nom V |\n|---|---|\n| /VA | 4.5-5.5 |\n"
            "| /VB | 1.8 \u00b15% |\n")
    path = tmp_path / "power_tree.md"
    path.write_text(tree, encoding="utf-8")
    assert cr.power_tree_volts(path, {"/VA", "/VB"}) == {"/VA": 5.5,
                                                         "/VB": 1.8}


# ------------------------------------------------------------ synthetic boards

def _ws(tmp_path: Path, fps: list[tuple], parts: dict, bom=None,
        tree: str | None = None) -> Path:
    """fps: (ref, lcsc, {pad: net or (net, x, y)}[, footprint[, value]]);
    parts: {lcsc: extraction}."""
    ws = tmp_path / "ws"
    (ws / "kicad").mkdir(parents=True)
    (ws / "parts").mkdir()
    lines = ["(kicad_pcb (version 20240108)"]
    for ref, lcsc, pads, *fp in fps:
        lines.append(f'  (footprint "{fp[0] if fp else "x"}" '
                     f'(property "Reference" "{ref}")'
                     f' (property "LCSC" "{lcsc}")'
                     + (f' (property "Value" "{fp[1]}")' if len(fp) > 1
                        else ""))
        for num, net in pads.items():
            at = ""
            if isinstance(net, tuple):  # (net, x, y): a placed pad
                net, x, y = net
                at = f" (at {x} {y})"
            lines.append(f'    (pad "{num}" smd rect{at} (net "{net}"))')
        lines.append("  )")
    lines.append(")")
    (ws / "kicad" / "board.kicad_pcb").write_text("\n".join(lines))
    (ws / "parts" / "parts.json").write_text(json.dumps({"parts": bom or []}))
    for lcsc, ext in parts.items():
        (ws / "parts" / f"{lcsc}.json").write_text(json.dumps(ext))
    if tree:
        (ws / "architecture").mkdir()
        (ws / "architecture" / "power_tree.md").write_text(tree)
    return ws


MCU = {"mpn": "TESTMCU", "pinout": [
    {"pin": "1", "name": "VDD", "type": "power_in"},
    {"pin": "2", "name": "VSS", "type": "ground"},
    {"pin": "3", "name": "PA0", "type": "bidirectional"},
    {"pin": "4", "name": "ALERT", "type": "output"}],
    "abs_max": [
        {"param": "VDD supply voltage", "value": "-0.3 to 4.0", "unit": "V"},
        {"param": "Voltage on ALERT", "value": "-0.3 to VDD+0.3", "unit": "V"},
        {"param": "VIN on any other pin", "value": "-0.3 to 3.6", "unit": "V"},
        {"param": "VIH input high level", "value": 0.7, "unit": "V"}]}


def test_input_above_supply(tmp_path):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "2": "GND", "3": "+3V3",
                                      "4": "+5V"})], {"C1": MCU})
    p = _run(ws)
    errs = _errors(p)
    assert [e["pin"] for e in errs] == ["U1.4"]
    assert "input above supply" in errs[0]["msg"]
    assert errs[0]["limit_v"] == pytest.approx(3.6)   # VDD 3.3 + 0.3
    # PA0 on 3V3 is under its 3.6 V: kept quiet
    assert not any(v.get("pin") == "U1.3" for v in p["violations"])


def test_ground_pin_on_a_rail_is_reverse_polarity(tmp_path):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "2": "+3V3"})],
             {"C1": MCU})
    errs = _errors(_run(ws))
    assert [(e["kind"], e["pin"]) for e in errs] == \
        [("rating_reverse_polarity", "U1.2")]
    ws2 = _ws(tmp_path / "b", [("U1", "C1", {"1": "+3V3", "2": "GND"})],
              {"C1": MCU})
    assert _errors(_run(ws2)) == []


def test_negative_rail_below_abs_min(tmp_path):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "2": "GND",
                                      "3": "-5V"})], {"C1": MCU})
    errs = _errors(_run(ws))
    assert [(e["kind"], e["pin"]) for e in errs] == \
        [("rating_reverse_polarity", "U1.3")]


def test_pull_up_inference_warns_but_a_divider_stays_unknown(tmp_path):
    fps = [("U1", "C1", {"1": "+3V3", "2": "GND", "3": "/SIG"}),
           ("R1", "", {"1": "/SIG", "2": "+5V"})]
    p = _run(_ws(tmp_path, fps, {"C1": MCU}))
    ov = [v for v in p["violations"] if v["kind"] == "rating_over_voltage"]
    assert [(v["pin"], v["severity"]) for v in ov] == [("U1.3", "warning")]
    # the same pull-up plus a resistor to ground is a divider: no volts
    fps.append(("R2", "", {"1": "/SIG", "2": "GND"}))
    p = _run(_ws(tmp_path / "b", fps, {"C1": MCU}))
    assert not any(v["kind"] == "rating_over_voltage" for v in p["violations"])
    assert "/SIG" not in p["net_volts"]


def test_unrated_relative_bound_and_unnamed_pin(tmp_path):
    ext = {"mpn": "X", "pinout": [
        {"pin": "1", "name": "VCC", "type": "power_in"},
        {"pin": "2", "name": "IN", "type": "input"},
        {"pin": "3", "name": "MYSTERY", "type": "power_out"}],
        "abs_max": [{"param": "IN pin", "value": "-0.3 to VCC+0.3",
                     "unit": "V"}]}
    ws = _ws(tmp_path, [("U1", "C1", {"1": "/VX", "2": "+3V3",
                                      "3": "/Y"})], {"C1": ext})
    un = [v for v in _run(ws)["violations"] if v["kind"] == "rating_unrated"]
    assert len(un) == 1
    pins = " ".join(un[0]["pins"])
    assert "MYSTERY" in pins and "relative to VCC" in pins
    assert "1 VCC" in pins     # VCC itself: no row names it


def test_capacitor_voltage_attribute(tmp_path):
    bom = [{"lcsc": "C9", "mpn": "CAP",
            "attributes": [{"name": "Voltage Rated", "value": "6.3V"}]}]
    ws = _ws(tmp_path, [("C1", "C9", {"1": "+12V", "2": "GND"})], {}, bom)
    errs = _errors(_run(ws))
    assert [(e["kind"], e["refs"]) for e in errs] == \
        [("rating_over_voltage", ["C1"])]
    ws2 = _ws(tmp_path / "b", [("C1", "C9", {"1": "+5V", "2": "GND"})],
              {}, bom)
    assert _findings(_run(ws2)) == []


def _us_ws(tmp_path: Path, pa0: str, mpn_prop: bool = True,
           record: str | None = "TESTMCU", bom=None,
           mpn: str = "TESTMCU", value: str = "") -> Path:
    """A US-sourced board (#1119): U1 carries an MPN, no LCSC code."""
    ws = _ws(tmp_path, [], {}, bom)
    prop = f' (property "MPN" "{mpn}")' if mpn_prop else ""
    prop += f' (property "Value" "{value}")' if value else ""
    (ws / "kicad" / "board.kicad_pcb").write_text(
        '(kicad_pcb (version 20240108)\n'
        f'  (footprint "x" (property "Reference" "U1"){prop}\n'
        '    (pad "1" smd rect (net "+3V3"))\n'
        '    (pad "2" smd rect (net "GND"))\n'
        f'    (pad "3" smd rect (net "{pa0}")))\n'
        '  (footprint "x" (property "Reference" "C1")\n'
        '    (property "MPN" "GRM-6V3")\n'
        '    (pad "1" smd rect (net "+12V"))\n'
        '    (pad "2" smd rect (net "GND"))))')
    if record:
        (ws / "parts" / f"{record}.json").write_text(json.dumps(MCU))
    return ws


def test_mpn_keyed_records_load_on_a_us_sourced_board(tmp_path):
    """#1119: a part with no LCSC code finds parts/<MPN>.json by its MPN
    property, and a parts.json line by its mpn: the planted PA0 on +5V is
    an error that names the key, its quiet twin on +3V3 is clean."""
    bom = [{"mpn": "GRM-6V3", "refs": ["C1"],
            "attributes": [{"name": "Voltage Rated", "value": "6.3V"}]}]
    p = _run(_us_ws(tmp_path, "+5V", bom=bom))
    errs = sorted(_errors(p), key=lambda e: e["refs"])
    assert [(e["kind"], e["refs"], e.get("part_key")) for e in errs] == [
        ("rating_over_voltage", ["C1"], "MPN GRM-6V3"),
        ("rating_over_voltage", ["U1"], "MPN TESTMCU")]
    assert errs[1]["pin"] == "U1.3"
    assert p["keyed_by_mpn"] == {"C1": "GRM-6V3", "U1": "TESTMCU"}
    bom[0]["attributes"][0]["value"] = "25V"
    quiet = _run(_us_ws(tmp_path / "b", "+3V3", bom=bom))
    assert _errors(quiet) == []
    assert "TESTMCU" in quiet["extractions"]


def test_mpn_key_from_the_parts_json_ref_and_none_without_a_record(tmp_path):
    # no MPN property: the parts.json line that lists U1 gives the MPN
    bom = [{"mpn": "TESTMCU", "refs": ["U1"]}]
    p = _run(_us_ws(tmp_path, "+5V", mpn_prop=False, bom=bom))
    assert [e["refs"] for e in _errors(p)] == [["U1"]]
    # an MPN with no record under it stays unkeyed, as before
    p = _run(_us_ws(tmp_path / "b", "+5V", record=None))
    assert _errors(p) == []
    assert p["keyed_by_mpn"] == {}
    assert not any(v.get("part_key") for v in p["violations"])


def test_mpn_key_finds_a_record_saved_under_a_sanitized_name(tmp_path):
    # ABM8G-24.000MHZ is stored as ABM8G-24-000MHZ.json, a '#' as '-'
    p = _run(_us_ws(tmp_path, "+5V", mpn="TEST.MCU#2A", record="TEST-MCU-2A"))
    assert p["keyed_by_mpn"] == {"U1": "TEST-MCU-2A"}
    assert [e["refs"] for e in _errors(p)] == [["U1"]]
    p = _run(_us_ws(tmp_path / "b", "+3V3", mpn="TEST.MCU#2A",
                    record="TEST-MCU-2A"))
    assert _errors(p) == []
    # an MPN with a '/' is looked up only under its safe name
    p = _run(_us_ws(tmp_path / "c", "+5V", mpn="TEST/MCU", record="TEST-MCU"))
    assert p["keyed_by_mpn"] == {"U1": "TEST-MCU"}


def test_value_field_keys_a_part_with_no_number_only_with_a_record(tmp_path):
    """#815: an IC whose number is only in its Value field finds the record
    saved under it; its quiet twin on +3V3 is clean; with no record, or a
    parts.json line alone, it stays unkeyed."""
    p = _run(_us_ws(tmp_path, "+5V", mpn_prop=False, value="TESTMCU1"))
    assert p["keyed_by_mpn"] == {}      # the record is TESTMCU, not TESTMCU1
    p = _run(_us_ws(tmp_path / "b", "+5V", mpn_prop=False, value="TESTMCU1",
                    record="TESTMCU1"))
    assert p["keyed_by_mpn"] == {"U1": "TESTMCU1"}
    assert [(e["kind"], e["pin"]) for e in _errors(p)] == [
        ("rating_over_voltage", "U1.3")]
    quiet = _run(_us_ws(tmp_path / "c", "+3V3", mpn_prop=False,
                        value="TESTMCU1", record="TESTMCU1"))
    assert _errors(quiet) == []
    bom = [{"mpn": "TESTMCU1"}]
    p = _run(_us_ws(tmp_path / "d", "+5V", mpn_prop=False, value="TESTMCU1",
                    record=None, bom=bom))
    assert p["keyed_by_mpn"] == {}


def test_value_mpn_reads_part_numbers_not_component_values():
    for v in ("TPS62086", "AMS1117-3.3", "74LVC1G3157", "XC6220B331MR",
              "LM2596T-ADJ"):
        assert cr.value_mpn({"value": v}) == v
    for v in ("", "3.3V", "100nF", "10k", "4.7uF", "MCU", "LDO 3V3", "U1",
              "~"):
        assert cr.value_mpn({"value": v}) == ""


def test_constraints_and_power_tree_outrank_net_names(tmp_path):
    tree = "| Rail | Nom V |\n|---|---|\n| /VA, /VB | 5.0 / 2.5 |\n"
    ws = _ws(tmp_path, [("U1", "C1", {"1": "/VB", "2": "GND", "3": "/VA",
                                      "4": "+3V3"})], {"C1": MCU}, tree=tree)
    c = tmp_path / "constraints.json"
    c.write_text(json.dumps({"voltages": [{"net": "+3V3", "voltage": 5}]}))
    p = _run(ws, c)
    assert p["net_volts"]["/VA"] == {"v": 5.0, "src": "power_tree"}
    assert p["net_volts"]["/VB"] == {"v": 2.5, "src": "power_tree"}
    assert p["net_volts"]["+3V3"] == {"v": 5.0, "src": "constraints"}
    assert {e["pin"] for e in _errors(p)} == {"U1.3", "U1.4"}


def test_pin_ratings_outrank_the_free_text_rows(tmp_path):
    ext = dict(MCU, pin_ratings=[{"pins": ["PA0"], "kind": "voltage",
                                  "level": "abs_max", "min": -0.3, "max": 5.5,
                                  "source": "p.12 FT pin"}])
    jsonschema.validate(ext, datasheet_extract.DATASHEET_SCHEMA)
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "2": "GND",
                                      "3": "+5V"})], {"C1": ext})
    assert _errors(_run(ws)) == []
    ws2 = _ws(tmp_path / "b", [("U1", "C1", {"1": "+3V3", "2": "GND",
                                             "3": "+5V"})], {"C1": MCU})
    assert [e["pin"] for e in _errors(_run(ws2))] == ["U1.3"]


def test_regulator_output_over_current_warns(tmp_path):
    ext = {"mpn": "L78L33ACUTR", "pinout": [
        {"pin": "1", "name": "OUT", "type": "power_out"},
        {"pin": "2", "name": "GND", "type": "ground"}],
        "abs_max": [{"param": "Output current IO", "value": 100,
                     "unit": "mA"}]}
    fps = [("U1", "C1", {"1": "/RAIL", "2": "GND"})]
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"power": [{"net": "/RAIL", "current_a": 0.3}]}))
    p = _run(_ws(tmp_path, fps, {"C1": ext}), c)
    assert p["net_volts"]["/RAIL"]["src"] == "regulator U1"
    oc = [v for v in p["violations"] if v["kind"] == "rating_over_current"]
    assert [(v["pin"], v["severity"]) for v in oc] == [("U1.1", "warning")]
    c.write_text(json.dumps({"power": [{"net": "/RAIL", "current_a": 0.05}]}))
    p = _run(_ws(tmp_path / "b", fps, {"C1": ext}), c)
    assert not any(v["kind"] == "rating_over_current" for v in p["violations"])


# ------------------------------------------------------------ wiring

def test_verify_all_runs_it_only_with_a_parts_dir(tmp_path):
    check = next(c for c in verify_all.CHECKS if c["name"] == "check_ratings")
    assert check["needs"] == ["parts"]
    pcb = str(FIX / "overvolt" / "kicad" / "board.kicad_pcb")
    skipped = verify_all.run_one(check, {"pcb": pcb, "parts": None},
                                 tmp_path, strict=True)
    assert skipped["status"] == "skipped_error"
    ran = verify_all.run_one(check, {"pcb": pcb,
                                     "parts": str(FIX / "overvolt" / "parts")},
                             tmp_path)
    assert ran["status"] == "violations"
    assert [v["pin"] for v in ran["violations"]
            if v["severity"] == "error"] == ["U1.10"]


def test_fixtures_extractions_validate():
    for f in sorted(FIX.glob("*/parts/C*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        jsonschema.validate(data, datasheet_extract.DATASHEET_SCHEMA)


# ------------------------------------------------------------ reversed input

LED = {"mpn": "TESTLED", "pinout": [
    {"pin": "1", "name": "K", "type": "passive"},
    {"pin": "2", "name": "A", "type": "passive"}],
    "abs_max": [{"param": "Reverse voltage VR", "value": 5, "unit": "V"}]}


def _led_board(rail="+5V", conn_pads=None):
    return [("J1", "", conn_pads or {"1": rail, "2": "GND"}),
            ("R1", "", {"1": rail, "2": "/LED_A"}),
            ("D1", "C7", {"1": "GND", "2": "/LED_A"})]


def test_reversed_input_over_an_leds_reverse_rating_warns(tmp_path):
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"voltages": [{"net": "+5V", "voltage": 5.25}]}))
    p = _run(_ws(tmp_path, _led_board(), {"C7": LED}), c)
    rp = [v for v in p["violations"] if v["kind"] == "rating_reverse_polarity"]
    assert [(v["pin"], v["severity"], v["scenario"]) for v in rp] == \
        [("D1.1", "warning", "J1 reversed")]
    assert rp[0]["net_v"] == pytest.approx(5.25)
    assert rp[0]["limit_v"] == pytest.approx(5.0)
    # forward biased in normal use: no error, and the anode is rated as
    # the other side of the K-A row, not unrated
    assert _errors(p) == []
    assert not any(v["kind"] == "rating_unrated" and "D1" in v["msg"]
                   for v in p["violations"])
    assert p["reversed_inputs"] == [{"connector": "J1", "rail": "+5V",
                                     "rail_v": -5.25}]


def test_reversed_input_within_rating_or_not_a_two_net_input(tmp_path):
    p = _run(_ws(tmp_path, _led_board("+3V3"), {"C7": LED}))
    assert not any(v["kind"] == "rating_reverse_polarity"
                   for v in p["violations"])
    # a connector carrying a signal too (USB, a bus) is not a bare power
    # input that plugs in either way round
    fps = _led_board(conn_pads={"1": "+5V", "2": "GND", "3": "/SDA"})
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"voltages": [{"net": "+5V", "voltage": 5.25}]}))
    p = _run(_ws(tmp_path / "b", fps, {"C7": LED}), c)
    assert p["reversed_inputs"] == []
    assert not any(v["kind"] == "rating_reverse_polarity"
                   for v in p["violations"])


def test_diode_reverse_attribute_and_netlist_pin_names(tmp_path):
    # no extraction: parts.json's reverse-voltage attribute rates it, and
    # the netlist beside the board says pad 2 is the cathode
    bom = [{"lcsc": "C8", "mpn": "D", "attributes": [
        {"name": "Reverse Voltage (Vr)", "value": "4V"}]}]
    ws = _ws(tmp_path, [("D1", "C8", {"1": "GND", "2": "+5V"})], {}, bom)
    (ws / "kicad" / "board.net").write_text(
        '(export (nets (net (code "1") (name "+5V") (node (ref "D1") '
        '(pin "2") (pinfunction "K"))) (net (code "2") (name "GND") '
        '(node (ref "D1") (pin "1") (pinfunction "A")))))')
    errs = _errors(_run(ws))
    assert [(e["kind"], e["pin"]) for e in errs] == \
        [("rating_reverse_polarity", "D1.2")]
    assert "pad names" in errs[0]["msg"]
    # with no netlist, pad 1 is the cathode (KiCad's convention): the same
    # pads are then forward biased - within the reverse rating and rated,
    # but conducting straight across the rail (#645)
    ws2 = _ws(tmp_path / "b", [("D1", "C8", {"1": "GND", "2": "+5V"})],
              {}, bom)
    p = _run(ws2)
    assert [e["kind"] for e in _errors(p)] == ["rating_diode_forward"]
    assert not any(v["kind"] == "rating_unrated" for v in p["violations"])
    ws3 = _ws(tmp_path / "c", [("D1", "C8", {"1": "+5V", "2": "GND"})],
              {}, bom)
    errs = _errors(_run(ws3))
    assert [e["pin"] for e in errs] == ["D1.1"]
    assert "pad 1 taken as the cathode" in errs[0]["msg"]


TVS = {"lcsc": "C9", "mpn": "SMBJ5.0A", "attributes": [
    {"name": "Type", "value": "TVS"},
    {"name": "Polarity", "value": "Unidirectional"}]}


def _forward(p):
    return [v for v in p["violations"] if v["kind"] == "rating_diode_forward"]


def test_diode_forward_on_ground_and_backwards_clamp(tmp_path):
    # #645: a unidirectional TVS with its anode on the rail and cathode on
    # ground conducts across the rail; every rating holds
    ws = _ws(tmp_path, [("D1", "C9", {"1": "GND", "2": "+3V3"})], {}, [TVS])
    f = _forward(_run(ws))
    assert [(v["pin"], v["severity"]) for v in f] == [("D1.2", "error")]
    assert f[0]["net_v"] == pytest.approx(3.3)
    assert "(TVS)" in f[0]["msg"]
    # fitted the right way (cathode on the rail): clean
    ws = _ws(tmp_path / "b", [("D1", "C9", {"1": "+3V3", "2": "GND"})], {},
             [TVS])
    assert _forward(_run(ws)) == []
    # a clamp between two rails, anode on the higher one: backwards too
    ws = _ws(tmp_path / "c", [("D1", "C9", {"1": "+3V3", "2": "+5V"})], {},
             [TVS])
    assert [v["pin"] for v in _forward(_run(ws))] == ["D1.2"]


def test_diode_forward_left_alone(tmp_path):
    # a plain diode forward from one rail to another is an OR-ing path
    bom = [{"lcsc": "C8", "mpn": "1N5819", "attributes": []}]
    ws = _ws(tmp_path, [("D1", "C8", {"1": "+3V3", "2": "+5V"})], {}, bom)
    assert _forward(_run(ws)) == []
    # a bidirectional clamp has no wrong way round
    bi = [dict(TVS, mpn="SMBJ5.0CA", attributes=[
        {"name": "Type", "value": "TVS"}])]
    ws = _ws(tmp_path / "b", [("D1", "C9", {"1": "GND", "2": "+3V3"})], {},
             bi)
    assert _forward(_run(ws)) == []
    # an anode whose volts are only guessed through a pull-up (an LED
    # behind its resistor) is not judged
    ws = _ws(tmp_path / "c", [("R1", "", {"1": "+3V3", "2": "/LED_A"}),
                              ("D1", "C9", {"1": "GND", "2": "/LED_A"})],
             {}, [TVS])
    assert _forward(_run(ws)) == []


def test_clamp_kind():
    assert cr.clamp_kind(TVS) == ("TVS", False)
    assert cr.clamp_kind({"mpn": "SMBJ30CA"}) == ("TVS", True)
    assert cr.clamp_kind({"mpn": "BZT52C15", "attributes": [
        {"name": "Zener Voltage (Nom)", "value": "15V"}]}) == ("Zener", False)
    assert cr.clamp_kind({"mpn": "1N4148W", "attributes": []}) == (None, False)
    assert cr.clamp_kind(None) == (None, False)


def test_hwde_led_test_d1_reversed_input(tmp_path):
    """Issue #242: the cold review's POWER-2 - J1 reversed puts 5.25 V
    across D1 (KT-0603G, VR 5 V)."""
    import shutil
    ws = tmp_path / "hwde-led-test"
    src = ROOT / "boards" / "hwde-led-test"
    for d in ("kicad", "parts", "architecture"):
        shutil.copytree(src / d, ws / d)
    payload, _ = cr.run(["--pcb", str(ws / "kicad" / "hwde-led-test.kicad_pcb"),
                         "--constraints",
                         str(ws / "kicad" / "constraints.json")])
    rp = [v for v in payload["violations"]
          if v["kind"] == "rating_reverse_polarity"]
    assert [(v["pin"], v["scenario"]) for v in rp] == [("D1.1", "J1 reversed")]
    assert rp[0]["net_v"] == pytest.approx(5.25)


# ------------------------------------------- resistor Umax, fuses, ambient

def _cons(tmp_path: Path, **kw) -> Path:
    c = tmp_path / "constraints.json"
    c.write_text(json.dumps(kw))
    return c


def test_resistor_limiting_element_voltage_by_chip_size(tmp_path):
    # a 0402 bleeder across +60V: 50 V Umax (D/CRCW e3), at any power
    bom = [{"lcsc": "C25", "mpn": "0402WGF1005TCE", "attributes": []}]
    ws = _ws(tmp_path, [("R1", "C25", {"1": "+60V", "2": "GND"},
                         "Resistor_SMD:R_0402_1005Metric")], {}, bom)
    (e,) = _errors(_run(ws))
    assert e["kind"] == "rating_over_voltage" and e["refs"] == ["R1"]
    assert e["limit_v"] == 50 and "limiting element" in e["rating"]
    # the same part as an 0805 (150 V) holds, and is no longer unrated
    ws2 = _ws(tmp_path / "b", [("R1", "C25", {"1": "+60V", "2": "GND"},
                                "Resistor_SMD:R_0805_2012Metric")], {}, bom)
    assert _findings(_run(ws2)) == []


PPTC_BOM = [{"lcsc": "C70069", "mpn": "SMD1206P110TF", "category": "PTC "
             "Resettable Fuses", "attributes": [
                 {"name": "Current - Hold (Ih)", "value": "1.1A"},
                 {"name": "Current - Trip (It)", "value": "2.2A"},
                 {"name": "Voltage - Max", "value": "16V"},
                 {"name": "Operating Temperature", "value": "-40℃~+85℃"}]}]


def test_pptc_hold_current_at_ambient_and_vmax(tmp_path):
    # R-RAT-4's planted fault: a 1206 PPTC, 1.1 A hold at 23 C (~0.75 A at
    # 70 C), on a 1 A 20 V rail, Vmax 16 V: two errors
    fps = [("F1", "C70069", {"1": "/VIN_RAW", "2": "/VIN"})]
    ws = _ws(tmp_path, fps, {}, PPTC_BOM)
    c = _cons(tmp_path, ambient_c=70,
              voltages=[{"net": "/VIN_RAW", "voltage": 20},
                        {"net": "/VIN", "voltage": 20}],
              power=[{"net": "/VIN", "current_a": 1.0}])
    p = _run(ws, c)
    errs = sorted((e["kind"], e["refs"][0]) for e in _errors(p))
    assert errs == [("rating_over_current", "F1"),
                    ("rating_over_voltage", "F1")]
    oc = next(e for e in _errors(p) if e["kind"] == "rating_over_current")
    assert oc["limit_a"] == pytest.approx(0.748)
    assert p["fuses"][0]["kind"] == "PPTC"
    assert p["ambient"]["src"] == "constraints ambient_c"
    # at 25 C on a 12 V rail the same part holds 1 A with margin: quiet
    (tmp_path / "b").mkdir()
    c2 = _cons(tmp_path / "b", voltages=[{"net": "/VIN_RAW", "voltage": 12}],
               power=[{"net": "/VIN", "current_a": 0.9}])
    p2 = _run(ws, c2)
    assert _errors(p2) == []
    assert not any(v["kind"] == "rating_over_current" for v in p2["violations"])
    # under the 10 % margin warns: 1.05 A against 1.1 x 0.99
    (tmp_path / "c").mkdir()
    c3 = _cons(tmp_path / "c", ambient_c=25,
               power=[{"net": "/VIN", "current_a": 1.05}])
    w = [v for v in _run(ws, c3)["violations"]
         if v["kind"] == "rating_over_current"]
    assert [v["severity"] for v in w] == ["warning"]


def test_fuse_runs_at_75_percent_of_its_rerated_current(tmp_path):
    # Littelfuse 881 at 70 C: k 0.90. 1.5 A on a 2 A fuse is over
    # 0.75 x 0.9 x 2 = 1.35 A (warning); 2.5 A holds; 1.9 A on 2 A at 70 C
    # is over 1.8 A outright (error)
    def bom(amps):
        return [{"lcsc": "C9", "mpn": "0881002.MXEP", "attributes": [
            {"name": "Current Rating", "value": f"{amps}A"},
            {"name": "Voltage Rating (DC)", "value": "63V"}]}]
    fps = [("F1", "C9", {"1": "+12V", "2": "/V12F"})]
    def kinds(amps, iop, sub):
        d = tmp_path / sub
        d.mkdir()
        c = _cons(d, ambient_c=70, power=[{"net": "/V12F",
                                           "current_a": iop}])
        return [(v["kind"], v["severity"]) for v in
                _findings(_run(_ws(d, fps, {}, bom(amps)), c))]
    assert kinds(2, 1.5, "a") == [("rating_over_current", "warning")]
    assert kinds(2.5, 1.5, "b") == []
    assert kinds(2, 1.9, "c") == [("rating_over_current", "error")]


def test_fuse_voltage_is_the_rail_not_the_drop_across_it(tmp_path):
    bom = [{"lcsc": "C9", "mpn": "F", "attributes": [
        {"name": "Current Rating", "value": "2A"},
        {"name": "Voltage Rating", "value": "32V"}]}]
    ws = _ws(tmp_path, [("F1", "C9", {"1": "+48V", "2": "/V48F"})], {}, bom)
    (e,) = _errors(_run(ws))
    assert e["kind"] == "rating_over_voltage" and e["limit_v"] == 32
    assert e["net_v"] == 48


def test_operating_range_against_the_ambient(tmp_path):
    # a 0..70 C commercial part on an 85 C board; the -40..85 one holds
    bom = [{"lcsc": "C1", "mpn": "COMM", "attributes": [
               {"name": "Operating Temperature", "value": "0℃~+70℃"}]},
           {"lcsc": "C2", "mpn": "IND", "attributes": [
               {"name": "Operating Temperature", "value": "-40℃~+85℃"}]}]
    fps = [("U1", "C1", {"1": "+3V3", "2": "GND"}),
           ("U2", "C2", {"1": "+3V3", "2": "GND"})]
    ws = _ws(tmp_path, fps, {}, bom)
    p = _run(ws, _cons(tmp_path, ambient_c=85))
    t = [v for v in p["violations"] if v["kind"] == "rating_over_temperature"]
    assert [(v["refs"], v["severity"], v["limit_c"]) for v in t] == \
        [(["U1"], "error", 70)]
    assert p["operating_range"]["parts_checked"] == 2
    # a cold start below the industrial part's -40 C
    p = _run(ws, _cons(tmp_path, ambient_c=25, ambient_min_c=-45))
    t = [v for v in p["violations"] if v["kind"] == "rating_over_temperature"]
    assert sorted(v["refs"][0] for v in t) == ["U1", "U2"]
    # no ambient_c: 25 C, said so, and nothing fires
    p = _run(ws)
    assert p["ambient"]["c"] == 25 and "default" in p["ambient"]["src"]
    assert not any(v["kind"] == "rating_over_temperature"
                   for v in p["violations"])


def test_lamp_holder_led_is_not_a_pptc(tmp_path):
    # LCSC's LED attribute "Lamp Holder Type" once read as a hold current
    bom = [{"lcsc": "C2286", "mpn": "KT-0603R", "attributes": [
        {"name": "Lamp Holder Type", "value": "1.6mm x 0.8mm square LED"}]}]
    ws = _ws(tmp_path, [("D2", "C2286", {"1": "/LED", "2": "GND"})], {}, bom)
    assert _run(ws)["fuses"] == []


# ------------------------------------------------- not parts / held facts

def test_board_features_and_bom_excluded_footprints_are_not_rated(tmp_path):
    """#429: printed RF structures (exclude_from_bom custom copper) and
    parts.json board_feature lines are copper, not parts: no 'no LCSC'."""
    fps = [("RFS1", "", {"1": "/RF", "2": "GND", "3": "/X"}),
           ("RFS2", "", {"1": "/RF", "2": "GND", "3": "/X"}),
           ("U9", "", {"1": "/RF", "2": "GND", "3": "/X"})]
    bom = [{"refdes": ["RFS2"], "assembly_class": "board_feature"}]
    ws = _ws(tmp_path, fps, {}, bom)
    pcb = ws / "kicad" / "board.kicad_pcb"
    pcb.write_text(pcb.read_text().replace(
        '(property "Reference" "RFS1")',
        '(property "Reference" "RFS1") (attr smd exclude_from_pos_files '
        'exclude_from_bom)'))
    refs = {r for v in _run(ws)["violations"] for r in v["refs"]}
    assert refs == {"U9"}       # a real part with no LCSC still warns


def test_hand_verified_rating_is_kept_and_not_rewarned(tmp_path, monkeypatch):
    """#429: a rating someone recorded verified (ledger.py set) clears the
    unrated warning and is not rewritten to unknown by the next verify."""
    bom = [{"lcsc": "C77", "refdes": ["J1", "J2"], "mpn": "SMA"}]
    fps = [("J1", "C77", {"1": "/RF", "2": "GND"}),
           ("J2", "C77", {"1": "/RF", "2": "GND"})]
    monkeypatch.setenv("HWDE_LEDGER_WRITE", "1")
    ws = _ws(tmp_path, fps, {}, bom)
    (ws / "state.json").write_text("{}")
    import ledgerlib
    hand = {"subject": "J1", "kind": "rating", "status": "verified",
            "value": "50 V rated, checked by hand", "source": "datasheet p2"}
    ledgerlib.put(ws, [hand], "hand")
    p = _run(ws)
    un = [v for v in p["violations"] if v["kind"] == "rating_unrated"]
    assert [v["refs"] for v in un] == [["J2"]]
    assert "J1/rating" in p["held_verified"]
    led = ledgerlib.load(ws)["facts"]
    assert led["J1/rating"]["status"] == "verified"
    assert led["J1/rating"]["by"] == "hand"
    assert led["J2/rating"]["status"] == "unknown"
    # a real finding still overrides a held fact: J1 over its 'rated' volts
    bom[0]["attributes"] = [{"name": "Voltage Rated", "value": "6V"}]
    (ws / "parts" / "parts.json").write_text(json.dumps({"parts": bom}))
    pcb = ws / "kicad" / "board.kicad_pcb"
    pcb.write_text(pcb.read_text().replace('"/RF"', '"+12V"'))
    p = _run(ws)
    assert {e["refs"][0] for e in _errors(p)} == {"J1", "J2"}
    assert ledgerlib.load(ws)["facts"]["J1/rating"]["status"] == "failed"


# ------------------------------------------------------------ no attributes
# #675: a rated part whose parts.json row has no attributes and which nothing
# else rates is not a soft warning. precision-daq's D101 and F101 only showed
# up once the cache was filled, so verify had passed with them unjudged.

def _no_attrs(p: dict) -> list[dict]:
    return [v for v in p["violations"] if v["kind"] == "rating_no_attributes"]


def test_bare_diode_on_a_5v_reverse_rail_blocks_and_names_itself(tmp_path):
    bom = [{"lcsc": "C8", "mpn": "SM712", "attributes": []}]
    ws = _ws(tmp_path, [("D101", "C8", {"1": "+5V", "2": "GND"})], {}, bom)
    p = _run(ws)
    found = _no_attrs(p)
    assert [(v["severity"], v["refs"]) for v in found] == \
        [("error", ["D101"])]
    msg = found[0]["msg"]
    assert "D101" in msg and "C8" in msg and "SM712" in msg
    assert "parts_enrich" in msg and "parts_search" in msg


def test_diode_with_attributes_judged_in_rating_does_not_block(tmp_path):
    bom = [{"lcsc": "C8", "mpn": "SM712", "attributes": [
        {"name": "Reverse Voltage (Vr)", "value": "7V"}]}]
    ws = _ws(tmp_path, [("D101", "C8", {"1": "+5V", "2": "GND"})], {}, bom)
    p = _run(ws)
    assert _no_attrs(p) == [] and _errors(p) == []


def test_filled_diode_over_its_rating_gets_the_real_finding(tmp_path):
    # what filling precision-daq's cache showed: 5.25 V against a 5 V stand-off
    bom = [{"lcsc": "C8", "mpn": "SM712", "attributes": [
        {"name": "Reverse Voltage (Vr)", "value": "5V"}]}]
    ws = _ws(tmp_path, [("D101", "C8", {"1": "+5V25", "2": "GND"})], {}, bom)
    cons = tmp_path / "cons.json"
    cons.write_text(json.dumps({"voltages": [{"net": "+5V25", "voltage": 5.25},
                                             {"net": "GND", "voltage": 0}]}))
    errs = _errors(_run(ws, cons))
    assert [e["kind"] for e in errs] == ["rating_reverse_polarity"]


def test_bare_fuse_blocks_even_with_an_extraction(tmp_path):
    # fuse_findings reads hold current only from attributes, so an extraction
    # does not rate it
    bom = [{"lcsc": "C9", "mpn": "0881002", "attributes": []}]
    ws = _ws(tmp_path, [("F101", "C9", {"1": "+5V", "2": "/OUT"})],
             {"C9": {"mpn": "0881002", "pinout": []}}, bom)
    assert [v["refs"] for v in _no_attrs(_run(ws))] == [["F101"]]
    bom[0]["attributes"] = [{"name": "Hold Current", "value": "1A"}]
    ws2 = _ws(tmp_path / "b", [("F101", "C9", {"1": "+5V", "2": "/OUT"})],
              {}, bom)
    assert _no_attrs(_run(ws2)) == []


def test_bare_cap_blocks_and_one_finding_names_every_ref_on_the_code(tmp_path):
    bom = [{"lcsc": "C1", "mpn": "CL05", "attributes": []}]
    ws = _ws(tmp_path, [("C2", "C1", {"1": "+5V", "2": "GND"}),
                        ("C10", "C1", {"1": "+3V3", "2": "GND"})], {}, bom)
    found = _no_attrs(_run(ws))
    assert len(found) == 1 and found[0]["refs"] == ["C10", "C2"]
    bom[0]["attributes"] = [{"name": "Voltage Rated", "value": "16V"}]
    ws2 = _ws(tmp_path / "b", [("C2", "C1", {"1": "+5V", "2": "GND"})], {},
              bom)
    assert _no_attrs(_run(ws2)) == []


@pytest.mark.parametrize("row", [
    {"mpn": "LP5907-3.3"},                                # output in the MPN
    {"mpn": "AP2112", "description": "LDO regulator"},    # the row's words
    {"mpn": "SY8303AIC", "role": "VBUS to 5.1 V"},        # what it converts
    # the family's code, and no word says regulator (#751, linux-computer U603)
    {"mpn": "XC6206P332MR-G", "lcsc": "C5446"},
    {"mpn": "TLV75733PDBVR", "description": "", "category": ""},
    # a switching family, and no word or code says regulator (#763)
    {"mpn": "AP63205WU-7", "role": "5V"},             # rf-plasma U102
    {"mpn": "TPS54560BDDAR", "role": "+12V_BUCK"},    # rf-plasma U201/U202
    {"mpn": "TLV62569PDDCR", "role": "5 V -> +3V3"},  # linux-computer U302
    {"mpn": "TLV62568DBVR"}, {"mpn": "TPS54233DR"}, {"mpn": "TPS54302DDCR"},
    {"mpn": "LMR33630ADDAR"}, {"mpn": "AP63203WU-7"}, {"mpn": "AP63200WU-7"},
    # outside the first table, each named a switcher by its datasheet (#774)
    {"mpn": "LM5017MRX/NOPB"}, {"mpn": "LM5164DDAR", "role": "aux 5 V"},
    {"mpn": "TPS61040DBVR"}, {"mpn": "TPS61041DBVR"},
])
def test_bare_regulator_blocks_unless_extracted(tmp_path, row):
    lcsc = row.get("lcsc", "C3")
    bom = [{"lcsc": "C3", "attributes": [], **row}]
    pads = {"1": "+5V", "2": "GND", "3": "+3V3"}
    ws = _ws(tmp_path, [("U4", lcsc, pads)], {}, bom)
    assert [v["refs"] for v in _no_attrs(_run(ws))] == [["U4"]]
    ws2 = _ws(tmp_path / "b", [("U4", lcsc, pads)], {lcsc: MCU}, bom)
    assert _no_attrs(_run(ws2)) == []


@pytest.mark.parametrize("ref,mpn", [
    ("U4", "TPS2051CDGNR"),    # a package of the TPS2051C with no row
    ("U4", "TPS61042DRBR"),    # a white-LED driver, next to the TPS61040 boost
    ("U4", "LM51641"),         # a number that only starts with the LM5164
    ("U4", "LM50170"),         # the same for the LM5017
    ("U4", "TPS26630RGER"),    # an e-fuse outside the TPS26631 rows
    ("U4", "TPS3808G33DBVR"),  # a supervisor outside the TPS3808G01 row
    ("U4", "TPS92515DGQR"),    # an LED driver
    ("U4", "TLV3501AIDBVR"),   # a comparator
    ("U4", "TLV7031DBVR"),     # a comparator
    ("Q4", "TPS54560BDDAR"),   # a family counts on a U/VR/IC part only
    ("U4", "TPS7A0201PDBVR"),  # the family's adjustable code
    ("Q4", "XC6206P332MR-G"),  # a code counts on a U/VR/IC part only
])
def test_bare_part_with_no_usable_code_does_not_block(tmp_path, ref, mpn):
    bom = [{"lcsc": "C3", "mpn": mpn, "attributes": []}]
    pads = {"1": "+5V", "2": "GND", "3": "+3V3"}
    ws = _ws(tmp_path, [(ref, "C3", pads)], {}, bom)
    assert _no_attrs(_run(ws)) == []


# #797: power ICs that are no regulator, each named by its datasheet's title.
POWER_ICS = [
    ("TPS60403DBVR", "charge_pump"),       # electrometer U103, C11338
    ("LM66200DRLR", "dual ideal-diode OR, VBUS_J1/VBUS_J2 -> +5V, no back-feed"),
                                           # linux-computer U301, C3235556
    ("LM5069MM-2/NOPB", "hot-swap, auto-retry"),  # hc-power U2, C111822
    ("TPS2051BDBVR", "USB-A power switch"),       # linux-computer U607, C24593
    ("TPS22918DBVR", "load switch"),              # hc-power U6, C131941
    ("TPS26631RGER", "eFuse"),                    # rf-plasma U101, C1850273
    ("TPS2051CDBVR", "port 2 VBUS switch"),       # twin-side U113, C129581
    ("SY6280AAC", "load_switch"),                 # flex-link, C55136
    ("TPS3808G01DBVR", "reset supervisor"),       # linux-computer, C19653
    ("TPS3823-33DBVR", "watchdog supervisor"),    # rf-plasma, C7719
]


@pytest.mark.parametrize("mpn,role", POWER_ICS + [
    ("TPS60400DBVR", ""), ("TPS60401DBVR", ""), ("TPS60402DBVR", ""),
    ("LM5069MM-1/NOPB", ""),
])
def test_bare_power_ic_blocks_until_its_row_is_filled(tmp_path, mpn, role):
    bom = [{"lcsc": "C3", "mpn": mpn, "role": role, "attributes": []}]
    pads = {"1": "+5V", "2": "GND", "3": "/OUT"}
    found = _no_attrs(_run(_ws(tmp_path, [("U4", "C3", pads)], {}, bom)))
    assert [(v["severity"], v["refs"]) for v in found] == [("error", ["U4"])]
    assert "(power IC)" in found[0]["msg"] and mpn in found[0]["msg"]
    bom[0]["attributes"] = [{"name": "Input Voltage", "value": "1.6V~5.5V"}]
    p = _run(_ws(tmp_path / "b", [("U4", "C3", pads)], {}, bom))
    assert _no_attrs(p) == [] and _errors(p) == []


@pytest.mark.parametrize("mpn,role", POWER_ICS)
def test_bare_power_ic_with_an_extraction_does_not_block(tmp_path, mpn, role):
    bom = [{"lcsc": "C3", "mpn": mpn, "role": role, "attributes": []}]
    pads = {"1": "+5V", "2": "GND", "3": "/OUT"}
    ws = _ws(tmp_path, [("U4", "C3", pads)], {"C3": MCU}, bom)
    assert _no_attrs(_run(ws)) == []


@pytest.mark.parametrize("mpn,role", POWER_ICS)
def test_power_ic_family_invents_no_rating_and_names_no_rail(
        tmp_path, mpn, role):
    """The family says what the part is and which pad is its input, no rating.
    It isn't a regulator: a filled row isn't judged by its highest net (a charge
    pump's output is below its input, an ideal diode has two inputs), no output
    is asked of it, and its output net gets no volts. Here +12V is on no input
    pad, in any of the three tables."""
    row = {"lcsc": "C3", "mpn": mpn, "role": role, "attributes": []}
    pads = {"4": "+12V", "2": "GND", "3": "/RAIL"}
    assert cr.is_regulator({"ref": "U4"}, row) is False
    assert cr.reg_by_family("U4", mpn) is False and cr.reg_out_code(mpn) is None
    bare = _run(_ws(tmp_path, [("U4", "C3", pads)], {}, [row]))
    assert "/RAIL" not in bare["net_volts"]
    row["attributes"] = [{"name": "Input Voltage", "value": "1.6V~5.5V"}]
    p = _run(_ws(tmp_path / "b", [("U4", "C3", pads)], {}, [row]))
    assert "/RAIL" not in p["net_volts"]
    assert _errors(p) == [] and _unjudged(p) == []
    assert [v["refs"] for v in p["violations"]
            if v["kind"] == "rating_unrated"] == [["U4"]]


@pytest.mark.parametrize("ref,mpn", [
    ("Q4", "LM5069MM-2/NOPB"),  # a family counts on a U/VR/IC part only
    ("U4", "TPS60404DBVR"),     # past the TPS60400-03 datasheet
    ("U4", "TPS604031"),        # numbers that only start with a family's
    ("U4", "LM662001"),
    ("U4", "LM50691"),
    ("U4", "LM66100DCKR"),      # a sibling no entry's datasheet covers
    ("U4", "SN74LVC1G34DBVR"),  # a logic IC
    ("U4", "BME280"),           # a sensor
])
def test_bare_near_miss_of_a_power_ic_family_does_not_block(tmp_path, ref, mpn):
    bom = [{"lcsc": "C3", "mpn": mpn, "attributes": []}]
    pads = {"1": "+5V", "2": "GND", "3": "+3V3"}
    assert _no_attrs(_run(_ws(tmp_path, [(ref, "C3", pads)], {}, bom))) == []


# #805: a power IC's filled row against its rails. The rows are the listings'
# own (~/.cache/hwde/lcsc_listing), the pads the datasheets' (SLVS324C, SLVSG04,
# SNVS452G), and the 12 V and 100 V rails the faults.
TPS_ROW = [{"name": "Operating Voltage", "value": "1.8V~5.25V"},
           {"name": "Output Voltage", "value": "-5.5V~-1.6V"}]
LM66200_ROW = [{"name": "Input Voltage (Vin)", "value": "1.6V~5.5V"}]
LM5069_ROW = [{"name": "Voltage Rating", "value": "9V~80V"}]


def _tps_pads(rail):
    return {"1": "/VNEG", "2": rail, "3": "/CFN", "4": "GND", "5": "/CFP"}


def _lm66200_pads(vin1, vin2):
    return {"1": "GND", "2": "/VOUT", "3": vin1, "4": "GND", "5": "GND",
            "6": vin2, "7": "/VOUT"}


def _lm5069_pads(rail):
    return {"1": "/SNS", "2": rail, "3": "/UVLO", "4": "/OVLO", "5": "GND",
            "6": "/TIMER", "7": "/PWR", "8": "/PG", "9": "/VOUT", "10": "/GATE"}


# #807: the rows are the three boards' own, the pads the datasheets' (SLVS514Q,
# SLVSD76C, SLVSE94G), laid out as linux-computer U607, hc-power U6 and
# rf-plasma U101 are, exposed pad 25 included.
TPS2051B_ROW = [{"name": "Voltage - Supply", "value": "2.7V~5.5V"}]
TPS22918_ROW = [{"name": "Voltage - Supply", "value": "1V~5.5V"}]
TPS26631_ROW = [{"name": "Voltage Rating", "value": "4.5V~60V"}]


def _tps2051b_pads(rail):
    return {"1": "/VOUT", "2": "GND", "3": "/FLT", "4": "/EN", "5": rail}


def _tps22918_pads(rail):
    return {"1": rail, "2": "GND", "3": "/EN", "4": "/CT", "5": "/VOUT",
            "6": "/VOUT"}


# #815: the rows are the boards' own where filled (linux-computer's TPS3808,
# rf-plasma's TPS3823) and the datasheets' recommended range where the board's
# row is empty (twin-side's TPS2051C, flex-link's SY6280). The pads are the
# datasheets' (SLVSAU6I, the SY6280/SY6280A application note, TPS3808 Table 5-1,
# SLVS165O). rf-plasma's TPS3823 row lists its threshold options, so its top is
# 5 V, under the datasheet's 5.5 V: the row's figure is still the bound.
TPS2051C_ROW = [{"name": "Voltage - Supply", "value": "4.5V~5.5V"}]
SY6280_ROW = [{"name": "Voltage - Supply", "value": "2.4V~5.5V"}]
TPS3808_ROW = [{"name": "Voltage - Supply", "value": "1.7V~6.5V"}]
TPS3823_ROW = [{"name": "Voltage - Supply", "value": "2.5V、3V、3.3V、5V"}]


def _sy6280_pads(rail):
    return {"1": "/VOUT", "2": "GND", "3": "/ISET", "4": "/EN", "5": rail}


def _tps3808_pads(rail, sense=None):
    return {"1": "/RESET", "2": "GND", "3": "/MR", "4": "/CT",
            "5": sense or rail, "6": rail}


def _tps3823_pads(rail):
    return {"1": "/RESET", "2": "GND", "3": "/MR", "4": "/WDI", "5": rail}


def _tps26631_pads(rail, in_sys=None):
    return {"1": rail, "2": rail, "5": in_sys or rail, "6": "/UVLO", "7": "/OVP",
            "8": "GND", "9": "/DVDT", "10": "/ILIM", "15": "GND", "17": "/VOUT",
            "18": "/VOUT", "25": "GND"}


def _power_ic(tmp_path, mpn, attrs, pads, names=None, ext=None, role="", ref="U4"):
    bom = [{"lcsc": "C3", "mpn": mpn, "role": role, "attributes": attrs}]
    ws = _ws(tmp_path, [(ref, "C3", pads)], {"C3": ext} if ext else {}, bom)
    if names:
        nodes = " ".join(
            f'(net (name "{pads[p]}") (node (ref "{ref}") (pin "{p}") '
            f'(pinfunction "{n}")))' for p, n in names.items())
        (ws / "kicad" / "board.net").write_text(f"(export (nets {nodes}))")
    return ws


def _unrated(p):
    return [v["refs"] for v in p["violations"] if v["kind"] == "rating_unrated"]


def test_tps60403_on_12v_is_an_error_naming_ref_rail_and_limit(tmp_path):
    ws = _power_ic(tmp_path, "TPS60403DBVR", TPS_ROW, _tps_pads("+12V"))
    errs = _errors(_run(ws))
    assert [(v["kind"], v["refs"], v["net"], v["pin"]) for v in errs] == [
        ("rating_over_voltage", ["U4"], "+12V", "U4.2")]
    assert errs[0]["net_v"] == pytest.approx(12.0)
    assert errs[0]["limit_v"] == pytest.approx(5.25)
    assert errs[0]["rating"] == "parts.json Operating Voltage = 1.8V~5.25V"
    for word in ("U4", "TPS60403DBVR", "+12V", "12 V", "5.25 V"):
        assert word in errs[0]["msg"]
    # compared with the row, so its fact says so and where the number came from
    f = _fact(ws, "U4")
    assert (f["status"], f["rated_from"]) == ("failed", "distributor")


@pytest.mark.parametrize("rail", ["+3V3", "+5V"])
def test_tps60403_in_its_limit_is_no_error_and_stays_unrated(tmp_path, rail):
    ws = _power_ic(tmp_path, "TPS60403DBVR", TPS_ROW, _tps_pads(rail))
    p = _run(ws)
    assert _errors(p) == [] and _unrated(p) == [["U4"]]
    assert _fact(ws, "U4")["status"] == "unknown"


@pytest.mark.parametrize("vin1,vin2,over", [
    ("+5V", "+3V3", []),
    ("+12V", "+5V", [("+12V", "U4.3")]),
    ("+5V", "+12V", [("+12V", "U4.6")]),    # either input
    ("+12V", "+24V", [("+12V", "U4.3"), ("+24V", "U4.6")]),
])
def test_lm66200_either_input_over_its_limit(tmp_path, vin1, vin2, over):
    p = _run(_power_ic(tmp_path, "LM66200DRLR", LM66200_ROW,
                       _lm66200_pads(vin1, vin2)))
    errs = _errors(p)
    assert sorted((v["net"], v["pin"]) for v in errs) == over
    assert all(v["kind"] == "rating_over_voltage" and v["refs"] == ["U4"] and
               v["limit_v"] == pytest.approx(5.5) for v in errs)
    if not over:
        assert _unrated(p) == [["U4"]]


@pytest.mark.parametrize("rail,errs", [("+48V", []), ("+100V", ["+100V"])])
def test_lm5069_vin_against_its_row(tmp_path, rail, errs):
    found = _errors(_run(_power_ic(tmp_path, "LM5069MM-2/NOPB", LM5069_ROW,
                                   _lm5069_pads(rail))))
    assert [v["net"] for v in found] == errs
    assert all(v["kind"] == "rating_over_voltage" and v["pin"] == "U4.2" and
               v["limit_v"] == pytest.approx(80.0) for v in found)


@pytest.mark.parametrize("mpn,attrs,pads", [
    # the high rail is on a pad the table doesn't call an input
    ("TPS60403DBVR", TPS_ROW,
     {"1": "+12V", "2": "+3V3", "3": "/CFN", "4": "GND", "5": "/CFP"}),
    ("LM66200DRLR", LM66200_ROW, _lm66200_pads("+5V", "+5V") | {"2": "+12V"}),
    ("LM5069MM-2/NOPB", LM5069_ROW, _lm5069_pads("+48V") | {"9": "+100V"}),
    ("TPS2051BDBVR", TPS2051B_ROW, _tps2051b_pads("+5V") | {"1": "+12V"}),
    ("TPS22918DBVR", TPS22918_ROW, _tps22918_pads("+5V") | {"6": "+12V"}),
    ("TPS26631RGER", TPS26631_ROW, _tps26631_pads("+48V") | {"17": "+100V"}),
    ("TPS2051CDBVR", TPS2051C_ROW, _tps2051b_pads("+5V") | {"1": "+12V"}),
    ("SY6280AAC", SY6280_ROW, _sy6280_pads("+5V") | {"1": "+12V"}),
    # a supervisor's SENSE watches a rail of its own, and the table doesn't judge it
    ("TPS3808G01DBVR", TPS3808_ROW, _tps3808_pads("+3V3", sense="+12V")),
    ("TPS3823-33DBVR", TPS3823_ROW, _tps3823_pads("+3V3") | {"1": "+12V"}),
])
def test_power_ic_is_judged_at_its_input_not_its_highest_net(
        tmp_path, mpn, attrs, pads):
    p = _run(_power_ic(tmp_path, mpn, attrs, pads))
    assert _errors(p) == [] and _unrated(p) == [["U4"]]


@pytest.mark.parametrize("mpn,attrs,pads,net,pin,limit", [
    ("TPS2051BDBVR", TPS2051B_ROW, _tps2051b_pads("+12V"), "+12V", "U4.5", 5.5),
    ("TPS22918DBVR", TPS22918_ROW, _tps22918_pads("+12V"), "+12V", "U4.1", 5.5),
    ("TPS26631RGER", TPS26631_ROW, _tps26631_pads("+100V"), "+100V", "U4.1", 60),
    ("TPS2051CDBVR", TPS2051C_ROW, _tps2051b_pads("+12V"), "+12V", "U4.5", 5.5),
    ("SY6280AAC", SY6280_ROW, _sy6280_pads("+12V"), "+12V", "U4.5", 5.5),
    ("TPS3808G01DBVR", TPS3808_ROW, _tps3808_pads("+12V", sense="+3V3"),
     "+12V", "U4.6", 6.5),
    ("TPS3823-33DBVR", TPS3823_ROW, _tps3823_pads("+12V"), "+12V", "U4.5", 5),
])
def test_new_power_ic_over_its_input_is_an_error_naming_ref_rail_and_limit(
        tmp_path, mpn, attrs, pads, net, pin, limit):
    errs = _errors(_run(_power_ic(tmp_path, mpn, attrs, pads)))
    assert [(v["kind"], v["refs"], v["net"], v["pin"]) for v in errs] == [
        ("rating_over_voltage", ["U4"], net, pin)]
    assert errs[0]["limit_v"] == pytest.approx(limit)
    for word in ("U4", mpn, net, f"{limit:g} V"):
        assert word in errs[0]["msg"]


@pytest.mark.parametrize("mpn,attrs,pads", [
    ("TPS2051BDBVR", TPS2051B_ROW, _tps2051b_pads("+5V")),
    ("TPS22918DBVR", TPS22918_ROW, _tps22918_pads("+3V3")),
    ("TPS26631RGER", TPS26631_ROW, _tps26631_pads("+24V")),
    # exactly at the row's top passes, with nothing to spare
    ("TPS2051BDBVR", TPS2051B_ROW, _tps2051b_pads("+5V5")),
    ("TPS22918DBVR", TPS22918_ROW, _tps22918_pads("+5V5")),
    ("TPS26631RGER", TPS26631_ROW, _tps26631_pads("+60V")),
    ("TPS2051CDBVR", TPS2051C_ROW, _tps2051b_pads("+5V5")),
    ("SY6280AAC", SY6280_ROW, _sy6280_pads("+5V5")),
    ("TPS3808G01DBVR", TPS3808_ROW, _tps3808_pads("+6V5")),
    ("TPS3823-33DBVR", TPS3823_ROW, _tps3823_pads("+5V")),
])
def test_new_power_ic_within_its_input_passes_and_is_compared(
        tmp_path, mpn, attrs, pads):
    p = _run(_power_ic(tmp_path, mpn, attrs, pads))
    assert _errors(p) == [] and _no_attrs(p) == []
    assert all("no volts" not in v["msg"] for v in p["violations"])


@pytest.mark.parametrize("mpn,attrs,pads,net,limit", [
    # over the recommended maximum, under the absolute maximum (6 V, 6 V, 67 V)
    ("TPS2051BDBVR", TPS2051B_ROW, _tps2051b_pads("+5V8"), "+5V8", 5.5),
    ("TPS22918DBVR", TPS22918_ROW, _tps22918_pads("+5V8"), "+5V8", 5.5),
    ("TPS26631RGER", TPS26631_ROW, _tps26631_pads("+65V"), "+65V", 60),
    ("TPS2051CDBVR", TPS2051C_ROW, _tps2051b_pads("+5V8"), "+5V8", 5.5),
    ("TPS3808G01DBVR", TPS3808_ROW, _tps3808_pads("+6V8", sense="+3V3"),
     "+6V8", 6.5),
    # the listing's 5 V, not the datasheet's 5.5 V recommended or 6 V absolute
    ("TPS3823-33DBVR", TPS3823_ROW, _tps3823_pads("+5V4"), "+5V4", 5),
    # 5.25 V and 80 V rows against 5.5 V and 100 V absolute maxima
    ("TPS60403DBVR", TPS_ROW, _tps_pads("+5V4"), "+5V4", 5.25),
    ("LM5069MM-2/NOPB", LM5069_ROW, _lm5069_pads("+90V"), "+90V", 80),
])
def test_power_ic_bound_is_the_rows_top_not_the_absolute_maximum(
        tmp_path, mpn, attrs, pads, net, limit):
    errs = _errors(_run(_power_ic(tmp_path, mpn, attrs, pads)))
    assert [(v["net"], v["limit_v"]) for v in errs] == [(net, limit)]


@pytest.mark.parametrize("pads,net,pin,who", [
    (_tps26631_pads("+48V", in_sys="+100V"), "+100V", "U4.5", "IN_SYS"),
    (_tps26631_pads("+100V", in_sys="+48V"), "+100V", "U4.1", "IN"),
])
def test_tps26631_judges_both_its_input_pins(tmp_path, pads, net, pin, who):
    errs = _errors(_run(_power_ic(tmp_path, "TPS26631RGER", TPS26631_ROW, pads)))
    assert [(v["net"], v["pin"]) for v in errs] == [(net, pin)]
    assert f"{who} sits on" in errs[0]["msg"]


# The same part in another package has its input on other pads (SLVS514Q Table
# 5-1, SLVSE94G Table 5-1): the MPN's package code picks the pads.
TPS2051B_D = {"1": "GND", "2": "+12V", "3": "+12V", "4": "/EN", "5": "/FLT",
              "6": "/VOUT", "7": "/VOUT", "8": "/VOUT"}
TPS26631_PWP = {"1": "+100V", "2": "+100V", "3": "+100V", "4": "/GATE",
                "5": "/DRV", "6": "+48V", "7": "/UVLO", "8": "/OVP", "9": "GND",
                "21": "GND"}


@pytest.mark.parametrize("mpn,attrs,pads,pin", [
    ("TPS2051BDGNR", TPS2051B_ROW, TPS2051B_D | {"9": "GND"}, "U4.2"),
    ("TPS2051BDR", TPS2051B_ROW, TPS2051B_D, "U4.2"),
    ("TPS26631PWPR", TPS26631_ROW, TPS26631_PWP, "U4.1"),
])
def test_power_ic_package_in_the_mpn_picks_its_input_pads(
        tmp_path, mpn, attrs, pads, pin):
    errs = _errors(_run(_power_ic(tmp_path, mpn, attrs, pads)))
    assert [(v["kind"], v["pin"]) for v in errs] == [("rating_over_voltage", pin)]


@pytest.mark.parametrize("mpn,attrs,pads", [
    # pad 5 is the D package's OC and the PWP package's DRV, no input
    ("TPS2051BDGNR", TPS2051B_ROW, {"1": "GND", "2": "+5V", "3": "+5V",
                                    "4": "/EN", "5": "+12V"}),
    ("TPS26631PWPR", TPS26631_ROW, {"1": "+24V", "2": "+24V", "3": "+24V",
                                    "5": "+100V", "6": "+24V", "9": "GND"}),
])
def test_power_ic_other_packages_pads_are_not_its_input(
        tmp_path, mpn, attrs, pads):
    assert _errors(_run(_power_ic(tmp_path, mpn, attrs, pads))) == []


@pytest.mark.parametrize("mpn", [
    "TPS2051CDGNR",    # a package no board uses, its input on other pads
    "TPS3808G01DRVR",  # the WSON: VDD is pad 1, not 6
    "TPS3808G33DBVR",  # read only for G01
    "TPS3823-50DBVR",  # read only for -33
    "TPS3824-33DBVR",  # a sibling with its own pinout
    "SY6280AAAC",      # the SY6280A, which no board uses
    # one input bound is the wrong shape for these: an extraction judges them
    "INA226AIDGSR", "INA237AIDGSR", "INA240A2DR", "INA3221AIRGVR",
    "UCC27524ADR", "UCC27611DRVR", "SN6505BDBVR",
    "TPS2051BQDBVRQ1", # the automotive part has its own datasheet
    "TPS2051DBVR",     # no B, an older part
    "TPS26630RGER",    # read only for TPS26631
    "TPS22919DCKR",    # a sibling
])
def test_near_miss_of_a_new_power_ic_family_has_no_row(mpn):
    assert cr.power_ic_row(mpn) is None


def test_real_board_layouts_of_the_new_families_are_judged_by_number(tmp_path):
    """Pad 25 is U101's exposed pad: it must not put the part past its package."""
    errs = _errors(_run(_power_ic(tmp_path, "TPS26631RGER", TPS26631_ROW,
                                  _tps26631_pads("+100V"), ref="U101")))
    assert [v["pin"] for v in errs] == ["U101.1"]


def _rating_unrated(p, ref="U4"):
    return [v for v in p["violations"]
            if v["kind"] == "rating_unrated" and v["refs"] == [ref]]


@pytest.mark.parametrize("mpn,attrs,pads,who,net", [
    ("LM66200DRLR", LM66200_ROW, _lm66200_pads("/VA", "/VA"), "VIN1/VIN2", "/VA"),
    ("TPS2051BDBVR", TPS2051B_ROW, _tps2051b_pads("/VA"), "IN", "/VA"),
    ("TPS22918DBVR", TPS22918_ROW, _tps22918_pads("/VA"), "VIN", "/VA"),
    ("TPS26631RGER", TPS26631_ROW, _tps26631_pads("/VA"), "IN/IN_SYS", "/VA"),
])
def test_power_ic_input_with_no_volts_says_it_is_unjudged_and_why(
        tmp_path, mpn, attrs, pads, who, net):
    ws = _power_ic(tmp_path, mpn, attrs, pads)
    p = _run(ws)
    [v] = _rating_unrated(p)
    assert v["severity"] == "warning" and _errors(p) == []
    for word in ("U4", mpn, who, net, "no volts", "not judged"):
        assert word in v["msg"]
    assert _fact(ws, "U4")["status"] == "unknown"


@pytest.mark.parametrize("mpn,attrs,pads,said", [
    ("TPS26631RGER", TPS26631_ROW, _tps26631_pads("+24V"),
     "IN/IN_SYS at 24 V within the 60 V row; other pins unrated"),
    ("TPS26631RGER", TPS26631_ROW, _tps26631_pads("+60V"),
     "IN/IN_SYS at 60 V within the 60 V row; other pins unrated"),
    ("TPS26631RGER", TPS26631_ROW, _tps26631_pads("+24V", in_sys="+12V"),
     "IN_SYS at 12 V, IN at 24 V within the 60 V row; other pins unrated"),
    ("TPS2051BDBVR", TPS2051B_ROW, _tps2051b_pads("+5V"),
     "IN at 5 V within the 5.5 V row; other pins unrated"),
    ("TPS2051CDBVR", TPS2051C_ROW, _tps2051b_pads("+5V"),
     "IN at 5 V within the 5.5 V row; other pins unrated"),
    ("SY6280AAC", SY6280_ROW, _sy6280_pads("+5V"),
     "IN at 5 V within the 5.5 V row; other pins unrated"),
    ("TPS3808G01DBVR", TPS3808_ROW, _tps3808_pads("+3V3"),
     "VDD at 3.3 V within the 6.5 V row; other pins unrated"),
    ("TPS3823-33DBVR", TPS3823_ROW, _tps3823_pads("+3V3"),
     "VDD at 3.3 V within the 5 V row; other pins unrated"),
    ("LM66200DRLR", LM66200_ROW, _lm66200_pads("+5V", "+5V"),
     "VIN1/VIN2 at 5 V within the 5.5 V row; other pins unrated"),
])
def test_power_ic_input_within_its_row_says_so_and_stays_unrated(
        tmp_path, mpn, attrs, pads, said):
    """A pass is reported, not promoted: the warning and unknown fact stay (#708)."""
    ws = _power_ic(tmp_path, mpn, attrs, pads)
    p = _run(ws)
    [v] = _rating_unrated(p)
    assert said in v["msg"] and mpn in v["msg"]
    assert "no datasheet extraction" not in v["msg"]
    assert v["severity"] == "warning" and _errors(p) == []
    assert _fact(ws, "U4")["status"] == "unknown"


# #815: a monitor's shunt pins take the bus and its VS takes a logic supply, so
# one input bound can't judge it. An extraction with per-pin limits can, and needs
# no table row. Limits and pads: TI SBOS547C section 5.1 and Pin Functions, DGS.
INA226_EXT = {
    "mpn": "INA226AIDGSR",
    "pinout": [{"pin": p, "name": n, "type": t} for p, n, t in [
        ("1", "A1", "input"), ("2", "A0", "input"), ("3", "ALERT", "output"),
        ("4", "SDA", "bidirectional"), ("5", "SCL", "input"),
        ("6", "VS", "power_in"), ("7", "GND", "ground"), ("8", "VBUS", "input"),
        ("9", "IN-", "input"), ("10", "IN+", "input")]],
    "pin_ratings": [
        {"pins": ["VS"], "kind": "voltage", "level": "abs_max", "max": 6,
         "source": "SBOS547C 5.1"},
        {"pins": ["IN+", "IN-", "VBUS"], "kind": "voltage", "level": "abs_max",
         "min": -0.3, "max": 40, "source": "SBOS547C 5.1"}]}


def _ina226_pads(vs, bus):
    return {"1": "GND", "2": "GND", "3": "/ALERT", "4": "/SDA", "5": "/SCL",
            "6": vs, "7": "GND", "8": bus, "9": bus, "10": bus}


def test_monitor_extraction_judges_sense_pins_and_supply_apart(tmp_path):
    jsonschema.validate(INA226_EXT, datasheet_extract.DATASHEET_SCHEMA)
    ws = _power_ic(tmp_path, "INA226AIDGSR", [], _ina226_pads("+3V3", "+24V"),
                   ext=INA226_EXT)
    assert _errors(_run(ws)) == []
    assert _fact(ws, "U4")["status"] != "failed"


@pytest.mark.parametrize("vs,bus,net,pins,limit", [
    # 24 V is fine at IN+, and would burn VS
    ("+24V", "+24V", "+24V", {"U4.6"}, 6),
    ("+3V3", "+48V", "+48V", {"U4.8", "U4.9", "U4.10"}, 40),
])
def test_monitor_extraction_over_a_pin_limit_names_that_pin(
        tmp_path, vs, bus, net, pins, limit):
    errs = _errors(_run(_power_ic(tmp_path, "INA226AIDGSR", [],
                                  _ina226_pads(vs, bus), ext=INA226_EXT)))
    assert {v["pin"] for v in errs} == pins
    assert {(v["kind"], v["net"], v["limit_v"]) for v in errs} == {
        ("rating_over_voltage", net, limit)}


def test_power_ic_names_only_the_input_with_no_volts(tmp_path):
    p = _run(_power_ic(tmp_path, "LM66200DRLR", LM66200_ROW,
                       _lm66200_pads("+5V", "/VB")))
    [v] = _rating_unrated(p)
    assert "VIN2" in v["msg"] and "/VB" in v["msg"]
    assert "VIN1" not in v["msg"] and "+5V" not in v["msg"]


def test_power_ic_over_limit_input_still_fires_beside_one_with_no_volts(tmp_path):
    p = _run(_power_ic(tmp_path, "LM66200DRLR", LM66200_ROW,
                       _lm66200_pads("+12V", "/VB")))
    assert [v["net"] for v in _errors(p)] == ["+12V"]


def test_power_ic_input_on_a_pull_up_says_its_volts_are_a_guess(tmp_path):
    bom = [{"lcsc": "C3", "mpn": "TPS22918DBVR", "attributes": TPS22918_ROW}]
    ws = _ws(tmp_path, [("U4", "C3", _tps22918_pads("/IN")),
                        ("R1", "", {"1": "/IN", "2": "+12V"}, "R_0402")], {}, bom)
    [v] = _rating_unrated(_run(ws))
    for word in ("VIN", "/IN", "pull-up", "not judged"):
        assert word in v["msg"]


@pytest.mark.parametrize("pads,names", [
    (_tps_pads("+12V") | {"16": "/X"}, None),
    (_tps_pads("+12V"), {"1": "A_1", "2": "B_2"}),
])
def test_power_ic_with_no_input_pad_found_says_so(tmp_path, pads, names):
    p = _run(_power_ic(tmp_path, "TPS60403DBVR", TPS_ROW, pads, names))
    [v] = _rating_unrated(p)
    for word in ("no pad", "not judged", "SLVS324C"):
        assert word in v["msg"]


def test_lm66200_on_vbus_nets_is_judged_from_their_names(tmp_path):
    """linux-computer U301: no constraint gives VBUS_J1 or VBUS_J2 a voltage,
    the name rule does (5 V), so it is compared, not left with no volts."""
    p = _run(_power_ic(tmp_path, "LM66200DRLR", LM66200_ROW,
                       _lm66200_pads("VBUS_J1", "VBUS_J2"), ref="U301"))
    assert p["net_volts"]["VBUS_J1"]["v"] == pytest.approx(5.0)
    assert _errors(p) == []
    assert all("no volts" not in v["msg"] for v in _rating_unrated(p, "U301"))


def test_power_ic_row_with_regulator_words_is_still_judged_at_its_input(tmp_path):
    """A family's table outranks the words: no highest-net guess, no second
    finding from regulator_findings."""
    role = "charge pump voltage regulator"
    over = {"1": "+24V", "2": "+12V", "3": "/CFN", "4": "GND", "5": "/CFP"}
    errs = _errors(_run(_power_ic(tmp_path, "TPS60403DBVR", TPS_ROW, over,
                                  role=role)))
    assert [(v["net"], v["pin"]) for v in errs] == [("+12V", "U4.2")]
    fine = {"1": "+12V", "2": "+3V3", "3": "/CFN", "4": "GND", "5": "/CFP"}
    assert _errors(_run(_power_ic(tmp_path / "b", "TPS60403DBVR", TPS_ROW, fine,
                                  role=role))) == []


def test_power_ic_pad_names_say_which_pad_is_its_input(tmp_path):
    """Names are the table's too, and outrank its numbers: this symbol has IN
    on pad 1."""
    pads = {"1": "+12V", "2": "+3V3", "3": "/CFN", "4": "GND", "5": "/CFP"}
    names = {"1": "IN_1", "2": "OUT_2"}
    errs = _errors(_run(_power_ic(tmp_path, "TPS60403DBVR", TPS_ROW, pads, names)))
    assert [(v["net"], v["pin"]) for v in errs] == [("+12V", "U4.1")]
    assert "IN" in errs[0]["msg"]


def test_power_ic_with_names_the_table_lacks_is_not_judged(tmp_path):
    """The board has names and none is the datasheet's input: nothing says
    which pad it is, so no pad number stands in for it."""
    names = {"1": "A_1", "2": "B_2"}
    p = _run(_power_ic(tmp_path, "TPS60403DBVR", TPS_ROW, _tps_pads("+12V"), names))
    assert _errors(p) == [] and _unrated(p) == [["U4"]]


def test_power_ic_footprint_past_its_package_is_not_judged_by_number(tmp_path):
    """Pad 2 of a 16-pad footprint isn't the 5-pin DBV package's IN."""
    pads = _tps_pads("+12V") | {"16": "/X"}
    p = _run(_power_ic(tmp_path, "TPS60403DBVR", TPS_ROW, pads))
    assert _errors(p) == [] and _unrated(p) == [["U4"]]


@pytest.mark.parametrize("mpn,pads", [
    ("TPS60403DBVR", _tps_pads("+12V")),
    ("LM66200DRLR", _lm66200_pads("+12V", "+12V")),
    ("LM5069MM-2/NOPB", _lm5069_pads("+100V")),
    ("TPS2051BDBVR", _tps2051b_pads("+12V")),
    ("TPS22918DBVR", _tps22918_pads("+12V")),
    ("TPS26631RGER", _tps26631_pads("+100V")),
])
def test_power_ic_row_with_no_voltage_stays_unrated(tmp_path, mpn, pads):
    """No number in the row: the table has none to lend and none is invented."""
    attrs = [{"name": "Package", "value": "SOT-23"},
             {"name": "Output Voltage", "value": "-5.5V~-1.6V"}]
    ws = _power_ic(tmp_path, mpn, attrs, pads)
    p = _run(ws)
    assert _errors(p) == [] and _unrated(p) == [["U4"]]
    assert _no_attrs(p) == [] and _fact(ws, "U4")["status"] == "unknown"


def test_power_ic_input_on_a_pull_up_guess_is_not_judged(tmp_path):
    """A pull-up's volts are a guess at the pin, as for a regulator."""
    bom = [{"lcsc": "C3", "mpn": "TPS60403DBVR", "attributes": TPS_ROW}]
    ws = _ws(tmp_path, [("U4", "C3", _tps_pads("/IN")),
                        ("R1", "", {"1": "/IN", "2": "+12V"}, "R_0402")], {}, bom)
    assert [v for v in _errors(_run(ws)) if v["refs"] == ["U4"]] == []


def test_power_ic_extraction_outranks_its_row(tmp_path):
    ws = _power_ic(tmp_path, "TPS60403DBVR", TPS_ROW,
                   {"1": "+3V3", "2": "GND", "3": "/A", "4": "/B"}, ext=MCU)
    assert [v for v in _errors(_run(ws)) if "input rating" in v["msg"]] == []
    over = _power_ic(tmp_path / "b", "TPS60403DBVR", TPS_ROW,
                     {"1": "+12V", "2": "+12V", "3": "/A", "4": "/B"}, ext=MCU)
    errs = _errors(_run(over))
    assert errs and all("input rating" not in v["msg"] for v in errs)


@pytest.mark.parametrize("ref,mpn", [
    ("Q4", "LM66200DRLR"),      # a family counts on a U/VR/IC part only
    ("U4", "TPS60404DBVR"),     # past the TPS60400-03 datasheet
    ("U4", "LM66100DCKR"),      # a sibling no entry's datasheet covers
    ("U4", "SN74LVC1G34DBVR"),  # a logic IC
])
def test_near_miss_of_a_power_ic_family_is_not_judged_by_its_table(
        tmp_path, ref, mpn):
    ws = _power_ic(tmp_path, mpn, LM66200_ROW,
                   _lm66200_pads("+12V", "+12V"), ref=ref)
    assert _errors(_run(ws)) == []


@pytest.mark.parametrize("family,pins", [
    ("TPS60400", {"IN": "2"}), ("TPS60403", {"IN": "2"}),
    ("LM66200", {"VIN1": "3", "VIN2": "6"}),
    ("LM5069", {"VIN": "2"}),
    # every pad a part has for its input, in the package the MPN names
    ("TPS2051BDBV", {"IN": "5"}), ("TPS2051BDGN", {"IN": "2 3"}),
    ("TPS2051BDR", {"IN": "2 3"}),
    ("TPS22918DBV", {"VIN": "1"}),
    ("TPS26631RGE", {"IN": "1 2", "IN_SYS": "5"}),
    ("TPS26631PWP", {"IN": "1 2 3", "IN_SYS": "6"}),
    ("TPS2051CDBV", {"IN": "5"}),
    ("SY6280AAC", {"IN": "5"}),
    ("TPS3808G01DBV", {"VDD": "6"}),
    ("TPS3823-33DBV", {"VDD": "5"}),
])
def test_power_ic_table_gives_each_family_its_datasheet_pads(family, pins):
    row = cr.power_ic_row(family + "XYZ")
    assert row["input"] == pins
    assert row["datasheet"]     # cited, per family


def test_bare_coded_regulator_stays_unknown_and_names_no_rail(tmp_path):
    """The code only makes it a regulator for this finding: is_regulator and
    reg_out_volts stay strict, so no rail gets its volts from the code."""
    bom = [{"lcsc": "C3", "mpn": "XC6206P332MR-G", "attributes": []}]
    pads = {"1": "+5V", "2": "GND", "3": "/RAIL"}
    ws = _ws(tmp_path, [("U4", "C3", pads)], {}, bom)
    p = _run(ws)
    assert [v["refs"] for v in _no_attrs(p)] == [["U4"]]
    assert "/RAIL" not in p["net_volts"]
    assert not cr.is_regulator({"ref": "U4"}, bom[0])


@pytest.mark.parametrize("ref,lcsc,pads,row", [
    # a chip resistor is rated from its package
    ("R1", "C4", {"1": "+5V", "2": "GND"}, {"mpn": "0402WGF"}),
    # attributes do not rate these, so filling them would change nothing
    ("U1", "C4", {"1": "+5V", "2": "GND", "3": "/A"}, {"mpn": "STM32"}),
    ("J1", "C4", {"1": "+5V", "2": "GND"}, {"mpn": "USB-C"}),
    # one net stresses nothing
    ("C1", "C4", {"1": "GND", "2": "GND"}, {"mpn": "CL05"}),
])
def test_bare_parts_that_do_not_block(tmp_path, ref, lcsc, pads, row):
    bom = [{"lcsc": lcsc, "attributes": [], **row}]
    assert _no_attrs(_run(_ws(tmp_path, [(ref, lcsc, pads)], {}, bom))) == []


def test_part_with_no_row_at_all_stays_a_warning(tmp_path):
    # the scaffold's empty parts/: nothing to fill yet, intake must still pass
    ws = _ws(tmp_path, [("D1", "C8", {"1": "+5V", "2": "GND"})], {}, [])
    p = _run(ws)
    assert _no_attrs(p) == [] and _errors(p) == []
    assert any(v["kind"] == "rating_unrated" for v in p["violations"])


def test_rating_someone_verified_does_not_block(tmp_path):
    bom = [{"lcsc": "C8", "mpn": "SM712", "attributes": []}]
    ws = _ws(tmp_path, [("D101", "C8", {"1": "+5V", "2": "GND"})], {}, bom)
    out, _ = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                          None, None, trusted={"D101"})
    assert not [v for v in out if v["kind"] == "rating_no_attributes"]


def test_no_attributes_is_unknown_in_the_ledger_not_failed(tmp_path):
    # nothing was found wrong with the part: its rating was never looked at
    bom = [{"lcsc": "C8", "mpn": "SM712", "attributes": []}]
    ws = _ws(tmp_path, [("D101", "C8", {"1": "+5V", "2": "GND"})], {}, bom)
    _, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                            None, None)
    rating = next(f for f in facts["ledger_facts"]
                  if f["subject"] == "D101" and f["kind"] == "rating")
    assert rating["status"] == "unknown" and "parts_enrich" in rating["value"]


# ------------------------------------------- regulator input and output (#708)

def _reg_ws(tmp_path, attrs, pads, names=None, ext=None, **row):
    """A regulator U4 (C9) whose row holds attrs ({name: value}); names:
    {pad: pad name} in a netlist beside the board."""
    bom = [{"lcsc": "C9", "mpn": row.pop("mpn", "AP2112"),
            "attributes": [{"name": k, "value": v} for k, v in attrs.items()],
            "description": "LDO regulator", **row}]
    ws = _ws(tmp_path, [("U4", "C9", pads)], {"C9": ext} if ext else {}, bom)
    if names:
        nodes = " ".join(
            f'(net (name "{pads[p]}") (node (ref "U4") (pin "{p}") '
            f'(pinfunction "{n}")))' for p, n in names.items())
        (ws / "kicad" / "board.net").write_text(f"(export (nets {nodes}))")
    return ws


LDO_PADS = {"1": "+12V", "2": "GND", "3": "+3V3"}


def test_6v_ldo_on_12v_blocks_naming_part_rating_and_rail(tmp_path):
    """The known answer of #708: its row says 6 V, its rail says 12 V."""
    ws = _reg_ws(tmp_path, {"Input Voltage(Max)": "6V", "Voltage Rated": "6V"},
                 LDO_PADS)
    errs = _errors(_run(ws))
    assert len(errs) == 1
    v = errs[0]
    assert v["kind"] == "rating_over_voltage" and v["refs"] == ["U4"]
    assert v["net"] == "+12V" and v["limit_v"] == 6 and v["net_v"] == 12
    for word in ("U4", "AP2112", "6 V", "+12V", "12 V", "Input Voltage(Max)"):
        assert word in v["msg"]


def test_16v_ldo_on_12v_passes_and_stays_unrated_for_its_other_pins(tmp_path):
    ws = _reg_ws(tmp_path, {"Input Voltage(Max)": "16V", "Voltage Rated": "16V"},
                 LDO_PADS, mpn="AP2112-3.3")
    p = _run(ws)
    assert _errors(p) == []
    # only the input was judged: this does not say the part is verified
    assert [v["kind"] for v in p["violations"]] == ["rating_unrated"]


@pytest.mark.parametrize("name,value,errs", [
    ("Voltage - Supply", "2.5V~5.5V", 1),    # the top of a range rates it
    ("Voltage - Supply", "4.5V~28V", 0),
    ("Voltage - Supply", "500mV~5.5V", 1),   # not 500 V
    ("Voltage - Input(DC)", "5.5V~65V", 0),
    ("Operating Voltage", "5.7V", 1),
    ("Voltage Rating", "4.5V~60V", 0),
    ("Output Voltage", "3.3V", 0),           # not an input rating
    ("Dropout Voltage", "250mV", 0),
])
def test_input_rating_read_from_the_names_real_rows_use(
        tmp_path, name, value, errs):
    ws = _reg_ws(tmp_path, {name: value}, LDO_PADS)
    assert len(_errors(_run(ws))) == errs


def test_input_rating_prefers_the_input_row_over_a_generic_one(tmp_path):
    ws = _reg_ws(tmp_path, {"Voltage Rated": "40V",
                            "Input Voltage(Max)": "6V"}, LDO_PADS)
    assert [v["limit_v"] for v in _errors(_run(ws))] == [6]


@pytest.mark.parametrize("row", [
    {"mpn": "XYZ1", "description": "boost converter"},
    {"mpn": "XYZ2", "description": "step-up DC-DC"},
    {"mpn": "XYZ3", "description": "DC-DC", "extra": {"Output Voltage": "24V"}},
])
def test_unnamed_step_up_is_not_judged_by_its_highest_net(tmp_path, row):
    """Its output rail is above its input rating on purpose, and with no pad
    names nothing says which net feeds it."""
    attrs = {"Voltage - Supply": "5.5V", **row.pop("extra", {})}
    ws = _reg_ws(tmp_path, attrs, {"1": "+3V7", "2": "GND", "3": "+24V"}, **row)
    assert _errors(_run(ws)) == []


def test_boost_family_is_not_judged_by_its_highest_net(tmp_path):
    """The TPS61040 takes at most 6 V and makes up to 28 V, and neither its row
    nor a pad name has to say so: +12V here is its output (#774)."""
    ws = _reg_ws(tmp_path, {"Voltage - Supply": "6V"},
                 {"1": "+5V", "2": "GND", "3": "+12V"},
                 mpn="TPS61040DBVR", description="")
    assert _errors(_run(ws)) == []


@pytest.mark.parametrize("mpn", ["LM5017MRX/NOPB", "LM5164DDAR"])
def test_step_down_family_is_judged_by_its_highest_net(tmp_path, mpn):
    """A filled row with no word saying regulator is judged once the family
    says so (#774). The rating here is planted, not the part's own."""
    ws = _reg_ws(tmp_path, {"Voltage - Supply": "24V"},
                 {"1": "+48V", "2": "GND", "3": "+5V"},
                 mpn=mpn, description="")
    assert [(v["kind"], v["net"]) for v in _errors(_run(ws))] == \
        [("rating_over_voltage", "+48V")]


@pytest.mark.parametrize("mpn", ["LM5017MRX/NOPB", "LM5164DDAR", "TPS61040DBVR"])
def test_a_family_entry_names_no_rail(mpn):
    """It says what the part is, not what it makes."""
    assert cr.reg_out_code(mpn) is None
    assert cr.reg_out_volts(mpn) is None


def test_named_pads_judge_the_input_not_the_highest_net(tmp_path):
    attrs = {"Voltage - Supply": "5.5V", "Output Voltage": "2.2V~24V"}
    pads = {"1": "+3V7", "2": "GND", "3": "+24V"}
    names = {"1": "VIN_1", "2": "GND_2", "3": "VOUT_3"}
    ws = _reg_ws(tmp_path, attrs, pads, names, description="boost converter")
    assert _errors(_run(ws)) == []
    ws2 = _reg_ws(tmp_path / "b", attrs, {**pads, "1": "+12V"}, names,
                  description="boost converter")
    assert [(v["net"], v["pin"]) for v in _errors(_run(ws2))] == \
        [("+12V", "U4.1")]


def test_enable_pin_above_the_input_rating_blocks(tmp_path):
    pads = {"1": "+5V", "2": "GND", "3": "+12V", "5": "+3V3"}
    names = {"1": "IN_1", "2": "GND_2", "3": "EN_3", "5": "OUT_5"}
    ws = _reg_ws(tmp_path, {"Voltage - Supply": "6V"}, pads, names)
    errs = _errors(_run(ws))
    assert [(v["net"], v["pin"]) for v in errs] == [("+12V", "U4.3")]
    assert "EN" in errs[0]["msg"]


def test_enable_tied_to_an_over_rated_input_is_one_finding(tmp_path):
    pads = {"1": "+12V", "2": "GND", "3": "+12V", "5": "+3V3"}
    names = {"1": "IN_1", "2": "GND_2", "3": "EN_3", "5": "OUT_5"}
    ws = _reg_ws(tmp_path, {"Voltage - Supply": "6V"}, pads, names)
    assert len(_errors(_run(ws))) == 1


def test_enable_through_a_resistor_is_not_held_to_the_rail(tmp_path):
    """A pull-up's volts are a guess at the pin: the supply rule in run_check
    says the same."""
    bom = [{"lcsc": "C9", "mpn": "AP2112", "description": "LDO regulator",
            "attributes": [{"name": "Voltage - Supply", "value": "6V"}]}]
    pads = {"1": "+5V", "2": "GND", "3": "/EN", "5": "+3V3"}
    ws = _ws(tmp_path, [("U4", "C9", pads),
                        ("R1", "", {"1": "/EN", "2": "+12V"}, "R_0402")],
             {}, bom)
    (ws / "kicad" / "board.net").write_text(
        '(export (nets (net (name "/EN") (node (ref "U4") (pin "3") '
        '(pinfunction "EN_3")))))')
    assert [v for v in _errors(_run(ws)) if v["refs"] == ["U4"]] == []


@pytest.mark.parametrize("attrs,mpn,out_net,errs", [
    ({"Voltage - Supply": "6V", "Output Voltage": "3.3V"}, "AP2112", "+5V", 1),
    ({"Voltage - Supply": "6V", "Output Voltage": "3.3V"}, "AP2112", "+1V8", 1),
    ({"Voltage - Supply": "6V", "Output Voltage": "3.3V"}, "AP2112", "+3V3", 0),
    # the MPN says it when the row doesn't
    ({"Voltage - Supply": "6V"}, "LP5907-3.3", "+5V", 1),
    ({"Voltage - Supply": "6V"}, "LP5907-3.3", "+3V3", 0),
    # an adjustable part reaches anything in its range
    ({"Voltage - Supply": "28V", "Output Voltage": "768mV~7V"}, "T1", "+5V", 0),
    ({"Voltage - Supply": "28V", "Output Voltage": "768mV~7V"}, "T1", "+12V", 1),
    ({"Voltage - Supply": "28V"}, "T1", "+5V", 0),
])
def test_output_rail_against_what_the_row_says_it_makes(
        tmp_path, attrs, mpn, out_net, errs):
    pads = {"1": "+24V" if "28V" in attrs["Voltage - Supply"] else "+5V",
            "2": "GND", "3": out_net}
    names = {"1": "IN_1", "2": "GND_2", "3": "OUT_3"}
    if out_net == pads["1"]:
        pads["1"] = "+5V0"
    ws = _reg_ws(tmp_path, attrs, pads, names, mpn=mpn)
    found = [v for v in _errors(_run(ws))
             if v["kind"] == "rating_regulator_output"]
    assert len(found) == errs
    if errs:
        assert out_net in found[0]["msg"] and "U4" in found[0]["msg"]


@pytest.mark.parametrize("mpn,errs", [
    ("ADP7182-5.0", 0),   # a negative part: the rail's sign is what it makes
    ("ADP7182-3.3", 1),
    ("LT9999-5.0", 0),    # neither family: unknown sign, magnitude only
    ("AMS1117-5.0", 1),   # a positive part can't make -5 V (#744)
    ("AMS1117-3.3", 1)])
def test_unextracted_output_on_a_negative_rail(tmp_path, mpn, errs):
    ws = _reg_ws(tmp_path, {"Voltage - Supply": "15V"},
                 {"1": "+12V", "2": "GND", "3": "-5V"},
                 {"1": "IN", "3": "OUT"}, mpn=mpn)
    assert len([v for v in _errors(_run(ws))
                if v["kind"] == "rating_regulator_output"]) == errs


def test_output_needs_a_named_pad(tmp_path):
    """No pad names: nothing says which net it drives, so it isn't judged."""
    ws = _reg_ws(tmp_path, {"Voltage - Supply": "6V", "Output Voltage": "3.3V"},
                 {"1": "+5V", "2": "GND", "3": "+1V8"})
    assert _errors(_run(ws)) == []


def test_extraction_outranks_the_attribute(tmp_path):
    """Extracted parts are judged pin by pin from the datasheet's rows."""
    ws = _reg_ws(tmp_path, {"Voltage - Supply": "6V"},
                 {"1": "+3V3", "2": "GND", "3": "/A"}, ext=MCU)
    assert _errors(_run(ws)) == []


def test_over_rated_regulator_fails_its_ledger_fact(tmp_path):
    ws = _reg_ws(tmp_path, {"Input Voltage(Max)": "6V"}, LDO_PADS)
    _, got = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                          None, None)
    rating = {f["subject"]: f for f in got["ledger_facts"]
              if f["kind"] == "rating"}
    assert rating["U4"]["status"] == "failed"
    assert "+12V" in rating["U4"]["value"]


# ------------------------------------- an extracted regulator's output (#721)

def _fixed_ldo(mpn):
    return {"mpn": mpn, "pinout": [
        {"pin": "1", "name": "GND", "type": "ground"},
        {"pin": "2", "name": "VOUT", "type": "power_out"},
        {"pin": "3", "name": "VIN", "type": "power_in"}],
        "abs_max": [{"param": "Input voltage VIN", "value": 15, "unit": "V"}]}


def _fixed_buck(mpn="AP63205WU-7", **over):
    """AP6320x's real pinout: it has no output pin, FB senses the rail."""
    return {"mpn": mpn, "description": "fixed 5.0V output", "pinout": [
        {"pin": "1", "name": "FB", "type": "input"},
        {"pin": "2", "name": "EN", "type": "input"},
        {"pin": "3", "name": "VIN", "type": "power_in"},
        {"pin": "4", "name": "GND", "type": "ground"},
        {"pin": "5", "name": "SW", "type": "output"},
        {"pin": "6", "name": "BST", "type": "passive"}],
        "abs_max": [{"param": "Input voltage VIN", "value": 32, "unit": "V"}],
        **over}


def _buck_pads(fb):
    return {"1": fb, "2": "+12V", "3": "+12V", "4": "GND", "5": "SW_NODE",
            "6": "BST_NODE"}


def _out_errs(p):
    return [v for v in _errors(p) if v["kind"] == "rating_regulator_output"]


def test_extracted_fixed_buck_on_the_wrong_rail_blocks(tmp_path):
    """FB is a fixed part's output, on a rail
    the part can't make (#774)."""
    ws = _ws(tmp_path, [("U4", "C9", _buck_pads("+3V3"))],
             {"C9": _fixed_buck()})
    errs = _out_errs(_run(ws))
    assert [(v["refs"], v["net"], v["pin"]) for v in errs] == \
        [(["U4"], "+3V3", "U4.1")]
    assert errs[0]["net_v"] == 3.3


def test_extracted_fixed_buck_on_its_own_rail_passes(tmp_path):
    ws = _ws(tmp_path, [("U4", "C9", _buck_pads("+5V"))],
             {"C9": _fixed_buck()})
    p = _run(ws)
    assert _out_errs(p) == [] and _unjudged(p) == []


def test_extracted_fixed_buck_reads_its_mpn_with_no_description(tmp_path):
    ws = _ws(tmp_path, [("U4", "C9", _buck_pads("+12V"))],
             {"C9": _fixed_buck("AP63203WU-7", description="")})
    assert [v["net"] for v in _out_errs(_run(ws))] == ["+12V"]


def test_extraction_with_no_mpn_uses_the_rows_for_a_fixed_buck(tmp_path):
    ws = _ws(tmp_path, [("U4", "C9", _buck_pads("+3V3"))],
             {"C9": _fixed_buck("", description="")},
             [{"lcsc": "C9", "mpn": "AP63205WU-7", "attributes": []}])
    assert [v["net"] for v in _out_errs(_run(ws))] == ["+3V3"]


@pytest.mark.parametrize("mpn,over", [
    # an adjustable part's FB sits on a divider, whatever else is said
    ("AP63200WU-7", {"description": "buck, adjustable output"}),
    ("AP63201WU-7", {"description": "", "output_voltage": 5.0}),
    ("TPS54560BDDAR", {"description": "", "output_voltage": 5.0}),
    ("LM5164DDAR", {"description": "", "output_voltage": 5.0}),
    ("LMR33630ADDAR", {"description": "fixed 5.0V output"}),
])
@pytest.mark.parametrize("fb", ["+3V3", "FB_DIV"])
def test_adjustable_parts_fb_stays_out(tmp_path, mpn, over, fb):
    """No fixed-output claim is taken from an adjustable part. (AP63200's FB
    straight on +3V3 is judged from its Vref since #832: tested below.)"""
    if mpn == "AP63200WU-7" and fb == "+3V3":
        pytest.skip("judged by its Vref (#832)")
    ws = _ws(tmp_path, [("U4", "C9", _buck_pads(fb))],
             {"C9": _fixed_buck(mpn, **over)})
    p = _run(ws)
    assert _out_errs(p) == [] and _unjudged(p) == []


# ---------------------------------- adjustable: Vout from the FB divider (#832)

ADJ = {"description": "buck, adjustable output"}


def _adj_ws(tmp_path, out, top="100k", bot="32k", mpn="AP63200WU-7",
            bom=None, **over):
    """AP63200 (Vref 0.8 V) with top R1 out->FB and bottom R2 FB->GND:
    100k / 32k makes 0.8 * (1 + 100/32) = 3.3 V."""
    fps = [("U4", "C9", _buck_pads("FB_DIV")),
           ("R1", "C1", {"1": out, "2": "FB_DIV"}, "R_0402", top),
           ("R2", "C2", {"1": "FB_DIV", "2": "GND"}, "R_0402", bot)]
    return _ws(tmp_path, fps, {"C9": _fixed_buck(mpn, **{**ADJ, **over})},
               bom)


def test_adjustable_divider_making_its_rail_passes(tmp_path):
    p = _run(_adj_ws(tmp_path, "+3V3"))
    assert _out_errs(p) == [] and _unjudged(p) == []


def test_adjustable_divider_making_the_wrong_rail_blocks(tmp_path):
    errs = _out_errs(_run(_adj_ws(tmp_path, "+5V")))
    assert [(e["refs"], e["net"], e["pin"]) for e in errs] == \
        [(["U4"], "+5V", "U4.1")]
    assert errs[0]["net_v"] == 5.0
    assert errs[0]["limit_v"] == pytest.approx(3.3, abs=0.01)
    assert "R1 100k / R2 32k" in errs[0]["msg"]


def test_adjustable_divider_checked_against_the_block_vout(tmp_path):
    """The block's stated vout_v is a claim too: a divider on an unnamed net
    that makes 3.3 V under a "5 V" block is flagged."""
    ws = _adj_ws(tmp_path, "VOUT_A")
    c = tmp_path / "constraints.json"
    blk = {"name": "U4 AP63200 5 V", "operating_point": {"vout_v": 5.0}}
    c.write_text(json.dumps({"blocks": [blk]}))
    errs = _out_errs(_run(ws, c))
    assert len(errs) == 1 and "vout_v = 5" in errs[0]["msg"]
    blk["operating_point"]["vout_v"] = 3.3
    c.write_text(json.dumps({"blocks": [blk]}))
    assert _out_errs(_run(ws, c)) == []


def test_adjustable_divider_window_takes_the_resistor_tolerance(tmp_path):
    """3.5 V is outside 1 % resistors' window around 3.3 V (3.20-3.40),
    inside 5 %'s (3.0-3.6)."""
    tight = _ws(tmp_path / "a", [("U4", "C9", _buck_pads("FB_DIV")),
                                 ("R1", "C1", {"1": "+3V5", "2": "FB_DIV"},
                                  "R_0402", "100k"),
                                 ("R2", "C2", {"1": "FB_DIV", "2": "GND"},
                                  "R_0402", "32k")],
                {"C9": _fixed_buck("AP63200WU-7", **ADJ)})
    assert len(_out_errs(_run(tight))) == 1
    bom = [{"lcsc": c, "mpn": "RES", "attributes": [
        {"name": "Tolerance", "value": "±5%"}]} for c in ("C1", "C2")]
    loose = _ws(tmp_path / "b", [("U4", "C9", _buck_pads("FB_DIV")),
                                 ("R1", "C1", {"1": "+3V5", "2": "FB_DIV"},
                                  "R_0402", "100k"),
                                 ("R2", "C2", {"1": "FB_DIV", "2": "GND"},
                                  "R_0402", "32k")],
                {"C9": _fixed_buck("AP63200WU-7", **ADJ)}, bom)
    assert _out_errs(_run(loose)) == []


def test_adjustable_divider_with_no_vref_says_so(tmp_path):
    """No Vref row: the warning names the divider and the missing row."""
    p = _run(_adj_ws(tmp_path, "+5V", mpn="ZZ9999"))
    assert _out_errs(p) == []
    u = _unjudged(p)
    assert len(u) == 1 and "R1 100k / R2 32k" in u[0]["msg"]
    assert "regulator_vref.yaml" in u[0]["msg"]


@pytest.mark.parametrize("mpn, top, bot, rail, other", [
    ("TPS56637RPAR", "73.2k", "10k", "+5V", "+3V3"),    # 0.6 x 8.32 = 4.99 V
    ("TLV62595DMQR", "453k", "100k", "+3V3", "+5V")])   # 0.6 x 5.53 = 3.32 V
def test_verified_ti_vref_rows_judge_known_dividers(tmp_path, mpn, top, bot,
                                                    rail, other):
    """Known answer: a voice board's U102 TPS56637 and U201 TLV62595 dividers
    make their rails from the datasheets' 0.6 V VFB, judged on verified rows
    (before the rows, both outputs were left unjudged), and the same divider
    on the other rail blocks."""
    p = _run(_adj_ws(tmp_path / "ok", rail, top, bot, mpn))
    assert _out_errs(p) == [] and _unjudged(p) == []
    errs = _out_errs(_run(_adj_ws(tmp_path / "bad", other, top, bot, mpn)))
    assert len(errs) == 1 and errs[0]["net"] == other
    assert "Vref 0.6 V" in errs[0]["msg"]
    assert "unverified" not in errs[0]["msg"]


@pytest.mark.parametrize("mpn, top, bot, rail, worst, errs", [
    ("TPS56637RPAR", "73.2k", "10k", "+5V", 5.25, 0),    # 4.99 V (4.83-5.16)
    ("TLV62595DMQR", "453k", "100k", "+3V3", 3.47, 0),   # 3.32 V (3.23-3.41)
    ("TLV62595DMQR", "453k", "100k", "+3V3", 3.32, 0),   # a set point fits
    ("TLV62595DMQR", "453k", "100k", "+5V", 5.25, 1),    # 3.32 V isn't +5V
    ("TPS56637RPAR", "73.2k", "10k", "+5V", 5.6, 1),     # > 10 % over 4.99
    ("TPS56637RPAR", "73.2k", "10k", "+5V", 4.75, 1)])   # under the window
def test_constraints_voltage_is_the_rails_worst_case(tmp_path, mpn, top, bot,
                                                     rail, worst, errs):
    """constraints `voltages` give a rail's worst case, not its set point
    (constraints_schema.md): a voice board's +5V at 5.25 V over U102's 4.99 V
    divider fits, as it would over a fixed 5.0 V part; a worst case more than
    10 % over the divider's nominal, or under its window, still blocks."""
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"voltages": [{"net": rail, "voltage": worst}]}))
    found = _out_errs(_run(_adj_ws(tmp_path, rail, top, bot, mpn), c))
    assert len(found) == errs
    if errs:
        assert f"a worst case of {worst:g} V" in found[0]["msg"]


def test_power_tree_voltage_is_still_a_set_point(tmp_path):
    """Only a constraints voltage is a worst case: the power tree's 5.25 V
    over the same 4.99 V divider is a set point outside its window."""
    tree = "| Rail | Nom V |\n|---|---|\n| /V5 | 5.25 |\n"
    ws = _ws(tmp_path, [("U4", "C9", _buck_pads("FB_DIV")),
                        ("R1", "C1", {"1": "/V5", "2": "FB_DIV"}, "R_0402",
                         "73.2k"),
                        ("R2", "C2", {"1": "FB_DIV", "2": "GND"}, "R_0402",
                         "10k")],
             {"C9": _fixed_buck("TPS56637RPAR", **ADJ)}, tree=tree)
    errs = _out_errs(_run(ws))
    assert [e["net"] for e in errs] == ["/V5"]
    assert "/V5 = 5.25 V" in errs[0]["msg"]


def test_fixed_part_with_a_vref_row_keeps_its_fixed_judgement(tmp_path):
    """AP63205 matches the AP6320x Vref row too, but its FB on its own rail
    is a fixed output, not a 0.8 V divider tap."""
    ws = _ws(tmp_path, [("U4", "C9", _buck_pads("+5V"))],
             {"C9": _fixed_buck()})
    p = _run(ws)
    assert _out_errs(p) == [] and _unjudged(p) == []


def test_adjustable_fb_straight_on_a_rail_makes_vref(tmp_path):
    ws = _ws(tmp_path, [("U4", "C9", _buck_pads("+3V3"))],
             {"C9": _fixed_buck("AP63200WU-7", **ADJ)})
    errs = _out_errs(_run(ws))
    assert [(e["net"], e["limit_v"]) for e in errs] == [("+3V3", 0.8)]


def test_lite_main_dividers_judge_clean(tmp_path):
    """Known answer (#832): lite-main's U2 TPS54302 R9 100k / R10 13k makes
    5.18 V and U3 SY8089 R11 150k / R12 33k makes 3.33 V, as stated. Run on
    a copy: the check writes the workspace's ledger, and the pin is hashed."""
    import shutil
    ws = tmp_path / "lite-main"
    for d in ("kicad", "parts", "architecture"):
        shutil.copytree(ROOT / "pinned" / "lite-main" / d, ws / d)
    p, _ = cr.run(["--pcb", str(ws / "kicad" / "lite-main.kicad_pcb"),
                   "--constraints",
                   str(ws / "architecture" / "constraints.json")])
    bad = [v for v in p["violations"]
           if v["kind"].startswith("rating_regulator_output")
           and set(v.get("refs") or []) & {"U2", "U3"}]
    assert bad == []


def test_fixed_buck_fb_on_a_net_with_no_stated_rail_is_not_judged(tmp_path):
    ws = _ws(tmp_path, [("U4", "C9", _buck_pads("FB_SENSE"))],
             {"C9": _fixed_buck()})
    assert _out_errs(_run(ws)) == []


def test_fixed_buck_number_on_a_non_regulator_ref_is_not_judged(tmp_path):
    ws = _ws(tmp_path, [("Q4", "C9", _buck_pads("+3V3"))],
             {"C9": _fixed_buck(description="")})
    assert _out_errs(_run(ws)) == []


def test_a_fixed_bucks_fb_does_not_name_its_rail(tmp_path):
    """FB counts as an output only for judging: an unnamed net's volts still
    don't come from the part's MPN."""
    ws = _ws(tmp_path, [("U4", "C9", _buck_pads("VSENSE"))],
             {"C9": _fixed_buck()})
    assert "VSENSE" not in _run(ws)["net_volts"]


def _unjudged(p):
    return [v for v in p["violations"]
            if v["kind"] == "rating_regulator_output_unjudged"]


AMS5 = _fixed_ldo("AMS1117-5.0")
LDO_ON_3V3 = {"1": "GND", "2": "+3V3", "3": "+5V"}


def test_extracted_5v_ldo_on_a_3v3_rail_blocks(tmp_path):
    p = _run(_ws(tmp_path, [("U4", "C9", LDO_ON_3V3)], {"C9": AMS5}))
    errs = _out_errs(p)
    assert [(e["pin"], e["net"], e["net_v"]) for e in errs] == \
        [("U4.2", "+3V3", 3.3)]
    assert "AMS1117-5.0" in errs[0]["msg"]
    assert len(_errors(p)) == 1


def test_extracted_3v3_ldo_on_a_3v3_rail_passes(tmp_path):
    ws = _ws(tmp_path, [("U4", "C9", LDO_ON_3V3)],
             {"C9": _fixed_ldo("AMS1117-3.3")})
    assert _errors(_run(ws)) == []


def test_a_cap_on_the_rail_is_judged_at_the_rail_not_the_mpn(tmp_path):
    """The MPN's 5 V must not replace the board's 3.3 V: a 3.3 V cap on +3V3
    holds, whatever wrong part drives it, and only the LDO is flagged."""
    bom = [{"lcsc": "C7", "mpn": "CAP",
            "attributes": [{"name": "Voltage Rated", "value": "3.3V"}]}]
    ws = _ws(tmp_path, [("U4", "C9", LDO_ON_3V3),
                        ("C1", "C7", {"1": "+3V3", "2": "GND"})],
             {"C9": AMS5}, bom)
    p = _run(ws)
    assert p["net_volts"]["+3V3"] == {"v": 3.3, "src": "net name"}
    assert [e["kind"] for e in _errors(p)] == ["rating_regulator_output"]


def test_extracted_output_is_judged_against_constraints_and_power_tree(
        tmp_path):
    tree = "| Rail | Nom V |\n|---|---|\n| /VA | 3.3 |\n"
    pads = {"1": "GND", "2": "/VA", "3": "+5V"}
    p = _run(_ws(tmp_path, [("U4", "C9", pads)], {"C9": AMS5}, tree=tree))
    assert [e["net"] for e in _out_errs(p)] == ["/VA"]
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"voltages": [{"net": "+3V3", "voltage": 5}]}))
    p = _run(_ws(tmp_path / "b", [("U4", "C9", LDO_ON_3V3)], {"C9": AMS5}), c)
    assert _errors(p) == []  # constraints say +3V3 is really 5 V here


@pytest.mark.parametrize("mpn,rail,errs", [
    ("AMS1117-5.0", "+5V", 0),
    ("AMS1117-3.3", "+3V0", 0),   # exactly 10 % low
    ("AMS1117-5.0", "+4V4", 1),   # just past it
    ("AMS1117-5.0", "+5V6", 1),
    ("ADP7182-5.0", "-5V", 0),    # a negative part: the MPN has no sign
    ("ADP7182-5.0", "-3V3", 1),
])
def test_extracted_output_tolerance(tmp_path, mpn, rail, errs):
    pads = {"1": "GND", "2": rail, "3": "+12V"}
    ws = _ws(tmp_path, [("U4", "C9", pads)], {"C9": _fixed_ldo(mpn)})
    assert len(_out_errs(_run(ws))) == errs


def test_unnamed_rail_takes_the_mpn_and_is_not_judged_by_it(tmp_path):
    """Nothing but the MPN says what /RAIL is: it fills the net, and a second
    regulator isn't judged against the first one's MPN."""
    pads = {"1": "GND", "2": "/RAIL", "3": "+12V"}
    ws = _ws(tmp_path, [("U4", "C9", pads), ("U5", "C8", pads)],
             {"C9": AMS5, "C8": _fixed_ldo("AMS1117-3.3")})
    p = _run(ws)
    assert p["net_volts"]["/RAIL"] == {"v": 5.0, "src": "regulator U4"}
    assert _out_errs(p) == []


def test_extracted_output_read_from_the_row_when_the_mpn_has_none(tmp_path):
    """TLV75733-style: no decimal in the MPN, the row says 3.3 V."""
    bom = [{"lcsc": "C9", "mpn": "TLV75733PDBVR", "description": "LDO regulator",
            "attributes": [{"name": "Output Voltage", "value": "3.3V"}]}]
    pads = {"1": "GND", "2": "+5V", "3": "+12V"}
    p = _run(_ws(tmp_path, [("U4", "C9", pads)],
                 {"C9": _fixed_ldo("TLV75733PDBVR")}, bom))
    assert [e["net"] for e in _out_errs(p)] == ["+5V"]
    assert "3.3V" in _out_errs(p)[0]["rating"]


@pytest.mark.parametrize("ref,mpn,row,ptype,rail", [
    ("U4", "ACME1234", {}, "power_out", "+5V"),        # no output known
    ("U4", "AMS1117-3.3", {}, "output", "+5V"),        # not a power output
    ("Q4", "AMS1117-3.3", {}, "power_out", "+5V"),     # not a regulator's ref
    ("U4", "EFUSE1",                                   # not called a regulator
     {"description": "eFuse",
      "attributes": [{"name": "Output Voltage", "value": "3.3V"}]},
     "power_out", "+5V"),
])
def test_extracted_parts_whose_output_is_not_judged(
        tmp_path, ref, mpn, row, ptype, rail):
    ext = _fixed_ldo(mpn)
    ext["pinout"][1]["type"] = ptype
    bom = [dict(row, lcsc="C9", mpn=mpn)] if row else None
    ws = _ws(tmp_path, [(ref, "C9", {"1": "GND", "2": rail, "3": "+12V"})],
             {"C9": ext}, bom)
    assert _out_errs(_run(ws)) == []


def test_bare_buck_stays_unknown_and_names_no_rail(tmp_path):
    """As for a code: is_regulator and reg_out_volts stay strict (#763)."""
    bom = [{"lcsc": "C3", "mpn": "AP63205WU-7", "attributes": []}]
    pads = {"1": "+24V", "2": "GND", "3": "/RAIL"}
    ws = _ws(tmp_path, [("U4", "C3", pads)], {}, bom)
    p = _run(ws)
    assert [v["refs"] for v in _no_attrs(p)] == [["U4"]]
    assert "/RAIL" not in p["net_volts"]
    assert not cr.is_regulator({"ref": "U4"}, bom[0])


def test_bare_buck_blocks_once_per_bom_code(tmp_path):
    """linux-computer U302-U304: three parts, one row to fill (#763)."""
    bom = [{"lcsc": "C3", "mpn": "TLV62569PDDCR", "attributes": [],
            "role": "5 V -> +3V3"}]
    board = [(f"U{n}", "C3", {"1": "+5V", "2": "GND", "3": "+3V3"})
             for n in (302, 303, 304)]
    found = _no_attrs(_run(_ws(tmp_path, board, {}, bom)))
    assert [v["refs"] for v in found] == [["U302", "U303", "U304"]]


@pytest.mark.parametrize("mpn,volts", [
    ("RT9080-33GJ5", 3.3), ("RT9013-18GB", 1.8), ("RT9193-50GB", 5.0),
    ("TLV75733PDBVR", 3.3), ("TLV70018DSER", 1.8), ("TPS78233DDCR", 3.3),
    ("TPS7A0233PDBVR", 3.3), ("TPS7B6950QDCYRQ1", 5.0),
    ("TPS7A2045PDBVR", 4.5),   # TPS7A20 datasheet rev H, Table 8-1
    ("XC6206P332MR", 3.3), ("XC6206P302MR", 3.0),
    ("HT7333-A", 3.3), ("HT7850", 5.0), ("ME6211C33M5G", 3.3),
    # a whole part number, not a tenths code (Diodes AP63200 datasheet, p. 1)
    ("AP63203WU-7", 3.3), ("AP63205WU-7", 5.0),
])
def test_output_code_in_a_known_regulator_family(mpn, volts):
    assert cr.reg_out_code(mpn) == volts


@pytest.mark.parametrize("mpn", [
    "TPS54233DR", "TPS563201DDCR", "TPS61023DRLR",   # not an LDO family
    "TLV7031DBVR",                                   # comparator
    "TPS79601DCQR", "TPS7A4701RGWR", "TLV75801PDRVR",  # 01: adjustable
    "TPS793285DBVR",                                 # 2.85 V: not a two-digit code
    "RT9711ACBG", "HT1621B", "ME2108A33M3G",         # no code, or not this family
    "AMS1117", "AP63200WU-7", "AP63201WU-7", "",     # AP6320[01]: adjustable
])
def test_no_output_code_is_a_guess(mpn):
    assert cr.reg_out_code(mpn) is None


@pytest.mark.parametrize("mpn", ["RT9080-33GJ5", "TLV75733PDBVR"])
@pytest.mark.parametrize("rail,errs", [
    ("+5V0", 1), ("+5V", 1), ("+3V3", 0), ("+3V0", 0), ("+2V5", 1)])
def test_extracted_output_read_from_the_mpns_code(tmp_path, mpn, rail, errs):
    """No row at all and no voltage in the MPN's usual forms (#729)."""
    pads = {"1": "GND", "2": rail, "3": "+12V"}
    p = _run(_ws(tmp_path, [("U4", "C9", pads)], {"C9": _fixed_ldo(mpn)}))
    found = _out_errs(p)
    assert [e["net"] for e in found] == ([rail] if errs else [])
    assert all(mpn in e["rating"] for e in found)
    assert _unjudged(p) == []


@pytest.mark.parametrize("extra", [
    {"output_voltage": 3.3},
    {"description": "1 A low-dropout LDO, fixed 3.3V output"},
    {"description": "Low-noise LDO, 3.3 V fixed"},
])
@pytest.mark.parametrize("rail,errs", [("+5V", 1), ("+3V3", 0)])
def test_extracted_output_read_from_the_extraction(tmp_path, extra, rail, errs):
    ext = dict(_fixed_ldo("ACME1234"), **extra)
    pads = {"1": "GND", "2": rail, "3": "+12V"}
    p = _run(_ws(tmp_path, [("U4", "C9", pads)], {"C9": ext}))
    assert [e["net"] for e in _out_errs(p)] == ([rail] if errs else [])
    assert _unjudged(p) == []
    if errs:
        assert "extraction" in _out_errs(p)[0]["rating"]


def test_extractions_output_field_beats_the_mpns_code(tmp_path):
    ext = dict(_fixed_ldo("TLV75733PDBVR"), output_voltage=5.0)
    pads = {"1": "GND", "2": "+5V", "3": "+12V"}
    assert _out_errs(_run(_ws(tmp_path, [("U4", "C9", pads)], {"C9": ext}))) == []


def test_extracted_description_that_also_says_adjustable_is_no_voltage(tmp_path):
    ext = dict(_fixed_ldo("ACME1234"),
               description="LDO, adjustable or fixed 3.3V output")
    pads = {"1": "GND", "2": "+5V", "3": "+12V"}
    p = _run(_ws(tmp_path, [("U4", "C9", pads)], {"C9": ext}))
    assert _out_errs(p) == [] and len(_unjudged(p)) == 1


@pytest.mark.parametrize("words", [
    {"ext": {"description": "Adjustable LDO regulator"}},
    {"row": {"description": "LDO regulator"}},
    {"row": {"role": "12 V to 5 V regulator"}},
])
def test_extracted_regulator_with_no_known_output_warns(tmp_path, words):
    ext = dict(_fixed_ldo("ACME1234"), **words.get("ext", {}))
    bom = [dict(words["row"], lcsc="C9", mpn="ACME1234")] \
        if "row" in words else None
    pads = {"1": "GND", "2": "+5V", "3": "+12V"}
    p = _run(_ws(tmp_path, [("U4", "C9", pads)], {"C9": ext}, bom))
    assert _out_errs(p) == []
    got = _unjudged(p)
    assert len(got) == 1 and got[0]["severity"] == "warning"
    assert got[0]["refs"] == ["U4"] and got[0]["net"] == "+5V"
    assert "wasn't judged" in got[0]["msg"] and "ACME1234" in got[0]["msg"]


@pytest.mark.parametrize("ref,ext_words,row_words,ptype", [
    # nothing calls these regulators, or something says they aren't one
    ("U4", {}, None, "power_out"),
    ("U4", {"description": "60 V eFuse"},
     {"role": "4.5 V to 60 V eFuse / load switch"}, "power_out"),
    ("U4", {"description": "LDO with load switch"}, None, "power_out"),
    ("U4", {"description": "Li-ion charger with power path, DC-DC"}, None,
     "power_out"),
    ("U4", {"description": "Precision voltage reference"},
     {"role": "regulator"}, "power_out"),
    # a USB bridge's "to 3.3V" is no regulator's words, in an extraction
    ("U4", {"description": "USB to 3.3V UART bridge"}, None, "power_out"),
    ("Q4", {"description": "LDO regulator"}, None, "power_out"),
    ("U4", {"description": "LDO regulator"}, None, "output"),
])
def test_extracted_parts_that_dont_warn(tmp_path, ref, ext_words, row_words,
                                        ptype):
    ext = dict(_fixed_ldo("ACME1234"), **ext_words)
    ext["pinout"][1]["type"] = ptype
    bom = [dict(row_words, lcsc="C9", mpn="ACME1234")] if row_words else None
    pads = {"1": "GND", "2": "+5V", "3": "+12V"}
    p = _run(_ws(tmp_path, [(ref, "C9", pads)], {"C9": ext}, bom))
    assert _unjudged(p) == [] and _out_errs(p) == []


def test_unjudged_warning_needs_the_output_pin_on_the_board(tmp_path):
    ext = dict(_fixed_ldo("ACME1234"), description="LDO regulator")
    pads = {"1": "GND", "3": "+12V"}
    assert _unjudged(_run(_ws(tmp_path, [("U4", "C9", pads)], {"C9": ext}))) == []


def test_extraction_schema_takes_an_output_voltage():
    field = datasheet_extract.DATASHEET_SCHEMA["properties"]["output_voltage"]
    assert field["type"] == "number" and field["exclusiveMinimum"] == 0


def test_a_bad_output_voltage_in_an_extraction_is_no_voltage(tmp_path):
    ext = dict(_fixed_ldo("ACME1234"), output_voltage="3.3 V",
               description="LDO regulator")
    pads = {"1": "GND", "2": "+5V", "3": "+12V"}
    p = _run(_ws(tmp_path, [("U4", "C9", pads)], {"C9": ext}))
    assert _out_errs(p) == [] and len(_unjudged(p)) == 1


def test_wrong_output_fails_an_extracted_regulators_ledger_fact(tmp_path):
    ws = _ws(tmp_path, [("U4", "C9", LDO_ON_3V3)], {"C9": AMS5})
    _, got = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                          None, None)
    rating = {f["subject"]: f for f in got["ledger_facts"]
              if f["kind"] == "rating"}
    assert rating["U4"]["status"] == "failed"
    assert "+3V3" in rating["U4"]["value"]


# #744: a filled row and no extraction, its voltage only in the MPN's code.
CODED_ATTRS = {"Input Voltage": "6V", "Output Current": "200mA"}
CODED_NAMES = {"1": "IN", "3": "OUT"}


@pytest.mark.parametrize("mpn,rail,errs", [
    ("TLV75733PDBVR", "+1V8", 1),   # the issue's known answer
    ("TLV75733PDBVR", "+3V3", 0),
    ("TLV75733PDBVR", "+3V0", 0),   # exactly 10 % low
    ("TLV75733PDBVR", "+2V9", 1),   # just past it
    ("RT9080-33GJ5", "+1V8", 1),
    ("XC6206P332MR-G", "+3V3", 0),
])
@pytest.mark.parametrize("words", ["LDO regulator", ""])
def test_unextracted_output_from_the_mpn_code(tmp_path, mpn, rail, errs, words):
    """The code judges with or without the row's words saying regulator."""
    ws = _reg_ws(tmp_path, CODED_ATTRS, {"1": "+5V", "2": "GND", "3": rail},
                 CODED_NAMES, mpn=mpn, description=words)
    p = _run(ws)
    found = _out_errs(p)
    assert len(found) == errs
    assert _unjudged(p) == []
    if errs:
        assert "MPN code" in found[0]["rating"] and "U4" in found[0]["msg"]


def test_the_row_outranks_the_mpn_code(tmp_path):
    ws = _reg_ws(tmp_path, dict(CODED_ATTRS, **{"Output Voltage": "1.8V"}),
                 {"1": "+5V", "2": "GND", "3": "+1V8"}, CODED_NAMES,
                 mpn="TLV75733PDBVR")
    assert _out_errs(_run(ws)) == []


def test_a_code_gives_the_row_its_input_check_too(tmp_path):
    ws = _reg_ws(tmp_path, CODED_ATTRS, {"1": "+12V", "2": "GND", "3": "+3V3"},
                 CODED_NAMES, mpn="TLV75733PDBVR", description="")
    assert [v["kind"] for v in _errors(_run(ws))] == ["rating_over_voltage"]


@pytest.mark.parametrize("mpn", [
    "TPS2051B",     # a load switch
    "TLV7031",      # a comparator
    "TPS7A0201",    # the family's adjustable code
])
def test_an_mpn_with_no_code_and_no_words_is_no_regulator(tmp_path, mpn):
    ws = _reg_ws(tmp_path, CODED_ATTRS, {"1": "+5V", "2": "GND", "3": "+1V8"},
                 CODED_NAMES, mpn=mpn, description="")
    p = _run(ws)
    assert _out_errs(p) == [] and _unjudged(p) == []


BUCK_ATTRS = {"Input Voltage": "4.5V~32V", "Output Current": "2A"}
BUCK_NAMES = {"1": "VIN", "3": "FB"}


@pytest.mark.parametrize("mpn,vin,rail,kinds", [
    ("AP63205WU-7", "3.8V~32V", "+5V", []),
    ("AP63205WU-7", "3.8V~32V", "+3V3", ["rating_regulator_output"]),
    ("AP63203WU-7", "3.8V~32V", "+3V3", []),
    ("AP63203WU-7", "3.8V~32V", "+5V", ["rating_regulator_output"]),
    ("AP63205WU-7", "3.8V~18V", "+5V", ["rating_over_voltage"]),
    # adjustable, FB straight on +3V3: it makes its 0.6 V Vref (#832)
    ("TLV62569PDDCR", "2.5V~5.5V", "+3V3", ["rating_over_voltage",
                                            "rating_regulator_output"]),
    ("TPS54560BDDAR", "4.5V~60V", "+5V", []),
])
def test_filled_buck_is_compared(tmp_path, mpn, vin, rail, kinds):
    """A fixed part's FB is its output (the AP63205 datasheet's VFB is 4.95-5.05
    V), so its rail is judged by what it makes; the row's limit judges VIN."""
    ws = _reg_ws(tmp_path, {"Input Voltage": vin, "Output Current": "2A"},
                 {"1": "+24V", "2": "GND", "3": rail}, BUCK_NAMES, mpn=mpn,
                 description="")
    p = _run(ws)
    assert _no_attrs(p) == []
    assert [v["kind"] for v in _errors(p)] == kinds


@pytest.mark.parametrize("mpn", ["TPS54560BDDAR", "AP63200WU-7"])
def test_an_adjustable_bucks_fb_is_not_its_output(tmp_path, mpn):
    """An adjustable part's FB sits on a divider, not on the rail: a stated
    Output Voltage says nothing about it."""
    ws = _reg_ws(tmp_path, {"Input Voltage": "4.5V~32V",
                            "Output Voltage": "5V"},
                 {"1": "+12V", "2": "GND", "3": "+3V3"}, BUCK_NAMES, mpn=mpn,
                 description="")
    assert _out_errs(_run(ws)) == []


def test_filled_adjustable_buck_says_its_output_is_unjudged(tmp_path):
    ws = _reg_ws(tmp_path, BUCK_ATTRS, {"1": "+12V", "2": "GND", "3": "/FB"},
                 BUCK_NAMES, mpn="TPS54560BDDAR", description="")
    p = _run(ws)
    assert _errors(p) == [] and len(_unjudged(p)) == 1


def test_code_does_not_name_the_rail(tmp_path):
    """reg_out_volts stays strict: the code judges, it never sets a net."""
    ws = _reg_ws(tmp_path, CODED_ATTRS, {"1": "+5V", "2": "GND", "3": "/RAIL"},
                 CODED_NAMES, mpn="TLV75733PDBVR")
    p = _run(ws)
    assert "/RAIL" not in p["net_volts"] and _errors(p) == []


def test_unextracted_regulator_nothing_gives_an_output_for_warns(tmp_path):
    ws = _reg_ws(tmp_path, {"Voltage - Supply": "6V"},
                 {"1": "+5V", "2": "GND", "3": "+1V8"}, CODED_NAMES,
                 mpn="TPS54302DDCR", description="buck converter")
    p = _run(ws)
    assert [v["net"] for v in _unjudged(p)] == ["+1V8"]
    assert _unjudged(p)[0]["severity"] == "warning"
    assert "U4" in _unjudged(p)[0]["msg"] and _errors(p) == []


def test_unextracted_unjudged_warning_with_no_output_pad_named(tmp_path):
    """No pad names, or none called an output: still no voltage from anywhere,
    so it says so rather than staying silent."""
    ws = _reg_ws(tmp_path, {"Voltage - Supply": "6V"},
                 {"1": "+5V", "2": "GND", "3": "/SW"},
                 mpn="SY8089A1AAC", description="buck converter")
    found = _unjudged(_run(ws))
    assert [v["net"] for v in found] == [None]
    assert "wasn't judged" in found[0]["msg"]


@pytest.mark.parametrize("attrs,row", [
    ({"Voltage - Supply": "6V", "Output Voltage": "3.3V"}, {}),   # the row says
    ({"Voltage - Supply": "6V"}, {"mpn": "AP2112-3.3"}),          # the MPN says
    ({"Voltage - Supply": "6V"}, {"mpn": "TLV75733PDBVR"}),       # the code says
    ({"Voltage - Supply": "6V"}, {"description": "load switch regulator"}),
    ({"Voltage - Supply": "6V"}, {"mpn": "XYZ", "description": "a thing"}),
])
def test_unextracted_parts_that_dont_warn(tmp_path, attrs, row):
    ws = _reg_ws(tmp_path, attrs, {"1": "+5V", "2": "GND", "3": "+3V3"},
                 CODED_NAMES, **row)
    assert _unjudged(_run(ws)) == []


@pytest.mark.parametrize("mpn,rail,errs", [
    ("AMS1117-5.0", "-5V", 1),     # positive family on a negative rail
    ("L7805CV", "-5V", 1),
    ("TLV75733PDBVR", "-3V3", 1),
    ("ADP7182-5.0", "+5V", 1),     # negative family on a positive rail
    ("TPS72325DBVR", "+2V5", 1),   # coded like a positive TI part, but negative
    ("TPS72325DBVR", "-2V5", 0),
    ("LT1964-5.0", "-5V", 0),
    ("MC7905-5.0", "-5V", 0),
    ("LT9999-5.0", "-5V", 0),      # neither: no false block
    ("LT9999-5.0", "+5V", 0),
])
def test_extracted_output_sign(tmp_path, mpn, rail, errs):
    pads = {"1": "GND", "2": rail, "3": "+12V"}
    ws = _ws(tmp_path, [("U4", "C9", pads)], {"C9": _fixed_ldo(mpn)})
    found = _out_errs(_run(ws))
    assert len(found) == errs
    if errs:
        assert "positive" in found[0]["msg"] or "negative" in found[0]["msg"]


@pytest.mark.parametrize("text,rail,errs", [
    ("Positive LDO regulator", "-5V", 1),
    ("Negative LDO regulator", "-5V", 0),
    ("Negative LDO regulator", "+5V", 1),
    ("Positive and negative dual regulator", "-5V", 0),
    ("Positive and negative dual regulator", "+5V", 0),
])
def test_the_words_tell_the_sign(tmp_path, text, rail, errs):
    ext = dict(_fixed_ldo("LT9999-5.0"), description=text)
    pads = {"1": "GND", "2": rail, "3": "+12V"}
    assert len(_out_errs(_run(_ws(tmp_path / "e", [("U4", "C9", pads)],
                                  {"C9": ext})))) == errs
    ws = _reg_ws(tmp_path / "r", {"Voltage - Supply": "15V"},
                 {"1": "+12V", "2": "GND", "3": rail}, CODED_NAMES,
                 mpn="LT9999-5.0", description=text)
    assert len(_out_errs(_run(ws))) == errs


@pytest.mark.parametrize("attr,rail,errs", [
    ({"Output Configuration": "Positive"}, "-5V", 1),
    ({"Output Configuration": "Negative"}, "-5V", 0),
    ({"Output Voltage": "-5V"}, "-5V", 0),
    ({"Output Voltage": "-5V"}, "+5V", 1),
])
def test_the_row_attributes_tell_the_sign(tmp_path, attr, rail, errs):
    ws = _reg_ws(tmp_path, dict({"Voltage - Supply": "15V"}, **attr),
                 {"1": "+12V", "2": "GND", "3": rail}, CODED_NAMES,
                 mpn="LT9999-5.0", description="LDO regulator")
    assert len(_out_errs(_run(ws))) == errs


def test_a_floating_positive_part_is_not_called_wrong(tmp_path):
    """No pad on ground: its own ground is at another rail, so a positive part
    can sit on a net below board ground on purpose."""
    pads = {"1": "-12V", "2": "-5V", "3": "+12V"}
    ws = _ws(tmp_path, [("U4", "C9", pads)], {"C9": AMS5})
    assert _out_errs(_run(ws)) == []


def test_sign_is_not_judged_on_a_rail_the_mpn_named(tmp_path):
    """/RAIL = +5 here only because the MPN's 5.0 filled it, with no sign: that
    can't say a negative part is on a positive rail."""
    ws = _reg_ws(tmp_path, {"Voltage - Supply": "15V"},
                 {"1": "+12V", "2": "GND", "3": "/RAIL"}, CODED_NAMES,
                 mpn="ADP7182-5.0")
    assert _errors(_run(ws)) == []


# ---------------------------------------------- suspect datasheets (#706)

def _suspect_ws(tmp_path, index):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "2": "GND", "3": "+3V3"})],
             {"C1": MCU})
    d = ws / "parts" / "datasheets"
    d.mkdir()
    if index is not None:
        (d / "index.json").write_text(json.dumps({"C1": index}))
    return ws


def _u1(p, kind):
    return next(f for f in p["ledger_facts"] if f["subject"] == "U1"
                and f["kind"] == kind)


def test_suspect_datasheet_facts_are_not_verified(tmp_path):
    ws = _suspect_ws(tmp_path, {"status": "suspect", "reasons": [
        "MPN TESTMCU not in the text of pages 1-2"]})
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb",
                               ws / "parts", None, None)
    for kind in ("rating", "pinout"):
        f = _u1(facts, kind)
        assert f["status"] == "unknown" and "suspect" in f["value"], f
        assert f["source"] == ""
    w = [v for v in viol if v["kind"] == "rating_datasheet_suspect"]
    assert len(w) == 1 and w[0]["severity"] == "warning", viol
    assert "TESTMCU not in the text" in w[0]["msg"] and w[0]["refs"] == ["U1"]
    assert not _errors({"violations": viol})


def test_a_scan_is_not_judged_not_suspect_and_its_facts_wait(tmp_path):
    # a03cb, hwde-voice-faults: 4 of fmcw-radar's 7 suspect sheets were scans
    from test_datasheet_fetch import NOTEXT
    ws = _suspect_ws(tmp_path, {"url": "u", "sha256": "s", "mpn": "TESTMCU"})
    (ws / "parts" / "datasheets" / "C1.pdf").write_bytes(NOTEXT)
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb",
                               ws / "parts", None, None)
    assert not [v for v in viol if v["kind"] == "rating_datasheet_suspect"]
    assert facts["suspect_datasheets"] == {}
    w = [v for v in viol if v["kind"] == "rating_datasheet_unassessed"]
    assert len(w) == 1 and "scan" in w[0]["msg"], viol
    assert facts["unassessed_datasheets"] == ["C1"]
    for kind in ("rating", "pinout"):
        f = _u1(facts, kind)
        assert f["status"] == "unknown", f
        assert f["value"].startswith("datasheet not judged, not verified"), f


def test_a_sheet_that_prints_no_part_number_is_not_judged_too(tmp_path):
    # a03cb, hwde-voice-faults: the other 3 were sheets naming no part number
    from test_datasheet_fetch import KENTO_SHEET, _pdf
    ws = _suspect_ws(tmp_path, {"url": "u", "sha256": "s", "mpn": "TESTMCU"})
    (ws / "parts" / "datasheets" / "C1.pdf").write_bytes(
        _pdf([KENTO_SHEET, KENTO_SHEET]))
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb",
                               ws / "parts", None, None)
    assert not [v for v in viol if v["kind"] == "rating_datasheet_suspect"]
    assert facts["unassessed_datasheets"] == ["C1"]
    f = _u1(facts, "rating")
    assert f["status"] == "unknown", f
    assert f["value"].startswith("datasheet not judged, not verified"), f
    assert cr._doubt("MPN not printed: pages 1-2 ...") == "not judged"
    assert cr._doubt("MPN X is not in the text ...") == "suspect"


def test_ok_datasheet_index_still_verifies(tmp_path):
    ws = _suspect_ws(tmp_path, {"status": "ok", "reasons": []})
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb",
                               ws / "parts", None, None)
    assert _u1(facts, "rating")["status"] == "verified"
    assert _u1(facts, "pinout")["status"] == "verified"
    assert not [v for v in viol if v["kind"] == "rating_datasheet_suspect"]


def test_notice_pdf_on_disk_is_suspect_even_without_an_index(tmp_path):
    from test_datasheet_fetch import NOTICE
    ws = _suspect_ws(tmp_path, None)
    (ws / "parts" / "datasheets" / "C1.pdf").write_bytes(NOTICE)
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb",
                               ws / "parts", None, None)
    assert _u1(facts, "rating")["status"] == "unknown"
    assert [v for v in viol if v["kind"] == "rating_datasheet_suspect"]


# ------------------------------ where a rating's number came from (#781)

CAP50 = {"lcsc": "C9", "mpn": "CAP", "attributes": [
    {"name": "Voltage Rated", "value": "50V"}]}
RES = {"lcsc": "C8", "mpn": "RES", "attributes": [
    {"name": "Resistance", "value": "10k"}]}
DIODE = {"lcsc": "C7", "mpn": "D", "attributes": [
    {"name": "Reverse Voltage (Vr)", "value": "40V"}]}
FUSE = {"lcsc": "C6", "mpn": "0881002", "attributes": [
    {"name": "Current Rating", "value": "2A"},
    {"name": "Voltage Rating (DC)", "value": "63V"}]}


def _fact(ws, ref, kind="rating"):
    _, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                            None, None)
    return next(f for f in facts["ledger_facts"]
                if f["subject"] == ref and f["kind"] == kind)


def _index(ws, **entries):
    d = ws / "parts" / "datasheets"
    d.mkdir(exist_ok=True)
    (d / "index.json").write_text(json.dumps(entries))


BAD = {"status": "suspect", "reasons": ["no text layer"]}
GOOD = {"status": "ok", "reasons": []}


def test_attribute_rating_is_listed_and_says_distributor(tmp_path):
    ws = _ws(tmp_path, [("C1", "C9", {"1": "+5V", "2": "GND"})], {}, [CAP50])
    f = _fact(ws, "C1")
    assert f["status"] == "listed" and f["rated_from"] == "distributor"
    assert "distributor" in f["value"] and "datasheet rating" not in f["value"]
    assert f["source"] == "parts.json attributes of C9"


def test_diode_attribute_rating_is_listed(tmp_path):
    ws = _ws(tmp_path, [("D1", "C7", {"1": "+5V", "2": "GND"})], {}, [DIODE])
    f = _fact(ws, "D1")
    assert (f["status"], f["rated_from"]) == ("listed", "distributor")


def test_fuse_attribute_rating_is_listed(tmp_path):
    ws = _ws(tmp_path, [("F1", "C6", {"1": "+12V", "2": "/OUT"})], {}, [FUSE])
    f = _fact(ws, "F1")
    assert (f["status"], f["rated_from"]) == ("listed", "distributor")


def test_package_table_resistor_is_not_called_the_distributors(tmp_path):
    ws = _ws(tmp_path, [("R1", "C8", {"1": "+5V", "2": "GND"},
                         "Resistor_SMD:R_0402_1005Metric")], {}, [RES])
    f = _fact(ws, "R1")
    assert f["status"] == "listed" and f["rated_from"] == "package"
    assert "package" in f["value"] and "distributor" not in \
        f["value"].replace("no datasheet or distributor", "")
    assert "D/CRCW" in f["source"] and "parts.json" not in f["source"]


# ---------------- a capacitor rated from its part number or Value (#1307)

def _cap_ws(tmp_path, mpn="", value="", net="+12V", ref="C1"):
    """One 2-pad part on net/GND with no voltage attribute; the MPN as a
    footprint property, as KiCad libraries carry it."""
    ws = _ws(tmp_path, [(ref, "", {"1": net, "2": "GND"},
                         "Capacitor_SMD:C_0402_1005Metric", value)], {})
    pcb = ws / "kicad" / "board.kicad_pcb"
    if mpn:
        pcb.write_text(pcb.read_text().replace(
            '(property "LCSC" "")', f'(property "LCSC" "") (property "MPN" '
            f'"{mpn}")'))
    viol, facts = cr.run_check(pcb, ws / "parts", None, None)
    over = [v for v in viol if v["kind"] == "rating_over_voltage"]
    fact = next(f for f in facts["ledger_facts"]
                if f["subject"] == ref and f["kind"] == "rating")
    return over, fact, facts


def test_cap_rated_from_samsung_voltage_code_planted(tmp_path):
    # CL05A105KQ5NNNC: 10th character Q = 6.3 V, on a 12 V rail
    over, fact, facts = _cap_ws(tmp_path, "CL05A105KQ5NNNC")
    assert len(over) == 1 and over[0]["limit_v"] == 6.3
    assert "voltage code Q" in over[0]["msg"] and "Samsung" in over[0]["msg"]
    assert fact["status"] == "failed"


def test_cap_rated_from_samsung_voltage_code_quiet_twin(tmp_path):
    # the same part with code A = 25 V holds 12 V: listed, from the code
    over, fact, facts = _cap_ws(tmp_path, "CL05A105KA5NNNC")
    assert over == []
    assert (fact["status"], fact["rated_from"]) == ("listed", "part_code")
    assert "voltage code A" in fact["source"]
    assert facts["parts_rated_unextracted"] == ["C1"]
    assert not any(v.get("refs") == ["C1"] for v in cr.run_check(
        tmp_path / "ws" / "kicad" / "board.kicad_pcb",
        tmp_path / "ws" / "parts", None, None)[0]
        if v["kind"] == "rating_unrated")


def test_cap_rated_from_value_field(tmp_path):
    over, _, _ = _cap_ws(tmp_path, value="220u/16V", net="+24V")
    assert len(over) == 1 and over[0]["limit_v"] == 16
    assert "Value field" in over[0]["msg"]


def test_cap_value_field_quiet_twin(tmp_path):
    over, fact, _ = _cap_ws(tmp_path, value="C_0.1u_0402_35V_10%_JB",
                            net="+24V")
    assert over == [] and fact["rated_from"] == "part_code"


def test_cap_code_and_value_disagree_lower_held(tmp_path):
    # code A says 25 V, the Value says 10 V: 12 V is over the lower
    over, _, _ = _cap_ws(tmp_path, "CL05A105KA5NNNC", value="1u 10V")
    assert len(over) == 1 and over[0]["limit_v"] == 10
    assert "the lower" in over[0]["msg"]


def test_unknown_code_and_diode_value_are_not_read(tmp_path):
    # a Samsung code letter with no row is never guessed
    over, fact, _ = _cap_ws(tmp_path, "CL05A105KZ5NNNC")
    assert over == [] and fact["status"] == "unknown"
    # a zener's "5V1" is its clamp voltage, not a rating
    over, fact, _ = _cap_ws(tmp_path / "d", value="5V1", ref="D1")
    assert over == [] and fact["status"] == "unknown"


@pytest.mark.parametrize("mpn, volts, maker", [
    # each maker's own example or a part its page decodes
    ("CL05A105KA5NNNC", 25, "Samsung"),
    ("CL10B104KB8NNNC", 50, "Samsung"),
    ("GRM155R71C104KA88D", 16, "Murata"),
    ("GCM188R71H104KA57D", 50, "Murata"),
    ("CC0402KRX7R9BB104", 50, "Yageo"),
    ("CX0805MRX7R8BB223", 25, "Yageo"),
    ("C0402C132J5GACTU", 50, "KEMET"),
    ("UMK107B7474KA-TR", 50, "Taiyo Yuden"),
    ("MCASJ31LSB5106KTNA01", 6.3, "Taiyo Yuden"),
    ("0402CG101J500NT", 50, "Fenghua"),
    ("C1005X7R1C104K050BC", 16, "TDK"),
])
def test_mpn_voltage_code_per_maker(mpn, volts, maker):
    import mlcc
    r = mlcc.mpn_voltage(mpn)
    assert r["v"] == volts and maker in r["series"]
    assert "http" in r["cite"]


@pytest.mark.parametrize("mpn", [
    "CL05A105KZ5NNNC",       # Samsung code with no row
    "GRM31CR72E104KW03L",    # Murata 2E is a row; W03L tail still decodes
    "CGA3E2X7R1H104K080AA",  # TDK CGA: no entry
    "RC0402FR-0710KL",       # a resistor
])
def test_mpn_voltage_code_never_guessed(mpn):
    import mlcc
    r = mlcc.mpn_voltage(mpn)
    if mpn.startswith("GRM"):
        assert r["v"] == 250
    else:
        assert r is None


def test_every_voltage_code_table_cites_a_page():
    import mlcc
    for e in mlcc.load_vcodes():
        assert e["codes"] and "http" in e["cite"] and "p" in e["cite"]
        assert "v" in e["_rx"].groupindex


# ---------------- a diode rated from its part number's family (#1323)

def _diode_ws(tmp_path, mpn="", value="", pads=None, ref="D1"):
    """One 2-pad diode with no extraction and no attribute, pad 1 the
    cathode (KiCad's convention); the MPN as a footprint property."""
    ws = _ws(tmp_path, [(ref, "", pads or {"1": "+48V", "2": "GND"},
                         "Diode_SMD:D_SMA", value)], {})
    pcb = ws / "kicad" / "board.kicad_pcb"
    if mpn:
        pcb.write_text(pcb.read_text().replace(
            '(property "LCSC" "")', f'(property "LCSC" "") (property "MPN" '
            f'"{mpn}")'))
    viol, facts = cr.run_check(pcb, ws / "parts", None, None)
    rev = [v for v in viol if v["kind"] == "rating_reverse_polarity"]
    fact = next(f for f in facts["ledger_facts"]
                if f["subject"] == ref and f["kind"] == "rating")
    return rev, fact


def test_diode_rated_from_family_table_planted(tmp_path):
    # SS14 is a 40 V Schottky: on a 48 V rail, cathode up, it is over
    rev, fact = _diode_ws(tmp_path, "SS14")
    assert len(rev) == 1 and rev[0]["limit_v"] == 40
    assert rev[0]["severity"] == "error" and rev[0]["pin"] == "D1.1"
    assert "SS14" in rev[0]["msg"] and "vishay.com" in rev[0]["msg"]
    assert fact["status"] == "failed"


def test_diode_rated_from_family_table_quiet_twin(tmp_path):
    # SS16 (60 V) holds the same 48 V: listed, from the part number
    rev, fact = _diode_ws(tmp_path, "SS16")
    assert rev == []
    assert (fact["status"], fact["rated_from"]) == ("listed", "part_code")
    assert "SS12, SS13, SS14, SS15, SS16" in fact["source"]
    # the Value field names the part when there is no MPN
    rev, fact = _diode_ws(tmp_path / "v", value="1N4007")
    assert rev == [] and fact["rated_from"] == "part_code"


def test_tvs_rated_by_its_standoff_not_its_clamp(tmp_path):
    # SMAJ16A: 16 V standoff (17.8 V breakdown, 26 V clamp); an 18 V rail
    # is over the standoff, a 15 V one is not
    rev, _ = _diode_ws(tmp_path, "SMAJ16A", pads={"1": "+18V", "2": "GND"})
    assert len(rev) == 1 and rev[0]["limit_v"] == 16
    assert "standoff VRWM" in rev[0]["msg"]
    rev, fact = _diode_ws(tmp_path / "q", "SMAJ16A",
                          pads={"1": "+15V", "2": "GND"})
    assert rev == [] and fact["rated_from"] == "part_code"


def test_bidirectional_tvs_is_checked_either_way_round(tmp_path):
    # SMBJ24CA blocks both ways: 48 V on its anode pad is still over 24 V
    rev, _ = _diode_ws(tmp_path, "SMBJ24CA", pads={"1": "GND", "2": "+48V"})
    assert len(rev) == 1 and rev[0]["limit_v"] == 24
    assert "bidirectional" in rev[0]["msg"]
    # the unidirectional part the same way round is forward, not reversed
    rev, _ = _diode_ws(tmp_path / "u", "SMBJ24A",
                       pads={"1": "GND", "2": "+48V"})
    assert rev == []


@pytest.mark.parametrize("ref, mpn, value", [
    ("D1", "XYZ54", ""),            # a family with no row
    ("D1", "SMF16CA", ""),          # onsemi SMF has no bidirectional row
    ("D1", "BZT52C8V2T", ""),       # a Zener: 8.2 V is its clamp
    ("D1", "", "BZT52C5V1"),
    ("LED1", "NCD0603R1", "red"),   # an LED
])
def test_diode_with_no_family_row_stays_unrated(tmp_path, ref, mpn, value):
    rev, fact = _diode_ws(tmp_path, mpn, value, ref=ref)
    assert rev == [] and fact["status"] == "unknown"


@pytest.mark.parametrize("mpn, volts, bi", [
    ("1N4148", 75, False), ("1N4001", 50, False), ("1N4007-T", 1000, False),
    ("1N5819", 40, False), ("1N5819HW-7-F", 40, False),
    ("SS14-E3/61T", 40, False), ("SS34", 40, False), ("B5819W", 40, False),
    ("BAT54LT1G", 30, False), ("MBRS140T3G", 40, False),
    ("MBR0520LT1G", 20, False), ("B240A-13-F", 40, False),
    ("B2100-13-F", 100, False), ("SMAJ16A", 16, False),
    ("SMAJ33CA", 33, True), ("SMBJ5.0A", 5.0, False), ("SMBJ24CA", 24, True),
    ("SMCJ48A", 48, False), ("SMF16A", 16, False), ("SMF70A", 70, False),
    ("ESD5Z3.3T1G", 3.3, False), ("PESD5V0S1BL", 5, True),
    ("ESD441DPYR", 5.5, False), ("TPD1E04U04DPYR", 3.6, False),
])
def test_diode_family_codes(mpn, volts, bi):
    import diode_vr
    r = diode_vr.mpn_reverse(mpn)
    assert (r["v"], r["bi"]) == (volts, bi) and "http" in r["cite"]


def test_every_diode_family_cites_a_sheet_and_no_clamp_is_a_rating():
    import diode_vr
    for e in diode_vr.load_families():
        assert e["codes"] and "http" in e["cite"]
        assert "k" in e["_rx"].groupindex
        assert e["rating"] in ("VR", "VDC", "VRWM")
        assert (e["rating"] == "VRWM") == (e["kind"] in ("tvs", "esd"))


def test_extracted_rating_stays_verified_and_says_datasheet(tmp_path):
    ws = _suspect_ws(tmp_path, None)
    f = _fact(ws, "U1")
    assert f["status"] == "verified" and f["rated_from"] == "datasheet"
    assert "datasheet rating" in f["value"]


def test_a_failed_attribute_fact_stays_failed(tmp_path):
    bom = [{**CAP50, "attributes": [{"name": "Voltage Rated", "value": "6.3V"}]}]
    ws = _ws(tmp_path, [("C1", "C9", {"1": "+12V", "2": "GND"})], {}, bom)
    f = _fact(ws, "C1")
    assert f["status"] == "failed"
    assert f["source"] == "parts.json attributes of C9"


def test_extraction_plus_attribute_takes_the_weaker_tier(tmp_path):
    """A fuse's voltage and hold current come from the row even with an
    extraction: the pin comparison alone doesn't make the fact read."""
    ext = {"mpn": "0881002", "pinout": [
        {"pin": "1", "name": "VIN", "type": "power_in"},
        {"pin": "2", "name": "VOUT", "type": "nc"}],
        "abs_max": [{"param": "VIN voltage", "value": "-0.3 to 80",
                     "unit": "V"}]}
    ws = _ws(tmp_path, [("F1", "C6", {"1": "+12V", "2": "/OUT"})], {"C6": ext},
             [FUSE])
    f = _fact(ws, "F1")
    assert (f["status"], f["rated_from"]) == ("listed", "distributor")


# a suspect PDF holds back only what was read from it

def test_suspect_pdf_leaves_an_unextracted_part_listed(tmp_path):
    """fmcw-radar's D1, SW1, SW2: an image-only PDF sits beside them and no
    number of theirs came from it."""
    ws = _ws(tmp_path, [("C1", "C9", {"1": "+5V", "2": "GND"})], {}, [CAP50])
    _index(ws, C9=BAD)
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                               None, None)
    f = next(x for x in facts["ledger_facts"]
             if x["subject"] == "C1" and x["kind"] == "rating")
    assert (f["status"], f["rated_from"]) == ("listed", "distributor")
    assert f["source"] == "parts.json attributes of C9"
    w = [v for v in viol if v["kind"] == "rating_datasheet_suspect"]
    assert w and w[0]["refs"] == ["C1"]      # the PDF is still suspect
    assert "read from it" in w[0]["msg"]


def test_suspect_pdf_holds_only_the_pinout_when_the_rating_is_the_row(tmp_path):
    """An extraction exists, but the fuse's rating came from attributes: the
    PDF sourced the pinout and nothing else."""
    ext = {"mpn": "0881002", "pinout": [{"pin": "1", "name": "A", "type": "nc"},
                                        {"pin": "2", "name": "B", "type": "nc"}]}
    ws = _ws(tmp_path, [("F1", "C6", {"1": "+12V", "2": "/OUT"})], {"C6": ext},
             [FUSE])
    assert _fact(ws, "F1", "pinout")["status"] == "verified"
    _index(ws, C6=BAD)
    assert (_fact(ws, "F1")["status"], _fact(ws, "F1")["rated_from"]) == \
        ("listed", "distributor")
    assert _fact(ws, "F1", "pinout")["status"] == "unknown"


def test_extraction_read_from_another_good_pdf_stays_verified(tmp_path):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "2": "GND", "3": "+3V3"})],
             {"C1": {**MCU, "source_pdf": "parts/C99_testmcu.pdf"}})
    _index(ws, C1=BAD, C99=GOOD)
    assert _fact(ws, "U1")["status"] == "verified"
    assert _fact(ws, "U1", "pinout")["status"] == "verified"


def test_extraction_read_from_another_suspect_pdf_is_held(tmp_path):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "2": "GND", "3": "+3V3"})],
             {"C1": {**MCU, "source_pdf": "parts/C99_testmcu.pdf"}})
    _index(ws, C1=GOOD, C99=BAD)
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                               None, None)
    f = _u1(facts, "rating")
    assert f["status"] == "unknown" and "no text layer" in f["value"]
    # C99 isn't on the board: nothing to warn about by refdes
    assert facts["suspect_datasheets"] == {}
    assert not [v for v in viol if v["kind"] == "rating_datasheet_suspect"]


@pytest.mark.parametrize("source_pdf,own", [
    (None, True),
    ("parts/C1.pdf", True),
    ("parts/U1_esp32-c3-mini-1_datasheet_en.pdf", True),   # can't tell: own
    ("boards/C5/parts/U1_datasheet.pdf", True),            # a folder, not a PDF
    ("boards/x/parts/C1.pdf (W5500 Datasheet, C2 errata, 2013)", True),
    ("boards\\x\\parts\\C2913198.pdf", False),
    ("boards/x/parts/U2_MCP73831T-2ACI-OT_C424093.pdf", False),
    ("parts/C970725_CH224K.pdf", False),
])
def test_which_pdf_an_extraction_was_read_from(source_pdf, own):
    got = cr.extraction_pdf({"source_pdf": source_pdf}, "C1")
    assert (got == "C1") is own, got


# the warning that tells a board what to do about it

def _no_read(viol):
    return [v for v in viol if v["kind"] == "rating_no_datasheet_read"]


def test_one_warning_names_every_listed_part_and_the_way_out(tmp_path):
    ws = _ws(tmp_path, [("C1", "C9", {"1": "+5V", "2": "GND"}),
                        ("C2", "C9", {"1": "+5V", "2": "GND"}),
                        ("R1", "C8", {"1": "+5V", "2": "GND"},
                         "Resistor_SMD:R_0402_1005Metric")], {}, [CAP50, RES])
    viol, _ = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                           None, None)
    w = _no_read(viol)
    assert len(w) == 1 and w[0]["severity"] == "warning"
    assert w[0]["refs"] == ["C1", "C2", "R1"]
    assert "datasheet_extract" in w[0]["msg"] and "3" in w[0]["msg"]


def test_no_warning_when_every_rating_was_read(tmp_path):
    ws = _suspect_ws(tmp_path, None)
    viol, _ = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                           None, None)
    assert _no_read(viol) == []


def test_a_rating_a_person_checked_is_not_warned_about(tmp_path):
    ws = _ws(tmp_path, [("C1", "C9", {"1": "+5V", "2": "GND"})], {}, [CAP50])
    viol, _ = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                           None, None, trusted={"C1"})
    assert _no_read(viol) == []


# ------------------------------------ unassessed datasheets (#755)

def test_old_index_entry_with_no_status_is_unassessed_not_ok(tmp_path):
    ws = _suspect_ws(tmp_path, {"url": "u", "sha256": "s", "mpn": "TESTMCU"})
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb",
                               ws / "parts", None, None)
    w = [v for v in viol if v["kind"] == "rating_datasheet_unassessed"]
    assert len(w) == 1 and w[0]["severity"] == "warning", viol
    assert w[0]["refs"] == ["U1"] and "C1" in w[0]["msg"]
    assert "--fetch" in w[0]["msg"] and "no PDF" in w[0]["msg"]
    assert facts["unassessed_datasheets"] == ["C1"]
    assert not [v for v in viol if v["kind"] == "rating_datasheet_suspect"]
    # a warning, and the facts stand: no file to judge is not a judgement
    assert _u1(facts, "rating")["status"] != "unknown" \
        or "suspect" not in _u1(facts, "rating")["value"]


def test_unassessed_warning_is_one_line_for_the_whole_board(tmp_path):
    ws = _suspect_ws(tmp_path, {"url": "u"})
    d = ws / "parts" / "datasheets"
    (d / "index.json").write_text(json.dumps({"C1": {"status": "unassessed",
                                                     "reasons": ["no PDF"]}}))
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb",
                               ws / "parts", None, None)
    assert len([v for v in viol
                if v["kind"] == "rating_datasheet_unassessed"]) == 1
    assert facts["unassessed_datasheets"] == ["C1"]


def test_assessed_or_unrecorded_datasheet_is_not_unassessed(tmp_path):
    for index in ({"status": "ok", "reasons": []}, None):
        ws = _suspect_ws(tmp_path / str(bool(index)), index)
        viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb",
                                   ws / "parts", None, None)
        assert facts["unassessed_datasheets"] == []
        assert not [v for v in viol
                    if v["kind"] == "rating_datasheet_unassessed"]


def test_a_pdf_on_disk_with_no_index_entry_is_judged_by_the_check(tmp_path):
    # #768: nobody has to run --assess; parts.json names the part, so a wrong
    # PDF takes its facts down at the first check
    from test_datasheet_fetch import WRONG
    ws = _suspect_ws(tmp_path, None)
    (ws / "parts" / "parts.json").write_text(json.dumps(
        {"parts": [{"lcsc": "C1", "mpn": "TESTMCU"}]}))
    d = ws / "parts" / "datasheets"
    (d / "C1.pdf").write_bytes(WRONG)
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb",
                               ws / "parts", None, None)
    assert facts["unassessed_datasheets"] == []
    assert [v["kind"] for v in viol if v["kind"].startswith("rating_datasheet")
            ] == ["rating_datasheet_suspect"]
    assert _u1(facts, "rating")["status"] == "unknown"
    assert json.loads((d / "index.json").read_text())["C1"]["status"] == "suspect"


def test_old_entry_with_a_wrong_pdf_is_suspect_with_no_assess_run(tmp_path):
    # the known answer in small: fmcw-radar's 44 verified held only because no
    # one had run --assess
    from test_datasheet_fetch import WRONG
    ws = _suspect_ws(tmp_path, {"url": "u", "sha256": "s", "mpn": "TESTMCU"})
    (ws / "parts" / "datasheets" / "C1.pdf").write_bytes(WRONG)
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb",
                               ws / "parts", None, None)
    assert "C1" in facts["suspect_datasheets"]
    assert _u1(facts, "rating")["status"] == "unknown"
    assert not [v for v in viol if v["kind"] == "rating_datasheet_unassessed"]


# ---------------------------------------- a listing against the datasheet (#799)
#
# Known answers, each read from both the LCSC listing and the part's own PDF
# (see reference/remediations/rating_listing_disagrees.md for what they mean).

# CH224K: LCSC says "Voltage - Supply 4V~30V". Its datasheet section 8.1.3
# gives VBUS 13.5 V and VDD 3.6 V: 4-30 V needs a series resistor. These rows
# are the ones the shipped pd-trigger board's extraction holds.
CH224K_ROWS = [
    {"param": "VDD operating supply voltage on pin 1, min (8.1.3)",
     "value": 3.0, "unit": "V"},
    {"param": "VDD operating supply voltage on pin 1, max (8.1.3)",
     "value": 3.6, "unit": "V"},
    {"param": "VODHV voltage on the VBUS pin (pin 8), min (8.1.3)",
     "value": -0.5, "unit": "V"},
    {"param": "VODHV voltage on the VBUS pin (pin 8), max (8.1.3)",
     "value": 13.5, "unit": "V"},
    {"param": "VIOCC voltage on CC1 / CC2 / CFG1 (pins 7, 6, 9), max (8.1.3)",
     "value": 8, "unit": "V"},
    {"param": "VIOUX voltage on the other pins, max (8.1.3)",
     "value": "VDD+0.5", "unit": "V"}]
CH224K = {"lcsc": "C970725", "mpn": "CH224K",
          "attributes": [{"name": "Voltage - Supply", "value": "4V~30V"}]}
# WJ500V-5.08-2P: LCSC says 250 V; the datasheet says 300 V (UL), 400 V (IEC).
WJ500V_ROWS = [{"param": "rated_voltage_UL", "value": 300, "unit": "V"},
               {"param": "rated_voltage_IEC", "value": 400, "unit": "V"},
               {"param": "withstanding_voltage_1min", "value": 1600,
                "unit": "VAC"}]
WJ500V = {"lcsc": "C8465", "mpn": "WJ500V-5.08-2P",
          "attributes": [{"name": "Voltage Rating (Max)", "value": "250V"}]}
# AO3400A: both say VDS 30 V. The gate row must not count against it.
AO3400A_ROWS = [{"param": "VDS Drain-Source Voltage", "value": 30, "unit": "V"},
                {"param": "VGS Gate-Source Voltage", "value": "+/-12",
                 "unit": "V"}]
AO3400A = {"lcsc": "C20917", "mpn": "AO3400A", "attributes": [
    {"name": "Drain to Source Voltage", "value": "30V"}]}
# 1N4148W: 75 V is the VR row; VRM 100 V on the same page is the peak.
N4148W_ROWS = [{"param": "VRM peak repetitive reverse voltage", "value": 100,
                "unit": "V"},
               {"param": "VR reverse voltage", "value": 75, "unit": "V"}]
N4148W = {"lcsc": "C81598", "mpn": "1N4148W", "attributes": [
    {"name": "Voltage - DC Reverse(Vr)", "value": "75V"}]}
# TPS563201: 17 V is the recommended input; abs max sits above it, and a
# listing of the recommended top is not wrong.
TPS563201_ROWS = [{"param": "VIN input voltage range, abs max", "value": 19,
                   "unit": "V"},
                  {"param": "VIN recommended operating", "value": 17,
                   "unit": "V"}]
TPS563201 = {"lcsc": "C116592", "mpn": "TPS563201", "attributes": [
    {"name": "Voltage - Input (Max)", "value": "17V"}]}


def _ext(rows, **kw):
    return {"abs_max": rows, **kw}


def _dis(bom, ext):
    return [c for c in cr.listing_comparisons(bom, ext) if not c["agree"]]


def test_listing_high_is_a_disagreement_ch224k():
    """Known answer: a listing above the whole part's ratings."""
    [c] = _dis(CH224K, _ext(CH224K_ROWS))
    assert c["listing_v"] == 30 and c["datasheet_v"] == 13.5
    assert c["side"] == "high"
    assert c["listing_attr"] == "Voltage - Supply"
    assert "VODHV" in c["datasheet_row"]


def test_listing_low_is_a_disagreement_wj500v():
    """Known answer: 250 V against 300 V (UL), the closer of the two rows, and
    the 1600 VAC withstanding test is no rating."""
    [c] = _dis(WJ500V, _ext(WJ500V_ROWS))
    assert c["listing_v"] == 250 and c["datasheet_v"] == 300
    assert c["side"] == "low"


@pytest.mark.parametrize("bom,rows", [
    (AO3400A, AO3400A_ROWS), (N4148W, N4148W_ROWS),
    (TPS563201, TPS563201_ROWS)],
    ids=["ao3400a", "1n4148w-vr-not-vrm", "tps563201-recommended-top"])
def test_listing_agrees_with_a_real_datasheet(bom, rows):
    """Known answers: same number, and a part with a row that is a different
    limit than the listed one is not a disagreement."""
    comps = cr.listing_comparisons(bom, _ext(rows))
    assert comps and all(c["agree"] for c in comps)


def test_listing_p_channel_rating_is_compared_by_size():
    bom = {"lcsc": "C9", "attributes": [
        {"name": "Drain to Source Voltage", "value": "30V"}]}
    ext = _ext([{"param": "VDS drain-source voltage", "value": -30, "unit": "V"}])
    assert not _dis(bom, ext)


def test_listing_within_tolerance_agrees():
    bom = {"lcsc": "C9", "attributes": [
        {"name": "Reverse Voltage (Vr)", "value": "5V"}]}
    ok = _ext([{"param": "VRM reverse working voltage", "value": 5.25, "unit": "V"}])
    assert not _dis(bom, ok)
    off = _ext([{"param": "VRM reverse working voltage", "value": 5.6, "unit": "V"}])
    assert [c["side"] for c in _dis(bom, off)] == ["low"]


def test_listing_supply_range_low_is_not_a_disagreement():
    """A supply listing may be a nominal: under the part's limit is no finding,
    and only a top over everything on the part is."""
    bom = {"lcsc": "C9", "attributes": [
        {"name": "Operating Voltage", "value": "3.3V"}]}
    assert not _dis(bom, _ext([{"param": "VDD supply", "value": 6, "unit": "V"}]))


def test_listing_supply_range_is_not_held_to_one_pin():
    """An input range over VDD but within the VIN pin's limit is the part's."""
    bom = {"lcsc": "C9", "attributes": [
        {"name": "Voltage - Supply", "value": "4V~30V"}]}
    ext = _ext([{"param": "VDD max", "value": 3.6, "unit": "V"},
                {"param": "VIN pin max", "value": 32, "unit": "V"}])
    assert not _dis(bom, ext)


def test_listing_without_a_datasheet_voltage_is_not_compared():
    assert cr.listing_comparisons(N4148W, _ext([
        {"param": "IF forward current", "value": 0.3, "unit": "A"}])) == []
    assert cr.listing_comparisons(N4148W, {}) == []
    assert cr.listing_comparisons({"lcsc": "C9", "attributes": []},
                                  _ext(N4148W_ROWS)) == []


def test_listing_pin_ratings_are_datasheet_rows():
    bom = {"lcsc": "C9", "attributes": [
        {"name": "Reverse Voltage (Vr)", "value": "100V"}]}
    ext = {"pinout": [{"pin": "1", "name": "K", "type": "passive"},
                      {"pin": "2", "name": "A", "type": "passive"}],
           "pin_ratings": [{"pins": ["K"], "ref": "A", "max": 40, "kind": "voltage",
                            "source": "VRRM p.1"}]}
    [c] = _dis(bom, ext)
    assert c["datasheet_v"] == 40 and c["side"] == "high"


def _disagree_ws(tmp_path, rail, attr="50V", ds=25):
    return _ws(tmp_path,
               [("D1", "C9", {"1": rail, "2": "GND"})],
               {"C9": {"mpn": "D", "abs_max": [
                   {"param": "VR reverse voltage", "value": ds, "unit": "V"}]}},
               bom=[{"lcsc": "C9", "mpn": "D", "attributes": [
                   {"name": "Voltage - DC Reverse(Vr)", "value": attr}]}])


def _run_listing(ws, trusted=None):
    viol, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                               None, None, trusted=trusted)
    return [v for v in viol if v["kind"] == "rating_listing_disagrees"], facts


@pytest.mark.parametrize("rail", ["+12V", "+30V", "+60V"])
def test_listing_finding_fires_on_either_side_of_the_rail(tmp_path, rail):
    """The issue's known answer: 50 V against 25 V, with the rail below both,
    between them and above both."""
    [f], facts = _run_listing(_disagree_ws(tmp_path, rail))
    assert f["severity"] == "warning"
    assert f["listing_v"] == 50 and f["datasheet_v"] == 25
    assert f["side"] == "high" and f["refs"] == ["D1"]
    assert f["rating"] == "reverse" and "50" in f["msg"] and "25" in f["msg"]
    assert f["check_used"] in ("datasheet", "listing", "package", "both", "none")
    assert facts["listing_comparison"] == [
        {"lcsc": "C9", "rating": "reverse", "listing_v": 50.0, "datasheet_v": 25.0,
         "agree": False, "side": "high"}]


def test_listing_matching_numbers_get_no_finding(tmp_path):
    fs, facts = _run_listing(_disagree_ws(tmp_path, "+12V", attr="25V"))
    assert fs == []
    assert [c["agree"] for c in facts["listing_comparison"]] == [True]


def test_listing_finding_says_which_number_the_check_used(tmp_path):
    """An extracted part is judged on its datasheet, never its listing. This
    diode's extraction has no K/A pins, so nothing judged it at all: the finding
    has to say that too."""
    [f], _ = _run_listing(_disagree_ws(tmp_path, "+12V"))
    assert f["check_used"] == "none"
    assert "compared neither" in f["msg"]
    ext = dict(MCU, abs_max=[{"param": "VIN pin max", "value": 6, "unit": "V"}],
               pin_ratings=[{"pins": ["VDD"], "max": 6, "kind": "voltage",
                             "source": "VDD p.1"}])
    ws = _ws(tmp_path / "b", [("U1", "C1", {"1": "+3V3", "2": "GND", "3": "+3V3"})],
             {"C1": ext},
             bom=[{"lcsc": "C1", "mpn": "M", "attributes": [
                 {"name": "Voltage - Supply", "value": "30V"}]}])
    [f], _ = _run_listing(ws)
    assert f["check_used"] == "datasheet"
    assert "used the datasheet" in f["msg"]


def test_listing_part_read_from_a_suspect_pdf_is_not_compared(tmp_path):
    """A number read from a PDF that may be another part's says nothing."""
    ws = _disagree_ws(tmp_path, "+12V")
    d = ws / "parts" / "datasheets"
    d.mkdir()
    (d / "index.json").write_text(json.dumps(
        {"C9": {"status": "suspect", "reasons": ["MPN not in the text"]}}))
    fs, facts = _run_listing(ws)
    assert fs == [] and facts["listing_comparison"] == []
    assert facts["listing_not_compared"] == {"C9": "suspect datasheet"}


def test_listing_finding_dropped_once_a_person_has_verified(tmp_path):
    fs, _ = _run_listing(_disagree_ws(tmp_path, "+12V"), trusted={"D1"})
    assert fs == []


def test_listing_finding_names_every_ref(tmp_path):
    ws = _ws(tmp_path,
             [("D1", "C9", {"1": "+12V", "2": "GND"}),
              ("D2", "C9", {"1": "+12V", "2": "GND"})],
             {"C9": {"mpn": "D", "abs_max": [
                 {"param": "VR reverse voltage", "value": 25, "unit": "V"}]}},
             bom=[{"lcsc": "C9", "mpn": "D", "attributes": [
                 {"name": "Voltage - DC Reverse(Vr)", "value": "50V"}]}])
    [f], _ = _run_listing(ws)
    assert f["refs"] == ["D1", "D2"]


def test_listing_finding_never_blocks(tmp_path):
    """The kind reports; it does not by itself fail a board."""
    p = _run(_disagree_ws(tmp_path, "+12V", attr="50V", ds=25))
    fs = [v for v in p["violations"] if v["kind"] == "rating_listing_disagrees"]
    assert fs and all(v["severity"] == "warning" for v in fs)


def test_planted_listing_mutant_is_caught():
    """The golden mutant, through the CLI entry score_checks uses."""
    mut = ROOT / "tests" / "golden" / "mutants" / \
        "listing-disagrees-with-datasheet"
    payload, _ = cr.run(["--pcb", str(mut / "blinky2.kicad_pcb"),
                         "--parts", str(mut / "parts")])
    hits = [v for v in payload["violations"]
            if v.get("kind") == "rating_listing_disagrees"]
    assert len(hits) == 1, json.dumps(payload["violations"])
    assert "D1" in hits[0]["refs"] and hits[0]["severity"] == "warning"
    assert hits[0]["listing_v"] == 50 and hits[0]["datasheet_v"] == 25


def test_a_vds_listing_is_not_judged_against_the_gate_row():
    # IRF640's own extraction: the VDS row is worded so it isn't read, VGS is.
    bom = {"attributes": [{"name": "Drain to Source Voltage", "value": "200V"}]}
    ext = {"abs_max": [{"param": "Vgs gate-to-source voltage", "value": "+/-20",
                        "unit": "V"}]}
    assert cr.listing_comparisons(bom, ext) == []


IRF640_VDSS = {"param": "Vdss drain-to-source voltage (headline p.1; V(BR)Dss "
                        "200 V min at Vgs=0 V, Id=250 uA, p.2)",
               "value": 200, "unit": "V"}


@pytest.mark.parametrize("listed,agree,side", [("200V", True, None),
                                               ("400V", False, "high")])
def test_irf640s_vdss_row_now_answers_its_vds_listing(listed, agree, side):
    """#810: the row whose note says '200 V min' is read, so the listing is
    compared against it, not left out; a planted 400 V listing is high."""
    bom = {"attributes": [{"name": "Drain to Source Voltage", "value": listed}]}
    ext = {"abs_max": [IRF640_VDSS, {"param": "Vgs gate-to-source voltage",
                                     "value": "+/-20", "unit": "V"}]}
    [c] = cr.listing_comparisons(bom, ext)
    assert c["datasheet_v"] == pytest.approx(200) and "Vdss" in c["datasheet_row"]
    assert (c["agree"], c["side"]) == (agree, side)


# NTMFS5C628NL's own abs-max table names the drain row VDSS and the gate row
# VGS; SSM3K2615R and DMP3013 name the gate row VGSS. The JEDEC suffix (S:
# third terminal shorted to source, O: open) names the same two pins.
NFET = {"mpn": "TESTNFET", "pinout": [
    {"pin": "1", "name": "G", "type": "input"},
    {"pin": "2", "name": "S", "type": "passive"},
    {"pin": "3", "name": "D", "type": "passive"}],
    "abs_max": [
        {"param": "Drain-to-Source Voltage VDSS", "value": 60, "unit": "V"},
        {"param": "Gate-to-Source Voltage VGSS", "value": "+/-20", "unit": "V"}]}


def _fet_ws(tmp_path, drain, gate="+12V", part=NFET):
    return _ws(tmp_path, [("Q1", "C5", {"1": gate, "2": "GND", "3": drain})],
               {"C5": part})


def test_vdss_row_rates_the_drain_and_catches_a_planted_overvoltage(tmp_path):
    p = _run(_fet_ws(tmp_path, "+80V"))
    [e] = _errors(p)
    assert e["kind"] == "rating_over_voltage" and e["pin"] == "Q1.3"
    assert e["limit_v"] == pytest.approx(60) and "VDSS" in e["rating"]


def test_vdss_quiet_twin_within_rating_is_rated_and_silent(tmp_path):
    p = _run(_fet_ws(tmp_path, "+48V"))
    assert _errors(p) == []
    assert not [v for v in p["violations"] if v["kind"] == "rating_unrated"]


def test_vgss_row_rates_the_gate(tmp_path):
    assert [e["pin"] for e in _errors(_run(_fet_ws(tmp_path, "+48V",
                                                   gate="+24V")))] == ["Q1.1"]


def test_p_channel_vdss_is_a_floor_not_a_ceiling(tmp_path):
    """DMP3013's row is -30 V: a drain 12 V above source is outside the
    P-channel's rating, one at source is not."""
    pfet = dict(NFET, abs_max=[
        {"param": "Drain-source voltage VDSS", "value": -30, "unit": "V"},
        {"param": "Gate-source voltage VGSS", "value": "+/-20", "unit": "V"}])
    ws = _ws(tmp_path, [("Q1", "C5", {"1": "GND", "2": "+12V", "3": "+12V"})],
             {"C5": pfet})
    p = _run(ws)
    assert _errors(p) == []
    assert not [v for v in p["violations"] if v["kind"] == "rating_unrated"]


@pytest.mark.parametrize("sym,pins,ref", [
    ("VDSS", {"3"}, "S"), ("VGSS", {"1"}, "S"), ("VCEO", {"3"}, "E"),
    ("VDS", {"3"}, "S")])
def test_jedec_symbol_names_its_two_pins(sym, pins, ref):
    pin = [{"pin": "1", "name": "G", "type": "input"},
           {"pin": "2", "name": "S", "type": "passive"},
           {"pin": "3", "name": "D", "type": "passive"}]
    if sym == "VCEO":
        pin = [{"pin": "1", "name": "B", "type": "input"},
               {"pin": "2", "name": "E", "type": "passive"},
               {"pin": "3", "name": "C", "type": "passive"}]
    got = cr.classify_row(f"{sym} max", cr.pin_index(pin), pin)
    assert got[0] == pins and got[2] == ref


def test_a_four_letter_word_that_is_no_symbol_binds_nothing():
    """VDDS is no JEDEC symbol (third letter is not the suffix)."""
    pin = [{"pin": "1", "name": "D", "type": "power_in"},
           {"pin": "2", "name": "S", "type": "passive"}]
    assert not cr.classify_row("VDDS", cr.pin_index(pin), pin)[0]


# ---------------------------------------------------- the extraction library
# reference/extractions/<lcsc>.json: hwde's own datasheet reads, which a board
# with no parts/<lcsc>.json falls back to, so a common part is judged on every
# board that uses it rather than left unrated.

NPN = {"mpn": "TESTNPN", "pinout": [
    {"pin": "1", "name": "B", "type": "input"},
    {"pin": "2", "name": "E", "type": "passive"},
    {"pin": "3", "name": "C", "type": "passive"}],
    "pin_ratings": [
        {"pins": ["C"], "kind": "voltage", "level": "abs_max", "max": 25,
         "ref": "E", "source": "test p.1: 'VCEO 25 V'"},
        {"pins": ["B"], "kind": "voltage", "level": "abs_max", "min": -5,
         "ref": "E", "source": "test p.1: 'VEBO 5 V'"}]}


def _lib(monkeypatch, tmp_path, entries):
    lib = tmp_path / "lib"
    lib.mkdir()
    for lcsc, ext in entries.items():
        (lib / f"{lcsc}.json").write_text(json.dumps(ext))
    monkeypatch.setattr(cr, "LIBRARY_DIR", lib)


def test_library_extraction_judges_a_part_the_board_has_none_for(
        tmp_path, monkeypatch):
    _lib(monkeypatch, tmp_path, {"C77": NPN})
    ws = _ws(tmp_path, [("Q1", "C77", {"1": "+3V3", "2": "GND",
                                       "3": "+48V"})], {})
    p = _run(ws)
    errs = _errors(p)
    assert [e["pin"] for e in errs] == ["Q1.3"]
    assert errs[0]["limit_v"] == pytest.approx(25)
    assert not [v for v in p["violations"] if v["kind"] == "rating_unrated"
                and "Q1" in (v.get("refs") or [])]


def test_board_extraction_wins_over_the_library(tmp_path, monkeypatch):
    _lib(monkeypatch, tmp_path, {"C77": NPN})
    own = json.loads(json.dumps(NPN))
    own["pin_ratings"][0]["max"] = 60
    ws = _ws(tmp_path, [("Q1", "C77", {"1": "+3V3", "2": "GND",
                                       "3": "+48V"})], {"C77": own})
    assert _errors(_run(ws)) == []


def test_no_library_entry_is_still_unrated(tmp_path, monkeypatch):
    _lib(monkeypatch, tmp_path, {})
    ws = _ws(tmp_path, [("Q1", "C77", {"1": "+3V3", "2": "GND",
                                       "3": "+48V"})], {})
    p = _run(ws)
    assert any(v["kind"] == "rating_unrated" and v["refs"] == ["Q1"]
               for v in p["violations"])


def test_inductors_and_beads_are_not_voltage_unrated(tmp_path):
    bom = [{"lcsc": "C5", "mpn": "IND", "attributes": []},
           {"lcsc": "C6", "mpn": "BEAD", "attributes": []},
           {"lcsc": "C7", "mpn": "CAP", "attributes": []}]
    ws = _ws(tmp_path, [("L1", "C5", {"1": "SW", "2": "+5V"}),
                        ("FB1", "C6", {"1": "+5V", "2": "+5VA"}),
                        ("C1", "C7", {"1": "+5V", "2": "GND"})], {}, bom)
    p = _run(ws)
    grouped = [v for v in p["violations"] if v["kind"] == "rating_unrated"
               and "two-terminal" in v["msg"]]
    assert [g["refs"] for g in grouped] == [["C1"]]


def _q1_unrated(p):
    return [v for v in p["violations"] if v["kind"] == "rating_unrated"
            and v["refs"] == ["Q1"]]


def test_rating_against_an_unknown_net_is_judged_at_the_board_rails(
        tmp_path, monkeypatch):
    # emitter on a node no constraint names: VCE is at most +12V - 0 V = 12 V,
    # inside VCEO 25 V, so C is judged, not left unrated. B stays unrated:
    # VEBO's -5 V against an unknown emitter could break at the rails
    _lib(monkeypatch, tmp_path, {"C77": NPN})
    ws = _ws(tmp_path, [("Q1", "C77", {"1": "+3V3", "2": "/EMIT",
                                       "3": "+12V"})], {})
    p = _run(ws)
    assert _errors(p) == []
    [u] = _q1_unrated(p)
    assert u["pins"] == ["1 B: rated against E, whose net has no known "
                         "voltage (at the board's rails, 0..12 V, it could "
                         "break the rating)"]


def test_rating_against_an_unknown_net_that_could_break_it_stays_unrated(
        tmp_path, monkeypatch):
    # +48V on C over an unknown emitter could be 48 V VCE: not passed silently
    _lib(monkeypatch, tmp_path, {"C77": NPN})
    ws = _ws(tmp_path, [("Q1", "C77", {"1": "+3V3", "2": "/EMIT",
                                       "3": "+48V"})], {})
    p = _run(ws)
    assert _errors(p) == []
    [u] = _q1_unrated(p)
    assert "rated against E" in u["msg"] and "could break" in u["msg"]


LIBRARY = sorted((ROOT / "reference" / "extractions").glob("C*.json"))


@pytest.mark.parametrize("path", LIBRARY, ids=lambda p: p.stem)
def test_library_entry_is_valid_and_cites_its_datasheet(path):
    """Fact, not fiction: every entry is schema-valid, names its LCSC code,
    records the PDF's URL and sha256, and every rating row cites a page."""
    ext = json.loads(path.read_text())
    jsonschema.validate(ext, datasheet_extract.DATASHEET_SCHEMA)
    assert ext["lcsc"] == path.stem
    src = ext["source_pdf"]
    assert "http" in src and re.search(r"sha256 [0-9a-f]{64}", src), src
    pins = {p["pin"] for p in ext["pinout"]} | \
        {p["name"] for p in ext["pinout"]}
    assert ext.get("pin_ratings"), "a library entry exists to rate pins"
    for row in ext["pin_ratings"]:
        assert re.search(r"\bp\.\s?\d+", row["source"]), row
        assert set(row["pins"]) <= pins, row
        assert row.get("ref") is None or row["ref"] in pins, row


# --- #835: pins the part drives itself, and bootstrap pins ----------------

DRV = {"mpn": "TESTDRV", "pinout": [
    {"pin": "1", "name": "VDD", "type": "power_in"},
    {"pin": "2", "name": "GND", "type": "ground"},
    {"pin": "3", "name": "OUT", "type": "output"},
    {"pin": "4", "name": "VREF", "type": "analog", "self_driven": True},
    {"pin": "5", "name": "BST", "type": "passive"},
    {"pin": "6", "name": "SW", "type": "output"},
    {"pin": "7", "name": "CFG", "type": "analog"}],
    "pin_ratings": [{"pins": ["VDD"], "kind": "voltage", "level": "abs_max",
                     "min": -0.3, "max": 3.9,
                     "source": "test datasheet p.1: 'VDD -0.3 3.9 V'"},
                    {"pins": ["SW"], "kind": "voltage", "level": "abs_max",
                     "min": -0.3, "max": 3.9, "source": "test p.1 'SW 3.9 V'"}]}


def _drv(tmp_path, out_net, bst_net="/BST", extra=()):
    fps = [("U1", "C9", {"1": "+3V3", "2": "GND", "3": out_net, "4": "/VREF",
                         "5": bst_net, "6": "/SW"}),
           ("C1", "C10", {"1": "/BST", "2": "/SW"}),
           ("C2", "C10", {"1": "/VREF", "2": "GND"}), *extra]
    return _run(_ws(tmp_path, fps, {"C9": DRV}))


def test_driven_pin_is_covered_by_the_supply_rating(tmp_path):
    p = _drv(tmp_path, "/OUTN")
    assert not _errors(p)
    un = [v for v in p["violations"] if v["kind"] == "rating_unrated"]
    # OUT, VREF and the bootstrap pin are covered; only nothing is left for U1
    assert not [v for v in un if "U1" in v["msg"]]
    assert any("U1.4" in c for c in p["pins_covered_by_rule"])


def test_external_rail_on_a_driven_pin_above_its_supply_is_an_error(tmp_path):
    p = _drv(tmp_path, "+5V")          # a 5 V rail driving the part's OUT
    errs = _errors(p)
    assert [e["pin"] for e in errs] == ["U1.3"]
    assert "covered by its supply rating" in errs[0]["msg"]
    assert errs[0]["limit_v"] == pytest.approx(3.9)


def test_bootstrap_net_with_an_extra_connection_is_not_covered(tmp_path):
    p = _drv(tmp_path, "/OUTN", extra=[("R1", "C11", {"1": "/BST", "2": "GND"})])
    un = [v for v in p["violations"] if v["kind"] == "rating_unrated"
          and "U1" in v["msg"]]
    assert un and "BST" in un[0]["msg"]
    assert not any("U1.5" in c for c in p["pins_covered_by_rule"])


def test_an_unmarked_analog_pin_stays_unrated(tmp_path):
    ws = _ws(tmp_path, [("U1", "C9", {"1": "+3V3", "2": "GND", "7": "/X"})],
             {"C9": DRV})
    un = [v for v in _run(ws)["violations"] if v["kind"] == "rating_unrated"]
    assert un and "CFG" in un[0]["msg"]


# --- #864: a net with no stated voltage, bounded by its feed and a clamp ----

def _zener(vz):
    return {"mpn": "TESTZ", "pinout": [
        {"pin": "1", "name": "K", "type": "passive"},
        {"pin": "2", "name": "A", "type": "passive"}],
        "pin_ratings": [{"pins": ["K"], "kind": "voltage", "level": "recommended",
                         "max": vz, "ref": "A", "source": "test p.1 Vz"}]}


PFET = {"mpn": "TESTPFET", "pinout": [
    {"pin": "1", "name": "G", "type": "input"},
    {"pin": "2", "name": "S", "type": "passive"}],
    "pin_ratings": [{"pins": ["G"], "kind": "voltage", "level": "abs_max",
                     "min": -10, "max": 10, "ref": "S", "source": "test p.1 Vgs"}]}


def _gate(tmp_path, vz, pd="500mW"):
    fps = [("Q1", "C20", {"1": "/G", "2": "+12V"}),
           ("D1", "C21", {"1": "+12V", "2": "/G"}),
           ("R1", "C22", {"1": "+12V", "2": "/G"}, "x", "100k"),
           ("R2", "C22", {"1": "/G", "2": "/PD"}, "x", "100k")]
    bom = [{"lcsc": "C21", "mpn": "TESTZ", "attributes": [
        {"name": "Pd - Power Dissipation", "value": pd}]}]
    return _run(_ws(tmp_path, fps, {"C20": PFET, "C21": _zener(vz)}, bom=bom))


def test_clamped_gate_net_is_bounded_and_the_clamp_judged(tmp_path):
    p = _gate(tmp_path, 8)          # gate in 4..12 V: Vgs -8..0 inside +-10
    bad = [v for v in p["violations"] if v["kind"] in
           ("rating_over_voltage", "rating_reverse_polarity",
            "rating_over_current", "rating_unrated") and
           any(r in ("Q1", "D1") for r in v["refs"])]
    assert not bad


def test_a_clamp_too_loose_for_the_gate_is_caught(tmp_path):
    p = _gate(tmp_path, 15)         # gate down to 0 V: Vgs -12 under -10
    hit = [v for v in p["violations"] if v["refs"] == ["Q1"]]
    assert [v["kind"] for v in hit] == ["rating_reverse_polarity"]
    assert hit[0]["severity"] == "warning"


def test_a_clamp_its_feed_could_burn_is_flagged(tmp_path):
    p = _gate(tmp_path, 8, pd="0.5mW")
    assert any(v["kind"] == "rating_over_current" and v["refs"] == ["D1"]
               for v in p["violations"])


# --- #867-869: TVS and abs-level clamps, K-side bounds, driver rails --------

def _tvs(vbr="8.89 to 9.83", mpn="SMF8.0A"):
    """A TVS as the extractor writes it: free-text rows, no pin_ratings."""
    return {"mpn": mpn, "pinout": [
        {"pin": "1", "name": "K", "type": "passive"},
        {"pin": "2", "name": "A", "type": "passive"}],
        "abs_max": [
            {"param": "VRWM reverse standoff voltage", "value": 8, "unit": "V"},
            {"param": "VBR breakdown voltage at IT = 1 mA, min to max",
             "value": vbr, "unit": "V"},
            {"param": "VC maximum clamping voltage at IPP", "value": 13.6,
             "unit": "V"}]}


def _tvs_gate(tmp_path, ext):
    fps = [("Q1", "C20", {"1": "/G", "2": "+12V"}),
           ("D1", "C21", {"1": "+12V", "2": "/G"}),
           ("R1", "C22", {"1": "+12V", "2": "/G"}, "x", "100k"),
           ("R2", "C22", {"1": "/G", "2": "/PD"}, "x", "100k")]
    return _run(_ws(tmp_path, fps, {"C20": PFET, "C21": ext}))


def _q1(p):
    return [v for v in p["violations"] if v["refs"] == ["Q1"]]


def test_a_tvs_free_text_breakdown_is_a_clamp(tmp_path):
    # VBR top 9.83 V: gate in 2.17..12 V, Vgs -9.83..0 inside +-10
    assert not _q1(_tvs_gate(tmp_path, _tvs()))
    assert cr.clamp_row({"pads": {"1": "a", "2": "b"}}, _tvs()) == ("1", 9.83)


def test_a_tvs_too_loose_for_the_gate_is_caught(tmp_path):
    p = _tvs_gate(tmp_path, _tvs("13.3 to 14.7", "SMF12A"))
    assert [v["kind"] for v in _q1(p)] == ["rating_reverse_polarity"]


def test_tvs_clamp_falls_back_to_vc_then_abs_row():
    pads = {"pads": {"1": "a", "2": "b"}}
    ext = _tvs()
    ext["abs_max"] = [r for r in ext["abs_max"] if "VBR" not in r["param"]]
    assert cr.clamp_row(pads, ext) == ("1", 13.6)
    abs_only = {"mpn": "BZT52C5V1", "pinout": ext["pinout"], "pin_ratings": [
        {"pins": ["K"], "kind": "voltage", "level": "abs_max", "max": 5.4,
         "ref": "A", "source": "test p.1"}]}
    assert cr.clamp_row(pads, abs_only) == ("1", 5.4)


def test_a_plain_or_bidirectional_diode_is_not_a_clamp():
    pads = {"pads": {"1": "a", "2": "b"}}
    plain = {"mpn": "1N4148W", "pinout": _tvs()["pinout"], "pin_ratings": [
        {"pins": ["K"], "kind": "voltage", "level": "abs_max", "max": 75,
         "ref": "A", "source": "test p.1 VR"}]}
    assert cr.clamp_row(pads, plain) is None
    assert cr.clamp_row(pads, _tvs(mpn="SMAJ8.0CA")) is None
    assert cr.clamp_row(pads, _tvs(), {"mpn": "X", "attributes": [
        {"name": "Type", "value": "TVS"},
        {"name": "Polarity", "value": "Bidirectional"}]}) is None


SINK = {"mpn": "TESTSINK", "pinout": [
    {"pin": "1", "name": "VIN", "type": "input"},
    {"pin": "2", "name": "GND", "type": "ground"}]}


def _sink(vmax):
    return {**SINK, "abs_max": [{"param": "Input voltage VIN", "value": vmax,
                                 "unit": "V"}]}


def _over(p, ref="U9"):
    return [v for v in p["violations"] if v["refs"] == [ref]
            and v["kind"] == "rating_over_voltage"]


def _kside(tmp_path, vmax, extra=(), parts=None):
    # /X: fed from a floating /IN, clamped by D1 with K on /X and A on GND
    fps = [("R1", "C22", {"1": "/IN", "2": "/X"}, "x", "1k"),
           ("D1", "C21", {"1": "/X", "2": "GND"}),
           ("U9", "C30", {"1": "/X", "2": "GND"}),
           ("J1", "C31", {"1": "+5V", "2": "GND"}), *extra]
    return _run(_ws(tmp_path, fps, {"C21": _zener(3.3), "C30": _sink(vmax),
                                    **(parts or {})}))


def test_a_clamp_with_its_cathode_on_the_unknown_net_bounds_it(tmp_path):
    hit = _over(_kside(tmp_path, 1))       # /X in 0..3.3 V
    assert hit and hit[0]["bound_v"] == [0.0, 3.3]
    assert not _over(_kside(tmp_path / "b", 5))


def test_clamped_nets_chain_through_a_resistor(tmp_path):
    # /Z fed from /X (0..3.3 V), clamped by D2 from +5V: 1.7..3.3 V
    extra = [("R2", "C22", {"1": "/X", "2": "/Z"}, "x", "1k"),
             ("D2", "C21", {"1": "+5V", "2": "/Z"}),
             ("U8", "C30", {"1": "/Z", "2": "GND"})]
    p = _kside(tmp_path, 3.0, extra)
    hit = _over(p, "U8")
    assert hit and hit[0]["bound_v"] == [pytest.approx(1.7), pytest.approx(3.3)]


DRIVER = {"mpn": "TESTDRVOUT", "pinout": [
    {"pin": "1", "name": "VDD", "type": "power_in"},
    {"pin": "2", "name": "VSS", "type": "ground"},
    {"pin": "3", "name": "OUT", "type": "output"}]}


def _driven(tmp_path, vmax, supply="+3V3", ref="U1"):
    fps = [(ref, "C40", {"1": supply, "2": "GND", "3": "/D"}),
           ("U9", "C30", {"1": "/D", "2": "GND"}),
           ("J1", "C31", {"1": "+5V", "2": "GND"})]
    return _run(_ws(tmp_path, fps, {"C40": DRIVER, "C30": _sink(vmax)}))


def test_a_net_is_bounded_by_its_drivers_rail(tmp_path):
    hit = _over(_driven(tmp_path, 1))
    assert hit and hit[0]["bound_v"] == [0.0, 3.3] and "U1" in hit[0]["msg"]
    p = _driven(tmp_path / "b", 3.6)
    assert not _over(p)
    assert not [v for v in p["violations"] if v["refs"] == ["U1"]
                and v["kind"] != "rating_unrated"]


def test_a_driver_with_an_unknown_supply_bounds_nothing(tmp_path):
    assert not _over(_driven(tmp_path, 1, supply="/VX"))


def test_a_connector_is_not_a_driver(tmp_path):
    # J2's far side is off the board: its supply pins don't bound its others
    assert not _over(_driven(tmp_path, 1, ref="J2"))


SINK_MIN = {"mpn": "TESTSINKMIN", "pinout": SINK["pinout"], "pin_ratings": [
    {"pins": ["VIN"], "kind": "voltage", "level": "abs_max", "min": -0.3,
     "max": 6, "ref": "GND", "source": "test p.1"}]}


def _rev(p, ref="U9"):
    return [v for v in p["violations"] if v["refs"] == [ref]
            and v["kind"] == "rating_reverse_polarity"]


def test_a_driver_with_no_ground_pin_swings_to_the_lowest_rail(tmp_path):
    # U1's VSS is not on the board: its low side is -5V, not 0 V (#1006)
    fps = [("U1", "C40", {"1": "+3V3", "3": "/D"}),
           ("U9", "C32", {"1": "/D", "2": "GND"}),
           ("J1", "C31", {"1": "+5V", "2": "-5V", "3": "GND"})]
    hit = _rev(_run(_ws(tmp_path, fps, {"C40": DRIVER, "C32": SINK_MIN})))
    assert hit and hit[0]["bound_v"] == [-5.0, 3.3]
    assert cr._supply_rail({"pads": {"1": "+3V3", "3": "/D"}}, DRIVER,
                           {"+3V3": 3.3}, -5.0) == (-5.0, 3.3)
    assert cr._supply_rail({"pads": {"1": "+3V3", "2": "GND", "3": "/D"}},
                           DRIVER, {"+3V3": 3.3, "GND": 0.0}, -5.0) == (0.0, 3.3)


def _pulled(tmp_path, vmax, supply="+5V"):
    # U1's OUT is typed push-pull but pulled up through R1 from /P, which
    # U2 drives from its own rail: if OUT is open drain in fact, /D reaches
    # U2's rail, not U1's (#1006)
    fps = [("U1", "C40", {"1": "+3V3", "2": "GND", "3": "/D"}),
           ("U2", "C40", {"1": supply, "2": "GND", "3": "/P"}),
           ("R1", "C22", {"1": "/P", "2": "/D"}, "x", "4.7k"),
           ("U9", "C30", {"1": "/D", "2": "GND"}),
           ("J1", "C31", {"1": "+5V", "2": "GND"})]
    return _run(_ws(tmp_path, fps, {"C40": DRIVER, "C30": _sink(vmax)}))


def test_a_pull_up_past_an_outputs_rail_reaches_the_net(tmp_path):
    hit = _over(_pulled(tmp_path, 3.6))
    assert hit and hit[0]["bound_v"] == [0.0, 5.0]
    assert "feed resistors" in hit[0]["msg"]
    # a pull-up from a net inside the driver's own rail keeps /D in it
    assert not _over(_pulled(tmp_path / "b", 3.6, supply="+3V3"))


def test_a_driver_is_judged_when_the_bound_leaves_its_rail(tmp_path):
    bounds, _ = cr.unstated_net_bounds(
        [{"ref": "U1", "lcsc": "C40", "pads": {"1": "+3V3", "2": "GND",
                                                "3": "/D"}},
         {"ref": "R1", "lcsc": "C22", "value": "4.7k",
          "pads": {"1": "+5V", "2": "/D"}}],
        {"C40": DRIVER}, {}, {"+3V3": 3.3, "GND": 0.0, "+5V": 5.0})
    assert bounds["/D"][:2] == (0.0, 5.0) and "U1" not in bounds["/D"][3]


def test_a_push_pull_output_is_held_by_its_rail_past_an_unknown_net():
    # R1 to /EXT, which nothing bounds, does not lift /D off U1's rail
    bounds, _ = cr.unstated_net_bounds(
        [{"ref": "U1", "lcsc": "C40", "pads": {"1": "+3V3", "2": "GND",
                                                "3": "/D"}},
         {"ref": "R1", "lcsc": "C22", "value": "100",
          "pads": {"1": "/D", "2": "/EXT"}},
         {"ref": "J2", "lcsc": "C31", "pads": {"1": "/EXT"}}],
        {"C40": DRIVER}, {}, {"+3V3": 3.3, "GND": 0.0})
    assert bounds["/D"][:2] == (0.0, 3.3) and "U1" in bounds["/D"][3]


OC_DRIVER = {"mpn": "TESTOC", "pinout": [
    *DRIVER["pinout"][:2], {"pin": "3", "name": "OUT",
                            "type": "open_collector"}]}


def test_a_soft_hull_does_not_judge_the_driver_or_lift_a_push_pull_pin():
    # lumina-par's /FAULT shape: an open-collector pin pulled up to its own
    # rail, with hysteresis into a net nothing holds; the 0..48 V hull that
    # net gives is the board's rails, not a pull past the pin's rail
    for drv, lcsc in ((OC_DRIVER, "C41"), (DRIVER, "C40")):
        bounds, _ = cr.unstated_net_bounds(
            [{"ref": "U1", "lcsc": lcsc, "pads": {"1": "+3V3", "2": "GND",
                                                  "3": "/D"}},
             {"ref": "R1", "lcsc": "C22", "value": "100k",
              "pads": {"1": "+3V3", "2": "/D"}},
             {"ref": "R2", "lcsc": "C22", "value": "100k",
              "pads": {"1": "/D", "2": "/HYS"}},
             {"ref": "J2", "lcsc": "C31", "pads": {"1": "/HYS"}}],
            {lcsc: drv}, {}, {"+3V3": 3.3, "GND": 0.0, "+48V": 48.0})
        assert "U1" in bounds["/D"][3]
        want = (0.0, 48.0) if drv is OC_DRIVER else (0.0, 3.3)
        assert bounds["/D"][:2] == want


# --- #1006: clamp power on driven nets, connector levels, negative grounds -

def _drv_amps(ma):
    return {**DRIVER, "pin_ratings": [
        {"pins": ["OUT"], "kind": "current", "level": "abs_max",
         "min": -ma / 1000, "max": ma / 1000, "source": "test p.2 IOUT"}]}


def _clamped_gpio(tmp_path, supply="+5V", ma=25.0, pd="200mW", vz=3.3):
    # U1 drives /D from its rail; D1 (K on /D, A on GND) clamps it at Vz
    fps = [("U1", "C40", {"1": supply, "2": "GND", "3": "/D"}),
           ("D1", "C21", {"1": "/D", "2": "GND"}),
           ("J1", "C31", {"1": "+5V", "2": "GND"})]
    bom = [{"lcsc": "C21", "mpn": "TESTZ", "attributes": [
        {"name": "Pd - Power Dissipation", "value": pd}]}]
    parts = {"C40": DRIVER if ma is None else _drv_amps(ma),
             "C21": _zener(vz)}
    return _run(_ws(tmp_path, fps, parts, bom=bom))


def _d1_power(p):
    return [v for v in p["violations"] if v["refs"] == ["D1"]
            and v["kind"] == "rating_over_current"]


def test_a_clamp_a_driver_pushes_past_is_judged_on_the_pins_current(tmp_path):
    # 5 V rail into a 3.3 V zener: 25 mA x 3.3 V = 83 mW, over a 50 mW Pd
    hit = _d1_power(_clamped_gpio(tmp_path, pd="50mW"))
    assert hit and "U1.3 at its rated 25 mA" in hit[0]["msg"]
    assert not _d1_power(_clamped_gpio(tmp_path / "b", pd="200mW"))


def test_a_drivers_rail_inside_the_clamp_puts_nothing_through_it(tmp_path):
    # a 3.3 V rail into a 5.1 V zener never conducts, whatever the Pd
    p = _clamped_gpio(tmp_path, supply="+3V3", pd="0.1mW", vz=5.1)
    assert not _d1_power(p)
    assert not any("D1" in n for n in p.get("not_judged") or [])


def test_a_clamp_with_no_driver_current_row_is_not_judged(tmp_path):
    p = _clamped_gpio(tmp_path, ma=None, pd="0.1mW")
    assert not _d1_power(p)
    assert any(n.startswith("D1: a clamp on /D") and "not judged" in n
               for n in p["not_judged"])


def _usb(tmp_path, vmax, value="USB_C_Receptacle", pin_name="D+"):
    fp = "USB_C" if "USB" in value else "Conn"
    fps = [("J2", "C50", {"A6": "/DP", "A1": "GND"}, fp, value),
           ("U9", "C30", {"1": "/DP", "2": "GND"}),
           ("J1", "C31", {"1": "+5V", "2": "GND"})]
    conn = {"mpn": "TESTCONN", "pinout": [
        {"pin": "A6", "name": pin_name, "type": "passive"},
        {"pin": "A1", "name": "GND", "type": "ground"}]}
    return _run(_ws(tmp_path, fps, {"C30": _sink(vmax), "C50": conn}))


def test_a_usb_data_pin_bounds_its_net_at_the_signal_level(tmp_path):
    hit = _over(_usb(tmp_path, 3.3))
    assert hit and hit[0]["bound_v"] == [0.0, 3.6]
    assert "USB 2.0 D+/D- signal level" in hit[0]["msg"]
    assert not _over(_usb(tmp_path / "b", 5))
    # the same pin on a connector that is not USB says nothing
    assert not _over(_usb(tmp_path / "c", 3.3, value="Conn_02"))
    assert not _over(_usb(tmp_path / "d", 3.3, pin_name="SBU1"))


def test_a_declared_interface_level_bounds_the_connectors_net(tmp_path):
    fps = [("J2", "C31", {"1": "/RX", "2": "GND"}),
           ("U9", "C30", {"1": "/RX", "2": "GND"}),
           ("J1", "C31", {"1": "+5V", "2": "GND"})]
    for sub, vmax, want in (("a", 3.6, True), ("b", 6, False)):
        ws = _ws(tmp_path / sub, fps, {"C30": _sink(vmax)})
        c = ws / "constraints.json"
        c.write_text(json.dumps({"interface_levels": [
            {"ref": "J2", "max": 5.0, "pins": ["1"]}]}))
        hit = _over(_run(ws, c))
        assert bool(hit) is want
        if want:
            assert hit[0]["bound_v"] == [0.0, 5.0]
            assert "constraints interface_levels J2" in hit[0]["msg"]
    # no entry: J2's far side is off the board, so /RX stays unknown
    assert not _over(_run(_ws(tmp_path / "c", fps, {"C30": _sink(3.6)})))


def _neg_gnd(tmp_path, gnd="-5V", vin="-3V"):
    # U9's only ground pin sits on -5V: its no-ref -0.3 V minimum is
    # measured from there, not from 0 V
    fps = [("U9", "C32", {"1": vin, "2": gnd}),
           ("J1", "C31", {"1": "+5V", "2": "-5V", "3": "GND"})]
    return _run(_ws(tmp_path, fps, {"C32": {**SINK_MIN, "pin_ratings": [
        {**SINK_MIN["pin_ratings"][0], "ref": None}]}}))


def test_a_rating_with_no_reference_is_measured_from_a_negative_ground(
        tmp_path):
    assert not _rev(_neg_gnd(tmp_path))          # -3 V is +2 V over -5V
    hit = _rev(_neg_gnd(tmp_path / "b", gnd="GND"))   # -3 V under GND
    assert hit
    # +3V3 over -5V is 8.3 V, past the 6 V maximum
    assert _over(_neg_gnd(tmp_path / "c", vin="+3V3"))
    assert not _over(_neg_gnd(tmp_path / "d", gnd="GND", vin="+3V3"))


def test_a_driver_on_a_rail_past_the_usb_level_leaves_the_net_unbounded(
        tmp_path):
    # a PD controller's CC pin on a VBUS-fed supply: its rail is what it is
    # fed from, not what it drives CC to, so neither bound is taken
    def run(sub, rail):
        fps = [("J2", "C50", {"A6": "/DP", "A1": "GND"}, "USB_C",
                "USB_C_Receptacle"),
               ("U9", "C30", {"1": "/DP", "2": "GND"}),
               ("U1", "C40", {"1": rail, "2": "GND", "3": "/DP"}),
               ("J1", "C31", {"1": "+5V", "2": "GND"})]
        conn = {"mpn": "TESTCONN", "pinout": [
            {"pin": "A6", "name": "D+", "type": "passive"},
            {"pin": "A1", "name": "GND", "type": "ground"}]}
        return _run(_ws(tmp_path / sub, fps, {"C30": _sink(3.3),
                                              "C40": DRIVER, "C50": conn}))
    p = run("a", "+5V")
    assert not _over(p)
    assert any(n.startswith("/DP: U1.3 sits on a rail past J2.A6")
               for n in p["not_judged"])
    # a 3.3 V rail stays inside the level: the net is bounded at 3.6 V
    hit = _over(run("b", "+3V3"))
    assert hit and hit[0]["bound_v"] == [0.0, 3.6]


# ---------------------------------------- pins by where the copper puts them

# A regulator whose land drawing has a post (pin 4) off the row, so only one
# turn fits: pins 1 VIN, 2 GND, 3 VOUT in a row, 4 the post above pin 2
REG = {"mpn": "TESTREG", "pinout": [
    {"pin": "1", "name": "VIN", "type": "power_in"},
    {"pin": "2", "name": "GND", "type": "ground"},
    {"pin": "3", "name": "VOUT", "type": "power_out"},
    {"pin": "4", "name": "NC", "type": "no_connect"}],
    "abs_max": [
        {"param": "Voltage on VIN", "value": "-0.3 to 20", "unit": "V"},
        {"param": "Voltage on VOUT", "value": "-0.3 to 6", "unit": "V"}],
    "land_pattern": {"pads": [
        {"number": "1", "at_mm": [-2.0, 0.0]},
        {"number": "2", "at_mm": [0.0, 0.0]},
        {"number": "3", "at_mm": [2.0, 0.0]},
        {"number": "4", "at_mm": [0.0, -3.0]}]}}


def test_pins_follow_the_copper_not_the_numbers(tmp_path):
    """#843. The quiet twin: symbol and footprint numbered mirrored alike
    (hifi-receiver's PLR237), so pad 3 is the copper under the datasheet's
    VIN and carries +12V. By number that was VOUT on 12 V; by place it is
    VIN, and quiet. The planted fault: the numbers agree with the
    datasheet, but the footprint's pad 1 (on +12V) sits where the drawing
    has VOUT."""
    twin = _ws(tmp_path, [("U1", "C7", {"3": ("+12V", -2.0, 0.0),
                                        "2": ("GND", 0.0, 0.0),
                                        "1": ("+5V", 2.0, 0.0),
                                        "4": ("", 0.0, -3.0)})], {"C7": REG})
    p = _run(twin)
    assert _errors(p) == []
    assert p["pins_by_position"] == {"U1": {"1": "3", "3": "1"}}
    by_number = json.loads(json.dumps(REG))
    del by_number["land_pattern"]
    p = _run(_ws(tmp_path / "n", [("U1", "C7", {"3": ("+12V", -2.0, 0.0),
                                                "2": ("GND", 0.0, 0.0),
                                                "1": ("+5V", 2.0, 0.0)})],
                 {"C7": by_number}))
    assert [e["pin"] for e in _errors(p)] == ["U1.3"]   # the old false error
    fault = _ws(tmp_path / "f", [("U1", "C7", {"1": ("+12V", 2.0, 0.0),
                                               "2": ("GND", 0.0, 0.0),
                                               "3": ("+5V", -2.0, 0.0),
                                               "4": ("", 0.0, -3.0)})],
                {"C7": REG})
    errs = _errors(_run(fault))
    assert [(e["kind"], e["pin"], e["board_pad"]) for e in errs] == \
        [("rating_over_voltage", "U1.3", "1")]
    assert "the footprint's pad 1" in errs[0]["msg"]


def test_a_row_with_no_fixed_turn_keeps_the_numbers(tmp_path):
    """Three pins in a row fit at 0 and 180 degrees with different pins:
    nothing says which, so pins stay matched by number, and the facts
    say why."""
    row = json.loads(json.dumps(REG))
    row["land_pattern"]["pads"] = row["land_pattern"]["pads"][:3]
    p = _run(_ws(tmp_path, [("U1", "C7", {"3": ("+12V", -2.0, 0.0),
                                          "2": ("GND", 0.0, 0.0),
                                          "1": ("+5V", 2.0, 0.0)})],
                 {"C7": row}))
    assert p["pins_by_position"] == {}
    assert "more than one turn" in p["pins_by_number_why"]["U1"]
    assert [e["pin"] for e in _errors(p)] == ["U1.3"]


# ------------------------------- FB networks beyond a plain divider (#847)

def _net_ws(tmp_path, net, pads=None, ext=None, bom=None):
    """AP63200 (Vref 0.8 V) with FB on FB_DIV and the parts in net:
    (ref, lcsc, pads[, footprint[, value]])."""
    fps = [("U4", "C9", pads or _buck_pads("FB_DIV"))] + list(net)
    return _ws(tmp_path, fps, {"C9": ext or _fixed_buck("AP63200WU-7",
                                                         **ADJ)}, bom)


def _r(ref, a, b, val, fp="R_0402"):
    return (ref, "C1", {"1": a, "2": b}, fp, val)


def _bot():
    return _r("R2", "FB_DIV", "GND", "32k")


@pytest.mark.parametrize("rail,bad", [("+3V3", False), ("+5V", True)])
@pytest.mark.parametrize("top", [
    [_r("R1", "{rail}", "MID", "47k"), _r("R3", "MID", "FB_DIV", "53k")],
    [_r("R1", "{rail}", "FB_DIV", "200k"), _r("R3", "{rail}", "FB_DIV",
                                              "200k")],
    [_r("RFBT1", "{rail}", "FB_DIV", "100k")],
    [_r("R1", "{rail}", "FB_DIV", "100k"),
     ("C5", "C2", {"1": "{rail}", "2": "FB_DIV"}, "C_0402", "22p")],
    [_r("R0", "{rail}", "SENSE", "0R"), _r("R1", "SENSE", "FB_DIV", "100k")],
    [("NT1", "C3", {"1": "{rail}", "2": "SENSE"}, "NetTie_2_SMD", "NT"),
     _r("R1", "SENSE", "FB_DIV", "100k")],
    [_r("R1", "{rail}", "FB_DIV", "100k"), _r("R5", "FB_DIV", "GND", "DNP")],
], ids=["series", "parallel", "rfbt-ref", "ff-cap", "zero-ohm", "net-tie",
        "dnp-value"])
def test_fb_networks_that_still_make_3v3(tmp_path, top, rail, bad):
    """Every network here makes 3.3 V: on +3V3 it is quiet, on +5V the
    planted fault blocks."""
    net = [(ref, lcsc, {k: v.format(rail=rail) for k, v in pads.items()},
            *rest) for ref, lcsc, pads, *rest in top] + [_bot()]
    p = _run(_net_ws(tmp_path, net))
    errs = _out_errs(p)
    assert _unjudged(p) == []
    if not bad:
        assert errs == []
        return
    assert len(errs) == 1 and errs[0]["net"] == "+5V"
    assert errs[0]["limit_v"] == pytest.approx(3.3, abs=0.02)


@pytest.mark.parametrize("rail,bad", [("+3V3", False), ("+5V", True)])
def test_a_dnp_attribute_leaves_the_option_open(tmp_path, rail, bad):
    """R5 10k across R2 would make 9.9 V; it is DNP on the board."""
    ws = _net_ws(tmp_path, [_r("R1", rail, "FB_DIV", "100k"), _bot(),
                            _r("R5", "FB_DIV", "GND", "10k")])
    pcb = ws / "kicad" / "board.kicad_pcb"
    pcb.write_text(pcb.read_text().replace(
        '(property "Reference" "R5")', '(property "Reference" "R5") '
        '(attr smd dnp)'))
    p = _run(ws)
    errs = _out_errs(p)
    assert _unjudged(p) == []
    assert (len(errs) == 1) == bad
    if bad:
        assert "R5 is DNP" in errs[0]["msg"]


@pytest.mark.parametrize("rail,bad", [("+3V3", False), ("+12V", True)])
def test_a_trim_pot_is_judged_over_its_travel(tmp_path, rail, bad):
    """R1 91k + RV1 20k pot (as a rheostat) over R2 32k: 0.8 * (1 + 91..111 /
    32) is about 3.1-3.6 V with the pot's 20 %; 3.3 V is in reach, 12 V is
    not."""
    net = [_r("R1", rail, "TR", "91k"),
           ("RV1", "C4", {"1": "TR", "2": "FB_DIV", "3": "FB_DIV"},
            "Potentiometer_Bourns_3296W", "20k"), _bot()]
    p = _run(_net_ws(tmp_path, net))
    errs = _out_errs(p)
    assert _unjudged(p) == []
    assert (len(errs) == 1) == bad
    if bad:
        assert "can be trimmed to" in errs[0]["msg"]


@pytest.mark.parametrize("rail,bad", [("+3V3", False), ("+5V", True)])
def test_the_divider_reads_its_rail_through_a_bead(tmp_path, rail, bad):
    """The top resistor senses VOUT_RAW, which reaches the stated rail through
    a ferrite bead."""
    net = [_r("R1", "VOUT_RAW", "FB_DIV", "100k"), _bot(),
           ("FB1", "C6", {"1": "VOUT_RAW", "2": rail}, "L_0603", "600R"),
           ("C7", "C2", {"1": "VOUT_RAW", "2": "GND"}, "C_0805", "22u")]
    p = _run(_net_ws(tmp_path, net))
    errs = _out_errs(p)
    assert (len(errs) == 1) == bad
    if bad:
        assert errs[0]["net"] == "+5V" and "bead" in errs[0]["msg"]


def test_a_bead_hop_is_not_taken_from_the_switch_node(tmp_path):
    """The regulator's own pad on the net (its SW) stops the hop."""
    net = [_r("R1", "SW_NODE", "FB_DIV", "100k"), _bot(),
           ("L1", "C6", {"1": "SW_NODE", "2": "+5V"}, "L_1210", "4.7u")]
    assert _out_errs(_run(_net_ws(tmp_path, net))) == []


@pytest.mark.parametrize("net,why", [
    ([_r("R1", "+5V", "FB_DIV", "100k"), _bot(),
      ("U5", "C8", {"1": "FB_DIV", "2": "GND"}, "SOT-23", "DAC")],
     "also carries U5"),
    ([_r("R1", "+5V", "FB_DIV", "100k"), _bot(),
      _r("R3", "+12V", "FB_DIV", "1M")], "more than one source"),
    ([_bot()], "no resistor on FB_DIV reaches"),
    ([_r("R1", "+5V", "FB_DIV", "100k"), _r("R2", "FB_DIV", "GND", "TBD")],
     "doesn't parse"),
], ids=["other-part", "two-sources", "no-top", "bad-value"])
def test_fb_networks_it_cannot_read_are_not_judged(tmp_path, net, why):
    """Each would make 3.3 V or worse on +5V; the check names why it can't
    judge them and leaves them out of the errors."""
    p = _run(_net_ws(tmp_path, net))
    assert _out_errs(p) == []
    [u] = _unjudged(p)
    assert why in u["msg"] and u["severity"] == "warning"
    assert any(n.startswith("U4 output:") and why in n
               for n in p["not_judged"])


@pytest.mark.parametrize("key", ["block", "ref", "refs"])
@pytest.mark.parametrize("vout,bad", [(3.3, False), (5.0, True)])
def test_a_block_naming_the_part_in_its_own_field(tmp_path, key, vout, bad):
    ws = _net_ws(tmp_path, [_r("R1", "VOUT_A", "FB_DIV", "100k"), _bot()])
    c = tmp_path / "constraints.json"
    c.write_text(json.dumps({"blocks": [
        {"name": "main buck", key: ["U4"] if key == "refs" else "U4",
         "operating_point": {"vout_v": vout}}]}))
    errs = _out_errs(_run(ws, c))
    assert (len(errs) == 1) == bad
    if bad:
        assert "vout_v = 5" in errs[0]["msg"]


@pytest.mark.parametrize("rail,bad", [("+3V3", False), ("+5V", True)])
def test_a_combined_fb_vout_pad_name_is_read_as_fb(tmp_path, rail, bad):
    ext = _fixed_buck("AP63200WU-7", **ADJ)
    ext["pinout"][0] = {"pin": "1", "name": "FB/VOUT", "type": "input"}
    p = _run(_net_ws(tmp_path, [_r("R1", rail, "FB_DIV", "100k"), _bot()],
                     ext=ext))
    assert (len(_out_errs(p)) == 1) == bad


# ------------------------------------------------- ratings over a TVS (#500)

BUS_DRIVER = {"mpn": "TEST-DRV", "pinout": [
    {"pin": "1", "name": "VM", "type": "power_in"},
    {"pin": "2", "name": "GND", "type": "ground"},
    {"pin": "3", "name": "VDRAIN", "type": "power_in"}],
    "abs_max": [{"param": "VM supply", "value": "-0.3~80", "unit": "V"}],
    "pin_ratings": [
        {"pins": ["VM"], "kind": "voltage", "level": "abs_max",
         "min": -0.3, "max": 80, "source": "seeded VM row"},
        {"pins": ["VDRAIN"], "kind": "voltage", "level": "abs_max",
         "min": -0.3, "max": 102, "source": "seeded VDRAIN row"}]}


def _cap_bom(lcsc, volts):
    return {"lcsc": lcsc, "mpn": f"CAP-{lcsc}", "attributes": [
        {"name": "Voltage Rated", "value": f"{volts}V"}]}


def _bus_ws(tmp_path, tvs_mpn="SMCJ58A", rail="+56V", tvs_attrs=None,
            tvs_ext=None, cap_v=63):
    """The #500 bus: a TVS (cathode pad 1 on the rail, anode on GND), a
    capacitor rated cap_v and a driver whose VM pin is rated 80 V."""
    fps = [("C1", "C11", {"1": rail, "2": "GND"}),
           ("U1", "C12", {"1": rail, "2": "GND", "3": rail})]
    bom = [_cap_bom("C11", cap_v)]
    parts = {"C12": BUS_DRIVER}
    if tvs_mpn:
        fps.append(("D1", "C10", {"1": rail, "2": "GND"}, "Diode_SMD:D_SMC"))
        bom.append({"lcsc": "C10", "mpn": tvs_mpn,
                    "attributes": tvs_attrs or []})
        if tvs_ext:
            parts["C10"] = tvs_ext
    return _ws(tmp_path, fps, parts, bom)


def _clamp_hits(p):
    return sorted((v["refs"][0], v["pin"], v["level"], v["net_v"],
                   v["limit_v"]) for v in p["violations"]
                  if v["kind"] == "rating_over_clamp")


def test_tvs_on_bus_judges_parts_at_breakdown_and_clamp(tmp_path):
    """The issue's known answer: SMCJ58A (71.2 V breakdown max, 93 V
    clamp) on a 56 V bus breaks a 63 V capacitor before it conducts and
    an 80 V supply pin in a surge; a 102 V pin holds."""
    p = _run(_bus_ws(tmp_path))
    assert _clamp_hits(p) == [("C1", None, "breakdown", 71.2, 63.0),
                              ("U1", "U1.1", "clamp", 93.0, 80.0)]
    for v in p["violations"]:
        if v["kind"] == "rating_over_clamp":
            assert v["severity"] == "warning" and v["clamp"] == ["D1"]
            assert "SMCJ58A" in v["msg"] and "Vishay" in v["msg"]
    rail = p["clamped_rails"]["+56V"]
    assert (rail["vbr"], rail["vc"], rail["by"]) == (71.2, 93.0, ["D1"])
    assert rail["judged"] == ["C1", "U1.1", "U1.3"]


def test_no_tvs_no_clamp_finding(tmp_path):
    p = _run(_bus_ws(tmp_path, tvs_mpn=None))
    assert _clamp_hits(p) == [] and p["clamped_rails"] == {}


def test_bidirectional_tvs_is_not_read_as_a_clamp(tmp_path):
    p = _run(_bus_ws(tmp_path, tvs_mpn="SMCJ58CA"))
    assert _clamp_hits(p) == [] and p["clamped_rails"] == {}


def test_a_part_the_nominal_breaks_is_left_to_over_voltage(tmp_path):
    p = _run(_bus_ws(tmp_path, cap_v=50))
    assert [h[0] for h in _clamp_hits(p)] == ["U1"]
    assert any(v["kind"] == "rating_over_voltage" and v["refs"] == ["C1"]
               for v in p["violations"])


def test_tvs_extraction_rows_win_over_its_part_number(tmp_path):
    """A free-text V(BR)/VC extraction is read before the family table."""
    ext = {"mpn": "SMCJ58A", "pinout": [
        {"pin": "1", "name": "K", "type": "passive"},
        {"pin": "2", "name": "A", "type": "passive"}], "abs_max": [
        {"param": "VBR breakdown voltage at IT, min to max",
         "value": "64.4 to 62.0", "unit": "V"},
        {"param": "VC maximum clamping voltage at IPP", "value": 78,
         "unit": "V"}]}
    p = _run(_bus_ws(tmp_path, tvs_ext=ext))
    assert _clamp_hits(p) == [("C1", None, "breakdown", 64.4, 63.0)]
    assert "datasheet extraction" in p["clamped_rails"]["+56V"]["src"][0]


def test_listing_breakdown_is_not_the_clamp_top(tmp_path):
    """LCSC's "Voltage - Breakdown" is the range's bottom: never the
    breakdown max. Its "Clamping Voltage" is read as VC."""
    attrs = [{"name": "Voltage - Breakdown (Min)", "value": "64.4V"},
             {"name": "Clamping Voltage", "value": "90V"}]
    p = _run(_bus_ws(tmp_path, tvs_mpn="TVS-NO-FAMILY-58", tvs_attrs=attrs))
    rail = p["clamped_rails"]["+56V"]   # no family row: vc only
    assert (rail["vbr"], rail["vc"]) == (None, 90.0)
    assert [h[:3] for h in _clamp_hits(p)] == [("C1", None, "clamp"),
                                               ("U1", "U1.1", "clamp")]
    p = _run(_bus_ws(tmp_path / "b", tvs_attrs=attrs))
    rail = p["clamped_rails"]["+56V"]
    assert (rail["vbr"], rail["vc"]) == (71.2, 90.0)


def test_constraints_clamp_v_states_the_clamp(tmp_path):
    ws = _bus_ws(tmp_path, tvs_mpn=None, rail="+12V", cap_v=16)
    cons = _cons(tmp_path, voltages=[
        {"net": "+12V", "voltage": 12, "clamp_v": 20, "breakdown_v": 15}])
    p = _run(ws, cons)
    assert _clamp_hits(p) == [("C1", None, "clamp", 20.0, 16.0)]
    assert p["clamped_rails"]["+12V"]["by"] == ["constraints"]


def test_mpn_clamp_reads_the_family_row():
    import diode_vr
    r = diode_vr.mpn_clamp("SMCJ58A")
    assert (r["vbr"], r["vc"], r["bi"]) == (71.2, 93.0, False)
    assert "88394" in r["cite"]
    assert diode_vr.mpn_clamp("SMCJ58CA")["bi"] is True
    assert diode_vr.mpn_clamp("SS14") is None
    assert diode_vr.mpn_clamp("") is None


# ------------------------------------------------------------ internal regulator

CORE = {"mpn": "TESTDAC", "pinout": [
    {"pin": "1", "name": "DVCC", "type": "power_in"},
    {"pin": "2", "name": "GND", "type": "ground"},
    {"pin": "3", "name": "DVDD", "type": "power_out", "internal_v": 3.3}],
    "pin_ratings": [
        {"pins": ["DVCC"], "kind": "voltage", "level": "abs_max",
         "max": 6.0, "source": "test p.1"},
        {"pins": ["DVDD"], "kind": "voltage", "level": "abs_max",
         "max": 3.6, "source": "test p.1"}]}


def _dvdd_cap(rated):
    return [{"lcsc": "C9", "mpn": "CAP",
             "attributes": [{"name": "Voltage Rated", "value": rated}]}]


def test_internal_regulator_pin_with_decoupling_only_is_rated(tmp_path):
    """#843: a power_out pin with `internal_v` and only a cap to ground on
    its net puts the datasheet's internal value on the net. Planted: the cap
    is rated 2.5 V under a 3.3 V internal rail, an error; its quiet twin at
    6.3 V is clean and the pin itself is compared, not left unknown."""
    fps = [("U1", "C1", {"1": "+5V", "2": "GND", "3": "/DVDD"}),
           ("C2", "C9", {"1": "/DVDD", "2": "GND"})]
    p = _run(_ws(tmp_path, fps, {"C1": CORE}, _dvdd_cap("2.5V")))
    assert p["net_volts"]["/DVDD"]["v"] == pytest.approx(3.3)
    assert p["net_volts"]["/DVDD"]["src"].startswith(
        "internal regulator U1.3 DVDD")
    assert [(e["kind"], e["refs"]) for e in _errors(p)] == \
        [("rating_over_voltage", ["C2"])]
    ws = _ws(tmp_path / "b", fps, {"C1": CORE}, _dvdd_cap("6.3V"))
    assert _findings(_run(ws)) == []
    _, facts = cr.run_check(ws / "kicad" / "board.kicad_pcb", ws / "parts",
                            None, None)
    u1 = next(f for f in facts["ledger_facts"] if f["subject"] == "U1"
              and f["kind"] == "rating")
    assert u1["status"] == "verified" and u1["value"].startswith("2 ")  # DVCC and DVDD


def test_internal_regulator_net_with_more_than_decoupling_stays_unknown(tmp_path):
    """#843: an outside supply or a load on the net, a cap not to ground, or
    no `internal_v` keeps the net unknown: the internal value is only the
    rail when nothing else can set it."""
    base = [("U1", "C1", {"1": "+5V", "2": "GND", "3": "/DVDD"}),
            ("C2", "C9", {"1": "/DVDD", "2": "GND"})]
    for i, extra in enumerate([
            [("U2", "C1", {"1": "/DVDD", "2": "GND"})],
            [("C3", "C9", {"1": "/DVDD", "2": "/OTHER"})],
            [("R1", "", {"1": "/DVDD", "2": "GND"})]]):
        p = _run(_ws(tmp_path / str(i), base + extra, {"C1": CORE},
                     _dvdd_cap("2.5V")))
        assert "/DVDD" not in p["net_volts"], extra
    bare = {**CORE, "pinout": [{k: v for k, v in r.items()
                                if k != "internal_v"} for r in CORE["pinout"]]}
    p = _run(_ws(tmp_path / "n", base, {"C1": bare}, _dvdd_cap("2.5V")))
    assert "/DVDD" not in p["net_volts"]
