#!/usr/bin/env python3
"""rating_fill: fill a two-pin passive's (or a connector's) rating rows from
its own datasheet.

A part record (parts/<MPN>.json) whose two passive pins carry no
`pin_ratings` row is counted by check_ratings as rating_unrated ("no rating
names it"), even when its free-text abs_max says "50 V". This reads the
record's datasheet PDF (the path its `source_pdf` names, else
parts/datasheets/<MPN>.pdf) with a reader for the series, and writes:

  voltage      pin_ratings {pins [pin 1], ref <pin 2 name>, min -V, max V,
               level abs_max}: the part's rated voltage across its two ends.
  current      pin_ratings kind current (level recommended) on both pins.
  power        pin_ratings kind power (W at the stated ambient) on both pins.
  temperature  operating_temp_c [lo, hi], which check_ratings holds against
               the board's ambient.

Every row cites the datasheet: its title or code, the page (1-based) and the
table or line it was read from. A figure the datasheet does not give stays
out, so the part stays unrated for that quantity (a ferrite bead's voltage).
A figure that disagrees with the part number's own code or with the record's
abs_max is not written: it is reported as a conflict for a person to read.

Readers (series, what each reads):
  Yageo RC_L chip resistors (RC0402FR-0710KL): Table 2 row for the size and
    power code (Rated power, operating temperature, maximum working
    voltage); the voltage written is the rated continuous working voltage,
    sqrt(P x R) or the maximum working voltage, whichever is less (the
    RCWV rule on the power-rating page).
  Murata GRM MLCCs: the reference sheet's header "<MPN>_(..., DC 16V)",
    checked against the part number's voltage code; temperature from the
    page's "Operating Temp. Range", where every range on the page agrees.
  Samsung CL MLCCs: "Rated Voltage 6.3V" checked against the voltage code;
    temperature from the X5R/X7R characteristic range "(From -55 C to 85 C".
  Murata BLM ferrite beads: the rated-current table row for the MPN (85 C
    ambient) and "Operating temperature range"; no voltage figure exists.
  JST PH wire-to-board headers (S2B-PH-SM4-TB, B10B-PH-K-S): the catalog's
    Specifications, "Voltage rating: 100 V AC/DC", "Current rating: 2 A
    AC/DC (AWG #24)" and "Temperature range". JST's catalog PDF draws its
    text in a font shifted by 0x1F; the reader undoes the shift.

A connector is the one multi-pin record filled: every pin passive, its
contacts the pins not named MP (mounting pads carry no rating). Its voltage
is rated between neighbouring contacts, so each contact but the last gets a
row with ref the next contact's name; its current row names every contact.

Rows this script wrote carry a source starting "rating_fill:" and are
replaced on a rerun, so it is idempotent; rows written by hand are kept, and
a hand row of the same kind stops the fill of that kind.

--fetch (#1263): a record with no datasheet PDF on the board has its
datasheet fetched from its own `datasheet` URL, else the parts.json line for
its file name (an LCSC code) or MPN, through datasheet_extract.fetch_datasheet
(the same URL checks and verdict) to parts/datasheets/<file name>.pdf, and
entered in that folder's index.json (url, sha256, verdict) as
datasheet_extract --fetch does. Each host is asked at most once per
keyless_stock.PACE seconds, and HTTP 429/503 is asked again after its
Retry-After or a doubling backoff (keyless_stock's TRIES, BACKOFF,
MAX_WAIT). A PDF there is cached: the next run reads it and asks nothing. A
row read from a fetched PDF cites its URL and index.json key. No PDF that
index.json judges suspect (a scan, another part's sheet) is read.

The report gives each record a status: rated (a row was filled) or
not_judged with the reason in `notes` (no PDF and why the fetch failed, a
suspect PDF, no figure found, or not a part this reads: an IC's ratings come
from its datasheet by hand).

Usage:
  rating_fill.py --parts DIR [--mpn MPN ...] [--write] [--fetch]
                 [--out report.json]
Exit: 0 (report printed; --write writes the records), 2 on bad input.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import datasheet_extract  # noqa: E402
import keyless_stock  # noqa: E402

TAG = "rating_fill:"
DEG = r"\s*(?:℃|°\s*C|ºC|C\b)"
RANGE_RE = re.compile(r"(-\d+)\s*(?:℃|°\s*C)?\s*to\s*\+?(\d+)" + DEG)

# --- helpers -----------------------------------------------------------------


def pdf_for(rec: dict, parts_dir: Path, stem: str | None = None
            ) -> Path | None:
    """The record's datasheet: the first .pdf path in source_pdf (relative to
    the board root or the parts dir), else parts/datasheets/<MPN>.pdf, else
    parts/datasheets/<stem>.pdf (the record's file name: where --fetch puts
    it, and datasheet_extract --fetch puts an LCSC part's)."""
    m = re.match(r"\s*(\S+?\.pdf)\b", str(rec.get("source_pdf") or ""), re.I)
    cands = []
    if m:
        cands += [parts_dir.parent / m.group(1), parts_dir / m.group(1)]
    mpn = str(rec.get("mpn") or "")
    cands.append(parts_dir / "datasheets" / f"{mpn}.pdf")
    # the sanitized names check_ratings also looks under ("#" -> "-")
    for rx in (r"[^A-Za-z0-9._-]", r"[^A-Za-z0-9_-]"):
        cands.append(parts_dir / "datasheets" / f"{re.sub(rx, '-', mpn)}.pdf")
    if stem:
        cands.append(parts_dir / "datasheets" / f"{stem}.pdf")
    return next((c for c in cands if c.is_file()), None)


def _title(rec: dict) -> str:
    """The datasheet's name as the record cites it: source_pdf's parenthesis."""
    m = re.search(r"\((.*)", str(rec.get("source_pdf") or ""))
    return (m.group(1).split(",")[0].strip() if m else "datasheet")[:80]


def _num(s: str) -> float:
    if "/" in s:
        a, b = s.split("/")
        return float(a) / float(b)
    return float(s)


def _ohms(code: str) -> float | None:
    """'10K' -> 10000, '4K7' -> 4700, '0R' -> 0, '100R' -> 100."""
    m = re.fullmatch(r"(\d*)([RKM])(\d*)", code)
    if not m or not (m.group(1) or m.group(3)):
        return None
    mult = {"R": 1.0, "K": 1e3, "M": 1e6}[m.group(2)]
    return float(f"{m.group(1) or 0}.{m.group(3) or 0}") * mult


def _page_ranges(text: str) -> set[tuple[float, float]]:
    return {(float(a), float(b)) for a, b in RANGE_RE.findall(text)}


def _unshift(text: str) -> str:
    """JST's catalog font: every glyph drawn 0x1F below its letter."""
    return "".join(chr(ord(c) + 0x1F) if 1 <= ord(c) <= 0x5F else c
                   for c in text)


# --- readers: (mpn, pages, title) -> {"rows": [...], "temp": ..., "notes": []}

def read_yageo_rc(mpn: str, pages: list[dict], title: str) -> dict | None:
    m = re.fullmatch(r"RC(\d{4})([BDFJ])([RKSP])-(\d\d|7W|3W|7D)(\w+?)L", mpn)
    if not m:
        return None
    size, pcode, val = m.group(1), m.group(4), m.group(5)
    ohms = _ohms(val)
    out = {"rows": [], "temp": None, "notes": []}
    if not ohms:
        out["notes"].append(f"{val}: a jumper or unreadable value, not rated "
                            "by sqrt(P x R)")
        return out
    row_re = re.compile(r"(\d+/\d+|\d+(?:\.\d+)?)\s*W\s+(-\d+)\s*℃\s*to\s*"
                        r"(\d+)\s*℃\s+(\d+)\s*V\s+(\d+)\s*V\s+(\d+)\s*V")
    for pg in pages:
        t = pg["text"]
        for lab in re.finditer(rf"^\s*RC{size}\s*$", t, re.M):
            nxt = re.search(r"^\s*RC\d{4}\s*$", t[lab.end():], re.M)
            block = t[lab.end(): lab.end() + nxt.start() if nxt else len(t)]
            rows = row_re.findall(block)
            if not rows:
                continue
            i = 1 if "W" in pcode else 0
            if i >= len(rows):
                out["notes"].append(f"Table 2 has no {pcode} power row for "
                                    f"RC{size}")
                return out
            pw, tlo, thi, vmax, _vol, _dwv = rows[i]
            p = _num(pw)
            rcwv = math.sqrt(p * ohms)
            v = min(float(vmax), rcwv)
            cite = (f"{title}, p{pg['page'] + 1} Table 2 RC{size} "
                    f"{'2x ' if i else ''}power row: {pw} W at 70 C, "
                    f"{tlo}..{thi} C, max working voltage {vmax} V")
            out["rows"].append({
                "kind": "voltage", "max": round(v, 3),
                "source": f"{cite}; rated continuous working voltage = "
                          f"min(sqrt(P x R) = {rcwv:.3g} V, {vmax} V) "
                          "(RCWV rule, power-rating page)"})
            out["rows"].append({"kind": "power", "max": p, "both": True,
                                "source": f"{cite}; derated to 0 W at "
                                          f"{thi} C"})
            out["temp"] = ([float(tlo), float(thi)], cite)
            return out
    out["notes"].append(f"no Table 2 row for RC{size} found")
    return out


GRM_VCODE = {"0E": 2.5, "0G": 4.0, "0J": 6.3, "1A": 10, "1C": 16, "1E": 25,
             "YA": 35, "1V": 35, "1H": 50, "2A": 100, "2D": 200, "2E": 250}


def read_murata_grm(mpn: str, pages: list[dict], title: str) -> dict | None:
    m = re.fullmatch(r"GRM\w{3}\w{2}(\w{2})\w+", mpn)
    if not m or not mpn.startswith("GRM"):
        return None
    code_v = GRM_VCODE.get(m.group(1))
    out = {"rows": [], "temp": None, "notes": []}
    hdr = re.compile(re.escape(mpn[:-1]) + r"_?\s*\((.*?)DC\s*([\d.]+)\s*V\)")
    for pg in pages:
        t = pg["text"]
        h = hdr.search(t)
        if not h:
            continue
        v = float(h.group(2))
        cite = f"{title}, p{pg['page'] + 1} header \"{h.group(0).strip()}\""
        if code_v is not None and abs(code_v - v) > 1e-6:
            out["notes"].append(f"conflict: header {v} V vs part-number code "
                                f"{m.group(1)} = {code_v} V; not written")
        else:
            out["rows"].append({"kind": "voltage", "max": v,
                                "source": f"{cite}, rated voltage DC; part "
                                          f"number voltage code {m.group(1)} "
                                          "agrees"})
        if re.search(r"Operating\s*Temp", t):
            rng = _page_ranges(t)
            if len(rng) == 1:
                (lo, hi), = rng
                out["temp"] = ([lo, hi], f"{title}, p{pg['page'] + 1} "
                                         f"Operating Temp. Range {lo:g} to "
                                         f"{hi:g} C")
            else:
                out["notes"].append(f"p{pg['page'] + 1}: temperature ranges "
                                    f"disagree {sorted(rng)}; none written")
        return out
    out["notes"].append("no header with the rated voltage found")
    return out


CL_VCODE = {"R": 4.0, "Q": 6.3, "P": 10, "O": 16, "A": 25, "L": 35, "B": 50,
            "C": 100}


def read_samsung_cl(mpn: str, pages: list[dict], title: str) -> dict | None:
    m = re.fullmatch(r"CL(\d\d)([A-Z])(\d{3})([A-Z])([A-Z])\w+", mpn)
    if not m:
        return None
    code_v = CL_VCODE.get(m.group(5))
    out = {"rows": [], "temp": None, "notes": []}
    for pg in pages:
        t = pg["text"]
        r = re.search(r"Rated Voltage\s*([\d.]+)\s*V", t)
        if r and out["rows"] == []:
            v = float(r.group(1))
            if code_v is not None and abs(code_v - v) > 1e-6:
                out["notes"].append(f"conflict: {v} V vs voltage code "
                                    f"{m.group(5)} = {code_v} V; not written")
            else:
                out["rows"].append({
                    "kind": "voltage", "max": v,
                    "source": f"{title}, p{pg['page'] + 1} part number "
                              f"field Rated Voltage {r.group(1)}V; voltage "
                              f"code {m.group(5)} agrees"})
        c = re.search(r"\(From\s*(-\d+)\s*℃\s*to\s*(\d+)\s*℃", t)
        if c and out["temp"] is None:
            out["temp"] = ([float(c.group(1)), float(c.group(2))],
                           f"{title}, p{pg['page'] + 1} temperature "
                           f"characteristic range (From {c.group(1)} C to "
                           f"{c.group(2)} C), taken as the operating range")
    if not out["rows"]:
        out["notes"].append("no Rated Voltage line found")
    return out


def read_murata_blm(mpn: str, pages: list[dict], title: str) -> dict | None:
    if not re.fullmatch(r"BLM\d\d\w+", mpn):
        return None
    out = {"rows": [], "temp": None,
           "notes": ["no voltage figure in a bead's datasheet: voltage "
                     "stays unrated"]}
    row = re.compile(re.escape(mpn) + r"\s+\S+\s+(\d+)\s+(-|\d+)\s+(\d+)\s+")
    for pg in pages:
        t = pg["text"]
        r = row.search(t)
        if r and r.group(2) != "-":
            out["rows"].append({
                "kind": "current", "max": float(r.group(2)) / 1000.0,
                "level": "recommended", "both": True,
                "source": f"{title}, p{pg['page'] + 1} rated current table "
                          f"row {mpn}: {r.group(2)} mA at 85 C ambient "
                          f"({r.group(3)} mA at 125 C)"})
        o = re.search(r"Operating\s+temperature\s+range\s*(-\d+)\s*°\s*C\s*to"
                      r"\s*\+?(\d+)\s*°\s*C", t, re.I)
        if o and out["temp"] is None:
            out["temp"] = ([float(o.group(1)), float(o.group(2))],
                           f"{title}, p{pg['page'] + 1} Operating "
                           f"temperature range {o.group(1)} to {o.group(2)} C")
    return out


JST_PH_RE = re.compile(r"[SB]\d+B-PH-\S+")


def read_jst_ph(mpn: str, pages: list[dict], title: str) -> dict | None:
    if not JST_PH_RE.fullmatch(mpn):
        return None
    out = {"rows": [], "temp": None, "notes": []}
    for pg in pages:
        t = pg["text"]
        if "rating" not in t:
            t = _unshift(t)
        where = f"{title}, p{pg['page'] + 1} PH Specifications"
        v = re.search(r"Voltage\s+rating:\s*(\d+(?:\.\d+)?)\s*V.{0,2}AC"
                      r"\s*[/,]\s*DC", t)
        if v and not any(r["kind"] == "voltage" for r in out["rows"]):
            out["rows"].append({
                "kind": "voltage", "max": float(v.group(1)),
                "source": f"{where}: Voltage rating {v.group(1)} V AC/DC "
                          "between contacts"})
        c = re.search(r"Current\s+rating:\s*(\d+(?:\.\d+)?)\s*A.{0,2}AC"
                      r"\s*[/,]\s*DC"
                      r"\s*.?(AWG\s*#\s*\d+)?", t)
        if c and not any(r["kind"] == "current" for r in out["rows"]):
            awg = f" ({c.group(2)})" if c.group(2) else ""
            out["rows"].append({
                "kind": "current", "max": float(c.group(1)),
                "level": "recommended",
                "source": f"{where}: Current rating {c.group(1)} A AC/DC"
                          f"{awg} per contact"})
        # the catalog's minus sign is drawn as a glyph of its own
        o = re.search(r"Temperature\s+range:\s*[-\u2212\xf4]\s*(\d+)\D{0,3}"
                      r"\s*to\s*\+?(\d+)", t)
        if o and out["temp"] is None:
            out["temp"] = ([-float(o.group(1)), float(o.group(2))],
                           f"{where}: Temperature range -{o.group(1)} to "
                           f"+{o.group(2)} C (including temperature rise)")
    return out


READERS = (read_yageo_rc, read_murata_grm, read_samsung_cl, read_murata_blm,
           read_jst_ph)
CONNECTORS = (JST_PH_RE,)


def contacts(rec: dict) -> list[dict] | None:
    """The pins a fill rates: both pins of a two-pin passive, a connector's
    contacts (its pins not named MP); None for any other record."""
    pins = list(rec.get("pinout") or [])
    if any(p.get("type") != "passive" for p in pins):
        return None
    if len(pins) == 2:
        return pins
    mpn = str(rec.get("mpn") or "")
    if not any(rx.fullmatch(mpn) for rx in CONNECTORS):
        return None
    cs = [p for p in pins if not str(p.get("pin", "")).upper().startswith("MP")
          and str(p.get("name", "")).upper() != "MP"]
    return cs if len(cs) >= 2 else None


# --- the fill ------------------------------------------------------------------

def _abs_max_figure(rec: dict, kind: str) -> list[float]:
    """The record's own free-text abs_max figures for a kind (V, A, W)."""
    unit = {"voltage": "V", "current": "A", "power": "W"}[kind]
    rx = {"voltage": r"rated voltage|working voltage|voltage rating",
          "current": r"rated current|current rating",
          "power": r"rated power"}[kind]
    return [float(r["value"]) for r in rec.get("abs_max") or []
            if str(r.get("unit") or "").strip() == unit
            and isinstance(r.get("value"), (int, float))
            and re.search(rx, str(r.get("param") or ""), re.I)]


def fill(rec: dict, pages: list[dict], title: str | None = None) -> dict:
    """-> {"record": new record, "filled": [...], "notes": [...]}; the record
    is unchanged when nothing reads. `title` names the datasheet in each
    row's source (default: the record's source_pdf)."""
    mpn = str(rec.get("mpn") or "")
    pins = contacts(rec)
    rep = {"mpn": mpn, "filled": [], "notes": [], "record": rec}
    if pins is None:
        rep["notes"].append("not a two-pin passive or a known connector: "
                            "skipped")
        return rep
    title = title or _title(rec)
    got = None
    for reader in READERS:
        got = reader(mpn, pages, title)
        if got is not None:
            break
    if got is None:
        rep["notes"].append("no reader for this series")
        return rep
    rep["notes"] += got["notes"]
    new = json.loads(json.dumps(rec))
    keep = [r for r in new.get("pin_ratings") or []
            if not str(r.get("source") or "").startswith(TAG)]
    hand = {r.get("kind", "voltage") for r in keep}
    rows = list(keep)
    for r in got["rows"]:
        kind = r["kind"]
        if kind in hand:
            rep["notes"].append(f"{kind}: a hand-written row exists, kept")
            continue
        own = _abs_max_figure(rec, kind)
        if kind == "voltage" and own and r["max"] > max(own) + 1e-9:
            rep["notes"].append(f"conflict: read {r['max']} V, over the "
                                f"record's abs_max {max(own)} V; not written")
            continue
        if kind in ("current", "power") and own and \
                all(abs(x - r["max"]) > 1e-9 for x in own):
            rep["notes"].append(f"conflict: read {r['max']} for {kind}, the "
                                f"record says {own}; not written")
            continue
        src = f"{TAG} {r['source']}"
        if kind == "voltage":
            for a, b in zip(pins, pins[1:]):
                rows.append({"pins": [a["pin"]], "kind": "voltage",
                             "level": "abs_max", "min": -r["max"],
                             "max": r["max"], "ref": b["name"],
                             "source": src})
        else:
            rows.append({"pins": [p["pin"] for p in pins], "kind": kind,
                         "level": r.get("level", "abs_max"), "max": r["max"],
                         "source": src})
        rep["filled"].append({"kind": kind, "max": r["max"], "source": src})
    if rows:
        new["pin_ratings"] = rows
    elif "pin_ratings" in new:
        del new["pin_ratings"]
    if got["temp"] is not None:
        old_src = str(rec.get("operating_temp_source") or "")
        if rec.get("operating_temp_c") is not None and \
                not old_src.startswith(TAG):
            rep["notes"].append("temperature: a hand-written range exists, "
                                "kept")
        else:
            (lo, hi), cite = got["temp"]
            new["operating_temp_c"] = [lo, hi]
            new["operating_temp_source"] = f"{TAG} {cite}"
            rep["filled"].append({"kind": "temperature", "range": [lo, hi],
                                  "source": f"{TAG} {cite}"})
    rep["record"] = new
    return rep


# --- fetching a datasheet the board lacks (--fetch, #1263) ----------------------

PACE = keyless_stock.PACE          # seconds between two requests to one host
TRIES = keyless_stock.TRIES        # asks of one URL while the host throttles
BACKOFF = keyless_stock.BACKOFF    # first wait with no Retry-After; doubles
MAX_WAIT = keyless_stock.MAX_WAIT  # cap on one wait, Retry-After included
_sleep = time.sleep                # tests replace it
_last_ask: dict[str, float] = {}   # host -> when this process last asked it


def paced(transport=None):
    """`transport` (researchlib.http_transport unless injected) asking each
    host at most once per PACE seconds; an answer of HTTP 429 or 503 is asked
    again after its Retry-After, else after BACKOFF doubling, each wait capped
    at MAX_WAIT, TRIES times in all (keyless_stock's rules)."""
    def get(url: str) -> dict:
        base = transport or datasheet_extract._lib("researchlib").http_transport
        host = urlsplit(url).hostname or ""
        wait = BACKOFF
        for n in range(TRIES):
            gap = PACE - (time.monotonic() - _last_ask.get(host, -math.inf))
            if gap > 0:
                _sleep(gap)
            _last_ask[host] = time.monotonic()
            resp = base(url)
            if int(resp.get("status") or 0) not in \
                    keyless_stock.THROTTLE_CODES or n == TRIES - 1:
                return resp
            after = keyless_stock._retry_after(resp.get("retry_after"))
            _sleep(min(MAX_WAIT, wait if after is None else after))
            wait *= 2
        return resp
    return get


def datasheet_url(rec: dict, stem: str, parts_dir: Path) -> str | None:
    """Where the record's datasheet is: its own `datasheet`, else the
    parts.json line for its file name (an LCSC code) or its MPN."""
    if rec.get("datasheet"):
        return str(rec["datasheet"])
    try:
        lines = json.loads((parts_dir / "parts.json").read_text(
            encoding="utf-8")).get("parts") or []
    except (OSError, ValueError, AttributeError):
        return None
    mpn = datasheet_extract._norm(rec.get("mpn"))
    for key in (lambda ln: str(ln.get("lcsc") or "") == stem,
                lambda ln: mpn and datasheet_extract._norm(ln.get("mpn"))
                == mpn):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("datasheet") and key(ln):
                return str(ln["datasheet"])
    return None


def fetch_for(rec: dict, stem: str, parts_dir: Path, transport=None) -> dict:
    """Fetch the record's datasheet to parts/datasheets/<stem>.pdf through
    datasheet_extract.fetch_datasheet and enter it in that folder's
    index.json as datasheet_extract --fetch does (url, sha256, verdict).
    -> {status: fetched|suspect|not_judged|missing|refused|error|no_url, url,
    why}."""
    url = datasheet_url(rec, stem, parts_dir)
    if not url:
        return {"status": "no_url",
                "why": "no datasheet URL in the record or parts.json"}
    ddir = parts_dir / "datasheets"
    mpn = rec.get("mpn") or None
    r = datasheet_extract.fetch_datasheet(url, ddir / f"{stem}.pdf",
                                          transport=paced(transport), mpn=mpn)
    out = {"status": r["status"], "url": r.get("final_url") or r["url"]}
    if r.get("file"):
        a = r["assessment"]
        idx = datasheet_extract._index(ddir)
        idx[stem] = {
            "mpn": mpn, "file": f"parts/datasheets/{stem}.pdf",
            **{k: r.get(k) for k in ("url", "final_url", "bytes", "sha256")},
            **{k: a[k] for k in datasheet_extract._ASSESSED if k in a},
            **({"assessed": datasheet_extract._stamp(r["sha256"], mpn)}
               if mpn else {})}
        datasheet_extract._write_index(ddir, idx)
        if a["reasons"]:
            out["why"] = "; ".join(a["reasons"])
    else:
        out["why"] = r.get("error") or r["status"]
    return out


def suspect_pdf(pdf: Path, parts_dir: Path) -> str | None:
    """'suspect: why' or 'not judged: why' (nothing to judge it by) if index.json
    judged this datasheet so, else None: nothing is read from such a PDF."""
    ent = datasheet_extract._index(parts_dir / "datasheets").get(pdf.stem)
    if not isinstance(ent, dict) or \
            ent.get("status") not in ("suspect", "not_judged") or \
            Path(str(ent.get("file") or "")).name != pdf.name:
        return None
    return (f"{ent['status'].replace('_', ' ')}: "
            + ("; ".join(ent.get("reasons") or []) or "no reason given"))


def fetched_title(pdf: Path, parts_dir: Path) -> str | None:
    """The citation of a PDF --fetch (or datasheet_extract --fetch) brought:
    the URL it came from and its index.json key."""
    ent = datasheet_extract._index(parts_dir / "datasheets").get(pdf.stem)
    if not isinstance(ent, dict) or not ent.get("url") or \
            Path(str(ent.get("file") or "")).name != pdf.name:
        return None
    return f"{ent.get('final_url') or ent['url']} (index.json {pdf.stem})"


def no_record(parts_dir: Path, files: list[Path]) -> list[dict]:
    """parts.json lines with no <MPN>.json or <LCSC>.json record beside
    them: nothing here to fill, so each is not judged, with that reason."""
    try:
        lines = json.loads((parts_dir / "parts.json").read_text(
            encoding="utf-8")).get("parts") or []
    except (OSError, ValueError, AttributeError):
        return []
    have = {datasheet_extract._norm(f.stem) for f in files}
    for f in files:
        try:
            have.add(datasheet_extract._norm(
                json.loads(f.read_text(encoding="utf-8")).get("mpn")))
        except (OSError, ValueError, AttributeError):
            pass
    out = []
    for ln in lines:
        if not isinstance(ln, dict) or not ln.get("mpn"):
            continue
        keys = {datasheet_extract._norm(ln.get("mpn")),
                datasheet_extract._norm(str(ln.get("lcsc") or ""))} - {""}
        if not keys & have:
            out.append({"mpn": ln["mpn"], "status": "not_judged",
                        "notes": ["no part record (parts/<MPN>.json) to "
                                  "fill: write it, then run this again"]})
    return out


def main(argv: list[str] | None = None, transport=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--parts", required=True, type=Path,
                    help="the board's parts/ directory of <MPN>.json records")
    ap.add_argument("--mpn", action="append", default=[],
                    help="only this record (repeatable)")
    ap.add_argument("--write", action="store_true",
                    help="write the filled records back (default: report)")
    ap.add_argument("--out", type=Path, help="write the report JSON here")
    ap.add_argument("--fetch", action="store_true",
                    help="fetch a datasheet the board lacks (paced, cited "
                         "in parts/datasheets/index.json)")
    a = ap.parse_args(argv)
    if not a.parts.is_dir():
        print(f"rating_fill: no directory {a.parts}", file=sys.stderr)
        return 2
    files = sorted(p for p in a.parts.glob("*.json")
                   if not p.name.endswith((".grounding.json", ".pages.json")))
    if a.mpn:
        files = [p for p in files if p.stem in set(a.mpn)]
    report, cache = [], {}
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or "pinout" not in rec:
            continue
        if contacts(rec) is None:
            if not rec.get("pin_ratings"):
                report.append({"file": f.name, "mpn": rec.get("mpn"),
                               "status": "not_judged", "filled": [],
                               "notes": ["not a two-pin passive or a known "
                                         "connector: its ratings come from "
                                         "its datasheet by hand"]})
            continue
        head = {"file": f.name, "mpn": rec.get("mpn")}
        pdf = pdf_for(rec, a.parts, f.stem)
        if pdf is None and a.fetch:
            head["fetch"] = fetch_for(rec, f.stem, a.parts, transport)
            pdf = pdf_for(rec, a.parts, f.stem)
        if pdf is None:
            why = head.get("fetch", {}).get("why")
            report.append({**head, "status": "not_judged", "filled": [],
                           "notes": [f"no datasheet PDF{f': {why}' if why
                                                        else ''}"]})
            continue
        why = suspect_pdf(pdf, a.parts)
        if why:
            report.append({**head, "status": "not_judged", "filled": [],
                           "pdf": str(pdf),
                           "notes": [f"datasheet {why.replace(':', ', not read:', 1)}"]})
            continue
        if pdf not in cache:
            cache[pdf] = datasheet_extract.extract_pdf_text(pdf)
        rep = fill(rec, cache[pdf], fetched_title(pdf, a.parts))
        new = rep.pop("record")
        rep = {**head, "status": "rated" if rep["filled"] else "not_judged",
               **rep, "pdf": str(pdf)}
        report.append(rep)
        if a.write and new != rec:
            f.write_text(json.dumps(new, indent=1, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    out = {"records": report,
           "filled": sum(1 for r in report if r["filled"])}
    if not a.mpn:
        out["no_record"] = no_record(a.parts, files)
    text = json.dumps(out, indent=1, ensure_ascii=False)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
