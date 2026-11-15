#!/usr/bin/env python
"""bgalib.py - BGA ball fields and their escape budget (#325).

One place answers "which footprints are ball grids, at what pitch, and what
via / track / clearance escapes them at this board's fab floors", so the
router (route_escape.py) and the rule writer (rules_gen.py) can never
disagree about the clearance an escape is laid at:

  find_bgas(bg)               -> [Bga]: footprints whose SMD pads sit on a
                                 regular grid (>= 3 x 3, pitch <= 1.27 mm)
  escape_params(bga, floors)  -> the dog-bone escape geometry and its budget
  dru_rule(ref, clearance)    -> the courtyard-scoped clearance rule text
  fine_via(bga, n, cap, free) -> the fab's smallest filled via for a fine
                                 field on a stack that fills vias free (#1252)
  fine_rule(ref, fv)          -> the courtyard rule that lets that via pass

The budget (all mm, c = escape clearance):
  via     = max(min via diameter, drill + 2 * min annular ring)
  c       = max(min clearance, hole clearance - via ring) - a track laid at
            c from the via copper is then also hole-clear of its drill
  street  = pitch - via >= track + 2c       one track between dog-bone vias
  gap     = pitch / sqrt2 >= pad/2 + via/2 + c   a via fits the diagonal gap
  channel = pitch - pad >= track + 2c       one track between balls (top)
A field that fails `gap` cannot be dog-boned at standard vias (0.5 mm
eMMC); a sparse one may still escape between its NC balls.

Not a CLI - library only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

MAX_PITCH = 1.27      # mm - coarser grids escape like any other part
MIN_ROWS = 3
GRID_TOL = 0.01       # mm - pad centre to grid line
MIN_FILL = 0.25       # balls / grid sites: a depopulated BGA keeps far more
MAX_BALL_ASPECT = 1.5  # long side / short side of a ball pad (QFN leads ~3)
RULE_PREFIX = "aiee_bga_escape_"
# rules_gen names whose clearance an escape rule must never override:
# voltage_rules (HV), package_rules (package ratings), isolation_rules (barriers)
KEEP_LAST = ("aiee_hv_", "aiee_pkg_rating_", "aiee_iso_")

# KiCad board-setup hole clearance when the project sets none
# (rules_gen.KICAD_HOLE_CLEARANCE_MM - the same number).
KICAD_HOLE_CLEARANCE_MM = 0.25

# Fine fields (#1252). JLC's BGA guide (jlcpcb.com/help/article/BGA-Design-
# Guidelines---PCB-Layout-Recommendations-for-BGA-packages) calls a 0.5 mm
# dog-bone not manufacturable and gives 0.25 mm pads with filled via-in-pad;
# its capability page (jlcpcb.com/capabilities/pcb-capabilities) gives the
# via: 0.25/0.15 mm minimum (jlc_capabilities abs_min_via_*), filled and
# capped (POFV, 0.15-0.55 mm vias, the default from 6 layers up), 0.2 mm via
# hole-to-hole, and 0.2 mm via hole to track.
FINE_PITCH_MM = 0.5
FINE_VIA_HOLE_CLEARANCE_MM = 0.2
FINE_RULE_PREFIX = "aiee_bga_fine_via_"


@dataclass
class Bga:
    ref: str
    layer: str                       # F.Cu | B.Cu
    pitch: float
    rows: int
    cols: int
    pad_d: float                     # ball land size (largest side)
    bbox: tuple[float, float, float, float]   # ball centres x0 y0 x1 y1
    balls: int
    nets: dict[str, str] = field(default_factory=dict)   # pad number -> net

    def as_dict(self) -> dict:
        return {"ref": self.ref, "layer": self.layer,
                "pitch_mm": round(self.pitch, 4), "rows": self.rows,
                "cols": self.cols, "pad_mm": round(self.pad_d, 4),
                "balls": self.balls,
                "bbox": [round(v, 4) for v in self.bbox]}


def _pitch(values: list[float]) -> float | None:
    """Smallest spacing of a sorted set of distinct coordinates, if every
    coordinate sits on that lattice; None otherwise."""
    xs = sorted(set(round(v, 3) for v in values))
    if len(xs) < MIN_ROWS:
        return None
    steps = [b - a for a, b in zip(xs, xs[1:])]
    p = min(steps)
    if p <= 0 or p > MAX_PITCH + 1e-9:
        return None
    for v in xs:
        k = (v - xs[0]) / p
        if abs(k - round(k)) * p > GRID_TOL:
            return None
    return p


def _local(pad, fp_at, fp_angle) -> tuple[float, float]:
    """Pad centre in the footprint frame (undo the footprint rotation)."""
    dx, dy = pad.center[0] - fp_at[0], pad.center[1] - fp_at[1]
    # geom places a pad at at + _rot(local, -angle); undo it
    a = math.radians(fp_angle)
    return (dx * math.cos(a) - dy * math.sin(a),
            dx * math.sin(a) + dy * math.cos(a))


def find_bgas(bg, refs: list[str] | None = None) -> list[Bga]:
    """Every footprint whose SMD copper pads form a regular ball grid.
    Detection is geometric (a QFN's perimeter is not a 3 x 3 grid with
    interior balls; a BGA's name is not trusted)."""
    by_ref: dict[str, list] = {}
    for p in bg.pads_of():
        if p.kind == "smd" and p.layers and len(p.layers) == 1:
            by_ref.setdefault(p.ref, []).append(p)
    out: list[Bga] = []
    for ref in sorted(by_ref):
        if refs and ref not in refs:
            continue
        pads = _ball_pads(by_ref[ref])
        if len(pads) < MIN_ROWS * MIN_ROWS:
            continue
        fp = bg.footprints.get(ref, {})
        at = fp.get("at") or _centroid(pads)
        angle = fp.get("angle", 0.0) or 0.0
        loc = [_local(p, at, angle) for p in pads]
        px = _pitch([x for x, _ in loc])
        py = _pitch([y for _, y in loc])
        if px is None or py is None or abs(px - py) > GRID_TOL:
            continue
        xs = sorted(set(round(x, 3) for x, _ in loc))
        ys = sorted(set(round(y, 3) for _, y in loc))
        cols = int(round((xs[-1] - xs[0]) / px)) + 1
        rows = int(round((ys[-1] - ys[0]) / py)) + 1
        # a ball field has interior balls; a perimeter-only ring does not
        interior = [1 for x, y in loc
                    if xs[0] + px / 2 < x < xs[-1] - px / 2
                    and ys[0] + py / 2 < y < ys[-1] - py / 2]
        if rows < MIN_ROWS or cols < MIN_ROWS or not interior:
            continue
        # a QFN's leads on a fine lattice (the two sides offset by half a
        # pitch) fill a few percent of the grid; a ball field fills most of it
        if len(pads) < MIN_FILL * rows * cols:
            continue
        cx = [p.center[0] for p in pads]
        cy = [p.center[1] for p in pads]
        out.append(Bga(
            ref=ref, layer=pads[0].layers[0], pitch=round(min(px, py), 4),
            rows=rows, cols=cols,
            pad_d=max(max(p.size) for p in pads),
            bbox=(min(cx), min(cy), max(cx), max(cy)), balls=len(pads),
            nets={p.number: p.net for p in pads if p.net}))
    return out


def _ball_pads(pads) -> list:
    """The pads that can be balls: near-round, and the common size. A QFN's
    exposed pad (far bigger than its leads) is dropped, so it can't pass as
    an interior ball; a footprint whose typical pad is an oblong lead has
    none."""
    if not pads:
        return []
    def aspect(p):
        a, b = max(p.size), min(p.size)
        return a / b if b > 0 else float("inf")
    sizes = sorted(max(p.size) for p in pads)
    med = sizes[len(sizes) // 2]
    common = [p for p in pads if max(p.size) <= 1.5 * med]
    round_ = [p for p in common if aspect(p) <= MAX_BALL_ASPECT]
    if len(round_) < 0.8 * len(common):
        return []
    return round_


def _centroid(pads) -> tuple[float, float]:
    return (sum(p.center[0] for p in pads) / len(pads),
            sum(p.center[1] for p in pads) / len(pads))


def escape_params(bga: Bga, floors: dict) -> dict:
    """Dog-bone escape geometry at the board's floors.

    floors: track_width, clearance (the fab minimum clearance, NOT a
    netclass value), via_diameter, via_drill, annular, hole_clearance."""
    drill = float(floors["via_drill"])
    annular = float(floors.get("annular") or 0.0)
    via = round(max(float(floors["via_diameter"]), drill + 2 * annular), 4)
    ring = (via - drill) / 2
    hole_cl = float(floors.get("hole_clearance") or KICAD_HOLE_CLEARANCE_MM)
    clearance = round(max(float(floors["clearance"]), hole_cl - ring), 4)
    track = round(float(floors["track_width"]), 4)
    p, pad = bga.pitch, bga.pad_d
    need_street = track + 2 * clearance
    budget = {
        "street": {"have": round(p - via, 4), "need": round(need_street, 4)},
        "gap": {"have": round(p / math.sqrt(2), 4),
                "need": round(pad / 2 + via / 2 + clearance, 4)},
        "channel": {"have": round(p - pad, 4), "need": round(need_street, 4)},
    }
    ok = {k: v["have"] >= v["need"] - 1e-6 for k, v in budget.items()}
    for k in budget:
        budget[k]["ok"] = ok[k]
    if ok["gap"] and ok["street"]:
        method, why = "dogbone", "gap vias and inner streets fit"
    elif ok["channel"]:
        method, why = "channel", ("no dog-bone at standard vias "
                                  f"({'gap' if not ok['gap'] else 'street'} "
                                  "budget fails); escaping between balls")
    else:
        method, why = "channel", ("dense field below the standard-via "
                                  "budget: only balls next to free (NC) "
                                  "sites can escape; via-in-pad/HDI for "
                                  "the rest")
    return {"via_size": via, "via_drill": drill, "track_width": track,
            "clearance": clearance, "method": method, "why": why,
            "budget": budget, "dogbone_ok": ok["gap"] and ok["street"]}


def fine_via(bga: Bga, n_copper: int, cap: dict | None,
             free: set[int]) -> dict | None:
    """The fab's smallest via, filled in the pad, for a field of pitch
    <= FINE_PITCH_MM on a stack whose filled vias cost nothing (`free`, the
    layer counts jlc_pricing via_fill_epoxy prices 0), else None. A 4-layer
    board never gets one, whatever `free` says. cap: the board's
    jlc_capabilities row; one with no abs_min_via_* floors (another fab)
    gives None. Returns floors keys for escape_params and fine_rule."""
    if n_copper <= 4 or n_copper not in free or not cap:
        return None
    if bga.pitch > FINE_PITCH_MM + GRID_TOL:
        return None
    keys = ("abs_min_via_diameter_mm", "abs_min_via_drill_mm",
            "abs_min_via_hole_to_hole_mm")
    if any(k not in cap for k in keys):
        return None
    di, dr, h2h = (float(cap[k]) for k in keys)
    return {"via_diameter": di, "via_drill": dr,
            "annular": round((di - dr) / 2, 4), "hole_to_hole": h2h,
            "hole_clearance": FINE_VIA_HOLE_CLEARANCE_MM}


def free_fill_layers(pricing: dict) -> set[int]:
    """Layer counts whose filled, capped vias the fab charges nothing for
    (pricing: order_quote.load_pricing(); options.via_fill_epoxy priced 0)."""
    table = (pricing.get("options") or {}).get("via_fill_epoxy") or {}
    return {int(k) for k, v in table.items() if float(v or 0) == 0}


def fine_rule_name(ref: str) -> str:
    return FINE_RULE_PREFIX + "".join(c if c.isalnum() else "_" for c in ref)


def fine_rule(ref: str, fv: dict) -> str:
    """KiCad DRU text: the fine via's floors (diameter, hole, ring, hole to
    hole, hole clearance) for copper touching the courtyard. A custom rule
    wins over the board-setup minimums in KiCad 10, so the board's own via
    floor stays in force everywhere else."""
    rows = (("via_diameter", fv["via_diameter"]),
            ("hole_size", fv["via_drill"]),
            ("annular_width", fv["annular"]),
            ("hole_to_hole", fv["hole_to_hole"]),
            ("hole_clearance", fv["hole_clearance"]))
    body = "".join(f"\t(constraint {c} (min {v:.4f}mm))\n" for c, v in rows)
    return (f'(rule "{fine_rule_name(ref)}"\n{body}'
            f'\t(condition "{rule_condition(ref)}")\n)\n')


def rule_name(ref: str) -> str:
    return RULE_PREFIX + "".join(c if c.isalnum() else "_" for c in ref)


def rule_condition(ref: str) -> str:
    q = ref.replace("'", "")
    return (f"A.intersectsCourtyard('{q}') || "
            f"B.intersectsCourtyard('{q}')")


def dru_rule(ref: str, clearance: float) -> str:
    """KiCad DRU text: escape clearance for copper touching the BGA's
    courtyard (its own balls, dog-bone vias and stubs). Appended after the
    board's floor rules so it wins inside the courtyard only. Byte-equal to
    rules_gen.Rule(...).render() + newline, so either writer can replace
    the other's rule."""
    return (f'(rule "{rule_name(ref)}"\n'
            f'\t(constraint clearance (min {clearance:.4f}mm))\n'
            f'\t(condition "{rule_condition(ref)}")\n'
            ')\n')


def ensure_rules(dru_text: str, rules: list[tuple]) -> tuple[str, list[str]]:
    """dru_text with each (ref, clearance) escape rule present exactly once
    (an existing rule of that name is replaced). A third item, a fine_via
    dict, adds that part's fine-via rule too (#1252). Returns (text, names
    changed)."""
    changed = []
    if not dru_text.strip():
        dru_text = "(version 1)\n"      # KiCad ignores a DRU without it
    for ref, clr, *fv in rules:
        dru_text = _put_rule(dru_text, rule_name(ref), dru_rule(ref, clr),
                             changed)
        if fv and fv[0]:
            dru_text = _put_rule(dru_text, fine_rule_name(ref),
                                 fine_rule(ref, fv[0]), changed)
    return dru_text, changed


def _put_rule(dru_text: str, name: str, new: str, changed: list[str]) -> str:
    import re
    pat = re.compile(r'\(rule "' + re.escape(name)
                     + r'"\n(?:[ \t].*\n)*\)(?:\n|$)')
    m = pat.search(dru_text)
    if m:
        if m.group(0) != new:
            dru_text = dru_text[:m.start()] + new + dru_text[m.end():]
            changed.append(name)
        return dru_text
    # before the first HV / package-rating rule: KiCad's later rule
    # wins, and an escape clearance must never undercut those spacings
    hv = re.search(r'^\(rule "(?:' + "|".join(KEEP_LAST) + ')',
                   dru_text, re.M)
    if hv:
        dru_text = dru_text[:hv.start()] + new + dru_text[hv.start():]
    else:
        if dru_text and not dru_text.endswith("\n"):
            dru_text += "\n"
        dru_text += new
    changed.append(name)
    return dru_text
