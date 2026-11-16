#!/usr/bin/env python
"""land_batch.py - read the pad numbering of a board set's ICs from their
datasheets' pin drawings, for fp_verify's board mode (#1288).

    land_batch.py                 the training set (errata_corpus boards:)
    land_batch.py --round N       holdout round N, unsealed
    land_batch.py --sealed N      holdout round N, sealed (scorers only)

Which parts: every IC (refdes U, IC or VR) on either revision of a board in
the set, in the BOM, with three or more pads on a net, and an LCSC code, a
manufacturer part number, or else a Value that reads as a part number
(check_ratings.value_mpn), whose hwde record does not already state its pad
places or numbering direction (land_pattern.pads or numbering in
reference/extractions). The listing and the datasheet are the ones
abs_max_batch.py fetches, in the same cache (abs-max/<set>/listings, pdfs),
the maker's datasheet too where reference/datasheet_urls.yaml names the
part, and the one abs_max_batch's resolver (scripts/lib/dsresolve.py) finds
by maker when the listing names none, cached under resolved/ with where it
came from in `datasheet_from`: a part is fetched once for both.

A part keyed only by its Value (#1406) is read only when one source names
it unambiguously (`value_listing`): exactly one entry of
reference/datasheet_urls.yaml, or else JLC listings whose MPN is exactly
the Value, all of one package (the lowest LCSC code's datasheet is read).
Listings in several packages are narrowed to the one package every board's
footprint fits, as lib/pkgname reads both names (#1444: AMS1117-3.3 on a
SOT-223 footprint takes the SOT-223 listing, not the SOT-89 one; SOT-23-6
and SOT-23-6L are one package), and report.json says so in
`package_from`. A footprint fitting none or more than one, or a name
pkgname can't read, leaves it out. No exact listing leaves it out too, and
the reason names the nearest MPN the search found (W25Q128JVS: nearest
W25Q128JVSIQ), which is not read: its numbering stays not_judged.
Anything else leaves it out with the reason. fp_verify then finds its
record by the Value on one exact match only.

For each part: the datasheet's pin-drawing pages (a page whose text says
"top view" or "bottom view", those that also name the pins first) and its
package-dimension pages (the last pages that name a package outline,
dimensions or a land pattern), each sent as its text and as a PNG
(pdftoppm at DPI), and one model reading of them (READER_MODEL, the fixed
prompt PROMPT): which view the pin drawing is, the pin numbers along each
side of the drawn body in one direction round it, the pinout, and the pitch
and row spans of the land pattern with the line they are printed on.

A deterministic check then keeps only what the PDF shows (`verify`):
 - the reader is sure, the drawing's page was sent and its text says the
   view it reports ("top view" / "bottom view");
 - the pin numbers round the body are each pin once, exactly 1..N, the
   numeric pins of the pinout, and run in order round it: up from pin 1 on
   a top view, down on a bottom view (a drawing seen from below is the
   mirror image). Anything else leaves the part out whole;
 - the pinout passes abs_max_batch's pinout check (each pin's name printed
   on the page it cites) and agrees with every board the part sits on
   (check_ratings' pinout_fact, else the nets: `amb.nets_agree`).
Then, only when the dimension page's text prints the pitch and each span
the drawing needs, and the pins of each side fit between the rows across
it, the pads' places are written (land_pattern.pads, top view, a bottom
view mirrored), and fp_verify fits them; otherwise only the direction is
(land_pattern.numbering ccw_top), and fp_verify judges that. A part that
fails is left out and counted, never guessed: its numbering stays
not_judged.

Output, under <cache>/land/<set>/ (<set> is `training` or `round-N`, in the
cache that holds the set's checkouts):
  ext/<key>.json     the kept reading (<key>: the LCSC code, or the MPN as
                     abs_max_batch spells a file name), in the library's
                     format (reference/extractions/README.md): pinout,
                     land_pattern with pads_source citing the drawing's and
                     the dimensions' pages, source_pdf with the URL and
                     sha256. errata_corpus passes this folder to fp_verify
                     as --parts.
  report.json        per part: what was kept and why the rest was not
  img/, reads/       the cache: a part is read again only when its PDF, the
                     prompt, the resolution or the model changes.
Standard output is counts only, never a part or a board: a scorer can run
this on a holdout round and hand the counts on.

The reader, its prompt and the checks are frozen with the training run
before any holdout round is read (#1288): a change after that comes from
training only, and a holdout part it fails on is reported not_judged.

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

import abs_max_batch as amb  # noqa: E402
import check_ratings  # noqa: E402
import datasheet_extract  # noqa: E402
import errata_corpus as ec  # noqa: E402
import fp_verify  # noqa: E402

READER_MODEL = amb.READER_MODEL
DPI = 110
IC_REF_RE = amb.IC_REF_RE
MIN_PADS = amb.MIN_PADS
VIEW_KEYS = ("top view", "bottom view")
PIN_KEYS = amb.PIN_KEYS
DIMS_KEYS = ("package outline", "package dimension", "mechanical",
             "land pattern", "recommended land", "recommended footprint",
             "footprint", "package information", "outline dimension",
             "pcb layout")
MAX_VIEW_PAGES = 4
MAX_DIMS_PAGES = 4
PAGE_CHARS = amb.PAGE_CHARS
WORKERS = amb.WORKERS
SIDES = ("left", "bottom", "right", "top")

PROMPT = """You read one IC datasheet's pin drawing and package drawing and return one JSON object, nothing else.

Part: {mpn} by {brand}. Package on the distributor's listing: {package}.

The pages are images in the current directory: {images}. Open each with the
Read tool and look at the drawings. Their text follows below, marked
"=== page N ===" (N counts from 1). p-NN.png is page NN of the PDF. Every
page number you return (view_page, dims_page, each pin's page) is that
number N, as in the file name and the "=== page N ===" mark, never the
image's place in the list above.

Return {{"package_found": true or false, "sure": true or false,
"view": "top" or "bottom", "view_page": <page number>, "view_quote": "...",
"sides": {{"left": [...], "bottom": [...], "right": [...], "top": [...]}},
"pinout": [...], "pitch_mm": number or null,
"span_mm": {{"x": number or null, "y": number or null}},
"dims_page": <page number> or null, "dims_quote": "..."}}.

"view", "view_page", "view_quote": the pin configuration drawing of this
exact part in the {package} package: the page it is on, whether its label
says TOP VIEW or BOTTOM VIEW, and that label copied as printed.

"sides": the pin numbers printed along each side of the drawn body, exactly
as the drawing shows them, going once round the body counter-clockwise as
drawn: "left" from top to bottom, "bottom" from left to right, "right" from
bottom to top, "top" from right to left. Each number as a string. A place
along a side where the drawing shows no pin is null. A side with no pins is
[]. Leave an exposed or thermal pad out of the sides.

"pinout": every pin of this exact part in the {package} package. Each item:
{{"pin": "<pin number as printed>", "name": "<pin name copied exactly as
printed>", "type": one of "power_in", "power_out", "ground", "input",
"output", "bidirectional", "passive", "analog", "clock", "nc",
"open_collector", "tristate", "unspecified", "page": <page number>}}. An
exposed or thermal pad is pin "EP", even where the datasheet numbers it.

"pitch_mm", "span_mm", "dims_page", "dims_quote": from the package's
dimension or recommended land pattern drawing of the {package} package: the
pin pitch, and the distance between the centres of the left and right rows
of pads (x) and of the top and bottom rows (y), as the page prints them
(nominal value). Null for any one the page does not print as a single
number; never compute one. "dims_quote": the line or labels they are printed
on.

If the datasheet covers several packages, use only the {package} one. If no
drawing matches it, or you cannot read which number is where, set
package_found false. Set "sure" false when the view label is missing or
unclear, or when any part of the answer is a guess."""
PROMPT_ID = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]


# --- which parts ---------------------------------------------------------------

def _states_numbering(rec: Path) -> bool:
    try:
        doc = json.loads(rec.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    land = (doc.get("land_pattern") or {}) if isinstance(doc, dict) else {}
    return bool(land.get("pads") or land.get("numbering"))


def wanted_parts(boards: list[dict], cache: Path, work: Path) -> tuple[dict, Counter]:
    """{key: [board part dicts]} for the ICs to read, and a count of the ICs
    left out by reason."""
    lib = fp_verify._record_index([fp_verify.EXTRACTIONS])
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
                key = amb.part_key(p)
                if key is None:
                    skipped["no part number"] += 1
                    continue
                names = {amb.ext_name(key).upper(),
                         (p.get("mpn") or "").strip().upper()} - {""}
                rec = next((lib[n] for n in names if n in lib), None)
                if rec is not None and _states_numbering(rec):
                    skipped["numbering in the library already"] += 1
                else:
                    parts.setdefault(key, []).append(p)
    return parts, skipped


def value_listing(value: str, store: Path, table: Path | None = None,
                  footprints: list[tuple[str, int]] = ()
                  ) -> tuple[dict | None, str]:
    """(listing, note) for a part keyed only by its Value (#1406) when one
    source names it unambiguously, else (None, why). The maker table first:
    exactly one entry naming the Value (read_part then reads that entry's
    datasheet), two or more is ambiguous. Else the JLC rows whose MPN is
    exactly the Value (case and spaces aside) and that name a datasheet:
    they must all give one package, and the lowest LCSC code's is kept.
    Rows in several packages are narrowed by the boards' footprints
    (`footprints`: each copy's footprint name and numbered pad count) to
    the one package all of them fit (`by_footprint`), and the note says so.
    No exact row: the why names the nearest MPN the search found
    (`nearest`), and the part stays unread. The search's rows are cached
    under listings/value-hits/."""
    import yaml
    from mpn_decode import norm
    doc = yaml.safe_load((table or amb.DATASHEET_URLS).read_text(encoding="utf-8"))
    ents = [e for e in (doc or {}).get("parts") or []
            if norm(value) in {norm(m) for m in e.get("mpns") or []}]
    if len(ents) > 1:
        return None, f"reference/datasheet_urls.yaml names it in {len(ents)} entries"
    if ents:
        return {"mpn": value}, ""
    f = store / "listings" / "value-hits" / f"{amb.ext_name('mpn:' + value)}.json"
    if f.is_file():
        hits = json.loads(f.read_text(encoding="utf-8"))
    else:
        partslib = datasheet_extract._lib("partslib")
        got, _ = partslib.live_search(value, page_size=25)
        if not got and not partslib.endpoint_reachable():
            raise partslib.PartsError("JLC parts search unreachable")
        hits = [partslib.normalize(h) for h in got]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(hits, ensure_ascii=False), encoding="utf-8")
    exact = [h for h in hits if norm(h.get("mpn")) == norm(value)]
    rows = sorted((h for h in exact if h.get("datasheet")),
                  key=lambda h: (len(h.get("lcsc") or ""), h.get("lcsc") or ""))
    if not rows:
        if exact:
            return None, (f"{len(exact)} JLC listings have exactly that MPN, "
                          "none with a datasheet")
        near = nearest(value, hits)
        return None, ("no JLC listing has exactly that MPN; nearest: "
                      f"{near['mpn']} ({near.get('lcsc') or 'no LCSC code'}, "
                      f"{near.get('package') or 'no package'})" if near else
                      "no JLC listing has exactly that MPN, nor one near it")
    pkgs = sorted({norm(r.get("package") or "") for r in rows})
    if len(pkgs) == 1:
        return rows[0], ""
    kept, why = by_footprint(rows, footprints)
    if not kept:
        return None, (f"{len(rows)} JLC listings have exactly that MPN, in "
                      f"{len(pkgs)} packages; {why}")
    return kept[0], (f"{kept[0].get('package')}, of {len(pkgs)} packages "
                     "listed, the one its footprint fits")


def by_footprint(rows: list[dict], footprints: list[tuple[str, int]]
                 ) -> tuple[list[dict], str]:
    """The rows (in their order) of the one package every footprint fits,
    else ([], why). Packages are told apart by what lib/pkgname reads from
    their names (SOT-23-6 and SOT-23-6L are one package), and a footprint
    fits a package when pkgname.compare finds nothing they disagree on.
    Any package name or footprint name it can't read, no footprint, none
    fitting or more than one: no pick."""
    import pkgname
    if not footprints:
        return [], "no footprint to choose by"
    foots = []
    for name, pads in footprints:
        d = pkgname.describe(name.split(":")[-1])
        if not (d["mount"] or d["family"]):
            return [], f"its footprint {name or '(none)'} is not read"
        foots.append((d, pads))
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        d = pkgname.describe(r.get("package") or "")
        if not (d["family"] or d["chip"]):
            return [], f"the package {r.get('package') or '(none)'} is not read"
        groups.setdefault(tuple(sorted(d.items())), []).append(r)
    fit = [g for k, g in groups.items()
           if all(not pkgname.compare(dict(k), d, pads) for d, pads in foots)]

    def names(gs) -> str:
        return ", ".join(sorted({r.get("package") or "" for g in gs for r in g}))
    if len(fit) == 1:
        return fit[0], ""
    if not fit:
        return [], f"its footprint fits none of them ({names(groups.values())})"
    return [], f"its footprint fits more than one ({names(fit)})"


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


# --- pages -----------------------------------------------------------------------

def pick_pages(pages: list[dict]) -> tuple[list[int], list[int], int]:
    """(0-based pin-drawing pages, 0-based dimension pages, how many pages
    say both "top view" and "bottom view"). Drawing pages: those that say a
    view and name the pins first, then those that say a view, up to
    MAX_VIEW_PAGES. Dimension pages: the last MAX_DIMS_PAGES pages naming a
    package outline, dimensions or a land pattern (they sit at the back)."""
    low = [p.get("text", "").lower() for p in pages]
    view = [i for i, t in enumerate(low) if any(k in t for k in VIEW_KEYS)]
    both = sum(1 for t in low if all(k in t for k in VIEW_KEYS))
    pin = [i for i in view if any(k in low[i] for k in PIN_KEYS)]
    views = (pin + [i for i in view if i not in pin])[:MAX_VIEW_PAGES]
    dims = [i for i, t in enumerate(low) if any(k in t for k in DIMS_KEYS)
            and i not in views][-MAX_DIMS_PAGES:]
    return sorted(views), dims, both


def render(pdf: Path, pages: list[int], dest: Path) -> list[str]:
    """Each 0-based page as dest/p-NN.png (NN 1-based), DPI dots per inch;
    the file names, made once."""
    dest.mkdir(parents=True, exist_ok=True)
    names = []
    for i in pages:
        f = dest / f"p-{i + 1:02d}.png"
        if not f.is_file():
            subprocess.run(["pdftoppm", "-r", str(DPI), "-png", "-singlefile",
                            "-f", str(i + 1), "-l", str(i + 1), str(pdf),
                            str(f.with_suffix(""))],
                           check=True, capture_output=True, timeout=120)
        names.append(f.name)
    return names


# --- the check -------------------------------------------------------------------

def _walk(sides: dict) -> list[str | None]:
    return [None if x is None else str(x).strip()
            for s in SIDES for x in (sides.get(s) or [])]


def check_order(reading: dict, pages: list[dict], sent: set[int]
                ) -> tuple[dict | None, str]:
    """({pin: name...} numeric pins in walk order, ...) or (None, why): the
    view and the walk round the body, as the module docstring says."""
    if not reading.get("package_found"):
        return None, "the reader found no drawing for the listed package"
    if reading.get("sure") is not True:
        return None, "the reader was not sure"
    view, pg = reading.get("view"), reading.get("view_page")
    if view not in ("top", "bottom"):
        return None, f"view {view!r} is neither top nor bottom"
    if pg not in sent:
        return None, f"view page {pg} was not sent"
    text = {p["page"] + 1: amb._norm(p.get("text", "")) for p in pages}
    if amb._norm(f"{view} view") not in text.get(pg, ""):
        return None, f"page {pg} does not say '{view} view'"
    sides = reading.get("sides")
    if not isinstance(sides, dict) or any(
            not isinstance(sides.get(s) or [], list) for s in SIDES):
        return None, "sides is not four lists"
    walk = [x for x in _walk(sides) if x is not None]
    if not walk or not all(x.isdigit() for x in walk):
        return None, "a pin round the body is not a number"
    n = len(walk)
    if len(set(walk)) != n or {int(x) for x in walk} != set(range(1, n + 1)):
        return None, f"the pins round the body are not 1..{n}, each once"
    nums = {str(p.get("pin")) for p in reading.get("pinout") or []
            if str(p.get("pin")).isdigit()}
    if nums != set(walk):
        return None, "the pins round the body are not the pinout's numbered pins"
    seq = [int(x) for x in walk]
    if view == "bottom":
        seq = seq[::-1]
    k = seq.index(1)
    if seq[k:] + seq[:k] != list(range(1, n + 1)):
        return None, (f"the pins do not run in order round a {view} view "
                      f"({'up' if view == 'top' else 'down'} the "
                      "counter-clockwise walk)")
    return {"view": view, "page": pg, "n": n}, ""


def place_pads(reading: dict, pages: list[dict], sent: set[int]
               ) -> tuple[list[dict] | None, str]:
    """The pads' top-view places from the sides, the pitch and the spans,
    or (None, why) when the dimension page does not print them."""
    sides = {s: [None if x is None else str(x).strip()
                 for x in (reading["sides"].get(s) or [])] for s in SIDES}
    p = reading.get("pitch_mm")
    span = reading.get("span_mm") or {}
    sx, sy = span.get("x"), span.get("y")
    dpg = reading.get("dims_page")
    if dpg is None:
        return None, "no dimension page given"
    if dpg not in sent:
        return None, f"dimension page {dpg} was not sent"
    nums = amb._numbers(next((q.get("text", "") for q in pages
                              if q["page"] + 1 == dpg), ""))
    need = [("pitch", p)]
    if sides["left"] or sides["right"]:
        need.append(("span x", sx))
    if sides["top"] or sides["bottom"]:
        need.append(("span y", sy))
    for what, v in need:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            return None, f"no {what} given"
        if float(v) not in nums:
            return None, f"{what} {v} is not printed on page {dpg}"
    long_lr = max(len(sides["left"]), len(sides["right"]))
    long_tb = max(len(sides["top"]), len(sides["bottom"]))
    if long_lr and long_tb and ((long_lr - 1) * p >= sy or (long_tb - 1) * p >= sx):
        return None, "a side's pins do not fit between the rows across it"
    at: dict[str, tuple[float, float]] = {}
    for s, pins in sides.items():
        m = len(pins)
        for i, pin in enumerate(pins):
            if pin is None:
                continue
            off = -(m - 1) / 2 * p + i * p
            at[pin] = {"left": (-sx / 2 if sx else 0.0, off),
                       "bottom": (off, sy / 2 if sy else 0.0),
                       "right": (sx / 2 if sx else 0.0, -off),
                       "top": (-off, -sy / 2 if sy else 0.0)}[s]
    flip = -1 if reading["view"] == "bottom" else 1
    return [{"number": k, "at_mm": [round(flip * x, 4) + 0.0, round(y, 4) + 0.0]}
            for k, (x, y) in sorted(at.items(), key=lambda kv: int(kv[0]))], ""


# --- one part ------------------------------------------------------------------

def ask_reader(prompt: str, cwd: Path, timeout: int = 900) -> dict:
    """One reading: claude -p with only Read, in the page images' folder, no
    session, no settings."""
    p = subprocess.run(
        ["claude", "-p", "--model", READER_MODEL, "--output-format", "json",
         "--tools", "Read", "--allowedTools", "Read",
         "--no-session-persistence", "--setting-sources", ""],
        input=prompt, capture_output=True, text=True, timeout=timeout,
        cwd=str(cwd))
    if p.returncode:
        raise RuntimeError(f"reader exit {p.returncode}: {p.stderr[-300:]}")
    res = json.loads(p.stdout).get("result") or ""
    m = re.search(r"\{.*\}", res, re.S)
    if not m:
        raise RuntimeError("reader gave no JSON object")
    return json.loads(m.group(0))


def read_part(key: str, on_boards: list[dict], out: Path, store: Path,
              reader=ask_reader) -> dict:
    """One part from listing to record; its report line. `store` is
    abs_max_batch's folder for the set (listings and PDFs shared)."""
    rep: dict = {"key": key, "boards": len(on_boards)}
    name = amb.ext_name(key)
    by_value = all(amb.keyed_by_value(p) for p in on_boards)
    if by_value:
        rep["keyed_by"] = "value"
    try:
        if by_value:
            hit, vwhy = value_listing(
                key[4:], store, footprints=[
                    (p.get("footprint") or "", len(p.get("nums") or p["pads"]))
                    for p in on_boards])
            if hit is None:
                return {**rep, "status": "not one listing for its Value", "why": vwhy}
            if vwhy:
                rep["package_from"] = vwhy
        else:
            hit = amb.find_listing(key, store)
        err = None
    except Exception as exc:  # noqa: BLE001 - counted, not fatal
        hit, err = None, exc
    hit = dict(hit or {})
    mpn = key[4:] if key.startswith("mpn:") else hit.get("mpn") or ""
    # the maker's datasheet when reference/datasheet_urls.yaml names the
    # part, as abs_max_batch does (#815), so both read the same PDF
    maker = amb.maker_datasheet(mpn) or amb.maker_datasheet(hit.get("mpn"))
    if maker:
        hit.update(datasheet=maker["url"], brand=maker["brand"],
                   package=maker["package"])
        rep["datasheet_from"] = "reference/datasheet_urls.yaml"
    elif err is not None:
        return {**rep, "status": "no listing", "why": str(err)[:200]}
    if not hit.get("datasheet"):
        # abs_max_batch's resolver and its cache (#815, #1406): one lookup
        # by maker serves both batches
        found = amb.resolved(mpn, hit.get("brand"), store, name)
        if not found.get("url"):
            return {**rep, "status": "no listing with a datasheet URL",
                    "resolver": found.get("miss"),
                    "why": str(found.get("why") or amb._tried(found))[:300]}
        hit.update(datasheet=found["url"],
                   brand=hit.get("brand") or found["maker"])
        rep["datasheet_from"] = found["via"]
    lcsc = hit.get("lcsc") or ""
    pkg = hit.get("package") or ""
    rep.update(mpn=mpn, lcsc=lcsc, package=pkg)
    pdf = store / "pdfs" / f"{name}.pdf"
    meta = store / "pdfs" / f"{name}.json"
    fix = datasheet_extract._lib("partslib").fix_datasheet_url
    if not (pdf.is_file() and meta.is_file()) or json.loads(
            meta.read_text(encoding="utf-8")).get("url") != fix(hit["datasheet"]):
        # not fetched yet, or from another URL than the one wanted now
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
    views, dims, both = pick_pages(pages)
    rep["pages_with_both_views"] = both
    if not views:
        return {**rep, "status": "no top or bottom view in the text"}
    send = sorted(set(views) | set(dims))
    img = out / "img" / name
    try:
        images = render(pdf, send, img)
    except Exception as exc:  # noqa: BLE001 - counted, not fatal
        return {**rep, "status": "pages not rendered", "why": str(exc)[:200]}
    body = "\n".join(f"=== page {i + 1} ===\n{pages[i].get('text', '')[:PAGE_CHARS]}"
                     for i in send)
    prompt = PROMPT.format(mpn=mpn, brand=hit.get("brand") or "?",
                           package=pkg or "?",
                           images=", ".join(images)) + "\n\n" + body
    ck = hashlib.sha256(f"{sha}\0{READER_MODEL}\0{DPI}\0{prompt}".encode()).hexdigest()
    cached = out / "reads" / f"{name}.json"
    reading = None
    if cached.is_file():
        c = json.loads(cached.read_text(encoding="utf-8"))
        reading = c.get("reading") if c.get("key") == ck else None
    if reading is None:
        try:
            reading = reader(prompt, img)
        except Exception as exc:  # noqa: BLE001 - counted, not fatal
            return {**rep, "status": "reader failed", "why": str(exc)[:300]}
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({"key": ck, "model": READER_MODEL,
                                      "prompt": PROMPT_ID, "dpi": DPI,
                                      "reading": reading},
                                     indent=1), encoding="utf-8")
    sent = {i + 1 for i in send}
    order, why = check_order(reading, pages, sent)
    if order is None:
        return {**rep, "status": "numbering not confirmed", "why": why}
    pinout, _, pwhy = amb.verify({"package_found": True, "ratings": [],
                                  "pinout": reading.get("pinout")},
                                 pages, set(), sent)
    if not pinout:
        return {**rep, "status": "pinout not confirmed",
                "why": "; ".join(pwhy)[:300]}
    ep = any(p["pin"].upper() == "EP" for p in pinout)
    pinout = [p for p in pinout if p["pin"].upper() != "EP"]
    ext = {"mpn": mpn, **({"lcsc": lcsc} if lcsc else {}), "package": pkg,
           "pinout": pinout, **({"exposed_pad": {"present": True}} if ep else {})}
    for b in on_boards:
        st, val = check_ratings.pinout_fact(b, ext)
        if st == "unknown":
            st, val = amb.nets_agree(b, pinout, val)
        if st != "verified":
            return {**rep, "status": "pinout disagrees with a board",
                    "why": val[:300]}
    doc = f"{hit.get('brand') or ''} {mpn} datasheet".strip()
    pads, dwhy = place_pads(reading, pages, sent)
    land = {"package": pkg, "pad_count": order["n"] + (1 if ep else 0),
            "pin1": "1"}
    if pads:
        land["pads"] = pads
        land["pads_source"] = (f"{doc} p.{order['page']} {order['view']} view; "
                               f"dimensions p.{reading['dims_page']}")
        land["pitch_mm"] = float(reading["pitch_mm"])
    else:
        land["numbering"] = "ccw_top"
        land["pads_source"] = f"{doc} p.{order['page']} {order['view']} view"
        rep["places_left_out"] = dwhy
    ext["land_pattern"] = land
    ext["description"] = (f"pad numbering read by land_batch.py "
                          f"({READER_MODEL}, prompt {PROMPT_ID}, {DPI} dpi) "
                          f"and checked against the PDF text")
    ext["source_pdf"] = (f"{doc}, {len(pages)} pages; {got.get('final_url') or got['url']} "
                         f"sha256 {sha}")
    f = amb._write(out / "ext" / f"{name}.json", ext)
    ok, code = datasheet_extract.do_validate(argparse.Namespace(validate=str(f)))
    if code:
        f.unlink()
        return {**rep, "status": "schema", "why": json.dumps(ok)[:300]}
    return {**rep, "status": "kept", "kept": "pads" if pads else "ccw_top"}


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
    boards = amb.set_boards(man, rnd, a.sealed is not None)
    if not boards:
        print("no boards in that set", file=sys.stderr)
        return 2
    label = f"round-{rnd}" if rnd else "training"
    out = cache / "land" / label
    store = cache / "abs-max" / label
    parts, skipped = wanted_parts(boards, cache, out / "work")
    todo = sorted(parts)[:a.limit] if a.limit else sorted(parts)
    with ThreadPoolExecutor(WORKERS) as pool:
        reps = list(pool.map(lambda c: read_part(c, parts[c], out, store), todo))
    keep = {amb.ext_name(r["key"]) for r in reps if r["status"] == "kept"}
    names = {amb.ext_name(k) for k in todo}
    for f in (out / "ext").glob("*.json") if (out / "ext").is_dir() else ():
        if f.stem not in keep and f.stem in names:
            f.unlink()      # a part whose reading no longer passes
    amb._write(out / "report.json", {"model": READER_MODEL, "prompt": PROMPT_ID,
                                     "dpi": DPI, "skipped": dict(skipped),
                                     "parts": reps})
    st = Counter(r["status"] for r in reps)
    kept = Counter(r["kept"] for r in reps if r.get("kept"))
    print(f"boards {len(boards)}; ICs to read {len(parts)} (read {len(todo)}); "
          "left out: " + (", ".join(f"{k} {v}" for k, v in sorted(skipped.items()))
                          or "none"))
    print("parts: " + ", ".join(f"{k} {v}" for k, v in st.most_common()))
    print(f"kept with pad places {kept['pads']}, with direction only "
          f"{kept['ccw_top']}; pages saying both views "
          f"{sum(r.get('pages_with_both_views', 0) for r in reps)}")
    print(f"written: {out}/ext (per part: {out}/report.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
