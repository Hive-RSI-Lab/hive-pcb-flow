"""check_courtyard.py - every footprint's courtyard covers its own pads
(P8 verify suite).

The hit: EasyEDA's 0603 footprints (pulled by lib_pull) ship their body
outline, +-0.80 x +-0.40 mm, as the courtyard, inside pads that reach
+-1.15 mm. KiCad's DRC tests courtyards only against each other, so a part
whose courtyard is smaller than its own copper passes every gate while a
neighbour may sit on its pads (hwde-led-test, three parts). placelib's
legality checks widen such a courtyard to the pad field internally; this
check makes the footprint itself say so.

One finding per footprint whose declared courtyard (F.CrtYd, or B.CrtYd for
a back-side part, as placelib reads it) leaves some of its pad copper
outside, by more than TOL_MM2:
  courtyard_pads   error: the pads the courtyard misses and by how much
A footprint with no courtyard at all is fp_verify's warning, not this
check's (stock mounting holes and logos have none).

The repair belongs in the footprint, not the board: fpfix rule E (run by
lib_pull) rebuilds a pulled courtyard that misses its pads as the pad field
plus 0.25 mm; a board made before then gets its footprints re-pulled or
fixed with `fpfix.py --lib` and updated from the library.

CLI: --pcb board.kicad_pcb [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from shapely import affinity
from shapely.ops import unary_union

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
import placelib  # noqa: E402

SCRIPT = "check_courtyard"
SOURCE = SCRIPT  # verify_all, gate and the scorecard match a finding to its check by source
# copper outside the courtyard below this (mm2) is arc sampling, not a miss
TOL_MM2 = 1e-3


def courtyard_abs(f: "placelib.Footprint"):
    """The footprint's declared courtyard in the board frame, or None."""
    if f.courtyard_local is None:
        return None
    p = affinity.rotate(f.courtyard_local, -f.angle, origin=(0, 0))
    return affinity.translate(p, f.pos[0], f.pos[1])


def check(pcb: Path) -> tuple[list[dict], dict]:
    bg = geom.load_board(pcb)
    model = placelib.PlaceModel(pcb)
    pads: dict[str, list] = {}
    for p in bg.pads_of():
        pads.setdefault(p.ref, []).append(p)
    viols = []
    seen = {"footprints": 0, "with_courtyard": 0, "no_courtyard": [],
            "duplicate_refs": sorted(bg.dup_refs)}
    for ref in sorted(model.footprints):
        f = model.footprints[ref]
        seen["footprints"] += 1
        cy = courtyard_abs(f)
        if cy is None and pads.get(ref):
            seen["no_courtyard"].append(ref)
        # a ref two parts share pools both parts' pads against one
        # courtyard: no verdict, listed in duplicate_refs
        if cy is None or not pads.get(ref) or ref in bg.dup_refs:
            continue
        seen["with_courtyard"] += 1
        missed = []
        for p in pads[ref]:
            out = p.poly.difference(cy).area
            if out > TOL_MM2:
                missed.append((p.number, out))
        if not missed:
            continue
        copper = unary_union([p.poly for p in pads[ref]])
        cx1, cy1, cx2, cy2 = cy.bounds
        px1, py1, px2, py2 = copper.bounds
        names = ", ".join(f"{n} ({a:.2f} mm2)" for n, a in missed)
        viols.append(checklib.violation(
            SCRIPT, "error", f.pos, "F.CrtYd" if f.side == "front"
            else "B.CrtYd", None, [ref],
            f"{ref} ({f.fpid}): the courtyard "
            f"({cx2 - cx1:.2f} x {cy2 - cy1:.2f} mm) leaves pad(s) {names} "
            f"outside it; the pads span {px2 - px1:.2f} x {py2 - py1:.2f} mm. "
            f"Fix the footprint (fpfix rule E) and update it from the library",
            SOURCE, kind="courtyard_pads", footprint=f.fpid,
            pads_outside=[n for n, _ in missed],
            courtyard_bbox=[checklib.rnd(v) for v in (cx1, cy1, cx2, cy2)],
            pads_bbox=[checklib.rnd(v) for v in (px1, py1, px2, py2)]))
    return viols, seen


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    viols, seen = check(pcb)
    return checklib.report(
        SCRIPT, pcb, viols, checked=[seen],
        nothing_to_judge=None if seen["with_courtyard"] else
        "no footprint with pads has a courtyard",
        not_judged=seen["no_courtyard"] if seen["with_courtyard"] else None
        ), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
