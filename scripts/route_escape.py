#!/usr/bin/env python
"""route_escape - BGA escape (dog-bone fan-out) before the other routers (#325).

Freerouting does poorly at fine-pitch BGA escapes, so P7 fans each ball grid
out first, with KRT's bga_fanout (tools/krt via env.find_krt), and leaves
every escaped ball as a short stub past the field for route_critical /
route_auto to pick up. Pre-routed copper survives them by construction (the
DSN exporter emits it as protected wires, see route_auto.py).

Per BGA (lib/bgalib.find_bgas: geometric grid detection, not the name):
  1. escape geometry from the board's FAB floors (bgalib.escape_params):
     via = max(min via, drill + 2 annular) - a guard for a fab row whose
     floor via breaks its own via ring (JLC 6L did, #443: fixed to 0.1);
     clearance = max(min clearance, hole clearance - ring). The netclass
     clearance is usually wider than a 0.8 mm field allows, so a
     courtyard-scoped DRU rule (bgalib.dru_rule; rules_gen.py writes the
     same rule) carries the escape clearance;
  2. KRT bga_fanout, dog-bone when the gap/street budget fits, else the
     channel engine (sparse fields escape between NC balls);
  3. via-in-pad is a paid fab option (filled and capped vias): KRT's
     dog-bone falls back to it for a ball with no free gap site, without
     checking the fallback against the gap vias already placed (0.57 mm
     apart, hole_to_hole + clearance errors on linux-computer). The fab
     decides whether they stay (via_in_pad_choice, #325): JLCPCB fills
     and caps vias free on 6+ layers and its BGA guide wants filled
     via-in-pad at 0.5 mm pitch, so there they are kept, checked
     (vip_conflicts) and marked filled; on a stack where filling costs
     (4 layers) those balls' escapes are removed. --allow-via-in-pad /
     --no-via-in-pad override;
  4. optional cap nudge (KRT place_fanout_clearance) for decouplers whose
     pads a via landed on (the back-side decouplers place_seed puts under
     the field). Moves are reported per ref;
  5. kicad-cli DRC against the board's real rules; the nets whose new
     copper is in a new error are ripped (route_critical's grading), up to
     ESCAPE_RIP_ROUNDS times. A starved_thermal names only the pad and the
     zone, so the nets with new copper next to the starved pad are ripped
     (rc.nets_starving_thermals, #979). The original board is replaced only
     when no new DRC error type remains, or when the only one left is a
     starved thermal no new copper is next to (a nudged cap, the refill):
     the escape is kept and each such pad is an escape_starved_thermal
     error.

Diff pairs (#529): every pair of constraints.json (route_critical.diff_specs:
explicit diff_pairs plus pairs discovered on high_speed names) with both
balls in one field goes to KRT as --diff-pairs, so KRT escapes it COUPLED
where it fits (dog-bone vias, converge to the pair pitch, parallel past the
boundary) and single-ended where it does not. --diff-pair-gap is the pair's
own edge gap on the first inner escape layer from rules_gen's per-layer
rules (zrules), the smallest when pairs differ. KRT escapes a coupled pair
toward the NEARER field edge, not toward its partner part. --pairs-first
runs a pairs-only pass before the rest so pair balls take the dog-bone gap
sites first (linux-computer U200: 3/3 pairs coupled instead of 1/3, but 15
single balls left instead of 1), so it is opt-in.

DRC rip per field (#529): a net whose new copper is in a new DRC error loses
its escape only at the field(s) within FIELD_REACH_MM of the error, so a pair
escaped at two BGAs that grazes a resistor beside one keeps the other.
left_for_routers is per field (a net escaped at U200 but not at U100 is left
at U100).

Via-in-pad at 0.8 mm (#552): every KRT run goes through lib/krt_launch.py
--hole-to-hole <board floor>, since KRT checks drills against its own 0.20
constant and placed dog-bone gap vias 0.566 mm from a via-in-pad (0.366
hole to hole against JLC 6L's 0.5). KRT still picks gap sites before it
falls back to via-in-pad, so with --allow-via-in-pad each field's NEW
in-pad vias are checked (vip_conflicts) against every via (hole to hole,
copper) and every foreign track (a floor-size 0.45 via bulges past a 0.32
pad onto stubs KRT threaded past the pad); a clashing in-pad ball loses its
escape and its neighbour keeps a clean one (DRC alone rips both). Where the
stack fills vias for free (6+ layers) and the fab's smallest filled via fits
inside the pad (lib/bgalib pad_via), each kept in-pad via wider than that is
swapped for it at the same centre (shrink_pad_vias) before the clash check,
and the courtyard gets its floors (aiee_bga_fine_via_<ref>, which rules_gen
keeps while those vias are on the board): an 8x8 0.8 mm field on 6 layers
keeps all 64 balls where the 0.45 vias lost three. The fact's pad_via.fab_cost
names the price (JLC's 0.15 hole is a paid via tier; dfm_check flags it). A ball
net with a .kicad_dru track-width rule wider than the escape track
(VBUS_J1's 0.32 neck) is not given to KRT (held_wide); left for the
routers. A net whose balls sit in its courtyard neck zone (rules_gen's
aiee_pwr_neck, lib/necklib) is judged at its neck width there (#937), and
--pads lays a stub that stays in the zone at the neck width. The 150+ track_dangling warnings after an escape are the kept
stubs themselves (open until routed), not rip leftovers.

--toward-partner (#547, opt-in): the constraints' diff-pair and
length_groups nets escape out of the field sides facing the nearest pad of
the same net on another part (partner_sides; krt_launch --escape-open
re-compiles KRT's dog-bone engine with a per-net goal). It costs gap sites:
a closed side sends more balls to via-in-pad. On linux-computer it is worse
than the default (U100's DDR balls are in its bottom-right corner, U200 is
up-left: forcing 48 nets out left/up loses most of them); it pays where a
field's balls already face their partner.

HDI builds (#477): on a board whose stack is an HDI build of stackups.yaml,
every field escapes through lib/hdilib.py instead of KRT: a microvia in the
pad, stacked and buried down to the next free layer (--hdi auto; --hdi off
keeps KRT's through-via dog-bones; a standard build never gets them). The
routers' rules come with it (hdilib via_type_rules, field_rules), and a power
ball whose net has an inner plane gets a stack to that plane.

Power and ground balls are not fanned (KRT treats them as plane balls: they
want one via straight to their plane - stitch_vias' job).

Power first (#923 point 1): before any field is escaped, the SMD pads of
the constraints' inner-plane power nets on the other parts (a PMIC's supply
pins, a regulator's output) get their plane vias (stitch_vias --nets power
--pads-only, ball-grid parts skipped). Escaped and routed signal tracks
otherwise take the room beside those pins first, and the stitch step that
runs after route_critical finds no spot: on a 0.5 mm SoC board close then
stalled at 125 opens until the signals around the PMIC and the USB-C power
pins were ripped by hand. The vias are graded by DRC on their own and
dropped as a set if they add an error type. --no-power-first skips it.

Pad fanout (#929): --pads U5.3,U5.7,L1.1 fans out named pads of any part
(QFN and SOT power pins, inductor and regulator pads) instead of ball grids:
a via beside each pad that clears every other net's copper and hole, and a
stub to it on the pad's layer, or a short join to the net's own copper when
no spot clears (lib/padfanout.py). The ops go through route_edit
(atomic, verified); --plan only reports them, --out-ops writes them.
--fan-width and --fan-via SIZE/DRILL override the net's width and the
board's common via. KRT is not used. With --via-in-pad (#940) a pad with
neither gets a via at its centre if its copper holds the whole via; the
fab must fill and cap vias at the board's layer count (jlc_pricing.yaml
options.via_fill_epoxy) or the run stops, and each such via is set filled
and capped, so fab_export's VIA-FILL.md carries it to the order. --out-ops
writes those fill ops under "then", to apply after "ops".

Plane balls (#956): --plane-balls U301 --plane-nets +0V75,+1V2 --via-in-pad
puts one filled, capped via at the centre of each ball of U301 on those
nets, and leaves every other ball to the fan-out. A 0.5 mm field has no
room for dog-bones, so route_escape leaves its plane balls and stitch_vias
skips ball grids; this is the step for them once the fab option is taken
(the same --via-in-pad gate). The via is --fan-via or the board's via
floor; only its drill must sit inside the ball (lib/padfanout.py). A ball
that already holds a via of its net is skipped; one with no room is a
pad_fanout_incomplete warning. It may run with --pads in one pass.

Facts: per BGA the geometry, budget, method, requested / escaped / kept
nets, the balls left for the routers, via-in-pad stripped, caps moved; the
DRU rules written; the DRC delta.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

from shapely.geometry import LineString, Point, box  # noqa: E402

import bgalib  # noqa: E402
import checklib  # noqa: E402
import geom  # noqa: E402
import hdilib  # noqa: E402
import kc  # noqa: E402
import necklib  # noqa: E402  (#937 neck zones)
import route_critical as rc  # noqa: E402  (KRT staging, floors, DRC delta, rip)
import routelib  # noqa: E402
import stitch_vias  # noqa: E402
import zrules  # noqa: E402
from checklib import CheckError, violation  # noqa: E402

SCRIPT = "route_escape"
ESCAPE_RIP_ROUNDS = 3
# DRC checks whose items name every net with unrouted copper, not the copper
# at fault: one stray island grows the count, and naming its items ripped
# the whole field (#325 fault test: 34 of 36 nets for one bad track).
NOT_A_FAULT = frozenset({"unconnected_items"})
PLANE_COVER = 0.5          # an inner zone over >= this share of the field is a plane
DEFAULT_GRID = 0.05        # mm - KRT stub-end snap; 0.1 shifts 0.8 mm-pitch stubs


# ------------------------------------------------------------ pure helpers

def escape_floors(pro_path: Path | None) -> dict:
    """The FAB floors escape geometry is built from (.kicad_pro
    board.design_settings.rules, written by fabfloors), not the netclass
    clearance grading_floors reports. KiCad stock values when absent."""
    fl = {"track_width": 0.2, "clearance": 0.2, "via_diameter": 0.5,
          "via_drill": 0.3, "annular": 0.0,
          "hole_clearance": bgalib.KICAD_HOLE_CLEARANCE_MM,
          "edge_clearance": 0.5, "hole_to_hole": 0.25}
    if pro_path is None or not Path(pro_path).is_file():
        return fl
    try:
        proj = json.loads(Path(pro_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fl
    rules = proj.get("board", {}).get("design_settings", {}).get("rules", {})
    for key, rk in (("track_width", "min_track_width"),
                    ("clearance", "min_clearance"),
                    ("via_diameter", "min_via_diameter"),
                    ("via_drill", "min_through_hole_diameter"),
                    ("annular", "min_via_annular_width"),
                    ("hole_clearance", "min_hole_clearance"),
                    ("hole_to_hole", "min_hole_to_hole")):
        if isinstance(rules.get(rk), (int, float)) and rules[rk] > 0:
            fl[key] = float(rules[rk])
    edge = rules.get("min_copper_edge_clearance") or rules.get("min_copper_to_edge")
    if edge:
        fl["edge_clearance"] = float(edge)
    return fl


def escape_layers(bg, bga: bgalib.Bga, explicit: list[str] | None = None
                  ) -> tuple[list[str], list[str]]:
    """(layers to escape on, inner plane layers left out). An inner layer
    is a plane under this field when one zone covers >= PLANE_COVER of the
    ball bbox; outer pours are fills, not planes, and stay usable."""
    if explicit:
        return [ly for ly in explicit if ly in bg.copper_layers], []
    field = box(*bga.bbox)
    planes = []
    for ly in bg.copper_layers[1:-1]:
        for z in bg.zones_of(layer=ly):
            # an unfilled inner zone at P7 is a planes_gen plane: count it
            if not z.filled or (field.area and z.fill_on(ly).intersection(
                    field).area >= PLANE_COVER * field.area):
                planes.append(ly)
                break
    return [ly for ly in bg.copper_layers if ly not in planes], planes


def in_pad_vias(bg, bga: bgalib.Bga, new_uuids: set[str]) -> list:
    """NEW vias whose centre lies inside one of the BGA's own pads on that
    net (KRT's via-in-pad fallback)."""
    pads = [p for p in bg.pads_of(ref=bga.ref) if p.net]
    out = []
    for v in bg.vias_of():
        if not v.uuid or v.uuid not in new_uuids:
            continue
        pt = Point(v.at)
        if any(p.net == v.net and p.poly.buffer(1e-4).contains(pt)
               for p in pads):
            out.append(v)
    return out


def via_in_pad_nets(bg, bga: bgalib.Bga, new_uuids: set[str]) -> dict[str, int]:
    """net -> count of NEW in-pad vias (in_pad_vias)."""
    out: dict[str, int] = {}
    for v in in_pad_vias(bg, bga, new_uuids):
        out[v.net] = out.get(v.net, 0) + 1
    return out


def via_in_pad_choice(n_copper: int, allow: bool, forbid: bool,
                      free: set[int]) -> tuple[bool, str]:
    """Keep KRT's in-pad vias in a ball field? The fab decides (#325).
    JLCPCB fills and caps vias (POFV) free on 6+ layer boards and charges
    for it below (jlc_pricing.yaml options.via_fill_epoxy, `free` = the
    layer counts priced 0; jlcpcb.com/help/article/pcb-via-covering), and
    its BGA guide calls a 0.5 mm pitch dog-bone not manufacturable: 0.25 mm
    pads with filled via-in-pad (jlcpcb.com/help/article/BGA-Design-
    Guidelines---PCB-Layout-Recommendations-for-BGA-packages). So: kept
    when the option is free on this stack, stripped when it would cost,
    and --allow-via-in-pad / --no-via-in-pad override either way."""
    if allow and forbid:
        raise CheckError("--allow-via-in-pad and --no-via-in-pad conflict")
    if allow:
        return True, "--allow-via-in-pad"
    if forbid:
        return False, "--no-via-in-pad"
    if n_copper in free:
        return True, (f"filled, capped vias are free on {n_copper} layers "
                      "(jlc_pricing via_fill_epoxy)")
    return False, (f"filled, capped vias cost extra on {n_copper} layers "
                   "(jlc_pricing via_fill_epoxy); --allow-via-in-pad to pay")


FIELD_REACH_MM = 3.0      # an escape's copper lies within this of its ball bbox


def field_region(bga: bgalib.Bga):
    return box(*bga.bbox).buffer(FIELD_REACH_MM)


SIDE_NORMALS = {"left": (-1.0, 0.0), "right": (1.0, 0.0),
                "up": (0.0, -1.0), "down": (0.0, 1.0)}
OPEN_COS = 0.38            # a side opens when the partner lies within ~67 deg of it


def open_sides(dx: float, dy: float) -> tuple[str, ...]:
    """Field sides (board axes, up = -y) facing a partner at (dx, dy) from
    the ball: one side within ~22 deg of an axis, else the two around it."""
    d = (dx * dx + dy * dy) ** 0.5
    if d < 1e-9:
        return ()
    return tuple(s for s, (nx, ny) in SIDE_NORMALS.items()
                 if (nx * dx + ny * dy) / d >= OPEN_COS)


def partner_sides(bg, bga: bgalib.Bga, nets) -> dict[str, tuple[str, ...]]:
    """net -> the field sides facing that net's nearest pad on another part
    (#547). Nets with no pad off this BGA are left out."""
    own: dict[str, list] = {}
    for pd in bg.pads_of(ref=bga.ref):
        if pd.net in nets:
            own.setdefault(pd.net, []).append(pd.center)
    out: dict[str, tuple[str, ...]] = {}
    for net in sorted(own):
        pts = own[net]
        cx = sum(x for x, _ in pts) / len(pts)
        cy = sum(y for _, y in pts) / len(pts)
        far = [pd.center for pd in bg.pads_of(net=net) if pd.ref != bga.ref]
        if not far:
            continue
        tx, ty = min(far, key=lambda q: (q[0] - cx) ** 2 + (q[1] - cy) ** 2)
        sides = open_sides(tx - cx, ty - cy)
        if sides:
            out[net] = sides
    return out


def toward_fact(sides: dict[str, tuple[str, ...]]) -> dict:
    """{"left+up": n nets, ...} for the report."""
    out: dict[str, int] = {}
    for s in sides.values():
        k = "+".join(s)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def toward_nets(cons: dict, board_nets, specs: list[dict]) -> list[str]:
    """The nets --toward-partner steers: every diff pair's P/N and every
    length_groups member/ref net (check_length_groups' matching)."""
    import check_length_groups as clg

    out = {x for s in specs for x in (s.get("p"), s.get("n")) if x}
    for g in cons.get("length_groups") or []:
        if not isinstance(g, dict):
            continue
        for key in ("nets", "ref_nets"):
            found, _ = clg.match_nets(g.get(key) or [], board_nets)
            out.update(found)
    return sorted(out)


def fanout_nets(bga: bgalib.Bga, wide: dict[str, float], track: float,
                plane_min: int = 6, neck: dict[str, float] | None = None
                ) -> tuple[list[str], list[str]]:
    """(nets KRT may fan out, nets held back because a .kicad_dru width rule
    is wider than the escape track). KRT fans every net it is given at one
    width (VBUS_J1 at U100: 0.089 mm under a 0.32 mm neck rule, #552), so a
    wide net is left for the routers. A net whose balls all sit in its neck
    zone (field_necks, rules_gen's aiee_pwr_neck, #937) is judged at the neck
    width: a neck no wider than the track lets KRT fan it out. KRT's own
    plane-ball rule (>= plane_min balls on the part) is kept, since a --nets
    list switches it off."""
    neck = neck or {}
    count: dict[str, int] = {}
    for n in bga.nets.values():
        if n:
            count[n] = count.get(n, 0) + 1
    held = sorted(n for n in count
                  if neck.get(n, wide.get(n, 0.0)) > track + 1e-6
                  and count[n] < plane_min)
    ok = sorted(n for n in count if count[n] < plane_min and n not in held)
    return ok, held


def field_necks(bg, bga: bgalib.Bga, necks: dict) -> dict[str, float]:
    """{net: neck width} for the field's nets whose every ball sits in a neck
    zone of its net (necklib.board_necks, #937): the width an escape stub
    from those balls may run at, inside the part's courtyard."""
    out: dict[str, float] = {}
    gone: set[str] = set()
    for pd in bg.pads_of(ref=bga.ref):
        if not pd.net or pd.net not in necks or pd.net in gone:
            continue
        ws = [necklib.neck_width(necks, pd.net, ly, pd.poly.centroid)
              for ly in pd.layers if ly in bg.copper_layers]
        ws = [w for w in ws if w is not None]
        if not ws:
            gone.add(pd.net)
            out.pop(pd.net, None)
            continue
        out[pd.net] = max(out.get(pd.net, 0.0), min(ws))
    return out


def pad_via_fact(bga: bgalib.Bga, pv: dict) -> dict:
    """The plan/result fact for a field's #552 pad via."""
    return {"via_mm": [pv["via_diameter"], pv["via_drill"]],
            "rule": bgalib.fine_rule_name(bga.ref),
            "hole_to_hole": pv["hole_to_hole"],
            "fab_cost": (f"filled {pv['via_diameter']:g}/{pv['via_drill']:g}"
                         " mm via in the pad: filling is free on this stack,"
                         " the small hole is JLC's paid via option "
                         "(dfm_check dfm_via_cost_tier)")}


def shrink_pad_vias(staged: Path, bg, bga: bgalib.Bga, new_uuids: set[str],
                    pv: dict) -> int:
    """Swap each NEW through via-in-pad of the field (in_pad_vias) wider
    than the pad via `pv` for one of pv's size at the same centre (#552).
    A ball's own tracks join it at that centre, so they stay connected.
    Returns how many were swapped."""
    first, last = bg.copper_layers[0], bg.copper_layers[-1]
    ops = []
    for v in in_pad_vias(bg, bga, new_uuids):
        if v.diameter <= pv["via_diameter"] + 1e-6 or \
                not (v.spans(first) and v.spans(last)):
            continue
        ops += [{"op": "remove", "uuid": v.uuid},
                {"op": "add_via", "at": list(v.at),
                 "size": pv["via_diameter"], "drill": pv["via_drill"],
                 "net": v.net}]
    if ops:
        rc.route_edit.apply_ops(staged, ops)
    return len(ops) // 2


def vip_conflicts(bg, bga: bgalib.Bga, new_uuids: set[str],
                  hole_to_hole: float, clearance: float) -> set[str]:
    """Nets whose NEW via-in-pad (a via centred in its own ball's pad)
    breaks hole-to-hole or copper clearance against any other via, or
    copper clearance against a foreign track (#552).
    KRT picks dog-bone gap sites before it falls back to via-in-pad and never
    re-checks the fallback against the gap vias around it: at 0.8 mm pitch a
    diagonal gap via sits 0.566 mm from the pad centre, inside any 0.5 mm
    hole-to-hole floor. Dropping the in-pad ball keeps its neighbour's
    clean gap escape (DRC alone rips both nets)."""
    pads = [pd for pd in bg.pads_of(ref=bga.ref) if pd.net]
    vias = list(bg.vias_of())
    region = field_region(bga)
    near = [v for v in vias if region.contains(Point(v.at))]
    inpad = []
    for v in near:
        if not v.uuid or v.uuid not in new_uuids:
            continue
        pt = Point(v.at)
        if any(pd.net == v.net and pd.poly.buffer(1e-4).contains(pt)
               for pd in pads):
            inpad.append(v)
    tracks = [x for x in bg.tracks_of() if x.net and region.intersects(x.poly)]
    out: set[str] = set()
    for v in inpad:
        for o in near:
            if o is v:
                continue
            d = ((o.at[0] - v.at[0]) ** 2 + (o.at[1] - v.at[1]) ** 2) ** 0.5
            if d < (o.drill + v.drill) / 2 + hole_to_hole - 1e-6 or (
                    o.net != v.net
                    and d < (o.diameter + v.diameter) / 2 + clearance - 1e-6):
                out.add(v.net)
                break
        else:
            # a floor-size via bulging past a small pad grazes the stubs KRT
            # threaded past the PAD (#552: 0.45 vias in 0.32 pads)
            reach = v.poly.buffer(clearance - 1e-6)
            if any(x.net != v.net and v.spans(x.layer)
                   and reach.intersects(x.poly) for x in tracks):
                out.add(v.net)
    return out


def error_fields(before: dict, after: dict, nets: set[str],
                 bgas: list) -> dict[str, set[str] | None]:
    """net -> refs of the ball fields whose escape of that net is in a new
    DRC error (the error position within FIELD_REACH_MM of the field), or
    None when an error lies near no field (rip the net everywhere). A pair
    escaped at two BGAs that grazes a resistor beside one keeps its escape
    at the other (#529: SDQS1 at U100 ripped its clean U200 escape)."""
    grown = set(rc.drc_delta(before, after)["new"])
    regions = {b.ref: field_region(b) for b in bgas}
    out: dict[str, set[str] | None] = {}
    for v in after["violations"]:
        if v.get("severity") != "error" or v.get("check") not in grown:
            continue
        for item in v.get("items", []):
            m = rc._ITEM_NET_RE.match(item.get("msg", ""))
            if not m or m.group(1) not in nets:
                continue
            net, pos = m.group(1), item.get("pos")
            refs = ({r for r, g in regions.items() if g.contains(Point(pos))}
                    if pos else set())
            if not refs or out.get(net, set()) is None:
                out[net] = None
            else:
                out.setdefault(net, set()).update(refs)
    for net in nets:
        out.setdefault(net, None)
    return out


def starved_pads(report: dict) -> dict[str, tuple]:
    """'Pad 2 [GND] of C330 on B.Cu' -> its position, for every pad of a
    starved_thermal ERROR in a DRC report (the error's other item is the
    zone)."""
    out: dict[str, tuple] = {}
    for v in report.get("violations", []):
        if v.get("check") != "starved_thermal" or v.get("severity") != "error":
            continue
        for item in v.get("items", []):
            msg = item.get("msg", "")
            if msg.startswith("Pad ") and item.get("pos"):
                out[msg] = tuple(item["pos"])
    return out


def starved_fields(before: dict, after: dict, nets: set[str],
                   bgas: list) -> dict[str, set[str] | None]:
    """net -> refs of the ball fields holding a newly starved pad, for nets
    rc.nets_starving_thermals named (the error names only the pad and the
    zone, so the field is the pad's, not the track's), or None when no
    field holds one (rip the net everywhere)."""
    old = starved_pads(before)
    spots = [Point(q) for m, q in starved_pads(after).items() if m not in old]
    regions = {b.ref: field_region(b) for b in bgas}
    refs = {r for r, g in regions.items() if any(g.contains(q) for q in spots)}
    return {n: set(refs) if refs else None for n in nets}


def faults_only(report: dict) -> dict:
    """The DRC report without the NOT_A_FAULT checks, for naming the nets
    to rip (the count still grades the escape)."""
    return {**report, "violations": [
        v for v in report.get("violations", [])
        if v.get("check") not in NOT_A_FAULT]}


def grade_and_rip(staged: Path, old_uuids: set[str], before: dict,
                  bgas: list, drc) -> tuple[dict, dict, list[dict]]:
    """DRC the staged escape (`drc()`: refill and grade) and rip, up to
    ESCAPE_RIP_ROUNDS times, the nets whose new copper is in a new error:
    named by the error's items, or for a starved_thermal (which names only
    the pad and the zone) by rc.nets_starving_thermals, the new copper next
    to the starved pad (#979: haos-voice U301's under-field decouplers threw
    the whole escape away). Returns (after, delta, rounds)."""
    rounds: list[dict] = []
    for _ in range(ESCAPE_RIP_ROUNDS):
        after = drc()
        delta = rc.drc_delta(before, after)
        if not delta["new"]:
            break
        mine = nets_with_new_copper(geom.BoardGeom.from_file(staged), old_uuids)
        fb, fa = faults_only(before), faults_only(after)
        bad = rc.nets_in_new_errors(fb, fa, mine)
        where = error_fields(fb, fa, bad, bgas) if bad else {}
        starving = rc.nets_starving_thermals(before, after, mine, staged,
                                             old_uuids) - bad
        if starving:
            where.update(starved_fields(before, after, starving, bgas))
        if not where:
            break
        rounds.append({"nets": sorted(where), "new_errors": delta["new"],
                       **({"starving_thermals": sorted(starving)}
                          if starving else {}),
                       "fields": {n: sorted(f) if f else "all"
                                  for n, f in sorted(where.items())},
                       "items_removed": rip_in_fields(staged, old_uuids, where,
                                                      bgas)})
    else:
        after = drc()
        delta = rc.drc_delta(before, after)
    delta["rip_rounds"] = rounds
    return after, delta, rounds


def unexplained_starving(before: dict, after: dict, delta: dict,
                         staged: Path, old_uuids: set[str]) -> dict:
    """The newly starved pads when a starved_thermal is the only new error
    left and no new copper is next to any of them (a nudged cap's pad, or
    the refill, cut the spoke: #979): msg -> position. Empty otherwise, and
    the escape is thrown away as for any other new error."""
    if set(delta["new"]) != {"starved_thermal"}:
        return {}
    mine = nets_with_new_copper(geom.BoardGeom.from_file(staged), old_uuids)
    if rc.nets_starving_thermals(before, after, mine, staged, old_uuids):
        return {}
    old = starved_pads(before)
    return {m: q for m, q in starved_pads(after).items() if m not in old}


def rip_in_fields(staged: Path, old_uuids: set[str],
                  where: dict[str, set[str] | None], bgas: list) -> int:
    """Remove the new copper of each net, only inside the named fields'
    regions (everywhere when None)."""
    regions = {b.ref: field_region(b) for b in bgas}
    bg = geom.BoardGeom.from_file(staged)
    ops = []
    for x in list(bg.tracks_of()) + list(bg.vias_of()):
        if not x.uuid or x.uuid in old_uuids or x.net not in where:
            continue
        refs = where[x.net]
        if refs is None or any(regions[r].intersects(x.poly) for r in refs):
            ops.append({"op": "remove", "uuid": x.uuid})
    if ops:
        rc.route_edit.apply_ops(staged, ops)
    return len(ops)


# #1339 (gaps row 139): a fine-via field's inner balls are fenced in. The
# 12 filled through vias of a 6 x 6 0.5 mm field's second ring stand 0.5 mm
# apart on every layer: 0.35 mm between hole edges where a track needs
# 0.2 + 0.0889 + 0.2, and no smaller through drill exists at the fab, so KRT
# finds no way out for the 2 x 2 inside on any layer. Freerouting can't
# either (its DSN grows the 0.25/0.15 padstack to the hole rule). The way
# out on a standard build: withhold one second-ring ball's via (a "gate"),
# which leaves 1.0 mm between its neighbours' holes, room for two tracks,
# and run two enclosed balls out through it on a free inner layer from
# filled vias in their own pads. One ball traded, two escaped. Only balls
# KRT already failed and that sit inside the via ring get this.
GATE_JOG = 0.3     # pitch: each track's offset from the gate ball's centre
GATE_REACH = 2.5   # pitch: stub end past the gate ball (1.5 beyond the field)
GATE_SIDES = (     # name, outward (drow, dcol), along (drow, dcol)
    ("left", (0, -1), (1, 0)), ("right", (0, 1), (1, 0)),
    ("up", (-1, 0), (0, 1)), ("down", (1, 0), (0, 1)))


def ball_grid(bg, bga: bgalib.Bga):
    """(row, col) -> pad for an axis-aligned field (rows run down y), plus
    the grid's (rows, cols); None when the balls don't sit on one."""
    x0, y0, x1, y1 = bga.bbox
    p = bga.pitch
    out = {}
    for pad in bg.pads_of(ref=bga.ref):
        if pad.number not in bga.nets and not pad.net:
            continue
        c, r = (pad.center[0] - x0) / p, (pad.center[1] - y0) / p
        if abs(c - round(c)) > 0.05 or abs(r - round(r)) > 0.05:
            return None
        out[(int(round(r)), int(round(c)))] = pad
    nr, nc = int(round((y1 - y0) / p)) + 1, int(round((x1 - x0) / p)) + 1
    return out, (nr, nc)


def _ring(rc_, shape) -> int:
    (r, c), (nr, nc) = rc_, shape
    return min(r, c, nr - 1 - r, nc - 1 - c)


def gate_trade_plan(bg, bga: bgalib.Bga, ep: dict, layers: list[str],
                    unescaped: set[str], protect: set[str],
                    old_uuids: set[str]) -> dict:
    """The gate trade for the enclosed balls of a fine-via (underpad) field
    that KRT left unescaped. Returns {"escaped": [nets], "traded": {gate
    net: [nets it let out]}, "layer": {net: layer}, "not_escaped": {net:
    why}, "ops": [route_edit ops to add]}; the traded nets' new copper is
    the caller's to rip first. `protect`: nets never traded (pairs, held
    wide nets). A gate ball with old copper of its own is never traded."""
    fv = ep["fine_via"]
    out = {"escaped": [], "traded": {}, "layer": {}, "not_escaped": {},
           "ops": []}
    got = ball_grid(bg, bga)
    if not got:
        return out
    grid, shape = got
    enclosed = {k: pad for k, pad in grid.items()
                if pad.net in unescaped and _ring(k, shape) >= 2}
    if not enclosed:
        return out
    for k, pad in enclosed.items():
        if _ring(k, shape) > 2:
            out["not_escaped"][pad.net] = (
                f"enclosed {_ring(k, shape)} rings deep: needs an HDI build "
                "(microvia in pad)")
    groups, taken = [], set()
    (nr, nc) = shape
    for side, o, a in GATE_SIDES:
        line = sorted((k for k in enclosed if _ring(k, shape) == 2
                       and k not in taken
                       and {"left": k[1] == 2, "right": k[1] == nc - 3,
                            "up": k[0] == 2, "down": k[0] == nr - 3}[side]),
                      key=lambda k: k[0] * a[0] + k[1] * a[1])
        i = 0
        while i < len(line):
            x = line[i]
            y = line[i + 1] if i + 1 < len(line) else None
            if y and (y[0] - x[0], y[1] - x[1]) == a:
                groups.append((o, a, [x, y]))
                i += 2
            else:
                groups.append((o, a, [x]))
                i += 1
        taken.update(line)

    p, w = bga.pitch, ep["track_width"]
    # the courtyard rule's copper clearance (ep's own is the fine ring's
    # track-to-via, which the fine rule's hole_clearance covers)
    clr = ep.get("rule_clearance", ep["clearance"])
    hclr = fv["hole_clearance"]
    h2h = fv["hole_to_hole"]
    vd, vh = fv["via_diameter"], fv["via_drill"]
    x0, y0 = bga.bbox[0], bga.bbox[1]

    def at(k, dr=0.0, dc=0.0):
        return (round(x0 + (k[1] + dc) * p, 4), round(y0 + (k[0] + dr) * p, 4))

    window = field_region(bga)
    items = [x for x in list(bg.tracks_of()) + list(bg.vias_of())
             + list(bg.pads_of()) if window.intersects(x.poly)]
    old_nets = {x.net for x in list(bg.tracks_of()) + list(bg.vias_of())
                if x.uuid in old_uuids and window.intersects(x.poly)}
    planned = []        # (net, layer|None for a via, poly, hole|None)

    def clash(net, layer, poly, hole, skip):
        """What `poly` (copper on `layer`; None: a via's ring, every layer,
        with its `hole`) comes too close to, or None when it keeps the
        field's clearance, hole clearance and hole-to-hole."""
        def who(n):
            return f"{n} on {layer or 'a via'}"
        for x in items:
            if x.net == net or x.net in skip:
                continue
            if not _on(x, layer):
                continue
            xp, xh = x.poly, _hole(x)
            if poly.distance(xp) < clr - 1e-6:
                return f"copper of {who(x.net)}"
            if xh is not None and poly.distance(xh) < hclr - 1e-6:
                return f"a hole of {who(x.net)}"
            if hole is not None and hole.distance(xp) < hclr - 1e-6:
                return f"copper of {who(x.net)} (hole clearance)"
            if hole is not None and xh is not None \
                    and hole.distance(xh) < h2h - 1e-6:
                return f"a hole of {who(x.net)} (hole to hole)"
        for n, ly, pp, hh in planned:
            if n == net:
                continue
            if layer is None or ly is None or ly == layer:
                if pp.distance(poly) < clr - 1e-6 or (
                        hole is not None and pp.distance(hole) < hclr - 1e-6
                ) or (hh is not None and poly.distance(hh) < hclr - 1e-6):
                    return f"the gate copper of {n}"
            if hole is not None and hh is not None \
                    and hole.distance(hh) < h2h - 1e-6:
                return f"the gate via of {n}"
        return None

    cands = [ly for ly in layers if ly != bga.layer]
    cands.sort(key=lambda ly: ly in ("F.Cu", "B.Cu"))     # inner first
    for o, a, balls in groups:
        nets = [enclosed[k].net for k in balls]
        options = []
        for s in ((1, -1),) if len(balls) == 1 else ((0, 1), (1, -1)):
            # gate outward of balls[s[0]]; it jogs -s[1]*a toward its side
            first = balls[s[0]]
            g = (first[0] + o[0], first[1] + o[1])
            gp = grid.get(g)
            gnet = gp.net if gp and gp.net else None
            if gnet and (gnet in protect or gnet in old_nets):
                continue
            paths = {}
            for k in balls:
                sgn = -s[1] if k == first else s[1]   # this track's side
                j = GATE_JOG if k == first else 1 - GATE_JOG
                if len(balls) == 1:     # straight out the gate's middle
                    pts = [at(k), at(g, GATE_REACH * o[0], GATE_REACH * o[1])]
                else:
                    # jog to the gate's lane: j of a pitch outward, and
                    # GATE_JOG to this track's side of the gate's centre
                    off = sgn * GATE_JOG
                    jdr = (j - 1) * o[0] + off * a[0]
                    jdc = (j - 1) * o[1] + off * a[1]
                    pts = [at(k), at(g, jdr, jdc),
                           at(g, GATE_REACH * o[0] + off * a[0],
                              GATE_REACH * o[1] + off * a[1])]
                paths[enclosed[k].net] = pts
            free = gnet is None or gnet in unescaped
            if len(balls) == 1 and not free:
                continue        # one for one gains nothing
            options.append((not free, gnet, paths))
        options.sort(key=lambda t: t[0])
        done, why = False, {}
        for _cost, gnet, paths in options:
            skip = {gnet} if gnet else set()
            for ly in cands:
                trial = []
                ok = True
                for net, pts in paths.items():
                    vp = Point(pts[0]).buffer(vd / 2)
                    hp = Point(pts[0]).buffer(vh / 2)
                    tp = LineString(pts).buffer(w / 2)
                    hit = clash(net, None, vp, hp, skip) \
                        or clash(net, ly, tp, None, skip)
                    if hit:
                        why.setdefault(ly, f"{net}: {hit}")
                        ok = False
                        break
                    trial += [(net, None, vp, hp), (net, ly, tp, None)]
                    planned.extend(trial[-2:])
                if not ok:
                    del planned[len(planned) - len(trial):]
                    continue
                for net, pts in paths.items():
                    out["ops"].append({"op": "add_via", "at": list(pts[0]),
                                       "size": vd, "drill": vh, "net": net})
                    out["ops"].append({"op": "add_path",
                                       "points": [list(q) for q in pts],
                                       "width": w, "layer": ly, "net": net})
                    out["escaped"].append(net)
                    out["layer"][net] = ly
                if gnet:
                    out["traded"][gnet] = sorted(paths)
                    items[:] = [x for x in items if x.net != gnet]
                done = True
                break
            if done:
                break
        if not done:
            for net in nets:
                out["not_escaped"][net] = (
                    "a lone enclosed ball: a gate would trade one ball for "
                    "one" if not options else
                    "no layer has room through a gate: " + "; ".join(
                        f"{ly}: {h}" for ly, h in why.items()))
    out["escaped"].sort()
    return out


def _hole(x):
    if isinstance(x, geom.Via):
        return Point(x.at).buffer(x.drill / 2)
    if getattr(x, "drill", None):
        return x.drill_poly
    return None


def _on(x, layer) -> bool:
    """x has copper on `layer` (None: on any layer - a through via's ring)."""
    if layer is None:
        return True
    if isinstance(x, geom.Track):
        return x.layer == layer
    return layer in x.layers


def nets_with_new_copper(bg, old_uuids: set[str]) -> set[str]:
    return {x.net for x in list(bg.tracks_of()) + list(bg.vias_of())
            if x.net and x.uuid and x.uuid not in old_uuids}


def fab_overrides(work: Path, ep: dict, floors: dict) -> Path:
    """Pin KRT's fab floor to the escape geometry (an overrides file also
    stops KRT escalating to its paid 'advanced' tier)."""
    p = work / "escape_fab_overrides.txt"
    p.write_text(
        f"clearance = {ep['clearance']}\n"
        f"track_width = {ep['track_width']}\n"
        f"via_diameter = {ep['via_size']}\n"
        f"via_drill = {ep['via_drill']}\n"
        f"hole_to_hole = {floors['hole_to_hole']}\n"
        f"board_edge = {floors['edge_clearance']}\n", encoding="utf-8")
    return p


def escape_pairs(bga: bgalib.Bga, specs: list[dict], geo: dict,
                 layers: list[str]) -> tuple[list[list[str]], float | None]:
    """([[p, n]...] of the pairs with both balls in this field, the edge
    gap to escape them at). The gap is the pair's rules_gen gap on the first
    inner escape layer (zrules geometry); None (KRT's default) when no pair
    has one."""
    balls = set(bga.nets.values())
    pairs = [[s["p"], s["n"]] for s in specs
             if s.get("p") in balls and s.get("n") in balls]
    inner = layers[1:-1]
    gaps = []
    for p, _n in pairs:
        for ly in inner:
            g = (geo.get(p) or {}).get(ly) or {}
            if "gap_mm" in g:
                gaps.append(float(g["gap_mm"]))
                break
    return pairs, (round(min(gaps), 4) if gaps else None)


def fanout_args(bga: bgalib.Bga, ep: dict, layers: list[str], fab: Path,
                grid_step: float, method: str | None = None,
                pairs: list[list[str]] | None = None,
                pair_gap: float | None = None) -> list[str]:
    extra: list[str] = []
    if pairs:
        extra += ["--diff-pairs", *[x for pr in pairs for x in pr]]
        if pair_gap:
            extra += ["--diff-pair-gap", f"{pair_gap:g}"]
    return extra + ["--component", bga.ref, "--layers", *layers,
            "--escape-method", method or ep["method"],
            "--track-width", str(ep["track_width"]),
            "--clearance", str(ep["clearance"]),
            "--via-size", str(ep["via_size"]),
            "--via-drill", str(ep["via_drill"]),
            "--grid-step", str(grid_step),
            "--fab-overrides", str(fab),
            "--check-for-previous", "--no-fix-drc-settings"]


def footprint_moves(before, after, refs: set[str] | None = None) -> list[dict]:
    out = []
    for ref, fa in sorted(after.footprints.items()):
        fb = before.footprints.get(ref)
        if not fb or (refs is not None and ref not in refs):
            continue
        (x0, y0), (x1, y1) = fb.get("at", (0, 0)), fa.get("at", (0, 0))
        a0, a1 = fb.get("angle", 0.0), fa.get("angle", 0.0)
        if abs(x1 - x0) > 1e-4 or abs(y1 - y0) > 1e-4 or abs(a1 - a0) > 1e-3:
            out.append({"ref": ref, "dx_mm": round(x1 - x0, 4),
                        "dy_mm": round(y1 - y0, 4),
                        "rot_deg": round(a1 - a0, 3)})
    return out


# ------------------------------------------------------------ HDI builds

def hdi_geometry(pitch: float, pad: float, caps: dict) -> tuple[float, float]:
    """(track, clearance) in the field: JLC's HDI floors (3 mil / 3 mil). They
    are what fits between via pads at 0.5 mm, and the field keeps them for
    every pitch so one rule covers it."""
    return (float(caps["min_trace_width_mm"]), float(caps["min_clearance_mm"]))


def planes_of(bg, bga: bgalib.Bga, planes: list[str]) -> dict[str, str]:
    """net -> the nearest inner plane under the field carrying it."""
    cu = bg.copper_layers
    start = cu.index(bga.layer)
    out: dict[str, str] = {}
    for ly in sorted(planes, key=lambda x: abs(cu.index(x) - start)):
        for z in bg.zones_of(layer=ly):
            if z.net and z.net not in out:
                out[z.net] = ly
    return out


def hdi_plan(bg, p: dict, spans: list, caps: dict, floors: dict,
             wide: dict[str, float], necks: dict | None = None
             ) -> hdilib.FieldPlan:
    b = p["bga"]
    track, clearance = hdi_geometry(b.pitch, b.pad_d, caps)
    fp = bg.footprints.get(b.ref, {})
    balls = bgalib._ball_pads([x for x in bg.pads_of(ref=b.ref)
                               if x.kind == "smd" and len(x.layers) == 1])
    _, held = fanout_nets(b, wide, track, plane_min=10 ** 6,
                          neck=field_necks(bg, b, necks or {}))
    region = field_region(b)
    return hdilib.plan_field(
        b, balls, fp.get("at"), fp.get("angle", 0.0) or 0.0, bg.copper_layers,
        spans, p["layers"], track=track, clearance=clearance,
        hole_clearance=float(caps["hole_to_trace_mm"]),
        plane_of=planes_of(bg, b, p["planes"]), held=set(held),
        existing=hdilib.obstacles_from_board(bg, region, b.ref))


def hdi_result(plan: hdilib.FieldPlan) -> dict:
    """The per-field record route_escape reports, in KRT's terms."""
    want = {b.net for b in plan.balls if b.net and b.role == "signal"}
    return {"requested": len(want), "escaped": len(want) - len(plan.left),
            "failed": len(plan.left), "unescaped_nets": plan.left}


# ------------------------------------------------------------ KRT steps

def power_first_ops(staged: Path, cons: Path | None,
                    skip_refs: list[str]) -> tuple[list[dict], dict]:
    """Plane vias for the constraints' inner-plane power nets' SMD pads,
    skipping skip_refs (the ball grids: their gap sites are the escape's).
    Returns (route_edit ops, fact); never touches the board."""
    if cons is None:
        return [], {"placed": 0, "note": "no constraints.json"}
    argv = ["--pcb", str(staged), "--constraints", str(cons), "--dry-run",
            "--nets", "power", "--pads-only"]
    if skip_refs:
        argv += ["--skip-refs", ",".join(sorted(skip_refs))]
    payload, _ = stitch_vias.run(argv)
    ops = payload["ops"]
    nets = payload.get("nets") or {}
    fact = {"nets": list(nets),
            "placed": sum(1 for o in ops if o["op"] == "add_via"),
            "requested": payload["requested"],
            "no_spot": sorted(f"{s['ref']}" for s in payload["skipped"]
                              if s["reason"] == "no_clear_spot")}
    if not nets:
        fact["note"] = "no power net with an inner plane"
    return ops, fact


def _run(krt: Path, script: str, args: list[str], timeout_s: int,
         log: Path, launch: list[str] | None = None) -> tuple[int, str]:
    """KRT script through lib/krt_launch.py (hwde's KRT patches, #552/#547)."""
    import subprocess
    cmd = [sys.executable, str(SCRIPTS / "lib" / "krt_launch.py"),
           *(launch or []), script, *args]
    try:
        cp = subprocess.run(cmd, cwd=str(krt), capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"KRT {script} timed out after {timeout_s}s") from exc
    out = (cp.stdout or "") + "\n" + (cp.stderr or "")
    log.write_text(out, encoding="utf-8")
    return cp.returncode, cp.stdout or ""


def _keep_sidecars(staged: Path, pristine: dict[str, bytes]) -> None:
    """KRT steps may rewrite the staged .kicad_pro (DRC floors); the board's
    own project + rules are the grading truth, so put them back."""
    for name, data in pristine.items():
        (staged.parent / name).write_bytes(data)


def fan_one(ctx: dict, bga: bgalib.Bga, ep: dict, layers: list[str],
            method: str | None = None, nets: list[str] | None = None,
            pairs: list[list[str]] | None = None,
            pair_gap: float | None = None, tag: str = "",
            open_to: dict[str, tuple[str, ...]] | None = None) -> dict:
    staged, work, krt = ctx["staged"], ctx["work"], ctx["krt"]
    out = work / (f"{staged.stem}.{bga.ref}.{method or ep['method']}"
                  f"{'.' + tag if tag else ''}.kicad_pcb")
    fl = {**ctx["floors"], **(ep.get("fine_via") or {})}
    fab = fab_overrides(work, ep, fl)
    launch = ["--hole-to-hole", f"{fl['hole_to_hole']:g}"]
    if open_to:
        plan = work / f"{out.stem}.open.json"
        plan.write_text(json.dumps({"open": {n: list(s) for n, s in
                                             sorted(open_to.items())}},
                                   indent=1), encoding="utf-8")
        launch += ["--escape-open", str(plan)]
    rcode, stdout = _run(krt, "bga_fanout.py",
                         [str(staged), "--output", str(out),
                          *fanout_args(bga, ep, layers, fab, ctx["grid_step"],
                                       method, pairs, pair_gap)]
                         + (["--nets", *nets] if nets else []),
                         ctx["timeout_s"], out.with_suffix(".log"), launch)
    summary = rc.parse_summary(stdout)
    if summary is None or not out.is_file():
        tail = stdout.strip()[-400:]
        raise CheckError(f"KRT bga_fanout on {bga.ref} produced no "
                         f"JSON_SUMMARY (rc={rcode}): {tail}")
    shutil.copy2(out, staged)
    _keep_sidecars(staged, ctx["pristine"])
    return summary


def nudge_caps(ctx: dict, clearance: float) -> list[dict]:
    staged, work, krt = ctx["staged"], ctx["work"], ctx["krt"]
    before = geom.BoardGeom.from_file(staged)
    out = work / f"{staged.stem}.capclear.kicad_pcb"
    _run(krt, "place_fanout_clearance.py",
         [str(staged), str(out), "--clearance", str(clearance),
          "--board-edge-clearance", str(ctx["floors"]["edge_clearance"])],
         ctx["timeout_s"], out.with_suffix(".log"))
    if not out.is_file():
        return []
    shutil.copy2(out, staged)
    _keep_sidecars(staged, ctx["pristine"])
    return footprint_moves(before, geom.BoardGeom.from_file(staged))


# ------------------------------------------------------------ CLI

def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--refs", default=None,
                    help="comma list of BGA refs (default: every ball grid)")
    ap.add_argument("--layers", default=None,
                    help="comma list of escape layers (default: every copper "
                         "layer that is not an inner plane under the field)")
    ap.add_argument("--method", choices=["auto", "dogbone", "channel",
                                         "underpad"], default=None,
                    help="KRT engine (default: dogbone when the budget fits, "
                         "else channel)")
    ap.add_argument("--allow-via-in-pad", "--via-in-pad",
                    dest="allow_via_in_pad", action="store_true",
                    help="keep KRT's via-in-pad fallbacks (a paid fab option: "
                         "filled, capped vias); with --pads, a pad with no "
                         "spot outside and nothing to join gets a filled, "
                         "capped via at its centre (#940). Ball fields "
                         "keep them by default where the fab fills vias "
                         "free (6+ layers at JLC, #325)")
    ap.add_argument("--no-via-in-pad", action="store_true",
                    help="strip KRT's via-in-pad fallbacks even where the "
                         "fab fills vias free")
    ap.add_argument("--no-gate-trade", action="store_true",
                    help="never withhold a ring-B ball's via to let an "
                         "enclosed inner ball out (row 139)")
    ap.add_argument("--no-cap-nudge", action="store_true",
                    help="never move decouplers; balls whose via lands on one "
                         "are ripped instead")
    ap.add_argument("--constraints", default=None,
                    help="constraints.json for the diff pairs escaped coupled "
                         "(default: next to the board)")
    ap.add_argument("--no-pairs", action="store_true",
                    help="escape diff-pair balls single-ended like any other")
    ap.add_argument("--pairs-first", action="store_true",
                    help="a pairs-only KRT pass before the rest, so pair balls "
                         "take the gap sites first (more pairs coupled, often "
                         "fewer single balls escaped)")
    ap.add_argument("--toward-partner", action="store_true",
                    help="escape the constraints' diff-pair and length_groups "
                         "nets out of the field sides facing the part each "
                         "one goes to (#547)")
    ap.add_argument("--hdi", choices=["auto", "on", "off"], default="auto",
                    help="auto (default): microvia-in-pad escape when the "
                         "board's stack is an HDI build; on: refuse a board "
                         "that isn't; off: KRT's through-via dog-bones")
    ap.add_argument("--stackup", default=None,
                    help="stackups.yaml entry (default: the one the board's "
                         "stack block matches)")
    ap.add_argument("--no-power-first", action="store_true",
                    help="skip the power pads' plane vias placed before "
                         "the escape (#923)")
    ap.add_argument("--plan", action="store_true",
                    help="no routing: report the BGAs, geometry and budget")
    ap.add_argument("--grid-step", type=float, default=DEFAULT_GRID)
    ap.add_argument("--timeout-s", type=int, default=900,
                    help="per-KRT-invocation process timeout")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--out-report", default=None)
    ap.add_argument("--pads", default=None,
                    help="pad fanout (#929): comma list of REF.PAD to give "
                         "a via and stub each, not a BGA escape")
    ap.add_argument("--fan-width", type=float, default=None,
                    help="--pads: stub width (default: the net's width)")
    ap.add_argument("--fan-via", default=None,
                    help="--pads: via SIZE/DRILL in mm (default: the "
                         "board's most common through via)")
    ap.add_argument("--out-ops", default=None,
                    help="--pads: write the route_edit ops here")
    ap.add_argument("--plane-balls", default=None,
                    help="comma list of ball-grid refs whose --plane-nets "
                         "balls each get a filled via in the pad; needs "
                         "--via-in-pad (#956)")
    ap.add_argument("--plane-nets", default=None,
                    help="--plane-balls: comma list of the plane nets")
    args = ap.parse_args(argv)
    if bool(args.plane_balls) != bool(args.plane_nets):
        raise CheckError("--plane-balls and --plane-nets go together")
    if args.pads or args.plane_balls:
        return pad_fanout(args)

    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    pro = pcb.with_suffix(".kicad_pro")
    floors = escape_floors(pro if pro.is_file() else None)
    bg0 = geom.BoardGeom.from_file(pcb)
    refs = [r.strip() for r in args.refs.split(",")] if args.refs else None
    bgas = bgalib.find_bgas(bg0, refs)
    if refs:
        missing = sorted(set(refs) - {b.ref for b in bgas})
        if missing:
            raise CheckError(f"not ball grids on this board: {missing}")
    explicit_layers = [x.strip() for x in args.layers.split(",")] \
        if args.layers else None
    specs: list[dict] = []
    cons = rc._sidecar(pcb, args.constraints, "constraints.json")
    cons_data = checklib.load_json(cons, "constraints") if cons else {}
    if cons_data:
        all_specs = rc.diff_specs(cons_data, bg0.nets)
        if not args.no_pairs:
            specs = all_specs
    else:
        all_specs = []
    if args.toward_partner and not cons_data:
        raise CheckError("--toward-partner needs constraints.json (its diff "
                         "pairs and length_groups name the nets)")
    geo = zrules.board_layer_geometry(pcb)
    build, entry = (None, None) if args.hdi == "off" \
        else hdilib.board_build(bg0, args.stackup)
    if args.hdi == "on" and not build:
        raise CheckError("--hdi on: this board's stack is not an HDI build "
                         "of stackups.yaml, which drills through vias only")
    hdi_caps = hdilib.load_hdi_caps()
    spans = hdilib.spans_of(entry, bg0.copper_layers, caps=hdi_caps) if build else []
    keep_vip, vip_why = via_in_pad_choice(
        len(bg0.copper_layers), args.allow_via_in_pad, args.no_via_in_pad,
        free_filled_via_layers())
    fine_cap = fine_cap_row(len(bg0.copper_layers)) \
        if keep_vip and not build else None
    plans = []
    for b in bgas:
        fv = bgalib.fine_via(b, len(bg0.copper_layers), fine_cap,
                             free_filled_via_layers()) if fine_cap else None
        ep = bgalib.escape_params(b, {**floors, **fv} if fv else floors)
        if fv:      # #1252: the fab's smallest via, filled, in the pad
            # the courtyard copper rule stays at the field's own clearance:
            # the fine via's thin ring needs 0.15 track-to-via, but that is
            # the fine rule's hole_clearance to check, not a track-to-track
            # rule Freerouting (netclass clearance) never sees - a 0.15
            # courtyard rule turned 18 of its 0.125 gaps into DRC errors
            ep.update(method="underpad", fine_via=fv,
                      rule_clearance=bgalib.escape_params(
                          b, floors)["clearance"],
                      why=(f"{b.pitch:g} mm field on {len(bg0.copper_layers)}"
                           f" layers: filled {fv['via_diameter']:g}/"
                           f"{fv['via_drill']:g} mm via in the pad "
                           "(JLC's BGA guide; POFV free from 6 layers)"))
        elif fine_cap:  # #552: kept in-pad vias that overhang the pad
            pv = bgalib.pad_via(b, len(bg0.copper_layers), fine_cap,
                                free_filled_via_layers(), ep["via_size"])
            if pv:
                ep["pad_via"] = pv
        if args.method:
            ep["method"] = args.method
        layers, planes = escape_layers(bg0, b, explicit_layers)
        pairs, gap = escape_pairs(b, specs, geo, layers)
        plans.append({"bga": b, "ep": ep, "layers": layers, "planes": planes,
                      "pairs": pairs, "pair_gap": gap})

    def _plan_fact(p):
        fv = p["ep"].get("fine_via")
        return {**p["bga"].as_dict(), **{k: p["ep"][k] for k in (
            "via_size", "via_drill", "track_width", "clearance", "method",
            "why", "budget")}, "layers": p["layers"],
            **({"fine_via": {"rule": bgalib.fine_rule_name(p["bga"].ref),
                             "hole_to_hole": fv["hole_to_hole"],
                             "hole_clearance": fv["hole_clearance"],
                             "rule_clearance": p["ep"]["rule_clearance"]}}
               if fv else {}),
            **({"pad_via": pad_via_fact(p["bga"], p["ep"]["pad_via"])}
               if p["ep"].get("pad_via") else {}),
            "plane_layers": p["planes"],
            **({"pairs": p["pairs"], "pair_gap_mm": p["pair_gap"]}
               if p["pairs"] else {}),
            **({"hdi_build": build} if build else {})}

    if args.plan or not bgas:
        facts = [_plan_fact(p) for p in plans]
        if build:
            for f, p in zip(facts, plans):
                pl = hdi_plan(bg0, p, spans, hdi_caps, floors, {})
                f["method"] = "hdi"
                f["hdi"] = pl.fact()
        payload = checklib.report(SCRIPT, str(pcb), [], facts={
            "bgas": facts, "floors": floors,
            "board_updated": False,
            "note": "plan only" if bgas else "no ball grid on this board"})
        return payload, args.out_report

    cli, krt = rc._tools()
    work = (Path(args.work_dir).resolve() if args.work_dir
            else pcb.parent / "route_escape")
    routelib.fresh_work_dir(work)
    staged = rc._stage_board(pcb, work)
    dru = staged.with_suffix(".kicad_dru")
    dru_text = dru.read_text(encoding="utf-8") if dru.is_file() else ""
    dru_text, rules_changed = bgalib.ensure_rules(
        dru_text, [(p["bga"].ref,
                    p["ep"].get("rule_clearance", p["ep"]["clearance"]),
                    p["ep"].get("fine_via") or p["ep"].get("pad_via"))
                   for p in plans])
    if rules_changed:
        dru.write_text(dru_text, encoding="utf-8")
    pristine = {f.name: f.read_bytes() for f in work.iterdir()
                if f.is_file() and f.suffix in (".kicad_pro", ".kicad_dru")
                and f.stem == staged.stem}
    ctx = {"staged": staged, "work": work, "krt": krt, "floors": floors,
           "grid_step": args.grid_step, "timeout_s": args.timeout_s,
           "pristine": pristine}

    if bg0.zones_of():
        kc.run_drc(cli, staged, refill=True, save_board=True)
    before = kc.run_drc(cli, staged, all_track_errors=True)
    power_first: dict = {"placed": 0, "note": "--no-power-first"}
    if not args.no_power_first:
        all_bgas = sorted(b.ref for b in bgalib.find_bgas(bg0, None))
        pf_ops, power_first = power_first_ops(staged, cons, all_bgas)
        if pf_ops:
            saved = staged.read_bytes()
            rc.route_edit.apply_ops(staged, pf_ops)
            if bg0.zones_of():
                kc.run_drc(cli, staged, refill=True, save_board=True)
            pf_after = kc.run_drc(cli, staged, all_track_errors=True)
            pf_new = rc.drc_delta(before, pf_after)["new"]
            if pf_new:     # drop the set; the escape runs as before
                staged.write_bytes(saved)
                power_first.update(placed=0, dropped_for_drc=pf_new)
            else:
                before = pf_after
    old_uuids = rc._copper_uuids(staged)

    wide = rc.dru_net_floors(dru, "track_width")
    necks = necklib.board_necks(staged, bg0,
                                rc.dru_neck_rules(dru, "track_width"))
    toward = set(toward_nets(cons_data, bg0.nets, all_specs)) \
        if args.toward_partner else set()
    keys = ("requested", "escaped", "failed", "unescaped_nets",
            "drc_grazes", "deferred_fanout_nets")
    results = []
    for p in plans:
        b, ep = p["bga"], p["ep"]
        if build:
            bg_now = geom.BoardGeom.from_file(staged)
            pl = hdi_plan(bg_now, p, spans, hdi_caps, floors, wide,
                          necks)
            if pl.ops:
                rc.route_edit.apply_ops(staged, pl.ops)
            results.append({
                **_plan_fact(p), "method": "hdi", "hdi": pl.fact(),
                "krt": hdi_result(pl),
                **({"held_wide": pl.held} if pl.held else {}),
                "via_in_pad": {"nets": 0, "vias": 0, "kept": True},
                "_vip": set(), "_lost": set(pl.held)})
            continue
        allowed, held = fanout_nets(b, wide, ep["track_width"],
                                    neck=field_necks(bg0, b, necks))
        only = allowed if held else None    # None: KRT's own net choice
        aset = set(allowed)
        first = None
        sides = (partner_sides(bg0, b, toward & aset) if toward else {})
        if p["pairs"] and args.pairs_first:
            # pairs first: their balls claim the dog-bone gap sites before
            # the single balls do (a full field otherwise leaves a deep pair
            # only via-in-pad, which is stripped)
            first = fan_one(ctx, b, ep, p["layers"],
                            nets=[x for pr in p["pairs"] for x in pr
                                  if x in aset],
                            pairs=p["pairs"], pair_gap=p["pair_gap"],
                            tag="pairs")
        summary = fan_one(ctx, b, ep, p["layers"], pairs=p["pairs"],
                          pair_gap=p["pair_gap"], nets=only, open_to=sides)
        r = {**_plan_fact(p), "krt": rc._slim(summary, keys),
             **({"krt_pairs": rc._slim(first, keys)} if first else {}),
             **({"toward_partner": toward_fact(sides)} if sides else {}),
             **({"held_wide": held} if held else {})}
        bg = geom.BoardGeom.from_file(staged)
        new = {x.uuid for x in list(bg.tracks_of()) + list(bg.vias_of())
               if x.uuid and x.uuid not in old_uuids}
        vip = via_in_pad_nets(bg, b, new)
        r["via_in_pad"] = {"nets": len(vip), "vias": sum(vip.values()),
                           "kept": keep_vip, "why": vip_why}
        if vip and not keep_vip:
            r["via_in_pad"]["items_removed"] = rc.rip_new_copper(
                staged, old_uuids, set(vip))
        elif vip:
            if ep.get("pad_via"):
                # #552: KRT lays the in-pad fallback at the dog-bone's via;
                # it shrinks to the filled pad via inside the pad
                n = shrink_pad_vias(staged, bg, b, new, ep["pad_via"])
                r["via_in_pad"]["shrunk"] = n
                if n:
                    bg = geom.BoardGeom.from_file(staged)
                    new = {x.uuid for x in list(bg.tracks_of())
                           + list(bg.vias_of())
                           if x.uuid and x.uuid not in old_uuids}
            clash = vip_conflicts(bg, b, new, (ep.get("fine_via")
                                               or ep.get("pad_via") or floors)
                                  ["hole_to_hole"], ep["clearance"])
            if clash:
                r["via_in_pad"]["clashing"] = sorted(clash)
                r["via_in_pad"]["items_removed"] = rip_in_fields(
                    staged, old_uuids, {n: {b.ref} for n in clash}, [b])
        r["_vip"] = set(vip) if not keep_vip else set()
        r["_lost"] = set(held) | set(r["via_in_pad"].get("clashing", []))
        if ep.get("fine_via") and keep_vip and not args.no_gate_trade:
            # row 139: balls the filled ring-B vias fence in get out
            # through a gate (one ring-B ball per side gives up its via)
            unesc = set(r["krt"].get("unescaped_nets", []) or []) \
                | set(r["via_in_pad"].get("clashing", []))
            protect = {x for pr in p["pairs"] for x in pr} | set(held)
            bg = geom.BoardGeom.from_file(staged)
            gt = gate_trade_plan(bg, b, ep, p["layers"], unesc, protect,
                                 old_uuids)
            if gt["escaped"]:
                traded = {g for g in gt["traded"]}
                if traded:
                    rc.rip_new_copper(staged, old_uuids, traded)
                rc.route_edit.apply_ops(staged, gt["ops"])
                r["_lost"] |= traded
            if gt["escaped"] or gt["not_escaped"]:
                r["enclosed_escape"] = {
                    **{k: gt[k] for k in ("escaped", "traded", "layer",
                                          "not_escaped")},
                    "fab_cost": "filled, capped via in pad (the enclosed "
                                "balls' own vias; free on this stack)"}
        results.append(r)

    moved = []
    if not args.no_cap_nudge and not build:
        clr = min(p["ep"]["clearance"] for p in plans)
        moved = nudge_caps(ctx, clr)

    # grade against the board's real rules; rip what broke them
    def drc():
        if bg0.zones_of():
            kc.run_drc(cli, staged, refill=True, save_board=True)
        return kc.run_drc(cli, staged, all_track_errors=True)

    after, delta, rounds = grade_and_rip(staged, old_uuids, before,
                                         [p["bga"] for p in plans], drc)
    starved = unexplained_starving(before, after, delta, staged, old_uuids)

    bg1 = geom.BoardGeom.from_file(staged)
    kept_nets = nets_with_new_copper(bg1, old_uuids)
    new_items = [x for x in list(bg1.tracks_of()) + list(bg1.vias_of())
                 if x.net and x.uuid and x.uuid not in old_uuids]
    violations = []
    for r, p in zip(results, plans):
        own = set(p["bga"].nets.values())
        vip = r.pop("_vip")
        ref = p["bga"].ref
        region = field_region(p["bga"])
        kept_here = {x.net for x in new_items if x.net in own
                     and region.intersects(x.poly)}
        ripped_here = {n for rd in rounds for n, f in rd["fields"].items()
                       if f == "all" or ref in f}
        r["kept_nets"] = len(kept_here)
        r["ripped_by_drc"] = sorted(ripped_here & own)
        ripped_all = ripped_here
        lost = set(r["krt"].get("unescaped_nets", []) or [])
        left = sorted((lost | (ripped_all & own) | vip | r.pop("_lost"))
                      - kept_here)
        r["left_for_routers"] = left
        if left:
            violations.append(violation(
                SCRIPT, "warning", None, None, None, [r["ref"]],
                f"{r['ref']}: {len(left)} ball net(s) not escaped (KRT "
                f"dropped {len(lost)}, via-in-pad stripped {len(vip)}, "
                f"via-in-pad clashing "
                f"{len(r['via_in_pad'].get('clashing', []))}, wide nets held "
                f"{len(r.get('held_wide', []))}, traded as gates for "
                f"enclosed balls "
                f"{len((r.get('enclosed_escape') or {}).get('traded', {}))}"
                f", DRC ripped "
                f"{len(r['ripped_by_drc'])}); left for route_critical / "
                "route_auto", SCRIPT, kind="bga_escape_incomplete",
                nets=left[:40]))
    moved_refs = {m.get("ref") for m in moved}
    for msg, pos in sorted(starved.items()):
        ref = msg.split(" of ", 1)[1].split()[0]
        layer = msg.rsplit(" on ", 1)[1]
        why = ("the cap nudge moved it" if ref in moved_refs else
               "no new escape copper is next to it; the zone refill cut it")
        violations.append(violation(
            SCRIPT, "error", pos, layer, None, [ref],
            f"{msg}: starved thermal after the escape ({why}); the escape is "
            "kept, give the pad its spokes back", SCRIPT,
            kind="escape_starved_thermal"))
    if delta["new"] and not starved:
        raise CheckError(
            "escape left new DRC errors after ripping (original board "
            f"untouched): {delta['new']}; work dir kept at {work}")

    filled = []
    if keep_vip and kept_nets:
        # kept in-pad vias get filling and capping set (route_edit
        # fill_vias), so check_wicking reads them closed and fab_export
        # writes VIA-FILL.md for the order, as --pads does (#940)
        import route_edit
        filled = [v for p in plans
                  for v in in_pad_vias(bg1, p["bga"], {x.uuid for x in new_items})]
        if filled:
            route_edit.apply_ops(staged, [
                {"op": "fill_vias", "kind": "via", "uuid_prefix": v.uuid,
                 "filled": True} for v in filled])
    for r, p in zip(results, plans):
        r["via_in_pad"]["filled"] = sum(
            1 for v in filled if v.net in set(p["bga"].nets.values()))
    teardropped = []
    if kept_nets:
        teardropped = enable_breakout_teardrops(
            cli, staged, work, [x for x in new_items
                                if isinstance(x, geom.Via)])
    for r, p in zip(results, plans):
        r["teardrops"] = sum(1 for v in teardropped
                             if v.net in set(p["bga"].nets.values()))

    board_updated = (bool(kept_nets) or bool(moved)
                     or bool(power_first.get("placed")))
    if board_updated:
        routelib.swap_in(staged, pcb)
        if rules_changed:
            shutil.copy2(dru, pcb.with_suffix(".kicad_dru"))

    payload = checklib.report(SCRIPT, str(pcb), violations, facts={
        "bgas": results,
        "caps_moved": moved,
        "power_first": power_first,
        "dru_rules": rules_changed if board_updated else [],
        "drc_delta": delta,
        "floors": floors,
        "board_updated": board_updated,
        "work_dir": str(work),
    })
    return payload, args.out_report


def breakout_vias(vias: list) -> list:
    """Escape vias whose ring is under the fab's absolute ring plus drill
    wander (teardroplib: JLC 0.05 + 0.075): a fine field's 0.15/0.25 via
    has 0.05. dfm_check flags each track join on one with no teardrop as
    dfm_ring_breakout; the routers add the inner-layer joins later."""
    import teardroplib as tdl
    lim = tdl.MIN_RING_ABS_MM + tdl.DRILL_WANDER_MM
    return [v for v in vias
            if v.uuid and (v.diameter - v.drill) / 2.0 < lim - 1e-9]


def enable_breakout_teardrops(cli, staged: Path, work: Path,
                              vias: list) -> list:
    """Turn on KiCad's per-via teardrop setting on the breakout_vias of the
    escape (lib/teardrop_swig enable). It makes no copper here: every later
    zone refill (drc_routed, route_teardrops) builds a drop on each track
    joining the via, including the inner-layer ones the routers add. The
    breakout vias that got it are returned."""
    import env
    import teardroplib as tdl
    todo = breakout_vias(vias)
    if not todo:
        return []
    bp = env.find_kicad_python(cli) if cli else None
    if bp is None:
        raise CheckError("KiCad bundled python not found (env.py)")
    cfg = tdl.config(None)
    job = {"verb": "enable", "board": str(staged), "out": str(staged),
           "vias": [v.uuid for v in todo], "pads": [],
           "params": {k: cfg[k] for k in (
               "length_ratio", "max_length_mm", "width_ratio",
               "max_width_mm", "filter_ratio", "curved")}}
    res = routelib.run_worker(bp, job, work,
                              worker=SCRIPTS / "lib" / "teardrop_swig.py")
    missing = set(res.get("missing") or [])
    return [v for v in todo if v.uuid not in missing]


def filled_via_layers() -> set[int]:
    """Copper layer counts the fab fills and caps vias on (POFV,
    jlc_pricing.yaml options.via_fill_epoxy)."""
    import order_quote
    table = (order_quote.load_pricing(order_quote.PRICING).get("options")
             or {}).get("via_fill_epoxy") or {}
    return {int(k) for k in table}


def fine_cap_row(n_copper: int) -> dict | None:
    """The JLC capability row (1 oz) whose abs_min_via_* floors size a
    fine field's via (#1252); None when there is no such row."""
    import fabfloors
    try:
        return fabfloors.profile(n_copper, 1.0)[1]
    except fabfloors.FabFloorError:
        return None


def free_filled_via_layers() -> set[int]:
    """Layer counts whose filled, capped vias the fab charges nothing for."""
    import order_quote
    return bgalib.free_fill_layers(order_quote.load_pricing(order_quote.PRICING))


def plane_ball_specs(bg, refs: list[str], nets: list[str]) -> list[str]:
    """REF.PAD for every pad of `refs` on `nets`, in pad order; a ref with
    no pads, or a net on none of their pads, is a typo and stops the run."""
    out: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        pads = bg.pads_of(ref=ref)
        if not pads:
            raise CheckError(f"--plane-balls {ref}: no such part on the board")
        for p in sorted(pads, key=lambda p: (len(p.number), p.number)):
            if p.net in nets:
                out.append(f"{ref}.{p.number}")
                seen.add(p.net)
    missing = [n for n in nets if n not in seen]
    if missing:
        raise CheckError(f"--plane-nets {', '.join(missing)}: on no ball of "
                         f"{', '.join(refs)}")
    return out


def pad_fanout(args):
    """--pads: lib/padfanout's vias and stubs, applied with route_edit.
    With --via-in-pad a via may sit in its pad (#940): only on a layer count
    the fab fills vias on, and each such via gets filling and capping set
    (route_edit fill_vias), so check_wicking reads it closed and fab_export
    writes VIA-FILL.md for the order."""
    import padfanout
    import route_edit
    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    via = None
    if args.fan_via:
        try:
            size, drill = (float(x) for x in args.fan_via.split("/"))
        except ValueError as e:
            raise CheckError("--fan-via wants SIZE/DRILL, e.g. 0.45/0.2") from e
        via = (size, drill)
    if args.fan_width is not None and args.fan_width <= 0:
        raise CheckError("--fan-width must be positive")
    bg = geom.BoardGeom.from_file(pcb)
    vip = bool(args.allow_via_in_pad)
    if vip:
        n = len(bg.copper_layers)
        offered = filled_via_layers()
        if n not in offered:
            raise CheckError(
                f"--via-in-pad: the fab fills and caps vias only on "
                f"{', '.join(map(str, sorted(offered))) or 'no'}-layer "
                f"boards (jlc_pricing.yaml options.via_fill_epoxy); this "
                f"board has {n} copper layers")
    if args.plane_balls and not vip:
        raise CheckError("--plane-balls puts a via in each ball's pad, a paid "
                         "fab option (filled, capped vias): add --via-in-pad "
                         "once that option is taken")
    specs = [s.strip() for s in (args.pads or "").split(",") if s.strip()]
    fo = padfanout.Fanout(bg, width=args.fan_width, via=via, via_in_pad=vip)
    ops, rows = fo.fan(specs)
    if args.plane_balls:
        balls = plane_ball_specs(
            bg, [r.strip() for r in args.plane_balls.split(",") if r.strip()],
            [n.strip() for n in args.plane_nets.split(",") if n.strip()])
        bops, brows = fo.fan(balls, in_pad_only=True)
        ops, rows = ops + bops, rows + brows
    # a selector sees the board as it was before its list ran, so the
    # vias in pads are filled by a second list once they exist
    fill = [{"op": "fill_vias", "kind": "via", "net": r["net"],
             "at": r["at"], "filled": True}
            for r in rows if r["result"] == "via in pad"]
    if args.out_ops:
        Path(args.out_ops).write_text(json.dumps(
            {"version": 1, "ops": ops, **({"then": fill} if fill else {})},
            indent=1) + "\n", encoding="utf-8")
    applied = bool(ops) and not args.plan
    if applied:
        route_edit.apply_ops(pcb, ops)
        if fill:
            route_edit.apply_ops(pcb, fill)
    violations = [violation(
        SCRIPT, "warning", None, None, None, [r["pad"].split(".")[0]],
        f"{r['pad']} ({r['net'] or 'no net'}): {r['result']}", SCRIPT,
        kind="pad_fanout_incomplete", nets=[r["net"]] if r["net"] else [])
        for r in rows if r["result"] in ("no spot", "no net",
                                         "no copper layer")]
    payload = checklib.report(SCRIPT, str(pcb), violations, facts={
        "pads": rows, "ops": len(ops), "board_updated": applied,
        "via_in_pad": len(fill)})
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
