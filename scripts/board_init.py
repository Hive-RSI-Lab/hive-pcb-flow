#!/usr/bin/env python
"""board_init.py - initialize a .kicad_pcb from a netlist (SPEC P5).

board-setup phase, script-driven: create the board, import the netlist
(footprints + net assignments), set the stackup from stackups.yaml, draw a
provisional outline and mounting holes. Placement proper is P6 - board_init
just spreads parts legally (no courtyard overlaps) so DRC setup is clean.

kicad-cli has NO netlist->board path, so the import runs under KiCad's bundled
python via lib/board_swig.py (the only interpreter with pcbnew), mirroring the
corpus builder. The venv driver here parses the netlist (sexpdata), picks the
stackup, drives the worker, injects the (stackup) block as text (SWIG can't
serialize it - LEARNINGS [swig]), writes a minimal .kicad_pro, and self-checks.

The .kicad_pro's design-rule minimums come from the selected JLC capability
profile via lib/fabfloors.py - never from a hard-coded default - and the
checks that enforce them are pinned to severity ERROR. Two shipped boards
proved why: a hard-coded `min_track_width: 0.1` (below EVERY JLC profile)
plus KiCad's default `min_hole_to_hole: 0.25` at *warning* let 189 unbuildable
traces and two sub-fab drill pairs pass `drc_routed` 0/0 and only surface at
the P9 DFM gate (LEARNINGS [board_init][rules_gen][dfm][gates]).

A new board looks finished by default: corners rounded (AUTO_CORNER_RADIUS,
capped at a tenth of the shorter side) and, once the shorter side reaches
AUTO_HOLES_MIN_SIDE, four M3 mounting holes that are plated with an exposed
copper ring and a ring of vias (MountingHole_3.2mm_M3_Pad_Via), joined to the
ground net. A footprint whose own drills sit closer than the fab profile's
hole-to-hole floor (KiCad's M2 / M2.5 _Pad_Via via rings, 0.35 / 0.425 mm vs
0.5) is swapped for its _Pad sibling or refused. A fixed --outline is not
widened for them, so there "auto" gives none when the packed parts reach
closer to an edge than a corner hole needs, and the report's
mounting_holes_skipped says so. A brief that wants none, a bare NPTH hole or another size says so
with --mounting-holes 0 / --mounting-hole-fp / --corner-radius 0, and wins.

The outline's top-left corner stays where the board already has it: a re-run
over an existing board keeps its origin (--origin keep, the default), so
absolute coordinates written against the first run (constraints planes,
pre-route ops) still land. Without it the origin follows the parts' packed
extent and moved on every netlist change: a VBUS stub landed 0.3 mm off and
shorted a GND pad (hifi-receiver-2 lite-main). --origin X,Y pins it; --origin
auto is the packed position. The report gives it as outline_origin and says
where it came from (origin_source). Without --origin the board lands in the
positive quadrant (a wide fixed outline centred on the packed parts used to start at negative X,
cutting PDF plots off). The aux (drill/place) and grid origin sit at the
outline's lower-left corner.

The outline is PROVISIONAL. Under a build mode whose binding makes geometry an
output (`state.py mode`, reference/build-modes.md) a fixed `--outline WxH` is
built as asked and the report says the size did not come from the placement
(`build_mode.fixed_outline_override`, a worker note): the usual route is
`--outline auto`, place, then `board_edit --outline fit`.

A footprint listed in reference/footprint_waivers.yaml (#813: the MC-311D
USB-C's pads sit 0.18 mm from its own pegs, under KiCad's 0.25 mm default)
gets its cited floor merged into the board's .kicad_dru before the setup DRC,
scoped to that footprint's own pads and pegs (lib/fpwaivers.py).

Self-check acceptance (SPEC S8): schematic parity == 0 (every part+net imported
correctly) AND zero setup DRC violations, EXCLUDING unconnected_items (the board
is unrouted by design), AND the written project's floors >= the fab profile.
Emits the normalized DRC report alongside.

Usage:
  board_init.py --netlist n.net --name board --out dir --layers 4|6|8|..32
                [--copper-oz 1] [--stackup NAME]
                [--outline auto|WxH] [--origin keep|auto|X,Y]
                [--mounting-holes auto|N]
                [--mounting-hole-fp LIB:NAME]
                [--corner-radius auto|R] [--cutout X,Y,W,H ...]
                [--workspace DIR] [--allow-fixed-outline]
                [--schematic s.kicad_sch]   # copy next to board -> enables parity
                [--fp-lib DIR ...] [--out-report r.json]

I/O: SPEC section 6 - argparse, JSON to stdout or --out-report, exit 0/1/2.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

import sexpdata
import yaml

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
from lib import env  # noqa: E402
from lib import fabfloors  # noqa: E402
from lib import fpwaivers  # noqa: E402
import fabmaterial  # noqa: E402
from lib import statelib  # noqa: E402
import kc  # noqa: E402

REFERENCE = SCRIPTS.parent / "reference"
STACKUP_FILE = REFERENCE / "stackups.yaml"
WORKER = SCRIPTS / "lib" / "board_swig.py"
DEFAULT_HOLE_FP = "MountingHole:MountingHole_3.2mm_M3_Pad_Via"


def board_swig_defaults() -> tuple[str, str]:
    """(AUTO_CORNER_RADIUS, AUTO_HOLES_MIN_SIDE) as the worker defines them -
    read from its source because it imports pcbnew, which this venv lacks."""
    txt = WORKER.read_text(encoding="utf-8")
    return tuple(re.search(rf"^{k} = ([\d.]+)", txt, re.M).group(1)
                 for k in ("AUTO_CORNER_RADIUS", "AUTO_HOLES_MIN_SIDE"))


# --------------------------------------------------------------- netlist parse

def _sym(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x


def _head(node):
    return _sym(node[0]) if isinstance(node, list) and node else None


def _find(node, name):
    return [c for c in node if isinstance(c, list) and _head(c) == name]


def _find1(node, name):
    r = _find(node, name)
    return r[0] if r else None


# Symbol fields KiCad's footprint/symbol field parity handles natively (or
# exempts); everything else (LCSC, MPN, ...) must be copied onto the footprint
# or `drc --schematic-parity` warns footprint_symbol_field_mismatch per part.
_NATIVE_FIELDS = {"Reference", "Value", "Footprint", "Datasheet", "Description"}


# Of those natives, the two KiCad 10.0.6's parity compares with the
# footprint's own text (measured: a footprint Description or Datasheet the
# symbol lacks gives footprint_symbol_field_mismatch). A library footprint
# brings its own, so the symbol's, empty included, must replace it (#1357)
PARITY_NATIVES = ("Description", "Datasheet")


def _all_fields(comp) -> dict[str, str]:
    """Every symbol field of a netlist (comp ...): {name: value}; a
    valueless `(field (name "Description"))` is ""."""
    fields: dict[str, str] = {}
    fnode = _find1(comp, "fields")
    if fnode:
        for f in _find(fnode, "field"):
            nnode = _find1(f, "name")
            name = _sym(nnode[1]) if nnode and len(nnode) > 1 else None
            value = _sym(f[-1]) if len(f) > 2 else ""
            if name:
                fields[name] = value
    return fields


def _comp_fields(comp) -> dict[str, str]:
    """Custom symbol fields of a netlist (comp ...): {name: value}."""
    return {k: v for k, v in _all_fields(comp).items()
            if k not in _NATIVE_FIELDS}


def _comp_natives(comp) -> dict[str, str]:
    """The symbol's PARITY_NATIVES the netlist carries: {name: value}. A
    netlist without them (a hand-written one) gives {}: leave the
    footprint's as they are."""
    return {k: v for k, v in _all_fields(comp).items()
            if k in PARITY_NATIVES}


def _comp_sheet(comp) -> dict:
    """The footprint's place in the hierarchy, as KiCad's "Update PCB from
    Schematic" writes it (#563): path "/<sheet uuid>.../<symbol uuid>"
    (root uuid left out), sheetname the human sheet path ("/ch2/", "/" on
    the root), sheetfile the sheet's file. {} for a netlist without
    tstamps (hand-written test netlists)."""
    ts = _find1(comp, "tstamps")
    if not ts or len(ts) < 2:
        return {}
    sym = str(_sym(ts[1]))      # multi-unit: one uuid per unit, the first
    sp = _find1(comp, "sheetpath")
    names, stamps = "/", "/"
    if sp:
        n, s = _find1(sp, "names"), _find1(sp, "tstamps")
        names = str(_sym(n[1])) if n and len(n) > 1 else "/"
        stamps = str(_sym(s[1])) if s and len(s) > 1 else "/"
    out = {"path": stamps.rstrip("/") + "/" + sym, "sheetname": names}
    for pr in _find(comp, "property"):
        nm, val = _find1(pr, "name"), _find1(pr, "value")
        if nm and val and len(val) > 1 and _sym(nm[1]) == "Sheetfile":
            out["sheetfile"] = str(_sym(val[1]))
    return out


def parse_netlist(path: Path) -> tuple[list[dict], dict]:
    """kicadsexpr netlist -> ([{ref, value, fp, dnp, exclude_from_bom,
    fields, path, sheetname, sheetfile, pins}], {"REF.PAD": net}); pins is
    {pad number: [pin name, pin type]} from the netlist's nodes."""
    data = sexpdata.loads(path.read_text(encoding="utf-8"))
    components: list[dict] = []
    netmap: dict[str, str] = {}
    pins: dict[str, dict[str, list[str]]] = {}
    for section in data[1:]:
        if _head(section) == "components":
            for comp in _find(section, "comp"):
                ref = _sym(_find1(comp, "ref")[1])
                vnode = _find1(comp, "value")
                value = _sym(vnode[1]) if vnode and len(vnode) > 1 else ""
                fnode = _find1(comp, "footprint")
                fp = _sym(fnode[1]) if fnode and len(fnode) > 1 else None
                # `(property (name "dnp"))` - valueless, present only when the
                # symbol carries KiCad's native do-not-populate attribute. It
                # is a property, not a field, so _comp_fields never sees it.
                # `exclude_from_bom` is written the same way when the symbol
                # says (in_bom no) (measured, KiCad 10.0.6).
                props = {_sym(_find1(pr, "name")[1])
                         for pr in _find(comp, "property")}
                components.append({"ref": ref, "value": value, "fp": fp,
                                   "dnp": "dnp" in props,
                                   "exclude_from_bom":
                                       "exclude_from_bom" in props,
                                   "fields": _comp_fields(comp),
                                   "natives": _comp_natives(comp),
                                   **_comp_sheet(comp)})
        elif _head(section) == "nets":
            for net in _find(section, "net"):
                nm = _sym(_find1(net, "name")[1])
                for node in _find(net, "node"):
                    r = _sym(_find1(node, "ref")[1])
                    p = _sym(_find1(node, "pin")[1])
                    netmap[f"{r}.{p}"] = nm
                    # The symbol pin's name and electrical type (#1220).
                    # KiCad's "Update PCB from Schematic" copies both onto
                    # the pad; without them every check that finds a pin by
                    # name can't judge the board.
                    pf, pt = _find1(node, "pinfunction"), _find1(node, "pintype")
                    fn = str(_sym(pf[1])) if pf and len(pf) > 1 else ""
                    # kicad-cli's netlist suffixes the pin number onto a
                    # name ("IN-_1" on pin 1); the pad keeps the symbol's
                    # own name ("IN-"), as Update PCB writes it.
                    if fn.endswith(f"_{p}") and len(fn) > len(str(p)) + 1:
                        fn = fn[: -len(str(p)) - 1]
                    pins.setdefault(str(r), {})[str(p)] = [
                        fn, str(_sym(pt[1])) if pt and len(pt) > 1 else ""]
    for c in components:
        c["pins"] = pins.get(str(c["ref"]), {})
    missing = [c["ref"] for c in components if not c["fp"]]
    if missing:
        raise ValueError(f"netlist components without a footprint: {missing}")
    return components, netmap


# --------------------------------------------------------------- stackup block

def build_stackup_block(stackup: dict) -> str:
    """(stackup ...) s-expr text from a stackups.yaml entry, geom-parseable."""
    lines = ["\t\t(stackup",
             '\t\t\t(layer "F.SilkS" (type "Top Silk Screen"))',
             '\t\t\t(layer "F.Paste" (type "Top Solder Paste"))',
             '\t\t\t(layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))']
    for ly in stackup["stack"]:
        if ly["type"] == "copper":
            lines.append(f'\t\t\t(layer "{ly["name"]}" (type "copper") '
                         f'(thickness {ly["thickness_mm"]}))')
        else:
            mat = ly.get("material", "FR4")
            er = ly.get("epsilon_r", 4.5)
            lt = ly.get("loss_tangent", 0.02)
            lines.append(
                f'\t\t\t(layer "{ly["name"]}" (type "{ly["type"]}") '
                f'(thickness {ly["thickness_mm"]}) (material "{mat}") '
                f'(epsilon_r {er}) (loss_tangent {lt}))')
    lines.append('\t\t\t(layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))')
    lines.append('\t\t\t(layer "B.SilkS" (type "Bottom Silk Screen"))')
    lines.append(f'\t\t\t(copper_finish "{stackup.get("copper_finish", "HASL")}")')
    dc = "yes" if stackup.get("dielectric_constraints") else "no"
    lines.append(f'\t\t\t(dielectric_constraints {dc})')
    lines.append("\t\t)")
    return "\n".join(lines) + "\n"


def inject_stackup(pcb_path: Path, stackup: dict) -> None:
    """Insert the (stackup) block into (setup ...) and set (general thickness)."""
    t = pcb_path.read_text(encoding="utf-8")
    if "(stackup" not in t:
        m = re.search(r"\(setup\n", t)
        if not m:
            raise RuntimeError("no (setup block in board to inject stackup")
        block = build_stackup_block(stackup)
        t = t[:m.end()] + block + t[m.end():]
    # set board thickness to the stackup total
    thick = stackup.get("thickness_mm", 1.6)
    t2 = re.sub(r"\(thickness [\d.]+\)", f"(thickness {thick})", t, count=1)
    if t2 == t and "(general" in t:
        t2 = re.sub(r"(\(general\n)", rf"\1\t\t(thickness {thick})\n", t, count=1)
    pcb_path.write_text(t2, encoding="utf-8")


# --------------------------------------------------------------- project file

def build_pro(pro_name: str, cap: dict) -> dict:
    """Minimal, hand-rolled .kicad_pro (LEARNINGS [kicad]: minimal pro is the
    DRC authority; suppress library-mismatch noise for imported footprints).

    Design-rule minimums and the severities that enforce them come from the
    capability profile (lib/fabfloors.py) - the single source rules_gen
    --pro writes too, so the two can never disagree."""
    severities = {"lib_footprint_issues": "ignore",
                  "lib_footprint_mismatch": "ignore"}
    severities.update(fabfloors.pro_rule_severities())
    return {
        "board": {"design_settings": {
            "rule_severities": severities,
            "rules": fabfloors.pro_rules(cap)}},
        "erc": {"rule_severities": {"lib_symbol_issues": "ignore",
                                    "lib_symbol_mismatch": "ignore",
                                    "footprint_link_issues": "ignore"}},
        "meta": {"filename": pro_name, "version": 3},
        "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
    }


def write_pro(pro_path: Path, cap: dict) -> dict:
    """Write the project file and return the floors it carries. Raises if
    what was written does not satisfy the profile (a regression guard on
    build_pro itself, and the standing P7-entry assertion in library form)."""
    pro = build_pro(pro_path.name, cap)
    bad = fabfloors.check_pro(pro, cap)
    if bad:
        raise RuntimeError("project file would ship sub-fab floors: "
                           + "; ".join(bad))
    pro_path.write_text(json.dumps(pro, indent=2), encoding="utf-8")
    return pro["board"]["design_settings"]["rules"]


# --------------------------------------------------------------- self check

SETUP_IGNORE_SOURCES = {"unconnected"}  # unrouted board -> unconnected expected


# Silk checks that a CROSS-footprint pair can trip purely because of the
# temporary shelf packing (refdes text of one part vs a neighbour). Placement
# (P6) re-positions everything, and P7's drc_routed gate re-checks silk at the
# final positions - so these are reported as transient, not init failures.
# A SINGLE-footprint silk violation (own silk over own pad) is a library
# defect and still fails (S14 finding: easyeda2kicad ships such footprints).
_TRANSIENT_SILK_CHECKS = {"silk_overlap", "silk_over_copper"}


def write_waivers(dru_path: Path, footprints: list[str]) -> list[str]:
    """Merge the footprint waivers (#813) for the board's footprints into its
    .kicad_dru, so the setup DRC holds a vendor footprint's own pads to its own
    pegs at the cited floor and not at KiCad's 0.25 mm default. Only the
    aiee_fpwaive_* rules are rewritten: a rules_gen .kicad_dru from an
    earlier run keeps every other rule. Returns the rule names written."""
    rules = fpwaivers.rules(footprints=footprints)
    old = dru_path.read_text(encoding="utf-8") if dru_path.is_file() else None
    if old is None and not rules:
        return []
    new = fpwaivers.merge_dru(old, rules)
    if new != old:
        dru_path.write_text(new, encoding="utf-8")
    return [r["name"] for r in rules]


def _rejects_unconnected_nets(check: dict) -> bool:
    """True when EVERY parity violation is KiCad refusing a pad that carries an
    `unconnected-(...)` pseudo-net.

    The netlist exporter invents that name for a pin the schematic leaves
    unconnected, and whether the PCB is supposed to carry it is NOT uniform:
    measured on KiCad 10.0.5, a FLAT schematic (tests/golden/usbbuck4) reports
    "Pad missing net given by schematic" if the pad is netless, while a
    HIERARCHICAL one (g0-sense, sheets /power/ + /main/) reports "No
    corresponding pin found in schematic" if the pad HAS it - 18 pads, and
    prefixing the sheet path onto the name does not help (measured). Rather
    than guess KiCad's rule, board_init builds with the pseudo-nets, asks the
    real checker, and rebuilds without them only when it sees this exact
    signature. The `unconnected-` guard is what keeps a genuine
    pad-not-in-symbol defect from being silently retried away."""
    parity = [v for v in check["setup_violations"]
              if v.get("source") == "parity"]
    if not parity:
        return False
    for v in parity:
        if v.get("check") != "net_conflict":
            return False
        if "No corresponding pin found in schematic" not in (v.get("msg") or ""):
            return False
        if not any("unconnected-" in (it.get("msg") or "")
                   for it in v.get("items", ())):
            return False
    return True


def _is_transient_silk(v: dict) -> bool:
    # silk vs the fixed BOARD EDGE at temporary shelf positions is purely
    # positional (single-item by nature) - placement re-seats everything.
    if v.get("check") == "silk_edge_clearance":
        return True
    return (v.get("check") in _TRANSIENT_SILK_CHECKS
            and len(set(v.get("refs") or [])) >= 2)


def self_check(cli: Path, pcb: Path, has_sch: bool) -> dict:
    report = kc.run_drc(cli, pcb, parity=has_sch)
    setup_all = [v for v in report["violations"]
                 if v["source"] not in SETUP_IGNORE_SOURCES]
    transient = [v for v in setup_all if _is_transient_silk(v)]
    setup = [v for v in setup_all if not _is_transient_silk(v)]
    parity = [v for v in report["violations"] if v["source"] == "parity"]
    unconnected = [v for v in report["violations"] if v["source"] == "unconnected"]
    return {
        "drc": report["counts"],
        "setup_violations": setup,
        "transient_silk": transient,
        "parity_count": len(parity),
        "unconnected_count": len(unconnected),
        "clean": len(setup) == 0,
    }


# --------------------------------------------------------- stackup freshness

FRESHNESS_MAX_AGE_DAYS = 14


def stackup_freshness(stackup: dict, today: _dt.date | None = None,
                      max_age_days: int = FRESHNESS_MAX_AGE_DAYS) -> dict | None:
    """V18 partial (T6 P5-7, ladder row 169): warn when an impedance-
    controlled stackup's JLC provenance is stale.

    JLC's template list CHURNS - JLC04161H-7628G vanished within 7 days of a
    verified live probe - and the recorded provenance.verified date was never
    checked by any script. For a stackup with a non-empty controlled_impedance
    list read back via jlc_open_api, returns a warning dict when the verified
    date is older than `max_age_days`; None otherwise. WARNING ONLY (the
    offering may be fine) - the credentialed live re-probe belongs with the
    ordering owner. NOT a bench metric: age is today-dependent, and score_p5
    cherry-picks its metric fields (test-pinned)."""
    prov = (stackup or {}).get("provenance") or {}
    if prov.get("method") == "design_intent":
        # Our own lamination for a fab that builds to the drawing: no date
        # makes it true, so say it on every run, impedance rows or not.
        return {"verified": str(prov.get("verified")), "age_days": None,
                "warning": ("design-intent stackup: the fab publishes none and "
                            "epsilon_r is assumed - send the stack with the "
                            "order and have the fab confirm or substitute it "
                            "before routing controlled impedance")}
    if prov.get("method") not in ("jlc_open_api", "jlc_calculator_template"):
        return None
    if not (stackup or {}).get("controlled_impedance"):
        return None
    verified = prov.get("verified")
    if not verified:
        return None
    warning = ("JLC template list churns (7628G vanished in <7 days) - "
               "re-probe getImpedanceTemplateSettingList before routing "
               "controlled impedance; recipe in stackups.yaml header")
    if prov.get("method") == "jlc_calculator_template":
        # Read from JLC's public calculator, never from the order API: say
        # so every time, not only once the date is old.
        warning = (f"read from JLC's impedance calculator, not the order "
                   f"API (order_api_verified: false) - re-run `hwde "
                   f"stackup_fetch entry --name {prov.get('template_name')}` "
                   f"and confirm the build in JLC's order form before "
                   f"routing controlled impedance")
        if not prov.get("order_api_verified"):
            return {"verified": str(verified), "age_days": None,
                    "warning": warning}
    try:
        age = (today or _dt.date.today()
               ) - _dt.date.fromisoformat(str(verified))
    except ValueError:
        return {"verified": str(verified), "age_days": None,
                "warning": f"unparseable provenance.verified date; {warning}"}
    if age.days <= max_age_days:
        return None
    return {"verified": str(verified), "age_days": age.days,
            "warning": f"verified {verified} ({age.days} days ago); {warning}"}


def mode_outline_guard(out_dir: Path, outline: str,
                       workspace: str | None = None,
                       allow_fixed: bool = False) -> dict | None:
    """U18: note a FIXED outline when the run's build mode makes geometry an
    output of the placement.

    bb-buck is the case this exists for. Its P2 derived 40 x 30 mm because the
    outline is the radiator; P5 was then handed 35 x 25 from the H1 checkpoint,
    and P6 optimized to FIT it - 0.05 mm of slack on all four edges. Nothing
    said so, because `--outline WxH` had no idea a mode was in force. Now it
    reads the recorded mode (`state.py mode`) and, when a fixed size is given
    anyway, builds it and says plainly that the size did not come from the
    layout (once a refusal; a refusal is a rule the agent cannot weigh, and an
    enclosure or a mating board can make a fixed size the right call).

    Returns the mode record when one is in force (for the report), None when
    there is no workspace or no mode. `--allow-fixed-outline` (deliberate)
    words the note as a choice rather than a warning.
    """
    ws = statelib.find_workspace(out_dir, workspace)
    if ws is None:
        return None
    try:
        state = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    mode = state.get("mode")
    if not mode:
        return None
    rec = {"workspace": str(ws).replace("\\", "/"), **mode}
    if outline == "auto" or not mode.get("geometry_is_output"):
        return rec
    if allow_fixed:
        rec["fixed_outline_override"] = (
            f"--outline {outline} accepted under binding "
            f"{mode.get('binding')} because --allow-fixed-outline was passed; "
            "the size did NOT come from the placement")
    else:
        rec["fixed_outline_override"] = (
            f"--outline {outline} under build mode {mode.get('target')} "
            f"(binding {mode.get('binding')}), where the board size is "
            "usually an OUTPUT of the placement: the size did NOT come from "
            "the placement. If nothing outside the board fixes it, re-run "
            "with --outline auto and board_edit.py --outline fit after the "
            "place gate (reference/recipes/resize-board.md); either way "
            "record why (state.py decision)")
    return rec


# --------------------------------------------------------------- main

_EDGE_XY = re.compile(r"\((?:start|end|mid|center)\s+(-?[\d.]+)\s+(-?[\d.]+)\)")


def outline_origin(pcb: Path) -> list[float] | None:
    """Top-left corner of a board's Edge.Cuts outline, or None."""
    from lib import fpfix  # noqa: E402  (paren scanner)
    text = pcb.read_text(encoding="utf-8", errors="replace")
    xs, ys = [], []
    for head, a, b in fpfix.top_level_nodes(text):
        if not head.startswith("gr_"):
            continue
        sub = text[a:b]
        if '"Edge.Cuts"' not in sub and " Edge.Cuts)" not in sub:
            continue
        for x, y in _EDGE_XY.findall(sub):
            xs.append(float(x))
            ys.append(float(y))
    return [round(min(xs), 3), round(min(ys), 3)] if xs else None


def resolve_origin(arg: str, pcb: Path) -> tuple[list[float] | None, str]:
    """(origin or None, where it came from) for --origin keep|auto|X,Y."""
    a = str(arg).strip().lower()
    if a == "auto":
        return None, "auto"
    if a == "keep":
        if pcb.is_file():
            o = outline_origin(pcb)
            if o is not None:
                return o, f"kept from {pcb.name}"
        return None, "auto (no board to keep it from)"
    m = re.fullmatch(r"\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*", a)
    if not m:
        raise RuntimeError(f"bad --origin {arg!r} (use keep, auto or X,Y)")
    return [float(m.group(1)), float(m.group(2))], "given"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--netlist", required=True)
    ap.add_argument("--name", required=True, help="board basename")
    ap.add_argument("--out", required=True, help="output directory (workspace/kicad)")
    ap.add_argument("--layers", type=int, default=4,
                    choices=list(range(2, 34, 2)),
                    help="copper layers, 2 to 32 (even); 6+ default to JLC's "
                         "free/general lamination for that count "
                         "(stackups.yaml defaults); an HDI build is "
                         "--stackup JLCH...")
    ap.add_argument("--copper-oz", type=float,
                    help="outer copper weight, selects the jlc_capabilities "
                         "profile whose minimums are written into the "
                         ".kicad_pro (default: the stackup's own outer copper)")
    ap.add_argument("--stackup", help="stackup name (default: stackups.defaults[layers])")
    ap.add_argument("--outline", default="auto",
                    help="'auto' (bbox+margin) or 'WxH' in mm, e.g. 60x40")
    ap.add_argument("--origin", default="keep",
                    help="outline top-left corner: 'keep' (default) = the "
                         "existing board's, else as 'auto'; 'auto' = where "
                         "the packed parts put it; 'X,Y' in mm")
    ap.add_argument("--margin", type=float, default=6.0)
    ap.add_argument("--corner-radius", default="auto",
                    help="round the outline corners by this radius in mm; "
                         "'auto' (default) = %s mm, capped at a tenth of the "
                         "shorter side; 0 = square corners. Clamped to half the "
                         "shorter side and to the mounting-hole inset"
                         % board_swig_defaults()[0])
    ap.add_argument("--cutout", action="append", default=[], metavar="X,Y,W,H",
                    help="rectangular edge notch in mm, relative to the "
                         "outline's top-left corner; repeatable. MUST touch an "
                         "outline edge and must not overlap a corner radius. "
                         "Interior windows are rejected (they mis-parse as the "
                         "board outline downstream).")
    ap.add_argument("--mounting-holes", default="auto",
                    help="corner mounting holes: 0..4, or 'auto' (default) = "
                         "4 when the board's shorter side is at least %s mm, "
                         "else none. On a fixed --outline, 'auto' also gives "
                         "none when the packed parts reach closer to an edge "
                         "than a corner hole needs (the report's "
                         "mounting_holes_skipped says so); pass 4 to have "
                         "them anyway and let placement clear the corners. "
                         "A brief that asks for none passes 0"
                         % board_swig_defaults()[1])
    ap.add_argument("--mounting-hole-fp", default=DEFAULT_HOLE_FP,
                    help="footprint for --mounting-holes (LIB:NAME). Default is "
                         "the M3 3.2 mm hole with a plated copper ring and a "
                         "ring of vias, joined to GND. A bare NPTH hole is "
                         "MountingHole:MountingHole_3.2mm_M3; a small board "
                         "asked for M2 wants MountingHole:MountingHole_2.2mm_"
                         "M2_Pad - the hole SIZE is a mechanical "
                         "requirement, so state it rather than inheriting M3. "
                         "A footprint whose own drills break the fab's "
                         "hole-to-hole floor (KiCad's M2 and M2.5 _Pad_Via "
                         "rings) is swapped for its _Pad sibling, noted in "
                         "worker_notes, or refused.")
    ap.add_argument("--workspace", help="workspace holding state.json "
                    "(default: the first parent of --out that has one) - the "
                    "recorded build mode says whether a fixed --outline is "
                    "earned (reference/build-modes.md)")
    ap.add_argument("--allow-fixed-outline", action="store_true",
                    help="a fixed --outline under a build mode whose binding "
                         "makes the geometry an OUTPUT is deliberate: the "
                         "note in build_mode.fixed_outline_override says so")
    ap.add_argument("--schematic", help="copy this .kicad_sch next to the board "
                    "so the self-check can run schematic parity")
    ap.add_argument("--fp-lib", action="append", default=[],
                    help="extra footprint library parent dir (repeatable)")
    ap.add_argument("--out-report", help="write JSON report here instead of stdout")
    args = ap.parse_args(argv)

    try:
        cli = env.find_kicad_cli()
        if cli is None:
            raise RuntimeError("kicad-cli not found (see check_env.py)")
        bp = env.find_kicad_python(cli)
        if bp is None:
            raise RuntimeError("bundled python (pcbnew) not found")

        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        pcb_path = out_dir / f"{args.name}.kicad_pcb"

        outline = {"mode": "auto"}
        if args.outline != "auto":
            m = re.fullmatch(r"([\d.]+)x([\d.]+)", args.outline)
            if not m:
                raise RuntimeError(f"bad --outline {args.outline!r} (use auto or WxH)")
            outline = {"mode": "fixed", "w": float(m.group(1)), "h": float(m.group(2))}
        # U18: under a geometry-as-output build mode a fixed outline is
        # built, and noted in the report (worker_notes) as not earned.
        build_mode = mode_outline_guard(out_dir, args.outline, args.workspace,
                                        args.allow_fixed_outline)

        origin, origin_source = resolve_origin(args.origin, pcb_path)

        components, netmap = parse_netlist(Path(args.netlist))

        stacks = yaml.safe_load(STACKUP_FILE.read_text(encoding="utf-8"))
        stk_name = args.stackup or stacks["defaults"].get(args.layers)
        if stk_name is None:
            raise RuntimeError(
                f"no default {args.layers}-layer stackup in stackups.yaml: "
                f"add JLC's with `hwde stackup_fetch list --prefix "
                f"JLC{args.layers:02d}` / `entry --name ...` and pass --stackup")
        stackup = stacks["stackups"].get(stk_name)
        if stackup is None:
            # a Rogers grade or hybrid JLC doesn't sell gets its reason, not
            # just "unknown name" (jlc_capabilities.yaml not_offered)
            why = fabmaterial.not_offered(
                fabmaterial.family_of_name(str(stk_name)))
            raise RuntimeError(f"stackup {stk_name!r} not in stackups.yaml"
                               + (f" - {why}" if why else ""))
        if stackup["layers"] != args.layers:
            raise RuntimeError(f"stackup {stk_name} is {stackup['layers']}-layer, "
                               f"--layers {args.layers}")
        if stackup.get("available") is False:
            # A stackup JLC does not sell must never size a design: the
            # phantom JLC04161H-3313 is why lumina-carrier's 100R MDI was
            # solved against a lamination nobody can build (LEARNINGS
            # 2026-07-30 [stackup][ordering]).
            ret = stackup.get("retired") or {}
            raise RuntimeError(
                f"stackup {stk_name} is marked available: false in "
                f"stackups.yaml - {ret.get('reason', 'not offered by JLC')}. "
                f"Use one of: {', '.join(ret.get('replacements') or []) or 'see stackups.yaml'}")

        # Fab floors come from the capability profile for (layers, outer
        # copper), never from a hard-coded default.
        outer_oz = args.copper_oz
        if outer_oz is None:
            coppers = [ly for ly in stackup["stack"] if ly["type"] == "copper"]
            outer_oz = float(coppers[0].get("copper_oz", 1.0)) if coppers else 1.0
        # ...and for the stackup's laminate: a Rogers board gets JLC's
        # high-frequency floors, and a material JLC doesn't build stops here;
        # ...and the stackup's fab: a PCBWay/OSH Park/AISLER entry takes
        # that vendor's floors (reference/vendors/, #446)
        material = fabmaterial.stackup_family(stackup)
        cap_class, cap = fabfloors.profile(args.layers, outer_oz,
                                           material=material,
                                           vendor=fabfloors.vendor_of(stackup))

        holes_arg = str(args.mounting_holes).strip().lower()
        if holes_arg != "auto":
            if not holes_arg.isdigit() or int(holes_arg) > 4:
                raise RuntimeError(f"bad --mounting-holes {args.mounting_holes!r}"
                                   f" (use auto or 0..4)")
            holes_arg = int(holes_arg)
        radius_arg = str(args.corner_radius).strip().lower()
        radius_arg = None if radius_arg == "auto" else float(radius_arg)
        if radius_arg is not None and radius_arg < 0:
            raise RuntimeError("--corner-radius must be >= 0")

        cutouts = []
        for spec in args.cutout:
            m = re.fullmatch(r"\s*([\d.]+),([\d.]+),([\d.]+),([\d.]+)\s*", spec)
            if not m:
                raise RuntimeError(f"bad --cutout {spec!r} (use X,Y,W,H in mm)")
            x, y, w, h = (float(g) for g in m.groups())
            if w <= 0 or h <= 0:
                raise RuntimeError(f"bad --cutout {spec!r}: W and H must be > 0")
            cutouts.append({"x": x, "y": y, "w": w, "h": h})

        job = {
            "out": str(pcb_path), "layers": args.layers,
            "components": components, "netmap": netmap,
            "fp_paths": args.fp_lib, "margin": args.margin, "outline": outline,
            "origin": origin,
            "corner_radius": radius_arg, "cutouts": cutouts,
            "mounting_holes": ({"count": holes_arg,
                                "inset": args.margin / 2.0,
                                "cu_edge": float(cap["min_copper_to_edge_mm"]),
                                "hole_to_hole": float(cap["min_hole_to_hole_mm"]),
                                "fp": args.mounting_hole_fp}
                               if holes_arg else None),
        }
        import tempfile

        def run_worker() -> dict:
            with tempfile.TemporaryDirectory(prefix="aiee_binit_") as td:
                jf = Path(td) / "job.json"
                jf.write_text(json.dumps(job), encoding="utf-8")
                cp = subprocess.run([str(bp), str(WORKER), str(jf)],
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=300)
            w = _last_json(cp.stdout)
            if w is None or w.get("status") != "pass":
                raise RuntimeError(f"board_swig worker failed: "
                                   f"{(cp.stdout or cp.stderr)[-600:]}")
            inject_stackup(pcb_path, stackup)
            # Every worker run (the unconnected-nets retry included) may
            # leave a default .kicad_pro behind; write ours AFTER it so its
            # ignore settings always survive to the self-check.
            w["_floors"] = write_pro(out_dir / f"{args.name}.kicad_pro", cap)
            return w

        worker = run_worker()
        floors = worker["_floors"]

        has_sch = False
        if args.schematic:
            sch_dst = out_dir / f"{args.name}.kicad_sch"
            src = Path(args.schematic).resolve()
            # The schematic often already lives next to the board (P4 builds
            # into kicad/) - copying a file onto itself raises SameFileError.
            if src != sch_dst.resolve():
                shutil.copy(args.schematic, sch_dst)
            has_sch = True

        waivers = write_waivers(out_dir / f"{args.name}.kicad_dru",
                                [c["fp"] for c in components])
        check = self_check(cli, pcb_path, has_sch)
        unconnected_nets_skipped = False
        if _rejects_unconnected_nets(check):
            # Measured, not assumed - see _rejects_unconnected_nets.
            job["skip_unconnected_nets"] = True
            worker = run_worker()
            floors = worker["_floors"]
            unconnected_nets_skipped = True
            check = self_check(cli, pcb_path, has_sch)

        worker_notes = list(worker.get("notes", []))
        fresh = stackup_freshness(stackup)
        if fresh:
            worker_notes.append(f"stackup_freshness: {fresh['warning']}")

        result = {
            "script": "board_init",
            "status": "pass" if check["clean"] else "violations",
            "board": str(pcb_path), "stackup": stk_name, "layers": args.layers,
            "fab_profile": cap_class, "copper_oz": outer_oz,
            "material": material,
            "vendor": stackup.get("vendor", "jlcpcb"),
            "fab_floors": floors,
            "components": len(components), "nets": worker["nets"],
            "outline_bbox": worker["bbox"],
            "mounting_holes": worker.get("mounting_holes", 0),
            "mounting_hole_fp": worker.get("mounting_hole_fp"),
            "mounting_hole_net": worker.get("mounting_hole_net"),
            "mounting_hole_inset": worker.get("mounting_hole_inset"),
            # "auto" dropping the holes on a fixed outline is easy to miss
            # in worker_notes; a board that wants them passes 4.
            "mounting_holes_skipped": next(
                (n + " - pass --mounting-holes 4 to place them anyway"
                 for n in worker_notes if "the fixed outline leaves" in n),
                None),
            "corner_radius": worker.get("corner_radius", 0.0),
            "outline_origin": worker.get("outline_origin"),
            "origin_source": origin_source,
            "aux_origin": worker.get("aux_origin"),
            "cutouts": worker.get("cutouts", []),
            "footprint_waivers": waivers,
            "self_check": check, "worker_notes": worker_notes,
            "unconnected_nets_skipped": unconnected_nets_skipped,
        }
        if fresh:
            result["stackup_freshness"] = fresh
        if build_mode:
            result["build_mode"] = build_mode
            if build_mode.get("fixed_outline_override"):
                worker_notes.append(build_mode["fixed_outline_override"])
        _emit(result, args.out_report)
        return 0 if check["clean"] else 1
    except Exception:
        _emit({"script": "board_init", "status": "error",
               "error": traceback.format_exc()}, args.out_report if 'args' in dir() else None)
        return 2


def _last_json(text: str) -> dict | None:
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _emit(obj: dict, out: str | None) -> None:
    s = json.dumps(obj, indent=2)
    if out:
        Path(out).write_text(s, encoding="utf-8")
    else:
        print(s)


if __name__ == "__main__":
    sys.exit(main())
