"""scripts/listing_survey.py: the disagreement rate by part class (#799)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import listing_survey as ls  # noqa: E402

SAMPLE = ROOT / "tests" / "fixtures" / "ratings" / "listing_sample.json"


def _board(root, name, parts, exts):
    d = root / name / "parts"
    d.mkdir(parents=True)
    (d / "parts.json").write_text(json.dumps({"parts": parts}))
    for lcsc, ext in exts.items():
        (d / f"{lcsc}.json").write_text(json.dumps(ext))


def _part(lcsc, attr, value, **kw):
    return {"lcsc": lcsc, "mpn": lcsc, "attributes": [{"name": attr, "value": value}], **kw}


CH224K = _part("C970725", "Voltage - Supply", "4V~30V", refdes=["U1"])
CH224K_EXT = {"abs_max": [{"param": "VODHV voltage on the VBUS pin", "value": 13.5, "unit": "V"}]}
WJ500V = _part("C8465", "Voltage Rating (Max)", "250V", refs=["J1"])
WJ500V_EXT = {"abs_max": [{"param": "rated_voltage_UL", "value": 300, "unit": "V"}]}
AO3400 = _part("C20917", "Drain to Source Voltage", "30V", refdes=["Q1"])
AO3400_EXT = {"abs_max": [{"param": "VDS drain-source voltage", "value": 30, "unit": "V"}]}


def test_real_parts_known_answers(tmp_path):
    _board(tmp_path, "b", [CH224K, WJ500V, AO3400],
           {"C970725": CH224K_EXT, "C8465": WJ500V_EXT, "C20917": AO3400_EXT})
    rows, skipped = ls.extracted_rows([tmp_path])
    got = {r["lcsc"]: (r["class"], r["side"]) for r in rows}
    assert got == {"C970725": ("ic", "high"), "C8465": ("connector", "low"),
                   "C20917": ("transistor", None)}
    assert skipped == []


def test_a_part_on_two_boards_counts_once(tmp_path):
    _board(tmp_path, "a", [AO3400], {"C20917": AO3400_EXT})
    _board(tmp_path, "b", [AO3400], {"C20917": AO3400_EXT})
    rows, _ = ls.extracted_rows([tmp_path])
    assert [r["lcsc"] for r in rows] == ["C20917"]


def test_no_voltage_row_is_counted_as_not_compared(tmp_path):
    _board(tmp_path, "a", [AO3400], {"C20917": {"abs_max": [
        {"param": "Id", "value": 5, "unit": "A"}]}})
    rows, skipped = ls.extracted_rows([tmp_path])
    assert rows == []
    assert [(s["lcsc"], s["why"]) for s in skipped] == [
        ("C20917", "no voltage row in the extraction")]


def test_a_part_with_no_extraction_is_not_in_the_extracted_rows(tmp_path):
    _board(tmp_path, "a", [AO3400], {})
    assert ls.extracted_rows([tmp_path]) == ([], [])


def test_class_falls_back_to_the_description():
    assert ls.part_class({"description": "N-Channel D2PAK MOSFETs ROHS"}) == "transistor"
    assert ls.part_class({"refs": ["D2"]}) == "diode"
    assert ls.part_class({"ref_prefix_hint": "C"}) == "capacitor"
    assert ls.part_class({}) == "other"


def test_wilson_upper_limit():
    assert ls.wilson_upper(0, 0) == 1.0
    assert 0.05 < ls.wilson_upper(0, 60) < 0.07
    assert ls.wilson_upper(1, 30) > 1 / 30


def test_table_splits_direction_and_counts_a_shared_datasheet_once():
    rows = [{"class": "resistor", "side": None, "datasheet": "cat"},
            {"class": "resistor", "side": None, "datasheet": "cat"},
            {"class": "ic", "side": "high", "datasheet": "x"},
            {"class": "ic", "side": "low", "datasheet": "y"}]
    t = {r["class"]: r for r in ls.table(rows)}
    assert t["resistor"]["parts"] == 2 and t["resistor"]["datasheets"] == 1
    assert (t["ic"]["high"], t["ic"]["low"], t["ic"]["rate"]) == (1, 1, 1.0)
    assert t["all"]["parts"] == 4 and t["all"]["high_rate"] == 0.25


def test_hand_read_sample_holds_its_known_answers():
    rows = {r["lcsc"]: r for r in ls.sample_rows(SAMPLE)}
    assert rows["C970725"]["side"] == "high"
    assert rows["C8465"]["side"] == "low"
    assert rows["C20917"]["agree"] and rows["C25076"]["agree"]
    # #810: three HX headers listed at their 1000 V withstand (rated 250 V),
    # KF128 listed at IEC 250 V (UL 300 V); USB4105 read from rev B3, 48 V.
    assert [r["lcsc"] for r in rows.values() if not r["agree"]] == [
        "C970725", "C8465", "C32713270", "C32713268", "C32713271", "C474952"]
    assert rows["C32713268"]["side"] == "high" and rows["C474952"]["side"] == "low"
    assert rows["C3020560"]["agree"]
    # a part LCSC served another maker's PDF for is left out
    assert not {"C110775", "C865172", "C865592", "C2918511"} & set(rows)


def test_a_corpus_listing_joins_its_extraction_by_lcsc_then_mpn(tmp_path):
    (tmp_path / "ext").mkdir()
    (tmp_path / "listings").mkdir()
    pin = {"pin_ratings": [{"pins": ["VCC"], "kind": "voltage", "level": "abs_max",
                            "min": -0.5, "max": 6.5, "source": "p.3 VCC -0.5 +6.5 V"}],
           "source_pdf": "x.pdf sha256 7e54baea5ee5b2c403f3"}
    (tmp_path / "ext" / "74LVC07AD-118.json").write_text(json.dumps(
        {**pin, "mpn": "74LVC07AD,118", "lcsc": "C6049"}))
    (tmp_path / "ext" / "TLV9001.json").write_text(json.dumps(
        {"mpn": "TLV9001IDBVR", "abs_max": [{"param": "Id", "value": 5, "unit": "A"}]}))
    for lcsc, mpn, v in (("C6049", "74LVC07AD,118", "1.65V~5.5V"),
                         ("C9", "TLV9001IDBVR", "1.8V~5.5V"),
                         ("C7", "NOEXT", "5V")):
        (tmp_path / "listings" / f"{lcsc}.json").write_text(json.dumps(
            {"lcsc": lcsc, "mpn": mpn, "description": "buffer IC",
             "attributes": [{"name": "Voltage - Supply", "value": v}]}))
    rows, skipped = ls.corpus_rows(tmp_path)
    assert [(r["lcsc"], r["class"], r["agree"], r["datasheet"]) for r in rows] == [
        ("C6049", "ic", True, "7e54baea5ee5")]
    assert [(s["lcsc"], s["why"]) for s in skipped] == [
        ("C9", "no voltage row in the extraction")]
