"""lib/keyless_stock.py and stock_check.py --mpn (#1157): an MPN-keyed BOM's
stock, read from a keyless distributor page, never "in stock" unasked."""
import csv
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import keyless_stock  # noqa: E402
import stock_check  # noqa: E402
import stocklib  # noqa: E402

PAGE = (ROOT / "tests" / "fixtures" / "keyless_stock"
        / "findchips.html").read_text(encoding="utf-8")
MPN = "BLM15PX221SN1D"


def page(text=PAGE):
    asked = []

    def fetch(url):
        asked.append(url)
        return text
    fetch.asked = asked
    return fetch


def down(url):
    raise stocklib.SourceError("HTTP 403 from " + url)


def only(*names):
    """The fixture page with just these distributors' blocks."""
    head, *blocks = re.split(r'(?=<div id="list\d+")', PAGE)
    keep = [b for b in blocks if any(f'data-distributor_name="{n}"' in
                                     b.split(">", 1)[0] for n in names)]
    return head + "".join(keep) + "</body></html>"


def test_parse_reads_each_offer_its_flag_and_country_split():
    offers = keyless_stock.parse_findchips(PAGE)
    assert [o["distributor"] for o in offers] == [
        "Alpha Parts", "Beta Electronics", "Beta Electronics",
        "Beta Electronics", "Gamma Brokers", "Delta Europe"]
    by = {o["sku"]: o for o in offers}
    assert by["A-1"]["authorized"] and by["B-1"]["authorized"]
    assert not by["G-1"]["authorized"]
    assert by["A-1"]["stock"] == 70000 and by["A-1"]["by_country"] is None
    assert by["D-1"]["by_country"] == {"DE": 90000, "US": 1200}
    assert by["D-1"]["stock"] == 91200
    assert by["A-1"]["price_breaks"] == [[1000, "USD", 0.04],
                                         [10000, "USD", 0.0327]]


def test_one_authorized_distributor_covering_the_need_is_ok():
    f = page()
    v = keyless_stock.judge(MPN, 90000, fetch=f)
    assert v["status"] == "ok" and v["best"]["distributor"] == "Delta Europe"
    assert f.asked == ["https://www.findchips.com/search/BLM15PX221SN1D"]


def test_planted_short_need_above_every_authorized_stock():
    # quiet twin: 90000 above is ok; 95000 is more than any one holds
    v = keyless_stock.judge(MPN, 95000, fetch=page())
    assert v["status"] == "short"
    assert "91200" in v["why"] and "Gamma Brokers lists 700000" in v["why"]


def test_broker_stock_counts_only_when_asked():
    assert keyless_stock.judge(MPN, 95000, fetch=page(),
                               brokers=True)["status"] == "ok"
    v = keyless_stock.judge(MPN, 100, fetch=page(only("Gamma Brokers")))
    assert v["status"] == "short"
    assert "no authorized distributor lists it" in v["why"]


def test_one_distributors_rows_are_not_summed():
    # Beta lists the same 25223 as cut tape and reel: 30000 is short
    f = page(only("Beta Electronics"))
    assert keyless_stock.judge(MPN, 25000, fetch=f)["status"] == "ok"
    assert keyless_stock.judge(MPN, 30000, fetch=f)["status"] == "short"


def test_a_near_miss_mpn_is_not_counted():
    # Beta's 900000 are BLM15PX221SN1, not ...SN1D
    f = page(only("Beta Electronics"))
    assert keyless_stock.judge(MPN, 100000, fetch=f)["status"] == "short"
    v = keyless_stock.judge("BLM15PX221SN", 1, fetch=page())
    assert v["status"] == "not_found" and "BLM15PX221SN1" in v["why"]


def test_a_results_page_with_no_offers_is_not_judged_not_not_found():
    # Findchips serves this empty page when it throttles, for parts it
    # lists a minute later: "no such part" is not what it says
    v = keyless_stock.judge(MPN, 1, fetch=page(only()))
    assert v["status"] == "not_judged" and "no offers" in v["why"]


def test_country_counts_only_split_stock_held_there():
    assert keyless_stock.judge(MPN, 1000, fetch=page(),
                               country="US")["status"] == "ok"
    v = keyless_stock.judge(MPN, 2000, fetch=page(), country="us")
    # Alpha's 70000 is not split by country, so it counts 0 in the US
    assert v["status"] == "short" and "in US" in v["why"]


def test_no_page_answering_is_not_judged_never_in_stock():
    v = keyless_stock.judge(MPN, 1, fetch=down)
    assert v["status"] == "not_judged" and "HTTP 403" in v["why"]
    # a page that is not a results page (a block page) is no answer either
    v = keyless_stock.judge(MPN, 1, fetch=page("<html>Access denied</html>"))
    assert v["status"] == "not_judged"


def test_price_at_the_order_quantity():
    assert keyless_stock.judge(MPN, 5000, fetch=page(only(
        "Alpha Parts")))["price"] == ["USD", 0.04]
    assert keyless_stock.price_at([[1000, "USD", 0.04]], 10) == ["USD", 0.04]
    assert keyless_stock.price_at([], 10) is None


def _mpn_bom(path, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Line", "Qty", "Designators", "Value", "Manufacturer",
                    "MPN", "Buy"])
        w.writerows(rows)


def test_cli_mpn_mode_judges_bought_rows_and_skips_board_features(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HWDE_STOCK_OFFLINE", "0")
    monkeypatch.setattr(keyless_stock, "http_text", page())
    bom = tmp_path / "BOM-US.csv"
    _mpn_bom(bom, [["1", "2", "FB1 FB2", "220R", "Murata", MPN, "yes"],
                   ["2", "1", "H1", "hole", "", "", "no (board feature)"]])
    out = tmp_path / "rep.json"
    assert stock_check.main(["--mpn", "--bom", str(bom), "--boards", "5",
                             "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert rep["counts"] == {"ok": 1} and rep["rows"][0]["need"] == 10
    assert rep["rows"][0]["refs"] == ["FB1", "FB2"]
    # the planted short twin: 50000 boards need 100000
    assert stock_check.main(["--mpn", "--bom", str(bom), "--boards",
                             "50000"]) == 1
    assert "BLOCKS FB1,FB2 BLM15PX221SN1D [short]" in capsys.readouterr().err


def test_cli_mpn_mode_with_every_page_down_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("HWDE_STOCK_OFFLINE", "0")
    monkeypatch.setattr(keyless_stock, "http_text", down)
    bom = tmp_path / "BOM-US.csv"
    _mpn_bom(bom, [["1", "1", "FB1", "220R", "Murata", MPN, "yes"],
                   ["2", "1", "U1", "?", "", "", "yes"]])
    out = tmp_path / "rep.json"
    assert stock_check.main(["--mpn", "--bom", str(bom), "--out",
                             str(out)]) == 1
    assert json.loads(out.read_text())["counts"] == {"not_judged": 2}


def test_cli_mpn_mode_needs_an_mpn_column_and_stays_live(tmp_path,
                                                         monkeypatch):
    bom = tmp_path / "BOM.csv"
    bom.write_text("Comment,Designator\n100nF,C1\n", encoding="utf-8")
    assert stock_check.main(["--mpn", "--bom", str(bom)]) == 2  # offline
    monkeypatch.setenv("HWDE_STOCK_OFFLINE", "0")
    out = tmp_path / "rep.json"
    assert stock_check.main(["--mpn", "--bom", str(bom), "--out",
                             str(out)]) == 2
    assert "no MPN column" in json.loads(out.read_text())["error"]
