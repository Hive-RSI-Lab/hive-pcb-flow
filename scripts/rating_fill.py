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

Usage:
  rating_fill.py --parts DIR [--mpn MPN ...] [--write] [--out report.json]
Exit: 0 (report printed; --write writes the records), 2 on bad input.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import datasheet_extract  # noqa: E402

TAG = "rating_fill:"
DEG = r"\s*(?:℃|°\s*C|ºC|C\b)"
RANGE_RE = re.compile(r"(-\d+)\s*(?:℃|°\s*C)?\s*to\s*\+?(\d+)" + DEG)

# --- helpers -----------------------------------------------------------------


def pdf_for(rec: dict, parts_dir: Path) -> Path | None:
    """The record's datasheet: the first .pdf path in source_pdf (relative to
    the board root or the parts dir), else parts/datasheets/<MPN>.pdf."""
    m = re.match(r"\s*(\S+?\.pdf)\b", str(rec.get("source_pdf") or ""), re.I)
    cands = []
    if m:
        cands += [parts_dir.parent / m.group(1), parts_dir / m.group(1)]
    mpn = str(rec.get("mpn") or "")
    cands.append(parts_dir / "datasheets" / f"{mpn}.pdf")
    # the sanitized names check_ratings also looks under ("#" -> "-")
    for rx in (r"[^A-Za-z0-9._-]", r"[^A-Za-z0-9_-]"):
        cands.append(parts_dir / "datasheets" / f"{re.sub(rx, '-', mpn)}.pdf")
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
        v = re.search(r"Voltage\s+rating:\s*(\d+(?:\.\d+)?)\s*V.{0,2}AC/DC", t)
        if v and not any(r["kind"] == "voltage" for r in out["rows"]):
            out["rows"].append({
                "kind": "voltage", "max": float(v.group(1)),
                "source": f"{where}: Voltage rating {v.group(1)} V AC/DC "
                          "between contacts"})
        c = re.search(r"Current\s+rating:\s*(\d+(?:\.\d+)?)\s*A.{0,2}AC/DC"
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


def fill(rec: dict, pages: list[dict]) -> dict:
    """-> {"record": new record, "filled": [...], "notes": [...]}; the record
    is unchanged when nothing reads."""
    mpn = str(rec.get("mpn") or "")
    pins = contacts(rec)
    rep = {"mpn": mpn, "filled": [], "notes": [], "record": rec}
    if pins is None:
        rep["notes"].append("not a two-pin passive or a known connector: "
                            "skipped")
        return rep
    title = _title(rec)
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--parts", required=True, type=Path,
                    help="the board's parts/ directory of <MPN>.json records")
    ap.add_argument("--mpn", action="append", default=[],
                    help="only this record (repeatable)")
    ap.add_argument("--write", action="store_true",
                    help="write the filled records back (default: report)")
    ap.add_argument("--out", type=Path, help="write the report JSON here")
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
            continue
        pdf = pdf_for(rec, a.parts)
        if pdf is None:
            report.append({"file": f.name, "mpn": rec.get("mpn"),
                           "filled": [], "notes": ["no datasheet PDF"]})
            continue
        if pdf not in cache:
            cache[pdf] = datasheet_extract.extract_pdf_text(pdf)
        rep = fill(rec, cache[pdf])
        new = rep.pop("record")
        rep["file"] = f.name
        rep["pdf"] = str(pdf)
        report.append(rep)
        if a.write and new != rec:
            f.write_text(json.dumps(new, indent=1, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    text = json.dumps({"records": report,
                       "filled": sum(1 for r in report if r["filled"])},
                      indent=1, ensure_ascii=False)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
