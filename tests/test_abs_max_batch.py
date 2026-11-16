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

def _fp(ref, pads, lcsc="", mpn="", bom=True, value=""):
    return {"ref": ref, "in_bom": bom, "lcsc": lcsc, "mpn": mpn,
            "value": value, "pads": pads, "fns": {}, "nums": sorted(pads)}


def test_part_key_is_the_lcsc_code_else_the_mpn():
    assert amb.part_key(_fp("U1", {}, lcsc="c123")) == "C123"
    assert amb.part_key(_fp("U1", {}, lcsc="x", mpn="TPS7A02")) \
        == "mpn:TPS7A02"
    assert amb.ext_name("mpn:TPS 7A/02") == "TPS-7A-02"
    assert amb.part_key(_fp("U1", {})) is None


def test_part_key_falls_back_to_a_value_that_reads_as_a_part_number():
    """#815: an IC with its number only in Value is keyed by it; an LCSC
    code or MPN still wins, and a Value like 3.3V or MCU keys nothing."""
    p = _fp("U1", {}, value="AMS1117-3.3")
    assert amb.part_key(p) == "mpn:AMS1117-3.3" and amb.keyed_by_value(p)
    p = _fp("U1", {}, mpn="TPS7A02", value="AMS1117-3.3")
    assert amb.part_key(p) == "mpn:TPS7A02" and not amb.keyed_by_value(p)
    assert amb.part_key(_fp("U1", {}, lcsc="C6186", value="AMS1117")) == "C6186"
    for v in ("3.3V", "MCU", "LDO 3V3"):
        assert amb.part_key(_fp("U1", {}, value=v)) is None


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

    def offline(mpn, out):              # no search: read_part as before #1521
        raise RuntimeError("no JLC search in tests")
    monkeypatch.setattr(amb, "search_rows", offline)

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


def test_a_pin_connections_page_is_sent():
    """AMS1117's datasheet heads its pinout "PIN CONNECTIONS" (#815)."""
    pages = [{"page": 0, "text": "PIN CONNECTIONS 1- Ground/Adjust 2- VOUT"},
             {"page": 1, "text": "Absolute Maximum Ratings"}]
    assert amb.pick_pages(pages)[0] == [0, 1]


def _table(tmp_path, monkeypatch, url="https://www.ti.com/lit/ds/ldo.pdf"):
    t = tmp_path / "datasheet_urls.yaml"
    t.write_text("version: 1\nparts:\n"
                 "  - mpns: [TESTLDO, TESTLDO-R]\n"
                 "    brand: Maker Inc\n"
                 "    package: SOT-23-5 (DBV)\n"
                 f"    url: {url}\n")
    monkeypatch.setattr(amb, "DATASHEET_URLS", t)


def test_the_maker_table_supplies_a_datasheet_the_listing_lacks(
        stand_ins, tmp_path, monkeypatch):
    out, reader, calls = stand_ins
    (amb.parts_enrich.cache_dir() / "C999.json").write_text(json.dumps({
        "lcsc": "C999", "mpn": "testldo-r", "brand": "Acme",
        "package": "SOT-23-5", "datasheet": ""}))
    assert amb.read_part("C999", [BOARD], out, reader)["status"] == \
        "no listing with a datasheet URL"
    _table(tmp_path, monkeypatch)
    rep = amb.read_part("C999", [BOARD], out, reader)
    assert rep["status"] == "kept"
    assert rep["datasheet_from"] == "reference/datasheet_urls.yaml"
    assert "Maker Inc" in calls[0] and "SOT-23-5 (DBV)" in calls[0]
    ext = json.loads((out / "ext" / "C999.json").read_text())
    assert "https://www.ti.com/lit/ds/ldo.pdf" in ext["source_pdf"]


def test_an_mpn_with_no_listing_is_read_from_the_maker_table(
        stand_ins, tmp_path, monkeypatch):
    out, reader, _ = stand_ins

    def no_listing(key, out):
        raise RuntimeError("JLC parts search unreachable")
    monkeypatch.setattr(amb, "find_listing", no_listing)
    board = _fp("U1", BOARD["pads"], mpn="TESTLDO")
    assert amb.read_part("mpn:TESTLDO", [board], out, reader)["status"] \
        == "no listing"
    _table(tmp_path, monkeypatch)
    rep = amb.read_part("mpn:TESTLDO", [board], out, reader)
    assert rep["status"] == "kept" and rep["mpn"] == "TESTLDO"


def test_a_datasheet_cached_from_another_url_is_fetched_again(
        stand_ins, tmp_path, monkeypatch):
    out, reader, _ = stand_ins
    fetched = []

    def fetch(url, dest, mpn=None):
        fetched.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 stand-in")
        if "example.com" in url:        # a scan: kept on disk, not read
            return {"url": url, "status": "suspect", "file": str(dest),
                    "assessment": {"reasons": ["no text layer"]}}
        return {"url": url, "status": "fetched", "file": str(dest)}
    monkeypatch.setattr(datasheet_extract, "fetch_datasheet", fetch)
    assert amb.read_part("C999", [BOARD], out, reader)["status"] == \
        "datasheet suspect"
    assert amb.read_part("C999", [BOARD], out, reader)["status"] == \
        "datasheet suspect"
    assert len(fetched) == 1            # the PDF on disk is cached
    _table(tmp_path, monkeypatch)
    assert amb.read_part("C999", [BOARD], out, reader)["status"] == "kept"
    assert fetched[1] == "https://www.ti.com/lit/ds/ldo.pdf"


def test_the_resolver_finds_a_datasheet_the_listing_lacks(
        stand_ins, monkeypatch):
    """No listing URL, no table entry: dsresolve's by-maker URL is read,
    cited, and cached with its fetch record (a second run asks nobody)."""
    import dsresolve
    out, reader, calls = stand_ins
    (amb.parts_enrich.cache_dir() / "C999.json").write_text(json.dumps({
        "lcsc": "C999", "mpn": "TESTLDO", "brand": "",
        "package": "SOT-23-5", "datasheet": ""}))
    url = "https://www.ti.com/lit/ds/symlink/testldo.pdf"
    asked = []

    def resolve(mpn, brand, dest):
        asked.append((mpn, brand))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 stand-in")
        return {"maker": "Texas Instruments", "url": url,
                "via": "Texas Instruments URL pattern ... for TESTLDO",
                "got": {"url": url, "status": "fetched", "file": str(dest)},
                "tried": []}
    monkeypatch.setattr(dsresolve, "resolve", resolve)
    monkeypatch.setattr(datasheet_extract, "fetch_datasheet",
                        lambda *a, **k: pytest.fail("fetched again"))
    rep = amb.read_part("C999", [BOARD], out, reader)
    assert rep["status"] == "kept", rep
    assert rep["datasheet_from"] == "Texas Instruments URL pattern ... for TESTLDO"
    assert "Texas Instruments" in calls[0]
    assert url in json.loads((out / "ext" / "C999.json").read_text())["source_pdf"]
    assert amb.read_part("C999", [BOARD], out, reader)["status"] == "kept"
    assert asked == [("TESTLDO", "")]


def test_a_resolver_miss_is_named_and_a_stall_is_asked_again(
        stand_ins, monkeypatch):
    import dsresolve
    out, reader, calls = stand_ins
    (amb.parts_enrich.cache_dir() / "C999.json").write_text(json.dumps({
        "lcsc": "C999", "mpn": "TESTLDO", "brand": "Texas Instruments",
        "package": "SOT-23-5", "datasheet": ""}))
    asked = []

    def resolve(mpn, brand, dest):
        asked.append(mpn)
        kind = "stalled" if len(asked) == 1 else "absent"
        return {"maker": "Texas Instruments", "miss": "not found",
                "tried": [{"url": "https://www.ti.com/x.pdf", "kind": kind,
                           "why": "HTTP 404"}]}
    monkeypatch.setattr(dsresolve, "resolve", resolve)
    for _ in range(3):
        rep = amb.read_part("C999", [BOARD], out, reader)
        assert rep["status"] == "no listing with a datasheet URL"
        assert rep["resolver"] == "not found" and "ti.com/x.pdf" in rep["why"]
    assert len(asked) == 2 and calls == []     # the stall was not cached


VALUE_PADS = {"1": "+5V", "2": "GND", "3": "+5V", "5": "+3V3"}
NEAR_ROW = {"lcsc": "C1234", "mpn": "74AHCT1G125GW", "brand": "Nexperia",
            "package": "SOT-353", "category": "Buffers"}


def _no_datasheet(monkeypatch, rows, miss="not found", kind="absent"):
    """A Value-keyed part with no listing URL, the search rows given, and
    the resolver missing (kind 'stalled' for a host that stalled)."""
    import dsresolve
    monkeypatch.setattr(amb, "find_listing", lambda key, out: {})
    monkeypatch.setattr(amb, "search_rows", lambda mpn, out: rows)
    monkeypatch.setattr(dsresolve, "resolve", lambda mpn, brand, dest: {
        "maker": "Nexperia", "miss": miss,
        "tried": [{"url": "https://www.nexperia.com/x.pdf", "kind": kind,
                   "why": "HTTP 404"}]})


def test_a_value_with_no_exact_listing_is_named_as_land_names_it(
        stand_ins, monkeypatch):
    """#1521 planted fault: a Value no search row is exactly, the maker's
    site missing, is 'not one listing for its Value' with the nearest MPN,
    the label land gives it; nothing is read."""
    out, reader, calls = stand_ins
    _no_datasheet(monkeypatch, [NEAR_ROW])
    board = _fp("U1", VALUE_PADS, value="74AHCT1G125DB")
    rep = amb.read_part("mpn:74AHCT1G125DB", [board], out, reader)
    assert rep["status"] == "not one listing for its Value", rep
    assert rep["keyed_by"] == "value" and rep["resolver"] == "not found"
    assert rep["why"] == ("no JLC listing has exactly that MPN; nearest: "
                          "74AHCT1G125GW (C1234, SOT-353)")
    assert calls == []
    _no_datasheet(monkeypatch, [])
    rep = amb.read_part("mpn:74AHCT1G125DB", [board], out, reader)
    assert rep["why"] == "no JLC listing has exactly that MPN, nor one near it"


def test_a_grade_listing_is_never_read_for_ratings(stand_ins, monkeypatch):
    """#1536: land takes pads from the maker's listing with a grade code
    (W25Q128JVSIQ for W25Q128JVS); ratings change with grade, so abs-max
    keeps the Value a near miss and reads nothing."""
    out, reader, calls = stand_ins
    row = {"lcsc": "C97521", "mpn": "W25Q128JVSIQ", "brand": "Winbond Elec",
           "package": "SOIC-8-208mil", "datasheet": "https://example.com/W25.pdf"}
    _no_datasheet(monkeypatch, [row])
    board = _fp("U1", VALUE_PADS, value="W25Q128JVS")
    rep = amb.read_part("mpn:W25Q128JVS", [board], out, reader)
    assert rep["status"] == "not one listing for its Value", rep
    assert rep["why"].startswith("no JLC listing has exactly that MPN; nearest: "
                                 "W25Q128JVSIQ (C97521")
    assert calls == []


def test_the_old_labels_stay_where_the_value_rule_does_not_hold(
        stand_ins, monkeypatch):
    """#1521 quiet twins: an exact row with no URL, an MPN field (not a
    Value), a stalled maker site and a failed search keep their labels."""
    out, reader, calls = stand_ins
    exact = {**NEAR_ROW, "mpn": "74ahct1g125db", "lcsc": "C9"}
    by_value = _fp("U1", VALUE_PADS, value="74AHCT1G125DB")
    by_field = _fp("U1", VALUE_PADS, mpn="74AHCT1G125DB")
    key = "mpn:74AHCT1G125DB"
    _no_datasheet(monkeypatch, [NEAR_ROW, exact])
    assert amb.read_part(key, [by_value], out, reader)["status"] \
        == "no listing with a datasheet URL"
    _no_datasheet(monkeypatch, [NEAR_ROW])
    assert amb.read_part(key, [by_field], out, reader)["status"] \
        == "no listing with a datasheet URL"
    assert amb.read_part(key, [by_value, by_field], out, reader)["status"] \
        == "no listing with a datasheet URL"
    shutil.rmtree(out / "resolved")     # misses are cached; a stall is not
    _no_datasheet(monkeypatch, [NEAR_ROW], miss="stalled", kind="stalled")
    assert amb.read_part(key, [by_value], out, reader)["status"] \
        == "datasheet site stalled, not judged"

    _no_datasheet(monkeypatch, [NEAR_ROW])

    def down(mpn, out):
        raise RuntimeError("JLC parts search unreachable")
    monkeypatch.setattr(amb, "search_rows", down)
    assert amb.read_part(key, [by_value], out, reader)["status"] \
        == "no listing with a datasheet URL"
    assert calls == []


def test_a_value_the_resolver_finds_is_still_read(stand_ins, monkeypatch):
    """#1521 quiet twin: no exact row, but the maker's site has it: read."""
    import dsresolve
    out, reader, calls = stand_ins
    _no_datasheet(monkeypatch, [NEAR_ROW])
    url = "https://www.ti.com/lit/ds/symlink/testldo.pdf"

    def resolve(mpn, brand, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 stand-in")
        return {"maker": "Texas Instruments", "url": url, "via": "pattern",
                "got": {"url": url, "status": "fetched", "file": str(dest)},
                "tried": []}
    monkeypatch.setattr(dsresolve, "resolve", resolve)
    board = _fp("U1", VALUE_PADS, value="TESTLDO")
    rep = amb.read_part("mpn:TESTLDO", [board], out, reader)
    assert rep["status"] == "kept" and len(calls) == 1


ARDUINO = {"lcsc": "C20627271", "mpn": "ABX00027", "brand": "Arduino",
           "package": "-", "category": "Development Boards & Kits"}


def test_a_part_listed_only_as_a_dev_board_is_left_out(stand_ins, monkeypatch):
    """#1521 planted fault: ABX00027's one exact row is a Development
    Boards & Kits listing: 'left out: a board', no resolver, nothing read."""
    import dsresolve
    out, reader, calls = stand_ins
    _no_datasheet(monkeypatch, [ARDUINO])
    monkeypatch.setattr(dsresolve, "resolve",
                        lambda *a: pytest.fail("a board was resolved"))
    board = _fp("U1", VALUE_PADS, value="ABX00027")
    rep = amb.read_part("mpn:ABX00027", [board], out, reader)
    assert rep["status"] == "left out: a board"
    assert rep["why"] == ("its JLC listing (C20627271, Arduino) is under "
                          "Development Boards & Kits")
    assert calls == []


def test_a_chip_beside_a_board_or_near_boards_stays_an_ic():
    """#1521 quiet twins: a board row beside the chip's, or boards whose
    MPN only starts with it (RP2040-Zero), leave the part an IC."""
    chip = {"lcsc": "C2040", "mpn": "RP2040", "category": "Microcontrollers"}
    board = {"lcsc": "C5", "mpn": "RP2040", "category": amb.BOARD_CATEGORY}
    near = {"lcsc": "C6", "mpn": "RP2040-Zero", "category": amb.BOARD_CATEGORY}
    assert amb.a_board("RP2040", [chip, board]) is None
    assert amb.a_board("RP2040", [near]) is None
    assert amb.a_board("RP2040", []) is None
    assert amb.a_board("rp2040", [board, near])


def test_an_exposed_pad_is_the_boards_pad_after_the_last_pin(stand_ins):
    out, _, _ = stand_ins
    ep = {"pin": "EP", "name": "GND", "type": "ground", "page": 1}

    def reader(prompt):
        return {"package_found": True, "pinout": PINOUT + [ep],
                "ratings": [_row()]}
    board = _fp("U1", {**BOARD["pads"], "6": "GND"}, lcsc="C999")
    rep = amb.read_part("C999", [board], out, reader)
    assert rep["status"] == "kept"
    ext = json.loads((out / "ext" / "C999.json").read_text())
    assert [p["pin"] for p in ext["pinout"]] == ["1", "2", "3", "4", "5"]
    assert ext["exposed_pad"] == {"present": True, "connect_to": "GND"}
    assert cr.pinout_fact(board, ext)[0] == "verified"
    # a seventh pad is still no pin of the part
    bad = _fp("U1", {**board["pads"], "7": "X"}, lcsc="C999")
    assert cr.pinout_fact(bad, ext)[0] == "failed"


def test_a_row_naming_only_the_exposed_pad_goes_with_it():
    pinout = [{"pin": "1", "name": "VIN", "type": "power_in"},
              {"pin": "EP", "name": "PAD", "type": "passive"}]
    rows = [{"pins": ["VIN"], "max": 6}, {"pins": ["PAD"], "max": 0.3},
            {"pins": ["VIN"], "ref": "PAD", "max": 6}]
    rest, kept, ep = amb.split_exposed_pad(pinout, rows)
    assert [p["pin"] for p in rest] == ["1"] and kept == rows[:1]
    assert ep == {"present": True, "connect_to": "PAD"}
    assert amb.split_exposed_pad(rest, kept) == (rest, kept, None)


def test_every_maker_table_entry_is_complete_and_fetchable():
    import yaml
    researchlib = datasheet_extract._lib("researchlib")
    doc = yaml.safe_load(amb.DATASHEET_URLS.read_text())
    seen = set()
    for e in doc["parts"]:
        assert set(e) <= {"mpns", "brand", "package", "url", "note"}, e
        assert e["mpns"] and e["brand"] and e["package"], e
        ck = researchlib.check_url(e["url"])
        assert ck["ok"], (e["url"], ck["reason"])
        for m in e["mpns"]:
            assert amb.maker_datasheet(m) is e or \
                amb.maker_datasheet(m)["url"] == e["url"]
            assert m.upper() not in seen, f"{m} is in two entries"
            seen.add(m.upper())
    assert amb.maker_datasheet("") is None
    assert amb.maker_datasheet("NOT-A-TABLE-PART") is None


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


def test_batch_work_dir_still_finds_the_other_revision(tmp_path, monkeypatch):
    """The batch's run dir is `abs-max/<set>/work/<id>/`, not the sweep's
    `runs/<id>/`: the other revision's checkout is still found, so its BOM
    backfills part numbers here as in the sweep (#1354)."""
    cache = tmp_path / "cache"
    b = {"id": "brd", "commit": "a" * 40,
         "rev_a": {"pcb": "a/b.kicad_pcb"}, "rev_b": {"pcb": "b/b.kicad_pcb"}}
    repo = ec.repo_dir(cache, b, "rev_a")
    for rev in ("rev_a", "rev_b"):
        f = ec.repo_dir(cache, b, rev) / b[rev]["pcb"]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("(kicad_pcb)")
    empty = cache / "abs-max" / "training" / "work" / "brd" / "constraints.json"
    empty.parent.mkdir(parents=True)
    empty.write_text("{}")

    def pins_pcb(b_, rev, repo_, empty_):
        f = empty_.parent / f"{rev}-pins.kicad_pcb"
        f.write_text("(kicad_pcb)")
        return f
    monkeypatch.setattr(ec, "_pins_pcb", pins_pcb)
    got = ec._other_rev(b, "rev_a", repo, empty)
    args = got.get("args", [])
    assert "--other-pcb" in args
    assert args[args.index("--other-repo") + 1] == str(
        ec.repo_dir(cache, b, "rev_b"))
