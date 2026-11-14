"""stitch_vias - stitching vias + via fences for plane hygiene (S11, SPEC P7.4).

Three jobs (SPEC P7.4: "GND stitching at rise-time-derived pitch"):

1. PAD STITCHING (default): every SMD pad of a target net whose layer does
   NOT carry that net's plane gets a through via tying it to the plane.
   Candidates form a small ring just past the pad edge (on-pad vias short
   neighbours at fine pitch - prior-attempt fact; 0.65 mm from pad centre
   verified safe for a 1.0 mm pad), measured from the pad's own edge along
   each bearing so a big pad's drill never lands on it (an open barrel
   under the paste wicks solder away), tried away-from-body first. A spot whose via disc misses
   the pad's own copper gets a short connecting track (corridor clearance-
   checked against foreign copper too). A pad with no clear spot is SKIPPED
   with a note, never forced. Pads already within 0.2 mm of a same-net via
   are skipped as already stitched. THT/NPTH pads are never stitched (their
   barrel reaches every layer; a co-located via trips hole_to_hole).
2. AREA STITCHING: extra same-net vias on a pitch grid across the net's
   plane fill - grid points inside the fill, >= 1 mm from the board edge,
   outside rule-area keepouts, >= pitch/2 from existing same-net vias, and
   bonding same-net copper on >= 2 copper layers (a via reaching only one
   plane is useless copper and a KiCad via_dangling warning - S11-verified
   live). --pitch auto derives the pitch from the fastest t_rise_ns among
   constraints["high_speed"] (see rise_pitch_mm), clamped to [2, 15] mm.
3. VIA FENCE (--fence-net NET): rows of GND vias flanking every routed track
   of NET, sampled at --fence-pitch (default: rise-time pitch clamped to
   [1, 10] mm), offset perpendicular on BOTH sides by --fence-offset
   (default 2 x track width + 0.3 mm). Used by route_critical for RF nets.

Every candidate must clear FOREIGN copper (any net != target) on ALL copper
layers a through via spans (via disc grown by the board clearance) and keep
a drill-EDGE-aware floor to every known hole (T6, P7A-4): the old model
kept 0.5 mm CENTRE distance and was drill-size-blind, so a via next to a
big/slotted THT drill shipped unmanufacturable spacing DRC-green (KiCad
never checks via drill vs same-net pad hole - LEARNINGS 819). Now every
hole is its real extent (via drills as discs, pad drills incl. slot
stadiums via geom.Pad.drill_poly) and a candidate needs
hole_edge >= EDGE_GAP + its own drill radius, EDGE_GAP = 0.5 - 0.3 =
0.2 mm - identical to the S11-verified 0.5 mm centre floor for two
standard 0.3-drill vias, stricter only where drills are bigger. When
stitching several nets, power nets go first and GND always last (prior-
attempt fact: GND candidates are numerous and box power pads out otherwise).

--nets power (#923 point 1) expands to the constraints' power nets that
have a plane fill on an inner layer (power_plane_nets): the nets whose SMD
pads want a via straight down. A switch node (switch_node, or a net whose
only pour is an outer-layer patch) is not one. --pads-only skips job 2, and
--skip-refs leaves named parts alone (route_escape runs this before its
ball-grid escape so the power pads get their plane vias before signal
escapes take the room beside them; the ball fields stay the escape's).

Contract (SPEC section 6):
  stitch_vias.py --pcb B.kicad_pcb [--constraints c.json] [--nets GND[,+3V3]]
      [--pitch MM|auto] [--max-vias N] [--via-size MM] [--via-drill MM]
      [--clearance MM] [--dry-run] [--strict] [--out-report r.json]
      [--pads-only] [--skip-refs U1,U2]  (--nets power: see above)
      [--fence-net NET [--fence-offset MM] [--fence-pitch MM]]
  Payload: {mode, requested, placed, skipped: [{ref|at, reason}], pitch_mm,
      fence_net?, nets: {...per-net facts...}, ops: [...]}. A net with no
      plane fill is reported and skipped (not an error). If >= 1 pad needed
      stitching for a net and NONE could be placed -> violation kind
      "stitch_impossible" (severity warning): exit 1 only with --strict,
      otherwise status stays "pass" with the violation listed as advisory.
  Writes go through route_edit.apply_ops (atomic + verified + idempotent);
  after an apply on a board with pours, zones are refilled via
  `kicad-cli pcb drc --refill-zones --save-board` (kc.run_drc). --dry-run
  only emits the ops list + report and never touches the board.

Determinism: pads iterate sorted by (ref, pad number), grid points by
(x, y), fence samples along sorted tracks; no RNG - identical inputs give a
byte-identical ops list.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import shapely  # noqa: E402
from shapely.geometry import LineString, Point  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

import checklib  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import kc  # noqa: E402
import route_edit  # noqa: E402
from checklib import CheckError  # noqa: E402

C_MM_PER_NS = 299.792458        # free-space c, mm/ns
KNEE_GHZ_NS = 0.35              # f_knee(GHz) = 0.35 / t_rise(ns)
PITCH_CLAMP = (2.0, 15.0)       # area-stitch pitch bounds, mm
FENCE_CLAMP = (1.0, 10.0)       # fence pitch bounds, mm
DEF_T_RISE_NS = 1.0
MIN_HOLE_DIST = 0.5             # drill centre-to-centre minimum, mm
DEF_CLEARANCE = 0.2             # copper clearance fallback, mm
EDGE_MARGIN = 1.0               # area vias: min distance to outline, mm
RING_RADII = (0.65, 0.9, 1.15)  # pad-stitch spot radii from pad centre, mm
RING_OFFSETS = (0, 45, -45, 90, -90, 135, -135, 180)  # deg from outward
NEAR_VIA_MM = 0.2               # pad counts as stitched: copper gap to via
MAX_TRACK_W = 0.3               # connecting-track width cap, mm
PAD_BITE_MM = 0.05              # min via-disc overlap into the pad copper
_QS = 16                        # shapely quad segs for via discs


# ============================================================ pure helpers

def rise_pitch_mm(constraints: dict | None,
                  clamp: tuple[float, float] = PITCH_CLAMP
                  ) -> tuple[float, float]:
    """Rise-time-derived stitching pitch (SPEC P7.4).

    f_knee = 0.35 / t_rise (GHz), t_rise = fastest t_rise_ns among
    constraints["high_speed"] entries (default 1 ns). Pitch is pinned by the
    plan at 1 ns -> ~4.283 mm inside a [2, 15] mm clamp, i.e.
    c / (f_knee * 200): the literal lambda/20 reading (c / (f_knee * 20))
    gives 42.8 mm, contradicting both the pinned default and the clamp
    window, so the /200 form is implemented. Returns (pitch_mm, t_rise_ns).
    """
    t = None
    for e in (constraints or {}).get("high_speed", []) or []:
        v = e.get("t_rise_ns")
        if v:
            t = float(v) if t is None else min(t, float(v))
    t = t if t is not None else DEF_T_RISE_NS
    f_knee = KNEE_GHZ_NS / t
    pitch = C_MM_PER_NS / (f_knee * 200.0)
    lo, hi = clamp
    return min(max(pitch, lo), hi), t


def outward_deg(pad_center: tuple[float, float],
                fp_centroid: tuple[float, float]) -> float:
    """Bearing (deg) from the footprint's pad centroid to this pad - the
    away-from-body direction preferred for stitch spots."""
    dx = pad_center[0] - fp_centroid[0]
    dy = pad_center[1] - fp_centroid[1]
    if math.hypot(dx, dy) < 1e-6:
        return 0.0
    return math.degrees(math.atan2(dy, dx))


def _edge_reach(center: tuple[float, float], a: float, pad_poly) -> float:
    """Distance from `center` to `pad_poly`'s far boundary along bearing
    `a` (radians); 0 when the ray never leaves the pad's outline."""
    far = max(pad_poly.bounds[2] - pad_poly.bounds[0],
              pad_poly.bounds[3] - pad_poly.bounds[1]) + 1.0
    ray = LineString([center, (center[0] + far * math.cos(a),
                               center[1] + far * math.sin(a))])
    hit = ray.intersection(pad_poly.boundary)
    if hit.is_empty:
        return 0.0
    parts = getattr(hit, "geoms", [hit])
    return max(math.dist(center, c) for g in parts for c in g.coords)


def ring_candidates(center: tuple[float, float], outward: float,
                    radii: tuple[float, ...] = RING_RADII,
                    offsets: tuple[float, ...] = RING_OFFSETS,
                    pad_poly=None, drill_r: float = 0.15
                    ) -> list[tuple[float, float]]:
    """Candidate via spots around a pad: rings just past the pad edge,
    nearest radius first, away-from-body angles first. Deterministic.

    With `pad_poly` the rings are measured from the pad EDGE along each
    bearing, so the drill stays off the pad: the first ring puts the drill
    edge on the pad edge (as 0.65 mm does for a 1.0 mm pad and a 0.3 mm
    drill), the later ones step out as before. Radii from the centre alone
    put a big pad's via inside it, an open barrel under the paste."""
    out = []
    for r in radii:
        for off in offsets:
            a = math.radians(outward + off)
            if pad_poly is not None:
                r_eff = max(r, _edge_reach(center, a, pad_poly) + drill_r
                            + (r - radii[0]))
            else:
                r_eff = r
            out.append((round(center[0] + r_eff * math.cos(a), 3),
                        round(center[1] + r_eff * math.sin(a), 3)))
    return out


def grid_candidates(fill, outline, keepouts, pitch: float,
                    edge_margin: float = EDGE_MARGIN
                    ) -> list[tuple[float, float]]:
    """Grid points at `pitch` inside `fill`, >= edge_margin from the board
    outline edge, outside every keepout polygon. Sorted by (x, y)."""
    if fill is None or fill.is_empty:
        return []
    inner = None
    if outline is not None and not outline.is_empty:
        inner = outline.buffer(-edge_margin)
    minx, miny, maxx, maxy = fill.bounds
    pts = []
    i = 0
    while True:
        x = round(minx + pitch / 2.0 + i * pitch, 3)
        if x > maxx:
            break
        j = 0
        while True:
            y = round(miny + pitch / 2.0 + j * pitch, 3)
            if y > maxy:
                break
            j += 1
            p = Point(x, y)
            if not fill.covers(p):
                continue
            if inner is not None and not inner.covers(p):
                continue
            if any(k.covers(p) for k in keepouts):
                continue
            pts.append((x, y))
        i += 1
    return sorted(pts)


def fence_points(line: LineString, width: float, pitch: float,
                 offset: float | None = None) -> list[tuple[float, float]]:
    """Flanking via spots for one track centerline: samples every `pitch`
    along the line (a segment shorter than pitch gets its midpoint), offset
    perpendicular on BOTH sides. Default offset = 2 * width + 0.3 mm."""
    off = offset if offset is not None else 2.0 * width + 0.3
    length = line.length
    if length < 1e-6:
        return []
    if length < pitch:
        ds = [length / 2.0]
    else:
        ds = []
        d = pitch / 2.0
        while d < length:
            ds.append(d)
            d += pitch
    h = min(0.01, length / 2.0)
    out = []
    for d in ds:
        p = line.interpolate(d)
        a = line.interpolate(max(d - h, 0.0))
        b = line.interpolate(min(d + h, length))
        tx, ty = b.x - a.x, b.y - a.y
        n = math.hypot(tx, ty)
        if n < 1e-9:
            continue
        nx, ny = -ty / n, tx / n
        out.append((round(p.x + off * nx, 3), round(p.y + off * ny, 3)))
        out.append((round(p.x - off * nx, 3), round(p.y - off * ny, 3)))
    return out


# ============================================================ scene checker

class Scene:
    """Board-independent clearance/hole checker for via candidates.

    foreign_of(layer, net) must return the union of the WIRED copper on
    `layer` belonging to nets other than `net` - tracks, pads and vias, but
    NEVER zone fills (fills re-flow around new copper at refill; see
    build_scene). Tests feed synthetic shapely geoms.
    Copper committed during this run is tracked in `new` so
    later candidates of OTHER nets see it; committed via hole discs join
    `holes` so the drill spacing holds between new vias too.

    `holes` entries are shapely geometries of DRILL EXTENTS (T6, P7A-4):
    discs for round drills, stadiums for slots. hole_ok is edge-to-edge:
    a new via (drill radius new_drill_r) is legal iff every hole's edge is
    >= (min_hole - 0.3) + new_drill_r away from its centre - for two
    standard 0.3 mm drills that reproduces the S11 0.5 mm centre floor
    exactly, and big/slot drills are finally seen at their real size.
    """

    def __init__(self, copper_layers, foreign_of, outline, clearance: float,
                 min_hole: float = MIN_HOLE_DIST, new_drill: float = 0.3,
                 hole_gap: float = 0.0):
        self.copper_layers = list(copper_layers)
        self._foreign_of = foreign_of
        self._cache: dict = {}
        self.outline = outline
        self.clearance = float(clearance)
        self.min_hole = float(min_hole)
        # the board's own hole_to_hole rule: KiCad grades it drill EDGE to
        # edge (a 0.5 mm JLC rule is 0.8 mm centre to centre for two
        # 0.3 mm drills), so it wins over the 0.5 mm centre floor
        self.hole_gap = float(hole_gap)
        self.new_drill_r = float(new_drill) / 2.0
        self.holes: list = []     # shapely geoms: drill extents
        self.new: list[tuple[str, str | None, object]] = []
        self.keepouts: list = []  # rule-area outlines; block every candidate

    def foreign(self, layer: str, net: str):
        key = (layer, net)
        if key not in self._cache:
            g = self._foreign_of(layer, net)
            try:
                shapely.prepare(g)
            except Exception:  # noqa: BLE001 - prepare is an optimization only
                pass
            self._cache[key] = g
        return self._cache[key]

    @property
    def edge_gap(self) -> float:
        return max(self.min_hole - 0.3, self.hole_gap)

    def hole_ok(self, x: float, y: float) -> bool:
        pt = Point(x, y)
        floor = self.edge_gap + self.new_drill_r
        return all(h.distance(pt) >= floor - 1e-9 for h in self.holes)

    def copper_ok(self, shape, net: str, layers=None) -> bool:
        """True if `shape` (already grown by clearance) misses all foreign
        copper on the given layers (default: every copper layer)."""
        layers = list(layers if layers is not None else self.copper_layers)
        for layer in layers:
            if self.foreign(layer, net).intersects(shape):
                return False
        for n2, l2, g in self.new:
            if n2 == net:
                continue
            if (l2 is None or l2 in layers) and g.intersects(shape):
                return False
        return True

    def via_check(self, x: float, y: float, via_r: float, net: str,
                  edge_margin: float) -> str | None:
        """None if a through via (radius via_r, net `net`) is legal at
        (x, y); else the rejection reason."""
        pt = Point(x, y)
        if self.outline is not None and not self.outline.is_empty:
            if (not self.outline.covers(pt)
                    or self.outline.boundary.distance(pt)
                    < edge_margin - 1e-9):
                return "edge"
        if not self.hole_ok(x, y):
            return "hole_to_hole"
        disc = pt.buffer(via_r + self.clearance, quad_segs=_QS)
        if any(k.intersects(disc) for k in self.keepouts):
            return "keepout"
        if not self.copper_ok(disc, net):
            return "foreign_copper"
        return None

    def track_ok(self, a, b, width: float, net: str, layer: str) -> bool:
        """Clearance-check a straight connecting track's corridor on its
        layer (flat caps: the round ends live inside pad/via, checked
        separately)."""
        line = LineString([a, b])
        if line.length < 1e-9:
            return True
        corridor = line.buffer(width / 2.0 + self.clearance,
                               cap_style="flat")
        return self.copper_ok(corridor, net, layers=[layer])

    def commit_via(self, x: float, y: float, via_r: float, net: str) -> None:
        self.holes.append(Point(x, y).buffer(self.new_drill_r,
                                             quad_segs=_QS))
        self.new.append((net, None, Point(x, y).buffer(via_r, quad_segs=_QS)))

    def commit_track(self, a, b, width: float, net: str, layer: str) -> None:
        self.new.append((net, layer,
                         LineString([a, b]).buffer(width / 2.0,
                                                   quad_segs=_QS)))


# ============================================================ board plumbing

def _pro_rules(pcb: Path) -> dict:
    """Design-rule minimums from the sidecar .kicad_pro (the DRC authority;
    LEARNINGS [kicad]). Missing/invalid file -> {}."""
    pro = pcb.with_suffix(".kicad_pro")
    if not pro.is_file():
        return {}
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    out = dict(((data.get("board") or {}).get("design_settings") or {})
               .get("rules") or {})
    for c in ((data.get("net_settings") or {}).get("classes") or []):
        if c.get("name") == "Default" and c.get("clearance") is not None:
            out.setdefault("default_clearance", c["clearance"])
    return out


def board_clearance(pcb: Path) -> float:
    """Board minimum copper clearance: max of the pro's min_clearance and
    the Default net class clearance, else the 0.2 mm fallback."""
    r = _pro_rules(pcb)
    vals = [float(v) for v in (r.get("min_clearance"),
                               r.get("default_clearance")) if v]
    return max(vals) if vals else DEF_CLEARANCE


def board_hole_to_hole(pcb: Path) -> float:
    """The hole_to_hole rule DRC grades this board at (edge to edge): the
    .kicad_dru baseline rule, else the .kicad_pro min_hole_to_hole, else
    KiCad's 0.25 mm default."""
    import route_critical as rc
    dru = Path(pcb).with_suffix(".kicad_dru")
    if dru.is_file():
        vals = [r["min_mm"] for r in rc.parse_dru_rules(
            dru.read_text(encoding="utf-8"))
            if r["constraint"] == "hole_to_hole" and not r["nets"]
            and r["condition"] is None]
        if vals:
            return max(vals)
    v = _pro_rules(Path(pcb)).get("min_hole_to_hole")
    return float(v) if v else 0.25


def build_scene(bg: geom.BoardGeom, clearance: float,
                new_drill: float = 0.3, hole_gap: float = 0.0) -> Scene:
    # Foreign copper = tracks + pads + vias of other nets ONLY - never zone
    # FILLS. Fills re-flow at the post-apply refill (a via through a foreign
    # plane gets a legal antipad; a track through a pour region pushes the
    # pour back), so treating fills as hard obstacles falsely rejects every
    # candidate on a board with solid planes (S11 acceptance: 4-layer
    # usbbuck4 -> 0/40 stitched, all "no_clear_spot", because In1/In2 planes
    # covered the board).
    def foreign_of(layer, net):
        geoms = [t.poly for t in bg.tracks_of(layer=layer) if t.net != net]
        geoms += [p.poly for p in bg.pads_of(layer=layer) if p.net != net]
        geoms += [v.poly for v in bg.vias_of(layer=layer) if v.net != net]
        return unary_union(geoms)
    sc = Scene(bg.copper_layers, foreign_of, bg.outline, clearance,
               new_drill=new_drill, hole_gap=hole_gap)
    # rule-area keepouts block ring/fence candidates too, not only the area
    # grid (S11 review finding); a through via spans every layer, so any
    # rule area on a copper layer counts.
    sc.keepouts = [ra["outline"] for ra in bg.rule_areas
                   if ra.get("outline") is not None]
    # Holes are DRILL EXTENTS (T6, P7A-4). A via with no recorded drill is
    # assumed the standard 0.3 mm (never weaker than the old centre floor).
    sc.holes = [Point(v.at).buffer(max(v.drill, 0.3) / 2.0, quad_segs=_QS)
                for v in bg.vias_of()]
    # THT barrels (pads spanning >1 copper layer) are drills too - at their
    # REAL extents incl. slot stadiums (geom.Pad.drill_poly carries the
    # -frot/-prot transform; LEARNINGS 819/827). NPTH holes count too: one
    # with copper on every layer is a barrel above, the rest are in geom's
    # npth list only.
    barrels = [p for p in bg.pads_of() if len(p.layers) > 1]
    for p in barrels + [p for p in bg.npth if len(p.layers) <= 1]:
        dp = p.drill_poly
        sc.holes.append(dp if not dp.is_empty
                        else Point(p.center).buffer(0.15, quad_segs=_QS))
    return sc


def _fp_centroids(bg: geom.BoardGeom) -> dict[str, tuple[float, float]]:
    acc: dict[str, list[tuple[float, float]]] = {}
    for p in bg.pads_of():
        acc.setdefault(p.ref, []).append(p.center)
    return {ref: (sum(c[0] for c in pts) / len(pts),
                  sum(c[1] for c in pts) / len(pts))
            for ref, pts in acc.items()}


def _bump(counts: dict, key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


# ============================================================ stitch jobs

def power_plane_nets(constraints: dict | None,
                     bg: geom.BoardGeom) -> list[str]:
    """The constraints' power nets on this board that have a filled zone on
    an inner copper layer, in constraints order. A switch_node entry is
    left out (its patch is an outer-layer pour, and a via field on it is
    EMI); so is a net with no inner plane (nothing to stitch down to)."""
    inner = set(bg.copper_layers[1:-1])
    out: list[str] = []
    for entry in (constraints or {}).get("power") or []:
        net = entry.get("net") if isinstance(entry, dict) else None
        if (not net or net in out or net not in bg.nets
                or entry.get("switch_node")):
            continue
        if inner & set(bg.layers_with_zone(net)):
            out.append(net)
    return out


def stitch_one_net(bg: geom.BoardGeom, scene: Scene, net: str, pitch: float,
                   via_size: float, via_drill: float, max_vias: int,
                   min_track: float, area: bool = True,
                   skip_refs: frozenset = frozenset()):
    """Pad stitching + area grid for one net (area=False: pads only; pads
    of skip_refs are left alone). Returns
    (ops, skipped, facts, violation_or_None)."""
    via_r = via_size / 2.0
    pad_edge_margin = via_r + scene.clearance
    plane_layers = bg.layers_with_zone(net)
    facts: dict = {"plane_layers": plane_layers}
    if not plane_layers:
        facts.update({"note": "no plane fill on any layer - net skipped",
                      "pads": {"requested": 0, "placed": 0, "skipped": 0},
                      "area": {"candidates": 0, "placed": 0,
                               "rejected": {}, "capped": False}})
        return [], [], facts, None
    fill = unary_union([bg.zone_fill(net, l) for l in plane_layers])
    same_vias = [(v.at, v.diameter) for v in bg.vias_of(net=net)]
    cents = _fp_centroids(bg)
    ops: list[dict] = []
    skipped: list[dict] = []

    # ---- job 1: SMD pads whose layer does not carry the plane
    pads = sorted((p for p in bg.pads_of(net=net)
                   if len(p.layers) == 1 and p.layers[0] not in plane_layers
                   and p.ref not in skip_refs),
                  key=lambda p: (p.ref, p.number))
    requested = placed = 0
    impossible_refs: list[str] = []
    for pad in pads:
        label = f"{pad.ref}.{pad.number}"
        if any(pad.poly.distance(Point(at)) <= NEAR_VIA_MM + dia / 2.0
               for at, dia in same_vias):
            skipped.append({"ref": label, "reason": "already_stitched"})
            continue
        requested += 1
        bearing = outward_deg(pad.center, cents.get(pad.ref, pad.center))
        hit = None
        for x, y in ring_candidates(pad.center, bearing,
                                    pad_poly=pad.poly,
                                    drill_r=via_drill / 2.0):
            if not fill.covers(Point(x, y)):
                continue  # via would miss the plane entirely
            if pad.poly.distance(Point(x, y)) < via_drill / 2.0 - 1e-6:
                continue  # drill on its own pad: paste wicks down it
            if scene.via_check(x, y, via_r, net, pad_edge_margin):
                continue
            track_op = None
            disc = Point(x, y).buffer(via_r, quad_segs=_QS)
            # hairline contact is not a reliable connection: require the via
            # disc to bite >= PAD_BITE_MM into the pad, else add a track
            if not disc.buffer(-PAD_BITE_MM).intersects(pad.poly):
                w = round(max(min_track, min(min(pad.size), MAX_TRACK_W)), 3)
                start = (round(pad.center[0], 3), round(pad.center[1], 3))
                if not scene.track_ok(start, (x, y), w, net, pad.layers[0]):
                    continue
                track_op = {"op": "add_track",
                            "start": [start[0], start[1]], "end": [x, y],
                            "width": w, "layer": pad.layers[0], "net": net}
            hit = ((x, y), track_op)
            break
        if hit is None:
            # S14: a pad already carrying a same-net TRACK is connected to
            # the net's copper elsewhere (the router routed it) - "no local
            # via spot" is then an advisory nuance, not "stitch_impossible"
            # (both P7 runs FP'd on track-connected LQFP GND pins).
            track_touch = any(
                t.poly.intersects(pad.poly)
                for t in bg.tracks_of(net=net, layer=pad.layers[0]))
            if track_touch:
                skipped.append({"ref": label,
                                "reason": "track_connected_no_local_spot"})
            else:
                skipped.append({"ref": label, "reason": "no_clear_spot"})
                impossible_refs.append(pad.ref)
            continue
        (x, y), track_op = hit
        ops.append({"op": "add_via", "at": [x, y], "size": via_size,
                    "drill": via_drill, "net": net})
        scene.commit_via(x, y, via_r, net)
        same_vias.append(((x, y), via_size))
        if track_op is not None:
            ops.append(track_op)
            scene.commit_track(tuple(track_op["start"]), (x, y),
                               track_op["width"], net, track_op["layer"])
        placed += 1
    facts["pads"] = {"requested": requested, "placed": placed,
                     "skipped": requested - placed}

    # ---- job 2: pitch grid across the plane fill
    keepouts = [ra["outline"] for ra in bg.rule_areas
                if ra["layers"] and not ra["outline"].is_empty]
    cands = (grid_candidates(fill, bg.outline, keepouts, pitch)
             if area else [])
    rejected: dict[str, int] = {}
    area_placed = 0
    capped = False
    for x, y in cands:
        if area_placed >= max_vias:
            capped = True
            break
        if any(math.hypot(x - at[0], y - at[1]) < pitch / 2.0
               for at, _d in same_vias):
            _bump(rejected, "near_same_net_via")
            continue
        disc = Point(x, y).buffer(via_r, quad_segs=_QS)
        if not fill.covers(disc):
            _bump(rejected, "fill_edge")
            continue
        # the via must bond same-net copper on >= 2 layers, else it hangs
        # off a single plane (KiCad via_dangling; live-verified on the
        # 2-layer blinky2+pour fixture)
        contact = 0
        for layer in bg.copper_layers:
            g = bg.net_copper(net, layer)
            if not g.is_empty and g.intersects(disc):
                contact += 1
                if contact >= 2:
                    break
        if contact < 2:
            _bump(rejected, "single_layer_contact")
            continue
        reason = scene.via_check(x, y, via_r, net, EDGE_MARGIN)
        if reason:
            _bump(rejected, reason)
            continue
        ops.append({"op": "add_via", "at": [x, y], "size": via_size,
                    "drill": via_drill, "net": net})
        scene.commit_via(x, y, via_r, net)
        same_vias.append(((x, y), via_size))
        area_placed += 1
    facts["area"] = {"candidates": len(cands), "placed": area_placed,
                     "rejected": rejected, "capped": capped}

    violation = None
    # S14: only pads with NO track connection count toward impossibility -
    # a track-connected pad is wired to its net elsewhere and its missing
    # local via spot is an advisory nuance (skip reason
    # track_connected_no_local_spot), not a stitch failure.
    if impossible_refs and placed == 0:
        violation = checklib.violation(
            "stitch_vias", "warning", None, None, net,
            sorted(set(impossible_refs)),
            f"stitch_impossible: none of {requested} pad(s) of {net} had a "
            "clear via spot", "stitch", kind="stitch_impossible")
    return ops, skipped, facts, violation


def fence_ops(bg: geom.BoardGeom, scene: Scene, fence_net: str, via_net: str,
              pitch: float, offset: float | None, via_size: float,
              via_drill: float, max_vias: int):
    """Via fence flanking every track of fence_net. Returns (ops, facts)."""
    via_r = via_size / 2.0
    edge_margin = via_r + scene.clearance
    tracks = sorted(
        bg.tracks_of(net=fence_net),
        key=lambda t: (t.layer, [(round(cx, 3), round(cy, 3))
                                 for cx, cy in t.shape.coords]))
    if not tracks:
        raise CheckError(f"fence net '{fence_net}' has no tracks on board")
    same_vias = [(v.at, v.diameter) for v in bg.vias_of(net=via_net)]
    ops: list[dict] = []
    rejected: dict[str, int] = {}
    requested = placed = 0
    capped = False
    for t in tracks:
        off = offset if offset is not None else 2.0 * t.width + 0.3
        spacing = min(pitch / 2.0, off)  # dedupe along the row, never the
        for x, y in fence_points(t.shape, t.width, pitch, off):  # twin side
            if placed >= max_vias:
                capped = True
                break
            requested += 1
            if any(math.hypot(x - at[0], y - at[1]) < spacing
                   for at, _d in same_vias):
                _bump(rejected, "near_same_net_via")
                continue
            reason = scene.via_check(x, y, via_r, via_net, edge_margin)
            if reason:
                _bump(rejected, reason)
                continue
            ops.append({"op": "add_via", "at": [x, y], "size": via_size,
                        "drill": via_drill, "net": via_net})
            scene.commit_via(x, y, via_r, via_net)
            same_vias.append(((x, y), via_size))
            placed += 1
        if capped:
            break
    facts = {"tracks": len(tracks), "requested": requested,
             "placed": placed, "rejected": rejected, "capped": capped}
    return ops, facts


# ============================================================ CLI

def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints", default=None,
                    help="constraints.json (high_speed t_rise for --pitch auto)")
    ap.add_argument("--nets", default="GND",
                    help='comma list; power nets before GND (default "GND"); '
                         '"power" = the constraints\' inner-plane power nets')
    ap.add_argument("--pads-only", action="store_true",
                    help="pad stitching only, no area grid")
    ap.add_argument("--skip-refs", default=None,
                    help="comma list of parts whose pads are left alone")
    ap.add_argument("--pitch", default="auto",
                    help='area-stitch grid pitch mm, or "auto" (rise-time)')
    ap.add_argument("--max-vias", type=int, default=200,
                    help="cap on grid/fence vias placed per net (default 200)")
    ap.add_argument("--via-size", type=float, default=0.6)
    ap.add_argument("--via-drill", type=float, default=0.3)
    ap.add_argument("--clearance", type=float, default=None,
                    help="copper clearance mm (default: board minimum or 0.2)")
    ap.add_argument("--dry-run", action="store_true",
                    help="emit the ops list + report only; board untouched")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on stitch_impossible (default: advisory)")
    ap.add_argument("--fence-net", default=None,
                    help="fence mode: flank this net's tracks with vias")
    ap.add_argument("--fence-offset", type=float, default=None,
                    help="perpendicular offset mm (default 2*width + 0.3)")
    ap.add_argument("--fence-pitch", type=float, default=None,
                    help="sample pitch mm (default rise-time, clamped 1..10)")
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)

    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    constraints = (checklib.load_json(args.constraints, "constraints")
                   if args.constraints else {})
    if args.via_drill >= args.via_size:
        raise CheckError("--via-drill must be < --via-size")

    bg = geom.BoardGeom.from_file(pcb)
    clearance = (args.clearance if args.clearance is not None
                 else board_clearance(pcb))
    min_track = float(_pro_rules(pcb).get("min_track_width") or 0.15)
    scene = build_scene(bg, clearance, new_drill=args.via_drill,
                        hole_gap=board_hole_to_hole(pcb))
    nets: list[str] = []
    for s in (args.nets or "").split(","):
        s = s.strip()
        if s == "power" and "power" not in bg.nets:
            nets += [n for n in power_plane_nets(constraints, bg)
                     if n not in nets]
        elif s and s not in nets:
            nets.append(s)
    if not nets and "power" not in (args.nets or ""):
        raise CheckError("--nets is empty")
    skip_refs = frozenset(r.strip() for r in (args.skip_refs or "").split(",")
                          if r.strip())

    violations: list[dict] = []
    skipped: list[dict] = []
    ops: list[dict] = []

    if args.fence_net:
        if not nets:
            raise CheckError("--fence-net needs a via net in --nets")
        if args.fence_pitch is not None:
            fpitch, t_used = float(args.fence_pitch), None
        else:
            fpitch, t_used = rise_pitch_mm(constraints, FENCE_CLAMP)
        if fpitch <= 0:
            raise CheckError("--fence-pitch must be > 0")
        via_net = nets[0]
        if via_net not in bg.nets:
            raise CheckError(f"via net '{via_net}' not on board")
        fops, ffacts = fence_ops(bg, scene, args.fence_net, via_net, fpitch,
                                 args.fence_offset, args.via_size,
                                 args.via_drill, args.max_vias)
        ops = fops
        requested, placed = ffacts["requested"], ffacts["placed"]
        extra = {"mode": "fence", "fence_net": args.fence_net,
                 "via_net": via_net, "pitch_mm": round(fpitch, 3),
                 "t_rise_ns": t_used, "offset_mm": args.fence_offset,
                 "fence": ffacts}
    else:
        if args.pitch == "auto":
            pitch, t_used = rise_pitch_mm(constraints)
        else:
            pitch, t_used = float(args.pitch), None
            if pitch <= 0:
                raise CheckError("--pitch must be > 0")
        # power nets first, GND last (GND boxes power pads out otherwise)
        ordered = ([n for n in nets if n != "GND"]
                   + [n for n in nets if n == "GND"])
        net_facts: dict[str, dict] = {}
        requested = placed = 0
        for net in ordered:
            if net not in bg.nets:
                raise CheckError(f"net '{net}' not on board")
            nops, nskip, nfacts, viol = stitch_one_net(
                bg, scene, net, pitch, args.via_size, args.via_drill,
                args.max_vias, min_track, area=not args.pads_only,
                skip_refs=skip_refs)
            ops += nops
            for s in nskip:
                s["net"] = net
            skipped += nskip
            net_facts[net] = nfacts
            requested += nfacts["pads"]["requested"]
            placed += nfacts["pads"]["placed"] + nfacts["area"]["placed"]
            if viol is not None:
                violations.append(viol)
        extra = {"mode": "stitch", "pitch_mm": round(pitch, 3),
                 "pads_only": bool(args.pads_only),
                 "t_rise_ns": t_used, "nets": net_facts}

    applied = refilled = False
    apply_by_status = None
    if ops and not args.dry_run:
        results = route_edit.apply_ops(pcb, ops)
        applied = True
        apply_by_status = {}
        for r in results:
            _bump(apply_by_status, r["status"])
        if bg.zones_of():  # new copper stales every pour it lands on
            cli = env.find_kicad_cli()
            if cli is None:
                raise CheckError("kicad-cli not found for zone refill "
                                 "(vias applied; refill manually)")
            try:
                kc.run_drc(cli, pcb, refill=True, save_board=True)
            except Exception as exc:  # noqa: BLE001
                raise CheckError(
                    "vias were APPLIED but the zone refill failed - the "
                    "board HAS been modified with stale pours; re-run "
                    "'kicad-cli pcb drc --refill-zones --save-board' "
                    f"manually. Cause: {exc}") from exc
            refilled = True

    payload = checklib.report(
        "stitch_vias", pcb, violations,
        requested=requested, placed=placed, skipped=skipped,
        via_size=args.via_size, via_drill=args.via_drill,
        clearance_mm=clearance, dry_run=bool(args.dry_run),
        applied=applied, refilled=refilled,
        apply_by_status=apply_by_status, ops=ops, **extra)
    if payload["status"] == "violations" and not args.strict:
        payload["status"] = "pass"
        payload["note"] = "advisory violations (use --strict to gate)"
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("stitch_vias", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
