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


def test_the_category_names_an_ic_before_its_feature_words():
    # #1435: TCA6416's description lists "Built-in LED driver" and LP2985's
    # "Noise bypass terminal"; LCSC's category says what each part is
    tca = {"category": "I/O Expanders",
           "description": "I/O expander, 16-bit, I2C, built-in LED driver, TSSOP-24"}
    lp = {"category": "Voltage Regulators - Linear, Low Drop Out (LDO) Regulators",
          "description": "150mA LDO, 3.3V output, noise bypass terminal, SOT-23-5"}
    assert ls.part_class(tca) == "ic"
    assert ls.part_class(lp) == "ic"
    assert ls.part_class({"category": "Gate Drivers",
                          "description": "Half-bridge MOSFET gate driver"}) == "ic"
    assert ls.part_class({"category": "LED Drivers", "description": "LED diode driver"}) == "ic"


def test_a_passive_category_still_names_its_class():
    assert ls.part_class({"category": "Pin Headers", "description": "2.54mm"}) == "connector"
    assert ls.part_class({"category": "TVS", "description": "SMA"}) == "diode"
    # a reference designator still wins over the category
    assert ls.part_class({"refdes": ["D1"], "category": "LED Drivers"}) == "diode"


def test_a_part_with_no_category_reads_its_description_as_before():
    assert ls.part_class({"description": "Buck converter 2A SOT-23-6"}) == "ic"
    assert ls.part_class({"description": "LED driver with connector"}) == "connector"


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
    # #810: supply rows read both ways. FT231X and FUSB302 list 5 V against a
    # 5.5 V recommended top, 9 % under: inside the finding's tolerance.
    raw = {p["lcsc"]: p for p in json.loads(SAMPLE.read_text())["parts"]}
    both = [k for k, p in raw.items() if p.get("abs_max_v")]
    assert len(both) == 47 and all(rows[k]["agree"] for k in both)
    assert raw["C132159"]["listing_v"] == 5.0 and raw["C132159"]["datasheet_v"] == 5.5
    # a PDF for another revision of the part is left out
    assert "C492195" not in rows


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


def test_a_supply_read_both_ways_shows_a_low_listing_only_under_the_recommended_top():
    # #810: datasheet_v is the recommended top, abs_max_v the abs-max top
    row = {"rating": "supply", "datasheet_v": 5.5, "abs_max_v": 6.5}
    assert ls.judge_read({**row, "listing_v": 3.6}) == "low"
    assert ls.judge_read({**row, "listing_v": 5.5}) is None
    # a listing that gives the abs-max top (SPX3819: 16 V recommended, 20 V
    # abs max) is not wrong, nor is anything between the two
    assert ls.judge_read({**row, "listing_v": 6.5}) is None
    assert ls.judge_read({**row, "listing_v": 6.0}) is None
    assert ls.judge_read({**row, "listing_v": 7.5}) == "high"
    # with no abs-max top a row is judged two-way, as before
    assert ls.judge_read({"rating": "rated", "datasheet_v": 50, "listing_v": 63}) == "high"
    assert ls.judge_read({"rating": "rated", "datasheet_v": 50, "listing_v": 54}) is None


def test_a_supply_read_both_ways_replaces_the_extractions_one_way_comparison(
        tmp_path, capsys):
    corpus = tmp_path / "corpus"
    (corpus / "ext").mkdir(parents=True)
    (corpus / "listings").mkdir()
    for lcsc, v in (("C1", "1.65V~3.6V"), ("C2", "1.65V~5.5V")):
        (corpus / "ext" / f"{lcsc}.json").write_text(json.dumps(
            {"lcsc": lcsc, "pin_ratings": [{"pins": ["VCC"], "kind": "voltage",
                                            "max": 6.5, "source": "VCC abs max"}]}))
        (corpus / "listings" / f"{lcsc}.json").write_text(json.dumps(
            {"lcsc": lcsc, "mpn": lcsc, "description": "buffer IC",
             "attributes": [{"name": "Voltage - Supply", "value": v}]}))
    sample = tmp_path / "sample.json"
    # C1's listing tops out at 3.6 V while the part runs to 5.5 V: the
    # extraction alone (3.6 under 6.5 abs max) calls it agreed
    sample.write_text(json.dumps({"parts": [
        {"lcsc": "C1", "mpn": "C1", "class": "ic", "rating": "supply",
         "listing_v": 3.6, "datasheet_v": 5.5, "abs_max_v": 6.5,
         "row": "recommended VCC top", "datasheet": "C1"},
        {"lcsc": "C2", "mpn": "C2", "class": "ic", "rating": "supply",
         "listing_v": 5.5, "datasheet_v": 6.5, "row": "VCC abs max",
         "datasheet": "C2"}]}))
    assert ls.main(["--corpus", str(corpus), "--sample", str(sample), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    got = {r["lcsc"]: (r["how"], r["side"]) for r in res["parts"]}
    # a row without the recommended top does not displace the extraction
    assert got == {"C1": ("read", "low"), "C2": ("extraction", None)}
    assert ls.main(["--corpus", str(corpus), "--sample", str(sample)]) == 0
    assert "supply: 2 parts, 1 read both ways" in capsys.readouterr().out
