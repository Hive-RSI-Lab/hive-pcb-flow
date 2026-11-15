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
  join   a track with exactly one end in the item's copper on that layer
         (a stub wholly inside the pad has no direction to drop along; a
         track running straight through is two joins, one per side), the
         track narrower than filter_ratio x the item's smaller copper size
         (KiCad makes no teardrop for a track as wide as the pad)
  held   a join on a layer where same-net zone fill already touches the
         item: KiCad's default `prefer_zone_connections` makes no drop
         there, and the pour holds the ring
  drop   a teardrop zone (same net, same layer) touching the item

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
from shapely.geometry import Point, Polygon
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


def _enabled(node) -> bool:
    td = _kid(node, "teardrops")
    if td is None:
        return False
    en = _kid(td, "enabled")
    return en is not None and _strs(en)[:1] == ["yes"]


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
    ref: str | None = None        # pads
    number: str | None = None
    joins: dict = field(default_factory=dict)   # layer -> n eligible tracks
    held: set = field(default_factory=set)      # layers with same-net fill
    drops: dict = field(default_factory=dict)   # layer -> n teardrop zones

    @property
    def name(self) -> str:
        if self.kind == "via":
            return f"via@{self.at[0]:.3f},{self.at[1]:.3f}"
        return f"{self.ref}.{self.number}"

    def missing(self) -> list[str]:
        """Layers with an unheld join and fewer drops than joins."""
        return sorted(l for l, n in self.joins.items()
                      if l not in self.held and self.drops.get(l, 0) < n)

    def wanted(self) -> int:
        """Drops KiCad should make: unheld joins."""
        return sum(n for l, n in self.joins.items() if l not in self.held)


@dataclass
class Board:
    items: list[Item]
    drop_zones: list[tuple[str, str, object]]   # (net, layer, outline)
    flex: list[str]

    @property
    def drop_count(self) -> int:
        return len(self.drop_zones)


def _pad_flags(root) -> dict:
    """(ref, number, x, y rounded) -> teardrops enabled, from the raw file
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
            out[key] = _enabled(pad)
    return out


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
    via_en = {}
    for v in _kids(root, "via"):
        u = _kid(v, "uuid")
        if u is not None and _strs(u):
            via_en[_strs(u)[0]] = _enabled(v)
    pad_en = _pad_flags(root)
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

    items: list[Item] = []
    for v in bg.vias_of():
        ring = (v.diameter - v.drill) / 2.0 if v.drill else None
        items.append(Item("via", v.net, v.at, v.poly, v.layers, ring,
                          v.diameter, via_en.get(v.uuid, False), uuid=v.uuid))
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
        items.append(Item(kind, p.net or "", p.center, p.poly, p.layers, ring,
                          min(p.size), pad_en.get(key, False),
                          ref=p.ref, number=p.number))
    if area is not None:
        items = [it for it in items if it.poly.intersects(area)]
        near = unary_union([it.poly for it in items]).buffer(0.05) \
            if items else None
        drop_zones = [d for d in drop_zones
                      if near is not None and d[2].intersects(near)]

    fr = float(cfg.get("filter_ratio") or DEFAULTS["filter_ratio"])
    for it in items:
        if not it.net:
            continue
        cu = it.poly.buffer(TOL)
        for layer in it.layers:
            n = 0
            for t in bg.tracks_of(net=it.net, layer=layer):
                ends = (t.shape.coords[0], t.shape.coords[-1])
                ins = [cu.contains(Point(e)) for e in ends]
                if t.width > fr * it.min_size_mm + 1e-9:
                    continue
                if ins[0] != ins[1]:
                    n += 1
                elif not ins[0] and t.shape.intersects(it.poly):
                    n += 2  # runs through the item: a drop on each side
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
            best[1].drops[layer] = best[1].drops.get(layer, 0) + 1
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
