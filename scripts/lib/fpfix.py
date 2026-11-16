#!/usr/bin/env python
"""fpfix.py - sanitise pulled (easyeda2kicad) footprints against the fab floor.

Every easyeda2kicad pull ships the same four library-inherent defects, all of
them measured against real DRC on the shipped boards (LEARNINGS 2026-07-28
[easyeda2kicad][drc], boards/*/lib/EDITS.md):

  A  sub-printable silk: `fp_circle` artifacts at 0.06-0.10 mm stroke, drawn at
     the first courtyard vertex, frequently ON or INSIDE pad 1. Below JLC's
     0.15 mm minimum line width they cannot print at all, so the ones clear of
     copper are PROMOTED to the printable floor (this is how a pin-1 mark gets
     kept) and the ones over copper are DELETED.
  B  silk under the silk-to-copper bar: outlines that clear pad copper by less
     than the bar. Fixed by NARROWING the stroke, never by moving coordinates
     (0.25 -> 0.20 buys 0.025 mm per edge and keeps the geometry byte-identical).
     Violating items sharing an original width narrow together, so a footprint
     outline keeps ONE uniform stroke.
  C  plated locating pegs: `(pad "" thru_hole circle (size D D) (drill D))` -
     copper diameter == drill diameter, i.e. zero annular ring. Two DRC ERRORS
     each plus a clearance error against the neighbouring pad. They are
     mechanical holes; the fix is the `np_thru_hole` padstack (position, size
     and drill unchanged).
  D  body legend text: `fp_text user` marks that sit INSIDE the part body and
     collide with the footprint's own silk outline (the 3-position DIP switch
     ships 8 `silk_overlap` warnings this way). They are hidden under the part
     once assembled, so they are deleted.
  E  courtyard inside the pads: EasyEDA's 0603 parts ship the body outline
     (+-0.80 x +-0.40 mm) as the courtyard, inside pads reaching +-1.15 mm, and
     KiCad's DRC never compares a courtyard with its own copper
     (check_courtyard does). A courtyard layer that leaves pad copper outside
     is redrawn as one rectangle: the old courtyard's bbox united with the pad
     field grown by COURTYARD_EXCESS, rounded outward to COURTYARD_GRID. A
     courtyard that covers its pads is never touched.
  F  exposed-pad thermal vias with no number: EasyEDA draws an EP's via grid
     as `(pad "" thru_hole circle ...)` with paste on them (the TPA3118's
     HTSSOP-32), so they join no net and print paste into open holes. An
     unnumbered plated hole with copper ring whose centre lies inside a
     numbered SMD pad takes that pad's number, loses its paste layers and
     connects solid to a pour (zone_connect 2: spokes starve a thermal via).

  G  placement type against the pads: EasyEDA's XR0850 power inductor ships
     `(attr through_hole)` on two SMD pads (lite-main L1-L4), so bom_cpl and
     KiCad's position export sent four reflowed parts to the THT step. A
     footprint with only SMD pads gets `smd`, one with only plated pins
     `through_hole` (fpattr.py; F's thermal vias aren't pins). One with both
     keeps its own type; with both and no type at all it is residue: only the
     part's drawing says how it is fitted.

  H  courtyard inside the body: EasyEDA's radial electrolytics
     (CAP-TH_BD6.3-P2.50, CAP-TH_BD10.0-P5.00) draw the can as a circle on
     F.SilkS and ship a courtyard round the two pads only (+-2.35 x +-1.05 mm
     under a 6.3 mm can), so placement packed an SO-8 and 1210s under the
     cans on hw-lab hc-power (#587). The body is the largest unfilled circle
     on a SilkS or Fab layer that holds the centre of every numbered pad (two
     or more); with none drawn, the footprint's own 3D model when it is a
     round can (fix_file: its outline above the board fills its smallest
     enclosing circle to ROUND_BODY). A courtyard that leaves more than
     BODY_TOL_MM2 of that disc outside is redrawn as a circle round the body
     and the pads plus COURTYARD_EXCESS, its radius rounded up to
     COURTYARD_GRID; one whose old graphics reach outside that circle becomes
     the rectangle over both. A footprint with no body drawn and no round
     model is left alone: nothing says where the body ends.

  I  an edge mark on Edge.Cuts: EasyEDA's edge-launch SMAs (BWSMA-KE-P001)
     draw the board edge the part sits on as an open line on Edge.Cuts. On
     a board KiCad merges a footprint's Edge.Cuts into the outline, so the
     line is a stray open contour inside it (DRC's malformed outline, a
     fab's unclosed edge file). An open run of lines or arcs on Edge.Cuts
     (one whose ends meet no other Edge.Cuts end) moves to Dwgs.User, where
     it still shows where the edge goes; a closed cut-out stays.
     check_model_fit.fit_footprint reads the mark (edge_mark) before this
     runs, to face and seat the model on the edge (#405).

Flags (reported, never fixed: only the part's drawing can settle them), from
the footprint's own 3D model when lib_pull fetched one (body_flags):
  model_off_footprint  the model's pins below the board miss the footprint's
                       plated holes: it doesn't line up (EasyEDA's USB4105
                       model sits 2.15 mm off), so render/STEP lie and the
                       two checks below are skipped. lib_pull fits the
                       model to its pads before fpfix runs
                       (check_model_fit.fit_footprint), so this is left
                       for a model no slide or turn fits
  peg_without_hole     the model reaches into the board where the footprint
                       has no hole: a locating peg or pin with nowhere to go
                       (the stacked RCA XDJK-0271-1650's two pegs)
  body_outside_courtyard  the model's body overhangs the courtyard, so
                       placement packs parts into it (the same RCA's body;
                       a round can's courtyard is redrawn by rule H first)

Text surgery on the raw file: only the nodes that change are rewritten, so
nothing else can be reformatted, and re-running is a no-op (idempotent).
Handles BOTH the legacy `(module ...)`/`(width W)` form easyeda2kicad emits and
KiCad-10's `(footprint ...)`/`(stroke (width W))` form.

Library API:
  edge_mark(text)        -> the open Edge.Cuts line (rule I), or None
  sanitize(text, **opts) -> (new_text, [action, ...])
  analyze(text, **opts)  -> {"items": [...], "worst_gap_mm": float|None, ...}
  fix_file(path, **opts) -> report dict

Board API (rules E, G and H):
  fix_board(pcb, dry_run=False) -> report dict: the courtyards and placement
  types of the footprints PLACED on a board, which KiCad copies in and a library fix does
  not reach. Run it after fixing the library the board was built from.

CLI (SPEC section 6): fpfix.py --lib <dir>.pretty [--dry-run] [--out r.json]
                      fpfix.py --pcb <board>.kicad_pcb [--dry-run] [--out r.json]
  exit 0 = clean or fixed, 1 = residue left (a defect no rule can repair), 2 = error
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import sexpdata
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geom  # noqa: E402  (pad polygons + arc sampling: one geometry source)
import fpattr  # noqa: E402  (rule G: the placement type the pads give)

# JLC's minimum silkscreen line width; anything thinner cannot print.
MIN_LINE_WIDTH = 0.15
# Required silk-stroke-edge to pad-copper-edge gap (the EDITS.md acceptance bar).
MIN_GAP = 0.15
# Stroke widths are chosen on this grid so a narrowed outline stays a round number.
WIDTH_GRID = 0.05
# Courtyard excess over the pad field (IPC-7351 nominal density, KiCad KLC).
COURTYARD_EXCESS = 0.25
# A rebuilt courtyard's corners land on this grid, rounded outward.
COURTYARD_GRID = 0.05
# Copper outside a courtyard below this (mm2) is arc sampling, not a miss.
COURTYARD_TOL_MM2 = 1e-3
# Rule H: body disc outside the courtyard below this (mm2) is sampling and
# stroke, not a can over its neighbours (a 0.05 mm sliver round a 5 mm can is
# 0.8 mm2).
BODY_TOL_MM2 = 0.5
# Rule H's model route: a model outline above the board filling this much of
# its smallest enclosing circle is a round can (a square 0.64, a 2:1 box 0.51).
ROUND_BODY = 0.85
# Fraction of the nominal glyph height used as per-character advance (stroke font).
TEXT_ADVANCE = 0.75

_SILK = "SilkS"
_GRAPHIC_HEADS = ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc")
_PLACEHOLDER = re.compile(r"%[A-Z]")


class FpFixError(RuntimeError):
    pass


# --------------------------------------------------------------- text scanning

def top_level_nodes(text: str) -> list[tuple[str, int, int]]:
    """(head, start, end) for every direct child node of the root s-expression.

    A paren scanner rather than a full parse, because the edit has to be a
    substring replacement: sexpdata round-tripping the whole file would
    reformat every node in it.
    """
    depth = 0
    in_str = False
    esc = False
    start = -1
    out: list[tuple[str, int, int]] = []
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
            if depth == 2:
                start = i
        elif ch == ")":
            if depth == 2 and start >= 0:
                sub = text[start:i + 1]
                m = re.match(r"\(\s*([A-Za-z_][\w]*)", sub)
                out.append(((m.group(1) if m else ""), start, i + 1))
                start = -1
            depth -= 1
    return out


def _parse(sub: str):
    try:
        return sexpdata.loads(sub)
    except Exception as exc:  # noqa: BLE001
        raise FpFixError(f"unparsable node: {sub[:60]}... ({exc})") from exc


def _tok(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x


def _kid(node, name):
    for c in node[1:] if isinstance(node, list) else []:
        if isinstance(c, list) and c and _tok(c[0]) == name:
            return c
    return None


def _nums(node) -> list[float]:
    return [float(x) for x in node[1:] if isinstance(x, (int, float))] if node else []


def _layers(node) -> list[str]:
    out = []
    for key in ("layer", "layers"):
        k = _kid(node, key)
        if k is not None:
            out += [str(_tok(t)) for t in k[1:]]
    return out


def _stroke_width(node) -> float | None:
    """(width W) [legacy] or (stroke (width W) ...) [KiCad 10]."""
    w = _kid(node, "width")
    if w is not None and _nums(w):
        return _nums(w)[0]
    st = _kid(node, "stroke")
    if st is not None:
        sw = _kid(st, "width")
        if sw is not None and _nums(sw):
            return _nums(sw)[0]
    return None


_W_LEGACY = re.compile(r"\(width\s+(-?[\d.]+)\s*\)")
_W_MODERN = re.compile(r"\(stroke\b((?:[^()]|\([^()]*\))*?)\(width\s+(-?[\d.]+)\s*\)")


def set_width(sub: str, new_w: float) -> str:
    """Rewrite a graphic node's stroke width, both file formats."""
    txt = f"{new_w:g}"
    if _W_LEGACY.search(sub):
        return _W_LEGACY.sub(lambda m: f"(width {txt})", sub, count=1)
    m = _W_MODERN.search(sub)
    if m:
        return sub[:m.start(2)] + txt + sub[m.end(2):]
    raise FpFixError("graphic has no stroke width to rewrite")


def _is_filled(node) -> bool:
    f = _kid(node, "fill")
    if f is None:
        return False
    if len(f) > 1:
        v = str(_tok(f[1])).lower()
        if v in ("solid", "yes", "true"):
            return True
        if v in ("none", "no", "false"):
            return False
        t = _kid(f, "type")
        if t is not None and len(t) > 1:
            return str(_tok(t[1])).lower() == "solid"
    return False


# ------------------------------------------------------------------- geometry

def _pts(node) -> list[tuple[float, float]]:
    out = []
    for c in node[1:] if isinstance(node, list) else []:
        if isinstance(c, list) and c and _tok(c[0]) == "xy":
            n = _nums(c)
            if len(n) >= 2:
                out.append((n[0], n[1]))
    return out


def graphic_shape(node):
    """Centerline geometry of a graphic node (unbuffered), or None."""
    head = _tok(node[0])
    if head == "fp_line":
        s, e = _kid(node, "start"), _kid(node, "end")
        if s is None or e is None:
            return None
        (sx, sy), (ex, ey) = _nums(s)[:2], _nums(e)[:2]
        if (sx, sy) == (ex, ey):
            return Point(sx, sy)
        return LineString([(sx, sy), (ex, ey)])
    if head == "fp_rect":
        s, e = _kid(node, "start"), _kid(node, "end")
        if s is None or e is None:
            return None
        (x1, y1), (x2, y2) = _nums(s)[:2], _nums(e)[:2]
        ring = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
        return Polygon(ring) if _is_filled(node) else LineString(ring)
    if head == "fp_circle":
        c, e = _kid(node, "center"), _kid(node, "end")
        if c is None or e is None:
            return None
        (cx, cy), (ex, ey) = _nums(c)[:2], _nums(e)[:2]
        r = math.hypot(ex - cx, ey - cy)
        disk = Point(cx, cy).buffer(max(r, 1e-6), quad_segs=24)
        return disk if _is_filled(node) else disk.exterior
    if head == "fp_arc":
        s, m, e = _kid(node, "start"), _kid(node, "mid"), _kid(node, "end")
        if s is not None and m is not None and e is not None:
            pts = geom._arc_points(tuple(_nums(s)[:2]), tuple(_nums(m)[:2]),
                                   tuple(_nums(e)[:2]))
            return LineString(pts) if len(pts) > 1 else None
        # legacy (fp_arc (start CENTER) (end START) (angle DEG))
        a = _kid(node, "angle")
        if s is None or e is None or a is None:
            return None
        (cx, cy), (sx, sy) = _nums(s)[:2], _nums(e)[:2]
        ang = _nums(a)[0]
        r = math.hypot(sx - cx, sy - cy)
        a0 = math.atan2(sy - cy, sx - cx)
        n = max(8, int(abs(ang) / 10) + 2)
        pts = [(cx + r * math.cos(a0 + math.radians(ang) * i / n),
                cy + r * math.sin(a0 + math.radians(ang) * i / n))
               for i in range(n + 1)]
        return LineString(pts)
    if head == "fp_poly":
        pts = _pts(_kid(node, "pts") or node)
        if len(pts) < 2:
            return None
        if _is_filled(node) and len(pts) >= 3:
            p = Polygon(pts)
            return p if p.is_valid and p.area > 0 else LineString(pts + [pts[0]])
        return LineString(pts + [pts[0]] if len(pts) > 2 else pts)
    return None


def _pad_poly(node, rot_offset: float = 0.0):
    """Copper polygon of a pad node in footprint-local coordinates. On a
    board a pad's angle is absolute: rot_offset (minus the footprint's angle)
    takes it back to the footprint's frame."""
    if len(node) < 4:
        return None
    ptype, shape = str(_tok(node[2])), str(_tok(node[3]))
    if ptype == "np_thru_hole":
        return None
    layers = _layers(node)
    if not any(ly.endswith(".Cu") or ly == "*.Cu" for ly in layers):
        return None
    at, size = _kid(node, "at"), _kid(node, "size")
    if at is None or size is None:
        return None
    a, s = _nums(at), _nums(size)
    if len(a) < 2 or len(s) < 2:
        return None
    rr = _kid(node, "roundrect_rratio")
    rratio = _nums(rr)[0] if rr is not None and _nums(rr) else 0.25
    rot = (a[2] if len(a) >= 3 else 0.0) + rot_offset
    return geom._pad_polygon(shape, s[0], s[1], rratio, (a[0], a[1]), rot)


def _text_box(node):
    """Inked box of an fp_text node (approximate, rotation-correct).

    Height is the nominal glyph height plus stroke thickness (KiCad's DRC
    measures the inked box, LEARNINGS 2026-07-29 [parts][silk]); width uses a
    per-character advance of TEXT_ADVANCE x size.
    """
    at = _kid(node, "at")
    if at is None or len(node) < 3:
        return None, ""
    a = _nums(at)
    if len(a) < 2:
        return None, ""
    txt = str(_tok(node[2]))
    eff = _kid(node, "effects")
    font = _kid(eff, "font") if eff is not None else None
    size = _nums(_kid(font, "size")) if font is not None and _kid(font, "size") else []
    h = size[1] if len(size) >= 2 else 1.0
    wch = size[0] if size else 1.0
    th = _nums(_kid(font, "thickness")) if font is not None and _kid(font, "thickness") else []
    t = th[0] if th else 0.15
    hw = (len(txt) * wch * TEXT_ADVANCE + t) / 2.0
    hh = (h + t) / 2.0
    ang = math.radians(a[2] if len(a) >= 3 else 0.0)
    ca, sa = math.cos(ang), math.sin(ang)
    corners = [(dx * ca - dy * sa + a[0], dx * sa + dy * ca + a[1])
               for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
    return Polygon(corners), txt


def _gap(shape, copper, width: float) -> float:
    """Signed stroke-edge to copper-edge gap: d(centerline) - width/2."""
    if copper is None or copper.is_empty or shape is None:
        return float("inf")
    return shape.distance(copper) - width / 2.0


def _floor_grid(w: float) -> float:
    return math.floor(w / WIDTH_GRID + 1e-9) * WIDTH_GRID


# ------------------------------------------------------------------ the rules

def _collect(text: str):
    """Parsed top-level nodes plus the copper union and the silk item list."""
    nodes = []
    for head, s, e in top_level_nodes(text):
        if head in _GRAPHIC_HEADS + ("pad", "fp_text"):
            nodes.append((head, s, e, _parse(text[s:e])))
    copper = unary_union([p for p in (_pad_poly(n) for h, _, _, n in nodes
                                      if h == "pad") if p is not None])
    silk = []
    for head, s, e, node in nodes:
        if head not in _GRAPHIC_HEADS:
            continue
        if not any(_SILK in ly for ly in _layers(node)):
            continue
        w = _stroke_width(node)
        sh = graphic_shape(node)
        if sh is None:
            continue
        silk.append({"head": head, "start": s, "end": e, "node": node,
                     "width": 0.0 if w is None else w, "shape": sh,
                     "has_width": w is not None, "filled": _is_filled(node)})
    return nodes, copper, silk


def analyze(text: str, min_gap: float = MIN_GAP,
            min_line_width: float = MIN_LINE_WIDTH) -> dict:
    """Measure a footprint without changing it (the report lib_pull emits)."""
    nodes, copper, silk = _collect(text)
    items, worst = [], None
    for it in silk:
        g = _gap(it["shape"], copper, it["width"])
        if g != float("inf") and (worst is None or g < worst):
            worst = g
        items.append({"kind": it["head"], "width_mm": round(it["width"], 4),
                      "gap_mm": None if g == float("inf") else round(g, 4),
                      # a filled graphic prints from its fill whatever its stroke
                      "printable": it["filled"] or it["width"] >= min_line_width - 1e-9})
    pegs = sum(1 for h, _, _, n in nodes if h == "pad" and _peg(n))
    return {"silk_items": len(silk), "items": items,
            "worst_gap_mm": None if worst is None else round(worst, 4),
            "unprintable_silk": sum(1 for i in items if not i["printable"]),
            "under_gap_silk": sum(1 for i in items
                                  if i["gap_mm"] is not None and i["gap_mm"] < min_gap - 1e-9),
            "plated_pegs": pegs,
            "copper_pads": sum(1 for h, _, _, n in nodes
                               if h == "pad" and _pad_poly(n) is not None)}


def _peg(node) -> bool:
    """A plated locating peg: unnamed thru_hole whose copper == its drill."""
    if len(node) < 4 or str(_tok(node[2])) != "thru_hole":
        return False
    name = str(_tok(node[1]))
    if name not in ("", '""'):
        return False
    size, drill = _kid(node, "size"), _kid(node, "drill")
    if size is None or drill is None:
        return False
    s, d = _nums(size), _nums(drill)
    if len(s) < 2 or not d:
        return False
    # circular hole, no annular ring anywhere
    return max(s[0], s[1]) <= max(d) + 1e-6


_PAD_TYPE = re.compile(r"(\(pad\s+(?:\"[^\"]*\"|\S+)\s+)thru_hole\b")


_EDGE_LAYER = re.compile(r'(\(\s*layer\s+"?)Edge\.Cuts("?\s*\))')
_END_GRID = 1e-3         # mm: Edge.Cuts ends this close meet


def _ends(node):
    """(start, end) of an fp_line or fp_arc, else None."""
    a, b = _kid(node, "start"), _kid(node, "end")
    if a is None or b is None:
        return None
    a, b = _nums(a), _nums(b)
    return (tuple(a[:2]), tuple(b[:2])) if len(a) >= 2 and len(b) >= 2 else None


def _open_edge_cuts(nodes) -> list:
    """The footprint's Edge.Cuts lines and arcs that close no contour: those
    left once every run of them that ends at a free end is pruned away
    (the leaves of the end graph, repeatedly). Rects, circles and polys are
    closed by themselves and never listed."""
    def key(p):
        return (round(p[0] / _END_GRID), round(p[1] / _END_GRID))
    segs = [(n, _ends(n[3])) for n in nodes if n[0] in ("fp_line", "fp_arc")
            and "Edge.Cuts" in _layers(n[3])]
    segs = [(n, ends) for n, ends in segs if ends is not None]
    live, opened = list(segs), []
    while True:
        deg: dict = {}
        for _n, (a, b) in live:
            for p in (a, b):
                deg[key(p)] = deg.get(key(p), 0) + 1
        leaves = [s for s in live if deg[key(s[1][0])] < 2 or deg[key(s[1][1])] < 2]
        if not leaves:
            break
        opened += leaves
        live = [s for s in live if s not in leaves]
    return [n for n, _ in opened]


def edge_mark(text: str):
    """((x1, y1), (x2, y2)): the board edge an edge-mount footprint draws
    as an open Edge.Cuts line (rule I's mark), in the footprint's file
    frame, or None when it draws none. Every open straight run must lie on
    one line (within _EDGE_COLLINEAR_MM); the mark is their span along it.
    Open arcs or runs on two lines say no single edge: None."""
    nodes = [(h, s, e, _parse(text[s:e])) for h, s, e in top_level_nodes(text)
             if h in ("fp_line", "fp_arc")]
    opened = _open_edge_cuts(nodes)
    if not opened or any(h != "fp_line" for h, *_ in opened):
        return None
    pts = [p for _h, _s, _e, n in opened for p in _ends(n)]
    a, b = max(((p, q) for p in pts for q in pts),
               key=lambda pq: math.dist(*pq))
    span = math.dist(a, b)
    if span < 0.5:
        return None
    ux, uy = (b[0] - a[0]) / span, (b[1] - a[1]) / span
    if any(abs((p[0] - a[0]) * uy - (p[1] - a[1]) * ux) > _EDGE_COLLINEAR_MM
           for p in pts):
        return None
    return (a, b)


_EDGE_COLLINEAR_MM = 0.05


def sanitize(text: str, *, min_gap: float = MIN_GAP,
             min_line_width: float = MIN_LINE_WIDTH,
             promote_silk: bool = True, fix_pegs: bool = True,
             fix_body_text: bool = True,
             fix_courtyard: bool = True,
             fix_ep_vias: bool = True,
             fix_attr: bool = True,
             fix_edge_mark: bool = True,
             model_body: tuple | None = None) -> tuple[str, list[dict]]:
    """Apply rules A-I. Returns (new_text, actions); idempotent. model_body
    is rule H's (cx, cy, r) from the footprint's 3D model (fix_file reads
    it), used only when no body outline is drawn."""
    nodes, copper, silk = _collect(text)
    actions: list[dict] = []
    edits: list[tuple[int, int, str | None, str]] = []  # start, end, replacement|None

    # ---- A: silk below the printable floor -> promote if clear, else delete
    survivors = []
    for it in silk:
        # A FILLED graphic prints from its fill, not its stroke: widening it
        # would grow the printed shape (the DIP switch's slider indicators are
        # solid fp_polys at stroke 0). Only unfilled strokes are promotable,
        # and a stroke of exactly 0 has no width to scale from.
        thin = (it["has_width"] and 0 < it["width"] < min_line_width - 1e-9
                and not it["filled"])
        if thin:
            if promote_silk and _gap(it["shape"], copper, min_line_width) >= min_gap - 1e-9:
                edits.append((it["start"], it["end"],
                              set_width(text[it["start"]:it["end"]], min_line_width),
                              "promote"))
                actions.append({"rule": "A", "action": "promote_silk_width",
                                "kind": it["head"], "from_mm": round(it["width"], 4),
                                "to_mm": min_line_width,
                                "gap_mm": round(_gap(it["shape"], copper, min_line_width), 4)})
                it = dict(it, width=min_line_width)
                survivors.append(it)
            else:
                edits.append((it["start"], it["end"], None, "delete"))
                actions.append({"rule": "A", "action": "delete_unprintable_silk",
                                "kind": it["head"], "width_mm": round(it["width"], 4),
                                "gap_mm": round(_gap(it["shape"], copper, it["width"]), 4)})
            continue
        survivors.append(it)

    # ---- B: narrow (never move) silk that sits under the silk-to-copper bar
    violators = []
    for it in survivors:
        g = _gap(it["shape"], copper, it["width"])
        if g == float("inf") or g >= min_gap - 1e-9:
            continue
        if not it["has_width"] or it["width"] <= 0 or it["filled"]:
            # no stroke to give back - the ink IS the geometry, and moving
            # coordinates is out of scope for an automatic library fix
            actions.append({"rule": "B", "action": "residue_silk_gap",
                            "kind": it["head"], "width_mm": round(it["width"], 4),
                            "gap_mm": round(g, 4),
                            "detail": "filled or stroke-less graphic: only a "
                                      "coordinate change could open this gap"})
            continue
        d = it["shape"].distance(copper)          # centerline distance to copper
        w_max = 2.0 * (d - min_gap)
        violators.append((it, g, w_max))
    # Items that share an original width narrow together: a footprint outline
    # keeps ONE uniform stroke (this is what the approved EDITS.md recipe did).
    by_width: dict[float, float] = {}
    for it, _g, w_max in violators:
        k = round(it["width"], 4)
        by_width[k] = min(by_width.get(k, float("inf")), w_max)
    for it, g, _w_max in violators:
        w_new = _floor_grid(by_width[round(it["width"], 4)])
        if w_new < min_line_width - 1e-9 or w_new >= it["width"] - 1e-9:
            actions.append({"rule": "B", "action": "residue_silk_gap",
                            "kind": it["head"], "width_mm": round(it["width"], 4),
                            "gap_mm": round(g, 4),
                            "detail": "cannot reach the gap by narrowing to the "
                                      f"{min_line_width:g} mm line-width floor"})
            continue
        edits.append((it["start"], it["end"],
                      set_width(text[it["start"]:it["end"]], w_new), "narrow"))
        actions.append({"rule": "B", "action": "narrow_silk_stroke",
                        "kind": it["head"], "from_mm": round(it["width"], 4),
                        "to_mm": round(w_new, 4), "gap_mm": round(g, 4),
                        "new_gap_mm": round(_gap(it["shape"], copper, w_new), 4)})

    # ---- C: plated locating pegs -> np_thru_hole
    if fix_pegs:
        for head, s, e, node in nodes:
            if head != "pad" or not _peg(node):
                continue
            sub = text[s:e]
            new = _PAD_TYPE.sub(lambda m: m.group(1) + "np_thru_hole", sub, count=1)
            if new == sub:
                continue
            at = _nums(_kid(node, "at"))
            edits.append((s, e, new, "peg"))
            actions.append({"rule": "C", "action": "peg_to_npth",
                            "at": [round(v, 3) for v in at[:2]],
                            "drill_mm": round(max(_nums(_kid(node, "drill"))), 3)})

    # ---- F: an exposed pad's unnumbered thermal vias -> the EP's number
    if fix_ep_vias:
        for s, e, new, act in ep_via_edits(text, nodes):
            edits.append((s, e, new, "ep_via"))
            actions.append(act)

    # ---- D: legend text hidden under the body that collides with own silk
    if fix_body_text:
        silk_geoms = [it["shape"].buffer(max(it["width"], 0.01) / 2.0)
                      for it in survivors]
        body = unary_union(silk_geoms).envelope if silk_geoms else None
        for head, s, e, node in nodes:
            if head != "fp_text" or len(node) < 2:
                continue
            if str(_tok(node[1])) != "user":
                continue
            if not any(_SILK in ly for ly in _layers(node)):
                continue
            box, txt = _text_box(node)
            if box is None or _PLACEHOLDER.fullmatch(txt.strip()):
                continue
            if body is None or not body.contains(box.centroid):
                continue
            if not any(box.intersects(g) for g in silk_geoms):
                continue
            edits.append((s, e, None, "body_text"))
            actions.append({"rule": "D", "action": "delete_body_legend_text",
                            "text": txt, "at": [round(v, 3) for v in _nums(_kid(node, "at"))[:2]]})

    # ---- E: a courtyard that leaves pad copper outside -> pads + excess
    # ---- H: a courtyard that leaves the body outside -> body + excess
    if fix_courtyard:
        for start, end, repl, act in courtyard_edits(text, nodes, copper,
                                                     model_body):
            edits.append((start, end, repl, "courtyard"))
            if act:
                actions.append(act)

    # ---- I: an open edge mark on Edge.Cuts -> Dwgs.User
    if fix_edge_mark:
        for head, s, e, node in _open_edge_cuts(nodes):
            sub = text[s:e]
            new = _EDGE_LAYER.sub(lambda m: m.group(1) + "Dwgs.User" + m.group(2),
                                  sub, count=1)
            if new == sub:
                continue
            edits.append((s, e, new, "edge_mark"))
            ends = _ends(node)
            actions.append({"rule": "I", "action": "edge_mark_to_dwgs_user",
                            "kind": head,
                            "from": [round(v, 3) for v in ends[0]] if ends else None,
                            "to": [round(v, 3) for v in ends[1]] if ends else None})

    out = text
    for start, end, repl, _why in sorted(edits, key=lambda t: -t[0]):
        if repl is None:
            # drop the node and the whitespace-only line it sat on
            ls = out.rfind("\n", 0, start) + 1
            le = end
            if out[le:le + 1] == "\n":
                le += 1
            if out[ls:start].strip():
                ls = start          # something else shares the line - keep it
                le = end
            out = out[:ls] + out[le:]
        else:
            out = out[:start] + repl + out[end:]

    # ---- G: the placement type (attr) the pads give, after F numbered the
    # thermal vias (they then count as the EP's, not as pins)
    if fix_attr:
        out, act = attr_edit(out)
        if act:
            actions.append(act)
    return out, actions


def attr_edit(text: str, rot_offset: float = 0.0) -> tuple[str, dict | None]:
    """Rule G on one footprint's text: (new text, action or None)."""
    v = fpattr.judge(_parse(text), rot_offset)
    if not v["mismatch"]:
        return text, None
    act = {"rule": "G", "action": "set_attr" if v["fix"] else "residue_attr",
           "from": v["declared"], "to": v["fix"], "smd_pads": v["smd"],
           "tht_pins": v["tht"], "thermal_vias": v["thermal_vias"],
           "detail": fpattr.why(v)}
    return (fpattr.set_type(text, v["fix"]) if v["fix"] else text), act


def _unnamed(node) -> bool:
    return str(_tok(node[1])) in ("", '""')


def _pad_number(node) -> str:
    return str(_tok(node[1]))


_BARE_NUM = re.compile(r"^[A-Za-z0-9_.+-]+$")


def ep_via_edits(text: str, nodes) -> list[tuple[int, int, str, dict]]:
    """Rule F: (start, end, replacement, action) for every unnumbered plated
    via (copper ring around its drill) whose centre is inside a numbered SMD
    pad, the exposed pad it cools."""
    eps = []
    for head, ps, pe, node in nodes:
        if head != "pad" or len(node) < 4 or _unnamed(node):
            continue
        if str(_tok(node[2])) != "smd":
            continue
        poly = _pad_poly(node)
        if poly is not None:
            # write the number as the EP's own text has it (KiCad 10 quotes)
            quoted = text[ps:pe].lstrip().startswith('(pad "')
            eps.append((_pad_number(node), poly, quoted))
    out = []
    if not eps:
        return out
    for head, s, e, node in nodes:
        if head != "pad" or len(node) < 4 or not _unnamed(node):
            continue
        if str(_tok(node[2])) != "thru_hole" or _peg(node):
            continue
        at = _nums(_kid(node, "at"))
        if len(at) < 2:
            continue
        hit = next(((n, q) for n, poly, q in eps if poly.contains(Point(at[0], at[1]))), None)
        if hit is None:
            continue
        num, quoted = hit
        sub = text[s:e]
        name = num if _BARE_NUM.match(num) and not quoted else '"' + num.replace('"', '\\"') + '"'
        new = re.sub(r'^\(pad\s+(?:""|\S+)', "(pad " + name, sub, count=1)
        # drop the paste layers: open holes must not print paste
        new = re.sub(r'\s+"?(?:\*|[FB])\.Paste"?(?=[\s)])', "", new)
        if "zone_connect" not in new:
            new = new[:-1].rstrip() + " (zone_connect 2))"
        out.append((s, e, new, {"rule": "F", "action": "number_ep_via",
                                "at": [round(v, 3) for v in at[:2]],
                                "pad": num,
                                "paste_dropped": new.count("Paste") < sub.count("Paste")}))
    return out


def _crtyd_layer(node) -> str | None:
    for ly in _layers(node):
        if ly.endswith(".CrtYd"):
            return ly
    return None


def _courtyard_area(shapes):
    """The area a layer's courtyard graphics enclose (lines polygonized)."""
    from shapely.ops import polygonize
    areas = [g for g in shapes if g.geom_type == "Polygon"]
    lines = [g if g.geom_type != "Polygon" else g.exterior for g in shapes
             if g.geom_type in ("LineString", "LinearRing", "Polygon")]
    if lines:
        areas += list(polygonize(unary_union(lines)))
    return unary_union(areas) if areas else None


def _crtyd_line(legacy: bool, layer: str, a, b, indent: str) -> str:
    f = lambda v: f"{v:.2f}"  # noqa: E731
    if legacy:
        return (f"{indent}(fp_line (start {f(a[0])} {f(a[1])}) (end {f(b[0])} "
                f"{f(b[1])}) (layer {layer}) (width 0.05))")
    return (f'{indent}(fp_line (start {f(a[0])} {f(a[1])}) (end {f(b[0])} '
            f'{f(b[1])}) (stroke (width 0.05) (type solid)) (layer "{layer}"))')


def body_circle(nodes, model_body: tuple | None = None):
    """Rule H's body, (cx, cy, r, side) in the footprint frame or None: the
    largest unfilled fp_circle on a SilkS or Fab layer whose disc holds the
    centre of every numbered pad (two or more), else model_body (cx, cy, r)
    on the front."""
    centres = [tuple(_nums(_kid(n, "at"))[:2]) for h, _, _, n in nodes
               if h == "pad" and len(n) >= 4 and not _unnamed(n)
               and _kid(n, "at") is not None]
    if len(centres) < 2:
        return None
    best = None
    for head, _s, _e, node in nodes:
        if head != "fp_circle" or _is_filled(node):
            continue
        side = next((ly.split(".")[0] for ly in _layers(node)
                     if ly.endswith((".SilkS", ".Fab"))), None)
        c, e = _kid(node, "center"), _kid(node, "end")
        if side is None or c is None or e is None:
            continue
        (cx, cy), (ex, ey) = _nums(c)[:2], _nums(e)[:2]
        r = math.hypot(ex - cx, ey - cy)
        if all(math.hypot(x - cx, y - cy) < r for x, y in centres) and \
                (best is None or r > best[2]):
            best = (cx, cy, r, side)
    if best is None and model_body is not None:
        cx, cy, r = model_body
        if all(math.hypot(x - cx, y - cy) < r for x, y in centres):
            best = (cx, cy, r, "F")
    return best


def _model_body(text: str, path: Path | None) -> tuple | None:
    """Rule H's (cx, cy, r) from the footprint's VRML model when its outline
    above the board is round (ROUND_BODY), else None."""
    from shapely.geometry import MultiPoint
    wrl, xf = _model_file(text, path)
    if wrl is None:
        return None
    above = [(x, y) for x, y, z in model_points(wrl, xf) if z >= -1e-6]
    if len(above) < 3:
        return None
    hull = MultiPoint(above).convex_hull
    if hull.geom_type != "Polygon":
        return None
    mbc = shapely.minimum_bounding_circle(hull)
    if hull.area < ROUND_BODY * mbc.area:
        return None
    c = mbc.centroid
    return (float(c.x), float(c.y), float(shapely.minimum_bounding_radius(hull)))


def _crtyd_circle(legacy: bool, layer: str, c, r: float) -> str:
    f = lambda v: f"{v:.2f}"  # noqa: E731
    if legacy:
        return (f"(fp_circle (center {f(c[0])} {f(c[1])}) (end {f(c[0] + r)} "
                f"{f(c[1])}) (layer {layer}) (width 0.05))")
    return (f'(fp_circle (center {f(c[0])} {f(c[1])}) (end {f(c[0] + r)} '
            f'{f(c[1])}) (stroke (width 0.05) (type solid)) (fill none) '
            f'(layer "{layer}"))')


def courtyard_edits(text: str, nodes, copper, model_body: tuple | None = None):
    """Rules E and H: (start, end, replacement|None, action|None) per
    courtyard graphic to rewrite. The first graphic of a failing layer
    becomes the new courtyard (four fp_lines, or one fp_circle round a
    body), the layer's others are dropped."""
    if copper is None or copper.is_empty:
        return []
    body = body_circle(nodes, model_body)
    by_layer: dict[str, list] = {}
    for head, s, e, node in nodes:
        if head not in _GRAPHIC_HEADS:
            continue
        ly = _crtyd_layer(node)
        if ly is None:
            continue
        sh = graphic_shape(node)
        by_layer.setdefault(ly, []).append((s, e, sh))
    legacy = text.lstrip().startswith("(module")
    out = []
    if body is not None and not any(ly.startswith(body[3] + ".")
                                    for ly in by_layer):
        return []   # no courtyard on the body's side: fp_verify's warning
    for ly, items in sorted(by_layer.items()):
        area = _courtyard_area([sh for _, _, sh in items if sh is not None])
        missed = copper.area if area is None else copper.difference(area).area
        disc = None
        if body is not None and ly.startswith(body[3] + "."):
            disc = Point(body[0], body[1]).buffer(body[2], quad_segs=32)
            bmiss = disc.area if area is None else disc.difference(area).area
            if bmiss <= BODY_TOL_MM2:
                disc = None
        if missed <= COURTYARD_TOL_MM2 and disc is None:
            continue
        s0, e0, _ = items[0]
        old = None if area is None else [round(v, 3) for v in area.bounds]
        if disc is not None:
            cx, cy, r = body[:3]
            far = max(math.hypot(x - cx, y - cy)
                      for x, y in shapely.get_coordinates(copper))
            g = COURTYARD_GRID
            rr = round(math.ceil((max(r, far) + COURTYARD_EXCESS) / g - 1e-6) * g, 4)
            circ = Point(cx, cy).buffer(rr, quad_segs=32)
            if area is None or area.difference(circ.buffer(0.01)).area <= COURTYARD_TOL_MM2:
                out.append((s0, e0, _crtyd_circle(legacy, ly, (cx, cy), rr),
                            {"rule": "H", "action": "rebuild_courtyard_body",
                             "layer": ly, "from_bbox": old,
                             "body": [round(v, 3) for v in (cx, cy, r)],
                             "to_circle": [round(cx, 3), round(cy, 3), rr],
                             "body_outside_mm2": round(bmiss, 2)}))
                for s, e, _ in items[1:]:
                    out.append((s, e, None, None))
                continue
            # the old courtyard reaches past the circle: one rectangle over
            # both, the body's square shrunk by the excess added below
            cu = unary_union([copper, circ.envelope.buffer(-COURTYARD_EXCESS,
                                                           join_style=2)])
        else:
            cu = copper
        x1, y1, x2, y2 = cu.bounds
        x1, y1 = x1 - COURTYARD_EXCESS, y1 - COURTYARD_EXCESS
        x2, y2 = x2 + COURTYARD_EXCESS, y2 + COURTYARD_EXCESS
        if area is not None:
            a1, b1, a2, b2 = area.bounds
            x1, y1, x2, y2 = min(x1, a1), min(y1, b1), max(x2, a2), max(y2, b2)
        g = COURTYARD_GRID
        # 1e-6 keeps a bound already on the grid from stepping out a cell
        x1, y1 = (math.floor(v / g + 1e-6) * g for v in (x1, y1))
        x2, y2 = (math.ceil(v / g - 1e-6) * g for v in (x2, y2))
        x1, y1, x2, y2 = (round(v, 4) + 0.0 for v in (x1, y1, x2, y2))
        ls = text.rfind("\n", 0, s0) + 1
        indent = text[ls:s0] if not text[ls:s0].strip() else ""
        corners = [(x1, y2), (x1, y1), (x2, y1), (x2, y2)]
        lines = [_crtyd_line(legacy, ly, corners[i], corners[(i + 1) % 4],
                             indent if i else "") for i in range(4)]
        out.append((s0, e0, "\n".join(lines),
                    {"rule": "E" if disc is None else "H",
                     "action": "rebuild_courtyard", "layer": ly,
                     "from_bbox": old, "to_bbox": [x1, y1, x2, y2],
                     "copper_outside_mm2": round(missed, 4)}))
        for s, e, _ in items[1:]:
            out.append((s, e, None, None))
    return out


def _fp_stem(name: str) -> str:
    """Footprint name without its .kicad_mod suffix (dots are legal in names)."""
    n = Path(name).name
    return n[:-len(".kicad_mod")] if n.endswith(".kicad_mod") else n


# ------------------------------------------------- the part's 3D model (flags)

# KiCad reads VRML in tenths of an inch.
WRL_UNIT_MM = 2.54
# Model geometry this far under the board top is a pin or peg, not a lead
# resting on the copper.
BELOW_BOARD_MM = 0.5
# Below-board points closer than this belong to one pin or peg.
FEATURE_LINK_MM = 1.0
# The body may overhang the courtyard by this much before it is flagged.
BODY_TOL_MM = 0.25

_MODEL = re.compile(r'\(model\s+(?:"([^"]+)"|(\S+))')
_XYZ = r"\(\s*{0}\s*\(xyz\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s*\)"


def _model_file(text: str, where: Path | None) -> tuple[Path | None, dict]:
    """The footprint's first VRML model on disk and its (offset, scale,
    rotate) xyz triples. A ${...} or moved path is looked up by name in the
    *.3dshapes beside the footprint's .pretty (where: the .kicad_mod)."""
    m = _MODEL.search(text)
    if not m:
        return None, {}
    tail = text[m.end():m.end() + 400]
    xf = {}
    for key, dflt in (("offset", (0.0, 0.0, 0.0)), ("scale", (1.0, 1.0, 1.0)),
                      ("rotate", (0.0, 0.0, 0.0))):
        k = re.search(_XYZ.format(key), tail)
        xf[key] = tuple(float(v) for v in k.groups()) if k else dflt
    raw = Path(m.group(1) or m.group(2))
    cands = [raw.with_suffix(".wrl"), raw] if "$" not in str(raw) else []
    if where is not None:
        lib = where.parent.parent if where.suffix == ".kicad_mod" else where.parent
        for d in sorted(lib.glob("*.3dshapes")):
            cands += [d / (raw.stem + ".wrl")]
    for c in cands:
        if c.suffix.lower() == ".wrl" and c.is_file():
            return c, xf
    return None, xf


def model_points(wrl: Path, xf: dict) -> list[tuple[float, float, float]]:
    """The model's vertices in footprint mm (y down, z up from the board)."""
    return [p for face in model_faces(wrl, xf) for p in face]


def model_faces(wrl: Path, xf: dict) -> list[list[tuple[float, float, float]]]:
    """The model's faces, each a vertex list in footprint mm (y down, z up
    from the board). None of a model rotated about x or y (it is no longer
    upright)."""
    ox, oy, oz = xf.get("offset", (0.0, 0.0, 0.0))
    sx, sy, sz = xf.get("scale", (1.0, 1.0, 1.0))
    rx, ry, rz = xf.get("rotate", (0.0, 0.0, 0.0))
    if abs(rx) % 360 > 1e-6 or abs(ry) % 360 > 1e-6:
        return []
    # KiCad turns a model by -rotate.z in its y-up frame
    a = math.radians(-rz)
    ca, sa = math.cos(a), math.sin(a)
    text = wrl.read_text(encoding="utf-8", errors="replace")
    faces = []
    num = re.compile(r"-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|-?\.\d+(?:[eE][-+]?\d+)?")
    # each point [...] list is indexed by the coordIndex [...] after it
    for blk, idx in re.findall(r"\bpoint\s*\[([^\]]*)\](?:.*?\bcoordIndex\s*\[([^\]]*)\])?",
                               text, re.S):
        v = [float(t) for t in num.findall(blk)]
        pts = []
        for i in range(0, len(v) - 2, 3):
            x, y, z = (v[i] * WRL_UNIT_MM * sx, v[i + 1] * WRL_UNIT_MM * sy,
                       v[i + 2] * WRL_UNIT_MM * sz)
            x, y = x * ca - y * sa, x * sa + y * ca
            pts.append((x + ox, -(y + oy), z + oz))
        if not idx:
            faces += [[p] for p in pts]
            continue
        face: list = []
        for t in idx.replace(",", " ").split():
            k = int(t)
            if k < 0:
                if face:
                    faces.append(face)
                face = []
            elif k < len(pts):
                face.append(pts[k])
        if face:
            faces.append(face)
    return faces


def _features(faces) -> list:
    """Group the xy extents of faces into pins/pegs: boxes linked within
    FEATURE_LINK_MM."""
    boxes: list[list[float]] = []
    for face in faces:
        xs, ys = [p[0] for p in face], [p[1] for p in face]
        boxes.append([min(xs), min(ys), max(xs), max(ys)])
    seeds, boxes = sorted(boxes), []
    for x, y, x2, y2 in seeds:
        for b in boxes:
            if (b[0] - FEATURE_LINK_MM <= x2 and x <= b[2] + FEATURE_LINK_MM
                    and b[1] - FEATURE_LINK_MM <= y2 and y <= b[3] + FEATURE_LINK_MM):
                b[0], b[1] = min(b[0], x), min(b[1], y)
                b[2], b[3] = max(b[2], x2), max(b[3], y2)
                break
        else:
            boxes.append([x, y, x2, y2])
    # boxes grown from sorted points can come to overlap: merge until stable
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if (a[0] - FEATURE_LINK_MM <= b[2] and b[0] - FEATURE_LINK_MM <= a[2]
                        and a[1] - FEATURE_LINK_MM <= b[3] and b[1] - FEATURE_LINK_MM <= a[3]):
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]),
                                max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    from shapely.geometry import box as _box
    return [_box(*b).buffer(0.05) for b in boxes]


def _hole_poly(node):
    """Footprint-local outline of a pad's hole (any plated or NPTH pad)."""
    at, drill = _kid(node, "at"), _kid(node, "drill")
    if at is None or drill is None:
        return None
    a = _nums(at)
    d = _nums(drill)
    if len(a) < 2 or not d:
        return None
    w, h = (d[0], d[1]) if len(d) >= 2 else (d[0], d[0])
    rot = a[2] if len(a) >= 3 else 0.0
    return geom._pad_polygon("oval" if w != h else "circle", w, h, 0.25,
                             (a[0], a[1]), rot)


def body_flags(text: str, path: Path | None = None) -> list[dict]:
    """The model-against-footprint flags (see the module doc); [] when the
    footprint has no VRML model on disk."""
    wrl, xf = _model_file(text, path)
    if wrl is None:
        return []
    faces = model_faces(wrl, xf)
    if not faces:
        return []
    pts = [p for face in faces for p in face]
    nodes, copper, _silk = _collect(text)
    pads = [n for h, _, _, n in nodes if h == "pad" and len(n) >= 4]
    holes = [(n, _hole_poly(n)) for n in pads
             if str(_tok(n[2])) in ("thru_hole", "np_thru_hole")]
    # a face wholly under the board is part of a pin or peg
    below = _features([f for f in faces if all(z < -BELOW_BOARD_MM for _, _, z in f)])
    smd = unary_union([q for q in (_pad_poly(n) for n in pads
                                   if str(_tok(n[2])) == "smd") if q is not None])
    # the pins: numbered plated holes, not the vias in an exposed pad
    pins = [(n, h) for n, h in holes
            if h is not None and str(_tok(n[2])) == "thru_hole" and not _unnamed(n)
            and (smd.is_empty or not smd.contains(h.centroid))]
    flags: list[dict] = []
    if pins:
        hit = [n for n, h in pins if any(f.intersects(h) for f in below)]
        if len(hit) * 2 < len(pins):
            flags.append({"flag": "model_off_footprint", "model": wrl.name,
                          "pins_met": len(hit), "pins": len(pins),
                          "detail": "the model's pins below the board miss the "
                                    "footprint's holes; check its offset/rotate "
                                    "against the drawing"})
            return flags
    land = unary_union([g for g in [copper] + [h for _, h in holes]
                        if g is not None and not g.is_empty])
    for f in below:
        if land.is_empty or not f.intersects(land):
            c = f.centroid
            x1, y1, x2, y2 = f.bounds
            flags.append({"flag": "peg_without_hole",
                          "at": [round(c.x, 2), round(c.y, 2)],
                          "size_mm": [round(x2 - x1 - 0.1, 2), round(y2 - y1 - 0.1, 2)],
                          "detail": "the model reaches into the board here and "
                                    "the footprint has no hole: a locating peg? "
                                    "add an NPTH per the part's drawing"})
    above = [(x, y) for x, y, z in pts if z >= -1e-6]
    crt = {}
    for head, _s, _e, node in nodes:
        ly = _crtyd_layer(node) if head in _GRAPHIC_HEADS else None
        if ly and ly.startswith("F."):
            sh = graphic_shape(node)
            if sh is not None:
                crt.setdefault(ly, []).append(sh)
    area = _courtyard_area(crt.get("F.CrtYd", []))
    if above:
        from shapely.geometry import MultiPoint
        body = MultiPoint(above).convex_hull
        if area is None:
            flags.append({"flag": "body_outside_courtyard", "detail": "no F.CrtYd courtyard"})
        else:
            out = body.difference(area.buffer(BODY_TOL_MM))
            if out.area > 0.01:
                bx1, by1, bx2, by2 = body.bounds
                cx1, cy1, cx2, cy2 = area.bounds
                over = {k: round(v, 2) for k, v in (("left", cx1 - bx1), ("top", cy1 - by1),
                                                    ("right", bx2 - cx2), ("bottom", by2 - cy2))
                        if v > BODY_TOL_MM}
                flags.append({"flag": "body_outside_courtyard",
                              "body_bbox": [round(v, 2) for v in body.bounds],
                              "courtyard_bbox": [round(v, 2) for v in area.bounds],
                              "overhang_mm": over, "area_mm2": round(out.area, 2),
                              "detail": "the model's body overhangs the courtyard; "
                                        "grow the courtyard over the body unless "
                                        "the overhang is meant to hang off the "
                                        "board edge"})
    return flags


def fix_file(path: str | Path, *, dry_run: bool = False,
             check_body: bool = True, **opts) -> dict:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    before = analyze(text, opts.get("min_gap", MIN_GAP),
                     opts.get("min_line_width", MIN_LINE_WIDTH))
    if opts.get("fix_courtyard", True) and "model_body" not in opts:
        try:
            opts["model_body"] = _model_body(text, p)
        except Exception:  # noqa: BLE001  (an unreadable model: no body)
            opts["model_body"] = None
    new, actions = sanitize(text, **opts)
    after = analyze(new, opts.get("min_gap", MIN_GAP),
                    opts.get("min_line_width", MIN_LINE_WIDTH))
    if new != text and not dry_run:
        p.write_text(new, encoding="utf-8")
    residue = [a for a in actions if a["action"].startswith("residue")]
    try:
        flags = body_flags(new, p) if check_body else []
    except Exception as exc:  # noqa: BLE001  (a flag check never fails a fix)
        flags = [{"flag": "model_unreadable", "detail": f"{type(exc).__name__}: {exc}"}]
    return {"footprint": p.stem, "file": str(p), "changed": new != text,
            "actions": actions, "residue": len(residue), "flags": flags,
            "before": {"worst_gap_mm": before["worst_gap_mm"],
                       "unprintable_silk": before["unprintable_silk"],
                       "under_gap_silk": before["under_gap_silk"],
                       "plated_pegs": before["plated_pegs"]},
            "after": {"worst_gap_mm": after["worst_gap_mm"],
                      "unprintable_silk": after["unprintable_silk"],
                      "under_gap_silk": after["under_gap_silk"],
                      "plated_pegs": after["plated_pegs"]}}


def fix_lib(pretty: str | Path, *, dry_run: bool = False,
            names: list[str] | None = None, **opts) -> dict:
    d = Path(pretty)
    if not d.is_dir():
        raise FpFixError(f"not a .pretty directory: {d}")
    files = sorted(d.glob("*.kicad_mod"))
    if names is not None:
        # NOT Path().stem: footprint names carry dots ("...-LS9.3-BL") and stem
        # would strip ".3-BL" as an extension, silently skipping the footprint.
        want = {_fp_stem(n) for n in names}
        files = [f for f in files if _fp_stem(f.name) in want]
    rows = [fix_file(f, dry_run=dry_run, **opts) for f in files]
    return {"script": "fpfix", "lib": str(d), "dry_run": bool(dry_run),
            "footprints": len(rows),
            "changed": sum(1 for r in rows if r["changed"]),
            "actions": sum(len(r["actions"]) for r in rows),
            "residue": sum(r["residue"] for r in rows),
            "flags": sum(len(r["flags"]) for r in rows),
            "status": "residue" if any(r["residue"] for r in rows) else "pass",
            "results": rows}


def _apply(text: str, edits) -> str:
    """Splice (start, end, replacement|None) edits, last first."""
    out = text
    for start, end, repl in sorted(edits, key=lambda t: -t[0]):
        if repl is None:
            ls = out.rfind("\n", 0, start) + 1
            le = end + (1 if out[end:end + 1] == "\n" else 0)
            if out[ls:start].strip():
                ls, le = start, end
            out = out[:ls] + out[le:]
        else:
            out = out[:start] + repl + out[end:]
    return out


def board_bodies(text: str) -> dict[str, tuple]:
    """Rule H's body circle, (cx, cy, r, side) in the footprint's file frame,
    per reference of every placed footprint that draws one."""
    out = {}
    for head, s, e in top_level_nodes(text):
        if head != "footprint":
            continue
        sub = text[s:e]
        root = _parse(sub)
        ref = next((str(_tok(c[2])) for c in root[1:]
                    if isinstance(c, list) and len(c) >= 3
                    and _tok(c[0]) == "property"
                    and str(_tok(c[1])) == "Reference"), None)
        nodes = [(h, a, b, _parse(sub[a:b])) for h, a, b in top_level_nodes(sub)
                 if h in ("fp_circle", "pad")]
        body = body_circle(nodes)
        if ref and body is not None:
            out[ref] = body
    return out


def fix_board(pcb: str | Path, *, dry_run: bool = False) -> dict:
    """Rules E, G and H on every footprint placed on a board, in place."""
    p = Path(pcb)
    text = p.read_text(encoding="utf-8")
    edits, rows = [], []
    for head, s, e in top_level_nodes(text):
        if head != "footprint":
            continue
        sub = text[s:e]
        root = _parse(sub)
        at = _nums(_kid(root, "at"))
        fangle = at[2] if len(at) >= 3 else 0.0
        ref = None
        for c in root[1:]:
            if isinstance(c, list) and len(c) >= 3 and _tok(c[0]) == "property" \
                    and str(_tok(c[1])) == "Reference":
                ref = str(_tok(c[2]))
        nodes = [(h, a, b, _parse(sub[a:b])) for h, a, b in top_level_nodes(sub)
                 if h in _GRAPHIC_HEADS + ("pad",)]
        copper = unary_union([q for q in (_pad_poly(n, -fangle)
                                          for h, _, _, n in nodes if h == "pad")
                              if q is not None])
        fe = courtyard_edits(sub, nodes, copper)
        new = _apply(sub, [(a, b, r) for a, b, r, _ in fe]) if fe else sub
        new, g = attr_edit(new, -fangle)
        acts = [act for *_, act in fe if act] + ([g] if g else [])
        if new == sub and not acts:
            continue
        if new != sub:
            edits.append((s, e, new))
        rows.append({"ref": ref, "footprint": str(_tok(root[1])),
                     "actions": acts})
    new = _apply(text, edits) if edits else text
    if new != text and not dry_run:
        p.write_text(new, encoding="utf-8")
    residue = sum(1 for r in rows for a in r["actions"]
                  if a["action"].startswith("residue"))
    return {"script": "fpfix", "pcb": str(p), "dry_run": bool(dry_run),
            "changed": len(edits), "residue": residue,
            "status": "residue" if residue else "pass", "results": rows}


# ------------------------------------------------------- real-DRC measurement

def scratch_drc(pretty: str | Path, names: list[str] | None = None,
                spacing: float = 30.0, timeout: int = 300) -> dict:
    """DRC one instance of each footprint on a bare board, alone.

    The only honest way to make a silk claim: geometry checkers and KiCad's DRC
    disagree (LEARNINGS 2026-07-28 CORRECTION - checkers saw 4 overlaps where
    DRC fired once). Returns {status, violations, counts, by_check, board}.
    """
    import subprocess
    import tempfile
    scripts = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(scripts))
    import kc  # noqa: PLC0415  (venv-side; the worker below is bundled python)
    from lib import env  # noqa: PLC0415

    cli = env.find_kicad_cli()
    if cli is None:
        raise FpFixError("no kicad-cli found (env.py)")
    bp = env.find_kicad_python(cli)
    if bp is None:
        raise FpFixError("KiCad bundled python not found (env.py)")

    with tempfile.TemporaryDirectory() as td:
        pcb = Path(td) / "fp_scratch.kicad_pcb"
        job = Path(td) / "job.json"
        job.write_text(json.dumps({"pretty": str(Path(pretty).resolve()),
                                   "names": names, "out": str(pcb),
                                   "spacing": spacing}), encoding="utf-8")
        cp = subprocess.run([str(bp), str(Path(__file__).with_name("fp_scratch.py")),
                             str(job)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
        try:
            res = json.loads((cp.stdout or "").strip().splitlines()[-1])
        except Exception as exc:  # noqa: BLE001
            raise FpFixError(f"scratch board worker failed: "
                             f"{(cp.stdout + cp.stderr)[-300:]}") from exc
        if res.get("status") != "ok":
            raise FpFixError(f"scratch board worker: {res.get('error')}")
        rep = kc.run_drc(cli, pcb, parity=False, all_track_errors=True)
    by_check: dict[str, int] = {}
    for v in rep["violations"]:
        by_check[v["check"]] = by_check.get(v["check"], 0) + 1
    return {"status": rep["status"], "counts": rep["counts"],
            "by_check": by_check, "violations": rep["violations"],
            "placed": res["placed"], "missing": res["missing"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--lib", help="a .pretty directory")
    src.add_argument("--pcb", help="a board: rules E, G and H on its placed footprints")
    ap.add_argument("--only", nargs="+", help="limit to these footprint names")
    ap.add_argument("--min-gap", type=float, default=MIN_GAP,
                    help=f"silk-to-copper bar, mm (default {MIN_GAP})")
    ap.add_argument("--min-line-width", type=float, default=MIN_LINE_WIDTH,
                    help=f"printable silk floor, mm (default {MIN_LINE_WIDTH})")
    ap.add_argument("--no-promote", action="store_true",
                    help="delete unprintable silk instead of promoting the clear ones")
    ap.add_argument("--no-pegs", action="store_true", help="leave plated peg holes alone")
    ap.add_argument("--no-body-text", action="store_true",
                    help="leave hidden legend text alone")
    ap.add_argument("--no-ep-vias", action="store_true",
                    help="leave an exposed pad's unnumbered vias alone")
    ap.add_argument("--no-attr", action="store_true",
                    help="leave the placement type (attr) alone")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-drc", action="store_true",
                    help="measure the result with real DRC on a scratch board")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args(argv)

    try:
        if args.pcb:
            payload = fix_board(args.pcb, dry_run=args.dry_run)
        else:
            payload = fix_lib(args.lib, dry_run=args.dry_run, names=args.only,
                              min_gap=args.min_gap,
                              min_line_width=args.min_line_width,
                              promote_silk=not args.no_promote,
                              fix_pegs=not args.no_pegs,
                              fix_body_text=not args.no_body_text,
                              fix_ep_vias=not args.no_ep_vias,
                              fix_attr=not args.no_attr)
        if args.verify_drc and args.lib:
            drc = scratch_drc(args.lib, args.only)
            payload["drc"] = {"status": drc["status"], "counts": drc["counts"],
                              "by_check": drc["by_check"],
                              "placed": len(drc["placed"]), "missing": drc["missing"]}
            if drc["counts"]["total"]:
                payload["status"] = "residue"
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"script": "fpfix", "status": "error",
                          "error": f"{type(exc).__name__}: {exc}"}, indent=1))
        return 2
    text = json.dumps(payload, indent=1, ensure_ascii=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 1 if payload["status"] == "residue" else 0


if __name__ == "__main__":
    sys.exit(main())
