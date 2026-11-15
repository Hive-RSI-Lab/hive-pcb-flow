"""cluster_violations.py - group open violations for fixer dispatch (SPEC 4).

The orchestrator's fix loop clusters violations "by net/region/type" and sends
one fixer agent per cluster, in parallel where regions don't overlap. This tool
produces those clusters from a verify_all summary (or any report carrying a
`violations` list): violations are grouped by (net, kind) and then split into
spatial regions (positions within `--radius` mm are one region). Each cluster
carries a bounding region and a suggested fixer domain so the orchestrator can
route it.

CLI: --input summary.json  (or --pcb-relative report) [--radius 5]
     [--out clusters.json]
Exit: 0 no clusters, 1 clusters present (work to do), 2 error.

Cluster schema:
    {"id", "net", "kinds"[], "checks"[], "severity", "count",
     "region": {"bbox": [xmin,ymin,xmax,ymax]|null, "center": [x,y]|null},
     "fixer": "<domain>", "violations": [ ...the raw violations... ]}
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import checklib  # noqa: E402

SCRIPT = "cluster_violations"
DEFAULT_RADIUS_MM = 5.0
SEV_RANK = {"error": 2, "warning": 1, "info": 0}

# The fixer domains fix_dispatch.py defines (kept in sync by
# tests/test_fix_dispatch.py; not imported - fix_dispatch imports this
# module). A violation carrying an explicit "domain" from this set (the
# verify-reviewer transcribes one per finding since T6) routes there
# directly - reviewer kinds are free slugs FIXER_HINTS cannot enumerate,
# which is why all 19 production review orders dead-ended in 'review'.
FIXER_DOMAINS = frozenset({
    "router", "placement", "plane", "silk", "schematic", "library",
    "fab", "parts", "review"})

# kind -> the fixer domain best suited to resolve it (fix_dispatch.py routes
# the actual agent dispatch off these; domains map to allowed-script sets and
# guidance there). Violations without a `kind` fall back to their `check`
# (kicad-cli DRC/ERC type names), so raw gate reports dispatch too.
FIXER_HINTS = {
    # S4/S5 verification checks
    "corridor_void": "plane", "no_reference_plane": "plane",
    "missing_return_via": "router", "missing_stitch_cap": "router",
    "insufficient_transition_vias": "router",
    "undersized_track": "router", "pour_neckdown": "router",
    "decoupler_distance": "placement", "decoupler_loop": "placement",
    "gnd_stub_long": "placement", "metadata_mismatch": "schematic",
    "diffpair_skew": "router", "diffpair_uncoupled": "router",
    "diffpair_via_asymmetry": "router", "diffpair_missing_net": "schematic",
    "diffpair_open_trunk": "router", "diffpair_width_imbalance": "router",
    "creepage": "placement", "plane_missing": "plane",
    # a voltage_pairs entry below its own nets' declared voltage: the fix is
    # the constraints entry (raise it or give a why), not the copper
    "creepage_pair_understated": "review",
    "thermal_area": "plane", "thermal_vias": "router",
    "silk_over_pad": "silk", "silk_illegible": "silk", "silk_thin": "silk",
    "silk_misattributed": "silk", "route_style": "router",
    "pdn_undecoupled": "schematic", "pdn_no_bulk": "schematic",
    "reg_input_no_hf": "schematic",
    # check_ratings: a pin past its datasheet rating is a schematic fix (a
    # divider, a clamp, a level shifter) or a different part; unrated is a
    # missing extraction row
    "rating_over_voltage": "schematic", "rating_reverse_polarity": "schematic",
    "rating_diode_forward": "schematic",
    "rating_outside_recommended": "schematic",
    "rating_over_current": "parts", "rating_unrated": "parts",
    "rating_no_attributes": "parts", "rating_regulator_output": "parts",
    "rating_regulator_output_unjudged": "parts",
    "rating_over_temperature": "parts",
    "rating_datasheet_suspect": "parts", "rating_datasheet_unassessed": "parts",
    "rating_no_datasheet_read": "parts",
    "rating_listing_disagrees": "parts",
    # sim gate (sim_run.py) - a failed bound is a schematic-value defect;
    # engine/measure trouble needs triage, not a copper fixer
    "sim_bound_fail": "schematic", "sim_measure_missing": "review",
    "sim_engine_error": "review",
    # S9 placement legality
    "courtyard_overlap": "placement", "outside_outline": "placement",
    "edge_violation": "placement", "keepout_violation": "placement",
    "courtyard_missing": "placement", "seed_unplaced": "placement",
    "mating_faces_inward": "placement", "mating_mouth_inset": "placement",
    "mating_zone_blocked": "placement",
    "mating_direction_unknown": "placement",
    "mating_mouth_disagrees": "library",
    "mating_mate_misaligned": "placement",
    # a placement.mates entry in constraints.json that cannot be used
    "mating_mate_invalid": "review",
    # the copper faces the edge but the 3D model is turned round: fix the
    # model's rotate in the footprint, not the placement
    "mating_model_reversed": "library",
    # S11 routing pipeline
    "critical_route_failed": "router", "critical_missing_net": "schematic",
    "power_width_blocked": "router", "escape_starved_thermal": "plane",
    "zone_unfilled": "plane", "stitch_impossible": "router",
    "plane_split": "plane", "plane_split_unrepairable": "plane",
    "cleanup_regression": "router",
    # S12 DFM (gerber-level)
    "dfm_trace_width": "router", "dfm_clearance": "router",
    "dfm_pour_width": "plane",
    "dfm_copper_to_edge": "router", "dfm_hole_size": "router",
    "dfm_hole_to_hole": "router", "dfm_hole_to_edge": "router",
    "dfm_annular_ring": "router",
    "dfm_silk_width": "silk", "dfm_silk_over_pad": "silk",
    "dfm_mask_dam": "silk",
    "dfm_missing_layer": "fab", "dfm_no_drill": "fab",
    "dfm_bom_incomplete": "parts",
    "cpl_polarity": "placement", "pad_net_mismatch": "schematic",
    # S6 footprint verification
    "pad_count": "library", "pin1_missing": "library", "pad_pitch": "library",
    "pad_size": "library", "no_courtyard": "library",
    "stroked_pad_primitive": "library",
    # S7 netlist audit
    "missing_net": "schematic", "diffpair_naming": "schematic",
    "diffpair_unpaired": "schematic", "power_no_consumers": "schematic",
    "power_undeclared": "schematic", "dangling_net": "schematic",
    "netlist_diff": "schematic",
    # kicad-cli DRC type names (check field; via the kind->check fallback)
    "track_width": "router", "clearance": "router",
    "unconnected_items": "router", "shorting_items": "router",
    "hole_clearance": "router", "hole_near_hole": "router",
    "via_dangling": "router", "track_dangling": "router",
    "track_angle": "router", "via_diameter": "router",
    "solder_mask_bridge": "router", "copper_edge_clearance": "router",
    # disallow-rule hits (T6 P5-4 / #1108: aiee_diff_outer_only_*, aiee_z*_outer_only*)
    "items_not_allowed": "router",
    "courtyards_overlap": "placement", "footprint": "placement",
    "silk_over_copper": "silk", "silk_overlap": "silk",
    "silk_edge_clearance": "silk", "text_height": "silk",
    "text_thickness": "silk",
    "zones_intersect": "plane", "starved_thermal": "plane",
    "isolated_copper": "plane",
    "invalid_outline": "fab", "duplicate_footprints": "schematic",
    "missing_footprint": "schematic", "extra_footprint": "schematic",
    "net_conflict": "schematic", "lib_footprint_issues": "library",
    "lib_footprint_mismatch": "library",
}


def _uf_find(parent, i):
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def spatial_split(items: list[dict], radius: float) -> list[list[dict]]:
    """Union-find the violations by position proximity; None-pos items each
    form their own group (they are net-level, not point defects)."""
    pts = [v.get("pos") for v in items]
    idx = [i for i, p in enumerate(pts) if p]
    parent = list(range(len(items)))
    for a in range(len(idx)):
        for b in range(a + 1, len(idx)):
            i, j = idx[a], idx[b]
            dx = pts[i][0] - pts[j][0]
            dy = pts[i][1] - pts[j][1]
            if (dx * dx + dy * dy) ** 0.5 <= radius:
                parent[_uf_find(parent, i)] = _uf_find(parent, j)
    groups: dict[int, list] = {}
    for i, v in enumerate(items):
        key = _uf_find(parent, i) if pts[i] else -(i + 1)
        groups.setdefault(key, []).append(v)
    return list(groups.values())


def region_of(vs: list[dict]) -> dict:
    pts = [v["pos"] for v in vs if v.get("pos")]
    if not pts:
        return {"bbox": None, "center": None}
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    bbox = [checklib.rnd(min(xs)), checklib.rnd(min(ys)),
            checklib.rnd(max(xs)), checklib.rnd(max(ys))]
    center = [checklib.rnd(sum(xs) / len(xs)), checklib.rnd(sum(ys) / len(ys))]
    return {"bbox": bbox, "center": center}


def kind_of(v: dict) -> str | None:
    """A violation's dispatch kind: explicit `kind` from the custom checks,
    else the kicad-cli `check` type (DRC/ERC reports carry no kind)."""
    return v.get("kind") or v.get("check")


def cluster(violations: list[dict], radius: float) -> list[dict]:
    by_key: dict[tuple, list] = {}
    for v in violations:
        by_key.setdefault((v.get("net"), kind_of(v)), []).append(v)
    clusters: list[dict] = []
    for (net, kind), items in by_key.items():
        for group in spatial_split(items, radius):
            sev = max((g.get("severity", "info") for g in group),
                      key=lambda s: SEV_RANK.get(s, 0))
            kinds = sorted({k for g in group if (k := kind_of(g))})
            checks = sorted({c for g in group
                             if (c := g.get("source") or g.get("check"))})
            # explicit per-violation domain wins when the group agrees on
            # exactly one valid name; else the kind-keyed hint table
            doms = {d for g in group
                    if (d := g.get("domain")) in FIXER_DOMAINS}
            fixer = doms.pop() if len(doms) == 1 \
                else FIXER_HINTS.get(kind, "review")
            clusters.append({
                "net": net, "kinds": kinds, "checks": checks, "severity": sev,
                "count": len(group), "region": region_of(group),
                "fixer": fixer,
                "violations": group,
            })
    # most severe, largest first; stable id
    clusters.sort(key=lambda c: (-SEV_RANK.get(c["severity"], 0), -c["count"]))
    for i, c in enumerate(clusters):
        c["id"] = i
    return clusters


def load_violations(path) -> tuple[list[dict], str | None]:
    data = checklib.load_json(path, "input report")
    if isinstance(data, list):
        return data, None
    return data.get("violations", []), data.get("board")


def run(argv=None):
    ap = argparse.ArgumentParser(
        description="Cluster open violations by (region, net, type).")
    ap.add_argument("--input", required=True,
                    help="verify_all summary.json or any report with violations")
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS_MM,
                    help="mm; positions within this join one region (default 5)")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args(argv)

    violations, board = load_violations(args.input)
    clusters = cluster(violations, args.radius)
    by_sev: dict[str, int] = {}
    for c in clusters:
        by_sev[c["severity"]] = by_sev.get(c["severity"], 0) + 1
    payload = {
        "script": SCRIPT, "board": board,
        "status": "violations" if clusters else "pass",
        "counts": {"clusters": len(clusters), "violations": len(violations),
                   "by_severity": by_sev},
        "clusters": clusters,
    }
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
