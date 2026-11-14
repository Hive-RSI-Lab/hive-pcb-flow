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
  - pad_pitch : nearest-neighbour pad spacing vs pitch_mm          (error)
  - row_spacing: centre-to-centre spacing of the two pad rows vs
                row_spacing_mm, either axis of a quad              (error)
  - pad_size  : MIN and MAX pad size vs pad_size_mm (the mode hid a (warning)
                tied asymmetric column 0.25 mm over max - LEARNINGS
                2026-07-28 SSOP-20)
  - annulus   : THT min over axes of (size-drill)/2 vs the 0.15 mm JLC floor,  (error)
                and vs land_pattern drill_mm/annulus_mm when given
  - courtyard : a courtyard layer is present                       (warning)
  - mechanical: with the extraction's `mechanical` block, every lead fits
                its hole and every locating post has one
                (check_connector_fit's tht_pin_tight, post_holes_missing ...)

Exit (SPEC section 6): 0 = no error-severity findings (warnings allowed),
1 = one or more errors, 2 = unreadable footprint/JSON.

Examples:
  fp_verify.py --footprint lib/aiee.pretty/C0402.kicad_mod --datasheet-json parts/C1525.json
  fp_verify.py --footprint fp.kicad_mod --datasheet-json ds.json --svg out.svg
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import checklib  # noqa: E402
import fplib  # noqa: E402

SOURCE = "check.fp_verify"

# JLC minimum annular ring (jlc_capabilities.yaml, the EDITS.md acceptance
# bar); a THT pad below it cannot fab reliably whatever the datasheet says.
ANNULUS_FLOOR_MM = 0.15


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

    # --- pin 1 present
    pin1 = str(land.get("pin1", "1"))
    if not any(p.number == pin1 for p in copper):
        nums = sorted({p.number for p in copper})
        v("error", None, f"no copper pad numbered '{pin1}' (pads: {nums})",
          "pin1_missing", expected_pin1=pin1)

    # --- pitch
    if land.get("pitch_mm") is not None and measured_pitch is not None:
        exp = float(land["pitch_mm"])
        if abs(measured_pitch - exp) > pitch_tol:
            pos = centers[0] if centers else None
            v("error", pos,
              f"nearest-pad pitch {measured_pitch:.3f} mm != datasheet "
              f"{exp:.3f} mm (tol {pitch_tol})",
              "pad_pitch", expected_mm=exp, measured_mm=round(measured_pitch, 4))

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
        "measured_pad_size_mm": list(measured_size) if measured_size else None,
        "measured_row_spacing_mm": [round(m, 4) for m in measured_rows] or None,
        "has_courtyard": fp.has_courtyard,
        "has_silk": fp.has_layer_kind("SilkS"),
        "land_pattern": land,
        "mechanical": mech_fact or None,
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
    ap.add_argument("--svg", help="write the overlay SVG here "
                    "(default: <footprint>.overlay.svg next to the report)")
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

        svg_path = Path(args.svg) if args.svg else fp_path.with_suffix(".overlay.svg")
        # A pitch/size finding implicates the whole pad field; count/pin1 don't
        # point at a specific pad, so nothing is reddened for those.
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
