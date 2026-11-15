"""check_creepage.py - same-layer clearance for high-voltage nets (SPEC 6.3, P8).

One concern: IPC-2221 electrical conductor spacing. For every net pair sitting
more than 30 V apart - derived from constraints.json `voltages`, or declared
explicitly via `voltage_pairs` - every same-layer copper item pair (track
segment, via, pad, zone fill) must meet the IPC-2221 Table 6-1 clearance for
that voltage difference, with the table ROW adjudicated per item type and the
board's coating (see T2; LEARNINGS 2026-07-29 check_creepage entries).

Row adjudication (IPC-2221 6.3.4 - exposed lands do NOT fall back to B2):
    inner layer item                    -> B1 (always)
    outer, coating none:  track/via/zone-> B2   pad -> A6
    outer, soldermask:    track/via/zone-> B4   pad -> A6 (mask relief exposes lands)
    outer, conformal:     track/via/zone-> A5   pad -> A7
Pair requirement = max(row(item_a), row(item_b)). Every violating item pair is
reported (not just the worst per net pair - LEARNINGS 2026-07-29: worst-only
hid 216 siblings), spatially deduped at 0.1 mm and capped at 500 per
(net pair, layer).

Waiver class (T6, follows check_return_path's precedent): a violating PAD pair
whose two pads belong to ONE footprint is the vendor land pattern - unfixable
at layout (the P8 lumina-carrier digest hand-adjudicated exactly this class:
"0.200 mm is the inter-lead gap of the SOIC-8 that TI ships"). Emitted at
severity "warning" with waiver_class="land_pattern_pitch" and
same_footprint=true - never silently dropped; part-selection (P3) scope.

Board-rule class (the board's own .kicad_dru, read from the .kicad_pcb's
stem sibling the way kicad-cli loads it): a custom clearance rule scoped to
ONE footprint - e.g. `(condition "A.intersectsCourtyard('U301') &&
B.intersectsCourtyard('U301')")` - says the owner accepts that package's
rating for copper at its own pins (a 0.5 mm-pitch gate driver whose
switch-node pin sits 0.3 mm from its grounded pad). A violating item pair
is demoted to severity "warning" with waiver_class="dru_footprint_scope",
dru_rule and scope_ref - again never dropped - only when ALL of these hold:
  - the rule is a `clearance` rule with no (layer ...) clause, or one
    naming F.Cu or B.Cu (then it is honoured on that layer only), and its
    condition is nothing but `&&`-joined A./B. insideCourtyard,
    intersectsCourtyard, intersectsFrontCourtyard, intersectsBackCourtyard
    or memberOfFootprint terms naming one literal reference, plus optional
    `A.Type != 'Zone'` / `B.Type != 'Zone'` terms (then no zone is governed:
    rules_gen's package_ratings rules carry them, #649). Any other term
    (net names, ||, other !, wildcards) makes the rule unreadable here, so it
    is not honoured and the IPC verdict stands;
  - one item is a pad of that footprint, and the other is that
    footprint's pad or a track, via or zone - never another footprint's
    pad, so part-to-part spacing keeps the full IPC requirement;
  - for courtyard terms, both ends of the gap lie inside that footprint's
    declared courtyard (a missing courtyard honours nothing); for
    memberOfFootprint on both A and B, both items are its pads;
  - the gap still meets the rule's own minimum.
What the demotion trusts: the owner's statement that the package is rated
for these nets at its own pitch. It does not model KiCad's rule precedence:
a later, differently-conditioned clearance rule that KiCad would apply to the
same pair is not consulted.

The corpus carries no >30 V nets, so this check is clean on all goldens; supply
voltages via constraints.json to exercise it (synthetic HV fixtures are tested).

What it can't judge it says (#601). A net outside `voltages` is read as 0 V, so
an unlisted 48 V net is never swept against its neighbours: not_judged names
any unlisted net whose name reads above 30 V, any unlisted supply (a RAIL_RE
name, a power[] entry, a pour) whose voltage nothing states, and any listed
>30 V net or voltage_pairs net not on the board. With no pair over 30 V at all
the report says nothing_to_judge. None of this is a finding: the name is a
reason to declare the voltage, not evidence of a gap.

CLI: --pcb board.kicad_pcb --constraints constraints.json
     [--coating {none,soldermask,conformal}] [--out report.json]
     exit 0/1/2 per SPEC section 6.

constraints.json inputs:
    {"voltages": [{"net": "HV+", "voltage": 400}, ...],
     "voltage_pairs": [{"a": "/TAP_A1", "b": "/TAP_A2", "voltage": 114}, ...],
     "coating": "none" | "soldermask" | "conformal"}
Nets not listed in `voltages` are treated as 0 V. `voltage_pairs` declares a
DIFFERENTIAL requirement between two named nets regardless of their node
voltages (a bridge/AC input that node voltages cannot express); an explicit
pair overrides the `voltages`-derived difference for that net pair - INCLUDING
a pair declared <= 30 V, which WAIVES the derived check for that pair (the
waiver is recorded in skipped_low_voltage_pairs).
A pair declared BELOW the larger declared node voltage of its own two nets
(pair-scoped or waived alike) raises a `creepage_pair_understated` warning
unless the entry carries a `"why"` string (#651): the override would
otherwise relax the spacing with nothing on record to say it is safe.
`--coating` overrides the constraints key; default "none".
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from shapely import affinity
from shapely.ops import nearest_points
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
from check_mate_pins import RAIL_RE  # noqa: E402
from check_ratings import SIGNAL_WORDS, base_net, volts_from_name  # noqa: E402
from checklib import CheckError, violation  # noqa: E402

SCRIPT = "check_creepage"
HV_THRESHOLD_V = 30.0          # IPC-2221 only regulates spacing above this
BAND_UPPER_V = [15, 30, 50, 100, 150, 170, 250, 300, 500]

# IPC-2221B Table 6-1, all seven rows (mm), band upper bounds as above.
# 51-100 / 101-150 V cells machine-verified 2026-07-29 (ema-eda, Altair Pollex
# quoting 6.3.4, protoexpress - LEARNINGS entry); full grid re-verified
# 2026-08-06 against ema-eda.com/ema-resources/blog/
# pcb-clearance-and-creepage-distance-table/ (agrees on every shared cell).
#   B1 internal conductors
#   B2 external, uncoated, <= 3050 m elevation
#   B3 external, uncoated, > 3050 m elevation
#   B4 external, permanent polymer coating (any elevation)
#   A5 external, conformal coating over the assembly
#   A6 external component lead/termination, uncoated
#   A7 external component lead/termination, conformal coated
ROW_TABLE = {
    "B1": [0.05, 0.05, 0.10, 0.10, 0.20, 0.20, 0.20, 0.20, 0.25],
    "B2": [0.10, 0.10, 0.60, 0.60, 0.60, 1.25, 1.25, 1.25, 2.50],
    "B3": [0.10, 0.10, 0.60, 1.50, 3.20, 3.20, 6.40, 12.5, 12.5],
    "B4": [0.05, 0.05, 0.13, 0.13, 0.40, 0.40, 0.40, 0.40, 0.80],
    "A5": [0.13, 0.13, 0.13, 0.13, 0.40, 0.40, 0.40, 0.40, 0.80],
    "A6": [0.13, 0.25, 0.40, 0.50, 0.80, 0.80, 0.80, 0.80, 1.50],
    "A7": [0.13, 0.13, 0.13, 0.13, 0.40, 0.40, 0.40, 0.80, 0.80],
}
# > 500 V: value at 500 V + slope * (V - 500), per the same sources.
ROW_SLOPE_MM_PER_V = {
    "B1": 0.0025, "B2": 0.005, "B3": 0.025, "B4": 0.00305,
    "A5": 0.00305, "A6": 0.00305, "A7": 0.00305,
}
COATINGS = ("none", "soldermask", "conformal")

MAX_EMIT_PER_PAIR_LAYER = 500  # safeguard against pathological pair counts
DEDUP_MM = 0.1                 # gap midpoints closer than this = same gap


def row_clearance_mm(dv: float, row: str) -> float:
    """IPC-2221 Table 6-1 minimum spacing for `dv` volts in the given row."""
    v = abs(dv)
    for ub, val in zip(BAND_UPPER_V, ROW_TABLE[row]):
        if v <= ub:
            return val
    return ROW_TABLE[row][-1] + ROW_SLOPE_MM_PER_V[row] * (v - 500)


def clearance_mm(dv: float, outer: bool) -> float:
    """IPC-2221 minimum spacing for a `dv`-volt difference, outer/inner layer.
    Pinned S5 API: outer -> B2 (external uncoated), inner -> B1."""
    return row_clearance_mm(dv, "B2" if outer else "B1")


def item_row(item_type: str, outer: bool, coating: str) -> str:
    """Table 6-1 row for one copper item (IPC-2221 6.3.4 adjudication)."""
    if not outer:
        return "B1"
    if item_type == "pad":
        # component lands: mask relief exposes them, so soldermask does NOT
        # move them off A6; only conformal coating over the assembly does.
        return "A7" if coating == "conformal" else "A6"
    # track / via / zone fill (vias treated as tented under soldermask)
    return {"none": "B2", "soldermask": "B4", "conformal": "A5"}[coating]


def voltage_map(cons: dict) -> dict[str, float]:
    vmap: dict[str, float] = {}
    for entry in cons.get("voltages", []):
        net = entry.get("net")
        if net is None or "voltage" not in entry:
            raise CheckError(f"voltages entry needs net+voltage: {entry}")
        vmap[net] = float(entry["voltage"])
    for entry in cons.get("nets", []):
        if entry.get("net") and "voltage" in entry:
            vmap[entry["net"]] = float(entry["voltage"])
    return vmap


# supply names RAIL_RE (connector rails) has no reason to know
HV_NAME_RE = re.compile(r"^(HV|VHV|VMOT|VPWR|VSUP|VPP|PWR)(_|\d|$)")


def undeclared_nets(bg: geom.BoardGeom, vmap: dict, cons: dict,
                    vpairs: list) -> list[str]:
    """Module doc: the nets this run read as 0 V for want of a voltage, worth
    naming because the 0 may be wrong: their names or their use say supply."""
    known = set(vmap) | {n for a, b, _v in vpairs for n in (a, b)}
    powered = {e.get("net") for e in cons.get("power") or []
               if isinstance(e, dict)}
    poured = {z.net for z in bg.zones_of() if z.net and not z.teardrop}
    out: list[str] = []
    for net in sorted(n for n in bg.nets if n and n not in known):
        named = volts_from_name(net)
        if named is not None and abs(named) > HV_THRESHOLD_V:
            out.append(f"{net}: its name reads {named:g} V but it has no "
                       "voltages entry, so it was read as 0 V")
        elif named is None and not SIGNAL_WORDS & set(base_net(net).split("_")) \
                and (net in powered or net in poured
                     or RAIL_RE.search(base_net(net))
                     or HV_NAME_RE.search(base_net(net))):
            out.append(f"{net}: a supply with no stated voltage, read as 0 V")
    return out


def voltage_pair_list(cons: dict) -> list[tuple[str, str, float]]:
    """Parse `voltage_pairs`: explicit differential requirements (a, b, |V|)."""
    out: list[tuple[str, str, float]] = []
    for entry in cons.get("voltage_pairs", []):
        a, b = entry.get("a"), entry.get("b")
        if not a or not b or "voltage" not in entry:
            raise CheckError(f"voltage_pairs entry needs a+b+voltage: {entry}")
        if a == b:
            raise CheckError(f"voltage_pairs entry names the same net twice: {a!r}")
        out.append((a, b, abs(float(entry["voltage"]))))
    return out


def understated_pairs(cons: dict, vmap: dict) -> list[dict]:
    """voltage_pairs entries declared below the larger declared node voltage
    of their own two nets, with no `why` (#651).

    An explicit pair OVERRIDES the voltages-derived difference, so a pair
    written too low silently relaxes the spacing: hw-lab mains-psu declared
    HV_BULK-DRAIN at 219 V while 375 V sits across it with the FET on, and
    1.25 mm passed where 2.5 mm was needed. The node voltages bound the
    worst case (the other net at 0 V), so a pair below max(|V_a|, |V_b|)
    needs a stated reason. Only declared node voltages count: a pair whose
    nets are both absent from `voltages` (a bridge input) is never flagged.
    """
    out = []
    for entry in cons.get("voltage_pairs", []):
        a, b = entry["a"], entry["b"]
        v = abs(float(entry["voltage"]))
        known = [n for n in (a, b) if n in vmap]
        if not known or str(entry.get("why") or "").strip():
            continue
        node = max(known, key=lambda n: abs(vmap[n]))
        if v >= abs(vmap[node]):
            continue
        out.append({"a": a, "b": b, "voltage": v, "node_net": node,
                    "node_voltage": abs(vmap[node])})
    return out


# one condition term: A./B. + a footprint-scoping function + one literal ref
_SCOPE_TERM_RE = re.compile(
    r"^([AB])\.(insideCourtyard|intersectsCourtyard|intersectsFrontCourtyard|"
    r"intersectsBackCourtyard|memberOfFootprint)\(\s*'([^'*?]+)'\s*\)$")
_SCOPE_SIDE = {"intersectsFrontCourtyard": "front",
               "intersectsBackCourtyard": "back"}


def _strip_parens(s: str) -> str:
    """Drop outer parens that wrap the WHOLE expression, repeatedly."""
    s = s.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        for i, ch in enumerate(s):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(s) - 1:
                return s       # the first paren closes early: not a wrapper
        s = s[1:-1].strip()
    return s


_NO_ZONE_RE = re.compile(r"^[AB]\.Type\s*!=\s*'Zone'$")


def parse_footprint_scope(condition: str | None) -> dict | None:
    """A DRU condition that ONLY scopes to one footprint -> {ref, sides,
    courtyard, both_member, no_zone}; anything else -> None (not honoured).
    `A.Type != 'Zone'` / `B.Type != 'Zone'` terms (rules_gen's
    package_ratings, #649) are read as "this rule governs no zone"."""
    if not condition or "||" in condition:
        return None
    refs, sides, fns, member_ab = set(), set(), set(), set()
    no_zone = False
    for term in _strip_parens(condition).split("&&"):
        term = _strip_parens(term)
        if _NO_ZONE_RE.match(term):
            no_zone = True
            continue
        m = None if "!" in term else _SCOPE_TERM_RE.match(term)
        if not m:
            return None
        ab, fn, ref = m.groups()
        refs.add(ref.strip())
        fns.add(fn)
        if fn in _SCOPE_SIDE:
            sides.add(_SCOPE_SIDE[fn])
        if fn == "memberOfFootprint":
            member_ab.add(ab)
    if len(refs) != 1 or len(sides) > 1:
        return None
    return {"ref": refs.pop(), "side": sides.pop() if sides else None,
            "courtyard": any(f != "memberOfFootprint" for f in fns),
            "both_member": member_ab == {"A", "B"}, "no_zone": no_zone}


def dru_footprint_scopes(pcb: Path) -> list[dict]:
    """Footprint-scoped clearance rules from the board's .kicad_dru sibling,
    each with its footprint's courtyard in the board frame. Parsed by
    route_critical.parse_dru_rules (the one DRU reader). Missing DRU -> []."""
    dru = Path(pcb).with_suffix(".kicad_dru")
    if not dru.is_file():
        return []
    import route_critical as rc  # noqa: PLC0415 - lazy, heavy imports
    rules = rc.parse_dru_rules(dru.read_text(encoding="utf-8"))
    scopes = []
    for rule in rules:
        if rule["constraint"] != "clearance" or \
                rule["layer"] not in (None, "F.Cu", "B.Cu"):
            continue
        sc = parse_footprint_scope(rule["condition"])
        if sc is not None:
            scopes.append({**sc, "rule": rule["name"],
                           "min_mm": rule["min_mm"], "layer": rule["layer"]})
    if not scopes:
        return []
    import placelib  # noqa: PLC0415 - only boards with a scoped rule pay
    fps = placelib.PlaceModel(pcb).footprints
    for sc in scopes:
        fp = fps.get(sc["ref"])
        sc["courtyard_poly"] = None
        if fp is None or fp.courtyard_local is None:
            continue
        if sc["side"] and fp.side != sc["side"]:
            continue
        p = affinity.rotate(fp.courtyard_local, -fp.angle, origin=(0, 0))
        sc["courtyard_poly"] = affinity.translate(p, fp.pos[0], fp.pos[1])
    return scopes


def scope_for(a, b, pa, pb, scopes, layer=None) -> dict | None:
    """The LAST (KiCad: later rule wins) footprint scope that governs this
    item pair, per the module docstring's conditions; None if none does.
    The caller still holds the gap to the scope's own min_mm. A scope whose
    rule names a layer governs only gaps on that `layer`."""
    hit = None
    for sc in scopes:
        if sc.get("layer") and sc["layer"] != layer:
            continue
        ref = sc["ref"]
        own = [it[0] == "pad" and it[3] == ref for it in (a, b)]
        foreign = any(it[0] == "pad" and it[3] != ref for it in (a, b))
        if not any(own) or foreign:
            continue
        if sc["both_member"] and not all(own):
            continue
        if sc.get("no_zone") and any(it[0] == "zone" for it in (a, b)):
            continue
        if sc["courtyard"]:
            cy = sc.get("courtyard_poly")
            if cy is None or cy.distance(pa) > 1e-6 or cy.distance(pb) > 1e-6:
                continue
        hit = sc
    return hit


def _layer_items(bg: geom.BoardGeom, net: str, layer: str, cache: dict) -> list:
    """Copper items of `net` on `layer` as (type, geometry, descriptor, ref)."""
    key = (net, layer)
    if key not in cache:
        items = []
        for t in bg.tracks_of(net, layer):
            c0, c1 = t.shape.coords[0], t.shape.coords[-1]
            items.append(("track", t.poly, {
                "type": "track",
                "ends": [[checklib.rnd(c0[0]), checklib.rnd(c0[1])],
                         [checklib.rnd(c1[0]), checklib.rnd(c1[1])]]}, None))
        for v in bg.vias_of(net, layer):
            items.append(("via", v.poly, {
                "type": "via",
                "at": [checklib.rnd(v.at[0]), checklib.rnd(v.at[1])]}, None))
        for p in bg.pads_of(net, layer):
            items.append(("pad", p.poly, {
                "type": "pad", "ref": p.ref, "pad": p.number}, p.ref))
        for z in bg.zones_of(net, layer):
            for poly in z.fills.get(layer, []):
                items.append(("zone", poly, {"type": "zone", "net": z.net}, None))
        cache[key] = items
    return cache[key]


def _same_fp(a, b) -> bool:
    return (a[0] == "pad" and b[0] == "pad"
            and bool(a[3]) and a[3] == b[3])


def _waiver(a, b, pa, pb, dist: float, scopes, layer=None):
    """(waiver_class, extras, msg suffix) for one violating item pair, or
    (None, {}, "") when it stays an error. Module docstring: both classes."""
    if _same_fp(a, b):
        # both pads of ONE footprint: the land pattern itself - not
        # fixable by placement/routing; named waiver class, visible.
        return ("land_pattern_pitch",
                {"same_footprint": True, "waiver_class": "land_pattern_pitch"},
                f"; land-pattern pitch (same footprint {a[3]}) - "
                "part-selection scope, not layout")
    sc = scope_for(a, b, pa, pb, scopes, layer) if scopes else None
    if sc is not None and dist + 1e-6 >= sc["min_mm"]:
        return ("dru_footprint_scope",
                {"waiver_class": "dru_footprint_scope",
                 "dru_rule": sc["rule"], "scope_ref": sc["ref"],
                 "dru_min_mm": sc["min_mm"]},
                f"; at {sc['ref']}'s own pins, where board rule "
                f"'{sc['rule']}' sets {sc['min_mm']:.2f} mm (package rating)")
    return None, {}, ""


def sweep_pair(bg: geom.BoardGeom, prim: str, sec: str, dv: float,
               coating: str, cache: dict, prim_label: str, scopes=()):
    """Item-level sweep of one net pair across all copper layers.

    Emits one violation PER violating item pair at the actual gap location
    (midpoint of the nearest points), spatially deduped within DEDUP_MM and
    capped at MAX_EMIT_PER_PAIR_LAYER per (net pair, layer); a waived gap
    never dedups away an error beside it. Returns (violations, per-(pair,layer)
    summaries)."""
    violations: list[dict] = []
    summaries: list[dict] = []
    for layer in bg.copper_layers:
        ia = _layer_items(bg, prim, layer, cache)
        ib = _layer_items(bg, sec, layer, cache)
        if not ia or not ib:
            continue
        outer = bg.stackup.is_outer(layer)
        types = {it[0] for it in ia} | {it[0] for it in ib}
        # prefilter bound: the largest requirement any item-type pair can carry
        req_max = max(row_clearance_mm(dv, item_row(t, outer, coating))
                      for t in types)
        tree = STRtree([it[1] for it in ib])
        hits = []      # (dist, midpoint, item_a, item_b, [row_a,row_b], req)
        min_gap = None
        for a in ia:
            row_a = item_row(a[0], outer, coating)
            req_a = row_clearance_mm(dv, row_a)
            for j in tree.query(a[1], predicate="dwithin",
                                distance=req_max + 1e-6):
                b = ib[int(j)]
                dist = a[1].distance(b[1])
                if min_gap is None or dist < min_gap:
                    min_gap = dist
                row_b = item_row(b[0], outer, coating)
                req = max(req_a, row_clearance_mm(dv, row_b))
                if dist + 1e-6 < req:
                    pa, pb = nearest_points(a[1], b[1])
                    mid = ((pa.x + pb.x) / 2.0, (pa.y + pb.y) / 2.0)
                    hits.append((dist, mid, a, b, [row_a, row_b], req,
                                 _waiver(a, b, pa, pb, dist, scopes, layer)))
        n_under = len(hits)
        # dedup: tightest pair wins within DEDUP_MM (a track inside its own
        # net's zone must not double-report the same physical gap). A kept
        # error shadows anything; a kept waived hit shadows only its own
        # class, so a waiver never hides an error beside it.
        hits.sort(key=lambda h: h[0])
        kept = []
        for h in hits:
            if any(k[6][0] in (None, h[6][0])
                   and (h[1][0] - k[1][0]) ** 2 + (h[1][1] - k[1][1]) ** 2
                   <= DEDUP_MM ** 2 for k in kept):
                continue
            kept.append(h)
        truncated = len(kept) > MAX_EMIT_PER_PAIR_LAYER

        sf_under = sum(1 for h in hits if h[6][0] == "land_pattern_pitch")
        dru_under = sum(1 for h in hits if h[6][0] == "dru_footprint_scope")
        for dist, mid, a, b, rows, req, wv in kept[:MAX_EMIT_PER_PAIR_LAYER]:
            refs = [r for r in (a[3], b[3]) if r]
            msg = (f"{prim_label} to {sec} on {layer}: {dist:.3f} mm copper "
                   f"spacing < IPC-2221 {req:.2f} mm ({rows[0]}/{rows[1]}) "
                   f"for {abs(dv):.0f} V") + wv[2]
            violations.append(violation(
                SCRIPT, "warning" if wv[0] else "error", mid, layer, prim,
                refs, msg, SCRIPT, kind="creepage", other_net=sec,
                delta_v=checklib.rnd(abs(dv)), spacing_mm=checklib.rnd(dist),
                required_mm=req, rows=rows, item=a[2], other_item=b[2],
                **wv[1]))
        summaries.append({
            "other_net": sec, "layer": layer, "delta_v": checklib.rnd(abs(dv)),
            "pairs_checked": len(ia) * len(ib),
            "pairs_under_requirement": n_under,
            "same_footprint_under": sf_under,
            "dru_footprint_scope_under": dru_under,
            # min gap among prefiltered candidates; None = all pairs on this
            # layer are farther apart than the largest possible requirement
            "min_gap_mm": checklib.rnd(min_gap) if min_gap is not None else None,
            "truncated": truncated})
    return violations, summaries


def check_net(bg: geom.BoardGeom, hv: str, v_hv: float, vmap: dict,
              coating: str = "none", overrides: frozenset = frozenset(),
              cache: dict | None = None, scopes=()):
    """Check the primary net `hv` against every other net that sits >30 V away.
    The violation is attributed to whichever net of the pair has the higher
    magnitude voltage (the one a fixer should move). Net pairs listed in
    `overrides` (frozensets of two net names) are owned by voltage_pairs;
    `scopes` are dru_footprint_scopes() rows."""
    if hv not in bg.nets:
        raise CheckError(f"voltage net {hv!r} not on board")
    cache = {} if cache is None else cache
    violations: list[dict] = []
    pair_summaries: list[dict] = []
    checked = 0
    for other in sorted(bg.nets):
        if not other or other == hv:
            continue
        v_other = vmap.get(other, 0.0)
        dv = v_hv - v_other
        if abs(dv) <= HV_THRESHOLD_V:
            continue
        # own each pair once: the LISTED net with the larger |V| emits it
        # (tie -> larger name, per tuple compare). Unlisted nets (0 V) are never
        # owners, so a listed HV net always owns its pair with a signal/gnd net.
        if other in vmap and (abs(v_other), other) > (abs(v_hv), hv):
            continue
        if frozenset((hv, other)) in overrides:
            continue           # explicit voltage_pairs entry owns this pair
        vs, summ = sweep_pair(bg, hv, other, dv, coating, cache,
                              f"{hv} ({v_hv:.0f} V)", scopes)
        violations.extend(vs)
        pair_summaries.extend(summ)
        checked += len(summ)   # (other_net, layer) combos with copper on both
    return violations, {"net": hv, "voltage": v_hv, "pairs_checked": checked,
                        "pairs": pair_summaries}


def run(argv=None):
    ap = argparse.ArgumentParser(
        description="IPC-2221 same-layer clearance for >30 V net pairs.")
    ap.add_argument("--pcb", required=True, help="path to .kicad_pcb")
    ap.add_argument("--constraints", required=True,
                    help="constraints.json with voltages/voltage_pairs/coating")
    ap.add_argument("--coating", choices=list(COATINGS),
                    help="board coating; overrides the constraints.json key "
                         "(default none)")
    ap.add_argument("--out", help="write JSON report here instead of stdout")
    args = ap.parse_args(argv)

    cons = checklib.load_json(args.constraints, "constraints")
    coating = args.coating or cons.get("coating", "none")
    if coating not in COATINGS:
        raise CheckError(f"coating must be one of {COATINGS}, got {coating!r}")
    vmap = voltage_map(cons)
    vpairs = voltage_pair_list(cons)
    bg = geom.load_board(Path(args.pcb))
    bg.assert_fresh()
    scopes = dru_footprint_scopes(Path(args.pcb))

    overrides = frozenset(frozenset((a, b)) for a, b, _v in vpairs)
    cache: dict = {}
    violations: list[dict] = []
    checked: list[dict] = []
    skipped: list[str] = []
    skipped_lv_pairs: list[dict] = []
    for net, v in sorted(vmap.items()):
        # a listed voltage net absent from the board (renamed / not placed) is
        # skipped, not fatal - other nets still get checked. It is still honored
        # as the reference voltage for other nets that DO have copper nearby.
        if net not in bg.nets:
            skipped.append(net)
            continue
        vs, facts = check_net(bg, net, v, vmap, coating=coating,
                              overrides=overrides, cache=cache,
                              scopes=scopes)
        violations.extend(vs)
        if facts["pairs_checked"] or vs:
            checked.append(facts)
    for a, b, v in vpairs:
        if v <= HV_THRESHOLD_V:
            skipped_lv_pairs.append({"a": a, "b": b, "voltage": v})
            continue
        absent = [n for n in (a, b) if n not in bg.nets]
        if absent:
            skipped.extend(n for n in absent if n not in skipped)
            continue
        vs, summ = sweep_pair(bg, a, b, v, coating, cache,
                              f"{a} (pair {v:.0f} V)", scopes)
        violations.extend(vs)
        checked.append({"net": a, "other_net": b, "voltage": v,
                        "voltage_pair": True, "pairs_checked": len(summ),
                        "pairs": summ})

    for u in understated_pairs(cons, vmap):
        violations.append(violation(
            SCRIPT, "warning", None, None, u["a"], [],
            (f"voltage_pairs {u['a']}-{u['b']} declares {u['voltage']:.0f} V, "
             f"below {u['node_net']}'s own {u['node_voltage']:.0f} V; the pair "
             f"overrides the derived difference, so spacing is judged at the "
             f"lower figure. Raise it, or add a \"why\" saying why the nets "
             f"never see more"),
            SCRIPT, kind="creepage_pair_understated", other_net=u["b"],
            pair_voltage=checklib.rnd(u["voltage"]),
            node_net=u["node_net"],
            node_voltage=checklib.rnd(u["node_voltage"])))

    not_judged = undeclared_nets(bg, vmap, cons, vpairs)
    not_judged += [f"{n}: listed above {HV_THRESHOLD_V:.0f} V but not on the "
                   "board (renamed?)" for n in skipped
                   if abs(vmap.get(n, HV_THRESHOLD_V + 1)) > HV_THRESHOLD_V]
    swept = any(c["pairs_checked"] for c in checked)
    nothing = None
    if not swept and not violations:
        nothing = ("no voltages or voltage_pairs in constraints: every net "
                   "reads as 0 V" if not vmap and not vpairs else
                   f"no two nets with copper on one layer are declared more "
                   f"than {HV_THRESHOLD_V:.0f} V apart")

    # sidecar-adoption lint (T6, facts only - never a violation): when no
    # coating is declared and findings were adjudicated on the uncoated
    # B2/B3 rows, a "coating" constraints key would change those verdicts.
    # The carrier run burned 2 fixer attempts on exactly this gap.
    hint: dict = {}
    if coating == "none":
        n = sum(1 for v in violations
                if {"B2", "B3"} & set(v.get("rows") or []))
        if n:
            hint = {"coating_undeclared_hint": True,
                    "coating_undeclared_count": n}

    payload = checklib.report(SCRIPT, args.pcb, violations, checked=checked,
                              skipped_absent_nets=skipped,
                              skipped_low_voltage_pairs=skipped_lv_pairs,
                              coating=coating,
                              dru_footprint_scopes=[
                                  {"rule": s["rule"], "ref": s["ref"],
                                   "min_mm": s["min_mm"],
                                   "courtyard_found":
                                       s["courtyard_poly"] is not None,
                                   **({"layer": s["layer"]}
                                      if s["layer"] else {})}
                                  for s in scopes],
                              nothing_to_judge=nothing, not_judged=not_judged,
                              **hint)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
