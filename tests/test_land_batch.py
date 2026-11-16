"""land_batch - a set's ICs' pad numbering read from their datasheets' pin
drawings, kept only where the PDF bears it out, for fp_verify's board mode
(#1288).

No network, no renderer and no model: the listing, the fetch, the PDF text,
the page images and the reader are stand-ins, so what is tested is the
deterministic part - which readings are kept, the record fp_verify reads
(the planted fault it then catches and its quiet twin), and that
errata_corpus hands the set's folder to fp_verify.
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

import datasheet_extract  # noqa: E402
import errata_corpus as ec  # noqa: E402
import fp_verify  # noqa: E402
import land_batch as lb  # noqa: E402

FP_BOARD = ROOT / "tests" / "fixtures" / "fp_board"
GOLDEN = ROOT / "tests" / "golden"

SO8_NAMES = ["OUT1", "IN1-", "IN1+", "GND", "IN2+", "IN2-", "OUT2", "VCC"]
SO8_TYPES = ["output", "input", "input", "ground", "input", "input",
             "output", "power_in"]
SO8_PAGES = [
    {"page": 0, "text": "SO8X dual comparator\nPin Configuration\n"
                        "SOIC-8 (TOP VIEW)\n" + "  ".join(
                            f"{i + 1} {n}" for i, n in enumerate(SO8_NAMES))},
    {"page": 1, "text": "Electrical Characteristics\nVCC 2.7 to 5.5 V"},
    {"page": 2, "text": "Package Outline Dimensions SOIC-8\n"
                        "e 1.27 BSC\nRecommended land pattern: rows 3.85 "
                        "apart, pad 1.55 x 0.6"},
]
SO8_PINOUT = [{"pin": str(i + 1), "name": n, "type": t, "page": 1}
              for i, (n, t) in enumerate(zip(SO8_NAMES, SO8_TYPES))]


def so8_reading(**kw):
    r = {"package_found": True, "sure": True, "view": "top", "view_page": 1,
         "view_quote": "SOIC-8 (TOP VIEW)",
         "sides": {"left": ["1", "2", "3", "4"], "bottom": [],
                   "right": ["5", "6", "7", "8"], "top": []},
         "pinout": SO8_PINOUT, "pitch_mm": 1.27,
         "span_mm": {"x": 3.85, "y": None}, "dims_page": 3,
         "dims_quote": "e 1.27 BSC; rows 3.85 apart"}
    r.update(kw)
    return r


def _sent(pages):
    views, dims, _ = lb.pick_pages(pages)
    return {i + 1 for i in set(views) | set(dims)}


# ------------------------------------------------------------- the checks

def test_pages_sent_are_the_pin_drawing_and_the_package_drawing():
    assert lb.pick_pages(SO8_PAGES) == ([0], [2], 0)


def test_a_top_view_read_up_the_walk_is_kept_with_the_pads_places():
    r = so8_reading()
    order, why = lb.check_order(r, SO8_PAGES, _sent(SO8_PAGES))
    assert why == "" and order == {"view": "top", "page": 1, "n": 8}
    pads, why = lb.place_pads(r, SO8_PAGES, _sent(SO8_PAGES))
    want = json.loads((FP_BOARD / "parts" / "C999008.json").read_text())
    assert why == "" and pads == want["land_pattern"]["pads"]


def test_a_bottom_view_is_mirrored_to_the_same_top_view_places():
    pages = [dict(p) for p in SO8_PAGES]
    pages[0]["text"] = pages[0]["text"].replace("TOP VIEW", "BOTTOM VIEW")
    r = so8_reading(view="bottom", sides={
        "left": ["8", "7", "6", "5"], "bottom": [],
        "right": ["4", "3", "2", "1"], "top": []})
    order, why = lb.check_order(r, pages, _sent(pages))
    assert why == "" and order["view"] == "bottom"
    pads, _ = lb.place_pads(r, pages, _sent(pages))
    assert pads == lb.place_pads(so8_reading(), SO8_PAGES,
                                 _sent(SO8_PAGES))[0]


@pytest.mark.parametrize("kw, reason", [
    ({"sides": {"left": ["8", "7", "6", "5"], "bottom": [],
                "right": ["4", "3", "2", "1"], "top": []}},
     "do not run in order round a top view"),
    ({"view": "bottom"}, "does not say 'bottom view'"),
    ({"view_page": 2}, "page 2 was not sent"),
    ({"sure": False}, "not sure"),
    ({"package_found": False}, "found no drawing"),
    ({"sides": {"left": ["1", "2", "3", "4"], "bottom": [],
                "right": ["5", "6", "7", "9"], "top": []}}, "not 1..8"),
    ({"sides": {"left": ["1", "2", "3", "4"], "bottom": [],
                "right": ["5", "6", "7", "7"], "top": []}}, "each once"),
    ({"sides": {"left": ["1", "2", "3", "4"], "bottom": [],
                "right": ["5", "6", "7", "EP"], "top": []}}, "not a number"),
    ({"pinout": SO8_PINOUT[:7]}, "not the pinout's numbered pins"),
])
def test_a_numbering_the_pdf_does_not_bear_out_is_left_out(kw, reason):
    order, why = lb.check_order(so8_reading(**kw), SO8_PAGES,
                                _sent(SO8_PAGES))
    assert order is None and reason in why


@pytest.mark.parametrize("kw, reason", [
    ({"pitch_mm": 1.25}, "pitch 1.25 is not printed on page 3"),
    ({"span_mm": {"x": 3.9, "y": None}}, "span x 3.9 is not printed"),
    ({"span_mm": {"x": None, "y": None}}, "no span x given"),
    ({"pitch_mm": None}, "no pitch given"),
    ({"dims_page": 2}, "dimension page 2 was not sent"),
])
def test_unprinted_dimensions_leave_only_the_direction(kw, reason):
    pads, why = lb.place_pads(so8_reading(**kw), SO8_PAGES, _sent(SO8_PAGES))
    assert pads is None and reason in why


def test_pins_that_overrun_the_rows_across_them_leave_the_places_out():
    pages = [dict(p) for p in SO8_PAGES]
    pages[2]["text"] += "\nD 0.9"
    r = so8_reading(sides={"left": ["1", "2"], "bottom": ["3", "4"],
                           "right": ["5", "6"], "top": ["7", "8"]},
                    span_mm={"x": 3.85, "y": 0.9})
    pads, why = lb.place_pads(r, pages, _sent(pages))
    assert pads is None and "do not fit" in why


# ------------------------------------------------------------- one part

@pytest.fixture
def stand_ins(tmp_path, monkeypatch):
    lst = tmp_path / "listings"
    lst.mkdir()
    for code, mpn, pkg in (("C999008", "SO8X", "SOIC-8"),
                           ("C9900003", "LQFP48-CCW", "LQFP-48")):
        (lst / f"{code}.json").write_text(json.dumps({
            "lcsc": code, "mpn": mpn, "brand": "Acme", "package": pkg,
            "datasheet": f"https://example.com/{mpn}.pdf"}))
    monkeypatch.setattr(lb.amb.parts_enrich, "cache_dir", lambda: lst)
    pages = {"SO8X": SO8_PAGES}

    def fetch(url, dest, mpn=None, transport=None, domains=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f"%PDF-1.4 {mpn}".encode())
        return {"url": url, "status": "fetched", "file": str(dest)}
    monkeypatch.setattr(datasheet_extract, "fetch_datasheet", fetch)
    monkeypatch.setattr(datasheet_extract, "extract_pdf_text",
                        lambda p: pages[p.read_bytes().decode().split()[-1]])
    monkeypatch.setattr(lb, "render", lambda pdf, pg, dest: [
        f"p-{i + 1:02d}.png" for i in pg])
    readings = {"SO8X": so8_reading()}
    calls = []

    def reader(prompt, cwd):
        calls.append(prompt)
        return readings[prompt.split("Part: ")[1].split()[0]]
    return tmp_path / "land", tmp_path / "abs-max", reader, calls, pages, readings


def _board_part(ref, n, lcsc):
    return {"ref": ref, "in_bom": True, "lcsc": lcsc, "mpn": "", "value": "",
            "pads": {str(i): f"N{i}" for i in range(1, n + 1)}, "fns": {},
            "nums": [str(i) for i in range(1, n + 1)]}


def _run_fp(tmp_path, pcb, parts):
    out = tmp_path / "fp.json"
    code = fp_verify.main(["--pcb", str(pcb), "--parts", str(parts),
                           "--out", str(out)])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_planted_so8_record_catches_the_mirrored_copies_and_fits_the_rest(
        stand_ins, tmp_path):
    """The planted fault: fp_board's U9 and U10 carry SO8X's land numbered
    from its bottom view; the reading's record catches both. Its quiet
    twin: U7 and U8, the same part numbered from the top view, fit, judged
    by the record land_batch wrote."""
    out, store, reader, calls, _, _ = stand_ins
    rep = lb.read_part("C999008", [_board_part("U7", 8, "C999008")], out,
                       store, reader)
    assert rep["status"] == "kept" and rep["kept"] == "pads"
    assert "p-01.png, p-03.png" in calls[0] and "=== page 3 ===" in calls[0]
    ext = json.loads((out / "ext" / "C999008.json").read_text())
    land = ext["land_pattern"]
    assert land["pad_count"] == 8 and land["pitch_mm"] == 1.27
    assert land["pads_source"] == ("Acme SO8X datasheet p.1 top view; "
                                   "dimensions p.3")
    assert "https://example.com/SO8X.pdf" in ext["source_pdf"]
    _, code = datasheet_extract.do_validate(
        type("A", (), {"validate": str(out / "ext" / "C999008.json")}))
    assert code == 0
    # a second run reads the cached reading, not the reader
    assert lb.read_part("C999008", [_board_part("U7", 8, "C999008")], out,
                        store, reader)["status"] == "kept"
    assert len(calls) == 1

    code, fp = _run_fp(tmp_path, FP_BOARD / "placed.kicad_pcb", out / "ext")
    bad = {v["refs"][0]: v for v in fp["violations"]}
    assert {"U9", "U10"} <= set(bad)
    assert {bad[r]["hint"] for r in ("U9", "U10")} == {"bottom_view_numbering"}
    by = {p["ref"]: p for p in fp["parts"]}
    for ref in ("U7", "U8"):
        assert by[ref]["pad_numbering"]["fit"] is True and ref not in bad
    assert by["U7"]["record"].endswith("C999008.json")
    assert str(out / "ext") in by["U7"]["record"]


LQFP_PINOUT = [{"pin": str(n), "name": f"P{n}", "type": "passive", "page": 1}
               for n in range(1, 49)]
LQFP_PAGES = [
    {"page": 0, "text": "LQFP48-CCW\nPin Configuration (TOP VIEW)\n"
                        + " ".join(f"{n} P{n}" for n in range(1, 49))},
    {"page": 1, "text": "Package Outline LQFP-48 7x7 mm\nsee drawing"},
]


def test_direction_only_record_catches_the_golden_mutant_and_passes_its_twin(
        stand_ins, tmp_path):
    """The dimension page prints no pitch, so only the direction (ccw_top) is
    written: the golden mutant lqfp-numbered-clockwise is caught at U1, and
    its quiet twin, blinky2's own U1 under the same record, passes."""
    out, store, reader, _, pages, readings = stand_ins
    pages["LQFP48-CCW"] = LQFP_PAGES
    readings["LQFP48-CCW"] = {
        "package_found": True, "sure": True, "view": "top", "view_page": 1,
        "sides": {"left": [str(n) for n in range(1, 13)],
                  "bottom": [str(n) for n in range(13, 25)],
                  "right": [str(n) for n in range(25, 37)],
                  "top": [str(n) for n in range(37, 49)]},
        "pinout": LQFP_PINOUT, "pitch_mm": 0.5,
        "span_mm": {"x": 8.5, "y": 8.5}, "dims_page": 2}
    rep = lb.read_part("C9900003", [_board_part("U1", 48, "C9900003")], out,
                       store, reader)
    assert rep["status"] == "kept" and rep["kept"] == "ccw_top"
    assert "not printed on page 2" in rep["places_left_out"]
    land = json.loads((out / "ext" / "C9900003.json").read_text())["land_pattern"]
    assert land["numbering"] == "ccw_top" and "pads" not in land

    mut = GOLDEN / "mutants" / "lqfp-numbered-clockwise"
    code, fp = _run_fp(tmp_path, mut / "blinky2.kicad_pcb", out / "ext")
    [v] = fp["violations"]
    assert (v["kind"], v["refs"], v["hint"], v["pos"]) == (
        "pad_numbering", ["U1"], "bottom_view_numbering", [128.0, 118.0])

    text = (GOLDEN / "blinky2" / "blinky2.kicad_pcb").read_text(encoding="utf-8")
    sys.path.insert(0, str(GOLDEN / "mutations"))
    import lqfp_numbered_clockwise as m
    s, e = m.mutlib.footprint_block(text, "U1")
    twin = text[:s] + text[s:e].replace(
        "(at 128 118)", '(at 128 118)\n\t\t(property "LCSC" "C9900003" '
        '(at 0 0 0) (layer "F.Fab") (hide yes))', 1) + text[e:]
    (tmp_path / "twin.kicad_pcb").write_text(twin, encoding="utf-8")
    code, fp = _run_fp(tmp_path, tmp_path / "twin.kicad_pcb", out / "ext")
    assert code == 0 and fp["violations"] == []
    assert fp["judged"] == {"numbering": 1}


def test_a_reading_that_fails_writes_no_record(stand_ins):
    out, store, reader, _, _, readings = stand_ins
    readings["SO8X"] = so8_reading(view="bottom")
    rep = lb.read_part("C999008", [_board_part("U7", 8, "C999008")], out,
                       store, reader)
    assert rep["status"] == "numbering not confirmed"
    assert "does not say 'bottom view'" in rep["why"]
    assert not (out / "ext" / "C999008.json").exists()


def test_a_pinout_that_misses_the_board_writes_no_record(stand_ins):
    out, store, reader, _, _, _ = stand_ins
    rep = lb.read_part("C999008", [_board_part("U7", 9, "C999008")], out,
                       store, reader)
    assert rep["status"] == "pinout disagrees with a board"
    assert not (out / "ext").exists()


def test_a_datasheet_with_no_view_is_not_read(stand_ins):
    out, store, reader, calls, pages, _ = stand_ins
    pages["SO8X"] = [{"page": 0, "text": "SO8X Pin Description 1 OUT1"}]
    rep = lb.read_part("C999008", [_board_part("U7", 8, "C999008")], out,
                       store, reader)
    assert rep["status"] == "no top or bottom view in the text" and calls == []


def test_the_maker_table_supplies_the_datasheet_the_listing_lacks(
        stand_ins, tmp_path, monkeypatch):
    """As abs_max_batch (#815): reference/datasheet_urls.yaml's entry for
    the part gives the PDF when its listing names none, so both read one."""
    out, store, reader, calls, _, _ = stand_ins
    lst = lb.amb.parts_enrich.cache_dir()
    (lst / "C999008.json").write_text(json.dumps({
        "lcsc": "C999008", "mpn": "SO8X", "brand": "Acme",
        "package": "SOIC-8", "datasheet": ""}))
    board = [_board_part("U7", 8, "C999008")]
    assert lb.read_part("C999008", board, out, store, reader)["status"] == \
        "no listing with a datasheet URL"
    t = tmp_path / "datasheet_urls.yaml"
    t.write_text("version: 1\nparts:\n  - mpns: [so8x]\n"
                 "    brand: Maker Inc\n    package: SOIC-8 (D)\n"
                 "    url: https://maker.example.com/so8x.pdf\n")
    monkeypatch.setattr(lb.amb, "DATASHEET_URLS", t)
    rep = lb.read_part("C999008", board, out, store, reader)
    assert rep["status"] == "kept"
    assert rep["datasheet_from"] == "reference/datasheet_urls.yaml"
    assert "Maker Inc" in calls[0]
    ext = json.loads((out / "ext" / "C999008.json").read_text())
    assert "https://maker.example.com/so8x.pdf" in ext["source_pdf"]


def test_the_resolver_finds_the_datasheet_and_a_miss_is_named(
        stand_ins, monkeypatch):
    """abs_max_batch's resolver (dsresolve, PR 1401) serves this batch too:
    a by-maker URL is read and cited, and cached so a second run asks
    nobody; its twin, a part the resolver misses, is left out with the
    resolver's reason and nothing read."""
    import dsresolve
    out, store, reader, calls, _, _ = stand_ins
    lst = lb.amb.parts_enrich.cache_dir()
    for code, mpn in (("C999008", "SO8X"), ("C999010", "SO8Y")):
        (lst / f"{code}.json").write_text(json.dumps({
            "lcsc": code, "mpn": mpn, "brand": "", "package": "SOIC-8",
            "datasheet": ""}))
    url = "https://www.ti.com/lit/ds/symlink/so8x.pdf"
    asked = []

    def resolve(mpn, brand, dest):
        asked.append(mpn)
        if mpn != "SO8X":
            return {"maker": "Texas Instruments", "miss": "not found",
                    "tried": [{"url": "https://www.ti.com/so8y.pdf",
                               "kind": "absent", "why": "HTTP 404"}]}
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 SO8X")
        return {"maker": "Texas Instruments", "url": url,
                "via": "Texas Instruments URL pattern for SO8X",
                "got": {"url": url, "status": "fetched", "file": str(dest)},
                "tried": []}
    monkeypatch.setattr(dsresolve, "resolve", resolve)
    monkeypatch.setattr(datasheet_extract, "fetch_datasheet",
                        lambda *a, **k: pytest.fail("fetched again"))
    board = [_board_part("U7", 8, "C999008")]
    rep = lb.read_part("C999008", board, out, store, reader)
    assert rep["status"] == "kept", rep
    assert rep["datasheet_from"] == "Texas Instruments URL pattern for SO8X"
    assert "Texas Instruments" in calls[0]
    ext = json.loads((out / "ext" / "C999008.json").read_text())
    assert url in ext["source_pdf"]
    assert lb.read_part("C999008", board, out, store, reader)["status"] == "kept"

    rep = lb.read_part("C999010", [_board_part("U8", 8, "C999010")], out,
                       store, reader)
    assert rep["status"] == "no listing with a datasheet URL"
    assert rep["resolver"] == "not found" and "so8y.pdf" in rep["why"]
    assert asked == ["SO8X", "SO8Y"] and len(calls) == 1


# ------------------------------------------------- parts keyed by Value

def _by_value(ref, value):
    return {**_board_part(ref, 8, ""), "value": value}


def _value_rows(store, value, rows):
    """The JLC search's rows for the Value, as value_listing caches them."""
    f = store / "listings" / "value-hits" / f"{value}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rows))


SO8X_ROW = {"lcsc": "C999008", "mpn": "SO8X", "brand": "Acme",
            "package": "SOIC-8", "datasheet": "https://example.com/SO8X.pdf"}


def test_a_part_keyed_by_its_value_is_read_and_found_by_it(stand_ins, tmp_path):
    """#1406 planted fault and quiet twin: a part with only its Value, named
    by one JLC listing, is read; fp_verify finds that record by the Value
    and catches U9 and U10 numbered from the bottom view, while U7, placed
    right, fits."""
    out, store, reader, calls, _, _ = stand_ins
    _value_rows(store, "SO8X", [SO8X_ROW, {**SO8X_ROW, "lcsc": "C999009"}])
    rep = lb.read_part("mpn:SO8X", [_by_value("U7", "SO8X")], out, store, reader)
    assert rep["status"] == "kept" and rep["keyed_by"] == "value"
    assert rep["lcsc"] == "C999008"
    assert (out / "ext" / "SO8X.json").is_file()

    text = (FP_BOARD / "placed.kicad_pcb").read_text(encoding="utf-8")
    text = text.replace('(property "LCSC" "C999008"', '(property "Note" "x"')
    head, *fps = text.split("\n\t(footprint ")
    fps = [fp.replace('(property "Value" ""', '(property "Value" "SO8X"')
           if '(property "Note" "x"' in fp else fp for fp in fps]
    pcb = tmp_path / "v.kicad_pcb"
    pcb.write_text("\n\t(footprint ".join([head, *fps]), encoding="utf-8")
    code, fp = _run_fp(tmp_path, pcb, out / "ext")
    by = {p["ref"]: p for p in fp["parts"]}
    bad = {v["refs"][0]: v["hint"] for v in fp["violations"]}
    assert code == 1
    assert bad["U9"] == bad["U10"] == "bottom_view_numbering"
    assert by["U7"]["pad_numbering"]["fit"] is True and "U7" not in bad
    assert by["U9"]["found_by"] == "value"
    assert by["U9"]["record"].endswith("SO8X.json")


def test_a_value_no_one_source_names_is_left_out(stand_ins, tmp_path,
                                                 monkeypatch):
    """#1406: no exact listing, listings in two packages, or two maker-table
    entries: left out with the reason, nothing read. One maker-table entry
    is enough on its own."""
    out, store, reader, calls, _, _ = stand_ins
    t = tmp_path / "datasheet_urls.yaml"
    t.write_text("version: 1\nparts: []\n")
    monkeypatch.setattr(lb.amb, "DATASHEET_URLS", t)
    board = [_by_value("U7", "SO8X")]

    _value_rows(store, "SO8X", [])
    rep = lb.read_part("mpn:SO8X", board, out, store, reader)
    assert rep["status"] == "not one listing for its Value"
    assert "no JLC listing" in rep["why"]

    _value_rows(store, "SO8X", [SO8X_ROW, {**SO8X_ROW, "lcsc": "C999009",
                                           "package": "SOP-8"}])
    rep = lb.read_part("mpn:SO8X", board, out, store, reader)
    assert rep["status"] == "not one listing for its Value"
    assert "under 2 package names" in rep["why"]

    entry = ("  - mpns: [SO8X]\n    brand: Maker Inc\n    package: SOIC-8\n"
             "    url: https://maker.example.com/{}.pdf\n")
    t.write_text("version: 1\nparts:\n" + entry.format("a") + entry.format("b"))
    rep = lb.read_part("mpn:SO8X", board, out, store, reader)
    assert "names it in 2 entries" in rep["why"]
    assert calls == [] and not (out / "ext" / "SO8X.json").exists()

    t.write_text("version: 1\nparts:\n" + entry.format("a"))
    rep = lb.read_part("mpn:SO8X", board, out, store, reader)
    assert rep["status"] == "kept"
    assert rep["datasheet_from"] == "reference/datasheet_urls.yaml"


SO8_FP = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
QFN16_ROW = {**SO8X_ROW, "lcsc": "C11", "package": "QFN-16(3x3)"}


def _on(fp, ref="U7"):
    return {**_by_value(ref, "SO8X"), "footprint": fp}


@pytest.fixture
def no_table(tmp_path, monkeypatch):
    t = tmp_path / "datasheet_urls.yaml"
    t.write_text("version: 1\nparts: []\n")
    monkeypatch.setattr(lb.amb, "DATASHEET_URLS", t)


def test_a_value_in_several_packages_takes_the_one_its_footprint_fits(
        stand_ins, no_table):
    """#1444 planted case: SO8X listed as a QFN-16 (the lower LCSC code) and
    an SOIC-8, on an SOIC-8 footprint: the SOIC-8 listing is read, and the
    report says the footprint chose it."""
    out, store, reader, calls, _, _ = stand_ins
    _value_rows(store, "SO8X", [QFN16_ROW, SO8X_ROW])
    rep = lb.read_part("mpn:SO8X", [_on(SO8_FP), _on(SO8_FP, "U8")], out,
                       store, reader)
    assert rep["status"] == "kept" and rep["lcsc"] == "C999008"
    assert rep["package"] == "SOIC-8"
    assert rep["package_from"].startswith("SOIC-8, of 2 packages")
    assert "SOIC-8" in calls[0] and "QFN-16" not in calls[0]


def test_one_package_under_two_spellings_is_one_package(stand_ins, no_table):
    """#1444: SOIC-8 and SOP-8 are one package to lib/pkgname, so a Value
    listed under both is read on a footprint that fits it."""
    out, store, reader, _, _, _ = stand_ins
    _value_rows(store, "SO8X", [SO8X_ROW, {**SO8X_ROW, "lcsc": "C999009",
                                           "package": "SOP-8"}])
    rep = lb.read_part("mpn:SO8X", [_on(SO8_FP)], out, store, reader)
    assert rep["status"] == "kept" and rep["lcsc"] == "C999008"
    assert rep["package_from"] == ("SOIC-8, its one package, listed under 2 "
                                   "names, the one its footprint fits")


@pytest.mark.parametrize("rows, foots, why", [
    # the footprint fits neither package
    ([QFN16_ROW, SO8X_ROW], ["Package_DFN_QFN:QFN-20-1EP_4x4mm_P0.5mm"],
     "fits none of them (QFN-16(3x3), SOIC-8)"),
    # two boards on footprints of the two packages: none fits both
    ([QFN16_ROW, SO8X_ROW], [SO8_FP, "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm"],
     "fits none of them"),
    # a 150 mil and a 208 mil SOIC-8 on an SOIC-8 footprint that says no width
    ([SO8X_ROW, {**SO8X_ROW, "lcsc": "C12", "package": "SOIC-8-208mil"}],
     ["SOIC-8"], "fits more than one (SOIC-8, SOIC-8-208mil)"),
    # a footprint name pkgname can't read
    ([QFN16_ROW, SO8X_ROW], ["MyLib:U_THING"], "footprint MyLib:U_THING is not read"),
    # a package name pkgname can't read
    ([SO8X_ROW, {**SO8X_ROW, "lcsc": "C12", "package": "WEIRD-PKG"}], [SO8_FP],
     "the package WEIRD-PKG is not read"),
])
def test_a_footprint_that_does_not_settle_the_package_leaves_it_out(
        stand_ins, no_table, rows, foots, why):
    """#1444 quiet twins: no single package fits every footprint, or a name
    is not read: left out, nothing read, the packages named."""
    out, store, reader, calls, _, _ = stand_ins
    _value_rows(store, "SO8X", rows)
    rep = lb.read_part("mpn:SO8X", [_on(f, f"U{i}") for i, f in enumerate(foots)],
                       out, store, reader)
    assert rep["status"] == "not one listing for its Value"
    assert "under 2 package names" in rep["why"] and why in rep["why"]
    assert calls == [] and not (out / "ext" / "SO8X.json").exists()


@pytest.mark.parametrize("rows, why", [
    ([{**SO8X_ROW, "mpn": "SO8X-TR", "lcsc": "C77"},
      {**SO8X_ROW, "mpn": "SO8X-TR-LONG", "lcsc": "C78"},
      {**SO8X_ROW, "mpn": "SO1Y", "lcsc": "C5"}],
     "no JLC listing has exactly that MPN; nearest: SO8X-TR (C77, SOIC-8)"),
    ([{**SO8X_ROW, "mpn": "ZZTOP", "lcsc": "C5"}],
     "no JLC listing has exactly that MPN, nor one near it"),
    ([], "no JLC listing has exactly that MPN, nor one near it"),
    ([{**SO8X_ROW, "datasheet": ""}],
     "1 JLC listings have exactly that MPN, none with a datasheet"),
])
def test_no_exact_listing_names_the_nearest_and_reads_nothing(
        stand_ins, no_table, rows, why):
    """#1444: no exact listing: the reason names the nearest MPN the search
    found (sharing at least half the Value's start), and nothing is read,
    so the part's numbering stays not_judged."""
    out, store, reader, calls, _, _ = stand_ins
    _value_rows(store, "SO8X", rows)
    rep = lb.read_part("mpn:SO8X", [_on(SO8_FP)], out, store, reader)
    assert rep["status"] == "not one listing for its Value"
    assert rep["why"] == why
    assert calls == [] and not (out / "ext" / "SO8X.json").exists()


def test_a_part_with_an_mpn_somewhere_keeps_the_mpn_listing(stand_ins):
    """#1406: when any board gives the part an MPN, its listing is the MPN
    search's, as before; the Value's rule is not applied."""
    out, store, reader, _, _, _ = stand_ins
    f = store / "listings" / "mpn" / "SO8X.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(SO8X_ROW))
    _value_rows(store, "SO8X", [])     # would leave it out if consulted
    rep = lb.read_part("mpn:SO8X", [_by_value("U7", "SO8X"),
                                    {**_by_value("U8", ""), "mpn": "SO8X"}],
                       out, store, reader)
    assert rep["status"] == "kept" and "keyed_by" not in rep


# ------------------------------------------------- what fp_verify is given

def test_errata_corpus_passes_the_sets_folder_to_fp_verify(tmp_path):
    b = {"id": "lqplant", "commit": "0" * 40,
         "rev_a": {"name": "a", "pcb": "blinky2.kicad_pcb"},
         "rev_b": {"name": "b", "pcb": "blinky2.kicad_pcb"}, "defects": []}
    cache = tmp_path / "cache"
    repo = ec.repo_dir(cache, b)
    repo.mkdir(parents=True)
    mut = GOLDEN / "mutants" / "lqfp-numbered-clockwise"
    shutil.copy(mut / "blinky2.kicad_pcb", repo / "blinky2.kicad_pcb")
    assert ec.land_dir(cache, b) == cache / "land" / "training" / "ext"
    assert ec.land_dir(cache, {"swept": {"round": 3}}).parent.name == "round-3"
    empty = tmp_path / "constraints.json"
    empty.write_text("{}")
    assert "--parts" not in ec._fp_verify(b, "rev_a", repo, empty)
    ext = ec.land_dir(cache, b)
    ext.mkdir(parents=True)
    shutil.copy(mut / "parts" / "C9900003.json", ext / "C9900003.json")
    cmd = ec._fp_verify(b, "rev_a", repo, empty)
    assert cmd[cmd.index("--parts") + 1] == str(ext)
    hits = [v for v in ec.audit(cache, b, "rev_a", "fp_verify")
            if v.get("severity") == "error"]
    assert [v.get("refs") for v in hits] == [["U1"]]
