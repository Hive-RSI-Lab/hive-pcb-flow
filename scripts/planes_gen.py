"""planes_gen - GND/power plane pours + thermal vias + refill (S11, SPEC P7.2).

Plans copper pours from constraints.json["planes"] (or sensible defaults),
creates them via the SWIG bundled-python worker (lib/route_swig.py add_zones -
zones are saved UNFILLED, ZONE_FILLER segfaults headless), drops thermal-via
grids into exposed pads that sit on a plane net (route_edit op list), refills
via `kicad-cli pcb drc --refill-zones --save-board`, then verifies every
planned zone actually filled (geom re-parse). Inner plane layers (In*.Cu)
whose pours cover the board and that carry no other net's tracks are marked
LT_POWER and SAVED with the board so route_auto's DSN export carries
"(type power)"; an inner layer with only a region pour, or with signal
routing, keeps its type and gets a note (#925).

Contract:
  planes_gen.py --pcb B.kicad_pcb [--constraints c.json] [--out-report r.json]
      [--inset-mm 0.5] [--no-thermal-vias] [--ep-min-mm2 4.0]
      [--via-size 0.6] [--via-drill 0.3] [--via-pitch 1.2]
  exit 0 = all planned zones filled; 1 = a planned zone filled to ZERO area
  (violation kind "zone_unfilled" - board still updated, the fix loop owns
  it); 2 = error (original board untouched).

constraints.json["planes"] schema (this module is the owner):
  "planes": [
    {"net": "GND",            # required - net name on the board
     "layer": "In1.Cu",       # required - copper layer for the pour
     "region": null,          # optional [x1,y1,x2,y2] mm; null/absent =
                              #   board outline bbox inset by --inset-mm
     "priority": 0,           # optional; auto-raised so a smaller zone
                              #   overlapping a larger one on the same layer
                              #   wins (power island inside a pour)
     "min_island_mm2": null,  # optional island-removal area threshold
     "clearance": null,       # optional local zone clearance mm
     "min_width": 0.25,       # optional min fill width mm (corpus default)
     "connect": "thermal"},   # optional "solid" -> (connect_pads yes ...)
                              #   for high-current fan-in lobes (T6 P7B-2;
                              #   thermal relief starves a 5 A pad)
    ...]

When the key is absent, defaults are derived from the board:
  2-layer: one GND pour on B.Cu spanning the outline.
  4-layer: GND plane on In1.Cu + the dominant power net (most pads among
           constraints["power"] entries, else pad counts) on In2.Cu.
Either way, every constraints["high_speed"][*].reference net MUST end up
with a plane: a missing reference net gets a default full pour on the first
plane-candidate layer with no planned zone (noted in facts).

Atomicity: the board + same-stem sidecars are staged in a scratch dir inside
the board's directory; worker + route_edit + refill operate on the staged
copy; only the .kicad_pcb is os.replace()d back (board.Save drops a default
.kicad_pro sibling - the real one is never touched). Any failure = exit 2,
original untouched.

Idempotency: a planned zone whose (net, layer, region) is already covered
>= 80% by an existing fill is SKIPPED (status "existing" in facts.zones) -
re-adding it would duplicate the pour and KiCad 10 DRC flags overlapping
same-priority zones (zones_intersect) even for the SAME net (S11-verified).

A board that GREW after the pour (board_edit) is the exception: when the
existing same-net zone OUTLINES on the layer miss more than REGROW_TOL of
the planned region and each such zone lies wholly inside it, those zones
are removed (at any fill coverage: a board that grew a lot is the same
case, #923 point 2) and the planned one added in their place (status "regrown").
That is what lets "re-run planes_gen" after a resize reach the new edge.

Thermal vias honour constraints.json["thermal"][*].min_vias per ref: 0
skips that part's grid (a live tab such as an LDO's VOUT must not be
drilled into the GND plane), and a grid short of a positive floor is noted
in facts.thermal_pads.

Known limitation: zero-fill detection intersects the net's fill with the
planned region - a PRE-EXISTING same-net fill inside the region can mask a
new zone that filled to nothing (zone creation itself is count-verified).
"""
from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import checklib  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import kc  # noqa: E402
import route_edit  # noqa: E402
import routelib  # noqa: E402
from checklib import CheckError  # noqa: E402
from shapely.geometry import MultiPolygon, Point, Polygon, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

INSET_MM = 0.5          # default pour inset from the outline bbox
MIN_WIDTH_MM = 0.25     # zone min fill width (golden corpus min_thickness)
VIA_SIZE_MM = 0.6       # thermal via diameter
VIA_DRILL_MM = 0.3      # thermal via drill
VIA_PITCH_MM = 1.2      # thermal via grid pitch
VIA_MARGIN_MM = 0.05    # via annulus must stay this far inside the pad
EP_MIN_MM2 = 4.0        # exposed-pad heuristic floor (largest netted SMD pad)
FILL_EPS_MM2 = 0.01     # below this a planned zone counts as unfilled
EXISTING_COVER = 0.80   # existing-fill coverage above which an entry is skipped
REGROW_TOL_MM2 = 1.0    # zone-outline shortfall below which a pour is current
PLANE_LAYER_COVER = 0.80  # outline share an inner layer's pours must cover
                          # before the layer is typed power (#925)

_INNER_RE = re.compile(r"^In\d+\.Cu$")
_PLANE_KEYS = {"net", "layer", "region", "priority", "min_island_mm2",
               "clearance", "min_width", "connect"}
_CONNECT_VALUES = {"solid", "thermal"}
HOLE_EDGE_GAP = 0.5     # mm drill edge-to-edge floor (KiCad hole_to_hole)
EP_OWN_ARRAY = 4        # existing thru-hole items in the land -> skip grid


# ------------------------------------------------------------------ planning

def _ref_nets(constraints: dict | None) -> list[str]:
    """Reference nets demanded by constraints["high_speed"] (str or {layer: net})."""
    out: list[str] = []
    for e in (constraints or {}).get("high_speed") or []:
        ref = e.get("reference") if isinstance(e, dict) else None
        vals = ([ref] if isinstance(ref, str)
                else list(ref.values()) if isinstance(ref, dict) else [])
        for v in vals:
            if isinstance(v, str) and v and v not in out:
                out.append(v)
    return out


def _pad_counts(bg: geom.BoardGeom) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in bg.pads_of():
        if p.net:
            counts[p.net] = counts.get(p.net, 0) + 1
    return counts


def dominant_power_net(bg: geom.BoardGeom, constraints: dict | None) -> str | None:
    """The power net with the most pads: candidates from constraints["power"],
    else every board net except GND / high-speed reference nets."""
    counts = _pad_counts(bg)
    cands = [e.get("net") for e in (constraints or {}).get("power") or []
             if isinstance(e, dict)]
    cands = [n for n in cands if n and counts.get(n, 0) >= 2]
    if not cands:
        skip = {"GND", *_ref_nets(constraints)}
        cands = [n for n, c in counts.items() if n not in skip and c >= 2]
    if not cands:
        return None
    return max(sorted(cands), key=lambda n: counts[n])


def _default_entries(bg: geom.BoardGeom,
                     constraints: dict | None) -> tuple[list[dict], list[str]]:
    layers = bg.copper_layers
    notes: list[str] = []
    if len(layers) >= 4:
        plan = [{"net": "GND", "layer": layers[1]}]
        pwr = dominant_power_net(bg, constraints)
        if pwr:
            plan.append({"net": pwr, "layer": layers[2]})
        else:
            plan.append({"net": "GND", "layer": layers[2]})
            notes.append(f"no power net found; {layers[2]} gets a second "
                         "GND plane")
    else:
        plan = [{"net": "GND", "layer": layers[-1]}]
    return plan, notes


def _plane_candidate_layers(bg: geom.BoardGeom) -> list[str]:
    """Preferred layers for an auto-added plane: inner first, then B.Cu."""
    inner = [l for l in bg.copper_layers if _INNER_RE.match(l)]
    return inner + [bg.copper_layers[-1]]


def _region_rect(entry: dict, bg: geom.BoardGeom, inset: float,
                 idx: int) -> tuple[float, float, float, float]:
    region = entry.get("region")
    if region is not None:
        if not (isinstance(region, (list, tuple)) and len(region) == 4
                and all(isinstance(v, (int, float)) for v in region)):
            raise CheckError(f"planes[{idx}]: region must be [x1,y1,x2,y2] mm")
        x1, y1, x2, y2 = (float(v) for v in region)
    else:
        if bg.outline.is_empty:
            raise CheckError("board has no Edge.Cuts outline - cannot derive "
                             "a default pour region")
        bx1, by1, bx2, by2 = bg.outline.bounds
        x1, y1 = bx1 + inset, by1 + inset
        x2, y2 = bx2 - inset, by2 - inset
    if not (x2 > x1 and y2 > y1):
        raise CheckError(f"planes[{idx}]: degenerate region "
                         f"[{x1}, {y1}, {x2}, {y2}]")
    return (round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3))


def _rect_area(z: dict) -> float:
    if z.get("_geom") is not None:
        return z["_geom"].area
    x1, y1, x2, y2 = z["_rect"]
    return (x2 - x1) * (y2 - y1)


def _shape(z: dict):
    """The entry's region as a shapely polygon (a merged entry's union)."""
    return z["_geom"] if z.get("_geom") is not None else box(*z["_rect"])


_MERGE_KEYS = ("net", "layer", "connect", "clearance", "min_width",
               "min_island_mm2")


def merge_same_net(plan: list[dict]) -> tuple[list[dict], list[str]]:
    """Fold same-net, same-layer entries whose regions overlap or touch, and
    whose settings match, into ONE zone over their union. Two such zones
    can't both stay: at one priority KiCad 10 flags them zones_intersect,
    and at two (what assign_priorities would give them) it fills them apart,
    so the net is split at the seam (lite-main, hifi-receiver-2: its PVDD
    strips were joined by hand-drawn tracks across every seam). A union with
    a hole, or one that only meets at a corner, stays as separate entries.
    Same-net entries that touch but differ in settings are left apart with
    a note. Returns (plan, notes)."""
    notes: list[str] = []
    n = len(plan)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(n):
        for j in range(i + 1, n):
            a, b = plan[i], plan[j]
            if a["net"] != b["net"] or a["layer"] != b["layer"] \
                    or a.get("_existing") or b.get("_existing") \
                    or not _rects_overlap(a["_rect"], b["_rect"]):
                continue
            if all(a.get(k) == b.get(k) for k in _MERGE_KEYS):
                parent[find(i)] = find(j)
            else:
                notes.append(
                    f"{a['net']} pours on {a['layer']} at {list(a['_rect'])} "
                    f"and {list(b['_rect'])} meet but differ in settings, so "
                    "they stay two zones that KiCad fills apart: give them "
                    "the same settings or join them with a track")
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out: list[dict] = []
    for root in sorted(groups, key=lambda r: min(groups[r])):
        idx = groups[root]
        if len(idx) == 1:
            out.append(plan[idx[0]])
            continue
        u = unary_union([box(*plan[i]["_rect"]) for i in idx])
        if not isinstance(u, Polygon) or u.interiors:
            out.extend(plan[i] for i in idx)
            notes.append(f"{plan[idx[0]]['net']} pours on "
                         f"{plan[idx[0]]['layer']} meet only at a corner or "
                         "around a hole; left as separate zones")
            continue
        u = u.simplify(0)
        z = dict(plan[idx[0]])
        given = [plan[i]["priority"] for i in idx
                 if plan[i].get("priority") is not None]
        z["priority"] = max(given) if given else None
        z["_geom"] = u
        z["_rect"] = tuple(round(v, 3) for v in u.bounds)
        z["_merged_from"] = [list(plan[i]["_rect"]) for i in idx]
        out.append(z)
        notes.append(f"merged {len(idx)} touching {z['net']} pours on "
                     f"{z['layer']} into one zone")
    return out, notes


def _rects_overlap(a, b) -> bool:
    # <= not <: rects that merely TOUCH along a shared edge still trip
    # KiCad 10 zones_intersect at equal priority (LEARNINGS 1327 (b), the
    # keepout-band pattern), so touching counts as conflicting; same-net
    # fills at distinct priorities still merge into one island.
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def assign_priorities(plan: list[dict]) -> None:
    """A smaller zone overlapping a larger one on the same layer needs a
    STRICTLY higher priority or the island never fills (KiCad fills the
    higher-priority zone first and the lower one keeps clear of it). ANY
    same-layer overlap - INCLUDING a shared edge - gets distinct
    priorities: KiCad 10 DRC flags same-priority overlapping/touching zones
    (zones_intersect) even for the same net.
    Explicit priorities are honored when they already satisfy that."""
    order = sorted(range(len(plan)), key=lambda i: (-_rect_area(plan[i]), i))
    done: list[int] = []
    for i in order:
        z = plan[i]
        floor = 0
        for j in done:  # every done entry has area >= z's (sort order)
            o = plan[j]
            if o["layer"] == z["layer"] and _rects_overlap(o["_rect"],
                                                           z["_rect"]) \
                    and _shape(o).intersects(_shape(z)):
                floor = max(floor, o["priority"] + 1)
        given = z.get("priority")
        z["priority"] = max(int(given) if given is not None else 0, floor)
        done.append(i)


def build_plan(bg: geom.BoardGeom, constraints: dict | None,
               inset: float = INSET_MM) -> tuple[list[dict], dict]:
    """-> (plan, meta). Plan entries carry net/layer/region/priority plus the
    resolved "_rect"; meta = {source, added_for_reference, notes}."""
    constraints = constraints or {}
    notes: list[str] = []
    if isinstance(constraints.get("planes"), list):
        plan = [dict(e) for e in constraints["planes"]]
        source = "constraints"
    else:
        plan, notes = _default_entries(bg, constraints)
        source = "defaults"

    # validation
    if not plan:
        raise CheckError("planes plan is empty")
    for i, z in enumerate(plan):
        if not isinstance(z, dict):
            raise CheckError(f"planes[{i}]: entries must be objects")
        unknown = z.keys() - _PLANE_KEYS
        if unknown:
            raise CheckError(f"planes[{i}]: unknown keys {sorted(unknown)}")
        net, layer = z.get("net"), z.get("layer")
        if not (isinstance(net, str) and net):
            raise CheckError(f"planes[{i}]: net must be a non-empty string")
        if layer not in bg.copper_layers:
            raise CheckError(f"planes[{i}]: layer {layer!r} is not a copper "
                             f"layer of this board {bg.copper_layers}")
        if net not in bg.nets:
            raise CheckError(f"planes[{i}]: net {net!r} not on board")
        if z.get("connect") is not None \
                and z["connect"] not in _CONNECT_VALUES:
            raise CheckError(f"planes[{i}]: connect must be one of "
                             f"{sorted(_CONNECT_VALUES)}, got "
                             f"{z['connect']!r}")

    # every high-speed reference net must end up with a plane
    added: list[dict] = []
    planned_nets = {z["net"] for z in plan}
    for net in _ref_nets(constraints):
        if net in planned_nets:
            continue
        if net not in bg.nets:
            raise CheckError(f"high_speed reference net {net!r} not on board")
        used = {z["layer"] for z in plan}
        layer = next((l for l in _plane_candidate_layers(bg) if l not in used),
                     None)
        if layer is None:
            layer = bg.copper_layers[-1]
            notes.append(f"no free plane layer for reference net {net}; "
                         f"overlapping pour added on {layer}")
        entry = {"net": net, "layer": layer}
        plan.append(entry)
        added.append({"net": net, "layer": layer})
        planned_nets.add(net)
        notes.append(f"added default pour for high-speed reference net "
                     f"{net} on {layer}")

    for i, z in enumerate(plan):
        z["_rect"] = _region_rect(z, bg, inset, i)
    plan, merge_notes = merge_same_net(plan)
    notes += merge_notes
    for z in plan:
        # stale outlines first, at ANY coverage (#923 point 2): a board that
        # grew by more than 1 - EXISTING_COVER left the old zone in place
        # beside the new one, and DRC called the pair zones_intersect
        stale = _stale_outlines(bg, z)
        if stale:
            z["_regrow"] = stale
            notes.append(f"existing {z['net']} zone on {z['layer']} "
                         "stops short of the planned region (the board "
                         "grew); its outline is replaced by the planned "
                         "one")
            continue
        cov = _existing_cover(bg, z["net"], z["layer"], _shape(z))
        if cov >= EXISTING_COVER:
            z["_existing"] = round(cov, 3)
            notes.append(f"existing {z['net']} fill covers "
                         f"{cov:.0%} of the {z['layer']} region; zone not "
                         "re-added (re-run / pre-poured board)")
    assign_priorities(plan)
    return plan, {"source": source, "added_for_reference": added,
                  "notes": notes}


def _stale_outlines(bg: geom.BoardGeom, z: dict) -> list[dict]:
    """add_zones "remove" entries for the same-net zones on z's layer whose
    OUTLINES no longer reach the planned region - a board that grew after
    the pour (board_edit) leaves the plane's polygon at the old edge, and
    "existing fill covers it" would otherwise keep it there for good.
    Empty when the outlines already cover the region (to REGROW_TOL), or
    when a stale zone is not wholly inside the region (then it is not a
    smaller copy of this plane, and replacing it could lose copper)."""
    region = _shape(z)
    if not hasattr(region, "area"):
        region = box(*region)
    target = region.intersection(bg.outline) if not bg.outline.is_empty \
        else region
    zones = [q for q in bg.zones_of(z["net"], z["layer"])
             if not q.teardrop and len(q.outline) >= 3
             and tuple(q.layers) == (z["layer"],)]
    if not zones or target.is_empty:
        return []
    polys = [Polygon(q.outline).buffer(0) for q in zones]
    missing = target.difference(unary_union(polys)).area
    if missing <= max(REGROW_TOL_MM2, 0.01 * target.area):
        return []
    # inside the UNCLIPPED region: a rounded board corner clips the target
    # but not the (rectangular) zone outline drawn from the same region
    grown = region.buffer(0.05)
    stale = [(q, pg) for q, pg in zip(zones, polys) if grown.covers(pg)]
    if not stale:
        return []
    return [{"name": q.name, "net": q.net, "layer": z["layer"],
             "inside": [round(v, 4) for v in pg.bounds]} for q, pg in stale]


def _existing_cover(bg: geom.BoardGeom, net: str, layer: str, region) -> float:
    """Fraction of the (region ^ outline) already covered by `net`'s fill;
    region is a shapely polygon or an [x1, y1, x2, y2] rect."""
    fill = bg.zone_fill(net, layer)
    if fill.is_empty:
        return 0.0
    if not hasattr(region, "area"):
        region = box(*region)
    if not bg.outline.is_empty:
        region = region.intersection(bg.outline)
    if region.area <= FILL_EPS_MM2:
        return 0.0
    return fill.intersection(region).area / region.area


def worker_zones(plan: list[dict]) -> list[dict]:
    """Plan entries -> lib/route_swig.py add_zones zone dicts. The optional
    "connect": "solid" key (T6, P7B-2) makes the zone connect pads SOLID
    ((connect_pads yes ...)) instead of KiCad's default thermal relief -
    the scripted form of the pd-trigger pour fan-in hand patch (LEARNINGS
    791: thermal spokes would starve a 5 A pad), which router.md's
    never-hand-edit rule used to contradict."""
    return [{"net": z["net"], "layer": z["layer"],
             **({"poly": [[round(x, 4), round(y, 4)] for x, y in
                          list(z["_geom"].exterior.coords)[:-1]]}
                if z.get("_geom") is not None else {"rect": list(z["_rect"])}),
             "priority": z["priority"],
             "min_island_mm2": z.get("min_island_mm2"),
             "clearance": z.get("clearance"),
             "min_width": z.get("min_width", MIN_WIDTH_MM),
             "connect": z.get("connect")}
            for z in plan]


def plane_layer_types(plan: list[dict], bg: geom.BoardGeom
                      ) -> tuple[dict[str, str], list[str]]:
    """-> ({layer: "power"}, notes). An INNER layer becomes "power" only
    when it is a plane: its planned pours cover at least PLANE_LAYER_COVER
    of the board outline and it carries no track of a net not poured on it.
    A layer with only a region pour (a power island) or with signal routing
    is left as it is, with a note naming it (#925): marking it power makes
    the stackup describe a routing layer as a plane, and fill, no-track
    rules, the DSN export and fab notes read that type. Outer pours stay
    signal."""
    types: dict[str, str] = {}
    notes: list[str] = []
    outline = bg.outline
    for layer in sorted({z["layer"] for z in plan
                         if _INNER_RE.match(z["layer"])},
                        key=bg.copper_layers.index):
        zs = [z for z in plan if z["layer"] == layer]
        nets = {z["net"] for z in zs}
        cover = unary_union([_shape(z) for z in zs])
        frac = (cover.intersection(outline).area / outline.area
                if not outline.is_empty and outline.area > 0 else 0.0)
        foreign = sorted({t.net or "" for t in bg.tracks_of(layer=layer)
                          if t.net not in nets})
        n_tracks = sum(1 for t in bg.tracks_of(layer=layer)
                       if t.net not in nets)
        if frac >= PLANE_LAYER_COVER and not foreign:
            types[layer] = "power"
            continue
        why = []
        if frac < PLANE_LAYER_COVER:
            why.append(f"its pours cover {frac:.0%} of the board")
        if foreign:
            shown = ", ".join(n or "(no net)" for n in foreign[:5])
            more = f" +{len(foreign) - 5} more" if len(foreign) > 5 else ""
            why.append(f"it carries {n_tracks} track(s) of other nets "
                       f"({shown}{more})")
        notes.append(f"{layer} layer type not set to power: "
                     + " and ".join(why))
    return types, notes


# ------------------------------------------------------------- thermal vias

def ep_pads(bg: geom.BoardGeom, nets: set[str],
            min_mm2: float = EP_MIN_MM2) -> list:
    """Exposed-pad candidates: per footprint, the largest NETTED SMD pad when
    its area >= min_mm2 AND its net is a plane net. Standard EP footprints
    carry no-net SMD sub-segments overlapping the EP - those are ignored
    entirely (prior-attempt fact). A footprint whose EP sits on a non-plane
    net is skipped wholesale."""
    by_ref: dict[str, list] = {}
    for p in bg.pads_of():
        by_ref.setdefault(p.ref, []).append(p)
    out = []
    for ref in sorted(by_ref):
        netted_smd = [p for p in by_ref[ref] if len(p.layers) == 1 and p.net]
        if not netted_smd:
            continue
        # copper area, not the anchor box: a custom pad's anchor can be
        # 0.005 mm while its primitives make ~7 mm2 (LMG2100 PGND, #435)
        ep = max(netted_smd, key=lambda p: (_pad_area(p), p.number))
        if _pad_area(ep) < min_mm2:
            continue
        if ep.net not in nets:
            continue  # EP belongs to another net -> skip the whole footprint
        out.append(ep)
    return out


def _pad_area(p) -> float:
    try:
        a = p.poly.area
    except Exception:  # noqa: BLE001 - degenerate geometry: the box
        a = 0.0
    return a if a > 0 else p.size[0] * p.size[1]


def via_keepouts(bg: geom.BoardGeom) -> list:
    """Outlines of rule areas that forbid vias. A through via spans every
    copper layer, so a via keepout on any layer blocks it (#435)."""
    return [ra["outline"] for ra in bg.rule_areas
            if not ra["outline"].is_empty
            and str((ra.get("flags") or {}).get("vias", "")) == "not_allowed"]


def via_grid(poly: Polygon, *, size: float = VIA_SIZE_MM,
             pitch: float = VIA_PITCH_MM,
             margin: float = VIA_MARGIN_MM) -> list[tuple[float, float]]:
    """Grid points (pitch-spaced, centered on the polygon centroid) where a
    via of diameter `size` fits fully inside `poly`. Vias in the EP's own pad
    need no connecting track."""
    if poly.is_empty:
        return []
    inner = poly.buffer(-(size / 2.0 + margin))
    if inner.is_empty:
        return []
    cx, cy = poly.centroid.x, poly.centroid.y
    minx, miny, maxx, maxy = poly.bounds
    nx = int(math.ceil((maxx - minx) / (2.0 * pitch)))
    ny = int(math.ceil((maxy - miny) / (2.0 * pitch)))
    pts = []
    for j in range(-ny, ny + 1):
        for i in range(-nx, nx + 1):
            p = (cx + i * pitch, cy + j * pitch)
            if inner.covers(Point(p)):
                pts.append((round(p[0], 3), round(p[1], 3)))
    pts.sort()
    return pts


def _ep_existing_drills(bg: geom.BoardGeom, ep) -> list:
    """Drill extents already inside an EP's land: board vias whose centre
    lies in the pad plus same-footprint thru-hole pads (the U22 eFuse ships
    15 vias-in-pad in its OWN land - LEARNINGS 1327 (a); gridding on top
    produced 24 hole_to_hole + 2 holes_co_located)."""
    out = []
    for v in bg.vias_of():
        if ep.poly.covers(Point(v.at)):
            out.append(Point(v.at).buffer(max(v.drill, 0.3) / 2.0,
                                          quad_segs=8))
    for p in bg.pads_of(ref=ep.ref):
        if len(p.layers) > 1 and ep.poly.covers(Point(p.center)):
            dp = p.drill_poly
            out.append(dp if not dp.is_empty
                       else Point(p.center).buffer(0.15, quad_segs=8))
    return out


def thermal_via_ops(bg: geom.BoardGeom, plane_nets: set[str], *,
                    ep_min_mm2: float = EP_MIN_MM2, size: float = VIA_SIZE_MM,
                    drill: float = VIA_DRILL_MM,
                    pitch: float = VIA_PITCH_MM,
                    pour_layers: dict[str, set[str]] | None = None,
                    min_vias: dict[str, int] | None = None
                    ) -> tuple[list[dict], list[dict]]:
    """-> (route_edit add_via ops, per-pad facts). T6 (P7A-5): existing
    drills inside the land are respected - grid points inside the hole
    floor of any of them are dropped, and a land already carrying >=
    EP_OWN_ARRAY thru-hole items skips the grid entirely (the footprint
    ships its own via array). #435: no via inside a rule area that forbids
    vias, and none for a pad whose net has no pour on another copper layer
    (pour_layers: net -> layers poured; None = don't ask).
    min_vias (ref -> constraints.thermal[*].min_vias): 0 means the part
    wants no thermal vias (a live tab, an LDO's VOUT) and its grid is
    skipped; a grid that fits fewer than a positive floor says so in the
    pad's note (check_thermal holds the floor as an error)."""
    min_vias = min_vias or {}
    ops: list[dict] = []
    pads: list[dict] = []
    keepouts = via_keepouts(bg)
    for ep in ep_pads(bg, plane_nets, ep_min_mm2):
        if min_vias.get(ep.ref) == 0:
            pads.append({"ref": ep.ref, "pad": ep.number, "net": ep.net,
                         "vias": 0,
                         "note": "constraints.thermal min_vias is 0; "
                                 "grid skipped"})
            continue
        if pour_layers is not None and \
                not (set(pour_layers.get(ep.net) or ()) - set(ep.layers)):
            pads.append({"ref": ep.ref, "pad": ep.number, "net": ep.net,
                         "vias": 0,
                         "note": "net has no pour on another copper layer; "
                                 "vias would connect nothing"})
            continue
        existing = _ep_existing_drills(bg, ep)
        if len(existing) >= EP_OWN_ARRAY:
            pads.append({"ref": ep.ref, "pad": ep.number, "net": ep.net,
                         "vias": 0,
                         "note": f"footprint ships its own via array "
                                 f"({len(existing)} thru-hole items in the "
                                 "land); grid skipped"})
            continue
        pts = via_grid(ep.poly, size=size, pitch=pitch)
        if existing:
            floor = HOLE_EDGE_GAP + drill / 2.0
            pts = [p for p in pts
                   if all(h.distance(Point(p)) >= floor - 1e-9
                          for h in existing)]
        if keepouts:
            n0 = len(pts)
            pts = [p for p in pts
                   if not any(k.intersects(Point(p).buffer(size / 2.0))
                              for k in keepouts)]
            if n0 and not pts:
                pads.append({"ref": ep.ref, "pad": ep.number, "net": ep.net,
                             "vias": 0,
                             "note": "pad inside a rule area that forbids "
                                     "vias; grid skipped"})
                continue
        if not pts:
            continue
        for x, y in pts:
            ops.append({"op": "add_via", "at": [x, y], "size": size,
                        "drill": drill, "net": ep.net})
        fact = {"ref": ep.ref, "pad": ep.number, "net": ep.net,
                "vias": len(pts)}
        want = min_vias.get(ep.ref)
        if want and len(pts) < want:
            fact["note"] = (f"grid fits {len(pts)} vias, under "
                            f"constraints.thermal min_vias {want}")
        pads.append(fact)
    return ops, pads


def thermal_min_vias(constraints: dict | None) -> dict[str, int]:
    """ref -> min_vias from constraints["thermal"] entries that set it."""
    out: dict[str, int] = {}
    for e in (constraints or {}).get("thermal") or []:
        if isinstance(e, dict) and e.get("ref") \
                and e.get("min_vias") is not None:
            out[str(e["ref"])] = int(e["min_vias"])
    return out


# ------------------------------------------------------------- verification

def pour_layers(bg: geom.BoardGeom, plan: list[dict]) -> dict[str, set[str]]:
    """net -> copper layers it is poured on: the plan plus zones already on
    the board."""
    out: dict[str, set[str]] = {}
    for z in plan:
        out.setdefault(z["net"], set()).add(z["layer"])
    for z in bg.zones_of():
        if z.net:
            out.setdefault(z.net, set()).update(z.layers)
    return out


def _fill_in_region(bg: geom.BoardGeom, net: str, layer: str,
                    region) -> tuple[float, int]:
    """(area mm2, island count) of `net`'s zone fill inside the region (a
    shapely polygon or an [x1, y1, x2, y2] rect)."""
    fill = bg.zone_fill(net, layer)
    if fill.is_empty:
        return 0.0, 0
    clipped = fill.intersection(region if hasattr(region, "area")
                                else box(*region))
    if clipped.is_empty:
        return 0.0, 0
    if isinstance(clipped, Polygon):
        parts = [clipped]
    elif isinstance(clipped, MultiPolygon):
        parts = list(clipped.geoms)
    else:  # GeometryCollection from a boundary graze
        parts = [g for g in getattr(clipped, "geoms", [])
                 if isinstance(g, Polygon)]
    parts = [g for g in parts if g.area > FILL_EPS_MM2]
    return sum(g.area for g in parts), len(parts)


def verify_zones(after: geom.BoardGeom, plan: list[dict],
                 pre_counts: dict) -> tuple[list[dict], list[dict]]:
    """Count-check zone creation (hard error) + per-zone fill facts and
    zone_unfilled violations (exit-1 material). Entries skipped as
    "_existing" are fill-checked but not count-checked."""
    want: dict[tuple[str, str], int] = {}
    for z in plan:
        if z.get("_existing"):
            continue
        key = (z["net"], z["layer"])
        want[key] = want.get(key, 0) + 1
    for (net, layer), n in want.items():
        have = len(after.zones_of(net=net, layer=layer))
        if have < pre_counts.get((net, layer), 0) + n:
            raise CheckError(f"zone creation not persisted: expected "
                             f">={pre_counts.get((net, layer), 0) + n} zones "
                             f"for {net} on {layer}, found {have}")
    zone_facts: list[dict] = []
    violations: list[dict] = []
    for z in plan:
        area, islands = _fill_in_region(after, z["net"], z["layer"], _shape(z))
        zone_facts.append({"net": z["net"], "layer": z["layer"],
                           "region": list(z["_rect"]),
                           "priority": z["priority"],
                           "status": "existing" if z.get("_existing")
                           else "regrown" if z.get("_regrow") else "added",
                           "area_mm2": round(area, 3), "islands": islands})
        if area < FILL_EPS_MM2:
            x1, y1, x2, y2 = z["_rect"]
            violations.append(checklib.violation(
                "planes_gen", "error", ((x1 + x2) / 2.0, (y1 + y2) / 2.0),
                z["layer"], z["net"], [],
                f"planned zone on {z['layer']} filled to zero area",
                "planes_gen", kind="zone_unfilled",
                region=list(z["_rect"])))
    return zone_facts, violations


# -------------------------------------------------------------------- driver

def _stage_board(pcb: Path, work: Path) -> Path:
    """Copy the board + every same-stem sidecar (pro/prl/dru/sch) into work -
    kicad-cli refill needs the project sidecars for correct rules."""
    staged = work / pcb.name
    for src in pcb.parent.glob(pcb.stem + ".*"):
        if src.is_file() and not src.name.endswith(".lck"):
            shutil.copy2(src, work / src.name)
    if not staged.is_file():
        raise CheckError(f"staging failed: {staged}")
    return staged


def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints", default=None,
                    help="constraints.json (default: next to the board)")
    ap.add_argument("--inset-mm", type=float, default=INSET_MM,
                    help="default pour inset from the outline bbox")
    ap.add_argument("--no-thermal-vias", action="store_true",
                    help="skip the exposed-pad thermal via grids")
    ap.add_argument("--ep-min-mm2", type=float, default=EP_MIN_MM2,
                    help="exposed-pad area floor for thermal vias")
    ap.add_argument("--via-size", type=float, default=VIA_SIZE_MM)
    ap.add_argument("--via-drill", type=float, default=VIA_DRILL_MM)
    ap.add_argument("--via-pitch", type=float, default=VIA_PITCH_MM)
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)

    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    constraints = {}
    cpath = Path(args.constraints) if args.constraints \
        else pcb.parent / "constraints.json"
    if args.constraints or cpath.is_file():
        constraints = checklib.load_json(cpath, "constraints")

    bg = geom.BoardGeom.from_file(pcb)
    plan, meta = build_plan(bg, constraints, args.inset_mm)
    to_add = [z for z in plan if not z.get("_existing")]
    zones = worker_zones(to_add)
    layer_types, type_notes = plane_layer_types(plan, bg)
    meta["notes"] += type_notes
    plane_nets = {z["net"] for z in plan}
    removes = [r for z in plan for r in z.get("_regrow") or []]
    pre_counts = {(n, l): len(bg.zones_of(net=n, layer=l))
                  - sum(1 for r in removes
                        if (r["net"], r["layer"]) == (n, l))
                  for n, l in {(z["net"], z["layer"]) for z in to_add}}

    cli = env.find_kicad_cli()
    bp = env.find_kicad_python(cli) if cli else None
    if bp is None:
        raise CheckError("KiCad bundled python not found (env.py)")

    stage = Path(tempfile.mkdtemp(prefix=".aiee_planes_", dir=pcb.parent))
    try:
        staged = _stage_board(pcb, stage)

        # 1. zones (saved unfilled; inner plane layer types saved too).
        # Runs even with an empty to-add list so layer types stay enforced
        # on a re-run.
        routelib.run_worker(bp, {"verb": "add_zones", "board": str(staged),
                                 "out": str(staged), "zones": zones,
                                 "remove": removes,
                                 "layer_types": layer_types}, stage)

        # 2. thermal vias under exposed pads (atomic op list on the staged copy)
        via_ops: list[dict] = []
        via_pads: list[dict] = []
        if not args.no_thermal_vias:
            via_ops, via_pads = thermal_via_ops(
                bg, plane_nets, ep_min_mm2=args.ep_min_mm2,
                size=args.via_size, drill=args.via_drill,
                pitch=args.via_pitch, pour_layers=pour_layers(bg, plan),
                min_vias=thermal_min_vias(constraints))
            if via_ops:
                route_edit.apply_ops(staged, via_ops)

        # 3. refill (the pipeline's only headless fill path)
        kc.run_drc(cli, staged, refill=True, save_board=True)

        # 4. verify, then swap ONLY the .kicad_pcb back
        after = geom.BoardGeom.from_file(staged)
        zone_facts, violations = verify_zones(after, plan, pre_counts)
        os.replace(staged, pcb)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    facts = {
        "plan_source": meta["source"],
        "zones_added": len(to_add),
        "zones": zone_facts,
        "thermal_vias": len(via_ops),
        "thermal_pads": via_pads,
        "layer_types": layer_types,
        "added_for_reference": meta["added_for_reference"],
        "notes": meta["notes"],
    }
    payload = checklib.report("planes_gen", str(pcb), violations, facts=facts)
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("planes_gen", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
