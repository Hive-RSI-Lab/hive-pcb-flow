#!/usr/bin/env python3
"""sim_si.py - crosstalk by coupled-line simulation (#497).

    hwde sim_si --pcb B --xtalk --aggressor A --victim V [--constraints C]
                [--t-rise-ns T] [--swing V] [--engine auto|cpl|modal|xyce|both]
                [--out R.json] [--json]

check_si's crosstalk is Bogatin's closed form of a step on a matched line. This
simulates what is actually there. Both nets' pad-to-pad paths come off the
board; wherever the aggressor runs beside the victim on one layer, the coupled
section's 2x2 L, C is solved on the board's stackup (lib/impedance.lc_microstrip
/ lc_stripline, the same grid as the estimate) and joined to the rest of each
path by T lines. Terminals are the real ones:

  driver      R_drv (si.nets[net].driver_ohm, default 20) + any series R on that
              pad's end; on the aggressor a linear ramp behind them.
  receiver    R_in || C_in (si.nets[net].c_in_pf, default 3).
  other pads  a resistor to a rail is its parallel termination at that point of
              the path; a capacitor to a rail its value; an IC pin C_in. One
              tapping a coupled section sits at the section's nearer end. Off-path
              copper isn't modelled (`off_path_mm` says how much).

Which end drives: si.nets[net].driver ("U1" or "U1.3"), else the end with the
series R, else an oscillator part. With none of those it isn't known, so every
orientation is run and the worst kept (`driver_assumed`).

The ramp is t_r long, 0-100. A real edge with that 10-90 has a peak dV/dt of
1.02 V/t_r (Gaussian), and peak dV/dt is what FEXT follows, so ramp length =
the class's t_r gives FEXT the real slope and is what check_si's formula uses.

Engines (lib/coupledline.py). `auto` runs ngspice's CPL, with the copper's DC R,
and the modal network (two T lines per section between ideal transformers, exact
and lossless) on the same deck: CPL's peak is reported, the modal one replaces it
if they differ by more than CPL_DISAGREE, because CPL has failed silently here
(coupledline CPL_MIN_R). On hifi core's I2S deck CPL, modal, Xyce and an LC ladder
agree within 0.5%. `cpl` and `modal` run one. `xyce` is the modal deck in Xyce 7.10,
which has no multiconductor line device. `both` adds Xyce to auto.

Reported: each victim end's peak, signed, as volts and against
  budget   check_si's VICTIM_BUDGET x the aggressor's swing, for comparison;
  margin   the receiver's noise margin: si.nets[victim].noise_margin_v, else
           NOISE_MARGIN_FRAC x its swing for a digital victim (LVCMOS33's
           V_IL 0.8 - V_OL 0.4 = 0.4 V of 3.3), the budget for an analog one.

Not modelled: drivers' IBIS (#486), a coupled victim on another layer
(broadside), third conductors in the cross-section, loss above DC. Exit 0 clean,
1 over budget at a receiver, 2 cannot run.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import checklib  # noqa: E402
import coupledline as cl  # noqa: E402
import geom  # noqa: E402
import impedance  # noqa: E402
import netconn  # noqa: E402
import solvers  # noqa: E402
from checklib import CheckError, violation  # noqa: E402
from shapely.geometry import Point  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_si  # noqa: E402

SCRIPT = "sim_si"
NOISE_MARGIN_FRAC = 0.12     # LVCMOS33: (0.8 - 0.4) / 3.3
C_IN_PF = 3.0                # a CMOS input with its package
R_IN_OHM = 1e6
SECTION_STEPS = 50.0         # a coupled run under t_r / this joins a neighbour
MIN_LINE_MM = 0.01           # uncoupled pieces under this are a node
LUMPED_STEPS = 50.0          # an uncoupled span under t_r / this is a wire
DECK_TIMEOUT_S = 30.0        # a deck takes well under a second
DECK_RETRY_S = 120.0         # a deck that timed out gets one more go (#1377)
CPL_DISAGREE = 0.10          # CPL vs modal peak, past which CPL isn't believed
CPL_FLOOR_V = 0.002          # ... when the difference is at least this


@dataclass
class Piece:
    s0: float
    s1: float
    track: object      # geom.Track
    line: object       # shapely LineString, oriented along the path


class NetPath:
    """A net's copper between two pads, walked in order."""

    def __init__(self, bg, lm, net, a, b):
        self.net, self.a, self.b, self.lm = net, a, b, lm
        g = netconn.build(bg, net)
        r = netconn.shortest_path(g, netconn.pad_node(g, a), netconn.pad_node(g, b))
        if r is None:
            raise CheckError(f"{net}: no copper path {a.ref}.{a.number} to {b.ref}.{b.number}")
        self.pieces: list[Piece] = []
        cur, s = Point(a.center), 0.0
        for _, _, _, eid in r[1]:
            t = g.tracks.get(eid)
            if t is None or t.length <= 0:
                continue
            xy = list(t.shape.coords)
            if Point(xy[0]).distance(cur) > Point(xy[-1]).distance(cur):
                xy = xy[::-1]
            line = type(t.shape)(xy)
            self.pieces.append(Piece(s, s + t.length, t, line))
            s += t.length
            cur = Point(xy[-1])
        self.length = s
        on = {id(p.track) for p in self.pieces}
        self.off_path_mm = sum(t.length for t in bg.tracks_of(net) if id(t) not in on)

    def project(self, pt, layer):
        """(s, distance to centreline, heading, width) on `layer`, nearest."""
        best = None
        for p in self.pieces:
            if p.track.layer != layer:
                continue
            d = p.line.distance(pt)
            if best is None or d < best[1]:
                at = p.line.project(pt)
                best = (p.s0 + at, d, check_si._heading(p.line, at), p.track.width)
        return best

    def s_of(self, xy) -> float:
        best, sb = None, 0.0
        pt = Point(xy)
        for p in self.pieces:
            d = p.line.distance(pt)
            if best is None or d < best:
                best, sb = d, p.s0 + p.line.project(pt)
        return sb

    def spans(self, s0, s1):
        """[(length_mm, z0, td_s)] of the uncoupled copper from s0 to s1."""
        out = []
        for p in self.pieces:
            lo, hi = max(s0, p.s0), min(s1, p.s1)
            if hi - lo <= 0:
                continue
            ln = self.lm.line(p.track.layer, p.track.width)
            z = ln["z0"]
            if z is None:
                raise CheckError(f"{self.net}: no reference plane under {p.track.layer}")
            td = (hi - lo) * ln["tpd_ps_mm"] * 1e-12
            if out and abs(out[-1][1] - z) < 1e-6:
                out[-1] = (out[-1][0] + hi - lo, z, out[-1][2] + td)
            else:
                out.append((hi - lo, z, td))
        return out


def section_lc(lm, layer, w1, w2, gap) -> dict:
    g = lm._geom(layer)
    if g[0] == "ms":
        return impedance.lc_microstrip(w1, w2, gap, g[1], g[2], g[3])
    return impedance.lc_stripline(w1, w2, gap, g[1], g[2], g[3], g[4], g[5])


def coupled_sections(lm, pa: NetPath, pv: NetPath, t_r_s: float,
                     sample=check_si.SAMPLE_MM) -> list[dict]:
    """Runs of pa beside pv as sections {a0, a1, v0, v1, layer, wa, wv, gap, lc}.
    Same test as check_si.crosstalk: same layer, within 6 h, within 30 deg; a
    sample's gap goes on check_si's grid so the section's L, C is the solve the
    estimate used."""
    samples = []
    for p in pa.pieces:
        n = max(1, int(p.track.length / sample))
        dx = p.track.length / n
        h = lm.height(p.track.layer)
        for i in range(n):
            d = (i + 0.5) * dx
            pt = p.line.interpolate(d)
            f = pv.project(pt, p.track.layer)
            if f is None:
                continue
            sv, dist, hv, wv = f
            if sv <= 1e-6 or sv >= pv.length - 1e-6:
                continue   # past the victim's end: its foot isn't abreast
            gap = dist - (p.track.width + wv) / 2.0
            ha = check_si._heading(p.line, d)
            dang = abs((math.degrees(ha - hv) + 90.0) % 180.0 - 90.0)
            if gap <= 0 or gap > check_si.CROSSTALK_GAP_H * h + p.track.width \
                    or dang > check_si.PARALLEL_DEG:
                continue
            samples.append({"a": p.s0 + d, "v": sv, "dx": dx, "gap": gap,
                            "layer": p.track.layer, "wa": p.track.width, "wv": wv})
    secs: list[dict] = []
    for s in samples:
        bin_gap = max(check_si.GAP_BIN_MM,
                      round(s["gap"] / check_si.GAP_BIN_MM) * check_si.GAP_BIN_MM)
        cur = secs[-1] if secs else None
        joined = cur and s["a"] - s["dx"] / 2 - cur["a1"] < 1e-6 and cur["layer"] == s["layer"]
        if joined and cur["gap"] == bin_gap and cur["wa"] == s["wa"] and cur["wv"] == s["wv"]:
            cur["a1"], cur["v1"] = s["a"] + s["dx"] / 2, s["v"]
            cur["n"] += 1
            continue
        secs.append({"a0": s["a"] - s["dx"] / 2, "a1": s["a"] + s["dx"] / 2, "v0": s["v"],
                     "v1": s["v"], "layer": s["layer"], "wa": s["wa"], "wv": s["wv"],
                     "gap": bin_gap, "n": 1, "after": bool(joined),
                     "lc": section_lc(lm, s["layer"], s["wa"], s["wv"], bin_gap)})
    v_mm_s = min((1.0 / lm.line(s["layer"], s["wa"])["tpd_ps_mm"] for s in samples),
                 default=0.0) * 1e12
    l_min = v_mm_s * t_r_s / SECTION_STEPS
    secs = _merge_short(secs, l_min)
    for s in secs:
        _stretch_short(s, l_min)
    return _on_victim(secs, pv)


def _merge_lc(x, lx, y, ly) -> dict:
    """L and C per metre of two runs taken as one: per-length totals average."""
    k = lx + ly
    return {m: [[(x[m][i][j] * lx + y[m][i][j] * ly) / k for j in (0, 1)] for i in (0, 1)]
            for m in ("L", "C")}


def _merge_short(secs, l_min):
    """A run electrically shorter than the edge can see joins the neighbour it
    touches, L and C averaged by length (per-length totals are what a slow edge
    feels), so the deck isn't many few-ps sections."""
    i = 0
    while i < len(secs):
        s = secs[i]
        if s["a1"] - s["a0"] >= l_min:
            i += 1
            continue
        nb = i - 1 if s["after"] and i > 0 else (
            i + 1 if i + 1 < len(secs) and secs[i + 1]["after"] else None)
        if nb is None:
            i += 1
            continue
        lo, hi = sorted((i, nb))
        x, y = secs[lo], secs[hi]
        lx, ly = x["a1"] - x["a0"], y["a1"] - y["a0"]
        x.update(lc=_merge_lc(x["lc"], lx, y["lc"], ly),
                 gap=(x["gap"] * lx + y["gap"] * ly) / (lx + ly),
                 a1=y["a1"], v1=y["v1"], n=x["n"] + y["n"])
        del secs[hi]
        i = lo
    return secs


def _stretch_short(s, l_min):
    """An isolated run under l_min keeps its mutual totals (Cm len, Lm len) over
    l_min of line: coupling right, delay long by < t_r / SECTION_STEPS."""
    length = s["a1"] - s["a0"]
    s["model_mm"] = length
    if length >= l_min:
        return
    r = length / l_min
    s["model_mm"] = l_min
    s["lc"] = {"L": [[s["lc"]["L"][0][0], s["lc"]["L"][0][1] * r],
                     [s["lc"]["L"][1][0] * r, s["lc"]["L"][1][1]]],
               "C": [[s["lc"]["C"][0][0], s["lc"]["C"][0][1] * r],
                     [s["lc"]["C"][1][0] * r, s["lc"]["C"][1][1]]]}


def _on_victim(secs, pv):
    """Sections' ends on the victim: half a sample past the feet, joined runs
    sharing a node. Sections whose victim stretch overlaps one kept earlier
    (two aggressor runs beside one stretch) are dropped and counted."""
    kept: list[dict] = []
    for i, s in enumerate(secs):
        if s["after"] and kept and kept[-1] is secs[i - 1]:
            s["v0"] = kept[-1]["v1"]
        else:
            step = 1.0 if s["v1"] >= s["v0"] else -1.0
            s["v0"] = min(pv.length, max(0.0, s["v0"] - step * 0.125))
        step = 1.0 if s["v1"] >= s["v0"] else -1.0
        s["v1"] = min(pv.length, max(0.0, s["v1"] + step * 0.125))
        lo, hi = sorted((s["v0"], s["v1"]))
        clash = any(max(lo, min(k["v0"], k["v1"])) < min(hi, max(k["v0"], k["v1"])) - 1e-6
                    for k in kept)
        s["dropped"] = clash
        if not clash:
            kept.append(s)
    return secs


# ------------------------------------------------------------ terminals

def terminal_pads(bg, net):
    """The two farthest connected pads of a net, as check_si.line_length."""
    pads = bg.pads_of(net)
    g = netconn.build(bg, net)
    named = [(p, netconn.pad_node(g, p)) for p in pads]
    named = [(p, n) for p, n in named if n]
    best = None
    for i, (p, a) in enumerate(named):
        for q, b in named[i + 1:]:
            d = netconn.shortest_path_len(g, a, b)
            if d is not None and (best is None or d > best[0]):
                best = (d, p, q)
    return (best[1], best[2]) if best else None


def _pad_name(p):
    return f"{p.ref}.{p.number}"


def driver_end(bg, net, spec, a, b, power_nets):
    """(driving pad or None) by the module doc's rule."""
    want = spec.get("driver")
    for p in (a, b):
        if want and want in (p.ref, _pad_name(p)):
            return p
    series = {s["ref"] for s in check_si.series_resistors(bg, net, power_nets)}
    for p in (a, b):
        if p.ref in series:
            return p
    osc, _ = check_si.oscillator_parts(bg, net, power_nets)
    for p in (a, b):
        if p.ref in osc:
            return p
    return None


def _series_ohm(bg, pad, net, power_nets) -> float:
    for s in check_si.series_resistors(bg, net, power_nets):
        if s["ref"] == pad.ref:
            return s["ohm"] or 0.0
    return 0.0


def _value(bg, ref):
    return (bg.footprints.get(ref) or {}).get("value")


def _rail_resistor(bg, pad, power_nets):
    """Ohms of a 2-pad resistor from this pad to a rail, else None."""
    pads = bg.pads_of(ref=pad.ref)
    if not pad.ref.startswith("R") or len(pads) != 2:
        return None
    other = next(q for q in pads if q is not pad)
    if not other.net or not check_si.is_power(other.net, power_nets):
        return None
    return check_si.ohms(_value(bg, pad.ref)) or None


def side_loads(bg, net, terminals, spec, power_nets):
    """[(pad, kind, value)] for the net's other pads (module doc)."""
    out = []
    series = {s["ref"] for s in check_si.series_resistors(bg, net, power_nets)}
    c_in = float(spec.get("c_in_pf", C_IN_PF)) * 1e-12
    for p in bg.pads_of(net):
        if any(p is t for t in terminals):
            continue
        two = len(bg.pads_of(ref=p.ref)) == 2
        if two and p.ref in series:
            continue
        val = _value(bg, p.ref)
        if two and p.ref.startswith("R"):
            ohm = _rail_resistor(bg, p, power_nets)
            if ohm:
                out.append((p, "r", ohm))
            continue
        if two and p.ref.startswith("C"):
            f = _farads(val)
            if f:
                out.append((p, "c", f))
            continue
        out.append((p, "c", c_in))
    return out


def _farads(val):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([pnuµ]?)", str(val or ""))
    if not m:
        return None
    return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "": 1.0}[m.group(2)]


# ------------------------------------------------------------ the deck

def _end_elements(deck, node, pad, is_driver, net, spec, bg, power_nets, source=None):
    """A terminal: driver (optionally the aggressor's ramp) or receiver."""
    rs = _series_ohm(bg, pad, net, power_nets)
    if is_driver:
        r = float(spec.get("driver_ohm", check_si.DRIVER_OHM)) + rs
        if source:
            deck.ramp(f"{node}_src", source["v"], source["t0"], source["t_r"])
            deck.r(f"{node}_src", node, max(r, 0.01))
        else:
            deck.r(node, 0, max(r, 0.01))
        return r
    term = _rail_resistor(bg, pad, power_nets)
    if term:                 # the far pad is the parallel termination itself
        deck.r(node, 0, term)
        return None
    c_in = float(spec.get("c_in_pf", C_IN_PF)) * 1e-12
    rx = node
    if rs:
        rx = f"{node}_rx"
        deck.r(node, rx, rs)
    deck.r(rx, 0, float(spec.get("r_in_ohm", R_IN_OHM)))
    deck.c(rx, 0, c_in)
    return None


def _line_nodes(deck, prefix, path: NetPath, intervals, marks, min_td=0.0) -> dict:
    """T lines along path outside `intervals`; {s: node} at every break. A span
    under min_td is a wire (its ends share a node): ngspice's timestep collapses
    on a T line of a few ps, and the edge can't see it."""
    breaks = sorted({0.0, path.length, *marks, *(x for iv in intervals for x in iv)})
    uniq: list[float] = []
    for b in breaks:
        if not uniq or b - uniq[-1] > MIN_LINE_MM:
            uniq.append(b)
    uniq[-1] = max(uniq[-1], path.length)

    def snap(s):
        return min(uniq, key=lambda u: abs(u - s))
    nodes = {u: f"{prefix}{i}" for i, u in enumerate(uniq)}
    cov = [tuple(sorted((snap(x), snap(y)))) for x, y in intervals]
    k = 0
    for u0, u1 in zip(uniq, uniq[1:]):
        if any(lo <= u0 and u1 <= hi for lo, hi in cov):
            continue
        prev = nodes[u0]
        spans = path.spans(u0, u1)
        if sum(sp[2] for sp in spans) < min_td:
            nodes[u1] = prev      # a wire: the two breaks are one node
            continue
        for j, (_, z, td) in enumerate(spans):
            nxt = nodes[u1] if j == len(spans) - 1 else f"{prefix}x{k}"
            k += 1
            deck.line(prev, nxt, z, td)
            prev = nxt
    return {"nodes": nodes, "snap": snap}


def _clear_of(s, intervals):
    """A load tapping a coupled section sits at the section's nearer end: a
    section has no nodes inside it."""
    for x, y in intervals:
        lo, hi = sorted((x, y))
        if lo < s < hi:
            return lo if s - lo <= hi - s else hi
    return s


def build_deck(bg, lm, pa, pv, secs, a_driver, v_driver, spec_a, spec_v, swing, t_r_s,
               power_nets):
    """The deck of one orientation, with probes. Returns (deck, receivers, drivers)."""
    deck = cl.Deck(title=f"crosstalk {pa.net} -> {pv.net}")
    kept = [s for s in secs if not s["dropped"]]
    a_loads = side_loads(bg, pa.net, (pa.a, pa.b), spec_a, power_nets)
    v_loads = side_loads(bg, pv.net, (pv.a, pv.b), spec_v, power_nets)
    a_iv = [(s["a0"], s["a1"]) for s in kept]
    v_iv = [(s["v0"], s["v1"]) for s in kept]
    a_marks = [_clear_of(pa.s_of(p.center), a_iv) for p, _, _ in a_loads]
    v_marks = [_clear_of(pv.s_of(p.center), v_iv) for p, _, _ in v_loads]
    min_td = t_r_s / LUMPED_STEPS
    an = _line_nodes(deck, "a", pa, a_iv, a_marks, min_td)
    vn = _line_nodes(deck, "v", pv, v_iv, v_marks, min_td)
    for s in kept:
        t = lm.st.copper_thickness.get(s["layer"], 0.035)
        deck.coupled(an["nodes"][an["snap"](s["a0"])], vn["nodes"][vn["snap"](s["v0"])],
                     an["nodes"][an["snap"](s["a1"])], vn["nodes"][vn["snap"](s["v1"])],
                     s["lc"], s["model_mm"] * 1e-3,
                     (cl.copper_r(s["wa"], t), cl.copper_r(s["wv"], t)))
    src = {"v": swing, "t0": 0.1e-9, "t_r": t_r_s}
    a_near, a_far = (an["nodes"][an["snap"](0.0)], an["nodes"][an["snap"](pa.length)])
    v_near, v_far = (vn["nodes"][vn["snap"](0.0)], vn["nodes"][vn["snap"](pv.length)])
    for node, pad in ((a_near, pa.a), (a_far, pa.b)):
        drv = pad is a_driver
        _end_elements(deck, node, pad, drv, pa.net, spec_a, bg, power_nets, src if drv else None)
    ends = {}
    for node, pad in ((v_near, pv.a), (v_far, pv.b)):
        drv = pad is v_driver
        _end_elements(deck, node, pad, drv, pv.net, spec_v, bg, power_nets)
        ends[_pad_name(pad)] = {"node": node, "receiver": not drv}
    for (p, kind, val), s in zip(a_loads, a_marks):
        n = an["nodes"][an["snap"](s)]
        (deck.r if kind == "r" else deck.c)(n, 0, val)
    for (p, kind, val), s in zip(v_loads, v_marks):
        n = vn["nodes"][vn["snap"](s)]
        (deck.r if kind == "r" else deck.c)(n, 0, val)
        if kind == "c" and not p.ref.startswith("C"):
            ends[_pad_name(p)] = {"node": n, "receiver": True}
    delay = sum(sp[2] for sp in pa.spans(0, pa.length)) + sum(sp[2] for sp in pv.spans(0, pv.length))
    rc = 5 * 100 * float(spec_v.get("c_in_pf", C_IN_PF)) * 1e-12
    deck.tstop = src["t0"] + t_r_s + 6 * delay + rc
    deck.tstep = t_r_s / 50.0
    deck.probes = sorted({e["node"] for e in ends.values()})
    return deck, ends


def _peaks(deck, ends, mode, engine, timeout=DECK_TIMEOUT_S) -> dict:
    r = deck.run(mode, engine, timeout=timeout)
    return {name: cl.peak(r[e["node"]]) for name, e in ends.items()}


def _orientations(a, b, known):
    return [known] if known is not None else [a, b]


def simulate_pair(bg, lm, aggressor, victim, specs=None, si=None, power_nets=frozenset(),
                  t_rise_ns=None, swing=None, engine="auto",
                  timeout=DECK_TIMEOUT_S) -> dict:
    """One aggressor -> victim. {sections, ends, noise_v, budget_v, margin_v,
    engines, ...}, or {"not_simulated": why}."""
    specs = specs or {}
    si = si or {}
    sa, sv = specs.get(aggressor) or {}, specs.get(victim) or {}
    ta, tv = terminal_pads(bg, aggressor), terminal_pads(bg, victim)
    if not ta or not tv:
        return {"not_simulated": "a net has no copper path between two pads"}
    pa, pv = NetPath(bg, lm, aggressor, *ta), NetPath(bg, lm, victim, *tv)
    t_r_s = float(t_rise_ns if t_rise_ns is not None else sa.get("t_rise_ns", 2.0)) * 1e-9
    secs = coupled_sections(lm, pa, pv, t_r_s)
    kept = [s for s in secs if not s["dropped"]]
    if not kept:
        return {"not_simulated": "no parallel run between the two nets' pad-to-pad paths",
                "off_path_mm": {aggressor: checklib.rnd(pa.off_path_mm, 1),
                                victim: checklib.rnd(pv.off_path_mm, 1)}}
    v = float(swing if swing is not None else sa.get("swing_v", check_si.SWING_V))
    drv_a = driver_end(bg, aggressor, sa, *ta, power_nets)
    drv_v = driver_end(bg, victim, sv, *tv, power_nets)
    best = None
    for da in _orientations(*ta, drv_a):
        for dv in _orientations(*tv, drv_v):
            deck, ends = build_deck(bg, lm, pa, pv, secs, da, dv, sa, sv, v, t_r_s, power_nets)
            got = _run_engines(deck, ends, engine, timeout)
            rx = max((abs(got["peaks"][n]) for n, e in ends.items() if e["receiver"]), default=0.0)
            if best is None or rx > best["rx"]:
                best = {"rx": rx, "got": got, "ends": ends, "da": da, "dv": dv}
    vcls = check_si.victim_class(victim, si, power_nets, specs)
    budget = check_si.VICTIM_BUDGET.get(vcls, check_si.VICTIM_BUDGET["digital"]) * v
    margin = sv.get("noise_margin_v")
    if margin is None:
        margin = budget if vcls == "analog" else \
            NOISE_MARGIN_FRAC * float(sv.get("swing_v", check_si.SWING_V))
    return {
        "aggressor": aggressor, "victim": victim, "victim_class": vcls,
        "swing_v": v, "t_rise_ns": t_r_s * 1e9,
        "aggressor_path": {"from": _pad_name(best["da"]), "length_mm": checklib.rnd(pa.length, 2),
                           "off_path_mm": checklib.rnd(pa.off_path_mm, 1)},
        "victim_path": {"driver": _pad_name(best["dv"]), "length_mm": checklib.rnd(pv.length, 2),
                        "off_path_mm": checklib.rnd(pv.off_path_mm, 1)},
        "driver_assumed": {aggressor: drv_a is None, victim: drv_v is None},
        "sections": [{"layer": s["layer"], "length_mm": checklib.rnd(s["a1"] - s["a0"], 2),
                      "gap_mm": checklib.rnd(s["gap"], 3), "dropped": s["dropped"]} for s in secs],
        "coupled_mm": checklib.rnd(sum(s["a1"] - s["a0"] for s in kept), 2),
        "ends": {n: {"peak_mv": checklib.rnd(1000 * best["got"]["peaks"][n], 2),
                     "receiver": e["receiver"]} for n, e in best["ends"].items()},
        "noise_v": best["rx"], "noise_mv": checklib.rnd(1000 * best["rx"], 2),
        "budget_mv": checklib.rnd(1000 * budget, 1), "margin_mv": checklib.rnd(1000 * margin, 1),
        "over_budget": best["rx"] > budget, "over_margin": best["rx"] > margin,
        "engines": best["got"]["engines"],
    }


def _run_engines(deck, ends, engine, timeout=DECK_TIMEOUT_S) -> dict:
    if engine == "xyce":
        return {"peaks": _peaks(deck, ends, "modal", "xyce", timeout),
                "engines": {"used": "xyce modal"}}
    if engine == "cpl":
        return {"peaks": _peaks(deck, ends, "cpl", "ngspice", timeout),
                "engines": {"used": "ngspice cpl"}}
    modal = _peaks(deck, ends, "modal", "ngspice", timeout)
    if engine == "modal":
        return {"peaks": modal, "engines": {"used": "ngspice modal"}}
    cpl = _peaks(deck, ends, "cpl", "ngspice", timeout)
    diff = max(abs(cpl[n] - modal[n]) for n in ends)
    top = max(abs(modal[n]) for n in ends) or 1e-12
    bad = diff > CPL_FLOOR_V and diff / top > CPL_DISAGREE
    info = {"used": "ngspice modal (CPL disagreed)" if bad else "ngspice cpl",
            "cpl_vs_modal_mv": checklib.rnd(1000 * diff, 3)}
    if engine == "both":
        xy = _peaks(deck, ends, "modal", "xyce", timeout)
        info["xyce_vs_modal_mv"] = checklib.rnd(
            1000 * max(abs(xy[n] - modal[n]) for n in ends), 3)
    return {"peaks": modal if bad else cpl, "engines": info}


def available(engine="auto") -> str | None:
    """Why the engine can't run, or None."""
    need = ["xyce"] if engine == "xyce" else ["ngspice"] + (["xyce"] if engine == "both" else [])
    for t in need:
        if solvers.tool_path(t) is None:
            return f"{t} not found (no IIC-OSIC-TOOLS tree)"
    return None


def run(argv=None):
    ap = argparse.ArgumentParser(description="Crosstalk by coupled-line simulation.")
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--xtalk", action="store_true", required=True)
    ap.add_argument("--aggressor", required=True)
    ap.add_argument("--victim", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--t-rise-ns", type=float)
    ap.add_argument("--swing", type=float)
    ap.add_argument("--engine", choices=("auto", "cpl", "modal", "xyce", "both"), default="auto")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    why = available(args.engine)
    if why:
        raise CheckError(why)
    cons = checklib.load_json(args.constraints, "constraints") \
        if args.constraints and Path(args.constraints).is_file() else {}
    bg = geom.load_board(Path(args.pcb))
    power = {p.get("net") for p in cons.get("power", []) if isinstance(p, dict)}
    power |= {z.net for z in bg.zones_of() if z.net and not z.teardrop}  # #633
    lm = check_si.LineModel(bg)
    if not bg.tracks_of(args.aggressor) or not bg.tracks_of(args.victim):
        raise CheckError("a net has no tracks on this board")
    specs = check_si.resolve(bg, cons, power)
    res = simulate_pair(bg, lm, args.aggressor, args.victim, specs, cons.get("si") or {}, power,
                        args.t_rise_ns, args.swing, args.engine)
    vs = []
    if res.get("over_budget"):
        vs.append(violation(
            SCRIPT, "warning", check_si.rep_point(bg, args.victim), None, args.victim, [],
            f"{args.aggressor} puts {res['noise_mv']:.0f} mV on {args.victim}'s receiver, "
            f"budget {res['budget_mv']:.0f} mV, noise margin {res['margin_mv']:.0f} mV",
            SCRIPT, kind="si_crosstalk_sim", aggressor=args.aggressor,
            noise_mv=res["noise_mv"], budget_mv=res["budget_mv"], margin_mv=res["margin_mv"]))
    return checklib.report(SCRIPT, args.pcb, vs, xtalk=res), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
