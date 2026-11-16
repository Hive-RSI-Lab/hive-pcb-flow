#!/usr/bin/env python
"""fp_verify.py - footprint pad geometry vs. datasheet land pattern (SPEC.md P3, 6.1).

Footprint verification is a top-3 real-world failure mode (SPEC P3: "mandatory").
This parses a .kicad_mod (legacy (module ..) or modern (footprint ..)) and diffs
its copper pads against the land_pattern block of a datasheet-extract JSON
(datasheet_extract.py), emitting normalized violations plus an SVG overlay with
dimensions for human side-by-side review.

Checks (land_pattern fields drive which run):
  - pad_count : # copper pads vs land_pattern.pad_count            (error)
  - pin1      : a copper pad numbered land_pattern.pin1 ('1')      (error)
                when the record's pin1 ball (A1) is in neither the copper
                nor its pinout on a ball grid, it is depopulated: the
                grid's A1 site is placed from the ball names and the
                pin-1 silk mark checked at that corner (info; a mark at
                another corner is a warning). A pin1 that is prose, not a
                pad name, goes to not_judged (#1127)
  - pad_pitch : each row of like pads (one size, one shared x or y,  (error)
                three or more) at pitch_mm, or at its own pitches_mm
                entry; every gap a whole number of pitches (a gap
                over 1.5 pitches parts two groups). Pads in no
                row (a switch, shield or thermal pad) are not pitch, and
                a row at another pitch the record doesn't state goes to
                not_judged. With no row, the nearest-pad pitch (#1066)
  - row_spacing: centre-to-centre spacing of the two pad rows vs
                row_spacing_mm, either axis of a quad              (error)
  - pad_size  : MIN and MAX pad size vs pad_size_mm (the mode hid a (warning)
                tied asymmetric column 0.25 mm over max - LEARNINGS
                2026-07-28 SSOP-20). Sorted, so it can't see a pad turned
                a quarter; pads judged axis by axis (below) are left out
  - pad_turned / pad_size by axis (#1331): each land_pattern.pads   (error /
                entry with size_mm [x, y] in the drawing's frame, laid   warning)
                on its pad under the quarter turn that fits the drawing's
                places; on a two-terminal land, pad_length_mm along the
                line through both pads and pad_width_mm across it. A pad
                that fits only with its width and height swapped is
                pad_turned (error), any other misfit pad_size (warning).
                A split, custom or odd-angled pad goes to not_judged
  - land_gap  : a two-terminal land's gap, inner edge to inner edge,   (error)
                vs gap_mm. A pad turned a quarter moves the gap under the
                body while the centres (row_spacing, check_fp_geometry)
                stay put: KiCad's stock 0201 is 0.18 mm where Murata's
                GRM033 land is 0.2 to 0.25. Every size here is one number
                (+- size_tol) or the maker's [min, max]
  - annulus   : THT min over axes of (size-drill)/2 vs the 0.15 mm JLC floor,  (error)
                and vs land_pattern drill_mm/annulus_mm when given
  - courtyard : a courtyard layer is present                       (warning)
  - stroked_pad_primitive: a custom pad's closed primitive (gr_circle,
                gr_poly, gr_rect) drawn with a stroke and no fill   (warning)
                KiCad 10.0.6's DRC and connectivity saw only a stroked
                gr_circle's centre line while the Gerber plots the full ring
                (#1133): a short on the ring passed drc_routed and a real
                connection read unconnected; gr_poly / gr_rect likely the same
  - mechanical: with the extraction's `mechanical` block, every lead fits
                its hole and every locating post has one
                (check_connector_fit's tht_pin_tight, post_holes_missing ...)

  - pad_numbering: which pad carries which pin (#1102). With the     (error)
                record's land_pattern.pads (top-view places from the land
                drawing, cited in land_pattern.pads_source) each place
                must find its numbered pad as the clear nearest (one to one,
                nearest under 0.7 of the next) under a quarter turn, no
                mirror; else each pad on another pin's place is named, with
                hint bottom_view_numbering (the mirror fits) or
                numbering_rotated (a one-step shift fits). Places that fit
                the footprint and its mirror alike (a 2-pad land) and lands
                of another shape (check_fp_geometry's job) go to
                not_judged. A letter/number ball grid must be
                numbered as its top view (A1 top left, numbers rising
                rightwards, letters downwards, under any turn). With
                land_pattern.numbering "ccw_top" the numeric pads must run
                counter-clockwise from the top. With none of these the
                check goes to not_judged. Example: SiTime's 3.2 x 2.5 mm
                PQFN-4 oscillator has pins 1 and 2 on a LONG side (SiT1602B
                POD-PQFN-004-X03225-002); numbering round the short side
                puts VDD on OE and the clock never starts

Coverage: the payload's `coverage` says, per check (pin1, pad_numbering,
pitch, pad_size, land_gap), judged / not_judged / not_stated, so a check
that judged nothing shows as such, not as a pass. `--pairs FILE` (FOOTPRINT<TAB>RECORD lines)
runs the set and reports those counts plus every error.

Board mode (#1285): `--pcb BOARD.kicad_pcb [--parts DIR] [--ref U1 ...]` runs
the pad-numbering checks on each footprint as placed. Each one is taken back to
its library frame (at 0 0, front side: a bottom part's y and B./F. layers
mirrored back, each pad's angle less the part's turn) and judged against the
record its LCSC field (else --parts' lcsc, else its MPN) finds in --parts or
hwde's extractions (by file name, or a record's mpn or lcsc field). A part
with none of those is looked up by its Value when that reads as a part number,
and only when exactly one record carries it as its name or mpn; else it is
not_judged with the reason (#1406). Only pad_numbering / bga_numbering
findings are kept, each at the part's board place with refs, side and
footprint. `judged` counts the parts each record judged; `parts` says per
part what record (or none) it got.

Exit (SPEC section 6): 0 = no error-severity findings (warnings allowed),
1 = one or more errors, 2 = unreadable footprint/JSON.

Examples:
  fp_verify.py --footprint lib/aiee.pretty/C0402.kicad_mod --datasheet-json parts/C1525.json
  fp_verify.py --footprint fp.kicad_mod --datasheet-json ds.json --svg out.svg
  fp_verify.py --pairs pairs.tsv
  fp_verify.py --pcb board.kicad_pcb --parts parts/ --out fp_verify.json
"""
from __future__ import annotations

import argparse
import math
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import check_fp_geometry as fpg  # noqa: E402  (its frames and fit, #1102)
import checklib  # noqa: E402
import fplib  # noqa: E402

SOURCE = "check.fp_verify"

# JLC minimum annular ring (jlc_capabilities.yaml, the EDITS.md acceptance
# bar); a THT pad below it cannot fab reliably whatever the datasheet says.
ANNULUS_FLOOR_MM = 0.15

# Float slack on every tolerance test: a land exactly at the tolerance
# passes. 0.55 - 0.50 is 0.0500000000000000444 in floats, so a bare
# `> 0.05` called a pad 0.05 mm off a finding (#1332)
EPS_MM = 1e-6


# A pad or ball name: "1", "A1", "EP", "SH2". Anything with a space is prose
# (an MGM240P record's "top-left in land pattern view (Fig 8.3); ...", #1127)
PAD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,7}$")
BALL = re.compile(r"^([A-Z]{1,2})(\d{1,3})$")


# A copper dot under this on its longest side is no land (an esp32c3's
# 0.005 mm marker numbered 49): neither a pad to count nor a size (#1379)
SLIVER_MM = 0.05

def _sliver(p) -> bool:
    return max(p.size) < SLIVER_MM


def _exposed_pads(copper: list) -> set[str]:
    """Numbers of an exposed pad: every pad with the number sits strictly
    inside the box of the other numbered pads' centres, and the number is on
    more than one pad (an exposed pad split for paste) or on a pad of 2.5
    times the leads' commonest area. A BGA's inner balls are inside the box
    too, but one ball to a number and of the balls' size (#1379)"""
    by: dict = {}
    for q in copper:
        if q.number and not _sliver(q):
            by.setdefault(q.number, []).append(q)
    areas = Counter(round(q.size[0] * q.size[1], 4)
                    for qs in by.values() if len(qs) == 1 for q in qs)
    common = areas.most_common(1)[0][0] if areas else 0.0
    out = set()
    for n, ps in by.items():
        rest = [q for m, qs in by.items() if m != n for q in qs]
        if len(rest) < 2:
            continue
        xs = [q.center[0] for q in rest]
        ys = [q.center[1] for q in rest]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        inside = all(x0 + 0.01 < q.center[0] < x1 - 0.01
                     and y0 + 0.01 < q.center[1] < y1 - 0.01 for q in ps)
        big = common > 0 and any(q.size[0] * q.size[1] >= 2.5 * common
                                 for q in ps)
        if inside and (len(ps) > 1 or big):
            out.add(n)
    return out


def _cover(copper: list, ep: set[str], land: dict, exp: float,
           measured: list[float]) -> float | None:
    """The share of the record's land, along the lead, that the board's
    pads cover: rows at the measured spacing nearest `exp`, each as long as
    the commonest long side of the lead pads, against the record's rows at
    `exp`, each as long as pad_size_mm's long side. None without a record
    pad size or a lead pad. check_fp_geometry calls a row spacing whose
    cover reaches its COVER_FRAC info (#1378)"""
    try:
        lr = max(float(x) for x in land["pad_size_mm"])
    except (KeyError, TypeError, ValueError):
        return None
    sides = Counter(round(max(q.size), 3) for q in copper
                    if q.number not in ep and not _sliver(q))
    if not sides or not measured or lr <= 0:
        return None
    lb = sides.most_common(1)[0][0]
    m = min(measured, key=lambda x: abs(x - exp))
    lo = max((m - lb) / 2, (exp - lr) / 2)
    hi = min((m + lb) / 2, (exp + lr) / 2)
    return max(0.0, hi - lo) / lr


def _median_step(lines: dict) -> tuple[float, float] | None:
    """Median per-index step along lines of (index, x, y); gaps divide out."""
    steps = []
    for line in lines.values():
        line = sorted(line)
        steps += [((x1 - x0) / (j - i), (y1 - y0) / (j - i))
                  for (i, x0, y0), (j, x1, y1) in zip(line, line[1:]) if j != i]
    if not steps:
        return None
    return (statistics.median(s[0] for s in steps),
            statistics.median(s[1] for s in steps))


def depopulated_pin1(path: Path, copper: list[fplib.Pad], pin1: str,
                     ds: dict) -> dict | None:
    """Pin 1 as a depopulated ball (#1127): the record names ball `pin1`
    (A1), it is in neither the copper nor the record's pinout, and the pads
    are a letter-row / number-column grid. Returns None when that doesn't
    hold (a real pin1_missing), {"why"} when the grid gives no pin-1 site,
    else {"at": the site from the grid's row and column steps, "marks":
    "ok" | "wrong" | "none", "off": marks at another corner}. The marks are
    cpl_verify's: a dot, small shape or corner chamfer on F.SilkS."""
    want = BALL.match(pin1)
    pinout = {str(p.get("pin")) for p in ds.get("pinout") or []}
    if not want or pin1 in pinout:
        return None
    balls = [(m.group(1), int(m.group(2)), p) for p in copper
             if (m := BALL.match(p.number or ""))]
    rows = sorted({b[0] for b in balls}, key=lambda r: (len(r), r))
    # a USB-C's A1..A12 / B1..B12 is two rows of pins, not a ball grid
    if len(balls) < 6 or len(rows) < 3 or len(balls) < 0.8 * len(copper):
        return None
    if want.group(1) not in rows:
        return {"why": f"ball {pin1} is in neither the copper nor the pinout, "
                       f"and no ball is in its row {want.group(1)}, so the "
                       "grid gives no site for it"}
    rank = {r: i for i, r in enumerate(rows)}
    by_row: dict = {}
    by_col: dict = {}
    for r, c, p in balls:
        by_row.setdefault(r, []).append((c, p.center[0], p.center[1]))
        by_col.setdefault(c, []).append((rank[r], p.center[0], p.center[1]))
    across, down = _median_step(by_row), _median_step(by_col)
    if not across or not down or min(math.hypot(*across), math.hypot(*down)) <= 0:
        return {"why": f"ball {pin1} is depopulated, but the ball names give "
                       "no row and column step to place its site"}
    r1, c1 = rank[want.group(1)], int(want.group(2))
    xs = [p.center[0] - (c - c1) * across[0] - (rank[r] - r1) * down[0]
          for r, c, p in balls]
    ys = [p.center[1] - (c - c1) * across[1] - (rank[r] - r1) * down[1]
          for r, c, p in balls]
    at = (round(statistics.median(xs), 3), round(statistics.median(ys), 3))
    import cpl_verify as cv  # its pin-1 mark reader; only ball grids pay
    import sexpdata
    named = [p for p in copper if p.number]
    box = (min(p.center[0] for p in named), min(p.center[1] for p in named),
           max(p.center[0] for p in named), max(p.center[1] for p in named))
    if len(cv._nearest_corners(*at, box)) > 2:
        return {"why": f"ball {pin1} is depopulated, but its site {at} from "
                       "the ball names is not at a corner of the pad field"}
    root = sexpdata.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    marks = [m for node in root[1:] if isinstance(node, list) and node
             if (m := cv._silk_mark(node, "top", box))]
    corner = cv._nearest_corners(*at, box)
    off = [[round(x, 3), round(y, 3)] for x, y in marks
           if not cv._nearest_corners(x, y, box) & corner]
    return {"at": list(at), "marks": "none" if not marks else
            "wrong" if off else "ok", "off": off}


def _nearest_neighbour_pitch(centers: list[tuple[float, float]]) -> float | None:
    if len(centers) < 2:
        return None
    best = math.inf
    for i in range(len(centers)):
        for j in range(i + 1, len(centers)):
            d = math.dist(centers[i], centers[j])
            if d < best:
                best = d
    return best if best < math.inf else None


def _clusters(vals: list[float], tol: float = 0.02) -> list[list[float]]:
    out: list[list[float]] = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def _row_spacings(centers: list[tuple[float, float]]) -> list[float]:
    """Centre-to-centre spacing of the two pad rows, one per axis that has
    them: an axis qualifies when exactly two coordinate groups hold the most
    pads (a SOIC's columns; a QFP's left/right and top/bottom), or when the
    axis has just two groups of two pads or more (a SOT-23-5's rows of 3
    and 2, #1332). An axis of equal small groups (a SOIC's pitch axis) or a
    lone biggest group beside a lone pad (SOT-23) has no row pair."""
    spacings = []
    for axis in (0, 1):
        groups = _clusters([c[axis] for c in centers])
        if len(groups) < 2:
            continue
        top = max(len(g) for g in groups)
        rows = [g for g in groups if len(g) == top]
        if len(rows) != 2:
            if len(groups) == 2 and min(len(g) for g in groups) >= 2:
                rows = groups
            else:
                continue
        a, b = (sum(g) / len(g) for g in rows)
        spacings.append(abs(b - a))
    return spacings


def _modal_size(pads: list[fplib.Pad]) -> tuple[float, float] | None:
    if not pads:
        return None
    # Compare orientation-independently: sort each pad's (w,h).
    sizes = Counter(tuple(round(v, 3) for v in sorted(p.size)) for p in pads)
    return sizes.most_common(1)[0][0]


def _pitch_rows(pads: list[fplib.Pad], min_pads: int = 3,
                tol: float = 0.02) -> list[dict]:
    """Rows of pads for pitch (#1066): pads of one size (either way round)
    whose centres share an x or a y, at least `min_pads` of them. A shield
    tab, switch contact or thermal pad has a size or a line of its own, so
    it sits in no row and is not taken as pitch; nor is a thermal pad
    split into a grid of one number. Each row lists its pads in
    order along the row and the spacings between neighbours."""
    by_size: dict[tuple, list[fplib.Pad]] = {}
    for p in pads:
        by_size.setdefault(tuple(round(s, 2) for s in sorted(p.size)), []).append(p)
    rows = []
    for group in by_size.values():
        for axis in (0, 1):
            line: list[list[fplib.Pad]] = []
            for p in sorted(group, key=lambda q: q.center[axis]):
                if line and p.center[axis] - line[-1][-1].center[axis] <= tol:
                    line[-1].append(p)
                else:
                    line.append([p])
            for run in line:
                # a thermal pad split into a grid shares one number: no pins
                if len({p.number for p in run}) < min_pads:
                    continue
                run.sort(key=lambda q: q.center[1 - axis])
                gaps = [run[i + 1].center[1 - axis] - run[i].center[1 - axis]
                        for i in range(len(run) - 1)]
                if min(gaps) <= tol:  # stacked pads, not a row
                    continue
                mode = Counter(round(g, 2) for g in gaps).most_common()
                top = mode[0][1]
                rows.append({"pads": run, "gaps": gaps,
                             "pitch": min(g for g, n in mode if n == top)})
    rows.sort(key=lambda r: -len(r["pads"]))
    return rows


def _pad_set(spec) -> set[str]:
    """A pitches_mm entry's pads: "1-8", "1-8,10", ["A1", "A2"] or a mix."""
    items = spec if isinstance(spec, list) else str(spec).split(",")
    out: set[str] = set()
    for it in items:
        it = str(it).strip()
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", it)
        if m:
            out.update(str(n) for n in range(int(m.group(1)), int(m.group(2)) + 1))
        elif it:
            out.add(it)
    return out


def _off_pitch(row: dict, pitch: float, tol: float) -> list[tuple]:
    """Neighbours in `row` whose spacing is not a whole number of pitches
    (a skipped pin is fine; a pad moved along the row is not). A gap over
    1.5 pitches that is no whole number of them parts two groups on one
    line (an RJ45's two LED pairs), so it is no finding."""
    bad = []
    for i, g in enumerate(row["gaps"]):
        k = max(1, round(g / pitch))
        if abs(g - k * pitch) > tol + EPS_MM and g <= 1.5 * pitch:
            bad.append((row["pads"][i], row["pads"][i + 1], g))
    return bad


def _row_name(row: dict) -> str:
    nums = [p.number for p in row["pads"]]
    return f"{nums[0]}..{nums[-1]} ({len(nums)} pads)"


TURNS = (0, 90, 180, 270)


def _number_centres(copper: list[fplib.Pad]) -> dict[str, tuple[float, float]]:
    """Pad number -> centre; a pad split in parts is its parts' mean."""
    acc: dict[str, list] = {}
    for p in copper:
        if p.number:
            acc.setdefault(p.number, []).append(p.center)
    return {k: (sum(x for x, _ in v) / len(v), sum(y for _, y in v) / len(v))
            for k, v in acc.items()}


NEAR_RATIO = 0.7  # a pad's nearest drawing pin must be clearly nearer than the next


def _assign(ref: dict, got: dict, deg: float) -> tuple[dict, float] | None:
    """Pad -> the drawing pin nearest it, with `got` turned `deg` and the
    centroids aligned, and the worst pad-to-pin distance; None unless the
    map is one to one and each pad's nearest pin is clearly nearer than its
    next (NEAR_RATIO). Nearest, not within a tolerance: a maker's land often
    sits a little in or out of a footprint's pad centres (rows 3.85 mm apart
    on the drawing, 5 mm on the footprint), and that is check_fp_geometry's
    business, not numbering's."""
    g = {k: fpg._turn(*xy, deg) for k, xy in got.items()}
    rc = (sum(x for x, _ in ref.values()) / len(ref),
          sum(y for _, y in ref.values()) / len(ref))
    gc = (sum(x for x, _ in g.values()) / len(g),
          sum(y for _, y in g.values()) / len(g))
    out, worst = {}, 0.0
    for k, (x, y) in g.items():
        d = sorted((math.hypot(x - gc[0] - rx + rc[0], y - gc[1] - ry + rc[1]), r)
                   for r, (rx, ry) in ref.items())
        if len(d) > 1 and d[0][0] >= NEAR_RATIO * d[1][0]:
            return None
        out[k] = d[0][1]
        worst = max(worst, d[0][0])
    return (out, worst) if len(set(out.values())) == len(out) else None


def _turns(ref: dict, got: dict) -> tuple[float, ...]:
    """TURNS, plus the turn the shared pad numbers point to and its quarter
    offsets when that is off a quarter turn (#1409): a round can's land
    drawing (TO-46 at 45 degrees) is often drawn turned by something else,
    and under a quarter turn each pad's nearest pin is then its neighbour's.
    The angle is the least-squares (Kabsch) turn of the footprint's pads
    onto the drawing's, about their centroids, in fpg._turn's sense."""
    common = [k for k in ref if k in got]
    if len(common) < 2:
        return TURNS
    rc = [sum(ref[k][i] for k in common) / len(common) for i in (0, 1)]
    gc = [sum(got[k][i] for k in common) / len(common) for i in (0, 1)]
    dot = cross = 0.0
    for k in common:
        gx, gy = got[k][0] - gc[0], got[k][1] - gc[1]
        rx, ry = ref[k][0] - rc[0], ref[k][1] - rc[1]
        dot += gx * rx + gy * ry
        cross += gx * ry - gy * rx
    if abs(dot) + abs(cross) < 1e-9:
        return TURNS
    deg = -math.degrees(math.atan2(cross, dot)) % 360
    off = min(deg % 90, 90 - deg % 90)
    reach = max(math.hypot(got[k][0] - gc[0], got[k][1] - gc[1]) for k in common)
    if reach * math.radians(off) <= 0.01:
        return TURNS
    return TURNS + tuple(round((deg + q) % 360, 3) for q in TURNS)


def _fit_limit(ref: dict, tol: float, pad_reach: float = 0.0) -> float:
    """How far a pad may sit from the drawing pin it is matched to for a
    mismatch to count (#1409): 0.4 x the drawing's closest pin spacing, as
    #1286's fit uses, or `pad_reach` (half the footprint pads' typical
    longer side: a maker's SO-8 rows 3.85 mm apart against the footprint's
    5 mm leave each pad 0.58 mm off, still on its pin's land), and never
    under the size tolerance."""
    pts = list(ref.values())
    near = min((math.hypot(a[0] - b[0], a[1] - b[1])
                for i, a in enumerate(pts) for b in pts[i + 1:]), default=0.0)
    return max(tol, 0.4 * near, pad_reach)


def _clean(ref: dict, got: dict, tol: float) -> list[tuple[float, dict, float]]:
    """The turns (_turns) whose nearest-pin map is clean and whose worst
    distance is within `tol` of the best turn's, with that distance: a
    near-square body (3.2 x 2.5 mm) turned a quarter still maps cleanly, a
    little worse, and must not stand in for the turn that really fits."""
    maps = [(t, *m) for t in _turns(ref, got)
            if (m := _assign(ref, got, t)) is not None]
    if not maps:
        return []
    best = min(w for _, _, w in maps)
    return [(t, m, w) for t, m, w in maps if w <= best + tol]


def _fits(ref: dict, got: dict, tol: float) -> float | None:
    """The turn (no mirror; a quarter turn, or the one the pads point to,
    _turns) under which every pad's nearest drawing pin is its own, else
    None."""
    return next((t for t, m, _ in _clean(ref, got, tol)
                 if all(k == r for k, r in m.items())), None)


def _shifted(got: dict, s: int) -> dict:
    """`got` with its numeric pads renumbered s steps round (pad k named
    k - s); other names (EP, MP) stay."""
    order = sorted((k for k in got if k.isdigit()), key=int)
    out = {k: v for k, v in got.items() if not k.isdigit()}
    for i, k in enumerate(order):
        out[order[(i - s) % len(order)]] = got[k]
    return out


def _pad_reach(copper: list[fplib.Pad], numbers: list[str]) -> float:
    """Half the median longer side of the copper pads named in `numbers`."""
    sides = sorted(max(p.size) / 2 for p in copper if p.number in numbers)
    return sides[len(sides) // 2] if sides else 0.0


def _misplaced(ref: dict, got: dict, tol: float,
               pad_reach: float = 0.0) -> list[tuple[str, str]] | None:
    """(pad, the drawing's pin at its place) for each pad on another pin's
    place, in the clean turn where most pads sit on their own pin; None
    when no turn gives a clean one-to-one match within _fit_limit (the
    shape differs, or the drawing is turned some way the fit can't find:
    nearest with no limit read a TO-46 drawn at 45 degrees as misnumbered,
    #1409)."""
    best = None
    limit = _fit_limit(ref, tol, pad_reach)
    for _, m, w in _clean(ref, got, tol):
        if w > limit + EPS_MM:
            continue
        pairs = [(k, r) for k, r in m.items() if k != r]
        if best is None or len(pairs) < len(best):
            best = pairs
    if best is None:
        return None
    return sorted(best, key=lambda kv: (not kv[0].isdigit(),
                                        int(kv[0]) if kv[0].isdigit() else 0, kv[0]))


def _grid_handedness(copper: list[fplib.Pad]) -> float | None:
    """For a letter-row / number-column ball grid: the cross product of the
    column step (1 -> 2) and the row step (A -> B). JEDEC's top view has A1
    top left, numbers rising rightwards and letters downwards, which is
    positive in KiCad's y-down frame under any turn; a grid numbered from
    the bottom view is its mirror, negative. None if not a grid."""
    balls = [(m.group(1), int(m.group(2)), p) for p in copper
             if (m := BALL.match(p.number or ""))]
    rows = sorted({b[0] for b in balls}, key=lambda r: (len(r), r))
    cols = {b[1] for b in balls}
    if (len(balls) < 6 or len(rows) < 3 or len(cols) < 3
            or len(balls) < 0.8 * len(copper)):
        return None
    rank = {r: i for i, r in enumerate(rows)}
    by_row: dict = {}
    by_col: dict = {}
    for r, c, p in balls:
        by_row.setdefault(r, []).append((c, p.center[0], p.center[1]))
        by_col.setdefault(c, []).append((rank[r], p.center[0], p.center[1]))
    across, down = _median_step(by_row), _median_step(by_col)
    if not across or not down:
        return None
    hand = across[0] * down[1] - across[1] * down[0]
    # a zero cross product is no grid: a USB-C receptacle's A and B rows
    # and S shield tabs match the ball pattern but its steps are parallel
    return hand if abs(hand) > 1e-6 else None


def _ccw_turn(got: dict) -> float | None:
    """Sum of the turn from pad k to pad k+1 about the pads' centroid over
    the numeric pads in order: negative is counter-clockwise seen from the
    top in KiCad's y-down frame. None with fewer than three numeric pads."""
    order = sorted((k for k in got if k.isdigit()), key=int)
    if len(order) < 3:
        return None
    cx = sum(got[k][0] for k in order) / len(order)
    cy = sum(got[k][1] for k in order) / len(order)
    tot = 0.0
    for a, b in zip(order, order[1:]):
        ax, ay = got[a][0] - cx, got[a][1] - cy
        bx, by = got[b][0] - cx, got[b][1] - cy
        tot += math.atan2(ax * by - ay * bx, ax * bx + ay * by)
    return tot


def _drawing_turn(ref: dict, got: dict, tol: float) -> int | None:
    """The quarter turn that lays the footprint's numbered pads on the land
    drawing's places, or on their mirror image (a bottom-view numbering is
    pad_numbering's finding, and the mirror leaves each axis where it is);
    None if neither fits."""
    common = sorted(set(ref) & set(got))
    if len(common) < 2:
        return None
    r = {k: ref[k] for k in common}
    g = {k: got[k] for k in common}
    t = _fits(r, g, tol)
    return t if t is not None else _fits(
        {k: (-x, y) for k, (x, y) in r.items()}, g, tol)


def _span(val, tol: float) -> tuple[float, float, str]:
    """A land dimension as (lo, hi, text): the maker's [min, max] range as
    given, or one number +- tol (#1331)."""
    if isinstance(val, (list, tuple)):
        lo, hi = sorted(float(x) for x in val)
        return lo, hi, f"{lo:g}..{hi:g}"
    x = float(val)
    return x - tol, x + tol, f"{x:g}+-{tol:g}"


def _within(x: float, span: tuple) -> bool:
    return span[0] - EPS_MM <= x <= span[1] + EPS_MM


def _along(p: fplib.Pad, angle: float) -> tuple[float, float] | None:
    """The pad's extent along the direction `angle` degrees and across it;
    None for a pad at an odd angle to it, which has no one length along it."""
    if p.shape == "circle":
        return (p.size[0], p.size[0])
    rel = (p.at[2] - angle) % 180
    if min(rel, 180 - rel) < 1:
        return (p.size[0], p.size[1])
    if abs(rel - 90) < 1:
        return (p.size[1], p.size[0])
    return None


COVERED = ("pin1", "pad_numbering", "pitch", "pad_size", "land_gap")


def coverage(land: dict, not_judged: list[dict], numbering: dict | None,
             numbered: int) -> dict[str, str]:
    """Per check: judged, not_judged, or not_stated (the record gives it
    nothing to judge). A check that comes out not_judged on every part
    judges nothing: --pairs counts these over a set of parts so it shows
    as a number, not a silent pass (#1102)."""
    left = {n["check"] for n in not_judged}
    stated = {
        "pitch": land.get("pitch_mm") is not None or bool(land.get("pitches_mm")),
        "pad_size": bool(land.get("pad_size_mm")) or any(
            land.get(k) is not None for k in ("pad_length_mm", "pad_width_mm"))
        or any(q.get("size_mm") for q in land.get("pads") or []),
        "land_gap": land.get("gap_mm") is not None,
    }
    out = {}
    for c in COVERED:
        if c in left:
            out[c] = "not_judged"
        elif c == "pad_numbering":
            out[c] = "judged" if numbering else "not_stated"
        elif not stated.get(c, True):
            out[c] = "not_stated"
        else:
            out[c] = "judged"
    return out


def coverage_report(pairs: list[tuple[str, str]], pitch_tol: float,
                    size_tol: float) -> dict:
    """fp_verify over (footprint, record) pairs: per check, how many were
    judged, not judged (by why) or not stated, and each error found."""
    count = {c: Counter() for c in COVERED}
    why = {c: Counter() for c in COVERED}
    errors, unread = [], []
    for fp_path, rec in pairs:
        try:
            ds = checklib.load_json(rec, "datasheet-json")
            viol, facts, _ = verify(Path(fp_path), ds, pitch_tol, size_tol)
        except Exception as e:  # noqa: BLE001  (one bad pair, not the set)
            unread.append({"footprint": fp_path, "record": rec, "why": str(e)})
            continue
        for c, st in facts["coverage"].items():
            count[c][st] += 1
        for n in facts["not_judged"]:
            if n["check"] in why:   # a drill of another kind is no COVERED check
                why[n["check"]][n["why"][:90]] += 1
        errors += [{"footprint": fp_path, "record": rec, "kind": v.get("kind") or v["check"],
                    "message": v["msg"]}
                   for v in viol if v["severity"] == "error"]
    return {"pairs": len(pairs),
            "coverage": {c: {"judged": count[c]["judged"],
                             "not_judged": count[c]["not_judged"],
                             "not_stated": count[c]["not_stated"],
                             "not_judged_why": dict(why[c].most_common())}
                         for c in COVERED},
            "errors": errors, "unreadable": unread}


def verify(footprint: Path, ds: dict, pitch_tol: float, size_tol: float) -> tuple[list[dict], dict]:
    fp = fplib.parse_footprint(footprint)
    copper = fp.copper_pads
    centers = [p.center for p in copper]
    measured_pitch = _nearest_neighbour_pitch(centers)
    measured_size = _modal_size(copper)
    land = ds.get("land_pattern") or {}
    refs = [fp.name]
    viol: list[dict] = []

    def v(sev, pos, msg, kind, **extra):
        viol.append(checklib.violation(
            "fp_verify", sev, pos, "F.Cu", None, refs, msg, SOURCE, kind=kind, **extra))

    # --- pad count. A record counts pins: an exposed pad split for paste
    # is one pin on many pads, a copper dot is none, and many records leave
    # the exposed pad out of the count. So the count matches on raw pads or
    # on distinct numbers (with no gap in 1..max), a miss by just the
    # exposed pad is info, and an error names the numbers skipped (#1379)
    ep = _exposed_pads(copper)
    if "pad_count" in land:
        expected = int(land["pad_count"])
        lands = [p for p in copper if not _sliver(p)]
        nums = {p.number for p in lands if p.number}
        digits = sorted(int(n) for n in nums if n.isdigit())
        skipped = ([n for n in range(1, digits[-1] + 1) if str(n) not in nums]
                   if digits else [])
        if expected == len(lands) or (expected == len(nums) and not skipped):
            pass
        elif ep and len(nums - ep) == expected and not skipped:
            v("info", None,
              f"{len(nums)} pad numbers against the record's {expected}: "
              f"the record leaves out the exposed pad {', '.join(sorted(ep))}",
              "pad_count_ep", expected=expected, measured=len(nums),
              exposed=sorted(ep))
        else:
            gap = (f"; numbers skipped: {', '.join(map(str, skipped))}"
                   if skipped else "")
            v("error", None,
              f"copper pad count {len(lands)} ({len(nums)} numbers) != "
              f"datasheet {expected}{gap}",
              "pad_count", expected=expected, measured=len(lands),
              numbers=len(nums), skipped=skipped)

    # --- pin 1 present. A record's pin1 that is a sentence names no pad,
    # and a depopulated A1 ball is absent by design (#1127): neither is a
    # missing pin 1, so each is judged another way or listed in not_judged
    pin1 = str(land.get("pin1", "1"))
    not_judged: list[dict] = []
    pin1_fact: dict | None = None
    if not PAD_NAME.match(pin1):
        not_judged.append({"check": "pin1", "why": (
            f"the record's pin1 {pin1[:60]!r} is not a pad name, so there is "
            "no pad to look for: re-extract it as the pad number and put the "
            "description in notes")})
    elif not any(p.number == pin1 for p in copper):
        pin1_fact = depopulated_pin1(footprint, copper, pin1, ds)
        if pin1_fact is None:
            nums = sorted({p.number for p in copper})
            v("error", None, f"no copper pad numbered '{pin1}' (pads: {nums})",
              "pin1_missing", expected_pin1=pin1)
        elif pin1_fact.get("why"):
            not_judged.append({"check": "pin1", "why": pin1_fact["why"]})
        elif pin1_fact["marks"] == "wrong":
            v("warning", pin1_fact["at"],
              f"ball {pin1} is depopulated, and a pin-1 mark sits at another "
              f"corner than the grid's {pin1} site {pin1_fact['at']}: "
              "move the mark to that corner",
              "pin1_mark_off_corner", expected_pin1=pin1,
              site_mm=pin1_fact["at"], marks_off=pin1_fact["off"])
        else:
            mark = ("its pin-1 mark is at that corner"
                    if pin1_fact["marks"] == "ok" else
                    "no pin-1 mark on F.SilkS to check against")
            v("info", pin1_fact["at"],
              f"ball {pin1} is depopulated (in neither the copper nor the "
              f"record's pinout); the grid's {pin1} site is at "
              f"{pin1_fact['at']}, and {mark}",
              "pin1_depopulated", expected_pin1=pin1,
              site_mm=pin1_fact["at"], mark=pin1_fact["marks"])

    # --- pad numbering (#1102): which pad carries which pin. Judged from
    # the record's land_pattern.pads (top-view positions from the land
    # drawing), else from a ball grid's names (the pin map is the
    # position), else from land_pattern.numbering; with none, not_judged
    numbering_fact: dict | None = None
    got = _number_centres(copper)
    stated_pads = land.get("pads") or []
    if stated_pads:
        ref_all = {str(q["number"]): (float(q["at_mm"][0]), float(q["at_mm"][1]))
                   for q in stated_pads}
        common = sorted(set(ref_all) & set(got))
        ref = {k: ref_all[k] for k in common}
        mine = {k: got[k] for k in common}
        mirror = {k: (-x, y) for k, (x, y) in ref.items()}
        if len(common) < 2:
            not_judged.append({"check": "pad_numbering", "why": (
                "land_pattern.pads shares fewer than two pad numbers with "
                "the footprint, so there is nothing to place")})
        elif ((turn := _fits(ref, mine, size_tol)) is not None
              and _fits(mirror, mine, size_tol) is not None):
            not_judged.append({"check": "pad_numbering", "why": (
                "the land drawing's places fit both the footprint and its "
                "mirror image, so they can't tell a top-view numbering from "
                "a bottom-view one")})
        elif turn is not None:
            numbering_fact = {"from": "pads", "fit": True,
                              "turn_deg": round(turn, 1) if turn % 1 else int(turn),
                              "pads": len(common)}
        elif (moved := _misplaced(ref, mine, size_tol, _pad_reach(
                copper, common))) is None:
            not_judged.append({"check": "pad_numbering", "why": (
                "the footprint's pads don't fall one to one on the land "
                "drawing's places, each within 0.4 x the drawing's closest "
                "pin spacing or half a pad, under any quarter turn or the turn the pad "
                "numbers point to (the shape differs; check_fp_geometry "
                "judges that), so numbering is unchecked")})
        else:
            hint = ("bottom_view_numbering" if _fits(mirror, mine, size_tol) is not None
                    else "numbering_rotated" if any(
                        _fits(ref, _shifted(mine, s), size_tol) is not None for s in (1, -1))
                    else None)
            where = "; ".join(f"pad {a} sits where the drawing has pin {b}"
                              for a, b in moved[:6])
            why = {"bottom_view_numbering": (
                       ": the mirror image of the drawing fits, so the "
                       "pads were numbered from a bottom view"),
                   "numbering_rotated": (
                       ": the numbers are shifted one step round the body")
                   }.get(hint, "")
            numbering_fact = {"from": "pads", "fit": False, "hint": hint,
                              "pads": len(common)}
            v("error", got.get(moved[0][0]),
              f"pad numbering differs from the record's land pattern{why}. "
              f"{where}",
              "pad_numbering", hint=hint, drawing=land.get("pads_source"),
              misplaced=[{"pad": a, "drawing_pin": b} for a, b in moved])
    elif (hand := _grid_handedness(copper)) is not None:
        numbering_fact = {"from": "ball_names", "fit": hand > 0}
        if hand < 0:
            numbering_fact["hint"] = "bottom_view_numbering"
            v("error", None,
              "ball grid numbered as its mirror image: seen from the top, "
              "with A1 at the top left, numbers must rise to the right and "
              "letters downwards. Here they run the other way round, so the "
              "balls were numbered from the bottom view",
              "pad_numbering", hint="bottom_view_numbering")
    elif land.get("numbering") == "ccw_top" and (t := _ccw_turn(got)) is not None:
        numbering_fact = {"from": "numbering", "fit": t < 0}
        if t > 0:
            numbering_fact["hint"] = "bottom_view_numbering"
            v("error", None,
              "pads are numbered clockwise seen from the top; the record "
              "says counter-clockwise (ccw_top), so the footprint is the "
              "part's mirror image, numbered from the bottom view",
              "pad_numbering", hint="bottom_view_numbering")
    elif len(got) >= 2:
        not_judged.append({"check": "pad_numbering", "why": (
            "the record states no pad positions (land_pattern.pads) and no "
            "numbering direction, so which pad carries which pin is "
            "unchecked")})

    # --- pad size and gap, axis by axis (#1331). The sorted pad_size_mm
    # check below passes a pad turned a quarter, and on a two-terminal chip
    # that moves the gap under the body, which row spacing (centre to
    # centre) never sees. land_pattern.pads[].size_mm is judged in the land
    # drawing's frame, turned as the pads fit it; gap_mm, pad_length_mm and
    # pad_width_mm along and across the line through a two-terminal land's
    # pads. Each is one number (+- size_tol) or the maker's [min, max]
    size_judged: set[int] = set()   # id() of each pad judged on both axes
    by_axis_fact: dict | None = None
    sized = [q for q in stated_pads if q.get("size_mm")]
    if sized:
        fit = _drawing_turn(
            {str(q["number"]): (float(q["at_mm"][0]), float(q["at_mm"][1]))
             for q in stated_pads}, got, size_tol)
        if fit is None:
            not_judged.append({"check": "pad_size", "why": (
                "the footprint's pads don't lie on land_pattern.pads' places "
                "under any quarter turn, so the drawing's per-pad size_mm "
                "can't be laid on them")})
        else:
            turned, off, odd, judged = [], [], [], []
            for q in sized:
                n = str(q["number"])
                same = [p for p in copper if p.number == n]
                if not same:
                    continue      # a missing pad is pad_count's or pin1's finding
                p = same[0]
                dims = (_along(p, -fit) if len(same) == 1
                        and p.shape != "custom" else None)
                if dims is None:
                    odd.append(n)
                    continue
                want = [_span(d, size_tol) for d in q["size_mm"]]
                size_judged.add(id(p))
                judged.append(n)
                if all(_within(m, w) for m, w in zip(dims, want)):
                    continue
                swapped = _within(dims[1], want[0]) and _within(dims[0], want[1])
                (turned if swapped else off).append((p, dims, want))

            def said(rows):
                return "; ".join(
                    f"pad {p.number} is {d[0]:g} x {d[1]:g} mm where the "
                    f"drawing has {w[0][2]} x {w[1][2]}" for p, d, w in rows[:6])

            if turned:
                v("error", turned[0][0].center,
                  "pad size turned a quarter against the land drawing (x by y "
                  f"in the drawing's frame): {said(turned)}. Swap each one's "
                  "width and height",
                  "pad_turned", drawing=land.get("pads_source"),
                  pads=[p.number for p, _, _ in turned],
                  measured_mm=[list(d) for _, d, _ in turned])
            if off:
                v("warning", off[0][0].center,
                  "pad size differs from the land drawing (x by y in the "
                  f"drawing's frame): {said(off)}",
                  "pad_size", drawing=land.get("pads_source"),
                  pads=[p.number for p, _, _ in off],
                  measured_mm=[list(d) for _, d, _ in off],
                  expected_mm=[[[round(w[0], 4), round(w[1], 4)] for w in ws]
                               for _, _, ws in off])
            if odd:
                not_judged.append({"check": "pad_size", "why": (
                    f"pads {', '.join(odd)}: split in parts, custom-shaped or "
                    "at an odd angle, so they have no one size along the "
                    "drawing's axes")})
            by_axis_fact = {"turn_deg": fit, "pads": judged}

    # a two-terminal land as the maker draws it: the gap between the pads'
    # inner edges, each pad's length along the line through both centres
    # and its width across it
    two_fact: dict | None = None
    lw = {k: _span(land[k], size_tol) for k in ("pad_length_mm", "pad_width_mm")
          if land.get(k) is not None}
    if land.get("gap_mm") is not None or lw:
        asked = (["land_gap"] if land.get("gap_mm") is not None else []) + (
            ["pad_size"] if lw else [])
        why = None
        custom = any(p.shape == "custom" for p in copper)
        if len(copper) != 2 or custom:
            why = (f"{len(copper)} copper pads{', one custom' if custom else ''}"
                   ": gap_mm, pad_length_mm and pad_width_mm describe a "
                   "two-terminal land of two plain pads")
        else:
            a, b = sorted(copper, key=lambda p: (p.number, p.at))
            dx, dy = b.at[0] - a.at[0], b.at[1] - a.at[1]
            ang = math.degrees(math.atan2(-dy, dx))
            ea, eb = _along(a, ang), _along(b, ang)
            if math.hypot(dx, dy) < 1e-6 or ea is None or eb is None:
                why = ("the pads sit at an odd angle to the line through "
                       "their centres, so they have no one length along it")
        if why:
            not_judged += [{"check": c, "why": why} for c in asked]
        else:
            gap = math.hypot(dx, dy) - (ea[0] + eb[0]) / 2
            two_fact = {"gap_mm": round(gap, 4), "pads": [
                {"pad": p.number, "along_mm": e[0], "across_mm": e[1]}
                for p, e in ((a, ea), (b, eb))]}
            if land.get("gap_mm") is not None:
                g = _span(land["gap_mm"], size_tol)
                if not _within(gap, g):
                    risk = ("solder can bridge under the body" if gap < g[0]
                            else "the terminals may not reach the pads, and "
                            "the part can tombstone")
                    v("error", a.center,
                      f"pads {a.number} and {b.number} are {gap:.3f} mm apart, "
                      f"inner edge to inner edge; the record's land has a "
                      f"{g[2]} mm gap, so {risk}",
                      "land_gap", expected_mm=[round(g[0], 4), round(g[1], 4)],
                      stated_mm=[round(x, 4) for x in (
                          sorted(map(float, land["gap_mm"]))
                          if isinstance(land["gap_mm"], (list, tuple))
                          else [float(land["gap_mm"])] * 2)],
                      measured_mm=round(gap, 4))
            ln, wd = lw.get("pad_length_mm"), lw.get("pad_width_mm")
            want = " and ".join(t for t in (ln and f"{ln[2]} mm long",
                                            wd and f"{wd[2]} mm wide") if t)
            turned2: dict = {}
            off2: dict = {}
            for p, (al, ac) in ((a, ea), (b, eb)):
                if not lw or id(p) in size_judged:
                    continue    # no size stated, or judged on its size_mm above
                if ln and wd:
                    size_judged.add(id(p))
                if (ln is None or _within(al, ln)) and (wd is None or _within(ac, wd)):
                    continue
                swapped = ln and wd and _within(ac, ln) and _within(al, wd)
                (turned2 if swapped else off2).setdefault((al, ac), []).append(p)
            for (al, ac), ps in turned2.items():
                nums = [p.number for p in ps]
                v("error", ps[0].center,
                  f"pad{'s' if len(ps) > 1 else ''} {' and '.join(nums)}: "
                  f"{al:g} mm along the line through both pads and {ac:g} mm "
                  f"across it; the record's land is {want}, so "
                  f"{'each pad is' if len(ps) > 1 else 'the pad is'} turned a "
                  "quarter: swap width and height",
                  "pad_turned", pads=nums, measured_mm=[al, ac])
            for (al, ac), ps in off2.items():
                nums = [p.number for p in ps]
                v("warning", ps[0].center,
                  f"pad{'s' if len(ps) > 1 else ''} {' and '.join(nums)}: "
                  f"{al:g} mm along the line through both pads and {ac:g} mm "
                  f"across it; the record's land is {want}",
                  "pad_size", pads=nums, measured_mm=[al, ac],
                  expected_mm={"length": ln and [round(ln[0], 4), round(ln[1], 4)],
                               "width": wd and [round(wd[0], 4), round(wd[1], 4)]})

    # --- pitch, judged row by row (#1066): the nearest pair of pads in the
    # whole footprint is no pitch when a switch contact, shield tab or
    # thermal pad sits close to a pin row. Each row of like pads is judged
    # against its pitches_mm entry, or pitch_mm; a footprint with no row of
    # three keeps the nearest-pad pitch (a chip passive, a SOT-23)
    rows = _pitch_rows(copper)
    pitch_facts: list[dict] = []
    if rows:
        measured_pitch = rows[0]["pitch"]

    def judge_row(row, exp, why):
        bad = _off_pitch(row, exp, pitch_tol)
        pitch_facts.append({"pads": _row_name(row), "pitch_mm": row["pitch"],
                            "expected_mm": exp, "ok": not bad})
        if abs(row["pitch"] - exp) > pitch_tol + EPS_MM:
            v("error", row["pads"][0].center,
              f"pad row {_row_name(row)} pitch {row['pitch']:.3f} mm != "
              f"datasheet {exp:.3f} mm{why} (tol {pitch_tol})",
              "pad_pitch", expected_mm=exp, measured_mm=row["pitch"],
              pads=[p.number for p in row["pads"]])
        elif bad:
            a, b, g = bad[0]
            v("error", b.center,
              f"pads {a.number} and {b.number} are {g:.3f} mm apart, not a "
              f"whole number of the {exp:.3f} mm pitch{why} (row "
              f"{_row_name(row)}, tol {pitch_tol}): a pad has moved along "
              "the row",
              "pad_pitch", expected_mm=exp, measured_mm=round(g, 4),
              pads=[p.number for x in bad for p in x[:2]])

    stated = [e for e in land.get("pitches_mm") or [] if e.get("pads")]
    claimed: set[str] = set()
    for e in stated:
        want = _pad_set(e["pads"])
        claimed |= want
        mine = _pitch_rows([p for p in copper if p.number in want], min_pads=2)
        if not mine:
            not_judged.append({"check": "pitch", "why": (
                f"pitches_mm pads {e['pads']}: the footprint's pads there "
                "form no row to measure")})
        for row in mine:
            judge_row(row, float(e["pitch_mm"]), f" for pads {e['pads']}")
    if land.get("pitch_mm") is not None:
        exp = float(land["pitch_mm"])
        rest = [r for r in rows
                if not claimed & {p.number for p in r["pads"]}]
        if not rows and measured_pitch is not None and not stated:
            # basis: the two closest pads, which on a part with no row of
            # three may be no pitch at all (a USB-A's shield beside a pin),
            # so a caller may weigh it lower than a row's pitch (#1332)
            if abs(measured_pitch - exp) > pitch_tol + EPS_MM:
                v("error", centers[0] if centers else None,
                  f"nearest-pad pitch {measured_pitch:.3f} mm != datasheet "
                  f"{exp:.3f} mm (tol {pitch_tol})",
                  "pad_pitch", expected_mm=exp,
                  measured_mm=round(measured_pitch, 4), basis="nearest_pads")
        elif rest:
            near = [r for r in rest
                    if abs(r["pitch"] - exp) <= pitch_tol + EPS_MM]
            for row in near or rest[:1]:
                judge_row(row, exp, "")
            if near:
                for row in rest:
                    if row not in near:
                        not_judged.append({"check": "pitch", "why": (
                            f"pad row {_row_name(row)} is at "
                            f"{row['pitch']:.3f} mm and the record states "
                            f"only {exp:.3f} mm; list it in "
                            "land_pattern.pitches_mm to judge it")})

    # --- row spacing (error): on a two-row leaded package it decides
    # whether the pads catch the lead feet at all, and a wrong one slides
    # both rows off the leads while pad count and pitch still pass
    measured_rows = _row_spacings(centers)
    if land.get("row_spacing_mm") is not None and copper:
        exp = float(land["row_spacing_mm"])
        if not measured_rows:
            v("warning", None,
              f"datasheet row spacing {exp:g} mm, but the footprint has no "
              "two pad rows to measure it on: check it by hand",
              "row_spacing_unmeasured", expected_mm=exp)
        elif all(abs(m - exp) > size_tol + EPS_MM for m in measured_rows):
            got = ", ".join(f"{m:.3f}" for m in measured_rows)
            v("error", None,
              f"pad row spacing {got} mm != datasheet {exp:g} mm "
              f"(tol {size_tol}, centre to centre): the rows may miss the "
              "lead feet",
              "row_spacing", expected_mm=exp,
              measured_mm=[round(m, 4) for m in measured_rows],
              cover=(None if (c := _cover(copper, ep, land, exp, measured_rows))
                     is None else round(c, 3)))

    # --- pad size (warning): bound MIN and MAX, never the mode - a 10/10 tie
    # on an asymmetric SSOP-20 hid a column 0.25 mm over the datasheet max
    # (LEARNINGS 2026-07-28 [librarian][easyeda2kicad]). Sorted, so blind to
    # a pad turned a quarter: pads judged axis by axis above are left out
    rest = [p for p in copper if id(p) not in size_judged
            and p.number not in ep and not _sliver(p)]
    if land.get("pad_size_mm") and rest:
        exp = tuple(sorted(float(x) for x in land["pad_size_mm"]))
        distinct = sorted({tuple(round(v_, 3) for v_ in sorted(p.size))
                           for p in rest})
        extremes = [distinct[0]] if len(distinct) == 1 else [distinct[0], distinct[-1]]
        offending = [list(s) for s in extremes
                     if any(abs(m - e) > size_tol + EPS_MM
                            for m, e in zip(s, exp))]
        if offending:
            # basis: sides sorted small to large, blind to which way a pad
            # lies, so a caller may weigh it lower than an axis-by-axis
            # finding (#1332)
            v("warning", None,
              f"pad size(s) {offending} mm != datasheet {list(exp)} mm "
              f"(tol {size_tol}; min/max of {len(distinct)} distinct sizes)",
              "pad_size", expected_mm=list(exp), measured_mm=offending,
              basis="sorted")

    # --- THT annulus (error): drill-aware; a wrong annulus passed silently
    # before fplib parsed (drill ...) at all (ladder rows 111/115).
    # drill_mm is one number, and a part may have holes of two kinds (a
    # USB-A's 0.92 mm pins beside its 2.3 mm shield legs): the group of
    # like drills nearest drill_mm is always judged, and a hole off it by
    # more than half of drill_mm in another group is a hole of another
    # kind, listed in not_judged, not a drill_mismatch (#1332)
    holes = [p for p in copper if p.ptype == "thru_hole" and p.drill is not None]
    other_kind: set[int] = set()
    if land.get("drill_mm") is not None and holes:
        want_d = float(land["drill_mm"])
        groups = _clusters([p.drill for p in holes])
        nearest = min(groups, key=lambda g: abs(sum(g) / len(g) - want_d))
        other_kind = {id(p) for p in holes
                      if not any(abs(p.drill - d) <= 1e-9 for d in nearest)
                      and abs(p.drill - want_d) > want_d / 2}
        if other_kind:
            odd = [p for p in holes if id(p) in other_kind]
            not_judged.append({"check": "drill", "why": (
                f"pads {', '.join(sorted({p.number for p in odd}))}: drills "
                f"of {', '.join(sorted({f'{p.drill:g}' for p in odd}))} mm "
                f"are another kind of hole than the record's one drill_mm "
                f"{want_d:g} mm (a shield or mounting leg); list them in "
                "the record's mechanical block to judge them")})
    for p in holes:
        # per axis: an oval drill in an oval pad (C165948 shell: 1.2x2.0 pad,
        # 0.8x1.5 drill) is 0.20 mm all round, not min(size) - long axis
        dx, dy = p.drill_xy or (p.drill, p.drill)
        ann = min((p.size[0] - dx) / 2.0, (p.size[1] - dy) / 2.0)
        if ann < ANNULUS_FLOOR_MM - 1e-9:
            v("error", p.center,
              f"pad {p.number}: THT annulus {ann:.3f} mm < {ANNULUS_FLOOR_MM} mm "
              f"JLC floor (pad {p.size[0]:g}x{p.size[1]:g} mm on {dx:g}x{dy:g} mm drill)",
              "annulus_floor", measured_mm=round(ann, 4),
              floor_mm=ANNULUS_FLOOR_MM, drill_mm=p.drill)
        if id(p) in other_kind:
            continue    # the annulus floor above holds; drill_mm is not its
        if land.get("drill_mm") is not None and \
                abs(p.drill - float(land["drill_mm"])) > size_tol + EPS_MM:
            v("error", p.center,
              f"pad {p.number}: drill {p.drill:g} mm != datasheet "
              f"{float(land['drill_mm']):g} mm (tol {size_tol})",
              "drill_mismatch", expected_mm=float(land["drill_mm"]),
              measured_mm=p.drill)
        if land.get("annulus_mm") is not None and \
                abs(ann - float(land["annulus_mm"])) > size_tol + EPS_MM:
            v("error", p.center,
              f"pad {p.number}: annulus {ann:.3f} mm != datasheet "
              f"{float(land['annulus_mm']):g} mm (tol {size_tol})",
              "annulus_mismatch", expected_mm=float(land["annulus_mm"]),
              measured_mm=round(ann, 4))

    # --- mechanical drawing (the extraction's `mechanical` block): leads in
    # their holes, a hole per locating post - check_connector_fit's rules,
    # here on the library footprint before it reaches a board
    mech = ds.get("mechanical") or {}
    mech_fact: dict = {}
    if mech.get("pins") or mech.get("posts"):
        import check_connector_fit as ccf
        local = ccf.LocalFrame(fp.name)
        used = ccf.check_pins(fp.name, local, fp.pads, mech, viol, mech_fact)
        ccf.check_posts(fp.name, local, fp.pads, mech, used, viol, mech_fact)

    # --- stroked closed primitives in a custom pad (warning): KiCad 10.0.6's
    # DRC and connectivity read only a stroked gr_circle's centre line, the
    # Gerber the whole ring, so DRC can miss a short on it (#1133, a TDK T5848
    # GND ring)
    for p in copper:
        if p.stroked_prims:
            v("warning", p.center,
              f"pad {p.number}: {', '.join(p.stroked_prims)} is stroked, not "
              "filled. KiCad 10.0.6's DRC and connectivity saw only the centre "
              "line of a stroked gr_circle while the Gerber plots the full stroke "
              "(#1133), so a short on it can pass DRC; gr_poly / gr_rect are "
              "likely the same. Rebuild it as filled gr_poly (a ring: two "
              "half-annuli overlapping at the seams)",
              "stroked_pad_primitive", primitives=p.stroked_prims)

    # --- courtyard presence (warning; LEARNINGS: easyeda2kicad sometimes omits it)
    if not fp.has_courtyard:
        v("warning", None, "no courtyard layer (F/B.CrtYd) - courtyard-based "
          "DRC/overlap checks will silently degrade", "no_courtyard")

    facts = {
        "footprint": fp.name,
        "footprint_file": str(footprint),
        "copper_pads": len(copper),
        "total_pads": len(fp.pads),
        "measured_pitch_mm": round(measured_pitch, 4) if measured_pitch else None,
        "pitch_rows": pitch_facts or None,
        "measured_pad_size_mm": list(measured_size) if measured_size else None,
        "pad_size_by_axis": by_axis_fact,
        "two_terminal_land": two_fact,
        "measured_row_spacing_mm": [round(m, 4) for m in measured_rows] or None,
        "has_courtyard": fp.has_courtyard,
        "has_silk": fp.has_layer_kind("SilkS"),
        "land_pattern": land,
        "mechanical": mech_fact or None,
        "pin1": pin1_fact,
        "pad_numbering": numbering_fact,
        "not_judged": not_judged,
        "coverage": coverage(land, not_judged, numbering_fact, len(got)),
    }
    return viol, facts, fp


# ------------------------------------------------------------- board mode

# a placed footprint's coordinate pairs that a flip to the bottom mirrors
_XY_HEADS = ("at", "xy", "start", "end", "mid", "center")
EXTRACTIONS = Path(__file__).resolve().parent.parent / "reference" / "extractions"


def _sym(x):
    import sexpdata
    return sexpdata.Symbol(x)


def _flip_layer(x):
    """B.* <-> F.* (a layer name as a string or a symbol)."""
    s = str(fpg._t(x))
    if s[:2] in ("B.", "F."):
        s = ("F." if s[0] == "B" else "B.") + s[2:]
        return _sym(s) if not isinstance(x, str) else s
    return x


def library_copy(node, rot: float, bottom: bool):
    """A placed footprint (its board node) as its library footprint: the
    origin at 0 0, no turn, and a bottom-side copy unflipped. The board file
    keeps pads in the footprint's own frame but a pad's angle absolute (the
    footprint's turn added), and a flipped footprint's items with y -> -y,
    angles negated and B./F. layers swapped (KiCad's flip; check_fp_geometry
    _frames)."""
    def walk(n, depth, in_pad):
        out = []
        for i, c in enumerate(n):
            if not isinstance(c, list) or not c:
                if bottom and i > 0 and fpg._t(n[0]) in ("layer", "layers"):
                    c = _flip_layer(c)
                out.append(c)
                continue
            h = str(fpg._t(c[0]))
            if depth == 0 and h == "at":
                out.append([_sym("at"), 0, 0])
                continue
            if depth == 0 and h == "layer":
                out.append([_sym("layer"), "F.Cu"])
                continue
            c = walk(c, depth + 1, h == "pad")
            nums = [isinstance(fpg._t(v), (int, float)) for v in c[1:]]
            if h in _XY_HEADS and len(c) >= 3 and nums[0] and nums[1]:
                if bottom:
                    c[2] = -c[2]
                if h == "at" and in_pad:
                    ang = (c[3] if len(c) > 3 and nums[2] else 0) - rot
                    ang = (-ang if bottom else ang) % 360
                    c = c[:3] + ([round(ang, 6)] if abs(ang) > 1e-9 else [])
            elif h == "angle" and bottom and len(c) > 1 and nums[0]:
                c[1] = -c[1]
            out.append(c)
        return out
    return walk(node, 0, False)


def _record_index(dirs: list[Path]) -> dict[str, Path]:
    """Record file by name stem (an LCSC number or MPN) and by its "mpn"
    and "lcsc" fields, the first dir winning (the board's parts dir, then
    reference/extractions). A record landed from a Value (#1406) is named
    by its MPN, and a board may give only the LCSC number."""
    idx: dict[str, Path] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            if f.name == "parts.json":
                continue
            idx.setdefault(f.stem.upper(), f)
            try:
                doc = checklib.load_json(f, f"record {f.name}")
            except checklib.CheckError:
                continue
            for k in ("mpn", "lcsc"):
                if isinstance(doc, dict) and isinstance(doc.get(k), str) \
                        and doc[k].strip():
                    idx.setdefault(doc[k].strip().upper(), f)
    return idx


def _value_index(dirs: list[Path]) -> dict[str, list[Path]]:
    """#1406: every record file under each name (file stem, and "mpn"
    field), case aside, from the first dir that has the name: a Value names
    a record only when exactly one file there carries it, so the files are
    kept, not just the first."""
    idx: dict[str, list[Path]] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        here: dict[str, list[Path]] = {}
        for f in sorted(d.glob("*.json")):
            if f.name == "parts.json":
                continue
            names = {f.stem.upper()}
            try:
                doc = checklib.load_json(f, f"record {f.name}")
            except checklib.CheckError:
                doc = None
            if isinstance(doc, dict) and isinstance(doc.get("mpn"), str):
                names.add(doc["mpn"].strip().upper())
            for n in names - {""}:
                here.setdefault(n, []).append(f)
        for n, fs in here.items():
            idx.setdefault(n, fs)
    return idx


def board_report(pcb: Path, parts: Path | None, only: set[str] | None,
                 pitch_tol: float, size_tol: float) -> tuple[list[dict], dict]:
    """The pad-numbering checks on each footprint placed in a .kicad_pcb
    (#1285): every copy taken back to its library form (a bottom one
    unflipped), its record found by LCSC number or MPN, and verify()'s
    pad_numbering findings kept, under the placed reference and position.
    A part with neither takes its record from its Value when that reads as
    a part number and exactly one record file carries it as its name or
    mpn (#1406); none, or two or more, leaves it not_judged and says so."""
    import tempfile

    import sexpdata
    import check_ratings
    import matinglib
    tree = sexpdata.loads(Path(pcb).read_text(encoding="utf-8"))
    rows = matinglib.load_parts(parts)
    dirs = ([parts] if parts else []) + [EXTRACTIONS]
    idx = _record_index(dirs)
    vidx = _value_index(dirs)
    viols: list[dict] = []
    placed: list[dict] = []
    judged = Counter()
    with tempfile.TemporaryDirectory(prefix="fp_verify-") as tmp:
        for node in fpg._kids(tree, "footprint"):
            props = {str(fpg._t(p[1])): str(fpg._t(p[2]))
                     for p in fpg._kids(node, "property") if len(p) > 2}
            for t in fpg._kids(node, "fp_text"):
                if len(t) > 2 and str(fpg._t(t[1])) == "reference":
                    props.setdefault("Reference", str(fpg._t(t[2])))
            ref = props.get("Reference", "?")
            if only and ref not in only:
                continue
            at = fpg._nums(fpg._kid(node, "at"))
            rot = at[2] if len(at) > 2 else 0.0
            side = str(fpg._t((fpg._kid(node, "layer") or [None, "F.Cu"])[1]))
            bottom = side.startswith("B.")
            row = rows.get(ref) or {}
            keys = [fpg.lcsc_of(SimpleNamespace(fields=props)),
                    str(row.get("lcsc") or ""), matinglib.part_mpn(props, row)]
            rec = next((idx[k.strip().upper()] for k in keys
                        if k and k.strip().upper() in idx), None)
            # no LCSC number and no MPN: the Value, on one exact match only
            value = "" if any((k or "").strip() for k in keys) else \
                check_ratings.value_mpn({"value": props.get("Value")})
            by_value = vidx.get(value.upper(), []) if value else []
            if len(by_value) == 1:
                rec = by_value[0]
            no_rec = (
                f"its Value {value} names {len(by_value)} records "
                f"({', '.join(f.name for f in by_value)}); none is picked"
                if len(by_value) > 1 else
                f"no record named by its Value {value} (the parts dir, then "
                "reference/extractions)" if value else
                "no record found by its LCSC number or MPN (the parts dir, "
                "then reference/extractions), and not a lettered ball grid")
            ds = checklib.load_json(rec, f"record {rec.name}") if rec else {}
            mod = Path(tmp) / f"{len(placed)}.kicad_mod"
            mod.write_text(sexpdata.dumps(library_copy(node, rot, bottom)),
                           encoding="utf-8")
            try:
                found, facts, _ = verify(mod, ds if isinstance(ds, dict) else {},
                                         pitch_tol, size_tol)
            except Exception as e:  # noqa: BLE001  (one odd footprint, not the board)
                placed.append({"ref": ref, "footprint": str(fpg._t(node[1])),
                               "side": "bottom" if bottom else "top",
                               "unreadable": str(e)[:200]})
                continue
            num = facts["pad_numbering"]
            if num:
                judged[num["from"]] += 1
            for v in found:
                if not str(v.get("kind") or "").startswith(
                        ("pad_numbering", "bga_numbering")):
                    continue
                v["refs"] = [ref]
                v["footprint"] = str(fpg._t(node[1]))
                v["side"] = "bottom" if bottom else "top"
                if len(at) >= 2:
                    v["pos"] = [at[0], at[1]]
                v["layer"] = side
                viols.append(v)
            placed.append({
                "ref": ref, "footprint": str(fpg._t(node[1])),
                "side": "bottom" if bottom else "top",
                "record": str(rec) if rec else None, "pad_numbering": num,
                **({"found_by": "value"} if rec and value else {}),
                "not_judged": ([n["why"] for n in facts["not_judged"]
                                if n["check"] == "pad_numbering"] if rec
                               else [no_rec] if not num
                               else []) or None})
    n = sum(judged.values())
    facts = {"placed": len(placed), "judged": dict(judged),
             "parts": placed,
             "nothing_to_judge": None if viols or n else (
                 "no placed footprint could have its pad numbering judged: "
                 "none is a lettered ball grid and none has a record with "
                 "land_pattern.pads or numbering")}
    return viols, facts


# ------------------------------------------------------------------ SVG overlay

def _pad_corners(p: fplib.Pad) -> list[tuple[float, float]]:
    cx, cy, rot = p.at
    w, h = p.size
    ca, sa = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    pts = []
    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        pts.append((cx + dx * ca - dy * sa, cy + dx * sa + dy * ca))
    return pts


def write_svg(path: Path, fp: fplib.Footprint, bad_pads: set[str],
              land: dict, scale: float = 40.0, margin: float = 10.0) -> None:
    """Overlay: copper pads (rotated rects) with numbers + size labels.

    Pads flagged as geometry mismatches are drawn red; others blue. A header
    line states measured vs datasheet-expected count/pitch/size.
    """
    # The overlay is written before the report, whose writer makes the
    # --out folder: make it here too, or a new folder ends in exit 2
    path.parent.mkdir(parents=True, exist_ok=True)
    copper = fp.copper_pads
    all_pts = [pt for p in copper for pt in _pad_corners(p)]
    if not all_pts:
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" '
                        'height="40"><text x="5" y="20">no copper pads</text></svg>',
                        encoding="utf-8")
        return
    xs = [x for x, _ in all_pts]
    ys = [y for _, y in all_pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    header = 46.0

    def sx(x):
        return (x - minx) * scale + margin

    def sy(y):
        return (y - miny) * scale + margin + header

    width = (maxx - minx) * scale + 2 * margin
    height = (maxy - miny) * scale + 2 * margin + header
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
             f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}">',
             '<rect width="100%" height="100%" fill="white"/>']
    exp_ct = land.get("pad_count", "?")
    exp_pitch = land.get("pitch_mm", "?")
    parts.append(f'<text x="{margin}" y="16" font-family="monospace" '
                 f'font-size="12" fill="black">{fp.name}: {len(copper)} pads '
                 f'(datasheet {exp_ct}), pitch/size from datasheet '
                 f'{exp_pitch}mm</text>')
    parts.append(f'<text x="{margin}" y="32" font-family="monospace" '
                 f'font-size="11" fill="#b00">red = geometry mismatch</text>'
                 if bad_pads else
                 f'<text x="{margin}" y="32" font-family="monospace" '
                 f'font-size="11" fill="#080">all pads within tolerance</text>')
    for p in copper:
        corners = _pad_corners(p)
        pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in corners)
        bad = p.number in bad_pads
        fill = "#f8caca" if bad else "#cfe0f8"
        stroke = "#b00000" if bad else "#20509a"
        parts.append(f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" '
                     f'stroke-width="1"/>')
        parts.append(f'<text x="{sx(p.center[0]):.1f}" y="{sy(p.center[1]):.1f}" '
                     f'font-family="monospace" font-size="10" fill="black" '
                     f'text-anchor="middle" dominant-baseline="central">'
                     f'{p.number}</text>')
        parts.append(f'<text x="{sx(p.center[0]):.1f}" y="{sy(p.center[1]) + 11:.1f}" '
                     f'font-family="monospace" font-size="7" fill="#555" '
                     f'text-anchor="middle">{p.size[0]:g}x{p.size[1]:g}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--footprint", help=".kicad_mod file")
    ap.add_argument("--datasheet-json",
                    help="datasheet-extract JSON (uses its land_pattern block)")
    ap.add_argument("--pcb", help="instead of one footprint: a .kicad_pcb; "
                    "runs the pad-numbering checks on each placed footprint "
                    "(a bottom one unflipped), its record found in --parts "
                    "or reference/extractions by LCSC number or MPN")
    ap.add_argument("--parts", help="with --pcb: the board's parts dir "
                    "(parts.json and per-part records)")
    ap.add_argument("--ref", action="append",
                    help="with --pcb: only this reference (repeatable)")
    ap.add_argument("--pairs", help="instead of one footprint: a file of "
                    "FOOTPRINT<TAB>RECORD lines; reports per check how many "
                    "were judged, not judged and not stated, and each error")
    ap.add_argument("--svg", help="write the overlay SVG here (default: "
                    "<report stem>.overlay.svg beside --out; none without "
                    "--out, so nothing is written into the library)")
    ap.add_argument("--pitch-tol-mm", type=float, default=0.05)
    ap.add_argument("--size-tol-mm", type=float, default=0.1)
    ap.add_argument("--out", help="write JSON report here instead of stdout")
    return ap


def run(argv: list[str] | None = None):
    """The SPEC section 6 entry the gate calls (checks.d): (payload, out)."""
    args = _parser().parse_args(argv)
    if args.pcb:
        pcb = Path(args.pcb)
        if not pcb.is_file():
            raise checklib.CheckError(f"board not found: {pcb}")
        viol, facts = board_report(
            pcb, Path(args.parts) if args.parts else None,
            set(args.ref) if args.ref else None,
            args.pitch_tol_mm, args.size_tol_mm)
        payload = checklib.report("fp_verify", pcb, viol, **facts)
        payload["status"] = ("violations" if any(
            v["severity"] == "error" for v in viol) else "pass")
        return payload, args.out
    if args.pairs:
        rows = [ln.split("\t")[:2] for ln in Path(args.pairs).read_text(
            encoding="utf-8").splitlines() if ln.strip()
            and not ln.startswith("#")]
        rep = coverage_report([(a.strip(), b.strip()) for a, b in rows],
                              args.pitch_tol_mm, args.size_tol_mm)
        payload = checklib.report("fp_verify", Path(args.pairs), [], **rep)
        payload["status"] = "violations" if rep["errors"] else "pass"
        return payload, args.out
    if not (args.footprint and args.datasheet_json):
        raise checklib.CheckError(
            "give --footprint and --datasheet-json, or --pairs")
    fp_path = Path(args.footprint)
    if not fp_path.exists():
        raise checklib.CheckError(f"footprint not found: {fp_path}")
    ds = checklib.load_json(args.datasheet_json, "datasheet-json")
    viol, facts, fp = verify(fp_path, ds, args.pitch_tol_mm, args.size_tol_mm)

    # Never beside the footprint: a .svg in a .pretty folder is a stray
    # file in the library (#1066)
    svg_path = (Path(args.svg) if args.svg else
                Path(args.out).with_suffix(".overlay.svg") if args.out
                else None)
    if svg_path is not None:
        # A pitch/size finding implicates the whole pad field; count/pin1
        # don't point at a specific pad, so nothing is reddened for those.
        flag = any(v["kind"] in ("pad_pitch", "pad_size", "row_spacing",
                                 "pad_turned", "land_gap")
                   for v in viol)
        bad_pads = {p.number for p in fp.copper_pads} if flag else set()
        write_svg(svg_path, fp, bad_pads, facts["land_pattern"])
        facts["svg"] = str(svg_path)

    has_error = any(v["severity"] == "error" for v in viol)
    payload = checklib.report("fp_verify", fp_path, viol, **facts)
    payload["status"] = "violations" if has_error else "pass"
    return payload, args.out


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("fp_verify", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
