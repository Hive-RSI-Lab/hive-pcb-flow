"""padfanout - a via and a stub for each named pad (#929).

route_escape fans out ball grids. A QFN's or SOT's power pin, an inductor's
pad or a regulator's pad needs the same thing before the planes go in: a via
next to the pad that clears every other net's copper and hole, and a short
stub from the pad to it on the pad's own layer. This was fanout.py on the
voice board; route_escape --pads calls it now.

For each pad, in the order given:
  - skip it if a via of its net is already within `skip` mm of its centre;
  - look for a via spot outward from the footprint along the pad's long
    axis first, then off the pad's two sides (never inward, under the
    part): steps from the pad's edge out to MAX_DIST_MM, with a small
    sideways offset at each step;
  - a spot must clear every other net's copper on every layer (the
    board's clearance, the net's rule), every hole (hole to hole), every
    other net's copper from the drill (hole clearance), its own net's SMD
    pads by VIA_PAD_KEEP_MM from the drill (route_finish's rule), the board
    edge, and keepout areas. The stub must clear other nets' copper on the
    pad's layer. Zones are ignored: they refill around new copper;
  - with no spot, join the pad to its own net's copper on its layer within
    `join` mm by a straight, 45-degree or L-shaped track;
  - with neither, and via_in_pad set (route_escape --via-in-pad, #940),
    put the via at the pad's centre when the pad's copper holds the whole
    via and the via clears everything above but the pad itself: a via in
    pad, which must be filled and capped (the caller sets that on the via,
    and fab_export's VIA-FILL.md then tells the fab);
  - otherwise report the pad as `no spot`.
A stub or join that lies wholly in its net's neck zone (necklib, rules_gen's
aiee_pwr_neck) is laid at the neck width, not the net's full width (#937):
a fine-pitch power pin gets out between its neighbours.
Each via and stub is an obstacle to the pads after it.

Plane balls (fan(..., in_pad_only=True), route_escape --plane-balls, #956):
a fine-pitch ball grid's power and ground balls have no room for a
dog-bone, so each gets one via at its centre and nothing else: no spot
search, no join. A ball already holding a via of its net is skipped. The
via is `ball_via` (the board's via floor unless given), and only its drill
must sit inside the ball's copper: a 0.5 mm grid's 0.2 mm land is smaller
than any via the fab drills, and the filled, capped via becomes the land.
It must clear everything else as above, except its own net's SMD pads: a
filled via drains no paste, so a decoupler's pad on the far side, under the
ball, is no reason to keep off.

fan(bg, pads, rules, ...) -> (ops, report): route_edit add_track/add_via
ops and one dict per pad.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from shapely.geometry import LineString, Point, box

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import necklib  # noqa: E402
import route_finish as rf  # noqa: E402
from checklib import CheckError  # noqa: E402

STEP_MM = 0.04           # via search step outward from the pad edge
MAX_DIST_MM = 1.4        # the farthest a via goes from the pad edge
LATERAL_MM = (0.0, 0.15, -0.15, 0.3, -0.3)
SKIP_MM = 0.9            # a via of the net this close already fans it out
JOIN_MM = 2.5            # how far the join fallback looks for own copper
NEAR_MM = 3.0            # obstacle query reach


def _r(v: float) -> float:
    return round(v, 4)


def _p(q) -> list[float]:
    return [_r(q[0]), _r(q[1])]


class Fanout:
    def __init__(self, bg, rules=None, *, width: float | None = None,
                 via: tuple[float, float] | None = None,
                 via_in_pad: bool = False,
                 ball_via: tuple[float, float] | None = None):
        self.bg = bg
        self.rules = rules or rf.board_rules(Path(bg.path), bg)
        self.obs = rf.Obstacles(bg)
        self.width = width
        self.via = via or (self.rules.via_size, self.rules.via_drill)
        self.via_in_pad = via_in_pad
        self.ball_via = ball_via or via or self.rules.via_floor
        for size, drill in {self.via, self.ball_via}:
            if drill >= size:
                raise CheckError(f"via drill {drill} must be under its "
                                 f"size {size}")
        self.cu = frozenset(bg.copper_layers)
        out = bg.outline
        self.inside = None if out is None or out.is_empty else out
        self.keepouts = [ra["outline"] for ra in
                         list(getattr(bg, "rule_areas", []))
                         + list(getattr(bg, "footprint_rule_areas", []))
                         if ra.get("outline") is not None]
        self._conn = 0

    # ------------------------------------------------------------ checks

    def _near(self, geom_):
        x0, y0, x1, y1 = geom_.bounds
        return self.obs.near(box(x0 - NEAR_MM, y0 - NEAR_MM,
                                 x1 + NEAR_MM, y1 + NEAR_MM))

    def _foreign(self, it, net: str) -> bool:
        return it.net != net and net not in it.tied

    def via_ok(self, at, net: str, own=None, via=None,
               filled: bool = False) -> bool:
        """A via of `net` at `at` clears everything; `own` (a pad) is the
        pad it sits in, which is not an obstacle to it; `via` is (size,
        drill), default the fanout's. A `filled` via drains no paste, so its
        own net's SMD pads (a decoupler's under the ball grid) do not keep
        it off."""
        size, drill = via or self.via
        P = Point(at)
        r = self.rules
        if self.inside is not None and not self.inside.buffer(
                -(r.edge_clearance + size / 2 + r.margin)).contains(P):
            return False
        if any(k.buffer(size / 2).intersects(P) for k in self.keepouts):
            return False
        for it in self._near(P):
            if own is not None and it.ref == own.ref and (
                    it.shape is own.poly or it.shape.equals(own.poly)):
                continue
            d = it.shape.distance(P)
            if it.hole is not None and \
                    it.hole.distance(P) - drill / 2 < r.hole_to_hole + r.margin:
                return False
            if self._foreign(it, net):
                if it.layers & self.cu and \
                        d - it.half - size / 2 < r.clr(net, it.net):
                    return False
                if d - it.half - drill / 2 < r.hole_clearance + r.margin:
                    return False
            elif it.smd and not filled and d - drill / 2 < rf.VIA_PAD_KEEP_MM:
                return False
        return True

    def track_ok(self, a, b, layer: str, net: str, w: float) -> bool:
        if a == b:
            return True
        seg = LineString([a, b])
        r = self.rules
        if self.inside is not None and not self.inside.buffer(
                -(r.edge_clearance + w / 2 + r.margin)).contains(seg):
            return False
        for it in self._near(seg):
            if not self._foreign(it, net):
                continue
            if layer in it.layers and \
                    it.shape.distance(seg) - it.half - w / 2 < r.clr(net, it.net):
                return False
            if it.hole is not None and \
                    it.hole.distance(seg) - w / 2 < r.hole_clearance + r.margin:
                return False
        return True

    # ------------------------------------------------------------ search

    def _commit(self, ops: list) -> None:
        self._conn += 1
        self.obs.added.extend(rf.ops_items(ops, self._conn,
                                           self.bg.copper_layers))

    @staticmethod
    def directions(pad, centre) -> list[tuple[int, int]]:
        """Outward along the pad's long axis first, then its two sides;
        never back toward the footprint's centre."""
        x0, y0, x1, y1 = pad.poly.bounds
        px, py = pad.center
        if x1 - x0 >= y1 - y0:
            d = (1 if px >= centre[0] else -1, 0)
        else:
            d = (0, 1 if py >= centre[1] else -1)
        return [d] + [q for q in ((1, 0), (-1, 0), (0, 1), (0, -1))
                      if q != d and q != (-d[0], -d[1])]

    def spot(self, pad, centre, net, layer, w):
        """(via centre, distance from the pad edge) or None."""
        x0, y0, x1, y1 = pad.poly.bounds
        px, py = pad.center
        drill = self.via[1]
        start = drill / 2 + rf.VIA_PAD_KEEP_MM
        n = int(round((MAX_DIST_MM - start) / STEP_MM)) + 1
        for d in self.directions(pad, centre):
            tip = (x1 if d[0] > 0 else x0 if d[0] < 0 else px,
                   y1 if d[1] > 0 else y0 if d[1] < 0 else py)
            for i in range(n):
                dist = start + i * STEP_MM
                for lat in LATERAL_MM:
                    v = (tip[0] + d[0] * dist + d[1] * lat,
                         tip[1] + d[1] * dist + d[0] * lat)
                    if self.via_ok(v, net) and self.track_ok(
                            pad.center, v, layer, net,
                            self.seg_width(pad.center, v, net, layer, w)):
                        return v, dist
        return None

    def seg_width(self, a, b, net, layer, w):
        """The width a stub from a to b is laid at: the net's neck width
        where the whole stub lies in its neck zone (necklib, rules_gen's
        aiee_pwr_neck, #937), else w. A width given by the caller
        (--fan-width) is kept as given."""
        if self.width or net not in self.rules.necks:
            return w
        nw = necklib.neck_width(self.rules.necks, net, layer,
                                LineString([a, b]))
        return min(w, nw) if nw is not None else w

    def in_pad(self, pad, net, ball: bool = False):
        """The pad's centre, if a via of the net fits there: an SMD pad
        whose copper holds the whole via (a plane ball's: the ball via's
        drill), clear of everything else."""
        if pad.drill is not None:
            return None
        via = self.ball_via if ball else self.via
        c = (float(pad.center[0]), float(pad.center[1]))
        if not pad.poly.contains(Point(c).buffer(via[1 if ball else 0] / 2)):
            return None
        return c if self.via_ok(c, net, own=pad, via=via,
                                filled=ball) else None

    def _has_via(self, pad, net, skip: float, ball: bool) -> bool:
        """A via of the net already fans the pad out: within `skip` of its
        centre, or for a plane ball, on the ball's own copper (a neighbour
        ball's via half a pitch away does not count)."""
        P = Point(pad.center)
        pts = [Point(v.at) for v in self.bg.vias_of(net=net)]
        pts += [it.shape for it in self.obs.added
                if it.net == net and it.shape.geom_type == "Point"]
        if ball:
            return any(pad.poly.intersects(q) for q in pts)
        return any(q.distance(P) < skip for q in pts)

    def join(self, pad, net, layer, w, reach):
        """A straight, 45-degree or L path to own copper on the layer."""
        pc = pad.center
        tg = [v.at for v in self.bg.vias_of(net=net)]
        for t in self.bg.tracks_of(net=net, layer=layer):
            tg += [t.shape.coords[0], t.shape.coords[-1]]
        tg += [p.center for p in self.bg.pads_of(net=net)
               if layer in p.layers
               and not (p.ref == pad.ref and p.number == pad.number)]
        tg += [p for it in self.obs.added if it.net == net
               and layer in it.layers
               for p in (list(it.shape.coords) if it.shape.geom_type ==
                         "LineString" else [(it.shape.x, it.shape.y)])]
        tg = sorted({(float(q[0]), float(q[1])) for q in tg
                     if 0 < math.dist(q, pc) < reach},
                    key=lambda q: math.dist(q, pc))
        for q in tg:
            dx, dy = q[0] - pc[0], q[1] - pc[1]
            m = min(abs(dx), abs(dy))
            sx, sy = math.copysign(1, dx), math.copysign(1, dy)
            for path in ([pc, q],
                         [pc, (pc[0] + sx * m, pc[1] + sy * m), q],
                         [pc, (q[0] - sx * m, q[1] - sy * m), q],
                         [pc, (q[0], pc[1]), q], [pc, (pc[0], q[1]), q]):
                if all(self.track_ok(path[i], path[i + 1], layer, net,
                                     self.seg_width(path[i], path[i + 1],
                                                    net, layer, w))
                       for i in range(len(path) - 1)):
                    return [p for i, p in enumerate(path)
                            if i == 0 or p != path[i - 1]]
        return None

    def fan(self, specs: list[str], *, skip: float = SKIP_MM,
            reach: float = JOIN_MM, in_pad_only: bool = False
            ) -> tuple[list[dict], list[dict]]:
        if in_pad_only and not self.via_in_pad:
            raise CheckError("plane balls take a via in the pad: a filled, "
                             "capped via the fab must offer (via_in_pad)")
        ops: list[dict] = []
        report: list[dict] = []
        for spec in specs:
            ref, _, num = spec.strip().partition(".")
            if not num:
                raise CheckError(f"--pads {spec!r}: want REF.PAD")
            pads = [p for p in self.bg.pads_of(ref=ref) if p.number == num]
            if not pads:
                raise CheckError(f"--pads {spec!r}: no such pad on the board")
            pad = pads[0]
            net = pad.net or ""
            row = {"pad": spec, "net": net}
            report.append(row)
            if not net:
                row["result"] = "no net"
                continue
            on = [ly for ly in pad.layers if ly in self.cu]
            if not on:
                row["result"] = "no copper layer"
                continue
            layer = on[0]
            row["layer"] = layer
            if self._has_via(pad, net, skip, in_pad_only):
                row["result"] = "has via"
                continue
            if in_pad_only:
                vip = self.in_pad(pad, net, ball=True)
                if vip is None:
                    row["result"] = "no spot"
                    continue
                new = [{"op": "add_via", "at": _p(vip),
                        "size": self.ball_via[0], "drill": self.ball_via[1],
                        "net": net}]
                row.update(result="via in pad", at=_p(vip))
                self._commit(new)
                ops += new
                continue
            w = self.width or self.rules.width(net)
            fp = self.bg.footprints.get(ref, {})
            centre = fp.get("at", pad.center)
            hit = self.spot(pad, centre, net, layer, w)
            new: list[dict] = []
            if hit:
                v, dist = hit
                new.append({"op": "add_track", "start": _p(pad.center),
                            "end": _p(v), "width": self.seg_width(
                                pad.center, v, net, layer, w),
                            "layer": layer, "net": net})
                new.append({"op": "add_via", "at": _p(v),
                            "size": self.via[0], "drill": self.via[1],
                            "net": net})
                row.update(result="via", at=_p(v), dist_mm=_r(dist))
            else:
                path = self.join(pad, net, layer, w, reach)
                vip = None if path is not None or not self.via_in_pad \
                    else self.in_pad(pad, net)
                if path is not None:
                    new += [{"op": "add_track", "start": _p(a), "end": _p(b),
                             "width": self.seg_width(a, b, net, layer, w),
                             "layer": layer, "net": net}
                            for a, b in zip(path, path[1:])]
                    row.update(result="join", to=_p(path[-1]))
                elif vip is not None:
                    new.append({"op": "add_via", "at": _p(vip),
                                "size": self.via[0], "drill": self.via[1],
                                "net": net})
                    row.update(result="via in pad", at=_p(vip))
                else:
                    row["result"] = "no spot"
                    continue
            self._commit(new)
            ops += new
        return ops, report
