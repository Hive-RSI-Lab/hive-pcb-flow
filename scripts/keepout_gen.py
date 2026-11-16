"""keepout_gen - copper keep-outs (KiCad rule areas) from constraints.json
(SPEC P7).

constraints["placement"]["keepouts"] steer placement only. An entry that also
says "copper": true becomes a rule area on its side's copper layers ("side"
front -> F.Cu, back -> B.Cu, both or absent -> every copper layer) where no
track, via or pour may go; pads and footprints stay allowed, so an antenna's
own module can sit at its edge. Freerouting gets rule areas as DSN keepouts,
stitch_vias and planes_gen keep out of them, and DRC grades them.
"copper" may instead be a dict naming what is kept out, e.g. {"tracks": true,
"vias": true, "pours": false}. An optional "name" labels the area. An
optional "layers" list (copper layer names, e.g. ["In1.Cu", "In2.Cu"])
overrides "side" for the copper only: a heater coupon's inner layers
cleared under a top-copper island, a hot loop forced onto F.Cu.

--edge-band W (or constraints placement.edge_band_mm) adds a copper keep-out
band W mm wide along the board edge (was ultra-front's pinned
edge_keepout.py): no tracks and no vias, but pours, pads and footprints
allowed, on every copper layer or on --edge-layers (placement
.edge_band_layers). It follows the real Edge.Cuts outline - arcs, notches,
and the windows cut inside it - not its bounding box. KiCad rule areas take
no holes, so the ring is cut into hole-free pieces named aiee_keepout__edge_*.
A rerun that names no band keeps the one already drawn; --edge-band 0
removes it. The payload counts tracks and vias already inside it.

Run it after placement and before planes_gen/route_auto (the ESP32 antenna on
lite-main, hifi-receiver-2, was drawn by a board-local script). Idempotent:
every area it drew before (named aiee_keepout_*) is replaced.

Contract (SPEC section 6):
  keepout_gen.py --pcb B.kicad_pcb [--constraints c.json] [--dry-run]
      [--edge-band W] [--edge-layers F.Cu,B.Cu] [--out-report r.json]
  Payload: {areas: [{name, layers, reason, rect|poly}], removed,
  edge_band: {width_mm, layers, pieces, area_mm2, inside: {tracks, vias}}
  | null}. exit 0;
  2 = error (board untouched).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

from shapely.geometry import Polygon, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

import checklib  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import routelib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "keepout_gen"
PREFIX = "aiee_keepout"
KEPT = ("tracks", "vias", "pours")
# plan_areas strips a label's leading "_", so no keepout name reaches this
EDGE = f"{PREFIX}__edge"
AREA_EPS = 1e-6          # mm2: slivers below this are dropped


def plan_areas(constraints: dict, copper_layers: list[str]) -> list[dict]:
    """constraints placement keepouts with "copper" -> set_rule_areas
    entries (pure)."""
    out = []
    keeps = ((constraints.get("placement") or {}).get("keepouts") or [])
    for i, k in enumerate(keeps):
        cu = k.get("copper")
        if not cu:
            continue
        side = k.get("side", "both")
        if side == "front":
            layers = [copper_layers[0]]
        elif side == "back":
            layers = [copper_layers[-1]]
        elif side == "both":
            layers = list(copper_layers)
        else:
            raise CheckError(f"placement.keepouts[{i}]: side {side!r} is "
                             "not front, back or both")
        if k.get("layers"):
            bad = [l for l in k["layers"] if l not in copper_layers]
            if bad:
                raise CheckError(f"placement.keepouts[{i}]: layers {bad} are "
                                 f"not copper layers of this board")
            layers = [l for l in copper_layers if l in k["layers"]]
        label = re.sub(r"[^A-Za-z0-9_]+", "_",
                       str(k.get("name") or k.get("reason") or i)).strip("_")
        area = {"name": f"{PREFIX}_{label or i}", "layers": layers,
                "reason": k.get("reason", "")}
        if isinstance(cu, dict):
            area.update({w: bool(cu.get(w, False)) for w in KEPT})
        else:
            area.update({w: True for w in KEPT})
        if "rect" in k:
            x1, y1, x2, y2 = (float(v) for v in k["rect"])
            area["rect"] = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
        elif "poly" in k:
            area["poly"] = [[float(x), float(y)] for x, y in k["poly"]]
        else:
            raise CheckError(f"placement.keepouts[{i}]: needs rect or poly")
        out.append(area)
    names = [a["name"] for a in out]
    for j, a in enumerate(out):          # unique names
        if names.count(a["name"]) > 1:
            a["name"] = f"{a['name']}_{j}"
    return out


def edge_band(faces: list, width: float):
    """The copper within `width` mm of the board edge: the largest
    Edge.Cuts face less the windows inside it, minus its inward offset.
    Follows arcs and notches (a concave corner's band is round)."""
    if not faces or faces[0].is_empty:
        raise CheckError("the board has no closed Edge.Cuts outline for an "
                         "edge band")
    board = faces[0]
    wins = [f for f in faces[1:] if f.within(board)]
    region = board.difference(unary_union(wins)) if wins else board
    inside = region.buffer(-width)
    if inside.area <= AREA_EPS:
        raise CheckError(f"an edge band {width:g} mm wide covers the whole "
                         "board")
    return region.difference(inside)


def _pieces(g) -> list:
    return [q for q in getattr(g, "geoms", [g])
            if q.geom_type == "Polygon" and q.area > AREA_EPS]


def hole_free(g) -> list:
    """`g` cut into polygons without holes (a KiCad rule area has one
    outline): a polygon with a hole is split at a vertical line through
    that hole, until none is left."""
    out, todo = [], _pieces(g)
    while todo:
        p = todo.pop()
        if not p.interiors:
            out.append(p)
            continue
        x = Polygon(p.interiors[0]).representative_point().x
        x0, y0, x1, y1 = p.bounds
        for b in (box(x0 - 1, y0 - 1, x, y1 + 1),
                  box(x, y0 - 1, x1 + 1, y1 + 1)):
            todo += _pieces(p.intersection(b))
    return sorted(out, key=lambda q: (round(q.bounds[1], 3),
                                      round(q.bounds[0], 3)))


def plan_edge_band(faces: list, width: float, layers: list[str]
                   ) -> tuple[list[dict], object]:
    """(set_rule_areas entries, the band) for an edge band (pure)."""
    band = edge_band(faces, width)
    areas = []
    for i, q in enumerate(hole_free(band)):
        pts = [[round(x, 5), round(y, 5)]
               for x, y in q.simplify(1e-5).exterior.coords[:-1]]
        areas.append({"name": f"{EDGE}_{i}", "layers": list(layers),
                      "reason": f"copper edge band {width:g} mm",
                      "tracks": True, "vias": True, "pours": False,
                      "poly": pts})
    return areas, band


def _kept_band(bg) -> list[dict]:
    """The edge band already on the board, as set_rule_areas entries."""
    out = []
    for ra in bg.rule_areas:
        if not str(ra.get("name") or "").startswith(EDGE) \
                or ra["outline"].is_empty:
            continue
        fl = ra.get("flags") or {}
        out.append({"name": ra["name"], "layers": list(ra["layers"]),
                    "reason": "kept",
                    "tracks": fl.get("tracks") == "not_allowed",
                    "vias": fl.get("vias") == "not_allowed",
                    "pours": fl.get("copperpour") == "not_allowed",
                    "poly": [[x, y] for x, y in
                             ra["outline"].exterior.coords[:-1]]})
    return out


def _band_request(args, constraints: dict, copper: list[str]):
    """(width, layers) asked for - CLI first, then constraints - or
    (None, None) when nobody names a band."""
    pl = constraints.get("placement") or {}
    width = args.edge_band if args.edge_band is not None \
        else pl.get("edge_band_mm")
    if width is None:
        return None, None
    if isinstance(width, bool) or not isinstance(width, (int, float)) \
            or not width >= 0 or width == float("inf"):
        raise CheckError(f"edge band width {width!r} must be a number >= 0")
    layers = ([l.strip() for l in args.edge_layers.split(",") if l.strip()]
              if args.edge_layers else pl.get("edge_band_layers")) \
        or list(copper)
    bad = [l for l in layers if l not in copper]
    if bad:
        raise CheckError(f"edge band layers {bad} are not copper layers of "
                         f"this board {copper}")
    return float(width), [l for l in copper if l in layers]


def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints", default=None,
                    help="constraints.json (default: next to the board)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--edge-band", type=float, default=None, metavar="W",
                    help="copper keep-out band W mm wide along the edge "
                         "(no tracks, no vias); 0 removes it")
    ap.add_argument("--edge-layers", default=None,
                    help="comma-separated copper layers for the band "
                         "(default: every copper layer)")
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)

    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    cons = Path(args.constraints) if args.constraints else \
        pcb.parent / "constraints.json"
    constraints = checklib.load_json(cons, "constraints") \
        if cons.is_file() else {}
    bg = geom.BoardGeom.from_file(pcb)
    areas = plan_areas(constraints, bg.copper_layers)
    width, layers = _band_request(args, constraints, bg.copper_layers)
    band_facts, band = None, None
    if width is None:
        band_areas = _kept_band(bg)
        if band_areas:
            band = unary_union([Polygon(a["poly"]) for a in band_areas])
            layers = sorted({l for a in band_areas for l in a["layers"]},
                            key=bg.copper_layers.index)
    elif width > 0:
        band_areas, band = plan_edge_band(bg.outline_faces, width, layers)
    else:
        band_areas = []
    if band is not None:
        on = set(layers)
        band_facts = {
            "width_mm": width, "layers": layers, "kept": width is None,
            "pieces": len(band_areas), "area_mm2": round(band.area, 3),
            # copper already in the band: DRC will flag it
            "inside": {
                "tracks": sum(1 for t in bg.tracks_of() if t.layer in on
                              and t.shape.intersects(band)
                              and not t.shape.touches(band)),
                "vias": sum(1 for v in bg.vias_of()
                            if on & set(v.layers)
                            and v.poly.intersects(band)
                            and not v.poly.touches(band))}}
    old = [ra.get("name") for ra in bg.rule_areas
           if str(ra.get("name") or "").startswith(PREFIX)]
    facts = {"areas": areas, "removed": len(old), "constraints": str(cons),
             "dry_run": args.dry_run, "edge_band": band_facts}
    areas = areas + band_areas
    if not args.dry_run and (areas or old):  # areas: keepouts + band
        cli = env.find_kicad_cli()
        bp = env.find_kicad_python(cli) if cli else None
        if bp is None:
            raise CheckError("KiCad bundled python not found (env.py)")
        stage = Path(tempfile.mkdtemp(prefix=".aiee_keepout_", dir=pcb.parent))
        try:
            staged = stage / pcb.name
            shutil.copy2(pcb, staged)
            res = routelib.run_worker(bp, {
                "verb": "set_rule_areas", "board": str(staged),
                "out": str(staged), "prefix": PREFIX, "areas": areas}, stage)
            back = [ra for ra in geom.BoardGeom.from_file(staged).rule_areas
                    if str(ra.get("name") or "").startswith(PREFIX)]
            got = [ra.get("name") for ra in back]
            if sorted(got) != sorted(a["name"] for a in areas):
                raise CheckError(f"rule areas after the edit {sorted(got)} "
                                 "are not the planned ones (board untouched)")
            if band is not None:
                drawn = unary_union([ra["outline"] for ra in back
                                     if ra["name"].startswith(EDGE)])
                off = drawn.symmetric_difference(band).area
                if off > 1e-3 * band.area + 0.01:
                    raise CheckError(
                        f"the edge band read back differs from the plan by "
                        f"{off:.3f} mm2 (board untouched)")
            os.replace(staged, pcb)
            facts["removed"] = res["removed"]
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    payload = {"script": SCRIPT, "status": "pass", "board": str(pcb),
               "facts": facts}
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
