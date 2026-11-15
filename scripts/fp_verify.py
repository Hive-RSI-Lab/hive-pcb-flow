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
  - pad_pitch : nearest-neighbour pad spacing vs pitch_mm          (error)
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
  - pad_numbering: with land_pattern.pads (where each pin sits in the  (error)
                datasheet's top view) the record is fitted onto the pads
                (quarter turns, and the angle the pin numbers point to, with
                a shift; mirrored too, to name the cause) and each spot's
                pad must carry the record's pin. `cause`:
                bottom_view_numbering (the numbers fit the record mirrored),
                numbering_rotated (they fit it turned: numbered from the
                wrong corner), mirrored_footprint (the pads fit only
                mirrored), numbers_differ. Still an error when the record's
                own pins run clockwise and the footprint's don't: the message
                says the record may be the fault, but a part that really
                numbers clockwise must not pass on a wrong footprint
  - pad_numbering_tied: after a pass, a record pin on a pad the       (warning)
                footprint numbers otherwise, where a datasheet may tie the
                two: the record's EP on a tab the footprint numbers 2
                (`rule` shared), or a pin the record lists twice on a pad
                numbered 4 or unnumbered, which gets no net (`rule`
                repeated). Check the datasheet; the fit itself passed
  - pad_positions_unmatched: land_pattern.pads fit the pads at no     (warning)
                turn tried, straight or mirrored (check the record);
                numbering is then judged by direction, if the family
                allows, else unchecked
  - pad_positions_off_axis: land_pattern.pads share no row or column  (warning)
                while the pads do and fit no way that passes: the record is
                turned by an angle that is no quarter turn, and its pin
                numbers don't point to one that fits; judged as above
  - pad_names_unmatched: land_pattern.pads share under half their     (warning)
                pin numbers with the pads (named VDD/GND, say, where the
                footprint numbers them); judged as above
  - pad_numbering_direction: no per-pin data (or after a pass): on a  (warning)
                perimeter package (SOIC, SOJ, SOP, SOT, SC-70, DFN, SON,
                QFN, QFP, LGA, PowerPAK ...; lib/padnum.py lists them)
                top-view numbers run counter-clockwise from pin 1;
                clockwise means numbered from the bottom view. An error
                when land_pattern.numbering says the other way
  - bga_numbering_mirrored: on a BGA/CSP/WLP seen from the top with   (warning)
                A1 top left, numbers run right and letters down; one of
                the two flipped means numbered from the bottom view
  - land_pattern_invalid: a land_pattern.pads entry the check can't   (warning)
                read (not an object, no number, at_mm not two finite
                numbers: left out; a bad size_mm: size ignored), pads not
                a list, or a numbering other than ccw_top / cw_top
                (ignored). `field` says which

Pad numbering (lib/padnum.py; facts.pad_numbering says what was judged and
why not). The geometry checks never read a pad number, so these catch a
footprint that matches the land pattern pad for pad with the pins on the
wrong pads. Example: on SiTime's 3.2 x 2.5 mm PQFN-4 oscillator, pins 1 and 2
share a LONG side (2.1 mm apart, SiT1602B drawing POD-PQFN-004-X03225-002);
numbering round the short side puts VDD on OE and OUT on the supply, and the
clock never starts. Those numbers still run counter-clockwise, so only
land_pattern.pads catches it. The direction is read from the pins round
the outline: tabs and exposed pads are left out, with the drilled pads
numbered like them (thermal vias), and a ring whose top number is over
twice its count of numbers (13, 4, 58: names, not pin numbers) gets no
direction. The direction checks are warnings, not errors, because some
parts really number clockwise in the top view: measured 2026-11-15 they
flag 0 of 1536 perimeter footprints in KiCad's stock library (17 give no
direction) and 1 of 303 BGAs (a WLCSP-6, not yet confirmed), and 3 of 456
pulled ones: two Bosch LGA-8s (BME280), which really number clockwise, and
an SO-6 not yet confirmed. A record that says which way
(land_pattern.numbering: cw_top) silences that and makes a mismatch an
error. Left alone: connectors (by the footprint's name), PLCCs, SOT-227
modules, through-hole footprints (lib/fpattr.py's rule), unnamed families
(chips, crystals, modules) and names that declare their numbering
(Clockwise, CW, Reverse, SOT-143R, Pin_Swap, pinswapped).

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
import checklib  # noqa: E402
import fplib  # noqa: E402
import padnum  # noqa: E402

SOURCE = "check.fp_verify"

# JLC minimum annular ring (jlc_capabilities.yaml, the EDITS.md acceptance
# bar); a THT pad below it cannot fab reliably whatever the datasheet says.
ANNULUS_FLOOR_MM = 0.15

# findings that name the pads carrying the wrong pin (reddened in the overlay)
NUMBERING_KINDS = ("pad_numbering", "pad_numbering_tied",
                   "pad_numbering_direction", "bga_numbering_mirrored")


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


_TURNS = {1: "a quarter turn counter-clockwise", -1: "a quarter turn clockwise",
          2: "a half turn"}
_WAY = {"ccw": "counter-clockwise", "cw": "clockwise"}
_CAUSE = {
    "numbering_rotated": "the numbers would fit the record turned {turns} from "
                         "where its pins fit the pads: numbered from the wrong "
                         "corner or round the wrong side",
    "bottom_view_numbering": "the numbers fit the record mirrored: numbered "
                             "from the datasheet's bottom view",
    "mirrored_footprint": "the pads fit the record only mirrored: the "
                          "footprint was drawn from the datasheet's bottom view",
    "numbers_differ": "the numbers fit the record at no turn tried, "
                      "straight or mirrored",
}


def _turn_words(fit: dict) -> str:
    """fit_record's numbering_rotated turn in words."""
    if fit["turns"] in _TURNS:
        return _TURNS[fit["turns"]]
    d = fit.get("turn_deg") or 0.0
    way = "counter-clockwise" if d > 0 else "clockwise"
    return f"about {abs(d):.0f} degrees {way}" if abs(d) >= 1 else \
        f"about {abs(d):.1f} degrees {way}"


def _nums(nums) -> str:
    return "/".join(n if n else "(unnumbered)" for n in nums)


def _tie_message(tie: dict) -> str:
    """Why a pin on a pad numbered otherwise passed, and what to check."""
    pin, nums = tie["pin"], [n for n in tie["pads"] if n]
    if tie["rule"] == "shared":
        n = _nums(nums)
        return (f"the record's pin {pin} sits on pad {n}, a tab or exposed "
                f"pad: the footprint ties it to pin {n}; check that the "
                f"datasheet connects {pin} to pin {n}")
    if not nums:
        return (f"the record lists pin {pin} here as well, and this pad has "
                "no number, so it gets no net: number it "
                f"{pin} unless the datasheet leaves it unconnected")
    n = _nums(nums)
    return (f"the record lists pin {pin} here as well, and the footprint "
            f"numbers this pad {n}, which the record doesn't use: check that "
            f"the datasheet ties it to pin {pin} and that the symbol has a "
            f"pin {n}, or the pad gets no net")


def _through_hole(fp: fplib.Footprint) -> bool:
    """A through-hole part by lib/fpattr.py's rule: its pads, when they are
    all one kind (a plated hole inside an SMD pad is that pad's thermal via,
    not a pin), else its (attr). fpattr pulls in shapely, so it loads here."""
    import fpattr
    import sexpdata
    v = fpattr.judge(sexpdata.loads(
        Path(fp.path).read_text(encoding="utf-8", errors="replace")))
    return (v["pads_say"] or v["declared"]) == "through_hole"


def _check_numbering(fp: fplib.Footprint, ds: dict, land: dict, v,
                     size_tol: float = 0.1) -> dict:
    """Which pad carries which pin (lib/padnum.py). With land_pattern.pads
    (the record's top-view pin positions) every pin's spot is compared,
    within size_tol (--size-tol-mm) at least; else, and after a pass, the
    direction the numbers run is judged by package family. Emits through
    `v`; returns the facts."""
    # library footprints are drawn from the front: a pad whose only copper
    # is B.Cu (an exposed pad's back, a far-side test point) is no pin's land
    copper = [p for p in fp.copper_pads
              if [ly for ly in p.layers if ly.endswith(".Cu")] != ["B.Cu"]]
    declared = land.get("numbering")
    if declared is not None and declared not in ("ccw_top", "cw_top"):
        v("warning", None,
          f"land_pattern.numbering is {declared!r}, not ccw_top or cw_top: "
          "ignored", "land_pattern_invalid", field="numbering")
        declared = None
    expected = "cw" if declared == "cw_top" else "ccw"
    family, why = padnum.classify(
        fp.name, *(n for n in (land.get("package"), ds.get("package"))
                   if isinstance(n, str)))
    if family is not None and _through_hole(fp):
        family, why = None, "through_hole"
    skipped: list = []
    rec = padnum.record_pads(land, skipped)
    if skipped:
        said = "; ".join(
            s["why"] if s["index"] is None else
            f"pads[{s['index']}]" + (f" (pin {s['number']})" if s["number"] else "")
            + f": {s['why']}" for s in skipped[:4])
        if len(skipped) > 4:
            said += f"; {len(skipped) - 4} more"
        given = land.get("pads")
        compared = (f"{len(rec)} of {len(given)} record pins compared"
                    if isinstance(given, list) else "No record pin compared")
        v("warning", None, f"{said}. {compared}; fix the record",
          "land_pattern_invalid", field="pads", skipped=skipped)
    fit = padnum.fit_record(copper, rec, size_tol) if rec else None
    fp_dir = padnum.judge(copper, family) if family else None
    rec_dir = padnum.judge(padnum.record_as_pads(rec), family) \
        if family and rec else None
    # the record decides when its pins fit the pads; else the direction does
    by_record = bool(fit) and fit["verdict"] in ("pass", "numbering")
    fact: dict = {"method": "record" if by_record else
                  "direction" if fp_dir else None,
                  "family": family, "why_not": None if family else why,
                  "expected": _WAY[expected], "declared": declared}

    def way(d):
        """'cw'/'ccw' for a perimeter result, else None."""
        return d.get("direction") if d and d["family"] == "perimeter" else None

    if fp_dir:
        fact["direction"] = {k: fp_dir[k] for k in
                             ("family", "direction", "up", "down", "order",
                              "turn", "mirrored") if k in fp_dir}
    if rec_dir:
        fact["record_direction"] = way(rec_dir) if rec_dir["family"] == "perimeter" \
            else ("mirrored" if rec_dir["mirrored"] else "top")
    if fit:
        place = fit["placement"] or {}
        fact.update(result=fit["verdict"], cause=fit["cause"], turns=fit["turns"],
                    cost_mm=fit["cost"], tol_mm=fit["tol"],
                    shared_names=fit["shared_names"],
                    turn_deg=fit["turn_deg"],
                    record_rotation_deg=place["turn_deg"] if place else None,
                    record_mirrored=place.get("mirrored"),
                    renames=[{"pin": r["pin"], "pads": r["pads"]}
                             for r in fit["renames"]],
                    ties=[{"pin": t["pin"], "pads": t["pads"], "rule": t["rule"]}
                          for t in fit["ties"]])
        if fit["verdict"] == "pass":
            for t in fit["ties"]:
                v("warning", t["at"], _tie_message(t), "pad_numbering_tied",
                  pads=list(t["pads"]), pin=t["pin"], rule=t["rule"])
        if fit["verdict"] == "numbering":
            mm = fit["mismatches"]
            spots = "; ".join(f"pad {_nums(m['pads'])} sits where the datasheet's "
                              f"top view has pin {m['pin']}" for m in mm[:4])
            if len(mm) > 4:
                spots += f"; {len(mm) - 4} more"
            hint = _CAUSE[fit["cause"]].format(turns=_turn_words(fit))
            if fit["cause"] in ("bottom_view_numbering", "mirrored_footprint"):
                hint += (" (if the record was copied from a bottom view or "
                         "with y up, fix the record instead)")
            if way(rec_dir) not in (None, expected) and way(fp_dir) == expected:
                # still an error: some parts really number clockwise, and
                # the record is what the footprint is checked against
                hint += (f"; the record's own pins run {_WAY[way(rec_dir)]} "
                         f"and the footprint's {_WAY[expected]}: check the "
                         "record first (a record copied from a bottom view "
                         "runs the wrong way); if the part really numbers "
                         f"{_WAY[way(rec_dir)]}, the footprint is wrong")
            elif rec_dir and fp_dir and fp_dir["family"] == rec_dir["family"] \
                    == "bga" and rec_dir["mirrored"] and not fp_dir["mirrored"]:
                # every ball grid's top view runs the same way: the record
                # is the likelier fault, but it is what the footprint is
                # checked against
                hint += ("; the record's ball rows and columns run as in a "
                         "bottom view and the footprint's as in the top view: "
                         "check the record first")
            bad = {n for m in mm for n in m["pads"]}
            if fit["cause"] == "mirrored_footprint":
                # drawn from the bottom view, pads and numbers mirrored
                # together, it can match the mirrored record spot for spot:
                # the pads are wrong, not their numbers. Those off their pins
                # with the record laid straight, or all when that tells none
                # apart (padnum._misplaced)
                bad |= set(fit["misplaced"] or (p.number for p in copper))
            bad = sorted(bad)
            v("error", mm[0]["at"] if mm else None,
              f"{spots}: {hint}" if spots else hint,
              "pad_numbering", pads=bad, cause=fit["cause"], turns=fit["turns"],
              turn_deg=fit["turn_deg"],
              mismatches=[{"pin": m["pin"], "pads": m["pads"]} for m in mm])
            return fact
        # without a direction to fall back on (no package family, too few
        # pads, numbers that run both ways) the numbering went unchecked
        judged = bool(fp_dir) and (fp_dir["family"] == "bga" or
                                   fp_dir.get("direction") is not None)
        rest = ("the numbering was checked only by direction" if judged else
                "the numbering was not checked")
        if fit["verdict"] == "names_differ":
            n_rec = len({r["number"] for r in rec})
            v("warning", None,
              f"land_pattern.pads and the footprint's pads share "
              f"{fit['shared_names']} of the record's {n_rec} pin numbers: "
              "nothing to compare (the record numbers its pins as the "
              f"pinout does: '1', 'A1', 'EP'); {rest}",
              "pad_names_unmatched", shared_names=fit["shared_names"])
        if fit["verdict"] == "unmatched":
            v("warning", None,
              f"land_pattern.pads fit the footprint's pads at no turn tried, "
              f"straight or mirrored (worst pin {fit['cost']:.2f} mm off, limit "
              f"{fit['tol']:.2f} mm): check the record's pad positions; {rest}",
              "pad_positions_unmatched", cost_mm=fit["cost"], tol_mm=fit["tol"])
        if fit["verdict"] == "off_axis":
            v("warning", None,
              "land_pattern.pads share no row or column while the footprint's "
              "pads do, and fit no way that passes: the record looks turned by "
              "other than a quarter turn, and its pin numbers point to no turn "
              "that fits. Redraw it square to the axes; "
              f"{rest}", "pad_positions_off_axis")
    same = (" The record's pins sit the same way (land_pattern.pads matches "
            "pad for pad): if they were copied from a bottom view, fix both."
            if fit and fit["verdict"] == "pass" else "")
    if not fp_dir:
        return fact
    pin1 = next((p.copper_center for p in copper
                 if p.number.strip().upper() in ("1", "A1")), None)
    if fp_dir["family"] == "perimeter":
        got = fp_dir["direction"]
        if got is None or got == expected:
            return fact
        order = ", ".join(str(n) for n in fp_dir["order"][:12])
        if len(fp_dir["order"]) > 12:
            order += ", ..."
        if declared:
            msg = (f"pads numbered {_WAY[got]} seen from the top (round the "
                   f"outline: {order}), but the record's land_pattern.numbering "
                   f"is {declared} ({_WAY[expected]})")
        else:
            msg = (f"pads numbered clockwise seen from the top (round the "
                   f"outline: {order}): top-view numbers run counter-clockwise "
                   "from pin 1, so this looks numbered from the datasheet's "
                   "bottom view; if the part really numbers clockwise (Bosch's "
                   "LGA-8), say so in its record: land_pattern.numbering: cw_top")
        v("error" if declared else "warning", pin1, msg + same,
          "pad_numbering_direction", pads=[str(n) for n in fp_dir["order"]],
          direction=got, expected=expected)
    elif fp_dir["mirrored"]:
        v("warning", pin1,
          "ball rows and columns run the wrong way round: seen from the top "
          "with A1 at the top left, numbers run right and letters down, and "
          "here one of them runs the other way, as when numbered from the "
          "bottom view" + same,
          "bga_numbering_mirrored", pads=sorted({p.number for p in copper}),
          turn=fp_dir["turn"])
    return fact


def verify(footprint: Path, ds: dict, pitch_tol: float,
           size_tol: float) -> tuple[list[dict], dict, fplib.Footprint]:
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

    # --- which pad carries which pin: against the record's top view
    # (land_pattern.pads), else by the way the numbers run
    numbering = _check_numbering(fp, ds, land, v, size_tol)

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
        "pin1": pin1_fact,
        "not_judged": not_judged,
        "pad_numbering": numbering,
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

    Pads flagged as geometry or numbering mismatches are drawn red; others
    blue. A header
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
                 f'font-size="11" fill="#b00">red = geometry or numbering mismatch</text>'
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
        # a numbering finding names its pads
        for x in viol:
            if x["kind"] in NUMBERING_KINDS:
                bad_pads.update(x.get("pads") or [])
        write_svg(svg_path, fp, bad_pads, facts["land_pattern"])
        facts["svg"] = str(svg_path)

        has_error = any(v["severity"] == "error" for v in viol)
        payload = checklib.report("fp_verify", fp_path, viol, **facts)
        payload["status"] = "violations" if has_error else "pass"
        return payload, args.out

    return checklib.cli_wrap("fp_verify", run)


if __name__ == "__main__":
    sys.exit(main())
