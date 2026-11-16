"""geom.py - net-indexed, per-layer shapely geometry from a .kicad_pcb.

The single geometry source for the verification suite (S4/S5). Parses a KiCad
board's s-expressions (no SWIG, no IPC - pure venv) into shapely primitives so
that every check reasons about the SAME copper the fabricator will see.

All coordinates are millimetres (KiCad 6+ .kicad_pcb stores mm natively); areas
are therefore mm^2 and lengths mm. No unit conversion happens anywhere except
the pcbnew round-trip oracle used only by the S3 tests.

--------------------------------------------------------------------- public API
    load_board(path)               -> BoardGeom          (cached by path+mtime)
    BoardGeom.from_file(path)       -> BoardGeom          (uncached)

  BoardGeom:
    .copper_layers                  ordered [F.Cu, In1.Cu, ..., B.Cu] top->bottom
    .stackup                        Stackup (layer order, dielectric h, epsilon_r)
    .outline                        shapely Polygon of Edge.Cuts (largest face)
    .outline_faces                  every closed Edge.Cuts face, largest first
                                    (>1 = interior window / second board)
    .outline_items                  {gr_rect|gr_line|gr_arc|...: count} on
                                    Edge.Cuts, counted before the first-match
                                    return - the only view of what else is there
    .outline_arc_radii              exact radius of each Edge.Cuts arc
    .nets                           set of net names carrying copper

    .tracks_of(net=None, layer=None)    -> list[Track]
    .vias_of(net=None, layer=None)      -> list[Via]     (layer = spans that layer)
    .pads_of(net=None, layer=None, ref=None) -> list[Pad]
    .zones_of(net=None, layer=None)     -> list[Zone]    (copper zones only)
    .rule_areas                         keepout areas [{name, layers, outline}]
    .footprint_rule_areas               the same, from inside footprints
                                        (never copper, never "unfilled")

    .net_copper(net, layer)         -> (Multi)Polygon: union of ALL copper
                                       (tracks+pads+vias+zone fill) of net on layer
    .zone_fill(net, layer)          -> (Multi)Polygon: union of net's zone fill
    .layer_copper(layer, net=None, exclude=None) -> union of copper on a layer
    .net_area(net, layer)           -> float mm^2   (== net_copper(...).area)
    .net_area_by_layer(net)         -> {layer: mm^2}

    .adjacent_copper(layer)         -> (above|None, below|None)  copper neighbours
    .layers_with_zone(net)          -> [layers where net has a filled zone]

    .fill_status()                  -> {zone_id: bool filled}
    .unfilled_zones()               -> [Zone] with an outline but no fill
    .assert_fresh(refill=False)     raise StaleFillError on unfilled (or, with
                                    refill=True, on fills that differ from a fresh
                                    `kicad-cli drc --refill-zones` via the S0 path);
                                    KicadCliMissing when that refill has no
                                    kicad-cli (a missing tool, not a stale fill)

Flipped (back-side) footprints need no special handling anywhere: pcbnew bakes
the mirror into the stored file (pad locals mirrored, angles negated, layers
renamed to B.*), so the front-side transform applies to every footprint
(S3-verified against a SWIG-flipped board; LEARNINGS [geometry]).

Design notes are in PROGRESS.md (S3) and LEARNINGS.md [geometry].
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Iterable, Optional

import sexpdata
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import polygonize, unary_union

UNITS = "mm"
# shapely buffer smoothness (quarter-circle segments). 24 keeps via/round-pad
# area error under ~0.1% while staying fast on the ~130-via RF board.
_QUAD_SEGS = 24
# FR4 relative permittivity used when the board carries no (stackup) block.
_FR4_ER = 4.5
# Typical finished-copper thicknesses (mm): 1 oz outer, 0.5 oz inner.
_CU_OUTER = 0.035
_CU_INNER = 0.0152
# KiCad's ZONE_CONNECTION as `(zone_connect N)` stores it: how a zone joins a
# pad of its net (Pad.zone_connect, Zone.connect).
ZONE_CONNECT_INHERIT = -1
ZONE_CONNECT_NONE = 0
ZONE_CONNECT_THERMAL = 1
ZONE_CONNECT_SOLID = 2
ZONE_CONNECT_THT_THERMAL = 3       # thermal for a through-hole pad, else solid


class GeomError(RuntimeError):
    """A board could not be parsed into usable geometry."""


class StaleFillError(GeomError):
    """Zones are unfilled or their fill is stale (see assert_fresh)."""


class KicadCliMissing(GeomError):
    """A refill was needed but no kicad-cli could be found (env.find_kicad_cli
    returned None, or a pin such as HWDE_KICAD_CLI points at nothing). A
    missing tool, not a stale fill."""


# ============================================================ s-expr helpers
# sexpdata yields nested lists; bare tokens are sexpdata.Symbol, quoted tokens
# are str, numbers are int/float. These helpers walk that tree by node head.

def _tok(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x


def _is_node(n) -> bool:
    return isinstance(n, list) and bool(n)


def _head(n):
    return _tok(n[0]) if _is_node(n) else None


def _kids(n, name: str) -> list:
    return [c for c in n[1:] if _is_node(c) and _head(c) == name]


def _kid(n, name: str):
    for c in n[1:]:
        if _is_node(c) and _head(c) == name:
            return c
    return None


def _nums(n) -> list[float]:
    """Positional numeric args of a node, e.g. (at 1 2 90) -> [1.0, 2.0, 90.0]."""
    return [float(x) for x in n[1:] if isinstance(x, (int, float))]


def _strs(n) -> list[str]:
    # plain str: a bare token (KiCad <= 5 writes `(net 1 GND)`, `(layer
    # F.Cu)`) is a sexpdata.Symbol, which never equals the same str, so a
    # constraints net or "F.Cu" would never match it
    return [_tok(x) if isinstance(x, sexpdata.Symbol) else x
            for x in n[1:] if isinstance(x, str)]


def _num_of(n, *names):
    """The first number of n's first child named one of `names`, or None."""
    for name in names:
        k = _kid(n, name)
        if k is not None and _nums(k):
            return _nums(k)[0]
    return None


def _thermal_of(n) -> dict:
    """A pad's or footprint's own zone connection and thermal overrides
    (Pad.zone_connect ...): None where it does not say. KiCad 7 renamed
    (thermal_width) to (thermal_bridge_width)."""
    zc = _num_of(n, "zone_connect")
    return {"zone_connect": int(zc) if zc is not None else None,
            "thermal_gap": _num_of(n, "thermal_gap"),
            "spoke_width": _num_of(n, "thermal_bridge_width", "thermal_width"),
            "spoke_angle": _num_of(n, "thermal_bridge_angle")}


def _uuid_of(node) -> "Optional[str]":
    """(uuid "...") / legacy (tstamp ...) child of a board item, else None."""
    for key in ("uuid", "tstamp"):
        u = _kid(node, key)
        if u is not None and len(u) > 1:
            return str(_tok(u[1]))
    return None


def _pts(node, arcs: bool = False,
         tol: float | None = None) -> list[tuple[float, float]]:
    """Coordinates from a (pts (xy x y) ...) node. A KiCad 7+ outline may
    hold (arc (start x y) (mid x y) (end x y)) segments among its points (a
    custom pad with rounded corners); with `arcs` each is sampled in its
    place (_arc_points, to within tol mm when given), without it they are
    skipped, as zone and rule-area outlines are still read."""
    out = []
    for c in node[1:]:
        h = _head(c)
        if h == "xy":
            v = _nums(c)
            if len(v) >= 2:
                out.append((v[0], v[1]))
        elif h == "arc" and arcs:
            ends = [_kid(c, k) for k in ("start", "mid", "end")]
            ends = [_nums(e)[:2] if e else [] for e in ends]
            if all(len(e) == 2 for e in ends):
                out += _arc_points(*(tuple(e) for e in ends), tol=tol)
    return out


def _rot(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate (x,y) about the origin by `deg` using the standard CCW matrix."""
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (c * x - s * y, s * x + c * y)


# ============================================================ data model

@dataclass(frozen=True)
class Track:
    net: str
    layer: str
    width: float
    shape: LineString  # centerline
    # KiCad item uuid (T8): lets board_update/route_edit name this exact item
    # for removal. None on synthetic boards without (uuid ...) tokens.
    uuid: Optional[str] = None

    @cached_property
    def poly(self) -> Polygon:
        return self.shape.buffer(self.width / 2.0, quad_segs=_QUAD_SEGS,
                                 cap_style="round", join_style="round")

    @property
    def length(self) -> float:
        return self.shape.length


# `(via <word> ...)`: KiCad 10 writes blind, buried and micro; KiCad 9 and
# earlier wrote blind for both blind and buried.
_VIA_KINDS = {"blind": "blind", "buried": "buried", "micro": "micro",
              "through": "through"}


@dataclass(frozen=True)
class Via:
    net: str
    at: tuple[float, float]
    diameter: float
    drill: float
    layers: tuple[str, ...]  # copper layers spanned (inclusive)
    uuid: Optional[str] = None  # see Track.uuid
    # KiCad's via type: through | blind | buried | micro (the word after
    # `(via`; KiCad 10 writes none for a through via)
    kind: str = "through"

    @cached_property
    def poly(self) -> Polygon:
        return Point(self.at).buffer(self.diameter / 2.0, quad_segs=_QUAD_SEGS)

    def spans(self, layer: str) -> bool:
        return layer in self.layers


@dataclass(frozen=True)
class Pad:
    ref: str
    number: str
    net: Optional[str]
    shape: str            # rect | roundrect | circle | oval | (trapezoid/custom)
    size: tuple[float, float]
    center: tuple[float, float]
    angle: float          # absolute board-frame degrees
    rratio: float
    layers: tuple[str, ...]  # copper layers occupied
    # Drill geometry (T6, P7A-4): (w, h) mm - circle drills store (d, d),
    # `(drill oval w h)` stores (w, h); None for SMD pads. drill_offset is
    # the pad-local `(drill ... (offset x y))`, rotated with the pad. KiCad
    # keeps the HOLE at the pad's (at), `center`, and moves the COPPER by the
    # offset (pcbnew PAD::GetOffset, checked in SWIG 2026-10-04), whatever
    # the token's name suggests: poly is offset, drill_poly is not.
    drill: Optional[tuple[float, float]] = None
    drill_offset: tuple[float, float] = (0.0, 0.0)
    # KiCad's "Castellated pad" fabrication property, `(property
    # pad_prop_castellated)` - a plated half-hole on the board edge
    # (lib/castellation.py).
    castellated: bool = False
    # A custom pad's copper in the pad's own frame (its primitives unioned
    # with its anchor), or None: printed RF structures, net ties and odd
    # footprints are custom pads, and their bounding box is not their copper.
    custom: Optional[object] = None
    # The pad's type token: smd | thru_hole | np_thru_hole | connect.
    kind: str = ""
    # The symbol pin's name as the board stores it, `(pinfunction "SW")`
    # ("" when the board has none); check_current finds a regulator's
    # switch node by it.
    pinfunction: str = ""
    # The symbol pin's electrical type, `(pintype "power_out")` ("" when the
    # board has none); a "+no_connect" suffix is dropped.
    pintype: str = ""
    # How a zone of its net joins it: the pad's `(zone_connect N)`, else its
    # footprint's, as ZONE_CONNECT_* (-1: the zone's own). The thermal
    # overrides likewise (the pad's `(thermal_gap x)`, `(thermal_bridge_width
    # x)` and `(thermal_bridge_angle x)`, or the footprint's): None to take
    # the zone's, and KiCad's spoke angle default (thermal_spokes) for it.
    zone_connect: int = -1
    thermal_gap: Optional[float] = None
    spoke_width: Optional[float] = None
    spoke_angle: Optional[float] = None
    # The pad's own `(clearance x)`, else its footprint's: None when neither
    # sets one. KiCad 10's DRC holds a pair with one to it in place of every
    # netclass and .kicad_dru clearance (barriers included), floored only by
    # the board setup's minimum clearance; physical and hole clearances
    # still hold (checked with kicad-cli, #1275).
    clearance: Optional[float] = None

    @cached_property
    def copper_center(self) -> tuple[float, float]:
        """Centre of the pad's copper: `center` moved by the drill offset,
        turned with the pad the same -angle way drill_poly's shape is."""
        ox, oy = self.drill_offset
        if not ox and not oy:
            return self.center
        o = affinity.rotate(Point(ox, oy), -self.angle, origin=(0, 0),
                            use_radians=False)
        return (self.center[0] + o.x, self.center[1] + o.y)

    @cached_property
    def poly(self) -> Polygon:
        if self.custom is not None and not self.custom.is_empty:
            g = affinity.rotate(self.custom, -self.angle, origin=(0, 0), use_radians=False)
            return affinity.translate(g, self.copper_center[0], self.copper_center[1])
        return _pad_polygon(self.shape, self.size[0], self.size[1],
                            self.rratio, self.copper_center, self.angle)

    @cached_property
    def drill_poly(self) -> Polygon:
        """Hole extents as board-frame copper-free area: a disc for round
        drills, a stadium for `(drill oval w h)` slots. The SHAPE rotates by
        -self.angle - the pad's stored angle is the ABSOLUTE board angle
        (LEARNINGS [routing][stitch][drc] 2026-07-28: using prot - frot
        silently yields a drill turned 90 deg). Empty polygon for SMD pads.
        """
        if self.drill is None:
            return Polygon()
        w, h = self.drill
        if w <= 0 or h <= 0:
            return Polygon()
        if abs(w - h) < 1e-9:
            g = Point(0, 0).buffer(w / 2.0, quad_segs=_QUAD_SEGS)
        elif w > h:
            half = (w - h) / 2.0
            g = LineString([(-half, 0), (half, 0)]).buffer(
                h / 2.0, quad_segs=_QUAD_SEGS)
        else:
            half = (h - w) / 2.0
            g = LineString([(0, -half), (0, half)]).buffer(
                w / 2.0, quad_segs=_QUAD_SEGS)
        g = affinity.rotate(g, -self.angle, origin=(0, 0), use_radians=False)
        return affinity.translate(g, self.center[0], self.center[1])

    def on(self, layer: str) -> bool:
        return layer in self.layers


@dataclass
class Zone:
    net: str
    layers: tuple[str, ...]           # declared layers
    fills: dict[str, list[Polygon]]   # layer -> filled polygons (may be empty)
    zone_id: int
    # The zone's own `(connect_pads (clearance x))`: the least gap a fill
    # leaves to other nets' copper (netclass or DRU rules can only widen
    # it). None when the zone does not say.
    clearance: Optional[float] = None
    # `(name ...)` and the first outline ring `(polygon (pts ...))`
    name: str = ""
    outline: tuple = ()
    # `(attr (teardrop ...))`: KiCad builds one of these per track joining a
    # teardropped via or pad. It is copper of that net but not a pour, so a
    # "nets with a zone are planes" rule must skip it (#633).
    teardrop: bool = False
    # How the fill joins its net's pads, `(connect_pads [yes|no|
    # thru_hole_only] ...)`: ZONE_CONNECT_* (thermal when the token has no
    # keyword, as KiCad reads it).
    connect: int = 1
    # `(fill (thermal_gap x) (thermal_bridge_width x))` and
    # `(min_thickness x)`; None when the zone does not say (KiCad's 0.5,
    # 0.5 and 0.25 then).
    thermal_gap: Optional[float] = None
    spoke_width: Optional[float] = None
    min_thickness: Optional[float] = None
    uuid: Optional[str] = None  # see Track.uuid

    @property
    def filled(self) -> bool:
        return any(polys for polys in self.fills.values())

    def fill_on(self, layer: str) -> MultiPolygon | Polygon:
        """The union of this zone's fill on `layer`, built once: fills are
        never changed after parsing, and callers that compare geometry by
        identity (check_current's pour stages) need the same object back."""
        cache = self.__dict__.setdefault("_fill_on", {})
        if layer not in cache:
            cache[layer] = _union(self.fills.get(layer, []))
        return cache[layer]

    def fill_area(self, layer: str) -> float:
        return self.fill_on(layer).area


@dataclass
class Stackup:
    """Physical layer stack. Copper order is authoritative (from the board's
    (layers) list, top->bottom); dielectric heights + epsilon_r come from a
    (stackup) block when present, else documented FR4 defaults (assumed=True)."""
    copper_layers: list[str]
    total_thickness: float
    dielectrics: list[dict]  # per gap: {above, below, height, epsilon_r, kind,
                             # loss_tangent (None: not in the block), material}
    copper_thickness: dict[str, float]
    assumed: bool
    source: str

    def index(self, layer: str) -> int:
        return self.copper_layers.index(layer)

    def is_outer(self, layer: str) -> bool:
        return layer in (self.copper_layers[0], self.copper_layers[-1])

    def adjacent(self, layer: str) -> tuple[Optional[str], Optional[str]]:
        """(above, below) copper neighbours in the stack, or None at an edge."""
        i = self.index(layer)
        above = self.copper_layers[i - 1] if i > 0 else None
        below = self.copper_layers[i + 1] if i < len(self.copper_layers) - 1 else None
        return above, below

    def height_between(self, a: str, b: str) -> float:
        i, j = sorted((self.index(a), self.index(b)))
        return sum(d["height"] for d in self.dielectrics[i:j])

    def epsilon_between(self, a: str, b: str) -> float:
        i, j = sorted((self.index(a), self.index(b)))
        gaps = self.dielectrics[i:j] or self.dielectrics
        return sum(d["epsilon_r"] for d in gaps) / len(gaps)

    def side(self, layer: str, ref: str) -> tuple[float, float, str]:
        """(height, epsilon_r, kind) of the dielectric from `layer` to `ref`,
        as impedance.stripline_sides takes it; kind "" unless they are
        neighbours (core | prepreg, from the stackup block)."""
        i, j = sorted((self.index(layer), self.index(ref)))
        kind = self.dielectrics[i].get("kind", "") if j == i + 1 else ""
        return self.height_between(layer, ref), self.epsilon_between(layer, ref), kind

    def tand_between(self, a: str, b: str) -> tuple[Optional[float], str]:
        """(tan d, material) of the dielectric between two copper layers. tan d
        is weighted by height, None when no gap carries one."""
        i, j = sorted((self.index(a), self.index(b)))
        gaps = [d for d in self.dielectrics[i:j] if d.get("loss_tangent") is not None]
        h = sum(d["height"] for d in gaps)
        if not gaps or h <= 0:
            return None, ""
        return (sum(d["height"] * d["loss_tangent"] for d in gaps) / h,
                gaps[0].get("material") or "")

    def as_dict(self) -> dict:
        return {
            "copper_layers": self.copper_layers,
            "total_thickness": self.total_thickness,
            "dielectrics": self.dielectrics,
            "copper_thickness": self.copper_thickness,
            "assumed": self.assumed,
            "source": self.source,
        }


# ============================================================ geometry builders

def _pad_polygon(shape: str, w: float, h: float, rratio: float,
                 center: tuple[float, float], angle: float) -> Polygon:
    """Copper polygon for a pad, centred at `center`, oriented `angle` deg.

    Built at the origin then rotated by -angle (KiCad CW-positive with Y down)
    and translated. All corpus shapes are axis-symmetric, so the rotation sign
    does not change the covered region; only `center` (fixed by the footprint
    transform in the caller) matters. trapezoid falls back to the size
    bounding box; a custom pad with primitives is built by _custom_pad.
    """
    w = max(w, 0.0)
    h = max(h, 0.0)
    if shape == "circle":
        g = Point(0, 0).buffer(w / 2.0, quad_segs=_QUAD_SEGS)
    elif shape == "oval":
        if w >= h:
            r, half = h / 2.0, (w - h) / 2.0
            line = LineString([(-half, 0), (half, 0)]) if half > 0 else None
        else:
            r, half = w / 2.0, (h - w) / 2.0
            line = LineString([(0, -half), (0, half)])
        g = (line.buffer(r, quad_segs=_QUAD_SEGS) if half > 0
             else Point(0, 0).buffer(r, quad_segs=_QUAD_SEGS))
    elif shape == "roundrect":
        # KiCad clamps rratio to [0, 0.5]; malformed footprints (easyeda2kicad)
        # can exceed it, and an unclamped r inverts the inner box -> garbage
        # polygon LARGER than the pad. Clamp; r == min/2 is a stadium (valid).
        r = min(max(0.0, rratio) * min(w, h), min(w, h) / 2.0)
        if r <= 0:
            g = box(-w / 2, -h / 2, w / 2, h / 2)
        else:
            inner = box(-w / 2 + r, -h / 2 + r, w / 2 - r, h / 2 - r)
            g = inner.buffer(r, quad_segs=_QUAD_SEGS, join_style="round")
    else:  # rect, trapezoid, custom -> bounding box
        g = box(-w / 2, -h / 2, w / 2, h / 2)
    g = affinity.rotate(g, -angle, origin=(0, 0), use_radians=False)
    return affinity.translate(g, center[0], center[1])


# The last pcbnew file version without fill tokens: KiCad fills a gr_poly or
# gr_rect that names no fill in a file this old (#1165).
LEGACY_FILL_VERSION = 20210108


def _has_fill_token(node) -> bool:
    """Whether any (fill ...) node is anywhere under `node`."""
    stack = [node]
    while stack:
        n = stack.pop()
        if _head(n) == "fill":
            return True
        stack.extend(x for x in n[1:] if _is_node(x))
    return False


def legacy_fill(root) -> bool:
    """Whether a closed poly or rect that names no fill is filled in this file,
    decided per file (#1165): yes when it is KiCad 5 format - a `(module` head,
    no (version), or a version up to LEGACY_FILL_VERSION - or when it has no
    fill token anywhere (easyeda2kicad writes `(gr_poly (pts ...) (width 0.1))`
    and nothing else). A newer file that writes fill tokens means no fill by
    leaving one out, so its stroked, fill-less shapes stay outlines."""
    if _head(root) == "module":
        return True
    vn = _kid(root, "version")
    ver = _nums(vn)[0] if vn is not None and _nums(vn) else None
    if ver is None or ver <= LEGACY_FILL_VERSION:
        return True
    return not _has_fill_token(root)


def _copper_prim(n, legacy=False):
    """One copper graphic's shape in its own frame: a pad primitive (gr_*) or a
    footprint graphic (fp_*), filled or stroked. None when it isn't one.
    `legacy` (see legacy_fill): a poly or rect with no fill token is filled; a
    circle with none stays a ring, as KiCad 5 drew it."""
    kind = _head(n)[3:]
    wn = _kid(n, "width")
    st = _kid(n, "stroke")
    if wn is None and st is not None:
        wn = _kid(st, "width")
    sw = _nums(wn)[0] if wn is not None and _nums(wn) else 0.0
    fill = _kid(n, "fill")
    if fill is None:
        filled = legacy and kind in ("poly", "rect")
    else:
        filled = any(_tok(x) in ("yes", "solid", "true") for x in fill[1:])
    try:
        if kind == "poly":
            pts = _pts(_kid(n, "pts"), arcs=True)
            if len(pts) >= 3:
                g = Polygon(pts)
                if not g.is_valid:
                    g = g.buffer(0)
                if filled or sw <= 0:
                    return g.buffer(sw / 2.0) if sw > 0 else g
                return g.exterior.buffer(sw / 2.0)
        elif kind == "rect":
            a, b = _nums(_kid(n, "start")), _nums(_kid(n, "end"))
            g = box(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
            return g.buffer(sw / 2.0, join_style="mitre") if filled or sw <= 0 \
                else g.exterior.buffer(sw / 2.0)
        elif kind == "circle":
            c, e = _nums(_kid(n, "center")), _nums(_kid(n, "end"))
            r = math.hypot(e[0] - c[0], e[1] - c[1])
            g = Point(c[0], c[1]).buffer(r, quad_segs=_QUAD_SEGS)
            return g.buffer(sw / 2.0) if filled or sw <= 0 else g.exterior.buffer(sw / 2.0)
        elif kind == "line":
            a, b = _nums(_kid(n, "start")), _nums(_kid(n, "end"))
            return LineString([a[:2], b[:2]]).buffer(max(sw, 1e-3) / 2.0)
        elif kind == "arc":
            a, m, b = (_nums(_kid(n, k)) for k in ("start", "mid", "end"))
            return LineString(_arc_points(tuple(a[:2]), tuple(m[:2]), tuple(b[:2]))) \
                .buffer(max(sw, 1e-3) / 2.0)
    except (TypeError, IndexError, ValueError):
        pass
    return None


def _custom_pad(pad, w: float, h: float, legacy=False):
    """A custom pad's copper in its own frame: the anchor (rect or circle,
    `size`) unioned with its primitives - gr_poly (filled, or its outline
    stroked), gr_rect, gr_circle, gr_line, gr_arc. None when it has none,
    so the caller keeps the old bounding-box fallback."""
    prims = _kid(pad, "primitives")
    if prims is None:
        return None
    anchor = "rect"
    opts = _kid(pad, "options")
    if opts is not None and _kid(opts, "anchor") is not None:
        anchor = _tok(_kid(opts, "anchor")[1]) if len(_kid(opts, "anchor")) > 1 else "rect"
    parts = [Point(0, 0).buffer(w / 2.0, quad_segs=_QUAD_SEGS) if anchor == "circle"
             else box(-w / 2, -h / 2, w / 2, h / 2)]
    for n in prims[1:]:
        if _is_node(n):
            parts.append(_copper_prim(n, legacy))
    return _union(parts)


def _union(geoms: Iterable) -> MultiPolygon | Polygon:
    geoms = [g for g in geoms if g is not None and not g.is_empty]
    if not geoms:
        return Polygon()
    if len(geoms) == 1:
        return geoms[0]
    return unary_union(geoms)


def _arc_radius(start, mid, end) -> Optional[float]:
    """Exact radius of the circle through three points (None if colinear)."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return None
    cx = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1)
          + (x3**2 + y3**2) * (y1 - y2)) / d
    cy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3)
          + (x3**2 + y3**2) * (x2 - x1)) / d
    return math.hypot(x1 - cx, y1 - cy)


def _arc_points(start, mid, end, n: int = 16,
                tol: float | None = None) -> list[tuple[float, float]]:
    """Sample a circular arc through three points (used for track/edge
    arcs) in n steps, or more where tol (mm) is given: enough that no
    chord strays further than tol from the arc."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, mid, end]  # colinear -> polyline
    ux = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1)
          + (x3**2 + y3**2) * (y1 - y2)) / d
    uy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3)
          + (x3**2 + y3**2) * (x2 - x1)) / d
    cx, cy = ux, uy
    a1 = math.atan2(y1 - cy, x1 - cx)
    a2 = math.atan2(y2 - cy, x2 - cx)
    a3 = math.atan2(y3 - cy, x3 - cx)
    # ensure the sampled sweep passes through mid
    def norm(a, ref):
        while a < ref:
            a += 2 * math.pi
        return a
    a3u = norm(a3, a1)
    a2u = norm(a2, a1)
    if not (a1 <= a2u <= a3u):  # sweep the other way
        a3u = a3 - 2 * math.pi if a3 > a1 else a3
    r = math.hypot(x1 - cx, y1 - cy)
    if tol is not None and tol > 0 and r > tol:
        # a chord over angle t strays r * (1 - cos(t / 2)) from the arc
        n = max(n, math.ceil(abs(a3u - a1) / (2 * math.acos(1 - tol / r))))
    pts = [(cx + r * math.cos(a1 + (a3u - a1) * t / n),
            cy + r * math.sin(a1 + (a3u - a1) * t / n)) for t in range(n + 1)]
    # Pin the sampled ends to the DECLARED endpoints. Recomputing them from the
    # centre+angle lands ~1e-14 off, which is enough that an arc and the line
    # meeting it never node in unary_union - polygonize then finds no closed
    # face and a rounded board outline silently parses as POLYGON EMPTY.
    pts[0], pts[-1] = start, end
    return pts


# ============================================================ stackup model

def _build_stackup(root, copper_layers: list[str], total_thickness: float) -> Stackup:
    setup = _kid(root, "setup")
    stk = _kid(setup, "stackup") if setup is not None else None
    n = len(copper_layers)
    cu_th = {l: (_CU_OUTER if l in (copper_layers[0], copper_layers[-1]) else _CU_INNER)
             for l in copper_layers}

    if stk is not None:
        # Parse dielectric sublayers between coppers from an explicit stackup.
        dielectrics: list[dict] = []
        gap: list[dict] = []
        order: list[tuple[str, dict]] = []
        for layer in _kids(stk, "layer"):
            name = _strs(layer)[0] if _strs(layer) else ""
            th = _kid(layer, "thickness")
            er = _kid(layer, "epsilon_r")
            ty = _kid(layer, "type")
            lt = _kid(layer, "loss_tangent")
            mat = _kid(layer, "material")
            entry = {
                "height": _nums(th)[0] if th is not None and _nums(th) else 0.0,
                "epsilon_r": _nums(er)[0] if er is not None and _nums(er) else _FR4_ER,
                "type": (_strs(ty)[0] if ty is not None and _strs(ty) else ""),
                "loss_tangent": _nums(lt)[0] if lt is not None and _nums(lt) else None,
                "material": (_strs(mat)[0] if mat is not None and _strs(mat) else ""),
            }
            order.append((name, entry))
        # Walk copper-to-copper, summing dielectric sublayers between them.
        cu_names = set(copper_layers)
        cur: Optional[str] = None
        acc_h = 0.0
        acc_er: list[float] = []
        acc_ty: list[str] = []
        acc_lt: list[tuple[float, float]] = []
        acc_mat = ""
        for name, entry in order:
            if name in cu_names:
                if cur is not None:
                    dielectrics.append({
                        "above": cur, "below": name, "height": acc_h,
                        "epsilon_r": (sum(acc_er) / len(acc_er)) if acc_er else _FR4_ER,
                        # core | prepreg (a gap of both reads prepreg: it
                        # is pressed, not a laminate bought whole)
                        "kind": ("core" if acc_ty and set(acc_ty) == {"core"}
                                 else "prepreg" if acc_ty else ""),
                        "loss_tangent": (sum(h * v for h, v in acc_lt) / sum(h for h, _ in acc_lt)
                                         if acc_lt and sum(h for h, _ in acc_lt) > 0 else None),
                        "material": acc_mat,
                    })
                cur = name
                acc_h, acc_er, acc_ty, acc_lt, acc_mat = 0.0, [], [], [], ""
                if entry["height"] > 0:  # copper thickness from the block
                    cu_th[name] = entry["height"]
            else:
                acc_h += entry["height"]
                if entry["epsilon_r"]:
                    acc_er.append(entry["epsilon_r"])
                if entry["type"] in ("core", "prepreg"):
                    acc_ty.append(entry["type"])
                if entry["loss_tangent"] is not None:
                    acc_lt.append((entry["height"], entry["loss_tangent"]))
                acc_mat = acc_mat or entry["material"]
        if len(dielectrics) == n - 1:
            return Stackup(copper_layers, total_thickness, dielectrics, cu_th,
                           assumed=False, source="board (stackup)")

    # No usable stackup block -> documented FR4 defaults.
    cu_total = sum(cu_th.values())
    dielec_total = max(total_thickness - cu_total, 0.1)
    if n == 2:
        heights = [dielec_total]
    elif n == 4:
        # JLC-typical 1.6 mm ratio: prepreg / core / prepreg ~= 0.165/0.67/0.165.
        heights = [dielec_total * 0.165, dielec_total * 0.67, dielec_total * 0.165]
    else:
        heights = [dielec_total / (n - 1)] * (n - 1) if n > 1 else []
    dielectrics = [{"above": copper_layers[i], "below": copper_layers[i + 1],
                    "height": heights[i], "epsilon_r": _FR4_ER}
                   for i in range(n - 1)]
    return Stackup(copper_layers, total_thickness, dielectrics, cu_th,
                   assumed=True, source="default FR4 (no stackup block)")


# ============================================================ BoardGeom

class BoardGeom:
    """Parsed, net-indexed geometry for one .kicad_pcb. Build via
    `BoardGeom.from_file(path)` or the cached `load_board(path)`."""

    def __init__(self, path: Path, root: list):
        self.path = Path(path)
        self._union_cache: dict[tuple, object] = {}
        # an absent fill on a poly or rect means filled in this file (#1165)
        self.legacy_fill = legacy_fill(root)

        # ---- layers (copper order = file order, top->bottom) ----
        self._layer_by_ord: dict[int, str] = {}
        # canonical name -> the board's own name for it (`(5 "F.SilkS" user
        # "F.Silkscreen")`), which a DRU rule may name the layer by (#1140)
        self.layer_user_names: dict[str, str] = {}
        copper: list[str] = []
        layers_node = _kid(root, "layers")
        if layers_node is None:
            raise GeomError(f"no (layers) block in {path}")
        # KiCad 5 and older let a copper layer be renamed (cmm2-jlc's "GND"
        # and "POWER"), and every item then names it so: map those names to
        # the canonical In<n>.Cu by ordinal before anything reads a layer.
        self.layer_aliases = _canonical_copper_names(root, layers_node)
        for entry in layers_node[1:]:
            if not _is_node(entry):
                continue
            ordn = int(entry[0]) if isinstance(entry[0], (int, float)) else None
            name = _strs(entry)[0] if _strs(entry) else None
            if name is None:
                continue
            own = [x for x in entry[2:] if isinstance(x, str)
                   and not isinstance(x, sexpdata.Symbol)]
            if own and own[0] != name:
                self.layer_user_names[name] = own[0]
            if ordn is not None:
                self._layer_by_ord[ordn] = name
            if name.endswith(".Cu"):
                copper.append(name)
        if not copper:
            raise GeomError(f"no copper layers in {path}")
        self.copper_layers = copper
        self._copper_set = set(copper)

        gen = _kid(root, "general")
        thick_node = _kid(gen, "thickness") if gen is not None else None
        self.thickness = _nums(thick_node)[0] if thick_node and _nums(thick_node) else 1.6

        # ---- optional numeric net table (num -> name); items usually name-only ----
        self._net_table: dict[int, str] = {}
        for netn in _kids(root, "net"):
            nums = [a for a in netn[1:] if isinstance(a, (int, float))]
            strs = _strs(netn)
            if nums:
                self._net_table[int(nums[0])] = strs[0] if strs else ""

        self.stackup = _build_stackup(root, self.copper_layers, self.thickness)

        # ---- primitives ----
        self._tracks: list[Track] = []
        self._vias: list[Via] = []
        self._pads: list[Pad] = []
        self._zones: list[Zone] = []
        self.rule_areas: list[dict] = []  # keepout areas: {name, layers, outline}
        # zones on layers missing from the stack (not copper; see _parse_zones)
        self.off_stack_zones: list[dict] = []
        self.footprint_rule_areas: list[dict] = []  # the same, inside footprints
        # every np_thru_hole pad, with or without copper layers (a pad with
        # none is not in _pads): route_auto keeps the router off them and
        # stitch_vias keeps its drills hole-to-hole clear of them
        self.npth: list[Pad] = []
        # copper a footprint draws outside its pads, e.g. a net tie's bridge:
        # {"ref", "layer", "shape", "nets"}, nets being those of its pads
        self.fp_copper: list[dict] = []
        # each footprint that declares net_tie_pad_groups (KiCad's net tie: a
        # Bridged solder jumper, a NetTie), whose own copper joins each
        # group's pads (check_net_tie): {"ref", "lib", "layer" (its side),
        # "at", "groups" (lists of pad numbers), "pads" (its Pads),
        # "graphics" (its drawings on every layer: {"layer", "shape"})}
        self.net_ties: list[dict] = []
        self.footprints: dict[str, dict] = {}  # ref -> {"lib", "value", "props"}
        self._parse_tracks(root)
        self._parse_vias(root)
        self._parse_footprints(root)
        self._parse_zones(root)
        self.outline = self._parse_outline(root)
        self.edge_faces = _edge_faces(root)

        self.nets: set[str] = set()
        for coll in (self._tracks, self._vias, self._zones):
            self.nets.update(x.net for x in coll if x.net)
        self.nets.update(p.net for p in self._pads if p.net)

    # -------------------------------------------------- construction
    @classmethod
    def from_file(cls, path) -> "BoardGeom":
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GeomError(f"cannot read {path}: {exc}") from exc
        try:
            root = sexpdata.loads(text)
        except Exception as exc:  # sexpdata raises various parse errors
            raise GeomError(f"s-expr parse failed for {path}: {exc}") from exc
        if _head(root) != "kicad_pcb":
            raise GeomError(f"not a kicad_pcb file: {path}")
        return cls(path, root)

    def _resolve_net(self, item) -> Optional[str]:
        node = _kid(item, "net")
        if node is None:
            return None
        strs = _strs(node)
        if strs:
            return strs[-1]  # (net "NAME") or (net N "NAME")
        nums = [a for a in node[1:] if isinstance(a, (int, float))]
        if nums:
            return self._net_table.get(int(nums[0]))
        return None

    def _copper_of(self, layers_node) -> tuple[str, ...]:
        """Copper layers named by a (layers ...) node, expanding *.Cu wildcards
        and the F&B.Cu front-and-back shorthand (zones/rule areas use it)."""
        out: list[str] = []
        for tok in _strs(layers_node) if layers_node is not None else []:
            if "*" in tok and tok.endswith(".Cu"):
                out = list(self.copper_layers)
                break
            if tok == "F&B.Cu":
                out += [n for n in ("F.Cu", "B.Cu")
                        if n in self._copper_set and n not in out]
                continue
            if tok in self._copper_set and tok not in out:
                out.append(tok)
        return tuple(out)

    def _zone_layers(self, zone) -> tuple[str, ...]:
        """Declared copper layers of a zone/rule-area node ((layers) or (layer))."""
        layers_node = _kid(zone, "layers")
        if layers_node is not None:
            return self._copper_of(layers_node)
        layer = _kid(zone, "layer")
        if layer is not None and _strs(layer):
            ln = _strs(layer)[0]
            if ln in self._copper_set:
                return (ln,)
        return ()

    def _parse_tracks(self, root):
        for seg in _kids(root, "segment"):
            s, e = _kid(seg, "start"), _kid(seg, "end")
            w, layer = _kid(seg, "width"), _kid(seg, "layer")
            if not (s and e and w and layer):
                continue
            ln = _strs(layer)[0] if _strs(layer) else None
            if ln not in self._copper_set:
                continue
            a, b = _nums(s), _nums(e)
            self._tracks.append(Track(
                net=self._resolve_net(seg) or "", layer=ln,
                width=_nums(w)[0], shape=LineString([tuple(a[:2]), tuple(b[:2])]),
                uuid=_uuid_of(seg)))
        for arc in _kids(root, "arc"):
            s, m, e = _kid(arc, "start"), _kid(arc, "mid"), _kid(arc, "end")
            w, layer = _kid(arc, "width"), _kid(arc, "layer")
            if not (s and m and e and w and layer):
                continue
            ln = _strs(layer)[0] if _strs(layer) else None
            if ln not in self._copper_set:
                continue
            pts = _arc_points(tuple(_nums(s)[:2]), tuple(_nums(m)[:2]), tuple(_nums(e)[:2]))
            self._tracks.append(Track(
                net=self._resolve_net(arc) or "", layer=ln,
                width=_nums(w)[0], shape=LineString(pts), uuid=_uuid_of(arc)))

    def _parse_vias(self, root):
        for via in _kids(root, "via"):
            at = _kid(via, "at")
            size = _kid(via, "size")
            drill = _kid(via, "drill")
            layers = _kid(via, "layers")
            if not (at and size):
                continue
            # A via's (layers A B) is a from/to SPAN, not a literal set: a
            # through via has copper on every copper layer between A and B.
            names = [t for t in _strs(layers) if t in self._copper_set] if layers else []
            if len(names) >= 2:
                i, j = sorted((self.copper_layers.index(names[0]),
                               self.copper_layers.index(names[-1])))
                spanned = tuple(self.copper_layers[i:j + 1])
            elif len(names) == 1:
                spanned = (names[0],)
            else:
                spanned = tuple(self.copper_layers)  # default through-via
            a = _nums(at)
            words = [_tok(w) for w in via[1:] if isinstance(w, sexpdata.Symbol)]
            kind = next((w for w in words if w in _VIA_KINDS), "through")
            self._vias.append(Via(
                net=self._resolve_net(via) or "", at=(a[0], a[1]),
                diameter=_nums(size)[0],
                drill=_nums(drill)[0] if drill and _nums(drill) else 0.0,
                layers=spanned, uuid=_uuid_of(via),
                kind=_VIA_KINDS.get(kind, "through")))

    def _parse_footprints(self, root):
        for fp in _kids(root, "footprint"):
            # A zone in a footprint is saved in board coordinates, not the
            # footprint's, and DRC enforces its keepout like any other.
            self.footprint_rule_areas += [self._rule_area(z) for z in _kids(fp, "zone")
                                          if _kid(z, "keepout") is not None]
            at = _kid(fp, "at")
            fnums = _nums(at) if at is not None else [0, 0, 0]
            fx, fy = fnums[0], fnums[1]
            fangle = fnums[2] if len(fnums) > 2 else 0.0
            ref, value, props = "?", None, {}
            for prop in _kids(fp, "property"):
                pv = _strs(prop)
                if len(pv) >= 2 and pv[0] == "Reference":
                    ref = pv[1]
                elif len(pv) >= 2 and pv[0] == "Value":
                    value = pv[1]
                elif len(pv) >= 2:
                    props[pv[0]] = pv[1]
            lib = _strs(fp)[0] if _strs(fp) else ""
            # ref -> lib id + Value field (check_current bounds a resistor
            # branch's current from these; check_thermal knows a package by
            # its LCSC/MPN props); the last duplicate ref wins
            # attrs: KiCad's (attr smd dnp ...) flags; check_pdn leaves a
            # dnp cap out of a rail's reservoir
            attr = _kid(fp, "attr")
            self.footprints[ref] = {"lib": lib, "value": value, "props": props,
                                    "at": (fx, fy), "angle": fangle,
                                    "attrs": tuple(str(_tok(x)) for x in _strs(attr))
                                    if attr is not None else ()}
            first_pad = len(self._pads)
            fp_th = dict(_thermal_of(fp), clearance=_num_of(fp, "clearance"))
            for pad in _kids(fp, "pad"):
                self._add_pad(pad, fx, fy, fangle, ref, fp_th)
            nets = frozenset(p.net for p in self._pads[first_pad:] if p.net)
            self._add_fp_copper(fp, fx, fy, fangle, ref, nets)
            tie = _kid(fp, "net_tie_pad_groups")
            if tie is not None:
                side = _kid(fp, "layer")
                self.net_ties.append({
                    "ref": ref, "lib": lib,
                    "layer": _strs(side)[0] if side and _strs(side) else "F.Cu",
                    "at": (fx, fy),
                    # KiCad splits each group string on commas
                    "groups": [nums for nums in (
                        [t.strip() for t in g.split(",") if t.strip()]
                        for g in _strs(tie)) if nums],
                    "pads": self._pads[first_pad:],
                    "graphics": [{"layer": ln, "shape": shape} for ln, shape in
                                 self._fp_graphics(fp, fx, fy, fangle)]})

    def _fp_graphics(self, fp, fx, fy, fangle, layers=None):
        """(layer, board-frame shape) of each fp_poly, fp_rect, fp_circle,
        fp_line and fp_arc the footprint draws, on `layers` (a set) or on
        any layer. check_wicking calls it unbound, with self None: then a
        fill-less shape reads as the modern format has it."""
        legacy = self.legacy_fill if self is not None else False
        c, s = _rot(1.0, 0.0, -fangle)
        for kind in ("fp_poly", "fp_rect", "fp_circle", "fp_line", "fp_arc"):
            for n in _kids(fp, kind):
                layer = _kid(n, "layer")
                ln = _strs(layer)[0] if layer is not None and _strs(layer) else None
                if ln is None or (layers is not None and ln not in layers):
                    continue
                g = _copper_prim(n, legacy)
                if g is not None and not g.is_empty:
                    yield ln, affinity.affine_transform(g, [c, -s, s, c, fx, fy])

    def _add_fp_copper(self, fp, fx, fy, fangle, ref, nets):
        for ln, shape in self._fp_graphics(fp, fx, fy, fangle, self._copper_set):
            self.fp_copper.append({"ref": ref, "layer": ln, "nets": nets,
                                   "shape": shape})

    def _add_pad(self, pad, fx, fy, fangle, ref, fp_th=None):
        """Back-side (flipped) footprints need NO special handling: pcbnew
        Flip() bakes the mirror into the stored values - pad locals are
        already mirrored, angles negated, and the pad (layers) list renamed
        to B.* in the file - so the same fp + R(-fp_angle).local transform
        reproduces pcbnew's positions for front AND back parts
        (S3-verified against a SWIG-flipped board; V10, LEARNINGS [geometry]).
        """
        # positional: (pad "num" <type> <shape> ...)
        number = str(pad[1]) if len(pad) > 1 else "?"
        pshape = _tok(pad[3]) if len(pad) > 3 else "rect"
        at = _kid(pad, "at")
        size = _kid(pad, "size")
        if at is None or size is None:
            return
        lx, ly, *rest = _nums(at)
        pad_angle = rest[0] if rest else 0.0
        # absolute center = fp + R(-fangle) . (lx, ly)
        dx, dy = _rot(lx, ly, -fangle)
        center = (fx + dx, fy + dy)
        sz = _nums(size)
        w, h = sz[0], (sz[1] if len(sz) > 1 else sz[0])
        rr = _kid(pad, "roundrect_rratio")
        rratio = _nums(rr)[0] if rr and _nums(rr) else 0.0
        layers = self._copper_of(_kid(pad, "layers"))
        npth = len(pad) > 2 and _tok(pad[2]) == "np_thru_hole"
        if not layers and not npth:
            return  # no copper (e.g. paste-only pad)
        drill = None
        drill_off = (0.0, 0.0)
        dnode = _kid(pad, "drill")
        if dnode is not None:
            dnums = _nums(dnode)
            oval = any(_tok(x) == "oval" for x in dnode[1:]
                       if isinstance(x, (sexpdata.Symbol, str)))
            if oval and len(dnums) >= 2:
                drill = (dnums[0], dnums[1])
            elif dnums:
                drill = (dnums[0], dnums[0])
            onode = _kid(dnode, "offset")
            if onode is not None:
                on = _nums(onode)
                if len(on) >= 2:
                    drill_off = (on[0], on[1])
        custom = _custom_pad(pad, w, h, self.legacy_fill) if pshape == "custom" else None
        th = dict(_thermal_of(pad), clearance=_num_of(pad, "clearance"))
        for k, v in (fp_th or {}).items():
            if th[k] is None or (k == "zone_connect"
                                 and th[k] == ZONE_CONNECT_INHERIT):
                th[k] = v
        made = Pad(
            custom=custom,
            ref=ref, number=number, net=self._resolve_net(pad),
            shape=pshape, size=(w, h), center=center, angle=pad_angle,
            rratio=rratio, layers=layers, drill=drill,
            drill_offset=drill_off,
            kind=str(_tok(pad[2])) if len(pad) > 2 else "",
            pinfunction=next((str(pf[1]) for pf in _kids(pad, "pinfunction")
                              if len(pf) > 1 and isinstance(pf[1], str)), ""),
            pintype=next((str(pf[1]).split("+")[0]
                          for pf in _kids(pad, "pintype")
                          if len(pf) > 1 and isinstance(pf[1], str)), ""),
            castellated=any(_tok(x) == "pad_prop_castellated"
                            for prop in _kids(pad, "property")
                            for x in prop[1:]),
            zone_connect=ZONE_CONNECT_INHERIT if th["zone_connect"] is None
            else th["zone_connect"],
            thermal_gap=th["thermal_gap"] or None,
            spoke_width=th["spoke_width"] or None,
            spoke_angle=th["spoke_angle"], clearance=th["clearance"])
        if npth and drill:
            self.npth.append(made)
        if layers:
            self._pads.append(made)

    def _rule_area(self, zone) -> dict:
        name_node = _kid(zone, "name")
        poly_node = _kid(zone, "polygon")
        pts = (_pts(_kid(poly_node, "pts"))
               if poly_node is not None and _kid(poly_node, "pts") else [])
        return {
            "name": _strs(name_node)[0] if name_node and _strs(name_node) else "",
            "layers": self._zone_layers(zone),
            "outline": Polygon(pts) if len(pts) >= 3 else Polygon(),
            # Keepout flags ({"footprints": "not_allowed", ...}); not
            # serialised. A rule area that only scopes a rule has all
            # of them "allowed".
            "flags": {_tok(k[0]): _tok(k[1]) for k in _kid(zone, "keepout")[1:]
                      if _is_node(k) and len(k) > 1},
        }

    def _parse_zones(self, root):
        zid = 0
        for zone in _kids(root, "zone"):
            declared = self._zone_layers(zone)
            if not declared:
                named = [t for n in (_kid(zone, "layer"), _kid(zone, "layers"))
                         if n is not None for t in _strs(n)]
                if named and not any(t in self._copper_set or "*" in t
                                     or t == "F&B.Cu" for t in named):
                    # A zone on a layer the stack doesn't have (ambient rev_d,
                    # a 2-layer KiCad 4 board, keeps zones on In1/In2.Cu):
                    # no copper is made of it, so it can't be stale either.
                    self.off_stack_zones.append(
                        {"net": self._resolve_net(zone) or "",
                         "layers": named})
                    continue
            if _kid(zone, "keepout") is not None:
                # Rule area: never fills, carries no copper. It must NOT enter
                # the zone list or the freshness gate would flag it as an
                # eternally-unfilled zone (the plane-split mutant carries one).
                # Outline kept as metadata (S9 placement keepouts).
                self.rule_areas.append(self._rule_area(zone))
                continue
            net = self._resolve_net(zone) or ""
            fills: dict[str, list[Polygon]] = {}
            for fp in _kids(zone, "filled_polygon"):
                fl = _kid(fp, "layer")
                fln = _strs(fl)[0] if fl is not None and _strs(fl) else (
                    declared[0] if declared else None)
                if fln is None:
                    continue
                pts = _pts(_kid(fp, "pts")) if _kid(fp, "pts") is not None else _pts(fp)
                if len(pts) >= 3:
                    poly = Polygon(pts)
                    if not poly.is_valid:
                        poly = poly.buffer(0)
                    if not poly.is_empty:
                        fills.setdefault(fln, []).append(poly)
            cp = _kid(zone, "connect_pads")
            cl = _kid(cp, "clearance") if cp is not None else None
            kw = [_tok(x) for x in cp[1:] if isinstance(x, sexpdata.Symbol)] \
                if cp is not None else []
            fnode = _kid(zone, "fill")
            nm = _kid(zone, "name")
            at = _kid(zone, "attr")
            pg = _kid(zone, "polygon")
            ring = _pts(_kid(pg, "pts")) if pg is not None and \
                _kid(pg, "pts") is not None else []
            self._zones.append(Zone(net=net, layers=declared or tuple(fills.keys()),
                                    fills=fills, zone_id=zid,
                                    clearance=_nums(cl)[0] if cl and _nums(cl)
                                    else None,
                                    name=_strs(nm)[0] if nm is not None
                                    and _strs(nm) else "",
                                    outline=tuple(ring),
                                    teardrop=at is not None
                                    and _kid(at, "teardrop") is not None,
                                    connect={
                                        "yes": ZONE_CONNECT_SOLID,
                                        "no": ZONE_CONNECT_NONE,
                                        "thru_hole_only":
                                            ZONE_CONNECT_THT_THERMAL,
                                    }.get(kw[0] if kw else "",
                                          ZONE_CONNECT_THERMAL),
                                    thermal_gap=_num_of(fnode, "thermal_gap")
                                    if fnode is not None else None,
                                    spoke_width=_num_of(
                                        fnode, "thermal_bridge_width")
                                    if fnode is not None else None,
                                    min_thickness=_num_of(zone,
                                                          "min_thickness"),
                                    uuid=_uuid_of(zone)))
            zid += 1

    def _parse_outline(self, root) -> Polygon:
        """The board outline: the LARGEST closed face Edge.Cuts forms.

        `self.outline_faces` keeps every face, largest first - one face is a
        plain outline, more than one means interior windows or a second board
        in the file. Anything REWRITING Edge.Cuts (board_edit) must look at the
        faces, not just the winner, or it silently drops the others.

        `self.outline_arc_radii` holds every Edge.Cuts arc's EXACT radius (from
        its three declared points). A corner radius read back from the sampled
        polygon's AREA carries the sampling bias, so a board re-edited through
        board_edit would inflate its own radius a little each time.

        `self.outline_items` counts the Edge.Cuts primitives BY KIND. Every
        closed shape (gr_rect, gr_poly, gr_circle) is a face of its own next to
        the faces the lines and arcs close, and the largest wins: a mounting
        hole drawn as a circle is a window, never the board (#1015)."""
        self.outline_faces: list[Polygon] = []
        self.outline_arc_radii: list[float] = []
        self.outline_items: dict[str, int] = {}
        for kind in ("gr_rect", "gr_poly", "gr_circle", "gr_line", "gr_arc"):
            n = sum(1 for g in _kids(root, kind)
                    if _layer_name(g) == "Edge.Cuts")
            if n:
                self.outline_items[kind] = n
        edges = []
        # closed shapes are faces in their own right; they compete with the
        # line/arc faces on area, so a mounting-hole circle or an inner rect
        # window never stands in for a line-drawn outline (#1015)
        shapes: list[Polygon] = []
        for rect in _kids(root, "gr_rect"):
            if _layer_name(rect) != "Edge.Cuts":
                continue
            s, e = _nums(_kid(rect, "start")), _nums(_kid(rect, "end"))
            if len(s) >= 2 and len(e) >= 2:
                shapes.append(box(min(s[0], e[0]), min(s[1], e[1]),
                                  max(s[0], e[0]), max(s[1], e[1])))
        for poly in _kids(root, "gr_poly"):
            if _layer_name(poly) != "Edge.Cuts":
                continue
            pts = _pts(_kid(poly, "pts")) if _kid(poly, "pts") else _pts(poly)
            if len(pts) >= 3:
                shapes.append(Polygon(pts))
        for circ in _kids(root, "gr_circle"):
            if _layer_name(circ) != "Edge.Cuts":
                continue
            c, e = _nums(_kid(circ, "center")), _nums(_kid(circ, "end"))
            if len(c) >= 2 and len(e) >= 2:
                r = math.hypot(e[0] - c[0], e[1] - c[1])
                shapes.append(Point(c[0], c[1]).buffer(
                    r, quad_segs=_QUAD_SEGS * 2))
        for gl in _kids(root, "gr_line"):
            if _layer_name(gl) != "Edge.Cuts":
                continue
            s, e = _nums(_kid(gl, "start")), _nums(_kid(gl, "end"))
            if len(s) >= 2 and len(e) >= 2:
                edges.append(LineString([(s[0], s[1]), (e[0], e[1])]))
        for ga in _kids(root, "gr_arc"):
            if _layer_name(ga) != "Edge.Cuts":
                continue
            s, m, e = _kid(ga, "start"), _kid(ga, "mid"), _kid(ga, "end")
            if s and m and e:
                sp, mp, ep = (tuple(_nums(s)[:2]), tuple(_nums(m)[:2]),
                              tuple(_nums(e)[:2]))
                edges.append(LineString(_arc_points(sp, mp, ep)))
                r = _arc_radius(sp, mp, ep)
                if r is not None:
                    self.outline_arc_radii.append(r)
        faces = list(polygonize(unary_union(edges))) if edges else []
        faces = sorted(shapes + faces, key=lambda g: g.area, reverse=True)
        if faces:
            self.outline_faces = faces
            return faces[0]
        return Polygon()

    # -------------------------------------------------- accessors
    def tracks_of(self, net: str = None, layer: str = None) -> list[Track]:
        return [t for t in self._tracks
                if (net is None or t.net == net) and (layer is None or t.layer == layer)]

    def vias_of(self, net: str = None, layer: str = None) -> list[Via]:
        return [v for v in self._vias
                if (net is None or v.net == net) and (layer is None or v.spans(layer))]

    def pads_of(self, net: str = None, layer: str = None, ref: str = None) -> list[Pad]:
        return [p for p in self._pads
                if (net is None or p.net == net)
                and (layer is None or p.on(layer))
                and (ref is None or p.ref == ref)]

    def zones_of(self, net: str = None, layer: str = None) -> list[Zone]:
        return [z for z in self._zones
                if (net is None or z.net == net)
                and (layer is None or layer in z.layers or layer in z.fills)]

    def zone_fill(self, net: str, layer: str) -> MultiPolygon | Polygon:
        key = ("zone_fill", net, layer)
        if key not in self._union_cache:
            self._union_cache[key] = _union(
                [z.fill_on(layer) for z in self.zones_of(net, layer)])
        return self._union_cache[key]

    def net_copper(self, net: str, layer: str) -> MultiPolygon | Polygon:
        """Union of ALL copper (tracks+pads+vias+zone fill) of `net` on `layer`."""
        key = ("net_copper", net, layer)
        if key not in self._union_cache:
            parts = [t.poly for t in self.tracks_of(net, layer)]
            parts += [p.poly for p in self.pads_of(net, layer)]
            parts += [v.poly for v in self.vias_of(net, layer)]
            parts += [z.fill_on(layer) for z in self.zones_of(net, layer)]
            self._union_cache[key] = _union(parts)
        return self._union_cache[key]

    def layer_copper(self, layer: str, net: str = None,
                     exclude: str = None) -> MultiPolygon | Polygon:
        """Union of copper on a layer. `net` restricts, `exclude` omits a net."""
        key = ("layer_copper", layer, net, exclude)
        if key not in self._union_cache:
            nets = ([net] if net else sorted(self.nets))
            self._union_cache[key] = _union(
                [self.net_copper(n, layer) for n in nets if n != exclude])
        return self._union_cache[key]

    def net_area(self, net: str, layer: str) -> float:
        return self.net_copper(net, layer).area

    def net_area_by_layer(self, net: str) -> dict[str, float]:
        return {l: self.net_area(net, l) for l in self.copper_layers
                if self.net_area(net, l) > 0}

    def adjacent_copper(self, layer: str) -> tuple[Optional[str], Optional[str]]:
        return self.stackup.adjacent(layer)

    def layers_with_zone(self, net: str) -> list[str]:
        out = []
        for l in self.copper_layers:
            if any(z.fills.get(l) for z in self.zones_of(net, l)):
                out.append(l)
        return out

    # -------------------------------------------------- zone-fill freshness
    def fill_status(self) -> dict[int, bool]:
        return {z.zone_id: z.filled for z in self._zones}

    def unfilled_zones(self) -> list[Zone]:
        return [z for z in self._zones if not z.filled]

    def assert_fresh(self, *, refill: bool = False, area_tol: float = 0.01) -> None:
        """Refuse to run on stale/unfilled zones.

        Fast mode (default): raise StaleFillError if any zone has an outline but
        no fill - unless KiCad's own refill leaves that zone empty too (a zone
        drawn where higher-priority copper or clearances leave no room fills to
        nothing; minuet v3's GND zone 257 is one). Thorough mode (refill=True):
        additionally run the S0-verified
        `kicad-cli pcb drc --refill-zones --save-board` on a temp copy and raise
        if any zone's fill area drifts by more than `area_tol` (relative) - i.e.
        the committed fill is stale versus a fresh refill.
        """
        unfilled = [z for z in self.unfilled_zones()
                    if z.zone_id not in self._empty_by_refill()]
        if unfilled:
            why = getattr(self, "_refill_why", None)
            raise StaleFillError(
                f"{len(unfilled)} zone(s) unfilled in {self.path.name}: "
                f"ids {[z.zone_id for z in unfilled]}"
                + (f" (and KiCad could not be asked whether a refill leaves "
                   f"them empty too: {why})" if why else ""))
        if not refill:
            return
        fresh = _refill_copy(self.path)
        try:
            other = BoardGeom.from_file(fresh)
            for z in self._zones:
                for l in z.layers:
                    a0 = z.fill_area(l)
                    a1 = next((oz.fill_area(l) for oz in other._zones
                               if oz.zone_id == z.zone_id), 0.0)
                    denom = max(a0, a1, 1e-6)
                    if abs(a0 - a1) / denom > area_tol:
                        raise StaleFillError(
                            f"zone {z.zone_id} on {l}: committed fill {a0:.3f} mm^2 "
                            f"differs from fresh {a1:.3f} mm^2 (> {area_tol:.0%})")
        finally:
            _cleanup(fresh)

    def _empty_by_refill(self) -> set[int]:
        """Ids of this board's unfilled zones that a fresh KiCad refill leaves
        empty as well. Zones are matched by net, layers and outline (a refill
        renumbers: it regenerates teardrop zones). Empty when there is nothing
        to ask about, or KiCad can't be asked (then every unfilled zone counts
        as stale, as before)."""
        if getattr(self, "_refill_empty", None) is not None:
            return self._refill_empty
        self._refill_empty = set()
        self._refill_why = None
        self._refilled = None
        unfilled = self.unfilled_zones()
        if not unfilled:
            return self._refill_empty
        try:
            fresh = _refill_copy(self.path)
        except KicadCliMissing as exc:          # say so in assert_fresh
            self._refill_why = str(exc)
            return self._refill_empty
        except Exception as exc:                # kicad-cli failed: say why
            self._refill_why = (f"the kicad-cli refill failed: "
                                f"{type(exc).__name__}: {str(exc)[:300]}")
            return self._refill_empty
        try:
            try:
                other = BoardGeom.from_file(fresh)
            except Exception as exc:
                self._refill_why = (f"the refilled copy did not parse: "
                                    f"{type(exc).__name__}: {str(exc)[:300]}")
                return self._refill_empty
            # judged in place of this board by fresh_board(); its path points
            # back here so sibling files (.kicad_pro, schematic) still resolve
            other.path = self.path
            self._refilled = other
            filled = {(z.net, z.layers, z.outline) for z in other._zones if z.filled}
            known = {(z.net, z.layers, z.outline) for z in other._zones}
            self._refill_empty = {z.zone_id for z in unfilled
                                  if (z.net, z.layers, z.outline) in known
                                  and (z.net, z.layers, z.outline) not in filled}
        finally:
            _cleanup(fresh)
        return self._refill_empty

    def stale_patches(self) -> list[dict]:
        """The PATCH_<net> zones (zone_patch) that look left behind (#1424):
        one that holds no via of its net (a via is held when its copper
        meets the outline on one of the zone's layers, as route_edit's
        #1145 check reads it), and one with a layer it fills nothing on.
        Read the second on a fresh board only: after assert_fresh() an
        empty zone is one KiCad's own refill leaves empty too. Each:
        {uuid, name, net, layers, at (a point inside the outline), box
        (patch_region: the outline's bounds for zone_patch), no_via:
        bool, empty: [layers it fills nothing on]}; a zone with
        neither is not listed. zone_patch never writes an empty patch, but
        a strip it pours in corridor mode joins pads and holds no via by
        design, so no_via alone is a hint, not proof."""
        from shapely.geometry import Polygon
        out = []
        for z in self._zones:
            if z.teardrop or not z.outline or z.name != f"PATCH_{z.net}":
                continue
            ring = Polygon(z.outline)
            ring = ring if ring.is_valid else ring.buffer(0)
            lys = [ly for ly in z.layers if ly in self._copper_set]
            no_via = not any(ring.intersects(v.poly)
                             for v in self.vias_of(net=z.net)
                             if any(v.spans(ly) for ly in lys))
            empty = [ly for ly in lys if z.fill_on(ly).is_empty]
            if no_via or empty:
                p = ring.representative_point()
                out.append({"uuid": z.uuid, "name": z.name, "net": z.net,
                            "layers": lys, "at": [p.x, p.y],
                            "box": patch_region(z.outline),
                            "no_via": no_via, "empty": empty})
        return out

    # -------------------------------------------------- summary
    def summary(self) -> dict:
        nets = {}
        for n in sorted(self.nets):
            by = self.net_area_by_layer(n)
            if by:
                nets[n] = {layer: round(v, 6) for layer, v in by.items()}
        return {
            "board": self.path.name,
            "copper_layers": self.copper_layers,
            "thickness_mm": self.thickness,
            "stackup": self.stackup.as_dict(),
            "counts": {"tracks": len(self._tracks), "vias": len(self._vias),
                       "pads": len(self._pads), "zones": len(self._zones),
                       "nets": len(self.nets)},
            "outline_area_mm2": round(self.outline.area, 6),
            "unfilled_zones": [z.zone_id for z in self.unfilled_zones()],
            "rule_areas": [{"name": ra["name"], "layers": list(ra["layers"]),
                            "area_mm2": round(ra["outline"].area, 6)}
                           for ra in self.rule_areas],
            "net_area_mm2": nets,
        }


def patch_region(outline) -> str:
    """A zone outline's bounds as zone_patch --region takes them
    ("x0,y0,x1,y1", rounded out to 0.01 mm), so the region holds it."""
    xs, ys = [p[0] for p in outline], [p[1] for p in outline]
    box = [math.floor(min(xs) * 100) / 100, math.floor(min(ys) * 100) / 100,
           math.ceil(max(xs) * 100) / 100, math.ceil(max(ys) * 100) / 100]
    return ",".join(f"{v:g}" for v in box)


# ============================================================ layer helpers

def _edge_shape_parts(node, tx, closed: list, open_: list, prefix: str) -> None:
    """One Edge.Cuts shape (gr_* or fp_*) as a closed polygon or an open line,
    its points passed through `tx` (the footprint's transform, or identity)."""
    kind = _head(node)[len(prefix):]
    if kind == "rect":
        s, e = _nums(_kid(node, "start")), _nums(_kid(node, "end"))
        closed.append(Polygon([tx(s[0], s[1]), tx(e[0], s[1]),
                               tx(e[0], e[1]), tx(s[0], e[1])]))
    elif kind == "poly":
        pts = [tx(*p) for p in _pts(_kid(node, "pts") or node)]
        if len(pts) >= 3:
            closed.append(Polygon(pts))
    elif kind == "circle":
        c, e = _nums(_kid(node, "center")), _nums(_kid(node, "end"))
        cx, cy = tx(c[0], c[1])
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        closed.append(Point(cx, cy).buffer(r, quad_segs=_QUAD_SEGS * 2))
    elif kind == "line":
        s, e = _nums(_kid(node, "start")), _nums(_kid(node, "end"))
        open_.append(LineString([tx(s[0], s[1]), tx(e[0], e[1])]))
    elif kind == "arc":
        s, m, e = _kid(node, "start"), _kid(node, "mid"), _kid(node, "end")
        if s and m and e:
            open_.append(LineString([tx(*p) for p in _arc_points(
                tuple(_nums(s)[:2]), tuple(_nums(m)[:2]), tuple(_nums(e)[:2]))]))


def _edge_faces(root) -> list[Polygon]:
    """Every closed face Edge.Cuts draws, largest first and with no interior
    rings: board-level shapes of every kind and footprint-level ones (a milled
    slot in a footprint). `.outline` is only the largest board-level face and
    ignores footprints, so it can't answer "where are the slots"."""
    closed: list = []
    open_: list = []
    shapes = ("rect", "poly", "circle", "line", "arc")
    for kind in shapes:
        for g in _kids(root, f"gr_{kind}"):
            if _layer_name(g) == "Edge.Cuts":
                _edge_shape_parts(g, lambda x, y: (x, y), closed, open_, "gr_")
    for fp in _kids(root, "footprint"):
        at = _kid(fp, "at")
        fnums = _nums(at) if at is not None else [0, 0, 0]
        fx, fy = fnums[0], fnums[1]
        fa = fnums[2] if len(fnums) > 2 else 0.0

        def tx(x, y, fx=fx, fy=fy, fa=fa):
            dx, dy = _rot(x, y, -fa)
            return (fx + dx, fy + dy)
        for kind in shapes:
            for g in _kids(fp, f"fp_{kind}"):
                if _layer_name(g) == "Edge.Cuts":
                    _edge_shape_parts(g, tx, closed, open_, "fp_")
    faces = [Polygon(f.exterior) for f in closed if f.is_valid and f.area > 0]
    if open_:
        faces += [Polygon(f.exterior) for f in polygonize(unary_union(open_))]
    return sorted(faces, key=lambda g: g.area, reverse=True)


def _layer_name(node) -> Optional[str]:
    l = _kid(node, "layer")
    return _strs(l)[0] if l is not None and _strs(l) else None


# ============================================================ refill (S0 path)

def _refill_copy(pcb: Path) -> Path:
    """Copy the board to a temp dir and refill zones via kicad-cli. Returns the
    refilled copy's path. Reuses env.py (pin) + kc.run_drc (S0-verified flags)."""
    import shutil
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parent))          # lib/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # scripts/
    import env  # type: ignore
    import kc   # type: ignore
    try:
        cli = env.find_kicad_cli()
    except env.EnvError as exc:                 # a pin that points at nothing
        raise KicadCliMissing(f"kicad-cli not found for the refill: {exc}") \
            from exc
    if cli is None:
        raise KicadCliMissing(
            "kicad-cli not found for the refill (set HWDE_KICAD_CLI or "
            "HWDE_KICAD_ROOT, or put kicad-cli on PATH)")
    tmp = Path(tempfile.mkdtemp(prefix="geom_refill_"))
    dst = tmp / pcb.name
    shutil.copy2(pcb, dst)
    # copy the sibling .kicad_pro and .kicad_dru if present (keeps DRC rules
    # identical: a custom rule can set a zone's clearance below its own)
    for ext in (".kicad_pro", ".kicad_dru"):
        side = pcb.with_suffix(ext)
        if side.exists():
            shutil.copy2(side, dst.with_suffix(ext))
    kc.run_drc(cli, dst, refill=True, save_board=True)
    return dst


def _cleanup(pcb: Path) -> None:
    import shutil
    try:
        shutil.rmtree(pcb.parent, ignore_errors=True)
    except OSError:
        pass


# ============================================================ module cache

_CACHE: dict[str, tuple[tuple, BoardGeom]] = {}


def fresh_board(path) -> "tuple[BoardGeom, Optional[dict]]":
    """The board to judge, and a note on its zone fill (None when the
    committed fill is usable). Like BoardGeom.assert_fresh, but never raises
    on unfilled zones: when kicad-cli can refill them, the geometry of a
    refilled scratch copy is returned (the board file is not touched); when
    it can't, the board as committed, with the reason. Either way the note
    names the zones: {"refilled": bool, "zones": [{"id", "net", "layers"}],
    "why": str|None}."""
    bg = load_board(path)
    unfilled = [z for z in bg.unfilled_zones()
                if z.zone_id not in bg._empty_by_refill()]
    if not unfilled:
        return bg, None
    note = {"zones": [{"id": z.zone_id, "net": z.net, "layers": list(z.layers)}
                      for z in unfilled]}
    if bg._refilled is not None:
        note.update(refilled=True, why=None)
        return bg._refilled, note
    note.update(refilled=False, why=bg._refill_why or
                "kicad-cli gave no refilled copy")
    return bg, note


def fill_note_text(note: dict) -> str:
    """One line for a report's not_judged / notes on a fresh_board() note."""
    zs = ", ".join(f"{z['id']} ({z['net'] or 'no net'} on "
                   f"{'/'.join(z['layers']) or '?'})" for z in note["zones"])
    if note["refilled"]:
        return (f"zone(s) {zs} are unfilled in the committed board: judged on "
                "a kicad-cli refill of a scratch copy (refill and save the "
                "board in KiCad)")
    return (f"zone(s) {zs} are unfilled and could not be refilled "
            f"({note['why']}): judged with them empty, so copper on their "
            "nets is undercounted and findings there are only warnings")


def _canonical_copper_names(root, layers_node) -> dict[str, str]:
    """Rename user-named copper layers (KiCad <= 5 files, where items name a
    layer by its user name) to KiCad's canonical names, in place, by ordinal:
    0 F.Cu, 31 B.Cu, 1..30 In<n>.Cu. Returns {user name: canonical}."""
    aliases: dict[str, str] = {}
    for entry in layers_node[1:]:
        if not _is_node(entry) or not isinstance(entry[0], (int, float)):
            continue
        strs = _strs(entry)
        if len(strs) < 2 or strs[0].endswith(".Cu"):
            continue
        if _tok(strs[1]) not in ("signal", "power", "mixed", "jumper"):
            continue
        n = int(entry[0])
        aliases[str(strs[0])] = ("F.Cu" if n == 0 else "B.Cu" if n == 31
                                 else f"In{n}.Cu" if 0 < n < 31 else str(strs[0]))
    aliases = {k: v for k, v in aliases.items() if k != v}
    if not aliases:
        return aliases
    for entry in layers_node[1:]:
        if _is_node(entry) and len(entry) > 1 and str(entry[1]) in aliases:
            entry[1] = aliases[str(entry[1])]

    def walk(node):
        for i, c in enumerate(node):
            if _is_node(c):
                if _head(c) in ("layer", "layers"):
                    for j in range(1, len(c)):
                        if isinstance(c[j], str) and str(c[j]) in aliases:
                            c[j] = aliases[str(c[j])]
                walk(c)
    walk(root)
    return aliases


def load_board(path, *, refresh: bool = False) -> BoardGeom:
    """Parse a board, caching by resolved path + (mtime, size). Repeated calls in
    one process return the same BoardGeom (checks reuse the built geometry)."""
    path = Path(path)
    key = str(path.resolve())
    st = path.stat()
    sig = (st.st_mtime_ns, st.st_size)
    cached = _CACHE.get(key)
    if cached and cached[0] == sig and not refresh:
        return cached[1]
    bg = BoardGeom.from_file(path)
    _CACHE[key] = (sig, bg)
    return bg


# ============================================================ CLI

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Dump net-indexed geometry summary "
                                 "of a .kicad_pcb as JSON.")
    ap.add_argument("--pcb", required=True, help="path to .kicad_pcb")
    ap.add_argument("--net", help="restrict net_area output to this net")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    ap.add_argument("--check-fill", action="store_true",
                    help="exit 1 if any zone is unfilled")
    args = ap.parse_args(argv)
    try:
        bg = BoardGeom.from_file(Path(args.pcb))
        out = bg.summary()
        if args.net:
            out["net_area_mm2"] = {args.net: out["net_area_mm2"].get(args.net, {})}
    except Exception as exc:  # noqa: BLE001  (contract: any error -> exit 2)
        print(json.dumps({"script": "geom", "status": "error", "error": str(exc)}))
        return 2
    stale = bool(args.check_fill and out["unfilled_zones"])
    payload = {"script": "geom",
               "status": "violations" if stale else "pass", **out}
    text = json.dumps(payload, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
