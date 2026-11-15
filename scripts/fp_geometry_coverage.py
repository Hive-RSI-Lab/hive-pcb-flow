"""fp_geometry_coverage.py - how much of a set of boards check_fp_geometry
actually judges: footprints judged against each source (a land_patterns
row, KiCad's stock footprint, the LCSC model, the footprint's own name),
those not judged, by why, and the packages left unjudged most often (the
land_patterns rows worth adding next).

A gate that finds nothing on a set of boards is only evidence if it judged
them: this is the count that says which.

A footprint whose part has an LCSC number but no cached model is not
judged (the gate never fetches): --fill-cache fetches those models into the
shared cache (~/.cache/hwde/easyeda, read by every later run, the gate's
included), paced, waiting out EasyEDA's per-IP rate limit between rounds,
then counts again.

CLI: fp_geometry_coverage.py BOARD.kicad_pcb ... | @LIST
     [--live] [--easyeda-cache DIR] [--top N] [--boards N] [--json OUT]
     [--fill-cache [--pace S] [--wait-min M] [--budget-min B]]
     @LIST is a file of board paths, one per line (or space-separated on one).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
sys.path.insert(0, str(HERE))
import check_fp_geometry as g  # noqa: E402

SOURCES = ("drawing", "stock", "model", "name")
UNCACHED = "its LCSC model is not cached"
_PART = ("MPN", "Manufacturer Part Number", "MFR Part", "Part Number", "PartNumber")


def part_of(fields: dict) -> str:
    for k in _PART:
        if (fields.get(k) or "").strip():
            return fields[k].strip()
    return (fields.get("Value") or "").strip()


def list_file(path: Path) -> list[Path]:
    """Board paths from a file: one per line (a path may hold spaces), or
    space-separated on a single line."""
    lines = [x.strip() for x in path.read_text().splitlines() if x.strip()]
    if len(lines) == 1 and not Path(lines[0]).exists():
        lines = lines[0].split()
    return [Path(x) for x in lines]


def coverage(pcbs: list[Path], live: bool = False, easyeda_cache: Path | None = None,
             stock: g.Stock | None = None, rows: list[dict] | None = None) -> dict:
    stock = stock or g.Stock(g.kicad_footprint_dir())
    rows = g.load_land_patterns() if rows is None else rows
    any_src, first, why = Counter(), Counter(), Counter()
    pkgs: dict[str, dict] = defaultdict(lambda: {"n": 0, "boards": set(), "parts": Counter()})
    boards, errors, n = 0, {}, 0
    per_board, uncached = {}, Counter()
    for pcb in pcbs:
        trace: list = []
        try:
            g.check(pcb, live=live, easyeda_cache=easyeda_cache, stock=stock,
                    rows=rows, trace=trace)
        except Exception as e:  # noqa: BLE001 - one bad board doesn't stop the count
            errors[str(pcb)] = repr(e)[:200]
            continue
        boards += 1
        per_board[str(pcb)] = {"footprints": len(trace),
                               "judged": sum(t["why"] is None for t in trace),
                               "under_two_pads": sum(t["why"] == "under two numbered pads"
                                                     for t in trace)}
        for t in trace:
            n += 1
            for s in set(t["sources"]):
                any_src[s] += 1
            if t.get("model_uncached"):
                # judged by name or not at all, a cached model would judge it better
                uncached[t["lcsc"]] += 1
            if t["why"] is None:
                first[t["sources"][0]] += 1
                continue
            why[t["why"]] += 1
            if t["why"] == "under two numbered pads":
                continue
            p = pkgs[t["fpid"].split(":", 1)[-1]]
            p["n"] += 1
            p["boards"].add(str(pcb))
            p["parts"][part_of(t["fields"])] += 1
    top = sorted(pkgs.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    return {"boards": boards, "footprints": n,
            "judged": sum(first.values()),
            "judged_by_source": {s: any_src[s] for s in SOURCES},
            "judged_first_source": {s: first[s] for s in SOURCES},
            "not_judged": dict(why.most_common()),
            "not_judged_packages": [
                {"package": k, "footprints": v["n"], "boards": len(v["boards"]),
                 "parts": dict(v["parts"].most_common(6))} for k, v in top],
            "per_board": per_board,
            "uncached_lcsc": dict(uncached.most_common()),
            "errors": errors}


def share(b: dict) -> float | None:
    """A board's judged share of the footprints there is anything to judge
    on (two or more numbered pads); None with none."""
    m = b["footprints"] - b["under_two_pads"]
    return b["judged"] / m if m else None


def fill_cache(lcscs: list[str], pace_s: float = 1.0, wait_min: float = 5.0,
               budget_min: float = 120.0, cache: Path | None = None,
               fetcher=None, sleep=time.sleep) -> dict:
    """Fetch the models of `lcscs` into the cache, `pace_s` apart. On a
    403/429 (EasyEDA's per-IP limit), wait `wait_min` and go on with the rest,
    until all are asked or `budget_min` is spent. {fetched, none, left}:
    none = EasyEDA answered with no footprint, left = not asked in time."""
    import easyeda
    t0, todo = time.monotonic(), list(dict.fromkeys(x.strip().upper() for x in lcscs if x))
    got, none = [], []
    while todo:
        easyeda.reset_rate_limit()
        errs: dict = {}
        rest = []
        for i, x in enumerate(todo):
            m = easyeda.get(x, cache, fetch=True, pace_s=pace_s, fetcher=fetcher, errors=errs)
            if m is not None:
                got.append(x)
            elif x not in errs:
                none.append(x)
            elif easyeda.rate_limited():
                rest = todo[i:]
                break
            else:
                none.append(x)  # a network error other than the limit: not retried
        todo = rest
        if todo and (time.monotonic() - t0) / 60 + wait_min > budget_min:
            break
        if todo:
            print(f"fill-cache: {len(got)} fetched, rate limited with {len(todo)} "
                  f"left; waiting {wait_min:g} min", file=sys.stderr, flush=True)
            sleep(wait_min * 60)
    return {"fetched": got, "none": none, "left": todo}


def table(c: dict, top: int = 10, nboards: int = 0) -> str:
    out = [f"{c['boards']} boards, {c['footprints']} footprints, "
           f"{c['judged']} judged ({100 * c['judged'] / max(c['footprints'], 1):.0f} %)",
           "judged against (a footprint may count under several):"]
    out += [f"  {s:8} {c['judged_by_source'][s]}" for s in SOURCES]
    out.append("not judged:")
    out += [f"  {v:6}  {k}" for k, v in c["not_judged"].items()]
    out.append(f"packages not judged most often (top {top}):")
    for p in c["not_judged_packages"][:top]:
        parts = ", ".join(f"{k or '?'} x{v}" for k, v in list(p["parts"].items())[:3])
        out.append(f"  {p['footprints']:5} on {p['boards']:3} boards  {p['package']}  ({parts})")
    sh = sorted((share(b), k) for k, b in c["per_board"].items() if share(b) is not None)
    if sh:
        cuts = [sum(v < x for v, _ in sh) for x in (0.25, 0.5, 0.75)]
        out.append(f"judged share per board (of footprints with 2+ numbered pads): "
                   f"median {sh[len(sh) // 2][0]:.0%}, under 25 % on {cuts[0]}, "
                   f"under 50 % on {cuts[1]}, under 75 % on {cuts[2]} of {len(sh)}")
        for v, k in sh[:nboards]:
            b = c["per_board"][k]
            out.append(f"  {v:4.0%}  {b['judged']:4}/{b['footprints'] - b['under_two_pads']:<4} {k}")
    if c["uncached_lcsc"]:
        out.append(f"{sum(c['uncached_lcsc'].values())} footprints of "
                   f"{len(c['uncached_lcsc'])} LCSC numbers wait on an uncached model")
    if c["errors"]:
        out.append(f"{len(c['errors'])} boards not read: " + "; ".join(
            f"{Path(k).name}: {v}" for k, v in list(c["errors"].items())[:3]))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("boards", nargs="+")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--easyeda-cache", type=Path)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--boards", type=int, default=0, help="list the N least-judged boards")
    ap.add_argument("--fill-cache", action="store_true",
                    help="fetch uncached LCSC models, then count again")
    ap.add_argument("--pace", type=float, default=1.0)
    ap.add_argument("--wait-min", type=float, default=5.0)
    ap.add_argument("--budget-min", type=float, default=120.0)
    a = ap.parse_args(argv)
    pcbs = []
    for b in a.boards:
        pcbs += (list_file(Path(b[1:])) if b.startswith("@") else [Path(b)])
    stock = g.Stock(g.kicad_footprint_dir())
    rows = g.load_land_patterns()
    c = coverage(pcbs, live=a.live, easyeda_cache=a.easyeda_cache, stock=stock, rows=rows)
    if a.fill_cache and c["uncached_lcsc"]:
        print(table(c, a.top, a.boards))
        f = fill_cache(list(c["uncached_lcsc"]), a.pace, a.wait_min, a.budget_min,
                       a.easyeda_cache)
        print(f"fill-cache: {len(f['fetched'])} fetched, {len(f['none'])} with no "
              f"EasyEDA footprint, {len(f['left'])} left (rerun)\n---")
        c = coverage(pcbs, easyeda_cache=a.easyeda_cache, stock=stock, rows=rows)
        c["fill_cache"] = f
    if a.json:
        a.json.write_text(json.dumps(c, indent=1))
    print(table(c, a.top, a.boards))
    return 0


if __name__ == "__main__":
    sys.exit(main())
