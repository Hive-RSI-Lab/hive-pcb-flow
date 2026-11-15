"""check_length_groups.py - length-matched net groups (DDR byte lanes, address/
command, clocks) against their reference (hive-hwde #324).

check_diffpair matches the two halves of one pair and check_si holds a
source-synchronous group to a fraction of its unit interval by flight time;
neither matches a GROUP of nets to a reference in millimetres, which is how
DDR layout rules are written (i.MX6UL IMX6ULHDG Table 22: data within
+/-0.635 mm of its DQS, address/command within a window of the clock, ...).

Per net the ROUTED LENGTH is the copper path between the net's two farthest
terminal pads on netconn's connectivity graph (the same graph check_diffpair's
trunk uses, so a dead-end stub or a test-point branch does not count), plus
the via BARREL wherever that path changes layer: the dielectric heights
between the two layers, and the copper of the layers in between, from the
board's stackup (geom.Stackup; FR4 defaults when the board has no stackup
block, flagged stackup_assumed). The barrel below the exit layer is a stub,
not path, and is not counted. The delay of each net (ps) is reported too, as
a fact, from the effective permittivity of each segment's layer.

constraints.json "length_groups": a list of
    {"name": "BYTE0",
     "nets": ["DRAM_D[0-7]", "DRAM_DQM0"],   # exact names or fnmatch globs
     "ref_net": "DRAM_SDQS0_P",              # optional: one reference net
     "ref_nets": ["DRAM_SDQS0_*"],           # optional: several (globs ok)
     "ref_stat": "min",                      # min (default) | max | mean of
                                             #   the reference nets' lengths,
                                             #   or sum: one clock split by a
                                             #   series resistor (CLK_R + CLK);
                                             #   every part must be routed
     "tol_mm": 0.635,                        # |length - reference| limit
     "window_mm": [-5.08, 0],                # optional: length - reference
                                             #   must fall in [lo, hi]; replaces
                                             #   the symmetric tol_mm
     "max_mm": 50.8, "min_mm": 0,            # optional absolute bounds
     "not_longer_than": ["DRAM_SDCLK0_*"],   # optional: no net may be longer
                                             #   than the SHORTEST of these
     "terminals": ["U1", "U3"],              # optional: only these refs' pads
                                             #   bound a net (skip a tap/TP)
     "severity": "error"}                    # optional, default error
With no reference net the group is matched to its own LONGEST member (the
tuning convention: meanders lengthen the short ones), which is the same as
max - min <= tol_mm. Net names match with or without the leading "/" of a
hierarchical sheet path, so "DRAM_D0" finds "/DRAM_D0".

Findings (kind):
  length_group_mismatch    a net outside tol_mm / window_mm of the reference
  length_group_bound       a net past max_mm / min_mm / not_longer_than
  length_group_unrouted    a member has no copper (warning: not judged)
  length_group_open        a member's copper does not join two terminal
                           pads (warning: its length is total copper)
  length_group_no_nets     a glob that names no net on the board (warning)
  length_group_undeclared  the board carries DDR-named nets but declares no
                           length_groups (warning: nothing was matched)
A bad group (no nets key, a window with lo > hi, an unknown ref_stat) stops
the check (exit 2): a malformed constraint must not pass silently.

CLI: --pcb board.kicad_pcb [--constraints constraints.json] [--out report.json]
     exit 0/1/2 per SPEC section 6. Board only: without constraints (or
     without length_groups) it checks nothing and passes, bar the DDR warning.

Library use (route_critical's tuning step): net_length(bg, net, terminals)
returns the same measurement as a dict.
"""
from __future__ import annotations

import argparse
import fnmatch
import heapq
import itertools
import math
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
import netconn  # noqa: E402
from checklib import CheckError, violation  # noqa: E402

SCRIPT = "check_length_groups"
C_MM_PER_PS = 0.299792458      # speed of light, mm/ps
DEFAULT_ER = 4.5               # FR4, when the stackup has no dielectric
REF_STATS = ("min", "max", "mean", "sum")
SEVERITIES = ("error", "warning")
GROUP_KEYS = {"name", "nets", "ref_net", "ref_nets", "ref_stat", "tol_mm",
              "window_mm", "max_mm", "min_mm", "not_longer_than", "terminals",
              "severity", "note"}
# DDR data nets by name: DRAM_D0, DDR_DQ12, /DDR3_DQ7, MEM_DQ0 ... Eight or more
# on a board without length_groups is the "undeclared" warning.
DDR_DATA = re.compile(r"(?:^|/)(?:DRAM|DDR\d?L?|MEM|SDRAM)_?(?:D|DQ)\d{1,2}$",
                      re.IGNORECASE)
DDR_MIN_NETS = 8


# ------------------------------------------------------------ measurement

def _stack_index(bg, layer):
    try:
        return bg.stackup.index(layer)
    except ValueError:
        return None


def barrel_mm(bg, a: str, b: str) -> float:
    """Via barrel length a signal travels from layer a to layer b: the
    dielectrics between them plus the copper of the layers strictly between.
    0 for the same layer or a layer outside the stackup."""
    ia, ib = _stack_index(bg, a), _stack_index(bg, b)
    if ia is None or ib is None or ia == ib:
        return 0.0
    i, j = sorted((ia, ib))
    st = bg.stackup
    h = sum(d["height"] for d in st.dielectrics[i:j])
    cu = sum(st.copper_thickness.get(l, 0.0) for l in st.copper_layers[i + 1:j])
    return h + cu


def layer_eps_eff(bg, layer: str) -> float:
    """Effective permittivity a trace on `layer` sees: the mean epsilon_r of
    the dielectrics either side for an inner layer (stripline), and the usual
    microstrip blend (er + 1) / 2 + (er - 1) / 2 * 0.8 for an outer one
    (Hammerstad's 1 / sqrt(1 + 12 h / w) is about 0.8 near w = 2 h). Only
    the reported delay uses it; the check judges millimetres."""
    st = bg.stackup
    i = _stack_index(bg, layer)
    if i is None or len(st.copper_layers) < 2 or not st.dielectrics:
        return DEFAULT_ER
    above = st.dielectrics[i - 1] if i > 0 else None
    below = st.dielectrics[i] if i < len(st.dielectrics) else None
    ers = [d["epsilon_r"] for d in (above, below) if d and d.get("epsilon_r")]
    if not ers:
        return DEFAULT_ER
    er = sum(ers) / len(ers)
    if above is None or below is None:
        return (er + 1) / 2 + (er - 1) / 2 * 0.8
    return er


def _path(g: netconn.NetGraph, a, b):
    """Dijkstra a -> b returning (length, [edge_id, ...]) or None."""
    if a not in g.adj or b not in g.adj:
        return None
    tie = itertools.count()
    dist = {a: 0.0}
    prev: dict = {}
    pq = [(0.0, next(tie), a)]
    while pq:
        d, _, u = heapq.heappop(pq)
        if u == b:
            break
        if d > dist.get(u, math.inf):
            continue
        for v, w, eid in g.adj[u]:
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = (u, eid)
                heapq.heappush(pq, (nd, next(tie), v))
    if b not in dist:
        return None
    edges = []
    node = b
    while node != a:
        node, eid = prev[node]
        edges.append(eid)
    edges.reverse()
    return dist[b], edges


def net_length(bg, net: str, terminals=None) -> dict:
    """The routed length of `net`: track + via barrel along the copper path
    between its two farthest terminal pads (module doc). `terminals` limits
    the pads to those refs. Keys: routed (any copper), connected (a pad-to-
    pad path exists), length_mm, track_mm, via_mm, vias, delay_ps, from/to
    ("REF.pin"), total_track_mm, layers {layer: mm}, and path_tracks (the
    geom.Track objects along the path, in order; not for the report)."""
    tracks = bg.tracks_of(net)
    total = sum(t.length for t in tracks)
    out = {"net": net, "routed": bool(tracks),
           "total_track_mm": checklib.rnd(total)}
    pads = bg.pads_of(net=net)
    if terminals:
        want = set(terminals)
        pads = [p for p in pads if p.ref in want]
    if not tracks:
        return {**out, "connected": False, "length_mm": 0.0,
                "pads": sorted(f"{p.ref}.{p.number}" for p in pads)}
    g = netconn.build(bg, net)
    named = [(f"{p.ref}.{p.number}", netconn.pad_node(g, p)) for p in pads]
    named = [(n, node) for n, node in named if node is not None]
    best = None
    for (na, a), (nb, b) in itertools.combinations(named, 2):
        r = _path(g, a, b)
        if r is not None and (best is None or r[0] > best[0][0]):
            best = (r, na, nb)
    if best is None:
        return {**out, "connected": False, "length_mm": checklib.rnd(total),
                "track_mm": checklib.rnd(total), "via_mm": 0.0,
                "pads": sorted(n for n, _ in named)}
    (_, edges), na, nb = best
    path_tracks = [g.tracks[e] for e in edges if e in g.tracks]
    track_mm = sum(t.length for t in path_tracks)
    via_mm, vias, prev_layer = 0.0, 0, None
    by_layer: dict[str, float] = {}
    delay_ps = 0.0
    for t in path_tracks:
        by_layer[t.layer] = by_layer.get(t.layer, 0.0) + t.length
        delay_ps += t.length * math.sqrt(layer_eps_eff(bg, t.layer)) / C_MM_PER_PS
        if prev_layer is not None and t.layer != prev_layer:
            b = barrel_mm(bg, prev_layer, t.layer)
            via_mm += b
            vias += 1
            # a barrel is surrounded by dielectric: its delay uses the mean er
            delay_ps += b * math.sqrt(_mean_er(bg)) / C_MM_PER_PS
        prev_layer = t.layer
    return {**out, "connected": True, "path_tracks": path_tracks,
            "length_mm": checklib.rnd(track_mm + via_mm),
            "track_mm": checklib.rnd(track_mm), "via_mm": checklib.rnd(via_mm),
            "vias": vias, "delay_ps": checklib.rnd(delay_ps, 1),
            "from": na, "to": nb,
            "layers": {k: checklib.rnd(v) for k, v in sorted(by_layer.items())}}


def _mean_er(bg) -> float:
    ers = [d["epsilon_r"] for d in bg.stackup.dielectrics if d.get("epsilon_r")]
    return sum(ers) / len(ers) if ers else DEFAULT_ER


# ------------------------------------------------------------ groups

def _variants(name: str) -> set[str]:
    return {name, name.lstrip("/")}


def match_nets(patterns, nets) -> tuple[list[str], list[str]]:
    """(matched board nets in board order, patterns that matched nothing).
    A pattern matches a net's full name or its name without the leading '/'."""
    if isinstance(patterns, str):
        patterns = [patterns]
    names = sorted(n for n in nets if n)
    found: list[str] = []
    empty: list[str] = []
    for pat in patterns or []:
        hit = [n for n in names
               if any(fnmatch.fnmatchcase(v, pat) or fnmatch.fnmatchcase(v, pat.lstrip("/"))
                      for v in _variants(n))]
        if not hit:
            empty.append(pat)
        for n in hit:
            if n not in found:
                found.append(n)
    return found, empty


def validate_group(i: int, g) -> None:
    where = f"length_groups[{i}]"
    if not isinstance(g, dict):
        raise CheckError(f"{where} is not an object")
    where = f"length_groups[{i}] ({g.get('name', '?')})"
    unknown = sorted(checklib.data_keys(g) - GROUP_KEYS)
    if unknown:
        raise CheckError(f"{where}: unknown key(s) {unknown}")
    if not g.get("nets"):
        raise CheckError(f"{where}: no 'nets'")
    if g.get("ref_stat", "min") not in REF_STATS:
        raise CheckError(f"{where}: ref_stat must be one of {REF_STATS}")
    if g.get("severity", "error") not in SEVERITIES:
        raise CheckError(f"{where}: severity must be one of {SEVERITIES}")
    w = g.get("window_mm")
    if w is not None:
        if (not isinstance(w, list) or len(w) != 2
                or not all(isinstance(x, (int, float)) for x in w) or w[0] > w[1]):
            raise CheckError(f"{where}: window_mm must be [lo, hi] with lo <= hi")
    elif g.get("tol_mm") is None and not any(
            k in g for k in ("max_mm", "min_mm", "not_longer_than")):
        raise CheckError(f"{where}: needs tol_mm, window_mm or a bound")
    if g.get("tol_mm") is not None and float(g["tol_mm"]) < 0:
        raise CheckError(f"{where}: tol_mm is negative")


def _ref_nets(g) -> list:
    out = list(g.get("ref_nets") or [])
    if g.get("ref_net"):
        out.insert(0, g["ref_net"])
    return out


def _rep_point(bg, net):
    tks = bg.tracks_of(net)
    if not tks:
        return None
    c = tks[0].shape.interpolate(0.5, normalized=True).coords[0]
    return (c[0], c[1])


def _rep_layer(bg, net):
    tks = bg.tracks_of(net)
    return tks[0].layer if tks else None


def check_group(bg, g: dict, cache: dict):
    """(violations, facts) for one validated group."""
    name = g.get("name") or "?"
    sev = g.get("severity", "error")
    terminals = g.get("terminals")
    vs: list[dict] = []

    def measure(net):
        key = (net, tuple(terminals or ()))
        if key not in cache:
            cache[key] = net_length(bg, net, terminals)
        return cache[key]

    def no_nets(pats, role):
        for pat in pats:
            vs.append(violation(
                SCRIPT, "warning", None, None, None, [],
                f"length group {name}: {role} pattern {pat!r} names no net on "
                f"the board (stale constraints?)", SCRIPT,
                kind="length_group_no_nets", group=name, pattern=pat))

    nets, empty = match_nets(g["nets"], bg.nets)
    no_nets(empty, "nets")
    refs, rempty = match_nets(_ref_nets(g), bg.nets)
    no_nets(rempty, "ref_nets")
    caps, cempty = match_nets(g.get("not_longer_than") or [], bg.nets)
    no_nets(cempty, "not_longer_than")

    usable: dict[str, dict] = {}
    for net in dict.fromkeys(nets + refs + caps):
        m = measure(net)
        if not m["routed"]:
            if net in nets or net in refs:
                vs.append(violation(
                    SCRIPT, "warning", None, None, net, [],
                    f"length group {name}: {net} is not routed; not judged",
                    SCRIPT, kind="length_group_unrouted", group=name))
            continue
        if not m["connected"]:
            vs.append(violation(
                SCRIPT, "warning", _rep_point(bg, net), None, net, [],
                f"length group {name}: {net}'s copper does not join two "
                f"terminal pads ({', '.join(m.get('pads') or []) or 'none'}); "
                f"its length {m['length_mm']:.2f} mm is total copper, not judged",
                SCRIPT, kind="length_group_open", group=name))
            continue
        usable[net] = m

    stat = g.get("ref_stat", "min")
    ref_lens = [usable[n]["length_mm"] for n in refs if n in usable]
    member_lens = {n: usable[n]["length_mm"] for n in nets if n in usable}
    if refs:
        if stat == "sum" and len(ref_lens) < len(refs):
            # a partial sum is a short reference, not a smaller one: judge
            # nothing until every part of the chain is routed and joined
            ref_len, ref_what = None, "reference not fully routed"
        elif ref_lens:
            ref_len = {"min": min, "max": max, "mean": statistics.fmean,
                       "sum": math.fsum}[stat](ref_lens)
            ref_what = f"{stat} of {', '.join(r for r in refs if r in usable)}"
        else:
            ref_len, ref_what = None, "reference not routed"
    elif member_lens:
        ref_len = max(member_lens.values())
        ref_what = f"longest member {max(member_lens, key=member_lens.get)}"
    else:
        ref_len, ref_what = None, "no routed member"

    window = g.get("window_mm")
    tol = g.get("tol_mm")
    if window is not None:
        lo, hi = float(window[0]), float(window[1])
    elif tol is not None:
        lo, hi = -float(tol), float(tol)
    else:
        lo = hi = None
    cap_lens = [usable[n]["length_mm"] for n in caps if n in usable]
    cap = min(cap_lens) if cap_lens else None
    max_mm, min_mm = g.get("max_mm"), g.get("min_mm")

    rows = []
    for net in nets:
        m = measure(net)
        row = {k: m.get(k) for k in ("net", "routed", "connected", "length_mm",
                                     "track_mm", "via_mm", "vias", "delay_ps",
                                     "from", "to")}
        if net in usable and ref_len is not None and lo is not None:
            row["delta_mm"] = checklib.rnd(m["length_mm"] - ref_len)
        rows.append(row)
        if net not in usable:
            continue
        L = m["length_mm"]
        common = dict(kind=None, group=name, length_mm=L,
                      refs_of_path=[m.get("from"), m.get("to")])
        if ref_len is not None and lo is not None and net not in refs:
            d = L - ref_len
            if d < lo - 1e-6 or d > hi + 1e-6:
                need = (lo - d) if d < lo else (hi - d)
                lim = (f"+/-{hi:g} mm" if window is None
                       else f"window [{lo:g}, {hi:g}] mm")
                vs.append(violation(
                    SCRIPT, sev, _rep_point(bg, net), _rep_layer(bg, net), net, [],
                    f"length group {name}: {net} is {L:.3f} mm, {d:+.3f} mm "
                    f"from the reference {ref_len:.3f} mm ({ref_what}); "
                    f"limit {lim}, so it needs {need:+.3f} mm",
                    SCRIPT, **{**common, "kind": "length_group_mismatch",
                               "reference_mm": checklib.rnd(ref_len),
                               "delta_mm": checklib.rnd(d),
                               "adjust_mm": checklib.rnd(need),
                               "window_mm": [lo, hi]}))
        for what, bad, lim in (
                ("max_mm", max_mm is not None and L > float(max_mm) + 1e-6, max_mm),
                ("min_mm", min_mm is not None and L < float(min_mm) - 1e-6, min_mm),
                ("not_longer_than", cap is not None and net not in caps
                 and L > cap + 1e-6, cap)):
            if not bad:
                continue
            label = ("longer than the shortest of "
                     f"{', '.join(c for c in caps if c in usable)}"
                     if what == "not_longer_than"
                     else "over max_mm" if what == "max_mm" else "under min_mm")
            vs.append(violation(
                SCRIPT, sev, _rep_point(bg, net), _rep_layer(bg, net), net, [],
                f"length group {name}: {net} is {L:.3f} mm, {label} "
                f"({float(lim):.3f} mm)",
                SCRIPT, **{**common, "kind": "length_group_bound",
                           "bound": what, "limit_mm": checklib.rnd(float(lim))}))

    lens = list(member_lens.values())
    facts = {"name": name, "nets": rows,
             "reference_mm": checklib.rnd(ref_len) if ref_len is not None else None,
             "reference": ref_what, "window_mm": [lo, hi] if lo is not None else None,
             "spread_mm": checklib.rnd(max(lens) - min(lens)) if lens else None,
             "not_longer_than_mm": checklib.rnd(cap) if cap is not None else None,
             "ref_nets": refs, "cap_nets": caps,
             "max_mm": max_mm, "min_mm": min_mm}
    return vs, facts


def ddr_nets(nets) -> list[str]:
    return sorted(n for n in nets if n and DDR_DATA.search(n))


# ------------------------------------------------------------ CLI

def run(argv=None):
    ap = argparse.ArgumentParser(
        description="Length-matched net groups against their reference.")
    ap.add_argument("--pcb", required=True, help="path to .kicad_pcb")
    ap.add_argument("--constraints", help="constraints.json (length_groups)")
    ap.add_argument("--out", help="write JSON report here instead of stdout")
    args = ap.parse_args(argv)

    cons = checklib.load_json(args.constraints, "constraints") \
        if args.constraints else {}
    groups = cons.get("length_groups")
    if groups is not None and not isinstance(groups, list):
        raise CheckError("constraints length_groups is not a list")
    groups = groups or []
    for i, g in enumerate(groups):
        validate_group(i, g)

    bg = geom.load_board(Path(args.pcb))
    violations: list[dict] = []
    checked: list[dict] = []
    if not groups:
        ddr = ddr_nets(bg.nets)
        if len(ddr) >= DDR_MIN_NETS:
            violations.append(violation(
                SCRIPT, "warning", None, None, ddr[0], [],
                f"{len(ddr)} DDR data nets ({ddr[0]} ...) but constraints "
                f"declare no length_groups: nothing was length-matched",
                SCRIPT, kind="length_group_undeclared", nets=ddr))
    cache: dict = {}
    for g in groups:
        vs, facts = check_group(bg, g, cache)
        violations.extend(vs)
        checked.append(facts)
    payload = checklib.report(SCRIPT, args.pcb, violations, groups=checked,
                              stackup_assumed=bool(bg.stackup.assumed),
                              nothing_to_judge=None if checked else
                              "no length_groups in constraints")
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
