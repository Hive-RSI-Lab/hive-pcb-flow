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
                2026-07-28 SSOP-20)
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
                drawing) the numbered pads must fit them under a quarter
                turn, no mirror, within the size tolerance; else each pad
                on another pin's place is named, with hint
                bottom_view_numbering (the mirror fits) or numbering_rotated
                (a one-step shift fits). A letter/number ball grid must be
                numbered as its top view (A1 top left, numbers rising
                rightwards, letters downwards, under any turn). With
                land_pattern.numbering "ccw_top" the numeric pads must run
                counter-clockwise from the top. With none of these the
                check goes to not_judged. Example: SiTime's 3.2 x 2.5 mm
                PQFN-4 oscillator has pins 1 and 2 on a LONG side (SiT1602B
                POD-PQFN-004-X03225-002); numbering round the short side
                puts VDD on OE and the clock never starts

Exit (SPEC section 6): 0 = no error-severity findings (warnings allowed),
1 = one or more errors, 2 = unreadable footprint/JSON.

Examples:
  fp_verify.py --footprint lib/aiee.pretty/C0402.kicad_mod --datasheet-json parts/C1525.json
  fp_verify.py --footprint fp.kicad_mod --datasheet-json ds.json --svg out.svg
"""
from __future__ import annotations

import argparse
import math
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import check_fp_geometry as fpg  # noqa: E402  (its frames and fit, #1102)
import checklib  # noqa: E402
import fplib  # noqa: E402

SOURCE = "check.fp_verify"

# JLC minimum annular ring (jlc_capabilities.yaml, the EDITS.md acceptance
# bar); a THT pad below it cannot fab reliably whatever the datasheet says.
ANNULUS_FLOOR_MM = 0.15


# A pad or ball name: "1", "A1", "EP", "SH2". Anything with a space is prose
# (an MGM240P record's "top-left in land pattern view (Fig 8.3); ...", #1127)
PAD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,7}$")
BALL = re.compile(r"^([A-Z]{1,2})(\d{1,3})$")


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
    pads (a SOIC's columns; a QFP's left/right and top/bottom). An axis of
    equal small groups (a SOIC's pitch axis) or a lone biggest group
    (SOT-23) has no row pair."""
    spacings = []
    for axis in (0, 1):
        groups = _clusters([c[axis] for c in centers])
        if len(groups) < 2:
            continue
        top = max(len(g) for g in groups)
        rows = [g for g in groups if len(g) == top]
        if len(rows) != 2:
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
        if abs(g - k * pitch) > tol and g <= 1.5 * pitch:
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


def _fit_turn(ref: dict, got: dict, tol: float) -> int | None:
    """The quarter turn that puts every numbered pad of `got` on its pin
    in `ref` (no mirror; centroids aligned), else None."""
    for f in fpg._frames("top", TURNS):
        if fpg._err(ref, got, f)[0] <= tol:
            return f[1]
    return None


def _shifted(got: dict, s: int) -> dict:
    """`got` with its numeric pads renumbered s steps round (pad k named
    k - s); other names (EP, MP) stay."""
    order = sorted((k for k in got if k.isdigit()), key=int)
    out = {k: v for k, v in got.items() if not k.isdigit()}
    for i, k in enumerate(order):
        out[order[(i - s) % len(order)]] = got[k]
    return out


def _misplaced(ref: dict, got: dict, tol: float) -> list[tuple[str, str]]:
    """(pad, the drawing's pin at that place) for each pad on another pin's
    place, in the frame where the most pads land on their own pin; [] when
    the pads as a set don't fit the drawing (the shape differs)."""
    best = None
    rc = (sum(x for x, _ in ref.values()) / len(ref),
          sum(y for _, y in ref.values()) / len(ref))
    for f in fpg._frames("top", TURNS):
        if not fpg._set_fits(ref, got, f, tol):
            continue
        g = {k: fpg._turn(*f[0](*xy), f[1]) for k, xy in got.items()}
        gc = (sum(x for x, _ in g.values()) / len(g),
              sum(y for _, y in g.values()) / len(g))
        pairs = []
        for k, (x, y) in g.items():
            near = min(ref, key=lambda r: math.hypot(
                x - gc[0] - ref[r][0] + rc[0], y - gc[1] - ref[r][1] + rc[1]))
            if near != k:
                pairs.append((k, near))
        if best is None or len(pairs) < len(best):
            best = pairs
    return sorted(best or [], key=lambda kv: (not kv[0].isdigit(),
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
    return across[0] * down[1] - across[1] * down[0]


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

    # --- pad count
    if "pad_count" in land:
        expected = int(land["pad_count"])
        if len(copper) != expected:
            v("error", None,
              f"copper pad count {len(copper)} != datasheet {expected}",
              "pad_count", expected=expected, measured=len(copper))

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
        if len(common) < 2:
            not_judged.append({"check": "pad_numbering", "why": (
                "land_pattern.pads shares fewer than two pad numbers with "
                "the footprint, so there is nothing to place")})
        elif (turn := _fit_turn(ref, mine, size_tol)) is not None:
            numbering_fact = {"from": "pads", "fit": True, "turn_deg": turn,
                              "pads": len(common)}
        else:
            mirror = {k: (-x, y) for k, (x, y) in ref.items()}
            hint = ("bottom_view_numbering"
                    if _fit_turn(mirror, mine, size_tol) is not None else
                    "numbering_rotated" if any(
                        _fit_turn(ref, _shifted(mine, s), size_tol) is not None
                        for s in (1, -1)) else None)
            moved = _misplaced(ref, mine, size_tol)
            if moved:
                where = "; ".join(f"pad {a} sits where the drawing has pin {b}"
                                  for a, b in moved[:6])
            else:
                worst, at = fpg._err(ref, mine, fpg._frames("top")[0])
                where = (f"the pads don't match the drawing's places under "
                         f"any quarter turn (pad {at} is {worst:.2f} mm off)")
            why = {"bottom_view_numbering": (
                       ": the mirror image of the drawing fits, so the "
                       "pads were numbered from a bottom view"),
                   "numbering_rotated": (
                       ": the numbers are shifted one step round the body")
                   }.get(hint, "")
            numbering_fact = {"from": "pads", "fit": False, "hint": hint,
                              "pads": len(common)}
            v("error", got.get(moved[0][0]) if moved else None,
              f"pad numbering differs from the record's land pattern{why}. "
              f"{where} (tol {size_tol} mm)",
              "pad_numbering", hint=hint,
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
        if abs(row["pitch"] - exp) > pitch_tol:
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
            if abs(measured_pitch - exp) > pitch_tol:
                v("error", centers[0] if centers else None,
                  f"nearest-pad pitch {measured_pitch:.3f} mm != datasheet "
                  f"{exp:.3f} mm (tol {pitch_tol})",
                  "pad_pitch", expected_mm=exp,
                  measured_mm=round(measured_pitch, 4))
        elif rest:
            near = [r for r in rest if abs(r["pitch"] - exp) <= pitch_tol]
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
        elif all(abs(m - exp) > size_tol for m in measured_rows):
            got = ", ".join(f"{m:.3f}" for m in measured_rows)
            v("error", None,
              f"pad row spacing {got} mm != datasheet {exp:g} mm "
              f"(tol {size_tol}, centre to centre): the rows may miss the "
              "lead feet",
              "row_spacing", expected_mm=exp,
              measured_mm=[round(m, 4) for m in measured_rows])

    # --- pad size (warning): bound MIN and MAX, never the mode - a 10/10 tie
    # on an asymmetric SSOP-20 hid a column 0.25 mm over the datasheet max
    # (LEARNINGS 2026-07-28 [librarian][easyeda2kicad])
    if land.get("pad_size_mm") and copper:
        exp = tuple(sorted(float(x) for x in land["pad_size_mm"]))
        distinct = sorted({tuple(round(v_, 3) for v_ in sorted(p.size))
                           for p in copper})
        extremes = [distinct[0]] if len(distinct) == 1 else [distinct[0], distinct[-1]]
        offending = [list(s) for s in extremes
                     if any(abs(m - e) > size_tol for m, e in zip(s, exp))]
        if offending:
            v("warning", None,
              f"pad size(s) {offending} mm != datasheet {list(exp)} mm "
              f"(tol {size_tol}; min/max of {len(distinct)} distinct sizes)",
              "pad_size", expected_mm=list(exp), measured_mm=offending)

    # --- THT annulus (error): drill-aware; a wrong annulus passed silently
    # before fplib parsed (drill ...) at all (ladder rows 111/115)
    for p in copper:
        if p.ptype != "thru_hole" or p.drill is None:
            continue
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
        if land.get("drill_mm") is not None and \
                abs(p.drill - float(land["drill_mm"])) > size_tol:
            v("error", p.center,
              f"pad {p.number}: drill {p.drill:g} mm != datasheet "
              f"{float(land['drill_mm']):g} mm (tol {size_tol})",
              "drill_mismatch", expected_mm=float(land["drill_mm"]),
              measured_mm=p.drill)
        if land.get("annulus_mm") is not None and \
                abs(ann - float(land["annulus_mm"])) > size_tol:
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
        "measured_row_spacing_mm": [round(m, 4) for m in measured_rows] or None,
        "has_courtyard": fp.has_courtyard,
        "has_silk": fp.has_layer_kind("SilkS"),
        "land_pattern": land,
        "mechanical": mech_fact or None,
        "pin1": pin1_fact,
        "pad_numbering": numbering_fact,
        "not_judged": not_judged,
    }
    return viol, facts, fp


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--footprint", required=True, help=".kicad_mod file")
    ap.add_argument("--datasheet-json", required=True,
                    help="datasheet-extract JSON (uses its land_pattern block)")
    ap.add_argument("--svg", help="write the overlay SVG here (default: "
                    "<report stem>.overlay.svg beside --out; none without "
                    "--out, so nothing is written into the library)")
    ap.add_argument("--pitch-tol-mm", type=float, default=0.05)
    ap.add_argument("--size-tol-mm", type=float, default=0.1)
    ap.add_argument("--out", help="write JSON report here instead of stdout")

    def run():
        args = ap.parse_args(argv)
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
            flag = any(v["kind"] in ("pad_pitch", "pad_size", "row_spacing")
                       for v in viol)
            bad_pads = {p.number for p in fp.copper_pads} if flag else set()
            write_svg(svg_path, fp, bad_pads, facts["land_pattern"])
            facts["svg"] = str(svg_path)

        has_error = any(v["severity"] == "error" for v in viol)
        payload = checklib.report("fp_verify", fp_path, viol, **facts)
        payload["status"] = "violations" if has_error else "pass"
        return payload, args.out

    return checklib.cli_wrap("fp_verify", run)


if __name__ == "__main__":
    sys.exit(main())
