"""teardroplib - teardrops on vias and plated-hole pads, and the annular-ring
breakout rule they relieve (#480). venv side, pure (sexpdata + shapely).

KiCad 8+ keeps teardrops as a per-item setting: a via or pad carries
`(teardrops ... (enabled yes))`, and every zone refill (pcbnew, and
`kicad-cli pcb drc --refill-zones` - checked on 10.0.6) regenerates one
teardrop zone per track joining that item, `(zone ... (attr (teardrop (type
padvia))))`, on the track's layer. So the board stores the INTENT on the item
and the copper as zones; scripts/route_teardrops.py sets the intent (SWIG,
lib/teardrop_swig.py) and refills.

Terms:
  item   a via, or a pad with a drill and copper (thru_hole); SMD pads only
         when the scope asks (off by default; never under a fine-pitch part)
  join   a track KiCad 10.0.6 makes a drop for (TEARDROP_MANAGER::
         UpdateTeardrops, read from its source and probed): one end in the
         item's copper on that layer, where the track's centreline, run on,
         crosses at least the track's width of copper (a shorter chord is a
         graze: no drop), or neither end in it and the track over the
         item's centre (a track through it). A stub wholly inside, or a
         track crossing the copper off the centre with no end in it, gets
         none. A track through a via got three drops in 10.0.6 (both ways,
         one twice); it counts as one join. The track must be narrower than
         max_width_mm, width_ratio x and filter_ratio x the item's smaller
         copper size, from the item's own teardrop settings when it has
         them on, else from the constraints (10.0.6, a 0.6 mm via at
         filter_ratio 0.9: 0.54 mm got none, 0.53 mm one; max_width 0.3 and
         width_ratio 0.5 stopped a 0.35 mm track). Not modelled: KiCad
         skips a pad (not a via) sharing a pour with the track when its
         teardrops say no drops on pads in zones
  held   a join on a layer where same-net zone fill already touches the
         item: the pour holds the ring, so the ring rule and
         route_teardrops leave it be. KiCad 10.0.6 makes the drop there
         all the same (a via and a thermal PTH pad in their own net's
         pour, prefer_zone_connections yes or no), so a check that a drop
         stays (part_repair, #1138) counts those layers too:
         missing(in_pours=True)
  drop   a teardrop zone (same net, same layer) touching the item; it
         answers one join, the one whose track, or a track going on from
         that track's outer end (two_tracks lets a drop run onto it), runs
         within its width / 2 + BAND_MM of the drop's tip (the drop's
         farthest corner outside the item's copper). A join no drop answers
         is missing, so a track's lost drop shows even when a track through
         the item left extra ones

Ring breakout (dfm_check family `teardrop`): the fab drills within
+/- drill_wander_mm of the pad centre (JLC 0.075). A ring under
min_ring_abs_mm + drill_wander_mm (JLC 0.05 + 0.075 = 0.125) can lose its
copper at the track neck; IPC-2221 / IPC-6012 class 2 accept that breakout
only where the land is filleted. Such an item with an unheld join lacking a
drop is `dfm_ring_breakout`. On a board where teardrops are REQUIRED (rigid-
flex: a rule area named FLEX_*, or constraints `teardrops.required`), any
item with an unheld join lacking a drop is `dfm_teardrop_missing` (error) and
a breakout is an error too; elsewhere breakout is a warning.

constraints.json:
  "teardrops": {"enabled": true,        # route_teardrops runs (default off,
                                        #  on when required)
                "required": null,       # true/false; null = flex boards
                "vias": true, "pth": true, "smd": false,
                "fine_pitch_mm": 0.65,  # SMD pads of a part this tight: none
                "nets": ["*"], "exclude_nets": [],
                "length_ratio": 0.5, "max_length_mm": 1.0,
                "width_ratio": 1.0, "max_width_mm": 2.0,
                "filter_ratio": 0.9, "curved": false}
"""
from __future__ import annotations

import fnmatch
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import sexpdata
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

DEFAULTS = {
    "enabled": False, "required": None,
    "vias": True, "pth": True, "smd": False,
    "fine_pitch_mm": 0.65,
    "nets": ["*"], "exclude_nets": [],
    "length_ratio": 0.5, "max_length_mm": 1.0,
    "width_ratio": 1.0, "max_width_mm": 2.0,
    "filter_ratio": 0.9, "curved": False,
}
FLEX_AREA_GLOB = "FLEX_*"     # flex-link's rule areas (FLEX_TAIL, ...)
DRILL_WANDER_MM = 0.075       # JLC drill position tolerance (#480)
MIN_RING_ABS_MM = 0.05        # JLC "via diameter >= hole + 0.1" (abs ring)
TOL = 0.005                   # mm: "end inside copper" slack
BAND_MM = 0.02                # mm past a track's half width a drop's tip may lie
END_MM = 0.01                 # mm: two track ends this close join (KiCad's
                              #  teardrop tolerance)
MAX_ERROR_MM = 0.005          # KiCad's default arc-to-polygon error


def _tok(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x


def _kid(node, name):
    for c in node[1:]:
        if isinstance(c, list) and c and _tok(c[0]) == name:
            return c
    return None


def _kids(node, name):
    return [c for c in node[1:]
            if isinstance(c, list) and c and _tok(c[0]) == name]


def _nums(node):
    return [float(x) for x in node[1:]
            if isinstance(x, (int, float)) and not isinstance(x, bool)]


def _strs(node):
    return [str(_tok(x)) for x in node[1:]
            if isinstance(x, (str, sexpdata.Symbol))]


# ------------------------------------------------------------- item params

# What one via or pad carries in its (teardrops ...) block, under the names
# the constraints block uses; route_edit's add_via "teardrops" takes these
# (#1138). two_tracks / prefer_zones are KiCad's allow_two_segments /
# prefer_zone_connections, both on for a new item (checked on 10.0.6).
ITEM_PARAMS = dict({k: DEFAULTS[k] for k in (
    "length_ratio", "max_length_mm", "width_ratio", "max_width_mm",
    "filter_ratio", "curved")}, two_tracks=True, prefer_zones=True)
_FILE_KEYS = {
    "best_length_ratio": "length_ratio", "max_length": "max_length_mm",
    "best_width_ratio": "width_ratio", "max_width": "max_width_mm",
    "filter_ratio": "filter_ratio", "curved_edges": "curved",
    "allow_two_segments": "two_tracks",
    "prefer_zone_connections": "prefer_zones",
}


def params_of(node) -> dict | None:
    """A via or pad node's teardrop block as ITEM_PARAMS names plus
    "enabled"; None when it has no block (KiCad writes one only for an item
    with teardrops enabled, so None means off, at the defaults)."""
    td = _kid(node, "teardrops")
    if td is None:
        return None
    out = {"enabled": False}
    for c in td[1:]:
        if not isinstance(c, list) or not c:
            continue
        key = _tok(c[0])
        if key == "enabled":
            out["enabled"] = _strs(c)[:1] == ["yes"]
        elif key in _FILE_KEYS:
            name = _FILE_KEYS[key]
            if isinstance(ITEM_PARAMS[name], bool):
                out[name] = _strs(c)[:1] == ["yes"]
            elif _nums(c):
                out[name] = _nums(c)[0]
    return out


def via_params(root) -> dict:
    """Every via's uuid -> params_of (None: no teardrop block)."""
    out = {}
    for v in _kids(root, "via"):
        u = _kid(v, "uuid")
        if u is not None and _strs(u):
            out[_strs(u)[0]] = params_of(v)
    return out


def same_params(want: dict, got: dict | None, tol: float = 1e-4) -> bool:
    """True when got (params_of) is enabled and has every value in want (a
    key the block lacks reads as ITEM_PARAMS)."""
    if not got or not got.get("enabled"):
        return False
    for k, w in want.items():
        if k == "enabled":
            continue
        g = got.get(k, ITEM_PARAMS.get(k))
        if isinstance(w, bool) or isinstance(g, bool):
            if bool(w) != bool(g):
                return False
        elif g is None or abs(float(w) - float(g)) > tol:
            return False
    return True


# ------------------------------------------------------------- config

def config(constraints: dict | None) -> dict:
    """The constraints' `teardrops` block over DEFAULTS (unknown keys are an
    error: a misspelt key would silently do nothing)."""
    cfg = dict(DEFAULTS)
    blk = (constraints or {}).get("teardrops") or {}
    if not isinstance(blk, dict):
        raise ValueError("constraints teardrops must be an object")
    bad = sorted(k for k in blk if k not in DEFAULTS and not k.startswith("_"))
    if bad:
        raise ValueError(f"constraints teardrops: unknown keys {bad}")
    cfg.update({k: v for k, v in blk.items() if not k.startswith("_")})
    return cfg


def load_constraints(path) -> dict | None:
    if not path:
        return None
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def flex_areas(root) -> list[str]:
    """Names of zones / rule areas marking flex (FLEX_*)."""
    out = []
    for z in _kids(root, "zone"):
        nm = _kid(z, "name")
        name = _strs(nm)[0] if nm is not None and _strs(nm) else ""
        if fnmatch.fnmatchcase(name, FLEX_AREA_GLOB):
            out.append(name)
    return sorted(set(out))


def required(cfg: dict, flex: list[str]) -> bool:
    if cfg.get("required") is not None:
        return bool(cfg["required"])
    return bool(flex)


def net_ok(cfg: dict, net: str | None) -> bool:
    if not net:
        return False
    if any(fnmatch.fnmatchcase(net, g) for g in cfg.get("exclude_nets") or []):
        return False
    return any(fnmatch.fnmatchcase(net, g) for g in cfg.get("nets") or ["*"])


# ------------------------------------------------------------- the board

@dataclass
class Item:
    kind: str                     # via | pth | smd
    net: str
    at: tuple[float, float]
    poly: object                  # copper outline (same on every layer)
    layers: tuple[str, ...]
    ring_mm: float | None         # (smaller copper size - drill) / 2
    min_size_mm: float
    enabled: bool                 # (teardrops (enabled yes)) on the item
    uuid: str | None = None       # vias
    params: dict | None = None    # its own settings when enabled (params_of)
    ref: str | None = None        # pads
    number: str | None = None
    joins: dict = field(default_factory=dict)   # layer -> n eligible tracks
    held: set = field(default_factory=set)      # layers with same-net fill
    drops: dict = field(default_factory=dict)   # layer -> n teardrop zones
    # set by read(): the width limits it is judged by (_limits), every
    # track KiCad would drop on at some width, layer -> [(width, band)]
    # (band: (centreline, half width + BAND_MM) of the track and of each
    # track going on from its outer end), and its drops' tips, layer ->
    # [Point]
    lim: tuple | None = None
    cands: dict = field(default_factory=dict)
    tips: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        if self.kind == "via":
            return f"via@{self.at[0]:.3f},{self.at[1]:.3f}"
        return f"{self.ref}.{self.number}"

    def deficit(self, layer: str, size: float | None = None,
                lim=None) -> int:
        """Joins on layer that no drop answers (see drop in the module
        docstring). size and lim judge the tracks' widths at another copper
        size and limits than the item's own (part_repair: a new via at the
        size of the via it replaced, #1138). An item read() did not fill
        compares its counts."""
        if layer not in self.cands:
            return max(0, self.joins.get(layer, 0) - self.drops.get(layer, 0))
        s = self.min_size_mm if size is None else size
        lm = lim or self.lim or _limits(ITEM_PARAMS)
        bands = [b for w, b in self.cands[layer] if not too_wide(w, s, lm)]
        return len(bands) - _matched(bands, self.tips.get(layer, []))

    def missing(self, *, in_pours: bool = False) -> list[str]:
        """Layers with a join no drop answers (deficit), less the held
        ones unless in_pours (KiCad makes the drop in a pour too; see
        held in the module docstring)."""
        return sorted(l for l in self.joins
                      if (in_pours or l not in self.held)
                      and self.deficit(l) > 0)

    def wanted(self) -> int:
        """Drops route_teardrops asks for: the unheld joins."""
        return sum(n for l, n in self.joins.items() if l not in self.held)


@dataclass
class Board:
    items: list[Item]
    drop_zones: list[tuple[str, str, object]]   # (net, layer, outline)
    flex: list[str]

    @property
    def drop_count(self) -> int:
        return len(self.drop_zones)


def pad_params(root) -> dict:
    """(ref, number, x, y rounded) -> params_of the pad, from the raw file
    (geom.Pad has no teardrop field)."""
    import geom
    out = {}
    for fp in _kids(root, "footprint"):
        at = _kid(fp, "at")
        fn = _nums(at) if at is not None else [0, 0]
        fx, fy = fn[0], fn[1]
        fa = fn[2] if len(fn) > 2 else 0.0
        ref = "?"
        for prop in _kids(fp, "property"):
            pv = _strs(prop)
            if len(pv) >= 2 and pv[0] == "Reference":
                ref = pv[1]
        for pad in _kids(fp, "pad"):
            pat = _kid(pad, "at")
            if pat is None:
                continue
            lx, ly = _nums(pat)[:2]
            dx, dy = geom._rot(lx, ly, -fa)
            key = (ref, str(pad[1]), round(fx + dx, 3), round(fy + dy, 3))
            out[key] = params_of(pad)
    return out


_LIMITS = ("max_width_mm", "width_ratio", "filter_ratio")


def _limits(p: dict) -> tuple[float, float, float]:
    """max_width_mm, width_ratio, filter_ratio from p (params_of or a
    config); a value missing, negative or not a number reads as
    ITEM_PARAMS. Zero stands: KiCad makes no drop under it."""
    out = []
    for k in _LIMITS:
        v = p.get(k)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) \
            and math.isfinite(v) and v >= 0
        out.append(float(v) if ok else float(ITEM_PARAMS[k]))
    return tuple(out)


def _in_band(tip, band) -> bool:
    return any(line.distance(tip) <= half for line, half in band)


def _matched(bands: list, tips: list) -> int:
    """The most joins (bands) that distinct drops (tips) answer: a
    matching, by augmenting paths (a few of each per item and layer)."""
    owner: dict[int, int] = {}          # tip -> band

    def place(b, seen):
        for j, tip in enumerate(tips):
            if j in seen or not _in_band(tip, bands[b]):
                continue
            seen.add(j)
            if j not in owner or place(owner[j], seen):
                owner[j] = b
                return True
        return False

    return sum(1 for b in range(len(bands)) if place(b, set()))


def _kicad_circle(at, r: float, max_err: float = MAX_ERROR_MM) -> Polygon:
    """A round item's copper as KiCad 10.0.6 polygonises it for the graze
    test (TransformCircleToPolygon, ERROR_INSIDE, at least 16 sides,
    rounded up to a multiple of 8, corners on the circle from angle 0)."""
    rel = min(max_err / r, 1.0) if r > 0 else 1.0
    inc = min(360.0 / 8, math.degrees(math.acos(1.0 - rel)) * 2)
    n = max(2, int(360.0 / inc + 0.5)) if inc > 0 else 16
    n = (max(16, n) + 7) // 8 * 8
    return Polygon([(at[0] + r * math.cos(2 * math.pi * k / n),
                     at[1] + r * math.sin(2 * math.pi * k / n))
                    for k in range(n)])


def _coords(g) -> list:
    """Every point of a shapely intersection result."""
    if g.is_empty:
        return []
    if hasattr(g, "geoms"):
        return [c for p in g.geoms for c in _coords(p)]
    return list(g.coords)


def _chord(coords, shape, inside) -> float:
    """KiCad 10.0.6's computeChordThroughShape: the copper the track's
    centreline (run on both ways) crosses in the span holding its inside
    end; inf (KiCad keeps the drop) for an arc, a zero-length track or a
    span it cannot find."""
    if len(coords) != 2:
        return math.inf
    (x0, y0), (x1, y1) = coords[0][:2], coords[1][:2]
    ln = math.hypot(x1 - x0, y1 - y0)
    if ln == 0:
        return math.inf
    dx, dy = (x1 - x0) / ln, (y1 - y0) / ln
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    bx0, by0, bx1, by1 = shape.bounds
    reach = math.hypot(bx1 - bx0, by1 - by0) + ln
    sx, sy = mx - dx * reach, my - dy * reach
    line = LineString([(sx, sy), (mx + dx * reach, my + dy * reach)])
    proj = sorted({round((px - sx) * dx + (py - sy) * dy, 9)
                   for px, py in _coords(line.intersection(shape.boundary))})
    if len(proj) < 2:
        return math.inf
    ins = (inside[0] - sx) * dx + (inside[1] - sy) * dy
    for a, b in zip(proj, proj[1:]):
        m = (a + b) / 2
        # TOL: read() finds the inside end in the copper grown by TOL
        if shape.contains(Point(sx + dx * m, sy + dy * m)) \
                and a - TOL <= ins <= b + TOL:
            return b - a
    return math.inf


def _join_band(t, at, cu, chord_shape, ends) -> tuple | None:
    """The band a drop for track t on this item lies in (see drop in the
    module docstring), or None when KiCad 10.0.6 makes it none: t wholly
    inside, crossing off the centre with no end inside, or entering on a
    chord shorter than its width (a graze). ends: (x, y) -> the tracks of
    t's net and layer with an end there, keyed by _end_key."""
    a, b = t.shape.coords[0][:2], t.shape.coords[-1][:2]
    ins = [cu.contains(Point(a)), cu.contains(Point(b))]
    if ins[0] and ins[1]:
        return None
    if not ins[0] and not ins[1]:
        if t.shape.distance(Point(at)) > t.width / 2.0:
            return None
        outer = (a, b)
    else:
        inside = a if ins[0] else b
        # KiCad compares whole nm; a chord within 0.1 um of the width
        # counts as a graze, so a tie never asks for a drop
        if _chord(list(t.shape.coords), chord_shape, inside) \
                < t.width + 1e-4:
            return None
        outer = (b if ins[0] else a,)
    band = [(t.shape, t.width / 2.0 + BAND_MM)]
    for e in outer:
        for u in _near_ends(ends, e):
            if u is not t:
                band.append((u.shape, u.width / 2.0 + BAND_MM))
    return tuple(band)


def _end_key(p) -> tuple[int, int]:
    return (math.floor(p[0] / (2 * END_MM)), math.floor(p[1] / (2 * END_MM)))


def _end_index(tracks) -> dict:
    out: dict = {}
    for t in tracks:
        for e in (t.shape.coords[0], t.shape.coords[-1]):
            out.setdefault(_end_key(e), []).append((e[:2], t))
    return out


def _near_ends(ends: dict, p) -> list:
    kx, ky = _end_key(p)
    out = []
    for i in (-1, 0, 1):
        for j in (-1, 0, 1):
            for e, t in ends.get((kx + i, ky + j), ()):
                if math.dist(e, p) <= END_MM + 1e-9 and \
                        all(t is not u for u in out):
                    out.append(t)
    return out


def too_wide(width: float, size: float, limits) -> bool:
    """True when KiCad makes no drop for a track this wide on an item of
    this smaller copper size: at or over max_width_mm, or width_ratio x or
    filter_ratio x the size (limits: _limits). See join in the module
    docstring for the 10.0.6 probe."""
    max_w, wr, fr = limits
    eps = 1e-6
    return width >= max_w - eps or width >= wr * size - eps \
        or width >= fr * size - eps


def _fine_pitch_refs(bg, limit: float) -> set[str]:
    by_ref: dict[str, list] = {}
    for p in bg.pads_of():
        if p.kind == "smd":
            by_ref.setdefault(p.ref, []).append(p.center)
    out = set()
    for ref, cs in by_ref.items():
        best = min((math.dist(a, b) for i, a in enumerate(cs)
                    for b in cs[i + 1:]), default=None)
        if best is not None and best < limit - 1e-9:
            out.add(ref)
    return out


def read(pcb, cfg: dict | None = None, area=None) -> Board:
    """Every candidate item with its joins, held layers and drops.

    area (a shapely geometry, board mm) keeps only the items whose copper
    meets it, and the drops that meet those: a local check (part_repair's
    new vias, #1138) at a fraction of the whole board's cost. An item near
    its edge can then count a drop that belongs to a neighbour left out;
    give the area a margin round the items that matter."""
    import geom
    cfg = cfg or dict(DEFAULTS)
    text = Path(pcb).read_text(encoding="utf-8")
    root = sexpdata.loads(text)
    bg = geom.BoardGeom(Path(pcb), root)
    via_par = via_params(root)
    pad_par = pad_params(root)
    drop_zones = []
    for z in _kids(root, "zone"):
        attr = _kid(z, "attr")
        if attr is None or _kid(attr, "teardrop") is None:
            continue
        net = _kid(z, "net")
        layer = _kid(z, "layer")
        poly = _kid(z, "polygon")
        pts = _kid(poly, "pts") if poly is not None else None
        xy = [tuple(_nums(p)[:2]) for p in _kids(pts, "xy")] if pts else []
        if len(xy) >= 3 and net is not None and layer is not None:
            drop_zones.append((_strs(net)[0] if _strs(net) else "",
                               _strs(layer)[0], Polygon(xy).buffer(0)))

    def own(p):
        """(enabled, its own settings over ITEM_PARAMS or None)"""
        if not p or not p.get("enabled"):
            return False, None
        return True, dict(ITEM_PARAMS, **{k: v for k, v in p.items()
                                          if k in ITEM_PARAMS})

    items: list[Item] = []
    round_r: dict[int, float] = {}      # id(item) -> radius, KiCad's IsRound
    for v in bg.vias_of():
        ring = (v.diameter - v.drill) / 2.0 if v.drill else None
        en, par = own(via_par.get(v.uuid))
        items.append(Item("via", v.net, v.at, v.poly, v.layers, ring,
                          v.diameter, en, uuid=v.uuid, params=par))
        round_r[id(items[-1])] = v.diameter / 2.0
    fine = _fine_pitch_refs(bg, float(cfg.get("fine_pitch_mm") or 0))
    for p in bg.pads_of():
        if p.kind == "thru_hole" and p.drill:
            kind = "pth"
            # per pad axis: an oval pad round a slot keeps its ring on
            # both axes (drill and size share the pad frame)
            ring = min(p.size[0] - p.drill[0], p.size[1] - p.drill[1]) / 2.0
        elif p.kind == "smd" and p.ref not in fine:
            kind, ring = "smd", None
        else:
            continue
        key = (p.ref, p.number, round(p.center[0], 3), round(p.center[1], 3))
        en, par = own(pad_par.get(key))
        items.append(Item(kind, p.net or "", p.center, p.poly, p.layers, ring,
                          min(p.size), en, ref=p.ref, number=p.number,
                          params=par))
        if p.shape == "circle" or (p.shape == "oval"
                                   and abs(p.size[0] - p.size[1]) < 1e-9):
            round_r[id(items[-1])] = min(p.size) / 2.0
    if area is not None:
        items = [it for it in items if it.poly.intersects(area)]
        near = unary_union([it.poly for it in items]).buffer(0.05) \
            if items else None
        drop_zones = [d for d in drop_zones
                      if near is not None and d[2].intersects(near)]

    lim_cfg = _limits(cfg)
    by_net_layer: dict = {}     # (net, layer) -> (tracks, _end_index)
    for it in items:
        if not it.net:
            continue
        it.lim = _limits(it.params) if it.params else lim_cfg
        cu = it.poly.buffer(TOL)
        r = round_r.get(id(it))
        chord_shape = _kicad_circle(it.at, r) if r else it.poly
        box = cu.bounds
        for layer in it.layers:
            key = (it.net, layer)
            if key not in by_net_layer:
                ts = bg.tracks_of(net=it.net, layer=layer)
                by_net_layer[key] = (ts, _end_index(ts))
            tracks, ends = by_net_layer[key]
            cands = []
            for t in tracks:
                tb = t.shape.bounds
                if tb[2] < box[0] or tb[0] > box[2] or tb[3] < box[1] \
                        or tb[1] > box[3]:
                    continue
                band = _join_band(t, it.at, cu, chord_shape, ends)
                if band is not None:
                    cands.append((t.width, band))
            if cands:
                it.cands[layer] = cands
            n = sum(1 for w, _ in cands
                    if not too_wide(w, it.min_size_mm, it.lim))
            if n:
                it.joins[layer] = n
            # same-net pour on the item (a teardrop zone is a zone too, but
            # never a pour: it must not mark its own item held)
            for z in bg.zones_of(net=it.net, layer=layer):
                if _is_drop(z, layer, drop_zones):
                    continue
                if any(f.intersects(it.poly) for f in z.fills.get(layer, [])):
                    it.held.add(layer)
                    break
    # each drop to ONE item: of the same-net items on its layer whose copper
    # it touches, the one whose centre is nearest a vertex (KiCad anchors
    # the polygon on the pad/via centre; a drop running along a short track
    # also touches the item at the far end)
    for net, layer, poly in drop_zones:
        verts = list(poly.exterior.coords) if hasattr(poly, "exterior") \
            else [c for g in poly.geoms for c in g.exterior.coords]
        best = None
        for it in items:
            if it.net != net or layer not in it.layers or \
                    not poly.intersects(it.poly.buffer(TOL)):
                continue
            d = min(math.dist(it.at, v) for v in verts)
            if best is None or d < best[0]:
                best = (d, it)
        if best is not None:
            it = best[1]
            it.drops[layer] = it.drops.get(layer, 0) + 1
            cu = it.poly.buffer(TOL)
            out = [v for v in verts if not cu.contains(Point(v))]
            if out:
                tip = max(out, key=lambda v: math.dist(it.at, v))
                it.tips.setdefault(layer, []).append(Point(tip))
    return Board(items, drop_zones, flex_areas(root))


def _is_drop(zone, layer, drop_zones) -> bool:
    """geom.Zone carries no attr: a zone is a teardrop when its fill sits
    inside one of the teardrop outlines on that layer."""
    fills = zone.fills.get(layer, [])
    if not fills:
        return False
    for net, l, poly in drop_zones:
        if net == zone.net and l == layer and \
                all(poly.buffer(0.01).contains(f) for f in fills):
            return True
    return False


def select(board: Board, cfg: dict) -> tuple[list[Item], list[Item]]:
    """(to enable, already enabled) under cfg's scope: items on matching
    nets with at least one unheld join."""
    scope = {"via": cfg.get("vias", True), "pth": cfg.get("pth", True),
             "smd": cfg.get("smd", False)}
    todo, done = [], []
    for it in board.items:
        if not scope.get(it.kind) or not net_ok(cfg, it.net):
            continue
        if not it.wanted():
            continue
        (done if it.enabled else todo).append(it)
    return todo, done


# ------------------------------------------------------------- ring rule

def ring_findings(board: Board, *, req: bool, wander: float = DRILL_WANDER_MM,
                  abs_min: float = MIN_RING_ABS_MM) -> list[dict]:
    """Plain dicts: {kind, severity, item, pos, net, ring_mm, layers, msg}."""
    out = []
    lim = abs_min + wander
    for it in board.items:
        if it.kind == "smd" or not it.net:
            continue
        miss = it.missing()
        breakout = it.ring_mm is not None and it.ring_mm < lim - 1e-9
        if breakout and miss:
            out.append({
                "kind": "dfm_ring_breakout",
                "severity": "error" if req else "warning",
                "item": it.name, "pos": it.at, "net": it.net,
                "ring_mm": round(it.ring_mm, 4), "limit_mm": round(lim, 4),
                "layers": miss,
                "msg": (f"{it.kind} {it.name} ring {it.ring_mm:.3f} mm < "
                        f"{abs_min} + {wander} drill wander: the drill can "
                        f"break out at the track neck on {', '.join(miss)} "
                        f"with no teardrop there (route_teardrops.py)")})
        elif req and miss:
            out.append({
                "kind": "dfm_teardrop_missing", "severity": "error",
                "item": it.name, "pos": it.at, "net": it.net,
                "ring_mm": None if it.ring_mm is None else round(it.ring_mm, 4),
                "limit_mm": None, "layers": miss,
                "msg": (f"{it.kind} {it.name}: teardrops are required on this "
                        f"board (flex) and the track join on "
                        f"{', '.join(miss)} has none (route_teardrops.py)")})
    return out
