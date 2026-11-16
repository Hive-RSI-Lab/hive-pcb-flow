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
03-route.route_edit.json (prunes, pushes and orphan patch zones removed,
then the new routes), which replay on --pcb in this order:

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
old pad and meets no new pad of its net is named in the report's notes, as
is one base/B's refill leaves filling nothing (left over: zone_patch
--remove it, #1424); one
the run leaves joining no copper of its net at all (DRC's isolated copper,
one more island of the net) is removed in 03-route, and the report names
the pads it met. A pad the run leaves starved, its pour reaching it by one
spoke (as when the prune takes copper that held the pour off it), gets a
track of its net on that spoke, from its centre into the fill: KiCad's DRC
counts the track as a second spoke, but it adds no new path, so the report
lists the pad as a waived thermal, not a fixed one. Only a pad the run did
not move, with no fewer spokes from that zone than it had before, gets
one, and the tracks stay only if DRC then finds nothing new; any other
starved pad fails the gate, and the notes say why.
Teardrops (#1138): each new via takes the teardrop settings of the nearest
via of its net the run removes (ripped, pushed, or pruned before the route
or by a peel after a refill), when that via had them on (route_edit's
add_via "teardrops"; the replay carries them), and the refill builds the
drops. The joins lay a net at the widest width ripped from it, so a track
they end on such a via can be too wide for its drop: it narrows from the
via to NECK_PAST beyond where the drop ends, to a width the drop takes,
the net's floor allows and the track does not graze the via at (Necks in
the report). A track narrow enough for a drop that grazes the via necks
the same way, to a width under its own that clears the graze; one that
grazes at every such width stays. A neck that stops short of the drop's
end (a branch, a via, board copper) keeps the drop: KiCad lays it on over
the copper past (#1320). When the net's floor forbids every width the
drop takes, the new via grows (teardroplib grow_size, at most 1.5 x) to
a size the floor takes a drop on, where no other net's copper or the
board edge is within its clearance; else the track stays. A via that
takes its teardrops in a peel after the refill narrows its joins there,
the track laid again (#1320), and does not grow (it is on the board).
After the last refill, the check reads
the drops round the repair area (lib/teardroplib.py, modelled on KiCad
10.0.6) and fails the run when, on a layer, fewer tracks into a via have a
drop than had one on the via it is judged by, as far as the tracks into it
now allow: a new via by the via it took its teardrops from, a via on both
boards in the area with teardrops on by itself before (a layer in its own
net's pour counts). It names each track left without one and why: too wide
for one, grazing the copper, or none made. A track that crosses a via's
copper off the centre is not asked for one: KiCad makes only a sliver of a
drop there, with no fillet. A via that took or had teardrops and has them
off fails it too, as does a via a new one landed on (route_edit makes
nothing there) in place of a removed via with teardrops on, when its own
are off. A new via with a track short of a drop and no loss is a note, not
a failure. Not checked: the vias the run removes (one with teardrops on
that passed them to no new via is a note), and pads. DRC has no check for
a missing drop: this is the only one.

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
import re
import shlex
import shutil
import sys
from collections import Counter, deque
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import sexpdata  # noqa: E402
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
import teardroplib as tdl  # noqa: E402
from checklib import CheckError  # noqa: E402
from teardroplib import (  # noqa: E402,F401  (shared, #1320)
    NECK_PAST, neck_lengths, neck_tracks, neck_widths)

MARGIN = 2.0   # mm round a part's old and new pads: the rip window
TOL = 1e-3     # mm
SHOWN = 40     # new violations listed in REPORT.md (all in part_repair.json)
TOUCH = 1e-3   # mm: copper this close is one piece (as net_islands has it)
NEAR = 0.05    # mm: a neighbour query's reach, past every rule's tol
REPRUNE = 3    # peels after a refill, at most (#1140)
CUT_MIN = 0.01  # mm: a dead end cut back leaves this much track, at least
LOST_NEAR = 1.0  # mm: how near what cut a pour back from copper is named
CUT_CAP = 4     # cuts of one track in one peel, at most
TD_READ = 3.0  # mm past the repair area the teardrop check reads (#1138)
TD_SHOWN = 8   # vias short of drops named in the gate's line
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


def _td_params(p: dict | None) -> dict | None:
    """A via's teardroplib.params_of as add_via's teardrops (#1138); None
    when its teardrops are off. A value route_edit would refuse (negative,
    not a number) is left out, so the default stands for it; a zero is
    copied as it is (KiCad 10.0.6: max_length 0 sets no cap, max_width 0
    makes no drop; the 10.0 branch reads max_width 0 as no limit)."""
    if not p or not p.get("enabled"):
        return None
    out = {}
    for k, d in tdl.ITEM_PARAMS.items():
        v = p.get(k)
        if isinstance(d, bool):
            if isinstance(v, bool):
                out[k] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool) \
                and math.isfinite(v) and v >= 0:
            out[k] = float(v)
    return out


def _lands_on(op: dict, vias) -> str | None:
    """The uuid of the via add_via op lands on, which route_edit answers
    "exists" without making one (same net and span, within TOL); None when
    it lands on none."""
    span = {op.get("top", "F.Cu"), op.get("bot", "B.Cu")}
    x, y = op["at"]
    for v in vias:
        if v.net == op["net"] and abs(v.at[0] - x) <= TOL + 1e-9 \
                and abs(v.at[1] - y) <= TOL + 1e-9 \
                and {v.layers[0], v.layers[-1]} == span:
            return v.uuid or ""
    return None


def restore_teardrops(adds: list, lost: list, params: dict, vias,
                      landed: dict | None = None) -> list:
    """Teardrops for the run's new vias (#1138): each add_via takes those
    of the nearest via of its net the run takes off the board (lost; the
    lower uuid on a tie), op "teardrops" set in place, when that via had
    them on; one that had them off leaves the add without (route_edit's
    default: off). Returns, per op in adds, the uuid of the via it took
    them from, or None. params: uuid -> teardroplib.params_of on the base
    board; vias: the work board's, less those the same op list removes (on
    one of those route_edit makes a new via). An add that lands on a via
    already there takes none: route_edit would set them on that via and
    make nothing, and the replay leaves such an add out, so it would differ
    from the work board. landed (when given) takes that via's uuid -> the
    lost via's, for the check to see the via in its place has them on."""
    by_net: dict = {}
    for v in lost:
        by_net.setdefault(v.net, []).append(v)
    src: list = []
    for op in adds:
        cands = by_net.get(op.get("net")) if op["op"] == "add_via" else None
        if not cands:
            src.append(None)
            continue
        v = min(cands, key=lambda c: (math.dist(c.at, op["at"]),
                                      c.uuid or ""))
        p = _td_params(params.get(v.uuid))
        on = _lands_on(op, vias) if p is not None else None
        if p is None or on is not None:
            if on is not None and landed is not None:
                landed[on] = v.uuid
            src.append(None)
            continue
        op["teardrops"] = dict(p)
        src.append(v.uuid)
    return src


def late_teardrops(kept: list, made: dict, td_src: list, gone: list,
                   params: dict, skip) -> list:
    """restore_teardrops for a re-prune after the route (#1138), which takes
    vias off the board after the new vias are made: each add_via in made
    (the uuid it made -> its index in kept) that took no teardrops yet
    (td_src None) and is not in skip (indices going now or gone) takes
    them from the nearest via in gone (every via the run has taken off,
    this pass's too), op "teardrops" and td_src set in place. Returns
    those add_vias, to send again: route_edit sets the teardrops on the
    via the op made ("exists"), and the replay's op has them from the
    start. Every setting is spelled out: on a via already there route_edit
    keeps the via's own value for a key left out, on a new via the
    default."""
    idx = sorted(i for i in made.values() if i not in skip
                 and td_src[i] is None and kept[i]["op"] == "add_via")
    again = []
    for i, s in zip(idx, restore_teardrops([kept[i] for i in idx], gone,
                                           params, [])):
        if s is not None:
            td_src[i] = s
            kept[i]["teardrops"] = dict(tdl.ITEM_PARAMS,
                                        **kept[i]["teardrops"])
            again.append(dict(kept[i]))
    return again


def _via_room(v: dict, size: float, rules, bgw, kept: list, i: int,
              going, moved: dict) -> bool:
    """True when add_via v (kept[i]) at this size keeps its clearance to
    every copper item of another net on its layers (#1320, a via that
    grows for its drop): bgw's tracks, vias and pads less the uuids in
    going and moved, moved's cut segments (uuid -> (was, Seg)), the other
    adds in kept; and the board edge (bgw.outline) at edge_clearance."""
    net, c, r = v["net"], Point(_pt2(v["at"])), size / 2
    cu = list(bgw.copper_layers)
    lys = set(_via_layers(v, cu))

    def hits(shape, other, half) -> bool:
        return shape.distance(c) < r + half + rules.clr(net, other) - 1e-9

    for _, s in moved.values():
        if s.net != net and s.layer in lys \
                and hits(LineString([s.a, s.b]), s.net, s.width / 2):
            return False
    for it in bgw.tracks_of() + bgw.vias_of() + bgw.pads_of():
        u = getattr(it, "uuid", None)
        if it.net == net or u in going or u in moved:
            continue
        on = getattr(it, "layers", None) or (it.layer,)
        if lys & set(on) and hits(it.poly, it.net, 0.0):
            return False
    for k, o in enumerate(kept):
        if k == i or o.get("net") == net:
            continue
        if o["op"] == "add_track" and o["layer"] in lys and hits(
                LineString([_pt2(o["start"]), _pt2(o["end"])]), o["net"],
                float(o["width"]) / 2):
            return False
        if o["op"] == "add_via" and lys & set(_via_layers(o, cu)) and hits(
                Point(_pt2(o["at"])), o["net"], float(o["size"]) / 2):
            return False
    out = bgw.outline
    return out is None or out.is_empty or out.boundary.distance(c) >= \
        r + rules.edge_clearance + rules.margin - 1e-9


def _pt2(e) -> tuple:
    return (float(e[0]), float(e[1]))


def _late_necks(kept: list, td_src: list, fresh, made: dict, skip,
                width_of, board_at) -> tuple[list, list, dict]:
    """neck_tracks for the vias in fresh (kept indices that took their
    teardrops in a peel after the refill, late_teardrops) alone, on the
    routes of the run that are on the board (made: uuid -> kept index);
    ops in skip stay as they are (cut back, taken off, already there).
    kept and td_src change in place. Returns (route_edit ops: each
    narrowed track removed and laid again, a split's far piece laid,
    the neck entries, the kept index of each add op by its position in
    the ops)."""
    mask = [s if i in fresh else None for i, s in enumerate(td_src)]
    n0 = len(kept)
    was = list(kept)
    necks = neck_tracks(kept, mask, skip, width_of, board_at)
    td_src += [None] * (len(kept) - len(td_src))
    by_i = {i: u for u, i in made.items()}
    ops: list = []
    at: dict = {}
    for k in range(n0):
        if kept[k] is was[k] or k not in by_i:
            continue
        ops.append({"op": "remove", "uuid": by_i[k]})
        at[len(ops)] = k
        ops.append(dict(kept[k]))
    for k in range(n0, len(kept)):
        at[len(ops)] = k
        ops.append(dict(kept[k]))
    return ops, necks, at


def unplaced_teardrops(gone: list, params: dict, placed,
                       on_work) -> list:
    """The vias the run took off the board with teardrops on that passed
    them to no via on the work board (#1138): no new via of their net
    took them (each takes its nearest's, restore_teardrops) or the one
    that did was pruned, and no via landed in their place. Their drops
    went with them: a note, not a failure, as the check judges the vias
    on the work board. gone: every via the run took off; params: uuid ->
    teardroplib.params_of on the base board; placed: the uuids of the
    removed vias a via on the work board took them from or stood in for;
    on_work: the work board's via uuids. Entries of via, net, at."""
    out, seen = [], set()
    for v in sorted(gone, key=lambda v: (v.uuid or "")):
        if not v.uuid or v.uuid in seen:
            continue
        seen.add(v.uuid)
        if v.uuid in on_work or v.uuid in placed \
                or _td_params(params.get(v.uuid)) is None:
            continue
        out.append({"via": v.uuid, "net": v.net,
                    "at": [round(v.at[0], 4), round(v.at[1], 4)]})
    return out


def td_read(params) -> float:
    """mm past the repair area the teardrop check reads (#1138): TD_READ, or
    2.5 x the longest max_length_mm of the items with teardrops on (params:
    their params_of), so the item a drop that touches a via in the area
    belongs to is read too; else the drop is credited to that via."""
    longest = [p.get("max_length_mm") for p in params
               if p and p.get("enabled")]
    longest = [v for v in longest if isinstance(v, (int, float))
               and not isinstance(v, bool) and math.isfinite(v)]
    return max([TD_READ] + [2.5 * v for v in longest])


def _td_entry(it, layers, why: str) -> dict:
    return {"via": it.uuid, "net": it.net,
            "at": [_r(it.at[0]), _r(it.at[1])], "layers": layers, "why": why}


def _td_line(e: dict) -> str:
    return (f"{e['net']} via at ({e['at'][0]:.3f}, {e['at'][1]:.3f})"
            f" {e['why']}")


_TD_WHY = {"no drop": "has none", "grazes": "grazes the copper"}


def _td_reasons(it, layer: str) -> str:
    """Each track into it (a teardroplib Item) on layer that no drop
    answers, and why it has none (Item.unanswered). Too wide comes first:
    a track too wide for a drop on the via gets none whatever its line,
    and one that ends on the centre of a via narrower than itself grazes
    the copper only by its width."""
    out = [f"a {w:.4g} mm track " + (
        f"is too wide for a drop on a {it.min_size_mm:g} mm via"
        if why == "too wide" or it.too_wide(w) else _TD_WHY[why])
        for w, why in sorted(it.unanswered(layer))]
    return ", ".join(out) or \
        f"{it.into(layer) - it.answered(layer)} without one"


def _td_widths(it, layer: str) -> str:
    """' (a 0.2 mm track)': the widths of the tracks on layer a drop
    answers on it (Item.answered_widths); '' when unknown."""
    ws = [f"{w:.4g}" for w in it.answered_widths(layer)]
    if not ws:
        return ""
    if len(ws) == 1:
        return f" (a {ws[0]} mm track)"
    return f" ({', '.join(ws[:-1])} and {ws[-1]} mm tracks)"


def _td_judge(it, ref, against: str, d0) -> tuple[list, list]:
    """(lost, short) of the work board's it against ref, each a list of
    (layer, why). On a layer, fewer tracks into it that a drop answers
    (Item.answered, at any width) than on ref, or than the tracks into it
    now (Item.into) if fewer, is lost; with ref None, a join no drop
    answers (Item.deficit) is. More joins no drop answers than d0(layer)
    and nothing lost is short."""
    lost, short = [], []
    for layer in sorted(set(it.cands) | set(it.joins) | set(it.grazes)):
        a1, n1, d1 = it.answered(layer), it.into(layer), it.deficit(layer)
        a0 = ref.answered(layer) if ref is not None else None
        why = (f"on {layer} {a1} of {n1} tracks into it have a drop"
               + (f", against {a0} {against}{_td_widths(ref, layer)}"
                  if ref is not None else "")
               + f": {_td_reasons(it, layer)}")
        if (a1 < min(a0, n1)) if ref is not None else d1 > 0:
            lost.append((layer, why))
        elif d1 > d0(layer):
            short.append((layer, why))
    return lost, short


def _td_add(out: list, it, rows: list) -> None:
    if rows:
        out.append(_td_entry(it, [layer for layer, _ in rows],
                             "; ".join(why for _, why in rows)))


def teardrop_loss(b0, b1, new_src: dict, area,
                  landed: dict | None = None) -> tuple[list, list]:
    """The drops the run lost (#1138), from teardroplib.read of the base
    (b0) and work (b1) boards round the repair area: (lost, short), entries
    of via, net, at, layers, why. Judged per layer by the tracks into a
    via that a drop answers, at any width (a layer in the via's own net's
    pour counts, as KiCad makes the drop there too): fewer than on the via
    it is judged by, or than the tracks into it now if fewer, is lost, and
    each track without a drop is named with why (too wide, grazing the
    copper, or none made); a track crossing the copper off the centre is
    not into the via (teardroplib Item.into). A new via that took
    a lost via's teardrops (new_src: its uuid -> that via's) is judged by
    that via and must have them on; a join of it with no drop and nothing
    lost is short. A via on both boards, in the area, with teardrops on on
    base, is judged by itself there and must keep them on; more joins with
    no drop than before and nothing lost is short. landed: the uuid of a
    via a new one landed on (restore_teardrops) -> the lost via it stood
    in for, which had teardrops on; with its own off, it is lost too."""
    v0 = {it.uuid: it for it in b0.items if it.kind == "via" and it.uuid}
    v1 = {it.uuid: it for it in b1.items if it.kind == "via" and it.uuid}
    lost, short = [], []
    for u, s in sorted(new_src.items()):
        it = v1.get(u)
        if it is None:  # the re-prune took it
            continue
        if not it.enabled:
            lost.append(_td_entry(it, [], "has its teardrops off"))
            continue
        gone, thin = _td_judge(it, v0.get(s), "on the via it took its"
                               " teardrops from", lambda layer: 0)
        _td_add(lost, it, gone)
        _td_add(short, it, thin)
    for u, it0 in sorted(v0.items()):
        it1 = v1.get(u)
        if it1 is None or not it0.enabled or u in new_src \
                or not it0.poly.intersects(area):
            continue
        if not it1.enabled:
            lost.append(_td_entry(it1, [], "had teardrops on and has them"
                                  " off"))
            continue
        gone, thin = _td_judge(it1, it0, "before", it0.deficit)
        _td_add(lost, it1, gone)
        _td_add(short, it1, thin)
    told = {e["via"] for e in lost}
    for u, s in sorted((landed or {}).items()):
        it = v1.get(u)
        if it is None or it.enabled or u in told:
            continue
        lost.append(_td_entry(
            it, [], f"has its teardrops off, and the route's via for a"
            f" removed one with them on ({s}) landed on it: route_edit"
            ' add_via on it with "teardrops": {} turns them on'))
    return lost, short


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


def patch_notes(bg, old: dict, new: dict, uuids: bool = False) -> list:
    """PATCH_<net> zones whose fill met an old pad of their net and meets
    no new one: zone_patch's to reshape. A patch layer bg's refill left
    empty is named too (#1424): it fills nothing before the run, so it is
    left over, zone_patch's to remove. uuids=True: (zone uuid, note)
    pairs, so a note can go with its zone (orphan_patches)."""
    notes = []
    for z in bg.zones_of():
        if not z.name.startswith("PATCH_") or z.teardrop:
            continue
        for ly in dict.fromkeys(list(z.layers) + list(z.fills)):
            if ly not in bg.copper_layers:
                continue
            fill = z.fill_on(ly)
            if fill.is_empty:
                note = (f"zone {z.name} on {ly} fills nothing after the"
                        f" refill: left over, remove it with zone_patch"
                        f" --net {z.net} --layer {ly} --region "
                        f"{geom.patch_region(z.outline)} --remove")
                notes.append((z.uuid, note) if uuids else note)
                continue
            for ref in old:
                met = [p for p in old[ref] if p.net == z.net
                       and ly in p.layers and fill.intersects(p.poly)]
                meets = [p for p in new.get(ref, []) if p.net == z.net
                         and ly in p.layers and fill.intersects(p.poly)]
                if met and not meets:
                    note = (f"zone {z.name} on {ly} met {ref}."
                            f"{met[0].number} and meets no new pad of"
                            f" {z.net}: reshape it with zone_patch --net"
                            f" {z.net} --layer {ly} (or --remove)")
                    notes.append((z.uuid, note) if uuids else note)
    return notes


def _meets_net(bg, z, ly: str) -> bool:
    """Does zone z's fill on ly meet other copper of its net there: a
    track, via or pad of the net on ly, or another zone's fill?"""
    fill = z.fill_on(ly)
    if fill.is_empty:
        return False
    grown = fill.buffer(TOUCH)
    items = bg.tracks_of(net=z.net, layer=ly) + bg.vias_of(
        net=z.net, layer=ly) + bg.pads_of(net=z.net, layer=ly)
    if any(grown.intersects(it.poly) for it in items):
        return True
    return any(o is not z and grown.intersects(o.fill_on(ly))
               for o in bg.zones_of(net=z.net, layer=ly)
               if o.fills.get(ly))


def orphan_patches(bg0, bg1) -> list:
    """(zone, pads) for each PATCH_<net> zone (zone_patch) that met copper
    of its net on bg0 and whose fill on bg1 meets none on any layer: the
    run's moves left it joining nothing, and DRC finds it as isolated
    copper. pads: the "REF.N" of the net's pads its bg0 fill met. A zone
    is the same on both boards by its uuid; one without a uuid stays."""
    base = {z.uuid: z for z in bg0.zones_of() if z.uuid}
    out = []
    for z in bg1.zones_of():
        z0 = base.get(z.uuid) if z.uuid else None
        if z0 is None or z.teardrop or not z.name.startswith("PATCH_") \
                or not z.filled \
                or any(_meets_net(bg1, z, ly) for ly in z.fills) \
                or not any(_meets_net(bg0, z0, ly) for ly in z0.fills):
            continue
        pads = sorted({f"{p.ref}.{p.number}" for ly in z0.fills
                       for p in bg0.pads_of(net=z0.net, layer=ly)
                       if z0.fill_on(ly).buffer(TOUCH).intersects(p.poly)})
        out.append((z, pads))
    return out


def _drop_patch(z, pads: list, gone: list, marks: list, payload: dict,
                early: list) -> None:
    """Records that orphan patch zone z (orphan_patches) goes: its uuid in
    gone (the route ops remove it, so the replay does), where its fill was
    in marks (the gate looks there), a line in payload["removed_zones"],
    and a note in place of patch_notes' early ones about it."""
    lys = sorted(ly for ly, fs in z.fills.items() if fs)
    gone.append(z.uuid)
    marks.append(shapely.union_all([z.fill_on(ly) for ly in lys])
                 .buffer(1.0))   # repair_area's margin
    payload["removed_zones"].append({"uuid": z.uuid, "name": z.name,
                                     "net": z.net, "layers": lys,
                                     "met": pads})
    payload["notes"] = [n for n in payload["notes"] if n not in early]
    met = ", ".join(pads)
    payload["notes"].append(
        f"zone {z.name} on {'/'.join(lys)} joins no copper of {z.net}"
        f" after the run, where it met {met or 'copper of its net'} before:"
        " removed, as DRC finds it isolated copper"
        + (f"; if {met} needs a patch, build one with zone_patch --net"
           f" {z.net} --layer {lys[0]}" if met else ""))


# KiCad's pad description (PAD::GetItemDescription): "Pad 2 [GND] of
# C210 on B.Cu", "PTH pad 1 [GND] of J1". A zone's own description lists
# its layers ("on F.Cu and B.Cu", "on F.Cu, In1.Cu and B.Cu"), past three
# only the first ("on F.Cu and 3 more"), so the layer is read from the
# violation's text instead
# (drc_test_provider_zone_connections.cpp in KiCad 10.0.6): "Thermal
# relief connection to zone incomplete (layer B.Cu; zone min spoke count
# 2; actual 1)", by the board's own name for the layer.
_PAD_RE = re.compile(r"^(?:PTH pad|Pad) (.+?) \[.*\] of (\S+)(?: on (.+))?$")
_STARVED_LAYER_RE = re.compile(r"\(layer (.+?);")


def layer_names(bg) -> dict:
    """{a name KiCad's DRC may give a layer of bg: the canonical name}: a
    renamed layer's own name (`(1 "In1.Cu" signal "SIG1")`), and a KiCad 5
    board's user name for a copper layer (geom's layer_aliases)."""
    names = {own: canon for canon, own in bg.layer_user_names.items()}
    names.update(bg.layer_aliases)
    return names


def starved_pads(drc0: dict, drc1: dict, names: dict | None = None) -> list:
    """(ref, pad number, layer, zone uuid) of each starved thermal on the
    after board that no before violation matches (unmatched). The layer
    is the one the violation's text names, else the pad's (an SMD pad's
    description names it), mapped to its canonical name by names
    (layer_names); None when neither names one."""
    names = names or {}
    out = []
    for v in unmatched(drc0, drc1):
        if v.get("check") != "starved_thermal":
            continue
        pad = zone = layer = None
        for it in v.get("items") or []:
            msg = it.get("msg") or ""
            m = _PAD_RE.match(msg)
            if m:
                pad, layer = (m.group(2), m.group(1)), m.group(3)
            elif msg.startswith("Zone"):
                zone = it.get("uuid")
        m = _STARVED_LAYER_RE.search(v.get("msg") or "")
        if m:
            layer = m.group(1)
        if pad and zone:
            out.append((*pad, names.get(layer, layer) if layer else None,
                        zone))
    return out


SPOKE_REACH = 10     # steps of SPOKE_STEP mm past the spoke's end, at most
SPOKE_STEP = 0.05
SPOKE_NARROW = 0.02  # mm a spoke track narrows by a try, to the width floor


def _at(p) -> tuple:
    return (round(p.center[0], 3), round(p.center[1], 3))


def _unmoved(p, bg0) -> bool:
    """Whether bg0 has pad p as it is: a pad of its ref and number on the
    same layers with the same copper."""
    return any(q.number == p.number and set(q.layers) == set(p.layers)
               and q.poly.symmetric_difference(p.poly).area < 1e-6
               for q in bg0.pads_of(ref=p.ref))


def spoke_tracks(bg0, bg, starved: list, rules) -> tuple[list, list, list]:
    """(add_track ops, notes, waived) for the starved thermals of bg
    (starved_pads) that one live spoke joins: a track of the pad's net
    from its copper centre along that spoke into the fill. KiCad's DRC
    counts a net track from inside the pad's ring to the fill as a spoke
    when the live ones fall short (rf.Obstacles.spoke_count), so the pad
    passes with two. The track lies on the spoke's own copper and adds no
    new path: it waives the check, it does not mend the join. So it is
    laid only where the run is not what starves the pad: on a pad the run
    did not move (bg0, the board before the run, has it with the same
    copper on the same layers) that has no fewer spokes from that zone on
    that layer than it had on bg0 (the run took copper that held the pour
    off it, C210.2 on the voice board: 0 spokes before, 1 after). Each
    track is an entry of waived: pad, layer, zone, net, spokes [before,
    after] and the op; the caller notes those. rules: the router's
    (width_floor, net_layers). A pad that gets no track gets a note."""
    obs = rf.Obstacles(bg, spokes=True)
    obs0 = rf.Obstacles(bg0, spokes=True)
    was = {(p.ref, p.number, layer, z.uuid, _at(p)): i
           for i, (p, z, layer, *_) in enumerate(obs0._thermal_index())}
    want = set(starved)
    found = set()
    ops, notes, waived = [], [], []
    for i, (p, z, layer, gap, sw, mt) in enumerate(obs._thermal_index()):
        key = (p.ref, p.number, layer, z.uuid)
        if key not in want:
            continue
        found.add(key)
        n = obs.spoke_count(i)
        i0 = was.get((*key, _at(p)))
        n0 = -1 if i0 is None else obs0.spoke_count(i0)
        where = f"{p.ref}.{p.number} on {layer}"
        live = [s for s in rf.thermal_spokes(
            p, z.fills.get(layer, ()), gap, sw, mt,
            joined=obs._joined(p, z, layer), mids=True) if s[0][0]]
        why, op = "", None
        if not _unmoved(p, bg0):
            why = ("the run moved it, so the run starves it and a track"
                   " would only pass the count")
        elif p.custom is not None and not p.custom.is_empty:
            why = "it is a custom pad (KiCad's proxy spokes)"
        elif not 0 < n < rf.SPOKES_MIN:
            why = f"the spoke model counts {n}"
        elif n < n0:
            why = (f"the run cut its spokes from {n0} to {n}, so a track"
                   " would only pass the count")
        elif not live:
            why = "no live spoke into fill that meets other copper of it"
        elif not rules.net_layers(p.net, [layer]):
            why = f"the .kicad_dru keeps {p.net} off {layer}"
        else:
            op = _spoke_track(p, z, layer, gap, sw, mt, live[0][2], rules)
        if op is None:
            notes.append(
                f"{where} starves (DRC: fewer thermal spokes from"
                f" {z.name or z.net} than it asks) and no track was laid on"
                f" its spoke: {why or 'none fits in the spoke and the fill'};"
                " move the copper that holds the pour off it, or join the"
                " pad solid")
            continue
        ops.append(op)
        waived.append({"pad": f"{p.ref}.{p.number}", "layer": layer,
                       "zone": z.name or z.uuid, "net": p.net,
                       "spokes": [max(n0, 0), n], "track": op})
    for r, num, ly, _ in sorted(want - found,
                                key=lambda k: (k[0], k[1], k[2] or "")):
        notes.append(
            f"{r}.{num} starves, but DRC names no layer for it, so no track"
            " was laid on its spoke" if ly is None else
            f"{r}.{num} on {ly} starves, but the spoke model finds no"
            " thermal pad like it short of spokes to lay a track for")
    return ops, notes, waived


def _spoke_track(p, z, layer, gap, sw, mt, mid, rules):
    """The add_track op from pad p's copper centre along its spoke through
    mid (thermal_spokes) to the first point past the spoke's end that is
    in z's fill, if the track lies in the fill and the pad; else None. It
    is as wide as the spoke less 0.02 mm where that fits; where other
    copper notches the fill along the spoke (C210.2 on the voice board: a
    via's clearance), the widest that fits, SPOKE_NARROW mm a try, down to
    the net's width floor. DRC counts the track as a spoke at any width."""
    end = rf.spoke_end(p, mid, gap, mt)
    if end is None:
        return None
    (ox, oy), (ux, uy) = end
    fill = z.fill_on(layer)
    inner = fill.buffer(-TOUCH)
    room = shapely.union(fill, p.poly).buffer(1e-4)
    floor = rules.width_floor(p.net)
    w0 = max(floor, min(sw, min(p.size)) - 0.02)
    widths = [max(floor, round(w0 - k * SPOKE_NARROW, 4))
              for k in range(int((w0 - floor) / SPOKE_NARROW + 1e-9) + 1)]
    if widths[-1] > floor:
        widths.append(floor)
    a = tuple(p.copper_center)
    ends = [b for b in ((ox + ux * k * SPOKE_STEP, oy + uy * k * SPOKE_STEP)
                        for k in range(SPOKE_REACH + 1))
            if inner.contains(Point(b))]
    for w in widths:
        for b in ends:
            if room.contains(LineString([a, b]).buffer(w / 2)):
                return {"op": "add_track", "net": p.net, "layer": layer,
                        "start": [round(a[0], 4), round(a[1], 4)],
                        "end": [round(b[0], 4), round(b[1], 4)],
                        "width": w}
    return None


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
    patch_pairs = patch_notes(bg0, old, new, uuids=True)
    payload["notes"] += [n for _, n in patch_pairs]

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
            " it. If DRC still finds it starved at the end, the run lays a"
            " track on its live spoke, which DRC counts as a second but"
            " which adds no new path: the fix is to move that copper or the"
            " part, or join the pad solid")
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

    gone_zones: list = []   # PATCH_ zones the run left joining nothing
    zone_marks: list = []   # and where their fill was (the gate's area)

    def ops_now():
        return [{"op": "remove", "uuid": u}
                for u in dict.fromkeys(pushed + pruned + gone_zones)] + [
            o for u, (was, new) in moved.items()
            for o in _set_ends(u, was, new)] + kept

    # a new via on a net whose removed vias had teardrops gets them back,
    # set as on the nearest of those (#1138)
    lost = {it.uuid: it for it in ripped
            if not isinstance(it, geom.Track) and it.uuid}
    for u in pushed + pruned:
        if isinstance(by_uuid.get(u), geom.Via):
            lost.setdefault(u, by_uuid[u])
    root0 = sexpdata.loads(base.read_text(encoding="utf-8"))
    td_params = tdl.via_params(root0)
    td_reach = td_read(list(td_params.values())
                       + list(tdl.pad_params(root0).values()))
    del root0
    # the route's list removes pushed and pruned: an add on one of those
    # makes a new via (route_swig), so it lands on none of them
    going = set(pushed + pruned)
    td_landed: dict = {}
    td_src = restore_teardrops(kept, list(lost.values()), td_params,
                               [v for v in bgw.vias_of()
                                if v.uuid not in going], td_landed)
    td_gone = list(lost.values())

    # the joins lay a net at the widest width ripped from it, so a track
    # can end on a new via too wide for the drop the removed via had: it
    # narrows there, to a width the drop takes (#1138)
    def neck_w(i, p):
        # a via that takes its teardrops in a peel after the refill takes
        # them from a via that peel took off, not one in lost
        v = kept[i]
        src = lost.get(td_src[i]) or by_uuid.get(td_src[i])
        sp = dict(tdl.ITEM_PARAMS, **(_td_params(
            td_params.get(td_src[i])) or {}))
        widths = [] if src is None else [
            t.width for t in bg0.tracks_of(v["net"])
            if tdl.takes_drop(t.width, src.diameter, sp)
            and min(math.dist(t.shape.coords[0], src.at),
                    math.dist(t.shape.coords[-1], src.at))
            <= src.diameter / 2 + TOL]
        return neck_widths(float(v["size"]), p, widths,
                           router.rules.width(v["net"]),
                           router.rules.width_floor(v["net"]))

    # board copper of the net at a point ends a neck's walk there: the
    # work board's, less what the run takes off, with the prune's cuts
    def board_at(net, layer, pt) -> bool:
        q = Point(pt)
        for u, (_, s) in moved.items():
            if s.net == net and s.layer == layer and LineString(
                    [s.a, s.b]).distance(q) <= s.width / 2 + TOL:
                return True
        return any(it.poly.distance(q) <= TOL
                   for it in bgw.tracks_of(net, layer)
                   + bgw.vias_of(net, layer) + bgw.pads_of(net, layer)
                   if getattr(it, "uuid", None) not in going
                   and getattr(it, "uuid", None) not in moved)

    # a via whose net's floor forbids every width its drop takes grows to
    # a size the floor takes a drop on (#1320), where the board leaves it
    # room: no copper of another net (the work board's, less what the run
    # takes off, the prune's cuts, the run's own adds) within its new edge
    # plus the clearance, and the board edge at its clearance. Pours
    # refill round it.
    def grow_via(i, p):
        v = kept[i]
        net, rules = v["net"], router.rules
        s = tdl.grow_size(float(v["size"]), p, rules.width_floor(net))
        if s is None:
            return None
        return s if _via_room(v, s, rules, bgw, kept, i, going, moved) \
            else None

    necks = neck_tracks(kept, td_src, shorter, neck_w, board_at, grow_via)
    payload["necks"] = neck_lengths(necks, kept, set())
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
        return shapely.union_all([repair_area(reach, adds, ripped + [
            by_uuid[u] for u in dict.fromkeys(pushed + pruned + list(moved))
            if u in by_uuid])] + zone_marks)

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
    payload["removed_zones"] = []
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
        # a PATCH_<net> zone the moves left joining no copper of its net
        # is a dead end too: DRC finds it as isolated copper, net_islands
        # as one more island of its net (#1140)
        orphans = orphan_patches(bg0, bg1)
        if not dead and not late and not orphans:
            break
        if npass == REPRUNE:
            left = set(dead) | set(late) | {z.uuid for z, _ in orphans}
            payload["notes"].append(
                f"after {REPRUNE} refills the prune still finds dead "
                f"copper: {', '.join(sorted(left))}")
            break
        step = [{"op": "remove", "uuid": u} for u in dead]
        step += [{"op": "remove", "uuid": z.uuid} for z, _ in orphans]
        for z, pads in orphans:
            _drop_patch(z, pads, gone_zones, zone_marks, payload,
                        [n for u, n in patch_pairs if u == z.uuid])
        for u, (was, new) in late.items():
            step += _set_ends(u, was, new)
            if u in made:   # a route of this run: its add op is cut back
                i = made[u]
                ends = (tuple(kept[i]["start"]), tuple(kept[i]["end"]))
                kept[i] = dict(kept[i], start=list(new.a), end=list(new.b))
                shorter[i] = (shorter.get(i, (ends,))[0], (new.a, new.b))
            else:
                moved[u] = (moved.get(u, (was,))[0], new)
        # a via this peel takes off passes its teardrops on to the new vias
        # of its net, as those removed before the route did, when it is
        # the nearest the run took off; the refill below builds the drops
        # (#1138)
        td_gone += [by_uuid[u] for u in dead if u not in made
                    and isinstance(by_uuid.get(u), geom.Via)]
        bare = {i for i, s in enumerate(td_src) if s is None}
        off = {made[u] for u in dead if u in made} | drop
        step += late_teardrops(kept, made, td_src, td_gone, td_params, off)
        # and its joins narrow for the drop, as the first neck had them:
        # a narrowed route of the run is laid again (#1320)
        going.update(u for u in dead if u not in made)
        fresh = {i for i in bare if td_src[i] is not None}
        late_neck = []
        if fresh:
            late_neck = _late_necks(kept, td_src, fresh, made,
                                    set(shorter) | off | dup,
                                    neck_w, board_at)
            necks += late_neck[1]
            at0 = len(step)
            step += late_neck[0]
        res = route_edit.apply_ops(work, step)
        if late_neck:
            for k, op in enumerate(late_neck[0]):
                r = res[at0 + k] if at0 + k < len(res) else {}
                if op["op"] == "remove":
                    made.pop(op["uuid"], None)
                elif r.get("uuid"):
                    made[r["uuid"]] = late_neck[2][k]
        for u in dead:
            if u in made:
                drop.add(made[u])
            else:
                pruned.append(u)
                moved.pop(u, None)
        drc1 = kc.run_drc(cli, work, refill=True, save_board=True)
        bg1 = geom.BoardGeom.from_file(work)
    # a pad the run left starved, its pour reaching it by one spoke now
    # (the prune took copper that held the pour off it): a track of its
    # net on that spoke makes DRC count two (#1140). That waives the
    # check, it does not mend the join, so spoke_tracks lays one only
    # where the run neither moved the pad nor cut its spokes; the tracks
    # stay only when DRC finds nothing new with them, and the report
    # lists each as a waived thermal, not a fixed one
    starved = starved_pads(drc0, drc1, layer_names(bg1))
    payload["spoke_tracks"] = []
    payload["waived_thermals"] = []
    if starved:
        sp_ops, sp_notes, waived = spoke_tracks(bg0, bg1, starved,
                                                router.rules)
        payload["notes"] += sp_notes
        if sp_ops:
            aside = work.with_name(f".{work.name}.spokes")
            shutil.copy2(work, aside)
            route_edit.apply_ops(work, sp_ops)
            drc2 = kc.run_drc(cli, work, refill=True, save_board=True)
            new = unmatched(drc1, drc2)
            if new:
                shutil.copy2(aside, work)
                what = ", ".join(sorted({_key(v) for v in new}))
                payload["notes"] += [
                    f"{w['pad']} on {w['layer']} starves: a"
                    f" {w['track']['width']:.3f} mm {w['net']} track on its"
                    f" spoke was laid and taken back (with the spoke tracks"
                    f" DRC finds {what} that it did not before); move the"
                    " copper that holds the pour off it, or join the pad"
                    " solid" for w in waived]
            else:
                drc1 = drc2
                bg1 = geom.BoardGeom.from_file(work)
                kept += sp_ops
                adds = adds + sp_ops   # the gate's area takes them in
                payload["spoke_tracks"] = sp_ops
                still = {(f"{r}.{num}", ly) for r, num, ly, _ in
                         starved_pads(drc0, drc1, layer_names(bg1))}
                for w in waived:
                    tr = (f"a {w['track']['width']:.3f} mm {w['net']} track"
                          " on its spoke")
                    if (w["pad"], w["layer"]) in still:
                        payload["notes"].append(
                            f"{w['pad']} on {w['layer']} still starves with"
                            f" {tr}; move the copper that holds the pour off"
                            " it, or join the pad solid")
                        continue
                    payload["waived_thermals"].append(w)
                    n0, n = w["spokes"]
                    payload["notes"].append(
                        f"{w['pad']} on {w['layer']}: the run left it {n}"
                        f" spoke{'' if n == 1 else 's'} from {w['zone']},"
                        f" where it had {n0}; {tr} makes DRC count {n + 1}."
                        " That waives the starved-thermal check, it does"
                        " not fix the join: the track adds no new path")
            aside.unlink()
    area = gate_area()
    new_src = {u: td_src[i] for u, i in made.items() if td_src[i]}
    payload["trimmed"] += [kept[i] for i in sorted(drop)]
    payload["shortened"] = [
        _cut_note(u, was.net, was.layer, (was.a, was.b), (new.a, new.b))
        for u, (was, new) in moved.items()] + [
        _cut_note("", kept[i]["net"], kept[i]["layer"], *shorter[i])
        for i in sorted(shorter) if i not in drop | dup]
    # a neck the re-prune cut back or took off reads as it is now
    payload["necks"] = neck_lengths(necks, kept, drop | dup)
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
    # the drops after the refill: DRC has no check for a missing one
    reach_td = area.buffer(td_reach)
    td_lost, td_short = teardrop_loss(tdl.read(base, None, area=reach_td),
                                      tdl.read(work, None, area=reach_td),
                                      new_src, area, td_landed)
    on_work = {it.uuid for it in bg1.vias_of()}
    td_from = {u: s for u, s in new_src.items() if u in on_work}
    td_in = {u: s for u, s in td_landed.items() if u in on_work}
    td_unplaced = unplaced_teardrops(
        td_gone, td_params, set(td_from.values()) | set(td_in.values()),
        on_work)
    payload["teardrops"] = {
        "restored": len(td_from), "from": td_from, "landed": td_in,
        "lost": td_lost, "short": td_short, "unplaced": td_unplaced}
    if td_unplaced:
        payload["notes"].append(
            f"teardrops: {len(td_unplaced)} via(s) the run removed had"
            " teardrops on and passed them to no via on the board (each"
            " new via takes the settings of the nearest via the run"
            " removed of its net; no new via took theirs, or the one"
            " that did was pruned), so their drops went with them: "
            + "; ".join(f"{e['net']} via at ({e['at'][0]:.3f},"
                        f" {e['at'][1]:.3f})"
                        for e in td_unplaced[:TD_SHOWN])
            + (f"; and {len(td_unplaced) - TD_SHOWN} more in"
               " part_repair.json" if len(td_unplaced) > TD_SHOWN else ""))
    if td_short:
        payload["notes"].append(
            "teardrops: a track into a via has no drop, though the via lost"
            " none: " + "; ".join(_td_line(e) for e in td_short))
    if td_lost:
        worse.append(
            f"teardrops: {len(td_lost)} via(s) short of drops the base board"
            " had (DRC does not see it): "
            + "; ".join(_td_line(e) for e in td_lost[:TD_SHOWN])
            + (f"; and {len(td_lost) - TD_SHOWN} more in part_repair.json"
               if len(td_lost) > TD_SHOWN else ""))
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
    for z in p.get("removed_zones") or []:
        lines += [f"Removed zone {z['name']} ({z['net']} on"
                  f" {'/'.join(z['layers'])}): it met"
                  f" {', '.join(z['met']) or 'copper of its net'} and joins"
                  " none after the run (isolated copper).", ""]
    if p.get("waived_thermals"):
        lines += ["Waived thermals: a track of the pad's net on its one"
                  " live spoke makes DRC count one more spoke; it adds no"
                  " path, so each is waived, not fixed:", ""] + [
            f"- {w['pad']} on {w['layer']} ({w['zone']}): spokes"
            f" {w['spokes'][0]} before, {w['spokes'][1]} after; a"
            f" {w['track']['width']:.3f} mm {w['net']} track"
            for w in p["waived_thermals"]] + [""]
    td = p.get("teardrops") or {}
    if td.get("restored") or td.get("lost") or td.get("unplaced"):
        lines += [f"Teardrops: {td.get('restored', 0)} new via(s) took them"
                  " from the vias the run removed"
                  + (f"; {len(td['lost'])} via(s) short of drops the base"
                     " board had (below)" if td.get("lost") else "")
                  + (f"; {len(td['unplaced'])} removed via(s) passed theirs"
                     " to no via (notes)" if td.get("unplaced") else "")
                  + ".", ""]
    if p.get("necks"):
        lines += ["Necks: a track too wide for a drop on a new via that"
                  " took teardrops narrows there, so the drop is made:",
                  ""] + [
            f"- {n['net']} on {n['layer']} at ({n['via'][0]:.3f},"
            f" {n['via'][1]:.3f}): {n['was']:.3f} mm to {n['width']:.3f} mm"
            f" for {n['length']:.3f} mm" + (
                f" (via grown {n['grown'][0]:.3f} to {n['grown'][1]:.3f} mm)"
                if n.get("grown") else "") for n in p["necks"]] + [""]
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
