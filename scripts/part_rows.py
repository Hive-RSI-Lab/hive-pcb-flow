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
                      mcu_pins     timers: each port pin's TIMn_CHx[N]
                                   from an "alternate function mapping"
                                   table (a row runs from a line starting
                                   PB0 to the next pin), one cite per pin;
                                   input_only: GPIO names on a line that
                                   says "input only"
                      i2c_addresses  one row per distinct address a page
                                   states ("7-bit slave address is
                                   0101100b", "address 0x48", "2Ch"), as
                                   a fixed `address`; a datasheet may state
                                   several (the part's own and an EEPROM
                                   it masters), and address straps are
                                   left to the reader
                      pin_abs_max  one row per input-voltage line on an
                                   "absolute maximum" page: vmin from its
                                   first negative number ("VSS - 0.3" is
                                   -0.3), vmax_vdd from "VDD + n" (VCC,
                                   IOVDD, VDDIOx too), else vmax from the
                                   next number ("VI -0.5 7 V")
                      pin_requirements  a part a sentence puts between two
                                   pins ("connect 0.1-uF capacitor between
                                   VBST and SW"): pin, to, kinds [C],
                                   value; rows a figure shows (a divider,
                                   a second filter capacitor) the reader
                                   adds by hand
                      source_limits  a regulator row from the
                                   recommended-conditions and thermal
                                   tables: iout_max from an "IOUT Output
                                   current 0 200 mA" line, tj_max from
                                   the top of a "TJ ... junction
                                   temperature" range, theta_ja the
                                   least of an "RθJA Junction-to-ambient"
                                   line's package columns
                    A draft is a lead list for a reader, not a table: the
                    reader fixes the fields, the match regex and the
                    source, then runs --verify, and only then sets
                    verified: true.
  --verify YAML ... --pdf FILE ...
                    check every row with a `cite` in the given tables
                    against the PDFs (matched by sha256, else by file name
                    = doc): the quote must be on the cited page, the
                    table named on it or the two pages before, and each
                    number of the row's cited fields must be a number in the
                    quote at some SI scale (1.0e-6 A matches "1 uA", 30000
                    ohm matches "30 kohm", a value "100n" matches "0.1-uF"); an `address` must be in the
                    quote as 7-bit binary (0101100b), 0x2C or 2Ch. A quote found only on another
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
            "voh_drop", "vin_max", "max_v", "vmin", "vmax", "vmax_vdd",
            "vf_min")


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
# "Connect 0.1-uF capacitor between VBST and SW pins", "A 0.1-uF ceramic
# capacitor must be connected between the VBST to SW pin", "100nF capacitor
# is recommended from BST to SW": a typical-application part between pins
_PIN_CAP = re.compile(
    r"(\d+(?:\.\d+)?)\s*-?\s*(p|n|µ|μ|u)F\s+(?:\w+\s+){0,2}capacitor\s+"
    r"(?:(?:must|should)\s+be\s+(?:connected|placed)\s+|is\s+(?:required|"
    r"recommended)\s+)?(?:between|from)\s+(?:the\s+)?([A-Z][A-Z0-9_+]*)\s+"
    r"(?:pin\s+)?(?:and|to)\s+(?:the\s+)?([A-Z][A-Z0-9_+]*)\b")
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
       "k": 1e3, "M": 1e6, "": 1.0}
_VDD = r"V\s*DD\w*"
_FRAC = re.compile(r"(0\s*\.\s*\d+)\s*[x×*]?\s*" + _VDD)
_VOH_DROP = re.compile(_VDD + r"\s*-\s*(\d\s*\.\s*\d+)")
_LEAK_UNIT = re.compile(r"(\d+(?:\s*\.\s*\d+)?)\s*(n|µ|μ|u)A\b")
_AF_PAGE = re.compile(r"alternate\s+function\s+mapping", re.I)
_AF_PIN = re.compile(r"^\s*(P[A-K]\d{1,2})\b")
_TIM = re.compile(r"\bTIM\d+_CH\d+N?(?![0-9A-Za-z])")
_INPUT_ONLY = re.compile(r"\binput[\s-]+only\b", re.I)
_GPIO = re.compile(r"\b(P[A-K]\d{1,2}|GPIO\d{1,2})\b")
_ADDR = re.compile(
    r"(?:7-bit\s+)?(?:slave|target|device|I\s*2\s*C|SMBus)\s+address"
    r"(?:\s+(?:is|of|=|:))?\s*:?\s*"
    r"(?:([01]{7})\s*b\b|0x([0-9A-Fa-f]{2})\b|([0-9A-Fa-f]{2})h\b)", re.I)
_ABS_PAGE = re.compile(r"absolute\s+maximum", re.I)
_ABS_LINE = re.compile(r"input\s+voltage|voltage\s+on\s+any|\bV\s*PIN\b|"
                       r"pin\s+voltage|^\s*V\s*I(?:N|/O)?\b", re.I)
_ABS_SKIP = re.compile(r"output|current|differential|common.mode|"
                       r"operating|recommended", re.I)
_ABS_END = re.compile(r"recommended\s+operating|electrical\s+characteristics|"
                      r"thermal\s+information|ESD\s+ratings", re.I)
_ABS_VDD = re.compile(r"(?:IO)?V\s*(?:DD|CC)\w*\s*(?:\(\d\))?\s*\+\s*"
                      r"(\d+(?:\.\d+)?)")


def _si(v) -> float | None:
    """A value as a table writes it: 100n, 0.1u, 4.7uF, 10k, 1.0e-6."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?(?:e-?\d+)?)\s*([pnuµμmkM]?)"
                     r"(?:F|H|ohm|Ω)?\s*", str(v or ""))
    return float(m.group(1)) * _SI[m.group(2)] if m else None


def _pin_parts(pages, doc, digest) -> list[dict]:
    """pin_requirements rows from 'connect a 0.1-uF capacitor between A
    and B' sentences, one per distinct pin pair."""
    rows, seen = [], set()
    for pno, text in enumerate(pages, 1):
        flat = re.sub(r"\s+", " ", text)
        for m in _PIN_CAP.finditer(flat):
            num, unit, a, b = m.groups()
            if (a, b) in seen:
                continue
            seen.add((a, b))
            unit = "u" if unit in "µμ" else unit
            rows.append({"id": f"C_{a}", "pin": a, "to": b, "kinds": ["C"],
                         "value": f"{num}{unit}", "source": "TODO",
                         "cite": [_cite(doc, digest, pno, "", m.group(0),
                                        ["value"])]})
    return rows


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


def _af_pins(pages, doc, digest) -> tuple[dict, list]:
    """({pin: [TIMn_CHx...]}, cites) from every alternate-function mapping
    page: a row starts at a line beginning with a port pin (PB0) and runs
    to the next one, so a wrapped cell (SPI1_NSS/ newline I2S1_WS) stays
    in its pin's row."""
    timers: dict[str, list[str]] = {}
    cites = []
    for pno, text in enumerate(pages, 1):
        if not _AF_PAGE.search(text):
            continue
        lines = text.splitlines()
        table = _table_name(lines)
        rows: list[list[str]] = []
        for x in lines:
            if _AF_PIN.match(x):
                rows.append([x])
            elif rows and x.strip():
                rows[-1].append(x)
        for row in rows:
            pin = _AF_PIN.match(row[0]).group(1)
            seg = " ".join(r.strip() for r in row)
            tims = sorted(set(_TIM.findall(re.sub(r"\s*_\s*", "_", seg))))
            if not tims or pin in timers:
                continue
            timers[pin] = tims
            cites.append(_cite(doc, digest, pno, table, seg, []))
    return timers, cites


def _input_only(pages, doc, digest) -> tuple[list, list]:
    """(pins, cites): GPIO names on a line that says input only."""
    pins, cites = [], []
    for pno, text in enumerate(pages, 1):
        lines = text.splitlines()
        for i, x in enumerate(lines):
            if not _INPUT_ONLY.search(x):
                continue
            got = [g for g in _GPIO.findall(x) if g not in pins]
            if got:
                pins += got
                cites.append(_cite(doc, digest, pno, _table_name(lines),
                                   x.strip(), []))
    return pins, cites


_PIN_PAGE = re.compile(r"pin\s+(?:functions|descriptions?|configuration)|"
                       r"pinning\s+information|pin\s+assignment", re.I)
# a pin name is upper case (a trailing n for active-low: RESETn)
_PIN_NAME = r"([A-Z0-9_+#]*[A-Z][A-Z0-9_+#]*n?(?:/[A-Z0-9_+#]+n?)*)"
_PIN_NUMS = r"(\d{1,3}(?:\s*,\s*\d{1,3})*)"
_PIN_LINE = (re.compile(rf"^\s*{_PIN_NAME}\s+{_PIN_NUMS}(?![\d.\w])"),
             re.compile(rf"^\s*{_PIN_NUMS}\s+{_PIN_NAME}(?![\w/])"))
# a contents page names 'Pin Configuration' too: its lines end in dot leaders
_LEADERS = re.compile(r"\.{5,}|(?:\. ){5,}")
# a section heading ('7 Specifications') ends a table run onto a next page
_HEADING = re.compile(r"^\s*\d+(?:\.\d+)*\s+[A-Z][a-z]")


def _pin_run(text: str, start: int, stop_at_heading: bool):
    """({number: name}, quote) of the pin lines of TEXT from START."""
    pins: dict[str, str] = {}
    first = last = None
    pos = start
    for line in text[start:].splitlines(keepends=True):
        hit = None
        for i, rx in enumerate(_PIN_LINE):
            m = rx.match(line)
            if m:
                hit = (m.group(1), m.group(2)) if i == 0 else (
                    m.group(2), m.group(1))
                break
        if hit and hit[0] not in ("PIN", "NO", "NAME"):
            for n in re.split(r"\s*,\s*", hit[1]):
                pins.setdefault(str(int(n)), hit[0])
            first = pos if first is None else first
            last = pos + len(line.rstrip())
        elif stop_at_heading and _HEADING.match(line):
            break
        pos += len(line)
    return pins, (text[first:last] if first is not None else "")


def _pin_tables(pages, doc, digest, part) -> list[dict]:
    """One part_pinouts row per pin table ('Pin Functions', 'Pin
    Descriptions', 'Pinning information' and the like) that gives at least
    3 pins: each line 'NAME 4' / 'NAME 7, 8' / '4 NAME' after the heading,
    and on the next page up to its first section heading (a table that
    runs on). Each page's run is one cite, quoted first line to last. A
    contents page (dot leaders) is skipped."""
    out = []
    for pno, text in enumerate(pages, 1):
        heads = list(_PIN_PAGE.finditer(text))
        if not heads or len(_LEADERS.findall(text)) >= 3:
            continue
        pins, quote = _pin_run(text, heads[-1].start(), False)
        if len(pins) < 3:
            continue
        cites = [_cite(doc, digest, pno, _table_name(text.splitlines()),
                       quote, ["pins"])]
        if pno < len(pages) and not _PIN_PAGE.search(pages[pno]):
            more, q2 = _pin_run(pages[pno], 0, True)
            if more:
                for k, v in more.items():
                    pins.setdefault(k, v)
                cites.append(_cite(doc, digest, pno + 1, "", q2, ["pins"]))
        out.append({"part": part, "match": "TODO", "footprint": "TODO",
                    "pins": dict(sorted(pins.items(),
                                        key=lambda kv: int(kv[0]))),
                    "source": "TODO", "cite": cites})
    return out


def _addresses(pages, doc, digest, part) -> list[dict]:
    """One draft i2c_addresses row per distinct address a page states
    ("The 7-bit slave address is 0101100b", "address 0x48", "2Ch"). A
    datasheet often states more than one (the part's own and the EEPROM
    it masters): the reader keeps the part's."""
    out, seen = [], set()
    for pno, text in enumerate(pages, 1):
        flat = re.sub(r"\s+", " ", text)
        for m in _ADDR.finditer(flat):
            b, x, h = m.groups()
            adr = int(b, 2) if b else int(x or h, 16)
            if adr in seen or adr > 0x7F:
                continue
            seen.add(adr)
            out.append({"mpn": part, "match": "TODO", "address": adr,
                        "source": "TODO",
                        "cite": [_cite(doc, digest, pno,
                                       _table_name(text.splitlines()),
                                       m.group(0), ["address"])]})
    return out


def _abs_max(line: str):
    """{vmin, vmax|vmax_vdd} from one abs-max line, else None."""
    t = unicodedata.normalize("NFKC", line).translate(_DASHES)
    t = t.replace("\u26b6", "-")  # Espressif's minus glyph
    t = re.sub(r"\(\d\)|(?<=[a-z\)])\d\b", " ", t)  # footnote markers
    t = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", t)
    out: dict = {}
    m = re.search(r"-\s*(\d+(?:\.\d+)?)", t)
    if m:
        out["vmin"] = -float(m.group(1))
    v = _ABS_VDD.search(t)
    if v:
        out["vmax_vdd"] = float(v.group(1))
    else:
        rest = t[m.end():] if m else t
        n = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)\s*V\b", rest) or \
            re.search(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])", rest)
        if n:
            out["vmax"] = float(n.group(1))
    return out if ("vmax" in out or "vmax_vdd" in out) else None


def _abs_rows(pages, part, doc, digest) -> list[dict]:
    rows, seen = [], set()
    for pno, text in enumerate(pages, 1):
        if not _ABS_PAGE.search(text):
            continue
        lines = text.splitlines()
        on = False
        for line in lines:
            # only lines inside the abs-max table: from its heading to the
            # next table (recommended operating conditions, ...)
            if _ABS_PAGE.search(line):
                on = True
            elif _ABS_END.search(line):
                on = False
            if not on:
                continue
            if (not _ABS_LINE.search(line) or _ABS_SKIP.search(line)
                    or re.match(r"\s*\d+(\.\d+)+\s", line)  # TOC/heading
                    or re.search(r"[<>≤≥]", line)):  # a condition, not a row
                continue
            vals = _abs_max(line)
            q = line.strip()
            if not vals or q in seen:
                continue
            seen.add(q)
            rows.append({"mpn": part, "pins": "TODO", "supply": "TODO",
                         **vals,
                         "cite": [_cite(doc, digest, pno, _table_name(lines),
                                        q, sorted(vals))]})
            if len(rows) >= 8:
                return rows
    return rows


_IOUT_RE = re.compile(r"^\s*I\s?OUT\b.*?output current\b.*?"
                      r"(\d+(?:\.\d+)?)\s*(mA|A)\b", re.I)
_TJ_RE = re.compile(r"^\s*T\s?J\b.*?junction temperature\b.*?"
                    r"([-–−]?\d+)\s+\+?(\d+)\s*°C", re.I)
_RJA_RE = re.compile(r"R\s?[θϴ]\s?JA\b.*?junction-to-ambient", re.I)


def _reg_rows(pages, part, doc, digest) -> list[dict]:
    """A source_limits.yaml regulator draft: iout_max, tj_max, theta_ja,
    each from the first line of its kind; [] without an IOUT line."""
    row: dict = {"mpn": part, "match": "TODO", "kind": "ldo",
                 "out": "^V?OUT$", "in": "^V?IN$", "source": "TODO"}
    cites = []
    for pno, text in enumerate(pages, 1):
        for line in text.splitlines():
            q = line.strip()
            m = _IOUT_RE.search(q)
            if m and "iout_max" not in row:
                nums = re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)(?=\s+(?:\d|mA|A\b))",
                                  q[:m.end()])
                v = float(nums[-1] if nums else m.group(1))
                row["iout_max"] = v / 1000 if m.group(2) == "mA" else v
                cites.append(_cite(doc, digest, pno, "", q, ["iout_max"]))
            m = _TJ_RE.search(q)
            if m and "tj_max" not in row:
                row["tj_max"] = float(m.group(2))
                cites.append(_cite(doc, digest, pno, "", q, ["tj_max"]))
            if _RJA_RE.search(q) and "theta_ja" not in row:
                tail = q[_RJA_RE.search(q).end():]
                nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?",
                                                     tail)]
                if nums:
                    row["theta_ja"] = min(nums)
                    cites.append(_cite(doc, digest, pno, "", q,
                                       ["theta_ja"]))
    if "iout_max" not in row:
        return []
    row["cite"] = cites
    return [row]


def draft(part: str, pdf: Path) -> dict:
    pages = pdf_pages(pdf)
    doc, digest = pdf.name, sha256(pdf)
    out: dict = {"part": part, "doc": doc, "sha256": digest,
                 "pages": len(pages), "pin_states": [], "pin_leakage": [],
                 "io_levels": [], "mcu_pins": [], "i2c_addresses": [],
                 "pin_abs_max": [], "pin_requirements": [],
                 "part_pinouts": [], "source_limits": []}
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
    timers, tcites = _af_pins(pages, doc, digest)
    only, ocites = _input_only(pages, doc, digest)
    if timers or only:
        mcu: dict = {"mpn": part, "match": "TODO", "source": "TODO"}
        if timers:
            mcu["timers"] = timers
        if only:
            mcu["input_only"] = only
        mcu["cite"] = tcites + ocites
        out["mcu_pins"].append(mcu)
    out["i2c_addresses"] = _addresses(pages, doc, digest, part)
    out["pin_abs_max"] = _abs_rows(pages, part, doc, digest)
    out["pin_requirements"] = _pin_parts(pages, doc, digest)
    out["part_pinouts"] = _pin_tables(pages, doc, digest, part)
    out["source_limits"] = _reg_rows(pages, part, doc, digest)
    for key in ("pin_states", "pin_leakage", "io_levels", "mcu_pins",
                "i2c_addresses", "pin_abs_max", "pin_requirements",
                "part_pinouts", "source_limits"):
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
            label = (v.get("mpn") or v.get("pin") or v.get("part")
                     if isinstance(v, dict) else "")
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


def _addr_in_quote(adr, quote: str) -> bool:
    """A 7-bit address as the quote writes it: 0101100b / 0101100, 0x2C,
    2Ch."""
    if not isinstance(adr, int) or isinstance(adr, bool):
        return False
    q = unicodedata.normalize("NFKC", quote).casefold()
    return any(re.search(rf"\b{pat}\b", q)
               for pat in (f"{adr:07b}b?", f"0x{adr:02x}", f"{adr:02x}h"))


def _pin_key(name: str) -> str:
    t = unicodedata.normalize("NFKC", str(name)).translate(_DASHES)
    return re.sub(r"[~{}]", "", t).upper()


def _pins_in_quote(pins: dict, quote: str) -> list[str]:
    """The pins {number: name} the quote does not give: a pin is given
    when its name stands next to a run of pin numbers ('GND 6', 'NC 7, 8',
    '1, 2, 3 S') that holds its number."""
    t = unicodedata.normalize("NFKC", quote or "").translate(_DASHES)
    toks = re.findall(r"[^\s,;()]+", t)
    keys = [_pin_key(x) for x in toks]

    def run(i, step):
        nums = []
        while 0 <= i < len(toks) and toks[i].isdigit():
            nums.append(str(int(toks[i])))
            i += step
        return nums

    missing = []
    for num, name in pins.items():
        want = _pin_key(name)
        if not any(k == want and str(num) in run(i + 1, 1) + run(i - 1, -1)
                   for i, k in enumerate(keys)):
            missing.append(f"{num}={name}")
    return missing


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
        if f == "pins":
            # a long table may be quoted over several cites: a pin need
            # only stand in one of the row's pin cites
            pins = {str(k): v for k, v in (row[f] or {}).items()}
            missing = _pins_in_quote(pins, cite.get("quote") or "")
            others = " ".join(c.get("quote") or "" for c in row["cite"]
                              if c is not cite and "pins" in
                              (c.get("fields") or ()))
            missing = [m for m in missing if _pins_in_quote(
                dict([m.split("=", 1)]), others)]
            if missing:
                probs.append("pins not in the quote as a name beside its "
                             "number: " + ", ".join(missing[:8]))
            continue
        if f == "address":
            if not _addr_in_quote(row[f], cite.get("quote") or ""):
                probs.append(f"address={row[f]!r} is not in the quote as "
                             "7-bit binary, 0xNN or NNh")
            continue
        if f == "value":  # an SI string: "100n" is 0.1 in "0.1-uF"
            # ...and 0.1 nF is "100 pF", 2.2 nF is "2,200 pF"
            val = _si(row[f])
            nums_k = numbers(re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "",
                                    cite.get("quote") or ""))
            if val is None or not any(_in_quote(val * s, ns)
                                      for s in (1.0, 1e12)
                                      for ns in (nums, nums_k)):
                probs.append(f"value={row[f]!r} is not a number in the "
                             "quote")
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
            code = 0 if any(res[k] for k in (
                "pin_states", "pin_leakage", "io_levels", "mcu_pins",
                "i2c_addresses", "pin_abs_max", "pin_requirements",
                "part_pinouts", "source_limits")) else 1
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
