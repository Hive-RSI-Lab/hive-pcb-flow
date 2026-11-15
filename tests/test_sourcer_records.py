"""BOM and verify work from what the part-sourcer records at P3: one line per
distinct part, no refdes, the board's `LCSC Part` fields (easyeda2kicad's
name), LCSC's own attribute names, and no decoupling association on a board
with no IC (hwde-led-test, issue #5)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path[:0] = [str(SCRIPTS), str(SCRIPTS / "lib")]

import bom_cpl  # noqa: E402
import check_pdn  # noqa: E402
import check_ratings  # noqa: E402
import geom  # noqa: E402

LED_TEST = ROOT / "boards" / "hwde-led-test"
LED_PCB = LED_TEST / "kicad" / "hwde-led-test.kicad_pcb"


def sourcer_parts(tmp_path, drop=("refdes",)):
    data = json.loads((LED_TEST / "parts" / "parts.json").read_text())
    for line in data["parts"]:
        for k in drop:
            line.pop(k, None)
    p = tmp_path / "parts" / "parts.json"
    p.parent.mkdir()
    p.write_text(json.dumps(data))
    return p


def test_lcsc_field_names():
    for name in ("LCSC", "LCSC Part", "lcsc part #", "LCSC#", "LCSC Part No."):
        assert bom_cpl.is_lcsc_field(name), name
    for name in ("LCSC URL", "MPN", "Reference"):
        assert not bom_cpl.is_lcsc_field(name), name


def test_board_lcsc_part_fields_map_every_ref():
    got = {r: f["lcsc"] for r, f in bom_cpl.board_lcsc_map(LED_PCB).items()}
    assert got == {"C1": "C19666", "D1": "C12624", "J1": "C492401",
                   "R1": "C23179"}


def test_bom_rows_carry_lcsc_without_refdes(tmp_path):
    parts = sourcer_parts(tmp_path)
    board = bom_cpl.board_part_fields(LED_PCB)
    parts_map = {**bom_cpl.board_lcsc_map(LED_PCB),
                 **bom_cpl.load_parts_map(parts)}
    rows = bom_cpl.build_bom(
        [{"ref": r, "val": "", "package": ""} for r in board], parts_map)
    assert sorted(r["LCSC"] for r in rows) == sorted(
        ["C19666", "C12624", "C492401", "C23179"])


def test_refdes_less_lines_bind_to_board_refs(tmp_path):
    parts = sourcer_parts(tmp_path)
    recs = bom_cpl.bind_refs_by_lcsc(bom_cpl.load_parts_records(parts),
                                     bom_cpl.board_part_fields(LED_PCB))
    assert {r["mpn"]: r["refs"] for r in recs}["0603WAF4700T5E"] == ["R1"]


def test_named_ref_and_classed_line_are_not_rebound():
    recs = [{"refs": ["R1"], "lcsc": "C1", "cls": None},
            {"refs": [], "lcsc": "C1", "cls": None},
            {"refs": [], "lcsc": "C2", "cls": "hand_install"}]
    board = {"R1": {"lcsc": "C1"}, "R2": {"lcsc": "C1"}, "J9": {"lcsc": "C2"}}
    out = bom_cpl.bind_refs_by_lcsc(recs, board)
    assert [r["refs"] for r in out] == [["R1"], ["R2"], []]


def test_resistor_supply_max_is_a_voltage_rating():
    for name in ("Voltage-Supply(Max)", "Voltage - Supply (Max)",
                 "Working Voltage", "Voltage Rating (Max)", "Voltage Rated"):
        assert check_ratings.V_ATTR_RE.search(name), name
    for name in ("Voltage - Forward(Vf)", "Overload Voltage (Max)"):
        assert not check_ratings.V_ATTR_RE.search(name), name


def _ratings(tmp_path, voltages=None):
    parts = sourcer_parts(tmp_path)
    cons = {"voltages": [{"net": n, "voltage": v}
                         for n, v in voltages.items()]} if voltages else None
    out, _ = check_ratings.run_check(LED_PCB, parts.parent, cons, None)
    return out


def test_r1_rated_75v_from_its_attribute(tmp_path):
    unrated = [r for v in _ratings(tmp_path) if v["kind"] == "rating_unrated"
               for r in v["refs"]]
    assert "R1" not in unrated and "C1" not in unrated and "J1" not in unrated


def test_parts_over_their_rating_are_errors(tmp_path):
    over = {v["refs"][0]: v["limit_v"]
            for v in _ratings(tmp_path, {"+5V": 80, "/LED_A": 0})
            if v["kind"] == "rating_over_voltage"}
    assert over == {"R1": 75.0, "C1": 16.0}  # J1 is rated 250 V


def test_dropped_attributes_are_named(tmp_path):
    parts = sourcer_parts(tmp_path, drop=("refdes", "attributes"))
    out, _ = check_ratings.run_check(LED_PCB, parts.parent, None, None)
    msg = next(v["msg"] for v in out if v["kind"] == "rating_unrated")
    # R1 is rated anyway, from its chip package's limiting element voltage,
    # and C1 from its MPN's voltage code (CL10A475KO8NNNC, O = 16 V, #1307)
    assert "carries no attributes" in msg and "D1" in msg
    assert "R1" not in msg and "C1" not in msg


def test_bulk_cap_counted_on_a_rail_with_no_ic():
    bg = geom.load_board(LED_PCB)
    vs, facts = check_pdn.check_rail(bg, "+5V", 0.01, [])
    assert vs == []
    assert facts["caps"] == ["C1"] and facts["caps_from_board"] == ["C1"]
    assert facts["bulk_count"] == 1


def test_board_caps_join_the_recorded_ones():
    """decoupling.json records a cap against an IC pin; the rail's bulk cap
    by its connector (C1) is on the board, not in the file, and counts."""
    bg = geom.load_board(LED_PCB)
    assoc = [{"cap": "C9", "rail": "+5V", "value": "100nF"}]
    vs, facts = check_pdn.check_rail(bg, "+5V", 0.01, assoc)
    assert vs == []
    assert facts["caps"] == ["C1", "C9"] and facts["caps_from_board"] == ["C1"]
    assert facts["cap_count"] == 2 and facts["bulk_count"] == 1


def test_a_recorded_board_cap_is_counted_once():
    bg = geom.load_board(LED_PCB)
    vs, facts = check_pdn.check_rail(bg, "+5V", 0.01,
                                     [{"cap": "C1", "rail": "+5V"}])
    assert vs == [] and facts["caps_from_board"] == []
    assert facts["caps"] == ["C1"] and facts["cap_count"] == 1


def test_rail_with_no_cap_still_errors():
    bg = geom.load_board(LED_PCB)
    vs, _ = check_pdn.check_rail(bg, "/LED_A", None, [])
    assert [v["kind"] for v in vs] == ["pdn_undecoupled"]
