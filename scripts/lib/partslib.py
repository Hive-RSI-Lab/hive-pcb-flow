"""partslib.py - shared helpers for the S6 parts tooling (parts_search.py).

Source ladder for JLCPCB/LCSC part data (SPEC.md P3, section 6.1):

  1. live  - the ANONYMOUS JLCPCB parts search wrapped by the pinned
             easyeda2kicad 1.0.1 (EasyedaApi.search_jlcpcb_components). No
             credential needed - the spec's "credentialed Parts API" (verify-
             later V5) turned out to be unnecessary for search (LEARNINGS
             2026-07-22 [parts]).
  2. db    - a cached jlcparts SQLite database (--db); offline fallback. The
             jlcparts (yaqwsx) schema is targeted but the reader is column-
             tolerant. No such DB ships on this host; the code path is unit-
             tested against a synthetic one.
  3. web   - web search is the agent-level last resort (a non-interactive
             script cannot browse); parts_search signals it via exit 2 +
             remediation when live is unreachable and no --db is given.

Output is kept ASCII-safe with json.dumps(ensure_ascii=True) - JLC descriptions
carry non-ASCII (mu, +/-, ohm) that crash a cp1252 console (LEARNINGS [windows]).
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

# pin type / classification is JLC's; we normalize "base"/"expand" -> Basic/Extended.


class PartsError(RuntimeError):
    """Lookup cannot proceed (network down + no cache, unusable db). CLI exit 2."""


# ------------------------------------------------------------- live search

def _missing_dep(module: str, exc: BaseException) -> PartsError:
    return PartsError(
        f"cannot search: python module '{module}' is not importable under "
        f"{sys.executable} ({type(exc).__name__}: {exc}). This is a missing "
        f"dependency, not a network outage. Fix: {_venv_hint()}")


def _venv_hint() -> str:
    """Where the deps live: the Hive module's runner (bin/hwde) when the scripts
    sit inside the module, else the standalone hwde venv."""
    runner = Path(__file__).resolve().parents[2] / "bin" / "hwde"
    if not runner.is_file():
        return ("run with the hwde venv (.venv/bin/python; on Linux hosts "
                "~/.local/hwde-venv/bin/python).")
    py = os.environ.get("HWDE_PY", "")
    if py and Path(py) == Path(sys.executable):
        return (f"the module's venv ({Path(py).parent.parent}) is missing it; "
                f"remove that folder and run {runner} again: it rebuilds the "
                "venv from requirements.lock.")
    script = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
    script = script if script and script != "-c" else "parts_search"
    return (f"run it through the module's runner, `{runner} {script} ...`, "
            "which builds the venv from requirements.lock; or with the "
            f"$HWDE_PY that `eval \"$({runner} env)\"` sets.")


def require_httpx():
    """Import httpx or raise a PartsError naming the cause (call before any network)."""
    try:
        import httpx
    except ImportError as exc:
        raise _missing_dep("httpx", exc) from exc
    return httpx


def _import_easyeda_api():
    """Import EasyedaApi with its import-time stdout/stderr muffled."""
    logging.disable(logging.CRITICAL)
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
    except ImportError as exc:
        raise _missing_dep("easyeda2kicad", exc) from exc
    return EasyedaApi


def live_search(keyword: str, page: int = 1, page_size: int = 25,
                part_type: str | None = None) -> tuple[list[dict], int]:
    """Raw JLCPCB search -> (results, total).

    EasyedaApi swallows network errors and returns an empty result set, so an
    empty return here is ambiguous (no matches vs. offline); callers resolve it
    with endpoint_reachable().
    """
    EasyedaApi = _import_easyeda_api()
    api = EasyedaApi()
    raw = api.search_jlcpcb_components(
        keyword, page=page, page_size=page_size, part_type=part_type)
    return list(raw.get("results", [])), int(raw.get("total", 0) or 0)


# The endpoint easyeda2kicad wraps. Its keyword match is on part numbers and
# a few description tokens, so a kind ('RCA jack', 'Toslink') returns zero
# rows; the same endpoint browses a JLC category (secondSortName), filters
# attributes server-side (componentAttributeList: [{name: [exact values]}])
# and drops out-of-stock rows (stockFlag) - none of which the wrapper passes.
JLC_SEARCH_URL = ("https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/"
                  "smtGood/selectSmtComponentList")


def _jlc_item(item: dict) -> dict:
    """A raw JLC list row -> the result shape search_jlcpcb_components gives."""
    prices = item.get("componentPrices") or []
    return {
        "lcsc": item.get("componentCode", ""),
        "model": item.get("componentModelEn", ""),
        "brand": item.get("componentBrandEn", ""),
        "package": item.get("componentSpecificationEn", ""),
        "category": item.get("componentTypeEn", ""),
        "stock": item.get("stockCount", 0),
        "type": "Basic" if item.get("componentLibraryType") == "base" else "Extended",
        "price": prices[0].get("productPrice") if prices else None,
        "price_breaks": [{"qty": p.get("startNumber"), "price": p.get("productPrice")}
                         for p in prices],
        "min_qty": item.get("minPurchaseNum", 1),
        "description": item.get("describe", ""),
        "url": item.get("lcscGoodsUrl", ""),
        "datasheet": item.get("dataManualUrl", ""),
        "attributes": [{"name": a.get("attribute_name_en", ""),
                        "value": a["attribute_value_name"]}
                       for a in (item.get("attributes") or [])
                       if a.get("attribute_value_name")
                       and a["attribute_value_name"] != "-"],
    }


def category_search(category: str, *, attrs: dict[str, list[str]] | None = None,
                    keyword: str = "", in_stock: bool = True,
                    part_type: str | None = None, page: int = 1,
                    page_size: int = 1000, timeout: float = 30.0
                    ) -> tuple[list[dict], int]:
    """One page of a JLC category browse -> (results, total).

    Unlike live_search, a network or API failure raises (httpx error or
    PartsError) rather than looking like an empty category.
    """
    httpx = require_httpx()
    payload: dict[str, Any] = {"currentPage": page, "pageSize": page_size,
                               "secondSortName": category}
    if keyword:
        payload["keyword"] = keyword
    if in_stock:
        payload["stockFlag"] = True
    if part_type:
        payload["componentLibraryType"] = part_type
    if attrs:
        payload["componentAttributeList"] = [{k: list(v)} for k, v in attrs.items()]
    r = httpx.post(JLC_SEARCH_URL, json=payload, timeout=timeout, headers={
        "User-Agent": "Mozilla/5.0", "Origin": "https://jlcpcb.com",
        "Referer": "https://jlcpcb.com/parts"})
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 200:
        raise PartsError(f"JLC category search failed: code {body.get('code')} "
                         f"{body.get('message') or ''}".strip())
    info = (body.get("data") or {}).get("componentPageInfo") or {}
    return ([_jlc_item(i) for i in info.get("list") or []],
            int(info.get("total") or 0))


def endpoint_reachable(timeout: float = 8.0) -> bool:
    """True if jlcpcb.com answers - distinguishes 'no matches' from 'offline'."""
    httpx = require_httpx()
    try:
        r = httpx.get("https://jlcpcb.com/", timeout=timeout,
                      headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code < 500
    except (httpx.TransportError, httpx.TimeoutException, OSError):
        return False


# ------------------------------------------------------------- normalization

def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_WWW_DATASHEET = re.compile(
    r"^https?://www\.lcsc\.com/datasheet/lcsc_datasheet_(.+\.pdf)$", re.I)
# the older datasheet.lcsc.com/lcsc/<stem>.pdf (and /szlcsc/) form now
# redirects to www.lcsc.com/datasheet/C<code>.pdf, the same viewer shell
# (#1476: 6 of 9 training "datasheet refused" parts); its stem is the
# wmsc file's name too. One path segment only, no query.
_OLD_DATASHEET = re.compile(
    r"^https?://datasheet\.lcsc\.com/(?:lcsc|szlcsc)/([^/?#]+\.pdf)$", re.I)


def fix_datasheet_url(url: str) -> str:
    """Rewrite www.lcsc.com datasheet URLs to the fetchable wmsc mirror.

    www.lcsc.com/datasheet/lcsc_datasheet_<stem>.pdf serves a JS viewer SHELL
    (HTML, curl exit 0) to any non-browser client; the same file is a real PDF
    at wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/<stem>.pdf (LEARNINGS
    2026-07-28 [parts][datasheet]: 7/7 + 18/18 recovered on first retry).
    The older datasheet.lcsc.com/lcsc/<stem>.pdf and /szlcsc/ forms now
    redirect to that shell too, and are rewritten the same way (#1476).
    Pure string transform, idempotent; wmsc-form and other hosts pass through.
    """
    m = _WWW_DATASHEET.match(url or "") or _OLD_DATASHEET.match(url or "")
    if m:
        return "https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/" + m.group(1)
    return url or ""


def normalize(item: dict) -> dict:
    """One JLC search result -> the pipeline's candidate-part shape.

    Superset of SPEC P3 parts.json fields (mpn, lcsc, value/package, basic,
    stock, price) plus datasheet URL + attributes the sourcer/datasheet agents
    consume. `basic` (bool) is the JLC no-feeder-fee flag.
    """
    stock = item.get("stock")
    return {
        "lcsc": item.get("lcsc", "") or "",
        "mpn": item.get("model", "") or "",
        "brand": item.get("brand", "") or "",
        "description": item.get("description", "") or "",
        "package": item.get("package", "") or "",
        "category": item.get("category", "") or "",
        "type": item.get("type", "") or "",
        "basic": item.get("type") == "Basic",
        "stock": int(stock) if isinstance(stock, (int, float)) else 0,
        "price": _as_float(item.get("price")),
        "price_breaks": item.get("price_breaks", []) or [],
        "min_qty": item.get("min_qty", 1),
        # rewrite the unfetchable www form at the SOURCE so no agent sees it
        "datasheet": fix_datasheet_url(item.get("datasheet", "") or ""),
        "url": item.get("url", "") or "",
        "attributes": item.get("attributes", []) or [],
    }


_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
       "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_QTY = re.compile(r"(\d+(?:\.\d+)?)\s*([pnuµμmkKMG]?)")


def attr_max(p: dict, name: str) -> float | None:
    """The largest quantity in attribute `name` ('250mA、1.25A、5A' -> 5.0),
    SI prefixes applied; None when the part doesn't state it."""
    best = None
    for a in p.get("attributes") or []:
        if a.get("name", "").lower() != name.lower():
            continue
        for num, pre in _QTY.findall(str(a.get("value", ""))):
            v = float(num) * _SI[pre]
            best = v if best is None else max(best, v)
    return best


def rank_key(p: dict) -> tuple:
    """Prefer JLC Basic (no feeder fee, SPEC P3), then more stock, then cheaper."""
    price = p["price"] if p["price"] is not None else float("inf")
    return (0 if p["basic"] else 1, -p["stock"], price)


def apply_filters(parts: list[dict], *, basic_only: bool = False,
                  package: str | None = None, min_stock: int = 0,
                  max_price: float | None = None, brand: str | None = None,
                  contains: str | None = None) -> list[dict]:
    out = []
    for p in parts:
        if basic_only and not p["basic"]:
            continue
        if package and package.lower() not in p["package"].lower():
            continue
        if p["stock"] < min_stock:
            continue
        if max_price is not None and (p["price"] is None or p["price"] > max_price):
            continue
        if brand and brand.lower() not in p["brand"].lower():
            continue
        if contains and contains.lower() not in (
                p["description"] + " " + p["mpn"]).lower():
            continue
        out.append(p)
    return out


# ------------------------------------------------------------- jlcparts DB fallback

# jlcparts (yaqwsx) cache.sqlite3: components(lcsc INTEGER PK, mfr, package,
# basic INTEGER, description, datasheet, stock INTEGER, price TEXT-json, ...).
_DB_SEARCH_COLS = ("description", "mfr")


def db_search(db_path: str | Path, keyword: str, limit: int = 25) -> list[dict]:
    """Keyword search a cached jlcparts SQLite DB -> normalized candidates.

    Column-tolerant: only relies on columns that exist. Raises PartsError if the
    file/table is missing so the caller can fall through to the web-search hint.
    """
    p = Path(db_path)
    if not p.exists():
        raise PartsError(f"jlcparts DB not found: {p}")
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise PartsError(f"cannot open jlcparts DB {p}: {exc}") from exc
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(components)")]
        if not cols:
            raise PartsError(f"{p} has no 'components' table")
        search_cols = [c for c in _DB_SEARCH_COLS if c in cols]
        if not search_cols:
            raise PartsError(f"{p} components table lacks description/mfr columns")
        where = " OR ".join(f'"{c}" LIKE ?' for c in search_cols)
        like = f"%{keyword}%"
        rows = con.execute(
            f"SELECT * FROM components WHERE {where} LIMIT ?",
            (*([like] * len(search_cols)), limit),
        ).fetchall()
    except sqlite3.Error as exc:
        raise PartsError(f"jlcparts DB query failed: {exc}") from exc
    finally:
        con.close()
    return [normalize(_db_row_to_item(dict(zip(cols, r)))) for r in rows]


def _db_row_to_item(row: dict) -> dict:
    """A jlcparts components row -> the JLC-search item shape normalize() wants."""
    price = None
    raw_price = row.get("price")
    if isinstance(raw_price, str) and raw_price.strip():
        try:
            breaks = json.loads(raw_price)
            if isinstance(breaks, list) and breaks:
                price = breaks[0].get("price")
        except (json.JSONDecodeError, AttributeError):
            price = None
    lcsc = row.get("lcsc")
    lcsc_str = f"C{lcsc}" if isinstance(lcsc, int) else (str(lcsc) if lcsc else "")
    return {
        "lcsc": lcsc_str,
        "model": row.get("mfr", ""),
        "brand": row.get("manufacturer", "") or row.get("brand", ""),
        "description": row.get("description", ""),
        "package": row.get("package", ""),
        "category": row.get("category", ""),
        "type": "Basic" if row.get("basic") else "Extended",
        "stock": row.get("stock", 0),
        "price": price,
        "datasheet": row.get("datasheet", ""),
    }


# ------------------------------------------------------------- output contract

def utf8_stdout() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def emit(payload: dict, out: str | None) -> int:
    """Write/print JSON; exit 0 for pass/empty/no_candidate, 2 for error
    (SPEC section 6: a search has no exit-1 "violations" notion - a query
    that ran but found no conforming pick is still a completed query)."""
    text = json.dumps(payload, indent=1, ensure_ascii=True)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if payload.get("status") in ("pass", "empty", "no_candidate") else 2
