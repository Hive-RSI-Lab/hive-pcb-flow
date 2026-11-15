#!/usr/bin/env python
"""check_mounting.py - a board big enough to need screws has a place for one
(P8 verify; board only; hive-pcb-flow #1088).

The hit: CMM2-JLC Rev.A, a 150 x 95 mm main board, shipped with no mounting
hole at all ("Missing mounting holes", fixed in Rev.B). Nothing in KiCad's
DRC or a fab's DFM asks for one, because a board can mount many ways; the
gap is only seen when the board meets its case.

Finding (warning, one per board):

  no_mounting_holes  the board outline is at least MIN_AREA_MM2 (or its long
                     side at least MIN_SIDE_MM) and it has no mounting hole:
                     no footprint that is only a hole (MountingHole, standoff,
                     a reference H/MH n), no lone plated or bare hole of
                     M2-M5 screw clearance (HOLE_MIN_MM-HOLE_MAX_MM) that is
                     not a part's own pin or peg, and no round window of that
                     size cut in Edge.Cuts. Fix: add M2.5/M3 holes (KiCad's
                     MountingHole library) at the corners, or say how it
                     mounts.

Exempt, and said in the report's facts (`mounts_by`):
  castellated  the board carries castellated edge pads: a module soldered
               onto a carrier (lib/castellation.py).
  edge_card    a row of EDGE_FINGERS or more plated fingers on one copper
               side whose pads run to the board edge (a card that plugs into
               an edge connector: PCIe, M.2, a cartridge), or an edge-card
               footprint (Connector_PCBEdge, a "CardEdge" name).
  rail         tabs on two opposite edges that stand out of the outline,
               short and shallow and mirrored across the board (the board
               slides into a case's slots or a rail by them; icE1usb's
               four 4 mm x 1 mm corner tabs): on each of the two edges, two
               or more runs touch the outline's bounding line, each at most
               TAB_MAX_FRAC of the edge, the edge between them recessed by
               TAB_MIN_DEPTH_MM-TAB_MAX_DEPTH_MM, and the runs of one edge
               the other's within TAB_MIRROR_MM. Seen on one corpus board
               only: a board with mirrored edge tabs that is screwed down
               should declare it.
  declared     constraints `mechanical.mounting`: {by: "clip" | "adhesive" |
               "stack" | "rail" | "edge_card" | "castellated" | "cable" |
               "none", why: "..."} says it mounts another way.

Board size is the outline's bounding box and area (the largest Edge.Cuts
face). Thresholds: MIN_AREA_MM2 2500 (a 50 x 50 mm board) or a long side of
MIN_SIDE_MM 80; smaller boards (breakouts, sensor tabs, wearables) are often
held by their connector, a header or tape, and are not judged.

CLI: --pcb board.kicad_pcb [--constraints c.json] [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import castellation  # noqa: E402
import checklib  # noqa: E402
import geom  # noqa: E402

SCRIPT = "check_mounting"
SOURCE = SCRIPT

MIN_AREA_MM2 = 2500.0   # 50 x 50 mm
MIN_SIDE_MM = 80.0
HOLE_MIN_MM = 2.0       # M1.6 close fit is 1.7; M2 clearance is 2.2-2.4
HOLE_MAX_MM = 5.6       # M5 clearance is 5.3-5.5
EDGE_FINGERS = 5
EDGE_TOUCH_MM = 0.5     # a finger's pad within this of the outline edge
MOUNTS_BY = ("clip", "adhesive", "stack", "rail", "edge_card", "castellated",
             "cable", "none")
_HOLE_FP = re.compile(r"mount|standoff|screw.?hole|spacer", re.I)
_HOLE_REF = re.compile(r"^(H|MH|MTG|HOLE)\d+$", re.I)
_EDGE_FP = re.compile(r"Connector_PCBEdge|CardEdge|Card_Edge|EdgeConn|Edge_Conn|"
                      r"_Bus_Edge|_EDGE$|PCIe_x|M\.2_Key|BUS_PCIe|MiniPCIe|"
                      r"_Bus_(Pin_)?Header", re.I)
TAB_MAX_FRAC = 0.15      # a tab's run along its edge, of the edge's length
TAB_MIN_DEPTH_MM = 0.3   # how far the edge between tabs sits back
TAB_MAX_DEPTH_MM = 3.0
TAB_MIRROR_MM = 0.5      # one edge's tab runs against the other edge's
EDGE_CONNECT_MM = 1.0   # a KiCad "connect" (edge connector) pad this near


def declared(constraints: dict | None) -> dict | None:
    m = ((constraints or {}).get("mechanical") or {}).get("mounting")
    if m is None:
        return None
    if not (isinstance(m, dict) and m.get("by") in MOUNTS_BY
            and isinstance(m.get("why"), str) and m["why"].strip()):
        raise checklib.CheckError(
            "constraints mechanical.mounting must be {by: one of "
            f"{', '.join(MOUNTS_BY)}, why: text}}")
    return m


def mounting_holes(bg) -> list[dict]:
    """Every place a screw can go: a hole-only footprint, a lone screw-sized
    hole, a round Edge.Cuts window of screw size."""
    out = []
    by_ref: dict[str, list] = {}
    for p in bg.pads_of():
        by_ref.setdefault(p.ref, []).append(p)
    for ref, pads in by_ref.items():
        lib = bg.footprints.get(ref, {}).get("lib", "")
        drilled = [p for p in pads if p.drill is not None]
        if not drilled:
            continue
        big = [p for p in drilled if HOLE_MIN_MM - 1e-6 <= min(p.drill)
               <= HOLE_MAX_MM + 1e-6 and abs(p.drill[0] - p.drill[1]) < 1e-6]
        named = bool(_HOLE_FP.search(lib.rpartition(":")[2])
                     or lib.startswith("MountingHole:")
                     or _HOLE_REF.match(ref))
        # a footprint that is only a hole: screw-sized holes, and around
        # them nothing but vias or copper on the hole's own net (a
        # MountingHole_..._Pad_Via ring). A part with its own screw holes
        # (a D-sub's "MountingHoles", a jack's pegs) has signal pins too.
        hole_only = bool(big) and all(
            p in big or (p.net == big[0].net and (
                p.drill is None or min(p.drill) < 1.0))
            for p in pads)
        lone = hole_only and len(big) == 1
        if (named and hole_only) or lone:
            p = max(drilled, key=lambda q: min(q.drill))
            out.append({"ref": ref, "at": [checklib.rnd(c) for c in p.center],
                        "drill_mm": checklib.rnd(min(p.drill)),
                        "how": "footprint" if named else "lone hole"})
    outline = bg.outline
    for face in getattr(bg, "outline_faces", [])[1:]:
        if outline.is_empty or not outline.contains(face.representative_point()):
            continue
        minx, miny, maxx, maxy = face.bounds
        w, h = maxx - minx, maxy - miny
        d = 2.0 * math.sqrt(face.area / math.pi)
        if abs(w - h) < 0.1 * max(w, h) and HOLE_MIN_MM - 1e-6 <= d \
                <= HOLE_MAX_MM + 0.3:
            c = face.centroid
            out.append({"ref": None, "at": [checklib.rnd(c.x), checklib.rnd(c.y)],
                        "drill_mm": checklib.rnd(d), "how": "Edge.Cuts window"})
    return out


def edge_hull(pcb: Path):
    """Convex hull of every point drawn on Edge.Cuts (board and footprint
    graphics), or None when there are fewer than three."""
    import sexpdata
    from shapely.geometry import MultiPoint
    root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    pts: list[tuple[float, float]] = []

    def walk(n):
        if not geom._is_node(n):
            return
        head = str(geom._tok(n[0])) if n else ""
        if head.startswith(("gr_", "fp_")) and any(
                geom._is_node(k) and str(geom._tok(k[0])) == "layer"
                and any(str(geom._tok(x)) == "Edge.Cuts" for x in k[1:])
                for k in n[1:]):
            for k in n[1:]:
                if geom._is_node(k) and str(geom._tok(k[0])) in (
                        "start", "end", "mid", "center", "xy"):
                    v = geom._nums(k)
                    if len(v) >= 2:
                        pts.append((v[0], v[1]))
                elif geom._is_node(k) and str(geom._tok(k[0])) == "pts":
                    # (xy ..) points, and KiCad 8+'s (arc (start)(mid)(end))
                    for q in k[1:]:
                        if not geom._is_node(q):
                            continue
                        if str(geom._tok(q[0])) == "xy":
                            v = geom._nums(q)
                            pts.append((v[0], v[1]))
                        elif str(geom._tok(q[0])) == "arc":
                            for e in q[1:]:
                                v = geom._nums(e) if geom._is_node(e) else []
                                if len(v) >= 2:
                                    pts.append((v[0], v[1]))
            return
        for k in n[1:]:
            walk(k)
    walk(root)
    if len(pts) < 3:
        return None
    hull = MultiPoint(pts).convex_hull
    return hull if hull.area > 0 else None


def _fp_libs(pcb: Path) -> list[str]:
    """Every footprint's lib id, read from the file: BoardGeom keys parts by
    reference, and a board whose references it can't read ("?") keeps one."""
    return re.findall(r'^\s*\((?:footprint|module) "?([^"\s)]+)',
                      pcb.read_text(encoding="utf-8", errors="replace"), re.M)


def edge_fingers(bg, pcb: Path) -> dict | None:
    """The longest row of plated fingers running to the board edge, per copper
    side, when one reaches EDGE_FINGERS; or an edge-card footprint."""
    for lib in _fp_libs(pcb):
        if _EDGE_FP.search(lib):
            ref = next((r for r, fp in bg.footprints.items()
                        if fp.get("lib") == lib), None)
            return {"ref": ref, "lib": lib, "fingers": None}
    drilled = [p.center for p in bg.pads_of() if p.drill is not None]
    ring = bg.outline.exterior if not bg.outline.is_empty else None
    if ring is None:
        return None
    best = None
    pads = {layer: [p for p in bg.pads_of(layer=layer)
                    if p.kind in ("smd", "connect") and not p.castellated]
            for layer in ("F.Cu", "B.Cu")}
    for layer, other in (("F.Cu", "B.Cu"), ("B.Cu", "F.Cu")):
        by_ref: dict[str, int] = {}
        for p in pads[layer]:
            d = p.poly.distance(ring)
            if p.kind == "connect":
                # KiCad's edge-connector pad type: no paste, no doubt
                ok = d <= EDGE_CONNECT_MM
            else:
                # a plain SMD pad at the edge is a finger only with its twin
                # on the other side (a module soldered at the edge, a Pico,
                # has pads on one side)
                ok = d <= EDGE_TOUCH_MM and any(
                    math.dist(p.center, q.center) < 0.3 for q in pads[other]) \
                    and not any(math.dist(p.center, c) < 0.3 for c in drilled)
            if ok:
                by_ref[p.ref] = by_ref.get(p.ref, 0) + 1
        for ref, n in by_ref.items():
            if n >= EDGE_FINGERS and (best is None or n > best["fingers"]):
                best = {"ref": ref, "lib": bg.footprints.get(ref, {}).get("lib"),
                        "fingers": n, "layer": layer}
    return best


def _runs(outline, band, along_x: bool) -> list[tuple[float, float]]:
    """The runs (lo, hi) along an edge where OUTLINE fills BAND."""
    hit = outline.intersection(band)
    out = []
    for g in getattr(hit, "geoms", [hit]):
        if g.is_empty or g.area <= 0:
            continue
        x0, y0, x1, y1 = g.bounds
        out.append((x0, x1) if along_x else (y0, y1))
    out.sort()
    merged: list[list[float]] = []
    for lo, hi in out:
        if merged and lo <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


def rail_tabs(outline) -> dict | None:
    """Mirrored tabs on two opposite edges (the docstring's `rail`), or
    None."""
    from shapely.geometry import box  # noqa: PLC0415
    if outline.is_empty:
        return None
    minx, miny, maxx, maxy = outline.bounds
    e = 0.05
    for along_x, lo_band, hi_band, length, edge_lo, edge_hi in (
            (True, lambda d: box(minx, miny + d, maxx, miny + d + e),
             lambda d: box(minx, maxy - d - e, maxx, maxy - d), maxx - minx,
             "bottom", "top"),
            (False, lambda d: box(minx + d, miny, minx + d + e, maxy),
             lambda d: box(maxx - d - e, miny, maxx - d, maxy), maxy - miny,
             "left", "right")):
        sides = []
        for band in (lo_band, hi_band):
            runs = _runs(outline, band(0.0), along_x)
            if len(runs) < 2 or any(hi - lo > TAB_MAX_FRAC * length
                                    for lo, hi in runs):
                break
            # the edge between the outer runs sits back by a tab's depth:
            # gone at TAB_MIN_DEPTH_MM, whole by TAB_MAX_DEPTH_MM
            span = (runs[0][1], runs[-1][0])
            def covered(d, band=band):
                return any(lo <= span[0] + 0.05 and hi >= span[1] - 0.05
                           for lo, hi in _runs(outline, band(d), along_x))
            if covered(TAB_MIN_DEPTH_MM) or not covered(TAB_MAX_DEPTH_MM):
                break
            sides.append(runs)
        else:
            a, b = sides
            if len(a) == len(b) and all(
                    abs(p[0] - q[0]) <= TAB_MIRROR_MM
                    and abs(p[1] - q[1]) <= TAB_MIRROR_MM
                    for p, q in zip(a, b)):
                return {"edges": [edge_lo, edge_hi], "tabs": len(a) * 2,
                        "runs_mm": [[checklib.rnd(lo), checklib.rnd(hi)]
                                    for lo, hi in a]}
    return None


def check(pcb: Path, constraints: dict | None = None
          ) -> tuple[list[dict], dict]:
    bg = geom.BoardGeom.from_file(pcb)
    estimated = bg.outline.is_empty
    if estimated:
        # an outline that doesn't close (a KiCad 5 arc geom can't read, a
        # hairline gap): the hull of every Edge.Cuts point stands in for it
        hull = edge_hull(pcb)
        if hull is None:
            raise checklib.CheckError(f"{pcb}: no Edge.Cuts outline")
        bg.outline = hull
    minx, miny, maxx, maxy = bg.outline.bounds
    w, h = maxx - minx, maxy - miny
    area = bg.outline.area
    holes = mounting_holes(bg)
    seen = {"outline_mm": [checklib.rnd(w), checklib.rnd(h)],
            "outline_estimated": estimated,
            "outline_area_mm2": checklib.rnd(area),
            "min_area_mm2": MIN_AREA_MM2, "min_side_mm": MIN_SIDE_MM,
            "mounting_holes": holes, "mounts_by": None}
    big = area >= MIN_AREA_MM2 or max(w, h) >= MIN_SIDE_MM
    seen["judged"] = big
    if holes or not big:
        return [], seen
    dec = declared(constraints)
    if dec is not None:
        seen["mounts_by"] = {"by": dec["by"], "why": dec["why"],
                             "from": "constraints"}
        return [], seen
    n_cast = castellation.count(pcb)
    if n_cast:
        seen["mounts_by"] = {"by": "castellated", "pads": n_cast,
                             "refs": castellation.refs(pcb)}
        return [], seen
    if not estimated:
        tabs = rail_tabs(bg.outline)
        if tabs is not None:
            seen["mounts_by"] = {"by": "rail", "from": "edge tabs", **tabs}
            return [], seen
    edge = edge_fingers(bg, pcb)
    if edge is not None:
        seen["mounts_by"] = {"by": "edge_card", **edge}
        return [], seen
    return [checklib.violation(
        SCRIPT, "warning", (checklib.rnd((minx + maxx) / 2),
                            checklib.rnd((miny + maxy) / 2)),
        "Edge.Cuts", None, [],
        f"{w:.0f} x {h:.0f} mm board ({area:.0f} mm2) with no mounting hole: "
        "no MountingHole footprint, no lone M2-M5 hole and no round "
        "Edge.Cuts window. Add M2.5/M3 holes at the corners, or say how it "
        "mounts in constraints mechanical.mounting {by, why}",
        SOURCE, kind="no_mounting_holes", outline_mm=[checklib.rnd(w),
                                                      checklib.rnd(h)])], seen


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons = checklib.load_json(args.constraints, "constraints") \
        if args.constraints else None
    vios, seen = check(pcb, cons)
    return checklib.report(
        SCRIPT, pcb, vios, checked=[seen],
        nothing_to_judge=None if vios or seen["judged"] else
        f"the board is under {MIN_AREA_MM2:g} mm2 and {MIN_SIDE_MM:g} mm a "
        "side, too small to need mounting holes"), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
