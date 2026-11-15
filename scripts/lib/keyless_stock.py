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
and an empty page cannot tell "no such part" from "not now".

A polite client (#1241): requests to a source are spaced PACE seconds apart
(across threads); a throttle (HTTP 429 or 503, or that empty page) is waited
out - Retry-After when the source sends one, else BACKOFF doubling - and
asked again up to TRIES times, holding every thread meanwhile; a part's
answer is kept for CACHE_TTL (a day) under cache_dir() so the same MPN is
not asked twice in a day; the User-Agent is stocklib.UA, a plain browser
string with no name, address or account in it. A row still throttled after
the last try is not_judged with reason "throttled", not "no answer".

offers(mpn, fetch=http_text, cache=None) -> {source, url, offers[],
  cached_at} or SourceError (Throttled when the source throttles)
  offer: {distributor, authorized, mpn, mfr, stock, by_country{CC: n} or
  None, price_breaks [[qty, currency, unit_price]], sku}
  Every offer on the page, whatever MPN it names: the caller keeps the
  exact ones (same(): equal after norm()). No offers is a SourceError.
judge(mpn, need, fetch, country=None, brokers=False, cache=None) -> verdict
  {status ok|short|not_found|not_judged, mpn, need, best{distributor,
  stock}, why, source, url, price, cached_at}; a not_judged one also has
  reason "throttled" or "no_answer".
cache: a directory (stock_check passes cache_dir() when it asks live), or
  None for none (tests' fake fetchers).
  ok: one authorized distributor holds the whole need (or, with --country
  XX, holds it in that country's stock: an offer that does not split its
  stock by country counts 0 there). Stock is not summed across
  distributors or across one distributor's rows (Newark lists the same
  stock as reel and cut tape); a broker (no "Authorized Distributor" on
  its header) is only counted with brokers=True and otherwise named in
  the why.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import stocklib

FINDCHIPS = "https://www.findchips.com/search/{mpn}"
SOURCES = ("findchips",)
STATUSES = ("ok", "short", "not_found", "not_judged")
BLOCKING = ("short", "not_found", "not_judged")
PACE = 2.0  # seconds between two requests to one source (#1241: >= 1)
TRIES = 3  # asks of one page while the source throttles
BACKOFF = 15.0  # first wait after a throttle with no Retry-After; doubles
MAX_WAIT = 120.0  # cap on one wait, Retry-After included
CACHE_TTL = 24 * 3600  # a part's answer is kept a day
THROTTLE_CODES = (429, 503)
_pace_lock = threading.Lock()
_last = [0.0]
_sleep = time.sleep  # tests replace it


class Throttled(stocklib.SourceError):
    """The source said "not now" (HTTP 429/503, or its empty results page);
    retry_after is its Retry-After in seconds, when it sent one."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


def _wait_turn(pace: float | None = None) -> None:
    with _pace_lock:
        gap = (PACE if pace is None else pace) - (time.monotonic()
                                                  - _last[0])
        if gap > 0:
            time.sleep(gap)
        _last[0] = time.monotonic()


def _hold_all(seconds: float) -> None:
    """After a throttle: no thread asks again for `seconds` (the pacer's
    next turn is pushed past it)."""
    with _pace_lock:
        _last[0] = max(_last[0], time.monotonic() + seconds - PACE)


def _retry_after(value) -> float | None:
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        return None  # an HTTP-date: fall back to BACKOFF


def http_text(url: str) -> str:
    """The page at url as text, or stocklib.SourceError (Throttled on HTTP
    429/503)."""
    _wait_turn()
    req = urllib.request.Request(url, headers={
        "User-Agent": stocklib.UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=stocklib.TIMEOUT) as r:  # noqa: S310
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        msg = f"HTTP {exc.code} from {url.split('?')[0]}"
        if exc.code in THROTTLE_CODES:
            raise Throttled(msg, _retry_after(
                (exc.headers or {}).get("Retry-After"))) from exc
        raise stocklib.SourceError(msg) from exc
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


def cache_dir() -> Path | None:
    """Where answers are kept: HWDE_STOCK_CACHE, else ~/.cache/hwde/
    keyless_stock; None when HWDE_STOCK_CACHE is 0 or off."""
    val = os.environ.get("HWDE_STOCK_CACHE", "")
    if val.lower() in ("0", "off", "no"):
        return None
    return (Path(val).expanduser() if val
            else Path.home() / ".cache" / "hwde" / "keyless_stock")


def _cache_file(cache: Path, url: str) -> Path:
    return Path(cache) / (hashlib.sha256(url.encode()).hexdigest()[:24]
                          + ".json")


def _cached(cache: Path | None, url: str) -> dict | None:
    if cache is None:
        return None
    try:
        rec = json.loads(_cache_file(cache, url).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (rec.get("url") != url or not rec.get("offers")
            or time.time() - float(rec.get("t", 0)) >= CACHE_TTL):
        return None
    return rec


def _keep(cache: Path | None, url: str, got: list) -> str:
    at = stocklib.now()
    if cache is not None:
        try:
            Path(cache).mkdir(parents=True, exist_ok=True)
            f = _cache_file(cache, url)
            tmp = f.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"url": url, "t": time.time(),
                                       "at": at, "offers": got}),
                           encoding="utf-8")
            tmp.replace(f)
        except OSError:
            pass  # an unwritable cache only costs a later re-ask
    return at


def offers(mpn: str, fetch=http_text, cache: Path | None = None) -> dict:
    url = FINDCHIPS.format(mpn=urllib.parse.quote(norm(mpn), safe=""))
    rec = _cached(cache, url)
    if rec is not None:
        return {"source": "findchips", "url": url, "offers": rec["offers"],
                "cached_at": rec.get("at")}
    last: Throttled | None = None
    for i in range(TRIES):
        if last is not None:
            wait = min(MAX_WAIT, last.retry_after if last.retry_after
                       is not None else BACKOFF * 2 ** (i - 1))
            _hold_all(wait)
            _sleep(wait)
        try:
            got = parse_findchips(fetch(url))
        except Throttled as exc:
            last = exc
            continue
        if got:
            return {"source": "findchips", "url": url, "offers": got,
                    "cached_at": _keep(cache, url, got)}
        last = Throttled("findchips answered with no offers at all, as it "
                         "does when it throttles")
    raise Throttled(f"{last} (asked {TRIES} times, backing off)",
                    last.retry_after)


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
          brokers: bool = False, cache: Path | None = None) -> dict:
    v = {"mpn": mpn, "need": need, "country": country, "best": None,
         "price": None, "source": None, "url": None}
    try:
        page = offers(mpn, fetch, cache)
    except Throttled as exc:
        return {**v, "status": "not_judged", "reason": "throttled",
                "why": f"throttled by the keyless page ({exc}): stock "
                       "unknown"}
    except stocklib.SourceError as exc:
        return {**v, "status": "not_judged", "reason": "no_answer",
                "why": f"no keyless page answered ({exc}): stock unknown"}
    v.update(source=page["source"], url=page["url"],
             offers=len(page["offers"]), cached_at=page.get("cached_at"))
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
                           f"(need {need}, {page['source']})"}
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
    # one source only: a distributor Findchips does not list (Verical and
    # Arrow were missing for GRM32ER71H106KA12L) is not seen, so say where
    return {**v, "status": "short",
            "why": f"need {need}, {top} on {page['source']}{note}"}
