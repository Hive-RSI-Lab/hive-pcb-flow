"""check_current.py - trace width vs current per power net (SPEC 6.3).

Per net in constraints.json["power"] with a budgeted current:
 - every track segment's width is compared against the IPC-2152 minimum for
   the full budget at dT (worst case: no per-branch current attribution -
   the whole rail may flow through any segment; override per region below);
 - pour neckdowns: each zone fill is eroded by required_width/2 - if the
   net's via attachment points then fall in separate components, the pour
   necks below the requirement between them (polygon-erosion equivalent of
   the spec's medial-axis min-width);
 - layer transitions: net vias are clustered (<= 2 mm) and each cluster needs
   ceil(I / via_amps) vias (default 0.5 A per via, per spec).

Stitch vias (PCB-0023-A /SW: one pour on four layers, ~120 stitch vias, 112
one-via "transitions" each asked for 24 vias at 12 A). A via is a STITCH
when no same-net track touches it, every same-net pad it touches is in the
net's pour on that layer (the fill covers >= PAD_IN_POUR_FRAC of the pad's
area), the via itself touches fill on every layer where it touches such a
pad, and it touches fill on >= 2 layers. A thermal-relief pad is not in the
pour: it reaches the pour only through spokes, so its current crosses the
via, even when part of the via's ring straddles the relief gap into the
fill (PCB-0023-A D201). A cluster made
only of stitches is not judged as a transition. Instead every fill piece
pair on two layers is judged once, counting every via and plated
through-hole joining those two pieces (the per-path count of a pour-to-pour
hop, same ceil(I / via_amps) threshold, override at the group centroid,
extras stitch=true). A pair is skipped when one piece is a dead end - no pad
or track touches it and every barrel on it joins the other piece - because
then it only parallels the other pour and no load current crosses into it
and back out. A cluster with any via a track or a loose pad feeds keeps the
per-cluster rule, so a track that changes layers is judged as before.

Plane-fed rails, follow-ups (#844, #845, #846):
 - a track lying inside its own same-net fill, pads and vias (>= COVERED_FRAC
   of its area) is a tap in the pour and carries no load current of its own,
   even when it bridges two parts of the graph (#844);
 - a stub that holds a source is judged against that source's output when the
   power entry names `source`: too narrow, or too few vias, is an error
   (`source_stub`); without `source` it stays an advisory warning (#845);
 - whether a connector pin is a source or a load follows the pad's KiCad
   pintype (power_out = source, power_in only = load); passive or untyped
   pins fall back to the reference-prefix rule (#846).

Pad-exit necks (PCB-0019-A SW_L1/SW_L2: 0.30 mm off a TPS63001's 0.24 mm
pads at 0.5 mm pitch, where a 0.5 mm track breaks clearance to the next pin,
so no layout clears the finding). A run of undersized same-net, same-layer
segments touching end to end is accepted, not reported, when all hold:
 1. one of its segments ends on a same-net pad on that layer;
 2. every segment is at least min(required, w_pad) wide, where w_pad is the
    short side of the narrowest pad the run touches - the pad already
    constricts the current to w_pad, so a track no narrower than the copper
    it leaves adds no tighter section (a pad as wide as the requirement
    leaves nothing to excuse, so the full rule applies);
 3. the run's centerline outside the net's other copper on that layer (pads,
    vias, zone fill, the tracks not in the run) totals <= PAD_EXIT_MAX_MM.
    This bounds the neck to the pad's escape: a sub-millimetre neck between
    wide copper at both ends is heat-sunk by it, which IPC-2152's
    long-conductor charts do not describe; a longer one is a routed trace
    and must meet the full width.
Accepted runs are listed in facts["pad_exit_necks"] for the reviewer.

Pour connectivity is judged across layers (PCB-0017-B GND: the F.Cu fill is
split into islands, each stitched to one unbroken B.Cu pour). When a fill on
one layer is eroded, the net's fills on every other layer are eroded by
their own required_width/2, and the pieces are joined through every same-net
via and plated through-hole whose layers both pieces sit on. The attachment
vias of the fill under test must all land in one joined component. Pieces on
the SAME layer are never joined through a via alone (a via touching two
same-layer pieces and nothing else is not a path), so a single-layer pour is
judged exactly as before. Via ampacity at those stitches stays the
transition-via rule's job. A via joins the pieces its copper disk touches;
a plated pad, whose centre no fill reaches (the fill stops at its thermal
relief), joins on each layer the fill that touches its copper, within
THT_JOIN_MM of the pad (PCB-0021-A GND: J4's shell pins never joined).

Leaf branches (PCB-0021-A +SYS: a leg of the pour, bridged over /CHG_A by a
B.Cu strip and two vias, feeds only R6, a 470R LED resistor, and was judged
at the rail's 2.2 A). A finding that still fails after any override is
re-judged at the current its far side can draw, when that is less:
 - a part's draw is bounded only for a two-pad chip resistor (ref R<n>):
   sqrt(P/R) from its Value and the imperial size in its footprint id, P the
   highest common rating for that size (RES_RATING_W). Any other part may
   draw the whole budget, and so may a neck side that reaches no pad: copper
   leading nowhere known is not a leaf. A via-cluster side reaching no pad
   is passive (it only joins cluster vias), but some bounded part must lie
   past the cluster;
 - pour neck: the fill's attachment vias are grouped by the components they
   land in at the failing width. Each group floods the net's raw copper on
   every layer (through vias and plated pads) with the other groups'
   territories (eroded pieces and vias) as walls, and collects the pads it
   reaches. At most one group may reach an unbounded part; every other group
   must meet the rest at one place only (else it may carry a bypass). The
   neck is re-tested at the summed bound of those groups' parts;
 - via cluster: the cluster's vias are cut and the net's raw copper falls
   into components. At most one holds an unbounded part, no cluster via
   lies wholly inside it, and every other side reaches it through exactly
   one cluster via. The cluster needs ceil(bound / via_amps) vias;
 - track (a bridge, hifi-receiver-2 core +3V3: 0.3 mm taps off plane vias
   to single pull-ups, judged at the rail's 0.8 A): the track is taken out
   and the net's raw copper must fall into exactly two parts, one at each
   end, with nothing else touching it. When one part reaches only bounded
   parts, the track is re-judged at their sum.
Re-judged findings that pass are listed in facts["leaf_branches"]; ones that
still fail carry extras leaf_loads. A part on one of the entry's declared
sense_pads is bounded like a sense pin (#1042: a Kelvin line draws microamps,
and the regulator notes ask for a thin trace, TI SNVA721A section 2.5,
LMR33630 SNVSAN3F section 10.1). A track that is no bridge only because a
router's staircase of short steps reads as a loop takes the sides of the two
bridges leading out of it (cluster_sides).

Override reach (LEARNINGS 2026-07-28/29: overrides used to feed only track
widths, making the via rule net-wide and unsatisfiable for branch taps):
 - a via CLUSTER whose centroid falls inside an override region is judged at
   the override current (need = ceil(override / via_amps));
 - a pour NECK is first evaluated at the full budget; if the failing neck's
   reported position falls inside an override region the fill is re-tested at
   the override requirement - passing drops the violation, failing re-emits
   the re-test's neck at the override current. Approximation: the re-test's
   neck position may itself land outside the region (the reported point is a
   representative sample of the split, not the whole neck).

Every undersized_track violation carries extras "bridge": true|false - true
when the segment is a cut edge of the net's connectivity graph (tracks + vias
+ pads + zone fills, lib/netconn.py), i.e. the sole path: the whole judged
current really crosses it. false = a parallel same-net path exists (which may
still be jointly undersized - severity is NOT reduced; the label is for the
fixer, LEARNINGS 2026-07-29 "no bridge awareness").

Two non-bridge cases are dropped instead (PCB-0018-A: signal taps that
leave FET pins sitting in the PHASE/LS_SRC pour that carries the phase
current), and listed by position in facts["pour_taps"]:
 - pour-shunted: both ends sit on the net's pour - inside its fill on the
   track's layer, or on a same-net pad or via whose copper touches a
   same-net fill on any layer. The track is in parallel with that pour, so
   the pour carries the current, and the pour-neck pass judges the pour;
 - inside pad copper: the track lies wholly inside same-net pad and via
   copper on its layer (a stub drawn over a pad adds no section).
A bridge is never dropped: when it is the sole path, the whole judged
current may cross it, and only a declared override can say otherwise.

Plane-fed rails ("plane_fed": true on the entry): the rail's trunk is its
zone fill, so every via is a single-pin leaf tap by construction and the
net-wide budget is unattributable per cluster/segment (LEARNINGS 2026-07-29:
27 one-via clusters on +3V3, doubling infeasible). Semantics:
 - no zone fill anywhere on the net -> error "plane_missing", then the entry
   is checked as if plane_fed were false;
 - pour necks: unchanged, error at the full budget (a trunk neck is real);
 - via clusters / track segments OUTSIDE any override region: still checked
   at the full budget but downgraded to severity "warning" with extras
   advisory=true (a labeled worst-case screen);
 - INSIDE an override region: error at the override current (a declared
   regulator-feed tap stays enforceable);
 - plane-fed stubs (#828): an advisory track or via cluster that takes
   current off the plane to leaf pads is charged what those pads draw, not
   the plane's whole budget (19 false findings on core, each a resistor, IC
   or cap tap). A track counts as a stub when exactly one of its ends reaches
   the fill (a loop back to the fill counts only when both ends tap it at one
   place, within PLANE_TAP_MM); a via cluster when it reaches the fill at one
   place. What lies past the tap is the stub's leaf refs. The screen stays
   advisory when the refs hold a source (a regulator, inductor, or a
   connector when nothing else sources the rail) or at least half the rail's
   loads (that is the trunk). Otherwise: all-cap refs are judged as a
   decoupling branch (#699); unbounded loads go to facts "unjudged"; bounded
   loads are judged at their sum and, when short, become an ERROR naming
   them (leaf_loads, current_a), else a facts "leaf_branches" entry with
   stub=true. A track whose area is at most COVERED_FRAC outside pad, via
   and fill copper carries nothing of its own and goes to "pour_taps".

IPC-2152 basis: 10 degC chart readings at 1 oz outer copper (interpolation
table vendored from kicad-happy, MIT), converted to copper cross-section so
inner layers scale by their (thinner) copper thickness from geom's stackup.
Below the first table row the requirement interpolates linearly to (0, 0),
consistent with published chart readings (~0.20 mm at 0.4 A). dT != 10 scales
the equivalent current by (10/dT)^0.44 (IPC curve-family approximation).

Derived return-net coverage (T6; the pd-trigger 5A GND choke class): no board
ever declares its return net, so return-path ampacity went unchecked - the
worst real defect of any run (5A return choked through 0.2 mm GND necks rated
0.80 A, caught only by the reviewer AFTER verify passed 8/8). When the largest
declared rail is >= RETURN_SYNTH_MIN_A (3.0 A) and the return net (optional
per-entry "return_net", default "GND") exists on the board with a zone fill
and is not itself declared, ONE entry is synthesized for it at the max rail's
budget with plane-fed semantics. ALL derived findings - pour necks included -
are advisory warnings with extras derived=true: the budget is a heuristic
(max of declared rails), so error severity is not justified (measured: the
SHIPPED pd-trigger board necks 0.86 mm vs the 1.75 mm 5 A requirement on a
plane fed from multiple directions). Below the threshold the screen is noise:
the rf4 golden fires a 0.07 mm sliver neck at a 0.15 A budget.

Switch nodes (errata corpus digits-1: an LM2596's OUT net, auto-named
Net-(D1-K), escaped the Power net class and shipped at 0.25 mm for 3 A). A
regulator in reference/switch_nodes.yaml (matched on Value / MPN) whose
switch pin (pad pinfunction SW, LX, OUT ... per row) sits on a net no power
entry names gets one entry at the part's rated current, judged at dT =
INFERRED_DT_C (60 C: what takes a board at 50 C ambient to ~110 C, near
FR-4's Tg). The rating is the most the node carries, not what this board
draws, so this is a gross-error screen: a node left at signal width fails
it (digits v1, 0.25 mm), the designer's own fix passes it (digits v2, 0.75
mm needs 0.71 at 60 C). Its findings are warnings with extras inferred=true
and from_part; a constraints.json entry for the net (the playbook writes one
per switching node at P2) replaces it, at the board's real current and dT. A part
with a pin named SW/LX/PH that no row matches is listed in the report's
"unrated_switch_nodes" (facts only).

Unjudged branches (#699; the power-integrity tools attribute current per sink
from a source/sink definition): the rail's source is the entry's "source", else
check_power_path.power_parts (L, Q, F, J, shunt, an IC with two pads on the
rail) plus any part with an output pin on it (VOUT, SW, LX). A via cluster, a
bridge track or a pour neck failing at the budget is cut. When exactly one side
holds a source, the other sides hold no declared load and at least one
unbounded part, and they hold under half of the rail's loads (parts neither
capacitors nor pure sources nor bounded), they are a branch: what it draws is
not known, so it is not judged at the budget but listed in facts "unjudged"
with its refs, along with any thin track on a branch past such a via. Everything
else keeps the budget: a cut with no source or a
source on two sides (a two-pad IC may be the heavy load), a parallel path, the
side with half or more of the loads (a trunk), a declared load, a far side
holding a diode, a transformer or a switcher's input (known by its switch pin,
or a switch net with an inductor), or on a rail with no plane a bulk capacitor
beside any other part (the part's DC runs through the same copper), an override
region, and a derived or inferred entry with no "source".

Capacitor branches (#798): a far side of capacitors only carries no DC, so it is
judged at the ripple it really carries. Capacitors of 1 uF or less are
decoupling (check_decoupling's), listed in "unjudged" with a "why". A bigger one
takes the rail's converter, read from the board and constraints.json, RMS:
  buck output: dI / sqrt(12), dI = Vout * (1 - D) / (L * fsw), D = Vout / Vin;
    the inductor from the rail to a switch node, Vin the higher declared rail on
    the switcher, fsw from reference/converter_fsw.yaml or the buck block's
    fsw_khz. It alone is counted when the rail also feeds a switcher.
  buck input: Iout * sqrt(D * (1 - D)), D = Vout / Vin, Iout = the power entry
    of the rail past the switcher's inductor; several converters add in RSS,
    which assumes they are not synchronized. Vin is the vin_v of the one buck
    block with that Vout, else the declared rail: a declared voltage is a limit,
    and the higher Vin understates this figure below D = 0.5.
  class-D or half-bridge bus: Iout * sqrt(D * (1 - D)) from the block's
    operating_point (bus_net, iout_a, duty). The bus draws Iout for D of the
    period, as a buck's input does; #798 sets this shape. With a buck output on
    the same rail, the two add in RSS. A bus block with no bus_net leaves every
    rail it could be on unjudged (any rail not declared below its vin_v): the
    rail's other converters alone would understate what its caps carry.
  declared bus figure (#816): a bus block's bus_irms_a, with bus_irms_source
    saying where the design states it, wins over the duty shape, since a
    three-phase bridge's bus is not one duty. It reads as declared, never as
    computed, and "declared_bus" in facts says what the duty shape would have
    given. A figure that isn't above 0, is above the block's iout_a, or cites
    nothing leaves the cap unjudged and says why.
  return side (#816): a cap's current goes in one pad and out the other, so a
    cap on a return net (declared 0 V, or undeclared with a ground name) takes
    the figure of the rail on its other pad. Bulk caps of several rails on one
    branch add in RSS; a decoupling cap's rail adds nothing.
Sources: Freescale/NXP AN4067 rev 1.0 (2010), "Selecting L and C Components in
the Power Stage of the MC34700 Switching Regulators": section 2.2, output RMS
dI / sqrt(12); section 3, input RMS Iout * sqrt(D - D^2). dI is the same
inductor ripple as TI SLVA477C, "Basic Calculation of a Buck Converter's Power
Stage". The whole figure goes on each capacitor's copper, not shared among
parallel ones: an upper bound. Copper that carries it passes, with the figure
and its inputs in facts "ripple_caps"; copper that doesn't fails as an error
with both in the finding, plane-fed or not. A boost, or a figure with any input
missing, stays in "unjudged" and its "why" says what is missing: no default is
assumed. An inrush is a transient, not judged by this steady-state limit.

CLI: --pcb board.kicad_pcb --constraints constraints.json [--out report.json]
     exit 0/1/2 per SPEC section 6.

constraints.json["power"] entries:
    {"net": "+3V3",              # required, exact board net name
     "current_a": 0.4,           # required, budgeted rail current
     "dt_c": 10,                 # optional temperature rise (default 10)
     "via_amps": 0.5,            # optional per-via ampacity (default 0.5)
     "plane_fed": true,          # optional, see plane-fed semantics above
     "loads": {"U402": 0.12},    # optional per-part current (A): a branch
                                 # via/track/neck is judged at the loads past
                                 # it (leaf branches); 0 for a sense tap
     "source": "J1",             # optional ref or list of refs feeding the
                                 # rail; else inferred (unjudged branches)
     "overrides": [              # optional per-region current attribution
        {"near": [x, y], "radius_mm": 2.0, "current_a": 0.1}]}
Segments whose midpoint (via clusters: centroid; pour necks: reported neck
position) falls in an override region are checked against the override
current instead of the full budget (branch traces / taps).
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from functools import partial
from pathlib import Path

import yaml

from shapely import STRtree
from shapely.geometry import Point
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import checklib  # noqa: E402
import engcalc  # noqa: E402
import geom  # noqa: E402
import netconn  # noqa: E402
from checklib import CheckError, violation  # noqa: E402

SCRIPT = "check_current"
# IPC-2152 minimum trace width (mm) at 1 oz outer copper, dT = 10 degC.
# Chart-reading interpolation table vendored from kicad-happy (MIT).
IPC2152_1OZ_10C = [(0.0, 0.0), (0.5, 0.25), (1.0, 0.50), (2.0, 1.10),
                   (3.0, 1.80), (5.0, 3.50), (7.0, 5.50), (10.0, 9.0)]
OZ1_MM = 0.035                 # 1 oz copper thickness the table assumes
VIA_AMPS_DEFAULT = 0.5         # spec: >= 1 via per 0.5 A at transitions
VIA_CLUSTER_MM = 2.0           # vias within this distance share current
WIDTH_TOL_MM = 1e-3
RETURN_SYNTH_MIN_A = 3.0       # derive return-net coverage at/above this rail
PLANE_TAP_MM = 2.0     # plane-fed stubs: fill touches this close are one tap
COVERED_FRAC = 0.01    # a track this much outside fill and pads adds no copper
PLANE_HINT_SINGLE_VIA_FRAC = 0.8  # plane_fed_candidate hint threshold
DECOUPLING_MAX_F = 1e-6        # at or below: decoupling, not bulk (docstring)
PAD_EXIT_MAX_MM = 1.0          # longest accepted pad-exit neck (docstring)
TOUCH_MM = 1e-3                # end-to-end / end-on-pad contact tolerance
THT_JOIN_MM = 1.0              # plated pad joins fill this far out (relief)
PAD_IN_POUR_FRAC = 0.9         # fill covers this much of a pad "in the pour"
SWITCH_NODES_YAML = (Path(__file__).resolve().parent.parent / "reference"
                     / "switch_nodes.yaml")
CONVERTER_FSW_YAML = SWITCH_NODES_YAML.with_name("converter_fsw.yaml")
BUS_TOPOLOGIES = {"class-d-amp", "half-bridge-inverter"}
RETURN_NET = re.compile(r"(?:[ADPSC]?GND\w*|VSS\w*|0V)", re.I)
AN4067 = "NXP AN4067"
INFERRED_DT_C = 60.0           # switch-node screen without constraints
SWITCH_PIN_NAMES = {"SW", "LX", "PH"}  # unrated-part facts (switch_nodes.yaml)
MPN_PROPS = ("MPN", "Manufacturer Part Number", "Manufacturer_Part_Number",
             "Part Number", "PN")


def width_1oz_10c(current_a: float) -> float:
    """Table interpolation; linear extrapolation beyond the last row."""
    pts = IPC2152_1OZ_10C
    if current_a <= 0:
        return 0.0
    for (i0, w0), (i1, w1) in zip(pts, pts[1:]):
        if current_a <= i1:
            return w0 + (current_a - i0) / (i1 - i0) * (w1 - w0)
    (i0, w0), (i1, w1) = pts[-2], pts[-1]
    return w1 + (current_a - i1) / (i1 - i0) * (w1 - w0)


def required_width_mm(current_a: float, dt_c: float, cu_mm: float) -> float:
    """Minimum width on a layer with `cu_mm` copper for current at dT."""
    i_equiv = current_a * (10.0 / dt_c) ** 0.44 if dt_c > 0 else current_a
    area_mm2 = width_1oz_10c(i_equiv) * OZ1_MM
    return area_mm2 / cu_mm


def cluster_vias(vias: list, max_gap: float = VIA_CLUSTER_MM) -> list[list]:
    """Union-find grouping of vias by center distance <= max_gap."""
    parent = list(range(len(vias)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(vias)):
        for j in range(i + 1, len(vias)):
            d = math.hypot(vias[i].at[0] - vias[j].at[0],
                           vias[i].at[1] - vias[j].at[1])
            if d <= max_gap:
                parent[find(i)] = find(j)
    groups: dict[int, list] = {}
    for i, v in enumerate(vias):
        groups.setdefault(find(i), []).append(v)
    return list(groups.values())


def region_current(entry: dict, pos) -> float | None:
    """Override current for a position, or None when no region covers it."""
    for ov in entry.get("overrides", []):
        near = ov.get("near")
        if near and math.hypot(pos[0] - near[0],
                               pos[1] - near[1]) <= ov.get("radius_mm", 2.0):
            return float(ov["current_a"])
    return None


def segment_current(entry: dict, midpoint) -> float:
    ov = region_current(entry, midpoint)
    return float(entry["current_a"]) if ov is None else ov


def pad_exit_necks(bg: geom.BoardGeom, net: str, thin: list) -> dict:
    """Accepted pad-exit necks (module docstring). `thin` lists
    (Track, required_mm) for every undersized segment of `net`; returns
    {id(Track): info} for the segments of each accepted run."""
    accepted: dict[int, dict] = {}
    by_layer: dict[str, list] = {}
    for t, req in thin:
        by_layer.setdefault(t.layer, []).append((t, req))
    for layer, items in by_layer.items():
        # runs = undersized segments touching end to end (union-find)
        parent = list(range(len(items)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if items[i][0].shape.distance(items[j][0].shape) <= TOUCH_MM:
                    parent[find(i)] = find(j)
        runs: dict[int, list] = {}
        for i, it in enumerate(items):
            runs.setdefault(find(i), []).append(it)
        pads = bg.pads_of(net, layer)
        for run in runs.values():
            ends = [Point(c) for t, _ in run
                    for c in (t.shape.coords[0], t.shape.coords[-1])]
            touched = [p for p in pads
                       if any(p.poly.distance(e) <= TOUCH_MM for e in ends)]
            if not touched:
                continue                        # 1. not a pad exit
            w_pad = min(min(p.size) for p in touched)
            if any(t.width + WIDTH_TOL_MM < min(req, w_pad) for t, req in run):
                continue                        # 2. narrower than the pad
            ids = {id(t) for t, _ in run}
            anchor = [p.poly for p in pads] + \
                [v.poly for v in bg.vias_of(net, layer)] + \
                [t.poly for t in bg.tracks_of(net, layer) if id(t) not in ids] + \
                [z.fill_on(layer) for z in bg.zones_of(net, layer)]
            line = unary_union([t.shape for t, _ in run])
            free = line.difference(unary_union(anchor)).length
            if free > PAD_EXIT_MAX_MM + WIDTH_TOL_MM:
                continue                        # 3. longer than an escape
            pad = min(touched, key=lambda p: min(p.size))
            info = {"pad": f"{pad.ref}.{pad.number}", "layer": layer,
                    "pad_width_mm": checklib.rnd(w_pad),
                    "neck_mm": checklib.rnd(min(t.width for t, _ in run)),
                    "length_mm": checklib.rnd(free)}
            for t, _ in run:
                accepted[id(t)] = info
    return accepted


def pour_shunted(bg: geom.BoardGeom, net: str, t) -> bool:
    """True when both ends of track `t` sit on the net's pour: inside its
    fill on t's layer, or on a same-net pad or via whose copper touches a
    same-net fill on any layer (module docstring, pour taps)."""
    fills = {l: bg.zone_fill(net, l) for l in bg.layers_with_zone(net)}
    if not fills:
        return False
    own = fills.get(t.layer)
    items = bg.pads_of(net, t.layer) + bg.vias_of(net, t.layer)

    def anchored(xy) -> bool:
        p = Point(xy)
        if own is not None and own.buffer(TOUCH_MM).contains(p):
            return True
        return any(it.poly.buffer(TOUCH_MM).contains(p)
                   and any(fills[l].intersects(it.poly)
                           for l in it.layers if l in fills)
                   for it in items)
    return anchored(t.shape.coords[0]) and anchored(t.shape.coords[-1])


def inside_pad_copper(bg: geom.BoardGeom, net: str, t) -> bool:
    """True when track `t` lies wholly inside same-net pad and via copper
    on its layer: a stub drawn over a pad adds no section of its own."""
    cover = [p.poly for p in bg.pads_of(net, t.layer)] + \
        [v.poly for v in bg.vias_of(net, t.layer)]
    if not cover:
        return False
    return t.shape.difference(unary_union(cover)).length <= TOUCH_MM


def covered_by_own_copper(bg: geom.BoardGeom, net: str, t, fills=None) -> bool:
    """True when track `t` lies (all but COVERED_FRAC of its area) inside the
    same-net fill, pads and vias of its layer: the copper around it carries
    the current, so the track's own width is not the section that counts,
    whether or not the track graph (which has no zone copper) calls it a
    bridge."""
    if fills is None:
        fills = {l: bg.zone_fill(net, l) for l in bg.layers_with_zone(net)}
    cover = geom._union(
        [p.poly for p in bg.pads_of(net, t.layer)]
        + [v.poly for v in bg.vias_of(net, t.layer)]
        + ([fills[t.layer]] if t.layer in fills else []))
    return t.poly.difference(cover).area <= COVERED_FRAC * t.poly.area


def _stitches(bg: geom.BoardGeom, net: str, fills: dict) -> list[dict]:
    """Per same-net via and plated through-hole, {layer: geometry} of the
    copper it joins on that layer: the places where fills on different
    layers join. A via joins whatever its copper disk touches (a fill can
    end on the via's ring short of its centre). A plated pad's centre sits
    in no fill - the fill stops at its thermal relief -
    so it joins, on each layer whose raw fill (`fills[layer]`) touches its
    copper, that touching fill within THT_JOIN_MM of the pad: the ring
    around its relief. The spokes' own ampacity is not this check's to
    judge, as the via barrel's is the transition-via rule's."""
    out = [{l: v.poly for l in v.layers} for v in bg.vias_of(net)]
    for p in bg.pads_of(net):
        if p.drill is None or len(p.layers) < 2:
            continue
        ring = p.poly.buffer(THT_JOIN_MM)
        joins = {}
        for l in p.layers:
            f = fills.get(l)
            if f is None or f.is_empty:
                continue
            touching = [g for g in getattr(f, "geoms", [f])
                        if g.distance(p.poly) <= TOUCH_MM]
            if touching:
                joins[l] = unary_union(touching).intersection(ring)
        out.append(joins)
    return out


def _pieces(fill, radius: float) -> list:
    eroded = fill.buffer(-radius) if radius > 0 else fill
    return [] if eroded.is_empty else list(getattr(eroded, "geoms", [eroded]))


def fill_pieces(bg: geom.BoardGeom, net: str) -> dict[str, list]:
    """{layer: [polygon, ...]} - the net's merged zone fill on each layer,
    split into its connected pieces (abutting zones merge into one)."""
    out = {}
    for layer in bg.layers_with_zone(net):
        fill = bg.zone_fill(net, layer)
        if not fill.is_empty:
            out[layer] = list(getattr(fill, "geoms", [fill]))
    return out


def via_pieces(bg: geom.BoardGeom, net: str, pieces: dict) -> list:
    """Per via of `net` (bg.vias_of order): (is_stitch, frozenset of the
    (layer, piece index) fill pieces its copper touches). A via is a stitch
    when no same-net track touches it, every same-net pad it touches is in
    the net's pour on that layer (fill covers >= PAD_IN_POUR_FRAC of it, so
    not a thermal-relief pad), the via touches fill on each layer where it
    touches such a pad, and it touches fill on >= 2 layers - it only joins
    pour to pour (module docstring, stitch vias)."""
    fill = {l: unary_union(ps) for l, ps in pieces.items()}

    def in_pour(p, l) -> bool:
        f = fill.get(l)
        return f is not None and p.poly.area > 0 and \
            f.intersection(p.poly).area >= PAD_IN_POUR_FRAC * p.poly.area

    out = []
    for v in bg.vias_of(net):
        touched = frozenset(
            (l, i) for l in v.layers for i, p in enumerate(pieces.get(l, ()))
            if p.intersects(v.poly))
        tracked = any(t.poly.intersects(v.poly)
                      for l in v.layers for t in bg.tracks_of(net, l))
        on_fill = {l for l, _ in touched}
        loose_pad = any(
            p.poly.intersects(v.poly)
            and (l not in on_fill or not in_pour(p, l))
            for l in v.layers for p in bg.pads_of(net, l))
        stitch = not tracked and not loose_pad and len(on_fill) >= 2
        out.append((stitch, touched))
    return out


def pth_pieces(bg: geom.BoardGeom, net: str, pieces: dict) -> list:
    """(pad, frozenset of touched fill pieces) per plated multi-layer pad
    of `net`: its barrel joins pours like a via does."""
    return [(p, frozenset(
        (l, i) for l in p.layers for i, q in enumerate(pieces.get(l, ()))
        if q.intersects(p.poly)))
        for p in bg.pads_of(net) if p.drill is not None and len(p.layers) > 1]


def anchored_pieces(bg: geom.BoardGeom, net: str, pieces: dict) -> set:
    """Fill pieces that a same-net pad or track touches on their layer:
    the only places load current can enter or leave a pour."""
    out = set()
    for l, polys in pieces.items():
        cop = [p.poly for p in bg.pads_of(net, l)] + \
            [t.poly for t in bg.tracks_of(net, l)]
        out |= {(l, i) for i, q in enumerate(polys)
                if any(q.intersects(c) for c in cop)}
    return out


def stitch_bundles(links: list, judged: set, anchored: set) -> list[list]:
    """Groups of barrels (vias, then plated pads; `links` holds (via or
    pad, touched pieces) for each) that join the same two fill pieces on
    different layers - the path a pour-to-pour current takes. Returned as
    lists of those vias and pads, once per distinct group (one through-via set joining 3+
    layers is one group), and only when a member is in `judged` (stitch
    vias left out of the cluster rule). A pair is skipped when one of its
    pieces is a dead end: no pad or track touches it and every barrel on it
    is in the group, so no load current can cross into it and back out."""
    pairs: dict[frozenset, list[int]] = {}
    on_piece: dict[tuple, set[int]] = {}
    for i, (_, touched) in enumerate(links):
        for a in touched:
            on_piece.setdefault(a, set()).add(i)
            for b in touched:
                if a < b and a[0] != b[0]:
                    pairs.setdefault(frozenset((a, b)), []).append(i)
    seen, out = set(), []
    for pair, members in pairs.items():
        key = frozenset(members)
        if key in seen or not key & judged:
            continue
        if any(pc not in anchored and on_piece[pc] <= key for pc in pair):
            continue
        seen.add(key)
        out.append([links[i][0] for i in members])
    return out


def _neck_graph(bg: geom.BoardGeom, net: str, layer: str, fill,
                others: dict | None):
    """(attachment vias, components) for one fill; components(radius)
    returns (nodes, root) where nodes[i] = (layer, reach polygon), the
    fill's own pieces first, and root(i) its joined component."""
    vias = [v for v in bg.vias_of(net, layer)
            if fill.buffer(0.01).contains(Point(v.at))]
    others = {l: o for l, o in (others or {}).items()
              if l != layer and not o[0].is_empty}
    fills = {layer: fill, **{l: o[0] for l, o in others.items()}}
    stitches = [s for s in _stitches(bg, net, fills)
                if layer in s and any(l in s for l in others)]
    # other layers' pieces are fixed at their own requirement: erode once
    other_nodes = []                     # (layer, reach polygon)
    for l, (ofill, oreq) in others.items():
        r = oreq / 2.0
        other_nodes += [(l, part.buffer(r + 0.01)) for part in _pieces(ofill, r)]

    def components(radius: float):
        own = [part.buffer(radius + 0.01) for part in _pieces(fill, radius)]
        nodes = [(layer, g) for g in own] + other_nodes
        parent = list(range(len(nodes)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        if own and stitches and other_nodes:
            tree = STRtree([g for _, g in nodes])
            for joins in stitches:
                hit = [int(i) for l, g in joins.items()
                       for i in tree.query(g, predicate="intersects")
                       if nodes[int(i)][0] == l]
                if len({nodes[i][0] for i in hit}) < 2:
                    continue             # same-layer pieces only: no path
                for i in hit[1:]:
                    parent[find(i)] = find(hit[0])
        return nodes, find, len(own)
    return vias, components


def pour_neck(bg: geom.BoardGeom, net: str, layer: str, fill,
              required: float, others: dict | None = None):
    """None if the pour carries `required` width between all via attachments,
    else (neck_width_mm, pos) of the tightest failing neck.

    `others` maps every other copper layer with a fill of this net to
    (fill, required_mm on that layer); pieces join across layers through
    same-net vias and plated through-holes (module docstring)."""
    vias, components = _neck_graph(bg, net, layer, fill, others)
    pts = [Point(v.at) for v in vias]
    if len(pts) < 2:
        return None

    def connected(radius: float) -> bool:
        nodes, find, n_own = components(radius)
        if not n_own:
            return False
        common = None
        for p in pts:
            comps = {find(i) for i in range(n_own) if nodes[i][1].contains(p)}
            common = comps if common is None else common & comps
            if not common:
                return False
        return True
    if connected(required / 2.0):
        return None
    lo, hi = 0.0, required / 2.0    # lo connected, hi not
    for _ in range(12):
        mid = (lo + hi) / 2.0
        if connected(mid):
            lo = mid
        else:
            hi = mid
    # locate approximately: centroid gap between split components
    eroded = fill.buffer(-hi)
    parts = list(getattr(eroded, "geoms", [eroded])) if not eroded.is_empty else []
    pos = parts[0].representative_point().coords[0] if parts else \
        fill.representative_point().coords[0]
    return 2.0 * lo, pos


# ---- leaf branches (module docstring) -------------------------------------

# Highest common rating per imperial chip-resistor size (W). Generous on
# purpose: the bound sqrt(P/R) grows with P, so it errs high.
RES_RATING_W = {"0201": 0.1, "0402": 0.2, "0603": 0.25, "0805": 0.5,
                "1206": 0.5, "1210": 0.75, "1812": 1.0, "2010": 1.0,
                "2512": 3.0}
_RES_SIZE = re.compile(r"(?<!\d)(" + "|".join(RES_RATING_W) + r")(?!\d)")
_OHM_MULT = {"": 1.0, "R": 1.0, "r": 1.0, "k": 1e3, "K": 1e3, "M": 1e6,
             "m": 1e-3}


def parse_ohms(value) -> float | None:
    """Ohms from a Value field's first token (470R, 4k7, 10k, 2.2K, 1M,
    100, 10kOhm), else None. "m" is milli: reading a megohm as milliohms
    only raises the bound."""
    toks = str(value or "").split()
    if not toks:
        return None
    tok = toks[0].replace("Ω", "R").replace("Ω", "R")
    tok = re.sub(r"(?i)ohms?$", "R", tok)
    m = re.fullmatch(r"(\d+)([RrKkMm])(\d+)", tok)
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _OHM_MULT[m.group(2)]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KkMm]?)[Rr]?", tok)
    if m:
        return float(m.group(1)) * _OHM_MULT[m.group(2)]
    return None


# Declared per-ref load currents of the power entry under test (entry
# "loads": {"U402": 0.12, "U5": 0}), set by check_net; a pad on a sense pin
# draws nothing worth judging (SENSE_PIN, a Kelvin tap carries microamps).
_DECLARED: dict[str, float] = {}
SENSE_PIN = re.compile(r"(SENSE|SNS|KELVIN|^FB$|^VFB$|^CS[+-]?$|^S[+-]$|"
                       r"^ISNS|^VSNS|[+-]SENSE|SENSE[+-])")
SENSE_BOUND_A = 0.001
# The entry's declared sense pads ("sense_pads" keys, "REF.PAD"), set by
# check_net (#1042): a Kelvin line's sense end draws microamps however its
# pin is named (a feedback divider's top resistor, a t:SENSE part).
_SENSE_PADS: set = set()


def _sense_only(bg: geom.BoardGeom, ref: str) -> bool:
    pads = [p for p in bg.pads_of(ref=ref) if p.net == _NET_UNDER_TEST[0]]
    return bool(pads) and all(
        (pin_name(p) and SENSE_PIN.search(pin_name(p)))
        or ((p.ref, str(p.number)) in _SENSE_PADS
            and not SOURCE_PIN.fullmatch(pin_name(p) or ""))
        for p in pads)


_NET_UNDER_TEST: list = [None]
SOURCE_PIN = re.compile(r"V?OUT\d*|VO|SW\d*|LX\d*")  # a regulator's output
SWITCH_NET = re.compile(r"(?:^|[/_])(?:SW|LX)\d*$")    # /pwr/SW, BUCK5_LX


def load_bound(bg: geom.BoardGeom, ref: str) -> float | None:
    """Most current part `ref` can pass (A), or None when unknown. A ref
    declared in the entry's "loads" is bounded at that current; a part
    whose pads on the net are all sense pins, or the entry's declared
    sense_pads (not a regulator's output pin), at SENSE_BOUND_A; a two-pad
    chip resistor at sqrt(P_rated / R) from its Value and the imperial
    size in its footprint id. Every other part may draw the net's whole
    budget."""
    if ref in _DECLARED:
        return _DECLARED[ref]
    if _sense_only(bg, ref):
        return SENSE_BOUND_A
    if not re.fullmatch(r"R\d+", ref) or len(bg.pads_of(ref=ref)) != 2:
        return None
    fp = bg.footprints.get(ref) or {}
    ohms = parse_ohms(fp.get("value"))
    size = _RES_SIZE.search(fp.get("lib") or "")
    if not ohms or ohms <= 0 or size is None:
        return None
    return math.sqrt(RES_RATING_W[size.group(1)] / ohms)


def _loads(bg: geom.BoardGeom, refs) -> float | None:
    """Summed load_bound of `refs`, None if any is unbounded or there are
    none: copper that reaches no pad is not known to be a leaf."""
    if not refs:
        return None
    total = 0.0
    for r in sorted(set(refs)):
        b = load_bound(bg, r)
        if b is None:
            return None
        total += b
    return total


def _copper_graph(bg: geom.BoardGeom, net: str, walls: dict | None = None,
                  skip: frozenset = frozenset(), drop=None):
    """The net's raw copper as parts per layer (minus `walls[layer]`, and
    without the track `drop`), joined through every via and plated pad
    whose id() is not in `skip`. Returns (nodes [(layer, part)], root(i),
    hits(item) -> indices of the nodes its copper touches on its own
    layers)."""
    nodes = []
    for l in bg.copper_layers:
        cop = bg.net_copper(net, l)
        if drop is not None and l == drop.layer:
            cop = geom._union(
                [t.poly for t in bg.tracks_of(net, l) if t is not drop]
                + [p.poly for p in bg.pads_of(net, l)]
                + [v.poly for v in bg.vias_of(net, l)]
                + [z.fill_on(l) for z in bg.zones_of(net, l)])
        if walls and l in walls and not cop.is_empty:
            cop = cop.difference(walls[l])
        if cop.is_empty:
            continue
        nodes += [(l, g) for g in getattr(cop, "geoms", [cop])
                  if g.geom_type == "Polygon" and not g.is_empty]
    parent = list(range(len(nodes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    tree = STRtree([g for _, g in nodes]) if nodes else None

    def hits(item) -> list[int]:
        if tree is None:
            return []
        return [int(i) for i in tree.query(item.poly, predicate="intersects")
                if nodes[int(i)][0] in item.layers]
    links = [v for v in bg.vias_of(net) if id(v) not in skip]
    links += [p for p in bg.pads_of(net) if id(p) not in skip
              and p.drill is not None and len(p.layers) > 1]
    for it in links:
        hole = Point(getattr(it, "at", None) or it.center)
        if walls and any(l in walls and walls[l].contains(hole)
                         for l in it.layers):
            continue                     # its barrel belongs to a wall
        h = hits(it)
        for i in h[1:]:
            parent[find(i)] = find(h[0])
    return nodes, find, hits


def _farads(bg: geom.BoardGeom, ref: str) -> float | None:
    return engcalc.parse_value((bg.footprints.get(ref) or {}).get("value"))


def bulk_caps(bg: geom.BoardGeom, net: str) -> set:
    """Refs of the rail's bulk capacitors."""
    def bulk(ref):
        return _farads(bg, ref) is not None and _farads(bg, ref) > DECOUPLING_MAX_F
    return {p.ref for p in bg.pads_of(net=net)
            if re.fullmatch(r"C\d+", p.ref) and bulk(p.ref)}


def entry_sources(bg: geom.BoardGeom, net: str, entry: dict, plane_fed: bool):
    """(every ref that may source the rail, the ones that are only a source,
    the bulk capacitors that keep the budget), or None when the entry names
    none and none can be inferred (module docstring, unjudged branches). An IC
    in power_parts is both: two pads on the rail may be a regulator's output or
    an amplifier's PVDD."""
    stiff = set() if plane_fed else bulk_caps(bg, net)
    named = entry.get("source")
    if named:
        refs = {str(named)} if isinstance(named, str) else {str(r) for r in named}
        on_net = {p.ref for p in bg.pads_of(net=net)}
        missing = sorted(refs - on_net)
        if missing:
            raise CheckError(f"power entry {net!r}: source {missing} has no "
                             f"pad on the net")
        return refs, refs, stiff
    if entry.get("derived") or entry.get("inferred"):
        return None
    import check_power_path              # it imports this module
    parts = set(check_power_path.power_parts(bg, net))
    pure = {r for r in parts if check_power_path._kind(r)
            not in check_power_path.IC_KINDS}
    pure |= {p.ref for p in bg.pads_of(net=net)
             if SOURCE_PIN.fullmatch(pin_name(p) or "")}
    parts |= pure
    return (parts, pure, stiff) if parts else None


def feeds_or_converts(bg: geom.BoardGeom, ref: str, net: str) -> bool:
    """A part the rail's current may run through whatever power_parts picked:
    a diode or transformer (lumina's bridge D2 feeds V48_RTN), or a switcher's
    input, known by its switch pin or an inductor on a switch net (its U20 on
    V48_RAW)."""
    if re.fullmatch(r"[DT]\d+", ref):
        return True
    import check_power_path              # it imports this module
    if check_power_path._kind(ref) not in check_power_path.IC_KINDS:
        return False
    return any(p.net != net and (
        pin_name(p) in SWITCH_PIN_NAMES
        or SWITCH_NET.search(p.net or "") and any(      # /ENC_SW is a button
            check_power_path._kind(o.ref) == "L" for o in bg.pads_of(net=p.net)))
        for p in bg.pads_of(ref=ref))


def branch_refs(bg: geom.BoardGeom, net: str, sides: list, sources):
    """Sorted refs past a cut whose `sides` (sets of refs) make its far side
    a branch of unknown load, else None (module docstring, unjudged
    branches)."""
    every, pure, stiff = sources
    holding = [s for s in sides if s & every]
    if len(holding) != 1:
        return None
    far = set().union(*(s for s in sides if s is not holding[0]), set())
    if not far or far & set(_DECLARED) or _loads(bg, far) is not None:
        return None
    if all(re.fullmatch(r"C\d+", ref) for ref in far):
        return sorted(far)               # no DC: judged by its ripple
    if far & stiff or any(feeds_or_converts(bg, ref, net) for ref in far):
        return None

    def is_load(ref):
        return ref not in pure and not re.fullmatch(r"C\d+", ref) and (
            ref in _DECLARED or load_bound(bg, ref) is None)
    rail = {p.ref for p in bg.pads_of(net=net)}
    if 2 * sum(map(is_load, far)) >= sum(map(is_load, rail)):
        return None                      # half the rail's loads: a trunk
    return sorted(far)


def _fsw_row(bg: geom.BoardGeom, ref: str, rows: list[dict]) -> dict | None:
    fp = bg.footprints.get(ref) or {}
    texts = [fp.get("value") or ""] + [fp["props"].get(k, "") for k in MPN_PROPS]
    return next((r for r in rows if any(r["rx"].search(t) for t in texts if t)),
                None)


def fsw_rows(path: Path = CONVERTER_FSW_YAML) -> list[dict]:
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["regulators"]
    return [dict(r, rx=re.compile(r["match"], re.I)) for r in rows]


def _is_ic(ref: str) -> bool:
    import check_power_path              # it imports this module
    return check_power_path._kind(ref) in check_power_path.IC_KINDS


def _other_nets(bg: geom.BoardGeom, ref: str, net: str) -> list[str]:
    return [p.net for p in bg.pads_of(ref=ref) if p.net and p.net != net]


def _switchers_on(bg: geom.BoardGeom, node: str) -> list[str]:
    """ICs driving `node`: its switch pin is on it, or it is a switch net."""
    return sorted({p.ref for p in bg.pads_of(net=node) if _is_ic(p.ref) and (
        pin_name(p) in SWITCH_PIN_NAMES or SWITCH_NET.search(node))})


def _buck_output(bg, net: str, ind: str, node: str, volts: dict, blocks: list):
    """The ripple term of inductor `ind` from `net` to switch node `node`, or a
    string saying what can't be read."""
    vout = volts.get(net)
    if vout is None:
        return f"{net} has no declared voltage for {ind}'s ripple"
    if any("boost" in str(b.get("topology", "")) and (
            b.get("operating_point") or {}).get("vin_v") == vout for b in blocks):
        return f"{ind} is a boost's inductor; only a buck's ripple is read"
    for pad in bg.pads_of(net=node):
        if re.fullmatch(r"D\d+", pad.ref) and any(
                volts.get(n, -math.inf) > vout
                for n in _other_nets(bg, pad.ref, node)):
            return f"{ind} is a boost's inductor; only a buck's ripple is read"
    ics = _switchers_on(bg, node)
    if len(ics) != 1:
        return f"{node} has {len(ics)} switcher ICs; can't read {ind}'s input"
    ic = ics[0]
    above = {}
    for p in bg.pads_of(ref=ic):
        if p.net and p.net != node and volts.get(p.net, -math.inf) > vout:
            if SOURCE_PIN.fullmatch(pin_name(p) or ""):
                return f"{ind} is a boost's inductor; only a buck's ripple is read"
            above[p.net] = volts[p.net]
    if len(above) != 1:
        return (f"{ic} has {len(above)} declared rails above {net}; can't read "
                f"its input voltage")
    (vin_net, vin), = above.items()
    henries = engcalc.parse_value((bg.footprints.get(ind) or {}).get("value"))
    if not henries:
        return f"{ind} has no readable inductance"
    row = _fsw_row(bg, ic, fsw_rows())
    if row:
        fsw, where = float(row["fsw_hz"]), row["source"]
    else:
        found = [b["operating_point"]["fsw_khz"] for b in blocks
                 if b.get("topology") == "buck"
                 and (b.get("operating_point") or {}).get("vout_v") == vout
                 and (b.get("operating_point") or {}).get("fsw_khz")]
        if len(found) != 1:
            return (f"{ic}'s switching frequency isn't known: no row in "
                    f"reference/converter_fsw.yaml, no buck block with fsw_khz "
                    f"for {vout:g} V")
        fsw, where = float(found[0]) * 1e3, "buck block fsw_khz"
    if vout <= 0 or vin <= vout:
        return f"{ic}'s duty can't be read from {vin:g} V to {vout:g} V"
    duty = vout / vin
    ripple = vout * (1 - duty) / (henries * fsw)
    return {"from": ic, "amps": ripple / math.sqrt(12),
            "formula": f"dI / sqrt(12), dI = Vout * (1 - Vout / Vin) / (L * fsw) "
                       f"({AN4067} 2.2)",
            "inputs": {"L_h": henries, "fsw_hz": fsw, "vin_v": vin, "vout_v": vout,
                       "inductor": ind, "fsw_source": where}}


def _buck_input(bg, net: str, ic: str, volts: dict, cons: dict):
    """The ripple term of switcher `ic` fed from `net`, or a string saying what
    can't be read."""
    outs = sorted({n for p in bg.pads_of(ref=ic)
                   if p.net and p.net != net and (
                       pin_name(p) in SWITCH_PIN_NAMES or SWITCH_NET.search(p.net))
                   for o in bg.pads_of(net=p.net) if re.fullmatch(r"L\d+", o.ref)
                   for n in _other_nets(bg, o.ref, p.net)})
    if len(outs) != 1:
        return f"{ic} switches into {len(outs)} inductor rails; can't read its output"
    out = outs[0]
    vin, vout = volts.get(net), volts.get(out)
    if vin is None or vout is None:
        missing = net if vin is None else out
        return f"{missing} has no declared voltage for {ic}'s duty"
    declared = [b["operating_point"]["vin_v"] for b in cons.get("blocks") or []
                if b.get("topology") == "buck"
                and (b.get("operating_point") or {}).get("vout_v") == vout
                and b["operating_point"].get("vin_v")]
    if len(declared) == 1:
        vin = float(declared[0])
    if vout <= 0 or vin <= vout:
        return f"{ic} isn't a buck from {vin:g} V to {vout:g} V"
    iout = next((e.get("current_a") for e in cons.get("power") or []
                 if e.get("net") == out), None)
    if iout is None:
        return f"{out} has no power entry for {ic}'s Iout"
    duty = vout / vin
    return {"from": ic, "amps": float(iout) * math.sqrt(duty * (1 - duty)),
            "formula": f"Iout * sqrt(D * (1 - D)), D = Vout / Vin ({AN4067} 3)",
            "inputs": {"iout_a": float(iout), "duty": duty, "vin_v": vin,
                       "vout_v": vout, "out_net": out}}


def _declared_bus_term(topology: str, name: str, op: dict):
    """The term of a block's declared bus ripple (module docstring), or a string
    saying why it can't be used."""
    amps, iout = op["bus_irms_a"], op.get("iout_a")
    source = op.get("bus_irms_source")
    who = f"{topology} block {name}"
    if isinstance(amps, bool) or not isinstance(amps, (int, float)):
        return f"{who} has bus_irms_a {amps!r}, which is not a number"
    if iout is None:
        return f"{who} needs iout_a to bound its bus_irms_a"
    if not isinstance(source, str) or not source.strip():
        return f"{who} declares bus_irms_a with no bus_irms_source to cite"
    if amps < 0:
        return f"{who} has a negative bus_irms_a {amps}"
    if amps == 0:
        return f"{who} has bus_irms_a 0: a figure must be above 0"
    if amps > float(iout):
        return (f"{who} has bus_irms_a {amps} above iout_a {iout}: the bus "
                f"can't ripple more than the phase current")
    term = {"from": name, "amps": float(amps), "declared": True, "source": source,
            "formula": f"declared ({source})",
            "inputs": {"bus_irms_a": float(amps), "iout_a": float(iout)}}
    duty = op.get("duty")
    if duty is not None and 0 <= float(duty) <= 1:
        term["duty_formula_a"] = float(iout) * math.sqrt(
            float(duty) * (1 - float(duty)))
    return term


def _bus_terms(net: str, blocks: list):
    """Ripple terms of the class-D or half-bridge blocks declaring this bus, or
    a string saying what can't be read."""
    terms = []
    for b in blocks:
        op = b.get("operating_point") or {}
        if b.get("topology") not in BUS_TOPOLOGIES or op.get("bus_net") != net:
            continue
        name = b.get("block") or b.get("name") or b["topology"]
        duty, iout = op.get("duty"), op.get("iout_a")
        if op.get("bus_irms_a") is not None:
            term = _declared_bus_term(b["topology"], name, op)
            if isinstance(term, str):
                return term
            terms.append(term)
            continue
        if duty is None or iout is None:
            return (f"{b['topology']} block {name} needs iout_a and duty in its "
                    f"operating_point")
        if not 0 <= float(duty) <= 1:
            return f"{b['topology']} block {name} has duty {duty} outside 0..1"
        terms.append({"from": name,
                      "amps": float(iout) * math.sqrt(duty * (1 - float(duty))),
                      "formula": f"Iout * sqrt(D * (1 - D)) (#798; {AN4067} 3)",
                      "inputs": {"iout_a": float(iout), "duty": float(duty)}})
    return terms


def _is_return(net: str, volts: dict) -> bool:
    """A return net: declared 0 V, or undeclared with a ground name."""
    if net in volts:
        return volts[net] == 0
    return bool(RETURN_NET.fullmatch(net.lstrip("/")))


def _return_rails(bg: geom.BoardGeom, net: str, far: list, volts: dict) -> dict:
    """{rail: [refs]}: the rail on the other pad of each bulk cap of `far`."""
    rails: dict = {}
    for ref in far:
        value = _farads(bg, ref)
        if value is None or value <= DECOUPLING_MAX_F:
            continue
        for rail in set(_other_nets(bg, ref, net)):
            if not _is_return(rail, volts):
                rails.setdefault(rail, []).append(ref)
    return rails


def _return_ripple(bg: geom.BoardGeom, net: str, far: list, cons: dict,
                   volts: dict) -> dict:
    """cap_ripple for caps on a return net: the figures of their rails."""
    rails = _return_rails(bg, net, far, volts)
    if not rails:
        return {"why": f"no converter to read on {net}"}
    terms = []
    for rail in sorted(rails):
        rip = cap_ripple(bg, rail, rails[rail], cons)
        if "why" in rip:
            return {"why": f"return of {rail}: {rip['why']}"}
        terms += [dict(t, rail=rail) for t in rip["terms"]]
    return {"amps": math.sqrt(sum(t["amps"] ** 2 for t in terms)), "terms": terms}


def _may_draw_from(block: dict, net: str, volts: dict) -> bool:
    """A class-D or half-bridge block with no bus_net could be on this rail,
    unless the rail is declared below the block's own input."""
    op = block.get("operating_point") or {}
    if block.get("topology") not in BUS_TOPOLOGIES or op.get("bus_net"):
        return False
    vin, rail = op.get("vin_v"), volts.get(net)
    return vin is None or rail is None or rail >= float(vin)


def _declared_volts(cons: dict | None) -> dict:
    return {row["net"]: float(row["voltage"])
            for row in (cons or {}).get("voltages") or []
            if row.get("net") and row.get("voltage") is not None}


def cap_ripple(bg: geom.BoardGeom, net: str, far: list, cons: dict | None):
    """What the capacitors of branch `far` carry (module docstring, capacitor
    branches): {"amps", "terms"} from the rail's converter, {"why"} when it
    stays unjudged, None when `far` holds anything but capacitors."""
    if not all(re.fullmatch(r"C\d+", ref) for ref in far):
        return None
    values = [_farads(bg, ref) for ref in far]
    if not any(v is not None and v > DECOUPLING_MAX_F for v in values):
        return {"why": "decoupling, 1 uF or less: check_decoupling's" if all(
            v is not None for v in values) else "capacitor value not readable"}
    cons = cons or {}
    blocks = cons.get("blocks") or []
    volts = _declared_volts(cons)
    if _is_return(net, volts):
        return _return_ripple(bg, net, far, cons, volts)
    buses = _bus_terms(net, blocks)
    if isinstance(buses, str):
        return {"why": buses}
    unlinked = [b.get("block") or b.get("name") or b["topology"] for b in blocks
                if _may_draw_from(b, net, volts)]
    if unlinked:
        return {"why": f"blocks {unlinked} declare no bus_net, so they may draw "
                       f"from {net} and its ripple can't be read"}
    outputs = []
    for pad in bg.pads_of(net=net):
        if not re.fullmatch(r"L\d+", pad.ref):
            continue
        for node in _other_nets(bg, pad.ref, net):
            if _switchers_on(bg, node):
                term = _buck_output(bg, net, pad.ref, node, volts, blocks)
                if isinstance(term, str):
                    return {"why": term}
                outputs.append(term)
    terms = buses + outputs
    if not terms:
        for ic in sorted({p.ref for p in bg.pads_of(net=net) if _is_ic(p.ref)
                          and feeds_or_converts(bg, p.ref, net)}):
            term = _buck_input(bg, net, ic, volts, cons)
            if isinstance(term, str):
                return {"why": term}
            terms.append(term)
    if not terms:
        unlinked = [b.get("block") or b.get("name") for b in blocks
                    if b.get("topology") in BUS_TOPOLOGIES
                    and not (b.get("operating_point") or {}).get("bus_net")]
        return {"why": f"no converter to read on {net}" + (
            f"; blocks {unlinked} declare no bus_net" if unlinked else "")}
    return {"amps": math.sqrt(sum(t["amps"] ** 2 for t in terms)), "terms": terms}


def rail_ripple(bg: geom.BoardGeom, net: str, cons: dict | None) -> float:
    """The ripple figure of this rail's bulk caps, 0 when it has none or none
    can be read. It can pass the rail's budget (a buck's input carries more
    RMS than DC below D = 0.5), so copper that meets the budget is held to it
    too once it proves a capacitor branch."""
    caps = sorted(bulk_caps(bg, net))
    groups = [caps[:1]] if caps else []
    volts = _declared_volts(cons)
    if _is_return(net, volts):       # any one rail's cap has that rail's figure
        groups = [refs[:1] for refs in
                  _return_rails(bg, net, caps, volts).values()]
    rips = [cap_ripple(bg, net, refs, cons) for refs in groups]
    return max((r["amps"] for r in rips if r and "amps" in r), default=0.0)


def ripple_text(rip: dict) -> str:
    """The figure and its inputs, for a finding's message."""
    parts = []
    for t in rip["terms"]:
        back = f"return of {t['rail']}: " if t.get("rail") else ""
        if t.get("declared"):
            parts.append(f"{back}{t['from']} bus ripple {t['amps']:g} A RMS "
                         f"declared ({t['source']})")
            continue
        args = ", ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}"
                         for k, v in t["inputs"].items())
        parts.append(f"{back}{t['from']} {t['amps']:.3f} A = {t['formula']} "
                     f"with {args}")
    return f"capacitor branch judged at its ripple {rip['amps']:.2f} A RMS: " + \
        "; ".join(parts)


def _neck_sides(bg: geom.BoardGeom, net: str, layer: str, fill,
                required: float, others: dict | None):
    """[(refs, clean())] per side of a pour split at `required`; clean() says
    whether the side meets the others at one joint only, and is slow on a big
    side, so call it last. None when nothing splits."""
    vias, components = _neck_graph(bg, net, layer, fill, others)
    nodes, find, n_own = components(required / 2.0)
    roots = [{find(i) for i in range(n_own)
              if nodes[i][1].contains(Point(v.at))} for v in vias]
    gp = list(range(len(vias)))

    def gfind(i):
        while gp[i] != i:
            gp[i] = gp[gp[i]]
            i = gp[i]
        return i
    for i in range(len(vias)):
        for j in range(i + 1, len(vias)):
            if roots[i] & roots[j]:
                gp[gfind(i)] = gfind(j)
    groups: dict[int, dict] = {}
    for i, v in enumerate(vias):
        g = groups.setdefault(gfind(i), {"vias": [], "roots": set()})
        g["vias"].append(v)
        g["roots"] |= roots[i]
    if len(groups) < 2:
        return None
    walls = {}                           # group -> {layer: its territory}
    for k, g in groups.items():
        by_layer: dict[str, list] = {}
        for i, (l, poly) in enumerate(nodes):
            if find(i) in g["roots"]:
                by_layer.setdefault(l, []).append(poly)
        for v in g["vias"]:              # a side eroded away keeps its vias
            for l in v.layers:
                by_layer.setdefault(l, []).append(v.poly)
        walls[k] = {l: unary_union(ps) for l, ps in by_layer.items()}
    links = bg.vias_of(net) + [p for p in bg.pads_of(net)
                               if p.drill is not None and len(p.layers) > 1]
    def clean(k, reached, cn, chits):
        # where this side meets the others: same-layer edges and stitches
        touched, contacts = set(), []
        for h in groups:
            if h == k:
                continue
            for i in reached:
                l, part = cn[i]
                w = walls[h].get(l)
                if w is not None and part.buffer(TOUCH_MM).intersects(w):
                    touched.add(h)
                    contacts.append(part.buffer(TOUCH_MM).intersection(w))
            for it in links:
                if set(chits(it)) & reached and any(
                        l in walls[h] and it.poly.intersects(walls[h][l])
                        for l in it.layers):
                    touched.add(h)
                    contacts.append(it.poly)
        joints = unary_union(contacts).buffer(required / 2.0) \
            if contacts else None
        return not (len(touched) > 1 or (
            joints is not None and
            len(getattr(joints, "geoms", [joints])) > 1))
    sides = []
    for k, g in groups.items():
        rest = [walls[h] for h in groups if h != k]
        wall = {l: unary_union([w[l] for w in rest if l in w])
                for l in {l for w in rest for l in w}}
        cn, cfind, chits = _copper_graph(bg, net, wall)
        start = {cfind(i) for v in g["vias"] for i in chits(v)}
        reached = {i for i in range(len(cn)) if cfind(i) in start}
        refs = {p.ref for p in bg.pads_of(net) if set(chits(p)) & reached}
        sides.append((refs, partial(clean, k, reached, cn, chits)))
    return sides


def neck_leaf_bound(bg: geom.BoardGeom, net: str, layer: str, fill,
                    required: float, others: dict | None):
    """(amps, refs) a pour neck failing at `required` can carry at most,
    when every side of the split but one reaches only bounded loads;
    else None (module docstring, leaf branches)."""
    sides = _neck_sides(bg, net, layer, fill, required, others)
    if sides is None:
        return None
    mains, leaf_refs = 0, set()
    for refs, clean in sides:
        if _loads(bg, refs) is None:
            mains += 1
        elif not clean():
            return None                  # may bypass the neck: no bound
        else:
            leaf_refs |= refs
    if mains > 1:
        return None
    return _loads(bg, leaf_refs), sorted(leaf_refs)


def neck_branch(bg: geom.BoardGeom, net: str, layer: str, fill,
                required: float, others: dict | None, sources):
    """branch_refs for a pour neck: None if a far side may bypass the neck."""
    sides = _neck_sides(bg, net, layer, fill, required, others)
    if sides is None:
        return None
    far = branch_refs(bg, net, [refs for refs, _ in sides], sources)
    if far is None or any(not clean() for refs, clean in sides
                          if not refs & sources[0]):
        return None
    return far


def _cut_cluster(bg: geom.BoardGeom, net: str, group: list):
    """Cut a cluster's barrels: (ends, touched sides, side -> refs, nodes,
    root(i))."""
    nodes, find, hits = _copper_graph(bg, net,
                                      skip=frozenset(id(b) for b in group))
    ends = [{find(i) for i in hits(v)} for v in group]
    touched = set().union(*ends)
    refs_of = {c: set() for c in touched}
    for p in bg.pads_of(net):
        for c in {find(i) for i in hits(p)} & touched:
            refs_of[c].add(p.ref)
    return ends, touched, refs_of, nodes, find


def _single_entry(ends: list, main, rest: set) -> bool:
    """Whether the sides in `rest` reach `main` through one barrel each, and
    no barrel sits inside `main`."""
    if any(e <= {main} for e in ends):
        return False
    parent = {c: c for c in rest}

    def cfind(c):
        while parent[c] != c:
            c = parent[c]
        return c
    for e in ends:
        side = sorted(e & rest)
        for c in side[1:]:
            parent[cfind(c)] = cfind(side[0])
    into_main: dict = {}
    for e in ends:
        if main in e:
            for side in {cfind(c) for c in e & rest}:
                into_main[side] = into_main.get(side, 0) + 1
    return not any(n > 1 for n in into_main.values())


def cluster_branch(bg: geom.BoardGeom, net: str, group: list, sources):
    """(branch_refs, [(layer, polygon)] of its copper) for a via cluster or
    stitch bundle: None when a barrel sits inside the source's side or a far
    side has a way around."""
    ends, touched, refs_of, nodes, find = _cut_cluster(bg, net, group)
    near = [c for c in touched if refs_of[c] & sources[0]]
    if len(near) != 1 or not _single_entry(ends, near[0], touched - set(near)):
        return None
    far = branch_refs(bg, net, list(refs_of.values()), sources)
    if far is None:
        return None
    return far, [n for i, n in enumerate(nodes)
                 if find(i) in touched and find(i) != near[0]]


def cluster_leaf_bound(bg: geom.BoardGeom, net: str, group: list):
    """(amps, refs) a via cluster or stitch bundle (vias and plated pads)
    can carry at most, when cutting its barrels leaves one side with
    unbounded loads and every other side reaching it through exactly one
    of them; else None (module docstring, leaf branches)."""
    ends, touched, refs_of, _nodes, _find = _cut_cluster(bg, net, group)
    # a side reaching no pad is passive: it only joins the cluster's vias
    main = [c for c in touched
            if refs_of[c] and _loads(bg, refs_of[c]) is None]
    if len(main) > 1:
        return None
    rest = touched - set(main)
    if main and not _single_entry(ends, main[0], rest):
        return None                      # inside the main side, or a bypass
    refs = set().union(*(refs_of[c] for c in rest))
    if not refs:
        return None                      # no known load past the cluster
    return _loads(bg, refs), sorted(refs)


def track_sides(bg: geom.BoardGeom, net: str, track):
    """[refs, refs] when taking a track out leaves the net's raw copper in two
    parts, one at each end; else None."""
    nodes, find, hits = _copper_graph(bg, net, drop=track)
    on = [i for i, (l, _p) in enumerate(nodes) if l == track.layer]
    coords = track.shape.coords
    ends = []
    for p in (coords[0], coords[-1]):
        cap = Point(p).buffer(track.width / 2.0)
        ends.append({find(i) for i in on if nodes[i][1].intersects(cap)})
    sides = {find(i) for i in on if nodes[i][1].intersects(track.poly)}
    if (len(sides) != 2 or any(len(e) != 1 for e in ends)
            or ends[0] == ends[1]):
        return None                      # a parallel path, or a mid-track tap
    return [{p.ref for p in bg.pads_of(net)
             if any(find(i) == side for i in hits(p))} for side in sides]


def cluster_sides(bg: geom.BoardGeom, net: str, g, bridges: set, eid):
    """track_sides for a track that is no bridge only because a router's
    staircase of steps shorter than the track is wide joins its own pieces
    end cap to end cap (netconn rule b), so one line reads as a loop (#1042).
    The cluster is the part of netconn's graph round edge `eid` joined by
    non-bridge edges. When every node in it is a track end (no pad, via or
    pour) and exactly two bridge tracks lead out of it, every current through
    it runs through either bridge, so the sides of a bridge are its sides.
    Else None: a loop that reaches a pad, via or pour may be a real parallel
    path."""
    todo = [a for a, nbrs in g.adj.items() if any(e == eid for _, _, e in nbrs)]
    seen, exits = set(todo), set()
    while todo:
        u = todo.pop()
        if u[0] != "pt":
            return None
        for v, _w, e in g.adj[u]:
            if e in bridges:
                exits.add(e)
            elif v not in seen:
                seen.add(v)
                todo.append(v)
    if len(exits) != 2:
        return None
    for e in sorted(exits, key=str):
        sides = track_sides(bg, net, g.tracks[e])
        if sides:
            return sides
    return None


def track_leaf_bound(bg: geom.BoardGeom, sides: list | None):
    """(amps, refs) a track with these track_sides can carry at most, when one
    side reaches only bounded loads; else None (module docstring, leaf
    branches)."""
    best = None
    for refs in sides or ():
        amps = _loads(bg, refs)
        if amps is not None and (best is None or amps < best[0]):
            best = (amps, sorted(refs))
    return best


def _stub_graph(bg: geom.BoardGeom, net: str, fills: dict,
                skip: frozenset = frozenset()):
    """The net's copper outside its fills, minus the items whose id() is in
    `skip`: (nodes [(layer, part)], root(i), hits(item), tapped roots - the
    parts that touch a same-layer fill)."""
    nodes = []
    for l in bg.copper_layers:
        cop = geom._union([x.poly for x in (bg.tracks_of(net, l)
                                            + bg.pads_of(net, l)
                                            + bg.vias_of(net, l))
                           if id(x) not in skip])
        if cop.is_empty:
            continue
        nodes += [(l, g) for g in getattr(cop, "geoms", [cop])
                  if g.geom_type == "Polygon" and not g.is_empty]
    parent = list(range(len(nodes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    tree = STRtree([g for _, g in nodes]) if nodes else None

    def hits(item) -> list[int]:
        if tree is None:
            return []
        return [int(i) for i in tree.query(item.poly, predicate="intersects")
                if nodes[int(i)][0] in item.layers]
    links = [x for x in bg.vias_of(net) if id(x) not in skip]
    links += [x for x in bg.pads_of(net) if id(x) not in skip
              and x.drill is not None and len(x.layers) > 1]
    for it in links:
        h = hits(it)
        for i in h[1:]:
            parent[find(i)] = find(h[0])
    tapped = {find(i) for i, (l, part) in enumerate(nodes)
              if l in fills and part.intersects(fills[l])}
    return nodes, find, hits, tapped


def _places(shapes: list) -> int:
    """How many places `shapes` make, joining any PLANE_TAP_MM apart."""
    parent = list(range(len(shapes)))

    def pfind(i):
        while parent[i] != i:
            i = parent[i]
        return i
    for i in range(len(shapes)):
        for j in range(i):
            if shapes[i].distance(shapes[j]) <= PLANE_TAP_MM:
                parent[pfind(i)] = pfind(j)
    return len({pfind(i) for i in range(len(shapes))})


def stub_refs(bg: geom.BoardGeom, net: str, items: list):
    """What a track or via cluster on a plane-fed rail feeds off the plane:
    "covered" when the track's copper lies inside the fill and pads of its
    layer, sorted refs, or None when it does not hang off the plane at one
    place (module docstring, plane-fed stubs)."""
    fills = {l: bg.zone_fill(net, l) for l in bg.layers_with_zone(net)}
    if not fills:
        return None
    track = items[0] if isinstance(items[0], geom.Track) else None
    if track is not None and covered_by_own_copper(bg, net, track, fills):
        return "covered"
    nodes, find, hits, tapped = _stub_graph(
        bg, net, fills, frozenset(id(x) for x in items))

    def roots(shape, layers) -> set:
        return {find(i) for i, (l, part) in enumerate(nodes)
                if l in layers and part.intersects(shape)}

    def taps(rs) -> list:
        return [part.intersection(fills[l]) for i, (l, part) in enumerate(nodes)
                if find(i) in rs and l in fills and part.intersects(fills[l])]
    if track is not None:
        ends = [roots(Point(c).buffer(track.width / 2.0 + 0.01),
                      (track.layer,))
                for c in (track.shape.coords[0], track.shape.coords[-1])]
        touched = roots(track.poly.buffer(0.01), (track.layer,))
        if ends[0] & ends[1]:
            # a loop inside one stub: it carries no more than the stub
            fed = touched
            if len(touched) != 1 or _places(taps(touched)) != 1:
                return None
        else:
            fed = touched - tapped
            if len(touched & tapped) != 1:
                return None              # not fed off the plane at one end
    else:
        own = [it.poly for it in items
               if any(l in fills and it.poly.intersects(fills[l])
                      for l in it.layers)]
        if not own:
            return None                  # the cluster does not reach a fill
        fed = set().union(*(roots(it.poly.buffer(0.01), it.layers)
                            for it in items))
        if _places(own + taps(fed & tapped)) != 1:
            return None                  # a parallel path through the plane
    return sorted({p.ref for p in bg.pads_of(net)
                   if any(find(i) in fed for i in hits(p))})


CONNECTOR_REF = re.compile(r"(?:J|P|CN|USB|TB|BT)\d+")


def _is_connector(bg: geom.BoardGeom, ref: str, net: str) -> bool:
    """Whether `ref` is a connector on `net` for the purpose of "the rail's
    source when nothing else sources it" (#846). The pin type the symbol
    gives its pads on the net decides: power_out is a source, power_in a
    load (an output-only header on a rail fed off-board). A part whose pads
    there are passive or untyped falls back to its reference prefix."""
    types = _pin_types(bg, ref, net)
    if "power_out" in types:
        return True
    if types == {"power_in"}:
        return False
    return bool(CONNECTOR_REF.fullmatch(ref))


def _pin_types(bg: geom.BoardGeom, ref: str, net: str) -> set:
    return {p.pintype for p in bg.pads_of(ref=ref) if p.net == net} - {""}


def _only_power_in(bg: geom.BoardGeom, ref: str, net: str) -> bool:
    """A connector whose pads on the rail are all power_in takes the rail's
    current; it does not source it (#846)."""
    return CONNECTOR_REF.fullmatch(ref) is not None \
        and _pin_types(bg, ref, net) == {"power_in"}


def stub_feeds(bg: geom.BoardGeom, net: str, refs: list, sources) -> bool:
    """Whether a plane-fed stub feeding `refs` may carry the rail's budget:
    it holds the rail's source (a connector only when the rail has no other
    source), or half the rail's loads."""
    _every, pure, _stiff = sources
    own = {r for r in pure if not _is_connector(bg, r, net)}
    conn = {r for r in pure - own if not _only_power_in(bg, r, net)}
    srcs = own or conn
    if set(refs) & srcs:
        return True

    def is_load(ref):
        return ref not in srcs and not re.fullmatch(r"C\d+", ref) \
            and (ref in _DECLARED or load_bound(bg, ref) is None)
    rail = {p.ref for p in bg.pads_of(net=net)}
    n = sum(map(is_load, refs))
    return n > 0 and 2 * n >= sum(map(is_load, rail))


def _stub_error(v, t, items, amps, refs, net, dt_c, cu, need, via_amps):
    """Turn advisory finding `v` into an error at its plane-fed stub's loads."""
    tail = (f"; plane-fed stub: {', '.join(refs)} draw at most {amps:.3f} A")
    if t is not None:
        req = required_width_mm(amps, dt_c, cu[t.layer])
        msg = (f"{net} track {t.width:.3f} mm wide on {t.layer}; IPC-2152 "
               f"needs {req:.3f} mm for {amps:.3f} A at dT={dt_c:.0f}C" + tail)
        v.update(required_mm=checklib.rnd(req))
    else:
        msg = (f"{net} layer transition at ({v['pos'][0]:.2f}, "
               f"{v['pos'][1]:.2f}) has {len(items)} via(s); {amps:.2f} A "
               f"needs {need} (>= 1 via per {via_amps} A)" + tail)
        v.update(required=need)
    v["msg"] = v["items"][0]["msg"] = msg
    v["severity"] = "error"
    v.pop("advisory", None)
    v.update(current_a=amps, leaf_loads=refs,
             refs=sorted(set(v.get("refs") or []) | set(refs)))


def _source_stub_error(v, t, items, refs, net, amps, dt_c, cu, via_amps):
    """A stub that holds the source the power entry names carries that
    source's output, the entry's current_a: the advisory screen becomes an
    error naming it (#845). Without a named source the output is not known
    and the screen stays advisory."""
    need = max(1, math.ceil(amps / via_amps))
    if t is not None:
        if t.width + WIDTH_TOL_MM >= required_width_mm(amps, dt_c, cu[t.layer]):
            return
    elif len(items) >= need:
        return
    _stub_error(v, t, items, amps, refs, net, dt_c, cu, need, via_amps)
    v["msg"] = v["items"][0]["msg"] = v["msg"].replace(
        "plane-fed stub:", "source stub:").replace("draw at most",
                                                   "output")
    v["source_stub"] = True


def check_net(bg: geom.BoardGeom, entry: dict, cons: dict | None = None):
    net = entry.get("net")
    if not net:
        raise CheckError('power entry without "net"')
    if net not in bg.nets:
        raise CheckError(f"power net {net!r} not on board "
                         f"(nets: {sorted(n for n in bg.nets if n)})")
    if "current_a" not in entry:
        raise CheckError(f'power entry {net!r} without "current_a"')
    budget = float(entry["current_a"])
    dt_c = float(entry.get("dt_c", 10.0))
    via_amps = float(entry.get("via_amps", VIA_AMPS_DEFAULT))
    derived = bool(entry.get("derived", False))
    cu = bg.stackup.copper_thickness
    violations: list[dict] = []
    _DECLARED.clear()
    _DECLARED.update({str(r): float(a)
                      for r, a in (entry.get("loads") or {}).items()})
    _NET_UNDER_TEST[0] = net
    _SENSE_PADS.clear()
    _SENSE_PADS.update(tuple(str(k).rsplit(".", 1))
                       for k in (entry.get("sense_pads") or {})
                       if "." in str(k))

    # ---- plane-fed rail: the declared trunk must exist
    plane_fed = bool(entry.get("plane_fed", False))
    if plane_fed and not bg.layers_with_zone(net):
        pos = None
        for layer in bg.copper_layers:
            cop = bg.net_copper(net, layer)
            if not cop.is_empty:
                pos = cop.representative_point().coords[0]
                break
        violations.append(violation(
            SCRIPT, "error", pos, None, net, [],
            f"{net} is declared plane_fed but has no zone fill on any layer",
            SCRIPT, kind="plane_missing"))
        plane_fed = False       # rest of the check runs at full semantics

    leaf_branches: list[dict] = []           # dropped: bounded leaf loads
    unjudged: list[dict] = []                # dropped: unknown branch loads
    sources = entry_sources(bg, net, entry, plane_fed)

    # ---- track segments
    min_seen: dict[str, float] = {}
    undersized: list[tuple[dict, object]] = []   # (violation, Track)
    pour_taps: list = []                         # dropped: pour-shunted taps
    thin = []                                    # (Track, mid, ov, amps, req)
    for t in bg.tracks_of(net):
        mid = t.shape.interpolate(0.5, normalized=True).coords[0]
        ov = region_current(entry, mid)
        amps = budget if ov is None else ov
        req = required_width_mm(amps, dt_c, cu[t.layer])
        min_seen[t.layer] = min(min_seen.get(t.layer, 9e9), t.width)
        if t.width + WIDTH_TOL_MM < req:
            thin.append((t, mid, ov, amps, req))
    exits = pad_exit_necks(bg, net, [(x[0], x[4]) for x in thin]) \
        if thin else {}
    for t, mid, ov, amps, req in thin:
        if id(t) in exits:
            continue
        advisory = plane_fed and ov is None
        msg = (f"{net} track {t.width:.3f} mm wide on {t.layer}; IPC-2152 "
               f"needs {req:.3f} mm for {amps:.2f} A at dT={dt_c:.0f}C")
        extras = {}
        if advisory:
            msg += ("; advisory: plane-fed rail, full-budget worst-case "
                    "screen (per-segment current unattributed)")
            extras["advisory"] = True
        x0, y0 = t.shape.coords[0]
        x1, y1 = t.shape.coords[-1]
        v = violation(
            SCRIPT, "warning" if advisory else "error", mid, t.layer,
            net, [], msg, SCRIPT, kind="undersized_track",
            width_mm=checklib.rnd(t.width), required_mm=checklib.rnd(req),
            current_a=amps, segment={"start": [checklib.rnd(x0),
                                               checklib.rnd(y0)],
                                     "end": [checklib.rnd(x1),
                                             checklib.rnd(y1)]},
            **extras)
        violations.append(v)
        undersized.append((v, t))

    ripple_caps: list[dict] = []

    def judge_branch_track(v, t, far):
        """A thin track on branch `far`: past a capacitor branch, its ripple
        (module docstring); else what it carries is unknown."""
        nonlocal violations
        rip = cap_ripple(bg, net, far, cons)
        if rip and "amps" in rip:
            req = required_width_mm(rip["amps"], dt_c, cu[t.layer])
            if t.width + WIDTH_TOL_MM < req:
                msg = (f"{net} track {t.width:.3f} mm wide on {t.layer}; "
                       f"IPC-2152 needs {req:.3f} mm for {rip['amps']:.2f} A at "
                       f"dT={dt_c:.0f}C; {ripple_text(rip)}")
                v["msg"] = v["items"][0]["msg"] = msg
                v["severity"] = "error"
                v.pop("advisory", None)
                v.update(required_mm=checklib.rnd(req), current_a=rip["amps"],
                         refs=far, ripple=rip)
                return
            ripple_caps.append({"kind": "undersized_track", "layer": t.layer,
                                "pos": v["pos"], "refs": far,
                                "amps": checklib.rnd(rip["amps"]),
                                "terms": rip["terms"]})
        else:
            item = {"kind": "undersized_track", "layer": t.layer,
                    "pos": v["pos"], "refs": far}
            if rip:
                item["why"] = rip["why"]
            unjudged.append(item)
        violations = [x for x in violations if x is not v]
        undersized.remove((v, t))

    # ---- a capacitor branch's track that meets the budget but not its ripple
    rail = rail_ripple(bg, net, cons) if sources else 0.0
    if rail > budget:
        wide = [(t, required_width_mm(rail, dt_c, cu[t.layer]))
                for t in bg.tracks_of(net)
                if region_current(entry, t.shape.interpolate(
                    0.5, normalized=True).coords[0]) is None
                and t.width + WIDTH_TOL_MM >= required_width_mm(
                    budget, dt_c, cu[t.layer])]
        wide = [x for x in wide if x[0].width + WIDTH_TOL_MM < x[1]]
        exits = pad_exit_necks(bg, net, wide) if wide else {}
        for t, req in wide:
            sides = None if id(t) in exits else track_sides(bg, net, t)
            far = branch_refs(bg, net, sides, sources) if sides else None
            rip = cap_ripple(bg, net, far, cons) if far else None
            if not rip or "amps" not in rip:
                continue
            req = required_width_mm(rip["amps"], dt_c, cu[t.layer])
            if t.width + WIDTH_TOL_MM < req:
                x0, y0 = t.shape.coords[0]
                x1, y1 = t.shape.coords[-1]
                violations.append(violation(
                    SCRIPT, "error",
                    t.shape.interpolate(0.5, normalized=True).coords[0],
                    t.layer, net, far,
                    f"{net} track {t.width:.3f} mm wide on {t.layer}; IPC-2152 "
                    f"needs {req:.3f} mm for {rip['amps']:.2f} A at "
                    f"dT={dt_c:.0f}C; {ripple_text(rip)}", SCRIPT,
                    kind="undersized_track", width_mm=checklib.rnd(t.width),
                    required_mm=checklib.rnd(req), current_a=rip["amps"],
                    ripple=rip, bridge=True,
                    segment={"start": [checklib.rnd(x0), checklib.rnd(y0)],
                             "end": [checklib.rnd(x1), checklib.rnd(y1)]}))

    # ---- bridge labeling (LEARNINGS 2026-07-29: cut edge = sole path)
    if undersized:
        g = netconn.build(bg, net, include_zones=True)
        bridges = netconn.bridge_tracks(g)
        # g.tracks maps edge_id -> the SAME Track objects bg.tracks_of yields
        # (geom filters one cached list; netconn stores them unchanged), so
        # identity lookup is sound. Zero-length tracks are absent from the
        # graph: label those bridge=true (sole-path is the conservative call).
        edge_of = {id(trk): eid for eid, trk in g.tracks.items()}
        for v, t in undersized:
            eid = edge_of.get(id(t))
            v["bridge"] = True if eid is None else eid in bridges
        # taps inside their own pour or copper carry no load current of their own
        shunted = [(v, t) for v, t in undersized
                   if covered_by_own_copper(bg, net, t)
                   or not v["bridge"] and (pour_shunted(bg, net, t)
                                           or inside_pad_copper(bg, net, t))]
        if shunted:
            drop = {id(v) for v, _ in shunted}
            violations = [v for v in violations if id(v) not in drop]
            undersized = [(v, t) for v, t in undersized if id(v) not in drop]
            pour_taps = [v["pos"] for v, _ in shunted]
        # a bridge whose far side feeds only bounded loads: their sum
        for v, t in list(undersized):
            if v["bridge"]:
                sides = track_sides(bg, net, t)
            else:
                eid = edge_of.get(id(t))
                sides = None if eid is None else cluster_sides(
                    bg, net, g, bridges, eid)
            leaf = track_leaf_bound(bg, sides)
            if leaf is None or leaf[0] >= v["current_a"]:
                mid = t.shape.interpolate(0.5, normalized=True).coords[0]
                far = branch_refs(bg, net, sides, sources) if (
                    sides and sources
                    and region_current(entry, mid) is None) else None
                if far:
                    judge_branch_track(v, t, far)
                continue
            amps, refs = leaf
            req = required_width_mm(amps, dt_c, cu[t.layer])
            if t.width + WIDTH_TOL_MM >= req:
                violations = [x for x in violations if x is not v]
                undersized.remove((v, t))
                leaf_branches.append({
                    "kind": "undersized_track", "layer": t.layer,
                    "pos": v["pos"], "current_a": checklib.rnd(amps),
                    "loads": refs})
                continue
            msg = (f"{net} track {t.width:.3f} mm wide on {t.layer}; "
                   f"IPC-2152 needs {req:.3f} mm for {amps:.3f} A at "
                   f"dT={dt_c:.0f}C; leaf branch: {', '.join(refs)} draw "
                   f"at most {amps:.3f} A")
            v["msg"] = v["items"][0]["msg"] = msg
            v.update(required_mm=checklib.rnd(req), current_a=amps,
                     leaf_loads=refs)

    # ---- pour neckdowns (always at the full budget; plane_fed keeps error)
    zone_layers = bg.layers_with_zone(net)

    def others_at(amps, layer):
        # the net's fills on every other layer, at their own requirement
        return {l: (bg.zone_fill(net, l), required_width_mm(amps, dt_c, cu[l]))
                for l in zone_layers if l != layer}

    for z in bg.zones_of(net):
        for layer in z.fills:
            fill = z.fill_on(layer)
            if fill.is_empty:
                continue
            amps = budget
            ov = None
            rip = None
            req = required_width_mm(budget, dt_c, cu[layer])
            neck = pour_neck(bg, net, layer, fill, req,
                             others_at(budget, layer))
            if neck is not None:
                ov = region_current(entry, neck[1])
                if ov is not None:
                    # failing neck sits in an override region: re-test the
                    # fill at the override requirement; passing drops it
                    amps = ov
                    req = required_width_mm(ov, dt_c, cu[layer])
                    neck = pour_neck(bg, net, layer, fill, req,
                                     others_at(ov, layer))
            leaf = neck_leaf_bound(bg, net, layer, fill, req,
                                   others_at(amps, layer)) \
                if neck is not None else None
            if leaf is not None and leaf[0] < amps:
                # far side feeds only bounded loads: judge it at their sum
                at = neck[1]
                amps, refs = leaf
                req = required_width_mm(amps, dt_c, cu[layer])
                neck = pour_neck(bg, net, layer, fill, req,
                                 others_at(amps, layer)) if amps > 0 else None
                if neck is None:
                    leaf_branches.append({
                        "kind": "pour_neckdown", "layer": layer,
                        "pos": [checklib.rnd(at[0]), checklib.rnd(at[1])],
                        "current_a": checklib.rnd(amps), "loads": refs})
            else:
                leaf = None
                far = neck_branch(bg, net, layer, fill, req,
                                  others_at(amps, layer), sources) if (
                    neck is not None and ov is None and sources) else None
                rip = cap_ripple(bg, net, far, cons) if far else None
                pos_at = [checklib.rnd(neck[1][0]),
                          checklib.rnd(neck[1][1])] if far else None
                if rip and "amps" in rip:
                    amps = rip["amps"]
                    req = required_width_mm(amps, dt_c, cu[layer])
                    neck = pour_neck(bg, net, layer, fill, req,
                                     others_at(amps, layer))
                    if neck is None:
                        ripple_caps.append({
                            "kind": "pour_neckdown", "layer": layer,
                            "pos": pos_at, "refs": far,
                            "amps": checklib.rnd(amps), "terms": rip["terms"]})
                elif far:
                    item = {"kind": "pour_neckdown", "layer": layer,
                            "pos": pos_at, "refs": far}
                    if rip:
                        item["why"] = rip["why"]
                    unjudged.append(item)
                    neck = None
            if neck is not None:
                width, pos = neck
                msg = (f"{net} pour on {layer} necks to ~{width:.2f} mm "
                       f"between via attachments; IPC-2152 needs {req:.3f} mm "
                       f"for {amps:.2f} A at dT={dt_c:.0f}C")
                extras = {}
                if leaf is not None:
                    msg += (f"; leaf branch: {', '.join(leaf[1])} draw at "
                            f"most {amps:.3f} A")
                    extras["leaf_loads"] = leaf[1]
                elif rip and "amps" in rip:
                    msg += f"; {ripple_text(rip)}"
                    extras["ripple"] = rip
                if derived:
                    # derived return entry: the budget itself is a heuristic
                    # (max declared rail), so the neck is a labeled screen,
                    # not an error - see module docstring (T6).
                    msg += ("; advisory: derived return-net coverage "
                            "(no declared entry; budget = max declared rail)")
                    extras["advisory"] = True
                violations.append(violation(
                    SCRIPT, "warning" if derived else "error", pos, layer,
                    net, [], msg, SCRIPT,
                    kind="pour_neckdown", neck_mm=checklib.rnd(width),
                    required_mm=checklib.rnd(req), current_a=amps, **extras))

    # ---- transition via count (per-cluster override via centroid);
    # all-stitch clusters are judged per pour pair instead (docstring)
    vias = bg.vias_of(net)
    pieces = fill_pieces(bg, net) if vias else {}
    links = via_pieces(bg, net, pieces)
    stitch_ids = {id(v) for v, (s, _) in zip(vias, links) if s}
    clusters = cluster_vias(vias)
    judged = []                          # (vias / plated pads, stitch bundle?)
    pooled: set[int] = set()             # via indices left to the pair rule
    index = {id(v): i for i, v in enumerate(vias)}
    for group in clusters:
        if all(id(v) in stitch_ids for v in group):
            pooled |= {index[id(v)] for v in group}
        else:
            judged.append((group, False))
    if pooled:
        barrels = [(v, t) for v, (_, t) in zip(vias, links)] + \
            pth_pieces(bg, net, pieces)
        judged += [(g, True) for g in stitch_bundles(
            barrels, pooled, anchored_pieces(bg, net, pieces))]
    via_groups: dict[int, list] = {}     # advisory finding -> its vias
    for group, bundle in judged:
        at = [getattr(b, "at", None) or b.center for b in group]
        cx = sum(x for x, _ in at) / len(at)
        cy = sum(y for _, y in at) / len(at)
        ov = region_current(entry, (cx, cy))
        amps = budget if ov is None else ov
        need = max(1, math.ceil(amps / via_amps))
        rip = far = None
        over = ov is None and len(group) * via_amps < rail
        leaf = cluster_leaf_bound(bg, net, group) \
            if len(group) < need else None
        if leaf is not None and leaf[0] < amps:
            # every side but one feeds only bounded loads: their sum
            amps = leaf[0]
            need = max(1, math.ceil(amps / via_amps))
            if len(group) >= need:
                leaf_branches.append({
                    "kind": "insufficient_transition_vias",
                    "pos": [checklib.rnd(cx), checklib.rnd(cy)],
                    "current_a": checklib.rnd(amps), "loads": leaf[1]})
        else:
            leaf = None
            branch = cluster_branch(bg, net, group, sources) if (
                (len(group) < need or over) and ov is None and sources) else None
            if branch:
                far, copper = branch
                rip = cap_ripple(bg, net, far, cons)
                if len(group) >= need and not (rip and "amps" in rip):
                    continue                 # meets the budget, no figure
                # a thin track on that branch carries no more than it does
                for v, t in list(undersized):
                    mid = t.shape.interpolate(0.5, normalized=True).coords[0]
                    if region_current(entry, mid) is None and any(
                            l == t.layer and part.intersects(Point(mid))
                            for l, part in copper):
                        judge_branch_track(v, t, far)
                if rip and "amps" in rip:
                    amps = rip["amps"]
                    need = max(1, math.ceil(amps / via_amps))
                    if len(group) >= need:
                        ripple_caps.append({
                            "kind": "insufficient_transition_vias",
                            "pos": [checklib.rnd(cx), checklib.rnd(cy)],
                            "refs": far, "amps": checklib.rnd(amps),
                            "vias": len(group), "terms": rip["terms"]})
                else:
                    item = {"kind": "insufficient_transition_vias",
                            "pos": [checklib.rnd(cx), checklib.rnd(cy)],
                            "refs": far}
                    if rip:
                        item["why"] = rip["why"]
                    unjudged.append(item)
                    continue
        judged_ripple = rip is not None and "amps" in rip
        if len(group) < need:
            advisory = plane_fed and ov is None and not judged_ripple
            if bundle:
                msg = (f"{net} pour-to-pour transition near ({cx:.2f}, "
                       f"{cy:.2f}) has {len(group)} via(s) or plated "
                       f"hole(s) joining the same two pours; {amps:.2f} A "
                       f"needs {need} "
                       f"(>= 1 via per {via_amps} A)")
            else:
                msg = (f"{net} layer transition at ({cx:.2f}, {cy:.2f}) has "
                       f"{len(group)} via(s); {amps:.2f} A needs {need} "
                       f"(>= 1 via per {via_amps} A)")
            extras = {"stitch": True} if bundle else {}
            if leaf is not None:
                msg += (f"; leaf branch: {', '.join(leaf[1])} draw at most "
                        f"{amps:.3f} A")
                extras["leaf_loads"] = leaf[1]
            if judged_ripple:
                msg += f"; {ripple_text(rip)}"
                extras["ripple"] = rip
            if advisory:
                msg += ("; advisory: plane-fed rail, per-cluster current "
                        "unattributed (each via is a leaf tap off the plane)")
                extras["advisory"] = True
            violations.append(violation(
                SCRIPT, "warning" if advisory else "error", (cx, cy), None,
                net, far if judged_ripple else [], msg, SCRIPT,
                kind="insufficient_transition_vias",
                vias=len(group), required=need, **extras))
            if advisory:
                via_groups[id(violations[-1])] = group

    # ---- plane-fed stubs: an advisory finding is judged at its stub's loads
    if plane_fed and not derived and sources:
        tracks = {id(v): t for v, t in undersized}
        for v in [x for x in violations if x.get("advisory")]:
            items = [tracks[id(v)]] if id(v) in tracks else via_groups.get(id(v))
            refs = stub_refs(bg, net, items) if items else None
            if refs is None or refs != "covered" and stub_feeds(
                    bg, net, refs, sources):
                if refs is not None and entry.get("source") \
                        and set(refs) & set(sources[1]):
                    _source_stub_error(v, tracks.get(id(v)), items,
                                       sorted(set(refs) & set(sources[1])),
                                       net, budget, dt_c, cu, via_amps)
                continue                 # a trunk: the full-budget screen
            t = tracks.get(id(v))
            if refs == "covered":
                pour_taps.append(v["pos"])
            elif refs and all(re.fullmatch(r"C\d+", r) for r in refs) \
                    and t is not None:
                v.pop("advisory", None)
                judge_branch_track(v, t, refs)
                continue
            elif refs and _loads(bg, [r for r in refs
                                      if not re.fullmatch(r"C\d+", r)]) is None:
                if t is not None:
                    judge_branch_track(v, t, refs)
                    continue
                unjudged.append({"kind": v["kind"], "pos": v["pos"],
                                 "refs": refs})
            else:
                dc = [r for r in refs if not re.fullmatch(r"C\d+", r)]
                amps = _loads(bg, dc) or 0.0
                need = max(1, math.ceil(amps / via_amps))
                short = t.width + WIDTH_TOL_MM < required_width_mm(
                    amps, dt_c, cu[t.layer]) if t is not None \
                    else len(items) < need
                if short:
                    _stub_error(v, t, items, amps, dc, net, dt_c, cu, need,
                                via_amps)
                    continue
                leaf_branches.append({"kind": v["kind"], "pos": v["pos"],
                                      "current_a": checklib.rnd(amps),
                                      "loads": dc, "stub": True})
            violations = [x for x in violations if x is not v]
            if t is not None:
                undersized.remove((v, t))

    facts = {"net": net, "current_a": budget, "dt_c": dt_c,
             "required_mm_by_layer": {
                 l: checklib.rnd(required_width_mm(budget, dt_c, cu[l]))
                 for l in bg.copper_layers},
             "min_track_mm_by_layer": {l: checklib.rnd(w)
                                       for l, w in min_seen.items()},
             "via_clusters": len(clusters)}
    if stitch_ids:
        facts["stitch_vias"] = len(stitch_ids)
        facts["stitch_bundles"] = sum(1 for _, b in judged if b)
    if undersized or pour_taps:
        facts["bridge_labeled"] = True
    if pour_taps:
        facts["pour_taps"] = pour_taps
    if leaf_branches:
        facts["leaf_branches"] = leaf_branches
    if unjudged:
        facts["unjudged"] = unjudged
    if ripple_caps:
        facts["ripple_caps"] = ripple_caps
    declared = _bus_terms(net, (cons or {}).get("blocks") or [])
    if not isinstance(declared, str) and any(t.get("declared") for t in declared):
        facts["declared_bus"] = [t for t in declared if t.get("declared")]
    if exits:
        seen = []
        for info in exits.values():
            if info not in seen:
                seen.append(info)
        facts["pad_exit_necks"] = seen
    if entry.get("plane_fed"):
        facts["plane_fed"] = True
        facts["advisory_violations"] = sum(
            1 for v in violations if v.get("advisory"))
    if derived:
        facts["derived"] = True
        for v in violations:
            v["derived"] = True
    elif (not entry.get("plane_fed") and clusters
            and bg.layers_with_zone(net)):
        # sidecar-adoption lint (T6, facts only): a rail with a zone fill
        # whose via clusters are almost all single-via is the plane-fed
        # shape - the entry probably wants "plane_fed": true (the carrier
        # +3V3 gap: T2 built the key, no sidecar ever adopted it).
        single = sum(1 for g in clusters if len(g) == 1)
        if single >= PLANE_HINT_SINGLE_VIA_FRAC * len(clusters):
            facts["plane_fed_candidate"] = True
    return violations, facts


def derived_return_entry(entries: list[dict], bg: geom.BoardGeom) -> dict | None:
    """Synthesize the return-net entry (module docstring, T6), or None.

    Only when: a declared rail reaches RETURN_SYNTH_MIN_A, the return net
    (per-entry "return_net", default "GND") is on the board with a zone fill,
    and it is not already a declared power entry."""
    real = [e for e in entries if e.get("net") and "current_a" in e]
    if not real:
        return None
    mx = max(real, key=lambda e: float(e["current_a"]))
    if float(mx["current_a"]) < RETURN_SYNTH_MIN_A:
        return None
    ret = mx.get("return_net") or next(
        (e["return_net"] for e in real if e.get("return_net")), "GND")
    if ret in {e["net"] for e in real}:
        return None                      # already declared - owner's numbers win
    if ret not in bg.nets or not bg.layers_with_zone(ret):
        return None                      # routed-only return: not judgeable here
    return {"net": ret, "current_a": float(mx["current_a"]),
            "dt_c": float(mx.get("dt_c", 10.0)),
            "via_amps": float(mx.get("via_amps", VIA_AMPS_DEFAULT)),
            "plane_fed": True, "derived": True}


def switch_rows(path: Path = SWITCH_NODES_YAML) -> list[dict]:
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["regulators"]
    return [dict(r, rx=re.compile(r["match"], re.I),
                 pins={p.upper() for p in r["pins"]}) for r in rows]


def pin_name(pad: geom.Pad) -> str:
    """'OUT_2' on pad 2 -> 'OUT'; '~{SW}' -> 'SW'."""
    name = pad.pinfunction.strip()
    if name.endswith(f"_{pad.number}"):
        name = name[: -len(pad.number) - 1]
    return re.sub(r"~\{([^}]*)\}", r"\1", name).upper()


def switch_row(fp: dict, rows: list[dict]) -> dict | None:
    """The switch_nodes.yaml row a footprint matches on Value / MPN."""
    texts = [fp.get("value") or ""] + [fp["props"].get(k, "")
                                       for k in MPN_PROPS]
    return next((r for r in rows
                 if any(t and r["rx"].search(t.strip()) for t in texts)), None)


def is_switch_pin(pad: geom.Pad, bg: geom.BoardGeom, rows: list[dict]) -> bool:
    """A rated part's switch pin (its row's pins), or any SW/LX/PH pin."""
    name = pin_name(pad)
    row = switch_row(bg.footprints.get(pad.ref) or {"props": {}}, rows)
    return name in SWITCH_PIN_NAMES or bool(row and name in row["pins"])


def inferred_switch_entries(entries: list[dict], bg: geom.BoardGeom,
                            rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(entries, unrated): one entry per switch net no power entry names, at
    the matched regulator's rated current (module docstring)."""
    named = {e.get("net") for e in entries}
    found: dict[str, dict] = {}
    unrated: list[dict] = []
    for ref, fp in sorted(bg.footprints.items()):
        row = switch_row(fp, rows)
        for pad in bg.pads_of(ref=ref):
            name = pin_name(pad)
            net = pad.net
            if not net or net.startswith("unconnected-") or net in named:
                continue
            if row is None:
                if name in SWITCH_PIN_NAMES:
                    unrated.append({"ref": ref, "value": fp.get("value"),
                                    "pin": name, "net": net})
                continue
            if name not in row["pins"]:
                continue
            cur = found.get(net)
            if cur is None or row["current_a"] > cur["current_a"]:
                found[net] = {"net": net, "current_a": float(row["current_a"]),
                              "dt_c": INFERRED_DT_C, "inferred": True, "from_part": ref,
                              "part": fp.get("value")}
    uniq = {(u["ref"], u["net"]): u for u in unrated}
    return list(found.values()), list(uniq.values())


def run(argv=None):
    ap = argparse.ArgumentParser(
        description="Trace width / pour neck / via count vs current (IPC-2152).")
    ap.add_argument("--pcb", required=True, help="path to .kicad_pcb")
    ap.add_argument("--constraints",
                    help="constraints.json with a power list; without it only "
                         "the switch nodes inferred from the parts are judged")
    ap.add_argument("--out", help="write JSON report here instead of stdout")
    args = ap.parse_args(argv)

    cons = (checklib.load_json(args.constraints, "constraints")
            if args.constraints else {})
    entries = cons.get("power", [])
    bg = geom.load_board(Path(args.pcb))
    bg.assert_fresh()

    violations: list[dict] = []
    checked: list[dict] = []
    for entry in entries:
        vs, facts = check_net(bg, entry, cons)
        violations.extend(vs)
        checked.append(facts)
    inferred, unrated = inferred_switch_entries(entries, bg, switch_rows())
    for entry in inferred:
        vs, facts = check_net(bg, entry, cons)
        for v in vs:
            v["severity"] = "warning"
            v["msg"] = v["items"][0]["msg"] = (
                f"{v['msg']}; inferred: {entry['net']} is "
                f"{entry['from_part']} ({entry['part']})'s switch node, at its "
                f"rated {entry['current_a']:g} A, screened at dT="
                f"{INFERRED_DT_C:.0f}C - name the net in "
                f"constraints.json power with this board's current")
            v.update(inferred=True, from_part=entry["from_part"])
            if entry["from_part"] not in v["refs"]:
                v["refs"] = sorted(v["refs"] + [entry["from_part"]])
        violations.extend(vs)
        checked.append(dict(facts, inferred=True,
                            from_part=entry["from_part"]))
    ret_entry = derived_return_entry(entries, bg)
    if ret_entry is not None:
        vs, facts = check_net(bg, ret_entry, cons)
        violations.extend(vs)
        checked.append(facts)

    extra = {"unrated_switch_nodes": unrated} if unrated else {}
    payload = checklib.report(
        SCRIPT, args.pcb, violations, checked=checked,
        stackup_assumed=bg.stackup.assumed,
        nothing_to_judge=None if checked else
        "no power entries in constraints and no switcher the table knows",
        not_judged=[f"{u['ref']} ({u.get('value')}) {u['pin']} on {u['net']}: "
                    "a switch node whose part isn't in switch_nodes.yaml and "
                    "with no power entry" for u in unrated], **extra)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
