"""layoutimpl - P3 layout implications: deterministic screens (U9).

v3 design decision 4: deliver layout constraints EARLIER instead of running
stages concurrently. Four board runs found, at P6/P7, facts that were already
in the P3 datasheet extraction (LEARNINGS 2026-08-09): the SO-8EP exposed pad
holds at most 12 thermal vias so a 4x4 array is impossible; a DB128L terminal's
wire entry is at local +Y so `rot 0` on the left edge points it INTO the
board; a spread part's courtyard can make a small board unplaceable; a
per-net width floor wider than a pad makes every stub illegal. Each is
arithmetic on numbers P3 already holds, so P3 computes them here.

Fields per part (`implications(extract, part, cap)`):
  thermal_vias      exposed-pad via capacity: max nx x ny array at the fab
                    pitch floor, plus whether a square NxN fits at all
  orientation       wire-entry direction (terminal-block families + the
                    extraction's land_pattern.wire_entry_local) and the
                    rotation that points it off each board edge
  courtyard         courtyard w x h and area (land_pattern.courtyard_mm, else
                    a chip-package table) - summed against the board budget
  routing           pad pitch vs the fab routing floor: can a track pass
                    between two pads, and the widest track a pad can take
                    without necking (the netclass-floor-vs-stub finding)

`screen(ws_dir, ...)` runs every part in parts/parts.json and cross-checks
the result against architecture/constraints.json: a `thermal[].min_vias`
above the pad's capacity, a `placement.edges[].rot` that points a wire entry
inward, a courtyard sum above the board budget. Those are CONFLICTS (the
caller exits 1). `score(implications, needs)` is the part-sourcer's ranking
term: a candidate that cannot meet a stated need loses to one that can.

Library only; the CLI is datasheet_extract.py --screen / --implications.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import fabfloors

# Terminal-block families whose wire entry faces local +Y (KiCad y down):
# KF128 (LEARNINGS 2026-07-28) and DB128L (2026-08-09, confirmed on the
# side render). Match on the MPN; an extraction's
# land_pattern.wire_entry_local outranks this table.
WIRE_ENTRY_FAMILIES = [
    (re.compile(r"^KF128", re.I), "+Y"),
    (re.compile(r"^DB128L?[-_]", re.I), "+Y"),
]
# Rotation (KiCad degrees, CCW on screen) that turns a local direction to
# face OFF each board edge. Local +Y at rot 90 faces +x (right) and at rot
# 270 faces -x (left) - the DB128L finding's "270 = out the LEFT edge".
_EDGE_VEC = {"left": (-1, 0), "right": (1, 0), "top": (0, -1),
             "bottom": (0, 1)}
_LOCAL_VEC = {"+Y": (0, 1), "-Y": (0, -1), "+X": (1, 0), "-X": (-1, 0)}

# IPC-7351 nominal courtyards (mm) for chip packages, used only when the
# extraction carries no land_pattern.courtyard_mm.
CHIP_COURTYARD = {"0201": (1.2, 0.7), "0402": (1.9, 1.0),
                  "0603": (3.0, 1.5), "0805": (3.4, 1.9),
                  "1206": (4.6, 2.3), "1210": (4.6, 3.2),
                  "2010": (6.1, 3.2), "2512": (7.4, 3.9),
                  "SOD-123": (4.3, 2.0), "SOT-23": (3.4, 3.0),
                  "SOIC-8": (6.0, 5.4), "SO-8EP": (6.0, 5.4)}
# Courtyard-sum / board-area screens. Above TIGHT, P6 placement has needed
# the annealer's full budget on past boards; above OVER it has not closed.
TIGHT_RATIO, OVER_RATIO = 0.45, 0.65
DEFAULT_CAP_CLASS = "2layer_1oz"


def norm_pkg(pkg: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (pkg or "").lower())


def fab_row(cap_class: str = DEFAULT_CAP_CLASS) -> dict:
    rows = fabfloors.load_capabilities()
    if cap_class not in rows:
        raise fabfloors.FabFloorError(
            f"no capability row {cap_class!r} (have: {', '.join(sorted(rows))})")
    return rows[cap_class]


# ---- individual screens ----------------------------------------------------

def via_capacity(ep_w: float, ep_h: float, cap: dict) -> dict:
    """Largest in-pad via array on a w x h exposed pad at the fab floors.

    Pitch floor = max(drill + hole-to-hole, via land + copper clearance);
    n vias fit on an axis iff (n-1)*pitch + land <= axis. Square NxN arrays
    are reported separately because thermal notes are written as 4x4/3x3.
    """
    drill = cap["min_via_drill_mm"]
    land = cap["min_via_diameter_mm"]
    pitch = max(drill + cap["min_hole_to_hole_mm"],
                land + cap["min_clearance_mm"])

    def fit(axis: float) -> int:
        return 0 if axis < land else int((axis - land) / pitch + 1e-9) + 1
    nx, ny = fit(ep_w), fit(ep_h)
    sq = min(nx, ny)
    return {"ep_mm": [ep_w, ep_h], "drill_mm": drill, "land_mm": land,
            "pitch_floor_mm": round(pitch, 3), "nx": nx, "ny": ny,
            "max_vias": nx * ny, "max_square": f"{sq}x{sq}",
            "square_4x4_fits": sq >= 4}


def wire_entry(mpn: str | None, extract: dict | None) -> str | None:
    lp = (extract or {}).get("land_pattern") or {}
    if lp.get("wire_entry_local"):
        return lp["wire_entry_local"]
    for rx, d in WIRE_ENTRY_FAMILIES:
        if mpn and rx.search(mpn):
            return d
    return None


def edge_rotations(local: str) -> dict:
    """{edge: rot} such that the local direction faces off that edge."""
    lx, ly = _LOCAL_VEC[local]
    out = {}
    for edge, (ex, ey) in _EDGE_VEC.items():
        for rot in (0, 90, 180, 270):
            # KiCad rotation on a y-down screen: (x, y) -> (x cos + y sin,
            # -x sin + y cos) for CCW-positive degrees.
            r = math.radians(rot)
            vx = round(lx * math.cos(r) + ly * math.sin(r))
            vy = round(-lx * math.sin(r) + ly * math.cos(r))
            if (vx, vy) == (ex, ey):
                out[edge] = rot
    return out


def courtyard(extract: dict | None, package: str | None) -> dict | None:
    lp = (extract or {}).get("land_pattern") or {}
    if lp.get("courtyard_mm"):
        w, h = lp["courtyard_mm"]
        src = "extraction"
    else:
        want = norm_pkg(package or lp.get("package"))
        hit = next((v for k, v in CHIP_COURTYARD.items()
                    if norm_pkg(k) == want), None)
        if hit is None:
            return None
        w, h = hit
        src = "table"
    return {"w_mm": w, "h_mm": h, "area_mm2": round(w * h, 2), "source": src}


def routing(extract: dict | None, cap: dict) -> dict | None:
    lp = (extract or {}).get("land_pattern") or {}
    pitch, size = lp.get("pitch_mm"), lp.get("pad_size_mm")
    if not pitch or not size:
        return None
    pad_w = min(size)
    gap = pitch - pad_w
    need = cap["min_trace_width_mm"] + 2 * fabfloors.outer_clearance(cap)
    return {"pitch_mm": pitch, "pad_w_mm": pad_w, "gap_mm": round(gap, 3),
            "track_between_pads": gap >= need,
            "max_stub_width_mm": pad_w,
            "note": ("a netclass width floor above max_stub_width_mm makes "
                     "every stub into this pad illegal - pour that net as a "
                     "zone (LEARNINGS 2026-08-09 netclass-floor vs stubs)")}


def implications(extract: dict | None, part: dict | None = None,
                 cap: dict | None = None) -> dict:
    """The layout_implications object for one part (see module doc)."""
    cap = cap or fab_row()
    part = part or {}
    mpn = part.get("mpn") or (extract or {}).get("mpn")
    pkg = part.get("package") or (extract or {}).get("package")
    out: dict = {"mpn": mpn, "package": pkg, "gaps": []}
    ep = (extract or {}).get("exposed_pad") or {}
    if ep.get("present"):
        if ep.get("size_mm"):
            out["thermal_vias"] = via_capacity(*ep["size_mm"], cap)
        else:
            out["gaps"].append("exposed_pad.size_mm missing - via "
                               "capacity unknown")
    we = wire_entry(mpn, extract)
    if we:
        out["orientation"] = {"wire_entry_local": we,
                              "rot_for_edge": edge_rotations(we)}
    cy = courtyard(extract, pkg)
    if cy:
        out["courtyard"] = cy
    else:
        out["gaps"].append("courtyard unknown (no land_pattern.courtyard_mm "
                           "and package not in CHIP_COURTYARD)")
    rt = routing(extract, cap)
    if rt:
        out["routing"] = rt
    return out


# ---- workspace screen -------------------------------------------------------

_REF = r"[A-Z]{1,3}\d+"


def part_refs(part: dict) -> list[str]:
    """Refs a parts.json entry stands for: an explicit `refs` list, else the
    LEADING refs of its `role` ("C5-C8, input bank" -> C5..C8; "J1 (LEFT)
    and J2 (RIGHT) ..." -> J1, J2). Refs mentioned later in the prose are
    not this part's, nor is a ref with another prefix."""
    if part.get("refs"):
        return list(part["refs"])
    role = re.sub(r"\([^)]*\)", "", part.get("role") or "")
    m = re.match(rf"\s*({_REF}(?:-{_REF})?(?:\s*(?:,|and|&|/)\s*"
                 rf"{_REF}(?:-{_REF})?)*)", role)
    if not m:
        return []
    refs = []
    for tok in re.split(r"\s*(?:,|and|&|/)\s*", m.group(1).strip()):
        if "-" in tok:
            a, b = tok.split("-")
            pa, na = re.match(r"([A-Z]+)(\d+)", a).groups()
            pb, nb = re.match(r"([A-Z]+)(\d+)", b).groups()
            if pa == pb and int(na) <= int(nb):
                refs += [f"{pa}{i}" for i in range(int(na), int(nb) + 1)]
                continue
        refs.append(tok)
    # one parts.json entry is one MPN, so its refs share a prefix: "R1, Q1
    # gate-to-GND bleed" is R1 alone, "C3, P4 UPDATE" is C3 alone
    lead = re.match(r"[A-Z]+", refs[0]).group(0)
    return [r for r in refs if re.match(r"[A-Z]+", r).group(0) == lead]


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def screen(ws: Path, board_mm: tuple[float, float] | None = None,
           cap_class: str = DEFAULT_CAP_CLASS) -> dict:
    """Per-part implications for a workspace + conflicts with its
    constraints. Reads parts/parts.json, parts/<lcsc>.json extractions
    (else parts/<mpn>.json) and architecture/constraints.json (kicad/
    copies after P5)."""
    ws = Path(ws)
    cap = fab_row(cap_class)
    pj = next((p for p in (ws / "parts" / "parts.json",
                           ws / "kicad" / "parts.json") if p.is_file()), None)
    if pj is None:
        raise FileNotFoundError(f"no parts.json under {ws}/parts or kicad")
    cj = next((p for p in (ws / "architecture" / "constraints.json",
                           ws / "kicad" / "constraints.json")
               if p.is_file()), None)
    cons = _load(cj) if cj else {}
    parts, by_ref = [], {}
    for part in _load(pj).get("parts", []):
        # keyed by the LCSC code, else by the MPN on a US-sourced board
        # (#1119: parts/<MPN>.json, or its safe name: '#', '/' and '.'
        # made '-', as check_ratings.key_by_mpn reads it)
        names = []
        for k in ("lcsc", "mpn"):
            v = str(part.get(k) or "").strip()
            if v:
                names += [v, re.sub(r"[^A-Za-z0-9._-]", "-", v),
                          re.sub(r"[^A-Za-z0-9_-]", "-", v)]
        key = next((n for n in names if "/" not in n and not n.startswith(".")
                    and (pj.parent / f"{n}.json").is_file()), None)
        ext_path = pj.parent / f"{key or part.get('lcsc')}.json"
        extract = _load(ext_path) if key else None
        imp = implications(extract, part, cap)
        imp.update(lcsc=part.get("lcsc"), refs=part_refs(part),
                   qty=part.get("qty_per_board") or 1,
                   extraction=ext_path.name if extract else None)
        parts.append(imp)
        for r in imp["refs"]:
            by_ref[r] = imp
    conflicts = []
    for t in cons.get("thermal") or []:
        imp = by_ref.get(t.get("ref"))
        tv = (imp or {}).get("thermal_vias")
        if tv and t.get("min_vias") and t["min_vias"] > tv["max_vias"]:
            conflicts.append({
                "kind": "thermal_vias_over_capacity", "ref": t["ref"],
                "wanted": t["min_vias"], "max": tv["max_vias"],
                "detail": (f"constraints.thermal[{t['ref']}].min_vias "
                           f"{t['min_vias']} > the {tv['ep_mm'][0]} x "
                           f"{tv['ep_mm'][1]} mm pad's {tv['nx']} x "
                           f"{tv['ny']} = {tv['max_vias']} at "
                           f"{tv['pitch_floor_mm']} mm pitch; square "
                           f"{tv['max_square']} max")})
    for e in (cons.get("placement") or {}).get("edges") or []:
        imp = by_ref.get(e.get("ref"))
        rots = ((imp or {}).get("orientation") or {}).get("rot_for_edge")
        if rots and e.get("edge") in rots and "rot" in e \
                and int(e["rot"]) % 360 != rots[e["edge"]]:
            conflicts.append({
                "kind": "wire_entry_faces_inward", "ref": e["ref"],
                "edge": e["edge"], "rot": e["rot"],
                "want_rot": rots[e["edge"]],
                "detail": (f"placement.edges[{e['ref']}] rot {e['rot']} on "
                           f"the {e['edge']} edge; wire entry is local "
                           f"{imp['orientation']['wire_entry_local']}, so "
                           f"rot {rots[e['edge']]} faces off-board")})
    total = sum(p["courtyard"]["area_mm2"] * p["qty"]
                for p in parts if p.get("courtyard"))
    budget = {"courtyard_sum_mm2": round(total, 1),
              "unknown_parts": [p["mpn"] for p in parts
                                if not p.get("courtyard")]}
    if board_mm:
        area = board_mm[0] * board_mm[1]
        ratio = total / area
        budget.update(board_mm=list(board_mm), board_mm2=area,
                      ratio=round(ratio, 3),
                      verdict=("over" if ratio > OVER_RATIO else "tight"
                               if ratio > TIGHT_RATIO else "ok"))
        if ratio > OVER_RATIO:
            conflicts.append({"kind": "courtyard_over_budget",
                              "detail": f"courtyards {total:.0f} mm2 = "
                                        f"{ratio:.0%} of the {area:.0f} mm2 "
                                        f"board (> {OVER_RATIO:.0%})"})
        for p in parts:
            cy = p.get("courtyard")
            if cy and (min(cy["w_mm"], cy["h_mm"]) > min(board_mm)
                       or max(cy["w_mm"], cy["h_mm"]) > max(board_mm)):
                conflicts.append({"kind": "courtyard_exceeds_board",
                                  "mpn": p["mpn"],
                                  "detail": f"{cy['w_mm']} x {cy['h_mm']} mm "
                                            f"does not fit {board_mm}"})
    return {"workspace": ws.as_posix(), "cap_class": cap_class,
            "parts": parts, "board_budget": budget, "conflicts": conflicts}


# ---- part-sourcer scoring ---------------------------------------------------

def score(imp: dict, needs: dict) -> dict:
    """Rank term for one candidate against the slot's stated needs:
    {min_thermal_vias, edge, max_courtyard_mm2, min_stub_width_mm}.
    Each unmet need is a named fail; score = 100 - 40/fail - 10/gap, so any
    candidate that meets every need outranks one that does not."""
    fails = []
    tv = imp.get("thermal_vias")
    if needs.get("min_thermal_vias"):
        if not tv:
            fails.append("thermal_vias unknown")
        elif tv["max_vias"] < needs["min_thermal_vias"]:
            fails.append(f"thermal_vias {tv['max_vias']} < "
                         f"{needs['min_thermal_vias']}")
    if needs.get("edge"):
        rots = (imp.get("orientation") or {}).get("rot_for_edge")
        if rots is None:
            fails.append("wire entry direction unknown")
    cy = imp.get("courtyard")
    if needs.get("max_courtyard_mm2") and cy \
            and cy["area_mm2"] > needs["max_courtyard_mm2"]:
        fails.append(f"courtyard {cy['area_mm2']} > "
                     f"{needs['max_courtyard_mm2']} mm2")
    rt = imp.get("routing")
    if needs.get("min_stub_width_mm") and rt \
            and rt["max_stub_width_mm"] < needs["min_stub_width_mm"]:
        fails.append(f"pad {rt['max_stub_width_mm']} mm < stub floor "
                     f"{needs['min_stub_width_mm']} mm (pour the net)")
    gaps = len(imp.get("gaps") or [])
    return {"score": 100 - 40 * len(fails) - 10 * gaps, "fails": fails,
            "gaps": gaps}
