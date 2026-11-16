#!/usr/bin/env python
"""check_power_path.py - how much current each power path can carry, from
the copper itself: pad -> track -> via -> pour -> pad.

check_current judges every segment and via cluster against the net's
budget one at a time. That answers "is this piece wide enough for the
budget", not "what does the chain from the connector to the switch carry",
and a budget written low (0.5 A on a 5 A VBUS) passes 0.25 mm tracks and
two vias at every layer change. This builds the net's copper as a flow
network and computes the maximum current from the source part's pads to
each power part's pads (max-flow = min-cut), so parallel tracks and vias
add up and the narrowest cross-section anywhere on the way is found:

  track segment   an edge of capacity I(width, layer copper, dT) - the
                  IPC-2152 width table check_current uses, inverted
  via             an edge per layer it lands on, from that layer's copper
                  to its barrel, of capacity --via-amps or I(barrel): the
                  plated barrel's cross-section (pi x drill x plating) read
                  as a track of the same section, so a current crossing
                  layers passes one barrel and a current staying on a
                  layer does not
  pad, pour fill  no limit (pour necks are check_current's pour-neck
                  rule; a thermal-relief spoke is not modelled here)
  joins           a track end in a pad, via or fill, a track end on
                  another track's body (a T-junction), a via in a pad or
                  fill, a via on a track's body (its centre in the
                  track's copper, or the track's centreline through its
                  pad), a track passing through a pad (its centreline
                  through the pad, or the pad's centre in its copper),
                  a pad in a fill, two track ends overlapping

The min cut is reported as the bottleneck: the tracks (width, layer) and
via barrels that together carry the whole path's capacity. A path is
judged against the current it must carry: constraints.json's
"power_paths" [{net, from, to, current_a, dt_c?}], or each power net's
budget (constraints "power" current_a) from the net's likely source (an
inductor, then a transistor, fuse, connector, low-ohm resistor) to every
other power part on it (those kinds, and ICs with two or more pads on the
net: power pins). `--path NET:FROM:TO:A` adds one from the command line.
A USB PD input is judged at its contract too: every VBUS-named net a
connector feeds, from the connector to each power part, at
constraints.json's "pd_source": {"contract_a": 5} - or, when the board
carries a PD sink controller (CH224, IP2721, HUSB238, STUSB4500...) and
declares no contract, at 3 A (USB-C's ceiling without an e-marked 5 A
cable), as a derived warning. A budget written low (0.5 A on a 5 A VBUS)
is exactly what this is for.

A declared power_paths current also flows through the series part at the
path's end: a shunt, inductor, fuse or bead has the same current on its other
net, whether or not the list names that net (hc-power: "+12V from R104", and
IL_A from L10 to R104 was 0.2 mm at 12.5 A). So the path carries on to the next
part while the other net has exactly one more power part, and each hop is
judged as a declared path (origin "series from ..."). Parts in parallel between
the same two nets split the current. Where the net fans out to several power
parts the walk stops: the share of each is not known. A budget in "power" does
not carry on: it is the whole rail's, not what one branch passes. Pass
--no-series to judge the list alone.

Findings (kind; severity):
  path_capacity   the path carries less than its current (error; a warning
                  when the current was derived, not declared), or less than
                  --margin x its current (warning, default 1.25)
  path_open       no copper path from the source to the part (error)

CLI: --pcb board.kicad_pcb [--constraints constraints.json]
     [--path VBUS:J1:Q1:5] [--via-amps 1.0] [--plating-mm 0.020]
     [--dt 10] [--margin 1.25] [--out report.json]
Exit 0 pass / 1 violations / 2 error.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from collections import deque
from pathlib import Path

from shapely import STRtree
from shapely.geometry import Point

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import check_current  # noqa: E402
import checklib  # noqa: E402
import engcalc  # noqa: E402
import geom  # noqa: E402

SCRIPT = "check_power_path"
INF = 1e9
TOL = 0.05
SNAP = 3
PLATING_MM = 0.020       # JLC's typical finished barrel plating (18-25 um)
DT_DEFAULT = 10.0
MARGIN = 1.25
SOURCE_KINDS = ("L", "Q", "F", "J", "R")
IC_KINDS = ("U", "IC")
SHUNT_OHMS = 0.1
DEFAULT_PD_A = 3.0       # USB-C's ceiling without a 5 A e-marked cable
PD_SINK_RE = re.compile(r"CH22[1-4]|IP27\d\d|HUSB23\d|STUSB4500|TPS2575|"
                        r"FUSB302|AP3377\d|CYPD3177|WT2003|PD.?SINK", re.I)


def amps_for_width(width_mm: float, cu_mm: float, dt_c: float) -> float:
    """Inverse of check_current.required_width_mm: the current a track of
    width_mm on cu_mm copper carries at dT."""
    w1 = width_mm * cu_mm / check_current.OZ1_MM
    pts = check_current.IPC2152_1OZ_10C
    if w1 <= 0:
        return 0.0
    i_eq = None
    for (i0, w0), (i1, wb) in zip(pts, pts[1:]):
        if w1 <= wb:
            i_eq = i0 + (w1 - w0) / (wb - w0) * (i1 - i0)
            break
    if i_eq is None:
        (i0, w0), (i1, wb) = pts[-2], pts[-1]
        i_eq = i1 + (w1 - wb) / (wb - w0) * (i1 - i0)
    return i_eq / ((10.0 / dt_c) ** 0.44 if dt_c > 0 else 1.0)


def via_amps(drill_mm: float, plating_mm: float, dt_c: float) -> float:
    """A barrel's current: its copper section as a track of that section."""
    area = math.pi * drill_mm * plating_mm
    return amps_for_width(area / check_current.OZ1_MM,
                          check_current.OZ1_MM, dt_c)


# ------------------------------------------------------------------ graph

class Flow:
    """Dinic max-flow on an undirected capacity graph."""

    def __init__(self):
        self.idx: dict = {}
        self.adj: list[list[int]] = []
        self.to: list[int] = []
        self.cap: list[float] = []
        self.tag: list = []

    def node(self, n) -> int:
        if n not in self.idx:
            self.idx[n] = len(self.adj)
            self.adj.append([])
        return self.idx[n]

    def edge(self, a, b, cap: float, tag=None) -> None:
        u, v = self.node(a), self.node(b)
        if u == v:
            return
        for x, y in ((u, v), (v, u)):
            self.adj[x].append(len(self.to))
            self.to.append(y)
            self.cap.append(cap)
            self.tag.append(tag)

    def maxflow(self, s, t) -> tuple[float, list]:
        sys.setrecursionlimit(max(sys.getrecursionlimit(), 4 * len(self.adj) + 1000))
        """(flow, min-cut tags) from node s to node t; caps are consumed."""
        if s not in self.idx or t not in self.idx:
            return 0.0, []
        S, T = self.idx[s], self.idx[t]
        cap = list(self.cap)
        total = 0.0
        n = len(self.adj)
        while True:
            level = [-1] * n
            level[S] = 0
            q = deque([S])
            while q:
                u = q.popleft()
                for e in self.adj[u]:
                    if cap[e] > 1e-12 and level[self.to[e]] < 0:
                        level[self.to[e]] = level[u] + 1
                        q.append(self.to[e])
            if level[T] < 0:
                break
            it = [0] * n

            def push(u, f):
                if u == T:
                    return f
                while it[u] < len(self.adj[u]):
                    e = self.adj[u][it[u]]
                    v = self.to[e]
                    if cap[e] > 1e-12 and level[v] == level[u] + 1:
                        d = push(v, min(f, cap[e]))
                        if d > 0:
                            cap[e] -= d
                            cap[e ^ 1] += d
                            return d
                    it[u] += 1
                return 0.0

            while True:
                f = push(S, INF * 10)
                if f <= 1e-12:
                    break
                total += f
        seen = [False] * n
        seen[S] = True
        q = deque([S])
        while q:
            u = q.popleft()
            for e in self.adj[u]:
                if cap[e] > 1e-12 and not seen[self.to[e]]:
                    seen[self.to[e]] = True
                    q.append(self.to[e])
        cut = []
        for u in range(n):
            if not seen[u]:
                continue
            for e in self.adj[u]:
                if not seen[self.to[e]] and self.tag[e] is not None \
                        and self.cap[e] < INF:
                    cut.append(self.tag[e])
        return total, cut


def _snap(xy):
    return (round(xy[0], SNAP), round(xy[1], SNAP))


def build_graph(bg: geom.BoardGeom, net: str, dt_c: float,
                via_a: float | None, plating: float) -> Flow:
    g = Flow()
    cu = bg.stackup.copper_thickness
    tracks = [t for t in bg.tracks_of(net) if t.length > 0]
    vias = bg.vias_of(net)
    pads = bg.pads_of(net=net)
    fills = [(layer, poly) for z in bg.zones_of(net)
             for layer, polys in z.fills.items() for poly in polys]
    ends = []  # (node, xy, layer, half width)
    for t in tracks:
        for xy in (t.shape.coords[0], t.shape.coords[-1]):
            ends.append((("pt", t.layer, _snap(xy)), xy, t.layer, t.width / 2))
    # a track end on another track's body (a T-junction) is copper joined
    # there, and so is a via on its body (its centre in the track's copper,
    # or the track's centreline through its pad): the track is split at the
    # end's or the via's foot
    on_track = {}  # via index -> the track nodes at its feet
    via_pts = [Point(v.at) for v in vias]
    pad_tree = STRtree([p.poly for p in pads]) if pads else None
    pad_mid = [p.poly.centroid for p in pads]
    pad_feet = []  # (track node, pad): a track passing through a pad
    for i, t in enumerate(tracks):
        cuts = {0.0, t.length}
        for node, xy, layer, _hw in ends:
            if layer != t.layer:
                continue
            pt = Point(xy)
            if t.shape.distance(pt) <= t.width / 2 + TOL:
                d = t.shape.project(pt)
                if TOL < d < t.length - TOL:
                    cuts.add(d)
        feet = []
        x0, y0, x1, y1 = t.shape.bounds
        for vi, v in enumerate(vias):
            reach = max(t.width, v.diameter) / 2 + TOL
            x, y = v.at
            if not v.spans(t.layer) or x < x0 - reach or x > x1 + reach \
                    or y < y0 - reach or y > y1 + reach:
                continue  # off the track's box: the exact test can't pass
            if t.shape.distance(via_pts[vi]) <= reach:
                d = t.shape.project(via_pts[vi])
                if TOL < d < t.length - TOL:
                    cuts.add(d)
                feet.append((vi, d))
        # a pad the track passes through, neither end in it (#785): its
        # centreline crosses the pad, or the pad's centre is in its copper
        through = []
        if pad_tree is not None:
            hw = t.width / 2 + TOL
            a, b = Point(t.shape.coords[0]), Point(t.shape.coords[-1])
            for pi in pad_tree.query(t.shape.buffer(hw)):
                pd = pads[pi]
                if t.layer not in pd.layers or pd.poly.distance(a) <= TOL \
                        or pd.poly.distance(b) <= TOL:
                    continue  # an end in the pad is joined below
                if t.shape.distance(pd.poly) <= TOL or \
                        t.shape.distance(pad_mid[pi]) <= hw:
                    d = t.shape.project(pad_mid[pi])
                    if TOL < d < t.length - TOL:
                        cuts.add(d)
                    through.append((pd, d))
        cap = amps_for_width(t.width, cu.get(t.layer, check_current.OZ1_MM),
                             dt_c)
        pts = sorted(cuts)
        nodes = [("pt", t.layer, _snap(t.shape.interpolate(d).coords[0]))
                 for d in pts]
        for k in range(len(pts) - 1):
            g.edge(nodes[k], nodes[k + 1], cap, ("track", i))
        for vi, d in feet:
            k = min(range(len(pts)), key=lambda k: abs(pts[k] - d))
            on_track.setdefault(vi, []).append(nodes[k])
        for pd, d in through:
            k = min(range(len(pts)), key=lambda k: abs(pts[k] - d))
            pad_feet.append((nodes[k], pd))
        for d, n in zip(pts[1:-1], nodes[1:-1]):
            ends.append((n, t.shape.interpolate(d).coords[0], t.layer,
                         t.width / 2))
    # end-to-end overlap on a layer
    for k, (na, pa, la, ha) in enumerate(ends):
        for nb, pb, lb, hb in ends[k + 1:]:
            if la == lb and na != nb and \
                    math.dist(pa, pb) <= ha + hb + TOL:
                g.edge(na, nb, INF)
    fill_tree = STRtree([f for _l, f in fills]) if fills else None

    def pads_at(geom_, layer):
        if pad_tree is None:
            return []
        return [pads[i] for i in pad_tree.query(geom_.buffer(TOL))
                if layer in pads[i].layers
                and pads[i].poly.distance(geom_) <= TOL]

    def fills_at(geom_, layer, reach=TOL):
        if fill_tree is None:
            return []
        return [i for i in fill_tree.query(geom_.buffer(reach))
                if fills[i][0] == layer
                and fills[i][1].distance(geom_) <= reach]

    for node, xy, layer, _hw in ends:
        pt = Point(xy)
        for p in pads_at(pt, layer):
            g.edge(node, ("pad", p.ref, p.number), INF)
        for j in fills_at(pt, layer):
            g.edge(node, ("fill", j), INF)
    for node, pd in pad_feet:
        g.edge(node, ("pad", pd.ref, pd.number), INF)
    for vi, v in enumerate(vias):
        cap = via_a if via_a else via_amps(v.drill, plating, dt_c)
        disc = v.poly
        for node, xy, layer, _hw in ends:
            if v.spans(layer) and math.dist(xy, v.at) <= \
                    v.diameter / 2 + TOL:
                g.edge(node, ("via", vi, layer), INF)
        for node in on_track.get(vi, ()):
            g.edge(node, ("via", vi, node[1]), INF)
        for layer in v.layers:
            for p in pads_at(disc, layer):
                g.edge(("via", vi, layer), ("pad", p.ref, p.number), INF)
            for j in fills_at(Point(v.at), layer, v.diameter / 2):
                g.edge(("via", vi, layer), ("fill", j), INF)
            if ("via", vi, layer) in g.idx:
                g.edge(("via", vi, layer), ("barrel", vi), cap,
                       ("via", vi))
    for p in pads:
        for layer in p.layers:
            for j in fills_at(p.poly, layer):
                g.edge(("pad", p.ref, p.number), ("fill", j), INF)
    # two zones of the net on one layer that overlap are one copper
    for j, (layer, poly) in enumerate(fills):
        for k in fills_at(poly, layer):
            if k > j:
                g.edge(("fill", j), ("fill", int(k)), INF)
    g.meta = {"tracks": tracks, "vias": vias}
    return g


def path_flow(bg, net, src_ref, dst_ref, dt_c, via_a, plating) -> dict:
    g = build_graph(bg, net, dt_c, via_a, plating)
    src = [p for p in bg.pads_of(net=net) if p.ref == src_ref]
    dst = [p for p in bg.pads_of(net=net) if p.ref == dst_ref]
    if not src or not dst:
        return {"amps": None, "why": f"{src_ref if not src else dst_ref} "
                                     f"has no pad on {net}"}
    for p in src:
        g.edge(("S",), ("pad", p.ref, p.number), INF)
    for p in dst:
        g.edge(("pad", p.ref, p.number), ("T",), INF)
    flow, cut = g.maxflow(("S",), ("T",))
    if flow >= INF / 2:
        return {"amps": None, "pour_only": True, "neck_tracks": [],
                "neck_vias": [], "why": "pour and pads all the way: no "
                "track or via limits it (pour necks are check_current's)"}
    tracks, vias = g.meta["tracks"], g.meta["vias"]
    neck_t, neck_v = [], []
    for kind, i in sorted(set(cut)):
        if kind == "track":
            t = tracks[i]
            neck_t.append({"layer": t.layer, "width_mm": checklib.rnd(
                t.width, 3), "at": [checklib.rnd(c, 3) for c in
                                    t.shape.interpolate(0.5, normalized=True).coords[0]]})
        else:
            v = vias[i]
            neck_v.append({"at": [checklib.rnd(c, 3) for c in v.at],
                           "drill_mm": v.drill})
    return {"amps": flow, "neck_tracks": neck_t, "neck_vias": neck_v}


# ------------------------------------------------------------------ paths

def _kind(ref: str) -> str:
    m = re.match(r"[A-Za-z]+", ref)
    return m.group(0).upper() if m else ""


def power_parts(bg: geom.BoardGeom, net: str) -> list[str]:
    """Power parts on a net, the likely source first: an inductor (a
    regulator's output), a transistor (a switch's output), a fuse, a
    connector, a shunt (<= 0.1 ohm); then ICs with two or more pads on it
    (a regulator's VIN, an amplifier's PVDD - its power pins, not a logic
    VDD's single pad)."""
    pads = bg.pads_of(net=net)
    refs = sorted({p.ref for p in pads})
    out = []
    for ref in refs:
        k = _kind(ref)
        if k == "R":
            v = engcalc.parse_value((bg.footprints.get(ref) or {})
                                    .get("value") or "")
            if v is None or v > SHUNT_OHMS:
                continue
        elif k in IC_KINDS:
            if sum(1 for p in pads if p.ref == ref) < 2:
                continue
        elif k not in SOURCE_KINDS:
            continue
        out.append(ref)

    def key(r):
        m = re.match(r"([A-Za-z]+)(\d*)", r)
        k = _kind(r)
        rank = SOURCE_KINDS.index(k) if k in SOURCE_KINDS else \
            len(SOURCE_KINDS)
        return (rank, int(m.group(2) or 0), r)
    return sorted(out, key=key)


def pd_contract(bg: geom.BoardGeom, cons: dict) -> tuple[float, str] | None:
    """(amps, why) a USB PD input carries: constraints' pd_source, else
    DEFAULT_PD_A when a PD sink controller is on the board."""
    pd = cons.get("pd_source") or {}
    if pd.get("contract_a"):
        return float(pd["contract_a"]), "constraints pd_source"
    for ref, fp in sorted(bg.footprints.items()):
        if _kind(ref) in IC_KINDS and PD_SINK_RE.search(fp.get("value") or ""):
            return DEFAULT_PD_A, (f"{ref} {fp['value']} is a PD sink: "
                                  f"{DEFAULT_PD_A:g} A assumed")
    return None


def vbus_entries(bg: geom.BoardGeom) -> list[tuple[str, str]]:
    """(net, source ref) for every VBUS-named net a connector feeds, and the
    nets the same current reaches through a series two-pad part on the way
    (a shunt of <= 0.1 ohm, a ferrite or inductor; not a fuse, which
    usually guards a branch rated below the input): the one it arrived
    through is that net's source. The first connector on a net is its
    entry."""
    out, todo, seen = [], [], set()
    for net in sorted(bg.nets):
        if "VBUS" not in net.upper():
            continue
        js = sorted({p.ref for p in bg.pads_of(net=net) if _kind(p.ref) in
                     ("J", "USB", "CN", "P")})
        if js:
            todo.append((net, js[0]))
            seen.add(net)
    while todo:
        net, src = todo.pop(0)
        out.append((net, src))
        series = [r for r in power_parts(bg, net) if r != src
                  and _kind(r) in ("R", "L")] + sorted(
            {p.ref for p in bg.pads_of(net=net) if _kind(p.ref) == "FB"})
        for ref in series:
            others = {p.net for p in bg.pads_of(ref=ref)} - {net, None}
            if len(others) == 1:
                nxt = others.pop()
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append((nxt, ref))
    return out


def _is_series(bg, ref: str) -> bool:
    """A part whose current on one net is its current on the other: two
    nets, and an inductor, bead, fuse or shunt (<= SHUNT_OHMS)."""
    k = _kind(ref)
    if k == "R":
        v = engcalc.parse_value((bg.footprints.get(ref) or {})
                                .get("value") or "")
        if v is None or v > SHUNT_OHMS:
            return False
    elif k not in ("L", "FB", "F"):
        return False
    return len({p.net for p in bg.pads_of(ref=ref)} - {None}) == 2


def _leads_on(bg, ref: str, net: str) -> bool:
    """Is there a power part past `ref` on its other net?"""
    far = {p.net for p in bg.pads_of(ref=ref)} - {None, net}
    return any(r != ref for n in far for r in power_parts(bg, n))


def series_paths(bg, declared: list[dict]) -> list[dict]:
    """The hops a declared current must also pass, past the series part at
    either end of a declared path. Nets the list already names are left to
    the list; a fan-out ends the walk (see the module docstring)."""
    named = {p["net"] for p in declared}
    out, seen = [], set()
    todo = [(p, end) for p in declared for end in (p["from"], p["to"])]
    while todo:
        p, ref = todo.pop(0)
        if ref not in bg.footprints or not _is_series(bg, ref):
            continue
        nets = {q.net for q in bg.pads_of(ref=ref)} - {None}
        for net in sorted(nets - {p["net"]}):
            if net in named or net in seen:
                continue
            seen.add(net)
            parts = power_parts(bg, net)
            twins = [r for r in parts if _is_series(bg, r) and {
                q.net for q in bg.pads_of(ref=r)} - {None} == nets]
            # a 0R out to a net with no other power part is a sense tap
            # (hc-power R105), not the path
            nxt = [r for r in parts if r not in twins and not (
                _kind(r) == "R" and not _leads_on(bg, r, net))]
            if len(nxt) != 1:
                continue
            amps = p["current_a"] / max(len(twins), 1)
            hop = {"net": net, "from": ref, "to": nxt[0], "current_a": amps,
                   "dt_c": p["dt_c"], "derived": False,
                   "origin": f"series from {p['net']} {p['from']} -> "
                             f"{p['to']} through {ref}"}
            out.append(hop)
            todo.append((hop, nxt[0]))
    return out


def planned_paths(bg, cons: dict, extra: list[str], dt: float,
                  series: bool = True) -> list[dict]:
    out = []
    for p in cons.get("power_paths") or []:
        out.append({"net": p["net"], "from": p["from"], "to": p["to"],
                    "current_a": float(p["current_a"]),
                    "dt_c": float(p.get("dt_c", dt)), "origin":
                    "constraints power_paths", "derived": False})
    for item in extra:
        parts = item.split(":")
        if len(parts) != 4:
            raise checklib.CheckError(f"--path {item!r}: want NET:FROM:TO:A")
        out.append({"net": parts[0], "from": parts[1], "to": parts[2],
                    "current_a": float(parts[3]), "dt_c": dt,
                    "origin": "--path", "derived": False})
    if series:
        out += series_paths(bg, out)
    named = {p["net"] for p in out}
    for e in cons.get("power") or []:
        net = e.get("net")
        if not net or net not in bg.nets or not e.get("current_a") \
                or net in named:
            continue
        refs = power_parts(bg, net)
        for dst in refs[1:]:
            out.append({"net": net, "from": refs[0], "to": dst,
                        "current_a": float(e["current_a"]),
                        "dt_c": float(e.get("dt_c", dt)),
                        "origin": "constraints power budget",
                        "derived": False})
    pd = pd_contract(bg, cons)
    if pd:
        amps, why = pd
        declared = why == "constraints pd_source"
        have = {(p["net"], p["from"], p["to"]): p for p in out}
        for net, j in vbus_entries(bg):
            if any(e.get("net") == net and float(e.get("current_a") or 0)
                   >= amps for e in cons.get("power") or []):
                continue
            parts = power_parts(bg, net)
            series = [r for r in parts if _kind(r) not in IC_KINDS]
            # the contract flows on to the series parts; an IC (the PD
            # controller, a buck's VIN) draws only its share of it
            for dst in [r for r in (series if len(series) > 1 else parts)
                        if r != j]:
                row = have.get((net, j, dst))
                if row is not None and row["current_a"] >= amps:
                    continue
                if row is not None and row["origin"] != \
                        "constraints power budget":
                    continue
                if row is not None:
                    out.remove(row)
                out.append({"net": net, "from": j, "to": dst,
                            "current_a": amps, "dt_c": dt,
                            "origin": f"USB PD input ({why})",
                            "derived": not declared})
    return out


def run(argv=None):
    a = _parser().parse_args(argv)
    bg = geom.load_board(Path(a.pcb))
    cons = checklib.load_json(a.constraints, "constraints") \
        if a.constraints else {}
    paths = planned_paths(bg, cons, a.path, a.dt, series=a.series)
    vios, facts = [], []
    for p in paths:
        if p["net"] not in bg.nets:
            raise checklib.CheckError(f"path net {p['net']} not on board")
        r = path_flow(bg, p["net"], p["from"], p["to"], p["dt_c"],
                      a.via_amps, a.plating_mm)
        row = {**p, **{k: (checklib.rnd(v, 3) if isinstance(v, float) else v)
                       for k, v in r.items()}}
        facts.append(row)
        need = p["current_a"]
        where = f"{p['net']} {p['from']} -> {p['to']}"
        if r.get("pour_only"):
            continue
        if r["amps"] is None or r["amps"] <= 1e-9:
            vios.append(checklib.violation(
                SCRIPT, "error", None, None, p["net"], [p["from"], p["to"]],
                f"{where}: no copper path ({r.get('why', 'open')})", SCRIPT,
                kind="path_open"))
            continue
        margin = 1.0 if p["origin"] == "constraints power budget" \
            else a.margin
        if r["amps"] < margin * need:
            neck = "; ".join(
                [f"{t['width_mm']} mm on {t['layer']} at {t['at']}"
                 for t in r["neck_tracks"][:6]] +
                ([f"{len(r['neck_vias'])} via(s) "
                  f"{sorted({v['drill_mm'] for v in r['neck_vias']})} mm"]
                 if r["neck_vias"] else []))
            sev = "error" if r["amps"] < need and not p["derived"] \
                else "warning"
            pos = (r["neck_tracks"][0]["at"] if r["neck_tracks"] else
                   r["neck_vias"][0]["at"] if r["neck_vias"] else None)
            vios.append(checklib.violation(
                SCRIPT, sev, pos, None, p["net"], [p["from"], p["to"]],
                f"{where} carries {r['amps']:.3g} A at dT {p['dt_c']:g} C "
                f"against {need:g} A ({p['origin']}); bottleneck: {neck}",
                SCRIPT, kind="path_capacity", amps=r["amps"],
                current_a=need, neck_tracks=r["neck_tracks"],
                neck_vias=r["neck_vias"]))
    return checklib.report(SCRIPT, a.pcb, vios, paths=facts,
                           via_amps=a.via_amps or
                           f"barrel, {a.plating_mm} mm plating",
                           stackup_assumed=bg.stackup.assumed,
                           nothing_to_judge=None if facts else
                           "no power path found to judge"), a.out


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--path", action="append", default=[],
                    help="NET:FROM:TO:AMPS")
    ap.add_argument("--via-amps", type=float, default=None,
                    help="per-via current (default: from the barrel)")
    ap.add_argument("--plating-mm", type=float, default=PLATING_MM)
    ap.add_argument("--dt", type=float, default=DT_DEFAULT)
    ap.add_argument("--margin", type=float, default=MARGIN)
    ap.add_argument("--no-series", dest="series", action="store_false",
                    help="judge the declared paths only, not the hops past "
                         "their series parts")
    ap.add_argument("--out")
    return ap


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
