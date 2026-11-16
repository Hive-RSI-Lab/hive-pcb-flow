"""check_diffpair.py - differential-pair skew / coupling / symmetry (SPEC 6.3).

Per pair (from constraints.json["diff_pairs"], else auto-discovered by name):
 - intra-pair length SKEW (mm and ps via stackup epsilon_r), measured on each
   net's TRUNK - the branch-free path between the pair's two matched terminals
   (a USB D+ pull-up stub, a series-R tap, etc. must NOT inflate the length);
 - GAP consistency: nearest-partner distance sampled along the coupled run
   (min / median / p90 / max), warned if it wanders beyond tolerance;
 - UNCOUPLED length: run of either trace whose partner has walked away farther
   than coupling_gap_max - the fingerprint of a one-sided meander/detour.
   Copper inside a ball field that holds one of the pair's balls (the BGA's
   ball bbox grown by one pitch, lib/bgalib) is BREAKOUT, reported as
   breakout_p_mm / breakout_n_mm and not judged: between dog-bone vias a
   0.8 mm field has room for one track per channel, so P and N leave it
   one ball pitch apart by construction (#529);
 - VIA symmetry: the two nets should transition layers together;
 - RUNNING skew (dynamic phase, #476): along the coupled trunk, each sample
   of the P centreline is matched to the nearest point of N's trunk on the
   same layer; s_p - s_n (arc length from the matched start terminals) is the
   skew built up so far. A run of samples over max_phase_skew_mm (default
   max_skew_mm) of one sign, lasting at least phase_window_mm (else rise_ps
   / 10 as distance, else 5 mm), is a diffpair_phase finding - a pair with
   +1 mm at one bend and -1 mm at the next ends at skew 0 but carries 1 mm of
   phase error in between. The excursion that runs into the far end and is
   no bigger than the end skew is the end skew itself (diffpair_skew's);
 - IMPEDANCE as drawn: the pair's dominant width and its median edge gap on
   its main layer, solved by lib/impedance.py's 2D field solver against the
   board's own stackup (coated microstrip outside, stripline inside). Always
   reported as zdiff_ohm; when the spec carries impedance_ohm, a miss beyond
   impedance_tol_pct (default 10, JLC's standard tolerance) warns and beyond
   twice that is an error.
 - ZONE PAIRS (#890): a pair drawn as two zones and no tracks (hw-lab
   zcoupon) is sampled along the edge P shows N: the ray across the gap
   gives the edge gap and, carried on, each strip's width at the same spot.
   Each layer both fill reports its widths, gap and Zdiff (judged against
   impedance_ohm as above); a side narrower than its partner by more than
   max_width_imbalance_pct (default 10) over ZONE_IMBALANCE_MM is a
   diffpair_width_imbalance error. Each strip's length is area / width,
   taken only where that agrees with its outline (perimeter / 2 - width);
   the sum per net gives diffpair_skew, the copper beyond coupling_max of
   the partner, over the strip's width, diffpair_uncoupled, and the via
   counts diffpair_via_asymmetry, at the track limits (#1000). A zone that
   is not one strip (a pad, a pour, a stepped width) leaves skew and
   uncoupled length not_judged with where; phase is always not_judged on a zone, which has
   no drawn centreline. A MIXED pair, tracks into a zone strip and out,
   closes its trunk through the strip at the strip's length and judges the
   layer both nets fill as above.

Why not raw length? Adding a meander to the SHORTER trace (the corpus
diffpair-skew mutant does exactly this) barely moves the length skew but is a
real signal-integrity defect - it is caught by uncoupled length, not by skew.
Both are reported; the pair fails on whichever exceeds its threshold.

The trunk is found on netconn's per-net connectivity graph (segments joined
through vias, pads and overlapping same-layer end caps - LEARNINGS 2026-07-29:
an endpoint-snap-only graph turns sub-0.3 mm endpoint mismatches into a
total-copper-length "skew"). The shortest path between two matched terminal
PAD nodes ignores dead-end stubs; if the terminals cannot be connected at all
the length falls back to total copper and a diffpair_open_trunk warning is
emitted so the bogus skew is never silent.

CLI: --pcb board.kicad_pcb [--constraints constraints.json] [--out report.json]
     exit 0/1/2 per SPEC section 6.

constraints.json["diff_pairs"] entries (all keys but the nets optional):
    {"p": "/USB_DP", "n": "/USB_DM",   # exact board net names
     "gap_mm": 0.65,                   # nominal centre-to-centre pitch
     "max_skew_mm": 5.0,               # length-match tolerance
     "max_uncoupled_mm": 5.0,          # allowed one-sided uncoupled run
     "coupling_factor": 3.0,           # coupled if gap <= factor * nominal
     "term_pair_mm": 2.5,              # cross-ref terminal pairing window
     "impedance_ohm": 90,              # differential target
     "impedance_tol_pct": 10,          # allowed miss, percent
     "max_phase_skew_mm": 0.15,        # running-skew limit (default max_skew_mm)
     "phase_window_mm": 5.0,           # shorter excursions are not judged
     "rise_ps": 500,                   # else window = rise_ps / 10 as distance
     "kind": "sense"}                  # DC Kelvin / low-frequency sense pair
A "kind" of sense (also kelvin, dc_sense, low_frequency) marks a shunt-sense
pair: no skew, uncoupled-length or impedance limits (high-speed rules mean
nothing at DC); open trunk and via asymmetry still apply, and an optional
"max_sense_uncoupled_mm" keeps a "routed together" limit. Auto-discovered
pairs get kind sense only on a narrow name rule (is_sense_name).
"""
from __future__ import annotations

import argparse
import math
import os
import re
import statistics
import sys
from pathlib import Path

from shapely.geometry import LineString, Point, box
from shapely.ops import nearest_points, unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
import impedance  # noqa: E402
import netconn  # noqa: E402
import bgalib  # noqa: E402
from checklib import CheckError, violation  # noqa: E402

SCRIPT = "check_diffpair"
C_MM_PER_PS = 0.299792458      # speed of light, mm/ps
MAX_SKEW_MM = 5.0              # default intra-pair length-match tolerance
MAX_UNCOUPLED_MM = 5.0         # default one-sided uncoupled run
COUPLING_FACTOR = 3.0          # coupled where gap <= factor * nominal pitch
TERM_PAIR_MM = 2.5             # cross-ref matched-terminal window (same-ref: none)
GAP_TOL_MM = 0.5              # coupled-region gap may wander this much (warn)
IMPEDANCE_TOL_PCT = 10.0       # JLC's standard controlled-impedance tolerance
SAMPLE_MM = 0.1               # centerline sampling step for gap / uncoupled
PHASE_WINDOW_MM = 5.0         # default: shorter running-skew excursions pass
TAIL_TOL_MM = 0.05            # an end excursion this close to the end skew is it

# Suffix pairs used to auto-discover pairs: two nets sharing a stem whose final
# token is (positive, negative). Deliberately conservative - the ambiguous
# single-letter H/L (hi/lo bus bytes) and P/M pairs are excluded so a bus or an
# ordinary net pair is not mistaken for a differential pair (which would then
# false-positive on uncoupled length). USB D+/D- is matched via the DP/DM token,
# not a bare P/M. Explicit constraints.json["diff_pairs"] always wins.
SUFFIX_PAIRS = [("_P", "_N"), ("DP", "DM"), ("D+", "D-"), ("+", "-"), ("P", "N")]
POSITIVE = {"_P", "DP", "D+", "+", "P"}       # which token is the + net


SENSE_KINDS = {"sense", "kelvin", "dc_sense", "low_frequency"}
# Narrow name default for AUTO-discovered pairs only (an explicit diff_pairs
# entry classifies itself via "kind"): the last path segment of the stem is a
# current-sense name. Deliberately not "SNS"/"SENSE" alone (VSNS, feedback
# sense nets are not shunt pairs).
SENSE_STEM = re.compile(r"(?:^|[/_])(ISNS|ISENSE|ISEN|CSA|SHUNT|KELVIN)\d*_?$")


def is_sense_name(p: str, n: str) -> bool:
    """True when the auto-discovered pair p/n names a shunt-sense pair."""
    stem = os.path.commonprefix([p.upper(), n.upper()])
    return bool(SENSE_STEM.search(stem))


def is_sense(spec: dict) -> bool:
    return str(spec.get("kind", "")).lower() in SENSE_KINDS


# ------------------------------------------------------------ pairing

def discover_pairs(nets) -> list[tuple[str, str]]:
    """Auto-pair nets by name: equal stem, complementary final token.
    Returns (positive, negative) ordered tuples."""
    names = sorted(n for n in nets if n)
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for name in names:
        if name in seen:
            continue
        up = name.upper()
        for a, b in SUFFIX_PAIRS:
            matched = None
            for lo, hi in ((a, b), (b, a)):
                if up.endswith(lo):
                    stem = up[: len(up) - len(lo)]
                    cand = [m for m in names if m != name
                            and m.upper() == stem + hi and m not in seen]
                    if cand:
                        pos = name if lo in POSITIVE else cand[0]
                        neg = cand[0] if pos == name else name
                        matched = (pos, neg, cand[0])
                        break
            if matched:
                pairs.append((matched[0], matched[1]))
                seen.update((name, matched[2]))
                break
    return pairs


# ------------------------------------------------------------ trunk

def _pad_dist(a, b) -> float:
    return math.hypot(a.center[0] - b.center[0], a.center[1] - b.center[1])


def matched_terminals(bg, p, n, term_pair_mm=TERM_PAIR_MM):
    """The (p_pad, n_pad) terminal pairs bounding the coupled trunk.

    SAME-REF pairing first, with NO distance cap: a footprint that carries
    both halves matches its own pads however far apart they sit (LEARNINGS
    2026-07-29: a magjack's RXP/RXN pads are 4.58 mm apart, so any window
    drops the connector terminal and the escape is never measured). Only a
    p-pad with no same-ref n-pad uses the `term_pair_mm` window (spec key
    "term_pair_mm", default 2.5 mm). Unmatched pads (series-R tap, pull-up)
    are branch ends and excluded."""
    p_pads, n_pads = bg.pads_of(net=p), bg.pads_of(net=n)
    matches = []
    for pp in p_pads:
        same = [nn for nn in n_pads if nn.ref == pp.ref]
        if same:
            matches.append((pp, min(same, key=lambda nn: _pad_dist(pp, nn))))
            continue
        best, bd = None, term_pair_mm
        for nn in n_pads:
            d = _pad_dist(pp, nn)
            if d <= bd:
                best, bd = nn, d
        if best is not None:
            matches.append((pp, best))
    return matches


def trunk_length(bg, net, terminals):
    """(length_mm, branch_free, unreachable): path length of `net` between its
    two farthest matched-terminal PAD nodes on the netconn connectivity graph.
    Falls back to total track length (branch_free False) when the graph cannot
    connect any terminal pair; `unreachable` then names the terminal pads
    ("REF.pin") so the caller can report the open trunk."""
    g = netconn.build(bg, net)
    total = sum(t.length for t in bg.tracks_of(net))
    if len(terminals) < 2:
        return total, False, sorted(f"{t.ref}.{t.number}" for t in terminals)
    named = [(f"{t.ref}.{t.number}", netconn.pad_node(g, t)) for t in terminals]
    best = None
    for i in range(len(named)):
        for j in range(i + 1, len(named)):
            L = netconn.shortest_path_len(g, named[i][1], named[j][1])
            if L is not None and (best is None or L > best):
                best = L
    if best is None:
        return total, False, sorted(name for name, _ in named)
    return best, True, []


# ------------------------------------------------------------ running skew

def trunk_pieces(g, steps):
    """([(line, layer, s0, track)], length) for a netconn.shortest_path step
    list: each track edge as a LineString oriented along the path, s0 the
    path length before it (join edges - via, pad, cap overlap - add their
    weight, as trunk_length counts them)."""
    out, s = [], 0.0
    for u, _v, w, eid in steps:
        t = g.tracks.get(eid) if not isinstance(eid, tuple) else None
        if t is not None:
            a = t.shape.coords[0]
            fwd = u == ("pt", netconn._snap(a, netconn.SNAP))
            line = t.shape if fwd else LineString(list(t.shape.coords)[::-1])
            out.append((line, t.layer, s, t))
        s += w
    return out, s


def pair_trunks(bg, p, n, term):
    """The two trunks between the SAME matched terminal pairs (the pair of
    pairs giving the longest P path), each as trunk_pieces; None when either
    net cannot connect them."""
    gp, gn = netconn.build(bg, p), netconn.build(bg, n)
    best = None
    for i in range(len(term)):
        for j in range(i + 1, len(term)):
            rp = netconn.shortest_path(gp, netconn.pad_node(gp, term[i][0]),
                                       netconn.pad_node(gp, term[j][0]))
            rn = netconn.shortest_path(gn, netconn.pad_node(gn, term[i][1]),
                                       netconn.pad_node(gn, term[j][1]))
            if rp and rn and (best is None or rp[0] > best[0]):
                best = (rp[0], rp, rn)
    if best is None:
        return None
    _, rp, rn = best
    pp, lp = trunk_pieces(gp, rp[1])
    pn, ln = trunk_pieces(gn, rn[1])
    return {"p": pp, "n": pn, "lp": lp, "ln": ln}


def _tangent(line, at: float):
    L = line.length
    a = line.interpolate(max(0.0, at - 0.01))
    b = line.interpolate(min(L, at + 0.01))
    d = math.hypot(b.x - a.x, b.y - a.y)
    return ((b.x - a.x) / d, (b.y - a.y) / d) if d > 0 else (0.0, 0.0)


def running_skew(trunks, coupling_max, step=SAMPLE_MM):
    """[(s_p, s_n, r, (x, y))] along P's trunk, in path order: each sample of
    P matched to the nearest point of N's trunk on the same layer, r = s_p -
    s_n the skew built up so far (+ = P longer). A sample counts only when N
    runs ABREAST of it: within coupling_max, parallel (|cos| >= 0.9) and
    with the foot inside the N segment, not clamped to its end - otherwise a
    P sample beside a one-sided detour maps to the detour leg's foot and
    reads as a ramp. Corners and detours therefore show as steps."""
    by_layer: dict[str, list] = {}
    for line, layer, s0, _ in trunks["n"]:
        if line.length > 0:
            by_layer.setdefault(layer, []).append((line, s0))
    out = []
    for line, layer, s0, _ in trunks["p"]:
        cands = by_layer.get(layer)
        L = line.length
        if not cands or L <= 0:
            continue
        k = max(1, int(math.ceil(L / step)))
        for i in range(k):
            sl = (i + 0.5) * L / k
            pt = line.interpolate(sl)
            tp = None
            best = None
            for nl, ns0 in cands:
                d = nl.distance(pt)
                if d > coupling_max or (best is not None and d >= best[0]):
                    continue
                t = nl.project(pt)
                if t <= 1e-6 or t >= nl.length - 1e-6:
                    continue                    # clamped: not abreast
                if tp is None:
                    tp = _tangent(line, sl)
                tn = _tangent(nl, t)
                if abs(tp[0] * tn[0] + tp[1] * tn[1]) < 0.9:
                    continue
                best = (d, ns0 + t)
            if best is None:
                continue
            sp = s0 + sl
            out.append((sp, best[1], sp - best[1], (pt.x, pt.y)))
    return out


def excursions(samples, limit, step=SAMPLE_MM) -> list[dict]:
    """Runs of consecutive samples with |r| over `limit` and one sign."""
    out, cur = [], None
    for i, (sp, _sn, r, xy) in enumerate(samples):
        sign = 1 if r > 0 else -1
        if abs(r) > limit and cur is not None and cur["sign"] == sign:
            cur["i1"] = i
            if abs(r) > abs(cur["peak"]):
                cur["peak"], cur["at"] = r, xy
            continue
        if cur is not None:
            out.append(cur)
            cur = None
        if abs(r) > limit:
            cur = {"sign": sign, "i0": i, "i1": i, "peak": r, "at": xy}
    if cur is not None:
        out.append(cur)
    for e in out:
        e["from_mm"] = samples[e["i0"]][0]
        e["to_mm"] = samples[e["i1"]][0]
        e["length_mm"] = e["to_mm"] - e["from_mm"] + step
    return out


def phase_window(spec, eps) -> float:
    if spec.get("phase_window_mm") is not None:
        return float(spec["phase_window_mm"])
    if spec.get("rise_ps"):
        return float(spec["rise_ps"]) / 10 * C_MM_PER_PS / math.sqrt(eps)
    return PHASE_WINDOW_MM


def pair_phase(bg, spec, term, coupling_max, eps) -> dict | None:
    """Running-skew profile of a pair (module doc): trunks, samples, limit,
    window, signed end skew, every excursion and the flagged ones (long
    enough and not the end skew's own tail). None when the trunks are open."""
    tr = pair_trunks(bg, spec["p"], spec["n"], term)
    if tr is None:
        return None
    samples = running_skew(tr, coupling_max)
    limit = float(spec.get("max_phase_skew_mm",
                           spec.get("max_skew_mm", MAX_SKEW_MM)))
    window = phase_window(spec, eps)
    end = tr["lp"] - tr["ln"]
    exc = excursions(samples, limit)
    for e in exc:
        e["tail"] = (e["i1"] == len(samples) - 1 and e["sign"] * end > 0
                     and abs(e["peak"]) <= abs(end) + TAIL_TOL_MM)
    flagged = [e for e in exc if e["length_mm"] >= window - 1e-9 and not e["tail"]]
    peak = max((r for _, _, r, _ in samples), key=abs, default=0.0)
    return {"trunks": tr, "samples": samples, "limit_mm": limit,
            "window_mm": window, "end_skew_mm": end, "peak_mm": peak,
            "excursions": exc, "flagged": flagged}


# ------------------------------------------------------------ coupling

def net_geometry(bg, net):
    return unary_union([t.shape for t in bg.tracks_of(net)])


def sample_gaps(line_geom, other_geom, step=SAMPLE_MM, with_points=False):
    """(arc_len, gap) samples of one net's centerline vs the other net;
    (arc_len, gap, midpoint) with with_points."""
    out = []
    if line_geom.is_empty or other_geom.is_empty:
        return out  # distance to an empty geometry is NaN; caller handles
    for line in getattr(line_geom, "geoms", [line_geom]):
        L = line.length
        if L <= 0:
            continue
        nseg = max(1, int(round(L / step)))
        for i in range(nseg):
            seglen = L / nseg
            mid = line.interpolate((i + 0.5) * seglen)
            out.append((seglen, other_geom.distance(mid), mid) if with_points
                       else (seglen, other_geom.distance(mid)))
    return out


def breakout_region(bg, p, n):
    """Union of the ball fields that hold a ball of p or n: the BGA's ball
    bbox grown by one pitch (the escape stubs end just past the outer row).
    None when the pair has no ball."""
    regions = []
    for b in bgalib.find_bgas(bg):
        if p in b.nets.values() or n in b.nets.values():
            x0, y0, x1, y1 = b.bbox
            regions.append(box(x0, y0, x1, y1).buffer(b.pitch, join_style=2))
    return unary_union(regions) if regions else None


def coupling_stats(bg, p, n, coupling_max, breakout=None):
    gp, gn = net_geometry(bg, p), net_geometry(bg, n)
    sp = sample_gaps(gp, gn, with_points=True)
    sn = sample_gaps(gn, gp, with_points=True)

    def split(samples):
        inside = [(sl, g) for sl, g, pt in samples
                  if breakout is not None and breakout.contains(pt)]
        if not inside:
            return [(sl, g) for sl, g, _ in samples], 0.0
        return ([(sl, g) for sl, g, pt in samples if not breakout.contains(pt)],
                sum(sl for sl, _ in inside))

    sp, bo_p = split(sp)
    sn, bo_n = split(sn)
    unc_p = sum(sl for sl, g in sp if g > coupling_max)
    unc_n = sum(sl for sl, g in sn if g > coupling_max)
    coupled = [g for sl, g in sp + sn if g <= coupling_max]
    allgaps = sorted(g for _, g in sp + sn)
    nominal = statistics.median([g for _, g in sp + sn]) if allgaps else 0.0
    stats = {
        "nominal_gap_mm": checklib.rnd(nominal),
        "gap_min_mm": checklib.rnd(allgaps[0]) if allgaps else None,
        "gap_median_mm": checklib.rnd(statistics.median(allgaps)) if allgaps else None,
        "gap_p90_mm": checklib.rnd(allgaps[int(0.9 * (len(allgaps) - 1))]) if allgaps else None,
        "coupled_gap_max_mm": checklib.rnd(max(coupled)) if coupled else None,
    }
    if breakout is not None:
        stats["breakout_p_mm"] = checklib.rnd(bo_p)
        stats["breakout_n_mm"] = checklib.rnd(bo_n)
    return unc_p, unc_n, stats


# ------------------------------------------------------------ per-pair check

def check_pair(bg: geom.BoardGeom, spec: dict):
    p, n = spec.get("p"), spec.get("n")
    if p not in bg.nets or n not in bg.nets:
        raise CheckError(f"diff pair {p!r}/{n!r} not both on board")
    factor = float(spec.get("coupling_factor", COUPLING_FACTOR))

    # A pair with an unrouted half cannot be coupling-checked (an empty geometry
    # makes distance() NaN); report it as not-routed and judge nothing yet.
    tracks_p, tracks_n = bg.tracks_of(p), bg.tracks_of(n)
    if not tracks_p and not tracks_n:
        fp, fn = zone_fills(bg, p), zone_fills(bg, n)
        if set(fp) & set(fn):            # drawn as two zones (#890)
            nom = float(spec.get("gap_mm") or 0.2)
            return zone_pair(bg, spec, p, n, fp, fn,
                             max(factor * nom, nom + 0.5))
    if not tracks_p or not tracks_n:
        return [], {"pair": [p, n], "routed": False,
                    "length_p_mm": checklib.rnd(sum(t.length for t in tracks_p)),
                    "length_n_mm": checklib.rnd(sum(t.length for t in tracks_n)),
                    "note": "pair not fully routed; coupling not evaluated"}

    term = matched_terminals(bg, p, n, float(spec.get("term_pair_mm",
                                                      TERM_PAIR_MM)))
    lp, okp, unreach_p = trunk_length(bg, p, [pp for pp, nn in term])
    ln, okn, unreach_n = trunk_length(bg, n, [nn for pp, nn in term])
    # A mixed pair (#1000): a trunk the tracks leave open may close through
    # the net's own zone strip; it then counts the strip's length, or, when
    # the zone is not one strip, closes with its length not judged.
    through_zone, zone_why = [], []
    for net, ok, side in ((p, okp, 0), (n, okn, 1)):
        if ok or not bg.zones_of(net):
            continue
        r = zone_trunk_length(bg, net, [pair[side] for pair in term])
        if r is None:
            continue
        through_zone.append(net)
        if r[0] is None:
            zone_why.append(r[2])
        if net == p:
            lp, okp, unreach_p = (r[0] if r[0] is not None else lp), True, []
        else:
            ln, okn, unreach_n = (r[0] if r[0] is not None else ln), True, []
    skew = abs(lp - ln)

    # nominal pitch drives the coupling threshold
    nom = spec.get("gap_mm")
    if nom is None:
        _, _, s0 = coupling_stats(bg, p, n, 1e9)
        nom = s0["nominal_gap_mm"] or 0.2
    coupling_max = max(factor * nom, nom + 0.5)
    unc_p, unc_n, stats = coupling_stats(bg, p, n, coupling_max,
                                         breakout_region(bg, p, n))
    # the part of a mixed pair drawn as zones on a layer both nets fill:
    # judged per layer as a zone pair, its strips' uncoupled length added
    zone_vs, zone_rows, zone_strips_, zone_unc_why = [], {}, {}, []
    fp, fn = zone_fills(bg, p), zone_fills(bg, n)
    if set(fp) & set(fn):
        zone_vs, zone_rows, zone_strips_ = zone_layers(bg, spec, p, n, fp, fn,
                                                       coupling_max)
        _, _, zup, zun, zwhy = strips_measure(p, n, zone_strips_,
                                              coupling_max)
        unc_p += zup or 0.0
        unc_n += zun or 0.0
        if zup is None or zun is None:
            zone_unc_why = zwhy or ["a side is wider than a strip"]

    eps = _pair_epsilon(bg, p, n)
    skew_ps = skew * math.sqrt(eps) / C_MM_PER_PS

    rep_pt = _rep_point(bg, p)

    violations: list[dict] = []
    common = dict(kind=None, pair=[p, n], length_p_mm=checklib.rnd(lp),
                  length_n_mm=checklib.rnd(ln), branch_free=bool(okp and okn))

    # An open trunk means the length above is TOTAL copper, not a trunk - the
    # skew number is meaningless and must never pass silently (LEARNINGS
    # 2026-07-29: reported only as branch_free:false, never as a violation).
    for bad_net, ok, length, unreach in ((p, okp, lp, unreach_p),
                                         (n, okn, ln, unreach_n)):
        if ok:
            continue
        who = ", ".join(unreach) if unreach else "fewer than 2 matched pads"
        violations.append(violation(
            SCRIPT, "warning", _rep_point(bg, bad_net), None, bad_net, [],
            f"diff pair {p}/{n}: {bad_net} trunk is open - cannot connect "
            f"terminal(s) {who}; its length {length:.2f} mm is total copper "
            f"length, so the reported skew is not a trunk skew",
            SCRIPT, **{**common, "kind": "diffpair_open_trunk",
                       "fallback_length_mm": checklib.rnd(length)}))

    sense = is_sense(spec)
    measured = not zone_why
    violations += pair_rules(spec, p, n, lp if measured else None,
                             ln if measured else None, unc_p, unc_n,
                             coupling_max, eps, rep_pt, common)
    # Gap consistency is REPORTED (the spec's "gap deviation histogram") but is
    # not a gate: a legitimate pair fans out to > nominal gap at its pad
    # breakouts, which must not false-positive. The one-sided detour that a bad
    # gap would signal is already caught, with a clean pad-breakout margin, by
    # the uncoupled-length term above.
    vv, vp, vn = via_rule(bg, spec, p, n, rep_pt, common)
    violations += vv
    violations += zone_vs

    phase = None
    if okp and okn and not sense and not through_zone:
        ph = pair_phase(bg, spec, term, coupling_max, eps)
        if ph is not None:
            phase = {"peak_mm": checklib.rnd(ph["peak_mm"]),
                     "end_skew_mm": checklib.rnd(ph["end_skew_mm"]),
                     "limit_mm": ph["limit_mm"],
                     "window_mm": checklib.rnd(ph["window_mm"]),
                     "samples": len(ph["samples"]),
                     "excursions": [{"from_mm": checklib.rnd(e["from_mm"]),
                                     "to_mm": checklib.rnd(e["to_mm"]),
                                     "peak_mm": checklib.rnd(e["peak"])}
                                    for e in ph["flagged"]]}
            if ph["flagged"]:
                worst = max(ph["flagged"], key=lambda e: abs(e["peak"]))
                pk = abs(worst["peak"])
                violations.append(violation(
                    SCRIPT, "error" if pk > 2 * ph["limit_mm"] else "warning",
                    worst["at"], None, p, [],
                    f"diff pair {p}/{n} running skew reaches {worst['peak']:+.2f} mm "
                    f"over {worst['length_mm']:.1f} mm of its trunk "
                    f"({worst['from_mm']:.1f}-{worst['to_mm']:.1f} mm from "
                    f"{term[0][0].ref if term else '?'}); limit "
                    f"{ph['limit_mm']:g} mm over {ph['window_mm']:.1f} mm (end skew "
                    f"{ph['end_skew_mm']:+.2f} mm): tune near the bend that "
                    f"builds it", SCRIPT,
                    **{**common, "kind": "diffpair_phase",
                       "peak_mm": checklib.rnd(worst["peak"]),
                       "from_mm": checklib.rnd(worst["from_mm"]),
                       "to_mm": checklib.rnd(worst["to_mm"]),
                       "limit_mm": ph["limit_mm"],
                       "window_mm": checklib.rnd(ph["window_mm"]),
                       "excursions": len(ph["flagged"])}))

    zinfo = pair_impedance(bg, p, n, stats["gap_median_mm"])
    zinfo["layers"] = layer_impedance(bg, p, n, coupling_max)
    target = None if sense else spec.get("impedance_ohm")
    if target and zinfo.get("zdiff_ohm"):
        tol = float(spec.get("impedance_tol_pct", IMPEDANCE_TOL_PCT))
        miss = (zinfo["zdiff_ohm"] - float(target)) / float(target) * 100
        if abs(miss) > tol:
            violations.append(violation(
                SCRIPT, "error" if abs(miss) > 2 * tol else "warning", rep_pt, None,
                p, [], f"diff pair {p}/{n} is {zinfo['zdiff_ohm']:.1f} ohm as drawn "
                f"(width {zinfo['width_mm']} mm, gap {zinfo['gap_mm']} mm on "
                f"{zinfo['signal_layer']}); target {float(target):g} ohm +/-{tol:g}%",
                SCRIPT, **{**common, "kind": "diffpair_impedance",
                           "target_ohm": float(target), "miss_pct": checklib.rnd(miss),
                           **zinfo}))

    if target:
        tol = float(spec.get("impedance_tol_pct", IMPEDANCE_TOL_PCT))
        for lay, row in zinfo["layers"].items():
            if lay == zinfo.get("signal_layer") or not row.get("zdiff_ohm"):
                continue          # the main layer is judged above
            miss = (row["zdiff_ohm"] - float(target)) / float(target) * 100
            if abs(miss) > tol:
                violations.append(violation(
                    SCRIPT, "error" if abs(miss) > 2 * tol else "warning", rep_pt,
                    lay, p, [], f"diff pair {p}/{n} is {row['zdiff_ohm']:.1f} ohm "
                    f"as drawn on {lay} (width {row['width_mm']} mm, gap "
                    f"{row['gap_mm']} mm, {row['coupled_mm']} mm coupled); target "
                    f"{float(target):g} ohm +/-{tol:g}%",
                    SCRIPT, **{**common, "kind": "diffpair_impedance",
                               "target_ohm": float(target),
                               "miss_pct": checklib.rnd(miss), **row}))

    facts = {"pair": [p, n], "kind": "sense" if sense else "diff", "length_p_mm": checklib.rnd(lp),
             "length_n_mm": checklib.rnd(ln), "skew_mm": checklib.rnd(skew),
             "skew_ps": checklib.rnd(skew_ps), "branch_free": bool(okp and okn),
             "uncoupled_p_mm": checklib.rnd(unc_p),
             "uncoupled_n_mm": checklib.rnd(unc_n),
             "coupling_max_mm": checklib.rnd(coupling_max),
             "vias_p": vp, "vias_n": vn, **stats, "impedance": zinfo,
             **({"phase": phase} if phase is not None else {})}
    not_judged = []
    if zone_why and not sense:
        facts.pop("skew_mm")
        facts.pop("skew_ps")
        not_judged.append("skew: " + "; ".join(zone_why))
    if zone_unc_why:
        not_judged.append("uncoupled length: only the tracks' share is "
                          "counted; " + "; ".join(zone_unc_why))
    if through_zone:
        facts["trunk_through_zone"] = through_zone
        if not sense:
            not_judged.append(PHASE_ZONES)
    if zone_rows:
        facts["zones"] = zone_rows
    if not_judged:
        facts["not_judged"] = not_judged
    return violations, facts


def layer_zdiff(bg, layer: str, w: float, s: float) -> tuple[float | None, str]:
    """(Zdiff, model) of a pair of width w and edge gap s on `layer`: an
    outer layer as coated microstrip over its neighbour, an inner one as
    stripline sitting on the dielectric it was etched on (the core;
    impedance.stripline_sides) and embedded in the other. (None, "") when
    the stackup has no reference plane."""
    if len(bg.copper_layers) < 2 or not bg.stackup.dielectrics:
        return None, ""
    above, below = bg.stackup.adjacent(layer)
    t = bg.stackup.copper_thickness.get(layer, 0.035)
    if above is None or below is None:
        ref = below or above
        return impedance.field_microstrip(
            w, bg.stackup.height_between(layer, ref), t,
            bg.stackup.epsilon_between(layer, ref), s)["zdiff"], "coated microstrip"
    (hb, eb), (hf, ef) = impedance.stripline_sides(bg.stackup.side(layer, above),
                                                   bg.stackup.side(layer, below))
    return impedance.field_stripline(w, hb, hf, t, eb, ef, s)["zdiff"], "stripline"


def pair_impedance(bg, p, n, gap_c2c) -> dict:
    """Zdiff of the pair as drawn on its main layer (the layer carrying most of
    its length): the length-dominant track width and the median centre-to-
    centre gap minus that width. An inner layer is solved as stripline with
    the trace sitting on the dielectric it was etched on (the core;
    impedance.stripline_sides) and embedded in the other."""
    tracks = bg.tracks_of(p) + bg.tracks_of(n)
    by_layer: dict[str, float] = {}
    for t in tracks:
        by_layer[t.layer] = by_layer.get(t.layer, 0.0) + t.length
    sig = max(by_layer, key=by_layer.get)
    by_w: dict[float, float] = {}
    for t in tracks:
        if t.layer == sig:
            by_w[round(t.width, 4)] = by_w.get(round(t.width, 4), 0.0) + t.length
    w = max(by_w, key=by_w.get)
    out = {"signal_layer": sig, "width_mm": w}
    if gap_c2c is None or gap_c2c - w <= 0.01:
        return {**out, "note": "no measurable edge gap; impedance not solved"}
    s = round(gap_c2c - w, 4)
    out["gap_mm"] = s
    try:
        z, model = layer_zdiff(bg, sig, w, s)
    except (ValueError, RuntimeError, ZeroDivisionError) as e:
        return {**out, "note": f"impedance not solved: {e}"}
    if z is None:
        return {**out, "note": "no reference plane in the stackup; impedance not solved"}
    return {**out, "zdiff_ohm": checklib.rnd(z), "model": model,
            "stackup_assumed": bool(bg.stackup.assumed)}


LAYER_MIN_COUPLED_MM = 2.0    # a layer judged on its own carries this much pair


def layer_impedance(bg, p, n, coupling_max) -> dict:
    """Zdiff on EACH layer the pair runs coupled for >= LAYER_MIN_COUPLED_MM
    (#489: a pair that moves L1 -> L3 changes width and gap with the layer,
    so the main-layer figure alone says nothing about the other run).
    {layer: {width_mm, gap_mm, coupled_mm, zdiff_ohm, model}}."""
    out: dict[str, dict] = {}
    layers = sorted({t.layer for t in bg.tracks_of(p)} & {t.layer for t in bg.tracks_of(n)})
    for lay in layers:
        tp = [t for t in bg.tracks_of(p) if t.layer == lay]
        tn = [t for t in bg.tracks_of(n) if t.layer == lay]
        gp = unary_union([t.shape for t in tp])
        gn = unary_union([t.shape for t in tn])
        samples = [(sl, g) for sl, g in sample_gaps(gp, gn) + sample_gaps(gn, gp)
                   if g <= coupling_max]
        coupled = sum(sl for sl, _ in samples) / 2.0
        if coupled < LAYER_MIN_COUPLED_MM:
            continue
        by_w: dict[float, float] = {}
        for t in tp + tn:
            by_w[round(t.width, 4)] = by_w.get(round(t.width, 4), 0.0) + t.length
        w = max(by_w, key=by_w.get)
        c2c = statistics.median(g for _, g in samples)
        row = {"width_mm": w, "gap_mm": checklib.rnd(c2c - w),
               "coupled_mm": checklib.rnd(coupled)}
        if c2c - w > 0.01:
            try:
                z, model = layer_zdiff(bg, lay, w, round(c2c - w, 4))
            except (ValueError, RuntimeError, ZeroDivisionError):
                z, model = None, ""
            if z is not None:
                row.update(zdiff_ohm=checklib.rnd(z), model=model)
        out[lay] = row
    return out


# ------------------------------------------------------------ zone pairs (#890)

ZONE_IMBALANCE_PCT = 10.0     # a zone pair's sides may differ by this much
ZONE_IMBALANCE_MM = 1.0       # over a stretch at least this long
ZONE_RAY_MM = 5.0             # a cross-section this wide is a pour, not a strip
STRIP_AGREE_MM = 0.5          # area/width and outline lengths of a strip agree
STRIP_AGREE_PCT = 5.0         # to this much (the larger), else not a strip
PHASE_ZONES = ("phase: a zone strip has no drawn centreline, so the running "
               "skew along it is not measured")


def zone_fills(bg, net) -> dict:
    """{layer: filled copper} of the net's zones, layers with a fill only."""
    out = {}
    for lay in bg.copper_layers:
        g = bg.zone_fill(net, lay) if bg.zones_of(net, lay) else None
        if g is not None and not g.is_empty:
            out[lay] = g
    return out


def _across(poly, at, d) -> float | None:
    """How far the ray from boundary point `at` along unit vector d runs
    inside poly before it leaves it: the strip's width there. None when the
    ray leaves at once (the far edge) or never (a pour)."""
    ray = LineString([at, (at[0] + d[0] * ZONE_RAY_MM,
                           at[1] + d[1] * ZONE_RAY_MM)])
    hit = ray.intersection(poly)
    first = [g for g in getattr(hit, "geoms", [hit])
             if g.geom_type == "LineString" and g.length > 1e-6
             and g.distance(Point(at)) < 1e-4]
    if not first or first[0].length >= ZONE_RAY_MM - 1e-3:
        return None
    return first[0].length


def strip_width(poly, step=SAMPLE_MM) -> float | None:
    """A zone strip's own width: the median, over boundary points every
    step, of the ray along the inward normal across the copper. The end
    caps' rays run along the strip and drop out of the median of a strip
    longer than a few widths. None when no ray crosses (a pour)."""
    ws = []
    for ring in [poly.exterior, *poly.interiors]:
        for i in range(int(ring.length / step)):
            s = (i + 0.5) * step
            a, b = ring.interpolate(s - 0.01), ring.interpolate(s + 0.01)
            d = math.hypot(b.x - a.x, b.y - a.y)
            if d <= 0:
                continue
            q = ring.interpolate(s)
            nx, ny = -(b.y - a.y) / d, (b.x - a.x) / d
            if not poly.contains(Point(q.x + nx * 1e-3, q.y + ny * 1e-3)):
                nx, ny = -nx, -ny
            w = _across(poly, (q.x, q.y), (nx, ny))
            if w:
                ws.append(w)
    return statistics.median(ws) if ws else None


def strip_length(poly) -> tuple[float | None, float | None, str]:
    """(length, width, why) of one zone strip. A strip of constant width w
    has area w*L and an outline 2L + 2w long, bends included, so area / w
    is its centreline length when the two agree; when they do not (a pad,
    a pour, a stepped width, a hole) the length is not one number: (None,
    width, why)."""
    w = strip_width(poly)
    if not w:
        return None, None, "wider than a strip"
    a = poly.area / w
    b = poly.length / 2 - w
    if abs(a - b) > max(STRIP_AGREE_MM, STRIP_AGREE_PCT / 100 * max(a, b)):
        return None, w, (f"not one width (area/width {a:.2f} mm, outline "
                         f"{b:.2f} mm)")
    return a, w, ""


def _pieces(g):
    return [x for x in getattr(g, "geoms", [g]) if x.geom_type == "Polygon"]


def zone_strips(fill, partner, coupling_max) -> tuple[list, list]:
    """([(piece, length, width)], [why]) for the pieces of one net's fill on
    a layer that come within coupling_max of the partner's copper there:
    the pair's own strips, not a pour of the same net elsewhere."""
    out, bad = [], []
    for poly in _pieces(fill):
        if partner is None or partner.distance(poly) > coupling_max:
            continue
        L, w, why = strip_length(poly)
        if L is None:
            bad.append(why)
        out.append((poly, L, w))
    return out, bad


def strip_uncoupled(strips, partner, w_partner, coupling_max) -> float:
    """Length of a strip set farther than coupling_max (centre to centre,
    as for tracks) from the partner: the area of its copper beyond
    coupling_max - w_partner/2 of the partner's edge, over its width."""
    far = partner.buffer(coupling_max - w_partner / 2)
    return sum(poly.difference(far).area / w for poly, _L, w in strips if w)


CROSS_MAX_DEG = 25.0          # a width ray within this of the edge's normal


def _crosses(ring, s, d) -> bool:
    """Whether the ray along d from arc length s of ring crosses the strip
    (within CROSS_MAX_DEG of the edge's normal) rather than running along
    it, as it does where a strip bends away from its partner (#1000)."""
    a, b = ring.interpolate(s - 0.01), ring.interpolate(s + 0.01)
    t = math.hypot(b.x - a.x, b.y - a.y)
    if t <= 0:
        return True
    along = abs((b.x - a.x) * d[0] + (b.y - a.y) * d[1]) / t
    return along < math.sin(math.radians(CROSS_MAX_DEG))


def _ring_at(g, pt):
    """(ring, arc length) of the boundary of g through pt."""
    for poly in getattr(g, "geoms", [g]):
        for ring in [poly.exterior, *poly.interiors]:
            if ring.distance(pt) < 1e-6:
                return ring, ring.project(pt)
    return None, 0.0


def zone_pair_samples(gp, gn, coupling_max, step=SAMPLE_MM) -> list[tuple]:
    """[(point, width_p, width_n, gap)] every step along the edge of P that
    faces N within coupling_max. The ray from N's nearest point across the
    gap gives the edge gap; carried on, the width of each strip at the same
    spot, so the two sides are compared where they couple. A spot where the
    ray runs along either edge (a bend away, a corner) is no width."""
    out = []
    for poly in getattr(gp, "geoms", [gp]):
        for ring in [poly.exterior, *poly.interiors]:
            k = int(ring.length / step)
            for i in range(k):
                q = ring.interpolate(i * step)
                g = gn.distance(q)
                if not 1e-6 < g <= coupling_max:
                    continue
                r = nearest_points(q, gn)[1]
                d = ((q.x - r.x) / g, (q.y - r.y) / g)
                if not _crosses(ring, i * step, d):
                    continue
                rn, sn = _ring_at(gn, r)
                if rn is not None and not _crosses(rn, sn, d):
                    continue
                wp = _across(gp, (q.x, q.y), d)
                wn = _across(gn, (r.x, r.y), (-d[0], -d[1])) if wp else None
                if wn:
                    out.append((q, wp, wn, g))
    return out


def _imbalanced_stretch(samples, tol_pct, step=SAMPLE_MM):
    """The longest run of consecutive samples whose sides differ by more
    than tol_pct of their mean width: (length_mm, worst sample) or None."""
    best, run = None, []
    for s in samples + [None]:
        bad = s is not None and \
            abs(s[1] - s[2]) > tol_pct / 100.0 * (s[1] + s[2]) / 2
        if bad:
            run.append(s)
            continue
        if run and (best is None or len(run) > len(best)):
            best = run
        run = []
    if not best:
        return None
    worst = max(best, key=lambda s: abs(s[1] - s[2]))
    return len(best) * step, worst


def zone_trunk_length(bg, net, terminals):
    """(length, zone_mm, why) of a trunk that runs through the net's own
    zone strips (a mixed pair: tracks into a zone strip and out): the path
    on netconn's graph with zones, each zone node it crosses counted at its
    strip length. None when even that cannot connect the terminals; length
    None, with why, when a zone on the path is not a strip."""
    if len(terminals) < 2 or not bg.zones_of(net):
        return None
    g = netconn.build(bg, net, include_zones=True)
    polys, j = {}, 0
    for z in bg.zones_of(net):          # netconn's ("zone", j) numbering
        for layer, ps in z.fills.items():
            for poly in ps:
                polys[("zone", j)] = (layer, poly)
                j += 1
    best = None
    nodes = [netconn.pad_node(g, t) for t in terminals]
    for i in range(len(nodes)):
        for k in range(i + 1, len(nodes)):
            r = netconn.shortest_path(g, nodes[i], nodes[k])
            if r and (best is None or r[0] > best[0]):
                best = r
    if best is None:
        return None
    crossed = []
    for u, v, _w, _e in best[1]:
        for x in (u, v):
            if x in polys and x not in crossed:
                crossed.append(x)
    extra = 0.0
    for x in crossed:
        layer, poly = polys[x]
        L, _w, why = strip_length(poly)
        if L is None:
            return None, None, f"{net}'s trunk crosses a zone on {layer} {why}"
        extra += L
    return best[0] + extra, extra, ""


def zone_layers(bg, spec, p, n, fp, fn, coupling_max):
    """(violations, {layer: row}, strips) for each layer both nets fill: the
    width and gap sampled along the coupled run, Zdiff as drawn, a side
    narrower than its partner over ZONE_IMBALANCE_MM, and each net's strips
    there (zone_strips) for length and uncoupled length."""
    violations: list[dict] = []
    common = dict(pair=[p, n], drawn_as="zones")
    target = None if is_sense(spec) else spec.get("impedance_ohm")
    tol = float(spec.get("impedance_tol_pct", IMPEDANCE_TOL_PCT))
    imb_pct = float(spec.get("max_width_imbalance_pct", ZONE_IMBALANCE_PCT))
    layers: dict[str, dict] = {}
    strips: dict[str, tuple] = {}
    for lay in sorted(set(fp) & set(fn)):
        smp = zone_pair_samples(fp[lay], fn[lay], coupling_max)
        coupled = len(smp) * SAMPLE_MM
        if coupled < LAYER_MIN_COUPLED_MM:
            continue
        wp = statistics.median(s[1] for s in smp)
        wn = statistics.median(s[2] for s in smp)
        gap = statistics.median(s[3] for s in smp)
        row = {"width_p_mm": checklib.rnd(wp), "width_n_mm": checklib.rnd(wn),
               "min_width_p_mm": checklib.rnd(min(s[1] for s in smp)),
               "min_width_n_mm": checklib.rnd(min(s[2] for s in smp)),
               "gap_mm": checklib.rnd(gap), "coupled_mm": checklib.rnd(coupled)}
        sp, bad_p = zone_strips(fp[lay], fn[lay], coupling_max)
        sn, bad_n = zone_strips(fn[lay], fp[lay], coupling_max)
        strips[lay] = (sp, bad_p, sn, bad_n, wp, wn)
        if not bad_p:
            row["length_p_mm"] = checklib.rnd(sum(L for _, L, _ in sp))
        if not bad_n:
            row["length_n_mm"] = checklib.rnd(sum(L for _, L, _ in sn))
        w = (wp + wn) / 2
        try:
            z, model = layer_zdiff(bg, lay, w, gap)
        except (ValueError, RuntimeError, ZeroDivisionError):
            z, model = None, ""
        if z is not None:
            row.update(zdiff_ohm=checklib.rnd(z), model=model)
        layers[lay] = row
        at = smp[len(smp) // 2][0]
        if target and z is not None:
            miss = (z - float(target)) / float(target) * 100
            if abs(miss) > tol:
                violations.append(violation(
                    SCRIPT, "error" if abs(miss) > 2 * tol else "warning",
                    (at.x, at.y), lay, p, [],
                    f"diff pair {p}/{n} (zones) is {z:.1f} ohm as drawn on "
                    f"{lay} (width {w:.4f} mm, gap {gap:.4f} mm); target "
                    f"{float(target):g} ohm +/-{tol:g}%", SCRIPT,
                    **{**common, "kind": "diffpair_impedance",
                       "target_ohm": float(target),
                       "miss_pct": checklib.rnd(miss), **row}))
        st = _imbalanced_stretch(smp, imb_pct)
        if st and st[0] >= ZONE_IMBALANCE_MM:
            length, (q, a, b, _) = st
            thin = p if a < b else n
            violations.append(violation(
                SCRIPT, "error", (q.x, q.y), lay, thin, [],
                f"diff pair {p}/{n} (zones): {thin} is {min(a, b):.4f} mm "
                f"against {max(a, b):.4f} mm for {length:.1f} mm on {lay}; "
                f"limit {imb_pct:g}% apart", SCRIPT,
                **{**common, "kind": "diffpair_width_imbalance",
                   "width_p_mm": checklib.rnd(a), "width_n_mm": checklib.rnd(b),
                   "stretch_mm": checklib.rnd(length),
                   "limit_pct": imb_pct}))
    return violations, layers, strips


def strips_measure(p, n, strips, coupling_max):
    """(len_p, len_n, unc_p, unc_n, why) summed over zone_layers' strips;
    a length, and so its uncoupled length (area over width), is None when a
    strip on its net is not one width (why says where): a pour named like a
    pair has no length to read (#1000)."""
    lp = ln = 0.0
    up = un = 0.0
    why = []
    for lay, (sp, bad_p, sn, bad_n, wp, wn) in sorted(strips.items()):
        for net, bad in ((p, bad_p), (n, bad_n)):
            why += [f"{net} on {lay} is {b}" for b in bad]
        if lp is not None:
            lp = None if bad_p else lp + sum(L for _, L, _ in sp)
        if ln is not None:
            ln = None if bad_n else ln + sum(L for _, L, _ in sn)
        if up is not None:
            up = None if bad_p or any(not w for _, _, w in sp) \
                else up + strip_uncoupled(
                sp, unary_union([x for x, _, _ in sn]), wn, coupling_max)
        if un is not None:
            un = None if bad_n or any(not w for _, _, w in sn) \
                else un + strip_uncoupled(
                sn, unary_union([x for x, _, _ in sp]), wp, coupling_max)
    return lp, ln, up, un, why


def pair_rules(spec, p, n, lp, ln, unc_p, unc_n, coupling_max, eps, at,
               common) -> list[dict]:
    """The length rules both drawings share: SKEW against max_skew_mm
    (lengths None: not judged) and UNCOUPLED length against
    max_uncoupled_mm (None: not judged), sense pairs as the module doc."""
    out: list[dict] = []
    sense = is_sense(spec)
    max_skew = float(spec.get("max_skew_mm", MAX_SKEW_MM))
    max_unc = float(spec.get("max_uncoupled_mm", MAX_UNCOUPLED_MM))
    if lp is not None and ln is not None and not sense:
        skew = abs(lp - ln)
        skew_ps = skew * math.sqrt(eps) / C_MM_PER_PS
        if skew > max_skew:
            out.append(violation(
                SCRIPT, "error" if skew > 2 * max_skew else "warning", at, None,
                p, [], f"diff pair {p}/{n} length skew {skew:.2f} mm "
                f"(~{skew_ps:.0f} ps, eps_r {eps:.1f}); limit {max_skew:.1f} mm",
                SCRIPT, **{**common, "kind": "diffpair_skew",
                           "skew_mm": checklib.rnd(skew),
                           "skew_ps": checklib.rnd(skew_ps),
                           "limit_mm": max_skew}))
    if sense:
        max_unc = float(spec.get("max_sense_uncoupled_mm", math.inf))
    if unc_p is not None and unc_n is not None and max(unc_p, unc_n) > max_unc:
        who, uval = (p, unc_p) if unc_p >= unc_n else (n, unc_n)
        out.append(violation(
            SCRIPT, "error", at, None, who, [],
            f"diff pair {p}/{n} has {uval:.2f} mm of {who} running uncoupled "
            f"(> {coupling_max:.2f} mm from its partner); limit {max_unc:.1f} mm",
            SCRIPT, **{**common, "kind": "diffpair_uncoupled",
                       "uncoupled_mm": checklib.rnd(uval),
                       "uncoupled_p_mm": checklib.rnd(unc_p),
                       "uncoupled_n_mm": checklib.rnd(unc_n),
                       "coupling_max_mm": checklib.rnd(coupling_max),
                       "limit_mm": max_unc}))
    return out


def via_rule(bg, spec, p, n, at, common) -> tuple[list, int, int]:
    """([diffpair_via_asymmetry], vias_p, vias_n): the two nets should
    change layers together."""
    vp, vn = len(bg.vias_of(p)), len(bg.vias_of(n))
    if abs(vp - vn) <= int(spec.get("max_via_asym", 0)):
        return [], vp, vn
    return [violation(
        SCRIPT, "warning", at, None, p, [],
        f"diff pair {p}/{n} via count asymmetric: {p} has {vp}, {n} has {vn}",
        SCRIPT, **{**common, "kind": "diffpair_via_asymmetry",
                   "vias_p": vp, "vias_n": vn})], vp, vn


def zone_pair(bg, spec, p, n, fp, fn, coupling_max) -> tuple[list, dict]:
    """A pair drawn as two zones (hw-lab zcoupon): per layer as zone_layers;
    over all layers each net's strip length gives the skew, its copper
    beyond coupling_max of the partner the uncoupled length, and the via
    counts their symmetry, judged as for tracks (#1000). The running skew
    is not judged: a strip has no drawn centreline to walk."""
    violations, layers, strips = zone_layers(bg, spec, p, n, fp, fn,
                                             coupling_max)
    if not layers:
        return [], {"pair": [p, n], "routed": False, "drawn_as": "zones",
                    "note": "zone pair not coupled on any layer; "
                            "coupling not evaluated"}
    main = max(layers, key=lambda l: layers[l]["coupled_mm"])
    lp, ln, up, un, why = strips_measure(p, n, strips, coupling_max)
    sp = strips[main][0]
    c = sp[0][0].representative_point() if sp else None
    at = (c.x, c.y) if c is not None else None
    eps = _pair_epsilon(bg, p, n)
    common = dict(pair=[p, n], drawn_as="zones",
                  length_p_mm=checklib.rnd(lp) if lp is not None else None,
                  length_n_mm=checklib.rnd(ln) if ln is not None else None)
    violations += pair_rules(spec, p, n, lp, ln, up, un, coupling_max, eps,
                             at, common)
    vv, vp, vn = via_rule(bg, spec, p, n, at, common)
    violations += vv
    sense = is_sense(spec)
    not_judged = []
    if (lp is None or ln is None) and not sense:
        not_judged.append("skew: " + "; ".join(why))
    if up is None or un is None:
        not_judged.append("uncoupled length: " + ("; ".join(why) or
                                                  "a side is wider than a strip"))
    if not sense:
        not_judged.append(PHASE_ZONES)
    facts = {"pair": [p, n], "kind": "sense" if sense else "diff",
             "drawn_as": "zones", "signal_layer": main, "layers": layers,
             "length_p_mm": common["length_p_mm"],
             "length_n_mm": common["length_n_mm"],
             "vias_p": vp, "vias_n": vn,
             "coupling_max_mm": checklib.rnd(coupling_max)}
    if lp is not None and ln is not None:
        facts["skew_mm"] = checklib.rnd(abs(lp - ln))
        facts["skew_ps"] = checklib.rnd(abs(lp - ln) * math.sqrt(eps)
                                        / C_MM_PER_PS)
    if up is not None and un is not None:
        facts["uncoupled_p_mm"] = checklib.rnd(up)
        facts["uncoupled_n_mm"] = checklib.rnd(un)
    if not_judged:
        facts["not_judged"] = not_judged
    return violations, facts


DEFAULT_ER = 4.5              # FR4 fallback when no reference dielectric exists


def _pair_layers(bg, p, n):
    """Two stackup layers to read epsilon between: the pair's signal layer and
    its nearest reference. Falls back to the outer gap."""
    layers = {t.layer for t in bg.tracks_of(p)} | {t.layer for t in bg.tracks_of(n)}
    sig = next((l for l in bg.copper_layers if l in layers), bg.copper_layers[0])
    above, below = bg.stackup.adjacent(sig)
    ref = below or above or (bg.copper_layers[1] if len(bg.copper_layers) > 1
                             else sig)
    return sig, ref


def _pair_epsilon(bg, p, n) -> float:
    """Effective epsilon_r for the ps conversion; FR4 default on a single-copper
    board (no dielectric gap -> epsilon_between would divide by zero)."""
    if len(bg.copper_layers) < 2 or not bg.stackup.dielectrics:
        return DEFAULT_ER
    try:
        eps = bg.stackup.epsilon_between(*_pair_layers(bg, p, n))
    except (ZeroDivisionError, ValueError, IndexError):
        return DEFAULT_ER
    return eps if eps and eps > 0 else DEFAULT_ER


def _rep_point(bg, net):
    tks = bg.tracks_of(net)
    if not tks:
        return None
    c = tks[0].shape.interpolate(0.5, normalized=True).coords[0]
    return (c[0], c[1])


# ------------------------------------------------------------ CLI

def resolve_pairs(bg, cons) -> list[dict]:
    # an EXPLICIT diff_pairs key wins even when empty ([] means "no pairs");
    # only a missing key falls back to name-based auto-discovery.
    if cons and "diff_pairs" in cons:
        return [s for s in (cons["diff_pairs"] or []) if s.get("p") and s.get("n")]
    return [{"p": p, "n": n, **({"kind": "sense"} if is_sense_name(p, n) else {})}
            for p, n in discover_pairs(bg.nets)]


def run(argv=None):
    ap = argparse.ArgumentParser(
        description="Differential-pair skew / coupling / symmetry check.")
    ap.add_argument("--pcb", required=True, help="path to .kicad_pcb")
    ap.add_argument("--constraints", help="constraints.json (diff_pairs list); "
                    "pairs are auto-discovered by name if omitted")
    ap.add_argument("--out", help="write JSON report here instead of stdout")
    args = ap.parse_args(argv)

    cons = checklib.load_json(args.constraints, "constraints") \
        if args.constraints else {}
    bg = geom.load_board(Path(args.pcb))
    bg.assert_fresh()

    violations: list[dict] = []
    checked: list[dict] = []
    untargeted: list[str] = []
    pair_nj: list[str] = []
    for spec in resolve_pairs(bg, cons):
        p, n = spec.get("p"), spec.get("n")
        # A stale named pair (renamed/absent net) is a warning, not a whole-check
        # abort: other pairs still get judged.
        if p not in bg.nets or n not in bg.nets:
            missing = [x for x in (p, n) if x not in bg.nets]
            violations.append(violation(
                SCRIPT, "warning", None, None, p, [],
                f"diff pair {p}/{n} names net(s) not on the board: {missing} "
                f"(stale constraints?)", SCRIPT, kind="diffpair_missing_net",
                pair=[p, n], missing=missing))
            continue
        vs, facts = check_pair(bg, spec)
        violations.extend(vs)
        checked.append(facts)
        pair_nj += [f"{p}/{n}: {x}" for x in facts.get("not_judged", [])]
        if facts.get("routed") is not False and not is_sense(spec) \
                and not spec.get("impedance_ohm"):
            untargeted.append(f"{p}/{n}: no impedance target, and held to the "
                              f"generic {MAX_SKEW_MM:g} mm skew and uncoupled "
                              "limits unless declared")

    unrouted = ["/".join(c["pair"]) for c in checked if c.get("routed") is False]
    assumed = bool(bg.stackup.assumed) and bool(checked)
    payload = checklib.report(
        SCRIPT, args.pcb, violations, checked=checked,
        stackup_assumed=bool(bg.stackup.assumed),
        not_judged=[f"{u}: not fully routed" for u in unrouted] + untargeted
        + pair_nj
        + (["stackup: no (stackup) block, so any Zdiff is on the default "
            "FR4"] if assumed else []),
        nothing_to_judge=None if len(checked) > len(unrouted) else
        "no routed differential pair")
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
