"""dsresolve.py - a datasheet URL for a part whose listing names none.

    resolve(mpn, brand, dest)  ->  {url, via, maker, got, tried}
                               or  {miss, maker, tried}

Goes by maker (reference/datasheet_makers.yaml): the maker is the listing's
brand when there is one, else the family its MPN starts with. Then:

 1. the maker's own URL patterns, for the MPN and for the MPN with its
    ordering suffix cut (`candidates`), each fetched as
    datasheet_extract.fetch_datasheet fetches (the fetch allowlist on every
    hop, a host's block named);
 2. failing those, LCSC's copy of the maker's PDF: a JLC search for the
    part, keeping listings under the maker's brand whose MPN is the part's
    with a suffix, and that name a datasheet.

A listing for a board (the part number and EVM, EVK, KIT ...) is passed
over, and a PDF whose page 1 opens as a user's guide or a board's manual is
refused: TLV320AIC3104EVM's "datasheet" is its user's guide.

A PDF is taken only when the fetcher passes it (match_mpn) and its first
two pages name the part, or the part with its ordering suffix cut
(`candidates`), as a word of its own (`names_part`): AD7245's datasheet
carries "AD724" inside "AD7245", and is not AD724's; TPS620865's carries
"TPS62086", and match_mpn alone takes it for TPS62086RLTR's.

Every result says where its URL came from (`via`), and every miss is named:
"maker not known" (no brand or family in the table; a clone's brand is not
read as its original's maker), "refused" (a PDF came back for another part,
a PDF with nothing to check it by, or a host or the allowlist refused it),
"not found" (no pattern URL is a PDF and LCSC lists no copy), "stalled"
(a host that might have had it timed out, once more after STALL_BACKOFF
seconds, so the part is not judged rather than called not found). `tried`
lists each URL and what came of it.

A stalled miss gets one more way (#1520, `mirror`): when the maker's own host
stalled twice, LCSC's live product page for the part itself, or for the
part on a reel (its maker's packing code alone, land_batch's `PACKING`:
LPS35HW as LPS35HWTR), names the PDF LCSC keeps. It is taken only from
datasheet.lcsc.com, only the one the page gives for that product code,
and fetched and checked like any other; `via` starts MIRROR_VIA so the
part shows where its sheet came from. Another grade, temperature range or
package (AD724 as AD724JRZ) is another part, and is never used.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent):        # lib/ and scripts/: its lazy imports
    if str(_p) not in sys.path:         # (datasheet_extract) under python -I
        sys.path.insert(0, str(_p))
MAKERS = HERE.parent.parent / "reference" / "datasheet_makers.yaml"

# Bump when a change here can change a result: cached results are keyed by it.
# 3: a PDF fetch could not judge (status not_judged: a scan, or no part number
# printed) is its own kind, unjudged.
# 4: a stalled fetch is tried once more after STALL_BACKOFF, and a miss
# with a stall in it is "stalled", not "not found" (#1480).
# 5: a stalled miss tries LCSC's mirror of the exact part or the part on a
# reel, read off its live product page (`mirror`, #1520).
VERSION = 5
FETCH_TIMEOUT = 20.0            # seconds: a maker site that stalls is skipped
STALL_BACKOFF = 15.0            # seconds before the one retry of a stalled fetch
MAX_CUT = 6                     # ordering-suffix characters `candidates` cuts
MIRROR_VIA = "datasheet from LCSC mirror after maker stall"
LCSC_PAGE = "https://www.lcsc.com/product-detail/{code}.html"
# the sheet a product page names: on datasheet.lcsc.com, for that product
# (the page also links its certificates and related products' sheets)
LCSC_SHEET = re.compile(
    r"https://datasheet\.lcsc\.com/datasheet/pdf/[0-9a-f]+\.pdf"
    r"\?productCode=(C\d+)")
MAX_LCSC = 3                    # LCSC copies tried per part

# A listing for a board, not the chip (TLV320AIC3104EVM): its "datasheet" is
# the board's user's guide.
BOARD_SUFFIX = re.compile(r"(EVM|EVK|EVB|EVAL|DEMO|KIT|BOARD)")
# Page 1 of a user's guide or a board's manual, not a datasheet.
NOT_A_SHEET = re.compile(r"USER'?S GUIDE|USER MANUAL|EVALUATION (MODULE|BOARD|KIT)")

_DEAD: dict[str, str] = {}      # host -> why: this process skips it after
_DEAD_LOCK = threading.Lock()   # one timeout (after its retry) or one block
_sleep = time.sleep             # tests stand in for the backoff


def load_makers(path: Path | None = None) -> list[dict]:
    import yaml
    doc = yaml.safe_load((path or MAKERS).read_text(encoding="utf-8")) or {}
    return list(doc.get("makers") or [])


def table_sha(path: Path | None = None) -> str:
    return hashlib.sha256((path or MAKERS).read_bytes()).hexdigest()[:16]


def _brand_has(brand: str, alias: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])",
                     (brand or "").lower()) is not None


def maker_of(mpn: str, brand: str | None, makers: list[dict]
             ) -> tuple[dict | None, str]:
    """(the maker's entry, how it was known), or (None, why not). A listing's
    brand decides when there is one; the MPN's family only when there is no
    brand."""
    brand = (brand or "").strip()
    if brand:
        for m in makers:
            if any(_brand_has(brand, a) for a in m.get("brands") or []):
                return m, f"brand {brand!r}"
        return None, f"brand {brand!r} is no maker in the table"
    up = (mpn or "").strip().upper()
    for m in makers:
        for p in m.get("prefixes") or []:
            if re.match(p, up):
                return m, f"MPN family {p!r}"
    return None, "no brand, and no family in the table starts the MPN"


def candidates(mpn: str) -> list[str]:
    """Part numbers to try, longest first: the MPN up to a ',', '(', '/' or
    space (and up to a lower-case 'x' standing for a grade, "XC6206P33xMR"),
    then the same with up to MAX_CUT trailing characters cut. A cut never
    splits a run of digits nor drops one of more than two (TLV320AIC3104 is
    not cut to TLV320AIC31, another part), and leaves 4 characters or more
    with a digit among them."""
    s = re.split(r"[,(/\s]", (mpn or "").strip())[0]
    m = re.search(r"(?<=[A-Z0-9])x", s)
    if m and s[:m.start()].upper() == s[:m.start()]:
        s = s[:m.start()]
    s = s.upper().rstrip("-_.")
    out = [s] if len(s) >= 4 else []
    for cut in range(1, min(MAX_CUT, len(s) - 1) + 1):
        raw = s[:len(s) - cut]
        if raw and raw[-1].isdigit() and s[len(raw)].isdigit():
            continue
        if any(len(r) > 2 for r in re.findall(r"\d+", s[len(raw):])):
            continue
        c = raw.rstrip("-_.")
        if len(c) >= 4 and re.search(r"\d", c) and c not in out:
            out.append(c)
    return out


def names_part(part: str, texts: list[str]) -> bool:
    """Do these pages name `part` as a word of its own: not inside a longer
    word, and not followed by a digit (AD724 is not in "AD7245")?"""
    p = re.escape(part.upper()).replace(r"\-", "[-‐–]?")
    rx = re.compile(r"(?<![A-Z0-9])" + p + r"(?![0-9])")
    return any(rx.search((t or "").upper()) for t in texts)


def first_pages(pdf: Path, n: int = 2) -> list[str]:
    try:
        from pypdf import PdfReader
        pages = PdfReader(str(pdf)).pages
        return [(pages[i].extract_text() or "") for i in range(min(n, len(pages)))]
    except Exception:  # noqa: BLE001 - an unreadable PDF names nothing
        return []


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _verdict(got: dict) -> tuple[str, str]:
    """(kind, why) for one fetch: ok, wrong (another part's PDF or a bad
    file), unjudged (nothing to judge it by: a scan, or no part number
    printed), blocked (the allowlist or the host refused), absent (no PDF
    there), stalled (the transport failed)."""
    st = got.get("status")
    err = str(got.get("error") or "")
    if st == "fetched":
        return "ok", ""
    if st == "suspect":
        return "wrong", "; ".join((got.get("assessment") or {}).get("reasons") or [])
    if st == "not_judged":
        return "unjudged", "; ".join(
            (got.get("assessment") or {}).get("reasons") or [])
    if st == "refused" and (got.get("blocked_by_host") or "not a PDF" not in err):
        return "blocked", err
    if st == "error" and err.startswith("transport failure"):
        return "stalled", err
    return "absent", err or str(st)


def resolve(mpn: str, brand: str | None, dest: Path, *, makers=None,
            fetch=None, transport=None, search=None, pages=None,
            page=None) -> dict:
    """A datasheet for `mpn` fetched into `dest`; see the module doc.
    fetch/transport/search/pages/page stand in for datasheet_extract's
    fetcher, the HTTP transport, the JLC search, the PDF text and LCSC's
    product page in tests."""
    if makers is None:
        makers = load_makers()
    maker, how = maker_of(mpn, brand, makers)
    if maker is None:
        return {"miss": "maker not known", "why": how, "tried": []}
    if fetch is None:
        import datasheet_extract  # noqa: PLC0415 - scripts/ on the path
        fetch = datasheet_extract.fetch_datasheet
    if transport is None:
        import researchlib  # noqa: PLC0415

        def transport(u):
            return researchlib.http_transport(u, timeout=FETCH_TIMEOUT)
    pages = pages or first_pages
    tried: list[dict] = []
    base = {"maker": maker["name"], "maker_from": how}
    cands = candidates(mpn)
    tmp = dest.with_name(dest.stem + ".try.pdf")

    def attempt(url: str) -> dict | None:
        host = _host(url)
        with _DEAD_LOCK:
            dead = _DEAD.get(host)
        if dead:
            tried.append({"url": url, "kind": "skipped", "why": dead[1],
                          "after": dead[0]})
            return None
        tmp.unlink(missing_ok=True)
        got = fetch(url, tmp, mpn=mpn, transport=transport)
        kind, why = _verdict(got)
        if kind == "stalled":
            # once more after a pause: st.com times out now and then (#1480)
            _sleep(STALL_BACKOFF)
            tmp.unlink(missing_ok=True)
            got = fetch(url, tmp, mpn=mpn, transport=transport)
            kind, why = _verdict(got)
            if kind == "stalled":
                why = f"twice, {STALL_BACKOFF:g} s apart: {why}"
        if kind == "ok":
            texts = pages(tmp)
            head = (texts[0] if texts else "")[:300].upper()
            if not any(names_part(c, texts) for c in cands):
                kind, why = "wrong", (f"pages 1-2 name none of "
                                      f"{', '.join(cands)} as a part")
            elif NOT_A_SHEET.search(head):
                kind, why = "wrong", ("page 1 is a user's guide or a board's "
                                      f"manual ({NOT_A_SHEET.search(head)[0]!r})")
        tried.append({"url": url, "kind": kind, "why": why[:200]})
        if kind == "stalled" or got.get("blocked_by_host"):
            with _DEAD_LOCK:
                _DEAD.setdefault(host, (kind, f"{host} {kind} earlier: {why[:120]}"))
        if kind != "ok":
            tmp.unlink(missing_ok=True)
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
        return {**got, "file": str(dest)}

    for tpl in maker.get("urls") or []:
        for c in cands:
            url = tpl.format(lower=c.lower(), upper=c)
            got = attempt(url)
            if got:
                return {**base, "url": got["url"], "got": got, "tried": tried,
                        "via": f"{maker['name']} URL pattern {tpl} for {c} "
                               f"({maker.get('cite') or 'datasheet_makers.yaml'})"}

    own = list(tried)       # what the maker's own host did
    copies = lcsc_copies(cands, maker, search, tried)
    for c, h in copies[:MAX_LCSC]:
        import partslib  # noqa: PLC0415
        url = partslib.fix_datasheet_url(h["datasheet"])
        got = attempt(url)
        if got:
            return {**base, "url": got["url"], "got": got, "tried": tried,
                    "via": f"LCSC's copy of the {maker['name']} PDF, listed for "
                           f"{h.get('lcsc')} {h.get('mpn')} ({h.get('brand')})"}
    # a skip after a host stalled counts as that stall: the part was not judged
    kinds = {t["kind"] for t in tried} | {
        "stalled" for t in tried if t.get("after") == "stalled"}
    miss = "refused" if kinds & {"wrong", "unjudged", "blocked"} \
        else "stalled" if "stalled" in kinds else "not found"
    if miss == "stalled" and any(t["kind"] == "stalled" or t.get("after")
                                 == "stalled" for t in own):
        for h, url in mirror(mpn, maker, search, page, tried):
            got = attempt(url)
            if got:
                return {**base, "url": got["url"], "got": got, "tried": tried,
                        "via": f"{MIRROR_VIA}: {maker['name']}'s host stalled "
                               f"twice; LCSC's product page for {h.get('lcsc')} "
                               f"{h.get('mpn')} ({h.get('brand')})"}
    return {**base, "miss": miss, "tried": tried}


def same_part(mpn: str, hit: dict) -> str:
    """'exact' when the listing is the part, 'packing' when it is the part
    with its maker's packing code alone (land_batch.PACKING, under that
    maker's brand), else ''. Espressif's fit codes are flash sizes, another
    part, and are not packing here."""
    import land_batch  # noqa: PLC0415 - scripts/ on the path
    from mpn_decode import norm  # noqa: PLC0415
    if norm(hit.get("mpn")) == norm(mpn):
        return "exact"
    code = land_batch.packing_suffix(mpn, hit)
    return "packing" if code and code not in land_batch.ESPRESSIF_FITS else ""


def mirror(mpn: str, maker: dict, search, page, tried: list[dict]
           ) -> list[tuple[dict, str]]:
    """[(listing, PDF URL)]: LCSC listings under the maker's brand that are
    the part or the part on a reel (`same_part`, exact first), each with the
    datasheet.lcsc.com sheet its live product page names for its own
    product code. Anything else the search finds is noted and passed over."""
    import partslib  # noqa: PLC0415
    if search is None:
        def search(q):
            rows, _ = partslib.live_search(q, page_size=25)
            return [partslib.normalize(r) for r in rows]
    if page is None:
        page = lcsc_page
    q = (candidates(mpn) or [mpn])[0]
    try:
        hits = search(q)
    except Exception as exc:  # noqa: BLE001 - a failed search is a note
        tried.append({"url": f"lcsc search {q} (mirror)", "kind": "stalled",
                      "why": f"{type(exc).__name__}: {exc}"[:200]})
        return []
    keep, other = [], set()
    for h in hits or []:
        if not h.get("lcsc") or not any(_brand_has(h.get("brand") or "", a)
                                        for a in maker.get("brands") or []):
            continue
        how = same_part(q, h)
        if how:
            keep.append((how != "exact", h.get("lcsc") or "", h))
        elif str(h.get("mpn") or "").upper().startswith(q):
            other.add(str(h.get("mpn")))
    if other:
        tried.append({"url": f"lcsc search {q} (mirror)", "kind": "skipped",
                      "why": "not the part or the part on a reel: "
                             + ", ".join(sorted(other))[:160]})
    out = []
    for _, code, h in sorted(keep, key=lambda k: k[:2])[:MAX_LCSC]:
        url = LCSC_PAGE.format(code=code)
        try:
            html = page(code)
        except Exception as exc:  # noqa: BLE001 - a failed page is a note
            tried.append({"url": url, "kind": "stalled",
                          "why": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        sheets = [m[0] for m in LCSC_SHEET.finditer(html or "")
                  if m[1] == code]
        if not sheets:
            tried.append({"url": url, "kind": "absent",
                          "why": f"the page names no datasheet.lcsc.com "
                                 f"sheet for {code}"})
            continue
        out.append((h, sheets[0]))
    return out


def lcsc_page(code: str) -> str:
    """LCSC's live product page for `code`, as text."""
    import researchlib  # noqa: PLC0415
    got = researchlib.http_transport(LCSC_PAGE.format(code=code),
                                     timeout=FETCH_TIMEOUT)
    if int(got.get("status") or 0) != 200:
        raise OSError(f"HTTP {got.get('status')}")
    return (got.get("body") or b"").decode("utf-8", "replace")


def lcsc_copies(cands: list[str], maker: dict, search, tried: list[dict]
                ) -> list[tuple[str, dict]]:
    """[(candidate, listing)]: LCSC listings under the maker's brand whose MPN
    is a candidate or a candidate with a suffix (not one more digit: AD7245
    is not AD724), naming a datasheet; the closest MPN first. Searches the
    candidates longest first and stops at the first that finds any."""
    import partslib  # noqa: PLC0415
    from mpn_decode import norm  # noqa: PLC0415
    if search is None:
        def search(q):
            rows, _ = partslib.live_search(q, page_size=25)
            return [partslib.normalize(r) for r in rows]
    for c in cands:
        try:
            hits = search(c)
        except Exception as exc:  # noqa: BLE001 - a failed search is a note
            tried.append({"url": f"lcsc search {c}", "kind": "stalled",
                          "why": f"{type(exc).__name__}: {exc}"[:200]})
            return []
        nc, keep, other = norm(c), [], set()
        for h in hits or []:
            hm = norm(h.get("mpn"))
            if not hm.startswith(nc) or (nc[-1:].isdigit() and hm[len(nc):][:1].isdigit()):
                continue
            if BOARD_SUFFIX.search(hm[len(nc):]):
                other.add(f"{h.get('mpn')} (a board, not the part)")
                continue
            if not any(_brand_has(h.get("brand") or "", a)
                       for a in maker.get("brands") or []):
                other.add(str(h.get("brand")))
                continue
            if h.get("datasheet"):
                keep.append(h)
        if other:
            tried.append({"url": f"lcsc search {c}", "kind": "skipped",
                          "why": "passed over (another brand, or a board): "
                                 + ", ".join(sorted(other))[:160]})
        if keep:
            keep.sort(key=lambda h: (len(norm(h.get("mpn"))), h.get("lcsc") or ""))
            return [(c, h) for h in keep]
    tried.append({"url": f"lcsc search {cands[0] if cands else ''}",
                  "kind": "absent", "why": f"no {maker['name']} listing with "
                                           "a datasheet"})
    return []
