"""rating_fill: a two-pin passive's rating rows from its datasheet text.

Synthetic page text stands in for each series' datasheet (pages as
datasheet_extract.extract_pdf_text returns them), so no PDF is read. The
board cases build a one-part workspace and run check_ratings on it: a filled
capacitor on a rail over its rated voltage is an error, the same part on a
rail under it is quiet, and a bead stays unrated for voltage.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import check_ratings as cr  # noqa: E402
import datasheet_extract  # noqa: E402
import rating_fill as rf  # noqa: E402

PINS = [{"pin": "1", "name": "1", "type": "passive"},
        {"pin": "2", "name": "2", "type": "passive"}]


def _rec(mpn: str, **kw) -> dict:
    return {"mpn": mpn, "pinout": [dict(p) for p in PINS],
            "source_pdf": f"parts/datasheets/{mpn}.pdf (Test sheet {mpn}, "
                          "3 pages)"} | kw


GRM = "GRM155R71C104KA88D"
GRM_PAGES = [{"page": 0, "text": "Reference Sheet"},
             {"page": 1, "text": "GRM155R71C104KA88_ (0402, X7R:EIA, 0.1uF, "
                                 "DC16V)\nOperating Temp. Range -55 to 125°C"}]
RC = "RC0402FR-0710KL"
RC_PAGES = [{"page": 3, "text": "Table 2\nRC0402\n1/16 W -55 ℃ to 155 ℃ "
                                "50 V 100 V 100 V\nRC0603\n1/10 W -55 ℃ to "
                                "155 ℃ 75 V 150 V 150 V\n"}]
CL = "CL21A106KQCLNNC"
CL_PAGES = [{"page": 0, "text": "Rated Voltage 6.3V\nX5R (From -55℃ to 85℃"}]
BLM = "BLM21PG221SN1D"
BLM_PAGES = [{"page": 0, "text": "BLM21PG221SN1D 220±25% 220 2000 1500 0.05 \n"
                                 "Operating temperature range -55°C to "
                                 "+125°C"}]


def test_murata_grm_voltage_and_temperature_cite_the_page():
    rep = rf.fill(_rec(GRM), GRM_PAGES)
    new = rep["record"]
    v = [r for r in new["pin_ratings"] if r["kind"] == "voltage"]
    assert len(v) == 1
    assert (v[0]["pins"], v[0]["ref"], v[0]["max"], v[0]["min"]) == \
        (["1"], "2", 16.0, -16.0)
    assert v[0]["source"].startswith("rating_fill: Test sheet") \
        and "p2 header" in v[0]["source"]
    assert new["operating_temp_c"] == [-55.0, 125.0]
    assert "p2" in new["operating_temp_source"]
    jsonschema.validate(new, datasheet_extract.DATASHEET_SCHEMA)


def test_yageo_rc_voltage_is_sqrt_p_r_and_power_on_both_pins():
    new = rf.fill(_rec(RC), RC_PAGES)["record"]
    by = {r["kind"]: r for r in new["pin_ratings"]}
    assert by["voltage"]["max"] == pytest.approx(25.0)    # sqrt(1/16 x 10k)
    assert by["power"]["max"] == pytest.approx(0.0625)
    assert by["power"]["pins"] == ["1", "2"]
    assert "p4 Table 2 RC0402" in by["voltage"]["source"]
    assert new["operating_temp_c"] == [-55.0, 155.0]
    jsonschema.validate(new, datasheet_extract.DATASHEET_SCHEMA)


def test_samsung_cl_reads_rated_voltage_and_characteristic_range():
    new = rf.fill(_rec(CL), CL_PAGES)["record"]
    assert [r["max"] for r in new["pin_ratings"]] == [6.3]
    assert new["operating_temp_c"] == [-55.0, 85.0]


def test_bead_voltage_stays_unrated_current_and_temperature_fill():
    rep = rf.fill(_rec(BLM), BLM_PAGES)
    new = rep["record"]
    assert [r["kind"] for r in new["pin_ratings"]] == ["current"]
    assert new["pin_ratings"][0]["max"] == pytest.approx(2.0)
    assert new["operating_temp_c"] == [-55.0, 125.0]
    assert any("voltage stays unrated" in n for n in rep["notes"])
    jsonschema.validate(new, datasheet_extract.DATASHEET_SCHEMA)


def test_no_figure_writes_nothing():
    rec = _rec(GRM)
    rep = rf.fill(rec, [{"page": 0, "text": "nothing useful here"}])
    assert rep["filled"] == [] and "pin_ratings" not in rep["record"]


def test_conflicts_are_reported_not_written():
    # the header says 25 V but the part number's code 1C means 16 V
    pages = [{"page": 0, "text": "GRM155R71C104KA88_ (0402, X7R, DC25V)"}]
    rep = rf.fill(_rec(GRM), pages)
    assert "pin_ratings" not in rep["record"]
    assert any("conflict" in n for n in rep["notes"])
    # the record's own abs_max says 10 V: the 16 V read is not written
    rec = _rec(GRM, abs_max=[{"param": "Rated voltage", "value": 10,
                              "unit": "V"}])
    rep = rf.fill(rec, GRM_PAGES)
    assert not any(r["kind"] == "voltage"
                   for r in rep["record"].get("pin_ratings") or [])
    assert any("conflict" in n for n in rep["notes"])


def test_rerun_is_idempotent_and_hand_rows_are_kept():
    once = rf.fill(_rec(GRM), GRM_PAGES)["record"]
    twice = rf.fill(once, GRM_PAGES)["record"]
    assert twice == once
    hand = {"pins": ["1"], "kind": "current", "level": "recommended",
            "max": 1.5, "source": "p5 hand row"}
    rec = _rec(BLM, pin_ratings=[hand])
    new = rf.fill(rec, BLM_PAGES)["record"]
    assert new["pin_ratings"] == [hand]


def test_not_a_two_pin_passive_is_skipped():
    rec = _rec(GRM)
    rec["pinout"].append({"pin": "3", "name": "3", "type": "passive"})
    rep = rf.fill(rec, GRM_PAGES)
    assert rep["record"] is rec and rep["filled"] == []


def test_main_writes_records_and_reports(tmp_path):
    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / f"{GRM}.json").write_text(json.dumps(_rec(GRM)))
    (parts / "OTHER.json").write_text(json.dumps(_rec("XYZ123")))
    assert rf.main(["--parts", str(parts)]) == 0     # no PDF: reported only
    assert "pin_ratings" not in json.loads((parts / f"{GRM}.json").read_text())
    assert rf.main(["--parts", str(tmp_path / "missing")]) == 2


# ------------------------------------------------- check_ratings on a board

def _ws(tmp_path: Path, rail: str, rec: dict) -> Path:
    ws = tmp_path / "ws"
    (ws / "kicad").mkdir(parents=True)
    (ws / "parts").mkdir()
    mpn = rec["mpn"]
    (ws / "kicad" / "board.kicad_pcb").write_text(
        "(kicad_pcb (version 20240108)\n"
        f'  (footprint "x" (property "Reference" "C1") (property "LCSC" '
        f'"{mpn}")\n    (pad "1" smd rect (net "{rail}"))\n'
        '    (pad "2" smd rect (net "GND"))\n  )\n)')
    (ws / "parts" / "parts.json").write_text(json.dumps({"parts": []}))
    (ws / "parts" / f"{mpn}.json").write_text(json.dumps(rec))
    return ws


def _kinds(ws: Path) -> list[tuple[str, str]]:
    payload, _ = cr.run(["--pcb", str(ws / "kicad" / "board.kicad_pcb")])
    return [(v["kind"], v["severity"]) for v in payload["violations"]
            if v["kind"] != "rating_no_datasheet_read"]


def test_filled_cap_over_its_rating_is_caught_and_the_twin_is_quiet(tmp_path):
    rec = rf.fill(_rec(GRM), GRM_PAGES)["record"]
    # unfilled, the cap is only unrated: nothing judges its 16 V
    assert [k for k, _ in _kinds(_ws(tmp_path / "a", "+24V", _rec(GRM)))] \
        == ["rating_unrated"]
    # filled: 24 V across a 16 V cap is an error; pin 2's name "2" resolves
    # to GND (aliases() drops all-digit names)
    assert ("rating_over_voltage", "error") in \
        _kinds(_ws(tmp_path / "b", "+24V", rec))
    # the quiet twin: the same cap on 5 V draws nothing
    assert _kinds(_ws(tmp_path / "c", "+5V", rec)) == []


def test_filled_bead_still_unrated_for_voltage(tmp_path):
    rec = rf.fill(_rec(BLM), BLM_PAGES)["record"]
    assert any(k == "rating_unrated" for k, _ in
               _kinds(_ws(tmp_path, "+5V", rec)))


# ------------------------------------------------- a JST PH connector

PH = "S2B-PH-SM4-TB"
PH_PINS = [{"pin": "1", "name": "1", "type": "passive"},
           {"pin": "2", "name": "2", "type": "passive"},
           {"pin": "MP1", "name": "MP", "type": "passive"},
           {"pin": "MP2", "name": "MP", "type": "passive"}]
PH_TEXT = ("Specifications\n Current rating: 2 AɹAC/DC ïAWG #24ʣ\n"
           " Voltage rating: 100 VɹAC/DC\n Temperature range: ô40ˆ "
           "to +105ˆ (including temperature rise)")
# the catalog's PDF text: every glyph 0x1F below its letter
PH_PAGES = [{"page": 0, "text": "".join(
    chr(ord(c) - 0x1F) if 0x20 <= ord(c) <= 0x7E else c for c in PH_TEXT)}]


def _ph(**kw) -> dict:
    return _rec(PH, pinout=[dict(p) for p in PH_PINS]) | kw


def test_jst_ph_rates_its_contacts_not_its_mounting_pads():
    rep = rf.fill(_ph(), PH_PAGES)
    rows = rep["record"]["pin_ratings"]
    volt = [r for r in rows if r["kind"] == "voltage"]
    assert [(r["pins"], r["ref"], r["min"], r["max"]) for r in volt] == \
        [(["1"], "2", -100.0, 100.0)]
    cur = [r for r in rows if r["kind"] == "current"]
    assert cur[0]["pins"] == ["1", "2"] and cur[0]["max"] == 2.0
    assert "AWG #24" in cur[0]["source"] and "p1" in volt[0]["source"]
    assert rep["record"]["operating_temp_c"] == [-40.0, 105.0]


def test_jst_ph_ten_contacts_rate_each_neighbour_pair():
    pins = [{"pin": str(i), "name": f"P{i}", "type": "passive"}
            for i in range(1, 11)] + PH_PINS[2:]
    rec = _rec("S10B-PH-SM4-TB(LF)(SN)", pinout=pins)
    rows = rf.fill(rec, PH_PAGES)["record"]["pin_ratings"]
    assert [(r["pins"][0], r["ref"]) for r in rows if r["kind"] == "voltage"] \
        == [(str(i), f"P{i + 1}") for i in range(1, 10)]


def test_a_multi_pin_part_that_is_no_known_connector_is_skipped():
    rep = rf.fill(_rec("XYZ-4P", pinout=[dict(p) for p in PH_PINS]), PH_PAGES)
    assert rep["filled"] == [] and "skipped" in rep["notes"][0]


def test_pdf_for_finds_a_sanitized_file_name(tmp_path):
    (tmp_path / "datasheets").mkdir()
    pdf = tmp_path / "datasheets" / "12401610E4-2A.pdf"
    pdf.write_bytes(b"%PDF")
    assert rf.pdf_for({"mpn": "12401610E4#2A"}, tmp_path) == pdf


def test_filled_connector_over_its_rating_is_caught_and_the_twin_is_quiet(
        tmp_path):
    # unfilled: only unrated
    assert [k for k, _ in _kinds(_ws(tmp_path / "a", "+150V", _ph()))] == \
        ["rating_unrated"]
    rec = rf.fill(_ph(), PH_PAGES)["record"]
    assert ("rating_over_voltage", "error") in \
        _kinds(_ws(tmp_path / "b", "+150V", rec))
    assert _kinds(_ws(tmp_path / "c", "+12V", rec)) == []
