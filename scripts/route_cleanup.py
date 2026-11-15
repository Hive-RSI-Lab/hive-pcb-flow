"""route_cleanup - post-route hygiene: dangling copper, loops, off-angle snap,
90-deg corners (S11, SPEC P7.4).

Runs AFTER route_auto / stitch_vias / plane_repair and BEFORE the drc_routed
gate. Four ordered passes, each a pure analysis over the parsed board that
emits route_edit ops:

  1. DANGLING: iteratively (fixpoint, cap 20) remove track segments and vias
     with a free end - an endpoint touching NO other same-net item (segment,
     via, pad copper, or same-net zone fill on that layer). "Touches" is
     COPPER-BODY overlap, not centerline proximity (T6/V13 root cause: two
     VBUS stubs ending 1.0 mm from a 3.0 mm trunk's centerline sit 0.5 mm
     INSIDE its copper - KiCad-connected, DRC 0 unconnected - and the old
     centerline test called them free and cut the net). On a DRC-clean board
     real gaps keep centerline distance >= (w1+w2)/2 + clearance, so the
     widened test cannot join across a legal gap. As in KiCad's own test, a
     track touching BOTH ends of a segment counts for the nearer end only,
     so a short jog or spur lying across a track is free (DRC flags it).
     A free end in same-net fill is a legal pour termination (kept); a via
     is kept when it joins 2+ same-net items or sits in same-net fill on any
     layer it spans. Our own graph analysis is the source of truth; DRC's
     track_dangling/via_dangling warnings are reported as a cross-check only.
  2. LOOPS: per net, graph of rounded endpoints (vias merge nodes across their
     spanned layers); cycles made ONLY of track segments lose their single
     longest segment (connectivity preserved); fixpoint, cap 10. Paths through
     zone fill or pad copper never form edges, so parallel drops to a plane
     are NOT treated as loops. Guard (T6): a victim whose netconn edge is a
     BRIDGE of the net's full copper graph (tracks+vias+pads+zones) is load-
     bearing by definition and is vetoed, never removed. After loop removal
     the dangling sweep re-runs once (a broken loop can orphan a stub).
  3. SNAP (skip with --no-snap): every straight segment check_route_style
     calls off-angle (its own `off_angle` helper) is rerouted between the
     same endpoints on 0/90 and 45-degree legs. A segment is first cut at
     its joints: KiCad joins two items when an anchor of one (track or arc
     end, via centre, pad centre) lies inside the other's copper, so a
     same-net anchor on the segment's centreline, away from its end caps,
     is a joint the legs must pass through. Copper that only overlaps the
     segment, with no anchor on it, is not a joint. Each piece tries the
     two doglegs first (diagonal-first, axis-first), then, only when both
     fail, the two Z routes (axis-45-axis, 45-axis-45), which swing half as
     far off the line. A route is legal when its legs keep width/2 + the
     pair's clearance (max of the two nets' netclass and .kicad_dru
     clearance, never below the board floor) from every foreign track, via,
     pad and drill (a drill - NPTH, pad or via hole - also keeps the board's
     min_hole_clearance, default 0.25 mm), width/2 + the copper-to-edge rule
     from the board edge, and stay out of track keepouts. Where the router
     already left the replaced copper closer than that, a route is also
     legal when it comes no closer to each foreign net than the old copper
     did and still keeps what DRC enforces: an unconditioned .kicad_dru
     clearance rule overrides the netclass in KiCad's DRC. That fallback is
     off when the .kicad_dru has any conditioned or layer-scoped clearance
     rule, since its value then depends on the pair. Legs accepted this run count as foreign copper for the
     next, and a foreign segment already replaced stops blocking (sweeps
     repeat, cap 5). Foreign zone FILL is not an obstacle: the refill after
     apply moves it. Of the legal routes the one that makes the fewest
     needless jogs wins, then the one with more spare clearance
     (diagonal-first on a tie). Then each needless jog that contains a
     snapped leg is merged: its three segments become one straight or
     dogleg between the outer ends, when legal and the net ends with fewer
     jogs (fact `jogs_merged`). --dejog merges every needless jog on the
     board the same way, snapped leg or not (a grid router's 0.1 mm
     staircases: 1392 on hifi-receiver-2 core, a third of them legal to
     merge), on every net the snap would touch. Left alone: a piece with no legal route,
     every diff-pair member (check_diffpair.discover_pairs by name, unless
     --constraints declares diff_pairs - then that list, even [], is the
     whole set, so speaker A+/A- nets get snapped), every net
     named with --keep-net and, with --constraints, every net that
     constraints.json gives intended geometry (diff_pairs members,
     length_match groups, rf entries, high_speed entries with an
     impedance_ohm), since bending one side alone breaks a pair's coupling
     and a matched net's length. Each one left is listed in `off_angle_left` with
     its reason (blocked + the limiting net, or kept_net); check_route_style
     fails verify on it until it is fixed by hand or waived for that net.
  4. CORNERS (skip with --no-smooth): same-net/layer/width segment pairs
     meeting at 88-92 deg with both legs >= 3*width get a 45-deg chamfer:
     both legs shortened by c = min(min_leg/3, 2*width, 1.0 mm) plus the
     connecting diagonal - only when the diagonal's corridor (width/2 +
     0.2 mm) is clear of foreign copper and of pad copper of ANY net, and the
     corner is not at a pad center/via (0.05 mm) or near other attachments.
     Segments the snap pass replaced are not chamfered.

--snap-only runs pass 3 alone (no dangling/loop/corner edits). It is the
routing stage's mandatory step after plane_repair; the full cleanup stays
optional.

The full op list is generated from the parse BEFORE anything is applied
(--dry-run stops there and needs no toolchain). Otherwise: DRC before,
route_edit.apply_ops (atomic), zone refill iff an op touched a layer carrying
fill, DRC after. There is no rollback after apply: if connectivity degraded
(unconnected_items grew) or new DRC errors appeared, the report says so
LOUDLY (violation kind "cleanup_regression", exit 1) - the orchestrator can
git-restore the board.

Contract (SPEC section 6):
  route_cleanup.py --pcb B.kicad_pcb [--dry-run] [--no-smooth] [--no-snap]
                   [--snap-only] [--keep-net NET ...]
                   [--constraints constraints.json] [--out-report r.json]
  JSON to stdout or --out-report; exit 0 pass / 1 violations / 2 error.
  Deterministic: stable sorts (uuid order) everywhere, no RNG.
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import sexpdata  # noqa: E402
from shapely.geometry import LineString, Point  # noqa: E402

import check_route_style as crs  # noqa: E402  (off_angle: one definition)
import checklib  # noqa: E402
import geom  # noqa: E402
import kc  # noqa: E402
import netconn  # noqa: E402
import route_edit  # noqa: E402
from checklib import CheckError  # noqa: E402

TOL = 0.01              # mm: "touches" tolerance for connectivity
PAD_VIA_KEEPOUT = 0.05  # mm: corners this close to a pad center/via stay
SMOOTH_MIN_LEG = 3.0    # legs must be >= 3*width to chamfer
SMOOTH_MAX_C = 1.0      # mm: chamfer cut ceiling
SMOOTH_CLEAR = 0.2      # mm: clearance margin around the chamfer corridor
DANGLING_CAP = 20       # fixpoint iteration caps
LOOP_CAP = 10
SNAP_MARGIN = 0.005     # mm: kept beyond the clearance rule (rounding)
SNAP_PROBE = 0.05       # mm: spatial-query slack around the snapped legs
KICAD_CLEARANCE = 0.2   # mm: KiCad defaults when the project sets none
KICAD_EDGE = 0.5
KICAD_HOLE_CLEARANCE = 0.25  # mm: board-setup min_hole_clearance default
DRILL = "<drill>"       # SnapEnv obstacle uuid of a hole (NPTH, pad, via)


# ============================================================ data model

@dataclass(frozen=True)
class Seg:
    uuid: str
    net: str
    layer: str
    width: float
    a: tuple[float, float]
    b: tuple[float, float]

    @property
    def length(self) -> float:
        return math.dist(self.a, self.b)


@dataclass(frozen=True)
class ViaItem:
    uuid: str
    net: str
    at: tuple[float, float]
    size: float
    drill: float
    layers: tuple[str, ...]  # spanned copper layers (inclusive)


@dataclass(frozen=True)
class PadItem:
    net: str | None
    layers: tuple[str, ...]
    center: tuple[float, float]
    poly: object  # shapely polygon


# ============================================================ local parser
# geom.py deliberately hides uuids; op generation needs them, so this small
# sexpdata walk extracts JUST (segment ...) and (via ...) nodes + uuids.

def parse_items(pcb: Path, copper_layers: list[str]) -> tuple[list[Seg],
                                                              list[ViaItem]]:
    try:
        root = sexpdata.loads(Path(pcb).read_text(encoding="utf-8"))
    except Exception as exc:
        raise CheckError(f"cannot parse {pcb}: {exc}") from exc

    def tok(x):
        return x.value() if isinstance(x, sexpdata.Symbol) else x

    def kid(node, name):
        for c in node[1:]:
            if isinstance(c, list) and c and tok(c[0]) == name:
                return c
        return None

    def nums(node):
        return [float(x) for x in node[1:] if isinstance(x, (int, float))]

    def first_str(node):
        for x in node[1:]:
            v = tok(x)
            if isinstance(v, str):
                return v
        return None

    net_table = {}
    for c in root[1:]:
        if isinstance(c, list) and c and tok(c[0]) == "net":
            n = nums(c)
            if n:
                net_table[int(n[0])] = first_str(c) or ""

    def net_of(item):
        node = kid(item, "net")
        if node is None:
            return ""
        s = first_str(node)
        if s is not None:
            return s
        n = nums(node)
        return net_table.get(int(n[0]), "") if n else ""

    def uuid_of(item):
        node = kid(item, "uuid") or kid(item, "tstamp")
        return (first_str(node) or "") if node is not None else ""

    cu = set(copper_layers)
    segs: list[Seg] = []
    vias: list[ViaItem] = []
    for c in root[1:]:
        if not (isinstance(c, list) and c):
            continue
        h = tok(c[0])
        if h == "segment":
            s, e = kid(c, "start"), kid(c, "end")
            w, l = kid(c, "width"), kid(c, "layer")
            if not (s and e and w and l) or first_str(l) not in cu:
                continue
            a, b = nums(s), nums(e)
            segs.append(Seg(uuid_of(c), net_of(c), first_str(l), nums(w)[0],
                            (a[0], a[1]), (b[0], b[1])))
        elif h == "via":
            at, size = kid(c, "at"), kid(c, "size")
            if not (at and size):
                continue
            lnode = kid(c, "layers")
            names = [tok(x) for x in lnode[1:]] if lnode is not None else []
            names = [n for n in names if isinstance(n, str) and n in cu]
            if len(names) >= 2:  # from/to SPAN, expanded like geom does
                i, j = sorted((copper_layers.index(names[0]),
                               copper_layers.index(names[-1])))
                span = tuple(copper_layers[i:j + 1])
            elif names:
                span = (names[0],)
            else:
                span = tuple(copper_layers)  # default through-via
            p = nums(at)
            d = kid(c, "drill")
            vias.append(ViaItem(uuid_of(c), net_of(c), (p[0], p[1]),
                                nums(size)[0],
                                nums(d)[0] if d and nums(d) else 0.0, span))
    return segs, vias


# ============================================================ geometry helpers

def _pt_seg_dist(p, a, b) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    l2 = dx * dx + dy * dy
    if l2 <= 1e-18:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def _node(layer: str, p) -> tuple:
    """Snap a point to the TOL grid; node identity for graph passes."""
    return (layer, round(p[0] * 100), round(p[1] * 100))


def _fill_touches(fillfn, net, layer, p, tol) -> bool:
    g = fillfn(net, layer)
    return (g is not None and not g.is_empty
            and g.distance(Point(p)) <= tol)


# ============================================================ pass 1: dangling

def _seg_free(seg: Seg, segs, vias, pads, fillfn, arcs, tol) -> bool:
    """True if `seg` dangles: one of its ends touches no other same-net item.

    This is KiCad 10's rule (CONNECTIVITY_DATA::TestTrackEndpointDangling,
    which DRC's track_dangling calls with aIgnoreTracksInPads=true): each
    item counts for the end it touches. A track or arc that touches BOTH
    ends counts only for the end nearer one of its own ends (KiCad's
    getMinDist; a tie goes to `b`, KiCad's end), so a short jog or spur
    lying across a track still dangles. A pad or via that touches both ends
    counts for both (that is what aIgnoreTracksInPads=true does), and so
    does a fill.

    One exception keeps the safe side: when that one-end rule is all that
    makes `seg` dangle, and the same-net copper `seg` meets falls into two
    or more pieces that do not touch each other (`_bridges`), `seg` stays.
    It is then the only local link between, say, the track it lies across
    and a pad or track at its other end, and dropping it could split the
    net. DRC may still flag it; route_cleanup leaves it rather than raise
    an island count.

    "Touches" is COPPER overlap, not centerline proximity (T6/V13 fix): the
    end's round cap (radius seg.width/2) overlapping another item's body
    counts as connected, as in KiCad's connectivity model. KiCad's test has
    no tolerance and neither has the both-ends case here; every other
    contact gets `tol` more, so this never drops a segment KiCad would keep.
    On a DRC-clean board that cannot join across a real gap (centerline
    distance of legally separated copper >= (w1+w2)/2 + clearance > every
    reach used here)."""
    half = seg.width / 2.0
    pa, pb = Point(seg.a), Point(seg.b)
    hit_a = _fill_touches(fillfn, seg.net, seg.layer, seg.a, half + tol)
    hit_b = _fill_touches(fillfn, seg.net, seg.layer, seg.b, half + tol)
    if hit_a and hit_b:
        return False
    both = False                           # a track/arc touched both ends

    def track(da, db, reach, ends) -> bool:
        nonlocal hit_a, hit_b, both
        if da < reach and db < reach:      # KiCad's hit at both ends
            both = True
            ma = min(math.dist(seg.a, e) for e in ends)
            mb = min(math.dist(seg.b, e) for e in ends)
            if ma < mb - 1e-6:
                hit_a = True
            else:
                hit_b = True
        else:
            hit_a = hit_a or da <= reach + tol
            hit_b = hit_b or db <= reach + tol
        return hit_a and hit_b

    for o in segs:
        if o is seg or o.net != seg.net or o.layer != seg.layer:
            continue
        if track(_pt_seg_dist(seg.a, o.a, o.b), _pt_seg_dist(seg.b, o.a, o.b),
                 (seg.width + o.width) / 2.0, (o.a, o.b)):
            return False
    for net, layer, line, aw in arcs:
        if net == seg.net and layer == seg.layer and track(
                line.distance(pa), line.distance(pb), (seg.width + aw) / 2.0,
                (line.coords[0], line.coords[-1])):
            return False
    for v in vias:
        if v.net == seg.net and seg.layer in v.layers:
            r = v.size / 2.0 + half + tol
            hit_a = hit_a or math.dist(seg.a, v.at) <= r
            hit_b = hit_b or math.dist(seg.b, v.at) <= r
    for pd in pads:
        if pd.net == seg.net and seg.layer in pd.layers:
            hit_a = hit_a or pd.poly.distance(pa) <= half + tol
            hit_b = hit_b or pd.poly.distance(pb) <= half + tol
    if hit_a and hit_b:
        return False
    return not (both and _bridges(seg, segs, vias, pads, fillfn, arcs, tol))


def _body(a, b):
    """Centerline of a track as a shapely geometry (a point if it has no
    length, which LineString would reject)."""
    return LineString([a, b]) if tuple(a) != tuple(b) else Point(a)


def _bridges(seg: Seg, segs, vias, pads, fillfn, arcs, tol) -> bool:
    """True if the same-net copper meeting `seg` (within seg.width/2 + tol
    of its centerline, on its layer) falls into 2+ pieces that do not
    overlap one another: `seg` is then the only local link between them.

    The pieces are the other tracks and arcs, the vias that span the layer,
    the pads, and each polygon of the fill, grouped by real overlap. Copper
    that joins further away still counts as two pieces here, so this errs
    towards keeping `seg`."""
    reach = seg.width / 2.0 + tol
    body = _body(seg.a, seg.b)
    shapes = []

    def add(g):
        if g is not None and not g.is_empty and g.distance(body) <= reach:
            shapes.append(g)

    for o in segs:
        if o is not seg and o.net == seg.net and o.layer == seg.layer:
            add(_body(o.a, o.b).buffer(o.width / 2.0))
    for net, layer, line, aw in arcs:
        if net == seg.net and layer == seg.layer:
            add(line.buffer(aw / 2.0))
    for v in vias:
        if v.net == seg.net and seg.layer in v.layers:
            add(Point(v.at).buffer(v.size / 2.0))
    for pd in pads:
        if pd.net == seg.net and seg.layer in pd.layers:
            add(pd.poly)
    fill = fillfn(seg.net, seg.layer)
    if fill is not None and not fill.is_empty:
        for piece in getattr(fill, "geoms", [fill]):
            add(piece)
    parent = list(range(len(shapes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(shapes)):
        for j in range(i + 1, len(shapes)):
            if find(i) != find(j) and shapes[i].intersects(shapes[j]):
                parent[find(i)] = find(j)
    return len({find(i) for i in range(len(shapes))}) > 1


def _via_keep(v: ViaItem, segs, pads, fillfn, tol, vias=()) -> bool:
    """Keep a via if it sits in same-net fill on any spanned layer or joins
    2+ same-net items (tracks/pads/vias); otherwise it has a free end. Track
    contact is copper overlap (barrel + track half-width, T6/V13 fix). An
    HDI stack is several vias at one spot, each touching one track at most,
    so a via sharing a layer with this one counts as an item it joins."""
    for layer in v.layers:
        if _fill_touches(fillfn, v.net, layer, v.at, tol):
            return True
    r = v.size / 2.0 + tol
    contacts = 0
    for s in segs:
        if (s.net == v.net and s.layer in v.layers
                and _pt_seg_dist(v.at, s.a, s.b) <= r + s.width / 2.0):
            contacts += 1
            if contacts >= 2:
                return True
    pt = Point(v.at)
    for pd in pads:
        if (pd.net == v.net and set(pd.layers) & set(v.layers)
                and pd.poly.distance(pt) <= r):
            contacts += 1
            if contacts >= 2:
                return True
    for o in vias:
        if (o is not v and o.net == v.net and set(o.layers) & set(v.layers)
                and math.dist(o.at, v.at) <= r + o.size / 2.0):
            contacts += 1
            if contacts >= 2:
                return True
    return False


def find_dangling(segs, vias, pads, fillfn, arcs=(), tol=TOL,
                  cap=DANGLING_CAP) -> tuple[list[str], list[str]]:
    """Fixpoint removal of free-ended segments/vias. Returns uuid lists
    (segments, vias) in removal order. Items without a uuid are anchors only
    (they can never be removed)."""
    alive_s = list(segs)
    alive_v = list(vias)
    gone_s: list[str] = []
    gone_v: list[str] = []
    for _ in range(cap):
        drop_s = [s for s in alive_s if s.uuid and _seg_free(
            s, alive_s, alive_v, pads, fillfn, arcs, tol)]
        drop_v = [v for v in alive_v
                  if v.uuid and not _via_keep(v, alive_s, pads, fillfn, tol,
                                              alive_v)]
        if not drop_s and not drop_v:
            break
        ds = {id(x) for x in drop_s}
        dv = {id(x) for x in drop_v}
        alive_s = [s for s in alive_s if id(s) not in ds]
        alive_v = [v for v in alive_v if id(v) not in dv]
        gone_s += sorted(s.uuid for s in drop_s)
        gone_v += sorted(v.uuid for v in drop_v)
    return gone_s, gone_v


# ============================================================ pass 2: loops

def _via_canon(vias):
    """Canonical-node mapper: vias merge (layer, pos) nodes across their
    span. Finalized before segment processing, so lookups are stable."""
    parent: dict = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for v in sorted(vias, key=lambda v: (v.uuid, v.at)):
        keys = [_node(l, v.at) for l in v.layers]
        for k in keys[1:]:
            ra, rb = find(keys[0]), find(k)
            if ra != rb:
                parent[ra] = rb
    return lambda k: find(k) if k in parent else k


def _bfs_path(adj, src, dst):
    """Edge path src->dst in the (segment-only) adjacency, or None."""
    if src == dst:
        return []
    prev = {src: None}
    queue = [src]
    while queue:
        nxt = []
        for n in queue:
            for m, e in adj.get(n, ()):
                if m in prev:
                    continue
                prev[m] = (n, e)
                if m == dst:
                    path, cur = [], m
                    while prev[cur] is not None:
                        n2, e2 = prev[cur]
                        path.append(e2)
                        cur = n2
                    return path
                nxt.append(m)
        queue = nxt
    return None


def _loop_sweep(segs, canon) -> list[Seg]:
    """One sweep: the longest segment of each independent pure-track cycle.
    Cycles touching a segment already picked this sweep are deferred to the
    next fixpoint iteration."""
    parent: dict = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    adj: dict = {}
    victims: list[Seg] = []
    tainted: set[int] = set()
    for s in sorted(segs, key=lambda s: (s.uuid, s.layer, s.a, s.b)):
        ka = canon(_node(s.layer, s.a))
        kb = canon(_node(s.layer, s.b))
        ra, rb = find(ka), find(kb)
        closes = ra == rb
        if not closes:
            parent[ra] = rb
        if closes:
            path = _bfs_path(adj, ka, kb)
            if path is None:  # connected through a dropped edge only
                continue
            cycle = path + [s]
            if any(id(e) in tainted for e in cycle):
                continue
            with_uuid = [e for e in cycle if e.uuid]
            if not with_uuid:
                continue
            victim = max(with_uuid, key=lambda e: (e.length, e.uuid))
            victims.append(victim)
            tainted.add(id(victim))
            if victim is s:
                continue  # s dropped; keep it out of the adjacency
        adj.setdefault(ka, []).append((kb, s))
        adj.setdefault(kb, []).append((ka, s))
    return victims


def find_loops(segs, vias, cap=LOOP_CAP) -> tuple[list[str], int]:
    """Break pure track-segment cycles (vias merge nodes across layers; pads
    and zone fill never form edges). Returns (uuids removed, loops broken)."""
    alive = list(segs)
    canon = _via_canon(vias)
    removed: list[str] = []
    loops = 0
    for _ in range(cap):
        victims = _loop_sweep(alive, canon)
        if not victims:
            break
        ids = {id(v) for v in victims}
        alive = [s for s in alive if id(s) not in ids]
        removed += sorted(v.uuid for v in victims)
        loops += len(victims)
    return removed, loops


def loop_bridge_veto(bg: geom.BoardGeom, victims: list[Seg]
                     ) -> tuple[list[str], list[str]]:
    """Split loop victims into (removable_uuids, vetoed_uuids).

    A victim whose netconn edge is a BRIDGE (cut edge) of the net's FULL
    copper connectivity graph (tracks + vias + pads + zones) is load-bearing
    by definition - the loop model disagreed with real connectivity, so the
    removal is vetoed (T6/V13 guard; the veto can only prevent removals).
    Victims that cannot be matched to a netconn edge are vetoed too (never
    remove what cannot be verified)."""
    removable: list[str] = []
    vetoed: list[str] = []
    by_net: dict[str, list[Seg]] = {}
    for s in victims:
        by_net.setdefault(s.net, []).append(s)
    for net in sorted(by_net):
        g = netconn.build(bg, net, include_zones=True)
        bridges = netconn.bridge_tracks(g)
        tracks = bg.tracks_of(net)

        def edge_of(s: Seg):
            for i, t in enumerate(tracks):
                if t.layer != s.layer or len(t.shape.coords) != 2:
                    continue
                c0 = t.shape.coords[0]
                c1 = t.shape.coords[-1]
                if ((math.dist(c0, s.a) <= 1e-3 and math.dist(c1, s.b) <= 1e-3)
                        or (math.dist(c0, s.b) <= 1e-3
                            and math.dist(c1, s.a) <= 1e-3)):
                    return i
            return None

        for s in by_net[net]:
            eid = edge_of(s)
            if eid is None or eid in bridges:
                vetoed.append(s.uuid)
            else:
                removable.append(s.uuid)
    return sorted(removable), sorted(vetoed)


# ============================================================ pass 3: snap

@dataclass
class SnapEnv:
    """What a snapped leg must clear. obstacles: layer -> [(net, geom, uuid)]
    of every track, via, pad and drill on that layer (zone fill excluded:
    the refill moves it; a drill's uuid is DRILL). clearance: net -> netclass/DRU clearance; floor: the
    board minimum any pair keeps; edge: copper-to-edge rule (None = no
    outline check); keepouts: [(layers, polygon)] areas tracks may not enter.
    hard / hard_floor: the clearance KiCad's DRC actually enforces, when the
    .kicad_dru has a clearance rule (hard_floor = the largest of them, scoped
    ones included) - a custom rule overrides
    the netclass value, so a board can pass DRC with gaps under its netclass
    clearance (None: DRC enforces the netclass model, same as clr).
    hole: the board's min_hole_clearance, kept from every foreign drill in
    both models.
    """
    obstacles: dict
    clearance: dict
    floor: float
    edge: float | None = None
    outline: object = None
    keepouts: tuple = ()
    hard: dict | None = None
    hard_floor: float = 0.0
    hole: float = KICAD_HOLE_CLEARANCE

    def clr(self, a: str, b: str) -> float:
        return max(self.floor, self.clearance.get(a, 0.0),
                   self.clearance.get(b, 0.0))

    def clr_hard(self, a: str, b: str) -> float:
        if self.hard is None:
            return self.clr(a, b)
        return max(self.hard_floor, self.hard.get(a, 0.0),
                   self.hard.get(b, 0.0))


def snap_paths(a, b) -> list[tuple[str, list[tuple[float, float]]]]:
    """The 0/90 + 45 routes from a to b as [(name, interior points)]: the
    two one-bend doglegs (diagonal-first, axis-first), then the two Z routes
    that put the bend run in the middle (axis_z: axis-45-axis, diagonal_z:
    45-axis-45). A Z swings half as far off the a-b line as a dogleg, so it
    fits a gap a dogleg does not, at the cost of one more leg."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    m = min(abs(dx), abs(dy))
    d = (math.copysign(m, dx), math.copysign(m, dy))   # the 45 run
    r = (dx - d[0], dy - d[1])                         # the 0/90 run

    def at(*steps):
        x, y = a
        out = []
        for sx, sy in steps:
            x, y = x + sx, y + sy
            out.append((round(x, 6), round(y, 6)))
        return out

    hd, hr = (d[0] / 2, d[1] / 2), (r[0] / 2, r[1] / 2)
    return [("diagonal_first", at(d)), ("axis_first", at(r)),
            ("axis_z", at(hr, d)), ("diagonal_z", at(hd, r))]


def _tee_joints(s: Seg, segs, vias, pads, arcs, tol=TOL) -> list:
    """Where same-net copper joins s away from both end caps, ordered from a
    to b ([] when nothing does). KiCad joins two items when an anchor of one
    lies inside the other's copper: track and arc ends, via centres, pad
    centres. So a joint is such an anchor within half-width + tol of s's
    centreline and more than half-width from both ends; the snapped legs bend
    through it. Copper that only overlaps s, with no anchor on it, is not
    joined through s and does not stop the snap."""
    half = s.width / 2.0
    reach = half + tol
    cands: list = []
    for o in segs:
        if o is not s and o.net == s.net and o.layer == s.layer:
            cands += [o.a, o.b]
    for net, layer, aline, _aw in arcs:
        if net == s.net and layer == s.layer:
            cands += [aline.coords[0], aline.coords[-1]]
    cands += [v.at for v in vias if v.net == s.net and s.layer in v.layers]
    cands += [pd.center for pd in pads
              if pd.net == s.net and s.layer in pd.layers]
    joints = {}
    for p in cands:
        p = (float(p[0]), float(p[1]))
        if _pt_seg_dist(p, s.a, s.b) <= reach \
                and min(math.dist(p, s.a), math.dist(p, s.b)) > half:
            joints[crs._key(p)] = p
    return sorted(joints.values(), key=lambda p: math.dist(s.a, p))


def _path_margin(s: Seg, pts, env: SnapEnv, trees: dict, added: list,
                 gone: set) -> tuple[float, str, float, dict]:
    """(spare clearance of the route s.a -> pts -> s.b in mm against the
    netclass/DRU model, what limits it, spare against what DRC enforces,
    the netclass spare per foreign net). A negative spare breaks that rule;
    -inf means the route leaves the board or enters a keepout. A drill
    keeps max(rule, hole clearance). The per-net map keys the board edge as
    EDGE. `gone`: uuids of tracks already replaced this run."""
    legs = LineString([s.a, *pts, s.b])
    half = s.width / 2.0
    worst, why, hard = math.inf, "", math.inf
    per: dict[str, float] = {}
    if env.outline is not None and env.edge is not None:
        if not env.outline.covers(legs):
            return -math.inf, "board edge", -math.inf, {EDGE: -math.inf}
        worst = hard = env.outline.boundary.distance(legs) - (
            half + env.edge + SNAP_MARGIN)
        why = "board edge"
        per[EDGE] = worst
    for layers, poly in env.keepouts:
        if s.layer in layers and poly.intersects(legs):
            return -math.inf, "keepout", -math.inf, {EDGE: -math.inf}
    tree, items = trees.get(s.layer, (None, []))
    reach = half + max([env.floor, env.hole]
                       + list(env.clearance.values())) \
        + SNAP_MARGIN + SNAP_PROBE
    near = tree.query(legs.buffer(reach)) if tree is not None else ()
    cands = [items[i] for i in near if items[i][2] not in gone]
    cands += [(net, g, "") for layer, net, g in added if layer == s.layer]
    for net, geom_, uid in cands:
        if net == s.net:
            continue
        dist = geom_.distance(legs) - half - SNAP_MARGIN
        hole = env.hole if uid == DRILL else 0.0
        gap = dist - max(env.clr(s.net, net), hole)
        name = net or "<no net>"
        if gap < worst:
            worst, why = gap, name
        per[name] = min(per.get(name, math.inf), gap)
        hard = min(hard, dist - max(env.clr_hard(s.net, net), hole))
    return worst, why, hard, per


EDGE = "<board edge>"


def _legal(m: float, hard: float, per: dict, before: dict) -> bool:
    """A route is legal when it keeps the full netclass/DRU clearance, or -
    where the router already left the copper it replaces closer than that -
    when it still keeps what DRC enforces and comes no closer to EACH
    foreign net (and the edge) under the netclass than the replaced copper
    did. `per` / `before`: _path_margin's per-net spares of the route and of
    the replaced copper; a net absent from `before` was out of reach."""
    if m >= 0:
        return True
    if hard < 0:
        return False
    return all(g >= 0 or g >= before.get(k, 0.0) - 1e-6
               for k, g in per.items())


def _chain(a, pts, b) -> list:
    """a, pts..., b without zero-length steps."""
    out = [tuple(a)]
    for p in [*pts, tuple(b)]:
        if math.dist(out[-1], p) > 1e-6:
            out.append(tuple(p))
    return out


def find_snaps(segs, vias, pads, arcs, env: SnapEnv,
               skip_nets=frozenset(), cap: int = 5, dejog: bool = False
               ) -> tuple[list[dict], list[dict], list[dict], int]:
    """Plans for off-angle segments -> (ops, snaps, left, jogs_merged).

    Each off-angle segment (cut at its tee joints, if any) gets the legal
    route that makes the fewest needless jogs, then has the most spare
    clearance - a dogleg when one fits, else a Z. Sweeps until no blocked
    segment frees up (a foreign segment replaced in one sweep no longer
    blocks in the next), cap sweeps. Then every needless jog that has a
    snapped leg in it is merged: its three segments become one dogleg (or
    one straight) between the outer ends when that is legal and leaves the
    net with fewer jogs; with `dejog`, every needless jog on a net not in
    `skip_nets` is, snapped leg or not. The ops are the difference between the board and
    the final copper: removes for replaced segments, add_track for legs."""
    from shapely.strtree import STRtree  # noqa: PLC0415
    trees = {}
    for layer, items in env.obstacles.items():
        items = list(items)
        trees[layer] = (STRtree([g for _n, g, _u in items]) if items
                        else None, items)
    added: list[tuple] = []   # (layer, net, copper) of accepted legs
    gone: set[str] = set()    # uuids replaced so far
    snaps: list[dict] = []
    left: dict[str, dict] = {}
    todo = []
    for s in sorted(segs, key=lambda s: (s.uuid, s.layer, s.a, s.b)):
        if not s.uuid or not crs.off_angle(s.a, s.b):
            continue
        item = {"uuid": s.uuid, "net": s.net, "layer": s.layer,
                "pos": _r4(((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2))}
        if s.net in skip_nets:
            left[s.uuid] = {**item, "reason": "kept_net"}
        else:
            stops = [s.a, *_tee_joints(s, segs, vias, pads, arcs), s.b]
            todo.append((s, item, list(zip(stops, stops[1:]))))
    # live copper per (net, layer): {id: (a, b, width, id)}; ids are the
    # board uuid (or id() for a uuid-less track) and "<uuid>:<n>" for legs
    live: dict[tuple, dict] = {}
    for s in segs:
        k = s.uuid or id(s)
        live.setdefault((s.net, s.layer), {})[k] = (s.a, s.b, s.width, k)
    board = {s.uuid for s in segs if s.uuid}
    legs: dict[str, tuple] = {}    # leg id -> (net, layer)
    anchors: dict[tuple, list] = {}
    for v in vias:
        for layer in v.layers:
            anchors.setdefault((v.net, layer), []).append(
                Point(v.at).buffer(v.size / 2.0))
    for pd in pads:
        for layer in pd.layers:
            anchors.setdefault((pd.net, layer), []).append(pd.poly)

    def jogs_made(s: Seg, path) -> int:
        """Needless jogs the legs of `path` form with the tracks at its ends."""
        ends = {crs._key(path[0]), crs._key(path[-1])}
        local = [t for k, t in live.get((s.net, s.layer), {}).items()
                 if k != s.uuid and (crs._key(t[0]) in ends
                                     or crs._key(t[1]) in ends)]
        local += [(p, q, s.width, f"leg{i}")
                  for i, (p, q) in enumerate(zip(path, path[1:]))]
        return len(crs.find_jogs(local, anchors.get((s.net, s.layer), [])))

    def best_route(s: Seg, a, b):
        """(name, path, spare) of the best legal route a -> b, or (None,
        limiting item, None)."""
        piece = Seg(s.uuid, s.net, s.layer, s.width, a, b)
        if not crs.off_angle(a, b):
            return "kept", [tuple(a), tuple(b)], math.inf
        limit = ""
        before = _path_margin(piece, [], env, trees, added, gone)[3]
        routes = snap_paths(a, b)
        for tier in (routes[:2], routes[2:]):
            best = None
            for name, pts in tier:
                m, why, hard, per = _path_margin(piece, pts, env, trees,
                                                 added, gone)
                if not _legal(m, hard, per, before):
                    limit = limit or why
                    continue
                path = _chain(a, pts, b)
                rank = (-jogs_made(s, path), m)
                if best is None or rank > best[0]:
                    best = (rank, name, path)
            if best is not None:
                return best[1], best[2], best[0][1]
        return None, limit, None

    def put(key, base: str, path, width) -> None:
        cur = live.setdefault(key, {})
        for p, q in zip(path, path[1:]):
            lid = f"{base}:{len(legs) + 1}"
            legs[lid] = key
            cur[lid] = (p, q, width, lid)
        added.append((key[1], key[0], LineString(path).buffer(
            width / 2.0, quad_segs=8)))

    for _ in range(cap):
        progress = False
        for s, item, pieces in todo:
            if s.uuid in gone:
                continue
            routes, limit = [], ""
            for a, b in pieces:
                name, path, spare = best_route(s, a, b)
                if name is None:
                    limit = path
                    break
                routes.append((name, path, spare))
            if len(routes) < len(pieces):
                left[s.uuid] = {**item, "reason": "blocked", "by": limit}
                continue
            left.pop(s.uuid, None)
            gone.add(s.uuid)
            progress = True
            key = (s.net, s.layer)
            live[key].pop(s.uuid, None)
            for _name, path, _spare in routes:
                put(key, s.uuid, path, s.width)
            spare = min(r[2] for r in routes)
            snaps.append({
                **item, "bend": "+".join(r[0] for r in routes),
                "path": [_r4(p) for r in routes for p in r[1][:-1]]
                + [_r4(s.b)],
                "spare_mm": round(spare, 4) if math.isfinite(spare)
                else None})
        if not progress:
            break
    every = (sorted((k for k in live if k[0] not in skip_nets), key=str)
             if dejog else None)
    merged = _merge_jogs(live, legs, board, gone, anchors, env, trees,
                         added, vias, pads, arcs, put, keys=every)
    ops = [{"op": "remove", "uuid": u} for u in sorted(gone & board)]
    for (net, layer), cur in sorted(live.items(), key=lambda kv: kv[0]):
        for lid in sorted(k for k in cur if k in legs):
            a, b, w, _k = cur[lid]
            ops.append({"op": "add_track", "start": _r6(a), "end": _r6(b),
                        "width": round(w, 4), "layer": layer, "net": net})
    return ops, snaps, sorted(left.values(), key=lambda x: x["uuid"]), merged


def _merge_jogs(live, legs, board, gone, anchors, env, trees, added,
                vias, pads, arcs, put, cap: int = 50, keys=None) -> int:
    """Merge the needless jogs (check_route_style.find_jogs) that have a
    snapped leg in them, or every one on the (net, layer) `keys` when
    given (--dejog): s1-s2-s3 through two bare joints becomes one route
    from s1's far end to s3's far end - straight when that is on the grid,
    else the dogleg with the most spare clearance - when all three are the
    same width, none has another track joining mid-length, the route is
    legal, and the net ends with fewer jogs. Returns how many merged."""
    merged = 0
    every = keys is not None
    if keys is None:
        keys = sorted({k for k in legs.values()}, key=str)
    for key in keys:
        net, layer = key
        anch = anchors.get(key, [])
        # a merge takes out at least one segment, so a net's own length
        # bounds the merges --dejog can make on it
        for _ in range(max(cap, len(live[key])) if every else cap):
            cur = live[key]
            vals = list(cur.values())
            jogs = crs.find_jogs(vals, anch)
            done = False
            for _pos, _side, mid in jogs:
                a, b, w, _ = cur[mid]
                ka, kb = crs._key(a), crs._key(b)
                i1 = next(k for k, t in cur.items() if k != mid
                          and ka in (crs._key(t[0]), crs._key(t[1])))
                i3 = next(k for k, t in cur.items() if k != mid
                          and kb in (crs._key(t[0]), crs._key(t[1])))
                trio = (i1, mid, i3)
                if not (every or any(k in legs for k in trio)) or any(
                        k not in legs and k not in board for k in trio):
                    continue
                if len({round(cur[k][2], 4) for k in trio}) != 1:
                    continue
                t1, t3 = cur[i1], cur[i3]
                p0 = t1[0] if crs._key(t1[1]) == ka else t1[1]
                p3 = t3[1] if crs._key(t3[0]) == kb else t3[0]
                segs_now = [Seg(str(k), net, layer, t[2], t[0], t[1])
                            for k, t in cur.items()]
                names = {str(k) for k in trio}
                if any(_tee_joints(sg, segs_now, vias, pads, arcs) != []
                       for sg in segs_now if sg.uuid in names):
                    continue
                pseudo = Seg("", net, layer, w, p0, p3)
                before = _path_margin(pseudo, [a, b], env, trees, added,
                                      gone)[3]
                cands = ([("straight", [])] if not crs.off_angle(p0, p3)
                         else snap_paths(p0, p3)[:2])
                best = None
                for _name, pts in cands:
                    m, _why, hard, per = _path_margin(pseudo, pts, env,
                                                      trees, added, gone)
                    if not _legal(m, hard, per, before):
                        continue
                    path = _chain(p0, pts, p3)
                    trial = [t for k, t in cur.items() if k not in trio]
                    trial += [(p, q, w, f"t{i}")
                              for i, (p, q) in enumerate(zip(path, path[1:]))]
                    n = len(crs.find_jogs(trial, anch))
                    if n < len(jogs) and (best is None
                                          or (n, -m) < best[0]):
                        best = ((n, -m), path)
                if best is None:
                    continue
                for k in trio:
                    del cur[k]
                    if k in board:
                        gone.add(k)
                base = next((str(k).split(":")[0] for k in trio
                             if k in legs), str(mid))
                put(key, base, best[1], w)
                merged += 1
                done = True
                break
            if not done:
                break
    return merged


def _snap_legs(ops):
    """(layer, net, copper) of every add_track in a snap op list."""
    return [(op["layer"], op["net"],
             LineString([op["start"], op["end"]]).buffer(op["width"] / 2.0,
                                                         quad_segs=8))
            for op in ops if op["op"] == "add_track"]


def _rules_clearance(pcb: Path, nets) -> tuple[dict, float, float,
                                                dict | None, float, float]:
    """(per-net clearance, board floor, copper-to-edge, hard per-net, hard
    floor, hole clearance) from the .kicad_pro netclasses and the .kicad_dru
    beside the board. hard is None unless the .kicad_dru carries a
    clearance rule: KiCad then enforces such rules instead of the netclass
    clearance. hard_floor is the LARGEST clearance min over all clearance
    rules, conditioned, layer-scoped or per-net included: what DRC enforces
    depends on the pair there, so the fallback takes the conservative
    maximum and a scoped rule can only make it stricter. Hole
    clearance: the board setup's min_hole_clearance (KiCad's 0.25 mm when
    unset), raised by any .kicad_dru hole_clearance rule."""
    import json  # noqa: PLC0415
    import route_critical as rc  # noqa: PLC0415 - heavy, lazy
    pro, dru = pcb.with_suffix(".kicad_pro"), pcb.with_suffix(".kicad_dru")
    floor, edge = KICAD_CLEARANCE, KICAD_EDGE
    try:
        proj = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        proj = {}
    rules = ((proj.get("board") or {}).get("design_settings") or {}).get(
        "rules") or {}
    for c in (proj.get("net_settings") or {}).get("classes") or []:
        if c.get("name") == "Default" and \
                isinstance(c.get("clearance"), (int, float)):
            floor = float(c["clearance"])
    if isinstance(rules.get("min_clearance"), (int, float)):
        floor = max(floor, float(rules["min_clearance"]))
    if isinstance(rules.get("min_copper_edge_clearance"), (int, float)):
        edge = float(rules["min_copper_edge_clearance"])
    hole = KICAD_HOLE_CLEARANCE
    if isinstance(rules.get("min_hole_clearance"), (int, float)):
        hole = float(rules["min_hole_clearance"])
    try:
        text = dru.read_text(encoding="utf-8")
    except OSError:
        text = ""
    hard, hard_floor = None, 0.0
    for r in rc.parse_dru_rules(text):
        if r["constraint"] == "hole_clearance":
            hole = max(hole, r["min_mm"])
        if r["constraint"] == "clearance":
            # conservative: the largest clearance of ANY rule, scoped or
            # not, so a conditioned rule can only make the fallback stricter
            hard_floor = max(hard_floor, r["min_mm"])
            hard = {}
        if r["nets"]:
            continue
        if r["constraint"] == "clearance":
            floor = max(floor, r["min_mm"])
        elif r["constraint"] == "edge_clearance":
            edge = max(edge, r["min_mm"])
    if hard is not None and isinstance(rules.get("min_clearance"),
                                       (int, float)):
        hard_floor = max(hard_floor, float(rules["min_clearance"]))
    per_net = rc.build_net_clearances(pro if pro.is_file() else None,
                                      dru, nets) or {}
    return per_net, floor, edge, hard, hard_floor, hole


def snap_env(bg: geom.BoardGeom) -> SnapEnv:
    """The SnapEnv of a parsed board."""
    obstacles: dict = {l: [] for l in bg.copper_layers}
    for t in bg.tracks_of():
        obstacles[t.layer].append((t.net or "", t.poly, t.uuid or ""))
    for v in bg.vias_of():
        hole = Point(v.at).buffer(v.drill / 2.0, quad_segs=8) \
            if v.drill > 0 else None
        for layer in bg.copper_layers:
            if v.spans(layer):
                obstacles[layer].append((v.net or "", v.poly, ""))
                if hole is not None:
                    obstacles[layer].append((v.net or "", hole, DRILL))
    for p in bg.pads_of():
        for layer in bg.copper_layers:
            if p.on(layer):
                obstacles[layer].append((p.net or "", p.poly, ""))
            if not p.drill_poly.is_empty:
                obstacles[layer].append((p.net or "", p.drill_poly, DRILL))
    per_net, floor, edge, hard, hard_floor, hole_clr = _rules_clearance(
        bg.path, sorted(bg.nets))
    keepouts = tuple(
        (tuple(ra["layers"]), ra["outline"]) for ra in bg.rule_areas
        if ra.get("flags", {}).get("tracks") == "not_allowed"
        and not ra["outline"].is_empty)
    outline = bg.outline if bg.outline is not None \
        and not bg.outline.is_empty else None
    return SnapEnv(obstacles, per_net, floor, edge if outline else None,
                   outline, keepouts, hard, hard_floor, hole_clr)


# ============================================================ pass 4: corners

def _r6(p) -> list[float]:
    return [round(p[0], 6), round(p[1], 6)]


def _r4(p) -> list[float]:
    return [round(p[0], 4), round(p[1], 4)]


def find_corners(segs, vias, pads, foreign_fn, tol=TOL,
                 attach=()) -> tuple[list[dict], list[dict]]:
    """45-deg chamfer plans for clean ~90-deg corners. foreign_fn(layer, net)
    -> copper of every OTHER net on that layer. attach: tracks planned by an
    earlier pass (snap legs); never chamfered, but they still join a corner,
    so an elbow they land on is a tee and stays. Returns (ops, corners)."""
    ends: dict = {}
    for s in sorted(segs, key=lambda s: s.uuid):
        if not s.uuid or not s.net:
            continue
        ends.setdefault(_node(s.layer, s.a), []).append((s, 0))
        ends.setdefault(_node(s.layer, s.b), []).append((s, 1))
    for s in attach:
        for k in (_node(s.layer, s.a), _node(s.layer, s.b)):
            if k in ends:
                ends[k].append((s, None))
    ops: list[dict] = []
    corners: list[dict] = []
    consumed: set[str] = set()
    for key in sorted(ends):
        pair = ends[key]
        if len(pair) != 2 or any(e is None for _, e in pair):
            continue  # only clean 2-segment elbows are smoothable
        (s1, e1), (s2, e2) = pair
        if (s1 is s2 or s1.net != s2.net
                or abs(s1.width - s2.width) > 1e-3
                or s1.uuid in consumed or s2.uuid in consumed):
            continue
        w = s1.width
        c1, f1 = ((s1.a, s1.b)[e1], (s1.a, s1.b)[1 - e1])
        c2, f2 = ((s2.a, s2.b)[e2], (s2.a, s2.b)[1 - e2])
        v1 = (f1[0] - c1[0], f1[1] - c1[1])
        v2 = (f2[0] - c2[0], f2[1] - c2[1])
        la, lb = math.hypot(*v1), math.hypot(*v2)
        if min(la, lb) < SMOOTH_MIN_LEG * w:
            continue
        cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (la * lb)
        ang = math.degrees(math.acos(max(-1.0, min(1.0, cosang))))
        if not (88.0 <= ang <= 92.0):
            continue
        c = min(min(la, lb) / 3.0, 2.0 * w, SMOOTH_MAX_C)
        corner = c1
        # corners at pad centers / vias stay
        if any(math.dist(corner, pd.center) <= PAD_VIA_KEEPOUT
               for pd in pads):
            continue
        if any(math.dist(corner, v.at)
               <= max(PAD_VIA_KEEPOUT, c + v.size / 2.0 + tol)
               for v in vias):
            continue
        # nothing else may attach inside the region the chamfer removes
        if any(s1.layer in pd.layers
               and pd.poly.distance(Point(corner)) <= c + tol
               for pd in pads):
            continue
        if any(o is not s1 and o is not s2
               and o.net == s1.net and o.layer == s1.layer
               and _pt_seg_dist(corner, o.a, o.b) <= c + tol
               for o in (*segs, *attach)):
            continue
        p1 = (c1[0] + v1[0] / la * c, c1[1] + v1[1] / la * c)
        p2 = (c2[0] + v2[0] / lb * c, c2[1] + v2[1] / lb * c)
        corridor = LineString([p1, p2]).buffer(w / 2.0 + SMOOTH_CLEAR,
                                               quad_segs=8)
        foreign = foreign_fn(s1.layer, s1.net)
        if (foreign is not None and not foreign.is_empty
                and corridor.intersects(foreign)):
            continue
        if any(s1.layer in pd.layers and corridor.intersects(pd.poly)
               for pd in pads):  # pad copper of ANY net blocks the chamfer
            continue
        consumed.update((s1.uuid, s2.uuid))
        wid = round(w, 4)
        ops += [
            {"op": "remove", "uuid": s1.uuid},
            {"op": "remove", "uuid": s2.uuid},
            {"op": "add_track", "start": _r4(f1), "end": _r4(p1),
             "width": wid, "layer": s1.layer, "net": s1.net},
            {"op": "add_track", "start": _r4(f2), "end": _r4(p2),
             "width": wid, "layer": s1.layer, "net": s1.net},
            {"op": "add_track", "start": _r4(p1), "end": _r4(p2),
             "width": wid, "layer": s1.layer, "net": s1.net},
        ]
        corners.append({"corner": _r4(corner), "layer": s1.layer,
                        "net": s1.net, "chamfer_mm": round(c, 4),
                        "uuids": [s1.uuid, s2.uuid]})
    return ops, corners


# ============================================================ plan + driver

def build_plan(bg: geom.BoardGeom, segs, vias, smooth: bool = True,
               snap: bool = True, hygiene: bool = True,
               keep_nets=(), discover: bool = True,
               dejog: bool = False) -> dict:
    """All four passes over one parse -> {ops, op_layers, facts...}.
    hygiene=False (--snap-only) skips dangling, loops and corners.
    discover=False leaves name-paired nets (X_P/X_N, A+/A-) to the snap
    too: constraints that declare diff_pairs, even as [], name them all.
    dejog=True has pass 3 merge every needless jog, not only snapped ones."""
    pads = [PadItem(p.net, tuple(p.layers), p.center, p.poly)
            for p in bg.pads_of()]
    arcs = tuple((t.net, t.layer, t.shape, t.width) for t in bg.tracks_of()
                 if len(t.shape.coords) > 2)  # arcs anchor, never removed
    cache: dict = {}

    def fillfn(net, layer):
        key = (net, layer)
        if key not in cache:
            cache[key] = bg.zone_fill(net, layer)
        return cache[key]

    gone_s, gone_v = (find_dangling(segs, vias, pads, fillfn, arcs)
                      if hygiene else ([], []))
    dead = set(gone_s) | set(gone_v)
    alive_s = [s for s in segs if s.uuid not in dead]
    alive_v = [v for v in vias if v.uuid not in dead]
    loop_uuids, _loops = (find_loops(alive_s, alive_v) if hygiene
                          else ([], 0))
    # T6/V13 guard: a victim that is a bridge of the net's FULL connectivity
    # graph is load-bearing - veto its removal (the loop stays, warning-level
    # outcome; the veto can only PREVENT copper loss).
    victim_segs = [s for s in alive_s if s.uuid in set(loop_uuids)]
    loop_uuids, loop_vetoed = loop_bridge_veto(bg, victim_segs)
    dead |= set(loop_uuids)
    alive_s = [s for s in alive_s if s.uuid not in dead]
    # a broken loop can orphan a stub: one more dangling sweep on the
    # remainder (counted separately - orphaned_after_loops)
    orphan_s: list[str] = []
    orphan_v: list[str] = []
    if loop_uuids:
        orphan_s, orphan_v = find_dangling(alive_s, alive_v, pads, fillfn,
                                           arcs)
        dead |= set(orphan_s) | set(orphan_v)
        alive_s = [s for s in alive_s if s.uuid not in dead]
        alive_v = [v for v in alive_v if v.uuid not in dead]
    snap_ops: list[dict] = []
    snaps: list[dict] = []
    left: list[dict] = []
    merged = 0
    env = None
    if snap:
        import check_diffpair  # noqa: PLC0415 - lazy
        found = check_diffpair.discover_pairs(bg.nets) if discover else []
        skip = {n for pair in found for n in pair} | set(keep_nets)
        env = snap_env(bg)
        snap_ops, snaps, left, merged = find_snaps(
            alive_s, alive_v, pads, arcs, env, frozenset(skip),
            dejog=dejog)
    corner_ops: list[dict] = []
    corners: list[dict] = []
    if smooth and hygiene:
        # every track the snap replaced (snapped, or merged into a jog fix)
        snapped = {o["uuid"] for o in snap_ops if o["op"] == "remove"}

        def foreign(layer, net):
            # snapped legs of other nets are foreign copper for a chamfer
            legs = [g for (l, n, g) in _snap_legs(snap_ops) if l == layer
                    and n != net]
            base = bg.layer_copper(layer, exclude=net)
            return geom._union([base] + legs) if legs else base

        planned = [Seg("", o["net"], o["layer"], o["width"],
                       tuple(o["start"]), tuple(o["end"]))
                   for o in snap_ops if o["op"] == "add_track"]
        corner_ops, corners = find_corners(
            [s for s in alive_s if s.uuid not in snapped], alive_v, pads,
            foreign, attach=planned)
    ops = ([{"op": "remove", "uuid": u} for u in gone_s]
           + [{"op": "remove", "uuid": u} for u in gone_v]
           + [{"op": "remove", "uuid": u} for u in loop_uuids]
           + [{"op": "remove", "uuid": u} for u in orphan_s]
           + [{"op": "remove", "uuid": u} for u in orphan_v]
           + snap_ops + corner_ops)
    layer_of = {s.uuid: (s.layer,) for s in segs}
    layer_of.update({v.uuid: v.layers for v in vias})
    op_layers: set[str] = set()
    for op in ops:
        if op["op"] == "remove":
            op_layers.update(layer_of.get(op["uuid"], ()))
        else:
            op_layers.add(op["layer"])
    return {
        "ops": ops, "op_layers": op_layers,
        "dangling_segments": len(gone_s), "dangling_vias": len(gone_v),
        "dangling_removed": len(gone_s) + len(gone_v),
        "loops_broken": len(loop_uuids), "loop_bridge_vetoed": len(loop_vetoed),
        "off_angle_snapped": len(snaps), "snaps": snaps,
        "off_angle_left": left, "jogs_merged": merged,
        "orphaned_after_loops": len(orphan_s) + len(orphan_v),
        "corners_smoothed": len(corners),
        "corners": corners,
    }


def _length_group_nets(e: dict, board_nets) -> set[str]:
    """Every net a length_groups entry names (members, references, caps):
    globs are matched against the board's nets when given, else taken as
    exact names."""
    pats = []
    for key in ("nets", "ref_nets", "not_longer_than"):
        v = e.get(key) or []
        pats.extend([v] if isinstance(v, str) else v)
    if e.get("ref_net"):
        pats.append(e["ref_net"])
    pats = [p for p in pats if isinstance(p, str) and p]
    if board_nets is None:
        return set(pats)
    import check_length_groups  # noqa: PLC0415 - lazy
    return set(check_length_groups.match_nets(pats, board_nets)[0])


def constraint_keep_nets(cons: dict, board_nets=None) -> set[str]:
    """Nets constraints.json gives intended geometry, which the snap must
    leave: both members of every declared diff pair (check_diffpair takes
    diff_pairs as authoritative), every net of a length_match group
    ({"nets": [...]}) and of a length_groups entry (globs resolved against
    board_nets when given), every rf entry and every high_speed entry carrying an
    impedance_ohm target (route_critical routes both at impedance width)."""
    def entries(key):
        return [e for e in (cons.get(key) or []) if isinstance(e, dict)]
    out: set[str] = set()
    for e in entries("diff_pairs"):
        out.update(n for n in (e.get("p"), e.get("n")) if n)
    for e in entries("length_match"):
        out.update(n for n in (e.get("nets") or []) if isinstance(n, str) and n)
    for e in entries("length_groups"):
        out.update(_length_group_nets(e, board_nets))
    for e in entries("rf"):
        if e.get("net"):
            out.add(e["net"])
    for e in entries("high_speed"):
        if e.get("net") and e.get("impedance_ohm") is not None:
            out.add(e["net"])
    return out


def _drc_facts(report: dict) -> dict:
    counts = report["counts"]
    return {
        "unconnected": counts["by_source"].get("unconnected", 0),
        "errors": counts["by_severity"].get("error", 0),
        "warnings": counts["by_severity"].get("warning", 0),
        "dangling_flagged": sum(
            1 for v in report["violations"]
            if v.get("check") in ("track_dangling", "via_dangling")),
    }


def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="generate + report ops only; board untouched, "
                         "no toolchain needed")
    ap.add_argument("--no-smooth", action="store_true",
                    help="skip pass 4 (corner smoothing)")
    ap.add_argument("--no-snap", action="store_true",
                    help="skip pass 3 (off-angle segments to 0/90 + 45)")
    ap.add_argument("--snap-only", action="store_true",
                    help="run pass 3 alone (no dangling/loop/corner edits)")
    ap.add_argument("--dejog", action="store_true",
                    help="pass 3 also merges every needless jog on the "
                         "board (check_route_style), not only the ones a "
                         "snap made")
    ap.add_argument("--keep-net", action="append", default=[],
                    metavar="NET",
                    help="leave this net's off-angle segments (an RF or "
                         "length-matched net); repeatable")
    ap.add_argument("--constraints", default=None,
                    help="constraints.json: also leave its diff_pairs, "
                         "length_match, rf and impedance-controlled "
                         "high_speed nets")
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)
    if args.snap_only and args.no_snap:
        raise CheckError("--snap-only and --no-snap exclude each other")
    if args.dejog and args.no_snap:
        raise CheckError("--dejog is part of pass 3: drop --no-snap")

    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    bg = geom.BoardGeom.from_file(pcb)
    # dangling detection reads zone fills as legal terminations - a stale/
    # unfilled pour would make live pour-terminated stubs look dangling
    # (S11 review finding; plane_repair has the same guard).
    bg.assert_fresh()
    keep = set(args.keep_net)
    discover = True
    if args.constraints:
        cons = checklib.load_json(args.constraints, "constraints")
        if not isinstance(cons, dict):
            raise CheckError(f"constraints {args.constraints} is not an "
                             "object")
        keep |= constraint_keep_nets(cons, bg.nets)
        # a declared diff_pairs list is the whole set: speaker A_SPK_L+/-
        # are not a pair just because their names end in +/-
        discover = not isinstance(cons.get("diff_pairs"), list)
    segs, vias = parse_items(pcb, bg.copper_layers)
    plan = build_plan(bg, segs, vias, smooth=not args.no_smooth,
                      snap=not args.no_snap, hygiene=not args.snap_only,
                      dejog=args.dejog,
                      keep_nets=sorted(keep), discover=discover)
    facts = {k: plan[k] for k in (
        "dangling_removed", "dangling_segments", "dangling_vias",
        "loops_broken", "loop_bridge_vetoed", "orphaned_after_loops",
        "off_angle_snapped", "snaps", "off_angle_left", "jogs_merged",
        "corners_smoothed", "corners")}

    if args.dry_run:
        payload = checklib.report(
            "route_cleanup", pcb, [], dry_run=True, ops_applied=0,
            refilled=False, drc_before=None, drc_after=None,
            ops=plan["ops"], **facts)
        return payload, args.out_report

    cli = kc.resolve_cli()
    before = kc.run_drc(cli, pcb, all_track_errors=True)
    b = _drc_facts(before)
    applied = 0
    refilled = False
    if plan["ops"]:
        route_edit.apply_ops(pcb, plan["ops"])
        applied = len(plan["ops"])
        fill_layers = {l for z in bg.zones_of()
                       for l, polys in z.fills.items() if polys}
        refilled = bool(fill_layers & plan["op_layers"])
        try:
            after = kc.run_drc(cli, pcb, all_track_errors=True,
                               refill=refilled, save_board=refilled)
        except Exception as exc:  # noqa: BLE001
            raise CheckError(
                "cleanup ops were APPLIED but the refill/DRC step failed - "
                "the board HAS been modified; re-run 'kicad-cli pcb drc "
                "--refill-zones --save-board' and re-check. "
                f"Cause: {exc}") from exc
        a = _drc_facts(after)
    else:
        a = b

    violations = []
    if a["unconnected"] > b["unconnected"] or a["errors"] > b["errors"]:
        violations.append(checklib.violation(
            "cleanup_regression", "error", None, None, None, [],
            f"cleanup DEGRADED the board: unconnected {b['unconnected']}"
            f"->{a['unconnected']}, DRC errors {b['errors']}->{a['errors']}."
            " The board file HAS been modified - restore it from git before"
            " continuing.", "route_cleanup",
            kind="cleanup_regression", drc_before=b, drc_after=a))
    payload = checklib.report(
        "route_cleanup", pcb, violations, dry_run=False, ops_applied=applied,
        refilled=refilled, drc_before=b, drc_after=a, ops=plan["ops"],
        **facts)
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("route_cleanup", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
