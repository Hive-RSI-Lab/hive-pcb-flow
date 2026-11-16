"""ballmaps - a ball-grid part's ball map from its datasheet's ball list or
the maker's pinout file, checked before it goes into a record (#1455).

No network: the files are small synthetic copies of the two makers'
formats, and the fetch runs on a stand-in transport.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import ballmaps as bm  # noqa: E402

BALLS = [f"{r}{c}" for r in "ABCD" for c in range(1, 5)]          # 16, 4 x 4
NAMES = {b: ("GND" if b in ("A1", "D4") else "VCCIO0" if b == "B2"
             else f"PT{i}A") for i, b in enumerate(BALLS)}


def lattice_csv(rows=None, column="CABGA16") -> bytes:
    rows = rows if rows is not None else [(NAMES[b], b) for b in BALLS]
    out = ["# Lattice pinout, synthetic", "",
           f"PAD,Pin/Ball Function,Bank,Dual Function,Differential,{column},QFN32"]
    out += [f"{i},{n},0,,,{b},-" for i, (n, b) in enumerate(rows)]
    out.append(f"{len(rows)},NC_ONLY_QFN,0,,,-,7")      # not in this package
    return ("\n".join(out) + "\n").encode()


def xilinx_txt(rows=None, total=16) -> bytes:
    rows = rows if rows is not None else [(b, NAMES[b]) for b in BALLS]
    out = ["Device/Package xc6slx9ftg16 1/1/2020", "",
           "Pin\tBank\tBUFIO2\tPin Description   "]
    out += [f"{b}  \t0\t\t{n}   " for b, n in rows]
    if total is not None:
        out += ["", f"Total Number of Pins generated, {total}"]
    return ("\n".join(out) + "\n").encode()


LAT = {"format": "lattice_csv", "column": "CABGA16", "balls": 16,
       "package": "caBGA-16"}
XIL = {"format": "xilinx_pkg_txt", "balls": 16, "package": "FTG16"}


# --- both makers' formats ------------------------------------------------------

def test_lattice_csv_reads_the_package_column_only():
    pin, why = bm.from_file(lattice_csv(), LAT, ["LFE5U-12F-6BG16C"])
    assert why == "" and len(pin) == 16
    by = {p["pin"]: p for p in pin}
    assert by["A1"] == {"pin": "A1", "name": "GND", "type": "ground"}
    assert by["B2"]["type"] == "power_in" and by["A2"]["type"] == "unspecified"
    assert "NC_ONLY_QFN" not in {p["name"] for p in pin}


def test_xilinx_txt_reads_trailing_spaces_and_its_own_total():
    pin, why = bm.from_file(xilinx_txt(), XIL, ["XC6SLX9-2FTG16C"])
    assert why == "" and len(pin) == 16
    assert {p["pin"]: p["name"] for p in pin}["A2"] == "PT1A"


def test_a_file_that_is_not_the_format_is_refused():
    assert bm.from_file(b"pin,name\nA1,GND\n", LAT)[0] is None
    assert "Total Number of Pins" in bm.from_file(xilinx_txt(total=None), XIL)[1]
    assert "no CABGA16 column" in bm.from_file(lattice_csv(column="CABGA25"), LAT)[1]
    assert "not one of" in bm.from_file(b"x", {"format": "pdf", "balls": 16})[1]


# --- planted faults and the quiet twin ---------------------------------------

@pytest.mark.parametrize("rows, why", [
    ([(NAMES[b], b) for b in BALLS[:-1]], "15 balls listed, the package has 16"),
    ([(NAMES[b], b) for b in BALLS] + [("PT99A", "C3")],
     "ball C3 is listed twice (PT10A, PT99A)"),
    ([(NAMES[b], b) for b in BALLS[:-1]] + [("PT15A", "4D")],
     "not a letter-row / number-column grid (4D)"),
])
def test_planted_bad_maps_are_refused(rows, why):
    pin, got = bm.from_file(lattice_csv(rows), LAT)
    assert pin is None and why in got


def test_counts_must_agree_everywhere():
    assert "totals 17" in bm.from_file(xilinx_txt(total=17), XIL)[1]
    assert "states 25 balls" in bm.from_file(
        lattice_csv(), LAT, ["LFE5U-12F-6BG25C"])[1]
    assert "no ball count" in bm.check([(b, NAMES[b]) for b in BALLS], None)[1]


def test_quiet_twin_a_whole_map_is_kept_with_its_order():
    pin, why = bm.check([(b, NAMES[b]) for b in reversed(BALLS)], 16)
    assert why == "" and [p["pin"] for p in pin] == BALLS


def test_ball_count_reads_package_names():
    assert bm.ball_count("LFE5U-85F-8BG381I") == 381
    assert bm.ball_count("XC6SLX25-FTG256") == 256
    assert bm.ball_count("BGA-256_14.0x14.0mm_Layout16x16_P0.8mm") == 256
    assert bm.ball_count("APS51208N-OBR-BD") is None
    assert bm.ball_count("BGA-256", "CABGA381") is None


# --- the datasheet's ball list -----------------------------------------------

def _pages(lines, head="Ball Assignments"):
    return [{"page": 0, "text": "Electrical characteristics\nVCC 3.3 V"},
            {"page": 1, "text": head + "\n" + "\n".join(lines)}]


def test_a_ball_list_in_the_text_is_read():
    lines = [f"{b} {NAMES[b]} I/O" for b in BALLS]
    pin, why, used = bm.from_pages(_pages(lines), 16)
    assert why == "" and len(pin) == 16 and used == [2]


def test_a_list_continued_on_the_next_page_repeats_rows_harmlessly():
    lines = [f"{b} {NAMES[b]}" for b in BALLS]
    pages = _pages(lines[:10]) + [{"page": 2, "text": "Ball Assignments (cont.)\n"
                                   + "\n".join(lines[8:])}]
    pin, why, used = bm.from_pages(pages, 16)
    assert why == "" and used == [2, 3]


def test_a_drawn_ball_map_is_not_a_list():
    # a drawn grid's rows print ball after ball: no name follows any of them
    rows = [" ".join(f"{r}{c}" for c in range(1, 5)) for r in "ABCD"]
    pin, why, _ = bm.from_pages(_pages(rows, "Ball map (top view)"), 16)
    assert pin is None and "no ball list" in why


def test_a_ball_with_two_names_in_the_text_is_refused():
    lines = [f"{b} {NAMES[b]}" for b in BALLS] + ["C3 VDDQ"]
    pin, why, _ = bm.from_pages(_pages(lines), 16)
    assert pin is None and "C3 is listed twice" in why


def test_pages_that_never_say_ball_are_not_read():
    lines = [f"{b} {NAMES[b]}" for b in BALLS]
    assert bm.from_pages(_pages(lines, "Pin list"), 16)[0] is None


# --- the curated table -------------------------------------------------------

def test_the_table_names_each_training_part_once():
    for name in ("LFE5U-25F-6BG256C", "LFE5U-25F-8BG256x", "LFE5U-85F-8BG381I",
                 "XC6SLX25-FTG256", "xc6slx25-2ftg256c"):
        entry, why = bm.curated([name])
        assert why == "" and entry["balls"] == bm.ball_count(name)
    assert bm.curated(["LFE5U-45F-6BG381C"])[0] is None   # another die
    assert bm.curated(["XC6SLX25-CSG324"])[0] is None     # another package


def test_two_entries_for_one_part_is_not_a_choice(tmp_path):
    t = tmp_path / "t.yaml"
    t.write_text("parts:\n"
                 "  - {match: ['ABC-*'], balls: 16}\n"
                 "  - {match: ['ABC-1*'], balls: 25}\n", encoding="utf-8")
    entry, why = bm.curated(["abc-12"], t)
    assert entry is None and "2 entries" in why
    assert bm.curated(["ABC-2"], t)[0]["balls"] == 16


def test_every_table_entry_is_whole():
    for e in bm.load_table():
        assert e["format"] in bm.FORMATS and e["url"].startswith("https://")
        assert e["match"] and e["balls"] == bm.ball_count(e["package"])
        assert e["format"] != "lattice_csv" or e.get("column")


# --- the fetch ---------------------------------------------------------------

def _tr(body=b"", status=200, hops=None):
    def t(url):
        return {"status": status, "body": body, "final_url": (hops or [url])[-1],
                "hops": hops or [url], "headers": {}}
    return t


def test_fetch_keeps_a_text_file_and_its_hash(tmp_path):
    url = "https://www.latticesemi.com/view_document?document_id=1"
    hops = [url, "https://www.latticesemi.com/api/document/download?id=1"]
    got = bm.fetch_file(url, tmp_path / "f.csv", _tr(lattice_csv(), hops=hops))
    assert got["status"] == "fetched" and got["bytes"] == len(lattice_csv())
    assert (tmp_path / "f.csv").read_bytes() == lattice_csv()
    assert len(got["sha256"]) == 64


@pytest.mark.parametrize("url, tr, word", [
    ("https://example.com/pins.csv", _tr(b"x"), "refused"),
    ("https://www.latticesemi.com/d", _tr(b"x", hops=[
        "https://www.latticesemi.com/d", "https://evil.example/d"]), "refused"),
    ("https://www.latticesemi.com/d", _tr(b"%PDF-1.7 ..."), "refused"),
    ("https://www.latticesemi.com/d", _tr(b"<!DOCTYPE html><p>sign in"), "refused"),
    ("https://www.latticesemi.com/d", _tr(b"gone", status=404), "error"),
])
def test_fetch_refuses_what_is_not_the_makers_file(tmp_path, url, tr, word):
    got = bm.fetch_file(url, tmp_path / "f", tr)
    assert got["status"] == word and not (tmp_path / "f").exists()
