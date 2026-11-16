"""stocklib.py - can JLC place this BOM row today? Live stock and part match.

The person's flow is "I order it, it arrives, I plug it in": no surprise at
JLC's BOM review. Two surprises reached it on the hi-fi core board (10-28):
J1 (C5184243) showed 2230 in JLC's parts search but was oversold (JLC's own
`canPresaleNumber` -372; LCSC 0), and U8 (C84681, CH340C) was marked not for
sale ("no longer manufactured") with C7464026 as JLC's alternative - which
JLC's review swapped in, at 0 stock. Neither showed in `stock`, the one field
parts_search keeps. So this reads both sources' own records for each row and
says, per row, whether the part on the BOM is the part JLC will place, and
whether there is enough of it.

Sources (both anonymous; every call goes through an injectable `fetch`, so
tests never touch the network):
  jlc   POST jlcpcb.com .../smtGood/selectSmtComponentList {keyword: <C code>}
        - the record whose componentCode is the code: componentModelEn,
        componentBrandEn, componentLibraryType (base/expand), stockCount,
        canPresaleNumber (what can still be sold: stockCount minus what is
        already promised - negative when oversold), isBuyComponent ("0" = not
        for sale) with noBuyReason, componentAlternativesCode (what JLC's BOM
        review substitutes), describe.
  lcsc  GET wmsc.lcsc.com/ftps/wm/product/detail?productCode=<C code>
        - productModel, brandNameEn, stockNumber.

Verdicts per row (`status`):
  ok           the code is JLC's, for sale, the model/value matches, and JLC
               can sell at least the quantity the build needs
  short        JLC can sell fewer than the build needs (available < need)
  not_for_sale JLC will not sell this code (isBuyComponent 0) - with the
               substitute its review would place, looked up the same way
  not_found    JLC has no record of the code: it cannot place it
  mismatch     the code's model is not the BOM's MPN, or (no MPN named) its
               description does not carry the BOM's value, or its package is
               a chip of another size than the row's footprint (an 0402 part
               on an 0603 pad: oasis v0.2 Rfbb1, docs/errata-corpus.md)
  unknown      a source could not be asked (network, HTTP status, a page that
               is not JSON) - never a pass
Every status but `ok` blocks the order. `notes` carries what does not block
but a reader should know: LCSC's own stock disagreeing with JLC's, another
code with the same model (the review may match by MPN), the minimum purchase.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import math
import re
import urllib.error
import urllib.request

JLC_SEARCH = ("https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/"
              "smtGood/selectSmtComponentList")
LCSC_DETAIL = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={code}"
TIMEOUT = 20.0
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
      "Gecko) Chrome/124.0 Safari/537.36")
BLOCKING = ("short", "not_for_sale", "not_found", "mismatch", "unknown")


class SourceError(Exception):
    """A source could not be asked: the row is unknown, not failed."""


def offline() -> bool:
    """HWDE_STOCK_OFFLINE=1: never ask (tests, check_all's scratch runs)."""
    return os.environ.get("HWDE_STOCK_OFFLINE", "") not in ("", "0")


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def http_fetch(method: str, url: str, body: dict | None = None) -> dict:
    """JSON from url, or SourceError. urllib honours the proxy env vars."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers.update({"Content-Type": "application/json",
                        "Origin": "https://jlcpcb.com",
                        "Referer": "https://jlcpcb.com/parts"})
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310
            raw = r.read()
    except urllib.error.HTTPError as exc:
        raise SourceError(f"HTTP {exc.code} from {url.split('?')[0]}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SourceError(f"{type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise SourceError(f"not JSON from {url.split('?')[0]} "
                          f"({raw[:80]!r})") from exc


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def jlc_record(code: str, fetch=http_fetch) -> dict | None:
    """JLC's record for an exact C code, or None when it has none."""
    body = fetch("POST", JLC_SEARCH,
                 {"keyword": code, "currentPage": 1, "pageSize": 10})
    try:
        rows = body["data"]["componentPageInfo"]["list"] or []
    except (KeyError, TypeError) as exc:
        if isinstance(body, dict) and body.get("code") not in (200, None):
            raise SourceError(f"JLC search answered code {body.get('code')}: "
                              f"{body.get('message')}") from exc
        rows = []
    for r in rows:
        if str(r.get("componentCode") or "").upper() == code.upper():
            return {
                "code": r.get("componentCode"),
                "model": r.get("componentModelEn") or "",
                "brand": r.get("componentBrandEn") or "",
                "library": {"base": "basic", "expand": "extended"}.get(
                    r.get("componentLibraryType"),
                    r.get("componentLibraryType")),
                "stock": _int(r.get("stockCount")),
                "can_sell": _int(r.get("canPresaleNumber")),
                "for_sale": str(r.get("isBuyComponent", "1")) != "0",
                "no_buy_reason": r.get("noBuyReason"),
                "alternative": r.get("componentAlternativesCode") or None,
                "min_purchase": _int(r.get("minPurchaseNum")),
                "description": r.get("describe") or "",
                "package": r.get("componentSpecificationEn") or "",
            }
    return None


def lcsc_record(code: str, fetch=http_fetch) -> dict | None:
    body = fetch("GET", LCSC_DETAIL.format(code=code))
    if not isinstance(body, dict):
        raise SourceError("LCSC detail: not an object")
    res = body.get("result")
    if body.get("code") != 200 or not isinstance(res, dict):
        if body.get("code") in (404,) or res is None:
            return None
        raise SourceError(f"LCSC detail answered code {body.get('code')}")
    return {"code": res.get("productCode"),
            "model": res.get("productModel") or "",
            "brand": res.get("brandNameEn") or "",
            "stock": _int(res.get("stockNumber"))}


def jlc_same_model(model: str, fetch=http_fetch) -> list[str]:
    """Every JLC code whose model is exactly `model` (for the ambiguity note)."""
    if not model:
        return []
    body = fetch("POST", JLC_SEARCH,
                 {"keyword": model, "currentPage": 1, "pageSize": 25})
    try:
        rows = body["data"]["componentPageInfo"]["list"] or []
    except (KeyError, TypeError):
        return []
    return sorted({str(r.get("componentCode")) for r in rows
                   if norm(r.get("componentModelEn")) == norm(model)})


def norm(s) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(s or "").upper())


# SI prefixes, case-sensitive: m is milli and M is mega (a 1M resistor is
# not a 1m\u03a9 shunt). K, U, N, P and MEG are how BOMs spell k, u, n, p, M.
_PREFIX = {"": 1.0, "p": 1e-12, "P": 1e-12, "n": 1e-9, "N": 1e-9,
           "u": 1e-6, "U": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3,
           "M": 1e6, "MEG": 1e6, "Meg": 1e6, "meg": 1e6, "G": 1e9}
_UNIT = {"f": "F", "h": "H", "r": "\u03a9", "ohm": "\u03a9", "ohms": "\u03a9",
         "\u03c9": "\u03a9", "v": "V", "a": "A", "w": "W", "hz": "Hz"}
_VALUE = re.compile(r"^(\d+(?:\.\d+)?)(MEG|Meg|meg|[pPnNuUmkKMG]?)"
                    r"(F|f|H|h|R|r|[Oo][Hh][Mm][Ss]?|\u03a9|V|v|A|W|w|"
                    r"[Hh][Zz])?$")
_DESC_Q = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s?([pnumkKMG]?)"
                     r"(F|H|\u03a9|[Oo]hms?|OHMS?|V|A|W|Hz)?(?![A-Za-z])")


def _micro(s: str) -> str:
    return s.replace("\u00b5", "u").replace("\u03bc", "u")


def value_quantity(value: str) -> tuple[float, str] | None:
    """A passive's value as (number in base units, unit): '100nF' is
    (1e-07, 'F'), '0.5R' and '500m\u03a9' are (0.5, '\u03a9'), '4K7' is
    (4700.0, '') - no unit given. The prefix keeps its case: m is milli, M is
    mega. None when the value is not a plain quantity ('CH340C').
    '470uF/50V' reads its first quantity; '4R7', '4K7', '4M7' and '4m7' read
    as 4.7 with R, k, M and m; '100 nF' reads as '100nF'."""
    v = re.sub(r"^(\d+(?:\.\d+)?)\s+(?=[^\d\s/,])", r"\1",
               _micro(str(value or "").strip()))
    v = re.split(r"[/\s,]", v)[0]
    m = re.match(r"^(\d+)([RrKkMmG])(\d+)$", v)
    if m:
        v = f"{m.group(1)}.{m.group(3)}{m.group(2)}"
    m = _VALUE.match(v)
    if not m:
        return None
    num, pre, unit = m.group(1), m.group(2) or "", m.group(3) or ""
    return float(num) * _PREFIX[pre], _UNIT.get(unit.lower(), unit)


def desc_quantities(text: str) -> list[tuple[float, str]]:
    """Every quantity with a unit in JLC's description, in base units:
    '4.7k\u03a9 \u00b11% 62.5mW' is [(4700.0, '\u03a9'), (0.0625, 'W')]. A
    prefixed number with no unit ('RES 4.7K 1%') comes back with unit ''."""
    out = []
    for m in _DESC_Q.finditer(_micro(str(text or ""))):
        num, pre, unit = m.groups()
        if not pre and not unit:        # '0402', '1%': not a value
            continue
        unit = unit or ""
        out.append((float(num) * _PREFIX[pre], _UNIT.get(unit.lower(), unit)))
    return out


def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=0.0)


def matches(bom_mpn: str, bom_value: str, rec: dict) -> tuple[bool | None, str]:
    """(match, why). None = nothing to compare against (unknown)."""
    model = rec.get("model") or ""
    if bom_mpn and norm(bom_mpn) != norm(bom_value) and \
            value_quantity(bom_mpn) is None:
        ok = norm(bom_mpn) == norm(model) or (
            len(norm(bom_mpn)) >= 5 and (norm(bom_mpn) in norm(model)
                                         or norm(model) in norm(bom_mpn)))
        return ok, (f"BOM MPN {bom_mpn} vs JLC model {model}")
    if norm(bom_value) and norm(bom_value) == norm(model):
        # The value is the MPN, even one that reads as a bare number
        # (Wurth's '750315240'): not a quantity.
        return True, f"BOM value {bom_value} is JLC model {model}"
    q = value_quantity(bom_value or bom_mpn)
    if q is not None:
        # Compared as numbers in base units, so 0.5R is 500m\u03a9 and 100nF
        # is 0.1uF; a value with no unit ('4K7', '100n') takes any unit,
        # and so does a description's '4.7K' with none.
        ok = any(_same(q[0], n) and (not q[1] or not u or q[1] == u)
                 for n, u in desc_quantities(rec.get("description")))
        return ok, (f"value {bom_value} {'in' if ok else 'not in'} JLC's "
                    f"description '{rec.get('description')}' ({model})")
    # A part named by its family in the value ('LP5907-3.3', 'EC11'): every
    # word of it in JLC's model ('LP5907MFX-3.3/NOPB', 'EC11E1834403').
    words = [w for w in re.split(r"[^0-9A-Za-z.]+", str(bom_value or ""))
             if len(w) >= 3 and re.search(r"\d", w)]
    if words and all(w.upper() in str(model).upper() for w in words):
        return True, f"BOM value {bom_value} in JLC model {model}"
    # A value that is the MPN itself, in words too short for the rule above
    # ('TYPE-C-31-M-12'): the whole of it against JLC's model, as an MPN.
    whole, nmodel = norm(bom_value or bom_mpn), norm(model)
    if whole and nmodel and (whole == nmodel or (
            min(len(whole), len(nmodel)) >= 5
            and (whole in nmodel or nmodel in whole))):
        return True, f"BOM value {bom_value or bom_mpn} is JLC model {model}"
    return None, (f"nothing on the BOM row to compare with JLC's model "
                  f"{model} (no MPN, value '{bom_value}' is not a plain "
                  "quantity)")


def package_clash(footprint: str, jlc: dict) -> str | None:
    """Why the part's chip size isn't the footprint's, or None (same size,
    or either side names no chip size: engcalc.package_code)."""
    from engcalc import package_code
    pad = package_code(footprint)
    size = package_code(jlc.get("package"))
    if pad is None or size is None or pad == size:
        return None
    return (f"JLC's {jlc.get('code')} ({jlc.get('model')}) is a {size} part "
            f"but the row's footprint {footprint} is {pad}: it won't fit "
            "the pads")


def check_row(code: str, need: int, mpn: str = "", value: str = "",
              fetch=http_fetch, ambiguity: bool = True,
              footprint: str = "") -> dict:
    """One BOM row's verdict (see the module docstring)."""
    out = {"lcsc": code, "need": need, "mpn": mpn, "value": value,
           "checked_at": now(), "notes": []}
    if not code:
        out.update(status="unknown",
                   why="no LCSC number on the row: JLC matches it by "
                       "comment and footprint, and nothing here can check "
                       "what it picks")
        return out
    try:
        jlc = jlc_record(code, fetch)
    except SourceError as exc:
        out.update(status="unknown", why=f"JLC parts search: {exc}")
        return out
    out["jlc"] = jlc
    try:
        out["lcsc_record"] = lcsc_record(code, fetch)
    except SourceError as exc:
        out["lcsc_record"] = None
        out["notes"].append(f"LCSC detail not read: {exc}")
    if jlc is None:
        out.update(status="not_found",
                   why=f"JLC's parts library has no {code}: it cannot "
                       "place it")
        return out
    match, mwhy = matches(mpn, value, jlc)
    out["match"] = {"ok": match, "why": mwhy}
    avail = jlc["can_sell"] if jlc["can_sell"] is not None else jlc["stock"]
    out["available"] = avail
    lc = out.get("lcsc_record") or {}
    if lc.get("stock") is not None and avail is not None and \
            lc["stock"] < need <= avail:
        out["notes"].append(f"LCSC shows {lc['stock']} in stock; JLC's own "
                            f"stock covers the build ({avail})")
    if jlc["stock"] is not None and avail is not None and \
            avail < need <= jlc["stock"]:
        out["notes"].append(f"JLC lists {jlc['stock']} in stock but can "
                            f"sell {avail} (the rest is already promised)")
    if jlc.get("min_purchase") and jlc["min_purchase"] > need:
        out["notes"].append(f"JLC's minimum purchase is "
                            f"{jlc['min_purchase']} (the build needs {need})")
    if ambiguity and jlc["model"] and jlc["library"] != "basic":
        try:
            same = [c for c in jlc_same_model(jlc["model"], fetch)
                    if c.upper() != code.upper()]
        except SourceError:
            same = []
        if same:
            out["notes"].append(
                f"JLC also lists {', '.join(same)} as {jlc['model']}: its "
                f"BOM review may match by MPN, so check it shows {code}")
    if not jlc["for_sale"]:
        alt = jlc.get("alternative")
        why = (f"JLC does not sell {code} "
               f"({jlc.get('no_buy_reason') or 'not for sale'})")
        if alt:
            try:
                arec = jlc_record(alt, fetch)
            except SourceError as exc:
                arec, why = None, why + f"; its substitute {alt}: {exc}"
            if arec is not None:
                aav = (arec["can_sell"] if arec["can_sell"] is not None
                       else arec["stock"])
                out["substitute"] = {**arec, "available": aav}
                why += (f"; its BOM review substitutes {alt} "
                        f"({arec['model']}, {aav} available"
                        f"{'' if (aav or 0) >= need else ' - short'})")
        out.update(status="not_for_sale", why=why)
        return out
    if match is False:
        out.update(status="mismatch", why=mwhy)
        return out
    clash = package_clash(footprint, jlc)
    if clash:
        out.update(status="mismatch", why=clash)
        return out
    if avail is None:
        out.update(status="unknown", why="JLC's record carries no stock")
        return out
    if avail < need:
        out.update(status="short",
                   why=f"JLC can sell {avail} of {code} ({jlc['model']}); "
                       f"the build needs {need}")
        return out
    if match is None:
        out.update(status="unknown", why=mwhy)
        return out
    out.update(status="ok", why=f"{avail} available at JLC, need {need}; "
                                f"{mwhy}")
    return out
