"""fp_geometry_faults.py - plant footprint-geometry faults on copies of real
boards and report which check_fp_geometry catches: the evidence that its
quiet on clean boards is judgement, not an empty gate.

Each fault is planted on one footprint the gate judges, on a fresh copy of
the board, and is caught when the copy has a finding on that footprint that
the original board has not (the original is the quiet twin):

  pitch      the pads' spacing along the pad row set to the next smaller
             standard pitch (1.27 -> 1.0, 0.65 -> 0.5 ...)
  shrink     pad 1 shrunk 30 % on each side (half its area)
  pin1       pad 1 swapped with the pad farthest from it (pin 1 moved to the
             opposite corner)
  courtyard  the courtyard's right edge cut in by 1 mm and another part
             moved so one of its pads sits in the strip left out
  crt_only   the same cut with nothing in the strip (stays quiet by design:
             a short courtyard over empty board harms nothing)

CLI: fp_geometry_faults.py BOARD.kicad_pcb ... [--out DIR] [--per N]
     [--json OUT]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_fp_geometry as g  # noqa: E402

FAULTS = ("pitch", "shrink", "pin1", "courtyard", "crt_only")
QUIET = ("crt_only",)
PITCHES = (0.3, 0.35, 0.4, 0.5, 0.65, 0.8, 0.95, 1.0, 1.27, 1.5, 2.0, 2.54,
           3.5, 3.81, 5.0, 5.08)
_NUM = r"(-?\d+(?:\.\d+)?(?:e-?\d+)?)"


class Skip(Exception):
    """This fault can't be planted on this footprint."""


def _close(text: str, i: int) -> int:
    """Index just past the paren that closes the one at text[i]."""
    depth, q = 0, False
    for j in range(i, len(text)):
        c = text[j]
        if c == '"' and text[j - 1] != "\\":
            q = not q
        elif q:
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    raise Skip("unbalanced block")


def block(text: str, ref: str) -> tuple[int, int]:
    """(start, end) of the footprint whose Reference is `ref`."""
    for m in re.finditer(r"\(footprint\s", text):
        e = _close(text, m.start())
        b = text[m.start():e]
        if re.search(r'\(property\s+"Reference"\s+"%s"' % re.escape(ref), b) or \
                re.search(r'\(fp_text\s+reference\s+"?%s"?[\s)]' % re.escape(ref), b):
            return m.start(), e
    raise Skip(f"{ref}: no block")


def _kids(b: str, name: str) -> list[tuple[int, int]]:
    """Spans of the footprint block's direct children called `name`."""
    out, depth, q, i = [], 0, False, 0
    while i < len(b):
        c = b[i]
        if c == '"' and b[i - 1] != "\\":
            q = not q
        elif not q and c == "(":
            depth += 1
            if depth == 2 and b.startswith(name, i + 1) and not b[i + 1 + len(name)].isalnum() \
                    and b[i + 1 + len(name)] != "_":
                e = _close(b, i)
                out.append((i, e))
                i, depth = e, 1
                continue
        elif not q and c == ")":
            depth -= 1
        i += 1
    return out


def _sub(b: str, span, new: str) -> str:
    return b[:span[0]] + new + b[span[1]:]


def _pads(b: str) -> list[dict]:
    out = []
    for s in _kids(b, "pad"):
        t = b[s[0]:s[1]]
        m = re.match(r'\(pad\s+"([^"]*)"\s+(\w+)', t)
        a = re.search(r"\(at\s+%s\s+%s" % (_NUM, _NUM), t)
        z = re.search(r"\(size\s+%s\s+%s\)" % (_NUM, _NUM), t)
        if not (m and a and z) or m.group(2) == "np_thru_hole" or not m.group(1):
            continue
        out.append({"span": s, "num": m.group(1), "x": float(a.group(1)),
                    "y": float(a.group(2)), "w": float(z.group(1)), "h": float(z.group(2))})
    return out


def _edit_pads(b: str, fn) -> str:
    """Apply fn(pad, text) -> new text to every pad, last first."""
    for p in sorted(_pads(b), key=lambda p: -p["span"][0]):
        b = _sub(b, p["span"], fn(p, b[p["span"][0]:p["span"][1]]))
    return b


def _set_at(t: str, x: float, y: float) -> str:
    return re.sub(r"\(at\s+%s\s+%s" % (_NUM, _NUM), f"(at {x:.4f} {y:.4f}", t, count=1)


def plant_pitch(b: str, fp: g.FP) -> str:
    got = g._centres(fp.pads)
    if len(got) < 3:
        raise Skip("under three pads: no pad row")
    p0 = g.pitch(got)
    pts = list(got.values())
    pair = min(((a, c) for i, a in enumerate(pts) for c in pts[i + 1:]
                if math.hypot(a[0] - c[0], a[1] - c[1]) > 1e-3),
               key=lambda ac: math.hypot(ac[0][0] - ac[1][0], ac[0][1] - ac[1][1]))
    axis = 0 if abs(pair[0][0] - pair[1][0]) >= abs(pair[0][1] - pair[1][1]) else 1
    smaller = [s for s in PITCHES if s < p0 - 0.02]
    if not smaller:
        raise Skip(f"pitch {p0:.2f}: none smaller")
    f = smaller[-1] / p0
    c = sum(v[axis] for v in pts) / len(pts)

    def fn(p, t):
        x, y = p["x"], p["y"]
        if axis == 0:
            x = c + (x - c) * f
        else:
            y = c + (y - c) * f
        return _set_at(t, x, y)
    return _edit_pads(b, fn), f"pitch {p0:.3f} -> {smaller[-1]} along {'xy'[axis]}"


def _one(fp: g.FP) -> str:
    nums = [p.number for p in fp.pads]
    if nums.count("1") != 1:
        raise Skip("no single pad 1")
    return "1"


def plant_shrink(b: str, fp: g.FP) -> str:
    _one(fp)

    def fn(p, t):
        if p["num"] != "1":
            return t
        return re.sub(r"\(size\s+%s\s+%s\)" % (_NUM, _NUM),
                      f"(size {p['w'] * 0.7:.4f} {p['h'] * 0.7:.4f})", t, count=1)
    return _edit_pads(b, fn), "pad 1 at 70 % of each side"


def plant_pin1(b: str, fp: g.FP) -> str:
    _one(fp)
    got = g._centres(fp.pads)
    if len(got) < 3:
        raise Skip("under three pads: a 2-pad swap is a half turn")
    x1, y1 = got["1"]
    far = max((k for k in got if k != "1"),
              key=lambda k: math.hypot(got[k][0] - x1, got[k][1] - y1))
    if [p.number for p in fp.pads].count(far) != 1:
        raise Skip(f"pad {far} not single")

    def fn(p, t):
        if p["num"] == "1":
            return t.replace('(pad "1"', f'(pad "{far}"', 1)
        if p["num"] == far:
            return t.replace(f'(pad "{far}"', '(pad "1"', 1)
        return t
    return _edit_pads(b, fn), f"pads 1 and {far} swapped"


def _cut(b: str) -> tuple[str, float]:
    """The block with its courtyard's right edge 1 mm in, and that edge's x."""
    spans = []
    for kind in ("fp_line", "fp_rect", "fp_poly", "fp_arc", "fp_circle"):
        for s in _kids(b, kind):
            if re.search(r'\(layer\s+"?[FB]\.CrtYd', b[s[0]:s[1]]):
                if kind in ("fp_arc", "fp_circle"):
                    raise Skip("courtyard has an arc or circle")
                spans.append(s)
    if not spans:
        raise Skip("no courtyard")
    pt = re.compile(r"\((start|end|xy)\s+%s\s+%s\)" % (_NUM, _NUM))
    x1 = max(float(m.group(2)) for s in spans for m in pt.finditer(b[s[0]:s[1]]))
    for s in sorted(spans, key=lambda s: -s[0]):
        t = pt.sub(lambda m: (f"({m.group(1)} {float(m.group(2)) - 1.0:.4f} {m.group(3)})"
                              if float(m.group(2)) > x1 - 1e-3 else m.group(0)), b[s[0]:s[1]])
        b = _sub(b, s, t)
    return b, x1


def plant_crt_only(b: str, fp: g.FP) -> str:
    b, x1 = _cut(b)
    return b, f"courtyard right edge {x1:.2f} -> {x1 - 1:.2f}"


def plant_courtyard(text: str, fp: g.FP, fps: list) -> str:
    """Whole-board edit: the cut, and the smallest other same-side part
    moved so its first pad sits 0.5 mm inside the strip."""
    s, e = block(text, fp.ref)
    b, x1 = _cut(text[s:e])
    if fp.side == "bottom":
        raise Skip("bottom side")
    got = g._centres(fp.pads)
    cy = sum(v[1] for v in got.values()) / len(got)
    cand = [o for o in fps if o is not fp and o.side == fp.side and o.pads
            and o.ref != "?" and len(o.pads) <= 3]
    if not cand:
        raise Skip("no small part to move")
    o = min(cand, key=lambda o: math.hypot(o.at[0] - fp.at[0], o.at[1] - fp.at[1]))
    q = o.pads[0]
    dx, dy = g._turn(x1 - 0.5, cy, fp.rot)
    qx, qy = g._turn(q.x, q.y, o.rot)
    nx, ny = fp.at[0] + dx - qx, fp.at[1] + dy - qy
    text = text[:s] + b + text[e:]
    s2, e2 = block(text, o.ref)
    ob = text[s2:e2]
    m = re.search(r"\n\s*\(at\s+%s\s+%s" % (_NUM, _NUM), ob)
    if not m:
        raise Skip(f"{o.ref}: no position")
    ob = ob[:m.start(1)] + f"{nx:.4f} {ny:.4f}" + ob[m.end(2):]
    return text[:s2] + ob + text[e2:], (f"courtyard right edge {x1:.2f} -> {x1 - 1:.2f}, "
                                        f"{o.ref} moved into the strip")


def plant(text: str, fp: g.FP, fault: str, fps: list) -> tuple[str, str]:
    if fault == "courtyard":
        return plant_courtyard(text, fp, fps)
    s, e = block(text, fp.ref)
    b, what = {"pitch": plant_pitch, "shrink": plant_shrink, "pin1": plant_pin1,
               "crt_only": plant_crt_only}[fault](text[s:e], fp)
    return text[:s] + b + text[e:], what


def _findings(pcb: Path, stock, rows, ref=None, **kw) -> dict:
    v, _, _ = g.check(pcb, stock=stock, rows=rows, **kw)
    out = defaultdict(list)
    for x in v:
        out[x["refs"][0]].append(x["kind"])
    return out


def run(pcbs: list[Path], out: Path, per: int = 2, easyeda_cache=None) -> list[dict]:
    stock = g.Stock(g.kicad_footprint_dir())
    rows = g.load_land_patterns()
    res = []
    out.mkdir(parents=True, exist_ok=True)
    for pcb in pcbs:
        trace: list = []
        g.check(pcb, stock=stock, rows=rows, trace=trace, easyeda_cache=easyeda_cache)
        base = _findings(pcb, stock, rows, easyeda_cache=easyeda_cache)
        fps = g.board_footprints(pcb)
        byref = {f.ref: f for f in fps}
        text = pcb.read_text(encoding="utf-8")
        judged = [t for t in trace if t["why"] is None and t["ref"] in byref
                  and not base.get(t["ref"])]
        # the most pads first: a fault on a 2-pad part is often a half turn
        judged.sort(key=lambda t: (-t["pads"], t["ref"]))
        for fault in FAULTS:
            done = 0
            for t in judged:
                if done >= per:
                    break
                fp = byref[t["ref"]]
                if fault in ("shrink", "courtyard", "crt_only") and "stock" not in t["sources"]:
                    continue  # pad size and courtyard are judged against stock only
                try:
                    new, what = plant(text, fp, fault, fps)
                except Skip:
                    continue
                dst = out / f"{pcb.parent.name}-{pcb.stem}-{fault}-{fp.ref}.kicad_pcb"
                dst.write_text(new, encoding="utf-8")
                got = _findings(dst, stock, rows, easyeda_cache=easyeda_cache)
                hit = sorted(set(got.get(fp.ref, [])) - set(base.get(fp.ref, [])))
                res.append({"board": str(pcb), "fault": fault, "ref": fp.ref,
                            "fpid": fp.fpid, "sources": t["sources"], "what": what,
                            "caught": bool(hit), "kinds": hit, "copy": str(dst)})
                done += 1
    return res


def table(res: list[dict]) -> str:
    out = []
    agg = defaultdict(lambda: [0, 0])
    for r in res:
        agg[r["fault"]][0] += r["caught"]
        agg[r["fault"]][1] += 1
    out.append("fault       caught / planted")
    for f in FAULTS:
        c, n = agg[f]
        out.append(f"  {f:10} {c:3} / {n}" + (
            "   (the quiet twin: a finding here is a false one)" if f in QUIET else ""))
    out.append("")
    for r in res:
        tag = (("FALSE" if r["caught"] else "quiet") if r["fault"] in QUIET
               else ("CAUGHT" if r["caught"] else "missed"))
        out.append(f"{tag:6} {r['fault']:9} "
                   f"{Path(r['board']).parent.name}/{r['ref']} {r['fpid']} "
                   f"[{'+'.join(r['sources'])}] {r['what']}: {', '.join(r['kinds']) or '-'}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("boards", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("fp_geometry_faults"))
    ap.add_argument("--per", type=int, default=2, help="footprints per fault per board")
    ap.add_argument("--easyeda-cache", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    res = run(a.boards, a.out, a.per, a.easyeda_cache)
    if a.json:
        a.json.write_text(json.dumps(res, indent=1))
    print(table(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
