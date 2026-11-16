"""teardroplib - teardrops on vias and plated-hole pads, and the annular-ring
breakout rule they relieve (#480). venv side, pure (sexpdata + shapely).

KiCad 8+ keeps teardrops as a per-item setting: a via or pad carries
`(teardrops ... (enabled yes))`, and every zone refill (pcbnew, and
`kicad-cli pcb drc --refill-zones` - checked on 10.0.6) regenerates one
teardrop zone per track joining that item, `(zone ... (attr (teardrop (type
padvia))))`, on the track's layer. So the board stores the INTENT on the item
and the copper as zones; scripts/route_teardrops.py sets the intent (SWIG,
lib/teardrop_swig.py) and refills.

Model: KiCad 10.0.6, the pipeline's kicad-cli and pcbnew. Each rule below
was read from that tag's source and probed on it. The 10.0 branch after
the tag (ae47c29e, 20cbfb7a) makes drops otherwise in three ways not
modelled here: max_width 0 means no limit (10.0.6 makes no drop); a track
through a via gets two drops, one each side (10.0.6 makes up to three,
and this counts one join); and a drop beside another net's copper is
fitted round it or left out (10.0.6 makes it whole). A pipeline moved to
a newer KiCad needs those three ported.

Terms:
  item   a via, or a pad with a drill and copper (thru_hole); SMD pads only
         when the scope asks (off by default; never under a fine-pitch part)
  join   a track KiCad 10.0.6 makes a drop for (TEARDROP_MANAGER::
         UpdateTeardrops, read from its source and probed): one end in the
         item's copper on that layer, where the track's centreline, run on,
         crosses at least the track's width of copper (a shorter chord is a
         graze: no drop), or neither end in it and the track over the
         item's centre (a track through it). A stub wholly inside gets
         none. A track crossing the copper off the centre with no end in
         it gets only a sliver of a drop, on one side inside the copper or
         just out to the track's edge (probed on a 0.6 mm via: 0.15 and
         0.25 mm tracks 0.15-0.27 mm off its centre, either way): no
         fillet, so it is no join and no track into the item. read()
         keeps the grazes and those crossings apart (Item.grazes), so the
         ring rule can name them. A track through a via got three drops
         in 10.0.6 (both ways, one twice); it counts as one join. The
         track must be narrower than max_width_mm, width_ratio x and
         filter_ratio x the item's smaller copper size, from the item's
         own teardrop settings when it has them on, else from the
         constraints (10.0.6, a 0.6 mm via at filter_ratio 0.9: 0.54 mm
         got none, 0.53 mm one; max_width 0.3 and width_ratio 0.5 stopped
         a 0.35 mm track). Not modelled: KiCad
         skips a pad (not a via) sharing a pour with the track when its
         teardrops say no drops on pads in zones
  held   a join on a layer where same-net zone fill already touches the
         item: the pour holds the ring, so the ring rule and
         route_teardrops leave it be. KiCad 10.0.6 makes the drop there
         all the same (a via and a thermal PTH pad in their own net's
         pour, prefer_zone_connections yes or no), so a check that a drop
         stays (part_repair, #1138) counts those layers too:
         missing(in_pours=True)
  drop   a teardrop zone of type padvia (or none: older files), same net
         and layer, touching the item (a track-end drop, type track_end,
         is not the item's); it answers one join, the one whose track, or
         a track going on from that track's outer end (two_tracks lets a
         drop run onto it), runs within its width / 2 + BAND_MM of the
         drop's tip (the drop's farthest corner outside the item's copper).
         A join no drop answers is missing, so a track's lost drop shows
         even when a track through the item left extra ones.
         Item.answered counts the tracks drops answer at any width,
         Item.into the tracks into the item (joins at any width and
         grazes, not crossings) and Item.unanswered names the rest

Ring breakout (dfm_check family `teardrop`): the fab drills within
+/- drill_wander_mm of the pad centre (JLC 0.075). A ring under
min_ring_abs_mm + drill_wander_mm (JLC 0.05 + 0.075 = 0.125) can lose its
copper at the track neck; IPC-2221 / IPC-6012 class 2 accept that breakout
only where the land is filleted. Such an item with an unheld join lacking a
drop, or with an unheld track that grazes its copper (KiCad makes it no
drop) or crosses it off the centre (only a sliver of one), is
`dfm_ring_breakout`: the track must run into the copper. A track too wide
for a drop (too_wide) is neither: by the item's own limits it gets none.
On a board where teardrops are REQUIRED
(rigid-flex: a rule area named FLEX_*, or constraints `teardrops.required`),
any item with such a join or track is `dfm_teardrop_missing` (error) and a
breakout is an error too; elsewhere breakout is a warning.

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


def _enabled(node) -> bool:
    """True when the via or pad node has teardrops on (params_of)."""
    p = params_of(node)
    return bool(p and p.get("enabled"))


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
    drops: dict = field(default_factory=dict)   # layer -> n drops (padvia)
    # set by read(): the width limits it is judged by (_limits), every
    # track KiCad would drop on at some width, layer -> [(width, band)]
    # (band: (centreline, half width + BAND_MM) of the track and of each
    # track going on from its outer end), the tracks into it KiCad drops
    # on at no width, layer -> [(width, "grazes" | "crosses")], and its
    # drops' tips, layer -> [Point]
    lim: tuple | None = None
    cands: dict = field(default_factory=dict)
    grazes: dict = field(default_factory=dict)
    tips: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        if self.kind == "via":
            return f"via@{self.at[0]:.3f},{self.at[1]:.3f}"
        return f"{self.ref}.{self.number}"

    def too_wide(self, width: float) -> bool:
        """too_wide at this item's size and limits."""
        return too_wide(width, self.min_size_mm,
                        self.lim or _limits(ITEM_PARAMS))

    def _answer(self, layer: str) -> tuple[list, set]:
        """(layer's cands, those KiCad drops on first; the indices in it
        a drop answers). Matching those first keeps a drop on the track
        it was made for when a too-wide track runs beside it."""
        cs = sorted(self.cands.get(layer, []),
                    key=lambda c: self.too_wide(c[0]))
        return cs, set(_match([b for _, b in cs], self.tips.get(layer, [])))

    def unanswered(self, layer: str) -> list[tuple[float, str]]:
        """The tracks into the item on layer that no drop answers, as
        (width, why): "no drop" (a join), "too wide" (too_wide), and
        "grazes" (see join in the module docstring). A crossing is not
        into the item (into)."""
        cs, hit = self._answer(layer)
        out = [(w, "too wide" if self.too_wide(w) else "no drop")
               for i, (w, _) in enumerate(cs) if i not in hit]
        return out + [g for g in self.grazes.get(layer, [])
                      if g[1] == "grazes"]

    def answered(self, layer: str) -> int:
        """Tracks on layer, at any width, that a drop answers. An item
        read() did not fill: the lesser of its joins and drops."""
        if layer not in self.cands:
            return min(self.joins.get(layer, 0), self.drops.get(layer, 0))
        return len(self._answer(layer)[1])

    def answered_widths(self, layer: str) -> list[float]:
        """The widths of the tracks on layer a drop answers, smallest
        first; [] for an item read() did not fill."""
        if layer not in self.cands:
            return []
        cs, hit = self._answer(layer)
        return sorted(cs[i][0] for i in hit)

    def into(self, layer: str) -> int:
        """Tracks into the item on layer: its joins at any width and its
        grazes. An off-centre crossing is not: KiCad gives it only a
        sliver of a drop, so a check that drops stay (part_repair) does
        not ask one for it. An item read() did not fill: its joins."""
        if layer not in self.cands and layer not in self.grazes:
            return self.joins.get(layer, 0)
        return len(self.cands.get(layer, [])) + sum(
            1 for _, why in self.grazes.get(layer, []) if why == "grazes")

    def deficit(self, layer: str) -> int:
        """Joins on layer that no drop answers (see drop in the module
        docstring). An item read() did not fill compares its counts."""
        if layer not in self.cands:
            return max(0, self.joins.get(layer, 0) - self.drops.get(layer, 0))
        return sum(1 for _, why in self.unanswered(layer) if why == "no drop")

    def undropped(self) -> list[str]:
        """Unheld layers with a track narrow enough for a drop that grazes
        the copper (KiCad makes it none) or crosses it off the centre (only
        a sliver of one, no fillet): the ring rule asks for the track to
        run into the copper."""
        return sorted(l for l, gs in self.grazes.items()
                      if l not in self.held
                      and any(not self.too_wide(w) for w, _ in gs))

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


def _match(bands: list, tips: list) -> dict[int, int]:
    """The most joins (bands) that distinct drops (tips) answer, band
    index -> tip index: a matching, by augmenting paths (a few of each per
    item and layer). A band matched stays matched as later ones are
    placed, so the bands first in the list go first."""
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

    for b in range(len(bands)):
        place(b, set())
    return {b: j for j, b in owner.items()}


def _matched(bands: list, tips: list) -> int:
    """How many joins (bands) distinct drops (tips) answer (_match)."""
    return len(_match(bands, tips))


def _kicad_circle(at, r: float, max_err: float = MAX_ERROR_MM) -> Polygon:
    """A round item's copper as KiCad 10.0.6 polygonises it for the graze
    test (TransformCircleToPolygon, ERROR_INSIDE: at least 16 sides,
    rounded up to a multiple of 8, corners on the circle half a step off
    angle 0; probed on pcbnew, a 0.6 mm via's at +/-7.5 deg)."""
    rel = min(max_err / r, 1.0) if r > 0 else 1.0
    inc = min(360.0 / 8, math.degrees(math.acos(1.0 - rel)) * 2)
    n = max(2, int(360.0 / inc + 0.5)) if inc > 0 else 16
    n = (max(16, n) + 7) // 8 * 8
    return Polygon([(at[0] + r * math.cos(2 * math.pi * (k + 0.5) / n),
                     at[1] + r * math.sin(2 * math.pi * (k + 0.5) / n))
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


def chord_of(a, b, at, size: float) -> float:
    """The chord a straight track from a (its end in the copper) to b
    crosses on a round item of this size at at, as KiCad 10.0.6 reads it
    for the graze test (_kicad_circle, _chord): a track this wide or wider
    gets no drop there (grazes). inf when there is no chord to read."""
    return _chord([tuple(a[:2]), tuple(b[:2])],
                  _kicad_circle(at, size / 2.0), tuple(a[:2]))


def grazes_on(width: float, chord: float) -> bool:
    """True when KiCad 10.0.6 makes no drop for a track this wide that
    enters the copper on chord (chord_of): it compares whole nm, so a
    chord within 0.1 um of the width counts as a graze, and a tie never
    asks for a drop."""
    return chord < width + 1e-4


def _join_band(t, at, cu, chord_shape, ends) -> tuple:
    """(band, None): the band a drop for track t on this item lies in (see
    drop in the module docstring); or (None, why) when KiCad 10.0.6 makes
    it none, or no fillet: "inside" (t wholly in the copper), "crosses"
    (its centreline crosses the copper off the centre, no end in it: a
    sliver of a drop), "grazes" (it enters on a chord shorter than its
    width), None (its centreline never meets the copper: not into the
    item). ends: (x, y) -> the tracks of t's net and layer with an end
    there, keyed by _end_key."""
    a, b = t.shape.coords[0][:2], t.shape.coords[-1][:2]
    ins = [cu.contains(Point(a)), cu.contains(Point(b))]
    if ins[0] and ins[1]:
        return None, "inside"
    if not ins[0] and not ins[1]:
        if t.shape.distance(Point(at)) > t.width / 2.0:
            return None, ("crosses" if t.shape.intersects(cu) else None)
        outer = (a, b)
    else:
        inside = a if ins[0] else b
        if grazes_on(t.width, _chord(list(t.shape.coords), chord_shape,
                                     inside)):
            return None, "grazes"
        outer = (b if ins[0] else a,)
    band = [(t.shape, t.width / 2.0 + BAND_MM)]
    for e in outer:
        for u in _near_ends(ends, e):
            if u is not t:
                band.append((u.shape, u.width / 2.0 + BAND_MM))
    return tuple(band), None


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


def takes_drop(width: float, size: float, p: dict) -> bool:
    """True when KiCad makes a drop for a track this wide on an item of
    this smaller copper size with teardrop settings p (params_of, or
    add_via's "teardrops": a key left out reads as ITEM_PARAMS)."""
    return not too_wide(width, size, _limits(p))


def drop_length(size: float, p: dict) -> float:
    """mm along a track, from where it leaves the item's copper, that
    KiCad 10.0.6 lays a drop over: length_ratio x the item's size, capped
    by max_length_mm (0 sets no cap). p as for takes_drop."""
    n = []
    for k in ("length_ratio", "max_length_mm"):
        v = p.get(k)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) \
            and math.isfinite(v) and v >= 0
        n.append(float(v) if ok else float(ITEM_PARAMS[k]))
    return min(n[0] * size, n[1]) if n[1] > 0 else n[0] * size


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
    # every teardrop zone (each is copper, never a pour), and those that
    # are an item's drop: type padvia, or none in an older file
    drop_zones, credit = [], []
    for z in _kids(root, "zone"):
        attr = _kid(z, "attr")
        td = _kid(attr, "teardrop") if attr is not None else None
        if td is None:
            continue
        net = _kid(z, "net")
        layer = _kid(z, "layer")
        poly = _kid(z, "polygon")
        pts = _kid(poly, "pts") if poly is not None else None
        xy = [tuple(_nums(p)[:2]) for p in _kids(pts, "xy")] if pts else []
        if len(xy) >= 3 and net is not None and layer is not None:
            drop_zones.append((_strs(net)[0] if _strs(net) else "",
                               _strs(layer)[0], Polygon(xy).buffer(0)))
            ty = _kid(td, "type")
            if ty is None or _strs(ty)[:1] in ([], ["padvia"]):
                credit.append(drop_zones[-1])

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
        credit = [d for d in credit
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
            cands, grazes = [], []
            for t in tracks:
                tb = t.shape.bounds
                if tb[2] < box[0] or tb[0] > box[2] or tb[3] < box[1] \
                        or tb[1] > box[3]:
                    continue
                band, why = _join_band(t, it.at, cu, chord_shape, ends)
                if band is not None:
                    cands.append((t.width, band))
                elif why in ("grazes", "crosses"):
                    grazes.append((t.width, why))
            if cands:
                it.cands[layer] = cands
            if grazes:
                it.grazes[layer] = grazes
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
    # also touches the item at the far end). A track_end drop (KiCad's
    # track-to-track teardrops) is no item's.
    for net, layer, poly in credit:
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
        bare = it.undropped()
        layers = sorted(set(miss) | set(bare))
        why = ""
        if bare:
            why = (f"; on {', '.join(bare)} a track grazes the copper "
                   f"(KiCad gives it no drop) or crosses it off its centre "
                   f"(only a sliver of one): run the track into the copper")
        breakout = it.ring_mm is not None and it.ring_mm < lim - 1e-9
        if breakout and layers:
            out.append({
                "kind": "dfm_ring_breakout",
                "severity": "error" if req else "warning",
                "item": it.name, "pos": it.at, "net": it.net,
                "ring_mm": round(it.ring_mm, 4), "limit_mm": round(lim, 4),
                "layers": layers,
                "msg": (f"{it.kind} {it.name} ring {it.ring_mm:.3f} mm < "
                        f"{abs_min} + {wander} drill wander: the drill can "
                        f"break out at the track neck on {', '.join(layers)} "
                        f"with no teardrop there (route_teardrops.py){why}")})
        elif req and layers:
            out.append({
                "kind": "dfm_teardrop_missing", "severity": "error",
                "item": it.name, "pos": it.at, "net": it.net,
                "ring_mm": None if it.ring_mm is None else round(it.ring_mm, 4),
                "limit_mm": None, "layers": layers,
                "msg": (f"{it.kind} {it.name}: teardrops are required on this "
                        f"board (flex) and the track join on "
                        f"{', '.join(layers)} has none "
                        f"(route_teardrops.py){why}")})
    return out


# ------------------------------------------------------------------ necks
# A join laid at a net's width can end a track too wide for a drop on a
# via with teardrops on (KiCad makes none at or over filter_ratio x its
# size): it narrows from the via out past where the drop ends (#1138,
# part_repair; #1320, route_finish and route_auto too). These work on
# route_edit ops (add_track, add_via with "teardrops"); neck_board turns
# board tracks into such ops and back.

NECK_PAST = 0.05  # mm a neck runs on past where its drop ends (#1138)
NECK_TOL = 1e-3   # mm: an end this close to a point is on it


def _r4(v) -> float:
    return round(float(v), 4)


def neck_widths(size: float, p: dict, widths, wide: float,
                floor: float) -> list:
    """The widths a track into a new via of size with teardrop settings p
    may narrow to so KiCad makes its drop (#1138), best first: widths (the
    removed via's tracks that took a drop), widest first, then wide (the
    net's width), then floor (its narrowest DRC passes); only those at or
    over floor that take a drop here (teardroplib takes_drop), each once.
    Empty when none does."""
    out: list = []
    for w in sorted(set(widths), reverse=True) + [wide, floor]:
        w = round(float(w), 6)
        if w >= floor - 1e-9 and takes_drop(w, size, p) \
                and w not in out:
            out.append(w)
    return out


def _exit_at(a, b, c, r: float) -> float:
    """The fraction along a->b where it leaves the circle (c, r) that a is
    in: the larger root of |a + u(b - a) - c| = r, kept in [0, 1]."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    fx, fy = a[0] - c[0], a[1] - c[1]
    qa = dx * dx + dy * dy
    if qa <= 0:
        return 1.0
    qb = 2 * (fx * dx + fy * dy)
    qc = fx * fx + fy * fy - r * r
    disc = max(0.0, qb * qb - 4 * qa * qc)
    return min(1.0, max(0.0, (-qb + math.sqrt(disc)) / (2 * qa)))


def _pt(e) -> tuple:
    return (float(e[0]), float(e[1]))


def _far_end(t: dict, a) -> tuple:
    """(the end of add_track t that is not a, True when a is its start)."""
    at_start = math.dist(_pt(t["start"]), a) <= NECK_TOL
    return (_pt(t["end"]) if at_start else _pt(t["start"])), at_start


def _chain_next(kept, j, b, seen, shorter, board_at=None):
    """The add_track a walk along kept[j] goes on to from its end b: the
    one other add_track of its net and layer with an end at b (the prune's
    shorter ones counted), when it is not in seen or shorter, no add_via
    of the run stands at b, and no board copper of the net meets b on the
    layer (board_at(net, layer, b)). Else None: the walk stops at a
    branch, a via or a joint with the board."""
    t = kept[j]
    nxt = [k for k, o in enumerate(kept)
           if k != j and o["op"] == "add_track" and o["net"] == t["net"]
           and o["layer"] == t["layer"]
           and any(math.dist(_pt(e), b) <= NECK_TOL
                   for e in (o["start"], o["end"]))]
    if len(nxt) != 1 or nxt[0] in seen or nxt[0] in shorter:
        return None
    if any(o["op"] == "add_via" and math.dist(_pt(o["at"]), b) <= NECK_TOL
           for o in kept):
        return None
    if board_at is not None and board_at(t["net"], t["layer"], b):
        return None
    return nxt[0]


def _leaving(kept, j, a, c, r, shorter, board_at=None):
    """The ends (in the copper, outside it) of the track a drop on the via
    (c, r) lies on, for kept[j] with end a in it: kept[j] when it leaves
    the circle, else the first track the walk from its far end
    (_chain_next) reaches that does, past stubs wholly inside. None when
    the walk stops first."""
    seen: set = set()
    while j is not None and j not in seen:
        seen.add(j)
        b, _ = _far_end(kept[j], a)
        if math.dist(b, c) > r:
            return a, b
        j, a = _chain_next(kept, j, b, seen, shorter, board_at), b
    return None


def neck_tracks(kept: list, td_src: list, shorter, width_of,
                board_at=None) -> list:
    """Tracks too wide for a drop on a new via that took teardrops (#1138).
    The join lays a net at the widest width the run ripped from it, so it
    can end a 0.3 mm track on a 0.254 mm via whose drops the removed via
    had on 0.2 mm ones, and KiCad makes none for it (teardroplib
    takes_drop). Each add_track in kept with an end in such a via's copper
    (td_src not None), too wide for its drop, narrows from that end to
    NECK_PAST past where the drop ends (drop_length past the via's edge),
    walking on through the one add_track of its net and layer that goes on
    from its far end (_chain_next); the rest keeps its width, a new
    add_track appended to kept with None appended to td_src. width_of(i,
    p): the widths the via at kept[i] with settings p may take
    (neck_widths), best first; the neck takes the first one the track
    does not graze the via at (teardroplib chord_of, grazes_on, read on
    the track that leaves the copper, _leaving). With none, or no track
    that leaves, the track stays and the gate names the drop it lost; a
    track narrow enough for a drop that grazes stays too. Ops in shorter
    (cut back by the prune) stay as they are. board_at(net, layer, pt):
    True where board copper of the net meets pt, which ends a walk.
    Returns one entry per via, net, layer, width and was: via (its "at"),
    net, layer, width, was, length (mm narrowed) and ops (the kept
    indices narrowed)."""
    out: dict = {}
    for i in range(len(kept)):
        v = kept[i]
        if v["op"] != "add_via" or td_src[i] is None \
                or v.get("teardrops") is None:
            continue
        p = dict(ITEM_PARAMS, **v["teardrops"])
        size, c = float(v["size"]), _pt(v["at"])
        r = size / 2
        ws = None
        for j in range(len(kept)):
            t = kept[j]
            if t["op"] != "add_track" or t["net"] != v["net"] \
                    or j in shorter \
                    or takes_drop(float(t["width"]), size, p):
                continue
            near = [e for e in (_pt(t["start"]), _pt(t["end"]))
                    if math.dist(e, c) <= r + NECK_TOL]
            if not near:
                continue
            if ws is None:
                ws = list(width_of(i, p) or [])
            if not ws:
                break
            a = min(near, key=lambda e: math.dist(e, c))
            leave = _leaving(kept, j, a, c, r, shorter, board_at)
            if leave is None:
                continue
            chord = chord_of(leave[0], leave[1], c, size)
            w = next((x for x in ws if not grazes_on(x, chord)), None)
            if w is None:
                continue
            was = float(t["width"])
            laid, ops = _neck_walk(kept, td_src, j, a, c, r,
                                   drop_length(size, p) + NECK_PAST, w,
                                   shorter, board_at)
            if not ops:
                continue
            key = (_r4(c[0]), _r4(c[1]), v["net"], t["layer"], w, was)
            e = out.setdefault(key, {
                "via": [_r4(c[0]), _r4(c[1])], "net": v["net"],
                "layer": t["layer"], "width": w, "was": was,
                "length": 0.0, "ops": []})
            e["length"] = _r4(e["length"] + laid)
            e["ops"] += ops
    return list(out.values())


def _neck_walk(kept, td_src, j, a, c, r, reach, w, shorter,
               board_at=None) -> tuple:
    """Narrow kept[j] (an add_track with end a in the via's copper (c, r))
    to w from a out to reach mm past where the walk leaves the copper,
    then on through the one add_track of its net and layer that goes on
    from its far end (_chain_next); a track that ends past that point is
    split there, its far piece appended to kept (td_src gets None). A
    split point that rounds onto the track's far end narrows it whole,
    one that rounds onto a narrows none of it. Returns (the mm of track
    narrowed, the kept indices narrowed)."""
    left = None      # mm still to narrow past the via's edge; None inside
    done = 0.0
    ops: list = []
    seen: set = set()
    while j is not None and j not in seen:
        seen.add(j)
        t = kept[j]
        if float(t["width"]) <= w + 1e-9:
            break
        b, at_start = _far_end(t, a)
        n = math.dist(a, b)
        u0 = 0.0
        if left is None:
            if math.dist(b, c) <= r:
                u0 = None          # wholly inside: narrow it all
            else:
                u0 = _exit_at(a, b, c, r)
                left = reach
        if u0 is not None and n * (1 - u0) > left + 1e-9:
            u = u0 + left / n
            pt = [_r4(a[0] + (b[0] - a[0]) * u), _r4(a[1] + (b[1] - a[1]) * u)]
            if pt == [_r4(a[0]), _r4(a[1])]:
                break              # nothing of it to narrow
            if pt != [_r4(b[0]), _r4(b[1])]:
                if at_start:
                    kept[j] = dict(t, end=pt, width=w)
                    kept.append(dict(t, start=pt))
                else:
                    kept[j] = dict(t, start=pt, width=w)
                    kept.append(dict(t, end=pt))
                td_src.append(None)
                ops.append(j)
                return done + math.dist(a, pt), ops
            kept[j] = dict(t, width=w)   # the split is its far end
            ops.append(j)
            return done + n, ops
        kept[j] = dict(t, width=w)
        ops.append(j)
        done += n
        if u0 is not None:
            left -= n * (1 - u0)
        j, a = _chain_next(kept, j, b, seen, shorter, board_at), b
    return done, ops


def neck_lengths(necks: list, kept: list, gone) -> list:
    """necks (neck_tracks) as the re-prune leaves them (#1138): each
    length read again from its ops in kept that are not in gone (the
    indices the prune took off or the board already had), an entry with
    none of them left dropped, and the ops left out."""
    out = []
    for e in necks:
        live = [k for k in e["ops"] if k not in gone]
        if not live:
            continue
        n = sum(math.dist(_pt(kept[k]["start"]), _pt(kept[k]["end"]))
                for k in live)
        out.append({k: v for k, v in e.items() if k != "ops"}
                   | {"length": _r4(n)})
    return out


def drop_vias(bg, params: dict) -> list:
    """The vias of BoardGeom bg with teardrops on (params: via_params of
    its file), each as an add_via op with "teardrops" (its params) and
    "uuid": what neck_tracks reads a via a drop must stay on from."""
    out = []
    for v in bg.vias_of():
        p = params.get(v.uuid) if v.uuid else None
        if not p or not p.get("enabled"):
            continue
        out.append({"op": "add_via", "at": [float(v.at[0]), float(v.at[1])],
                    "size": float(v.diameter), "drill": float(v.drill),
                    "net": v.net, "uuid": v.uuid,
                    "teardrops": {k: x for k, x in p.items()
                                  if k != "enabled"}})
    return out


def neck_ops(ops: list, vias: list, width_of, board_at=None) -> list:
    """Neck a route's own ops (#1320): the add_tracks in ops that end too
    wide for a drop on a via of the board with teardrops on (vias:
    drop_vias), and on an add_via in ops that carries "teardrops", narrow
    as neck_tracks has it. ops is changed in place (a split appends its
    far piece); width_of(via op, p) gives the widths, best first
    (neck_widths). Returns neck_tracks' entries without their ops."""
    vs = [v for v in vias
          if any(o["op"] == "add_track" and o["net"] == v["net"]
                 for o in ops)]
    kept = [dict(v) for v in vs] + ops
    src = ["board"] * len(vs) + [
        "own" if o["op"] == "add_via" and o.get("teardrops") is not None
        else None for o in ops]
    n = len(vs)
    necks = neck_tracks(kept, src, {}, lambda i, p: width_of(kept[i], p),
                        board_at)
    ops[:] = kept[n:]
    return [{k: x for k, x in e.items() if k != "ops"} for e in necks]


def neck_board(tracks: list, vias: list, width_of,
               board_at=None) -> tuple[list, list]:
    """neck_ops for tracks already on a board (#1320: route_auto's, whose
    vias Freerouting replaced and the run turned teardrops back on for):
    tracks are geom Tracks with a uuid, vias drop_vias. A track that
    narrows is removed and laid again as add_tracks: the narrowed piece
    and, when it splits, the rest at its width. Returns (route_edit ops,
    neck entries)."""
    ops = [{"op": "add_track", "start": list(t.shape.coords[0]),
            "end": list(t.shape.coords[-1]), "width": float(t.width),
            "layer": t.layer, "net": t.net} for t in tracks]
    was = [dict(o) for o in ops]
    necks = neck_ops(ops, vias, width_of, board_at)
    if not necks:
        return [], []
    out = []
    for t, a, b in zip(tracks, was, ops):
        if a != b:
            out.append({"op": "remove", "uuid": t.uuid})
            out.append(b)
    out += ops[len(tracks):]
    return out, necks
