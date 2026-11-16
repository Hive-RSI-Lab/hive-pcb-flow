#!/usr/bin/env python
"""abs_max_batch.py - read the absolute-maximum pin ratings of a board set's
ICs from their datasheets, for check_ratings (#1339).

    abs_max_batch.py                 the training set (errata_corpus boards:)
    abs_max_batch.py --round N       holdout round N, unsealed
    abs_max_batch.py --sealed N      holdout round N, sealed (scorers only)

Which parts: every IC (refdes U, IC or VR) on either revision of a board in
the set, in the BOM, with three or more pads on a net, an LCSC code, a
manufacturer part number or a part number in Value, and no extraction in hwde's library
(reference/extractions). A part is keyed by its LCSC code, else by its MPN,
else by its Value when that reads as a part number (#815; check_ratings
reads the extraction back under it). An MPN's listing comes from an
exact-MPN search of JLC's catalogue, cached under listings/mpn/; no exact
match, no reading. A part with none of these is left out. So is a board
soldered on as a module (#1521): an MPN whose exact JLC rows are all
listed under Development Boards & Kits ("left out: a board"). A part
keyed by its Value that no JLC row names exactly, and that the maker
resolver below can't place either, is reported as land_batch reports it:
"not one listing for its Value", with the nearest MPN the search found.

For each part: its LCSC listing (cached), the datasheet that listing names
(or, for a part reference/datasheet_urls.yaml names, the maker's datasheet
it gives: the listing names none, or one the fetcher refuses; #815; else,
when the listing names none, the one scripts/lib/dsresolve.py finds by
maker, cached under resolved/, with where it came from in the report's
`datasheet_from` and a named miss in its `resolver`),
fetched as `datasheet_extract.fetch_datasheet` fetches one (a file judged
suspect is not read), the text of its absolute-maximum pages and its
pin-description pages, and one model reading of them (READER_MODEL, the
fixed prompt PROMPT): the pinout of the listing's package and the
absolute-maximum rows that bound pins' voltages, each row with its page, the
line quoted, and whether the reader is sure.

A deterministic check then keeps only what the PDF shows (`verify`):
 - each pin name of the pinout is printed on the page it cites;
 - a row is kept only if the reader is sure, its page is an absolute-maximum
   page (or the one after), its quote is on that page, every number in its
   bounds and every pin it names are in the quote, and those pins are not
   ground; a row about the supply that names no pin ("DC Supply
   Voltage") is the part's one power_in pin, and only when it has one;
 - the pinout agrees with every board the part sits on (check_ratings'
   own pinout_fact: every pad a pin, and named alike where the board names
   them; a pin the reader calls "EP" leaves the pinout for the
   extraction's exposed_pad, so the board's pad one past the last pin
   matches it). Where the board names no pin, the nets must agree instead
   (`nets_agree`): every ground pin on a ground net, at least one, and no
   power input on one.
A part that fails the pinout checks is left out whole. A row that fails is
left out, never guessed: the pin stays unrated.

Output, under <cache>/abs-max/<set>/ (<set> is `training` or `round-N`, in
the cache that holds the set's checkouts):
  ext/<key>.json     the kept reading (<key>: the LCSC code, or the MPN
                     as check_ratings spells a file name), in the library's format
                     (reference/extractions/README.md): pinout, pin_ratings
                     whose `source` cites the datasheet's page and quotes the
                     line, source_pdf with the URL and sha256. errata_corpus
                     passes this folder to check_ratings as --extractions.
  report.json        per part: what was kept and why the rest was not
  pdfs/, listings/, resolved/, reads/   the cache: a part is read again only when its
                     PDF, the prompt or the model changes.
Standard output is counts only, never a part or a board: a scorer can run
this on a holdout round and hand the counts on.

Exit 0 when it ran (parts it could not read are counted, not errors), 2 on a
bad set or cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))

import check_ratings  # noqa: E402
import datasheet_extract  # noqa: E402
import errata_corpus as ec  # noqa: E402
import parts_enrich  # noqa: E402

READER_MODEL = "claude-opus-5-5"
IC_REF_RE = re.compile(r"^(?:U|IC|VR)\d")
MIN_PADS = 3
ABS_KEY = "absolute maximum"
PIN_KEYS = ("pin configuration", "pin description", "pin function",
            "pin assignment", "pinout", "pin out", "terminal function",
            "pin no", "pin name", "pin connection")
MAX_PIN_PAGES = 4
PAGE_CHARS = 7000
WORKERS = 4

PROMPT = """You read one IC datasheet's text and return one JSON object, nothing else.

Part: {mpn} by {brand}. Package on the distributor's listing: {package}.

Return {{"package_found": true or false, "pinout": [...], "ratings": [...]}}.

"pinout": every pin of this exact part in the {package} package, from its pin
configuration table or drawing. Each item: {{"pin": "<pin number as printed>",
"name": "<pin name copied exactly as printed>", "type": one of "power_in",
"power_out", "ground", "input", "output", "bidirectional", "passive",
"analog", "clock", "nc", "open_collector", "tristate", "unspecified",
"page": <page number>}}. An exposed or thermal pad is pin "EP". If the
datasheet covers several packages, give only the {package} one. If no
package matches, or you cannot tell which numbers go with it, set
package_found false and give an empty pinout.

"ratings": the rows of the ABSOLUTE MAXIMUM RATINGS table that limit a pin's
voltage. Not recommended operating conditions, not electrical
characteristics, not current, power, temperature or ESD. Each item:
{{"pins": [pin names from your pinout], "min": number or null,
"max": number or "<PIN>+<number>" or null, "ref": pin name or null,
"page": <page number>, "quote": "<the row copied exactly from the page text,
with its numbers>", "sure": true or false}}. "ref" is the pin the voltage is
measured against when that is not ground ("BST to SW": pins ["BST"], ref
"SW"). A limit set by another pin ("VIN + 0.3 V") is written "VIN+0.3".
Set "sure" false when the row covers a group you would have to expand ("all
other pins", "I/O pins"), when it is unclear which pins it covers, or when
any part of it is a guess. Never write a number the page does not print.

Pages are marked "=== page N ===" (N counts from 1)."""

PROMPT_ID = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]


# --- which parts ---------------------------------------------------------------

def set_boards(man: Path, round_n: int | None, sealed: bool) -> list[dict]:
    """The set's boards: training (`boards:`), or holdout round N."""
    if round_n is None:
        return ec.load(man)
    rounds = ec.sealed_rounds(man)
    out = [b for b in ec._raw(man, "holdout")
           if (b.get("swept") or {}).get("round") == round_n]
    if any(b["id"] in rounds for b in out) != sealed:
        raise SystemExit(f"round {round_n} is "
                         + ("not sealed: use --round" if sealed
                            else "sealed: use --sealed (scorers only)"))
    return out


def wanted_parts(boards: list[dict], cache: Path, work: Path) -> tuple[dict, Counter]:
    """{key: [board part dicts]} for the ICs to read (key: the LCSC code,
    else 'mpn:<MPN>'), and a count of the ICs left out by reason."""
    parts: dict[str, list[dict]] = {}
    skipped: Counter = Counter()
    for b in boards:
        for rev in ("rev_a", "rev_b"):
            if rev not in b:
                continue
            repo = ec.repo_dir(cache, b, rev)
            if not repo.is_dir():
                skipped["board not fetched"] += 1
                continue
            empty = work / b["id"] / "constraints.json"
            empty.parent.mkdir(parents=True, exist_ok=True)
            empty.write_text("{}", encoding="utf-8")
            try:
                pcb = ec._pcb(b, rev, repo, empty)
                board = check_ratings.load_parts_on_board(pcb)
            except Exception:  # noqa: BLE001 - a board it can't read is counted
                skipped["board unreadable"] += 1
                continue
            for p in board:
                if not (p["in_bom"] and IC_REF_RE.match(p["ref"])
                        and len(p["pads"]) >= MIN_PADS):
                    continue
                key = part_key(p)
                if key is None:
                    skipped["no part number"] += 1
                elif (check_ratings.LIBRARY_DIR / f"{ext_name(key)}.json").is_file():
                    skipped["in the library already"] += 1
                else:
                    parts.setdefault(key, []).append(p)
    return parts, skipped


def part_key(p: dict) -> str | None:
    """A part's key: its LCSC code (C<digits>), else 'mpn:<MPN>', else
    'mpn:<Value>' when the Value reads as a part number (#815: many boards
    carry an IC's number only there; check_ratings.key_by_mpn reads the
    extraction back under it), else None."""
    lc = (p.get("lcsc") or "").strip().upper()
    if re.fullmatch(r"C\d+", lc):
        return lc
    mpn = (p.get("mpn") or "").strip() or check_ratings.value_mpn(p)
    return f"mpn:{mpn}" if mpn and check_ratings._safe_name(mpn) else None


def keyed_by_value(p: dict) -> bool:
    """Whether part_key took the part's number from its Value field."""
    lc = (p.get("lcsc") or "").strip().upper()
    return not (re.fullmatch(r"C\d+", lc) or (p.get("mpn") or "").strip()) \
        and bool(check_ratings.value_mpn(p))


def ext_name(key: str) -> str:
    """The extraction's file stem, as check_ratings looks it up: the LCSC
    code, or the MPN with characters other than letters, digits, '.', '_'
    and '-' made '-' (key_by_mpn's second spelling)."""
    if not key.startswith("mpn:"):
        return key
    return re.sub(r"[^A-Za-z0-9._-]", "-", key[4:])


def find_listing(key: str, out: Path) -> dict | None:
    """The part's LCSC listing. An LCSC key: that code's listing (the shared
    listing cache first). An MPN key: a JLC search for the MPN, keeping only
    rows whose MPN is exactly it (case and spaces aside) and that name a
    datasheet; the first such row, or None. Cached under listings/."""
    if not key.startswith("mpn:"):
        shared = parts_enrich.cache_dir() / f"{key}.json"
        if shared.is_file():
            return json.loads(shared.read_text(encoding="utf-8")) or None
        return parts_enrich.listing(key, out / "listings")
    from mpn_decode import norm
    mpn = key[4:]
    f = out / "listings" / "mpn" / f"{ext_name(key)}.json"
    if f.is_file():
        return json.loads(f.read_text(encoding="utf-8")) or None
    partslib = datasheet_extract._lib("partslib")
    rows, _ = partslib.live_search(mpn, page_size=25)
    if not rows and not partslib.endpoint_reachable():
        raise partslib.PartsError("JLC parts search unreachable")
    hits = [h for h in map(partslib.normalize, rows)
            if norm(h.get("mpn")) == norm(mpn) and h.get("datasheet")]
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(hits[0] if hits else {}, ensure_ascii=False),
                 encoding="utf-8")
    return hits[0] if hits else None


# JLC's category for a development board or kit (#1521): a board soldered
# on as a module (ABX00027, an Arduino Nano Every), not an IC with pins to
# rate or number.
BOARD_CATEGORY = "Development Boards & Kits"


def search_rows(mpn: str, out: Path) -> list[dict]:
    """Every row of a JLC search for the MPN, normalized, cached under
    listings/value-hits/ (land_batch.value_listing reads the same file)."""
    f = out / "listings" / "value-hits" / f"{ext_name('mpn:' + mpn)}.json"
    if f.is_file():
        return json.loads(f.read_text(encoding="utf-8"))
    partslib = datasheet_extract._lib("partslib")
    got, _ = partslib.live_search(mpn, page_size=25)
    if not got and not partslib.endpoint_reachable():
        raise partslib.PartsError("JLC parts search unreachable")
    hits = [partslib.normalize(h) for h in got]
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(hits, ensure_ascii=False), encoding="utf-8")
    return hits


def nearest(value: str, hits: list[dict]) -> dict | None:
    """The search row whose MPN shares the longest start with the Value
    (case and spaces aside), at least half of it and four characters; the
    shorter MPN, then the lower LCSC code, on a tie. None when none does."""
    from mpn_decode import norm
    v = norm(value)

    def common(m: str) -> int:
        n = 0
        while n < min(len(m), len(v)) and m[n] == v[n]:
            n += 1
        return n
    best = sorted(((common(norm(h.get("mpn"))), h) for h in hits
                   if h.get("mpn")),
                  key=lambda t: (-t[0], len(t[1]["mpn"]),
                                 len(t[1].get("lcsc") or ""),
                                 t[1].get("lcsc") or ""))
    if best and best[0][0] >= max(4, (len(v) + 1) // 2):
        return best[0][1]
    return None


def no_exact_why(value: str, hits: list[dict]) -> str:
    """Why a Value has no listing (#1444): no row is exactly it, and the
    nearest MPN the search found, if one is near."""
    near = nearest(value, hits)
    return ("no JLC listing has exactly that MPN; nearest: "
            f"{near['mpn']} ({near.get('lcsc') or 'no LCSC code'}, "
            f"{near.get('package') or 'no package'})" if near else
            "no JLC listing has exactly that MPN, nor one near it")


def a_board(mpn: str, hits: list[dict]) -> str | None:
    """Why the part is a board, not an IC (#1521): every row whose MPN is
    exactly it (case and spaces aside) is listed under BOARD_CATEGORY, and
    there is one. None otherwise: a board's row beside a chip's, or near
    rows only (RP2040-Zero is not RP2040), leave the part an IC."""
    from mpn_decode import norm
    exact = [h for h in hits if norm(h.get("mpn")) == norm(mpn)]
    if not exact or any(h.get("category") != BOARD_CATEGORY for h in exact):
        return None
    return (f"its JLC listing ({', '.join(h.get('lcsc') or '?' for h in exact)}, "
            f"{exact[0].get('brand') or 'no brand'}) is under {BOARD_CATEGORY}")


DATASHEET_URLS = HERE.parent / "reference" / "datasheet_urls.yaml"


def maker_datasheet(mpn: str | None, table: Path | None = None) -> dict | None:
    """The entry of reference/datasheet_urls.yaml that names this MPN (case
    and spaces aside), or None: the maker's datasheet for a part whose
    listing names none, or one the fetcher refuses (#815)."""
    from mpn_decode import norm
    if not (mpn or "").strip():
        return None
    import yaml
    doc = yaml.safe_load((table or DATASHEET_URLS).read_text(encoding="utf-8"))
    for e in (doc or {}).get("parts") or []:
        if norm(mpn) in {norm(m) for m in e.get("mpns") or []}:
            return e
    return None


def resolved(mpn: str, brand: str | None, out: Path, name: str,
             resolve=None) -> dict:
    """dsresolve.resolve for a part whose listing names no datasheet, cached
    under resolved/ (keyed by the resolver's version, its maker table, the
    MPN and the brand). A found PDF lands where read_part reads it, with
    its fetch record beside it. A miss is cached too, unless a host stalled
    (it may answer next time)."""
    import dsresolve
    if not (mpn or "").strip():
        return {"miss": "maker not known", "why": "no MPN to go by"}
    key = hashlib.sha256(json.dumps(
        [dsresolve.VERSION, dsresolve.table_sha(), mpn, brand or ""]
    ).encode()).hexdigest()
    f = out / "resolved" / f"{name}.json"
    pdf = out / "pdfs" / f"{name}.pdf"
    if f.is_file():
        c = json.loads(f.read_text(encoding="utf-8"))
        if c.get("key") == key and (c["found"].get("miss") or pdf.is_file()):
            return c["found"]
    found = (resolve or dsresolve.resolve)(mpn, brand, pdf)
    if found.get("url"):
        _write(out / "pdfs" / f"{name}.json", found["got"])
    if found.get("url") or not any(
            t["kind"] in ("stalled", "skipped") for t in found.get("tried") or []):
        _write(f, {"key": key, "found": found})
    return found


def _tried(found: dict) -> str:
    return "; ".join(f"{t['url']}: {t['kind']} {t.get('why') or ''}".strip()
                     for t in found.get("tried") or [])


# --- one part ------------------------------------------------------------------

def _norm(s: str) -> str:
    """Text for matching: no whitespace, one kind of minus, case-folded."""
    s = re.sub(r"[−‐-―﹣－]", "-", s or "")
    return re.sub(r"\s+", "", s).casefold()


def pick_pages(pages: list[dict]) -> tuple[list[int], set[int]]:
    """(0-based pages to send, 1-based absolute-maximum pages a row may
    cite). The absolute-maximum pages and the one after each (a table runs
    on), and up to MAX_PIN_PAGES pin-description pages."""
    low = [p.get("text", "").lower() for p in pages]
    am = [i for i, t in enumerate(low) if ABS_KEY in t]
    abs_ok = {i for a in am for i in (a, a + 1) if i < len(pages)}
    pin = [i for i, t in enumerate(low) if any(k in t for k in PIN_KEYS)
           and i not in abs_ok][:MAX_PIN_PAGES]
    return sorted(abs_ok | set(pin)), {i + 1 for i in abs_ok}


def _numbers(text: str) -> set[float]:
    return {float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)}


def _bound_ok(v, names: set[str], quote: str) -> tuple[bool, str]:
    """A min or max: a number printed in the quote, or '<PIN>+<n>' with the
    pin in the pinout and both in the quote."""
    if v is None:
        return True, ""
    if isinstance(v, bool):
        return False, "bound is not a number"
    if isinstance(v, (int, float)):
        return (abs(float(v)) in _numbers(quote),
                f"{v} is not in the quote")
    m = re.fullmatch(r"\s*([A-Za-z][A-Za-z0-9_]*)\s*([+-])\s*(\d+(?:\.\d+)?)\s*",
                     str(v))
    if not m:
        return False, f"bound {v!r} is neither a number nor PIN+n"
    if m.group(1).upper() not in names:
        return False, f"bound {v!r} names no pin of the pinout"
    if _norm(m.group(1)) not in _norm(quote):
        return False, f"bound {v!r}: {m.group(1)} is not in the quote"
    return (float(m.group(3)) in _numbers(quote),
            f"bound {v!r}: {m.group(3)} is not in the quote")


def verify(reading: dict, pages: list[dict], abs_pages: set[int],
           sent: set[int]) -> tuple[list[dict], list[dict], list[str]]:
    """(pinout, kept rating rows, why rows/pinout were dropped). An empty
    pinout means the part is left out whole."""
    why: list[str] = []
    text = {p["page"] + 1: _norm(p.get("text", "")) for p in pages}
    pinout = reading.get("pinout") or []
    if not reading.get("package_found") or not pinout:
        return [], [], ["the reader found no pinout for the listed package"]
    seen = set()
    for p in pinout:
        pin, name = str(p.get("pin") or ""), str(p.get("name") or "")
        if not pin or not name or pin in seen:
            return [], [], [f"pinout entry {p!r} is empty or repeated"]
        seen.add(pin)
        if p.get("type") not in datasheet_extract.PIN_TYPES:
            return [], [], [f"pin {pin}: type {p.get('type')!r} is not a pin type"]
        pg = p.get("page")
        if pg not in sent or _norm(name) not in text.get(pg, ""):
            return [], [], [f"pin {pin} {name!r} is not on page {pg}"]
    out_pins = [{"pin": str(p["pin"]), "name": str(p["name"]),
                 "type": p["type"]} for p in pinout]
    names = {p["name"].upper(): p for p in out_pins}
    supply = [p["name"] for p in out_pins if p["type"] == "power_in"]
    rows = []
    for r in reading.get("ratings") or []:
        q, pg = str(r.get("quote") or ""), r.get("page")
        pins = [str(x) for x in r.get("pins") or []]
        bad = None
        if r.get("sure") is not True:
            bad = "the reader was not sure"
        elif pg not in abs_pages:
            bad = f"page {pg} is not an absolute-maximum page"
        elif len(_norm(q)) < 4 or _norm(q) not in text.get(pg, ""):
            bad = f"the quote is not on page {pg}"
        elif not pins:
            bad = "no pins"
        elif r.get("min") is None and r.get("max") is None:
            bad = "no bound"
        else:
            for n in pins + ([r["ref"]] if r.get("ref") else []):
                if n.upper() not in names:
                    bad = f"pin {n!r} is not in the pinout"
                elif _norm(n) not in _norm(q) and not (
                        pins == [n] and supply == [n]
                        and "supply" in q.lower()):
                    # a supply row may name no pin ("DC Supply Voltage"):
                    # it is the part's one power input, or it is left out
                    bad = f"pin {n!r} is not named in the quote"
                if bad:
                    break
            if not bad and any(names[n.upper()]["type"] == "ground"
                               for n in pins):
                bad = "rates a ground pin"
            for k in ("min", "max"):
                ok, msg = _bound_ok(r.get(k), set(names), q)
                if not bad and not ok:
                    bad = msg
        if bad:
            why.append(f"row {pins} p.{pg}: {bad}")
            continue
        row = {"pins": pins, "kind": "voltage", "level": "abs_max"}
        for k in ("min", "max"):
            if r.get(k) is not None:
                row[k] = r[k]
        if r.get("ref"):
            row["ref"] = str(r["ref"])
        row["page"], row["quote"] = pg, " ".join(q.split())
        rows.append(row)
    return out_pins, rows, why


def split_exposed_pad(pinout: list[dict], rows: list[dict]
                      ) -> tuple[list[dict], list[dict], dict | None]:
    """(pinout, rows, exposed_pad). The reader names an exposed pad pin "EP"
    (PROMPT); boards number it one past the last pin. check_ratings'
    pinout_fact takes that pad when the extraction says it has an exposed
    pad, so the pad leaves the pinout for the library's exposed_pad field,
    and a row that names a pin no longer in the pinout goes with it."""
    ep = [p for p in pinout if p["pin"].upper() == "EP"]
    if not ep:
        return pinout, rows, None
    rest = [p for p in pinout if p["pin"].upper() != "EP"]
    left = {p["name"].upper() for p in rest}
    rows = [r for r in rows if all(
        n.upper() in left for n in r["pins"] + ([r["ref"]] if r.get("ref") else []))]
    return rest, rows, {"present": True, "connect_to": ep[0]["name"]}


def nets_agree(b: dict, pinout: list[dict], why: str) -> tuple[str, str]:
    """When the board names a pin unlike the datasheet (MISO / DO), the
    pinout stands if the nets agree with it: every ground pin on the board's
    ground, at least one of them, and every power input off it."""
    gnd = [p["pin"] for p in pinout if p["type"] == "ground"
           and p["pin"] in b["pads"]]
    if not gnd:
        return "unknown", why + "; no ground pin to check the nets by"
    off = [p["pin"] for p in pinout if p["pin"] in b["pads"] and (
        (p["type"] == "ground") != check_ratings.is_ground(b["pads"][p["pin"]]))
        and p["type"] in ("ground", "power_in")]
    if off:
        return "unknown", why + f"; pins {', '.join(off[:6])} on the wrong net"
    return "verified", "names differ, ground and supply nets agree"


def ask_reader(prompt: str, timeout: int = 900) -> dict:
    """One reading: claude -p with no tools, no session, no settings."""
    p = subprocess.run(
        ["claude", "-p", "--model", READER_MODEL, "--output-format", "json",
         "--tools", "", "--no-session-persistence", "--setting-sources", ""],
        input=prompt, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"reader exit {p.returncode}: {p.stderr[-300:]}")
    res = json.loads(p.stdout).get("result") or ""
    m = re.search(r"\{.*\}", res, re.S)
    if not m:
        raise RuntimeError("reader gave no JSON object")
    return json.loads(m.group(0))


def read_part(key: str, on_boards: list[dict], out: Path,
              reader=ask_reader) -> dict:
    """One part from listing to extraction; its report line."""
    rep: dict = {"key": key, "boards": len(on_boards)}
    name = ext_name(key)
    try:
        hit, err = find_listing(key, out), None
    except Exception as exc:  # noqa: BLE001 - counted, not fatal
        hit, err = None, exc
    hit = dict(hit or {})
    mpn = key[4:] if key.startswith("mpn:") else hit.get("mpn") or ""
    maker = maker_datasheet(mpn) or maker_datasheet(hit.get("mpn"))
    if maker:
        hit.update(datasheet=maker["url"], brand=maker["brand"],
                   package=maker["package"])
        rep["datasheet_from"] = "reference/datasheet_urls.yaml"
    elif err is not None:
        return {**rep, "status": "no listing", "why": str(err)[:200]}
    if not hit.get("datasheet"):
        from mpn_decode import norm
        try:
            rows = search_rows(mpn, out) if key.startswith("mpn:") else None
        except Exception:  # noqa: BLE001 - no search, no relabel: as before
            rows = None
        board = a_board(mpn, rows or [])
        if board:
            return {**rep, "status": "left out: a board", "why": board}
        found = resolved(mpn, hit.get("brand"), out, name)
        if not found.get("url"):
            # a maker site that stalled twice may have had it: not judged (#1480)
            if found.get("miss") == "stalled":
                return {**rep, "status": "datasheet site stalled, not judged",
                        "resolver": "stalled",
                        "why": str(found.get("why") or _tried(found))[:300]}
            if rows is not None and all(keyed_by_value(p) for p in on_boards) \
                    and not any(norm(h.get("mpn")) == norm(mpn) for h in rows):
                # named as land_batch names it (#1521): no row is the
                # Value, so no maker could be known from one; nothing read
                return {**rep, "keyed_by": "value",
                        "status": "not one listing for its Value",
                        "resolver": found.get("miss"),
                        "why": no_exact_why(mpn, rows)}
            return {**rep, "status": "no listing with a datasheet URL",
                    "resolver": found.get("miss"),
                    "why": str(found.get("why") or _tried(found))[:300]}
        hit.update(datasheet=found["url"],
                   brand=hit.get("brand") or found["maker"])
        rep["datasheet_from"] = found["via"]
    lcsc = hit.get("lcsc") or ""
    pkg = hit.get("package") or ""
    rep.update(mpn=mpn, lcsc=lcsc, package=pkg)
    pdf = out / "pdfs" / f"{name}.pdf"
    meta = out / "pdfs" / f"{name}.json"
    fix = datasheet_extract._lib("partslib").fix_datasheet_url
    if not (pdf.is_file() and meta.is_file()) or json.loads(
            meta.read_text(encoding="utf-8")).get("url") != fix(hit["datasheet"]):
        # not fetched yet, or fetched from another URL (a table entry added
        # since, or the listing's URL changed): fetch the one wanted now
        pdf.unlink(missing_ok=True)
        got = datasheet_extract.fetch_datasheet(hit["datasheet"], pdf, mpn=mpn)
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps(got, indent=1), encoding="utf-8")
    got = json.loads(meta.read_text(encoding="utf-8"))
    if got.get("status") != "fetched":
        return {**rep, "status": f"datasheet {got.get('status')}",
                "why": str(got.get("error") or
                           (got.get("assessment") or {}).get("reasons"))[:300]}
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    pages = datasheet_extract.extract_pdf_text(pdf)
    send, abs_pages = pick_pages(pages)
    if not abs_pages:
        return {**rep, "status": "no absolute-maximum page in the text"}
    body = "\n".join(f"=== page {i + 1} ===\n{pages[i].get('text', '')[:PAGE_CHARS]}"
                     for i in send)
    prompt = PROMPT.format(mpn=mpn, brand=hit.get("brand") or "?",
                           package=pkg or "?") + "\n\n" + body
    key = hashlib.sha256(f"{sha}\0{READER_MODEL}\0{prompt}".encode()).hexdigest()
    cached = out / "reads" / f"{name}.json"
    reading = None
    if cached.is_file():
        c = json.loads(cached.read_text(encoding="utf-8"))
        reading = c.get("reading") if c.get("key") == key else None
    if reading is None:
        try:
            reading = reader(prompt)
        except Exception as exc:  # noqa: BLE001 - counted, not fatal
            return {**rep, "status": "reader failed", "why": str(exc)[:300]}
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({"key": key, "model": READER_MODEL,
                                      "prompt": PROMPT_ID, "reading": reading},
                                     indent=1), encoding="utf-8")
    pinout, rows, why = verify(reading, pages, abs_pages,
                               {i + 1 for i in send})
    rep.update(rows_read=len(reading.get("ratings") or []), dropped=why)
    if not pinout:
        return {**rep, "status": "pinout not confirmed"}
    pinout, rows, ep = split_exposed_pad(pinout, rows)
    ext = {"mpn": mpn, **({"lcsc": lcsc} if lcsc else {}), "package": pkg,
           "pinout": pinout, **({"exposed_pad": ep} if ep else {})}
    for b in on_boards:
        st, val = check_ratings.pinout_fact(b, ext)
        if st == "unknown":
            st, val = nets_agree(b, pinout, val)
        if st != "verified":
            return {**rep, "status": "pinout disagrees with a board",
                    "why": val[:300]}
    if not rows:
        return {**rep, "status": "no row kept"}
    doc = f"{hit.get('brand') or ''} {mpn} datasheet".strip()
    ext["description"] = (f"absolute-maximum pin ratings read by "
                          f"abs_max_batch.py ({READER_MODEL}, prompt "
                          f"{PROMPT_ID}) and checked against the PDF text")
    ext["pin_ratings"] = [
        {**{k: v for k, v in r.items() if k not in ("page", "quote")},
         "source": f"{doc} p.{r['page']} Absolute Maximum Ratings: "
                   f"'{r['quote']}'"} for r in rows]
    ext["source_pdf"] = (f"{doc}, {len(pages)} pages; {got.get('final_url') or got['url']} "
                         f"sha256 {sha}")
    ok, code = datasheet_extract.do_validate(argparse.Namespace(
        validate=str(_write(out / "ext" / f"{name}.json", ext))))
    if code:
        (out / "ext" / f"{name}.json").unlink()
        return {**rep, "status": "schema", "why": json.dumps(ok)[:300]}
    return {**rep, "status": "kept", "rows_kept": len(rows)}


def _write(f: Path, data: dict) -> Path:
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    return f


# --- main ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--round", type=int, metavar="N",
                   help="holdout round N (unsealed)")
    g.add_argument("--sealed", type=int, metavar="N",
                   help="sealed holdout round N (scorers only)")
    ap.add_argument("--cache")
    ap.add_argument("--sealed-cache")
    ap.add_argument("--manifest", default=str(ec.MANIFEST))
    ap.add_argument("--limit", type=int, help="read at most N parts (a pilot)")
    a = ap.parse_args(argv)
    cache = ec.cache_root(a.cache)
    man = Path(a.manifest)
    ec.guard_training_cache(cache, ec.sealed_rounds(man))
    rnd = a.sealed if a.sealed is not None else a.round
    if a.sealed is not None:
        cache = ec.sealed_cache_root(a.sealed_cache, cache)
    boards = set_boards(man, rnd, a.sealed is not None)
    if not boards:
        print("no boards in that set", file=sys.stderr)
        return 2
    out = cache / "abs-max" / (f"round-{rnd}" if rnd else "training")
    parts, skipped = wanted_parts(boards, cache, out / "work")
    todo = sorted(parts)[:a.limit] if a.limit else sorted(parts)
    with ThreadPoolExecutor(WORKERS) as pool:
        reps = list(pool.map(lambda c: read_part(c, parts[c], out), todo))
    keep = {ext_name(r["key"]) for r in reps if r["status"] == "kept"}
    names = {ext_name(k) for k in todo}
    for f in (out / "ext").glob("*.json") if (out / "ext").is_dir() else ():
        if f.stem not in keep and f.stem in names:
            f.unlink()      # a part whose reading no longer passes
    by_value = sorted({ext_name(k) for k, ps in parts.items()
                       if any(keyed_by_value(p) for p in ps)})
    _write(out / "report.json", {"model": READER_MODEL, "prompt": PROMPT_ID,
                                 "skipped": dict(skipped),
                                 "keyed_by_value": by_value, "parts": reps})
    st = Counter(r["status"] for r in reps)
    print(f"boards {len(boards)}; ICs to read {len(parts)} (read {len(todo)}); "
          "left out: " + (", ".join(f"{k} {v}" for k, v in sorted(skipped.items()))
                          or "none"))
    print(f"keyed by their Value field: {len(by_value)}")
    print("parts: " + ", ".join(f"{k} {v}" for k, v in st.most_common()))
    print(f"rows kept {sum(r.get('rows_kept', 0) for r in reps)}, rows read "
          f"{sum(r.get('rows_read', 0) for r in reps)}")
    print(f"written: {out}/ext (per part: {out}/report.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
