#!/usr/bin/env python
"""part_rows.py - draft netlist_audit rule-table rows from a datasheet, and check their cites (#1100).

The datasheet rules (pin_state, pull_vs_leakage, driver_contention,
io_overvoltage, i2c_address_collision, the mcu_pins rules) judge only the
parts their reference tables hold. This tool keeps those tables growing
without trusting anything nobody read:

  PART --pdf FILE   draft rows for PART from the datasheet's text (pypdf, one
                    text per 1-based PDF page). Get the PDF from the
                    part-sourcer's cache (`datasheet_extract --fetch WS`
                    writes WS/parts/datasheets/<lcsc>.pdf) or the vendor.
                    Every drafted row is `verified: false` and carries a
                    `cite` list: {doc, sha256, page, table, quote, fields},
                    where quote is text copied from that page and fields the
                    row's numbers it gives. What it drafts:
                      pin_states   "do not leave the X pin floating" ->
                                   X forbid [open]
                      pin_leakage  an I/O row: vil_frac / vih_frac from a
                                   VIL/VIH line in fractions of VDD, leak
                                   from a leakage line
                      io_levels    a driver row: voh_drop from the largest
                                   "VDD-n" on a VOH line
                    i2c_addresses and mcu_pins are not drafted from text
                    (an address table's straps and an AF matrix need a
                    reader or the vendor's pin XML); the report lists the
                    pages that mention them as `leads`. A draft is a lead
                    list for a reader, not a table: the reader fixes the
                    fields and the pin regex, then runs --verify, and only
                    then sets verified: true.
  --verify YAML ... --pdf FILE ...
                    check every row with a `cite` in the given tables
                    against the PDFs (matched by sha256, else by file name
                    = doc): the quote must be on the cited page, the
                    table named on it or the two pages before, and each
                    number of the row's cited fields must be a number in the
                    quote at some SI scale (1.0e-6 A matches "1 uA", 30000
                    ohm matches "30 kohm"). A quote found only on another
                    page is reported with the page it is on. Rows whose PDF
                    was not given are listed as unchecked.

Text is compared after NFKC, dash folding and with every space removed
(pypdf splits "0 . 3" and runs words together), so a quote survives
re-extraction but a page number or a value that is wrong does not.

netlist_audit.py never reports a finding from a `verified: false` row as a
violation: it goes under not_judged.

Contract (SPEC section 6): argparse, JSON to stdout or --out.
exit 0 = drafted / every cite checked out, 1 = a cite failed (or nothing
drafted), 2 = unreadable PDF or table.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_SCALES = (1.0, 1e3, 1e6, 1e9, 1e-3, 1e-6)
# The number fields of each table's rows that a cite can stand behind.
_NUMERIC = ("ipd", "ipu", "leak", "rpu", "vil", "vih", "vil_frac", "vih_frac",
            "voh_drop", "vin_max", "max_v")


def norm(text: str) -> str:
    """Text as cites compare it: NFKC, dashes folded, no whitespace at all."""
    t = unicodedata.normalize("NFKC", text or "").translate(_DASHES)
    return re.sub(r"\s+", "", t)


def numbers(quote: str) -> list[float]:
    """The numbers in a quote. Spaces still split numbers ('25 40 55' is
    three), but not around a decimal point (pypdf's '0 . 3' is 0.3)."""
    t = unicodedata.normalize("NFKC", quote or "").translate(_DASHES)
    t = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", t)
    return [float(m) for m in _NUM.findall(t)]


def pdf_pages(path: Path) -> list[str]:
    """Text per page, index 0 = PDF page 1. Raises ValueError on non-PDF."""
    data = path.read_bytes()
    if not data.startswith(b"%PDF"):
        raise ValueError(f"{path}: not a PDF")
    from io import BytesIO

    from pypdf import PdfReader
    return [(p.extract_text() or "") for p in PdfReader(BytesIO(data)).pages]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- drafting

_FLOAT = re.compile(r"[Dd]o not leave (?:the )?([A-Z][A-Z0-9_]*) pin floating")
_VDD = r"V\s*DD\w*"
_FRAC = re.compile(r"(0\s*\.\s*\d+)\s*[x×*]?\s*" + _VDD)
_VOH_DROP = re.compile(_VDD + r"\s*-\s*(\d\s*\.\s*\d+)")
_LEAK_UNIT = re.compile(r"(\d+(?:\s*\.\s*\d+)?)\s*(n|µ|μ|u)A\b")
_LEADS = {"i2c_addresses": re.compile(r"(slave|target|device) address", re.I),
          "mcu_pins": re.compile(r"alternate function", re.I)}


def _window(lines: list[str], i: int, n: int) -> str:
    return " ".join(x.strip() for x in lines[i:i + n] if x.strip())


def _cite(doc, digest, page, table, quote, fields) -> dict:
    return {"doc": doc, "sha256": digest, "page": page, "table": table,
            "quote": quote, "fields": fields}


def _table_name(lines: list[str]) -> str:
    for x in lines:
        m = re.search(r"\b(Table\s+[\d.-]+[.:]?[^\n]{0,60})", x)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _first(pages, doc, digest, pat, key, span, fields, take):
    """(value, cite) from the first page/line where `pat` matches and
    take(window) gives a value; else (None, None)."""
    for pno, text in enumerate(pages, 1):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not pat.search(line):
                continue
            win = _window(lines, i, span)
            val = take(win)
            if val is not None:
                return val, _cite(doc, digest, pno, _table_name(lines), win,
                                  fields)
    return None, None


def _frac(win: str):
    m = _FRAC.search(win.translate(_DASHES))
    return float(re.sub(r"\s", "", m.group(1))) if m else None


def _drop(win: str):
    vals = [float(re.sub(r"\s", "", v))
            for v in _VOH_DROP.findall(win.translate(_DASHES))]
    return max(vals) if vals else None


def _leak(win: str):
    scale = {"n": 1e-9, "µ": 1e-6, "μ": 1e-6, "u": 1e-6}
    vals = [float(re.sub(r"\s", "", v)) * scale[u]
            for v, u in _LEAK_UNIT.findall(win)]
    return max(vals) if vals else None


def draft(part: str, pdf: Path) -> dict:
    pages = pdf_pages(pdf)
    doc, digest = pdf.name, sha256(pdf)
    out: dict = {"part": part, "doc": doc, "sha256": digest,
                 "pages": len(pages), "pin_states": [], "pin_leakage": [],
                 "io_levels": [], "leads": {}}
    pins_seen = set()
    for pno, text in enumerate(pages, 1):
        for m in _FLOAT.finditer(text.replace("\n", " ")):
            pin = m.group(1)
            if pin in pins_seen:
                continue
            pins_seen.add(pin)
            out["pin_states"].append({
                "pin": pin, "forbid": ["open"],
                "why": f"the datasheet says not to leave {pin} floating",
                "cite": [_cite(doc, digest, pno, "", m.group(0), [])]})
    row: dict = {"pin": "TODO", "cite": []}
    for key, pat in (("vil_frac", re.compile(r"\bV\s*IL\b(?!\s*\()")),
                     ("vih_frac", re.compile(r"\bV\s*IH\b(?!\s*\()"))):
        val, cite = _first(pages, doc, digest, pat, key, 4, [key], _frac)
        if val is not None:
            row[key] = val
            row["cite"].append(cite)
    val, cite = _first(pages, doc, digest,
                       re.compile(r"I\s*lkg|I\s*leak|Input\s+leakage", re.I),
                       "leak", 6, ["leak"], _leak)
    if val is not None:
        row["leak"] = val
        row["cite"].append(cite)
    if row["cite"]:
        out["pin_leakage"].append(row)
    val, cite = _first(pages, doc, digest, re.compile(r"\bV\s*OH\b"),
                       "voh_drop", 30, ["voh_drop"], _drop)
    if val is not None:
        out["io_levels"].append({"supply": "TODO", "pins": "TODO",
                                 "voh_drop": val, "cite": [cite]})
    for table, pat in _LEADS.items():
        hits = [p for p, t in enumerate(pages, 1) if pat.search(t)]
        if hits:
            out["leads"][table] = hits[:20]
    for key in ("pin_states", "pin_leakage", "io_levels"):
        for r in out[key]:
            r["verified"] = False
    return out


# --------------------------------------------------------------- verifying

def _rows(node, path="", verified=False):
    """Yield (path, row, verified) for every dict under node carrying a
    `cite`; verified is the nearest `verified` key at or above the row (a
    pin_leakage pin row takes its part's)."""
    if isinstance(node, dict):
        verified = node.get("verified", verified) is True
        if isinstance(node.get("cite"), list):
            yield path, node, verified
        for k, v in node.items():
            if k != "cite":
                yield from _rows(v, f"{path}.{k}" if path else str(k),
                                 verified)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            label = v.get("mpn") or v.get("pin") if isinstance(v, dict) else ""
            yield from _rows(v, f"{path}[{label or i}]", verified)


def _flat(v) -> list[float]:
    if isinstance(v, bool):
        return []
    if isinstance(v, (int, float)):
        return [float(v)]
    if isinstance(v, (list, tuple)):
        return [x for e in v for x in _flat(e)]
    return []


def _in_quote(val: float, nums: list[float]) -> bool:
    a = abs(val)
    for s in _SCALES:
        for q in nums:
            if abs(a * s - q) <= 1e-9 * max(1.0, q):
                return True
    return False


def check_cite(row: dict, cite: dict, pages: list[str]) -> list[str]:
    """Problems with one cite of one row; [] when it holds."""
    probs = []
    quote = norm(cite.get("quote") or "")
    page = cite.get("page")
    if not quote:
        return ["cite has no quote"]
    if not isinstance(page, int) or not 1 <= page <= len(pages):
        probs.append(f"page {page} not in the PDF (1-{len(pages)})")
    elif quote not in norm(pages[page - 1]):
        on = [p for p, t in enumerate(pages, 1) if quote in norm(t)]
        probs.append(f"quote not on page {page}"
                     + (f"; it is on page {', '.join(map(str, on))}"
                        if on else "; not found in the PDF"))
    table = norm(str(cite.get("table") or "")).casefold()
    if table and isinstance(page, int) and 1 <= page <= len(pages):
        # a table may run on from up to two pages before its cited page
        if not any(table in norm(pages[p - 1]).casefold()
                   for p in range(max(1, page - 2), page + 1)):
            probs.append(f"{cite['table']} is not on page {page} or the "
                         "two before it")
    nums = numbers(cite.get("quote") or "")
    for f in cite.get("fields") or ():
        if f not in row:
            probs.append(f"cited field {f} not in the row")
            continue
        for v in _flat(row[f]):
            if not _in_quote(v, nums):
                probs.append(f"{f}={v:g} is not a number in the quote")
    return probs


def verify(tables: list[Path], pdfs: list[Path]) -> dict:
    import yaml
    by_sha, by_name = {}, {}
    for p in pdfs:
        pages = pdf_pages(p)
        by_sha[sha256(p)] = pages
        by_name[p.name] = pages
    rows = []
    for t in tables:
        data = yaml.safe_load(t.read_text()) or {}
        for path, row, ver in _rows(data):
            for i, cite in enumerate(row["cite"]):
                pages = by_sha.get(cite.get("sha256")) or by_name.get(
                    cite.get("doc"))
                rec = {"table": t.name, "row": path, "cite": i,
                       "page": cite.get("page"),
                       "verified": ver}
                if pages is None:
                    rec["status"] = "unchecked"
                    rec["problems"] = [f"no PDF given for {cite.get('doc')}"]
                else:
                    probs = check_cite(row, cite, pages)
                    rec["status"] = "fail" if probs else "ok"
                    rec["problems"] = probs
                rows.append(rec)
    n = {s: sum(r["status"] == s for r in rows)
         for s in ("ok", "fail", "unchecked")}
    return {"cites": rows, "counts": n}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("part", nargs="?", help="the part to draft rows for")
    ap.add_argument("--pdf", nargs="+", type=Path, default=[],
                    help="the datasheet (draft: one; --verify: any number)")
    ap.add_argument("--verify", nargs="+", type=Path, metavar="YAML",
                    help="check the cites of these rule tables")
    ap.add_argument("--out", type=Path, help="write the JSON here")
    a = ap.parse_args(argv)
    try:
        if a.verify:
            res = verify(a.verify, a.pdf)
            code = 1 if res["counts"]["fail"] else 0
        elif a.part and len(a.pdf) == 1:
            res = draft(a.part, a.pdf[0])
            code = 0 if (res["pin_states"] or res["pin_leakage"]
                         or res["io_levels"]) else 1
        else:
            ap.error("give PART --pdf FILE, or --verify YAML... --pdf FILE...")
    except (OSError, ValueError) as e:
        print(json.dumps({"error": str(e)}))
        return 2
    except Exception as e:  # pypdf / yaml parse errors
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 2
    text = json.dumps(res, indent=2, ensure_ascii=False)
    if a.out:
        a.out.write_text(text + "\n")
    else:
        print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
