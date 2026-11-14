#!/usr/bin/env python
"""check_kelvin.py - does each current-sense amplifier read its shunt, or the
shunt plus copper? (P8 verify suite)

The hit (hc-power cold review 2, 2026-11-02, all gates green): a 2 mOhm shunt's
tap left from the inductor pad, past a 0.86 mOhm neck, so the amplifier read 40 %
high and the over-current trip fired under full load; a 1 mOhm output shunt's "+"
tap sat upstream of a 4 mm copper neck, 21 % high on the power reading. iso-io's
INA3221 taps off a rail trunk read 27-60 % high on 100 mOhm. Nets are connected,
widths are legal, so nothing else sees it.

What it measures is the error itself, not a routing rule. For each shunt side it
solves the DC potential in that net's real copper (tracks, pours, vias, on every
layer) with the shunt's pad as the sink and 1 A entering at the net's other
current-carrying parts. A tap carries no current, so its potential is that of
the copper where it joins. The tap's potential above the shunt pad, per amp, is
resistance the amplifier reads as shunt.

  shunt     a two-net resistor of at most SHUNT_OHMS, above zero
  reader    an IC with a tap on both of its nets. A tap is the IC's own pin on the
            net, or through a resistor of at most TAP_MAX_OHMS from the net, where
            the pin is sense-named or the board stores no pin name. An IC whose
            named VIN sits on one net and its SENSE on the other (a hot-swap's
            limit) has no pair, and is not judged. Several candidate pins on a
            side (a board with no pin names, or a channel pair not picked by
            number) take the kindest.
  entering  constraints.json power_paths on this net that end at the shunt, split
            by their current. Otherwise the other power parts on the net
            (check_power_path.power_parts, plus beads, net ties, transformers
            and regulator output pins; loads (ICs) only when nothing else is
            there), each solved alone and the kindest result taken: with no
            list, which part feeds the shunt is not known, and a tap only fails
            if it is wrong for all of them.
  pads      equipotential: a soldered pad has a part's terminal on it.

Declared sense taps (#936): a constraints.json power entry's "sense_pads"
{"R113.1": "C114.1"} names the pad a sense line starts on (a feedback
divider's top, a regulator's SENSE pin) and the pad it must read, on that net.
Power copper is every pad and pour on the net, and every track and via on a
path between two of them; the sense line is the copper from the sense pad that
is none of that. Where it first touches power copper is where it reads. A
contact within SENSE_SLACK_MM of the named pad is the tap itself (the power
track and the sense track may both end on it). The hit: the voice board's
divider tops sat in their rails' pours, reading 6-16 mm from the pad named.

Findings:
  kelvin_error       error at ERROR_FRAC of the shunt or more across both taps,
                     warning at WARN_FRAC. Names each tap and the copper's mOhm.
  sense_tap_off_pad  error: a declared sense line touches a pour, another pad,
                     or a power track or via before the named pad. tap_mm is
                     how far from the pad the farthest such contact reaches.
  sense_tap_missing  error: no copper of its own from the sense pad to the
                     named pad (not routed).
  sense_tap_bad      error: a sense_pads entry names a pad not on the board,
                     or a tap pad not on the entry's net.

A shunt with no reader, a net too large to solve at CELL_MAX_MM and a side with
nothing entering are listed in not_judged. Good to about +/-30 % on a neck
a few cells long: the thresholds are for a tap in the wrong place, not for
tuning one.

CLI: --pcb board.kicad_pcb [--constraints c.json (power_paths, power[].sense_pads)]
     [--out report.json]   exit 0/1/2 per SPEC section 6.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

import numpy as np
from scipy import sparse
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import splu
import shapely
from shapely.geometry import Point
from shapely.strtree import STRtree

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import check_irdrop  # noqa: E402
import check_power_path  # noqa: E402
import checklib  # noqa: E402
import engcalc  # noqa: E402
import geom  # noqa: E402

SCRIPT = "check_kelvin"
SHUNT_OHMS = 0.1
TAP_MAX_OHMS = 10e3
ERROR_FRAC = 0.05
WARN_FRAC = 0.02
CELL_MIN_MM = 0.05
CELL_MAX_MM = 0.25
CELL_BUDGET = 300_000
IC_KINDS = ("U", "IC")
EXTRA_SOURCE_KINDS = ("FB", "T", "NT")
PAD_BOOST = 100.0
SENSE_SLACK_MM = 0.5     # a contact this near the tap pad is the tap itself (#936)
SENSE_PIN = re.compile(
    r"^[~/]?(V?IN\d*[-+−PN]\d*|[-+]\d*IN\d*|I?SENSE.*|I?SNS.*|ISEN.*|CS[-+PN]?\d*|"
    r"S[PN][ABC]?\d*|ISN\d*|ISP\d*|IS[-+]\d*|VS[-+]\d*)$", re.I)
OUTPUT_PIN = re.compile(r"^(V?OUT\d*|SW\d*)$", re.I)


def _kind(ref: str) -> str:
    return check_power_path._kind(ref)


def _ohms(bg, ref: str) -> float | None:
    if not _kind(ref).startswith("R"):
        return None
    return engcalc.parse_value((bg.footprints.get(ref) or {}).get("value") or "")


def _nets(bg, ref: str) -> set[str]:
    return {p.net for p in bg.pads_of(ref=ref)} - {None}


def shunts(bg) -> list[tuple[str, float, list[str]]]:
    out = []
    for ref in sorted(bg.footprints):
        v = _ohms(bg, ref)
        nets = _nets(bg, ref)
        if v is not None and 0 < v <= SHUNT_OHMS and len(nets) == 2:
            out.append((ref, v, sorted(nets)))
    return out


def taps_on(bg, net: str, shunt: str) -> dict[str, list[dict]]:
    """IC ref -> the taps on `net`: {pad (where the tap joins the net), via (the
    resistor, or None), pin (the IC's pin name)}."""
    out: dict[str, list[dict]] = {}
    for p in bg.pads_of(net=net):
        if p.ref == shunt:
            continue
        if _kind(p.ref) in IC_KINDS and (
                not p.pinfunction or SENSE_PIN.match(p.pinfunction)):
            out.setdefault(p.ref, []).append(
                {"pad": p, "via": None, "pin": p.pinfunction})
            continue
        v = _ohms(bg, p.ref)
        if v is None or v > TAP_MAX_OHMS or (0 < v <= SHUNT_OHMS):
            continue
        far = _nets(bg, p.ref) - {net}
        if len(far) != 1:
            continue
        for q in bg.pads_of(net=next(iter(far))):
            if _kind(q.ref) in IC_KINDS and (
                    not q.pinfunction or SENSE_PIN.match(q.pinfunction)):
                out.setdefault(q.ref, []).append(
                    {"pad": p, "via": p.ref, "pin": q.pinfunction or q.number,
                     "far": q.net})
    return out


def _channel(pin: str) -> str | None:
    m = re.search(r"\d", pin)
    return m.group(0) if m else None


def _one_off(a: str, b: str) -> bool:
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def _by_name(taps: list[dict], other: list[dict], other_net: str) -> list[dict]:
    """A side's taps cut to the ones named as the other side's partner: /SNA
    through its 0R goes with /SPA, not /SNC, on a board where the sense
    returns all hang off GND."""
    names = {other_net} | {t["far"] for t in other if t.get("far")}
    hit = [t for t in taps if t.get("far")
           and any(_one_off(t["far"], n) for n in names)]
    return hit or taps


def readers(bg, shunt: str, nets: list[str]) -> list[dict]:
    """[{ic, sides: {net: [taps]}}]: ICs with a tap on both nets, paired by
    channel number where the pins carry one (INA3221 IN+1 / IN-1)."""
    a, b = (taps_on(bg, n, shunt) for n in nets)
    out = []
    for ic in sorted(set(a) & set(b)):
        ta, tb = a[ic], b[ic]
        chans = {_channel(t["pin"]) for t in ta} & {_channel(t["pin"]) for t in tb}
        chans.discard(None)
        if chans and (len(ta) > 1 or len(tb) > 1):
            ta = [t for t in ta if _channel(t["pin"]) in chans] or ta
            tb = [t for t in tb if _channel(t["pin"]) in chans] or tb
        ta, tb = _by_name(ta, tb, nets[1]), _by_name(tb, ta, nets[0])
        out.append({"ic": ic, "sides": {nets[0]: ta, nets[1]: tb}})
    return out


def entering(bg, net: str, shunt: str, skip: set[str],
             paths: list[dict]) -> tuple[list[str], list[float] | None]:
    """The parts whose current reaches the shunt across this net, and their
    shares when the board declares them (None: not known)."""
    declared: dict[str, float] = {}
    for p in paths:
        if p.get("net") != net or shunt not in (p.get("from"), p.get("to")):
            continue
        other = p["to"] if p["from"] == shunt else p["from"]
        if bg.pads_of(net=net, ref=other):
            declared[other] = declared.get(other, 0.0) + float(p["current_a"])
    if declared:
        refs = sorted(declared)
        total = sum(declared.values())
        return refs, [declared[r] / total for r in refs]
    pads = bg.pads_of(net=net)
    parts = check_power_path.power_parts(bg, net)
    refs = {r for r in parts if _kind(r) not in IC_KINDS}
    refs |= {p.ref for p in pads if _kind(p.ref) in EXTRA_SOURCE_KINDS}
    refs |= {p.ref for p in pads if _kind(p.ref) in IC_KINDS
             and OUTPUT_PIN.match(p.pinfunction or "")}
    refs -= skip | {shunt}
    # another amplifier's 0R tap is not a source
    refs = {r for r in refs if _kind(r) != "R"
            or check_power_path._leads_on(bg, r, net)}
    if not refs:
        refs = {r for r in parts if _kind(r) in IC_KINDS} - skip - {shunt}
    return sorted(refs), None


class NetGrid(check_irdrop.NetGrid):
    """check_irdrop's grid, with tracks under two cells wide burned in one cell
    wide at their own conductance, so a thin tap or neck on a coarse grid is
    neither lost nor made wider than it is."""

    def __init__(self, bg, net, layers, cell):
        super().__init__(bg, net, layers, cell)
        self.scale = {}
        for l in layers:
            parts = [p.poly for p in bg.pads_of(net, l)]
            parts += [v.poly for v in bg.vias_of(net, l)]
            parts += [z.fill_on(l) for z in bg.zones_of(net, l)]
            thin = []
            for t in bg.tracks_of(net, l):
                (thin if t.width < 2 * cell else parts).append(
                    t if t.width < 2 * cell else t.poly)
            m = self._raster(geom._union(parts))
            s = np.ones_like(m, dtype=float)
            for t in sorted(thin, key=lambda t: t.width):
                self._burn(t, m, s)
            pads = geom._union([p.poly for p in bg.pads_of(net, l)])
            if not pads.is_empty:
                s[self._raster(pads) & m] = PAD_BOOST
            self.mask[l] = m
            self.scale[l] = s
        self._number()
        self._trees = {}

    def _burn(self, t, m, s):
        steps = max(int(t.shape.length / (self.cell / 3)), 1)
        prev = None
        w = min(t.width / self.cell, 1.0)
        mine = set()
        for k in range(steps + 1):
            pt = t.shape.interpolate(k / steps, normalized=True)
            x, y = pt.x, pt.y
            j = int((x - self.x0) / self.cell)
            i = int((y - self.y0) / self.cell)
            if not (0 <= i < self.ny and 0 <= j < self.nx):
                continue
            cells = [(i, j)]
            if prev and prev[0] != i and prev[1] != j:
                cells.append((prev[0], j))  # keep it 4-connected
            for c in cells:
                if c in mine:
                    continue
                mine.add(c)
                if not m[c]:
                    m[c] = True
                    s[c] = w
                elif s[c] < 1.0:
                    s[c] = min(1.0, s[c] + w)  # tracks side by side
            prev = (i, j)


def _edges(grid, rows, cols, vals):
    for l in grid.layers:
        g = 1.0 / check_irdrop.sheet_res_ohm_sq(grid.bg.stackup.copper_thickness[l])
        s = grid.scale[l].ravel()
        for a, b, ka, kb in grid.layer_edges(l):
            rows.append(a)
            cols.append(b)
            vals.append(g * np.minimum(s[ka], s[kb]))


def laplacian(grid) -> sparse.csr_matrix:
    rows, cols, vals = [], [], []
    _edges(grid, rows, cols, vals)
    bg = grid.bg
    for v in bg.vias_of(grid.net):
        span = [l for l in bg.copper_layers if l in v.layers and l in grid.mask]
        if len(span) >= 2:
            check_irdrop._barrel_edges(grid, v.at[0], v.at[1], v.diameter / 2.0,
                                       v.drill or v.diameter / 2.0, span,
                                       rows, cols, vals)
    for p in bg.pads_of(grid.net):
        span = [l for l in bg.copper_layers if l in p.layers and l in grid.mask]
        if len(span) >= 2:
            drill = min(p.drill) if p.drill else \
                check_irdrop.THT_DRILL_FRACTION * min(p.size)
            check_irdrop._barrel_edges(grid, p.center[0], p.center[1],
                                       max(p.size) / 2.0, drill, span,
                                       rows, cols, vals)
    r, c, g = (np.concatenate(x) for x in (rows, cols, vals))
    n = grid.n_nodes
    adj = sparse.coo_matrix((np.r_[g, g], (np.r_[r, c], np.r_[c, r])),
                            shape=(n, n)).tocsr()
    return (sparse.diags(np.asarray(adj.sum(axis=1)).ravel()) - adj).tocsr()


def pad_nodes(grid, p) -> np.ndarray:
    pools = [grid.poly_cells(l, p.poly) for l in p.layers if l in grid.mask]
    pool = np.concatenate(pools) if pools else np.empty(0, dtype=np.int64)
    if len(pool):
        return np.unique(pool)
    near = grid.nearest_node([l for l in p.layers if l in grid.mask],
                             p.center[0], p.center[1],
                             max(p.size) / 2.0 + 3.0 * grid.cell)
    return np.array([near], dtype=np.int64) if near is not None \
        else np.empty(0, dtype=np.int64)


def pick_cell(bg, net: str, layers: list[str]) -> float | None:
    area = sum(bg.net_area(net, l) for l in layers)
    h = max(CELL_MIN_MM, math.sqrt(area / CELL_BUDGET))
    return None if h > CELL_MAX_MM else h


def tap_mohm(bg, grids: dict, net: str, shunt: str, sources: list[str],
             shares: list[float] | None,
             taps: list[dict]) -> tuple[list[float] | None, str | None, float]:
    """Each tap's potential above the shunt's pad(s) per amp entering, mOhm:
    the declared mix, or the source that makes it smallest."""
    layers = [l for l in bg.copper_layers if bg.net_area(net, l) > 0]
    if net not in grids:
        cell = pick_cell(bg, net, layers)
        if cell is None:
            grids[net] = None
        else:
            g = NetGrid(bg, net, layers, cell)
            lap = laplacian(g)
            grids[net] = (g, lap, connected_components(
                lap, directed=False)[1])
    if grids[net] is None:
        return None, f"net {net} too large to solve at {CELL_MAX_MM} mm", 0.0
    g, lap, labels = grids[net]
    sink = np.concatenate([pad_nodes(g, p) for p in bg.pads_of(net=net, ref=shunt)])
    if not len(sink):
        return None, f"shunt pad on {net} lands on no copper", g.cell
    comp = labels[sink[0]]
    keep = labels == comp
    keep[sink] = False
    idx = np.nonzero(keep)[0]
    lu = None
    tap_nodes = []
    for t in taps:
        nodes = pad_nodes(g, t["pad"])
        # a candidate off the shunt's copper can't be read: infinity, so the
        # kindest pick ignores it
        tap_nodes.append(nodes[labels[nodes] == comp] if len(nodes) else nodes)
    if not any(len(n) for n in tap_nodes):
        return None, (f"no tap is joined to the shunt's copper on {net} "
                      "(not routed?)"), g.cell
    per_src = []
    for k, ref in enumerate(sources):
        pads = bg.pads_of(net=net, ref=ref)
        nodes = np.concatenate([pad_nodes(g, p) for p in pads]) if pads else []
        nodes = np.asarray(nodes, dtype=np.int64)
        nodes = np.setdiff1d(nodes[labels[nodes] == comp] if len(nodes)
                             else nodes, sink)
        if not len(nodes):
            continue
        b = np.zeros(g.n_nodes)
        b[nodes] = 1.0 / len(nodes)
        lu = lu or splu(lap[idx][:, idx].tocsc())
        v = np.zeros(g.n_nodes)
        v[idx] = lu.solve(b[idx])
        per_src.append(((shares[k] if shares else None),
                        [float(v[n].mean()) * 1e3 if len(n) else math.inf
                         for n in tap_nodes]))
    if not per_src:
        return None, f"nothing entering {net} reaches the shunt in copper", g.cell
    if shares:
        total = sum(w for w, _ in per_src)
        out = [sum(w * vals[i] for w, vals in per_src) / total
               for i in range(len(taps))]
    else:
        out = [min(vals[i] for _, vals in per_src) for i in range(len(taps))]
    return out, None, g.cell


def judge(bg, paths: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    vios, facts, left = [], [], []
    grids: dict = {}
    for ref, ohms, nets in shunts(bg):
        rs = readers(bg, ref, nets)
        if not rs:
            left.append(f"{ref} ({ohms * 1e3:g} mOhm): no amplifier with a tap "
                        "on both of its nets")
            continue
        for r in rs:
            total = 0.0
            sides = []
            why = None
            for net in nets:
                taps = r["sides"][net]
                skip = {r["ic"]} | {t["via"] for t in taps if t["via"]}
                src, shares = entering(bg, net, ref, skip, paths)
                if not src:
                    why = f"nothing enters {net} but the shunt"
                    continue
                vals, w, cell = tap_mohm(bg, grids, net, ref, src, shares, taps)
                if vals is None:
                    why = w
                    continue
                # several candidate taps: channel pins could not pick one, so
                # take the kindest
                k = int(np.argmin(vals))
                t = taps[k]
                total += vals[k]
                sides.append({
                    "net": net, "tap": t["via"] or f"{r['ic']}.{t['pad'].number}",
                    "pin": t["pin"], "from": src,
                    "pad": [checklib.rnd(c, 2) for c in t["pad"].center],
                    "mohm": checklib.rnd(vals[k], 3),
                    "cell_mm": checklib.rnd(cell, 3)})
            frac = total * 1e-3 / ohms
            fact = {"shunt": ref, "ohms": ohms, "ic": r["ic"], "sides": sides,
                    "extra_mohm": checklib.rnd(total, 3),
                    "error_pct": checklib.rnd(100 * frac, 1)}
            if why:
                fact["partial"] = why
                left.append(f"{ref} / {r['ic']}: {why}")
            facts.append(fact)
            if frac < WARN_FRAC:
                continue
            worst = max(sides, key=lambda s: s["mohm"])
            names = "; ".join(
                f"{s['tap']} on {s['net']} joins {s['mohm']:.3g} mOhm from "
                f"{ref}'s pad, with current from {', '.join(s['from'])}"
                for s in sides if s["mohm"] > 0.1 * total)
            vios.append(checklib.violation(
                SCRIPT, "error" if frac >= ERROR_FRAC else "warning",
                worst["pad"], None, worst["net"], [ref, r["ic"]],
                f"{r['ic']} reads {ref} ({ohms * 1e3:g} mOhm) "
                f"{100 * frac:.0f}% high: {names}. Take each tap from {ref}'s "
                "own pad on a track that carries no other current",
                SCRIPT, kind="kelvin_error", extra_mohm=checklib.rnd(total, 3),
                error_pct=checklib.rnd(100 * frac, 1)))
    return vios, facts, left


# ---- declared Kelvin sense taps (#936) --------------------------------------

def _pad_named(bg, name: str):
    """"R113.1" -> that pad, or None."""
    ref, _, num = str(name).rpartition(".")
    if not ref or not num:
        return None
    return next((p for p in bg.pads_of(ref=ref) if p.number == num), None)


def _sense_items(bg, nets: set[str]) -> list[tuple]:
    """(kind, key, layer, shape, label, obj) for every piece of copper on
    `nets`, and the copper of each net tie that joins two of them. key names
    the item across its layers (a via or a through pad is one item)."""
    items = []
    for t in (t for n in nets for t in bg.tracks_of(n)):
        items.append(("track", ("t", id(t)), t.layer, t.poly, "a power track", t))
    for v in (v for n in nets for v in bg.vias_of(n)):
        for l in v.layers:
            if l in bg.copper_layers:
                items.append(("via", ("v", id(v)), l, v.poly,
                              "a power via", v))
    ties = {c["ref"] for c in bg.fp_copper if len(c["nets"] & nets) >= 2}
    for p in (p for n in nets for p in bg.pads_of(n)):
        kind = "tie" if p.ref in ties else "pad"
        for l in p.layers:
            items.append((kind, ("p", id(p)), l, p.poly, f"{p.ref}.{p.number}", p))
    for c in bg.fp_copper:
        if c["ref"] in ties:
            items.append(("tie", ("c", id(c)), c["layer"], c["shape"],
                          f"net tie {c['ref']}", c))
    for z in (z for n in nets for z in bg.zones_of(n)):
        for l, polys in z.fills.items():
            for k, poly in enumerate(polys):
                items.append(("zone", ("z", z.zone_id, l, k), l, poly,
                              f"the {z.net} pour" + (f" {z.name}" if z.name else ""),
                              z))
    return items


def _far_mm(contact, pad_poly) -> float:
    """How far the contact reaches from the tap pad: its farthest point."""
    return max((pad_poly.distance(Point(c))
                for c in shapely.get_coordinates(contact)), default=0.0)


_ROOT = ("root",)


def root_block(neigh: dict, root) -> set:
    """The nodes that share a biconnected block with `root` (it not counted):
    those on a cycle through it. Tarjan's, iterative."""
    disc, low = {root: 0}, {root: 0}
    out, stack, t = set(), [], 1
    walk = [(root, iter(neigh[root]))]
    while walk:
        v, nbrs = walk[-1]
        for w in nbrs:
            if w not in disc:
                disc[w] = low[w] = t
                t += 1
                stack.append(w)
                walk.append((w, iter(neigh[w])))
                break
            low[v] = min(low[v], disc[w])
        else:
            walk.pop()
            if not walk:
                break
            p = walk[-1][0]
            low[p] = min(low[p], low[v])
            if low[v] >= disc[p]:
                block = []
                while True:
                    x = stack.pop()
                    block.append(x)
                    if x == v:
                        break
                if p == root and len(block) > 1:
                    out.update(block)
    return out


def sense_walk(bg, sense, tap) -> dict:
    """Walk the copper joined to the sense pad, outward, until it meets the tap
    pad. Power copper is every pad and pour, and every track and via on a path
    between two of them (not a dead end); the first contact the sense line
    makes with it is where the line really taps. A
    contact inside the tap pad's outline (or within SENSE_SLACK_MM of it, the
    overhang of a wide track ending on the pad) is the tap itself: the power
    track and the sense track may both end on it. Returns
    {reached, joins: [{label, layer, at, mm}]}."""
    nets = {sense.net, tap.net} - {None}
    items = _sense_items(bg, nets)
    by_layer: dict[str, list[tuple]] = {}
    by_key: dict[tuple, list[tuple]] = {}
    for it in items:
        by_layer.setdefault(it[2], []).append(it)
        by_key.setdefault(it[1], []).append(it)
    trees = {l: STRtree([it[3] for it in its]) for l, its in by_layer.items()}
    near = tap.poly.buffer(SENSE_SLACK_MM)
    start, goal = ("p", id(sense)), ("p", id(tap))

    def touching(key):
        """(other item, contact) for each item `key` touches off the tap."""
        for me in by_key[key]:
            l = me[2]
            for k in trees[l].query(me[3], predicate="intersects"):
                other = by_layer[l][int(k)]
                if other[1] == key:
                    continue
                contact = me[3].intersection(other[3])
                if not contact.is_empty:
                    yield other, contact

    # power copper carries current: each pad and pour, and each piece on a
    # path between two of them (the sense line, whose sense pad is no
    # terminal, is a dead end). Pruning dead ends one by one missed a
    # router's line whose short 45-degree jogs overlap in a cycle (#1043): a
    # piece is on such a path when it shares a biconnected block with a
    # root joined to every terminal
    terminal = {it[1] for it in items if it[0] in ("pad", "zone")} - {start}
    neigh: dict[tuple, set] = {k: set() for k in by_key if k != start}
    for k in neigh:
        for other, contact in touching(k):
            o = other[1]
            if o != start and (goal in (k, o) or not near.contains(contact)):
                neigh[k].add(o)
                neigh[o].add(k)
    neigh[_ROOT] = set(terminal)
    for k in terminal:
        neigh[k].add(_ROOT)
    power = terminal | root_block(neigh, _ROOT)

    seen, queue = {start}, [start]
    joins, reached = [], False
    while queue:
        for other, contact in touching(queue.pop(0)):
            if other[1] == goal:
                reached = True
                continue
            if other[1] in seen:
                continue
            # a contact by the tap is the tap itself when it meets power
            # copper; the sense line's own short jogs there still lead on
            # to the pad (#1042)
            if near.contains(contact) and other[1] in power:
                continue
            seen.add(other[1])
            if other[1] in power:
                rest = contact.difference(near)
                c = rest.representative_point()
                joins.append({"label": other[4], "layer": other[2],
                              "at": [checklib.rnd(c.x, 2), checklib.rnd(c.y, 2)],
                              "mm": checklib.rnd(_far_mm(rest, tap.poly), 2)})
            else:
                queue.append(other[1])
    return {"reached": reached, "joins": joins}


def judge_sense(bg, power: list[dict]) -> tuple[list[dict], list[dict]]:
    """Each power entry's sense_pads {sense end pad: Kelvin pad} (#936): the
    sense line must leave the sense pad and end on the Kelvin pad on copper of
    its own, touching no pour and no other pad on the way."""
    vios, facts = [], []
    for e in power:
        net = e.get("net")
        for s_name, t_name in sorted((e.get("sense_pads") or {}).items()):
            sense, tap = _pad_named(bg, s_name), _pad_named(bg, t_name)
            bad = (f"{s_name} is not a pad on this board" if sense is None
                   else f"{t_name} is not a pad on this board" if tap is None
                   else f"{t_name} is on {tap.net}, not {net}" if tap.net != net
                   else f"{s_name} and {t_name} are the same pad"
                   if sense is tap else None)
            if bad:
                vios.append(checklib.violation(
                    SCRIPT, "error", None, None, net, [s_name, t_name],
                    f"power {net} sense_pads {s_name} -> {t_name}: {bad}, so "
                    "the sense line can't be checked. Name the pad the sense "
                    "line starts on and the pad it must tap, as REF.PAD",
                    SCRIPT, kind="sense_tap_bad"))
                continue
            w = sense_walk(bg, sense, tap)
            facts.append({"net": net, "sense": s_name, "tap": t_name, **w})
            refs = sorted({sense.ref, tap.ref})
            if w["joins"]:
                worst = max(w["joins"], key=lambda j: j["mm"])
                where = "; ".join(
                    f"{j['label']} at ({j['at'][0]}, {j['at'][1]}) on {j['layer']}, "
                    f"{j['mm']:.1f} mm from {t_name}" for j in w["joins"][:4])
                more = (f" and {len(w['joins']) - 4} more"
                        if len(w["joins"]) > 4 else "")
                vios.append(checklib.violation(
                    SCRIPT, "error", worst["at"], worst["layer"], net, refs,
                    f"{s_name}'s sense line on {net} joins other copper before "
                    f"it reaches {t_name}: {where}{more}. It reads the voltage "
                    f"there, not at {t_name}. Run it from {s_name} to {t_name} "
                    "on a track of its own, cleared from the pour",
                    SCRIPT, kind="sense_tap_off_pad",
                    tap_mm=worst["mm"], joins=len(w["joins"])))
            elif not w["reached"]:
                vios.append(checklib.violation(
                    SCRIPT, "error", [checklib.rnd(c, 2) for c in sense.center],
                    None, net, refs,
                    f"{s_name}'s sense line on {net} never reaches {t_name} on "
                    f"copper of its own (not routed?). Run it from {s_name} to "
                    f"{t_name} on a track that touches nothing else",
                    SCRIPT, kind="sense_tap_missing"))
    return vios, facts


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints", help="constraints.json, for power_paths and power[].sense_pads")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    bg = geom.load_board(Path(a.pcb))
    cons = checklib.load_json(a.constraints, "constraints") \
        if a.constraints else {}
    power = [e for e in cons.get("power") or [] if e.get("sense_pads")]
    if power or any(readers(bg, r, n) for r, _, n in shunts(bg)):
        bg.assert_fresh()
    vios, facts, left = judge(bg, cons.get("power_paths") or [])
    s_vios, sense = judge_sense(bg, power)
    return checklib.report(
        SCRIPT, a.pcb, vios + s_vios, shunts=facts, sense_taps=sense,
        not_judged=left,
        nothing_to_judge=None if facts or power else
        "no shunt read by an amplifier and no sense_pads declared on this "
        "board"), a.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
