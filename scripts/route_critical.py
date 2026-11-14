"""route_critical - script-driven routing of CRITICAL nets before Freerouting
(S11, SPEC P7.1).

Wraps the vendored KiCadRoutingTools (KRT, tools/krt via env.find_krt) to route
the nets whose geometry is load-bearing BEFORE route_auto hands the remainder
to Freerouting: differential pairs (route_diff.py), RF single-ended nets at
impedance width, and high-current power nets at IPC-2152 width (route.py).
Pre-routed copper survives Freerouting by construction: the DSN exporter emits
it as guide wires which Freerouting protects (see route_auto.py).

Critical-net sources (constraints.json, sidecar default like place_metrics):
  diff pairs  constraints["diff_pairs"] [{p, n, gap_mm?, impedance_ohm?,
              max_skew_mm?, max_uncoupled_mm?}] PLUS auto-discovery over the
              high_speed net names via check_diffpair.discover_pairs - the S5
              checker's own suffix rules (_P/_N, DP/DM, D+/D-), so the pipeline
              and the checker can never pair differently. A pair of kind
              sense / kelvin / dc_sense / low_frequency (or a discovered
              pair with a shunt-sense name) is NOT routed here: it needs
              equal legs, not a coupled run, so it goes to facts.skipped.
  rf          constraints["rf"] [{net, impedance_ohm}] plus high_speed entries
              carrying impedance_ohm that are not half of a diff pair; routed
              single-ended at solve_width() impedance width. Via FENCING is
              NOT done here: each routed RF fact carries a fence_handoff note
              (stitch_vias.py --fence-net <net> owns the fence).
  power       constraints["power"] [{net, current_a, dt_c?}]; width = IPC-2152
              minimum (check_current.required_width_mm, rules_gen's helper)
              * 1.5 margin, clamped >= 0.3 mm. An entry's sense_pads
              {sense pad: Kelvin tap pad} (#936) is honoured (#1043): KRT
              routes the rail with each sense pad detached (netless, so it
              never tees the rail into it), then route_sense_lines lays each
              sense line from its sense pad to its tap pad on copper of its
              own (mazeroute, every other piece of rail copper a wall) and
              keeps the pour off it with an aiee_sense_* rule area. The
              line is a signal, not the rail (#1042): it is laid at the
              board's Default netclass width (sense_width), never the
              rail's IPC width, and an aiee_sensew_* rule area that hugs
              it (nothing disallowed) is the mark rules_gen's
              aiee_sense_width_<net> rule lets down to the fab minimum.
              After the final refill sense_verdicts walks each line
              (check_kelvin.sense_walk): a line that joins rail copper
              before its tap is an error, sense_route_off_pad.

KRT facts this adapter encodes (all machine-verified against KRT 0.19.0):
  * KRT scripts are invoked as subprocesses of THIS python with
    cwd=<plugins dir> (they sys.path-insert relative dirs) and args as a list
    (never a shell - Git Bash mangles "/USB_DP" into a path). The final
    "JSON_SUMMARY: {...}" stdout line (the LAST one - a reconcile pass may
    print a scoped second summary) is the machine-readable outcome; KRT's own
    exit code / internal DRC are advisory only.
  * --no-fix-drc-settings is ALWAYS passed (without it KRT rewrites the
    .kicad_pro DRC floors - prior-attempt fact), so routed copper must grade
    clean against the board's EXISTING settings. The adapter therefore passes
    explicit floors read from the .kicad_pro (KiCad stock defaults when
    absent: clearance 0.2, via 0.5/0.3, copper-to-edge 0.5) as --clearance /
    --via-size / --via-drill / --board-edge-clearance, plus a --fab-overrides
    file pinning the same values so KRT's fab-tier escalation can never route
    below what kicad-cli will grade (S11: its 0.3/0.15 escalation via drew
    annular/hole/diameter DRC errors on a stock board).
  * Every KRT call routes staged -> FRESH output file; on success the output
    replaces the staged board (which keeps its .kicad_pro sidecar - KRT reads
    netclasses from the INPUT's sibling, and boards must never travel without
    their project file).
  * DP/DM suffixes ARE in KRT's pair detector (net_queries.extract_diff_pair_base
    rule r'^(.*?)D([PMN])$'), verified live on /USB_DP + /USB_DM.
  * KRT 0.19.0 limitation + workaround: a 2-terminal pair where one net has an
    extra stub pad (USB pull-up R, series tap) MISPAIRS its route terminals
    (connectivity.get_net_endpoints Case 3 makes the stub pad a "source" and
    the ref-midpoint target selector then prefers a degenerate cross pairing)
    and the pair dies with "no-escape-path" at both ends. The adapter detaches
    such stub pads - pads outside every matched terminal per
    check_diffpair.matched_terminals - from their net in the KRT INPUT text,
    routes, then restores the net node in the OUTPUT (KRT's writer preserves
    footprint text byte-identical; the restore is verified before the board is
    accepted). The stub itself is ordinary Freerouting work later. A two-pad
    resistor across P and N (a differential termination: DDR CK's 100 ohm at
    the DRAM) is detached the same way while two other terminals remain, so
    KRT routes a 2-terminal pair from the escape tips instead of a chain of
    legs from the pads (#529). So is a diode with one pad on each half (an
    ESD clamp tapping the pair, esd_tap_pads), and a connector that lands
    the pair twice (USB-C A and B rows) keeps only the row nearest the far
    end (duplicate_row_pads): otherwise the clamp and the 1.7 mm row-to-row
    hop are legs of their own and the pair is 'electrically-short'.
  * Pairs out of escaped BGAs (#529): route_diff runs through
    lib/krt_launch.py --via-joint-ends. KRT's stub free-end finder reports a
    dog-bone via joint as a free end, so a pair started at the via inside the
    ball field (no-escape-path) instead of at route_escape's stub tip. A
    pair ball no copper touches (route_escape left it) is named in the
    failure (unescaped_balls).
  * Diff-pair width ladder: the impedance-derived width is tried first; if the
    pair defers/fails (fine-pitch escape), one fallback rung at 0.2 mm width
    retries. Every routed rung is judged by check_diffpair first (#529): an
    uncoupled-run or impedance ERROR rejects it (the next rung is tried; with
    none left the pair is critical_pair_rejected and no copper is kept). A
    skew error is kept and reported: route_tune trims skew. --grid-step 0.05
    (default here) matters: 0.1 quantizes away the
    tap corridors of 0.5 mm-pitch parts (S11-verified: +3V3 15/16 pads at 0.1,
    21/21 at 0.05).
  * KRT 0.19.0 limitation + workaround: a power net routed wider than the
    base width keeps its extra half-width as a FRACTIONAL grid margin, and
    against an off-grid 45-degree obstacle (a diff-pair track) that margin
    comes up to half a diagonal grid phase short (usbbuck4: VBUS ran 0.1854
    mm from USB_DM against 0.2, 3-6 DRC errors depending on the board's
    incidental UUID order). The power pass therefore routes with the
    clearance of every net already carrying tracks raised by grid_step / 2
    in its own --net-clearances file (power_net_clearances); DRC still
    grades against the board's real floors.

Layer discipline: diff pairs and RF prefer the outer layer over the first
inner reference plane (F.Cu on the 4-layer stack, cost 1) with the far outer
layer allowed at cost 3 (power: cost 2); inner layers are excluded and KRT
marks them as forbidden obstacles for via spans. Actual layers used are
re-read from the output (geom) and reported as facts.

Verification before the board is touched: kicad-cli DRC (kc.run_drc,
--all-track-errors) runs on the staged board BEFORE and AFTER. When an error
type grew, the routed nets whose new tracks/vias are items of those errors
have their new copper removed (nets_in_new_errors / rip_new_copper; each a
critical_route_rolled_back violation) and DRC runs once more; an error type
still grown -> exit 2, original board untouched (unconnected_items
legitimately DROPS). The S5 diff-pair check
(check_diffpair.check_pair) then runs in-process on the routed pairs; its
violations gate status like the router's own.

Determinism: KRT's routing is deterministic for identical inputs, but it
stamps fresh uuid4s on every track/via, so byte-identical reruns are
impossible. Determinism here (and in tests) = same nets routed + same DRC
outcome, never same bytes.

Contract (SPEC section 6):
  route_critical.py --pcb B.kicad_pcb [--constraints c.json]
      [--only diff|power|rf|bus] [--plan plan.json] [--grid-step MM]
      [--timeout-s S] [--work-dir DIR] [--out-report r.json]
      [--pad-window [--nets A,B]]
  --only bus (#546; not in the default order): constraints length_groups'
  single-ended nets in one KRT --bus run. Run it after --only diff, so the
  pairs land first.
  A stripline pair's BGA terminals are re-laid first when KRT can't launch
  from them (lib/pairlaunch.py); a pair that doesn't route gets its stubs back.
  exit 0 = every critical item routed + DRC gained nothing + diff-pair check
  passes; 1 = some item failed or a check flags (board still updated with
  what succeeded - only reachable when DRC gained no new errors); 2 = error
  (original board untouched).
  --pad-window (T6, ladder row 78): pure-geometry probe, no KRT and no
  writes - reports the widest connectable track per pad of each power net
  ({ref, pad, net, widest_mm, rule_min_mm, ok}) and exits 1 when a DRU
  per-net width floor is geometrically unmeetable at a pad (the pd-trigger
  USB-C VBUS case: 1.75 mm rule vs ~1.49 mm ceiling).
"""
from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import math
import re
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

from shapely import STRtree  # noqa: E402
from shapely.geometry import LineString, Point, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

import check_current  # noqa: E402  (required_width_mm - rules_gen's helper)
import bgalib  # noqa: E402
import check_diffpair  # noqa: E402  (discover_pairs / matched_terminals / check_pair)
import checklib  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import impedance as imp  # noqa: E402
import kc  # noqa: E402
import check_kelvin  # noqa: E402  (#1043 sense_walk / _pad_named)
import mazeroute  # noqa: E402  (#1043 sense lines)
import pairlaunch  # noqa: E402  (#546 pair stubs a coupled router can launch from)
import route_edit  # noqa: E402  (width normalization ops)
import zrules  # noqa: E402  (#489 rules_gen's per-layer impedance widths)
import routelib  # noqa: E402  (fresh_work_dir / swap_in)
from checklib import CheckError, violation  # noqa: E402

SCRIPT = "route_critical"
DEFAULT_ORDER = ("diff", "rf", "power")
ALL_KINDS = (*DEFAULT_ORDER, "bus")   # bus runs only when asked for (#546)
BUS_TIMEOUT_S = 3000        # KRT --bus on ~50 nets: 9+ min on a loaded tower
POWER_MARGIN = 1.5          # IPC-2152 minimum * this
POWER_WIDTH_FLOOR = 0.3     # mm - never route a declared power net thinner
FALLBACK_PAIR_WIDTH = 0.2   # mm - diff ladder rung 2 (fine-pitch escape)
BASE_TRACK_WIDTH = 0.2      # mm - KRT base width (power-tap neckdown target)
# KRT iteration ladder (T6, LEARNINGS 1433/1504): the 200k default A*
# iteration cap - not congestion - is what fails a 60-70 mm haul; 4M routed
# both carrier hauls first try. Retry once, failed nets only, when KRT says
# "No route found after N iterations" and the Coverage diagnostic blames a
# mostly-STATIC frontier (share >= 0.7 - rip sets cannot help by
# construction; unknown share retries too, the no-route line alone marks
# iteration exhaustion).
RETRY_MAX_ITERATIONS = 4_000_000
RETRY_MAX_PROBE_ITERATIONS = 60_000
STATIC_RETRY_SHARE = 0.7
NO_ROUTE_RE = re.compile(r"No route found after \d+ iterations")
COVERAGE_RE = re.compile(
    r"Coverage: (\d+)/(\d+) frontier cells attributed to routed nets; "
    r"(\d+) static/unrippable")
PAD_WINDOW_CAP = 8.0        # mm - widest track the --pad-window probe reports
KRT_REMEDIATION = (
    "KiCadRoutingTools not found (env.find_krt). Re-fetch: download "
    "https://github.com/drandyhaas/KiCadRoutingTools/releases/download/"
    "v0.19.0/KiCadRoutingTools-0.19.0.zip, unzip under tools/krt/, and copy "
    "grid_router-windows-x86_64.pyd to plugins/rust_router/grid_router.pyd "
    "(see tools/krt/PROVENANCE.txt). A shared tools folder (HWDE_TOOLS_DIR) "
    "may hold it under kicad-routing-tools/ instead, with the Linux core "
    "grid_router-linux-x86_64.so as grid_router.so. HWDE_KRT_DIR overrides.")

# KiCad stock board-setup floors, used when the .kicad_pro carries no value.
KICAD_DEFAULTS = {
    "clearance": 0.2,        # Default netclass clearance
    "track_width": 0.2,      # rules.min_track_width
    "via_diameter": 0.5,     # board setup min via diameter
    "via_drill": 0.3,        # board setup min through hole
    "edge_clearance": 0.5,   # board setup copper-to-edge
}


# ------------------------------------------------------------ pure helpers

def parse_summary(stdout: str) -> dict | None:
    """The LAST 'JSON_SUMMARY: {...}' line of a KRT run, parsed."""
    last = None
    for line in stdout.splitlines():
        if line.startswith("JSON_SUMMARY:"):
            last = line[len("JSON_SUMMARY:"):].strip()
    if last is None:
        return None
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return None


def coverage_static_share(stdout: str) -> float | None:
    """Static/unrippable share of the blocking frontier from KRT's LAST
    'Coverage: A/T frontier cells ...; S static/unrippable' diagnostic
    (LEARNINGS 1504: >0.7 means raise iterations, never widen rip sets).
    None when the line never appeared."""
    last = None
    for m in COVERAGE_RE.finditer(stdout or ""):
        last = m
    if last is None:
        return None
    total = int(last.group(2))
    if total <= 0:
        return None
    return int(last.group(3)) / total


def power_width_mm(current_a: float, dt_c: float = 10.0,
                   cu_mm: float = 0.035) -> float:
    """Routing width for a power net: IPC-2152 minimum * margin, floored."""
    need = check_current.required_width_mm(float(current_a), dt_c, cu_mm)
    return round(max(need * POWER_MARGIN, POWER_WIDTH_FLOOR), 4)


def default_impedance(p: str, n: str) -> int:
    """rules_gen's default: USB pairs 90 ohm differential, else 100."""
    return 90 if "USB" in (p + n).upper() else 100


def diff_specs(constraints: dict, board_nets, *,
               sense: list | None = None) -> list[dict]:
    """Explicit constraints['diff_pairs'] entries PLUS S5-discovered pairs
    over the high_speed net names. Returns [{p, n, impedance_ohm, ...}].
    A sense / low-frequency pair (check_diffpair.is_sense, or on discovery
    is_sense_name) is left out: it wants equal legs, not a coupled run at
    an impedance, and coupling it to a gap breaks the symmetry it needs.
    Its [p, n] goes into `sense` when a list is given."""
    hs = constraints.get("high_speed", []) or []
    imp_of = {e.get("net"): e.get("impedance_ohm") for e in hs}
    specs: list[dict] = []
    used: set[str] = set()
    for entry in constraints.get("diff_pairs", []) or []:
        p, n = entry.get("p"), entry.get("n")
        if not p or not n:
            continue
        if check_diffpair.is_sense(entry):
            used.update((p, n))
            if sense is not None:
                sense.append([p, n])
            continue
        spec = dict(entry)
        spec.setdefault("impedance_ohm",
                        imp_of.get(p) or imp_of.get(n) or default_impedance(p, n))
        specs.append(spec)
        used.update((p, n))
    hs_nets = [e.get("net") for e in hs if e.get("net")]
    for p, n in check_diffpair.discover_pairs(hs_nets):
        if p in used or n in used:
            continue
        if check_diffpair.is_sense_name(p, n):
            used.update((p, n))
            if sense is not None:
                sense.append([p, n])
            continue
        z = imp_of.get(p) or imp_of.get(n) or default_impedance(p, n)
        specs.append({"p": p, "n": n, "impedance_ohm": z})
        used.update((p, n))
    del board_nets  # membership is judged (with a warning) at route time
    return specs


def rf_specs(constraints: dict, pair_nets: set[str]) -> list[dict]:
    """constraints['rf'] plus high_speed entries with impedance_ohm that are
    not half of a diff pair."""
    specs: list[dict] = []
    seen: set[str] = set()
    for entry in constraints.get("rf", []) or []:
        net = entry.get("net")
        if net and net not in pair_nets and net not in seen:
            specs.append({"net": net,
                          "impedance_ohm": entry.get("impedance_ohm", 50)})
            seen.add(net)
    for entry in constraints.get("high_speed", []) or []:
        net, z = entry.get("net"), entry.get("impedance_ohm")
        if net and z and net not in pair_nets and net not in seen:
            specs.append({"net": net, "impedance_ohm": z})
            seen.add(net)
    return specs


def power_specs(constraints: dict, cu_mm: float = 0.035) -> list[dict]:
    specs = []
    for entry in constraints.get("power", []) or []:
        net = entry.get("net")
        if not net or entry.get("current_a") is None:
            continue
        cur = float(entry["current_a"])
        dt = float(entry.get("dt_c", 10.0))
        ipc = round(check_current.required_width_mm(cur, dt, cu_mm), 4)
        w = power_width_mm(cur, dt, cu_mm)
        if entry.get("min_width_mm") is not None:   # rules_gen's floor (#923)
            w = round(max(w, float(entry["min_width_mm"])), 4)
        specs.append({"net": net, "current_a": cur, "dt_c": dt,
                      "ipc_min_mm": ipc, "width_mm": w,
                      "switch_node": bool(entry.get("switch_node")),
                      "sense_pads": dict(entry.get("sense_pads") or {})})
    # a switch node goes first, while the pins to its inductor are open
    # (#924): its one short run on one layer has no detour to fall back on
    return sorted(specs, key=lambda s: not s["switch_node"])


def plan_order(only: str | None, plan: dict | None) -> list[str]:
    """Item-kind order: --plan {'order': [...]} overrides, --only filters.
    'bus' is not in the default order: --only bus or a plan names it."""
    order = list(DEFAULT_ORDER)
    if plan:
        want = plan.get("order")
        if not (isinstance(want, list) and want
                and all(k in ALL_KINDS for k in want)):
            raise CheckError(f"plan 'order' must be a non-empty subset of "
                             f"{list(ALL_KINDS)}, got {want!r}")
        order = list(dict.fromkeys(want))
    if only == "bus" and not plan:
        return ["bus"]
    if only:
        order = [k for k in order if k == only]
    return order


def bus_specs(constraints: dict, board_nets, pair_nets: set[str]) -> list[dict]:
    """The length_groups (#324) as buses: {name, nets} per group of single-
    ended members on this board. A pair's nets stay with the diff step, and a
    net in two groups (DDR's *_MATCH groups) routes once, under the first."""
    seen: set[str] = set()
    out = []
    for g in constraints.get("length_groups") or []:
        nets = []
        for pat in g.get("nets") or []:
            nets += [n for n in sorted(board_nets)
                     if fnmatch.fnmatchcase(n, pat)
                     or fnmatch.fnmatchcase(n.lstrip("/"), pat.lstrip("/"))]
        nets = [n for n in dict.fromkeys(nets) if n not in pair_nets and n not in seen]
        seen.update(nets)
        if nets:
            out.append({"name": g.get("name"), "nets": nets})
    return out


def grading_floors(pro_path: Path | None) -> dict:
    """The floors kicad-cli DRC will grade at, from the .kicad_pro (KiCad
    stock defaults when a value is absent). clearance = the LOOSEST netclass
    (a --clearance ceiling at that value caps nothing that matters, but pins
    the no-netclass fallback base to the graded default)."""
    floors = dict(KICAD_DEFAULTS)
    if pro_path is None or not Path(pro_path).is_file():
        return floors
    try:
        proj = json.loads(Path(pro_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return floors
    rules = proj.get("board", {}).get("design_settings", {}).get("rules", {})
    if rules.get("min_track_width"):
        floors["track_width"] = float(rules["min_track_width"])
    # KiCad writes "min_copper_edge_clearance" (fabfloors.PRO_RULE_KEYS);
    # the legacy "min_copper_to_edge" spelling is kept for compatibility.
    edge = rules.get("min_copper_edge_clearance") \
        or rules.get("min_copper_to_edge")
    if edge:
        floors["edge_clearance"] = float(edge)
    if rules.get("min_via_diameter"):
        floors["via_diameter"] = float(rules["min_via_diameter"])
    if rules.get("min_through_hole_diameter"):
        floors["via_drill"] = float(rules["min_through_hole_diameter"])
    if rules.get("min_hole_to_hole"):
        floors["hole_to_hole"] = float(rules["min_hole_to_hole"])
    classes = proj.get("net_settings", {}).get("classes", []) or []
    clearances = [c.get("clearance") for c in classes
                  if isinstance(c.get("clearance"), (int, float))]
    if clearances:
        floors["clearance"] = float(max(clearances))
    return floors


def pair_via_size(floors: dict) -> float:
    """Via diameter for a coupled pair's layer change. KRT 0.19.0 sets the
    P and N vias via_size + clearance apart (centre to centre) and never
    checks hole_to_hole between them (only between same-net vias, its #300):
    on JLC's 6-layer floor (0.45/0.2 via, 0.125 clearance, 0.5 hole-to-hole)
    that is 0.575 against the 0.7 the fab needs, two DRC errors per layer
    change (#489 live). So the pair's vias grow just enough that KRT's
    spacing clears hole_to_hole; a board whose floors already allow it keeps
    the floor via."""
    need = floors["via_drill"] + floors.get("hole_to_hole", 0.0) - floors["clearance"]
    return max(floors["via_diameter"], math.ceil(need * 200 - 1e-6) / 200)


# ------------------------------------------------------------ DRU parsing

_DRU_RULE_RE = re.compile(r'\(rule\s+"([^"]+)"(.*?)\n\)', re.S)
_DRU_CONSTRAINT_RE = re.compile(
    r'\(constraint\s+(\w+)\s+\(min\s+([\d.]+)\s*mm\)\)')
_DRU_NETNAME_RE = re.compile(r"\.NetName\s*==\s*'([^']+)'")
_DRU_CONDITION_RE = re.compile(r'\(condition\s+"((?:[^"\\]|\\.)*)"\s*\)')
_DRU_LAYER_RE = re.compile(r'\(layer\s+"?([^\s")]+)"?\s*\)')


def parse_dru_rules(text: str) -> list[dict]:
    """Named (rule ...) blocks with a (min Xmm) constraint ->
    [{name, constraint, min_mm, nets, condition, layer}]. nets = NetName
    literals in the condition ([] for unconditioned/baseline rules);
    condition = the raw condition string (None when absent); layer = the
    (layer ...) clause token (None when absent). Only the shapes rules_gen
    emits are recognized; anything else simply yields no row. A barrier rule
    (aiee_iso_*) yields none on purpose: it holds between two domains, and every
    reader here takes a NetName as net-wide, which would hold a net 8 mm from
    its own domain. routelib.dru_barriers reads those."""
    out: list[dict] = []
    for m in _DRU_RULE_RE.finditer(text or ""):
        name, body = m.group(1), m.group(2)
        if name.startswith("aiee_iso_"):
            continue
        c = _DRU_CONSTRAINT_RE.search(body)
        if not c:
            continue
        cond = _DRU_CONDITION_RE.search(body)
        lay = _DRU_LAYER_RE.search(body)
        out.append({"name": name, "constraint": c.group(1),
                    "min_mm": float(c.group(2)),
                    "nets": sorted(set(_DRU_NETNAME_RE.findall(body))),
                    "condition": cond.group(1) if cond else None,
                    "layer": lay.group(1) if lay else None})
    return out


_DRU_COURTYARD_RE = re.compile(r"intersectsCourtyard\('([^']+)'\)")


def _dru_text(dru_path: Path | None) -> str | None:
    if dru_path is None or not Path(dru_path).is_file():
        return None
    try:
        return Path(dru_path).read_text(encoding="utf-8")
    except OSError:
        return None


def dru_net_floors(dru_path: Path | None, constraint: str) -> dict[str, float]:
    """Per-net minimums of one constraint type from a .kicad_dru
    (e.g. 'track_width' -> {net: floor_mm}). Missing file -> {}. A rule
    held to a footprint's courtyard (rules_gen's aiee_pwr_neck) is not the
    net's floor: dru_neck_rules reads those (#924)."""
    text = _dru_text(dru_path)
    if text is None:
        return {}
    floors: dict[str, float] = {}
    for rule in parse_dru_rules(text):
        if rule["constraint"] != constraint:
            continue
        if _DRU_COURTYARD_RE.search(rule["condition"] or ""):
            continue
        for net in rule["nets"]:
            floors[net] = max(floors.get(net, 0.0), rule["min_mm"])
    return floors


def dru_neck_rules(dru_path: Path | None,
                   constraint: str = "track_width") -> list[dict]:
    """The .kicad_dru's courtyard-held minimums of one constraint, such as
    rules_gen's aiee_pwr_neck: [{"net", "ref", "min_mm"}], one per net and
    courtyard the rule names (#924). Missing file -> []."""
    text = _dru_text(dru_path)
    if text is None:
        return []
    out = []
    for rule in parse_dru_rules(text):
        refs = _DRU_COURTYARD_RE.findall(rule["condition"] or "")
        if rule["constraint"] != constraint or not refs:
            continue
        out += [{"net": net, "ref": ref, "min_mm": rule["min_mm"]}
                for net in rule["nets"] for ref in refs]
    return out


def build_net_clearances(pro_path: Path | None, dru_path: Path | None,
                         board_nets) -> dict[str, float] | None:
    """Per-net clearance map for KRT's --net-clearances (LEARNINGS 1522):
    KRT's --clearance is a CAP on its auto-read netclass map (it silently
    pulled 0.635 mm HV nets DOWN to 0.2), and KRT cannot read a .kicad_dru,
    so DRU-only HV clearance never reaches the router without this file.

    Values = max(netclass clearance, DRU per-net clearance rules) - the max
    over the class value means nothing is ever capped DOWN. Fails OPEN
    (returns None -> no file emitted -> today's behavior) on anything
    unparseable rather than emit wrong values."""
    try:
        out: dict[str, float] = {}
        nets = list(board_nets)
        if pro_path is not None and Path(pro_path).is_file():
            proj = json.loads(Path(pro_path).read_text(encoding="utf-8"))
            ns = proj.get("net_settings") or {}
            classes = {c.get("name"): c for c in ns.get("classes") or []
                       if isinstance(c, dict)}
            for pat in ns.get("netclass_patterns") or []:
                cls = classes.get(pat.get("netclass"))
                pattern = pat.get("pattern")
                if not cls or not pattern \
                        or not isinstance(cls.get("clearance"), (int, float)):
                    continue
                for net in nets:
                    if fnmatch.fnmatchcase(net, pattern):
                        out[net] = max(out.get(net, 0.0),
                                       float(cls["clearance"]))
        for net, clr in dru_net_floors(dru_path, "clearance").items():
            if net in board_nets:
                out[net] = max(out.get(net, 0.0), clr)
        out = {n: round(v, 4) for n, v in sorted(out.items()) if v > 0}
        return out or None
    except Exception:  # noqa: BLE001 - fail open, never emit wrong values
        return None


def pair_geometry(bg: geom.BoardGeom, impedance_ohm: float,
                  gap_c2c_mm: float | None = None) -> tuple[float, float]:
    """(track_width, edge_gap) mm for a differential pair on the preferred
    outer layer, from the board stackup (impedance.diff_pair - the same math
    rules_gen uses). constraints gap_mm is CENTER-to-center (check_diffpair's
    convention); KRT's --diff-pair-gap is edge-to-edge."""
    outer = bg.copper_layers[0]
    _, below = bg.stackup.adjacent(outer)
    if below is None or not bg.stackup.dielectrics:
        return FALLBACK_PAIR_WIDTH, 0.2
    h = bg.stackup.height_between(outer, below)
    er = bg.stackup.epsilon_between(outer, below)
    t = bg.stackup.copper_thickness.get(outer, 0.035)
    if gap_c2c_mm is not None:
        # solve width for the pinned center-to-center pitch: w + s = pitch
        w, s = imp.diff_pair(float(impedance_ohm), h, t, er)
        # re-solve with gap pinned so pitch holds approximately
        for _ in range(8):
            s = max(float(gap_c2c_mm) - w, 0.05)
            w, s = imp.diff_pair(float(impedance_ohm), h, t, er, gap=s)
        return w, s
    return imp.diff_pair(float(impedance_ohm), h, t, er)


def rf_width(bg: geom.BoardGeom, impedance_ohm: float) -> float:
    outer = bg.copper_layers[0]
    _, below = bg.stackup.adjacent(outer)
    if below is None or not bg.stackup.dielectrics:
        return BASE_TRACK_WIDTH
    h = bg.stackup.height_between(outer, below)
    er = bg.stackup.epsilon_between(outer, below)
    t = bg.stackup.copper_thickness.get(outer, 0.035)
    return round(imp.solve_width(float(impedance_ohm), h, t, er), 4)


# ------------------------------------------------------------ pad window

def _pad_window_foreign(bg: geom.BoardGeom, net: str, layer: str):
    """WIRED foreign copper on `layer`: tracks/pads/vias of every OTHER net
    INCLUDING unnetted pads (bg.nets omits them - LEARNINGS 1538: 37
    invisible no-net items caused 4 real clearance errors), but never zone
    fills (fills re-flow around new copper at refill)."""
    parts = [t.poly for t in bg.tracks_of(layer=layer) if t.net != net]
    parts += [p.poly for p in bg.pads_of(layer=layer) if p.net != net]
    parts += [v.poly for v in bg.vias_of(layer=layer) if v.net != net]
    return unary_union(parts) if parts else None


def widest_connectable_mm(pad, foreign, outline, clearance: float,
                          edge_clearance: float,
                          cap: float = PAD_WINDOW_CAP) -> float:
    """The widest track that can legally touch this pad (LEARNINGS 795):
    sup over centreline points P of 2*(dist(P, foreign) - CLR) subject to
    dist(P, pad copper) <= W/2, with P additionally kept on the board
    (outline eroded by edge_clearance + W/2). Solved by bisection on W -
    feasible(W) iff pad.buffer(W/2), clipped to the eroded outline, minus
    foreign.buffer(CLR + W/2) still has area. Returns cap when unbounded
    within the cap."""
    fl = None
    if foreign is not None and not foreign.is_empty:
        window = box(*pad.poly.buffer(cap + clearance + 0.1).bounds)
        fl = foreign.intersection(window)
        if fl.is_empty:
            fl = None

    def feasible(w: float) -> bool:
        region = pad.poly.buffer(w / 2.0)
        if outline is not None and not outline.is_empty:
            region = region.intersection(
                outline.buffer(-(edge_clearance + w / 2.0)))
        if fl is not None and not region.is_empty:
            region = region.difference(fl.buffer(clearance + w / 2.0))
        return not region.is_empty and region.area > 1e-9

    if feasible(cap):
        return float(cap)
    lo, hi = 0.0, cap
    for _ in range(14):                      # cap/2^14 < 0.001 mm
        mid = (lo + hi) / 2.0
        if feasible(mid):
            lo = mid
        else:
            hi = mid
    return round(lo, 3)


def _clearance_floor(pro_path: Path | None) -> float:
    """The board's minimum copper clearance (rules.min_clearance, else the
    Default netclass) - the OPTIMISTIC floor for the pad window: if the
    width is unmeetable even at the floor, it is unmeetable, full stop."""
    if pro_path is not None and Path(pro_path).is_file():
        try:
            proj = json.loads(Path(pro_path).read_text(encoding="utf-8"))
            rules = (proj.get("board", {}).get("design_settings", {})
                     .get("rules", {}))
            if isinstance(rules.get("min_clearance"), (int, float)) \
                    and rules["min_clearance"] > 0:
                return float(rules["min_clearance"])
            for c in (proj.get("net_settings", {}).get("classes") or []):
                if c.get("name") == "Default" \
                        and isinstance(c.get("clearance"), (int, float)):
                    return float(c["clearance"])
        except (OSError, json.JSONDecodeError):
            pass
    return KICAD_DEFAULTS["clearance"]


def pad_window_report(pcb: Path, constraints: dict,
                      nets_arg: str | None) -> dict:
    """--pad-window: deterministic no-routing probe - the measured width
    ceiling per pad of each power net vs its DRU per-net track_width floor
    (ladder row 78; converts the pd-trigger premise falsification and the
    carrier's ad-hoc pad_gap.py into one scripted call)."""
    bg = geom.BoardGeom.from_file(pcb)
    if nets_arg:
        nets = [s.strip() for s in nets_arg.split(",") if s.strip()]
    else:
        nets = [e.get("net") for e in constraints.get("power") or []
                if isinstance(e, dict) and e.get("net")]
    if not nets:
        raise CheckError("--pad-window: no target nets (pass --nets or "
                         "declare constraints['power'])")
    pro = pcb.with_suffix(".kicad_pro")
    clearance = _clearance_floor(pro if pro.is_file() else None)
    edge = grading_floors(pro if pro.is_file() else None)["edge_clearance"]
    rule_floors = dru_net_floors(pcb.with_suffix(".kicad_dru"), "track_width")
    outer = set(_outer_layers(bg))
    rows: list[dict] = []
    violations: list[dict] = []
    missing: list[str] = []
    for net in nets:
        if net not in bg.nets:
            missing.append(net)
            continue
        f_cache: dict = {}
        for pad in sorted(bg.pads_of(net=net),
                          key=lambda p: (p.ref, p.number)):
            layers = [l for l in pad.layers if l in outer] \
                or [pad.layers[0]]
            widest = 0.0
            for layer in layers:            # best attach layer wins
                if layer not in f_cache:
                    f_cache[layer] = _pad_window_foreign(bg, net, layer)
                widest = max(widest, widest_connectable_mm(
                    pad, f_cache[layer], bg.outline, clearance, edge))
            rule_min = rule_floors.get(net)
            ok = rule_min is None or widest >= rule_min - 1e-9
            rows.append({"ref": pad.ref, "pad": pad.number, "net": net,
                         "widest_mm": widest, "rule_min_mm": rule_min,
                         "ok": ok})
            if not ok:
                violations.append(violation(
                    SCRIPT, "error", list(pad.center), layers[0], net,
                    [pad.ref],
                    f"power pad {pad.ref}.{pad.number} ({net}): widest "
                    f"connectable track {widest} mm < DRU floor {rule_min} "
                    "mm - no legal rule-width track can reach this pad; "
                    "pour fan-in instead "
                    "(reference/remediations/track_width.md step 4)",
                    SCRIPT, kind="pad_window_unmeetable",
                    widest_mm=widest, rule_min_mm=rule_min))
    return checklib.report(SCRIPT, str(pcb), violations, facts={
        "probe": "pad_window", "pad_window": rows, "nets": nets,
        "missing_nets": missing, "clearance_mm": clearance,
        "edge_clearance_mm": edge, "cap_mm": PAD_WINDOW_CAP})


# ------------------------------------------------------------ stub-pad surgery

_NET_NODE = r'\n[ \t]*\(net(?:\s+\d+)?\s+"{name}"\s*\)'


def _footprint_block(text: str, ref: str) -> tuple[int, int]:
    """(start, end) of the footprint block whose Reference property is `ref`."""
    m = re.search(r'\(property\s+"Reference"\s+"%s"' % re.escape(ref), text)
    if not m:
        raise CheckError(f"footprint {ref!r} not found in board text")
    start = text.rfind("(footprint", 0, m.start())
    if start < 0:
        raise CheckError(f"no (footprint block encloses Reference {ref!r}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return start, i + 1
    raise CheckError(f"unbalanced footprint block for {ref!r}")


def _pad_block(block: str, number: str) -> tuple[int, int]:
    """(start, end) of the (pad "number" ...) node inside a footprint block."""
    m = re.search(r'\(pad\s+"%s"\s' % re.escape(number), block)
    if not m:
        raise CheckError(f"pad {number!r} not found in footprint block")
    depth = 0
    for i in range(m.start(), len(block)):
        if block[i] == "(":
            depth += 1
        elif block[i] == ")":
            depth -= 1
            if depth == 0:
                return m.start(), i + 1
    raise CheckError(f"unbalanced pad block {number!r}")


def detach_stub_pads(text: str, targets: list[tuple]
                     ) -> tuple[str, list[tuple[str, str]]]:
    """Remove the (net ...) node of every pad of net inside footprint ref,
    for each (ref, net) target; a (ref, net, number) target detaches that
    one pad only (a connector's second row). Returns (new_text, restores)
    where restores is [(detached_block, original_block)] for
    restore_stub_pads."""
    restores: list[tuple[str, str]] = []
    for target in targets:
        ref, net = target[0], target[1]
        number = target[2] if len(target) > 2 else None
        start, end = _footprint_block(text, ref)
        block = text[start:end]
        pat = _NET_NODE.format(name=re.escape(net))
        if number is None:
            newblock, nsub = re.subn(pat, "", block)
        else:
            ps, pe = _pad_block(block, number)
            pad, nsub = re.subn(pat, "", block[ps:pe])
            newblock = block[:ps] + pad + block[pe:]
        if nsub == 0:
            raise CheckError(f"no pad of net {net!r} found in footprint {ref!r}")
        if text.count(block) != 1:
            raise CheckError(f"footprint block for {ref!r} is not unique")
        text = text.replace(block, newblock, 1)
        restores.append((newblock, block))
    return text, restores


def restore_stub_pads(text: str, restores: list[tuple[str, str]]) -> str:
    """Re-insert the original footprint blocks into KRT's output. KRT's
    writer preserves footprint text byte-identical; anything else is a hard
    error (the caller rolls back).

    Applied in REVERSE detach order: when two stub targets share one
    footprint, detach_stub_pads nests its edits (the second detach was made
    on the once-detached block), so only the innermost (last) detached form
    exists in the routed output (S11 review finding)."""
    for detached, original in reversed(restores):
        if text.count(detached) != 1:
            raise CheckError("cannot restore stub pad net: detached footprint "
                             "block not found verbatim in routed output "
                             "(KRT writer changed footprint text?)")
        text = text.replace(detached, original, 1)
    return text


def unmatched_stub_pads(bg: geom.BoardGeom, p: str, n: str
                        ) -> list[tuple[str, str]]:
    """(ref, net) groups of pair pads outside every matched terminal (USB
    pull-up, series tap). Grouped by (ref, net); a group is a stub only when
    NONE of its pads is a matched terminal."""
    matched: set[tuple[str, tuple[float, float]]] = set()
    for pp, nn in check_diffpair.matched_terminals(bg, p, n):
        matched.add((pp.ref, (round(pp.center[0], 3), round(pp.center[1], 3))))
        matched.add((nn.ref, (round(nn.center[0], 3), round(nn.center[1], 3))))
    groups: dict[tuple[str, str], list[bool]] = {}
    for net in (p, n):
        for pad in bg.pads_of(net=net):
            key = (pad.ref, net)
            is_matched = (pad.ref, (round(pad.center[0], 3),
                                    round(pad.center[1], 3))) in matched
            groups.setdefault(key, []).append(is_matched)
    return sorted(k for k, flags in groups.items() if not any(flags))


TERMINATION_REF = re.compile(r"^R\d")   # a resistor: a 2-pin connector is a terminal


def termination_pads(bg: geom.BoardGeom, p: str, n: str
                     ) -> list[tuple[str, str]]:
    """(ref, net) of every two-pad resistor with one pad on p and the other on n
    (a differential termination across the pair: DDR CK's 100 ohm at the
    DRAM, an LVDS receiver's R). It taps the pair, it is not a trunk end:
    KRT would make it a third terminal and route the pair as a chain of
    legs from the pads, never from route_escape's stub tips (#529:
    SDCLK0 U100 -> R205 -> U200 'electrically-short'). Detached only while
    the pair keeps two other terminals; its hookup is a short single-ended
    stub for route_auto."""
    refs: dict[str, list] = {}
    for net in (p, n):
        for pad in bg.pads_of(net=net):
            refs.setdefault(pad.ref, []).append(pad)
    out = []
    for ref, pads in sorted(refs.items()):
        if not TERMINATION_REF.match(ref):
            continue
        allpads = bg.pads_of(ref=ref)
        if len(allpads) == 2 and {q.net for q in pads} == {p, n} \
                and len(pads) == 2:
            out += [(ref, p), (ref, n)]
    others = {pad.ref for net in (p, n) for pad in bg.pads_of(net=net)} \
        - {r for r, _ in out}
    return out if len(others) >= 2 else []


ESD_TAP_REF = re.compile(r"^D\d")      # a diode array: TVS / ESD clamp


def esd_tap_pads(bg: geom.BoardGeom, p: str, n: str
                 ) -> list[tuple[str, str]]:
    """(ref, net) of every diode part with exactly ONE pad on p and ONE on n
    (a TPD2E2U06-style ESD clamp hanging off the pair). It shares a ref
    across both halves, so matched_terminals makes it a third trunk end and
    KRT routes the pair as a chain of legs through it; a clamp off the
    straight line (or a short leg to the connector) then defers the whole
    pair to single-ended as 'electrically-short'. A flow-through clamp (two
    pads per net) IS inline and stays a terminal. Detached only while the
    pair keeps two other terminal refs; the tap is a short single-ended
    stub for route_auto, as termination_pads."""
    refs: dict[str, list] = {}
    for net in (p, n):
        for pad in bg.pads_of(net=net):
            refs.setdefault(pad.ref, []).append(pad)
    out = []
    for ref, pads in sorted(refs.items()):
        if ESD_TAP_REF.match(ref) and len(pads) == 2 \
                and {q.net for q in pads} == {p, n}:
            out += [(ref, p), (ref, n)]
    others = set(refs) - {r for r, _ in out}
    return out if len(others) >= 2 else []


def duplicate_row_pads(bg: geom.BoardGeom, p: str, n: str,
                       skip: set[str] = frozenset()
                       ) -> list[tuple[str, str, str]]:
    """(ref, net, pad) of every extra row of a connector that lands the pair
    twice (USB-C A6/A7 + B6/B7). KRT makes each row its own terminal, so
    the A-to-B hop becomes a ~1.7 mm leg that is deferred single-ended and
    the pair reports 'partial'. The row whose midpoint is nearest the other
    terminal refs (those not in `skip`) is kept; the others are detached
    pad by pad and their short row-to-row hop is left for route_auto."""
    p_pads, n_pads = bg.pads_of(net=p), bg.pads_of(net=n)
    refs = {q.ref for q in p_pads + n_pads} - set(skip)
    out: list[tuple[str, str, str]] = []
    for ref in sorted(refs):
        rp = [q for q in p_pads if q.ref == ref]
        rn = [q for q in n_pads if q.ref == ref]
        if len(rp) < 2 or len(rn) < 2:
            continue
        far = [q for q in p_pads + n_pads if q.ref in refs and q.ref != ref]
        if not far:
            continue
        cx = sum(q.center[0] for q in far) / len(far)
        cy = sum(q.center[1] for q in far) / len(far)
        rows = []
        for pp in rp:
            nn = min(rn, key=lambda q: math.hypot(q.center[0] - pp.center[0],
                                                  q.center[1] - pp.center[1]))
            mx = (pp.center[0] + nn.center[0]) / 2
            my = (pp.center[1] + nn.center[1]) / 2
            rows.append((math.hypot(mx - cx, my - cy), pp, nn))
        rows.sort(key=lambda r: r[0])
        keep = {rows[0][1].number, rows[0][2].number}
        out += [(ref, p, q.number) for q in rp if q.number not in keep]
        out += [(ref, n, q.number) for q in rn if q.number not in keep]
    return out


# ------------------------------------------------------------ KRT invocation

def _tools() -> tuple[Path, Path]:
    cli = env.find_kicad_cli()
    if cli is None:
        raise CheckError("kicad-cli not found (check_env.py --full has "
                         "remediation); pipeline pins KiCad 10.0.3")
    krt = env.find_krt()
    if krt is None:
        raise CheckError(KRT_REMEDIATION)
    return cli, krt


def _stage_board(pcb: Path, work: Path) -> Path:
    """Board + every same-stem sidecar (pro/prl/dru/sch) -> work dir."""
    work.mkdir(parents=True, exist_ok=True)
    staged = work / pcb.name
    for src in pcb.parent.glob(pcb.stem + ".*"):
        if src.is_file() and not src.name.endswith(".lck"):
            shutil.copy2(src, work / src.name)
    if not staged.is_file():
        raise CheckError(f"staging failed: {staged}")
    return staged


def write_fab_overrides(work: Path, floors: dict) -> Path:
    """Pin KRT's fab floor to the board's DRC grading floors (hard floor: the
    overrides file disables fab-tier escalation below what DRC grades)."""
    p = work / "fab_overrides.txt"
    p.write_text(
        f"clearance = {floors['clearance']}\n"
        f"track_width = {floors['track_width']}\n"
        f"via_diameter = {floors['via_diameter']}\n"
        f"via_drill = {floors['via_drill']}\n"
        f"board_edge = {floors['edge_clearance']}\n", encoding="utf-8")
    return p


class KrtTimeout(CheckError):
    """One KRT run hit its process timeout. Pair routing treats it as that
    pair failing, so the pairs already on the staged board still get reported."""


def run_krt(krt_dir: Path, script: str, staged: Path, out: Path,
            extra: list[str], floors: dict, fab_file: Path,
            grid_step: float, timeout_s: int,
            net_clearances: Path | None = None,
            stdout_sink: list | None = None,
            layer_widths: dict[str, float] | None = None) -> dict:
    """One KRT run staged -> fresh out. Returns the parsed LAST JSON_SUMMARY.
    Args are a list (no shell) so net names like '/USB_DP' survive.
    net_clearances: optional per-net clearance JSON for KRT's
    --net-clearances (NOT capped by --clearance - LEARNINGS 1522; this is
    the only path a DRU-only HV clearance reaches the router).
    stdout_sink: when given, KRT's full stdout is appended to it (the
    Coverage / no-route diagnostics live there, not in the summary).
    Every run goes through lib/krt_launch.py, which always keeps net-tie
    footprints' copper on copper (#1008: KRT's writer moved their bridges
    to silkscreen).
    layer_widths: {layer: mm} drawn per layer (#489: rules_gen's stripline
    widths), through the launcher, since KRT only fills its per-layer
    widths from its own impedance solver.
    route_diff.py always runs with --via-joint-ends (#529): a pair escaped
    by route_escape starts at its stub tips, not at the dog-bone via inside
    the ball field."""
    flags: list[str] = []
    if layer_widths:
        wpath = out.with_suffix(".layer_widths.json")
        wpath.write_text(json.dumps(layer_widths, sort_keys=True), encoding="utf-8")
        flags += ["--layer-widths", str(wpath)]
    if script == "route_diff.py":
        flags.append("--via-joint-ends")
    launch = [sys.executable, str(SCRIPTS / "lib" / "krt_launch.py"), *flags,
              script]
    args = [*launch, str(staged), "--output", str(out),
            "--no-fix-drc-settings",
            "--grid-step", str(grid_step),
            "--clearance", str(floors["clearance"]),
            "--via-size", str(floors["via_diameter"]),
            "--via-drill", str(floors["via_drill"]),
            "--board-edge-clearance", str(floors["edge_clearance"]),
            "--fab-overrides", str(fab_file)]
    if net_clearances is not None:
        args += ["--net-clearances", str(net_clearances)]
    args += extra
    try:
        cp = subprocess.run(args, cwd=str(krt_dir), capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise KrtTimeout(f"KRT {script} timed out after {timeout_s}s") from exc
    if stdout_sink is not None:
        stdout_sink.append(cp.stdout or "")
    summary = parse_summary(cp.stdout or "")
    if summary is None:
        # KRT's "nothing to route" path (every target net already fully
        # connected) prints no JSON_SUMMARY but rc=0 + writes an unchanged
        # copy - a legitimate no-op on idempotent re-runs, not an error.
        if cp.returncode == 0 and out.is_file() \
                and "nothing to route" in (cp.stdout or ""):
            return {"already_routed": True, "successful": 0, "failed": 0}
        tail = ((cp.stdout or "") + "\n" + (cp.stderr or "")).strip()[-600:]
        raise CheckError(f"KRT {script} produced no JSON_SUMMARY "
                         f"(rc={cp.returncode}): {tail}")
    return summary


def _pair_outcome(summary: dict, p: str, n: str) -> tuple[bool, str]:
    """(routed_as_coupled_pair, reason) from a route_diff JSON_SUMMARY."""
    for rep in summary.get("pair_reports", []) or []:
        if rep.get("p_net") == p and rep.get("n_net") == n:
            if rep.get("outcome") == "coupled" and \
                    rep.get("pair") in (summary.get("routed_diff_pairs") or []):
                return True, "coupled"
            return False, (rep.get("failure_reason") or rep.get("outcome")
                           or "not-routed")
    # no report row for this pair at all -> it was never even detected
    return False, "pair-not-detected"


def _net_failed(summary: dict, net: str) -> str | None:
    """Failure reason if a route.py summary says `net` did not fully route."""
    if net in (summary.get("failed_single") or []):
        return "single-ended route failed"
    for item in summary.get("failed_multipoint") or []:
        if item.get("net_name") == net:
            k = len(item.get("failed_pads") or [])
            return f"multipoint: {k} pad(s) unconnected"
    return None


def _outer_layers(bg: geom.BoardGeom) -> list[str]:
    cl = bg.copper_layers
    return [cl[0]] if len(cl) < 2 else [cl[0], cl[-1]]


def _slim(summary: dict, keys: tuple[str, ...]) -> dict:
    return {k: summary.get(k) for k in keys if k in summary}


# ------------------------------------------------------------ item runners

def _next_out(ctx: dict, tag: str) -> Path:
    """A FRESH output path per KRT attempt (route.py re-reads DRC floors from
    an output-sibling .kicad_pro on same-path reruns)."""
    ctx["seq"] = ctx.get("seq", 0) + 1
    return ctx["work"] / f"krt_{ctx['seq']:02d}_{tag}.kicad_pcb"


STRIPLINE_COST = "2"   # an impedance-sized inner layer: after the near outer


def _stripline_width_for_pitch(bg: geom.BoardGeom, layer: str, z: float,
                               pitch: float, lo: float, hi: float) -> float:
    """Width in [lo, hi] whose pair, at centre pitch `pitch`, is z ohm on
    `layer` (check_diffpair.layer_zdiff: the checker's own model). Zdiff
    falls as the width grows at fixed pitch; outside [lo, hi] the nearer end."""
    def zd(w: float) -> float:
        return check_diffpair.layer_zdiff(bg, layer, w, pitch - w)[0] or z
    try:
        if zd(lo) <= z:
            return lo
        if zd(hi) >= z:
            return hi
        for _ in range(30):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if zd(mid) > z else (lo, mid)
        return round((lo + hi) / 2, 4)
    except (ValueError, RuntimeError, ZeroDivisionError):
        return lo


# Cost of a pinned pair's later layers: only worth using to reach its pins.
PINNED_ESCAPE_COST = "8"


def pair_layer_plan(bg: geom.BoardGeom, z: float, gap_c2c: float | None,
                    geo: dict, p: str, n: str) -> dict | None:
    """KRT geometry for a pair rules_gen sized on stripline layers (#489),
    or None when it has no per-layer rules (2L/4L boards: unchanged).

    KRT's coupled router holds ONE centre pitch (track_width + gap) on every
    layer and draws each layer at layer_widths[layer]. The pitch is the
    widest layer's own (width + gap from the .kicad_dru); every other layer
    is re-solved to hit z at that pitch: the outer by pair_geometry, an inner
    one inside its DRU width window. So each layer lands on its impedance
    and its edge gap is never under the DRU gap minimum."""
    inner = {ly: g for ly, g in (geo.get(p) or {}).items()
             if ly in bg.copper_layers[1:-1] and "width_mm" in g and "gap_mm" in g}
    if not inner:
        return None
    w_o, g_o = pair_geometry(bg, z, gap_c2c)
    pitch = max([w_o + g_o] + [g["width_mm"] + g["gap_mm"] for g in inner.values()])
    if w_o + g_o < pitch - 1e-4:
        w_o, g_o = pair_geometry(bg, z, pitch)
        pitch = max(pitch, w_o + g_o)
    widths = {ly: round(w_o, 4) for ly in _outer_layers(bg)}
    for ly, g in sorted(inner.items()):
        w = g["width_mm"]
        if w + g["gap_mm"] < pitch - 1e-4:
            w = _stripline_width_for_pitch(bg, ly, z, pitch, g["width_min_mm"],
                                           g["width_max_mm"])
        widths[ly] = round(w, 4)
    outer = _outer_layers(bg)
    layers = [outer[0], *sorted(inner)] + outer[1:]
    costs = ["1"] + [STRIPLINE_COST] * len(inner) + (["3"] if len(outer) > 1 else [])
    return {"layers": layers, "costs": costs, "track_width": round(w_o, 4),
            "gap": round(pitch - w_o, 4), "pitch_mm": round(pitch, 4),
            "layer_widths": widths}


def bare_bga_balls(bg: geom.BoardGeom, p: str, n: str) -> list[str]:
    """"REF.PIN" of every ball of p or n, inside a ball field (bgalib),
    that no copper of its net touches: route_escape left it (dropped,
    ripped by DRC, via-in-pad stripped). A coupled pair cannot launch from
    a bare inner ball of an escaped field (#529)."""
    out = []
    for b in bgalib.find_bgas(bg):
        for num, net in sorted(b.nets.items()):
            if net not in (p, n):
                continue
            pad = next((q for q in bg.pads_of(net=net, ref=b.ref)
                        if q.number == num), None)
            if pad is None:
                continue
            ring = pad.poly
            if any(t.poly.intersects(ring) for t in bg.tracks_of(net=net)) \
                    or any(v.poly.intersects(ring) for v in bg.vias_of(net=net)):
                continue
            out.append(f"{b.ref}.{num}")
    return out


def launch_pair(ctx: dict, bg: geom.BoardGeom, p: str, n: str, plan: dict,
                skip_refs) -> dict | None:
    """Re-lay this stripline pair's BGA terminals where KRT can launch it
    (#546: lib/pairlaunch.py). Edits the staged board; the caller restores it
    if the pair doesn't route. Returns the fact, None when nothing changed."""
    inner = [ly for ly in plan["layers"] if ly in bg.copper_layers[1:-1]]
    if not inner:
        return None
    floors = ctx["floors"]
    spec = pairlaunch.Spec(
        layer=inner[0], width=plan["layer_widths"][inner[0]],
        pitch=plan["pitch_mm"], clearance=floors["clearance"],
        via_d=floors["via_diameter"], via_drill=floors["via_drill"],
        hole_to_hole=floors.get("hole_to_hole", 0.0))
    got = pairlaunch.plan_launch(bg, bgalib.find_bgas(bg), p, n, spec, skip_refs)
    if got["ops"]:
        route_edit.apply_ops(ctx["staged"], got["ops"])
        ctx.setdefault("launched", set()).update((p, n))
    if not got["ops"] and not got["failed"]:
        return None
    return {"terminals": got["terminals"], "failed": got["failed"]}


def snap_layer_widths(pcb: Path, nets: tuple[str, str],
                      widths: dict[str, float], old_uuids: set[str]) -> int:
    """KRT draws a pair's connector to a stub tip at --track-width, whatever
    the layer's width is (#546: 0.1054 mm on In2 against a 0.0978 max). Set
    this run's new tracks to their layer's width; returns how many changed."""
    bg = geom.BoardGeom.from_file(pcb)
    ops = [{"op": "set_width", "width": widths[t.layer], "uuid_prefix": t.uuid}
           for net in nets for t in bg.tracks_of(net=net)
           if t.layer in widths and t.uuid not in old_uuids
           and abs(t.width - widths[t.layer]) > 1e-4]
    if ops:
        route_edit.apply_ops(pcb, ops)
    return len(ops)


# check_diffpair kinds that reject a routed rung (#529): a pair KRT reports
# "coupled" can still run apart for 15 mm (Ethernet on linux-computer) or sit
# off its impedance. Skew alone is route_tune's job (it trims it), so a skew
# error is kept and reported.
REJECT_KINDS = ("diffpair_uncoupled", "diffpair_impedance")


def rung_verdict(pcb: Path, spec: dict, pitch: float, z: float | None
                 ) -> tuple[list[dict], dict]:
    """check_diffpair on one routed rung. Returns (rejecting findings,
    check facts); an empty list keeps the rung."""
    p, n = spec["p"], spec["n"]
    cspec = {"p": p, "n": n, "gap_mm": round(pitch, 4)}
    if z:
        cspec["impedance_ohm"] = z
    for k in ("max_skew_mm", "max_uncoupled_mm", "coupling_factor",
              "impedance_tol_pct", "term_pair_mm", "kind"):
        if k in spec:
            cspec[k] = spec[k]
    vs, facts = check_diffpair.check_pair(geom.BoardGeom.from_file(pcb), cspec)
    bad = [{"kind": v.get("kind"), "msg": v.get("msg"),
            **({"layer": v["layer"]} if v.get("layer") else {})}
           for v in vs if v.get("severity") == "error"
           and v.get("kind") in REJECT_KINDS]
    keep = ("skew_mm", "uncoupled_p_mm", "uncoupled_n_mm", "breakout_p_mm",
            "breakout_n_mm", "coupling_max_mm", "gap_median_mm")
    return bad, {k: facts[k] for k in keep if k in facts}


def route_diff_item(ctx: dict, spec: dict) -> tuple[dict | None, dict | None]:
    """Route one diff pair (width ladder + stub-pad detach). Returns
    (fact, violation): exactly one is None."""
    p, n = spec["p"], spec["n"]
    staged = ctx["staged"]
    bg = geom.BoardGeom.from_file(staged)
    if p not in bg.nets or n not in bg.nets:
        missing = [x for x in (p, n) if x not in bg.nets]
        return None, violation(
            SCRIPT, "warning", None, None, p, [],
            f"diff pair {p}/{n} names net(s) not on the board: {missing} "
            f"(stale constraints?)", SCRIPT,
            kind="critical_missing_net", pair=[p, n], missing=missing)

    z = spec.get("impedance_ohm", default_impedance(p, n))
    plan = pair_layer_plan(bg, z, spec.get("gap_mm"), ctx.get("zgeo") or {}, p, n)
    if plan:
        w, g = plan["track_width"], plan["gap"]
    else:
        w, g = pair_geometry(bg, z, spec.get("gap_mm"))
    ladder = [(w, g)]
    if abs(w - FALLBACK_PAIR_WIDTH) > 1e-6:
        ladder.append((FALLBACK_PAIR_WIDTH, max(g, ctx["floors"]["clearance"])))
    # constraints may pin a pair to layers, cheapest first (a bus laid out as
    # a river: each pair on its planned layer, none hooking over the next).
    # Pinned pairs get no fallback rung: that width only exists on the outers.
    pinned = list(spec.get("layers") or [])
    if pinned:
        ladder = ladder[:1]

    stubs = unmatched_stub_pads(bg, p, n)
    terms = [t for t in termination_pads(bg, p, n) if t not in stubs]
    terms += [t for t in esd_tap_pads(bg, p, n)
              if t not in stubs and t not in terms]
    stubs = sorted(stubs + terms)
    stubs += duplicate_row_pads(bg, p, n, skip={r for r, *_ in stubs})
    original_text = staged.read_text(encoding="utf-8")
    launch = None
    # a pair pinned to an outer layer keeps its outer-layer escape stubs
    if plan and (not pinned or pinned[0] in bg.copper_layers[1:-1]):
        launch = launch_pair(ctx, bg, p, n, plan,
                             {s[0] for s in stubs if len(s) == 2})
    pre_uuids = _copper_uuids(staged)
    restores: list[tuple[str, str]] = []
    if stubs:
        detached_text, restores = detach_stub_pads(
            staged.read_text(encoding="utf-8"), stubs)
        staged.write_text(detached_text, encoding="utf-8")
    routed_ok = False

    outer = _outer_layers(bg)
    costs = ["1"] if len(outer) == 1 else ["1", "3"]
    rejected: list[dict] = []
    try:
        last_reason = "no attempt"
        for rung, (rw, rg) in enumerate(ladder, 1):
            out = _next_out(ctx, f"diff_r{rung}")
            # rung 1 of a stripline-sized pair may use its inner layers at
            # their own widths; the 0.2 mm fallback stays on the outers
            lplan = plan if rung == 1 else None
            use_layers = lplan["layers"] if lplan else outer
            use_costs = lplan["costs"] if lplan else costs
            if pinned:
                use_layers = pinned
                use_costs = ["1"] + [PINNED_ESCAPE_COST] * (len(pinned) - 1)
            try:
                summary = run_krt(
                    ctx["krt"], "route_diff.py", staged, out,
                    ["--nets", p, n,
                     "--layers", *use_layers,
                     "--layer-costs", *use_costs,
                     "--track-width", f"{rw:g}", "--diff-pair-gap", f"{rg:g}",
                     "--via-size", f"{pair_via_size(ctx['floors']):g}"],
                    ctx["floors"], ctx["fab_file"], ctx["grid_step"],
                    ctx["timeout_s"],
                    net_clearances=ctx.get("net_clearances"),
                    layer_widths=lplan["layer_widths"] if lplan else None)
            except KrtTimeout:
                last_reason = "krt-timeout"
                continue
            if summary.get("already_routed"):
                # idempotent re-run: both nets already fully connected; the
                # post-run check_diffpair pass still validates coupling.
                ok, reason = True, "already_routed"
            else:
                ok, reason = _pair_outcome(summary, p, n)
            if ok:
                text = out.read_text(encoding="utf-8")
                if restores:
                    text = restore_stub_pads(text, restores)
                out.write_text(text, encoding="utf-8")
                if lplan:
                    snap_layer_widths(out, (p, n), lplan["layer_widths"], pre_uuids)
                bad, check = rung_verdict(out, spec, rw + rg, z)
                if bad:
                    rejected.append({"rung": rung, "width_mm": rw, "gap_mm": rg,
                                     "findings": bad, "check": check})
                    last_reason = "check_diffpair: " + ", ".join(
                        sorted({b["kind"] for b in bad}))
                    continue
                os.replace(out, staged)
                restores = []          # consumed: staged now holds original pads
                routed_ok = True
                bg2 = geom.BoardGeom.from_file(staged)
                layers = sorted({t.layer for net in (p, n)
                                 for t in bg2.tracks_of(net=net)})
                fact = {"kind": "diff", "nets": [p, n],
                        "impedance_ohm": z,
                        "width_mm": rw, "gap_mm": rg,
                        "impedance_width_mm": w, "ladder_rung": rung,
                        **({"layer_widths": lplan["layer_widths"],
                            "pitch_mm": lplan["pitch_mm"]} if lplan else {}),
                        "stub_pads_detached": [list(s) for s in stubs],
                        **({"terminations_left": sorted({r for r, _ in terms})}
                           if terms else {}),
                        "layers_used": layers,
                        "check": check,
                        **({"launch": launch} if launch else {}),
                        **({"rejected_rungs": rejected} if rejected else {}),
                        "krt": _slim(summary, ("routed_diff_pairs",
                                               "total_vias", "total_time",
                                               "min_clearance_used"))}
                return fact, None
            last_reason = reason
        if rejected:
            return None, violation(
                SCRIPT, "error", None, None, p, [],
                f"critical diff pair {p}/{n} routed but failed check_diffpair "
                f"on every rung ({last_reason}); the copper was not kept: "
                + "; ".join(f["msg"] for f in rejected[-1]["findings"])[:400],
                SCRIPT, kind="critical_pair_rejected", pair=[p, n],
                reason=last_reason, rejected_rungs=rejected)
        bare = bare_bga_balls(bg, p, n)
        hint = (f"; ball(s) {', '.join(bare)} have no escape (route_escape "
                "left them): escape them first") if bare else ""
        return None, violation(
            SCRIPT, "error", None, None, p, [],
            f"critical diff pair {p}/{n} failed to route as a coupled pair "
            f"({last_reason}); tried widths "
            f"{[rw for rw, _ in ladder]}{hint}", SCRIPT,
            kind="critical_route_failed", pair=[p, n], reason=last_reason,
            **({"unescaped_balls": bare} if bare else {}),
            **({"launch": launch} if launch else {}))
    finally:
        if not routed_ok:  # undo the launch and the detach on the staged board
            staged.write_text(original_text, encoding="utf-8")
            ctx.get("launched", set()).difference_update((p, n))


def _route_single_item(ctx: dict, kind: str, nets: list[str],
                       extra: list[str], per_net_meta: dict,
                       net_clearances: Path | None = None,
                       layer_widths: dict[str, float] | None = None
                       ) -> tuple[list, list]:
    """Shared route.py runner for power/rf. Returns (facts, violations).
    net_clearances: this run's --net-clearances file (default: the shared
    ctx map).

    Iteration ladder (T6, LEARNINGS 1433/1504): when KRT reports
    'No route found after N iterations' and the Coverage diagnostic blames a
    mostly-static frontier, the failed nets get ONE retry at 4M iterations -
    the scripted form of the fix that routed both carrier long hauls first
    try, replacing ~10 manual rip-set attempts."""
    staged = ctx["staged"]
    ncl = net_clearances or ctx.get("net_clearances")
    out = _next_out(ctx, kind)
    sink: list[str] = []
    summary = run_krt(ctx["krt"], "route.py", staged, out,
                      ["--nets", *nets] + extra,
                      ctx["floors"], ctx["fab_file"], ctx["grid_step"],
                      ctx["timeout_s"],
                      net_clearances=ncl,
                      stdout_sink=sink, layer_widths=layer_widths)
    os.replace(out, staged)  # keep partial successes; failures become violations
    stdout = sink[0] if sink else ""
    share = coverage_static_share(stdout)
    reasons = {net: _net_failed(summary, net) for net in nets}
    failed = sorted(n for n, r in reasons.items() if r)
    retry = None
    if failed and NO_ROUTE_RE.search(stdout) \
            and (share is None or share >= STATIC_RETRY_SHARE):
        retry = {"nets": failed, "static_share": share,
                 "max_iterations": RETRY_MAX_ITERATIONS, "kept": False}
        out2 = _next_out(ctx, f"{kind}_hi")
        try:
            s2 = run_krt(ctx["krt"], "route.py", staged, out2,
                         ["--nets", *failed] + extra
                         + ["--max-iterations", str(RETRY_MAX_ITERATIONS),
                            "--max-probe-iterations",
                            str(RETRY_MAX_PROBE_ITERATIONS)],
                         ctx["floors"], ctx["fab_file"], ctx["grid_step"],
                         ctx["timeout_s"],
                         net_clearances=ncl, layer_widths=layer_widths)
        except CheckError as exc:
            retry["error"] = str(exc)[:200]
        else:
            os.replace(out2, staged)
            retry["kept"] = True
            for net in failed:
                reasons[net] = _net_failed(s2, net)
    bg2 = geom.BoardGeom.from_file(staged)
    facts, violations = [], []
    for net in nets:
        reason = reasons[net]
        layers = sorted({t.layer for t in bg2.tracks_of(net=net)})
        if reason is None:
            fact = {"kind": kind, "nets": [net], **per_net_meta.get(net, {}),
                    "layers_used": layers,
                    "krt": _slim(summary, ("successful", "failed",
                                           "multipoint_pads_connected",
                                           "multipoint_pads_total",
                                           "total_vias", "total_time"))}
            if retry and net in retry["nets"]:
                fact["iteration_retry"] = True
            facts.append(fact)
        else:
            violations.append(violation(
                SCRIPT, "error", None, None, net, [],
                f"critical {kind} net {net} failed to route: {reason}",
                SCRIPT, kind="critical_route_failed", reason=reason,
                layers_used=layers, static_share=share,
                iteration_retry=bool(retry)))
    if retry and facts:
        facts[0]["krt_retry"] = retry
    return facts, violations


_SEG_BLOCK_RE = re.compile(r"\(segment\b.*?\(uuid \"([0-9a-f-]+)\"\)\s*\)",
                           re.S)
_SEG_FIELD_RES = {
    "start": re.compile(r"\(start ([-\d.]+) ([-\d.]+)\)"),
    "end": re.compile(r"\(end ([-\d.]+) ([-\d.]+)\)"),
    "width": re.compile(r"\(width ([-\d.]+)\)"),
    "layer": re.compile(r"\(layer \"([^\"]+)\"\)"),
    "net": re.compile(r"\(net \"((?:[^\"\\]|\\.)*)\"\)"),
}
CRUMB_MM = 0.05


def _widen_fits(poly, net: str, layer: str, trees: dict, added: list,
                clearance: float, keep_in) -> bool:
    """True when `poly` (a widened `net` segment on `layer`) keeps
    `clearance` from every other net's wired copper on that layer - the
    board's own (trees[layer]: STRtree + nets) and segments widened earlier
    in this pass (`added`) - and stays inside `keep_in` (the outline eroded
    by the edge clearance) when that is given."""
    if keep_in is not None and not poly.within(keep_in):
        return False
    grown = poly.buffer(clearance - 1e-4) if clearance > 0 else poly
    tree, polys, nets = trees.get(layer, (None, [], []))
    if tree is not None:
        for i in tree.query(grown):
            if nets[int(i)] != net and grown.intersects(polys[int(i)]):
                return False
    return not any(ly == layer and n != net and grown.intersects(q)
                   for ly, n, q in added)


def normalize_power_widths(staged: Path, specs: list[dict],
                           clearance: float = 0.0,
                           edge_clearance: float | None = None,
                           fence: set | None = None) -> dict:
    """Post-KRT width discipline (S11 acceptance finding): KRT can emit a few
    base-width segments and sub-grid crumbs on power nets even with
    --no-power-tap-neckdown, which violate rules_gen's aiee_pwr_width_* DRU
    floors. Raise segments below the net's IPC-2152 minimum (the DRU floor -
    the un-margined check_current/rules_gen number) to exactly that floor
    (remove + re-add via route_edit - atomic, verified) and drop crumbs
    (< 0.05 mm). Targeting the floor, not the 1.5x-margined command, keeps
    legitimate fine-pitch neckdowns of low-current nets intact.

    #957: a widen is applied only when the widened segment keeps `clearance`
    from every other net's tracks, pads and vias (unnetted pads included)
    and, with `edge_clearance`, stays that far inside the outline. KRT's
    narrow segments are pad entries between foreign pads; widening them
    blind (the voice board's +5V to 1.56 mm round its buck) shorted 18
    places and bridged mask at 40. A segment that can't take its floor is
    left as KRT drew it and listed in "blocked" - the honest track_width
    error, which the caller reports, instead of a short.
    fence: (ref, pad number) pads kept clear as if they were another net's -
    the Kelvin sense pads (#1043), which no rail segment may be widened onto.
    Returns {"widened": n, "crumbs_removed": n, "blocked": [...]}."""
    want = {s["net"]: s["ipc_min_mm"] for s in specs}
    text = staged.read_text(encoding="utf-8")
    bg = geom.BoardGeom.from_file(staged)
    keep_in = None
    if edge_clearance is not None and bg.outline is not None \
            and not bg.outline.is_empty:
        keep_in = bg.outline.buffer(-edge_clearance + 1e-4)
    trees: dict = {}
    for layer in bg.copper_layers:
        items = [(t.poly, t.net) for t in bg.tracks_of(layer=layer)]
        items += [(p.poly, SENSE_FENCE if (p.ref, p.number) in (fence or ())
                   else p.net) for p in bg.pads_of(layer=layer)]
        items += [(v.poly, v.net) for v in bg.vias_of(layer=layer)]
        if items:
            polys = [q for q, _n in items]
            trees[layer] = (STRtree(polys), polys, [n for _q, n in items])
    ops, added, blocked = [], [], []
    widened = crumbs = 0
    for m in _SEG_BLOCK_RE.finditer(text):
        block, uid = m.group(0), m.group(1)
        net_m = _SEG_FIELD_RES["net"].search(block)
        if not net_m or net_m.group(1) not in want:
            continue
        net = net_m.group(1)
        w = float(_SEG_FIELD_RES["width"].search(block).group(1))
        if w >= want[net] - 1e-3:
            continue
        sx, sy = map(float, _SEG_FIELD_RES["start"].search(block).groups())
        ex, ey = map(float, _SEG_FIELD_RES["end"].search(block).groups())
        layer = _SEG_FIELD_RES["layer"].search(block).group(1)
        if math.hypot(ex - sx, ey - sy) < CRUMB_MM:
            ops.append({"op": "remove", "uuid": uid})
            crumbs += 1
            continue
        line = LineString([(sx, sy), (ex, ey)])
        poly = line.buffer(want[net] / 2.0)
        if not _widen_fits(poly, net, layer, trees, added, clearance,
                           keep_in):
            blocked.append({"net": net, "layer": layer,
                            "start": [sx, sy], "end": [ex, ey],
                            "width_mm": w, "want_mm": want[net]})
            continue
        ops.append({"op": "remove", "uuid": uid})
        ops.append({"op": "add_track", "start": [sx, sy], "end": [ex, ey],
                    "width": want[net], "layer": layer, "net": net})
        added.append((layer, net, poly))
        widened += 1
    if ops:
        route_edit.apply_ops(staged, ops)
    return {"widened": widened, "crumbs_removed": crumbs, "blocked": blocked}


def power_net_clearances(ctx: dict, obstacle_nets: list[str]) -> Path | None:
    """--net-clearances file for the power route.py runs: the shared map with
    `obstacle_nets` (nets already carrying tracks - the diff pairs, off-grid)
    raised by grid_step / 2 over their own clearance (else the floor). KRT
    keeps an obstacle out by that obstacle net's clearance, and its
    fractional-margin shortfall (module docstring) is under half a diagonal
    grid phase, so half a grid step covers it. Only those nets are raised:
    raising the routed power nets instead cost +3V3 a fine-pitch pad tap.
    Returns the shared map unchanged when there is nothing to raise."""
    path = ctx.get("net_clearances")
    if not obstacle_nets:
        return path
    ncl = json.loads(path.read_text(encoding="utf-8")) if path else {}
    for net in obstacle_nets:
        base = max(ncl.get(net, 0.0), ctx["floors"]["clearance"])
        ncl[net] = round(base + ctx["grid_step"] / 2.0, 4)
    out = ctx["work"] / "net_clearances_power.json"
    out.write_text(json.dumps(ncl, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    return out


def route_power_item(ctx: dict, specs: list[dict]) -> tuple[list, list]:
    bg = geom.BoardGeom.from_file(ctx["staged"])
    live = [s for s in specs if s["net"] in bg.nets]
    missing = [s for s in specs if s["net"] not in bg.nets]
    # A power net that a plane carries is NOT trunk-routed on the outer
    # layers: the plane IS the trunk (golden usbbuck4 pattern - +3V3 lives on
    # In2). Outer trunk tracks + their vias fragment the plane locally (S11
    # acceptance: a routed +3V3 trunk starved J2's thermal spokes onto an
    # In2 island - error starved_thermal). PTH pads reach the plane through
    # thermal spokes; SMD pads get vias from stitch_vias (next chain step).
    plane_carried = [s for s in live
                     if any(z.filled for z in bg.zones_of(net=s["net"]))]
    live = [s for s in live if s not in plane_carried]
    violations = [violation(
        SCRIPT, "warning", None, None, s["net"], [],
        f"power net {s['net']} not on the board (stale constraints?)",
        SCRIPT, kind="critical_missing_net") for s in missing]
    plane_facts = [
        {"kind": "power", "nets": [s["net"]], "plane_carried": True,
         "current_a": s["current_a"],
         "plane_layers": sorted(
             ly for z in bg.zones_of(net=s["net"])
             for ly, polys in z.fills.items() if polys),
         "note": "plane is the trunk; SMD pads stitched by stitch_vias, "
                 "PTH pads via thermal spokes"}
        for s in plane_carried]
    if not live:
        f, v = route_sense_lines(ctx, plane_carried)
        return plane_facts + f, violations + v
    outer = _outer_layers(bg)
    costs = ["1"] if len(outer) == 1 else ["1", "2"]
    # Per-net neckdown policy (S11 acceptance finding): KRT necks power taps
    # down to the 0.2 mm base width near fine-pitch pads. That is legal only
    # for nets whose IPC-2152/DRU floor is <= 0.2 (e.g. +3V3 @0.4A -> 0.20);
    # nets with a fatter floor (+5V @0.5A -> 0.25) must route with
    # --no-power-tap-neckdown or the neck violates aiee_pwr_width_*. A pad
    # that cannot take the un-necked width then fails loudly.
    relaxed = [s for s in live if s["ipc_min_mm"] <= BASE_TRACK_WIDTH + 1e-3]
    strict = [s for s in live if s["ipc_min_mm"] > BASE_TRACK_WIDTH + 1e-3]
    power = {s["net"] for s in specs}
    ncl = power_net_clearances(ctx, sorted(
        {tk.net for tk in bg.tracks_of() if tk.net} - power))
    facts, vs = list(plane_facts), []
    # #1043: KRT sees each sense pad netless, an obstacle, so it never tees
    # the rail into it; the sense line is laid after, from pad to tap
    fence = sense_pad_targets(bg, live)
    staged = ctx["staged"]
    text, restores = detach_stub_pads(staged.read_text(encoding="utf-8"),
                                      fence)
    staged.write_text(text, encoding="utf-8")
    for group, extra in ((strict, ["--no-power-tap-neckdown"]), (relaxed, [])):
        if not group:
            continue
        nets = [s["net"] for s in group]
        widths = [f"{s['width_mm']:g}" for s in group]
        f, v = _route_single_item(
            ctx, "power", nets,
            ["--power-nets", *nets, "--power-nets-widths", *widths,
             "--track-width", f"{BASE_TRACK_WIDTH:g}", *extra,
             "--layers", *outer, "--layer-costs", *costs],
            {s["net"]: {"current_a": s["current_a"],
                        "width_mm": s["width_mm"],
                        "ipc_min_mm": s["ipc_min_mm"],
                        "neckdown": not extra} for s in group},
            ncl)
        facts.extend(f)
        vs.extend(v)
    if restores:
        staged.write_text(restore_stub_pads(
            staged.read_text(encoding="utf-8"), restores), encoding="utf-8")
    norm = normalize_power_widths(ctx["staged"], live,
                                  clearance=ctx["floors"]["clearance"],
                                  edge_clearance=ctx["floors"][
                                      "edge_clearance"],
                                  fence={(r, n) for r, _net, n in fence})
    if facts:
        facts[0]["width_normalization"] = norm
    vs.extend(power_width_blocked(norm["blocked"]))
    f, v = route_sense_lines(ctx, plane_carried + live)
    return facts + f, violations + vs + v


def power_width_blocked(blocked: list[dict]) -> list[dict]:
    """One error per power net with segments normalize_power_widths could
    not raise to the net's floor without breaking clearance (#957)."""
    by_net: dict[str, list[dict]] = {}
    for b in blocked:
        by_net.setdefault(b["net"], []).append(b)
    out = []
    for net, segs in sorted(by_net.items()):
        first = segs[0]
        out.append(violation(
            SCRIPT, "error", first["start"], first["layer"], net, [],
            f"power net {net}: {len(segs)} segment(s) below its "
            f"{first['want_mm']} mm floor could not be widened without "
            "breaking clearance to other nets' copper - left narrow; "
            "pour fan-in or a neck rule at those pads "
            "(reference/remediations/track_width.md step 4)",
            SCRIPT, kind="power_width_blocked", segments=segs[:20],
            want_mm=first["want_mm"]))
    return out


SENSE_FENCE = "~sense"      # a net name no board carries (#1043)
SENSE_PREFIX = "aiee_sense"  # route_critical's sense-line pour keepouts
SENSE_TAP_KEEP_MM = 0.4      # the pour may still meet the line this near its
#                              tap (under check_kelvin.SENSE_SLACK_MM)
SENSE_TAP_SHARE_MM = 0.45    # rail copper this near the tap pad is the tap
SENSE_MARK = "aiee_sensew"   # rules_gen's sense-width mark (#1042); the
#                              set_rule_areas prefix SENSE_PREFIX covers it
SENSE_MARK_MM = 0.02         # the mark's margin round the line's copper:
#                              wide enough for KiCad's arc error, too thin
#                              to enclose any wider (rail) track


def sense_width(pro_path: Path | None) -> float:
    """The width a sense line is laid at (#1042): the board's Default
    netclass track width, never under its min_track_width. A feedback
    divider or SENSE pin draws microamps, so the rail's IPC width does not
    apply; the regulator notes ask for a short, thin trace (TI SNVA721A
    section 2.5; knowledge record buck-fb-route-rules)."""
    floors = grading_floors(pro_path)
    w = None
    if pro_path is not None and Path(pro_path).is_file():
        try:
            proj = json.loads(Path(pro_path).read_text(encoding="utf-8"))
            for c in (proj.get("net_settings", {}).get("classes") or []):
                if c.get("name") == "Default" \
                        and isinstance(c.get("track_width"), (int, float)):
                    w = float(c["track_width"])
        except (OSError, json.JSONDecodeError):
            pass
    return max(w or floors["track_width"], floors["track_width"])


def sense_pairs(specs: list[dict]) -> list[tuple[str, str, str]]:
    """(net, sense pad, tap pad) for each declared sense line (#936)."""
    return [(s["net"], sense, tap) for s in specs
            for sense, tap in sorted((s.get("sense_pads") or {}).items())]


def sense_pad_targets(bg: geom.BoardGeom, specs: list[dict]
                      ) -> list[tuple[str, str, str]]:
    """detach_stub_pads targets (ref, net, number): each declared sense pad
    that is on its rail and has no copper yet. A bad name is skipped here;
    check_kelvin reports it (sense_tap_bad)."""
    out = []
    for net, s_name, t_name in sense_pairs(specs):
        sense = check_kelvin._pad_named(bg, s_name)
        tap = check_kelvin._pad_named(bg, t_name)
        if sense is None or tap is None or sense is tap \
                or sense.net != net or tap.net != net \
                or _pad_copper(bg, sense):
            continue
        out.append((sense.ref, net, sense.number))
    return sorted(set(out))


def _pad_copper(bg: geom.BoardGeom, pad) -> bool:
    """True when a track or via of the pad's net already touches it."""
    for ly in pad.layers:
        for t in bg.tracks_of(net=pad.net, layer=ly):
            if t.poly.intersects(pad.poly):
                return True
    return any(v.poly.intersects(pad.poly) for v in bg.vias_of(net=pad.net)
               if set(v.layers) & set(pad.layers))


def fence_rail(router, net: str, sense, tap) -> int:
    """Make every piece of `net`'s copper a wall for the sense line (#1043),
    except its two ends and the rail copper within SENSE_TAP_SHARE_MM of the
    tap pad (the rail lands on the tap too, and the line may meet it there:
    that is the tap). A track that crosses that ring is split: the near piece
    stays the rail's, the rest becomes a wall. Pours stay the rail's at the
    router's pour cost; route_sense_lines keeps them off the line after.
    Returns the number of items fenced."""
    fenced = 0
    for it in router.obs.items:
        if it.net != net:
            continue
        if it.ref and (it.shape.equals(sense.poly)
                       or it.shape.equals(tap.poly)):
            continue
        near = tap.poly.buffer(max(0.0, SENSE_TAP_SHARE_MM - it.half))
        if it.half > 0 and it.shape.intersects(near):
            rest = it.shape.difference(near)
            if rest.is_empty:
                continue            # all of it is at the tap
            router.obs.added.append(dataclasses.replace(
                it, shape=it.shape.intersection(near)))
            it.shape = rest
        it.net = SENSE_FENCE
        fenced += 1
    return fenced


def sense_keepout(bg: geom.BoardGeom, net: str, new_uuids: set[str], sense,
                  tap, clearance: float) -> list[dict]:
    """aiee_sense_* rule areas (pours not allowed) round a laid sense line:
    its new tracks and vias and its sense pad, grown by clearance, less the
    ring round its tap pad where the pour meets the rail anyway. Also the
    line's aiee_sensew_* marks (#1042): its new tracks grown by
    SENSE_MARK_MM, nothing disallowed, for rules_gen's sense width rule."""
    by_layer: dict[str, list] = {}
    for t in bg.tracks_of(net=net):
        if t.uuid in new_uuids:
            by_layer.setdefault(t.layer, []).append(t.poly.buffer(clearance))
    for v in bg.vias_of(net=net):
        if v.uuid in new_uuids:
            for ly in v.layers:
                if ly in bg.copper_layers:
                    by_layer.setdefault(ly, []).append(
                        v.poly.buffer(clearance))
    for ly in sense.layers:
        if ly in bg.copper_layers:
            by_layer.setdefault(ly, []).append(sense.poly.buffer(clearance))
    cut = tap.poly.buffer(SENSE_TAP_KEEP_MM)
    areas = []
    tag = re.sub(r"[^A-Za-z0-9]+", "_", f"{sense.ref}_{sense.number}")
    # the sense-width mark (#1042): the line's own tracks, tap end and all,
    # grown by SENSE_MARK_MM; KiCad's enclosedByArea takes no wider track
    marks: dict[str, list] = {}
    for t in bg.tracks_of(net=net):
        if t.uuid in new_uuids:
            marks.setdefault(t.layer, []).append(t.poly.buffer(SENSE_MARK_MM))
    for ly in sorted(marks):
        shape = unary_union(marks[ly])
        for k, poly in enumerate(q for q in getattr(shape, "geoms", [shape])
                                 if q.geom_type == "Polygon"
                                 and not q.is_empty):
            pts = [[round(x, 4), round(y, 4)] for x, y in list(
                poly.simplify(SENSE_MARK_MM / 10).exterior.coords)[:-1]]
            areas.append({"name": f"{SENSE_MARK}_{tag}_{ly.replace('.', '_')}"
                                  f"_{k}", "layers": [ly], "poly": pts,
                          "tracks": False, "vias": False, "pours": False})
    for ly in sorted(by_layer):
        shape = unary_union(by_layer[ly]).difference(cut)
        polys = getattr(shape, "geoms", [shape])
        for k, poly in enumerate(q for q in polys
                                 if q.geom_type == "Polygon" and not q.is_empty):
            pts = [[round(x, 4), round(y, 4)]
                   for x, y in list(poly.simplify(0.005).exterior.coords)[:-1]]
            areas.append({"name": f"{SENSE_PREFIX}_{tag}_{k}", "layers": [ly],
                          "poly": pts, "tracks": False, "vias": False,
                          "pours": True})
    return areas


def _set_sense_keepouts(staged: Path, areas: list[dict]) -> dict:
    cli = env.find_kicad_cli()
    bp = env.find_kicad_python(cli) if cli else None
    if bp is None:
        raise CheckError("KiCad bundled python not found (env.py)")
    stage = Path(tempfile.mkdtemp(prefix=".aiee_sense_", dir=staged.parent))
    try:
        return routelib.run_worker(
            bp, {"verb": "set_rule_areas", "board": str(staged),
                 "out": str(staged), "prefix": SENSE_PREFIX,
                 "areas": areas}, stage)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def route_sense_lines(ctx: dict, specs: list[dict]) -> tuple[list, list]:
    """Lay each declared sense line (#1043): from its sense pad to its Kelvin
    tap pad on a track of its own (mazeroute; fence_rail walls off the rest
    of the rail), then keep the pour off it (sense_keepout). A sense pad that
    already has copper is left as it is (pre_routed): sense_verdicts judges
    it with the rest after the final refill."""
    staged = ctx["staged"]
    facts, vios, areas = [], [], []
    for net, s_name, t_name in sense_pairs(specs):
        bg = geom.BoardGeom.from_file(staged)
        sense = check_kelvin._pad_named(bg, s_name)
        tap = check_kelvin._pad_named(bg, t_name)
        fact = {"kind": "sense", "nets": [net], "sense": s_name,
                "tap": t_name}
        if sense is None or tap is None or sense is tap \
                or sense.net != net or tap.net != net:
            continue        # check_kelvin's sense_tap_bad names it
        if _pad_copper(bg, sense):
            facts.append({**fact, "pre_routed": True})
            continue
        before = {t.uuid for t in bg.tracks_of(net=net)} \
            | {v.uuid for v in bg.vias_of(net=net)}
        try:
            router = mazeroute.Router(bg)
            fence_rail(router, net, sense, tap)
            ops = router.route(net, s_name, t_name,
                               ctx.get("sense_width") or sense_width(None),
                               pull=True)
            route_edit.apply_ops(staged, ops)
        except CheckError as exc:
            vios.append(violation(
                SCRIPT, "error", [checklib.rnd(c, 2) for c in sense.center],
                None, net, sorted({sense.ref, tap.ref}),
                f"sense line {s_name} -> {t_name} on {net} could not be laid "
                f"on copper of its own: {str(exc)[:200]}. Make room for it "
                f"from {s_name} to {t_name}, or route it by hand",
                SCRIPT, kind="sense_route_failed"))
            continue
        bg = geom.BoardGeom.from_file(staged)
        new = ({t.uuid for t in bg.tracks_of(net=net)}
               | {v.uuid for v in bg.vias_of(net=net)}) - before
        areas += sense_keepout(bg, net, new, sense, tap,
                               ctx["floors"]["clearance"])
        facts.append({**fact, "tracks": sum(o["op"] == "add_track"
                                            for o in ops),
                      "vias": sum(o["op"] != "add_track" for o in ops)})
    if areas:
        _set_sense_keepouts(staged, areas)
    return facts, vios


def sense_verdicts(bg: geom.BoardGeom, facts: list[dict]) -> list[dict]:
    """Walk each sense line route_critical laid (#1043) on the refilled
    board: one error, sense_route_off_pad, for a line that joins rail copper
    before its tap pad or never reaches it."""
    out = []
    for f in facts:
        if f.get("kind") != "sense" or f.get("pre_routed"):
            continue
        sense = check_kelvin._pad_named(bg, f["sense"])
        tap = check_kelvin._pad_named(bg, f["tap"])
        if sense is None or tap is None:
            continue
        w = check_kelvin.sense_walk(bg, sense, tap)
        f["joins"], f["reached"] = len(w["joins"]), w["reached"]
        if w["joins"] or not w["reached"]:
            at = (w["joins"][0]["at"] if w["joins"]
                  else [checklib.rnd(c, 2) for c in sense.center])
            what = ("joins " + ", ".join(j["label"] for j in w["joins"][:3])
                    if w["joins"] else "never reaches it")
            out.append(violation(
                SCRIPT, "error", at, None, f["nets"][0],
                sorted({sense.ref, tap.ref}),
                f"sense line {f['sense']} -> {f['tap']} on {f['nets'][0]} "
                f"{what} before its tap pad: it reads the rail there, not "
                f"at {f['tap']}", SCRIPT, kind="sense_route_off_pad",
                joins=len(w["joins"])))
    return out


def route_rf_item(ctx: dict, specs: list[dict]) -> tuple[list, list]:
    bg = geom.BoardGeom.from_file(ctx["staged"])
    live = [s for s in specs if s["net"] in bg.nets]
    missing = [s for s in specs if s["net"] not in bg.nets]
    violations = [violation(
        SCRIPT, "warning", None, None, s["net"], [],
        f"rf net {s['net']} not on the board (stale constraints?)",
        SCRIPT, kind="critical_missing_net") for s in missing]
    facts = []
    outer = _outer_layers(bg)
    costs = ["1"] if len(outer) == 1 else ["1", "3"]
    for s in live:
        w = rf_width(bg, s["impedance_ohm"])
        # #489: inner layers rules_gen sized for this net (stripline), each
        # drawn at its own DRU width
        inner = {ly: lw for ly, lw in zrules.layer_widths(
            ctx.get("zgeo") or {}, [s["net"]]).items()
            if ly in bg.copper_layers[1:-1]}
        layers, lcosts, widths = outer, costs, None
        if inner:
            layers = [outer[0], *sorted(inner)] + outer[1:]
            lcosts = ["1"] + [STRIPLINE_COST] * len(inner) + costs[1:]
            widths = {**{ly: w for ly in outer}, **inner}
        meta = {"impedance_ohm": s["impedance_ohm"], "width_mm": w,
                "fence_handoff": f"stitch_vias.py --fence-net {s['net']}"}
        if widths:
            meta["layer_widths"] = widths
        f, vs = _route_single_item(
            ctx, "rf", [s["net"]],
            ["--track-width", f"{w:g}", "--layers", *layers,
             "--layer-costs", *lcosts],
            {s["net"]: meta}, layer_widths=widths)
        facts.extend(f)
        violations.extend(vs)
    return facts, violations


def route_bus_item(ctx: dict, specs: list[dict], impedance: dict[str, float]
                   ) -> tuple[list, list]:
    """Every length group's nets, in one KRT --bus run (#546): KRT detects the
    groups from the clustered stub tips, plans a corridor per group, routes
    the members side by side and rips up among them. Input copper is
    read-only, so a net that fails keeps its escape stubs. Width: each layer's
    own for the group's impedance, when the nets have one. route_tune trims
    the lengths afterwards."""
    bg = geom.BoardGeom.from_file(ctx["staged"])
    nets = [n for s in specs for n in s["nets"]]
    outer = _outer_layers(bg)
    inner = {ly: lw for ly, lw in zrules.layer_widths(
        ctx.get("zgeo") or {}, nets).items() if ly in bg.copper_layers[1:-1]}
    zs = {impedance[n] for n in nets if n in impedance}
    w = rf_width(bg, zs.pop()) if len(zs) == 1 else BASE_TRACK_WIDTH
    w = max(w, ctx["floors"]["track_width"])
    layers = [outer[0], *sorted(inner)] + outer[1:]
    group_of = {n: s["name"] for s in specs for n in s["nets"]}
    meta = {n: {"group": group_of[n], "width_mm": w,
                **({"layer_widths": {**{ly: w for ly in outer}, **inner}} if inner else {})}
            for n in nets}
    widths = {**{ly: w for ly in outer}, **inner} if inner else None
    return _route_single_item(
        {**ctx, "timeout_s": max(ctx["timeout_s"], BUS_TIMEOUT_S)}, "bus", nets,
        ["--track-width", f"{w:g}", "--layers", *layers,
         "--bus", "--ordering", "bus", "--keep-input-copper"],
        meta, layer_widths=widths)


# ------------------------------------------------------------ verification

def drc_by_check(report: dict, severity: str | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for v in report["violations"]:
        if severity is not None and v.get("severity") != severity:
            continue
        counts[v["check"]] = counts.get(v["check"], 0) + 1
    return counts


def drc_delta(before: dict, after: dict) -> dict:
    """Per-check-type count changes. 'new' = ERROR-severity types that
    INCREASED (the rollback trigger); 'new_warnings' = increased warning
    types, reported only - post-route hygiene (dangling crumbs etc.) is
    route_cleanup's job later in the P7 chain, not a reason to discard a
    good critical route."""
    b, a = drc_by_check(before), drc_by_check(after)
    be = drc_by_check(before, "error")
    ae = drc_by_check(after, "error")
    new = {k: c - be.get(k, 0) for k, c in ae.items() if c > be.get(k, 0)}
    bw = drc_by_check(before, "warning")
    aw = drc_by_check(after, "warning")
    new_w = {k: c - bw.get(k, 0) for k, c in aw.items() if c > bw.get(k, 0)}
    return {"before": b, "after": a, "new": new, "new_warnings": new_w}


_ITEM_NET_RE = re.compile(r"^(?:Track|Via|Arc) \[([^\]]+)\]")


def nets_in_new_errors(before: dict, after: dict, nets: set[str]) -> set[str]:
    """The routed nets whose tracks or vias are items of an ERROR whose
    check type grew between the two DRC reports - the nets whose new copper
    broke the board (lite-main, hifi-receiver-2: KRT laid /OUT_L_N and
    /OUT_R_* at their 1.5x width across the TPA3118's 0.65 mm-pitch pins,
    41 shorting_items, and the whole run rolled back)."""
    grown = set(drc_delta(before, after)["new"])
    out: set[str] = set()
    for v in after["violations"]:
        if v.get("severity") != "error" or v.get("check") not in grown:
            continue
        for item in v.get("items", []):
            m = _ITEM_NET_RE.match(item.get("msg", ""))
            if m and m.group(1) in nets:
                out.add(m.group(1))
    return out


_STARVED_PAD_RE = re.compile(r"^Pad \S+ \[[^\]]*\] of (\S+) on (\S+)")


def nets_starving_thermals(before: dict, after: dict, nets: set[str],
                           staged: Path, old_uuids: set[str],
                           margin_mm: float = 0.5) -> set[str]:
    """The routed nets whose NEW copper starved a plane pad's thermal spokes.
    A starved_thermal error names only the zone and the pad (both on the
    plane net), never the track that cut the spoke, so nets_in_new_errors
    cannot rip it and the whole run rolls back (haos-voice pass 6:
    /OSPI0_D1 and /OSPI0_D5 on B.Cu past C336/C342's GND pads under the
    AM62 cut one of two spokes each). Named here: each routed net with a
    new track on the pad's layer, or a new via, within the pad's half
    extent + `margin_mm` of the pad centre."""
    if "starved_thermal" not in set(drc_delta(before, after)["new"]):
        return set()
    spots = []
    for v in after["violations"]:
        if v.get("check") != "starved_thermal" or v.get("severity") != "error":
            continue
        for item in v.get("items", []):
            m = _STARVED_PAD_RE.match(item.get("msg", ""))
            if m and item.get("pos"):
                spots.append((m.group(2), tuple(item["pos"])))
    if not spots:
        return set()
    bg = geom.BoardGeom.from_file(staged)
    pads = list(bg.pads_of())
    new = [x for x in list(bg.tracks_of()) + list(bg.vias_of())
           if x.net in nets and x.uuid and x.uuid not in old_uuids]
    out: set[str] = set()
    for layer, pos in spots:
        half = 0.0
        for p in pads:
            if math.dist(p.center, pos) < 1e-3:
                half = max(p.size) / 2.0
                break
        reach = half + margin_mm
        pt = Point(pos)
        for x in new:
            # a via counts on any layer: its pad or barrel sits in the pour
            on = getattr(x, "layer", layer) == layer
            if on and x.poly.distance(pt) <= reach:
                out.add(x.net)
    return out


def rip_new_copper(staged: Path, old_uuids: set[str], nets: set[str]) -> int:
    """Remove every track/via of `nets` that was not on the board before
    this run (uuid not in old_uuids). Returns the items removed."""
    bg = geom.BoardGeom.from_file(staged)
    ops = [{"op": "remove", "uuid": x.uuid}
           for x in list(bg.tracks_of()) + list(bg.vias_of())
           if x.net in nets and x.uuid and x.uuid not in old_uuids]
    if ops:
        route_edit.apply_ops(staged, ops)
    return len(ops)


def _new_copper_nets(pcb: Path, old_uuids: set[str]) -> set[str]:
    """The nets with a track or via that was not on the board before."""
    bg = geom.BoardGeom.from_file(pcb)
    return {x.net for x in list(bg.tracks_of()) + list(bg.vias_of())
            if x.net and x.uuid and x.uuid not in old_uuids}


def _copper_uuids(pcb: Path) -> set[str]:
    bg = geom.BoardGeom.from_file(pcb)
    return {x.uuid for x in list(bg.tracks_of()) + list(bg.vias_of())
            if x.uuid}


def diffpair_postcheck(staged: Path, routed_pairs: list[dict]) -> tuple[list, list]:
    """S5 check_diffpair on the routed pairs only. Returns (violations, facts)."""
    if not routed_pairs:
        return [], []
    bg = geom.BoardGeom.from_file(staged)
    violations, checked = [], []
    for fact in routed_pairs:
        p, n = fact["nets"]
        spec = {"p": p, "n": n,
                "gap_mm": round(fact["width_mm"] + fact["gap_mm"], 4)}
        for k in ("max_skew_mm", "max_uncoupled_mm", "coupling_factor"):
            if k in fact.get("constraint", {}):
                spec[k] = fact["constraint"][k]
        vs, cf = check_diffpair.check_pair(bg, spec)
        violations.extend(vs)
        checked.append(cf)
    return violations, checked


# ------------------------------------------------------------ CLI

def _sidecar(pcb: Path, explicit: str | None, name: str) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise CheckError(f"{name} not found: {p}")
        return p
    cand = pcb.parent / name
    return cand if cand.is_file() else None


def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints", default=None,
                    help="constraints.json (default: next to the board)")
    ap.add_argument("--only", choices=list(ALL_KINDS), default=None)
    ap.add_argument("--plan", default=None,
                    help='plan.json {"order": ["diff","rf","power"]}')
    ap.add_argument("--pad-window", action="store_true",
                    help="no-routing probe: widest connectable track per "
                         "pad of each power net vs its DRU width floor "
                         "(exit 1 when a floor is geometrically unmeetable)")
    ap.add_argument("--nets", default=None,
                    help="--pad-window net list (comma; default: "
                         "constraints['power'] nets)")
    ap.add_argument("--grid-step", type=float, default=0.05,
                    help="KRT routing grid (mm); 0.05 keeps 0.5 mm-pitch tap "
                         "corridors routable")
    ap.add_argument("--timeout-s", type=int, default=600,
                    help="per-KRT-invocation process timeout")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)

    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    cons_path = _sidecar(pcb, args.constraints, "constraints.json")
    constraints = checklib.load_json(cons_path, "constraints") if cons_path else {}
    if args.pad_window:   # pure geometry probe: no KRT, board untouched
        return pad_window_report(pcb, constraints, args.nets), args.out_report
    plan = checklib.load_json(args.plan, "plan") if args.plan else None
    order = plan_order(args.only, plan)

    bg0 = geom.BoardGeom.from_file(pcb)
    cu_outer = bg0.stackup.copper_thickness.get(bg0.copper_layers[0], 0.035)

    sense_pairs: list = []
    d_specs = diff_specs(constraints, bg0.nets, sense=sense_pairs)
    pair_nets = {x for s in d_specs for x in (s["p"], s["n"])}
    r_specs = rf_specs(constraints, pair_nets)
    p_specs = [s for s in power_specs(constraints, cu_outer)
               if s["net"] not in pair_nets]
    by_kind = {"diff": d_specs, "rf": r_specs, "power": p_specs,
               "bus": bus_specs(constraints, bg0.nets, pair_nets)}
    todo = [(k, by_kind[k]) for k in order if by_kind[k]]
    skipped = [{"kind": k, "count": len(by_kind[k]), "why": "not in --only/plan"}
               for k in DEFAULT_ORDER if by_kind[k] and k not in order]  # bus is opt-in
    if sense_pairs:
        skipped.append({"kind": "diff", "count": len(sense_pairs),
                        "pairs": sense_pairs,
                        "why": "sense / low-frequency pair: routed for "
                               "symmetry, not coupled to an impedance"})

    if not todo:
        payload = checklib.report(SCRIPT, str(pcb), [], facts={
            "routed": [], "skipped": skipped, "board_updated": False,
            "note": "no critical nets declared or selected"})
        return payload, args.out_report

    cli, krt = _tools()
    # .resolve(): KRT subprocesses run with cwd=<plugins dir>, so a relative
    # work dir makes KRT die with "fab-overrides file not found" -> "no
    # JSON_SUMMARY" (LEARNINGS 1318, live on lumina-carrier).
    work = (Path(args.work_dir).resolve() if args.work_dir
            else pcb.parent / "route_critical")
    routelib.fresh_work_dir(work)
    staged = _stage_board(pcb, work)
    floors = grading_floors(work / (pcb.stem + ".kicad_pro"))
    ctx = {"staged": staged, "work": work, "krt": krt, "floors": floors,
           "fab_file": write_fab_overrides(work, floors),
           "grid_step": args.grid_step, "timeout_s": args.timeout_s,
           # #489: rules_gen's per-layer (stripline) impedance widths
           "zgeo": zrules.board_layer_geometry(staged),
           # #1042: a sense line is laid thin, not at its rail's width
           "sense_width": sense_width(work / (pcb.stem + ".kicad_pro"))}
    # Per-net clearances for KRT (T6, LEARNINGS 1522): --clearance is a CAP
    # on KRT's auto-read netclass map and KRT cannot read a .kicad_dru, so
    # HV clearance must travel in an explicit --net-clearances file.
    ncl = build_net_clearances(work / (pcb.stem + ".kicad_pro"),
                               work / (pcb.stem + ".kicad_dru"), bg0.nets)
    if ncl:
        ncl_file = work / "net_clearances.json"
        ncl_file.write_text(json.dumps(ncl, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        ctx["net_clearances"] = ncl_file

    # fresh fills before the baseline so stale pours don't skew the DRC delta
    if bg0.zones_of():
        kc.run_drc(cli, staged, refill=True, save_board=True)
    before = kc.run_drc(cli, staged, all_track_errors=True)
    old_uuids = _copper_uuids(staged)
    baseline = work / "baseline.kicad_pcb"
    shutil.copy2(staged, baseline)

    routed, violations = [], []
    for kind, specs in todo:
        if kind == "diff":
            for spec in specs:
                fact, v = route_diff_item(ctx, spec)
                if fact:
                    for k in ("max_skew_mm", "max_uncoupled_mm", "coupling_factor"):
                        if k in spec:
                            fact.setdefault("constraint", {})[k] = spec[k]
                    routed.append(fact)
                if v:
                    violations.append(v)
        elif kind == "power":
            facts, vs = route_power_item(ctx, specs)
            routed.extend(facts)
            violations.extend(vs)
        elif kind == "rf":
            facts, vs = route_rf_item(ctx, specs)
            routed.extend(facts)
            violations.extend(vs)
        elif kind == "bus":
            z = {}
            for h in constraints.get("high_speed") or []:
                if h.get("net") and h.get("impedance_ohm"):
                    z[h["net"]] = float(h["impedance_ohm"])
            facts, vs = route_bus_item(ctx, specs, z)
            routed.extend(facts)
            violations.extend(vs)

    # verification on the staged result, before the original is touched
    if bg0.zones_of():
        kc.run_drc(cli, staged, refill=True, save_board=True)
    after = kc.run_drc(cli, staged, all_track_errors=True)
    delta = drc_delta(before, after)
    ripped = None
    if delta["new"]:
        # keep what routed clean: rip only the nets whose new copper is in a
        # new error, and re-check once
        # a net that failed part-way keeps its partial copper (route.py
        # keeps partial successes) but has no fact: name it by its new
        # copper (haos-voice pass 6: a failed +3V3 kept a 0.96 mm track
        # across U202's 0.4 mm-pitch pins, 8 shorting_items, unrippable)
        mine = {n for f in routed if not f.get("plane_carried")
                for n in f["nets"]} | _new_copper_nets(staged, old_uuids)
        bad = nets_in_new_errors(before, after, mine) | \
            nets_starving_thermals(before, after, mine, staged, old_uuids)
        if bad:
            # half a pair is no use: a rolled-back net takes its partner
            for f in routed:
                if f["kind"] == "diff" and set(f["nets"]) & bad:
                    bad |= set(f["nets"])
            ripped = {"nets": sorted(bad), "new_errors": delta["new"],
                      "items_removed": rip_new_copper(staged, old_uuids, bad)}
            # a launched pair's old stubs are gone: put them back
            back = sorted(bad & ctx.get("launched", set()))
            if back:
                route_edit.apply_ops(staged, [
                    {"op": "rip", "net": back},
                    {"op": "copy_from", "pcb": str(baseline), "net": back}])
            if bg0.zones_of():
                kc.run_drc(cli, staged, refill=True, save_board=True)
            after = kc.run_drc(cli, staged, all_track_errors=True)
            delta = drc_delta(before, after)
            delta["ripped"] = ripped
            routed = [f for f in routed if not set(f["nets"]) & bad]
            for net in sorted(bad):
                violations.append(violation(
                    SCRIPT, "error", None, None, net, [],
                    f"critical net {net} routed into new DRC errors "
                    f"({', '.join(sorted(ripped['new_errors']))}); its new "
                    "copper was removed and the rest kept", SCRIPT,
                    kind="critical_route_rolled_back"))
    if delta["new"]:
        raise CheckError(
            "routing introduced new DRC errors (rolled back, original board "
            f"untouched): {delta['new']}; work dir kept at {work}")

    dp_viol, dp_facts = diffpair_postcheck(
        staged, [f for f in routed if f["kind"] == "diff"])
    violations.extend(dp_viol)
    if any(f["kind"] == "sense" for f in routed):
        violations.extend(sense_verdicts(geom.BoardGeom.from_file(staged),
                                         routed))

    board_updated = bool(routed)
    if board_updated:
        routelib.swap_in(staged, pcb)

    payload = checklib.report(SCRIPT, str(pcb), violations, facts={
        "routed": routed,
        "skipped": skipped,
        "drc_delta": delta,
        "diffpair_check": "pass" if not dp_viol else "violations",
        "diffpair_facts": dp_facts,
        "floors": floors,
        "net_clearances": ncl,
        "board_updated": board_updated,
        "work_dir": str(work),
    })
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
