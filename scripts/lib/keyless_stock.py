"""keyless_stock.py - distributor stock for an MPN, read from the pages
that answer without an API key (the --mpn gate of stock_check.py).

A BOM bought in the US is keyed by MPN, not by an LCSC code, and the
distributors' own APIs (DigiKey, Mouser) need keys a fresh workspace has
not got. Asked from here on 2026-11-15 with a browser User-Agent, most
keyless pages refuse (Newark, TTI, DigiKey, Octopart, Online Components:
HTTP 403; Arrow, Verical: no answer); Findchips' search page answered, with
each distributor's offers as data- attributes of its result rows. So that
is the one source here (SOURCES); a page that stops answering, or answers
with a page this parser does not know, is a SourceError, and the caller's
row is not_judged, never "in stock". So is a results page with no offers
at all: after a burst of some 70 searches Findchips served empty pages
for parts it had listed minutes before (BLM15PX221SN1D, GRM155R71C104KA88D),
and an empty page cannot tell "no such part" from "not now". Requests are
spaced PACE seconds apart.

offers(mpn, fetch=http_text) -> {source, url, offers[]} or SourceError
  offer: {distributor, authorized, mpn, mfr, stock, by_country{CC: n} or
  None, price_breaks [[qty, currency, unit_price]], sku}
  Every offer on the page, whatever MPN it names: the caller keeps the
  exact ones (same(): equal after norm()). No offers is a SourceError.
judge(mpn, need, fetch, country=None, brokers=False) -> verdict
  {status ok|short|not_found|not_judged, mpn, need, best{distributor,
  stock}, why, source, url, price}
  ok: one authorized distributor holds the whole need (or, with --country
  XX, holds it in that country's stock: an offer that does not split its
  stock by country counts 0 there). Stock is not summed across
  distributors or across one distributor's rows (Newark lists the same
  stock as reel and cut tape); a broker (no "Authorized Distributor" on
  its header) is only counted with brokers=True and otherwise named in
  the why.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import stocklib

FINDCHIPS = "https://www.findchips.com/search/{mpn}"
SOURCES = ("findchips",)
STATUSES = ("ok", "short", "not_found", "not_judged")
BLOCKING = ("short", "not_found", "not_judged")
PACE = 2.0  # seconds between two requests to one source
_pace_lock = threading.Lock()
_last = [0.0]


def _wait_turn(pace: float | None = None) -> None:
    with _pace_lock:
        gap = (PACE if pace is None else pace) - (time.monotonic()
                                                  - _last[0])
        if gap > 0:
            time.sleep(gap)
        _last[0] = time.monotonic()


def http_text(url: str) -> str:
    """The page at url as text, or stocklib.SourceError."""
    _wait_turn()
    req = urllib.request.Request(url, headers={
        "User-Agent": stocklib.UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=stocklib.TIMEOUT) as r:  # noqa: S310
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise stocklib.SourceError(
            f"HTTP {exc.code} from {url.split('?')[0]}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise stocklib.SourceError(f"{type(exc).__name__}: {exc}") from exc


def norm(mpn: str) -> str:
    return re.sub(r"\s+", "", str(mpn or "")).upper()


def same(a: str, b: str) -> bool:
    return bool(norm(a)) and norm(a) == norm(b)


def _int(v) -> int | None:
    digits = re.sub(r"[,\s]", "", str(v or ""))
    return int(digits) if digits.isdigit() else None


_ROW = re.compile(r"<tr\s[^>]*\bdata-mfrpartnumber=[^>]*>", re.S)
_ATTR = re.compile(r'data-([a-z_]+)="([^"]*)"')
_COUNTRY = re.compile(r"Stock\s+([A-Z]{2})\s*-\s*(?:<[^>]*>\s*)*([\d,]+)")


def parse_findchips(text: str) -> list[dict]:
    """The offers on a Findchips search page; SourceError when the page is
    not one (a block page, a changed layout)."""
    if ('id="anchors-number-authorized"' not in text
            or "Price and Stock Results" not in text):
        raise stocklib.SourceError("findchips: not a search results page")
    out = []
    for block in re.split(r'<div id="list\d+" class="distributor-results',
                          text)[1:]:
        head = re.search(r'class="other-disti-details">([^<]*)<', block)
        authorized = bool(head and "authorized distributor"
                          in html.unescape(head.group(1)).lower())
        for tr in _ROW.finditer(block):
            a = {k: html.unescape(v) for k, v in _ATTR.findall(tr.group(0))}
            split = {cc: int(n.replace(",", ""))
                     for cc, n in _COUNTRY.findall(a.get("stock", ""))}
            stock = _int(a.get("instock"))
            if stock is None:
                stock = (sum(split.values()) if split
                         else _int(re.sub(r"<[^>]*>", "",
                                          a.get("stock", ""))))
            try:
                breaks = [[int(q), c, float(p)]
                          for q, c, p in json.loads(a.get("price") or "[]")]
            except (ValueError, TypeError):
                breaks = []
            out.append({"distributor": a.get("distributor_name", ""),
                        "authorized": authorized,
                        "mpn": a.get("mfrpartnumber", ""),
                        "mfr": a.get("mfr", ""), "stock": stock or 0,
                        "by_country": split or None,
                        "price_breaks": sorted(breaks),
                        "sku": a.get("distino", "")})
    return out


def offers(mpn: str, fetch=http_text) -> dict:
    url = FINDCHIPS.format(mpn=urllib.parse.quote(norm(mpn), safe=""))
    got = parse_findchips(fetch(url))
    if not got:
        raise stocklib.SourceError(
            "findchips answered with no offers at all, as it does when it "
            "throttles")
    return {"source": "findchips", "url": url, "offers": got}


def price_at(breaks: list, qty: int):
    """[currency, unit price] at qty: the largest break at or below it (the
    smallest break when qty is under every one), or None."""
    if not breaks:
        return None
    fit = [b for b in breaks if b[0] <= qty] or breaks[:1]
    return [fit[-1][1], fit[-1][2]]


def _held(o: dict, country: str | None) -> int:
    if not country:
        return o["stock"]
    return (o["by_country"] or {}).get(country.upper(), 0)


def judge(mpn: str, need: int, fetch=http_text, country: str | None = None,
          brokers: bool = False) -> dict:
    v = {"mpn": mpn, "need": need, "country": country, "best": None,
         "price": None, "source": None, "url": None}
    try:
        page = offers(mpn, fetch)
    except stocklib.SourceError as exc:
        return {**v, "status": "not_judged",
                "why": f"no keyless page answered ({exc}): stock unknown"}
    v.update(source=page["source"], url=page["url"],
             offers=len(page["offers"]))
    mine = [o for o in page["offers"] if same(o["mpn"], mpn)]
    if not mine:
        near = sorted({o["mpn"] for o in page["offers"]})[:3]
        return {**v, "status": "not_found",
                "why": f"{page['source']} lists no offer for {norm(mpn)}"
                       + (f" (only {', '.join(near)})" if near else "")}
    per: dict[str, dict] = {}
    for o in mine:
        if o["authorized"] or brokers:
            cur = per.get(o["distributor"])
            if cur is None or _held(o, country) > _held(cur, country):
                per[o["distributor"]] = o
    where = f" in {country.upper()}" if country else ""
    if per:
        best = max(per.values(), key=lambda o: _held(o, country))
        held = _held(best, country)
        v["best"] = {"distributor": best["distributor"], "stock": held,
                     "authorized": best["authorized"]}
        v["price"] = price_at(best["price_breaks"], need)
        if held >= need:
            return {**v, "status": "ok",
                    "why": f"{best['distributor']} holds {held}{where} "
                           f"(need {need})"}
    broker = max((o for o in mine if not o["authorized"]),
                 key=lambda o: _held(o, country), default=None)
    note = (f"; broker {broker['distributor']} lists "
            f"{_held(broker, country)}" if broker and not brokers
            and _held(broker, country) else "")
    top = (f"at most {v['best']['stock']}{where} at one "
           f"{'distributor' if brokers else 'authorized distributor'} "
           f"({v['best']['distributor']})" if v["best"]
           else f"no {'' if brokers else 'authorized '}distributor lists "
                f"it{where}")
    return {**v, "status": "short",
            "why": f"need {need}, {top}{note}"}
