"""netconn.py - per-net copper connectivity graph (tracks + vias + pads + zones).

Provenance: LEARNINGS.md 2026-07-29 [check_diffpair][gates]. An endpoint-snap-
only track graph ignores vias and pad copper, so sub-0.3 mm endpoint mismatches
(machine-measured 0.1414 / 0.2229 / 0.2530 mm on lumina-carrier's MDI pairs)
split a net into components and silently turn "trunk skew" into total-copper-
length. Joined here per the machine-verified KiCad connectivity rules
(LEARNINGS 2026-07-30 [kicad][connectivity]): an endpoint landing inside a VIA
or PAD is connected; two overlapping same-layer round END CAPS are connected
(DRC and netlist were clean on all three lumina cases); an endpoint inside
another track's BODY is NOT connected and is deliberately not joined here.

Shared by check_diffpair (trunk/skew) and check_current (bridge = cut-edge
detection, LEARNINGS 2026-07-29 [check_current][gates] "no bridge awareness").

Node ids (hashable tuples):
    ("pt", (x, y))         track endpoint snapped to `snap` decimals
    ("via", i)             i-th via of the net (bg.vias_of(net) order)
    ("pad", ref, number)   a Pad's copper
    ("zone", j)            j-th zone-fill polygon (include_zones=True only)

Edges: every track segment contributes one edge (edge_id = its index in
bg.tracks_of(net), recorded in NetGraph.tracks); join edges carry distinct
("join", k) ids so bridge finding can tell parallel edges apart, and are
absent from NetGraph.tracks - bridge_tracks() reports SEGMENT edges only.
"""
from __future__ import annotations

import heapq
import itertools
import math

from shapely.geometry import Point

# Default connect slop (mm): covers float dust between "touching" copper items
# without bridging real DRC-visible gaps (smallest corpus clearance 0.1 mm).
TOL = 0.05
SNAP = 3            # decimal places for endpoint-node identity (geom convention)


class NetGraph:
    """Undirected weighted multigraph of one net's copper connectivity.

    adj:    {node: [(other_node, weight_mm, edge_id), ...]}
    tracks: {edge_id: Track} for segment edges (join edges are absent)
    """

    def __init__(self):
        self.adj: dict = {}
        self.tracks: dict = {}

    @property
    def nodes(self):
        return self.adj.keys()

    def add_node(self, n) -> None:
        self.adj.setdefault(n, [])

    def add_edge(self, a, b, weight: float, edge_id) -> None:
        self.adj.setdefault(a, []).append((b, weight, edge_id))
        self.adj.setdefault(b, []).append((a, weight, edge_id))


def _snap(pt, snap: int):
    return (round(pt[0], snap), round(pt[1], snap))


def build(bg, net: str, include_zones: bool = False, snap: int = SNAP,
          tol: float = TOL) -> NetGraph:
    """Connectivity graph of `net`. Join rules (all weights in mm):

    a) coincident snap: endpoints rounding to the same (x, y) are ONE node,
       layer-agnostic (preserves the pre-netconn behavior where exactly
       coincident cross-layer endpoints connect).
    b) same-layer cap overlap: endpoints of two DIFFERENT tracks on the SAME
       layer within (w1 + w2)/2 + tol are joined (round end caps overlap;
       weight = endpoint distance), except that an end in reach of both end
       nodes of one track (its own included) drops the farther: a T-tap
       reaching both caps of a short trunk joins the nearer only (#1336, no
       false loop beside the trunk). Two ends join only when each keeps the
       other's node.
    c) via join: an endpoint within via.diameter/2 + tol of the via center,
       with the via spanning the track's layer (weight = distance to center).
    d) pad join: an endpoint inside pad.poly buffered by tol, with the
       track's layer in pad.layers (weight = distance to pad.center). A via
       whose centre is inside the pad and that spans a pad layer joins the
       pad too: a via-in-pad has no track on the pad's layer (#546).
    e) include_zones=True: each zone-fill polygon becomes a ("zone", j) node
       joined at weight 0 to any endpoint within tol of the fill (same layer)
       and any via within its own radius of the fill (via must span the fill
       layer). check_current passes this so a pour-paralleled segment is not
       called a bridge; check_diffpair does not.
    """
    g = NetGraph()
    tracks = bg.tracks_of(net)
    vias = bg.vias_of(net)
    pads = bg.pads_of(net=net)
    join_ids = itertools.count()
    joined: set = set()          # {frozenset({a, b})} - dedupe join edges only

    def join(a, b, w: float) -> None:
        if a == b:
            return
        key = frozenset((a, b))
        if key in joined:
            return
        joined.add(key)
        g.add_edge(a, b, w, ("join", next(join_ids)))

    # segment edges + endpoint inventory: (node, raw_pt, layer, half_w, track_i)
    ends: list[tuple] = []
    for i, t in enumerate(tracks):
        if t.length <= 0:
            continue                       # zero-length segment: no edge
        p0, p1 = t.shape.coords[0], t.shape.coords[-1]
        a = ("pt", _snap(p0, snap))
        b = ("pt", _snap(p1, snap))
        g.add_edge(a, b, t.length, i)
        g.tracks[i] = t
        ends.append((a, (p0[0], p0[1]), t.layer, t.width / 2.0, i))
        ends.append((b, (p1[0], p1[1]), t.layer, t.width / 2.0, i))

    # b) same-layer cap overlap, via a uniform grid (cell >= max join reach)
    if ends:
        cell = max(max(2.0 * hw for _, _, _, hw, _ in ends) + tol, 0.1)
        grid: dict[tuple[int, int], list[int]] = {}
        # end k -> {node: distance} of the ends in cap reach of it, its own
        # node at 0. A short track can have both its end nodes in reach of
        # one end (a T-tap on its body, #1336): joining both makes a loop
        # beside the track that isn't in the copper, also when other tracks
        # carry those nodes on. So of any track's two end nodes in reach,
        # the farther is dropped, and two ends join only when each keeps
        # the other's node.
        reach: list[dict] = [{e[0]: 0.0} for e in ends]
        pairs: list[tuple[int, int, float]] = []
        for k, (_, pt, _, _, _) in enumerate(ends):
            grid.setdefault((int(pt[0] // cell), int(pt[1] // cell)), []).append(k)
        for (cx, cy), members in grid.items():
            neigh = [k for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                     for k in grid.get((cx + dx, cy + dy), ())]
            for ka in members:
                na, pa, la, ha, ta = ends[ka]
                for kb in neigh:
                    if kb <= ka:
                        continue
                    nb, pb, lb, hb, tb = ends[kb]
                    if ta == tb or la != lb:
                        continue
                    d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                    if d <= ha + hb + tol:
                        pairs.append((ka, kb, d))
                        for k, o in ((ka, nb), (kb, na)):
                            if d < reach[k].get(o, math.inf):
                                reach[k][o] = d
        span = {(ends[k][0], ends[k + 1][0]) for k in range(0, len(ends), 2)}
        kept = []
        for r in reach:
            drop = {(x if r[x] > r[y] else y) for x, y in span
                    if x != y and x in r and y in r}
            kept.append(r.keys() - drop)
        for ka, kb, d in pairs:
            if ends[kb][0] in kept[ka] and ends[ka][0] in kept[kb]:
                join(ends[ka][0], ends[kb][0], d)

    # c) via joins
    for vi, v in enumerate(vias):
        vnode = ("via", vi)
        g.add_node(vnode)
        reach = v.diameter / 2.0 + tol
        for node, pt, layer, _, _ in ends:
            if not v.spans(layer):
                continue
            d = math.hypot(pt[0] - v.at[0], pt[1] - v.at[1])
            if d <= reach:
                join(node, vnode, d)

    # d) pad joins
    for p in pads:
        pnode = ("pad", p.ref, p.number)
        g.add_node(pnode)
        poly = p.poly
        minx, miny, maxx, maxy = poly.bounds
        for node, pt, layer, _, _ in ends:
            if layer not in p.layers:
                continue
            if not (minx - tol <= pt[0] <= maxx + tol
                    and miny - tol <= pt[1] <= maxy + tol):
                continue
            if poly.distance(Point(pt)) <= tol:
                join(node, pnode,
                     math.hypot(pt[0] - p.center[0], pt[1] - p.center[1]))
        for vi, v in enumerate(vias):
            if any(v.spans(ly) for ly in p.layers) \
                    and poly.distance(Point(v.at)) <= tol:
                join(("via", vi), pnode,
                     math.hypot(v.at[0] - p.center[0], v.at[1] - p.center[1]))

    # e) zone-fill joins
    if include_zones:
        j = 0
        for z in bg.zones_of(net):
            for layer, polys in z.fills.items():
                for poly in polys:
                    znode = ("zone", j)
                    j += 1
                    g.add_node(znode)
                    for node, pt, elayer, _, _ in ends:
                        if elayer == layer and poly.distance(Point(pt)) <= tol:
                            join(node, znode, 0.0)
                    for vi, v in enumerate(vias):
                        if (v.spans(layer)
                                and poly.distance(Point(v.at)) <= v.diameter / 2.0):
                            join(("via", vi), znode, 0.0)
    return g


def shortest_path_len(g: NetGraph, a, b) -> float | None:
    """Dijkstra a -> b over edge weights; None if either node is absent or
    unreachable. Heap entries carry a counter so mixed-type nodes never get
    compared on distance ties."""
    if a is None or b is None or a not in g.adj or b not in g.adj:
        return None
    if a == b:
        return 0.0
    tie = itertools.count()
    dist = {a: 0.0}
    pq = [(0.0, next(tie), a)]
    while pq:
        d, _, u = heapq.heappop(pq)
        if u == b:
            return d
        if d > dist.get(u, math.inf):
            continue
        for v, w, _eid in g.adj[u]:
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(pq, (nd, next(tie), v))
    return dist.get(b)


def shortest_path(g: NetGraph, a, b):
    """Dijkstra a -> b returning (length, [(u, v, weight, edge_id), ...]) in
    path order, or None when either node is absent or b is unreachable. Same
    weights as shortest_path_len, so the two agree on length."""
    if a is None or b is None or a not in g.adj or b not in g.adj:
        return None
    tie = itertools.count()
    dist = {a: 0.0}
    prev: dict = {}
    pq = [(0.0, next(tie), a)]
    while pq:
        d, _, u = heapq.heappop(pq)
        if u == b:
            break
        if d > dist.get(u, math.inf):
            continue
        for v, w, eid in g.adj[u]:
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = (u, w, eid)
                heapq.heappush(pq, (nd, next(tie), v))
    if b not in dist:
        return None
    steps = []
    node = b
    while node != a:
        u, w, eid = prev[node]
        steps.append((u, node, w, eid))
        node = u
    steps.reverse()
    return dist[b], steps


def pad_node(g: NetGraph, pad):
    """The graph node for a Pad object, or None if the pad is not in this
    graph (pad on another net, or graph built for a different net)."""
    n = ("pad", pad.ref, pad.number)
    return n if n in g.adj else None


def bridge_tracks(g: NetGraph) -> set:
    """Edge_ids of SEGMENT edges that are bridges (cut edges) of the graph.

    Tarjan low-link on the multigraph, iterative (nets can chain hundreds of
    segments). Multi-edges are told apart by edge_id - only the specific edge
    used to enter a node is skipped, so a parallel edge between the same node
    pair acts as a back edge and neither copy is a bridge. Self-loops are
    never bridges. Join edges are excluded from the result by construction
    (their ids are not in g.tracks)."""
    disc: dict = {}
    low: dict = {}
    bridges: set = set()
    timer = 0
    for root in g.adj:
        if root in disc:
            continue
        disc[root] = low[root] = timer
        timer += 1
        stack = [(root, None, iter(g.adj[root]))]
        while stack:
            u, in_eid, it = stack[-1]
            pushed = False
            for v, _w, eid in it:
                if v == u or eid == in_eid:
                    continue             # self-loop / the edge we arrived by
                if v in disc:
                    if disc[v] < low[u]:
                        low[u] = disc[v]
                else:
                    disc[v] = low[v] = timer
                    timer += 1
                    stack.append((v, eid, iter(g.adj[v])))
                    pushed = True
                    break
            if not pushed:
                stack.pop()
                if stack:
                    pu = stack[-1][0]
                    if low[u] < low[pu]:
                        low[pu] = low[u]
                    if low[u] > disc[pu] and in_eid in g.tracks:
                        bridges.add(in_eid)
    return bridges
