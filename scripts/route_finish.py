"""route_finish - close the last unconnected items with a grid maze router (S11).

route_auto's Freerouting ladder can stall at 88-98% on a dense board, and
its KRT pass may short the board or lay power nets under their width rule.
This pass routes what DRC still reports as unconnected, one connection at a
time, on a fine grid (default 0.1 mm) with 8-way moves and through vias:

- The two sides of a connection are copper ISLANDS of the net (pads, tracks,
  vias and zone fill joined through vias), found from the board's geometry,
  so a stranded pour fragment, a boxed-in pad or a track stub are all just
  islands. A side on a plane layer is reached by dropping a via into it.
- The rules are the board's own: track width per net from the .kicad_dru
  (rules_gen's power widths), clearance from the .kicad_dru baseline or the
  netclass, per-net clearance floors, hole-to-hole, hole clearance, copper to
  edge, rule-area keepouts. A clearance rule for a net pair or a part's
  courtyard (rules_gen's HV waivers and package ratings) is read per item
  pair as DRC reads it, the last rule that holds winning (#682). Vias match the board's most common through via.
  On an HDI build (#477) a layer change may also take any span the build
  laminates, at its own size and JLC's HDI numbers, for a share of a through
  via's cost (SPAN_COST): a microvia is the cheap way down one layer.
- An HDI build's nets start at their floor width and the smallest legal via,
  unless a rule sizes their copper, and every layer costs the same, because
  the field's stubs fill the board's outer layers first. While a net is still
  open, its stub tips keep a corridor (EXIT_MM) straight ahead on their own
  layer, so that nets routed earlier don't wall it in (#784).
- Other-net zone fills are not walls (they refill around new copper) but cost
  more to cross, so a route only splits a pour when it must; a piece it cuts
  off is joined by the next pass.
- When no path exists: rip-up and reroute of this run's own routes; a wider
  window; a finer grid at the net's floor width and the smallest legal via;
  then, for a pad walled in by other nets' tracks, a search where their
  board copper is passable at a price, ripping the fewest signal nets in the
  way (never a pour net or a net with a width rule; at most 4 nets). Ripped
  nets come back unconnected and the next pass routes them again.
  A side no track can leave even then, with only fixed copper about (every
  cell of its own copper inside a neighbour's clearance), fails with the
  pad, the value and the rule that wall it in, and no rip is tried (#682).
- Every route is applied through route_edit, the board refilled and DRC run.
  A connection whose copper DRC names in an error is taken out again. A pass
  is kept when the unconnected count drops and no error type grows; a pass
  that ripped board copper may hold provisionally, and the run must then
  beat the board as it was before it. A ripped net must also end in no more
  pieces (DRC unconnected items) than before its rip, or the run goes back
  to the board before its first rip (#939). Stubs the rips leave hanging are
  swept (route_cleanup's dangling pass). Everything runs on a staged copy.
- --rip-errors (route_auto passes it) first takes out the tracks and vias DRC
  names in a short, clearance, crossing, hole or width error, and routes
  them again; kept only if errors + unconnected end lower with no error type
  growing.

Contract:
  route_finish.py --pcb B.kicad_pcb [--nets A,B] [--layers auto|F.Cu,B.Cu]
      [--grid 0.1] [--margin 4] [--min-width 0.2] [--clearance-margin 0.02]
      [--via-size MM --via-drill MM] [--max-conns N] [--rip-errors]
      [--no-rip-board] [--dry-run] [--work-dir DIR] [--out-report r.json]
      [--skip-conns c.json] [--protect-nets A,B] [--plan-seconds S]
      [--start-report r.json] [--trace t.jsonl] [--measure-only]
  exit 0 = nothing left unconnected and DRC clean; 1 = some remain or other
  violations (board updated with what was kept); 2 = error (board untouched).

finish_staged() is the importable form route_auto calls on its staged board.

The last group of flags is what route_close (the chunked driver) runs a chunk
with: connections to leave out, nets never to rip, when to stop searching so
the chunk still reports, the report the last chunk left so this one needn't
repeat its DRC, and a file naming the connection being searched, so a chunk
that dies can be blamed on it.
"""
from __future__ import annotations

import argparse
import ctypes
import dataclasses
import fnmatch
import hashlib
import heapq
import json
import math
import os
import re
import subprocess
import sys
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import numpy as np  # noqa: E402
import shapely  # noqa: E402
import shapely.affinity  # noqa: E402
import shapely.ops  # noqa: E402
from shapely.geometry import LineString, Point, box  # noqa: E402
from shapely.strtree import STRtree  # noqa: E402
from scipy import ndimage  # noqa: E402

import check_wicking  # noqa: E402
import checklib  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import hdilib  # noqa: E402
import kc  # noqa: E402
from necklib import NECK_REACH_MM, board_necks  # noqa: E402,F401  (#937)
import routelib  # noqa: E402
import teardroplib as tdl  # noqa: E402
from checklib import CheckError  # noqa: E402

KICAD_HOLE_CLEARANCE = 0.25   # board setup default min hole clearance
KICAD_HOLE_TO_HOLE = 0.25     # board setup default min hole to hole
SAME_NET_HOLE_MM = 0.13       # JLC HDI, one net, edge to edge (jlc_capabilities hdi)
EXIT_MM = 1.0                 # #784, how far an open stub tip's corridor runs
SPOKES_MIN = 2                # KiCad's min_resolved_spokes default: fewer
#                               is DRC's starved thermal (#1140)
SPOKE_DEFAULTS = (0.5, 0.5, 0.25)   # KiCad's thermal gap, spoke width and zone
#                                     min thickness when the board says none
VIA_PAD_KEEP_MM = 0.1         # drill edge to an own-net SMD pad's mask
#                               opening: past its registration (check_wicking)
VIA_WEB_NEAR_MM = 0.25        # check_wicking NEAR_MM: an untented via this
#                               close to a pad needs a full mask dam
PAD_VIA_TRIES = 3             # parts tried one at a time to name the pads a
#                               failed route needs a via in (#636)
FINE_MARGIN_MM = 0.005        # #923 point 3, the fine rung's clearance margin:
#                               0.5 mm-pitch balls leave ~0.25 mm, which a floor
#                               track fits under 0.005 but not the default 0.02
VIA_RETRIES = 6               # #784, searches again for a route's too-close vias


# ------------------------------------------------------------------ rules

@dataclass
class Rules:
    clearance: float
    hole_clearance: float
    hole_to_hole: float
    edge_clearance: float
    min_width: float
    via_size: float
    via_drill: float
    net_width: dict = field(default_factory=dict)
    net_clearance: dict = field(default_factory=dict)
    margin: float = 0.02
    track_floor: float = 0.1
    via_floor: tuple = (0.45, 0.2)   # the smallest via DRC passes
    spans: list = field(default_factory=list)   # the HDI build's (#477)
    span_hole_clearance: float = 0.15
    span_hole_to_hole: float = 0.24
    copper: list = field(default_factory=list)
    via_in_pad_refs: frozenset = frozenset()   # refs whose own-net SMD pads
    #                                          take a via (filled + capped)
    necks: dict = field(default_factory=dict)  # net -> {layer: [(zone,
    #                    width)]}: where its tracks may narrow (#924)
    bans: object = None   # routelib.DruBans: the .kicad_dru's disallow
    #                       track and via rules (#1140)
    sized_nets: frozenset = frozenset()   # nets a DRU width or gap rule
    #       sizes, whatever its bounds: never ripped (#1140)
    mask: dict | None = None   # check_wicking.via_keep_model: pad mask
    #       margins and untented sides, None when the board sets none (#636)
    pad_vias: bool = False   # every own-net SMD pad takes a via: only the
    #       search that names the pad a failed route needs (#636)
    pair: object = None   # PairRules: the DRU's clearance rules held per
    #       net pair or courtyard, None when it has none (#682)
    net_clearance_rule: dict = field(default_factory=dict)   # net -> the
    #       DRU rule its net_clearance floor comes from

    def pad_via_keep(self, ref: str, number: str, via_r: float,
                     drill: float) -> float:
        """How far a via's centre keeps off an own-net SMD pad's copper
        (#636): its hole out of the pad's mask opening by VIA_PAD_KEEP_MM,
        and an untented via's own opening a full dam from the pad's (or
        VIA_WEB_NEAR_MM off its copper), as check_wicking measures them."""
        m = self.mask or {}
        side, margin = (m.get("pads") or {}).get((ref, number), (None, 0.0))
        margin = max(margin, 0.0)
        keep = drill / 2 + margin + VIA_PAD_KEEP_MM
        if side and (m.get("open") or {}).get(side):
            keep = max(keep, via_r + 0.01 + min(
                VIA_WEB_NEAR_MM, m["via_margin"] + margin + m["dam"]))
        return keep

    def net_layers(self, net: str, layers) -> list:
        """Of `layers`, those the board's DRU lets the net have a track on
        (#1140: a route on a layer a disallow rule bans fails DRC)."""
        banned = self.bans.track_layers(net) if self.bans else set()
        return [ly for ly in layers if ly not in banned]

    def via_bans(self, net: str) -> set:
        """The via kinds (through, micro, blind, buried) a DRU disallow
        rule keeps off the net."""
        return self.bans.via_kinds(net) if self.bans else set()

    def width(self, net: str) -> float:
        w = max(self.net_width.get(net, 0.0) + 0.001, self.min_width)
        return math.ceil(w * 1000 - 1e-6) / 1000

    def width_floor(self, net: str) -> float:
        """The narrowest width DRC passes for the net (the pro floor or the
        net's .kicad_dru rule)."""
        w = max(self.net_width.get(net, 0.0), self.track_floor) + 0.001
        return min(self.width(net), math.ceil(w * 1000 - 1e-6) / 1000)

    def clr(self, a: str | None, b: str | None) -> float:
        return self.net_clr(a, b) + self.margin

    def net_clr(self, a: str | None, b: str | None) -> float:
        """The net-wide clearance of a net pair, no margin."""
        return max(self.clearance, self.net_clearance.get(a or "", 0.0),
                   self.net_clearance.get(b or "", 0.0))

    def item_clr(self, a, b) -> float:
        """clr() for two items (Item or Probe), lowered where a pair or
        courtyard rule of the DRU holds them closer (#682)."""
        if self.pair is None:
            return self.clr(a.net, b.net)
        return self.pair.value(a, b)[0] + self.margin

    def held_by(self, net: str) -> str:
        """The rule a net's clearance floor comes from, or ''."""
        return self.net_clearance_rule.get(net, "")


# A DRU rule conditioned on just this applies to every via, so its via
# constraints are board floors (rules_gen writes the fab's that way, #981).
VIA_ONLY = "A.Type == 'via'"
VIA_CONSTRAINTS = ("via_diameter", "hole_size", "annular_width")
KICAD_ANNULAR = 0.1


def board_rules(pcb: Path, bg: geom.BoardGeom, *, min_width: float = 0.2,
                margin: float = 0.02, via_size: float | None = None,
                via_drill: float | None = None) -> Rules:
    """The rules DRC grades this board at: the .kicad_dru's unconditioned
    rules win over the .kicad_pro netclass floors (KiCad custom rules take
    precedence), per-net rules on top. A rule on `A.Type == 'via'` alone
    counts as unconditioned for the via's size, drill and annular ring."""
    import route_critical as rc
    pro = pcb.with_suffix(".kicad_pro")
    dru = pcb.with_suffix(".kicad_dru")
    floors = rc.grading_floors(pro)
    base: dict[str, float] = {}
    if dru.is_file():
        for r in rc.parse_dru_rules(dru.read_text(encoding="utf-8")):
            if not r["nets"] and (r["condition"] is None or (
                    r["constraint"] in VIA_CONSTRAINTS
                    and r["condition"].strip() == VIA_ONLY)):
                base[r["constraint"]] = max(base.get(r["constraint"], 0.0),
                                            r["min_mm"])
    pro_json: dict = {}
    try:
        pro_json = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    pro_json = pro_json if isinstance(pro_json, dict) else {}
    pro_rules = (pro_json.get("board") or {}).get(
        "design_settings", {}).get("rules", {}) or {}
    # The via's annular ring: the DRU's, or the .kicad_pro's, whichever is
    # larger (KiCad's 0.1 mm stock default when neither sets one).
    annular = max(base.get("annular_width", 0.0),
                  float(pro_rules.get("min_via_annular_width") or 0.0)) \
        or KICAD_ANNULAR
    vias = {}
    for v in bg.vias_of():
        if v.kind != "through":     # the through via is what's being sized
            continue
        k = (round(v.diameter, 3), round(v.drill, 3))
        vias[k] = vias.get(k, 0) + 1
    common = max(vias, key=lambda k: (vias[k], k)) if vias else None
    vs = via_size or (common[0] if common else max(
        floors["via_diameter"], base.get("via_diameter", 0.0), 0.6))
    vd = via_drill or (common[1] if common else max(
        floors["via_drill"], base.get("hole_size", 0.0), 0.3))
    classes = routelib.pro_net_classes(pro_json, bg.nets)
    dru_text = dru.read_text(encoding="utf-8") if dru.is_file() else ""
    net_clr = rc.dru_net_floors(dru, "clearance")
    held: dict[str, tuple] = {}
    for r in rc.parse_dru_rules(dru_text) if dru_text else []:
        if r["constraint"] == "clearance" and not re.search(
                r"intersectsCourtyard", r["condition"] or ""):
            for n in r["nets"]:
                if r["min_mm"] >= held.get(n, (-1.0, ""))[0]:
                    held[n] = (r["min_mm"], r["name"])
    clearance = base.get("clearance", floors["clearance"])

    def old(a, b):
        return max(clearance, net_clr.get(a or "", 0.0),
                   net_clr.get(b or "", 0.0))
    pair = pair_rules(pcb, dru_text, old, float(
        pro_rules.get("min_clearance") or 0.0)) if dru_text else None
    build, entry = hdilib.board_build(bg)
    hdi: dict = {}
    if build:
        caps = hdilib.load_hdi_caps()
        hdi = {"spans": hdilib.spans_of(entry, bg.copper_layers, caps=caps),
               "span_hole_clearance": float(caps["hole_to_trace_mm"]),
               "span_hole_to_hole": float(caps["hole_to_hole_diff_net_mm"]),
               "copper": list(bg.copper_layers)}
    return Rules(
        clearance=clearance, pair=pair,
        net_clearance_rule={n: v[1] for n, v in held.items()},
        hole_clearance=max(base.get("hole_clearance", 0.0), float(
            pro_rules.get("min_hole_clearance", KICAD_HOLE_CLEARANCE))),
        hole_to_hole=max(base.get("hole_to_hole", 0.0), float(
            pro_rules.get("min_hole_to_hole", KICAD_HOLE_TO_HOLE))),
        edge_clearance=base.get("edge_clearance", floors["edge_clearance"]),
        min_width=max(min_width, floors["track_width"]),
        track_floor=max(floors["track_width"], base.get("track_width", 0.0)),
        via_floor=(min(vs, max(base.get("via_diameter", floors["via_diameter"]),
                               base.get("hole_size", floors["via_drill"])
                               + 2 * annular)),
                   min(vd, base.get("hole_size", floors["via_drill"]))),
        via_size=vs, via_drill=vd,
        net_width=rc.dru_net_floors(dru, "track_width"),
        net_clearance=net_clr,
        necks=board_necks(pcb, bg, rc.dru_neck_rules(dru, "track_width")),
        bans=routelib.dru_layer_bans(
            dru.read_text(encoding="utf-8") if dru.is_file() else "",
            list(bg.copper_layers), net_classes=classes,
            layer_names=getattr(bg, "layer_user_names", None)),
        sized_nets=frozenset(rc.dru_sized_nets(dru, bg.nets, classes)),
        mask=check_wicking.via_keep_model(pcb),
        margin=margin, **hdi)


# ------------------------------------------------------ pair rules (#682)

PAIR_TRIES = 4        # searches again when a found path's track misses a
#                       courtyard rule's value: the grid reads the rule per
#                       cell, DRC per whole segment
PAIR_COURTS_MAX = 3   # courtyards near one item the grid tells apart; past
#                       it the item keeps its net-wide clearance
_PAIR_TYPES = {"pad", "via", "track", "arc", "zone", "text", "graphic",
               "footprint"}


class Probe:
    """A track or via not laid yet as a DRU condition reads it (#682): its
    net, its type (track or via) and the courtyards it meets."""
    __slots__ = ("net", "kind", "courts", "layers", "ref")

    def __init__(self, net: str, kind: str, courts=frozenset(), layers=()):
        self.net, self.kind, self.courts = net, kind, frozenset(courts)
        self.layers, self.ref = frozenset(layers), ""


def _dru_terms(tree, out: list) -> list:
    """Every leaf term of a parsed DRU condition."""
    if tree[0] in ("and", "or"):
        for t in tree[1]:
            _dru_terms(t, out)
    else:
        out.append(tree)
    return out


def _rule_layer_ok(layer: str | None, ly: str) -> bool:
    """Whether a .kicad_dru rule's (layer ...) clause takes in copper layer
    ly (board_update._rule_layer_ok): outer, inner, or one layer's name;
    no clause or a name this reader does not know takes in every layer."""
    if layer == "outer":
        return ly in ("F.Cu", "B.Cu")
    if layer == "inner":
        return re.fullmatch(r"In\d+\.Cu", ly) is not None
    if layer is not None and re.fullmatch(r"(F|B|In\d+)\.Cu", layer):
        return layer == ly
    return True


class PairRules:
    """The .kicad_dru clearance rules that hold for a pair of items, not a
    net (#682): a net pair (A.NetName == 'x' && B.NetName == 'y', rules_gen's
    aiee_hv_waived_*) or a part's courtyard (A.intersectsCourtyard('U2') &&
    B.intersectsCourtyard('U2'), its aiee_pkg_rating_*), which Rules.clr's
    net-wide floors hold at the HV value. value() resolves a pair as KiCad
    does: the last rule in the file that holds wins, floored by the board's
    minimum clearance and any isolation barrier, and never above Rules.clr's
    net-wide value: a term this reader cannot answer keeps that value."""

    def __init__(self, rows: list, fps: dict, courts: dict, floor: float,
                 barriers: list, old):
        self.rows = rows            # [(min, layer, tree, name)], file order
        self.fps = fps              # ref -> placelib.Footprint, None: dup
        self.courts = courts        # (ref, side 0 own / 1 far) -> polygon
        self.keys = list(courts)
        self.tree = STRtree([courts[k] for k in self.keys]) \
            if self.keys else None
        self.floor = floor
        self.barriers = barriers    # routelib.dru_barriers
        self.old = old              # (net, net) -> net-wide value, no margin
        self._named: dict = {}
        self._memo: dict = {}
        self._held: dict = {}       # id(item) -> (item, courts)

    # -- the items' courtyards
    def courts_of(self, it, keep: bool = True) -> frozenset:
        """The (ref, side) courtyards an item's copper meets."""
        if isinstance(it, Probe):
            return it.courts
        got = self._held.get(id(it))
        if got is not None and got[0] is it:
            return got[1]
        out = frozenset()
        if self.tree is not None:
            shape = it.shape if it.half <= 0 else it.shape.buffer(
                it.half, quad_segs=4)
            out = frozenset(self.keys[i] for i in self.tree.query(
                shape, predicate="intersects"))
        if keep:
            self._held[id(it)] = (it, out)
        return out

    def near(self, shape, reach: float) -> list:
        """The courtyards within reach of shape."""
        if self.tree is None:
            return []
        return [self.keys[i] for i in self.tree.query(
            shape, predicate="dwithin", distance=reach)]

    # -- the condition
    def _fpid(self, ref: str, sel: str):
        fp = self.fps.get(ref)
        return fp.fpid if fp is not None and ":" in sel else None

    def _scope(self, fn: str, sel: str, it):
        import route_critical as rc
        if fn == "member":
            if getattr(it, "kind", "") != "pad":
                return False
            return rc.dru_selector_matches(sel, it.ref, self._fpid(it.ref,
                                                                    sel))
        if sel in ("A", "B"):
            return False
        if sel not in self._named:
            self._named[sel] = [
                (ref, m) for ref in self.fps
                if (m := rc.dru_selector_matches(
                    sel, ref, self._fpid(ref, sel))) is not False]
        mine = self.courts_of(it)
        out = False
        for ref, m in self._named[sel]:
            fp = self.fps.get(ref)
            if fp is None:
                out = None
                continue
            sides = (0, 1) if fn == "court" else (0 if fn == fp.side else 1,)
            hit = rc._or3(None if fp.courtyard_unreadable[s] else
                          False if fp.courtyard_exact[s] is None else
                          (ref, s) in mine for s in sides)
            if hit is False:
                continue
            if m is True and hit is True:
                return True
            out = None
        return out

    @staticmethod
    def _type_eq(it, literal: str):
        lit = literal.lower()
        if "*" in lit or "?" in lit or lit not in _PAIR_TYPES:
            return None
        kind = getattr(it, "kind", "")
        if kind in ("pad", "zone"):
            return lit == kind
        if kind == "via":
            return True if lit == "via" else None if lit == "track" else False
        if kind == "segment":       # a board track or arc
            return None if lit in ("track", "arc") else False
        if kind == "track":         # a straight one laid here
            return lit == "track"
        return None

    def _eval(self, tree, a, b):
        import route_critical as rc
        kind = tree[0]
        if kind == "and":
            return rc._and3(self._eval(t, a, b) for t in tree[1])
        if kind == "or":
            return rc._or3(self._eval(t, a, b) for t in tree[1])
        if kind not in ("scope", "type", "net"):
            return None
        it = a if tree[1] == "A" else b
        if kind == "scope":
            return self._scope(tree[2], tree[3], it)
        hit = self._type_eq(it, tree[3]) if kind == "type" else \
            rc._dru_str_eq(getattr(it, "net", None) or "", tree[3])
        return hit if hit is None or tree[2] == "==" else not hit

    def _barrier(self, x: str, y: str) -> float:
        x, y = x.lower(), y.lower()

        def hit(gs, n):
            return any(fnmatch.fnmatchcase(n, g.lower()) for g in gs)
        return max([b["clearance_mm"] for b in self.barriers
                    if (hit(b["a"], x) and hit(b["b"], y))
                    or (hit(b["a"], y) and hit(b["b"], x))] or [0.0])

    def _sig(self, it) -> tuple:
        kind = getattr(it, "kind", "")
        return (it.net or "", kind, self.courts_of(it),
                it.ref if kind == "pad" else "")

    def resolve(self, a, b, layer: str) -> tuple[float, str | None]:
        """(clearance, rule) DRC holds a and b to on layer, no margin; rule
        None when it is the net-wide value."""
        import route_critical as rc
        key = (self._sig(a), self._sig(b), layer)
        got = self._memo.get(key)
        if got is not None:
            return got
        old = self.old(a.net, b.net)
        win, maybe = None, []
        for mn, ly, tree, name in reversed(self.rows):
            if not _rule_layer_ok(ly, layer):
                continue
            h = rc._or3((self._eval(tree, a, b), self._eval(tree, b, a)))
            if h is True:
                win = (mn, name)
                break
            if h is None:
                maybe.append(mn)
        got = (old, None)
        if win is not None:
            v = max([win[0], self.floor, self._barrier(a.net or "",
                                                        b.net or "")] + maybe)
            if v < old - 1e-9:
                got = (v, win[1])
        self._memo[key] = got
        return got

    def value(self, a, b) -> tuple[float, str | None]:
        """The largest of resolve() over the layers a and b share (all
        either has when they share none)."""
        lys = (a.layers & b.layers) or (a.layers | b.layers)
        best = (-1.0, None)
        for ly in sorted(lys):
            v = self.resolve(a, b, ly)
            if v[0] > best[0]:
                best = v
        return best if best[0] >= 0 else (self.old(a.net, b.net), None)


def pair_rules(pcb: Path, dru_text: str, old, floor: float):
    """PairRules for the board, or None when its .kicad_dru has no
    clearance rule a pair or courtyard conditions (#682)."""
    import placelib
    import route_critical as rc
    rows, selectors = [], set()
    pairwise = False
    for r in rc.parse_dru_rules(dru_text):
        if r["constraint"] != "clearance":
            continue
        tree = rc.parse_dru_condition(r["condition"])
        terms = _dru_terms(tree, [])
        sides = {t[1] for t in terms if t[0] == "net"}
        scoped = [t for t in terms if t[0] == "scope"]
        pairwise |= bool(scoped) or sides == {"A", "B"}
        selectors |= {t[3] for t in scoped if t[2] != "member"}
        rows.append((r["min_mm"], r["layer"], tree, r["name"]))
    if not pairwise:
        return None
    model = placelib.PlaceModel.__new__(placelib.PlaceModel)
    model.footprints, model.dup_refs = {}, set()
    model._parse(pcb)
    fps = dict(model.footprints)
    fps.update(dict.fromkeys(model.dup_refs))
    courts = {}
    for ref, fp in fps.items():
        if fp is None or not any(
                rc.dru_selector_matches(s, ref, fp.fpid) is not False
                for s in selectors):
            continue
        for s in (0, 1):
            g = fp.courtyard_exact[s]
            if g is not None and not fp.courtyard_unreadable[s]:
                courts[ref, s] = shapely.affinity.translate(
                    shapely.affinity.rotate(g, -fp.angle, origin=(0, 0)),
                    *fp.pos)
    return PairRules(rows, fps, courts, floor,
                     routelib.dru_barriers(dru_text), old)


# ---------------------------------------------------------------- islands

def net_islands(bg: geom.BoardGeom, net: str) -> list[dict]:
    """The net's copper as islands: [{layer: [Polygon, ...]}], polygons on
    different layers joined when a via or plated hole of the net touches
    both."""
    polys: list[tuple[str, object]] = []
    for layer in bg.copper_layers:
        g = bg.net_copper(net, layer)
        if g.is_empty:
            continue
        for p in getattr(g, "geoms", [g]):
            if not p.is_empty:
                polys.append((layer, p))
    parent = list(range(len(polys)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    links = [Point(v.at) for v in bg.vias_of(net=net)]
    links += [Point(p.center) for p in bg.pads_of(net=net) if p.drill]
    for pt in links:
        hit = [i for i, (_, p) in enumerate(polys) if p.distance(pt) < 1e-3]
        for i in hit[1:]:
            parent[find(i)] = find(hit[0])
    groups: dict[int, dict] = {}
    for i, (layer, p) in enumerate(polys):
        groups.setdefault(find(i), {}).setdefault(layer, []).append(p)
    return list(groups.values())


_LAYER_RE = re.compile(r" on ([A-Za-z0-9]+\.Cu)\b")


def pick_island(islands: list[dict], pos, msg: str = "") -> int | None:
    """The island a DRC item names: nearest to its position, on the layer
    the message names when it names one (a zone is reported by a corner)."""
    if not pos or not islands:
        return None
    pt = Point(pos)
    m = _LAYER_RE.search(msg or "")
    hint = m.group(1) if m and "F.Cu - B.Cu" not in (msg or "") else None
    best, best_d = None, float("inf")
    for i, isl in enumerate(islands):
        for layer, ps in isl.items():
            if hint and layer != hint:
                continue
            d = min(p.distance(pt) for p in ps)
            if d < best_d:
                best, best_d = i, d
    if best is None and hint:
        return pick_island(islands, pos, "")
    return best


# -------------------------------------------------------------- obstacles

@dataclass
class Item:
    net: str
    layers: frozenset
    shape: object          # centreline / point / polygon
    half: float            # copper beyond the shape (track/via radius)
    hole: object = None    # drill extent polygon, or None
    conn: int = -1         # route_finish connection that added it
    uuid: str | None = None  # a board track or via route_finish may rip
    tied: frozenset = frozenset()  # nets this copper may touch (a net tie's)
    smd: bool = False      # an SMD pad: no via of its own net inside it
    ref: str = ""          # a pad's footprint ref
    number: str = ""       # and its number
    locked: bool = False   # (locked yes) board copper: a wall, never ripped
    kind: str = ""         # pad, via, segment (a board track or arc), track
    #   (one routed here), "" (footprint copper): what a rule's A.Type
    #   reads (#682)


class Obstacles:
    """The board's copper and holes, indexed for windowed rasterising.
    spokes=True: spokes_near() also gives the thermal spokes a route must
    leave (#1140); off, it gives none, as before. links=True (the default):
    it also gives the bands where a pour alone joins a via or pad to the
    rest of its net (pour_links, #1311)."""

    def __init__(self, bg: geom.BoardGeom, *, spokes: bool = False,
                 links: bool = True):
        self.bg = bg
        self.spokes = spokes
        self.links = links
        self._link_tree = None
        # pockets a found path walled off with a pour link, kept clear on
        # the next search (pour_cuts): (zone net, layer, shape, clearance)
        self.guards: list = []
        # #682: items a pair or courtyard rule let a route pass closer than
        # the net-wide value (id -> item), checked exactly on a found path;
        # those it missed against, read stricter on the next search (id ->
        # 1: a track meets a courtyard only where its copper does, 2: the
        # net-wide value)
        self.pair_soft: dict = {}
        self.pair_strict: dict = {}
        self._spoke_pads = None      # (pad, zone, layer, gap, width, min)
        self._spoke_tree = None
        self._spoke_live: dict = {}  # index into _spoke_pads -> live spokes
        self._net_cu: dict = {}      # (net, layer) -> its copper (_net_copper)
        cu = frozenset(bg.copper_layers)
        items: list[Item] = []
        for t in bg.tracks_of():
            items.append(Item(t.net or "", frozenset({t.layer}), t.shape,
                              t.width / 2, uuid=t.uuid, locked=t.locked,
                              kind="segment"))
        for v in bg.vias_of():
            items.append(Item(v.net or "", frozenset(v.layers) or cu,
                              Point(v.at), v.diameter / 2,
                              Point(v.at).buffer(v.drill / 2, quad_segs=4),
                              uuid=v.uuid, locked=v.locked, kind="via"))
        for p in bg.pads_of():
            dp = p.drill_poly if p.drill else None
            items.append(Item(p.net or "", frozenset(p.layers) & cu, p.poly,
                              0.0, dp if dp is not None and not dp.is_empty
                              else None, smd=p.kind == "smd", ref=p.ref,
                              number=p.number, kind="pad"))
        # DRC sees this copper as netless, so a track through it is a short
        for c in bg.fp_copper:
            items.append(Item("", frozenset({c["layer"]}), c["shape"], 0.0,
                              tied=c["nets"]))
        self.items = items
        self.tree = STRtree([shapely.envelope(
            it.shape if it.hole is None else shapely.union(it.shape, it.hole))
            for it in items])
        self.added: list[Item] = []
        self.hidden: set = set()        # board copper ripped this pass
        self.soft_board = None   # set of nets that stay walls: every other
        #                          net's board tracks/vias cost, not block
        self.fills = [(z.net, layer, p) for z in bg.zones_of()
                      for layer, ps in z.fills.items() for p in ps]
        # beside fills: a teardrop's, which meets no via for KiCad (#1140)
        self.fill_teardrop = [bool(z.teardrop) for z in bg.zones_of()
                              for ps in z.fills.values() for _ in ps]
        self.fill_tree = STRtree([p for _, _, p in self.fills]) \
            if self.fills else None

    def near(self, win):
        out = [self.items[i] for i in self.tree.query(win)
               if self.items[i].uuid not in self.hidden]

        out += [it for it in self.added if it.shape.intersects(win)
                or it.shape.distance(win) < 2.0]
        return out

    def fills_near(self, win):
        if self.fill_tree is None:
            return []
        return [self.fills[i] for i in self.fill_tree.query(win)]

    def _thermal_index(self):
        if self._spoke_pads is None:
            self._spoke_pads = thermal_pads(self.bg)
            self._spoke_tree = STRtree([
                box(*p.poly.buffer(gap + mt + sw).bounds)
                for p, _, _, gap, sw, mt in self._spoke_pads]) \
                if self._spoke_pads else None
        return self._spoke_pads

    def _net_copper(self, net: str, layer: str):
        """(shapes, owners, STRtree) of `net`'s copper on `layer` as KiCad's
        connectivity meets a zone with it: a pad or via by its copper, a
        track by its ends, another fill by its polygon. Owners are ("pad",
        ref, number), ("via" or "track", uuid) or ("zone", id, index).
        Built on first use."""
        key = (net, layer)
        if key not in self._net_cu:
            shapes, owners = [], []
            for p in self.bg.pads_of():
                if (p.net or "") == net and layer in p.layers:
                    shapes.append(p.poly)
                    owners.append(("pad", p.ref, p.number))
            for v in self.bg.vias_of():
                if (v.net or "") == net and (not v.layers
                                             or v.spans(layer)):
                    shapes.append(v.poly)
                    owners.append(("via", v.uuid))
            for t in self.bg.tracks_of():
                if (t.net or "") == net and t.layer == layer:
                    c = list(t.shape.coords)
                    shapes.append(shapely.multipoints([c[0], c[-1]]))
                    owners.append(("track", t.uuid))
            for z in self.bg.zones_of():
                if (z.net or "") == net and not z.teardrop:
                    for i, f in enumerate(z.fills.get(layer, ())):
                        shapes.append(f)
                        owners.append(("zone", id(z), i))
            self._net_cu[key] = (shapes, owners,
                                 STRtree(shapes) if shapes else None)
        return self._net_cu[key]

    def _joined(self, pad, zone, layer: str):
        """A test of whether a fill polygon of `zone` on `layer` meets copper
        of its net other than `pad` and itself: KiCad's DRC does not count
        spokes into an island that joins only the pad
        (m_SingleConnectionOutlines, connectivity_algo.cpp)."""
        shapes, owners, tree = self._net_copper(zone.net or "", layer)

        def joined(poly) -> bool:
            if tree is None:
                return False
            for i in tree.query(poly, predicate="intersects"):
                o = owners[int(i)]
                if o == ("pad", pad.ref, pad.number):
                    continue
                if o[0] == "zone" and shapes[int(i)].equals(poly):
                    continue
                return True
            return False
        return joined

    def _live(self, i: int) -> list:
        if i not in self._spoke_live:
            p, z, layer, gap, sw, mt = self._spoke_pads[i]
            self._spoke_live[i] = thermal_spokes(
                p, z.fills.get(layer, ()), gap, sw, mt,
                joined=self._joined(p, z, layer))
        return self._spoke_live[i]

    def spokes_near(self, win) -> list:
        """(zone net, layer, keep-out, zone clearance) of the pour links near
        win (links_near), then of the spokes kept for each thermal pad near
        win: its best SPOKES_MIN live ones in the
        board's fill (thermal_spokes). Other-net copper held its clearance
        off them leaves the refill those spokes, so a route does not starve
        a thermal pad (C221 on the voice board). Built per pad on first use.
        No spokes unless the Obstacles were made with spokes=True."""
        out = self.links_near(win)
        if not self.spokes or not self._thermal_index():
            return out
        for i in self._spoke_tree.query(win):
            p, z, layer = self._spoke_pads[int(i)][:3]
            out += [(z.net, layer, shape, z.clearance)
                    for _, shape in self._live(int(i))[:SPOKES_MIN]]
        return out

    def links_near(self, win) -> list:
        """(zone net, layer, band, zone clearance) of each pour link near
        win (pour_links): where a pour's fill alone joins a via or pad to
        the rest of its net. [] unless the Obstacles were made with
        links=True."""
        if not self.links:
            return []
        out = [self.link(int(i)) for i in self.link_index().query(win)]
        return out + [g for g in self.guards if g[2].intersects(win)]

    def link_index(self):
        """STRtree of the pour links' bands (pour_links), built on first
        use."""
        if self._link_tree is None:
            self._links = pour_links(self.bg)
            self._link_tree = STRtree([b for _, _, b, _, _ in self._links])
        return self._link_tree

    def link(self, i: int) -> tuple:
        return self._links[i][:4]

    def spoke_count(self, i: int) -> int:
        """The spokes KiCad's DRC counts for thermal pad i (zones/zc.cpp):
        its live spokes into fill that meets other copper of the net, then,
        short of SPOKES_MIN, each net track from inside the pad's ring to
        the fill; only when that gives none, its spokes into an island only
        it meets. -1 when no fill reaches it, which DRC leaves to its
        connectivity check."""
        p, z, layer, gap, *_ = self._spoke_pads[i]
        live = self._live(i)
        if not live:
            return -1
        n = sum(1 for (tied, *_), _ in live if tied)
        if n < SPOKES_MIN:
            ring = p.poly.buffer(gap / 2)
            fills = z.fills.get(layer, ())
            anchor = Point(p.copper_center)
            for t in self.bg.tracks_of():
                if (t.net or "") != (p.net or "") or t.layer != layer:
                    continue
                c = list(t.shape.coords)
                a, b = Point(c[0]), Point(c[-1])
                # only a track joined to the pad itself: an end in its
                # copper or its anchor on the track (GetConnectedTracks)
                if not (p.poly.intersects(a) or p.poly.intersects(b)
                        or t.shape.distance(anchor) <= t.width / 2):
                    continue
                for x, y in ((a, b), (b, a)):
                    if ring.contains(x) and any(f.intersects(y)
                                                for f in fills):
                        n += 1
                        break
        return n if n else len(live)

    def short_spokes(self, refs) -> list:
        """(ref, pad number, layer, spokes) of refs' thermal pads that
        KiCad's DRC finds joined by fewer spokes than it asks (spoke_count:
        between none and SPOKES_MIN; with none the pad is not joined there
        at all, which DRC leaves to its connectivity check). No route mends
        that: the fill around the pad is what starves it."""
        refs = set(refs)
        out = []
        for i, (p, z, layer, *_) in enumerate(self._thermal_index()):
            if p.ref in refs:
                n = self.spoke_count(i)
                if 0 < n < SPOKES_MIN:
                    out.append((p.ref, p.number, layer, n))
        return out


def zone_join(pad, zone) -> int:
    """How `zone` joins `pad` (a pad of its net), as geom.ZONE_CONNECT_*:
    the pad's or its footprint's own setting, else the zone's; a
    through-hole-only zone is thermal to a through-hole pad and solid to
    others. A .kicad_dru zone_connection rule is not read."""
    c = pad.zone_connect if pad.zone_connect != geom.ZONE_CONNECT_INHERIT \
        else zone.connect
    if c == geom.ZONE_CONNECT_THT_THERMAL:
        c = geom.ZONE_CONNECT_THERMAL if pad.kind == "thru_hole" \
            else geom.ZONE_CONNECT_SOLID
    return c


def pour_nets(bg: geom.BoardGeom) -> set:
    """The nets with a pour: a zone of the net that is not a teardrop. KiCad
    builds a teardrop zone per teardropped track end, so counting those made
    nearly every signal net of a teardropped board a pour net that no router
    would rip (#1140)."""
    return {z.net for z in bg.zones_of() if z.net and not z.teardrop}


def thermal_pads(bg: geom.BoardGeom) -> list:
    """(pad, zone, layer, thermal gap, spoke width, zone min thickness) for
    each pad a zone of its net joins by thermal spokes on a layer the zone
    has fill on. A teardrop zone is not a pour and joins nothing."""
    zones: dict = {}
    for z in bg.zones_of():
        if z.net and not z.teardrop and z.filled:
            zones.setdefault(z.net, []).append(z)
    gap0, sw0, mt0 = SPOKE_DEFAULTS
    out = []
    for p in bg.pads_of():
        for z in zones.get(p.net or "", ()):
            if zone_join(p, z) != geom.ZONE_CONNECT_THERMAL:
                continue
            gap = p.thermal_gap or z.thermal_gap or gap0
            sw = p.spoke_width or z.spoke_width or sw0
            mt = z.min_thickness or mt0
            out += [(p, z, layer, gap, sw, mt) for layer in p.layers
                    if z.fills.get(layer)]
    return out


LINK_TOL = 1e-3               # copper this near is joined (_net_graph)


def _net_graph(bg: geom.BoardGeom, net: str) -> tuple[list, list]:
    """`net`'s copper as a graph: nodes (kind, owner, layers, shape, zone),
    kind "pad", "via", "track" or "fill" (a fill polygon of one layer;
    zone None for the others), and edges (node, node, layer), two nodes
    whose copper meets on a layer they share. Close to KiCad's
    connectivity: a track joins by any of its copper, not only its ends."""
    cu = tuple(bg.copper_layers)
    nodes = []
    for p in bg.pads_of(net=net):
        nodes.append(("pad", (p.ref, p.number), set(p.layers) & set(cu),
                      p.poly, None))
    for v in bg.vias_of(net=net):
        nodes.append(("via", v.uuid, set(v.layers or cu), v.poly, None))
    for t in bg.tracks_of(net=net):
        nodes.append(("track", t.uuid, {t.layer}, t.poly, None))
    for z in bg.zones_of(net):
        for layer, ps in z.fills.items():
            for p in ps:
                nodes.append(("fill", id(z), {layer}, p, z))
    edges = []
    for layer in cu:
        on = [i for i, n in enumerate(nodes) if layer in n[2]]
        if len(on) < 2:
            continue
        tree = STRtree([nodes[i][3] for i in on])
        a, b = tree.query([nodes[i][3] for i in on], predicate="dwithin",
                          distance=LINK_TOL)
        edges += [(on[i], on[j], layer) for i, j in zip(a, b) if i < j]
    return nodes, edges


def _bridges(n: int, edges: list, weight: list) -> list:
    """(edge index, weight on the far side, weight of its component) of each
    bridge of the graph (Tarjan, iterative): the edge whose loss splits its
    component, the far side being the DFS child's subtree."""
    adj: list[list] = [[] for _ in range(n)]
    for e, (a, b, _) in enumerate(edges):
        adj[a].append((b, e))
        adj[b].append((a, e))
    tin = [-1] * n
    low = [0] * n
    sub = list(weight)
    out, timer = [], 0
    for root in range(n):
        if tin[root] != -1:
            continue
        found = []
        tin[root] = low[root] = timer
        timer += 1
        stack = [(root, -1, iter(adj[root]))]
        while stack:
            v, pe, it = stack[-1]
            for w, e in it:
                if e == pe:
                    continue
                if tin[w] == -1:
                    tin[w] = low[w] = timer
                    timer += 1
                    stack.append((w, e, iter(adj[w])))
                    break
                low[v] = min(low[v], tin[w])
            else:
                stack.pop()
                if stack:
                    u = stack[-1][0]
                    low[u] = min(low[u], low[v])
                    sub[u] += sub[v]
                    if low[v] > tin[u]:
                        found.append((pe, v))
        out += [(e, sub[v], sub[root]) for e, v in found]
    return out


def pour_links(bg: geom.BoardGeom) -> list:
    """(net, layer, band, zone clearance, owner) for each via, and each pad
    a pour joins solid, whose join to a fill polygon of its pour is all
    that keeps pads of its net together: an edge of the net's copper graph
    (_net_graph) whose loss leaves pads on both sides (#1311; U202.1's via
    to the In3.Cu +0V75 pour on the voice board). band is that fill within
    the zone's min thickness of the item's copper: other nets' copper held
    the zone's clearance off it leaves the refill the join, as with a
    thermal pad's spokes. owner is ("via", uuid) or ("pad", ref, number).
    A thermal pad is left to its spokes (Obstacles.spokes_near). Built once
    per board: its copper is not changed after parsing."""
    cache = bg.__dict__.setdefault("_pour_links", {})
    if "all" in cache:
        return cache["all"]
    out, detail = [], []
    for net in sorted(pour_nets(bg)):
        nodes, edges = _net_graph(bg, net)
        pads = [1 if nd[0] == "pad" else 0 for nd in nodes]
        adj: dict = {}
        for a, b, layer in edges:
            adj.setdefault((a, layer), []).append(b)
            adj.setdefault((b, layer), []).append(a)
        for e, far, total in _bridges(len(nodes), edges, pads):
            if not 0 < far < total:
                continue
            a, b, layer = edges[e]
            if nodes[a][0] == "fill":
                a, b = b, a
            item, fill = nodes[a], nodes[b]
            z = fill[4]
            if fill[0] != "fill" or z.teardrop or item[0] not in (
                    "via", "pad"):
                continue
            if item[0] == "pad":
                p = next(q for q in bg.pads_of(net=net)
                         if (q.ref, q.number) == item[1])
                if zone_join(p, z) != geom.ZONE_CONNECT_SOLID:
                    continue
            mt = z.min_thickness or SPOKE_DEFAULTS[2]
            band = fill[3].intersection(item[3].buffer(mt + LINK_TOL,
                                                       quad_segs=8))
            if band.is_empty:
                continue
            out.append((net, layer, band, z.clearance,
                        (item[0],) + (item[1] if item[0] == "pad"
                                      else (item[1],))))
            # what pour_cuts needs: the item's copper, the zone, the fill's
            # other copper on the layer (not more of the zone's fill), the
            # min thickness
            detail.append((item[3], z, [
                nodes[k][3] for k in adj.get((b, layer), ())
                if k != a and nodes[k][4] is not z], mt))
    cache["all"] = out
    cache["detail"] = detail
    return out


LINK_ROUNDS = 4          # searches again round a pocket a path walls off
LINK_WINDOW_MM = 3.0     # how far round a pour link pour_cuts looks


def pour_cuts(obs: Obstacles, ops: list, net: str, rules: Rules) -> list:
    """(owner, layer, pocket, zone net, zone clearance) of each pour link
    (pour_links) of another net that the copper of `ops` would cut from
    the rest of its pour (#1311). Predicts the refill round the link: the
    fill less the new copper, held max(zone clearance, rule clearance) off,
    opened by the zone's min thickness; the link is cut when no piece of
    that fill both meets the item and reaches the fill's other copper or
    the edge of the LINK_WINDOW_MM window, past which the fill goes on.
    pocket is what a search again keeps the clearance off: the fill the new
    copper walls in with the item, or, when it only thins the fill to
    nothing, the band and what is left of the fill at the item. [] with
    links off."""
    if not obs.links or not ops:
        return []
    cu: dict = {}
    for it in ops_items(ops, 0, obs.bg.copper_layers):
        shape = it.shape.buffer(it.half, quad_segs=8)
        for layer in it.layers:
            cu.setdefault(layer, []).append(shape)
    if not any(cu.values()):
        return []
    cu = {k: shapely.union_all(v) for k, v in cu.items()}
    tree = obs.link_index()
    detail = obs.bg.__dict__["_pour_links"]["detail"]
    reach = shapely.union_all(list(cu.values())).buffer(LINK_WINDOW_MM)
    out = []
    for i in sorted(int(i) for i in tree.query(reach, predicate="intersects")):
        znet, layer, band, zclr, owner = obs._links[i]
        if znet == net or layer not in cu:
            continue
        item, z, others, mt = detail[i]
        fill = _zone_fill(obs.bg, z, layer)
        x0, y0, x1, y1 = band.bounds
        win = box(x0 - LINK_WINDOW_MM, y0 - LINK_WINDOW_MM,
                  x1 + LINK_WINDOW_MM, y1 + LINK_WINDOW_MM)
        copper = cu[layer].intersection(win)
        if copper.is_empty:
            continue
        c = max(zclr or 0.0, rules.clr(net, znet))
        f = fill.intersection(win)
        left = f.difference(copper.buffer(c, quad_segs=8)).buffer(
            -mt / 2 + LINK_TOL, quad_segs=8).buffer(mt / 2 - LINK_TOL,
                                                     quad_segs=8)
        edge = win.exterior
        if fill.difference(win).is_empty:
            edge = None                    # the fill ends inside the window
        outer = [o.intersection(win) for o in others]
        outer = [o for o in outer if not o.is_empty]

        def joined(piece):
            return (edge is not None and piece.distance(edge) <= LINK_TOL) \
                or any(piece.distance(o) <= LINK_TOL for o in outer)

        pieces = [g for g in getattr(left, "geoms", [left])
                  if not g.is_empty and g.distance(item) <= LINK_TOL]
        if any(joined(g) for g in pieces):
            continue
        walled = f.difference(copper)
        walled = [g for g in getattr(walled, "geoms", [walled])
                  if not g.is_empty and g.distance(item) <= LINK_TOL]
        pocket = [g for g in walled if not joined(g)]
        pocket = shapely.union_all(pocket or [band.buffer(mt)] + pieces)
        out.append((owner, layer, pocket, znet, zclr))
    return out


def _zone_fill(bg: geom.BoardGeom, z, layer: str):
    """z's fill on layer as one shape, made once per board."""
    cache = bg.__dict__["_pour_links"].setdefault("fill", {})
    if (id(z), layer) not in cache:
        cache[(id(z), layer)] = shapely.union_all(z.fills.get(layer, []))
    return cache[(id(z), layer)]


def cut_note(cuts: list) -> str:
    """pour_cuts' links, one line."""
    return "; ".join(
        f"cuts the {layer} {znet} pour off "
        + (f"via {o[1]}" if o[0] == "via" else f"pad {o[1]}.{o[2]}")
        for o, layer, _, znet, _ in cuts)


def thermal_spokes(pad, fills, gap: float, sw: float, mt: float,
                   joined=None, mids: bool = False) -> list:
    """The live thermal spokes of `pad` in `fills` (one zone's polygons on
    one layer), best first, as (score, keep-out). A spoke is where a fill
    polygon crosses the pad outline grown by half the thermal gap, as
    KiCad's DRC counts them for DRCE_STARVED_THERMAL (zones/zc.cpp). Its
    keep-out is a bar the spoke's width from the pad edge to where KiCad's
    refill ends the spoke and tests it for fill (spoke_end), with a disc
    there as wide as the zone's min thickness: the refill keeps a spoke
    whose end lands in fill that width allows. joined(poly) says whether
    other copper of the net meets that polygon (default: all do); DRC does
    not count a spoke into an island only the pad meets. Best is joined,
    then in the largest polygon, then with the most fill within 1 mm of
    its end. mids=True: (score, keep-out, (x, y)), the last where the
    spoke crosses the ring (its middle there)."""
    sw = min(sw, min(pad.size)) if min(pad.size) > 0 else sw
    edge = pad.poly.buffer(gap / 2).exterior
    out = []
    for poly in fills:
        if not poly.intersects(edge):
            continue
        lines = [g for g in shapely.get_parts(poly.intersection(edge))
                 if g.geom_type == "LineString" and g.length > 1e-6]
        if not lines:
            continue
        merged = shapely.line_merge(shapely.multilinestrings(lines))
        tied = joined(poly) if joined is not None else True
        for piece in shapely.get_parts(merged):
            if piece.is_ring:          # fill all round the ring: no spoke
                continue
            m = piece.interpolate(0.5, normalized=True)
            end = spoke_end(pad, (m.x, m.y), gap, mt)
            if end is None:
                continue
            ux, uy = end[1]
            p_in = (m.x - ux * gap / 2, m.y - uy * gap / 2)
            p_out = end[0]
            if (p_out[0] - p_in[0]) * ux + (p_out[1] - p_in[1]) * uy < 1e-6:
                p_out = (m.x + ux * (gap + mt) / 2, m.y + uy * (gap + mt) / 2)
            shape = shapely.union(
                LineString([p_in, p_out]).buffer(sw / 2, cap_style="flat"),
                Point(p_out).buffer(max(mt, sw) / 2))
            near = poly.intersection(Point(p_out).buffer(1.0)).area
            out.append(((bool(tied), round(poly.area, 6), round(near, 6)),
                        shape) + (((m.x, m.y),) if mids else ()))
    out.sort(key=lambda f: f[0], reverse=True)
    return out


SPOKE_EPS = 0.005    # KiCad's max error, which its refill grows the box by


def spoke_end(pad, through, gap: float, mt: float):
    """((x, y), (ux, uy)): where KiCad's refill ends the thermal spoke of
    `pad` that crosses its thermal ring at `through`, and the spoke's
    direction; None at the spoke's origin. zone_filler.cpp runs a spoke
    from the centre of the pad's own-frame bounding box (its copper box,
    unturned) to that box grown by the thermal gap and half the zone's min
    thickness, and keeps it if that end is in fill; a round pad's box is
    met on its axis whatever the spoke's angle. A custom pad's spokes along
    its own segments (KiCad's proxy spokes) are not modelled."""
    if pad.custom is not None and not pad.custom.is_empty:
        x0, y0, x1, y1 = pad.custom.bounds
    else:
        w, h = pad.size
        x0, y0, x1, y1 = -w / 2, -h / 2, w / 2, h / 2
    a = math.radians(pad.angle)
    ca, sa = math.cos(a), math.sin(a)
    # the pad's frame turns to the board's by -angle (geom._pad_polygon)
    lx, ly = (x0 + x1) / 2, (y0 + y1) / 2
    cx0, cy0 = pad.copper_center
    ox, oy = cx0 + lx * ca + ly * sa, cy0 - lx * sa + ly * ca
    dx, dy = through[0] - ox, through[1] - oy
    d = math.hypot(dx, dy)
    if d < 1e-9:
        return None
    ux, uy = dx / d, dy / d
    vx, vy = ux * ca - uy * sa, ux * sa + uy * ca      # in the pad's frame
    grow = gap + SPOKE_EPS + mt / 2
    hx, hy = (x1 - x0) / 2 + grow, (y1 - y0) / 2 + grow
    round_pad = pad.shape == "circle" or (
        pad.shape == "oval" and abs(pad.size[0] - pad.size[1]) < 1e-9)
    if round_pad and pad.custom is None:
        reach = hx
    else:
        reach = min(hx / abs(vx) if abs(vx) > 1e-12 else math.inf,
                    hy / abs(vy) if abs(vy) > 1e-12 else math.inf)
    return ((ox + ux * reach, oy + uy * reach), (ux, uy))


# ------------------------------------------------------------------- grid

@dataclass
class Grid:
    x0: float
    y0: float
    nx: int
    ny: int
    g: float
    layers: list            # routable layer names, index = k
    blk: list               # per k: bytes, 1 = no track centre here
    vblk: bytes             # 1 = no via centre here
    via: tuple              # (size, drill) the via mask was built for
    cost: list              # per k: list of float step multipliers
    in_a: list              # per k: bytes, cell inside side A's copper
    in_b: list
    a_plane: bytes          # cell inside side A's copper on a plane layer
    b_plane: bytes
    # #477, per k: (k2, span, its via mask, the k its via must find clear)
    span_moves: list = field(default_factory=list)
    gave_up: bool = False   # astar stopped at max_nodes, not out of cells
    neck: dict = field(default_factory=dict)   # (k, cell) -> the narrower
    #                          width a track there is cleared for (#924)

    def xy(self, i: int, j: int) -> tuple[float, float]:
        return (round(self.x0 + i * self.g, 4), round(self.y0 + j * self.g, 4))


def _mask(shape, r, X, Y, x0, y0, g, nx, ny):
    """Bool mask of grid points within r of shape (sub-window only). Its points
    are made here: all of a whole-board grid's as shapely objects is ~6 GB."""
    b = shape.bounds
    i0 = max(0, int(math.floor((b[0] - r - x0) / g)))
    i1 = min(nx - 1, int(math.ceil((b[2] + r - x0) / g)))
    j0 = max(0, int(math.floor((b[1] - r - y0) / g)))
    j1 = min(ny - 1, int(math.ceil((b[3] + r - y0) / g)))
    if i0 > i1 or j0 > j1:
        return None
    at = (slice(j0, j1 + 1), slice(i0, i1 + 1))
    return at, shapely.dwithin(shape, shapely.points(X[at], Y[at]), r)


def _on_board(window, outline, slack: float = 1.0):
    """window cut to the board and a little past it, for a pad over the edge.
    Nothing routes off the board, and a grid over empty space costs memory by
    the square of --margin: 200 mm on a 110 mm board was 9 GB, not 0.5."""
    if outline is None or outline.is_empty:
        return window
    bx0, by0, bx1, by1 = outline.bounds
    minx, miny, maxx, maxy = window
    cut = (max(minx, bx0 - slack), max(miny, by0 - slack),
           min(maxx, bx1 + slack), min(maxy, by1 + slack))
    return cut if cut[0] < cut[2] and cut[1] < cut[3] else window


def _pair_split(obs: Obstacles, rules: Rules, net: str, it: Item,
                kind: str, layers, reach: float) -> tuple | None:
    """For a route of net past item it (#682): (courts, values), values[code]
    the clearance (margin in) a kind ("track" or "via") of copper holds to
    it when it meets the courts code's bits name, or None when the net-wide
    value holds wherever it runs."""
    pr = rules.pair
    if pr is None:
        return None
    old = rules.clr(net, it.net)
    if obs.pair_strict.get(id(it), 0) >= 2:
        return None
    cand = pr.near(it.shape, reach)
    if len(cand) > PAIR_COURTS_MAX:
        return None
    lys = frozenset(layers)
    vals = []
    for code in range(1 << len(cand)):
        S = [cand[j] for j in range(len(cand)) if code >> j & 1]
        vals.append(min(old, rules.item_clr(Probe(net, kind, S, lys), it)))
    if min(vals) >= old - 1e-9:
        return None
    obs.pair_soft[id(it)] = it
    return cand, vals


def pair_misses(obs: Obstacles, rules: Rules, ops: list, net: str) -> list:
    """The items of obs.pair_soft a path's copper (ops) comes closer to
    than the rule DRC reads for the pair (#682), as ids: the grid read a
    track's courtyards per cell, DRC reads the whole segment."""
    pr = rules.pair
    if pr is None or not obs.pair_soft:
        return []
    mine = [(op, Probe(net, op.kind, pr.courts_of(op, keep=False), op.layers))
            for op in ops_items(ops, -1, obs.bg.copper_layers)]
    out = []
    for i, it in obs.pair_soft.items():
        for op, probe in mine:
            if not op.layers & it.layers:
                continue
            gap = op.shape.distance(it.shape) - op.half - it.half
            if gap < rules.net_clr(net, it.net) and gap < (
                    pr.value(probe, it)[0] - 1e-4):
                out.append(i)
                break
    return out


def _pair_mask(pr, cand, vals, it_shape, rad, r_in, X, Y, args):
    """_mask with a radius per grid point: rad(values[code]), code the
    courts (of cand) within r_in of the point."""
    m = _mask(it_shape, max(rad(v) for v in vals), *args)
    if not m or len(set(vals)) == 1 or not m[1].any():
        return m
    at, hit = m
    sub = np.nonzero(hit)
    pts = shapely.points(X[at][sub], Y[at][sub])
    code = np.zeros(len(pts), int)
    for j, key in enumerate(cand):
        code |= (shapely.distance(pr.courts[key], pts) <= r_in).astype(int) << j
    r = np.array([rad(v) for v in vals])[code]
    out = np.zeros_like(hit)
    out[sub] = shapely.distance(it_shape, pts) <= r
    return at, out


def build_grid(obs: Obstacles, rules: Rules, net: str, side_a: dict,
               side_b: dict, layers: list, window, g: float,
               pour_cost: float = 3.0, inner_cost: float = 2.0,
               width: float | None = None, via=None,
               rip_cost: float = 40.0, necks: bool = True) -> Grid:
    """The search grid of one connection. Where the net has a neck zone
    (Rules.necks) a cell is cleared for the neck width, and path_to_ops lays
    the neck width there (#924). necks=False: the full width everywhere.
    The grid's layers are `layers` less those a DRU disallow rule keeps the
    net's tracks off (#1140; Grid.layers): a through via still crosses
    them, held off their other-net copper by vblk; a side on one is reached
    by a via into it, as a plane's is. A via kind the DRU disallows the
    net (Rules.via_bans) is left out. CheckError when no layer is left."""
    bg = obs.bg
    allowed = rules.net_layers(net, layers)
    if not allowed:
        raise CheckError(f"{net}: the board's .kicad_dru keeps its tracks"
                         f" off every layer asked for ({', '.join(layers)})")
    layers = allowed
    minx, miny, maxx, maxy = _on_board(window, bg.outline)
    x0 = math.floor(minx / g) * g
    y0 = math.floor(miny / g) * g
    nx = int(math.ceil((maxx - x0) / g)) + 1
    ny = int(math.ceil((maxy - y0) / g)) + 1
    xs = x0 + np.arange(nx) * g
    ys = y0 + np.arange(ny) * g
    X, Y = np.meshgrid(xs, ys)
    win = box(x0 - 1, y0 - 1, x0 + nx * g + 1, y0 + ny * g + 1)
    w = rules.width(net) if width is None else width
    via_size, via_drill = via or (rules.via_size, rules.via_drill)
    vr = via_size / 2
    K = len(layers)
    blk = [np.zeros((ny, nx), bool) for _ in range(K)]
    vblk = np.zeros((ny, nx), bool)
    cost = [np.full((ny, nx), inner_cost if layer not in (
        bg.copper_layers[0], bg.copper_layers[-1]) else 1.0) for layer in layers]
    args = (X, Y, x0, y0, g, nx, ny)

    soft = obs.soft_board
    pr = rules.pair
    obs.pair_soft = {}
    vh = via_drill / 2 + rules.hole_clearance + rules.margin

    def track_mask(it, on):
        # a pair or courtyard rule may hold it closer to some cells (#682);
        # a track meets a courtyard where any of it does, so a cell this
        # near one reads as in it, and the found path is checked exactly
        c = rules.clr(net, it.net)
        sp = _pair_split(obs, rules, net, it, "track",
                         [layers[k] for k in on], it.half + w + 2 * c) \
            if on else None
        if sp is None:
            return _mask(it.shape, it.half + w / 2 + c, *args)
        r_in = w / 2 + (0 if obs.pair_strict.get(id(it)) else c)
        return _pair_mask(pr, sp[0], sp[1], it.shape,
                          lambda v: it.half + w / 2 + v, r_in, X, Y, args)

    def via_mask(it):
        c = rules.clr(net, it.net)
        sp = _pair_split(obs, rules, net, it, "via", it.layers,
                         it.half + 2 * vr + c)
        if sp is None:
            return _mask(it.shape, it.half + max(vr + c, vh), *args)
        return _pair_mask(pr, sp[0], sp[1], it.shape,
                          lambda v: it.half + max(vr + v, vh), vr, X, Y, args)

    for it in obs.near(win):
        other = it.net != net and net not in it.tied
        if other and soft is not None and it.uuid and it.net not in soft \
                and not it.locked:
            # crossing it means ripping it: passable, at a price
            on = [k for k, layer in enumerate(layers) if layer in it.layers]
            m = track_mask(it, on)
            if m:
                for k in on:
                    cost[k][m[0]][m[1]] += rip_cost
            other = False   # its copper is passable; its hole still counts
        if other:
            on = [k for k, layer in enumerate(layers) if layer in it.layers]
            if on:
                m = track_mask(it, on)
                if m:
                    for k in on:
                        blk[k][m[0]] |= m[1]
            if it.layers:
                # the via's ring keeps the clearance, its hole the hole
                # clearance, from other-net copper
                m = via_mask(it)
                if m:
                    vblk[m[0]] |= m[1]
        if it.smd and not other and not rules.pad_vias \
                and it.ref not in rules.via_in_pad_refs:
            # no via in its own net's SMD pad: the open barrel wicks the
            # joint's solder (check_wicking wick_via_in_pad), and via-in-pad
            # is a paid fab option a route may not take on its own; only
            # --via-in-pad-refs parts take one (fill them after: route_edit
            # fill_vias). The keep is from the pad's mask opening (#636).
            m = _mask(it.shape, rules.pad_via_keep(it.ref, it.number, vr,
                                                   via_drill), *args)
            if m:
                vblk[m[0]] |= m[1]
        if it.hole is not None:
            if other:
                m = _mask(it.hole, rules.hole_clearance + w / 2 + rules.margin,
                          *args)
                if m:
                    for k in range(K):
                        blk[k][m[0]] |= m[1]
                m = _mask(it.hole, rules.hole_clearance + vr + rules.margin,
                          *args)
                if m:
                    vblk[m[0]] |= m[1]
            m = _mask(it.hole, rules.hole_to_hole + via_drill / 2
                      + rules.margin, *args)
            if m:
                vblk[m[0]] |= m[1]

    for znet, layer, poly in obs.fills_near(win):
        if znet == net or layer not in layers:
            continue
        k = layers.index(layer)
        inside = shapely.contains_xy(poly, X, Y)
        cost[k][inside] *= pour_cost

    for znet, layer, shape, zclr in obs.spokes_near(win):
        # a thermal pad's kept spokes (#1140): the zone's clearance off them
        if znet == net:
            continue
        c = max((zclr or 0.0) + rules.margin, rules.clr(net, znet))
        if layer in layers:
            m = _mask(shape, c + w / 2, *args)
            if m:
                blk[layers.index(layer)][m[0]] |= m[1]
        m = _mask(shape, c + vr, *args)
        if m:
            vblk[m[0]] |= m[1]

    for ra in bg.rule_areas + bg.footprint_rule_areas:
        fl = ra.get("flags", {})
        poly = ra["outline"]
        if poly.is_empty or not poly.intersects(win):
            continue
        if fl.get("tracks") == "not_allowed":
            for k, layer in enumerate(layers):
                if layer in ra["layers"]:
                    m = _mask(poly, w / 2 + rules.margin, *args)
                    if m:
                        blk[k][m[0]] |= m[1]
        if fl.get("vias") == "not_allowed":
            m = _mask(poly, vr + rules.margin, *args)
            if m:
                vblk[m[0]] |= m[1]

    outline = bg.outline
    if outline is not None and not outline.is_empty:
        tin = shapely.contains_xy(
            outline.buffer(-(rules.edge_clearance + w / 2 + rules.margin)), X, Y)
        vin = shapely.contains_xy(
            outline.buffer(-(rules.edge_clearance + vr + rules.margin)), X, Y)
        for k in range(K):
            blk[k] |= ~tin
        vblk |= ~vin

    def side(sd, plane):
        if plane:
            polys = [p for layer, ps in sd.items() if layer not in layers
                     for p in ps]
            if not polys:
                return np.zeros((ny, nx), bool)
            return shapely.contains_xy(shapely.union_all(polys), X, Y)
        out = []
        for layer in layers:
            ps = sd.get(layer, [])
            out.append(shapely.contains_xy(shapely.union_all(ps), X, Y)
                       if ps else np.zeros((ny, nx), bool))
        return out

    a, b = side(side_a, False), side(side_b, False)
    neck: dict = {}
    if necks:
        _narrow_necks(obs, rules, net, layers, window, g, w, via, blk, X, Y,
                      neck, dict(pour_cost=pour_cost, inner_cost=inner_cost,
                                 rip_cost=rip_cost))
    span_moves = _span_moves(obs, rules, net, layers, win, args, K, blk, outline)
    no_via = rules.via_bans(net)
    if "through" in no_via:
        vblk[:] = True
    if no_via and span_moves:
        span_moves = [[mv for mv in ms if mv[1].kind not in no_via]
                      for ms in span_moves]
    return Grid(
        x0=x0, y0=y0, nx=nx, ny=ny, g=g, layers=list(layers),
        blk=[m.ravel().astype(np.uint8).tobytes() for m in blk],
        vblk=vblk.ravel().astype(np.uint8).tobytes(),
        via=(via_size, via_drill),
        cost=[array("d", c.ravel().astype(np.float64).tobytes()) for c in cost],
        in_a=[m.ravel().astype(np.uint8).tobytes() for m in a],
        in_b=[m.ravel().astype(np.uint8).tobytes() for m in b],
        a_plane=side(side_a, True).ravel().astype(np.uint8).tobytes(),
        b_plane=side(side_b, True).ravel().astype(np.uint8).tobytes(),
        span_moves=span_moves, neck=neck)


def _narrow_necks(obs, rules: Rules, net: str, layers: list, window, g: float,
                  w: float, via, blk: list, X, Y, neck: dict, kw: dict) -> None:
    """Clear the net's neck zones for its neck width (#924): a cell inside
    one takes the walls a track of that width sees, built on the same grid,
    and is noted in `neck` for path_to_ops. Only zones narrower than w."""
    zones = rules.necks.get(net) or {}
    by_w: dict = {}
    for layer, zs in zones.items():
        if layer not in layers:
            continue
        for zone, nw in zs:
            if nw < w - 1e-6:
                by_w.setdefault(nw, {}).setdefault(layer, []).append(zone)
    ny, nx = X.shape
    for nw in sorted(by_w, reverse=True):     # the narrowest is laid last
        inside = {}
        for layer, zs in by_w[nw].items():
            m = shapely.contains_xy(shapely.union_all(zs), X, Y)
            if m.any():
                inside[layers.index(layer)] = m
        if not inside:
            continue
        gn = build_grid(obs, rules, net, {}, {}, layers, window, g, width=nw,
                        via=via, necks=False, **kw)
        assert (gn.nx, gn.ny) == (nx, ny)
        for k, m in inside.items():
            blk[k][m] = np.frombuffer(gn.blk[k], np.uint8).reshape(
                ny, nx)[m].astype(bool)
            for c in np.flatnonzero(m.ravel()):
                neck[(k, int(c))] = nw


def _span_moves(obs, rules: Rules, net: str, layers: list, win, args, K: int,
                blk: list, outline) -> list:
    """What an HDI build's spans add to the search (#477): per routable layer
    k, the layers its spans reach, each with its own via mask. A span's via is
    only there on the layers it crosses, so it is held to what is on those
    layers, at JLC's HDI numbers rather than the through-via floors."""
    if not rules.spans:
        return []
    X, Y = args[0], args[1]
    moves: list = [[] for _ in range(K)]
    for sp in rules.spans:
        on = set(rules.copper[sp.i:sp.j + 1])
        ks = [k for k, ly in enumerate(layers) if ly in on]
        if len(ks) < 2:
            continue
        vr = sp.size / 2
        m = np.zeros((args[6], args[5]), bool)
        for it in obs.near(win):
            if not on & set(it.layers):
                continue
            other = it.net != net and net not in it.tied
            if other:
                hit = _mask(it.shape, it.half + max(
                    vr + rules.clr(net, it.net),
                    sp.drill / 2 + rules.span_hole_clearance + rules.margin), *args)
                if hit:
                    m[hit[0]] |= hit[1]
            if it.hole is not None and other:
                hit = _mask(it.hole, sp.drill / 2 + rules.margin
                            + rules.span_hole_to_hole, *args)
                if hit:
                    m[hit[0]] |= hit[1]
            if it.hole is not None and not other:
                # one net's vias: an exact stack, or JLC's same-net spacing.
                # KiCad's DRC doesn't always report a miss (hw-lab hdi-bga).
                hit = _mask(it.hole, sp.drill / 2 + rules.margin
                            + SAME_NET_HOLE_MM, *args)
                if hit:
                    stack = shapely.dwithin(
                        it.hole.centroid, shapely.points(X[hit[0]], Y[hit[0]]), 0.01)
                    m[hit[0]] |= hit[1] & ~stack
        for znet, layer, shape, zclr in obs.spokes_near(win):
            if znet != net and layer in on:
                hit = _mask(shape, max((zclr or 0.0) + rules.margin,
                                       rules.clr(net, znet)) + vr, *args)
                if hit:
                    m[hit[0]] |= hit[1]
        for ra in obs.bg.rule_areas + obs.bg.footprint_rule_areas:
            if ra.get("flags", {}).get("vias") == "not_allowed" \
                    and not ra["outline"].is_empty and ra["outline"].intersects(win):
                hit = _mask(ra["outline"], vr + rules.margin, *args)
                if hit:
                    m[hit[0]] |= hit[1]
        if outline is not None and not outline.is_empty:
            m |= ~shapely.contains_xy(
                outline.buffer(-(rules.edge_clearance + vr + rules.margin)), X, Y)
        mask = m.ravel().astype(np.uint8).tobytes()
        for k in ks:
            for k2 in ks:
                if k2 != k:
                    moves[k].append((k2, sp, mask, ks))
    return moves


SPAN_COST = {"micro": 0.3, "buried": 0.6, "blind": 0.6}  # of a through via


def span_for(gr: Grid, k: int, k2: int, c: int):
    """The cheapest span joining layers k and k2 at cell c, or None."""
    best = None
    for kk, sp, mask, ks in (gr.span_moves[k] if gr.span_moves else ()):
        if kk == k2 and not mask[c] and not any(gr.blk[q][c] for q in ks) \
                and (best is None or SPAN_COST[sp.kind] < SPAN_COST[best.kind]):
            best = sp
    return best


INF = float("inf")
_MOVES = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
          (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)),
          (-1, 1, math.sqrt(2)), (-1, -1, math.sqrt(2))]


def no_path(gr: Grid) -> bool:
    """True when sides A and B share no connected free region, so astar can't
    succeed and needn't search (#784: most of a dense field's time went on
    searches like that). Every move astar has is one of these links, so this
    never refutes a search that would have found a path."""
    if any(gr.a_plane) or any(gr.b_plane):
        return False
    K = len(gr.layers)

    def cells(mask):
        return np.frombuffer(mask, np.uint8).reshape(gr.ny, gr.nx)

    free = [cells(b) == 0 for b in gr.blk]
    in_a = [(cells(b) > 0) & f for b, f in zip(gr.in_a, free)]
    in_b = [(cells(b) > 0) & f for b, f in zip(gr.in_b, free)]
    if not any(a.any() for a in in_a) or not any(b.any() for b in in_b):
        return True
    labels, regions = [], 0
    for k in range(K):
        lab, count = ndimage.label(free[k], np.ones((3, 3), bool))
        lab = lab.astype(np.int64)
        lab[lab > 0] += regions
        labels.append(lab)
        regions += count
    parent = np.arange(regions + 1)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(k, k2, where):
        """Join the regions a layer change at these cells connects."""
        pairs = np.stack([labels[k][where], labels[k2][where]], 1)
        for x, y in np.unique(pairs, axis=0).tolist():
            if x and y:
                parent[find(x)] = find(y)

    through = (cells(gr.vblk) == 0) & np.logical_and.reduce(free)
    if through.any():
        for k in range(1, K):
            join(0, k, through)
    for k, moves in enumerate(gr.span_moves or []):
        for k2, _span, mask, clear in moves:
            if k2 > k:
                join(k, k2, (cells(mask) == 0)
                     & np.logical_and.reduce([free[q] for q in clear]))

    def reached(side):
        return {find(x) for k in range(K)
                for x in np.unique(labels[k][side[k]]).tolist()}

    return not reached(in_a) & reached(in_b)


_astar_lib = None


def _load_astar():
    """route_astar.c as a library, built on first use and cached by its hash.
    None, and the Python search runs, if there is no C compiler here."""
    global _astar_lib
    if _astar_lib is None:
        _astar_lib = False
        src = Path(__file__).with_name("route_astar.c")
        tag = hashlib.sha1(src.read_bytes()).hexdigest()[:12]
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "hwde"
        so = cache / f"route_astar-{tag}.so"
        try:
            if not so.exists():
                cache.mkdir(parents=True, exist_ok=True)
                tmp = so.with_suffix(f".{os.getpid()}.tmp")
                subprocess.run(["cc", "-O2", "-shared", "-fPIC", str(src), "-lm", "-o", str(tmp)],
                               check=True, capture_output=True)
                tmp.replace(so)
            _astar_lib = ctypes.CDLL(str(so))
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"route_finish: Python search, route_astar.c not built: {e}", file=sys.stderr)
    return _astar_lib or None


def _astar_c(lib, gr: Grid, target, via_cost, max_nodes, deadline, search_weight):
    """(found node or None, prev) from route_astar.c."""
    K = len(gr.layers)
    prev = array("i", [0]) * (K * gr.nx * gr.ny)
    u8 = ctypes.POINTER(ctypes.c_ubyte)
    f8 = ctypes.POINTER(ctypes.c_double)
    def b(x):
        return ctypes.cast(ctypes.c_char_p(x), u8)
    costs = [c if isinstance(c, array) else array("d", c) for c in gr.cost]
    cost_ptrs = (f8 * K)(*[ctypes.cast(c.buffer_info()[0], f8) for c in costs])
    found = ctypes.c_int32(0)
    lib.route_astar.argtypes = None
    rc = lib.route_astar(
        gr.nx, gr.ny, K, (u8 * K)(*[b(x) for x in gr.blk]), b(gr.vblk), cost_ptrs,
        (u8 * K)(*[b(x) for x in gr.in_a]), (u8 * K)(*[b(x) for x in gr.in_b]),
        b(gr.a_plane), b(gr.b_plane), ctypes.c_double(via_cost),
        ctypes.c_long(max_nodes),
        ctypes.c_double(-1.0 if deadline is None else deadline),
        ctypes.c_double(search_weight), target[0], target[1],
        ctypes.cast(prev.buffer_info()[0], ctypes.POINTER(ctypes.c_int32)),
        ctypes.byref(found))
    if rc == 4:
        raise MemoryError("route_astar")
    if rc == 2:
        gr.gave_up = True
    return (found.value if rc == 0 else None), prev


def astar(gr: Grid, target, via_cost: float = 15.0,
          max_nodes: int = 3_000_000, deadline: float | None = None,
          search_weight: float = 1.0):
    """Multi-source search from side A's free cells to side B's.

    Nodes are (cell, k); a via (layer change, or a drop into a plane-layer
    side) costs via_cost grid steps, an HDI build's span a share of that
    (SPAN_COST, #477). Returns [(i, j, k, via_here)] from A to
    B, where a final/initial entry with k = -1 is a via into a plane, or None.
    None after max_nodes sets gr.gave_up: a path may still exist.
    search_weight > 1 inflates the distance estimate: far fewer nodes on a
    long route across pours (each step there costs several times the
    estimate), for a route that may not be the shortest.
    """
    nx, ny, K = gr.nx, gr.ny, len(gr.layers)
    N = nx * ny
    ti, tj = target
    lib = None if gr.span_moves else _load_astar()
    if lib:
        found, prev = _astar_c(lib, gr, target, via_cost, max_nodes, deadline, search_weight)
        return _walk_back(gr, found, prev)
    # flat per-node arrays: a whole-board grid has ~10M nodes, and dicts of
    # that size cost several GB. A via drop into B's plane (n < 0) is rare,
    # and stays in a dict.
    dist = array("d", [INF]) * (K * N)
    prev = array("i", [0]) * (K * N)
    drop_dist: dict[int, float] = {}
    pq: list = []

    def h(c):
        di, dj = abs(c % nx - ti), abs(c // nx - tj)
        return search_weight * (max(di, dj) + (math.sqrt(2) - 1) * min(di, dj))

    def via_ok(c):
        if gr.vblk[c]:
            return False
        return not any(gr.blk[k][c] for k in range(K))

    START = -1
    for k in range(K):
        ina, blk = gr.in_a[k], gr.blk[k]
        for c in range(N):
            if ina[c] and not blk[c]:
                n = k * N + c
                dist[n] = 0.0
                prev[n] = START
                heapq.heappush(pq, (h(c), 0.0, n))
    for c in range(N):
        if gr.a_plane[c] and via_ok(c):
            for k in range(K):
                n = k * N + c
                if via_cost < dist[n]:
                    dist[n] = via_cost
                    prev[n] = -2 - c          # starts with a via at c
                    heapq.heappush(pq, (via_cost + h(c), via_cost, n))
    expanded = 0
    found = None
    while pq:
        f, gcost, n = heapq.heappop(pq)
        if n < 0:                      # via drop into side B's plane
            found = n
            break
        if gcost > dist[n]:
            continue
        k, c = divmod(n, N)
        if gr.in_b[k][c]:
            found = n
            break
        expanded += 1
        if expanded > max_nodes:
            gr.gave_up = True
            break
        if deadline is not None and not expanded & 0x3FFF \
                and time.monotonic() >= deadline:
            break
        i, j = c % nx, c // nx
        blk, cost = gr.blk[k], gr.cost[k]
        for di, dj, step in _MOVES:
            ii, jj = i + di, j + dj
            if not (0 <= ii < nx and 0 <= jj < ny):
                continue
            c2 = jj * nx + ii
            if blk[c2]:
                continue
            if di and dj and (blk[j * nx + ii] and blk[jj * nx + i]):
                continue               # no squeezing between two walls
            ng = gcost + step * cost[c2]
            n2 = k * N + c2
            if ng < dist[n2]:
                dist[n2] = ng
                prev[n2] = n
                heapq.heappush(pq, (ng + h(c2), ng, n2))
        if gr.span_moves:
            for k2, sp, mask, ks in gr.span_moves[k]:
                if mask[c] or any(gr.blk[q][c] for q in ks):
                    continue
                n2 = k2 * N + c
                ng = gcost + via_cost * SPAN_COST[sp.kind]
                if ng < dist[n2]:
                    dist[n2] = ng
                    prev[n2] = n
                    heapq.heappush(pq, (ng + h(c), ng, n2))
        if via_ok(c):
            for k2 in range(K):
                if k2 == k:
                    continue
                n2 = k2 * N + c
                ng = gcost + via_cost
                if ng < dist[n2]:
                    dist[n2] = ng
                    prev[n2] = n
                    heapq.heappush(pq, (ng + h(c), ng, n2))
            if gr.b_plane[c]:
                n2 = -2 - (k * N + c)
                ng = gcost + via_cost
                if ng < drop_dist.get(n2, INF):
                    drop_dist[n2] = ng
                    heapq.heappush(pq, (ng, ng, n2))
    return _walk_back(gr, found, prev)


def _walk_back(gr: Grid, found, prev):
    """The path astar found, from side A to side B."""
    if found is None:
        return None
    N = gr.nx * gr.ny
    nx = gr.nx
    START = -1
    path = []
    n = found
    if n < 0:                          # ends with a via into B's plane
        last = -2 - n
        k, c = divmod(last, N)
        path.append((c % nx, c // nx, -1))
        n = last
    while True:
        k, c = divmod(n, N)
        path.append((c % nx, c // nx, k))
        p = prev[n]
        if p == START:
            break
        if p <= -2:                    # started with a via at c
            path.append((c % nx, c // nx, -1))
            break
        n = p
    path.reverse()
    return path


def merge_close_vias(path: list, min_cells: float, can_move=None) -> list:
    """A short run between two layer changes puts two vias of one net too close
    together for JLC's same-net hole spacing, and KiCad's DRC doesn't always say
    so. Take both layer changes at the run's start and carry the jog on the next
    layer, if can_move(run, next_layer) says that copper fits: the search never
    looked at those cells. DRC still judges the moved copper."""
    if any(q[2] == -1 for q in path):
        return path
    runs: list[list] = []
    for q in path:
        if runs and runs[-1][0][2] == q[2]:
            runs[-1].append(q)
        else:
            runs.append([q])
    n = 1
    while n < len(runs) - 1:
        run = runs[n]
        nxt = runs[n + 1][0][2]
        if math.dist(run[0][:2], run[-1][:2]) < min_cells and run[0][:2] != run[-1][:2] \
                and (can_move is None or can_move(run, nxt)):
            runs[n + 1] = [(q[0], q[1], nxt) for q in run] + runs[n + 1]
            runs[n] = [run[0]]
        n += 1
    return [q for run in runs for q in run]


def _can_move_run(gr: Grid, run: list, k2: int) -> bool:
    """A span joins the run's layer to k2 at its start, and k2 is open along it."""
    (i, j, k) = run[0]
    return span_for(gr, k, k2, j * gr.nx + i) is not None \
        and not any(gr.blk[k2][y * gr.nx + x] for x, y, _ in run)


def too_close_via(gr: Grid, path: list, rules: Rules) -> tuple | None:
    """(k, cell) of a layer change this path makes too near its own last one,
    where merge_close_vias couldn't help, or None. The search can't see this:
    one net's vias don't block each other while it runs, and DRC would take the
    whole route out afterwards (#784)."""
    if not rules.spans or any(q[2] == -1 for q in path):
        return None
    path = _merged(gr, path, rules)
    vias = []
    for (i, j, k), (i2, j2, k2) in zip(path, path[1:]):
        if k != k2:
            sp = span_for(gr, k, k2, j * gr.nx + i)
            drill = sp.drill if sp else gr.via[1]
            for x, y, d in vias:
                gap = math.dist((x, y), (i, j)) * gr.g - (d + drill) / 2
                if (x, y) != (i, j) \
                        and gap < rules.span_hole_to_hole + rules.margin:
                    return k, j * gr.nx + i
            vias.append((i, j, drill))
    return None


def _merged(gr: Grid, path: list, rules: Rules) -> list:
    """path_to_ops' own merge of an HDI route's close vias."""
    return merge_close_vias(
        path, (SAME_NET_HOLE_MM + rules.via_floor[1]) / gr.g,
        lambda run, k2: _can_move_run(gr, run, k2))


def _straights(gr: Grid, run: list, width: float) -> list:
    """One layer's run of cells -> (start, end, width) per straight piece. A
    step that touches a neck cell takes the neck width it was cleared for
    (#924): the neck reaches one step past its zone, onto a cell cleared for
    the full width, so the wide track starts where it was checked."""
    def step_w(q0, q1):
        ws = [gr.neck.get((q[2], q[1] * gr.nx + q[0])) for q in (q0, q1)]
        return min([x for x in ws if x] or [width])
    out = []
    for q0, q1 in zip(run, run[1:]):
        if q0[:2] == q1[:2]:
            continue
        d, w = (q1[0] - q0[0], q1[1] - q0[1]), step_w(q0, q1)
        if out and out[-1][3] == d and out[-1][2] == w:
            out[-1][1] = q1
        else:
            out.append([q0, q1, w, d])
    return [(a, b, w) for a, b, w, _ in out]


def path_to_ops(gr: Grid, path, net: str, width: float, rules: Rules) -> list:
    """Grid path -> route_edit ops: one track per straight run, a via at
    every layer change and at a plane drop (k = -1). A layer change on an HDI
    build takes the span the search priced it at (#477)."""
    if rules.spans:
        path = _merged(gr, path, rules)
    ops = []
    vias = []
    runs: list[list] = []
    for i, j, k in path:
        if k == -1:
            vias.append((gr.xy(i, j), None))
            continue
        if runs and runs[-1][0][2] == k:
            runs[-1].append((i, j, k))
        else:
            if runs:
                vias.append((gr.xy(i, j),
                             span_for(gr, runs[-1][0][2], k, j * gr.nx + i)))
            runs.append([(i, j, k)])
    for run in runs:
        layer = gr.layers[run[0][2]]
        for c0, c1, w in _straights(gr, run, width):
            ops.append({"op": "add_track", "start": list(gr.xy(*c0[:2])),
                        "end": list(gr.xy(*c1[:2])), "width": w,
                        "layer": layer, "net": net})
    seen = set()
    for at, sp in vias:
        if (at, sp) in seen:
            continue
        seen.add((at, sp))
        ops.append(sp.as_op(at, net) if sp else
                   {"op": "add_via", "at": list(at), "size": gr.via[0],
                    "drill": gr.via[1], "net": net})
    return ops


def ops_items(ops: list, conn: int, copper_layers) -> list[Item]:
    out = []
    for op in ops:
        if op["op"] == "add_track":
            out.append(Item(op["net"], frozenset({op["layer"]}),
                            LineString([op["start"], op["end"]]),
                            op["width"] / 2, conn=conn, kind="track"))
        else:
            on = list(copper_layers)
            if "top" in op:
                on = on[on.index(op["top"]):on.index(op["bot"]) + 1]
            out.append(Item(op["net"], frozenset(on),
                            Point(op["at"]), op["size"] / 2,
                            Point(op["at"]).buffer(op["drill"] / 2,
                                                   quad_segs=4), conn=conn,
                            kind="via"))
    return out


# ----------------------------------------------------------------- driver

def routable_layers(bg: geom.BoardGeom, spec: str = "auto") -> list[str]:
    """auto: every copper layer but inner planes (a fill and no tracks)."""
    if spec and spec != "auto":
        want = [s.strip() for s in spec.split(",") if s.strip()]
        bad = [w for w in want if w not in bg.copper_layers]
        if bad:
            raise CheckError(f"not copper layers of this board: {bad}")
        return want
    import route_auto
    planes = set(route_auto._auto_power_layers(bg))
    return [layer for layer in bg.copper_layers if layer not in planes]


def unconnected_pairs(drc: dict, nets: set | None = None) -> list[dict]:
    out = []
    for v in drc["violations"]:
        if v.get("source") != "unconnected" or not v.get("net"):
            continue
        if nets and v["net"] not in nets:
            continue
        items = v.get("items") or []
        if len(items) < 2:
            continue
        out.append({"net": v["net"], "a": items[0], "b": items[1]})
    return out


def err_counts(report: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in report["violations"]:
        if v.get("severity") == "error" and v.get("source") != "unconnected":
            key = f"{v.get('source')}:{v.get('check')}"
            out[key] = out.get(key, 0) + 1
    return out


def unconnected_by_net(report: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in report["violations"]:
        if v.get("source") == "unconnected":
            out[v.get("net") or ""] = out.get(v.get("net") or "", 0) + 1
    return out


# KiCad's DRC stops reporting unconnected items here, so a board with more than
# this many shows the same total however many connections a pass closes.
DRC_UNCONNECTED_CAP = 499


def _pass_helped(before: int, after: int, jobs: list[dict]) -> bool:
    """Did the unconnected count drop? At the DRC's cap the count can't show it:
    a routed connection that DRC didn't take out is the only evidence left."""
    if after < before:
        return True
    return (before >= DRC_UNCONNECTED_CAP and after >= DRC_UNCONNECTED_CAP
            and any(j["status"] == "routed" for j in jobs))


def conn_key(net: str, a, b) -> tuple:
    """A connection's name across runs: its net and the two points the plan
    joins. It stays the same until one of the net's two islands changes, and
    then it is a different connection and worth a new try."""
    return (net, *sorted((tuple(a), tuple(b))))


def stub_exits(bg: geom.BoardGeom) -> dict[str, list]:
    """{net: [(layer, (x, y), (dx, dy))]}: where each net's stubs end in open
    board and the way they head there. A track end is open when no other track
    of the net shares that layer and point and no via or pad of the net is on
    it. Stubs of different nets end at one x,y on different layers, so a tip's
    only way out is often straight ahead on its own layer (#784)."""
    ends: dict[tuple, list] = {}
    for t in bg.tracks_of():
        if not t.net:
            continue
        coords = list(t.shape.coords)
        for at, back in ((coords[0], coords[1]), (coords[-1], coords[-2])):
            key = (t.net, t.layer, round(at[0], 3), round(at[1], 3))
            ends.setdefault(key, []).append((at, back))
    vias = {(v.net, round(v.at[0], 3), round(v.at[1], 3)) for v in bg.vias_of()}
    pads: dict[str, list] = {}
    for p in bg.pads_of():
        pads.setdefault(p.net, []).append(p.poly)
    exits: dict[str, list] = {}
    for (net, layer, x, y), seen in ends.items():
        if len(seen) > 1 or (net, x, y) in vias:
            continue
        (ax, ay), (bx, by) = seen[0]
        if any(p.distance(Point(ax, ay)) < 0.01 for p in pads.get(net, ())):
            continue
        d = math.hypot(ax - bx, ay - by)
        if d:
            exits.setdefault(net, []).append(
                (layer, (ax, ay), ((ax - bx) / d, (ay - by) / d)))
    return exits


def exit_keepouts(exits: dict, open_nets, routing: str, length: float,
                  half) -> list[Item]:
    """The corridors past the open tips of open_nets, as that net's own copper,
    so they stop every net but their owner. half(net): half the corridor's
    width. routing: the net being routed, whose own corridors aren't needed."""
    return [Item(net, frozenset({layer}),
                 LineString([(x, y), (x + dx * length, y + dy * length)]),
                 half(net), kind="track")
            for net in dict.fromkeys(open_nets) if net != routing
            for layer, (x, y), (dx, dy) in exits.get(net, ())]


def plan_routes(bg: geom.BoardGeom, rules: Rules, pairs: list[dict],
                layers: list[str], *, grid: float = 0.1, margin: float = 4.0,
                max_conns: int | None = None, max_nodes: int = 3_000_000,
                search_weight: float = 1.0,
                via_cost_mm: float = 1.5, rip_board: bool = False,
                rip_board_nets: int = 4, ripped_out: list | None = None,
                log=None, skip: set | None = None,
                keep_nets_extra=(), deadline: float | None = None,
                on_try=None) -> list[dict]:
    """Find a route for each pair, shortest first; each found route is an
    obstacle for the ones after it. Returns [{net, ops, status, ...}].

    skip: conn_key()s to leave out, so they don't use up max_conns.
    keep_nets_extra: nets never ripped, like pour nets. deadline
    (time.monotonic()): start no search after it and cut the one running;
    what was found so far still comes back. on_try(job): called before each
    connection's search."""
    obs = Obstacles(bg)
    isl_cache: dict[str, list] = {}
    jobs = []
    seen: set = set()

    def add_job(net, isl, ia, ib, a_pos, b_pos):
        job = {"net": net, "a": a_pos, "b": b_pos}
        if ia is None or ib is None:
            job.update(status="skipped", reason="item not on the net's copper")
        elif ia == ib:
            job.update(status="skipped",
                       reason="geometry already joins both items")
        elif (net, min(ia, ib), max(ia, ib)) in seen:
            return
        else:
            seen.add((net, min(ia, ib), max(ia, ib)))
            ua = shapely.union_all([p for ps in isl[ia].values() for p in ps])
            ub = shapely.union_all([p for ps in isl[ib].values() for p in ps])
            pa, pb = shapely.ops.nearest_points(ua, ub)
            job.update(side_a=isl[ia], side_b=isl[ib], pa=(pa.x, pa.y),
                       pb=(pb.x, pb.y), dist=pa.distance(pb))
            job["a"], job["b"] = [round(pa.x, 3), round(pa.y, 3)], \
                [round(pb.x, 3), round(pb.y, 3)]
        jobs.append(job)

    zone_nets = []
    for pr in pairs:
        net = pr["net"]
        if net not in isl_cache:
            isl_cache[net] = net_islands(bg, net)
        isl = isl_cache[net]
        if any(pr[s].get("msg", "").startswith("Zone") for s in ("a", "b")):
            # KiCad names a zone by its outline's first corner, not by the
            # fill piece that is cut off: join every island to the largest
            if net not in zone_nets:
                zone_nets.append(net)
            continue
        ia = pick_island(isl, pr["a"].get("pos"), pr["a"].get("msg", ""))
        ib = pick_island(isl, pr["b"].get("pos"), pr["b"].get("msg", ""))
        add_job(net, isl, ia, ib, pr["a"].get("pos"), pr["b"].get("pos"))
    for net in zone_nets:
        isl = isl_cache[net]
        area = [sum(p.area for ps in i.values() for p in ps) for i in isl]
        main = max(range(len(isl)), key=lambda i: area[i])
        for i in range(len(isl)):
            if i != main:
                add_job(net, isl, i, main, None, None)
    todo = sorted((j for j in jobs if "side_a" in j), key=lambda j: j["dist"])
    if skip:
        kept_todo = []
        for j in todo:
            if conn_key(j["net"], j["a"], j["b"]) in skip:
                j.update(status="skipped", reason="set aside")
            else:
                kept_todo.append(j)
        todo = kept_todo
    if max_conns is not None:
        for j in todo[max_conns:]:
            j.update(status="skipped", reason="over --max-conns")
        todo = todo[:max_conns]
    exits = stub_exits(bg) if rules.spans else {}
    inner_cost = {"inner_cost": 1.0} if rules.spans else {}

    def search(job, hide=False, board=False, base=None):
        """(path, grid, width) over the ladder: the window, a wider window,
        then a finer grid at the net's floor width for a pad boxed in by
        its neighbours, kept close to the ends: under a large --margin that
        grid is several GB. That rung's clearance margin is FINE_MARGIN_MM,
        so a floor track fits between 0.5 mm-pitch balls (#923). An HDI build's net with no rule on its copper takes
        the floor width from the start. hide: leave this pass's own routes
        out. base: the rules to search under (default: the pass's)."""
        rules_ = base or rules
        net = job["net"]
        if not rules.net_layers(net, layers):
            job["banned"] = sorted(layers)      # #1140: DRC would fail it
            return None, None, None
        floor = rules.width_floor(net), rules.via_floor
        start = (floor if rules.spans and net not in keep_nets
                 else (rules.width(net), None))
        held = obs.added
        if hide:
            obs.added = []
        obs.added = obs.added + exit_keepouts(
            exits, [j["net"] for j in todo
                    if j.get("status") in (None, "ripped")],
            net, EXIT_MM, lambda nt: rules.width_floor(nt) / 2)
        if board:
            obs.soft_board = keep_nets
        fine = dataclasses.replace(
            rules_, margin=min(rules.margin, FINE_MARGIN_MM))
        guards = list(obs.guards)
        strict = dict(obs.pair_strict)
        try:
            for m, g, (w, v), rr in (
                    (margin, grid, start, rules_),
                    (margin * 2.5, grid, start, rules_),
                    (min(margin, 4.0), grid / 2, floor, fine)):
                (ax, ay), (bx, by) = job["pa"], job["pb"]
                win = (min(ax, bx) - m, min(ay, by) - m,
                       max(ax, bx) + m, max(ay, by) + m)
                hit = None
                rounds = tries = 0
                # a path that cuts a pour off a via or pad only the pour
                # joins is searched again round the pocket it walls off
                # (#1311); one that still cuts is kept and noted. One that
                # misses a pair or courtyard rule's exact value is searched
                # again with that item read stricter (#682)
                while True:
                    gr = build_grid(obs, rr, net, job["side_a"],
                                    job["side_b"], layers, win, g, width=w,
                                    via=v, **inner_cost)
                    if no_path(gr):
                        break
                    target = (int(round((bx - gr.x0) / g)),
                              int(round((by - gr.y0) / g)))
                    for _ in range(VIA_RETRIES + 1):
                        path = astar(gr, target, via_cost=via_cost_mm / g,
                                     max_nodes=max_nodes, deadline=deadline,
                                     search_weight=search_weight)
                        near = path and too_close_via(gr, path, rr)
                        if not near:
                            break
                        # search again with that layer change's cell closed
                        k, c = near
                        gr.blk[k] = gr.blk[k][:c] + b"\1" + gr.blk[k][c + 1:]
                        path = None
                    if not path:
                        if hit is None:
                            job["gave_up"] = job.get("gave_up") or gr.gave_up
                        break
                    ops = path_to_ops(gr, path, net, w, rules)
                    miss = pair_misses(obs, rr, ops, net)
                    if miss:
                        if tries >= PAIR_TRIES:
                            break
                        tries += 1
                        for i in miss:
                            obs.pair_strict[i] = obs.pair_strict.get(i, 0) + 1
                        continue
                    cuts = pour_cuts(obs, ops, net, rules)
                    if hit is None or not cuts:
                        hit = (path, gr, w, cuts)
                    if not cuts or rounds >= LINK_ROUNDS:
                        break
                    rounds += 1
                    obs.guards += [(zn, ly, pk, zc)
                                   for _, ly, pk, zn, zc in cuts]
                if hit:
                    path, gr, w, cuts = hit
                    if cuts:
                        job["pour_cut"] = cut_note(cuts)
                    else:
                        job.pop("pour_cut", None)
                    return path, gr, w
        finally:
            obs.added = held
            obs.soft_board = None
            obs.guards = guards
            obs.pair_strict = strict
        return None, None, None

    def commit(n, job, path, gr, w):
        ops = path_to_ops(gr, path, job["net"], w, rules)
        job.update(status="routed", ops=ops, width=w,
                   vias=sum(op["op"] == "add_via" for op in ops))
        obs.added.extend(ops_items(ops, n, bg.copper_layers))

    # power and pour nets are never ripped: their copper is sized by rule
    # nor are constraints diff pairs: a pair ripped here comes back single-ended (#613)
    keep_nets = pour_nets(bg) | set(rules.net_width) | rules.sized_nets | {""}
    keep_nets |= set(keep_nets_extra or ())

    def expired():
        return deadline is not None and time.monotonic() >= deadline

    def _in_pads(path, gr, w, net) -> list:
        """(ref, pad) of the own-net SMD pads a path's vias sit in."""
        out = []
        for op in path_to_ops(gr, path, net, w, rules):
            if op["op"] != "add_via":
                continue
            at = Point(op["at"])
            vr, vd = op["size"] / 2, op["drill"]
            for it in obs.near(at.buffer(vr + 1.0)):
                if it.smd and it.net == net \
                        and it.ref not in rules.via_in_pad_refs \
                        and it.shape.distance(at) < rules.pad_via_keep(
                            it.ref, it.number, vr, vd) - 1e-6 \
                        and (it.ref, it.number) not in out:
                    out.append((it.ref, it.number))
        return sorted(out)

    def _needs_pad_via(job) -> list:
        """(ref, pad) of the own-net SMD pads a failed job's route would
        put a via in: searched with every pad's via keep lifted, then, when
        that takes more than one part, with one part's lifted at a time
        (the fewest to name). [] when it fails that way too."""
        net = job["net"]
        # a search that ran out of nodes would only run out again
        if expired() or job.get("gave_up") or not any(
                it.smd and it.net == net
                and it.ref not in rules.via_in_pad_refs for it in obs.items):
            return []
        held = {k: job[k] for k in ("gave_up", "pour_cut") if k in job}
        try:
            path, gr, w = search(job, base=dataclasses.replace(
                rules, pad_vias=True))
            found = _in_pads(path, gr, w, net) if path else []
            refs = sorted({r for r, _ in found})
            for r in refs[:PAD_VIA_TRIES] if len(refs) > 1 else ():
                if expired():
                    break
                path, gr, w = search(job, base=dataclasses.replace(
                    rules, via_in_pad_refs=rules.via_in_pad_refs | {r}))
                if path:
                    return [(r, n) for rr, n in _in_pads(path, gr, w, net)
                            if rr == r] or found
            return found
        finally:
            for k in ("gave_up", "pour_cut"):
                job.pop(k, None)
            job.update(held)

    def _walled(job) -> str:
        """#682: why a failed job's side can't be left even by a floor-width
        track on the fine rung with only fixed copper about (pads, locked
        copper, fills; no other route): every cell of its own copper is
        inside a neighbour's clearance and it has no plane to drop to. ''
        when neither side is so held, or a side spans 10 mm or more."""
        net = job["net"]
        w, v = rules.width_floor(net), rules.via_floor
        fine = dataclasses.replace(
            rules, margin=min(rules.margin, FINE_MARGIN_MM))
        held = obs.added, set(obs.hidden)
        obs.added = []
        obs.hidden |= {it.uuid for it in obs.items
                       if it.uuid and not it.locked}
        try:
            for key in ("side_a", "side_b"):
                sd = job[key]
                u = shapely.union_all([p for ps in sd.values() for p in ps])
                x0, y0, x1, y1 = u.bounds
                if max(x1 - x0, y1 - y0) >= 10.0:
                    continue
                try:
                    gr = build_grid(obs, fine, net, sd, sd, layers,
                                    (x0 - 1, y0 - 1, x1 + 1, y1 + 1),
                                    grid / 2, width=w, via=v, **inner_cost)
                except CheckError:
                    return ""
                if any(gr.a_plane) or any(
                        ((np.frombuffer(a, np.uint8) > 0)
                         & (np.frombuffer(b, np.uint8) == 0)).any()
                        for a, b in zip(gr.in_a, gr.blk)):
                    continue
                by: dict = {}
                for it in obs.near(u.buffer(1.0)):
                    if not it.net or it.net == net or net in it.tied \
                            or not it.layers & set(gr.layers):
                        continue
                    c = rules.item_clr(Probe(net, "track", (), gr.layers),
                                       it) - rules.margin
                    if it.shape.distance(u) - it.half < c + w:
                        by[it.net] = max(by.get(it.net, 0.0), c)
                if not by:
                    continue
                top = max(by.values())
                rule = rules.held_by(net) or next(
                    (rules.held_by(n) for n in sorted(by)
                     if by[n] >= top - 1e-9 and rules.held_by(n)), "")
                pads = sorted({f"{it.ref} pad {it.number}"
                               for it in obs.near(u) if it.kind == "pad"
                               and it.net == net and it.ref
                               and it.shape.intersects(u)})
                return (f"no path: {', '.join(pads) or 'its copper'} walled "
                        f"in: {net} held {top:g} mm"
                        + (f" (rule {rule})" if rule else "")
                        + f" from {', '.join(sorted(by))}")
            return ""
        finally:
            obs.added, obs.hidden = held

    queue = list(range(len(todo)))
    ripped_once: set[int] = set()
    ripped_board: set[int] = set()
    steps = 0
    while queue:
        if expired():
            for n in queue:
                todo[n].update(status="skipped", reason="over the deadline")
            break
        n = queue.pop(0)
        job = todo[n]
        steps += 1
        if on_try:
            on_try(job)
        path, gr, w = search(job)
        walled = ""
        if path:
            commit(n, job, path, gr, w)
        elif not (job.get("banned") or job.get("gave_up") or expired()):
            # #682: a side only fixed copper holds in; no rip frees it
            walled = _walled(job)
        if walled:
            pass
        elif n not in ripped_once:
            # rip-up and reroute: when only this pass's own routes are in
            # the way, take out the ones the free path runs into, route
            # this one, and queue them again (each job rips once)
            ripped_once.add(n)
            path, gr, w = search(job, hide=True)
            if path:
                mine = ops_items(path_to_ops(gr, path, job["net"], w, rules),
                                 n, bg.copper_layers)
                block = set()
                for it in obs.added:
                    if it.net == job["net"]:
                        continue
                    c = rules.clr(job["net"], it.net)
                    for m in mine:
                        if not (it.layers & m.layers):
                            continue
                        d = it.shape.distance(m.shape) - it.half - m.half
                        if d < c and d < rules.item_clr(m, it):
                            block.add(it.conn)
                            break
                if block:
                    obs.added = [it for it in obs.added
                                 if it.conn not in block]
                    for c in sorted(block):
                        todo[c].update(status="ripped")
                        todo[c].pop("ops", None)
                        queue.append(c)
                    commit(n, job, path, gr, w)
                    job["ripped"] = len(block)
        if job.get("status") != "routed" and rip_board and not walled \
                and n not in ripped_board:
            # still walled in: the tracks and vias of other nets in the
            # way (pads, pours and holes stay) are ripped, and their nets
            # come back unconnected for the next pass to route again
            ripped_board.add(n)
            path, gr, w = search(job, hide=True, board=True)
            if path:
                mine = ops_items(path_to_ops(gr, path, job["net"], w, rules),
                                 n, bg.copper_layers)
                rip, nets, locked = [], set(), []
                win = shapely.union_all([m.shape for m in mine]).buffer(1.0)
                for it in obs.near(win) + obs.added:
                    if it.net == job["net"]:
                        continue
                    c = rules.clr(job["net"], it.net)
                    if any(((it.layers & m.layers) and (d := it.shape.distance(
                            m.shape) - it.half - m.half) < c
                            and d < rules.item_clr(m, it)) or (
                            it.hole is not None and m.hole is not None
                            and it.hole.distance(m.hole)
                            < rules.hole_to_hole + rules.margin)
                            for m in mine):
                        if it.locked:
                            # a person fixed it in place (#664): no rip
                            locked.append(it.uuid)
                        elif it.uuid:
                            rip.append(it.uuid)
                            nets.add(it.net)
                        elif it.conn >= 0:
                            rip.append(("conn", it.conn))
                own = {u[1] for u in rip if isinstance(u, tuple)}
                board_rip = [u for u in rip if not isinstance(u, tuple)]
                fixed = sorted(nt for nt in nets if nt in keep_nets)
                if locked:
                    job["rip_refused"] = sorted(nets)
                    job["locked_kept"] = sorted(set(locked))
                elif fixed or len(nets) > rip_board_nets:
                    job["rip_refused"] = fixed or sorted(nets)
                    if not fixed:
                        # only the count stopped it: a wider budget may
                        # free the ball (route_auto's wide-rip retry)
                        job["rip_over_budget"] = len(nets)
                else:
                    obs.hidden.update(board_rip)
                    obs.added = [it for it in obs.added if it.conn not in own]
                    for c in sorted(own):
                        todo[c].update(status="ripped")
                        todo[c].pop("ops", None)
                        queue.append(c)
                    commit(n, job, path, gr, w)
                    job["rip_uuids"] = sorted(set(board_rip))
                    job["ripped_nets"] = sorted(nets)
        if job.get("status") not in ("routed",):
            if expired():
                job.update(status="skipped", reason="cut by the deadline")
            else:
                job.update(status="failed", reason=(
                    "the .kicad_dru keeps its tracks off every routable layer"
                    if job.get("banned") else
                    "search stopped at --max-nodes; a path may exist"
                    if job.get("gave_up") else "no path on the grid"))
                if walled:
                    # #682: no rip or pad via frees it; route_close sets
                    # it aside rather than trying it every round
                    job.update(walled=True, reason=walled)
                elif not job.get("banned"):
                    in_pad = _needs_pad_via(job)
                    if in_pad:
                        # #636: no via in a pad unasked; say which pad
                        # would take one, and how to allow it
                        refs = sorted({r for r, _ in in_pad})
                        job["via_in_pad"] = [f"{r} pad {n}" for r, n in in_pad]
                        job["reason"] = (
                            "routes only with a via in "
                            + ", ".join(job["via_in_pad"])
                            + "; to allow it (filled and capped), pass "
                            f"--via-in-pad-refs {','.join(refs)}")
        if log:
            log(f"{steps} {job['net']}: {job['status']}")
    for j in jobs:
        for k in ("side_a", "side_b"):
            j.pop(k, None)
    if ripped_out is not None:
        ripped_out.extend(sorted(obs.hidden))
    return jobs


def _named_conns(after: dict, uuid_conn: dict) -> dict[int, list]:
    """Connections whose copper DRC names in an error -> the checks."""
    bad: dict[int, list] = {}
    for v in after["violations"]:
        if v.get("severity") != "error" or v.get("source") == "unconnected":
            continue
        for it in v.get("items", []):
            if it.get("uuid") in uuid_conn:
                checks = bad.setdefault(uuid_conn[it["uuid"]], [])
                if v.get("check") not in checks:
                    checks.append(v.get("check"))
    return bad


def board_drop_vias(pcb: Path, bg: geom.BoardGeom) -> list:
    """The board's vias with teardrops on, as teardroplib drop_vias has
    them; [] without reading the file's tree when it has no teardrop
    setting at all."""
    import sexpdata
    text = Path(pcb).read_text(encoding="utf-8")
    if "(teardrops" not in text:
        return []
    return tdl.drop_vias(bg, tdl.via_params(sexpdata.loads(text)))


def neck_fns(bg: geom.BoardGeom, rules: Rules, vias: list, skip=()):
    """(width_of, board_at) for teardroplib neck_ops on board bg (#1320).
    width_of(via, p): the widths a track into the drop via may narrow to,
    best first (neck_widths): the board's own tracks into that via that
    take a drop, widest first, then the net's width, then its floor.
    board_at(net, layer, pt): board copper of the net there, which ends a
    neck's walk; the tracks in skip (uuids: those the neck walks) and the
    drop vias (neck_ops' list ends a walk at one) left out."""
    on = {v["uuid"] for v in vias} | set(skip)

    def width_of(v, p):
        c, size = tuple(v["at"]), float(v["size"])
        had = [t.width for t in bg.tracks_of(v["net"])
               if t.uuid not in skip and tdl.takes_drop(t.width, size, p)
               and min(math.dist(t.shape.coords[0], c),
                       math.dist(t.shape.coords[-1], c))
               <= size / 2 + tdl.NECK_TOL]
        return tdl.neck_widths(size, p, had, rules.width(v["net"]),
                               rules.width_floor(v["net"]))

    def board_at(net, layer, pt) -> bool:
        q = Point(pt)
        return any(it.poly.distance(q) <= tdl.NECK_TOL
                   for it in bg.tracks_of(net, layer)
                   + bg.vias_of(net, layer) + bg.pads_of(net, layer)
                   if getattr(it, "uuid", None) not in on)
    return width_of, board_at


def neck_jobs(pcb: Path, bg: geom.BoardGeom, rules: Rules,
              jobs: list, rip=()) -> list:
    """Neck the routed jobs' tracks that end too wide for a drop on a via
    of the board with teardrops on (#1320): a join is laid at the net's
    width, and KiCad makes no drop for a track at or over filter_ratio x
    the via's size, so the track narrows from the via out past where the
    drop ends (teardroplib neck_ops) to a width neck_fns gives. Each job's
    ops change in place, and its "necks" lists what narrowed. The board
    copper in rip (uuids the plan takes off) is not there for it. Returns
    every job's necks."""
    rip = set(rip)
    vias = [v for v in board_drop_vias(pcb, bg) if v["uuid"] not in rip]
    if not vias:
        return []
    width_of, board_at = neck_fns(bg, rules, vias, rip)
    out = []
    for j in jobs:
        if j.get("status") != "routed" or not j.get("ops"):
            continue
        necks = tdl.neck_ops(j["ops"], vias, width_of, board_at)
        if necks:
            j["necks"] = necks
            out += necks
    return out


def _one_pass(cli, staged, drc, opts, *, parity, refill, log):
    """Plan, apply, DRC, take out connections DRC names in an error.
    Returns (jobs, drc_after or None when nothing was applied)."""
    import route_edit

    bg = geom.BoardGeom.from_file(staged)
    rules = board_rules(staged, bg, min_width=opts["min_width"],
                        margin=opts["clearance_margin"],
                        via_size=opts["via_size"], via_drill=opts["via_drill"])
    rules.via_in_pad_refs = opts["via_in_pad_refs"]
    lays = routable_layers(bg, opts["layers"])
    pairs = unconnected_pairs(drc, opts["nets"])
    rip: list = []
    jobs = plan_routes(bg, rules, pairs, lays, grid=opts["grid"],
                       margin=opts["margin"], max_conns=opts["max_conns"],
                       rip_board=opts["rip_board"], ripped_out=rip, log=log,
                       rip_board_nets=opts["rip_board_nets"],
                       skip=opts["skip"], keep_nets_extra=opts["keep_nets"],
                       deadline=opts["deadline"], on_try=opts["on_try"],
                       max_nodes=opts["max_nodes"],
                       search_weight=opts["search_weight"])
    if opts["skip"] is not None:
        # a chunk of route_close: it tries these again next round, so the
        # chunk's later passes don't spend their time on them
        opts["skip"].update(conn_key(j["net"], j["a"], j["b"]) for j in jobs
                            if j["status"] == "failed")
    # a join at the net's width can end too wide for a via's drop (#1320)
    neck_jobs(staged, bg, rules, jobs, rip)
    all_ops, op_conn = [], []
    for c, j in enumerate(jobs):
        if j["status"] == "routed":
            all_ops += j["ops"]
            op_conn += [c] * len(j["ops"])
    if not all_ops:
        return jobs, None, rules, lays
    res = route_edit.apply_ops(staged, all_ops + [
        {"op": "remove", "uuid": u} for u in rip])
    uuid_conn: dict[str, int] = {}
    conn_uuids: dict[int, list] = {}
    for c, r in zip(op_conn, res):
        if r.get("uuid"):
            uuid_conn[r["uuid"]] = c
            conn_uuids.setdefault(c, []).append(r["uuid"])
    after = None
    for _ in range(3):
        t0 = time.monotonic()
        if refill:
            kc.run_drc(cli, staged, refill=True, save_board=True)
        after = kc.run_drc(cli, staged, parity=parity, all_track_errors=True)
        opts["drc_s"] = max(opts["drc_s"], time.monotonic() - t0)
        bad = _named_conns(after, uuid_conn)
        if not bad:
            break
        route_edit.apply_ops(staged, [{"op": "remove", "uuid": u}
                                      for c in bad for u in conn_uuids[c]])
        for c in bad:
            jobs[c].update(status="rolled_back",
                           reason="DRC after refill: " + ", ".join(bad[c]))
            for u in conn_uuids.pop(c):
                uuid_conn.pop(u, None)
        after = None
    if after is None:
        if refill:
            kc.run_drc(cli, staged, refill=True, save_board=True)
        after = kc.run_drc(cli, staged, parity=parity, all_track_errors=True)
    if opts["skip"] is not None:
        opts["skip"].update(conn_key(j["net"], j["a"], j["b"]) for j in jobs
                            if j["status"] == "rolled_back")
    return jobs, after, rules, lays


def finish_staged(cli: Path, staged: Path, drc: dict, *, nets=None,
                  layers: str = "auto", grid: float = 0.1,
                  margin: float = 4.0, min_width: float = 0.2,
                  clearance_margin: float = 0.02, via_size=None,
                  via_drill=None, max_conns=None, parity: bool = False,
                  refill: bool = True, dry_run: bool = False,
                  passes: int = 6, rip_errors: bool = False,
                  rip_board: bool = True, keep_nets: set | None = None,
                  log=None, skip_conns=None, rip_board_nets: int = 4,
                  deadline: float | None = None, on_try=None,
                  max_nodes: int = 3_000_000,
                  search_weight: float = 1.0,
                  via_in_pad_refs=None, protect_uuids=None) -> dict:
    """Route the staged board's unconnected pairs in place, in passes: a
    route can split another net's pour, and the next pass joins the piece.
    A pass is kept only when the unconnected count drops and no error type
    grows against the start of the pass. Returns facts with "kept" and,
    when kept, "drc" (the report after).

    rip_errors takes out the tracks and vias DRC names in a copper error,
    but never one in protect_uuids (the caller's copper from before
    routing, #655); facts "ripped_items" and "protected_kept" list both by
    net and position.

    rip_board_nets: the most other nets one walled-in connection may rip
    (rip_board); a connection refused only for the count carries
    rip_over_budget, the count it needed."""
    import shutil

    opts = dict(nets=set(nets) if nets else None, layers=layers, grid=grid,
                margin=margin, min_width=min_width,
                clearance_margin=clearance_margin, via_size=via_size,
                via_drill=via_drill, max_conns=max_conns,
                rip_board=rip_board, rip_board_nets=rip_board_nets,
                drc_s=0.0, deadline=deadline,
                skip=None if skip_conns is None else set(skip_conns),
                keep_nets=keep_nets, on_try=on_try, max_nodes=max_nodes,
                search_weight=search_weight,
                via_in_pad_refs=frozenset(via_in_pad_refs or ()))
    start_un = sum(unconnected_by_net(drc).values())
    facts: dict = {"unconnected_before": start_un,
                   "errors_before": err_counts(drc), "passes": []}
    if dry_run:
        bg = geom.BoardGeom.from_file(staged)
        rules = board_rules(staged, bg, min_width=min_width,
                            margin=clearance_margin, via_size=via_size,
                            via_drill=via_drill)
        rules.via_in_pad_refs = opts["via_in_pad_refs"]
        lays = routable_layers(bg, layers)
        jobs = plan_routes(bg, rules, unconnected_pairs(drc, opts["nets"]),
                           lays, grid=grid, margin=margin,
                           max_conns=max_conns, rip_board=rip_board, log=log,
                           rip_board_nets=rip_board_nets, skip=opts["skip"], keep_nets_extra=keep_nets,
                           max_nodes=max_nodes, search_weight=search_weight)
        facts.update(kept=False, rules=_rules_facts(rules), layers=lays,
                     connections=jobs,
                     routed=sum(j["status"] == "routed" for j in jobs))
        return facts
    backup = staged.with_name("prefinish_maze_" + staged.name)
    origin = staged.with_name("prerip_maze_" + staged.name)
    cur = drc
    kept_any = False
    checkpoint = None
    touched: set = set()   # nets whose board copper a pass ripped
    rip_nets: set = set()
    ckpt_file = staged.with_name("ckpt_maze_" + staged.name)
    # the island guard (#939): the board before the first pass that ripped
    # board copper, and each ripped net's unconnected count before its rip
    rip_file = staged.with_name("preripnets_maze_" + staged.name)
    rip_start = None    # (drc, kept_any) at rip_file
    rip_base: dict[str, int] = {}
    ripped = []
    if rip_errors:
        # an autorouter's shorts and clearance misses: take out the tracks
        # and vias DRC names, and route those connections again
        kept_uuids: list[str] = []
        ripped = _error_copper(staged, drc, protect=protect_uuids,
                               kept=kept_uuids)
        if ripped or kept_uuids:
            bg0 = geom.BoardGeom.from_file(staged)
            facts["ripped_items"] = describe_copper(bg0, ripped)
            lk = locked_uuids(bg0)
            facts["protected_kept"] = describe_copper(
                bg0, [u for u in kept_uuids if u not in lk])
            if lk & set(kept_uuids):
                # DRC named locked copper (#664): it stays, the error too
                facts["locked_kept"] = describe_copper(
                    bg0, [u for u in kept_uuids if u in lk])
        rip_nets = {v.get("net") for v in drc["violations"]
                    if v.get("check") in RIP_CHECKS and v.get("net")}
        if ripped:
            import route_edit
            shutil.copy2(staged, origin)
            try:
                route_edit.apply_ops(staged, [{"op": "remove", "uuid": u}
                                              for u in ripped])
                if refill:
                    kc.run_drc(cli, staged, refill=True, save_board=True)
                cur = kc.run_drc(cli, staged, parity=parity,
                                 all_track_errors=True)
            except CheckError as exc:
                shutil.copy2(origin, staged)
                origin.unlink(missing_ok=True)
                facts["rip_error"] = str(exc)[:300]
                facts.pop("ripped_items", None)
                ripped, cur = [], drc
        facts["ripped"] = len(ripped)
    try:
        for n in range(passes):
            if not unconnected_pairs(cur, opts["nets"]):
                break
            if n and deadline is not None and time.monotonic() >= deadline:
                break
            shutil.copy2(staged, backup)
            try:
                jobs, after, rules, lays = _one_pass(
                    cli, staged, cur, opts, parity=parity, refill=refill,
                    log=log)
            except CheckError as exc:
                shutil.copy2(backup, staged)
                facts["passes"].append({"error": str(exc)[:300]})
                break
            facts.setdefault("rules", _rules_facts(rules))
            facts.setdefault("layers", lays)
            st: dict[str, int] = {}
            for j in jobs:
                st[j["status"]] = st.get(j["status"], 0) + 1
            entry = {"pass": n + 1, "pairs": len(jobs), "by_status": st,
                     "connections": [{k: v for k, v in j.items() if k != "ops"}
                                     for j in jobs]}
            facts["passes"].append(entry)
            if after is None:
                break
            b_un = sum(unconnected_by_net(cur).values())
            a_un = sum(unconnected_by_net(after).values())
            b_err, a_err = err_counts(cur), err_counts(after)
            grew = {k: [b_err.get(k, 0), v] for k, v in a_err.items()
                    if v > b_err.get(k, 0)}
            entry.update(unconnected_before=b_un, unconnected_after=a_un,
                         grew=grew)
            board_rip = any(j.get("rip_uuids") for j in jobs)
            if board_rip:
                if rip_start is None:
                    shutil.copy2(backup, rip_file)
                    rip_start = (cur, kept_any)
                was = unconnected_by_net(cur)
                for j in jobs:
                    for net in j.get("ripped_nets", []):
                        rip_base.setdefault(net, was.get(net, 0))
            for j in jobs:
                touched.update(j.get("ripped_nets", []))
                if board_rip and j["status"] == "routed":
                    touched.add(j["net"])
            if b_un >= DRC_UNCONNECTED_CAP:
                entry["unconnected_capped"] = True
            if _pass_helped(b_un, a_un, jobs) and not grew:
                entry["kept"] = True
                cur = after
                kept_any = True
            elif board_rip and not grew and n + 1 < passes:
                # ripping other nets' copper opens them up: the next passes
                # route them again, and the whole run must beat the board as
                # it was before this pass, or it goes back to that
                entry["kept"] = "provisional"
                if checkpoint is None:
                    shutil.copy2(backup, ckpt_file)
                    checkpoint = (cur, kept_any)
                cur = after
            else:
                entry["kept"] = False
                shutil.copy2(backup, staged)
                break
        if checkpoint is not None:
            c_drc, c_kept = checkpoint
            c_un = sum(unconnected_by_net(c_drc).values())
            c_err, a_err = err_counts(c_drc), err_counts(cur)
            if sum(unconnected_by_net(cur).values()) >= c_un or any(
                    v > c_err.get(k, 0) for k, v in a_err.items()):
                shutil.copy2(ckpt_file, staged)
                cur, kept_any = c_drc, c_kept
                facts["provisional_undone"] = True
            else:
                kept_any = True
        if rip_start is not None:
            cur, kept_any, touched = _island_guard(
                staged, rip_file, rip_start, rip_base, cur, kept_any,
                touched, facts)
    finally:
        backup.unlink(missing_ok=True)
        ckpt_file.unlink(missing_ok=True)
        rip_file.unlink(missing_ok=True)
    if kept_any and (ripped or touched):
        cur = _sweep_dangling(cli, staged, touched | rip_nets, cur,
                              parity=parity, refill=refill, facts=facts)
    if ripped:
        # the rip is kept only if the board ends up better than it started:
        # no error type grows, and errors + unconnected drop
        b_err, a_err = err_counts(drc), err_counts(cur)
        b_tot = sum(b_err.values()) + start_un
        a_tot = sum(a_err.values()) + sum(unconnected_by_net(cur).values())
        grew = [k for k, v in a_err.items() if v > b_err.get(k, 0)]
        if grew or a_tot >= b_tot:
            shutil.copy2(origin, staged)
            cur, kept_any = drc, False
            facts["rip_kept"] = False
        else:
            kept_any = True
            facts["rip_kept"] = True
        origin.unlink(missing_ok=True)
    facts.update(kept=kept_any, drc_s=round(opts["drc_s"], 1),
                 unconnected_after=sum(unconnected_by_net(cur).values()),
                 errors_after=err_counts(cur))
    if kept_any:
        facts["drc"] = cur
    return facts


def _island_guard(staged, rip_file, rip_start, rip_base, cur, kept_any,
                  touched, facts):
    """A ripped net must end in no more pieces than it had before its rip
    (#939). The board-wide count can drop while one ripped net is left
    split: A joined, B cut in two and no path back for B. Its pieces show as
    DRC unconnected items on B, so B's count is compared with its count
    before the pass that ripped it; when any ripped net's count went up, the
    run goes back to the board before its first rip. At DRC's unconnected
    cap the per-net counts are cut short and say nothing: the split is
    reported, not undone. Returns (cur, kept_any, touched)."""
    import shutil

    now = unconnected_by_net(cur)
    split = {n: [b, now.get(n, 0)] for n, b in sorted(rip_base.items())
             if now.get(n, 0) > b}
    if not split:
        return cur, kept_any, touched
    start_drc, start_kept = rip_start
    if max(sum(now.values()), sum(unconnected_by_net(start_drc).values())) \
            >= DRC_UNCONNECTED_CAP:
        facts["rip_split_capped"] = split
        return cur, kept_any, touched
    shutil.copy2(rip_file, staged)
    facts["rip_split_undone"] = split
    return start_drc, start_kept, set()


def _sweep_dangling(cli, staged, nets, cur, *, parity, refill, facts):
    """Ripping a segment out of another net leaves the rest of its old path
    hanging: remove the free-ended tracks and vias of the nets touched (the
    route_cleanup dangling pass), kept only if DRC holds."""
    import route_cleanup
    import route_edit
    import shutil

    bg = geom.BoardGeom.from_file(staged)
    segs, vias = route_cleanup.parse_items(staged, bg.copper_layers)[:2]
    pads = [route_cleanup.PadItem(p.net, tuple(p.layers), p.center, p.poly)
            for p in bg.pads_of()]
    cache: dict = {}

    def fillfn(net, layer):
        if (net, layer) not in cache:
            cache[(net, layer)] = bg.zone_fill(net, layer)
        return cache[(net, layer)]

    segs = [x for x in segs if x.net in nets]
    vias = [x for x in vias if x.net in nets]
    # #664: a locked stub stays; with no uuid it is an anchor only
    keep_locked = locked_uuids(bg)
    segs = [dataclasses.replace(x, uuid=None) if x.uuid in keep_locked
            else x for x in segs]
    vias = [dataclasses.replace(x, uuid=None) if x.uuid in keep_locked
            else x for x in vias]
    gone_s, gone_v = route_cleanup.find_dangling(segs, vias, pads, fillfn)
    if not gone_s and not gone_v:
        return cur
    keep = staged.with_name("predangle_maze_" + staged.name)
    shutil.copy2(staged, keep)
    try:
        route_edit.apply_ops(staged, [{"op": "remove", "uuid": u}
                                      for u in gone_s + gone_v])
        if refill:
            kc.run_drc(cli, staged, refill=True, save_board=True)
        after = kc.run_drc(cli, staged, parity=parity, all_track_errors=True)
        b_err, a_err = err_counts(cur), err_counts(after)
        if sum(unconnected_by_net(after).values()) > sum(
                unconnected_by_net(cur).values()) or any(
                v > b_err.get(k, 0) for k, v in a_err.items()):
            shutil.copy2(keep, staged)
            facts["dangling_removed"] = 0
            return cur
        facts["dangling_removed"] = len(gone_s) + len(gone_v)
        return after
    except CheckError as exc:
        shutil.copy2(keep, staged)
        facts["dangling_error"] = str(exc)[:200]
        return cur
    finally:
        keep.unlink(missing_ok=True)


RIP_CHECKS = {"shorting_items", "clearance", "tracks_crossing",
              "hole_clearance", "hole_to_hole", "track_width"}


def locked_uuids(bg) -> set[str]:
    """uuids of the board's (locked yes) tracks and vias (#664)."""
    return {x.uuid for x in list(bg.tracks_of()) + list(bg.vias_of())
            if x.locked and x.uuid}


def _error_copper(staged: Path, drc: dict, protect=None,
                  kept: list | None = None) -> list[str]:
    """uuids of the tracks and vias DRC names in a copper error. A uuid in
    `protect` (copper placed on purpose before routing, #655) is never
    taken: it goes to `kept` instead, and the error stays for the report."""
    bg = geom.BoardGeom.from_file(staged)
    mine = {t.uuid for t in bg.tracks_of()} | {v.uuid for v in bg.vias_of()}
    # locked copper (#664) is kept the same way as protected copper
    protect = set(protect or ()) | locked_uuids(bg)
    out: list[str] = []
    for v in drc["violations"]:
        if v.get("severity") != "error" or v.get("check") not in RIP_CHECKS:
            continue
        for it in v.get("items", []):
            u = it.get("uuid")
            if u in mine and u not in out and (
                    it.get("msg", "").startswith(("Track", "Via"))):
                if u in protect:
                    if kept is not None and u not in kept:
                        kept.append(u)
                    continue
                out.append(u)
    return out


def describe_copper(bg, uuids) -> list[dict]:
    """Each named track or via by net and position, so a report says what
    a rip took (#655): {kind, net, layer|layers, at|start+end}."""
    want = set(uuids)
    out = []
    for v in bg.vias_of():
        if v.uuid in want:
            out.append({"kind": "via", "net": v.net,
                        "at": [round(v.at[0], 4), round(v.at[1], 4)],
                        "layers": list(v.layers), "uuid": v.uuid})
    for t in bg.tracks_of():
        if t.uuid in want:
            (x1, y1), (x2, y2) = t.shape.coords[0], t.shape.coords[-1]
            out.append({"kind": "track", "net": t.net, "layer": t.layer,
                        "start": [round(x1, 4), round(y1, 4)],
                        "end": [round(x2, 4), round(y2, 4)],
                        "uuid": t.uuid})
    return out


def _rules_facts(rules: Rules) -> dict:
    facts = {k: v for k, v in rules.__dict__.items() if not isinstance(v, dict)}
    # An HDI board's Rules carry hdilib.Span objects; the report is JSON (#477).
    if facts.get("spans"):
        facts["spans"] = [dataclasses.asdict(s) for s in facts["spans"]]
    facts["via_in_pad_refs"] = sorted(facts.get("via_in_pad_refs", ()))
    facts["sized_nets"] = sorted(facts.get("sized_nets", ()))
    if facts.get("bans") is not None:
        facts["bans"] = facts["bans"].facts()
    # A PairRules holds geometry and memos, not JSON: name its rules (#1501).
    if facts.get("pair") is not None:
        facts["pair"] = [name for _, _, _, name in facts["pair"].rows]
    return facts


def add_routing_args(ap: argparse.ArgumentParser) -> None:
    """The options that say how to route, shared with route_close."""
    ap.add_argument("--nets", default=None, help="only these nets (comma list)")
    ap.add_argument("--layers", default="auto")
    ap.add_argument("--grid", type=float, default=0.1)
    ap.add_argument("--margin", type=float, default=4.0,
                    help="search window beyond the two sides, mm")
    ap.add_argument("--min-width", type=float, default=0.2)
    ap.add_argument("--clearance-margin", type=float, default=0.02)
    ap.add_argument("--via-size", type=float, default=None)
    ap.add_argument("--via-drill", type=float, default=None)
    ap.add_argument("--max-nodes", type=int, default=3_000_000,
                    help="nodes one search may expand before it gives up")
    ap.add_argument("--search-weight", type=float, default=1.0,
                    help="inflate the search's distance estimate: above 1, "
                         "far fewer nodes on long routes, not always shortest")
    ap.add_argument("--via-in-pad-refs", default=None,
                    help="refs (comma list) whose own-net SMD pads may take a"
                         " via: approved filled+capped via-in-pad only; mark"
                         " them filled after (route_edit fill_vias)")


ROUTING_FLAGS = ("nets", "layers", "grid", "margin", "min_width",
                 "clearance_margin", "via_size", "via_drill", "max_nodes",
                 "search_weight", "via_in_pad_refs")


def run(argv: list[str] | None = None):
    started = time.monotonic()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    add_routing_args(ap)
    ap.add_argument("--max-conns", type=int, default=None)
    ap.add_argument("--rip-errors", action="store_true",
                    help="first take out the tracks/vias DRC names in a short,"
                         " clearance, crossing or width error, and route those"
                         " again (kept only if the board ends better)")
    ap.add_argument("--no-rip-board", action="store_true",
                    help="never rip other nets' existing tracks/vias to free"
                         " a walled-in pad (only this run's own routes)")
    ap.add_argument("--skip-conns", default=None,
                    help="JSON list of connections to leave out, each"
                         " [net, [ax, ay], [bx, by]] as a report's connections"
                         " name them; a connection that fails is also left out"
                         " of this run's later passes")
    ap.add_argument("--protect-nets", default=None,
                    help="never rip these nets' copper (comma list)")
    ap.add_argument("--plan-seconds", type=float, default=None,
                    help="stop searching this long after the start and apply"
                         " what was found; the rest come back skipped")
    ap.add_argument("--start-report", default=None,
                    help="a report of this board as it is now, with its zones"
                         " filled: skips the first refill and DRC")
    ap.add_argument("--trace", default=None,
                    help="append one JSON line per connection before searching")
    ap.add_argument("--measure-only", action="store_true",
                    help="refill, save and report; route nothing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--out-report", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    cli = env.find_kicad_cli()
    if cli is None:
        raise CheckError("kicad-cli not found (check_env.py)")
    import route_auto
    work = routelib.fresh_work_dir(Path(args.work_dir).resolve()
                                   if args.work_dir
                                   else pcb.parent / "route_finish")
    staged = route_auto._stage_board(pcb, work)
    parity = staged.with_suffix(".kicad_sch").is_file()
    has_zones = bool(geom.BoardGeom.from_file(staged).zones_of())
    t0 = time.monotonic()
    if args.start_report:
        drc = checklib.load_json(Path(args.start_report), "start report")
    else:
        if has_zones:
            kc.run_drc(cli, staged, refill=True, save_board=True)
        drc = kc.run_drc(cli, staged, parity=parity, all_track_errors=True)
    drc_s = time.monotonic() - t0
    log = (lambda s: print(s, file=sys.stderr)) if args.verbose else None
    if args.measure_only:
        routelib.swap_in(staged, pcb)
        left = unconnected_pairs(drc)
        return ({"script": "route_finish",
                 "status": "pass" if drc["counts"]["total"] == 0
                 else "violations",
                 "board": str(pcb), "counts": drc["counts"],
                 "violations": drc["violations"],
                 "facts": {"kept": False, "drc_s": round(drc_s, 1), "passes": [],
                           "unconnected_left": len(left),
                           "work_dir": str(work)}}, args.out_report)
    on_try = None
    if args.trace:
        trace = Path(args.trace)

        def on_try(job):
            with trace.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps([job["net"], job["a"], job["b"]]) + "\n")
    skip = None
    if args.skip_conns is not None:
        skip = {conn_key(*c) for c in
                checklib.load_json(Path(args.skip_conns), "skip list")}
    facts = finish_staged(
        cli, staged, drc,
        nets=args.nets.split(",") if args.nets else None,
        layers=args.layers, grid=args.grid, margin=args.margin,
        min_width=args.min_width, clearance_margin=args.clearance_margin,
        via_size=args.via_size, via_drill=args.via_drill,
        max_conns=args.max_conns, parity=parity, refill=has_zones,
        dry_run=args.dry_run, rip_errors=args.rip_errors,
        rip_board=not args.no_rip_board, log=log, skip_conns=skip,
        keep_nets=pair_nets(pcb) | set(filter(None,
                                              (args.protect_nets or "").split(","))),
        deadline=(None if args.plan_seconds is None
                  else started + args.plan_seconds),
        on_try=on_try, max_nodes=args.max_nodes,
        search_weight=args.search_weight,
        via_in_pad_refs=set(filter(None, (args.via_in_pad_refs or "")
                                   .split(","))))
    final = facts.pop("drc", None) or drc
    if facts.get("kept") and not args.dry_run:
        routelib.swap_in(staged, pcb)
    left = unconnected_pairs(final)
    status = "pass" if final["counts"]["total"] == 0 else "violations"
    payload = {"script": "route_finish", "status": status, "board": str(pcb),
               "counts": final["counts"],
               "violations": final["violations"],
               "facts": {**facts, "unconnected_left": len(left),
                         "work_dir": str(work)}}
    return payload, args.out_report


def pair_nets(pcb) -> set:
    """Nets of the diff pairs in the constraints.json beside the board."""
    path = Path(pcb).parent / "constraints.json"
    if not path.exists():
        return set()
    pairs = json.loads(path.read_text()).get("diff_pairs", [])
    return {p[k] for p in pairs for k in ("p", "n")}


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("route_finish", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
