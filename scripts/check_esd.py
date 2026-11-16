"""check_esd.py - ESD clamps at external connectors: present, first on the
line, short to ground (reference/ingest/emc-thermal.md, rules ESD-1..5).

One concern: an ESD strike at a connector pin must reach a clamp (TVS / ESD
array) before it reaches anything else, through as little inductance as the
layout allows. TI SLVA680A section 2 models the line as L1/L2 (connector to
clamp), L3 (clamp to ground) and L4 (clamp to the protected IC); what reaches
the IC is

    V = Vclamp + Rdyn * I + (L2 + L3) * dI/dt

and an IEC 61000-4-2 level-4 contact strike (8 kV) rises to 30 A in 0.8 ns,
dI/dt = 3.75e10 A/s (IEC 61000-4-2:2008 Table 3), so every 1 nH of shared
path adds ~37 V. A thin PCB trace's partial inductance is ~1 nH/mm (Grover's
bar formula, reproduced in reference/ingest/emc-thermal/verify.py) and a
1.6 mm through via ~1.2 nH, which turns the layout into a volts estimate.

Which connectors: refs J*, P*, CN*, X*, USB* (not TP*), or constraints.json
{"esd": {"connectors": [...]}}; {"esd": {"internal": [...]}} drops board-to-
board ones nobody touches, and so do FFC / FPC / board-to-board Values and
(#1496) pin header, pin socket and JST ZH / SH / PH footprints, the
programming, board-to-board and in-housing harness families; {"esd":
{"external": [...]}} keeps a ref judged all the same. Which nets: every
pad's net but ground-like (GND, AGND, SHIELD, CHASSIS, EARTH...) and
unconnected ones; {"esd": {"exempt_nets": [...]}} drops more. Which parts
clamp: Value / footprint / MPN matching CLAMP_RE, or {"esd": {"clamps":
[...]}}. A connector with its own isolation transformer (an RJ45 magjack,
BARRIER_RE, or {"esd": {"barriers": [...]}}) has its lines recorded behind
the barrier and not judged.

Per connector line, the clamp is the clamp pad on the connector's net, or one
two-pin series part (resistor, bead, inductor, fuse) away. A rail's TVS
behind its input fuse / PPTC (an F ref) is the usual order and passes. With
no clamp, a capacitor of at least 1 uF from the line to ground that the
strike reaches before any IC pin is the charge sink (the IEC source's 150 pF
x 8 kV is 1.2 uC, 1.2 V on 1 uF; shunt_c records the estimate). Distances
are along the routed tracks (Dijkstra over the net's segments, vias and
pads); a net with no routed path falls back to the straight line and says
so.

Findings:
  esd_unclamped        error: a signal line with no clamp (power lines:
                       warning; a power rail is often clamped by its own
                       regulator's input TVS elsewhere)
  esd_behind_series    warning: the clamp sits behind a series part, so the
                       strike crosses that part first (SLVA680 wants the line
                       to run straight to the clamp pin)
  esd_branch_first     warning: another part's pad is reached before the
                       clamp, off the connector-clamp path: the strike splits
                       there (SLVA680 Fig 2-5 case 2, L2)
  esd_via_first        warning: vias between the connector and the clamp
                       (SLVA680 2.3: avoid; each ~1.2 nH)
  esd_clamp_far        warning: clamp more than FAR_MM of track from the pin
                       (judgement: no vendor gives a number; "as close as
                       design rules allow", SLVAEX9)
  esd_ground_far       warning: the clamp's ground pad has no ground via or
                       through pad within GND_VIA_MM (SLVA680 2.4: "one VIA
                       immediately adjacent to the ground pin")

  esd_corridor_coupled warning: another net's track runs in the corridor
                       beside the connector-to-clamp stretch (same or
                       adjacent copper layer) and the strike's dI/dt couples
                       more than the victim's abs max into it (SLVA680 2.2,
                       emc-thermal ESD-9). V = M dI/dt, M summed over every
                       parallel piece.

  esd_clamp_cap        error: the clamp's capacitance (datasheet max) is
                       over the line's interface budget (CAP_BUDGET_PF);
                       warning when the sheet gives only a typical figure
  esd_clamp_vrwm       error: the clamp's stand-off voltage (VRWM) is below
                       the line's working voltage, so it conducts in normal
                       use
  esd_pair_mismatch    warning: the two clamps on a USB D+/D- pair differ in
                       capacitance by more than PAIR_TOL (USB 2.0 7.1.6.1)
  esd_clamp_bipolar    error: a line marked bipolar (esd.bipolar_v) swings
                       below ground past a one-way clamp's forward drop
                       while the clamp's ground pin is on ground, so its
                       ground-side diode conducts every negative half-cycle;
                       or a two-way clamp stands off less than the peak

Clamp ratings (#310) come only from reference/esd_clamps.yaml, rows read from
each maker's datasheet (an LCSC row for a second source's own sheet first,
else the maker's row by name). A clamp with no row is not judged, and says
so. The interface: constraints esd.interfaces {net glob: usb_fs | usb_hs |
usb_ss5 | usb_ss10 | usb_cc | none}, else the net's name (USB D+/D- is taken
as full speed; the high-speed budget is reported as a fact, hs_cap). HDMI,
Ethernet, RF and the rest have no cited per-line limit: not judged. The
working voltage: constraints esd.line_v (a number or {net glob: V}), else
USB 2.0 VOH max on D+/D-, VBUS (or pd_source.contract_v), or the volts in a
power rail's name; CC and other signals are not judged. Every clamped line
carries fact["rating"].

A bipolar line (one that swings both sides of ground: E1/T1, RS-232, audio,
a transformer's line side) is marked by constraints esd.bipolar_v (a number
or {net glob: peak V}); no line is taken as bipolar by its name. The row's
polarity (uni: one way, a diode from ground to the line, forward drop vf;
bi: blocks both ways up to its stand-off) is the datasheet's. A one-way
clamp with a pin on ground fails when the negative peak passes vf; one with
no pin on ground (its GND pin left floating, as on a transformer's line
side) clamps only line to line through its diodes and Zener, and is not
judged. A two-way clamp fails when its VRWM is below the peak. No polarity
or no vf in the row: not judged, and it says so.

The corridor (ESD-9): every segment of the routed connector-to-clamp path is
the aggressor. A victim piece is another net's track segment on the same or
an adjacent copper layer, within the corridor, at most 30 degrees off the
aggressor's direction (perpendicular filaments have no partial mutual). M is
Grover's filament mutual (lib/emsolve.filament_mutual, the bars' GMD as d; it
matches FastHenry within 0.2 %, `sim_solve validate --only fasthenry_pair`)
times cos(angle). Over a ground plane (the nearest other layer whose GND fill
covers the aggressor) the strike returns in the plane, so M is the loop
mutual by images, M(d) - M(d_image); with no plane it is the free-space
partial mutual (an upper bound). Ground nets are not victims; another
clamped connector line (the pair partner) and power rails (decoupled) are
listed, not judged. Victim abs max: constraints esd.victim_abs_max_v (a
number or {net glob: V}), else the lowest numeric abs max of the ICs' pins on
that net (sim_inrush.pin_abs_max: the datasheet extractions in --parts, by
default the board's own parts/, then reference/pin_ratings.yaml), else
VICTIM_ABS_MAX_V = 3.6 (judgement: VDD + 0.3 of 3.3 V CMOS; the finding names
which one it used). Tracks only: another net's pour is not a victim. Corridor half-width: constraints
esd.corridor_mm, else (a judgement, like FAR_MM) the lateral gap at which a
neighbour running the stretch's whole length on its layer would pick up
CORRIDOR_V (constraints esd.corridor_v), capped at CORRIDOR_MAX_MM. The
coupled volts are open-circuit volts in the victim's loop: a screen, not a
sign-off (a terminated victim sees less).

Every line also reports est_extra_v: (shared path + ground path) inductance
times the strike's dI/dt at the level asked ({"esd": {"level_kv": 8}}). It
is an order-of-magnitude screen, not a sign-off: a trace over a solid plane
has less loop inductance than its partial self-inductance, so it errs high.

Runs in the verify suite (scripts/checks.d/check_esd.yaml); by hand:
    bin/hwde check_esd --pcb board.kicad_pcb [--constraints c.json] [--parts D]
CLI exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import fnmatch
import heapq
import math
import re
import sys
from pathlib import Path

from shapely.geometry import Point

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import emsolve  # noqa: E402
import geom  # noqa: E402

SCRIPT = "check_esd"
SOURCE = SCRIPT

CONNECTOR_RE = re.compile(r"^(J|P|CN|X|USB)\d", re.I)
CLAMP_RE = re.compile(
    r"SMBJ|SMAJ|SMCJ|SMF\d|P6KE|P4SMA|PESD|ESD\d|ESD[A-Z]|\bESD|TVS|SD05|"
    r"SRV05|USBLC|PRTR5V|TPD\dE|TPD\dS|RCLAMP|SP05\d|SP30\d|SP72\d|SP3012|"
    r"IP4220|NUP\d|CDSOT|LESD|D5V0|ULC\d|SMDA|TPD4S|IP4234|SZESD|"
    r"VARISTOR|\bMOV\b", re.I)
GROUND_RE = re.compile(r"(^|[^A-Z])(A|D|P|S)?GND|GROUND|SHIELD|CHASSIS|EARTH|"
                       r"^VSS|^0V$", re.I)
POWER_RE = re.compile(r"^\+|VBUS|^VCC|^VDD|^VIN|^VSYS|^VBAT|^V\d|PWR|^\d+V\d*",
                      re.I)
SERIES_REF_RE = re.compile(r"^(R|FB|L|F)\d", re.I)
# board-to-board by their Value: a flat-flex or board-to-board connector
# carries lines between two of the product's own boards, which no finger
# touches once it is built (constraints esd.connectors brings one back)
INTERNAL_RE = re.compile(r"\bFFC\b|\bFPC\b|B2B|BOARD.TO.BOARD", re.I)
# internal by footprint (#1496): pin headers and sockets (KiCad
# PinHeader_/PinSocket_, the aiee/EasyEDA HDR- family) and JST's small
# wire-to-board families ZH (1.5 mm), SH (1.0 mm) and PH (2.0 mm) carry
# programming, board-to-board and in-housing harness lines; a cable that
# leaves the product ends in a jack, a USB or RJ receptacle, a terminal
# block or a power-rated housing (JST VH/XH are left judged: they carry
# field wiring as often as a harness). A judgement: constraints
# esd.external lists a ref that is user-facing all the same.
INTERNAL_FP_RE = re.compile(
    r"PinHeader_|PinSocket_|:HDR-|JST_(?:ZH|SH|PH)_", re.I)
# a connector whose lines sit behind its own isolation transformer: an RJ45
# with integrated magnetics (HanRun HR911105A, "magjack"); the cable side is
# the transformer's, the board side never sees the strike's common mode.
# constraints esd.barriers adds more
BARRIER_RE = re.compile(r"HR911105|MAG.?JACK|INTEGRATED.MAGNETICS", re.I)
FUSE_REF_RE = re.compile(r"^F\d", re.I)
IC_REF_RE = re.compile(r"^(U|IC)\d", re.I)
CAP_REF_RE = re.compile(r"^C(?!ONN)[A-Z]*\d", re.I)   # C3, Cin1, CX2
# a shunt capacitor this large soaks the strike's charge: the IEC 61000-4-2
# source is 150 pF at the level's volts (Figure 1), so 8 kV dumps 1.2 uC,
# 1.2 V on 1 uF (a judgement on the threshold: the charge sum, not a
# vendor number)
SHUNT_MIN_F = 1e-6
STRIKE_F = 150e-12

# IEC 61000-4-2:2008 Table 3: 3.75 A per kV first peak, 0.8 ns 10-90 rise
AMPS_PER_KV = 3.75
RISE_S = 0.8e-9
NH_PER_MM = 1.0      # thin trace partial self-inductance (verify.py, Grover)
NH_PER_VIA = 1.2     # 1.6 mm through via, 0.3 mm drill (verify.py, Johnson)
FAR_MM = 10.0        # judgement (see docstring)
GND_VIA_MM = 2.0     # judgement: pad edge to the nearest ground via
JOIN_MM = 0.01
VICTIM_ABS_MAX_V = 3.6   # judgement: VDD + 0.3 of 3.3 V CMOS
CORRIDOR_V = 3.6         # judgement: corridor = where a full-length run gets this
CORRIDOR_MAX_MM = 10.0
CORRIDOR_MIN_MM = 0.5
PARALLEL_COS = math.cos(math.radians(30))


def dI_dt(level_kv: float) -> float:
    return AMPS_PER_KV * level_kv / RISE_S


def is_ground(net: str | None) -> bool:
    return bool(net) and bool(GROUND_RE.search(net))


def is_power(net: str) -> bool:
    return bool(POWER_RE.search(net))


def fp_text(fp: dict) -> str:
    """Value, library name and the part properties (not the free-text
    description or datasheet link)."""
    props = [str(v) for k, v in (fp.get("props") or {}).items()
             if not re.search(r"descr|datasheet|^ki_", k, re.I)]
    return " ".join([fp.get("value") or "", fp.get("lib") or ""] + props)


def shunt_cap(bg, cp, on_net: list, best: dict) -> dict | None:
    """The line's nearest capacitor of at least SHUNT_MIN_F to ground that
    the strike reaches before any IC pin on the line (along the tracks, or
    the straight line when unrouted): {ref, farads, mm, routed}."""
    from check_decoupling import parse_farads

    def reach(p):
        n = _pad_node(p)
        if n in best:
            return best[n][0], True
        return math.dist(cp.copper_center, p.copper_center), False
    first_ic = min((reach(p)[0] for p in on_net if IC_REF_RE.match(p.ref)),
                   default=math.inf)
    out = None
    for p in on_net:
        if not CAP_REF_RE.match(p.ref):
            continue
        two = bg.pads_of(ref=p.ref)
        if len(two) != 2 or not any(is_ground(q.net) for q in two
                                    if q.number != p.number):
            continue
        f = parse_farads(bg.footprints.get(p.ref, {}).get("value"))
        if f is None or not SHUNT_MIN_F <= f < 1.0:
            continue
        mm, routed = reach(p)
        if mm < first_ic and (out is None or mm < out["mm"]):
            out = {"ref": p.ref, "farads": f, "mm": mm, "routed": routed}
    return out


def is_clamp(fp: dict, extra: set, ref: str) -> bool:
    if ref in extra:
        return True
    text = " ".join([fp.get("value") or "", fp.get("lib") or ""] +
                    [str(v) for v in (fp.get("props") or {}).values()])
    return bool(CLAMP_RE.search(text))


# ----------------------------------------------------------- the net graph

class NetGraph:
    """A net's routed copper as a graph: nodes are pads ("p", ref, num),
    track ends ("t", i, end) and vias ("v", i); edges are segments (their
    length) and zero-length joins where copper touches."""

    def __init__(self, bg, net: str):
        self.adj: dict = {}
        self.pos: dict = {}
        tracks = bg.tracks_of(net)
        self.tracks = tracks
        vias = bg.vias_of(net)
        pads = bg.pads_of(net)
        ends = []
        for i, t in enumerate(tracks):
            a, b = t.shape.coords[0], t.shape.coords[-1]
            na, nb = ("t", i, 0), ("t", i, 1)
            self.pos[na], self.pos[nb] = a, b
            self._edge(na, nb, t.length)
            ends += [(na, a, t.layer, t.width), (nb, b, t.layer, t.width)]
        for j, v in enumerate(vias):
            self.pos[("v", j)] = v.at
            self.adj.setdefault(("v", j), [])
        for p in pads:
            n = ("p", p.ref, p.number)
            self.pos[n] = p.copper_center
            self.adj.setdefault(n, [])
        for k, (na, a, la, wa) in enumerate(ends):
            for nb, b, lb, wb in ends[k + 1:]:
                if la == lb and math.dist(a, b) <= (wa + wb) / 2 + JOIN_MM \
                        and na[1] != nb[1]:
                    self._edge(na, nb, math.dist(a, b))
            for j, v in enumerate(vias):
                if v.spans(la) and math.dist(a, v.at) <= v.diameter / 2 + JOIN_MM:
                    self._edge(na, ("v", j), 0.0)
            for p in pads:
                if p.on(la) and p.poly.buffer(JOIN_MM).contains(Point(a)):
                    self._edge(na, ("p", p.ref, p.number), 0.0)
        for j, v in enumerate(vias):
            for p in pads:
                if any(p.on(l) for l in v.layers) and \
                        p.poly.buffer(JOIN_MM).contains(Point(v.at)):
                    self._edge(("v", j), ("p", p.ref, p.number), 0.0)

    def _edge(self, a, b, w):
        self.adj.setdefault(a, []).append((b, w))
        self.adj.setdefault(b, []).append((a, w))

    def paths_from(self, src):
        """Dijkstra: node -> (distance, previous node)."""
        best = {src: (0.0, None)}
        heap = [(0.0, 0, src)]
        tie = 0
        while heap:
            d, _, n = heapq.heappop(heap)
            if d > best[n][0]:
                continue
            for m, w in self.adj.get(n, []):
                nd = d + w
                if m not in best or nd < best[m][0] - 1e-12:
                    best[m] = (nd, n)
                    tie += 1
                    heapq.heappush(heap, (nd, tie, m))
        return best

    @staticmethod
    def walk(best, dst) -> list:
        out, n = [], dst
        while n is not None:
            out.append(n)
            n = best[n][1]
        return out[::-1]


def _pad_node(p) -> tuple:
    return ("p", p.ref, p.number)


def _line_to(bg, graph, best, src_pad, dst_pad) -> dict:
    """Routed distance, path and via count from src_pad to dst_pad (straight
    line when the net doesn't connect them by track)."""
    dst = _pad_node(dst_pad)
    if dst in best:
        path = NetGraph.walk(best, dst)
        return {"mm": best[dst][0], "routed": True, "path": path,
                "vias": sum(1 for n in path if n[0] == "v")}
    return {"mm": math.dist(src_pad.copper_center, dst_pad.copper_center),
            "routed": False, "path": [], "vias": 0}


def ground_reach(bg, pad) -> float | None:
    """Pad edge to the nearest ground via or through ground pad (mm)."""
    cands = [v.poly for v in bg.vias_of() if is_ground(v.net)]
    cands += [p.poly for p in bg.pads_of() if is_ground(p.net)
              and p.drill is not None and p.ref != pad.ref]
    if not cands:
        return None
    return min(pad.poly.distance(c) for c in cands)


# ------------------------------------------------------------ the corridor

def _z(stack, layer: str) -> float:
    return stack.height_between(stack.copper_layers[0], layer)


def _pieces(track) -> list:
    """(p0, p1) straight pieces of a track's centreline (arcs are polylines)."""
    c = list(track.shape.coords)
    return [(c[i], c[i + 1]) for i in range(len(c) - 1)
            if math.dist(c[i], c[i + 1]) > 1e-6]


def stretch_tracks(graph, path) -> list:
    """The net's tracks the connector-to-clamp path runs along."""
    out, seen = [], set()
    for a, b in zip(path, path[1:]):
        if a[0] == b[0] == "t" and a[1] == b[1] and a[1] not in seen:
            seen.add(a[1])
            out.append(graph.tracks[a[1]])
    return out


def ground_plane(bg, layer: str, pt) -> str | None:
    """The nearest other copper layer whose ground fill covers pt."""
    st = bg.stackup
    i = st.index(layer)
    order = sorted((l for l in st.copper_layers if l != layer),
                   key=lambda l: abs(st.index(l) - i))
    P = Point(pt)
    for l in order:
        for z in bg.zones_of(layer=l):
            if is_ground(z.net) and z.fill_on(l).covers(P):
                return l
    return None


def pair_mutual(p0, p1, layer_a, w_a, q0, q1, layer_v, w_v, stack,
                plane) -> tuple[float, float, float] | None:
    """(M H, parallel mm, lateral gap mm) of victim piece q on aggressor
    piece p, or None when they are not parallel enough."""
    ux, uy = p1[0] - p0[0], p1[1] - p0[1]
    la = math.hypot(ux, uy)
    ux, uy = ux / la, uy / la
    vx, vy = q1[0] - q0[0], q1[1] - q0[1]
    lv = math.hypot(vx, vy)
    cos = (ux * vx + uy * vy) / lv
    if abs(cos) < PARALLEL_COS:
        return None
    b1 = (q0[0] - p0[0]) * ux + (q0[1] - p0[1]) * uy
    b2 = (q1[0] - p0[0]) * ux + (q1[1] - p0[1]) * uy
    mx, my = (q0[0] + q1[0]) / 2 - p0[0], (q0[1] + q1[1]) / 2 - p0[1]
    lat = abs(mx * uy - my * ux)
    overlap = max(0.0, min(la, max(b1, b2)) - max(0.0, min(b1, b2)))
    if overlap <= 0:
        return None
    za, zv = _z(stack, layer_a), _z(stack, layer_v)
    t = stack.copper_thickness.get(layer_a, 0.035)
    w = (w_a + w_v) / 2
    d = emsolve.rect_gmd(w, t, lat, za - zv, n=4) if lat < 5 * w + 1 else \
        math.hypot(lat, za - zv)
    lo, hi = sorted((b1, b2))
    m = emsolve.filament_mutual(0.0, la, lo, hi, d)
    if plane is not None:   # the strike returns in the plane: its image
        zp = _z(stack, plane)
        m -= emsolve.filament_mutual(0.0, la, lo, hi,
                                     math.hypot(lat, 2 * zp - za - zv))
    # summed as magnitudes: a net's pieces are drawn in either direction, so
    # the sign of cos says nothing about its current (an upper bound)
    return abs(cos) * max(m, 0.0), overlap, lat


def corridor_width(length_mm: float, h: float | None,
                   didt: float, volts: float) -> float:
    """Lateral gap (mm) at which a track running the whole stretch beside it
    picks up `volts` (bisection), within [CORRIDOR_MIN_MM, CORRIDOR_MAX_MM]."""
    if length_mm <= 0:
        return CORRIDOR_MIN_MM

    def v(d):
        m = emsolve.filament_mutual(0, length_mm, 0, length_mm, d)
        if h is not None:
            m -= emsolve.filament_mutual(0, length_mm, 0, length_mm,
                                         math.hypot(d, 2 * h))
        return m * didt
    lo, hi = CORRIDOR_MIN_MM, CORRIDOR_MAX_MM
    if v(hi) >= volts:
        return hi
    if v(lo) <= volts:
        return lo
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if v(mid) > volts else (lo, mid)
    return hi


def rated_pins(bg, parts_dir: Path | None, nets: set) -> dict:
    """net -> (lowest numeric abs max of the ICs' pins on it, where from)."""
    import sim_board
    import sim_inrush
    comps = {}
    for p in bg.pads_of():
        if p.net in nets:
            ref = comps.setdefault(p.ref, {
                "ref": p.ref, "pins": {},
                "value": bg.footprints.get(p.ref, {}).get("value") or "",
                "kind": (re.match(r"[A-Za-z]*", p.ref).group(0)).upper()})
            ref["pins"][p.number] = {
                "net": p.net, "fn": sim_board.norm_fn(p.pinfunction, p.number)}
    parts = {}
    pj = parts_dir / "parts.json" if parts_dir else None
    if pj and pj.is_file():
        for row in checklib.load_json(pj, "parts.json").get("parts") or []:
            for r in row.get("refdes") or []:
                parts[r] = row
    rated, _ = sim_inrush.pin_abs_max({"comps": comps}, parts, parts_dir, nets,
                                      library=True)
    out: dict = {}
    for r in rated:
        if r["net"] not in out or r["max_v"] < out[r["net"]][0]:
            out[r["net"]] = (r["max_v"], f"{r['ref']} pin {r['pin']}: {r['source']}")
    return out


def victim_limit(cfg: dict, net: str, rated: dict) -> tuple[float, str]:
    vm = cfg.get("victim_abs_max_v")
    if isinstance(vm, (int, float)):
        return float(vm), "constraints esd.victim_abs_max_v"
    if isinstance(vm, dict):
        for pat, v in vm.items():
            if fnmatch.fnmatchcase(net, pat):
                return float(v), f"constraints esd.victim_abs_max_v[{pat}]"
    if net in rated:
        return rated[net]
    return VICTIM_ABS_MAX_V, "default (judgement: VDD + 0.3 of 3.3 V CMOS)"


def corridor(bg, cfg: dict, net: str, stretch: list, didt: float,
             clamped_nets: set, parts_dir: Path | None
             ) -> tuple[dict, list[dict]]:
    """Facts for one connector-to-clamp stretch and its neighbours
    [{net, layer, parallel_mm, gap_mm, m_nh, v, limit_v, judged}]."""
    st = bg.stackup
    length = sum(t.length for t in stretch)
    longest = max(stretch, key=lambda t: t.length)
    mid = longest.shape.interpolate(0.5, normalized=True).coords[0]
    plane0 = ground_plane(bg, longest.layer, mid)
    h0 = st.height_between(longest.layer, plane0) if plane0 else None
    if cfg.get("corridor_mm") is not None:
        width, src = float(cfg["corridor_mm"]), "constraints esd.corridor_mm"
    else:
        width = corridor_width(length, h0, didt,
                               float(cfg.get("corridor_v", CORRIDOR_V)))
        src = ("judgement: the gap where a full-length neighbour picks up "
               f"{float(cfg.get('corridor_v', CORRIDOR_V)):g} V")
    fact = {"stretch_mm": checklib.rnd(length, 2),
            "layers": sorted({t.layer for t in stretch}),
            "plane": plane0, "plane_h_mm": None if h0 is None else
            checklib.rnd(h0, 3),
            "model": "loop_over_plane" if plane0 else "partial_free_space",
            "corridor_mm": checklib.rnd(width, 2), "corridor_source": src}
    by_net: dict = {}
    for t in stretch:
        near = [t.layer] + [l for l in st.adjacent(t.layer) if l]
        tm = t.shape.interpolate(0.5, normalized=True).coords[0]
        plane = ground_plane(bg, t.layer, tm)
        zone = t.shape.buffer(width + t.width / 2)
        for l in near:
            for v in bg.tracks_of(layer=l):
                if (not v.net or v.net == net or is_ground(v.net)
                        or not zone.intersects(v.shape)):
                    continue
                vpart = v.shape.intersection(zone)
                for p0, p1 in _pieces(t):
                    for g in getattr(vpart, "geoms", [vpart]):
                        if g.geom_type != "LineString" or g.length < 1e-6:
                            continue
                        c = list(g.coords)
                        for q0, q1 in zip(c, c[1:]):
                            if math.dist(q0, q1) < 1e-6:
                                continue
                            r = pair_mutual(p0, p1, t.layer, t.width, q0, q1,
                                            l, v.width, st, plane)
                            if r is None:
                                continue
                            m, par, lat = r
                            e = by_net.setdefault(v.net, {
                                "net": v.net, "layers": set(), "m": 0.0,
                                "parallel_mm": 0.0, "gap_mm": math.inf,
                                "at": q0})
                            e["layers"].add(l)
                            e["m"] += m
                            e["parallel_mm"] += par
                            gap = max(lat - (t.width + v.width) / 2, 0.0) \
                                if l == t.layer else lat
                            if gap < e["gap_mm"]:
                                e["gap_mm"], e["at"] = gap, q0
    out = []
    rated = rated_pins(bg, parts_dir, set(by_net)) if by_net else {}
    for n, e in sorted(by_net.items()):
        m = abs(e["m"])
        lim, lsrc = victim_limit(cfg, n, rated)
        judged = not (n in clamped_nets or is_power(n))
        out.append({"net": n, "layers": sorted(e["layers"]),
                    "parallel_mm": checklib.rnd(e["parallel_mm"], 2),
                    "gap_mm": checklib.rnd(e["gap_mm"], 3),
                    "m_nh": checklib.rnd(m * 1e9, 3),
                    "v": checklib.rnd(m * didt, 1), "limit_v": lim,
                    "limit_source": lsrc, "judged": judged,
                    "why_not": None if judged else
                    ("clamped connector line" if n in clamped_nets
                     else "power rail (decoupled)"),
                    "_at": e["at"]})
    return fact, out


# ------------------------------------------------- the clamps' ratings (#310)

# A clamp's capacitance against its interface's per-line budget (pF):
#   usb_fs   50: NXP AN11392 2.6, reference/interfaces/usb-fs.json
#   usb_hs   4: TI SLVAF82 (emc-thermal ESD-10), "< 4 pF"
#   usb_ss5  0.5, usb_ss10 0.3: TI SLVAF82
#   usb_cc   600: USB PD 5.8.6 cReceiver max, via onsemi AN-5086
#            (reference/interfaces/usbc-pd-sink.md)
# HDMI, Ethernet, RF and the rest: no cited per-line limit, not judged.
CAP_BUDGET_PF = {"usb_fs": 50.0, "usb_hs": 4.0, "usb_ss5": 0.5,
                 "usb_ss10": 0.3, "usb_cc": 600.0}
CAP_SOURCE = {"usb_fs": "NXP AN11392 2.6", "usb_hs": "TI SLVAF82",
              "usb_ss5": "TI SLVAF82", "usb_ss10": "TI SLVAF82",
              "usb_cc": "USB PD 5.8.6 cReceiver"}
INTERFACES = set(CAP_BUDGET_PF) | {"none"}
USB_DATA_V = 3.6     # USB 2.0 Table 7-7: VOH max 3.6 V on D+/D-
VBUS_V = 5.0         # USB default VBUS; a PD contract: pd_source.contract_v
PAIR_TOL = 0.10      # USB 2.0 7.1.6.1: D+/D- capacitance matched within 10 %
ESD_CLAMPS_YAML = HERE.parent / "reference" / "esd_clamps.yaml"
# the line's leaf name (after the last '/'), fullmatch; group pol = the
# polarity mark a D+/D- pair differs by
_USB_D_RES = [re.compile(p, re.I) for p in (
    r"D(?P<pol>[+-])", r"D_?(?P<pol>[PN])", r"D(?P<pol>M)",
    r"(?:\w*[_-])?USB\w*?[_-]?D(?P<pol>[+-])(?:_[IO])?",
    r"(?:\w*[_-])?USB\w*?[_-]?D_?(?P<pol>[PNM])(?:_[IO])?",
    r"(?:\w*[_-])?USB\d?[_-]?(?P<pol>[PN])",
    r"D_?USB\d?_?(?P<pol>[PN])")]
_CC_RE = re.compile(r"(?:\w*[_-])?CC[12]", re.I)
_SS_RE = re.compile(r"\w*SS_?(?:TX|RX)\w*", re.I)
_RAIL_V_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)V(\d+)?(?!\d)", re.I)
_LCSC_RE = re.compile(r"C\d{2,}")


def load_esd_clamps(path: Path = ESD_CLAMPS_YAML) -> list[dict]:
    import yaml
    rows = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
        "parts") or []
    return [{**r, "_m": re.compile(r["match"], re.I)} for r in rows]


def _leaf(net: str) -> str:
    return net.rsplit("/", 1)[-1]


def usb_data_pol(net: str) -> tuple[str, str] | None:
    """(pair key, polarity) for a USB D+/D- line by its name, else None."""
    leaf = _leaf(net)
    for rx in _USB_D_RES:
        m = rx.fullmatch(leaf)
        if m:
            g = "pol"
            s, e = m.span(g)
            pol = m.group(g).upper()
            return (net[:len(net) - len(leaf)] + leaf[:s] + "*" + leaf[e:],
                    "P" if pol in "+P" else "N")
    return None


def line_interface(net: str, power: bool, cfg: dict) -> tuple[str | None, str]:
    """(interface, where it came from): constraints esd.interfaces
    {glob: kind} first, else the net's name. None: no budget known."""
    for glob, kind in (cfg.get("interfaces") or {}).items():
        if fnmatch.fnmatchcase(net, glob) or fnmatch.fnmatchcase(_leaf(net), glob):
            if kind not in INTERFACES:
                return None, (f"esd.interfaces {glob}: {kind!r} is not one of "
                              f"{', '.join(sorted(INTERFACES))}")
            return (None if kind == "none" else kind), f"esd.interfaces {glob}"
    if power:
        return None, "power rail"
    if usb_data_pol(net):
        return "usb_fs", "name (USB D+/D-; full speed assumed, esd.interfaces sets usb_hs)"
    if _CC_RE.fullmatch(_leaf(net)):
        return "usb_cc", "name (USB-C CC)"
    if _SS_RE.fullmatch(_leaf(net)):
        return "usb_ss5", "name (USB SuperSpeed, 5 Gbps assumed)"
    return None, "no interface known by name (esd.interfaces sets one)"


def working_v(net: str, power: bool, iface: str | None, cfg: dict,
              constraints: dict) -> tuple[float | None, str]:
    lv = cfg.get("line_v")
    if isinstance(lv, (int, float)) and not isinstance(lv, bool):
        return float(lv), "esd.line_v"
    if isinstance(lv, dict):
        for glob, v in lv.items():
            if fnmatch.fnmatchcase(net, glob) or fnmatch.fnmatchcase(_leaf(net), glob):
                return float(v), f"esd.line_v {glob}"
    if iface in ("usb_fs", "usb_hs"):
        return USB_DATA_V, "USB 2.0 Table 7-7 VOH max"
    if not power:
        return None, "not a power rail or a USB 2.0 data line (esd.line_v sets one)"
    if re.search(r"VBUS", net, re.I):
        pd = (constraints.get("pd_source") or {}).get("contract_v")
        if pd:
            return float(pd), "pd_source.contract_v"
        return VBUS_V, "USB default VBUS (a PD contract: pd_source.contract_v)"
    m = _RAIL_V_RE.search(_leaf(net))
    if m:
        v = float(m.group(1)) + (float("0." + m.group(2)) if m.group(2) else 0.0)
        return v, "the rail's name"
    return None, "no voltage in the rail's name (esd.line_v sets one)"


def bipolar_v(net: str, cfg: dict) -> tuple[float | None, str | None]:
    """The peak a line marked bipolar swings to on each side of ground:
    constraints esd.bipolar_v (a number, or {net glob: peak V}), else None."""
    bv = cfg.get("bipolar_v")
    if isinstance(bv, (int, float)) and not isinstance(bv, bool):
        return float(bv), "esd.bipolar_v"
    if isinstance(bv, dict):
        for glob, v in bv.items():
            if fnmatch.fnmatchcase(net, glob) or fnmatch.fnmatchcase(_leaf(net), glob):
                return float(v), f"esd.bipolar_v {glob}"
    return None, None


def clamp_row(fp: dict, rows: list[dict]) -> dict | None:
    """An LCSC row for the part's LCSC code first, else a maker row (no
    lcsc) matching its value or MPN."""
    props = fp.get("props") or {}
    lcsc = next((str(v).strip() for k, v in props.items()
                 if re.search(r"lcsc|jlc", k, re.I)
                 and _LCSC_RE.fullmatch(str(v).strip())), None)
    text = " ".join([fp.get("value") or ""] + [
        str(v) for k, v in props.items() if not k.startswith("ki_")])
    for r in rows:
        if r.get("lcsc") and lcsc and str(r["lcsc"]) == lcsc and r["_m"].search(text):
            return r
    for r in rows:
        if not r.get("lcsc") and r["_m"].search(text):
            return r
    return None


def _rate_bipolar(bg, fact, r, row, vrwm, vfrom, cfg, at) -> list:
    """A line marked bipolar against the clamp's polarity (the row's, from
    its datasheet): r["bipolar_check"] pass / fail / not_judged, or None on
    a line not marked bipolar."""
    bv, bfrom = bipolar_v(fact["net"], cfg)
    r["bipolar_v"], r["bipolar_v_from"] = bv, bfrom
    r["polarity"] = row.get("polarity") if row else None
    if bv is None:
        r["bipolar_check"] = None
        return []
    ref, net = fact["clamp"], fact["net"]
    gnd = sorted({p.net for p in bg.pads_of(ref=ref) if is_ground(p.net)})
    r["ground_pins_on"] = gnd
    pol = r["polarity"]
    if not row:
        r["bipolar_check"] = "not_judged"
        return []
    if pol not in ("uni", "bi"):
        r["bipolar_check"] = "not_judged"
        r["why_not"].append("bipolar: the row gives no polarity")
        return []
    head = (f"{fact['connector']} pin {fact['pin']} ({net}): the clamp {ref} "
            f"({r['part']})")
    if pol == "bi":
        if vrwm is None:
            r["bipolar_check"] = "not_judged"
            r["why_not"].append("bipolar: the row gives no stand-off voltage")
            return []
        r["bipolar_check"] = "fail" if vrwm < bv else "pass"
        if vrwm >= bv:
            return []
        return [checklib.violation(
            SCRIPT, "error", at(fact), None, net, [fact["connector"], ref],
            f"{head} is two-way but stands off only {vrwm:g} V ({vfrom}) on "
            f"a line that swings to +/-{bv:g} V ({bfrom}): it conducts on "
            "both peaks; use a clamp whose VRWM is at least the peak",
            SOURCE, kind="esd_clamp_bipolar", polarity=pol, vrwm=vrwm,
            bipolar_v=bv, bipolar_v_from=bfrom)]
    if not gnd:
        r["bipolar_check"] = "not_judged"
        r["why_not"].append(
            "bipolar: no pin of the one-way clamp is on ground, so it clamps "
            "only line to line (diodes and Zener in series): not judged")
        return []
    vf = row.get("vf")
    if vf is None:
        r["bipolar_check"] = "not_judged"
        r["why_not"].append("bipolar: the row gives no forward drop (vf)")
        return []
    vf = float(vf)
    r["vf"] = vf
    r["bipolar_check"] = "fail" if bv > vf else "pass"
    if bv <= vf:
        return []
    return [checklib.violation(
        SCRIPT, "error", at(fact), None, net, [fact["connector"], ref],
        f"{head} is one-way with its ground pin on {', '.join(gnd)}, and the "
        f"line swings to -{bv:g} V ({bfrom}), past the clamp's {vf:g} V "
        f"forward drop (esd_clamps.yaml, {row.get('vf_kind', 'vf')}): its "
        "ground-side diode conducts every negative half-cycle and clips the "
        "signal; use a two-way clamp rated for the peak, or clamp line to "
        "line with its ground pin left off ground", SOURCE,
        kind="esd_clamp_bipolar", polarity=pol, vf=vf, bipolar_v=bv,
        bipolar_v_from=bfrom, ground_pins_on=gnd)]


def rate_clamps(bg, lines: list, cfg: dict, constraints: dict,
                rows: list[dict]) -> list:
    """Capacitance against the interface's budget, VRWM against the line's
    working voltage, and D+/D- clamps matched; a fact on every line."""
    fps = bg.footprints
    viols = []

    def at(fact):
        cp = next((p for p in bg.pads_of(ref=fact["connector"])
                   if p.number == fact["pin"]), None)
        return cp.copper_center if cp else (0.0, 0.0)

    pairs: dict = {}
    for fact in lines:
        ref = fact.get("clamp")
        if not ref:
            continue
        fp = fps.get(ref, {})
        row = clamp_row(fp, rows)
        net, power = fact["net"], fact["power"]
        iface, ifrom = line_interface(net, power, cfg)
        wv, wfrom = working_v(net, power, iface, cfg, constraints)
        r = {"part": (fp.get("value") or ""), "row": row["source"] if row else None,
             "interface": iface, "interface_from": ifrom, "budget_pf": None,
             "cap_pf": None, "cap_kind": None, "vrwm": None, "vrwm_from": None,
             "working_v": wv, "working_v_from": wfrom, "why_not": []}
        fact["rating"] = r
        vrwm, vfrom = (row.get("vrwm"), f"esd_clamps.yaml ({row.get('vrwm_kind', 'vrwm')})") \
            if row and row.get("vrwm") is not None else (None, None)
        if vrwm is None and power:
            from surgelib import family_clamp
            text = " ".join([fp.get("value") or ""] + [
                str(v) for v in (fp.get("props") or {}).values()])
            fam = family_clamp(text)
            if fam and fam.get("vrwm"):
                vrwm, vfrom = fam["vrwm"], "the TVS family name (stand-off in the part number)"
        r["vrwm"], r["vrwm_from"] = vrwm, vfrom
        if not row:
            r["why_not"].append("no row in reference/esd_clamps.yaml for this part")
        # capacitance
        cap, kind = None, None
        if row:
            if row.get("cap_pf_max") is not None:
                cap, kind = float(row["cap_pf_max"]), "max"
            elif row.get("cap_pf_typ") is not None:
                cap, kind = float(row["cap_pf_typ"]), "typ"
        r["cap_pf"], r["cap_kind"] = cap, kind
        if iface is None:
            r["cap"] = "not_judged"
            r["why_not"].append(f"capacitance: {ifrom}")
        elif cap is None:
            r["cap"] = "not_judged"
        else:
            budget = CAP_BUDGET_PF[iface]
            r["budget_pf"] = budget
            r["cap"] = "fail" if cap > budget else "pass"
            if iface == "usb_fs" and not ifrom.startswith("esd.interfaces"):
                r["hs_budget_pf"] = CAP_BUDGET_PF["usb_hs"]
                r["hs_cap"] = "fail" if cap > CAP_BUDGET_PF["usb_hs"] else "pass"
            if cap > budget:
                sev = "error" if kind == "max" else "warning"
                viols.append(checklib.violation(
                    SCRIPT, sev, at(fact), None, net,
                    [fact["connector"], ref],
                    f"{fact['connector']} pin {fact['pin']} ({net}): the clamp "
                    f"{ref} ({r['part']}) hangs {cap:g} pF ({kind}, its "
                    f"datasheet) on a {iface} line whose budget is "
                    f"{budget:g} pF ({CAP_SOURCE[iface]}): use a lower-"
                    "capacitance clamp", SOURCE, kind="esd_clamp_cap",
                    cap_pf=cap, cap_kind=kind, budget_pf=budget,
                    interface=iface, interface_from=ifrom))
        # working voltage
        if wv is None:
            r["vrwm_check"] = "not_judged"
            r["why_not"].append(f"working voltage: {wfrom}")
        elif vrwm is None:
            r["vrwm_check"] = "not_judged"
            if row:
                r["why_not"].append("the row gives no stand-off voltage")
        else:
            r["vrwm_check"] = "fail" if vrwm < wv else "pass"
            if vrwm < wv:
                viols.append(checklib.violation(
                    SCRIPT, "error", at(fact), None, net,
                    [fact["connector"], ref],
                    f"{fact['connector']} pin {fact['pin']} ({net}): the clamp "
                    f"{ref} ({r['part']}) stands off {vrwm:g} V ({vfrom}) on "
                    f"a line that works at {wv:g} V ({wfrom}): it conducts "
                    "in normal use; use a clamp whose VRWM is at least the "
                    "line's voltage", SOURCE, kind="esd_clamp_vrwm",
                    vrwm=vrwm, working_v=wv, working_v_from=wfrom))
        viols += _rate_bipolar(bg, fact, r, row, vrwm, vfrom, cfg, at)
        pol = usb_data_pol(net) if iface in ("usb_fs", "usb_hs") else None
        if pol:
            pairs.setdefault(pol[0], {})[pol[1]] = fact
    for key, pn in sorted(pairs.items()):
        if set(pn) != {"P", "N"}:
            continue
        fp_, fn_ = pn["P"], pn["N"]
        rp, rn = fp_["rating"], fn_["rating"]
        same = fp_["clamp"] == fn_["clamp"] or (
            rp["part"] and rp["part"] == rn["part"] and rp["row"] == rn["row"])
        verdict, why = "pass", None
        if same:
            why = "one part, or the same part on both lines"
        elif not (rp["row"] and rn["row"]):
            verdict, why = "not_judged", "a clamp of the pair has no row"
        else:
            rowp = clamp_row(bg.footprints.get(fp_["clamp"], {}), rows)
            rown = clamp_row(bg.footprints.get(fn_["clamp"], {}), rows)
            k = "cap_pf_typ" if rowp.get("cap_pf_typ") is not None and \
                rown.get("cap_pf_typ") is not None else "cap_pf_max"
            cp_, cn_ = rowp.get(k), rown.get(k)
            if cp_ is None or cn_ is None:
                verdict, why = "not_judged", "no like-for-like capacitance on both"
            elif abs(cp_ - cn_) > PAIR_TOL * max(cp_, cn_):
                verdict = "fail"
                viols.append(checklib.violation(
                    SCRIPT, "warning", at(fp_), None, fp_["net"],
                    [fp_["clamp"], fn_["clamp"]],
                    f"{fp_['net']} / {fn_['net']}: the clamps differ, "
                    f"{fp_['clamp']} {cp_:g} pF vs {fn_['clamp']} {cn_:g} pF "
                    f"({k[7:]}), more than {PAIR_TOL:.0%} apart (USB 2.0 "
                    "7.1.6.1 wants D+ and D- matched): use the same clamp on "
                    "both lines", SOURCE, kind="esd_pair_mismatch",
                    cap_p_pf=cp_, cap_n_pf=cn_))
        for f in (fp_, fn_):
            f["rating"]["pair"] = verdict
            f["rating"]["pair_with"] = (fn_ if f is fp_ else fp_)["net"]
            if why:
                f["rating"]["pair_why"] = why
    return viols


# --------------------------------------------------------------- the check

def check(pcb: Path, constraints: dict | None = None,
          parts_dir: Path | None = None) -> tuple[list, dict]:
    cfg = (constraints or {}).get("esd") or {}
    level_kv = float(cfg.get("level_kv", 8.0))
    didt = dI_dt(level_kv)
    explicit = set(cfg.get("connectors") or [])
    internal = set(cfg.get("internal") or [])
    external = set(cfg.get("external") or [])
    exempt = set(cfg.get("exempt_nets") or [])
    extra_clamps = set(cfg.get("clamps") or [])
    bg = geom.load_board(pcb)
    fps = bg.footprints
    clamps = {r for r, fp in fps.items() if is_clamp(fp, extra_clamps, r)}
    conns, skipped, by_fp = [], [], []
    for r in sorted(fps):
        if r in clamps or r in internal:
            continue
        if explicit:
            if r in explicit:
                conns.append(r)
            continue
        if not CONNECTOR_RE.match(r) or r.upper().startswith("TP"):
            continue
        if r not in external:
            if INTERNAL_RE.search(fps[r].get("value") or ""):
                skipped.append(r)
                continue
            if INTERNAL_FP_RE.search(fps[r].get("lib") or ""):
                by_fp.append(r)
                continue
        conns.append(r)
    barriers = sorted(r for r in conns if r in set(cfg.get("barriers") or [])
                      or BARRIER_RE.search(fp_text(fps[r])))
    graphs: dict = {}
    viols, lines = [], []
    pending = []

    def graph(net):
        if net not in graphs:
            graphs[net] = NetGraph(bg, net)
        return graphs[net]

    for ref in conns:
        seen_nets = set()
        for cp in bg.pads_of(ref=ref):
            net = cp.net
            if (not net or net in seen_nets or is_ground(net) or net in exempt
                    or "unconnected" in net.lower()):
                continue
            seen_nets.add(net)
            on_net = [p for p in bg.pads_of(net) if p.ref != ref]
            if not on_net:
                continue        # a pin wired to nothing else
            power = is_power(net)
            fact = {"connector": ref, "pin": cp.number, "net": net,
                    "power": power}
            lines.append(fact)
            if ref in barriers:
                fact.update(clamp=None, barrier=ref)
                continue
            g = graph(net)
            best = g.paths_from(_pad_node(cp))
            cpads = [p for p in on_net if p.ref in clamps]
            series = None
            if not cpads:
                # one two-pin series part away
                for sp in on_net:
                    if not SERIES_REF_RE.match(sp.ref):
                        continue
                    two = bg.pads_of(ref=sp.ref)
                    if len(two) != 2:
                        continue
                    other = next(p for p in two if p.number != sp.number)
                    if not other.net or other.net == net or is_ground(other.net):
                        continue
                    far = [p for p in bg.pads_of(other.net)
                           if p.ref in clamps]
                    if far:
                        series, cpads = sp.ref, far
                        break
            if not cpads:
                shunt = shunt_cap(bg, cp, on_net, best)
                if shunt:
                    fact.update(clamp=None, shunt_c={
                        "ref": shunt["ref"],
                        "uf": checklib.rnd(shunt["farads"] * 1e6, 3),
                        "mm": checklib.rnd(shunt["mm"], 2),
                        "routed": shunt["routed"],
                        "est_v": checklib.rnd(STRIKE_F * level_kv * 1e3
                                              / shunt["farads"], 3)})
                    continue
                sev = "warning" if power else "error"
                viols.append(checklib.violation(
                    SCRIPT, sev, cp.copper_center, None, net, [ref],
                    f"{ref} pin {cp.number} ({net}) leaves the board with no "
                    "ESD clamp on its line: add a TVS / ESD array at the "
                    "connector, or list the connector as internal "
                    "(constraints esd.internal) or the net as exempt",
                    SOURCE, kind="esd_unclamped", power=power))
                fact["clamp"] = None
                continue
            if series:
                fact["series"] = series
                sp = next(p for p in on_net if p.ref == series)
                line = _line_to(bg, g, best, cp, sp)
                fact.update(clamp=cpads[0].ref, mm=checklib.rnd(line["mm"], 2),
                            routed=line["routed"])
                if FUSE_REF_RE.match(series) and (
                        power or is_power(_leaf(net))):
                    # a rail's input fuse / PPTC ahead of its TVS is the
                    # usual order (the TVS's fault current opens it); the
                    # leaf name too, so a sheet's /12V_IN counts as a rail
                    fact["series_fuse"] = True
                    continue
                viols.append(checklib.violation(
                    SCRIPT, "warning", cp.copper_center, None, net,
                    [ref, series, cpads[0].ref],
                    f"{ref} pin {cp.number} ({net}): the clamp "
                    f"{cpads[0].ref} sits behind series part {series}, so "
                    "the strike crosses it first: move the clamp to the "
                    "connector side", SOURCE, kind="esd_behind_series"))
                continue
            routes = [(_line_to(bg, g, best, cp, p), p) for p in cpads]
            line, clamp = min(routes, key=lambda r: r[0]["mm"])
            fact.update(clamp=clamp.ref, clamp_pin=clamp.number,
                        mm=checklib.rnd(line["mm"], 2), routed=line["routed"],
                        vias=line["vias"])
            if line["routed"]:
                st_tracks = stretch_tracks(g, line["path"])
                if st_tracks:
                    pending.append((fact, net, st_tracks, clamp))
            # parts reached before the clamp, off its path
            on_path = set(line["path"])
            first = []
            if line["routed"]:
                for p in on_net:
                    n = _pad_node(p)
                    if p.ref in clamps or n in on_path or n not in best:
                        continue
                    if best[n][0] < line["mm"] - JOIN_MM:
                        first.append((best[n][0], p))
            # the clamp's stub off the line onward (the nearest other part
            # past it): (d(c,T) + d(T,X) - d(c,X)) / 2
            stub = 0.0
            if line["routed"]:
                tbest = g.paths_from(_pad_node(clamp))
                for p in on_net:
                    n = _pad_node(p)
                    if p.ref in clamps or n not in best or n not in tbest:
                        continue
                    s = (line["mm"] + tbest[n][0] - best[n][0]) / 2
                    stub = s if stub == 0.0 else min(stub, s)
            fact["stub_mm"] = checklib.rnd(max(stub, 0.0), 2)
            if first:
                d, p = min(first, key=lambda x: x[0])
                viols.append(checklib.violation(
                    SCRIPT, "warning", p.copper_center, None, net,
                    [ref, p.ref, clamp.ref],
                    f"{ref} pin {cp.number} ({net}): {p.ref} pin {p.number} "
                    f"is {d:.1f} mm from the pin, before the clamp "
                    f"{clamp.ref} ({line['mm']:.1f} mm) and off its path: "
                    "route the connector to the clamp pin first, then on "
                    "(SLVA680 2.3)", SOURCE, kind="esd_branch_first"))
            if line["vias"]:
                viols.append(checklib.violation(
                    SCRIPT, "warning", cp.copper_center, None, net,
                    [ref, clamp.ref],
                    f"{ref} pin {cp.number} ({net}): {line['vias']} via(s) "
                    f"between the pin and the clamp {clamp.ref} (~"
                    f"{NH_PER_VIA:g} nH each): keep the line on one layer "
                    "to the clamp", SOURCE, kind="esd_via_first",
                    vias=line["vias"]))
            if line["mm"] > FAR_MM:
                viols.append(checklib.violation(
                    SCRIPT, "warning", clamp.copper_center, None, net,
                    [ref, clamp.ref],
                    f"{ref} pin {cp.number} ({net}): the clamp {clamp.ref} is "
                    f"{line['mm']:.1f} mm of {'track' if line['routed'] else 'straight line'} "
                    f"from the pin (> {FAR_MM:g} mm): the strike's loop "
                    "radiates into everything along it; move the clamp to "
                    "the connector", SOURCE, kind="esd_clamp_far",
                    mm=checklib.rnd(line["mm"], 2)))
            gpads = [p for p in bg.pads_of(ref=clamp.ref) if is_ground(p.net)]
            gmm = None
            if gpads:
                reach = [ground_reach(bg, p) for p in gpads]
                reach = [r for r in reach if r is not None]
                gmm = min(reach) if reach else None
            fact["ground_via_mm"] = None if gmm is None else checklib.rnd(gmm, 2)
            if gpads and (gmm is None or gmm > GND_VIA_MM):
                where = ("no ground via on the board" if gmm is None else
                         f"the nearest ground via is {gmm:.1f} mm away")
                viols.append(checklib.violation(
                    SCRIPT, "warning", gpads[0].copper_center, None,
                    gpads[0].net, [clamp.ref],
                    f"{clamp.ref}: {where} from its ground pad (> "
                    f"{GND_VIA_MM:g} mm): put a via to the ground plane at "
                    "the pad (SLVA680 2.4)", SOURCE, kind="esd_ground_far",
                    ground_via_mm=fact["ground_via_mm"]))
            nh = (max(stub, 0.0) + (gmm or 0.0)) * NH_PER_MM
            fact["est_extra_nh"] = checklib.rnd(nh, 2)
            fact["est_extra_v"] = checklib.rnd(nh * 1e-9 * didt, 1)
    clamped = {f["net"] for f in lines if f.get("clamp")}
    for fact, net, st_tracks, clamp in pending:
        cf, neigh = corridor(bg, cfg, net, st_tracks, didt, clamped, parts_dir)
        for nb in neigh:
            at = nb.pop("_at")
            if nb["judged"] and nb["v"] > nb["limit_v"]:
                viols.append(checklib.violation(
                    SCRIPT, "warning", at, None, nb["net"],
                    [fact["connector"], clamp.ref],
                    f"{fact['connector']} pin {fact['pin']} ({net}): "
                    f"{nb['net']} runs {nb['parallel_mm']:.1f} mm beside the "
                    f"connector-to-clamp stretch ({clamp.ref}), "
                    f"{nb['gap_mm']:.2f} mm away on {'/'.join(nb['layers'])}: "
                    f"an {level_kv:g} kV strike couples ~{nb['v']:.1f} V "
                    f"(M {nb['m_nh']:.2f} nH, {cf['model']}) against its "
                    f"{nb['limit_v']:g} V abs max ({nb['limit_source']}): route it outside the "
                    f"{cf['corridor_mm']:.1f} mm corridor or move the clamp "
                    "to the connector (SLVA680 2.2)", SOURCE,
                    kind="esd_corridor_coupled", victim=nb["net"],
                    coupled_v=nb["v"], m_nh=nb["m_nh"],
                    parallel_mm=nb["parallel_mm"], gap_mm=nb["gap_mm"],
                    limit_v=nb["limit_v"], limit_source=nb["limit_source"],
                    corridor_mm=cf["corridor_mm"]))
        cf["neighbours"] = neigh
        fact["corridor"] = cf
    viols += rate_clamps(bg, lines, cfg, constraints or {}, load_esd_clamps())
    return viols, {"level_kv": level_kv, "di_dt_a_per_s": didt,
                   "connectors": conns, "internal_by_value": skipped,
                   "internal_by_footprint": by_fp, "barriers": barriers,
                   "clamps": sorted(clamps),
                   "lines": lines}


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--parts", help="datasheet extractions and parts.json "
                    "for the victims' abs max (default: the board's parts/)")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons = (checklib.load_json(args.constraints, "constraints")
            if args.constraints else None)
    parts = Path(args.parts) if args.parts else pcb.resolve().parent.parent / "parts"
    viols, facts = check(pcb, cons, parts if parts.is_dir() else None)
    return checklib.report(
        SCRIPT, pcb, viols, checked=[facts],
        nothing_to_judge=None if viols or facts["connectors"] else
        "no external connector (a J, P, CN, X or USB ref, or constraints "
        "esd.connectors) on the board"), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
