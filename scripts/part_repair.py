"""part_repair - move, rotate or re-pad parts on a routed board and mend the copper round them.

  hwde part_repair --pcb B.kicad_pcb (--ops FILE | --op JSON ...) --out DIR
                   [--margin 2] [--keep NET ...] [--apply] [--out-report R]

The ops are place_edit's footprint ops (place, move, rotate, flip, swap_fp,
renumber). On a routed board each leaves copper on the wrong pads: a moved
part's tracks end in the air, and a re-padded part's tracks touch pads of
another net, which KiCad would carry onto that net when it saves. This tool
does what was done by hand round a part move (the haos-voice C216-C224 moves,
the Y301 re-pad): rip the copper the move spoils, make the move, route every
net back together, and keep the result only if the board got no worse.

  1. base: --pcb is copied to DIR/base, refilled and DRC'd. That DRC and each
     net's island count (route_finish.net_islands) are the yardstick.
  2. trial: the ops run on a scratch copy (DIR/trial) to learn where every
     pad lands and on which net.
  3. window: per part, the box round its old and new pads grown by --margin.
  4. rip: tracks and vias that touch an old pad and no new pad of their net,
     and other-net copper the new pads would short or come too close to
     (board_update.swap_clashes). Behind each goes the chain of track up to
     the first via, pad, junction, same-net pour or free end, or up to the
     first segment that leaves the window: that one stays.
  5. work: DIR/work gets the rips (route_edit), then the ops (place_edit,
     refilled). Then no track or via of another net may touch a pad of the
     parts (KiCad would carry it onto the pad's net when it saves), unless
     it touched one of their old pads on the base board too: a rip that
     missed something is an error here, whatever the op.
  6. join: every net the rips or the parts touch is routed back to as few
     islands as it had (lib/mazeroute.py), the parts' nets first: at the
     widest width ripped from it, then at its rule width, then letting the
     maze router push other signal nets' copper aside and put it back
     (Router.reroute), one window at a time: each window its islands meet.
     Then what the run left dangling goes, as KiCad's DRC would find it
     (track_dangling, via_dangling): from 1 mm round the copper the run
     added and removed, and the windows and --margin round the copper
     pushed aside, out along each dead chain to its end, wherever that
     is. A track with an end that meets nothing of its net, a via that
     nothing meets on two of its layers; pours count, teardrops do not.
     The router's own dead tails are trimmed from its routes the same way.
     What dangled on the base board stays. No removal splits a net: a
     dead end that is the only link between copper of its net is cut back
     instead, its dead end to the last copper it meets (a board track by
     set_point, keeping its uuid).
  7. check: the routes go in, the board is refilled and DRC'd. The repair
     area is the windows, --margin round the copper pushed aside, and 1 mm
     round the copper the run added or removed. The run passes when no net
     has more islands than before (the nets above, and every net with a
     pour that meets the repair area), no DRC check counts more errors or
     warnings than before, no net has more unconnected items, and no error
     or warning is new in the repair area: one that no base violation
     matches (by check, net and item positions, else by check and item
     uuids) and that lies, or has an item, in it. So a fix that clears one
     clearance error and makes another still fails.

In DIR on a pass: 01-rip.route_edit.json, 02-place.place_edit.json and
03-route.route_edit.json (prunes and pushes removed, then the new routes),
which replay on --pcb in this order:

  hwde route_edit --pcb B --ops DIR/01-rip.route_edit.json
  hwde place_edit --pcb B --ops DIR/02-place.place_edit.json --allow-routed
  hwde route_edit --pcb B --ops DIR/03-route.route_edit.json --refill

An op file with nothing in it is not written (and its step is skipped).
The last step written takes --refill, so the zones are filled as on
DIR/work/B: when there is no 03-route (every net still joined or tied in
by its pour), place_edit takes it.
--apply does all three at once: DIR/work/B replaces --pcb, on a pass only and
only if --pcb has not changed since the run began. Always in DIR: REPORT.md,
part_repair.json, and base/, trial/ and work/ with their boards. When DRC got
worse, both list the new violations on work/B (where, which net, the items'
uuids), so the next try knows what to rip or move. Zones are
refilled, not reshaped: a PATCH_<net> zone (zone_patch) whose fill met an
old pad and meets no new pad of its net is named in the report's notes.
The new vias have no teardrops (route_edit's add_via sets none), so a via
that is ripped and replaced loses its drops, and DRC does not see it.

Contract: --pcb is read only, except with --apply on a pass. Exit 0 pass,
1 violations (a net would not join, or the board got worse: --pcb untouched,
no op files), 2 error (part_repair.json and REPORT.md say which, once DIR is
made). part_repair.json's board_sha256 is --pcb's hash when the run began:
the op files replay on that board. With --apply, a --pcb that changed
during the run is not replaced (exit 2; the op files stay).
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import math
import os
import shlex
import shutil
import sys
from collections import Counter, deque
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import shapely  # noqa: E402
from shapely.geometry import LineString, Point, box  # noqa: E402

import board_update  # noqa: E402
import checklib  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import kc  # noqa: E402
import mazeroute  # noqa: E402
import place_edit  # noqa: E402
import placelib  # noqa: E402
import route_auto  # noqa: E402
import route_cleanup  # noqa: E402
import route_edit  # noqa: E402
import route_finish as rf  # noqa: E402
import route_improve  # noqa: E402
import routelib  # noqa: E402
from checklib import CheckError  # noqa: E402

MARGIN = 2.0   # mm round a part's old and new pads: the rip window
TOL = 1e-3     # mm
SHOWN = 40     # new violations listed in REPORT.md (all in part_repair.json)
TOUCH = 1e-3   # mm: copper this close is one piece (as net_islands has it)
NEAR = 0.05    # mm: a neighbour query's reach, past every rule's tol
REPRUNE = 3    # peels after a refill, at most (#1140)
CUT_MIN = 0.01  # mm: a dead end cut back leaves this much track, at least
LOST_NEAR = 1.0  # mm: how near what cut a pour back from copper is named
CUT_CAP = 4     # cuts of one track in one peel, at most
RIP_FILE = "01-rip.route_edit.json"
PLACE_FILE = "02-place.place_edit.json"
ROUTE_FILE = "03-route.route_edit.json"


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _ends(t) -> tuple:
    c = list(t.shape.coords)
    return tuple(c[0]), tuple(c[-1])


def _touches(it, pad) -> bool:
    if isinstance(it, geom.Track):
        return it.layer in pad.layers and it.poly.intersects(pad.poly)
    return any(it.spans(ly) for ly in pad.layers) \
        and it.poly.intersects(pad.poly)


def _kind(it) -> str:
    return "track" if isinstance(it, geom.Track) else "via"


def _r(v) -> float:
    return round(float(v), 4)


def windows(old: dict, new: dict, margin: float) -> dict:
    """ref -> the box round its old and new pads, grown by margin."""
    out = {}
    for ref in old:
        polys = [p.poly for p in old[ref] + new.get(ref, [])]
        if polys:
            x0, y0, x1, y1 = shapely.union_all(polys).bounds
            out[ref] = box(x0 - margin, y0 - margin, x1 + margin,
                           y1 + margin)
    return out


def seeds(bg, old: dict, new: dict) -> list:
    """Tracks and vias that touch an old pad and no new pad of their net."""
    out: dict = {}
    for ref, pads in old.items():
        for p in pads:
            for it in board_update._touching(bg, p):
                if not any(board_update._nets_equal(it.net, q.net)
                           and _touches(it, q) for q in new.get(ref, [])):
                    out[it.uuid] = it
    return list(out.values())


class Rip:
    """The copper to rip on the base board, chain by chain."""

    def __init__(self, bg, win, keep):
        self.bg, self.win, self.keep = bg, win, set(keep)
        self.tracks: dict = {}
        for t in bg.tracks_of():
            self.tracks.setdefault((t.net, t.layer), []).append(t)
        self.vias: dict = {}
        for v in bg.vias_of():
            self.vias.setdefault(v.net, []).append(v)
        self.pads: dict = {}
        for p in bg.pads_of():
            self.pads.setdefault(p.net, []).append(p)
        self._fills: dict = {}
        self.items: dict = {}       # uuid -> track or via, in rip order

    def _fill(self, net, layer):
        if (net, layer) not in self._fills:
            self._fills[(net, layer)] = self.bg.zone_fill(net, layer)
        return self._fills[(net, layer)]

    def _take(self, it, why: str) -> bool:
        if not it.uuid:
            raise CheckError(f"{why}: a {_kind(it)} of {it.net!r} has no"
                             " uuid, so no op can remove it")
        if it.net in self.keep:
            raise CheckError(f"{why}: {it.net} is --keep, but its {_kind(it)}"
                             f" {it.uuid[:8]} must go")
        if it.uuid in self.items:
            return False
        self.items[it.uuid] = it
        return True

    def next_track(self, cur, p):
        """The one track the chain from cur goes on to at its end p, or None
        where it stops: a via, a pad, the net's pour, a free end, a
        junction, a T onto a track's middle, an arc, or a segment that
        leaves the window (it stays)."""
        net, layer, half = cur.net, cur.layer, cur.width / 2
        pt = Point(p)
        for v in self.vias.get(net, []):
            if v.spans(layer) and math.dist(p, v.at) \
                    <= v.diameter / 2 + half + TOL:
                return None
        for q in self.pads.get(net, []):
            if layer in q.layers and q.poly.distance(pt) <= half + TOL:
                return None
        fill = self._fill(net, layer)
        if fill is not None and not fill.is_empty \
                and fill.distance(pt) <= half + TOL:
            return None
        near = [t for t in self.tracks.get((net, layer), [])
                if t.uuid != cur.uuid
                and t.shape.distance(pt) <= (cur.width + t.width) / 2 + TOL]
        if len(near) != 1:
            return None
        t = near[0]
        if t.uuid in self.items or len(t.shape.coords) != 2:
            return None
        a, b = _ends(t)
        if min(math.dist(a, p), math.dist(b, p)) \
                > max(TOL, min(cur.width, t.width) / 2):
            return None
        if not self.win.contains(t.poly):
            return None
        return t

    def walk(self, t, why: str) -> None:
        """Rip track t and the chain behind both its ends."""
        if not self._take(t, why):
            return
        for p in _ends(t):
            cur = t
            while True:
                nxt = self.next_track(cur, p)
                if nxt is None or not self._take(nxt, why):
                    break
                a, b = _ends(nxt)
                p = b if math.dist(a, p) <= math.dist(b, p) else a
                cur = nxt

    def via(self, v, why: str) -> None:
        """Rip via v and, behind it, its tracks that lie in the window."""
        if not self._take(v, why):
            return
        for ly in self.bg.copper_layers:
            if not v.spans(ly):
                continue
            for t in self.tracks.get((v.net, ly), []):
                if t.uuid not in self.items and t.poly.intersects(v.poly) \
                        and self.win.contains(t.poly):
                    self.walk(t, why)

    def add(self, it, why: str) -> None:
        if isinstance(it, geom.Track):
            self.walk(it, why)
        else:
            self.via(it, why)


def _restore(router, held) -> None:
    router.obs.hidden = set(held[0])
    router.obs.added = list(held[1])
    router._conn = held[2]


def _held(router):
    return set(router.obs.hidden), list(router.obs.added), router._conn


def _meets(router, net: str, w) -> bool:
    """Does box w meet one of net's islands in the router?"""
    return any(w.intersects(p) for i in router.islands(net)
               for ly, ps in i.items() if ly is not None for p in ps)


def join_net(router, net: str, want: int, near, width, keep, boxes=()):
    """Route net back to `want` islands: (ops, uuids pushed aside, how,
    {net pushed: islands it may end with}). The last try pushes other
    nets aside (Router.reroute) in each of `boxes` that one of the net's
    islands meets, one box at a time, then joins what is left. Raises
    CheckError with every try's reason when none works; the router is then
    as it was."""
    why = []
    for w in dict.fromkeys([width, None]):
        held = _held(router)
        try:
            ops = router.join_islands(net, want, near, w)
            how = "rule width" if w is None else f"width {w:g}"
            return ops, [], how, {}
        except CheckError as e:
            _restore(router, held)
            why.append(f"{'rule width' if w is None else f'width {w:g}'}:"
                       f" {e}")
    held = _held(router)
    uuids, ops, maxes, tried = [], [], {}, []
    try:
        for b in boxes:
            if len(router.islands(net, scraps=False)) <= want:
                break
            if not _meets(router, net, b):
                continue
            try:
                u, o, m = router.reroute(net, b.bounds, keep=keep)
            except CheckError as e:     # reroute left the router as it was
                tried.append(str(e))
                continue
            uuids += u
            ops += o
            for v, n in m.items():
                maxes[v] = max(maxes.get(v, 0), n)
        if not uuids:
            raise CheckError("; ".join(tried) or "no window meets its"
                             " islands")
        ops += router.join_islands(net, want, near)
        return ops, uuids, "pushed other nets aside", maxes
    except CheckError as e:
        _restore(router, held)
        why.append(f"push: {e}")
    raise CheckError(f"{net}: " + "; ".join(why))


def _failed_nets(joins: dict) -> list:
    return [n for n, j in joins.items() if j.get("how") == "failed"]


def _via_layers(op: dict, cu: list) -> list:
    if "top" in op and "bot" in op and op["top"] in cu and op["bot"] in cu:
        i, j = sorted((cu.index(op["top"]), cu.index(op["bot"])))
        return cu[i:j + 1]
    return list(cu)


@dataclasses.dataclass(eq=False)
class _Cu:
    """One piece of a net's copper in _dangling's graph: a track, arc, via,
    pad or piece of pour, its centre line, point or polygon (`core`) and
    how far its copper reaches past that (`r`)."""
    uid: str        # the uuid, "+i" for add i, "" for copper that stays
    kind: str       # "seg", "arc", "via", "pad" or "fill"
    item: object    # route_cleanup's Seg, ViaItem or PadItem, the arc's
                    # (net, layer, line, width) or the fill polygon
    layers: frozenset
    core: object
    r: float
    free: bool = False   # may be removed
    alive: bool = True
    near: list = None    # copper within NEAR, on a shared layer (cached)
    touch: list = None   # copper within TOUCH of it (cached)
    _grown: object = None

    @property
    def bounds(self):
        x0, y0, x1, y1 = self.core.bounds
        return x0 - self.r, y0 - self.r, x1 + self.r, y1 + self.r

    def grown(self):
        if self._grown is None:
            self._grown = self.core.buffer(self.r + TOUCH)
        return self._grown


def _cu_touch(n: _Cu, m: _Cu) -> bool:
    """Copper of n and m within TOUCH of each other (they share a layer).
    Two pieces of one pour never touch: they are pieces of its union."""
    if n.kind == "fill" and m.kind == "fill":
        return False
    if n.kind == "fill":
        return n.core.intersects(m.grown())
    if m.kind == "fill":
        return m.core.intersects(n.grown())
    return n.core.distance(m.core) <= n.r + m.r + TOUCH


def _via_free(v, segs, arcs, pads, vias, fills, tol) -> bool:
    """KiCad 10's via rule (CONNECTIVITY_DATA::TestTrackEndpointDangling,
    DRC's via_dangling): a via dangles when nothing of its net meets it,
    or all that does meets it on one layer. Tracks and arcs on a layer it
    spans, pads, other vias and `fills` ((layer, polygon) of its net's
    pour, teardrops left out) count for the layers they meet it on, each
    with `tol` more. A pad or via that meets it on two or more of its
    layers keeps it: KiCad counts such an item for its first layer only,
    and this side never drops a via KiCad keeps."""
    pt = Point(v.at)
    r = v.size / 2.0 + tol
    span = set(v.layers)
    hit = set()
    for layer, poly in fills:
        if layer in span and layer not in hit and poly.distance(pt) <= r:
            hit.add(layer)
    for s in segs:
        if s.net == v.net and s.layer in span and route_cleanup._pt_seg_dist(
                v.at, s.a, s.b) <= r + s.width / 2.0:
            hit.add(s.layer)
    for net, layer, line, w in arcs:
        if net == v.net and layer in span and line.distance(pt) <= r + w / 2.0:
            hit.add(layer)
    for pd in pads:
        both = span & set(pd.layers)
        if pd.net == v.net and both and pd.poly.distance(pt) <= r:
            if len(both) > 1:
                return False
            hit |= both
    for o in vias:
        both = span & set(o.layers)
        if o is not v and o.net == v.net and both \
                and math.dist(o.at, v.at) <= r + o.size / 2.0:
            if len(both) > 1:
                return False
            hit |= both
    return len(hit) <= 1


def _meet_at(line, g, last: bool) -> float:
    """How far along `line` the copper core `g` meets it: the last (or,
    last=False, the first) point of line inside or on g, else the point of
    line nearest g."""
    meet = line.intersection(g)
    if not meet.is_empty:
        ts = [line.project(Point(c)) for c in shapely.get_coordinates(meet)]
        return max(ts) if last else min(ts)
    return line.project(Point(shapely.shortest_line(line, g).coords[0]))


def _net_copper(net, segs, vias, arcs, pads, fills, fixed) -> list:
    """_Cu pieces of one net: segs and vias (free unless their uid is in
    `fixed` or empty), and the arcs, pads and pour pieces, which stay."""
    out = []
    for s in segs:
        out.append(_Cu(s.uuid, "seg", s, frozenset([s.layer]),
                       route_cleanup._body(s.a, s.b), s.width / 2.0,
                       free=bool(s.uuid) and s.uuid not in fixed))
    for v in vias:
        out.append(_Cu(v.uuid, "via", v, frozenset(v.layers), Point(v.at),
                       v.size / 2.0,
                       free=bool(v.uuid) and v.uuid not in fixed))
    for a in arcs:
        out.append(_Cu("", "arc", a, frozenset([a[1]]), a[2], a[3] / 2.0))
    for pd in pads:
        out.append(_Cu("", "pad", pd, frozenset(pd.layers), pd.poly, 0.0))
    for layer, poly in fills:
        shapely.prepare(poly)
        out.append(_Cu("", "fill", poly, frozenset([layer]), poly, 0.0))
    return out


def _dangling(pcb: Path, bg, nets, *, gone=(), adds=(), seed=None,
              anchors=(), cuts: dict | None = None) -> tuple[list, list]:
    """The dead ends in `nets` on pcb, less `gone`, plus the `adds`
    (route_edit add ops), peeled off one by one as KiCad's DRC finds them:
    a track with an end that meets nothing of its net (route_cleanup
    _seg_free, without its bridge exception), a via that nothing meets on
    two layers (_via_free). Pours count, teardrops do not (a refill makes
    them again from what is left). Arcs, pads and the `anchors` (uuids)
    stay. The peel starts from the tracks and vias whose copper meets
    `seed` (all of them when it is None) and follows each removal to the
    copper next to it, inside `seed` or not, so a dead chain goes to its
    end. A dead end that is the only link between copper it meets (no
    other way joins it across the net) stays: no removal splits a net.
    Such a track is cut back instead, its dead end to the last copper it
    meets (#1140: a route that tees into a track whose end lost its
    partner), and `cuts`, when given, gets its uid (as the removals have
    it) and its route_cleanup.Seg before and after, for the cut tracks
    that stay.
    Returns (the removed uuids in order, the indices of the adds removed,
    sorted)."""
    cu = list(bg.copper_layers)
    nets = set(nets)
    gone, fixed = set(gone), set(anchors)
    segs, vias = route_cleanup.parse_items(pcb, cu)
    per: dict = {}
    for s in segs:
        if s.net in nets and s.uuid not in gone:
            per.setdefault(s.net, ([], []))[0].append(s)
    for v in vias:
        if v.net in nets and v.uuid not in gone:
            per.setdefault(v.net, ([], []))[1].append(v)
    for i, op in enumerate(adds):
        if op.get("net") not in nets:
            continue
        if op["op"] == "add_track":
            per.setdefault(op["net"], ([], []))[0].append(route_cleanup.Seg(
                f"+{i}", op["net"], op["layer"], float(op["width"]),
                tuple(op["start"]), tuple(op["end"])))
        elif op["op"] == "add_via":
            per.setdefault(op["net"], ([], []))[1].append(
                route_cleanup.ViaItem(
                    f"+{i}", op["net"], tuple(op["at"]), float(op["size"]),
                    float(op.get("drill", 0.0)),
                    tuple(_via_layers(op, cu))))
    arcs: dict = {}
    for t in bg.tracks_of():
        if t.net in per and t.uuid not in gone and len(t.shape.coords) > 2:
            arcs.setdefault(t.net, []).append(
                (t.net, t.layer, t.shape, t.width))
    pads: dict = {}
    for p in bg.pads_of():
        if p.net in per:
            pads.setdefault(p.net, []).append(route_cleanup.PadItem(
                p.net, tuple(p.layers), p.center, p.poly))
    if seed is not None:
        shapely.prepare(seed)
    tol = route_cleanup.TOL
    removed: list = []
    for net in sorted(per):
        fills = []
        for layer in cu:
            u = shapely.union_all([z.fill_on(layer)
                                   for z in bg.zones_of(net, layer)
                                   if not z.teardrop])
            fills += [(layer, g) for g in getattr(u, "geoms", [u])
                      if not g.is_empty]
        ns = _net_copper(net, *per[net], arcs.get(net, []),
                         pads.get(net, []), fills, fixed)
        tree = shapely.STRtree([box(*n.bounds) for n in ns])

        def near(n):
            if n.near is None:
                x0, y0, x1, y1 = n.bounds
                n.near = [ns[i] for i in sorted(tree.query(box(
                    x0 - NEAR, y0 - NEAR, x1 + NEAR, y1 + NEAR)))
                    if ns[i] is not n and ns[i].layers & n.layers]
            return n.near

        def touching(n):
            if n.touch is None:
                n.touch = [m for m in near(n) if _cu_touch(n, m)]
            return n.touch

        def free(x):
            nb = [m for m in near(x) if m.alive]
            ss = [m.item for m in nb if m.kind == "seg"]
            vs = [m.item for m in nb if m.kind == "via"]
            ac = [m.item for m in nb if m.kind == "arc"]
            pd = [m.item for m in nb if m.kind == "pad"]
            fl = [(next(iter(m.layers)), m.core) for m in nb
                  if m.kind == "fill"]
            if x.kind == "via":
                return _via_free(x.item, ss, ac, pd, vs, fl, tol)
            here = [g for ly, g in fl if ly == x.item.layer]
            pour = shapely.MultiPolygon(
                [q for g in here for q in getattr(g, "geoms", [g])]) \
                if here else None
            return route_cleanup._seg_free(
                x.item, ss, vs, pd, lambda _n, _l: pour, ac, tol,
                bridges=False)

        def splits(x):
            """Removing x leaves two pieces of the copper it touches with
            no other way between them across the net."""
            nb = [m for m in touching(x) if m.alive]
            if len(nb) < 2:
                return False
            idx = {id(m): i for i, m in enumerate(nb)}
            parent = list(range(len(nb)))

            def find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i

            for i, m in enumerate(nb):
                for o in touching(m):
                    j = idx.get(id(o))
                    if j is not None and find(i) != find(j):
                        parent[max(find(i), find(j))] = min(find(i), find(j))
            part = [find(i) for i in range(len(nb))]
            want = set(part) - {part[0]}
            if not want:
                return False
            todo = deque(m for i, m in enumerate(nb) if part[i] == part[0])
            seen = {id(m) for m in todo}
            x.alive = False
            try:
                while todo and want:
                    for m in touching(todo.popleft()):
                        if m.alive and id(m) not in seen:
                            seen.add(id(m))
                            if id(m) in idx:
                                want.discard(part[idx[id(m)]])
                            todo.append(m)
                return bool(want)
            finally:
                x.alive = True

        def cut(x):
            """Cut track x, which dangles but is the only link between
            copper it meets, back from each dead end to the last copper
            it meets there; False (x as it was) when no end is dead by
            itself (one copper item meets both, KiCad's one-end rule), the
            piece left would be shorter than CUT_MIN or would still
            dangle, or something x met no longer meets it."""
            s, line = x.item, x.core
            if line.geom_type != "LineString":
                return False
            half = s.width / 2.0
            hits = [m for m in touching(x) if m.alive]
            nb = [m for m in near(x) if m.alive]
            dead = [not any(m.core.distance(Point(p)) <= half + m.r + tol
                            for m in nb) for p in (s.a, s.b)]
            if not hits or not any(dead):
                return False
            lo, hi = 0.0, line.length
            if dead[0]:
                lo = min(_meet_at(line, m.core, last=True) for m in hits)
            if dead[1]:
                hi = max(_meet_at(line, m.core, last=False) for m in hits)
            if hi - lo < CUT_MIN:
                return False
            a = tuple(round(v, 6) for v in line.interpolate(lo).coords[0])
            b = tuple(round(v, 6) for v in line.interpolate(hi).coords[0])
            if (a, b) == (tuple(s.a), tuple(s.b)):
                return False
            x.item = route_cleanup.Seg(s.uuid, s.net, s.layer, s.width, a, b)
            x.core = route_cleanup._body(a, b)
            x._grown, x.touch = None, None
            if free(x) or not all(_cu_touch(x, m) for m in hits):
                x.item, x.core = s, line
                x._grown, x.touch = None, None
                return False
            return True

        start = [n for n in ns if n.free and (
            seed is None or shapely.dwithin(seed, n.core, n.r))]
        todo = deque(sorted(start, key=lambda n: n.uid))
        queued = {id(n) for n in todo}
        short: dict = {}
        ncut: dict = {}
        while todo:
            x = todo.popleft()
            queued.discard(id(x))
            if not x.alive or not free(x):
                continue
            if splits(x):
                if x.kind != "seg" or ncut.get(id(x), 0) >= CUT_CAP:
                    continue
                was = x.item
                if not cut(x):
                    continue
                # what met the tail it lost may dangle now
                short.setdefault(id(x), (x, was))
                ncut[id(x)] = ncut.get(id(x), 0) + 1
            else:
                x.alive = False
                removed.append(x.uid)
            for m in sorted(near(x), key=lambda n: n.uid):
                if m.alive and m.free and id(m) not in queued:
                    todo.append(m)
                    queued.add(id(m))
        if cuts is not None:
            cuts.update((x.uid, (was, x.item))
                        for x, was in short.values() if x.alive)
    old = [u for u in removed if not u.startswith("+")]
    trimmed = sorted(int(u[1:]) for u in removed if u.startswith("+"))
    return old, trimmed


def _dead_on(pcb: Path, bg, nets) -> set:
    """The uuids of the tracks and vias of nets that dangle on pcb as it
    is: what _dangling removes there or cuts back. A run leaves them."""
    cuts: dict = {}
    return set(_dangling(pcb, bg, nets, cuts=cuts)[0]) | set(cuts)


def _core(it) -> tuple:
    """A geom Track's or Via's centre line or point, its copper's reach
    past that, and the copper layers it is on."""
    if isinstance(it, geom.Track):
        return it.shape, it.width / 2.0, (it.layer,)
    return Point(it.at), it.diameter / 2.0, tuple(it.layers)


def _joins(g, it) -> set:
    """What of its net meets track or via `it` on board g, pours left out:
    tracks and vias by uuid, pads as REF.NUM."""
    core, r, layers = _core(it)
    tol = route_cleanup.TOL
    out = set()
    for t in g.tracks_of(net=it.net):
        if t.uuid != it.uuid and t.layer in layers \
                and t.shape.distance(core) <= r + t.width / 2.0 + tol:
            out.add(t.uuid)
    for v in g.vias_of(net=it.net):
        if v.uuid != it.uuid and set(v.layers) & set(layers) \
                and Point(v.at).distance(core) <= r + v.diameter / 2.0 + tol:
            out.add(v.uuid)
    for p in g.pads_of(net=it.net):
        if set(p.layers) & set(layers) and p.poly.distance(core) <= r + tol:
            out.add(f"{p.ref}.{p.number}")
    return out


def _pour_cut(bg0, bg, uuids, made, cache: dict) -> dict:
    """(#1140) Of the tracks and vias `uuids` (what a prune of bg would
    take or cut back), those that were on the base board bg0, not made by
    this run, met a pour of their net there on a layer whose pour on bg no
    longer meets them, and still meet all else of their net they met
    there: the refill cut the pour back from them, and the run took none
    of what they hung on. They are live design copper, not copper the run
    left hanging, so the prune leaves them and the gate counts what that
    costs. uuid -> (the item on bg, the layers it lost). `cache` keeps
    each board's pours."""
    tol = route_cleanup.TOL

    def fill(g, net, layer):
        k = (id(g), net, layer)
        if k not in cache:
            cache[k] = shapely.union_all([
                z.fill_on(layer) for z in g.zones_of(net, layer)
                if not z.teardrop])
        return cache[k]

    def meets(g, it, layer):
        core, r, _ = _core(it)
        f = fill(g, it.net, layer)
        return not f.is_empty and f.distance(core) <= r + tol

    want = {u for u in uuids if u and u not in made}
    if not want:
        return {}
    was = {it.uuid: it for it in bg0.tracks_of() + bg0.vias_of()
           if it.uuid in want}
    out = {}
    for it in bg.tracks_of() + bg.vias_of():
        a = was.get(it.uuid)
        if a is None:
            continue
        lost = [ly for ly in _core(a)[2]
                if ly in _core(it)[2] and meets(bg0, a, ly)
                and not meets(bg, it, ly)]
        if lost and _joins(bg0, a) <= _joins(bg, it):
            out[it.uuid] = (it, lost)
    return out


def _xy(p) -> str:
    return f"({_r(p[0])}, {_r(p[1])})"


def _pour_note(bg, it, lost, adds, refs) -> str:
    """A note on board copper `it` whose pour on the `lost` layers the
    refill cut back (_pour_cut): what of the run's did it, the nearest add
    of another net (`adds`, route_edit ops on the board) or pad of a
    moved part (`refs`) on such a layer within LOST_NEAR, and the pad of
    its net it sits on."""
    core, r, layers = _core(it)
    cu = list(bg.copper_layers)
    best = None
    for op in adds:
        if op.get("net") == it.net:
            continue
        if op["op"] == "add_via" and set(_via_layers(op, cu)) & set(lost):
            g = Point(op["at"]).buffer(float(op["size"]) / 2.0)
            what = f"the new via of {op['net']} at {_xy(op['at'])}"
        elif op["op"] == "add_track" and op["layer"] in lost:
            g = LineString([op["start"], op["end"]]).buffer(
                float(op["width"]) / 2.0)
            what = (f"the new track of {op['net']} from"
                    f" {_xy(op['start'])} to {_xy(op['end'])}")
        else:
            continue
        d = g.distance(core) - r
        if d <= LOST_NEAR and (best is None or d < best[0]):
            best = (d, what)
    for ref in refs:
        for p in bg.pads_of(ref=ref):
            if p.net != it.net and set(p.layers) & set(lost):
                d = p.poly.distance(core) - r
                if d <= LOST_NEAR and (best is None or d < best[0]):
                    best = (d, f"{ref}.{p.number} in its new place")
    on = next((f" on {p.ref}.{p.number}" for p in bg.pads_of(net=it.net)
               if set(p.layers) & set(layers)
               and p.poly.distance(core) <= r + route_cleanup.TOL), "")
    kind = _kind(it)
    where = (f"at {_xy(it.at)}" if kind == "via" else
             f"from {_xy(it.shape.coords[0])} to {_xy(it.shape.coords[-1])}")
    pours = " and ".join(lost) + (" pours" if len(lost) > 1 else " pour")
    return (f"{best[1] + ' cuts' if best else 'the refill cuts'} the {pours}"
            f" of {it.net} back from {kind} {it.uuid[:8]} {where}{on}: the"
            f" {kind} stays (it met the pour on the base board) and the gate"
            " counts what that costs")


def _peel(pcb: Path, bg0, bg, nets, *, made, adds_on, refs, anchors: set,
          notes: list, **kw) -> tuple[list, list, dict]:
    """_dangling(pcb, bg, nets, **kw), but the board copper whose pour the
    refill cut back (_pour_cut against the base board bg0) stays: it joins
    `anchors` (grown in place), with a note each in `notes` (`adds_on`,
    the add ops on bg, and `refs` name what cut it), and the peel runs
    again. Returns _dangling's removals and adds removed, and its cuts."""
    cache: dict = {}
    while True:
        cuts: dict = {}
        dead, trim = _dangling(pcb, bg, nets, anchors=anchors, cuts=cuts,
                               **kw)
        hit = _pour_cut(bg0, bg, [u for u in list(dead) + list(cuts)
                                  if not u.startswith("+")], made, cache)
        if not hit:
            return dead, trim, cuts
        anchors |= set(hit)
        notes += [_pour_note(bg, it, lost, adds_on, refs)
                  for it, lost in hit.values()]


def _set_ends(uuid: str, was, new) -> list:
    """route_edit set_point ops that move track uuid's ends from where Seg
    was has them to where Seg new has them (the same track, cut back)."""
    return [{"op": "set_point", "uuid": uuid, "which": w, "to": list(q)}
            for w, p, q in (("start", was.a, new.a), ("end", was.b, new.b))
            if tuple(p) != tuple(q)]


def _cut_note(uuid: str, net: str, layer: str, was, now) -> dict:
    """One payload["shortened"] row: track uuid ("" for a route of this
    run) cut back from the ends in was to those in now ([start, end])."""
    return {"uuid": uuid, "net": net, "layer": layer,
            "from": [_r(v) for q in was for v in q],
            "to": [_r(v) for q in now for v in q]}


def _made(ops: list, results: list, first: int) -> tuple[dict, set]:
    """route_edit's results for ops, whose adds start at index first: the
    uuid each add made, to its index among the adds, and the indices of
    the adds that made nothing (the copper was there already)."""
    made, dup = {}, set()
    for i in range(first, len(ops)):
        r = results[i] if i < len(results) else {}
        if r.get("uuid"):
            made[r["uuid"]] = i - first
        elif r.get("status") == "exists":
            dup.add(i - first)
    return made, dup


def touch_shorts(bg, refs, before=frozenset()) -> list[str]:
    """Tracks and vias of another net whose copper touches a pad of refs on
    bg (KiCad would carry them onto the pad's net when it saves), less the
    (ref, uuid) pairs in `before`: one line each."""
    out = []
    for r in refs:
        for p in bg.pads_of(ref=r):
            for it in board_update._touching(bg, p):
                if not board_update._nets_equal(it.net, p.net) \
                        and (r, it.uuid) not in before:
                    out.append(f"{r}.{p.number} ({p.net or 'no net'}) <-"
                               f" {it.net or 'no net'} {_kind(it)}"
                               f" {(it.uuid or '?')[:8]}")
    return out


def _shorted(bg, old: dict) -> set:
    """(ref, uuid) of the other-net tracks and vias that touch a ref's
    pads on bg already."""
    return {(r, it.uuid) for r, pads in old.items() for p in pads
            for it in board_update._touching(bg, p)
            if not board_update._nets_equal(it.net, p.net)}


def patch_notes(bg, old: dict, new: dict) -> list[str]:
    """PATCH_<net> zones whose fill met an old pad of their net and meets
    no new one: zone_patch's to reshape."""
    notes = []
    for z in bg.zones_of():
        if not z.name.startswith("PATCH_"):
            continue
        for ly in z.fills:
            fill = z.fill_on(ly)
            if fill.is_empty:
                continue
            for ref in old:
                met = [p for p in old[ref] if p.net == z.net
                       and ly in p.layers and fill.intersects(p.poly)]
                meets = [p for p in new.get(ref, []) if p.net == z.net
                         and ly in p.layers and fill.intersects(p.poly)]
                if met and not meets:
                    notes.append(
                        f"zone {z.name} on {ly} met {ref}."
                        f"{met[0].number} and meets no new pad of {z.net}:"
                        f" reshape it with zone_patch --net {z.net} --layer"
                        f" {ly} (or --remove)")
    return notes


def _vsig(v: dict) -> tuple:
    pts = sorted(tuple(round(c, 3) for c in it["pos"])
                 for it in v.get("items") or [] if it.get("pos"))
    return v.get("source"), v.get("check"), v.get("net"), tuple(pts)


def _usig(v: dict):
    uu = sorted(it["uuid"] for it in v.get("items") or [] if it.get("uuid"))
    return (v.get("source"), v.get("check"), tuple(uu)) if uu else None


def unmatched(drc0: dict, drc1: dict) -> list[dict]:
    """The after violations that no before violation matches: first by
    source, check, net and item positions (_vsig), then, for the rest, by
    source, check and item uuids (_usig: the same items, moved or reshaped
    by the refill). Each before violation matches one after at most."""
    pool: dict = {}
    for i, v in enumerate(drc0["violations"]):
        pool.setdefault(_vsig(v), []).append(i)
    used, rest = set(), []
    for v in drc1["violations"]:
        got = pool.get(_vsig(v))
        if got:
            used.add(got.pop())
        else:
            rest.append(v)
    left = Counter(_usig(v) for i, v in enumerate(drc0["violations"])
                   if i not in used and _usig(v))
    out = []
    for v in rest:
        sig = _usig(v)
        if sig and left[sig]:
            left[sig] -= 1
        else:
            out.append(v)
    return out


def _key(v: dict) -> str:
    return "unconnected" if v.get("source") == "unconnected" \
        else f"{v.get('source')}:{v.get('check')}"


def _issue(v: dict) -> bool:
    """An error or warning that issue_counts counts (not unconnected)."""
    return v.get("source") != "unconnected" \
        and v.get("severity") in ("error", "warning")


def _in(area, v: dict) -> bool:
    """Does violation v, or one of its items, lie in area?"""
    if area is None:
        return False
    pts = [v.get("pos")] + [it.get("pos") for it in v.get("items") or []]
    return any(area.intersects(Point(p)) for p in pts if p)


def gates(drc0: dict, drc1: dict, isl: dict, area=None) -> list[str]:
    """Why the repaired board is worse than the base one (empty: it isn't).
    area: where the run changed the board (repair_area); an error or
    warning in it that matches no base violation (unmatched) fails too."""
    worse = [f"{n or '(no net)'}: {a} -> {b} islands"
             for n, (a, b) in sorted(isl.items()) if b > a]
    i0, i1 = route_improve.issue_counts(drc0), route_improve.issue_counts(drc1)
    worse += [f"DRC {k}: {i0.get(k, 0)} -> {v}"
              for k, v in sorted(i1.items()) if v > i0.get(k, 0)]
    u0, u1 = rf.unconnected_by_net(drc0), rf.unconnected_by_net(drc1)
    worse += [f"unconnected {n or '(no net)'}: {u0.get(n, 0)} -> {v}"
              for n, v in sorted(u1.items()) if v > u0.get(n, 0)]
    a, b = route_improve.unconnected(drc0), route_improve.unconnected(drc1)
    if b > a:
        worse.append(f"unconnected: {a} -> {b}")
    near = Counter(_key(v) for v in unmatched(drc0, drc1)
                   if _issue(v) and _in(area, v))
    worse += [f"DRC {k}: {n} new near the repair (none like it before)"
              for k, n in sorted(near.items())]
    return worse


def repair_area(win, adds: list, gone: list, margin: float = 1.0):
    """The windows, and margin mm round the copper the run added (route_edit
    add ops) and removed (tracks and vias): where its new violations lie."""
    shapes = [win]
    for op in adds:
        if op["op"] == "add_track":
            shapes.append(LineString([op["start"], op["end"]]).buffer(
                op["width"] / 2 + margin))
        elif op["op"] == "add_via":
            shapes.append(Point(op["at"]).buffer(op["size"] / 2 + margin))
    shapes += [it.poly.buffer(margin) for it in gone]
    return shapely.union_all(shapes)


def new_violations(drc0: dict, drc1: dict, area=None) -> list[dict]:
    """The after violations behind gates' DRC and unconnected lines.

    The after violations that no before violation matches (unmatched),
    for each source:check that counts more errors or warnings than before,
    each net with more unconnected items, and every error or warning in
    area: where to look on DIR/work/B, and the uuids of the items to rip or
    reroute."""
    i0, i1 = route_improve.issue_counts(drc0), route_improve.issue_counts(drc1)
    grew = {k for k, n in i1.items() if n > i0.get(k, 0)}
    u0, u1 = rf.unconnected_by_net(drc0), rf.unconnected_by_net(drc1)
    grew_net = {k for k, n in u1.items() if n > u0.get(k, 0)}
    out = []
    for v in unmatched(drc0, drc1):
        key = _key(v)
        if key == "unconnected":
            if (v.get("net") or "") not in grew_net:
                continue
        elif not _issue(v) or (key not in grew and not _in(area, v)):
            continue
        out.append({"key": key, "severity": v.get("severity"),
                    "pos": v.get("pos"), "layer": v.get("layer"),
                    "net": v.get("net"), "msg": v.get("msg"),
                    "items": [{"msg": it.get("msg"), "pos": it.get("pos"),
                               "uuid": it.get("uuid")}
                              for it in v.get("items") or []]})
    return out


def _vline(v: dict) -> str:
    """One REPORT.md line. An item away from the marker is named where it
    is: KiCad puts a zone's marker (starved_thermal) at the zone's position,
    which may be far from the pad that starves."""
    def xy(p):
        return f"({p[0]:.3f}, {p[1]:.3f})"
    at = f" at {xy(v['pos'])}" if v.get("pos") else ""
    on = ", ".join(x for x in (v.get("layer") and f"on {v['layer']}",
                               v.get("net") and f"net {v['net']}") if x)
    items = v.get("items") or []
    uu = [it["uuid"] for it in items if it.get("uuid")]
    away = [f"{it.get('msg') or 'item'} at {xy(it['pos'])}" for it in items
            if it.get("pos") and v.get("pos")
            and math.dist(it["pos"], v["pos"]) > 1e-3]
    return (f"{v['key']}{at}{' ' + on if on else ''}: {v.get('msg') or ''}"
            + (f" (items {', '.join(uu)})" if uu else "")
            + (f"; {'; '.join(away)}" if away else ""))


def _write_ops(path: Path, ops: list) -> None:
    path.write_text(json.dumps({"version": 1, "ops": ops}, indent=1) + "\n",
                    encoding="utf-8")


def repair(pcb: Path, ops: list, out: Path, *, margin: float = MARGIN,
           keep=(), apply: bool = False, allow_unread: bool = False) -> dict:
    """The steps in the module docstring, in out; the payload. Once out is
    made, an error is written to part_repair.json and REPORT.md (status
    "error") before it is raised."""
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        raise CheckError("KiCad bundled python not found (env.py)")
    sha0 = _sha(pcb)
    out = routelib.fresh_work_dir(out)
    refs = list(dict.fromkeys(op["ref"] for op in ops))
    payload: dict = {"script": "part_repair", "status": "pass",
                     "board": str(pcb), "board_sha256": sha0,
                     "out": str(out), "refs": refs, "ops": len(ops),
                     "notes": [], "applied": False}
    try:
        return _steps(pcb, ops, out, payload, cli=cli, margin=margin,
                      keep=keep, apply=apply, allow_unread=allow_unread)
    except Exception as e:
        payload["status"] = "error"
        payload["error"] = f"{type(e).__name__}: {e}"
        try:
            _finish(payload, out)
        except Exception:       # the first error is the one to raise
            pass
        raise


def _steps(pcb: Path, ops: list, out: Path, payload: dict, *, cli, margin,
           keep, apply, allow_unread=False) -> dict:
    refs = payload["refs"]

    # 1. base: refilled, DRC'd
    base = route_auto._stage_board(pcb, out / "base")
    # a disallow rule the router can't read stops the run before the DRC
    # (#1140): the routes could break it
    bans = rf.board_rules(base, geom.BoardGeom.from_file(base)).bans
    unread = list(bans.unread) if bans else []
    if unread:
        payload["unread_dru_rules"] = unread
        payload["allow_unread_rules"] = bool(allow_unread)
        if not allow_unread:
            raise CheckError(
                "the .kicad_dru's disallow rule(s) " + ", ".join(unread)
                + " have a condition the router doesn't read, so its routes"
                " could break them: --allow-unread-rules routes anyway and"
                " leaves them to the DRC gate")
    drc0 = kc.run_drc(cli, base, refill=True, save_board=True)
    bg0 = geom.BoardGeom.from_file(base)

    # 2. trial: where every pad lands
    trial = route_auto._stage_board(base, out / "trial")
    place_edit.apply_ops(trial, copy.deepcopy(ops), touch_check=False)
    bgt = geom.BoardGeom.from_file(trial)
    old = {r: bg0.pads_of(ref=r) for r in refs}
    new = {r: bgt.pads_of(ref=r) for r in refs}

    # 3. windows, 4. rips
    wins = windows(old, new, margin)
    win = shapely.union_all(list(wins.values()))
    payload["windows"] = {r: [_r(v) for v in w.bounds]
                          for r, w in wins.items()}
    rip = Rip(bg0, win, keep)
    for it in seeds(bg0, old, new):
        rip.add(it, "copper on an old pad")
    clashes = 0
    # a rule's footprint test reads the board the ops leave: the parts
    # where the trial puts them. A board's model is loaded only when such
    # a test first needs it (#1163): most boards have none
    models: dict = {}

    def model_of(path: Path) -> placelib.PlaceModel:
        if path not in models:
            models[path] = placelib.PlaceModel(path)
        return models[path]

    def new_fps() -> dict:
        moved = model_of(trial).footprints
        return {r: moved.get(r) for r in refs}

    for r in refs:
        hits = board_update.swap_clashes(
            bg0, r, new[r], base, model=lambda: model_of(base),
            pending=set(refs) - {r}, new_fps=new_fps)
        clashes += len(hits)
        for it in hits:
            rip.add(it, f"other-net copper too close to {r}'s new pads")
    ripped = list(rip.items.values())
    by_net: dict = {}
    for it in ripped:
        by_net.setdefault(it.net, {"tracks": 0, "vias": 0})[
            _kind(it) + "s"] += 1
    payload["ripped"] = {
        "tracks": sum(isinstance(i, geom.Track) for i in ripped),
        "vias": sum(not isinstance(i, geom.Track) for i in ripped),
        "clashes": clashes, "by_net": by_net}
    payload["notes"] += patch_notes(bg0, old, new)

    # 5. work: rips, then the ops (refilled), then no other-net copper on
    # the parts' pads
    work = route_auto._stage_board(base, out / "work")
    rip_ops = [{"op": "remove", "uuid": it.uuid} for it in ripped]
    if rip_ops:
        route_edit.apply_ops(work, rip_ops)
    results = place_edit.apply_ops(work, copy.deepcopy(ops), refill=True,
                                   touch_check=True)
    payload["notes"] += [n for res in results if isinstance(res, dict)
                         for n in res.get("notes", [])]

    # 6. joins: the parts' nets first, then what the rips touched
    part_nets = [p.net for r in refs for p in old[r] + new[r] if p.net]
    nets = list(dict.fromkeys(part_nets + [it.net for it in ripped
                                           if it.net]))
    bgw = geom.BoardGeom.from_file(work)
    shorts = touch_shorts(bgw, refs, _shorted(bg0, old))
    if shorts:
        raise CheckError("other-net copper touches the parts' pads after the"
                         " rips (a rip missed it; KiCad would put it on the"
                         " pad's net): " + "; ".join(shorts))
    # spokes: the routes keep off the thermal spokes the refill keeps (#1140)
    router = mazeroute.Router(bgw, spokes=True)
    for ref, num, layer, n in router.obs.short_spokes(refs):
        payload["notes"].append(
            f"{ref}.{num} on {layer}: the refill after the moves leaves it"
            f" {n} thermal spoke{'' if n == 1 else 's'} where DRC asks"
            f" {rf.SPOKES_MIN} (starved thermal); the copper already round"
            " the pad starves the fill there, so no route of this run mends"
            " it: move that copper or the part, or join the pad solid")
    base_isl: dict = {}

    def base_count(n):
        if n not in base_isl:
            base_isl[n] = len(rf.net_islands(bg0, n))
        return base_isl[n]

    widest: dict = {}
    for it in ripped:
        if isinstance(it, geom.Track):
            widest[it.net] = max(widest.get(it.net, 0.0), it.width)
    near_of = {}
    for r, w in wins.items():
        for p in old[r] + new[r]:
            near_of[p.net] = near_of[p.net].union(w) if p.net in near_of \
                else w

    def join_pass(order):
        """Join each net of order back to its base count, in turn, and the
        nets a push re-routed: (joins, adds, pushed, failed reasons,
        queue)."""
        adds: list = []
        pushed: list = []
        joins: dict = {}
        failed: list = []
        queue = list(order)
        for net in queue:
            # a fill scrap the moves cut off is the refill's (Router.islands)
            want, have = base_count(net), len(router.islands(net,
                                                             scraps=False))
            joins[net] = {"islands_before": want, "after_rip": have}
            if have <= want:
                joins[net]["how"] = "still joined"
                continue
            near = near_of.get(net) or win
            own = [r for r in refs
                   if any(p.net == net for p in old[r] + new[r])]
            boxes = [wins[r] for r in own] + [w for r, w in wins.items()
                                              if r not in own]
            try:
                jops, gone, how, maxes = join_net(
                    router, net, want, near, widest.get(net), keep, boxes)
            except CheckError as e:
                joins[net]["how"] = "failed"
                failed.append(str(e))
                continue
            joins[net].update(how=how, tracks=sum(
                o["op"] == "add_track" for o in jops), vias=sum(
                o["op"] == "add_via" for o in jops))
            adds += jops
            pushed += gone
            for v in maxes:
                if v not in queue:
                    queue.append(v)
                    base_count(v)
        return joins, adds, pushed, failed, queue

    # one net's join can take the room another needs, and no push moves a
    # power net's copper (Router.rippable): on the C216-C224 job +3V3's
    # push left /soc/VDDA_1P8_USB_F no path (#1140). So when nets fail,
    # the joins start again, once, from the board after the rips, with
    # the nets that failed first; if that fails too, both passes report.
    held = _held(router)
    joins, adds, pushed, failed, queue = join_pass(nets)
    if failed:
        first = _failed_nets(joins)
        _restore(router, held)
        joins, adds, pushed, failed2, queue = join_pass(
            first + [n for n in nets if n not in first])
        payload["join_passes"] = [
            {"first": [], "failed": first},
            {"first": first, "failed": _failed_nets(joins)}]
        if failed2:
            failed = [f"pass 1: {f}" for f in failed] + [
                f"pass 2, {', '.join(first)} first: {f}" for f in failed2]
        else:
            failed = []
            payload["notes"].append(
                f"{', '.join(first)} would not join in the parts' order, so"
                " the joins ran again from the rips with "
                + ("it" if len(first) == 1 else "them") + " first: every"
                " net joined")
    payload["joins"] = joins
    payload["pushed"] = pushed
    if failed:
        payload["status"] = "violations"
        payload["failed"] = failed
        return _finish(payload, out)

    # prune what the rips, pushes and joins left hanging: in the windows,
    # and within margin of the copper pushed aside (a pushed track crosses
    # a window's edge, so what it led to outside can be left dangling)
    nets = set(queue)
    by_uuid = {it.uuid: it for it in bgw.tracks_of() + bgw.vias_of()
               if it.uuid}
    reach = shapely.union_all([win] + [by_uuid[u].poly.buffer(margin)
                                       for u in pushed if u in by_uuid])
    # (#1140): from 1 mm round what the run added and removed, out along
    # each dead chain; what was dead on the base board stays
    seed = repair_area(reach, adds, ripped + [by_uuid[u] for u in pushed
                                              if u in by_uuid])
    anchors = _dead_on(base, bg0, nets)
    # board copper a moved pad cut its pour back from stays (_peel)
    pruned, trim, cuts = _peel(work, bg0, bgw, nets, made={}, adds_on=(),
                               refs=refs, anchors=anchors,
                               notes=payload["notes"], gone=pushed,
                               adds=adds, seed=seed)
    payload["pruned"] = list(pruned)
    payload["trimmed"] = [adds[i] for i in trim]
    # a dead tail the prune can't remove without splitting a net is cut
    # back to its last joint (#1140): a board track by set_point, so it
    # keeps its uuid, a route of this run in its add op; adds stays whole
    # (the gate's area is round all the run put down)
    moved = {u: c for u, c in cuts.items() if not u.startswith("+")}
    shorter: dict = {}   # kept index -> (the add's ends before, now)
    kept = []
    for i, op in enumerate(adds):
        if i in set(trim):
            continue
        c = cuts.get(f"+{i}")
        if c:
            op = dict(op, start=list(c[1].a), end=list(c[1].b))
            shorter[len(kept)] = ((c[0].a, c[0].b), (c[1].a, c[1].b))
        kept.append(op)

    def ops_now():
        return [{"op": "remove", "uuid": u}
                for u in dict.fromkeys(pushed + pruned)] + [
            o for u, (was, new) in moved.items()
            for o in _set_ends(u, was, new)] + kept

    route_ops = ops_now()

    # 7. check
    made: dict = {}
    dup: set = set()
    if route_ops:
        res = route_edit.apply_ops(work, route_ops)
        # an add that met its copper already there made nothing, and goes
        # from the replay too: else a later peel of that copper would
        # leave the replay with it and work without
        made, dup = _made(route_ops, res, len(route_ops) - len(kept))
    drc1 = kc.run_drc(cli, work, refill=True, save_board=True)
    bg1 = geom.BoardGeom.from_file(work)

    def gate_area():
        return repair_area(reach, adds, ripped + [
            by_uuid[u] for u in dict.fromkeys(pushed + pruned + list(moved))
            if u in by_uuid])

    def pour_nets(area):
        return {z.net for g in (bg0, bg1) for z in g.zones_of()
                if z.net and not z.teardrop
                and any(f.intersects(area) for fs in z.fills.values()
                        for f in fs)}

    # the refill reshapes the pours, so copper the prune kept for a pour
    # that no longer meets it is a dead end now: peel again on the
    # refilled board, as DRC finds it, until nothing more goes (#1140).
    # Each pass reads the pour nets of the area as it has grown; a pass
    # after the last refill only checks, and names what is left.
    seen = set(nets)
    late_anchors = set(anchors)
    drop: set = set()
    for npass in range(REPRUNE + 1):
        area = gate_area()
        late_nets = nets | pour_nets(area)
        extra = late_nets - seen
        if extra:
            late_anchors |= _dead_on(base, bg0, extra)
            seen |= extra
        # board copper the run's routes cut its pour back from stays
        dead, _, late = _peel(
            work, bg0, bg1, late_nets, made=made,
            adds_on=[op for i, op in enumerate(kept) if i not in drop | dup],
            refs=refs, anchors=late_anchors, notes=payload["notes"],
            seed=area)
        if not dead and not late:
            break
        if npass == REPRUNE:
            payload["notes"].append(
                f"after {REPRUNE} refills the prune still finds dead "
                f"copper: {', '.join(sorted(set(dead) | set(late)))}")
            break
        step = [{"op": "remove", "uuid": u} for u in dead]
        for u, (was, new) in late.items():
            step += _set_ends(u, was, new)
            if u in made:   # a route of this run: its add op is cut back
                i = made[u]
                ends = (tuple(kept[i]["start"]), tuple(kept[i]["end"]))
                kept[i] = dict(kept[i], start=list(new.a), end=list(new.b))
                shorter[i] = (shorter.get(i, (ends,))[0], (new.a, new.b))
            else:
                moved[u] = (moved.get(u, (was,))[0], new)
        route_edit.apply_ops(work, step)
        for u in dead:
            if u in made:
                drop.add(made[u])
            else:
                pruned.append(u)
                moved.pop(u, None)
        drc1 = kc.run_drc(cli, work, refill=True, save_board=True)
        bg1 = geom.BoardGeom.from_file(work)
    area = gate_area()
    payload["trimmed"] += [kept[i] for i in sorted(drop)]
    payload["shortened"] = [
        _cut_note(u, was.net, was.layer, (was.a, was.b), (new.a, new.b))
        for u, (was, new) in moved.items()] + [
        _cut_note("", kept[i]["net"], kept[i]["layer"], *shorter[i])
        for i in sorted(shorter) if i not in drop | dup]
    kept = [op for i, op in enumerate(kept) if i not in drop | dup]
    payload["pruned"] = pruned
    route_ops = ops_now()
    payload["added"] = {
        "tracks": sum(o["op"] == "add_track" for o in kept),
        "vias": sum(o["op"] == "add_via" for o in kept)}
    pours = pour_nets(area)
    isl = {n: [base_count(n), len(rf.net_islands(bg1, n))]
           for n in sorted(nets | pours)}
    payload["islands"] = isl
    payload["drc"] = {"before": drc0["counts"], "after": drc1["counts"]}
    worse = gates(drc0, drc1, isl, area)
    if worse:
        payload["status"] = "violations"
        payload["worse"] = worse
        payload["new_violations"] = new_violations(drc0, drc1, area)
        return _finish(payload, out)

    files = []
    for name, body in ((RIP_FILE, rip_ops), (PLACE_FILE, ops),
                       (ROUTE_FILE, route_ops)):
        if body:
            _write_ops(out / name, body)
            files.append(name)
    payload["op_files"] = files
    payload["replay"] = _replay(pcb, out, files)
    if apply:
        tmp = pcb.with_name(f".{pcb.name}.part_repair")
        shutil.copy2(work, tmp)  # work is DIR/work/B itself
        if _sha(pcb) != payload["board_sha256"]:
            tmp.unlink()
            raise CheckError(
                f"{pcb} changed while part_repair ran: not applied. The op"
                f" files in {out} replay on the board as it was when the run"
                f" began (sha256 {payload['board_sha256'][:12]}), not on it"
                " as it is now")
        os.replace(tmp, pcb)
        payload["applied"] = True
    return _finish(payload, out)


def _replay(pcb: Path, out: Path, files: list) -> list[str]:
    """The commands that replay `files` on pcb, in order; the last one
    refills the zones (route_edit and place_edit both take --refill).
    Paths are shell-quoted, so a line pastes as it is."""
    tail = {RIP_FILE: "route_edit", PLACE_FILE: "place_edit",
            ROUTE_FILE: "route_edit"}
    cmds = []
    for i, f in enumerate(files):
        extra = " --allow-routed" if f == PLACE_FILE else ""
        if i == len(files) - 1:
            extra += " --refill"
        cmds.append(f"hwde {tail[f]} --pcb {shlex.quote(str(pcb))}"
                    f" --ops {shlex.quote(str(out / f))}{extra}")
    return cmds


def _finish(payload: dict, out: Path) -> dict:
    (out / "part_repair.json").write_text(json.dumps(payload, indent=1),
                                          encoding="utf-8")
    (out / "REPORT.md").write_text(report_md(payload), encoding="utf-8")
    return payload


def report_md(p: dict) -> str:
    lines = [f"# part_repair: {p['status']}", "",
             f"Board: `{p['board']}`; parts: {', '.join(p['refs'])};"
             f" ops: {p['ops']}.", ""]
    if p.get("error"):
        lines += [f"Error: {p['error']}", ""]
    rp = p.get("ripped")
    if rp:
        lines += [f"Ripped {rp['tracks']} tracks and {rp['vias']} vias"
                  f" ({rp['clashes']} too close to new pads).", ""]
    if p.get("joins"):
        lines += ["| net | islands before | after rip | how | tracks | vias |",
                  "|---|---|---|---|---|---|"]
        for n, j in p["joins"].items():
            lines.append(f"| {n} | {j['islands_before']} | {j['after_rip']}"
                         f" | {j.get('how', '')} | {j.get('tracks', '')}"
                         f" | {j.get('vias', '')} |")
        lines.append("")
    if p.get("pruned") or p.get("trimmed"):
        lines += [f"Pruned {len(p.get('pruned') or [])} dead ends the run"
                  f" left (tracks and vias) and {len(p.get('trimmed') or [])}"
                  " the router added; what dangled on the board before"
                  " stays.", ""]
    if p.get("shortened"):
        n = len(p["shortened"])
        lines += [f"Cut back {n} track{'' if n == 1 else 's'} with a dead"
                  " end that is the only link between copper of its net, to"
                  " the last copper it meets there.", ""]
    for key, head in (("failed", "Would not join"),
                      ("worse", "Worse than before")):
        if p.get(key):
            lines += [f"{head}:", ""] + [f"- {x}" for x in p[key]] + [""]
    nv = p.get("new_violations") or []
    if nv:
        lines += ["New violations (on work/, none like them before):", ""] \
            + [f"- {_vline(v)}" for v in nv[:SHOWN]]
        if len(nv) > SHOWN:
            lines.append(f"- and {len(nv) - SHOWN} more in part_repair.json")
        lines.append("")
    if p.get("unread_dru_rules"):
        how = "; --allow-unread-rules routed anyway and left them to DRC." \
            if p.get("allow_unread_rules") else ", so the run stopped."
        lines += ["The .kicad_dru's disallow rule(s) "
                  + ", ".join(p["unread_dru_rules"])
                  + " have a condition the router doesn't read" + how, ""]
    if p.get("drc"):
        b, a = p["drc"]["before"], p["drc"]["after"]
        lines += [f"DRC total {b.get('total')} -> {a.get('total')}.", ""]
    if p.get("replay"):
        lines += ["Replay on the board, in order:", "", "```"] \
            + p["replay"] + ["```", ""]
    if p.get("notes"):
        lines += ["Notes:", ""] + [f"- {x}" for x in p["notes"]] + [""]
    lines.append(f"Applied to the board: {'yes' if p['applied'] else 'no'}.")
    return "\n".join(lines) + "\n"


def load_ops(ops_file: str | None, inline: list[str]) -> list[dict]:
    """place_edit's footprint ops, validated; swap_fp paths made absolute."""
    ops = place_edit.load_ops(ops_file, inline)
    bad = sorted({op["op"] for op in ops
                  if op["op"] not in place_edit.FOOTPRINT_OPS})
    if bad:
        raise CheckError(f"part_repair takes footprint ops only"
                         f" ({', '.join(sorted(place_edit.FOOTPRINT_OPS))}),"
                         f" not {', '.join(bad)}: run place_edit for those")
    for op in ops:
        if op["op"] == "swap_fp":
            op["footprint"] = str(Path(op["footprint"]).expanduser()
                                  .resolve())
    return ops


def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--ops", default=None, help="ops file {version: 1, ops}")
    ap.add_argument("--op", action="append", default=[],
                    help="one op (or a list) as JSON; repeatable")
    ap.add_argument("--out", required=True,
                    help="work dir: boards, op files and the report")
    ap.add_argument("--margin", type=float, default=MARGIN,
                    help="mm round each part's old and new pads that may be"
                         f" ripped (default {MARGIN:g})")
    ap.add_argument("--keep", action="append", default=[],
                    help="a net whose copper must not be ripped; repeatable")
    ap.add_argument("--apply", action="store_true",
                    help="on a pass, put the repaired board in place of"
                         " --pcb")
    ap.add_argument("--allow-unread-rules", action="store_true",
                    help="route even when the .kicad_dru has a disallow rule"
                         " whose condition the router doesn't read (the DRC"
                         " gate judges it); without this the run stops")
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)
    if args.margin <= 0:
        raise CheckError("--margin must be more than 0")
    ops = load_ops(args.ops, args.op)
    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    out = Path(args.out).resolve()
    if out == pcb.parent or out in pcb.parents:
        raise CheckError("--out must be a directory of its own, not the"
                         " board's or one above it")
    payload = repair(pcb, ops, out, margin=args.margin, keep=args.keep,
                     apply=args.apply, allow_unread=args.allow_unread_rules)
    return checklib.stamp(payload, pcb), args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("part_repair", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
