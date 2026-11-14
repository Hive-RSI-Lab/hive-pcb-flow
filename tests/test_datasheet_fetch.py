"""Datasheet fetches follow LCSC's HTML redirect stub, offline (brief 261028-141933).

wmsc.lcsc.com answers a datasheet URL with 200 text/html and a 140-byte
`<script>window.top.location.href='https://datasheet.lcsc.com/...'</script>`;
the PDF is at that target. Part of `hwde test`.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import datasheet_extract  # noqa: E402
import researchlib  # noqa: E402

WMSC = ("https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/"
        "1806151818_Hubei-KENTO-Elec-KT-0603G_C12624.pdf")
TARGET = ("https://datasheet.lcsc.com/datasheet/pdf/"
          "69e84a3abd84e5ab5856c47a2d0334ce.pdf?productCode=C12624")
STUB = f"<script>window.top.location.href='{TARGET}'</script>".encode()
PDF = b"%PDF-1.7\n" + b"x" * 2000


def site(stub_to: str = TARGET):
    def handle(req: httpx.Request) -> httpx.Response:
        if str(req.url) == WMSC:
            body = f"<script>window.top.location.href='{stub_to}'</script>"
            return httpx.Response(200, content=body.encode(),
                                  headers={"content-type": "text/html"})
        if str(req.url) == TARGET:
            return httpx.Response(200, content=PDF,
                                  headers={"content-type": "application/pdf"})
        return httpx.Response(404)
    return httpx.MockTransport(handle)


def test_stub_redirect():
    assert len(STUB) == 140, len(STUB)
    assert researchlib.stub_redirect(STUB) == TARGET
    assert researchlib.stub_redirect(
        b'<meta http-equiv="refresh" content="0; url=https://a.example/x.pdf">'
    ) == "https://a.example/x.pdf"
    assert researchlib.stub_redirect(
        b"<script>location.replace('/d/x.pdf')</script>") == "/d/x.pdf"
    assert researchlib.stub_redirect(PDF) is None
    assert researchlib.stub_redirect(b"<html>" + b" " * 5000 + STUB) is None
    assert researchlib.stub_redirect(
        b"<script>location.href='javascript:void(0)'</script>") is None


def test_http_transport_follows_stub():
    r = researchlib.http_transport(WMSC, _mock=site())
    assert r["body"] == PDF, r["body"][:60]
    assert r["hops"] == [WMSC, TARGET], r["hops"]
    assert r["final_url"] == TARGET


def fake(stub_to: str = TARGET):
    return lambda url: researchlib.http_transport(url, _mock=site(stub_to))


def test_fetch_workspace():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "parts").mkdir()
        (ws / "parts" / "parts.json").write_text(json.dumps({"parts": [
            {"lcsc": "C12624", "mpn": "KT-0603G", "datasheet": WMSC},
            {"lcsc": "C1", "mpn": "X"}]}))
        args = Namespace(fetch=str(ws), lcsc=None)
        out, code = datasheet_extract.do_fetch(args, transport=fake())
        assert code == 1, out          # C1 has no datasheet URL
        got = {r["lcsc"]: r for r in out["datasheets"]}
        assert got["C12624"]["status"] == "fetched", got["C12624"]
        assert got["C1"]["status"] == "missing"
        assert (ws / "parts/datasheets/C12624.pdf").read_bytes() == PDF
        idx = json.loads((ws / "parts/datasheets/index.json").read_text())
        assert idx["C12624"]["final_url"] == TARGET

        args = Namespace(fetch=str(ws), lcsc="C12624")
        out, code = datasheet_extract.do_fetch(args, transport=fake())
        assert code == 0 and out["fetched"] == 1, out


def test_stub_off_the_allowlist_is_refused():
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "x.pdf"
        r = datasheet_extract.fetch_datasheet(
            WMSC, dest, transport=fake("https://evil.example/x.pdf"))
        assert r["status"] == "refused" and "evil.example" in r["error"], r
        assert not dest.exists()


def test_html_is_never_written():
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "x.pdf"
        r = datasheet_extract.fetch_datasheet(
            WMSC, dest, transport=lambda u: {
                "status": 200, "final_url": u, "hops": [u],
                "body": b"<html>no pdf here</html>"})
        assert r["status"] == "refused" and "not a PDF" in r["error"], r
        assert not dest.exists()



def _pdf(pages: list[str]) -> bytes:
    """Minimal text PDF, one uncompressed content stream per page."""
    objs, kids = [], []
    for i, t in enumerate(pages):
        kids.append(f"{4 + 2 * i} 0 R")
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, t in enumerate(pages):
        c = f"BT /F1 12 Tf 72 700 Td ({t}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Contents {5 + 2 * i} 0 R /Resources << /Font << /F1 3 0 R >> >> >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(c) + c + b"\nendstream")
    out, offs = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % n + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)


NOTICE = _pdf(["LCSC Datasheet Notice - Datasheet temporarily unavailable"])
PDF = _pdf([f"KT-0603G datasheet page {i} " + "x" * 300 for i in range(1, 4)])  # a whole one (#706)
REAL = _pdf([f"Page {i} absolute maximum ratings VCC 5.5 V " + "x" * 300
             for i in range(1, 9)])


def _serve(body: bytes):
    return lambda u: {"status": 200, "final_url": u, "hops": [u], "body": body}


def test_lcsc_notice_pdf_is_missing_not_fetched():
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "x.pdf"
        r = datasheet_extract.fetch_datasheet(WMSC, dest, transport=_serve(NOTICE))
        assert r["status"] == "missing" and r["stub"] is True, r
        assert not dest.exists()
        r = datasheet_extract.fetch_datasheet(WMSC, dest, transport=_serve(REAL))
        assert r["status"] == "fetched" and dest.read_bytes() == REAL, r


def test_notice_reworded_hits_size_backstop():
    reworded = _pdf(["Sorry, this datasheet is currently unavailable."])
    assert datasheet_extract.stub_notice(reworded)
    assert datasheet_extract.stub_notice(REAL) is None


def test_fetch_workspace_reports_stub_as_missing():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "parts").mkdir()
        (ws / "parts" / "parts.json").write_text(json.dumps({"parts": [
            {"lcsc": "C194022", "mpn": "X", "datasheet": WMSC}]}))
        out, code = datasheet_extract.do_fetch(
            Namespace(fetch=str(ws), lcsc=None), transport=_serve(NOTICE))
        assert code == 1 and out["fetched"] == 0, out
        assert out["datasheets"][0]["status"] == "missing"
        assert not (ws / "parts/datasheets/C194022.pdf").exists()
        assert not (ws / "parts/datasheets/index.json").exists()


def test_pdf_mode_refuses_a_stub_on_disk():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "C1.pdf"
        f.write_bytes(NOTICE)
        pdf, err = datasheet_extract._checked_pdf(str(f))
        assert pdf is None and "placeholder" in err["error"], err
        f.write_bytes(REAL)
        assert datasheet_extract._checked_pdf(str(f))[1] is None


# ---------------------------------------------- suspect datasheets (#706)

RIGHT = _pdf([f"TESTPART123 datasheet page {i} " + "x" * 300 for i in range(1, 5)])
WRONG = _pdf([f"OTHERCHIP999 datasheet page {i} " + "x" * 300 for i in range(1, 5)])
TRUNC = RIGHT[:len(RIGHT) * 2 // 3]
NOTEXT = _pdf([""] * 4)
MPN = "TESTPART123-QFN"     # the orderable code; the family is TESTPART123


def test_assess_right_part_is_ok_with_page_and_text_counts():
    a = datasheet_extract.assess_pdf(RIGHT, MPN)
    assert a["status"] == "ok" and a["reasons"] == [], a
    assert a["pages"] == 4 and len(a["chars_per_page"]) == 4
    assert all(c > 300 for c in a["chars_per_page"]) and a["text_chars"] > 1200


def test_assess_wrong_part_is_suspect():
    a = datasheet_extract.assess_pdf(WRONG, MPN)
    assert a["status"] == "suspect" and a["pages"] == 4, a
    assert any("TESTPART123" in r and "pages 1-2" in r for r in a["reasons"]), a


def test_assess_truncated_file_is_suspect():
    a = datasheet_extract.assess_pdf(TRUNC, MPN)
    assert a["status"] == "suspect", a
    assert any("truncated" in r for r in a["reasons"]), a


def test_assess_no_text_layer_is_suspect():
    a = datasheet_extract.assess_pdf(NOTEXT, MPN)
    assert a["status"] == "suspect" and a["text_chars"] == 0, a
    assert any("no text layer" in r for r in a["reasons"]), a


def test_assess_without_an_mpn_skips_only_the_name_check():
    assert datasheet_extract.assess_pdf(WRONG, None)["status"] == "ok"
    assert datasheet_extract.assess_pdf(NOTEXT, None)["status"] == "suspect"


def test_fetch_suspect_is_not_fetched_but_is_kept_and_indexed():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "parts").mkdir()
        (ws / "parts" / "parts.json").write_text(json.dumps({"parts": [
            {"lcsc": "C1", "mpn": MPN, "datasheet": WMSC},
            {"lcsc": "C2", "mpn": MPN, "datasheet": WMSC}]}))
        bodies = {"C1": WRONG, "C2": RIGHT}
        for lcsc, body in bodies.items():
            out, code = datasheet_extract.do_fetch(
                Namespace(fetch=str(ws), lcsc=lcsc), transport=_serve(body))
            r = out["datasheets"][0]
            assert r["status"] == ("suspect" if body is WRONG else "fetched"), r
            assert (ws / f"parts/datasheets/{lcsc}.pdf").is_file()
            assert code == (1 if body is WRONG else 0), out
        idx = json.loads((ws / "parts/datasheets/index.json").read_text())
        assert idx["C1"]["status"] == "suspect" and idx["C1"]["reasons"], idx
        assert idx["C1"]["pages"] == 4 and len(idx["C1"]["chars_per_page"]) == 4
        assert idx["C2"]["status"] == "ok" and idx["C2"]["reasons"] == [], idx


# ------------------------------------- series datasheets (#755)
#
# A passive's datasheet covers a series and never prints the orderable code.
# These are the ordering-code sections of the real PDFs (fmcw-radar, 2026-11),
# cut to what reference/ordering_codes.yaml reads.

JUNK = "November 2015 1.86g MLCC " + "x" * 200
SAMSUNG = ("Part Numbering System CL 10 A 106 M P 8 N N N C "
           "1. SERIES CODE CL = Multi layer Ceramic Capacitors "
           "2. SIZE CODE 02 = 01005(0402) 21 = 0805(2012) 43 = 1812(4532) "
           "05 = 0402(1005) 32 = 1210(3225) 10 = 0603(1608) 42 = 1808(4520) "
           "3. DIELECTRIC CODE C = C0G A = X5R F = Y5V B = X7R X = X6S "
           "4. CAPACITANCE CODE 5. TOLERANCE CODE K = 10% M = 20% "
           "6. RATED VOLTAGE CODE R = 4V O = 16V B = 50V Q = 6.3V A = 25V "
           "P = 10V L = 35V")
YAGEO_RT = ("THIN FILM CHIP RESISTORS RT series ORDERING INFORMATION - GLOBAL "
            "PART NUMBER RT XXXX F X X XX XXXX L (1) SIZE "
            "RT0100 / 0201 / 0402 / 0603 / 0805 / 1206 (2) TOLERANCE "
            "L = 0.01% B = 0.1% D = 0.5% F = 1% (3) PACKAGING TYPE "
            "R = Paper/PE taping reel (4) TEMPERATURE COEFFICIENT "
            "A = 5 ppm/C D = 25 ppm/C (5) TAPING REEL 07 = 7 inch dia. Reel "
            "(6) RESISTANCE VALUE")
YAGEO_CC = ("Class 2, X5R 4 V TO 50 V ORDERING INFORMATION - GLOBAL PART "
            "NUMBER CC XXXX X X X5R X BB XXX (1) SIZE - INCH BASED (METRIC) "
            "0201 (0603) 0402 (1005) 0603 (1608) (2) TOLERANCE K = 10% "
            "M = 20% (3) PACKING STYLE R = Paper/PE taping reel "
            "(4) RATED VOLTAGE 4 = 4 V 5 = 6.3 V 6 = 10 V 7 = 16 V "
            "(5) CAPACITANCE VALUE")
UNIROYAL = ("Thick Film Chip Resistors Part Name 01005/0201/0402/0603/0805/"
            "1206/2512 Series 2. Explanation of Part No. System Part No. "
            "includes 14 codes 2.1 1st~4th codes: Part name 2.2 5th~6th "
            "codes: Power rating Normal Size WH 07 W2 W3 W4 W8 WA WG WM 1W "
            "2.3 7th code: Tolerance. D=0.5% F=1% G=2% J=5% 2.4 8th~11th "
            "codes: Resistance Value 12th code T=Tape/Reel")
JST = ("SH CONNECTOR Model No. BM06B-SRSS-TB BM07B-SRSS-TB "
       "SM05B-SRSS-TB SM06B-SRSS-TB SM07B-SRSS-TB")
# SiTime's guide as pypdf reads it, curly quotes and all; the PDF helper
# here can't set those, so the PDF copy has straight ones.
SITIME_TEXT = ("Ordering Information The Part No. Guide is for reference only. "
               "Packaging \u201cD\u201d: 8 mm Tape & Reel, 3 ku reel \u201cT\u201d: "
               "12/16 mm Tape & Reel, 3 ku reel \u201cE\u201d: 8 mm Tape & Reel, "
               "1 ku reel Blank for Bulk Frequency Refer to Frequency List below "
               "Part Family \u201cSiT1602\u201d Revision Letter \u201cB\u201d is the "
               "revision Temperature Range \u201cC\u201d Commercial, -20 to 70\u00baC "
               "\u201cI\u201d Industrial, -40 to 85\u00baC Output Drive Strength "
               "\u201c\u2013\u201d Default (datasheet limits) \u201cL\u201d \u201cT\u201d "
               "Package Size \u201c7\u201d 2.0 x 1.6 mm \u201c1\u201d 2.5 x 2.0 mm "
               "\u201c2\u201d 3.2 x 2.5 mm Frequency Stability \u201c1\u201d for "
               "\u00b120 ppm \u201c2\u201d for \u00b125 ppm \u201c3\u201d for \u00b150 ppm "
               "Feature Pin \u201cE\u201d for Output Enable \u201cS\u201d for Standby "
               "\u201cN\u201d for No Connect Supply Voltage \u201c18\u201d for 1.8 V "
               "\u00b110% \u201c33\u201d for 3.3 V \u00b110% \u201cXX\u201d for 2.25 V "
               "to 3.63 V")
SITIME = SITIME_TEXT.translate({0x201c: '"', 0x201d: '"', 0x2013: "-",
                                0xb1: "+/-", 0xba: " deg "})
FAMILY_DOCS = {"samsung": SAMSUNG, "yageo_rt": YAGEO_RT, "yageo_cc": YAGEO_CC,
               "uniroyal": UNIROYAL, "jst": JST, "sitime": SITIME}
FAMILY_MPNS = {"samsung": ["CL05B104KO5NNNC", "CL10A106KP8NNNC"],
               "yageo_rt": ["RT0402BRD0712KL"],
               "yageo_cc": ["CC0402KRX5R6BB474"],
               "uniroyal": ["0402WGF1001TCE", "0603WAF100JT5E"],
               "jst": ["SM06B-SRSS-TB"],
               "sitime": ["SIT1602BI-22-18E-25.000000E",
                          "SiT1602BC-12-18E-66.666660D",
                          "SiT1602BIL22-XXE-25.000000T"]}


def _doc(section: str) -> bytes:
    """Page 1-2 junk (as the real ones are), the ordering section after."""
    return _pdf([JUNK, JUNK, section, JUNK])


def test_series_datasheet_decodes_each_known_family():
    for family, mpns in FAMILY_MPNS.items():
        for mpn in mpns:
            a = datasheet_extract.assess_pdf(_doc(FAMILY_DOCS[family]), mpn)
            assert a["status"] == "ok" and a["reasons"] == [], (mpn, a)


def test_cl_series_datasheet_for_c1525_says_how_it_matched():
    a = datasheet_extract.assess_pdf(_doc(SAMSUNG), "CL05B104KO5NNNC")
    assert a["match"]["how"] == "series" and a["match"]["scheme"] == "samsung_cl"
    assert a["match"]["fields"]["size"] == "05"
    assert a["match"]["fields"]["dielectric"] == "B"
    assert a["match"]["fields"]["voltage"] == "O"


def test_sitime_guide_decodes_with_its_own_curly_quotes():
    match, why = datasheet_extract.series_match("SIT1602BI-22-18E-25.000000E",
                                                SITIME_TEXT)
    assert why is None and match["scheme"] == "sitime_osc", (match, why)
    f = match["fields"]
    assert (f["family"], f["revision"], f["temperature"], f["drive"]) == \
        ("1602", "B", "I", "")
    assert (f["size"], f["stability"], f["voltage"], f["feature"],
            f["packing"]) == ("2", "2", "18", "E", "E")
    match, why = datasheet_extract.series_match("SIT1602BI-22-18E-25.000000E",
                                                SITIME_TEXT.replace("\u201c2\u201d 3.2", "x"))
    assert match is None and "size=2" in why, (match, why)


def test_named_datasheet_says_how_it_matched():
    assert datasheet_extract.assess_pdf(RIGHT, MPN)["match"]["how"] == "family"
    whole = _pdf([f"{MPN} datasheet " + "x" * 300])
    assert datasheet_extract.assess_pdf(whole, MPN)["match"]["how"] == "whole"
    a = datasheet_extract.assess_pdf(_doc(JST), "SM06B-SRSS-TB")
    assert a["match"]["how"] == "document" and a["match"]["page"] == 3


def test_wrong_family_stays_suspect_for_every_pair():
    for doc_family, doc in FAMILY_DOCS.items():
        for family, mpns in FAMILY_MPNS.items():
            if family == doc_family:
                continue
            for mpn in mpns:
                a = datasheet_extract.assess_pdf(_doc(doc), mpn)
                assert a["status"] == "suspect", (mpn, doc_family, a)
                assert "match" not in a, a


def test_the_right_series_with_a_field_it_cannot_decode_stays_suspect():
    for mpn, doc in [("CL77B104KO5NNNC", SAMSUNG),      # no size 77
                     ("CL05G104KO5NNNC", SAMSUNG),      # no dielectric G
                     ("CL05B104KZ5NNNC", SAMSUNG),      # no voltage Z
                     ("CC0402KRX7R6BB474", YAGEO_CC),   # the X5R-only document
                     ("CC9999KRX5R6BB474", YAGEO_CC),
                     ("RT9999BRD0712KL", YAGEO_RT),
                     ("0402WZF1001TCE", UNIROYAL),      # no power code WZ
                     ("SIT1602BI-92-18E-25.000000E", SITIME),   # no size 9
                     ("SIT1602BI-25-18E-25.000000E", SITIME),   # no stability 5
                     ("SIT1602BX-22-18E-25.000000E", SITIME),   # no range X
                     ("SIT1602BI-22-15E-25.000000E", SITIME),   # no 15 supply
                     ("SIT1602BI-22-18Q-25.000000E", SITIME),   # no feature Q
                     ("SIT1602BI-22-18E-25.000000Z", SITIME),   # no packing Z
                     ("SIT1602CI-22-18E-25.000000E", SITIME),   # revision C
                     ("SIT8008BI-22-18E-25.000000E", SITIME)]:  # another family
        a = datasheet_extract.assess_pdf(_doc(doc), mpn)
        assert a["status"] == "suspect", (mpn, a)
        assert any(mpn in r for r in a["reasons"]), a


def test_a_series_name_alone_decodes_nothing():
    # names the series word, has no ordering table
    prose = "CL = Multi layer Ceramic Capacitors, RT series, general catalog"
    for mpn in ("CL05B104KO5NNNC", "RT0402BRD0712KL"):
        assert datasheet_extract.assess_pdf(_doc(prose), mpn)["status"] == "suspect"


def test_the_whole_mpn_deep_in_another_parts_datasheet_stays_suspect():
    # another part's document naming this one in passing: no ordering words
    # on that page. The base family deep in a document never counts.
    aside = "Decouple the rail with one USBLC6-2SC6 on each input"
    a = datasheet_extract.assess_pdf(_doc(aside), "USBLC6-2SC6")
    assert a["status"] == "suspect", a
    table = "Model No. USBLC6-2SC6X"
    a = datasheet_extract.assess_pdf(_doc(table), "USBLC6-2P6")
    assert a["status"] == "suspect", a


def test_series_match_is_in_the_index_after_a_fetch():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "parts").mkdir()
        (ws / "parts" / "parts.json").write_text(json.dumps({"parts": [
            {"lcsc": "C1525", "mpn": "CL05B104KO5NNNC", "datasheet": WMSC}]}))
        out, code = datasheet_extract.do_fetch(
            Namespace(fetch=str(ws), lcsc=None), transport=_serve(_doc(SAMSUNG)))
        assert code == 0, out
        idx = json.loads((ws / "parts/datasheets/index.json").read_text())
        assert idx["C1525"]["status"] == "ok"
        assert idx["C1525"]["match"]["scheme"] == "samsung_cl"


def _assess_ws(d: str, index: dict, pdfs: dict[str, bytes]) -> Path:
    ws = Path(d)
    ddir = ws / "parts" / "datasheets"
    ddir.mkdir(parents=True)
    (ddir / "index.json").write_text(json.dumps(index))
    for lcsc, body in pdfs.items():
        (ddir / f"{lcsc}.pdf").write_bytes(body)
    return ws


def test_assess_writes_a_status_into_old_entries_and_keeps_the_rest():
    old = {"file": "parts/datasheets/x.pdf", "url": "u", "sha256": "s"}
    with tempfile.TemporaryDirectory() as d:
        ws = _assess_ws(d, {
            "C1525": {**old, "mpn": "CL05B104KO5NNNC"},
            "C2": {**old, "mpn": "CL05B104KO5NNNC"},
            "C3": {**old, "mpn": "CL05B104KO5NNNC"}},
            {"C1525": _doc(SAMSUNG), "C2": _doc(YAGEO_RT)})
        out, code = datasheet_extract.do_assess(Namespace(assess=str(ws),
                                                          lcsc=None))
        assert code == 1, out
        assert (out["ok"], out["suspect"], out["unassessed"]) == (1, 1, 1), out
        idx = json.loads((ws / "parts/datasheets/index.json").read_text())
        assert idx["C1525"]["status"] == "ok" and idx["C1525"]["url"] == "u"
        assert idx["C2"]["status"] == "suspect" and idx["C2"]["reasons"]
        # no PDF to read: stays unassessed, never ok
        assert idx["C3"]["status"] == "unassessed" and idx["C3"]["reasons"]


def test_assess_takes_the_mpn_from_parts_json_for_an_entry_without_one():
    with tempfile.TemporaryDirectory() as d:
        ws = _assess_ws(d, {"C1525": {"url": "u"}}, {"C1525": _doc(SAMSUNG)})
        (ws / "parts" / "parts.json").write_text(json.dumps({"parts": [
            {"lcsc": "C1525", "mpn": "CL05B104KO5NNNC"}]}))
        out, code = datasheet_extract.do_assess(Namespace(assess=str(ws),
                                                          lcsc=None))
        assert code == 0, out
        idx = json.loads((ws / "parts/datasheets/index.json").read_text())
        assert idx["C1525"]["status"] == "ok"


def test_assess_without_any_index_is_a_bad_workspace():
    with tempfile.TemporaryDirectory() as d:
        out, code = datasheet_extract.do_assess(Namespace(assess=d, lcsc=None))
        assert code == 2 and out["status"] == "error", out


# ---- the family match respects words and breaks (#768) -------------------

CRYSTAL = "Output Frequency  8.000000MHz\nOutput Type ： CMOS\nSupply 3.3V"


def test_family_match_does_not_span_a_colon_between_words():
    # the false pass of #768: 7X80000600's PDF "passed" as TYPE-C-31-M-12
    assert datasheet_extract.mpn_in_text("TYPE-C-31-M-12", CRYSTAL) is None
    assert not datasheet_extract.match_mpn("TYPE-C-31-M-12", [CRYSTAL])[0]


@pytest.mark.parametrize("text", [
    "Output Type CMOS",            # a plain space at the MPN's own hyphen
    "Type\nCMOS",                  # a line break
    "Type, CMOS",
    "Type: C",
    "Type (C)",
])
def test_family_match_does_not_cross_a_real_break(text):
    assert datasheet_extract.mpn_in_text("TYPE-C-31-M-12", text) is None


@pytest.mark.parametrize("mpn,text", [
    ("NE555P", "XNE555 timer"),         # mid-word
    ("NE555P", "1NE555 timer"),
    ("TYPE-C-31-M-12", "PROTOTYPE-C board"),
    ("ABCDE1239", "ABC DE123"),          # a space the MPN does not have
])
def test_family_match_starts_at_a_word_and_stays_in_one(mpn, text):
    assert datasheet_extract.mpn_in_text(mpn, text) is None


@pytest.mark.parametrize("mpn,text,form", [
    ("BGT60TR13CE6327XUMA1", "the BGT60TR13C radar", "BGT60TR13C"),
    ("BGT60TR13CE6327XUMA1", "BGT60TR13C E6327", "BGT60TR13C"),
    ("NE555P", "NE555 timer", "NE555"),
    ("NE555P", "(NE555) timer", "NE555"),
    ("NE555P", "NE555D and NE555", "NE555"),     # a sibling's suffix is fine
    ("TPS7A2036PDBVR", "TPS7A20 300 mA LDO", "TPS7A20"),
    ("TPS259531DSGR", "TPS25953x eFuse", "TPS25953"),
    ("SN74AVC4T774PWR", "SN74AVC4T774\nProcess", "SN74AVC4T774"),
    ("ESP32-S3-WROOM-1-N16R8", "ESP32-S3-WROOM-1\nDatasheet",
     "ESP32S3WROOM1"),
    ("ESP32-S3-WROOM-1-N16R8", "ESP32 S3 WROOM 1 module", "ESP32S3WROOM1"),
    ("TYPE-C-31-M-12", "TYPE-C connector", "TYPEC"),
    ("TYPE-C-31-M-12", "Type C-31 receptacle", "TYPEC31"),
    ("LM2596S-5.0", "LM2596S 5.0 V", "LM2596S50"),
])
def test_family_matches_that_stay(mpn, text, form):
    assert datasheet_extract.mpn_in_text(mpn, text) == form


def test_the_whole_mpn_and_the_half_and_five_character_floor():
    f = datasheet_extract.mpn_in_text
    assert f("USBLC6-2SC6", "USBLC6-2SC6 TVS") == "USBLC62SC6"
    assert f("AB-12", "ab12") == "AB12"              # shorter than 5: whole
    assert f("ABCDEF", "ABCDE") == "ABCDE"           # 5 of 6
    assert f("ABCDEFG", "ABCD") is None              # below 5
    assert f("ABCDEFGHIJKL", "ABCDEF") == "ABCDEF"   # half of 12
    assert f("ABCDEFGHIJKL", "ABCDE") is None        # under half
    assert f("", "anything") is None
    assert "half" in datasheet_extract.mpn_in_text.__doc__
    assert "60%" not in datasheet_extract.mpn_in_text.__doc__


# ---- the assessment runs by itself where an index is read (#768) -----------

def _counting_assess(monkeypatch):
    calls = []
    real = datasheet_extract.assess_pdf

    def assess(data, mpn=None):
        calls.append(mpn)
        return real(data, mpn)
    monkeypatch.setattr(datasheet_extract, "assess_pdf", assess)
    monkeypatch.setattr(datasheet_extract, "_VERDICTS", {})
    return calls


def _index_of(ws):
    return json.loads((ws / "parts/datasheets/index.json").read_text())


OLD = {"file": "x", "url": "u", "sha256": "s", "mpn": MPN}


def test_reading_an_index_assesses_its_pdfs_and_records_them(tmp_path):
    ws = _assess_ws(str(tmp_path), {"C1": OLD, "C2": OLD},
                    {"C1": RIGHT, "C2": WRONG})
    parts = ws / "parts"
    assert list(datasheet_extract.suspect_datasheets(parts, ["C1", "C2"])) \
        == ["C2"]
    idx = _index_of(ws)
    assert idx["C1"]["status"] == "ok" and idx["C2"]["status"] == "suspect"
    assert idx["C1"]["url"] == "u"     # the rest of the entry stays
    assert idx["C1"]["assessed"]["sha256"] == hashlib.sha256(RIGHT).hexdigest()
    assert datasheet_extract.unassessed_datasheets(parts, ["C1", "C2"]) == []


def test_a_second_read_does_not_parse_or_rewrite(tmp_path, monkeypatch):
    calls = _counting_assess(monkeypatch)
    ws = _assess_ws(str(tmp_path), {"C1": OLD}, {"C1": RIGHT})
    parts = ws / "parts"
    datasheet_extract.suspect_datasheets(parts, ["C1"])
    assert calls == [MPN]
    index = parts / "datasheets" / "index.json"
    written = index.stat().st_mtime_ns
    datasheet_extract.suspect_datasheets(parts, ["C1"])
    datasheet_extract.unassessed_datasheets(parts, ["C1"])
    assert calls == [MPN] and index.stat().st_mtime_ns == written


@pytest.mark.parametrize("change", ["pdf", "mpn", "version"])
def test_a_verdict_is_redone_when_what_it_judged_changes(tmp_path, monkeypatch,
                                                         change):
    calls = _counting_assess(monkeypatch)
    ws = _assess_ws(str(tmp_path), {"C1": OLD}, {"C1": RIGHT})
    parts = ws / "parts"
    ddir = parts / "datasheets"
    datasheet_extract.suspect_datasheets(parts, ["C1"])
    assert _index_of(ws)["C1"]["status"] == "ok"
    if change == "pdf":
        (ddir / "C1.pdf").write_bytes(WRONG)
        want = "suspect"
    elif change == "mpn":
        idx = _index_of(ws)
        idx["C1"]["mpn"] = "OTHERPART99"
        (ddir / "index.json").write_text(json.dumps(idx))
        want = "suspect"
    else:
        monkeypatch.setattr(datasheet_extract, "ASSESS_VERSION",
                            datasheet_extract.ASSESS_VERSION + 1)
        want = "ok"
    datasheet_extract.suspect_datasheets(parts, ["C1"])
    assert len(calls) == 2 and _index_of(ws)["C1"]["status"] == want


def test_an_old_verdict_with_no_stamp_is_redone(tmp_path, monkeypatch):
    # written by the matcher before this one: it may be the TYPEC false pass
    calls = _counting_assess(monkeypatch)
    ws = _assess_ws(str(tmp_path), {"C1": {**OLD, "status": "ok"}},
                    {"C1": WRONG})
    assert list(datasheet_extract.suspect_datasheets(ws / "parts", ["C1"])) \
        == ["C1"]
    assert calls == [MPN]


def test_a_pdf_with_no_entry_gets_one_with_the_mpn_from_parts_json(tmp_path):
    ws = _assess_ws(str(tmp_path), {}, {"C1": RIGHT, "C2": WRONG})
    (ws / "parts/parts.json").write_text(json.dumps({"parts": [
        {"lcsc": "C1", "mpn": MPN}, {"lcsc": "C2", "mpn": MPN}]}))
    assert list(datasheet_extract.suspect_datasheets(ws / "parts",
                                                     ["C1", "C2"])) == ["C2"]
    idx = _index_of(ws)
    assert idx["C1"]["status"] == "ok" and idx["C1"]["mpn"] == MPN
    assert idx["C1"]["file"] == "parts/datasheets/C1.pdf"


def test_a_pdf_with_no_mpn_anywhere_is_read_but_not_called_ok(tmp_path):
    ws = _assess_ws(str(tmp_path), {}, {"C1": RIGHT, "C2": TRUNC})
    parts = ws / "parts"
    assert list(datasheet_extract.suspect_datasheets(parts, ["C1", "C2"])) \
        == ["C2"]
    assert datasheet_extract.unassessed_datasheets(parts, ["C1", "C2"]) \
        == ["C1"]
    assert "no MPN" in _index_of(ws)["C1"]["reasons"][0]


def test_a_read_writes_nothing_when_no_pdf_is_on_disk(tmp_path):
    # no new knowledge: a check must not dirty every committed index in a fleet
    ws = _assess_ws(str(tmp_path), {"C1": OLD}, {})
    index = ws / "parts/datasheets/index.json"
    before = index.read_bytes()
    assert datasheet_extract.suspect_datasheets(ws / "parts", ["C1"]) == {}
    assert datasheet_extract.unassessed_datasheets(ws / "parts", ["C1"]) \
        == ["C1"]
    assert index.read_bytes() == before


def test_the_verdict_of_a_pdf_since_gone_stands(tmp_path):
    ws = _assess_ws(str(tmp_path), {"C1": OLD}, {"C1": WRONG})
    parts = ws / "parts"
    assert "C1" in datasheet_extract.suspect_datasheets(parts, ["C1"])
    (parts / "datasheets/C1.pdf").unlink()
    assert "C1" in datasheet_extract.suspect_datasheets(parts, ["C1"])


def test_a_tree_it_cannot_write_is_still_judged_once(tmp_path, monkeypatch):
    calls = _counting_assess(monkeypatch)
    ws = _assess_ws(str(tmp_path), {"C1": OLD}, {"C1": WRONG})

    def no_write(*_a, **_k):
        raise OSError("read-only file system")
    monkeypatch.setattr(datasheet_extract, "_write_index", no_write)
    parts = ws / "parts"
    assert "C1" in datasheet_extract.suspect_datasheets(parts, ["C1"])
    assert datasheet_extract.unassessed_datasheets(parts, ["C1"]) == []
    assert calls == [MPN] and "status" not in _index_of(ws)["C1"]


def test_no_datasheets_dir_is_nothing_and_creates_nothing(tmp_path):
    parts = tmp_path / "parts"
    assert datasheet_extract.suspect_datasheets(parts, ["C1"]) == {}
    assert datasheet_extract.unassessed_datasheets(parts, ["C1"]) == []
    assert not parts.exists()


def test_without_pypdf_a_read_records_nothing(tmp_path, monkeypatch):
    def no_pypdf(*_a, **_k):
        raise ImportError("pypdf")
    monkeypatch.setattr(datasheet_extract, "assess_pdf", no_pypdf)
    monkeypatch.setattr(datasheet_extract, "_VERDICTS", {})
    ws = _assess_ws(str(tmp_path), {"C1": OLD}, {"C1": RIGHT})
    assert datasheet_extract.unassessed_datasheets(ws / "parts", ["C1"]) \
        == ["C1"]
    assert "status" not in _index_of(ws)["C1"]


def test_assess_and_fetch_stamp_so_the_next_read_does_not_parse(tmp_path,
                                                                monkeypatch):
    ws = _assess_ws(str(tmp_path), {"C1": OLD}, {"C1": RIGHT})
    datasheet_extract.do_assess(Namespace(assess=str(ws), lcsc=None))
    calls = _counting_assess(monkeypatch)
    datasheet_extract.suspect_datasheets(ws / "parts", ["C1"])
    assert calls == []
    # --assess reads again even when nothing changed: it is the forced form
    datasheet_extract.do_assess(Namespace(assess=str(ws), lcsc=None))
    assert calls == [MPN]


def test_a_fetched_verdict_is_stamped_and_not_read_again(tmp_path, monkeypatch):
    ws = tmp_path
    (ws / "parts").mkdir()
    (ws / "parts/parts.json").write_text(json.dumps({"parts": [
        {"lcsc": "C1", "mpn": MPN, "datasheet": WMSC}]}))

    datasheet_extract.do_fetch(Namespace(fetch=str(ws), lcsc=None),
                               transport=_serve(RIGHT))
    assert _index_of(ws)["C1"]["assessed"]["mpn"] == MPN
    calls = _counting_assess(monkeypatch)
    datasheet_extract.suspect_datasheets(ws / "parts", ["C1"])
    assert calls == []


# ---- the page the warning points at (#768) ---------------------------------

def test_the_suspect_remediation_page_covers_each_case():
    page = (Path(__file__).resolve().parent.parent / "reference" / "remediations"
            / "rating_datasheet_suspect.md").read_text(encoding="utf-8")
    assert page.startswith("# rating_datasheet_suspect")
    for section in ("## Is it real?", "## Fix ladder", "## Do not", "## Verify",
                    "no text layer", "never names", "ordering table",
                    "ordering_codes.yaml", "--force", "--fetch"):
        assert section in page, section
