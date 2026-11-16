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
def _fresh_breaker():
    dsresolve._DEAD.clear()
    yield
    dsresolve._DEAD.clear()


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
    assert "analog.com" in dsresolve._DEAD.get("www.analog.com", "")


def test_a_stalled_host_is_not_asked_again_this_run(tmp_path):
    url = "https://www.analog.com/media/en/technical-documentation/data-sheets/{}.pdf"
    asked = []

    def transport(u):
        asked.append(u)
        raise TimeoutError("read timed out")
    for mpn in ("AD724", "AD8221"):
        dsresolve.resolve(mpn, "Analog Devices", tmp_path / "p.pdf",
                          transport=transport, search=lambda q: [])
    assert asked == [url.format("AD724")]


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
    assert got["miss"] == "not found" and evm not in [t["url"] for t in got["tried"]]
    assert any("a board" in t["why"] for t in got["tried"])

    dsresolve._DEAD.clear()             # ti.com answers again
    got = dsresolve.resolve("TLV320AIC3104", "Texas Instruments",
                            tmp_path / "p.pdf",
                            transport=_serve({TI.format("tlv320aic3104"): guide}),
                            search=lambda q: [])
    assert got["miss"] == "refused", got
    assert "user's guide" in got["tried"][0]["why"].lower()
