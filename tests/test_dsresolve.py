"""dsresolve finds a datasheet by maker for a part whose listing names none,
offline: a stand-in transport serves the maker's site and LCSC, a stand-in
search stands for JLC's catalogue. Part of `hwde test`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import dsresolve  # noqa: E402
import researchlib  # noqa: E402
from test_datasheet_fetch import _pdf  # noqa: E402


def _sheet(name: str) -> bytes:
    return _pdf([f"{name} 3-A step-down converter datasheet " + "x" * 300,
                 "Absolute Maximum Ratings " + "y" * 300])


def _serve(pages: dict):
    """A transport: each URL in `pages` serves its body, any other a 404;
    a body that is an exception is raised (a stalled host)."""
    def transport(url):
        body = pages.get(url, b"<html>not here</html>")
        if isinstance(body, Exception):
            raise body
        return {"status": 200 if url in pages else 404, "final_url": url,
                "hops": [url], "body": body}
    return transport


@pytest.fixture(autouse=True)
def _fresh_breaker(monkeypatch):
    dsresolve._DEAD.clear()
    monkeypatch.setattr(dsresolve, "_sleep", SLEPT.append)  # no real backoff

    def offline(code):
        raise AssertionError(f"LCSC page {code} asked for in a test")
    monkeypatch.setattr(dsresolve, "lcsc_page", offline)
    SLEPT.clear()
    yield
    dsresolve._DEAD.clear()


SLEPT: list[float] = []


TI = "https://www.ti.com/lit/ds/symlink/{}.pdf"


def test_a_planted_wrong_part_datasheet_is_refused(tmp_path):
    """The maker's URL for TPS62086 serves TPS620865's datasheet: refused,
    and named so, not taken."""
    planted = _sheet("TPS620865")
    pages = {TI.format(c.lower()): planted
             for c in dsresolve.candidates("TPS62086RLTR")}
    got = dsresolve.resolve("TPS62086RLTR", "Texas Instruments",
                            tmp_path / "p.pdf", transport=_serve(pages),
                            search=lambda q: [])
    assert got["miss"] == "refused" and "url" not in got, got
    assert any(t["kind"] == "wrong" for t in got["tried"])
    assert not (tmp_path / "p.pdf").exists()


def test_its_quiet_twin_is_taken_and_cited(tmp_path):
    """The same part, its own datasheet at the cut part number: taken, with
    where its URL came from."""
    pages = {TI.format("tps62086"): _sheet("TPS62086")}
    got = dsresolve.resolve("TPS62086RLTR", "TI(德州仪器)", tmp_path / "p.pdf",
                            transport=_serve(pages), search=lambda q: [])
    assert got["url"] == TI.format("tps62086"), got
    assert got["maker"] == "Texas Instruments"
    assert got["via"].startswith("Texas Instruments URL pattern "
                                 "https://www.ti.com/lit/ds/symlink/")
    assert "for TPS62086" in got["via"]
    assert (tmp_path / "p.pdf").read_bytes().startswith(b"%PDF")
    assert got["got"]["status"] == "fetched"


@pytest.mark.parametrize("mpn,brand", [
    ("AMS1117-3.3", "UMW(友台半导体)"),      # a clone's brand: not the original's maker
    ("TESTLDO", "Acme"),
    ("LM358", None),                        # second-sourced: no family pins a maker
])
def test_a_maker_the_table_does_not_know_is_named(tmp_path, mpn, brand):
    got = dsresolve.resolve(mpn, brand, tmp_path / "p.pdf",
                            transport=_serve({}), search=lambda q: [])
    assert got == {"miss": "maker not known", "why": got["why"], "tried": []}


# #1434: the brand strings LCSC lists fmcw-radar's passives and connectors
# under; "CJ" (Jiangsu Changjing) is not cjiang (Changjiang Microelectronics)
@pytest.mark.parametrize("brand,maker", [
    ("FH (Guangdong Fenghua Advanced Tech)", "Fenghua"),
    ("Hubei KENTO Elec", "KENTO"),
    ("TXC Corp", "TXC"),
    ("Korean Hroparts Elec", "HRO"),
    ("JSMSEMI", "JSMSEMI"),
    ("cjiang (Changjiang Microelectronics Tech)", "Changjiang Microelectronics"),
    ("XKB Connection", "XKB"),
    ("CJ(江苏长电/长晶)", None),
])
def test_a_listing_brand_names_its_maker(brand, maker):
    m, _ = dsresolve.maker_of("KT-0603R", brand, dsresolve.load_makers())
    assert (m and m["name"]) == maker


def test_a_known_maker_with_no_pattern_tries_lcscs_copy_under_its_brand(tmp_path):
    """KENTO has no URL pattern: LCSC's copy is tried, and one listed under
    another brand is passed over."""
    right = "https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/KT-0603R_C2286.pdf"
    other = "https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/KT-0603R_C9.pdf"
    hits = [{"lcsc": "C9", "mpn": "KT-0603R", "brand": "Everlight", "datasheet": other},
            {"lcsc": "C2286", "mpn": "KT-0603R", "brand": "Hubei KENTO Elec",
             "datasheet": right}]
    got = dsresolve.resolve("KT-0603R", "Hubei KENTO Elec", tmp_path / "p.pdf",
                            transport=_serve({right: _sheet("KT-0603R"),
                                              other: _sheet("KT-0603R")}),
                            search=lambda q: hits)
    assert got["url"] == right and got["maker"] == "KENTO", got
    assert [t["url"] for t in got["tried"]][-1] == right
    assert other not in [t["url"] for t in got["tried"]]


def test_no_pdf_anywhere_is_not_found(tmp_path):
    got = dsresolve.resolve("TPS62086RLTR", None, tmp_path / "p.pdf",
                            transport=_serve({}), search=lambda q: [])
    assert got["maker"] == "Texas Instruments"
    assert got["miss"] == "not found"
    assert got["tried"][-1]["url"].startswith("lcsc search ")


def test_lcsc_copy_is_the_named_fallback_and_skips_a_longer_part(tmp_path):
    """analog.com stalls; LCSC lists AD7245AQ (another part, one digit
    more), AD724JRZ under a clone's brand, and AD724JRZ under ADI's."""
    wrong = "https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/AD7245AQ_C650700.pdf"
    clone = "https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/clone_C1.pdf"
    right = "https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/AD724JRZ_C650728.pdf"
    hits = [{"lcsc": "C650700", "mpn": "AD7245AQ", "brand": "Analog Devices",
             "datasheet": wrong},
            {"lcsc": "C1", "mpn": "AD724JRZ", "brand": "HXY MOSFET",
             "datasheet": clone},
            {"lcsc": "C650728", "mpn": "AD724JRZ", "brand": "Analog Devices",
             "datasheet": right}]
    pages = {"https://www.analog.com/media/en/technical-documentation/"
             "data-sheets/AD724.pdf": TimeoutError("read timed out"),
             wrong: _sheet("AD7245"), clone: _sheet("AD724"),
             right: _sheet("AD724")}
    got = dsresolve.resolve("AD724", "Analog Devices", tmp_path / "p.pdf",
                            transport=_serve(pages), search=lambda q: hits)
    assert got["url"] == right, got
    assert got["via"] == ("LCSC's copy of the Analog Devices PDF, listed for "
                          "C650728 AD724JRZ (Analog Devices)")
    urls = [t["url"] for t in got["tried"]]
    assert wrong not in urls and clone not in urls
    assert got["tried"][0]["kind"] == "stalled"
    assert "analog.com" in dsresolve._DEAD["www.analog.com"][1]


def test_a_stalled_host_is_not_asked_again_this_run(tmp_path):
    url = "https://www.analog.com/media/en/technical-documentation/data-sheets/{}.pdf"
    asked = []

    def transport(u):
        asked.append(u)
        raise TimeoutError("read timed out")
    for mpn in ("AD724", "AD8221"):
        dsresolve.resolve(mpn, "Analog Devices", tmp_path / "p.pdf",
                          transport=transport, search=lambda q: [])
    assert asked == [url.format("AD724")] * 2     # one retry, then no more


def test_names_part_wants_the_part_as_a_word_of_its_own():
    assert dsresolve.names_part("AD724", ["AD724 RGB to NTSC Encoder"])
    assert dsresolve.names_part("AD724", ["AD724JR, AD724JRZ"])
    assert not dsresolve.names_part("AD724", ["AD7245 12-bit DAC"])
    assert not dsresolve.names_part("TPS62086", ["TPS620865 converter"])
    assert not dsresolve.names_part("LT1117", ["ALT1117"])
    assert dsresolve.names_part("SI7021-A20", ["Si7021-A20 I2C Humidity"])


@pytest.mark.parametrize("mpn,want,never", [
    ("TLV320AIC3104", ["TLV320AIC3104"], "TLV320AIC31"),
    ("FUSB302B01MPX", ["FUSB302B01MPX", "FUSB302B01", "FUSB302B"], "FUSB302B0"),
    ("XC6206P332MR", ["XC6206P332MR", "XC6206P332"], "XC6206P3"),
    ("XC6206P33xMR", ["XC6206P33"], "XC6206P33XMR"),
    ("SI7021-A20-GM1R", ["SI7021-A20-GM1R", "SI7021-A20"], "SI7021-A2"),
    ("ESP32-S3-WROOM-1(N16R8)", ["ESP32-S3-WROOM-1"], "ESP32-S3-WROOM-1(N16R8)"),
])
def test_candidates_cut_the_ordering_suffix_not_the_part(mpn, want, never):
    got = dsresolve.candidates(mpn)
    assert [c for c in got if c in want] == want, got
    assert never not in got


def test_every_maker_entry_is_complete_and_its_urls_fetchable():
    makers = dsresolve.load_makers()
    assert len({m["name"] for m in makers}) == len(makers)
    for m in makers:
        assert m["name"] and m["brands"] and m.get("cite"), m
        for p in m.get("prefixes") or []:
            re.compile(p)
        for tpl in m.get("urls") or []:
            assert set(re.findall(r"{(\w*)}", tpl)) <= {"lower", "upper"}, tpl
            ck = researchlib.check_url(tpl.format(lower="abc123", upper="ABC123"))
            assert ck["ok"], (m["name"], ck["reason"])
    # a brand matches one maker only
    for m in makers:
        for a in m["brands"]:
            who = [n["name"] for n in makers
                   if any(dsresolve._brand_has(a, b) for b in n["brands"])]
            assert who == [m["name"]], (a, who)


def test_a_board_listing_and_a_users_guide_are_not_the_part(tmp_path):
    """ti.com resets; LCSC lists TLV320AIC3104EVM, whose "datasheet" is the
    board's user's guide (seen on the training corpus): passed over. A
    user's guide at the maker's own URL is refused by its page 1."""
    evm = "https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/TLV320AIC3104EVM_C19542415.pdf"
    hits = [{"lcsc": "C19542415", "mpn": "TLV320AIC3104EVM",
             "brand": "Texas Instruments", "datasheet": evm}]
    guide = _pdf(["User's Guide SLAU218A TLV320AIC3104EVM and TLV320AIC3104 "
                  + "x" * 300, "y" * 300])
    pages = {TI.format("tlv320aic3104"): ConnectionResetError("reset"),
             evm: guide}
    got = dsresolve.resolve("TLV320AIC3104", "Texas Instruments",
                            tmp_path / "p.pdf", transport=_serve(pages),
                            search=lambda q: hits)
    # ti.com stalled twice: not judged, not "not found" (#1480)
    assert got["miss"] == "stalled" and evm not in [t["url"] for t in got["tried"]]
    assert any("a board" in t["why"] for t in got["tried"])

    dsresolve._DEAD.clear()             # ti.com answers again
    got = dsresolve.resolve("TLV320AIC3104", "Texas Instruments",
                            tmp_path / "p.pdf",
                            transport=_serve({TI.format("tlv320aic3104"): guide}),
                            search=lambda q: [])
    assert got["miss"] == "refused", got
    assert "user's guide" in got["tried"][0]["why"].lower()


def test_a_scan_is_unjudged_not_wrong():
    reasons = ["no text layer: 0 chars over 4 pages (a scan?)"]
    assert dsresolve._verdict({"status": "not_judged", "assessment": {
        "reasons": reasons}}) == ("unjudged", reasons[0])
    assert dsresolve._verdict({"status": "suspect", "assessment": {
        "reasons": ["x"]}})[0] == "wrong"


# --- #1480: one retry after a stall, then not judged --------------------------
ST = "https://www.st.com/resource/en/datasheet/{}.pdf"


def _flaky(pages: dict, stalls: dict):
    """_serve, but each URL in `stalls` times out that many times first."""
    serve, asked = _serve(pages), []

    def transport(url):
        asked.append(url)
        if stalls.get(url, 0) > 0:
            stalls[url] -= 1
            raise TimeoutError("read timed out")
        return serve(url)
    return transport, asked


def test_a_stall_is_retried_once_after_a_backoff(tmp_path):
    """Quiet twin: st.com times out once, then serves LPS331AP's sheet."""
    url = ST.format("lps331ap")
    transport, asked = _flaky({url: _sheet("LPS331AP")}, {url: 1})
    got = dsresolve.resolve("LPS331AP", "STMicroelectronics", tmp_path / "p.pdf",
                            transport=transport, search=lambda q: [])
    assert got["url"] == url, got
    assert asked == [url, url] and SLEPT == [dsresolve.STALL_BACKOFF]
    assert "www.st.com" not in dsresolve._DEAD
    # a later part on st.com is fetched, not skipped
    url2 = ST.format("lps35hw")
    got = dsresolve.resolve("LPS35HW", "STMicroelectronics", tmp_path / "q.pdf",
                            transport=_serve({url2: _sheet("LPS35HW")}),
                            search=lambda q: [])
    assert got["url"] == url2, got


def test_a_host_that_stalls_twice_leaves_the_part_not_judged(tmp_path):
    """Planted fault: st.com times out on the retry too. The part is
    "stalled" (not judged), not "not found", and so is the next one,
    which skips st.com without asking it again."""
    url = ST.format("lps331ap")
    transport, asked = _flaky({url: _sheet("LPS331AP")}, {url: 2})
    got = dsresolve.resolve("LPS331AP", "STMicroelectronics", tmp_path / "p.pdf",
                            transport=transport, search=lambda q: [])
    assert got["miss"] == "stalled" and "url" not in got, got
    assert asked == [url, url] and SLEPT == [dsresolve.STALL_BACKOFF]
    assert got["tried"][0]["why"].startswith("twice, 15 s apart")
    got = dsresolve.resolve("LPS35HW", "STMicroelectronics", tmp_path / "q.pdf",
                            transport=transport, search=lambda q: [])
    assert got["miss"] == "stalled", got
    assert asked == [url, url] and len(SLEPT) == 1
    assert got["tried"][0]["kind"] == "skipped"


def test_a_404_is_not_retried(tmp_path):
    """A missing PDF is no stall: one ask, no pause, "not found"."""
    transport, asked = _flaky({}, {})
    got = dsresolve.resolve("LPS331AP", "STMicroelectronics", tmp_path / "p.pdf",
                            transport=transport, search=lambda q: [])
    assert got["miss"] == "not found" and SLEPT == [], got
    assert len(asked) == len(set(asked))


# --- #1520: LCSC's mirror after the maker's host stalls twice ----------------
SHEET = "https://datasheet.lcsc.com/datasheet/pdf/{}.pdf?productCode={}"


def _page(sheets: dict, asked: list):
    """A stand-in for LCSC's live product page: `sheets` maps a product code
    to the HTML it serves."""
    def page(code):
        asked.append(code)
        return sheets.get(code, "<html>no sheet</html>")
    return page


def _product_html(code: str, sheet: str) -> str:
    """A product page as LCSC serves it: its certificate, a related
    product's sheet, and its own sheet."""
    return (f'<a href="https://datasheet.lcsc.com/certification/rohs/'
            f'030fa2248f0ae1184de9ec04fd5926b8.pdf">RoHS</a>'
            f'<a href="{SHEET.format("4d1dc0782382f0eb", "C45066865")}">x</a>'
            f'<script type="application/ld+json">{{"url": "{sheet}"}}</script>')


@pytest.mark.parametrize("mpn,listing,code", [
    ("LPS35HW", "LPS35HWTR", "C19632285"),          # ST's packing code TR
    ("STM32C051C8T6", "STM32C051C8T6", "C45870050"),  # the part itself
])
def test_after_a_maker_stall_lcscs_mirror_of_the_part_is_taken(
        tmp_path, mpn, listing, code):
    """Quiet twin: st.com stalls twice; LCSC lists the part (or the part on
    a reel) with no datasheet; its live product page names the sheet for
    its own code, which is fetched, checked and marked."""
    sheet = SHEET.format("9b42a1ad2bb8093d956a0f7fe9a716a0", code)
    url = ST.format(mpn.lower())
    transport, asked = _flaky({sheet: _sheet(mpn)}, {url: 2})
    hits = [{"lcsc": code, "mpn": listing, "brand": "STMicroelectronics",
             "datasheet": ""}]
    pages_asked: list = []
    got = dsresolve.resolve(
        mpn, "STMicroelectronics", tmp_path / "p.pdf", transport=transport,
        search=lambda q: hits,
        page=_page({code: _product_html(code, sheet)}, pages_asked))
    assert got["url"] == sheet, got
    assert got["via"].startswith(dsresolve.MIRROR_VIA)
    assert f"{code} {listing}" in got["via"]
    assert pages_asked == [code] and asked[:2] == [url, url]


def test_a_grade_variant_is_never_the_mirror(tmp_path):
    """Planted fault: analog.com stalls twice; LCSC lists AD724 only as
    AD724JRZ (J a grade, R a package: another part) under ADI's brand, and
    its page names a sheet. Not used: still stalled, its page not read."""
    sheet = SHEET.format("3ad362b6a92eca5246d457ae073feb28", "C650728")
    url = ("https://www.analog.com/media/en/technical-documentation/"
           "data-sheets/AD724.pdf")
    transport, asked = _flaky({sheet: _sheet("AD724")}, {url: 2})
    hits = [{"lcsc": "C650728", "mpn": "AD724JRZ", "brand": "Analog Devices",
             "datasheet": ""}]
    pages_asked: list = []
    got = dsresolve.resolve(
        "AD724", "Analog Devices", tmp_path / "p.pdf", transport=transport,
        search=lambda q: hits,
        page=_page({"C650728": _product_html("C650728", sheet)}, pages_asked))
    assert got["miss"] == "stalled" and "url" not in got, got
    assert pages_asked == [] and sheet not in asked
    assert any("AD724JRZ" in t["why"] and "(mirror)" in t["url"]
               for t in got["tried"])


def test_the_mirror_wants_its_own_codes_sheet_and_a_maker_stall(tmp_path):
    """A page naming only another product's sheet gives nothing; and a
    maker site that answers (404, no stall) never sends a part to the
    mirror at all."""
    code, url = "C19632285", ST.format("lps35hw")
    hits = [{"lcsc": code, "mpn": "LPS35HWTR", "brand": "STMicroelectronics",
             "datasheet": ""}]
    other = SHEET.format("4d1dc0782382f0eb", "C45066872")
    transport, asked = _flaky({other: _sheet("LPS35HW")}, {url: 2})
    got = dsresolve.resolve(
        "LPS35HW", "STMicroelectronics", tmp_path / "p.pdf",
        transport=transport, search=lambda q: hits,
        page=_page({code: _product_html(code, other).replace(other, "")
                    + f'<a href="{other}">'}, []))
    assert got["miss"] == "stalled" and other not in asked, got
    assert any(t["kind"] == "absent" and code in t["why"] for t in got["tried"])

    dsresolve._DEAD.clear()
    pages_asked: list = []
    got = dsresolve.resolve(
        "LPS35HW", "STMicroelectronics", tmp_path / "p.pdf",
        transport=_serve({}), search=lambda q: hits,
        page=_page({}, pages_asked))
    assert got["miss"] == "not found" and pages_asked == [], got
