"""mazeroute - routes between named ends on route_finish's grid and search.

route_finish joins the islands DRC reports unconnected. A dense board also
needs routes nobody reported yet: from this pad to that point, three nets
through one channel together, a net through another net's stub. These were
scratch scripts on the voice board (#926 #927 #928); they are now this
library, and route_edit's `route`, `route_group` and `reroute` ops and
zone_patch's corridor mode call it. The obstacles, rules, grid and the C
search are route_finish's, so a route here keeps the clearances its routes
keep.

An END names where a route starts or stops:
    "U1.3"            pad 3 of U1, on its own copper layers
    "U1"              every pad of U1 on the route's net
    "12.5,3.2@F.Cu"   a point on a layer; "12.5,3.2" on every routable layer
    "U1.3|U1.4"       any one of several (a list of strings works too)

Router.route(net, a, b)          one route, as route_edit ops
Router.group(jobs)               PathFinder: every job routed together, the
                                 cells two nets want priced up each round
                                 until none is shared (#928)
Router.reroute(net, region, ...) rip other signal nets' copper in a box,
                                 route the net, route the ripped nets back,
                                 in another order when that fails; no
                                 ripped net may end with more islands than it
                                 had (#927)
Router.corridor(net, a, b, hw)   the route as a polygon, for a pour (#926)
"""
from __future__ import annotations

import math
import random
import sys
from array import array
from pathlib import Path

import numpy as np
import shapely
import shapely.ops
from scipy import ndimage
from shapely.geometry import Point, box

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import route_finish as rf  # noqa: E402
from checklib import CheckError  # noqa: E402

GRID = 0.05          # mm: fine enough for a 0.4 mm pitch pad's 0.2 mm gap
MARGIN = 3.0         # mm: the window past both ends
VIA_COST_MM = 1.5    # a via costs this much track
GROUP_ITERS = 30     # PathFinder rounds before it gives up
PRES_START = 1.0    # PathFinder: the price of a cell another net uses...
PRES_GROW = 1.5     # ...grows this much each round
HIST_STEP = 0.5     # and a cell shared at the end of a round keeps this
REROUTE_TRIES = 6    # orders reroute tries before it gives up


def _side_union(side: dict):
    return shapely.union_all([p for ps in side.values() for p in ps])


def _merge(*sides: dict) -> dict:
    out: dict = {}
    for sd in sides:
        for layer, ps in sd.items():
            out.setdefault(layer, []).extend(ps)
    return out


class Router:
    """Routes on one board, each route an obstacle to the next."""

    def __init__(self, bg, *, rules=None, layers=None, grid: float = GRID,
                 margin: float = MARGIN, via_cost_mm: float = VIA_COST_MM,
                 max_nodes: int = 3_000_000):
        self.bg = bg
        self.rules = rules or rf.board_rules(Path(bg.path), bg)
        self.layers = list(layers) if layers else rf.routable_layers(bg)
        bad = [x for x in self.layers if x not in bg.copper_layers]
        if bad:
            raise CheckError(f"not copper layers of this board: {bad}")
        self.obs = rf.Obstacles(bg)
        self.g = grid
        self.margin = margin
        self.via_cost_mm = via_cost_mm
        self.max_nodes = max_nodes
        self._conn = 0
        # power, pour and rule-width nets are never ripped (route_finish's
        # keep_nets): their copper is sized by rule
        self.keep_nets = ({z.net for z in bg.zones_of()}
                          | set(self.rules.net_width) | {""})

    # ------------------------------------------------------------ ends

    def end(self, spec, net: str) -> dict:
        """An END (see the module doc) as a side: {layer: [polygons]}."""
        parts = spec if isinstance(spec, (list, tuple)) else str(spec).split("|")
        side: dict = {}
        for raw in parts:
            s = str(raw).strip()
            if not s:
                continue
            if "," in s:
                xy, _, layer = s.partition("@")
                try:
                    x, y = (float(v) for v in xy.split(","))
                except ValueError as e:
                    raise CheckError(f"end {s!r}: want x,y[@layer]") from e
                on = [layer] if layer else self.layers
                for ly in on:
                    if ly not in self.layers:
                        raise CheckError(f"end {s!r}: {ly} is not a routable"
                                         f" layer ({', '.join(self.layers)})")
                    side.setdefault(ly, []).append(
                        Point(x, y).buffer(self.g * 0.75, quad_segs=4))
                continue
            ref, _, num = s.partition(".")
            pads = [p for p in self.bg.pads_of(ref=ref)
                    if not num or p.number == num]
            if not pads:
                raise CheckError(f"end {s!r}: no such pad on the board")
            if num and pads[0].net != net:
                raise CheckError(f"end {s!r} is on net {pads[0].net!r},"
                                 f" not {net!r}")
            pads = [p for p in pads if p.net == net]
            if not pads:
                raise CheckError(f"end {s!r}: no pad of {ref} on {net!r}")
            for p in pads:
                on = [ly for ly in p.layers if ly in self.layers]
                if not on:
                    raise CheckError(f"end {s!r}: the pad is on no routable"
                                     f" layer ({', '.join(self.layers)})")
                for ly in on:
                    side.setdefault(ly, []).append(p.poly)
        if not side:
            raise CheckError(f"end {spec!r} names nothing")
        return side

    # ---------------------------------------------------------- search

    def window(self, a: dict, b: dict, margin: float):
        x0, y0, x1, y1 = shapely.union_all(
            [_side_union(a), _side_union(b)]).bounds
        return (x0 - margin, y0 - margin, x1 + margin, y1 + margin)

    def grid(self, net, a, b, win, width=None):
        return rf.build_grid(self.obs, self.rules, net, a, b, self.layers,
                             win, self.g, width=width)

    @staticmethod
    def target(gr, a: dict, b: dict):
        """The cell of side B nearest side A: the search's estimate aims
        there."""
        _, pb = shapely.ops.nearest_points(_side_union(a), _side_union(b))
        return (min(gr.nx - 1, max(0, int(round((pb.x - gr.x0) / gr.g)))),
                min(gr.ny - 1, max(0, int(round((pb.y - gr.y0) / gr.g)))))

    def search(self, gr, a, b):
        """A path on the grid, with too-close vias searched away the way
        route_finish does, or None."""
        if rf.no_path(gr):
            return None
        tgt = self.target(gr, a, b)
        for _ in range(rf.VIA_RETRIES + 1):
            path = rf.astar(gr, tgt, via_cost=self.via_cost_mm / gr.g,
                            max_nodes=self.max_nodes)
            near = path and rf.too_close_via(gr, path, self.rules)
            if not near:
                return path
            k, c = near
            gr.blk[k] = gr.blk[k][:c] + b"\1" + gr.blk[k][c + 1:]
        return None

    def find(self, net, a, b, width=None):
        """(ops, path, grid) for a route from side a to side b over a
        widening window, or None."""
        w = width or self.rules.width(net)
        for m in (self.margin, self.margin * 2.5):
            gr = self.grid(net, a, b, self.window(a, b, m), w)
            path = self.search(gr, a, b)
            if path:
                return rf.path_to_ops(gr, path, net, w, self.rules), path, gr
        return None

    def commit(self, ops: list) -> None:
        """Later routes keep clear of these."""
        self._conn += 1
        self.obs.added.extend(rf.ops_items(ops, self._conn,
                                           self.bg.copper_layers))

    def route(self, net, a, b, width=None, pull=False) -> list:
        """One route from END a to END b, committed, as route_edit ops.
        pull: straighten the grid's staircase (pulled_ops)."""
        sa, sb = self.end(a, net), self.end(b, net)
        hit = self.find(net, sa, sb, width)
        if hit is None:
            raise CheckError(f"route {net} {a} -> {b}: no path on "
                             f"{', '.join(self.layers)} at the board's rules")
        ops = hit[0]
        if pull:
            ops = pulled_ops(hit[2], hit[1], ops,
                             width or self.rules.width(net))
        self.commit(ops)
        return ops

    def corridor(self, net, a, b, hw: float, width=None):
        """The route from a to b as a polygon: its centreline buffered by
        hw, and its vias' pads. zone_patch pours it (#926)."""
        sa, sb = self.end(a, net), self.end(b, net)
        hit = self.find(net, sa, sb, width or 2 * hw)
        if hit is None:
            raise CheckError(f"corridor {net} {a} -> {b}: no path")
        ops = hit[0]
        by_layer: dict = {}
        for op in ops:
            if op["op"] == "add_track":
                by_layer.setdefault(op["layer"], []).append(
                    shapely.LineString([op["start"], op["end"]]).buffer(hw))
        vias = [Point(op["at"]).buffer(op["size"] / 2) for op in ops
                if op["op"] == "add_via"]
        return ({ly: shapely.union_all(ps + vias) for ly, ps in by_layer.items()},
                ops)

    # --------------------------------------------------------- islands

    def islands(self, net: str) -> list[dict]:
        """The net's copper as it stands in this router: the board's, less
        what is ripped (obs.hidden), plus what is routed (obs.added), as
        [{layer: [polygons]}]. Each island also lists its pads' count under
        the key None."""
        cu = list(self.bg.copper_layers)
        pieces: list[tuple[frozenset, object, bool]] = []
        for it in self.obs.items + self.obs.added:
            if it.net != net or (it.uuid and it.uuid in self.obs.hidden):
                continue
            shape = it.shape.buffer(it.half, quad_segs=4) if it.half \
                else it.shape
            if it.hole is not None:
                shape = shapely.union(shape, it.hole)
            ly = frozenset(it.layers) & frozenset(cu)
            if ly and not shape.is_empty:
                is_pad = it.uuid is None and it.conn < 0 \
                    and it.shape.geom_type == "Polygon" and not it.tied
                pieces.append((ly, shape, is_pad))
        for znet, layer, poly in self.obs.fills:
            if znet == net:
                pieces.append((frozenset({layer}), poly, False))
        parent = list(range(len(pieces)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        tree = shapely.STRtree([p[1] for p in pieces]) if pieces else None
        for i, (ly, shape, _) in enumerate(pieces):
            for j in tree.query(shape, predicate="intersects"):
                j = int(j)
                if j > i and ly & pieces[j][0]:
                    parent[find(j)] = find(i)
        groups: dict[int, dict] = {}
        for i, (ly, shape, is_pad) in enumerate(pieces):
            g = groups.setdefault(find(i), {None: 0})
            g[None] += is_pad
            for layer in ly:
                g.setdefault(layer, []).append(shape)
        return list(groups.values())

    def join_islands(self, net: str, want: int, near=None,
                     width=None) -> list:
        """Route between the net's islands until it has at most `want`,
        nearest pair first (to `near`'s centre when given); committed ops.
        Islands without a pad join too: a ripped track leaves stubs that
        would hang dead otherwise."""
        ops: list = []
        for _ in range(64):
            isl = self.islands(net)
            if len(isl) <= want:
                return ops
            sides = [{k: v for k, v in i.items() if k is not None}
                     for i in isl]
            polys = [_side_union(s) for s in sides]
            best = None
            for i in range(len(isl)):
                for j in range(i + 1, len(isl)):
                    d = polys[i].distance(polys[j])
                    if near is not None:
                        d += polys[i].distance(near) + polys[j].distance(near)
                    if best is None or d < best[0]:
                        best = (d, i, j)
            _, i, j = best
            others = _merge(*[s for k, s in enumerate(sides) if k != i])
            hit = self.find(net, sides[i], others, width)
            if hit is None:
                hit = self.find(net, sides[j], _merge(
                    *[s for k, s in enumerate(sides) if k != j]), width)
            if hit is None:
                raise CheckError(f"{net}: {len(isl)} islands, no path joins"
                                 f" them on {', '.join(self.layers)}")
            self.commit(hit[0])
            ops += hit[0]
        raise CheckError(f"{net}: islands won't join")

    # --------------------------------------------------------- reroute

    def rippable(self, it, net: str, keep=()) -> bool:
        """A board track or via reroute may take up: another signal net's,
        at no more than its rule width. Power and pour nets, nets with a
        width rule, `keep`, and copper drawn wider than its net's width
        (someone sized it) stay."""
        from check_testpoints import is_power

        if not it.uuid or it.uuid in self.obs.hidden or it.net == net \
                or it.net in self.keep_nets or it.net in keep \
                or is_power(it.net):
            return False
        return it.hole is not None \
            or 2 * it.half <= self.rules.width(it.net) + 1e-3

    def ripped_by(self, region, net: str, keep=()) -> list:
        """Board tracks reroute may rip whose centreline crosses the box,
        and such vias with their centre in it."""
        win = box(*region)
        out = []
        for it in self.obs.items:
            if not self.rippable(it, net, keep):
                continue
            if it.hole is not None:          # a via: its centre
                if win.contains(it.shape) or win.touches(it.shape):
                    out.append(it)
            elif it.shape.intersects(win):
                out.append(it)
        return out

    def reroute(self, net, region, a=None, b=None, *, tries=REROUTE_TRIES,
                width=None, keep=(), seed: int = 0
                ) -> tuple[list, list, dict]:
        """Rip other signal nets' copper in region, route net (from END a to
        END b, or join its islands near the region), then route each ripped
        net back to as few islands as it had; in shuffled orders until one
        works (#927). Returns (removed uuids, added ops, {net: most islands
        it may end with}), with the router's state at that order's end."""
        before = {net: len(self.islands(net))}
        rip = self.ripped_by(region, net, keep)
        if not rip:
            raise CheckError(
                f"reroute {net}: nothing in {list(region)} may be ripped"
                " (power, pour and width-rule nets, keep, and copper wider"
                " than its net's width stay)")
        victims = list(dict.fromkeys(it.net for it in rip))
        for v in victims:
            before[v] = len(self.islands(v))
        uuids = [it.uuid for it in rip]
        held_hidden, held_added, held_conn = set(self.obs.hidden), \
            list(self.obs.added), self._conn
        rng = random.Random(seed)
        order = list(victims)
        why = []
        near = box(*region)
        for t in range(max(1, tries)):
            self.obs.hidden = held_hidden | set(uuids)
            self.obs.added = list(held_added)
            self._conn = held_conn
            ops: list = []
            try:
                if a is not None and b is not None:
                    ops += self.route(net, a, b, width)
                else:
                    ops += self.join_islands(net, before[net] - 1, near,
                                             width)
                for v in order:
                    ops += self.join_islands(v, before[v], near)
                return uuids, ops, {**{v: before[v] for v in victims},
                                    net: max(1, before[net] - 1)}
            except CheckError as e:
                why.append(f"order {t + 1} ({', '.join(order) or '-'}): {e}")
                rng.shuffle(order)
        self.obs.hidden, self.obs.added, self._conn = \
            held_hidden, held_added, held_conn
        raise CheckError(f"reroute {net} in {list(region)}: no order routes"
                         f" it and every ripped net back: " + "; ".join(why))

    # ----------------------------------------------------------- group

    def group(self, jobs: list[dict], *, iterations: int = GROUP_ITERS,
              log=None) -> list:
        """PathFinder (#928): route every job ({net, from, to, [width]})
        on one shared grid; a cell another job's route claims costs more
        each round, and a cell shared at a round's end keeps a history
        price, until no two routes share a cell. Committed ops, all jobs;
        CheckError (with the rounds' best) when no round is clean."""
        if not jobs:
            return []
        prepared = []
        for jb in jobs:
            net = jb["net"]
            sa, sb = self.end(jb["from"], net), self.end(jb["to"], net)
            w = jb.get("width") or self.rules.width(net)
            prepared.append((net, sa, sb, w))
        bounds = shapely.union_all(
            [shapely.union_all([_side_union(a), _side_union(b)])
             for _, a, b, _ in prepared]).bounds
        win = (bounds[0] - self.margin, bounds[1] - self.margin,
               bounds[2] + self.margin, bounds[3] + self.margin)
        grids = [self.grid(net, a, b, win, w) for net, a, b, w in prepared]
        gr0 = grids[0]
        K, ny, nx = len(self.layers), gr0.ny, gr0.nx
        base = [[np.frombuffer(c, np.float64).reshape(ny, nx).copy()
                 for c in gr.cost] for gr in grids]
        hist = [np.zeros((ny, nx)) for _ in range(K)]
        g = self.g
        # how far apart two routes' centres must be, in cells: each claims
        # a disk of its own width plus the clearance; a via a disk of its
        # pad's
        reach = []
        for net, _, _, w in prepared:
            c = self.rules.clr(net, net)
            reach.append((max(1.0, (w + c) / g),
                          max(1.0, (gr0.via[0] / 2 + w / 2 + c) / g)))

        def disk(d):
            """Cells nearer than d cells: two centres exactly d apart
            are legal."""
            r = int(math.ceil(d))
            yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
            return xx * xx + yy * yy < d * d - 1e-6

        def claims(path, n):
            """Per layer, the cells n's route keeps other routes out of."""
            r, rv = reach[n]
            line = [np.zeros((ny, nx), bool) for _ in range(K)]
            via = np.zeros((ny, nx), bool)
            for (i, j, k), nxt in zip(path, path[1:] + [None]):
                if k >= 0:
                    line[k][j, i] = True
                if k < 0 or (nxt is not None and nxt[2] != k):
                    via[j, i] = True
            out = [ndimage.binary_dilation(m, disk(r)) if m.any() else m
                   for m in line]
            if via.any():
                vm = ndimage.binary_dilation(via, disk(rv))
                out = [o | vm for o in out]
            return out

        def own(path):
            cells = [np.zeros((ny, nx), bool) for _ in range(K)]
            for i, j, k in path:
                for kk in (range(K) if k < 0 else [k]):
                    cells[kk][j, i] = True
            return cells

        paths: list = [None] * len(prepared)
        claim: list = [None] * len(prepared)
        best = None
        pres = PRES_START
        for it in range(max(1, iterations)):
            for n, (net, a, b, w) in enumerate(prepared):
                occ = [np.zeros((ny, nx)) for _ in range(K)]
                for m, cl in enumerate(claim):
                    if m != n and cl is not None:
                        for k in range(K):
                            occ[k] += cl[k]
                gr = grids[n]
                gr.cost = [array("d", (base[n][k] * (1 + hist[k])
                                       * (1 + pres * occ[k])).tobytes())
                           for k in range(K)]
                path = self.search(gr, a, b)
                if path is None:
                    raise CheckError(f"route_group {net}: no path even with"
                                     " the other nets ignored")
                paths[n] = path
                claim[n] = claims(path, n)
            shared = 0
            for n, path in enumerate(paths):
                mine = own(path)
                for m, cl in enumerate(claim):
                    if m == n:
                        continue
                    for k in range(K):
                        hit = mine[k] & cl[k]
                        if hit.any():
                            shared += int(hit.sum())
                            hist[k] += HIST_STEP * hit
            if log:
                log(f"route_group round {it + 1}: {shared} shared cells")
            if best is None or shared < best[0]:
                best = (shared, list(paths), it + 1)
            if shared == 0:
                break
            pres *= PRES_GROW
        shared, paths, rounds = best
        if shared:
            raise CheckError(
                f"route_group: {len(jobs)} nets still share {shared} cells"
                f" after {iterations} rounds (best round {rounds})")
        ops: list = []
        for n, (net, _, _, w) in enumerate(prepared):
            job_ops = rf.path_to_ops(grids[n], paths[n], net, w, self.rules)
            self.commit(job_ops)
            ops += job_ops
        self.rounds = rounds
        return ops


# --------------------------------------------------------- string pulling

def _sight(gr, k: int, c0, c1) -> bool:
    """True when a track centre may run straight from cell c0 to cell c1 on
    layer k: every cell round each sample along the way (a quarter cell
    apart) is free in the search grid, which already holds the width and
    clearance."""
    blk = gr.blk[k]
    n = max(1, int(math.ceil(4 * math.hypot(c1[0] - c0[0], c1[1] - c0[1]))))
    for s in range(n + 1):
        x = c0[0] + (c1[0] - c0[0]) * s / n
        y = c0[1] + (c1[1] - c0[1]) * s / n
        for i in {math.floor(x), math.ceil(x)}:
            for j in {math.floor(y), math.ceil(y)}:
                if not (0 <= i < gr.nx and 0 <= j < gr.ny) \
                        or blk[j * gr.nx + i]:
                    return False
    return True


def pulled_ops(gr, path, ops: list, width: float) -> list:
    """ops with each layer run's tracks redrawn as the fewest straight
    pieces the grid allows (#1042). A grid path that runs off the eight
    directions is a staircase of steps shorter than the track is wide; their
    end caps overlap, so check_current's netconn sees a loop where there is
    one line and can no longer call it a leaf branch. Run ends stay where
    they were (the vias and the pads), so ops' vias are kept as they are.
    A straight stretch of the path is always one piece. A run through a
    neck cell (gr.neck) is left alone."""
    neck = getattr(gr, "neck", None) or {}
    runs: list[list] = []
    for q in path:
        if q[2] == -1:
            runs.append([])
            continue
        if runs and runs[-1] and runs[-1][-1][2] == q[2]:
            runs[-1].append(q)
        else:
            runs.append([q])
    tracks = [op for op in ops if op["op"] == "add_track"]
    if not tracks:
        return ops
    net = tracks[0]["net"]
    out = []
    for run in (r for r in runs if len(r) > 1):
        k = run[0][2]
        if any((k, q[1] * gr.nx + q[0]) in neck for q in run):
            return ops
        step = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(run, run[1:])]
        pts = [run[0]]
        i = 0
        while i < len(run) - 1:
            j = i + 1
            # a stretch the path itself runs straight along is clear by
            # construction; _sight's rounding may not see that hugging a
            # keepout edge
            while j + 1 < len(run) and (
                    all(d == step[i] for d in step[i:j + 1])
                    or _sight(gr, k, run[i], run[j + 1])):
                j += 1
            pts.append(run[j])
            i = j
        # the greedy pass bends where it first loses sight; a bend whose
        # neighbours see each other is not needed
        m = 1
        while m < len(pts) - 1:
            if _sight(gr, k, pts[m - 1], pts[m + 1]):
                del pts[m]
                m = max(1, m - 1)
            else:
                m += 1
        for q0, q1 in zip(pts, pts[1:]):
            if q0[:2] != q1[:2]:
                out.append({"op": "add_track", "start": list(gr.xy(*q0[:2])),
                            "end": list(gr.xy(*q1[:2])), "width": width,
                            "layer": gr.layers[k], "net": net})
    return out + [op for op in ops if op["op"] != "add_track"]
