#!/usr/bin/env python
"""rules_gen.py - constraints.json -> KiCad custom design rules + net classes (SPEC P5).

KiCad custom DRC rules (`.kicad_dru`) are the enforcement backbone: anything
expressible as a rule becomes a named DRC violation, so the standard DRC gate
(gate.py drc) carries it for free. This script emits:

  1. A `<board>.kicad_dru` = fab-floor BASELINE (from reference/jlc_capabilities.yaml
     for the board's layer count / copper weight - the weight is DERIVED from the
     stackup's own outer copper unless --copper-oz overrides, and an explicit
     mismatch against a named stackup is refused, T6 P5-1) + DESIGN rules from
     constraints:
       - power nets  -> minimum track width (IPC-2152 via check_current) per net,
         floored at the entry's min_width_mm; a "switch_node" entry also gets
         no-via, one-layer and keep-away rules, and (with --pcb) an undeclared
         regulator switch pin's net gets a width at its rating (#924, #923)
       - diff pairs  -> diff_pair_gap rule + differential width (impedance.py +
         stackup); on an inner STRIPLINE layer (a pour on the layers above and
         below, from constraints planes) a layer-scoped width/gap solved with
         impedance.field_stripline (#323); any other inner layer keeps the
         `disallow track` guard (T6 P5-4)
       - high_speed impedance_ohm (single-ended) -> outer microstrip width as
         a fact, and a layer-scoped track_width rule per stripline layer
       - voltages / voltage_pairs -> named HV clearance rules from the IPC-2221
         table in check_creepage (T6 P5-3; carrier hand-authored these before)
     Baseline rules are emitted FIRST and per-net rules LAST: when two rules of the
     same constraint type match one item, the LATER rule wins (LEARNINGS [drc]), so
     the specific per-net rule overrides the generic floor.
     `--check-dru PATH` instead ASSERTS an existing DRU still carries the aiee_*
     fab-floor baseline (T6 P5-5, ladder row 160 - DRUs do get hand-edited).
  2. (optional, --pro) net classes written into `<board>.kicad_pro` net_settings:
     ONE POWER CLASS PER REQUIRED WIDTH (never one class at the widest - that put
     pd-trigger's 20 mA /VDD in the same 1.75 mm class as its 5 A VBUS and would
     have had Freerouting drive 1.75 mm traces into 0.6 mm pads; LEARNINGS
     2026-07-28 [routing][rules_gen][freerouting]; a plane_fed rail's class is
     sized for its taps, power_rules) and one class per differential
     impedance, assigned by netclass_patterns; plus board.design_settings.rules
     minimums and the fab-floor severities from lib/fabfloors.py - the same single
     source board_init writes, so the two files cannot disagree. These drive the
     router (S11) and placement; DRU is the DRC enforcer.
     Every net with an aiee_hv_* clearance rule is ALSO moved into a class carrying
     that clearance (a `_HV_<c>mm` variant of its power/diff class, or `HV_<c>mm`),
     and every class clearance is floored at the board's min_hole_clearance minus
     the class's via ring: the DSN export hands Freerouting netclasses only, so a
     DRU-only clearance cost PCB-0018 a 1 h route with 406 clearance errors.

With the board (--pcb, default the .kicad_pcb beside --pro), its pads add
neck-down rules where a power net meets pads narrower than its width
(neck_rules).

Conditions use `A.NetName == 'NET'` (NOT `A.Net`, which silently matches nothing -
LEARNINGS [drc]). kicad-cli auto-loads the .kicad_dru sitting next to the board.

I/O: SPEC section 6 - argparse, JSON to stdout or --out, exit 0/1/2, no interactivity.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import re
import sys
import traceback
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_current  # noqa: E402  (required_width_mm)
import fabfloors  # noqa: E402  (the single source of fab minimums)
import fabmaterial  # noqa: E402  (which laminate: floors + order)
import impedance as imp  # noqa: E402

REFERENCE = SCRIPTS.parent / "reference"
CAP_FILE = REFERENCE / "jlc_capabilities.yaml"
STACKUP_FILE = REFERENCE / "stackups.yaml"

# Copper finished thickness (mm) by weight, for IPC-2152 width scaling.
CU_OZ_MM = {1.0: 0.035, 0.5: 0.0175, 2.0: 0.070}


# --------------------------------------------------------------- reference data

def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# capability lookup lives in lib/fabfloors.py (board_init uses the same one)
capability_class = fabfloors.capability_class


# --------------------------------------------------------------- DRU rules

class Rule:
    """One .kicad_dru rule."""
    def __init__(self, name: str, constraint: str, minv: float | None = None,
                 condition: str | None = None, layer: str | None = None,
                 severity: str | None = None, extra: str | None = None,
                 comment: str | None = None):
        self.comment = comment
        self.name = name
        self.constraint = constraint
        self.minv = minv
        self.condition = condition
        self.layer = layer
        self.severity = severity
        self.extra = extra

    def render(self) -> str:
        lines = [f'# {ln}' for ln in (self.comment or "").splitlines()]
        lines.append(f'(rule "{self.name}"')
        if self.severity:
            lines.append(f'\t(severity {self.severity})')
        if self.layer:
            lines.append(f'\t(layer {self.layer})')
        if self.minv is not None:
            lines.append(f'\t(constraint {self.constraint} (min {self.minv:.4f}mm))')
        elif self.extra is not None:
            lines.append(f'\t(constraint {self.constraint} {self.extra})')
        else:
            lines.append(f'\t(constraint {self.constraint})')
        if self.condition:
            lines.append(f'\t(condition "{self.condition}")')
        lines.append(')')
        return "\n".join(lines)


def baseline_rules(cap: dict) -> list[Rule]:
    """Fab-floor rules from a jlc_capabilities.yaml design-rule row.

    Minimums come from fabfloors.DRU_FLOOR_KEYS - the same map
    fabfloors.check_dru asserts against - so the writer and the checker
    cannot drift (T6 P5-5)."""
    def floor(name: str) -> float:
        return cap[fabfloors.DRU_FLOOR_KEYS[name]]

    r: list[Rule] = []
    r.append(Rule("aiee_track_width_floor", "track_width", floor("aiee_track_width_floor"),
                  condition="A.Type == 'track'"))
    r.append(Rule("aiee_clearance_floor", "clearance", floor("aiee_clearance_floor")))
    r.append(Rule("aiee_via_drill_floor", "hole_size", floor("aiee_via_drill_floor"),
                  condition="A.Type == 'via'"))
    r.append(Rule("aiee_via_diameter_floor", "via_diameter", floor("aiee_via_diameter_floor"),
                  condition="A.Type == 'via'"))
    r.append(Rule("aiee_annular_floor", "annular_width", floor("aiee_annular_floor"),
                  condition="A.Type == 'via'"))
    r.append(Rule("aiee_edge_clearance_floor", "edge_clearance", floor("aiee_edge_clearance_floor")))
    r.append(Rule("aiee_hole_to_hole_floor", "hole_to_hole", floor("aiee_hole_to_hole_floor")))
    r.append(Rule("aiee_silk_width_floor", "text_thickness", floor("aiee_silk_width_floor"),
                  condition="A.Type == 'text' && (A.Layer == 'F.Silkscreen' || A.Layer == 'B.Silkscreen')"))
    assert {x.name for x in r} == set(fabfloors.DRU_FLOOR_KEYS)
    if fabfloors.OUTER_CLEARANCE_KEY in cap:
        r.append(Rule(fabfloors.OUTER_CLEARANCE_RULE, "clearance",
                      fabfloors.outer_clearance(cap), layer="outer"))
    if fabfloors.INNER_HOLE_KEY in cap:
        r.append(Rule(fabfloors.INNER_HOLE_RULE, "hole_clearance",
                      float(cap[fabfloors.INNER_HOLE_KEY]), layer="inner"))
    return r


def tap_current_a(entry: dict) -> float:
    """What one track of a plane-fed rail carries: its declared
    tap_current_a, else the entry's per-via ampacity (check_current's
    default 0.5 A) - a tap feeds its pad through one via or the pour edge -
    and never more than the rail."""
    tap = entry.get("tap_current_a", entry.get("via_amps",
                                               check_current.VIA_AMPS_DEFAULT))
    return min(float(tap), float(entry["current_a"]))


def power_rules(constraints: dict, cu_mm: float) -> tuple[list[Rule], list[dict]]:
    """Per-power-net minimum track width from IPC-2152 (check_current).

    A plane_fed rail's trunk is its pour (constraints "planes"), so its
    tracks are taps: they are sized for tap_current_a, not the rail's
    current. One width per net at the full 5 A (2.38 mm on 1 oz) would sit
    on every stub into a 0.34 mm HTSSOP pin and is unmeetable; lite-main
    (hifi-receiver-2) declared its 5 A rails at 0.4-0.8 A to get the taps
    through, which hid the real current from every other check."""
    rules: list[Rule] = []
    facts: list[dict] = []
    plane_nets = {pl.get("net") for pl in constraints.get("planes", []) or []}
    for entry in constraints.get("power", []):
        net = entry["net"]
        cur = float(entry["current_a"])
        dt = float(entry.get("dt_c", 10.0))
        fact = {"net": net, "current_a": cur, "dt_c": dt}
        sized = cur
        if entry.get("plane_fed"):
            sized = tap_current_a(entry)
            fact.update({"plane_fed": True, "tap_current_a": sized,
                         "trunk_width_mm": round(check_current.required_width_mm(
                             cur, dt, cu_mm), 4),
                         "plane_declared": net in plane_nets})
        w = check_current.required_width_mm(sized, dt, cu_mm)
        if entry.get("min_width_mm") is not None:
            # a stated floor (a switch node's 0.5 mm at 0.6 A): never under
            # the current's own width, and no dt_c to pick to reach it (#923)
            fact["declared_min_width_mm"] = float(entry["min_width_mm"])
            w = max(w, float(entry["min_width_mm"]))
        safe = re.sub(r"[^A-Za-z0-9]", "_", net).strip("_")
        rules.append(Rule(f"aiee_pwr_width_{safe}", "track_width", round(w, 4),
                          condition=f"A.NetName == '{net}' && A.Type == 'track'"))
        fact["min_width_mm"] = round(w, 4)
        facts.append(fact)
    return rules, facts


# --------------------------------------------------------------- board pads

def board_pads(pcb: Path | None):
    """geom.BoardGeom of the board rules_gen sizes for, or None."""
    if pcb is None or not Path(pcb).is_file():
        return None
    sys.path.insert(0, str(SCRIPTS / "lib"))
    import geom
    return geom.BoardGeom.from_file(pcb)


def neck_rules(bg, power_facts: list[dict], cap: dict
               ) -> tuple[list[Rule], list[dict]]:
    """A neck-down allowance where a power net meets pads narrower than its
    width rule: inside the courtyard of each footprint with such a pad, the
    net's tracks may narrow to the narrowest such pad's short side (floored
    at the fab's track width) - check_current's own pad-exit bound, a track
    no narrower than the copper it leaves. Emitted after the per-net width
    rules (the later rule wins). check_current still judges every neck: one
    longer than its 1 mm pad exit must carry the full width. Returns
    (rules, facts)."""
    rules: list[Rule] = []
    facts: list[dict] = []
    floor = float(cap["min_trace_width_mm"])
    for f in power_facts:
        net, w = f["net"], float(f["min_width_mm"])
        narrow: dict[str, float] = {}
        for p in bg.pads_of(net=net):
            short = min(p.size)
            if short < w - 1e-6:
                narrow[p.ref] = min(narrow.get(p.ref, short), short)
        for ref in sorted(narrow):
            neck = round(max(floor, min(narrow[ref], w)), 4)
            name = f"aiee_pwr_neck_{_safe(net)}_{_safe(ref)}"
            rules.append(Rule(name, "track_width", neck, condition=(
                f"A.NetName == '{net}' && A.Type == 'track' "
                f"&& A.intersectsCourtyard('{ref}')")))
            facts.append({"net": net, "ref": ref, "pad_short_mm":
                          round(narrow[ref], 4), "neck_mm": neck,
                          "width_mm": w, "rule": name})
    return rules, facts


SENSE_MARK = "aiee_sensew"  # route_critical.SENSE_MARK: its sense lines' marks


def sense_width_rules(constraints: dict, cap: dict
                      ) -> tuple[list[Rule], list[dict]]:
    """The sense-line width rule (#1042). A power entry's sense_pads lines
    (route_critical lays each from its sense pad to its declared tap) carry
    a feedback divider's or SENSE pin's microamps, not the rail's current:
    the regulator notes ask for a short, thin trace (TI SNVA721A section
    2.5; LMR33630 SNVSAN3F section 10.1, a 100 kOhm RFBT; knowledge record
    buck-fb-route-rules). So the line's tracks, and only them, may run at
    the fab's minimum width: the rule holds a track of the net that lies
    wholly inside an aiee_sensew_* rule area, the mark route_critical draws
    a hair round each line it lays. A wider rail track is never enclosed.
    Emitted after the power width and neck rules (the later rule wins).
    check_current still judges the rail: it skips only the declared sense
    pads' own path. Returns (rules, facts)."""
    rules: list[Rule] = []
    facts: list[dict] = []
    floor = round(float(cap["min_trace_width_mm"]), 4)
    for entry in constraints.get("power", []) or []:
        if not entry.get("sense_pads"):
            continue
        net = entry["net"]
        name = f"aiee_sense_width_{_safe(net)}"
        rules.append(Rule(name, "track_width", floor, condition=(
            f"A.NetName == '{net}' && A.Type == 'track' "
            f"&& A.enclosedByArea('{SENSE_MARK}_*')"), comment=(
            f"{net}: its sense lines (route_critical's {SENSE_MARK}_* marks)"
            " carry microamps; a thin trace at the fab minimum")))
        facts.append({"net": net, "width_mm": floor, "rule": name,
                      "sense_pads": sorted(entry["sense_pads"])})
    return rules, facts


SWITCH_KEEP_AWAY_MM = 1.0   # switch_node.keep_away_mm default


def switch_node_rules(constraints: dict, bg, power_facts: list[dict],
                      cu_mm: float) -> tuple[list[Rule], list[dict], list[str]]:
    """Switch-node rules (#924, #923 point 6). A power entry with
    "switch_node" ({ref?, layer?, keep_away?, keep_away_mm?} or true) is a
    regulator's SW/LX net: one short run on the regulator's side, so
    rules_gen forbids vias on it and tracks on every other layer, and keeps
    its copper keep_away_mm from the pads of each keep_away ref (mic,
    antenna, crystal). Its width is the entry's own power rule (current_a,
    min_width_mm) and its neck stays the courtyard neck rule.

    With a board, a switch pin's net that no power entry names gets a width
    rule too, at the regulator's rated current at check_current's screen dT
    (the same entry check_current judges it by), so the router no longer
    lays it at signal width: inferred, warned, no via/layer rules - those
    need the designer's word. A switch pin on a part the table doesn't know
    is only warned. Returns (rules, facts, warnings); inferred facts are
    appended to power_facts so they get a net class."""
    rules: list[Rule] = []
    facts: list[dict] = []
    warnings: list[str] = []
    width_of = {f["net"]: f["min_width_mm"] for f in power_facts}
    rows = check_current.switch_rows()
    for entry in constraints.get("power", []) or []:
        sw = entry.get("switch_node")
        if not sw:
            continue
        sw = sw if isinstance(sw, dict) else {}
        net = entry["net"]
        pins = []
        if bg is not None:
            pins = [p for p in bg.pads_of(net=net)
                    if (p.ref == sw["ref"] if sw.get("ref") else
                        check_current.is_switch_pin(p, bg, rows))]
        ref = sw.get("ref") or (pins[0].ref if pins else None)
        layer = sw.get("layer") or next(
            (ly for p in pins for ly in p.layers if ly in ("F.Cu", "B.Cu")),
            "F.Cu")
        cond = f"A.NetName == '{net}'"
        safe = _safe(net)
        rules.append(Rule(f"aiee_sw_novia_{safe}", "disallow via",
                          condition=cond, comment=(
                              f"{net}: switch node{f' of {ref}' if ref else ''}"
                              f" - one run on {layer}, no vias, no other layer")))
        rules.append(Rule(f"aiee_sw_layer_{safe}", "disallow track",
                          condition=f"{cond} && A.Layer != '{layer}'"))
        away = float(sw.get("keep_away_mm", SWITCH_KEEP_AWAY_MM))
        keep = list(sw.get("keep_away") or [])
        for r in keep:
            if any(ch in r for ch in "*?"):
                raise SystemExit(f"power {net}: switch_node keep_away {r!r} "
                                 "is a wildcard; name each part")
            rules.append(Rule(f"aiee_sw_away_{safe}_{_safe(r)}", "clearance",
                              round(away, 4), condition=(
                                  f"{cond} && B.memberOfFootprint('{r}')")))
        if bg is not None and ref is None:
            warnings.append(f"{net} is declared a switch node but no pad on "
                            "it is a switch pin (SW/LX/PH or switch_nodes.yaml); name the "
                            "regulator in switch_node.ref")
        facts.append({"net": net, "ref": ref, "layer": layer,
                      "width_mm": width_of.get(net), "keep_away": keep,
                      "keep_away_mm": away if keep else None})
    if bg is None:
        return rules, facts, warnings
    entries = constraints.get("power", []) or []
    inferred, unrated = check_current.inferred_switch_entries(
        entries, bg, rows)
    for e in inferred:
        net = e["net"]
        w = round(check_current.required_width_mm(e["current_a"], e["dt_c"],
                                                  cu_mm), 4)
        rules.append(Rule(f"aiee_pwr_width_{_safe(net)}", "track_width", w,
                          condition=f"A.NetName == '{net}' && A.Type == 'track'",
                          comment=(f"{net}: {e['from_part']} ({e['part']})'s "
                                   f"switch node, inferred at its rated "
                                   f"{e['current_a']:g} A")))
        power_facts.append({"net": net, "current_a": e["current_a"],
                            "dt_c": e["dt_c"], "min_width_mm": w,
                            "inferred_switch_node": True,
                            "from_part": e["from_part"]})
        facts.append({"net": net, "ref": e["from_part"], "inferred": True,
                      "width_mm": w})
        warnings.append(f"{net} is {e['from_part']} ({e['part']})'s switch "
                        f"node and no power entry names it: sized for its "
                        f"rated {e['current_a']:g} A; declare it in power "
                        "with this board's current and switch_node")
    for u in unrated:
        warnings.append(f"{u['net']} is on {u['ref']}'s {u['pin']} pin, a "
                        "switch node with no power entry and a part "
                        "switch_nodes.yaml doesn't rate: declare it in power "
                        "with switch_node")
    return rules, facts, warnings


def _bga_refs(bg) -> list[str]:
    """Refs of the board's ball grids (none without a board)."""
    if bg is None:
        return []
    sys.path.insert(0, str(SCRIPTS / "lib"))
    import bgalib
    return [b.ref.replace("'", "") for b in bgalib.find_bgas(bg)]


def class_via_diameter(cap: dict) -> float:
    """Netclass via pad: the fab's minimum pad, raised so the minimum drill
    keeps the minimum annular ring. The shipped vendor rows already agree
    (test_fab #443); a workspace vendor file copied from a capability sheet
    may not (a 0.2032 pad on a 0.127 drill leaves 0.038, under a 0.0635
    ring floor)."""
    ring = float(cap.get("min_annular_ring_mm", 0.0))
    return round(max(float(cap["min_via_diameter_mm"]),
                     float(cap["min_via_drill_mm"]) + 2 * ring), 4)


def bga_rules(bg, cap: dict, stackup: dict | None = None
              ) -> tuple[list[Rule], list[dict]]:
    """The escape clearance inside each ball grid's courtyard (#325): the
    netclass clearance (hole clearance less the class via's ring) is wider
    than a 0.8 mm field's streets allow. Same geometry and rule text as
    route_escape (lib/bgalib) - one writer's rule is the other's. On an HDI
    build (#477) the via types' own floors come too, and each field gets the
    HDI line's track and clearance (lib/hdilib)."""
    sys.path.insert(0, str(SCRIPTS / "lib"))
    import bgalib
    import hdilib
    floors = {"track_width": cap["min_trace_width_mm"],
              "clearance": fabfloors.outer_clearance(cap),
              "via_diameter": cap["min_via_diameter_mm"],
              "via_drill": cap["min_via_drill_mm"],
              "annular": cap.get("min_annular_ring_mm", 0.0),
              "hole_clearance": float(cap.get("min_copper_to_hole_mm",
                                              KICAD_HOLE_CLEARANCE_MM))}
    rules: list[Rule] = []
    facts: list[dict] = []
    hdi = hdilib.is_hdi(stackup)
    caps = hdilib.load_hdi_caps() if hdi else {}

    def add_hdi(rows):
        for nm, constraint, value, cond in rows:
            rules.append(Rule(hdilib.HDI_RULE_PREFIX + nm, constraint, value,
                              condition=cond))

    if hdi:
        add_hdi(hdilib.via_type_rules(stackup, caps))
    for b in bgalib.find_bgas(bg):
        ep = bgalib.escape_params(b, floors)
        name = bgalib.rule_name(b.ref)
        clr = float(caps["min_clearance_mm"]) if hdi else ep["clearance"]
        rules.append(Rule(name, "clearance", clr,
                          condition=bgalib.rule_condition(b.ref)))
        if hdi:
            add_hdi(hdilib.field_rules(b.ref, float(caps["min_trace_width_mm"]),
                                       clr, bgalib.rule_condition(b.ref)))
        facts.append({"ref": b.ref, "pitch_mm": b.pitch,
                      "clearance_mm": ep["clearance"],
                      "via_mm": [ep["via_size"], ep["via_drill"]],
                      "method": ep["method"], "rule": name})
    return rules, facts


DIFF_SUFFIXES = [("_P", "_N"), ("_DP", "_DM"), ("+", "-"), ("_H", "_L")]


def detect_diff_pairs(constraints: dict) -> list[dict]:
    """Pair high_speed nets by name suffix. Returns [{p, n, base, impedance_ohm}]."""
    nets = [e["net"] for e in constraints.get("high_speed", [])]
    imp_of = {e["net"]: e.get("impedance_ohm") for e in constraints.get("high_speed", [])}
    pairs: list[dict] = []
    used: set[str] = set()
    # explicit diff_pairs section wins
    for dp in constraints.get("diff_pairs", []):
        if str(dp.get("kind", "")).lower() in ("sense", "kelvin", "dc_sense", "low_frequency"):
            # a DC shunt-sense pair: check_diffpair judges it as "routed
            # together"; it has no impedance, so no width or gap rule either
            used.update([dp["p"], dp["n"]])
            continue
        pairs.append({"p": dp["p"], "n": dp["n"], "base": dp.get("base", dp["p"]),
                      "impedance_ohm": dp.get("impedance_ohm", 90)})
        used.update([dp["p"], dp["n"]])
    for net in nets:
        if net in used:
            continue
        for pa, nb in DIFF_SUFFIXES:
            if net.endswith(pa):
                base = net[:-len(pa)]
                mate = base + nb
                if mate in nets and mate not in used:
                    z = imp_of.get(net) or imp_of.get(mate)
                    if z is None:
                        z = 90 if "USB" in base.upper() else 100
                    pairs.append({"p": net, "n": mate, "base": base,
                                  "impedance_ohm": int(z)})
                    used.update([net, mate])
                break
    return pairs


def _series_dielectric(stack: list[dict], a: int, b: int) -> tuple[float, float]:
    """(height, epsilon_r) of the dielectrics between stack rows a and b: the
    heights summed, epsilon_r as plies in series (h / sum(h_i / er_i))."""
    plies = [ly for ly in stack[a + 1:b] if ly["type"] != "copper"]
    h = sum(float(ly["thickness_mm"]) for ly in plies)
    if h <= 0:
        return 0.0, 4.5
    inv = sum(float(ly["thickness_mm"]) / float(ly.get("epsilon_r", 4.5))
              for ly in plies)
    return h, h / inv


def _gap_kind(stack: list[dict], a: int, b: int) -> str:
    """core | prepreg | "" for the gap between stack rows a and b, as
    geom.Stackup reads a board's: core only when every ply is core."""
    kinds = {ly["type"] for ly in stack[a + 1:b] if ly["type"] in ("core", "prepreg")}
    return "core" if kinds == {"core"} else "prepreg" if kinds else ""


def plane_layers(stackup: dict, constraints: dict) -> set[str]:
    """Copper layers that carry a pour: constraints planes' layers, else
    planes_gen's defaults (the 2nd and 3rd copper on 4+ layers)."""
    names = [ly["name"] for ly in stackup.get("stack", []) if ly["type"] == "copper"]
    planes = constraints.get("planes")
    if planes is None:
        return set(names[1:3]) if len(names) >= 4 else set()
    return {e.get("layer") for e in planes if e.get("layer")}


def stripline_layers(stackup: dict, constraints: dict) -> dict[str, dict]:
    """Inner signal layers that are stripline: a pour on the copper layer
    above AND below, none on the layer itself. Each maps to the geometry
    impedance.field_stripline takes: the trace sits on the dielectric it was
    etched on (h_base, the core; impedance.stripline_sides) and is embedded
    in the other (h_fill) - the same rule check_diffpair solves with."""
    stack = stackup.get("stack") or []
    rows = [i for i, ly in enumerate(stack) if ly["type"] == "copper"]
    names = [stack[i]["name"] for i in rows]
    pours = plane_layers(stackup, constraints)
    out: dict[str, dict] = {}
    for k in range(1, len(rows) - 1):
        name = names[k]
        if name in pours or names[k - 1] not in pours or names[k + 1] not in pours:
            continue
        up = (*_series_dielectric(stack, rows[k - 1], rows[k]),
              _gap_kind(stack, rows[k - 1], rows[k]))
        dn = (*_series_dielectric(stack, rows[k], rows[k + 1]),
              _gap_kind(stack, rows[k], rows[k + 1]))
        (hb, eb), (hf, ef) = imp.stripline_sides(up, dn)
        t = float(stack[rows[k]].get("thickness_mm")
                  or CU_OZ_MM.get(stack[rows[k]].get("copper_oz", 0.5), 0.0175))
        out[name] = {"h_base": round(hb, 4), "er_base": round(eb, 3),
                     "h_fill": round(hf, 4), "er_fill": round(ef, 3),
                     "t": t, "planes": [names[k - 1], names[k + 1]]}
    return out


def pair_gap(h: float, gap_floor: float = 0.0) -> float:
    """The gap a pair is drawn at before its width is solved: clamp(h, 0.13,
    0.30) (tight coupling), never under the fab's clearance `gap_floor`. A
    gap under the clearance is no pair a fab builds: on a board drawn in
    zones the fill shaves one side to the clearance, and the pair is
    unbalanced (issue #741, PCBWay 8L's 0.13 mm against 6 mil)."""
    return max(min(max(h, 0.13), 0.30), gap_floor)


def stripline_geometry(z_target: float, geo: dict, diff: bool,
                       floor: float = 0.0, gap_floor: float = 0.0) -> dict:
    """Width (and, for a pair, the gap) whose stripline impedance on `geo` is
    z_target. A pair's gap is pinned at clamp(h, 0.13, 0.30), h the thinner
    of the two dielectrics (h_base or h_fill, #663), the outer
    pairs' convention (tight coupling, a gap the fab draws; pair_gap: never
    under `gap_floor`, the fab's clearance, #741), and the width
    solved; when that width is under the fab floor the width is pinned at
    the floor and the gap solved instead (a thin prepreg makes a 100 ohm
    pair narrow: JLC's 6-layer L3 wants 0.078 mm at 0.13 mm)."""
    hb, hf, t = geo["h_base"], geo["h_fill"], geo["t"]
    eb, ef = geo["er_base"], geo["er_fill"]
    h_near = min(hb, hf)
    s = pair_gap(h_near, gap_floor) if diff else None
    key = "zdiff" if diff else "z0"

    def z_of(w: float, gap: float | None = s) -> float:
        return imp.field_stripline(w, hb, hf, t, eb, ef, gap)[key]

    # Z falls with width; a guess from the thinner side's height
    w = imp._secant_log(z_of, float(z_target), max(0.05, 0.8 * h_near), 0.02, 2.0)
    out = {"model": "stripline"}
    if w < floor:
        w = floor
        if diff:   # Zdiff grows with the gap: solve 1/Z, which falls
            s = imp._secant_log(lambda g: 1.0 / z_of(w, g), 1.0 / float(z_target),
                                s, 0.05, 1.0)
            out["gap_solved"] = True
        else:
            out["below_fab_floor"] = True
    out.update({"width_mm": round(w, 4),
                "zdiff_ohm" if diff else "z0_ohm": round(z_of(w, s), 2)})
    if diff:
        out["gap_mm"] = round(s, 4)
    return out


def _layer_tag(layer: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", layer.replace(".Cu", ""))


def _neckdown(cond: str, bga_refs: list[str] | None) -> str:
    """An impedance width rule's condition, minus the inside of every ball
    grid's courtyard: escape stubs neck down to the fab floor there (#325's
    dog-bones), as every BGA fan-out does, and the stripline width applies
    from the field's edge on."""
    if not bga_refs:
        return cond
    inside = " || ".join(f"A.intersectsCourtyard('{r}')" for r in bga_refs)
    return f"({cond}) && !({inside})"


def _width_window(w: float, floor: float) -> str:
    """track_width (min/opt/max) for an impedance-sized width: +-10 % (about
    +-6 % in Z), the minimum never under the fab floor."""
    lo = max(floor, w * 0.9)
    return f"(min {lo:.4f}mm) (opt {w:.4f}mm) (max {max(lo, w * 1.1):.4f}mm)"


def diff_pair_rules(pairs: list[dict], stackup: dict, constraints: dict | None = None,
                    floor: float = 0.0, bga_refs: list[str] | None = None,
                    gap_floor: tuple[float, float] = (0.0, 0.0)
                    ) -> tuple[list[Rule], list[dict]]:
    """diff_pair_gap rule + computed width/gap per pair (impedance.py).

    Inner layers: a stripline layer (stripline_layers: a pour above and
    below) gets its own solved width/gap as layer-scoped track_width and
    diff_pair_gap rules; any other inner layer keeps the guard (a named
    disallow-track), since nothing there is a reference to size against.
    gap_floor: (outer, inner) clearance no pair gap is drawn under."""
    rules: list[Rule] = []
    facts: list[dict] = []
    # outer microstrip: dielectric gap to the nearest inner plane + its er
    h, er, cu_oz = outer_microstrip_params(stackup)
    strip = stripline_layers(stackup, constraints or {})
    inner = [ly["name"] for ly in stackup.get("stack", [])
             if ly["type"] == "copper"][1:-1]
    for dp in pairs:
        w, gap = imp.diff_pair(float(dp["impedance_ohm"]), h,
                               imp.CU_OZ_MM.get(cu_oz, 0.035), er,
                               gap=pair_gap(h, gap_floor[0]))
        pinned = w < floor
        if pinned:   # under the fab's min trace: pin the width, solve the gap
            w, gap = imp.diff_pair(float(dp["impedance_ohm"]), h,
                                   imp.CU_OZ_MM.get(cu_oz, 0.035), er,
                                   width=floor)
        safe = re.sub(r"[^A-Za-z0-9]", "_", dp["base"]).strip("_")
        cond = f"A.NetName == '{dp['p']}' || A.NetName == '{dp['n']}'"
        # gap floor: a differential gap smaller than this is a skew/coupling defect
        rules.append(Rule(f"aiee_diff_gap_{safe}", "diff_pair_gap", round(gap, 4),
                          condition=cond))
        layers: dict[str, dict] = {}
        for lname, geo in strip.items():
            try:
                g = stripline_geometry(float(dp["impedance_ohm"]), geo, diff=True,
                                       floor=floor, gap_floor=gap_floor[1])
            except (ValueError, RuntimeError, ZeroDivisionError) as e:
                layers[lname] = {"note": f"stripline not solved: {e}"}
                continue
            layers[lname] = g
            tag = _layer_tag(lname)
            rules.append(Rule(f"aiee_diff_w_{safe}_{tag}", "track_width",
                              layer=f'"{lname}"', condition=_neckdown(cond, bga_refs),
                              extra=_width_window(g["width_mm"], floor)))
            rules.append(Rule(f"aiee_diff_gap_{safe}_{tag}", "diff_pair_gap",
                              layer=f'"{lname}"', condition=_neckdown(cond, bga_refs),
                              extra=f"(min {g['gap_mm'] * 0.9:.4f}mm) "
                                    f"(opt {g['gap_mm']:.4f}mm)"))
        # Inner-layer guard (T6 P5-4): an inner layer with no pour on both
        # sides has no geometry to solve, so a pair segment there is a NAMED
        # DRC violation instead of a silent impedance error. Layer
        # transitions via vias remain legal (vias are not tracks).
        unsized = [n for n in inner if n not in layers or "width_mm" not in layers[n]]
        if unsized and len(unsized) == len(inner):
            rules.append(Rule(f"aiee_diff_outer_only_{safe}", "disallow track",
                              layer="inner", condition=cond))
        else:
            for n in unsized:
                rules.append(Rule(f"aiee_diff_outer_only_{safe}_{_layer_tag(n)}",
                                  "disallow track", layer=f'"{n}"', condition=cond))
        f = {"pair": [dp["p"], dp["n"]], "impedance_ohm": dp["impedance_ohm"],
             "width_mm": w, "gap_mm": gap}
        if pinned:
            f["gap_solved"] = True
        if layers:
            f["layers"] = layers
        facts.append(f)
    return rules, facts


def impedance_rules(constraints: dict, stackup: dict, pairs: list[dict],
                    floor: float = 0.0, bga_refs: list[str] | None = None
                    ) -> tuple[list[Rule], list[dict]]:
    """Single-ended high_speed nets with impedance_ohm: the outer microstrip
    width (a fact: the router's class is left to the board, RF boards draw
    their own) and, per stripline layer, a layer-scoped track_width rule."""
    in_pair = {n for dp in pairs for n in (dp["p"], dp["n"])}
    nets = [e for e in constraints.get("high_speed", [])
            if e.get("impedance_ohm") and e.get("net") not in in_pair]
    if not nets:
        return [], []
    h, er, cu_oz = outer_microstrip_params(stackup)
    t = imp.CU_OZ_MM.get(cu_oz, 0.035)
    strip = stripline_layers(stackup, constraints)
    rules: list[Rule] = []
    facts: list[dict] = []
    by_z: dict[float, dict] = {}
    for e in nets:
        z = float(e["impedance_ohm"])
        if z not in by_z:
            w_out = imp.solve_width(z, h, t, er)
            sol = {"outer_width_mm": round(max(w_out, floor), 4), "layers": {}}
            if w_out < floor:   # a single line can't trade gap for width
                sol["outer_below_fab_floor"] = True
            for lname, geo in strip.items():
                try:
                    g = stripline_geometry(z, geo, diff=False, floor=floor)
                except (ValueError, RuntimeError, ZeroDivisionError) as ex:
                    sol["layers"][lname] = {"note": f"stripline not solved: {ex}"}
                    continue
                sol["layers"][lname] = g
            by_z[z] = sol
        facts.append({"net": e["net"], "impedance_ohm": z, **by_z[z]})
    # one rule per (impedance, layer): its nets ORed in the condition
    for z, sol in by_z.items():
        members = [f["net"] for f in facts if f["impedance_ohm"] == z]
        cond = " || ".join(f"A.NetName == '{n}'" for n in members)
        for lname, g in sol["layers"].items():
            if "width_mm" not in g:
                continue
            rules.append(Rule(f"aiee_z{_mm_tag(z)}_w_{_layer_tag(lname)}", "track_width",
                              layer=f'"{lname}"', condition=_neckdown(cond, bga_refs),
                              extra=_width_window(g["width_mm"], floor)))
    return rules, facts


# --------------------------------------------------------------- HV clearance

def _vtag(volts: float) -> str:
    """48.0 -> '48', 12.5 -> '12p5' (for rule names)."""
    return ("%g" % abs(volts)).replace(".", "p")


def _safe(net: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", net).strip("_")


def hv_clearance_mm(dv: float, coating: str) -> float:
    """Worst-case OUTER-layer IPC-2221 spacing for a dv-volt pair under
    `coating`: max over the track/via/zone row and the exposed-land row -
    the largest requirement check_creepage can adjudicate for any item
    pair, so one net-wide DRU value covers every combination. Table lives
    in check_creepage (single source - no numbers duplicated here)."""
    import check_creepage as cc
    rows = {cc.item_row("track", True, coating), cc.item_row("pad", True, coating)}
    return max(cc.row_clearance_mm(dv, r) for r in rows)


def hv_row_split_mm(dv: float, coating: str) -> tuple[float, float]:
    """(track/via/zone row, exposed-land row) outer IPC-2221 spacing for a
    dv-volt difference under `coating` - the two values check_creepage
    picks between per item pair. Inner layers use B1, never larger than
    either outer row, so the outer track row covers them."""
    import check_creepage as cc
    return (cc.row_clearance_mm(dv, cc.item_row("track", True, coating)),
            cc.row_clearance_mm(dv, cc.item_row("pad", True, coating)))


def voltage_rules(constraints: dict, cap: dict) -> tuple[list[Rule], list[dict]]:
    """HV clearance DRU rules from constraints voltages/voltage_pairs (T6
    P5-3 == P2-4, ladder rows 117/118 context).

    rules_gen previously never read `voltages`, so 48 V spacing on
    lumina-carrier was hand-authored into the DRU at P5 (L0 prose in P2
    recipes). Emits, AFTER the baseline (later-wins ordering):
      - one `aiee_hv_<V>v_<net>` clearance rule per net with |V| above the
        IPC-2221 threshold, conditioned on A.NetName only (clearance is
        pairwise, so a single-net condition enforces the net against
        everything - the shape carrier hand-wrote as HV_48V_clearance);
        sorted ascending by clearance so a pair matching two HV rules
        resolves to the LARGER value (later rule wins). When the
        exposed-land row is the larger one (soldermask: B4 tracks vs A6
        lands) the net rule carries the track row (fab floor at least)
        and an outer-layer `<rule>_land` carries the land row for gaps
        with a pad on either side - check_creepage's per-item split, so
        tented track gaps are not held to a pad's spacing.
      - one pair-scoped rule per explicit voltage_pairs entry (emitted
        last: an explicit pair OVERRIDES the derived per-net value, same
        semantics as check_creepage). A pair declared at or under the
        threshold between nets that carry a derived HV rule restores the
        baseline clearance for that pair - the check_creepage waiver,
        mirrored in the DRU (`aiee_hv_waived_*`).
    Severity stays error; the voltage is encoded in the rule name so P8
    triage can waive consciously. The pad-escape pre-check remains ladder
    row 117's separate item - not folded in here."""
    import check_creepage as cc
    coating = constraints.get("coating", "none")
    if coating not in cc.COATINGS:
        raise ValueError(f"unknown coating {coating!r} (use one of {cc.COATINGS})")
    vmap = cc.voltage_map(constraints)
    threshold = cc.HV_THRESHOLD_V
    rules: list[Rule] = []
    facts: list[dict] = []

    net_rules: list[tuple[float, Rule, dict]] = []
    hv_nets: set[str] = set()
    for net in sorted(n for n, v in vmap.items() if abs(v) > threshold):
        dv = abs(vmap[net])
        clr = hv_clearance_mm(dv, coating)
        name = f"aiee_hv_{_vtag(dv)}v_{_safe(net)}"
        track_clr, land_clr = hv_row_split_mm(dv, coating)
        fact = {"net": net, "voltage": vmap[net],
                "clearance_mm": round(clr, 4),
                "coating": coating, "rule": name}
        if land_clr > track_clr:
            # the exposed-land row (A6) is the larger one, so the net-wide
            # value would hold every tented track/via/zone gap to a pad's
            # number. Split it the way check_creepage adjudicates: the
            # net-wide rule at the track row (never under the fab floor),
            # plus an outer-layer rule at the land row for any gap with a
            # pad on either side.
            base = max(track_clr, fabfloors.outer_clearance(cap))
            land = f"{name}_land"
            net_rules.append((base, Rule(name, "clearance", round(base, 4),
                                         condition=f"A.NetName == '{net}'"),
                              {**fact, "track_clearance_mm": round(base, 4),
                               "land_rule": land}))
            net_rules.append((land_clr, Rule(
                land, "clearance", round(land_clr, 4), layer="outer",
                condition=(f"A.NetName == '{net}' && "
                           "(A.Type == 'Pad' || B.Type == 'Pad')")), None))
        else:
            net_rules.append((clr, Rule(name, "clearance", round(clr, 4),
                                        condition=f"A.NetName == '{net}'"),
                              fact))
        hv_nets.add(net)
    for clr, rule, fact in sorted(net_rules, key=lambda t: (t[0], t[1].name)):
        rules.append(rule)
        if fact is not None:
            facts.append(fact)

    for a, b, pv in cc.voltage_pair_list(constraints):
        if pv > threshold:
            clr = hv_clearance_mm(pv, coating)
            name = f"aiee_hv_{_vtag(pv)}v_{_safe(a)}__{_safe(b)}"
            waiver = False
        elif a in hv_nets or b in hv_nets:
            # explicit sub-threshold pair between derived-HV nets = waiver:
            # restore the fab-floor clearance for exactly this pair
            clr = fabfloors.outer_clearance(cap)
            name = f"aiee_hv_waived_{_safe(a)}__{_safe(b)}"
            waiver = True
        else:
            continue  # sub-threshold pair with no derived rule to override
        rules.append(Rule(name, "clearance", round(clr, 4),
                          condition=f"A.NetName == '{a}' && B.NetName == '{b}'"))
        facts.append({"pair": [a, b], "voltage": pv,
                      "clearance_mm": round(clr, 4), "coating": coating,
                      "rule": name, "waiver": waiver})
    return rules, facts


def package_rules(constraints: dict) -> tuple[list[Rule], list[dict]]:
    """Footprint-scoped clearance from constraints["package_ratings"]:
    [{"ref": "U2", "clearance_mm": 0.1, "why": "...", "layer": "F.Cu"}].

    The owner's statement that a part is rated for its nets at its own pin
    pitch (an LM5069's 48 V pin 0.2 mm from its neighbour, a 100 V 0402's
    0.4 mm pad gap). Emitted after the HV rules, so inside that one
    courtyard it wins over the net-wide IPC value; the condition has
    exactly the shape check_creepage honours as waiver_class
    "dru_footprint_scope" (pairs stay visible as warnings at P8). The
    courtyard form leaves zones out (#649): a pour under the part keeps
    the net-wide clearance."""
    rules: list[Rule] = []
    facts: list[dict] = []
    for e in constraints.get("package_ratings", []) or []:
        ref = str(e["ref"])
        if not re.fullmatch(r"[A-Za-z_]+[0-9]+", ref):
            raise ValueError(f"package_ratings: bad ref {ref!r}")
        clr = float(e["clearance_mm"])
        name = f"aiee_pkg_rating_{_safe(ref)}"
        # pads_only: just the part's own pads. A courtyard term also matches a
        # long track that starts inside it, and every fill it passes.
        term = "memberOfFootprint" if e.get("pads_only") else "intersectsCourtyard"
        # layer: the part's own copper side. The courtyard term is layer-blind,
        # so without it the rating also lowers the other outer layer under the
        # part, below the fab's outer floor (2 oz JLC: 0.15 mm on B.Cu).
        layer = e.get("layer")
        if layer is not None and layer not in ("F.Cu", "B.Cu"):
            raise ValueError(f"package_ratings {ref}: layer {layer!r} is not "
                             "F.Cu or B.Cu")
        cond = f"A.{term}('{ref}') && B.{term}('{ref}')"
        if term == "intersectsCourtyard":
            # #649: the courtyard also holds every pour under the part (a
            # PGND fill 0.2 mm from a 600 V drain patch passed DRC). The
            # rating is for the part's own pins and fanout, not for fills.
            cond += " && A.Type != 'Zone' && B.Type != 'Zone'"
        rules.append(Rule(name, "clearance", round(clr, 4),
                          condition=cond,
                          layer=f'"{layer}"' if layer else None))
        fact = {"ref": ref, "clearance_mm": round(clr, 4),
                "why": e.get("why", ""), "rule": name}
        if layer:
            fact["layer"] = layer
        facts.append(fact)
    return rules, facts


def _domain_expr(side: str, pats: list[str]) -> str:
    """(A.NetName == 'x' || A.NetName == '/usb/*'). KiCad's == takes * and ?
    (checked live), case-insensitively, so a glob can only catch more than
    check_isolation's, never less. It has no [..] set: refused, not misread."""
    for p in pats:
        if "[" in p or "'" in p:
            raise ValueError(f"isolation net pattern {p!r}: a DRU condition has "
                             "no [..] set and no quote; list the nets")
    return "(" + " || ".join(f"{side}.NetName == '{p}'" for p in pats) + ")"


def _mm_up(mm: float) -> float:
    """To the rule's 4 decimals, upward: a rule never sits under its table."""
    return math.ceil(round(mm * 1e4, 6)) / 1e4


def _hv_floor(hv_facts: list[dict], nets_a: list[str], nets_b: list[str]) -> float:
    """The largest HV clearance a barrier rule would override (the later rule
    wins, so it must carry it): a net-wide rule on a net of either domain, or
    a pair rule across the barrier. A waiver lowers nothing."""
    def hit(net, pats):
        return any(fnmatch.fnmatchcase(net, p) for p in pats)
    floor = 0.0
    for f in hv_facts:
        if f.get("waiver"):
            continue
        if "net" in f:
            on = hit(f["net"], nets_a + nets_b)
        else:
            a, b = f["pair"]
            on = (hit(a, nets_a) and hit(b, nets_b)) or (hit(a, nets_b) and hit(b, nets_a))
        if on:
            floor = max(floor, float(f["clearance_mm"]))
    return floor


def isolation_rules(constraints: dict, cap: dict, stackup: dict | None,
                    hv_facts: list[dict], bg=None) -> tuple[list[Rule], list[dict]]:
    """Barrier clearance from constraints `isolation` (#595), from the same
    isotables.requirement() numbers check_isolation judges with.

    One rule per barrier and layer class, scoped to the pair of domains, so a
    domain's own nets keep their ordinary spacing:
      aiee_iso_<barrier>_outer   the creepage requirement as a straight distance
      aiee_iso_<barrier>_inner   the inner rule (only with inner copper)
      aiee_iso_part_<ref>_<barrier>   a rated part's own pads, pad to pad only

    Slots: a DRU clearance is a straight distance and can't see a path round a
    slot, so the outer rule never takes slot credit: it's the full creepage
    number, which also keeps the gap honest for a slot that doesn't run past the
    copper. A board whose slot does earn the creepage declares
    slot_earns_creepage on the barrier; its rule drops to the clearance
    requirement, and check_isolation alone judges the creepage round the slot.

    The part exception goes only to a part whose declared rating covers the
    barrier: an unrated or short part stays flagged by DRC, as it is by
    check_isolation. It's memberOfFootprint, not the courtyard: a track through
    the courtyard is still a crossing. These rules follow the HV and package
    rules, so neither can lower a barrier inside a courtyard."""
    iso = constraints.get("isolation")
    if not iso:
        return [], []
    import check_isolation as ci
    import isotables
    domains = iso.get("domains") or {}
    barriers = iso.get("barriers") or []
    try:
        if not barriers:
            raise ci.CheckError("isolation.barriers must list at least one barrier")
        netmap = ci.domain_map(iso, sorted(bg.nets))[0] if bg is not None else None
        for b in barriers:
            ci._check_barrier(b, domains)
            for d in b["between"]:
                if not (domains[d] or {}).get("nets"):
                    raise ci.CheckError(f"isolation domain {d!r} needs nets")
        reqs = [isotables.requirement(b) for b in barriers]
    except (ci.CheckError, isotables.IsoTableError) as exc:
        raise ValueError(f"isolation: {exc}") from exc
    has_inner = bool(stackup) and sum(
        1 for ly in stackup["stack"] if ly["type"] == "copper") > 2
    span = ci.spanning_parts(bg, netmap) if bg is not None else None
    rated = iso.get("parts") or {}
    rules: list[Rule] = []
    part_rules: list[Rule] = []
    facts: list[dict] = []
    for b, req in zip(barriers, reqs):
        da, db = b["between"]
        nets_a, nets_b = domains[da]["nets"], domains[db]["nets"]
        cond = f"{_domain_expr('A', nets_a)} && {_domain_expr('B', nets_b)}"
        tag = _safe(str(b["name"]))
        hv = _hv_floor(hv_facts, nets_a, nets_b)
        cl, cr = req["clearance"], req["creepage"]
        slotted = bool(b.get("slot_earns_creepage"))
        table = cl if slotted else cr
        outer = _mm_up(max(table["mm"], hv))
        why = (f"barrier {b['name']} {da}/{db}, outer layers: "
               + (f"clearance {cl['mm']:.3f} mm, because slot_earns_creepage says a "
                  f"slot earns the creepage ({cr['mm']:.3f} mm); check_isolation "
                  "judges that path, DRC can't"
                  if slotted else
                  f"creepage {cr['mm']:.3f} mm as a straight distance, no slot credit; "
                  "a DRU clearance can't see a path round a slot")
               + f"\nbasis: {table['basis']}"
               + ("" if table["verified"] else "; table row or rule not read from a document")
               + (f"\nraised to the net's HV clearance {hv:.3f} mm" if hv > table["mm"] else ""))
        name = f"aiee_iso_{tag}_outer"
        rules.append(Rule(name, "clearance", outer, condition=cond, layer="outer",
                          comment=why))
        fact = {"barrier": b["name"], "between": [da, db], "rule": name,
                "layers": "outer", "clearance_mm": outer,
                "basis": table["basis"], "verified": bool(table["verified"]),
                "slot_earns_creepage": slotted, "hv_floor_mm": hv or None}
        facts.append(fact)
        if has_inner:
            r = req["inner"]
            inner = _mm_up(max(r["mm"], hv))
            name = f"aiee_iso_{tag}_inner"
            rules.append(Rule(
                name, "clearance", inner, condition=cond, layer="inner",
                # undeclared, the number is from memory and check_isolation only
                # warns on it; DRC says the same, and pours still keep it
                severity=None if r["declared"] or hv > r["mm"] else "warning",
                comment=f"barrier {b['name']} {da}/{db}, inner layers: solid "
                        f"insulation {r['mm']:.3f} mm\nbasis: {r['basis']}"))
            facts.append({"barrier": b["name"], "between": [da, db], "rule": name,
                          "layers": "inner", "clearance_mm": inner,
                          "basis": r["basis"], "verified": bool(r["verified"]),
                          "hv_floor_mm": hv or None})
        for ref, rating in sorted(rated.items()):
            if not re.fullmatch(r"[A-Za-z_]+[0-9]+", str(ref)):
                raise ValueError(f"isolation.parts: bad ref {ref!r}")
            if span is not None and not {da, db} <= set(span.get(ref, ())):
                continue
            short = ci.rating_shortfalls(rating, b, req)
            if short:
                fact.setdefault("parts_refused", []).append({"ref": ref, "shortfalls": short})
                continue
            floor = _mm_up(fabfloors.outer_clearance(cap))
            name = f"aiee_iso_part_{_safe(ref)}_{tag}"
            part_rules.append(Rule(
                name, "clearance", floor,
                condition=f"A.memberOfFootprint('{ref}') && B.memberOfFootprint('{ref}')"
                          f" && {cond}",
                comment=f"{ref}'s own pads across barrier {b['name']}: its declared "
                        f"rating covers it; source: {rating.get('source', 'none given')}"))
            fact.setdefault("parts_excepted", []).append({"ref": ref, "rule": name})
    return rules + part_rules, facts


def outer_microstrip_params(stackup: dict) -> tuple[float, float, float]:
    """(h, epsilon_r, outer_copper_oz) for outer-layer microstrip from a stackup.

    h = thickness of the dielectric between the outer copper and the next
    (reference) copper; er = its epsilon_r; oz = outer copper weight.
    """
    stack = stackup["stack"]
    outer_oz = stack[0].get("copper_oz", 1.0)
    # first dielectric after the top copper
    for layer in stack[1:]:
        if layer["type"] != "copper":
            return float(layer["thickness_mm"]), float(layer.get("epsilon_r", 4.5)), outer_oz
    return 0.2104, 4.05, outer_oz  # fallback (should not happen for >=2 copper)


# --------------------------------------------------------------- net classes

def _mm_tag(mm: float) -> str:
    """0.4 -> '0p4' (for class names)."""
    return ("%g" % round(mm, 4)).replace(".", "p")


def power_class_name(width_mm: float) -> str:
    """Class name for a power width: 1.75 -> 'Pwr_1p75mm'."""
    return f"Pwr_{_mm_tag(width_mm)}mm"


def net_classes(constraints: dict, power_facts: list[dict],
                diff_facts: list[dict], cap: dict,
                hv_facts: list[dict] | None = None
                ) -> tuple[list[dict], list[dict]]:
    """Build net_settings classes + netclass_patterns for the .kicad_pro.

    Power nets are bucketed BY THEIR OWN required width (one class per
    distinct width); a net whose IPC-2152 width is at or under the Default
    class width simply joins Default. Flattening every power net into one
    class at the widest width is a routing defect, not a conservatism: the
    netclass width is what the DSN export hands Freerouting, so a 20 mA rail
    inherited the 5 A trunk's 1.75 mm and could not enter its own pads.

    HV nets (voltage_rules facts) get the same treatment for clearance: each
    moves into a variant of the class it would otherwise have, at
    max(class clearance, its HV clearance). A pair-scoped HV rule raises both
    nets against everything - stricter than the DRU, never looser - and a
    waiver lowers nothing (a netclass cannot hold a per-pair value). The two
    nets of a diff pair take the same value so they stay in one class.
    """
    classes: list[dict] = []
    patterns: list[dict] = []

    def base_class(name: str, track: float, clearance: float,
                   dpw: float = 0.2, dpg: float = 0.25) -> dict:
        return {
            "name": name, "clearance": round(clearance, 4), "track_width": round(track, 4),
            "via_diameter": class_via_diameter(cap), "via_drill": cap["min_via_drill_mm"],
            "diff_pair_width": round(dpw, 4), "diff_pair_gap": round(dpg, 4),
            "diff_pair_via_gap": round(dpg, 4),
            "microvia_diameter": 0.3, "microvia_drill": 0.1, "line_style": 0,
            "bus_width": 12, "wire_width": 6,
            "pcb_color": "rgba(0, 0, 0, 0.000)",
            "schematic_color": "rgba(0, 0, 0, 0.000)",
        }

    default_track = default_class(cap)["track_width"]
    buckets: dict[float, list[dict]] = {}
    for f in power_facts:
        w = round(max(float(f["min_width_mm"]), default_track), 4)
        buckets.setdefault(w, []).append(f)
    for w in sorted(buckets):
        if w <= default_track + 1e-9:
            cname = "Default"          # thin rails need no wider class
        else:
            cname = power_class_name(w)
            # the fab floor, not a blanket 0.2: a power net that lands on a
            # fine-pitch part (CH224Q VBUS, 0.176 mm between pads) cannot
            # meet 0.2 at its own pads. Spacing a voltage needs comes from
            # voltages/voltage_pairs (the _HV_ variants below).
            classes.append(base_class(cname, w, fabfloors.outer_clearance(cap)))
        for f in buckets[w]:
            f["netclass"] = cname       # traceability in the JSON report
            f["class_width_mm"] = w
            patterns.append({"netclass": cname, "pattern": f["net"]})
    for f in diff_facts:
        cname = f"Diff{f['impedance_ohm']}"
        if not any(c["name"] == cname for c in classes):
            classes.append(base_class(cname, f["width_mm"], fabfloors.outer_clearance(cap),
                                      dpw=f["width_mm"], dpg=f["gap_mm"]))
        for n in f["pair"]:
            patterns.append({"netclass": cname, "pattern": n})

    hv: dict[str, float] = {}
    for f in hv_facts or []:
        if f.get("waiver"):
            continue
        for n in ([f["net"]] if "net" in f else f["pair"]):
            hv[n] = max(hv.get(n, 0.0), float(f["clearance_mm"]))
    for f in diff_facts:
        clr = max(hv.get(n, 0.0) for n in f["pair"])
        if clr:
            hv.update({n: clr for n in f["pair"]})
    by_name = {c["name"]: c for c in classes}
    by_name["Default"] = default_class(cap)
    assigned = {p["pattern"]: p for p in patterns}
    for net in sorted(hv):
        p = assigned.get(net)
        if p is None:
            p = assigned[net] = {"netclass": "Default", "pattern": net}
            patterns.append(p)
        base = by_name[p["netclass"]]
        clr = round(hv[net], 4)
        if clr <= base["clearance"] + 1e-9:
            continue
        prefix = "HV" if base["name"] == "Default" else base["name"] + "_HV"
        cname = f"{prefix}_{_mm_tag(clr)}mm"
        if cname not in by_name:
            by_name[cname] = {**base, "name": cname, "clearance": clr}
            classes.append(by_name[cname])
        p["netclass"] = cname
    for f in power_facts:
        f["netclass"] = assigned[f["net"]]["netclass"]
    used = {p["netclass"] for p in patterns}
    return [c for c in classes if c["name"] in used], patterns


# KiCad's own board-setup min_hole_clearance, which a project file carries
# unless someone sets it; hwde does not set it.
KICAD_HOLE_CLEARANCE_MM = 0.25


def hole_clearance_floor(cls: dict, hole_clearance: float) -> float:
    """Least class clearance that keeps a track laid at it outside the
    board's hole clearance of the class's own via: hole clearance minus the
    via ring. On 4layer_1oz (0.1016 clearance, 0.45/0.2 via) that is 0.125,
    not 0.1016, and Freerouting knows nothing of hole clearance."""
    ring = (cls["via_diameter"] - cls["via_drill"]) / 2
    return round(hole_clearance - ring, 4)


def default_class(cap: dict) -> dict:
    return {
        "name": "Default", "clearance": fabfloors.outer_clearance(cap),
        "track_width": max(cap["min_trace_width_mm"], 0.2),
        "via_diameter": class_via_diameter(cap), "via_drill": cap["min_via_drill_mm"],
        "diff_pair_width": 0.2, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
        "microvia_diameter": 0.3, "microvia_drill": 0.1, "line_style": 0,
        "bus_width": 12, "wire_width": 6,
        "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)",
    }


def update_pro(pro_path: Path, classes: list[dict], patterns: list[dict],
               cap: dict) -> None:
    """Read-modify-write the .kicad_pro: net_settings + design-rule minimums.

    Every class clearance is raised to hole_clearance_floor (the pro's own
    min_hole_clearance, else KiCad's 0.25 mm); a Diff class no higher than
    its pair gap. Keeps the rest of the (minimal, hand-rolled) pro intact -
    only touches net_settings, board.design_settings.rules and the fab-floor
    severities (LEARNINGS [kicad]: a minimal pro is the DRC authority; do not paste a
    full default blob). Floors + severities come from lib/fabfloors.py, the
    same source board_init writes, and are asserted after the merge."""
    pro = json.loads(pro_path.read_text(encoding="utf-8")) if pro_path.exists() else {}
    board = pro.setdefault("board", {})
    ds = board.setdefault("design_settings", {})
    # The floors being written win: the class floor below must see this run's
    # min_hole_clearance, not the one left in the file by the previous run.
    hole_clr = float({**(ds.get("rules") or {}), **fabfloors.pro_rules(cap)}
                     .get("min_hole_clearance", KICAD_HOLE_CLEARANCE_MM))
    all_classes = []
    for c in [default_class(cap)] + classes:
        floor = hole_clearance_floor(c, hole_clr)
        if c["name"].startswith("Diff"):
            # the pair's own gap is a clearance between its two nets
            floor = min(floor, c["diff_pair_gap"])
        all_classes.append({**c, "clearance": max(c["clearance"], floor)})
    pro["net_settings"] = {
        "classes": all_classes,
        "meta": {"version": 3},
        "net_colors": None,
        "netclass_assignments": None,
        "netclass_patterns": patterns,
    }
    ds["rules"] = {**(ds.get("rules") or {}), **fabfloors.pro_rules(cap)}
    ds["rule_severities"] = {**(ds.get("rule_severities") or {}),
                             **fabfloors.pro_rule_severities()}
    bad = fabfloors.check_pro(pro, cap)
    if bad:
        raise RuntimeError("project file would ship sub-fab floors: "
                           + "; ".join(bad))
    pro_path.write_text(json.dumps(pro, indent=2), encoding="utf-8")


# --------------------------------------------------------------- render / main

DRU_HEADER = ("(version 1)\n"
              "# Generated by hwde rules_gen.py from constraints + jlc_capabilities.\n"
              "# Baseline (fab floor) first, per-net design rules last (later rule wins).\n")


def unique_rule_names(rules: list[Rule]) -> list[dict]:
    """Give every rule its own name, in place. _safe folds every
    non-alphanumeric to '_', so A_SPK_L+ and A_SPK_L- both become
    aiee_pwr_width_A_SPK_L; the second and later of a name get _2, _3.
    Returns [{from, to}] for the report."""
    seen: set[str] = {r.name for r in rules}
    count: dict[str, int] = {}
    renamed: list[dict] = []
    for r in rules:
        n = count[r.name] = count.get(r.name, 0) + 1
        if n == 1:
            continue
        k = n
        while f"{r.name}_{k}" in seen:
            k += 1
        new = f"{r.name}_{k}"
        seen.add(new)
        renamed.append({"from": r.name, "to": new})
        r.name = new
    return renamed


def render_dru(rules: list[Rule]) -> str:
    return DRU_HEADER + "\n".join(r.render() for r in rules) + "\n"


def build(constraints: dict, cap: dict, stackup: dict, baseline_only: bool,
          bg=None) -> tuple[list[Rule], dict]:
    """bg: the board (geom.BoardGeom), when there is one: its pads give the
    neck-down rules and the power classes' clearance."""
    outer_oz = cap.get("outer_copper_oz", 1.0)
    cu_mm = CU_OZ_MM.get(outer_oz, 0.035)
    rules = baseline_rules(cap)
    report: dict = {"power": [], "diff_pairs": [], "classes": [], "patterns": [],
                    "hv_rules": [], "necks": []}
    if not baseline_only:
        pr, pf = power_rules(constraints, cu_mm)
        sr, sf, sw_warn = switch_node_rules(constraints, bg, pf, cu_mm)
        nr, nf = neck_rules(bg, pf, cap) if bg is not None else ([], [])
        snr, snf = sense_width_rules(constraints, cap)
        pairs = detect_diff_pairs(constraints)
        floor = float(cap.get("min_trace_width_mm", 0.0) or 0.0)
        bga_refs = _bga_refs(bg)
        gap_floor = ((fabfloors.outer_clearance(cap), float(cap["min_clearance_mm"]))
                     if "min_clearance_mm" in cap else (0.0, 0.0))
        dr, df = diff_pair_rules(pairs, stackup, constraints, floor, bga_refs,
                                 gap_floor)
        zr, zf = impedance_rules(constraints, stackup, pairs, floor, bga_refs)
        hr, hf = voltage_rules(constraints, cap)
        kr, kf = package_rules(constraints)
        br, bf = bga_rules(bg, cap, stackup) if bg is not None else ([], [])
        # HV and package-rating clearances stay last: KiCad's later rule
        # wins, and a courtyard's escape clearance must never undercut them
        ir, if_ = isolation_rules(constraints, cap, stackup, hf, bg)
        # switch-node width rules ride with the power widths (the necks
        # after them win in the courtyard); their via/layer/keep-away
        # rules come after the necks, before HV and isolation
        sw_width = [r for r in sr if r.constraint == 'track_width']
        sw_other = [r for r in sr if r.constraint != 'track_width']
        # the sense-line widths (#1042) come after the necks: they win
        # on the line's own tracks, inside its aiee_sensew_* marks
        rules += pr + sw_width + nr + snr + sw_other + dr + zr + br + hr + kr + ir
        report["package_rules"] = kf
        report["isolation_rules"] = if_
        report["necks"] = nf
        report["sense_widths"] = snf
        report["switch_nodes"] = sf
        report["switch_warnings"] = sw_warn
        report["bga_escape"] = bf
        report["renamed_rules"] = unique_rule_names(rules)
        classes, patterns = net_classes(constraints, pf, df, cap, hf)
        report["power"] = pf
        report["diff_pairs"] = df
        report["impedance"] = zf
        report["stripline_layers"] = stripline_layers(stackup, constraints)
        report["classes"] = classes
        report["patterns"] = patterns
        report["hv_rules"] = hf
    return rules, report


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--constraints", help="constraints.json (high_speed/power/diff_pairs)")
    ap.add_argument("--layers", type=int, default=4)
    ap.add_argument("--copper-oz", type=float, default=None,
                    help="outer copper weight (default: derived from the "
                         "stackup's own outer copper, like board_init; an "
                         "explicit value contradicting an explicitly named "
                         "stackup is refused)")
    ap.add_argument("--stackup", help="stackup name from stackups.yaml "
                    "(default: defaults[<layers>])")
    ap.add_argument("--out-dru", help="write the .kicad_dru here")
    ap.add_argument("--pro", help="also update this .kicad_pro (net classes + minimums)")
    ap.add_argument("--pcb", help="the board, for its pads: neck-down rules "
                    "where a power net meets pads narrower than its width "
                    "(default: the .kicad_pcb beside --pro, when there is one)")
    ap.add_argument("--baseline-only", action="store_true",
                    help="emit only the fab-floor baseline (for template generation)")
    ap.add_argument("--check-dru", metavar="PATH",
                    help="check an EXISTING .kicad_dru for the aiee_* fab-floor "
                         "baseline instead of generating (exit 1 on failures); "
                         "resolves the capability row from --layers/--stackup/"
                         "--copper-oz like generation does")
    ap.add_argument("--out", help="write the JSON report here instead of stdout")
    args = ap.parse_args(argv)

    try:
        # Stackup resolution FIRST: the copper weight (-> capability row and
        # every fab floor) is derived from it (T6 P5-1). rules_gen previously
        # defaulted --copper-oz to 1.0 REGARDLESS of --stackup, silently
        # selecting sub-fab 1-oz floors and double-width IPC-2152 power
        # classes on every 2-oz board - the exact defect family T1 killed in
        # board_init (board-setup.md's own step-3 command omits the flag).
        stacks = load_yaml(STACKUP_FILE)
        stk_name = args.stackup or stacks["defaults"].get(args.layers)
        stackup = stacks["stackups"].get(stk_name) if stk_name else None
        if stackup is not None and stackup.get("available") is False \
                and not args.baseline_only:
            ret = stackup.get("retired") or {}
            raise SystemExit(
                f"stackup {stk_name} is marked available: false in "
                f"stackups.yaml - {ret.get('reason', 'not offered by JLC')}; "
                f"use one of: {', '.join(ret.get('replacements') or []) or 'see stackups.yaml'}")

        derived_oz = None
        if stackup is not None:
            coppers = [ly for ly in stackup["stack"] if ly["type"] == "copper"]
            if coppers:
                derived_oz = float(coppers[0].get("copper_oz", 1.0))
        if args.copper_oz is not None and args.stackup and derived_oz is not None \
                and abs(args.copper_oz - derived_oz) > 1e-9:
            raise SystemExit(
                f"--copper-oz {args.copper_oz:g} contradicts stackup "
                f"{stk_name} (outer copper {derived_oz:g} oz). Drop the flag "
                f"- it is derived from the stackup - or pick the matching "
                f"stackup; a board must never quietly get another copper "
                f"weight's fab floors (T1)")
        outer_oz = args.copper_oz if args.copper_oz is not None else \
            (derived_oz if derived_oz is not None else 1.0)

        # Laminate: the named stackup's, checked against the board's own
        # stackup block when there is a board - a Rogers board must not get
        # FR-4 floors or FR-4 diff-pair geometry (SI-09 at design time).
        if stk_name and stackup is None and not args.baseline_only:
            why = fabmaterial.not_offered(fabmaterial.family_of_name(stk_name))
            raise SystemExit(f"stackup {stk_name!r} not in stackups.yaml"
                             + (f" - {why}" if why else ""))
        try:
            material = (fabmaterial.stackup_family(stackup)
                        if stackup is not None else fabmaterial.FR4)
        except fabmaterial.MaterialError as exc:
            raise SystemExit(f"stackup {stk_name}: {exc}") from exc
        board_pcb = Path(args.pcb) if args.pcb else (
            Path(args.pro).with_suffix(".kicad_pcb") if args.pro else None)
        if board_pcb is not None and board_pcb.is_file() \
                and fabmaterial.board_materials(board_pcb):
            board_mat = fabmaterial.board_family(board_pcb)
            if board_mat != material:
                hint = ""
                if not args.stackup:
                    same = [n for n, s in stacks["stackups"].items()
                            if s.get("available") is not False
                            and s.get("layers") == args.layers
                            and s.get("material_family") == board_mat]
                    hint = (f"; pass --stackup ({', '.join(same)})" if same
                            else "")
                raise SystemExit(
                    f"{board_pcb.name} is built on {board_mat} (its stackup "
                    f"block) but rules_gen resolved stackup {stk_name} "
                    f"({material}){hint}. Fab floors and impedance geometry "
                    "would be the wrong laminate's")
        try:
            cls, cap = fabfloors.profile(args.layers, outer_oz,
                                         material=material,
                                         vendor=fabfloors.vendor_of(stackup))
        except fabfloors.FabFloorError as exc:
            raise SystemExit(str(exc)) from exc

        if args.check_dru:
            # T6 P5-5 (ladder row 160): assert a (possibly hand-edited) DRU
            # still carries the aiee_* fab floors. Read-only; exit 1 = the
            # named failures below, for the fix-loop / a later gate wiring.
            failures = fabfloors.check_dru(
                Path(args.check_dru).read_text(encoding="utf-8"), cap)
            result = {
                "script": "rules_gen", "mode": "check_dru",
                "status": "pass" if not failures else "fail",
                "dru": args.check_dru, "capability_class": cls,
                "copper_oz": outer_oz, "failures": failures,
            }
            out = json.dumps(result, indent=2)
            if args.out:
                Path(args.out).write_text(out, encoding="utf-8")
            else:
                print(out)
            return 0 if not failures else 1

        if stackup is None and not args.baseline_only:
            # only needed for diff pairs; fall back to the first AVAILABLE
            # stackup with a matching layer count, else the first available
            stackup = next(
                (s for s in stacks["stackups"].values()
                 if s.get("available") is not False
                 and s.get("layers") == args.layers),
                next(s for s in stacks["stackups"].values()
                     if s.get("available") is not False))

        constraints = {}
        if args.constraints:
            constraints = json.loads(Path(args.constraints).read_text(encoding="utf-8"))

        pcb = Path(args.pcb) if args.pcb else (
            Path(args.pro).with_suffix(".kicad_pcb") if args.pro else None)
        if args.pcb and not pcb.is_file():
            raise SystemExit(f"--pcb not found: {pcb}")
        bg = None if args.baseline_only else board_pads(pcb)
        rules, report = build(constraints, cap, stackup or {}, args.baseline_only,
                              bg)
        dru_text = render_dru(rules)

        if args.out_dru:
            Path(args.out_dru).write_text(dru_text, encoding="utf-8")
        if args.pro and not args.baseline_only:
            update_pro(Path(args.pro), report["classes"], report["patterns"], cap)

        result = {
            "script": "rules_gen", "status": "pass",
            "capability_class": cls, "stackup": stk_name,
            "copper_oz": outer_oz, "material": material,
            "rule_count": len(rules),
            "rule_names": [r.name for r in rules],
            "renamed_rules": report.get("renamed_rules", []),
            "power": report["power"], "diff_pairs": report["diff_pairs"],
            "impedance": report.get("impedance", []),
            "stripline_layers": report.get("stripline_layers", {}),
            "hv_rules": report["hv_rules"],
            "isolation_rules": report.get("isolation_rules", []),
            "package_rules": report.get("package_rules", []),
            "necks": report["necks"],
            "sense_widths": report.get("sense_widths", []),
            "switch_nodes": report.get("switch_nodes", []),
            "warnings": [
                f"{f['net']} is plane_fed but constraints planes has no pour "
                "for it: its taps are sized for the plane that isn't there"
                for f in report["power"]
                if f.get("plane_fed") and not f.get("plane_declared")]
            + report.get("switch_warnings", []),
            "pcb": str(pcb) if bg is not None else None,
            "classes": [c["name"] for c in report["classes"]],
            "class_clearances": {c["name"]: c["clearance"]
                                 for c in report["classes"]},
            "out_dru": args.out_dru, "pro": args.pro if not args.baseline_only else None,
        }
        if not args.out_dru:
            result["dru"] = dru_text
        out = json.dumps(result, indent=2)
        if args.out:
            Path(args.out).write_text(out, encoding="utf-8")
        else:
            print(out)
        return 0
    except SystemExit as e:
        print(json.dumps({"script": "rules_gen", "status": "error", "error": str(e)}))
        return 2
    except Exception:
        print(json.dumps({"script": "rules_gen", "status": "error",
                          "error": traceback.format_exc()}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
