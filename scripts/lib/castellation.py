"""castellation.py - castellated (plated half-hole) edge pads: rules, count, checks.

A pad is castellated when its footprint marks it `(property
pad_prop_castellated)`, KiCad's "Castellated pad" fabrication property
(geom.Pad.castellated). Nothing else counts: a plated hole that merely sits on
the outline without the property is reported as an unmarked half-hole, because
it would be ordered without JLC's castellated process.

Rules come from the `castellated:` block of reference/jlc_capabilities.yaml,
which cites JLC's two published sources. Consumers:
  dfm_check.py     the `castellation` family (check()), and the exemption of
                   castellated holes from the ordinary hole-to-edge and
                   annular-ring checks (holes()) and of their pads' copper from
                   copper-to-edge (edge_copper())
  castellated_fp.py  footprint sizing (load_rules())
  order_quote.py  needs_standard_pcba() - Economic PCBA can't build them
  order_quote.py / order_submit.py / bom_cpl.py  count() / refs() - the
                   board, not a brief or a doc, decides whether the order
                   says castellated

Everything is millimetres, board frame. Pure geometry over geom.BoardGeom; no
KiCad process.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import yaml
from shapely.geometry import LineString, Point

import checklib
import geom

CAPABILITIES = (Path(__file__).resolve().parents[2] / "reference"
                / "jlc_capabilities.yaml")
CHECK = "dfm_check"
SOURCE = "castellation"
# Kinds this module emits (dfm_check's `castellation` coverage family).
KINDS = ("castellated_drill", "castellated_off_outline",
         "castellated_hole_to_hole", "castellated_ring",
         "castellated_pad_extension", "castellated_to_other_edge",
         "castellated_to_corner", "castellated_board_size",
         "castellated_thickness", "castellated_unmarked",
         "castellated_needs_panel")
# A turn sharper than this at one outline vertex is a sharp corner. geom
# samples an Edge.Cuts arc into 16 chords, so a rounded corner never turns
# this much at any one vertex: it is found as a stretch of small same-way
# turns instead (_runs), adding up to CORNER_DEG or more within a radius of
# CORNER_MAX_R_MM. A wider curve (a round board, a sweeping edge) stays a run.
CORNER_DEG = 20.0
CORNER_MAX_R_MM = 5.0
_STRAIGHT_DEG = 0.5  # below this a vertex is straight (noding, float noise)
# a rounded corner turning more than 150 deg has no useful virtual corner
_HALF_ROUND_COS = math.cos(math.radians(150.0))
# KiCad 8 (property "Reference" "U1"), KiCad 6 (fp_text reference "U1") (#1429)
_REF = re.compile(r'\((?:property\s+"Reference"|fp_text\s+reference)\s+"?([^"\s)]*)')
_PROP = re.compile(r"\(property\s+pad_prop_castellated\s*\)")


def load_rules(path: Path | None = None) -> dict:
    caps = yaml.safe_load(Path(path or CAPABILITIES).read_text("utf-8"))
    rules = (caps or {}).get("castellated")
    if not isinstance(rules, dict):
        raise checklib.CheckError(f"{path or CAPABILITIES} has no castellated: block")
    return rules


def needs_standard_pcba(w_mm: float, h_mm: float) -> dict:
    """What Standard PCBA takes at JLC, the only service for a castellated or
    a two-sided board (castellated_assembly: block of jlc_capabilities.yaml):
    rails and fiducials, on a board or panel at least this big."""
    a = (yaml.safe_load(CAPABILITIES.read_text("utf-8")) or {}).get(
        "castellated_assembly") or {}
    lo = float(a.get("min_panel_mm", 70.0))
    return {"service": a.get("service", "standard"), "min_panel_mm": lo,
            "rails_and_fiducials": True,
            "rail_min_mm": float(a.get("rail_min_mm", 5.0)),
            "min_fiducials": int(a.get("min_fiducials", 3)),
            "panel_needed": min(w_mm, h_mm) < lo - 1e-6,
            "source": a.get("source")}


def pads(bg) -> list:
    return [p for p in bg.pads_of() if p.castellated]


def count(pcb: Path) -> int:
    """Castellated pads on the board file (0 when it has none). A text scan,
    not a geometry load: the order and BOM legs must count a board that
    geom cannot fully parse (a stub, a board mid-edit) the same way. The
    token only occurs inside a pad's `(property ...)`."""
    return len(_PROP.findall(Path(pcb).read_text(encoding="utf-8",
                                                 errors="replace")))


def refs(pcb: Path) -> list[str]:
    """References of the footprints carrying a castellated pad (same text
    scan as count())."""
    text = Path(pcb).read_text(encoding="utf-8", errors="replace")
    out = set()
    for block in re.split(r"\(footprint\s", text)[1:]:
        m = _REF.search(block)
        if m and _PROP.search(block):
            out.add(m.group(1))
    return sorted(out)


def _hole(p) -> tuple[float, float, float]:
    """(x, y, diameter) of a pad's drill; diameter 0 for a pad with none."""
    if p.drill is None:
        return p.center[0], p.center[1], 0.0
    c = p.drill_poly.centroid
    return c.x, c.y, min(p.drill)


def holes(bg) -> list[tuple[float, float, float]]:
    """(x, y, diameter) of every castellated drill, for dfm_check's exemptions."""
    return [_hole(p) for p in pads(bg) if p.drill is not None]


def edge_copper(bg) -> list:
    """(drill, drill + pad copper) polygons of every castellated pad with a
    drill: what dfm_check's copper-to-edge exempts, as the pad's own shape."""
    return [(p.drill_poly, p.drill_poly.union(p.poly))
            for p in pads(bg) if p.drill is not None]


def _turns(pts) -> list[float]:
    """Signed turn in degrees at each vertex of a closed ring (no repeat)."""
    n = len(pts)
    out = []
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        h1 = math.atan2(b[1] - a[1], b[0] - a[0])
        h2 = math.atan2(c[1] - b[1], c[0] - b[0])
        out.append((math.degrees(h2 - h1) + 180.0) % 360.0 - 180.0)
    return out


def _corner_spans(pts) -> list[tuple[int, int]]:
    """(first, last) vertex index of each corner, in ring order. A sharp
    corner is one vertex (first == last). A rounded corner is the stretch of
    vertices a sampled Edge.Cuts arc leaves: consecutive small turns the same
    way, from the tangent point on one edge to the tangent point on the
    next. last may be below first when the stretch wraps past vertex 0."""
    n = len(pts)
    turn = _turns(pts)

    def kind(i):
        a = abs(turn[i])
        if a > CORNER_DEG:
            return "sharp"
        if a < _STRAIGHT_DEG:
            return "straight"
        return "+" if turn[i] > 0 else "-"

    kinds = [kind(i) for i in range(n)]
    smooth = {"+", "-"}

    def joined(i):
        """Vertex i and i+1 lie on one tight curve: both turn the same small
        way and the chord between them is an arc sample's, not a straight
        edge's (chord / turn is the radius it implies)."""
        j = (i + 1) % n
        if kinds[i] not in smooth or kinds[j] != kinds[i]:
            return False
        step = math.radians(max(abs(turn[i]), abs(turn[j])))
        return math.dist(pts[i], pts[j]) <= CORNER_MAX_R_MM * step

    link = [joined(i) for i in range(n)]
    if all(link):
        return []  # one curve all the way round: a round board has no corner
    # start the scan where a stretch begins so none wraps past the start
    s0 = next(i for i in range(n) if not link[i - 1])
    spans = []
    k = 0
    while k < n:
        i = (s0 + k) % n
        if kinds[i] == "sharp":
            spans.append((i, i))
            k += 1
            continue
        if kinds[i] not in smooth:
            k += 1
            continue
        m = k
        while m + 1 < n and link[(s0 + m) % n]:
            m += 1
        idx = [(s0 + q) % n for q in range(k, m + 1)]
        total = abs(sum(turn[q] for q in idx))
        length = sum(math.dist(pts[idx[q]], pts[idx[q + 1]])
                     for q in range(len(idx) - 1))
        if total >= CORNER_DEG and (
                length / math.radians(total) <= CORNER_MAX_R_MM):
            spans.append((idx[0], idx[-1]))
        k = m + 1
    return sorted(spans)


def _virtual_corner(pts, i: int, j: int):
    """Where the edges meeting a rounded corner (vertices i..j) would cross
    if the corner were sharp: the corner a drawing dimensions, and the one
    JLC's hole-to-corner rule is measured from. None when the two edges are
    near parallel (a half-round end has no such point)."""
    n = len(pts)
    (ax, ay), (bx, by) = pts[i - 1], pts[i]
    (cx, cy), (dx, dy) = pts[j], pts[(j + 1) % n]
    ux, uy, vx, vy = bx - ax, by - ay, dx - cx, dy - cy
    den = ux * vy - uy * vx
    if (ux * vx + uy * vy) < _HALF_ROUND_COS * math.hypot(ux, uy) * math.hypot(
            vx, vy):
        return None
    s = ((cx - bx) * vy - (cy - by) * vx) / den
    return Point(bx + s * ux, by + s * uy)


def _runs(face) -> tuple[list[LineString], list, list[LineString]]:
    """The outline split at its corners: the edge runs between corners, the
    corner points, and the arcs of the rounded corners as cut.

    A sharp corner's point is its vertex. A rounded corner's point is the
    virtual sharp corner its two edges meet at, so rounding a corner never
    moves where the 3 mm is counted from; a half-round end with no such
    point counts from its arc. The arcs are edges too: a hole cut into one,
    or within 1 mm of one, breaks the other-edge rule like any edge."""
    runs, corners, arcs = [], [], []
    # a routed slot inside the outline is cut like the outer edge: a panel's
    # half-holes sit on its slots
    for ring in (face.exterior, *face.interiors):
        r, c, a = _ring_runs(list(ring.coords)[:-1])
        runs += r
        corners += c
        arcs += a
    return runs, corners, arcs


def _ring_runs(pts: list) -> tuple[list[LineString], list, list[LineString]]:
    n = len(pts)
    spans = _corner_spans(pts)
    if not spans:
        return [LineString(pts + [pts[0]])], [], []

    def path(i, j):
        return [pts[m % n] for m in range(i, (j if j >= i else j + n) + 1)]

    corners, arcs = [], []
    for i, j in spans:
        if i == j:
            corners.append(Point(pts[i]))
            continue
        arcs.append(LineString(path(i, j)))
        corners.append(_virtual_corner(pts, i, j) or arcs[-1])
    runs = []
    for k, (_, j) in enumerate(spans):
        nxt = spans[(k + 1) % len(spans)][0]
        # one corner only: its run goes all the way round back to it
        runs.append(LineString(path(j, nxt + n if len(spans) == 1 else nxt)))
    return runs, corners, arcs


def _chord(poly, c: Point, d: tuple[float, float]) -> float:
    """Copper length from c along unit direction d inside poly."""
    far = LineString([(c.x, c.y), (c.x + 20 * d[0], c.y + 20 * d[1])])
    seg = poly.intersection(far)
    if seg.is_empty:
        return 0.0
    parts = getattr(seg, "geoms", [seg])
    return max((g.length for g in parts
                if g.distance(c) < 1e-6), default=0.0)


def check(bg, rules: dict, thickness_mm: float | None = None) -> tuple[list, dict]:
    """Violations for every castellated pad, plus the facts dfm_check reports."""
    vios: list = []
    cps = pads(bg)
    faces = bg.outline_faces or ([bg.outline] if not bg.outline.is_empty else [])
    face = faces[0] if faces else None
    tol = float(rules.get("on_outline_tol_mm", 0.05))

    def add(kind, p, msg, **extra):
        x, y, _ = _hole(p)
        vios.append(checklib.violation(
            CHECK, "error", (x, y), None, p.net, [p.ref], msg, SOURCE,
            kind=kind, pad=f"{p.ref}.{p.number}", **extra))

    # an ordinary plated hole straddling the outline is a half-hole too
    if face is not None:
        edge = face.boundary
        for p in bg.pads_of():
            if p.castellated or p.drill is None:
                continue
            x, y, d = _hole(p)
            if d > 0 and Point(x, y).distance(edge) < d / 2.0 - tol:
                add("castellated_unmarked", p,
                    f"{p.ref}.{p.number}: plated hole cut by the board edge but "
                    "the pad is not marked castellated (pad_prop_castellated); "
                    "JLC would not plate it with the castellated process")
    facts = {"castellated_pads": len(cps),
             "castellated_refs": sorted({p.ref for p in cps})}
    if not cps:
        return vios, facts
    if face is None:
        for p in cps:
            add("castellated_off_outline", p,
                f"{p.ref}.{p.number}: castellated pad but the board has no "
                "closed outline")
        return vios, facts

    runs, corners, arcs = _runs(face)
    # a rule the fab doesn't publish (a vendor profile without the key,
    # docs/vendors.md) reads 0: nothing to hold the board to
    lo_d = float(rules.get("min_drill_mm", 0.0))
    lo_ring = float(rules.get("min_annular_ring_mm", 0.0))
    lo_ext = float(rules.get("min_pad_extension_mm", 0.0))
    lo_edge = float(rules.get("min_to_other_edge_mm", 0.0))
    lo_corner = float(rules.get("min_to_corner_mm", 0.0))
    on_edge = []
    for p in cps:
        x, y, d = _hole(p)
        c = Point(x, y)
        if p.drill is None:
            add("castellated_drill", p,
                f"{p.ref}.{p.number}: castellated pad has no drill")
            continue
        if d < lo_d - 1e-6:
            add("castellated_drill", p,
                f"{p.ref}.{p.number}: castellated drill {d:.3f} mm below JLC "
                f"minimum {lo_d} mm", diameter_mm=checklib.rnd(d), min_mm=lo_d)
        off = c.distance(face.boundary)
        if off > tol:
            add("castellated_off_outline", p,
                f"{p.ref}.{p.number}: castellated hole centre {off:.3f} mm off "
                "the board outline - a castellated pad must sit on the edge",
                distance_mm=checklib.rnd(off))
            continue
        on_edge.append(p)
        # the edge the hole sits on: a run, or a rounded corner's arc when the
        # hole is cut into the corner itself; every other run and corner arc
        # is an "other edge"
        run = min(runs + arcs, key=lambda r: r.distance(c))
        others = [r for r in runs + arcs if r is not run]
        if others:
            de = min(r.distance(c) for r in others) - d / 2.0
            if de < lo_edge - 1e-6:
                add("castellated_to_other_edge", p,
                    f"{p.ref}.{p.number}: castellated hole {de:.3f} mm from "
                    f"another board edge, JLC minimum {lo_edge} mm",
                    distance_mm=checklib.rnd(de), min_mm=lo_edge)
        if corners:
            dc = min(k.distance(c) for k in corners) - d / 2.0
            if dc < lo_corner - 1e-6:
                add("castellated_to_corner", p,
                    f"{p.ref}.{p.number}: castellated hole {dc:.3f} mm from a "
                    f"board corner, JLC minimum {lo_corner} mm",
                    distance_mm=checklib.rnd(dc), min_mm=lo_corner)
        # local edge direction -> tangent t and inward normal n
        s = run.project(c)
        a = run.interpolate(max(s - 0.05, 0.0))
        b = run.interpolate(min(s + 0.05, run.length))
        tx, ty = b.x - a.x, b.y - a.y
        norm = math.hypot(tx, ty) or 1.0
        tx, ty = tx / norm, ty / norm
        nx, ny = -ty, tx
        if not face.contains(Point(x + 0.1 * nx, y + 0.1 * ny)):
            nx, ny = -nx, -ny
        r = d / 2.0
        ext = _chord(p.poly, c, (nx, ny)) - r
        if ext < lo_ext - 1e-6:
            add("castellated_pad_extension", p,
                f"{p.ref}.{p.number}: pad reaches {ext:.3f} mm inward past the "
                f"hole, JLC minimum {lo_ext} mm",
                extension_mm=checklib.rnd(ext), min_mm=lo_ext)
        ring = min(_chord(p.poly, c, (tx, ty)),
                   _chord(p.poly, c, (-tx, -ty))) - r
        if ring < lo_ring - 1e-6:
            add("castellated_ring", p,
                f"{p.ref}.{p.number}: annular ring {ring:.3f} mm beside the "
                f"half-hole, JLC minimum {lo_ring} mm",
                ring_mm=checklib.rnd(ring), min_mm=lo_ring)

    lo_hh = float(rules.get("min_hole_to_hole_mm", 0.0))
    hs = [(p, *_hole(p)) for p in on_edge]
    for i in range(len(hs)):
        for j in range(i + 1, len(hs)):
            pi, xi, yi, di = hs[i]
            _, xj, yj, dj = hs[j]
            gap = math.hypot(xi - xj, yi - yj) - (di + dj) / 2.0
            if gap < lo_hh - 1e-6:
                add("castellated_hole_to_hole", pi,
                    f"castellated holes {pi.ref}.{pi.number} and "
                    f"{hs[j][0].ref}.{hs[j][0].number} {gap:.3f} mm apart, JLC "
                    f"minimum {lo_hh} mm", distance_mm=checklib.rnd(gap),
                    min_mm=lo_hh)

    x0, y0, x1, y1 = face.bounds
    lo_b = float(rules.get("min_board_mm", 0.0))
    if min(x1 - x0, y1 - y0) < lo_b - 1e-6:
        add("castellated_board_size", cps[0],
            f"board {x1 - x0:.1f} x {y1 - y0:.1f} mm is below JLC's "
            f"{lo_b} x {lo_b} mm minimum for castellated holes")
    lo_t = float(rules.get("min_thickness_mm", 0.0))
    if thickness_mm is not None and thickness_mm < lo_t - 1e-6:
        add("castellated_thickness", cps[0],
            f"board {thickness_mm} mm thick is below JLC's {lo_t} mm minimum "
            "for castellated holes")
    return vios, facts
