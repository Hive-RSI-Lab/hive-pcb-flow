"""abs_max_batch - a set's ICs' absolute-maximum ratings read from their
datasheets, kept only where the PDF text shows them (#1339).

No network and no model: the listing, the fetch, the PDF text and the reader
are stand-ins, so what is tested is the deterministic part - which rows are
kept, which parts are left out, the file check_ratings reads, and that
errata_corpus hands that folder to check_ratings.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import abs_max_batch as amb  # noqa: E402
import check_ratings as cr  # noqa: E402
import datasheet_extract  # noqa: E402
import errata_corpus as ec  # noqa: E402

PAGES = [
    {"page": 0, "text": "TESTLDO 300 mA LDO\nPin Configuration\n"
                        "1 VIN  2 GND  3 EN  4 NC  5 VOUT"},
    {"page": 1, "text": "Absolute Maximum Ratings\n"
                        "Input voltage VIN, EN to GND −0.3 to 6.5 V\n"
                        "VOUT to GND -0.3 to VIN + 0.3 V\n"
                        "All other pins -0.3 to 6 V"},
    {"page": 2, "text": "Recommended Operating Conditions\nVIN 2.5 to 5.5 V"},
    {"page": 3, "text": "Electrical Characteristics"},
]

PINOUT = [{"pin": "1", "name": "VIN", "type": "power_in", "page": 1},
          {"pin": "2", "name": "GND", "type": "ground", "page": 1},
          {"pin": "3", "name": "EN", "type": "input", "page": 1},
          {"pin": "4", "name": "NC", "type": "nc", "page": 1},
          {"pin": "5", "name": "VOUT", "type": "power_out", "page": 1}]


def _row(**kw):
    r = {"pins": ["VIN", "EN"], "min": -0.3, "max": 6.5, "ref": None,
         "page": 2, "quote": "Input voltage VIN, EN to GND -0.3 to 6.5 V",
         "sure": True}
    r.update(kw)
    return r


def _verify(rows, pinout=PINOUT):
    send, abs_pages = amb.pick_pages(PAGES)
    return amb.verify({"package_found": True, "pinout": pinout,
                       "ratings": rows}, PAGES, abs_pages,
                      {i + 1 for i in send})


def test_pages_sent_are_the_abs_max_page_the_next_and_the_pinout():
    send, abs_pages = amb.pick_pages(PAGES)
    assert send == [0, 1, 2] and abs_pages == {2, 3}


def test_a_sure_row_quoted_from_the_abs_max_page_is_kept():
    pins, rows, why = _verify([_row()])
    assert len(pins) == 5 and why == []
    assert rows[0]["pins"] == ["VIN", "EN"] and rows[0]["max"] == 6.5
    assert rows[0]["page"] == 2


def test_a_relative_bound_is_kept_when_its_pin_and_number_are_quoted():
    _, rows, why = _verify([_row(pins=["VOUT"], max="VIN+0.3",
                                 quote="VOUT to GND -0.3 to VIN + 0.3 V")])
    assert why == [] and rows[0]["max"] == "VIN+0.3"


@pytest.mark.parametrize("kw, reason", [
    ({"sure": False}, "not sure"),
    ({"page": 3}, "quote is not on page 3"),
    ({"page": 1}, "not an absolute-maximum page"),
    ({"max": 7.0}, "7.0 is not in the quote"),
    ({"pins": ["VIN", "VOUT"]}, "'VOUT' is not named in the quote"),
    ({"pins": ["VIN", "GND"]}, "rates a ground pin"),
    ({"pins": ["VIN", "SW"]}, "'SW' is not in the pinout"),
    ({"quote": "Input voltage VIN, EN to GND -0.3 to 6.6 V"}, "not on page"),
    ({"max": "VDD+0.3"}, "names no pin"),
    ({"min": None, "max": None}, "no bound"),
])
def test_a_row_the_page_does_not_bear_out_is_left_out(kw, reason):
    _, rows, why = _verify([_row(**kw)])
    assert rows == [] and reason in why[0]


def test_a_supply_row_naming_no_pin_is_the_one_power_input():
    pages = [dict(p) for p in PAGES]
    pages[1]["text"] += "\nDC Supply Voltage ....... -0.3V to +6.5V"
    q = "DC Supply Voltage ....... -0.3V to +6.5V"
    send, ab = amb.pick_pages(pages)
    reading = {"package_found": True, "pinout": PINOUT,
               "ratings": [_row(pins=["VIN"], quote=q),
                           _row(pins=["EN"], quote=q)]}
    _, rows, why = amb.verify(reading, pages, ab, {i + 1 for i in send})
    assert [r["pins"] for r in rows] == [["VIN"]]
    assert "'EN' is not named in the quote" in why[0]


def test_a_pin_name_not_on_its_page_leaves_the_part_out():
    bad = [dict(p) for p in PINOUT]
    bad[2]["name"] = "SHDN"
    pins, rows, why = _verify([_row()], bad)
    assert pins == [] and rows == [] and "SHDN" in why[0]


def test_no_package_found_leaves_the_part_out():
    pins, _, why = amb.verify({"package_found": False, "pinout": PINOUT,
                               "ratings": [_row()]}, PAGES, {2}, {1, 2})
    assert pins == [] and "no pinout" in why[0]


# ------------------------------------------------------------ which parts

def _fp(ref, pads, lcsc="", mpn="", bom=True):
    return {"ref": ref, "in_bom": bom, "lcsc": lcsc, "mpn": mpn,
            "pads": pads, "fns": {}, "nums": sorted(pads)}


def test_part_key_is_the_lcsc_code_else_the_mpn():
    assert amb.part_key(_fp("U1", {}, lcsc="c123")) == "C123"
    assert amb.part_key(_fp("U1", {}, lcsc="x", mpn="TPS7A02")) \
        == "mpn:TPS7A02"
    assert amb.ext_name("mpn:TPS 7A/02") == "TPS-7A-02"
    assert amb.part_key(_fp("U1", {})) is None


def test_nets_agree_when_only_names_differ():
    b = _fp("U1", {"1": "+5V", "2": "GND", "3": "EN", "5": "+3V3"})
    pins = [{k: v for k, v in p.items() if k != "page"} for p in PINOUT]
    assert amb.nets_agree(b, pins, "w")[0] == "verified"
    b["pads"]["2"] = "+5V"
    st, why = amb.nets_agree(b, pins, "w")
    assert st == "unknown" and "wrong net" in why


# ------------------------------------------------------------- one part

@pytest.fixture
def stand_ins(tmp_path, monkeypatch):
    lst = tmp_path / "listings"
    lst.mkdir()
    (lst / "C999.json").write_text(json.dumps({
        "lcsc": "C999", "mpn": "TESTLDO", "brand": "Acme",
        "package": "SOT-23-5", "datasheet": "https://example.com/ldo.pdf"}))
    monkeypatch.setattr(amb.parts_enrich, "cache_dir", lambda: lst)

    def fetch(url, dest, mpn=None, transport=None, domains=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 stand-in")
        return {"url": url, "status": "fetched", "file": str(dest)}
    monkeypatch.setattr(datasheet_extract, "fetch_datasheet", fetch)
    monkeypatch.setattr(datasheet_extract, "extract_pdf_text",
                        lambda p: PAGES)
    calls = []

    def reader(prompt):
        calls.append(prompt)
        return {"package_found": True, "pinout": PINOUT,
                "ratings": [_row(), _row(sure=False, pins=["NC"])]}
    return tmp_path / "out", reader, calls


BOARD = _fp("U1", {"1": "+5V", "2": "GND", "3": "+5V", "5": "+3V3"},
            lcsc="C999")


def test_read_part_writes_a_cited_extraction_check_ratings_reads(stand_ins):
    out, reader, calls = stand_ins
    rep = amb.read_part("C999", [BOARD], out, reader)
    assert rep["status"] == "kept" and rep["rows_kept"] == 1
    assert "=== page 2 ===" in calls[0] and "SOT-23-5" in calls[0]
    ext = json.loads((out / "ext" / "C999.json").read_text())
    assert ext["mpn"] == "TESTLDO" and ext["lcsc"] == "C999"
    [r] = ext["pin_ratings"]
    assert r["source"] == ("Acme TESTLDO datasheet p.2 Absolute Maximum "
                           "Ratings: 'Input voltage VIN, EN to GND -0.3 to "
                           "6.5 V'")
    assert "https://example.com/ldo.pdf" in ext["source_pdf"]
    assert "sha256 " in ext["source_pdf"]
    _, code = datasheet_extract.do_validate(
        type("A", (), {"validate": str(out / "ext" / "C999.json")}))
    assert code == 0
    assert cr.load_extraction(out / "nowhere", "C999", out / "ext") == ext
    # a second run reads the cached reading, not the reader
    assert amb.read_part("C999", [BOARD], out, reader)["status"] == "kept"
    assert len(calls) == 1


def test_read_part_leaves_out_a_part_whose_pins_miss_the_board(stand_ins):
    out, reader, _ = stand_ins
    board = _fp("U1", {"1": "+5V", "2": "GND", "6": "X"}, lcsc="C999")
    rep = amb.read_part("C999", [board], out, reader)
    assert rep["status"] == "pinout disagrees with a board"
    assert not (out / "ext" / "C999.json").exists()


def test_a_suspect_datasheet_is_not_read(stand_ins, monkeypatch):
    out, reader, calls = stand_ins
    monkeypatch.setattr(datasheet_extract, "fetch_datasheet",
                        lambda url, dest, mpn=None: {
                            "url": url, "status": "suspect",
                            "assessment": {"reasons": ["other part"]}})
    rep = amb.read_part("C999", [BOARD], out, reader)
    assert rep["status"] == "datasheet suspect" and calls == []


# ------------------------------------------------- what check_ratings reads

def test_check_ratings_reads_the_extractions_folder(tmp_path):
    from test_check_ratings import _errors, _ws
    ext = {"mpn": "TESTLDO", "pinout": [
        {k: v for k, v in p.items() if k != "page"} for p in PINOUT],
        "pin_ratings": [{"pins": ["VIN"], "kind": "voltage",
                         "level": "abs_max", "max": 6.5,
                         "source": "test p.2: 'VIN 6.5 V'"}]}
    folder = tmp_path / "batch"
    folder.mkdir()
    (folder / "C999.json").write_text(json.dumps(ext))
    ws = _ws(tmp_path, [("U1", "C999", {"1": "+12V", "2": "GND",
                                        "5": "+3V3"})], {})
    argv = ["--pcb", str(ws / "kicad" / "board.kicad_pcb")]
    assert _errors(cr.run(argv)[0]) == []
    errs = _errors(cr.run(argv + ["--extractions", str(folder)])[0])
    assert [e["pin"] for e in errs] == ["U1.1"]


def test_errata_corpus_passes_the_sets_folder_to_check_ratings(tmp_path):
    fix = ROOT / "tests" / "fixtures" / "ratings" / "overvolt"
    b = {"id": "ratplant", "commit": "0" * 40,
         "rev_a": {"name": "a", "pcb": "kicad/board.kicad_pcb"},
         "rev_b": {"name": "b", "pcb": "kicad/board.kicad_pcb"},
         "defects": []}
    cache = tmp_path / "cache"
    repo = ec.repo_dir(cache, b)
    shutil.copytree(fix, repo)
    (repo / "state.json").write_text("{}")
    assert ec.abs_max_dir(cache, b) == cache / "abs-max" / "training" / "ext"
    assert ec.abs_max_dir(cache, {"swept": {"round": 3}}).parent.name \
        == "round-3"
    pins = lambda: [v.get("pin") for v in  # noqa: E731
                    ec.audit(cache, b, "rev_a", "check_ratings")
                    if v.get("severity") == "error"]
    assert "U1.10" not in pins()
    ext = ec.abs_max_dir(cache, b)
    ext.mkdir(parents=True)
    shutil.copy(fix / "parts" / "C8734.json", ext / "C8734.json")
    assert "U1.10" in pins()
