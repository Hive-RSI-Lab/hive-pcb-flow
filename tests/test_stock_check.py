"""stock_check / stocklib: does a BOM row name a part JLC will place, today?

The sources are faked from the records JLC and LCSC returned on 2026-10-29
for the hi-fi core board (J1 oversold, U8 not for sale with a substitute at
0) - the two surprises that reached the person's BOM review.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import stock_check  # noqa: E402
import stocklib  # noqa: E402

JLC = {
    "C5184243": dict(componentModelEn="USB4105-GF-A-120",
                     componentLibraryType="expand", stockCount=2230,
                     canPresaleNumber=-372, isBuyComponent="1",
                     minPurchaseNum=13,
                     describe="1 16P 48V 5A Female Type-C USB 2.0 SMD"),
    "C84681": dict(componentModelEn="CH340C", componentLibraryType="expand",
                   stockCount=109322, canPresaleNumber=108194,
                   isBuyComponent="0",
                   noBuyReason="This product is no longer manufacture",
                   componentAlternativesCode="C7464026"),
    "C7464026": dict(componentModelEn="CH340C", componentLibraryType="expand",
                     stockCount=0, canPresaleNumber=-14400,
                     isBuyComponent="1"),
    "C1525": dict(componentModelEn="CL05B104KO5NNNC",
                  componentLibraryType="base", stockCount=32814868,
                  canPresaleNumber=24916183, isBuyComponent="1",
                  describe="100nF 16V X7R ±10% 0402 Multilayer Ceramic "
                           "Capacitors MLCC - SMD/SMT ROHS"),
    "C25744": dict(componentModelEn="0402WGF1002TCE",
                   componentLibraryType="base", stockCount=900000,
                   canPresaleNumber=800000, isBuyComponent="1",
                   describe="-55℃~+155℃ 10kΩ 50V 62.5mW Thick Film "
                            "Resistor ±1% 0402",
                   componentSpecificationEn="0402"),
    "C2765186": dict(componentModelEn="TYPE-C-31-M-12",
                     componentLibraryType="expand", stockCount=40000,
                     canPresaleNumber=39000, isBuyComponent="1",
                     describe="16P Female Type-C SMD USB Connectors"),
    "C474881": dict(componentModelEn="KF301-5.0-2P",
                    componentLibraryType="expand", stockCount=5000,
                    canPresaleNumber=5000, isBuyComponent="1"),
}
LCSC = {"C5184243": 0, "C84681": 109248, "C7464026": 0, "C1525": 10 ** 7,
        "C25744": 10 ** 6, "C474881": 4000, "C2765186": 39000}


def fake_fetch(down=()):
    calls = []

    def fetch(method, url, body=None):
        calls.append((method, url, body))
        if method == "POST":
            if "jlc" in down:
                raise stocklib.SourceError("HTTP 503 from jlcpcb.com")
            kw = body["keyword"]
            rows = [{"componentCode": c, **r} for c, r in JLC.items()
                    if c == kw or r["componentModelEn"] == kw]
            return {"code": 200, "data": {"componentPageInfo":
                                          {"list": rows}}}
        if "lcsc" in down:
            raise stocklib.SourceError("not JSON from wmsc.lcsc.com")
        code = url.rsplit("=", 1)[1]
        if code not in LCSC:
            return {"code": 404, "result": None}
        return {"code": 200, "result": {
            "productCode": code, "stockNumber": LCSC[code],
            "productModel": JLC[code]["componentModelEn"]}}
    fetch.calls = calls
    return fetch


def test_oversold_part_is_short_though_its_stock_reads_2230():
    v = stocklib.check_row("C5184243", 5, mpn="USB4105-GF-A-120",
                           value="USB4105-GF-A-120", fetch=fake_fetch())
    assert v["status"] == "short"
    assert v["available"] == -372
    assert any("2230" in n for n in v["notes"])


def test_not_for_sale_names_the_substitute_and_its_stock():
    v = stocklib.check_row("C84681", 5, mpn="CH340C", value="CH340C",
                           fetch=fake_fetch())
    assert v["status"] == "not_for_sale"
    assert v["substitute"]["code"] == "C7464026"
    assert "C7464026" in v["why"] and "short" in v["why"]


def test_basic_passive_matches_by_value_in_jlc_description():
    for code, value in (("C1525", "100nF"), ("C25744", "10k")):
        v = stocklib.check_row(code, 100, mpn=value, value=value,
                               fetch=fake_fetch())
        assert v["status"] == "ok", v


def test_0402_part_on_an_0603_footprint_is_a_mismatch():
    # oasis v0.2 Rfbb1 (docs/errata-corpus.md): the right value, the wrong size
    v = stocklib.check_row("C25744", 10, value="10k", fetch=fake_fetch(),
                           footprint="R_0603_1608Metric")
    assert v["status"] == "mismatch"
    assert "0402" in v["why"] and "0603" in v["why"]
    for fp in ("Resistor_SMD:R_0402_1005Metric", "", "SOT-23"):
        v = stocklib.check_row("C25744", 10, value="10k", fetch=fake_fetch(),
                               footprint=fp)
        assert v["status"] == "ok", (fp, v)


def test_wrong_value_is_a_mismatch():
    v = stocklib.check_row("C1525", 10, mpn="10nF", value="10nF",
                           fetch=fake_fetch())
    assert v["status"] == "mismatch"


def test_wrong_mpn_is_a_mismatch():
    v = stocklib.check_row("C1525", 10, mpn="GRM155R71C104KA88D",
                           value="100nF", fetch=fake_fetch())
    assert v["status"] == "mismatch"


def test_a_label_for_a_value_cannot_be_matched_and_is_unknown():
    v = stocklib.check_row("C474881", 2, mpn="SPK A L", value="SPK A L",
                           fetch=fake_fetch())
    assert v["status"] == "unknown"


def test_value_that_is_the_mpn_matches_without_a_digit_word():
    # TYPE-C-31-M-12: no 3-char word with a digit, but the whole value is
    # JLC's model; issue #180.
    for mpn in ("TYPE-C-31-M-12", ""):
        v = stocklib.check_row("C2765186", 5, mpn=mpn,
                               value="TYPE-C-31-M-12", fetch=fake_fetch())
        assert v["status"] == "ok", v


def test_code_jlc_does_not_know_is_not_found():
    v = stocklib.check_row("C999999999", 1, fetch=fake_fetch())
    assert v["status"] == "not_found"


def test_source_down_is_unknown_never_a_pass():
    v = stocklib.check_row("C1525", 1, value="100nF",
                           fetch=fake_fetch(down=("jlc",)))
    assert v["status"] == "unknown"
    assert "503" in v["why"]


def test_lcsc_down_only_notes_it():
    v = stocklib.check_row("C1525", 1, value="100nF",
                           fetch=fake_fetch(down=("lcsc",)))
    assert v["status"] == "ok"
    assert any("LCSC" in n for n in v["notes"])


def test_lcsc_zero_with_jlc_stock_is_noted_not_failed():
    JLC["C9"] = dict(componentModelEn="XB1234", componentLibraryType="expand",
                     stockCount=50, canPresaleNumber=50, isBuyComponent="1")
    LCSC["C9"] = 0
    try:
        v = stocklib.check_row("C9", 5, mpn="XB1234", value="XB1234",
                               fetch=fake_fetch())
    finally:
        JLC.pop("C9"), LCSC.pop("C9")
    assert v["status"] == "ok"
    assert any("LCSC shows 0" in n for n in v["notes"])


def test_value_quantity():
    q = stocklib.value_quantity
    assert q("100nF") == pytest.approx((1e-7, "F"))
    assert q("4K7") == (4700.0, "")
    assert q("470R") == (470.0, "Ω")
    assert q("470uF/50V") == pytest.approx((470e-6, "F"))
    assert q("CH340C") is None
    # m is milli, M is mega - in both spellings (issue #762)
    assert q("1M") == (1e6, "")
    assert q("1mΩ") == (1e-3, "Ω")
    assert q("4M7") == pytest.approx((4.7e6, ""))
    assert q("4m7") == pytest.approx((4.7e-3, ""))
    assert q("0.5R") == q("500mΩ") == q("500mOhm") == (0.5, "Ω")
    # a space between the number and its prefix (BOMs from KiCad fields)
    assert q("100 nF") == q("100nF")
    assert q("2.2 uF") == pytest.approx((2.2e-6, "F"))
    assert q("100 / 50V") == (100.0, "")


def _desc_match(bom_value, describe):
    return stocklib.matches("", bom_value, {"model": "X",
                                            "description": describe})[0]


@pytest.mark.parametrize("bom,describe", [
    # issue #762: SI-prefix equivalents of the same value
    ("0.5R", "500mΩ ±1% 2W 2512 Current Sense Resistor"),
    ("100nF", "0.1uF 50V X7R ±10% 0402 MLCC"),
    ("1nF", "1000pF 50V C0G ±5% 0402 MLCC"),
    ("4K7", "4.7kΩ 62.5mW Thick Film Resistor ±1% 0402"),
    ("4K7", "RES 4.7K 1% 0402"),
    ("2.2uH", "2.2µH ±20% 3A SMD Power Inductor"),
    ("100 nF", "100nF 50V X7R ±10% 0603 MLCC"),
    ("1.00k", "100mW 1kΩ 75V Thin Film Resistor ±0.1% 0603"),
])
def test_value_matches_its_si_prefix_equivalent(bom, describe):
    assert _desc_match(bom, describe) is True


@pytest.mark.parametrize("bom,describe", [
    ("1M", "1mΩ ±1% 3W 2512 Current Sense Resistor"),   # mega is not milli
    ("1mΩ", "1MΩ 62.5mW Thick Film Resistor ±1% 0402"),
    ("4m7", "4.7MΩ 62.5mW Thick Film Resistor ±1% 0402"),
    ("100nF", "100nH ±5% 0402 Inductor"),               # same number, not F
    ("0.5R", "5Ω ±1% 0402"),
])
def test_value_does_not_match_another_magnitude_or_unit(bom, describe):
    assert _desc_match(bom, describe) is False


def test_a_value_that_is_the_model_matches_though_it_reads_as_a_number():
    rec = {"model": "750315240",
           "description": "1:1.1 1A 440uH 5V 5kV SMD-6P Power Transformers"}
    assert stocklib.matches("", "750315240", rec)[0] is True
    assert stocklib.matches("", "750315241", rec)[0] is False


def test_check_rows_quantity_is_refs_times_boards():
    rows = [{"Comment": "100nF", "Designator": "C1,C2,C3", "LCSC": "C1525"},
            {"Comment": "USB4105-GF-A-120", "Designator": "J1",
             "LCSC": "C5184243"}]
    res = stock_check.check_rows(rows, boards=5, fetch=fake_fetch(), jobs=1)
    assert res["rows"][0]["need"] == 15
    assert res["status"] == "fail"
    assert [b["refs"] for b in res["blocking"]] == [["J1"]]
    kinds = [v["kind"] for v in stock_check.violations(res)]
    assert kinds == ["bom_stock_short"]


def _write_bom(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        w.writerows(rows)


def test_cli_refuses_offline(tmp_path, capsys):
    bom = tmp_path / "BOM.csv"
    _write_bom(bom, [["100nF", "C1", "C0402", "C1525"]])
    assert stock_check.main(["--bom", str(bom)]) == 2
    assert "HWDE_STOCK_OFFLINE" in capsys.readouterr().out


def test_cli_exit_and_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HWDE_STOCK_OFFLINE", "0")
    monkeypatch.setattr(stocklib, "http_fetch", fake_fetch())
    ws = tmp_path / "ws"
    (ws / "fab").mkdir(parents=True)
    (ws / "parts").mkdir()
    (ws / "parts" / "parts.json").write_text(json.dumps({"parts": [
        {"refdes": ["U8"], "lcsc": "C84681", "mpn": "CH340C"}]}))
    _write_bom(ws / "fab" / "BOM.csv", [["100nF", "C1", "C0402", "C1525"],
                                        ["CH340C", "U8", "SOP-16", "C84681"]])
    assert stock_check.main(["--workspace", str(ws), "--boards", "2"]) == 1
    rep = json.loads((ws / "reports" / "stock_check.json").read_text())
    assert rep["counts"] == {"ok": 1, "not_for_sale": 1}
    assert "U8" in capsys.readouterr().err


POS = ("Ref,Val,Package,PosX,PosY,Rot,Side\n"
       "C1,100nF,C0402,1.0,-1.0,0,top\n"
       "U8,CH340C,SOP-16,3.0,-1.0,0,top\n")


def _bom_cpl(tmp_path, **kw):
    import bom_cpl
    pcb = tmp_path / "s.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    pos = tmp_path / "s-pos.csv"
    pos.write_text(POS, encoding="utf-8")
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps({"parts": [
        {"refdes": ["C1"], "lcsc": "C1525", "value": "100nF"},
        {"refdes": ["U8"], "lcsc": "C84681", "value": "CH340C",
         "mpn": "CH340C"}]}), encoding="utf-8")
    return bom_cpl.run(pcb, tmp_path / "fab", pos=pos, parts_json=pj, **kw)


def test_bom_cpl_fails_a_row_jlc_will_not_place(tmp_path):
    rep = _bom_cpl(tmp_path, stock=True, stock_fetch=fake_fetch())
    assert rep["status"] == "violations"
    v = [x for x in rep["violations"] if x["kind"].startswith("bom_stock")]
    assert [(x["kind"], x["refs"]) for x in v] == \
        [("bom_stock_not_for_sale", ["U8"])]
    assert rep["stock"]["rows"][0]["need"] == 5   # build_qty boards


def test_bom_cpl_offline_says_stock_was_not_checked(tmp_path):
    rep = _bom_cpl(tmp_path)          # HWDE_STOCK_OFFLINE=1 (conftest)
    assert rep["stock"]["status"] == "skipped"
    assert "unknown" in rep["stock"]["why"]
    assert not [x for x in rep["violations"]
                if x["kind"].startswith("bom_stock")]
