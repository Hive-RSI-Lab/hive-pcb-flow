#!/usr/bin/env python3
"""listing_survey - how often does a distributor's listed voltage rating disagree
with the datasheet, by part class (#799)? Its answer decides whether the `listed`
rating tier may stay non-blocking.

Walks every `parts/parts.json` under the roots and compares each part that has an
extraction beside it, with check_ratings.listing_comparisons. --sample adds the
parts someone read by hand, one row per part (tests/fixtures/ratings/
listing_sample.json); an extraction wins for a part in both. A part on several
boards counts once. --corpus adds a datasheet corpus laid out as the errata
corpus is (ext/<MPN>.json extractions, listings/<LCSC>.json LCSC listings): a
listing joins the extraction that names its LCSC code, else its MPN. Give it
the training split only, never a held-out one.

Many parts can share one catalogue PDF, so the table gives parts and distinct
datasheets: the rate is per part, but its evidence is only as wide as the second
count. Only `high` lets a real over-voltage pass a `listed` check. The bound is
the 95 % upper limit on the high rate (Wilson), which is what a zero is worth.

Writes nothing but may judge a PDF on disk (suspect_datasheets), so give it a copy.
    bin/hwde listing_survey ROOT [ROOT...] [--sample FILE] [--corpus DIR] [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import check_ratings  # noqa: E402
import datasheet_extract  # noqa: E402

CLASSES = {"R": "resistor", "RN": "resistor", "C": "capacitor", "L": "inductor",
           "FB": "inductor", "D": "diode", "LED": "diode", "TVS": "diode",
           "Q": "transistor", "J": "connector", "P": "connector",
           "CN": "connector", "U": "ic", "IC": "ic", "VR": "ic", "F": "fuse"}


# for a row with no reference designator: what LCSC's description says it is
DESCRIBED = (("connector", r"connector|header|terminal|socket|\bwire.to"),
             ("transistor", r"mosfet|\bfet\b|transistor"),
             ("diode", r"diode|tvs|esd|schottky|rectifier|led\b"),
             ("capacitor", r"capacitor|\bmlcc\b"),
             ("resistor", r"resistor"), ("inductor", r"inductor|ferrite"),
             ("fuse", r"\bfuse"))


def part_class(part: dict) -> str:
    """A part's class from its first reference designator's letters, else from
    the parts.json hint for one, else from its description. Anything left with
    a voltage rating and no words for it is `other`."""
    refs = part.get("refdes") or part.get("refs") or part.get("ref_prefix_hint") or []
    ref = refs[0] if isinstance(refs, list) and refs else str(refs)
    m = re.match(r"[A-Za-z]+", str(ref))
    known = CLASSES.get(m.group(0).upper()) if m else None
    if known:
        return known
    text = str(part.get("description") or "")
    for name, rx in DESCRIBED:
        if re.search(rx, text, re.I):
            return name
    return "ic" if re.search(r"regulator|converter|amplifier|controller|sensor|"
                             r"\bic\b|switch|expander|management|module|comparator",
                             text, re.I) else "other"


def wilson_upper(hits: int, n: int, z: float = 1.96) -> float:
    """95 % upper limit on a proportion from hits in n."""
    if not n:
        return 1.0
    p = hits / n
    den = 1 + z * z / n
    return min(1.0, (p + z * z / (2 * n) + z * math.sqrt(
        p * (1 - p) / n + z * z / (4 * n * n))) / den)


def _worst(comps: list[dict]) -> dict:
    """The comparison a part counts as: a high disagreement over a low one over
    an agreement, the same order the finding uses."""
    return min(comps, key=lambda c: (c["agree"], c["side"] != "high"))


def extracted_rows(roots: list[Path]) -> tuple[list[dict], list[dict]]:
    """(one row per compared part, one per part that could not be compared)."""
    rows: dict[str, dict] = {}
    skipped: dict[str, dict] = {}
    for root in roots:
        for pj in sorted(root.rglob("parts/parts.json")):
            try:
                data = json.loads(pj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            parts = data.get("parts", []) if isinstance(data, dict) else data
            for part in parts:
                lcsc = str(part.get("lcsc") or "").strip()
                f = pj.parent / f"{lcsc}.json"
                if not lcsc or lcsc in rows or not f.is_file() or \
                        not check_ratings.listing_ratings(part):
                    continue
                try:
                    ext = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                pdf = check_ratings.extraction_pdf(ext, lcsc)
                base = {"lcsc": lcsc, "mpn": part.get("mpn"), "board": pj.parent.parent.name,
                        "class": part_class(part)}
                if datasheet_extract.suspect_datasheets(pj.parent, {pdf}):
                    skipped[lcsc] = {**base, "why": "suspect datasheet"}
                    continue
                comps = check_ratings.listing_comparisons(part, ext)
                if not comps:
                    skipped[lcsc] = {**base, "why": "no voltage row in the extraction"}
                    continue
                c = _worst(comps)
                skipped.pop(lcsc, None)
                rows[lcsc] = {**base, "how": "extraction", "rating": c["rating"],
                              "listing_v": c["listing_v"], "datasheet_v": c["datasheet_v"],
                              "row": c["datasheet_row"], "agree": c["agree"],
                              "side": c["side"], "datasheet": pdf}
    return list(rows.values()), list(skipped.values())


SHA_RE = re.compile(r"sha256\s+([0-9a-f]{12})")


def _mpn_key(mpn) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(mpn or "").upper())


def corpus_rows(corpus: Path) -> tuple[list[dict], list[dict]]:
    """(compared, not compared) for a corpus dir: each listings/<LCSC>.json
    against the ext/*.json that names its LCSC code, else its MPN. A listing
    with no extraction is left out, as a board part with none is. The PDF a
    row counts against is the source_pdf's sha256 when it gives one."""
    exts: dict[str, dict] = {}
    for f in sorted((corpus / "ext").glob("*.json")):
        try:
            ext = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for k in (str(ext.get("lcsc") or "").strip(), _mpn_key(ext.get("mpn")),
                  _mpn_key(f.stem)):
            if k:
                exts.setdefault(k, ext)
    rows, skipped = [], []
    for f in sorted((corpus / "listings").glob("*.json")):
        try:
            part = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        lcsc = str(part.get("lcsc") or f.stem).strip()
        ext = exts.get(lcsc) or exts.get(_mpn_key(part.get("mpn")))
        if not ext or not check_ratings.listing_ratings(part):
            continue
        sha = SHA_RE.search(str(ext.get("source_pdf") or ""))
        base = {"lcsc": lcsc, "mpn": part.get("mpn"), "board": corpus.name,
                "class": part_class(part)}
        comps = check_ratings.listing_comparisons(part, ext)
        if not comps:
            skipped.append({**base, "why": "no voltage row in the extraction"})
            continue
        c = _worst(comps)
        rows.append({**base, "how": "extraction", "rating": c["rating"],
                     "listing_v": c["listing_v"], "datasheet_v": c["datasheet_v"],
                     "row": c["datasheet_row"], "agree": c["agree"], "side": c["side"],
                     "datasheet": sha.group(1) if sha else lcsc})
    return rows, skipped


def sample_rows(path: Path, tol: float = check_ratings.LISTING_TOL) -> list[dict]:
    """Hand-read rows, judged by the same tolerance as the finding."""
    out = []
    for r in json.loads(path.read_text(encoding="utf-8"))["parts"]:
        agree = abs(r["listing_v"] - r["datasheet_v"]) <= tol * r["datasheet_v"]
        out.append({**r, "how": "read", "agree": agree, "side": None if agree else
                    "high" if r["listing_v"] > r["datasheet_v"] else "low"})
    return out


def table(rows: list[dict]) -> list[dict]:
    """One row per class, most-parts first, and a final `all`."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["class"], []).append(r)
    out = []
    for cls, rs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])) + \
            [("all", rows)]:
        high = sum(r["side"] == "high" for r in rs)
        low = sum(r["side"] == "low" for r in rs)
        out.append({"class": cls, "parts": len(rs),
                    "datasheets": len({r["datasheet"] for r in rs}),
                    "high": high, "low": low,
                    "rate": (high + low) / len(rs) if rs else 0.0,
                    "high_rate": high / len(rs) if rs else 0.0,
                    "high_upper95": wilson_upper(high, len(rs))})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("roots", nargs="*", type=Path)
    ap.add_argument("--sample", type=Path, help="hand-read parts, JSON")
    ap.add_argument("--corpus", type=Path, action="append", default=[],
                    help="a corpus dir with ext/ and listings/ (training only)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rows, skipped = extracted_rows(a.roots)
    for corpus in a.corpus:
        have = {r["lcsc"] for r in rows}
        more, miss = corpus_rows(corpus)
        rows += [r for r in more if r["lcsc"] not in have]
        skipped += [s for s in miss if s["lcsc"] not in have]
    if a.sample:
        have = {r["lcsc"] for r in rows}
        rows += [r for r in sample_rows(a.sample) if r["lcsc"] not in have]
        skipped = [s for s in skipped if s["lcsc"] not in {r["lcsc"] for r in rows}]
    res = {"table": table(rows), "disagree": [r for r in rows if not r["agree"]],
           "not_compared": skipped, "parts": rows}
    if a.json:
        print(json.dumps(res, indent=1))
        return 0
    print(f"{'class':<12}{'parts':>6}{'PDFs':>6}{'high':>6}{'low':>5}{'rate':>7}"
          f"{'high≤':>8}")
    for t in res["table"]:
        print(f"{t['class']:<12}{t['parts']:>6}{t['datasheets']:>6}{t['high']:>6}"
              f"{t['low']:>5}{t['rate']:>7.1%}{t['high_upper95']:>8.1%}")
    for r in res["disagree"]:
        print(f"{r['side']:<5}{r['lcsc']} {r['mpn']} ({r['class']}): listing "
              f"{r['listing_v']:g} V, datasheet {r['datasheet_v']:g} V ({r['row']})")
    print(f"not compared: {len(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
